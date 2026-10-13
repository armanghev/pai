from __future__ import annotations

import re
from datetime import UTC, datetime

from pai import grades
from pai.drafts import normalize
from pai.tools import Tool, ToolContext


def _tokens(text: str) -> list[str]:
    """normalize(), plus 'cs101' -> 'cs', '101' so codes typed without spaces still match."""
    return re.findall(r"[a-z]+|\d+", normalize(text))


def _hit(word: str, hay: list[str]) -> bool:
    if word.isdigit():
        return any(word in h for h in hay)
    return any(h.startswith(word[:4]) and len(word) >= 2 for h in hay)


def pick_course(courses, text: str):
    words = _tokens(text)
    best, best_hits = None, 0
    for c in courses:
        hay = _tokens(f"{c.code} {c.name}")
        hits = sum(_hit(w, hay) for w in words)
        if hits > best_hits:
            best, best_hits = c, hits
    return best


async def grade_needed(args: dict, ctx: ToolContext) -> str:
    if ctx.canvas is None:
        raise RuntimeError("Canvas is not configured")
    target = grades.parse_target(str(args["target"]))
    if target is None:
        return "Give a target like A, B, C or a percentage such as 85."
    courses, _ = await ctx.canvas.snapshot()
    course = pick_course(courses, args["course"])
    if course is None:
        names = ", ".join(c.code for c in courses)
        return f"Which class? Yours are: {names}."
    rows = await ctx.canvas.assignment_groups(course)
    groups = grades.groups_from_canvas(rows, datetime.now(UTC))
    now_text = (
        f" (Canvas has you at {course.score:g}% right now)" if course.score is not None else ""
    )
    return f"*{course.code}*{now_text}: {grades.needed(groups, course.weighted, target)}"


TOOLS = [
    Tool(
        name="grade_needed",
        description=(
            "Work out what average the owner needs on the rest of a class's posted work to "
            "finish with a target grade, using Canvas grades and assignment-group weights. "
            "It's an estimate: work not posted on Canvas yet isn't counted."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "course": {"type": "string", "description": "Course code or name, e.g. 'comm'"},
                "target": {"type": "string", "description": "Letter (A, B, C) or a percent"},
            },
            "required": ["course", "target"],
            "additionalProperties": False,
        },
        fn=grade_needed,
    )
]
