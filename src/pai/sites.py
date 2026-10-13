"""HTTP uptime checks."""

from __future__ import annotations

import time

import httpx


async def check(url: str, timeout: float = 10, http: httpx.AsyncClient | None = None) -> dict:
    own = http is None
    http = http or httpx.AsyncClient(timeout=timeout, follow_redirects=True)
    started = time.monotonic()
    try:
        r = await http.get(url, headers={"User-Agent": "pai-uptime/1.0"})
        ms = round((time.monotonic() - started) * 1000)
        return {"url": url, "up": r.status_code < 400, "status": r.status_code, "ms": ms}
    except httpx.HTTPError as e:
        return {"url": url, "up": False, "status": None, "error": type(e).__name__}
    finally:
        if own:
            await http.aclose()
