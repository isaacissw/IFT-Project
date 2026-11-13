"""Rollback tests: a failure halfway through a confirm must undo EVERYTHING.

The confirm steps run in this order: mark confirmed, change cash, change
holdings, mark executed. We force the holdings step to blow up. By then the
order is already marked confirmed and the cash is already changed, so a correct
rollback has real work to do: cash, holdings and the order's status must all end
up exactly as they were before the confirm started.
"""

from datetime import datetime, timezone

import pytest

from app import db
from app.services.trading import (
    confirm_buy,
    confirm_sell,
    create_pending_buy,
    create_pending_sell,
)
from dbhelpers import snapshot, stock_id, trainee_id

NOW = datetime(2026, 3, 2, 15, 0, 0, tzinfo=timezone.utc)


def _boom(*args, **kwargs):
    raise RuntimeError("simulated failure in the middle of a confirm")


def test_buy_rolls_back_everything_when_holdings_update_fails(db_conn, monkeypatch):
    user, ssol = trainee_id(db_conn), stock_id(db_conn, "SSOL")
    tx_id = create_pending_buy(db_conn, user, ssol, 10, NOW)
    before = snapshot(db_conn)

    monkeypatch.setattr(db, "upsert_holding", _boom)
    with pytest.raises(RuntimeError):
        confirm_buy(db_conn, user, tx_id, NOW)

    assert snapshot(db_conn) == before  # cash, holdings and status all unchanged
    pending = [t for t in snapshot(db_conn)["transactions"] if t["transaction_id"] == tx_id]
    assert pending[0]["status"] == "pending"


def test_sell_rolls_back_everything_when_holdings_update_fails(db_conn, monkeypatch):
    user, ssol = trainee_id(db_conn), stock_id(db_conn, "SSOL")
    buy_id = create_pending_buy(db_conn, user, ssol, 10, NOW)
    confirm_buy(db_conn, user, buy_id, NOW)
    sell_id = create_pending_sell(db_conn, user, ssol, 4, NOW)
    before = snapshot(db_conn)

    monkeypatch.setattr(db, "reduce_holding", _boom)
    with pytest.raises(RuntimeError):
        confirm_sell(db_conn, user, sell_id, NOW)

    assert snapshot(db_conn) == before


def test_buy_still_works_after_a_rolled_back_attempt(db_conn, monkeypatch):
    """The failed attempt must not leave a lock or half-open transaction behind."""
    user, ssol = trainee_id(db_conn), stock_id(db_conn, "SSOL")
    tx_id = create_pending_buy(db_conn, user, ssol, 10, NOW)

    with monkeypatch.context() as patch:
        patch.setattr(db, "upsert_holding", _boom)
        with pytest.raises(RuntimeError):
            confirm_buy(db_conn, user, tx_id, NOW)

    confirm_buy(db_conn, user, tx_id, NOW)  # real implementation back in place
    assert db.get_holding(db_conn, user, ssol)["quantity"] == 10
    assert str(db.get_user_by_id(db_conn, user)["cash_balance"]) == "9575.00"
