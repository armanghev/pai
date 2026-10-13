from datetime import UTC, datetime, timedelta
from types import SimpleNamespace as N

from factories import assignment

from pai import heartbeat
from pai.config import Config
from pai.jobs import Jobs
from pai.tools.heartbeat import edit_heartbeat

CFG = Config(timezone="America/Los_Angeles")
TZ = CFG.tz
NOW = datetime(2026, 10, 12, 18, 30, tzinfo=TZ)  # Monday evening
ALL = {"deadlines_soon", "due_today_untouched", "break_after_shift", "inbox_unforwarded", "errors"}


def test_default_checklist_is_seeded(conn):
    assert {r["kind"] for r in heartbeat.items(conn)} == ALL


def test_facts_from_each_item(conn):
    items = [
        assignment(1, due=NOW + timedelta(hours=2)),  # soon
        assignment(2, due=NOW.replace(hour=23, minute=59)),  # later today, untouched
        assignment(3, due=NOW.replace(hour=23, minute=0)),  # later today, worked on
        assignment(4, due=NOW + timedelta(days=2)),
    ]
    unread = [
        {"id": 7, "from": "Dr. Lee", "subject": "Essay 2"},
        {"id": 8, "from": "x", "subject": "old"},
    ]
    errors = [N(id=5, logger="pai.jobs", message="job leads failed")]
    facts = heartbeat.facts(conn, ALL, NOW, TZ, items, {3}, unread, {"8"}, errors, "16:30", True)
    keys = [f.key for f in facts]
    assert keys == ["soon:1", "untouched:2", "break:2026-10-12", "inbox:7", "err:5"]
    assert facts[0].text == "due in 2h 00m: CS/IT 135 Lab 1 (no time logged)"
    no_break = heartbeat.facts(
        conn, ALL, NOW.replace(hour=17), TZ, [], set(), [], set(), [], "16:30", True
    )
    assert no_break == []  # too soon after the shift


def test_dedupe_ttl(conn):
    f = [heartbeat.Fact("soon:1", "x"), heartbeat.Fact("err:5", "y")]
    heartbeat.mark(conn, ["soon:1"], NOW)
    assert [x.key for x in heartbeat.fresh(conn, f, NOW + timedelta(hours=5))] == ["err:5"]
    assert len(heartbeat.fresh(conn, f, NOW + timedelta(hours=7))) == 2


class Agent:
    def __init__(self, conn, beats):
        self.conn, self.beats, self.calls = conn, list(beats), []

    async def structured(self, route, system, user, schema, job):
        self.calls.append((route, user, job))
        return self.beats.pop(0)


def _jobs(conn, config, beats, items=()):
    sent = []

    async def send(app, text, **k):
        sent.append(text)

    async def all_assignments():
        return list(items)

    async def unread_messages():
        return []

    canvas = N(all_assignments=all_assignments, unread_messages=unread_messages)
    agent = Agent(conn, beats)
    bot = N(
        agent=N(conn=conn, config=config, structured=agent.structured), canvas=canvas, send=send
    )
    jobs = Jobs(bot)
    return jobs, agent, sent


async def test_beat_calls_haiku_only_when_something_is_new(conn, config, monkeypatch):
    monkeypatch.setattr(Jobs, "quiet_now", lambda self: False)
    soon = datetime.now(UTC) + timedelta(hours=1)
    beat = heartbeat.Beat(
        ok=False, message="Lab 1 is due in an hour and untouched.", keys=["soon:1"]
    )
    jobs, agent, sent = _jobs(conn, config, [beat], [assignment(1, due=soon)])
    for k in ("break_after_shift",):
        conn.execute("UPDATE heartbeat_items SET enabled = 0 WHERE kind = ?", (k,))
    await jobs.heartbeat(None)
    assert sent == ["💓 Lab 1 is due in an hour and untouched."]
    route, user, job = agent.calls[0]
    assert route == "chat" and job == "heartbeat" and "[soon:1] due in" in user
    await jobs.heartbeat(None)  # same fact, already flagged: no model call
    assert len(agent.calls) == 1


async def test_nothing_new_means_no_call_and_ok_means_no_message(conn, config, monkeypatch):
    monkeypatch.setattr(Jobs, "quiet_now", lambda self: False)
    jobs, agent, sent = _jobs(conn, config, [])
    conn.execute("UPDATE heartbeat_items SET enabled = 0 WHERE kind = 'break_after_shift'")
    await jobs.heartbeat(None)
    assert agent.calls == [] and sent == []
    heartbeat.add_item(conn, "remind me to drink water if I'm studying late")
    jobs, agent, sent = _jobs(conn, config, [heartbeat.Beat(ok=True, message="", keys=[])])
    await jobs.heartbeat(None)
    assert len(agent.calls) == 1 and sent == []
    assert "Also watch for" in agent.calls[0][1]


async def test_skips_in_quiet_hours_and_near_spend_cap(conn, config, monkeypatch):
    heartbeat.add_item(conn, "anything")
    jobs, agent, sent = _jobs(conn, config, [])
    monkeypatch.setattr(Jobs, "quiet_now", lambda self: True)
    await jobs.heartbeat(None)
    monkeypatch.setattr(Jobs, "quiet_now", lambda self: False)
    conn.execute(
        "INSERT INTO api_usage (model, cost_usd, created_at) VALUES ('x', ?, ?)",
        (config.budget.daily_usd * 0.95, datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.000Z")),
    )
    await jobs.heartbeat(None)
    assert agent.calls == []


async def test_edit_checklist_from_chat(ctx):
    assert (await edit_heartbeat({"action": "add", "text": "also watch for gym"}, ctx)).startswith(
        "Added #6."
    )
    out = await edit_heartbeat({"action": "remove", "text": "dinner"}, ctx)
    assert out.startswith("Stopped watching: No dinner or break after the shift.")
    row = ctx.conn.execute(
        "SELECT enabled FROM heartbeat_items WHERE kind = 'break_after_shift'"
    ).fetchone()
    assert row[0] == 0  # built-in items are switched off, not deleted
    await edit_heartbeat({"action": "remove", "text": "gym"}, ctx)
    assert "gym" not in await edit_heartbeat({"action": "list"}, ctx)
    assert "Need exactly one match" in await edit_heartbeat({"action": "pause", "text": "the"}, ctx)
