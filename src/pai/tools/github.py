from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

from pai.tools import Tool, ToolContext


async def github_activity(args: dict, ctx: ToolContext) -> dict:
    if ctx.github is None:
        raise RuntimeError("GitHub is not configured (GITHUB_TOKEN in .env)")
    gh, cfg = ctx.github, ctx.config.github
    now = datetime.now(UTC)
    since = now - timedelta(days=int(args.get("days", 7)))
    repos = await gh.repos(cfg.repos, cfg.active_days, now)
    summaries = await asyncio.gather(*(gh.week_summary(r, since) for r in repos))
    failures = await asyncio.gather(*(gh.failed_runs(r["full_name"], since) for r in repos))
    return {"repos": list(summaries), "failed_runs": [f for group in failures for f in group]}


TOOLS = [
    Tool(
        name="github_activity",
        description=(
            "The owner's side projects on GitHub: commits, open PRs and issues, and failed CI "
            "runs over the last N days, per repo."
        ),
        input_schema={
            "type": "object",
            "properties": {"days": {"type": "integer", "description": "Default 7."}},
            "additionalProperties": False,
        },
        fn=github_activity,
    )
]
