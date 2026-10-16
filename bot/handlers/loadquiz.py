"""``/load_quiz`` (admins, spec §16.1) — reply to a file and the bot takes what it is:

- a **quiz file** ``qz_….json`` (the bot sends one to the main dump with every new quiz) or a ``.zip`` of them →
  the quizzes are put back (``quiz.loader``: same ID when free, cards / data still in the dump channels) → one report
  (✅ loaded · ♻️ already here · ❌ why not) and the quiz page when it was one quiz;
- an **exam bundle** ``.zip`` (``exam.json`` + ``img/`` — what the bot keeps for every exam) or an exam JSON, or a
  **question JSON** → the ``/json`` card (PDF / quiz, language, shuffle, explanations, seconds);
- a ``.zip`` of question JSON files → the ``/json_batch`` card.

A zip is downloaded once: its kind decides where it goes (the ``/json`` paths take the downloaded file).

Depends on: ``quiz.loader``, ``handlers.jsoncmd``. Used by: ``handlers/__init__``.
"""

from __future__ import annotations

import asyncio
import logging
import time
import zipfile

import orjson

from bot.core.access import guard
from bot.core.sender import NO_PREVIEW
from bot.handlers import jsoncmd
from bot.jsonq import parse
from bot.quiz import loader, ui

log = logging.getLogger(__name__)
REPORT_LINES = 40
HOW = ("📥 <b>Load quizzes</b>\n\nReply <code>/load_quiz</code> to:\n"
       "• a <b>quiz file</b> <code>qz_….json</code> — the file the bot puts in the main dump with every new quiz — or a "
       "<code>.zip</code> of them → the quizzes are back (same ID and links when the ID is free)\n"
       "• an <b>exam bundle</b> <code>.zip</code> (exam.json + img/) or an exam JSON, or a <b>question JSON</b> → "
       "PDF / quiz card\n• a <code>.zip</code> of question JSON files → the /json_batch card\n\n"
       "<i>Quiz files keep their cards in the image dump — the bot must be able to read that channel.</i>")


@guard("admin", private_only=True)
async def load_quiz_command(client, message) -> None:
    app, user = client.app, message.from_user
    reply = message.reply_to_message
    doc = reply.document if reply is not None else None
    if doc is None or not (jsoncmd.is_json(doc) or jsoncmd.is_zip(doc)):
        return await app.sender.send_message(message.chat.id, HOW, reply_to_message_id=message.id)
    if jsoncmd.is_json(doc):
        return await jsoncmd._take(app, message.chat.id, user, reply)
    if (doc.file_size or 0) > jsoncmd.ZIP_MAX:
        return await app.sender.send_message(message.chat.id, f"📦 <b>Zip too large</b> — at most "
                                             f"{jsoncmd.ZIP_MAX // 2**20} MB.", reply_to_message_id=message.id)
    tmp = app.config.download_root / "_tmp" / f"load_{user.id}_{int(time.time() * 1000)}.zip"
    tmp.parent.mkdir(parents=True, exist_ok=True)
    status = await app.sender.send_message(message.chat.id, "📥 <b>Reading the zip…</b>", reply_to_message_id=reply.id)
    try:
        fast = getattr(app, "fastdl", None)
        await (fast.download(reply, str(tmp)) if fast else app.client.download_media(reply, file_name=str(tmp)))
        kind, files = await asyncio.to_thread(_zip_kind, tmp)
        if kind == "exam":
            await app.sender.delete(message.chat.id, status.id)
            return await jsoncmd._take(app, message.chat.id, user, reply, src=tmp)
        if kind == "questions":
            await app.sender.delete(message.chat.id, status.id)
            return await jsoncmd._take_zip(app, message.chat.id, user, reply, src=tmp)
        if kind != "quizzes":
            return await app.sender.edit_text(message.chat.id, status.id, "😕 <b>Nothing to load in this zip</b> — no quiz "
                                              "files, exam bundle or question JSON files.\n\n" + HOW)
        await load_files(app, message.chat.id, user.id, files, status.id)
    except zipfile.BadZipFile:
        await app.sender.edit_text(message.chat.id, status.id, "😕 <b>This zip could not be opened.</b>")
    finally:
        tmp.unlink(missing_ok=True)


def _zip_kind(path) -> tuple[str, list]:
    """``exam`` (a bundle) · ``quizzes`` (with the quiz files read) · ``questions`` · ``none``."""
    with zipfile.ZipFile(path) as z:
        if "exam.json" in z.namelist():
            return "exam", []
    files = loader.read_zip(path)
    quizzes, questions = [], 0
    for name, raw in files:
        try:
            obj = orjson.loads(raw)
        except orjson.JSONDecodeError:
            quizzes.append((name, None))
            continue
        if parse.detect(obj):
            questions += 1
        else:  # a quiz file — or not, which the report then says
            quizzes.append((name, obj))
    if any(obj is not None and parse.is_quiz_doc(obj) for _n, obj in quizzes):
        return "quizzes", quizzes
    return ("questions", []) if questions else ("none", [])


async def load_files(app, chat_id: int, uid: int, files: list[tuple[str, dict | None]], status_id: int) -> None:
    """Quiz files → put back one by one (``loader.restore``) → one report; one quiz → its page too."""
    reach: dict = {}
    rows, done, posted = [], [], []
    counts = {"loaded": 0, "new_id": 0, "here": 0, "failed": 0}
    last = time.monotonic()
    for k, (name, obj) in enumerate(files, 1):
        if obj is None:
            status, quiz, why = "failed", None, "not readable JSON"
        elif not parse.is_quiz_doc(obj):
            status, quiz, why = "failed", None, "not a quiz file (a question JSON? use /json)"
        else:
            try:
                status, quiz, why = await loader.restore(app, obj, uid, reach)
            except Exception as e:
                status, quiz, why = "failed", None, f"error <code>{app.dump.report_error(e, f'load_quiz {name}')}</code>"
        counts[status] += 1
        rows.append(loader.line(status, quiz, name, why, app.me.username))
        if quiz is not None:
            done.append(quiz)
            if status in ("loaded", "new_id"):
                posted.append(quiz)
        if len(files) > 3 and time.monotonic() - last > 3:
            last = time.monotonic()
            await app.sender.edit_text(chat_id, status_id, f"📥 <b>Loading quizzes</b> · {k}/{len(files)}…")
    new = counts["loaded"] + counts["new_id"]
    head = (f"📥 <b>Load quiz</b> · {len(files)} file{'s' if len(files) != 1 else ''}\n"
            + " · ".join(x for x in (f"✅ {new} loaded" if new else "", f"♻️ {counts['here']} already here" if counts["here"] else "",
                                     f"❌ {counts['failed']} not loaded" if counts["failed"] else "") if x))
    body = "\n".join(rows[:REPORT_LINES]) + (f"\n… +{len(rows) - REPORT_LINES} more" if len(rows) > REPORT_LINES else "")
    text = (head + "\n━━━━━━━━━━━━━━━━\n" + body)[:4000]
    if len(done) == 1 and len(files) == 1:
        quiz = done[0]
        stats = await app.repo.quiz_stats.get(quiz["_id"], copy=False)
        await app.sender.edit_text(chat_id, status_id, (head + "\n\n" + ui.page_text(app, quiz, stats, uid))[:4000],
                                   reply_markup=ui.page_markup(app, uid, quiz, back=None), link_preview_options=NO_PREVIEW)
    else:
        await app.sender.edit_text(chat_id, status_id, text, link_preview_options=NO_PREVIEW)
    if new:
        app.dump.notify(f"📥 <b>/load_quiz</b> by <code>{uid}</code> · {new} quiz{'zes' if new != 1 else ''} loaded"
                        + (f" · ♻️ {counts['here']} already here" if counts["here"] else "")
                        + (f" · ❌ {counts['failed']}" if counts["failed"] else ""))
    loader.post_later(app, posted)


async def take_quiz_file(app, chat_id: int, uid: int, name: str, raw: bytes, status_id: int) -> None:
    """A quiz file reached ``/json`` / ``/load_quiz`` as one ``.json``: admins load it, others are told."""
    if not app.access.is_admin(uid):
        return await app.sender.edit_text(chat_id, status_id, "🎯 <b>This is a quiz file</b> — only admins can load quizzes "
                                          "(/load_quiz). To play a quiz, open its link or /quizzes.")
    try:
        obj = orjson.loads(raw)
    except orjson.JSONDecodeError:
        obj = None
    await load_files(app, chat_id, uid, [(name, obj)], status_id)

