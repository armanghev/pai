"""Read-only Canvas client: courses, assignments with submissions, current grades."""

from __future__ import annotations

import asyncio
import re
import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

_NEXT = re.compile(r'<([^>]+)>;\s*rel="next"')


def parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def short_course(name: str) -> str:
    """'CS/IT 101 - 1114' -> 'CS/IT 101'."""
    return name.split(" - ")[0].strip()


@dataclass
class Course:
    id: int
    code: str
    name: str
    score: float | None
    grade: str | None
    weighted: bool = False  # grade uses assignment group weights
    end_at: datetime | None = None  # course or term end


@dataclass
class Assignment:
    id: int
    course_id: int
    course: str
    title: str
    due_at: datetime | None
    points: float | None
    html_url: str
    submitted: bool
    missing: bool
    late: bool
    excused: bool
    score: float | None
    grade: str | None
    graded_at: datetime | None
    lock_at: datetime | None = None  # after this Canvas stops accepting submissions
    locked_for_user: bool = False

    def closed(self, now: datetime) -> bool:
        """Past due and locked: it can't be turned in anymore."""
        if self.lock_at is not None and self.lock_at <= now:
            return True
        return self.locked_for_user and self.due_at is not None and self.due_at < now

    @property
    def needs_submission(self) -> bool:
        return not (self.submitted or self.excused)

    def as_dict(self) -> dict[str, Any]:
        d = asdict(self)
        for k in ("due_at", "graded_at"):
            d[k] = d[k].isoformat() if d[k] else None
        return d


def assignment_from_json(a: dict[str, Any], course: Course) -> Assignment:
    sub = a.get("submission") or {}
    state = sub.get("workflow_state")
    return Assignment(
        id=a["id"],
        course_id=course.id,
        course=course.code,
        title=" ".join((a.get("name") or "").split()) or "(untitled)",
        due_at=parse_ts(a.get("due_at")),
        points=a.get("points_possible"),
        html_url=a.get("html_url") or "",
        submitted=bool(sub.get("submitted_at"))
        or state in ("submitted", "graded", "pending_review"),
        missing=bool(sub.get("missing")),
        late=bool(sub.get("late")),
        excused=bool(sub.get("excused")),
        score=sub.get("score"),
        grade=sub.get("grade"),
        graded_at=parse_ts(sub.get("graded_at")) if state == "graded" else None,
        lock_at=parse_ts(a.get("lock_at")),
        locked_for_user=bool(a.get("locked_for_user")),
    )


class CanvasClient:
    """Canvas reads are cached for CACHE_SECONDS so quick /due and /missing checks don't
    each cost a dozen API calls. Grade watch and the planner pass fresh=True."""

    CACHE_SECONDS = 600

    def __init__(self, base_url: str, token: str, http: httpx.AsyncClient | None = None):
        self.base = base_url.rstrip("/")
        self.http = http or httpx.AsyncClient(timeout=30)
        self.headers = {"Authorization": f"Bearer {token}"}
        self._cache: tuple[float, list[Course], list[Assignment]] | None = None
        self._lock = asyncio.Lock()

    def _cached(self) -> tuple[list[Course], list[Assignment]] | None:
        if self._cache and time.monotonic() - self._cache[0] < self.CACHE_SECONDS:
            return self._cache[1], self._cache[2]
        return None

    async def snapshot(self, fresh: bool = False) -> tuple[list[Course], list[Assignment]]:
        """Courses and all their assignments, from cache unless stale or `fresh`."""
        async with self._lock:  # concurrent callers share one fetch
            hit = None if fresh else self._cached()
            if hit:
                return hit
            courses = await self.courses()
            per_course = await asyncio.gather(*(self.assignments(c) for c in courses))
            items = [a for group in per_course for a in group]
            self._cache = (time.monotonic(), courses, items)
            return courses, items

    async def _get_all(self, path: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        url: str | None = f"{self.base}{path}"
        out: list[dict[str, Any]] = []
        query: dict[str, Any] | None = {"per_page": 100, **params}
        while url:
            r = await self.http.get(url, params=query, headers=self.headers)
            r.raise_for_status()
            out.extend(r.json())
            m = _NEXT.search(r.headers.get("link", ""))
            url, query = (m.group(1), None) if m else (None, None)  # next URL carries the query
        return out

    async def courses(self) -> list[Course]:
        rows = await self._get_all(
            "/api/v1/courses",
            {"enrollment_state": "active", "include[]": ["total_scores", "term"]},
        )
        courses = []
        for c in rows:
            if "id" not in c or c.get("access_restricted_by_date"):
                continue
            enr = next((e for e in c.get("enrollments", []) if e.get("type") == "student"), {})
            courses.append(
                Course(
                    id=c["id"],
                    code=short_course(c.get("course_code") or c.get("name", "")),
                    name=c.get("name", ""),
                    score=enr.get("computed_current_score"),
                    grade=enr.get("computed_current_grade"),
                    weighted=bool(c.get("apply_assignment_group_weights")),
                    end_at=parse_ts(c.get("end_at") or (c.get("term") or {}).get("end_at")),
                )
            )
        return courses

    async def announcements(self, courses: list[Course], since: datetime) -> list[dict]:
        if not courses:
            return []
        rows = await self._get_all(
            "/api/v1/announcements",
            {
                "context_codes[]": [f"course_{c.id}" for c in courses],
                "start_date": since.date().isoformat(),
                "end_date": (datetime.now(UTC) + timedelta(days=1)).date().isoformat(),
            },
        )
        code = {f"course_{c.id}": c.code for c in courses}
        return [
            {
                "id": a["id"],
                "course": code.get(a.get("context_code"), ""),
                "title": a.get("title") or "(untitled)",
                "message": a.get("message") or "",
                "author": (a.get("author") or {}).get("display_name") or "",
                "posted_at": parse_ts(a.get("posted_at")),
                "url": a.get("html_url") or "",
            }
            for a in rows
        ]

    async def unread_messages(self) -> list[dict]:
        r = await self.http.get(
            f"{self.base}/api/v1/conversations",
            params={"scope": "unread", "per_page": 20},
            headers=self.headers,
        )
        r.raise_for_status()
        return [
            {
                "id": m["id"],
                "subject": m.get("subject") or "(no subject)",
                "preview": m.get("last_message") or "",
                "from": ", ".join(p.get("name", "") for p in (m.get("participants") or [])[:2]),
                "at": parse_ts(m.get("last_message_at")),
                "url": f"{self.base}/conversations#filter=type=inbox",
            }
            for m in r.json()
        ]

    async def assignment_groups(self, course: Course) -> list[dict]:
        return await self._get_all(
            f"/api/v1/courses/{course.id}/assignment_groups",
            {"include[]": ["assignments", "submission"]},
        )

    async def assignments(self, course: Course) -> list[Assignment]:
        rows = await self._get_all(
            f"/api/v1/courses/{course.id}/assignments",
            {"include[]": "submission", "order_by": "due_at"},
        )
        return [assignment_from_json(a, course) for a in rows if a.get("published", True)]

    async def all_assignments(self, fresh: bool = False) -> list[Assignment]:
        return (await self.snapshot(fresh))[1]


def is_overdue(a: Assignment, now: datetime, lookback_days: int = 14) -> bool:
    """Past due, not turned in, still submittable, and recent enough to still matter."""
    if a.due_at is None or a.due_at >= now or not a.needs_submission:
        return False
    if a.closed(now):  # locked: shown as locked, never planned
        return False
    if a.score is not None:  # graded anyway (e.g. a zero was entered)
        return False
    return a.missing or (bool(a.points) and a.due_at >= now - timedelta(days=lookback_days))


def upcoming(
    items: list[Assignment],
    now: datetime,
    days: int,
    include_submitted: bool = False,
    include_overdue: bool = True,
) -> list[Assignment]:
    """Due between now and now+days (plus, by default, anything overdue and unsubmitted)."""
    end = now + timedelta(days=days)
    out = []
    for a in items:
        if a.due_at is None:
            continue
        if (now <= a.due_at <= end or (include_overdue and is_overdue(a, now))) and (
            include_submitted or a.needs_submission
        ):
            out.append(a)
    return sorted(out, key=lambda a: a.due_at)


def missing(items: list[Assignment], now: datetime) -> list[Assignment]:
    """Past due, not turned in and still open (Canvas's missing flag, or recent work)."""
    return sorted((a for a in items if is_overdue(a, now)), key=lambda a: a.due_at)


def locked_missed(
    items: list[Assignment], now: datetime, lookback_days: int = 60
) -> list[Assignment]:
    """Not turned in and now locked, so it can't be submitted. Recent ones only."""
    since = now - timedelta(days=lookback_days)
    return sorted(
        (
            a
            for a in items
            if a.due_at
            and since <= a.due_at < now
            and a.needs_submission
            and a.score is None
            and a.closed(now)
            and (a.missing or a.points)
        ),
        key=lambda a: a.due_at,
    )
