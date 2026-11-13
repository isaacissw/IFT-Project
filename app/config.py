"""Application configuration.

Reads all settings from environment variables so that secrets never live in
code (the standing project rules require SECRET_KEY, DB credentials, and seed
passwords to come from the environment). In development, a local ``.env`` file
is loaded via python-dotenv; in production the real environment supplies the
values.
"""

import os

# Load variables from a local .env file in development. In production the real
# process environment already holds these values, and load_dotenv() simply does
# nothing when no .env file is present, so this call is always safe.
from dotenv import load_dotenv

load_dotenv()


class Config:
    """Flask configuration object built from environment variables.

    Attribute names match Flask's expected config keys where relevant
    (SECRET_KEY). The remaining values are read by app code (db.py, the seed
    loader, the market service, and the demo-user resolver).
    """

    # Where the MySQL database lives, as mysql://user:password@host:port/dbname.
    # The real value (with the password) belongs in .env, never in code. The
    # default below has no password, so it only works for a passwordless local
    # user. The URL is parsed inside app/db.py.
    DATABASE_URL = os.environ.get(
        "DATABASE_URL", "mysql://stock_app@localhost:3306/stock_trading"
    )

    # Flask session signing key. No default is provided: a real value must be
    # supplied via the environment (see .env.example for a placeholder).
    SECRET_KEY = os.environ.get("SECRET_KEY")

    # Fixed demo customer identity for Phase 1 (no login yet). When unset, the
    # app resolves the demo user by looking up username "trainee1" at startup.
    DEMO_USER_ID = os.environ.get("DEMO_USER_ID")

    # Time zone used only for displaying timestamps. Defaults to US Eastern,
    # matching the SAD wireframe. Interpreted with zoneinfo.
    MARKET_TIMEZONE = os.environ.get("MARKET_TIMEZONE", "America/New_York")

    # Seed passwords, read by seed.py and stored only as Werkzeug hashes.
    SEED_ADMIN_PASSWORD = os.environ.get("SEED_ADMIN_PASSWORD")
    SEED_CUSTOMER_PASSWORD = os.environ.get("SEED_CUSTOMER_PASSWORD")
