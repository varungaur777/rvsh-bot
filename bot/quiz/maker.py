"""Quiz creation and HTML files (spec §16): task runners.

``run_make(app, task)`` (heavy lane; ``🎯 Make Quiz`` on an exam): the exam from the bank (``exam.registry.ensure`` —
reuse, else build) → per scope (the full exam, or one quiz per section) → **dedup** by fingerprint (same exam
version + language + scope → the existing quiz) → questions (``quiz.build``) → cards drawn in the pool
(``render.quiz_card``, chunks of ``CHUNK``) and uploaded to the **image dump** by the uploader team, in parallel, with
progress (``🖼 Cards 34/80``) → quiz saved → a post to play it (▶️ Play / 👥 Play in a group) in the quiz dump channel
(``QUIZ_DUMP_CHANNEL_ID``, else the dump) and the quiz JSON in the main dump → the quiz page(s) to the user.

Cards are drawn once per exam question + language + card look and shared by every quiz of that exam (full and
section quizzes): their dump refs live in ``exams.quiz_cards.c<CARD_LOOK>_<lang>.<qref>`` and are saved after each
chunk, so a cancelled or restarted creation continues where it stopped.

``run_html(app, task)`` (light when cached): the quiz's offline HTML file (``quiz.html``) — cached on the quiz
(``html.<look>``) as a dump copy, so the next request is a ``copy_message``.

Depends on: ``exam.registry``, ``quiz.build``, ``quiz.store``, ``quiz.html``, ``render.quiz_card``, ``core.delivery``,
uploader pool. Used by: ``handlers.exam`` (Make Quiz), ``handlers.quiz`` (HTML file).
"""

from __future__ import annotations

import asyncio
import hashlib
import html
import logging
import re
import shutil
import time

import orjson

from bot.compute import run_cpu
from bot.core.sender import NO_PREVIEW
from bot.core.delivery import deliver
from bot.exam.keys import meta_line
from bot.exam.marks import short_section
from bot.quiz import build, store
from bot.ui.keyboards import btn, kb
from bot.ui.progress import ProgressMessage

log = logging.getLogger(__name__)
CARD_LOOK = 2  # bump when the card design changes: cards of the old look are drawn again (2: tables, tall pictures)
CHUNK = 6
UPLOADS_AT_ONCE = 10
LANG_NAMES = {"en": "English", "hi": "Hindi", "both": "English + Hindi"}


def _meta(exam: dict):
    from bot.exam.registry import _meta as m
    return m(exam)


def card_look(app) -> str:
    """8 hex digits of the ``/ps`` card colours, ``""`` while they are all default — part of the card cache slot."""
    colors = {k: v for k, v in (app.settings.get("card.colors") or {}).items() if v}
    if not colors:
        return ""
    return hashlib.sha1(repr(sorted(colors.items())).encode()).hexdigest()[:8]


def html_look() -> str:
    from bot.quiz.html import TEMPLATE
    return hashlib.sha1(TEMPLATE.read_bytes()).hexdigest()[:8]


async def run_make(app, task) -> None:
    progress = ProgressMessage(app.sender, task.chat_id, interval_s=app.settings.get("edit_interval_s"), task=task)
    progress.attach(task.data["ui_message_id"])
    try:
        await _make(app, task, progress)
    finally:
        progress.close()


async def _make(app, task, progress: ProgressMessage) -> None:
    from bot.core.tgref import download
    from bot.exam.registry import ensure
    from bot.quiz import ui

    prefs = task.data["prefs"]
    progress.set("📚 <b>Preparing the questions…</b>")
    bank = await ensure(app, task)
    key = bank["key"]
    rec = await app.repo.exams.get(key) or {}
    version = int(rec.get("version") or 1)
    lang = prefs.get("lang") or "en"
    sections = [s.get("name", "") for s in rec.get("sections") or [] if isinstance(s, dict)]
    scopes = list(range(len(sections))) if prefs.get("scope") == "each" and len(sections) > 1 else [None]
    path = bank.get("path")
    made: list[tuple[dict, bool]] = []
    skipped = 0
    for n_scope, idx in enumerate(scopes, 1):
        task.check()
        fp = build.fingerprint(f"{rec.get('fingerprint')}|v{version}", lang, idx)
        old = await store.find_by_fp(app, fp)
        if old:
            made.append((old, True))
            continue
        if path is None:
            progress.set("📥 <b>Loading the question paper…</b>")
            path = str(await download(app, bank["ref"], task.workdir / "bank"))
        what = f" · {html.escape(short_section(sections[idx]))} ({n_scope}/{len(scopes)})" if idx is not None else ""
        progress.set(f"🧩 <b>Building the quiz</b>{what}…")
        b = await run_cpu(app, "parse", task.task_id, build.build_questions, path, lang, idx)
        skipped += b["skipped"]
        if not b["questions"]:
            continue
        refs = await cards_for(app, task, progress, key, rec, path, lang, b["cards"], what)
        questions = []
        for q in b["questions"]:
            if q["mode"] == "card":
                q["cards"] = refs.get(q["ref"]) or []
                if not q["cards"]:  # its card could not be made: a text poll if the text exists, else skipped
                    if not q.get("text"):
                        skipped += 1
                        continue
                    q["mode"], q["poll"] = "poll", {"q": q["text"][:build.POLL_Q], "o": list("ABCDEFGHIJ"[:q["n"]])}
            questions.append(q)
        quiz = await _save(app, task, key, rec, version, lang, idx, sections, b, questions, fp, prefs)
        await _announce(app, task, quiz)
        made.append((quiz, False))
    if not made:
        return await progress.finish("😕 <b>No quiz could be made</b> — no question of this sheet has its answer and "
                                     "options." + (f"\n<i>{skipped} questions skipped.</i>" if skipped else ""))
    new = sum(1 for _q, old in made if not old)
    head = ("✅ <b>Your quiz is ready!</b>" if len(made) == 1 else f"✅ <b>{len(made)} quizzes are ready!</b>")
    if new < len(made):
        head += "\n<i>This exam already had " + ("this quiz" if len(made) == 1 else f"{len(made) - new} of them")\
                + " — here it is" + ("" if len(made) == 1 else " too") + ".</i>"
    if skipped:
        head += f"\n<i>{skipped} questions skipped (no answer or options on the sheet).</i>"
    first, _ = made[0]
    stats = await app.repo.quiz_stats.get(first["_id"], copy=False)
    await progress.finish(head + "\n\n" + ui.page_text(app, first, stats, task.user_id),
                          reply_markup=ui.page_markup(app, task.user_id, first, back=None))
    for quiz, _old in made[1:]:
        stats = await app.repo.quiz_stats.get(quiz["_id"], copy=False)
        await app.sender.send_message(task.chat_id, ui.page_text(app, quiz, stats, task.user_id),
                                      reply_markup=ui.page_markup(app, task.user_id, quiz, back=None))


async def cards_for(app, task, progress, key: str, rec: dict, path: str, lang: str, items: list[dict], what: str,
                    slot: str | None = None, label: str = "Cards") -> dict:
    """Card dump refs for ``items`` ([{qref, number, section, kind?}] — ``kind`` ``"x"`` = explanation cards): the ones
    the exam already has in ``exams.quiz_cards.<slot>``, the rest drawn + uploaded now (saved after every chunk)."""
    from bot.render.quiz_card import render_cards

    slot = slot or f"c{CARD_LOOK}_{lang}"
    look = card_look(app)
    if look:  # /ps card colours changed: cards of the new colours (the default ones keep the plain slot)
        slot = f"{slot}_{look}"
    have = dict(((rec.get("quiz_cards") or {}).get(slot)) or {})
    need = [c for c in items if c["qref"] not in have]
    total, done = len(items), len(items) - len(need)
    if not need:
        return have
    exam = rec.get("exam") or {}
    header = {"exam_name": exam.get("exam_name") or "Quiz", "meta_line": meta_line(_meta(exam)) or exam.get("exam_label") or "",
              "branding": app.config.branding}
    chat = app.config.image_dump_channel_id or app.config.dump_channel_id
    sem = asyncio.Semaphore(UPLOADS_AT_ONCE)
    progress.set(f"🖼 <b>Drawing the {label.lower()}</b>{what} · 0/{total}…" if not done else f"🖼 <b>{label}</b>{what} · {done}/{total}…")
    kinds = {c["qref"]: c.get("kind") for c in need}

    async def up(qref: str, number, files: list[str]) -> tuple[str, list[dict]]:
        out = []
        for i, f in enumerate(files, 1):
            async with sem:
                cap = f"🃏 {html.escape(exam.get('exam_name') or 'Quiz')} · Q{number}" + (" · 💡 explanation" if kinds.get(qref) == "x" else "") \
                      + (f" · {i}/{len(files)}" if len(files) > 1 else "") \
                      + f" · {LANG_NAMES.get(lang, lang)}\n<code>{html.escape(key)}</code>"
                _bot, msg = await app.uploads.send_photo(chat, f, cap[:1000])
            out.append({"chat_id": msg.chat.id, "message_id": msg.id})
        return qref, out

    work = task.workdir / "cards"
    for k in range(0, len(need), CHUNK):
        task.check()
        chunk = need[k:k + CHUNK]
        ctx = {"bundle": path, "workdir": str(task.workdir / "card_img"), "lang": lang, "header": header,
               "colors": app.settings.get("card.colors") or {},
               "questions": chunk, "max_h_px": int(app.settings.get("quiz.card_max_height_px") or 1800)}
        res = await run_cpu(app, "render", task.task_id, render_cards, ctx, str(work), est_mb=120)
        numbers = {c["qref"]: c["number"] for c in chunk}
        got = await asyncio.gather(*(up(q, numbers.get(q), files) for q, files in res["cards"].items()),
                                   return_exceptions=True)
        upd = {}
        for g in got:
            if isinstance(g, BaseException):
                if isinstance(g, asyncio.CancelledError):
                    raise g
                log.warning("quiz card upload failed: %r", g)
                continue
            qref, refs = g
            have[qref] = refs
            upd[f"quiz_cards.{slot}.{qref}"] = refs
        if upd:
            await app.repo.exams.update(key, {"$set": upd})
        done += len(chunk)
        progress.set(f"🖼 <b>{label}</b>{what} · {done}/{total}…")
        shutil.rmtree(work, ignore_errors=True)
    return have


def _title(exam: dict, section: str | None) -> str:
    name = exam.get("exam_name") or "Quiz"
    return f"{name} · {short_section(section, 40)}" if section else name


async def _save(app, task, key, rec, version, lang, idx, sections, b, questions, fp, prefs) -> dict:
    exam = rec.get("exam") or {}
    qid = store.new_id()
    while await app.repo.quizzes.get(qid, copy=False):
        qid = store.new_id()
    section = sections[idx] if idx is not None else None
    doc = {"title": _title(exam, section), "exam_key": key, "json_ref": rec.get("json_ref"), "version": version,
           "meta": {k: exam.get(k) for k in ("exam_name", "level", "date", "shift", "time", "source")},
           "meta_line": meta_line(_meta(exam)), "scope": section or "Full exam", "section": idx, "lang": lang,
           "languages": b.get("languages") or [], "sections": b["sections"], "questions": questions,
           "total": len(questions), "skipped": b["skipped"],
           "cards": sum(1 for q in questions if q["mode"] == "card"),
           "marking": {"plus": float(prefs.get("plus") or 1), "neg": float(prefs.get("neg") or 0)},
           "default_time": int(prefs.get("time") or app.settings.get("quiz.default_question_time_s") or 30),
           "created_by": task.user_id, "created_at": store.now_iso(), "fingerprint": fp, "plays": 0}
    saved = await app.repo.quizzes.put(qid, doc)
    await app.repo.exams.update(key, {"$addToSet": {"quiz_ids": qid}})
    await app.repo.users.update(task.user_id, {"$addToSet": {"quiz_db.created": qid}}, upsert=False)
    log.info("quiz %s made: %s (%d q, %d cards, %s)", qid, key, len(questions), doc["cards"], lang)
    return saved


def dump_sig(quiz: dict) -> str:
    """What a quiz dump post stands for — title, language, scope, seconds, explanations and every question in order
    (its reference, right answer, card / poll, options, poll text, explanation card): any change → a new post."""
    parts = [quiz.get("title"), quiz.get("lang"), quiz.get("scope"), quiz.get("default_time"), bool(quiz.get("explain"))]
    for q in quiz.get("questions") or []:
        parts.append([q.get("ref"), q.get("c"), q.get("mode"), q.get("n"), (q.get("poll") or {}).get("q"), bool(q.get("xc"))])
    return hashlib.sha1(orjson.dumps(parts, default=str)).hexdigest()[:20]


async def _post_once(app, quiz: dict, chan: int, post: str, markup) -> None:
    """Owner 2026-10-13 ("quiz dump mai quiz post krne se phle dekh lena ki khi phle se to nhi dali hui udhr yahi quiz
    id, koi quiz modify hui hai to post krdena"): the post is skipped when this quiz — or another quiz with the very
    same content (``dump_sig``) — is already in that channel; a quiz whose content changed since its post is posted
    again. Each post is recorded on its quiz (``dump_post``: chat, message, sig, time)."""
    sig = dump_sig(quiz)
    mine = quiz.get("dump_post") or {}
    if mine.get("chat_id") == chan and mine.get("sig") == sig:
        log.info("quiz %s already in the quiz dump (%s/%s) — not posted again", quiz["_id"], chan, mine.get("message_id"))
        return
    twins = [x for x in await app.repo.quizzes.find({"dump_post.sig": sig, "dump_post.chat_id": chan},
                                                    projection={"_id": 1, "dump_post": 1}) if x["_id"] != quiz["_id"]]
    if twins:
        log.info("quiz %s: the same quiz %s is already in the quiz dump (%s/%s) — not posted again", quiz["_id"],
                 twins[0]["_id"], chan, twins[0]["dump_post"].get("message_id"))
        return
    msg = await app.sender.send_message(chan, post, reply_markup=markup, link_preview_options=NO_PREVIEW)
    rec = {"chat_id": chan, "message_id": getattr(msg, "id", None), "sig": sig, "at": store.now_iso()}
    await app.repo.quizzes.update(quiz["_id"], {"$set": {"dump_post": rec}}, upsert=False)
    quiz["dump_post"] = rec
    log.info("quiz %s posted to the quiz dump (%s/%s)%s", quiz["_id"], chan, rec["message_id"],
             " — changed since its last post" if mine else "")


async def _announce_post(app, quiz: dict) -> None:
    """The quiz dump post (▶️ Play / 👥 Play in a group, 📱 the Mini App when it is set up) — once per channel
    (``_post_once``); never raises."""
    from bot.quiz import ui
    me = app.me.username
    lang = LANG_NAMES.get(quiz["lang"], quiz["lang"])
    meta = html.escape(quiz.get("meta_line") or "")
    scope = html.escape(quiz.get("scope") or "") if quiz.get("section") is not None else ""
    post = (f"🎯 <b>{html.escape(quiz['title'])}</b>\n" + (f"<i>{meta}</i>\n" if meta else "")
            + (f"📚 {scope}\n" if scope else "")
            + f"\n🧩 <b>{quiz['total']}</b> questions · 🌐 {lang} · ⏱ {quiz['default_time']} s each"
            + (" · 💡 with explanations" if quiz.get("explain") else "")
            + f"\n\n▶️ Tap <b>Play</b> to attempt it\n🔗 {ui.share_link(app, quiz['_id'])}"
            + (f"\n\n{html.escape(app.config.branding)}" if app.config.branding else ""))
    rows = [[btn("▶️ Play", url=f"https://t.me/{me}?start={quiz['_id']}", style="success"),
             btn("👥 Play in a group", url=f"https://t.me/{me}?startgroup={quiz['_id']}")]]
    mini = ui.miniapp_link(app, quiz["_id"])
    if mini:
        rows.append([btn("📱 Play in the Web App", url=mini, style="primary")])
    chan = app.config.quiz_dump_channel_id or app.config.dump_channel_id
    try:
        await _post_once(app, quiz, chan, post, kb(*rows))
    except Exception as e:
        log.warning("quiz dump post failed: %r", e)
        app.dump.notify(f"⚠️ Quiz dump post failed for <code>{quiz['_id']}</code>: {html.escape(repr(e))[:300]}")


async def _announce(app, task, quiz: dict) -> None:
    """Owner 2026-10-13 ("quiz dump mai keval quiz ki ek post bna kr bhjni hai, jisse attempt ho, na ki json, json to
    main dump mai hi dalna hai"): the quiz dump channel gets one post to play it (``_announce_post``); the quiz JSON
    (with who made it) goes to the main dump — the file ``/load_quiz`` takes back. Posted by the main bot (the
    uploader team when it cannot); never fails the task."""
    from bot.quiz import ui
    await _announce_post(app, quiz)
    n_card = quiz.get("cards", 0)
    lang = LANG_NAMES.get(quiz["lang"], quiz["lang"])
    meta = html.escape(quiz.get("meta_line") or "")
    try:
        name = task.data.get("user_name") or str(task.user_id)
        text = (f"🎯 <b>New quiz</b> · <code>{quiz['_id']}</code>\n<b>{html.escape(quiz['title'])}</b>\n"
                + (f"<i>{meta}</i>\n" if meta else "")
                + f"📚 {html.escape(quiz.get('scope') or '')} · 🌐 {lang}\n"
                f"🧩 {quiz['total']} questions ({n_card} cards, {quiz['total'] - n_card} text polls)"
                + (f" · {quiz['skipped']} skipped" if quiz.get("skipped") else "")
                + f" · ⏱ {quiz['default_time']} s\n"
                f"👤 <a href=\"tg://user?id={task.user_id}\">{html.escape(name)}</a> · <code>{task.user_id}</code>\n"
                f"🔗 {ui.share_link(app, quiz['_id'])}")
        out = task.workdir / "out" / f"{quiz['_id']}.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(orjson.dumps(quiz, option=orjson.OPT_INDENT_2))
        try:
            await app.sender.send_document(app.config.dump_channel_id, str(out), caption=text[:1024], file_name=out.name)
        except Exception as e:
            log.info("quiz JSON post by the main bot failed (%r) — uploader team", e)
            await app.uploads.send_document(app.config.dump_channel_id, str(out), text[:1024])
    except Exception as e:
        log.warning("quiz JSON post to the dump failed: %r", e)


# ------------------------------------------------------------------ HTML file
def _file_name(quiz: dict) -> str:
    name = re.sub(r'[\\/:*?"<>|\r\n\t]+', " ", quiz.get("title") or "Quiz")
    return re.sub(r"\s+", " ", name).strip()[:120] + " - Quiz.html"


async def run_html(app, task) -> None:
    from bot.core.tgref import download
    from bot.quiz.html import write_offline

    quiz = await store.get(app, task.data["quiz_id"])
    if quiz is None:
        return await app.sender.send_message(task.chat_id, "This quiz was deleted.")
    look = html_look()
    caption = (f"🌐 <b>{html.escape(quiz['title'])}</b>\n<i>{html.escape(quiz.get('meta_line') or '')}</i>\n"
               f"🧩 {quiz['total']} questions · open in any browser — works offline\n{html.escape(app.config.branding)}")
    cached = (quiz.get("html") or {}).get(look)
    if cached:
        try:
            await app.sender.copy(task.chat_id, cached["chat_id"], cached["message_id"], caption=caption)
            return
        except Exception as e:
            log.info("cached HTML of %s unusable (%r) — building", quiz["_id"], e)
    msg = await app.sender.send_message(task.chat_id, "🌐 <b>Making your HTML quiz…</b>")
    if not quiz.get("json_ref"):
        return await app.sender.edit_text(task.chat_id, msg.id, "😕 This quiz has no exam data to build the HTML from.")
    bundle = await download(app, dict(quiz["json_ref"]), task.workdir / "bank")
    out = task.workdir / "out" / _file_name(quiz)
    out.parent.mkdir(parents=True, exist_ok=True)
    from bot.render.theme import brand_url
    await run_cpu(app, "render", task.task_id, write_offline, str(bundle), quiz, str(out), app.config.branding,
                  brand_url(app.settings, app.config.branding),
                  est_mb=120)
    who = f"👤 <code>{task.user_id}</code> · quiz <code>{quiz['_id']}</code>"
    rec = await deliver(app, task, out, caption=f"{caption}\n{who}"[:1024], user_caption=caption)
    task.outputs.append(rec)
    if rec.get("ref"):
        await app.repo.quizzes.update(quiz["_id"], {"$set": {f"html.{look}": rec["ref"]}}, upsert=False)
    try:
        await app.sender.delete(task.chat_id, msg.id)
    except Exception:
        pass
    log.info("quiz %s: HTML built (%s)", quiz["_id"], time.strftime("%H:%M:%S"))
