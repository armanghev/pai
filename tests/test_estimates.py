from datetime import datetime, time, timedelta

from factories import assignment
from test_planner import DUE, MON, NOW, FakeCanvas, ParseClient, blk, plan

from pai import estimates, focus
from pai.agent import Agent
from pai.config import Config
from pai.jobs import review
from pai.planner import Block, make_plan

TZ = Config(timezone="America/Los_Angeles").tz


def test_kind_rules():
    k = estimates.kind
    assert k("ENG 101", "Essay 1 Final Draft") == "essay"
    assert k("ENG 101", "Essay 2 Video Peer Review Discussion") == "discussion"
    assert k("CS/IT 101", "Quiz - System Software") == "quiz"
    assert k("CS/IT 135", "Assignment 4: Number Guessing Game") == "lab"
    assert k("COMM 101", "Read chapter 3") == "reading"
    assert k("COMM 101", "DO: Course Assg #1 - Your Multiple Identities") == "other"


def _plan_with(aid, day, start, end, course="CS/IT 135", task="problems 1-5"):
    return plan(
        Block(day=day, start=start, end=end, course=course, task=task, assignment_id=aid),
        Block(
            day=day + timedelta(days=1),
            start=time(18),
            end=time(19),
            course="ENG 101",
            task="outline the essay",
            assignment_id=9,
        ),
    )


def test_tie_to_running_block_or_label():
    p = _plan_with(4, MON, time(18), time(19, 30))
    at = datetime.combine(MON, time(17, 45), TZ)  # 15 min before the block
    assert estimates.tie(p, "", at, TZ) == 4
    assert estimates.tie(p, "CS/IT 135", at, TZ) == 4
    assert estimates.tie(p, "essay outline", at, TZ) == 9  # label beats the live block
    assert estimates.tie(p, "laundry", datetime.combine(MON, time(12), TZ), TZ) is None
    assert estimates.tie(None, "x", at, TZ) is None


def _sample(conn, aid, est_min, actual_min, done=True, title="Essay 1"):
    p = plan(blk(time(18), time(18) if est_min == 0 else time(18, est_min), aid=aid))
    conn.execute(
        "INSERT INTO study_plans (week_start, plan_json) VALUES (?, ?)",
        ("2026-10-12", p.model_dump_json()),
    )
    sid = focus.start(conn, 120, "x", NOW, aid)
    conn.execute(
        "UPDATE focus_sessions SET minutes = ?, status = 'done' WHERE id = ?", (actual_min, sid)
    )
    return assignment(aid, title=title, submitted=done, due=NOW + timedelta(days=3))


def test_collect_ratios_and_notes(conn):
    items = [
        _sample(conn, 1, 30, 48),
        _sample(conn, 2, 40, 60),
        _sample(conn, 3, 50, 90),
        _sample(conn, 4, 30, 30, done=False),  # not finished yet: no sample
    ]
    assert estimates.collect(conn, items, NOW) == 3
    assert estimates.collect(conn, items, NOW) == 0  # once each
    assert estimates.planned_minutes(conn, 1) == 30
    (ratio, n) = estimates.ratios(conn)["essay"]
    assert n == 3 and round(ratio, 2) == 1.6  # median of 1.6, 1.5, 1.8
    assert estimates.notes({"essay": (1.6, 3)}, {"essay"}) == [
        "essays: you take ~1.6x your estimates, plan adjusted"
    ]
    assert estimates.notes({"essay": (1.05, 3)}, {"essay"}) == []  # close enough
    assert estimates.notes({"essay": (1.6, 3)}, {"lab"}) == []  # not in this plan


def test_ratios_need_three_samples_and_are_clamped(conn):
    conn.executemany(
        "INSERT INTO estimate_samples (assignment_id, kind, estimated, actual) VALUES (?,?,?,?)",
        [
            (1, "lab", 10, 100),
            (2, "lab", 10, 90),
            (3, "quiz", 60, 10),
            (4, "quiz", 60, 10),
            (5, "quiz", 60, 10),
            (6, "reading", 30, 30),
        ],
    )
    assert estimates.ratios(conn) == {"quiz": (0.5, 3)}  # lab has 2, reading 1


async def test_plan_uses_learned_pace(conn, ctx, tmp_path):
    conn.executemany(
        "INSERT INTO estimate_samples (assignment_id, kind, estimated, actual) VALUES (?,?,?,?)",
        [(i, "lab", 60, 96) for i in range(10, 13)],
    )
    config = Config.model_validate(
        {
            "timezone": "America/Los_Angeles",
            "db_path": str(tmp_path / "x.db"),
            "schedule": {"study": [{"days": ["mon"], "start": "17:30", "end": "22:00"}]},
        }
    )
    client = ParseClient(plan(blk(time(18), time(19))))
    ctx.config = config
    ctx.agent = Agent(client, conn, config, [], system_prompt="x")
    ctx.canvas = FakeCanvas([assignment(1, due=DUE + timedelta(days=1))])
    text = await make_plan(ctx, MON, MON, NOW)
    request = client.requests[0]["messages"][0]["content"]
    assert "| lab" in request and "- lab: 1.6x (3 samples)" in request
    assert "labs and coding: you take ~1.6x your estimates, plan adjusted" in text


def test_review_shows_pace():
    text = review.build(NOW, TZ, [], [], None, 0, None, ["• essays: 1.6x planned (3 samples)"])
    assert "⏱ *Your pace vs. plans*\n• essays: 1.6x planned (3 samples)" in text


def test_focus_start_records_assignment(conn):
    sid = focus.start(conn, 25, "lab", NOW, 7)
    row = conn.execute("SELECT assignment_id FROM focus_sessions WHERE id = ?", (sid,)).fetchone()
    assert row[0] == 7
