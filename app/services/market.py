"""Market service: answers one question, "is the market open right now?"

``is_market_open(conn, now, tz_name)`` returns True or False. Trades are only
allowed when it says True, and the price generator only moves prices then.

How it decides, in order:

  1. The administrator's MANUAL OVERRIDE (the ``market_settings`` table) wins:
     "force_open" means open, "force_closed" means closed, no matter the time.
  2. Otherwise ("none") the WEEKLY SCHEDULE decides (the ``market_schedule``
     table, one row per day of the week). We convert ``now`` to the market's
     time zone (New York by default), find today's row, and the market is open
     when the day is not marked closed and the time is at or after opening and
     BEFORE closing (so at exactly 5:00:00 PM a 9-5 market is already closed).

Like every service: no Flask, no SQL (reads go through ``app/db.py``), and the
time is passed in rather than read from the clock, so tests can ask "was the
market open at 8:59 AM last Monday?" and always get the same answer.
"""

from datetime import datetime
from zoneinfo import ZoneInfo

from app import db

# Default market time zone, matching MARKET_TIMEZONE's default in app/config.py.
DEFAULT_MARKET_TIMEZONE = "America/New_York"

# Python's datetime.weekday() gives 0 for Monday ... 6 for Sunday. This list turns
# that number into the day name stored in the market_schedule table.
_WEEKDAY_NAMES = (
    "Monday",
    "Tuesday",
    "Wednesday",
    "Thursday",
    "Friday",
    "Saturday",
    "Sunday",
)


def _parse_time_of_day(value):
    """Turn a stored ``"HH:MM:SS"`` market-hours string into a ``datetime.time``.

    app/db.py hands TIME columns back as plain "HH:MM:SS" text; turning it into
    a time object lets us compare it with the current time of day.
    """
    return datetime.strptime(value, "%H:%M:%S").time()


def is_market_open(conn, now, tz_name=DEFAULT_MARKET_TIMEZONE):
    """Return True if the market is open at the moment ``now``, else False.

    Arguments:
      - ``conn``: an open database connection from ``app/db.py``.
      - ``now``: the moment to check, as a timezone-aware UTC ``datetime``.
      - ``tz_name``: the market's time zone, e.g. ``"America/New_York"``. The
        schedule's hours are in this time zone.
    """
    # Step 1: the administrator's manual override beats the schedule.
    settings = db.get_market_settings(conn)
    override_mode = settings["override_mode"] if settings is not None else "none"
    if override_mode == "force_open":
        return True
    if override_mode == "force_closed":
        return False

    # Step 2: no override, so use the schedule. First work out the local day
    # and time in the market's time zone (UTC 15:00 is 10:00 AM in New York).
    local_now = now.astimezone(ZoneInfo(tz_name))
    weekday_name = _WEEKDAY_NAMES[local_now.weekday()]

    # Today's schedule row. No row, or a day marked "closed all day"
    # (is_holiday = 1), means closed.
    schedule = db.get_schedule_for_day(conn, weekday_name)
    if schedule is None or int(schedule["is_holiday"]) == 1:
        return False

    # Open from the opening time up to (but not including) the closing time.
    open_time = _parse_time_of_day(schedule["market_open_time"])
    close_time = _parse_time_of_day(schedule["market_close_time"])
    local_time = local_now.time()
    return open_time <= local_time < close_time
