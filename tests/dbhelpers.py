"""Shared helpers for tests that need a real MySQL database.

Tests never touch the development database. They use a separate database whose
name ends in ``_test`` (by default ``stock_trading_test``, on the same server
and with the same login as ``DATABASE_URL`` in ``.env``). You create that
database once in MySQL Workbench:

    CREATE DATABASE stock_trading_test;
    GRANT ALL ON stock_trading_test.* TO 'stock_app'@'localhost';

Building a schema is slow compared to deleting rows, so the tables are created
once per test run and every later call just empties them and (optionally)
re-seeds. Each test or Hypothesis example therefore still starts from a clean,
known state.
"""

import os
from contextlib import contextmanager
from decimal import Decimal

from app import db
from app.config import Config  # importing Config also loads .env

# Throwaway passwords so the seed loader can hash something. Not real secrets.
os.environ.setdefault("SEED_ADMIN_PASSWORD", "test-admin-password-not-real")
os.environ.setdefault("SEED_CUSTOMER_PASSWORD", "test-customer-password-not-real")

_schema_ready = False

_SNAPSHOT_QUERIES = {
    "users": "SELECT * FROM users ORDER BY user_id",
    "stocks": "SELECT * FROM stocks ORDER BY stock_id",
    "holdings": "SELECT * FROM holdings ORDER BY holdings_id",
    "transactions": "SELECT * FROM transactions ORDER BY transaction_id",
    "market_schedule": "SELECT * FROM market_schedule ORDER BY schedule_id",
    "market_settings": "SELECT * FROM market_settings ORDER BY setting_id",
}


def snapshot(conn):
    """Every row of every table as plain dicts, for exact before/after comparison."""
    return {
        table: [dict(row) for row in conn.execute(sql).fetchall()]
        for table, sql in _SNAPSHOT_QUERIES.items()
    }


# --- Small test-setup helpers (test-only writes; production code never does this) ---


def trainee_id(conn):
    """The seeded ``trainee1`` customer's user_id."""
    return db.get_user_by_username(conn, "trainee1")["user_id"]


def stock_id(conn, ticker):
    """The stock_id of a seeded stock, looked up by ticker."""
    for row in db.list_stocks(conn):
        if row["ticker_symbol"] == ticker:
            return row["stock_id"]
    raise AssertionError(f"seeded stock {ticker!r} not found")


def set_override(conn, mode):
    """Set market_settings.override_mode to 'none', 'force_open' or 'force_closed'."""
    conn.execute("UPDATE market_settings SET override_mode = ? WHERE setting_id = 1", (mode,))


def set_cash(conn, user_id, amount):
    """Set a user's cash balance (amount is a Decimal)."""
    conn.execute("UPDATE users SET cash_balance = ? WHERE user_id = ?", (amount, user_id))


def set_price(conn, ticker, price):
    """Change a stock's price (admins cannot do this in the app; tests simulate the generator)."""
    conn.execute("UPDATE stocks SET current_price = ? WHERE ticker_symbol = ?", (price, ticker))


def add_customer(conn, username="trainee2"):
    """Insert a second customer with 10000.00 cash and return their user_id."""
    return db.insert_user(
        conn,
        first_name="Other",
        last_name="Customer",
        email=f"{username}@example.com",
        username=username,
        password_hash="x",
        user_type="customer",
        cash_balance=Decimal("10000.00"),
        is_active=1,
        created_at="2026-01-01T00:00:00+00:00",
        updated_at="2026-01-01T00:00:00+00:00",
    )


def transactions_for(conn, user_id):
    """All of a user's transaction rows, oldest first."""
    return [
        dict(r)
        for r in conn.execute(
            "SELECT * FROM transactions WHERE user_id = ? ORDER BY transaction_id", (user_id,)
        ).fetchall()
    ]


def test_database_url():
    """The URL of the throwaway test database (never the development one)."""
    explicit = os.environ.get("TEST_DATABASE_URL")
    if explicit:
        url = explicit
    else:
        url = db.with_database_name(Config.DATABASE_URL, "stock_trading_test")
    name = db.parse_database_url(url)["database"]
    # Safety net: these helpers delete data, so refuse anything not named *_test.
    assert name.endswith("_test"), (
        f"Refusing to run tests against database {name!r}; its name must end in '_test'."
    )
    return url


# pytest treats functions named test_* as tests; this is a helper, not a test.
test_database_url.__test__ = False


@contextmanager
def fresh_db(seed=False):
    """Yield a connection to the test database, emptied (and optionally seeded)."""
    global _schema_ready
    from seed import seed_into

    conn = db.get_connection(test_database_url())
    try:
        if not _schema_ready:
            db.drop_all_tables(conn)
            db.init_db(conn)
            _schema_ready = True
        else:
            db.clear_all_rows(conn)
        if seed:
            with db.transaction(conn):
                seed_into(conn)
        yield conn
    finally:
        conn.close()
