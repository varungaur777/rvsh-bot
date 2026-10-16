"""Uploader-bot pool: all dump-channel traffic (inputs, output copies, task logs, dump notices, bundles, quiz cards)
goes through here so the main bot never spends its channel budget (spec §19, owner 2026-10-12: "a team").

Telegram limits (Bot FAQ + MTProto files doc, checked 2026-10-12)
-----------------------------------------------------------------
- One bot may post ≈ **20 messages/min** in one group/channel → each bot's ``Sender`` keeps 17/min, burst 3 per
  chat (never > 20 in any 60 s), ≈ 30/s overall → 25/s.
- Uploading the **bytes** (``upload.saveFilePart``) is not a message: it needs no channel budget. Parts stay on
  the server "several minutes to several hours"; ``FILE_PART_X_MISSING`` → re-save that part and send again.
- Kurigram uploads on a separate media session; ``max_concurrent_transmissions`` files at once per bot.

How the team works
------------------
1. A file job goes to the bot that can **post soonest** in that chat: its rate bucket (tokens now, flood pauses
   included) minus the jobs already assigned to it there; ties → fewer jobs in hand, then fewer sent. N bots ≈ N ×
   17 posts/min in one channel.
2. That bot uploads the bytes first — up to ``per_bot_concurrency`` files in parallel per bot, **no** channel
   token held — then takes one token and posts (``messages.sendMedia`` with the uploaded file).
3. ``FloodWait`` when posting: the bot's bucket for that chat is paused for ``e.value``. A short wait (≤ ``STAY_S``)
   is waited out on the same bot (its upload stays valid); a longer one → the job moves to the bot that can post
   soonest (if any is sooner). Rights errors (not admin, …) take a bot out for 10 minutes.
4. No uploader bots → the main bot does the work through its own sender.

Depends on: ``core.sender`` (one ``Sender`` per bot), Kurigram raw API.
Used by: ``core.delivery``, ``core.tasklog``, ``core.dump`` (notifier), ``exam.inputs``, ``exam.registry``.

Public API
----------
- ``UploaderPool(fallback_client, fallback_sender, per_bot_concurrency=4, send_media=send_uploaded)`` ·
  ``add(client, name)`` · ``size``
- ``await send_document(chat_id, path, caption="", reply_to=None, progress=None, file_name=None, **kw)``
  ``-> (bot_name, Message)`` · ``await send_photo(...)`` (same) · ``await send_message(chat_id, text, reply_to=None,
  **kw)`` — ``kw`` goes to Kurigram (``parse_mode``, ``link_preview_options``); ``priority`` is accepted and ignored,
  so the pool can stand in for a ``Sender`` (dump notifier).
- ``await edit_text(bot_name, chat_id, message_id, text)`` — edits must come from the bot that sent.
- ``stats()`` — per bot: sent, in hand, uploading, flood waits, errors, paused seconds, MB uploaded.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path

from pyrogram import errors as tg_errors
from pyrogram import raw, utils

from bot.core.sender import LOG, NO_PREVIEW, Sender

log = logging.getLogger(__name__)

STAY_S = 20.0       # flood waits up to this long are waited out on the same bot (upload kept)
RIGHTS_PAUSE_S = 600
_RIGHTS = (tg_errors.ChatAdminRequired, tg_errors.ChannelPrivate, tg_errors.ChatWriteForbidden, tg_errors.PeerIdInvalid)


async def send_uploaded(client, chat_id, file, path: str, *, kind: str, caption: str, reply_to: int | None,
                        file_name: str | None = None, parse_mode=None):
    """Post an already uploaded file (``client.save_file`` result) — the part of ``send_document`` that counts as
    a message. A part dropped from Telegram's temporary storage is uploaded again (files API)."""
    if kind == "photo":
        media = raw.types.InputMediaUploadedPhoto(file=file)
    else:
        name = file_name or Path(path).name
        media = raw.types.InputMediaUploadedDocument(
            mime_type=client.guess_mime_type(name) or "application/octet-stream", file=file,
            attributes=[raw.types.DocumentAttributeFilename(file_name=name)])
    for _ in range(3):
        try:
            r = await client.invoke(raw.functions.messages.SendMedia(
                peer=await client.resolve_peer(chat_id), media=media, random_id=client.rnd_id(),
                reply_to=raw.types.InputReplyToMessage(reply_to_msg_id=reply_to) if reply_to else None,
                **await utils.parse_text_entities(client, caption, parse_mode, None)))
        except tg_errors.FilePartMissing as e:
            part = getattr(e, "file_part", None)
            await client.save_file(path, file_id=file.id, file_part=int(part if part is not None else e.value))
            continue
        msgs = await utils.parse_messages(client=client, messages=r)
        return msgs[0] if msgs else None
    raise tg_errors.FilePartMissing()


@dataclass
class _Worker:
    name: str
    client: object
    sender: Sender
    slots: asyncio.Semaphore          # parallel byte uploads of this bot
    is_fallback: bool = False
    inflight: int = 0                 # jobs in hand (uploading, waiting for a token, posting)
    uploading: int = 0
    pending: dict = field(default_factory=dict)  # chat_id → jobs in hand that still need a post there
    sent: int = 0
    floods: int = 0
    errors: int = 0
    paused_until: float = 0.0         # rights problems only (floods pause the chat bucket, not the bot)
    last_error: str = ""
    bytes_up: int = 0

    def eta(self, chat_id, now: float) -> float:
        """Seconds until this bot could post one more message in ``chat_id`` (jobs it already holds included)."""
        if self.paused_until > now:
            return 1e6 + self.paused_until - now
        b = self.sender.bucket(chat_id)
        need = self.pending.get(chat_id, 0) + 1 - b.available()
        return max(0.0, need / b.rate)


class UploaderPool:
    def __init__(self, fallback_client, fallback_sender: Sender, per_bot_concurrency: int = 4,
                 send_media=send_uploaded) -> None:
        self.per_bot = max(1, int(per_bot_concurrency))
        self.send_media = send_media
        self._fallback = _Worker("main", fallback_client, fallback_sender, asyncio.Semaphore(self.per_bot), is_fallback=True)
        self.workers: list[_Worker] = []

    def add(self, client, name: str) -> None:
        """Register an uploader bot. Its sender never retries by itself: the pool decides (stay or move)."""
        self.workers.append(_Worker(name, client, Sender(client, max_retries=0), asyncio.Semaphore(self.per_bot)))

    @property
    def size(self) -> int:
        return len(self.workers)

    def _pick(self, chat_id, avoid: _Worker | None = None) -> _Worker:
        team = [w for w in self.workers if w is not avoid] or self.workers or [self._fallback]
        now = time.monotonic()
        return min(team, key=lambda w: (round(w.eta(chat_id, now), 1), w.inflight, w.sent))

    async def _job(self, chat_id, make, upload: tuple[str, object] | None = None):
        """``make(client, file) -> (fn, args, kwargs)`` builds the posting call; ``upload = (path, progress)``."""
        attempts = max(3, len(self.workers) + 2)
        last: Exception | None = None
        avoid: _Worker | None = None
        for _ in range(attempts):
            w = self._pick(chat_id, avoid)
            wait = w.paused_until - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            w.inflight += 1
            w.pending[chat_id] = w.pending.get(chat_id, 0) + 1
            try:
                file = None
                if upload is not None:
                    async with w.slots:
                        w.uploading += 1
                        try:
                            file = await w.client.save_file(upload[0], progress=upload[1])
                        finally:
                            w.uploading -= 1
                    try:
                        w.bytes_up += Path(upload[0]).stat().st_size
                    except OSError:
                        pass
                stays = 0
                while True:
                    fn, args, kw = make(w.client, file)
                    try:
                        msg = await w.sender.call(chat_id, fn, *args, priority=LOG, retries=0, **kw)
                        w.sent += 1
                        return w.name, msg
                    except tg_errors.FloodWait as e:  # the sender has paused this bot's bucket for the chat
                        secs = float(getattr(e, "value", 5) or 5)
                        w.floods += 1
                        last = e
                        better = self._pick(chat_id, w)
                        stays += 1
                        if stays <= 3 and (secs <= STAY_S or better is w or better.eta(chat_id, time.monotonic()) >= secs):
                            log.info("uploader %s flood-waits %.0fs — waiting it out (upload kept)", w.name, secs)
                            continue
                        log.info("uploader %s flood-waits %.0fs — %s takes the job", w.name, secs, better.name)
                        avoid = w
                        break
            except _RIGHTS as e:  # this bot cannot post here: out for 10 minutes, another one takes over
                w.errors += 1
                w.last_error = e.__class__.__name__
                w.paused_until = time.monotonic() + RIGHTS_PAUSE_S
                last, avoid = e, w
                log.warning("uploader %s cannot post in %s (%s) — paused %ds", w.name, chat_id, w.last_error, RIGHTS_PAUSE_S)
                if w.is_fallback or not self.workers:
                    raise
            finally:
                w.inflight -= 1
                left = w.pending.get(chat_id, 1) - 1
                if left > 0:
                    w.pending[chat_id] = left
                else:
                    w.pending.pop(chat_id, None)
        raise last or RuntimeError("upload failed")

    # ------------------------------------------------------------ public
    async def send_document(self, chat_id: int, path: str, caption: str = "", reply_to: int | None = None, *,
                            progress=None, file_name: str | None = None, parse_mode=None, **_kw):
        return await self._file(chat_id, path, "document", caption, reply_to, progress, file_name, parse_mode)

    async def send_photo(self, chat_id: int, photo: str, caption: str = "", reply_to: int | None = None, *,
                         progress=None, parse_mode=None, **_kw):
        return await self._file(chat_id, photo, "photo", caption, reply_to, progress, None, parse_mode)

    def _file(self, chat_id, path, kind, caption, reply_to, progress, file_name, parse_mode):
        path = str(path)

        def make(client, file):
            return self.send_media, (client, chat_id, file, path), dict(
                kind=kind, caption=(caption or "")[:1024], reply_to=reply_to, file_name=file_name, parse_mode=parse_mode)
        return self._job(chat_id, make, upload=(path, progress))

    async def send_message(self, chat_id: int, text: str, reply_to: int | None = None, **kw):
        kw.pop("priority", None)
        kw.setdefault("link_preview_options", NO_PREVIEW)
        if reply_to:
            kw["reply_to_message_id"] = reply_to  # the sender turns it into reply_parameters

        def make(client, _file):
            return client.send_message, (chat_id, text), dict(kw)
        return await self._job(chat_id, make)

    async def edit_text(self, bot_name: str, chat_id: int, message_id: int, text: str) -> None:
        """Only the sending bot can edit: a short flood wait is waited out, a long one raises (callers post anew)."""
        w = next((x for x in [*self.workers, self._fallback] if x.name == bot_name), None)
        if w is None:
            raise LookupError(bot_name)
        for attempt in range(3):
            try:
                await w.sender.call(chat_id, w.client.edit_message_text, chat_id, message_id, text, priority=LOG,
                                    retries=0, link_preview_options=NO_PREVIEW)
                return
            except tg_errors.FloodWait as e:
                w.floods += 1
                if float(getattr(e, "value", 5) or 5) > STAY_S or attempt == 2:
                    raise

    def stats(self) -> list[dict]:
        now = time.monotonic()
        out = []
        for w in (self.workers or [self._fallback]):
            flood = max((-b.available() / b.rate for b in w.sender._chats.values() if b.available() < 0), default=0)
            out.append({"bot": w.name, "sent": w.sent, "inflight": w.inflight, "uploading": w.uploading,
                        "floods": w.floods, "errors": w.errors, "mb_up": round(w.bytes_up / 2**20, 1),
                        "paused_s": int(max(w.paused_until - now, flood, 0))})
        return out
