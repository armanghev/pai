"""Flashcards: Haiku turns notes into cards once; quizzing is local spaced repetition (SM-2)."""

from __future__ import annotations

import sqlite3
from datetime import date, timedelta

from pydantic import BaseModel, Field

SYSTEM = (
    "You turn a student's class notes into flashcards for spaced-repetition review. "
    "Make one card per important fact, definition, term or concept, 5 to 25 cards, without "
    "duplicates. Fronts are short questions or prompts; backs are short, exact answers (one "
    "or two sentences, no more). Use only what's in the notes."
)
RATINGS = {"again": 0, "hard": 3, "good": 4, "easy": 5}


class Card(BaseModel):
    front: str
    back: str


class Cards(BaseModel):
    cards: list[Card] = Field(max_length=25)


def save(conn: sqlite3.Connection, deck: str, cards: list[Card], today: date) -> int:
    rows = [(deck.strip()[:40], c.front.strip(), c.back.strip(), today.isoformat()) for c in cards]
    conn.executemany("INSERT INTO flashcards (deck, front, back, due_on) VALUES (?, ?, ?, ?)", rows)
    return len(rows)


async def make(agent, conn: sqlite3.Connection, deck: str, notes: str, today: date) -> int:
    if len(notes.strip()) < 40:
        raise ValueError("send a bit more of your notes than that")
    cards = await agent.structured("chat", SYSTEM, notes[:30_000], Cards, job="flashcards")
    return save(conn, deck, cards.cards, today)


def decks(conn: sqlite3.Connection, today: date) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT deck, COUNT(*) AS total, SUM(due_on <= ?) AS due FROM flashcards "
        "GROUP BY deck ORDER BY deck",
        (today.isoformat(),),
    ).fetchall()


def due_count(conn: sqlite3.Connection, today: date) -> int:
    return conn.execute(
        "SELECT COUNT(*) FROM flashcards WHERE due_on <= ?", (today.isoformat(),)
    ).fetchone()[0]


def next_due(
    conn: sqlite3.Connection, today: date, deck: str | None = None, skip: int | None = None
) -> sqlite3.Row | None:
    """The next card to review, avoiding `skip` (the card just failed) when there are others."""
    sql = "SELECT * FROM flashcards WHERE due_on <= ?"
    args: list = [today.isoformat()]
    if deck:
        sql += " AND deck = ?"
        args.append(deck)
    rows = conn.execute(sql + " ORDER BY due_on, reps, id LIMIT 2", args).fetchall()
    for row in rows:
        if row["id"] != skip:
            return row
    return rows[0] if rows else None


def schedule(ease: float, interval: int, reps: int, quality: int) -> tuple[float, int, int]:
    """SM-2. Returns (ease, interval_days, reps). Interval 0 means 'again today'."""
    if quality < 3:
        return max(1.3, ease - 0.2), 0, 0
    ease = max(1.3, ease + 0.1 - (5 - quality) * (0.08 + (5 - quality) * 0.02))
    reps += 1
    if reps == 1:
        interval = 1
    elif reps == 2:
        interval = 3 if quality < 5 else 4
    else:
        interval = max(interval + 1, round(interval * ease * (0.8 if quality == 3 else 1)))
    return ease, interval, reps


def rate(conn: sqlite3.Connection, card_id: int, rating: str, today: date) -> int:
    row = conn.execute("SELECT * FROM flashcards WHERE id = ?", (card_id,)).fetchone()
    if row is None:
        raise LookupError("card not found")
    ease, interval, reps = schedule(row["ease"], row["interval"], row["reps"], RATINGS[rating])
    conn.execute(
        "UPDATE flashcards SET ease = ?, interval = ?, reps = ?, due_on = ? WHERE id = ?",
        (ease, interval, reps, (today + timedelta(days=interval)).isoformat(), card_id),
    )
    return interval


def next_due_date(conn: sqlite3.Connection, deck: str | None = None) -> str | None:
    sql, args = "SELECT MIN(due_on) FROM flashcards", []
    if deck:
        sql, args = sql + " WHERE deck = ?", [deck]
    return conn.execute(sql, args).fetchone()[0]
