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

from dbhelpers import fresh_db, test_database_url

# Hypothesis normally fails an example that takes over 200 ms. A real MySQL
# database is slower than the old in-memory one, so a slow example is not a bug.
# Turn the time limit off for the whole suite.
settings.register_profile("mysql", deadline=None)
settings.load_profile("mysql")

# Obviously-fake, non-secret placeholder passwords used only so ``seed_into``
# can compute a Werkzeug hash for the two demo users during tests.
_FAKE_SEED_ADMIN_PASSWORD = "test-admin-password-not-real"
_FAKE_SEED_CUSTOMER_PASSWORD = "test-customer-password-not-real"


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
def app(monkeypatch):
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

    from app import create_app

    flask_app = create_app()
    flask_app.config.update(
        TESTING=True,
        # CSRF is ENABLED by default (WTF_CSRF_ENABLED left True).
        DATABASE_URL=database_url,
    )

    return flask_app


@pytest.fixture
def client(app):
    """The Flask test client for the CSRF-enabled ``app``."""
    return app.test_client()
