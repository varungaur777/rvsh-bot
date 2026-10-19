"""Bot-side fetching policy: direct / remote / auto (spec §12.1).

Depends on: ``net.fetcher``, ``core.settings``, ``core.config``. Used by: exam input flow, ``/batch``.

Public API
----------
- ``await fetch_url(app, url, progress=None, on_assets=None, light=False) -> list[Page]`` (engine: setting
  ``fetch.engine``; ``light``: the page(s) only, for ``/links``)
  - ``fetch.mode = direct``: fetch from this machine (proxy: setting ``http.proxy`` or env ``HTTP_PROXY``).
  - ``remote``: ask the fetch server (``FETCH_API_URL`` + ``FETCH_API_TOKEN``).
  - ``auto``: direct first; on a refusal (non-200, network error, non-exam page) use the server if configured.
- ``await check_server(api_url, token) -> dict`` — ``/bot → config.env → 🧪 Test fetch server``: reachable?
  token right? (``GET /health``, then ``POST /fetch`` with a non-exam URL: 400 = token accepted, 401 = wrong;
  no exam site is opened). ``{"ok", "step": "url" | "reach" | "token" | "done", "detail", "busy", "ms"}``.

Exceptions → outcome: ``SourceUnavailable`` (key ``source_unavailable``) → the user gets the
"save as .mhtml and send the file" guide; the technical reason goes to logs/dump.
"""

from __future__ import annotations

import asyncio
import logging
import time

import aiohttp

from bot.core.errors import SourceUnavailable
from bot.net import fetcher

log = logging.getLogger(__name__)


async def fetch_url(app, url: str, progress=None, on_assets=None, *, light: bool = False) -> list[fetcher.Page]:
    """``light``: the page(s) only — no images / CSS / other-language postbacks (``/links`` language check)."""
    mode = app.settings.get("fetch.mode") or "direct"
    api, token = app.config.fetch_api_url, app.config.fetch_api_token
    proxy = app.settings.get("http.proxy") or app.config.http_proxy or None
    if mode == "remote" and not api:
        mode = "direct"
    try:
        if mode == "remote":
            return await fetcher.fetch_remote(api, token, url, assets=not light)
        try:
            return await fetcher.fetch_exam(url, proxy=proxy, progress=progress, on_assets=on_assets,
                                            other_language=bool(app.settings.get("fetch.other_language", True)),
                                            assets=not light,
                                            engine=app.settings.get("fetch.engine") or "auto",
                                            concurrency=int(app.settings.get("fetch.concurrency") or 48))
        except fetcher.FetchError as e:
            if mode == "auto" and api:
                log.info("direct fetch refused (%s) — trying the remote fetch server", e.reason)
                if progress:
                    progress("trying the backup server")
                return await fetcher.fetch_remote(api, token, url, assets=not light)
            raise
    except fetcher.FetchError as e:
        log.info("fetch failed (%s, status %s) for %s: %s", e.reason, e.status, fetcher.redact(url), e.detail[:200])
        raise SourceUnavailable(detail=f"{e.reason} status={e.status} {e.detail}"[:300], reason=e.reason) from e


async def check_server(api: str, token: str, timeout: float = 15) -> dict:
    if not api:
        return {"ok": False, "step": "url", "detail": "FETCH_API_URL is not set"}
    api = api.rstrip("/")
    t0 = time.monotonic()
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout)) as s:
            async with s.get(api + "/health") as r:
                health = await r.json(content_type=None) if r.status == 200 else None
                if not isinstance(health, dict) or not health.get("ok"):
                    async with s.get(api + "/api/health") as r2:
                        webapp = r2.status == 200
                    return {"ok": False, "step": "reach", "detail": (
                        "this is the bot's own Web App link, not the fetch server" if webapp else
                        f"/health answered {r.status} — not a fetch server?")}
            ms = int((time.monotonic() - t0) * 1000)
            async with s.post(api + "/fetch", json={"url": "https://example.invalid/token-check"},
                              headers={"X-Token": token}) as r:
                status = r.status
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as e:
        if "getaddrinfo" in str(e) or type(e).__name__ == "ClientConnectorDNSError":
            return {"ok": False, "step": "reach", "detail": "address not found — a new trycloudflare link needs about "
                                                            "a minute to start working; test again (or the link is old)"}
        return {"ok": False, "step": "reach", "detail": type(e).__name__ + (f": {e}" if str(e) else "")}
    if status == 401:
        return {"ok": False, "step": "token", "detail": "the server refused the token", "ms": ms}
    if status != 400:
        return {"ok": False, "step": "token", "detail": f"unexpected answer {status} to the token check", "ms": ms}
    return {"ok": True, "step": "done", "busy": int(health.get("busy") or 0), "ms": ms}
