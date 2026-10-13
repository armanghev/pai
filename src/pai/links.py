"""Send a link, get a summary. Readable text is pulled out locally; Haiku summarizes."""

from __future__ import annotations

import base64
import re
from html.parser import HTMLParser

import httpx

URL_RE = re.compile(r"https?://\S+")
TRIGGER_RE = re.compile(r"^(?:summari[sz]e|tl;?dr|sum)\b[:\s]*", re.I)
MAX_BYTES = 8 * 1024 * 1024
MAX_CHARS = 40_000
SYSTEM = (
    "You summarize articles and class readings for a busy student reading on their phone. "
    "Give a one-line gist in bold, then 3 to 6 short bullet points with the key ideas, then, "
    "if it's a class reading, one line on what's likely to matter for an exam or discussion. "
    "Use Telegram Markdown (*bold*, • bullets). Under 200 words. Summarize only what's there."
)
SKIP = {"script", "style", "noscript", "nav", "footer", "header", "aside", "form", "svg"}


class _Text(HTMLParser):
    def __init__(self):
        super().__init__()
        self.title, self.parts, self._skip, self._in_title = "", [], 0, False

    def handle_starttag(self, tag, attrs):
        if tag in SKIP:
            self._skip += 1
        elif tag == "title":
            self._in_title = True
        elif tag in ("p", "br", "li", "h1", "h2", "h3", "h4", "tr", "div"):
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in SKIP and self._skip:
            self._skip -= 1
        elif tag == "title":
            self._in_title = False

    def handle_data(self, data):
        if self._in_title:
            self.title += data
        elif not self._skip:
            self.parts.append(data)


class _Faithful(HTMLParser):
    """Keeps every line, with <pre> blocks verbatim, list items as bullets, table cells
    split by pipes, and link and embed targets spelled out (instructions often live there)."""

    BLOCK = ("p", "div", "br", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "ul", "ol", "table")

    def __init__(self):
        super().__init__()
        self.out: list[str] = []
        self.links: list[str] = []  # every href and embed src, in order
        self._pre = 0
        self._skip = 0
        self._href: str | None = None
        self._link_start = 0
        self._cells = 0

    def handle_starttag(self, tag, attrs):
        a = {k: v or "" for k, v in attrs}
        if tag in ("script", "style"):
            self._skip += 1
        elif tag == "pre":
            self._pre += 1
            self.out.append("\n```\n")
        elif tag == "li":
            self.out.append("\n- ")
        elif tag in ("td", "th"):
            self.out.append(" | " if self._cells else "")
            self._cells += 1
        elif tag in self.BLOCK:
            self.out.append("\n")
            self._cells = 0 if tag == "tr" else self._cells
        elif tag in ("strong", "b") and not self._pre:
            self.out.append("**")
        elif tag == "a" and a.get("href"):
            self._href, self._link_start = a["href"], len(self.out)
            self.links.append(a["href"])
        elif tag in ("iframe", "embed") and a.get("src"):
            self.links.append(a["src"])
            label = a.get("title") or "embedded content"
            self.out.append(f"\n[{label}: {a['src']}]\n")
        elif tag == "img" and a.get("alt"):
            self.out.append(f"[image: {a['alt']}]")

    def handle_endtag(self, tag):
        if tag in ("script", "style") and self._skip:
            self._skip -= 1
        elif tag == "pre" and self._pre:
            self._pre -= 1
            self.out.append("\n```\n")
        elif tag in self.BLOCK:
            self.out.append("\n")
        elif tag in ("strong", "b") and not self._pre:
            self.out.append("**")
        elif tag == "a" and self._href is not None:
            text = "".join(self.out[self._link_start :]).strip()
            if self._href not in text and not self._href.startswith(("#", "mailto:")):
                self.out.append(f" ({self._href})")
            self._href = None

    def handle_data(self, data):
        if self._skip:
            return
        self.out.append(data if self._pre else re.sub(r"\s+", " ", data))


def html_to_faithful_text(html: str) -> str:
    """Assignment instructions as Markdown-ish text, keeping menus, code and exact strings."""
    return faithful_text_and_links(html)[0]


def faithful_text_and_links(html: str) -> tuple[str, list[str]]:
    """html_to_faithful_text, plus every link and embed target in the order they appear."""
    p = _Faithful()
    p.feed(html)
    p.close()
    text = "".join(p.out)
    out, blank, fenced = [], False, False
    for line in text.split("\n"):  # collapse runs of blank lines, but not inside code
        if line == "```":
            fenced = not fenced
        if fenced and line != "```":
            out.append(line)  # verbatim: trailing spaces in prompts matter
        elif line.strip():
            out.append(line.rstrip())
            blank = False
        elif not blank:
            out.append("")
            blank = True
    return "\n".join(out).strip(), p.links


def html_to_text(html: str) -> tuple[str, str]:
    p = _Text()
    p.feed(html)
    text = re.sub(r"[ \t\r\f\v]+", " ", "".join(p.parts))
    lines = [line.strip() for line in text.split("\n")]
    # Drop menu-like crumbs: keep lines with some substance.
    body = "\n".join(line for line in lines if len(line) > 40 or line.endswith((".", "?", "!")))
    return " ".join(p.title.split()), body[:MAX_CHARS]


def wants_summary(text: str) -> str | None:
    """A message that is just a link, or 'summarize <link>'. Returns the URL."""
    stripped = TRIGGER_RE.sub("", text.strip())
    m = URL_RE.fullmatch(stripped.strip())
    return m.group(0) if m else None


async def fetch(url: str) -> list[dict]:
    """Return content blocks for the model: the text of a page, or a PDF document."""
    async with httpx.AsyncClient(
        timeout=25, follow_redirects=True, headers={"User-Agent": "Mozilla/5.0 (pai)"}
    ) as http:
        r = await http.get(url)
        r.raise_for_status()
    if len(r.content) > MAX_BYTES:
        raise ValueError("that file is too big to summarize (over 8 MB)")
    ctype = r.headers.get("content-type", "").split(";")[0].strip().lower()
    if ctype == "application/pdf" or r.content[:5] == b"%PDF-":
        return [
            {
                "type": "document",
                "source": {
                    "type": "base64",
                    "media_type": "application/pdf",
                    "data": base64.standard_b64encode(r.content).decode(),
                },
            },
            {"type": "text", "text": f"Summarize this PDF from {url}."},
        ]
    if "html" not in ctype and not ctype.startswith("text/"):
        raise ValueError(f"I can only summarize web pages and PDFs, not {ctype or 'that'}")
    title, body = html_to_text(r.text) if "html" in ctype else ("", r.text[:MAX_CHARS])
    if len(body) < 200:
        raise ValueError("I couldn't find readable text on that page (it may need a login or JS)")
    return [{"type": "text", "text": f"Title: {title}\nURL: {url}\n\n{body}"}]


async def summarize(agent, url: str) -> str:
    return await agent.complete("chat", SYSTEM, await fetch(url), job="link_summary")
