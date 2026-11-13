# How to run the tests and the website

All commands go in a terminal inside VS Code: **Terminal > New Terminal**. It opens in the
project folder (`StockTradingApp`). Commands are for PowerShell (the Windows default).

## Before anything: is MySQL running?

MySQL Server must be running. Press the Windows key, type **Services**, open it, find
**MySQL80** and check the Status says **Running**. (If not, right-click it > Start.)

## A. Run the automated tests

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

- Each `.` is a test that passed. At the end you want a line like `112 passed`.
- It takes about two minutes. It only uses the `stock_trading_test` database, so it never
  touches the data you see on the website.
- To run just one file: `.\.venv\Scripts\python.exe -m pytest -q tests\test_routes.py`
- A failure prints `F` and then the name of the test and what went wrong. Send that to
  Claude.

## B. Run the website and try it yourself

1. **Reset the demo data** (do this any time you want a clean start). It drops the tables,
   rebuilds them and loads the 6 stocks and the demo customer with $10,000:

   ```powershell
   .\.venv\Scripts\python.exe -m flask --app app init-db
   ```

   You should see `users: 2`, `stocks: 6`, `holdings: 0`, `transactions: 0`.

2. **Start the server:**

   ```powershell
   .\.venv\Scripts\python.exe -m flask --app app run
   ```

   Leave this terminal open. You should see `Running on http://127.0.0.1:5000`.

3. **Open the site:** go to http://127.0.0.1:5000 in your browser. You land on the login page.

4. **Log in** (or create your own account with **Create an account**; new accounts start with
   $0.00, so for trading use the demo customer):
   - Customer: username `trainee1`, password = the `SEED_CUSTOMER_PASSWORD` value in your `.env`.
   - Administrator: username `admin`, password = the `SEED_ADMIN_PASSWORD` value. You land on the
     Administrator Dashboard (admins cannot trade, and customers cannot open `/admin`).
   - A wrong password shows "Invalid username or password." Use **Log out** (top right) to leave.

5. **Try a purchase** (logged in as `trainee1`):
   - The top bar shows Cash Balance `$10,000.00` and Portfolio Value `$0.00`.
   - Click **Trade** next to *Sonoran Solar Corp (SSOL)*.
   - Type `10` in Quantity and click **Review Order**.
   - The review screen shows Total Cost `$425.00` and Cash After `$9,575.00`.
   - Click **Confirm Transaction**.
   - Back on the dashboard you should see the green message
     `Purchased 10 shares of SSOL at $42.50 for $425.00.`, Cash Balance `$9,575.00`, and
     SSOL listed under **My Portfolio** and **Recent Transactions**.

6. **Try a sale:** click **Trade** on SSOL again, click the **Sell** button at the top of the
   page, enter `4`, **Review Order**, **Confirm Transaction**. Cash goes up by `$170.00` and
   you now own 6 shares.

7. **Try the error messages:**
   - Trade **Grand Canyon Holdings (GCNY)**, quantity `1`: **Insufficient Funds**
     (`This order costs $12,500.00 but your cash balance is ...`).
   - Trade **Saguaro Rare Earths (SAGU)**, quantity `51`: **Insufficient Shares Available**
     (`Only 50 shares of SAGU are available.`).
   - Type `abc` or `0` or `1.5` as the quantity: a message asks for a whole number.
   - On the review screen, click **Cancel** instead of Confirm: `Order cancelled.` and
     nothing is charged.

8. **Stop the server:** click in the terminal and press **Ctrl+C**.

## C. Look at the data in MySQL Workbench

Open Workbench, connect, then run in a query tab:

```sql
USE stock_trading;
SELECT user_id, username, cash_balance FROM users;
SELECT * FROM holdings;
SELECT * FROM transactions;
```

After the purchase above you will see cash `9575.00`, a holdings row with quantity 10, and a
transaction with status `executed`. This is how you prove the website really changed the
database.

## If something goes wrong

| What you see | What it means |
| --- | --- |
| `Access denied for user 'stock_app'` | The password in `.env` does not match the one set in MySQL. |
| `Can't connect to MySQL server` | MySQL80 is not running (see the first section). |
| `Unknown database` | Create the database in Workbench (see README step 2). |
| `Demo user is not resolved` | Run the `init-db` command (step B.1). |
| Port 5000 already in use | An old server is still running. Press Ctrl+C in that terminal. |
