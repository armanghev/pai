import logging
from types import SimpleNamespace as N

import pytest
from conftest import FakeClient, response, text_block

from pai import errorlog, fixer
from pai.agent import Agent
from pai.bot import PaiBot
from pai.tools import default_tools

BOT_TOKEN = "123456789:" + "A" * 35


@pytest.fixture
def log(monkeypatch):
    fresh = errorlog.ErrorLog(secrets=["canvas-secret-value-123"])
    monkeypatch.setattr(errorlog, "LOG", fresh)
    logger = logging.getLogger("pai.test_fixer")
    logger.addHandler(fresh)
    yield logger
    logger.removeHandler(fresh)


def test_captures_warnings_with_tracebacks_and_redacts(log):
    log.info("not kept")
    log.warning("canvas said 401 for canvas-secret-value-123")
    try:
        raise RuntimeError(f"bad token {BOT_TOKEN} and https://x/y?verifier=abc123&z=1")
    except RuntimeError:
        log.exception("job leads failed")
    first, second = errorlog.LOG.recent()
    assert first.level == "WARNING" and first.message == "canvas said 401 for [redacted]"
    assert second.level == "ERROR" and "Traceback" in second.trace
    assert BOT_TOKEN not in second.trace and "verifier=[redacted]&z=1" in second.trace
    assert [r.id for r in errorlog.LOG.recent(min_level="ERROR")] == [second.id]
    assert errorlog.LOG.get(second.id) is second


def test_buffer_is_bounded(log):
    for i in range(errorlog.MAX_RECORDS + 5):
        log.warning("w%d", i)
    assert len(errorlog.LOG.records) == errorlog.MAX_RECORDS
    assert errorlog.LOG.recent(1)[0].message == f"w{errorlog.MAX_RECORDS + 4}"


async def test_source_tools_stay_inside_the_package(ctx):
    text = await fixer.read_source({"path": "src/pai/bot.py", "start": 1, "end": 3}, ctx)
    assert text.startswith("src/pai/bot.py (") and '   1  """Telegram handlers' in text
    for bad in (
        "../../.env",
        "/etc/passwd",
        "../../config.toml",
        "../../pyproject.toml",
        "migrations/__init__.py/../../../README.md",
    ):
        with pytest.raises(ValueError):
            await fixer.read_source({"path": bad}, ctx)
    hits = await fixer.search_source({"pattern": r"def fix_cmd"}, ctx)
    assert hits.startswith("src/pai/bot.py:")
    assert await fixer.search_source({"pattern": "("}, ctx) == (
        "Bad regex: missing ), unterminated subpattern at position 0"
    )
    assert "src/pai/prompts/fix.md" in fixer.files() and all(
        not f.endswith((".env", ".toml")) for f in fixer.files()
    )


async def test_recent_errors_tool(ctx, log):
    assert (await fixer.recent_errors({}, ctx)).startswith("No warnings or errors")
    log.error("boom")
    assert " ERROR pai.test_fixer: boom" in await fixer.recent_errors({}, ctx)


async def test_fix_runs_on_sonnet_with_only_read_tools(conn, config, log):
    client = FakeClient(response([text_block(f"*What happened* … {BOT_TOKEN}")]))
    agent = Agent(client, conn, config, default_tools(), system_prompt="x")
    bot = PaiBot(agent, owner_id=1)
    sent = []

    async def send(app, text, **k):
        sent.append(text)

    async def chat_action(*a, **k):
        pass

    bot.send = send
    await bot.diagnose(N(bot=N(send_chat_action=chat_action)), "why did /leads fail")
    req = client.requests[0]
    assert req["model"] == config.models.planner
    assert [t["name"] for t in req["tools"]] == ["recent_errors", "read_source", "search_source"]
    assert "Source files:\nsrc/pai/" in req["system"][0]["text"]
    assert sent[-1] == "*What happened* … [redacted]"
    names = {t.name for t in default_tools()}
    assert not names & {"recent_errors", "read_source", "search_source"}
    assert conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 0


async def test_error_alerts_are_rate_limited(conn, config, log):
    bot = PaiBot(Agent(FakeClient(), conn, config, [], system_prompt="x"), owner_id=1)
    sent = []

    async def send(app, text, silent=False, markup=None, html=False):
        sent.append((text, markup))

    bot.send = send
    for i in range(5):
        log.error("boom %d", i)
        await bot.jobs.error_alerts(None)
        await bot.jobs.error_alerts(None)  # nothing new: no second ping
    assert len(sent) == 3
    text, markup = sent[0]
    assert text.startswith("⚠️ Something broke: `pai.test_fixer`: boom 0")
    assert markup.inline_keyboard[0][0].callback_data.startswith("x:")
