"""Admin task screens (spec §10, §23): /task — running, waiting and open tasks with who, what, stage and time, and
a cancel button for each; /cq — clear the queue or cancel everything. Every cancel asks "sure?" first, and the
task's user is told an admin cancelled it.

Depends on: ``tasks.lanes`` (``app.tasks``), ``core.access.guard``, ``ui``. Used by: ``handlers/__init__.py``,
the Admin Panel.

Callback data: ``tsk:<admin id>:<task id or "">:<arg>`` — ``ref`` · ``x`` (ask) · ``xy`` (cancel it) ·
``cq`` / ``cqy`` (clear queue) · ``ca`` / ``cay`` (cancel all) · ``back``.
"""

from __future__ import annotations

import html
import time

from bot.core.access import guard
from bot.ui.keyboards import btn, cb, kb, parse_cb
from bot.ui.progress import human_eta
from bot.ui.texts import t

KINDS = {"marks": "Marks", "custom_pdf": "Custom PDF", "original_pdf": "Original PDF", "results": "Result PDF",
         "exam": "Exam (choosing)", "html_quiz": "HTML quiz", "tg_quiz": "Quiz", "merge": "PDF merge",
         "clean": "PDF clean", "ocr_pdf": "OCR PDF", "batch": "Batch"}
_MAX_BUTTONS = 12


async def _name(app, uid: int) -> str:
    doc = await app.repo.users.get(uid, copy=False)
    return ((doc or {}).get("tg_db") or {}).get("name") or str(uid)


async def render(app, admin_id: int):
    tm = app.tasks
    running, queued, held = tm.running(), tm.queued(), tm.held()
    head = f"🗂 <b>Tasks</b>  ·  ▶️ {len(running)} running  ·  ⏳ {len(queued)} waiting  ·  ✋ {len(held)} open"
    blocks = []
    for title, group in (("▶️ Running", running), ("⏳ Waiting", queued), ("✋ Open — user is choosing", held)):
        if not group:
            continue
        rows = []
        for i, task in enumerate(group, 1):
            who = html.escape(await _name(app, task.user_id))
            since = task.started or task.created
            pos = f"#{tm.position(task.task_id)} · " if task in queued and tm.position(task.task_id) else ""
            line = (f"{i}. <b>{KINDS.get(task.kind, task.kind)}</b> · {task.lane} · {pos}<code>{task.task_id}</code>\n"
                    f"    👤 {who} (<code>{task.user_id}</code>) · ⏱ {human_eta(_now() - since)}")
            if task.stage:
                line += f"\n    {html.escape(task.stage)}"
            rows.append(line)
        blocks.append(f"\n<b>{title}</b>\n<blockquote expandable>" + "\n".join(rows) + "</blockquote>")
    text = head + ("".join(blocks) if blocks else "\n\n✅ Nothing running or waiting.")
    buttons = []
    for task in (running + queued + held)[:_MAX_BUTTONS]:
        who = await _name(app, task.user_id)
        buttons.append(btn(f"✖️ {KINDS.get(task.kind, task.kind)} · {who[:14]}", cb("tsk", admin_id, task.task_id, "x")))
    rows = [buttons[i:i + 2] for i in range(0, len(buttons), 2)]
    extra = []
    if queued:
        extra.append(btn(f"🧹 Clear queue ({len(queued)})", cb("tsk", admin_id, "", "cq")))
    if running or queued or held:
        extra.append(btn("⛔ Cancel all", cb("tsk", admin_id, "", "ca"), style="danger"))
    return text[:4000], kb(*rows, extra, [btn("🔄 Refresh", cb("tsk", admin_id, "", "ref"), style="primary"),
                                         btn(t("btn_home"), cb("nav", admin_id, "home"))])


def _now() -> float:
    return time.time()


@guard("admin", force_sub=False, private_only=True)  # private data: never in a group (owner 2026-10-14)
async def task_command(client, message) -> None:
    text, markup = await render(client.app, message.from_user.id)
    await client.app.sender.send_message(message.chat.id, text, reply_markup=markup)


@guard("admin", force_sub=False)
async def cq_command(client, message) -> None:
    app, uid = client.app, message.from_user.id
    n_q, n_all = len(app.tasks.queued()), len(app.tasks.queued() + app.tasks.running() + app.tasks.held())
    if not n_all:
        return await app.sender.send_message(message.chat.id, "✅ Nothing running or waiting.")
    await app.sender.send_message(message.chat.id, f"🧹 <b>Clear tasks</b>\n⏳ {n_q} waiting · {n_all} in all\n"
                                  "Pick one — I'll ask once more before doing it.", reply_markup=kb(
                                      [btn(f"🧹 Clear the queue ({n_q})", cb("tsk", uid, "", "cq"))] if n_q else [],
                                      [btn(f"⛔ Cancel everything ({n_all})", cb("tsk", uid, "", "ca"), style="danger")],
                                      [btn("🗂 Show tasks", cb("tsk", uid, "", "ref"))]))


def _sure(uid: int, yes: str, label: str, obj: str = ""):
    return kb([btn(label, cb("tsk", uid, obj, yes), style="danger")], [btn(t("btn_back"), cb("tsk", uid, "", "back"))])


@guard("admin", force_sub=False)
async def on_button(client, query) -> None:
    app, admin = client.app, query.from_user
    data = parse_cb(query.data)
    arg, tid = data.arg, data.obj
    chat_id, msg_id = query.message.chat.id, query.message.id
    by = f"admin {admin.first_name or ''} ({admin.id})".replace("  ", " ")
    note = None
    if arg == "x":
        task = app.tasks.tasks.get(tid)
        if task is None or task.closed:
            note = "That task has already ended."
        else:
            who = html.escape(await _name(app, task.user_id))
            await app.sender.answer(query.id)
            return await app.sender.edit_text(
                chat_id, msg_id, f"✖️ Cancel <b>{KINDS.get(task.kind, task.kind)}</b> of {who} "
                f"(<code>{task.task_id}</code>)?\n<i>They will be told an admin cancelled it.</i>",
                reply_markup=_sure(admin.id, "xy", "✅ Yes, cancel it", tid))
    elif arg == "xy":
        task = app.tasks.tasks.get(tid)
        if task is not None and not task.closed and await app.tasks.cancel(tid, by):
            await _tell_user(app, task)
            note = "Task cancelled."
        else:
            note = "That task has already ended."
    elif arg in ("cq", "ca"):
        n = len(app.tasks.queued()) if arg == "cq" else len(app.tasks.queued() + app.tasks.running() + app.tasks.held())
        what = f"remove <b>{n}</b> waiting task{'s' if n != 1 else ''}" if arg == "cq" else f"cancel <b>all {n}</b> tasks"
        await app.sender.answer(query.id)
        return await app.sender.edit_text(chat_id, msg_id, f"⚠️ Sure? This will {what}.\n<i>Their users are told.</i>",
                                          reply_markup=_sure(admin.id, arg + "y", "✅ Yes, do it"))
    elif arg in ("cqy", "cay"):
        victims = app.tasks.queued() if arg == "cqy" else app.tasks.queued() + app.tasks.running() + app.tasks.held()
        n = await (app.tasks.clear_queue(by) if arg == "cqy" else app.tasks.cancel_all(by))
        for task in victims:
            await _tell_user(app, task)
        note = f"{n} task{'s' if n != 1 else ''} cancelled."
        app.dump.notify(f"🧹 {html.escape(by)}: {note}")
    await app.sender.answer(query.id, note)
    text, markup = await render(app, admin.id)
    await app.sender.edit_text(chat_id, msg_id, text, reply_markup=markup)


async def _tell_user(app, task) -> None:
    if not task.chat_id:
        return
    try:
        await app.sender.send_message(task.chat_id, "✖️ <b>An admin cancelled your task.</b> You can send it again.")
    except Exception:
        pass
