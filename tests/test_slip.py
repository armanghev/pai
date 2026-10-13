from datetime import date, datetime, time, timedelta

from factories import assignment
from test_planner import FakeCanvas

from pai.availability import FreeBlock
from pai.config import Config
from pai.planner import Block, StudyPlan, latest_plan
from pai.slip import replan_tonight, reschedule

CFG = Config(timezone="America/Los_Angeles")
TZ = CFG.tz
MON = date(2026, 10, 12)
TUE, WED = MON + timedelta(days=1), MON + timedelta(days=2)


def at(d, hh, mm=0):
    return datetime.combine(d, time(hh, mm), TZ)


def blk(d, s, e, course, task, aid):
    return Block(day=d, start=s, end=e, course=course, task=task, assignment_id=aid)


LAB = blk(MON, time(19), time(20), "CS/IT 135", "lab", 1)
QUIZ = blk(MON, time(20), time(21), "CS/IT 101", "quiz review", 2)
READ = blk(MON, time(21), time(21, 45), "COMM 101", "reading", 3)
TOMORROW = blk(TUE, time(18), time(19), "ENG 101", "outline", 4)
PLAN = StudyPlan(blocks=[LAB, QUIZ, READ, TOMORROW], notes=["n"], at_risk=[])
DUES = {1: at(MON, 23, 59), 2: at(WED, 23, 59), 3: at(TUE, 23, 59), 4: at(WED, 12)}
LATER = [FreeBlock(TUE, time(18), time(22)), FreeBlock(WED, time(18), time(22))]


def test_late_start_fits_tonight_first_then_moves_flexible_work():
    tonight = [FreeBlock(MON, time(20), time(22))]  # an hour late: 2h left tonight
    new, changed, wont = reschedule(PLAN, DUES, tonight, LATER, at(MON, 19), TZ)
    assert changed == [
        "CS/IT 135 lab 20:00–21:00 (due tonight)",
        "COMM 101 reading 21:00–21:45",
        "CS/IT 101 quiz review 21:45–22:00",
        "CS/IT 101 quiz review moved to Tue",
    ]
    assert wont == []
    tue = [(b.task, b.start, b.end) for b in new.blocks if b.day == TUE]
    # Tomorrow's existing block is kept; the moved work goes around it.
    assert tue == [("outline", time(18), time(19)), ("quiz review", time(19), time(19, 45))]
    assert new.notes == ["n"]


def test_missed_block_and_work_that_cannot_fit():
    tonight = [FreeBlock(MON, time(21, 30), time(22))]  # missed the 19:00 lab, 30 min left
    new, changed, wont = reschedule(PLAN, DUES, tonight, [], at(MON, 20, 10), TZ, missed=True)
    assert changed[0] == "CS/IT 135 lab 21:30–22:00 (due tonight)"
    assert "CS/IT 135 lab (30 min, due Mon 23:59)" in wont  # due tonight, no room left
    assert any(w.startswith("COMM 101 reading (45 min") for w in wont)
    assert any("Didn't fit after a slip: CS/IT 135 lab" in r for r in new.at_risk)


def test_flexible_work_never_moves_past_its_due_date():
    tonight = []
    later = [FreeBlock(WED, time(18), time(22))]  # reading is due Tue: Wed is too late
    _, changed, wont = reschedule(PLAN, DUES, tonight, later, at(MON, 19), TZ)
    assert "CS/IT 101 quiz review moved to Wed" in changed
    assert not any("reading moved" in c for c in changed)
    assert any(w.startswith("COMM 101 reading") for w in wont)


def test_nothing_left_tonight():
    plan = StudyPlan(blocks=[TOMORROW], notes=[], at_risk=[])
    assert reschedule(plan, DUES, [], LATER, at(MON, 19), TZ) == (plan, [], [])


async def test_replan_tonight_saves_and_reports(conn, ctx, tmp_path):
    ctx.config = Config.model_validate(
        {
            "timezone": "America/Los_Angeles",
            "db_path": str(tmp_path / "x.db"),
            "schedule": {
                "study": [{"days": ["mon", "tue", "wed"], "start": "18:00", "end": "22:00"}]
            },
        }
    )
    ctx.canvas = FakeCanvas([assignment(i, due=DUES[i]) for i in (1, 2, 3, 4)])
    conn.execute(
        "INSERT INTO study_plans (week_start, plan_json) VALUES (?, ?)",
        (MON.isoformat(), PLAN.model_dump_json()),
    )
    text = await replan_tonight(ctx, at(MON, 19), delay=60)
    assert text.startswith("New plan: CS/IT 135 lab 20:01–21:01 (due tonight)")
    assert "moved to Tue" in text
    saved = latest_plan(conn, MON)
    assert [b.task for b in saved.blocks if b.day == MON][0] == "lab"
    assert await replan_tonight(ctx, at(MON, 23), 0) == "Nothing left on tonight's plan to move. 🦉"
