from datetime import date, datetime, time, timedelta

from conftest import FakeClient
from factories import assignment

from pai.agent import Agent
from pai.availability import FreeBlock
from pai.config import Config
from pai.planner import Block, StudyPlan, format_plan, latest_plan, make_plan, validate

CFG = Config(timezone="America/Los_Angeles")
TZ = CFG.tz
MON = date(2026, 10, 12)
NOW = datetime(2026, 10, 11, 20, 0, tzinfo=TZ)
FREE = [FreeBlock(MON, time(17, 30), time(22, 0))]
DUE = datetime(2026, 10, 12, 23, 59, tzinfo=TZ)
ITEMS = [assignment(1, due=DUE)]


def plan(*blocks):
    return StudyPlan(blocks=list(blocks), notes=[], at_risk=[])


def blk(start, end, aid=1, day=MON):
    return Block(day=day, start=start, end=end, course="CS", task="t", assignment_id=aid)


def test_valid_plan():
    assert validate(plan(blk(time(18), time(19))), FREE, ITEMS, TZ, NOW) == []


def test_outside_free_block():
    problems = validate(plan(blk(time(16), time(17))), FREE, ITEMS, TZ, NOW)
    assert "not inside any free block" in problems[0]


def test_after_due():
    early_due = [assignment(1, due=datetime(2026, 10, 12, 18, 30, tzinfo=TZ))]
    problems = validate(plan(blk(time(18), time(19))), FREE, early_due, TZ, NOW)
    assert "ends after the assignment is due" in problems[0]


def test_unknown_assignment():
    problems = validate(plan(blk(time(18), time(19), aid=999)), FREE, ITEMS, TZ, NOW)
    assert "unknown assignment_id" in problems[0]


def test_format_groups_by_day():
    text = format_plan(
        StudyPlan(blocks=[blk(time(18), time(19))], notes=["n"], at_risk=["r"]), "Plan"
    )
    assert "*Mon Oct 12*" in text and "18:00–19:00" in text and "*At risk*" in text


class FakeCanvas:
    def __init__(self, items):
        self.items = items

    async def all_assignments(self, fresh=False):
        return self.items


class ParseClient(FakeClient):
    def __init__(self, *plans):
        super().__init__()
        self.plans = list(plans)
        self.beta.messages.parse = self._parse

    async def _parse(self, **kwargs):
        from types import SimpleNamespace

        from conftest import usage

        self.requests.append(kwargs)
        return SimpleNamespace(
            parsed_output=self.plans.pop(0),
            stop_reason="end_turn",
            model="claude-sonnet-5-5",
            usage=usage(),
        )


async def test_make_plan_retries_then_drops_bad_blocks(conn, ctx, tmp_path):
    config = Config.model_validate(
        {
            "timezone": "America/Los_Angeles",
            "db_path": str(tmp_path / "x.db"),
            "schedule": {
                "work": [{"days": ["mon"], "start": "08:00", "end": "16:30"}],
                "study": [{"days": ["mon"], "start": "17:30", "end": "22:00"}],
            },
        }
    )
    bad = plan(blk(time(12), time(13)))
    still_bad = plan(blk(time(12), time(13)), blk(time(18), time(19)))
    client = ParseClient(bad, still_bad)
    ctx.config = config
    ctx.agent = Agent(client, conn, config, [], system_prompt="x")
    ctx.canvas = FakeCanvas([assignment(1, due=DUE + timedelta(days=1))])

    text = await make_plan(ctx, MON, MON, NOW)
    assert len(client.requests) == 2
    assert "broke these rules" in client.requests[1]["messages"][0]["content"]
    assert client.requests[0]["output_format"] is StudyPlan
    assert client.requests[0]["fallbacks"] == "default"  # Sonnet route
    saved = latest_plan(conn, MON)
    assert [(b.start, b.end) for b in saved.blocks] == [(time(18), time(19))]
    assert "18:00–19:00" in text and "12:00" not in text
    assert "[t](https://canvas.test/a/1)" in text  # linked to Canvas
    assert saved.blocks[0].url == "https://canvas.test/a/1"
