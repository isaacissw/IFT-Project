"""Signup, login, logout and access control.

The first half tests the service (app/services/auth.py) directly against the
MySQL test database. The second half drives the real pages with the Flask test
client, with CSRF protection switched on.
"""

import re
from datetime import datetime, timezone
from decimal import Decimal

import pytest
from werkzeug.security import check_password_hash

from app import db
from app.services.auth import (
    GENERIC_LOGIN_ERROR,
    LoginError,
    SignupError,
    authenticate,
    register_customer,
    validate_signup,
)
from dbhelpers import ADMIN_PASSWORD, CUSTOMER_PASSWORD, snapshot, stock_id, test_database_url

NOW = datetime(2026, 3, 2, 15, 0, 0, tzinfo=timezone.utc)

GOOD = dict(
    first_name="Pat",
    last_name="Broker",
    email="Pat.Broker@Example.com",
    username="pat_broker",
    password="Sup3rSecret",
    confirm_password="Sup3rSecret",
)


def _register(conn, **overrides):
    return register_customer(conn, now=NOW, **{**GOOD, **overrides})


# =============================== service: signup ===============================


def test_signup_creates_a_customer_with_no_cash(db_conn):
    user_id = _register(db_conn)
    user = db.get_user_by_id(db_conn, user_id)
    assert user["user_type"] == "customer"
    assert user["cash_balance"] == Decimal("0.00")
    assert user["is_active"]
    assert user["username"] == "pat_broker"
    assert user["email"] == "pat.broker@example.com"  # stored in lowercase
    assert user["created_at"] == "2026-03-02T15:00:00+00:00"  # account-creation timestamp
    assert user["updated_at"] == "2026-03-02T15:00:00+00:00"


def test_password_is_stored_only_as_a_hash(db_conn):
    user = db.get_user_by_id(db_conn, _register(db_conn))
    assert user["password_hash"] != GOOD["password"]
    assert GOOD["password"] not in user["password_hash"]
    assert check_password_hash(user["password_hash"], GOOD["password"])
    assert not check_password_hash(user["password_hash"], "wrong-Password1")


def test_two_people_with_the_same_password_get_different_hashes(db_conn):
    first = db.get_user_by_id(db_conn, _register(db_conn))
    second = db.get_user_by_id(db_conn, _register(db_conn, username="other_one", email="o@example.com"))
    assert first["password_hash"] != second["password_hash"]  # salted


def test_signup_rejects_a_taken_username_even_in_other_case(db_conn):
    with pytest.raises(SignupError) as err:
        _register(db_conn, username="TRAINEE1", email="new@example.com")
    assert err.value.errors == {"username": "That username is already taken."}


def test_signup_rejects_a_taken_email(db_conn):
    with pytest.raises(SignupError) as err:
        _register(db_conn, email="trainee1@example.com")
    assert err.value.errors == {"email": "An account with that email already exists."}


def test_failed_signup_writes_nothing(db_conn):
    before = snapshot(db_conn)
    with pytest.raises(SignupError):
        _register(db_conn, password="short")
    with pytest.raises(SignupError):
        _register(db_conn, username="trainee1")
    assert snapshot(db_conn) == before


@pytest.mark.parametrize(
    "field, value, expected_error_field",
    [
        ("first_name", "", "first_name"),
        ("first_name", "   ", "first_name"),
        ("first_name", "x" * 101, "first_name"),
        ("last_name", "", "last_name"),
        ("email", "", "email"),
        ("email", "not-an-email", "email"),
        ("email", "two@@example.com", "email"),
        ("email", "no spaces@example.com", "email"),
        ("username", "ab", "username"),
        ("username", "x" * 31, "username"),
        ("username", "has space", "username"),
        ("username", "semi;colon", "username"),
        ("username", "quote'", "username"),
        ("password", "short1", "password"),
        ("password", "alllettersnodigits", "password"),
        ("password", "12345678901", "password"),
        ("password", "a1" * 65, "password"),  # 130 characters: too long
    ],
)
def test_signup_validation_rules(field, value, expected_error_field):
    overrides = {field: value}
    if field == "password":
        overrides["confirm_password"] = value
    _clean, errors = validate_signup(**{**GOOD, **overrides})
    assert expected_error_field in errors


def test_signup_password_must_not_equal_username():
    _clean, errors = validate_signup(**{**GOOD, "username": "Passw0rd99", "password": "passw0rd99", "confirm_password": "passw0rd99"})
    assert "password" in errors


def test_signup_passwords_must_match():
    _clean, errors = validate_signup(**{**GOOD, "confirm_password": "Different1"})
    assert errors == {"confirm_password": "The two passwords do not match."}


def test_valid_signup_has_no_errors_and_values_are_trimmed():
    clean, errors = validate_signup(**{**GOOD, "first_name": "  Pat ", "email": " PAT@Example.com "})
    assert errors == {}
    assert clean["first_name"] == "Pat" and clean["email"] == "pat@example.com"


# ============================== service: authenticate ==========================


def test_authenticate_accepts_the_right_password(db_conn):
    user = authenticate(db_conn, "trainee1", CUSTOMER_PASSWORD)
    assert user["username"] == "trainee1" and user["user_type"] == "customer"
    assert authenticate(db_conn, "admin", ADMIN_PASSWORD)["user_type"] == "admin"


def test_authenticate_trims_the_username(db_conn):
    assert authenticate(db_conn, "  trainee1 ", CUSTOMER_PASSWORD)["username"] == "trainee1"


def test_wrong_password_and_unknown_user_give_the_same_message(db_conn):
    with pytest.raises(LoginError) as wrong_password:
        authenticate(db_conn, "trainee1", "not-the-password")
    with pytest.raises(LoginError) as unknown_user:
        authenticate(db_conn, "nobody_here", "not-the-password")
    assert str(wrong_password.value) == str(unknown_user.value) == GENERIC_LOGIN_ERROR


@pytest.mark.parametrize("username, password", [("", ""), ("trainee1", ""), ("", CUSTOMER_PASSWORD)])
def test_blank_credentials_are_rejected(db_conn, username, password):
    with pytest.raises(LoginError):
        authenticate(db_conn, username, password)


def test_a_deactivated_account_cannot_log_in(db_conn):
    db_conn.execute("UPDATE users SET is_active = 0 WHERE username = 'trainee1'")
    with pytest.raises(LoginError):
        authenticate(db_conn, "trainee1", CUSTOMER_PASSWORD)


@pytest.mark.parametrize("hostile", ["' OR '1'='1", "trainee1' --", "x'; DROP TABLE users; --"])
def test_sql_injection_in_login_fields_does_not_work(db_conn, hostile):
    with pytest.raises(LoginError):
        authenticate(db_conn, hostile, hostile)
    assert db.count_rows(db_conn, "users") == 2


def test_new_user_can_log_in_with_the_password_they_chose(db_conn):
    _register(db_conn)
    assert authenticate(db_conn, "pat_broker", GOOD["password"])["email"] == "pat.broker@example.com"


# ================================ web pages ====================================


def _token(response):
    return re.search(r'name="csrf_token" value="([^"]+)"', response.get_data(as_text=True)).group(1)


def _log_in(anon_client, username, password):
    page = anon_client.get("/login")
    return anon_client.post(
        "/login", data={"csrf_token": _token(page), "username": username, "password": password}
    )


def test_login_and_signup_pages_load(anon_client):
    login = anon_client.get("/login")
    signup = anon_client.get("/signup")
    assert login.status_code == 200 and 'type="password"' in login.get_data(as_text=True)
    assert signup.status_code == 200 and 'name="confirm_password"' in signup.get_data(as_text=True)


def test_customer_login_goes_to_the_trading_dashboard(anon_client):
    response = _log_in(anon_client, "trainee1", CUSTOMER_PASSWORD)
    assert response.status_code == 302 and response.headers["Location"] == "/"
    page = anon_client.get("/").get_data(as_text=True)
    assert "Available Stocks" in page and "$10,000.00" in page and "trainee1" in page


def test_administrator_login_goes_to_the_admin_dashboard(anon_client):
    response = _log_in(anon_client, "admin", ADMIN_PASSWORD)
    assert response.status_code == 302 and response.headers["Location"] == "/admin/"
    page = anon_client.get("/admin/").get_data(as_text=True)
    assert "Administrator Dashboard" in page
    assert "Cash Balance" not in page  # the admin has no trading account to show


def test_failed_login_shows_a_generic_message_and_keeps_only_the_username(anon_client):
    response = _log_in(anon_client, "trainee1", "wrong-password-1")
    html = response.get_data(as_text=True)
    assert response.status_code == 401
    assert "Invalid username or password." in html
    assert 'value="trainee1"' in html
    assert "wrong-password-1" not in html
    assert anon_client.get("/").status_code == 302  # still not logged in


def test_unknown_user_gets_the_same_page_as_a_wrong_password(anon_client):
    wrong_password = _log_in(anon_client, "trainee1", "wrong-password-1")
    unknown_user = _log_in(anon_client, "ghost_user", "wrong-password-1")
    assert wrong_password.status_code == unknown_user.status_code == 401
    assert "Invalid username or password." in unknown_user.get_data(as_text=True)


def test_login_without_csrf_token_is_rejected(anon_client):
    response = anon_client.post("/login", data={"username": "trainee1", "password": CUSTOMER_PASSWORD})
    assert response.status_code == 400


def test_session_cookie_is_httponly_and_samesite_lax(anon_client):
    cookie = _log_in(anon_client, "trainee1", CUSTOMER_PASSWORD).headers.get_all("Set-Cookie")
    session_cookie = next(c for c in cookie if c.startswith("session="))
    assert "HttpOnly" in session_cookie
    assert "SameSite=Lax" in session_cookie


def test_the_session_cookie_holds_no_password_or_role(anon_client):
    cookie = _log_in(anon_client, "trainee1", CUSTOMER_PASSWORD).headers.get_all("Set-Cookie")
    assert CUSTOMER_PASSWORD not in "".join(cookie)


def test_logged_in_users_are_sent_away_from_the_login_page(client, admin_client):
    assert client.get("/login").headers["Location"] == "/"
    assert admin_client.get("/signup").headers["Location"] == "/admin/"


# ---- access control ----------------------------------------------------------


@pytest.mark.parametrize("path", ["/", "/trade/1", "/trade/1?mode=sell", "/admin/"])
def test_pages_redirect_to_login_when_logged_out(anon_client, path):
    response = anon_client.get(path)
    assert response.status_code == 302 and response.headers["Location"] == "/login"


def test_login_required_message_is_shown(anon_client):
    page = anon_client.get("/", follow_redirects=True).get_data(as_text=True)
    assert "Please log in to continue." in page


@pytest.mark.parametrize(
    "path", ["/trade/buy/review", "/trade/buy/confirm", "/trade/sell/review", "/trade/sell/confirm", "/trade/buy/cancel"]
)
def test_trading_posts_do_nothing_when_logged_out(anon_client, path):
    """Even with a valid CSRF token, a logged-out visitor cannot trade."""
    conn = db.get_connection(test_database_url())
    try:
        before = snapshot(conn)
        token = _token(anon_client.get("/login"))
        response = anon_client.post(
            path, data={"csrf_token": token, "stock_id": stock_id(conn, "SSOL"), "quantity": "1", "transaction_id": "1"}
        )
        assert response.status_code == 302 and response.headers["Location"] == "/login"
        assert snapshot(conn) == before
    finally:
        conn.close()


def test_customers_cannot_open_the_admin_area(client):
    response = client.get("/admin/")
    assert response.status_code == 302 and response.headers["Location"] == "/"


def test_administrators_cannot_trade(admin_client):
    for path in ("/", "/trade/1"):
        response = admin_client.get(path)
        assert response.status_code == 302 and response.headers["Location"] == "/admin/"


def test_a_deactivated_account_is_logged_out_on_its_next_request(client):
    conn = db.get_connection(test_database_url())
    try:
        conn.execute("UPDATE users SET is_active = 0 WHERE username = 'trainee1'")
    finally:
        conn.close()
    response = client.get("/")
    assert response.status_code == 302 and response.headers["Location"] == "/login"


def test_a_forged_user_id_in_the_session_of_a_missing_user_is_ignored(anon_client):
    with anon_client.session_transaction() as session:
        session["user_id"] = 999_999
    assert anon_client.get("/").headers["Location"] == "/login"


# ---- logout --------------------------------------------------------------------


def test_logout_ends_the_session(client):
    token = _token(client.get("/"))
    response = client.post("/logout", data={"csrf_token": token}, follow_redirects=True)
    assert "You have been logged out." in response.get_data(as_text=True)
    assert client.get("/").headers["Location"] == "/login"


def test_logout_needs_a_csrf_token_and_a_post(client):
    assert client.post("/logout").status_code == 400
    assert client.get("/logout").status_code == 405
    assert client.get("/").status_code == 200  # still logged in


# ---- signup page ---------------------------------------------------------------


def test_signup_logs_the_new_customer_in(anon_client):
    token = _token(anon_client.get("/signup"))
    response = anon_client.post("/signup", data={"csrf_token": token, **GOOD})
    assert response.status_code == 302 and response.headers["Location"] == "/"

    page = anon_client.get("/").get_data(as_text=True)
    assert "Welcome! Your account has been created." in page
    assert "pat_broker" in page
    assert "Cash Balance: <strong>$0.00</strong>" in page

    conn = db.get_connection(test_database_url())
    try:
        user = db.get_user_by_username(conn, "pat_broker")
        assert user["user_type"] == "customer" and user["created_at"]
    finally:
        conn.close()


def test_a_new_customer_with_no_cash_cannot_buy(anon_client):
    token = _token(anon_client.get("/signup"))
    anon_client.post("/signup", data={"csrf_token": token, **GOOD})
    conn = db.get_connection(test_database_url())
    try:
        sid = stock_id(conn, "SAGU")
    finally:
        conn.close()
    page = anon_client.get(f"/trade/{sid}")
    response = anon_client.post(
        "/trade/buy/review", data={"csrf_token": _token(page), "stock_id": sid, "quantity": "1"}
    )
    assert response.status_code == 400
    assert "Insufficient Funds" in response.get_data(as_text=True)


def test_signup_errors_are_shown_and_passwords_are_not_echoed(anon_client):
    token = _token(anon_client.get("/signup"))
    response = anon_client.post(
        "/signup",
        data={"csrf_token": token, **GOOD, "username": "trainee1", "password": "Sup3rSecret", "confirm_password": "Nope1234"},
    )
    html = response.get_data(as_text=True)
    assert response.status_code == 400
    assert "That username is already taken." in html
    assert "Pat" in html and "pat_broker" not in html  # the other typed values are kept
    assert "Sup3rSecret" not in html and "Nope1234" not in html


def test_signup_with_a_duplicate_email_changes_nothing(anon_client):
    conn = db.get_connection(test_database_url())
    try:
        before = snapshot(conn)
        token = _token(anon_client.get("/signup"))
        response = anon_client.post("/signup", data={"csrf_token": token, **GOOD, "email": "admin@example.com"})
        assert response.status_code == 400
        assert "An account with that email already exists." in response.get_data(as_text=True)
        assert snapshot(conn) == before
    finally:
        conn.close()


def test_signup_cannot_create_an_administrator(anon_client):
    token = _token(anon_client.get("/signup"))
    anon_client.post("/signup", data={"csrf_token": token, **GOOD, "user_type": "admin"})
    conn = db.get_connection(test_database_url())
    try:
        assert db.get_user_by_username(conn, "pat_broker")["user_type"] == "customer"
    finally:
        conn.close()


def test_signup_without_csrf_token_is_rejected(anon_client):
    assert anon_client.post("/signup", data=GOOD).status_code == 400
