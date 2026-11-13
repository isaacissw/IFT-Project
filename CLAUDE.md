# Stock Trading System (IFT 401 capstone)

Simulated stock-market web app. Customers (trainee brokers) buy/sell simulated stocks;
admins manage stocks and market hours. Source of truth is the SAD (put it at `docs/SAD.pdf`).
The full standing rules are in `docs/project-rules.md` (copied from Kiro) — read them before changing code.
NOTE: MySQL 8 is REQUIRED for the final version (team decision), so the SQLite-only parts of the rules are
being replaced by MySQL. Kiro's original files stay in `C:\Users\sjr_c\Documents\401Project` (read-only reference).

## About the user
Student, not a strong programmer. Build ONE feature at a time, explain changes briefly in plain
language, and add short "why" comments only in tricky spots (transactions, Decimal, locking).
Stop and summarize after each feature; wait for approval before the next one.

## Stack (fixed)
Python 3.14, Flask 3 app factory + Blueprints, Jinja2 templates, plain CSS/JS (no frameworks, no ORM,
no migrations), MySQL 8 via PyMySQL (local and AWS RDS), pytest (+ hypothesis).
Tests use a separate `stock_trading_test` database (name must end in `_test`); dev data is never touched.
Run: `.venv\Scripts\Activate.ps1`, `flask --app app init-db`, `flask --app app run`, `pytest -q`.

## Non-negotiable rules
- ALL SQL lives in `app/db.py`; parameterized only (`?` is translated to `%s` by db.py), never f-strings/concat.
- Money is `Decimal` from strings, never float; no money math inside SQL.
- Services (`app/services/`) never import Flask; routes stay thin.
- Multi-step changes run in one `transaction()` (START TRANSACTION) taking `FOR UPDATE` locks on the
  stock and user rows; re-validate at commit time.
- `transactions` rows that are confirmed/executed are never updated or deleted.
- CSRF on every POST; POST-Redirect-GET after state changes; pages work without JS.
- Every feature ships with tests; full suite must pass. Record evidence in `docs/VERIFICATION.md`.
- Tables are exactly: users, stocks, holdings, transactions, market_schedule, market_settings.
  No other schema changes without asking.

## Before the final demo (do not forget)
- The seed keeps the market open 24/7 so we can test any time. The business case says the default is
  weekdays 9:00-17:00 (market time zone), closed weekends. At the end: change `seed.py` to 09:00:00-17:00:00
  with Saturday/Sunday `is_holiday=1` (see `tests/dbhelpers.py:use_business_hours`), re-run init-db, and
  update docs/HOW_TO_RUN.md (trades then only work 9-5 ET weekdays unless the admin override is on).

## Status
This folder (`StockTradingApp`) is the real build. It started as a copy of Kiro's output (buy AND sell on
SQLite) and has been moved to MySQL. Done: MySQL switch, buy/sell tests, login + signup + logout with
customer/admin roles, cash deposit/withdraw + Financial Records page (includes transaction history).
Remaining work, in order: admin tools (create stock with logo/description, market hours + override toggle),
price generator, trade modal (Buy/Sell selector + live total), AWS deployment, then the 9-5 seed switch.
Auth notes: `app/routes/auth.py` loads `g.current_user` each request; guard blueprints with
`require_role(...)` in a `before_request`. Tests: `client` = logged-in trainee1, `admin_client`, `anon_client`. Claude Code is the builder from here; Kiro is no longer used on this folder.
