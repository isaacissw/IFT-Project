"""Two requests at the same moment: the database row locks must keep the books right.

Each thread opens its OWN database connection (like two browser tabs hitting the
server at once) and they are released together with a barrier. Each scenario is
repeated several times because a race only sometimes overlaps.
"""

import threading
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from app import db
from app.services.cash import DEPOSIT, WITHDRAWAL, confirm_cash, create_pending_cash
from app.services.trading import (
    InsufficientFundsError,
    InsufficientSharesError,
    ValidationError,
    confirm_buy,
    create_pending_buy,
)
from dbhelpers import add_customer, set_cash, stock_id, test_database_url, trainee_id

NOW = datetime(2026, 3, 2, 15, 0, 0, tzinfo=timezone.utc)
D = Decimal
ROUNDS = 6


def _race(*jobs):
    """Run each job (a function taking a fresh connection) at the same instant.

    Returns one entry per job: ('ok', result) or ('error', exception).
    """
    barrier = threading.Barrier(len(jobs))
    results = [None] * len(jobs)

    def run(index, job):
        conn = db.get_connection(test_database_url())
        try:
            barrier.wait(timeout=10)
            results[index] = ("ok", job(conn))
        except Exception as exc:  # noqa: BLE001 - we want to record whatever happened
            results[index] = ("error", exc)
        finally:
            conn.close()

    threads = [threading.Thread(target=run, args=(i, job)) for i, job in enumerate(jobs)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
        assert not t.is_alive(), "a thread hung (possible deadlock)"
    return results


def _outcomes(results):
    return sorted(kind for kind, _ in results)


def test_double_click_on_confirm_deposits_only_once(db_conn):
    user = trainee_id(db_conn)
    expected = D("10000.00")
    for _ in range(ROUNDS):
        tx_id = create_pending_cash(db_conn, user, DEPOSIT, D("100.00"), NOW)
        results = _race(
            lambda c: confirm_cash(c, user, tx_id, NOW, DEPOSIT),
            lambda c: confirm_cash(c, user, tx_id, NOW, DEPOSIT),
        )
        expected += D("100.00")
        assert _outcomes(results) == ["error", "ok"]
        loser = next(r for kind, r in results if kind == "error")
        assert isinstance(loser, ValidationError)
        assert db.get_user_by_id(db_conn, user)["cash_balance"] == expected


def test_two_withdrawals_cannot_spend_the_same_money(db_conn):
    """Balance 100.00, two pending withdrawals of 80.00: only one may go through."""
    user = trainee_id(db_conn)
    for _ in range(ROUNDS):
        set_cash(db_conn, user, D("100.00"))
        first = create_pending_cash(db_conn, user, WITHDRAWAL, D("80.00"), NOW)
        second = create_pending_cash(db_conn, user, WITHDRAWAL, D("80.00"), NOW)
        results = _race(
            lambda c: confirm_cash(c, user, first, NOW, WITHDRAWAL),
            lambda c: confirm_cash(c, user, second, NOW, WITHDRAWAL),
        )
        assert _outcomes(results) == ["error", "ok"]
        loser = next(r for kind, r in results if kind == "error")
        assert isinstance(loser, InsufficientFundsError)
        assert db.get_user_by_id(db_conn, user)["cash_balance"] == D("20.00")


def test_two_buyers_cannot_take_the_same_last_shares(db_conn):
    """SAGU has 50 shares. Two customers each have a pending buy of all 50."""
    sagu = stock_id(db_conn, "SAGU")
    alice, bob = trainee_id(db_conn), add_customer(db_conn)
    first = create_pending_buy(db_conn, alice, sagu, 50, NOW)
    second = create_pending_buy(db_conn, bob, sagu, 50, NOW)
    results = _race(
        lambda c: confirm_buy(c, alice, first, NOW),
        lambda c: confirm_buy(c, bob, second, NOW),
    )
    assert _outcomes(results) == ["error", "ok"]
    loser = next(r for kind, r in results if kind == "error")
    assert isinstance(loser, InsufficientSharesError)
    assert db.get_shares_held_sum(db_conn, sagu) == 50  # never oversold
    # The buyer who lost still has all their cash.
    winner_is_alice = results[0][0] == "ok"
    loser_id = bob if winner_is_alice else alice
    assert db.get_user_by_id(db_conn, loser_id)["cash_balance"] == D("10000.00")


def test_double_click_on_buy_confirm_buys_only_once(db_conn):
    user, ssol = trainee_id(db_conn), stock_id(db_conn, "SSOL")
    tx_id = create_pending_buy(db_conn, user, ssol, 10, NOW)
    results = _race(
        lambda c: confirm_buy(c, user, tx_id, NOW),
        lambda c: confirm_buy(c, user, tx_id, NOW),
    )
    assert _outcomes(results) == ["error", "ok"]
    assert db.get_holding(db_conn, user, ssol)["quantity"] == 10
    assert db.get_user_by_id(db_conn, user)["cash_balance"] == D("9575.00")


@pytest.mark.parametrize("attempt", range(3))
def test_a_deposit_and_a_withdrawal_at_once_both_apply_correctly(db_conn, attempt):
    user = trainee_id(db_conn)
    set_cash(db_conn, user, D("1000.00"))
    deposit = create_pending_cash(db_conn, user, DEPOSIT, D("250.00"), NOW)
    withdrawal = create_pending_cash(db_conn, user, WITHDRAWAL, D("100.00"), NOW)
    results = _race(
        lambda c: confirm_cash(c, user, deposit, NOW, DEPOSIT),
        lambda c: confirm_cash(c, user, withdrawal, NOW, WITHDRAWAL),
    )
    assert _outcomes(results) == ["ok", "ok"]
    assert db.get_user_by_id(db_conn, user)["cash_balance"] == D("1150.00")  # no lost update
