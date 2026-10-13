from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest
from conftest import usage

from pai import spend

LA = ZoneInfo("America/Los_Angeles")


def test_cost_math():
    # 1M input + 1M output on Haiku 5.5
    assert spend.cost_usd("claude-haiku-5-5", 1_000_000, 1_000_000) == pytest.approx(0.60)
    # Cache writes bill at 1.25x input; reads at the cache-read rate.
    assert spend.cost_usd("claude-sonnet-5-5", 0, 0, 1_000_000, 1_000_000) == pytest.approx(2.7)


def test_unknown_model_priced_conservatively():
    assert spend.cost_usd("claude-new", 1_000_000, 0) == spend.cost_usd(
        "claude-opus-5-5", 1_000_000, 0
    )


def test_period_starts_use_local_midnight():
    now = datetime(2026, 10, 10, 3, 0, tzinfo=UTC)  # Friday 20:00 in LA
    starts = spend.period_starts(now, LA)
    assert starts["today"].isoformat() == "2026-10-09T00:00:00-07:00"
    assert starts["week"].date().isoformat() == "2026-10-05"  # Monday
    assert starts["month"].date().isoformat() == "2026-10-01"


def test_spent_today_respects_timezone(conn):
    # 06:30 UTC on the 10th is 23:30 on the 9th in LA: yesterday, local time.
    conn.execute(
        "INSERT INTO api_usage (model, cost_usd, created_at) VALUES ('m', 1.0, ?)",
        ("2026-10-10T06:30:00.000Z",),
    )
    conn.execute(
        "INSERT INTO api_usage (model, cost_usd, created_at) VALUES ('m', 0.25, ?)",
        ("2026-10-10T08:00:00.000Z",),
    )
    now = datetime(2026, 10, 10, 20, 0, tzinfo=UTC)
    assert spend.spent_today(conn, now, LA) == pytest.approx(0.25)
    assert "Month: $1.2500" in spend.summary(conn, now, LA)


def test_record_and_default_timestamp_format_match(conn):
    spend.record(conn, "claude-haiku-5-5", usage(1000, 100), job="chat")
    stored = conn.execute("SELECT created_at FROM api_usage").fetchone()[0]
    assert len(stored) == len(spend.utc_iso(datetime.now(UTC)))
    assert spend.spent_since(conn, datetime(2000, 1, 1, tzinfo=UTC)) > 0
