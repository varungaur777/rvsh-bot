"""Access-control middleware + safe handler wrapper (spec §6.2, §6.4, §6.5).

Depends on: ``core.forcesub``, ``core.errors``, ``core.dump``, ``db.repo``, ``ui``.
Used by: every handler through ``@guard(...)``.

Public API
----------
- ``Access(app)`` · ``blocked_action`` (re-run by the "I've joined" button) · ``role(user_id) -> "owner" | "admin" | "user"`` · ``priority(user_id)`` ·
  ``is_admin(user_id)`` · ``await ensure_user(user)`` · ``await group_authorised(chat)``
- Bot closed (``/off``): only admins / the owner — and everyone **inside an authorised group** (``/auth``, owner
  2026-10-13: "jab bot off ho to keval auth groups mai chle").
- ``guard(role="user", force_sub=True, private_only=False, shared=False)`` — decorator for message and
  callback handlers. Order (spec §6.4): new-user upsert → ban → public off → force-sub (private only) →
  role → per-user rate limit → callback owner check → run handler inside the error wrapper.

Exceptions → outcome (error wrapper)
------------------------------------
- ``BotError`` → friendly text ``error_<key>`` (or ``error_generic``) to the user; nothing raw.
- ``UserBlockedBot`` → silently ignored (cleanup already scheduled by the sender).
- anything else → dump report with a short error id; the user sees ``error_generic`` with that id.
- ``StopPropagation``/``ContinuePropagation`` from Kurigram pass through untouched.
"""

from __future__ import annotations

import contextlib
import contextvars
import functools
import html
import logging
import time

from pyrogram import StopPropagation, ContinuePropagation, enums
from pyrogram.types import CallbackQuery, Message

from bot.core.errors import BotError, UserBlockedBot
from bot.core.ratelimit import TokenBucket
from bot.ui.keyboards import btn, cb, kb, parse_cb
from bot.ui.texts import t

log = logging.getLogger(__name__)
_ROLE_RANK = {"user": 0, "admin": 1, "owner": 2}
REPLAYING: contextvars.ContextVar[bool] = contextvars.ContextVar("fsub_replay", default=False)  # "I've joined" re-runs
BLOCKED_KEEP_S, BLOCKED_MAX, ASK_AGAIN_S = 600, 30, 30  # force-sub: actions kept to re-run; one join message per burst


class Access:
    def __init__(self, app) -> None:
        self.app = app
        self._known: set[int] = set()
        self._seen_at: dict[int, float] = {}
        self._buckets: dict[int, TokenBucket] = {}
        # user → [(handler, update, time)] to re-run, in order, after "I've joined" (several files sent before joining)
        self.blocked_action: dict[int, list] = {}

    # ------------------------------------------------------------ roles
    def role(self, user_id: int | None) -> str:
        if user_id is None:
            return "user"
        if user_id == self.app.config.owner_id:
            return "owner"
        if user_id in (self.app.settings.get("admins") or []):
            return "admin"
        return "user"

    def is_admin(self, user_id: int | None) -> bool:
        return self.role(user_id) in ("admin", "owner")

    def priority(self, user_id: int) -> int:
        return {"owner": 0, "admin": 1}.get(self.role(user_id), 2)

    # ------------------------------------------------------------ users & groups
    async def ensure_user(self, user) -> dict | None:
        """Add a user on first contact (+ dump card); refresh name/last_seen at most once an hour."""
        if user is None or user.is_bot:
            return None
        uid, now = user.id, time.time()
        if uid in self._known and now - self._seen_at.get(uid, 0) < 3600:
            return await self.app.repo.users.get(uid, copy=False)
        name = " ".join(x for x in (user.first_name, user.last_name) if x) or "—"
        doc = await self.app.repo.users.get(uid, copy=False)
        is_new = doc is None
        doc = await self.app.repo.users.update(uid, {
            "$set": {"tg_db.name": name, "tg_db.username": user.username or "", "tg_db.last_seen": _iso(now),
                     "tg_db.blocked_bot": False},
            "$setOnInsert": {"tg_db.joined_at": _iso(now), "tg_db.banned": False, "tg_db.lang_pref": "ask",
                             "quiz_db": {"created": [], "attempted": []}, "exam_db": {}},
        })
        self._known.add(uid)
        self._seen_at[uid] = now
        if is_new:
            link = f'<a href="tg://user?id={uid}">{html.escape(name)}</a>'
            try:  # owner 2026-10-13: "new members join par, total kitne members hai ye bhi btana"
                total = f"\n👥 Total users: <b>{await self.app.repo.users.count():,}</b>"
            except Exception:
                total = ""
            self.app.dump.notify(f"👤 <b>New user</b>\n{link}\nID: <code>{uid}</code>\nUsername: "
                                 f"{'@' + user.username if user.username else '—'}\nJoined: {_iso(now)}{total}")
        return doc

    async def is_banned(self, user_id: int, chat=None) -> bool:
        doc = await self.app.repo.users.get(user_id, copy=False)
        if doc and doc.get("tg_db", {}).get("banned"):
            return True
        if chat is not None and chat.type in (enums.ChatType.GROUP, enums.ChatType.SUPERGROUP):
            g = await self.app.repo.groups.get(chat.id, copy=False)
            return bool(g and g.get("banned"))
        return False

    async def group_authorised(self, chat) -> bool:
        """``chat`` is a group the owner / an admin authorised (``/auth``): it keeps working while the bot is /off."""
        if chat is None or chat.type not in (enums.ChatType.GROUP, enums.ChatType.SUPERGROUP):
            return False
        g = await self.app.repo.groups.get(chat.id, copy=False)
        return bool(g and g.get("authorised"))

    def rate_ok(self, user_id: int) -> bool:
        """≈ 5 actions burst, 1 per second sustained per user (admins exempt)."""
        if self.is_admin(user_id):
            return True
        b = self._buckets.get(user_id)
        if b is None:
            if len(self._buckets) > 20000:
                self._buckets = {k: v for k, v in self._buckets.items() if v.idle_for < 300}
            b = self._buckets[user_id] = TokenBucket(1.0, 5)
        b._refill()
        if b.tokens >= 1:
            b.tokens -= 1
            b.last_used = time.monotonic()
            return True
        return False


def _iso(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


def guard(role: str = "user", *, force_sub: bool = True, private_only: bool = False, shared: bool = False,
          anon: bool = False):
    """Wrap a Kurigram handler ``(client, update)`` with access checks and error handling.

    ``shared=True`` lets anyone press a callback button (group quiz controls check permissions themselves).
    ``anon=True``: the handler also takes messages without a sender — an anonymous group admin (``sender_chat`` = the
    group) or a channel posting in a group. Other handlers never see them: an anonymous admin gets ``anon_admin``
    (who sent it is unknown), a channel nothing.
    """

    def deco(fn):
        @functools.wraps(fn)
        async def wrapper(client, update):
            app = client.app
            if not app.ready.is_set():  # concurrent boot: wait until DB/lease/lanes are up (milliseconds)
                await app.ready.wait()
            _track_lag(app, update)
            is_cb = isinstance(update, CallbackQuery)
            user = update.from_user
            chat = update.message.chat if is_cb and update.message else getattr(update, "chat", None)
            uid = user.id if user else None
            if is_cb and parse_cb(update.data or "") is None:  # a button whose data is not ours: never a crash
                log.warning("unreadable button data %r from %s", (update.data or "")[:64], uid)
                with contextlib.suppress(Exception):
                    await app.sender.answer(update.id, "⚠️ This button no longer works — open it again.", show_alert=True)
                return
            try:
                if uid is None:  # anonymous admin / channel post
                    if chat is not None and await app.access.is_banned(0, chat):
                        return
                    if anon and role == "user":
                        return await fn(client, update)
                    if role == "user" and not private_only and _anonymous_admin(update):
                        await app.sender.send_message(chat.id, t("anon_admin"), reply_to_message_id=update.id)
                    return
                await app.access.ensure_user(user)
                if await app.access.is_banned(uid, chat):
                    return
                acc = app.access
                if not app.settings.get("public_enabled") and not acc.is_admin(uid) \
                        and not await acc.group_authorised(chat):
                    return await _deny(app, update, t("public_off"))
                if private_only and chat is not None and chat.type != enums.ChatType.PRIVATE:
                    return
                if is_cb and not shared:
                    data = parse_cb(update.data or "")
                    if data and data.user_id != uid:
                        return await app.sender.answer(update.id, t("not_your_button"), show_alert=True)
                if _ROLE_RANK[acc.role(uid)] < _ROLE_RANK[role]:
                    return await _deny(app, update, t("admins_only"))
                # anti-spam for commands and buttons only (spec §6.4.6): files and pasted links of one exam often come
                # 5+ at once and must never be dropped (B-024); a re-run after "I've joined" is not counted again
                limited = is_cb or (getattr(update, "text", None) or "").startswith("/")
                if limited and not REPLAYING.get() and not acc.rate_ok(uid):
                    if is_cb:
                        return await app.sender.answer(update.id, t("slow_down"))
                    return
                if force_sub and (chat is None or chat.type == enums.ChatType.PRIVATE) and not acc.is_admin(uid):
                    missing = await app.forcesub.missing(uid)
                    if missing:
                        if is_cb and (update.data or "").startswith("fsub:"):
                            return await _ask_join(app, update, uid, missing)
                        now = time.monotonic()
                        pending = [p for p in acc.blocked_action.get(uid, []) if now - p[2] < BLOCKED_KEEP_S]
                        asked = bool(pending) and now - pending[-1][2] < ASK_AGAIN_S
                        acc.blocked_action[uid] = (pending + [(wrapper, update, now)])[-BLOCKED_MAX:]
                        if asked:  # a burst of files: one join message, every file kept
                            return
                        return await _ask_join(app, update, uid, missing)
                return await fn(client, update)
            except (StopPropagation, ContinuePropagation):
                raise
            except UserBlockedBot:
                return
            except BotError as e:
                if e.key in ("cancelled",):
                    return
                text = t(f"error_{e.key}")
                if text == f"error_{e.key}" or not text:
                    text = t("error_generic", error_id=app.dump.report_error(e, fn.__qualname__))
                await _deny(app, update, text)
            except Exception as e:
                error_id = app.dump.report_error(e, fn.__qualname__)
                await _deny(app, update, t("error_generic", error_id=error_id))

        return wrapper

    return deco


def _anonymous_admin(update) -> bool:
    """A message an anonymous admin sent in its group (Telegram shows the group itself as the sender)."""
    sc, chat = getattr(update, "sender_chat", None), getattr(update, "chat", None)
    return getattr(update, "from_user", None) is None and sc is not None and chat is not None and sc.id == chat.id


def _track_lag(app, update) -> None:
    """How long after sending did we start handling this message? Logged when > 3 s (diagnoses "late replies")."""
    date = getattr(update, "date", None)
    if date is None:
        return
    try:
        lag = time.time() - date.timestamp()
    except (AttributeError, OSError, ValueError):
        return
    app.max_update_lag = max(app.max_update_lag, lag)
    if lag > 3:
        log.warning("update handled %.1fs after it was sent (chat %s)", lag, getattr(getattr(update, "chat", None), "id", "?"))


async def _deny(app, update, text: str) -> None:
    """Tell the user why; this reply itself must never raise (it runs inside the error wrapper)."""
    try:
        if isinstance(update, CallbackQuery):
            plain = html.unescape(text).replace("<b>", "").replace("</b>", "").replace("<code>", "").replace("</code>", "")
            ok = await app.sender.answer(update.id, plain[:190], show_alert=True)
            if not ok and update.message is not None:  # press already answered (error came later): say it in chat
                await app.sender.send_message(update.message.chat.id, text)
        elif isinstance(update, Message):
            await app.sender.send_message(update.chat.id, text, reply_to_message_id=update.id)
    except UserBlockedBot:
        pass
    except Exception as e:  # e.g. network stall — already reported by the caller; never escalate
        log.info("could not tell the user about an error: %r", e)


async def _ask_join(app, update, uid: int, missing: list) -> None:
    links = await app.forcesub.join_buttons(missing)
    rows = [[btn(f"📢 {title}", url=url)] for title, url in links]
    rows.append([btn(t("btn_joined"), cb("fsub", uid), style="success")])
    if isinstance(update, CallbackQuery):
        await app.sender.answer(update.id, t("force_sub_still"), show_alert=False)
        await app.sender.send_message(update.message.chat.id, t("force_sub"), reply_markup=kb(*rows))
    else:
        await app.sender.send_message(update.chat.id, t("force_sub"), reply_markup=kb(*rows), reply_to_message_id=update.id)
