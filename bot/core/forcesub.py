"""Force-subscribe checks (spec §6.4 step 4) — private chats only, never in groups.

Depends on: Kurigram, ``core.settings``, ``core.dump``. Used by: ``core.access``, ``core.events`` (invalidation).

Public API
----------
- ``ForceSub(client, settings, dump, extra=())`` · ``channels()`` — ``force_sub.channels`` (settings, /bot) plus
  ``FORCE_SUB_CHANNELS`` from ``.env`` (``extra``), without repeats · ``await missing(user_id) -> list[channel]``
  (empty = allowed) · ``invalidate(user_id)`` · ``clear_cache()`` (the list changed) ·
  ``await join_buttons(channels) -> list[(title, url)]`` · ``await check(bot_id) -> list[dict]`` (each channel:
  found? bot admin? link) and ``check_text(rows)`` for /bot and the boot check.
- Admins and the owner are never asked to join (``core.access``) — test with a normal account.

Rules
-----
- Only **positive** results are cached (``force_sub.cache_ttl_s``, default 600 s); a user who left is
  removed from the cache by ``chat_member`` updates (``invalidate``).
- Concurrent checks for one user share one in-flight lookup.
- Misconfiguration (bot not admin, channel gone, any API error) → the owner is alerted in dump (at most
  once per channel per hour) and the user is let through (**fail open**).
"""

from __future__ import annotations

import asyncio
import html
import logging
import time

from pyrogram import enums, errors as tg_errors

log = logging.getLogger(__name__)

_MEMBER = {enums.ChatMemberStatus.MEMBER, enums.ChatMemberStatus.ADMINISTRATOR, enums.ChatMemberStatus.OWNER}


class ForceSub:
    def __init__(self, client, settings, dump=None, extra=()) -> None:
        self.client = client
        self.settings = settings
        self.dump = dump
        self.extra = list(extra)  # FORCE_SUB_CHANNELS from .env
        self._ok_until: dict[int, float] = {}
        self._inflight: dict[int, asyncio.Future] = {}
        self._alerted: dict[str, float] = {}
        self._links: dict[str, tuple[str, str]] = {}

    def channels(self) -> list:
        out, seen = [], set()
        for ch in [*(self.settings.get("force_sub.channels") or []), *self.extra]:
            ch = normalize(ch)
            if ch is not None and str(ch).lower() not in seen:
                seen.add(str(ch).lower())
                out.append(ch)
        return out

    def invalidate(self, user_id: int) -> None:
        self._ok_until.pop(user_id, None)

    def clear_cache(self) -> None:
        """The channel list changed: everyone is checked again, links are looked up again."""
        self._ok_until.clear()
        self._links.clear()
        self._alerted.clear()

    async def check(self, bot_id: int) -> list[dict]:
        """Each must-join channel: ``{"channel", "title", "url", "ok", "problem"}`` — the bot must be an admin
        there to see who joined."""
        rows = []
        for ch in self.channels():
            row = {"channel": ch, "title": str(ch), "url": "", "ok": False, "problem": ""}
            try:
                chat = await self.client.get_chat(ch)
                row["title"] = chat.title or str(ch)
                row["url"] = f"https://t.me/{chat.username}" if chat.username else (chat.invite_link or "")
            except Exception as e:
                row["problem"] = f"channel not found or the bot is not in it ({e.__class__.__name__})"
                rows.append(row)
                continue
            try:
                m = await self.client.get_chat_member(ch, bot_id)
                if m.status in (enums.ChatMemberStatus.ADMINISTRATOR, enums.ChatMemberStatus.OWNER):
                    row["ok"] = True
                else:
                    row["problem"] = "the bot is not an admin there"
            except Exception as e:
                row["problem"] = f"the bot is not an admin there ({e.__class__.__name__})"
            rows.append(row)
        return rows

    async def missing(self, user_id: int) -> list:
        """Force-sub channels the user has not joined (``[]`` = may continue)."""
        chans = self.channels()
        if not chans or self._ok_until.get(user_id, 0) > time.monotonic():
            return []
        fut = self._inflight.get(user_id)
        if fut is not None:
            return await asyncio.shield(fut)
        fut = asyncio.get_running_loop().create_future()
        self._inflight[user_id] = fut
        try:
            result = [c for c, joined in zip(chans, await asyncio.gather(*(self._joined(c, user_id) for c in chans))) if not joined]
            if not result:
                self._ok_until[user_id] = time.monotonic() + float(self.settings.get("force_sub.cache_ttl_s") or 600)
            fut.set_result(result)
            return result
        except Exception as e:  # never lock users out because of our own failure
            fut.set_result([])
            log.warning("force-sub check failed: %r", e)
            return []
        finally:
            self._inflight.pop(user_id, None)

    async def _joined(self, channel, user_id: int) -> bool:
        try:
            m = await self.client.get_chat_member(channel, user_id)
            return m.status in _MEMBER or (m.status == enums.ChatMemberStatus.RESTRICTED and getattr(m, "is_member", False))
        except tg_errors.UserNotParticipant:
            return False
        except tg_errors.FloodWait:
            return True  # fail open, do not hammer the API
        except Exception as e:
            self._alert(channel, e)
            return True

    def _alert(self, channel, exc: Exception) -> None:
        key = str(channel)
        if self._alerted.get(key, 0) > time.monotonic():
            return
        self._alerted[key] = time.monotonic() + 3600
        log.warning("force-sub channel %s misconfigured: %r", channel, exc)
        if self.dump:
            self.dump.notify(f"⚠️ <b>Force-sub misconfigured</b> for <code>{channel}</code>: <code>{exc!r}</code>\n"
                             "Users are let through until it is fixed (make the bot an admin there).")

    async def join_buttons(self, channels: list) -> list[tuple[str, str]]:
        """(title, url) for each channel — username link, else an exported invite link (cached)."""
        out = []
        for ch in channels:
            key = str(ch)
            if key not in self._links:
                try:
                    chat = await self.client.get_chat(ch)
                    url = f"https://t.me/{chat.username}" if chat.username else (chat.invite_link or await self.client.export_chat_invite_link(ch))
                    self._links[key] = (chat.title or "Channel", url)
                except Exception as e:
                    self._alert(ch, e)
                    continue
            out.append(self._links[key])
        return out


def normalize(ch):
    """``-100…`` (number or text) → int; ``@name`` / ``t.me/name`` / ``name`` → ``"@name"``; empty → None."""
    if isinstance(ch, int):
        return ch
    s = str(ch or "").strip()
    if not s:
        return None
    if s.lstrip("-").isdigit():
        return int(s)
    s = s.rsplit("t.me/", 1)[-1].strip("/ ")
    return "@" + s.lstrip("@")


def check_text(rows: list[dict]) -> str:
    if not rows:
        return "📢 <b>Must-join channels</b>: none — everyone can use the bot without joining."
    lines = []
    for r in rows:
        name = html.escape(r["title"])
        link = f' · <a href="{html.escape(r["url"])}">link</a>' if r["url"] else ""
        lines.append(f"✅ {name}{link} — working" if r["ok"] else
                     f"⚠️ {name} (<code>{html.escape(str(r['channel']))}</code>) — {html.escape(r['problem'])}; "
                     "users are let through until the bot is made an admin there")
    return ("📢 <b>Must-join channels</b>\n<blockquote>" + "\n".join(lines) + "</blockquote>\n"
            "<i>Admins and the owner are never asked to join — test with a normal account.</i>")
