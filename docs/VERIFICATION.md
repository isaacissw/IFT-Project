# Verification record

## Company logo upload restored (SAD Administrator Requirement)

- The SAD explicitly requires "upload a company logo image during stock creation", so the feature that
  was removed for scope was restored with the same safety design: the file's content (its signature
  bytes) decides whether it is a PNG/JPEG/GIF/WebP image; SVG and everything else is refused; max 1 MB
  (request limit 2 MB, HTTP 413 above that); stored under a random name (the uploaded name is never
  used); only names the app generated are served, with a `nosniff` header; an image saved for a stock
  that then fails to be created is deleted again; a replaced logo's old file is deleted after the
  change succeeds; an edit with a bad replacement image changes nothing.
- Logos show next to the company name on the customer dashboard, the admin inventory and the edit page.
- `pytest -q` -> 485 passed (454 earlier + 31 logo tests restored).
- Also in this change: every mention of the earlier AI tool was replaced with context explaining the
  project's history; `docs/project-rules.md` now starts with a note on which of its rules still apply;
  the price generator (`app/services/pricing.py`, `app/cli.py`) is annotated step by step with worked
  examples. No program logic changed in the generator.
## Random price generator (SAD process map #8)

- `pytest -q` -> 454 passed (392 earlier + 62 new: `test_pricing.py`, `test_price_rules.py`,
  `test_price_generator_cli.py`).
- Each update moves every price by a random whole number of basis points between -50 and +50 (0.5%),
  exact Decimal math rounded to the cent; a cheap stock still moves by a cent; never below $0.01 or
  above the column maximum. Property test: for ANY price and step the result is valid and the move
  is at most 0.5% + 1 cent.
- Market closed (schedule, weekend, or forced closed) -> nothing in the database changes, not even
  the update time. Forced open -> prices move even on a closed day.
- `price_updated_at` is recorded on every update and shown on the customer dashboard.
- Only the generator can change a price after creation: one db function (`update_stock_price`) and
  source scans prove no other SQL or caller touches `current_price`. Mutation check: planting a rogue
  price write in `app/routes/admin.py` made 2 rule tests fail; removed again.
- Trading interplay: an order reviewed before a price move must be reviewed again (PriceChangedError,
  nothing charged); a tick racing a confirm in parallel never charges a wrong amount (8 rounds).
- Runs as its own process: `flask --app app run-price-generator [--once] [--seconds N] [--step-bps N]
  [--seed N]`; a failed update (e.g. database down) is reported and retried; Ctrl+C stops cleanly.
- Bug found while testing and fixed: the loop captured `time.sleep` when it was defined, so a test
  could not stop it and the test run hung. It now looks the sleep function up when it runs.
- Real run: generator every 3 s next to `flask run`; the dashboard showed e.g. SSOL $42.61 -> $42.47,
  MESA $308.45 -> $310.61 within 7 seconds, with the update time changing.
## Administrator tools (stock inventory, market hours, manual override)

- `pytest -q` -> 392 passed (294 earlier + 98 new in `test_admin_services.py` and `test_admin_routes.py`).
- Create stock: company name, ticker (1-5 letters, upper-cased, unique), shares outstanding (whole
  number, 1 to 1,000,000,000), initial price ($0.01 to $1,000,000.00, 2 decimals) and an optional
  description (up to 2000 chars); every problem is shown at once and nothing is written on error; a new
  stock can be traded immediately.
- Edit stock: name, description, shares. The ticker and price cannot be changed, enforced by the shape
  of the code (no price parameter exists) and tested, including a forged price in the request. Shares
  cannot go below what customers own (checked under a row lock).
- Market: per-day hours (open must be before close; or "closed all day", which keeps the stored hours)
  saved all-or-nothing; manual override none/force_open/force_closed recorded with who and when; forcing
  the market closed stops customers trading (tested end to end).
- Access control: only administrators reach `/admin/*`; customers and logged-out visitors are redirected
  and cannot change anything even with a valid CSRF token; every POST needs a CSRF token; company names
  are HTML-escaped on every page.
- Scope decision: company-logo upload was built and then removed on the team's decision (not needed for
  this project). The unused `stocks.logo_filename` column remains in the schema.
- Real browser run (before the logo removal, same admin screens): logged in as admin, created a stock,
  forced the market closed (status line updated).
- The weekly schedule editor is also how the final switch to the 9-5 weekday schedule can be made
  (or via the seed file, see CLAUDE.md). The demo market stays open 24/7 until then.
## Cash deposits and withdrawals (Financial Records page)

- `pytest -q` -> 294 passed (187 earlier + 107 new: `test_cash.py`, `test_cash_routes.py`,
  `test_concurrency.py`).
- Rules: amount is a plain positive number with at most 2 decimals (rejects 0, negatives, 1.234,
  1e3, $5, 1,000, text); a deposit is limited to $1,000,000.00 and the balance can never exceed
  $99,999,999.99; a withdrawal cannot exceed the balance ("Insufficient Funds", re-checked at
  confirm); a review/confirm step as with trades; a confirmed cash transaction can never be
  cancelled; every cash event is timestamped; cash works at any hour (not tied to market hours).
- Safety: confirm runs in one locked transaction with a rollback on any failure. Two simultaneous
  confirms (double-click, two withdrawals spending the same money, two buyers of the last shares)
  are tested with real parallel connections. Mutation check: with the row locks removed, 5 of the 7
  concurrency tests fail (lost update 900.00 vs 1150.00, oversold shares), so the tests do catch races.
- Real browser run: new account $0.00 -> deposit 1000.50 -> over-withdrawal 5000 refused ->
  withdraw 200.25 -> balance 800.25, both rows in the history with timestamps.
- Also: the Financial Records page shows the full executed-transaction history (latest 100), so the
  "view history of transactions" requirement is met; filtering/paging can come later.
- Hardening: cancelling a pending buy/sell now also checks the transaction type.

## Login, signup and logout

- `pytest -q` -> 187 passed (119 earlier + 68 new in `tests/test_auth.py`, plus updated route tests).
- Rules enforced and tested: passwords stored only as salted hashes; failed login gives one generic
  message whether the username or password was wrong; session cookie is HttpOnly + SameSite=Lax;
  every POST (including login, signup, logout) needs a CSRF token; logged-out visitors are sent to
  `/login` and cannot trade even with a valid token; customers cannot open `/admin`; administrators
  cannot trade; a deactivated account is logged out on its next request; signup always creates a
  customer with $0.00 cash and a creation timestamp; SQL-injection text in login fields is harmless.
- Real browser run against `flask run`: signup -> auto-login -> dashboard with welcome message;
  logout; wrong password -> "Invalid username or password." (HTTP 401); correct password -> dashboard.
- Not built yet (noted for later): rate-limiting/lockout after repeated failed logins, password reset.

## Test coverage added (buy, sell, rollback, routes, CSRF, SQL safety)

- `pytest -q` -> 112 passed (38 earlier + 74 new) against the MySQL test database.
- New files: `test_trading_buy.py`, `test_trading_sell.py`, `test_rollback.py`,
  `test_routes.py`, `test_sql_safety.py`, `test_sql_injection.py`.
- Bug found by these tests and fixed: when the price changed between review and confirm,
  the stale pending order was supposed to be deleted, but the transaction rollback undid
  the delete. `PriceChangedError` now carries `keep_changes = True` so `db.transaction()`
  commits the delete before the error is raised.
- Also fixed: the 404 page now shows `Stock not found.` as the requirements specify.
- Known gap (not yet fixed): after a price-change error on confirm the user lands on the
  dashboard rather than the trade page, because the confirm form does not send the stock id.

## MySQL migration (SQLite -> MySQL 8.0)

Commands run on a fresh database and the real results:

- `flask --app app init-db` -> users 2, stocks 6, holdings 0, transactions 0,
  market_schedule 7, market_settings 1.
- `pytest -q` -> 38 passed (against the separate `stock_trading_test` database).
- Real HTTP flow with CSRF enabled (Flask test client against the MySQL dev database):
  - Buy 10 SSOL: cash 10000.00 -> 9575.00, holding none -> 10, SSOL available
    1,000,000 -> 999,990, transactions 0 -> 1 (`buy`, qty 10, price 42.50, total 425.00,
    status `executed`, created/confirmed/executed timestamps all set).
  - Re-submitting the same confirm: "already processed" message, nothing changed.
  - Sell 4 SSOL: cash 9575.00 -> 9745.00, holding 10 -> 6, SSOL available -> 999,994,
    transactions 1 -> 2.
  - Buy 1 GCNY: "Insufficient Funds", no database change.
  - POST without a CSRF token: HTTP 400.
