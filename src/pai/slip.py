"""'I'm behind' replanning: rework only the rest of tonight, deterministically.

Tonight's remaining plan blocks (plus the one just missed, if any) become tasks. Work due
tonight is fitted first into tonight's real free time (the study windows already leave out
dinner, work, class and the bedtime cutoff). Flexible work that doesn't fit moves to the
earliest free time on a later day this week before it's due. What can't fit is said
plainly. No model call. Block lengths already carry any learned pace from the planner.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta

from pai.availability import FreeBlock, effective_schedule, free_blocks, load_overrides, week_start
from pai.planner import Block, StudyPlan, latest_plan

MIN_CHUNK = 15  # minutes; shorter leftovers aren't worth a session


@dataclass
class Task:
    assignment_id: int | None
    course: str
    task: str
    minutes: int
    due: datetime | None
    url: str | None = None


def _mins(t: time) -> int:
    return t.hour * 60 + t.minute


def _time(m: int) -> time:
    return time(m // 60, m % 60)


def _subtract(free: list[FreeBlock], taken: list[Block]) -> list[FreeBlock]:
    out = []
    for f in free:
        spans = [(_mins(f.start), _mins(f.end))]
        for b in taken:
            if b.day != f.day:
                continue
            bs, be = _mins(b.start), _mins(b.end)
            spans = [
                piece
                for s, e in spans
                for piece in ((s, min(e, bs)), (max(s, be), e))
                if piece[1] - piece[0] > 0
            ]
        out += [FreeBlock(f.day, _time(s), _time(e)) for s, e in spans]
    return out


def _place(task: Task, free: list[FreeBlock], tz, latest: datetime | None) -> list[Block]:
    """Fill `task.minutes` into `free` (mutated), earliest first, ending before `latest`."""
    placed, need = [], task.minutes
    for i, f in enumerate(free):
        if need < MIN_CHUNK:
            break
        start, end = _mins(f.start), _mins(f.end)
        if latest is not None:
            cap = datetime.combine(f.day, f.start, tz)
            if cap >= latest:
                continue
            if datetime.combine(f.day, f.end, tz) > latest:
                end = min(end, _mins(latest.astimezone(tz).time()))
        take = min(need, end - start)
        if take < MIN_CHUNK:
            continue
        placed.append(
            Block(
                day=f.day,
                start=_time(start),
                end=_time(start + take),
                course=task.course,
                task=task.task,
                assignment_id=task.assignment_id,
                url=task.url,
            )
        )
        free[i] = FreeBlock(f.day, _time(start + take), f.end)
        need -= take
    task.minutes = max(need, 0)
    return placed


def reschedule(
    plan: StudyPlan,
    dues: dict[int, datetime],
    tonight: list[FreeBlock],
    later: list[FreeBlock],
    now: datetime,
    tz,
    missed: bool = False,
) -> tuple[StudyPlan, list[str], list[str]]:
    """New plan, plus (what changed, what won't fit) as short phrases."""
    today = now.astimezone(tz).date()
    end_of_today = datetime.combine(today + timedelta(days=1), time(0), tz)

    def ends(b: Block) -> datetime:
        return datetime.combine(b.day, b.end, tz)

    todays = sorted((b for b in plan.blocks if b.day == today), key=lambda b: b.start)
    moving = [b for b in todays if ends(b) > now]
    if missed:  # the block that just ended without getting done comes back too
        ended = [b for b in todays if ends(b) <= now]
        if ended:
            moving.insert(0, ended[-1])
    if not moving:
        return plan, [], []

    tasks = [
        Task(
            b.assignment_id,
            b.course,
            b.task,
            _mins(b.end) - _mins(b.start),
            dues.get(b.assignment_id) if b.assignment_id is not None else None,
            b.url,
        )
        for b in moving
    ]
    tasks.sort(key=lambda t: (t.due is None or t.due > end_of_today, t.due or end_of_today))
    kept = [b for b in plan.blocks if b not in moving]
    tonight = list(tonight)
    later = _subtract([f for f in later if f.day > today], kept)

    new_blocks, changed, wont_fit = [], [], []
    for t in tasks:
        tonight_due = t.due is not None and t.due <= end_of_today
        latest = t.due if t.due and t.due > now else None
        placed = _place(t, tonight, tz, latest)
        if placed:
            first, last = placed[0], placed[-1]
            note = " (due tonight)" if tonight_due else ""
            changed.append(f"{t.course} {t.task} {first.start:%H:%M}–{last.end:%H:%M}{note}")
        if t.minutes >= MIN_CHUNK and not tonight_due:
            moved = _place(t, later, tz, t.due)
            if moved:
                days = list(dict.fromkeys(f"{b.day:%a}" for b in moved))
                changed.append(f"{t.course} {t.task} moved to {'/'.join(days)}")
            placed += moved
        if t.minutes >= MIN_CHUNK:
            due = f", due {t.due.astimezone(tz):%a %H:%M}" if t.due else ""
            wont_fit.append(f"{t.course} {t.task} ({t.minutes} min{due})")
        new_blocks += placed

    blocks = sorted(kept + new_blocks, key=lambda b: (b.day, b.start))
    risk = [f"Didn't fit after a slip: {w}" for w in wont_fit]
    return (
        StudyPlan(blocks=blocks, notes=plan.notes, at_risk=plan.at_risk + risk),
        changed,
        wont_fit,
    )


async def replan_tonight(ctx, now: datetime, delay: int = 0, missed: bool = False) -> str:
    """Rework tonight after a slip, save the plan, and return one short message."""
    cfg, tz = ctx.config, ctx.config.tz
    today = now.astimezone(tz).date()
    plan = latest_plan(ctx.conn, today)
    if plan is None:
        return "There's no plan for this week yet. /replan makes one."
    items = await ctx.canvas.all_assignments() if ctx.canvas else []
    dues = {a.id: a.due_at for a in items if a.due_at}
    end = week_start(today) + timedelta(days=6)
    schedule = effective_schedule(ctx.conn, cfg)
    overrides = load_overrides(ctx.conn, today, end)
    start_at = now + timedelta(minutes=delay)
    tonight = free_blocks(cfg, today, today, overrides, start_at, schedule)
    later = free_blocks(cfg, today + timedelta(days=1), end, overrides, now, schedule)
    new, changed, wont_fit = reschedule(plan, dues, tonight, later, now, tz, missed)
    if not changed and not wont_fit:
        return "Nothing left on tonight's plan to move. 🦉"
    ctx.conn.execute(
        "INSERT INTO study_plans (week_start, plan_json) VALUES (?, ?)",
        (week_start(today).isoformat(), new.model_dump_json()),
    )
    text = "New plan: " + ", ".join(changed) + "." if changed else "Couldn't place anything."
    if wont_fit:
        text += "\n⚠️ Won't fit before it's due: " + "; ".join(wont_fit) + "."
    return text
