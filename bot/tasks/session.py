"""Temporary per-user store (spec §9.3) — in memory, small, cleared only by ``TaskContext`` cleanup or timeout.

Depends on: stdlib. Used by: handlers (collection, preference cards), ``tasks.context``.

Public API
----------
- ``Session`` — step/state, active task id, input refs (dump copies) + original message ids, dump anchor id,
  source URLs, preferences, timeout handle, free-form ``data``, ``lock`` (button handlers: one press at a time).
- ``SessionStore`` · ``get(user_id)`` (creates) · ``peek(user_id)`` · ``clear(user_id)`` ·
  ``arm_timeout(user_id, seconds, on_timeout)`` · ``disarm(user_id)``
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from bot.core.bg import spawn

log = logging.getLogger(__name__)


@dataclass(slots=True)
class Session:
    user_id: int
    step: str = ""  # e.g. "exam:collect", "exam:prefs"
    task_id: str | None = None
    inputs: list[dict] = field(default_factory=list)  # [{"tg": TgRef, "msg_id": original id, "name": …}]
    anchor_id: int | None = None  # first input's copy in the dump channel; outputs reply to it
    urls: list[str] = field(default_factory=list)
    prefs: dict[str, Any] = field(default_factory=dict)
    ui_message_id: int | None = None  # the card/progress message being edited
    data: dict[str, Any] = field(default_factory=dict)
    timeout: asyncio.TimerHandle | None = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)  # one button press at a time (double taps)


class SessionStore:
    def __init__(self) -> None:
        self._s: dict[int, Session] = {}

    def get(self, user_id: int) -> Session:
        s = self._s.get(user_id)
        if s is None:
            s = self._s[user_id] = Session(user_id)
        return s

    def peek(self, user_id: int) -> Session | None:
        return self._s.get(user_id)

    def clear(self, user_id: int) -> None:
        """Forget the session; live UI helpers kept in ``data`` (progress messages) are stopped too."""
        s = self._s.pop(user_id, None)
        if s is None:
            return
        if s.timeout:
            s.timeout.cancel()
        for v in s.data.values():
            close = getattr(v, "close", None)
            if callable(close):
                close()

    def arm_timeout(self, user_id: int, seconds: float, on_timeout: Callable[[int], Awaitable[Any]]) -> None:
        """(Re)start the inactivity timer of a user's interactive step."""
        s = self.get(user_id)
        if s.timeout:
            s.timeout.cancel()
        loop = asyncio.get_running_loop()

        def fire() -> None:
            s.timeout = None
            spawn(_safe(on_timeout(user_id)), f"timeout-{user_id}")

        s.timeout = loop.call_later(seconds, fire)

    def disarm(self, user_id: int) -> None:
        s = self._s.get(user_id)
        if s and s.timeout:
            s.timeout.cancel()
            s.timeout = None

    def __len__(self) -> int:
        return len(self._s)


async def _safe(coro) -> None:
    try:
        await coro
    except Exception:
        log.exception("session timeout handler failed")
