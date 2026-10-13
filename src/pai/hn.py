"""Hacker News picks for the morning briefing (Algolia HN API, no key, no model)."""

from __future__ import annotations

import re

import httpx

FRONT_PAGE = "https://hn.algolia.com/api/v1/search"


def pick(hits: list[dict], topics: list[str], count: int) -> list[dict]:
    """Stories matching a topic first (by points), then the rest by points."""
    words = [re.compile(rf"\b{re.escape(t.lower())}\b") for t in topics]

    def matches(h: dict) -> bool:
        title = (h.get("title") or "").lower()
        return any(w.search(title) for w in words)

    ranked = sorted(hits, key=lambda h: (not matches(h), -(h.get("points") or 0)))
    return ranked[:count]


async def digest(topics: list[str], count: int) -> list[dict]:
    async with httpx.AsyncClient(timeout=15) as http:
        r = await http.get(FRONT_PAGE, params={"tags": "front_page", "hitsPerPage": 50})
        r.raise_for_status()
    stories = [h for h in r.json().get("hits", []) if h.get("title")]
    return [
        {
            "title": h["title"],
            "url": h.get("url") or f"https://news.ycombinator.com/item?id={h['objectID']}",
            "points": h.get("points") or 0,
            "comments": f"https://news.ycombinator.com/item?id={h['objectID']}",
        }
        for h in pick(stories, topics, count)
    ]


def format_lines(stories: list[dict]) -> list[str]:
    return [f"• [{s['title']}]({s['url']}) ({s['points']} pts)" for s in stories]
