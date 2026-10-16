"""``TaskContext`` — one user job and **the single cleanup path** (spec §10).

Depends on: ``tasks.session``. Used by: ``tasks.lanes`` and every task runner.

Public API
----------
- ``TaskContext(task_id, user_id, kind, lane, priority, workdir, title)`` — fields: status, stage, created/
  started/ended, ``ended_by``, ``error_id``, ``outputs``, ``cancel_event``.
- ``await task.prepare()`` — workdir for a held (interactive) task.
- ``async with task:`` — marks running; on exit (complete / error / cancellation) calls ``close()``.
- ``await task.close(status, ended_by=None, error_id=None)`` — idempotent cleanup: cancel child jobs, delete
  the workdir, clear the user's session, run registered ``on_close`` hooks (registry removal, task log,
  notifications). **No other code deletes task workdirs.**
- ``task.spawn(coro)`` — child job cancelled with the task. ``task.keep(coro)`` — job that must finish (input
  dump uploads): not cancelled; the workdir is deleted after it ends (≤ ``KEEP_TIMEOUT_S``), while the task
  itself closes at once. ``task.check()`` raises ``TaskCancelled``.
- Status values: queued · running · completed · cancelled · error · blocked · timeout · restart.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

from bot.core.errors import TaskCancelled, UserBlockedBot

log = logging.getLogger(__name__)

_ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"
KEEP_TIMEOUT_S = 300
_DETACHED: set[asyncio.Task] = set()  # strong refs for cleanup jobs that outlive their task


def _detached(coro) -> asyncio.Task:
    job = asyncio.get_running_loop().create_task(coro)
    _DETACHED.add(job)
    job.add_done_callback(_DETACHED.discard)
    return job


def new_task_id() -> str:
    return "t" + "".join(secrets.choice(_ALPHABET) for _ in range(6))


@dataclass(eq=False)
class TaskContext:
    task_id: str
    user_id: int
    kind: str  # marks | original_pdf | custom_pdf | html_quiz | tg_quiz | merge | clean | ocr_pdf | batch | …
    lane: str  # light | heavy | background
    priority: int  # owner 0 · admin 1 · user 2
    workdir: Path
    title: str = ""
    chat_id: int | None = None
    status: str = "queued"
    stage: str = ""
    created: float = field(default_factory=time.time)
    started: float | None = None
    ended: float | None = None
    ended_by: str | None = None
    error_id: str | None = None
    outputs: list[dict] = field(default_factory=list)
    data: dict[str, Any] = field(default_factory=dict)
    cancel_event: asyncio.Event = field(default_factory=asyncio.Event)
    _children: set = field(default_factory=set)
    _keep: set = field(default_factory=set)
    _on_close: list = field(default_factory=list)
    _closed: bool = False

    # ------------------------------------------------------------ lifecycle
    async def prepare(self) -> None:
        """Create the workdir for an interactive (held) task — inputs are stored here from the first file."""
        self.status = "preparing"
        await asyncio.to_thread((self.workdir / "in").mkdir, parents=True, exist_ok=True)

    async def __aenter__(self) -> "TaskContext":
        self.status, self.started = "running", time.time()
        await asyncio.to_thread(self.workdir.mkdir, parents=True, exist_ok=True)
        return self

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        if exc_type is None:
            await self.close("completed")
        elif issubclass(exc_type, (asyncio.CancelledError, TaskCancelled)):
            await self.close(self.status if self.status in ("restart", "timeout", "blocked") else "cancelled")
        elif issubclass(exc_type, UserBlockedBot):
            await self.close("blocked", ended_by="user blocked the bot")
        else:
            await self.close("error")
        return False  # never swallow: callers/lanes decide how to report

    def on_close(self, hook: Callable[["TaskContext"], Awaitable[Any] | Any]) -> None:
        """Register cleanup work (runs once, in order, even if earlier hooks fail)."""
        self._on_close.append(hook)

    async def close(self, status: str, *, ended_by: str | None = None, error_id: str | None = None) -> None:
        """Idempotent cleanup — see module docstring."""
        if self._closed:
            return
        self._closed = True
        self.status = status
        self.ended = time.time()
        self.ended_by = ended_by or self.ended_by or ("user" if status == "completed" else None)
        self.error_id = error_id or self.error_id
        self.cancel_event.set()
        for child in list(self._children):
            child.cancel()
        if self._children:
            await asyncio.gather(*self._children, return_exceptions=True)
        if self._keep:  # dump uploads still running: the workdir goes when they finish (user is not kept waiting)
            _detached(self._drain_then_remove(set(self._keep)))
        else:
            await asyncio.to_thread(shutil.rmtree, self.workdir, ignore_errors=True)
        for hook in self._on_close:
            try:
                res = hook(self)
                if asyncio.iscoroutine(res):
                    await res
            except Exception:
                log.exception("on_close hook failed for task %s", self.task_id)

    # ------------------------------------------------------------ helpers
    def spawn(self, coro) -> asyncio.Task:
        """Start a child job tied to this task (cancelled when the task closes)."""
        child = asyncio.get_running_loop().create_task(coro)
        self._children.add(child)
        child.add_done_callback(self._children.discard)
        return child

    def keep(self, coro) -> asyncio.Task:
        """Start a job that must **finish** even if the task closes (e.g. an input's dump upload): it is not
        cancelled on close; the workdir is removed after it ends (at most ``KEEP_TIMEOUT_S``)."""
        job = asyncio.get_running_loop().create_task(coro)
        self._keep.add(job)
        job.add_done_callback(self._keep.discard)
        return job

    async def _drain_then_remove(self, jobs: set) -> None:
        _done, pending = await asyncio.wait(jobs, timeout=KEEP_TIMEOUT_S)
        for job in pending:
            job.cancel()
        await asyncio.to_thread(shutil.rmtree, self.workdir, ignore_errors=True)

    def check(self) -> None:
        """Raise ``TaskCancelled`` if the task was cancelled (call between steps of long loops)."""
        if self.cancel_event.is_set():
            raise TaskCancelled()

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def elapsed(self) -> float:
        start = self.started or self.created
        return (self.ended or time.time()) - start
