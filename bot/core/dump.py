"""Dump-channel notifier: fire-and-forget events, batched and rate-limited (spec §6.1).

Depends on: ``core.sender``. Used by: boot/shutdown reports, access (new user/group), error reporting,
DB state changes, governor alerts, log rotation, task summaries.

Public API
----------
- ``DumpNotifier(sender, chat_id)`` · ``start()`` · ``await stop()`` (drains the queue)
- ``notify(text)`` — queued, never blocks, never raises. Small texts within ``batch_window_s`` are joined
  into one message (≤ 4096 chars).
- ``send_file(path, caption, delete_after=False)`` — queued document upload (log files, tracebacks).
- ``report_error(exc, where, error_id) -> str`` — logs + queues a dump report; returns the error id. A chat that
  refused the bot (``sender.undeliverable``: blocked, can't post, slow mode, gone) is only logged — not a bug.
- ``new_error_id()`` — short id shown to users ("Error ID: K7Q2").

Queue is bounded (``max_queue``); when full the oldest text events are dropped and counted (``dropped``).
"""

from __future__ import annotations

import asyncio
import html
import logging
import re
import secrets
import traceback
from pathlib import Path

from pyrogram import enums
from pyrogram import errors as tg_errors

from bot.core.sender import LOG, NO_PREVIEW

log = logging.getLogger(__name__)
_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


def new_error_id() -> str:
    return "".join(secrets.choice(_ALPHABET) for _ in range(5))


class DumpNotifier:
    def __init__(self, sender, chat_id: int, *, batch_window_s: float = 2.0, max_queue: int = 1000) -> None:
        self.sender = sender
        self.chat_id = chat_id
        self.batch_window_s = batch_window_s
        self.queue: asyncio.Queue = asyncio.Queue(max_queue)
        self.dropped = 0
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.get_running_loop().create_task(self._run(), name="dump-notifier")

    async def stop(self, timeout: float = 10) -> None:
        if self._task is None:
            return
        await self.queue.put(None)
        try:
            await asyncio.wait_for(self._task, timeout)
        except asyncio.TimeoutError:
            self._task.cancel()
        self._task = None

    # ------------------------------------------------------------ producers
    def notify(self, text: str) -> None:
        """Queue an HTML text event (fire-and-forget)."""
        self._put(("text", text))

    def send_file(self, path: Path, caption: str = "", delete_after: bool = False) -> None:
        self._put(("file", (Path(path), caption, delete_after)))

    def report_error(self, exc: BaseException, where: str, error_id: str | None = None) -> str:
        """Log an unexpected error and queue a dump report with its traceback."""
        error_id = error_id or new_error_id()
        from bot.core.sender import undeliverable
        if why := undeliverable(exc):  # a chat refused the bot (blocked, no right to post, slow mode): not a bug
            log.info("%s in %s: %s (%r) — not reported", error_id, where, why, exc)
            return error_id
        tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        log.error("error %s in %s: %r\n%s", error_id, where, exc, tb)
        text = f"⚠️ <b>Error</b> <code>{error_id}</code> in <code>{html.escape(where)}</code>\n<code>{html.escape(repr(exc))[:500]}</code>"
        if len(tb) < 3000:
            text += f"\n<pre>{html.escape(tb[-3000:])}</pre>"
        self.notify(text)
        return error_id

    def _put(self, item) -> None:
        try:
            self.queue.put_nowait(item)
        except asyncio.QueueFull:
            self.dropped += 1
        except RuntimeError:  # no running loop (shutdown)
            self.dropped += 1

    # ------------------------------------------------------------ consumer
    async def _run(self) -> None:
        while True:
            item = await self.queue.get()
            if item is None:
                return
            try:
                if item[0] == "file":
                    await self._send_file(*item[1])
                    continue
                texts = [item[1]]
                deadline = asyncio.get_running_loop().time() + self.batch_window_s
                stop = False
                while sum(len(t) + 2 for t in texts) < 3500:
                    timeout = deadline - asyncio.get_running_loop().time()
                    if timeout <= 0:
                        break
                    try:
                        nxt = await asyncio.wait_for(self.queue.get(), timeout)
                    except asyncio.TimeoutError:
                        break
                    if nxt is None:
                        stop = True
                        break
                    if nxt[0] == "file":
                        await self._send_texts(texts)
                        texts = []
                        await self._send_file(*nxt[1])
                        continue
                    texts.append(nxt[1])
                await self._send_texts(texts)
                if stop:
                    return
            except Exception:  # the notifier must never die
                log.exception("dump notifier failed to deliver an event")

    async def _send_texts(self, texts: list[str]) -> None:
        """Batched events, split only **between** events (a cut inside an HTML tag makes Telegram reject the
        whole message); an event that is too long or has broken HTML is sent as plain text instead."""
        chunk: list[str] = []
        for text in texts:
            if chunk and sum(len(x) + 2 for x in chunk) + len(text) > 4000:
                await self._send_chunk(chunk)
                chunk = []
            chunk.append(text)
        if chunk:
            await self._send_chunk(chunk)

    async def _send_chunk(self, events: list[str]) -> None:
        body = "\n\n".join(events)
        if len(body) <= 4096:
            try:
                await self.sender.send_message(self.chat_id, body, priority=LOG, link_preview_options=NO_PREVIEW)
                return
            except tg_errors.BadRequest as e:  # e.g. ENTITY parse error from a truncated tag
                log.info("dump batch not sent as HTML (%r)", e)
                if len(events) > 1:  # find the bad one: the others keep their formatting
                    for ev in events:
                        await self._send_chunk([ev])
                    return
        await self._send_plain(body)

    async def _send_plain(self, body: str) -> None:
        plain = html.unescape(re.sub(r"<[^>]+>", "", body))
        for i in range(0, len(plain), 4000):
            await self.sender.send_message(self.chat_id, plain[i:i + 4000], priority=LOG,
                                           parse_mode=enums.ParseMode.DISABLED, link_preview_options=NO_PREVIEW)

    async def _send_file(self, path: Path, caption: str, delete_after: bool) -> None:
        try:
            await self.sender.send_document(self.chat_id, str(path), caption=caption[:1024], priority=LOG)
            if delete_after:
                path.unlink(missing_ok=True)
        except FileNotFoundError:
            log.warning("dump file vanished: %s", path)
