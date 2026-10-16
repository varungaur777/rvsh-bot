"""Durable journal of Mongo writes made while Mongo was unreachable (spec §9.2).

Depends on: orjson. Used by: ``db/repo.py``.

Format: ``_db/_outbox/outbox.jsonl``, one op per line:
``{"ts": "...", "coll": "...", "id": ..., "op": "replace|delete", "doc": {...}}``.
Updates made during an outage are journaled as the **resulting full document** (``replace``), never as
operators: a conditional update replayed later could otherwise give a different result than the one the
JSON mirror already holds. Replay sends only the **last** op per document (in order of last occurrence),
so a document changed 100 times offline costs one Mongo write.

Exceptions → outcome: a replay error stops the replay and keeps the remaining ops for the next attempt.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from pathlib import Path

import orjson

log = logging.getLogger(__name__)


class Outbox:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._lock = asyncio.Lock()

    async def append(self, op: dict) -> None:
        op = {"ts": datetime.now(timezone.utc).isoformat(), **op}
        line = orjson.dumps(op, option=orjson.OPT_NON_STR_KEYS) + b"\n"
        async with self._lock:
            await asyncio.to_thread(self._append_sync, line)

    def _append_sync(self, line: bytes) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, "ab") as f:
            f.write(line)

    def size(self) -> int:
        try:
            return sum(1 for _ in open(self.path, "rb"))
        except FileNotFoundError:
            return 0

    async def replay(self, mongo) -> int:
        """Apply queued ops to ``mongo`` in order; returns how many were applied."""
        async with self._lock:
            try:
                lines = await asyncio.to_thread(self.path.read_bytes)
            except FileNotFoundError:
                return 0
            latest: dict[str, dict] = {}
            for x in lines.splitlines():
                if x.strip():
                    op = orjson.loads(x)
                    key = f'{op["coll"]}|{op["id"]!r}'
                    latest.pop(key, None)  # re-insert → ordered by last occurrence
                    latest[key] = op
            ops = list(latest.values())
            done = 0
            try:
                for op in ops:
                    if op["op"] == "replace":
                        await mongo.replace(op["coll"], op["id"], op["doc"])
                    elif op["op"] == "delete":
                        await mongo.delete(op["coll"], op["id"])
                    done += 1
            finally:
                rest = ops[done:]
                data = b"".join(orjson.dumps(o, option=orjson.OPT_NON_STR_KEYS) + b"\n" for o in rest)
                await asyncio.to_thread(self.path.write_bytes, data)
                if rest:
                    log.warning("outbox replay stopped, %d ops kept", len(rest))
            return done
