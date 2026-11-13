"""Clock helper.

This module provides ``now_utc()`` — the single source of "now" for the whole
application. Every timestamp written to the database and every market-open
check flows from this one function.

Why a dedicated module instead of calling ``datetime.now`` directly all over
the code?

- Determinism in tests. Services never call ``now_utc()`` themselves; routes
  call it and pass the resulting ``now`` into services as a plain argument.
  That lets tests inject a fixed time so "market open" / "market closed"
  behavior does not depend on the real wall clock.
- Consistency. Timestamps are always timezone-aware UTC, stored as ISO-8601
  text, and only converted to the market time zone for display.
"""

from datetime import datetime, timezone


def now_utc() -> datetime:
    """Return the current time as a timezone-aware UTC ``datetime``.

    This is the single source of "now". Keeping it tiny and in one place is
    deliberate: tests inject time by passing a fixed value where this result
    would normally be used, so time-dependent logic stays deterministic.
    """
    return datetime.now(timezone.utc)
