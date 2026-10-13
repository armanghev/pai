"""'What do I need on the rest of this class?' Pure math over Canvas assignment groups.

It's an estimate: it only knows assignments already posted on Canvas, ungraded work counts as
"remaining" (except locked work that was never turned in, which counts as a zero), and
drop-lowest rules are ignored.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from pai.canvas import parse_ts

LETTERS = {"A": 90.0, "B": 80.0, "C": 70.0, "D": 60.0}


@dataclass
class Group:
    name: str
    weight: float  # percent; ignored when the course isn't weighted
    earned: float = 0.0
    graded: float = 0.0  # points possible on graded work
    remaining: float = 0.0  # points possible on work not graded yet


def parse_target(text: str) -> float | None:
    """'B', 'b+', '85', '85%' -> a percentage. B+ means 87, B- means 80 (the B floor)."""
    t = text.strip().upper().rstrip("%")
    if not t:
        return None
    if t[0] in LETTERS and t[1:] in ("", "+", "-"):
        return LETTERS[t[0]] + (7 if t[1:] == "+" else 0)
    try:
        value = float(t)
    except ValueError:
        return None
    return value if 0 < value <= 100 else None


def groups_from_canvas(rows: list[dict], now: datetime) -> list[Group]:
    groups = []
    for g in rows:
        grp = Group(name=g.get("name", ""), weight=float(g.get("group_weight") or 0))
        for a in g.get("assignments") or []:
            pts = float(a.get("points_possible") or 0)
            if pts <= 0 or a.get("omit_from_final_grade"):
                continue
            sub = a.get("submission") or {}
            if sub.get("excused"):
                continue
            score = sub.get("score")
            if score is not None and sub.get("workflow_state") == "graded":
                grp.earned += float(score)
                grp.graded += pts
                continue
            lock_at = parse_ts(a.get("lock_at"))
            closed = lock_at is not None and lock_at <= now
            if closed and not sub.get("submitted_at"):
                grp.graded += pts  # can't be turned in anymore: a zero
            else:
                grp.remaining += pts
        groups.append(grp)
    return groups


def projection(groups: list[Group], weighted: bool) -> tuple[float, float] | None:
    """Final percent as a + b*x, where x is the average (0-1) on remaining work."""
    if weighted:
        live = [g for g in groups if g.graded + g.remaining > 0 and g.weight > 0]
        total_w = sum(g.weight for g in live)
        if not total_w:
            return None
        a = sum(g.weight * g.earned / (g.graded + g.remaining) for g in live) / total_w
        b = sum(g.weight * g.remaining / (g.graded + g.remaining) for g in live) / total_w
        return a * 100, b * 100
    earned = sum(g.earned for g in groups)
    possible = sum(g.graded + g.remaining for g in groups)
    remaining = sum(g.remaining for g in groups)
    if not possible:
        return None
    return earned / possible * 100, remaining / possible * 100


def needed(groups: list[Group], weighted: bool, target: float) -> str:
    """One plain sentence on what average the remaining posted work needs."""
    proj = projection(groups, weighted)
    remaining = sum(g.remaining for g in groups)
    if proj is None:
        return "There's no graded or upcoming work on Canvas yet to go on."
    a, b = proj
    if remaining == 0 or b <= 0:
        final = a
        verdict = (
            "already there" if final >= target else "not reachable with the work posted so far"
        )
        return (
            f"Everything posted is graded, so you're at about {final:.1f}% "
            f"({verdict} for {target:g}%). More work may still be added."
        )
    x = (target - a) / b * 100
    pts = f"{remaining:g} pts"
    if x <= 0:
        return f"You've got {target:g}% locked in even with zeros on the remaining {pts}."
    if x > 100:
        return (
            f"{target:g}% isn't reachable with the posted work: you'd need {x:.0f}% on the "
            f"remaining {pts}."
        )
    return (
        f"You need about {x:.0f}% on the remaining {pts} of posted work to finish at {target:g}%."
    )
