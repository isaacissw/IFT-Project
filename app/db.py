"""Data-access module: the ONLY module permitted to contain SQL or open a
database connection.

This module talks to MySQL 8 through PyMySQL. It provides:

  * ``get_connection(database_url)`` -- opens a connection configured the way
    every rule requires (autocommit on so we control transactions ourselves,
    READ COMMITTED isolation, UTC session time zone, exact DECIMAL handling),
  * ``transaction(conn)`` -- the all-or-nothing transaction context manager
    (``START TRANSACTION`` ... ``COMMIT`` / ``ROLLBACK``),
  * ``init_db`` / ``drop_all_tables`` -- build or tear down the schema,
  * every named query function the services and routes use.

Routes and services never write SQL or touch a connection directly.

Two translation rules keep the rest of the app unchanged:

  * Timestamps. The services pass ISO-8601 strings
    (``now.isoformat()``, e.g. ``"2026-10-19T04:38:35.326977+00:00"``). MySQL
    stores them in ``DATETIME(6)`` columns as UTC. ``_ts()`` converts on the way
    in, and a result converter turns them back into the same ISO-8601 UTC
    string on the way out, so templates and services see what they always did.
  * Market hours (``TIME`` columns) are read back as ``"HH:MM:SS"`` strings.
"""

import os
from contextlib import contextmanager
from datetime import datetime, timezone
from urllib.parse import unquote, urlparse

import pymysql
from pymysql.constants import CLIENT, FIELD_TYPE
from pymysql.converters import conversions as _default_conversions

# Path to schema.sql, which lives at the project root (one level above app/).
# Resolving it from this file keeps init_db working no matter the current
# working directory.
_SCHEMA_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "schema.sql")


# ---------------------------------------------------------------------------
# Reading values back out of MySQL
# ---------------------------------------------------------------------------


def _decode_datetime(text):
    """Turn a MySQL DATETIME string into an ISO-8601 UTC string.

    MySQL gives ``"2026-10-19 04:38:35.326977"``. Everything we store is UTC,
    so we attach the UTC offset and return ``"2026-10-19T04:38:35.326977+00:00"``
    -- the same text the app has always handled.
    """
    return datetime.fromisoformat(text).replace(tzinfo=timezone.utc).isoformat()


def _decode_time(text):
    """Keep TIME columns as plain ``"HH:MM:SS"`` text (the market service parses it)."""
    return text


# PyMySQL's normal converters, with DATETIME/TIMESTAMP/TIME overridden as above.
# DECIMAL is left alone: PyMySQL already returns an exact ``decimal.Decimal``.
_CONVERSIONS = _default_conversions.copy()
_CONVERSIONS[FIELD_TYPE.DATETIME] = _decode_datetime
_CONVERSIONS[FIELD_TYPE.TIMESTAMP] = _decode_datetime
_CONVERSIONS[FIELD_TYPE.TIME] = _decode_time


def _ts(iso_text):
    """Convert an ISO-8601 timestamp string to a MySQL DATETIME(6) value (UTC).

    ``"2026-10-19T04:38:35.326977+00:00"`` -> ``"2026-10-19 04:38:35.326977"``.
    Any timezone offset is converted to UTC first, then dropped, because the
    column holds naive UTC.
    """
    dt = datetime.fromisoformat(iso_text)
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt.strftime("%Y-%m-%d %H:%M:%S.%f")


# ---------------------------------------------------------------------------
# Connection
# ---------------------------------------------------------------------------


class DuplicateError(Exception):
    """An INSERT/UPDATE broke a UNIQUE rule (e.g. the username or email already exists)."""


class Connection:
    """Thin wrapper around a PyMySQL connection.

    Its one job is to let every query in this module use ``?`` placeholders (the
    style the rest of the project already uses) while PyMySQL expects ``%s``.
    SQL text in this project never contains a literal ``?`` or ``%``, so a plain
    replace is safe. Values are ALWAYS passed as parameters, never formatted
    into the SQL text.
    """

    def __init__(self, raw):
        self._raw = raw

    def execute(self, sql, params=()):
        """Run one statement and return the cursor (fetchone/fetchall/rowcount/lastrowid).

        A "duplicate value for a UNIQUE column" error (MySQL error 1062) is
        re-raised as ``DuplicateError`` so services can react to it (for example
        two people signing up with the same username at the same moment) without
        importing the database driver.
        """
        cur = self._raw.cursor()
        try:
            cur.execute(sql.replace("?", "%s"), tuple(params))
        except pymysql.err.IntegrityError as exc:
            if exc.args and exc.args[0] == 1062:
                raise DuplicateError(str(exc)) from exc
            raise
        return cur

    def close(self):
        self._raw.close()


def parse_database_url(url):
    """Split ``mysql://user:password@host:port/dbname`` into its parts.

    The password is URL-decoded so special characters can be written as
    ``%40`` etc. in ``.env``. Raises ``ValueError`` for anything that is not a
    MySQL URL.
    """
    parsed = urlparse(url)
    if not (parsed.scheme or "").startswith("mysql"):
        raise ValueError("DATABASE_URL must start with 'mysql://'")
    database = (parsed.path or "").lstrip("/")
    if not database:
        raise ValueError("DATABASE_URL must name a database, e.g. mysql://user:pw@localhost:3306/stock_trading")
    return {
        "host": parsed.hostname or "localhost",
        "port": parsed.port or 3306,
        "user": unquote(parsed.username or ""),
        "password": unquote(parsed.password or ""),
        "database": database,
    }


def with_database_name(url, database):
    """Return ``url`` pointed at a different database name (used by the tests)."""
    parsed = urlparse(url)
    return parsed._replace(path="/" + database).geturl()


def get_connection(database_url):
    """Open a MySQL connection configured the way every rule requires.

    - ``autocommit=True`` so PyMySQL never opens its own hidden transaction; we
      control transactions explicitly with ``transaction()``.
    - READ COMMITTED isolation so each statement inside a transaction sees the
      latest committed data. Combined with ``SELECT ... FOR UPDATE`` row locks,
      this is what makes re-validation at commit time reliable.
    - UTC session time zone, exact ``Decimal`` for DECIMAL columns, and
      ``FOUND_ROWS`` so ``rowcount`` reports rows MATCHED by an UPDATE (the
      guarded confirm relies on that).
    - Rows come back as dicts, so ``row["column"]`` works everywhere.
    """
    p = parse_database_url(database_url)
    raw = pymysql.connect(
        host=p["host"],
        port=p["port"],
        user=p["user"],
        password=p["password"],
        database=p["database"],
        charset="utf8mb4",
        cursorclass=pymysql.cursors.DictCursor,
        autocommit=True,
        client_flag=CLIENT.FOUND_ROWS,
        conv=_CONVERSIONS,
        init_command="SET time_zone = '+00:00', SESSION transaction_isolation = 'READ-COMMITTED'",
    )
    return Connection(raw)


@contextmanager
def transaction(conn):
    """Run a block inside one all-or-nothing transaction.

    ``START TRANSACTION`` opens it; on a clean exit we COMMIT, and on any
    exception we ROLLBACK and re-raise, so a partially applied change is never
    left behind. Code inside should take row locks with the ``for_update=True``
    readers (or ``get_transaction_for_update``) so two requests cannot
    interleave on the same rows.
    """
    conn.execute("START TRANSACTION")
    try:
        yield conn
        conn.execute("COMMIT")
    except Exception as exc:
        # Normally any error undoes everything. One exception: an error that
        # sets ``keep_changes = True`` (PriceChangedError) means "the work done
        # so far, deleting the stale pending order, is intentional, so save it
        # and then report the error".
        if getattr(exc, "keep_changes", False):
            conn.execute("COMMIT")
        else:
            conn.execute("ROLLBACK")
        raise


# ---------------------------------------------------------------------------
# Schema setup / teardown
# ---------------------------------------------------------------------------

# Fixed, hard-coded statements (children before parents). Nothing here is built
# from input, so there is no string formatting anywhere in the SQL.
_DROP_STATEMENTS = (
    "DROP TABLE IF EXISTS transactions",
    "DROP TABLE IF EXISTS holdings",
    "DROP TABLE IF EXISTS market_schedule",
    "DROP TABLE IF EXISTS market_settings",
    "DROP TABLE IF EXISTS stocks",
    "DROP TABLE IF EXISTS users",
)

_CLEAR_STATEMENTS = (
    "DELETE FROM transactions",
    "DELETE FROM holdings",
    "DELETE FROM market_schedule",
    "DELETE FROM market_settings",
    "DELETE FROM stocks",
    "DELETE FROM users",
)


def init_db(conn):
    """Create the schema by running every statement in schema.sql.

    schema.sql is a trusted, checked-in file (not user input). PyMySQL runs one
    statement at a time, so the file is split on ``;`` and each piece is run.
    """
    with open(_SCHEMA_PATH, "r", encoding="utf-8") as f:
        schema_sql = f.read()
    for statement in schema_sql.split(";"):
        if statement.strip():
            conn.execute(statement)


def drop_all_tables(conn):
    """Drop every project table (foreign-key checks are paused while doing it)."""
    conn.execute("SET FOREIGN_KEY_CHECKS = 0")
    try:
        for statement in _DROP_STATEMENTS:
            conn.execute(statement)
    finally:
        conn.execute("SET FOREIGN_KEY_CHECKS = 1")


def clear_all_rows(conn):
    """Delete every row from every table, keeping the tables (used by the tests)."""
    for statement in _CLEAR_STATEMENTS:
        conn.execute(statement)

# ---------------------------------------------------------------------------
# READ functions: look things up, change nothing.
#
# How every query in this file protects against SQL injection:
#   The SQL text is always a fixed string with ``?`` marks where values go, and
#   the values are handed over separately as a tuple, e.g.
#       conn.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
#   The database driver inserts each value safely, so even text like
#   "1; DROP TABLE users" is treated as plain data, never as SQL. We NEVER glue
#   values into the SQL text ourselves (no f-strings, no +, no % formatting).
#
# Money columns are DECIMAL(10,2) in MySQL and come back as Python Decimal
# objects, which are exact (no floating-point rounding errors).
# ---------------------------------------------------------------------------


def get_user_by_id(conn, user_id, for_update=False):
    """Return one user row by id, or None if there is no such user.

    ``for_update=True`` adds ``FOR UPDATE`` so the row is locked until the
    surrounding transaction ends. The confirm flows use it on the user's row so
    two requests cannot spend the same cash at the same time. (Two fixed SQL
    strings rather than building one, so no SQL is ever assembled from pieces.)
    """
    if for_update:
        cur = conn.execute(
            "SELECT * FROM users WHERE user_id = ? FOR UPDATE",
            (user_id,),
        )
    else:
        cur = conn.execute(
            "SELECT * FROM users WHERE user_id = ?",
            (user_id,),
        )
    return cur.fetchone()


def get_user_by_username(conn, username):
    """Return one user row by username, or None. Used by login and signup.

    MySQL compares usernames case-insensitively, so "Trainee1" finds "trainee1".
    """
    cur = conn.execute(
        "SELECT * FROM users WHERE username = ?",
        (username,),
    )
    return cur.fetchone()


def get_user_by_email(conn, email):
    """Return one user row by email, or None. (MySQL compares emails case-insensitively.)"""
    cur = conn.execute(
        "SELECT * FROM users WHERE email = ?",
        (email,),
    )
    return cur.fetchone()


def get_stock(conn, stock_id, for_update=False):
    """Return one stock row by id, or None if there is no such stock.

    ``for_update=True`` locks the stock row until the transaction ends. Every
    buy/sell confirm locks the stock first, so confirms on the same stock run
    one at a time and the shares-available check cannot go stale mid-commit.
    """
    if for_update:
        cur = conn.execute(
            "SELECT * FROM stocks WHERE stock_id = ? FOR UPDATE",
            (stock_id,),
        )
    else:
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

    "Shares available to buy" is never stored; it is always worked out as
    company_shares_outstanding - (this total). Buying moves shares out of the
    available pool and selling puts them back automatically. SUM() gives NULL
    when nobody holds the stock, so COALESCE turns that into 0.
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


# The only table names count_rows will accept. A table NAME cannot be a ``?``
# placeholder (placeholders are only for values), so count_rows has to put the
# name into the SQL text itself. That is safe ONLY because the name must first
# match this fixed list, which never comes from user input. It is the one place
# in the project where anything is inserted into SQL text, and the SQL-safety
# test (tests/test_sql_safety.py) allows exactly this one exception.
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
# INSERT functions for new users, stocks and market settings.
#
# Used by seed.py (the demo data), by signup (insert_user) and by the admin
# "Create New Stock" form (insert_stock).
#
# What the callers pass in:
#   * money/prices as Decimal (exact),
#   * timestamps as ISO-8601 text such as "2026-03-02T15:00:00+00:00";
#     _ts() converts them to the format MySQL stores,
#   * market hours as "HH:MM:SS" text.
# These functions never do money math and never read the clock themselves;
# the caller decides those values. Each one runs a single INSERT and returns
# the new row's id where that is useful.
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
            _ts(created_at),
            _ts(updated_at),
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
            _ts(created_at),
            _ts(price_updated_at),
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
            _ts(created_at),
            _ts(updated_at),
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
        (1, override_mode, updated_by, _ts(updated_at)),
    )

# ---------------------------------------------------------------------------
# Every order goes through three statuses in the transactions table:
#
#   pending   -> written when the customer clicks "Review Order". Nothing has
#                happened to their money or shares yet. Cancel deletes it.
#   confirmed -> set the moment they click "Confirm Transaction".
#   executed  -> set right after, once cash and shares have actually moved.
#
# confirmed and executed happen inside the same database transaction, so from
# the outside an order is either still pending or fully done. Once a row is
# confirmed/executed nothing in this file can change or delete it (the SAD's
# "confirmed transactions cannot be cancelled" rule).
#
# insert_pending_buy is step 1 for a purchase: one INSERT, no money moved.
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
    ``status='pending'``, the reviewed ``price_per_share`` and ``total_amount``
    (Decimals), ``created_at``, and ``confirmed_at``/``executed_at`` left empty
    (NULL) because nothing has been confirmed yet. Returns the new
    ``transaction_id`` (MySQL's auto-numbered id, via ``cursor.lastrowid``).
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
            _ts(created_at),
        ),
    )
    return cur.lastrowid

# ---------------------------------------------------------------------------
# CONFIRM functions: the writes that actually move money and shares.
#
# The services (trading.py, cash.py) call these INSIDE one ``transaction()``,
# so they all succeed together or are all undone together. They do not start
# their own transaction.
#
# Rules they follow:
#   * The new cash balance is calculated in Python with Decimal and then
#     written; SQL never does the arithmetic. (Exact, and easy to read.)
#   * guarded_confirm_update and delete_pending_transaction only ever touch
#     rows that are still 'pending', so a confirmed/executed transaction can
#     never be changed or deleted by mistake.
#   * delete_pending_transaction is also used on its own by "Cancel".
# ---------------------------------------------------------------------------


def get_transaction_for_update(conn, transaction_id):
    """Re-read one transaction row by id inside the confirm transaction.

    A real ``SELECT ... FOR UPDATE``: the row stays locked until the
    surrounding transaction commits or rolls back, so a second, simultaneous
    confirm of the same order has to wait and then sees the final status.
    Returns the row, or None if there is no such transaction.
    """
    cur = conn.execute(
        "SELECT * FROM transactions WHERE transaction_id = ? FOR UPDATE",
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
        (_ts(confirmed_at), transaction_id),
    )
    return cur.rowcount


def update_user_cash(conn, user_id, new_balance, updated_at):
    """Write a Python-computed new cash balance for one user.

    ``new_balance`` is a Decimal the caller already computed in Python (e.g.
    ``cash_balance - total_amount``); no arithmetic happens in SQL.
    ``updated_at`` is an ISO-8601 string supplied by the caller.
    """
    conn.execute(
        "UPDATE users SET cash_balance = ?, updated_at = ? WHERE user_id = ?",
        (new_balance, _ts(updated_at), user_id),
    )


def upsert_holding(conn, user_id, stock_id, quantity, now):
    """Add ``quantity`` shares to the user's holding of a stock, or create it.

    If a holdings row already exists for ``(user_id, stock_id)``, read its
    current quantity, add ``quantity`` in Python (integer math, not money), and
    write the new total back with ``updated_at = now``. Otherwise insert a new
    holdings row with the given ``quantity`` and ``created_at = updated_at =
    now``. ``now`` is an ISO-8601 string supplied by the caller.

    ("Upsert" = update if it exists, insert if it doesn't.) The rollback test
    deliberately breaks this function mid-purchase to prove the whole purchase
    is undone, which is why it is kept as one separate named function.
    """
    now = _ts(now)
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
        (_ts(executed_at), transaction_id),
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
# SELL functions: the mirror image of the buy functions above.
#
# insert_pending_sell records the reviewed sell order (status 'pending').
# reduce_holding takes shares out of the customer's holding; when they sell
# their last share the holding row is deleted. The shares go back into the
# "available to buy" pool automatically, because that number is always
# calculated as shares_outstanding minus everything customers hold.
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
    (the proceeds, both Decimals),
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
        (user_id, stock_id, quantity, price_per_share, total_amount, _ts(created_at)),
    )
    return cur.lastrowid


# ---------------------------------------------------------------------------
# Cash deposit / withdrawal write function
#
# Cash transactions reuse the same pending -> confirmed -> executed life cycle
# as trades, and the same confirm-path helpers above (get_transaction_for_update,
# guarded_confirm_update, update_user_cash, set_transaction_executed,
# delete_pending_transaction). Only the INSERT is different: a cash row has no
# stock, quantity or price (the table's CHECK constraint requires them NULL).
# ---------------------------------------------------------------------------


def insert_pending_cash(conn, user_id, transaction_type, amount, created_at):
    """Insert one pending ``deposit`` or ``withdrawal`` row and return its new id.

    ``transaction_type`` is 'deposit' or 'withdrawal' (passed as a value, so it
    is a ``?`` placeholder like everything else). ``amount`` is a Decimal
    already validated in Python; ``created_at`` is an ISO-8601 string.
    """
    cur = conn.execute(
        """
        INSERT INTO transactions (
            user_id, stock_id, transaction_type, quantity,
            price_per_share, total_amount, status,
            created_at, confirmed_at, executed_at
        )
        VALUES (?, NULL, ?, NULL, NULL, ?, 'pending', ?, NULL, NULL)
        """,
        (user_id, transaction_type, amount, _ts(created_at)),
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
    now = _ts(now)
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


# ---------------------------------------------------------------------------
# Administrator functions: stock inventory and market controls
#
# Note what is NOT here: nothing in this section can change a stock's price.
# The SAD says an administrator sets the price only when the stock is created
# (insert_stock); after that only the system's price generator changes it.
# ---------------------------------------------------------------------------


def get_stock_by_ticker(conn, ticker_symbol):
    """Return one stock row by ticker symbol, or None."""
    cur = conn.execute(
        "SELECT * FROM stocks WHERE ticker_symbol = ?",
        (ticker_symbol,),
    )
    return cur.fetchone()


def update_stock_details(conn, stock_id, company_name, description, shares_outstanding):
    """Change a stock's editable details. There is deliberately no price parameter.

    ``description`` may be None. The ticker symbol and the price are never
    changed here.
    """
    conn.execute(
        """
        UPDATE stocks
        SET company_name = ?, description = ?, company_shares_outstanding = ?
        WHERE stock_id = ?
        """,
        (company_name, description, shares_outstanding, stock_id),
    )


def list_market_schedule(conn):
    """Return the seven market_schedule rows in calendar order (Monday first)."""
    cur = conn.execute(
        """
        SELECT * FROM market_schedule
        ORDER BY FIELD(day_of_week, 'Monday', 'Tuesday', 'Wednesday', 'Thursday',
                       'Friday', 'Saturday', 'Sunday')
        """
    )
    return cur.fetchall()


def set_day_hours(conn, day_of_week, open_time, close_time, updated_at):
    """Open a weekday between two 'HH:MM:SS' times (and clear its closed-all-day flag)."""
    conn.execute(
        """
        UPDATE market_schedule
        SET market_open_time = ?, market_close_time = ?, is_holiday = 0, updated_at = ?
        WHERE day_of_week = ?
        """,
        (open_time, close_time, _ts(updated_at), day_of_week),
    )


def set_day_closed(conn, day_of_week, updated_at):
    """Mark a weekday closed all day (is_holiday = 1), keeping its stored hours."""
    conn.execute(
        "UPDATE market_schedule SET is_holiday = 1, updated_at = ? WHERE day_of_week = ?",
        (_ts(updated_at), day_of_week),
    )


def set_market_override(conn, override_mode, updated_by, updated_at):
    """Set the manual market override: 'none', 'force_open' or 'force_closed'."""
    conn.execute(
        "UPDATE market_settings SET override_mode = ?, updated_by = ?, updated_at = ? WHERE setting_id = ?",
        (override_mode, updated_by, _ts(updated_at), 1),
    )


# ---------------------------------------------------------------------------
# Price generator: the ONE function that changes a stock's price after creation
#
# The SAD says an administrator sets only the INITIAL price (insert_stock) and
# afterwards only the system's price generator moves it. This is that single
# writer. tests/test_price_rules.py fails if any other SQL changes
# stocks.current_price, or if anything but app/services/pricing.py calls this.
# ---------------------------------------------------------------------------


def update_stock_price(conn, stock_id, new_price, updated_at):
    """Set a stock's new market price (a Decimal computed in Python) and stamp when it changed."""
    conn.execute(
        "UPDATE stocks SET current_price = ?, price_updated_at = ? WHERE stock_id = ?",
        (new_price, _ts(updated_at), stock_id),
    )