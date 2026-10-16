"""MongoDB backend over the PyMongo Async API (spec §9.2). Motor is deprecated — not used.

Depends on: pymongo (``AsyncMongoClient``). Used by: ``db/repo.py`` only.

Public API
----------
- ``MongoStore(url, db_name=None)`` · ``await connect(timeout_ms)`` · ``await ping()`` · ``healthy``
- ``get / replace / update / delete / find / iterate / count / ensure_indexes / close``

Exceptions → outcome
--------------------
All network/server failures surface as ``MongoUnavailable``; the repository then marks Mongo unhealthy,
serves reads from JSON and queues writes in the outbox (nothing is lost, the user notices nothing).
"""

from __future__ import annotations

import logging
from typing import Any

from pymongo import AsyncMongoClient, ReturnDocument
from pymongo.errors import ConfigurationError, OperationFailure, PyMongoError

log = logging.getLogger(__name__)


class MongoUnavailable(Exception):
    pass


INDEXES: dict[str, list[list[tuple[str, int]]]] = {
    "exam_results": [[("exam.exam_key", 1)], [("exam.exam_name_slug", 1), ("exam.date", 1)]],
    "quizzes": [[("fingerprint", 1)], [("dump_post.sig", 1)]],
    "exams": [[("fingerprint", 1)]],
    "exam_urls": [[("exam_name_slug", 1), ("date", 1), ("start_hhmm", 1)], [("url", 1)], [("exam_key", 1)]],
    "users": [[("tg_db.blocked_bot", 1)]],
}


class MongoStore:
    def __init__(self, url: str, db_name: str | None = None) -> None:
        self.url = url
        self.db_name = db_name
        self.client: AsyncMongoClient | None = None
        self.db = None
        self.healthy = False

    async def connect(self, timeout_ms: int = 4000) -> bool:
        """Create the client and ping once; returns health. Never raises."""
        if not self.url:
            return False
        try:
            self.client = AsyncMongoClient(self.url, serverSelectionTimeoutMS=timeout_ms, connectTimeoutMS=timeout_ms,
                                           appname="rvsh-bot", tz_aware=False, maxPoolSize=20)
            try:
                default = self.client.get_default_database()
                name = self.db_name or default.name
            except ConfigurationError:
                name = self.db_name or "rvsh_bot"
            self.db = self.client[name]
            return await self.ping()
        except PyMongoError as e:
            log.warning("mongo connect failed: %r", e)
            self.healthy = False
            return False

    async def ping(self) -> bool:
        if self.db is None:
            return False
        try:
            await self.db.command("ping")
            self.healthy = True
        except PyMongoError as e:
            if self.healthy:
                log.warning("mongo ping failed: %r", e)
            self.healthy = False
        return self.healthy

    def _coll(self, name: str):
        if self.db is None:
            raise MongoUnavailable("not connected")
        return self.db[name]

    async def _run(self, coro):
        try:
            return await coro
        except OperationFailure:
            raise  # a real query error (bad operator…) — not an outage
        except PyMongoError as e:
            self.healthy = False
            raise MongoUnavailable(repr(e)) from e

    async def get(self, coll: str, doc_id: Any) -> dict | None:
        return await self._run(self._coll(coll).find_one({"_id": doc_id}))

    async def replace(self, coll: str, doc_id: Any, doc: dict) -> None:
        await self._run(self._coll(coll).replace_one({"_id": doc_id}, doc, upsert=True))

    async def update(self, coll: str, doc_id: Any, update: dict, flt: dict | None = None, upsert: bool = True) -> dict | None:
        """Atomic update; returns the new document, or ``None`` when ``flt`` did not match."""
        query = {"_id": doc_id, **(flt or {})}
        return await self._run(self._coll(coll).find_one_and_update(
            query, update, upsert=upsert and not flt, return_document=ReturnDocument.AFTER))

    async def apply(self, coll: str, doc_id: Any, update: dict) -> None:
        """Upsert-update without reading the document back (``update`` returns the whole new document — 2 MB
        for a quiz with 10 000 players)."""
        await self._run(self._coll(coll).update_one({"_id": doc_id}, update, upsert=True))

    async def delete(self, coll: str, doc_id: Any) -> None:
        await self._run(self._coll(coll).delete_one({"_id": doc_id}))

    async def delete_all(self, coll: str) -> int:
        """Every document of ``coll`` (its indexes stay)."""
        res = await self._run(self._coll(coll).delete_many({}))
        return int(res.deleted_count)

    async def find(self, coll: str, flt: dict | None = None, *, projection: dict | None = None,
                   sort: list[tuple[str, int]] | None = None, limit: int = 0, skip: int = 0) -> list[dict]:
        cur = self._coll(coll).find(flt or {}, projection)
        if sort:
            cur = cur.sort(sort)
        if skip:
            cur = cur.skip(skip)
        if limit:
            cur = cur.limit(limit)
        return await self._run(cur.to_list())

    async def iterate(self, coll: str, flt: dict | None = None, *, projection: dict | None = None, batch: int = 500):
        """Async iterator over a query, fetched ``batch`` documents at a time (flat RAM for big exports)."""
        cur = self._coll(coll).find(flt or {}, projection, batch_size=batch)
        while True:
            try:
                doc = await anext(cur)
            except StopAsyncIteration:
                return
            except OperationFailure:
                raise
            except PyMongoError as e:
                self.healthy = False
                raise MongoUnavailable(repr(e)) from e
            yield doc

    async def count(self, coll: str, flt: dict | None = None) -> int:
        return await self._run(self._coll(coll).count_documents(flt or {}))

    async def ensure_indexes(self) -> None:
        for coll, specs in INDEXES.items():
            for keys in specs:
                try:
                    await self._run(self._coll(coll).create_index(keys))
                except (MongoUnavailable, OperationFailure) as e:
                    log.warning("index %s %s not created: %r", coll, keys, e)

    async def close(self) -> None:
        if self.client is not None:
            await self.client.close()
