from types import SimpleNamespace as N

import pytest
from conftest import FakeClient, response, text_block, tool_block

from pai import routines
from pai.agent import COST_CAP, Agent
from pai.bot import PaiBot
from pai.tools import Tool, default_tools
from pai.tools.routines import create_routine


def test_parse_and_describe():
    assert routines.parse_days(["Friday"]) == ["fri"]
    assert routines.parse_days(["weekdays"]) == ["mon", "tue", "wed", "thu", "fri"]
    assert routines.parse_days(["daily"]) == routines.DAYS
    with pytest.raises(ValueError):
        routines.parse_days(["someday"])
    assert routines.parse_time("7:05") == "07:05"
    with pytest.raises(ValueError):
        routines.parse_time("5pm")
    assert routines.describe(["fri"], "17:00") == "every Fri at 17:00"
    assert routines.describe(routines.WEEKDAYS, "07:30") == "weekdays at 07:30"


def test_cap_counts_confirmed_jobs_only(conn):
    for i in range(routines.MAX_ROUTINES):
        rid = routines.propose(conn, f"job {i}", "do it", ["mon"], "09:00")
        routines.set_status(conn, rid, "active")
    with pytest.raises(ValueError, match="already 10"):
        routines.propose(conn, "one more", "x", ["mon"], "09:00")


async def test_create_routine_asks_with_buttons(ctx):
    asked = []

    async def send_buttons(text, rows):
        asked.append((text, rows))

    ctx.send_buttons = send_buttons
    out = await create_routine(
        {
            "title": "Friday due list",
            "prompt": "Send what's due next week.",
            "days": ["fri"],
            "time": "17:00",
        },
        ctx,
    )
    assert "isn't active until they tap Create" in out
    ((text, rows),) = asked
    assert text.startswith("🔁 New recurring job: *Friday due list*, every Fri at 17:00")
    assert rows == [[("✅ Create", "j:ok:1"), ("✖️ Cancel", "j:no:1")]]
    assert routines.get(ctx.conn, 1)["status"] == "pending"


async def test_scheduled_runs_skip_history_and_stop_at_cost(conn, config, ctx):
    calls = []

    async def noop(args, ctx):
        calls.append(1)
        return "ok"

    tool = Tool(
        "noop", "x", {"type": "object", "properties": {}, "additionalProperties": False}, noop
    )
    conn.execute("INSERT INTO messages (role, text) VALUES ('user', 'old chat')")
    cheap = response([tool_block("noop")], stop_reason="tool_use")
    pricey = response([tool_block("noop")], stop_reason="tool_use", inp=1_000_000)
    client = FakeClient(cheap, pricey)
    reply = await Agent(client, conn, config, [tool], system_prompt="x").run(
        "job", ctx, job="routine", history=False, max_cost=0.01
    )
    assert reply.text == COST_CAP and calls == [1]
    assert len(client.requests) == 2
    first = client.requests[0]["messages"][0]["content"]
    assert first.endswith("\njob") and "old chat" not in first  # no replayed history
    assert conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 1  # nothing saved


class FakeQueue:
    def __init__(self):
        self.jobs = []

    def run_daily(self, cb, time, days, data, name):
        job = N(cb=cb, time=time, days=days, data=data, name=name, removed=False)
        job.schedule_removal = lambda: setattr(job, "removed", True)
        self.jobs.append(job)

    def get_jobs_by_name(self, name):
        return [j for j in self.jobs if j.name == name and not j.removed]

    def run_repeating(self, *a, **k):
        pass

    def run_once(self, *a, **k):
        pass


def _bot(conn, config, client):
    agent = Agent(client, conn, config, default_tools(), system_prompt="x")
    bot = PaiBot(agent, owner_id=1)
    sent = []

    async def send(app, text, silent=False, markup=None, html=False):
        sent.append((text, silent, markup))

    bot.send = send
    q = FakeQueue()
    app = N(job_queue=q, bot=None)
    return bot, app, q, sent


async def test_confirm_schedules_and_run_reports(conn, config):
    client = FakeClient(response([text_block("3 things due next week.")]))
    bot, app, q, sent = _bot(conn, config, client)
    rid = routines.propose(conn, "Friday due list", "Send what's due.", ["fri"], "17:00")
    edits = []

    async def answer(text=None):
        edits.append(("answer", text))

    async def edit(text, **k):
        edits.append(("edit", text))

    query = N(data=f"j:ok:{rid}", answer=answer, edit_message_text=edit)
    await bot.routine_button(N(callback_query=query), N(application=app))
    assert routines.get(conn, rid)["status"] == "active"
    (job,) = q.get_jobs_by_name(f"routine:{rid}")
    assert job.days == (5,) and f"{job.time:%H:%M}" == "17:00"
    assert ("answer", "Created") in edits

    await job.cb(N(application=app, job=N(data=rid)))
    text, silent, _ = sent[-1]
    assert text == "🔁 *Friday due list*\n3 things due next week."
    request = client.requests[0]
    assert (
        'Scheduled job "Friday due list" (every Fri at 17:00)' in request["messages"][-1]["content"]
    )
    assert request["model"] == "claude-haiku-5-5"
    assert routines.get(conn, rid)["last_run_at"] is not None

    pause = N(data=f"j:pause:{rid}", answer=answer, edit_message_text=edit)
    await bot.routine_button(N(callback_query=pause), N(application=app))
    assert routines.get(conn, rid)["status"] == "paused" and job.removed is True
    await job.cb(N(application=app, job=N(data=rid)))  # paused: does nothing
    assert len(client.requests) == 1


async def test_reload_on_startup(conn, config):
    bot, app, q, _ = _bot(conn, config, FakeClient())
    a = routines.propose(conn, "a", "x", ["mon"], "08:00")
    b = routines.propose(conn, "b", "x", ["tue"], "08:00")
    routines.propose(conn, "c", "x", ["wed"], "08:00")  # never confirmed
    routines.set_status(conn, a, "active")
    routines.set_status(conn, b, "paused")
    assert bot.jobs.reload_routines(app) == 1
    assert [j.name for j in q.jobs] == [f"routine:{a}"]
