"""Seed loader: fills a brand-new, empty database with the demo data.

"Seeding" means putting starting data into a new database. ``flask --app app
init-db`` builds the empty tables and then calls ``seed_into(conn)``, which adds:

  - one admin user (``admin`` / Ada Admin, cash 0.00),
  - one customer user (``trainee1`` / Taylor Trainee, cash 10000.00),
  - six stocks created by the admin,
  - seven ``market_schedule`` rows (Monday..Sunday, always open in the demo),
  - one ``market_settings`` row (``override_mode`` = 'none'),

leaving ``holdings`` and ``transactions`` empty.

Design rules this file follows:

  - NO SQL here. All SQL lives in ``app/db.py``; this module only calls the
    named insert helpers there and never touches a cursor or connection
    directly (it does not call ``conn.execute``).
  - Money is ``Decimal`` built from strings (``Decimal("10000.00")``), never
    float, so amounts are exact.
  - Timestamps come from ``app.clock.now_utc()`` and are passed as ISO-8601
    strings via ``.isoformat()``.
  - Passwords come ONLY from environment variables (SEED_ADMIN_PASSWORD and
    SEED_CUSTOMER_PASSWORD in .env) and are stored ONLY as a one-way hash
    (Werkzeug's ``generate_password_hash``), so the real password is never in
    the database. A missing setting raises a clear error; no default password
    is ever invented and no secret is printed.
"""

import os
from decimal import Decimal

from werkzeug.security import generate_password_hash

from app import db
from app.clock import now_utc


# The six demo stocks: (ticker, company name, shares outstanding, starting price).
# They are chosen to make demos easy: SAGU has only 50 shares (try buying 51 to
# see "Insufficient Shares") and GCNY costs more than the demo customer's
# $10,000 (try buying 1 to see "Insufficient Funds").
_SEED_STOCKS = (
    ("SSOL", "Sonoran Solar Corp", 1000000, Decimal("42.50")),
    ("DSRT", "Desert Data Systems", 500000, Decimal("128.75")),
    ("CPTR", "Copper Trail Mining", 250000, Decimal("17.20")),
    ("MESA", "Mesa Aerospace Inc", 100000, Decimal("310.00")),
    ("SAGU", "Saguaro Rare Earths", 50, Decimal("5.00")),
    ("GCNY", "Grand Canyon Holdings", 10000, Decimal("12500.00")),
)

# One market_schedule row per weekday; the demo market is open all day every
# day (00:00:00..23:59:59, not a holiday) unless an override is set later.
_WEEKDAYS = (
    "Monday",
    "Tuesday",
    "Wednesday",
    "Thursday",
    "Friday",
    "Saturday",
    "Sunday",
)


def _require_env(name):
    """Return the value of a required env var, or raise a clear error.

    Seed passwords must come from the environment; we never invent a default.
    The error names the missing variable but never prints any secret value.
    """
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(
            f"{name} is not set. Set it in the environment (or .env) before "
            f"seeding; the seed loader will not invent a default password."
        )
    return value


def seed_into(conn):
    """Insert all the demo data into an empty (tables-only) database.

    Expects ``conn`` to already have the schema (``init_db`` was run). The
    caller is responsible for the transaction boundary (the init-db CLI wraps
    this in one ``transaction()``); the test fixture may call it directly.

    Returns the admin and customer ``user_id`` values as a small dict, handy
    for callers/tests, though nothing here depends on specific id numbers.
    """
    # One timestamp for the whole seed run, from the single clock source, as an
    # ISO-8601 string. created_at/updated_at/price_updated_at all use it.
    now = now_utc().isoformat()

    # Read the required seed passwords from the environment and hash them.
    # Only the hash is ever stored; the plaintext never leaves this function.
    admin_password_hash = generate_password_hash(_require_env("SEED_ADMIN_PASSWORD"))
    customer_password_hash = generate_password_hash(_require_env("SEED_CUSTOMER_PASSWORD"))

    # --- Admin user ------------------------------------------------------
    admin_user_id = db.insert_user(
        conn,
        first_name="Ada",
        last_name="Admin",
        email="admin@example.com",
        username="admin",
        password_hash=admin_password_hash,
        user_type="admin",
        cash_balance=Decimal("0.00"),
        is_active=1,
        created_at=now,
        updated_at=now,
    )

    # --- Customer user (the fixed demo trainee) --------------------------
    customer_user_id = db.insert_user(
        conn,
        first_name="Taylor",
        last_name="Trainee",
        email="trainee1@example.com",
        username="trainee1",
        password_hash=customer_password_hash,
        user_type="customer",
        cash_balance=Decimal("10000.00"),
        is_active=1,
        created_at=now,
        updated_at=now,
    )

    # --- Six stocks, all created by the admin ----------------------------
    for ticker, company_name, shares_outstanding, price in _SEED_STOCKS:
        db.insert_stock(
            conn,
            ticker_symbol=ticker,
            company_name=company_name,
            company_shares_outstanding=shares_outstanding,
            current_price=price,
            description=None,
            logo_filename=None,
            created_by=admin_user_id,
            created_at=now,
            price_updated_at=now,
        )

    # --- Seven market_schedule rows (one per weekday) --------------------
    # TODO (before the final demo): the business case says the default schedule
    # is weekdays 9:00-17:00 and closed on weekends. For development and testing
    # the demo market is open all day, every day. See "Before the final demo" in
    # CLAUDE.md and tests/dbhelpers.py:use_business_hours for the 9-5 setup.
    for day in _WEEKDAYS:
        db.insert_market_schedule_row(
            conn,
            created_by_admin_id=admin_user_id,
            day_of_week=day,
            market_open_time="00:00:00",
            market_close_time="23:59:59",
            is_holiday=0,
            created_at=now,
            updated_at=now,
        )

    # --- One market_settings row (no override) ---------------------------
    db.insert_market_settings(
        conn,
        override_mode="none",
        updated_by=None,
        updated_at=now,
    )

    # holdings and transactions are intentionally left empty.

    return {"admin_user_id": admin_user_id, "customer_user_id": customer_user_id}
