"""Administrator blueprint: stock inventory and market controls.

Everything here needs a logged-in administrator (see ``_administrators_only``).
Like every route file it stays thin: read the form, call
``app/services/admin.py``, then render a page or redirect.
"""

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
from app.routes.auth import require_role
from app.services import admin as admin_service
from app.services import market
from app.services.admin import FormError, WEEKDAYS
from app.services.trading import ValidationError

bp = Blueprint("admin", __name__, url_prefix="/admin")


@bp.before_request
def _administrators_only():
    return require_role("admin")


def _open_db():
    return db.get_connection(current_app.config["DATABASE_URL"])


def _tz_name():
    return current_app.config.get("MARKET_TIMEZONE", "America/New_York")


def _admin_id():
    return g.current_user["user_id"]


def _schedule_rows_from_db(conn):
    return [
        {
            "day": row["day_of_week"],
            "closed": bool(row["is_holiday"]),
            "open": str(row["market_open_time"])[:5],
            "close": str(row["market_close_time"])[:5],
        }
        for row in db.list_market_schedule(conn)
    ]


def _render_dashboard(stock_values=None, stock_errors=None, schedule_rows=None, schedule_errors=None, status=200):
    conn = _open_db()
    try:
        inventory = []
        for stock in db.list_stocks(conn):
            held = db.get_shares_held_sum(conn, stock["stock_id"])
            inventory.append({**stock, "shares_available": int(stock["company_shares_outstanding"]) - held})
        settings = db.get_market_settings(conn)
        market_open = market.is_market_open(conn, now_utc(), _tz_name())
        rows = schedule_rows if schedule_rows is not None else _schedule_rows_from_db(conn)
    finally:
        conn.close()
    return (
        render_template(
            "admin_dashboard.html",
            inventory=inventory,
            override_mode=settings["override_mode"],
            market_open=market_open,
            schedule=rows,
            schedule_errors=schedule_errors or {},
            stock_values=stock_values or {},
            stock_errors=stock_errors or {},
            tz_name=_tz_name(),
        ),
        status,
    )


@bp.route("/")
def dashboard():
    return _render_dashboard()


# --- create a stock -----------------------------------------------------------------


@bp.route("/stocks", methods=["POST"])
def create_stock():
    form = request.form
    values = {k: form.get(k, "") for k in ("company_name", "ticker", "shares", "price", "description")}

    conn = _open_db()
    try:
        admin_service.create_stock(
            conn, _admin_id(), values["company_name"], values["ticker"], values["shares"],
            values["price"], values["description"], now_utc(),
        )
    except FormError as err:
        return _render_dashboard(stock_values=values, stock_errors=err.errors, status=400)
    finally:
        conn.close()

    flash(("success", f"Stock {values['ticker'].strip().upper()} was created."))
    return redirect(url_for("admin.dashboard"))


# --- edit a stock ----------------------------------------------------------------------


def _render_edit(stock, values, errors, status=200):
    return (
        render_template("admin_edit_stock.html", stock=stock, values=values, errors=errors),
        status,
    )


@bp.route("/stocks/<int:stock_id>/edit", methods=["GET", "POST"])
def edit_stock(stock_id):
    conn = _open_db()
    try:
        stock = db.get_stock(conn, stock_id)
    finally:
        conn.close()
    if stock is None:
        abort(404, "Stock not found.")

    if request.method == "GET":
        values = {
            "company_name": stock["company_name"],
            "shares": str(stock["company_shares_outstanding"]),
            "description": stock["description"] or "",
        }
        return _render_edit(stock, values, {})

    form = request.form
    values = {k: form.get(k, "") for k in ("company_name", "shares", "description")}
    conn = _open_db()
    try:
        admin_service.update_stock(conn, stock_id, values["company_name"], values["description"], values["shares"])
    except FormError as err:
        return _render_edit(stock, values, err.errors, 400)
    except ValidationError:
        abort(404, "Stock not found.")
    finally:
        conn.close()

    flash(("success", f"Stock {stock['ticker_symbol']} was updated."))
    return redirect(url_for("admin.dashboard"))


# --- market controls ------------------------------------------------------------------------


@bp.route("/market/schedule", methods=["POST"])
def save_schedule():
    form = request.form
    days = {
        day: {
            "closed": form.get(f"closed_{day}") is not None,
            "open": form.get(f"open_{day}", ""),
            "close": form.get(f"close_{day}", ""),
        }
        for day in WEEKDAYS
    }
    conn = _open_db()
    try:
        admin_service.update_market_schedule(conn, days, now_utc())
    except FormError as err:
        rows = [{"day": d, **days[d]} for d in WEEKDAYS]
        return _render_dashboard(schedule_rows=rows, schedule_errors=err.errors, status=400)
    finally:
        conn.close()
    flash(("success", "Market hours were saved."))
    return redirect(url_for("admin.dashboard"))


@bp.route("/market/override", methods=["POST"])
def save_override():
    mode = request.form.get("mode", "")
    conn = _open_db()
    try:
        admin_service.set_market_override(conn, _admin_id(), mode, now_utc())
    except ValidationError as err:
        flash(("error", str(err)))
    else:
        messages = {
            "none": "Market override is off: the market follows its schedule.",
            "force_open": "Market is manually forced OPEN.",
            "force_closed": "Market is manually forced CLOSED.",
        }
        flash(("success", messages[mode]))
    finally:
        conn.close()
    return redirect(url_for("admin.dashboard"))
