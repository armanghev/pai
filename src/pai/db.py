"""SQLite connection, migrations runner and small query helpers."""

from __future__ import annotations

import re
import sqlite3
from importlib import resources
from pathlib import Path

MIGRATIONS_PACKAGE = "pai.migrations"


def connect(path: Path | str) -> sqlite3.Connection:
    """Open the database in WAL mode and apply any pending migrations."""
    if str(path) != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, isolation_level=None)  # autocommit; explicit BEGIN when needed
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")  # fewer fsyncs, easier on the SD card
    conn.execute("PRAGMA foreign_keys = ON")
    migrate(conn)
    return conn


def _migrations() -> list[tuple[int, str]]:
    found = []
    for entry in resources.files(MIGRATIONS_PACKAGE).iterdir():
        name = entry.name
        if name.endswith(".sql"):
            found.append((int(name.split("_", 1)[0]), entry.read_text()))
    return sorted(found)


def migrate(conn: sqlite3.Connection) -> int:
    """Apply migrations newer than PRAGMA user_version. Returns the resulting version."""
    current = conn.execute("PRAGMA user_version").fetchone()[0]
    for version, sql in _migrations():
        if version <= current:
            continue
        conn.execute("BEGIN")
        try:
            # Comments go first so a ';' inside one can't split a statement.
            for statement in re.sub(r"--[^\n]*", "", sql).split(";"):
                if statement.strip():
                    conn.execute(statement)
            conn.execute(f"PRAGMA user_version = {version}")
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        current = version
    return current


def kv_get(conn: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    row = conn.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def kv_set(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO kv (key, value) VALUES (?, ?) "
        "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
        (key, value),
    )
