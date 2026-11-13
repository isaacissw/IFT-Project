"""Smoke test proving the pytest harness and the ``db_conn`` fixture work.

This is a trivial test whose only job is to prove that ``pytest -q`` collects
and runs at least one passing test, and that the ``db_conn`` fixture builds a
usable, schema-complete database.
"""

# The six tables defined by schema.sql (SAD ERD names + the three additions).
EXPECTED_TABLES = {
    "users",
    "stocks",
    "holdings",
    "transactions",
    "market_schedule",
    "market_settings",
}


def test_db_conn_has_six_tables(db_conn):
    """The fresh database built from schema.sql contains exactly the six tables."""
    rows = db_conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table'"
    ).fetchall()
    table_names = {row["name"] for row in rows}

    # All six expected tables are present (ignore any SQLite internal tables
    # such as sqlite_sequence created by AUTOINCREMENT).
    assert EXPECTED_TABLES <= table_names


def test_db_conn_is_seeded(db_conn):
    """The ``db_conn`` fixture now yields a SEEDED database (task 4.4).

    Seeding inserts exactly two users (admin + trainee1) and the six demo
    stocks, so a freshly built ``db_conn`` should reflect that known data.
    """
    user_count = db_conn.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"]
    stock_count = db_conn.execute("SELECT COUNT(*) AS n FROM stocks").fetchone()["n"]

    assert user_count == 2
    assert stock_count == 6
