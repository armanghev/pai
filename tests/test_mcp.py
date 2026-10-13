import json
from types import SimpleNamespace as N

import httpx
import pytest
from conftest import FakeClient, response, text_block

from pai import errorlog, mcp
from pai.agent import MCP_BETA, Agent
from pai.bot import PaiBot

TOKEN = "mcp-secret-token-123456"
TOOLS_PAGE_1 = {
    "tools": [
        {
            "name": "search_issues",
            "description": "Find issues",
            "annotations": {"readOnlyHint": True},
        },
        {"name": "create_issue", "description": "Open an issue"},
    ],
    "nextCursor": "p2",
}
TOOLS_PAGE_2 = {"tools": [{"name": "get_issue", "annotations": {"readOnlyHint": True}}]}


def _server(seen):
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen.append((body.get("method"), dict(request.headers)))
        if body["method"] == "initialize":
            return httpx.Response(
                200,
                json={"jsonrpc": "2.0", "id": 1, "result": {"protocolVersion": "2025-06-18"}},
                headers={"mcp-session-id": "s1"},
            )
        if body["method"] == "notifications/initialized":
            return httpx.Response(202)
        page = TOOLS_PAGE_2 if body["params"].get("cursor") else TOOLS_PAGE_1
        msg = json.dumps({"jsonrpc": "2.0", "id": body["id"], "result": page})
        sse = f"event: message\ndata: {msg}\n\n"
        return httpx.Response(200, text=sse, headers={"content-type": "text/event-stream"})

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_list_tools_over_json_and_sse():
    seen = []
    tools = await mcp.list_tools("https://gh.example/mcp", TOKEN, _server(seen))
    assert [(t["name"], t["read_only"]) for t in tools] == [
        ("search_issues", True),
        ("create_issue", False),
        ("get_issue", True),
    ]
    methods = [m for m, _ in seen]
    assert methods == ["initialize", "notifications/initialized", "tools/list", "tools/list"]
    assert seen[0][1]["authorization"] == f"Bearer {TOKEN}"
    assert seen[2][1]["mcp-session-id"] == "s1"


@pytest.fixture
def log(monkeypatch):
    fresh = errorlog.ErrorLog()
    monkeypatch.setattr(errorlog, "LOG", fresh)
    return fresh


def _add(conn):
    tools = [
        {"name": "search_issues", "description": "", "read_only": True},
        {"name": "create_issue", "description": "", "read_only": False},
    ]
    return mcp.add(conn, "GitHub", "https://gh.example/mcp", TOKEN, tools)


def test_add_enables_only_read_only_tools_and_redacts_token(conn, log):
    sid = _add(conn)
    assert [(t["name"], t["enabled"]) for t in mcp.tools_of(conn, sid)] == [
        ("search_issues", 1),
        ("create_issue", 0),
    ]
    assert log.redact(f"failed with {TOKEN}") == "failed with [redacted]"
    with pytest.raises(ValueError, match="https"):
        mcp.add(conn, "plain", "http://x.example/mcp", None, [])
    with pytest.raises(ValueError, match="name"):
        mcp.add(conn, "Bad Name!", "https://x.example/mcp", None, [])


def test_relevance_and_request_shape(conn, log):
    sid = _add(conn)
    assert mcp.relevant(conn, "what's due tomorrow?") == []
    assert [s["name"] for s in mcp.relevant(conn, "check github for my open bugs")] == ["github"]
    assert [s["name"] for s in mcp.relevant(conn, "run search_issues for pai")] == ["github"]
    assert mcp.relevant(conn, "create_issue please") == []  # write tool still off
    servers, toolsets = mcp.request_parts(conn, mcp.relevant(conn, "github"))
    assert servers == [
        {
            "type": "url",
            "url": "https://gh.example/mcp",
            "name": "github",
            "authorization_token": TOKEN,
        }
    ]
    assert toolsets == [
        {
            "type": "mcp_toolset",
            "mcp_server_name": "github",
            "default_config": {"enabled": False},
            "configs": {"search_issues": {"enabled": True}},
        }
    ]
    tool_id = mcp.tools_of(conn, sid)[1]["id"]
    mcp.set_tool(conn, tool_id, True)
    assert "create_issue" in mcp.request_parts(conn, mcp.servers(conn))[1][0]["configs"]
    mcp.set_server(conn, "github", False)
    assert mcp.relevant(conn, "github") == []
    assert mcp.set_server(conn, "github", None) and mcp.servers(conn) == []


async def test_agent_adds_connector_only_when_asked(conn, config, ctx):
    client = FakeClient(
        response([text_block("x")], stop_reason="pause_turn"),
        response([text_block("3 open bugs.")]),
        response([text_block("hi")]),
    )
    agent = Agent(client, conn, config, [], system_prompt="x")
    parts = (
        [{"type": "url", "url": "https://gh.example/mcp", "name": "github"}],
        [{"type": "mcp_toolset", "mcp_server_name": "github"}],
    )
    reply = await agent.run("github bugs?", ctx, mcp=parts)
    assert reply.text == "3 open bugs." and reply.rounds == 2  # pause_turn resumed
    first = client.requests[0]
    assert first["mcp_servers"] == parts[0] and first["tools"][-1] == parts[1][0]
    assert MCP_BETA in first["betas"]
    await agent.run("hi", ctx)
    plain = client.requests[-1]
    assert "mcp_servers" not in plain and plain["tools"] == []


async def test_connect_add_deletes_token_message_and_never_echoes_it(
    conn, config, monkeypatch, log
):
    bot = PaiBot(Agent(FakeClient(), conn, config, [], system_prompt="x"), owner_id=1)
    sent, deleted = [], []

    async def send(app, text, **k):
        sent.append(text)

    async def fake_list(url, token):
        return [
            {"name": "search_issues", "description": "", "read_only": True},
            {"name": "create_issue", "description": "", "read_only": False},
        ]

    async def delete():
        deleted.append(True)

    bot.send = send
    monkeypatch.setattr(mcp, "list_tools", fake_list)
    update = N(message=N(delete=delete))
    ctx = N(application=None, args=["add", "github", "https://gh.example/mcp", TOKEN])
    await bot.connect_cmd(update, ctx)
    assert deleted == [True]
    assert sent[0].startswith("🔌 Connected *github*: 2 tools, 1 read-only ones on.")
    assert "✅ search_issues" in sent[1] and "▫️ create_issue (writes)" in sent[1]
    assert all(TOKEN not in s for s in sent)
    assert mcp.servers(conn)[0]["token"] == TOKEN  # stored, just never shown
