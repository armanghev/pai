from datetime import timedelta

from factories import assignment, utc

from pai.config import Config
from pai.jobs.grade_watch import diff

TZ = Config(timezone="America/Los_Angeles").tz
NOW = utc(2026, 10, 10, 20)


def test_first_run_is_silent_baseline(conn):
    items = [assignment(1, due=NOW + timedelta(days=2)), assignment(2, score=9, graded_at=NOW)]
    assert diff(conn, items, NOW, TZ) == []
    assert conn.execute("SELECT COUNT(*) FROM canvas_items").fetchone()[0] == 2


def test_grade_posted_then_changed_then_quiet(conn):
    diff(conn, [assignment(1, due=NOW - timedelta(days=1), submitted=True)], NOW, TZ)
    graded = assignment(1, due=NOW - timedelta(days=1), submitted=True, score=8, graded_at=NOW)
    alerts = diff(conn, [graded], NOW, TZ)
    assert len(alerts) == 1 and "Grade posted" in alerts[0] and "8/10" in alerts[0]
    regraded = assignment(1, due=NOW - timedelta(days=1), submitted=True, score=9, graded_at=NOW)
    assert "Grade changed" in diff(conn, [regraded], NOW, TZ)[0]
    assert diff(conn, [regraded], NOW, TZ) == []


def test_new_assignments_grouped_into_one_message(conn):
    diff(conn, [], NOW, TZ)
    alerts = diff(
        conn,
        [
            assignment(5, due=NOW + timedelta(days=3)),
            assignment(6, due=NOW + timedelta(days=20), points=50),
            assignment(7),  # no due date
            assignment(8, due=NOW - timedelta(days=3)),  # already past: not "new"
            assignment(9, due=NOW + timedelta(days=2), submitted=True),
        ],
        NOW,
        TZ,
    )
    overdue, text = alerts  # Lab 8 gets the usual past-due alert, not a "new" line
    assert "Past due" in overdue and "Lab 8" in overdue
    assert text.startswith("📋 *Canvas changes*\n")
    assert "🆕 New: *CS/IT 135* Lab 5 (due Tue 10/13 13:00, 10 pts)" in text
    assert "Lab 6 (due Fri 10/30 13:00, 50 pts)" in text
    assert "Lab 7 (no due date, 10 pts)" in text
    assert "Lab 8" not in text and "Lab 9" not in text


def test_due_date_moved_set_and_removed(conn):
    base = [
        assignment(1, due=NOW + timedelta(days=2)),
        assignment(2),
        assignment(3, due=NOW + timedelta(days=1)),
    ]
    diff(conn, base, NOW, TZ)
    moved = [
        assignment(1, due=NOW + timedelta(days=4)),
        assignment(2, due=NOW + timedelta(days=5)),
        assignment(3),
    ]
    (text,) = diff(conn, moved, NOW, TZ)
    assert "📅 *CS/IT 135* Lab 1: due Mon 10/12 13:00 → Wed 10/14 13:00" in text
    assert "Lab 2: due date set: Thu 10/15 13:00" in text
    assert "Lab 3: due date removed (was Sun 10/11 13:00)" in text
    assert diff(conn, moved, NOW, TZ) == []


def test_points_changed_and_removed(conn):
    other = assignment(20, course_id=200, course="COMM 101", due=NOW + timedelta(days=1))
    diff(conn, [assignment(1, due=NOW + timedelta(days=2)), assignment(2, due=NOW), other], NOW, TZ)
    # Lab 2 gone from CS/IT 135; COMM 101 missing from this fetch entirely: left alone.
    (text,) = diff(conn, [assignment(1, due=NOW + timedelta(days=2), points=20)], NOW, TZ)
    assert "🔢 *CS/IT 135* Lab 1: points 10 → 20" in text
    assert "🗑 Removed or unpublished: *CS/IT 135* Lab 2 (was due Sat 10/10 13:00)" in text
    assert "COMM" not in text
    ids = {r[0] for r in conn.execute("SELECT id FROM canvas_items")}
    assert ids == {"1", "20"}


def test_changes_to_stale_work_are_ignored(conn):
    old = NOW - timedelta(days=30)
    diff(conn, [assignment(1, due=old), assignment(2, due=old), assignment(3)], NOW, TZ)
    alerts = diff(
        conn,
        [assignment(1, due=old + timedelta(days=1), points=99), assignment(3)],
        NOW,
        TZ,
    )
    assert all("Canvas changes" not in a for a in alerts)


def test_overdue_fires_once(conn):
    diff(conn, [], NOW, TZ)
    late = assignment(1, due=NOW - timedelta(hours=3))
    assert "Past due" in diff(conn, [late], NOW, TZ)[0]
    assert diff(conn, [late], NOW, TZ) == []
