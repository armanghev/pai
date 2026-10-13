"""Habit check-ins: a daily yes/no per habit and streaks. No model calls."""

from __future__ import annotations

import sqlite3
from datetime import date, timedelta


def add(conn: sqlite3.Connection, name: str) -> str:
    name = " ".join(name.split())[:60]
    if not name:
        raise ValueError("give the habit a name")
    conn.execute(
        "INSERT INTO habits (name) VALUES (?) ON CONFLICT (name) DO UPDATE SET active = 1",
        (name,),
    )
    return name


def remove(conn: sqlite3.Connection, name: str) -> bool:
    cur = conn.execute(
        "UPDATE habits SET active = 0 WHERE name = ? AND active = 1", (name.strip(),)
    )
    return cur.rowcount > 0


def active(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT id, name FROM habits WHERE active = 1 ORDER BY id").fetchall()


def record(conn: sqlite3.Connection, habit_id: int, day: date, done: bool) -> None:
    conn.execute(
        "INSERT INTO habit_checks (habit_id, day, done) VALUES (?, ?, ?) "
        "ON CONFLICT (habit_id, day) DO UPDATE SET done = excluded.done",
        (habit_id, day.isoformat(), int(done)),
    )


def streak(conn: sqlite3.Connection, habit_id: int, today: date) -> int:
    """Consecutive done days ending today (or yesterday, if today isn't answered yet)."""
    rows = conn.execute(
        "SELECT day, done FROM habit_checks WHERE habit_id = ? AND day <= ? ORDER BY day DESC",
        (habit_id, today.isoformat()),
    ).fetchall()
    answers = {r["day"]: bool(r["done"]) for r in rows}
    d = today if today.isoformat() in answers else today - timedelta(days=1)
    n = 0
    while answers.get(d.isoformat()):
        n += 1
        d -= timedelta(days=1)
    return n


def week_report(conn: sqlite3.Connection, today: date) -> str:
    habits = active(conn)
    if not habits:
        return "No habits yet. Add one with /habits add Gym"
    start = today - timedelta(days=6)
    lines = ["*Habit week*"]
    for h in habits:
        rows = conn.execute(
            "SELECT day, done FROM habit_checks WHERE habit_id = ? AND day BETWEEN ? AND ?",
            (h["id"], start.isoformat(), today.isoformat()),
        ).fetchall()
        done = {r["day"] for r in rows if r["done"]}
        dots = "".join(
            "🟩" if (start + timedelta(days=i)).isoformat() in done else "⬜" for i in range(7)
        )
        s = streak(conn, h["id"], today)
        lines.append(
            f"{dots} *{h['name']}*: {len(done)}/7" + (f", {s}-day streak 🔥" if s >= 3 else "")
        )
    return "\n".join(lines)
