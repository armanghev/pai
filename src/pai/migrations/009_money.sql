-- Read-only money data from Plaid. Numbers live here and are only ever shown behind
-- Telegram spoilers; they never go to long-term memory, the heartbeat or /fix.
CREATE TABLE money_items (
    label           TEXT PRIMARY KEY,
    cursor          TEXT,
    last_error      TEXT,
    error_alerted   TEXT,
    synced_at       TEXT
);

CREATE TABLE money_accounts (
    account_id  TEXT PRIMARY KEY,
    item        TEXT NOT NULL,
    kind        TEXT NOT NULL,
    mask        TEXT,
    balance     REAL,
    available   REAL,
    credit_limit REAL,
    due_date    TEXT,
    minimum_due REAL,
    updated_at  TEXT NOT NULL
);

CREATE TABLE money_transactions (
    id         TEXT PRIMARY KEY,
    account_id TEXT NOT NULL,
    day        TEXT NOT NULL,
    amount     REAL NOT NULL,
    merchant   TEXT,
    category   TEXT,
    pending    INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX money_transactions_day ON money_transactions (day);

CREATE TABLE holdings_snapshots (
    day         TEXT NOT NULL,
    account_id  TEXT NOT NULL,
    security_id TEXT NOT NULL,
    ticker      TEXT,
    quantity    REAL,
    price       REAL,
    value       REAL NOT NULL,
    cost_basis  REAL,
    PRIMARY KEY (day, account_id, security_id)
);
