"""Event-loop lag monitor (spec §1 target: lag < 100 ms under load).

Depends on: stdlib. Used by: ``bot.app`` (start), ``/stats``, startup report.

Public API: ``LoopLag(interval_s=0.5)`` · ``start()`` · ``stop()`` · ``last_ms`` · ``max_ms`` (peak over the last
minute) · ``avg_ms`` (exponential moving average) · ``stalls`` (count).

**Watchdog** (2026-11-05, replies 15–40 s late with no clue why): a daemon thread notices when the loop has not
ticked for ``STALL_S``; it logs **where** the loop thread is stuck (its Python stack, once per stall) and, when the
loop is back, how long it was stuck. A thread, so it still works while the loop itself is blocked.
"""

from __future__ import annotations

import asyncio
import logging
import sys
import threading
import time
import traceback
from collections import deque

log = logging.getLogger(__name__)
STALL_S = 3.0


class LoopLag:
    def __init__(self, interval_s: float = 0.5) -> None:
        self.interval_s = interval_s
        self.last_ms = 0.0
        self.avg_ms = 0.0
        self._window: deque[tuple[float, float]] = deque()
        self._task: asyncio.Task | None = None
        self._tick = time.monotonic()
        self._loop_thread: int | None = None
        self._stop = threading.Event()
        self.stalls = 0

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.get_running_loop().create_task(self._run(), name="loop-lag")
            self._loop_thread = threading.get_ident()
            self._tick = time.monotonic()
            self._stop.clear()
            threading.Thread(target=self._watch, name="loop-watchdog", daemon=True).start()

    def stop(self) -> None:
        if self._task:
            self._task.cancel()
        self._stop.set()

    def _watch(self) -> None:
        stuck_since = None
        while not self._stop.wait(1.0):
            idle = time.monotonic() - self._tick
            if idle >= STALL_S and stuck_since is None:
                stuck_since = self._tick
                self.stalls += 1
                frame = sys._current_frames().get(self._loop_thread)
                where = "".join(traceback.format_stack(frame, limit=12)) if frame is not None else "(no stack)"
                log.warning("event loop blocked for %.1f s so far — the loop thread is here:\n%s", idle, where)
            elif idle < STALL_S and stuck_since is not None:
                log.warning("event loop was blocked for %.1f s", self._tick - stuck_since)
                stuck_since = None

    @property
    def max_ms(self) -> float:
        return max((v for _, v in self._window), default=0.0)

    async def _run(self) -> None:
        while True:
            t = time.perf_counter()
            await asyncio.sleep(self.interval_s)
            lag = max(0.0, (time.perf_counter() - t - self.interval_s) * 1000)
            now = self._tick = time.monotonic()
            self.last_ms = lag
            self.avg_ms = lag if not self.avg_ms else 0.9 * self.avg_ms + 0.1 * lag
            self._window.append((now, lag))
            while self._window and now - self._window[0][0] > 60:
                self._window.popleft()
