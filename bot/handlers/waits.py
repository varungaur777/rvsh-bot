"""Messages an admin screen is waiting for (a broadcast's messages, an ID to add as admin, a user to look up …).

Depends on: ``core.access.guard``, ``tasks.session``. Used by: ``handlers/__init__.py`` (handler group −1, so these
messages never reach the exam input handlers), ``handlers/broadcast.py``, ``handlers/people.py``.

Public API
----------
- ``WAITERS`` — ``{session step: async fn(app, message, sess)}``; a module registers its steps at import.
- ``waiting_filter()`` — Kurigram filter: private, non-command message whose sender's session step is in ``WAITERS``.
- ``intercept(client, message)`` — runs the step's function (admins only) and always stops propagation.
"""

from __future__ import annotations

from typing import Awaitable, Callable

from pyrogram import StopPropagation, filters

from bot.core.access import guard

WAITERS: dict[str, Callable[..., Awaitable[None]]] = {}


def waiting_filter():
    def check(_, client, m) -> bool:
        if not m.from_user or not m.chat or m.chat.id != m.from_user.id:
            return False
        s = client.app.sessions.peek(m.from_user.id)
        return bool(s and s.step in WAITERS and not (m.text or "").startswith("/"))
    return filters.create(check)


async def intercept(client, message) -> None:
    try:
        await _take(client, message)
    finally:
        raise StopPropagation


@guard("admin", force_sub=False, private_only=True)
async def _take(client, message) -> None:
    app = client.app
    sess = app.sessions.peek(message.from_user.id)
    fn = WAITERS.get(sess.step) if sess else None
    if fn is not None:
        await fn(app, message, sess)
