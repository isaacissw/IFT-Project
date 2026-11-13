"""Route tests for the Financial Records page and cash deposits / withdrawals.

These drive the real pages with Flask's test client (CSRF on), the way a person
would: open the page, submit the form, confirm.
"""

import re
from decimal import Decimal

import pytest

from app import db
from dbhelpers import CUSTOMER_PASSWORD, snapshot, stock_id, test_database_url, trainee_id

D = Decimal


def _token(html_or_response):
    html = html_or_response if isinstance(html_or_response, str) else html_or_response.get_data(as_text=True)
    return re.search(r'name="csrf_token" value="([^"]+)"', html).group(1)


def _tx_id(html):
    return re.search(r'name="transaction_id" value="(\d+)"', html).group(1)


@pytest.fixture
def conn(app):
    connection = db.get_connection(test_database_url())
    yield connection
    connection.close()


def _review(client, kind, amount):
    page = client.get("/financial-records")
    return client.post(f"/cash/{kind}/review", data={"csrf_token": _token(page), "amount": amount})


def _confirm(client, kind, review_response):
    html = review_response.get_data(as_text=True)
    return client.post(
        f"/cash/{kind}/confirm",
        data={"csrf_token": _token(html), "transaction_id": _tx_id(html)},
        follow_redirects=True,
    )


def _balance(conn):
    return db.get_user_by_id(conn, trainee_id(conn))["cash_balance"]


# --- the page ---------------------------------------------------------------------


def test_financial_records_page_shows_balance_forms_and_history(client):
    html = client.get("/financial-records").get_data(as_text=True)
    assert "$10,000.00" in html
    assert "Deposit Cash" in html and "Withdraw Cash" in html
    assert 'min="0.01"' in html and 'step="0.01"' in html
    assert "No transactions yet." in html


def test_navigation_links_are_shown_to_customers(client):
    html = client.get("/").get_data(as_text=True)
    assert 'href="/financial-records"' in html and "Financial Records" in html


def test_admins_and_logged_out_visitors_cannot_use_the_page(anon_client, admin_client):
    for visitor, expected in ((anon_client, "/login"), (admin_client, "/admin/")):
        response = visitor.get("/financial-records")
        assert response.status_code == 302 and response.headers["Location"] == expected


# --- deposit ---------------------------------------------------------------------------


def test_review_screen_for_a_deposit(client, conn):
    html = _review(client, "deposit", "250.50").get_data(as_text=True)
    assert "Confirm Your Deposit" in html
    assert "$250.50" in html and "$10,000.00" in html and "$10,250.50" in html
    assert "Once confirmed, this transaction cannot be cancelled." in html
    assert _balance(conn) == D("10000.00")  # nothing moved yet


def test_deposit_flow_updates_balance_and_history(client, conn):
    html = _confirm(client, "deposit", _review(client, "deposit", "250.50")).get_data(as_text=True)
    assert "Deposited $250.50. Your new cash balance is $10,250.50." in html
    assert "<dd>$10,250.50</dd>" in html
    assert "deposit" in html  # in the history table
    assert _balance(conn) == D("10250.50")


def test_deposited_money_can_be_used_to_trade(client, conn):
    """Zero out the account, deposit, then buy: the whole money path end to end."""
    conn.execute("UPDATE users SET cash_balance = 0 WHERE username = 'trainee1'")
    _confirm(client, "deposit", _review(client, "deposit", "500"))
    sid = stock_id(conn, "SSOL")
    page = client.get(f"/trade/{sid}")
    review = client.post("/trade/buy/review", data={"csrf_token": _token(page), "stock_id": sid, "quantity": "10"})
    assert "Confirm Your Purchase" in review.get_data(as_text=True)
    html = review.get_data(as_text=True)
    client.post("/trade/buy/confirm", data={"csrf_token": _token(html), "transaction_id": _tx_id(html)})
    assert _balance(conn) == D("75.00")  # 500 - 10 * 42.50


def test_cash_rows_show_dashes_not_none_on_the_dashboard(client, conn):
    _confirm(client, "deposit", _review(client, "deposit", "100"))
    html = client.get("/").get_data(as_text=True)
    assert "deposit" in html
    assert "None" not in html


# --- withdrawal -------------------------------------------------------------------------


def test_withdrawal_flow(client, conn):
    review = _review(client, "withdraw", "300")
    assert "Confirm Your Withdrawal" in review.get_data(as_text=True)
    html = _confirm(client, "withdraw", review).get_data(as_text=True)
    assert "Withdrew $300.00. Your new cash balance is $9,700.00." in html
    assert _balance(conn) == D("9700.00")


def test_withdrawing_too_much_shows_insufficient_funds(client, conn):
    before = snapshot(conn)
    response = _review(client, "withdraw", "10000.01")
    html = response.get_data(as_text=True)
    assert response.status_code == 400
    assert "Insufficient Funds" in html
    assert "This withdrawal of $10,000.01 exceeds your cash balance of $10,000.00." in html
    assert 'value="10000.01"' in html  # what the user typed is kept
    assert snapshot(conn) == before


# --- bad input -----------------------------------------------------------------------------


@pytest.mark.parametrize("kind", ["deposit", "withdraw"])
@pytest.mark.parametrize("bad", ["0", "-5", "1.234", "1e3", "abc", "1,000", "$5", "5; DROP TABLE users;--"])
def test_bad_amounts_are_rejected_and_nothing_is_written(client, conn, kind, bad):
    before = snapshot(conn)
    response = _review(client, kind, bad)
    assert response.status_code == 400
    assert "Please enter an amount greater than $0.00 with at most two decimal places." in response.get_data(as_text=True)
    assert snapshot(conn) == before


def test_blank_amount_is_rejected(client):
    assert _review(client, "deposit", "").status_code == 400


def test_deposit_over_the_limit_is_rejected(client):
    response = _review(client, "deposit", "1000000.01")
    assert response.status_code == 400
    assert "limited to $1,000,000.00" in response.get_data(as_text=True)


def test_unknown_cash_action_is_404(client):
    assert client.post("/cash/gift/review", data={"csrf_token": "x"}).status_code in (400, 404)
    assert client.get("/cash/deposit/review").status_code == 405  # POST only


# --- confirm / cancel behavior ----------------------------------------------------------------


def test_confirming_twice_applies_only_once(client, conn):
    html = _review(client, "deposit", "100").get_data(as_text=True)
    form = {"csrf_token": _token(html), "transaction_id": _tx_id(html)}
    client.post("/cash/deposit/confirm", data=form)
    second = client.post("/cash/deposit/confirm", data=form, follow_redirects=True)
    assert "This transaction has already been processed or does not exist." in second.get_data(as_text=True)
    assert _balance(conn) == D("10100.00")
    assert db.count_rows(conn, "transactions") == 1


def test_successful_confirm_redirects_so_refresh_cannot_repeat_it(client):
    html = _review(client, "deposit", "100").get_data(as_text=True)
    response = client.post(
        "/cash/deposit/confirm", data={"csrf_token": _token(html), "transaction_id": _tx_id(html)}
    )
    assert response.status_code == 302 and response.headers["Location"] == "/financial-records"


def test_cancel_discards_the_request(client, conn):
    html = _review(client, "withdraw", "100").get_data(as_text=True)
    response = client.post(
        "/cash/withdraw/cancel",
        data={"csrf_token": _token(html), "transaction_id": _tx_id(html)},
        follow_redirects=True,
    )
    assert "Withdrawal cancelled." in response.get_data(as_text=True)
    assert db.count_rows(conn, "transactions") == 0
    assert _balance(conn) == D("10000.00")


def test_a_withdrawal_confirmed_after_the_balance_dropped_is_refused(client, conn):
    review = _review(client, "withdraw", "6000")
    conn.execute("UPDATE users SET cash_balance = 1000 WHERE username = 'trainee1'")
    html = _confirm(client, "withdraw", review).get_data(as_text=True)
    assert "Insufficient Funds" in html
    assert _balance(conn) == D("1000.00")


def test_garbage_transaction_ids_are_harmless(client, conn):
    before = snapshot(conn)
    token = _token(client.get("/financial-records"))
    for kind in ("deposit", "withdraw"):
        for action in ("confirm", "cancel"):
            for bad in ("", "abc", "1 OR 1=1", "999999"):
                response = client.post(
                    f"/cash/{kind}/{action}", data={"csrf_token": token, "transaction_id": bad}, follow_redirects=True
                )
                assert response.status_code == 200
    assert snapshot(conn) == before


# --- security -------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [f"/cash/{kind}/{action}" for kind in ("deposit", "withdraw") for action in ("review", "confirm", "cancel")],
)
def test_posts_without_a_csrf_token_are_rejected(client, conn, path):
    before = snapshot(conn)
    assert client.post(path, data={"amount": "10", "transaction_id": "1"}).status_code == 400
    assert snapshot(conn) == before


@pytest.mark.parametrize("path", ["/cash/deposit/review", "/cash/withdraw/confirm"])
def test_logged_out_visitors_cannot_move_money(anon_client, conn, path):
    before = snapshot(conn)
    token = _token(anon_client.get("/login"))
    response = anon_client.post(path, data={"csrf_token": token, "amount": "10", "transaction_id": "1"})
    assert response.status_code == 302 and response.headers["Location"] == "/login"
    assert snapshot(conn) == before


def test_the_account_is_taken_from_the_session_not_the_form(client, conn):
    """A forged user_id in the form must be ignored: only the logged-in customer's money moves."""
    from dbhelpers import add_customer

    other = add_customer(conn)
    page = client.get("/financial-records")
    client.post("/cash/deposit/review", data={"csrf_token": _token(page), "amount": "50", "user_id": other})
    html = client.post(
        "/cash/deposit/review", data={"csrf_token": _token(page), "amount": "50", "user_id": other}
    ).get_data(as_text=True)
    client.post("/cash/deposit/confirm", data={"csrf_token": _token(html), "transaction_id": _tx_id(html), "user_id": other})
    assert db.get_user_by_id(conn, other)["cash_balance"] == D("10000.00")  # untouched
    assert _balance(conn) == D("10050.00")
