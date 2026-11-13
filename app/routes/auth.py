"""Authentication blueprint: signup, login and logout, plus the access guards.

Routes stay thin: they read the form, call ``app/services/auth.py``, and then keep
the session. Who is logged in is remembered in Flask's signed session cookie as
just a ``user_id``; on every request that id is looked up in the database again,
so a deleted or deactivated account is logged out immediately and the user's
role is never trusted from the cookie.
"""

from flask import (
    Blueprint,
    current_app,
    flash,
    g,
    redirect,
    render_template,
    request,
    session,
    url_for,
)

from app import db
from app.clock import now_utc
from app.services import auth as auth_service
from app.services.auth import LoginError, SignupError

bp = Blueprint("auth", __name__)


def _open_db():
    return db.get_connection(current_app.config["DATABASE_URL"])


@bp.before_app_request
def load_current_user():
    """Before every request: work out who (if anyone) is logged in -> ``g.current_user``."""
    g.current_user = None
    if request.endpoint == "static":
        return
    user_id = session.get("user_id")
    if user_id is None:
        return
    conn = _open_db()
    try:
        user = db.get_user_by_id(conn, user_id)
    finally:
        conn.close()
    if user is None or not user["is_active"]:
        session.clear()  # the account is gone or switched off: log them out
        return
    g.current_user = user


def home_url(user):
    """Where a logged-in user belongs: customers trade, administrators administer."""
    if user["user_type"] == "admin":
        return url_for("admin.dashboard")
    return url_for("trading.dashboard")


def require_role(role):
    """Guard used by other blueprints' ``before_request``.

    Returns ``None`` when the current user may continue. Otherwise returns the
    redirect to send them to: the login page if nobody is logged in, or their own
    home page if they are logged in with the wrong role.
    """
    user = g.get("current_user")
    if user is None:
        flash(("error", "Please log in to continue."))
        return redirect(url_for("auth.login"))
    if user["user_type"] != role:
        return redirect(home_url(user))
    return None


def _start_session(user_id):
    # Wipe whatever the browser had before, so an old session id can never be
    # "upgraded" into a logged-in one (session fixation).
    session.clear()
    session["user_id"] = user_id


@bp.route("/login", methods=["GET", "POST"])
def login():
    if g.current_user is not None:
        return redirect(home_url(g.current_user))

    if request.method == "GET":
        return render_template("login.html", username="", error=None)

    username = request.form.get("username", "")
    conn = _open_db()
    try:
        user = auth_service.authenticate(conn, username, request.form.get("password", ""))
    except LoginError as err:
        return (
            render_template(
                "login.html",
                username=username.strip(),
                error={"heading": "Login failed", "body": str(err)},
            ),
            401,
        )
    finally:
        conn.close()

    _start_session(user["user_id"])
    return redirect(home_url(user))


@bp.route("/signup", methods=["GET", "POST"])
def signup():
    if g.current_user is not None:
        return redirect(home_url(g.current_user))

    if request.method == "GET":
        return render_template("signup.html", values={}, errors={})

    form = request.form
    conn = _open_db()
    try:
        user_id = auth_service.register_customer(
            conn,
            form.get("first_name", ""),
            form.get("last_name", ""),
            form.get("email", ""),
            form.get("username", ""),
            form.get("password", ""),
            form.get("confirm_password", ""),
            now_utc(),
        )
    except SignupError as err:
        # Passwords are never sent back to the page; everything else is kept.
        kept = {k: form.get(k, "") for k in ("first_name", "last_name", "email", "username")}
        return render_template("signup.html", values=kept, errors=err.errors), 400
    finally:
        conn.close()

    _start_session(user_id)
    flash(("success", "Welcome! Your account has been created."))
    return redirect(url_for("trading.dashboard"))


@bp.route("/logout", methods=["POST"])
def logout():
    session.clear()
    flash(("success", "You have been logged out."))
    return redirect(url_for("auth.login"))
