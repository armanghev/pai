"""Chat tools for link summaries and flashcards (the same features as the commands)."""

from __future__ import annotations

from datetime import datetime

from pai import flashcards, links
from pai.tools import Tool, ToolContext


async def summarize_link(args: dict, ctx: ToolContext) -> str:
    return await links.summarize(ctx.agent, args["url"])


async def make_flashcards(args: dict, ctx: ToolContext) -> str:
    today = datetime.now(ctx.config.tz).date()
    n = await flashcards.make(ctx.agent, ctx.conn, args["deck"], args["notes"], today)
    return f"Made {n} cards in deck {args['deck']!r}. They can review with /quiz {args['deck']}."


TOOLS = [
    Tool(
        name="summarize_link",
        description=(
            "Fetch a web page or PDF (article, class reading) and return a short summary. Use "
            "when the owner shares a link and asks what it says. Not for YouTube links."
        ),
        input_schema={
            "type": "object",
            "properties": {"url": {"type": "string"}},
            "required": ["url"],
            "additionalProperties": False,
        },
        fn=summarize_link,
    ),
    Tool(
        name="make_flashcards",
        description=(
            "Turn class notes the owner pasted into a deck of spaced-repetition flashcards. "
            "Pass their notes text verbatim. Pick a short deck name (e.g. the course code) if "
            "they didn't give one."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "deck": {"type": "string"},
                "notes": {"type": "string"},
            },
            "required": ["deck", "notes"],
            "additionalProperties": False,
        },
        fn=make_flashcards,
    ),
]
