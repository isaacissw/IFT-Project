"""Application package for the Stock Trading System.

This package exposes the application factory ``create_app()``, which builds and
configures the Flask app: loading ``Config``, setting the session-cookie flags
required by the standing rules, enabling CSRF protection, registering the
blueprints (login/signup, trading, administrator) and the ``init-db`` CLI
command, and registering the 404 handler and the template filters.

Who is logged in is worked out per request in ``app/routes/auth.py``.
"""

import click
from flask import Flask, current_app, g
from flask.cli import with_appcontext
from flask_wtf.csrf import CSRFProtect

from .config import Config
from . import db as db_module
from .routes.admin import bp as admin_bp
from .routes.auth import bp as auth_bp
from .routes.trading import bp as trading_bp

# One CSRF extension instance, initialised against the app inside create_app().
csrf = CSRFProtect()


def create_app(config_object=Config):
    """Build and configure the Flask application."""
    app = Flask(__name__, instance_relative_config=True)
    app.config.from_object(config_object)

    # Sessions (the "you are logged in" cookie) and CSRF tokens are signed with
    # SECRET_KEY, so the app cannot work safely without one.
    if not app.config.get("SECRET_KEY"):
        raise RuntimeError(
            "SECRET_KEY is not set. Put a long random value in your .env file "
            "(see .env.example)."
        )

    # Session-cookie flags required by the standing security rules: the cookie
    # is not readable from JavaScript (HttpOnly) and is only sent on same-site
    # or top-level navigations (SameSite=Lax).
    app.config["SESSION_COOKIE_HTTPONLY"] = True
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"

    # CSRF protection, so every POST form must carry a valid token.
    csrf.init_app(app)

    # The database is MySQL, reached through DATABASE_URL (see .env). Every
    # request opens its own connection through app/db.py.

    app.register_blueprint(auth_bp)
    app.register_blueprint(trading_bp)
    app.register_blueprint(admin_bp)
    app.cli.add_command(init_db_command)

    @app.errorhandler(404)
    def _not_found(error):
        # Routes that abort(404, "Stock not found.") get that message shown.
        return error.description or "Not Found", 404

    @app.context_processor
    def _inject_current_user():
        # Makes ``current_user`` available in every template (None if logged out).
        return {"current_user": g.get("current_user")}

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
