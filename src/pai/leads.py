"""Weekly lead finder for client work: Sonnet + web search drafts notes; it never sends any.

The research call returns JSON, one object per business, so links are data rather than
prose. Pai builds the clickable links itself (handles become profile URLs, a Maps search
link when no place link was found) and sends one Telegram message per lead.
"""

from __future__ import annotations

import html
import json
import re
import sqlite3
from datetime import date
from urllib.parse import quote_plus, urlparse

from pydantic import BaseModel, ValidationError

from pai import prompts
from pai.tools.research import WEB_SEARCH

MAX_SEARCHES = 20  # ~$0.20 of search per weekly run
JSON_BLOCK = re.compile(r"```(?:json)?\s*(\{.*\})\s*```", re.S)
HANDLE = re.compile(r"^@?([A-Za-z0-9._-]{1,60})$")

# Profile hosts per network, and how to turn a bare handle into a profile URL.
SOCIAL = {
    "instagram": (("instagram.com",), "https://www.instagram.com/{}/"),
    "facebook": (("facebook.com", "fb.com"), "https://www.facebook.com/{}"),
    "tiktok": (("tiktok.com",), "https://www.tiktok.com/@{}"),
    "yelp": (("yelp.com",), None),  # Yelp has no handles; only a found URL counts
}
LABELS = {"instagram": "Instagram", "facebook": "Facebook", "tiktok": "TikTok", "yelp": "Yelp"}


class Lead(BaseModel):
    name: str
    neighborhood: str = ""
    address: str = ""
    what: str = ""
    phone: str = ""
    website: str = ""  # URL found, or empty when there is none
    website_note: str = ""  # e.g. "no website found", "broken", "not mobile friendly"
    instagram: str = ""  # URL or @handle, as found
    facebook: str = ""
    tiktok: str = ""
    yelp: str = ""
    google_maps: str = ""  # place URL, if the search found one
    weakness: str = ""
    draft_note: str = ""


class LeadList(BaseModel):
    leads: list[Lead]
    most_promising: str = ""


def category_for(categories: list[str], today: date) -> str:
    return categories[today.isocalendar().week % len(categories)]


def already_suggested(conn: sqlite3.Connection, limit: int = 60) -> list[str]:
    rows = conn.execute("SELECT name FROM leads ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return [r["name"] for r in rows]


def record(conn: sqlite3.Connection, names: list[str], category: str) -> list[str]:
    names = [n.strip() for n in names if n.strip()]
    conn.executemany(
        "INSERT INTO leads (name, category) VALUES (?, ?)", [(n, category) for n in names]
    )
    return names


def _web_url(value: str, hosts: tuple[str, ...] | None = None) -> str | None:
    """`value` as an http(s) URL, if it is one (and on one of `hosts`, when given)."""
    value = value.strip()
    if not value:
        return None
    if not value.startswith(("http://", "https://")) and "." in value and " " not in value:
        value = "https://" + value  # "rosastacos.com"
    u = urlparse(value)
    if u.scheme not in ("http", "https") or not u.netloc or " " in value:
        return None
    host = u.netloc.lower().removeprefix("www.").removeprefix("m.")
    if hosts and not any(host == h or host.endswith("." + h) for h in hosts):
        return None
    return value


def social_url(network: str, value: str) -> str | None:
    """A profile URL from what the search found: a URL on that network, or a handle."""
    hosts, template = SOCIAL[network]
    url = _web_url(value, hosts)
    if url:
        return url
    m = HANDLE.match(value.strip())
    return template.format(m.group(1)) if m and template else None


def maps_url(lead: Lead, area: str) -> str:
    """The place link the search found, else a Maps search for the name and address."""
    found = _web_url(lead.google_maps, ("google.com", "goo.gl", "maps.app.goo.gl"))
    if found:
        return found
    where = lead.address or lead.neighborhood or area
    return "https://www.google.com/maps/search/?api=1&query=" + quote_plus(f"{lead.name} {where}")


def links(lead: Lead, area: str) -> list[tuple[str, str]]:
    """(label, url) pairs for one lead, in a fixed order."""
    out = []
    site = _web_url(lead.website)
    if site:
        out.append(("Website", site))
    for network in SOCIAL:
        url = social_url(network, getattr(lead, network))
        if url:
            out.append((LABELS[network], url))
    out.append(("Google Maps", maps_url(lead, area)))
    return out


def _a(label: str, url: str) -> str:
    return f'<a href="{html.escape(url, quote=True)}">{html.escape(label)}</a>'


def render(lead: Lead, area: str, n: int, total: int) -> str:
    """One lead as a Telegram HTML message."""
    e = html.escape
    where = lead.neighborhood or lead.address
    lines = [f"<b>{n}/{total}. {e(lead.name)}</b>" + (f" ({e(where)})" if where else "")]
    if lead.what:
        lines.append(e(lead.what))
    if lead.address and lead.address != where:
        lines.append(f"📍 {e(lead.address)}")
    if lead.phone:
        lines.append(f"📞 {e(lead.phone)}")  # Telegram makes numbers tappable itself
    found = links(lead, area)
    lines.append("🔗 " + " · ".join(_a(label, url) for label, url in found))
    if not _web_url(lead.website):
        lines.append(f"🌐 {e(lead.website_note or 'no website found')}")
    elif lead.website_note:
        lines.append(f"🌐 {e(lead.website_note)}")
    if lead.weakness:
        lines.append(f"<b>What's weak:</b> {e(lead.weakness)}")
    if lead.draft_note:
        lines.append(f"<b>Draft note:</b>\n<i>{e(lead.draft_note)}</i>")
    return "\n".join(lines)


def parse(text: str) -> LeadList | None:
    """The JSON the research call ends with, if it's there and valid."""
    m = JSON_BLOCK.search(text)
    raw = m.group(1) if m else text[text.find("{") : text.rfind("}") + 1]
    try:
        return LeadList.model_validate(json.loads(raw))
    except (ValueError, ValidationError):
        return None


async def find(
    agent, conn: sqlite3.Connection, config, today: date, category: str | None = None
) -> list[str]:
    """Research leads; returns Telegram HTML messages: a header, then one per lead."""
    cfg = config.leads
    category = category or category_for(cfg.categories, today)
    seen = already_suggested(conn)
    spread = (
        f" (anywhere in it, e.g. {', '.join(cfg.neighborhoods)}; spread the picks out)"
        if cfg.neighborhoods
        else ""
    )
    user = (
        f"Area: {cfg.area}{spread}\nCategory: {category}\n"
        f"Find up to {cfg.count} businesses.\n"
        f"Today is {today:%Y-%m-%d}.\n"
        f"Already suggested (skip these): {', '.join(seen) if seen else 'none yet'}"
    )
    system = prompts.render("leads.md", config)
    tool = {**WEB_SEARCH, "max_uses": MAX_SEARCHES}
    text = await agent.complete("planner", system, user, job="leads", tools=[tool])
    found = parse(text)
    if found is None:  # rare: reshape the prose with a cheap structured call
        found = await agent.structured(
            "chat",
            "Turn these business leads into the schema. Copy names, URLs, handles and "
            "notes exactly as written; leave a field empty when it isn't given.",
            text,
            LeadList,
            job="leads_parse",
        )
    items = found.leads[: cfg.count]
    record(conn, [lead.name for lead in items], category)
    title = f"<b>Leads: {html.escape(category)} in {html.escape(cfg.area)}</b>"
    if not items:
        return [f"{title}\nNo good candidates this time; nothing to suggest."]
    header = (
        f"{title} · {len(items)} found\n<i>Drafts for you to review. Nothing has been sent.</i>"
    )
    if found.most_promising:
        header += f"\n⭐ {html.escape(found.most_promising)}"
    return [header] + [render(lead, cfg.area, i, len(items)) for i, lead in enumerate(items, 1)]
