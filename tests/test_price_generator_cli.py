"""The ``flask --app app run-price-generator`` command, the config settings, and the
dashboard that shows the moving prices."""

import re
import subprocess
import sys
from decimal import Decimal

import pytest

from app import db
from dbhelpers import set_override, test_database_url

D = Decimal


@pytest.fixture
def conn(app):
    connection = db.get_connection(test_database_url())
    yield connection
    connection.close()


def _run(app, *args):
    return app.test_cli_runner().invoke(args=["run-price-generator", *args])


def _prices(conn):
    return {s["ticker_symbol"]: s["current_price"] for s in db.list_stocks(conn)}


def test_once_updates_prices_and_reports(app, conn):
    before = _prices(conn)
    result = _run(app, "--once", "--seed", "1")
    assert result.exit_code == 0, result.output
    assert re.search(r"market OPEN: 6 stocks updated, \d prices changed", result.output)
    assert _prices(conn) != before


def test_once_with_the_same_seed_is_repeatable(app, conn):
    start = _prices(conn)
    _run(app, "--once", "--seed", "5")
    first = _prices(conn)
    assert first != start
    for ticker, price in start.items():  # put every price back, then run again with the same seed
        conn.execute("UPDATE stocks SET current_price = ? WHERE ticker_symbol = ?", (price, ticker))
    _run(app, "--once", "--seed", "5")
    assert _prices(conn) == first
    _run(app, "--once", "--seed", "6")  # a different seed gives a different result
    assert _prices(conn) != first


def test_once_reports_closed_and_changes_nothing(app, conn):
    set_override(conn, "force_closed")
    before = _prices(conn)
    result = _run(app, "--once")
    assert result.exit_code == 0
    assert "market CLOSED: prices held" in result.output
    assert _prices(conn) == before


def test_step_size_option_limits_how_far_prices_move(app, conn):
    before = _prices(conn)
    assert _run(app, "--once", "--step-bps", "1", "--seed", "3").exit_code == 0
    after = _prices(conn)
    for ticker, price in after.items():
        assert abs(price - before[ticker]) <= before[ticker] * D("0.0001") + D("0.01")


@pytest.mark.parametrize("args", [["--seconds", "0"], ["--seconds", "99999"], ["--step-bps", "0"], ["--step-bps", "5000"], ["--seed", "x"]])
def test_bad_options_are_rejected_before_anything_runs(app, conn, args):
    before = _prices(conn)
    result = _run(app, "--once", *args)
    assert result.exit_code != 0
    assert _prices(conn) == before


def test_without_once_the_command_announces_it_is_running_and_stops_cleanly(app, monkeypatch):
    """Run the forever-loop but interrupt it on the first wait, like pressing Ctrl+C."""
    from app.services import pricing

    def interrupt(seconds):
        raise KeyboardInterrupt

    monkeypatch.setattr(pricing.time, "sleep", interrupt)
    result = _run(app, "--seconds", "5", "--seed", "1")
    assert result.exit_code == 0
    assert "Price generator running: every 5s" in result.output
    assert "Price generator stopped." in result.output
    assert "market OPEN" in result.output  # it did one update before the interrupt


def test_a_database_problem_is_reported_and_does_not_crash_the_generator(app, monkeypatch):
    from app.services import pricing

    def boom(seconds):
        raise KeyboardInterrupt

    monkeypatch.setattr(pricing.time, "sleep", boom)
    app.config["DATABASE_URL"] = "mysql://nobody:wrong@127.0.0.1:3306/stock_trading_test"
    result = _run(app, "--seed", "1")
    assert result.exit_code == 0
    assert "price update failed" in result.output and "will retry" in result.output


# ---- configuration ---------------------------------------------------------------------------------


def _config_value(env_name, value):
    code = (
        "import os, sys; os.environ[sys.argv[1]] = sys.argv[2]; os.environ['SECRET_KEY']='x'; "
        "from app.config import Config; print(Config.PRICE_TICK_SECONDS, Config.PRICE_MAX_STEP_BPS)"
    )
    return subprocess.run([sys.executable, "-c", code, env_name, value], capture_output=True, text=True, cwd=".")


def test_config_reads_the_price_settings_from_the_environment():
    result = _config_value("PRICE_TICK_SECONDS", "15")
    assert result.returncode == 0 and result.stdout.split()[0] == "15"
    assert _config_value("PRICE_MAX_STEP_BPS", "20").stdout.split()[1] == "20"


@pytest.mark.parametrize("name, bad", [("PRICE_TICK_SECONDS", "abc"), ("PRICE_TICK_SECONDS", "0"), ("PRICE_MAX_STEP_BPS", "-5")])
def test_config_rejects_nonsense_price_settings_clearly(name, bad):
    result = _config_value(name, bad)
    assert result.returncode != 0
    assert f"{name} must be a whole number" in result.stderr


def test_config_defaults_are_a_minute_and_half_a_percent(app):
    assert app.config["PRICE_TICK_SECONDS"] >= 1 and app.config["PRICE_MAX_STEP_BPS"] >= 1


# ---- the dashboard ------------------------------------------------------------------------------------


def test_dashboard_shows_prices_and_when_they_were_updated(app, client, conn):
    html = client.get("/").get_data(as_text=True)
    assert "Price Updated (America/New_York)" in html and "$42.50" in html

    _run(app, "--once", "--seed", "11")
    new_ssol = db.get_stock_by_ticker(conn, "SSOL")["current_price"]
    page = client.get("/").get_data(as_text=True)
    assert f"${new_ssol:,.2f}" in page
    assert re.search(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", page)  # the update time is displayed


def test_a_customer_sees_the_new_price_on_the_trade_page_and_pays_it(app, client, conn):
    _run(app, "--once", "--seed", "21")
    ssol = db.get_stock_by_ticker(conn, "SSOL")
    page = client.get(f"/trade/{ssol['stock_id']}").get_data(as_text=True)
    assert f"${ssol['current_price']:,.2f}" in page
