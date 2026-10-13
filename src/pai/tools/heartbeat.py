"""edit_heartbeat: the owner changes what the background check-ins watch for."""

from __future__ import annotations

from pai import heartbeat
from pai.tools import Tool, ToolContext


async def edit_heartbeat(args: dict, ctx: ToolContext) -> str:
    action, text = args["action"], args.get("text", "").strip()
    if action == "list":
        rows = heartbeat.items(ctx.conn, enabled_only=False)
        return "; ".join(f"#{r['id']} {r['text']}{'' if r['enabled'] else ' (off)'}" for r in rows)
    if action == "add":
        hid = heartbeat.add_item(ctx.conn, text)
        return f"Added #{hid}. Confirm in one short line."
    hits = heartbeat.find_item(ctx.conn, text)
    if len(hits) != 1:
        listing = "; ".join(f"#{h['id']} {h['text']}" for h in hits) or "none"
        return f"Need exactly one match, found: {listing}. Ask which one."
    h = hits[0]
    heartbeat.set_item(ctx.conn, h["id"], {"remove": None, "pause": False, "resume": True}[action])
    done = {"remove": "Stopped watching", "pause": "Paused", "resume": "Watching again"}[action]
    return f"{done}: {h['text']}. Confirm in one short line."


TOOLS = [
    Tool(
        name="edit_heartbeat",
        description=(
            "Change the checklist Pai's background check-ins (every ~45 min) watch: 'also "
            "watch for X' adds an item, 'stop checking dinner' removes or pauses one. Use "
            "action=list to see it."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["add", "remove", "pause", "resume", "list"]},
                "text": {
                    "type": "string",
                    "description": "The new item, or words matching an existing one.",
                },
            },
            "required": ["action"],
            "additionalProperties": False,
        },
        fn=edit_heartbeat,
    )
]
