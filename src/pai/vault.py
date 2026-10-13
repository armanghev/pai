"""The owner's Obsidian vault (e.g. ~/Notes): project notes, inbox capture, drift checks.

Reads the small YAML-ish frontmatter the vault's templates write (scalars and simple
lists), so no YAML dependency. Project notes live in projects/<Name>.md.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

log = logging.getLogger(__name__)

ACTIVE = "active"
# Personal projects are real work too; only shelved ones count as drift.
SHELVED = ("paused", "stalled")
_SHIP_LOG = re.compile(r"^- (\d{4}-\d{2}-\d{2}):\s*(.+)$")
_GITHUB = re.compile(r"github\.com[/:]([\w.-]+/[\w.-]+?)(?:\.git)?/?$")


def parse_frontmatter(text: str) -> tuple[dict, str]:
    """Returns (properties, body). Handles `key: value` and `key:` followed by `  - item`."""
    if not text.startswith("---\n"):
        return {}, text
    end = text.find("\n---", 4)
    if end == -1:
        return {}, text
    props: dict = {}
    key = None
    for line in text[4:end].splitlines():
        if line.startswith("  - ") and key:
            if not isinstance(props.get(key), list):
                props[key] = []
            props[key].append(_scalar(line[4:]))
        elif ":" in line and not line.startswith(" "):
            key, _, value = line.partition(":")
            key = key.strip()
            value = value.strip()
            props[key] = [] if value == "[]" else _scalar(value) if value else None
    return props, text[end + 4 :].lstrip("\n")


def _scalar(value: str):
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        value = value[1:-1]
    if value.startswith("[[") and value.endswith("]]"):
        value = value[2:-2]
    return value


def _date(value) -> date | None:
    try:
        return date.fromisoformat(str(value)) if value else None
    except ValueError:
        return None


def _section(body: str, heading: str) -> list[str]:
    """Lines under `## heading` up to the next `## `."""
    out, inside = [], False
    for line in body.splitlines():
        if line.startswith("## "):
            inside = line[3:].strip().lower().startswith(heading.lower())
            continue
        if inside:
            out.append(line)
    return out


@dataclass
class Project:
    name: str
    status: str
    priority: int | None = None
    kind: str = ""
    next_milestone: str = ""
    ship_start: date | None = None
    ship_end: date | None = None
    last_activity: date | None = None
    path: str = ""
    repo: str = ""
    done: int = 0
    open_items: list[str] = field(default_factory=list)
    ship_log: list[tuple[date, str]] = field(default_factory=list)
    body: str = ""

    @property
    def github_repo(self) -> str | None:
        m = _GITHUB.search(self.repo or "")
        return m.group(1) if m else None

    def local_path(self) -> Path | None:
        p = Path(self.path).expanduser() if self.path else None
        return p if p and (p / ".git").exists() else None

    def summary(self) -> dict:
        return {
            "name": self.name,
            "status": self.status,
            "priority": self.priority,
            "kind": self.kind,
            "next_milestone": self.next_milestone or None,
            "ship_window": (
                f"{self.ship_start or '?'} to {self.ship_end}" if self.ship_end else None
            ),
            "milestone_items": f"{self.done}/{self.done + len(self.open_items)} done",
            "last_activity": str(self.last_activity) if self.last_activity else None,
        }


def parse_project(name: str, text: str) -> Project | None:
    props, body = parse_frontmatter(text)
    if props.get("type") != "project":
        return None
    items = [
        line.strip()
        for line in _section(body, "Features: current milestone")
        if line.strip().startswith("- [")
    ]
    log = []
    for line in _section(body, "Ship log"):
        m = _SHIP_LOG.match(line.strip())
        if m and (d := _date(m.group(1))):
            log.append((d, m.group(2)))
    try:
        priority = int(props["priority"]) if props.get("priority") else None
    except ValueError:
        priority = None
    return Project(
        name=name,
        status=str(props.get("status") or ""),
        priority=priority,
        kind=str(props.get("kind") or ""),
        next_milestone=str(props.get("next_milestone") or ""),
        ship_start=_date(props.get("ship_start")),
        ship_end=_date(props.get("ship_end")),
        last_activity=_date(props.get("last_activity")),
        path=str(props.get("path") or ""),
        repo=str(props.get("repo") or ""),
        done=sum(1 for i in items if i[3:4].lower() == "x"),
        open_items=[i[6:].strip() for i in items if i.startswith("- [ ]") and i[6:].strip()],
        ship_log=log,
        body=body,
    )


def load_projects(vault: Path) -> list[Project]:
    projects = []
    for f in sorted((vault / "projects").glob("*.md")):
        p = parse_project(f.stem, f.read_text())
        if p:
            projects.append(p)
    return projects


def by_priority(projects: list[Project]) -> list[Project]:
    return sorted(projects, key=lambda p: (p.priority is None, p.priority or 0, p.name))


def find(projects: list[Project], name: str) -> Project | None:
    q = name.lower().strip()
    exact = [p for p in projects if p.name.lower() == q]
    if exact:
        return exact[0]
    loose = [p for p in projects if q in p.name.lower() or q in Path(p.path).name.lower()]
    return loose[0] if len(loose) == 1 else None


def capture(vault: Path, text: str, now: datetime, inbox: str = "inbox") -> Path:
    """Appends one line to inbox/Telegram YYYY-MM-DD.md; returns the file's vault path."""
    folder = vault / inbox
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"Telegram {now:%Y-%m-%d}.md"
    if not path.exists():
        path.write_text(
            f"---\ntype: capture\nsource: telegram\ndate: {now:%Y-%m-%d}\n---\n"
            f"# Telegram captures {now:%Y-%m-%d}\n\n"
        )
    with path.open("a") as f:
        f.write(f"- {now:%H:%M} {' '.join(text.split())}\n")
    return path.relative_to(vault)


def briefing_lines(projects: list[Project], today: date) -> list[str]:
    """One line per active project, top priority first."""
    out = []
    for p in by_priority([p for p in projects if p.status == ACTIVE]):
        tag = f"P{p.priority} " if p.priority else ""
        line = f"• {tag}*{p.name}*"
        if p.next_milestone:
            line += f": {p.next_milestone}"
        total = p.done + len(p.open_items)
        if total:
            line += f" ({p.done}/{total} done)"
        if p.ship_end is None:
            line += ". ⚪ no ship window"
        elif p.ship_end < today:
            line += f". 🔴 ship window ended {p.ship_end:%b} {p.ship_end.day}"
        elif p.ship_start and today < p.ship_start:
            line += f". Ships {p.ship_start:%b} {p.ship_start.day}–{p.ship_end:%b} {p.ship_end.day}"
        else:
            days = (p.ship_end - today).days
            line += f". 🟡 in ship window, {days} day(s) left"
        if p.open_items:
            line += f"\n   next: {p.open_items[0][:90]}"
        out.append(line)
    return out


async def local_commits(path: Path, since: datetime) -> int:
    proc = await asyncio.create_subprocess_exec(
        "git",
        "-C",
        str(path),
        "rev-list",
        "--all",
        "--count",
        f"--since={since.isoformat()}",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    out, _ = await proc.communicate()
    return int(out.strip() or 0) if proc.returncode == 0 else 0


async def commit_counts(projects: list[Project], since: datetime, github=None) -> dict[str, int]:
    """Commits per project since `since`: local git (all branches) when the code is on this
    machine, else GitHub. Projects neither can see are left out."""

    async def one(p: Project) -> tuple[str, int | None]:
        if path := p.local_path():
            return p.name, await local_commits(path, since)
        if github and (repo := p.github_repo):
            try:
                return p.name, await github.commit_count(repo, since)
            except Exception:
                return p.name, None
        return p.name, None

    pairs = await asyncio.gather(*(one(p) for p in projects))
    return {name: n for name, n in pairs if n is not None}


def drift_alerts(
    projects: list[Project], week: dict[str, int], recent: dict[str, int], quiet_days: int
) -> list[str]:
    """`week`: commits in the last 7 days; `recent`: commits in the last `quiet_days`."""
    active = [p for p in by_priority(projects) if p.status == ACTIVE]
    out = []
    for p in active:
        if p.name in recent and recent[p.name] == 0:
            out.append(f"🟠 *{p.name}* has had no commits in {quiet_days} days.")
    active_total = sum(week.get(p.name, 0) for p in active)
    elsewhere = sorted(
        ((week[p.name], p) for p in projects if p.status in SHELVED and week.get(p.name)),
        key=lambda x: -x[0],
    )
    side_total = sum(n for n, _ in elsewhere)
    if side_total > active_total:
        top = ", ".join(f"{p.name} {n}" for n, p in elsewhere[:3])
        focus = ", ".join(p.name for p in active) or "nothing"
        out.append(
            f"🧭 This week {side_total} commits went to paused projects ({top}) "
            f"vs {active_total} on your active ones ({focus})."
        )
    return out


def shipped_since(projects: list[Project], since: date) -> list[dict]:
    """Ship log entries on or after `since`, newest first."""
    rows = [
        {"project": p.name, "date": str(d), "entry": text}
        for p in projects
        for d, text in p.ship_log
        if d >= since
    ]
    return sorted(rows, key=lambda r: r["date"], reverse=True)


# ---- git sync (when Pai runs somewhere other than the Mac) ---------------------

_git_lock = asyncio.Lock()
_last_pull: dict[Path, float] = {}
PULL_MAX_AGE = 60  # seconds; chat tools and jobs share one fresh pull


async def _git(vault: Path, *args: str) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(
        "git",
        "-C",
        str(vault),
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    out, _ = await proc.communicate()
    return proc.returncode, out.decode(errors="replace").strip()


async def pull(vault: Path, max_age: float = PULL_MAX_AGE) -> None:
    """Fast-forwards to the vault repo on GitHub. Failures are logged; the local copy is
    still usable, just possibly stale."""
    async with _git_lock:
        if time.monotonic() - _last_pull.get(vault, -max_age) < max_age:
            return
        code, out = await _git(vault, "pull", "--rebase", "--autostash", "-q")
        if code:
            log.warning("vault pull failed: %s", out)
        else:
            _last_pull[vault] = time.monotonic()


async def push(vault: Path, rel: Path, message: str) -> bool:
    """Commits one file and pushes, rebasing once if the Mac pushed in between."""
    async with _git_lock:
        await _git(vault, "add", "--", str(rel))
        code, out = await _git(vault, "commit", "-q", "-m", message)
        if code and "nothing to commit" not in out:
            log.warning("vault commit failed: %s", out)
            return False
        for _ in range(2):
            code, out = await _git(vault, "push", "-q")
            if code == 0:
                return True
            await _git(vault, "pull", "--rebase", "--autostash", "-q")
        log.warning("vault push failed: %s", out)
        return False
