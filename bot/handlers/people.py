"""Admins, groups and users (spec §23): /admin, /groups, /user.

Depends on: ``core.access.guard``, ``core.settings`` (``admins``), ``db.repo`` (users, groups), ``handlers.waits``.
Used by: ``handlers/__init__.py``, the Admin Panel.

- **/admin** — the owner and every admin (name, username, id). The owner adds (`➕ Add admin` → send an ID,
  @username or a forwarded message → "Make X an admin?") and removes (`✖️ <name>` → "Sure?"); admins only see the
  list. Command menus are refreshed for the person changed.
- **/groups** — every group the bot is in (🤖 bot is admin · 👥 member · ⛔ banned · 👋 left) with its member count,
  8 per page; a group's card shows its link — the public one, or (bot is admin) the **private invite link**
  (``events.refresh_info``) —, members, admins (count + names, bots marked), the bot's rights, authorised / banned
  with one button each (`✅ Authorise` ⇄ `✖️ Remove authorisation`, `⛔ Ban group` ⇄ `✅ Unban group` — the label
  always says what the tap does) and `🔄 Refresh info` (owner 2026-10-13).
- **/user <id | @username>** (also as a reply to the user's message, or `👤 Find a user` on /groups) — everything
  about a user (owner 2026-10-13: "user command bhi dalo jisse uski sari info mil jae, even konse groups mai hai ye
  bhi"): Telegram profile (name, @username, premium, DC, bio, scam/fake flags), role, joined / last seen, blocked the
  bot, banned, current task, exams (name, date, shift, roll numbers, last marks), quizzes, and **the groups they are
  in** (each known group checked live, ≤ ``MAX_GROUP_CHECKS``, cached ``GROUPS_TTL_S``); a user who never started
  the bot is shown from Telegram alone. `⛔ Ban` / `✅ Unban` (admins and the owner cannot be banned; a ban cancels
  their task), `✉️ Message` (→ the /msg step), `🔄 Refresh`, `📄 Full report (.txt)` (every exam result with marks,
  every link they sent, every group) — sent to the asking admin only (personal data, never to the dump).

Callback data: ``adm:<admin>:<target>:<arg>`` · ``grp:<admin>:<chat or user id>:<arg>``.
"""

from __future__ import annotations

import asyncio
import html
import re
import time
from pathlib import Path

from pyrogram import enums

from bot.core.access import guard
from bot.handlers.waits import WAITERS
from bot.ui.keyboards import btn, cb, kb, parse_cb
from bot.ui.texts import t

PAGE = 8
MAX_GROUP_CHECKS = 60
GROUPS_TTL_S = 300
_ROLE = {enums.ChatMemberStatus.OWNER: "owner", enums.ChatMemberStatus.ADMINISTRATOR: "admin",
         enums.ChatMemberStatus.MEMBER: "member", enums.ChatMemberStatus.RESTRICTED: "restricted"}


def _who(doc: dict | None, uid: int) -> str:
    tg = (doc or {}).get("tg_db") or {}
    name = html.escape(tg.get("name") or "—")
    user = f" · @{html.escape(tg['username'])}" if tg.get("username") else ""
    return f'<a href="tg://user?id={uid}">{name}</a>{user} · <code>{uid}</code>'


async def resolve_user(app, text: str, forward_from=None) -> int | None:
    """A user from an admin's reply: an ID, ``@username``, or a forwarded message's sender (when they allow it)."""
    if forward_from is not None:
        return forward_from.id
    text = (text or "").strip()
    if re.fullmatch(r"\d{1,20}", text):
        return int(text)
    if re.fullmatch(r"@?[A-Za-z][A-Za-z0-9_]{3,31}", text):
        try:
            return (await app.client.get_users(text.lstrip("@"))).id
        except Exception:
            return None
    return None


def _forwarded_sender(message):
    """A forwarded message's sender when they allow it (``forward_origin``; ``forward_from`` is deprecated)."""
    return getattr(getattr(message, "forward_origin", None), "sender_user", None)


def _from_message(app, message):
    return resolve_user(app, message.text or message.caption or "", _forwarded_sender(message))


async def _refresh_commands(app) -> None:
    fn = getattr(app, "_set_commands", None)
    if fn is not None:
        try:
            await fn()
        except Exception:
            pass


# ================================================================== /admin
async def admin_screen(app, viewer: int):
    owner = app.config.owner_id
    admins = [a for a in (app.settings.get("admins") or []) if a != owner]
    rows = [f"👑 {_who(await app.repo.users.get(owner, copy=False), owner)}  <i>owner</i>"]
    for i, a in enumerate(admins, 1):
        rows.append(f"{i}. {_who(await app.repo.users.get(a, copy=False), a)}")
    text = f"👮 <b>Admins</b> · {len(admins)}\n<blockquote>" + "\n".join(rows) + "</blockquote>"
    buttons = []
    if viewer == owner:
        for a in admins:
            name = (((await app.repo.users.get(a, copy=False)) or {}).get("tg_db") or {}).get("name") or str(a)
            buttons.append([btn(f"✖️ Remove {name[:24]}", cb("adm", viewer, str(a), "rm"))])
        buttons.append([btn("➕ Add admin", cb("adm", viewer, "", "add"), style="success")])
        text += "\nAdmins can use every admin command. Only you (the owner) add or remove them."
    else:
        text += "\n<i>Only the owner adds or removes admins.</i>"
    return text, kb(*buttons, [btn(t("btn_home"), cb("nav", viewer, "home"))])


@guard("admin", force_sub=False)
async def admin_command(client, message) -> None:
    text, markup = await admin_screen(client.app, message.from_user.id)
    await client.app.sender.send_message(message.chat.id, text, reply_markup=markup)


@guard("admin", force_sub=False)
async def admin_button(client, query) -> None:
    app, uid = client.app, query.from_user.id
    data = parse_cb(query.data)
    chat_id, msg_id = query.message.chat.id, query.message.id
    if uid != app.config.owner_id and data.arg != "back":
        return await app.sender.answer(query.id, "Only the owner can change admins.", show_alert=True)
    target = int(data.obj) if data.obj.lstrip("-").isdigit() else None
    admins = list(app.settings.get("admins") or [])
    note = None
    if data.arg == "add":
        sess = app.sessions.get(uid)
        sess.step, sess.ui_message_id = "adm:add", msg_id
        await app.sender.answer(query.id)
        return await app.sender.edit_text(chat_id, msg_id, "➕ <b>Add admin</b>\nSend their user ID, @username, or forward "
                                          "a message from them.", reply_markup=kb([btn(t("btn_back"), cb("adm", uid, "", "back"))]))
    if data.arg == "rm" and target is not None:
        await app.sender.answer(query.id)
        who = _who(await app.repo.users.get(target, copy=False), target)
        return await app.sender.edit_text(chat_id, msg_id, f"✖️ Remove {who} from admins?\n<i>They become a normal user.</i>",
                                          reply_markup=kb([btn("✅ Yes, remove", cb("adm", uid, str(target), "rmy"), style="danger")],
                                                          [btn(t("btn_back"), cb("adm", uid, "", "back"))]))
    if data.arg == "rmy" and target in admins:
        admins.remove(target)
        await app.settings.set("admins", admins, by=uid)
        await _drop_commands(app, target)
        app.dump.notify(f"👮 Admin removed: <code>{target}</code> by owner")
        note = "Removed."
    elif data.arg == "addy" and target is not None and target not in admins and target != app.config.owner_id:
        admins.append(target)
        await app.settings.set("admins", admins, by=uid)
        await _refresh_commands(app)
        app.dump.notify(f"👮 Admin added: <code>{target}</code> by owner")
        note = "Added — they can use admin commands now."
    sess = app.sessions.peek(uid)
    if sess and sess.step == "adm:add":
        app.sessions.clear(uid)
    await app.sender.answer(query.id, note)
    text, markup = await admin_screen(app, uid)
    await app.sender.edit_text(chat_id, msg_id, text, reply_markup=markup)


async def _drop_commands(app, uid: int) -> None:
    try:
        from pyrogram.types import BotCommandScopeChat
        await app.client.delete_bot_commands(scope=BotCommandScopeChat(uid))
    except Exception:
        pass


async def _take_admin(app, message, sess) -> None:
    uid = message.from_user.id
    target = await _from_message(app, message)
    if target is None:
        return await app.sender.send_message(message.chat.id, "I couldn't find that user. Send their numeric ID (they "
                                             "must have started the bot), an @username, or forward a message from them.")
    app.sessions.clear(uid)
    if target == app.config.owner_id or target in (app.settings.get("admins") or []):
        return await app.sender.send_message(message.chat.id, "They are already an admin.")
    who = _who(await app.repo.users.get(target, copy=False), target)
    await app.sender.send_message(message.chat.id, f"👮 Make {who} an admin?\n<i>They can use every admin command "
                                  "(broadcast, tasks, results, settings).</i>",
                                  reply_markup=kb([btn("✅ Yes, make admin", cb("adm", uid, str(target), "addy"), style="success")],
                                                  [btn(t("btn_back"), cb("adm", uid, "", "back"))]))


# ================================================================== /groups
def _group_icon(g: dict) -> str:
    if g.get("banned"):
        return "⛔"
    if g.get("present") is False:
        return "👋"
    return "🤖" if g.get("bot_is_admin") else "👥"


async def groups_screen(app, viewer: int, page: int = 0):
    groups = await app.repo.groups.find({}, projection={"title": 1, "bot_is_admin": 1, "banned": 1, "present": 1,
                                                        "members_count": 1})
    groups.sort(key=lambda g: (g.get("banned", False), g.get("present") is False, not g.get("bot_is_admin"),
                               (g.get("title") or "").lower()))
    n_admin = sum(1 for g in groups if g.get("bot_is_admin") and g.get("present") is not False)
    pages = max(1, -(-len(groups) // PAGE))
    page = max(0, min(page, pages - 1))
    text = (f"👥 <b>Groups</b> · {len(groups)}  ·  🤖 bot is admin in {n_admin}\n"
            "<i>🤖 bot is admin · 👥 member · ⛔ banned · 👋 left</i>\n\nTap a group to see or change it."
            if groups else "👥 <b>Groups</b>\nNo group known yet.")
    text += ("\n<i>A group shows up here once the bot is added, or after any message in it. Telegram doesn't let "
             "a bot list its groups — use ➕ Add a group for one that is missing.</i>")
    rows = [[btn(f"{_group_icon(g)} {(g.get('title') or str(g['_id']))[:30]}"
                 + (f" · 👥 {g['members_count']:,}" if g.get("members_count") else ""), cb("grp", viewer, str(g["_id"]), "open"))]
            for g in groups[page * PAGE:(page + 1) * PAGE]]
    if pages > 1:
        rows.append([btn("◀️", cb("grp", viewer, "", f"p_{page - 1}")) if page else btn(" ", cb("grp", viewer, "", "noop")),
                     btn(f"{page + 1} / {pages}", cb("grp", viewer, "", "noop")),
                     btn("▶️", cb("grp", viewer, "", f"p_{page + 1}")) if page < pages - 1 else btn(" ", cb("grp", viewer, "", "noop"))])
    tools = [btn("➕ Add a group", cb("grp", viewer, "", "add"))]
    if groups:
        tools.append(btn("🔄 Check all again", cb("grp", viewer, "", "recheck")))
    rows += [tools, [btn("👤 Find a user", cb("grp", viewer, "", "find")), btn(t("btn_home"), cb("nav", viewer, "home"))]]
    return text, kb(*rows)


def _a(url: str) -> str:
    return f'<a href="{html.escape(url)}">{html.escape(url)}</a>'


async def group_card(app, viewer: int, chat_id: int, refresh: bool = False):
    from bot.core.events import refresh_info
    g = await refresh_info(app, chat_id, force=refresh)  # members, admins, invite link (re-read every 10 min)
    if g is None:
        return None
    title = html.escape(g.get("title") or str(chat_id))
    if g.get("link"):
        link = f"🔗 {_a(g['link'])}"
    elif g.get("invite_link"):
        link = f"🔗 {_a(g['invite_link'])}  <i>(private invite link)</i>"
    elif g.get("bot_is_admin"):
        link = "🔗 private group — no invite link (give the bot the “Invite users” right, then 🔄 Refresh info)"
    else:
        link = "🔗 private group — make the bot an admin to get its invite link"
    rights = ", ".join(k.replace("can_", "").replace("_", " ") for k, v in (g.get("rights") or {}).items() if v) or "—"
    yes = lambda v: "yes" if v else "no"  # noqa: E731
    admins = g.get("admins") or []
    n_bots = sum(1 for x in admins if x.get("bot"))
    people = f"👥 Members: <b>{g['members_count']:,}</b>" if g.get("members_count") else "👥 Members: ?"
    people += (f"  ·  👮 Admins: <b>{g.get('admins_count', len(admins))}</b>" + (f" ({n_bots} bot{'s' if n_bots != 1 else ''})"
                                                                               if n_bots else "")) if admins else "  ·  👮 Admins: ?"
    text = (f"{_group_icon(g)} <b>{title}</b>\nID <code>{chat_id}</code>\n{link}\n<blockquote>{people}\n"
            f"🤖 Bot is admin: <b>{yes(g.get('bot_is_admin'))}</b>  ·  still in it: <b>{yes(g.get('present') is not False)}</b>\n"
            f"🔑 Rights: {rights}\n"
            f"✅ Authorised: <b>{yes(g.get('authorised'))}</b> <i>(works while the bot is /off)</i>  ·  "
            f"⛔ Banned: <b>{yes(g.get('banned'))}</b>\n"
            f"🎲 Auto quiz: <b>{'on' if g.get('auto_quiz', True) else 'off'}</b>"
            f"{'' if app.settings.get('autoquiz.enabled') else ' <i>(off for all groups in /qz)</i>'}\n"
            f"📅 Added {html.escape((g.get('added_at') or '—')[:10])}"
            + (f"  ·  checked {html.escape(g['info_at'][11:16])} UTC" if g.get("info_at") else "") + "</blockquote>")
    if admins:
        rows = [f"{'👑' if x.get('owner') else '🤖' if x.get('bot') else '👮'} "
                f'<a href="tg://user?id={x["id"]}">{html.escape(x.get("name") or str(x["id"]))}</a>'
                + (f" @{html.escape(x['username'])}" if x.get("username") else "")
                + (f" · <i>{html.escape(x['title'])}</i>" if x.get("title") else "") for x in admins]
        text += "\n👮 <b>Admins</b>\n<blockquote expandable>" + "\n".join(rows) + "</blockquote>"
    sid = str(chat_id)
    auth = (btn("✖️ Remove authorisation", cb("grp", viewer, sid, "unauth")) if g.get("authorised")
            else btn("✅ Authorise", cb("grp", viewer, sid, "auth"), style="success"))
    ban = (btn("✅ Unban group", cb("grp", viewer, sid, "unban"), style="success") if g.get("banned")
           else btn("⛔ Ban group", cb("grp", viewer, sid, "ban"), style="danger"))
    aq = (btn("🎲 Turn auto quiz off", cb("grp", viewer, sid, "aqoff")) if g.get("auto_quiz", True)
          else btn("🎲 Turn auto quiz on", cb("grp", viewer, sid, "aqon"), style="success"))
    rows = [[auth]] if app.access.role(viewer) == "owner" else []  # owner only (owner 2026-10-13)
    return text[:4000], kb(*rows, [ban], [aq], [btn("🔄 Refresh info", cb("grp", viewer, sid, "gref"))],
                           [btn(t("btn_back"), cb("grp", viewer, "", "p_0"))])


@guard("owner", force_sub=False)
async def auth_command(client, message) -> None:
    """``/auth`` · ``/deauth`` (**owner only** — owner 2026-10-13: "auth dauth comand bhi owner only kro"; the group
    card's Authorise button too): in a group → that group; in private → ``/auth <ID |
    @username | t.me link>``, or the list of authorised groups. An authorised group keeps working while the bot is
    closed (``/off``) — its members can use the bot there and quizzes run there; elsewhere only admins."""
    from bot.core.events import register_group

    app = client.app
    uid = message.from_user.id
    on = (getattr(message, "command", None) or ["auth"])[0].lower() == "auth"
    parts = (message.text or "").split(maxsplit=1)
    arg = parts[1].strip() if len(parts) > 1 else ""
    in_group = message.chat.type in (enums.ChatType.GROUP, enums.ChatType.SUPERGROUP)
    if not arg and not in_group:
        rows = await app.repo.groups.find({"authorised": True}, projection={"title": 1, "present": 1})
        listing = "\n".join(f"• {html.escape(g.get('title') or str(g['_id']))} · <code>{g['_id']}</code>"
                            + (" · 👋 left" if g.get("present") is False else "") for g in rows) or "<i>none yet</i>"
        return await app.sender.send_message(
            message.chat.id, f"✅ <b>Authorised groups</b> · {len(rows)}\n<blockquote expandable>{listing}</blockquote>\n"
            "Send <code>/auth</code> inside a group, or <code>/auth -100… | @group | t.me link</code> here. "
            "<code>/deauth</code> the same way removes it.\n<i>Authorised groups keep working while the bot is /off.</i>")
    ref = arg or message.chat.id
    g = await app.repo.groups.get(int(ref), copy=False) if str(ref).lstrip("-").isdigit() else None
    if g is None:
        g, problem = await register_group(app, ref)
        if g is None:
            return await app.sender.send_message(message.chat.id, f"❌ Couldn't find that group: {html.escape(problem)}",
                                                 reply_to_message_id=message.id)
    gid = g["_id"]
    await app.repo.groups.update(gid, {"$set": {"authorised": on}}, upsert=False)
    title = html.escape(g.get("title") or str(gid))
    app.dump.notify(f"👥 {'Authorised' if on else 'Authorisation removed'}: <b>{title}</b> <code>{gid}</code> by <code>{uid}</code>")
    text = (f"✅ <b>{title}</b> is authorised — it keeps working while the bot is closed (/off)." if on
            else f"✖️ <b>{title}</b> is no longer authorised.")
    await app.sender.send_message(message.chat.id, text, reply_to_message_id=message.id)


@guard("admin", force_sub=False, private_only=True)  # private data: never in a group (owner 2026-10-14)
async def groups_command(client, message) -> None:
    text, markup = await groups_screen(client.app, message.from_user.id)
    await client.app.sender.send_message(message.chat.id, text, reply_markup=markup)


# ================================================================== users
async def tg_profile(app, target: int) -> dict:
    """What Telegram tells the bot about a user (empty when it can't see them). Bio needs a full lookup."""
    out: dict = {}
    try:
        u = await app.client.get_users(target)
    except Exception:
        return out
    if u is None:
        return out
    status = getattr(u, "verification_status", None)  # u.is_scam / is_fake / is_verified are deprecated (log noise)
    out.update(name=" ".join(x for x in (u.first_name, u.last_name) if x) or "—", username=u.username or "",
               premium=bool(getattr(u, "is_premium", False)), bot=bool(u.is_bot), dc=getattr(u, "dc_id", None),
               flags=[f for f in ("scam", "fake", "verified") if getattr(status, f"is_{f}", False)]
               + (["deleted"] if getattr(u, "is_deleted", False) else []))
    try:
        out["bio"] = (await asyncio.wait_for(app.client.get_chat(target), 8)).bio or ""
    except Exception:
        pass
    return out


async def user_groups(app, target: int, force: bool = False) -> dict:
    """The known groups this user is in, checked live (``get_chat_member`` per group, 5 at a time, ≤
    ``MAX_GROUP_CHECKS`` groups, 20 s at most) — Telegram does not let a bot list a user's groups. Cached
    ``GROUPS_TTL_S`` per user. ``{"in": [(group doc, role)], "checked": n, "total": n, "at": ts}``."""
    cache = app.__dict__.setdefault("_user_groups", {})
    hit = cache.get(target)
    if hit and not force and time.time() - hit["at"] < GROUPS_TTL_S:
        return hit
    groups = await app.repo.groups.find({"present": {"$ne": False}}, projection={"title": 1, "link": 1, "invite_link": 1})
    groups.sort(key=lambda g: (g.get("title") or "").lower())
    todo, sem, found, checked = groups[:MAX_GROUP_CHECKS], asyncio.Semaphore(5), [], [0]

    async def one(g: dict) -> None:
        async with sem:
            try:
                m = await app.client.get_chat_member(g["_id"], target)
            except Exception:  # not a member (UserNotParticipant), or the bot can't look
                checked[0] += 1
                return
            checked[0] += 1
            role = _ROLE.get(m.status)
            if role == "restricted" and not getattr(m, "is_member", True):
                role = None
            if role:
                found.append((g, role))

    jobs = [asyncio.ensure_future(one(g)) for g in todo]
    if jobs:
        _done, pending = await asyncio.wait(jobs, timeout=20)
        for j in pending:
            j.cancel()
    found.sort(key=lambda x: (x[0].get("title") or "").lower())
    res = cache[target] = {"in": found, "checked": checked[0], "total": len(groups), "at": time.time()}
    if len(cache) > 500:
        for k in sorted(cache, key=lambda k: cache[k]["at"])[:100]:
            cache.pop(k, None)
    return res


async def _exam_rows(app, doc: dict, limit: int | None = None) -> list[dict]:
    """The user's exams from ``exam_db`` (exam key → roll numbers, last marks), with name / date / shift from ``exams``."""
    rows = []
    for key, e in list((doc.get("exam_db") or {}).items())[:limit]:
        x = ((await app.repo.exams.get(key, copy=False)) or {}).get("exam") or {}
        rows.append({"key": key, "name": x.get("exam_name") or key.split("|")[0].replace("-", " ").title(),
                     "date": "-".join(reversed((x.get("date") or "").split("-"))), "shift": x.get("shift"),
                     "rolls": list((e or {}).get("roll_nos") or []), "marks": (e or {}).get("last_total")})
    return rows


def _flags(p: dict) -> str:
    bits = []
    if p.get("premium"):
        bits.append("⭐ premium")
    if p.get("dc"):
        bits.append(f"DC {p['dc']}")
    if p.get("bot"):
        bits.append("🤖 bot")
    bits += [f"⚠️ {f}" for f in p.get("flags") or []]
    return " · ".join(bits)


async def user_card(app, viewer: int, target: int, refresh: bool = False):
    doc = await app.repo.users.get(target, copy=False)
    prof = await tg_profile(app, target)
    if doc is None and not prof:
        return None
    tg = (doc or {}).get("tg_db") or {}
    if doc is None:  # never started the bot: name / username from Telegram
        doc = {"tg_db": {"name": prof.get("name"), "username": prof.get("username")}}
    role = app.access.role(target)
    yes = lambda v: "yes" if v else "no"  # noqa: E731
    quiz = doc.get("quiz_db") or {}
    lines = [f"🎖 Role: <b>{role}</b>" + (f"  ·  {_flags(prof)}" if _flags(prof) else "")]
    if prof.get("name") and prof["name"] != tg.get("name"):
        lines.append(f"🪪 On Telegram now: {html.escape(prof['name'])}" + (f" @{html.escape(prof['username'])}" if prof.get("username") else ""))
    if prof.get("bio"):
        lines.append(f"📝 Bio: {html.escape(prof['bio'][:200])}")
    if tg:
        lines += [f"📅 Joined {html.escape((tg.get('joined_at') or '—')[:10])}  ·  last seen {html.escape((tg.get('last_seen') or '—')[:16].replace('T', ' '))}",
                  f"🚫 Blocked the bot: <b>{yes(tg.get('blocked_bot'))}</b>  ·  ⛔ Banned: <b>{yes(tg.get('banned'))}</b>"]
    else:
        lines.append("ℹ️ <b>Has not started the bot</b> — nothing stored about them.")
    task = app.tasks.active_for(target)
    if task is not None:
        lines.append(f"⏳ Now: <b>{html.escape(task.kind)}</b> · {html.escape(task.status)} · <code>{task.task_id}</code>")
    exams = await _exam_rows(app, doc)
    n_rolls = sum(len(r["rolls"]) for r in exams)
    lines.append(f"📊 Exams: <b>{len(exams)}</b> ({n_rolls} result{'s' if n_rolls != 1 else ''})  ·  🎯 Quizzes made "
                 f"{len(quiz.get('created') or [])}, played {len(quiz.get('attempted') or [])}")
    grp = await user_groups(app, target, force=refresh)
    lines.append(f"👥 In <b>{len(grp['in'])}</b> of the bot's groups"
                 + (f"  <i>(checked {grp['checked']} of {grp['total']})</i>" if grp["checked"] < grp["total"] else ""))
    text = f"👤 {_who(doc, target)}\n<blockquote>" + "\n".join(lines) + "</blockquote>"
    if exams:
        rows = [f"{html.escape(r['name'][:40])} · {r['date'] or '?'}" + (f" · S{r['shift']}" if r["shift"] else "")
                + (f" · roll {', '.join(map(html.escape, r['rolls']))}" if r["rolls"] else "")
                + (f" · <b>{r['marks']:g}</b>" if isinstance(r["marks"], (int, float)) else "") for r in exams[:15]]
        if len(exams) > 15:
            rows.append(f"… {len(exams) - 15} more in the full report")
        text += "\n📊 <b>Exams</b>\n<blockquote expandable>" + "\n".join(rows) + "</blockquote>"
    if grp["in"]:
        rows = [f"{'👑' if r == 'owner' else '👮' if r == 'admin' else '👤'} {html.escape((g.get('title') or str(g['_id']))[:40])}"
                + (f" · {r}" if r != "member" else "") for g, r in grp["in"]]
        text += "\n👥 <b>Groups</b>\n<blockquote expandable>" + "\n".join(rows) + "</blockquote>"
    sid, rows = str(target), []
    if role == "user" and tg:
        rows.append([btn("✅ Unban", cb("grp", viewer, sid, "uunban"), style="success") if tg.get("banned")
                     else btn("⛔ Ban", cb("grp", viewer, sid, "uban"), style="danger")])
    if tg and not tg.get("blocked_bot"):
        rows.append([btn("✉️ Message", cb("grp", viewer, sid, "umsg"))])
    rows.append([btn("🔄 Refresh", cb("grp", viewer, sid, "uref")), btn("📄 Full report (.txt)", cb("grp", viewer, sid, "urep"))])
    return text[:4000], kb(*rows, [btn("👥 Groups", cb("grp", viewer, "", "p_0")), btn(t("btn_home"), cb("nav", viewer, "home"))])


async def user_report(app, target: int) -> str:
    """Everything about a user as plain text (📄 Full report): profile, stored record, every exam with each saved
    result (marks, right / wrong / skipped), every link they sent, every group they are in."""
    doc = await app.repo.users.get(target, copy=False) or {}
    tg, prof = doc.get("tg_db") or {}, await tg_profile(app, target)
    out = [f"User {target}", "=" * 40,
           f"Name: {tg.get('name') or prof.get('name') or '-'}",
           f"Username: @{tg.get('username') or prof.get('username')}" if (tg.get("username") or prof.get("username")) else "Username: -",
           f"Role: {app.access.role(target)}"]
    if prof:
        out.append("Telegram: " + (_flags(prof).replace("⭐ ", "").replace("🤖 ", "").replace("⚠️ ", "") or "-"))
        if prof.get("bio"):
            out.append(f"Bio: {prof['bio']}")
    if tg:
        out += [f"Joined: {tg.get('joined_at') or '-'}", f"Last seen: {tg.get('last_seen') or '-'}",
                f"Blocked the bot: {'yes' if tg.get('blocked_bot') else 'no'}", f"Banned: {'yes' if tg.get('banned') else 'no'}",
                f"Language: {tg.get('lang_pref') or '-'}"]
    else:
        out.append("Has not started the bot.")
    exams = await _exam_rows(app, doc)
    out += ["", f"EXAMS ({len(exams)})", "-" * 40]
    for r in exams:
        out.append(f"{r['name']} | {r['date'] or '?'} | shift {r['shift'] or '?'} | key {r['key']}")
        for roll in r["rolls"]:
            res = await app.repo.exam_results.get(f"{r['key']}|{roll}", copy=False) or {}
            a = res.get("aggregate") or {}
            out.append(f"   roll {roll}: " + (f"{a.get('marks', '?')} / {a.get('max_marks', '?')} · right {a.get('right', '?')} · "
                                             f"wrong {a.get('wrong', '?')} · skipped {a.get('unattempted', '?')} · {a.get('marking', '')}"
                                             if a else "no stored result"))
    links = await app.repo.exam_urls.find({"added_by": target}, projection={"url": 1, "exam_name": 1, "date": 1, "shift": 1,
                                                                            "first_seen": 1}, sort=[("first_seen", 1)])
    out += ["", f"LINKS SENT ({len(links)})", "-" * 40]
    out += [f"{(x.get('first_seen') or '')[:10]} | {x.get('exam_name') or '?'} | {x.get('date') or '?'} | "
            f"shift {x.get('shift') or '?'} | {x.get('url')}" for x in links]
    grp = await user_groups(app, target, force=True)
    out += ["", f"GROUPS ({len(grp['in'])}; checked {grp['checked']} of {grp['total']} known groups)", "-" * 40]
    out += [f"{g.get('title') or g['_id']} | {role} | {g['_id']} | {g.get('link') or g.get('invite_link') or '-'}"
            for g, role in grp["in"]]
    quiz = doc.get("quiz_db") or {}
    out += ["", f"QUIZZES: made {len(quiz.get('created') or [])}, played {len(quiz.get('attempted') or [])}"]
    return "\n".join(out) + "\n"


async def _send_report(app, chat_id: int, viewer: int, target: int) -> None:
    folder = Path(app.config.download_root) / "tasks"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"user_{target}_{int(time.time())}.txt"
    try:
        await asyncio.to_thread(path.write_text, await user_report(app, target), "utf-8")
        await app.sender.send_document(chat_id, str(path), caption=f"📄 Full report · user <code>{target}</code>",
                                       file_name=f"User {target}.txt")
    finally:
        path.unlink(missing_ok=True)


@guard("admin", force_sub=False, private_only=True)
async def user_command(client, message) -> None:
    app, uid = client.app, message.from_user.id
    parts = (message.text or "").split(maxsplit=1)
    reply = getattr(message, "reply_to_message", None)
    if len(parts) < 2 and reply is not None:  # /user as a reply: the replied message's sender (or forward source)
        who = _forwarded_sender(reply) or getattr(reply, "from_user", None)
        return await _send_user(app, message.chat.id, uid, who.id if who else None)
    if len(parts) < 2:
        return await _ask_user(app, message.chat.id, uid)
    target = await resolve_user(app, parts[1])
    await _send_user(app, message.chat.id, uid, target)


async def _ask_user(app, chat_id: int, uid: int) -> None:
    app.sessions.clear(uid)
    app.sessions.get(uid).step = "usr:find"
    await app.sender.send_message(chat_id, "👤 Send the user's ID, @username, or forward a message from them.")


async def _send_user(app, chat_id: int, uid: int, target: int | None) -> None:
    card = await user_card(app, uid, target) if target is not None else None
    if card is None:
        return await app.sender.send_message(chat_id, "No such user — not in the bot's records and Telegram doesn't show "
                                             "them to the bot. Try their @username, or forward a message from them.")
    await app.sender.send_message(chat_id, card[0], reply_markup=card[1])


async def _take_user(app, message, sess) -> None:
    app.sessions.clear(message.from_user.id)
    await _send_user(app, message.chat.id, message.from_user.id, await _from_message(app, message))


@guard("admin", force_sub=False)
async def groups_button(client, query) -> None:
    app, uid = client.app, query.from_user.id
    data = parse_cb(query.data)
    arg, obj = data.arg, data.obj
    chat_id, msg_id = query.message.chat.id, query.message.id
    target = int(obj) if obj.lstrip("-").isdigit() else None
    if arg == "noop":
        return await app.sender.answer(query.id)
    if arg == "find":
        await app.sender.answer(query.id)
        return await _ask_user(app, chat_id, uid)
    if arg.startswith("p_"):
        await app.sender.answer(query.id)
        text, markup = await groups_screen(app, uid, int(arg[2:]))
        return await app.sender.edit_text(chat_id, msg_id, text, reply_markup=markup)
    if arg == "add":
        app.sessions.clear(uid)
        sess = app.sessions.get(uid)
        sess.step, sess.data["grp_msg"] = "grp:add", msg_id
        await app.sender.answer(query.id)
        return await app.sender.edit_text(
            chat_id, msg_id, "➕ <b>Add a group</b>\nSend the group's ID (like <code>-1001234567890</code>), its "
            "@username or t.me link. The bot must already be in the group.\n<i>Tip: in the group, any message makes "
            "it show up by itself.</i>", reply_markup=kb([btn(t("btn_back"), cb("grp", uid, "", "p_0"))]))
    if arg == "recheck":
        await app.sender.answer(query.id, "Checking…")
        from bot.core.events import register_group
        for g in await app.repo.groups.find({}, projection={"_id": 1}):
            doc, problem = await register_group(app, g["_id"])
            if doc is None:  # gone, or the bot was removed
                await app.repo.groups.update(g["_id"], {"$set": {"present": False, "bot_is_admin": False}}, upsert=False)
        text, markup = await groups_screen(app, uid)
        return await app.sender.edit_text(chat_id, msg_id, text, reply_markup=markup)
    if arg == "urep" and target is not None:
        await app.sender.answer(query.id, "Making the report…")
        return await _send_report(app, chat_id, uid, target)
    note = None
    if target is not None and target < 0 and arg in ("aqon", "aqoff"):
        await app.repo.groups.update(target, {"$set": {"auto_quiz": arg == "aqon"}}, upsert=False)
        note = "Auto quiz on for this group." if arg == "aqon" else "Auto quiz off for this group."
    elif target is not None and target < 0 and arg in ("auth", "unauth") and app.access.role(uid) != "owner":
        return await app.sender.answer(query.id, "Only the owner can authorise groups.", show_alert=True)
    elif target is not None and target < 0 and arg in ("auth", "unauth", "ban", "unban"):
        field, value = ("authorised", arg == "auth") if arg in ("auth", "unauth") else ("banned", arg == "ban")
        await app.repo.groups.update(target, {"$set": {field: value}}, upsert=False)
        if arg == "ban" and getattr(app, "quiz", None) is not None:
            await app.quiz.stop_chat(target)
        note = {"auth": "Authorised.", "unauth": "Authorisation removed.", "ban": "Group banned — the bot ignores it now.",
                "unban": "Group unbanned."}[arg]
        app.dump.notify(f"👥 {note} <code>{target}</code> by <code>{uid}</code>")
    elif target is not None and arg in ("uban", "uunban"):
        if app.access.role(target) != "user":
            return await app.sender.answer(query.id, "Admins and the owner can't be banned.", show_alert=True)
        await app.repo.users.update(target, {"$set": {"tg_db.banned": arg == "uban"}}, upsert=False)
        if arg == "uban":
            task = app.tasks.active_for(target)
            if task:
                await app.tasks.cancel(task.task_id, f"admin {uid}: user banned")
            app.sessions.clear(target)
        note = "User banned — the bot ignores them now." if arg == "uban" else "User unbanned."
        app.dump.notify(f"👤 {note} <code>{target}</code> by <code>{uid}</code>")
    elif target is not None and arg == "umsg":
        app.sessions.clear(uid)
        sess = app.sessions.get(uid)
        name = html.escape((((await app.repo.users.get(target, copy=False)) or {}).get("tg_db") or {}).get("name") or str(target))
        sess.step, sess.data["msg_to"], sess.data["msg_name"] = "msg:collect", target, name
        await app.sender.answer(query.id)
        return await app.sender.send_message(chat_id, f"✉️ Send the message for <b>{name}</b> — any type. /cancel to stop.")
    elif arg in ("gref", "uref"):
        note = "Updated."
    await app.sender.answer(query.id, note)
    card = (await group_card(app, uid, target, refresh=arg == "gref") if target is not None and target < 0
            else await user_card(app, uid, target, refresh=arg == "uref") if target is not None else None)
    if card is not None:
        await app.sender.edit_text(chat_id, msg_id, card[0], reply_markup=card[1])


async def _take_group(app, message, sess) -> None:
    from bot.core.events import register_group
    uid, panel = message.from_user.id, sess.data.get("grp_msg")
    app.sessions.clear(uid)
    doc, problem = await register_group(app, (message.text or "").strip())
    if doc is not None:
        text, markup = await group_card(app, uid, doc["_id"])
        text = "✅ Group recorded.\n\n" + text
    else:
        text = f"⚠️ {html.escape(problem)}.\nAdd the bot to the group first, then try again."
        markup = kb([btn("➕ Try again", cb("grp", uid, "", "add"))], [btn(t("btn_back"), cb("grp", uid, "", "p_0"))])
    if panel:
        try:
            await app.sender.delete(message.chat.id, [message.id])
        except Exception:
            pass
        return await app.sender.edit_text(message.chat.id, panel, text, reply_markup=markup)
    await app.sender.send_message(message.chat.id, text, reply_markup=markup)


WAITERS.update({"adm:add": _take_admin, "usr:find": _take_user, "grp:add": _take_group})
