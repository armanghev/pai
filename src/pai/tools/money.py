"""money_answer: the owner's money questions, answered by SQL with numbers behind spoilers.

The tool sends the answer itself and hands the model no amounts, so nothing about money
ends up in a reply, in chat history or in long-term memory.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from pai import money
from pai.tools import Tool, ToolContext

PERIODS = ("today", "yesterday", "this_week", "last_week", "this_month", "last_month")


def period_bounds(period: str, today):
    if period == "yesterday":
        d = today - timedelta(days=1)
        return d, d
    if period in ("this_week", "last_week"):
        start = today - timedelta(days=today.weekday())
        if period == "last_week":
            return start - timedelta(days=7), start - timedelta(days=1)
        return start, today
    if period in ("this_month", "last_month"):
        start = today.replace(day=1)
        if period == "last_month":
            end = start - timedelta(days=1)
            return end.replace(day=1), end
        return start, today
    return today, today


async def money_answer(args: dict, ctx: ToolContext) -> str:
    if ctx.send_html is None or not ctx.money_on:
        return "Money isn't connected (or is paused with /money off). Say so in one line."
    today = datetime.now(UTC).astimezone(ctx.config.tz).date()
    start, end = period_bounds(args.get("period", "today"), today)
    await ctx.send_html(money.answer(ctx.conn, args["topic"], start, end, today))
    return (
        "Sent the answer as its own message with the amounts hidden behind spoilers. Reply in "
        "one short line with no numbers, and don't save anything about money to memory."
    )


TOOLS = [
    Tool(
        name="money_answer",
        description=(
            "Answer a question about the owner's money (read-only Plaid data): spending over a "
            "period, bank balances, the credit card (owed, minimum, due date), investments "
            "(account values and today's change) or holdings (each stock, ETF, fund or crypto "
            "position with shares, value and today's change). Sends the numbers itself, hidden "
            "behind spoilers."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "topic": {
                    "type": "string",
                    "enum": [
                        "spending",
                        "balances",
                        "credit",
                        "investments",
                        "holdings",
                        "overview",
                    ],
                },
                "period": {"type": "string", "enum": list(PERIODS)},
            },
            "required": ["topic"],
            "additionalProperties": False,
        },
        fn=money_answer,
    )
]
