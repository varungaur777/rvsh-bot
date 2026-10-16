"""Fair, work-conserving CPU slots (spec §11.2).

Each job kind has a number of slots (``parse``, ``card``, ``render``, ``ocr``, ``merge``…). A job takes a slot of
its kind for its **owner** (the task id):

- **work-conserving** — a free slot is handed out at once; no fixed waits;
- **fair** — when slots are short, the next slot goes to the owner that currently holds the fewest slots of that
  kind (ties → the one waiting longest). 2 active users → 1 slot each in parallel; 1 user → all slots
  (owner's original OCR rule, generalised to N slots);
- the governor can **cap** a kind (``set_cap``: throttled → 1, critical → 0 = paused); ``None`` removes the cap.

Depends on: stdlib. Used by: ``compute.Compute``.

Public API: ``Scheduler(limits)`` · ``async with slot(kind, owner):`` · ``set_limit(kind, n)`` · ``set_cap(kind, n)``
· ``stats()``.
"""

from __future__ import annotations

import asyncio
import itertools
from collections import deque
from contextlib import asynccontextmanager


class _Kind:
    def __init__(self, limit: int) -> None:
        self.limit = max(1, int(limit))
        self.cap: int | None = None
        self.running: dict[str, int] = {}
        self.waiting: dict[str, deque] = {}  # owner → futures (FIFO per owner)
        self.since: dict[str, int] = {}  # owner → arrival order of its oldest waiting job

    @property
    def effective(self) -> int:
        return self.limit if self.cap is None else min(self.limit, self.cap)

    @property
    def busy(self) -> int:
        return sum(self.running.values())

    def n_waiting(self) -> int:
        return sum(1 for q in self.waiting.values() for f in q if not f.done())


class Scheduler:
    def __init__(self, limits: dict[str, int]) -> None:
        self.kinds = {k: _Kind(n) for k, n in limits.items()}
        self._order = itertools.count()

    def _kind(self, kind: str) -> _Kind:
        k = self.kinds.get(kind)
        if k is None:
            k = self.kinds[kind] = _Kind(1)
        return k

    @asynccontextmanager
    async def slot(self, kind: str, owner: str):
        k = self._kind(kind)
        if k.busy < k.effective and not k.n_waiting():
            k.running[owner] = k.running.get(owner, 0) + 1
        else:
            fut = asyncio.get_running_loop().create_future()
            q = k.waiting.setdefault(owner, deque())
            if not q:
                k.since[owner] = next(self._order)
            q.append(fut)
            try:
                await fut
            except asyncio.CancelledError:
                if fut.done() and not fut.cancelled():
                    self._release(k, owner)  # granted just as we were cancelled
                else:
                    self._drop(k, owner, fut)
                raise
        try:
            yield
        finally:
            self._release(k, owner)

    def _drop(self, k: _Kind, owner: str, fut) -> None:
        q = k.waiting.get(owner)
        if q and fut in q:
            q.remove(fut)
        if q is not None and not q:
            k.waiting.pop(owner, None)
            k.since.pop(owner, None)

    def _release(self, k: _Kind, owner: str) -> None:
        k.running[owner] -= 1
        if not k.running[owner]:
            del k.running[owner]
        self._dispatch(k)

    def _dispatch(self, k: _Kind) -> None:
        while k.busy < k.effective:
            owners = [o for o, q in k.waiting.items() if any(not f.done() for f in q)]
            if not owners:
                return
            owner = min(owners, key=lambda o: (k.running.get(o, 0), k.since.get(o, 0)))
            q = k.waiting[owner]
            while q and q[0].done():
                q.popleft()
            fut = q.popleft()
            if q:
                k.since[owner] = next(self._order)  # its next job queues behind the others (round-robin)
            else:
                k.waiting.pop(owner, None)
                k.since.pop(owner, None)
            k.running[owner] = k.running.get(owner, 0) + 1
            fut.set_result(None)

    def set_limit(self, kind: str, n: int) -> None:
        k = self._kind(kind)
        k.limit = max(1, int(n))
        self._dispatch(k)

    def set_cap(self, kind: str, cap: int | None) -> None:
        k = self._kind(kind)
        k.cap = None if cap is None else max(0, int(cap))
        self._dispatch(k)

    def active(self) -> bool:
        """Any slot taken or any job waiting (the governor samples faster then)."""
        return any(k.busy or k.n_waiting() for k in self.kinds.values())

    def stats(self) -> str:
        return " · ".join(f"{name} {k.busy}/{k.effective}" + (f" (+{k.n_waiting()} waiting)" if k.n_waiting() else "")
                          + (" ⏸" if k.cap == 0 else "") for name, k in self.kinds.items())
