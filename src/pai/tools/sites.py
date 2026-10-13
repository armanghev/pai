from __future__ import annotations

import asyncio

from pai.sites import check
from pai.tools import Tool, ToolContext


async def check_site(args: dict, ctx: ToolContext) -> dict:
    timeout = ctx.config.monitor.timeout_seconds
    urls = [args["url"]] if args.get("url") else [s.url for s in ctx.config.monitor.sites]
    if not urls:
        return {"results": [], "note": "No sites are configured under [monitor] in config.toml."}
    return {"results": list(await asyncio.gather(*(check(u, timeout) for u in urls)))}


TOOLS = [
    Tool(
        name="check_site",
        description=(
            "Check whether a website is up (HTTP status and response time). With no url, "
            "checks every watched site."
        ),
        input_schema={
            "type": "object",
            "properties": {"url": {"type": "string"}},
            "additionalProperties": False,
        },
        fn=check_site,
    )
]
