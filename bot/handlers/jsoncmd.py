"""``/json`` — a question JSON file → PDF(s) / quiz(zes) (spec §16.8; owner 2026-10-13).

Flow: send the ``.json`` file (``/json`` explains; ``/json`` as a reply to a file takes that file) → it is read
(``jsonq.parse.inspect_file`` in the pool) → **one card** with every choice (owner's card style: a title button
above each choice, the chosen one green ✅): make 📄 PDF / 🎯 Quiz / both · 🌐 language (English and Hindi are always
**separate** PDFs / quizzes — "hindi english pdf alag alag hi banani hai, aur quiz bhi") · PDF: answers under each
question / answer key at the end / none, explanations under each question / at the end / none · quiz: shuffle the
questions (numbers stay 1…n), explanation after each question on / off, seconds per question → ▶️ Confirm → heavy
lane (``jsonq.runner.run_json``). Who: admins, or everyone with ``/qz → Everyone can use /json``; in a group only
an authorised one (``/auth``, owner 2026-10-14), where a ``/json_batch`` card also takes ``5-20`` from its user.

Callback data: ``jsn:<uid>:<task_id>:<arg>`` — ``m_<pdf|quiz|both>``, ``l_<both|en|hi>``, ``a_<after|grid|key|none>``
(``key`` = only the answer key grid: no answer under the questions, no explanations — owner 2026-10-15: "only anskey
grid ka bh option … explanation and answer nhi likhna"),
``x_<after|end|none>``, ``s_<0|1>`` (shuffle), ``e_<1|0>`` (quiz explanations), ``t_<s>``, ``h_<key>``, ``ok``, ``cancel``.
Session steps: ``json:read`` → ``json:prefs`` → (Confirm) ``json:queued``; ``input_timeout_s`` closes the card.

``/json_batch`` (owner 2026-10-13: "yahi kam bulk mai krwana hai, bas is bar ek zip file input dunga … zip file par
reply krne par kam kregi, sabhi json ke liye prefrence ek bare mai lelena"): as a reply to a ``.zip`` → every
``.json`` in it is read (``parse.inspect_zip``: natural name order, limits, files that are not question JSONs /
repeats skipped and listed) → the **same card once** for all files (``_batch_text``) → ``runner.run_json_batch``
does them one by one with those choices. The card says how many files the zip has and **which to make** (owner
2026-10-13: "zip mai kitni file hai, aur kha se leke kha tak banani hai ye bhi puchna"): `All N`, or send ``5-20``
(``on_range_text``). While it runs: ⏸ Pause / ▶️ Resume / ✖️ Cancel on the progress message (``jsb:<uid>:<task>:
pause|resume|cancel`` → ``on_batch_button``; "batch command ko pause and cancel bhi kia ja ske bich mai hi").

**Other files** (``parse.inspect_any``, spec §16.1): an exam bundle ``.zip`` / exam JSON gets the same card (its
own answer key per language; images already inside); a stored quiz file ``qz_….json`` goes to ``/load_quiz``
(admins). ``_take`` / ``_take_zip`` also take a file ``/load_quiz`` already downloaded (``src``).

Depends on: ``jsonq``, task lanes, sessions. Used by: ``handlers/__init__``, ``handlers.loadquiz``.
"""

from __future__ import annotations

import asyncio
import html
import logging
import re
import shutil

from pyrogram import StopPropagation, enums, filters

from bot.compute import run_cpu
from bot.core.access import guard
from bot.tasks.lanes import TaskExists
from bot.jsonq import parse
from bot.jsonq.runner import NAMES, batch_ctl, file_range, langs_out, run_json, run_json_batch
from bot.ui.keyboards import btn, cb, kb, parse_cb
from bot.ui.texts import t

log = logging.getLogger(__name__)
JSON_MAX = 30 * 1024 * 1024
ZIP_MAX = 2000 * 1024 * 1024  # Telegram's own file limit (owner 2026-10-14: "300 json etc ki limit hta do")
ZIP_SECONDS_PER_FILE = 0.25  # reading one JSON of a zip takes ≈ 0.03 s on a PC; the job may take this much per file
TIMES = (10, 15, 20, 30, 45, 60)
HINTS = {"m": "What to make from the file", "l": "English and Hindi are made as separate PDFs / quizzes",
         "a": "Where the right answers go in the PDF (Only key grid: the questions, a key at the end, no explanations)", "x": "Where the explanations go in the PDF",
         "s": "Mix the question order once (numbers stay 1, 2, 3 …)",
         "r": "Send the file numbers to make, like 5-20 (or one number). The list above shows each file's number.", "e": "After each answer: the explanation "
         "(short ones inside the poll, long ones as a picture)", "t": "Time for each question"}


def is_json(doc) -> bool:
    return bool(doc) and ((doc.file_name or "").lower().endswith(".json") or (doc.mime_type or "") == "application/json")


json_filter = filters.create(lambda _, __, m: bool(getattr(m, "document", None)) and is_json(m.document))


def _allowed(app, uid: int) -> bool:
    return bool(app.settings.get("feature.json_public")) or app.access.is_admin(uid)


LOCKED = "🔒 <b>/json is for admins right now.</b>"
HOW = ("📄 <b>PDF or quiz from a question JSON file</b>\n\nSend me the <code>.json</code> file (questions with "
       "<code>en</code> / <code>hi</code> parts, options, answers and explanations). I'll ask:\n"
       "• 📄 PDF or 🎯 quiz (or both)\n• 🌐 language — English and Hindi are made <b>separately</b>\n"
       "• PDF: answers under each question, as a key at the end, or only the key grid (no explanations); explanations "
       "under each question or at the end\n"
       "• Quiz: shuffle the questions, explanation after each answer, seconds per question\n\n"
       "<i>Image links in the file are downloaded and printed. The file's series name, time and marks are not used.</i>")


async def _group_closed(app, message, command: str) -> bool:
    """``/json`` / ``/json_batch`` in a group work only in an authorised one (owner 2026-10-14: "/json, /json_batch ye
    bhi owner/admin command hai, authorise group mai chal skti hai"); True (and told, when they may use it) otherwise."""
    if message.chat.type not in (enums.ChatType.GROUP, enums.ChatType.SUPERGROUP):
        return False
    if await app.access.group_authorised(message.chat):
        return False
    if _allowed(app, message.from_user.id):
        await app.sender.send_message(message.chat.id, f"🔒 {command} works in a group only after the owner authorises it "
                                      "(/auth). In a private chat with me it always works.", reply_to_message_id=message.id)
    return True


@guard()
async def json_command(client, message) -> None:
    app, user = client.app, message.from_user
    if await _group_closed(app, message, "/json"):
        return
    if not _allowed(app, user.id):
        return await app.sender.send_message(message.chat.id, LOCKED)
    reply = message.reply_to_message
    if reply is not None and reply.document and is_json(reply.document):
        return await _take(app, message.chat.id, user, reply)
    await app.sender.send_message(message.chat.id, HOW)


def is_zip(doc) -> bool:
    return bool(doc) and ((doc.file_name or "").lower().endswith(".zip")
                          or (doc.mime_type or "") in ("application/zip", "application/x-zip-compressed"))


HOW_BATCH = ("📦 <b>Many JSON files at once</b>\n\nPut the question JSON files in one <code>.zip</code>, send it, then "
             "<b>reply to the zip</b> with <code>/json_batch</code>.\nYou pick the choices <b>once</b> (PDF / quiz, "
             "language, answers, explanations, shuffle, time) and every file is done with them, one by one.\n\n"
             f"<i>Any number of files (a zip up to {ZIP_MAX // 2**20} MB), {parse.ZIP_FILE_MAX // 2**20} MB each. "
             "Files that are not question "
             "JSONs are skipped and listed.</i>")


@guard()
async def json_batch_command(client, message) -> None:
    """``/json_batch`` as a reply to a ``.zip`` of question JSON files (owner 2026-10-13)."""
    app, user = client.app, message.from_user
    if await _group_closed(app, message, "/json_batch"):
        return
    if not _allowed(app, user.id):
        return await app.sender.send_message(message.chat.id, LOCKED.replace("/json", "/json_batch"))
    reply = message.reply_to_message
    if reply is not None and reply.document and is_zip(reply.document):
        return await _take_zip(app, message.chat.id, user, reply)
    await app.sender.send_message(message.chat.id, HOW_BATCH, reply_to_message_id=message.id)


@guard(private_only=True)
async def on_document(client, message) -> None:
    app, user = client.app, message.from_user
    if not _allowed(app, user.id):
        return await app.sender.send_message(message.chat.id, LOCKED, reply_to_message_id=message.id)
    await _take(app, message.chat.id, user, message)


async def _fetch(app, msg, path, src=None) -> None:
    """The sent file into ``path`` (moved from ``src`` when ``/load_quiz`` downloaded it already)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if src is not None:
        shutil.move(str(src), str(path))
        return
    fast = getattr(app, "fastdl", None)
    await (fast.download(msg, str(path)) if fast else app.client.download_media(msg, file_name=str(path)))


NOT_READABLE = ("😕 <b>This is not a file I can read.</b>\n<i>A question JSON needs <code>questions</code> with "
                "<code>en</code> / <code>hi</code> parts (question, options, answer). Also taken: an exam bundle "
                "<code>.zip</code> (exam.json + img/) or exam JSON, and quiz files (/load_quiz).</i>")


async def _take(app, chat_id: int, user, msg, src=None) -> None:
    uid = user.id
    bundle = is_zip(msg.document)
    if (msg.document.file_size or 0) > (ZIP_MAX if bundle else JSON_MAX):
        return await app.sender.send_message(chat_id, f"📦 <b>File too large</b> — a JSON file is at most {JSON_MAX // 2**20} MB"
                                             + (f", a zip {ZIP_MAX // 2**20} MB." if bundle else "."),
                                             reply_to_message_id=msg.id)
    try:
        task = app.tasks.create(uid, "json", "light", app.access.priority(uid), title="json", chat_id=chat_id)
        app.tasks.hold(task)
    except TaskExists:
        return await app.sender.send_message(chat_id, t("task_exists"), reply_to_message_id=msg.id)
    sess = app.sessions.get(uid)
    if sess.step or sess.ui_message_id is not None:  # another menu was open: it closes
        app.sessions.clear(uid)
        sess = app.sessions.get(uid)
    sess.step, sess.task_id = "json:read", task.task_id
    task.data.update(user_name=" ".join(x for x in (user.first_name, user.last_name) if x) or str(uid),
                     username=user.username or "")
    status = await app.sender.send_message(chat_id, "📥 <b>Reading your file…</b>", reply_to_message_id=msg.id)
    sess.ui_message_id = task.data["ui_message_id"] = status.id
    try:
        await task.prepare()
        path = task.workdir / "in" / ("bundle.zip" if bundle else "questions.json")
        await _fetch(app, msg, path, src)
        info = await run_cpu(app, "parse", task.task_id, parse.inspect_any, str(path), str(task.workdir / "in" / "x"))
    except Exception as e:
        log.info("json file not readable: %r", e)
        await app.tasks.cancel(task.task_id, "unreadable")
        app.sessions.clear(uid)
        text = ("📦 <b>This exam JSON points to image files</b> — send its bundle <code>.zip</code> (exam.json + img/) "
                "instead." if "needs the bundle" in str(e) else NOT_READABLE)
        return await app.sender.edit_text(chat_id, status.id, text)
    if info.get("kind") == "quiz":  # a stored quiz (qz_….json) → /load_quiz
        from bot.handlers.loadquiz import take_quiz_file
        raw = path.read_bytes()
        await app.tasks.cancel(task.task_id, "quiz file")
        app.sessions.clear(uid)
        return await take_quiz_file(app, chat_id, uid, msg.document.file_name or "quiz.json", raw, status.id)
    if not langs_out(info, "both"):
        await app.tasks.cancel(task.task_id, "empty")
        return await app.sender.edit_text(chat_id, status.id, "😕 <b>No usable question</b> — every question needs at least 2 "
                                          "options and its answer.")
    task.data["json_path"], task.data["json"] = str(path), info
    has_x = bool(info.get("explanations"))
    sess.prefs = {"make": "pdf", "lang": "both", "answers": "after", "explain": "after" if has_x else "none",
                  "shuffle": False, "qexplain": has_x, "time": int(app.settings.get("quiz.default_question_time_s") or 30)}
    sess.step = "json:prefs"
    await app.sender.edit_text(chat_id, status.id, card_text(info, sess.prefs), reply_markup=card_markup(uid, task.task_id, info, sess.prefs))
    app.sessions.arm_timeout(uid, app.settings.get("input_timeout_s"), lambda u: _timeout(app, chat_id, u))


async def _take_zip(app, chat_id: int, user, msg, src=None) -> None:
    uid = user.id
    if (msg.document.file_size or 0) > ZIP_MAX:
        return await app.sender.send_message(chat_id, f"📦 <b>Zip too large</b> — at most {ZIP_MAX // 2**20} MB.",
                                             reply_to_message_id=msg.id)
    try:
        task = app.tasks.create(uid, "json", "light", app.access.priority(uid), title="json batch", chat_id=chat_id)
        app.tasks.hold(task)
    except TaskExists:
        return await app.sender.send_message(chat_id, t("task_exists"), reply_to_message_id=msg.id)
    sess = app.sessions.get(uid)
    if sess.step or sess.ui_message_id is not None:
        app.sessions.clear(uid)
        sess = app.sessions.get(uid)
    sess.step, sess.task_id = "json:read", task.task_id
    task.data.update(user_name=" ".join(x for x in (user.first_name, user.last_name) if x) or str(uid),
                     username=user.username or "")
    status = await app.sender.send_message(chat_id, "📥 <b>Reading your zip…</b>", reply_to_message_id=msg.id)
    sess.ui_message_id = task.data["ui_message_id"] = status.id
    try:
        await task.prepare()
        path = task.workdir / "in" / "batch.zip"
        await _fetch(app, msg, path, src)
        # no file-count limit: the time limit grows with the files, the unpacked size stops at the free disk
        n = await asyncio.to_thread(parse.zip_json_count, str(path))
        room = shutil.disk_usage(str(task.workdir)).free - int(app.settings.get("governor.disk_min_free_mb") or 0) * 2**20
        res = await run_cpu(app, "parse", task.task_id, parse.inspect_zip, str(path), str(task.workdir / "in" / "json"),
                            max(0, room), timeout=float(app.settings.get("governor.job_timeout_s") or 60)
                            + n * ZIP_SECONDS_PER_FILE)
    except Exception as e:
        log.info("json zip not readable: %r", e)
        await app.tasks.cancel(task.task_id, "unreadable")
        return await app.sender.edit_text(chat_id, status.id, "😕 <b>This zip could not be opened.</b> Send a normal "
                                          "(not password-protected) <code>.zip</code>.")
    usable = [f for f in res["files"] if langs_out(f["info"], "both")]
    res["empty"] = [f["name"] for f in res["files"] if f not in usable]
    res["files"] = usable
    res["zip_name"] = msg.document.file_name or "batch.zip"
    if not usable:
        await app.tasks.cancel(task.task_id, "empty")
        return await app.sender.edit_text(chat_id, status.id, "😕 <b>No usable question JSON in this zip.</b>\n"
                                          + _skipped_lines(res) + "\n<i>Each file needs <code>questions</code> with "
                                          "<code>en</code> / <code>hi</code> parts (question, options, answer).</i>")
    info = batch_summary(usable)
    task.data["batch"], task.data["json"] = res, info
    has_x = bool(info.get("explanations"))
    sess.prefs = {"make": "pdf", "lang": "both", "answers": "after", "explain": "after" if has_x else "none",
                  "shuffle": False, "qexplain": has_x, "time": int(app.settings.get("quiz.default_question_time_s") or 30)}
    sess.step = "json:prefs"
    sess.prefs["range"] = [1, len(usable)]
    await _show_card(app, chat_id, status.id, uid, task, sess.prefs)
    app.sessions.arm_timeout(uid, app.settings.get("input_timeout_s"), lambda u: _timeout(app, chat_id, u))


def batch_summary(files: list[dict]) -> dict:
    """The card's numbers for every file together (the language row shows when any file has both languages)."""
    langs: dict[str, int] = {}
    for f in files:
        for k, v in (f["info"].get("languages") or {}).items():
            langs[k] = langs.get(k, 0) + v
    infos = [f["info"] for f in files]
    return {"title": f"{len(files)} JSON files", "questions": sum(i["questions"] for i in infos), "languages": langs,
            "same": all(i.get("same") or len(i.get("languages") or {}) < 2 for i in infos),
            "images": sum(i["images"] for i in infos), "explanations": sum(i["explanations"] for i in infos),
            "unusable": sum(i.get("unusable") or 0 for i in infos), "files": len(files)}


def _skipped_lines(res: dict) -> str:
    out = []
    for key, what in (("bad", "not question JSONs / unreadable"), ("empty", "no usable question"),
                      ("big", f"over {parse.ZIP_FILE_MAX // 2**20} MB or no disk room left"), ("dups", "same questions as another file")):
        names = res.get(key) or []
        if names:
            shown = ", ".join(html.escape(n) for n in names[:8]) + (f" … +{len(names) - 8}" if len(names) > 8 else "")
            out.append(f"⚠️ {len(names)} skipped — {what}: <i>{shown}</i>")
    return "\n".join(out)


def card_text(info: dict, p: dict, batch: dict | None = None) -> str:
    if batch is not None:
        return _batch_text(info, p, batch)
    langs = info.get("languages") or {}
    if info.get("same") and len(langs) > 1:
        lang_line = "English + Hindi (both in one text)"
    else:
        lang_line = " · ".join(f"{'English' if k == 'en' else 'Hindi'} {v}" for k, v in langs.items())
    lines = [f"📄 <b>{html.escape(info['title'])}</b>"
             + (f"\n🏷 <i>{html.escape(info['exam_label'])}</i>" if info.get("exam_label") else ""),
             f"🧩 <b>{info['questions']}</b> questions · 🌐 {lang_line}",
             f"🖼 {info['images']} {'images' if info.get('bundle') else 'image links'} · 💡 {info['explanations']} explanations"]
    if info.get("unusable"):
        lines.append(f"⚠️ {info['unusable']} questions without options / answer are left out.")
    outs = langs_out(info, p["lang"])
    what = {"pdf": "PDF", "quiz": "quiz", "both": "PDF + quiz"}[p["make"]]
    lines += ["", f"👉 You'll get: <b>{', '.join(lbl for _l, lbl in outs)}</b> {what}" + ("s" if len(outs) > 1 else ""),
              "<i>The file's time and marks are not used.</i>" if not info.get("bundle") else
              "<i>Exam data (bundle): its own answer key per language; quiz marks +1 each.</i>",
              "👇 <b>✅ green</b> is chosen — tap to change, then ▶️ Confirm."]
    return "\n".join(lines)


def _batch_text(info: dict, p: dict, res: dict) -> str:
    files = res["files"]
    langs = " · ".join(f"{NAMES.get(k, k)} {v}" for k, v in (info.get("languages") or {}).items())
    listing = "\n".join(f"{n}. {html.escape(f['info']['title'][:70])} · {f['info']['questions']} q"
                        for n, f in enumerate(files[:40], 1)) + (f"\n… +{len(files) - 40} more" if len(files) > 40 else "")
    what = {"pdf": "PDFs", "quiz": "quizzes", "both": "PDFs + quizzes"}[p["make"]]
    lang = {"both": "English and Hindi separately", "en": "English", "hi": "Hindi"}[p["lang"]]
    n = len(files)
    first, last = file_range(p, n)
    entries = res.get("entries", n)
    lines = [f"📦 <b>{html.escape(res.get('zip_name') or 'batch.zip')}</b> · <b>{n}</b> JSON file{'s' if n != 1 else ''}"
             + (f" <i>(of {entries} .json in the zip)</i>" if entries != n else ""),
             f"🧩 <b>{info['questions']}</b> questions · 🌐 {langs}",
             f"🖼 {info['images']} image links · 💡 {info['explanations']} explanations",
             f"<blockquote expandable>{listing}</blockquote>"]
    skipped = _skipped_lines(res)
    if skipped:
        lines.append(skipped)
    lines += ["", f"🔢 Files to make: <b>{first}–{last}</b>" + (" (all)" if (first, last) == (1, n) else f" ({last - first + 1} of {n})")
              + " — <i>to make only some, send the numbers like</i> <code>5-20</code>",
              f"👉 For <b>every file</b>: {what} · {lang} <i>(a file with one language: just that one)</i>",
              "<i>One set of choices for all files — they are done one by one.</i>",
              "👇 <b>✅ green</b> is chosen — tap to change, then ▶️ Confirm."]
    return "\n".join(lines)[:4000]


def card_markup(uid: int, tid: str, info: dict, p: dict, batch_n: int = 0):
    def title(text, key):
        return [btn(f"— {text} —", cb("jsn", uid, tid, f"h_{key}"))]

    def pick(text, arg, on):
        return btn(("✅ " if on else "") + text, cb("jsn", uid, tid, arg), style="success" if on else None)

    rows = []
    if batch_n:  # owner 2026-10-13: "zip mai kitni file hai, aur kha se leke kha tak banani hai ye bhi puchna"
        first, last = file_range(p, batch_n)
        rows.append(title(f"🔢 Files {first}–{last} of {batch_n}", "r"))
        rows.append([pick(f"All {batch_n}", "r_all", (first, last) == (1, batch_n)),
                     btn("✏️ Some: send 5-20", cb("jsn", uid, tid, "h_r"))])
    rows += [title("📦 Make", "m"), [pick("📄 PDF", "m_pdf", p["make"] == "pdf"), pick("🎯 Quiz", "m_quiz", p["make"] == "quiz"),
                                    pick("📄 + 🎯 Both", "m_both", p["make"] == "both")]]
    langs = list((info.get("languages") or {}))
    if len(langs) > 1 and not info.get("same"):
        rows.append(title("🌐 Language", "l"))
        rows.append([pick("Both (separate)", "l_both", p["lang"] == "both"), pick("English", "l_en", p["lang"] == "en"),
                     pick("Hindi", "l_hi", p["lang"] == "hi")])
    has_x = bool(info.get("explanations"))
    if p["make"] in ("pdf", "both"):
        rows.append(title("🗝 Answers in the PDF", "a"))
        rows.append([pick("Under each", "a_after", p["answers"] == "after"), pick("Key at the end", "a_grid", p["answers"] == "grid")])
        rows.append([pick("Only key grid (no explanations)", "a_key", p["answers"] == "key"), pick("None", "a_none", p["answers"] == "none")])
        if has_x and p["answers"] != "key":
            rows.append(title("💡 Explanations in the PDF", "x"))
            rows.append([pick("Under each", "x_after", p["explain"] == "after"), pick("At the end", "x_end", p["explain"] == "end"),
                         pick("None", "x_none", p["explain"] == "none")])
    if p["make"] in ("quiz", "both"):
        rows.append(title("🔀 Shuffle the questions", "s"))
        rows.append([pick("No (file order)", "s_0", not p["shuffle"]), pick("Yes, shuffle", "s_1", p["shuffle"])])
        if has_x:
            rows.append(title("💡 Explanation after each answer", "e"))
            rows.append([pick("On", "e_1", p["qexplain"]), pick("Off", "e_0", not p["qexplain"])])
        rows.append(title("⏱ Seconds per question", "t"))
        times = [pick(f"{v}s", f"t_{v}", p["time"] == v) for v in TIMES]
        rows += [times[:3], times[3:]]
    return kb(*rows, [btn("▶️ Confirm", cb("jsn", uid, tid, "ok"), style="primary")],
              [btn(t("btn_cancel"), cb("jsn", uid, tid, "cancel"), style="danger")])


@guard()
async def on_button(client, query) -> None:
    app, uid = client.app, query.from_user.id
    data = parse_cb(query.data)
    sess = app.sessions.peek(uid)
    task = app.tasks.tasks.get(data.obj)
    chat_id, msg_id = query.message.chat.id, query.message.id
    if sess is None or task is None or task.user_id != uid or sess.ui_message_id != msg_id or sess.step != "json:prefs":
        return await app.sender.answer(query.id, "This menu has expired — send the file again.", show_alert=True)
    arg = data.arg
    if arg.startswith("h_"):
        return await app.sender.answer(query.id, HINTS.get(arg[2:], ""))
    async with sess.lock:
        if sess.step != "json:prefs" or task.closed:
            return await app.sender.answer(query.id)
        p = sess.prefs
        if arg == "cancel":
            sess.step = "json:cancelled"
            await app.sender.answer(query.id)
            await app.tasks.cancel(task.task_id, "user")
            return await app.sender.edit_text(chat_id, msg_id, "✖️ <b>Cancelled.</b> Send a JSON file any time.")
        if arg == "ok":
            sess.step = "json:queued"
            await app.sender.answer(query.id)
            app.sessions.disarm(uid)
            task.lane = "heavy"
            task.data["prefs"] = dict(p)
            runner = run_json_batch if task.data.get("batch") else run_json
            try:
                pos = await app.tasks.submit(task, lambda tk: runner(app, tk))
            except Exception:
                sess.step = "json:prefs"
                raise
            return await app.sender.edit_text(chat_id, msg_id, "⚙️ <b>Working on your file…</b>" if pos <= 1 else t("queued", pos=pos))
        key, _, val = arg.partition("_")
        if key == "m" and val in ("pdf", "quiz", "both"):
            p["make"] = val
        elif key == "l" and val in ("both", "en", "hi"):
            p["lang"] = val
        elif key == "a" and val in ("after", "grid", "key", "none"):
            p["answers"] = val
        elif key == "x" and val in ("after", "end", "none"):
            p["explain"] = val
        elif key == "s":
            p["shuffle"] = val == "1"
        elif key == "e":
            p["qexplain"] = val == "1"
        elif key == "t" and val.isdigit():
            p["time"] = int(val)
        elif key == "r" and val == "all" and task.data.get("batch"):
            p["range"] = [1, len(task.data["batch"]["files"])]
        await app.sender.answer(query.id)
        await _show_card(app, chat_id, msg_id, uid, task, p)
        app.sessions.arm_timeout(uid, app.settings.get("input_timeout_s"), lambda u: _timeout(app, chat_id, u))


async def _show_card(app, chat_id: int, msg_id: int, uid: int, task, p: dict) -> None:
    info, batch = task.data["json"], task.data.get("batch")
    await app.sender.edit_text(chat_id, msg_id, card_text(info, p, batch),
                               reply_markup=card_markup(uid, task.task_id, info, p, len(batch["files"]) if batch else 0))


_RANGE = re.compile(r"^\s*(\d{1,6})\s*(?:(?:-|–|—|to)\s*(\d{1,6}))?\s*$", re.I)


def _range_waiting(_, client, m) -> bool:
    """``5-20`` from the user whose ``/json_batch`` card is open, in the chat of that card (private or a group)."""
    if not m.from_user or not m.chat or not _RANGE.match(m.text or ""):
        return False
    sess = client.app.sessions.peek(m.from_user.id)
    if sess is None or sess.step != "json:prefs" or not sess.task_id:
        return False
    task = client.app.tasks.tasks.get(sess.task_id)
    return bool(task is not None and task.data.get("batch") and task.chat_id == m.chat.id)


range_filter = filters.create(_range_waiting)


async def on_range_text(client, message) -> None:
    """``5-20`` (or ``7``) typed while a ``/json_batch`` card is open → only those files are made."""
    try:
        await _take_range(client, message)
    finally:
        raise StopPropagation  # never an exam input


@guard()
async def _take_range(client, message) -> None:
    app, uid = client.app, message.from_user.id
    sess = app.sessions.peek(uid)
    task = app.tasks.tasks.get(sess.task_id) if sess and sess.task_id else None
    if task is None or not task.data.get("batch") or sess.step != "json:prefs":
        return
    m = _RANGE.match(message.text or "")
    n = len(task.data["batch"]["files"])
    a = int(m.group(1))
    b = int(m.group(2) or a)
    if not (1 <= a <= n and 1 <= b <= n):
        return await app.sender.send_message(message.chat.id, f"🔢 The files are numbered 1–{n}. Send like "
                                             f"<code>1-{n}</code>.", reply_to_message_id=message.id)
    async with sess.lock:
        sess.prefs["range"] = [min(a, b), max(a, b)]
        await _show_card(app, message.chat.id, sess.ui_message_id, uid, task, sess.prefs)
    app.sessions.arm_timeout(uid, app.settings.get("input_timeout_s"), lambda u: _timeout(app, message.chat.id, u))
    a, b = sess.prefs["range"]
    await app.sender.send_message(message.chat.id, f"✅ Files <b>{a}–{b}</b> ({b - a + 1} of {n}) — see the card above, "
                                  "then ▶️ Confirm.", reply_to_message_id=message.id)


@guard()
async def on_batch_button(client, query) -> None:
    """⏸ Pause / ▶️ Resume / ✖️ Cancel on a running ``/json_batch`` (owner 2026-10-13)."""
    app, uid = client.app, query.from_user.id
    d = parse_cb(query.data)
    task = app.tasks.tasks.get(d.obj)
    ctl = batch_ctl(d.obj)
    if task is None or task.closed or task.user_id != uid:
        return await app.sender.answer(query.id, "This batch has ended.", show_alert=True)
    if d.arg == "cancel":
        await app.sender.answer(query.id, "Cancelling…")
        await app.tasks.cancel(task.task_id, "user")
        return
    if ctl is None:  # still waiting in the queue
        return await app.sender.answer(query.id, "It hasn't started yet — ✖️ Cancel works now, pause once it runs.")
    if d.arg == "pause":
        ok = ctl.pause()
        return await app.sender.answer(query.id, "⏸ Pausing after the current step…" if ok else "Already paused.")
    if d.arg == "resume":
        ok = ctl.resume()
        return await app.sender.answer(query.id, "▶️ Going on." if ok else "It is running.")
    await app.sender.answer(query.id)


async def _timeout(app, chat_id: int, uid: int) -> None:
    sess = app.sessions.peek(uid)
    if sess is None or sess.step not in ("json:prefs", "json:read"):
        return
    task = app.tasks.tasks.get(sess.task_id) if sess.task_id else None
    msg_id = sess.ui_message_id
    sess.step = "json:timeout"
    if task is not None:
        await app.tasks.cancel(task.task_id, "timeout", status="timeout")
    if msg_id:
        try:
            await app.sender.edit_text(chat_id, msg_id, "⌛ <b>Timed out</b> — send the JSON file again any time.")
        except Exception:
            pass
