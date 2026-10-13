from datetime import timedelta
from types import SimpleNamespace

import pytest
from factories import assignment, utc

from pai import grades, prices
from pai.canvas import Course
from pai.config import Config
from pai.jobs import feeds, review

TZ = Config(timezone="America/Los_Angeles").tz
NOW = utc(2026, 10, 11, 19)  # Sunday 12:00 LA


@pytest.mark.parametrize(
    "text,pct",
    [("B", 80), ("b+", 87), ("A-", 90), ("85", 85), ("85%", 85), ("Z", None), ("0", None)],
)
def test_parse_target(text, pct):
    assert grades.parse_target(text) == pct


def _group(name, weight, *assignments):
    return {"name": name, "group_weight": weight, "assignments": list(assignments)}


def _a(points, score=None, lock_at=None, submitted=False):
    sub = {}
    if score is not None:
        sub = {"score": score, "workflow_state": "graded"}
    elif submitted:
        sub = {"submitted_at": "2026-10-01T00:00:00Z"}
    return {"points_possible": points, "submission": sub, "lock_at": lock_at}


def test_unweighted_need():
    rows = [_group("All", 0, _a(100, 70), _a(100))]
    g = grades.groups_from_canvas(rows, NOW)
    # (70 + x*100) / 200 = 0.8  ->  x = 90%
    assert "about 90%" in grades.needed(g, weighted=False, target=80)
    assert "locked in" in grades.needed(g, weighted=False, target=30)
    assert "isn't reachable" in grades.needed(g, weighted=False, target=95)


def test_weighted_need_and_locked_zeros():
    rows = [
        _group("Quizzes", 40, _a(10, 10), _a(10, lock_at="2026-10-01T00:00:00Z")),  # zero
        _group("Projects", 60, _a(100)),
    ]
    g = grades.groups_from_canvas(rows, NOW)
    assert g[0].graded == 20 and g[0].earned == 10 and g[1].remaining == 100
    a, b = grades.projection(g, weighted=True)
    assert a == pytest.approx(20) and b == pytest.approx(60)
    # 20 + 60x = 80 -> x = 100%
    assert "about 100%" in grades.needed(g, weighted=True, target=80)


def test_feeds_baseline_then_new_only(conn):
    first = [{"id": 1}, {"id": 2}]
    assert feeds.unseen(conn, "k", first) == []
    assert feeds.unseen(conn, "k", [*first, {"id": 3}]) == [{"id": 3}]
    assert feeds.unseen(conn, "k", [{"id": 3}]) == []


def test_feed_texts():
    a = {"course": "ENGL", "author": "Prof", "title": "Essay tips",
         "message": "<p>Read <b>this</b>.</p>", "url": "https://x"}  # fmt: skip
    assert feeds.announcement_text(a).startswith("📣 *ENGL* from Prof: Essay tips\nRead this.")
    assert len(feeds.preview("word " * 500)) <= 350


def test_quiet_hours_wrap_midnight(monkeypatch):
    from pai import jobs as jobs_mod

    cfg = Config(timezone="America/Los_Angeles")
    j = jobs_mod.Jobs(SimpleNamespace(agent=SimpleNamespace(config=cfg, conn=None)))
    for hhmm, quiet in [
        ("23:30", True),
        ("02:00", True),
        ("06:59", True),
        ("07:00", False),
        ("12:00", False),
    ]:
        h, m = map(int, hhmm.split(":"))

        class FakeDT(jobs_mod.datetime):
            @classmethod
            def now(cls, tz=None, h=h, m=m):
                return jobs_mod.datetime(2026, 10, 11, h, m, tzinfo=tz)

        monkeypatch.setattr(jobs_mod, "datetime", FakeDT)
        assert j.quiet_now() is quiet, hhmm


PRODUCT_LD = """<html><head><title>Ignored</title>
<script type="application/ld+json">{"@context":"https://schema.org","@type":"Product",
"name":"Sony WH-1000XM6","offers":[{"@type":"Offer","price":"399.99","priceCurrency":"USD"},
{"@type":"Offer","price":"379.00","priceCurrency":"USD"}]}</script></head></html>"""
PRODUCT_META = """<html><head><title>Desk Lamp | Shop</title>
<meta property="product:price:amount" content="1,249.50">
<meta property="product:price:currency" content="USD"></head></html>"""


def test_price_extract():
    p = prices.extract(PRODUCT_LD)
    assert (p.amount, p.currency, p.title) == (379.0, "USD", "Sony WH-1000XM6")
    p = prices.extract(PRODUCT_META)
    assert p.amount == 1249.5 and p.title == "Desk Lamp | Shop"
    assert prices.extract("<html>no price</html>") is None


def test_price_alerts(conn):
    wid = prices.add(conn, "https://shop/x", prices.Price(100, "USD", "Lamp"), None)
    row = lambda: conn.execute("SELECT * FROM price_watches WHERE id = ?", (wid,)).fetchone()  # noqa: E731
    assert prices.check(conn, row(), prices.Price(100, "USD", "Lamp")) is None
    assert "100.00 to $80.00" in prices.check(conn, row(), prices.Price(80, "USD", "Lamp"))
    tid = prices.add(conn, "https://shop/y", prices.Price(300, "USD", "Phone"), 250)
    trow = lambda: conn.execute("SELECT * FROM price_watches WHERE id = ?", (tid,)).fetchone()  # noqa: E731
    assert prices.check(conn, trow(), prices.Price(260, "USD", "")) is None
    assert "under your $250.00 target" in prices.check(conn, trow(), prices.Price(249, "USD", ""))
    assert prices.check(conn, trow(), prices.Price(240, "USD", "")) is None  # already below
    assert prices.remove(conn, tid) and [r["id"] for r in prices.active(conn)] == [wid]


def test_weekly_review():
    week = utc(2026, 10, 6, 19)  # Tuesday
    items = [
        assignment(1, due=week, submitted=True, score=9, graded_at=week + timedelta(days=1)),
        assignment(2, due=NOW + timedelta(days=3), points=50),
        assignment(3, due=NOW + timedelta(days=4), points=10),
    ]
    courses = [Course(1, "CS", "CS", 95.0, "A", end_at=NOW + timedelta(days=78))]
    text = review.build(NOW, TZ, courses, items, None, 125, None)
    assert "Turned in 1 assignment" in text and "Lab 1: 9/10" in text
    assert "2h 5m logged" in text and "• CS: A (95%)" in text
    assert "2 item(s) due. Biggest: CS/IT 135 Lab 2 (50 pts)" in text
    assert "11 weeks (78 days) left" in text
