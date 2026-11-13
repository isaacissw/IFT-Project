"""Application package for the Stock Trading System (Phase 1).

This package exposes the application factory ``create_app()``, which builds and
configures the Flask app: loading ``Config``, setting the session-cookie flags
required by the standing rules, enabling CSRF protection, ensuring the instance
folder exists, registering the trading blueprint and the ``init-db`` CLI
command, resolving the fixed demo user, and registering a 404 handler.

This task (1.3) is wiring only. The route bodies live in ``app/routes`` and are
filled in by later tasks; the seeding logic behind ``init-db`` is filled in by
task 4.3; the demo-user lookup goes through ``app/db.py`` read functions, which
may not all exist yet, so demo-user resolution is done lazily on the first
request and guarded so the app can be created before the database exists.
"""

import os

import click
from flask import Flask, current_app
from flask.cli import with_appcontext
from flask_wtf.csrf import CSRFProtect

from .config import Config
from . import db as db_module
from .routes.trading import bp as trading_bp

# One CSRF extension instance, initialised against the app inside create_app().
csrf = CSRFProtect()


def create_app(config_object=Config):
    """Build and configure the Flask application.

    Follows the design's "Application factory wiring" section step by step.
    """
    # 1. Create the app and load configuration. ``instance_relative_config``
    #    makes Flask aware of the instance/ folder convention.
    app = Flask(__name__, instance_relative_config=True)
    app.config.from_object(config_object)

    # 2. Session-cookie flags required by the standing security rules: the
    #    cookie is not readable from JavaScript (HttpOnly) and is only sent on
    #    same-site or top-level navigations (SameSite=Lax). SECRET_KEY comes
    #    from Config.
    app.config["SESSION_COOKIE_HTTPONLY"] = True
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"

    # 3. CSRF protection, so every POST form must carry a valid token.
    csrf.init_app(app)

    # 4. The database is MySQL, reached through DATABASE_URL (see .env). Every
    #    request opens its own connection through app/db.py.

    # 5. Resolve the demo user lazily on the first request. Doing it at import
    #    or create_app() time would fail before the database is initialised
    #    (e.g. before `flask --app app init-db` has ever run), so instead we
    #    start it unresolved and fill it in on demand. get_current_user_id()
    #    (task 8.1) reads app.config["RESOLVED_DEMO_USER_ID"].
    app.config.setdefault("RESOLVED_DEMO_USER_ID", None)

    @app.before_request
    def _resolve_demo_user():
        _ensure_demo_user_resolved(app)

    # 6. Register the trading blueprint (dashboard + trade routes; bodies added
    #    in task 8.1).
    app.register_blueprint(trading_bp)

    # 7. Register the init-db CLI command (`flask --app app init-db`). Its body
    #    is completed in task 4.3.
    app.cli.add_command(init_db_command)

    # 8. Register the 404 handler.
    @app.errorhandler(404)
    def _not_found(error):
        # The message body is intentionally minimal here; routes that need the
        # "Stock not found." text abort with their own message in task 8.1.
        return "Not Found", 404

    # Jinja filters used by the templates.
    from app.money import format_money as _format_money
    from datetime import datetime as _dt
    from zoneinfo import ZoneInfo as _ZoneInfo

    def _money_filter(value):
        # Format a Decimal (or Decimal-able) as $1,234.56.
        return _format_money(_to_decimal(value))

    def _to_decimal(value):
        from decimal import Decimal, InvalidOperation
        from jinja2 import Undefined
        # Treat missing/blank values as zero so a page that does not pass a
        # money value to the shared nav bar renders $0.00 instead of 500ing.
        if value is None or isinstance(value, Undefined):
            return Decimal("0")
        if isinstance(value, Decimal):
            return value
        try:
            return Decimal(str(value))
        except (InvalidOperation, ValueError):
            return Decimal("0")

    def _et_datetime_filter(iso_text, tz_name="America/New_York"):
        # Convert a stored ISO-8601 UTC string to local market time for display.
        if not iso_text:
            return ""
        dt = _dt.fromisoformat(iso_text)
        return dt.astimezone(_ZoneInfo(tz_name)).strftime("%Y-%m-%d %H:%M:%S")

    app.jinja_env.filters["money"] = _money_filter
    app.jinja_env.filters["et_datetime"] = _et_datetime_filter

    return app


def _ensure_demo_user_resolved(app):
    """Resolve the demo customer's user_id once and cache it in config.

    Resolution order (Requirement 5.1, 5.2):
      1. If DEMO_USER_ID is set in config, use it.
      2. Otherwise look up the user with username ``trainee1`` via app/db.py.

    This is best-effort and guarded: if the database is not yet initialised, or
    the read helper does not exist yet, we simply leave the value unresolved so
    the request can proceed (get_current_user_id, added in task 8.1, decides
    what to do when it is still None).
    """
    if app.config.get("RESOLVED_DEMO_USER_ID") is not None:
        return

    # Prefer the explicit environment-provided id when present.
    configured = app.config.get("DEMO_USER_ID")
    if configured:
        try:
            app.config["RESOLVED_DEMO_USER_ID"] = int(configured)
        except (TypeError, ValueError):
            app.config["RESOLVED_DEMO_USER_ID"] = configured
        return

    # Fall back to looking up username "trainee1". Guard every step so app
    # creation / first request never crashes before the DB (or the read
    # helper) exists; later tasks provide get_user_by_username and the DB.
    get_user_by_username = getattr(db_module, "get_user_by_username", None)
    if get_user_by_username is None:
        return

    try:
        conn = db_module.get_connection(app.config["DATABASE_URL"])
    except Exception:
        return

    try:
        row = get_user_by_username(conn, "trainee1")
    except Exception:
        row = None
    finally:
        try:
            conn.close()
        except Exception:
            pass

    if row is not None:
        app.config["RESOLVED_DEMO_USER_ID"] = row["user_id"]


# The six tables whose seeded row counts the init-db summary prints. This is
# the same whitelist app/db.py.count_rows validates against; listing it here in
# a fixed order only decides the print order -- the names are never user input.
_SUMMARY_TABLES = (
    "users",
    "stocks",
    "holdings",
    "transactions",
    "market_schedule",
    "market_settings",
)


@click.command("init-db")
@with_appcontext
def init_db_command():
    """Drop and recreate the database, seed it, then print per-table counts.

    Steps (design "Database Initialization and Seeding", Requirement 4.1):

      1. Connect to the MySQL database named in DATABASE_URL (the database
         itself must already exist; create it once in MySQL Workbench).
      2. Drop every project table so the recreate starts from a clean slate
         with no stale rows and no UNIQUE-constraint collisions on re-run.
      3. Build the schema from schema.sql via app/db.py's init_db().
      4. Run the seed loader inside one transaction() so every seeded row is
         committed together (and rolled back as a unit on any error).
      5. Print the seeded row count for each of the six whitelisted tables.

    All SQL stays in app/db.py: this command only calls db helpers
    (get_connection, drop_all_tables, init_db, count_rows) and seed_into. It
    never writes raw SQL and never prints any secret (the seed passwords are
    read and hashed inside seed_into and are not touched here).
    """
    # Imported here (not at module top) so app creation does not depend on
    # seed.py, which imports from this package.
    from seed import seed_into

    # 1-3. Connect, drop the old tables, recreate the schema.
    conn = db_module.get_connection(current_app.config["DATABASE_URL"])
    try:
        db_module.drop_all_tables(conn)
        db_module.init_db(conn)

        # 4. Seed inside one all-or-nothing transaction so the rows persist
        #    together in the file. get_connection uses isolation_level=None, so
        #    wrapping the inserts in transaction() (BEGIN IMMEDIATE ... COMMIT)
        #    is what commits them.
        with db_module.transaction(conn):
            seed_into(conn)

        # 5. Print the per-table summary. No secret is ever printed.
        click.echo("Database initialized and seeded. Row counts:")
        for table in _SUMMARY_TABLES:
            count = db_module.count_rows(conn, table)
            click.echo(f"  {table}: {count}")
    finally:
        conn.close()
