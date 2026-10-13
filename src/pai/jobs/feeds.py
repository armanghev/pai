"""Canvas announcements and inbox messages: alert on new ones only. No model calls."""

from __future__ import annotations

import json
import sqlite3

from pai import db
from pai.links import html_to_faithful_text

KEEP = 300  # remembered ids per feed


def unseen(conn: sqlite3.Connection, key: str, items: list[dict]) -> list[dict]:
    """Items whose id wasn't seen before. The first run only records a baseline."""
    raw = db.kv_get(conn, key)
    seen = set(json.loads(raw)) if raw else None
    ids = [str(i["id"]) for i in items]
    fresh = [] if seen is None else [i for i in items if str(i["id"]) not in seen]
    merged = list(dict.fromkeys([*(json.loads(raw) if raw else []), *ids]))[-KEEP:]
    db.kv_set(conn, key, json.dumps(merged))
    return fresh


def preview(html_or_text: str, limit: int = 350) -> str:
    text = " ".join(html_to_faithful_text(html_or_text).replace("**", "").split())
    return text if len(text) <= limit else text[: limit - 1].rsplit(" ", 1)[0] + "…"


def announcement_text(a: dict) -> str:
    by = f" from {a['author']}" if a["author"] else ""
    return f"📣 *{a['course']}*{by}: {a['title']}\n{preview(a['message'])}\n{a['url']}"


def message_text(m: dict) -> str:
    return (
        f"✉️ Canvas message from {m['from']}: *{m['subject']}*\n{preview(m['preview'])}\n{m['url']}"
    )
