"""Admin/owner handlers available in P1: /stats (+ its Refresh/Details buttons), /log, /on, /off, /restart (spec §23).

Depends on: ``core.access.guard``, ``core.logging``. Used by: ``handlers/__init__.py``.
"""

from __future__ import annotations

from bot.core.access import guard
from bot.core.logging import current_log_path
from bot.ui import stats_view
from bot.ui.keyboards import parse_cb


@guard("admin", force_sub=False, private_only=True)  # private data: never in a group (owner 2026-10-14)
async def stats(client, message) -> None:
    uid = message.from_user.id
    await client.app.sender.send_message(message.chat.id, client.app.stats_text(), reply_markup=stats_view.keyboard(uid))


@guard("admin", force_sub=False)
async def stats_button(client, query) -> None:
    """``sts:<uid>:<simple|detail>`` — 🔄 Refresh / switch view, editing the same message."""
    app, data = client.app, parse_cb(query.data)
    detail = data.obj == "detail"
    await app.sender.answer(query.id, "Updated")
    try:
        await app.sender.edit_text(query.message.chat.id, query.message.id, app.stats_text(detail),
                                   reply_markup=stats_view.keyboard(query.from_user.id, detail))
    except Exception:  # MESSAGE_NOT_MODIFIED: nothing changed since the last refresh
        pass


@guard("admin", force_sub=False, private_only=True)
async def send_log(client, message) -> None:
    app = client.app
    path = current_log_path()
    if not path or not path.exists():
        return await app.sender.send_message(message.chat.id, "No log file yet.")
    caption = f"🗒 {path.name}"
    await app.sender.send_document(message.chat.id, str(path), caption=caption)
    app.dump.send_file(path, f"{caption} · requested by {message.from_user.id}")


@guard("admin", force_sub=False)
async def public_on(client, message) -> None:
    await client.app.settings.set("public_enabled", True, by=message.from_user.id)
    await client.app.sender.send_message(message.chat.id, "🌐 The bot is now open to everyone.")
    client.app.dump.notify(f"🌐 Public access <b>ON</b> by <code>{message.from_user.id}</code>")


@guard("admin", force_sub=False)
async def public_off(client, message) -> None:
    await client.app.settings.set("public_enabled", False, by=message.from_user.id)
    await client.app.sender.send_message(message.chat.id, "🔒 The bot is now for admins only (including quizzes).")
    client.app.dump.notify(f"🔒 Public access <b>OFF</b> by <code>{message.from_user.id}</code>")


@guard("admin", force_sub=False)
async def restart(client, message) -> None:
    app = client.app
    await app.sender.send_message(message.chat.id, "♻️ Restarting gracefully… (database untouched)")
    app.request_stop(f"/restart by {message.from_user.id}", restart=True)
