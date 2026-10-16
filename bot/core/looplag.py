"""Event-loop lag monitor (spec §1 target: lag < 100 ms under load).

Depends on: stdlib. Used by: ``bot.app`` (start), ``/stats``, startup report.

Public API: ``LoopLag(interval_s=0.5)`` · ``start()`` · ``stop()`` · ``last_ms`` · ``max_ms`` (peak over the last
minute) · ``avg_ms`` (exponential moving average).
"""

from __future__ import annotations

import asyncio
import time
from collections import deque


class LoopLag:
    def __init__(self, interval_s: float = 0.5) -> None:
        self.interval_s = interval_s
        self.last_ms = 0.0
        self.avg_ms = 0.0
        self._window: deque[tuple[float, float]] = deque()
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.get_running_loop().create_task(self._run(), name="loop-lag")

    def stop(self) -> None:
        if self._task:
            self._task.cancel()

    @property
    def max_ms(self) -> float:
        return max((v for _, v in self._window), default=0.0)

    async def _run(self) -> None:
        while True:
            t = time.perf_counter()
            await asyncio.sleep(self.interval_s)
            lag = max(0.0, (time.perf_counter() - t - self.interval_s) * 1000)
            now = time.monotonic()
            self.last_ms = lag
            self.avg_ms = lag if not self.avg_ms else 0.9 * self.avg_ms + 0.1 * lag
            self._window.append((now, lag))
            while self._window and now - self._window[0][0] > 60:
                self._window.popleft()
