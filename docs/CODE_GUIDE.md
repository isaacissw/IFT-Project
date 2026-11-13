# Code Guide: how the Stock Trading System works

This is a plain-language tour of the code. Read it top to bottom once, then use it as a map.
Every Python file also starts with a comment explaining its job, and every page template starts
with a `{# ... #}` comment saying what the page is.

## 1. The big picture: three layers (the SAD's "3-tier architecture")

```
 Browser (HTML pages)          <- Tier 1: what the user sees      app/templates/, app/static/
      |  clicks / form posts
      v
 Flask app (Python)            <- Tier 2: the rules               app/routes/  ->  app/services/
      |  SQL through one module
      v
 MySQL database                <- Tier 3: the data                schema.sql, app/db.py
```

The browser never talks to the database. Every click goes to Flask, Flask applies the rules,
and only `app/db.py` talks to MySQL.

## 2. Inside the Flask app: routes, services, db

Each request passes through three kinds of file, always in this order:

| Layer | Folder | Its one job | Example |
|---|---|---|---|
| **Route** | `app/routes/` | Read the web request, call a service, then show a page or redirect. No rules, no SQL. | `buy_review()` in `routes/trading.py` |
| **Service** | `app/services/` | The business rules (the SAD's rules). Raises a named error when a rule fails. No web code, no SQL. | `create_pending_buy()` in `services/trading.py` |
| **Data** | `app/db.py` | The ONLY file with SQL. Small named functions like `get_stock()` or `update_user_cash()`. | `insert_pending_buy()` |

Why split it like this? Each piece is small and can be tested on its own. The services
can be tested without a web browser, and all the SQL can be checked for safety in one file.

## 3. What each file does

**`app/`**
- `__init__.py`: builds the app (`create_app()`): loads settings, switches on security, registers
  the routes and the `init-db` command.
- `config.py`: reads settings from `.env` (database address, secret key, timezone, price generator).
- `db.py`: every SQL statement, plus the connection and `transaction()` helper.
- `money.py`: exact money helpers (`Decimal`, rounding to cents, `$1,234.56` formatting).
- `clock.py`: `now_utc()`, the one place that reads the current time.
- `cli.py`: the `run-price-generator` command.

**`app/routes/`** (web pages and buttons)
- `auth.py`: log in, sign up, log out, and the `require_role()` guard that protects pages.
- `trading.py`: customer dashboard, trade page, buy/sell review/confirm/cancel.
- `cash.py`: Financial Records page, deposit/withdraw review/confirm/cancel.
- `admin.py`: administrator dashboard, create/edit stock (with optional logo), market hours, override.
- `media.py`: serves the uploaded company logos (only files the app itself created).

**`app/services/`** (the rules)
- `auth.py`: signup rules, password hashing, login check.
- `trading.py`: buy and sell rules (funds, shares, market open, price unchanged).
- `cash.py`: deposit and withdrawal rules.
- `market.py`: "is the market open right now?"
- `admin.py`: stock creation/editing rules, market-hours rules.
- `pricing.py`: the random price generator. Its comments walk through the logic step by step,
  with worked number examples; start there if prices are confusing.
- `uploads.py`: checks that an uploaded logo really is a small image, and stores it safely.
- `portfolio.py`: portfolio value (sum of shares x price).

**Other**
- `app/templates/*.html`: the pages (Jinja templates: HTML with `{{ value }}` placeholders).
- `app/static/css/style.css`: colors and layout (ASU maroon and gold at the top).
- `app/static/js/trade.js`: live "Estimated Total" on the trade page.
- `schema.sql`: creates the six tables. `seed.py`: the demo data.
- `tests/`: automated tests (`pytest -q` runs them all).

## 4. Follow one click: buying 10 shares of SSOL

1. **Trade page.** The customer clicks **Trade** next to SSOL. The browser asks for `/trade/3`, and
   `trade()` in `routes/trading.py` loads the stock and shows `trade.html`.
2. **Review Order.** The form posts to `/trade/buy/review`, and `buy_review()` runs:
   - it gets the customer's id **from the login session** (never from the form),
   - `parse_quantity("10")` checks it is a whole number,
   - `create_pending_buy()` checks the rules in order (stock exists, market open, enough shares,
     enough cash) and saves the order with status **pending**. No money has moved.
   - The route shows `confirm.html` with the cost and "cash after".
3. **Confirm Transaction.** The form posts to `/trade/buy/confirm`, and `confirm_buy()` runs inside
   ONE database transaction:
   - it **locks** the order row, the stock row and the customer's row (so nobody else can change
     them until it finishes),
   - re-checks everything (time has passed since the review, so the price may have moved),
   - marks the order confirmed, subtracts the cash, adds the shares, marks it **executed**,
   - commits. If anything fails partway, the database **rolls back** and nothing changes.
4. **Redirect.** The route sends the browser back to `/` with a green message. (Redirecting
   rather than showing a page directly means pressing Refresh cannot buy twice.)

Selling, depositing and withdrawing follow exactly the same review, confirm or cancel pattern.

## 5. Key ideas, in plain words

- **Decimal, never float.** Computers store normal decimal numbers (floats) slightly wrong:
  `0.1 + 0.2` is not exactly `0.3`. Money must be exact, so all money is `Decimal`, built from text.
- **Parameterized SQL (`?` marks).** Values are never pasted into SQL text; they are passed
  separately. This makes SQL injection (typing SQL into a form) harmless.
- **Transaction.** A group of database changes that all happen or none happen. `with db.transaction(conn):`
  in the code starts one.
- **Row lock (`FOR UPDATE`).** While a confirm is running, other requests that want the same rows
  must wait. This stops two clicks from spending the same money twice.
- **Pending, confirmed, executed.** The three statuses of a transaction. Pending orders can be
  cancelled; confirmed or executed ones can never be changed (the SAD's "no cancellation" rule).
- **CSRF token.** A hidden secret in every form, tied to the visitor's session, proving the form
  came from our own site. Forms without it are rejected.
- **Password hash.** Passwords are stored scrambled one-way. We can check a password is right,
  but nobody (including us) can read it back.
- **Session.** After login, the browser holds a signed cookie with just the user's id. Every
  request looks that user up again in the database.
- **Blueprint.** Flask's name for a group of related routes (one per file in `app/routes/`).
- **Jinja template.** An HTML file with placeholders: `{{ cash_balance | money }}` shows the
  cash formatted as money. Jinja escapes text automatically, so typed-in HTML can't run as code.
- **Basis point (bps).** One hundredth of a percent. 50 bps = 0.5%. Used by the price generator.
- **File signature.** The first few bytes of a file that identify its real type (every PNG starts with
  the same bytes). Logo uploads are checked this way, never by their file name.

## 6. Where to change things

| I want to... | Look in |
|---|---|
| change a color | the top of `app/static/css/style.css` |
| change wording on a page | `app/templates/<page>.html` |
| change a business rule (limits, checks) | `app/services/<area>.py` |
| add a new page or button | a route in `app/routes/` + a template |
| change or add SQL | `app/db.py` only |
| change the demo data or market hours default | `seed.py`, then run `init-db` |
| change how fast prices move | `.env`: `PRICE_TICK_SECONDS`, `PRICE_MAX_STEP_BPS` |
| change the logo size or allowed image types | `app/services/uploads.py` |

## 7. Reading the tests

Each file in `tests/` is named after what it checks (`test_cash.py`, `test_auth.py`, ...). Each
test is a short story: set something up, do one thing, `assert` what should now be true. The
tests use a separate `stock_trading_test` database, so they never touch your demo data.
