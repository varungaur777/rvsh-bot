"""Everyone's handlers: /start home menu, /help, navigation, force-sub re-check, /cancel, /q (spec §6.9, §10).

Depends on: ``core.access.guard``, ``ui``. Used by: ``handlers/__init__.py``.
Feature screens that are not built yet show a "coming soon" page with Back/Home, so the menu is complete
from day one and each phase only replaces its screen.
"""

from __future__ import annotations

import html

from pyrogram import enums

from bot.core.access import REPLAYING, guard
from bot.core.sender import NO_PREVIEW
from bot.handlers import adminpanel, helpvideo
from bot.ui.keyboards import home_keyboard, kb, nav_row, parse_cb
from bot.ui.texts import t

_LANE_STATE = {0: "running now"}


def _home_text(app, user) -> str:
    text = t("home", name=html.escape(user.first_name or "there"))
    channel, admin = app.settings.get("channel_link"), app.settings.get("admin_contact")
    if channel or admin:
        text += t("home_links", channel=channel or "—", admin=admin or "—")
    return text


@guard(private_only=False, anon=True)
async def start(client, message) -> None:
    app = client.app
    parts = (message.text or "").split(maxsplit=1)
    param = parts[1].strip() if len(parts) > 1 else ""
    from bot.handlers import quiz
    if param and (message.from_user is not None or quiz.is_anon_admin(message)):  # deep links: quiz pages, group
        if await quiz.deep_link(client, message, param):  # quiz starts (anonymous admins too), leaderboards, Web App
            return
    if message.chat.type != enums.ChatType.PRIVATE:
        return await app.sender.send_message(message.chat.id, f"👋 Open me in private: https://t.me/{app.me.username}?start=go")
    uid = message.from_user.id
    await app.sender.send_message(message.chat.id, _home_text(app, message.from_user),
                                  reply_markup=home_keyboard(uid, app.access.is_admin(uid)))


def help_text(app, uid: int) -> str:
    """Everyone's help; admins also get their commands, the owner theirs."""
    text = t("help")
    if app.access.is_admin(uid):
        text += t("help_admin")
    if uid == app.config.owner_id:
        text += t("help_owner")
    return text


@guard()
async def help_cmd(client, message) -> None:
    uid = message.from_user.id
    await client.app.sender.send_message(message.chat.id, help_text(client.app, uid), reply_markup=kb(nav_row(uid)))


@guard()
async def nav(client, query) -> None:
    """``nav:<uid>:<screen>`` — edits the menu message in place."""
    app, data = client.app, parse_cb(query.data)
    uid = query.from_user.id
    if data.obj == "home":
        text, markup = _home_text(app, query.from_user), home_keyboard(uid, app.access.is_admin(uid))
    elif data.obj == "help":
        text, markup = help_text(app, uid), kb(nav_row(uid))
    elif data.obj == "exam":  # 📊 Exam Tools: no command needed — how to send a sheet (it said "coming soon")
        text, markup = t("exam_how_to"), kb(*helpvideo.rows(app, uid), nav_row(uid))
    elif data.obj == "admin" and app.access.is_admin(uid):
        text, markup = adminpanel.hub(app, uid)
    elif data.obj == "quiz":
        from bot.handlers import quiz
        text, markup = await quiz.list_screen(app, uid)
    elif data.obj == "stats":
        from bot.handlers import quiz
        text, markup = await quiz.my_stats(app, uid)
    elif data.obj.startswith("a_") and app.access.is_admin(uid):
        await app.sender.answer(query.id)
        shown = await adminpanel.open_tool(client, query, data.obj[2:])
        if shown is not None:
            await app.sender.edit_text(query.message.chat.id, query.message.id, shown[0], reply_markup=shown[1])
        return
    else:
        text, markup = t("coming_soon"), kb(nav_row(uid))
    await app.sender.answer(query.id)
    await app.sender.edit_text(query.message.chat.id, query.message.id, text, reply_markup=markup,
                               link_preview_options=NO_PREVIEW)


@guard()
async def fsub_recheck(client, query) -> None:
    """"I've joined" — the guard already re-checked membership live; re-run, in order, everything the user sent
    while not joined (B-024: several files → every file, not only the last)."""
    app = client.app
    uid = query.from_user.id
    await app.sender.answer(query.id, "✅ Thanks for joining!")
    try:
        await app.sender.delete(query.message.chat.id, query.message.id)
    except Exception:
        pass
    token = REPLAYING.set(True)
    try:
        for handler, update, _at in app.access.blocked_action.pop(uid, []):  # every file/command sent before joining
            await handler(client, update)
    finally:
        REPLAYING.reset(token)


@guard(force_sub=False)
async def cancel(client, message) -> None:
    app = client.app
    uid = message.from_user.id
    task = app.tasks.active_for(uid)
    if task:
        await app.tasks.cancel(task.task_id, "user")
        return await app.sender.send_message(message.chat.id, t("cancelled"))
    if app.sessions.peek(uid):
        app.sessions.clear(uid)
        return await app.sender.send_message(message.chat.id, t("cancelled"))
    await app.sender.send_message(message.chat.id, t("nothing_to_cancel"))


@guard(force_sub=False)
async def queue_cmd(client, message) -> None:
    app = client.app
    uid = message.from_user.id
    lines = []
    task = app.tasks.active_for(uid)
    if task:
        pos = app.tasks.position(task.task_id)
        state = "running now" if pos == 0 else f"queued at position {pos} in the {task.lane} lane"
        lines.append(t("queue_pos", task=task.task_id, kind=task.kind, state=state))
    else:
        lines.append(t("queue_none"))
    if app.access.is_admin(uid):
        for name, lane in app.tasks.lanes.items():
            lines.append(f"\n<b>{name}</b> · {len(lane.running)}/{lane.limit} running · {len(lane.queue)} queued")
            for tsk in [app.tasks.tasks[x] for x in lane.running if x in app.tasks.tasks]:
                lines.append(f"  ▶ <code>{tsk.task_id}</code> {tsk.kind} · user <code>{tsk.user_id}</code> · {int(tsk.elapsed)}s")
            for i, (_, _, tid) in enumerate(lane.queue[:15], 1):
                if tid in app.tasks.tasks:
                    q = app.tasks.tasks[tid]
                    lines.append(f"  {i}. <code>{tid}</code> {q.kind} · user <code>{q.user_id}</code>")
    await app.sender.send_message(message.chat.id, "\n".join(lines))
