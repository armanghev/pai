"""Long-term memory: durable facts and preferences, sent with every chat request.

They ride in the newest user message rather than the system prompt, so the cached prefix
(system prompt and tools) stays byte-stable when a memory is added or forgotten.
"""

from __future__ import annotations

import sqlite3

CATEGORIES = ("preference", "school", "work", "schedule", "health", "people", "projects", "other")
MAX_FACTS = 40
MAX_CHARS = 2000
MAX_FACT_CHARS = 200


def add(conn: sqlite3.Connection, fact: str, category: str = "other", source: str = "") -> int:
    """Store a fact; an identical one (ignoring case and spacing) is reused, not duplicated."""
    fact = " ".join(fact.split())[:MAX_FACT_CHARS]
    if not fact:
        raise ValueError("empty memory")
    category = category if category in CATEGORIES else "other"
    for row in conn.execute("SELECT id, fact FROM memories"):
        if row["fact"].lower() == fact.lower():
            return row["id"]
    cur = conn.execute(
        "INSERT INTO memories (fact, category, source) VALUES (?, ?, ?)",
        (fact, category, source[:300]),
    )
    return cur.lastrowid


def forget(conn: sqlite3.Connection, memory_id: int) -> str | None:
    """Delete one memory; returns its text, or None if there was no such id."""
    row = conn.execute("SELECT fact FROM memories WHERE id = ?", (memory_id,)).fetchone()
    if row is None:
        return None
    conn.execute("DELETE FROM memories WHERE id = ?", (memory_id,))
    return row["fact"]


def find(conn: sqlite3.Connection, text: str) -> list[sqlite3.Row]:
    words = text.lower().split()
    return [r for r in all_(conn) if all(w in r["fact"].lower() for w in words)]


def all_(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM memories ORDER BY id").fetchall()


def block(conn: sqlite3.Connection) -> str:
    """The memory list for a request: the newest facts within the count and size caps, in
    the order they were saved. Empty when there is nothing to remember."""
    rows = conn.execute(
        "SELECT id, fact FROM memories ORDER BY id DESC LIMIT ?", (MAX_FACTS,)
    ).fetchall()
    lines, size = [], 0
    for r in rows:
        line = f"- #{r['id']} {r['fact']}"
        if size + len(line) > MAX_CHARS:
            break
        lines.append(line)
        size += len(line) + 1
    if not lines:
        return ""
    return "[What you remember about the owner]\n" + "\n".join(sorted(lines, key=_id))


def _id(line: str) -> int:
    return int(line.split()[1].lstrip("#"))
