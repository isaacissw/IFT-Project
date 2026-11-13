"""The SAD rule: an administrator sets only the INITIAL price; afterwards ONLY the system's
price generator changes it.

These tests read the source code to prove that rule holds everywhere in ``app/``, and
then check it end to end through the admin pages.
"""

import ast
import pathlib
import re
from decimal import Decimal

import pytest

from app import db
from dbhelpers import snapshot, stock_id, test_database_url

APP_DIR = pathlib.Path(__file__).resolve().parent.parent / "app"
D = Decimal


def _py_files():
    return sorted(APP_DIR.rglob("*.py"))


def _string_constants(tree):
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            yield node


def test_only_one_function_in_db_py_updates_a_price():
    """Any SQL that UPDATEs the stocks table and mentions current_price lives in update_stock_price."""
    tree = ast.parse((APP_DIR / "db.py").read_text(encoding="utf-8"))
    writers = set()
    for func in [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]:
        for const in _string_constants(func):
            sql = " ".join(const.value.split()).upper()
            if sql.startswith("UPDATE STOCKS") and "CURRENT_PRICE" in sql:
                writers.add(func.name)
    assert writers == {"update_stock_price"}


def test_no_price_writing_sql_exists_outside_db_py():
    offenders = []
    for path in _py_files():
        if path.name == "db.py" and path.parent == APP_DIR:
            continue
        for const in _string_constants(ast.parse(path.read_text(encoding="utf-8"))):
            if re.search(r"\bcurrent_price\s*=(?!=)", const.value):
                offenders.append(f"{path.relative_to(APP_DIR.parent)}:{const.lineno}")
    assert offenders == []


def test_only_the_pricing_service_calls_the_price_writer():
    callers = []
    for path in _py_files():
        if path.name == "db.py" and path.parent == APP_DIR:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and getattr(node.func, "attr", None) == "update_stock_price":
                callers.append(str(path.relative_to(APP_DIR)))
    assert callers == [str(pathlib.Path("services") / "pricing.py")]


def test_the_stock_edit_function_has_no_price_in_its_sql():
    tree = ast.parse((APP_DIR / "db.py").read_text(encoding="utf-8"))
    func = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "update_stock_details")
    statements = [" ".join(c.value.split()) for c in _string_constants(func) if c.value.lstrip().upper().startswith("UPDATE")]
    assert len(statements) == 1 and statements[0].startswith("UPDATE stocks")
    assert "price" not in statements[0].lower()  # the docstring may talk about price; the SQL may not


def test_the_scanner_would_catch_a_second_price_writer():
    """Self-check of the first scan's logic on a made-up offender."""
    bad = 'def sneaky(conn):\n    conn.execute("UPDATE stocks SET current_price = ? WHERE stock_id = ?", (1, 2))'
    found = set()
    for func in [n for n in ast.walk(ast.parse(bad)) if isinstance(n, ast.FunctionDef)]:
        for const in _string_constants(func):
            sql = " ".join(const.value.split()).upper()
            if sql.startswith("UPDATE STOCKS") and "CURRENT_PRICE" in sql:
                found.add(func.name)
    assert found == {"sneaky"}


# ---- through the real pages --------------------------------------------------------------


@pytest.fixture
def conn(app):
    connection = db.get_connection(test_database_url())
    yield connection
    connection.close()


def _token(client, path):
    return re.search(r'name="csrf_token" value="([^"]+)"', client.get(path).get_data(as_text=True)).group(1)


def test_an_admin_cannot_change_a_price_through_any_page(admin_client, conn):
    ssol = stock_id(conn, "SSOL")
    token = _token(admin_client, "/admin/")
    forged = {"csrf_token": token, "price": "1.00", "current_price": "1.00", "initial_price": "1.00"}
    admin_client.post(f"/admin/stocks/{ssol}/edit", data={**forged, "company_name": "Sonoran Solar Corp", "shares": "1000000"})
    admin_client.post("/admin/stocks", data={**forged, "company_name": "Sonoran Solar Corp", "ticker": "SSOL", "shares": "10"})
    admin_client.post("/admin/market/override", data={**forged, "mode": "none"})
    admin_client.post("/admin/market/schedule", data=forged)
    stock = db.get_stock(conn, ssol)
    assert stock["current_price"] == D("42.50")
    assert stock["price_updated_at"] == db.get_stock(conn, ssol)["price_updated_at"]


def test_a_customer_cannot_change_a_price(client, conn):
    before = snapshot(conn)
    token = _token(client, "/financial-records")
    client.post(f"/admin/stocks/{stock_id(conn, 'SSOL')}/edit", data={"csrf_token": token, "current_price": "1.00"})
    assert snapshot(conn) == before


def test_the_creation_price_is_the_only_price_an_admin_ever_enters(admin_client, conn):
    token = _token(admin_client, "/admin/")
    admin_client.post(
        "/admin/stocks",
        data={"csrf_token": token, "company_name": "Acme", "ticker": "ACME", "shares": "100", "price": "19.99", "description": ""},
    )
    assert db.get_stock_by_ticker(conn, "ACME")["current_price"] == D("19.99")
