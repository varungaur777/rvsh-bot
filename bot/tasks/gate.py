"""Fair limited-concurrency gate with visible queue positions (owner 2026-10-12: "queue lgaya hai ya nhi?").

The task lanes (``tasks.lanes``) queue the *outputs* (Marks, PDFs…). Work that happens while the user is still in
the interactive flow also needs a queue, or a crowd would run it all at once:
- ``prepare`` gate — after **Done**: download the files + parse the sheets (``queue.prepare_concurrent``, auto),
- ``fetch`` gate — opening pasted links (``fetch.parallel_links``, 3): exam servers are slow and easily overloaded.

Order: priority (owner 0 · admin 1 · user 2), then arrival. A waiting caller gets ``on_wait(position)`` when it
starts waiting and whenever its position changes (the handler shows "⏳ You are #3 in the queue").

Depends on: stdlib. Used by: ``handlers/exam.py`` (``app.gates``), ``/stats``.

Public API
----------
- ``Gate(name, limit)`` · ``async with gate.slot(priority=2, on_wait=None):`` · ``set_limit(n)`` (live) ·
  ``active`` · ``waiting`` · ``stats()``
- ``Gates(prepare=Gate, fetch=Gate)`` — the app's gates.

Cancellation while waiting removes the caller from the queue; a slot is always released (also on errors).
"""

from __future__ import annotations

import asyncio
import heapq
import itertools
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Callable


class Gate:
    def __init__(self, name: str, limit: int) -> None:
        self.name = name
        self.limit = max(1, int(limit))
        self.active = 0
        self._q: list[list] = []  # [priority, seq, future, on_wait, last_position]
        self._seq = itertools.count()
        self.served = 0
        self.max_wait_s = 0.0

    @property
    def waiting(self) -> int:
        return sum(1 for e in self._q if not e[2].done())

    @asynccontextmanager
    async def slot(self, priority: int = 2, on_wait: Callable[[int], object] | None = None):
        if self.active < self.limit and not self._q:
            self.active += 1
        else:
            loop = asyncio.get_running_loop()
            entry = [priority, next(self._seq), loop.create_future(), on_wait, 0]
            heapq.heappush(self._q, entry)
            started = loop.time()
            self._report()
            try:
                await entry[2]
            except asyncio.CancelledError:
                if entry[2].done() and not entry[2].cancelled():
                    self._release()  # the slot was granted just as we were cancelled
                else:
                    self._q.remove(entry)
                    heapq.heapify(self._q)
                    self._report()
                raise
            self.max_wait_s = max(self.max_wait_s, loop.time() - started)
        try:
            yield
        finally:
            self.served += 1
            self._release()

    def _release(self) -> None:
        self.active -= 1
        self._wake()

    def _wake(self) -> None:
        while self._q and self.active < self.limit:
            entry = heapq.heappop(self._q)
            if entry[2].done():
                continue
            self.active += 1
            entry[2].set_result(None)
        self._report()

    def _report(self) -> None:
        """Tell every waiting caller its (new) 1-based position."""
        for pos, entry in enumerate(sorted(e for e in self._q if not e[2].done()), 1):
            if entry[3] is not None and entry[4] != pos:
                entry[4] = pos
                try:
                    entry[3](pos)
                except Exception:  # a UI callback must never break the queue
                    pass

    def set_limit(self, n: int) -> None:
        self.limit = max(1, int(n))
        self._wake()

    def stats(self) -> str:
        return f"{self.name} {self.active}/{self.limit} busy, {self.waiting} waiting (max wait {self.max_wait_s:.0f}s)"


@dataclass
class Gates:
    prepare: Gate
    fetch: Gate
