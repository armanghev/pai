-- Habit check-ins: one row per habit per local day answered.
CREATE TABLE habits (
    id         INTEGER PRIMARY KEY,
    name       TEXT NOT NULL UNIQUE COLLATE NOCASE,
    active     INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
CREATE TABLE habit_checks (
    habit_id INTEGER NOT NULL REFERENCES habits (id),
    day      TEXT NOT NULL,                 -- local date, YYYY-MM-DD
    done     INTEGER NOT NULL,
    PRIMARY KEY (habit_id, day)
);

-- Real study time, from /focus.
CREATE TABLE focus_sessions (
    id         INTEGER PRIMARY KEY,
    label      TEXT NOT NULL DEFAULT '',
    started_at TEXT NOT NULL,
    planned_minutes INTEGER NOT NULL,
    minutes    INTEGER,                      -- set when it ends
    status     TEXT NOT NULL DEFAULT 'running' CHECK (status IN ('running', 'done', 'stopped'))
);
CREATE INDEX focus_started ON focus_sessions (started_at);

-- Spaced-repetition flashcards.
CREATE TABLE flashcards (
    id          INTEGER PRIMARY KEY,
    deck        TEXT NOT NULL COLLATE NOCASE,
    front       TEXT NOT NULL,
    back        TEXT NOT NULL,
    ease        REAL NOT NULL DEFAULT 2.5,
    interval    INTEGER NOT NULL DEFAULT 0,  -- days
    reps        INTEGER NOT NULL DEFAULT 0,
    due_on      TEXT NOT NULL,               -- local date
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
CREATE INDEX flashcards_due ON flashcards (deck, due_on);

-- Businesses the lead finder already suggested, so it doesn't repeat them.
CREATE TABLE leads (
    id         INTEGER PRIMARY KEY,
    name       TEXT NOT NULL COLLATE NOCASE,
    category   TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
