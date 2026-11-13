"""Service-level tests for the administrator rules: stocks, market hours and the override."""

import inspect
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from app import db
from app.services import admin, market
from app.services.admin import (
    FormError,
    create_stock,
    set_market_override,
    update_market_schedule,
    update_stock,
    validate_new_stock,
)
from app.services.trading import ValidationError, confirm_buy, create_pending_buy
from dbhelpers import add_customer, snapshot, stock_id, trainee_id

NOW = datetime(2026, 3, 2, 15, 0, 0, tzinfo=timezone.utc)  # Monday 10:00 AM New York
D = Decimal


GOOD = dict(company_name="Acme Rockets", ticker="acme", shares="5000", price="19.99", description="Rockets.")


def _admin_id(conn):
    return db.get_user_by_username(conn, "admin")["user_id"]


def _create(conn, **overrides):
    values = {**GOOD, **overrides}
    return create_stock(
        conn, _admin_id(conn), values["company_name"], values["ticker"], values["shares"],
        values["price"], values["description"], NOW,
    )


# ============================== create stock ===============================


def test_create_stock_saves_everything_the_form_gave(db_conn):
    new_id = _create(db_conn)
    stock = db.get_stock(db_conn, new_id)
    assert stock["ticker_symbol"] == "ACME"  # upper-cased
    assert stock["company_name"] == "Acme Rockets"
    assert stock["company_shares_outstanding"] == 5000
    assert stock["current_price"] == D("19.99")
    assert stock["description"] == "Rockets."
    assert stock["logo_filename"] is None  # logos are out of scope
    assert stock["created_by"] == _admin_id(db_conn)
    assert stock["created_at"] == stock["price_updated_at"] == "2026-03-02T15:00:00+00:00"


def test_description_is_optional(db_conn):
    stock = db.get_stock(db_conn, _create(db_conn, description="   "))
    assert stock["description"] is None


def test_values_are_trimmed(db_conn):
    stock = db.get_stock(db_conn, _create(db_conn, company_name="  Acme Rockets ", ticker=" acme ", shares=" 10 ", price=" 5 "))
    assert (stock["company_name"], stock["ticker_symbol"], stock["company_shares_outstanding"]) == ("Acme Rockets", "ACME", 10)
    assert stock["current_price"] == D("5.00")


def test_a_new_stock_can_be_traded_right_away(db_conn):
    new_id = _create(db_conn, shares="100", price="10.00")
    user = trainee_id(db_conn)
    tx = create_pending_buy(db_conn, user, new_id, 5, NOW)
    confirm_buy(db_conn, user, tx, NOW)
    assert db.get_holding(db_conn, user, new_id)["quantity"] == 5
    assert db.get_user_by_id(db_conn, user)["cash_balance"] == D("9950.00")


def test_duplicate_ticker_is_rejected_in_any_case(db_conn):
    before = snapshot(db_conn)
    with pytest.raises(FormError) as err:
        _create(db_conn, ticker="ssol")
    assert err.value.errors == {"ticker": "A stock with that ticker already exists."}
    assert snapshot(db_conn) == before


@pytest.mark.parametrize(
    "field, value, error_key",
    [
        ("company_name", "", "company_name"),
        ("company_name", "   ", "company_name"),
        ("company_name", "x" * 256, "company_name"),
        ("ticker", "", "ticker"),
        ("ticker", "TOOLONG", "ticker"),
        ("ticker", "AB1", "ticker"),
        ("ticker", "A B", "ticker"),
        ("ticker", "A;", "ticker"),
        ("shares", "", "shares"),
        ("shares", "0", "shares"),
        ("shares", "-5", "shares"),
        ("shares", "1.5", "shares"),
        ("shares", "1e3", "shares"),
        ("shares", "abc", "shares"),
        ("shares", "1000000001", "shares"),
        ("price", "", "price"),
        ("price", "0", "price"),
        ("price", "0.00", "price"),
        ("price", "-1", "price"),
        ("price", "1.234", "price"),
        ("price", "1e3", "price"),
        ("price", "$5", "price"),
        ("price", "1,000", "price"),
        ("price", "1000000.01", "price"),
        ("description", "x" * 2001, "description"),
    ],
)
def test_invalid_stock_fields_are_rejected_and_nothing_is_written(db_conn, field, value, error_key):
    before = snapshot(db_conn)
    with pytest.raises(FormError) as err:
        _create(db_conn, **{field: value})
    assert error_key in err.value.errors
    assert snapshot(db_conn) == before


def test_all_problems_are_reported_together():
    _clean, errors = validate_new_stock("", "", "", "", "")
    assert set(errors) == {"company_name", "ticker", "shares", "price"}


def test_edge_values_that_are_allowed(db_conn):
    _create(db_conn, ticker="ONE", shares="1", price="0.01")
    _create(db_conn, ticker="MAXX", shares="1000000000", price="1000000.00")
    assert db.get_stock_by_ticker(db_conn, "MAXX")["current_price"] == D("1000000.00")


# ============================== edit stock ===============================


def test_edit_changes_name_description_and_shares(db_conn):
    ssol = stock_id(db_conn, "SSOL")
    update_stock(db_conn, ssol, "Sonoran Solar Group", "Sun power.", "2000000")
    stock = db.get_stock(db_conn, ssol)
    assert stock["company_name"] == "Sonoran Solar Group"
    assert stock["description"] == "Sun power."
    assert stock["company_shares_outstanding"] == 2_000_000


def test_edit_never_changes_the_price_or_ticker(db_conn):
    ssol = stock_id(db_conn, "SSOL")
    before = db.get_stock(db_conn, ssol)
    update_stock(db_conn, ssol, "Renamed Corp", None, "1000000")
    after = db.get_stock(db_conn, ssol)
    assert after["current_price"] == before["current_price"] == D("42.50")
    assert after["price_updated_at"] == before["price_updated_at"]
    assert after["ticker_symbol"] == "SSOL"


def test_the_edit_functions_have_no_way_to_set_a_price():
    """The rule is built into the code's shape: no price parameter exists to be misused."""
    for func in (update_stock, db.update_stock_details):
        assert not any("price" in name for name in inspect.signature(func).parameters), func.__name__


def test_shares_cannot_drop_below_what_customers_own(db_conn):
    user, ssol = trainee_id(db_conn), stock_id(db_conn, "SSOL")
    tx = create_pending_buy(db_conn, user, ssol, 100, NOW)
    confirm_buy(db_conn, user, tx, NOW)
    before = snapshot(db_conn)
    with pytest.raises(FormError) as err:
        update_stock(db_conn, ssol, "Sonoran Solar Corp", None, "99")
    assert "100 shares" in err.value.errors["shares"]
    assert snapshot(db_conn) == before
    update_stock(db_conn, ssol, "Sonoran Solar Corp", None, "100")  # exactly what is owned is fine
    assert db.get_stock(db_conn, ssol)["company_shares_outstanding"] == 100


def test_edit_validation_and_unknown_stock(db_conn):
    ssol = stock_id(db_conn, "SSOL")
    before = snapshot(db_conn)
    with pytest.raises(FormError) as err:
        update_stock(db_conn, ssol, "", None, "0")
    assert set(err.value.errors) == {"company_name", "shares"}
    with pytest.raises(ValidationError, match="Stock not found."):
        update_stock(db_conn, 999_999, "X", None, "10")
    assert snapshot(db_conn) == before


# ============================== market hours ===============================


def _week(open_="09:00", close="17:00", closed=()):
    return {d: {"closed": d in closed, "open": open_, "close": close} for d in admin.WEEKDAYS}


def _is_open(conn, when_utc):
    return market.is_market_open(conn, when_utc, "America/New_York")


def test_schedule_update_applies_to_the_market_check(db_conn):
    update_market_schedule(db_conn, _week(closed=("Saturday", "Sunday")), NOW)
    rows = {r["day_of_week"]: r for r in db.list_market_schedule(db_conn)}
    assert rows["Monday"]["market_open_time"] == "09:00:00" and rows["Monday"]["market_close_time"] == "17:00:00"
    assert rows["Saturday"]["is_holiday"] and not rows["Monday"]["is_holiday"]
    assert rows["Monday"]["updated_at"] == "2026-03-02T15:00:00+00:00"

    assert _is_open(db_conn, datetime(2026, 3, 2, 14, 0, tzinfo=timezone.utc))        # Mon 9:00 AM ET
    assert not _is_open(db_conn, datetime(2026, 3, 2, 13, 59, tzinfo=timezone.utc))   # Mon 8:59 AM ET
    assert not _is_open(db_conn, datetime(2026, 3, 2, 22, 0, tzinfo=timezone.utc))    # Mon 5:00 PM ET (closing instant)
    assert not _is_open(db_conn, datetime(2026, 3, 7, 17, 0, tzinfo=timezone.utc))    # Saturday


def test_the_default_demo_schedule_is_listed_monday_first(db_conn):
    assert [r["day_of_week"] for r in db.list_market_schedule(db_conn)] == list(admin.WEEKDAYS)


def test_a_closed_day_keeps_its_stored_hours_and_can_reopen(db_conn):
    update_market_schedule(db_conn, _week(), NOW)  # known hours first: every day 09:00-17:00
    update_market_schedule(db_conn, _week(open_="10:30", close="15:45", closed=("Wednesday",)), NOW)
    wed = {r["day_of_week"]: r for r in db.list_market_schedule(db_conn)}["Wednesday"]
    assert wed["is_holiday"] and wed["market_open_time"] == "09:00:00"  # untouched while closed
    update_market_schedule(db_conn, _week(open_="10:30", close="15:45"), NOW)
    wed = {r["day_of_week"]: r for r in db.list_market_schedule(db_conn)}["Wednesday"]
    assert not wed["is_holiday"] and wed["market_open_time"] == "10:30:00" and wed["market_close_time"] == "15:45:00"


def test_times_with_seconds_are_accepted(db_conn):
    update_market_schedule(db_conn, _week(open_="09:00:00", close="16:59:59"), NOW)
    assert db.get_schedule_for_day(db_conn, "Tuesday")["market_close_time"] == "16:59:59"


@pytest.mark.parametrize(
    "open_, close",
    [("17:00", "09:00"), ("09:00", "09:00"), ("", "17:00"), ("09:00", ""), ("9am", "5pm"), ("25:00", "26:00"),
     ("09:60", "17:00"), ("09:00", "24:00"), ("nine", "five"), ("09:00; DROP TABLE users", "17:00")],
)
def test_bad_hours_are_rejected_and_nothing_changes(db_conn, open_, close):
    before = snapshot(db_conn)
    with pytest.raises(FormError) as err:
        update_market_schedule(db_conn, _week(open_=open_, close=close), NOW)
    assert set(err.value.errors) == set(admin.WEEKDAYS)  # every open day has the problem
    assert snapshot(db_conn) == before


def test_one_bad_day_means_no_day_is_saved(db_conn):
    week = _week()
    week["Thursday"] = {"closed": False, "open": "18:00", "close": "08:00"}
    before = snapshot(db_conn)
    with pytest.raises(FormError) as err:
        update_market_schedule(db_conn, week, NOW)
    assert list(err.value.errors) == ["Thursday"]
    assert snapshot(db_conn) == before


def test_closed_days_do_not_need_valid_times(db_conn):
    week = _week()
    week["Sunday"] = {"closed": True, "open": "", "close": ""}
    update_market_schedule(db_conn, week, NOW)
    assert db.get_schedule_for_day(db_conn, "Sunday")["is_holiday"]


def test_missing_day_is_an_error(db_conn):
    week = _week()
    del week["Friday"]
    with pytest.raises(FormError):
        update_market_schedule(db_conn, week, NOW)


# ============================== override ===============================


@pytest.mark.parametrize("mode", ["force_open", "force_closed", "none"])
def test_override_is_saved_with_who_and_when(db_conn, mode):
    admin_id = _admin_id(db_conn)
    set_market_override(db_conn, admin_id, mode, NOW)
    settings = db.get_market_settings(db_conn)
    assert settings["override_mode"] == mode
    assert settings["updated_by"] == admin_id
    assert settings["updated_at"] == "2026-03-02T15:00:00+00:00"


def test_forced_open_beats_a_closed_schedule_and_forced_closed_beats_an_open_one(db_conn):
    saturday = datetime(2026, 3, 7, 17, 0, tzinfo=timezone.utc)
    update_market_schedule(db_conn, _week(closed=("Saturday", "Sunday")), NOW)
    assert not _is_open(db_conn, saturday)
    set_market_override(db_conn, _admin_id(db_conn), "force_open", NOW)
    assert _is_open(db_conn, saturday)
    set_market_override(db_conn, _admin_id(db_conn), "force_closed", NOW)
    assert not _is_open(db_conn, datetime(2026, 3, 2, 15, 0, tzinfo=timezone.utc))
    set_market_override(db_conn, _admin_id(db_conn), "none", NOW)
    assert _is_open(db_conn, datetime(2026, 3, 2, 15, 0, tzinfo=timezone.utc))


@pytest.mark.parametrize("bad", ["", "open", "FORCE_OPEN", "none'; DROP TABLE users;--", None])
def test_unknown_override_values_are_rejected(db_conn, bad):
    before = snapshot(db_conn)
    with pytest.raises(ValidationError):
        set_market_override(db_conn, _admin_id(db_conn), bad, NOW)
    assert snapshot(db_conn) == before
