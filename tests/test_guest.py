from datetime import UTC, datetime
from types import SimpleNamespace as N

import pytest
from conftest import FakeClient, response, text_block
from telegram import Chat, Message, Update, User
from telegram.error import BadRequest
from telegram.ext import ApplicationHandlerStop

from pai import memory
from pai.agent import Agent
from pai.bot import GUEST_NOTE, PaiBot

OWNER = 42


def _guest_update(uid, text="@pai_owl_bot what's 15% of 80?"):
    msg = Message(
        message_id=1,
        date=datetime.now(UTC),
        chat=Chat(id=-100, type="group"),
        from_user=User(id=uid, first_name="x", is_bot=False),
        text=text,
        api_kwargs={"guest_query_id": "gq1"},
    )
    return Update(update_id=1, guest_message=msg)


def _app(answers, fail_markdown=False):
    async def answer_guest_query(qid, result):
        if fail_markdown and result.input_message_content.parse_mode:
            raise BadRequest("can't parse entities")
        answers.append((qid, result))

    return N(bot=N(username="pai_owl_bot", answer_guest_query=answer_guest_query))


async def test_gate_drops_strangers_who_tag_pai():
    bot = PaiBot(agent=None, owner_id=OWNER)
    await bot.gate(_guest_update(OWNER), None)  # owner passes
    with pytest.raises(ApplicationHandlerStop):
        await bot.gate(_guest_update(7), None)


async def test_owner_mention_is_answered_without_private_context(conn, config):
    memory.add(conn, "Gets a B in COMM 101")
    conn.execute("INSERT INTO messages (role, text) VALUES ('user', 'private chat')")
    client = FakeClient(response([text_block("*12*")]))
    bot = PaiBot(Agent(client, conn, config, [], system_prompt="x"), owner_id=OWNER)
    update = _guest_update(OWNER)
    update.guest_message._unfreeze()
    update.guest_message.guest_query_id = "gq1"
    answers = []
    await bot.guest(update, N(application=_app(answers)))
    content = client.requests[0]["messages"][-1]["content"]
    assert content.endswith(f"{GUEST_NOTE}\n\nwhat's 15% of 80?")
    assert "COMM 101" not in content  # no memories
    assert len(client.requests[0]["messages"]) == 1  # no chat history
    assert conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 1  # nothing saved
    ((qid, result),) = answers
    assert qid == "gq1" and result.input_message_content.message_text == "*12*"


async def test_stranger_mention_is_ignored_and_markdown_falls_back(conn, config):
    client = FakeClient(response([text_block("*unclosed")]))
    bot = PaiBot(Agent(client, conn, config, [], system_prompt="x"), owner_id=OWNER)
    answers = []
    stranger = _guest_update(7)
    await bot.guest(stranger, N(application=_app(answers)))
    assert client.requests == [] and answers == []

    owner = _guest_update(OWNER)
    owner.guest_message._unfreeze()
    owner.guest_message.guest_query_id = "gq2"
    await bot.guest(owner, N(application=_app(answers, fail_markdown=True)))
    ((qid, result),) = answers
    assert result.input_message_content.parse_mode is None
