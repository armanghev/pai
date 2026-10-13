from __future__ import annotations

from pai import prices
from pai.tools import Tool, ToolContext


async def watch_price(args: dict, ctx: ToolContext) -> str:
    url, target = args["url"], args.get("target")
    price = await prices.fetch(url)
    if price is None:
        return "I couldn't read a price on that page (some stores, like Amazon, block bots)."
    wid = prices.add(ctx.conn, url, price, target)
    goal = f" I'll ping you at ${target:,.2f} or less." if target else " I'll ping you if it drops."
    return f"Watching #{wid} {price.title or url}: ${price.amount:,.2f} now.{goal}"


TOOLS = [
    Tool(
        name="watch_price",
        description=(
            "Start watching a product page's price (checked daily). Alerts on any drop, or "
            "when it reaches the target price if one is given. Read-only: never buys anything."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "url": {"type": "string"},
                "target": {"type": "number", "description": "Alert at or below this price"},
            },
            "required": ["url"],
            "additionalProperties": False,
        },
        fn=watch_price,
    )
]
