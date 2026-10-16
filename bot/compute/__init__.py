"""Compute layer (spec §11): one process pool + fair slots + resource governor behind one call.

``await app.compute.run(kind, owner, fn, *args, est_mb=0, timeout=None)``
1. takes a slot of ``kind`` for ``owner`` (the task id) — fair across users, work-conserving (``scheduler``);
2. heavy kinds pass governor admission with their memory estimate (``governor``);
3. runs ``fn(*args)`` in the process pool (``pool``) — recreated on crashes, killed on timeouts (``governor.job_timeout_s``;
   heavy kinds ``HEAVY_TIMEOUT_X`` times that, unless the caller passes ``timeout``).

Kinds (slots):
- light — ``parse`` (sheets → data; ``pool.workers`` slots), ``card`` (scorecards / quiz cards; ``render.slots``):
  never throttled (the light lane and quizzes must never wait on the governor, spec §11.3);
- heavy — ``render`` (PDFs; ``render.slots``), ``ocr`` (``ocr.image_slots``), ``merge`` (``merge.slots``): capped by the
  governor (throttled → 1, critical → paused).

Idle: the governor samples every 10 s instead of 2 s, and a pool idle for ``pool.idle_trim_s`` (300 s) is retired
and replaced by one freshly warmed worker (RAM of grown workers returned to the OS).

``run_cpu(app, kind, owner, fn, *args)`` — the same, or ``asyncio.to_thread`` when the app has no compute layer
(unit-test fakes, tools).

Depends on: ``compute.pool``, ``compute.scheduler``, ``compute.governor``, ``core.settings``.
Used by: exam pipeline (parse + scorecard), input naming; later PDF/OCR/quiz renderers.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import shutil

from bot.compute.governor import Governor
from bot.compute.pool import ComputePool, JobTimeout
from bot.compute.scheduler import Scheduler

log = logging.getLogger(__name__)

LIGHT = ("parse", "card")
HEAVY = ("render", "ocr", "merge")
HEAVY_TIMEOUT_X = 5  # heavy jobs may run 5 × governor.job_timeout_s (60 s → 5 min; B-028)

__all__ = ["Compute", "JobTimeout", "run_cpu"]


class Compute:
    def __init__(self, settings, runtime=None, root=None, alert=None) -> None:
        self.settings = settings
        workers = settings.workers()
        self.pool = ComputePool(workers, settings.get("pool.max_tasks_per_child"), settings.get("governor.job_timeout_s"))
        self.scheduler = Scheduler(self._limits())
        self.governor = Governor(settings, runtime, on_change=self._governed, alert=alert, on_recycle=self.pool.recycle,
                                 worker_rss=self.pool.worker_rss_mb,
                                 disk_free_mb=(lambda: shutil.disk_usage(str(root)).free // 2**20) if root else None,
                                 busy=self.busy, on_tick=self._tick)
        self._warming: asyncio.Task | None = None

    def _limits(self) -> dict[str, int]:
        v = self.settings.value
        return {"parse": self.settings.workers(), "card": v("render.slots"), "render": v("render.slots"),
                "ocr": v("ocr.image_slots"), "merge": int(self.settings.get("merge.slots"))}

    def _governed(self, state: str, cap: int | None) -> None:
        for kind in HEAVY:
            self.scheduler.set_cap(kind, cap)

    def reload(self) -> None:
        """Settings changed (``pool.*``, ``render.slots``, ``ocr.*``, ``merge.slots``): resize live."""
        self.pool.set_workers(self.settings.workers())
        for kind, n in self._limits().items():
            self.scheduler.set_limit(kind, n)

    def start(self) -> None:
        self.governor.start()

    def stop(self) -> None:
        self.governor.stop()
        self.pool.shutdown()

    async def run(self, kind: str, owner: str, fn, *args, est_mb: int = 0, timeout: float | None = None):
        if timeout is None and kind in HEAVY:  # a PDF / bundle of a big exam on a slow host needs more than a sheet parse
            timeout = HEAVY_TIMEOUT_X * float(self.settings.get("governor.job_timeout_s") or 60)
        async with self.scheduler.slot(kind, owner):
            admit = self.governor.admit(est_mb) if kind in HEAVY and est_mb else contextlib.nullcontext()
            async with admit:
                return await self.pool.run(fn, *args, timeout=timeout)

    def busy(self) -> bool:
        return bool(self.pool.running or self.scheduler.active())

    def warm(self) -> None:
        """Start one worker in the background with the job modules imported (≈ 27 MB, ≈ 0.3 s)."""
        if self._warming is None or self._warming.done():
            self._warming = asyncio.get_running_loop().create_task(self._warm())

    async def _warm(self) -> None:
        from bot.exam.jobs import preload
        try:
            await self.pool.warm(preload)
        except Exception as e:
            log.warning("pool warm-up failed: %r", e)

    def _tick(self) -> None:
        """Governor tick: a pool idle for ``pool.idle_trim_s`` gives its RAM back, then one fresh worker is warmed."""
        if self.pool.trim_if_idle(float(self.settings.get("pool.idle_trim_s") or 0)) and self.settings.get("pool.warm_up"):
            self.warm()

    def stats(self) -> str:
        return (f"🧮 Pool: {self.pool.stats()}\n🎛 Slots: {self.scheduler.stats()}\n"
                f"🛡 Governor: {self.governor.stats()}")


async def run_cpu(app, kind: str, owner: str, fn, *args, **kw):
    """CPU work through ``app.compute`` when present, else a worker thread (fakes / tools)."""
    compute = getattr(app, "compute", None)
    if compute is None:
        return await asyncio.to_thread(fn, *args)
    return await compute.run(kind, owner, fn, *args, **kw)
