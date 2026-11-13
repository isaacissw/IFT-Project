# Stock Trading System

A simulated stock-market web application for training new stockbrokers (IFT 401).
Customers buy and sell simulated stocks with a review-then-confirm flow; all money and
stocks are simulated. Built with Flask, Jinja2 and MySQL 8.

Currently built: signup, login and logout (customer and administrator roles), buy and sell
flows, cash deposits and withdrawals with a Financial Records page (balance and transaction
history), administrator tools (create/edit stocks, weekly market hours, manual
open/close override), dashboard, market-hours check, and the full database schema. See `docs/` for the SAD and the project rules.

## Requirements

- Python 3.14
- MySQL 8 (Server running on your machine) and, optionally, MySQL Workbench
- The pinned dependencies in `requirements.txt`

## Setup

1. Create and activate a virtual environment, then install the dependencies.

   ```powershell
   python -m venv .venv
   .\.venv\Scripts\Activate.ps1
   pip install -r requirements.txt
   ```

2. Create the databases and a database user (once). In MySQL Workbench, open a query tab
   and run (choose your own password):

   ```sql
   CREATE DATABASE stock_trading;
   CREATE DATABASE stock_trading_test;
   CREATE USER 'stock_app'@'localhost' IDENTIFIED BY 'your-db-password';
   GRANT ALL ON stock_trading.* TO 'stock_app'@'localhost';
   GRANT ALL ON stock_trading_test.* TO 'stock_app'@'localhost';
   ```

   `stock_trading_test` is used only by the automated tests, so they never touch your
   real data.

3. Copy `.env.example` to `.env` and fill it in. Never commit `.env`.

   ```
   DATABASE_URL=mysql://stock_app:your-db-password@localhost:3306/stock_trading
   ```

   If your password contains `@`, `:` or `/`, write it URL-encoded (`%40`, `%3A`, `%2F`).
   Also set `SECRET_KEY`, `SEED_ADMIN_PASSWORD` and `SEED_CUSTOMER_PASSWORD`.

## Database initialization

Drops every project table, recreates it from `schema.sql`, loads the demo data and prints
row counts:

```powershell
flask --app app init-db
```

## Run

```powershell
flask --app app run
```

Open http://127.0.0.1:5000. You will be sent to the login page. `init-db` creates two demo
accounts: the customer `trainee1` (password = `SEED_CUSTOMER_PASSWORD` from your `.env`) and the
administrator `admin` (password = `SEED_ADMIN_PASSWORD`). New customers can also sign up at
`/signup`; they start with $0.00.

## Test

```powershell
pytest -q
```

## Project layout

- `app/db.py` — the only module that contains SQL
- `app/services/` — business rules (no Flask imports)
- `app/routes/` — thin Flask blueprints
- `schema.sql` — MySQL schema, `seed.py` — demo data
- `docs/` — SAD, project rules, verification record
