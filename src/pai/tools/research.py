from __future__ import annotations

from datetime import datetime

from pai.tools import Tool, ToolContext

RESEARCH_SYSTEM = (
    "You answer research questions for a busy person reading on their phone. Search the web, "
    "then give a short, direct answer (under 150 words) followed by 2-4 source links as a "
    "plain list. Say so when sources disagree or the answer is uncertain."
)
WEB_SEARCH = {"type": "web_search_20260209", "name": "web_search", "max_uses": 5}


async def search_web(args: dict, ctx: ToolContext) -> str:
    if ctx.agent is None:
        raise RuntimeError("research needs the agent")
    now = datetime.now(ctx.config.tz)
    city = ctx.config.weather.city
    near = f" The asker lives near {city}." if city else ""
    context = f"(Today is {now:%A %Y-%m-%d}.{near})"
    return await ctx.agent.complete(
        "planner",
        RESEARCH_SYSTEM,
        f"{context}\n\n{args['question']}",
        job="research",
        tools=[WEB_SEARCH],
    )


TOOLS = [
    Tool(
        name="search_web",
        description=(
            "Research a question on the web and get a short cited answer. Use for current "
            "events, facts you're unsure of, prices, or 'look up ...'. Also summarizes a URL "
            "if the question includes one."
        ),
        input_schema={
            "type": "object",
            "properties": {"question": {"type": "string"}},
            "required": ["question"],
            "additionalProperties": False,
        },
        fn=search_web,
    )
]
