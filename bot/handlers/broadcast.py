"""Admin messaging (spec §22, §23): /bc (broadcast), /stopbc, /msg (message one user).

Depends on: ``broadcast.bc`` (engine), ``core.access.guard``, ``tasks.session``, ``ui``. Used by: ``handlers/__init__.py``.

Flow (/bc): collect any number of messages of any type (albums stay albums) → ``✅ Done`` → one card: who gets it
(`All users` / `Groups` where the bot is admin / `IDs from a file` — .txt one ID per line or a JSON list), `👀 Preview
to me`, `▶️ Start` → "Send N messages to M users?" `✅ Yes, send now` → live progress (sent / blocked / failed / ETA)
→ report to the admin and the dump. One broadcast at a time; /stopbc stops it (any admin). /cancel closes the
card while it is still being prepared.

Session steps: ``bc:collect`` · ``bc:card`` · ``bc:ids`` (waiting for the file) · ``bc:confirm`` · ``msg:collect``.
Messages sent while a step is waiting for them are caught by ``handlers.waits`` (handler group −1) before the
exam input handlers see them.
"""

from __future__ import annotations

import asyncio
import html
import json
import re

from pyrogram import enums

from bot.broadcast import bc
from bot.core.access import guard
from bot.core.bg import spawn
from bot.core.errors import UserBlockedBot
from bot.handlers.waits import WAITERS
from bot.ui.keyboards import btn, cb, kb, parse_cb
from bot.ui.progress import ProgressMessage
from bot.ui.texts import t

_IDLE_S = 600  # the card closes after 10 minutes without a press


# ------------------------------------------------------------------ /bc
@guard("admin", force_sub=False, private_only=True)
async def bc_command(client, message) -> None:
    app, uid = client.app, message.from_user.id
    if getattr(app, "broadcast", None):
        return await app.sender.send_message(message.chat.id, "📢 A broadcast is already running — /stopbc stops it.")
    if app.tasks.active_for(uid):
        return await app.sender.send_message(message.chat.id, "You have a task open — finish it or /cancel it first.")
    app.sessions.clear(uid)
    sess = app.sessions.get(uid)
    sess.step, sess.data["bc_items"] = "bc:collect", []
    sess.data["bc_target"], sess.data["bc_ids"] = "users", []
    msg = await app.sender.send_message(
        message.chat.id, "📢 <b>Broadcast</b>\nSend the messages to broadcast — text, photos, files, albums, anything.\n"
        "They are sent exactly as you send them. Tap <b>✅ Done</b> when finished.", reply_markup=_collect_markup(uid))
    sess.ui_message_id = msg.id
    _arm(app, message.chat.id, uid)


def _collect_markup(uid: int, n: int = 0):
    rows = [[btn(t("btn_done"), cb("bc", uid, "", "done"), style="success")]] if n else []
    return kb(*rows, [btn(t("btn_cancel"), cb("bc", uid, "", "cancel"), style="danger")])


def _arm(app, chat_id: int, uid: int) -> None:
    async def expire(u: int) -> None:
        s = app.sessions.peek(u)
        if s and (s.step.startswith("bc:") or s.step.startswith("msg:")):
            mid = s.ui_message_id
            app.sessions.clear(u)
            if mid:
                await app.sender.edit_text(chat_id, mid, "⌛ <b>Closed</b> — no answer for a while. Send /bc again any time.")
    app.sessions.arm_timeout(uid, _IDLE_S, expire)


async def _take_message(app, message, sess) -> None:
    """``bc:collect``: one more message to broadcast (album parts join their album)."""
    uid = message.from_user.id
    items = sess.data["bc_items"]
    mg = message.media_group_id
    if mg and items and items[-1].get("group_id") == mg:
        items[-1]["ids"].append(message.id)
    else:
        items.append({"chat_id": message.chat.id, "ids": [message.id], "album": bool(mg), "group_id": mg})
    _arm(app, message.chat.id, uid)
    old = sess.data.get("bc_timer")
    if old:
        old.cancel()
    # albums and quick bursts arrive as many updates: one status message after they stop coming
    sess.data["bc_timer"] = asyncio.get_running_loop().call_later(
        1.2, lambda: spawn(_collect_status(app, message.chat.id, uid, sess), "bc-status"))


async def _collect_status(app, chat_id: int, uid: int, sess) -> None:
    if app.sessions.peek(uid) is not sess or sess.step != "bc:collect":
        return
    old = sess.ui_message_id
    n = len(sess.data["bc_items"])
    msg = await app.sender.send_message(chat_id, f"📥 <b>{n} message{'s' if n != 1 else ''} ready</b>\nSend more, or tap "
                                        "<b>✅ Done</b>.", reply_markup=_collect_markup(uid, n))
    sess.ui_message_id = msg.id
    if old:
        try:
            await app.sender.delete(chat_id, old)
        except Exception:
            pass


async def _take_ids(app, message, sess) -> None:
    doc = message.document
    if not doc or (doc.file_size or 0) > 20 * 1024 * 1024:
        return await app.sender.send_message(message.chat.id, "Send a <b>.txt</b> (one ID per line) or a <b>.json</b> "
                                             "list of IDs, up to 20 MB.")
    path = app.config.download_root / "tasks" / f"bc_ids_{message.from_user.id}"
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        await app.client.download_media(message, file_name=str(path))
        ids = parse_ids(await asyncio.to_thread(path.read_text, "utf-8", "ignore"))
    finally:
        path.unlink(missing_ok=True)
    if not ids:
        return await app.sender.send_message(message.chat.id, "No IDs found in that file. Send another one.")
    sess.data.update(bc_ids=ids, bc_target="ids")
    sess.step = "bc:card"
    await _show_card(app, message.chat.id, message.from_user.id, sess, new=True)


def parse_ids(text: str) -> list[int]:
    """IDs from a .txt (any separators) or JSON (a list of numbers, or of objects with ``id``/``_id``/``user_id``);
    order kept, duplicates dropped."""
    found: list[int] = []
    try:
        data = json.loads(text)
        rows = data if isinstance(data, list) else data.get("users") or data.get("ids") or [] if isinstance(data, dict) else []
        for r in rows:
            v = r.get("id", r.get("_id", r.get("user_id"))) if isinstance(r, dict) else r
            if isinstance(v, (int, str)) and re.fullmatch(r"-?\d{5,20}", str(v).strip()):
                found.append(int(v))
    except (ValueError, AttributeError):
        pass
    if not found:  # plain text (or JSON of another shape): every number that looks like a Telegram ID
        found = [int(x) for x in re.findall(r"(?<![\d\w])-?\d{5,20}(?![\d\w])", text)]
    return list(dict.fromkeys(found))


# ------------------------------------------------------------------ the card
async def _show_card(app, chat_id: int, uid: int, sess, *, new: bool = False) -> None:
    target = sess.data["bc_target"]
    users, groups = await bc.count(app, "users"), await bc.count(app, "groups")
    n_ids = len(sess.data["bc_ids"])
    sess.data["bc_counts"] = {"users": users, "groups": groups, "ids": n_ids}
    n = len(sess.data["bc_items"])
    text = (f"📢 <b>Broadcast</b> · {n} message{'s' if n != 1 else ''} ready\n\n"
            "👇 Pick who gets it — <b>✅ green</b> is chosen. Then tap <b>Start</b>.\n"
            "<i>Users who blocked the bot and banned users are skipped.</i>")

    def pick(label, arg, on):
        return btn(("✅ " if on else "") + label, cb("bc", uid, "", arg), style="success" if on else None)

    markup = kb([btn("— 🎯 Send to —", cb("bc", uid, "", "hint"))],
                [pick(f"All users · {users:,}", "t_users", target == "users"),
                 pick(f"Groups · {groups:,}", "t_groups", target == "groups")],
                [pick(f"IDs from a file · {n_ids:,}" if n_ids else "📄 IDs from a file", "t_ids", target == "ids")],
                [btn("👀 Preview to me", cb("bc", uid, "", "preview"))],
                [btn("▶️ Start", cb("bc", uid, "", "start"), style="primary")],
                [btn("➕ Add more", cb("bc", uid, "", "more")), btn(t("btn_cancel"), cb("bc", uid, "", "cancel"), style="danger")])
    if new or not sess.ui_message_id:
        msg = await app.sender.send_message(chat_id, text, reply_markup=markup)
        sess.ui_message_id = msg.id
    else:
        await app.sender.edit_text(chat_id, sess.ui_message_id, text, reply_markup=markup)


@guard("admin", force_sub=False)
async def on_button(client, query) -> None:
    app, uid = client.app, query.from_user.id
    arg = parse_cb(query.data).arg
    chat_id, msg_id = query.message.chat.id, query.message.id
    sess = app.sessions.peek(uid)
    if sess is None or not sess.step.startswith("bc:") or sess.ui_message_id != msg_id:
        return await app.sender.answer(query.id, "This menu has expired.", show_alert=True)
    async with sess.lock:
        if app.sessions.peek(uid) is not sess:
            return await app.sender.answer(query.id)
        _arm(app, chat_id, uid)
        if arg == "cancel":
            app.sessions.clear(uid)
            await app.sender.answer(query.id)
            return await app.sender.edit_text(chat_id, msg_id, "✖️ <b>Broadcast cancelled.</b> Nothing was sent.")
        if arg == "hint":
            return await app.sender.answer(query.id, "Who receives the broadcast")
        if arg == "done" and sess.step == "bc:collect" and sess.data["bc_items"]:
            sess.step = "bc:card"
        elif arg == "more" and sess.step in ("bc:card", "bc:ids"):
            sess.step = "bc:collect"
            await app.sender.answer(query.id)
            n = len(sess.data["bc_items"])
            return await app.sender.edit_text(chat_id, msg_id, f"📥 <b>{n} message{'s' if n != 1 else ''} ready</b>\n"
                                              "Send more, or tap <b>✅ Done</b>.", reply_markup=_collect_markup(uid, n))
        elif arg in ("t_users", "t_groups") and sess.step in ("bc:card", "bc:ids"):
            sess.data["bc_target"], sess.step = arg[2:], "bc:card"
        elif arg == "t_ids" and sess.step in ("bc:card", "bc:ids"):
            if not sess.data["bc_ids"] or sess.data["bc_target"] == "ids":  # (again) → ask for a file
                sess.step = "bc:ids"
                await app.sender.answer(query.id)
                return await app.sender.send_message(chat_id, "📄 Send a <b>.txt</b> file (one user or group ID per line) "
                                                     "or a <b>.json</b> list of IDs.")
            sess.data["bc_target"] = "ids"
        elif arg == "preview" and sess.step in ("bc:card", "bc:ids"):
            await app.sender.answer(query.id, "Sending you a preview…")
            preview = bc.Broadcast(app, uid, sess.data["bc_items"], "ids", [chat_id])
            await preview._send(chat_id)
            sess.ui_message_id = None  # the card goes below the preview
            return await _show_card(app, chat_id, uid, sess, new=True)
        elif arg == "start" and sess.step == "bc:card":
            target = sess.data["bc_target"]
            n_chats = sess.data["bc_counts"][target]
            if not n_chats:
                return await app.sender.answer(query.id, "Nobody to send to.", show_alert=True)
            sess.step = "bc:confirm"
            n = len(sess.data["bc_items"])
            await app.sender.answer(query.id)
            return await app.sender.edit_text(
                chat_id, msg_id, f"📢 Send <b>{n} message{'s' if n != 1 else ''}</b> to <b>{n_chats:,}</b> "
                f"{'users' if target == 'users' else 'groups' if target == 'groups' else 'chats'}?\n"
                "<i>This can't be undone. /stopbc stops it midway.</i>",
                reply_markup=kb([btn("✅ Yes, send now", cb("bc", uid, "", "yes"), style="success")],
                                [btn(t("btn_back"), cb("bc", uid, "", "back"))]))
        elif arg == "back" and sess.step == "bc:confirm":
            sess.step = "bc:card"
        elif arg == "yes" and sess.step == "bc:confirm":
            if getattr(app, "broadcast", None):
                return await app.sender.answer(query.id, "Another broadcast is running — /stopbc first.", show_alert=True)
            await app.sender.answer(query.id)
            data = sess.data
            b = bc.Broadcast(app, uid, data["bc_items"], data["bc_target"], data["bc_ids"])
            app.broadcast = b
            app.sessions.clear(uid)
            app.broadcast_job = asyncio.get_running_loop().create_task(_run(app, b, chat_id, msg_id, query.from_user))
            return
        else:  # a press that does not fit the current step (an old card)
            return await app.sender.answer(query.id)
        await app.sender.answer(query.id)
        await _show_card(app, chat_id, uid, sess)


async def _run(app, b, chat_id: int, msg_id: int, user) -> None:
    progress = ProgressMessage(app.sender, chat_id, interval_s=max(5, app.settings.get("edit_interval_s") or 3))
    progress.attach(msg_id)
    who = f"{html.escape(user.first_name or '')} (<code>{user.id}</code>)"
    app.dump.notify(f"📢 <b>Broadcast started</b> by {who} · {bc.TARGETS[b.target]} · {len(b.items)} message(s)")
    try:
        progress.set(b.render())
        report = await b.run(progress)
        text = bc.report_text(report)
    except Exception as e:  # the engine never raises per chat — this is a bug or the DB failing
        text = f"⚠️ <b>Broadcast broke off</b> after {b.done:,} chats. {t('error_generic', error_id=app.dump.report_error(e, 'broadcast'))}"
    finally:
        app.broadcast = None
    await progress.finish(text)
    app.dump.notify(f"{text}\n👤 {who}")


# ------------------------------------------------------------------ /stopbc
@guard("admin", force_sub=False)
async def stopbc_command(client, message) -> None:
    app = client.app
    b = getattr(app, "broadcast", None)
    if not b:
        return await app.sender.send_message(message.chat.id, "No broadcast is running.")
    b.stop(f"{message.from_user.first_name or ''} ({message.from_user.id})".strip())
    await app.sender.send_message(message.chat.id, f"⏹ Stopping the broadcast — {b.sent:,} sent so far. The report follows.")


# ------------------------------------------------------------------ /msg
@guard("admin", force_sub=False, private_only=True)
async def msg_command(client, message) -> None:
    """``/msg <user id> <text>`` sends the text; ``/msg <user id>`` waits for one message of any type."""
    app, uid = client.app, message.from_user.id
    parts = (message.text or "").split(maxsplit=2)
    if len(parts) < 2 or not re.fullmatch(r"-?\d{1,20}", parts[1]):
        return await app.sender.send_message(message.chat.id, "✉️ <b>Message a user</b>\n<code>/msg 123456789 Hello!</code> "
                                             "sends the text.\n<code>/msg 123456789</code> — then send any message "
                                             "(photo, file…) and I copy it to them.")
    target = int(parts[1])
    doc = await app.repo.users.get(target, copy=False) if target > 0 else None
    name = html.escape((doc or {}).get("tg_db", {}).get("name") or str(target))
    if doc and doc.get("tg_db", {}).get("blocked_bot"):
        return await app.sender.send_message(message.chat.id, f"🚫 {name} has blocked the bot — can't message them.")
    if len(parts) == 3:  # exactly as typed: as HTML a "<" would cut the rest of the text off without an error
        return await _deliver(app, message.chat.id, target, name,
                              lambda: app.sender.send_message(target, parts[2], parse_mode=enums.ParseMode.DISABLED))
    if app.tasks.active_for(uid):
        return await app.sender.send_message(message.chat.id, "You have a task open — finish it or /cancel it first.")
    app.sessions.clear(uid)
    sess = app.sessions.get(uid)
    sess.step, sess.data["msg_to"], sess.data["msg_name"] = "msg:collect", target, name
    await app.sender.send_message(message.chat.id, f"✉️ Send the message for <b>{name}</b> — any type. /cancel to stop.")
    _arm(app, message.chat.id, uid)


async def _msg_send(app, message, sess) -> None:
    target, name = sess.data["msg_to"], sess.data["msg_name"]
    app.sessions.clear(message.from_user.id)
    await _deliver(app, message.chat.id, target, name, lambda: app.sender.copy(target, message.chat.id, message.id))


async def _deliver(app, chat_id: int, target: int, name: str, send) -> None:
    try:
        await send()
    except UserBlockedBot:
        return await app.sender.send_message(chat_id, f"🚫 {name} has blocked the bot — not delivered.")
    except Exception as e:
        return await app.sender.send_message(chat_id, f"⚠️ Not delivered to {name}: {html.escape(e.__class__.__name__)}")
    await app.sender.send_message(chat_id, f"✅ Delivered to <b>{name}</b>.")


WAITERS.update({"bc:collect": _take_message, "bc:ids": _take_ids, "msg:collect": _msg_send})
