"""JSON mirror on disk — one file per document (spec §9.2, owner-confirmed).

Depends on: orjson, stdlib. Used by: ``db/repo.py`` only (callers never touch it directly).

Layout (``<root>`` = ``DOWNLOAD_ROOT/_db``)
--------------------------------------------
``users/<id % 100>/<id>.json`` · ``exam_results/<exam_key slug>/<roll>.json`` (one folder per exam, so the
result PDF reads one folder) · ``exam_urls/<exam_name_slug>/<hash>.json`` (id ``slug|hash``; one folder per exam)
· ``ocr_cache/<hash[:2]>/<hash>.json`` (``exam_mht`` too) · everything else
``<collection>/<id>.json``. Ids that are not filename-safe are slugged with a short hash suffix; the real
``_id`` is always inside the file.

Writes
------
``put`` queues the document; a per-document timer writes it after ``flush_ms`` (later puts within the
window replace the queued version → one disk write). Files are written atomically (tmp → fsync →
``os.replace``) on a worker thread. ``get`` sees queued documents immediately. ``flush()`` writes
everything now (shutdown, snapshots).

Never raises for a missing file/folder (= "not in the mirror"); disk errors propagate as ``OSError``.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import re
from pathlib import Path
from typing import Any, AsyncIterator

import orjson

log = logging.getLogger(__name__)

_SAFE = re.compile(r"^[A-Za-z0-9_.\-]{1,120}$")
_DELETE = object()


def slug(value: str, keep: int = 80) -> str:
    """Filename-safe slug; adds a short hash when characters had to be replaced (keeps ids unique)."""
    s = str(value)
    if _SAFE.match(s) and s not in (".", ".."):
        return s
    clean = re.sub(r"[^A-Za-z0-9_.\-]+", "-", s).strip("-.")[:keep] or "id"
    return f"{clean}~{hashlib.sha1(s.encode()).hexdigest()[:8]}"


class JsonStore:
    def __init__(self, root: Path, flush_ms: int = 1000) -> None:
        self.root = Path(root)
        self.flush_s = max(flush_ms, 0) / 1000
        self._pending: dict[Path, Any] = {}  # path → doc or _DELETE
        self._timers: dict[Path, asyncio.TimerHandle] = {}
        self._inflight: dict[Path, asyncio.Task] = {}  # path → last write task (chained)

    # ------------------------------------------------------------ paths
    def path_for(self, coll: str, doc_id: Any) -> Path:
        sid = str(doc_id)
        if coll == "users" and sid.lstrip("-").isdigit():
            return self.root / coll / str(abs(int(sid)) % 100) / f"{sid}.json"
        if coll == "exam_results" and "|" in sid:
            exam, roll = sid.rsplit("|", 1)
            return self.root / coll / slug(exam) / f"{slug(roll)}.json"
        if coll == "exam_urls" and "|" in sid:
            exam, h = sid.rsplit("|", 1)
            return self.root / coll / slug(exam) / f"{slug(h)}.json"
        if coll in ("ocr_cache", "exam_urls", "exam_mht"):
            s = slug(sid)
            return self.root / coll / s[:2] / f"{s}.json"
        return self.root / coll / f"{slug(sid)}.json"

    def folder(self, coll: str, sub: str | None = None) -> Path:
        """Folder of a collection, or of one group inside it (e.g. one exam's results)."""
        return self.root / coll / slug(sub) if sub else self.root / coll

    # ------------------------------------------------------------ reads
    async def get(self, coll: str, doc_id: Any) -> dict | None:
        path = self.path_for(coll, doc_id)
        if path in self._pending:
            doc = self._pending[path]
            return None if doc is _DELETE else orjson.loads(orjson.dumps(doc))
        return await asyncio.to_thread(_read, path)

    async def scan(self, coll: str, sub: str | None = None, limit: int | None = None) -> AsyncIterator[dict]:
        """Yield documents of a collection folder (recursively) — used only for fallback queries."""
        base = self.folder(coll, sub)
        # snapshot queued docs BEFORE listing the folder: a write finishing in between leaves its doc either in the
        # snapshot or on disk (it leaves ``_pending`` only once on disk) — never in neither
        pending = {p: d for p, d in self._pending.items() if base in p.parents}
        paths = await asyncio.to_thread(lambda: sorted(base.rglob("*.json")) if base.exists() else [])
        seen = 0
        for p in paths:
            doc = pending.pop(p, None)
            if doc is _DELETE:
                continue
            doc = doc if doc is not None else await asyncio.to_thread(_read, p)
            if doc is not None:
                yield doc
                seen += 1
                if limit and seen >= limit:
                    return
        for doc in pending.values():  # queued, not yet on disk
            if doc is not _DELETE:
                yield doc

    # ------------------------------------------------------------ writes
    def put(self, coll: str, doc_id: Any, doc: dict) -> None:
        """Queue a full document write (coalesced per document)."""
        self._queue(self.path_for(coll, doc_id), doc)

    def delete(self, coll: str, doc_id: Any) -> None:
        self._queue(self.path_for(coll, doc_id), _DELETE)

    def _queue(self, path: Path, doc: Any) -> None:
        self._pending[path] = doc
        if path in self._timers:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:  # no loop (sync tools) → write now
            _write(path, self._pending.pop(path))
            return
        self._timers[path] = loop.call_later(self.flush_s, self._fire, path)

    def _fire(self, path: Path) -> None:
        """Timer callback: write the latest queued version. Writes of one path are chained, so an older
        version can never land after a newer one; the doc stays readable from memory until it is on disk."""
        self._timers.pop(path, None)
        if path not in self._pending:
            return
        doc = self._pending[path]
        prev = self._inflight.get(path)
        task = asyncio.get_running_loop().create_task(self._write_after(prev, path, doc))
        self._inflight[path] = task

        def done(t: asyncio.Task, p: Path = path, d: Any = doc) -> None:
            if self._inflight.get(p) is t:
                del self._inflight[p]
            if self._pending.get(p) is d and p not in self._timers:
                del self._pending[p]  # nothing newer was queued meanwhile
            if not t.cancelled() and t.exception():
                log.error("json write failed for %s: %r", p, t.exception())

        task.add_done_callback(done)

    async def _write_after(self, prev: asyncio.Task | None, path: Path, doc: Any) -> None:
        if prev is not None:
            await asyncio.gather(prev, return_exceptions=True)
        await asyncio.to_thread(_write, path, doc)

    async def flush(self) -> None:
        """Write every queued document now and wait until all writes are on disk."""
        for path in list(self._timers):
            self._timers.pop(path).cancel()
            self._fire(path)
        if self._inflight:
            await asyncio.gather(*list(self._inflight.values()), return_exceptions=True)

    @property
    def pending_count(self) -> int:
        return len(self._pending)


def _read(path: Path) -> dict | None:
    try:
        return orjson.loads(path.read_bytes())
    except FileNotFoundError:
        return None
    except orjson.JSONDecodeError:
        # a torn/corrupt file must not crash reads; keep it aside for inspection
        try:
            path.replace(path.with_suffix(".corrupt"))
        except OSError:
            pass
        return None


def _write(path: Path, doc: Any) -> None:
    if doc is _DELETE:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f".{os.getpid()}.tmp")
    data = orjson.dumps(doc, option=orjson.OPT_INDENT_2 | orjson.OPT_NON_STR_KEYS)
    with open(tmp, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
