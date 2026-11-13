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


def _positive_int(name, default):
    """Read a whole-number setting from the environment, with a clear error if it is not one."""
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        value = int(raw)
    except ValueError:
        value = 0
    if value < 1:
        raise RuntimeError(f"{name} must be a whole number of 1 or more (got {raw!r}).")
    return value


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

    # Time zone used only for displaying timestamps. Defaults to US Eastern,
    # matching the SAD wireframe. Interpreted with zoneinfo.
    MARKET_TIMEZONE = os.environ.get("MARKET_TIMEZONE", "America/New_York")

    # Price generator: seconds between price updates, and the largest single
    # move in basis points (50 = 0.5%). Both optional.
    PRICE_TICK_SECONDS = _positive_int("PRICE_TICK_SECONDS", 60)
    PRICE_MAX_STEP_BPS = _positive_int("PRICE_MAX_STEP_BPS", 50)

    # Seed passwords, read by seed.py and stored only as Werkzeug hashes.
    SEED_ADMIN_PASSWORD = os.environ.get("SEED_ADMIN_PASSWORD")
    SEED_CUSTOMER_PASSWORD = os.environ.get("SEED_CUSTOMER_PASSWORD")
