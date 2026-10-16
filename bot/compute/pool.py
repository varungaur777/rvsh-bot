"""The one process pool for CPU work (spec §11.1).

Depends on: stdlib ``concurrent.futures`` / ``multiprocessing``, psutil. Used by: ``compute.Compute``.

Public API
----------
- ``ComputePool(workers, max_tasks_per_child=50, job_timeout_s=60)``
- ``await run(fn, *args, timeout=None)`` — ``fn`` must be a picklable top-level function (paths/bytes in, data
  out). The pool is created **lazily** on the first job (and by ``warm()``).
- ``set_workers(n)`` — live resize: new jobs go to a new pool, running jobs finish in the old one.
- ``recycle()`` — same, without resizing (governor: a worker grew too big / memory critical).
- ``worker_rss_mb() -> list[int]`` · ``stats()`` · ``shutdown()`` · ``warm(fn=None)``
- ``trim_if_idle(idle_s) -> bool`` — no job for ``idle_s`` → the pool is retired (workers exit, RAM returned);
  the caller warms one worker again so the next job starts without the spawn delay.

Workers are spawned on demand (``ProcessPoolExecutor`` only adds a worker when none is idle), so a quiet bot
keeps one warm worker, a busy one grows to ``workers``.

Workers: ``spawn`` start method everywhere (same behaviour on Windows, Linux, Colab), ``max_tasks_per_child``
recycling, thread caps (``OMP_THREAD_LIMIT=1`` …) and lower OS priority, so the bot's event loop always wins.

Exceptions → outcome
--------------------
- ``BrokenProcessPool`` (a worker died) → the pool is recreated and the job retried **once**.
- timeout → every worker of that pool is killed (a stuck worker cannot be interrupted), the pool is recreated
  and ``JobTimeout`` is raised; other jobs of the killed pool see ``BrokenProcessPool`` → retried once.
"""

from __future__ import annotations

import asyncio
import functools
import logging
import multiprocessing
import os
import time
from concurrent.futures import ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool

from bot.core.errors import BotError

log = logging.getLogger(__name__)

THREAD_CAPS = {"OMP_THREAD_LIMIT": "1", "OMP_NUM_THREADS": "1", "OPENBLAS_NUM_THREADS": "1", "MKL_NUM_THREADS": "1"}


class JobTimeout(BotError):
    key = "job_timeout"


def worker_init() -> None:
    """Runs once in every worker: thread caps + lower priority (spec §2.9)."""
    os.environ.update(THREAD_CAPS)
    try:
        import psutil
        p = psutil.Process()
        if os.name == "nt":
            p.nice(psutil.BELOW_NORMAL_PRIORITY_CLASS)
        else:
            p.nice(5)
    except Exception:
        pass


class ComputePool:
    def __init__(self, workers: int, max_tasks_per_child: int = 50, job_timeout_s: float = 60) -> None:
        self.workers = max(1, int(workers))
        self.max_tasks_per_child = max(1, int(max_tasks_per_child))
        self.job_timeout_s = job_timeout_s
        self._ex: ProcessPoolExecutor | None = None
        self.counters = {"jobs": 0, "errors": 0, "timeouts": 0, "restarts": 0, "recycles": 0, "trims": 0}
        self.running = 0
        self.last_used = time.monotonic()
        self._jobs_at_trim = 0

    # ------------------------------------------------------------ lifecycle
    def _ensure(self) -> ProcessPoolExecutor:
        if self._ex is None:
            self._ex = ProcessPoolExecutor(self.workers, mp_context=multiprocessing.get_context("spawn"),
                                           initializer=worker_init, max_tasks_per_child=self.max_tasks_per_child)
            log.info("process pool started: %d worker(s)", self.workers)
        return self._ex

    def _retire(self, kill: bool = False) -> None:
        ex, self._ex = self._ex, None
        if ex is None:
            return
        if kill:
            for proc in list((getattr(ex, "_processes", None) or {}).values()):
                try:
                    proc.kill()
                except Exception:
                    pass
        ex.shutdown(wait=False, cancel_futures=kill)

    def set_workers(self, n: int) -> None:
        n = max(1, int(n))
        if n != self.workers:
            self.workers = n
            self._retire()
            log.info("process pool resized to %d worker(s)", n)

    def recycle(self) -> None:
        if self._ex is not None:
            self.counters["recycles"] += 1
            self._retire()

    def shutdown(self) -> None:
        self._retire(kill=False)

    async def warm(self, fn=None) -> None:
        """Start one worker in the background; ``fn`` (a top-level function) pre-imports what jobs need."""
        await self.run(fn or os.getpid)

    def trim_if_idle(self, idle_s: float) -> bool:
        """Retire a pool that did real work and then sat idle (a fresh warm worker is ≈ 27 MB; used ones grow)."""
        if (self._ex is None or self.running or idle_s <= 0 or time.monotonic() - self.last_used < idle_s
                or self.counters["jobs"] - self._jobs_at_trim <= 1):
            return False
        self.counters["trims"] += 1
        self._jobs_at_trim = self.counters["jobs"]
        self._retire()
        log.info("process pool idle for %ds — workers stopped", int(idle_s))
        return True

    # ------------------------------------------------------------ jobs
    async def run(self, fn, *args, timeout: float | None = None):
        self.running += 1
        try:
            return await self._run(fn, *args, timeout=timeout)
        finally:
            self.running -= 1
            self.last_used = time.monotonic()

    async def _run(self, fn, *args, timeout: float | None = None):
        loop = asyncio.get_running_loop()
        call = functools.partial(fn, *args)
        for attempt in (0, 1):
            ex = self._ensure()
            try:
                fut = loop.run_in_executor(ex, call)
            except (BrokenProcessPool, RuntimeError):  # shut down meanwhile
                self._retire()
                continue
            try:
                result = await asyncio.wait_for(fut, timeout or self.job_timeout_s)
                self.counters["jobs"] += 1
                return result
            except BrokenProcessPool:
                self.counters["restarts"] += 1
                if self._ex is ex:
                    self._retire(kill=True)
                if attempt:
                    raise
                log.warning("process pool broke — recreating, retrying %s once", getattr(fn, "__name__", fn))
            except asyncio.TimeoutError:
                self.counters["timeouts"] += 1
                if self._ex is ex:
                    self._retire(kill=True)  # a stuck worker can only be killed
                raise JobTimeout(detail=f"{getattr(fn, '__name__', fn)} > {timeout or self.job_timeout_s}s")
            except Exception:
                self.counters["errors"] += 1
                raise
        raise BrokenProcessPool("pool could not run the job")

    # ------------------------------------------------------------ introspection
    def worker_pids(self) -> list[int]:
        procs = (getattr(self._ex, "_processes", None) or {}) if self._ex else {}
        return [p.pid for p in procs.values() if p.pid]

    def worker_rss_mb(self) -> list[int]:
        import psutil
        out = []
        for pid in self.worker_pids():
            try:
                out.append(psutil.Process(pid).memory_info().rss // 2**20)
            except Exception:
                pass
        return out

    def stats(self) -> str:
        rss = self.worker_rss_mb()
        state = f"{len(rss)}/{self.workers} up" if self._ex else f"0/{self.workers} (starts on first job)"
        return (f"{state} · RSS {sum(rss)} MB · jobs {self.counters['jobs']} · errors {self.counters['errors']} · "
                f"timeouts {self.counters['timeouts']} · restarts {self.counters['restarts']} · "
                f"recycles {self.counters['recycles']} · idle trims {self.counters['trims']}")
