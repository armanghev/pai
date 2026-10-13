from __future__ import annotations

import io
from datetime import UTC, datetime

from pai import drafts
from pai.canvas import Assignment
from pai.tools import Tool, ToolContext

CHAT_LIMIT = 30_000  # characters of instructions handed to the chat model
NOT_FOUND = (
    "I couldn't tell which assignment you mean. Try the name as it appears on "
    "Canvas, or paste its Canvas link."
)


async def _lookup(query: str, ctx: ToolContext) -> Assignment | None:
    if ctx.canvas is None:
        raise RuntimeError("Canvas is not configured")
    items = await ctx.canvas.all_assignments()
    query = await drafts.assignment_for_group_topic(ctx.canvas, query) or query
    matches = drafts.find(items, query)
    a = matches[0] if matches else None
    if a is None and ctx.agent is not None:
        a = await drafts.pick_with_model(ctx.agent, items, query, datetime.now(UTC), ctx.config.tz)
    return a


async def _with_submission(brief: drafts.Brief, a: Assignment, ctx: ToolContext) -> None:
    mine = await drafts.submission(ctx.canvas, a, ctx.config.tz)
    brief.text += "\n\n" + mine.text
    brief.documents += mine.documents


async def assignment_instructions(args: dict, ctx: ToolContext) -> str | list[dict]:
    a = await _lookup(args["assignment"], ctx)
    if a is None:
        return NOT_FOUND
    try:
        brief = await drafts.details(ctx.canvas, a)
    except drafts.NoInstructions:
        brief = drafts.Brief(f"Canvas has no instructions for {a.course} {a.title!r}.")
    if args.get("include_submission"):
        await _with_submission(brief, a, ctx)
    brief.text += f"\nCanvas link: {a.html_url}"
    return _result(brief)


def _result(brief: drafts.Brief) -> str | list[dict]:
    """A brief as a tool result: text capped at CHAT_LIMIT (saying so), PDFs as documents."""
    text = brief.text
    if len(text) > CHAT_LIMIT:
        cut = len(text) - CHAT_LIMIT
        text = text[:CHAT_LIMIT] + f"\n…(cut {cut:,} more characters; tell the owner)"
    if not brief.documents:
        return text
    return [*brief.documents, {"type": "text", "text": text}]


MAX_FILES_PER_CALL = 3


async def read_canvas_file(args: dict, ctx: ToolContext) -> str | list[dict]:
    if ctx.canvas is None:
        raise RuntimeError("Canvas is not configured")
    ids = [int(i) for i in args["file_ids"]]
    brief = drafts.Brief("")
    if len(ids) > MAX_FILES_PER_CALL:
        brief.text += f"(Opened the first {MAX_FILES_PER_CALL} of {len(ids)}; ask for the rest.)"
    for file_id in ids[:MAX_FILES_PER_CALL]:
        try:
            one = await drafts.file_brief(ctx.canvas, file_id)
        except Exception as e:  # e.g. 403 on a file this account can't open
            one = drafts.Brief(
                f"File {file_id}: couldn't open it ({type(e).__name__}). If it's attached to "
                "a discussion post, read that discussion first, then open it again."
            )
        brief.text += "\n\n" + one.text
        brief.documents += one.documents
    brief.text = brief.text.strip()
    return _result(brief)


async def draft_assignment(args: dict, ctx: ToolContext) -> str:
    a = await _lookup(args["assignment"], ctx)
    if a is None:
        return NOT_FOUND
    try:
        brief = await drafts.details(ctx.canvas, a, args.get("instructions", ""))
    except drafts.NoInstructions:
        return (
            f"Canvas didn't return any instructions for {a.course} {a.title!r}, and I won't "
            "guess at them. Paste the instructions under the command, like:\n"
            f"/draft {a.title}\n<instructions here>"
        )
    if args.get("use_submission"):
        await _with_submission(brief, a, ctx)
    text = await drafts.write(ctx.agent, brief, args.get("notes", ""))
    if ctx.send_file is None:
        return text
    await ctx.send_file(drafts.filename(a.title), io.BytesIO(text.encode()), f"📝 {drafts.LABEL}")
    return (
        f"Sent the reference draft for {a.course} {a.title!r} as a file. Reply in one short line."
    )


TELEGRAM_MAX = 50 * 1024 * 1024  # bots can't send bigger files
MAX_SENDS_PER_CALL = 3


async def send_canvas_file(args: dict, ctx: ToolContext) -> str:
    if ctx.canvas is None:
        raise RuntimeError("Canvas is not configured")
    if ctx.send_file is None:
        raise RuntimeError("can't send files from here")
    ids = [int(i) for i in args["file_ids"]]
    lines = []
    if len(ids) > MAX_SENDS_PER_CALL:
        lines.append(f"Sent only the first {MAX_SENDS_PER_CALL} of {len(ids)}; ask before more.")
    for file_id in ids[:MAX_SENDS_PER_CALL]:
        try:
            meta = await drafts.file_meta(ctx.canvas, file_id)
            name = meta.get("display_name") or meta.get("filename") or f"file-{file_id}"
            if (meta.get("size") or 0) > TELEGRAM_MAX:
                link = f"{ctx.canvas.base}/files/{file_id}"
                lines.append(f"{name} is over Telegram's 50 MB limit; link: {link}")
                continue
            data = await drafts.download(ctx.canvas, meta)
            await ctx.send_file(name, io.BytesIO(data), f"📎 {name} (from Canvas)")
            lines.append(f"Sent {name}.")
        except Exception as e:  # e.g. 403 on a file this account can't open
            lines.append(
                f"File {file_id}: couldn't get it ({type(e).__name__}). If it's attached to a "
                "discussion post, read that discussion first."
            )
    return " ".join(lines) + " Reply in one short line."


TOOLS = [
    Tool(
        name="assignment_instructions",
        description=(
            "Full instructions for one of the owner's Canvas assignments: the description, "
            "any Canvas pages and files it links to (PDFs attached), the discussion prompt "
            "and the rubric with its scoring levels. With include_submission, also what they "
            "turned in (text, files, discussion posts and replies), its grade, rubric scores "
            "and instructor comments. Use whenever they ask what an assignment wants, how it's "
            "graded, or about their own submission or feedback; don't guess from the title."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "assignment": {
                    "type": "string",
                    "description": "Words from the title or a Canvas link, e.g. 'lab 4'",
                },
                "include_submission": {
                    "type": "boolean",
                    "description": "Also read what they submitted and its feedback.",
                },
            },
            "required": ["assignment"],
            "additionalProperties": False,
        },
        fn=assignment_instructions,
    ),
    Tool(
        name="read_canvas_file",
        description=(
            "Open Canvas files by id, e.g. attachments listed as '[attached: essay.docx "
            "(file 123, ...)]' in a discussion thread. PDFs come back as documents, images as "
            "images, Word and text files as text; video and audio can't be opened. Up to 3 "
            "per call. Open only the files the question needs."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "file_ids": {
                    "type": "array",
                    "items": {"type": "integer"},
                    "description": "Canvas file ids, e.g. [123].",
                }
            },
            "required": ["file_ids"],
            "additionalProperties": False,
        },
        fn=read_canvas_file,
    ),
    Tool(
        name="send_canvas_file",
        description=(
            "Download Canvas files by id and send them to the owner in Telegram as documents: "
            "attachments, linked handouts or their own submitted files (ids show as "
            "'file 123' in what you've read). Use only when they ask for the file itself, "
            "up to 3 per call."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "file_ids": {
                    "type": "array",
                    "items": {"type": "integer"},
                    "description": "Canvas file ids, e.g. [123].",
                }
            },
            "required": ["file_ids"],
            "additionalProperties": False,
        },
        fn=send_canvas_file,
    ),
    Tool(
        name="draft_assignment",
        description=(
            "Write a reference draft of one of the owner's Canvas assignments, as a worked "
            "example for them to study from (they write and submit their own). Use only when "
            "they ask for a draft or sample of an assignment. Sends the draft as a file."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "assignment": {
                    "type": "string",
                    "description": "Words from the assignment title, e.g. 'essay 2'",
                },
                "notes": {"type": "string", "description": "Their extra instructions, if any."},
                "instructions": {
                    "type": "string",
                    "description": "Assignment instructions they pasted, verbatim, if any.",
                },
                "use_submission": {
                    "type": "boolean",
                    "description": "Build on what they already submitted (for a revision).",
                },
            },
            "required": ["assignment"],
            "additionalProperties": False,
        },
        fn=draft_assignment,
    ),
]
