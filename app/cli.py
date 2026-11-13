"""Command-line tools (run with ``flask --app app <command>``).

``run-price-generator`` starts the SAD's "Random Stock Price Generator", the
background process that keeps stock prices moving while the market is open.
The logic itself lives in ``app/services/pricing.py``; this file only reads the
options you type, then starts that logic and prints a line after each round.

Run it in its OWN terminal, next to the website (``flask --app app run``):

    flask --app app run-price-generator                 keeps running; Ctrl+C stops it
    flask --app app run-price-generator --seconds 5     update every 5 s (demos)
    flask --app app run-price-generator --once          one update, then exit
    flask --app app run-price-generator --seed 42       repeatable "random" moves

What you will see (one line per round):

    Price generator running: every 60s, up to 0.5% per move. Ctrl+C to stop.
    [2026-11-12 10:00:00] market OPEN: 6 stocks updated, 6 prices changed
    [2026-11-12 10:01:00] market OPEN: 6 stocks updated, 5 prices changed   <- one stock rolled 0
    [2026-11-12 17:00:00] market CLOSED: prices held                         <- nothing changed
"""

import random
from datetime import datetime
from zoneinfo import ZoneInfo

import click
from flask import current_app
from flask.cli import with_appcontext

from app import db
from app.services import pricing


def _stamp(tz_name):
    """The current time in the market's time zone, used at the start of each printed line."""
    return datetime.now(ZoneInfo(tz_name)).strftime("%Y-%m-%d %H:%M:%S")


# The @click decorators below turn this function into a terminal command and
# describe its options. Click checks the options BEFORE the function runs, so
# e.g. "--seconds 0" is refused with an error and nothing happens.
@click.command("run-price-generator")
@click.option("--once", is_flag=True, help="Do a single price update and exit.")
@click.option(
    "--seconds",
    type=click.IntRange(1, 3600),
    default=None,
    help="Seconds between updates (default: PRICE_TICK_SECONDS from .env, normally 60).",
)
@click.option(
    "--step-bps",
    type=click.IntRange(1, pricing.MAX_ALLOWED_STEP_BPS),
    default=None,
    help="Largest move per update in basis points, 50 = 0.5% (default: PRICE_MAX_STEP_BPS, normally 50).",
)
@click.option("--seed", type=int, default=None, help="Make the random moves repeatable (for demos and tests).")
@with_appcontext  # gives us the app's settings (current_app.config) from .env
def run_price_generator_command(once, seconds, step_bps, seed):
    """Move stock prices randomly and gradually while the market is open."""
    config = current_app.config
    tz_name = config["MARKET_TIMEZONE"]
    # An option typed on the command line wins; otherwise use the .env setting.
    seconds = seconds or config["PRICE_TICK_SECONDS"]
    step_bps = step_bps or config["PRICE_MAX_STEP_BPS"]
    database_url = config["DATABASE_URL"]
    # The random number source. With --seed the same sequence of rolls is
    # produced every run (handy for demos and tests); without it, it differs each run.
    rng = random.Random(seed)

    def report(result):
        """Called after every successful round with a pricing.TickResult."""
        if result.market_open:
            click.echo(
                f"[{_stamp(tz_name)}] market OPEN: {result.stocks_updated} stocks updated, "
                f"{result.prices_changed} prices changed"
            )
        else:
            click.echo(f"[{_stamp(tz_name)}] market CLOSED: prices held")

    def report_error(exc):
        """Called when a round fails (e.g. MySQL is down); the loop keeps going."""
        click.echo(f"[{_stamp(tz_name)}] price update failed ({type(exc).__name__}: {exc}); will retry", err=True)

    if not once:
        click.echo(f"Price generator running: every {seconds}s, up to {step_bps / 100:g}% per move. Ctrl+C to stop.")
    try:
        pricing.run_loop(
            # A small function that opens a NEW database connection each round.
            lambda: db.get_connection(database_url),
            tz_name,
            seconds=seconds,
            max_step_bps=step_bps,
            rng=rng,
            on_tick=report,
            on_error=report_error,
            max_ticks=1 if once else None,  # --once = exactly one round
        )
    except KeyboardInterrupt:
        # Ctrl+C raises KeyboardInterrupt; we catch it so the program ends tidily
        # instead of printing a long error message.
        click.echo("\nPrice generator stopped.")
