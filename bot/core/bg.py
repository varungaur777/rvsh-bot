"""Fire-and-forget coroutines, done safely — one helper for the whole bot (it used to be copied in three modules).

asyncio keeps only a weak reference to a task made by ``create_task``: one nobody holds can be garbage-collected
before it finishes (Python docs, ``asyncio.create_task``) — a quiz timer's next question, a button clean-up or a
"user blocked the bot" clean-up could silently never run. ``spawn`` keeps a strong reference until the job ends and
logs its error (never "Task exception was never retrieved").

Public API: ``spawn(coro, name=None) -> asyncio.Task`` · ``running() -> int``.
Depends on: stdlib. Used by: sender hooks, quiz engine timers, app boot jobs, sessions, lanes, inputs, task cleanup.
"""

from __future__ import annotations

import asyncio
import logging

log = logging.getLogger(__name__)
_JOBS: set[asyncio.Task] = set()


def spawn(coro, name: str | None = None) -> asyncio.Task:
    """Run ``coro`` in the background on the running loop; the task is kept alive until it ends."""
    job = asyncio.get_running_loop().create_task(coro, name=name)
    _JOBS.add(job)
    job.add_done_callback(_done)
    return job


def _done(job: asyncio.Task) -> None:
    _JOBS.discard(job)
    if job.cancelled():
        return
    exc = job.exception()
    if exc is not None:
        log.error("background job %s failed: %r", job.get_name(), exc, exc_info=exc)


def running() -> int:
    """Background jobs still going (``/stats``)."""
    return len(_JOBS)
