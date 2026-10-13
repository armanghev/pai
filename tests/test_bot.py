from types import SimpleNamespace

import pytest
from telegram.ext import ApplicationHandlerStop

from pai.bot import PaiBot, split_message


def test_short_message_untouched():
    assert split_message("hi") == ["hi"]


def test_split_prefers_paragraphs():
    text = ("a" * 3000) + "\n\n" + ("b" * 3000)
    assert split_message(text) == ["a" * 3000, "b" * 3000]


def test_split_hard_cuts_long_lines():
    chunks = split_message("x" * 9000)
    assert [len(c) for c in chunks] == [4096, 4096, 808]


def _update(uid):
    return SimpleNamespace(effective_user=SimpleNamespace(id=uid, username="u"))


async def test_gate_lets_owner_through():
    bot = PaiBot(agent=None, owner_id=42)
    await bot.gate(_update(42), None)  # no exception


async def test_gate_drops_strangers_and_logs_once(caplog):
    bot = PaiBot(agent=None, owner_id=42)
    for _ in range(3):
        with pytest.raises(ApplicationHandlerStop):
            await bot.gate(_update(7), None)
    assert sum("non-owner 7" in r.message for r in caplog.records) == 1


async def test_gate_drops_updates_without_user():
    bot = PaiBot(agent=None, owner_id=42)
    with pytest.raises(ApplicationHandlerStop):
        await bot.gate(SimpleNamespace(effective_user=None), None)


async def test_restart_announced_once(conn):
    from pai import db
    from pai.bot import RESTART_KEY

    sent = []

    class FakeBot(PaiBot):
        async def send(self, app, text):
            sent.append(text)

    bot = FakeBot(agent=SimpleNamespace(conn=conn), owner_id=42)
    await bot.announce_restart(None)
    assert sent == []
    db.kv_set(conn, RESTART_KEY, "1")
    await bot.announce_restart(None)
    await bot.announce_restart(None)
    assert len(sent) == 1 and "Update done" in sent[0]
