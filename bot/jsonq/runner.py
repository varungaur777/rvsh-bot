"""``/json`` task runner (heavy lane): a question JSON file → PDF(s) and / or quiz(zes) (spec §16.8).

Steps
-----
1. **Bank** — the file's fingerprint (its questions' content) names an ``exams`` record ``json:<fp16>`` (``source:
   "json"``): when it exists, its bundle is reused (no download, no image work) — unless it was built by an older
   ``parse.PARSE_V`` and the file has tables (``info["tables"]``): then it is built again (owner 2026-10-15: tables
   came out one cell per line). The record keeps the file's cleaned exam name (``exam.exam_label``). Else every
   image link is downloaded (``jsonq.fetch``, progress ``🖼 Images 34/120``), the bundle is built in the pool
   (``jsonq.parse.build_bundle``) and posted to the dump channel; the record keeps ``json_ref``, PDF copies
   (``outputs``) and quiz cards (``quiz_cards``).
2. **PDFs** — one per language (owner 2026-10-13: "hindi english pdf alag alag hi banani hai"), the custom PDF
   (``render.exam_pdf``) with the user's answer placement (under each question / key at the end / none) and
   explanation placement (under each question / at the end / none); cached per choice + ``/ps`` look.
3. **Quizzes** — one per language ("aur quiz bhi"); questions **shuffled once** when asked, numbered 1…n in the
   quiz's order (owner: "number serial wise hi dalna"); explanations optional (short → poll explanation, long → a
   card after the answer); marks +1 / no minus and the user's seconds — the file's time / marks are not used.
   Same file + language + choices → the existing quiz.

A file whose English and Hindi parts are the same text (both languages in one) gives **one** PDF / quiz labelled
"English + Hindi" — two copies would be identical.

Public API: ``run_json(app, task)`` · ``run_json_batch(app, task)`` (every file of a zip, one by one) · ``langs_out(summary, want) -> [(lang, label)]`` · ``file_name(title, label, ext)``.
Depends on: ``jsonq.parse``, ``jsonq.fetch``, ``render.exam_pdf``, ``quiz.build`` / ``quiz.maker`` / ``quiz.store``,
``core.delivery``. Used by: ``handlers.jsoncmd``.
"""

from __future__ import annotations

import asyncio
import hashlib
import html
import logging
import random
import re
import time

import orjson

from bot.compute import run_cpu
from bot.core.delivery import deliver
from bot.core.errors import TaskCancelled
from bot.core.sender import NO_PREVIEW
from bot.jsonq import fetch, parse
from bot.quiz import build, store
from bot.ui.progress import ProgressMessage, human_bytes

log = logging.getLogger(__name__)
NAMES = {"en": "English", "hi": "Hindi"}
JSON_LOOK = 2  # bump when JSON PDFs / quizzes are built differently: old cached copies are not reused (2: tables,
# explanation box, exam name — 2026-10-15)
XCARD_LOOK = 3  # explanation cards: 2 = always one card (owner 2026-10-13); 3 = box, tables, no overlaps


def langs_out(summary: dict, want: str) -> list[tuple[str, str]]:
    """The (language, label) of each PDF / quiz to make."""
    have = [x for x in ("en", "hi") if (summary.get("languages") or {}).get(x)]
    if not have:
        return []
    if summary.get("same") and len(have) > 1:
        return [(have[0], "English + Hindi")]
    picked = have if want == "both" else [want] if want in have else have[:1]
    return [(x, NAMES[x]) for x in picked]


async def run_json(app, task) -> None:
    progress = ProgressMessage(app.sender, task.chat_id, interval_s=app.settings.get("edit_interval_s"), task=task)
    progress.attach(task.data["ui_message_id"])
    try:
        await _run(app, task, progress)
    finally:
        progress.close()


async def _one(app, task, progress, ctl=None) -> tuple[int, list]:
    """One JSON file (``task.data["json"]`` / ``["json_path"]``) → (PDFs sent, [(quiz, made before)]). ``ctl``: a
    batch's pause / cancel gate, passed between the steps."""
    async def gate():
        if ctl is not None:
            await ctl.gate(progress)
        else:
            task.check()

    prefs, info = task.data["prefs"], task.data["json"]
    bank = await _bank(app, task, progress)
    jobs = langs_out(info, prefs.get("lang") or "both")
    make = prefs.get("make") or "pdf"
    pdfs = 0
    if make in ("pdf", "both"):
        for n, (lang, label) in enumerate(jobs, 1):
            await gate()
            pdfs += await _pdf(app, task, progress, bank, lang, label, f" ({n}/{len(jobs)})" if len(jobs) > 1 else "")
    made = []
    if make in ("quiz", "both"):
        for n, (lang, label) in enumerate(jobs, 1):
            await gate()
            q = await _quiz(app, task, progress, bank, lang, label, f" ({n}/{len(jobs)})" if len(jobs) > 1 else "")
            if q is not None:
                made.append(q)
    return pdfs, made


async def _run(app, task, progress: ProgressMessage) -> None:
    make = task.data["prefs"].get("make") or "pdf"
    pdfs, made = await _one(app, task, progress)
    quizzes = len(made)
    head = "✅ <b>Done!</b>" + (f" 📄 {pdfs} PDF{'s' if pdfs != 1 else ''} above." if pdfs else "")
    if make in ("quiz", "both") and not made:
        head += "\n😕 No quiz could be made — no question has its answer and options."
    if not made:
        return await progress.finish(head + "\n<i>Send another JSON file any time.</i>")
    from bot.quiz import ui
    first, old = made[0]
    stats = await app.repo.quiz_stats.get(first["_id"], copy=False)
    head += f"\n🎯 {quizzes} quiz{'zes' if quizzes != 1 else ''} ready" + (" (made before — here it is)" if old else "") + "."
    await progress.finish(head + "\n\n" + ui.page_text(app, first, stats, task.user_id),
                          reply_markup=ui.page_markup(app, task.user_id, first, back=None))
    for quiz, _old in made[1:]:
        stats = await app.repo.quiz_stats.get(quiz["_id"], copy=False)
        await app.sender.send_message(task.chat_id, ui.page_text(app, quiz, stats, task.user_id),
                                      reply_markup=ui.page_markup(app, task.user_id, quiz, back=None))


# ------------------------------------------------------------------ batch (/json_batch)
PAUSE_MAX_S = 2 * 3600  # a batch paused this long is cancelled (it holds a heavy-lane place)
_CTL: dict[str, "BatchCtl"] = {}


class BatchCtl:
    """⏸ Pause / ▶️ Resume / ✖️ Cancel of a running batch (owner 2026-10-13: "batch command ko pause and cancel bhi
    kia ja ske bich mai hi"). Pause takes effect at the next step (between PDFs / quizzes / files): the step running
    now finishes first."""

    def __init__(self, app, task, progress: ProgressMessage) -> None:
        self.app, self.task, self.progress = app, task, progress
        self.running = asyncio.Event()
        self.running.set()
        self.where = ""  # "after file 3/12" — for the paused text

    def markup(self):
        from bot.ui.keyboards import btn, cb, kb
        t = self.task
        if self.running.is_set():
            first = btn("⏸ Pause", cb("jsb", t.user_id, t.task_id, "pause"))
        else:
            first = btn("▶️ Resume", cb("jsb", t.user_id, t.task_id, "resume"), style="success")
        return kb([first, btn("✖️ Cancel", cb("jsb", t.user_id, t.task_id, "cancel"), style="danger")])

    def pause(self) -> bool:
        if not self.running.is_set():
            return False
        self.running.clear()
        self.progress.set_markup(self.markup())
        return True

    def resume(self) -> bool:
        if self.running.is_set():
            return False
        self.running.set()
        self.progress.set_markup(self.markup())
        return True

    async def gate(self, step: "_Step | None" = None) -> None:
        """A cancel point; while paused, wait here (≤ ``PAUSE_MAX_S``, then the batch is cancelled)."""
        self.task.check()
        if self.running.is_set():
            return
        text = (f"⏸ <b>Paused</b> {self.where}\n<i>Tap ▶️ Resume to go on, or ✖️ Cancel. Stops by itself after "
                f"{PAUSE_MAX_S // 3600} h.</i>")
        self.progress.set(text)
        try:
            await asyncio.wait_for(self.running.wait(), PAUSE_MAX_S)
        except asyncio.TimeoutError:
            self.task.data["batch_stop"] = "paused too long"
            raise TaskCancelled() from None
        self.task.check()
        if step is not None:
            step.set("▶️ Going on…")


def batch_ctl(task_id: str) -> BatchCtl | None:
    return _CTL.get(task_id)


class _Step:
    """The progress message with ``📦 File 3/12 · title`` above each step of that file."""

    def __init__(self, progress: ProgressMessage, head: str) -> None:
        self.progress, self.head = progress, head

    def set(self, text: str) -> None:
        self.progress.set(self.head + text)


def file_range(prefs: dict, n: int) -> tuple[int, int]:
    """The 1-based (first, last) files to make (owner 2026-10-13: "kha se leke kha tak banani hai ye bhi puchna")."""
    a, b = (prefs.get("range") or [1, n])[:2]
    a, b = max(1, min(int(a), n)), max(1, min(int(b), n))
    return (a, b) if a <= b else (b, a)


async def run_json_batch(app, task) -> None:
    """``/json_batch`` (owner 2026-10-13: "yahi kam bulk mai krwana hai … zip file … sare jsons ko bari bari se
    process karna hai … sabhi json ke liye prefrence ek bare mai"): the chosen files (``prefs["range"]``) of
    ``task.data["batch"]["files"]``, one by one, with the one set of choices; a file that fails is reported and the
    next one goes on; ⏸ / ▶️ / ✖️ on the progress message. Ends with one summary — also after a cancel: per file its
    PDFs and its quizzes as links."""
    progress = ProgressMessage(app.sender, task.chat_id, interval_s=app.settings.get("edit_interval_s"), task=task)
    progress.attach(task.data["ui_message_id"])
    ctl = _CTL[task.task_id] = BatchCtl(app, task, progress)
    progress.set_markup(ctl.markup())
    try:
        await _run_batch(app, task, progress, ctl)
    finally:
        _CTL.pop(task.task_id, None)
        progress.close()


async def _run_batch(app, task, progress: ProgressMessage, ctl: BatchCtl) -> None:
    files = task.data["batch"]["files"]
    total = len(files)
    first, last = file_range(task.data["prefs"], total)
    picked = list(enumerate(files, 1))[first - 1:last]
    make = task.data["prefs"].get("make") or "pdf"
    me = app.me.username
    rows, n_pdf, n_quiz, failed, done = [], 0, 0, 0, 0
    stopped = None
    try:
        for k, (n, f) in enumerate(picked, 1):
            ctl.where = f"before file {n}/{total}" if k > 1 else f"at file {n}/{total}"
            await ctl.gate()
            info = f["info"]
            task.data["json"], task.data["json_path"] = info, f["path"]
            run = f" · {k} of {len(picked)} in this run" if len(picked) != total else ""
            step = _Step(progress, f"📦 <b>File {n}/{total}</b>{run} · {html.escape(info['title'][:80])}\n")
            step.set("⏳ Starting…")
            ctl.where = f"in file {n}/{total} ({html.escape(info['title'][:60])})"
            try:
                pdfs, made = await _one(app, task, step, ctl)
            except (TaskCancelled, asyncio.CancelledError):
                raise
            except Exception as e:
                failed += 1
                done += 1
                err = app.dump.report_error(e, f"json batch {task.task_id} file {f['name']}")
                rows.append(f"<b>{n}.</b> {html.escape(info['title'])}\n⚠️ <i>Failed</i> (<code>{err}</code>) — the next "
                            "file went on.")
                continue
            done += 1
            n_pdf += pdfs
            n_quiz += len(made)
            bits = []
            if pdfs:
                bits.append(f"📄 {pdfs} PDF{'s' if pdfs != 1 else ''}")
            for quiz, old in made:
                label = "Quiz" if len(made) == 1 else f"{NAMES.get(quiz.get('lang'), quiz.get('lang') or '')} quiz"
                bits.append(f"🎯 <a href=\"https://t.me/{me}?start={quiz['_id']}\">{label}</a>" + (" ♻️" if old else ""))
            if make in ("quiz", "both") and not made:
                bits.append("😕 no quiz (no question with answer and options)")
            rows.append(f"<b>{n}.</b> {html.escape(info['title'])}\n" + (" · ".join(bits) or "—"))
    except (TaskCancelled, asyncio.CancelledError):
        stopped = task.data.get("batch_stop") or "cancelled"
        await _summary(app, task, progress, rows, picked, first, last, total, done, n_pdf, n_quiz, failed, stopped)
        raise
    await _summary(app, task, progress, rows, picked, first, last, total, done, n_pdf, n_quiz, failed, None)


async def _summary(app, task, progress, rows, picked, first, last, total, done, n_pdf, n_quiz, failed, stopped) -> None:
    span = f" · files {first}–{last} of {total}" if len(picked) != total else ""
    if stopped:
        title = f"✖️ <b>Batch {html.escape(stopped)}</b> · {done} of {len(picked)} file{'s' if len(picked) != 1 else ''} done"
    else:
        title = f"✅ <b>Batch done</b> · {len(picked)} file{'s' if len(picked) != 1 else ''}"
    head = (title + span + (f" · ⚠️ {failed} failed" if failed else "") + "\n"
            + " · ".join(x for x in ((f"📄 {n_pdf} PDF{'s' if n_pdf != 1 else ''} above" if n_pdf else ""),
                                     (f"🎯 {n_quiz} quiz{'zes' if n_quiz != 1 else ''}" if n_quiz else "")) if x)
            + "\n━━━━━━━━━━━━━━━━\n")
    foot = "\n━━━━━━━━━━━━━━━━\n<i>Tap a quiz to play it · ♻️ = made before from the same file.</i>"
    if not rows:
        foot = "<i>No file was finished.</i>"
    chunks, cur = [], head
    for r in rows:
        if len(cur) + len(r) + 2 > 3800:
            chunks.append(cur)
            cur = ""
        cur += r + "\n\n"
    chunks.append(cur.rstrip() + "\n" + foot if rows else cur + foot)
    try:
        progress.set_markup(None)
        await progress.finish(chunks[0], link_preview_options=NO_PREVIEW, reply_markup=None)
        for c in chunks[1:]:
            await app.sender.send_message(task.chat_id, c, link_preview_options=NO_PREVIEW)
    except Exception as e:  # the summary must never hide why the batch ended
        log.info("batch summary not sent: %r", e)


# ------------------------------------------------------------------ bank
async def _bank(app, task, progress: ProgressMessage) -> dict:
    from bot.core.tgref import from_message, private_link
    from bot.exam.registry import _KeyLock

    info = task.data["json"]
    key = f"json:{info['fingerprint'][:16]}"
    async with _KeyLock(key):
        rec = await app.repo.exams.get(key) or {}
        stale = (rec.get("source") == "json" and not info.get("bundle") and info.get("tables")
                 and int(rec.get("parse_v") or 1) < parse.PARSE_V)
        if rec.get("json_ref") and not stale:
            label = info.get("exam_label") or ""
            if "exam_label" in info and (rec.get("exam") or {}).get("exam_label", None) != label:
                await app.repo.exams.update(key, {"$set": {"exam.exam_label": label}})
                rec = await app.repo.exams.get(key) or rec
            await app.repo.exams.update(key, {"$inc": {"reuses": 1}})
            task.data["anchor_id"] = rec["json_ref"].get("message_id")
            return {"key": key, "rec": rec, "path": None}
        if info.get("bundle"):  # an exam bundle (/load_quiz, /json): already what the dump keeps
            return await _bundle_bank(app, task, progress, key, info)
        if stale:
            log.info("json bank %s: built by parse v%s, the file has tables — building it again", key,
                     rec.get("parse_v") or 1)
        src = task.data["json_path"]
        urls = info.get("urls") or []
        files: dict = {}
        if urls:
            progress.set(f"🖼 <b>Downloading images</b> · 0/{len(urls)}…")
            files = await fetch.download_images(
                urls, task.workdir / "jimg", proxy=app.settings.get("http.proxy") or None,
                progress=lambda d, n: progress.set(f"🖼 <b>Downloading images</b> · {d}/{n}…"))
        task.check()
        fmap = task.workdir / "jimg.json"
        fmap.write_bytes(orjson.dumps(files))
        progress.set("📚 <b>Preparing the questions</b> (images, text)…")
        out = task.workdir / "bank" / f"json_{info['fingerprint'][:12]}.zip"
        out.parent.mkdir(parents=True, exist_ok=True)
        b = await run_cpu(app, "render", task.task_id, parse.build_bundle, str(src), str(fmap), str(out), est_mb=120)
        missing = len(urls) - len(files)
        caption = (f"📦 <b>JSON data</b>\n<b>{html.escape(info['title'])}</b>\n"
                   + (f"<i>{html.escape(info['exam_label'])}</i>\n" if info.get("exam_label") else "")
                   + f"🧩 {b['questions']} questions · 🖼 {b['images']} images" + (f" ({missing} links failed)" if missing else "")
                   + f" · 🌐 {', '.join(b['languages'])} · {human_bytes(b['bytes'])}\n"
                   f"👤 <code>{task.user_id}</code> · 🔑 <code>{html.escape(key)}</code>")
        _bot, msg = await app.uploads.send_document(app.config.dump_channel_id, str(out), caption)
        ref = from_message(msg)
        task.data["anchor_id"] = msg.id
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        rec = {"source": "json", "exam": {"exam_name": info["title"], "exam_label": info.get("exam_label") or "",
                                          "source": "json", "exam_key": key}, "parse_v": parse.PARSE_V,
               "sections": [{"name": n, "count": c} for n, c in b["sections"]], "languages": b["languages"],
               "fingerprint": info["fingerprint"], "questions": b["questions"], "json_ref": ref, "version": 1,
               "images": b["images"], "images_missing": missing, "bundle_bytes": b["bytes"], "same_langs": bool(info.get("same")),
               "created_at": now, "created_by": task.user_id}
        await app.repo.exams.update(key, {"$set": rec})
        log.info("json bank: %s registered (%d q, %d images, %d missing, %s)", key, b["questions"], b["images"], missing,
                 human_bytes(b["bytes"]))
        if missing:
            app.dump.notify(f"⚠️ JSON <code>{html.escape(key)}</code>: {missing} of {len(urls)} image links could not be "
                            f"downloaded · 📦 {private_link(msg.chat.id, msg.id)}")
        return {"key": key, "rec": await app.repo.exams.get(key) or rec, "path": str(out)}


async def _bundle_bank(app, task, progress, key: str, info: dict) -> dict:
    """An exam bundle sent to ``/load_quiz`` / ``/json`` → posted to the dump as it is and registered like a JSON
    file's bank (``json:<fp16>``, ``source: "bundle"``). Called under the key's lock."""
    from pathlib import Path

    from bot.core.tgref import from_message

    path = Path(info["bundle"])
    progress.set("📤 <b>Saving the question data…</b>")
    caption = (f"📦 <b>Exam data</b> (loaded)\n<b>{html.escape(info['title'])}</b>\n"
               f"🧩 {info['questions']} questions · 🖼 {info['images']} images"
               + (f" ({info['images_missing']} missing)" if info.get("images_missing") else "")
               + f" · 🌐 {', '.join(info['languages'])} · {human_bytes(path.stat().st_size)}\n"
               f"👤 <code>{task.user_id}</code> · 🔑 <code>{html.escape(key)}</code>")
    _bot, msg = await app.uploads.send_document(app.config.dump_channel_id, str(path), caption)
    task.data["anchor_id"] = msg.id
    rec = {"source": "bundle", "exam": {"exam_name": info["title"], "source": "bundle", "exam_key": key},
           "sections": [{"name": n, "count": c} for n, c in info.get("sections") or []],
           "languages": sorted(info["languages"]), "fingerprint": info["fingerprint"], "questions": info["questions"],
           "json_ref": from_message(msg), "version": 1, "images": info["images"],
           "images_missing": info.get("images_missing", 0), "bundle_bytes": path.stat().st_size, "same_langs": False,
           "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "created_by": task.user_id}
    await app.repo.exams.update(key, {"$set": rec})
    log.info("exam bundle %s registered (%d q, %d images)", key, info["questions"], info["images"])
    return {"key": key, "rec": await app.repo.exams.get(key) or rec, "path": str(path)}


async def _path(app, task, progress, bank: dict) -> str:
    if bank["path"] is None:
        from bot.core.tgref import download
        progress.set("📥 <b>Loading the questions…</b>")
        bank["path"] = str(await download(app, dict(bank["rec"]["json_ref"]), task.workdir / "bank"))
    return bank["path"]


def file_name(title: str, label: str, ext: str) -> str:
    name = re.sub(r'[\\/:*?"<>|\r\n\t]+', " ", title)
    return re.sub(r"\s+", " ", name).strip()[:120] + f" - {label}.{ext}"


# ------------------------------------------------------------------ PDF
async def _pdf(app, task, progress, bank: dict, lang: str, label: str, count: str) -> int:
    from bot.exam.pipeline import PDF_LOOK, pdf_look
    from bot.render.exam_pdf import render_exam_pdf
    from bot.render.theme import app_theme

    prefs, info = task.data["prefs"], task.data["json"]
    answers, explain = prefs.get("answers") or "after", prefs.get("explain") or "none"
    if answers == "key":  # only the answer key grid: no answer lines, no explanations
        explain = "none"
    cols = 1 if str(app.settings.get("pdf.columns")) == "1" else 2
    font = "noto" if app.settings.get("pdf.font") == "noto" else "poppins"
    cache_key = f"json{JSON_LOOK}_{lang}_{answers}_{explain}_c{cols}_{font}_{pdf_look(app)}_l{PDF_LOOK}"
    title = info["title"]
    exam = info.get("exam_label") or ""
    caption = (f"📄 <b>{html.escape(title)}</b>\n" + (f"<i>{html.escape(exam)}</i>\n" if exam else "")
               + f"{label} · {_answers_text(answers)}"
               + (f" · 💡 explanations {_explain_text(explain)}" if explain != "none" else "")
               + f"\n{html.escape(app.config.branding)}")
    cached = (bank["rec"].get("outputs") or {}).get(cache_key)
    if cached:
        try:
            await app.sender.copy(task.chat_id, cached["chat_id"], cached["message_id"], caption=caption)
            task.outputs.append({"name": file_name(title, label, "pdf"), "size_h": "cached", "link": None})
            return 1
        except Exception as e:
            log.info("cached JSON PDF %s unusable (%r) — rendering", cache_key, e)
    path = await _path(app, task, progress, bank)
    progress.set(f"📄 <b>Building the {label} PDF</b>{count}…")
    out = task.workdir / "out" / file_name(title, label, "pdf")
    out.parent.mkdir(parents=True, exist_ok=True)
    ctx = {"bundle": path, "lang": lang, "columns": cols, "font": font, "answers": answers, "explain": explain,
           "title": title, "meta_line": exam, "lang_label": label, "theme": app_theme(app),
           "sections": None, "colors": app.settings.get("pdf.colors"), "workdir": str(task.workdir / "img")}
    res = await run_cpu(app, "render", task.task_id, render_exam_pdf, ctx, str(out), est_mb=150)
    progress.set(f"📤 <b>Sending the {label} PDF</b> · {res['pages']} pages{count}…")
    who = f"👤 <code>{task.user_id}</code> · task <code>{task.task_id}</code>"
    rec_out = await deliver(app, task, out, caption=f"{caption}\n{who}"[:1024], user_caption=caption)
    task.outputs.append(rec_out)
    if rec_out.get("ref"):
        await app.repo.exams.update(bank["key"], {"$set": {f"outputs.{cache_key}": rec_out["ref"]}})
        bank["rec"].setdefault("outputs", {})[cache_key] = rec_out["ref"]
    return 1


def _answers_text(a: str) -> str:
    return {"after": "answer under each question", "grid": "answer key at the end", "key": "only the answer key grid (no explanations)",
            "none": "no answers"}.get(a, a)


def _explain_text(e: str) -> str:
    return {"after": "under each question", "end": "at the end"}.get(e, e)


# ------------------------------------------------------------------ quiz
async def _quiz(app, task, progress, bank: dict, lang: str, label: str, count: str):
    from bot.quiz.maker import CARD_LOOK, _announce, cards_for

    prefs, info = task.data["prefs"], task.data["json"]
    shuffle, explain = bool(prefs.get("shuffle")), bool(prefs.get("qexplain", True)) and bool(info.get("explanations"))
    fp = hashlib.sha1(f"{info['fingerprint']}|{lang}|s{int(shuffle)}|e{int(explain)}|j{JSON_LOOK}|x{XCARD_LOOK}".encode()).hexdigest()
    old = await store.find_by_fp(app, fp)
    if old:
        return old, True
    path = await _path(app, task, progress, bank)
    progress.set(f"🧩 <b>Building the {label} quiz</b>{count}…")
    b = await run_cpu(app, "parse", task.task_id, build.build_questions, path, lang, None, explain)
    questions = list(b["questions"])
    if not questions:
        return None
    if shuffle:  # once, now: every player gets this order (owner: "question shuffle krne hai ya nhi")
        random.shuffle(questions)
    for i, q in enumerate(questions, 1):
        q["no"] = i
    # card numbers follow the quiz's order: an unshuffled quiz shares its cards, a shuffled one has its own
    suffix = f"_{fp[:8]}" if shuffle else ""
    sec = ""  # one section ("Questions"): not repeated on every card
    qitems = [{"qref": q["ref"], "number": q["no"], "section": sec} for q in questions if q["mode"] == "card"]
    xitems = [{"qref": q["ref"], "number": q["no"], "section": sec, "kind": "x"} for q in questions if q.get("xcard")]
    rec = bank["rec"]
    what = f" · {label}"
    refs = await cards_for(app, task, progress, bank["key"], rec, path, lang, qitems, what,
                           slot=f"j{CARD_LOOK}_{lang}{suffix}") if qitems else {}
    xrefs = await cards_for(app, task, progress, bank["key"], rec, path, lang, xitems, what,
                            slot=f"x{CARD_LOOK}{XCARD_LOOK}_{lang}{suffix}", label="Explanation cards") if xitems else {}
    kept, skipped = [], b["skipped"]
    for q in questions:
        if q["mode"] == "card":
            q["cards"] = refs.get(q["ref"]) or []
            if not q["cards"]:
                if not q.get("text"):
                    skipped += 1
                    continue
                q["mode"], q["poll"] = "poll", {"q": q["text"][:build.POLL_Q], "o": list("ABCDEFGHIJ"[:q["n"]])}
        if q.pop("xcard", False) and xrefs.get(q["ref"]):
            q["xc"] = xrefs[q["ref"]]
        kept.append(q)
    for i, q in enumerate(kept, 1):  # a question dropped above → the numbers stay 1…n
        q["no"] = i
    qid = store.new_id()
    while await app.repo.quizzes.get(qid, copy=False):
        qid = store.new_id()
    title = info["title"] + (f" · {label}" if label != "English + Hindi" and len(info.get("languages") or {}) > 1 else "")
    doc = {"title": title, "exam_key": bank["key"], "json_ref": rec.get("json_ref"), "version": 1,
           "meta": {"exam_name": info["title"], "source": "json"}, "meta_line": info.get("exam_label") or "",
           "scope": "Full", "section": None,
           "lang": lang, "languages": [lang], "sections": b["sections"], "questions": kept, "total": len(kept),
           "skipped": skipped, "cards": sum(1 for q in kept if q["mode"] == "card"),
           "explain": explain, "shuffled": shuffle, "source": "json",
           "marking": {"plus": 1.0, "neg": 0.0},
           "default_time": int(prefs.get("time") or app.settings.get("quiz.default_question_time_s") or 30),
           "created_by": task.user_id, "created_at": store.now_iso(), "fingerprint": fp, "plays": 0}
    saved = await app.repo.quizzes.put(qid, doc)
    await app.repo.exams.update(bank["key"], {"$addToSet": {"quiz_ids": qid}})
    await app.repo.users.update(task.user_id, {"$addToSet": {"quiz_db.created": qid}}, upsert=False)
    log.info("json quiz %s made: %s %s (%d q, %d cards, %d explanation cards, shuffle %s)", qid, bank["key"], lang,
             len(kept), doc["cards"], sum(1 for q in kept if q.get("xc")), shuffle)
    await _announce(app, task, saved)
    return saved, False
