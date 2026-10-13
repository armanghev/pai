-- Recurring jobs created from chat ("every Friday at 5pm send me ..."), confirmed by button.
CREATE TABLE routines (
    id          INTEGER PRIMARY KEY,
    title       TEXT NOT NULL,
    prompt      TEXT NOT NULL,
    days        TEXT NOT NULL,
    at          TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'active', 'paused')),
    last_run_at TEXT,
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
