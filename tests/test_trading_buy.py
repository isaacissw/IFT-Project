"""Service-level tests for the BUY flow: create_pending_buy, confirm_buy, cancel.

Each test starts from the seeded MySQL test database (trainee1 has 10000.00 cash,
no holdings, no transactions; the market is open all day). ``now`` is passed in
explicitly, so nothing depends on the wall clock.

These tests check real database effects: they read values before and after.
"""

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from app import db
from app.services.trading import (
    InsufficientFundsError,
    InsufficientSharesError,
    MarketClosedError,
    PriceChangedError,
    ValidationError,
    cancel_pending_buy,
    confirm_buy,
    create_pending_buy,
)
from dbhelpers import (
    add_customer,
    set_cash,
    set_override,
    set_price,
    snapshot,
    stock_id,
    trainee_id,
    transactions_for,
    use_business_hours,
)

NOW = datetime(2026, 3, 2, 15, 0, 0, tzinfo=timezone.utc)
LATER = datetime(2026, 3, 2, 15, 5, 0, tzinfo=timezone.utc)


def _cash(conn, user_id):
    return db.get_user_by_id(conn, user_id)["cash_balance"]


def _owned(conn, user_id, sid):
    holding = db.get_holding(conn, user_id, sid)
    return holding["quantity"] if holding else 0


def _available(conn, ticker):
    stock = db.get_stock(conn, stock_id(conn, ticker))
    return stock["company_shares_outstanding"] - db.get_shares_held_sum(conn, stock["stock_id"])


def _buy(conn, user, sid, qty, now=NOW):
    """Review then confirm a buy; returns the transaction id."""
    tx_id = create_pending_buy(conn, user, sid, qty, now)
    confirm_buy(conn, user, tx_id, now)
    return tx_id


# --- Happy paths --------------------------------------------------------------


def test_buy_creates_holding_and_moves_money(db_conn):
    user, ssol = trainee_id(db_conn), stock_id(db_conn, "SSOL")
    assert _cash(db_conn, user) == Decimal("10000.00")
    assert _owned(db_conn, user, ssol) == 0
    assert _available(db_conn, "SSOL") == 1_000_000

    tx_id = _buy(db_conn, user, ssol, 10)

    assert _cash(db_conn, user) == Decimal("9575.00")
    assert _owned(db_conn, user, ssol) == 10
    assert _available(db_conn, "SSOL") == 999_990
    (tx,) = transactions_for(db_conn, user)
    assert tx["transaction_id"] == tx_id
    assert tx["transaction_type"] == "buy"
    assert tx["quantity"] == 10
    assert tx["price_per_share"] == Decimal("42.50")
    assert tx["total_amount"] == Decimal("425.00")
    assert tx["status"] == "executed"
    assert tx["created_at"] and tx["confirmed_at"] and tx["executed_at"]


def test_pending_buy_moves_no_money_or_shares(db_conn):
    user, ssol = trainee_id(db_conn), stock_id(db_conn, "SSOL")
    create_pending_buy(db_conn, user, ssol, 10, NOW)
    assert _cash(db_conn, user) == Decimal("10000.00")
    assert _owned(db_conn, user, ssol) == 0
    (tx,) = transactions_for(db_conn, user)
    assert tx["status"] == "pending"
    assert tx["confirmed_at"] is None and tx["executed_at"] is None


def test_second_buy_adds_to_existing_holding(db_conn):
    user, ssol = trainee_id(db_conn), stock_id(db_conn, "SSOL")
    _buy(db_conn, user, ssol, 10)
    _buy(db_conn, user, ssol, 5, LATER)
    assert _owned(db_conn, user, ssol) == 15
    assert _cash(db_conn, user) == Decimal("9362.50")  # 10000 - 15 * 42.50
    assert len(transactions_for(db_conn, user)) == 2


def test_buy_with_exactly_all_cash_leaves_zero(db_conn):
    user, ssol = trainee_id(db_conn), stock_id(db_conn, "SSOL")
    set_cash(db_conn, user, Decimal("425.00"))
    _buy(db_conn, user, ssol, 10)
    assert _cash(db_conn, user) == Decimal("0.00")
    assert _owned(db_conn, user, ssol) == 10


# --- Review-time rejections: nothing may change -------------------------------


def test_insufficient_funds_gcny(db_conn):
    user, gcny = trainee_id(db_conn), stock_id(db_conn, "GCNY")
    before = snapshot(db_conn)
    with pytest.raises(InsufficientFundsError) as err:
        create_pending_buy(db_conn, user, gcny, 1, NOW)
    assert err.value.total == Decimal("12500.00")
    assert err.value.balance == Decimal("10000.00")
    assert snapshot(db_conn) == before


def test_insufficient_shares_51_sagu(db_conn):
    user, sagu = trainee_id(db_conn), stock_id(db_conn, "SAGU")
    before = snapshot(db_conn)
    with pytest.raises(InsufficientSharesError) as err:
        create_pending_buy(db_conn, user, sagu, 51, NOW)
    assert err.value.available == 50
    assert err.value.ticker == "SAGU"
    assert snapshot(db_conn) == before


def test_stock_not_found(db_conn):
    user = trainee_id(db_conn)
    before = snapshot(db_conn)
    with pytest.raises(ValidationError, match="Stock not found."):
        create_pending_buy(db_conn, user, 999_999, 1, NOW)
    assert snapshot(db_conn) == before


def test_market_closed_at_review(db_conn):
    user, ssol = trainee_id(db_conn), stock_id(db_conn, "SSOL")
    set_override(db_conn, "force_closed")
    before = snapshot(db_conn)
    with pytest.raises(MarketClosedError):
        create_pending_buy(db_conn, user, ssol, 1, NOW)
    assert snapshot(db_conn) == before


@pytest.mark.parametrize(
    "label, when_utc",
    [
        # New York is UTC-5 in winter (March 2 2026), UTC-4 once daylight saving starts.
        ("Monday 8:59 AM, before opening", datetime(2026, 3, 2, 13, 59, tzinfo=timezone.utc)),
        ("Monday 5:00 PM, exactly at closing", datetime(2026, 3, 2, 22, 0, tzinfo=timezone.utc)),
        ("Monday evening", datetime(2026, 3, 3, 1, 0, tzinfo=timezone.utc)),
        ("Saturday midday", datetime(2026, 3, 7, 17, 0, tzinfo=timezone.utc)),
        ("Sunday midday", datetime(2026, 3, 8, 17, 0, tzinfo=timezone.utc)),
    ],
)
def test_default_schedule_is_closed_outside_weekday_9_to_5(db_conn, label, when_utc):
    user, ssol = trainee_id(db_conn), stock_id(db_conn, "SSOL")
    use_business_hours(db_conn)
    before = snapshot(db_conn)
    with pytest.raises(MarketClosedError):
        create_pending_buy(db_conn, user, ssol, 1, when_utc)
    assert snapshot(db_conn) == before, label


def test_default_schedule_is_open_at_9am_on_a_weekday(db_conn):
    user, ssol = trainee_id(db_conn), stock_id(db_conn, "SSOL")
    use_business_hours(db_conn)
    opening = datetime(2026, 3, 2, 14, 0, tzinfo=timezone.utc)  # Monday 9:00 AM New York
    assert create_pending_buy(db_conn, user, ssol, 1, opening)


def test_force_open_overrides_the_weekend(db_conn):
    user, ssol = trainee_id(db_conn), stock_id(db_conn, "SSOL")
    use_business_hours(db_conn)
    set_override(db_conn, "force_open")
    saturday = datetime(2026, 3, 7, 17, 0, tzinfo=timezone.utc)
    assert create_pending_buy(db_conn, user, ssol, 1, saturday)


def test_first_failing_check_wins(db_conn):
    """Market closed beats insufficient funds when both are true."""
    user, gcny = trainee_id(db_conn), stock_id(db_conn, "GCNY")
    set_override(db_conn, "force_closed")
    with pytest.raises(MarketClosedError):
        create_pending_buy(db_conn, user, gcny, 1, NOW)


# --- Confirm-time re-validation ----------------------------------------------


def test_double_submit_changes_nothing(db_conn):
    user, ssol = trainee_id(db_conn), stock_id(db_conn, "SSOL")
    tx_id = _buy(db_conn, user, ssol, 10)
    after_first = snapshot(db_conn)
    with pytest.raises(ValidationError, match="already been processed"):
        confirm_buy(db_conn, user, tx_id, LATER)
    assert snapshot(db_conn) == after_first


def test_cannot_confirm_another_users_order(db_conn):
    owner, ssol = trainee_id(db_conn), stock_id(db_conn, "SSOL")
    intruder = add_customer(db_conn)
    tx_id = create_pending_buy(db_conn, owner, ssol, 10, NOW)
    before = snapshot(db_conn)
    with pytest.raises(ValidationError, match="already been processed"):
        confirm_buy(db_conn, intruder, tx_id, NOW)
    assert snapshot(db_conn) == before


def test_confirm_unknown_transaction(db_conn):
    user = trainee_id(db_conn)
    before = snapshot(db_conn)
    with pytest.raises(ValidationError, match="already been processed"):
        confirm_buy(db_conn, user, 999_999, NOW)
    assert snapshot(db_conn) == before


def test_market_closed_at_confirm(db_conn):
    user, ssol = trainee_id(db_conn), stock_id(db_conn, "SSOL")
    tx_id = create_pending_buy(db_conn, user, ssol, 10, NOW)
    set_override(db_conn, "force_closed")
    before = snapshot(db_conn)
    with pytest.raises(MarketClosedError):
        confirm_buy(db_conn, user, tx_id, NOW)
    assert snapshot(db_conn) == before  # still pending, cash and holdings untouched


def test_price_change_between_review_and_confirm(db_conn):
    user, ssol = trainee_id(db_conn), stock_id(db_conn, "SSOL")
    tx_id = create_pending_buy(db_conn, user, ssol, 10, NOW)
    set_price(db_conn, "SSOL", Decimal("45.00"))
    with pytest.raises(PriceChangedError) as err:
        confirm_buy(db_conn, user, tx_id, NOW)
    assert err.value.ticker == "SSOL"
    assert err.value.old == Decimal("42.50")
    assert err.value.new == Decimal("45.00")
    # The stale pending order is discarded; money and shares did not move.
    assert transactions_for(db_conn, user) == []
    assert _cash(db_conn, user) == Decimal("10000.00")
    assert _owned(db_conn, user, ssol) == 0


def test_insufficient_funds_at_confirm(db_conn):
    user, ssol = trainee_id(db_conn), stock_id(db_conn, "SSOL")
    tx_id = create_pending_buy(db_conn, user, ssol, 10, NOW)
    set_cash(db_conn, user, Decimal("100.00"))
    before = snapshot(db_conn)
    with pytest.raises(InsufficientFundsError):
        confirm_buy(db_conn, user, tx_id, NOW)
    assert snapshot(db_conn) == before


def test_insufficient_shares_at_confirm(db_conn):
    user, sagu = trainee_id(db_conn), stock_id(db_conn, "SAGU")
    other = add_customer(db_conn)
    tx_id = create_pending_buy(db_conn, user, sagu, 50, NOW)
    # Someone else buys 10 SAGU after the review screen, leaving only 40.
    db.upsert_holding(db_conn, other, sagu, 10, NOW.isoformat())
    before = snapshot(db_conn)
    with pytest.raises(InsufficientSharesError) as err:
        confirm_buy(db_conn, user, tx_id, NOW)
    assert err.value.available == 40
    assert snapshot(db_conn) == before


# --- Cancel -------------------------------------------------------------------


def test_cancel_pending_buy_deletes_only_that_row(db_conn):
    user, ssol = trainee_id(db_conn), stock_id(db_conn, "SSOL")
    keep = _buy(db_conn, user, ssol, 5)
    pending = create_pending_buy(db_conn, user, ssol, 10, LATER)
    cancel_pending_buy(db_conn, user, pending)
    remaining = [t["transaction_id"] for t in transactions_for(db_conn, user)]
    assert remaining == [keep]
    assert _cash(db_conn, user) == Decimal("9787.50")  # only the first buy applied
    assert _owned(db_conn, user, ssol) == 5


def test_cancel_executed_buy_does_nothing(db_conn):
    user, ssol = trainee_id(db_conn), stock_id(db_conn, "SSOL")
    tx_id = _buy(db_conn, user, ssol, 10)
    before = snapshot(db_conn)
    cancel_pending_buy(db_conn, user, tx_id)
    assert snapshot(db_conn) == before


def test_cannot_cancel_another_users_pending_buy(db_conn):
    owner, ssol = trainee_id(db_conn), stock_id(db_conn, "SSOL")
    intruder = add_customer(db_conn)
    tx_id = create_pending_buy(db_conn, owner, ssol, 10, NOW)
    before = snapshot(db_conn)
    cancel_pending_buy(db_conn, intruder, tx_id)
    assert snapshot(db_conn) == before
