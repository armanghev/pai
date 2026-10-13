"""Heartbeat check-ins: every ~45 minutes, code computes a few compact facts from the
owner's checklist and a tiny Haiku call decides whether any of it is worth a message.

Facts carry keys; anything flagged in the last TTL is left out, so nothing repeats, and
things other jobs already alert on (forwarded inbox messages, errors already pinged) are
excluded by construction. No facts and no owner-added items means no model call at all.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, time, timedelta

from pydantic import BaseModel

from pai.canvas import Assignment
from pai.spend import utc_iso

TTL = timedelta(hours=6)  # after a message about it
QUIET_TTL = timedelta(hours=2)  # judged not worth a message: look again later
SOON = timedelta(hours=4)
SPEND_HEADROOM = 0.9  # skip beats once today's spend is past 90% of the daily cap
MAX_ITEMS = 15
SYSTEM = (
    "You are Pai's heartbeat: a quick background check for a busy student. You get facts "
    "computed from their checklist. Decide if anything is worth interrupting them for right "
    "now. If not, return ok=true and an empty message. If so, return ok=false and one short, "
    "warm Telegram message (at most 3 lines) that covers only what matters, and list the "
    "fact keys you used. Never repeat the facts as a list; never invent anything."
)


class Beat(BaseModel):
    ok: bool
    message: str
    keys: list[str]


@dataclass
class Fact:
    key: str
    text: str


def items(conn: sqlite3.Connection, enabled_only: bool = True) -> list[sqlite3.Row]:
    q = "SELECT * FROM heartbeat_items" + (" WHERE enabled = 1" if enabled_only else "")
    return conn.execute(q + " ORDER BY id").fetchall()


def add_item(conn: sqlite3.Connection, text: str) -> int:
    if len(items(conn, enabled_only=False)) >= MAX_ITEMS:
        raise ValueError(f"the checklist already has {MAX_ITEMS} items; remove one first")
    text = " ".join(text.split())[:200]
    return conn.execute("INSERT INTO heartbeat_items (text) VALUES (?)", (text,)).lastrowid


def find_item(conn: sqlite3.Connection, words: str) -> list[sqlite3.Row]:
    ws = words.lower().split()
    return [r for r in items(conn, False) if all(w in r["text"].lower() for w in ws)]


def set_item(conn: sqlite3.Connection, item_id: int, enabled: bool | None) -> None:
    """Built-in items are switched off; owner-added ones are deleted when removed."""
    row = conn.execute("SELECT kind FROM heartbeat_items WHERE id = ?", (item_id,)).fetchone()
    if row and row["kind"] is None and enabled is None:
        conn.execute("DELETE FROM heartbeat_items WHERE id = ?", (item_id,))
    else:
        conn.execute(
            "UPDATE heartbeat_items SET enabled = ? WHERE id = ?", (int(bool(enabled)), item_id)
        )


def fresh(conn: sqlite3.Connection, facts: list[Fact], now: datetime) -> list[Fact]:
    seen = {
        r["key"]
        for r in conn.execute("SELECT key FROM heartbeat_seen WHERE until > ?", (utc_iso(now),))
    }
    return [f for f in facts if f.key not in seen]


def mark(conn: sqlite3.Connection, keys: list[str], now: datetime, ttl: timedelta = TTL) -> None:
    until = utc_iso(now + ttl)
    conn.executemany(
        "INSERT INTO heartbeat_seen (key, until) VALUES (?, ?)"
        " ON CONFLICT (key) DO UPDATE SET until = excluded.until",
        [(k, until) for k in keys],
    )
    conn.execute("DELETE FROM heartbeat_seen WHERE until <= ?", (utc_iso(now),))


def _hm(delta: timedelta) -> str:
    m = max(int(delta.total_seconds() // 60), 0)
    return f"{m // 60}h {m % 60:02d}m" if m >= 60 else f"{m}m"


def facts(
    conn: sqlite3.Connection,
    kinds: set[str],
    now: datetime,
    tz,
    assignments: list[Assignment] | None,
    worked: set[int],
    unread: list[dict] | None,
    forwarded: set[str],
    errors: list,
    shift_end: str,
    workday: bool,
) -> list[Fact]:
    """Compact facts for the enabled built-in items. `worked` holds assignment ids with
    focus time logged; `forwarded` the inbox ids the inbox job already sent."""
    out: list[Fact] = []
    local = now.astimezone(tz)
    todo = [a for a in assignments or [] if a.due_at and a.due_at > now and a.needs_submission]
    if "deadlines_soon" in kinds:
        for a in todo:
            if a.due_at - now <= SOON:
                started = "some time logged" if a.id in worked else "no time logged"
                out.append(
                    Fact(
                        f"soon:{a.id}",
                        f"due in {_hm(a.due_at - now)}: {a.course} {a.title} ({started})",
                    )
                )
    if "due_today_untouched" in kinds:
        for a in todo:
            same_day = a.due_at.astimezone(tz).date() == local.date()
            if same_day and a.due_at - now > SOON and a.id not in worked:
                out.append(
                    Fact(
                        f"untouched:{a.id}",
                        f"due today {a.due_at.astimezone(tz):%H:%M}, no time "
                        f"logged yet: {a.course} {a.title}",
                    )
                )
    if "break_after_shift" in kinds and workday:
        end = datetime.combine(local.date(), time.fromisoformat(shift_end), tz)
        if end + timedelta(hours=1, minutes=30) <= local <= local.replace(hour=21, minute=0):
            out.append(
                Fact(
                    f"break:{local.date()}",
                    f"shift ended {_hm(local - end)} ago; Pai can't see "
                    "meals or breaks, so a light one-time nudge is fine if nothing else matters",
                )
            )
    if "inbox_unforwarded" in kinds:
        for m in unread or []:
            if str(m["id"]) not in forwarded:
                out.append(
                    Fact(
                        f"inbox:{m['id']}",
                        f"unread Canvas message from {m['from']}: {m['subject']}",
                    )
                )
    if "errors" in kinds:
        for r in errors:
            out.append(Fact(f"err:{r.id}", f"Pai error in {r.logger}: {r.message[:150]}"))
    return out


def request(fresh: list[Fact], custom: list[str], context: list[str], now: datetime, tz) -> str:
    lines = [f"Now: {now.astimezone(tz):%A %H:%M}"]
    if fresh:
        lines += ["Facts:", *(f"- [{f.key}] {f.text}" for f in fresh)]
    if custom:
        lines += ["Also watch for (the owner's own checklist items):", *(f"- {c}" for c in custom)]
        lines += ["Context for those:", *(f"- {c}" for c in context or ["(nothing else known)"])]
    return "\n".join(lines)


def forwarded_ids(raw: str | None) -> set[str]:
    return set(json.loads(raw)) if raw else set()
