-- MySQL 8 schema for the Stock Trading System.
-- The six tables and every column name follow the SAD ERD (plus the three
-- approved additions: stocks.description, stocks.logo_filename, market_settings).
--
-- Notes for readers:
--   * Money columns are DECIMAL(10,2): exact, never floating point.
--   * Timestamps are DATETIME(6), always stored as UTC (microsecond precision).
--   * MySQL 8 enforces CHECK constraints, so the rules below are real.
--   * app/db.py runs this file one statement at a time, splitting on the
--     semicolon character, so do not put that character inside a comment or
--     a string anywhere in this file.

CREATE TABLE users (
    user_id       INT NOT NULL AUTO_INCREMENT PRIMARY KEY,
    first_name    VARCHAR(255) NOT NULL,
    last_name     VARCHAR(255) NOT NULL,
    email         VARCHAR(255) NOT NULL UNIQUE,
    username      VARCHAR(255) NOT NULL UNIQUE,
    password_hash VARCHAR(255) NOT NULL,
    user_type     VARCHAR(10)  NOT NULL,
    cash_balance  DECIMAL(10,2) NOT NULL DEFAULT 0.00,
    is_active     BOOLEAN      NOT NULL DEFAULT 1,
    created_at    DATETIME(6)  NOT NULL,
    updated_at    DATETIME(6)  NOT NULL,
    CONSTRAINT chk_users_user_type CHECK (user_type IN ('customer','admin')),
    CONSTRAINT chk_users_cash_balance CHECK (cash_balance >= 0)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE stocks (
    stock_id                   INT NOT NULL AUTO_INCREMENT PRIMARY KEY,
    ticker_symbol              VARCHAR(10)  NOT NULL UNIQUE,
    company_name               VARCHAR(255) NOT NULL,
    company_shares_outstanding INT          NOT NULL,
    current_price              DECIMAL(10,2) NOT NULL,
    description                TEXT         NULL,
    logo_filename              VARCHAR(255) NULL,
    created_by                 INT          NOT NULL,
    created_at                 DATETIME(6)  NOT NULL,
    price_updated_at           DATETIME(6)  NOT NULL,
    CONSTRAINT chk_stocks_shares CHECK (company_shares_outstanding > 0),
    CONSTRAINT chk_stocks_price CHECK (current_price > 0),
    CONSTRAINT fk_stocks_created_by FOREIGN KEY (created_by) REFERENCES users(user_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE holdings (
    holdings_id INT NOT NULL AUTO_INCREMENT PRIMARY KEY,
    user_id     INT         NOT NULL,
    stock_id    INT         NOT NULL,
    quantity    INT         NOT NULL,
    created_at  DATETIME(6) NOT NULL,
    updated_at  DATETIME(6) NOT NULL,
    CONSTRAINT uq_holdings_user_stock UNIQUE (user_id, stock_id),
    CONSTRAINT chk_holdings_quantity CHECK (quantity > 0),
    CONSTRAINT fk_holdings_user  FOREIGN KEY (user_id)  REFERENCES users(user_id),
    CONSTRAINT fk_holdings_stock FOREIGN KEY (stock_id) REFERENCES stocks(stock_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE transactions (
    transaction_id   INT NOT NULL AUTO_INCREMENT PRIMARY KEY,
    user_id          INT          NOT NULL,
    stock_id         INT          NULL,
    transaction_type VARCHAR(10)  NOT NULL,
    quantity         INT          NULL,
    price_per_share  DECIMAL(10,2) NULL,
    total_amount     DECIMAL(10,2) NOT NULL,
    status           VARCHAR(10)  NOT NULL,
    created_at       DATETIME(6)  NOT NULL,
    confirmed_at     DATETIME(6)  NULL,
    executed_at      DATETIME(6)  NULL,
    INDEX idx_transactions_user_created (user_id, created_at),
    CONSTRAINT chk_tx_type CHECK (transaction_type IN ('buy','sell','deposit','withdrawal')),
    CONSTRAINT chk_tx_status CHECK (status IN ('pending','confirmed','executed')),
    CONSTRAINT chk_tx_total CHECK (total_amount > 0),
    CONSTRAINT chk_tx_shape CHECK (
        (transaction_type IN ('buy','sell')
            AND stock_id IS NOT NULL
            AND quantity IS NOT NULL AND quantity > 0
            AND price_per_share IS NOT NULL)
        OR
        (transaction_type IN ('deposit','withdrawal')
            AND stock_id IS NULL
            AND quantity IS NULL
            AND price_per_share IS NULL)
    ),
    CONSTRAINT fk_tx_user  FOREIGN KEY (user_id)  REFERENCES users(user_id),
    CONSTRAINT fk_tx_stock FOREIGN KEY (stock_id) REFERENCES stocks(stock_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE market_schedule (
    schedule_id         INT NOT NULL AUTO_INCREMENT PRIMARY KEY,
    created_by_admin_id INT         NOT NULL,
    day_of_week         VARCHAR(10) NOT NULL UNIQUE,
    market_open_time    TIME        NOT NULL,
    market_close_time   TIME        NOT NULL,
    is_holiday          BOOLEAN     NOT NULL DEFAULT 0,
    created_at          DATETIME(6) NOT NULL,
    updated_at          DATETIME(6) NOT NULL,
    CONSTRAINT chk_schedule_day CHECK (day_of_week IN
        ('Monday','Tuesday','Wednesday','Thursday','Friday','Saturday','Sunday')),
    CONSTRAINT fk_schedule_admin FOREIGN KEY (created_by_admin_id) REFERENCES users(user_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE market_settings (
    setting_id    INT NOT NULL PRIMARY KEY,
    override_mode VARCHAR(12) NOT NULL DEFAULT 'none',
    updated_by    INT         NULL,
    updated_at    DATETIME(6) NOT NULL,
    CONSTRAINT chk_settings_id CHECK (setting_id = 1),
    CONSTRAINT chk_settings_mode CHECK (override_mode IN ('none','force_open','force_closed')),
    CONSTRAINT fk_settings_user FOREIGN KEY (updated_by) REFERENCES users(user_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
