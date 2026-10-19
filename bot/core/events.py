"""Chat-member events (spec §6.6, §6.4 force-sub invalidation).

Depends on: Kurigram types/enums, ``bot.app`` (via ``client.app``). Used by: ``handlers/__init__.py``.

Public API
----------
- ``on_chat_member(client, update)`` — one handler for every ``ChatMemberUpdated``:
  - someone left/was removed from a force-sub channel → their positive force-sub cache entry is dropped;
  - the bot was blocked in a private chat → ``app.on_user_blocked(user_id)`` (task cancel + cleanup);
  - the bot was added / promoted / demoted / removed in a group → ``groups`` doc updated, dump notified,
    and (P5) any running quiz there stopped when the bot can no longer run it.
- ``on_group_message(client, message)`` — any message the bot sees in a group it has no record of (it was added
  before ``groups`` existed, or the "added" event was missed) → the group is looked up and recorded, once per run.
  Bots cannot list their chats (owner 2026-10-13: "/groups mai vo list nhi kar rha jin group mai vo admin hai").
- ``await register_group(app, ref) -> (doc | None, problem)`` — look a group up by ID / @username / t.me link and
  record the bot's status there (``/groups`` → `➕ Add a group`, `🔄 Check all again`).
- ``await refresh_info(app, chat_id, force=False) -> doc | None`` — members, admins (names, bots) and, when the bot is
  an admin of a group without a public @username, its **private invite link** (the group's own link when Telegram
  shows it to the bot, else one link the bot makes once, named "Bot link" — never the primary link, so no link of
  the group stops working) → stored on the ``groups`` doc (owner 2026-10-13: "jab bot par admin right hai private
  link ke to use fetch kre aur show kare, group k total members and admins bhi"). Re-read at most every
  ``INFO_TTL_S`` unless ``force``; done after the bot is added / promoted / found, and when a group's card opens.
Never raises (errors are reported to dump) — except ``register_group``, which returns the problem.
"""

from __future__ import annotations

import html
import logging
import time

from pyrogram import enums

log = logging.getLogger(__name__)

_GONE = {enums.ChatMemberStatus.LEFT, enums.ChatMemberStatus.BANNED}
INFO_TTL_S = 600
MAX_ADMINS = 50


async def on_chat_member(client, upd) -> None:
    app = client.app
    if not app.ready.is_set():
        await app.ready.wait()
    try:
        new, chat = upd.new_chat_member, upd.chat
        member = (new or upd.old_chat_member)
        user = member.user if member else None
        if user is None:
            return
        status = new.status if new else enums.ChatMemberStatus.LEFT

        if status in _GONE and _is_force_sub_channel(app, chat):
            app.forcesub.invalidate(user.id)

        if user.id != app.me.id:
            return
        if chat.type == enums.ChatType.PRIVATE:
            if status in _GONE:
                await app.on_user_blocked(chat.id)
            return
        if chat.type not in (enums.ChatType.GROUP, enums.ChatType.SUPERGROUP):
            return
        await _bot_status_in_group(app, chat, status, new)
    except Exception as e:
        app.dump.report_error(e, "events.on_chat_member")


async def on_group_message(client, message) -> None:
    app = client.app
    chat = message.chat
    if chat is None or chat.type not in (enums.ChatType.GROUP, enums.ChatType.SUPERGROUP):
        return
    seen = getattr(app, "groups_seen", None)
    if seen is None:
        seen = app.groups_seen = set()
    if chat.id in seen:
        return
    seen.add(chat.id)
    try:
        if await app.repo.groups.get(chat.id, copy=False) is None:
            await register_group(app, chat.id, found=True)
    except Exception as e:
        app.dump.report_error(e, "events.on_group_message")


async def register_group(app, ref, found: bool = False) -> tuple[dict | None, str]:
    """``ref``: a chat ID, ``@username`` or ``t.me/name`` link. Records the group and the bot's status there."""
    if isinstance(ref, str):
        ref = ref.strip()
        ref = int(ref) if ref.lstrip("-").isdigit() else "@" + ref.rsplit("t.me/", 1)[-1].strip("/ ").lstrip("@")
    try:
        chat = await app.client.get_chat(ref)
    except Exception as e:
        return None, f"not found, or the bot is not in it ({e.__class__.__name__})"
    if chat.type not in (enums.ChatType.GROUP, enums.ChatType.SUPERGROUP):
        return None, "that is not a group"
    try:
        member = await app.client.get_chat_member(chat.id, app.me.id)
        status = member.status
    except Exception:
        member, status = None, enums.ChatMemberStatus.LEFT
    await _bot_status_in_group(app, chat, status, member, found=found)
    return await app.repo.groups.get(chat.id), ""


def _is_force_sub_channel(app, chat) -> bool:
    chans = app.settings.get("force_sub.channels") or []
    return chat.id in chans or (chat.username and (chat.username in chans or f"@{chat.username}" in chans))


async def _bot_status_in_group(app, chat, status, new, found: bool = False) -> None:
    is_admin = status in (enums.ChatMemberStatus.ADMINISTRATOR, enums.ChatMemberStatus.OWNER)
    present = status not in _GONE
    prev = await app.repo.groups.get(chat.id, copy=False) or {}
    rights = {}
    if is_admin and new is not None and new.privileges is not None:
        p = new.privileges
        rights = {k: bool(getattr(p, k, False)) for k in ("can_delete_messages", "can_pin_messages", "can_invite_users",
                                                          "can_restrict_members", "can_manage_chat")}
    link = f"https://t.me/{chat.username}" if chat.username else prev.get("link", "")
    await app.repo.groups.update(chat.id, {
        "$set": {"title": chat.title or "", "username": chat.username or "", "link": link, "present": present,
                 "bot_is_admin": is_admin, "rights": rights, "can_write": True},  # a refused send sets it false again
        "$setOnInsert": {"authorised": False, "banned": False, "auto_quiz": True, "lang_pref": "both",
                         "added_at": _now()},
    })
    title = html.escape(chat.title or str(chat.id))
    if not prev and present:
        what = "Found group" if found else "Added to group"
        app.dump.notify(f"👥 <b>{what}</b> {title}\nID: <code>{chat.id}</code> · admin: {'yes' if is_admin else 'no'}")
    elif not present:
        app.dump.notify(f"👋 <b>Removed from group</b> {title} (<code>{chat.id}</code>)")
    elif prev.get("bot_is_admin") != is_admin:
        app.dump.notify(f"{'⬆️ Promoted' if is_admin else '⬇️ Demoted'} in {title} (<code>{chat.id}</code>)")
    if not is_admin and app.on_group_lost is not None:  # quizzes need admin rights (spec §17.4)
        await app.on_group_lost(chat.id)
    if present:
        await refresh_info(app, chat.id, force=True)


async def refresh_info(app, chat_id: int, force: bool = False) -> dict | None:
    g = await app.repo.groups.get(chat_id, copy=False)
    if g is None or g.get("present") is False or (not force and time.time() - (g.get("info_ts") or 0) < INFO_TTL_S):
        return g
    c, upd = app.client, {"info_ts": time.time(), "info_at": _now()}
    chat = None
    try:
        chat = await c.get_chat(chat_id)
    except Exception as e:
        log.info("group %s: get_chat failed (%r)", chat_id, e)
    count = getattr(chat, "members_count", None)
    if not count:
        try:
            count = await c.get_chat_members_count(chat_id)
        except Exception as e:
            log.info("group %s: member count failed (%r)", chat_id, e)
    if count:
        upd["members_count"] = int(count)
    try:
        admins = []
        async for m in c.get_chat_members(chat_id, filter=enums.ChatMembersFilter.ADMINISTRATORS):
            u = m.user
            admins.append({"id": u.id, "name": " ".join(x for x in (u.first_name, u.last_name) if x) or str(u.id),
                           "username": u.username or "", "bot": bool(u.is_bot), "title": m.custom_title or "",
                           "owner": m.status == enums.ChatMemberStatus.OWNER})
        upd.update(admins=admins[:MAX_ADMINS], admins_count=len(admins))
    except Exception as e:
        log.info("group %s: admin list failed (%r)", chat_id, e)
    if g.get("bot_is_admin") and not (getattr(chat, "username", None) or g.get("username")):
        link = getattr(chat, "invite_link", None) or g.get("invite_link")
        if not link:
            try:  # an extra link, not the primary one: exporting would revoke the link the group already uses
                link = (await c.create_chat_invite_link(chat_id, name="Bot link")).invite_link
            except Exception as e:
                log.info("group %s: no invite link (%r)", chat_id, e)
        if link:
            upd["invite_link"] = link
    try:
        return await app.repo.groups.update(chat_id, {"$set": upd}, upsert=False)
    except Exception as e:
        app.dump.report_error(e, "events.refresh_info")
        return g


def _now() -> str:
    import time
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
