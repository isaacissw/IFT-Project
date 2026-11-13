"""Shared pytest fixtures for the test suite.

  - ``db_conn`` -- a connection to the MySQL *test* database, emptied and then
    seeded with the known demo data for every test, so tests are isolated and
    start from the same state.
  - ``app`` / ``client`` -- a Flask app from ``create_app()`` pointed at that
    same test database (seeded), with CSRF **enabled** by default, and the
    Flask test client for route-level tests.

The test database is separate from the development one (see
``tests/dbhelpers.py``); the tests can never touch your real data.

Seeding reads ``SEED_ADMIN_PASSWORD`` and ``SEED_CUSTOMER_PASSWORD`` from the
environment. The fixtures set obviously-fake throwaway values for those so
tests never depend on a real ``.env`` secret.

Services receive ``now`` as an argument (see ``app/clock.py``), so tests pass an
explicit ``now`` and never depend on the wall clock.
"""

import pytest
from hypothesis import settings

from app import db
from dbhelpers import ADMIN_PASSWORD, CUSTOMER_PASSWORD, fresh_db, test_database_url

# Hypothesis normally fails an example that takes over 200 ms. A real MySQL
# database is slower than the old in-memory one, so a slow example is not a bug.
# Turn the time limit off for the whole suite.
settings.register_profile("mysql", deadline=None)
settings.load_profile("mysql")

# Obviously-fake, non-secret placeholder passwords used only so ``seed_into``
# can compute a Werkzeug hash for the two demo users during tests.
_FAKE_SEED_ADMIN_PASSWORD = ADMIN_PASSWORD
_FAKE_SEED_CUSTOMER_PASSWORD = CUSTOMER_PASSWORD


def _set_fake_seed_passwords(monkeypatch):
    """Point the seed password env vars at safe, fake dev values for a test."""
    monkeypatch.setenv("SEED_ADMIN_PASSWORD", _FAKE_SEED_ADMIN_PASSWORD)
    monkeypatch.setenv("SEED_CUSTOMER_PASSWORD", _FAKE_SEED_CUSTOMER_PASSWORD)


@pytest.fixture
def db_conn(monkeypatch):
    """Yield a connection to the MySQL test database, freshly emptied and seeded."""
    _set_fake_seed_passwords(monkeypatch)
    with fresh_db(seed=True) as conn:
        yield conn


@pytest.fixture
def app(monkeypatch, tmp_path):
    """A Flask app from ``create_app()`` pointed at the seeded MySQL test database.

    CSRF stays ENABLED by default -- only a specific test that needs to bypass
    it disables it, so the default ``client`` is CSRF-enabled.
    """
    _set_fake_seed_passwords(monkeypatch)

    # Reset and seed the test database, then let the app open its own
    # connections to it for each request.
    with fresh_db(seed=True):
        pass

    database_url = test_database_url()
    monkeypatch.setenv("DATABASE_URL", database_url)
    # A throwaway signing key so the CSRF-enabled test client works without a
    # real .env. Not a secret.
    monkeypatch.setenv("SECRET_KEY", "test-secret-key-not-real")

    # The seeded market is open weekdays 9-5 New York time, so route tests must
    # not depend on the real clock. Freeze "now" at Monday 10:00 AM New York time.
    from datetime import datetime, timezone

    frozen_now = datetime(2026, 3, 2, 15, 0, 0, tzinfo=timezone.utc)
    monkeypatch.setattr("app.routes.trading.now_utc", lambda: frozen_now)

    from app import create_app

    flask_app = create_app()
    flask_app.config.update(
        TESTING=True,
        # CSRF is ENABLED by default (WTF_CSRF_ENABLED left True).
        DATABASE_URL=database_url,
        # Uploaded logos go to a throwaway folder, never the real instance/ one.
        UPLOAD_FOLDER=str(tmp_path / "logos"),
    )
    (tmp_path / "logos").mkdir(exist_ok=True)

    return flask_app


def _client_logged_in_as(app, username):
    """A test client whose session already says it is ``username`` (skips the slow password check).

    The real login form is tested in tests/test_auth.py; the other route tests just need
    to be "someone who is already logged in".
    """
    conn = db.get_connection(test_database_url())
    try:
        user_id = db.get_user_by_username(conn, username)["user_id"]
    finally:
        conn.close()
    test_client = app.test_client()
    with test_client.session_transaction() as session:
        session["user_id"] = user_id
    return test_client


@pytest.fixture
def client(app):
    """A CSRF-enabled test client already logged in as the demo customer ``trainee1``."""
    return _client_logged_in_as(app, "trainee1")


@pytest.fixture
def admin_client(app):
    """A CSRF-enabled test client already logged in as the administrator."""
    return _client_logged_in_as(app, "admin")


@pytest.fixture
def anon_client(app):
    """A CSRF-enabled test client that is NOT logged in."""
    return app.test_client()
