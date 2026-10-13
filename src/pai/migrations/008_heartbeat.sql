-- Heartbeat check-ins: the checklist (built-in items have a kind; owner-added ones don't)
-- and what's already been flagged, so the same thing isn't raised twice within a TTL.
CREATE TABLE heartbeat_items (
    id         INTEGER PRIMARY KEY,
    kind       TEXT,
    text       TEXT NOT NULL,
    enabled    INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

INSERT INTO heartbeat_items (kind, text) VALUES
    ('deadlines_soon', 'Deadlines in the next few hours not yet turned in'),
    ('due_today_untouched', 'Work due today with no time put in yet'),
    ('break_after_shift', 'No dinner or break after the shift'),
    ('inbox_unforwarded', 'Canvas inbox messages not yet forwarded'),
    ('errors', 'Failed jobs or errors since the last check');

CREATE TABLE heartbeat_seen (
    key   TEXT PRIMARY KEY,
    until TEXT NOT NULL
);
