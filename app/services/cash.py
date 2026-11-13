"""Cash service: deposits and withdrawals of simulated money.

Follows the same pattern as the stock trades:

  1. ``create_pending_cash`` checks the request and records it as ``pending``
     (no money moves yet) so the customer can review it,
  2. ``confirm_cash`` applies it inside one locked database transaction and
     re-checks everything at that moment, and
  3. ``cancel_pending_cash`` discards a request that was not confirmed.

Once a cash transaction is confirmed it is never changed or deleted (the audit
trail is immutable). Cash is not tied to market hours, so these work any time.

No Flask imports. Money is always ``Decimal`` built from text, never float.
"""

import re
from dataclasses import dataclass
from decimal import Decimal

from app import db
from app.money import MAX_MONEY, format_money, quantize, to_money
from app.services.trading import InsufficientFundsError, ValidationError

DEPOSIT = "deposit"
WITHDRAWAL = "withdrawal"
KINDS = (DEPOSIT, WITHDRAWAL)

# The most one deposit may add. (The balance column itself can hold up to MAX_MONEY.)
MAX_DEPOSIT = Decimal("1000000.00")

AMOUNT_MESSAGE = "Please enter an amount greater than $0.00 with at most two decimal places."
PROCESSED_MESSAGE = "This transaction has already been processed or does not exist."

# Digits, optionally followed by a dot and one or two digits. Nothing else: no
# sign, no commas, no exponent ("1e3"), no spaces inside, no currency symbol.
_AMOUNT_PATTERN = re.compile(r"^[0-9]+(\.[0-9]{1,2})?$")


@dataclass(frozen=True)
class CashQuote:
    """What a deposit/withdrawal would do. Pure calculation; nothing is written."""

    kind: str
    amount: Decimal
    balance_before: Decimal
    balance_after: Decimal


def parse_amount(raw):
    """Turn the text from the form into an exact ``Decimal`` amount, or raise ``ValidationError``.

    Accepts only a plain positive amount with at most two decimals ("100",
    "19.99", "0.05"). Everything else, including empty text, zero, negatives,
    three decimals, "1e3", "$5", "1,000" and non-numbers, gets AMOUNT_MESSAGE.
    """
    text = ("" if raw is None else str(raw)).strip()
    if not _AMOUNT_PATTERN.match(text):
        raise ValidationError(AMOUNT_MESSAGE)
    if len(text) > 20:  # absurdly long: refuse before doing any math
        raise ValidationError("Please enter a smaller amount.")
    amount = Decimal(text)  # built from text, so it is exact
    if amount <= 0:
        raise ValidationError(AMOUNT_MESSAGE)
    return quantize(amount)


def _check_kind(kind):
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {KINDS}")


def _validate(conn, user_id, kind, amount):
    """Rule checks shared by the quote and the pending request. Returns the current balance."""
    user = db.get_user_by_id(conn, user_id)
    if user is None:
        raise ValidationError("User not found.")
    balance = to_money(user["cash_balance"])

    if kind == DEPOSIT:
        if amount > MAX_DEPOSIT:
            raise ValidationError(f"Deposits are limited to {format_money(MAX_DEPOSIT)} at a time.")
        if balance + amount > MAX_MONEY:
            raise ValidationError(f"Your cash balance cannot be more than {format_money(MAX_MONEY)}.")
    elif amount > balance:
        raise InsufficientFundsError(total=amount, balance=balance)
    return balance


def quote_cash(conn, user_id, kind, amount):
    """Preview a deposit or withdrawal without writing anything."""
    _check_kind(kind)
    balance = _validate(conn, user_id, kind, amount)
    after = balance + amount if kind == DEPOSIT else balance - amount
    return CashQuote(kind=kind, amount=amount, balance_before=balance, balance_after=after)


def create_pending_cash(conn, user_id, kind, amount, now):
    """Record a pending deposit/withdrawal and return its ``transaction_id``.

    Validates first (so a bad request writes nothing). The customer's balance is
    NOT changed here; that happens at ``confirm_cash``.
    """
    _check_kind(kind)
    _validate(conn, user_id, kind, amount)
    return db.insert_pending_cash(conn, user_id, kind, amount, now.isoformat())


def confirm_cash(conn, user_id, transaction_id, now, kind):
    """Apply a pending deposit/withdrawal. All-or-nothing. Returns ``(amount, new_balance)``.

    Everything is re-read and re-checked inside one transaction, holding a lock
    on the transaction row and on the user's row, so two simultaneous confirms
    cannot both apply and a balance that changed since the review screen is
    noticed. Any failure undoes everything.
    """
    _check_kind(kind)
    with db.transaction(conn):
        tx = db.get_transaction_for_update(conn, transaction_id)
        if (
            tx is None
            or tx["user_id"] != user_id
            or tx["transaction_type"] != kind
            or tx["status"] != "pending"
        ):
            raise ValidationError(PROCESSED_MESSAGE)

        user = db.get_user_by_id(conn, user_id, for_update=True)
        balance = to_money(user["cash_balance"])
        amount = to_money(tx["total_amount"])

        if kind == DEPOSIT:
            new_balance = balance + amount
            if new_balance > MAX_MONEY:
                raise ValidationError(f"Your cash balance cannot be more than {format_money(MAX_MONEY)}.")
        else:
            if balance < amount:
                raise InsufficientFundsError(total=amount, balance=balance)
            new_balance = balance - amount

        stamp = now.isoformat()
        if db.guarded_confirm_update(conn, transaction_id, stamp) != 1:
            raise ValidationError(PROCESSED_MESSAGE)  # someone confirmed it a moment ago
        db.update_user_cash(conn, user_id, new_balance, stamp)  # new value computed in Python
        db.set_transaction_executed(conn, transaction_id, stamp)
    return amount, new_balance


def cancel_pending_cash(conn, user_id, transaction_id, kind):
    """Discard a pending deposit/withdrawal. Confirmed/executed ones are never touched."""
    _check_kind(kind)
    tx = db.get_transaction_for_update(conn, transaction_id)
    if (
        tx is None
        or tx["user_id"] != user_id
        or tx["transaction_type"] != kind
        or tx["status"] != "pending"
    ):
        return
    db.delete_pending_transaction(conn, transaction_id)  # itself limited to status='pending'
