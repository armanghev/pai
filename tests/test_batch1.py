from datetime import date, time, timedelta

import pytest
from factories import assignment, utc

from pai import focus, habits, hn
from pai.config import Config
from pai.jobs import briefing, recap
from pai.planner import Block, StudyPlan, build_request

TZ = Config(timezone="America/Los_Angeles").tz
TODAY = date(2026, 10, 12)


def test_habit_streaks_and_report(conn):
    habits.add(conn, "  Gym ")
    hid = habits.active(conn)[0]["id"]
    for i, done in enumerate([True, True, False, True, True, True]):
        habits.record(conn, hid, TODAY - timedelta(days=5 - i), done)
    assert habits.streak(conn, hid, TODAY) == 3
    # Today unanswered: the streak through yesterday still counts.
    assert habits.streak(conn, hid, TODAY + timedelta(days=1)) == 3
    report = habits.week_report(conn, TODAY)
    assert "*Gym*: 5/7" in report and "3-day streak" in report
    habits.record(conn, hid, TODAY, False)  # changing an answer overwrites it
    assert habits.streak(conn, hid, TODAY) == 0


def test_habit_add_is_case_insensitive_and_remove(conn):
    habits.add(conn, "Read")
    habits.add(conn, "read")
    assert len(habits.active(conn)) == 1
    assert habits.remove(conn, "READ")
    assert habits.active(conn) == []
    habits.add(conn, "Read")  # comes back
    assert len(habits.active(conn)) == 1


def test_focus_session_lifecycle(conn):
    now = utc(2026, 10, 12, 3, 0)  # Sunday 20:00 LA
    sid = focus.start(conn, 25, "CS/IT 135", now)
    with pytest.raises(RuntimeError):
        focus.start(conn, 25, "again", now)
    assert focus.finish(conn, sid, now + timedelta(minutes=40)) == 25  # capped at plan
    assert focus.running(conn) is None
    sid = focus.start(conn, 50, "", now + timedelta(hours=1))
    assert focus.finish(conn, sid, now + timedelta(hours=1, minutes=12), stopped=True) == 12
    week = focus.minutes_between(conn, *focus.week_bounds(now, TZ))
    assert week == {"CS/IT 135": 25, "general": 12}
    assert "37m" in focus.week_summary(conn, now, TZ)
    with pytest.raises(ValueError):
        focus.start(conn, 1, "", now)


def test_hn_pick_prefers_topics():
    hits = [
        {"title": "Gardening tips", "points": 900},
        {"title": "Claude writes Python", "points": 50},
        {"title": "Mail servers", "points": 300},
        {"title": "Bait: claudeX", "points": 1000},
    ]
    picked = hn.pick(hits, ["claude", "python"], 2)
    assert [h["title"] for h in picked] == ["Claude writes Python", "Bait: claudeX"]


def test_briefing_includes_stories():
    now = utc(2026, 10, 12, 13, 45)
    stories = [{"title": "Big news", "url": "https://x.test", "points": 10}]
    text = briefing.build(now, TZ, None, [], None, [], stories)
    assert "*Hacker News*" in text and "[Big news](https://x.test)" in text


def _plan(*blocks):
    return StudyPlan(blocks=list(blocks), notes=[], at_risk=[])


def _blk(h1, h2):
    return Block(day=TODAY, start=time(h1), end=time(h2), course="CS", task="Lab", assignment_id=1)


def test_recap_flags_slipped_study_and_tomorrow():
    now = utc(2026, 10, 13, 4, 0)  # Mon 21:00 LA
    items = [
        assignment(1, due=utc(2026, 10, 13, 20, 0)),
        assignment(2, due=now - timedelta(days=1)),
    ]
    text = recap.build(now, TZ, items, _plan(_blk(18, 20)), 30, [])
    assert "Planned study today: 120 min. Logged with /focus: 30 min." in text
    assert "Some of it slipped" in text and "/replan" in text
    assert "*Due tomorrow*" in text and "Lab 1" in text
    assert "1 overdue" in text


def test_recap_quiet_day():
    now = utc(2026, 10, 13, 4, 0)
    text = recap.build(now, TZ, [], None, 0, [])
    assert "Nothing due tomorrow" in text and "/replan" not in text


def test_planner_request_includes_logged_time():
    now = utc(2026, 10, 12, 3, 0)
    req = build_request([], [], TZ, now, {"CS/IT 135": 50})
    assert "Study already done this week" in req and "CS/IT 135: 50 min" in req
