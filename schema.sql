PRAGMA foreign_keys = ON;

CREATE TABLE users (
    user_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    first_name    VARCHAR(255) NOT NULL,
    last_name     VARCHAR(255) NOT NULL,
    email         VARCHAR(255) NOT NULL UNIQUE,
    username      VARCHAR(255) NOT NULL UNIQUE,
    password_hash VARCHAR(255) NOT NULL,
    user_type     VARCHAR(10)  NOT NULL CHECK (user_type IN ('customer','admin')),
    cash_balance  DECIMAL(10,2) NOT NULL DEFAULT 0.00 CHECK (cash_balance >= 0),
    is_active     BOOLEAN      NOT NULL DEFAULT 1,
    created_at    TIMESTAMP    NOT NULL,
    updated_at    TIMESTAMP    NOT NULL
);

CREATE TABLE stocks (
    stock_id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker_symbol              VARCHAR(10)  NOT NULL UNIQUE,
    company_name               VARCHAR(255) NOT NULL,
    company_shares_outstanding INTEGER      NOT NULL CHECK (company_shares_outstanding > 0),
    current_price              DECIMAL(10,2) NOT NULL CHECK (current_price > 0),
    description                TEXT         NULL,
    logo_filename              VARCHAR(255) NULL,
    created_by                 INTEGER      NOT NULL,
    created_at                 TIMESTAMP    NOT NULL,
    price_updated_at           TIMESTAMP    NOT NULL,
    FOREIGN KEY (created_by) REFERENCES users(user_id)
);

CREATE TABLE holdings (
    holdings_id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER   NOT NULL,
    stock_id    INTEGER   NOT NULL,
    quantity    INTEGER   NOT NULL CHECK (quantity > 0),
    created_at  TIMESTAMP NOT NULL,
    updated_at  TIMESTAMP NOT NULL,
    UNIQUE (user_id, stock_id),
    FOREIGN KEY (user_id)  REFERENCES users(user_id),
    FOREIGN KEY (stock_id) REFERENCES stocks(stock_id)
);

CREATE TABLE transactions (
    transaction_id  INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id         INTEGER      NOT NULL,
    stock_id        INTEGER      NULL,
    transaction_type VARCHAR(10) NOT NULL CHECK (transaction_type IN ('buy','sell','deposit','withdrawal')),
    quantity        INTEGER      NULL,
    price_per_share DECIMAL(10,2) NULL,
    total_amount    DECIMAL(10,2) NOT NULL CHECK (total_amount > 0),
    status          VARCHAR(10)  NOT NULL CHECK (status IN ('pending','confirmed','executed')),
    created_at      TIMESTAMP    NOT NULL,
    confirmed_at    TIMESTAMP    NULL,
    executed_at     TIMESTAMP    NULL,
    FOREIGN KEY (user_id)  REFERENCES users(user_id),
    FOREIGN KEY (stock_id) REFERENCES stocks(stock_id),
    CHECK (
        (transaction_type IN ('buy','sell')
            AND stock_id IS NOT NULL
            AND quantity IS NOT NULL AND quantity > 0
            AND price_per_share IS NOT NULL)
        OR
        (transaction_type IN ('deposit','withdrawal')
            AND stock_id IS NULL
            AND quantity IS NULL
            AND price_per_share IS NULL)
    )
);

CREATE INDEX idx_transactions_user_created ON transactions(user_id, created_at);

CREATE TABLE market_schedule (
    schedule_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    created_by_admin_id INTEGER NOT NULL,
    day_of_week        VARCHAR(10) NOT NULL UNIQUE
        CHECK (day_of_week IN ('Monday','Tuesday','Wednesday','Thursday','Friday','Saturday','Sunday')),
    market_open_time   TIME      NOT NULL,
    market_close_time  TIME      NOT NULL,
    is_holiday         BOOLEAN   NOT NULL DEFAULT 0,
    created_at         TIMESTAMP NOT NULL,
    updated_at         TIMESTAMP NOT NULL,
    FOREIGN KEY (created_by_admin_id) REFERENCES users(user_id)
);

CREATE TABLE market_settings (
    setting_id    INTEGER PRIMARY KEY CHECK (setting_id = 1),
    override_mode VARCHAR(12) NOT NULL DEFAULT 'none'
        CHECK (override_mode IN ('none','force_open','force_closed')),
    updated_by    INTEGER   NULL,
    updated_at    TIMESTAMP NOT NULL,
    FOREIGN KEY (updated_by) REFERENCES users(user_id)
);
