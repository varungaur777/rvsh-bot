"""One repository API over MongoDB + the per-document JSON mirror (spec §9.2). Callers never know which
store served them.

Depends on: ``db.json_backend``, ``db.mongo_backend``, ``db.outbox``, ``db.updates``.
Used by: everything that stores data (settings, access, tasks, exam, quiz, admin panels).

Public API
----------
- ``Repo(root, mongo_url, flush_ms, lru_size)`` · ``await start()`` · ``await close()``
- collections as attributes: ``repo.users``, ``repo.exams``, ``repo.exam_urls``, ``repo.exam_aliases``,
  ``repo.exam_results``, ``repo.quizzes``, ``repo.quiz_stats``, ``repo.groups``, ``repo.batches``,
  ``repo.settings``, ``repo.ocr_cache``, ``repo.marking_presets``, ``repo.running_quizzes``
- ``Collection.get(id, copy=True)`` · ``put(id, doc)`` · ``update(id, update, flt=None, upsert=True)`` ·
  ``delete(id)`` · ``find(flt, sort, limit, projection, sub)`` · ``stream(flt, projection, sub)`` · ``count(flt, sub)`` ·
  ``scan(sub)``
- ``repo.on_mongo_state`` — optional callback ``(healthy: bool, replayed: int)`` (dump notifier).

Consistency model (D-013)
-------------------------
- **Reads are local-first:** LRU → JSON file → Mongo (and a doc found only in Mongo is written to JSON:
  lazy hydration for ephemeral hosts). One process owns the data (instance lease), so both stores agree.
- **Writes:** under a per-document lock the update is applied locally (``updates.apply_update``) — the
  filter ``flt`` is checked there, once — then the JSON file is queued and Mongo receives the same
  operators (cheap, atomic). If Mongo is down or fails, the **resulting document** goes to the outbox.
- Every write stamps ``updated_at`` (UTC ISO).
- ``get(..., copy=True)`` returns a private copy; hot read-only paths may pass ``copy=False`` and must
  not mutate the result.

Exceptions → outcome
--------------------
- ``UpdateError`` (bad operator) propagates — programming error → task cancelled with an error ID.
- Mongo outages never propagate: state flips to unhealthy, writes go to the outbox, a health task replays
  it when Mongo returns.
"""

from __future__ import annotations

import asyncio
import contextlib
import copy as _copy
import logging
from collections import OrderedDict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from pymongo.errors import OperationFailure

from bot.core.bg import spawn
from bot.db.json_backend import JsonStore
from bot.db.mongo_backend import MongoStore, MongoUnavailable
from bot.db.outbox import Outbox
from bot.db.updates import apply_update, get_path, matches

log = logging.getLogger(__name__)

COLLECTIONS = (
    "users", "exams", "exam_urls", "exam_aliases", "exam_results", "quizzes", "quiz_stats", "groups",
    "batches", "settings", "ocr_cache", "marking_presets", "running_quizzes", "exam_mht",
)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class _LRU:
    def __init__(self, size: int) -> None:
        self.size = size
        self.data: OrderedDict[tuple, dict] = OrderedDict()

    def get(self, key: tuple) -> dict | None:
        doc = self.data.get(key)
        if doc is not None:
            self.data.move_to_end(key)
        return doc

    def set(self, key: tuple, doc: dict | None) -> None:
        if doc is None:
            self.data.pop(key, None)
            return
        self.data[key] = doc
        self.data.move_to_end(key)
        while len(self.data) > self.size:
            self.data.popitem(last=False)

    def drop_collection(self, coll: str) -> None:
        for k in [k for k in self.data if k[0] == coll]:
            del self.data[k]


class Collection:
    def __init__(self, repo: "Repo", name: str) -> None:
        self.repo = repo
        self.name = name

    # ------------------------------------------------------------ reads
    async def get(self, doc_id: Any, *, copy: bool = True) -> dict | None:
        r, key = self.repo, (self.name, doc_id)
        doc = r.cache.get(key)
        if doc is None:
            doc = await r.json.get(self.name, doc_id)
            if doc is None and r.mongo_ok:
                try:
                    doc = await r.mongo.get(self.name, doc_id)
                except MongoUnavailable:
                    r._mark_down()
                if doc is not None:
                    r.json.put(self.name, doc_id, doc)  # lazy hydration
            r.cache.set(key, doc)
        return _copy.deepcopy(doc) if (copy and doc is not None) else doc

    async def find(self, flt: dict | None = None, *, sort: list[tuple[str, int]] | None = None, limit: int = 0,
                   projection: dict | None = None, sub: str | None = None) -> list[dict]:
        """Query. Mongo when healthy; otherwise a JSON folder scan (``sub`` narrows it, e.g. one exam)."""
        r = self.repo
        if r.mongo_ok:
            try:
                return await r.mongo.find(self.name, flt, projection=projection, sort=sort, limit=limit)
            except MongoUnavailable:
                r._mark_down()
        out = [d async for d in r.json.scan(self.name, sub) if matches(d, flt)]
        for path, direction in reversed(sort or []):
            out.sort(key=lambda d, p=path: (get_path(d, p) is None, get_path(d, p)), reverse=direction < 0)
        return out[:limit] if limit else out

    async def stream(self, flt: dict | None = None, *, projection: dict | None = None, sub: str | None = None,
                     batch: int = 500):
        """Like ``find`` but yields documents one at a time (Mongo cursor in batches, else the JSON folder) — for
        exports over thousands of documents. Mongo failing before the first document → JSON; after it → raises
        ``MongoUnavailable`` (a half-read export must not silently mix stores)."""
        r = self.repo
        if r.mongo_ok:
            started = False
            try:
                async for doc in r.mongo.iterate(self.name, flt, projection=projection, batch=batch):
                    started = True
                    yield doc
                return
            except MongoUnavailable:
                r._mark_down()
                if started:
                    raise
        async for d in r.json.scan(self.name, sub):
            if matches(d, flt):
                yield d

    async def count(self, flt: dict | None = None, *, sub: str | None = None) -> int:
        r = self.repo
        if r.mongo_ok:
            try:
                return await r.mongo.count(self.name, flt)
            except MongoUnavailable:
                r._mark_down()
        return sum([1 async for d in r.json.scan(self.name, sub) if matches(d, flt)])

    def scan(self, sub: str | None = None):
        """Async iterator over the JSON mirror folder (no Mongo) — streaming exports, rebuilds."""
        return self.repo.json.scan(self.name, sub)

    # ------------------------------------------------------------ writes
    async def put(self, doc_id: Any, doc: dict) -> dict:
        """Replace the whole document."""
        async with self.repo._lock(self.name, doc_id):
            new = {**_copy.deepcopy(doc), "_id": doc_id, "updated_at": now_iso()}
            await self.repo._commit(self.name, doc_id, new, mongo_update=None)
            return _copy.deepcopy(new)

    async def update(self, doc_id: Any, update: dict, *, flt: dict | None = None, upsert: bool = True,
                     copy: bool = True) -> dict | None:
        """Apply Mongo-style operators. Returns the new document, or ``None`` when ``flt`` did not match
        (or the document is missing and ``upsert`` is False). ``copy=False``: the stored document itself — read
        only (big documents: ``quiz_stats`` with thousands of players)."""
        async with self.repo._lock(self.name, doc_id):
            cur = await self.get(doc_id, copy=False)
            if cur is None and not upsert:
                return None
            if flt and not matches(cur or {}, flt):
                return None
            stamped = {**update, "$set": {**update.get("$set", {}), "updated_at": now_iso()}}
            if not any(k.startswith("$") for k in update):  # replacement document
                stamped = {**update, "updated_at": now_iso()}
            new = apply_update(cur, stamped, is_insert=cur is None)
            new["_id"] = doc_id
            await self.repo._commit(self.name, doc_id, new, mongo_update=stamped)
            return _copy.deepcopy(new) if copy else new

    async def delete_all(self) -> int:
        """Every document of this collection, from Mongo **and** the JSON mirror (``/db`` → 🧨 Delete all, owner
        2026-10-13). Refused (``MongoUnavailable``) while Mongo is configured but down: a delete queued for later
        could not say which documents it meant. Returns how many were deleted."""
        r = self.repo
        if r.mongo is not None and not r.mongo_ok:
            raise MongoUnavailable("Mongo is down")
        ids = [d["_id"] async for d in r.json.scan(self.name)]
        n = 0
        if r.mongo is not None:
            try:
                n = await r.mongo.delete_all(self.name)
            except MongoUnavailable:
                r._mark_down()
                raise
        for doc_id in ids:
            r.json.delete(self.name, doc_id)
        r.cache.drop_collection(self.name)
        await r.json.flush()
        log.warning("collection %s emptied: %d documents (%d in the JSON mirror)", self.name, max(n, len(ids)), len(ids))
        return max(n, len(ids))

    async def delete(self, doc_id: Any) -> None:
        async with self.repo._lock(self.name, doc_id):
            r = self.repo
            r.cache.set((self.name, doc_id), None)
            r.json.delete(self.name, doc_id)
            if r.mongo_ok:
                try:
                    await r.mongo.delete(self.name, doc_id)
                    return
                except MongoUnavailable:
                    r._mark_down()
            if r.mongo is not None:
                await r.outbox.append({"coll": self.name, "id": doc_id, "op": "delete"})


class Repo:
    def __init__(self, root: Path, mongo_url: str = "", *, flush_ms: int = 1000, lru_size: int = 2000,
                 health_interval_s: float = 15.0) -> None:
        self.root = Path(root)
        self.json = JsonStore(self.root, flush_ms)
        self.mongo: MongoStore | None = MongoStore(mongo_url) if mongo_url else None
        self.outbox = Outbox(self.root / "_outbox" / "outbox.jsonl")
        self.cache = _LRU(lru_size)
        self.health_interval_s = health_interval_s
        self.on_mongo_state: Callable[[bool, int], Any] | None = None
        self._locks: dict[tuple, asyncio.Lock] = {}
        self._health_task: asyncio.Task | None = None
        self._writable = False  # Mongo writes allowed (connected AND outbox replayed)
        for name in COLLECTIONS:
            setattr(self, name, Collection(self, name))

    def collection(self, name: str) -> Collection:
        return getattr(self, name) if name in COLLECTIONS else Collection(self, name)

    @property
    def mongo_ok(self) -> bool:
        """Mongo may be used. Any noticed outage (exception *or* failed ping) closes writes until the outbox
        has been replayed — otherwise a new write could reach Mongo before older journaled ones (B-001)."""
        if self.mongo is None:
            return False
        if not self.mongo.healthy:
            if self._writable:
                self._mark_down()
            return False
        return self._writable

    # ------------------------------------------------------------ lifecycle
    async def start(self, connect_timeout_ms: int = 4000) -> None:
        """Connect to Mongo (short timeout) and start the health task. Never raises; boots on JSON if needed."""
        self.root.mkdir(parents=True, exist_ok=True)
        if self.mongo is not None:
            if await self.mongo.connect(connect_timeout_ms):
                await self._recover()
                asyncio.get_running_loop().create_task(self.mongo.ensure_indexes())
            self._health_task = asyncio.get_running_loop().create_task(self._health_loop())

    async def close(self) -> None:
        if self._health_task:
            self._health_task.cancel()
        await self.json.flush()
        if self.mongo is not None:
            await self.mongo.close()

    async def _health_loop(self) -> None:
        while True:
            await asyncio.sleep(self.health_interval_s)
            try:
                if self.mongo.db is None:
                    await self.mongo.connect()
                if await self.mongo.ping():
                    if not self._writable:
                        await self._recover()
                elif self._writable:
                    self._mark_down()
            except Exception:  # the loop must survive anything
                log.exception("mongo health loop error")

    async def _recover(self) -> None:
        """Replay the outbox, then allow Mongo writes again."""
        try:
            replayed = await self.outbox.replay(self.mongo)
        except MongoUnavailable:
            self.mongo.healthy = False
            return
        self._writable = True
        log.info("mongo healthy (replayed %d ops)", replayed)
        self._notify(True, replayed)

    def _mark_down(self) -> None:
        if self._writable:
            self._writable = False
            log.warning("mongo unavailable — serving from JSON, writes go to the outbox")
            self._notify(False, 0)

    def _notify(self, healthy: bool, replayed: int) -> None:
        if self.on_mongo_state:
            try:
                res = self.on_mongo_state(healthy, replayed)
                if asyncio.iscoroutine(res):
                    spawn(res, "mongo-state")
            except Exception:
                log.exception("on_mongo_state callback failed")

    # ------------------------------------------------------------ internals
    @contextlib.asynccontextmanager
    async def _lock(self, coll: str, doc_id: Any):
        key = (coll, repr(doc_id))
        lock = self._locks.get(key)
        if lock is None:
            lock = self._locks[key] = asyncio.Lock()
        try:
            async with lock:
                yield
        finally:
            if not lock.locked() and not getattr(lock, "_waiters", None):
                self._locks.pop(key, None)

    async def _commit(self, coll: str, doc_id: Any, new: dict, mongo_update: dict | None) -> None:
        self.cache.set((coll, doc_id), new)
        self.json.put(coll, doc_id, new)
        if self.mongo is None:
            return
        if self.mongo_ok:
            try:
                if mongo_update is None or not any(k.startswith("$") for k in mongo_update):
                    await self.mongo.replace(coll, doc_id, new)
                else:
                    await self.mongo.apply(coll, doc_id, mongo_update)  # no document back (big docs)
                return
            except MongoUnavailable:
                self._mark_down()
            except OperationFailure as e:  # operator the server rejects but the local engine accepted
                log.error("mongo rejected update on %s/%r (%r) — replacing with the local document", coll, doc_id, e)
                try:
                    await self.mongo.replace(coll, doc_id, new)
                    return
                except MongoUnavailable:
                    self._mark_down()
        await self.outbox.append({"coll": coll, "id": doc_id, "op": "replace", "doc": new})
