"""Service-level tests for cash deposits and withdrawals (app/services/cash.py).

Each test starts from the seeded MySQL test database: trainee1 has 10000.00.
"""

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from app import db
from app.services import cash
from app.services.cash import (
    DEPOSIT,
    WITHDRAWAL,
    cancel_pending_cash,
    confirm_cash,
    create_pending_cash,
    parse_amount,
    quote_cash,
)
from app.services.trading import (
    InsufficientFundsError,
    ValidationError,
    cancel_pending_buy,
    create_pending_buy,
)
from dbhelpers import add_customer, set_cash, set_override, snapshot, stock_id, trainee_id, transactions_for

NOW = datetime(2026, 3, 2, 15, 0, 0, tzinfo=timezone.utc)
LATER = datetime(2026, 3, 2, 15, 5, 0, tzinfo=timezone.utc)
D = Decimal


def _cash(conn, user_id):
    return db.get_user_by_id(conn, user_id)["cash_balance"]


def _do(conn, user, kind, amount, now=NOW):
    """Review then confirm; returns the transaction id."""
    tx_id = create_pending_cash(conn, user, kind, D(amount), now)
    confirm_cash(conn, user, tx_id, now, kind)
    return tx_id


# --- parse_amount ------------------------------------------------------------


@pytest.mark.parametrize(
    "text, expected",
    [("100", "100.00"), ("0.5", "0.50"), ("19.99", "19.99"), ("0.01", "0.01"), (" 20 ", "20.00"),
     ("1000000", "1000000.00"), ("007", "7.00"), ("12.30", "12.30")],
)
def test_parse_amount_accepts_plain_positive_amounts(text, expected):
    result = parse_amount(text)
    assert result == D(expected)
    assert isinstance(result, Decimal)
    assert result == result.quantize(D("0.01"))


@pytest.mark.parametrize(
    "text",
    ["", "   ", "0", "0.00", "0.0", "-5", "+5", "1.234", "1e3", "1E3", "abc", "1,000", "$5", ".5", "5.",
     "1 000", "1.2.3", "NaN", "Infinity", "١٢٣", "5; DROP TABLE users", None],
)
def test_parse_amount_rejects_everything_else(text):
    with pytest.raises(ValidationError, match="greater than \\$0.00 with at most two decimal places"):
        parse_amount(text)


def test_parse_amount_rejects_absurdly_long_input():
    with pytest.raises(ValidationError):
        parse_amount("9" * 5000)


# --- deposits -------------------------------------------------------------------


def test_deposit_adds_to_the_balance_and_records_the_transaction(db_conn):
    user = trainee_id(db_conn)
    tx_id = _do(db_conn, user, DEPOSIT, "250.50")

    assert _cash(db_conn, user) == D("10250.50")
    (tx,) = transactions_for(db_conn, user)
    assert tx["transaction_id"] == tx_id
    assert tx["transaction_type"] == "deposit"
    assert tx["total_amount"] == D("250.50")
    assert tx["stock_id"] is None and tx["quantity"] is None and tx["price_per_share"] is None
    assert tx["status"] == "executed"
    assert tx["created_at"] and tx["confirmed_at"] and tx["executed_at"]  # every cash event is timestamped


def test_pending_deposit_moves_no_money(db_conn):
    user = trainee_id(db_conn)
    create_pending_cash(db_conn, user, DEPOSIT, D("500.00"), NOW)
    assert _cash(db_conn, user) == D("10000.00")
    (tx,) = transactions_for(db_conn, user)
    assert tx["status"] == "pending" and tx["confirmed_at"] is None and tx["executed_at"] is None


def test_quote_shows_before_and_after_and_writes_nothing(db_conn):
    user = trainee_id(db_conn)
    before = snapshot(db_conn)
    quote = quote_cash(db_conn, user, DEPOSIT, D("100.25"))
    assert (quote.balance_before, quote.balance_after) == (D("10000.00"), D("10100.25"))
    quote = quote_cash(db_conn, user, WITHDRAWAL, D("100.25"))
    assert (quote.balance_before, quote.balance_after) == (D("10000.00"), D("9899.75"))
    assert snapshot(db_conn) == before


def test_a_new_customer_can_deposit_from_zero(db_conn):
    user = add_customer(db_conn)
    set_cash(db_conn, user, D("0.00"))
    _do(db_conn, user, DEPOSIT, "75.10")
    assert _cash(db_conn, user) == D("75.10")


def test_three_small_deposits_add_up_exactly(db_conn):
    user = trainee_id(db_conn)
    set_cash(db_conn, user, D("0.00"))
    for _ in range(3):
        _do(db_conn, user, DEPOSIT, "0.10")
    assert _cash(db_conn, user) == D("0.30")  # no floating-point drift


def test_deposit_larger_than_the_limit_is_rejected(db_conn):
    user = trainee_id(db_conn)
    before = snapshot(db_conn)
    with pytest.raises(ValidationError, match="limited to \\$1,000,000.00"):
        create_pending_cash(db_conn, user, DEPOSIT, D("1000000.01"), NOW)
    assert snapshot(db_conn) == before
    _do(db_conn, user, DEPOSIT, "1000000.00")  # exactly the limit is fine
    assert _cash(db_conn, user) == D("1010000.00")


def test_deposit_that_would_overflow_the_balance_is_rejected(db_conn):
    user = trainee_id(db_conn)
    set_cash(db_conn, user, D("99999999.00"))
    before = snapshot(db_conn)
    with pytest.raises(ValidationError, match="cannot be more than \\$99,999,999.99"):
        create_pending_cash(db_conn, user, DEPOSIT, D("1.00"), NOW)
    assert snapshot(db_conn) == before
    _do(db_conn, user, DEPOSIT, "0.99")
    assert _cash(db_conn, user) == D("99999999.99")


def test_balance_overflow_is_rechecked_at_confirm(db_conn):
    user = trainee_id(db_conn)
    tx_id = create_pending_cash(db_conn, user, DEPOSIT, D("500.00"), NOW)
    set_cash(db_conn, user, D("99999999.80"))  # changed after the review screen
    before = snapshot(db_conn)
    with pytest.raises(ValidationError, match="cannot be more than"):
        confirm_cash(db_conn, user, tx_id, NOW, DEPOSIT)
    assert snapshot(db_conn) == before


# --- withdrawals -----------------------------------------------------------------


def test_withdrawal_subtracts_from_the_balance(db_conn):
    user = trainee_id(db_conn)
    _do(db_conn, user, WITHDRAWAL, "250.00")
    assert _cash(db_conn, user) == D("9750.00")
    (tx,) = transactions_for(db_conn, user)
    assert tx["transaction_type"] == "withdrawal" and tx["status"] == "executed"
    assert tx["stock_id"] is None and tx["quantity"] is None and tx["price_per_share"] is None


def test_withdrawing_the_entire_balance_leaves_zero(db_conn):
    user = trainee_id(db_conn)
    _do(db_conn, user, WITHDRAWAL, "10000.00")
    assert _cash(db_conn, user) == D("0.00")


def test_withdrawal_more_than_the_balance_is_rejected(db_conn):
    user = trainee_id(db_conn)
    before = snapshot(db_conn)
    with pytest.raises(InsufficientFundsError) as err:
        create_pending_cash(db_conn, user, WITHDRAWAL, D("10000.01"), NOW)
    assert err.value.total == D("10000.01") and err.value.balance == D("10000.00")
    assert snapshot(db_conn) == before


def test_withdrawal_from_an_empty_account_is_rejected(db_conn):
    user = add_customer(db_conn)
    set_cash(db_conn, user, D("0.00"))
    with pytest.raises(InsufficientFundsError):
        create_pending_cash(db_conn, user, WITHDRAWAL, D("0.01"), NOW)


def test_balance_is_rechecked_at_confirm_for_withdrawals(db_conn):
    user = trainee_id(db_conn)
    tx_id = create_pending_cash(db_conn, user, WITHDRAWAL, D("500.00"), NOW)
    set_cash(db_conn, user, D("100.00"))  # money left the account after the review screen
    before = snapshot(db_conn)
    with pytest.raises(InsufficientFundsError):
        confirm_cash(db_conn, user, tx_id, NOW, WITHDRAWAL)
    assert snapshot(db_conn) == before  # still pending, balance untouched


def test_cash_works_even_when_the_market_is_closed(db_conn):
    user = trainee_id(db_conn)
    set_override(db_conn, "force_closed")
    _do(db_conn, user, DEPOSIT, "10.00")
    _do(db_conn, user, WITHDRAWAL, "5.00", LATER)
    assert _cash(db_conn, user) == D("10005.00")


# --- confirm is single-shot and owner-only -----------------------------------------


@pytest.mark.parametrize("kind", [DEPOSIT, WITHDRAWAL])
def test_double_submit_changes_nothing(db_conn, kind):
    user = trainee_id(db_conn)
    tx_id = _do(db_conn, user, kind, "100.00")
    after_first = snapshot(db_conn)
    with pytest.raises(ValidationError, match="already been processed"):
        confirm_cash(db_conn, user, tx_id, LATER, kind)
    assert snapshot(db_conn) == after_first


def test_cannot_confirm_another_users_transaction(db_conn):
    owner, intruder = trainee_id(db_conn), add_customer(db_conn)
    tx_id = create_pending_cash(db_conn, owner, DEPOSIT, D("100.00"), NOW)
    before = snapshot(db_conn)
    with pytest.raises(ValidationError, match="already been processed"):
        confirm_cash(db_conn, intruder, tx_id, NOW, DEPOSIT)
    assert snapshot(db_conn) == before


def test_a_deposit_cannot_be_confirmed_as_a_withdrawal(db_conn):
    user = trainee_id(db_conn)
    tx_id = create_pending_cash(db_conn, user, DEPOSIT, D("100.00"), NOW)
    before = snapshot(db_conn)
    with pytest.raises(ValidationError):
        confirm_cash(db_conn, user, tx_id, NOW, WITHDRAWAL)
    assert snapshot(db_conn) == before


def test_a_pending_trade_cannot_be_confirmed_as_cash(db_conn):
    user, ssol = trainee_id(db_conn), stock_id(db_conn, "SSOL")
    buy_id = create_pending_buy(db_conn, user, ssol, 1, NOW)
    before = snapshot(db_conn)
    with pytest.raises(ValidationError):
        confirm_cash(db_conn, user, buy_id, NOW, DEPOSIT)
    assert snapshot(db_conn) == before


def test_confirming_an_unknown_transaction_fails(db_conn):
    with pytest.raises(ValidationError, match="already been processed"):
        confirm_cash(db_conn, trainee_id(db_conn), 999_999, NOW, DEPOSIT)


# --- cancel ---------------------------------------------------------------------------


def test_cancel_discards_a_pending_request(db_conn):
    user = trainee_id(db_conn)
    tx_id = create_pending_cash(db_conn, user, DEPOSIT, D("100.00"), NOW)
    cancel_pending_cash(db_conn, user, tx_id, DEPOSIT)
    assert transactions_for(db_conn, user) == []
    assert _cash(db_conn, user) == D("10000.00")


def test_a_confirmed_cash_transaction_can_never_be_cancelled(db_conn):
    user = trainee_id(db_conn)
    tx_id = _do(db_conn, user, DEPOSIT, "100.00")
    before = snapshot(db_conn)
    cancel_pending_cash(db_conn, user, tx_id, DEPOSIT)
    assert snapshot(db_conn) == before


def test_cannot_cancel_another_users_request(db_conn):
    owner, intruder = trainee_id(db_conn), add_customer(db_conn)
    tx_id = create_pending_cash(db_conn, owner, DEPOSIT, D("100.00"), NOW)
    before = snapshot(db_conn)
    cancel_pending_cash(db_conn, intruder, tx_id, DEPOSIT)
    assert snapshot(db_conn) == before


def test_cancelling_with_the_wrong_kind_does_nothing(db_conn):
    user, ssol = trainee_id(db_conn), stock_id(db_conn, "SSOL")
    deposit_id = create_pending_cash(db_conn, user, DEPOSIT, D("100.00"), NOW)
    buy_id = create_pending_buy(db_conn, user, ssol, 1, NOW)
    before = snapshot(db_conn)
    cancel_pending_cash(db_conn, user, deposit_id, WITHDRAWAL)  # wrong kind
    cancel_pending_cash(db_conn, user, buy_id, DEPOSIT)  # a trade, not cash
    cancel_pending_buy(db_conn, user, deposit_id)  # cash, not a buy
    assert snapshot(db_conn) == before


# --- rollback ---------------------------------------------------------------------------


@pytest.mark.parametrize("kind", [DEPOSIT, WITHDRAWAL])
def test_failure_after_the_status_change_rolls_everything_back(db_conn, monkeypatch, kind):
    """The order is marked confirmed before the balance is written; a crash must undo that too."""
    user = trainee_id(db_conn)
    tx_id = create_pending_cash(db_conn, user, kind, D("100.00"), NOW)
    before = snapshot(db_conn)

    def boom(*args, **kwargs):
        raise RuntimeError("simulated failure while updating the balance")

    monkeypatch.setattr(db, "update_user_cash", boom)
    with pytest.raises(RuntimeError):
        confirm_cash(db_conn, user, tx_id, NOW, kind)
    assert snapshot(db_conn) == before


def test_bad_kind_is_a_programming_error(db_conn):
    with pytest.raises(ValueError):
        quote_cash(db_conn, trainee_id(db_conn), "gift", D("1.00"))
    assert cash.KINDS == ("deposit", "withdrawal")
