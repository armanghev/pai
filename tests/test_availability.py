from datetime import date, time

from pai.availability import add_override, free_blocks, load_overrides, subtract
from pai.config import Config

MON = date(2026, 10, 12)
SAT = date(2026, 10, 17)


def cfg(**schedule):
    base = {
        "work": [{"days": ["mon", "tue", "wed", "thu", "fri"], "start": "08:00", "end": "16:30"}],
        "classes": [{"name": "CS 135", "days": ["mon"], "start": "18:00", "end": "19:15"}],
        "study": [
            {"days": ["mon", "tue", "wed", "thu", "fri"], "start": "17:30", "end": "22:00"},
            {"days": ["sat", "sun"], "start": "10:00", "end": "18:00"},
        ],
    }
    base.update(schedule)
    return Config.model_validate({"schedule": base})


NO_OVERRIDES = {"off_work": [], "busy": []}


def test_subtract():
    assert subtract([(0, 100)], [(10, 20), (50, 60)]) == [(0, 10), (20, 50), (60, 100)]
    assert subtract([(0, 100)], [(0, 100)]) == []
    assert subtract([(0, 100)], [(200, 300)]) == [(0, 100)]


def test_class_carves_out_of_evening():
    blocks = free_blocks(cfg(), MON, MON, NO_OVERRIDES)
    assert [(b.start, b.end) for b in blocks] == [
        (time(17, 30), time(18, 0)),  # exactly the 30-minute minimum
        (time(19, 15), time(22, 0)),
    ]


def test_weekend_uses_weekend_window():
    blocks = free_blocks(cfg(), SAT, SAT, NO_OVERRIDES)
    assert [(b.start, b.end) for b in blocks] == [(time(10), time(18))]


def test_off_work_day_gets_daytime_window(conn):
    add_override(conn, MON, off_work=True)
    blocks = free_blocks(cfg(classes=[]), MON, MON, load_overrides(conn, MON, MON))
    assert [(b.start, b.end) for b in blocks] == [(time(10), time(18))]


def test_busy_override(conn):
    add_override(conn, SAT, busy=("12:00", "15:00"), why="dentist")
    blocks = free_blocks(cfg(), SAT, SAT, load_overrides(conn, SAT, SAT))
    assert [(b.start, b.end) for b in blocks] == [(time(10), time(12)), (time(15), time(18))]


def test_now_clips_today_and_skips_past_days():
    from datetime import datetime

    c = cfg()
    now = datetime(2026, 10, 17, 14, 0, tzinfo=c.tz)  # Saturday 14:00 local
    blocks = free_blocks(c, MON, SAT, NO_OVERRIDES, now)
    assert all(b.day == SAT for b in blocks)
    assert blocks[0].start == time(14, 1)


def test_schedule_changes_by_chat_override_config(conn):
    import pytest

    from pai.availability import effective_schedule, set_schedule

    c = cfg(classes=[])
    assert free_blocks(c, MON, MON, NO_OVERRIDES)[0].start == time(17, 30)
    set_schedule(
        conn, "classes", [{"name": "CS", "days": ["mon"], "start": "17:30", "end": "19:00"}]
    )
    sched = effective_schedule(conn, c)
    assert sched.classes[0].name == "CS" and sched.work == c.schedule.work
    blocks = free_blocks(c, MON, MON, NO_OVERRIDES, schedule=sched)
    assert [(b.start, b.end) for b in blocks] == [(time(19), time(22))]
    set_schedule(conn, "classes", [])
    assert effective_schedule(conn, c).classes == []
    with pytest.raises(ValueError):
        set_schedule(conn, "classes", [{"days": ["mon"], "start": "9", "end": "10:00"}])
    with pytest.raises(ValueError):
        set_schedule(conn, "naps", [])
