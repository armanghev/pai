from __future__ import annotations

from datetime import UTC, datetime

from pai.planner import make_plan, rest_of_week
from pai.tools import Tool, ToolContext


async def make_study_plan(args: dict, ctx: ToolContext) -> str:
    now = datetime.now(UTC)
    start, end = rest_of_week(now, ctx.config.tz)
    text = await make_plan(ctx, start, end, now)
    await ctx.send(text)
    return "The plan was sent to the owner as its own message. Reply with one short line."


TOOLS = [
    Tool(
        name="make_study_plan",
        description=(
            "Rebuild the study plan from now through Sunday using fresh Canvas data and the "
            "owner's free time, and send it to them. Use when they say 'replan', their week "
            "changed, or they ask for a plan. Record schedule changes with update_availability "
            "first."
        ),
        input_schema={"type": "object", "properties": {}, "additionalProperties": False},
        fn=make_study_plan,
    )
]
