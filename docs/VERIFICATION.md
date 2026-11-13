# Verification record

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
