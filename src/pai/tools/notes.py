"""Quick notes in one Markdown file, one note per line."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from pai.tools import Tool, ToolContext


def _lines(path: Path) -> list[str]:
    if not path.exists():
        return []
    return [line for line in path.read_text().splitlines() if line.startswith("- ")]


async def save_note(args: dict, ctx: ToolContext) -> str:
    path = ctx.config.notes_path
    path.parent.mkdir(parents=True, exist_ok=True)
    text = " ".join(args["text"].split())  # keep each note on one line
    tags = " ".join(f"#{t.lstrip('#')}" for t in args.get("tags", []))
    stamp = datetime.now(UTC).astimezone(ctx.config.tz).strftime("%Y-%m-%d %H:%M")
    if not path.exists():
        path.write_text("# Notes\n\n")
    with path.open("a") as f:
        f.write(f"- {stamp} · {text}{' ' + tags if tags else ''}\n")
    return "saved"


async def search_notes(args: dict, ctx: ToolContext) -> dict:
    lines = _lines(ctx.config.notes_path)
    query = (args.get("query") or "").lower()
    words = query.split()
    hits = [line[2:] for line in lines if all(w in line.lower() for w in words)]
    limit = int(args.get("limit", 20))
    return {"notes": hits[-limit:], "total_matches": len(hits)}


TOOLS = [
    Tool(
        name="save_note",
        description="Save a quick note (an idea, a fact to remember, a link) to the notes file.",
        input_schema={
            "type": "object",
            "properties": {
                "text": {"type": "string"},
                "tags": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["text"],
            "additionalProperties": False,
        },
        fn=save_note,
    ),
    Tool(
        name="search_notes",
        description=(
            "Find saved notes containing all the given words (or the latest notes if the "
            "query is empty)."
        ),
        input_schema={
            "type": "object",
            "properties": {"query": {"type": "string"}, "limit": {"type": "integer"}},
            "additionalProperties": False,
        },
        fn=search_notes,
    ),
]
