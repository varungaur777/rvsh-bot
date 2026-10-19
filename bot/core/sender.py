"""Central rate-limited sender — every outgoing call goes through here (spec §6.8, §4).

Depends on: Kurigram (``pyrogram``) errors, ``core.ratelimit``, ``core.errors``.
Used by: handlers, progress messages, dump notifier, task log, quiz sessions, broadcast, uploader workers.

Public API
----------
- Priorities: ``INTERACTIVE`` (0) < ``POLL`` (1) < ``PROGRESS`` (2) < ``LOG`` (3) < ``BULK`` (4).
- ``Sender(client, on_blocked=None, on_unwritable=None)`` · ``await call(chat_id, fn, *args, priority=…, cost=1, **kw)``
  — generic (``cost``: messages the call really sends, e.g. an album of 2 — the extra ones are charged to the chat).
- ``undeliverable(exc) -> "blocked" | "slowmode" | "cant_write" | "gone" | None``.
  ``bucket(chat_id)`` — that chat's ``TokenBucket`` (the uploader pool reads it to pick a bot).
- Shortcuts: ``send_message``, ``edit_text``, ``edit_markup``, ``copy``, ``forward``, ``send_document``,
  ``send_photo``, ``send_poll``, ``delete``, ``remove`` (delete, or close the card when that is refused — never
  raises), ``answer`` (callback queries; global bucket only).
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
- The message a send replies to is gone / not valid there (``REPLY_MESSAGE_ID_INVALID``,
  ``REPLY_TO_MONOFORUM_PEER_INVALID``, a deleted topic …): sent again once **without** the reply (Err.txt 2026-10-19).
- ``SlowmodeWait`` (a group's slow mode; the bot is not an admin there): the chat bucket pauses that long and the
  error is raised at once — never waited out (it can be an hour).
- The chat refuses the bot (``CHAT_SEND_PLAIN_FORBIDDEN`` and every other 403, ``CHAT_WRITE_FORBIDDEN``, banned,
  private / deleted channel, slow mode): ``on_unwritable(chat_id, reason)`` is scheduled for a group / channel (the
  ``groups`` doc gets ``can_write: false``) and the original error is raised. ``undeliverable(exc)`` names these
  cases (``blocked`` / ``slowmode`` / ``cant_write`` / ``gone``) so handlers, the quiz engine and the error reporter
  treat them as the chat's state, not as a bug: logged, never reported to dump as an error.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable

from pyrogram import errors as tg_errors
from pyrogram.types import LinkPreviewOptions, ReplyParameters

from bot.core.bg import spawn
from bot.core.errors import UserBlockedBot
from bot.core.ratelimit import TokenBucket

log = logging.getLogger(__name__)

INTERACTIVE, POLL, PROGRESS, LOG, BULK = 0, 1, 2, 3, 4
NO_PREVIEW = LinkPreviewOptions(is_disabled=True)



def _errors(*names: str) -> tuple:
    return tuple(getattr(tg_errors, n) for n in names if hasattr(tg_errors, n))


_BLOCKED = _errors("UserIsBlocked", "InputUserDeactivated", "UserDeactivated", "UserDeactivatedBan", "PeerIdInvalid")
_USER_GONE = _errors("UserIsBlocked", "InputUserDeactivated", "UserDeactivated", "UserDeactivatedBan")
_NOT_MODIFIED = getattr(tg_errors, "MessageNotModified", ())
_SLOWMODE = getattr(tg_errors, "SlowmodeWait", ())
_REPLY_BAD = _errors("ReplyMessageIdInvalid", "ReplyToMonoforumPeerInvalid", "ReplyToInvalid", "TopicIdInvalid",
                     "MessageIdInvalid", "TopicDeleted")
REPLY_ERRORS = _REPLY_BAD  # public: the uploader pool posts with its own reply field
# the chat refuses the bot (every 403 is a permission, never a bug of ours) / the chat is gone for the bot
_CANT_WRITE = _errors("ChatWriteForbidden", "ChatRestricted", "UserBannedInChannel", "ChatAdminRequired", "TopicClosed",
                      "TopicDeleted") + (tg_errors.Forbidden,)
_GONE = _errors("ChannelPrivate", "ChannelInvalid", "ChatIdInvalid", "ChatInvalid", "PeerIdInvalid", "UserIsBot")


def undeliverable(exc: BaseException | None) -> str | None:
    """Why Telegram refused — the chat's state, not a bug: ``"blocked"`` (a user blocked the bot / was deleted),
    ``"slowmode"``, ``"cant_write"`` (no right to post there / any 403), ``"gone"`` (private, deleted or invalid
    chat); ``None`` for anything else."""
    if exc is None:
        return None
    if isinstance(exc, UserBlockedBot) or isinstance(exc, _USER_GONE):
        return "blocked"
    if isinstance(exc, _SLOWMODE):
        return "slowmode"
    if isinstance(exc, _GONE):
        return "gone"
    if isinstance(exc, _CANT_WRITE):
        return "cant_write"
    return None


class Sender:
    def __init__(self, client, *, on_blocked: Callable[[int], Awaitable[Any]] | None = None,
                 on_unwritable: Callable[[int, str], Awaitable[Any]] | None = None,
                 global_rate: float = 25.0, private_rate: float = 1.0, group_rate: float = 17 / 60,
                 max_retries: int = 3) -> None:
        self.client = client
        self.on_blocked = on_blocked
        self.on_unwritable = on_unwritable
        self.global_bucket = TokenBucket(global_rate, global_rate)
        self.private_rate, self.group_rate = private_rate, group_rate
        self.max_retries = max_retries
        self._chats: dict[int, TokenBucket] = {}
        self.counters = {"sent": 0, "flood_waits": 0, "blocked": 0, "errors": 0, "unwritable": 0,
                         "reply_dropped": 0}

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
                   cost: int = 1, **kwargs: Any) -> Any:
        """Run ``fn(*args, **kwargs)`` after taking a chat token and a global token; handles flood waits.

        ``retries`` overrides ``max_retries`` (0 = a FloodWait is raised at once, after pausing the bucket).
        ``max_wait``: give up with ``asyncio.TimeoutError`` if the chat's rate budget has no token within that
        many seconds — for work that has a cheaper fallback (e.g. dump forwards → uploader bots).
        ``cost`` > 1 (an album): the extra messages are charged to the chat bucket after the call."""
        if "reply_to_message_id" in kwargs:  # deprecated in Kurigram → reply_parameters
            rid = kwargs.pop("reply_to_message_id")
            if rid:
                kwargs["reply_parameters"] = ReplyParameters(message_id=rid)
        bucket = self.bucket(chat_id)
        tries = self.max_retries if retries is None else retries
        attempt = 0
        while True:
            if bucket is not None:
                if max_wait is None:
                    await bucket.acquire(priority)
                else:  # a cancelled waiter is skipped by the bucket, so the timeout is safe
                    await asyncio.wait_for(bucket.acquire(priority), max_wait)
            await self.global_bucket.acquire(priority)
            try:
                result = await fn(*args, **kwargs)
                self.counters["sent"] += 1
                if cost > 1 and bucket is not None:
                    bucket.charge(cost - 1)
                return result
            except tg_errors.FloodWait as e:
                wait = float(getattr(e, "value", 5) or 5)
                self.counters["flood_waits"] += 1
                log.warning("FloodWait %.0fs on chat %s (attempt %d)", wait, chat_id, attempt + 1)
                (bucket or self.global_bucket).pause(wait)
                if wait > 60:
                    self.global_bucket.pause(min(wait, 10))
                if attempt >= tries:
                    raise
                attempt += 1
            except _NOT_MODIFIED:
                return None
            except _BLOCKED as e:
                if isinstance(chat_id, int) and chat_id > 0:
                    self.counters["blocked"] += 1
                    if self.on_blocked:
                        spawn(self.on_blocked(chat_id), "on-blocked")
                    raise UserBlockedBot(detail=f"{chat_id}: {e.__class__.__name__}") from e
                self._unwritable(chat_id, e)
                raise
            except _REPLY_BAD:
                if kwargs.pop("reply_parameters", None) is None:
                    self.counters["errors"] += 1
                    raise
                self.counters["reply_dropped"] += 1  # the message we answer was deleted / is not valid there
                log.info("reply target not valid in chat %s — sending without the reply", chat_id)
            except Exception as e:
                if isinstance(e, _SLOWMODE) and bucket is not None:  # never waited out (can be an hour)
                    bucket.pause(float(getattr(e, "value", 0) or 0))
                if undeliverable(e):
                    self._unwritable(chat_id, e)
                else:
                    self.counters["errors"] += 1
                raise

    def _unwritable(self, chat_id, exc: BaseException) -> None:
        """A chat refused the bot: counted and logged; for a group / channel the hook is told (``groups.can_write``)."""
        self.counters["unwritable"] += 1
        log.info("chat %s refused the bot: %s", chat_id, exc.__class__.__name__)
        if self.on_unwritable and isinstance(chat_id, int) and chat_id < 0:
            try:
                spawn(self.on_unwritable(chat_id, undeliverable(exc) or "cant_write"), "on-unwritable")
            except RuntimeError:  # no running loop (shutdown)
                pass

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

    async def remove(self, chat_id, message_id: int, closed_text: str | None = None) -> bool:
        """Delete one of the bot's own messages (a used card); when Telegram refuses (older than 48 h, no right —
        ``MESSAGE_DELETE_FORBIDDEN``, Err.txt) its buttons are removed instead (the text replaced by ``closed_text``
        when given). Never raises; True when it was deleted."""
        try:
            await self.delete(chat_id, message_id)
            return True
        except Exception as e:
            log.info("message %s in %s not deleted (%s) — closing it instead", message_id, chat_id, e.__class__.__name__)
        try:
            if closed_text:
                await self.edit_text(chat_id, message_id, closed_text, reply_markup=None)
            else:
                await self.edit_markup(chat_id, message_id, None)
        except Exception:
            pass
        return False

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
