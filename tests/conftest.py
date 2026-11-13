"""Shared pytest fixtures for the Phase 1 test suite.

This is the test harness foundation (tasks 3.3 and 4.4). It provides:

  - ``db_conn`` — a connection to a *fresh*, fully **seeded** SQLite database
    built from ``schema.sql`` plus ``seed_into(conn)`` for every test, so tests
    are isolated and start from the known demo data.
  - ``app`` / ``client`` — a Flask app from ``create_app()`` pointed at its own
    per-test temp database (also schema + seed), with CSRF **enabled** by
    default, and the Flask test client for route-level tests.

Seeding reads ``SEED_ADMIN_PASSWORD`` and ``SEED_CUSTOMER_PASSWORD`` from the
environment (see ``seed.py``). Tests must never depend on a real ``.env`` or a
real secret, so the fixtures set obviously-fake dev placeholder values for
those variables before seeding. These are throwaway values used only to produce
a Werkzeug hash for the two demo users; they are not real credentials.

Services receive ``now`` as an argument (see ``app/clock.py`` and the design's
"Clock injection" note), so no clock wiring is needed here beyond leaving room
for property/unit tests to pass an explicit ``now``.
"""

import pytest

from app import db


# Obviously-fake, non-secret placeholder passwords used only so ``seed_into``
# can compute a Werkzeug hash for the two demo users during tests. NEVER real
# secrets — these exist only to make seeding deterministic without a real .env.
_FAKE_SEED_ADMIN_PASSWORD = "test-admin-password-not-real"
_FAKE_SEED_CUSTOMER_PASSWORD = "test-customer-password-not-real"


def _set_fake_seed_passwords(monkeypatch):
    """Point the seed password env vars at safe, fake dev values for a test.

    ``seed_into`` reads these two variables and refuses to invent a default, so
    they must be present for seeding to work. We set fake throwaway values via
    monkeypatch so they are scoped to the test and never leak real secrets.
    """
    monkeypatch.setenv("SEED_ADMIN_PASSWORD", _FAKE_SEED_ADMIN_PASSWORD)
    monkeypatch.setenv("SEED_CUSTOMER_PASSWORD", _FAKE_SEED_CUSTOMER_PASSWORD)


def _build_seeded_db(db_path):
    """Open ``db_path``, build the schema, seed demo data, and return the conn.

    All database access goes through ``app.db`` (schema via ``init_db``) and the
    ``seed_into`` helper, which itself only calls ``app.db`` insert helpers — no
    raw SQL lives in this fixture module.
    """
    # Imported lazily so the module imports cleanly even while seed.py is being
    # developed; by task 4.4 seed_into exists.
    from seed import seed_into

    conn = db.get_connection(str(db_path))
    db.init_db(conn)
    seed_into(conn)
    return conn


@pytest.fixture
def db_conn(tmp_path, monkeypatch):
    """Yield a connection to a fresh, **seeded** SQLite database.

    Each test gets its own temp database file (via pytest's ``tmp_path``), built
    from ``schema.sql`` (through ``app.db.init_db``) and then populated with the
    known demo data via ``seed_into(conn)``. Fully isolated per test.
    """
    _set_fake_seed_passwords(monkeypatch)

    db_path = tmp_path / "test_stock_trading.db"
    conn = _build_seeded_db(db_path)
    try:
        yield conn
    finally:
        conn.close()


@pytest.fixture
def app(tmp_path, monkeypatch):
    """A Flask app from ``create_app()`` pointed at a seeded per-test temp DB.

    The app gets its OWN temp database file (a real file, not ``:memory:``, so
    later route tests can hit a seeded DB across requests). We build and seed it
    the same way as ``db_conn``, then point the app at it via ``DATABASE_URL``
    before ``create_app()`` reads config. CSRF stays ENABLED by default — the
    design says only a specific test that needs to bypass CSRF disables it, so
    the default ``client`` is CSRF-enabled.
    """
    _set_fake_seed_passwords(monkeypatch)

    # The app's database lives in its own file so it is independent of db_conn.
    app_db_path = tmp_path / "app_stock_trading.db"

    # Build and seed the app's database up front, then close our setup
    # connection; the app opens its own connections per request.
    conn = _build_seeded_db(app_db_path)
    conn.close()

    # Point the app's config at this temp DB before create_app() reads it.
    # db_path_from_url strips the "sqlite:///" prefix, so round-trip the path.
    database_url = "sqlite:///" + str(app_db_path).replace("\\", "/")
    monkeypatch.setenv("DATABASE_URL", database_url)
    # A throwaway signing key so the CSRF-enabled test client works without a
    # real .env. Not a secret.
    monkeypatch.setenv("SECRET_KEY", "test-secret-key-not-real")

    from app import create_app

    flask_app = create_app()
    flask_app.config.update(
        TESTING=True,
        # CSRF is ENABLED by default (WTF_CSRF_ENABLED left True). A variant
        # fixture/test disables it only where a test specifically needs to.
        DATABASE_URL=database_url,
        DATABASE_PATH=str(app_db_path),
    )

    return flask_app


@pytest.fixture
def client(app):
    """The Flask test client for the CSRF-enabled ``app``."""
    return app.test_client()
