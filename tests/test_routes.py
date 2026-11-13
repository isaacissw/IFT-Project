"""Route tests: drive the real pages with Flask's test client (CSRF switched ON).

The ``app`` fixture builds a seeded MySQL test database, so these tests behave
like a person clicking through the site: open a page, grab the hidden CSRF
token from the form, submit it.
"""

import re
from decimal import Decimal

import pytest

from app import db
from dbhelpers import set_override, snapshot, stock_id, test_database_url, trainee_id


def _token(html):
    """The hidden CSRF token from the first form on a page."""
    return re.search(r'name="csrf_token" value="([^"]+)"', html).group(1)


def _tx_id(html):
    return re.search(r'name="transaction_id" value="(\d+)"', html).group(1)


@pytest.fixture
def conn(app):
    """A direct connection to the same test database the app uses."""
    connection = db.get_connection(test_database_url())
    yield connection
    connection.close()


def _review(client, conn, kind, ticker, quantity):
    """Open the trade page and submit the review form; returns the response."""
    sid = stock_id(conn, ticker)
    page = client.get(f"/trade/{sid}?mode={kind}")
    return client.post(
        f"/trade/{kind}/review",
        data={"csrf_token": _token(page.get_data(as_text=True)), "stock_id": sid, "quantity": quantity},
    )


def _confirm(client, review_response, kind):
    html = review_response.get_data(as_text=True)
    return client.post(
        f"/trade/{kind}/confirm",
        data={"csrf_token": _token(html), "transaction_id": _tx_id(html)},
        follow_redirects=True,
    )


# --- Dashboard -----------------------------------------------------------------


def test_dashboard_shows_seeded_stocks_and_cash(client):
    response = client.get("/")
    html = response.get_data(as_text=True)
    assert response.status_code == 200
    for ticker in ("SSOL", "DSRT", "CPTR", "MESA", "SAGU", "GCNY"):
        assert ticker in html
    assert "$10,000.00" in html
    assert "You don't own any stocks yet." in html


def test_trade_page_shows_stock_details(client, conn):
    html = client.get(f"/trade/{stock_id(conn, 'SSOL')}").get_data(as_text=True)
    assert "Sonoran Solar Corp" in html
    assert "$42.50" in html
    assert "1,000,000" in html  # shares available
    assert 'min="1"' in html and 'step="1"' in html


def test_unknown_stock_is_404(client):
    response = client.get("/trade/999999")
    assert response.status_code == 404
    assert "Stock not found." in response.get_data(as_text=True)


# --- Buy and sell flows ----------------------------------------------------------


def test_review_screen_shows_the_order_details(client, conn):
    html = _review(client, conn, "buy", "SSOL", "10").get_data(as_text=True)
    assert "Confirm Your Purchase" in html
    assert "Sonoran Solar Corp (SSOL)" in html
    assert "$425.00" in html  # total cost
    assert "$10,000.00" in html and "$9,575.00" in html  # cash now / after
    assert "Once confirmed, this transaction cannot be cancelled." in html


def test_buy_flow_updates_dashboard_and_database(client, conn):
    review = _review(client, conn, "buy", "SSOL", "10")
    html = _confirm(client, review, "buy").get_data(as_text=True)

    assert "Purchased 10 shares of SSOL at $42.50 for $425.00." in html
    assert "$9,575.00" in html
    assert "You don't own any stocks yet." not in html
    user = trainee_id(conn)
    assert db.get_user_by_id(conn, user)["cash_balance"] == Decimal("9575.00")
    assert db.get_holding(conn, user, stock_id(conn, "SSOL"))["quantity"] == 10


def test_sell_flow_updates_dashboard_and_database(client, conn):
    _confirm(client, _review(client, conn, "buy", "SSOL", "10"), "buy")
    review = _review(client, conn, "sell", "SSOL", "4")
    assert "Confirm Your Sale" in review.get_data(as_text=True)
    html = _confirm(client, review, "sell").get_data(as_text=True)

    assert "Sold 4 shares of SSOL at $42.50 for $170.00." in html
    user = trainee_id(conn)
    assert db.get_user_by_id(conn, user)["cash_balance"] == Decimal("9745.00")
    assert db.get_holding(conn, user, stock_id(conn, "SSOL"))["quantity"] == 6


def test_cancel_discards_the_pending_order(client, conn):
    review = _review(client, conn, "buy", "SSOL", "10")
    html = review.get_data(as_text=True)
    response = client.post(
        "/trade/buy/cancel",
        data={"csrf_token": _token(html), "transaction_id": _tx_id(html)},
        follow_redirects=True,
    )
    assert "Order cancelled." in response.get_data(as_text=True)
    assert db.count_rows(conn, "transactions") == 0
    assert db.get_user_by_id(conn, trainee_id(conn))["cash_balance"] == Decimal("10000.00")


def test_confirming_twice_does_not_apply_twice(client, conn):
    review = _review(client, conn, "buy", "SSOL", "10")
    html = review.get_data(as_text=True)
    form = {"csrf_token": _token(html), "transaction_id": _tx_id(html)}
    client.post("/trade/buy/confirm", data=form)
    second = client.post("/trade/buy/confirm", data=form, follow_redirects=True)

    assert "This order has already been processed or does not exist." in second.get_data(as_text=True)
    assert db.get_user_by_id(conn, trainee_id(conn))["cash_balance"] == Decimal("9575.00")
    assert db.count_rows(conn, "transactions") == 1


def test_successful_confirm_redirects_instead_of_rendering(client, conn):
    """Post-Redirect-Get: refreshing the result page must not repeat the trade."""
    html = _review(client, conn, "buy", "SSOL", "10").get_data(as_text=True)
    response = client.post(
        "/trade/buy/confirm", data={"csrf_token": _token(html), "transaction_id": _tx_id(html)}
    )
    assert response.status_code == 302


# --- Error messages ----------------------------------------------------------------


def test_insufficient_funds_message(client, conn):
    before = snapshot(conn)
    response = _review(client, conn, "buy", "GCNY", "1")
    html = response.get_data(as_text=True)
    assert response.status_code == 400
    assert "Insufficient Funds" in html
    assert "This order costs $12,500.00 but your cash balance is $10,000.00." in html
    assert snapshot(conn) == before


def test_insufficient_shares_message(client, conn):
    response = _review(client, conn, "buy", "SAGU", "51")
    html = response.get_data(as_text=True)
    assert response.status_code == 400
    assert "Insufficient Shares Available" in html
    assert "Only 50 shares of SAGU are available." in html


def test_market_closed_message(client, conn):
    set_override(conn, "force_closed")
    response = _review(client, conn, "buy", "SSOL", "1")
    html = response.get_data(as_text=True)
    assert response.status_code == 400
    assert "Market Closed" in html
    assert "Trading is only allowed during market hours." in html


def test_sell_without_owning_shares_message(client, conn):
    response = _review(client, conn, "sell", "SSOL", "1")
    html = response.get_data(as_text=True)
    assert response.status_code == 400
    assert "Insufficient Shares" in html
    assert "You own 0 shares of SSOL." in html


@pytest.mark.parametrize("bad", ["", "0", "-5", "1.5", "1e3", "abc"])
def test_bad_quantity_is_rejected_and_kept_in_the_box(client, conn, bad):
    response = _review(client, conn, "buy", "SSOL", bad)
    html = response.get_data(as_text=True)
    assert response.status_code == 400
    assert "Please enter a whole number of shares greater than 0." in html
    assert f'value="{bad}"' in html  # what the user typed is kept
    assert db.count_rows(conn, "transactions") == 0


def test_quantity_over_the_maximum_is_rejected(client, conn):
    response = _review(client, conn, "buy", "SSOL", "1000001")
    assert response.status_code == 400
    assert "no more than 1,000,000" in response.get_data(as_text=True)


# --- CSRF ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "/trade/buy/review",
        "/trade/buy/confirm",
        "/trade/buy/cancel",
        "/trade/sell/review",
        "/trade/sell/confirm",
        "/trade/sell/cancel",
    ],
)
def test_post_without_csrf_token_is_rejected(client, conn, path):
    before = snapshot(conn)
    response = client.post(path, data={"stock_id": "1", "quantity": "1", "transaction_id": "1"})
    assert response.status_code == 400
    assert snapshot(conn) == before


def test_post_with_a_wrong_csrf_token_is_rejected(client, conn):
    response = client.post(
        "/trade/buy/review",
        data={"csrf_token": "not-a-real-token", "stock_id": "1", "quantity": "1"},
    )
    assert response.status_code == 400
