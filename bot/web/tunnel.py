"""HTTPS for the Web App through a Cloudflare Tunnel (spec §17.7.1).

- ``quick`` (default, no domain): ``cloudflared tunnel --url http://127.0.0.1:PORT`` → the ``https://*.trycloudflare.com``
  address cloudflared prints. **The link survives bot restarts** (owner 2026-10-13: an old Web App button was dead
  after every restart): cloudflared runs as its own process (detached, its log in ``<state>.log``) and its pid / port /
  URL are kept in a small state file; the next start **reuses** it when it is alive, serves the same port and answers
  ``/api/health`` through Cloudflare. Only a dead cloudflared, a PC restart or another port gives a new link.
- ``named``: ``cloudflared tunnel run --token CLOUDFLARED_TOKEN`` with the fixed ``WEBAPP_BASE_URL`` (a child of the
  bot, stopped with it — its address never changes anyway).
- ``off``: no tunnel; ``WEBAPP_BASE_URL`` (own reverse proxy) or nothing — a kept quick tunnel is ended.

cloudflared missing or the tunnel dying → ``url`` is ``None`` (Web App buttons hidden), the owner is told once,
and it is started again with a growing pause (30 s → 10 min). Everything else keeps working.

Public API: ``Tunnel(mode, port, *, token, base_url, cmd, alert, pid_file)`` · ``await start()`` · ``await stop()`` ·
``url`` · ``reused`` (the link was kept from the last run).
Depends on: stdlib, psutil, aiohttp (health check). Used by: ``web.server``.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import time

log = logging.getLogger(__name__)
_URL = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")
_WIN = (r"C:\Program Files (x86)\cloudflared\cloudflared.exe", r"C:\Program Files\cloudflared\cloudflared.exe")
WATCH_S = 15  # how often a kept cloudflared is checked
URL_WAIT_S = 60  # a new quick tunnel prints its address within this
LOG_CAP = 5 * 2**20


def find_cloudflared(cmd: str = "") -> str | None:
    if cmd and (os.path.isfile(cmd) or shutil.which(cmd)):
        return cmd
    found = shutil.which("cloudflared")
    if found:
        return found
    return next((p for p in _WIN if os.path.isfile(p)), None)


def _alive(pid: int) -> bool:
    """A running cloudflared with this pid (not a reused pid of another program)."""
    try:
        import psutil
        p = psutil.Process(pid)
        return p.is_running() and p.status() != psutil.STATUS_ZOMBIE and "cloudflared" in " ".join(
            [p.name()] + p.cmdline()[:1]).lower()
    except Exception:
        return False


def _kill(pid: int) -> None:
    with contextlib.suppress(Exception):
        import psutil
        p = psutil.Process(pid)
        p.terminate()
        p.wait(5)


class Tunnel:
    def __init__(self, mode: str, port: int, *, token: str = "", base_url: str = "", cmd: str = "", alert=None,
                 pid_file=None) -> None:
        self.mode, self.port, self.token, self.base_url, self.cmd = mode, port, token, base_url.rstrip("/"), cmd
        self.pid_file = str(pid_file) if pid_file else None
        self.state_file = (os.path.splitext(self.pid_file)[0] + ".json") if self.pid_file else None
        self.log_file = (os.path.splitext(self.pid_file)[0] + ".log") if self.pid_file else None
        self.alert = alert or (lambda text: None)
        self.url: str | None = None
        self.reused = False
        self.pid: int | None = None
        self.proc: asyncio.subprocess.Process | None = None
        self._task: asyncio.Task | None = None
        self._stopping = False
        self._told = False

    # ------------------------------------------------------------ state (quick mode)
    def _read_state(self) -> dict:
        try:
            with open(self.state_file, encoding="utf-8") as f:
                d = json.load(f)
            return d if isinstance(d, dict) else {}
        except Exception:
            return {}

    def _write_state(self) -> None:
        if not self.state_file:
            return
        with contextlib.suppress(OSError):
            with open(self.state_file, "w", encoding="utf-8") as f:
                json.dump({"pid": self.pid, "port": self.port, "url": self.url, "at": int(time.time())}, f)
        if self.pid_file:  # older bots read only the pid file
            with contextlib.suppress(OSError):
                with open(self.pid_file, "w") as f:
                    f.write(str(self.pid))

    def _forget_state(self) -> None:
        for p in (self.state_file, self.pid_file):
            if p:
                with contextlib.suppress(OSError):
                    os.remove(p)

    def _end_leftover(self) -> None:
        """A cloudflared from an earlier run that cannot be kept (other mode, other port, not answering) → ended."""
        st = self._read_state()
        pid = int(st.get("pid") or 0)
        if not pid and self.pid_file and os.path.isfile(self.pid_file):
            with contextlib.suppress(Exception):
                pid = int(open(self.pid_file).read().strip() or 0)
        if pid and _alive(pid):
            _kill(pid)
            log.info("ended a leftover cloudflared (pid %d)", pid)
        self._forget_state()

    async def _healthy(self, url: str) -> bool:
        """``/api/health`` of this bot answered through the tunnel (a few tries — Cloudflare may need a moment)."""
        import aiohttp
        for _ in range(3):
            try:
                async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as s:
                    async with s.get(f"{url}/api/health") as r:
                        if r.status == 200:
                            return True
            except Exception:
                pass
            await asyncio.sleep(2)
        return False

    async def _try_reuse(self) -> bool:
        st = self._read_state()
        pid, url = int(st.get("pid") or 0), st.get("url") or ""
        if not (pid and url and st.get("port") == self.port and _alive(pid)):
            return False
        if not await self._healthy(url):
            log.info("kept cloudflared (pid %d) does not answer — starting a new one", pid)
            return False
        self.pid, self.url, self.reused = pid, url, True
        log.info("web app tunnel kept from the last run: %s", url)
        return True

    # ------------------------------------------------------------ lifecycle
    async def start(self) -> None:
        if self.mode == "quick":
            if not await self._try_reuse():
                self._end_leftover()
        else:
            self._end_leftover()
        if self.mode == "off" or (self.mode == "named" and not self.token):
            self.url = self.base_url or None
            return
        self._task = asyncio.get_running_loop().create_task(self._keep_quick() if self.mode == "quick" else self._keep_named())

    def _args(self) -> list[str]:
        if self.mode == "named":
            return ["tunnel", "--no-autoupdate", "run", "--token", self.token]
        return ["tunnel", "--no-autoupdate", "--url", f"http://127.0.0.1:{self.port}"]

    def _spawn_detached(self, exe: str) -> int:
        """cloudflared as its own process (it outlives the bot), output appended to the log file."""
        if self.log_file and os.path.isfile(self.log_file) and os.path.getsize(self.log_file) > LOG_CAP:
            with contextlib.suppress(OSError):
                os.remove(self.log_file)
        out = open(self.log_file, "ab") if self.log_file else subprocess.DEVNULL
        kw: dict = {"stdin": subprocess.DEVNULL, "stdout": out, "stderr": out, "close_fds": True}
        try:
            if sys.platform == "win32":
                flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
                try:  # leave a kill-on-close job (a service manager / terminal) when allowed
                    return subprocess.Popen([exe, *self._args()], creationflags=flags | 0x01000000, **kw).pid
                except OSError:
                    return subprocess.Popen([exe, *self._args()], creationflags=flags, **kw).pid
            return subprocess.Popen([exe, *self._args()], start_new_session=True, **kw).pid
        finally:
            if out is not subprocess.DEVNULL:
                out.close()

    async def _wait_url(self, pid: int, offset: int) -> str | None:
        end = time.monotonic() + URL_WAIT_S
        while time.monotonic() < end and not self._stopping:
            if self.log_file:
                with contextlib.suppress(OSError):
                    with open(self.log_file, "rb") as f:
                        f.seek(offset)
                        m = _URL.search(f.read().decode(errors="ignore"))
                    if m:
                        return m.group(0)
            if not _alive(pid):
                return None
            await asyncio.sleep(0.5)
        return None

    async def _keep_quick(self) -> None:
        pause = 30
        while not self._stopping:
            if self.pid is None or not _alive(self.pid):
                was = self.url
                self.url, self.reused = None, False
                if was:
                    self._tell("⚠️ <b>Web App tunnel stopped</b> — starting it again (the link will change); Web App "
                               "buttons are hidden meanwhile.")
                exe = find_cloudflared(self.cmd)
                if exe is None:
                    self._tell("⚠️ <b>Web App off</b> — <code>cloudflared</code> is not installed on this host.")
                    await asyncio.sleep(600)
                    continue
                offset = os.path.getsize(self.log_file) if self.log_file and os.path.isfile(self.log_file) else 0
                try:
                    self.pid = await asyncio.to_thread(self._spawn_detached, exe)
                    url = await self._wait_url(self.pid, offset)
                except Exception as e:
                    log.warning("cloudflared failed: %r", e)
                    url = None
                if url is None:
                    if self.pid:
                        _kill(self.pid)
                    self.pid = None
                    self._forget_state()
                    if self._stopping:
                        return
                    self._tell("⚠️ <b>Web App tunnel</b> could not start — trying again; Web App buttons are hidden.")
                    await asyncio.sleep(pause)
                    pause = min(600, pause * 2)
                    continue
                self.url, self._told, pause = url, False, 30
                self._write_state()
                log.info("web app tunnel: %s", url)
            await asyncio.sleep(WATCH_S)

    async def _keep_named(self) -> None:
        pause = 30
        while not self._stopping:
            exe = find_cloudflared(self.cmd)
            if exe is None:
                self._tell("⚠️ <b>Web App off</b> — <code>cloudflared</code> is not installed on this host.")
                await asyncio.sleep(600)
                continue
            try:
                self.proc = await asyncio.create_subprocess_exec(exe, *self._args(), stdout=asyncio.subprocess.DEVNULL,
                                                                 stderr=asyncio.subprocess.DEVNULL)
                self.pid = self.proc.pid
                self._write_state()
                self.url = self.base_url or None
                await self.proc.wait()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("cloudflared failed: %r", e)
            if self._stopping:
                return
            self._tell("⚠️ <b>Web App tunnel stopped</b> — starting it again.")
            await asyncio.sleep(pause)
            pause = min(600, pause * 2)

    def _tell(self, text: str) -> None:
        if not self._told:
            self._told = True
            self.alert(text)

    async def stop(self) -> None:
        """A quick tunnel is **kept running** (the next start reuses its link); a named one is stopped."""
        self._stopping = True
        if self._task:
            self._task.cancel()
        if self.proc is not None and self.proc.returncode is None:
            with contextlib.suppress(Exception):
                self.proc.terminate()
                await asyncio.wait_for(self.proc.wait(), 5)
            self._forget_state()
