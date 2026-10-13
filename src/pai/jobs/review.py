"""Sunday weekly review: what got done, how study time went, what's next. Templated."""

from __future__ import annotations

from datetime import datetime, timedelta

from pai.canvas import Assignment, Course, is_overdue
from pai.planner import StudyPlan


def _minutes(b) -> int:
    return (b.end.hour * 60 + b.end.minute) - (b.start.hour * 60 + b.start.minute)


def build(
    now: datetime,
    tz,
    courses: list[Course],
    items: list[Assignment],
    plan: StudyPlan | None,
    focus_minutes: int,
    habit_report: str | None,
    pace: list[str] | None = None,
) -> str:
    local = now.astimezone(tz)
    week_start = (local - timedelta(days=local.weekday())).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    next_start, next_end = week_start + timedelta(days=7), week_start + timedelta(days=14)
    out = [f"*Weekly review* 🦉 week of {week_start:%b} {week_start.day}"]

    done = [a for a in items if a.submitted and a.due_at and week_start <= a.due_at < next_start]
    graded = [a for a in items if a.graded_at and a.graded_at >= week_start]
    out.append(f"\n✅ Turned in {len(done)} assignment(s) due this week.")
    if graded:
        out.append("*Grades posted*")
        for a in sorted(graded, key=lambda a: a.graded_at)[:8]:
            score = f"{a.score:g}/{a.points:g}" if a.points else f"{a.score:g}"
            out.append(f"• {a.course} {a.title}: {score}")
    overdue = [a for a in items if is_overdue(a, now)]
    if overdue:
        out.append(f"⚠️ {len(overdue)} still open past due. /missing")

    planned = sum(_minutes(b) for b in plan.blocks) if plan else 0
    hours = f"{focus_minutes // 60}h {focus_minutes % 60}m"
    if planned:
        pct = round(focus_minutes / planned * 100)
        out.append(
            f"\n📚 Study: {hours} logged of {planned // 60}h {planned % 60}m planned ({pct}%)."
        )
    elif focus_minutes:
        out.append(f"\n📚 Study: {hours} logged with /focus.")

    if pace:
        out.append("\n⏱ *Your pace vs. plans*")
        out += pace

    if courses:
        out.append("\n*Grades now*")
        out += [
            f"• {c.code}: "
            + " ".join(
                x for x in (c.grade, f"({c.score:g}%)" if c.score is not None else None) if x
            )
            for c in courses
        ]

    if habit_report:
        out.append("\n" + habit_report)

    upcoming = [
        a
        for a in items
        if a.due_at and next_start <= a.due_at.astimezone(tz) < next_end and a.needs_submission
    ]
    if upcoming:
        biggest = max(upcoming, key=lambda a: a.points or 0)
        out.append(
            f"\n*Next week:* {len(upcoming)} item(s) due. Biggest: {biggest.course} "
            f"{biggest.title}" + (f" ({biggest.points:g} pts)" if biggest.points else "")
        )
    else:
        out.append("\n*Next week:* nothing posted on Canvas yet.")

    ends = [c.end_at for c in courses if c.end_at and c.end_at > now]
    if ends:
        end = min(ends)
        days = (end - now).days
        out.append(f"🗓 {days // 7} weeks ({days} days) left in the semester.")
    return "\n".join(out)
