from datetime import UTC, datetime, timedelta

import httpx
import pytest

from pai.canvas import CanvasClient, Course, short_course
from pai.tools import default_tools
from pai.tools.notes import save_note, search_notes
from pai.tools.reminders import add_reminder, cancel_reminder, list_reminders, parse_when


def test_tool_names_unique_and_schemas_closed():
    tools = default_tools()
    names = [t.name for t in tools]
    assert len(names) == len(set(names))
    for t in tools:
        assert t.input_schema["type"] == "object"
        assert t.input_schema.get("additionalProperties") is False
    assert [t.definition() for t in tools] == [t.definition() for t in default_tools()]


async def test_reminders_roundtrip(ctx):
    scheduled, removed = [], []
    ctx.schedule_reminder = lambda rid, when: scheduled.append((rid, when))
    ctx.unschedule_reminder = removed.append
    when = (datetime.now(ctx.config.tz) + timedelta(days=1)).replace(microsecond=0, tzinfo=None)
    msg = await add_reminder({"text": "call the bank", "when": when.isoformat()}, ctx)
    assert msg.startswith("Reminder #1")
    assert scheduled[0][1].tzinfo is not None
    listed = await list_reminders({}, ctx)
    assert listed["pending"][0]["text"] == "call the bank"
    await cancel_reminder({"id": 1}, ctx)
    assert removed == [1] and (await list_reminders({}, ctx))["pending"] == []
    with pytest.raises(LookupError):
        await cancel_reminder({"id": 1}, ctx)


async def test_reminder_in_past_rejected(ctx):
    with pytest.raises(ValueError, match="past"):
        await add_reminder({"text": "x", "when": "2020-01-01T09:00"}, ctx)


def test_parse_when_localizes_naive(config):
    assert parse_when("2026-10-16T18:00", config.tz).utcoffset() == timedelta(hours=-7)
    assert parse_when("2026-10-16T18:00+00:00", config.tz).utcoffset() == timedelta(0)


async def test_notes(ctx):
    await save_note({"text": "idea:  owl\nstickers", "tags": ["merch"]}, ctx)
    await save_note({"text": "buy milk"}, ctx)
    found = await search_notes({"query": "owl"}, ctx)
    assert found["total_matches"] == 1 and "owl stickers #merch" in found["notes"][0]
    assert len((await search_notes({}, ctx))["notes"]) == 2


def test_short_course():
    assert short_course("CS/IT 101 - 1114") == "CS/IT 101"


async def test_canvas_follows_pagination():
    pages = {
        "1": ([{"id": 1, "name": "A", "published": True}],
              '<https://c.test/next?page=2>; rel="next"'),
        "2": ([{"id": 2, "name": "B", "published": True, "submission": {"workflow_state":
               "graded", "score": 5, "graded_at": "2026-10-01T00:00:00Z",
               "submitted_at": "2026-09-30T00:00:00Z"}}], ""),
    }  # fmt: skip

    def handler(request):
        assert request.headers["authorization"] == "Bearer tok"
        page = request.url.params.get("page", "1")
        body, link = pages[page]
        return httpx.Response(200, json=body, headers={"link": link})

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    c = CanvasClient("https://c.test", "tok", http)
    items = await c.assignments(Course(9, "CS", "CS", None, None))
    assert [a.id for a in items] == [1, 2]
    assert items[1].submitted and items[1].score == 5
    assert items[1].graded_at == datetime(2026, 10, 1, tzinfo=UTC)


def test_upcoming_without_overdue_and_missing():
    from factories import assignment, utc

    from pai.canvas import missing, upcoming

    now = utc(2026, 10, 12, 12)
    late = assignment(1, due=now - timedelta(days=3))
    soon = assignment(2, due=now + timedelta(days=2))
    later = assignment(3, due=now + timedelta(days=10))
    done = assignment(4, due=now - timedelta(days=1), submitted=True)
    items = [late, soon, later, done]
    assert [a.id for a in upcoming(items, now, 7)] == [1, 2]
    assert [a.id for a in upcoming(items, now, 7, include_overdue=False)] == [2]
    assert [a.id for a in missing(items, now)] == [1]


async def test_canvas_snapshot_is_cached(monkeypatch):
    calls = []

    def handler(request):
        calls.append(request.url.path)
        if request.url.path.endswith("/courses"):
            return httpx.Response(
                200, json=[{"id": 1, "course_code": "CS 1 - 9", "enrollments": []}]
            )
        return httpx.Response(200, json=[{"id": 5, "name": "Lab", "published": True}])

    c = CanvasClient(
        "https://c.test", "tok", httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )
    assert [a.id for a in await c.all_assignments()] == [5]
    await c.all_assignments()
    courses, _ = await c.snapshot()
    assert len(calls) == 2 and courses[0].code == "CS 1"
    await c.all_assignments(fresh=True)
    assert len(calls) == 4
    monkeypatch.setattr(CanvasClient, "CACHE_SECONDS", 0)
    await c.all_assignments()
    assert len(calls) == 6


def test_locked_work_is_not_missing_or_planned():
    from factories import assignment, utc

    from pai.canvas import is_overdue, locked_missed, missing, upcoming

    now = utc(2026, 10, 12, 12)
    due = now - timedelta(days=2)
    open_late = assignment(1, due=due)  # late, still accepts submissions
    locked = assignment(2, due=due, lock_at=due, locked_for_user=True)
    locked_later = assignment(3, due=due, lock_at=now + timedelta(days=1))  # lock not hit yet
    items = [open_late, locked, locked_later]
    assert [a.id for a in missing(items, now)] == [1, 3]
    assert [a.id for a in locked_missed(items, now)] == [2]
    assert not is_overdue(locked, now)
    assert 2 not in [a.id for a in upcoming(items, now, 7)]  # never reaches the planner


def test_canvas_json_lock_fields():
    from pai.canvas import Course, assignment_from_json

    a = assignment_from_json(
        {"id": 1, "name": "Q", "due_at": "2026-09-05T06:59:59Z",
         "lock_at": "2026-09-05T06:59:59Z", "locked_for_user": True},
        Course(1, "CS", "CS", None, None),
    )  # fmt: skip
    assert a.locked_for_user and a.lock_at == datetime(2026, 9, 5, 6, 59, 59, tzinfo=UTC)
    assert a.closed(datetime(2026, 10, 1, tzinfo=UTC))
