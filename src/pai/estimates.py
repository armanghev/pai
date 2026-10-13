"""Learned time estimates: how long each kind of work really takes compared with the plan.

A /focus session is tied to an assignment when it runs during that assignment's plan
block or its label matches one. Once the assignment is turned in (or a day past due),
its planned minutes (from the fullest plan that had it) and its logged minutes become a
sample. With 3+ samples of a kind, the median actual/planned ratio, clamped to
0.5x-2.5x, scales future plans. Plain rules, no model calls.
"""

from __future__ import annotations

import json
import re
import sqlite3
import statistics
from datetime import datetime, timedelta

from pai.canvas import Assignment

KINDS = ("essay", "discussion", "quiz", "lab", "reading", "other")
RULES = [  # first match wins: a "Peer Review Discussion" for an essay is a discussion
    ("quiz", r"\bquiz|\bexam\b|\btest\b|midterm"),  # "final" alone is often a draft
    ("discussion", r"discussion|forum|\bpost\b|\brepl(y|ies)\b|peer review"),
    ("essay", r"essay|paper|draft|reflection|writing|outline|thesis|\bspeech\b"),
    ("reading", r"reading|chapter|\bread\b|textbook"),
    ("lab", r"\blab\b|program|coding|\bcode\b|project|exercise"),
]
TECH_COURSE = re.compile(r"\bCS\b|\bCS/|CIS|\bIT\b|comput|program", re.I)
MIN_SAMPLES = 3
LOW, HIGH = 0.5, 2.5
NOTE_THRESHOLD = 0.15  # only mention a pace that's at least 15% off
LABELS = {
    "essay": "essays",
    "discussion": "discussions",
    "quiz": "quiz prep",
    "lab": "labs and coding",
    "reading": "readings",
    "other": "other work",
}


def kind(course: str, title: str) -> str:
    text = f"{title}"
    for name, pattern in RULES:
        if re.search(pattern, text, re.I):
            return name
    return "lab" if TECH_COURSE.search(course) else "other"


def _words(text: str) -> list[str]:
    return [w for w in re.sub(r"[^a-z0-9]+", " ", text.lower()).split() if len(w) > 1]


def tie(plan, label: str, now: datetime, tz) -> int | None:
    """The assignment a focus session is for: a plan block running now (or starting within
    30 minutes), else the nearest upcoming block whose course and task match the label."""
    if plan is None:
        return None
    local = now.astimezone(tz)
    blocks = [b for b in plan.blocks if b.assignment_id is not None]
    words = _words(label)

    def matches(b) -> bool:
        hay = _words(f"{b.course} {b.task}")
        hits = sum(any(h.startswith(w) for h in hay) for w in words)
        return bool(words) and hits / len(words) >= 0.6

    def start(b) -> datetime:
        return datetime.combine(b.day, b.start, tz)

    def end(b) -> datetime:
        return datetime.combine(b.day, b.end, tz)

    live = [b for b in blocks if start(b) - timedelta(minutes=30) <= local <= end(b)]
    for b in live:
        if not words or matches(b):
            return b.assignment_id
    later = sorted((b for b in blocks if end(b) >= local and matches(b)), key=start)
    return later[0].assignment_id if later else None


def planned_minutes(conn: sqlite3.Connection, assignment_id: int, recent: int = 30) -> int:
    """Minutes the fullest recent plan gave this assignment."""
    best = 0
    for row in conn.execute(
        "SELECT plan_json FROM study_plans ORDER BY id DESC LIMIT ?", (recent,)
    ):
        blocks = json.loads(row["plan_json"]).get("blocks", [])
        total = 0
        for b in blocks:
            if b.get("assignment_id") == assignment_id:
                s, e = (datetime.strptime(b[k][:5], "%H:%M") for k in ("start", "end"))
                total += int((e - s).total_seconds() // 60)
        best = max(best, total)
    return best


def collect(conn: sqlite3.Connection, items: list[Assignment], now: datetime) -> int:
    """Record a sample for each tied assignment that's finished. Returns how many."""
    by_id = {a.id: a for a in items}
    have = {r[0] for r in conn.execute("SELECT assignment_id FROM estimate_samples")}
    rows = conn.execute(
        "SELECT assignment_id, SUM(minutes) AS actual FROM focus_sessions"
        " WHERE assignment_id IS NOT NULL AND status != 'running' GROUP BY assignment_id"
    ).fetchall()
    added = 0
    for r in rows:
        a = by_id.get(r["assignment_id"])
        if a is None or a.id in have or not r["actual"]:
            continue
        if not (a.submitted or (a.due_at and a.due_at < now - timedelta(days=1))):
            continue
        estimated = planned_minutes(conn, a.id)
        if estimated <= 0:
            continue
        conn.execute(
            "INSERT INTO estimate_samples (assignment_id, kind, estimated, actual)"
            " VALUES (?, ?, ?, ?)",
            (a.id, kind(a.course, a.title), estimated, r["actual"]),
        )
        added += 1
    return added


def ratios(conn: sqlite3.Connection) -> dict[str, tuple[float, int]]:
    """kind -> (median actual/planned, sample count), for kinds with enough samples."""
    per: dict[str, list[float]] = {}
    for r in conn.execute("SELECT kind, estimated, actual FROM estimate_samples"):
        per.setdefault(r["kind"], []).append(r["actual"] / r["estimated"])
    return {
        k: (min(HIGH, max(LOW, statistics.median(v))), len(v))
        for k, v in per.items()
        if len(v) >= MIN_SAMPLES
    }


def notes(learned: dict[str, tuple[float, int]], kinds_in_plan: set[str]) -> list[str]:
    """One line per kind in this plan whose pace is noticeably off."""
    out = []
    for k, (r, _) in sorted(learned.items()):
        if k in kinds_in_plan and abs(r - 1) >= NOTE_THRESHOLD:
            out.append(f"{LABELS[k]}: you take ~{r:.1f}x your estimates, plan adjusted")
    return out


def summary(learned: dict[str, tuple[float, int]]) -> list[str]:
    """Lines for /review."""
    return [
        f"• {LABELS[k]}: {r:.1f}x planned ({n} samples)" for k, (r, n) in sorted(learned.items())
    ]
