"""Photo of a syllabus, flyer or whiteboard -> dated items -> reminders (Haiku vision)."""

from __future__ import annotations

import base64
from datetime import date, datetime, time, timedelta
from typing import Literal

from pydantic import BaseModel, Field

SYSTEM = (
    "You read photos of syllabi, flyers, whiteboards and schedules and pull out every item "
    "that has a date: assignments and exams (kind 'due'), events and meetings ('event'), "
    "and other dated to-dos ('task'). Use the current date given to resolve dates without a "
    "year to the next upcoming one. Include the time only when it's written. Keep titles "
    "short (under 60 characters) and specific. If nothing has a date, return no items and "
    "say what the photo shows in `summary`."
)


class Item(BaseModel):
    title: str
    date: date
    time: time | None
    kind: Literal["due", "event", "task"]
    course: str | None


class Extracted(BaseModel):
    items: list[Item] = Field(max_length=25)
    summary: str


def request(image: bytes, media_type: str, caption: str, now: datetime, tz) -> list[dict]:
    local = now.astimezone(tz)
    text = f"Current date: {local:%A %Y-%m-%d}."
    if caption:
        text += f"\nThe sender's note: {caption}"
    return [
        {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": media_type,
                "data": base64.standard_b64encode(image).decode(),
            },
        },
        {"type": "text", "text": text},
    ]


def remind_at(item: Item, tz, now: datetime) -> datetime | None:
    """When to remind: evening before a deadline, an hour before a timed event, else 9am."""
    if item.kind == "due":
        options = [
            datetime.combine(item.date - timedelta(days=1), time(18, 0), tz),
            datetime.combine(item.date, time(9, 0), tz),
        ]
    elif item.time is not None:
        options = [datetime.combine(item.date, item.time, tz) - timedelta(hours=1)]
    else:
        options = [datetime.combine(item.date, time(9, 0), tz)]
    return next((t for t in options if t > now), None)


def reminder_text(item: Item) -> str:
    prefix = f"{item.course}: " if item.course else ""
    when = f" at {item.time:%H:%M}" if item.time else ""
    if item.kind == "due":
        return f"{prefix}{item.title} is due {item.date:%a %m/%d}{when}"
    return f"{prefix}{item.title} on {item.date:%a %m/%d}{when}"
