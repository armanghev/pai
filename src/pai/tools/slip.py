"""replan_tonight: rework the rest of tonight after the owner slips."""

from __future__ import annotations

from datetime import UTC, datetime

from pai import slip
from pai.tools import Tool, ToolContext


async def replan_tonight(args: dict, ctx: ToolContext) -> str:
    text = await slip.replan_tonight(
        ctx,
        datetime.now(UTC),
        delay=max(0, min(int(args.get("delay_minutes", 0)), 240)),
        missed=bool(args.get("missed", False)),
    )
    return text + "\n(Saved. Pass this on as is, in one message.)"


TOOLS = [
    Tool(
        name="replan_tonight",
        description=(
            "When the owner says they're behind on tonight's study plan ('running 1h late', "
            "'missed my study block', 'starting 30 min late'), rework just the rest of tonight: "
            "work due tonight first, flexible work moved to later days. Saves the new plan."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "delay_minutes": {
                    "type": "integer",
                    "description": "How late they'll start, e.g. 60 for 'an hour late'.",
                },
                "missed": {
                    "type": "boolean",
                    "description": "True if they missed a block that already started.",
                },
            },
            "additionalProperties": False,
        },
        fn=replan_tonight,
    )
]
