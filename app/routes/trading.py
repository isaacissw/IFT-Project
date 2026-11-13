"""Trading blueprint: the dashboard and the buy flow.

Routes are deliberately thin. Each one parses request input, calls exactly one
service function, and then renders a template or issues a redirect. No SQL and
no business rules live here -- those are in app/db.py and app/services/*.

The current user's identity always comes from the server (the fixed demo user
for Phase 1), never from a form field or URL parameter.
"""

from decimal import Decimal

from flask import (
    Blueprint,
    abort,
    current_app,
    flash,
    redirect,
    render_template,
    request,
    url_for,
)

from app import db
from app.clock import now_utc
from app.money import format_money, to_money
from app.services import trading
from app.services.trading import (
    InsufficientFundsError,
    InsufficientSharesError,
    MarketClosedError,
    PriceChangedError,
    ValidationError,
)

bp = Blueprint("trading", __name__)


def get_current_user_id():
    """Resolve the current user's id from the server session/config only.

    Phase 1 has no login, so this returns the fixed demo customer's id that the
    application factory resolved at startup (from DEMO_USER_ID or by looking up
    username ``trainee1``). This is the single identity seam: Phase 3 swaps in a
    real session by changing only this function. The id is never read from a
    form field or URL.
    """
    user_id = current_app.config.get("RESOLVED_DEMO_USER_ID")
    if user_id is None:
        # The before_request hook resolves this lazily; if it is still unset the
        # database has not been initialized/seeded yet.
        abort(500, "Demo user is not resolved. Run 'flask --app app init-db'.")
    return user_id


def _tz_name():
    """The configured market time zone, passed to the market service."""
    return current_app.config.get("MARKET_TIMEZONE", "America/New_York")


def _open_db():
    """Open a request-scoped DB connection via the data-access module."""
    return db.get_connection(current_app.config["DATABASE_URL"])


def _portfolio_value(holdings):
    """Sum quantity * current_price across holdings, as a Decimal."""
    total = Decimal("0.00")
    for h in holdings:
        total += to_money(h["current_price"]) * int(h["quantity"])
    return total


@bp.route("/")
def dashboard():
    """Customer trading dashboard (SAD Wireframe 1)."""
    user_id = get_current_user_id()
    conn = _open_db()
    try:
        user = db.get_user_by_id(conn, user_id)
        stocks = db.list_stocks(conn)
        # Shares available per stock (outstanding - held across all users).
        stock_rows = []
        for s in stocks:
            held = db.get_shares_held_sum(conn, s["stock_id"])
            stock_rows.append(
                {
                    "stock_id": s["stock_id"],
                    "company_name": s["company_name"],
                    "ticker_symbol": s["ticker_symbol"],
                    "current_price": to_money(s["current_price"]),
                    "shares_available": int(s["company_shares_outstanding"]) - held,
                }
            )
        holdings = db.list_holdings_for_user(conn, user_id)
        holding_rows = []
        for h in holdings:
            price = to_money(h["current_price"])
            holding_rows.append(
                {
                    "stock_id": h["stock_id"],
                    "company_name": h["company_name"],
                    "ticker_symbol": h["ticker_symbol"],
                    "quantity": int(h["quantity"]),
                    "current_price": price,
                    "market_value": price * int(h["quantity"]),
                }
            )
        recent = db.get_recent_executed_transactions(conn, user_id, 5)
        cash_balance = to_money(user["cash_balance"])
        portfolio_value = _portfolio_value(holdings)
    finally:
        conn.close()

    return render_template(
        "dashboard.html",
        cash_balance=cash_balance,
        portfolio_value=portfolio_value,
        stocks=stock_rows,
        holdings=holding_rows,
        recent=recent,
        tz_name=_tz_name(),
    )


def _trade_context(conn, user_id, stock):
    """Build the template context shared by the trade page and its error form."""
    held = db.get_shares_held_sum(conn, stock["stock_id"])
    user = db.get_user_by_id(conn, user_id)
    holding = db.get_holding(conn, user_id, stock["stock_id"])
    owned = int(holding["quantity"]) if holding is not None else 0
    # The shared nav bar shows cash and portfolio value on every page, so
    # compute portfolio value here too (sum of quantity * current_price).
    portfolio_value = _portfolio_value(db.list_holdings_for_user(conn, user_id))
    return {
        "stock_id": stock["stock_id"],
        "company_name": stock["company_name"],
        "ticker_symbol": stock["ticker_symbol"],
        "current_price": to_money(stock["current_price"]),
        "shares_available": int(stock["company_shares_outstanding"]) - held,
        "cash_balance": to_money(user["cash_balance"]),
        "portfolio_value": portfolio_value,
        "owned_shares": owned,
    }


@bp.route("/trade/<int:stock_id>")
def trade(stock_id):
    """Trade entry page for one stock (buy or sell mode)."""
    user_id = get_current_user_id()
    mode = request.args.get("mode", "buy")
    if mode not in ("buy", "sell"):
        mode = "buy"
    conn = _open_db()
    try:
        stock = db.get_stock(conn, stock_id)
        if stock is None:
            abort(404, "Stock not found.")
        ctx = _trade_context(conn, user_id, stock)
    finally:
        conn.close()
    return render_template("trade.html", mode=mode, quantity="", error=None, **ctx)


def _render_trade_error(stock_id, quantity, heading, body, mode="buy"):
    """Re-render the trade page with an error box and the retained quantity."""
    user_id = get_current_user_id()
    conn = _open_db()
    try:
        stock = db.get_stock(conn, stock_id)
        if stock is None:
            abort(404, "Stock not found.")
        ctx = _trade_context(conn, user_id, stock)
    finally:
        conn.close()
    return render_template(
        "trade.html",
        mode=mode,
        quantity=quantity,
        error={"heading": heading, "body": body},
        **ctx,
    ), 400


@bp.route("/trade/buy/review", methods=["POST"])
def buy_review():
    """Validate input, create a pending buy, and show the confirmation screen."""
    user_id = get_current_user_id()
    raw_stock_id = request.form.get("stock_id", "")
    raw_quantity = request.form.get("quantity", "")

    # stock_id must be a plain integer; a bad id is a 404 (not found).
    try:
        stock_id = int(raw_stock_id)
    except (TypeError, ValueError):
        abort(404, "Stock not found.")

    # Parse quantity first so we can retain the raw input on error.
    try:
        quantity = trading.parse_quantity(raw_quantity)
    except ValidationError as err:
        return _render_trade_error(stock_id, raw_quantity, "Invalid quantity", str(err))

    conn = _open_db()
    try:
        tx_id = trading.create_pending_buy(
            conn, user_id, stock_id, quantity, now_utc(), _tz_name()
        )
        quote = trading.quote_buy(
            conn, user_id, stock_id, quantity, now_utc(), _tz_name()
        )
    except ValidationError as err:
        conn.close()
        if str(err) == "Stock not found.":
            abort(404, "Stock not found.")
        return _render_trade_error(stock_id, raw_quantity, "Invalid order", str(err))
    except MarketClosedError:
        conn.close()
        return _render_trade_error(
            stock_id, raw_quantity, "Market Closed",
            "Trading is only allowed during market hours.",
        )
    except InsufficientSharesError as err:
        conn.close()
        return _render_trade_error(
            stock_id, raw_quantity, "Insufficient Shares Available",
            f"Only {err.available} shares of {err.ticker} are available.",
        )
    except InsufficientFundsError as err:
        conn.close()
        return _render_trade_error(
            stock_id, raw_quantity, "Insufficient Funds",
            f"This order costs {format_money(err.total)} but your cash balance is {format_money(err.balance)}.",
        )
    finally:
        try:
            conn.close()
        except Exception:
            pass

    return render_template(
        "confirm.html",
        transaction_id=tx_id,
        company_name=quote.stock["company_name"],
        ticker_symbol=quote.stock["ticker_symbol"],
        quantity=quote.quantity,
        price_per_share=quote.price_per_share,
        total_cost=quote.total_cost,
        cash_before=quote.cash_before,
        cash_after=quote.cash_after,
    )


@bp.route("/trade/buy/confirm", methods=["POST"])
def buy_confirm():
    """Confirm (execute) a pending buy, then redirect to the dashboard."""
    user_id = get_current_user_id()
    raw_tx = request.form.get("transaction_id", "")
    raw_stock_id = request.form.get("stock_id", "")
    try:
        tx_id = int(raw_tx)
    except (TypeError, ValueError):
        flash(("error", "This order has already been processed or does not exist."))
        return redirect(url_for("trading.dashboard"))

    conn = _open_db()
    try:
        # Capture details for the success flash before the row is gone from view.
        tx = db.get_transaction_for_update(conn, tx_id)
        trading.confirm_buy(conn, user_id, tx_id, now_utc(), _tz_name())
        ticker = None
        if tx is not None and tx["stock_id"] is not None:
            stock = db.get_stock(conn, tx["stock_id"])
            ticker = stock["ticker_symbol"] if stock else None
        if tx is not None:
            msg = (
                f"Purchased {int(tx['quantity'])} shares of {ticker} at "
                f"{format_money(to_money(tx['price_per_share']))} for "
                f"{format_money(to_money(tx['total_amount']))}."
            )
            flash(("success", msg))
        return redirect(url_for("trading.dashboard"))
    except PriceChangedError as err:
        flash((
            "error",
            f"The price of {err.ticker} changed from {format_money(err.old)} to {format_money(err.new)}. Please review your order again.",
        ))
        # Back to the trade page so the user can review at the new price.
        try:
            sid = int(raw_stock_id)
            return redirect(url_for("trading.trade", stock_id=sid))
        except (TypeError, ValueError):
            return redirect(url_for("trading.dashboard"))
    except MarketClosedError:
        flash(("error", "Market Closed: Trading is only allowed during market hours."))
        return redirect(url_for("trading.dashboard"))
    except InsufficientSharesError as err:
        flash(("error", f"Only {err.available} shares of {err.ticker} are available."))
        return redirect(url_for("trading.dashboard"))
    except InsufficientFundsError as err:
        flash((
            "error",
            f"This order costs {format_money(err.total)} but your cash balance is {format_money(err.balance)}.",
        ))
        return redirect(url_for("trading.dashboard"))
    except ValidationError as err:
        flash(("error", str(err)))
        return redirect(url_for("trading.dashboard"))
    finally:
        conn.close()


@bp.route("/trade/buy/cancel", methods=["POST"])
def buy_cancel():
    """Cancel a pending buy, then redirect to the dashboard."""
    user_id = get_current_user_id()
    raw_tx = request.form.get("transaction_id", "")
    try:
        tx_id = int(raw_tx)
    except (TypeError, ValueError):
        return redirect(url_for("trading.dashboard"))
    conn = _open_db()
    try:
        trading.cancel_pending_buy(conn, user_id, tx_id)
    finally:
        conn.close()
    flash(("success", "Order cancelled."))
    return redirect(url_for("trading.dashboard"))


@bp.route("/trade/sell/review", methods=["POST"])
def sell_review():
    """Validate input, create a pending sell, and show the confirmation screen."""
    user_id = get_current_user_id()
    raw_stock_id = request.form.get("stock_id", "")
    raw_quantity = request.form.get("quantity", "")

    try:
        stock_id = int(raw_stock_id)
    except (TypeError, ValueError):
        abort(404, "Stock not found.")

    try:
        quantity = trading.parse_quantity(raw_quantity)
    except ValidationError as err:
        return _render_trade_error(stock_id, raw_quantity, "Invalid quantity", str(err), mode="sell")

    conn = _open_db()
    try:
        tx_id = trading.create_pending_sell(
            conn, user_id, stock_id, quantity, now_utc(), _tz_name()
        )
        quote = trading.quote_sell(
            conn, user_id, stock_id, quantity, now_utc(), _tz_name()
        )
    except ValidationError as err:
        conn.close()
        if str(err) == "Stock not found.":
            abort(404, "Stock not found.")
        return _render_trade_error(stock_id, raw_quantity, "Invalid order", str(err), mode="sell")
    except MarketClosedError:
        conn.close()
        return _render_trade_error(
            stock_id, raw_quantity, "Market Closed",
            "Trading is only allowed during market hours.", mode="sell",
        )
    except InsufficientSharesError as err:
        conn.close()
        return _render_trade_error(
            stock_id, raw_quantity, "Insufficient Shares",
            f"You own {err.available} shares of {err.ticker}.", mode="sell",
        )
    finally:
        try:
            conn.close()
        except Exception:
            pass

    return render_template(
        "confirm.html",
        mode="sell",
        transaction_id=tx_id,
        company_name=quote.stock["company_name"],
        ticker_symbol=quote.stock["ticker_symbol"],
        quantity=quote.quantity,
        price_per_share=quote.price_per_share,
        total_cost=quote.total_cost,
        cash_before=quote.cash_before,
        cash_after=quote.cash_after,
    )


@bp.route("/trade/sell/confirm", methods=["POST"])
def sell_confirm():
    """Confirm (execute) a pending sell, then redirect to the dashboard."""
    user_id = get_current_user_id()
    raw_tx = request.form.get("transaction_id", "")
    raw_stock_id = request.form.get("stock_id", "")
    try:
        tx_id = int(raw_tx)
    except (TypeError, ValueError):
        flash(("error", "This order has already been processed or does not exist."))
        return redirect(url_for("trading.dashboard"))

    conn = _open_db()
    try:
        tx = db.get_transaction_for_update(conn, tx_id)
        trading.confirm_sell(conn, user_id, tx_id, now_utc(), _tz_name())
        ticker = None
        if tx is not None and tx["stock_id"] is not None:
            stock = db.get_stock(conn, tx["stock_id"])
            ticker = stock["ticker_symbol"] if stock else None
        if tx is not None:
            msg = (
                f"Sold {int(tx['quantity'])} shares of {ticker} at "
                f"{format_money(to_money(tx['price_per_share']))} for "
                f"{format_money(to_money(tx['total_amount']))}."
            )
            flash(("success", msg))
        return redirect(url_for("trading.dashboard"))
    except PriceChangedError as err:
        flash((
            "error",
            f"The price of {err.ticker} changed from {format_money(err.old)} to {format_money(err.new)}. Please review your order again.",
        ))
        try:
            sid = int(raw_stock_id)
            return redirect(url_for("trading.trade", stock_id=sid, mode="sell"))
        except (TypeError, ValueError):
            return redirect(url_for("trading.dashboard"))
    except MarketClosedError:
        flash(("error", "Market Closed: Trading is only allowed during market hours."))
        return redirect(url_for("trading.dashboard"))
    except InsufficientSharesError as err:
        flash(("error", f"You own {err.available} shares of {err.ticker}."))
        return redirect(url_for("trading.dashboard"))
    except ValidationError as err:
        flash(("error", str(err)))
        return redirect(url_for("trading.dashboard"))
    finally:
        conn.close()


@bp.route("/trade/sell/cancel", methods=["POST"])
def sell_cancel():
    """Cancel a pending sell, then redirect to the dashboard."""
    user_id = get_current_user_id()
    raw_tx = request.form.get("transaction_id", "")
    try:
        tx_id = int(raw_tx)
    except (TypeError, ValueError):
        return redirect(url_for("trading.dashboard"))
    conn = _open_db()
    try:
        trading.cancel_pending_sell(conn, user_id, tx_id)
    finally:
        conn.close()
    flash(("success", "Order cancelled."))
    return redirect(url_for("trading.dashboard"))
