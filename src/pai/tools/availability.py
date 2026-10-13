from __future__ import annotations

from datetime import date

from pai.availability import SCHEDULE_KINDS, add_override, effective_schedule, set_schedule
from pai.tools import Tool, ToolContext


async def update_availability(args: dict, ctx: ToolContext) -> str:
    day = date.fromisoformat(args["date"])
    kind = args["kind"]
    if kind == "off_work":
        add_override(ctx.conn, day, off_work=True)
        return f"Noted: off work on {day:%a %m/%d}."
    start, end = args.get("start"), args.get("end")
    if not (start and end):
        raise ValueError("busy needs start and end (HH:MM)")
    add_override(ctx.conn, day, busy=(start, end), why=args.get("why", ""))
    return f"Noted: busy {day:%a %m/%d} {start}-{end}."


def _describe(blocks) -> list[str]:
    return [
        f"{b.name + ': ' if b.name else ''}{', '.join(b.days)} {b.start}-{b.end}" for b in blocks
    ]


async def get_weekly_schedule(args: dict, ctx: ToolContext) -> dict:
    s = effective_schedule(ctx.conn, ctx.config)
    return {k: _describe(getattr(s, k)) for k in SCHEDULE_KINDS}


async def set_weekly_schedule(args: dict, ctx: ToolContext) -> str:
    blocks = set_schedule(ctx.conn, args["kind"], args["blocks"])
    shown = "; ".join(_describe(blocks)) or "none"
    return f"Regular {args['kind']} schedule is now: {shown}. Future plans will use it."


_BLOCK = {
    "type": "object",
    "properties": {
        "name": {"type": "string", "description": "e.g. the class name"},
        "days": {
            "type": "array",
            "items": {"type": "string", "enum": ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]},
        },
        "start": {"type": "string", "description": "HH:MM 24h"},
        "end": {"type": "string", "description": "HH:MM 24h"},
    },
    "required": ["days", "start", "end"],
    "additionalProperties": False,
}

TOOLS = [
    Tool(
        name="update_availability",
        description=(
            "Record a one-off change to the owner's week for planning: a day off work, or a "
            "busy stretch (appointment, extra shift). Doesn't change the regular schedule."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "date": {"type": "string", "description": "YYYY-MM-DD"},
                "kind": {"type": "string", "enum": ["off_work", "busy"]},
                "start": {"type": "string", "description": "HH:MM, for busy"},
                "end": {"type": "string", "description": "HH:MM, for busy"},
                "why": {"type": "string"},
            },
            "required": ["date", "kind"],
            "additionalProperties": False,
        },
        fn=update_availability,
    ),
    Tool(
        name="get_weekly_schedule",
        description=(
            "The owner's regular weekly schedule: work hours, class meeting times and "
            "preferred study windows."
        ),
        input_schema={"type": "object", "properties": {}, "additionalProperties": False},
        fn=get_weekly_schedule,
    ),
    Tool(
        name="set_weekly_schedule",
        description=(
            "Change the regular weekly schedule when the owner tells you it changed (new job "
            "hours, an in-person or live remote class, different study times). Replaces the "
            "whole list for that kind, so call get_weekly_schedule first and send the full "
            "new list. Use an empty list to clear it (e.g. all classes are self-paced online)."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "kind": {"type": "string", "enum": list(SCHEDULE_KINDS)},
                "blocks": {"type": "array", "items": _BLOCK},
            },
            "required": ["kind", "blocks"],
            "additionalProperties": False,
        },
        fn=set_weekly_schedule,
    ),
]
