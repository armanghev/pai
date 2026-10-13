"""Tool registry: name -> (schema, async fn).

Each tool module exposes TOOLS, a list of Tool. The registry keeps them in a fixed
order so the tool definitions stay byte-stable for prompt caching.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any

from pai.config import Config

if TYPE_CHECKING:
    from pai.agent import Agent
    from pai.canvas import CanvasClient
    from pai.github import GitHubClient


@dataclass
class ToolContext:
    conn: sqlite3.Connection
    config: Config
    send: Callable[[str], Awaitable[None]]  # message the owner on Telegram
    agent: Agent | None = None  # for tools that make their own model call
    canvas: CanvasClient | None = None
    github: GitHubClient | None = None
    # Schedules reminder `id` to fire at `when`; set by the bot (JobQueue).
    schedule_reminder: Callable[[int, datetime], None] | None = None
    unschedule_reminder: Callable[[int], None] | None = None
    # Sends a file to the owner: (filename, file object, caption).
    send_file: Callable[[str, Any, str], Awaitable[None]] | None = None
    # Sends text already formatted as Telegram HTML (used for spoilered money answers).
    send_html: Callable[[str], Awaitable[None]] | None = None
    money_on: bool = False  # Plaid configured and not paused
    # Holdings may go to the model only in the owner's own chat (not jobs, guests or /fix).
    portfolio_ok: bool = False
    # Set when a tool returned private money data this exchange: the reply is masked in
    # history and nothing may be saved to long-term memory.
    private_seen: bool = False
    # Sends text with inline buttons: rows of (label, callback data).
    send_buttons: Callable[[str, list[list[tuple[str, str]]]], Awaitable[None]] | None = None


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    input_schema: dict[str, Any]
    fn: Callable[[dict[str, Any], ToolContext], Awaitable[Any]] = field(compare=False)

    def definition(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
        }


def default_tools() -> list[Tool]:
    from pai.tools import (
        availability,
        canvas,
        drafts,
        github,
        grades,
        heartbeat,
        memory,
        money,
        notes,
        planner,
        portfolio,
        prices,
        reminders,
        research,
        routines,
        sites,
        slip,
        study,
        system,
        telegram,
        vault,
        weather,
    )

    # Fixed order keeps the cached prefix byte-stable.
    return [
        *telegram.TOOLS,
        *system.TOOLS,
        *canvas.TOOLS,
        *planner.TOOLS,
        *availability.TOOLS,
        *reminders.TOOLS,
        *notes.TOOLS,
        *weather.TOOLS,
        *sites.TOOLS,
        *github.TOOLS,
        *research.TOOLS,
        *study.TOOLS,
        *drafts.TOOLS,
        *grades.TOOLS,
        *prices.TOOLS,
        *vault.TOOLS,
        *memory.TOOLS,
        *routines.TOOLS,
        *slip.TOOLS,
        *heartbeat.TOOLS,
        *money.TOOLS,
        *portfolio.TOOLS,
    ]
