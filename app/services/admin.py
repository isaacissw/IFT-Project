"""Administrator service: the stock inventory and the market controls.

Rules from the SAD and business case:

  * An administrator creates a stock with a company name, ticker, shares
    outstanding and an INITIAL price, plus an optional description.
    (Company logos were dropped from the project's scope; the unused
    ``stocks.logo_filename`` column is left in the schema.)
  * After creation the price is never changed by an administrator (only the
    system's price generator moves it). Nothing in this module can change it.
  * An administrator can change the market hours and switch a manual override
    that forces the market open or closed.

Everything here takes plain arguments (no Flask) so it can be tested directly.
"""

import re
from decimal import Decimal

from app import db
from app.money import quantize
from app.services.trading import ValidationError

NAME_MAX = 255
DESCRIPTION_MAX = 2000
MAX_SHARES = 1_000_000_000
MAX_INITIAL_PRICE = Decimal("1000000.00")

WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
OVERRIDE_MODES = ("none", "force_open", "force_closed")

_TICKER_PATTERN = re.compile(r"^[A-Z]{1,5}$")
_SHARES_PATTERN = re.compile(r"^[0-9]{1,12}$")
_PRICE_PATTERN = re.compile(r"^[0-9]+(\.[0-9]{1,2})?$")
_TIME_PATTERN = re.compile(r"^([01][0-9]|2[0-3]):([0-5][0-9])(:([0-5][0-9]))?$")


class FormError(Exception):
    """One or more form fields were not acceptable. ``errors`` maps field name -> message."""

    def __init__(self, errors):
        self.errors = errors
        super().__init__("; ".join(errors.values()))


# --------------------------------------------------------------------------
# Field checks (no database access)
# --------------------------------------------------------------------------


def _check_name(value, errors):
    name = (value or "").strip()
    if not name:
        errors["company_name"] = "Please enter the company name."
    elif len(name) > NAME_MAX:
        errors["company_name"] = f"The company name must be at most {NAME_MAX} characters."
    return name


def _check_description(value, errors):
    text = (value or "").strip()
    if len(text) > DESCRIPTION_MAX:
        errors["description"] = f"The description must be at most {DESCRIPTION_MAX} characters."
    return text or None  # blank means "no description"


def _check_shares(value, errors):
    text = (value or "").strip()
    if not _SHARES_PATTERN.match(text) or int(text) < 1:
        errors["shares"] = "Shares outstanding must be a whole number greater than 0."
        return None
    shares = int(text)
    if shares > MAX_SHARES:
        errors["shares"] = f"Shares outstanding cannot be more than {MAX_SHARES:,}."
        return None
    return shares


def _check_price(value, errors):
    text = (value or "").strip()
    if not _PRICE_PATTERN.match(text) or len(text) > 20 or Decimal(text) <= 0:
        errors["price"] = "The initial price must be greater than $0.00 with at most two decimal places."
        return None
    price = quantize(Decimal(text))  # built from text, so exact
    if price > MAX_INITIAL_PRICE:
        errors["price"] = "The initial price cannot be more than $1,000,000.00."
        return None
    return price


def _check_ticker(value, errors):
    ticker = (value or "").strip().upper()
    if not _TICKER_PATTERN.match(ticker):
        errors["ticker"] = "The ticker must be 1-5 letters (for example SSOL)."
    return ticker


def validate_new_stock(company_name, ticker, shares, price, description):
    """Check the create-stock form. Returns ``(clean_values, errors)``; errors is empty if OK."""
    errors = {}
    clean = {
        "company_name": _check_name(company_name, errors),
        "ticker": _check_ticker(ticker, errors),
        "shares": _check_shares(shares, errors),
        "price": _check_price(price, errors),
        "description": _check_description(description, errors),
    }
    return clean, errors


# --------------------------------------------------------------------------
# Stock inventory
# --------------------------------------------------------------------------


def create_stock(conn, admin_id, company_name, ticker, shares, price, description, now):
    """Create a new stock and return its ``stock_id``. Raises ``FormError`` if anything is wrong.

    ``price`` is the INITIAL price; this is the only time an administrator sets it.
    ``now`` (timezone-aware UTC) becomes the creation and price timestamps.
    """
    clean, errors = validate_new_stock(company_name, ticker, shares, price, description)
    if "ticker" not in errors and db.get_stock_by_ticker(conn, clean["ticker"]) is not None:
        errors["ticker"] = "A stock with that ticker already exists."
    if errors:
        raise FormError(errors)

    stamp = now.isoformat()
    try:
        return db.insert_stock(
            conn,
            ticker_symbol=clean["ticker"],
            company_name=clean["company_name"],
            company_shares_outstanding=clean["shares"],
            current_price=clean["price"],
            description=clean["description"],
            logo_filename=None,  # logos are out of scope; the column stays empty
            created_by=admin_id,
            created_at=stamp,
            price_updated_at=stamp,
        )
    except db.DuplicateError:
        # Someone created the same ticker between our check and the insert.
        raise FormError({"ticker": "A stock with that ticker already exists."})


def update_stock(conn, stock_id, company_name, description, shares):
    """Change a stock's name, description and shares outstanding.

    The ticker and the PRICE are never changed. Raises ``FormError`` for bad
    fields and ``ValidationError("Stock not found.")`` for an unknown stock.

    Shares outstanding cannot be set below the number customers currently own.
    The stock row is locked while we check and update, and every trade locks the
    same row first, so the number owned cannot change in between.
    """
    errors = {}
    name = _check_name(company_name, errors)
    desc = _check_description(description, errors)
    new_shares = _check_shares(shares, errors)
    if errors:
        raise FormError(errors)

    with db.transaction(conn):
        stock = db.get_stock(conn, stock_id, for_update=True)
        if stock is None:
            raise ValidationError("Stock not found.")
        held = db.get_shares_held_sum(conn, stock_id)
        if new_shares < held:
            raise FormError({
                "shares": f"Shares outstanding cannot be less than the {held:,} shares customers currently own."
            })
        db.update_stock_details(conn, stock_id, name, desc, new_shares)


# --------------------------------------------------------------------------
# Market controls
# --------------------------------------------------------------------------


def _parse_time(text):
    """'09:00' or '09:00:00' -> 'HH:MM:SS', or None if it is not a valid time of day."""
    match = _TIME_PATTERN.match((text or "").strip())
    if not match:
        return None
    return f"{match.group(1)}:{match.group(2)}:{match.group(4) or '00'}"


def update_market_schedule(conn, days, now):
    """Save new weekly hours. ``days`` maps each weekday name to a dict with
    ``closed`` (bool), ``open`` and ``close`` ('HH:MM' text).

    A closed day keeps its stored hours and is marked closed all day. An open day
    needs a valid opening time that is earlier than its closing time. All seven
    days are saved together or not at all. Raises ``FormError`` keyed by weekday.
    """
    errors = {}
    planned = []
    for day in WEEKDAYS:
        entry = days.get(day)
        if entry is None:
            errors[day] = "Missing."
            continue
        if entry.get("closed"):
            planned.append((day, None, None))
            continue
        open_time = _parse_time(entry.get("open"))
        close_time = _parse_time(entry.get("close"))
        if open_time is None or close_time is None:
            errors[day] = "Enter the opening and closing time (for example 09:00 and 17:00)."
        elif open_time >= close_time:
            errors[day] = "The opening time must be earlier than the closing time."
        else:
            planned.append((day, open_time, close_time))
    if errors:
        raise FormError(errors)

    with db.transaction(conn):
        for day, open_time, close_time in planned:
            if open_time is None:
                db.set_day_closed(conn, day, now.isoformat())
            else:
                db.set_day_hours(conn, day, open_time, close_time, now.isoformat())


def set_market_override(conn, admin_id, mode, now):
    """Set the manual override: 'none' (follow the schedule), 'force_open' or 'force_closed'."""
    if mode not in OVERRIDE_MODES:
        raise ValidationError("Please choose one of the three market override options.")
    db.set_market_override(conn, mode, admin_id, now.isoformat())
