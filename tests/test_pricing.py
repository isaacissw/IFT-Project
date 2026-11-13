"""Tests for the random price generator (app/services/pricing.py).

The random source and the clock are always passed in, so every result here is
repeatable. ``FixedRng`` always "rolls" the same step; ``random.Random(seed)``
gives a repeatable random sequence.
"""

import random
import threading
from datetime import datetime, timezone
from decimal import Decimal

import pytest
from hypothesis import given, strategies as st

from app import db
from app.services import market, pricing
from app.services.pricing import MAX_PRICE, MIN_PRICE, TickResult, next_price, run_loop, tick
from app.services.trading import PriceChangedError, confirm_buy, create_pending_buy
from dbhelpers import set_override, set_price, snapshot, stock_id, test_database_url, trainee_id, use_business_hours

D = Decimal
NOW = datetime(2026, 3, 2, 15, 0, 0, tzinfo=timezone.utc)  # Monday 10:00 AM New York
LATER = datetime(2026, 3, 2, 15, 1, 0, tzinfo=timezone.utc)
TZ = "America/New_York"


class FixedRng:
    """A 'random' source that always picks the same step (in basis points)."""

    def __init__(self, bps):
        self.bps = bps

    def randint(self, low, high):
        assert low <= self.bps <= high
        return self.bps


def _prices(conn):
    return {s["ticker_symbol"]: s["current_price"] for s in db.list_stocks(conn)}


# ======================== next_price (pure math) ===============================


@pytest.mark.parametrize(
    "price, bps, expected",
    [
        ("100.00", 50, "100.50"),     # +0.5%
        ("100.00", -50, "99.50"),     # -0.5%
        ("42.50", 50, "42.71"),       # 42.7125 rounds to 42.71
        ("42.50", -50, "42.29"),      # 42.2875 rounds to 42.29
        ("100.00", 0, "100.00"),      # a zero step changes nothing
        ("12500.00", 25, "12531.25"),
    ],
)
def test_next_price_applies_the_step_exactly(price, bps, expected):
    assert next_price(D(price), FixedRng(bps)) == D(expected)


def test_a_cheap_stock_still_moves_by_a_cent():
    """0.5% of $0.50 is a quarter of a cent, which would round to no move at all."""
    assert next_price(D("0.50"), FixedRng(50)) == D("0.51")
    assert next_price(D("0.50"), FixedRng(-50)) == D("0.49")
    assert next_price(D("0.50"), FixedRng(0)) == D("0.50")  # but a zero step stays put


def test_the_price_never_reaches_zero_or_goes_negative():
    assert next_price(D("0.01"), FixedRng(-50)) == MIN_PRICE
    assert next_price(D("0.01"), FixedRng(-1000), max_step_bps=1000) == MIN_PRICE
    price = D("0.02")
    for _ in range(20):
        price = next_price(price, FixedRng(-50))
    assert price == MIN_PRICE


def test_the_price_never_exceeds_the_column_maximum():
    assert next_price(MAX_PRICE, FixedRng(50)) == MAX_PRICE
    assert next_price(D("99999999.00"), FixedRng(1000), max_step_bps=1000) == MAX_PRICE


def test_money_stays_decimal_never_float():
    result = next_price(D("19.99"), random.Random(1))
    assert isinstance(result, Decimal)
    assert result == result.quantize(D("0.01"))


@pytest.mark.parametrize("bad", [0, -5, 1001, 10_000])
def test_a_silly_step_size_is_refused(bad):
    with pytest.raises(ValueError):
        next_price(D("10.00"), random.Random(1), max_step_bps=bad)


def test_the_same_seed_gives_the_same_prices():
    def walk(seed):
        rng, price, seen = random.Random(seed), D("42.50"), []
        for _ in range(50):
            price = next_price(price, rng)
            seen.append(price)
        return seen

    assert walk(7) == walk(7)
    assert walk(7) != walk(8)


@given(
    cents=st.integers(min_value=1, max_value=99_999_999_99),
    bps=st.integers(min_value=-50, max_value=50),
)
def test_property_every_move_is_small_positive_and_two_decimals(cents, bps):
    """For ANY price and ANY allowed step: the result is valid and the move is gradual."""
    current = D(cents) / 100
    new = next_price(current, FixedRng(bps))
    assert MIN_PRICE <= new <= MAX_PRICE
    assert new == new.quantize(D("0.01"))
    # At most the 0.5% step plus rounding (up to one cent either way).
    assert abs(new - current) <= current * D("0.005") + D("0.01")


def test_random_walk_stays_gradual_over_a_long_run():
    rng, price = random.Random(2026), D("100.00")
    for _ in range(2000):
        new = next_price(price, rng)
        assert abs(new - price) <= price * D("0.005") + D("0.01")
        price = new
    assert price > 0


# ========================= tick (one update pass) ==============================


def test_tick_moves_every_stock_while_the_market_is_open(db_conn):
    before = _prices(db_conn)
    result = tick(db_conn, NOW, TZ, FixedRng(50))
    after = _prices(db_conn)

    assert result == TickResult(market_open=True, stocks_updated=6, prices_changed=6)
    assert after["SSOL"] == D("42.71") and after["DSRT"] == D("129.39") and after["GCNY"] == D("12562.50")
    assert all(after[t] > before[t] for t in before)


def test_tick_records_when_the_price_changed(db_conn):
    tick(db_conn, NOW, TZ, FixedRng(50))
    assert {s["price_updated_at"] for s in db.list_stocks(db_conn)} == {"2026-03-02T15:00:00+00:00"}


def test_tick_changes_nothing_else(db_conn):
    before = snapshot(db_conn)
    tick(db_conn, NOW, TZ, FixedRng(50))
    after = snapshot(db_conn)
    for table in ("users", "holdings", "transactions", "market_schedule", "market_settings"):
        assert after[table] == before[table]
    for old, new in zip(before["stocks"], after["stocks"]):
        changed = {k for k in old if old[k] != new[k]}
        assert changed == {"current_price", "price_updated_at"}  # names, tickers and shares untouched


def test_tick_does_absolutely_nothing_while_the_market_is_closed(db_conn):
    set_override(db_conn, "force_closed")
    before = snapshot(db_conn)
    result = tick(db_conn, NOW, TZ, FixedRng(50))
    assert result == TickResult(market_open=False, stocks_updated=0, prices_changed=0)
    assert snapshot(db_conn) == before  # not even the "updated at" time moved


@pytest.mark.parametrize(
    "label, when_utc",
    [
        ("before opening", datetime(2026, 3, 2, 13, 59, tzinfo=timezone.utc)),
        ("exactly at closing", datetime(2026, 3, 2, 22, 0, tzinfo=timezone.utc)),
        ("Saturday", datetime(2026, 3, 7, 17, 0, tzinfo=timezone.utc)),
    ],
)
def test_prices_hold_outside_the_scheduled_hours(db_conn, label, when_utc):
    use_business_hours(db_conn)
    before = snapshot(db_conn)
    assert not tick(db_conn, when_utc, TZ, FixedRng(50)).market_open
    assert snapshot(db_conn) == before, label


def test_prices_move_again_when_the_market_reopens(db_conn):
    use_business_hours(db_conn)
    saturday = datetime(2026, 3, 7, 17, 0, tzinfo=timezone.utc)
    monday = datetime(2026, 3, 9, 15, 0, tzinfo=timezone.utc)
    start = _prices(db_conn)
    tick(db_conn, saturday, TZ, FixedRng(50))
    assert _prices(db_conn) == start
    tick(db_conn, monday, TZ, FixedRng(50))
    assert _prices(db_conn) != start


def test_manual_override_open_lets_prices_move_on_a_closed_day(db_conn):
    use_business_hours(db_conn)
    set_override(db_conn, "force_open")
    saturday = datetime(2026, 3, 7, 17, 0, tzinfo=timezone.utc)
    assert tick(db_conn, saturday, TZ, FixedRng(50)).prices_changed == 6


def test_a_new_stock_gets_its_prices_moved_too(db_conn):
    from app.services.admin import create_stock

    create_stock(db_conn, db.get_user_by_username(db_conn, "admin")["user_id"], "Acme", "ACME", "100", "10.00", "", NOW)
    assert tick(db_conn, NOW, TZ, FixedRng(50)).stocks_updated == 7
    assert db.get_stock_by_ticker(db_conn, "ACME")["current_price"] == D("10.05")


def test_many_ticks_keep_every_price_valid_and_gradual(db_conn):
    rng = random.Random(99)
    previous = _prices(db_conn)
    for minute in range(60):
        tick(db_conn, datetime(2026, 3, 2, 15, minute, tzinfo=timezone.utc), TZ, rng)
        current = _prices(db_conn)
        for ticker, price in current.items():
            assert price >= MIN_PRICE and price == price.quantize(D("0.01"))
            assert abs(price - previous[ticker]) <= previous[ticker] * D("0.005") + D("0.01")
        previous = current


def test_a_failure_part_way_leaves_every_stock_valid(db_conn, monkeypatch):
    """Each stock is its own transaction: a crash on one does not corrupt the others."""
    real = db.update_stock_price
    calls = {"n": 0}

    def flaky(conn, sid, price, stamp):
        calls["n"] += 1
        if calls["n"] == 3:
            raise RuntimeError("simulated failure")
        return real(conn, sid, price, stamp)

    monkeypatch.setattr(db, "update_stock_price", flaky)
    with pytest.raises(RuntimeError):
        tick(db_conn, NOW, TZ, FixedRng(50))
    prices = _prices(db_conn)
    assert all(p > 0 for p in prices.values())
    assert sum(1 for t, p in prices.items() if p != {"SSOL": D("42.50"), "DSRT": D("128.75"), "CPTR": D("17.20"),
                                                       "MESA": D("310.00"), "SAGU": D("5.00"), "GCNY": D("12500.00")}[t]) == 2


# ===================== prices and trading together ==============================


def test_trades_use_the_new_price_after_a_tick(db_conn):
    user, ssol = trainee_id(db_conn), stock_id(db_conn, "SSOL")
    tick(db_conn, NOW, TZ, FixedRng(50))  # SSOL 42.50 -> 42.71
    tx = create_pending_buy(db_conn, user, ssol, 10, LATER)
    confirm_buy(db_conn, user, tx, LATER)
    assert db.get_user_by_id(db_conn, user)["cash_balance"] == D("9572.90")  # 10000 - 10 * 42.71


def test_an_order_reviewed_before_a_price_move_must_be_reviewed_again(db_conn):
    user, ssol = trainee_id(db_conn), stock_id(db_conn, "SSOL")
    tx = create_pending_buy(db_conn, user, ssol, 10, NOW)
    tick(db_conn, LATER, TZ, FixedRng(50))
    with pytest.raises(PriceChangedError) as err:
        confirm_buy(db_conn, user, tx, LATER)
    assert (err.value.old, err.value.new) == (D("42.50"), D("42.71"))
    assert db.get_user_by_id(db_conn, user)["cash_balance"] == D("10000.00")  # nothing was charged


def test_a_zero_step_leaves_a_reviewed_order_valid(db_conn):
    user, ssol = trainee_id(db_conn), stock_id(db_conn, "SSOL")
    tx = create_pending_buy(db_conn, user, ssol, 10, NOW)
    tick(db_conn, LATER, TZ, FixedRng(0))  # prices unchanged (only the time stamp moves)
    confirm_buy(db_conn, user, tx, LATER)
    assert db.get_user_by_id(db_conn, user)["cash_balance"] == D("9575.00")


def test_a_price_update_racing_a_confirm_never_loses_or_double_charges_money(db_conn):
    """Confirm and a tick at the same instant: either the order completes at the old price,
    or it is refused for a price change. Never a wrong charge."""
    user, ssol = trainee_id(db_conn), stock_id(db_conn, "SSOL")
    outcomes = set()
    for round_ in range(8):
        set_price(db_conn, "SSOL", D("42.50"))
        db_conn.execute("UPDATE users SET cash_balance = 10000 WHERE user_id = ?", (user,))
        db_conn.execute("DELETE FROM holdings")
        tx = create_pending_buy(db_conn, user, ssol, 10, NOW)
        barrier = threading.Barrier(2)
        results = {}

        def do_confirm():
            conn = db.get_connection(test_database_url())
            try:
                barrier.wait(timeout=10)
                confirm_buy(conn, user, tx, LATER)
                results["confirm"] = "ok"
            except PriceChangedError:
                results["confirm"] = "price_changed"
            finally:
                conn.close()

        def do_tick():
            conn = db.get_connection(test_database_url())
            try:
                barrier.wait(timeout=10)
                tick(conn, LATER, TZ, FixedRng(50))
            finally:
                conn.close()

        threads = [threading.Thread(target=do_confirm), threading.Thread(target=do_tick)]
        [t.start() for t in threads]
        [t.join(timeout=30) for t in threads]
        assert not any(t.is_alive() for t in threads)

        cash = db.get_user_by_id(db_conn, user)["cash_balance"]
        holding = db.get_holding(db_conn, user, ssol)
        if results["confirm"] == "ok":
            assert cash == D("9575.00") and holding["quantity"] == 10  # charged exactly the reviewed price
        else:
            assert cash == D("10000.00") and holding is None
        outcomes.add(results["confirm"])
    assert outcomes <= {"ok", "price_changed"}


# ============================ run_loop ===========================================


def _factory():
    return db.get_connection(test_database_url())


def test_run_loop_ticks_on_a_timer_without_really_waiting(db_conn):
    sleeps, results = [], []
    count = run_loop(
        _factory, TZ, seconds=30, rng=FixedRng(50), sleep=sleeps.append, now_fn=lambda: NOW,
        on_tick=results.append, max_ticks=3,
    )
    assert count == 3
    assert sleeps == [30, 30]  # waits between ticks, not after the last one
    assert [r.market_open for r in results] == [True, True, True]
    assert db.get_stock_by_ticker(db_conn, "SSOL")["current_price"] > D("43.00")  # three upward steps


def test_run_loop_survives_a_failed_tick(db_conn):
    attempts, errors, results = {"n": 0}, [], []

    def flaky_factory():
        attempts["n"] += 1
        if attempts["n"] == 2:
            raise ConnectionError("database restarting")
        return _factory()

    count = run_loop(flaky_factory, TZ, rng=FixedRng(50), sleep=lambda s: None, now_fn=lambda: NOW,
                     on_tick=results.append, on_error=errors.append, max_ticks=4)
    assert count == 4 and len(results) == 3 and len(errors) == 1
    assert isinstance(errors[0], ConnectionError)


def test_run_loop_holds_prices_while_closed(db_conn):
    set_override(db_conn, "force_closed")
    before = _prices(db_conn)
    results = []
    run_loop(_factory, TZ, rng=FixedRng(50), sleep=lambda s: None, now_fn=lambda: NOW, on_tick=results.append, max_ticks=5)
    assert [r.market_open for r in results] == [False] * 5
    assert _prices(db_conn) == before
