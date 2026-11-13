"""Random stock price generator (SAD process map #8).

What the SAD asks for:

  * prices are random and move GRADUALLY up or down while the market is open,
  * prices do NOT change while the market is closed,
  * the price-update time is recorded,
  * an administrator never changes a price after creation: only this generator does.

How it works here:

  * ``next_price`` takes a stock's current price and returns a slightly different
    one: a random step of up to ``max_step_bps`` basis points (50 = 0.5%) either
    way, worked out with exact Decimal math (never float) and rounded to the cent.
  * ``tick`` is one pass over every stock. If the market is closed it does nothing
    at all. Otherwise each stock gets its own short database transaction that
    locks just that stock's row, so a customer's trade on the same stock either
    finishes first or sees the new price (and is asked to review the order again).
  * ``run_loop`` repeats ``tick`` on a timer. It is run as its own process
    (``flask --app app run-price-generator``), not inside the web server, so there
    is exactly one generator no matter how many web workers are running.

No Flask imports. The clock and the random source are passed in, so the tests are
fully repeatable.
"""

import random
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal

from app import db
from app.money import MAX_MONEY, quantize, to_money
from app.services import market

DEFAULT_TICK_SECONDS = 60
DEFAULT_MAX_STEP_BPS = 50  # 0.5% per tick at most
MAX_ALLOWED_STEP_BPS = 1000  # never allow more than a 10% jump in one tick

MIN_PRICE = Decimal("0.01")  # the database requires price > 0
MAX_PRICE = MAX_MONEY  # the largest value the DECIMAL(10,2) column holds

_BPS_BASE = Decimal(10000)


@dataclass(frozen=True)
class TickResult:
    """What one ``tick`` did."""

    market_open: bool
    stocks_updated: int  # stocks that were visited (their update time was stamped)
    prices_changed: int  # of those, how many actually got a different price


def next_price(current, rng, max_step_bps=DEFAULT_MAX_STEP_BPS):
    """Return the stock's next price: ``current`` moved by a random step of up to ``max_step_bps``.

    ``rng`` is a ``random.Random`` (or anything with ``randint``). The step is a
    whole number of basis points chosen uniformly from -max..+max, so up and down
    are equally likely. The result is always a positive Decimal with 2 decimals,
    between MIN_PRICE and MAX_PRICE. If a non-zero step would round away to nothing
    (cheap stocks), the price still moves by one cent so it keeps fluctuating.
    """
    if not 1 <= max_step_bps <= MAX_ALLOWED_STEP_BPS:
        raise ValueError(f"max_step_bps must be between 1 and {MAX_ALLOWED_STEP_BPS}")
    current = to_money(current)
    bps = rng.randint(-max_step_bps, max_step_bps)
    moved = quantize(current * (_BPS_BASE + bps) / _BPS_BASE)
    if moved == current and bps != 0:
        moved = current + (MIN_PRICE if bps > 0 else -MIN_PRICE)
    return min(max(moved, MIN_PRICE), MAX_PRICE)


def tick(conn, now, tz_name, rng, max_step_bps=DEFAULT_MAX_STEP_BPS):
    """Run one price update. Does NOTHING while the market is closed.

    ``now`` is the timezone-aware UTC time (also written as each stock's
    price-update time). Each stock is handled in its own transaction: lock the
    row, read the price as it is right now, write the next one.
    """
    if not market.is_market_open(conn, now, tz_name):
        return TickResult(market_open=False, stocks_updated=0, prices_changed=0)

    stamp = now.isoformat()
    visited = 0
    changed = 0
    for row in db.list_stocks(conn):
        with db.transaction(conn):
            stock = db.get_stock(conn, row["stock_id"], for_update=True)
            if stock is None:
                continue
            old = to_money(stock["current_price"])
            new = next_price(old, rng, max_step_bps)
            db.update_stock_price(conn, stock["stock_id"], new, stamp)
        visited += 1
        if new != old:
            changed += 1
    return TickResult(market_open=True, stocks_updated=visited, prices_changed=changed)


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

    A fresh database connection is opened for each tick, so the loop survives the
    database restarting. If a tick fails the error is reported to ``on_error`` and
    the loop carries on after the normal wait. ``sleep``, ``now_fn`` and
    ``max_ticks`` exist so tests can run the loop instantly. Returns the number
    of ticks attempted.
    """
    rng = rng or random.Random()
    sleep = sleep or time.sleep  # looked up now, not when the function was defined, so tests can replace it
    now_fn = now_fn or (lambda: datetime.now(timezone.utc))
    attempted = 0
    while max_ticks is None or attempted < max_ticks:
        attempted += 1
        try:
            conn = open_connection()
            try:
                result = tick(conn, now_fn(), tz_name, rng, max_step_bps)
            finally:
                conn.close()
            if on_tick:
                on_tick(result)
        except Exception as exc:  # noqa: BLE001 - keep the generator alive whatever goes wrong
            if on_error:
                on_error(exc)
        if max_ticks is None or attempted < max_ticks:
            sleep(seconds)
    return attempted
