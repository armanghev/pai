from __future__ import annotations

from datetime import UTC, datetime, timedelta

from pai.canvas import is_overdue, upcoming
from pai.tools import Tool, ToolContext


def _need_canvas(ctx: ToolContext):
    if ctx.canvas is None:
        raise RuntimeError("Canvas is not configured (CANVAS_BASE_URL / CANVAS_TOKEN in .env)")
    return ctx.canvas


async def get_canvas_items(args: dict, ctx: ToolContext) -> dict:
    canvas = _need_canvas(ctx)
    days = int(args.get("days", 14))
    now = datetime.now(UTC)
    items = upcoming(
        await canvas.all_assignments(),
        now,
        days,
        args.get("include_submitted", False),
        args.get("include_overdue", True),
    )
    tz = ctx.config.tz
    return {
        "items": [
            {
                "assignment_id": a.id,
                "course": a.course,
                "title": a.title,
                "due": a.due_at.astimezone(tz).strftime("%a %Y-%m-%d %H:%M"),
                "points": a.points,
                "submitted": a.submitted,
                "overdue": is_overdue(a, now),
                "url": a.html_url,
            }
            for a in items
        ]
    }


async def get_grades(args: dict, ctx: ToolContext) -> dict:
    canvas = _need_canvas(ctx)
    courses, items = await canvas.snapshot()
    since = datetime.now(UTC) - timedelta(days=int(args.get("recent_days", 14)))
    recent = sorted(
        (a for a in items if a.graded_at and a.graded_at >= since),
        key=lambda a: a.graded_at,
        reverse=True,
    )
    return {
        "courses": [{"course": c.code, "score": c.score, "grade": c.grade} for c in courses],
        "recently_graded": [
            {"course": a.course, "title": a.title, "score": a.score, "points": a.points}
            for a in recent
        ],
    }


TOOLS = [
    Tool(
        name="get_canvas_items",
        description=(
            "List upcoming Canvas assignments, quizzes and discussions with due dates (local "
            "time), points and whether they're submitted. Overdue unsubmitted work is included "
            "and flagged. Use for 'what's due', 'what do I have this week' and similar."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "days": {"type": "integer", "description": "Days ahead to look (default 14)."},
                "include_overdue": {
                    "type": "boolean",
                    "description": "Also list past-due unsubmitted work (default true).",
                },
                "include_submitted": {
                    "type": "boolean",
                    "description": "Also list work already turned in (default false).",
                },
            },
            "additionalProperties": False,
        },
        fn=get_canvas_items,
    ),
    Tool(
        name="get_grades",
        description=(
            "Current overall grade in each Canvas course, plus assignments graded recently "
            "with their scores."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "recent_days": {
                    "type": "integer",
                    "description": "How far back to list graded work (default 14).",
                }
            },
            "additionalProperties": False,
        },
        fn=get_grades,
    ),
]
