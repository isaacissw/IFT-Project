"""Market service.

Exposes ``is_market_open(conn, now, tz_name) -> bool``, which decides whether
the market is open at a given instant. It honors the ``market_settings``
override first and otherwise applies the weekday ``market_schedule`` in the
configured market time zone.

Layering rules this module follows (per design.md):

  - **No Flask imports.** This is a pure business-rule service. It never touches
    ``flask.request``/``flask.session`` or the app config; the market time zone
    arrives as a plain ``tz_name`` argument so the function is unit-testable.
  - **All database reads go through ``app/db.py``.** There is no SQL here; the
    function only calls the named query helpers ``get_market_settings`` and
    ``get_schedule_for_day``.
  - **``now`` is injected, never read from a clock here.** The caller passes a
    timezone-aware UTC ``datetime`` (from ``app.clock.now_utc()``), so tests can
    feed any instant and get a deterministic answer.
"""

from datetime import datetime
from zoneinfo import ZoneInfo

from app import db

# Default market time zone, matching MARKET_TIMEZONE's default in app/config.py.
# Passed as a plain argument (not read from Flask config) so this module stays
# free of any Flask dependency and remains unit-testable.
DEFAULT_MARKET_TIMEZONE = "America/New_York"

# Weekday index (Monday=0 .. Sunday=6) -> the day_of_week names stored in the
# market_schedule table. Python's datetime.weekday() returns this 0..6 index.
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

    The DB's TIME converter (app/db.py) hands these back as plain strings, so we
    parse them with the matching ``%H:%M:%S`` format to compare them as a
    time-of-day against the local wall-clock time.
    """
    return datetime.strptime(value, "%H:%M:%S").time()


def is_market_open(conn, now, tz_name=DEFAULT_MARKET_TIMEZONE):
    """Decide whether the market is open at ``now``. No Flask imports.

    Arguments:
      - ``conn``: an open ``app/db.py`` connection (all reads go through it).
      - ``now``: a timezone-aware UTC ``datetime`` (injected by the caller).
      - ``tz_name``: the market time zone name (e.g. ``"America/New_York"``),
        used to convert ``now`` to local wall-clock time for the schedule check.

    Rules (Requirement 6):
      1. Read the single ``market_settings`` row. ``force_open`` -> always open;
         ``force_closed`` -> always closed. The override dominates the schedule.
      2. Under ``none``: convert ``now`` to the market time zone, find the
         ``market_schedule`` row for that weekday, and report open only when a
         row exists, ``is_holiday`` is 0, and
         ``market_open_time <= local_time_of_day < market_close_time``.
      3. If no schedule row exists for the weekday, or ``is_holiday`` is 1,
         report closed.
    """
    # 1) Override dominates the schedule (Requirements 6.2, 6.3).
    settings = db.get_market_settings(conn)
    override_mode = settings["override_mode"] if settings is not None else "none"
    if override_mode == "force_open":
        return True
    if override_mode == "force_closed":
        return False

    # 2) 'none': evaluate the weekday schedule in the market time zone.
    local_now = now.astimezone(ZoneInfo(tz_name))
    weekday_name = _WEEKDAY_NAMES[local_now.weekday()]

    schedule = db.get_schedule_for_day(conn, weekday_name)
    # 3) No row for this weekday, or the day is a holiday -> closed
    #    (Requirement 6.5). is_holiday reads back as int 0/1.
    if schedule is None or int(schedule["is_holiday"]) == 1:
        return False

    # Open when the local time-of-day is within [open, close) (Requirement 6.4).
    open_time = _parse_time_of_day(schedule["market_open_time"])
    close_time = _parse_time_of_day(schedule["market_close_time"])
    local_time = local_now.time()
    return open_time <= local_time < close_time
