"""Grade and deadline watch: a pure diff against the last Canvas snapshot. No Claude calls.

Besides grades and overdue work it reports Canvas changes, grouped into one message: new
assignments, due dates moved, set or removed, points changed, and assignments removed or
unpublished. Changes to work more than a week past due are ignored."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta

from pai import db
from pai.canvas import Assignment, is_overdue
from pai.spend import utc_iso

INIT_KEY = "grade_watch_initialized"
STALE = timedelta(days=7)  # changes to work this far past due aren't worth a ping
OVERDUE_KEY = "overdue_notified"
CHANGES_KEY = "canvas_changes_log"  # recent change lines, for the after-work recap
KEEP_CHANGES = 100


def _fmt_due(dt: datetime, tz) -> str:
    return dt.astimezone(tz).strftime("%a %m/%d %H:%M")


def _parse(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value.replace("Z", "+00:00")) if value else None


def _score(a: Assignment) -> str:
    if a.points:
        return f"{a.score:g}/{a.points:g}"
    return a.grade or f"{a.score:g}"


def diff(conn: sqlite3.Connection, items: list[Assignment], now: datetime, tz) -> list[str]:
    """Compare `items` with the stored snapshot, update it, and return alert messages.

    The very first run only records a baseline, so old grades don't flood in.
    """
    first_run = db.kv_get(conn, INIT_KEY) is None
    known = {
        r["id"]: r
        for r in conn.execute("SELECT id, course_id, title, due_at, points FROM canvas_items")
    }
    grades = {r["assignment_id"]: r for r in conn.execute("SELECT * FROM grades")}
    overdue_sent = set(json.loads(db.kv_get(conn, OVERDUE_KEY, "[]")))
    alerts: list[str] = []
    changes: list[str] = []
    seen = utc_iso(now)

    def recent(*dues: datetime | None) -> bool:
        """At least one of these due dates is unset, upcoming or less than a week past."""
        return any(d is None or d > now - STALE for d in dues)

    conn.execute("BEGIN")
    try:
        for a in items:
            key = str(a.id)
            due = utc_iso(a.due_at) if a.due_at else None

            # grades
            if a.score is not None and a.graded_at is not None:
                old = grades.get(key)
                changed = old is None or old["score"] != a.score or old["grade"] != a.grade
                if changed:
                    if not first_run:
                        verb = (
                            "Grade posted"
                            if old is None or old["score"] is None
                            else "Grade changed"
                        )
                        alerts.append(f"🦉 {verb}: *{a.course}* {a.title}: {_score(a)}")
                    conn.execute(
                        "INSERT INTO grades (assignment_id, course_id, score, grade, graded_at,"
                        " notified_at) VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT (assignment_id) DO"
                        " UPDATE SET score = excluded.score, grade = excluded.grade,"
                        " graded_at = excluded.graded_at, notified_at = excluded.notified_at",
                        (key, str(a.course_id), a.score, a.grade, utc_iso(a.graded_at), seen),
                    )

            # Canvas changes
            prev = known.get(key)
            old_due = _parse(prev["due_at"]) if prev else None
            if first_run:
                pass
            elif prev is None:
                if a.needs_submission and (a.due_at is None or a.due_at > now):
                    when = f"due {_fmt_due(a.due_at, tz)}" if a.due_at else "no due date"
                    pts = f", {a.points:g} pts" if a.points else ""
                    changes.append(f"🆕 New: *{a.course}* {a.title} ({when}{pts})")
            elif recent(old_due, a.due_at):
                if prev["due_at"] != due and a.needs_submission:
                    if old_due and a.due_at:
                        what = f"due {_fmt_due(old_due, tz)} → {_fmt_due(a.due_at, tz)}"
                    elif a.due_at:
                        what = f"due date set: {_fmt_due(a.due_at, tz)}"
                    else:
                        what = f"due date removed (was {_fmt_due(old_due, tz)})"
                    changes.append(f"📅 *{a.course}* {a.title}: {what}")
                if (
                    prev["points"] is not None
                    and a.points is not None
                    and prev["points"] != a.points
                ):
                    changes.append(
                        f"🔢 *{a.course}* {a.title}: points {prev['points']:g} → {a.points:g}"
                    )

            # overdue, once each
            if is_overdue(a, now) and key not in overdue_sent:
                overdue_sent.add(key)
                if not first_run:
                    alerts.append(
                        f"⚠️ Past due and not submitted: *{a.course}* {a.title} "
                        f"(was due {_fmt_due(a.due_at, tz)})"
                    )

            conn.execute(
                "INSERT INTO canvas_items (id, course_id, title, due_at, points, html_url,"
                " submitted, seen_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT (id) DO UPDATE"
                " SET title = excluded.title, due_at = excluded.due_at, points = excluded.points,"
                " html_url = excluded.html_url, submitted = excluded.submitted,"
                " seen_at = excluded.seen_at",
                (key, str(a.course_id), a.title, due, a.points, a.html_url, int(a.submitted), seen),
            )
        # Gone from a course we still see: deleted or unpublished. Courses missing from this
        # fetch entirely (a failed request, a finished term) are left alone.
        courses = {str(a.course_id): a.course for a in items}
        current = {str(a.id) for a in items}
        for key, prev in known.items():
            if key in current or prev["course_id"] not in courses:
                continue
            old_due = _parse(prev["due_at"])
            if not first_run and recent(old_due):
                was = f" (was due {_fmt_due(old_due, tz)})" if old_due else ""
                course = courses[prev["course_id"]]
                changes.append(f"🗑 Removed or unpublished: *{course}* {prev['title']}{was}")
            conn.execute("DELETE FROM canvas_items WHERE id = ?", (key,))

        db.kv_set(conn, OVERDUE_KEY, json.dumps(sorted(overdue_sent)))
        db.kv_set(conn, INIT_KEY, seen)
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    if changes:
        alerts.append("📋 *Canvas changes*\n" + "\n".join(f"• {c}" for c in changes))
        log = json.loads(db.kv_get(conn, CHANGES_KEY, "[]"))
        log += [[seen, c] for c in changes]
        db.kv_set(conn, CHANGES_KEY, json.dumps(log[-KEEP_CHANGES:]))
    return alerts


def changes_since(conn: sqlite3.Connection, since: datetime) -> list[str]:
    """Change lines reported since `since`, oldest first."""
    cutoff = utc_iso(since)
    return [text for at, text in json.loads(db.kv_get(conn, CHANGES_KEY, "[]")) if at >= cutoff]
