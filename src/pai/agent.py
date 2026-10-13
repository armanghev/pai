"""The Claude tool loop: model routing, prompt caching, budget guard and spend tracking."""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

import anthropic

from pai import memory, prompts, spend
from pai.config import Config
from pai.tools import Tool, ToolContext

log = logging.getLogger(__name__)

Route = Literal["chat", "planner", "deep"]
EFFORT: dict[Route, str] = {"chat": "low", "planner": "medium", "deep": "high"}
MAX_ROUNDS = 8
HISTORY_TURNS = 6
MAX_TOKENS = 16000
FALLBACK_BETA = "server-side-fallback-2026-07-01"
MCP_BETA = "mcp-client-2025-11-20"
NO_SERVER_FALLBACK = ("claude-haiku",)  # Haiku has no server-side refusal fallback

CONTENT_BLOCKS = {"text", "document", "image"}


def _is_blocks(out: Any) -> bool:
    """A tool result made of API content blocks rather than data to serialize."""
    return (
        isinstance(out, list)
        and bool(out)
        and all(isinstance(b, dict) and b.get("type") in CONTENT_BLOCKS for b in out)
    )


PRIVATE_NOTE = "(Answered a portfolio question from their holdings; details not kept.)"
COST_CAP = "I stopped partway: this job hit its cost limit for one run."
BUDGET_HIT = "I've hit today's spending limit, so I'm resting my wings until midnight. 🦉"
REFUSED = "I can't help with that one."
TRUNCATED_NOTE = "\n\n(I ran out of room and cut that short.)"
ROUND_CAP = "That took more steps than I allow myself. Try asking in a simpler way?"


def load_system_prompt(config: Config | None = None) -> str:
    return prompts.render("system.md", config or Config())


@dataclass
class Reply:
    text: str
    model: str
    rounds: int
    cost_usd: float
    private: bool = False  # used private money data; send with numbers hidden


def final_text(content) -> str:
    """Text after the last tool step. Server tools interleave short working notes
    ("checking candidates…") with their calls; only the closing answer is for the user."""
    last_tool = max(
        (i for i, b in enumerate(content) if b.type not in ("text", "thinking")), default=-1
    )
    tail = "".join(b.text for b in content[last_tool + 1 :] if b.type == "text").strip()
    return tail or "".join(b.text for b in content if b.type == "text").strip()


class BudgetExceeded(RuntimeError):
    pass


class Agent:
    def __init__(
        self,
        client: anthropic.AsyncAnthropic,
        conn: sqlite3.Connection,
        config: Config,
        tools: list[Tool],
        system_prompt: str | None = None,
    ):
        self.client = client
        self.conn = conn
        self.config = config
        self.tools = {t.name: t for t in tools}
        # Frozen prefix: tools then system, cached as one block. No timestamps in here.
        self._tool_defs = [t.definition() for t in tools]
        self._system = [
            {
                "type": "text",
                "text": system_prompt or load_system_prompt(config),
                "cache_control": {"type": "ephemeral"},
            }
        ]

    # ---- history -------------------------------------------------------------

    def history(self) -> list[dict[str, Any]]:
        # Stored text is undated; each replayed turn gets the time it was sent so
        # "tonight" in Sunday's reply isn't read as Monday night.
        rows = self.conn.execute(
            "SELECT role, text, created_at FROM messages ORDER BY id DESC LIMIT ?",
            (HISTORY_TURNS,),
        ).fetchall()
        turns = [
            {"role": r["role"], "content": self._date_turn(r["role"], r["text"], r["created_at"])}
            for r in reversed(rows)
        ]
        while turns and turns[0]["role"] != "user":  # must open with a user turn
            turns.pop(0)
        return turns

    def _date_turn(self, role: str, text: str, created_at: str) -> str:
        sent = datetime.fromisoformat(created_at)
        if role == "user":
            return self._stamp(text, sent)
        return f"[sent {sent.astimezone(self.config.tz):%A %H:%M}]\n{text}"

    def _save(self, role: str, text: str, now: datetime) -> None:
        self.conn.execute(
            "INSERT INTO messages (role, text, created_at) VALUES (?, ?, ?)",
            (role, text, spend.utc_iso(now)),
        )

    # ---- one-shot helpers for jobs and tools --------------------------------

    def _guard(self, now: datetime | None = None) -> None:
        now = now or datetime.now(UTC)
        if spend.spent_today(self.conn, now, self.config.tz) >= self.config.budget.daily_usd:
            raise BudgetExceeded(BUDGET_HIT)

    def _routing(self, route: Route) -> tuple[str, dict[str, Any]]:
        model = getattr(self.config.models, route)
        extra: dict[str, Any] = {"output_config": {"effort": EFFORT[route]}}
        if not model.startswith(NO_SERVER_FALLBACK):
            extra["betas"] = [FALLBACK_BETA]
            extra["fallbacks"] = "default"
        return model, extra

    async def structured(self, route: Route, system: str, user: str | list, schema, job: str):
        """Single structured-output call (messages.parse). Returns a `schema` instance."""
        self._guard()
        model, extra = self._routing(route)
        r = await self.client.beta.messages.parse(
            model=model,
            max_tokens=MAX_TOKENS,
            system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": user}],
            output_format=schema,
            **extra,
        )
        spend.record(self.conn, r.model or model, r.usage, job)
        if r.stop_reason == "refusal" or r.parsed_output is None:
            raise RuntimeError(f"{job}: no structured output (stop_reason={r.stop_reason})")
        return r.parsed_output

    async def complete(
        self,
        route: Route,
        system: str,
        user: str | list,
        job: str,
        tools: list[dict[str, Any]] | None = None,
    ) -> str:
        """Single text call, optionally with server tools (e.g. web search)."""
        self._guard()
        model, extra = self._routing(route)
        messages: list[dict[str, Any]] = [{"role": "user", "content": user}]
        for _ in range(3):  # server tools can pause a long turn; resume it
            r = await self.client.beta.messages.create(
                model=model,
                max_tokens=MAX_TOKENS,
                system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
                messages=messages,
                **({"tools": tools} if tools else {}),
                **extra,
            )
            spend.record(self.conn, r.model or model, r.usage, job)
            if r.stop_reason != "pause_turn":
                break
            messages = [*messages, {"role": "assistant", "content": r.content}]
        if r.stop_reason == "refusal":
            return REFUSED
        return final_text(r.content)

    # ---- the loop ------------------------------------------------------------

    def _stamp(self, text: str, now: datetime) -> str:
        local = now.astimezone(self.config.tz)
        return f"[{local:%A %Y-%m-%d %H:%M} {local.tzname()}]\n{text}"

    async def _call(self, route: Route, messages: list[dict[str, Any]], on_text=None, mcp=None):
        """One model call. With `on_text`, it streams and passes the round's text so far.
        `mcp` is (mcp_servers, toolsets) for the API's MCP connector, appended after Pai's
        own tools so the cached prefix only changes on requests that use a server."""
        model, extra = self._routing(route)
        tools = self._tool_defs
        if mcp and mcp[0]:
            servers, toolsets = mcp
            tools = [*tools, *toolsets]
            extra = {**extra, "mcp_servers": servers}
            extra["betas"] = [*extra.get("betas", []), MCP_BETA]
        kwargs = dict(
            model=model,
            max_tokens=MAX_TOKENS,
            system=self._system,
            tools=tools,
            tool_choice={"type": "auto"},
            messages=messages,
            **extra,
        )
        if on_text is None:
            return await self.client.beta.messages.create(**kwargs)
        text = ""
        async with self.client.beta.messages.stream(**kwargs) as stream:
            async for event in stream:
                delta = getattr(event, "delta", None)
                if (
                    event.type == "content_block_delta"
                    and getattr(delta, "type", "") == "text_delta"
                ):
                    text += delta.text
                    await on_text(text)
            return await stream.get_final_message()

    async def _run_tool(self, block, ctx: ToolContext) -> dict[str, Any]:
        result: dict[str, Any] = {"type": "tool_result", "tool_use_id": block.id}
        tool = self.tools.get(block.name)
        try:
            if tool is None:
                raise LookupError(f"unknown tool {block.name}")
            out = await tool.fn(dict(block.input or {}), ctx)
            if _is_blocks(out):  # e.g. a PDF document plus text
                result["content"] = out
            else:
                result["content"] = out if isinstance(out, str) else json.dumps(out, default=str)
        except Exception as e:  # report to the model, don't crash the exchange
            log.exception("tool %s failed", block.name)
            result["content"] = f"{type(e).__name__}: {e}"
            result["is_error"] = True
        return result

    async def run(
        self,
        text: str,
        ctx: ToolContext,
        route: Route = "chat",
        now: datetime | None = None,
        job: str = "chat",
        history: bool = True,
        max_cost: float | None = None,
        on_text: Callable[[str], Awaitable[None]] | None = None,
        memories: bool = True,
        mcp: tuple[list, list] | None = None,
    ) -> Reply:
        """One exchange through the tool loop. Scheduled jobs pass history=False (no replay,
        nothing saved) and a max_cost that stops the loop once a run has spent that much."""
        now = now or datetime.now(UTC)
        model = getattr(self.config.models, route)

        if spend.spent_today(self.conn, now, self.config.tz) >= self.config.budget.daily_usd:
            log.warning("daily budget hit; skipping Claude")
            return Reply(BUDGET_HIT, model, 0, 0.0)

        remembered = memory.block(self.conn) if memories else ""
        latest = self._stamp(text, now)
        if remembered:  # here rather than the system prompt keeps the cached prefix stable
            latest = f"{remembered}\n\n{latest}"
        messages = [*(self.history() if history else []), {"role": "user", "content": latest}]
        total = 0.0
        retried_on_sonnet = False

        for rounds in range(1, MAX_ROUNDS + 1):
            response = await self._call(route, messages, on_text, mcp)
            total += spend.record(self.conn, response.model or model, response.usage, job)

            if response.stop_reason == "refusal":
                # Haiku has no server-side fallback: re-ask once on the planner model.
                if model.startswith(NO_SERVER_FALLBACK) and not retried_on_sonnet:
                    retried_on_sonnet = True
                    model, route = self.config.models.planner, "planner"
                    log.info("refusal on haiku; retrying on %s", model)
                    continue
                return self._finish(
                    text, REFUSED, model, rounds, total, now, history, ctx.private_seen
                )

            if response.stop_reason == "pause_turn":  # a long server-tool turn; resume it
                messages.append({"role": "assistant", "content": response.content})
                continue

            if response.stop_reason == "tool_use":
                if max_cost is not None and total >= max_cost:
                    log.warning("%s stopped at its $%.2f cost limit", job, max_cost)
                    return self._finish(
                        text, COST_CAP, model, rounds, total, now, history, ctx.private_seen
                    )
                calls = [b for b in response.content if b.type == "tool_use"]
                results = await asyncio.gather(*(self._run_tool(b, ctx) for b in calls))
                # Append the full content unchanged (thinking blocks included), then
                # every result in a single user message.
                messages.append({"role": "assistant", "content": response.content})
                messages.append({"role": "user", "content": list(results)})
                continue

            reply = "".join(b.text for b in response.content if b.type == "text").strip()
            if response.stop_reason == "max_tokens":
                reply += TRUNCATED_NOTE
            return self._finish(
                text, reply or "🦉", model, rounds, total, now, history, ctx.private_seen
            )

        return self._finish(
            text, ROUND_CAP, model, MAX_ROUNDS, total, now, history, ctx.private_seen
        )

    def _finish(
        self,
        user_text: str,
        reply: str,
        model: str,
        rounds: int,
        cost: float,
        now: datetime,
        keep: bool = True,
        private: bool = False,
    ) -> Reply:
        # Only the final text of each side is kept; tool calls stay inside the exchange. A
        # reply built on private money data isn't kept, so it isn't resent every turn.
        if keep:
            self._save("user", user_text, now)
            self._save("assistant", PRIVATE_NOTE if private else reply, now)
        log.info("reply via %s in %d round(s), $%.5f", model, rounds, cost)
        return Reply(reply, model, rounds, cost, private)
