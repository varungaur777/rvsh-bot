"""Quiz commands and buttons (spec §17): /quizzes, /quiz, deep links, playing here or in a group, controls,
results (review, retry), leaderboards, the HTML file, inline sharing, 👤 My Stats.

- ``/quizzes [words]`` (🎯 Quizzes): paged list, newest first (``/quizzes <words>`` searches titles); ``👤 My quizzes``.
- ``/quiz`` → your quizzes; ``/quiz <id>`` → the quiz page (private) or the start card (group).
- Deep links: ``?start=qz_…`` (page / group start card), ``?start=lb_qz_…`` (leaderboard), ``?start=wa_qz_…``
  (a Web App button — Web App buttons only work in private chats).
- Groups: the bot must be an admin there; one quiz per group, one per starter; while the bot is closed (``/off``) a
  quiz starts only in an authorised group or by an admin (owner 2026-10-13).
- Who may press (owner 2026-10-14: "group ka admin ho ya owner, quiz stop, play, cancel, pause, resume sab kr skta hai,
  kisi ne chalai ho, [normal user keval apni hi quiz …], bot admin/owner bhi sab kr skta hai"): controls ``qzc``
  (Pause / Resume / Stop), a group's start card ``qzg`` (time, ▶️ Start, ✖️ Cancel) and list ``qzl`` — the person
  it belongs to, the bot's owner / admins and, in a group, the group's admins / owner (anonymous too); other members
  never. Anonymous admins can start quizzes (``/quiz qz_…`` → a start card for the group's admins).
- One quiz per starter: a second start shows the first with ``⏹ Stop that quiz``.

Depends on: ``quiz.engine`` (``app.quiz``), ``quiz.store``, ``quiz.ui``, ``quiz.maker``, ``core.access.guard``.
Used by: ``handlers/__init__``, ``handlers.basic`` (deep links, home buttons).
"""

from __future__ import annotations

import html
import math
import time

from pyrogram import enums
from pyrogram.types import (InlineQueryResultArticle, InputTextMessageContent, WebAppInfo)

from bot.core.access import guard
from bot.core.sender import NO_PREVIEW
from bot.quiz import store, ui
from bot.quiz.engine import ANON_ADMIN_BOT, LABELS, QuizError
from bot.ui.keyboards import btn, cb, kb, nav_row, parse_cb
from bot.ui.texts import t

PER_PAGE = 8
REVIEW_PER_PAGE = 5
_admin_cache: dict[tuple[int, int], tuple[float, bool]] = {}


# ------------------------------------------------------------------ list
async def list_screen(app, uid: int, page: int = 0, flt: str = "all", words: str = "", group: bool = False):
    flt_q = {"created_by": uid} if flt == "mine" else {}
    rows = await app.repo.quizzes.find(flt_q, sort=[("created_at", -1)],
                                       projection={"title": 1, "meta_line": 1, "scope": 1, "section": 1, "total": 1,
                                                   "plays": 1, "created_at": 1, "created_by": 1, "lang": 1,
                                                   "default_time": 1, "explain": 1})
    if words:
        ws = words.lower().split()
        rows = [r for r in rows if all(w in (r.get("title") or "").lower() + " " + (r.get("meta_line") or "").lower()
                                       for w in ws)]
    pages = max(1, math.ceil(len(rows) / PER_PAGE))
    page = max(0, min(page, pages - 1))
    part = rows[page * PER_PAGE:(page + 1) * PER_PAGE]
    return (ui.list_text(app, part, page, pages, flt == "mine", len(rows), group, words),
            ui.list_markup(uid, part, page, pages, flt, group))


def _in_group(chat) -> bool:
    return chat is not None and chat.type in (enums.ChatType.GROUP, enums.ChatType.SUPERGROUP)


async def send_list(app, message, flt: str = "all", arg: str = "") -> None:
    """``/quizzes [page | words]`` and ``/quiz`` without an ID — in private and in **any group** (owner 2026-10-13:
    "/quiz, /quizzes command kisi bhi group mai chal ske"), anonymous admins too (their buttons: uid 0, the group's
    admins)."""
    uid = message.from_user.id if message.from_user else 0
    group = _in_group(message.chat)
    page = int(arg) - 1 if arg.isdigit() and 0 < int(arg) < 1000 else 0
    words = "" if arg.isdigit() else arg
    text, markup = await list_screen(app, uid, page, flt if uid else "all", words, group)
    await app.sender.send_message(message.chat.id, text, reply_markup=markup, link_preview_options=NO_PREVIEW,
                                  reply_to_message_id=message.id if group else None)


@guard(anon=True)
async def quizzes_command(client, message) -> None:
    if message.from_user is None and not is_anon_admin(message):
        return  # a channel post
    parts = (message.text or "").split(maxsplit=1)
    await send_list(client.app, message, "all", parts[1].strip() if len(parts) > 1 else "")


@guard(private_only=False, anon=True)
async def quiz_command(client, message) -> None:
    app, uid = client.app, message.from_user.id if message.from_user else None
    if uid is None and not is_anon_admin(message):
        return  # a channel post
    parts = (message.text or "").split()
    arg = parts[1] if len(parts) > 1 else ""
    if message.chat.type != enums.ChatType.PRIVATE:
        if not arg or not arg.startswith("qz_"):
            return await send_list(app, message, "all", arg)
        return await group_start(client, message, arg)
    if not arg:
        text, markup = await list_screen(app, uid, 0, "mine")
        return await app.sender.send_message(message.chat.id, text, reply_markup=markup, link_preview_options=NO_PREVIEW)
    await send_page(app, message.chat.id, uid, arg)


async def send_page(app, chat_id: int, uid: int, quiz_id: str) -> None:
    quiz = await store.get(app, quiz_id)
    if quiz is None:
        return await app.sender.send_message(chat_id, "🔎 No quiz with that ID. See /quizzes.")
    stats = await app.repo.quiz_stats.get(quiz_id, copy=False)
    await app.sender.send_message(chat_id, ui.page_text(app, quiz, stats, uid), reply_markup=ui.page_markup(app, uid, quiz),
                                  link_preview_options=NO_PREVIEW)


async def deep_link(client, message, param: str) -> bool:
    """``/start <param>`` for quizzes; True when handled."""
    app = client.app
    uid = message.from_user.id if message.from_user else 0  # 0: an anonymous admin in a group
    private = message.chat.type == enums.ChatType.PRIVATE
    if param.startswith("qz_"):
        if private:
            await send_page(app, message.chat.id, uid, param)
        else:
            await group_start(client, message, param)
        return True
    if param.startswith("lb_qz_") and private:
        quiz = await store.get(app, param[3:])
        if quiz is None:
            await app.sender.send_message(message.chat.id, "🔎 That quiz was deleted.")
        else:
            text, markup = await board_screen(app, uid, quiz)
            await app.sender.send_message(message.chat.id, text, reply_markup=markup)
        return True
    if param.startswith("wa_qz_") and private:
        quiz = await store.get(app, param[3:])
        url = ui.webapp_url(app, param[3:]) if quiz else None
        if url is None:
            await app.sender.send_message(message.chat.id, "📱 The Web App is not available right now — play here instead.",
                                          reply_markup=ui.page_markup(app, uid, quiz) if quiz else None)
        else:
            await app.sender.send_message(message.chat.id, f"📱 <b>{html.escape(quiz['title'])}</b>\nTap to open the Web App:",
                                          reply_markup=kb([btn("📱 Open the quiz", web_app=WebAppInfo(url=url), style="primary")]))
        return True
    return False


async def board_screen(app, uid: int, quiz: dict):
    stats = await app.repo.quiz_stats.get(quiz["_id"], copy=False)
    n = store.position(stats, 0)[1]
    text = (f"🏆 <b>Leaderboard</b> · {html.escape(quiz.get('title') or 'Quiz')}\n<i>{n} ranked player{'s' if n != 1 else ''} · "
            f"first private / Web App attempt of each player</i>\n\n" + store.board_text(stats, uid, n=20))
    return text, kb([btn("⬅️ Quiz", cb("qz", uid, quiz["_id"], "open")), nav_row(uid)[-1]])


# ------------------------------------------------------------------ buttons (private)
@guard(shared=True)
async def list_button(client, query) -> None:
    """The list's ⬅️ ➡️ / My / All: whoever sent the command, the bot's admins and the group's admins (``_may_press``);
    an anonymous admin's list (user 0): the group's admins and the bot's admins."""
    app, uid = client.app, query.from_user.id
    d = parse_cb(query.data)
    chat = query.message.chat if query.message else None
    if d is None or not await _may_press(app, d.user_id, uid, chat):
        return await app.sender.answer(query.id, t("not_your_button"), show_alert=True)
    text, markup = await list_screen(app, d.user_id, int(d.obj or 0), (d.arg or "all") if d.user_id else "all",
                                     group=_in_group(chat))
    await app.sender.answer(query.id)
    await app.sender.edit_text(query.message.chat.id, query.message.id, text, reply_markup=markup,
                               link_preview_options=NO_PREVIEW)


@guard()
async def on_button(client, query) -> None:
    app, uid = client.app, query.from_user.id
    d = parse_cb(query.data)
    chat_id, msg_id = query.message.chat.id, query.message.id
    quiz = await store.get(app, d.obj)
    arg = d.arg
    if quiz is None:
        return await app.sender.answer(query.id, "This quiz was deleted.", show_alert=True)
    edit = lambda text, markup=None: app.sender.edit_text(chat_id, msg_id, text, reply_markup=markup,  # noqa: E731
                                                          link_preview_options=NO_PREVIEW)
    if arg == "open":
        await app.sender.answer(query.id)
        stats = await app.repo.quiz_stats.get(quiz["_id"], copy=False)
        return await edit(ui.page_text(app, quiz, stats, uid), ui.page_markup(app, uid, quiz))
    if arg == "h_t":
        return await app.sender.answer(query.id, "How long each question stays open")
    if arg in ("play", "retry") or arg.startswith("t_"):
        if app.quiz.active_for(uid) is not None:  # owner: the first one is stopped before another starts
            text, markup = app.quiz.busy_note(uid)
            await app.sender.answer(query.id)
            return await app.sender.send_message(chat_id, text, reply_markup=markup)
        if arg == "retry":
            return await _retry(app, query, quiz)
        t = int(arg[2:]) if arg.startswith("t_") else int(quiz.get("default_time") or 30)
        await app.sender.answer(query.id)
        return await edit(ui.setup_text(quiz, t), ui.time_markup(app, uid, quiz, t))
    if arg.startswith("go_"):
        await app.sender.answer(query.id)
        try:
            await app.sender.edit_text(chat_id, msg_id, f"▶️ <b>{html.escape(quiz['title'])}</b> — starting…")
            await app.quiz.start(chat_id, quiz, query.from_user, int(arg[3:]))
        except QuizError as e:
            await app.sender.send_message(chat_id, str(e), reply_markup=e.markup)
        return
    if arg == "html":
        return await _html_file(app, query, quiz)
    if arg == "lb":
        await app.sender.answer(query.id)
        text, markup = await board_screen(app, uid, quiz)
        if (query.message.text or "").startswith("🏁"):  # from a result: keep the result, show the board below
            return await app.sender.send_message(chat_id, text, reply_markup=markup)
        return await edit(text, markup)
    if arg in ("del", "dely") and app.access.is_admin(uid):
        if arg == "del":
            await app.sender.answer(query.id)
            return await edit(f"🗑 Delete <b>{html.escape(quiz['title'])}</b> and its leaderboard?\n<i>Its cards stay in the "
                              "image dump (other quizzes of this exam use them).</i>",
                              kb([btn("🗑 Yes, delete", cb("qz", uid, quiz["_id"], "dely"), style="danger"),
                                  btn("✖️ No", cb("qz", uid, quiz["_id"], "open"))]))
        await store.delete(app, quiz["_id"])
        await app.repo.exams.update(quiz.get("exam_key") or "-", {"$pull": {"quiz_ids": quiz["_id"]}}, upsert=False)
        app.dump.notify(f"🗑 Quiz <code>{quiz['_id']}</code> deleted by <code>{uid}</code> · {html.escape(quiz['title'])}")
        await app.sender.answer(query.id, "Deleted.")
        text, markup = await list_screen(app, uid)
        return await edit(text, markup)
    if arg.startswith("rv_") or arg.startswith("rp_"):
        return await _review(app, query, quiz, int(arg[3:]), new=arg.startswith("rv_"))
    await app.sender.answer(query.id)


async def _retry(app, query, quiz: dict) -> None:
    uid = query.from_user.id
    res = app.quiz.result_of(uid, quiz["_id"])
    if res is None:
        return await app.sender.answer(query.id, "That result is too old — play again instead.", show_alert=True)
    qs = res["qlist"]
    redo = [qi for pos, qi in enumerate(qs) if pos not in res["answers"]
            or res["answers"][pos][0] != int(quiz["questions"][qi]["c"])]
    if not redo:
        return await app.sender.answer(query.id, "Nothing to retry — all right! 🎉", show_alert=True)
    await app.sender.answer(query.id)
    try:
        await app.quiz.start(query.message.chat.id, quiz, query.from_user, int(quiz.get("default_time") or 30),
                             qlist=redo, mode="retry")
    except QuizError as e:
        await app.sender.send_message(query.message.chat.id, str(e), reply_markup=e.markup)


async def _review(app, query, quiz: dict, page: int, new: bool) -> None:
    uid = query.from_user.id
    res = app.quiz.result_of(uid, quiz["_id"])
    if res is None:
        return await app.sender.answer(query.id, "That result is too old to review.", show_alert=True)
    items = []
    for pos, qi in enumerate(res["qlist"]):
        q = quiz["questions"][qi]
        mine = res["answers"].get(pos)
        if mine is not None and mine[0] == int(q["c"]):
            continue
        items.append((pos, q, mine))
    await app.sender.answer(query.id)
    if not items:
        return await app.sender.send_message(query.message.chat.id, "📝 Nothing to review — every answer was right! 🎉")
    pages = math.ceil(len(items) / REVIEW_PER_PAGE)
    page = max(0, min(page, pages - 1))
    lines = [f"📝 <b>Review</b> · {html.escape(quiz['title'])}\n<i>{len(items)} wrong / skipped · page {page + 1}/{pages}</i>"]
    for pos, q, mine in items[page * REVIEW_PER_PAGE:(page + 1) * REVIEW_PER_PAGE]:
        opts = (q.get("poll") or {}).get("o")
        c = int(q["c"])

        def say(i):
            return f"{LABELS[i]}" + (f" — {html.escape(opts[i])}" if opts and i < len(opts) and opts[i] != LABELS[i] else "")
        text = html.escape(q.get("text") or "")
        lines.append(f"\n<b>Q{pos + 1}</b>" + (f" <i>(paper Q{q['no']})</i>" if q.get("no") else "")
                     + (f"\n{text}" if text else "")
                     + f"\n{'❌ You: ' + say(mine[0]) if mine else '⭕ Skipped'}\n✅ Answer: {say(c)}")
    nav = []
    if page > 0:
        nav.append(btn("⬅️ Prev", cb("qz", uid, quiz["_id"], f"rp_{page - 1}")))
    if page + 1 < pages:
        nav.append(btn("Next ➡️", cb("qz", uid, quiz["_id"], f"rp_{page + 1}")))
    markup = kb(nav, [btn("🔁 Retry wrong", cb("qz", uid, quiz["_id"], "retry"))])
    text = "\n".join(lines)[:4000]
    if new:
        await app.sender.send_message(query.message.chat.id, text, reply_markup=markup)
    else:
        await app.sender.edit_text(query.message.chat.id, query.message.id, text, reply_markup=markup)


async def _html_file(app, query, quiz: dict) -> None:
    from bot.quiz.maker import html_look, run_html
    uid = query.from_user.id
    cached = (quiz.get("html") or {}).get(html_look())
    if app.tasks.active_for(uid) is not None:
        return await app.sender.answer(query.id, "⏳ You already have a task — wait for it or /cancel it.", show_alert=True)
    await app.sender.answer(query.id, "🌐 Sending the HTML file…" if cached else "🌐 Making the HTML file…")
    task = app.tasks.create(uid, "quiz_html", "light" if cached else "heavy", app.access.priority(uid),
                            title=quiz["_id"], chat_id=query.message.chat.id)
    task.data.update(quiz_id=quiz["_id"], user_name=query.from_user.first_name or "")
    await app.tasks.submit(task, lambda tk: run_html(app, tk))


# ------------------------------------------------------------------ groups
async def _group_ok(app, chat, uid: int) -> str:
    """'' when a quiz may start in this group, else why not."""
    g = await app.repo.groups.get(chat.id, copy=False) or {}
    if not app.settings.get("public_enabled") and not g.get("authorised") and not app.access.is_admin(uid):
        return "🔒 The bot is closed right now — quizzes run only in authorised groups (/auth)."
    if not g.get("bot_is_admin"):
        try:
            m = await app.client.get_chat_member(chat.id, app.me.id)
            if m.status in (enums.ChatMemberStatus.ADMINISTRATOR, enums.ChatMemberStatus.OWNER):
                return ""
        except Exception:
            pass
        return "👮 Make me an <b>admin</b> of this group first — quizzes need it."
    return ""


def _plain(text: str) -> str:
    """A chat note as a button alert (no HTML there)."""
    return html.unescape(text.replace("<b>", "").replace("</b>", ""))


def is_anon_admin(message) -> bool:
    """A message an anonymous admin sent in its group (Telegram shows the group as the sender)."""
    sc = getattr(message, "sender_chat", None)
    return message.from_user is None and sc is not None and sc.id == message.chat.id


async def group_start(client, message, quiz_id: str) -> None:
    """The start card. Anonymous admins too (owner 2026-10-13): their card (user 0) is for the group's admins, and
    whoever of them taps ▶️ Start becomes the starter."""
    app = client.app
    user = message.from_user
    if user is None and not is_anon_admin(message):
        return  # a channel posting in the group
    quiz = await store.get(app, quiz_id)
    if quiz is None:
        return await app.sender.send_message(message.chat.id, "🔎 No quiz with that ID.", reply_to_message_id=message.id)
    why = await _group_ok(app, message.chat, user.id if user else None)
    if why:
        return await app.sender.send_message(message.chat.id, why, reply_to_message_id=message.id)
    if app.quiz.session_for(message.chat.id) is not None:
        return await app.sender.send_message(message.chat.id, "⏳ A quiz is already running here — wait for it to end.",
                                             reply_to_message_id=message.id)
    if user is not None and app.quiz.active_for(user.id) is not None:
        text, markup = app.quiz.busy_note(user.id)
        return await app.sender.send_message(message.chat.id, text, reply_markup=markup, reply_to_message_id=message.id)
    secs = int(quiz.get("default_time") or 30)
    await app.sender.send_message(message.chat.id, ui.setup_text(quiz, secs, group=True),
                                  reply_markup=ui.time_markup(app, user.id if user else 0, quiz, secs, prefix="qzg"))


async def _group_admin(app, chat_id: int, uid: int) -> bool:
    if uid == ANON_ADMIN_BOT:  # Telegram's stand-in for an anonymous admin of this group
        return True
    key = (chat_id, uid)
    hit = _admin_cache.get(key)
    if hit and time.time() - hit[0] < 300:
        return hit[1]
    try:
        m = await app.client.get_chat_member(chat_id, uid)
        ok = m.status in (enums.ChatMemberStatus.ADMINISTRATOR, enums.ChatMemberStatus.OWNER)
    except Exception:
        ok = False
    _admin_cache[key] = (time.time(), ok)
    if len(_admin_cache) > 5000:
        _admin_cache.clear()
    return ok


@guard(private_only=False, shared=True)
async def group_button(client, query) -> None:
    app, uid = client.app, query.from_user.id
    d = parse_cb(query.data)
    chat = query.message.chat
    if not await _may_press(app, d.user_id, uid, chat):
        return await app.sender.answer(query.id, "These buttons are for the person who sent the command and the group's "
                                       "admins." if d.user_id else "An anonymous admin sent this — only the group's admins "
                                       "can use it.", show_alert=True)
    quiz = await store.get(app, d.obj)
    if quiz is None:
        return await app.sender.answer(query.id, "This quiz was deleted.", show_alert=True)
    if d.arg == "x":
        await app.sender.answer(query.id)
        return await app.sender.delete(chat.id, query.message.id)
    if d.arg == "card":  # a number on the group's quiz list → this quiz's start card, here (as /quiz qz_…)
        why = await _group_ok(app, chat, uid) or ("⏳ A quiz is already running here — wait for it to end."
                                                 if app.quiz.session_for(chat.id) is not None else "")
        if why:
            return await app.sender.answer(query.id, _plain(why), show_alert=True)
        await app.sender.answer(query.id)
        if d.user_id and app.quiz.active_for(d.user_id) is not None:
            text, markup = app.quiz.busy_note(d.user_id)
            return await app.sender.send_message(chat.id, text, reply_markup=markup)
        secs = int(quiz.get("default_time") or 30)
        return await app.sender.send_message(chat.id, ui.setup_text(quiz, secs, group=True),
                                             reply_markup=ui.time_markup(app, d.user_id, quiz, secs, prefix="qzg"))
    if d.arg == "h_t":
        return await app.sender.answer(query.id, "How long each question stays open")
    if d.arg.startswith("t_"):
        t = int(d.arg[2:])
        await app.sender.answer(query.id)
        return await app.sender.edit_text(chat.id, query.message.id, ui.setup_text(quiz, t, group=True),
                                          reply_markup=ui.time_markup(app, d.user_id, quiz, t, prefix="qzg"))
    if d.arg.startswith("go_"):
        why = await _group_ok(app, chat, uid)
        if why:
            return await app.sender.answer(query.id, _plain(why), show_alert=True)
        await app.sender.answer(query.id)
        try:
            await app.sender.delete(chat.id, query.message.id)
        except Exception:
            pass
        g = await app.repo.groups.get(chat.id, copy=False) or {}
        starter = None if uid == ANON_ADMIN_BOT else query.from_user
        try:
            await app.quiz.start(chat.id, quiz, starter, int(d.arg[3:]), group=True, chat_title=chat.title or "",
                                 chat_link=g.get("link") or g.get("invite_link") or "")
        except QuizError as e:
            await app.sender.send_message(chat.id, str(e), reply_markup=e.markup)


async def _may_press(app, owner: int, uid: int, chat) -> bool:
    """A group quiz button (start card, list) of ``owner`` (0 = an anonymous admin's): ``owner`` themself, the bot's
    owner / admins, or — in a group — the group's admins / owner, anonymous included (owner 2026-10-14)."""
    if (owner and uid == owner) or app.access.is_admin(uid):
        return True
    return _in_group(chat) and await _group_admin(app, chat.id, uid)


async def _can_control(app, s, query) -> bool:
    """Pause / Resume / Stop: the starter, the bot's owner / admins, and in a group its admins / owner (anonymous too)
    — owner 2026-10-14: "group ka admin ho ya owner, quiz stop, play, cancel, pause, resume sab kr skta hai, kisi ne
    chalai ho, [normal user keval apni hi quiz stop, cancel, pause, resume kar skta hai], bot admin/owner bhi sab kr
    skta hai"."""
    uid = query.from_user.id
    if (s.starter and uid == s.starter) or app.access.is_admin(uid):
        return True
    return s.group and await _group_admin(app, s.chat_id, uid)


@guard(shared=True, force_sub=False)
async def control_button(client, query) -> None:
    app = client.app
    d = parse_cb(query.data)
    s = app.quiz.by_sid(d.obj)
    if s is None:
        return await app.sender.answer(query.id, "This quiz has ended.")
    if not await _can_control(app, s, query):
        who = "the person who started it, the group's admins or the bot's admins" if s.group else "the person who started it"
        return await app.sender.answer(query.id, f"Only {who} can do that.", show_alert=True)
    if d.arg == "pause":
        ok = await app.quiz.pause(s.chat_id)
        return await app.sender.answer(query.id, "⏸ Paused." if ok else "Not running.")
    if d.arg == "resume":
        await app.sender.answer(query.id, "▶️ Resuming…")
        return await app.quiz.resume(s.chat_id)
    if d.arg == "stop":
        await app.sender.answer(query.id, "⏹ Stopping…")
        await app.quiz.stop(s.chat_id, "stopped")
        if query.message and query.message.id != s.start_msg and query.message.id != s.ctrl_msg:
            try:  # the "⏹ Stop that quiz" note
                await app.sender.edit_markup(query.message.chat.id, query.message.id, None)
            except Exception:
                pass
        return
    await app.sender.answer(query.id)


# ------------------------------------------------------------------ inline
@guard(force_sub=False)
async def inline_query(client, query) -> None:
    app = client.app
    raw_q = (query.query or "").strip()
    words = raw_q.lower()
    if raw_q.startswith("qz_"):  # 📤 Share inserts the quiz id (ids are case-sensitive)
        q = await store.get(app, raw_q.split()[0])
        found = [q] if q else []
    else:
        rows = await app.repo.quizzes.find({}, sort=[("plays", -1)], limit=0,
                                           projection={"title": 1, "meta_line": 1, "total": 1, "plays": 1})
        ws = words.split()
        found = [r for r in rows if all(w in ((r.get("title") or "") + " " + (r.get("meta_line") or "")).lower() for w in ws)][:20]
    me = app.me.username
    results = []
    for q in found:
        text = (f"🎯 <b>{html.escape(q.get('title') or 'Quiz')}</b>\n<i>{html.escape(q.get('meta_line') or '')}</i>\n"
                f"🧩 {q.get('total') or len(q.get('questions') or [])} questions · ▶️ {q.get('plays', 0)} plays\n\n"
                f"Tap ▶️ to play with {html.escape(app.config.branding)}")
        results.append(InlineQueryResultArticle(
            title=q.get("title") or "Quiz", id=q["_id"],
            description=f"{q.get('total') or len(q.get('questions') or [])} questions · {q.get('meta_line') or ''}"[:120],
            input_message_content=InputTextMessageContent(text),
            reply_markup=kb([btn("▶️ Play", url=f"https://t.me/{me}?start={q['_id']}"),
                             btn("👥 Play in a group", url=f"https://t.me/{me}?startgroup={q['_id']}")],
                            *([[btn("📱 Play in the Web App", url=mini)]] if (mini := ui.miniapp_link(app, q["_id"])) else []))))
    try:
        await query.answer(results, cache_time=30, is_personal=False)
    except Exception:
        pass


# ------------------------------------------------------------------ my stats
async def my_stats(app, uid: int):
    doc = await app.repo.users.get(uid, copy=False) or {}
    qdb = doc.get("quiz_db") or {}
    attempted = list(qdb.get("attempted") or []) if isinstance(qdb.get("attempted"), list) else []
    lines = ["👤 <b>Your stats</b>", ""]
    lines.append(f"📊 Exams checked: <b>{len(doc.get('exam_db') or {})}</b>  ·  🧩 Quizzes made: <b>{len(qdb.get('created') or [])}</b>")
    lines.append(f"🎯 Quizzes played: <b>{len(attempted)}</b>")
    shown = 0
    for qid in reversed(attempted):
        if shown >= 10:
            break
        quiz = await store.get(app, qid)
        stats = await app.repo.quiz_stats.get(qid, copy=False)
        p = ((stats or {}).get("players") or {}).get(str(uid)) or {}
        if quiz is None or not p:
            continue
        shown += 1
        r = p.get("ranked")
        rk = store.rank_of(stats, uid)
        line = f"\n• <b>{html.escape(quiz['title'][:60])}</b>\n   "
        line += (f"🏆 {r['score']}/{quiz.get('total')} · #{rk[0]} of {rk[1]}" if r and rk
                 else f"🔁 last {p.get('last_score', 0)}/{quiz.get('total')}")
        line += f" · played {p.get('attempts', 0)}×"
        lines.append(line)
    rows = [[btn("🎯 Quizzes", cb("qzl", uid, "0", "all")), btn("👤 My quizzes", cb("qzl", uid, "0", "mine"))], nav_row(uid)]
    return "\n".join(lines), kb(*rows)
