from __future__ import annotations

from datetime import UTC, datetime

from pai.spend import utc_iso
from pai.tools import Tool, ToolContext


def parse_when(value: str, tz) -> datetime:
    """ISO 8601; a time without an offset is taken as local time."""
    dt = datetime.fromisoformat(value)
    return dt.replace(tzinfo=tz) if dt.tzinfo is None else dt


async def add_reminder(args: dict, ctx: ToolContext) -> str:
    when = parse_when(args["when"], ctx.config.tz)
    if when <= datetime.now(UTC):
        raise ValueError(f"{when.isoformat()} is in the past; ask the owner for a future time")
    cur = ctx.conn.execute(
        "INSERT INTO reminders (text, due_at) VALUES (?, ?)", (args["text"], utc_iso(when))
    )
    rid = cur.lastrowid
    if ctx.schedule_reminder:
        ctx.schedule_reminder(rid, when)
    local = when.astimezone(ctx.config.tz)
    return f"Reminder #{rid} set for {local:%a %b %d at %H:%M}."


def pending(conn) -> list:
    return conn.execute(
        "SELECT id, text, due_at FROM reminders WHERE status = 'pending' ORDER BY due_at"
    ).fetchall()


async def list_reminders(args: dict, ctx: ToolContext) -> dict:
    tz = ctx.config.tz
    return {
        "pending": [
            {
                "id": r["id"],
                "text": r["text"],
                "when": datetime.fromisoformat(r["due_at"].replace("Z", "+00:00"))
                .astimezone(tz)
                .strftime("%a %Y-%m-%d %H:%M"),
            }
            for r in pending(ctx.conn)
        ]
    }


async def cancel_reminder(args: dict, ctx: ToolContext) -> str:
    rid = int(args["id"])
    cur = ctx.conn.execute(
        "UPDATE reminders SET status = 'cancelled' WHERE id = ? AND status = 'pending'", (rid,)
    )
    if cur.rowcount == 0:
        raise LookupError(f"no pending reminder #{rid}")
    if ctx.unschedule_reminder:
        ctx.unschedule_reminder(rid)
    return f"Cancelled reminder #{rid}."


TOOLS = [
    Tool(
        name="add_reminder",
        description=(
            "Schedule a Telegram reminder. Work out the exact time from the owner's words and "
            "the current date in their message. Survives restarts."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "What to remind them of."},
                "when": {
                    "type": "string",
                    "description": "ISO 8601 local time, e.g. 2026-10-16T18:00",
                },
            },
            "required": ["text", "when"],
            "additionalProperties": False,
        },
        fn=add_reminder,
    ),
    Tool(
        name="list_reminders",
        description="List pending reminders with their ids and times.",
        input_schema={"type": "object", "properties": {}, "additionalProperties": False},
        fn=list_reminders,
    ),
    Tool(
        name="cancel_reminder",
        description="Cancel a pending reminder by id (get ids from list_reminders).",
        input_schema={
            "type": "object",
            "properties": {"id": {"type": "integer"}},
            "required": ["id"],
            "additionalProperties": False,
        },
        fn=cancel_reminder,
    ),
]
