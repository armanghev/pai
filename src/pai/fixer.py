"""/fix: diagnose a problem from recent errors and Pai's own source, and propose a patch.

Read-only by construction: the fixer's tools can list recent errors and read or search
files inside the pai package (Python, prompts, SQL). It can't write, run, commit or
deploy anything; the owner takes a proposed diff to Claude Code to apply it.
"""

from __future__ import annotations

import re
from importlib import resources
from pathlib import Path

import pai
from pai import errorlog
from pai.tools import Tool, ToolContext

ROOT = Path(pai.__file__).resolve().parent
READABLE = {".py", ".md", ".sql"}
MAX_LINES = 400
MAX_HITS = 40
RUN_COST_LIMIT = 0.60  # dollars per /fix


def _path(rel: str) -> Path:
    """A readable file inside the package, or ValueError."""
    rel = rel.strip().removeprefix("src/").removeprefix("pai/")
    path = (ROOT / rel).resolve()
    if not path.is_relative_to(ROOT) or path.suffix not in READABLE or not path.is_file():
        raise ValueError(f"can only read .py/.md/.sql files inside src/pai, not {rel!r}")
    return path


def files() -> list[str]:
    return sorted(
        f"src/pai/{p.relative_to(ROOT)}"
        for p in ROOT.rglob("*")
        if p.is_file() and p.suffix in READABLE and "__pycache__" not in p.parts
    )


async def recent_errors(args: dict, ctx: ToolContext) -> str:
    limit = min(int(args.get("limit", 10)), 25)
    records = errorlog.LOG.recent(limit, args.get("min_level", "WARNING"))
    if not records:
        return "No warnings or errors recorded since Pai last started."
    out = []
    for r in records:
        local = r.at.astimezone(ctx.config.tz)
        out.append(f"#{r.id} {local:%a %m/%d %H:%M} {r.level} {r.logger}: {r.message}")
        if r.trace:
            out.append(r.trace.rstrip())
    return "\n".join(out)


async def read_source(args: dict, ctx: ToolContext) -> str:
    path = _path(args["path"])
    lines = path.read_text().splitlines()
    start = max(1, int(args.get("start", 1)))
    end = min(len(lines), int(args.get("end", start + MAX_LINES - 1)), start + MAX_LINES - 1)
    body = "\n".join(f"{i:4d}  {lines[i - 1]}" for i in range(start, end + 1))
    more = f"\n… {len(lines) - end} more lines" if end < len(lines) else ""
    return errorlog.LOG.redact(
        f"src/pai/{path.relative_to(ROOT)} ({len(lines)} lines)\n{body}{more}"
    )


async def search_source(args: dict, ctx: ToolContext) -> str:
    try:
        pattern = re.compile(args["pattern"])
    except re.error as e:
        return f"Bad regex: {e}"
    hits = []
    for rel in files():
        for i, line in enumerate(_path(rel).read_text().splitlines(), 1):
            if pattern.search(line):
                hits.append(f"{rel}:{i}: {line.strip()[:200]}")
                if len(hits) >= MAX_HITS:
                    return errorlog.LOG.redact("\n".join(hits) + "\n… (more hits cut)")
    return errorlog.LOG.redact("\n".join(hits) or "No matches.")


def _schema(props: dict, required: list[str]) -> dict:
    return {
        "type": "object",
        "properties": props,
        "required": required,
        "additionalProperties": False,
    }


TOOLS = [
    Tool(
        name="recent_errors",
        description="Recent WARNING/ERROR log records since Pai started, with tracebacks, "
        "newest last. Secrets are already redacted.",
        input_schema=_schema(
            {
                "limit": {"type": "integer", "description": "Default 10, max 25."},
                "min_level": {"type": "string", "enum": ["WARNING", "ERROR"]},
            },
            [],
        ),
        fn=recent_errors,
    ),
    Tool(
        name="read_source",
        description="Read one of Pai's source files (.py, prompts .md, migrations .sql) with "
        f"line numbers, up to {MAX_LINES} lines per call.",
        input_schema=_schema(
            {
                "path": {"type": "string", "description": "e.g. src/pai/bot.py"},
                "start": {"type": "integer"},
                "end": {"type": "integer"},
            },
            ["path"],
        ),
        fn=read_source,
    ),
    Tool(
        name="search_source",
        description="Regex search across Pai's source files; returns path:line: text.",
        input_schema=_schema({"pattern": {"type": "string"}}, ["pattern"]),
        fn=search_source,
    ),
]


def system_prompt() -> str:
    base = resources.files("pai.prompts").joinpath("fix.md").read_text().strip()
    return base + "\n\nSource files:\n" + "\n".join(files())
