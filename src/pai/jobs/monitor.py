"""Uptime, GitHub and host health alerting. State lives in kv so alerts fire on changes only."""

from __future__ import annotations

import json
import sqlite3

from pai import db

FAILS_BEFORE_DOWN = 2


def site_transition(conn: sqlite3.Connection, url: str, result: dict) -> str | None:
    """Update a site's state; return an alert only when it goes down or comes back."""
    key = f"site:{url}"
    state = json.loads(db.kv_get(conn, key, '{"down": false, "fails": 0}'))
    alert = None
    if result["up"]:
        if state["down"]:
            alert = f"✅ Back up: {url} ({result['status']}, {result['ms']} ms)"
        state = {"down": False, "fails": 0}
    else:
        state["fails"] += 1
        if not state["down"] and state["fails"] >= FAILS_BEFORE_DOWN:
            state["down"] = True
            why = result.get("status") or result.get("error", "no response")
            alert = f"🔴 Down: {url} ({why})"
    db.kv_set(conn, key, json.dumps(state))
    return alert


def health_alerts(conn: sqlite3.Connection, h: dict, temp_max: float, disk_max: float) -> list[str]:
    alerts = []
    checks = {
        "temp": (h["cpu_temp_c"] is not None and h["cpu_temp_c"] > temp_max,
                 f"🌡 The host is hot: {h['cpu_temp_c']}°C"),
        "disk": (h["disk_used_pct"] > disk_max, f"💾 Disk is {h['disk_used_pct']}% full"),
    }  # fmt: skip
    for name, (bad, msg) in checks.items():
        key = f"health_alert:{name}"
        was_bad = db.kv_get(conn, key) == "1"
        if bad and not was_bad:
            alerts.append(msg)
        db.kv_set(conn, key, "1" if bad else "0")
    return alerts
