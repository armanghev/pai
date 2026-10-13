"""Price watch: read a product page's price from its structured data. No model calls.

Most stores publish the price in JSON-LD (schema.org Product/Offer) or in meta tags for link
previews. Sites that block bots (Amazon often does) just report "couldn't read a price".
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from html import unescape

import httpx

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/126.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}
LD_JSON = re.compile(
    r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>', re.S | re.I
)
META = re.compile(r"<meta\s+([^>]+)>", re.I)
ATTR = re.compile(r'([\w:-]+)\s*=\s*["\']([^"\']*)["\']')
TITLE = re.compile(r"<title[^>]*>(.*?)</title>", re.S | re.I)


@dataclass
class Price:
    amount: float
    currency: str
    title: str


def _num(value) -> float | None:
    if value is None:
        return None
    try:
        return float(str(value).replace(",", "").replace("$", "").strip())
    except ValueError:
        return None


def _walk_offers(node, found: list):
    if isinstance(node, list):
        for x in node:
            _walk_offers(x, found)
    elif isinstance(node, dict):
        kind = node.get("@type")
        kinds = kind if isinstance(kind, list) else [kind]
        if any(k in ("Offer", "AggregateOffer") for k in kinds):
            amount = _num(node.get("price")) or _num(node.get("lowPrice"))
            if amount:
                found.append((amount, node.get("priceCurrency") or "USD"))
        for v in node.values():
            if isinstance(v, (dict, list)):
                _walk_offers(v, found)


def _title(html: str, ld: list) -> str:
    for node in ld:
        if isinstance(node, dict) and node.get("name"):
            return str(node["name"])
    m = TITLE.search(html)
    return " ".join(unescape(m.group(1)).split()) if m else ""


def extract(html: str) -> Price | None:
    ld = []
    for block in LD_JSON.findall(html):
        try:
            ld.append(json.loads(block.strip()))
        except json.JSONDecodeError:
            continue
    found: list = []
    _walk_offers(ld, found)
    if not found:
        meta = {}
        for m in META.finditer(html):
            attrs = dict((k.lower(), v) for k, v in ATTR.findall(m.group(1)))
            key = attrs.get("property") or attrs.get("name") or attrs.get("itemprop")
            if key:
                meta[key.lower()] = attrs.get("content")
        amount = _num(
            meta.get("product:price:amount") or meta.get("og:price:amount") or meta.get("price")
        )
        if amount:
            currency = meta.get("product:price:currency") or meta.get("og:price:currency") or "USD"
            found.append((amount, currency))
    if not found:
        return None
    amount, currency = min(found)  # the lowest offer is the price you'd actually pay
    return Price(amount, currency, _title(html, [x for x in ld if isinstance(x, dict)])[:120])


async def fetch(url: str) -> Price | None:
    async with httpx.AsyncClient(timeout=25, follow_redirects=True, headers=HEADERS) as http:
        r = await http.get(url)
    if r.status_code >= 400:
        return None
    return extract(r.text)


def add(conn: sqlite3.Connection, url: str, price: Price, target: float | None) -> int:
    cur = conn.execute(
        "INSERT INTO price_watches (url, title, target, last_price, currency)"
        " VALUES (?, ?, ?, ?, ?)",
        (url, price.title, target, price.amount, price.currency),
    )
    return cur.lastrowid


def active(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM price_watches WHERE active = 1 ORDER BY id").fetchall()


def remove(conn: sqlite3.Connection, wid: int) -> bool:
    return conn.execute("UPDATE price_watches SET active = 0 WHERE id = ?", (wid,)).rowcount > 0


def check(conn: sqlite3.Connection, row: sqlite3.Row, price: Price) -> str | None:
    """Store the new price and return an alert if it dropped (or hit the target)."""
    old, target = row["last_price"], row["target"]
    conn.execute("UPDATE price_watches SET last_price = ? WHERE id = ?", (price.amount, row["id"]))
    name = row["title"] or price.title or row["url"]
    if target is not None:
        if price.amount <= target and (old is None or old > target):
            return (
                f"💸 *{name}* is ${price.amount:,.2f}, at or under your ${target:,.2f} "
                f"target.\n{row['url']}"
            )
        return None
    if old is not None and price.amount < old - 0.005:
        return (
            f"💸 Price drop: *{name}* went from ${old:,.2f} to ${price.amount:,.2f}.\n{row['url']}"
        )
    return None
