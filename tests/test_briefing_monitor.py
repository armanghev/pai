from datetime import date, time, timedelta

from factories import assignment, utc

from pai.config import Config
from pai.jobs import briefing, monitor
from pai.planner import Block, StudyPlan

TZ = Config(timezone="America/Los_Angeles").tz
NOW = utc(2026, 10, 12, 13, 45)  # Mon 06:45 LA


def test_briefing_sections():
    items = [
        assignment(1, due=NOW + timedelta(hours=10)),  # today
        assignment(2, due=NOW + timedelta(days=2)),  # soon
        assignment(3, due=NOW - timedelta(days=1)),  # overdue
        assignment(4, due=NOW + timedelta(days=10)),  # later: not shown
    ]
    plan = StudyPlan(
        blocks=[Block(day=date(2026, 10, 12), start=time(18), end=time(19), course="CS",
                      task="Lab 1", assignment_id=1)],
        notes=[], at_risk=[],
    )  # fmt: skip
    text = briefing.build(
        NOW, TZ, "Sunny, 60°F–75°F", items, plan, [(NOW + timedelta(hours=3), "call bank")]
    )
    assert "Monday, Oct 12" in text and "Sunny" in text
    assert "*Overdue*" in text and "Lab 3" in text
    assert "*Due today*" in text and "*Next 3 days*" in text and "Lab 4" not in text
    assert "18:00–19:00" in text and "call bank" in text


def test_briefing_without_canvas():
    assert "Couldn't reach Canvas" in briefing.build(NOW, TZ, None, None, None, [])


def test_site_needs_two_failures_then_recovers(conn):
    down = {"up": False, "status": 503}
    assert monitor.site_transition(conn, "https://x.test", down) is None
    assert "Down" in monitor.site_transition(conn, "https://x.test", down)
    assert monitor.site_transition(conn, "https://x.test", down) is None
    up = {"up": True, "status": 200, "ms": 80}
    assert "Back up" in monitor.site_transition(conn, "https://x.test", up)
    assert monitor.site_transition(conn, "https://x.test", up) is None


def test_health_alerts_once_until_cleared(conn):
    hot = {"cpu_temp_c": 80.0, "disk_used_pct": 50}
    assert len(monitor.health_alerts(conn, hot, 75, 85)) == 1
    assert monitor.health_alerts(conn, hot, 75, 85) == []
    cool = {"cpu_temp_c": 50.0, "disk_used_pct": 50}
    assert monitor.health_alerts(conn, cool, 75, 85) == []
    assert len(monitor.health_alerts(conn, hot, 75, 85)) == 1
