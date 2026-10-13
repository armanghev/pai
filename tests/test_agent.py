from datetime import UTC, datetime

from conftest import FakeClient, response, text_block, tool_block

from pai import agent as agent_mod
from pai.agent import Agent
from pai.tools import Tool, default_tools

NOW = datetime(2026, 10, 10, 20, 0, tzinfo=UTC)  # Saturday 13:00 in Los Angeles


def make_agent(client, conn, config, tools=None):
    return Agent(client, conn, config, tools or default_tools(), system_prompt="You are Pai.")


async def test_plain_reply_saved_to_history(conn, config, ctx):
    client = FakeClient(response([text_block("Hoo, hi!")]))
    a = make_agent(client, conn, config)
    reply = await a.run("hello", ctx, now=NOW)
    assert reply.text == "Hoo, hi!"
    assert reply.rounds == 1
    rows = conn.execute("SELECT role, text FROM messages ORDER BY id").fetchall()
    assert [(r["role"], r["text"]) for r in rows] == [("user", "hello"), ("assistant", "Hoo, hi!")]


async def test_request_shape_is_cache_friendly(conn, config, ctx):
    client = FakeClient(response([text_block("a")]), response([text_block("b")]))
    a = make_agent(client, conn, config)
    await a.run("one", ctx, now=NOW)
    await a.run("two", ctx, now=NOW)
    first, second = client.requests
    # Frozen prefix: same tools and system bytes on every call, cache breakpoint on system.
    assert first["tools"] == second["tools"]
    assert first["system"] == second["system"]
    assert first["system"][-1]["cache_control"] == {"type": "ephemeral"}
    # The date lives in the user message, not the system prompt.
    assert "Saturday 2026-10-10 13:00" in first["messages"][-1]["content"]
    assert "2026" not in first["system"][0]["text"]
    # Haiku: low effort, no server-side fallback.
    assert first["model"] == "claude-haiku-5-5"
    assert first["output_config"] == {"effort": "low"}
    assert "fallbacks" not in first
    # History carries the previous text turns.
    assert [m["role"] for m in second["messages"]] == ["user", "assistant", "user"]


async def test_tool_loop_runs_tools_and_returns_results_together(conn, config, ctx):
    calls = []

    async def echo(args, _ctx):
        calls.append(args)
        return {"echo": args["x"]}

    async def boom(args, _ctx):
        raise RuntimeError("nope")

    tools = [
        Tool("echo", "echo", {"type": "object", "properties": {"x": {"type": "string"}}}, echo),
        Tool("boom", "boom", {"type": "object", "properties": {}}, boom),
    ]
    client = FakeClient(
        response(
            [tool_block("echo", {"x": "1"}, "a"), tool_block("boom", {}, "b")],
            stop_reason="tool_use",
        ),
        response([text_block("done")]),
    )
    a = make_agent(client, conn, config, tools)
    reply = await a.run("go", ctx, now=NOW)
    assert reply.text == "done" and reply.rounds == 2
    assert calls == [{"x": "1"}]
    tool_results = client.requests[1]["messages"][-1]["content"]
    assert [r["tool_use_id"] for r in tool_results] == ["a", "b"]
    assert tool_results[0]["content"] == '{"echo": "1"}'
    assert tool_results[1]["is_error"] is True
    assert "nope" in tool_results[1]["content"]
    # Tool calls never reach saved history.
    assert conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 2


async def test_unknown_tool_is_an_error_result(conn, config, ctx):
    client = FakeClient(
        response([tool_block("nonexistent")], stop_reason="tool_use"),
        response([text_block("sorry")]),
    )
    await make_agent(client, conn, config).run("go", ctx, now=NOW)
    assert client.requests[1]["messages"][-1]["content"][0]["is_error"] is True


async def test_round_cap(conn, config, ctx, monkeypatch):
    monkeypatch.setattr(agent_mod, "MAX_ROUNDS", 3)
    client = FakeClient(
        *[response([tool_block("pi_health")], stop_reason="tool_use") for _ in range(3)]
    )
    reply = await make_agent(client, conn, config).run("loop", ctx, now=NOW)
    assert reply.text == agent_mod.ROUND_CAP
    assert len(client.requests) == 3


async def test_haiku_refusal_retries_once_on_sonnet(conn, config, ctx):
    client = FakeClient(
        response([], stop_reason="refusal"),
        response([text_block("ok")], model="claude-sonnet-5-5"),
    )
    reply = await make_agent(client, conn, config).run("hmm", ctx, now=NOW)
    assert reply.text == "ok"
    retry = client.requests[1]
    assert retry["model"] == "claude-sonnet-5-5"
    assert retry["fallbacks"] == "default"
    assert retry["betas"] == [agent_mod.FALLBACK_BETA]
    assert retry["output_config"] == {"effort": "medium"}


async def test_refusal_after_retry_gives_up(conn, config, ctx):
    client = FakeClient(response([], stop_reason="refusal"), response([], stop_reason="refusal"))
    reply = await make_agent(client, conn, config).run("hmm", ctx, now=NOW)
    assert reply.text == agent_mod.REFUSED


async def test_max_tokens_is_flagged(conn, config, ctx):
    client = FakeClient(response([text_block("long")], stop_reason="max_tokens"))
    reply = await make_agent(client, conn, config).run("essay", ctx, now=NOW)
    assert reply.text.endswith(agent_mod.TRUNCATED_NOTE)


async def test_usage_recorded_per_call(conn, config, ctx):
    client = FakeClient(
        response([tool_block("pi_health")], stop_reason="tool_use", inp=1000, out=100),
        response([text_block("warm")], inp=1000, out=100, read=2000),
    )
    reply = await make_agent(client, conn, config).run("temp?", ctx, now=NOW)
    rows = conn.execute("SELECT model, cache_read_tokens, job FROM api_usage").fetchall()
    assert len(rows) == 2
    assert rows[1]["cache_read_tokens"] == 2000
    assert reply.cost_usd > 0


async def test_budget_guard_skips_claude(conn, config, ctx):
    conn.execute(
        "INSERT INTO api_usage (model, cost_usd, created_at) VALUES ('x', 5.0, ?)",
        ("2026-10-10T19:00:00.000Z",),
    )
    client = FakeClient()
    reply = await make_agent(client, conn, config).run("hi", ctx, now=NOW)
    assert reply.text == agent_mod.BUDGET_HIT
    assert client.requests == []


async def test_history_is_capped_and_starts_with_user(conn, config, ctx):
    for i in range(5):
        conn.execute("INSERT INTO messages (role, text) VALUES ('user', ?)", (f"u{i}",))
        conn.execute("INSERT INTO messages (role, text) VALUES ('assistant', ?)", (f"a{i}",))
    conn.execute("INSERT INTO messages (role, text) VALUES ('assistant', 'extra')")
    a = make_agent(FakeClient(), conn, config)
    hist = a.history()
    assert len(hist) <= agent_mod.HISTORY_TURNS
    assert hist[0]["role"] == "user"
    assert hist[-1]["content"].endswith("\nextra")


async def test_history_turns_carry_the_time_they_were_sent(conn, config, ctx):
    sunday = datetime(2026, 10, 12, 2, 2, tzinfo=UTC)  # Sunday 19:02 PDT
    monday = datetime(2026, 10, 12, 16, 30, tzinfo=UTC)  # Monday 09:30 PDT
    client = FakeClient(
        response([text_block("Essay 3 is due tonight at 11:59.")]),
        response([text_block("Morning!")]),
    )
    a = make_agent(client, conn, config)
    await a.run("what's due?", ctx, now=sunday)
    await a.run("hey pai", ctx, now=monday)

    msgs = client.requests[1]["messages"]
    assert [m["role"] for m in msgs] == ["user", "assistant", "user"]
    assert msgs[0]["content"] == "[Sunday 2026-10-11 19:02 PDT]\nwhat's due?"
    assert msgs[1]["content"] == "[sent Sunday 19:02]\nEssay 3 is due tonight at 11:59."
    assert msgs[2]["content"] == "[Monday 2026-10-12 09:30 PDT]\nhey pai"
    # Stored text stays undated, so nothing is ever stamped twice.
    rows = conn.execute("SELECT text FROM messages ORDER BY id").fetchall()
    assert [r["text"] for r in rows][:2] == ["what's due?", "Essay 3 is due tonight at 11:59."]


def test_system_prompt_ships_with_package():
    text = agent_mod.load_system_prompt()
    assert "Pai" in text and "owl" in text
    assert "draft_assignment" in text


def test_final_text_drops_working_notes():
    from types import SimpleNamespace as N

    content = [
        N(type="text", text="Checking candidates."),
        N(type="server_tool_use"),
        N(type="web_search_tool_result"),
        N(type="text", text="Here are the "),
        N(type="text", text="results."),
    ]
    assert agent_mod.final_text(content) == "Here are the results."
    assert agent_mod.final_text([N(type="text", text="only")]) == "only"
