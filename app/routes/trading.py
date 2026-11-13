"""Trading blueprint: the customer dashboard and the buy / sell pages.

URL map (what each web address does):
  GET  /                   the Customer Trading Dashboard
  GET  /trade/<stock_id>   the trade page for one stock (?mode=sell for selling)
  POST /trade/buy/review   "Review Order"  -> save a pending buy, show confirm page
  POST /trade/buy/confirm  "Confirm"       -> execute it, back to the dashboard
  POST /trade/buy/cancel   "Cancel"        -> discard the pending buy
  (the same three for /trade/sell/...)

Routes are deliberately thin. Each one reads the form, calls the service that
holds the rules (app/services/trading.py), and then shows a page or redirects.
No SQL and no business rules live here -- those are in app/db.py and app/services/.
A "blueprint" is just Flask's name for a group of related routes.

The current user's identity always comes from the server-side session (see
app/routes/auth.py), never from a form field or URL parameter.
"""

from decimal import Decimal

from flask import (
    Blueprint,
    abort,
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
from app.money import format_money, to_money
from app.routes.auth import require_role
from app.services import trading
from app.services.portfolio import portfolio_value
from app.services.trading import (
    InsufficientFundsError,
    InsufficientSharesError,
    MarketClosedError,
    PriceChangedError,
    ValidationError,
)

bp = Blueprint("trading", __name__)


@bp.before_request
def _customers_only():
    """Every trading page needs a logged-in customer (see app/routes/auth.py)."""
    return require_role("customer")


def get_current_user_id():
    """The logged-in customer's id, taken from the server-side session only.

    This is the single identity seam: it is never read from a form field or a
    URL. The ``_customers_only`` guard above has already made sure someone is
    logged in as a customer before any route in this file runs.
    """
    return g.current_user["user_id"]


def _tz_name():
    """The configured market time zone, passed to the market service."""
    return current_app.config.get("MARKET_TIMEZONE", "America/New_York")


def _open_db():
    """Open a request-scoped DB connection via the data-access module."""
    return db.get_connection(current_app.config["DATABASE_URL"])


_portfolio_value = portfolio_value  # shared with the Financial Records page


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
                    "price_updated_at": s["price_updated_at"],
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
    """Validate input, create a pending buy, and show the confirmation screen.

    This runs when the customer clicks "Review Order". Nothing is bought yet:
    the order is saved as *pending* and the confirm page is shown.
    """
    # Who is buying always comes from the login session, never from the form.
    user_id = get_current_user_id()
    raw_stock_id = request.form.get("stock_id", "")
    raw_quantity = request.form.get("quantity", "")

    # stock_id must be a plain integer; anything else is "not found" (404).
    try:
        stock_id = int(raw_stock_id)
    except (TypeError, ValueError):
        abort(404, "Stock not found.")

    # Check the quantity is a whole number 1..1,000,000. On a bad value we show
    # the trade page again with an error AND the text the customer typed.
    try:
        quantity = trading.parse_quantity(raw_quantity)
    except ValidationError as err:
        return _render_trade_error(stock_id, raw_quantity, "Invalid quantity", str(err))

    conn = _open_db()
    try:
        # Save the pending order (this also runs every buy rule) ...
        tx_id = trading.create_pending_buy(
            conn, user_id, stock_id, quantity, now_utc(), _tz_name()
        )
        # ... and work out the numbers the confirm page shows (cost, cash after).
        quote = trading.quote_buy(
            conn, user_id, stock_id, quantity, now_utc(), _tz_name()
        )
    # Each kind of rule failure gets its own heading and message on the page.
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
    """Confirm (execute) a pending buy, then redirect to the dashboard.

    This runs when the customer clicks "Confirm Transaction". The service
    re-checks everything and moves the money and shares in one all-or-nothing
    step. Afterwards we REDIRECT (Post-Redirect-Get) instead of showing a page
    directly, so pressing the browser's refresh button cannot buy twice.
    """
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
        # Read the order first so we can word the success message afterwards.
        tx = db.get_transaction_for_update(conn, tx_id)
        # The real work: re-check and execute (raises an error if anything fails).
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
