"""Second-brain vault tools: capture to the inbox, and answer questions about projects."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from pai import vault
from pai.tools import Tool, ToolContext

NOT_SET = {"error": "No vault configured. Set [vault] path in config.toml."}
BODY_LIMIT = 6000


async def _projects(ctx: ToolContext) -> list[vault.Project] | None:
    v = ctx.config.vault
    if v.path is None:
        return None
    if v.git_sync:
        await vault.pull(v.path)
    return vault.load_projects(v.path)


async def capture_to_vault(args: dict, ctx: ToolContext) -> dict:
    v = ctx.config.vault
    if v.path is None:
        return NOT_SET
    if v.git_sync:
        await vault.pull(v.path, max_age=0)
    now = datetime.now(UTC).astimezone(ctx.config.tz)
    rel = vault.capture(v.path, args["text"], now, v.inbox)
    message = f"Pai: Telegram capture {now:%Y-%m-%d %H:%M}"
    if v.git_sync and not await vault.push(v.path, rel, message):
        return {"saved_to": str(rel), "warning": "Saved, but couldn't push to GitHub yet."}
    return {"saved_to": str(rel)}


async def list_projects(args: dict, ctx: ToolContext) -> dict:
    projects = await _projects(ctx)
    if projects is None:
        return NOT_SET
    status = args.get("status")
    rows = [p for p in vault.by_priority(projects) if not status or p.status == status]
    return {"projects": [p.summary() for p in rows]}


async def get_project(args: dict, ctx: ToolContext) -> dict:
    projects = await _projects(ctx)
    if projects is None:
        return NOT_SET
    p = vault.find(projects, args["name"])
    if p is None:
        return {
            "error": f"No single project matches {args['name']!r}.",
            "projects": [x.name for x in projects],
        }
    body = p.body if len(p.body) <= BODY_LIMIT else p.body[:BODY_LIMIT] + "\n…(cut)"
    return {**p.summary(), "open_items": p.open_items, "note": body}


async def project_activity(args: dict, ctx: ToolContext) -> dict:
    projects = await _projects(ctx)
    if projects is None:
        return NOT_SET
    days = int(args.get("days", 7))
    now = datetime.now(UTC)
    since = now - timedelta(days=days)
    counts = await vault.commit_counts(projects, since, ctx.github)
    return {
        "days": days,
        "commits": {
            p.name: {"status": p.status, "commits": counts[p.name]}
            for p in vault.by_priority(projects)
            if counts.get(p.name)
        },
        "ship_log": vault.shipped_since(projects, since.astimezone(ctx.config.tz).date()),
    }


TOOLS = [
    Tool(
        name="capture_to_vault",
        description=(
            "Save an idea, todo or thought to the owner's second-brain vault inbox (Obsidian). "
            "Use this when they say to capture, jot down or add something to their vault or "
            "second brain, or share an app/feature idea. save_note is for quick facts instead."
        ),
        input_schema={
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
            "additionalProperties": False,
        },
        fn=capture_to_vault,
    ),
    Tool(
        name="list_projects",
        description=(
            "The owner's software projects from their vault: status (active, personal, paused, "
            "stalled, shipped), priority, next milestone, ship window and milestone progress."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "status": {
                    "type": "string",
                    "enum": ["active", "personal", "paused", "stalled", "shipped", "archived"],
                }
            },
            "additionalProperties": False,
        },
        fn=list_projects,
    ),
    Tool(
        name="get_project",
        description=(
            "One project's full vault note: roadmap, locked milestone items (done and open), "
            "parking lot, ship log and open questions. Use for 'what's left on X?'."
        ),
        input_schema={
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "required": ["name"],
            "additionalProperties": False,
        },
        fn=get_project,
    ),
    Tool(
        name="project_activity",
        description=(
            "Commits per project over the last N days (all branches when the code is local) "
            "plus ship log entries. Use for 'what did I ship/work on this week?'."
        ),
        input_schema={
            "type": "object",
            "properties": {"days": {"type": "integer", "minimum": 1, "maximum": 60}},
            "additionalProperties": False,
        },
        fn=project_activity,
    ),
]
