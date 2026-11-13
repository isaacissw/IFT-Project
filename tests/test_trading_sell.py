"""Service-level tests for the SELL flow: create_pending_sell, confirm_sell, cancel.

Most tests start from a customer who already bought 10 SSOL (cash 9575.00).
"""

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from app import db
from app.services.trading import (
    InsufficientSharesError,
    MarketClosedError,
    PriceChangedError,
    ValidationError,
    cancel_pending_sell,
    confirm_buy,
    confirm_sell,
    create_pending_buy,
    create_pending_sell,
)
from dbhelpers import (
    add_customer,
    set_override,
    set_price,
    snapshot,
    stock_id,
    trainee_id,
    transactions_for,
)

NOW = datetime(2026, 3, 2, 15, 0, 0, tzinfo=timezone.utc)
LATER = datetime(2026, 3, 2, 15, 5, 0, tzinfo=timezone.utc)


def _cash(conn, user_id):
    return db.get_user_by_id(conn, user_id)["cash_balance"]


def _owned(conn, user_id, sid):
    holding = db.get_holding(conn, user_id, sid)
    return holding["quantity"] if holding else 0


def _available(conn, sid):
    stock = db.get_stock(conn, sid)
    return stock["company_shares_outstanding"] - db.get_shares_held_sum(conn, sid)


@pytest.fixture
def owner(db_conn):
    """trainee1 after buying 10 SSOL."""
    user, ssol = trainee_id(db_conn), stock_id(db_conn, "SSOL")
    tx = create_pending_buy(db_conn, user, ssol, 10, NOW)
    confirm_buy(db_conn, user, tx, NOW)
    return user, ssol


def test_sell_part_of_holding(db_conn, owner):
    user, ssol = owner
    tx_id = create_pending_sell(db_conn, user, ssol, 4, LATER)
    confirm_sell(db_conn, user, tx_id, LATER)

    assert _cash(db_conn, user) == Decimal("9745.00")  # 9575 + 4 * 42.50
    assert _owned(db_conn, user, ssol) == 6
    assert _available(db_conn, ssol) == 999_994  # shares returned to inventory
    sell = transactions_for(db_conn, user)[-1]
    assert sell["transaction_type"] == "sell"
    assert sell["quantity"] == 4
    assert sell["total_amount"] == Decimal("170.00")
    assert sell["status"] == "executed"
    assert sell["confirmed_at"] and sell["executed_at"]


def test_pending_sell_moves_nothing(db_conn, owner):
    user, ssol = owner
    before_cash = _cash(db_conn, user)
    create_pending_sell(db_conn, user, ssol, 4, LATER)
    assert _cash(db_conn, user) == before_cash
    assert _owned(db_conn, user, ssol) == 10


def test_selling_everything_removes_the_holding_row(db_conn, owner):
    user, ssol = owner
    tx_id = create_pending_sell(db_conn, user, ssol, 10, LATER)
    confirm_sell(db_conn, user, tx_id, LATER)
    assert db.get_holding(db_conn, user, ssol) is None
    assert _cash(db_conn, user) == Decimal("10000.00")
    assert _available(db_conn, ssol) == 1_000_000


def test_cannot_sell_more_than_owned(db_conn, owner):
    user, ssol = owner
    before = snapshot(db_conn)
    with pytest.raises(InsufficientSharesError) as err:
        create_pending_sell(db_conn, user, ssol, 11, LATER)
    assert err.value.available == 10
    assert snapshot(db_conn) == before


def test_cannot_sell_a_stock_you_do_not_own(db_conn):
    user, dsrt = trainee_id(db_conn), stock_id(db_conn, "DSRT")
    before = snapshot(db_conn)
    with pytest.raises(InsufficientSharesError) as err:
        create_pending_sell(db_conn, user, dsrt, 1, NOW)
    assert err.value.available == 0
    assert snapshot(db_conn) == before


def test_sell_stock_not_found(db_conn):
    with pytest.raises(ValidationError, match="Stock not found."):
        create_pending_sell(db_conn, trainee_id(db_conn), 999_999, 1, NOW)


def test_market_closed_at_sell_review(db_conn, owner):
    user, ssol = owner
    set_override(db_conn, "force_closed")
    before = snapshot(db_conn)
    with pytest.raises(MarketClosedError):
        create_pending_sell(db_conn, user, ssol, 1, LATER)
    assert snapshot(db_conn) == before


def test_market_closed_at_sell_confirm(db_conn, owner):
    user, ssol = owner
    tx_id = create_pending_sell(db_conn, user, ssol, 4, LATER)
    set_override(db_conn, "force_closed")
    before = snapshot(db_conn)
    with pytest.raises(MarketClosedError):
        confirm_sell(db_conn, user, tx_id, LATER)
    assert snapshot(db_conn) == before


def test_sell_double_submit_changes_nothing(db_conn, owner):
    user, ssol = owner
    tx_id = create_pending_sell(db_conn, user, ssol, 4, LATER)
    confirm_sell(db_conn, user, tx_id, LATER)
    after_first = snapshot(db_conn)
    with pytest.raises(ValidationError, match="already been processed"):
        confirm_sell(db_conn, user, tx_id, LATER)
    assert snapshot(db_conn) == after_first


def test_cannot_confirm_another_users_sell(db_conn, owner):
    user, ssol = owner
    intruder = add_customer(db_conn)
    tx_id = create_pending_sell(db_conn, user, ssol, 4, LATER)
    before = snapshot(db_conn)
    with pytest.raises(ValidationError, match="already been processed"):
        confirm_sell(db_conn, intruder, tx_id, LATER)
    assert snapshot(db_conn) == before


def test_buy_order_cannot_be_confirmed_as_a_sell(db_conn):
    user, ssol = trainee_id(db_conn), stock_id(db_conn, "SSOL")
    buy_id = create_pending_buy(db_conn, user, ssol, 10, NOW)
    before = snapshot(db_conn)
    with pytest.raises(ValidationError, match="already been processed"):
        confirm_sell(db_conn, user, buy_id, NOW)
    assert snapshot(db_conn) == before


def test_price_change_between_sell_review_and_confirm(db_conn, owner):
    user, ssol = owner
    tx_id = create_pending_sell(db_conn, user, ssol, 4, LATER)
    set_price(db_conn, "SSOL", Decimal("50.00"))
    with pytest.raises(PriceChangedError) as err:
        confirm_sell(db_conn, user, tx_id, LATER)
    assert err.value.old == Decimal("42.50") and err.value.new == Decimal("50.00")
    # Only the original buy remains; the stale pending sell was discarded.
    assert [t["transaction_type"] for t in transactions_for(db_conn, user)] == ["buy"]
    assert _cash(db_conn, user) == Decimal("9575.00")
    assert _owned(db_conn, user, ssol) == 10


def test_sold_shares_cannot_be_sold_twice(db_conn, owner):
    """Two pending sells of 8 of 10 shares: the second confirm must fail."""
    user, ssol = owner
    first = create_pending_sell(db_conn, user, ssol, 8, LATER)
    second = create_pending_sell(db_conn, user, ssol, 8, LATER)
    confirm_sell(db_conn, user, first, LATER)
    with pytest.raises(InsufficientSharesError) as err:
        confirm_sell(db_conn, user, second, LATER)
    assert err.value.available == 2
    assert _owned(db_conn, user, ssol) == 2


def test_cancel_pending_sell(db_conn, owner):
    user, ssol = owner
    tx_id = create_pending_sell(db_conn, user, ssol, 4, LATER)
    cancel_pending_sell(db_conn, user, tx_id)
    assert [t["transaction_type"] for t in transactions_for(db_conn, user)] == ["buy"]


def test_cancel_executed_sell_does_nothing(db_conn, owner):
    user, ssol = owner
    tx_id = create_pending_sell(db_conn, user, ssol, 4, LATER)
    confirm_sell(db_conn, user, tx_id, LATER)
    before = snapshot(db_conn)
    cancel_pending_sell(db_conn, user, tx_id)
    assert snapshot(db_conn) == before
