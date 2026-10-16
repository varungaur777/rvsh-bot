"""Fast Telegram downloads: one file's parts fetched over several connections at once (owner 2026-10-12).

Why: Kurigram downloads a file in 1 MB chunks **one after another on one connection**, so a slow or distant data
centre makes every chunk pay a full round trip. Measured on the owner's PC (DC1, 3.7 MB ``.mht``): 20–21 s
sequential → 6.9 s with 4 connections of 512 KB parts.

How: per data centre a small pool of media connections (``tg.fast_download_connections``, default 4) is opened
on first use (≈ 3.5 s once; or at boot for ``tg.prewarm_dcs``) and **kept** for every later download. A foreign DC
is authorized **once** (``auth.exportAuthorization`` is flood-limited); its connections share that key.

Sharing (owner 2026-10-13: "bahut sare users aae to khi bandwidth jhel na pae"): a connection carries **one part
at a time**. Every download — of every user — takes a free connection for each part and gives it back, so at most
``connections`` parts (× 512 KB) are in flight per DC however many files are downloading: the line is never
flooded, files share it fairly (parts are served first come, first served) and each part is answered at the
line's speed.

Dead or slow connections (B-024, B-025): a part's time limit follows the measured speed (``PART_TIMEOUT_S`` up to
``PART_TIMEOUT_MAX_S``); a connection whose part times out is closed and replaced and the part is tried again on
another one; a pool idle for more than ``IDLE_PROBE_S`` is pinged first. Only when a part fails twice (or the
file has a problem: moved to another DC, expired reference…) is the file downloaded the normal Kurigram way, on a
**fresh** Kurigram media session and with a time limit — a download never fails or hangs because of this module.
One user's failure never closes the connections other downloads are using.

Depends on: Kurigram (``pyrogram``) raw API + ``file_id``. Used by: exam inputs (``exam.inputs.download_file``),
later PDF tools / OCR inputs.

Public API
----------
- ``FastDownloader(client, connections=4, part_kb=512)`` · ``await download(message, path, progress=None) -> str``
  (``progress``: Kurigram-style ``async (current, total)``) · ``await prewarm(dc_ids)`` · ``await close()`` ·
  ``stats()``.
"""

from __future__ import annotations

import asyncio
import logging
import os
import random
import time

from pyrogram import raw
from pyrogram.file_id import FileId, FileType

log = logging.getLogger(__name__)


PART_TIMEOUT_S = 20  # least time for one part (one request per connection: 512 KB at a quarter of the line)
PART_TIMEOUT_MAX_S = 90
IDLE_PROBE_S = 60  # a pool idle longer than this is pinged before use
PING_TIMEOUT_S = 4
PART_TRIES = 2


class _Pool:
    """The connections to one DC; ``free`` holds those not carrying a part right now."""

    def __init__(self, dc_id: int, sessions: list) -> None:
        self.dc_id = dc_id
        self.sessions = list(sessions)
        self.free: asyncio.Queue = asyncio.Queue()
        for s in self.sessions:
            self.free.put_nowait(s)
        self.used = time.monotonic()  # last time a connection answered (or the pool was made)
        self.part_s = 0.0  # moving average: seconds per part

    def timeout(self) -> float:
        return min(PART_TIMEOUT_MAX_S, max(PART_TIMEOUT_S, 4 * self.part_s))


class FastDownloader:
    def __init__(self, client, connections: int = 4, part_kb: int = 512) -> None:
        self.client = client
        self.connections = max(0, int(connections))
        self.part = max(4, int(part_kb)) * 1024 // 4096 * 4096  # GetFile: offset/limit multiples of 4 KB
        self.pools: dict[int, _Pool] = {}
        self._locks: dict[int, asyncio.Lock] = {}
        self.counters = {"fast": 0, "fallback": 0, "bytes": 0, "replaced": 0}

    # ------------------------------------------------------------ connections
    async def _open(self, dc_id: int):
        return await self.client.get_session(dc_id, is_media=True, temporary=True, export_authorization=False)

    async def _pool(self, dc_id: int) -> _Pool:
        """The DC's pool — made on first use; an idle one is pinged and its dead connections replaced."""
        lock = self._locks.setdefault(dc_id, asyncio.Lock())
        async with lock:  # one opener / prober at a time; other downloads wait here a few seconds at most
            pool = self.pools.get(dc_id)
            if pool is None or not pool.sessions:
                # authorize the DC once (Kurigram keeps that session); the media connections share its key, so
                # they skip auth.exportAuthorization — exporting per connection drew 3–5 s flood waits at boot
                await self.client.get_session(dc_id)
                made = await asyncio.gather(*(self._open(dc_id) for _ in range(self.connections)), return_exceptions=True)
                ok = [s for s in made if not isinstance(s, BaseException)]
                if not ok:
                    raise ConnectionError(f"no media connection to DC{dc_id}: {made[0]!r}")
                pool = self.pools[dc_id] = _Pool(dc_id, ok)
                log.info("fast download: %d connection(s) to DC%d ready", len(ok), dc_id)
            elif time.monotonic() - pool.used > IDLE_PROBE_S and pool.free.qsize() == len(pool.sessions):
                idle = [pool.free.get_nowait() for _ in range(pool.free.qsize())]
                alive = await asyncio.gather(*(_ping(s) for s in idle))
                for s, ok in zip(idle, alive):
                    if ok:
                        pool.free.put_nowait(s)
                    else:
                        await self._replace(pool, s)
                if not all(alive):
                    log.info("fast download: %d of %d idle connection(s) to DC%d were dead — replaced",
                             alive.count(False), len(alive), dc_id)
                pool.used = time.monotonic()
            return pool

    async def _replace(self, pool: _Pool, sess) -> None:
        """Close a connection that stopped answering and put a new one in its place (if it opens)."""
        if sess in pool.sessions:
            pool.sessions.remove(sess)
        self.counters["replaced"] += 1
        try:
            await asyncio.wait_for(sess.stop(), 5)
        except Exception:
            pass
        try:
            new = await asyncio.wait_for(self._open(pool.dc_id), 30)
        except Exception as e:
            log.info("fast download: no new connection to DC%d (%r)", pool.dc_id, e)
            if not pool.sessions:
                pool.free.put_nowait(None)  # wakes the parts waiting for a connection (the next download opens anew)
            return
        pool.sessions.append(new)
        pool.free.put_nowait(new)

    async def _drop(self, dc_id: int) -> None:
        pool = self.pools.pop(dc_id, None)
        for s in pool.sessions if pool else []:
            try:
                await s.stop()
            except Exception:
                pass

    async def prewarm(self, dc_ids) -> None:
        if self.connections:
            await asyncio.gather(*(self._pool(int(d)) for d in dc_ids), return_exceptions=True)

    async def close(self) -> None:
        for dc in list(self.pools):
            await self._drop(dc)

    # ------------------------------------------------------------ download
    async def download(self, message, path: str, progress=None) -> str:
        media = getattr(message, "document", None) or getattr(message, "video", None) or getattr(message, "audio", None)
        size = getattr(media, "file_size", 0) or 0
        if not self.connections or media is None or size <= self.part:
            return await self._fallback(message, path, progress, count=False)
        try:
            fid = FileId.decode(media.file_id)
        except Exception:
            return await self._fallback(message, path, progress)
        if fid.file_type not in (FileType.DOCUMENT, FileType.VIDEO, FileType.AUDIO, FileType.VOICE, FileType.VIDEO_NOTE):
            return await self._fallback(message, path, progress, count=False)
        loc = raw.types.InputDocumentFileLocation(id=fid.media_id, access_hash=fid.access_hash,
                                                  file_reference=fid.file_reference, thumb_size="")
        try:
            await self._parallel(fid.dc_id, loc, size, path, progress)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.info("fast download failed (%r) — normal download", e)
            return await self._fallback(message, path, progress, dc_id=fid.dc_id, size=size)
        self.counters["fast"] += 1
        self.counters["bytes"] += size
        return path

    async def _parallel(self, dc_id: int, loc, size: int, path: str, progress) -> None:
        pool = await self._pool(dc_id)
        n = -(-size // self.part)
        parts: asyncio.Queue = asyncio.Queue()
        for i in range(n):
            parts.put_nowait(i)
        tries = [0] * n
        await asyncio.to_thread(_prealloc, path, size)
        done = 0

        async def part(i: int) -> None:
            """One part on a free connection; a connection that times out is replaced and the part queued again."""
            nonlocal done
            sess = await pool.free.get()
            if sess is None:  # every connection is gone: pass the news on to the next waiter
                pool.free.put_nowait(None)
                raise ConnectionError(f"no live connection to DC{pool.dc_id}")
            t0 = time.monotonic()
            try:
                r = await sess.invoke(raw.functions.upload.GetFile(location=loc, offset=i * self.part, limit=self.part),
                                      retries=1, timeout=pool.timeout(), sleep_threshold=30)
            except asyncio.CancelledError:
                pool.free.put_nowait(sess)  # the request may still be answered; the connection itself is fine
                raise
            except Exception as e:
                tries[i] += 1
                if not _transient(e):
                    pool.free.put_nowait(sess)
                    raise
                await self._replace(pool, sess)
                if tries[i] >= PART_TRIES or not pool.sessions:
                    raise
                parts.put_nowait(i)
                return
            pool.free.put_nowait(sess)
            took = time.monotonic() - t0
            pool.used = time.monotonic()
            pool.part_s = took if not pool.part_s else 0.7 * pool.part_s + 0.3 * took
            if not isinstance(r, raw.types.upload.File):  # CDN redirect etc. → normal path
                raise RuntimeError(f"unexpected {type(r).__name__}")
            await asyncio.to_thread(_write_at, path, i * self.part, r.bytes)
            done += len(r.bytes)
            if progress:
                await progress(min(done, size), size)

        async def worker() -> None:
            while True:
                try:
                    i = parts.get_nowait()
                except asyncio.QueueEmpty:
                    return
                await part(i)

        # ``connections`` workers per file keep a part of it queued on every connection; with several files the
        # connections are shared (``pool.free``), so the line never carries more than ``connections`` parts
        # (a part put back is taken again by the worker that put it back, if no other one is free)
        jobs = [asyncio.ensure_future(worker()) for _ in range(self.connections)]
        finished, pending = await asyncio.wait(jobs, return_when=asyncio.FIRST_EXCEPTION)
        for job in pending:  # a part failed for good: stop the others, the caller falls back
            job.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        errors = [job.exception() for job in finished if job.exception()]  # read every one (no "never retrieved")
        if errors:
            raise errors[0]
        if os.path.getsize(path) != size or done < size:
            raise RuntimeError(f"short download {done}/{size}")

    async def _fallback(self, message, path: str, progress, count: bool = True, dc_id: int | None = None,
                        size: int = 0) -> str:
        """Kurigram's own download. After a fast-download failure its stored media session for that DC is replaced
        first (it may be just as dead), and the download gets a time limit (≥ 90 s, more for big files)."""
        if count:
            self.counters["fallback"] += 1
        if dc_id is not None:
            await self._reset_media_session(dc_id)
        limit = 90 + size / (64 * 1024)  # even 64 KB/s finishes
        return await asyncio.wait_for(self.client.download_media(message, file_name=path, progress=progress), limit)

    async def _reset_media_session(self, dc_id: int) -> None:
        sessions = getattr(self.client, "media_sessions", None)
        old = sessions.pop(dc_id, None) if isinstance(sessions, dict) else None
        if old is not None:
            try:
                await asyncio.wait_for(old.stop(), 5)
            except Exception:
                pass

    def stats(self) -> dict:
        return {**self.counters, "pools": {dc: len(p.sessions) for dc, p in self.pools.items()}}


def _prealloc(path: str, size: int) -> None:
    with open(path, "wb") as f:
        f.truncate(size)


def _write_at(path: str, offset: int, data: bytes) -> None:
    with open(path, "r+b") as f:
        f.seek(offset)
        f.write(data)


async def _ping(session) -> bool:
    try:  # retries=1: Kurigram's ``invoke`` makes ``retries`` attempts — 0 would mean none at all (B-025)
        await session.invoke(raw.functions.Ping(ping_id=random.getrandbits(63)), retries=1, timeout=PING_TIMEOUT_S)
        return True
    except Exception:
        return False


def _transient(e: BaseException) -> bool:
    """A dead or slow connection (worth a new one), not a file problem (moved, expired reference)."""
    return isinstance(e, (TimeoutError, asyncio.TimeoutError, OSError, ConnectionError)) or "timed out" in str(e).lower()
