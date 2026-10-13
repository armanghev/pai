"""Weekly study planner: Canvas items + free blocks -> Sonnet structured plan -> validated."""

from __future__ import annotations

import json
import logging
from datetime import UTC, date, datetime, time, timedelta

from pydantic import BaseModel
from pydantic.json_schema import SkipJsonSchema

from pai import estimates, prompts
from pai.availability import (
    FreeBlock,
    effective_schedule,
    free_blocks,
    load_overrides,
    week_start,
)
from pai.canvas import Assignment, is_overdue, upcoming
from pai.focus import minutes_between, week_bounds

log = logging.getLogger(__name__)


class Block(BaseModel):
    day: date
    start: time
    end: time
    course: str
    task: str
    assignment_id: int | None
    # Filled in by code after planning (never asked of the model): the Canvas link.
    url: SkipJsonSchema[str | None] = None


class StudyPlan(BaseModel):
    blocks: list[Block]
    notes: list[str]
    at_risk: list[str]


def planner_prompt(config) -> str:
    return prompts.render("planner.md", config)


def validate(
    plan: StudyPlan, free: list[FreeBlock], items: list[Assignment], tz, now: datetime
) -> list[str]:
    """Return human-readable violations; empty means the plan is usable."""
    problems = []
    due = {a.id: a.due_at for a in items}
    for b in plan.blocks:
        label = f"{b.day} {b.start:%H:%M}-{b.end:%H:%M} ({b.task})"
        if b.start >= b.end:
            problems.append(f"{label}: start is not before end")
            continue
        if not any(f.day == b.day and f.start <= b.start and b.end <= f.end for f in free):
            problems.append(f"{label}: not inside any free block (overlaps work, class or busy)")
        if b.assignment_id is not None:
            if b.assignment_id not in due:
                problems.append(f"{label}: unknown assignment_id {b.assignment_id}")
            else:
                d = due[b.assignment_id]
                end_dt = datetime.combine(b.day, b.end, tz)
                if d and d >= now and end_dt > d:
                    problems.append(
                        f"{label}: ends after the assignment is due ({d.astimezone(tz)})"
                    )
    return problems


def _overlapping(blocks: list[Block]) -> list[str]:
    out = []
    by_day: dict[date, list[Block]] = {}
    for b in blocks:
        by_day.setdefault(b.day, []).append(b)
    for day, bs in by_day.items():
        bs.sort(key=lambda b: b.start)
        for a, b in zip(bs, bs[1:], strict=False):
            if b.start < a.end:
                out.append(f"{day} {a.start:%H:%M} and {b.start:%H:%M} sessions overlap")
    return out


def build_request(
    items: list[Assignment],
    free: list[FreeBlock],
    tz,
    now: datetime,
    logged: dict[str, int] | None = None,
    pace: dict[str, tuple[float, int]] | None = None,
) -> str:
    lines = [f"Now: {now.astimezone(tz):%A %Y-%m-%d %H:%M} ({tz.key})", "", "Upcoming work:"]
    if not items:
        lines.append("(nothing due)")
    for a in items:
        status = " OVERDUE" if is_overdue(a, now) else ""
        lines.append(
            f"- assignment_id={a.id} | {a.course} | {a.title} | due "
            f"{a.due_at.astimezone(tz):%a %m/%d %H:%M} | {a.points or 0:g} pts"
            f" | {estimates.kind(a.course, a.title)}{status}"
        )
    if pace:
        lines += ["", "Learned pace (their actual time vs. what past plans gave, by kind):"]
        lines += [
            f"- {k}: {r:.1f}x ({n} samples); scale session time for this kind by this"
            for k, (r, n) in sorted(pace.items())
        ]
    if logged:
        lines += ["", "Study already done this week (logged with a timer), by label:"]
        lines += [f"- {label}: {m} min" for label, m in logged.items()]
    lines += ["", "Free blocks (local time):"]
    lines += [
        f"- {f.day.isoformat()} {f.start:%H:%M}-{f.end:%H:%M} ({f.minutes} min)" for f in free
    ]
    if not free:
        lines.append("(none)")
    return "\n".join(lines)


def format_plan(plan: StudyPlan, title: str) -> str:
    out = [f"*{title}*"]
    day = None
    for b in sorted(plan.blocks, key=lambda b: (b.day, b.start)):
        if b.day != day:
            day = b.day
            out.append(f"\n*{b.day:%a %b} {b.day.day}*")
        task = f"[{b.task}]({b.url})" if b.url else b.task
        out.append(f"• {b.start:%H:%M}–{b.end:%H:%M} {b.course}: {task}")
    if not plan.blocks:
        out.append("\nNo study sessions needed. Enjoy the week.")
    if plan.at_risk:
        out.append("\n*At risk*")
        out += [f"• {r}" for r in plan.at_risk]
    if plan.notes:
        out.append("\n*Notes*")
        out += [f"• {n}" for n in plan.notes]
    return "\n".join(out)


async def make_plan(ctx, start: date, end: date, now: datetime | None = None) -> str:
    """Plan [start, end], save it, and return the Telegram text."""
    now = now or datetime.now(UTC)
    cfg, tz = ctx.config, ctx.config.tz
    if ctx.canvas is None:
        return "Canvas isn't set up yet (CANVAS_TOKEN in .env)."
    all_items = await ctx.canvas.all_assignments(fresh=True)
    horizon = (datetime.combine(end, time(23, 59), tz) - now).days + 7  # look a week past the end
    items = upcoming(all_items, now, max(horizon, 7))
    schedule = effective_schedule(ctx.conn, cfg)
    free = free_blocks(cfg, start, end, load_overrides(ctx.conn, start, end), now, schedule)

    logged = minutes_between(ctx.conn, *week_bounds(now, tz))
    pace = estimates.ratios(ctx.conn)
    request = build_request(items, free, tz, now, logged, pace)
    plan = await ctx.agent.structured(
        "planner", planner_prompt(cfg), request, StudyPlan, job="study_plan"
    )
    problems = validate(plan, free, items, tz, now) + _overlapping(plan.blocks)
    if problems:
        log.info("plan had %d problems; retrying once", len(problems))
        retry = (
            request
            + "\n\nYour previous plan broke these rules; fix them:\n"
            + "\n".join(f"- {p}" for p in problems)
        )
        plan = await ctx.agent.structured(
            "planner", planner_prompt(cfg), retry, StudyPlan, job="study_plan"
        )
        problems = validate(plan, free, items, tz, now)
        if problems:  # drop whatever is still invalid rather than send a wrong plan
            log.warning("dropping invalid blocks after retry: %s", problems)
            plan.blocks = [
                b
                for b in plan.blocks
                if not validate(StudyPlan(blocks=[b], notes=[], at_risk=[]), free, items, tz, now)
            ]
        kept: list[Block] = []
        for b in sorted(plan.blocks, key=lambda b: (b.day, b.start)):
            if not kept or not _overlapping([kept[-1], b]):
                kept.append(b)
        plan.blocks = kept

    planned_kinds = {
        estimates.kind(a.course, a.title)
        for a in items
        if any(b.assignment_id == a.id for b in plan.blocks)
    }
    plan.notes = estimates.notes(pace, planned_kinds) + plan.notes
    urls = {a.id: a.html_url for a in items if a.html_url}
    for b in plan.blocks:
        b.url = urls.get(b.assignment_id) if b.assignment_id is not None else None

    ctx.conn.execute(
        "INSERT INTO study_plans (week_start, plan_json) VALUES (?, ?)",
        (week_start(start).isoformat(), plan.model_dump_json()),
    )
    title = f"Study plan {start:%a %m/%d} – {end:%a %m/%d}"
    return format_plan(plan, title)


def next_week(now: datetime, tz) -> tuple[date, date]:
    today = now.astimezone(tz).date()
    start = week_start(today) + timedelta(days=7)
    return start, start + timedelta(days=6)


def rest_of_week(now: datetime, tz) -> tuple[date, date]:
    today = now.astimezone(tz).date()
    return today, week_start(today) + timedelta(days=6)


def latest_plan(conn, today: date) -> StudyPlan | None:
    row = conn.execute(
        "SELECT plan_json FROM study_plans WHERE week_start = ? ORDER BY id DESC LIMIT 1",
        (week_start(today).isoformat(),),
    ).fetchone()
    return StudyPlan.model_validate(json.loads(row["plan_json"])) if row else None
