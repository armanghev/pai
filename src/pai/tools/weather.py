from __future__ import annotations

from pai.tools import Tool, ToolContext
from pai.weather import forecast


async def get_weather(args: dict, ctx: ToolContext) -> dict:
    w = ctx.config.weather
    return await forecast(
        ctx.conn, args.get("city") or w.city, ctx.config.timezone, w.units, args.get("days", 1)
    )


TOOLS = [
    Tool(
        name="get_weather",
        description=(
            "Current weather and the daily forecast (up to 7 days). Defaults to the owner's "
            "home city."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "days": {"type": "integer", "description": "1-7, default 1"},
                "city": {"type": "string", "description": "Only if they ask about elsewhere."},
            },
            "additionalProperties": False,
        },
        fn=get_weather,
    )
]
