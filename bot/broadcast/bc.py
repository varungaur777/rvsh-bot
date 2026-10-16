"""Broadcast engine (spec §22): copy an admin's messages to every user, every group where the bot is admin, or a
list of IDs — through the central sender's lowest-priority lane, so users' own work always goes first.

Depends on: ``core.sender`` (``BULK`` priority, flood waits), ``db.repo`` (users, groups), ``ui.progress``.
Used by: ``handlers/broadcast.py`` (/bc, /stopbc).

Public API
----------
- ``Broadcast(app, by, items, target, ids=None)`` — ``items``: ``[{"chat_id", "ids": [msg ids], "album": bool}]``
  (an album is copied as one album); ``target``: ``"users" | "groups" | "ids"``.
- ``await count(app, target, ids=None) -> int`` — how many chats a target reaches (blocked/banned users and groups
  the bot left are skipped).
- ``await b.run(progress)`` → the report dict; ``b.stop(by_name)``; ``b.render()`` (live text); ``report_text(r)``.

Outcome per chat: sent · blocked (user blocked the bot / deactivated → ``tg_db.blocked_bot`` is set by the
sender's ``on_blocked`` hook; group the bot cannot write to → ``groups.present`` / ``can_write`` set false) ·
failed (anything else, logged). A broadcast never raises for one chat.
"""

from __future__ import annotations

import asyncio
import html
import logging
import time

from pyrogram import errors as tg_errors

from bot.core.errors import UserBlockedBot
from bot.core.sender import BULK
from bot.ui.progress import bar, human_eta

log = logging.getLogger(__name__)

_GONE = tuple(getattr(tg_errors, n) for n in ("ChannelPrivate", "ChannelInvalid", "ChatIdInvalid", "PeerIdInvalid",
                                               "UserBannedInChannel", "ChatInvalid") if hasattr(tg_errors, n))
_MUTED = tuple(getattr(tg_errors, n) for n in ("ChatWriteForbidden", "ChatRestricted", "ChatAdminRequired",
                                                "ChatSendPlainForbidden", "ChatSendMediaForbidden")
               if hasattr(tg_errors, n))
TARGETS = {"users": "all users", "groups": "groups (bot is admin)", "ids": "IDs from a file"}


def _filter(target: str) -> dict:
    if target == "users":
        return {"tg_db.blocked_bot": {"$ne": True}, "tg_db.banned": {"$ne": True}}
    return {"bot_is_admin": True, "banned": {"$ne": True}, "present": {"$ne": False}}


async def count(app, target: str, ids: list[int] | None = None) -> int:
    if target == "ids":
        return len(ids or [])
    coll = app.repo.users if target == "users" else app.repo.groups
    return await coll.count(_filter(target))


class Broadcast:
    def __init__(self, app, by: int, items: list[dict], target: str, ids: list[int] | None = None) -> None:
        self.app, self.by, self.items, self.target, self.ids = app, by, items, target, ids or []
        self.total = 0
        self.sent = self.blocked = self.failed = 0
        self.started = 0.0
        self.ended = 0.0
        self.stopped_by: str | None = None
        self._stop = asyncio.Event()

    @property
    def done(self) -> int:
        return self.sent + self.blocked + self.failed

    def stop(self, by: str) -> None:
        if not self._stop.is_set():
            self.stopped_by = by
            self._stop.set()

    async def _chats(self):
        if self.target == "ids":
            for cid in self.ids:
                yield cid
            return
        coll = self.app.repo.users if self.target == "users" else self.app.repo.groups
        async for doc in coll.stream(_filter(self.target), projection={"_id": 1}):
            yield doc["_id"]

    async def _send(self, chat: int) -> None:
        s, client = self.app.sender, self.app.client
        for it in self.items:
            if it.get("album"):
                await s.call(chat, client.copy_media_group, chat, it["chat_id"], it["ids"][0], priority=BULK)
            else:
                await s.copy(chat, it["chat_id"], it["ids"][0], priority=BULK)

    async def _one(self, chat: int) -> None:
        try:
            await self._send(chat)
            self.sent += 1
        except UserBlockedBot:  # the sender's on_blocked hook marks the user (tg_db.blocked_bot) and cleans up
            self.blocked += 1
        except _GONE as e:
            self.blocked += 1
            if chat < 0:
                await self._mark_group(chat, {"present": False}, e)
        except _MUTED as e:
            self.blocked += 1
            if chat < 0:
                await self._mark_group(chat, {"can_write": False}, e)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # FloodWait after all retries, network, a deleted source message …
            self.failed += 1
            log.info("broadcast to %s failed: %r", chat, e)

    async def _mark_group(self, chat: int, fields: dict, e: Exception) -> None:
        try:
            await self.app.repo.groups.update(chat, {"$set": {**fields, "last_error": e.__class__.__name__}}, upsert=False)
        except Exception:
            pass

    async def run(self, progress=None) -> dict:
        self.total = await count(self.app, self.target, self.ids)
        self.started = time.time()
        q: asyncio.Queue = asyncio.Queue(maxsize=200)
        n_workers = max(1, int(self.app.settings.get("broadcast.concurrency") or 20))

        async def produce():
            async for chat in self._chats():
                if self._stop.is_set():
                    break
                await q.put(chat)
            for _ in range(n_workers):
                await q.put(None)

        async def work():
            while (chat := await q.get()) is not None:
                if not self._stop.is_set():
                    await self._one(chat)
                    if progress is not None:
                        progress.set(self.render())

        try:
            await asyncio.gather(produce(), *(work() for _ in range(n_workers)))
        finally:
            self.ended = time.time()
        return self.report()

    def _rate(self) -> float:
        took = (self.ended or time.time()) - self.started
        return self.done / took if took > 0 else 0.0

    def render(self) -> str:
        total = max(self.total, self.done)
        frac = self.done / total if total else 1.0
        rate = self._rate()
        eta = f"  ·  ⏳ {human_eta((total - self.done) / rate)} left" if rate > 0 and total > self.done else ""
        return (f"📢 <b>Broadcasting</b> · {TARGETS[self.target]}\n"
                f"{bar(frac)} {frac * 100:.0f}%  ·  {self.done:,} / {total:,}\n"
                f"✅ Sent <b>{self.sent:,}</b>  ·  🚫 Blocked <b>{self.blocked:,}</b>  ·  ⚠️ Failed <b>{self.failed:,}</b>\n"
                f"⚡ {rate:.0f} per second{eta}\n\n<i>/stopbc stops it.</i>")

    def report(self) -> dict:
        return {"target": self.target, "messages": len(self.items), "total": self.total, "sent": self.sent,
                "blocked": self.blocked, "failed": self.failed, "seconds": round((self.ended or time.time()) - self.started),
                "stopped_by": self.stopped_by, "by": self.by}


def report_text(r: dict) -> str:
    head = f"⏹ <b>Broadcast stopped</b> by {html.escape(r['stopped_by'])}" if r.get("stopped_by") else "✅ <b>Broadcast finished</b>"
    return (f"{head}\n🎯 {TARGETS[r['target']]} · {r['messages']} message{'s' if r['messages'] != 1 else ''}\n"
            f"✅ Sent <b>{r['sent']:,}</b>  ·  🚫 Blocked <b>{r['blocked']:,}</b>  ·  ⚠️ Failed <b>{r['failed']:,}</b>"
            f"  ·  of {r['total']:,}\n⏱ {human_eta(r['seconds'])}")
