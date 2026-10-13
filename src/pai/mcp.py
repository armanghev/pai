"""Remote MCP servers in the chat tool loop, through the Messages API's MCP connector.

Anthropic makes the MCP calls server-side; Pai only adds `mcp_servers` and an `mcp_toolset`
per server to a request. When a server is added, Pai lists its tools once (a minimal MCP
client over streamable HTTP) and enables only the read-only ones; write tools, and any
tool the server adds later, stay off until the owner turns them on. A server is attached
only when the message names it or one of its enabled tools, so plain chat keeps its cached
tool list. Tokens live in the DB and are never shown or logged.
"""

from __future__ import annotations

import json
import re
import sqlite3
from urllib.parse import urlparse

import httpx

from pai import errorlog

BETA = "mcp-client-2025-11-20"
PROTOCOL = "2025-06-18"
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,30}$")
MAX_SERVERS = 8


def _rpc_result(r: httpx.Response) -> dict:
    """The JSON-RPC result from a JSON or SSE response."""
    if "text/event-stream" in r.headers.get("content-type", ""):
        for line in r.text.splitlines():
            if line.startswith("data:"):
                msg = json.loads(line[5:].strip())
                if "result" in msg or "error" in msg:
                    break
        else:
            raise ValueError("no JSON-RPC response in the event stream")
    else:
        msg = r.json()
    if "error" in msg:
        raise ValueError(f"MCP error: {msg['error'].get('message', msg['error'])}")
    return msg["result"]


async def list_tools(
    url: str, token: str | None, http: httpx.AsyncClient | None = None
) -> list[dict]:
    """[{name, description, read_only}] from a streamable-HTTP MCP server."""
    own = http is None
    http = http or httpx.AsyncClient(timeout=20)
    headers = {"Accept": "application/json, text/event-stream"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        init = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": PROTOCOL,
                "capabilities": {},
                "clientInfo": {"name": "pai", "version": "1"},
            },
        }
        r = await http.post(url, json=init, headers=headers)
        r.raise_for_status()
        _rpc_result(r)
        if r.headers.get("mcp-session-id"):
            headers["Mcp-Session-Id"] = r.headers["mcp-session-id"]
        await http.post(
            url, json={"jsonrpc": "2.0", "method": "notifications/initialized"}, headers=headers
        )
        tools, cursor, n = [], None, 2
        while True:
            params = {"cursor": cursor} if cursor else {}
            r = await http.post(
                url,
                json={"jsonrpc": "2.0", "id": n, "method": "tools/list", "params": params},
                headers=headers,
            )
            r.raise_for_status()
            result = _rpc_result(r)
            for t in result.get("tools", []):
                hints = t.get("annotations") or {}
                tools.append(
                    {
                        "name": t["name"],
                        "description": (t.get("description") or "")[:200],
                        "read_only": bool(hints.get("readOnlyHint")),
                    }
                )
            cursor, n = result.get("nextCursor"), n + 1
            if not cursor or n > 20:
                return tools
    finally:
        if own:
            await http.aclose()


def add(conn: sqlite3.Connection, name: str, url: str, token: str | None, tools: list[dict]) -> int:
    name = name.lower()
    if not NAME_RE.match(name):
        raise ValueError("name: 2-31 lowercase letters, digits or dashes")
    if urlparse(url).scheme != "https":
        raise ValueError("the server URL must start with https://")
    if conn.execute("SELECT COUNT(*) FROM mcp_servers").fetchone()[0] >= MAX_SERVERS:
        raise ValueError(f"already {MAX_SERVERS} servers; remove one first")
    cur = conn.execute(
        "INSERT INTO mcp_servers (name, url, token) VALUES (?, ?, ?)", (name, url, token or None)
    )
    sid = cur.lastrowid
    conn.executemany(
        "INSERT INTO mcp_tools (server_id, name, description, read_only, enabled)"
        " VALUES (?, ?, ?, ?, ?)",
        [
            (sid, t["name"], t["description"], int(t["read_only"]), int(t["read_only"]))
            for t in tools
        ],
    )
    if token:
        errorlog.LOG.add_secret(token)
    return sid


def servers(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM mcp_servers ORDER BY name").fetchall()


def tools_of(conn: sqlite3.Connection, sid: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM mcp_tools WHERE server_id = ? ORDER BY read_only DESC, name", (sid,)
    ).fetchall()


def set_server(conn: sqlite3.Connection, name: str, enabled: bool | None) -> bool:
    """Turn a server on/off, or remove it when enabled is None. False if there's no such name."""
    if enabled is None:
        cur = conn.execute("DELETE FROM mcp_servers WHERE name = ?", (name.lower(),))
    else:
        cur = conn.execute(
            "UPDATE mcp_servers SET enabled = ? WHERE name = ?", (int(enabled), name.lower())
        )
    return cur.rowcount > 0


def set_tool(conn: sqlite3.Connection, tool_id: int, enabled: bool) -> sqlite3.Row | None:
    conn.execute("UPDATE mcp_tools SET enabled = ? WHERE id = ?", (int(enabled), tool_id))
    return conn.execute("SELECT * FROM mcp_tools WHERE id = ?", (tool_id,)).fetchone()


def relevant(conn: sqlite3.Connection, text: str) -> list[sqlite3.Row]:
    """Enabled servers this message names, by server name or one of its enabled tools."""
    words = set(re.findall(r"[a-z0-9_-]+", text.lower()))
    out = []
    for s in servers(conn):
        if not s["enabled"]:
            continue
        names = {s["name"]} | {t["name"].lower() for t in tools_of(conn, s["id"]) if t["enabled"]}
        if words & names:
            out.append(s)
    return out


def request_parts(conn: sqlite3.Connection, picked: list[sqlite3.Row]) -> tuple[list, list]:
    """(mcp_servers, toolsets) for a request. Only enabled tools are allowed (allowlist)."""
    defs, toolsets = [], []
    for s in picked:
        d = {"type": "url", "url": s["url"], "name": s["name"]}
        if s["token"]:
            d["authorization_token"] = s["token"]
        defs.append(d)
        allowed = [t["name"] for t in tools_of(conn, s["id"]) if t["enabled"]]
        toolsets.append(
            {
                "type": "mcp_toolset",
                "mcp_server_name": s["name"],
                "default_config": {"enabled": False},
                "configs": {name: {"enabled": True} for name in allowed},
            }
        )
    return defs, toolsets


def load_secrets(conn: sqlite3.Connection) -> None:
    """At startup: make sure every stored token is redacted from logs and /fix."""
    for s in servers(conn):
        if s["token"]:
            errorlog.LOG.add_secret(s["token"])
