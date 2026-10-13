"""API usage recording and spend summaries, all from the api_usage table."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

# USD per million tokens: (input, output, cache read). Cache writes bill at 1.25x input.
PRICES: dict[str, tuple[float, float, float]] = {
    "claude-haiku-5-5": (0.10, 0.50, 0.01),
    "claude-sonnet-5-5": (2.00, 10.00, 0.20),
    "claude-opus-5-5": (4.00, 20.00, 0.20),
}
# Unknown models are priced like Opus so the budget guard errs on the safe side.
_FALLBACK_PRICE = PRICES["claude-opus-5-5"]


def utc_iso(dt: datetime) -> str:
    """Same text format SQLite's strftime default in the schema produces."""
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def cost_usd(
    model: str,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
) -> float:
    p_in, p_out, p_read = PRICES.get(model, _FALLBACK_PRICE)
    return (
        input_tokens * p_in
        + output_tokens * p_out
        + cache_read_tokens * p_read
        + cache_write_tokens * p_in * 1.25
    ) / 1_000_000


def record(conn: sqlite3.Connection, model: str, usage, job: str | None = None) -> float:
    """Store one API response's usage. `usage` is the SDK's Usage object."""
    inp = usage.input_tokens or 0
    out = usage.output_tokens or 0
    read = getattr(usage, "cache_read_input_tokens", 0) or 0
    write = getattr(usage, "cache_creation_input_tokens", 0) or 0
    cost = cost_usd(model, inp, out, read, write)
    conn.execute(
        "INSERT INTO api_usage"
        " (model, input_tokens, output_tokens, cache_read_tokens, cost_usd, job)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (model, inp + write, out, read, cost, job),
    )
    return cost


def spent_since(conn: sqlite3.Connection, since: datetime) -> float:
    row = conn.execute(
        "SELECT COALESCE(SUM(cost_usd), 0) FROM api_usage WHERE created_at >= ?",
        (utc_iso(since),),
    ).fetchone()
    return float(row[0])


def period_starts(now: datetime, tz: ZoneInfo) -> dict[str, datetime]:
    """Local midnight of today, this Monday and the 1st of this month."""
    local = now.astimezone(tz)
    today = local.replace(hour=0, minute=0, second=0, microsecond=0)
    return {
        "today": today,
        "week": today - timedelta(days=today.weekday()),
        "month": today.replace(day=1),
    }


def spent_today(conn: sqlite3.Connection, now: datetime, tz: ZoneInfo) -> float:
    return spent_since(conn, period_starts(now, tz)["today"])


def summary(conn: sqlite3.Connection, now: datetime, tz: ZoneInfo) -> str:
    starts = period_starts(now, tz)
    lines = [
        f"{label.capitalize()}: ${spent_since(conn, start):.4f}" for label, start in starts.items()
    ]
    calls = conn.execute(
        "SELECT COUNT(*) FROM api_usage WHERE created_at >= ?", (utc_iso(starts["month"]),)
    ).fetchone()[0]
    lines.append(f"Calls this month: {calls}")
    return "\n".join(lines)
