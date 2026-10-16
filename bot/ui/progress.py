"""Throttled progress messages and transfer progress bars (spec §6.3).

Depends on: ``core.sender``. Used by: every task (download → parse → render → upload), broadcast, batch.

Public API
----------
- ``bar(fraction, width=10)`` → ``"▰▰▰▱▱▱▱▱▱▱"``
- ``human_bytes(n)``, ``human_eta(seconds)``
- ``ProgressMessage(sender, chat_id, interval_s=3, task=None)`` (``task.stage`` follows ``set``) · ``await start(text, **kw)`` · ``set(text)`` (non-blocking) · ``set_markup(markup)`` (buttons kept on every edit) ·
  ``await finish(text, **kw)`` (always delivered) · ``close()`` (stop, no final text) ·
  ``transfer(kind, name, header=…)`` → async Kurigram ``progress=`` callback for one transfer.
- ``Transfers(on_change)`` — the universal transfer block: ``add(kind, name, total, unit) -> item``
  (``item.update(cur, total)`` sync · ``item.kurigram`` async ``progress=`` callback · ``item.done(ok)``),
  ``render()``, ``active``. Kinds: ``down`` ⬇️, ``up`` ⬆️, ``fetch`` 🌐.

Edit limits (owner 2026-10-12, Telegram Bot API FAQ: edits count like messages — ≈1/s per chat, ≈20/min per group or
channel, ≈30/s overall): an edit happens only when the visible text changed, at most once per ``interval_s`` in a
private chat and once per ``GROUP_MIN_INTERVAL_S`` (6 s) in groups/channels; the gap grows with the bot's global
send queue (many users at once) and doubles after a FloodWait (no retry — the newest text is sent later). Edits use
``PROGRESS`` priority, so answers to users and quiz polls go first. A background task coalesces rapid updates
(only the newest text is sent), so callers never wait for Telegram. ``finish()`` is always delivered.
"""

from __future__ import annotations

import asyncio
import html
import logging
import re
import time
from typing import Callable

from pyrogram import errors as tg_errors

from bot.core.sender import INTERACTIVE, PROGRESS

log = logging.getLogger(__name__)
GROUP_MIN_INTERVAL_S = 6.0  # ≤ 10 edits/min per group message: leaves room under ≈20 msgs/min for sends & polls


def bar(fraction: float, width: int = 10) -> str:
    f = min(max(fraction, 0.0), 1.0)
    filled = int(round(f * width))
    return "▰" * filled + "▱" * (width - filled)


def human_bytes(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"


def human_eta(seconds: float) -> str:
    s = int(max(seconds, 0))
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m {s % 60:02d}s"
    return f"{s // 3600}h {s % 3600 // 60:02d}m"


def stage_of(text: str) -> str:
    """First line of a progress text without tags/entities, ≤ 80 chars — what /task shows as the task's stage."""
    line = html.unescape(re.sub(r"<[^>]+>", "", (text or "").strip().split("\n", 1)[0])).strip()
    return line[:79] + "…" if len(line) > 80 else line


class ProgressMessage:
    def __init__(self, sender, chat_id: int, *, interval_s: float = 3.0, reply_to: int | None = None,
                 task=None) -> None:
        self.sender = sender
        self.task = task  # its ``stage`` follows the first line of the text (/task shows it)
        self.chat_id = chat_id
        # Telegram (Bot API FAQ): ≈1 message/s per chat, ≈20/min per group/channel, ≈30/s overall — edits count
        # like messages. Private chats: ≥ interval_s between edits; groups/channels: ≥ GROUP_MIN_INTERVAL_S.
        self.interval_s = interval_s if chat_id > 0 else max(interval_s, GROUP_MIN_INTERVAL_S)
        self._backoff = 0.0  # extra seconds after a FloodWait on an edit (doubles, ≤ 60 s, decays on success)
        self.reply_to = reply_to
        self.message_id: int | None = None
        self._wanted: str | None = None
        self._shown: str | None = None
        self._last_edit = 0.0
        self._changed = asyncio.Event()
        self._task: asyncio.Task | None = None
        self._closed = False
        self.markup = None  # buttons kept on every edit (an edit without them would remove them)

    def set_markup(self, markup) -> None:
        """Buttons for the next edits (``None`` removes them); the message is edited soon even if its text is the same."""
        self.markup = markup
        self._shown = None
        self._changed.set()

    async def start(self, text: str, **kw) -> int:
        """Send the message that will be edited; returns its id."""
        if self.reply_to:
            kw.setdefault("reply_to_message_id", self.reply_to)
        msg = await self.sender.send_message(self.chat_id, text, priority=INTERACTIVE, **kw)
        self.message_id = msg.id
        self._shown = self._wanted = text
        self._last_edit = time.monotonic()
        self._task = asyncio.get_running_loop().create_task(self._loop())
        return msg.id

    def attach(self, message_id: int, text: str = "") -> None:
        """Reuse an existing message (e.g. the preference card) as the progress message."""
        self.message_id = message_id
        self._shown = text
        self._last_edit = time.monotonic()
        if self._task is None:
            self._task = asyncio.get_running_loop().create_task(self._loop())

    def _interval(self) -> float:
        """Current minimum gap between edits: base · (1 + global queue / 25) + flood backoff, ≤ 60 s. When the
        bot's global send queue is long (many users at once), every progress message slows down by itself."""
        busy = getattr(getattr(self.sender, "global_bucket", None), "waiting", 0)
        return min(self.interval_s * (1 + busy / 25) + self._backoff, 60.0)

    def set(self, text: str) -> None:
        """Request a new text (returns immediately; the edit is throttled)."""
        if self._closed:
            return
        self._wanted = text
        self._changed.set()
        if self.task is not None:
            self.task.stage = stage_of(text)

    async def finish(self, text: str, **kw) -> None:
        """Final state — always delivered (waits for the throttle window, never skipped)."""
        self._closed = True
        if self._task:
            self._task.cancel()
        if self.message_id is None:
            await self.start(text, **kw)
            self._task and self._task.cancel()
            return
        wait = self._interval() - (time.monotonic() - self._last_edit)
        if wait > 0:
            await asyncio.sleep(wait)
        if text != self._shown or kw:
            await self.sender.edit_text(self.chat_id, self.message_id, text, priority=INTERACTIVE, **kw)
            self._shown = text

    def close(self) -> None:
        """Stop editing without a final text (task cancelled / session cleared); safe to call twice."""
        self._closed = True
        if self._task:
            self._task.cancel()
            self._task = None

    async def _loop(self) -> None:
        try:
            while not self._closed:
                await self._changed.wait()
                self._changed.clear()
                wait = self._interval() - (time.monotonic() - self._last_edit)
                if wait > 0:
                    await asyncio.sleep(wait)
                text = self._wanted  # only the newest text is sent: intermediate states are coalesced
                if text and text != self._shown and not self._closed:
                    try:  # no retry on FloodWait: a newer text will come anyway — back off instead
                        extra = {"reply_markup": self.markup} if self.markup is not None else {}
                        await self.sender.edit_text(self.chat_id, self.message_id, text, priority=PROGRESS, retries=0,
                                                    **extra)
                        self._shown = text
                        self._backoff = max(0.0, self._backoff / 2 - 1)
                    except tg_errors.FloodWait as e:
                        self._backoff = min(max(self._backoff * 2, float(e.value or 5)), 60.0)
                        log.info("progress edit flood-wait %ss in chat %s — slowing down", e.value, self.chat_id)
                    except Exception as e:  # progress must never break the task
                        log.debug("progress edit failed: %r", e)
                    self._last_edit = time.monotonic()
        except asyncio.CancelledError:
            pass

    def transfer(self, kind: str, name: str = "", *, header: str = "", total: int | None = None):
        """One transfer shown on this message (``header`` above the block). Returns the Kurigram ``progress=``
        callback (async, so it runs on the event loop — sync callbacks run in Kurigram's thread pool)."""
        t = Transfers(lambda: self.set(f"{header}\n\n{t.render()}" if header else t.render()))
        return t.add(kind, name, total).kurigram


# ---------------------------------------------------------------------- universal transfer block
_KINDS = {"down": ("⬇️", "Downloading"), "up": ("⬆️", "Uploading"), "fetch": ("🌐", "Fetching")}


class _Transfer:
    __slots__ = ("owner", "kind", "name", "unit", "current", "total", "ok", "finished")

    def __init__(self, owner: "Transfers", kind: str, name: str, total: int | None, unit: str) -> None:
        self.owner, self.kind, self.name, self.unit = owner, kind, name, unit
        self.current, self.total = 0, total
        self.ok, self.finished = True, False

    def update(self, current: int, total: int | None = None) -> None:
        """Sync update (event-loop code: HTTP downloads, fetcher)."""
        self.current = current
        if total:
            self.total = total
        self.owner._changed()

    async def kurigram(self, current: int, total: int, *_args) -> None:
        """``progress=`` callback for Kurigram downloads/uploads."""
        self.update(current, total)

    def done(self, ok: bool = True) -> None:
        if self.total and ok:
            self.current = self.total
        self.ok, self.finished = ok, True
        self.owner._changed()


class Transfers:
    """Every running download / upload / fetch of a task, rendered as **one** block (owner 2026-10-12: one
    good-looking universal progress bar)::

        ⬇️ Downloading · file 2 of 3
        ▰▰▰▰▰▰▱▱▱▱ 62%
        4.8 MB of 7.7 MB · ⚡ 1.9 MB/s · ⏳ 2s left

    Transfers of one kind are aggregated (bytes summed, speed from the last few seconds); a kind disappears
    when all its transfers are done. ``unit="items"`` counts things (e.g. page images) instead of bytes.
    ``on_change`` is called (sync, on the loop) after every update — usually ``ProgressMessage.set``.
    """

    def __init__(self, on_change: Callable[[], None] | None = None) -> None:
        self.on_change = on_change
        self.items: list[_Transfer] = []
        self._samples: dict[str, list[tuple[float, int]]] = {}

    def add(self, kind: str, name: str = "", total: int | None = None, unit: str = "B") -> _Transfer:
        if kind not in _KINDS:
            raise ValueError(kind)
        item = _Transfer(self, kind, name, total, unit)
        self.items.append(item)
        self._changed()
        return item

    @property
    def active(self) -> bool:
        return any(not i.finished for i in self.items)

    def _changed(self) -> None:
        now = time.monotonic()
        for kind in {i.kind for i in self.items}:
            s = self._samples.setdefault(kind, [])
            s.append((now, sum(i.current for i in self.items if i.kind == kind)))
            while len(s) > 2 and now - s[0][0] > 5:  # speed over the last ~5 s
                s.pop(0)
        if self.on_change:
            self.on_change()

    def _speed(self, kind: str) -> float:
        s = self._samples.get(kind) or []
        if len(s) < 2 or s[-1][0] - s[0][0] < 0.3:
            return 0.0
        return max(0.0, (s[-1][1] - s[0][1]) / (s[-1][0] - s[0][0]))

    def render(self) -> str:
        blocks = []
        for kind, (icon, verb) in _KINDS.items():
            group = [i for i in self.items if i.kind == kind]
            if not group or all(i.finished for i in group):
                continue
            n, done = len(group), sum(1 for i in group if i.finished)
            unit = group[0].unit
            noun = "file" if unit == "B" else "item"
            title = f"{icon} <b>{verb}</b>"
            if n > 1:
                title += f" · {noun} {min(done + 1, n)} of {n}"
            elif group[0].name:
                title += f" · {html.escape(group[0].name[:40])}"
            cur = sum(i.current for i in group)
            known = all(i.total for i in group)
            tot = sum(i.total or 0 for i in group)
            speed = self._speed(kind)
            lines = [title]
            if known and tot:
                frac = cur / tot
                lines.append(f"<code>{bar(frac, 12)}</code> <b>{int(frac * 100)}%</b>")
                amount = (f"{human_bytes(cur)} of {human_bytes(tot)}" if unit == "B"
                          else f"{cur} of {tot} {group[0].name or 'items'}")
                extra = []
                if unit == "B" and speed:
                    extra.append(f"⚡ {human_bytes(speed)}/s")
                if speed and cur < tot:
                    extra.append(f"⏳ {human_eta((tot - cur) / speed)} left")
                lines.append(" · ".join([amount, *extra]))
            else:
                lines.append(f"{human_bytes(cur)}" + (f" · ⚡ {human_bytes(speed)}/s" if speed else "")
                             if unit == "B" else f"{cur} {group[0].name or 'items'}")
            blocks.append("\n".join(lines))
        return "\n\n".join(blocks)

    def close(self) -> None:
        self.on_change = None
