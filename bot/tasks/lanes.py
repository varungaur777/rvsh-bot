"""Three task lanes with live-resizable concurrency + the task registry (spec §10).

Depends on: ``tasks.context``. Used by: handlers (submit/cancel/position), ``/q``, ``/task``, ``/cq``,
shutdown, settings subscriptions (``queue.*``).

Public API
----------
- ``LANES = ("light", "heavy", "background")``
- ``TaskManager(root, limits)`` · ``start()`` · ``create(user_id, kind, lane, priority, title, chat_id)`` · ``hold(task)`` ·
  ``await submit(task, runner) -> position`` · ``await cancel(task_id, by)`` · ``await cancel_all(by)`` ·
  ``clear_queue(by)`` · ``position(task_id)`` · ``active_for(user_id)`` · ``running()`` · ``queued()`` ·
  ``set_limit(lane, n)`` · ``await shutdown(grace_s)``
- ``runner`` is ``async def runner(task) -> None`` and is executed as ``async with task: await runner(task)``.

Rules
-----
- One active-or-queued task per user (``TaskExists``).
- Queue order: priority (owner 0 · admin 1 · user 2), then FIFO.
- Lowering a lane limit never kills running tasks; it only stops new starts until below the limit.
- A task cancelled while queued is closed immediately (cleanup runs); while running, its asyncio task is
  cancelled and ``TaskContext.__aexit__`` cleans up.
- Errors raised by a runner are passed to ``on_error(task, exc)`` (error report + friendly message) after
  cleanup; they never stop the lane.
"""

from __future__ import annotations

import asyncio
import itertools
import logging
from pathlib import Path
from typing import Any, Awaitable, Callable

from bot.core.errors import BotError
from bot.tasks.context import TaskContext, new_task_id

log = logging.getLogger(__name__)

LANES = ("light", "heavy", "background")
Runner = Callable[[TaskContext], Awaitable[Any]]


class TaskExists(BotError):
    key = "task_exists"


class _Lane:
    def __init__(self, name: str, limit: int) -> None:
        self.name = name
        self.limit = max(1, limit)
        self.queue: list[tuple[int, int, str]] = []  # (priority, seq, task_id) — kept sorted
        self.running: set[str] = set()
        self.cond = asyncio.Condition()


class TaskManager:
    def __init__(self, root: Path, limits: dict[str, int]) -> None:
        self.root = Path(root)
        self.lanes = {name: _Lane(name, limits.get(name, 1)) for name in LANES}
        self.tasks: dict[str, TaskContext] = {}
        self._runners: dict[str, Runner] = {}
        self._jobs: dict[str, asyncio.Task] = {}
        self._by_user: dict[int, str] = {}
        self._seq = itertools.count()
        self._dispatchers: list[asyncio.Task] = []
        self.accepting = True
        self.on_error: Callable[[TaskContext, BaseException], Awaitable[Any]] | None = None
        self.on_start: Callable[[TaskContext], Awaitable[Any]] | None = None
        self.on_close: Callable[[TaskContext], Awaitable[Any]] | None = None
        self.on_position: Callable[[TaskContext, int], Awaitable[Any]] | None = None

    def start(self) -> None:
        loop = asyncio.get_running_loop()
        for lane in self.lanes.values():
            self._dispatchers.append(loop.create_task(self._dispatch(lane), name=f"lane-{lane.name}"))

    # ------------------------------------------------------------ create / submit
    def create(self, user_id: int, kind: str, lane: str, priority: int, *, title: str = "",
               chat_id: int | None = None) -> TaskContext:
        """New ``TaskContext`` with its workdir under ``<root>/tasks/<user>/<task>/`` (not yet queued)."""
        if lane not in self.lanes:
            raise ValueError(f"unknown lane {lane}")
        if user_id in self._by_user:
            raise TaskExists()
        tid = new_task_id()
        task = TaskContext(task_id=tid, user_id=user_id, kind=kind, lane=lane, priority=priority,
                           workdir=self.root / "tasks" / str(user_id) / tid, title=title, chat_id=chat_id)
        task.on_close(self._forget)
        return task

    def hold(self, task: TaskContext) -> None:
        """Register a task that is still interactive (reading files, asking preferences) before it is queued:
        it counts as the user's one task, shows in /q, and /cancel or a timeout closes it (cleanup path)."""
        if task.user_id in self._by_user:
            raise TaskExists()
        task.status = "preparing"
        self.tasks[task.task_id] = task
        self._by_user[task.user_id] = task.task_id

    async def submit(self, task: TaskContext, runner: Runner) -> int:
        """Queue a task; returns its 1-based position in its lane (1 = starts next/now)."""
        if not self.accepting:
            raise BotError("restarting")
        if task.user_id in self._by_user and self._by_user[task.user_id] != task.task_id:
            raise TaskExists()
        lane = self.lanes[task.lane]
        self.tasks[task.task_id] = task
        self._runners[task.task_id] = runner
        self._by_user[task.user_id] = task.task_id
        async with lane.cond:
            lane.queue.append((task.priority, next(self._seq), task.task_id))
            lane.queue.sort()
            lane.cond.notify_all()
        return self.position(task.task_id) or 1

    # ------------------------------------------------------------ queries
    def position(self, task_id: str) -> int | None:
        """1-based queue position, 0 when running, None when unknown."""
        task = self.tasks.get(task_id)
        if task is None:
            return None
        lane = self.lanes[task.lane]
        if task_id in lane.running:
            return 0
        for i, (_, _, tid) in enumerate(lane.queue):
            if tid == task_id:
                return i + 1
        return None

    def active_for(self, user_id: int) -> TaskContext | None:
        tid = self._by_user.get(user_id)
        return self.tasks.get(tid) if tid else None

    def running(self) -> list[TaskContext]:
        return [self.tasks[t] for lane in self.lanes.values() for t in lane.running if t in self.tasks]

    def queued(self) -> list[TaskContext]:
        return [self.tasks[t] for lane in self.lanes.values() for _, _, t in lane.queue if t in self.tasks]

    def held(self) -> list[TaskContext]:
        """Interactive tasks not yet queued."""
        busy = {t for lane in self.lanes.values() for t in lane.running} | {t for lane in self.lanes.values() for *_, t in lane.queue}
        return [t for tid, t in self.tasks.items() if tid not in busy]

    # ------------------------------------------------------------ control
    async def set_limit(self, lane: str, n: int) -> None:
        """Change a lane's concurrency live."""
        ln = self.lanes[lane]
        async with ln.cond:
            ln.limit = max(1, int(n))
            ln.cond.notify_all()

    async def cancel(self, task_id: str, by: str = "user", status: str = "cancelled") -> bool:
        """Cancel a held, queued or running task; ``status`` may be ``timeout``/``restart``/``blocked``."""
        task = self.tasks.get(task_id)
        if task is None or task.closed:
            return False
        lane = self.lanes[task.lane]
        task.ended_by = by
        if status != "cancelled":
            task.status = status
        async with lane.cond:
            before = len(lane.queue)
            lane.queue = [q for q in lane.queue if q[2] != task_id]
            was_queued = len(lane.queue) != before
        if was_queued:
            await task.close(status, ended_by=by)
            return True
        job = self._jobs.get(task_id)
        if job is not None:
            task.cancel_event.set()
            job.cancel()
            return True
        if task_id not in lane.running:  # held (interactive, never queued) — whatever status was just set
            await task.close(status, ended_by=by)
            return True
        return False

    async def cancel_all(self, by: str, status: str = "cancelled") -> int:
        ids = [t.task_id for t in self.held() + self.queued() + self.running()]
        for tid in ids:
            await self.cancel(tid, by, status=status)
        return len(ids)

    async def clear_queue(self, by: str) -> int:
        ids = [t.task_id for t in self.queued()]
        for tid in ids:
            await self.cancel(tid, by)
        return len(ids)

    async def shutdown(self, grace_s: float) -> None:
        """Stop intake, let running tasks finish for ``grace_s``, then cancel the rest (status ``restart``)."""
        self.accepting = False
        for tid in [t.task_id for t in self.queued() + self.held()]:
            await self.cancel(tid, "system: restart", status="restart")
        jobs = list(self._jobs.values())
        if jobs:
            done, pending = await asyncio.wait(jobs, timeout=grace_s)
            for tid, job in list(self._jobs.items()):
                if job in pending:
                    await self.cancel(tid, "system: restart", status="restart")
            if pending:
                await asyncio.wait(pending, timeout=10)
        for d in self._dispatchers:
            d.cancel()

    # ------------------------------------------------------------ internals
    async def _dispatch(self, lane: _Lane) -> None:
        while True:
            async with lane.cond:
                await lane.cond.wait_for(lambda: lane.queue and len(lane.running) < lane.limit)
                _, _, tid = lane.queue.pop(0)
                task = self.tasks.get(tid)
                if task is None or task.closed:
                    continue
                lane.running.add(tid)
            self._jobs[tid] = asyncio.get_running_loop().create_task(self._run(lane, task), name=f"task-{tid}")
            for i, (_, _, waiting) in enumerate(list(lane.queue)):
                if self.on_position and waiting in self.tasks:
                    asyncio.get_running_loop().create_task(_quiet(self.on_position(self.tasks[waiting], i + 1)))

    async def _run(self, lane: _Lane, task: TaskContext) -> None:
        runner = self._runners.pop(task.task_id)
        try:
            if self.on_start:
                await _quiet(self.on_start(task))
            async with task:
                try:
                    await runner(task)
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    # report inside the context: error id + user message exist before cleanup writes the task log
                    if isinstance(e, BotError) and e.key == "cancelled":
                        raise
                    if self.on_error:
                        await _quiet(self.on_error(task, e))
                    else:
                        log.exception("task %s failed", task.task_id)
                    raise
        except (asyncio.CancelledError, Exception):
            pass  # already reported above; cleanup done by TaskContext.__aexit__
        finally:
            self._jobs.pop(task.task_id, None)
            async with lane.cond:
                lane.running.discard(task.task_id)
                lane.cond.notify_all()

    async def _forget(self, task: TaskContext) -> None:
        if self._by_user.get(task.user_id) == task.task_id:
            del self._by_user[task.user_id]
        self.tasks.pop(task.task_id, None)
        self._runners.pop(task.task_id, None)
        if self.on_close:
            await _quiet(self.on_close(task))


async def _quiet(coro) -> None:
    try:
        await coro
    except Exception:
        log.exception("task hook failed")
