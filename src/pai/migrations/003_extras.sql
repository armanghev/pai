-- Price watch: product pages checked once a day.
CREATE TABLE price_watches (
    id         INTEGER PRIMARY KEY,
    url        TEXT NOT NULL,
    title      TEXT NOT NULL DEFAULT '',
    target     REAL,                      -- alert at or below this, NULL means any drop
    last_price REAL,
    currency   TEXT NOT NULL DEFAULT 'USD',
    active     INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
