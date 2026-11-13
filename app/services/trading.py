"""Trading service.

This module defines the typed exception hierarchy used by the buy flow and
(in later tasks) the buy operations: ``parse_quantity``, ``quote_buy``,
``create_pending_buy``, ``confirm_buy``, and ``cancel_pending_buy``, along with
the ``Quote`` dataclass.

Only the exception classes and the pure helpers ``parse_quantity`` and
``compute_total_cost`` are implemented here (tasks 6.1 and 6.3). The remaining
function bodies are implemented in later tasks. This module performs no Flask
imports; all money-valued attributes are :class:`decimal.Decimal`.
"""

from dataclasses import dataclass
from decimal import Decimal

from app import db
from app.money import MAX_MONEY, quantize, to_money
from app.services import market

# The largest whole-share quantity a single buy may request.
MAX_QUANTITY = 1_000_000


class ValidationError(Exception):
    """Input or business-rule check failed.

    Carries a plain, user-facing message string (``str(err)``). The route layer
    renders this message directly.
    """


class MarketClosedError(Exception):
    """Trade attempted while the market is closed.

    Carries no extra data; the route layer supplies the fixed heading/body.
    """


class InsufficientFundsError(Exception):
    """Cash balance is less than the order total.

    Carries the order ``total`` and the current cash ``balance`` (both
    :class:`~decimal.Decimal`) so the route can format
    "This order costs $X but your cash balance is $Y.".
    """

    def __init__(self, total: Decimal, balance: Decimal):
        self.total = total
        self.balance = balance
        super().__init__(
            f"Order total {total} exceeds cash balance {balance}."
        )


class InsufficientSharesError(Exception):
    """Requested quantity exceeds the shares available to buy.

    Carries the number of shares ``available`` (``int``) and the ``ticker`` so
    the route can format "Only N shares of TICKER are available.".
    """

    def __init__(self, available: int, ticker: str):
        self.available = available
        self.ticker = ticker
        super().__init__(
            f"Only {available} shares of {ticker} are available."
        )


class PriceChangedError(Exception):
    """Stock price changed between review and confirm.

    Carries the ``ticker`` and the ``old`` and ``new`` prices (both
    :class:`~decimal.Decimal`) so the route can format
    "The price of TICKER changed from $A to $B. Please review your order again.".
    """

    def __init__(self, ticker: str, old: Decimal, new: Decimal):
        self.ticker = ticker
        self.old = old
        self.new = new
        super().__init__(
            f"The price of {ticker} changed from {old} to {new}."
        )


def parse_quantity(raw) -> int:
    """Parse a share quantity from raw form input into a whole integer.

    Accepts only a plain string of ASCII base-10 digits (after stripping
    surrounding whitespace) representing a whole number from 1 through
    1,000,000.

    Using ``str.isdigit()`` rather than ``int()`` is deliberate: it rejects
    ``"1.5"``, ``"1e3"``, ``"-3"``, ``"+3"``, and ``""`` before any conversion,
    so only clean whole numbers ever reach ``int()``. Because ``str.isdigit()``
    also returns True for some non-ASCII Unicode digit characters, we
    additionally require every character to be an ASCII digit so that only
    standard base-10 whole numbers pass.

    :raises ValidationError: with ``Please enter a whole number of shares
        greater than 0.`` for empty, spaces-only, ``0``, negative, decimal,
        scientific-notation, or any other non-numeric input; and with
        ``Please enter a whole number of shares no more than 1,000,000.`` for a
        valid whole number greater than 1,000,000.
    """
    s = str(raw).strip()
    # isdigit() rejects "", "-1", "+3", "1.5", "1e3", "abc"; the ASCII check
    # rejects Unicode digit look-alikes so only base-10 whole numbers pass.
    if not s.isdigit() or not all(c in "0123456789" for c in s):
        raise ValidationError(
            "Please enter a whole number of shares greater than 0."
        )
    value = int(s)
    if value < 1:
        # Covers "0" (and "000") which are digit strings but not > 0.
        raise ValidationError(
            "Please enter a whole number of shares greater than 0."
        )
    if value > MAX_QUANTITY:
        raise ValidationError(
            "Please enter a whole number of shares no more than 1,000,000."
        )
    return value


def compute_total_cost(price, quantity: int) -> Decimal:
    """Compute the exact buy total: ``quantize(price * quantity)``.

    ``price`` arrives as a :class:`~decimal.Decimal` (from the DB) or a string;
    it is passed through :func:`app.money.to_money` so the result is always a
    Decimal built from text, never a float. ``quantity`` is a whole-share
    integer. The product is quantized to two decimal places using
    ROUND_HALF_UP.

    :raises ValidationError: when the computed total exceeds
        :data:`app.money.MAX_MONEY` (99999999.99), which no money column can
        hold.
    """
    total = quantize(to_money(price) * quantity)
    if total > MAX_MONEY:
        raise ValidationError("Order total is too large.")
    return total


@dataclass(frozen=True)
class Quote:
    """The read-only result of pricing a buy order.

    Produced by :func:`quote_buy` after every buy validation passes. A quote is
    purely a calculation: it writes nothing to the database. All money-valued
    fields are :class:`~decimal.Decimal`.

    Fields (order matters; see design.md "Quote return shape"):

      - ``stock``: the stock row as a plain ``dict`` of its columns
        (``stock_id``, ``ticker_symbol``, ``company_name``, ``current_price``,
        ...). The :class:`sqlite3.Row` is converted to a dict so the quote is a
        self-contained, immutable snapshot.
      - ``quantity``: the whole-share count being priced (``int``).
      - ``price_per_share``: the stock's current price as a Decimal.
      - ``total_cost``: ``quantize(price_per_share * quantity)``.
      - ``cash_before``: the user's cash balance before the (hypothetical) buy.
      - ``cash_after``: ``cash_before - total_cost``.
    """

    stock: dict
    quantity: int
    price_per_share: Decimal
    total_cost: Decimal
    cash_before: Decimal
    cash_after: Decimal


def _validate_buy(conn, user_id, stock_id, quantity, now, tz_name):
    """Run the shared buy validations in the fixed order (Requirement 13.1).

    Both :func:`quote_buy` and (later) ``create_pending_buy`` call this so they
    enforce exactly the same rules, in exactly the same sequence, and raise the
    first failing one. Nothing is written to the database here; this function
    only reads through ``app/db.py``.

    The caller passes an already-parsed ``int`` ``quantity`` (the route runs
    :func:`parse_quantity` first so the trade page can retain the raw input), so
    quantity is not re-parsed here.

    Order (the first failing check is the one raised):

      1. **Stock exists** — else :class:`ValidationError` ``"Stock not found."``.
      2. **Quantity valid** — the caller already parsed it to a valid int; no
         re-raise here.
      3. **Market open** — else :class:`MarketClosedError`.
      4. **Shares available** — ``company_shares_outstanding -
         get_shares_held_sum`` must be ``>= quantity``; else
         :class:`InsufficientSharesError`.
      5. **Funds** — ``compute_total_cost`` must be ``<= cash_balance``; else
         :class:`InsufficientFundsError`.

    Returns a tuple ``(stock, user, total, cash_before)`` so callers can build a
    quote or a pending row without re-reading. ``stock`` is the raw
    :class:`sqlite3.Row`; ``cash_before`` and ``total`` are
    :class:`~decimal.Decimal`.
    """
    # 1) Stock exists.
    stock = db.get_stock(conn, stock_id)
    if stock is None:
        raise ValidationError("Stock not found.")

    # 2) Quantity valid: the caller passed an already-parsed int. Nothing to do.

    # 3) Market open.
    if not market.is_market_open(conn, now, tz_name):
        raise MarketClosedError()

    # 4) Shares available = outstanding - already-held.
    shares_available = (
        int(stock["company_shares_outstanding"])
        - db.get_shares_held_sum(conn, stock_id)
    )
    if shares_available < quantity:
        raise InsufficientSharesError(
            available=shares_available, ticker=stock["ticker_symbol"]
        )

    # 5) Funds. compute_total_cost may raise ValidationError on overflow.
    total = compute_total_cost(stock["current_price"], quantity)
    user = db.get_user_by_id(conn, user_id)
    cash_before = to_money(user["cash_balance"])
    if cash_before < total:
        raise InsufficientFundsError(total=total, balance=cash_before)

    return stock, user, total, cash_before


def quote_buy(conn, user_id, stock_id, quantity, now,
              tz_name=market.DEFAULT_MARKET_TIMEZONE):
    """Price a buy order without writing anything to the database.

    Runs the shared buy validations in the fixed order (see
    :func:`_validate_buy`) and, when they all pass, returns a frozen
    :class:`Quote` describing the hypothetical purchase. No row is inserted,
    updated, or deleted; a quote is a pure calculation the review screen shows.

    ``quantity`` is an already-parsed whole-share ``int`` (the route runs
    :func:`parse_quantity` first). ``now`` is a timezone-aware UTC datetime
    injected by the caller. ``tz_name`` defaults to the market service's default
    time zone so this service stays free of Flask config; the route passes the
    configured ``MARKET_TIMEZONE``.

    :raises ValidationError: stock missing (``"Stock not found."``) or total
        overflow.
    :raises MarketClosedError: the market is closed at ``now``.
    :raises InsufficientSharesError: fewer shares are available than requested.
    :raises InsufficientFundsError: the user's cash is less than the total.
    """
    stock, _user, total, cash_before = _validate_buy(
        conn, user_id, stock_id, quantity, now, tz_name
    )

    price_per_share = to_money(stock["current_price"])
    return Quote(
        stock=dict(stock),
        quantity=quantity,
        price_per_share=price_per_share,
        total_cost=total,
        cash_before=cash_before,
        cash_after=cash_before - total,
    )


def create_pending_buy(conn, user_id, stock_id, quantity, now,
                       tz_name=market.DEFAULT_MARKET_TIMEZONE):
    """Record a single pending buy order and return its new ``transaction_id``.

    Runs the SAME shared buy validations in the SAME fixed order as
    :func:`quote_buy` (see :func:`_validate_buy`) BEFORE writing anything, so a
    failing check raises its typed exception and no row is inserted. When every
    check passes, inserts exactly one ``transactions`` row via
    :func:`app.db.insert_pending_buy` with ``transaction_type='buy'``,
    ``status='pending'``, the quoted ``price_per_share`` (the stock's current
    price as a Decimal) and the validated ``total_amount``, ``created_at =
    now.isoformat()``, and ``confirmed_at``/``executed_at`` NULL.

    This writes a pending order only: the user's ``cash_balance`` and every
    ``holdings`` row are left unchanged (money and shares move later, at
    :func:`confirm_buy`). All SQL lives in ``app/db.py``; this function issues
    no SQL itself.

    ``quantity`` is an already-parsed whole-share ``int`` (the route runs
    :func:`parse_quantity` first). ``now`` is a timezone-aware UTC datetime
    injected by the caller; its ``isoformat()`` string is stored as
    ``created_at``. ``tz_name`` defaults to the market service's default time
    zone so this service stays free of Flask config.

    :raises ValidationError: stock missing (``"Stock not found."``) or total
        overflow.
    :raises MarketClosedError: the market is closed at ``now``.
    :raises InsufficientSharesError: fewer shares are available than requested.
    :raises InsufficientFundsError: the user's cash is less than the total.
    """
    stock, _user, total, _cash_before = _validate_buy(
        conn, user_id, stock_id, quantity, now, tz_name
    )

    price_per_share = to_money(stock["current_price"])
    return db.insert_pending_buy(
        conn,
        user_id,
        stock_id,
        quantity,
        price_per_share,
        total,
        now.isoformat(),
    )

def confirm_buy(conn, user_id, transaction_id, now,
                tz_name=market.DEFAULT_MARKET_TIMEZONE):
    """Confirm and execute a pending buy as one all-or-nothing operation.

    Every read and write happens inside a single ``db.transaction()``
    (``BEGIN IMMEDIATE``). Any exception rolls the whole thing back, so cash,
    holdings, and the transaction row never end up partially updated.

    Steps (re-validated at commit time; browser-sent values are never trusted):
      1. Re-read the pending row. It must exist, belong to ``user_id``, be a
         ``buy``, and still be ``pending`` -- else raise ValidationError.
      2. Re-check the market is open -- else MarketClosedError.
      3. Re-read the stock's current price. If it differs from the price the
         user reviewed, delete the stale pending row and raise PriceChangedError
         so the user must review again at the new price.
      4. Re-check shares available and funds.
      5. Guarded single-row UPDATE pending -> confirmed (blocks double-submit).
      6. Debit cash (computed in Python), add shares, mark executed, commit.
    """
    with db.transaction(conn):
        tx = db.get_transaction_for_update(conn, transaction_id)
        if (
            tx is None
            or tx["user_id"] != user_id
            or tx["transaction_type"] != "buy"
            or tx["status"] != "pending"
        ):
            raise ValidationError(
                "This order has already been processed or does not exist."
            )

        # 2) Market must still be open.
        if not market.is_market_open(conn, now, tz_name):
            raise MarketClosedError()

        stock = db.get_stock(conn, tx["stock_id"])
        # Stock could in principle be gone; treat as a validation failure.
        if stock is None:
            raise ValidationError("Stock not found.")

        # 3) Price must be unchanged since review. Compare as Decimals.
        reviewed_price = to_money(tx["price_per_share"])
        current_price = to_money(stock["current_price"])
        if current_price != reviewed_price:
            # Discard the stale pending row (same transaction) and make the
            # user review again at the new price.
            db.delete_pending_transaction(conn, transaction_id)
            raise PriceChangedError(
                ticker=stock["ticker_symbol"],
                old=reviewed_price,
                new=current_price,
            )

        quantity = int(tx["quantity"])
        total = to_money(tx["total_amount"])

        # 4) Re-check shares available and funds inside the transaction.
        shares_available = (
            int(stock["company_shares_outstanding"])
            - db.get_shares_held_sum(conn, tx["stock_id"])
        )
        if shares_available < quantity:
            raise InsufficientSharesError(
                available=shares_available, ticker=stock["ticker_symbol"]
            )

        user = db.get_user_by_id(conn, user_id)
        cash_before = to_money(user["cash_balance"])
        if cash_before < total:
            raise InsufficientFundsError(total=total, balance=cash_before)

        # 5) Guarded confirm: exactly one pending row must flip to confirmed.
        now_iso = now.isoformat()
        affected = db.guarded_confirm_update(conn, transaction_id, now_iso)
        if affected != 1:
            # Someone already confirmed this order (double-submit); abort with
            # no change (the transaction rolls back).
            raise ValidationError(
                "This order has already been processed or does not exist."
            )

        # 6) Apply the purchase: debit cash (Python math), add shares, execute.
        new_cash = cash_before - total
        db.update_user_cash(conn, user_id, new_cash, now_iso)
        db.upsert_holding(conn, user_id, tx["stock_id"], quantity, now_iso)
        db.set_transaction_executed(conn, transaction_id, now_iso)
        # Commit happens on clean exit of the transaction() context manager.


def cancel_pending_buy(conn, user_id, transaction_id):
    """Cancel a pending buy that belongs to the user.

    Deletes the row only when it belongs to ``user_id`` and is still
    ``pending``. A confirmed or executed transaction is never cancelled or
    deleted (the audit trail is immutable), so this is a no-op in that case.
    """
    tx = db.get_transaction_for_update(conn, transaction_id)
    if (
        tx is None
        or tx["user_id"] != user_id
        or tx["status"] != "pending"
    ):
        return
    # delete_pending_transaction is itself scoped to status='pending'.
    db.delete_pending_transaction(conn, transaction_id)


# ---------------------------------------------------------------------------
# Sell flow (Phase 4) -- mirrors the buy flow above.
#
# Validation order for a sell:
#   1. Stock exists           -> ValidationError("Stock not found.")
#   2. Quantity valid         -> parse_quantity (done by the route)
#   3. Market open            -> MarketClosedError
#   4. User owns enough shares-> InsufficientSharesError(available=owned, ticker)
# Proceeds = quantize(price * quantity), reusing compute_total_cost.
# ---------------------------------------------------------------------------


def _validate_sell(conn, user_id, stock_id, quantity, now, tz_name):
    """Run the shared sell validations in the fixed order.

    Returns ``(stock, owned, proceeds)``. ``owned`` is the user's current
    holding quantity (0 if none). The InsufficientSharesError carries the
    owned quantity as ``available`` so the route can render
    "You own N shares of TICKER." (including N=0).
    """
    # 1) Stock exists.
    stock = db.get_stock(conn, stock_id)
    if stock is None:
        raise ValidationError("Stock not found.")

    # 2) Quantity already parsed to a valid int by the caller.

    # 3) Market open.
    if not market.is_market_open(conn, now, tz_name):
        raise MarketClosedError()

    # 4) User must own at least `quantity` shares.
    holding = db.get_holding(conn, user_id, stock_id)
    owned = int(holding["quantity"]) if holding is not None else 0
    if owned < quantity:
        raise InsufficientSharesError(available=owned, ticker=stock["ticker_symbol"])

    proceeds = compute_total_cost(stock["current_price"], quantity)
    return stock, owned, proceeds


def quote_sell(conn, user_id, stock_id, quantity, now,
               tz_name=market.DEFAULT_MARKET_TIMEZONE):
    """Price a sell order without writing anything to the database.

    Mirrors :func:`quote_buy`. Returns a :class:`Quote` whose ``total_cost``
    field carries the sale proceeds and whose ``cash_after`` is
    ``cash_before + proceeds``.
    """
    stock, _owned, proceeds = _validate_sell(
        conn, user_id, stock_id, quantity, now, tz_name
    )
    user = db.get_user_by_id(conn, user_id)
    cash_before = to_money(user["cash_balance"])
    price_per_share = to_money(stock["current_price"])
    return Quote(
        stock=dict(stock),
        quantity=quantity,
        price_per_share=price_per_share,
        total_cost=proceeds,
        cash_before=cash_before,
        cash_after=cash_before + proceeds,
    )


def create_pending_sell(conn, user_id, stock_id, quantity, now,
                        tz_name=market.DEFAULT_MARKET_TIMEZONE):
    """Record one pending sell order and return its new ``transaction_id``.

    Runs the same sell validations as :func:`quote_sell` before writing. On
    success inserts one ``transactions`` row (``transaction_type='sell'``,
    ``status='pending'``) with the quoted price and proceeds; cash and holdings
    are untouched until :func:`confirm_sell`.
    """
    stock, _owned, proceeds = _validate_sell(
        conn, user_id, stock_id, quantity, now, tz_name
    )
    price_per_share = to_money(stock["current_price"])
    return db.insert_pending_sell(
        conn,
        user_id,
        stock_id,
        quantity,
        price_per_share,
        proceeds,
        now.isoformat(),
    )


def confirm_sell(conn, user_id, transaction_id, now,
                 tz_name=market.DEFAULT_MARKET_TIMEZONE):
    """Confirm and execute a pending sell as one all-or-nothing operation.

    Mirrors :func:`confirm_buy`. In one ``db.transaction()``: re-read/lock the
    pending row (must be this user's pending ``sell``); re-check market open;
    re-check owned quantity; re-check price (delete + PriceChangedError on
    change); guarded single-row confirm; credit cash (old + proceeds, Python
    Decimal, rejecting a balance over 99,999,999.99); decrease the holding
    (deleting the row at 0 so shares return to inventory); mark executed; commit.
    Any failure rolls everything back.
    """
    with db.transaction(conn):
        tx = db.get_transaction_for_update(conn, transaction_id)
        if (
            tx is None
            or tx["user_id"] != user_id
            or tx["transaction_type"] != "sell"
            or tx["status"] != "pending"
        ):
            raise ValidationError(
                "This order has already been processed or does not exist."
            )

        if not market.is_market_open(conn, now, tz_name):
            raise MarketClosedError()

        stock = db.get_stock(conn, tx["stock_id"])
        if stock is None:
            raise ValidationError("Stock not found.")

        # Price unchanged since review.
        reviewed_price = to_money(tx["price_per_share"])
        current_price = to_money(stock["current_price"])
        if current_price != reviewed_price:
            db.delete_pending_transaction(conn, transaction_id)
            raise PriceChangedError(
                ticker=stock["ticker_symbol"],
                old=reviewed_price,
                new=current_price,
            )

        quantity = int(tx["quantity"])
        proceeds = to_money(tx["total_amount"])

        # Re-check the user still owns enough shares.
        holding = db.get_holding(conn, user_id, tx["stock_id"])
        owned = int(holding["quantity"]) if holding is not None else 0
        if owned < quantity:
            raise InsufficientSharesError(
                available=owned, ticker=stock["ticker_symbol"]
            )

        # Guarded confirm: exactly one pending row flips to confirmed.
        now_iso = now.isoformat()
        affected = db.guarded_confirm_update(conn, transaction_id, now_iso)
        if affected != 1:
            raise ValidationError(
                "This order has already been processed or does not exist."
            )

        # Credit cash (Python Decimal); reject if it would overflow the column.
        user = db.get_user_by_id(conn, user_id)
        cash_before = to_money(user["cash_balance"])
        new_cash = cash_before + proceeds
        if new_cash > MAX_MONEY:
            raise ValidationError("Resulting cash balance is too large.")
        db.update_user_cash(conn, user_id, new_cash, now_iso)

        # Return shares to inventory by reducing/removing the holding.
        db.reduce_holding(conn, user_id, tx["stock_id"], quantity, now_iso)
        db.set_transaction_executed(conn, transaction_id, now_iso)


def cancel_pending_sell(conn, user_id, transaction_id):
    """Cancel a pending sell that belongs to the user (no-op otherwise)."""
    tx = db.get_transaction_for_update(conn, transaction_id)
    if (
        tx is None
        or tx["user_id"] != user_id
        or tx["status"] != "pending"
    ):
        return
    db.delete_pending_transaction(conn, transaction_id)
