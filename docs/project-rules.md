---
inclusion: always
---

# Project Rules — Stock Trading System (Standing Context)

> This file is the standing context (Block A) for the Stock Trading System. It applies to every phase and every spec. It is set to always be included.

## Project: Stock Trading System (standing context)

You are building the Stock Trading System described in the attached Systems Analysis & Design document (docs/SAD.pdf, "Systems Design – Stock Trading App" by Stephen Colonna, William Persigehl, and Isaac Weakland).
## 1. What the system is and who it is for

Problem: A brokerage firm has no safe, hands-on environment where new stockbrokers can practice trading. Learning on live markets risks costly mistakes, and classroom-only training does not build practical skill in placing orders, settling transactions, or managing portfolios and cash accounts.

Solution: A simulated stock market web application. It behaves like a real market but carries zero financial risk: all money and all stocks are simulated.

Users (two roles):
- Customer (trainee stockbroker): views simulated stocks and prices, buys and sells shares, deposits and withdraws simulated cash, and views their portfolio, cash balance, and transaction history.
- Administrator: creates new stocks, edits stock details (never the price after creation), and controls market hours, including a manual open/close override.

Application type: a 3-tier web application:
- Tier 1 (Presentation): HTML, CSS, and a small amount of JavaScript, served by Flask as server-rendered Jinja2 templates. The browser never talks to the database.
- Tier 2 (Application): a Python Flask app server. It holds all business logic and enforces every business rule. Final hosting is AWS EC2, served on port 8000.
- Tier 3 (Data): a relational database. Local development uses SQLite. The final target is MySQL 8 on AWS RDS in a private subnet, reachable only from EC2 on port 3306.

## 2. Source of truth

The SAD is the source of truth for entities, attributes, data types, business rules, process flows, and screen layouts.

The table names and columns are the ones in the SAD ERD: users, stocks, holdings, transactions, market_schedule. Do not use any other entity names (for example, do NOT create Customer, Company, StockInventory, Portfolio, OrderHistory, or FinancialTransaction tables). In this system:
- "Customer" and "Administrator" are rows in users, told apart by user_type.
- "Company" and "stock inventory" live in stocks.
- "Portfolio" is the set of holdings rows for a user.
- "Order history" and "financial records" are the rows in transactions.

This prompt adds implementation detail that the SAD does not specify, plus exactly three small schema additions (listed in Section 6), which are needed to meet SAD requirements that the ERD does not cover. Make no other schema changes without asking first.

If you find a conflict between the SAD and this prompt, or something either one leaves ambiguous, stop and ask instead of guessing.
## 3. Tech stack (fixed, do not substitute)

- Python 3.14, Flask 3.x using the application-factory pattern (create_app() in app/__init__.py), and Blueprints for route groups.
- Jinja2 server-rendered templates with plain HTML and CSS. No front-end framework: no React, Vue, Angular, or Tailwind build step. Plain JavaScript is allowed only for small enhancements, such as the trade modal and the live cost preview. Every page and form must still work with JavaScript turned off.
- No ORM (no SQLAlchemy) and no migration tool (no Flask-Migrate, no Alembic). Use plain SQL through standard drivers:
  - local: Python built-in sqlite3 module.
  - AWS: pymysql.
- Flask-WTF is used only for CSRFProtect (CSRF tokens on every POST form). Build the forms by hand in the templates.
- Password hashing uses werkzeug.security (generate_password_hash / check_password_hash).
- Testing uses pytest.
- Production server: gunicorn (from the AWS phase on).
- Pin exact versions in requirements.txt (for example Flask==3.x.y) and confirm they install cleanly.

## 4. Project structure (use this layout)

```
stock-trading-system/
├── app/
│   ├── __init__.py          # create_app(), registers blueprints, CSRF, CLI commands
│   ├── config.py            # reads settings from environment variables
│   ├── db.py                # THE ONLY module that contains SQL or touches a DB connection
│   ├── money.py             # Decimal helpers: to_money(), quantize to 0.01, formatting
│   ├── clock.py             # now_utc() — the single source of "now", so tests can inject time
│   ├── services/            # business logic; no Flask request/session imports here
│   │   ├── market.py        # is_market_open(now)
│   │   ├── trading.py       # buy (and later sell) logic
│   │   └── ...              # later: auth.py, cash.py, admin.py, pricing.py
│   ├── routes/              # Blueprints; thin: parse input -> call service -> render/redirect
│   ├── templates/
│   └── static/ (css/, js/)
├── schema.sql               # CREATE TABLE statements (SQLite dialect)
├── seed.py                  # loads demo data (called by the init-db CLI command)
├── tests/
├── docs/ (SAD.pdf, VERIFICATION.md, later DEPLOY.md)
├── requirements.txt
├── .env.example             # every env var with a safe placeholder; no real secrets
├── .gitignore               # includes .env, instance/, *.db, __pycache__/, .venv/
└── README.md                # setup, init-db, run, test commands
```
## 5. Global engineering rules (every phase, no exceptions)

- One data-access module. All SQL lives in app/db.py. It opens connections (engine chosen by the DATABASE_URL environment variable), provides a transaction context manager, and exposes small named functions such as get_user(user_id), get_stock(stock_id), insert_transaction(...), update_cash_balance(...). Routes and services never write SQL or call cursor.execute. This also keeps the SQLite-vs-MySQL syntax differences in one file.
- Parameterized SQL only. Every SQL statement that includes any value uses driver placeholders (? for sqlite3, %s for pymysql, translated inside db.py). Never build SQL with string concatenation, +, f-strings, % formatting, or .format(). The only exception is fixed identifiers chosen from a hard-coded whitelist inside db.py (for example, an ORDER BY column), and those must never come from user input.
- Money is Decimal. All money and price columns are DECIMAL(10,2). In Python, all money is decimal.Decimal, created from strings (Decimal("42.50")), never from floats. Never use float for money anywhere (code, templates, or tests). Quantize results to 2 places using ROUND_HALF_UP.
- SQLite pitfall you must handle: SQLite has no true DECIMAL type and will silently store values as floating-point. So (a) register a sqlite3 adapter (Decimal -> str) and a converter (DECIMAL -> Decimal(str)), and connect with detect_types=sqlite3.PARSE_DECLTYPES; (b) never do money arithmetic inside SQL (no SET cash_balance = cash_balance - ?); read the value, compute the new value in Python with Decimal, and write the result; (c) include a test proving values like 0.10, 172.34, and 99999999.99 round-trip exactly.
- Share quantities are whole integers. No fractional shares.
- Service layer. Business rules live in app/services/*. Services take plain arguments (user_id, stock_id, quantity, now) and return results or raise typed exceptions (for example MarketClosedError, InsufficientFundsError, InsufficientSharesError, ValidationError). Services never import flask.request or flask.session, so they can be unit-tested with no web server.
- Atomic state changes. Every multi-step change (for example: debit cash + add shares + record transaction) runs inside one database transaction. If any step fails, roll back everything so there are no partial writes. Use explicit transaction control in db.py:
  - SQLite: open the connection with isolation_level=None and begin with BEGIN IMMEDIATE, so concurrent writers cannot interleave.
  - MySQL: START TRANSACTION, then SELECT ... FOR UPDATE on the user row and stock row being changed.
  - Run PRAGMA foreign_keys = ON on every SQLite connection.
- Re-validate at commit time. Anything checked on a review screen (market open, funds, shares available, price) is checked again inside the transaction that commits the change. Never trust values sent back from the browser (price, total, user id). The server always re-reads them from the database.
- Audit trail is immutable. Once a transactions row is confirmed or executed, it is never updated or deleted. db.py must not expose any function that does so. Confirmed transactions cannot be cancelled (SAD business rule).
- Timestamps. Every row records created_at (and updated_at / confirmed_at / executed_at / price_updated_at where the ERD has them), generated in Python from clock.now_utc() in UTC and passed as parameters. Store SQLite timestamps as ISO-8601 text. Convert to the market time zone for display only.
- Market time zone: MARKET_TIMEZONE env var, default America/New_York (the SAD wireframe uses ET). Use zoneinfo.
- Secrets stay out of code. SECRET_KEY, DB credentials, and seed passwords come from environment variables (a local .env loaded with python-dotenv in development). Commit only .env.example. Never print secrets in logs.
- Security basics: CSRF token on every POST form. Session cookie HttpOnly and SameSite=Lax. Jinja2 autoescaping stays on (never use |safe on user data). The user identity always comes from the server session (or, in Phase 1, the configured demo user), never from a form field or URL parameter.
- POST-Redirect-GET after every successful state change, so refreshing the page cannot repeat a trade.
- Testing: every feature ships with tests:
  - unit tests for each service rule (happy path + every failure path),
  - route tests using Flask test client,
  - database-effect tests that query real values before and after,
  - a rollback test that forces a failure midway and proves nothing changed. Tests use a fresh temporary database built from schema.sql + seed for each test (fixtures in tests/conftest.py). Inject time through clock so "market open" and "market closed" tests do not depend on when they run. The full suite must pass before a phase is done.
- Prove it works; a clean exit is not proof. For each phase: recreate the database from scratch, seed it, run the full test suite, start the app, exercise the real routes, and query the database before and after to confirm the real effects. Record the exact commands and their real output in docs/VERIFICATION.md (append a section per phase).
- Phase discipline: build only what the current phase lists as In Scope. Do not build anything on the Defer list, and do not add placeholder buttons, links, or "coming soon" pages for deferred features. When the phase is complete and verified: stop, give a short summary (files created/changed, how to run it, test results, the before/after database values, and any questions or assumptions), and wait for approval before starting the next phase.
- Explain as you go. The user is a student and not a strong programmer. Keep code readable, add short comments explaining why in the tricky places (transactions, Decimal handling, locking), and use clear names over clever code.
## 6. Database schema (defined in full in Phase 1)

Because there is no migration tool, schema.sql defines the complete schema in Phase 1, including tables used only by later phases, so we never have to restructure the database mid-project. "Setting up the database" means running schema.sql, then seeding.

Column names and types follow the SAD ERD exactly. SQLite has no ENUM, so ENUM columns become VARCHAR + CHECK (... IN (...)). Write schema.sql in SQLite dialect for now; the MySQL version comes in Phase 2.

### users (ERD: Users)
- user_id INTEGER PRIMARY KEY AUTOINCREMENT
- first_name VARCHAR(255) NOT NULL
- last_name VARCHAR(255) NOT NULL
- email VARCHAR(255) NOT NULL UNIQUE
- username VARCHAR(255) NOT NULL UNIQUE
- password_hash VARCHAR(255) NOT NULL
- user_type VARCHAR(10) NOT NULL CHECK IN ('customer','admin') (ERD ENUM)
- cash_balance DECIMAL(10,2) NOT NULL DEFAULT 0.00, CHECK (cash_balance >= 0)
- is_active BOOLEAN NOT NULL DEFAULT 1
- created_at TIMESTAMP NOT NULL
- updated_at TIMESTAMP NOT NULL

### stocks (ERD: Stocks)
- stock_id INTEGER PRIMARY KEY AUTOINCREMENT
- ticker_symbol VARCHAR(10) NOT NULL UNIQUE
- company_name VARCHAR(255) NOT NULL
- company_shares_outstanding INT NOT NULL, CHECK (> 0)
- current_price DECIMAL(10,2) NOT NULL, CHECK (> 0)
- description TEXT NULL  <- addition #1 (SAD requirement: optional stock description at creation)
- logo_filename VARCHAR(255) NULL  <- addition #2 (SAD requirement: company logo upload at creation)
- created_by INT NOT NULL, FK -> users(user_id) (only admins may create stocks)
- created_at TIMESTAMP NOT NULL
- price_updated_at TIMESTAMP NOT NULL

### holdings (ERD: Holdings, which tracks the stocks each user owns and how many)
- holdings_id INTEGER PRIMARY KEY AUTOINCREMENT
- user_id INT NOT NULL, FK -> users(user_id)
- stock_id INT NOT NULL, FK -> stocks(stock_id)
- quantity INT NOT NULL, CHECK (quantity > 0) (a row is deleted when a sale brings it to 0)
- created_at TIMESTAMP NOT NULL
- updated_at TIMESTAMP NOT NULL
- UNIQUE (user_id, stock_id)

### transactions (ERD: Transactions; this table is both the order history and the financial records)
- transaction_id INTEGER PRIMARY KEY AUTOINCREMENT
- user_id INT NOT NULL, FK -> users(user_id)
- stock_id INT NULL, FK -> stocks(stock_id) (NULL for cash deposits and withdrawals)
- transaction_type VARCHAR(10) NOT NULL CHECK IN ('buy','sell','deposit','withdrawal') (ERD ENUM)
- quantity INT NULL (whole shares for buy/sell; NULL for cash)
- price_per_share DECIMAL(10,2) NULL (NULL for cash)
- total_amount DECIMAL(10,2) NOT NULL, CHECK (> 0)
- status VARCHAR(10) NOT NULL CHECK IN ('pending','confirmed','executed') (ERD ENUM)
- created_at TIMESTAMP NOT NULL
- confirmed_at TIMESTAMP NULL (NULL until confirmed)
- executed_at TIMESTAMP NULL (NULL until executed)
- Table CHECK: buy/sell rows must have stock_id, quantity > 0, and price_per_share; deposit/withdrawal rows must have all three NULL.
- Index on (user_id, created_at).

### market_schedule (ERD: Market_Schedule; one row per weekday)
- schedule_id INTEGER PRIMARY KEY AUTOINCREMENT
- created_by_admin_id INT NOT NULL, FK -> users(user_id) (only admins may update market schedules)
- day_of_week VARCHAR(10) NOT NULL UNIQUE CHECK IN ('Monday','Tuesday','Wednesday','Thursday','Friday','Saturday','Sunday')
- market_open_time TIME NOT NULL (stored as 'HH:MM:SS' in SQLite)
- market_close_time TIME NOT NULL
- is_holiday BOOLEAN NOT NULL DEFAULT 0 (if 1, the market is closed all day)
- created_at TIMESTAMP NOT NULL
- updated_at TIMESTAMP NOT NULL

### market_settings  <- addition #3 (SAD requirement: administrator Manual Market Override toggle)
- setting_id INTEGER PRIMARY KEY CHECK (setting_id = 1) (always exactly one row)
- override_mode VARCHAR(12) NOT NULL DEFAULT 'none' CHECK IN ('none','force_open','force_closed')
- updated_by INT NULL, FK -> users(user_id)
- updated_at TIMESTAMP NOT NULL

### Derived values (computed, never stored):
- Shares available in inventory for a stock = company_shares_outstanding - SUM(holdings.quantity for that stock). Buying takes shares out of inventory; selling returns them (SAD: "return shares to the stock inventory upon a successful stock sale").
- Portfolio value = sum of (holding quantity x that stock current_price).
- Market open? (services/market.py -> is_market_open(now)):
  - If market_settings.override_mode is 'force_open', return True. If 'force_closed', return False.
  - Otherwise convert now to MARKET_TIMEZONE and find the market_schedule row for that weekday. If there is no row or is_holiday = 1, the market is closed. Otherwise it is open when market_open_time <= local time < market_close_time.
## 7. SAD business rules to enforce (all phases)

- Trades execute at the current market price, re-read from the database at the moment of execution.
- A confirmation prompt is shown before any transaction is fulfilled, and valid orders execute only after the user confirms.
- Confirmed transactions cannot be cancelled.
- Sufficient funds are validated before a purchase and before a withdrawal. Error text: "Insufficient Funds".
- Trades submitted outside market hours are rejected. Error text: "Market Closed".
- Selling more shares than owned is rejected. Error text: "Insufficient Shares".
- Portfolio, order history, and financial records update immediately after each transaction.
- Timestamps are recorded for account creation, every stock transaction, and every cash transaction.
- Admins cannot change a stock price after creation; only the system price generator changes prices.
- Prices fluctuate gradually during market hours and stay static while the market is closed.