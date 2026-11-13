"""Cash blueprint: the Financial Records page and cash deposits / withdrawals.

Flow (SAD process maps #3 and #6): enter an amount -> review screen -> confirm
or cancel. Like the trading routes these stay thin: read the form, call
``app/services/cash.py``, then render a page or redirect (Post-Redirect-Get
after every successful change). The customer is always the logged-in user from
the session, never something sent by the form.
"""

from flask import (
    Blueprint,
    current_app,
    flash,
    g,
    redirect,
    render_template,
    request,
    url_for,
)

from app import db
from app.clock import now_utc
from app.money import format_money
from app.routes.auth import require_role
from app.services import cash
from app.services.cash import DEPOSIT, WITHDRAWAL
from app.services.portfolio import portfolio_value
from app.services.trading import InsufficientFundsError, ValidationError

bp = Blueprint("cash", __name__)

# The web address uses "withdraw"; the database type is "withdrawal".
_KIND_FOR_URL = {"deposit": DEPOSIT, "withdraw": WITHDRAWAL}
_NOUN = {DEPOSIT: "Deposit", WITHDRAWAL: "Withdrawal"}


@bp.before_request
def _customers_only():
    return require_role("customer")


def _open_db():
    return db.get_connection(current_app.config["DATABASE_URL"])


def _user_id():
    return g.current_user["user_id"]


def _tz_name():
    return current_app.config.get("MARKET_TIMEZONE", "America/New_York")


def _render_records(error=None, error_kind=None, amount="", status=200):
    """Render the Financial Records page (balance, both forms, transaction history)."""
    conn = _open_db()
    try:
        user = db.get_user_by_id(conn, _user_id())
        holdings = db.list_holdings_for_user(conn, _user_id())
        history = db.get_recent_executed_transactions(conn, _user_id(), 100)
        cash_balance = user["cash_balance"]
        value = portfolio_value(holdings)
    finally:
        conn.close()
    return (
        render_template(
            "financial_records.html",
            cash_balance=cash_balance,
            portfolio_value=value,
            history=history,
            tz_name=_tz_name(),
            error=error,
            error_kind=error_kind,
            amount=amount,
        ),
        status,
    )


@bp.route("/financial-records")
def records():
    return _render_records()


@bp.route("/cash/<any(deposit, withdraw):url_kind>/review", methods=["POST"])
def review(url_kind):
    kind = _KIND_FOR_URL[url_kind]
    raw_amount = request.form.get("amount", "")

    conn = _open_db()
    try:
        amount = cash.parse_amount(raw_amount)
        tx_id = cash.create_pending_cash(conn, _user_id(), kind, amount, now_utc())
        quote = cash.quote_cash(conn, _user_id(), kind, amount)
    except ValidationError as err:
        return _render_records(
            {"heading": "Invalid amount", "body": str(err)}, url_kind, raw_amount, status=400
        )
    except InsufficientFundsError as err:
        return _render_records(
            {
                "heading": "Insufficient Funds",
                "body": f"This withdrawal of {format_money(err.total)} exceeds your cash balance of {format_money(err.balance)}.",
            },
            url_kind,
            raw_amount,
            status=400,
        )
    finally:
        conn.close()

    return render_template(
        "confirm_cash.html",
        url_kind=url_kind,
        noun=_NOUN[kind],
        transaction_id=tx_id,
        amount=quote.amount,
        balance_before=quote.balance_before,
        balance_after=quote.balance_after,
        cash_balance=quote.balance_before,
        portfolio_value=None,
    )


@bp.route("/cash/<any(deposit, withdraw):url_kind>/confirm", methods=["POST"])
def confirm(url_kind):
    kind = _KIND_FOR_URL[url_kind]
    try:
        tx_id = int(request.form.get("transaction_id", ""))
    except ValueError:
        flash(("error", cash.PROCESSED_MESSAGE))
        return redirect(url_for("cash.records"))

    conn = _open_db()
    try:
        amount, new_balance = cash.confirm_cash(conn, _user_id(), tx_id, now_utc(), kind)
    except InsufficientFundsError as err:
        flash((
            "error",
            f"Insufficient Funds: this withdrawal of {format_money(err.total)} exceeds your cash balance of {format_money(err.balance)}.",
        ))
    except ValidationError as err:
        flash(("error", str(err)))
    else:
        verb = "Deposited" if kind == DEPOSIT else "Withdrew"
        flash(("success", f"{verb} {format_money(amount)}. Your new cash balance is {format_money(new_balance)}."))
    finally:
        conn.close()
    return redirect(url_for("cash.records"))


@bp.route("/cash/<any(deposit, withdraw):url_kind>/cancel", methods=["POST"])
def cancel(url_kind):
    kind = _KIND_FOR_URL[url_kind]
    try:
        tx_id = int(request.form.get("transaction_id", ""))
    except ValueError:
        return redirect(url_for("cash.records"))
    conn = _open_db()
    try:
        cash.cancel_pending_cash(conn, _user_id(), tx_id, kind)
    finally:
        conn.close()
    flash(("success", f"{_NOUN[kind]} cancelled."))
    return redirect(url_for("cash.records"))
