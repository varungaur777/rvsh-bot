"""``/batch`` — response-sheet links in bulk (spec §14.7). Admins, private chat. The work itself: ``exam.batch``.

- ``/batch`` with links after it, or as a reply to a ``.txt`` / a message of links (raw lists welcome: duplicates,
  ``#`` lines, bare URLs, ``(date) Exam (Shift N): URL`` lines) → the batch card.
- ``/batch`` alone → the exams with saved links (``exam_urls``, 8 a page, ``📚 All exams``) → the batch card for that
  exam's saved links (Hindi + English per shift).
- **The card** (one message, ✅ green = chosen, like the exam prefs card): 📚 PDFs (each section + full / full /
  🧩 merged sections — each section of all shifts in one PDF / ⚡ one-liners — question + answer, all shifts; both
  come after the last shift), 🎨 style (custom / as in the sheet — not for one-liners, which are custom; merged as in
  the sheet = the portal look with a shift line on each page), 🗝 answers (custom, not one-liners: under each
  question / at section end), 🌐 language (both = 2 PDFs / Hindi /
  English), 🧪 run (all shifts / trial: the first 2) → ▶️ Start. Not answered within ``input_timeout_s`` → closed.
  OCR text comes later (the card says so).
- Start → a background-lane task beside the admin's own (``solo=False``); its progress message has ⏹ Stop (asks
  once more). A restart → "Resume?" card (``exam.batch.boot_notice``): ▶️ Resume / ✖️ Drop it.

Callback data ``bat:<uid>:<obj>:<arg>`` — ``pg`` (exam list page; obj = page) · ``e`` (an exam; obj = exam id or
``all``) · ``m_``/``s_``/``a_``/``l_``/``t_`` (card choices) · ``h_<row>`` (a row's title: hint) · ``go`` / ``no`` ·
``stop`` / ``stopy`` / ``stopn`` (obj = task id) · ``res`` / ``drop`` (obj = batch id) · ``x`` (close).

Depends on: ``exam.batch``, ``exam.urls``, ``handlers.links`` (pasted text, exam ids), ``tasks``. Used by:
``handlers/__init__``.
"""

from __future__ import annotations

import html
import logging
import math

from bot.core.access import guard
from bot.exam import batch as B
from bot.exam import urls as U
from bot.handlers import links as L
from bot.ui.keyboards import btn, cb, kb, parse_cb
from bot.ui.texts import t

log = logging.getLogger(__name__)
PAGE = 8
MAX_LINKS = 5000
_HINTS = {"m": "Per shift: full + sections, or full only · Merged: each section of all shifts in one PDF · "
               "One-liners: question + its answer, all shifts (both after the last shift)",
          "s": "Custom = the bot's clean layout from the exam bank; As in the sheet = the portal's own look",
          "a": "Custom PDFs: the answer under each question, or an answer grid at the end of each section",
          "l": "TCS: a shift's Hindi link + English link give both; EDQT: one link has both",
          "t": "Trial: only the first 2 shifts — to check the PDFs before a long run"}


@guard("admin", force_sub=False, private_only=True)
async def batch_command(client, message) -> None:
    app, uid = client.app, message.from_user.id
    try:
        text = await L.pasted_text(app, message)
    except L.TooBig:
        return await app.sender.send_message(message.chat.id, "📄 That file is too big — a link list is at most 2 MB.",
                                             reply_to_message_id=message.id)
    if text is None:
        body, markup = await _list_screen(app, uid, 0)
        return await app.sender.send_message(message.chat.id, body, reply_markup=markup)
    items = U.read_links(text)
    if not items:
        return await app.sender.send_message(
            message.chat.id, "🔎 No TCS (digialm) or EDQT (cbexams) response-sheet links found in that.\n"
            "<i>/batch alone shows the exams whose links are saved.</i>", reply_to_message_id=message.id)
    items = items[:MAX_LINKS]
    tcs = sum(1 for u, _ in items if U.detect_kind(u) == "tcs")
    codes = {U.shift_code(u) for u, _ in items} - {None}
    about = (f"🔗 <b>{len(items)} link{'s' if len(items) != 1 else ''}</b> (TCS {tcs} · EDQT {len(items) - tcs})"
             + (f" · about <b>{len(codes)}</b> TCS shift{'s' if len(codes) != 1 else ''}" if codes else "")
             + "\n<i>Links I don't know yet are read first (page only — per shift only until its Hindi and English "
               "sheets are found); you get the filtered list back.</i>")
    await _open_card(app, message.chat.id, uid, {"items": items, "title": "", "about": about}, reply_to=message.id)


async def _open_card(app, chat_id: int, uid: int, data: dict, reply_to: int | None = None, edit_id: int | None = None):
    sess = app.sessions.get(uid)
    sess.step, sess.data = "batch:prefs", {**data, "prefs": dict(B.PREFS)}
    body, markup = _card(uid, sess.data)
    if edit_id is not None:
        await app.sender.edit_text(chat_id, edit_id, body, reply_markup=markup)
        sess.ui_message_id = edit_id
    else:
        msg = await app.sender.send_message(chat_id, body, reply_markup=markup, reply_to_message_id=reply_to)
        sess.ui_message_id = msg.id
    app.sessions.arm_timeout(uid, app.settings.get("input_timeout_s") or 60, lambda u: _timeout(app, chat_id, u))


def _card(uid: int, d: dict):
    p = d["prefs"]
    body = ("📦 <b>Batch</b>" + (f" · {html.escape(d['title'])}" if d.get("title") else "") + f"\n{d['about']}\n\n"
            f"<b>Now:</b> {html.escape(B._prefs_line(p))}\n"
            "👇 Pick below — <b>✅ green</b> is chosen. Then tap <b>▶️ Start</b>.\n"
            "<i>Questions are kept as images from the sheet (OCR text comes with OCR). Each sheet's .mht goes to the dump "
            "and is remembered, so a shift works again after its link expires. Merged sections / one-liners: one PDF "
            "per section of all the shifts, made after the last one.</i>")

    def opt(label, arg, on):
        return btn(("✅ " if on else "") + label, cb("bat", uid, "", arg), style="success" if on else None)

    def title(text, row):
        return [btn(f"— {text} —", cb("bat", uid, "", f"h_{row}"))]
    oneliner = p["mode"] == "oneliner"
    rows = [title("📚 PDFs", "m"),
            [opt("Each section + Full", "m_each", p["mode"] == "each"), opt("Full only", "m_full", p["mode"] == "full")],
            [opt("🧩 Merged sections", "m_merged", p["mode"] == "merged"),
             opt("⚡ One-liners", "m_oneliner", p["mode"] == "oneliner")]]
    rows += [title("🎨 Style", "s"),  # one-liners as in the sheet too (owner 2026-10-19)
             [opt("Custom", "s_custom", p["style"] == "custom"),
              opt("As in the sheet", "s_original", p["style"] == "original")]]
    if p["style"] == "custom" and not oneliner:
        rows += [title("🗝 Answers", "a"),
                 [opt("Under each question", "a_after", p["answers"] == "after"),
                  opt("At section end", "a_grid", p["answers"] == "grid")]]
    rows += [title("🌐 Language", "l"),
             [opt("Both (2 PDFs)", "l_both", p["lang"] == "both"), opt("Hindi", "l_hi", p["lang"] == "hi"),
              opt("English", "l_en", p["lang"] == "en")],
             title("🧪 Run", "t"),
             [opt("All shifts", "t_0", not p["limit"]), opt(f"Trial: first {B.TRIAL}", f"t_{B.TRIAL}", bool(p["limit"]))],
             [btn("▶️ Start", cb("bat", uid, "", "go"), style="primary"),
              btn(t("btn_cancel"), cb("bat", uid, "", "no"), style="danger")]]
    return body, kb(*rows)


async def _timeout(app, chat_id: int, uid: int) -> None:
    sess = app.sessions.peek(uid)
    if not sess or sess.step != "batch:prefs":
        return
    msg_id = sess.ui_message_id
    app.sessions.clear(uid)
    if msg_id:
        try:
            await app.sender.edit_text(chat_id, msg_id, t("timeout"))
        except Exception:
            pass


async def _list_screen(app, uid: int, page: int):
    exams = await U.exams_with_links(app.repo)
    if not exams:
        return ("📦 <b>Batch</b>\nNo exam links saved yet — send <code>/batch</code> with links after it, or reply "
                "<code>/batch</code> to a .txt / message of links.", None)
    pages = max(1, math.ceil(len(exams) / PAGE))
    page = max(0, min(page, pages - 1))
    body = ("📦 <b>Batch</b> · pick an exam: its saved links, a <b>Hindi</b> and an <b>English</b> sheet per shift.\n"
            "<i>Or send</i> <code>/batch</code> <i>with links after it / as a reply to a .txt of links.</i>"
            + (f"\n<i>Page {page + 1}/{pages}</i>" if pages > 1 else ""))
    rows = [[btn(f"{(e['name'] or 'No name · ' + (e['code'] or '?'))[:30]} · 🇮🇳 {e['hi']} · 🇬🇧 {e['en']}"
                 + (f" · ? {e['unknown']}" if e["unknown"] else ""), cb("bat", uid, L.exam_id(e["slug"]), "e"))]
            for e in exams[page * PAGE:(page + 1) * PAGE]]
    nav = []
    if page:
        nav.append(btn("◀️", cb("bat", uid, str(page - 1), "pg")))
    if page + 1 < pages:
        nav.append(btn("▶️", cb("bat", uid, str(page + 1), "pg")))
    return body, kb(*rows, nav, [btn("📚 All exams", cb("bat", uid, "all", "e"), style="primary"),
                                 btn(t("btn_close"), cb("bat", uid, "", "x"), style="danger")])


# ------------------------------------------------------------------ buttons
@guard("admin", force_sub=False, private_only=True)
async def on_button(client, query) -> None:
    app, uid = client.app, query.from_user.id
    d = parse_cb(query.data)
    chat_id, msg_id = query.message.chat.id, query.message.id
    arg = d.arg
    if arg == "x":
        await app.sender.answer(query.id)
        return await app.sender.remove(chat_id, msg_id, "✖️ Closed.")
    if arg == "pg":
        await app.sender.answer(query.id)
        body, markup = await _list_screen(app, uid, int(d.obj or 0))
        return await app.sender.edit_text(chat_id, msg_id, body, reply_markup=markup)
    if arg == "e":
        picked = await L.pick(app, d.obj)
        if picked is None:
            return await app.sender.answer(query.id, "That exam has no saved links any more.", show_alert=True)
        title, slugs, c = picked
        await app.sender.answer(query.id)
        about = (f"🔗 Saved links · Hindi <b>{c['hi']}</b> · English <b>{c['en']}</b>"
                 + (f" · not known yet <b>{c['unknown']}</b> (read first, page only)" if c["unknown"] else ""))
        return await _open_card(app, chat_id, uid, {"slugs": slugs, "title": title, "about": about}, edit_id=msg_id)
    if arg in ("stop", "stopy", "stopn"):
        return await _stop(app, query, d.obj, arg)
    if arg in ("res", "drop"):
        return await _resume(app, query, d.obj, arg)
    sess = app.sessions.peek(uid)
    if not sess or sess.step != "batch:prefs" or sess.ui_message_id != msg_id:
        return await app.sender.answer(query.id, "This card has expired — send /batch again.", show_alert=True)
    if arg.startswith("h_"):
        return await app.sender.answer(query.id, _HINTS.get(arg[2:], ""))
    p = sess.data["prefs"]
    if arg == "no":
        app.sessions.clear(uid)
        await app.sender.answer(query.id)
        return await app.sender.edit_text(chat_id, msg_id, t("cancelled"))
    if arg == "go":
        app.sessions.disarm(uid)
        data = sess.data
        app.sessions.clear(uid)
        return await _start(app, query, data)
    key, _, val = arg.partition("_")
    field = {"m": "mode", "s": "style", "a": "answers", "l": "lang", "t": "limit"}.get(key)
    allowed = {"mode": ("each", "full", *B.COLLECT), "style": ("custom", "original"), "answers": ("after", "grid"),
               "lang": ("both", "hi", "en"), "limit": ("0", str(B.TRIAL))}
    if field is None or val not in allowed[field]:
        return await app.sender.answer(query.id)
    p[field] = int(val) if field == "limit" else val
    await app.sender.answer(query.id)
    app.sessions.arm_timeout(uid, app.settings.get("input_timeout_s") or 60, lambda u: _timeout(app, chat_id, u))
    body, markup = _card(uid, sess.data)
    await app.sender.edit_text(chat_id, msg_id, body, reply_markup=markup)


async def _start(app, query, data: dict, resume: str | None = None) -> None:
    uid, chat_id, msg_id = query.from_user.id, query.message.chat.id, query.message.id
    task = app.tasks.create(uid, "batch", "background", app.access.priority(uid), title="batch", chat_id=chat_id,
                            solo=False)
    await app.sender.answer(query.id)
    task.data.update(ui_message_id=msg_id, title=data.get("title") or "", prefs=data.get("prefs") or dict(B.PREFS))
    if resume:
        task.data["resume"] = resume
    elif data.get("items") is not None:
        task.data["items"] = data["items"]
    else:
        task.data["slugs"] = data.get("slugs")
    pos = await app.tasks.submit(task, lambda tk: B.run(app, tk))
    text = ("📦 <b>Batch starting…</b>" if pos <= 1 else
            f"📦 <b>Batch queued</b> — #{pos} on the background lane (it starts when the jobs before it finish).")
    await app.sender.edit_text(chat_id, msg_id, text, reply_markup=B._stop_markup(task))


async def _stop(app, query, task_id: str, arg: str) -> None:
    task = app.tasks.tasks.get(task_id)
    if task is None or task.kind != "batch" or task.user_id != query.from_user.id:
        return await app.sender.answer(query.id, "This batch has already ended.", show_alert=True)
    progress = task.data.get("progress")
    if arg == "stop":
        await app.sender.answer(query.id, "Stop the batch? Tap ⏹ Yes to confirm.")
        markup = kb([btn("⏹ Yes, stop", cb("bat", task.user_id, task_id, "stopy"), style="danger"),
                     btn("▶️ Keep going", cb("bat", task.user_id, task_id, "stopn"), style="success")])
    elif arg == "stopn":
        await app.sender.answer(query.id)
        markup = B._stop_markup(task)
    else:
        await app.sender.answer(query.id, "⏹ Stopping…")
        await app.tasks.cancel(task_id, f"admin {query.from_user.id}")
        return
    if progress is not None:
        progress.set_markup(markup)
    else:  # still queued: no progress message yet
        await app.sender.edit_text(query.message.chat.id, query.message.id, query.message.text or "📦 Batch",
                                   reply_markup=markup)


async def _resume(app, query, bid: str, arg: str) -> None:
    doc = await app.repo.batches.get(bid)
    chat_id, msg_id = query.message.chat.id, query.message.id
    if not doc or doc.get("status") != "running" or doc.get("user_id") != query.from_user.id:
        await app.sender.answer(query.id, "That batch is not waiting any more.", show_alert=True)
        return await app.sender.remove(chat_id, msg_id, "📦 Batch — nothing to resume.")
    if any(tk.kind == "batch" and tk.data.get("resume") == bid for tk in app.tasks.tasks.values()):
        return await app.sender.answer(query.id, "It is already running.", show_alert=True)
    if arg == "drop":
        await app.repo.batches.update(bid, {"$set": {"status": "dropped", "ended_at": B._now()}})
        await app.sender.answer(query.id)
        return await app.sender.edit_text(chat_id, msg_id, f"✖️ Batch dropped · {html.escape(doc.get('title') or '')}")
    await _start(app, query, {"title": doc.get("title"), "prefs": doc.get("prefs")}, resume=bid)
