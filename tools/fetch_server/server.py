"""Remote fetch server — fetches TCS/EDQT response-sheet URLs and returns ready ``.mht`` archives (spec §12.1).

Why: exam portals often answer only Indian IPs. Host this tiny server on any machine that can open the
links (a Windows PC, an Indian VPS…) and set ``FETCH_API_URL`` / ``FETCH_API_TOKEN`` in the bot.

Run
---
    pip install aiohttp            # lxml not needed
    python server.py --tunnel      # Windows: double-click start_fetch_server.bat

``--tunnel`` also starts a Cloudflare quick tunnel (``cloudflared``) and prints the ``https://….trycloudflare.com``
address and the token to put in the bot (``/bot → 🔑 config.env``) — owner 2026-10-14 ("token kese set kre? …
link kese milega"). The token: ``FETCH_TOKEN`` when set, else ``fetch_token.txt`` next to this file (made once,
random). Without ``--tunnel`` the server only listens (own port forwarding / VPS).

Files needed: this ``server.py`` + ``fetcher.py`` (copy ``bot/net/fetcher.py`` next to it). Inside the
bot repository it imports ``bot.net.fetcher`` directly — one implementation, no duplicated logic.

Env vars: ``FETCH_TOKEN`` (else ``fetch_token.txt``) · ``PORT`` (8787) · ``HOST`` (0.0.0.0) · ``MAX_CONCURRENT`` (2) ·
``HTTP_PROXY`` (optional, used for portal requests).

API
---
- ``POST /fetch`` ``{"url": "...", "assets": true}`` (``false``: the page(s) only) with header ``X-Token`` → ``200 {"pages": [{url, kind, section, assets,
  missing, mht_b64}]}`` or ``4xx/5xx {"error", "status", "detail"}``
- ``GET /health`` → ``{"ok": true, "busy": n}``

run it: [cloudflared tunnel --url http://localhost:8787]

"""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import logging
import os
import re
import secrets
import shutil
import sys
import time
from pathlib import Path

from aiohttp import web

try:  # inside the bot repository
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from bot.net import fetcher  # type: ignore
except ImportError:  # deployed standalone: fetcher.py copied next to this file
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import fetcher  # type: ignore

log = logging.getLogger("fetch_server")


def make_app(token: str, max_concurrent: int = 2, proxy: str | None = None) -> web.Application:
    sem = asyncio.Semaphore(max_concurrent)
    state = {"busy": 0}

    async def health(_request: web.Request) -> web.Response:
        return web.json_response({"ok": True, "busy": state["busy"]})

    async def fetch(request: web.Request) -> web.Response:
        if not hmac.compare_digest(request.headers.get("X-Token", ""), token):
            return web.json_response({"error": "unauthorized"}, status=401)
        try:
            body = await request.json()
            url = str(body["url"])
            assets = body.get("assets", True) is not False  # False: the bot's light check (/links)
        except (ValueError, KeyError, TypeError):
            return web.json_response({"error": "bad_request"}, status=400)
        if fetcher.detect_kind(url) is None:
            return web.json_response({"error": "unsupported_url"}, status=400)
        async with sem:
            state["busy"] += 1
            try:
                pages = await fetcher.fetch_exam(url, proxy=proxy, engine=os.environ.get("FETCH_ENGINE", "auto"),
                                                 assets=assets)
            except fetcher.FetchError as e:
                log.info("fetch failed %s: %s", fetcher.redact(url), e)
                return web.json_response({"error": e.reason, "status": e.status, "detail": e.detail[:200]}, status=502)
            finally:
                state["busy"] -= 1
        log.info("fetched %s → %d page(s) via %s", fetcher.redact(url), len(pages), pages[0].engine)
        return web.json_response(fetcher.pages_to_json(pages))

    app = web.Application(client_max_size=64 * 1024)
    app.router.add_get("/health", health)
    app.router.add_post("/fetch", fetch)
    return app


TOKEN_FILE = Path(__file__).resolve().parent / "fetch_token.txt"
_URL = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")
_WIN = (r"C:\Program Files (x86)\cloudflared\cloudflared.exe", r"C:\Program Files\cloudflared\cloudflared.exe")


def load_token(path: Path = TOKEN_FILE) -> str:
    """``FETCH_TOKEN``, else the saved ``fetch_token.txt`` (a random one is made the first time)."""
    token = os.environ.get("FETCH_TOKEN", "").strip()
    if token:
        return token
    with contextlib.suppress(OSError):
        token = path.read_text(encoding="utf-8").strip()
    if len(token) < 16:
        token = secrets.token_hex(16)
        path.write_text(token + "\n", encoding="utf-8")
    return token


def find_cloudflared() -> str | None:
    return shutil.which("cloudflared") or next((p for p in _WIN if os.path.isfile(p)), None)


def _box(lines: list[str]) -> str:
    w = max(len(x) for x in lines) + 2
    return "\n".join(["", "=" * w, *[" " + x for x in lines], "=" * w, ""])


async def _tunnel(app: web.Application, port: int, token: str):
    """cleanup_ctx: a quick tunnel to this server while it runs; its address is printed with the token."""
    exe = find_cloudflared()
    if not exe:
        print(_box(["cloudflared is not installed - no public link.",
                    "Windows: winget install Cloudflare.cloudflared   (then start this again)"]), flush=True)
        yield
        return
    proc = await asyncio.create_subprocess_exec(exe, "tunnel", "--no-autoupdate", "--url", f"http://127.0.0.1:{port}",
                                                stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.DEVNULL,
                                                stderr=asyncio.subprocess.PIPE)
    found = asyncio.get_running_loop().create_future()

    async def read() -> None:  # cloudflared logs to stderr: find the address, keep draining the pipe
        async for raw in proc.stderr:
            m = _URL.search(raw.decode(errors="ignore"))
            if m and not found.done():
                found.set_result(m.group(0))
        if not found.done():
            found.set_result(None)

    async def show() -> None:  # the server is listening meanwhile
        try:
            url = await asyncio.wait_for(asyncio.shield(found), 60)
        except asyncio.TimeoutError:
            url = None
        if url:
            print(_box(["Put these in the bot:  /bot -> config.env",
                        f"Fetch server URL   : {url}",
                        f"Fetch server token : {token}",
                        "then tap 'Test fetch server'.  /bot -> Network -> Fetching links: Auto",
                        "Keep this window open. A new start gives a NEW link (put it in the bot again).",
                        "A new link starts working in about a minute - wait for 'Link is working' below."]), flush=True)
            import aiohttp
            end = time.monotonic() + 300
            while time.monotonic() < end:  # Cloudflare's DNS for a new name takes a while
                with contextlib.suppress(Exception):
                    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as s:
                        async with s.get(url + "/health") as r:
                            if r.status == 200:
                                print(f"\n>>> Link is working now: {url}  (tap 'Test fetch server' in the bot)\n",
                                      flush=True)
                                return
                await asyncio.sleep(5)
            print("\n>>> The link does not answer from this PC yet - try 'Test fetch server' in the bot anyway.\n",
                  flush=True)
        else:
            print(_box(["cloudflared did not give a link (internet? firewall?). The server still runs locally."]),
                  flush=True)

    tasks = [asyncio.get_running_loop().create_task(read()), asyncio.get_running_loop().create_task(show())]
    try:
        yield
    finally:
        with contextlib.suppress(ProcessLookupError):
            proc.terminate()
        with contextlib.suppress(Exception):
            await asyncio.wait_for(proc.wait(), 5)
        for t in tasks:
            t.cancel()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    token = load_token()
    if len(token) < 16:
        sys.exit("Set FETCH_TOKEN to a secret of at least 16 characters.")
    port = int(os.environ.get("PORT", "8787"))
    app = make_app(token, int(os.environ.get("MAX_CONCURRENT", "2")), os.environ.get("HTTP_PROXY") or None)
    if "--tunnel" in sys.argv[1:]:
        app.cleanup_ctx.append(lambda a: _tunnel(a, port, token))
    web.run_app(app, host=os.environ.get("HOST", "0.0.0.0"), port=port)


if __name__ == "__main__":
    main()
