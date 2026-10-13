from datetime import datetime, time, timedelta

from factories import assignment

from pai.config import Config
from pai.jobs import briefing, grade_watch
from pai.planner import Block, StudyPlan

CFG = Config(timezone="America/Los_Angeles")
TZ = CFG.tz
NOW = datetime(2026, 10, 12, 16, 30, tzinfo=TZ)  # Monday after work
TODAY = NOW.date()


def test_shift_over_recap():
    items = [
        assignment(1, due=NOW.replace(hour=23, minute=59)),
        assignment(2, due=NOW.replace(hour=12)),  # earlier today: not "tonight"
        assignment(3, due=NOW + timedelta(days=1)),
        assignment(4, due=NOW.replace(hour=20), submitted=True),
    ]
    plan = StudyPlan(
        blocks=[
            Block(
                day=TODAY, start=time(10), end=time(11), course="X", task="old", assignment_id=None
            ),
            Block(
                day=TODAY,
                start=time(19),
                end=time(20),
                course="CS/IT 135",
                task="lab",
                assignment_id=1,
            ),
        ],
        notes=[],
        at_risk=[],
    )
    reminders = [(NOW + timedelta(hours=2), "call mom"), (NOW - timedelta(hours=1), "past")]
    text = briefing.shift_over(NOW, TZ, items, plan, reminders, ["📅 Lab 3: due moved"])
    assert text.startswith("*Shift's over* 🦉 Monday, Oct 12")
    assert "*Due tonight*\n• 23:59 CS/IT 135: Lab 1" in text
    assert "Lab 2" not in text and "Lab 3:" not in text.split("*Canvas")[0] and "Lab 4" not in text
    assert "*Canvas changes since this morning*\n• 📅 Lab 3: due moved" in text
    assert "*Tonight's plan*\n• 19:00–20:00 CS/IT 135: lab" in text and "old" not in text
    assert "*Reminders tonight*\n• 18:30 call mom" in text and "past" not in text
    first_line = text.splitlines()[0]
    assert "$" not in first_line


def test_quiet_day():
    text = briefing.shift_over(NOW, TZ, [], None, [], [])
    assert "Nothing due tonight. ✅" in text
    assert "Couldn't reach Canvas" in briefing.shift_over(NOW, TZ, None, None, [], [])


def test_changes_log_since_morning(conn):
    morning = NOW.replace(hour=6, minute=45)
    grade_watch.diff(conn, [], morning - timedelta(hours=1), TZ)
    grade_watch.diff(
        conn, [assignment(5, due=NOW + timedelta(days=2))], morning - timedelta(minutes=10), TZ
    )
    grade_watch.diff(
        conn,
        [assignment(5, due=NOW + timedelta(days=2)), assignment(6, due=NOW + timedelta(days=3))],
        NOW,
        TZ,
    )
    assert [c.split(": ")[1].split(" (")[0] for c in grade_watch.changes_since(conn, morning)] == [
        "*CS/IT 135* Lab 6"
    ]


def test_shift_end_comes_from_the_work_schedule():
    cfg = Config.model_validate(
        {
            "schedule": {
                "work": [
                    {"days": ["mon", "tue", "wed", "thu", "fri"], "start": "08:00", "end": "16:30"},
                    {"days": ["sat"], "start": "10:00", "end": "14:00"},
                ]
            }
        }
    )
    assert cfg.shift_end("mon") == "16:30" and cfg.shift_end("sat") == "14:00"
    assert cfg.shift_end("sun") is None  # no work: no recap
    cfg.jobs.shift_time = "17:00"
    assert cfg.shift_end("mon") == "17:00" and cfg.shift_end("sun") is None
