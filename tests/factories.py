from datetime import UTC, datetime

from pai.canvas import Assignment


def assignment(id=1, due=None, points=10.0, submitted=False, score=None, graded_at=None, **kw):
    base = dict(
        id=id,
        course_id=100,
        course="CS/IT 135",
        title=f"Lab {id}",
        due_at=due,
        points=points,
        html_url=f"https://canvas.test/a/{id}",
        submitted=submitted,
        missing=False,
        late=False,
        excused=False,
        score=score,
        grade=None if score is None else str(score),
        graded_at=graded_at,
    )
    base.update(kw)
    return Assignment(**base)


def utc(*args):
    return datetime(*args, tzinfo=UTC)
