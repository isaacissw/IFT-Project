# Verification record

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
