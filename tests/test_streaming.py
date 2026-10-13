from types import SimpleNamespace as N

from conftest import FakeClient, response, text_block, tool_block
from telegram.error import BadRequest

from pai.agent import Agent
from pai.bot import Draft
from pai.tools import Tool


class StreamClient(FakeClient):
    """Like FakeClient, but messages.stream yields text deltas of each scripted response."""

    def __init__(self, *responses):
        super().__init__(*responses)
        self.beta.messages.stream = self._stream

    def _stream(self, **kwargs):
        self.requests.append(kwargs)
        final = self.responses.pop(0)

        class Stream:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            def __aiter__(self):
                async def gen():
                    for b in final.content:
                        if b.type == "text":
                            for piece in b.text.split(" "):
                                yield N(
                                    type="content_block_delta",
                                    delta=N(type="text_delta", text=piece + " "),
                                )

                return gen()

            async def get_final_message(self):
                return final

        return Stream()


async def test_streamed_rounds_feed_on_text_and_keep_the_loop(conn, config, ctx):
    async def noop(args, ctx):
        return "ok"

    tool = Tool(
        "noop", "x", {"type": "object", "properties": {}, "additionalProperties": False}, noop
    )
    client = StreamClient(
        response([text_block("Checking"), tool_block("noop")], stop_reason="tool_use"),
        response([text_block("Lab 4 is due tonight.")]),
    )
    seen = []

    async def on_text(text):
        seen.append(text)

    reply = await Agent(client, conn, config, [tool], system_prompt="x").run(
        "what's due", ctx, on_text=on_text
    )
    assert reply.text == "Lab 4 is due tonight." and reply.rounds == 2
    assert seen[0] == "Checking " and seen[-1] == "Lab 4 is due tonight. "  # resets per round
    rows = conn.execute("SELECT role, text FROM messages ORDER BY id").fetchall()
    assert [r["text"] for r in rows] == ["what's due", "Lab 4 is due tonight."]
    assert conn.execute("SELECT COUNT(*) FROM api_usage").fetchone()[0] == 2  # spend kept


async def test_without_on_text_nothing_streams(conn, config, ctx):
    client = FakeClient(response([text_block("hi")]))  # has no .stream at all
    reply = await Agent(client, conn, config, [], system_prompt="x").run("hi", ctx)
    assert reply.text == "hi"


async def test_draft_throttles_and_turns_off_on_error(monkeypatch):
    calls = []
    fail = {"on": False}

    async def send_message_draft(chat_id, draft_id, text):
        if fail["on"]:
            raise BadRequest("drafts not allowed")
        calls.append(text)

    clock = {"t": 100.0}
    monkeypatch.setattr("pai.bot.clock.monotonic", lambda: clock["t"])
    d = Draft(N(send_message_draft=send_message_draft), 1, 99)
    await d.show("", force=True)
    await d("Lab")  # too soon after the placeholder
    clock["t"] += 1
    await d("Lab 4 is")
    assert calls == ["", "Lab 4 is"]
    fail["on"] = True
    clock["t"] += 1
    await d("Lab 4 is due")
    assert d.enabled is False
    clock["t"] += 1
    fail["on"] = False
    await d("Lab 4 is due tonight")
    assert calls == ["", "Lab 4 is"]  # stays off for this reply
