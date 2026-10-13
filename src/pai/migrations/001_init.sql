-- Short chat history: only final user/assistant text, never tool calls.
CREATE TABLE messages (
    id         INTEGER PRIMARY KEY,
    role       TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
    text       TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE TABLE api_usage (
    id                INTEGER PRIMARY KEY,
    model             TEXT NOT NULL,
    input_tokens      INTEGER NOT NULL DEFAULT 0,
    output_tokens     INTEGER NOT NULL DEFAULT 0,
    cache_read_tokens INTEGER NOT NULL DEFAULT 0,
    cost_usd          REAL NOT NULL DEFAULT 0,
    job               TEXT,
    created_at        TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
CREATE INDEX api_usage_created_at ON api_usage (created_at);

CREATE TABLE reminders (
    id         INTEGER PRIMARY KEY,
    text       TEXT NOT NULL,
    due_at     TEXT NOT NULL,
    status     TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'sent', 'cancelled')),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
CREATE INDEX reminders_pending ON reminders (status, due_at);

-- Latest Canvas snapshot.
CREATE TABLE canvas_items (
    id        TEXT PRIMARY KEY,
    course_id TEXT NOT NULL,
    title     TEXT NOT NULL,
    due_at    TEXT,
    points    REAL,
    html_url  TEXT,
    submitted INTEGER NOT NULL DEFAULT 0,
    seen_at   TEXT NOT NULL
);

-- For grade change detection.
CREATE TABLE grades (
    assignment_id TEXT PRIMARY KEY,
    course_id     TEXT NOT NULL,
    score         REAL,
    grade         TEXT,
    graded_at     TEXT,
    notified_at   TEXT
);

CREATE TABLE study_plans (
    id         INTEGER PRIMARY KEY,
    week_start TEXT NOT NULL,
    plan_json  TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

-- "I'm off Thursday"
CREATE TABLE availability_overrides (
    week_start TEXT PRIMARY KEY,
    json       TEXT NOT NULL
);

CREATE TABLE kv (
    key   TEXT PRIMARY KEY,
    value TEXT
);
