"""Route tests for the administrator pages (CSRF on, real forms)."""

import re
from decimal import Decimal

import pytest

from app import db
from dbhelpers import ADMIN_PASSWORD, CUSTOMER_PASSWORD, snapshot, stock_id, test_database_url, trainee_id

D = Decimal


def _token(client, path="/admin/"):
    html = client.get(path).get_data(as_text=True)
    return re.search(r'name="csrf_token" value="([^"]+)"', html).group(1)


@pytest.fixture
def conn(app):
    connection = db.get_connection(test_database_url())
    yield connection
    connection.close()


def _stock_form(token, **overrides):
    data = {"csrf_token": token, "company_name": "Acme Rockets", "ticker": "acme", "shares": "5000",
            "price": "19.99", "description": "Rockets."}
    data.update(overrides)
    return data


def _create(admin_client, **overrides):
    data = _stock_form(_token(admin_client), **overrides)
    return admin_client.post("/admin/stocks", data=data)


# ---------------------------- access control ---------------------------------


ADMIN_PAGES = ["/admin/", "/admin/stocks/1/edit"]
ADMIN_POSTS = ["/admin/stocks", "/admin/stocks/1/edit", "/admin/market/schedule", "/admin/market/override"]


@pytest.mark.parametrize("path", ADMIN_PAGES)
def test_logged_out_visitors_are_sent_to_login(anon_client, path):
    response = anon_client.get(path)
    assert response.status_code == 302 and response.headers["Location"] == "/login"


@pytest.mark.parametrize("path", ADMIN_PAGES)
def test_customers_are_sent_back_to_their_own_page(client, path):
    response = client.get(path)
    assert response.status_code == 302 and response.headers["Location"] == "/"


@pytest.mark.parametrize("path", ADMIN_POSTS)
def test_customers_cannot_post_to_admin_actions(client, conn, path):
    """Even with a valid CSRF token, a customer changes nothing."""
    before = snapshot(conn)
    token = re.search(r'name="csrf_token" value="([^"]+)"', client.get("/financial-records").get_data(as_text=True)).group(1)
    response = client.post(path, data={"csrf_token": token, "mode": "force_closed", "company_name": "x"})
    assert response.status_code == 302 and response.headers["Location"] == "/"
    assert snapshot(conn) == before


@pytest.mark.parametrize("path", ADMIN_POSTS)
def test_admin_posts_need_a_csrf_token(admin_client, conn, path):
    before = snapshot(conn)
    assert admin_client.post(path, data={"mode": "force_closed"}).status_code == 400
    assert snapshot(conn) == before


def test_admin_dashboard_lists_the_inventory(admin_client):
    html = admin_client.get("/admin/").get_data(as_text=True)
    assert "Create New Stock" in html and "Market Hours" in html and "Stock Inventory" in html
    for ticker in ("SSOL", "DSRT", "CPTR", "MESA", "SAGU", "GCNY"):
        assert ticker in html
    assert "$42.50" in html and "1,000,000" in html


# ---------------------------- create a stock ----------------------------------


def test_create_stock_end_to_end(admin_client, client, conn):
    response = _create(admin_client)
    assert response.status_code == 302 and response.headers["Location"] == "/admin/"
    page = admin_client.get("/admin/").get_data(as_text=True)
    assert "Stock ACME was created." in page and "Acme Rockets" in page and "$19.99" in page

    stock = db.get_stock_by_ticker(conn, "ACME")
    assert stock["current_price"] == D("19.99") and stock["company_shares_outstanding"] == 5000
    assert stock["created_by"] == db.get_user_by_username(conn, "admin")["user_id"]

    # Customers see it on their dashboard straight away.
    assert "Acme Rockets" in client.get("/").get_data(as_text=True)


def test_duplicate_ticker_shows_an_error(admin_client, conn):
    before = snapshot(conn)
    response = _create(admin_client, ticker="SSOL")
    assert response.status_code == 400
    assert "A stock with that ticker already exists." in response.get_data(as_text=True)
    assert snapshot(conn) == before


@pytest.mark.parametrize(
    "overrides, message",
    [
        ({"company_name": ""}, "Please enter the company name."),
        ({"ticker": "TOOLONGX"}, "The ticker must be 1-5 letters"),
        ({"shares": "0"}, "Shares outstanding must be a whole number greater than 0."),
        ({"price": "-3"}, "The initial price must be greater than $0.00"),
        ({"price": "12.345"}, "at most two decimal places"),
    ],
)
def test_validation_errors_are_shown(admin_client, conn, overrides, message):
    before = snapshot(conn)
    response = _create(admin_client, **overrides)
    assert response.status_code == 400
    assert message in response.get_data(as_text=True)
    assert snapshot(conn) == before


def test_sql_injection_in_the_stock_form_is_harmless(admin_client, conn):
    counts = {t: db.count_rows(conn, t) for t in ("users", "stocks", "transactions")}
    _create(admin_client, company_name="x'); DROP TABLE users;--", ticker="A'; --", shares="1; DROP TABLE stocks", price="1 OR 1=1")
    assert {t: db.count_rows(conn, t) for t in counts} == counts


def test_script_in_the_company_name_is_escaped_on_every_page(admin_client, client):
    _create(admin_client, company_name="<script>alert(1)</script>", ticker="XSS")
    for visitor, path in ((admin_client, "/admin/"), (client, "/")):
        html = visitor.get(path).get_data(as_text=True)
        assert "<script>alert(1)</script>" not in html
        assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html


# ---------------------------- edit a stock ----------------------------------


def test_edit_page_shows_ticker_and_price_as_read_only(admin_client, conn):
    html = admin_client.get(f"/admin/stocks/{stock_id(conn, 'SSOL')}/edit").get_data(as_text=True)
    assert "SSOL (cannot be changed)" in html and "$42.50" in html
    assert 'name="price"' not in html and 'name="ticker"' not in html  # nothing to edit


def test_edit_a_stock_end_to_end(admin_client, conn):
    ssol = stock_id(conn, "SSOL")
    data = {"csrf_token": _token(admin_client), "company_name": "Sonoran Solar Group", "shares": "2000000", "description": "Sun."}
    response = admin_client.post(f"/admin/stocks/{ssol}/edit", data=data)
    assert response.status_code == 302
    stock = db.get_stock(conn, ssol)
    assert (stock["company_name"], stock["company_shares_outstanding"], stock["description"]) == ("Sonoran Solar Group", 2_000_000, "Sun.")


def test_a_price_sent_with_an_edit_is_ignored(admin_client, conn):
    """The SAD forbids admins changing a price after creation, even by forging the request."""
    ssol = stock_id(conn, "SSOL")
    data = {"csrf_token": _token(admin_client), "company_name": "Sonoran Solar Corp", "shares": "1000000",
            "description": "", "price": "1.00", "current_price": "1.00"}
    admin_client.post(f"/admin/stocks/{ssol}/edit", data=data)
    assert db.get_stock(conn, ssol)["current_price"] == D("42.50")


def test_edit_cannot_reduce_shares_below_what_is_owned(admin_client, client, conn):
    ssol = stock_id(conn, "SSOL")
    page = client.get(f"/trade/{ssol}")
    token = re.search(r'name="csrf_token" value="([^"]+)"', page.get_data(as_text=True)).group(1)
    review = client.post("/trade/buy/review", data={"csrf_token": token, "stock_id": ssol, "quantity": "100"}).get_data(as_text=True)
    client.post("/trade/buy/confirm", data={"csrf_token": token, "transaction_id": re.search(r'name="transaction_id" value="(\d+)"', review).group(1)})

    data = {"csrf_token": _token(admin_client), "company_name": "Sonoran Solar Corp", "shares": "50", "description": ""}
    response = admin_client.post(f"/admin/stocks/{ssol}/edit", data=data)
    assert response.status_code == 400
    assert "currently own" in response.get_data(as_text=True)
    assert db.get_stock(conn, ssol)["company_shares_outstanding"] == 1_000_000


def test_editing_a_missing_stock_is_a_404(admin_client):
    assert admin_client.get("/admin/stocks/999999/edit").status_code == 404
    data = {"csrf_token": _token(admin_client), "company_name": "x", "shares": "1"}
    assert admin_client.post("/admin/stocks/999999/edit", data=data).status_code == 404


# ---------------------------- market controls ----------------------------------


def _schedule_form(token, open_="09:00", close="17:00", closed=("Saturday", "Sunday")):
    data = {"csrf_token": token}
    for day in ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"):
        data[f"open_{day}"], data[f"close_{day}"] = open_, close
        if day in closed:
            data[f"closed_{day}"] = "1"
    return data


def test_the_schedule_form_shows_the_current_hours(admin_client):
    html = admin_client.get("/admin/").get_data(as_text=True)
    assert 'name="open_Monday"' in html and 'name="closed_Sunday"' in html
    assert "currently" in html and "OPEN" in html  # the demo market is open all day


def test_saving_business_hours_changes_the_market(admin_client, conn):
    response = admin_client.post("/admin/market/schedule", data=_schedule_form(_token(admin_client)))
    assert response.status_code == 302
    rows = {r["day_of_week"]: r for r in db.list_market_schedule(conn)}
    assert rows["Monday"]["market_open_time"] == "09:00:00" and rows["Monday"]["market_close_time"] == "17:00:00"
    assert rows["Saturday"]["is_holiday"] and rows["Sunday"]["is_holiday"] and not rows["Friday"]["is_holiday"]
    page = admin_client.get("/admin/").get_data(as_text=True)
    assert "09:00" in page and "17:00" in page


def test_bad_hours_show_an_error_and_keep_what_was_typed(admin_client, conn):
    before = snapshot(conn)
    data = _schedule_form(_token(admin_client), closed=())
    data["open_Monday"], data["close_Monday"] = "18:00", "08:00"
    response = admin_client.post("/admin/market/schedule", data=data)
    html = response.get_data(as_text=True)
    assert response.status_code == 400
    assert "Monday: The opening time must be earlier than the closing time." in html
    assert 'value="18:00"' in html
    assert snapshot(conn) == before


@pytest.mark.parametrize("mode, text", [("force_closed", "forced CLOSED"), ("force_open", "forced OPEN"), ("none", "override is off")])
def test_override_toggle(admin_client, conn, mode, text):
    response = admin_client.post("/admin/market/override", data={"csrf_token": _token(admin_client), "mode": mode}, follow_redirects=True)
    assert text in response.get_data(as_text=True)
    assert db.get_market_settings(conn)["override_mode"] == mode


def test_a_bad_override_value_is_refused(admin_client, conn):
    before = snapshot(conn)
    response = admin_client.post("/admin/market/override", data={"csrf_token": _token(admin_client), "mode": "banana"}, follow_redirects=True)
    assert "Please choose one of the three market override options." in response.get_data(as_text=True)
    assert snapshot(conn) == before


def test_forcing_the_market_closed_stops_customers_trading(admin_client, client, conn):
    admin_client.post("/admin/market/override", data={"csrf_token": _token(admin_client), "mode": "force_closed"})
    ssol = stock_id(conn, "SSOL")
    page = client.get(f"/trade/{ssol}")
    token = re.search(r'name="csrf_token" value="([^"]+)"', page.get_data(as_text=True)).group(1)
    response = client.post("/trade/buy/review", data={"csrf_token": token, "stock_id": ssol, "quantity": "1"})
    assert response.status_code == 400
    assert "Market Closed" in response.get_data(as_text=True)

    admin_client.post("/admin/market/override", data={"csrf_token": _token(admin_client), "mode": "none"})
    response = client.post("/trade/buy/review", data={"csrf_token": token, "stock_id": ssol, "quantity": "1"})
    assert "Confirm Your Purchase" in response.get_data(as_text=True)
