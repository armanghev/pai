"""Read-only GitHub client for alerts and the side-project standup."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from typing import Any

import httpx

from pai.canvas import parse_ts

API = "https://api.github.com"


class GitHubClient:
    def __init__(self, token: str, user: str, http: httpx.AsyncClient | None = None):
        self.user = user
        self.http = http or httpx.AsyncClient(
            base_url=API,
            timeout=30,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )

    async def login(self) -> str:
        """The configured user, or the token's own login when none is set."""
        if not self.user:
            self.user = (await self._get("/user"))["login"]
        return self.user

    async def _get(self, path: str, **params) -> Any:
        r = await self.http.get(path, params=params)
        r.raise_for_status()
        return r.json()

    async def active_repos(self, since: datetime) -> list[dict]:
        repos = await self._get("/user/repos", sort="pushed", per_page=100, affiliation="owner")
        return [
            r
            for r in repos
            if not r.get("archived") and (parse_ts(r["pushed_at"]) or since) >= since
        ]

    async def repos(self, names: list[str], active_days: int, now: datetime) -> list[dict]:
        if names:
            return list(await asyncio.gather(*(self._get(f"/repos/{n}") for n in names)))
        return await self.active_repos(now - timedelta(days=active_days))

    async def failed_runs(self, repo: str, since: datetime) -> list[dict]:
        data = await self._get(f"/repos/{repo}/actions/runs", per_page=20, status="failure")
        return [
            {
                "repo": repo,
                "name": r["name"],
                "branch": r["head_branch"],
                "url": r["html_url"],
                "at": r["updated_at"],
            }
            for r in data.get("workflow_runs", [])
            if (parse_ts(r["updated_at"]) or since) > since
        ]

    async def new_issues(self, repo: str, since: datetime) -> list[dict]:
        """Issues and PRs opened by someone else since `since`."""
        me = await self.login()
        data = await self._get(
            f"/repos/{repo}/issues", since=since.isoformat(), state="all", per_page=30
        )
        return [
            {
                "repo": repo,
                "kind": "PR" if "pull_request" in i else "issue",
                "title": i["title"],
                "by": i["user"]["login"],
                "url": i["html_url"],
            }
            for i in data
            if (parse_ts(i["created_at"]) or since) > since and i["user"]["login"] != me
        ]

    async def commit_count(self, repo: str, since: datetime) -> int:
        """Distinct commits since `since` across every branch (work often lives on feature
        branches). Capped at 100 per branch."""
        branches = await self._get(f"/repos/{repo}/branches", per_page=100)
        pages = await asyncio.gather(
            *(
                self._get(
                    f"/repos/{repo}/commits", sha=b["name"], since=since.isoformat(), per_page=100
                )
                for b in branches
            )
        )
        return len({c["sha"] for page in pages for c in page})

    async def week_summary(self, repo: dict, since: datetime) -> dict:
        name = repo["full_name"]
        commits, pulls = await asyncio.gather(
            self._get(f"/repos/{name}/commits", since=since.isoformat(), per_page=100),
            self._get(f"/repos/{name}/pulls", state="open", per_page=20),
        )
        return {
            "repo": name,
            "description": repo.get("description") or "",
            "last_push": repo.get("pushed_at"),
            "commits_this_week": len(commits),
            "commit_messages": [c["commit"]["message"].splitlines()[0] for c in commits[:15]],
            "open_prs": [p["title"] for p in pulls],
            "open_issues": repo.get("open_issues_count", 0),
        }
