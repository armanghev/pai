"""portfolio_snapshot: the owner's holdings for the model, opt-in, own chat only.

Used for analysis he asks for ("am I too heavy in tech?"). Plain lookups still go through
money_answer, which never shows the model any numbers. The reply to a turn that used this
is sent with numbers behind spoilers and isn't kept in chat history or memory.
"""

from __future__ import annotations

from datetime import UTC, datetime

from pai import money
from pai.tools import Tool, ToolContext


async def portfolio_snapshot(args: dict, ctx: ToolContext) -> str:
    if not ctx.portfolio_ok:
        return "Not available here; only in the owner's own chat."
    if not ctx.money_on:
        return "Money isn't connected (or is paused). Say so in one line."
    if not money.ai_on(ctx.conn):
        return "Holdings aren't shared with you right now (/money ai on turns it on). Say so."
    ctx.private_seen = True
    today = datetime.now(UTC).astimezone(ctx.config.tz).date()
    return money.portfolio_for_model(ctx.conn, today) + (
        "\nAnswer the question; don't save any of this with remember."
    )


TOOLS = [
    Tool(
        name="portfolio_snapshot",
        description=(
            "The owner's investment holdings (account type, ticker, name, asset type, shares, "
            "value, % weight, today's change) for portfolio analysis they ask for, like 'am I "
            "too heavy in tech?' or 'how diversified am I?'. For plain lookups ('what do I "
            "hold?', 'how much is my IRA?') use money_answer instead. No bank data."
        ),
        input_schema={"type": "object", "properties": {}, "additionalProperties": False},
        fn=portfolio_snapshot,
    )
]
