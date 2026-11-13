# Feature: phase-1-buy-stock, Property 1: Decimal money round-trips exactly through storage
"""Property 1: Decimal money round-trips exactly through storage.

Validates: Requirements 2.10, 3.2

Requirement 2.10 says every money/price column is ``DECIMAL(10,2)``. Requirement
3.2 says the data-access module registers an adapter (``Decimal`` -> str) and a
converter (``DECIMAL`` column -> ``Decimal`` from its string) so money survives a
write/read cycle *exactly*, with no float rounding.

These tests prove that path end-to-end by writing a ``Decimal`` into a real
``DECIMAL(10,2)`` column from the schema -- ``users.cash_balance`` -- through the
same connection the app uses (``app.db.get_connection`` / ``PARSE_DECLTYPES``),
then reading it back and asserting the recovered value is a ``Decimal`` equal to
what was written, exact to the cent.

Note on exactness: a ``DECIMAL`` column in SQLite has NUMERIC affinity, so the
engine normalises the stored text (``"0.10"`` is kept as the number ``0.10``,
which prints as ``0.1``). The *value* therefore round-trips exactly -- the cents
are never lost and ``Decimal("0.10") == Decimal("0.1")`` -- even though the
surface text may be shortened. That exact-value guarantee is precisely what
Requirements 2.10 and 3.2 are about: money survives storage without float drift.

All money is built from strings. A float is never used to construct a money
value anywhere in this file.

Test isolation
--------------
Each round-trip runs against its OWN fresh, empty in-memory database that this
module builds and tears down itself (``app.db.get_connection(":memory:")`` +
``app.db.init_db(conn)``). The Hypothesis property test deliberately does NOT
use the shared, function-scoped ``db_conn`` fixture: Hypothesis reuses a
function-scoped fixture across every generated example, so a shared connection
would accumulate rows across examples and could collide on the UNIQUE
username/email columns (e.g. when an explicit ``@example`` value equals a
generated one). ``db_conn`` is also seeded (it already contains ``admin`` and
``trainee1``), so writing more users there would risk colliding with seeded
rows and would mutate shared state across examples. A fresh per-example database
removes all of that: every example starts empty and independent, so the test is
stable regardless of Hypothesis's example ordering, and no health-check
suppression is needed.
"""

from contextlib import contextmanager
from decimal import Decimal

from hypothesis import given, settings, example
from hypothesis import strategies as st

from app import db
from dbhelpers import fresh_db

# The maximum value a DECIMAL(10,2) money column can hold in this schema.
MAX_WHOLE = 99_999_999  # 8 integer digits
MAX_CENTS = 99


def _decimal_money(whole: int, cents: int) -> Decimal:
    """Build a DECIMAL(10,2)-expressible Decimal from integer parts.

    Always goes through strings -- never a float -- so the value is exact and
    carries exactly two fractional digits (e.g. ``Decimal("172.34")``).
    """
    return Decimal(f"{whole}.{cents:02d}")


# Strategy: integers 0..99999999 for the whole part combined with cents 0..99,
# assembled into a Decimal via a string. This covers exactly the value space a
# DECIMAL(10,2) column can represent, and never touches float.
money_decimals = st.builds(
    _decimal_money,
    st.integers(min_value=0, max_value=MAX_WHOLE),
    st.integers(min_value=0, max_value=MAX_CENTS),
)


@contextmanager
def _fresh_db():
    """Yield a connection to a brand-new, empty, isolated in-memory database.

    Opened through ``app.db.get_connection`` (so PARSE_DECLTYPES is on and the
    DECIMAL adapter/converter fire) and built from ``schema.sql`` via
    ``init_db``. ``:memory:`` means the database lives only for the life of this
    connection, so each caller gets a private, empty database with no seeded
    rows and no state shared with any other example or test. The connection is
    always closed on exit, which also discards the in-memory database.
    """
    with fresh_db(seed=False) as conn:
        yield conn


def _round_trip(conn, value: Decimal) -> Decimal:
    """Insert ``value`` into users.cash_balance and read it back.

    Inserts a minimal, fully valid ``users`` row (satisfying every NOT NULL and
    CHECK constraint) with ``cash_balance = value``, then selects that column
    back out. Because the connection was opened with PARSE_DECLTYPES and the
    DECIMAL converter is registered, the returned value is a ``Decimal``.

    The row is written into a fresh, empty database (see ``_fresh_db``), so the
    UNIQUE username/email are trivially unique -- a single fixed pair is enough
    and nothing can collide across examples.
    """
    cur = conn.execute(
        """
        INSERT INTO users (
            first_name, last_name, email, username, password_hash,
            user_type, cash_balance, is_active, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "Test",
            "User",
            # email and username are UNIQUE. The database is fresh and empty for
            # every round-trip, so a single fixed pair never collides -- no
            # dependence on the value and no cross-example shared state.
            "rt_test_user@example.com",
            "rt_test_user",
            "x",  # password_hash: any non-null string satisfies the schema
            "customer",  # user_type CHECK: 'customer' or 'admin'
            value,  # cash_balance: the DECIMAL(10,2) under test, CHECK >= 0
            1,
            "2024-01-01T00:00:00+00:00",
            "2024-01-01T00:00:00+00:00",
        ),
    )
    row = conn.execute(
        "SELECT cash_balance FROM users WHERE user_id = ?",
        (cur.lastrowid,),
    ).fetchone()
    return row["cash_balance"]


# No fixture is used here: each generated example opens its own fresh in-memory
# database, so there is no function-scoped fixture to reuse and therefore no
# health check to suppress. max_examples stays well above the required minimum
# of 100 iterations.
@settings(max_examples=200)
@given(value=money_decimals)
@example(value=Decimal("0.10"))
@example(value=Decimal("172.34"))
@example(value=Decimal("99999999.99"))
def test_decimal_money_round_trips_exactly(value):
    """A Decimal written to a DECIMAL(10,2) column reads back as an equal Decimal.

    We assert two things: the recovered object is a ``Decimal`` (so the
    converter fired rather than handing back a float or str), and it is exactly
    equal to the value written -- the cents are preserved with no float drift.
    Each example runs against its own fresh, empty in-memory database, so the
    outcome does not depend on how Hypothesis orders its examples.
    """
    with _fresh_db() as conn:
        read_back = _round_trip(conn, value)

    assert isinstance(read_back, Decimal)
    assert read_back == value
    # Exact to the cent: quantizing to 2dp does not change the recovered value,
    # i.e. no fractional cent slipped in during storage.
    assert read_back == read_back.quantize(Decimal("0.01"))


def test_decimal_money_round_trips_explicit_examples():
    """The named money values from the task round-trip exactly and by type.

    Equality is numeric (exact to the cent). The surface text may be
    normalised by SQLite's NUMERIC affinity (``0.10`` -> ``0.1``), but the value
    is identical, which is what matters for money. Each value uses its own fresh,
    empty in-memory database so there is no collision with seeded rows or with
    the other values in the loop.
    """
    for text in ("0.10", "172.34", "99999999.99"):
        expected = Decimal(text)
        with _fresh_db() as conn:
            read_back = _round_trip(conn, expected)
        assert isinstance(read_back, Decimal)
        assert read_back == expected


def test_three_dimes_sum_to_exactly_thirty_cents():
    """Three additions of 0.10 equal exactly 0.30 (no float drift).

    This is the classic case that breaks with float (0.1 + 0.1 + 0.1 == 0.30 is
    False in binary float). With Decimal built from strings it is exact, which
    is why all money in the system is Decimal.
    """
    dime = Decimal("0.10")
    assert dime + dime + dime == Decimal("0.30")
    assert sum([dime, dime, dime], Decimal("0")) == Decimal("0.30")
    assert dime * 3 == Decimal("0.30")
