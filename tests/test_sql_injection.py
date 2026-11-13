"""SQL-injection tests: hostile input must be rejected and must not touch the database."""

import re

from app import db
from dbhelpers import snapshot, stock_id, test_database_url

TABLES = ("users", "stocks", "holdings", "transactions", "market_schedule", "market_settings")


def _row_counts(conn):
    return {table: db.count_rows(conn, table) for table in TABLES}


def _table_names(conn):
    rows = conn.execute(
        "SELECT table_name AS name FROM information_schema.tables WHERE table_schema = DATABASE()"
    ).fetchall()
    return {row["name"] for row in rows}


def _token(client, conn):
    page = client.get(f"/trade/{stock_id(conn, 'SSOL')}")
    return re.search(r'name="csrf_token" value="([^"]+)"', page.get_data(as_text=True)).group(1)


def test_injection_in_the_quantity_field_is_rejected(client):
    conn = db.get_connection(test_database_url())
    try:
        counts, tables, before = _row_counts(conn), _table_names(conn), snapshot(conn)
        response = client.post(
            "/trade/buy/review",
            data={
                "csrf_token": _token(client, conn),
                "stock_id": stock_id(conn, "SSOL"),
                "quantity": "1; DROP TABLE users;--",
            },
        )
        assert response.status_code == 400
        assert "Please enter a whole number of shares greater than 0." in response.get_data(as_text=True)
        assert _table_names(conn) == tables == set(TABLES)
        assert _row_counts(conn) == counts
        assert snapshot(conn) == before
    finally:
        conn.close()


def test_injection_in_the_stock_id_field_is_rejected(client):
    conn = db.get_connection(test_database_url())
    try:
        counts, before = _row_counts(conn), snapshot(conn)
        token = _token(client, conn)
        for kind in ("buy", "sell"):
            response = client.post(
                f"/trade/{kind}/review",
                data={"csrf_token": token, "stock_id": "1 OR 1=1", "quantity": "1"},
            )
            assert response.status_code == 404
        assert _row_counts(conn) == counts
        assert snapshot(conn) == before
    finally:
        conn.close()


def test_injection_in_the_url_is_rejected(client):
    assert client.get("/trade/1 OR 1=1").status_code == 404
    assert client.get("/trade/1;DROP TABLE users").status_code == 404


def test_injection_in_the_transaction_id_is_harmless(client):
    conn = db.get_connection(test_database_url())
    try:
        before = snapshot(conn)
        token = _token(client, conn)
        for path in ("/trade/buy/confirm", "/trade/buy/cancel", "/trade/sell/confirm", "/trade/sell/cancel"):
            response = client.post(
                path, data={"csrf_token": token, "transaction_id": "1 OR 1=1"}, follow_redirects=True
            )
            assert response.status_code == 200
        assert snapshot(conn) == before
    finally:
        conn.close()


def test_hostile_text_is_treated_as_plain_data_by_the_data_layer(db_conn):
    """A classic ' OR '1'='1 username matches nobody, because values are parameters."""
    assert db.get_user_by_username(db_conn, "x' OR '1'='1") is None
    assert db.get_user_by_username(db_conn, "trainee1'; DROP TABLE users;--") is None
    assert db.count_rows(db_conn, "users") == 2
