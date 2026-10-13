"""Evening recap: tomorrow's work, today's plan vs logged study, a replan nudge. Templated."""

from __future__ import annotations

from datetime import datetime, timedelta

from pai.canvas import Assignment, is_overdue
from pai.planner import StudyPlan


def _minutes(b) -> int:
    return (b.end.hour * 60 + b.end.minute) - (b.start.hour * 60 + b.start.minute)


def build(
    now: datetime,
    tz,
    items: list[Assignment] | None,
    plan: StudyPlan | None,
    logged_today: int,
    reminders_tomorrow: list[tuple[datetime, str]],
) -> str:
    local = now.astimezone(tz)
    today, tomorrow = local.date(), local.date() + timedelta(days=1)
    out = ["*Evening recap* 🌙"]

    planned = [b for b in (plan.blocks if plan else []) if b.day == today]
    planned_min = sum(_minutes(b) for b in planned)
    slipped = False
    if planned:
        out.append(
            f"\nPlanned study today: {planned_min} min. Logged with /focus: {logged_today} min."
        )
        if logged_today < planned_min * 0.75:
            slipped = True
            out.append("Didn't get to:" if logged_today == 0 else "Some of it slipped:")
            out += [f"• {b.start:%H:%M} {b.course}: {b.task}" for b in planned]
    elif logged_today:
        out.append(f"\nYou logged {logged_today} min of focus today. Nice. 🦉")

    if items is None:
        out.append("\n_(Couldn't reach Canvas.)_")
    else:
        due = [
            a
            for a in items
            if a.due_at and a.due_at.astimezone(tz).date() == tomorrow and a.needs_submission
        ]
        overdue = [a for a in items if is_overdue(a, now)]
        if due:
            out.append("\n*Due tomorrow*")
            out += [f"• {a.due_at.astimezone(tz):%H:%M} {a.course}: {a.title}" for a in due]
        if overdue:
            out.append(f"\n⚠️ {len(overdue)} overdue item(s) still open. /due to see them.")
        if not due and not overdue:
            out.append("\nNothing due tomorrow.")

    tom = [(t, x) for t, x in reminders_tomorrow if t.astimezone(tz).date() == tomorrow]
    if tom:
        out.append("\n*Reminders tomorrow*")
        out += [f"• {t.astimezone(tz):%H:%M} {x}" for t, x in tom]

    if slipped:
        out.append("\nWant the rest of the week rebuilt around this? Send /replan.")
    return "\n".join(out)
