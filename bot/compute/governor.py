"""Resource governor (spec §11.3) — throttles **heavy** CPU work when RAM/CPU run short; light work never waits.

States
------
| State     | Trigger                                                     | Action (heavy kinds only)                         |
|-----------|-------------------------------------------------------------|---------------------------------------------------|
| normal    | below soft limits                                           | configured slots (cap stepped back up 1 per tick) |
| throttled | free RAM < ``free_soft_mb`` or CPU > ``cpu_soft_pct`` for 5 s | cap → 1                                          |
| critical  | free RAM < ``free_hard_mb`` (or disk < ``disk_min_free_mb``)  | cap → 0 (paused), gc + malloc_trim, recycle pool |
|           | free RAM back above the hard limit                          | → throttled (cap 1) until calm                    |
| recover   | free RAM > 1.25 × soft and CPU < soft − 10 for 15 s          | back to normal                                    |

Soft/hard RAM limits shrink automatically on small hosts (< 4 GB: 25 % / 10 % of RAM). Sampling: RAM + CPU every
2 s while CPU work runs or the state is not normal, every 10 s when idle (average of the last 3 samples); every 10th
sample worker RSS (> ``worker_rss_limit_mb`` → pool recycled after the current jobs) and free disk. A state held
for > 2 min (not normal) → one dump alert. Cost (measured): one sample ≈ 0.25 ms CPU, ~2 KB heap, no growth —
taken inline on the loop (cheaper than a thread hop).

**Admission control:** ``async with admit(est_mb)`` — a heavy job whose memory estimate does not fit into
free RAM − already admitted estimates − hard limit waits ("Waiting for resources…").

Depends on: ``core.runtime`` (cgroup-aware free RAM), psutil. Used by: ``compute.Compute``.

Public API: ``Governor(settings, runtime, on_change, alert=None, read=None, …, busy=None, on_tick=None)`` ·
``evaluate(free_mb, cpu_pct, now)`` (pure state step — unit-tested) · ``interval()`` · ``start()`` / ``stop()`` ·
``admit(est_mb)`` · ``state`` · ``cap`` · ``stats()``.
"""

from __future__ import annotations

import asyncio
import ctypes
import gc
import logging
import time
from contextlib import asynccontextmanager
from typing import Callable

log = logging.getLogger(__name__)

NORMAL, THROTTLED, CRITICAL = "normal", "throttled", "critical"


class Governor:
    def __init__(self, settings, runtime=None, on_change: Callable[[str, int | None], None] | None = None,
                 alert: Callable[[str], None] | None = None, read: Callable[[], tuple[int, float]] | None = None,
                 on_recycle: Callable[[], None] | None = None, worker_rss: Callable[[], list[int]] | None = None,
                 disk_free_mb: Callable[[], int] | None = None, busy: Callable[[], bool] | None = None,
                 on_tick: Callable[[], None] | None = None) -> None:
        self.settings, self.runtime = settings, runtime
        self.on_change, self.alert, self._read = on_change, alert, read
        self.on_recycle, self.worker_rss, self.disk_free_mb = on_recycle, worker_rss, disk_free_mb
        self.busy, self.on_tick = busy, on_tick
        self.samples = 0
        self.state = NORMAL
        self.cap: int | None = None  # heavy-kind cap: None = configured slots
        self.since = time.monotonic()
        self._cpu: list[float] = []
        self._cpu_hot_since: float | None = None
        self._calm_since: float | None = None
        self._alerted = False
        self.reserved_mb = 0
        self.free_mb = 0
        self._cond = asyncio.Condition()
        self._task: asyncio.Task | None = None
        self.disk_low = False

    # ------------------------------------------------------------ limits
    def limits(self) -> tuple[int, int]:
        soft, hard = int(self.settings.get("governor.free_soft_mb")), int(self.settings.get("governor.free_hard_mb"))
        total = getattr(self.runtime, "ram_total_mb", 0) or 0
        if total and total < 4096:
            soft, hard = min(soft, total * 25 // 100), min(hard, total * 10 // 100)
        return soft, hard

    # ------------------------------------------------------------ pure step
    def evaluate(self, free_mb: int, cpu_pct: float, now: float | None = None) -> str:
        """One sampling step → new state (and cap). Pure apart from ``self``; unit tests feed fake readings."""
        now = time.monotonic() if now is None else now
        self.free_mb = free_mb
        soft, hard = self.limits()
        cpu_soft = float(self.settings.get("governor.cpu_soft_pct"))
        self._cpu = (self._cpu + [cpu_pct])[-3:]
        cpu = sum(self._cpu) / len(self._cpu)
        if cpu > cpu_soft:
            self._cpu_hot_since = self._cpu_hot_since if self._cpu_hot_since is not None else now
        else:
            self._cpu_hot_since = None
        cpu_hot = self._cpu_hot_since is not None and now - self._cpu_hot_since >= 5
        calm = free_mb > 1.25 * soft and cpu < cpu_soft - 10
        self._calm_since = (self._calm_since if self._calm_since is not None else now) if calm else None

        new = self.state
        if free_mb < hard or self.disk_low:
            new = CRITICAL
        elif free_mb < soft or cpu_hot:
            new = THROTTLED
        elif self.state != NORMAL and self._calm_since is not None and now - self._calm_since >= 15:
            new = NORMAL
        elif self.state == CRITICAL:  # above the hard limit again: one heavy job at a time until calm
            new = THROTTLED
        self._apply(new, now)
        return self.state

    def _apply(self, new: str, now: float) -> None:
        cap = self.cap
        if new == CRITICAL:
            cap = 0
        elif new == THROTTLED:
            cap = 1
        elif self.cap is not None:  # normal again: step back up one slot per tick
            cap = self.cap + 1
            if cap >= 8:
                cap = None
        changed_state = new != self.state
        if changed_state:
            log.warning("governor: %s → %s (free RAM %d MB)", self.state, new, self.free_mb)
            self.state, self.since, self._alerted = new, now, False
            if new == CRITICAL:
                self._relieve()
        if cap != self.cap or changed_state:
            self.cap = cap
            if self.on_change:
                self.on_change(self.state, self.cap)
        if self.state != NORMAL and not self._alerted and now - self.since > 120 and self.alert:
            self._alerted = True
            self.alert(f"⚠️ <b>Governor</b>: {self.state} for {int(now - self.since)} s · free RAM {self.free_mb} MB")

    def _relieve(self) -> None:
        gc.collect()
        try:  # give freed heap back to the OS (glibc only)
            ctypes.CDLL("libc.so.6").malloc_trim(0)
        except Exception:
            pass
        if self.on_recycle:
            self.on_recycle()

    # ------------------------------------------------------------ admission
    @asynccontextmanager
    async def admit(self, est_mb: int):
        """Heavy job admission by memory estimate; waits while it does not fit (never deadlocks: a job alone is
        always admitted when nothing else is reserved)."""
        est = max(0, int(est_mb))
        _soft, hard = self.limits()
        async with self._cond:
            await self._cond.wait_for(lambda: self.reserved_mb == 0 or self.free_mb - self.reserved_mb - est >= hard)
            self.reserved_mb += est
        try:
            yield
        finally:
            async with self._cond:
                self.reserved_mb -= est
                self._cond.notify_all()

    # ------------------------------------------------------------ sampling loop
    def _sample(self) -> tuple[int, float]:
        if self._read:
            return self._read()
        import psutil

        from bot.core.runtime import available_ram_mb
        return available_ram_mb(self.runtime), psutil.cpu_percent(interval=None)

    def interval(self) -> float:
        """2 s while CPU work runs / admissions wait / the state is not normal; 10 s when idle."""
        busy = (self.busy() if self.busy else True) or self.reserved_mb or self.state != NORMAL
        return 2.0 if busy else 10.0

    async def _loop(self) -> None:
        tick = 0
        while True:
            try:
                free, cpu = self._sample()  # ≈ 0.25 ms: inline, a thread hop would cost more
                self.samples += 1
                if tick % 10 == 0:  # worker RSS + disk
                    limit = int(self.settings.get("governor.worker_rss_limit_mb"))
                    if self.worker_rss and any(r > limit for r in self.worker_rss()) and self.on_recycle:
                        log.info("governor: a worker passed %d MB — recycling the pool after current jobs", limit)
                        self.on_recycle()
                    if self.disk_free_mb:
                        self.disk_low = self.disk_free_mb() < int(self.settings.get("governor.disk_min_free_mb"))
                self.evaluate(free, cpu)
                async with self._cond:
                    self._cond.notify_all()  # free RAM changed: admission may proceed
                if self.on_tick:
                    self.on_tick()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # the governor must never die
                log.warning("governor sample failed: %r", e)
            tick += 1
            await asyncio.sleep(self.interval())

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.get_running_loop().create_task(self._loop(), name="governor")

    def stop(self) -> None:
        if self._task:
            self._task.cancel()
            self._task = None

    def stats(self) -> str:
        soft, hard = self.limits()
        cap = "configured" if self.cap is None else ("paused" if self.cap == 0 else f"cap {self.cap}")
        return (f"{self.state} ({cap}) · free RAM {self.free_mb} MB (soft {soft} / hard {hard}) · "
                f"admitted {self.reserved_mb} MB · {self.samples} samples, every {self.interval():g} s"
                + (" · ⚠️ disk low" if self.disk_low else ""))
