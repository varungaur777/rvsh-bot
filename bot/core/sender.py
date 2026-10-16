"""Central rate-limited sender — every outgoing call goes through here (spec §6.8, §4).

Depends on: Kurigram (``pyrogram``) errors, ``core.ratelimit``, ``core.errors``.
Used by: handlers, progress messages, dump notifier, task log, quiz sessions, broadcast, uploader workers.

Public API
----------
- Priorities: ``INTERACTIVE`` (0) < ``POLL`` (1) < ``PROGRESS`` (2) < ``LOG`` (3) < ``BULK`` (4).
- ``Sender(client, on_blocked=None)`` · ``await call(chat_id, fn, *args, priority=…, **kw)`` — generic.
  ``bucket(chat_id)`` — that chat's ``TokenBucket`` (the uploader pool reads it to pick a bot).
- Shortcuts: ``send_message``, ``edit_text``, ``edit_markup``, ``copy``, ``forward``, ``send_document``,
  ``send_photo``, ``send_poll``, ``delete``, ``answer`` (callback queries; global bucket only).
- ``stats()`` for ``/stats``.

Limits (Telegram Bot FAQ, checked 2026-10-12 — spec §4): ≈ 30 messages/s overall → 25/s here; one chat 1/s
(short bursts allowed) → 1/s, burst 3; a group/channel **20 messages/min** → 17/min with burst 3, so no 60-second
window ever holds more than 20 (burst + rate × 60 s).

Exceptions → outcome
--------------------
- ``FloodWait``: the chat bucket (or the global one for very long waits) is paused for ``e.value`` and the
  call retried, up to ``max_retries`` (or the per-call ``retries``); then the error propagates.
- ``asyncio.TimeoutError``: only with ``max_wait`` — no rate token within that time (nothing was sent).
- ``MessageNotModified``: returns ``None`` (an edit with identical content is not an error).
- ``UserIsBlocked`` / ``InputUserDeactivated`` / ``PeerIdInvalid`` for a private chat: ``on_blocked(chat_id)``
  is scheduled (task cleanup, ``blocked_bot=true``) and ``UserBlockedBot`` is raised.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable

from pyrogram import errors as tg_errors
from pyrogram.types import LinkPreviewOptions, ReplyParameters

from bot.core.errors import UserBlockedBot
from bot.core.ratelimit import TokenBucket

log = logging.getLogger(__name__)

INTERACTIVE, POLL, PROGRESS, LOG, BULK = 0, 1, 2, 3, 4
NO_PREVIEW = LinkPreviewOptions(is_disabled=True)

_BLOCKED = tuple(getattr(tg_errors, n) for n in ("UserIsBlocked", "InputUserDeactivated", "UserDeactivated",
                                                   "UserDeactivatedBan", "PeerIdInvalid") if hasattr(tg_errors, n))
_NOT_MODIFIED = getattr(tg_errors, "MessageNotModified", ())


class Sender:
    def __init__(self, client, *, on_blocked: Callable[[int], Awaitable[Any]] | None = None,
                 global_rate: float = 25.0, private_rate: float = 1.0, group_rate: float = 17 / 60,
                 max_retries: int = 3) -> None:
        self.client = client
        self.on_blocked = on_blocked
        self.global_bucket = TokenBucket(global_rate, global_rate)
        self.private_rate, self.group_rate = private_rate, group_rate
        self.max_retries = max_retries
        self._chats: dict[int, TokenBucket] = {}
        self.counters = {"sent": 0, "flood_waits": 0, "blocked": 0, "errors": 0}

    def bucket(self, chat_id: int | str | None) -> TokenBucket | None:
        """The rate bucket of a chat (created on first use); ``None`` = global only."""
        if chat_id is None:
            return None
        key = chat_id if isinstance(chat_id, int) else hash(chat_id)
        b = self._chats.get(key)
        if b is None:
            if len(self._chats) > 5000:  # forget idle chats
                for k in [k for k, v in self._chats.items() if v.idle_for > 600]:
                    del self._chats[k]
            private = isinstance(chat_id, int) and chat_id > 0
            b = self._chats[key] = TokenBucket(self.private_rate, 3) if private else TokenBucket(self.group_rate, 3)
        return b

    async def call(self, chat_id: int | str | None, fn: Callable[..., Awaitable[Any]], *args: Any,
                   priority: int = INTERACTIVE, retries: int | None = None, max_wait: float | None = None,
                   **kwargs: Any) -> Any:
        """Run ``fn(*args, **kwargs)`` after taking a chat token and a global token; handles flood waits.

        ``retries`` overrides ``max_retries`` (0 = a FloodWait is raised at once, after pausing the bucket).
        ``max_wait``: give up with ``asyncio.TimeoutError`` if the chat's rate budget has no token within that
        many seconds — for work that has a cheaper fallback (e.g. dump forwards → uploader bots)."""
        if "reply_to_message_id" in kwargs:  # deprecated in Kurigram → reply_parameters
            rid = kwargs.pop("reply_to_message_id")
            if rid:
                kwargs["reply_parameters"] = ReplyParameters(message_id=rid)
        bucket = self.bucket(chat_id)
        tries = self.max_retries if retries is None else retries
        for attempt in range(tries + 1):
            if bucket is not None:
                if max_wait is None:
                    await bucket.acquire(priority)
                else:  # a cancelled waiter is skipped by the bucket, so the timeout is safe
                    await asyncio.wait_for(bucket.acquire(priority), max_wait)
            await self.global_bucket.acquire(priority)
            try:
                result = await fn(*args, **kwargs)
                self.counters["sent"] += 1
                return result
            except tg_errors.FloodWait as e:
                wait = float(getattr(e, "value", 5) or 5)
                self.counters["flood_waits"] += 1
                log.warning("FloodWait %.0fs on chat %s (attempt %d)", wait, chat_id, attempt + 1)
                (bucket or self.global_bucket).pause(wait)
                if wait > 60:
                    self.global_bucket.pause(min(wait, 10))
                if attempt == tries:
                    raise
            except _NOT_MODIFIED:
                return None
            except _BLOCKED as e:
                if isinstance(chat_id, int) and chat_id > 0:
                    self.counters["blocked"] += 1
                    if self.on_blocked:
                        asyncio.get_running_loop().create_task(self.on_blocked(chat_id))
                    raise UserBlockedBot(detail=f"{chat_id}: {e.__class__.__name__}") from e
                raise
            except Exception:
                self.counters["errors"] += 1
                raise
        return None

    # ------------------------------------------------------------ shortcuts
    def send_message(self, chat_id, text, *, priority=INTERACTIVE, **kw):
        return self.call(chat_id, self.client.send_message, chat_id, text, priority=priority, **kw)

    def edit_text(self, chat_id, message_id, text, *, priority=INTERACTIVE, **kw):
        return self.call(chat_id, self.client.edit_message_text, chat_id, message_id, text, priority=priority, **kw)

    def edit_markup(self, chat_id, message_id, reply_markup=None, *, priority=INTERACTIVE):
        return self.call(chat_id, self.client.edit_message_reply_markup, chat_id, message_id, reply_markup, priority=priority)

    def copy(self, chat_id, from_chat_id, message_id, *, priority=INTERACTIVE, **kw):
        return self.call(chat_id, self.client.copy_message, chat_id, from_chat_id, message_id, priority=priority, **kw)

    def forward(self, chat_id, from_chat_id, message_ids, *, priority=INTERACTIVE, **kw):
        return self.call(chat_id, self.client.forward_messages, chat_id, from_chat_id, message_ids, priority=priority, **kw)

    def send_document(self, chat_id, document, *, priority=INTERACTIVE, **kw):
        return self.call(chat_id, self.client.send_document, chat_id, document, priority=priority, **kw)

    def send_photo(self, chat_id, photo, *, priority=INTERACTIVE, **kw):
        return self.call(chat_id, self.client.send_photo, chat_id, photo, priority=priority, **kw)

    def send_poll(self, chat_id, question, options, *, priority=POLL, **kw):
        return self.call(chat_id, self.client.send_poll, chat_id, question, options, priority=priority, **kw)

    def delete(self, chat_id, message_ids, *, priority=PROGRESS):
        return self.call(chat_id, self.client.delete_messages, chat_id, message_ids, priority=priority)

    async def answer(self, callback_query_id: str, text: str | None = None, *, show_alert: bool = False, **kw):
        """Answer a button press (global bucket only — answers are not chat messages). A press answered too late
        (Telegram keeps queries ~15 s; e.g. after a network stall) is not an error: it is logged and ignored."""
        try:
            return await self.call(None, self.client.answer_callback_query, callback_query_id, text, show_alert, **kw)
        except tg_errors.QueryIdInvalid:
            log.info("callback query %s expired before it was answered", callback_query_id)
            return None

    def stats(self) -> dict:
        return {**self.counters, "chats_tracked": len(self._chats), "global_waiting": self.global_bucket.waiting}
