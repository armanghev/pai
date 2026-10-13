import subprocess
from datetime import UTC, date, datetime, timedelta

import pytest

from pai import vault
from pai.config import Config, Vault
from pai.tools.vault import capture_to_vault, get_project, list_projects, project_activity

NOTE = """---
type: project
status: {status}
priority: {priority}
kind: web
last_activity: 2026-10-12
ship_start: {start}
ship_end: {end}
next_milestone: {milestone}
repo: {repo}
path: {path}
stack:
  - "[[Next.js]]"
tags:
  - project
---
# {name}

## Features: current milestone (locked)
- [x] Boot clean
- [ ] Rehearse the walkthrough
- [ ] Reset seed data

## Parking lot (not this milestone)
- Hosting

## Ship log
- 2026-10-12: Step 1 passed
- 2026-09-01: old entry
"""


def write(root, name, status="active", priority="", start="", end="", milestone="", repo="",
          path=""):  # fmt: skip
    (root / "projects").mkdir(parents=True, exist_ok=True)
    (root / "projects" / f"{name}.md").write_text(
        NOTE.format(
            name=name,
            status=status,
            priority=priority,
            start=start,
            end=end,
            milestone=milestone,
            repo=repo,
            path=path,
        )  # fmt: skip
    )


@pytest.fixture
def vault_dir(tmp_path):
    root = tmp_path / "vault"
    write(root, "WHL", priority="2", start="2026-10-19", end="2026-10-23", milestone="Demo-ready")
    write(root, "Orchestrat", priority="1", repo="github.com/octocat/orchestrat")
    write(root, "Pai", status="personal")
    write(root, "Linefree", status="paused")
    (root / "projects" / "Dashboard.md").write_text("---\ntype: dashboard\n---\n# not a project\n")
    return root


@pytest.fixture
def vctx(ctx, vault_dir):
    ctx.config.vault = Vault(path=vault_dir)
    return ctx


def test_frontmatter_scalars_lists_and_links():
    props, body = vault.parse_frontmatter('---\na: 1\nb: "x"\nc:\n  - "[[Go]]"\nd: []\ne:\n---\nhi')
    assert props == {"a": "1", "b": "x", "c": ["Go"], "d": [], "e": None}
    assert body == "hi"


def test_load_projects_parses_milestone_and_log(vault_dir):
    projects = vault.load_projects(vault_dir)
    assert [p.name for p in vault.by_priority(projects)] == ["Orchestrat", "WHL", "Linefree", "Pai"]
    whl = vault.find(projects, "whl")
    assert whl.ship_end == date(2026, 10, 23) and whl.priority == 2
    assert whl.done == 1 and whl.open_items == ["Rehearse the walkthrough", "Reset seed data"]
    assert whl.ship_log[0] == (date(2026, 10, 12), "Step 1 passed")
    assert vault.find(projects, "orch").github_repo == "octocat/orchestrat"


def test_briefing_lines_by_priority_and_window(vault_dir):
    lines = vault.briefing_lines(vault.load_projects(vault_dir), date(2026, 10, 13))
    assert len(lines) == 2  # personal projects stay out
    assert lines[0].startswith("• P1 *Orchestrat*") and "no ship window" in lines[0]
    assert "Demo-ready (1/3 done). Ships Oct 19–Oct 23" in lines[1]
    assert "next: Rehearse the walkthrough" in lines[1]
    late = vault.briefing_lines(vault.load_projects(vault_dir), date(2026, 10, 25))
    assert "ship window ended Oct 23" in late[1]
    inside = vault.briefing_lines(vault.load_projects(vault_dir), date(2026, 10, 20))
    assert "3 day(s) left" in inside[1]


def test_drift_alerts(vault_dir):
    projects = vault.load_projects(vault_dir)
    alerts = vault.drift_alerts(
        projects,
        week={"WHL": 2, "Orchestrat": 0, "Pai": 40, "Linefree": 25},
        recent={"WHL": 1, "Orchestrat": 0},
        quiet_days=4,
    )
    assert alerts[0] == "🟠 *Orchestrat* has had no commits in 4 days."
    assert "25 commits went to paused projects (Linefree 25) vs 2" in alerts[1]
    # Personal projects never count as drift.
    assert vault.drift_alerts(projects, {"WHL": 3, "Pai": 90}, {"WHL": 3}, 4) == []


def test_capture_appends_to_daily_inbox_file(vault_dir):
    now = datetime(2026, 10, 12, 9, 30)
    rel = vault.capture(vault_dir, "app idea:\n  a  thing", now)
    vault.capture(vault_dir, "second", now.replace(minute=45))
    text = (vault_dir / rel).read_text()
    assert str(rel) == "inbox/Telegram 2026-10-12.md"
    assert text.startswith("---\ntype: capture")
    assert text.endswith("- 09:30 app idea: a thing\n- 09:45 second\n")


async def test_local_git_commit_counts(tmp_path, vault_dir):
    repo = tmp_path / "repo"
    repo.mkdir()
    git = ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t"]
    subprocess.run([*git, "init", "-q"], check=True)
    subprocess.run([*git, "commit", "-q", "--allow-empty", "-m", "one"], check=True)
    write(vault_dir, "Local", path=str(repo))
    projects = vault.load_projects(vault_dir)
    counts = await vault.commit_counts(projects, datetime.now(UTC) - timedelta(days=1))
    assert counts == {"Local": 1}  # no local path or GitHub client for the others


async def test_tools(vctx):
    saved = await capture_to_vault({"text": "try Syncthing"}, vctx)
    assert saved["saved_to"].startswith("inbox/Telegram ")
    listed = await list_projects({"status": "active"}, vctx)
    assert [p["name"] for p in listed["projects"]] == ["Orchestrat", "WHL"]
    one = await get_project({"name": "whl"}, vctx)
    assert one["milestone_items"] == "1/3 done" and "Parking lot" in one["note"]
    assert "error" in await get_project({"name": "nope"}, vctx)
    activity = await project_activity({"days": 3650}, vctx)
    assert {"project": "WHL", "date": "2026-10-12", "entry": "Step 1 passed"} in activity[
        "ship_log"
    ]


async def test_tools_without_vault(ctx):
    assert "error" in await list_projects({}, ctx)
    assert "error" in await capture_to_vault({"text": "x"}, ctx)


def test_config_rejects_vault_without_projects(tmp_path):
    with pytest.raises(ValueError, match="no projects/ folder"):
        Config(vault={"path": str(tmp_path)})


async def test_git_sync_pulls_and_pushes_captures(tmp_path, vault_dir, vctx):
    def git(*args, cwd):
        subprocess.run(
            ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
            cwd=cwd, check=True, capture_output=True,
        )  # fmt: skip

    remote = tmp_path / "remote.git"
    git("init", "-q", "--bare", "-b", "main", str(remote), cwd=tmp_path)
    git("init", "-q", "-b", "main", cwd=vault_dir)
    git("add", "-A", cwd=vault_dir)
    git("commit", "-q", "-m", "init", cwd=vault_dir)
    git("remote", "add", "origin", str(remote), cwd=vault_dir)
    git("push", "-q", "-u", "origin", "main", cwd=vault_dir)
    for key, value in (("user.name", "Pai"), ("user.email", "pai@localhost")):
        git("config", key, value, cwd=vault_dir)

    # The Mac pushes a new project; Pai's next read sees it.
    mac = tmp_path / "mac"
    git("clone", "-q", str(remote), str(mac), cwd=tmp_path)
    write(mac, "Folio", status="personal")
    git("add", "-A", cwd=mac)
    git("commit", "-q", "-m", "folio", cwd=mac)
    git("push", "-q", cwd=mac)

    vctx.config.vault.git_sync = True
    vault._last_pull.clear()
    listed = await list_projects({}, vctx)
    assert "Folio" in [p["name"] for p in listed["projects"]]

    saved = await capture_to_vault({"text": "idea from the bus"}, vctx)
    assert "warning" not in saved
    git("pull", "-q", cwd=mac)
    assert "idea from the bus" in (mac / saved["saved_to"]).read_text()


async def test_github_commit_count_dedupes_across_branches():
    import httpx

    from pai.github import GitHubClient

    def handler(request):
        if request.url.path.endswith("/branches"):
            return httpx.Response(200, json=[{"name": "main"}, {"name": "demo"}])
        shas = {"main": ["a", "b"], "demo": ["a", "b", "c", "d"]}[request.url.params["sha"]]
        return httpx.Response(200, json=[{"sha": s} for s in shas])

    http = httpx.AsyncClient(
        base_url="https://api.github.com", transport=httpx.MockTransport(handler)
    )
    gh = GitHubClient("t", "octocat", http=http)
    assert await gh.commit_count("octocat/whl", datetime.now(UTC)) == 4
