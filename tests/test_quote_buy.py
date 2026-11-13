# Feature: phase-1-buy-stock, Property 8: Buy cost is the exact quantized product
# Feature: phase-1-buy-stock, Property 9: A quote writes nothing and reports consistent balances
# Feature: phase-1-buy-stock, Property 14: The first failing check in the fixed order is the one reported
"""Tests for ``quote_buy`` and ``compute_total_cost`` in ``app/services/trading.py``.

Covers test task 6.4 / Requirement 18.1 and the following design properties:

  - Property 8 (Requirements 8.1, 8.2, 8.3): the buy total is exactly
    ``quantize(price * quantity)`` using ROUND_HALF_UP, is always a ``Decimal``
    (never float), and a product exceeding 99999999.99 raises ``ValidationError``.
  - Property 9 (Requirements 9.1, 9.2): a successful ``quote_buy`` writes nothing
    to the database and reports ``cash_after == cash_before - total_cost``.
  - Property 14 (Requirement 13.1): when several validations fail at once, the
    one raised is the earliest in the fixed order {stock exists, quantity valid,
    market open, shares available, funds}.
  - Example tests: stock-not-found, insufficient funds (GCNY), insufficient
    shares (51 SAGU), a concrete SSOL quote, and a no-DB-writes check.

Test isolation
--------------
Every example builds its OWN fresh, seeded in-memory database
(``app.db.get_connection(":memory:")`` + ``init_db`` + ``seed_into``), following
the pattern in ``tests/test_market.py`` / ``tests/test_money_decimal.py``. We do
NOT reuse the shared, function-scoped ``db_conn`` fixture, because Hypothesis
reuses a function-scoped fixture across every generated example and the quote
path reads mutable shared state (cash balance, holdings). A fresh per-example
database keeps each example independent, so no health-check suppression is
needed.

The seed loader reads ``SEED_ADMIN_PASSWORD`` / ``SEED_CUSTOMER_PASSWORD`` from
the environment, so this module sets dummy values for both before seeding; the
hashes are never asserted on here.

Clock and market
----------------
``now`` is injected as a timezone-aware UTC ``datetime`` so these tests never
depend on the wall clock. To make the market-open outcome deterministic for the
valid-quote / funds / shares tests we set ``market_settings.override_mode`` to
``force_open`` via a direct UPDATE on the connection; the market-closed ordering
case uses ``force_closed``. (The ``app/db.py`` seam has no settings-update
helper, so the test writes the single settings row directly; this is test setup,
not production code.)
"""

import os
from contextlib import contextmanager
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
from zoneinfo import ZoneInfo

import pytest
from hypothesis import given, settings, example
from hypothesis import strategies as st

# Dummy seed passwords MUST be set before importing/using seed_into so the seed
# loader's _require_env check passes. They are throwaway values for tests only.
os.environ.setdefault("SEED_ADMIN_PASSWORD", "test-admin-password")
os.environ.setdefault("SEED_CUSTOMER_PASSWORD", "test-customer-password")

from app import db  # noqa: E402  (import after env setup is intentional)
from app.money import MAX_MONEY  # noqa: E402
from app.services.trading import (  # noqa: E402
    InsufficientFundsError,
    InsufficientSharesError,
    MarketClosedError,
    ValidationError,
    compute_total_cost,
    quote_buy,
)
from dbhelpers import fresh_db  # noqa: E402

# A timezone-aware UTC instant used as ``now`` throughout. Its exact value does
# not matter because the valid-quote tests force the market open via an
# override; it only needs to be tz-aware UTC.
_NOW = datetime(2024, 1, 3, 17, 0, 0, tzinfo=ZoneInfo("UTC"))

# The cent quantum and rounding mode the service is specified to use.
_CENT = Decimal("0.01")


@contextmanager
def _fresh_db():
    """Yield a connection to a brand-new, seeded, isolated in-memory database.

    Opened through ``app.db.get_connection`` (PARSE_DECLTYPES on, converters
    registered), built from ``schema.sql`` via ``init_db``, and populated with
    the fixed demo data via ``seed_into``. ``:memory:`` means the database lives
    only for the life of this connection, so each caller gets a private database
    with no state shared across examples.
    """
    with fresh_db(seed=True) as conn:
        yield conn


def _set_override(conn, mode):
    """Set ``market_settings.override_mode`` (test setup only).

    ``app/db.py`` exposes no settings-update helper (Phase 1 does not need one),
    so the test writes the single settings row directly. This is deliberately a
    test-only write; production code never updates settings this way.
    """
    conn.execute(
        "UPDATE market_settings SET override_mode = ? WHERE setting_id = 1",
        (mode,),
    )


def _trainee_id(conn):
    """Return the seeded ``trainee1`` customer's ``user_id``."""
    return db.get_user_by_username(conn, "trainee1")["user_id"]


def _stock_id_by_ticker(conn, ticker):
    """Resolve a seeded stock's ``stock_id`` by ticker (ids are autoincrement)."""
    for row in db.list_stocks(conn):
        if row["ticker_symbol"] == ticker:
            return row["stock_id"]
    raise AssertionError(f"seeded stock {ticker!r} not found")


def _snapshot(conn):
    """Capture a comparable snapshot of every table's rows for a no-write check.

    Returns a dict of table -> list of plain dict rows, ordered deterministically
    by primary key, so two snapshots can be compared for exact equality. This
    proves a quote changed nothing anywhere in the database.
    """
    tables = {
        "users": "user_id",
        "stocks": "stock_id",
        "holdings": "holdings_id",
        "transactions": "transaction_id",
        "market_schedule": "schedule_id",
        "market_settings": "setting_id",
    }
    snap = {}
    for table, pk in tables.items():
        # Table/pk names come from this hard-coded map, never from input.
        rows = conn.execute(
            f"SELECT * FROM {table} ORDER BY {pk}"  # noqa: S608 (trusted literals)
        ).fetchall()
        snap[table] = [dict(r) for r in rows]
    return snap


# ---------------------------------------------------------------------------
# Property 8: Buy cost is the exact quantized product (Requirements 8.1-8.3).
#
# Tested against compute_total_cost directly -- a pure function, no DB needed.
# ---------------------------------------------------------------------------

# A price Decimal built from a string "W.CC" (never float), with 0..9999 whole
# dollars and 0..99 cents. Paired with a whole-share quantity 1..1,000,000. The
# ranges let the product land both under and over the 99999999.99 cap, so both
# branches of the property are exercised.
_prices = st.builds(
    lambda w, c: Decimal(f"{w}.{c:02d}"),
    st.integers(min_value=0, max_value=9999),
    st.integers(min_value=0, max_value=99),
)
_quantities = st.integers(min_value=1, max_value=1_000_000)


@settings(max_examples=200)
@given(price=_prices, quantity=_quantities)
@example(price=Decimal("42.50"), quantity=10)   # SSOL example: 425.00
@example(price=Decimal("0.00"), quantity=1)     # zero price -> 0.00
@example(price=Decimal("9999.99"), quantity=1_000_000)  # far over the cap
def test_buy_cost_is_exact_quantized_product(price, quantity):
    """compute_total_cost == quantize(price*quantity) ROUND_HALF_UP, or raises.

    The expected total is computed independently here with ROUND_HALF_UP to two
    places. When it is within the DECIMAL(10,2) cap the function must return a
    ``Decimal`` exactly equal to it; when it exceeds the cap the function must
    raise ``ValidationError`` instead of returning a value no money column can
    hold.

    # Feature: phase-1-buy-stock, Property 8: Buy cost is the exact quantized product
    """
    expected = (price * quantity).quantize(_CENT, rounding=ROUND_HALF_UP)

    if expected > MAX_MONEY:
        with pytest.raises(ValidationError):
            compute_total_cost(price, quantity)
    else:
        total = compute_total_cost(price, quantity)
        assert isinstance(total, Decimal)
        assert total == expected
        # Exactly two decimal places, no fractional cent slipped in.
        assert total == total.quantize(_CENT)


def test_buy_cost_rounds_half_up():
    """A product whose third decimal is 5 rounds the cent UP (ROUND_HALF_UP).

    0.125 * 1 = 0.125 -> 0.13 under ROUND_HALF_UP (it would be 0.12 under
    banker's rounding), pinning the exact rounding mode the design requires.
    """
    assert compute_total_cost(Decimal("0.125"), 1) == Decimal("0.13")


def test_buy_cost_over_cap_raises():
    """A product just over 99999999.99 raises ValidationError (Req 8.3)."""
    # 2.00 * 50,000,000 = 100,000,000.00, which exceeds MAX_MONEY. But quantity
    # is capped at 1,000,000, so use a price large enough to overflow within it.
    with pytest.raises(ValidationError):
        compute_total_cost(Decimal("101.00"), 1_000_000)  # 101,000,000.00


# ---------------------------------------------------------------------------
# Property 9: A quote writes nothing and reports consistent balances
# (Requirements 9.1, 9.2).
# ---------------------------------------------------------------------------

# A whole-share quantity small enough that an affordable SSOL (price 42.50) buy
# is possible for trainee1's 10000.00 cash: 42.50 * 235 = 9987.50 <= 10000.00,
# so 1..235 shares always pass the funds check with the market forced open.
_affordable_ssol_qty = st.integers(min_value=1, max_value=235)


# deadline=None: each example builds and seeds a fresh in-memory database,
# which legitimately takes longer than Hypothesis's default 200ms per-example
# deadline. The work is intended, not a performance regression, so we disable
# the deadline (max_examples stays well above the required 100 iterations).
@settings(max_examples=200, deadline=None)
@given(quantity=_affordable_ssol_qty)
@example(quantity=1)
@example(quantity=10)
@example(quantity=235)
def test_quote_writes_nothing_and_balances_are_consistent(quantity):
    """A valid quote makes no DB change and cash_after == cash_before - total.

    With the market forced open and an affordable SSOL quantity, quote_buy must
    succeed, leave every table byte-for-byte unchanged, and return a Quote whose
    arithmetic ties out: total_cost is the quantized product, cash_before is the
    seeded 10000.00, and cash_after is exactly cash_before minus total_cost.

    # Feature: phase-1-buy-stock, Property 9: A quote writes nothing and reports consistent balances
    """
    with _fresh_db() as conn:
        _set_override(conn, "force_open")
        user_id = _trainee_id(conn)
        stock_id = _stock_id_by_ticker(conn, "SSOL")

        before = _snapshot(conn)
        quote = quote_buy(conn, user_id, stock_id, quantity, _NOW)
        after = _snapshot(conn)

        # 9.1: the quote wrote nothing anywhere.
        assert before == after

        # 9.2: balances are internally consistent and exact.
        assert isinstance(quote.total_cost, Decimal)
        assert quote.total_cost == compute_total_cost(Decimal("42.50"), quantity)
        assert quote.cash_before == Decimal("10000.00")
        assert quote.cash_after == quote.cash_before - quote.total_cost
        assert quote.quantity == quantity
        assert quote.price_per_share == Decimal("42.50")
        assert quote.stock["ticker_symbol"] == "SSOL"


# ---------------------------------------------------------------------------
# Property 14: The first failing check in the fixed order is the one reported
# (Requirement 13.1).
#
# The fixed order is: stock exists -> quantity valid -> market open ->
# shares available -> funds. quote_buy receives an already-parsed int quantity,
# so we drive the other four checks and assert the earliest failing one wins
# when multiple would fail simultaneously.
# ---------------------------------------------------------------------------

# Independent model of which exception the fixed order should raise, given which
# checks fail. ``None`` means the quote should succeed.
def _expected_first_failure(stock_missing, market_closed, shares_short, funds_short):
    if stock_missing:
        return ValidationError
    if market_closed:
        return MarketClosedError
    if shares_short:
        return InsufficientSharesError
    if funds_short:
        return InsufficientFundsError
    return None


# deadline=None: each example builds and seeds a fresh in-memory database (see
# the note on the previous property test), which exceeds the default per-example
# deadline; the work is intended.
@settings(max_examples=200, deadline=None)
@given(
    stock_missing=st.booleans(),
    market_closed=st.booleans(),
    shares_short=st.booleans(),
    funds_short=st.booleans(),
)
def test_first_failing_check_in_fixed_order_is_reported(
    stock_missing, market_closed, shares_short, funds_short
):
    """When several checks fail, the earliest in the fixed order is raised.

    We independently toggle each downstream check and compare quote_buy's
    behaviour against an independent model of the ordering:

      - stock exists is forced to fail by using a non-existent stock_id;
      - market open is failed via force_closed, passed via force_open;
      - shares available is failed by using SAGU (50 shares) and asking for more
        than 50, passed by asking for <= 50;
      - funds is failed by choosing a SAGU quantity whose cost exceeds trainee1's
        10000.00 cash, passed otherwise.

    SAGU (price 5.00, 50 shares outstanding) lets shares and funds be driven
    independently: 50 shares cost only 250.00, so within [1, 50] funds never
    fails; to fail funds without failing shares we would need > 2000 shares,
    which SAGU cannot supply -- so when funds_short is requested we assert via a
    separate affordable/over-cash split below. Here we only need the RELATIVE
    ordering among the checks that CAN co-occur, which the model encodes.

    # Feature: phase-1-buy-stock, Property 14: The first failing check in the fixed order is the one reported
    """
    with _fresh_db() as conn:
        _set_override(conn, "force_closed" if market_closed else "force_open")
        user_id = _trainee_id(conn)

        if stock_missing:
            # An id past any seeded stock so get_stock returns None.
            stock_id = 99_999
        else:
            stock_id = _stock_id_by_ticker(conn, "SAGU")  # 50 shares @ 5.00

        # Choose a quantity that fails/passes the shares check as requested.
        # SAGU has 50 shares available. > 50 fails shares; <= 50 passes.
        if shares_short:
            quantity = 51  # exceeds the 50 available -> shares check fails
        else:
            quantity = 10  # well within 50 -> shares check passes

        # funds_short: make the order cost exceed 10000.00 cash WITHOUT changing
        # the shares outcome. For SAGU within 50 shares the max cost is 250.00,
        # which never exceeds cash -- so to model a funds-only failure we raise
        # the stock price so the (passing-shares) quantity still overflows cash.
        # We do this only when funds should fail but shares should NOT, and the
        # stock exists and the market is open; otherwise an earlier check wins
        # and the price is irrelevant.
        if funds_short and not shares_short and not stock_missing:
            # 10 shares * 2000.00 = 20000.00 > 10000.00 cash, shares still OK
            # (10 <= 50). Price change is test setup via a direct UPDATE.
            conn.execute(
                "UPDATE stocks SET current_price = ? WHERE stock_id = ?",
                (Decimal("2000.00"), stock_id),
            )

        expected = _expected_first_failure(
            stock_missing, market_closed, shares_short, funds_short
        )

        if expected is None:
            # No check should fail: the quote must succeed.
            quote = quote_buy(conn, user_id, stock_id, quantity, _NOW)
            assert quote.stock["ticker_symbol"] == "SAGU"
        else:
            with pytest.raises(expected):
                quote_buy(conn, user_id, stock_id, quantity, _NOW)


# ---------------------------------------------------------------------------
# Example tests (Requirement 18.1): concrete, named scenarios.
# ---------------------------------------------------------------------------


def test_example_stock_not_found():
    """A non-existent stock_id raises ValidationError('Stock not found.')."""
    with _fresh_db() as conn:
        _set_override(conn, "force_open")
        user_id = _trainee_id(conn)
        with pytest.raises(ValidationError) as excinfo:
            quote_buy(conn, user_id, 99_999, 1, _NOW)
        assert str(excinfo.value) == "Stock not found."


def test_example_insufficient_funds_gcny():
    """Buying 1 GCNY (12500.00) exceeds trainee1's 10000.00 cash."""
    with _fresh_db() as conn:
        _set_override(conn, "force_open")
        user_id = _trainee_id(conn)
        stock_id = _stock_id_by_ticker(conn, "GCNY")
        with pytest.raises(InsufficientFundsError) as excinfo:
            quote_buy(conn, user_id, stock_id, 1, _NOW)
        err = excinfo.value
        assert err.total == Decimal("12500.00")
        assert err.balance == Decimal("10000.00")


def test_example_insufficient_shares_sagu():
    """Buying 51 SAGU exceeds the 50 shares available; reports N and ticker."""
    with _fresh_db() as conn:
        _set_override(conn, "force_open")
        user_id = _trainee_id(conn)
        stock_id = _stock_id_by_ticker(conn, "SAGU")
        with pytest.raises(InsufficientSharesError) as excinfo:
            quote_buy(conn, user_id, stock_id, 51, _NOW)
        err = excinfo.value
        assert err.available == 50
        assert err.ticker == "SAGU"


def test_example_ssol_quote_values_and_no_writes():
    """A 10-share SSOL quote totals 425.00, cash_after 9575.00, writes nothing.

    Pins the concrete values from the spec (10 SSOL @ 42.50 -> 425.00, leaving
    9575.00) and confirms a quote is side-effect free: holdings and transactions
    stay empty and the user's cash is unchanged afterward.
    """
    with _fresh_db() as conn:
        _set_override(conn, "force_open")
        user_id = _trainee_id(conn)
        stock_id = _stock_id_by_ticker(conn, "SSOL")

        before = _snapshot(conn)
        quote = quote_buy(conn, user_id, stock_id, 10, _NOW)
        after = _snapshot(conn)

        assert quote.price_per_share == Decimal("42.50")
        assert quote.quantity == 10
        assert quote.total_cost == Decimal("425.00")
        assert quote.cash_before == Decimal("10000.00")
        assert quote.cash_after == Decimal("9575.00")

        # No DB writes: full snapshot unchanged, and specifically holdings and
        # transactions remain empty with cash untouched.
        assert before == after
        assert db.count_rows(conn, "holdings") == 0
        assert db.count_rows(conn, "transactions") == 0
        assert db.get_user_by_id(conn, user_id)["cash_balance"] == Decimal("10000.00")
