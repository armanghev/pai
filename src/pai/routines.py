"""Recurring jobs from chat. Pai proposes one, the owner confirms with a button, and it runs
on schedule through the normal tool loop on Haiku: no chat history, a per-run cost cap,
the daily budget guard, and silent delivery during quiet hours."""

from __future__ import annotations

import re
import sqlite3

from pai.config import DAYS

MAX_ROUTINES = 10
RUN_COST_LIMIT = 0.05  # dollars per run; the loop stops once a run has spent this much
WEEKDAYS = DAYS[:5]


def parse_days(days: list[str]) -> list[str]:
    picked = {d.strip().lower()[:3] for d in days}
    if "dai" in picked or "eve" in picked:  # "daily", "every day"
        return list(DAYS)
    if "wee" in picked:  # "weekdays"
        picked = (picked - {"wee"}) | set(WEEKDAYS)
    bad = picked - set(DAYS)
    if bad or not picked:
        raise ValueError(f"days must be mon..sun, 'daily' or 'weekdays', not {sorted(bad)}")
    return [d for d in DAYS if d in picked]


def parse_time(at: str) -> str:
    m = re.fullmatch(r"([01]?\d|2[0-3]):([0-5]\d)", at.strip())
    if not m:
        raise ValueError("time must be HH:MM, 24-hour")
    return f"{int(m.group(1)):02d}:{m.group(2)}"


def describe(days: list[str], at: str) -> str:
    if days == list(DAYS):
        when = "every day"
    elif days == WEEKDAYS:
        when = "weekdays"
    else:
        when = "every " + ", ".join(d.capitalize() for d in days)
    return f"{when} at {at}"


def propose(conn: sqlite3.Connection, title: str, prompt: str, days: list[str], at: str) -> int:
    """Store a routine awaiting the owner's confirmation. Raises ValueError if invalid."""
    count = conn.execute("SELECT COUNT(*) FROM routines WHERE status != 'pending'").fetchone()[0]
    if count >= MAX_ROUTINES:
        raise ValueError(f"there are already {MAX_ROUTINES} recurring jobs; delete one in /jobs")
    picked, at = parse_days(days), parse_time(at)
    cur = conn.execute(
        "INSERT INTO routines (title, prompt, days, at) VALUES (?, ?, ?, ?)",
        (" ".join(title.split())[:60], prompt.strip()[:1000], ",".join(picked), at),
    )
    return cur.lastrowid


def get(conn: sqlite3.Connection, rid: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM routines WHERE id = ?", (rid,)).fetchone()


def listed(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM routines WHERE status != 'pending' ORDER BY id").fetchall()


def set_status(conn: sqlite3.Connection, rid: int, status: str) -> None:
    conn.execute("UPDATE routines SET status = ? WHERE id = ?", (status, rid))


def delete(conn: sqlite3.Connection, rid: int) -> None:
    conn.execute("DELETE FROM routines WHERE id = ?", (rid,))


def days_of(row: sqlite3.Row) -> list[str]:
    return row["days"].split(",")


def run_prompt(row: sqlite3.Row) -> str:
    return (
        f'Scheduled job "{row["title"]}" ({describe(days_of(row), row["at"])}) is running now. '
        "Do this and reply with just the result for the owner, short. Don't create, change or "
        f"cancel scheduled jobs in this run.\n\n{row['prompt']}"
    )
