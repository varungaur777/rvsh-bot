"""Handler registration — the only place that maps updates/commands to handler functions.

Depends on: Kurigram handlers/filters, ``handlers.*``, ``core.events``. Used by: ``bot.app``.
Add each new feature's handlers here (one module per command group).
"""

from __future__ import annotations

from pyrogram import filters
from pyrogram.handlers import (CallbackQueryHandler, ChatMemberUpdatedHandler, InlineQueryHandler, MessageHandler,
                                RawUpdateHandler)

from bot.core import events
from bot.handlers import (admin, basic, batch, broadcast, dbpanel, exam, files, helpvideo, jsoncmd, links, loadquiz, people,
                          quiz, results, settings, slides, taskview, update, waits)


def _cb(prefix: str):
    return filters.create(lambda _, __, q: (q.data or "").startswith(prefix + ":"))


async def _poll_votes(client, update, users, chats) -> None:
    from pyrogram import raw
    if not isinstance(update, raw.types.UpdateMessagePollVote):
        return
    app = client.app
    if not app.ready.is_set():
        await app.ready.wait()
    try:
        await app.quiz.raw_update(client, update, users, chats)
    except Exception as e:
        app.dump.report_error(e, "quiz poll vote")


def register(app) -> None:
    from pyrogram import raw
    c = app.client
    add = c.add_handler
    # quiz votes reach the engine raw (``_poll_votes``): Kurigram's own vote parser raises for an anonymous admin's vote
    # (``PeerChannel``) — and then no handler sees it — and nothing here reads parsed votes, so it is switched off
    parsers = getattr(getattr(c, "dispatcher", None), "update_parsers", None)
    if isinstance(parsers, dict):
        parsers.pop(raw.types.UpdateMessagePollVote, None)
    add(MessageHandler(basic.start, filters.command("start")))
    add(MessageHandler(basic.help_cmd, filters.command("help") & filters.private))
    add(MessageHandler(basic.cancel, filters.command("cancel")))
    add(MessageHandler(basic.queue_cmd, filters.command("q")))
    add(MessageHandler(admin.stats, filters.command("stats")))
    add(MessageHandler(admin.send_log, filters.command("log")))
    add(MessageHandler(admin.public_on, filters.command("on")))
    add(MessageHandler(admin.public_off, filters.command("off")))
    add(MessageHandler(admin.restart, filters.command("restart")))
    add(MessageHandler(update.update_command, filters.command("update")))
    add(MessageHandler(people.admin_command, filters.command("admin")))
    add(MessageHandler(people.groups_command, filters.command("groups")))
    add(MessageHandler(people.user_command, filters.command("user")))
    add(MessageHandler(settings.es_command, filters.command("es")))
    add(MessageHandler(settings.ps_command, filters.command("ps")))
    add(MessageHandler(settings.wm_command, filters.command("wm")))
    add(MessageHandler(settings.bot_command, filters.command("bot")))
    add(MessageHandler(settings.qz_command, filters.command("qz")))
    add(MessageHandler(quiz.quizzes_command, filters.command("quizzes")))
    add(MessageHandler(quiz.quiz_command, filters.command("quiz")))
    add(MessageHandler(people.auth_command, filters.command(["auth", "deauth"])))
    add(MessageHandler(dbpanel.db_command, filters.command("db")))
    add(MessageHandler(files.ls_command, filters.command("ls")))
    add(MessageHandler(files.clean_command, filters.command("clean")))
    add(MessageHandler(files.rm_command, filters.command("rm")))
    add(MessageHandler(taskview.task_command, filters.command("task")))
    add(MessageHandler(taskview.cq_command, filters.command("cq")))
    add(MessageHandler(broadcast.bc_command, filters.command("bc")))
    add(MessageHandler(broadcast.stopbc_command, filters.command("stopbc")))
    add(MessageHandler(broadcast.msg_command, filters.command("msg")))
    add(MessageHandler(jsoncmd.on_range_text, filters.text & jsoncmd.range_filter), group=-1)  # private or the card's group
    add(MessageHandler(waits.intercept, filters.private & waits.waiting_filter()), group=-1)  # /bc, /msg, ID asks
    add(MessageHandler(exam.exam_command, filters.command(["tcs", "edqt"])))
    add(MessageHandler(results.results_command, filters.command("results") & filters.private))
    add(MessageHandler(links.links_command, filters.command("links") & filters.private))
    add(MessageHandler(batch.batch_command, filters.command("batch") & filters.private))
    add(MessageHandler(slides.slides_command, filters.command("slides") & filters.private))
    add(MessageHandler(jsoncmd.json_command, filters.command("json")))
    add(MessageHandler(jsoncmd.json_batch_command, filters.command("json_batch")))
    add(MessageHandler(loadquiz.load_quiz_command, filters.command("load_quiz")))
    add(MessageHandler(jsoncmd.on_document, filters.private & jsoncmd.json_filter))  # question JSON files (/json)
    add(MessageHandler(exam.on_document, filters.private & filters.document))  # files work without a command
    add(MessageHandler(exam.on_text, filters.private & filters.text & ~filters.regex(r"^/")))  # pasted exam links
    add(CallbackQueryHandler(basic.nav, _cb("nav")))
    add(CallbackQueryHandler(exam.on_button, _cb("exm")))
    add(CallbackQueryHandler(results.on_button, _cb("res")))
    add(CallbackQueryHandler(links.on_button, _cb("lnk")))
    add(CallbackQueryHandler(batch.on_button, _cb("bat")))
    add(CallbackQueryHandler(jsoncmd.on_button, _cb("jsn")))
    add(CallbackQueryHandler(jsoncmd.on_batch_button, _cb("jsb")))
    add(CallbackQueryHandler(update.on_button, _cb("upd")))
    add(CallbackQueryHandler(basic.fsub_recheck, _cb("fsub")))
    add(CallbackQueryHandler(helpvideo.on_button, _cb("hlp")))
    add(CallbackQueryHandler(admin.stats_button, _cb("sts")))
    add(CallbackQueryHandler(broadcast.on_button, _cb("bc")))
    add(CallbackQueryHandler(taskview.on_button, _cb("tsk")))
    add(CallbackQueryHandler(people.admin_button, _cb("adm")))
    add(CallbackQueryHandler(people.groups_button, _cb("grp")))
    add(CallbackQueryHandler(files.ls_button, _cb("ls")))
    add(CallbackQueryHandler(settings.on_button, _cb("set")))
    add(CallbackQueryHandler(dbpanel.on_button, _cb("db")))
    add(CallbackQueryHandler(quiz.on_button, _cb("qz")))
    add(CallbackQueryHandler(quiz.list_button, _cb("qzl")))
    add(CallbackQueryHandler(quiz.group_button, _cb("qzg")))
    add(CallbackQueryHandler(quiz.control_button, _cb("qzc")))
    add(InlineQueryHandler(quiz.inline_query))
    add(RawUpdateHandler(_poll_votes), group=3)  # quiz poll answers (no other handler reads raw updates)
    add(CallbackQueryHandler(files.clean_button, _cb("cln")))
    add(CallbackQueryHandler(files.rm_button, _cb("rm")))
    add(ChatMemberUpdatedHandler(events.on_chat_member), group=1)
    add(MessageHandler(events.on_group_message, filters.group), group=2)  # groups the bot was in before /groups
