"""⚙️ Admin Panel (spec §6.9, §23): one menu for every admin tool; each button opens the same screen as its
command. Owner-only tools (Bot settings, Database) are shown to the owner only.

Depends on: the admin handler modules, ``ui.stats_view``. Used by: ``handlers/basic.nav`` (``nav:<uid>:admin`` and
``nav:<uid>:a_<tool>``).

Screens that live in one message (status, tasks, admins, groups, settings, files) replace the panel in place;
tools that start a conversation (broadcast, results, clean up) run their command as if it had been typed, through
the command's own access check.
"""

from __future__ import annotations

from types import SimpleNamespace

from bot.ui import stats_view
from bot.ui.keyboards import btn, cb, kb, nav_row


def hub(app, uid: int):
    owner = uid == app.config.owner_id
    try:
        issues = stats_view.problems(stats_view.collect(app))
    except Exception:  # the panel must open even if a reading fails
        issues = []
    health = ("✅ Everything is fine." if not issues else
              f"⚠️ <b>{len(issues)} thing{'s' if len(issues) != 1 else ''} need{'s' if len(issues) == 1 else ''} attention</b> — see 📈 Status.")
    running = len(app.tasks.running())
    waiting = len(app.tasks.queued())
    text = (f"⚙️ <b>Admin Panel</b>\n{health}\n🗂 {running} task{'s' if running != 1 else ''} running · {waiting} waiting"
            + ("\n📢 A broadcast is running." if getattr(app, "broadcast", None) else "")
            + "\n\nEvery button here also has a command — see /help.")
    a = lambda label, tool, **kw: btn(label, cb("nav", uid, f"a_{tool}"), **kw)  # noqa: E731
    rows = [[a("📈 Status", "stats"), a("🗂 Tasks", "task")],
            [a("📢 Broadcast", "bc"), a("📊 Results", "results")],
            [a("👮 Admins", "admin"), a("👥 Groups & users", "groups")],
            [a("🧾 Exam settings", "es"), a("📄 PDF settings", "ps")],
            [a("💧 Watermark", "wm"), a("🎯 Quiz settings", "qz")],
            [a("📁 Files", "ls"), a("🧹 Clean up", "clean")],
            [a("🗒 Log file", "log")]]
    if owner:
        rows.append([a("🤖 Bot settings", "bot", style="primary"), a("🗄 Database", "db", style="primary")])
    return text, kb(*rows, nav_row(uid))


async def open_tool(client, query, tool: str):
    """(text, markup) to show in place, or None when the tool sent its own message."""
    from bot.handlers import admin, broadcast, dbpanel, files, people, results, settings, taskview

    app, uid = client.app, query.from_user.id
    owner = uid == app.config.owner_id
    if tool == "stats":
        return app.stats_text(), stats_view.keyboard(uid)
    if tool == "task":
        return await taskview.render(app, uid)
    if tool == "admin":
        return await people.admin_screen(app, uid)
    if tool == "groups":
        return await people.groups_screen(app, uid)
    if tool in ("es", "ps", "wm", "qz") or (tool == "bot" and owner):
        return settings._card(app, uid, tool, 0)
    if tool == "ls":
        return await files.ls_screen(app, uid, files.root_of(app))
    if tool == "db" and owner:
        return await dbpanel.home(app, uid)
    command = {"bc": (broadcast.bc_command, "/bc"), "results": (results.results_command, "/results"),
               "clean": (files.clean_command, "/clean"), "log": (admin.send_log, "/log")}.get(tool)
    if command is None:
        return hub(app, uid)
    fn, text = command
    fake = SimpleNamespace(id=query.message.id, chat=query.message.chat, from_user=query.from_user, text=text,
                           caption=None, date=None)
    await fn(client, fake)  # through the command's own guard (role check included)
    return None

