from pai import db

TABLES = {
    "messages",
    "api_usage",
    "reminders",
    "canvas_items",
    "grades",
    "study_plans",
    "availability_overrides",
    "kv",
    "memories",
}
LATEST = db._migrations()[-1][0]


def test_migrations_are_numbered_without_gaps():
    assert [v for v, _ in db._migrations()] == list(range(1, LATEST + 1))


def _tables(conn):
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    return {r["name"] for r in rows}


def test_migrations_create_schema(tmp_path):
    conn = db.connect(tmp_path / "pai.db")
    assert TABLES <= _tables(conn)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == LATEST
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


def test_migrate_is_idempotent(tmp_path):
    path = tmp_path / "pai.db"
    db.connect(path).close()
    conn = db.connect(path)
    assert db.migrate(conn) == LATEST


def test_connect_creates_parent_dir(tmp_path):
    path = tmp_path / "nested" / "data" / "pai.db"
    db.connect(path)
    assert path.exists()


def test_kv_roundtrip(tmp_path):
    conn = db.connect(tmp_path / "pai.db")
    assert db.kv_get(conn, "missing", "dflt") == "dflt"
    db.kv_set(conn, "last_grade_check", "a")
    db.kv_set(conn, "last_grade_check", "b")
    assert db.kv_get(conn, "last_grade_check") == "b"


def test_reminder_status_is_checked(tmp_path):
    import sqlite3

    import pytest

    conn = db.connect(tmp_path / "pai.db")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO reminders (text, due_at, status) VALUES ('x', '2026-10-10', 'bogus')"
        )
