"""remember / forget: the chat model's handle on long-term memory."""

from __future__ import annotations

from pai import memory
from pai.tools import Tool, ToolContext


async def remember(args: dict, ctx: ToolContext) -> str:
    if ctx.private_seen:  # money data came up this exchange: nothing from it is kept
        return "Not saved: nothing from a money or portfolio question goes into memory."
    mid = memory.add(ctx.conn, args["fact"], args.get("category", "other"), args.get("source", ""))
    return f"Saved as memory #{mid}. Tell the owner in one short line what you'll remember."


async def forget(args: dict, ctx: ToolContext) -> str:
    if args.get("memory_id"):
        gone = memory.forget(ctx.conn, int(args["memory_id"]))
        return f"Forgot #{args['memory_id']}: {gone}" if gone else "No memory with that id."
    hits = memory.find(ctx.conn, args.get("text", ""))
    if len(hits) == 1:
        memory.forget(ctx.conn, hits[0]["id"])
        return f"Forgot #{hits[0]['id']}: {hits[0]['fact']}"
    if not hits:
        return "No memory matches that."
    listing = "; ".join(f"#{h['id']} {h['fact']}" for h in hits[:10])
    return f"Several match, ask which one: {listing}"


TOOLS = [
    Tool(
        name="remember",
        description=(
            "Save a durable fact or preference about the owner for future chats, e.g. 'hates "
            "studying before 10am', 'works evenings on weekdays', 'prefers "
            "Python examples'. Use when they state one, or say 'remember that…'. Don't save "
            "one-off dated details (this week's quiz, Thursday's speech): those belong in "
            "reminders or Canvas. Don't save secrets, passwords or account numbers."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "fact": {"type": "string", "description": "One short sentence, third person."},
                "category": {"type": "string", "enum": list(memory.CATEGORIES)},
                "source": {"type": "string", "description": "Their words it came from."},
            },
            "required": ["fact"],
            "additionalProperties": False,
        },
        fn=remember,
    ),
    Tool(
        name="forget",
        description=(
            "Delete a remembered fact, by its #id from the memory list or by words in it. "
            "Use when they say 'forget…' or a remembered fact is no longer true."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "memory_id": {"type": "integer"},
                "text": {"type": "string", "description": "Words from the fact."},
            },
            "additionalProperties": False,
        },
        fn=forget,
    ),
]
