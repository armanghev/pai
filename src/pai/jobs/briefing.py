"""Morning briefing and the after-work "Shift's over" recap. Templated, no model calls."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from pai.canvas import Assignment, is_overdue
from pai.hn import format_lines
from pai.planner import StudyPlan

log = logging.getLogger(__name__)


def build(
    now: datetime,
    tz,
    weather: str | None,
    items: list[Assignment] | None,
    plan: StudyPlan | None,
    reminders: list[tuple[datetime, str]],
    stories: list[dict] | None = None,
) -> str:
    local = now.astimezone(tz)
    today = local.date()
    out = [f"*Good morning!* 🦉 {local:%A, %b} {local.day}"]
    if weather:
        out.append(f"🌤 {weather}")

    if items is None:
        out.append("\n_(Couldn't reach Canvas this morning.)_")
    else:
        overdue = [a for a in items if is_overdue(a, now)]
        due_today = [
            a
            for a in items
            if a.due_at and a.due_at.astimezone(tz).date() == today and a.needs_submission
        ]
        soon = [
            a
            for a in items
            if a.due_at
            and today < a.due_at.astimezone(tz).date() <= today + timedelta(days=3)
            and a.needs_submission
        ]
        if overdue:
            out.append("\n*Overdue*")
            out += [f"• {a.course}: {a.title}" for a in overdue]
        if due_today:
            out.append("\n*Due today*")
            out += [f"• {a.due_at.astimezone(tz):%H:%M} {a.course}: {a.title}" for a in due_today]
        if soon:
            out.append("\n*Next 3 days*")
            out += [f"• {a.due_at.astimezone(tz):%a} {a.course}: {a.title}" for a in soon]
        if not (overdue or due_today or soon):
            out.append("\nNothing due in the next 3 days.")

    blocks = [b for b in (plan.blocks if plan else []) if b.day == today]
    if blocks:
        out.append("\n*Study today*")
        out += [f"• {b.start:%H:%M}–{b.end:%H:%M} {b.course}: {b.task}" for b in blocks]

    todays = [(t, text) for t, text in reminders if t.astimezone(tz).date() == today]
    if todays:
        out.append("\n*Reminders*")
        out += [f"• {t.astimezone(tz):%H:%M} {text}" for t, text in todays]

    if stories:
        out.append("\n*Hacker News*")
        out += format_lines(stories)
    return "\n".join(out)


def shift_over(
    now: datetime,
    tz,
    items: list[Assignment] | None,
    plan: StudyPlan | None,
    reminders: list[tuple[datetime, str]],
    changes: list[str],
) -> str:
    """After work: what's due tonight, Canvas changes since the morning, tonight's plan and
    reminders. Money goes in its own message (pai.money.recap), numbers behind spoilers."""
    local = now.astimezone(tz)
    today = local.date()
    out = [f"*Shift's over* 🦉 {local:%A, %b} {local.day}"]
    if items is None:
        out.append("\n_(Couldn't reach Canvas just now.)_")
    else:
        tonight = sorted(
            (
                a
                for a in items
                if a.due_at
                and a.due_at > now
                and a.due_at.astimezone(tz).date() == today
                and a.needs_submission
            ),
            key=lambda a: a.due_at,
        )
        if tonight:
            out.append("\n*Due tonight*")
            out += [f"• {a.due_at.astimezone(tz):%H:%M} {a.course}: {a.title}" for a in tonight]
        else:
            out.append("\nNothing due tonight. ✅")
    if changes:
        out.append("\n*Canvas changes since this morning*")
        out += [f"• {c}" for c in changes[-10:]]
    blocks = [
        b
        for b in (plan.blocks if plan else [])
        if b.day == today and datetime.combine(b.day, b.end, tz) > now
    ]
    if blocks:
        out.append("\n*Tonight's plan*")
        out += [f"• {b.start:%H:%M}–{b.end:%H:%M} {b.course}: {b.task}" for b in blocks]
    later = [(t, text) for t, text in reminders if t > now and t.astimezone(tz).date() == today]
    if later:
        out.append("\n*Reminders tonight*")
        out += [f"• {t.astimezone(tz):%H:%M} {text}" for t, text in later]
    return "\n".join(out)
