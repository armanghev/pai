"""Local, read-only preview of what Pai sees from Canvas.

    uv run python scripts/canvas_preview.py   ->  http://127.0.0.1:8765

Pick an assignment and the page shows the exact assignment_instructions tool output the
model gets (optionally with your submission), plus the raw Canvas JSON behind it. It only
makes GET requests, binds to localhost, and never starts the Telegram bot.
"""

from __future__ import annotations

import asyncio
import base64
import html
import json
import os
import re
import sqlite3
import threading
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, urlparse

import httpx

from pai import drafts
from pai.canvas import CanvasClient
from pai.config import Secrets, load_config
from pai.tools import ToolContext
from pai.tools import drafts as draft_tools

HOST, PORT = "127.0.0.1", int(os.environ.get("PREVIEW_PORT", "8765"))

CSS = """
:root { --bg:#fbfaf7; --fg:#1d1d1b; --muted:#6b6862; --card:#fff; --line:#e4e1da; --acc:#d9601a; }
@media (prefers-color-scheme: dark) {
  :root { --bg:#161513; --fg:#ecebe7; --muted:#9a968e; --card:#201f1c;
    --line:#33312d; --acc:#f08a4b; }
}
* { box-sizing: border-box; }
body { margin:0; background:var(--bg); color:var(--fg); font:15px/1.5 system-ui, sans-serif; }
main { max-width: 980px; margin: 0 auto; padding: 24px 16px 64px; }
h1 { font-size: 20px; margin: 0 0 4px; } h2 { font-size: 16px; margin: 28px 0 8px; }
.muted { color: var(--muted); font-size: 13px; }
form { display:flex; gap:8px; flex-wrap:wrap; align-items:center; margin:16px 0; }
input[type=text] { flex:1; min-width:220px; padding:8px 10px; border:1px solid var(--line);
  border-radius:8px; background:var(--card); color:var(--fg); font:inherit; }
button { padding:8px 14px; border:0; border-radius:8px; background:var(--acc); color:#fff;
  font:inherit; cursor:pointer; }
pre { background:var(--card); border:1px solid var(--line); border-radius:8px; padding:12px;
  white-space:pre-wrap; word-break:break-word; font:13px/1.45 ui-monospace, monospace; }
details { margin: 6px 0; } summary { cursor:pointer; }
table { width:100%; border-collapse:collapse; font-size:14px; }
td { padding:4px 8px 4px 0; border-bottom:1px solid var(--line); vertical-align:top; }
a { color: var(--acc); } .tag { font-size:12px; color:var(--muted); }
"""


class Preview:
    def __init__(self):
        self.config = load_config(os.environ.get("PAI_CONFIG", "config.toml"))
        secrets = Secrets()
        if not (secrets.canvas_base_url and secrets.canvas_token):
            raise SystemExit("CANVAS_BASE_URL and CANVAS_TOKEN must be set in .env")
        self.log: list[dict] = []
        self.loop = asyncio.new_event_loop()
        threading.Thread(target=self.loop.run_forever, daemon=True).start()
        self.lock = threading.Lock()  # one page at a time, so the JSON log is per page

        async def get_only(req: httpx.Request) -> None:
            if req.method != "GET":  # read-only: refuse anything else before it's sent
                raise RuntimeError(f"preview is read-only; blocked {req.method} {req.url}")

        async def record(r: httpx.Response) -> None:
            await r.aread()
            ctype = r.headers.get("content-type", "")
            body = r.json() if "json" in ctype else f"<{ctype}, {len(r.content):,} bytes>"
            self.log.append({"url": str(r.request.url), "status": r.status_code, "body": body})

        http = httpx.AsyncClient(
            timeout=30, event_hooks={"request": [get_only], "response": [record]}
        )
        self.canvas = CanvasClient(
            secrets.canvas_base_url, secrets.canvas_token.get_secret_value(), http=http
        )

        async def send(_text: str) -> None:
            pass

        self.ctx = ToolContext(
            conn=sqlite3.connect(":memory:"), config=self.config, send=send, canvas=self.canvas
        )

    def run(self, coro):
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout=120)

    def page(self, q: str, sub: bool, fresh: bool, files: str = "") -> str:
        self.log = []
        items = self.run(self.canvas.all_assignments(fresh=fresh))
        tz = self.config.tz
        now = datetime.now(UTC)
        parts = [
            "<h1>🦉 Pai · Canvas preview</h1>",
            f'<div class="muted">Read-only · {html.escape(self.canvas.base)} · '
            f'{len(items)} assignments · <a href="/?fresh=1">refresh from Canvas</a></div>',
            '<form method="get"><input type="text" name="q" placeholder="lab 4, essay 2, '
            f'or a Canvas link" value="{html.escape(q)}">'
            f'<label><input type="checkbox" name="sub" value="1"{" checked" if sub else ""}>'
            " include my submission</label><button>Show what Pai sees</button></form>",
        ]
        if files:
            parts += self.open_files(files)
        elif q:
            parts += self.result(items, q, sub)
        recent = sorted(
            (
                a
                for a in items
                if a.due_at and now - timedelta(days=30) <= a.due_at <= now + timedelta(days=30)
            ),
            key=lambda a: a.due_at,
        )
        rows = "".join(
            f"<tr><td>{a.due_at.astimezone(tz):%a %m/%d %H:%M}</td>"
            f"<td>{html.escape(a.course)}</td>"
            f'<td><a href="/?q={quote(a.html_url)}">{html.escape(a.title)}</a> '
            f'<a class="tag" href="/?q={quote(a.html_url)}&sub=1">+ submission</a></td>'
            f'<td class="tag">{"submitted" if a.submitted else "missing" if a.missing else ""}'
            f"{' · ' + html.escape(str(a.grade)) if a.grade else ''}</td></tr>"
            for a in recent
        )
        parts += ["<h2>Due in the last / next 30 days</h2>", f"<table>{rows}</table>"]
        return (
            "<!doctype html><meta charset=utf-8>"
            '<meta name="viewport" content="width=device-width, initial-scale=1">'
            f"<title>Canvas preview</title><style>{CSS}</style><main>{''.join(parts)}</main>"
        )

    def result(self, items, q: str, sub: bool) -> list[str]:
        resolved = self.run(drafts.assignment_for_group_topic(self.canvas, q)) or q
        matches = drafts.find(items, resolved)
        out = ["<h2>Matches</h2>"]
        if not matches:
            out.append(
                "<p class=muted>No match by name or link. (In chat, Pai would also ask Haiku "
                "to guess from a loose description; this preview skips that model call.)</p>"
            )
            return out
        out.append(
            "<ol>"
            + "".join(
                f"<li>{html.escape(a.course)} · {html.escape(a.title)}</li>" for a in matches[:5]
            )
            + "</ol>"
        )
        self.log = []
        args = {"assignment": q, "include_submission": sub}
        try:
            got = self.run(draft_tools.assignment_instructions(args, self.ctx))
        except Exception as e:
            got = f"Tool error: {type(e).__name__}: {e}"
        if isinstance(got, list):
            docs = [b for b in got if b["type"] == "document"]
            text = next((b["text"] for b in got if b["type"] == "text"), "")
            size = [len(base64.b64decode(d["source"]["data"])) for d in docs]
            listing = "".join(
                f"<li>📄 {html.escape(d.get('title', 'PDF'))} · {n / 1024:,.0f} KB</li>"
                for d, n in zip(docs, size, strict=True)
            )
            out.append(f"<h2>PDFs attached for the model ({len(docs)})</h2><ul>{listing}</ul>")
        else:
            text = got
        cut = "more characters; tell the owner" in text
        out.append(
            f"<h2>Tool output the model sees</h2><div class=muted>{len(text):,} characters"
            f"{' · <b>cut at the 30k cap</b>' if cut else ''}</div><pre>{html.escape(text)}</pre>"
        )
        refs = re.findall(r"\[attached: (.+?) \(file (\d+)", text)
        refs += re.findall(r"(?:Linked|Submitted) file: (.+?) \[file (\d+)\]", text)
        if refs:
            links = "".join(
                f'<li><a href="/?files={fid}">open</a> · <a href="/download?id={fid}">download</a> '
                f"{html.escape(name)} "
                f'<span class="tag">file {fid}</span></li>'
                for name, fid in refs
            )
            out.append(
                f"<h2>Files Pai can open or send ({len(refs)})</h2><div class=muted>Nothing "
                "here is downloaded until Pai calls read_canvas_file; click to see what it "
                f"would get; download gets the same file send_canvas_file sends in Telegram.</div>"
                f"<ul>{links}</ul>"
            )
        out += self.raw_log()
        return out

    def open_files(self, files: str) -> list[str]:
        self.log = []
        ids = [int(i) for i in re.findall(r"\d+", files)]
        got = self.run(draft_tools.read_canvas_file({"file_ids": ids}, self.ctx))
        blocks = got if isinstance(got, list) else [{"type": "text", "text": got}]
        out = [f"<h2>read_canvas_file({ids}) · what the model gets</h2>"]
        for b in blocks:
            if b["type"] == "text":
                out.append(f"<pre>{html.escape(b['text'])}</pre>")
            else:
                n = len(base64.b64decode(b["source"]["data"]))
                label = b.get("title") or b["source"]["media_type"]
                out.append(
                    f"<p>📎 {b['type']} block: {html.escape(label)} · {n / 1024:,.0f} KB</p>"
                )
        return out + self.raw_log()

    def download(self, file_id: int) -> tuple[str, bytes]:
        """The same bytes send_canvas_file would send to Telegram."""
        meta = self.run(drafts.file_meta(self.canvas, file_id))
        name = meta.get("display_name") or meta.get("filename") or f"file-{file_id}"
        return name, self.run(drafts.download(self.canvas, meta))

    def raw_log(self) -> list[str]:
        out = [f"<h2>Raw Canvas responses ({len(self.log)})</h2>"]
        for entry in self.log:
            body = entry["body"]
            shown = json.dumps(body, indent=2) if not isinstance(body, str) else body
            out.append(
                f"<details><summary>{entry['status']} · {html.escape(entry['url'])}</summary>"
                f"<pre>{html.escape(shown)}</pre></details>"
            )
        return out


def main() -> None:
    preview = Preview()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            url = urlparse(self.path)
            if url.path == "/download":
                self.download(parse_qs(url.query))
                return
            if url.path != "/":
                self.send_error(404)
                return
            qs = parse_qs(url.query)
            q = (qs.get("q") or [""])[0].strip()
            try:
                with preview.lock:
                    files = (qs.get("files") or [""])[0]
                    body = preview.page(q, bool(qs.get("sub")), bool(qs.get("fresh")), files)
                status = 200
            except Exception as e:
                body, status = f"<pre>{html.escape(f'{type(e).__name__}: {e}')}</pre>", 500
            data = body.encode()
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def download(self, qs):
            try:
                with preview.lock:
                    name, data = preview.download(int((qs.get("id") or ["0"])[0]))
            except Exception as e:
                self.send_error(502, f"{type(e).__name__}: {e}")
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Disposition", f"attachment; filename*=UTF-8''{quote(name)}")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *a):
            pass

    print(f"Canvas preview on http://{HOST}:{PORT}  (Ctrl-C to stop)")
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
