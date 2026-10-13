"""Free study time, computed in code so the planner only fills real gaps.

Free = study windows - work - classes - one-off busy times, clipped to "now".
Per-week overrides live in availability_overrides as JSON:
    {"off_work": ["2026-10-16"], "busy": [{"date": "2026-10-16", "start": "12:00",
     "end": "15:00", "why": "dentist"}]}
An off-work weekday uses the Saturday study windows instead of the weekday ones.

The regular weekly schedule starts from config.toml; anything the owner changes by
texting Pai is stored in kv under SCHEDULE_KEY and wins over the file, per list.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from pai import db
from pai.config import DAYS, Block, Config, Schedule

Interval = tuple[int, int]  # minutes since midnight, [start, end)
SCHEDULE_KEY = "weekly_schedule"
SCHEDULE_KINDS = ("work", "classes", "study")


def effective_schedule(conn: sqlite3.Connection, config: Config) -> Schedule:
    saved = json.loads(db.kv_get(conn, SCHEDULE_KEY, "{}"))
    return config.schedule.model_copy(
        update={k: [Block.model_validate(b) for b in v] for k, v in saved.items()}
    )


def set_schedule(conn: sqlite3.Connection, kind: str, blocks: list[dict]) -> list[Block]:
    """Replace one list (work, classes or study) of the regular weekly schedule."""
    if kind not in SCHEDULE_KINDS:
        raise ValueError(f"kind must be one of {SCHEDULE_KINDS}")
    parsed = [Block.model_validate(b) for b in blocks]  # validates before saving
    saved = json.loads(db.kv_get(conn, SCHEDULE_KEY, "{}"))
    saved[kind] = [b.model_dump() for b in parsed]
    db.kv_set(conn, SCHEDULE_KEY, json.dumps(saved))
    return parsed


def week_start(d: date) -> date:
    return d - timedelta(days=d.weekday())


def _minutes(t: time | str) -> int:
    t = time.fromisoformat(t) if isinstance(t, str) else t
    return t.hour * 60 + t.minute


def _to_time(m: int) -> time:
    return time(m // 60, m % 60) if m < 24 * 60 else time(23, 59)


def subtract(windows: list[Interval], busy: list[Interval]) -> list[Interval]:
    out = []
    for ws, we in windows:
        pieces = [(ws, we)]
        for bs, be in busy:
            nxt = []
            for ps, pe in pieces:
                if be <= ps or bs >= pe:
                    nxt.append((ps, pe))
                    continue
                if bs > ps:
                    nxt.append((ps, bs))
                if be < pe:
                    nxt.append((be, pe))
            pieces = nxt
        out.extend(pieces)
    return sorted(out)


@dataclass(frozen=True)
class FreeBlock:
    day: date
    start: time
    end: time

    @property
    def minutes(self) -> int:
        return _minutes(self.end) - _minutes(self.start)

    def __str__(self) -> str:
        return f"{self.day:%a %m/%d} {self.start:%H:%M}-{self.end:%H:%M}"


@dataclass(frozen=True)
class BusyBlock:
    day: date
    start: time
    end: time
    what: str


def load_overrides(conn: sqlite3.Connection, start: date, end: date) -> dict:
    merged: dict = {"off_work": [], "busy": []}
    rows = conn.execute(
        "SELECT json FROM availability_overrides WHERE week_start BETWEEN ? AND ?",
        (week_start(start).isoformat(), week_start(end).isoformat()),
    ).fetchall()
    for r in rows:
        data = json.loads(r["json"])
        merged["off_work"].extend(data.get("off_work", []))
        merged["busy"].extend(data.get("busy", []))
    return merged


def add_override(
    conn: sqlite3.Connection,
    day: date,
    off_work: bool = False,
    busy: tuple[str, str] | None = None,
    why: str = "",
) -> None:
    ws = week_start(day).isoformat()
    row = conn.execute("SELECT json FROM availability_overrides WHERE week_start = ?", (ws,))
    found = row.fetchone()
    data = json.loads(found["json"]) if found else {"off_work": [], "busy": []}
    if off_work and day.isoformat() not in data["off_work"]:
        data["off_work"].append(day.isoformat())
    if busy:
        data["busy"].append({"date": day.isoformat(), "start": busy[0], "end": busy[1], "why": why})
    conn.execute(
        "INSERT INTO availability_overrides (week_start, json) VALUES (?, ?) "
        "ON CONFLICT (week_start) DO UPDATE SET json = excluded.json",
        (ws, json.dumps(data)),
    )


def busy_for_day(
    config: Config, d: date, overrides: dict, schedule: Schedule | None = None
) -> list[BusyBlock]:
    schedule = schedule or config.schedule
    dow = DAYS[d.weekday()]
    out = []
    if d.isoformat() not in overrides["off_work"]:
        for b in schedule.work:
            if dow in b.days:
                out.append(BusyBlock(d, b.start_time, b.end_time, b.name or "work"))
    for b in schedule.classes:
        if dow in b.days:
            out.append(BusyBlock(d, b.start_time, b.end_time, b.name or "class"))
    for b in overrides["busy"]:
        if b["date"] == d.isoformat():
            out.append(
                BusyBlock(
                    d,
                    time.fromisoformat(b["start"]),
                    time.fromisoformat(b["end"]),
                    b.get("why", ""),
                )
            )
    return out


def free_blocks(
    config: Config,
    start: date,
    end: date,
    overrides: dict,
    now: datetime | None = None,
    schedule: Schedule | None = None,
) -> list[FreeBlock]:
    """Free study blocks for each date in [start, end], in local time."""
    schedule = schedule or config.schedule
    local_now = now.astimezone(config.tz) if now else None
    out = []
    d = start
    while d <= end:
        dow = DAYS[d.weekday()]
        window_day = "sat" if d.isoformat() in overrides["off_work"] else dow
        windows = [
            (_minutes(b.start_time), _minutes(b.end_time))
            for b in schedule.study
            if window_day in b.days
        ]
        busy = [
            (_minutes(b.start), _minutes(b.end))
            for b in busy_for_day(config, d, overrides, schedule)
        ]
        if local_now and d == local_now.date():
            busy.append((0, _minutes(local_now.time()) + 1))
        elif local_now and d < local_now.date():
            windows = []
        for s, e in subtract(windows, busy):
            if e - s >= schedule.min_block_minutes:
                out.append(FreeBlock(d, _to_time(s), _to_time(e)))
        d += timedelta(days=1)
    return out
