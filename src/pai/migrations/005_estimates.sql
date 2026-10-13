-- Learned time estimates: focus sessions tied to an assignment, and per-assignment
-- planned vs actual minutes once the work is done.
ALTER TABLE focus_sessions ADD COLUMN assignment_id INTEGER;

CREATE TABLE estimate_samples (
    assignment_id INTEGER PRIMARY KEY,
    kind          TEXT NOT NULL,
    estimated     INTEGER NOT NULL,
    actual        INTEGER NOT NULL,
    recorded_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
