"""Study session tracking for /focus. Real minutes feed the evening recap and /replan."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta

from pai.availability import week_start
from pai.spend import utc_iso


def _ts(v: str) -> datetime:
    return datetime.fromisoformat(v.replace("Z", "+00:00"))


def running(conn: sqlite3.Connection) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM focus_sessions WHERE status = 'running' ORDER BY id DESC LIMIT 1"
    ).fetchone()


def start(
    conn: sqlite3.Connection,
    minutes: int,
    label: str,
    now: datetime,
    assignment_id: int | None = None,
) -> int:
    if not 5 <= minutes <= 240:
        raise ValueError("pick between 5 and 240 minutes")
    if running(conn):
        raise RuntimeError("a session is already running; /stop it first")
    cur = conn.execute(
        "INSERT INTO focus_sessions (label, started_at, planned_minutes, assignment_id)"
        " VALUES (?, ?, ?, ?)",
        (label.strip()[:80], utc_iso(now), minutes, assignment_id),
    )
    return cur.lastrowid


def finish(conn: sqlite3.Connection, sid: int, now: datetime, stopped: bool = False) -> int:
    """Close a running session; returns minutes logged (capped at the planned length)."""
    row = conn.execute(
        "SELECT * FROM focus_sessions WHERE id = ? AND status = 'running'", (sid,)
    ).fetchone()
    if row is None:
        return 0
    elapsed = int((now - _ts(row["started_at"])).total_seconds() // 60)
    minutes = max(0, min(elapsed, row["planned_minutes"]))
    conn.execute(
        "UPDATE focus_sessions SET minutes = ?, status = ? WHERE id = ?",
        (minutes, "stopped" if stopped else "done", sid),
    )
    return minutes


def minutes_between(conn: sqlite3.Connection, start: datetime, end: datetime) -> dict[str, int]:
    """Logged minutes per label for sessions started in [start, end)."""
    rows = conn.execute(
        "SELECT label, SUM(minutes) AS m FROM focus_sessions WHERE status != 'running' "
        "AND started_at >= ? AND started_at < ? GROUP BY label",
        (utc_iso(start), utc_iso(end)),
    ).fetchall()
    return {r["label"] or "general": r["m"] or 0 for r in rows}


def day_bounds(now: datetime, tz) -> tuple[datetime, datetime]:
    local = now.astimezone(tz).replace(hour=0, minute=0, second=0, microsecond=0)
    return local, local + timedelta(days=1)


def week_bounds(now: datetime, tz) -> tuple[datetime, datetime]:
    today = now.astimezone(tz).date()
    start = datetime.combine(week_start(today), datetime.min.time(), tz)
    return start, start + timedelta(days=7)


def week_summary(conn: sqlite3.Connection, now: datetime | None, tz) -> str:
    now = now or datetime.now(UTC)
    per = minutes_between(conn, *week_bounds(now, tz))
    if not per:
        return "No focus time logged this week yet."
    total = sum(per.values())
    lines = [f"Logged this week: {total // 60}h {total % 60}m"]
    lines += [f"• {label}: {m} min" for label, m in sorted(per.items(), key=lambda kv: -kv[1])]
    return "\n".join(lines)
