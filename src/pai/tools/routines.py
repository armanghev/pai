"""create_routine: propose a recurring job; the owner confirms it with a button."""

from __future__ import annotations

from pai import routines
from pai.tools import Tool, ToolContext


async def create_routine(args: dict, ctx: ToolContext) -> str:
    if ctx.send_buttons is None:
        raise RuntimeError("can't ask for confirmation from here")
    rid = routines.propose(ctx.conn, args["title"], args["prompt"], args["days"], args["time"])
    row = routines.get(ctx.conn, rid)
    when = routines.describe(routines.days_of(row), row["at"])
    await ctx.send_buttons(
        f"🔁 New recurring job: *{row['title']}*, {when}\n_{row['prompt']}_\n\nCreate it?",
        [[("✅ Create", f"j:ok:{rid}"), ("✖️ Cancel", f"j:no:{rid}")]],
    )
    return (
        "Showed the owner the job with Create/Cancel buttons. It isn't active until they tap "
        "Create, so don't say it's set up. Reply in one short line."
    )


TOOLS = [
    Tool(
        name="create_routine",
        description=(
            "Set up a recurring job from chat, e.g. 'every Friday at 5pm send me what's due "
            "next week' or 'weekdays at 7:30 check the weather and my first class'. Shows the "
            "owner the job with a Create button; it only runs after they confirm. The prompt "
            "runs on schedule through your normal tools. Not for one-time reminders (use "
            "add_reminder). They manage existing jobs with /jobs."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "A few words, e.g. 'Friday due list'"},
                "prompt": {
                    "type": "string",
                    "description": "What to do each run, as an instruction to yourself.",
                },
                "days": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "mon..sun, or ['daily'] or ['weekdays'].",
                },
                "time": {"type": "string", "description": "HH:MM, 24-hour local time."},
            },
            "required": ["title", "prompt", "days", "time"],
            "additionalProperties": False,
        },
        fn=create_routine,
    )
]
