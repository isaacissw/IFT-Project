# Stock Trading System — Phase 1 (Buy a Stock)

A simulated stock-market web application. Phase 1 covers the local **Buy a Stock**
capability: one seeded demo customer can view stocks, enter a whole number of shares,
review a confirmation screen, and confirm a purchase that atomically debits cash,
adds holdings, and records a timestamped transaction.

This phase runs **locally only** against a SQLite database at `instance/stock_trading.db`.
There is no login and nothing runs in the cloud.

> Note: setup and run commands below are the Phase 1 working instructions and will be
> finalized in task 10.2.

## Requirements

- Python 3.14 (developed and verified on 3.14.7)
- The pinned dependencies in `requirements.txt`

## Setup

Create and activate a virtual environment, then install the pinned dependencies.

Windows (PowerShell):

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

macOS / Linux:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Copy the example environment file and adjust values as needed. Never commit `.env`.

```bash
cp .env.example .env
```

Set at least `SECRET_KEY`, `SEED_ADMIN_PASSWORD`, and `SEED_CUSTOMER_PASSWORD` in `.env`
before initializing the database. The placeholders in `.env.example` are intentionally
fake (for example `change-me-in-dev`) and must be replaced for real use.

## Database initialization

Drop and recreate the database from `schema.sql`, load the demo seed data, and print a
per-table row summary:

```bash
flask --app app init-db
```

## Run

```bash
flask --app app run
```

The app acts as the fixed demo customer `trainee1`.

## Test

```bash
pytest
```

## Pinned dependency versions (Python 3.14)

The exact versions installed and confirmed to install cleanly on Python 3.14:

| Package | Version |
| --- | --- |
| Flask | 3.1.2 |
| Werkzeug | 3.1.3 |
| Flask-WTF | 1.2.2 |
| python-dotenv | 1.1.1 |
| pytest | 8.4.2 |
| hypothesis | 6.140.3 |

`hypothesis` is pinned because the property-based tests depend on it (the pin stays
even if those optional tests are deferred). Flask pulls in Jinja2, click,
itsdangerous, and MarkupSafe transitively; those resolve to 3.14-compatible versions
automatically.
