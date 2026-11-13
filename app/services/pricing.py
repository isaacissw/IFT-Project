"""Random stock price generator (SAD process map #8).

=============================================================================
WHAT THE SAD AND THE BUSINESS CASE ASK FOR
=============================================================================
  * "The system will generate a random market price for each stock."
  * "The system will fluctuate stock prices GRADUALLY during open market hours."
  * "The system will maintain STATIC stock prices while the market is closed."
  * Record the time each price was updated.
  * An administrator sets only the INITIAL price; after that, ONLY this
    generator changes a price (enforced by tests/test_price_rules.py).

=============================================================================
THE BIG IDEA IN ONE SENTENCE
=============================================================================
Every minute, if the market is open, each stock's price takes one tiny random
step up or down (at most 0.5%), and the new price and the time are saved.

=============================================================================
THE THREE PIECES OF THIS FILE (read them in this order)
=============================================================================

    run_loop()  ->  repeats forever:  tick(), then wait, then tick(), then wait ...
       |
       v
    tick()      ->  ONE round of updates:
                      "Is the market open?"  No  -> do nothing at all.
                                             Yes -> for every stock:
                                                      lock it, read its price,
                                                      next_price(), save, unlock.
       |
       v
    next_price() -> the math for ONE stock: old price -> new price.

The command you type (``flask --app app run-price-generator``, in app/cli.py)
just calls run_loop(). The website never calls any of this.

=============================================================================
A KEY TERM: "BASIS POINT" (bps)
=============================================================================
One basis point is one hundredth of one percent (0.01%). It lets us talk about
small percentages with whole numbers, which is simpler and exact:

      1 bps  = 0.01%     ->  multiply the price by 1.0001
     25 bps  = 0.25%     ->  multiply by 1.0025
     50 bps  = 0.50%     ->  multiply by 1.0050   (the default biggest step)
    -50 bps  = -0.50%    ->  multiply by 0.9950

=============================================================================
WHY THIS DESIGN (the questions people usually ask)
=============================================================================
Q: Why a separate program instead of part of the website?
A: The website only runs code when someone clicks something. Prices must change
   on a clock even when nobody is clicking, so they need their own process with
   its own timer. Keeping it separate also guarantees exactly ONE generator: on
   AWS the website may run as several copies, and if each copy moved prices,
   prices would move several times per minute.

Q: Why is it "random" and "gradual" at the same time?
A: Each step is random (up or down equally likely), but small (at most 0.5%).
   Many small random steps make a wandering line, like a real stock chart,
   instead of wild jumps. This is called a "random walk".

Q: Why Decimal and not normal float numbers?
A: Floats store most decimals slightly wrong (0.1 + 0.2 is 0.30000000000000004).
   Money must be exact to the cent, so all prices are Decimal.

Q: What happens to a customer who is in the middle of a trade?
A: Their review screen remembers the price they saw. If a tick changes that
   price before they click Confirm, the confirm step notices the difference,
   refuses with "The price changed ... please review your order again", and
   charges nothing (see confirm_buy in app/services/trading.py).

Q: Why does each stock get its own small database transaction?
A: While a stock's row is being updated it is LOCKED, so a customer confirming
   a trade on that same stock waits a split second. Doing one stock at a time
   keeps each lock tiny, and if something fails halfway, every stock is still
   in a valid state (each one either got its new price or kept its old one).

No Flask imports. The clock (``now``) and the random source (``rng``) are passed
in from outside, so tests can replay exactly the same "random" moves every time.
"""

import random
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal

from app import db
from app.money import MAX_MONEY, quantize, to_money
from app.services import market

# ---------------------------------------------------------------------------
# Settings (the defaults; .env can change the first two, see app/config.py)
# ---------------------------------------------------------------------------
DEFAULT_TICK_SECONDS = 60     # how long to wait between rounds of updates
DEFAULT_MAX_STEP_BPS = 50     # biggest single move: 50 bps = 0.5%
MAX_ALLOWED_STEP_BPS = 1000   # safety cap: nobody may configure more than 10% per move

MIN_PRICE = Decimal("0.01")   # the database requires price > 0, so never go below 1 cent
MAX_PRICE = MAX_MONEY         # 99,999,999.99: the largest number the price column can hold

_BPS_BASE = Decimal(10000)    # 10000 bps = 100%; dividing by this turns bps into a fraction


@dataclass(frozen=True)
class TickResult:
    """A small report of what one round (tick) did, printed by the command."""

    market_open: bool     # False means the round was skipped because the market was closed
    stocks_updated: int   # how many stocks were visited (each got a new "price updated" time)
    prices_changed: int   # of those, how many ended up with a DIFFERENT price (a roll of 0 = same price)


# ===========================================================================
# PIECE 1: next_price  -- the math for one stock
# ===========================================================================


def next_price(current, rng, max_step_bps=DEFAULT_MAX_STEP_BPS):
    """Return the stock's next price: ``current`` moved by a random step of up to ``max_step_bps``.

    Worked examples with the default (max 50 bps = 0.5%), SSOL at $42.50:

        random roll   meaning     calculation                    raw result   saved price
        -----------   ---------   ----------------------------   ----------   -----------
            +37       +0.37%      42.50 x (10000+37)/10000       42.65725     $42.66
            -12       -0.12%      42.50 x (10000-12)/10000       42.44900     $42.45
            +50       +0.50%      42.50 x 10050/10000            42.71250     $42.71
              0        0.00%      42.50 x 10000/10000            42.50000     $42.50 (no change)

    Two safety rules on top of the math:
      * A cheap stock still moves. For a $0.50 stock, +5 bps is 0.50 x 1.0005 =
        0.50025, which rounds back to $0.50: the price would never change. So when
        the roll is not zero but rounding wiped out the move, we move 1 cent in the
        roll's direction instead ($0.51 for an up roll, $0.49 for a down roll).
      * The result is kept between MIN_PRICE ($0.01) and MAX_PRICE, so a price can
        never become zero, negative, or too big for the database.
    """
    # Refuse silly settings (for example a 0% or a 90% step) before doing anything.
    if not 1 <= max_step_bps <= MAX_ALLOWED_STEP_BPS:
        raise ValueError(f"max_step_bps must be between 1 and {MAX_ALLOWED_STEP_BPS}")

    # Make sure we are working with an exact Decimal (it may arrive as text).
    current = to_money(current)

    # STEP 1 - the random roll: a whole number from -max to +max, e.g. -50..+50.
    # Every value is equally likely, so up and down are equally likely, which
    # means prices wander rather than drift in one direction.
    bps = rng.randint(-max_step_bps, max_step_bps)

    # STEP 2 - apply it as a percentage, then round to the nearest cent.
    #   (10000 + bps) / 10000 turns the roll into a multiplier:
    #   +37 -> 1.0037, -12 -> 0.9988, 0 -> 1.0000.
    moved = quantize(current * (_BPS_BASE + bps) / _BPS_BASE)

    # STEP 3 - the cheap-stock rule: a non-zero roll that rounded away to
    # "no change" becomes a 1-cent move in the roll's direction.
    if moved == current and bps != 0:
        moved = current + (MIN_PRICE if bps > 0 else -MIN_PRICE)

    # STEP 4 - keep the price inside the allowed range ($0.01 .. column maximum).
    return min(max(moved, MIN_PRICE), MAX_PRICE)


# ===========================================================================
# PIECE 2: tick  -- one round of updates for every stock
# ===========================================================================


def tick(conn, now, tz_name, rng, max_step_bps=DEFAULT_MAX_STEP_BPS):
    """Run one round of price updates. Does NOTHING while the market is closed.

    Arguments:
      conn          an open database connection (from app/db.py)
      now           the current time (timezone-aware, UTC); also saved as each
                    stock's "price updated" time
      tz_name       the market's time zone, e.g. "America/New_York", used to decide
                    whether the market is open (its hours are in that time zone)
      rng           the random number source (random.Random); passed in so tests
                    can make the "random" rolls repeatable
      max_step_bps  biggest move per tick (50 = 0.5%)

    Example of one round at 10:00 AM on a weekday (market open), two stocks:

        SSOL  $42.50  -> lock row -> roll +37 -> $42.66 -> save, stamp 10:00 -> unlock
        GCNY  $12,500 -> lock row -> roll -20 -> $12,475.00 -> save, stamp 10:00 -> unlock
        result: TickResult(market_open=True, stocks_updated=2, prices_changed=2)

    Same call at 8:00 PM (market closed):
        result: TickResult(market_open=False, stocks_updated=0, prices_changed=0)
        and the database is not touched at all (not even the timestamps).
    """
    # STEP 1 - market check. This is the SAME function the trading code uses, so
    # "open" means exactly the same thing for prices and for trades: the admin's
    # manual override first, otherwise the weekly schedule.
    if not market.is_market_open(conn, now, tz_name):
        return TickResult(market_open=False, stocks_updated=0, prices_changed=0)

    stamp = now.isoformat()  # e.g. "2026-03-02T15:00:00+00:00"; stored as the update time
    visited = 0
    changed = 0

    # STEP 2 - every stock, including ones the administrator created recently.
    for row in db.list_stocks(conn):
        # STEP 3 - one short transaction per stock (everything inside the
        # "with" block is saved together, or undone together on an error).
        with db.transaction(conn):
            # Lock this stock's row and re-read its price. Re-reading matters:
            # the price we got from list_stocks a moment ago could already be
            # out of date, and the lock makes sure no trade confirm on this stock
            # runs at the same moment as our update.
            stock = db.get_stock(conn, row["stock_id"], for_update=True)
            if stock is None:
                continue  # the stock disappeared in between (cannot happen today; just being safe)
            old = to_money(stock["current_price"])
            new = next_price(old, rng, max_step_bps)
            # Save the new price and the time. db.update_stock_price is the ONLY
            # code in the whole app allowed to change a price after creation.
            db.update_stock_price(conn, stock["stock_id"], new, stamp)
        # Leaving the "with" block commits this stock's change and releases its lock.
        visited += 1
        if new != old:
            changed += 1
    return TickResult(market_open=True, stocks_updated=visited, prices_changed=changed)


# ===========================================================================
# PIECE 3: run_loop  -- repeat tick() on a timer
# ===========================================================================


def run_loop(
    open_connection,
    tz_name,
    seconds=DEFAULT_TICK_SECONDS,
    max_step_bps=DEFAULT_MAX_STEP_BPS,
    rng=None,
    sleep=None,
    now_fn=None,
    on_tick=None,
    on_error=None,
    max_ticks=None,
):
    """Call ``tick`` every ``seconds`` seconds, forever (or ``max_ticks`` times).

    What one pass of the loop does:
        1. open a FRESH database connection,
        2. run tick() with the current time,
        3. close the connection,
        4. tell the caller what happened (on_tick prints the "market OPEN: ..." line),
        5. wait ``seconds``, then go back to 1.

    Why a fresh connection each time? If MySQL restarts, an old connection would be
    dead forever; a new one each round lets the generator recover by itself.

    Why catch every error? A generator that crashes on one bad round would leave
    prices frozen until someone noticed. Instead the error is reported (on_error
    prints "price update failed ...; will retry") and the loop carries on.

    The extra parameters exist mainly for the tests:
      sleep      the waiting function (tests pass one that does not really wait)
      now_fn     where "the current time" comes from (tests pass a fixed time)
      max_ticks  stop after this many rounds (None = run forever; --once uses 1)

    Returns the number of rounds attempted.
    """
    # Fill in the real defaults HERE, at the moment the loop runs (not in the
    # parameter list above), so a test can replace time.sleep before calling us.
    rng = rng or random.Random()
    sleep = sleep or time.sleep
    now_fn = now_fn or (lambda: datetime.now(timezone.utc))

    attempted = 0
    while max_ticks is None or attempted < max_ticks:
        attempted += 1
        try:
            conn = open_connection()
            try:
                result = tick(conn, now_fn(), tz_name, rng, max_step_bps)
            finally:
                conn.close()  # always give the connection back, even after an error
            if on_tick:
                on_tick(result)
        except Exception as exc:  # noqa: BLE001 - keep the generator alive whatever goes wrong
            if on_error:
                on_error(exc)
        # Wait before the next round, but not after the very last one (so --once
        # exits immediately instead of sleeping for a minute first).
        if max_ticks is None or attempted < max_ticks:
            sleep(seconds)
    return attempted
