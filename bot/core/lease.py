"""Single-instance lease (spec §1.1): two processes with one bot token would steal each other's updates.

Depends on: pymongo (through ``db.mongo_backend.MongoStore``). Used by: ``bot.app`` boot/shutdown.

Public API
----------
- ``Lease(mongo, holder_info)`` · ``await acquire(max_wait_s=150) -> bool`` · ``start_heartbeat(on_lost)`` ·
  ``await release()``

Mechanics: document ``_lease/main`` = ``{holder, host, pid, started_at, heartbeat, handover}``. A lease is
free when absent, held by us, its heartbeat is older than ``stale_s`` (90 s), or its holder was a process on
this host that no longer exists (crash/kill → immediate takeover). A new instance that finds a
live holder writes ``handover = <its id>``; the holder's heartbeat loop sees it and shuts down gracefully,
releasing the lease. Without Mongo the lease is skipped (logged) — single-host development.
"""

from __future__ import annotations

import asyncio
import logging
import os
import secrets
import socket
import time
from typing import Awaitable, Callable

import psutil
from pymongo.errors import DuplicateKeyError, PyMongoError

log = logging.getLogger(__name__)


class Lease:
    def __init__(self, mongo, *, stale_s: float = 90, beat_s: float = 30) -> None:
        self.mongo = mongo
        self.stale_s, self.beat_s = stale_s, beat_s
        self.me = f"{socket.gethostname()}:{os.getpid()}:{secrets.token_hex(3)}"
        self._task: asyncio.Task | None = None
        self.held = False

    def _coll(self):
        return self.mongo.db["_lease"] if self.mongo is not None and self.mongo.db is not None and self.mongo.healthy else None

    async def acquire(self, max_wait_s: float = 150) -> bool:
        """Take the lease, asking a live holder to hand over; ``True`` also when no Mongo (nothing to lock)."""
        coll = self._coll()
        if coll is None:
            log.warning("no Mongo — single-instance lease skipped")
            return True
        deadline = time.time() + max_wait_s
        asked = False
        while True:
            now = time.time()
            try:
                cur = await coll.find_one({"_id": "main"})
                dead_local = bool(cur and cur.get("host") == socket.gethostname() and cur.get("pid")
                                  and not psutil.pid_exists(int(cur["pid"])))
                flt = {"_id": "main"} if dead_local else                     {"_id": "main", "$or": [{"holder": self.me}, {"heartbeat": {"$lt": now - self.stale_s}}]}
                if dead_local:
                    log.warning("previous instance on this host (pid %s) is gone — taking over its lease", cur.get("pid"))
                doc = await coll.find_one_and_update(
                    flt,
                    {"$set": self._fields(now)}, return_document=True)
                if doc is None:
                    try:
                        await coll.insert_one({"_id": "main", **self._fields(now)})
                        doc = True
                    except DuplicateKeyError:
                        doc = None
                if doc:
                    self.held = True
                    log.info("instance lease acquired (%s)", self.me)
                    return True
                if not asked:
                    await coll.update_one({"_id": "main"}, {"$set": {"handover": self.me}})
                    asked = True
                    log.warning("another instance holds the lease — asked it to hand over")
            except PyMongoError as e:
                log.warning("lease check failed (%r) — continuing without lease", e)
                return True
            if time.time() > deadline:
                return False
            await asyncio.sleep(5)

    def _fields(self, now: float) -> dict:
        return {"holder": self.me, "host": socket.gethostname(), "pid": os.getpid(), "heartbeat": now,
                "started_at": now, "handover": None}

    def start_heartbeat(self, on_lost: Callable[[str], Awaitable[None]]) -> None:
        if self._coll() is None or self._task is not None:
            return
        self._task = asyncio.get_running_loop().create_task(self._beat(on_lost), name="lease-heartbeat")

    async def _beat(self, on_lost) -> None:
        while True:
            await asyncio.sleep(self.beat_s)
            coll = self._coll()
            if coll is None:
                continue
            try:
                doc = await coll.find_one_and_update({"_id": "main", "holder": self.me},
                                                     {"$set": {"heartbeat": time.time()}}, return_document=True)
                if doc is None:
                    self.held = False
                    await on_lost("lease taken by another instance")
                    return
                if doc.get("handover"):
                    await on_lost(f"handover requested by {doc['handover']}")
                    return
            except PyMongoError:
                continue  # Mongo blip: keep running, try again next beat

    async def release(self) -> None:
        if self._task:
            self._task.cancel()
        coll = self._coll()
        if coll is None or not self.held:
            return
        try:
            await coll.delete_one({"_id": "main", "holder": self.me})
        except PyMongoError:
            pass
        self.held = False
