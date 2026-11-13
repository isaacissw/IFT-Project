# Feature: phase-1-buy-stock, Property 6: Market override dominates the schedule
# Feature: phase-1-buy-stock, Property 7: Scheduled market-open rule under no override
"""Tests for the market-open rule in ``app/services/market.py``.

Covers test task 18.1 / 18.8 and Requirements 6.2, 6.3, 6.4, 6.5:

  - Property 6 (Requirements 6.2, 6.3): the ``market_settings`` override
    dominates the weekday schedule. For ANY instant, ``force_open`` reports the
    market open and ``force_closed`` reports it closed, no matter what the
    schedule says (or whether a schedule row exists at all).
  - Property 7 (Requirements 6.4, 6.5): under ``override_mode = 'none'`` the
    market is open exactly when a schedule row exists for the local weekday,
    ``is_holiday`` is 0, and ``market_open_time <= local_time < market_close_time``;
    otherwise it is closed.
  - Example tests: closed-by-hours (before open, and at/after close),
    closed-by-``is_holiday=1``, closed-by-``force_closed``, open-by-``force_open``,
    and open during hours under ``'none'``.

Test isolation
--------------
Every example builds its OWN fresh, empty in-memory database
(``app.db.get_connection(":memory:")`` + ``init_db``), following the pattern in
``tests/test_money_decimal.py``. We deliberately do NOT use the shared, seeded
``db_conn`` fixture: Hypothesis reuses a function-scoped fixture across every
generated example, so a shared connection would accumulate ``market_schedule``
rows (``day_of_week`` is UNIQUE) and mutate shared state across examples. A
fresh per-example database keeps each example independent, so no health-check
suppression is needed.

Clock injection
---------------
``now`` is injected as a timezone-aware UTC ``datetime`` and the market time
zone is ``America/New_York``, so these tests never depend on the wall clock
(Requirement 18.8). For Property 7 we start from a chosen NY-local datetime,
build the schedule for that local weekday, and construct the UTC ``now`` by
converting that local instant to UTC. That avoids UTC-offset confusion: the
local weekday and local time-of-day we schedule against are exactly the ones
the function will recover when it converts ``now`` back to NY time.
"""

from contextlib import contextmanager
from datetime import datetime
from zoneinfo import ZoneInfo

from hypothesis import given, settings
from hypothesis import strategies as st

from app import db
from dbhelpers import fresh_db
from app.services.market import is_market_open

# The market time zone used throughout these tests.
TZ_NAME = "America/New_York"
_NY = ZoneInfo(TZ_NAME)
_UTC = ZoneInfo("UTC")

# Monday=0 .. Sunday=6 -> the day_of_week names stored in market_schedule,
# mirroring datetime.weekday() and the mapping in app/services/market.py.
_WEEKDAY_NAMES = (
    "Monday",
    "Tuesday",
    "Wednesday",
    "Thursday",
    "Friday",
    "Saturday",
    "Sunday",
)

# A fixed ISO-8601 timestamp used for the created_at/updated_at columns in test
# setup rows. Its value is irrelevant to the market-open rule.
_TS = "2024-01-01T00:00:00+00:00"


@contextmanager
def _fresh_db():
    """Yield a connection to a brand-new, empty, isolated in-memory database.

    Opened through ``app.db.get_connection`` (PARSE_DECLTYPES on, converters
    registered) and built from ``schema.sql`` via ``init_db``. ``:memory:``
    means the database lives only for the life of this connection, so each
    caller gets a private, empty database with no state shared across examples.
    """
    with fresh_db(seed=False) as conn:
        yield conn


def _insert_admin(conn):
    """Insert one minimal admin user and return its ``user_id``.

    ``market_schedule.created_by_admin_id`` and ``market_settings.updated_by``
    are foreign keys to ``users(user_id)``, so at least one user must exist
    before we can write those rows. We go through the ``app.db`` insert helper
    rather than raw SQL.
    """
    return db.insert_user(
        conn,
        first_name="Admin",
        last_name="User",
        email="admin@example.com",
        username="admin",
        password_hash="x",  # any non-null string satisfies the schema
        user_type="admin",
        cash_balance=0,
        is_active=1,
        created_at=_TS,
        updated_at=_TS,
    )


def _set_settings(conn, admin_id, override_mode):
    """Insert the single ``market_settings`` row with the given override mode."""
    db.insert_market_settings(
        conn,
        override_mode=override_mode,
        updated_by=admin_id,
        updated_at=_TS,
    )


def _add_schedule(
    conn,
    admin_id,
    *,
    day_of_week,
    market_open_time,
    market_close_time,
    is_holiday,
):
    """Insert one ``market_schedule`` row for a weekday.

    Times are ``"HH:MM:SS"`` strings per the schema's TIME columns; the market
    service parses them back with ``%H:%M:%S``.
    """
    db.insert_market_schedule_row(
        conn,
        created_by_admin_id=admin_id,
        day_of_week=day_of_week,
        market_open_time=market_open_time,
        market_close_time=market_close_time,
        is_holiday=is_holiday,
        created_at=_TS,
        updated_at=_TS,
    )


def _utc_from_ny(ny_dt_naive):
    """Build a timezone-aware UTC ``now`` from a naive NY-local datetime.

    We attach the NY zone to the naive local datetime, then convert to UTC. The
    function under test converts that UTC instant back to NY, recovering exactly
    the local weekday and time-of-day we started from -- so the schedule we set
    up for that local weekday/time is the one it evaluates.
    """
    return ny_dt_naive.replace(tzinfo=_NY).astimezone(_UTC)


# ---------------------------------------------------------------------------
# Property 6: Market override dominates the schedule (Requirements 6.2, 6.3).
# ---------------------------------------------------------------------------

# Generate an arbitrary UTC instant across a wide range of years so the local
# weekday/time vary freely; the override must win regardless.
_utc_datetimes = st.datetimes(
    min_value=datetime(2000, 1, 1),
    max_value=datetime(2100, 12, 31, 23, 59, 59),
    timezones=st.just(_UTC),
)


@settings(max_examples=200)
@given(now=_utc_datetimes, force_open=st.booleans())
def test_override_dominates_schedule(now, force_open):
    """force_open -> always open, force_closed -> always closed, for ANY now.

    We deliberately insert a schedule row that would say the OPPOSITE of the
    override for the generated instant (a holiday when force_open, a wide-open
    all-day window when force_closed), proving the override wins over whatever
    the schedule would otherwise decide. We also prove it wins when there is no
    schedule row at all.

    # Feature: phase-1-buy-stock, Property 6: Market override dominates the schedule
    """
    mode = "force_open" if force_open else "force_closed"
    weekday_name = _WEEKDAY_NAMES[now.astimezone(_NY).weekday()]

    with _fresh_db() as conn:
        admin_id = _insert_admin(conn)
        _set_settings(conn, admin_id, mode)
        # Schedule set to the OPPOSITE of the override for this weekday.
        if force_open:
            # Would be closed by schedule (holiday) -- override must force open.
            _add_schedule(
                conn,
                admin_id,
                day_of_week=weekday_name,
                market_open_time="00:00:00",
                market_close_time="23:59:59",
                is_holiday=1,
            )
        else:
            # Would be open by schedule (all day) -- override must force closed.
            _add_schedule(
                conn,
                admin_id,
                day_of_week=weekday_name,
                market_open_time="00:00:00",
                market_close_time="23:59:59",
                is_holiday=0,
            )

        assert is_market_open(conn, now, tz_name=TZ_NAME) is force_open

    # And again with NO schedule row at all: the override still dominates.
    with _fresh_db() as conn:
        admin_id = _insert_admin(conn)
        _set_settings(conn, admin_id, mode)
        assert is_market_open(conn, now, tz_name=TZ_NAME) is force_open


# ---------------------------------------------------------------------------
# Property 7: Scheduled market-open rule under no override
# (Requirements 6.4, 6.5).
# ---------------------------------------------------------------------------

# A NY-local wall-clock datetime: we schedule against its weekday/time and build
# `now` from it. The year range is wide so weekdays vary; seconds are included
# so the [open, close) boundary logic is exercised at second resolution.
_ny_local_datetimes = st.datetimes(
    min_value=datetime(2000, 1, 1),
    max_value=datetime(2100, 12, 31, 23, 59, 59),
)

# A time-of-day used for the schedule's open/close bounds.
_times_of_day = st.times()


def _expected_open(local_time, open_t, close_t, is_holiday, has_row):
    """Independently compute the expected open/closed decision under 'none'.

    Mirrors Requirement 6.4/6.5 without reusing the implementation: open only
    when a row exists, it is not a holiday, and open <= local_time < close.
    """
    if not has_row or is_holiday:
        return False
    return open_t <= local_time < close_t


@settings(max_examples=200)
@given(
    local_dt=_ny_local_datetimes,
    open_t=_times_of_day,
    close_t=_times_of_day,
    is_holiday=st.booleans(),
    has_row=st.booleans(),
)
def test_scheduled_rule_under_none(local_dt, open_t, close_t, is_holiday, has_row):
    """Under 'none', the function matches the independent schedule rule.

    We pick a NY-local datetime, optionally create a schedule row for its
    weekday with generated open/close times and holiday flag, and build a UTC
    ``now`` from that local instant. We then assert ``is_market_open`` agrees
    with an independently computed open/closed decision. Microseconds are
    dropped from the local time because the schedule stores only second
    resolution ("HH:MM:SS").

    # Feature: phase-1-buy-stock, Property 7: Scheduled market-open rule under no override
    """
    # The schedule stores HH:MM:SS, so compare at second resolution.
    local_dt = local_dt.replace(microsecond=0)
    local_time = local_dt.time()
    weekday_name = _WEEKDAY_NAMES[local_dt.weekday()]
    now = _utc_from_ny(local_dt)

    expected = _expected_open(
        local_time,
        open_t,
        close_t,
        is_holiday,
        has_row,
    )

    with _fresh_db() as conn:
        admin_id = _insert_admin(conn)
        _set_settings(conn, admin_id, "none")
        if has_row:
            _add_schedule(
                conn,
                admin_id,
                day_of_week=weekday_name,
                market_open_time=open_t.strftime("%H:%M:%S"),
                market_close_time=close_t.strftime("%H:%M:%S"),
                is_holiday=1 if is_holiday else 0,
            )

        assert is_market_open(conn, now, tz_name=TZ_NAME) is expected


# ---------------------------------------------------------------------------
# Example tests (Requirement 18.1): concrete, named scenarios.
#
# Each uses a NY-local datetime on a weekday with a known schedule. We use
# Wednesday 2024-01-03 (a real Wednesday) so the weekday name is unambiguous,
# and standard market hours 09:30:00 - 16:00:00.
# ---------------------------------------------------------------------------

# 2024-01-03 is a Wednesday. Market hours for the examples below.
_OPEN = "09:30:00"
_CLOSE = "16:00:00"


def _wednesday_at(h, m, s=0):
    """A UTC ``now`` for Wednesday 2024-01-03 at the given NY-local time."""
    return _utc_from_ny(datetime(2024, 1, 3, h, m, s))


def _setup_wednesday(conn, *, override_mode="none", is_holiday=0,
                     open_t=_OPEN, close_t=_CLOSE):
    """Seed an admin, settings, and a Wednesday schedule row for an example."""
    admin_id = _insert_admin(conn)
    _set_settings(conn, admin_id, override_mode)
    _add_schedule(
        conn,
        admin_id,
        day_of_week="Wednesday",
        market_open_time=open_t,
        market_close_time=close_t,
        is_holiday=is_holiday,
    )


def test_example_closed_before_open():
    """Under 'none', a time before market_open_time is closed (Req 6.4)."""
    with _fresh_db() as conn:
        _setup_wednesday(conn)
        now = _wednesday_at(9, 0)  # 09:00 NY, before 09:30 open
        assert is_market_open(conn, now, tz_name=TZ_NAME) is False


def test_example_closed_at_close_boundary():
    """Under 'none', exactly market_close_time is closed; [open, close) (Req 6.4)."""
    with _fresh_db() as conn:
        _setup_wednesday(conn)
        now = _wednesday_at(16, 0, 0)  # 16:00:00 NY, the close boundary
        assert is_market_open(conn, now, tz_name=TZ_NAME) is False


def test_example_closed_after_close():
    """Under 'none', a time after market_close_time is closed (Req 6.4)."""
    with _fresh_db() as conn:
        _setup_wednesday(conn)
        now = _wednesday_at(16, 30)  # 16:30 NY, after 16:00 close
        assert is_market_open(conn, now, tz_name=TZ_NAME) is False


def test_example_closed_by_is_holiday():
    """Under 'none', a holiday is closed even within hours (Req 6.5)."""
    with _fresh_db() as conn:
        _setup_wednesday(conn, is_holiday=1)
        now = _wednesday_at(12, 0)  # midday, within hours, but holiday
        assert is_market_open(conn, now, tz_name=TZ_NAME) is False


def test_example_closed_by_force_closed():
    """force_closed reports closed even within scheduled hours (Req 6.3)."""
    with _fresh_db() as conn:
        _setup_wednesday(conn, override_mode="force_closed")
        now = _wednesday_at(12, 0)  # within hours, but override forces closed
        assert is_market_open(conn, now, tz_name=TZ_NAME) is False


def test_example_open_by_force_open():
    """force_open reports open even outside scheduled hours (Req 6.2)."""
    with _fresh_db() as conn:
        # Outside hours (03:00) AND marked holiday -- override must still open.
        _setup_wednesday(conn, override_mode="force_open", is_holiday=1)
        now = _wednesday_at(3, 0)
        assert is_market_open(conn, now, tz_name=TZ_NAME) is True


def test_example_open_during_hours_under_none():
    """Under 'none', a time within [open, close) on a non-holiday is open (Req 6.4)."""
    with _fresh_db() as conn:
        _setup_wednesday(conn)
        now = _wednesday_at(12, 0)  # midday, within 09:30-16:00
        assert is_market_open(conn, now, tz_name=TZ_NAME) is True


def test_example_open_at_open_boundary():
    """Under 'none', exactly market_open_time is open; [open, close) (Req 6.4)."""
    with _fresh_db() as conn:
        _setup_wednesday(conn)
        now = _wednesday_at(9, 30, 0)  # 09:30:00 NY, the open boundary
        assert is_market_open(conn, now, tz_name=TZ_NAME) is True
