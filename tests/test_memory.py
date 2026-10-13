from conftest import FakeClient, response, text_block

from pai import memory
from pai.agent import Agent
from pai.tools import default_tools
from pai.tools.memory import forget, remember


def test_add_dedupes_and_block_caps(conn):
    a = memory.add(conn, "Hates  studying before 10am", "preference")
    assert memory.add(conn, "hates studying before 10AM") == a
    assert memory.add(conn, "x", "not-a-category") and memory.all_(conn)[-1]["category"] == "other"
    for i in range(60):
        memory.add(conn, f"fact number {i} " + "y" * 60)
    block = memory.block(conn)
    assert block.startswith("[What you remember about the owner]\n")
    lines = block.splitlines()[1:]
    assert len(lines) <= memory.MAX_FACTS and len(block) <= memory.MAX_CHARS + 60
    assert "fact number 59" in block  # newest kept
    ids = [int(line.split()[1].lstrip("#")) for line in lines]
    assert ids == sorted(ids)  # shown in the order they were saved


def test_empty_memory_adds_nothing(conn):
    assert memory.block(conn) == ""


async def test_memory_rides_in_latest_message_not_system(conn, config, ctx):
    client = FakeClient(response([text_block("a")]), response([text_block("b")]))
    agent = Agent(client, conn, config, default_tools(), system_prompt="You are Pai.")
    await agent.run("hi", ctx)
    memory.add(conn, "Works Tuesday and Thursday evenings", "work")
    await agent.run("plan my week", ctx)
    first, second = client.requests
    assert "remember" not in first["messages"][-1]["content"]
    latest = second["messages"][-1]["content"]
    assert latest.startswith("[What you remember about the owner]\n- #1 Works Tuesday")
    assert latest.endswith("plan my week")
    assert first["system"] == second["system"] and first["tools"] == second["tools"]
    # History replays the plain text, so memories aren't repeated in older turns.
    assert all("remember about" not in m["content"] for m in second["messages"][:-1])


async def test_remember_and_forget_tools(ctx):
    out = await remember({"fact": "Prefers Python examples", "category": "preference"}, ctx)
    assert out.startswith("Saved as memory #1.")
    memory.add(ctx.conn, "Works at the restaurant on Tuesdays")
    memory.add(ctx.conn, "Works out on Fridays")
    assert (await forget({"text": "works"}, ctx)).startswith("Several match")
    assert await forget({"text": "restaurant"}, ctx) == (
        "Forgot #2: Works at the restaurant on Tuesdays"
    )
    assert await forget({"memory_id": 1}, ctx) == "Forgot #1: Prefers Python examples"
    assert await forget({"memory_id": 1}, ctx) == "No memory with that id."
