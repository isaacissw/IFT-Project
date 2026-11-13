"""Data-access module: the ONLY module permitted to contain SQL or open a
database connection.

Phase 1 scope for this file (task 2.2): the SQLite connection setup (Decimal
adapter/converter, PARSE_DECLTYPES, row factory, PRAGMA foreign_keys), helpers
to turn ``DATABASE_URL`` into a filesystem path and ensure its directory
exists, the ``transaction()`` context manager built on ``BEGIN IMMEDIATE``, and
``init_db()`` which creates the schema from ``schema.sql``.

The named query functions (get_user_by_id, get_stock, insert_pending_buy, ...)
are added by later tasks. Routes and services never write SQL or touch a
connection directly.
"""

import os
import sqlite3
from contextlib import contextmanager
from decimal import Decimal

# Path to schema.sql, which lives at the project root (one level above app/).
# Resolving it from this file keeps init_db working no matter the current
# working directory.
_SCHEMA_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "schema.sql")


def _register_decimal():
    """Teach sqlite3 to store/read money as text, not float.

    SQLite has no real DECIMAL type and would otherwise store money as binary
    floating point, losing exact cents. We register:
      - an adapter so a Python ``Decimal`` is written as its string form, and
      - a converter so any column declared ``DECIMAL`` is read back as a
        ``Decimal`` rebuilt from that string.
    The converter only fires when a connection is opened with
    ``detect_types=sqlite3.PARSE_DECLTYPES`` (see ``get_connection``).
    """
    sqlite3.register_adapter(Decimal, lambda d: str(d))
    sqlite3.register_converter("DECIMAL", lambda b: Decimal(b.decode("utf-8")))


def _register_text_datetime():
    """Keep TIMESTAMP/TIME columns as the ISO-8601 text we stored.

    We store timestamps as ISO-8601 text (``now_utc().isoformat()``, e.g.
    ``"2026-10-19T04:38:35.326977+00:00"``) and market hours as ``"HH:MM:SS"``
    text, per the design rule "store SQLite timestamps as ISO-8601 text;
    convert for display only". But ``get_connection`` opens with
    ``detect_types=sqlite3.PARSE_DECLTYPES`` (required so the DECIMAL converter
    fires), which also activates sqlite3's built-in ``TIMESTAMP`` converter.
    That built-in converter is deprecated in Python 3.12+ and cannot parse an
    ``isoformat()`` string -- it chokes on the ``'T'`` separator and timezone
    offset and raises ``ValueError: not enough values to unpack`` when reading
    ANY row with a TIMESTAMP column. We register passthrough converters that
    simply decode the stored bytes as UTF-8 and hand back the plain ``str``,
    overriding the broken built-in so ISO-8601 timestamps and ``HH:MM:SS``
    times round-trip unchanged.
    """
    sqlite3.register_converter("TIMESTAMP", lambda b: b.decode("utf-8"))
    sqlite3.register_converter("TIME", lambda b: b.decode("utf-8"))


# Register once, at import time, so every connection in the process benefits.
_register_decimal()
_register_text_datetime()


def get_connection(db_path):
    """Open a SQLite connection configured the way every rule requires.

    - ``detect_types=sqlite3.PARSE_DECLTYPES`` so the DECIMAL converter fires.
    - ``isolation_level=None`` so Python's sqlite3 does NOT issue its own
      implicit BEGIN/COMMIT; we control transactions ourselves via
      ``transaction()`` and ``BEGIN IMMEDIATE``.
    - ``row_factory = sqlite3.Row`` so rows are accessible by column name.
    - ``PRAGMA foreign_keys = ON`` so foreign keys are enforced.
    """
    conn = sqlite3.connect(
        db_path,
        detect_types=sqlite3.PARSE_DECLTYPES,  # so the DECIMAL converter fires
        isolation_level=None,                  # we control BEGIN/COMMIT ourselves
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def db_path_from_url(url):
    """Turn a ``sqlite:///...`` URL into a plain filesystem path.

    Example: ``sqlite:///instance/stock_trading.db`` -> ``instance/stock_trading.db``.
    """
    prefix = "sqlite:///"
    assert url.startswith(prefix), "DATABASE_URL must start with 'sqlite:///'"
    return url[len(prefix):]


def ensure_instance_dir(db_path):
    """Make sure the directory that will hold the SQLite file exists."""
    os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)


@contextmanager
def transaction(conn):
    """Run a block inside one all-or-nothing transaction.

    ``BEGIN IMMEDIATE`` takes a write lock up front so two concurrent writers
    cannot interleave. On a clean exit we COMMIT; on any exception we ROLLBACK
    and re-raise, so a partially applied change is never left behind.
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


def init_db(conn):
    """Create the schema by executing schema.sql against ``conn``.

    Reads the full schema from the project-root ``schema.sql`` and runs it as a
    script. This is the only place that string is executed; it is a trusted,
    checked-in file, not user input.
    """
    with open(_SCHEMA_PATH, "r", encoding="utf-8") as f:
        schema_sql = f.read()
    conn.executescript(schema_sql)


# ---------------------------------------------------------------------------
# Named read query functions (task 4.1)
#
# Every VALUE below is passed with a ``?`` placeholder; no value is ever
# concatenated, f-string-interpolated, %-formatted, or .format()-ed into SQL.
# Money columns are declared DECIMAL(10,2) and come back as Decimal via the
# adapter/converter registered above. All of these are pure reads and are safe
# to call standalone (outside an open transaction).
# ---------------------------------------------------------------------------


def get_user_by_id(conn, user_id):
    """Return one user row by id, or None if there is no such user."""
    cur = conn.execute(
        "SELECT * FROM users WHERE user_id = ?",
        (user_id,),
    )
    return cur.fetchone()


def get_user_by_username(conn, username):
    """Return one user row by username, or None. Used to resolve the demo user."""
    cur = conn.execute(
        "SELECT * FROM users WHERE username = ?",
        (username,),
    )
    return cur.fetchone()


def get_stock(conn, stock_id):
    """Return one stock row by id, or None if there is no such stock."""
    cur = conn.execute(
        "SELECT * FROM stocks WHERE stock_id = ?",
        (stock_id,),
    )
    return cur.fetchone()


def list_stocks(conn):
    """Return every stock row, ordered by ticker, for the dashboard table."""
    cur = conn.execute(
        "SELECT * FROM stocks ORDER BY ticker_symbol"
    )
    return cur.fetchall()


def get_shares_held_sum(conn, stock_id):
    """Return the total shares of a stock held across all users.

    This is ``SUM(holdings.quantity)`` for the given stock and drives the
    Shares_Available calculation. SQLite returns NULL when there are no
    matching rows, so we coalesce to 0 and return a plain int.
    """
    cur = conn.execute(
        "SELECT COALESCE(SUM(quantity), 0) AS held FROM holdings WHERE stock_id = ?",
        (stock_id,),
    )
    row = cur.fetchone()
    return int(row["held"])


def list_holdings_for_user(conn, user_id):
    """Return the user's holdings joined to their stock, for the portfolio panel.

    Each row carries the holding quantity plus the stock's ticker, company
    name, and current price so the portfolio panel can show shares, current
    price, and market value without a second lookup.
    """
    cur = conn.execute(
        """
        SELECT
            h.holdings_id,
            h.user_id,
            h.stock_id,
            h.quantity,
            h.created_at,
            h.updated_at,
            s.ticker_symbol,
            s.company_name,
            s.current_price
        FROM holdings AS h
        JOIN stocks AS s ON s.stock_id = h.stock_id
        WHERE h.user_id = ?
        ORDER BY s.ticker_symbol
        """,
        (user_id,),
    )
    return cur.fetchall()


def get_holding(conn, user_id, stock_id):
    """Return the user's holding row for one stock, or None.

    Used to decide whether an upsert should add to an existing holding or
    insert a new one.
    """
    cur = conn.execute(
        "SELECT * FROM holdings WHERE user_id = ? AND stock_id = ?",
        (user_id, stock_id),
    )
    return cur.fetchone()


def get_recent_executed_transactions(conn, user_id, limit):
    """Return the user's executed transactions, newest first, up to ``limit``.

    The sort column is a FIXED literal (``ORDER BY executed_at DESC``) written
    directly in the SQL string -- it never comes from input. Only the row limit
    is a value, so it is passed with a ``?`` placeholder. A ticker column is
    joined in for display (NULL for cash transactions, which have no stock).
    """
    cur = conn.execute(
        """
        SELECT
            t.transaction_id,
            t.user_id,
            t.stock_id,
            t.transaction_type,
            t.quantity,
            t.price_per_share,
            t.total_amount,
            t.status,
            t.created_at,
            t.confirmed_at,
            t.executed_at,
            s.ticker_symbol
        FROM transactions AS t
        LEFT JOIN stocks AS s ON s.stock_id = t.stock_id
        WHERE t.user_id = ? AND t.status = 'executed'
        ORDER BY t.executed_at DESC
        LIMIT ?
        """,
        (user_id, limit),
    )
    return cur.fetchall()


def get_market_settings(conn):
    """Return the single market_settings row (setting_id = 1), or None."""
    cur = conn.execute(
        "SELECT * FROM market_settings WHERE setting_id = ?",
        (1,),
    )
    return cur.fetchone()


def get_schedule_for_day(conn, day_of_week):
    """Return the market_schedule row for a weekday name, or None.

    ``day_of_week`` is a value (e.g. 'Monday'), so it is passed with a ``?``
    placeholder -- never interpolated.
    """
    cur = conn.execute(
        "SELECT * FROM market_schedule WHERE day_of_week = ?",
        (day_of_week,),
    )
    return cur.fetchone()


# The only table names count_rows will accept. This set is hard-coded here and
# never comes from user input, so interpolating a member of it into the SQL is
# safe. A table name is an identifier, not a value, so it cannot be a ``?``
# placeholder -- this is the one allowed identifier interpolation (per the
# standing rules and design.md).
_COUNT_ROWS_WHITELIST = frozenset(
    {"users", "stocks", "holdings", "transactions", "market_schedule", "market_settings"}
)


def count_rows(conn, table):
    """Return the number of rows in a whitelisted table.

    ``table`` MUST be one of the hard-coded whitelist names; anything else
    raises ValueError before any SQL runs. Because a table name is an
    identifier (not a value) it cannot be a ``?`` placeholder, so the validated
    name is interpolated -- this is the single allowed identifier interpolation
    in the whole module. Used by the init-db summary and tests.
    """
    if table not in _COUNT_ROWS_WHITELIST:
        raise ValueError(f"count_rows: table {table!r} is not in the whitelist")
    # Safe: ``table`` is proven to be a member of the hard-coded whitelist above.
    cur = conn.execute(f"SELECT COUNT(*) AS n FROM {table}")
    row = cur.fetchone()
    return int(row["n"])

# ---------------------------------------------------------------------------
# Named INSERT helper functions for seeding (task 4.2)
#
# These are the dedicated write helpers the seed loader (seed.py) calls. All
# SQL stays here in app/db.py; seed.py never touches a cursor or writes SQL.
# Every value is passed with a ``?`` placeholder -- nothing is concatenated,
# f-string-interpolated, %-formatted, or .format()-ed into SQL.
#
# Money/price arguments are expected to arrive as Decimal (built from strings
# by the caller); the Decimal adapter registered above writes them as text.
# Timestamp arguments are expected to be ISO-8601 strings (now_utc().isoformat()
# at the caller) and market hour arguments as 'HH:MM:SS' strings. These helpers
# do not compute money and do not call now() -- the caller owns those values so
# the single clock source stays in app/clock.py.
#
# Seeding runs inside the caller's transaction; these helpers issue a single
# INSERT each and (where a generated id is useful) return the new row id.
# ---------------------------------------------------------------------------


def insert_user(
    conn,
    *,
    first_name,
    last_name,
    email,
    username,
    password_hash,
    user_type,
    cash_balance,
    is_active,
    created_at,
    updated_at,
):
    """Insert one ``users`` row and return its new ``user_id``.

    ``password_hash`` is the Werkzeug hash computed by the caller -- this
    function never sees or stores a plaintext password. ``cash_balance`` is a
    Decimal; ``created_at``/``updated_at`` are ISO-8601 strings.
    """
    cur = conn.execute(
        """
        INSERT INTO users (
            first_name, last_name, email, username, password_hash,
            user_type, cash_balance, is_active, created_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            first_name,
            last_name,
            email,
            username,
            password_hash,
            user_type,
            cash_balance,
            is_active,
            created_at,
            updated_at,
        ),
    )
    return cur.lastrowid


def insert_stock(
    conn,
    *,
    ticker_symbol,
    company_name,
    company_shares_outstanding,
    current_price,
    description,
    logo_filename,
    created_by,
    created_at,
    price_updated_at,
):
    """Insert one ``stocks`` row and return its new ``stock_id``.

    ``current_price`` is a Decimal; ``description``/``logo_filename`` may be
    None; ``created_by`` is the admin's ``user_id``; timestamps are ISO-8601
    strings.
    """
    cur = conn.execute(
        """
        INSERT INTO stocks (
            ticker_symbol, company_name, company_shares_outstanding,
            current_price, description, logo_filename, created_by,
            created_at, price_updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            ticker_symbol,
            company_name,
            company_shares_outstanding,
            current_price,
            description,
            logo_filename,
            created_by,
            created_at,
            price_updated_at,
        ),
    )
    return cur.lastrowid


def insert_market_schedule_row(
    conn,
    *,
    created_by_admin_id,
    day_of_week,
    market_open_time,
    market_close_time,
    is_holiday,
    created_at,
    updated_at,
):
    """Insert one ``market_schedule`` row (one weekday) and return its id.

    ``market_open_time``/``market_close_time`` are 'HH:MM:SS' strings;
    ``is_holiday`` is 0 or 1; timestamps are ISO-8601 strings.
    """
    cur = conn.execute(
        """
        INSERT INTO market_schedule (
            created_by_admin_id, day_of_week, market_open_time,
            market_close_time, is_holiday, created_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            created_by_admin_id,
            day_of_week,
            market_open_time,
            market_close_time,
            is_holiday,
            created_at,
            updated_at,
        ),
    )
    return cur.lastrowid


def insert_market_settings(
    conn,
    *,
    override_mode,
    updated_by,
    updated_at,
):
    """Insert the single ``market_settings`` row (setting_id is fixed at 1).

    The schema enforces ``setting_id = 1`` and allows exactly one row, so we
    insert ``setting_id = 1`` explicitly. ``updated_by`` may be None or an
    admin ``user_id``; ``updated_at`` is an ISO-8601 string.
    """
    conn.execute(
        """
        INSERT INTO market_settings (setting_id, override_mode, updated_by, updated_at)
        VALUES (?, ?, ?, ?)
        """,
        (1, override_mode, updated_by, updated_at),
    )

# ---------------------------------------------------------------------------
# Pending-buy write query function (task 6.7)
#
# This is the single INSERT the trading service calls to record a pending buy
# order (create_pending_buy). It is safe to call standalone -- one INSERT, no
# accompanying read/update -- and does not open its own transaction.
#
# Safety rules enforced here mirror the rest of the module:
#   - Every VALUE is passed with a ``?`` placeholder; nothing is concatenated,
#     f-string-interpolated, %-formatted, or .format()-ed into SQL.
#   - No money arithmetic happens in SQL. ``price_per_share`` and
#     ``total_amount`` arrive as Decimals already computed/validated in Python
#     by the caller and are written as text via the registered adapter.
#   - ``created_at`` arrives as an ISO-8601 string (the caller passes
#     ``now.isoformat()``); ``confirmed_at`` and ``executed_at`` are written as
#     NULL because a pending order has not yet been confirmed or executed.
# ---------------------------------------------------------------------------


def insert_pending_buy(
    conn,
    user_id,
    stock_id,
    quantity,
    price_per_share,
    total_amount,
    created_at,
):
    """Insert one pending ``buy`` transactions row and return its new id.

    Writes a single ``transactions`` row with ``transaction_type='buy'`` and
    ``status='pending'``, the quoted ``price_per_share`` and ``total_amount``
    (both Decimals, written as text via the registered adapter),
    ``created_at`` (an ISO-8601 string), and ``confirmed_at``/``executed_at``
    NULL (a pending order has not been confirmed or executed yet). Returns
    ``cursor.lastrowid`` -- the new ``transaction_id``.
    """
    cur = conn.execute(
        """
        INSERT INTO transactions (
            user_id, stock_id, transaction_type, quantity,
            price_per_share, total_amount, status,
            created_at, confirmed_at, executed_at
        )
        VALUES (?, ?, 'buy', ?, ?, ?, 'pending', ?, NULL, NULL)
        """,
        (
            user_id,
            stock_id,
            quantity,
            price_per_share,
            total_amount,
            created_at,
        ),
    )
    return cur.lastrowid

# ---------------------------------------------------------------------------
# Confirm-path write query functions (task 6.8)
#
# These are the writes used by the atomic buy-confirm algorithm (confirm_buy,
# task 6.10). Unless noted, they are designed to be called INSIDE an already
# open ``transaction()`` (the caller opens ``BEGIN IMMEDIATE``); they do NOT
# open their own transaction. ``delete_pending_transaction`` is also safe to
# call standalone for cancel.
#
# Safety rules enforced here:
#   - Every VALUE is passed with a ``?`` placeholder; nothing is concatenated,
#     f-string-interpolated, %-formatted, or .format()-ed into SQL.
#   - No money arithmetic happens in SQL. ``new_balance`` arrives as a Decimal
#     already computed in Python by the caller and is written as text via the
#     registered adapter. Holding quantity math (integer, not money) is also
#     done in Python in ``upsert_holding`` to keep a single clear seam.
#   - Timestamps arrive as ISO-8601 strings (the caller passes
#     ``now.isoformat()``); these helpers accept them as given and never call
#     a clock.
#   - The audit trail is immutable: ``guarded_confirm_update`` and
#     ``delete_pending_transaction`` are scoped to ``status='pending'`` so they
#     physically cannot touch a ``confirmed``/``executed`` row, and no function
#     here mutates the financial fields of an audited transaction.
# ---------------------------------------------------------------------------


def get_transaction_for_update(conn, transaction_id):
    """Re-read one transaction row by id inside the confirm transaction.

    The name reflects the design intent (a ``SELECT ... FOR UPDATE``-style
    re-read), but SQLite has no row-level ``FOR UPDATE``. The write
    serialization is provided by the surrounding ``BEGIN IMMEDIATE`` lock the
    caller already holds, so this is simply a ``SELECT`` of the current row.
    Returns the row, or None if there is no such transaction.
    """
    cur = conn.execute(
        "SELECT * FROM transactions WHERE transaction_id = ?",
        (transaction_id,),
    )
    return cur.fetchone()


def guarded_confirm_update(conn, transaction_id, confirmed_at):
    """Move a transaction from ``pending`` to ``confirmed`` and report the count.

    Runs ``UPDATE transactions SET status='confirmed', confirmed_at=? WHERE
    transaction_id=? AND status='pending'`` and returns the number of rows
    affected (``cursor.rowcount``). Because the WHERE clause requires
    ``status='pending'``, the update succeeds exactly once: the first call on a
    pending row returns 1; any later call (the row is now ``confirmed`` or has
    moved on to ``executed``) matches nothing and returns 0. This single-row
    guard is what blocks a concurrent double-submit from applying twice.

    ``confirmed_at`` is an ISO-8601 string supplied by the caller.
    """
    cur = conn.execute(
        """
        UPDATE transactions
        SET status = 'confirmed', confirmed_at = ?
        WHERE transaction_id = ? AND status = 'pending'
        """,
        (confirmed_at, transaction_id),
    )
    return cur.rowcount


def update_user_cash(conn, user_id, new_balance, updated_at):
    """Write a Python-computed new cash balance for one user.

    ``new_balance`` is a Decimal the caller already computed in Python (e.g.
    ``cash_balance - total_amount``); no arithmetic happens in SQL. It is
    written as text via the registered Decimal adapter. ``updated_at`` is an
    ISO-8601 string supplied by the caller.
    """
    conn.execute(
        "UPDATE users SET cash_balance = ?, updated_at = ? WHERE user_id = ?",
        (new_balance, updated_at, user_id),
    )


def upsert_holding(conn, user_id, stock_id, quantity, now):
    """Add ``quantity`` shares to the user's holding of a stock, or create it.

    If a holdings row already exists for ``(user_id, stock_id)``, read its
    current quantity, add ``quantity`` in Python (integer math, not money), and
    write the new total back with ``updated_at = now``. Otherwise insert a new
    holdings row with the given ``quantity`` and ``created_at = updated_at =
    now``. ``now`` is an ISO-8601 string supplied by the caller.

    Reading current-then-writing (rather than an arithmetic UPDATE in SQL)
    keeps a single, readable seam that the rollback test monkeypatches. Kept as
    one named function for exactly that reason.
    """
    existing = get_holding(conn, user_id, stock_id)
    if existing is None:
        conn.execute(
            """
            INSERT INTO holdings (user_id, stock_id, quantity, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (user_id, stock_id, quantity, now, now),
        )
    else:
        new_quantity = int(existing["quantity"]) + int(quantity)
        conn.execute(
            "UPDATE holdings SET quantity = ?, updated_at = ? WHERE user_id = ? AND stock_id = ?",
            (new_quantity, now, user_id, stock_id),
        )


def set_transaction_executed(conn, transaction_id, executed_at):
    """Set a transaction's status to ``executed`` and stamp ``executed_at``.

    Called on the happy path immediately after ``guarded_confirm_update`` has
    moved the row to ``confirmed``, completing the pending -> confirmed ->
    executed transition inside the same transaction. ``executed_at`` is an
    ISO-8601 string supplied by the caller.
    """
    conn.execute(
        "UPDATE transactions SET status = 'executed', executed_at = ? WHERE transaction_id = ?",
        (executed_at, transaction_id),
    )


def delete_pending_transaction(conn, transaction_id):
    """Delete a transaction row only if it is still ``pending``.

    Scoped to ``status='pending'`` so it can never delete an audited
    (``confirmed``/``executed``) transaction. Used standalone to cancel a
    pending order, and inside the confirm transaction on the price-changed path
    to discard the now-stale pending row.
    """
    conn.execute(
        "DELETE FROM transactions WHERE transaction_id = ? AND status = 'pending'",
        (transaction_id,),
    )


# ---------------------------------------------------------------------------
# Sell-path write query functions (Phase 4)
#
# These mirror the buy-path helpers. insert_pending_sell records a pending sell
# order; reduce_holding decreases a user's holding quantity and deletes the row
# when it reaches zero (shares then return to inventory automatically because
# availability is derived = company_shares_outstanding - SUM(holdings.quantity)).
# Same safety rules: ? placeholders only, no money math in SQL, timestamps are
# ISO-8601 strings supplied by the caller, and these run inside the caller's
# open transaction() on the confirm path.
# ---------------------------------------------------------------------------


def insert_pending_sell(
    conn,
    user_id,
    stock_id,
    quantity,
    price_per_share,
    total_amount,
    created_at,
):
    """Insert one pending ``sell`` transactions row and return its new id.

    Writes a single ``transactions`` row with ``transaction_type='sell'`` and
    ``status='pending'``, the quoted ``price_per_share`` and ``total_amount``
    (the proceeds, both Decimals written as text via the adapter),
    ``created_at`` (ISO-8601 string), and ``confirmed_at``/``executed_at`` NULL.
    Returns ``cursor.lastrowid`` -- the new ``transaction_id``.
    """
    cur = conn.execute(
        """
        INSERT INTO transactions (
            user_id, stock_id, transaction_type, quantity,
            price_per_share, total_amount, status,
            created_at, confirmed_at, executed_at
        )
        VALUES (?, ?, 'sell', ?, ?, ?, 'pending', ?, NULL, NULL)
        """,
        (user_id, stock_id, quantity, price_per_share, total_amount, created_at),
    )
    return cur.lastrowid


def reduce_holding(conn, user_id, stock_id, quantity, now):
    """Decrease a user's holding of a stock by ``quantity`` shares.

    Reads the current holding, subtracts ``quantity`` in Python (integer math,
    not money), and either updates the row with the new quantity and
    ``updated_at = now`` or, when the new quantity reaches 0, DELETES the
    holdings row entirely (SAD: shares return to inventory on a sale, which is
    automatic because availability is derived). Kept as one named function so
    the rollback test can monkeypatch it. ``now`` is an ISO-8601 string.

    The caller has already validated that the user owns at least ``quantity``
    shares, so a missing row or an over-sell is a programming error here.
    """
    existing = get_holding(conn, user_id, stock_id)
    if existing is None:
        raise ValueError("reduce_holding called with no existing holding")
    new_quantity = int(existing["quantity"]) - int(quantity)
    if new_quantity < 0:
        raise ValueError("reduce_holding would make quantity negative")
    if new_quantity == 0:
        conn.execute(
            "DELETE FROM holdings WHERE user_id = ? AND stock_id = ?",
            (user_id, stock_id),
        )
    else:
        conn.execute(
            "UPDATE holdings SET quantity = ?, updated_at = ? WHERE user_id = ? AND stock_id = ?",
            (new_quantity, now, user_id, stock_id),
        )
