-- Long-term memory: durable facts and preferences the owner has shared.
CREATE TABLE memories (
    id         INTEGER PRIMARY KEY,
    fact       TEXT NOT NULL,
    category   TEXT NOT NULL DEFAULT 'other',
    source     TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
