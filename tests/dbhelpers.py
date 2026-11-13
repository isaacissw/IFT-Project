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

from app import db
from app.config import Config  # importing Config also loads .env

# Throwaway passwords so the seed loader can hash something. Not real secrets.
os.environ.setdefault("SEED_ADMIN_PASSWORD", "test-admin-password-not-real")
os.environ.setdefault("SEED_CUSTOMER_PASSWORD", "test-customer-password-not-real")

_schema_ready = False


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
