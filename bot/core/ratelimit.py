"""Priority token buckets (spec §6.8).

Depends on: stdlib. Used by: ``core/sender.py``, ``uploader/workers.py`` (P5).

Public API
----------
- ``TokenBucket(rate_per_s, burst)`` · ``await acquire(priority)`` · ``pause(seconds)`` · ``charge(n)`` · ``idle_for`` ·
  ``available()`` (tokens now; negative while paused)
  Lower ``priority`` numbers are served first; equal priorities are FIFO. Cancelled waiters are skipped.
"""

from __future__ import annotations

import asyncio
import heapq
import itertools
import time


class TokenBucket:
    __slots__ = ("rate", "burst", "tokens", "last", "_waiters", "_timer", "_seq", "last_used")

    def __init__(self, rate_per_s: float, burst: float) -> None:
        self.rate = rate_per_s
        self.burst = burst
        self.tokens = burst
        self.last = time.monotonic()
        self.last_used = self.last
        self._waiters: list[tuple[int, int, asyncio.Future]] = []
        self._timer: asyncio.TimerHandle | None = None
        self._seq = itertools.count()

    def _refill(self) -> None:
        now = time.monotonic()
        self.tokens = min(self.burst, self.tokens + (now - self.last) * self.rate)
        self.last = now

    async def acquire(self, priority: int = 2) -> None:
        """Wait for one token. Higher-priority (lower number) waiters are served first."""
        self._refill()
        self.last_used = time.monotonic()
        if not self._waiters and self.tokens >= 1:
            self.tokens -= 1
            return
        fut = asyncio.get_running_loop().create_future()
        heapq.heappush(self._waiters, (priority, next(self._seq), fut))
        self._schedule()
        await fut

    def pause(self, seconds: float) -> None:
        """Telegram asked us to wait (FloodWait): no token is handed out for ``seconds``."""
        self._refill()
        self.tokens = min(self.tokens, 0) - seconds * self.rate
        if self._timer:
            self._timer.cancel()
            self._timer = None
        self._schedule()

    def charge(self, n: float) -> None:
        """``n`` more messages went out than tokens were taken (an album): they are owed — later waiters wait."""
        self._refill()
        self.tokens -= n

    def available(self) -> float:
        """Tokens right now (negative while a flood-wait pause lasts) — the uploader pool's "who posts soonest"."""
        self._refill()
        return self.tokens

    @property
    def idle_for(self) -> float:
        return time.monotonic() - self.last_used if not self._waiters else 0.0

    @property
    def waiting(self) -> int:
        return sum(1 for *_, f in self._waiters if not f.done())

    def _schedule(self) -> None:
        if self._timer is not None or not self._waiters:
            return
        delay = max(0.0, (1 - self.tokens) / self.rate) if self.tokens < 1 else 0.0
        self._timer = asyncio.get_running_loop().call_later(delay, self._wake)

    def _wake(self) -> None:
        self._timer = None
        self._refill()
        while self._waiters and self.tokens >= 1:
            _, _, fut = heapq.heappop(self._waiters)
            if fut.done():  # cancelled waiter
                continue
            self.tokens -= 1
            fut.set_result(None)
        while self._waiters and self._waiters[0][2].done():
            heapq.heappop(self._waiters)
        self._schedule()
