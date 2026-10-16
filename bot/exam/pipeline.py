"""Exam pipeline steps (spec §13–§14). P2b: inspect inputs + Marks (scorecard). Original/custom PDF and quiz
outputs plug in here in P4/P5.

Depends on: ``core.tgref``, ``parsers``, ``exam.marks``, ``exam.keys``, ``exam.results``, ``render.cards``,
``ui.progress``. Used by: ``handlers/exam.py``.

Public API
----------
- ``await inspect(app, task) -> dict`` — parses the inputs already stored in the task workdir (``exam.inputs``),
  registers exam URLs, returns a small summary (kept in the session; parsed data is not kept in RAM).
- ``await run_marks(app, task)`` — task runner: re-parse from the workdir, score with the confirmed
  marking, render the scorecard, deliver it (uploader bot → dump, reply to the anchor → copied to the user,
  ``core.delivery``), save the result.
- ``await run_original(app, task)`` — Marks + Original PDF (scorecard, then ``render.original_pdf``, spec §14.2).
- ``await run_custom(app, task)`` — Marks + Custom PDF (spec §14.3).
- ``scorecard_caption(meta, score, branding)`` — the caption (section-wise detail in an expandable quote).
- ``load_exam(task)`` — parse the files already in the workdir (shared by every output runner).

CPU steps (parse, score, render) run in the process pool through ``compute`` (fair ``parse``/``card`` slots;
pure functions in ``exam.jobs``: paths in, small data out).

Exceptions → outcome: ``ParseError`` / ``SourceUnavailable`` propagate to the lane/handler → friendly text,
task closed (workdir deleted).
"""

from __future__ import annotations

import html
import logging
from pathlib import Path

from bot.compute import run_cpu
from bot.core.delivery import deliver
from bot.exam.keys import exam_key, meta_line
from bot.exam.marks import mask_name, mask_roll, negative_from_fraction, short_section
from bot.exam.jobs import input_files, inspect_sheets, score_sheets
from bot.exam.results import save_result
from bot.exam.schema import main_languages
from bot.exam.urls import register as register_url
from bot.net.fetcher import detect_kind
from bot.parsers import parse_sources, source
from bot.parsers.models import ExamData, Marking
from bot.render.cards import render_scorecard
from bot.ui.progress import ProgressMessage

log = logging.getLogger(__name__)


def _input_dir(task) -> Path:
    return task.workdir / "in"


def load_exam(task) -> ExamData:
    """Parse every input file in the task workdir, in arrival order (sync; full data — tools/tests)."""
    files = input_files(_input_dir(task))
    hints = task.data.get("hints") or {}
    return parse_sources([source.load_path(p, source.Hints(**hints)) for p in files])


async def inspect(app, task) -> dict:
    """Parse the stored inputs once (process pool, ``parse`` slot) to show the user what was found (and detect
    the marking); register the exam URLs (links sent + page URL inside each MHT) in ``exam_urls``."""
    m, summary = await run_cpu(app, "parse", task.task_id, inspect_sheets, str(_input_dir(task)), task.data.get("hints") or {})
    urls = {it["url"] for it in task.data.get("inputs", []) if it.get("url") and it["status"] == "stored"}
    if m.url and detect_kind(m.url):
        urls.add(m.url)
    for u in urls:
        try:
            await register_url(app.repo, u, m, task.user_id)
        except Exception as e:  # registry problems never block the user
            log.warning("url registry failed: %r", e)
    return summary


def summary_text(s: dict, sections: bool = True) -> str:
    lines = [f"📄 <b>{html.escape(s['exam_name'] or 'Exam')}</b>"]
    if s.get("meta_line"):
        lines.append(f"<i>{html.escape(s['meta_line'])}</i>")
    facts = [f"🧩 <b>{s['questions']}</b> questions", f"📚 <b>{len(s['sections'])}</b> section{'s' if len(s['sections']) != 1 else ''}"]
    if s.get("languages"):
        facts.append("🌐 " + " + ".join({"en": "English", "hi": "Hindi"}.get(x, x) for x in s["languages"]))
    lines += ["", "  ·  ".join(facts)]
    if len(s["sections"]) > 1 and sections:
        rows = "\n".join(f"{i}. {html.escape(short_section(n))}  <i>· {c} Qs</i>" for i, (n, c) in enumerate(s["sections"], 1))
        lines.append(f"<blockquote expandable>{rows}</blockquote>")
    return "\n".join(lines)


def _marking(prefs: dict) -> Marking:
    plus = float(prefs.get("plus", 1))
    minus = negative_from_fraction(plus, float(prefs.get("neg", 0)))
    src = prefs.get("source", "user")
    return Marking(plus=plus, minus=minus, source=src)


def _per_section(prefs: dict, sections: list | None) -> dict[str, tuple[float, float]] | None:
    """Admins' "Different per section": ``prefs["per_section"]`` = ``[[plus, neg fraction], …]`` in the order of the
    summary's sections → ``{name: (plus, absolute minus)}``."""
    rows = prefs.get("per_section")
    if not rows or not sections:
        return None
    return {name: (float(pl), negative_from_fraction(float(pl), float(ng))) for (name, _), (pl, ng) in zip(sections, rows)}


async def run_marks(app, task) -> None:
    """Lane runner for the Marks output."""
    progress = ProgressMessage(app.sender, task.chat_id, interval_s=app.settings.get("edit_interval_s"), task=task)
    progress.attach(task.data["ui_message_id"])
    try:
        await _marks(app, task, progress)
    finally:
        progress.close()  # no-op after finish(); stops the edit loop when the task fails or is cancelled


async def _marks(app, task, progress: ProgressMessage, *, last: bool = True) -> None:
    progress.set("🧮 <b>Calculating your marks…</b>")
    prefs = task.data["prefs"]
    marking = _marking(prefs)
    per_section = _per_section(prefs, task.data.get("sections"))
    data, sc = await run_cpu(app, "parse", task.task_id, score_sheets, str(_input_dir(task)), task.data.get("hints") or {},
                             marking.plus, marking.minus, marking.source, per_section)
    m = data.meta
    cand = []
    if data.candidate.name:
        cand.append(mask_name(data.candidate.name, app.settings.get("privacy.mask_name")))  # display only; DB keeps it
    if data.candidate.roll_no:
        roll = data.candidate.roll_no
        cand.append(f"Roll {mask_roll(roll) if app.settings.get('privacy.mask_roll') else roll}")
    ctx = {"exam_name": m.exam_name or "Scorecard", "meta_line": meta_line(m),
           "candidate_line": " · ".join(cand), **sc.to_dict(), "branding": app.config.branding,
           "colors": {k: v for k in ("banner_border", "banner_fill") if (v := app.settings.get(f"pdf.colors.{k}"))}}
    progress.set("🎨 <b>Drawing your scorecard…</b>")
    out = task.workdir / "scorecard.png"
    await run_cpu(app, "card", task.task_id, render_scorecard, ctx, str(out))
    caption = scorecard_caption(m, sc, app.config.branding)
    who = f"👤 <code>{task.user_id}</code> · task <code>{task.task_id}</code>"
    progress.set("📤 <b>Sending your scorecard…</b>")
    # uploader bot → dump (reply to the anchor), then the main bot copies it to the user (core.delivery)
    task.outputs.append(await deliver(app, task, out, caption=f"{caption}\n{who}"[:1024], user_caption=caption, kind="photo"))
    rid = await save_result(app.repo, data, sc, task.user_id)
    task.data["json"] = f"result {'saved' if rid else 'not saved (no roll number)'} · {exam_key(m)}"
    task.data["meta"] = m
    if last:  # the exam JSON is built when a PDF / quiz needs it, not for marks (owner 2026-10-12)
        await progress.finish("✅ <b>Done!</b> Your scorecard is above. Send another sheet any time.")


LANG_NAMES = {"en": "English", "hi": "Hindi"}
PDF_LOOK = 4  # bump when the custom PDF looks different: cached copies of the old look are not reused (4: centred
# banner, answer strip, explanation box, bold fix — 2026-10-15)


def pdf_look(app) -> str:
    """8 hex digits of every setting a custom PDF's look depends on besides columns and font (``/ps`` colours, header /
    footer, ``/wm`` watermark, branding) — part of the cache key, so after a change the next request renders a fresh
    PDF instead of copying the old one (B-028: "ps settings mai … colour change krne ka ni hai kuch")."""
    import hashlib
    import json

    from bot.render.theme import pdf_theme
    theme = pdf_theme(app.settings, app.config.branding)
    theme["watermark"].pop("cache_dir", None)
    return hashlib.sha1(json.dumps(theme, sort_keys=True, default=str).encode()).hexdigest()[:8]


def pdf_file_name(m, lang: str | None = None, section: str | None = None) -> str:
    """``(17-09-2026) Exam name (Shift 1).pdf`` (+ `` - Hindi`` when two languages are produced); a section PDF is
    named after the section: ``(17-09-2026) Reasoning (Shift 1).pdf`` — spec §15.4."""
    import re as _re
    d = "-".join(reversed(m.date.split("-"))) if m.date else ""
    parts = [f"({d})" if d else "", section or m.exam_name or "Exam", f"(Shift {m.shift})" if m.shift else ""]
    name = " ".join(p for p in parts if p) + (f" - {LANG_NAMES.get(lang, lang)}" if lang else "")
    return _re.sub(r"\s+", " ", _re.sub(r'[\\/:*?"<>|\r\n\t]+', " ", name)).strip()[:150] + ".pdf"


async def run_custom(app, task) -> None:
    """Lane runner (heavy) for Marks + Custom PDF (spec §14.3): scorecard first, then the exam from the bank (reuse,
    else build) → one PDF per chosen language. Without the candidate's own answers marked, a PDF depends only on
    the exam + preferences, so it is cached in ``exams.outputs`` and later requests are a ``copy_message``."""
    progress = ProgressMessage(app.sender, task.chat_id, interval_s=app.settings.get("edit_interval_s"), task=task)
    progress.attach(task.data["ui_message_id"])
    try:
        await _marks(app, task, progress, last=False)
        await _custom(app, task, progress)
    finally:
        progress.close()


async def _custom(app, task, progress: ProgressMessage) -> None:
    from bot.core.tgref import download
    from bot.exam.registry import ensure
    from bot.render.exam_pdf import render_exam_pdf
    from bot.render.theme import app_theme

    prefs = task.data["prefs"]
    progress.set("📚 <b>Preparing the question paper…</b>")
    bank = await ensure(app, task)
    key = bank["key"]
    rec = await app.repo.exams.get(key) or {}
    # languages a whole PDF can be made in (a Hindi-medium sheet with an English part is Hindi only)
    secs = [(x.get("name", ""), x.get("count", 0)) for x in rec.get("sections") or [] if isinstance(x, dict)]
    langs = (main_languages([set(q.get("lh") or {}) for q in rec.get("qs") or []], secs or None)
             or rec.get("languages") or ["en"])
    want = prefs.get("lang") or "both"
    chosen_langs = langs if want == "both" else ([want] if want in langs else langs[:1])
    answers = prefs.get("answers") or "after"
    mine = bool(prefs.get("mine", True)) and answers == "after"
    m = task.data.get("meta")
    path = bank.get("path")
    outputs = rec.get("outputs") or {}
    look = pdf_look(app)
    # admins/owner: "Each section + Full" → the full PDF, then one PDF per section (spec §13.3); others: full only
    sections = rec.get("sections") or []
    split = prefs.get("split") == "each" and len(sections) > 1 and app.access.is_admin(task.user_id)
    jobs = [(lang, idx) for lang in chosen_langs for idx in [None, *(range(len(sections)) if split else ())]]
    for n_job, (lang, idx) in enumerate(jobs, 1):
        sec_name = sections[idx]["name"] if idx is not None else None
        name = pdf_file_name(m, lang if len(chosen_langs) > 1 else None, sec_name)
        kind = f"Section: {html.escape(sec_name)}" if sec_name else "Custom PDF"
        caption = (f"🧾 <b>{html.escape(m.exam_name or 'Exam')}</b>\n<i>{html.escape(meta_line(m))}</i>\n"
                   f"{kind} · {LANG_NAMES.get(lang, lang)}" + (" · your answers marked" if mine else "")
                   + f"\n{html.escape(app.config.branding)}")
        cols = 1 if str(app.settings.get("pdf.columns")) == "1" else 2  # 2 = hybrid; an old "auto" = 2
        font = "noto" if app.settings.get("pdf.font") == "noto" else "poppins"
        cache_key = (f"custom_v{rec.get('version', 1)}_{lang}_{answers}_c{cols}_{font}_{look}_l{PDF_LOOK}"  # /ps, /wm → new PDF
                     + (f"_s{idx}" if idx is not None else ""))
        count = f" ({n_job}/{len(jobs)})" if len(jobs) > 1 else ""
        cached = None if mine else outputs.get(cache_key)
        if cached:
            try:
                await app.sender.copy(task.chat_id, cached["chat_id"], cached["message_id"], caption=caption)
                task.outputs.append({"name": name, "size_h": "cached", "link": None})
                continue
            except Exception as e:  # dump copy gone: render again
                log.info("cached output %s unusable (%r) — rendering", cache_key, e)
        if path is None:
            progress.set("📥 <b>Loading the question paper…</b>")
            path = str(await download(app, bank["ref"], task.workdir / "bank"))
        what = html.escape(sec_name) if sec_name else "your PDF"
        progress.set(f"🧾 <b>Building {what}</b> · {LANG_NAMES.get(lang, lang)}{count}…")
        out = task.workdir / "out" / name
        out.parent.mkdir(parents=True, exist_ok=True)
        label = LANG_NAMES.get(lang, lang) + (f" · {sec_name}" if sec_name else "")
        ctx = {"bundle": path, "lang": lang, "columns": cols, "font": font, "answers": answers,
               "responses": task.data.get("responses") if mine else None, "title": m.exam_name, "meta_line": meta_line(m),
               "lang_label": label, "theme": app_theme(app), "sections": None if idx is None else [idx],
               "colors": app.settings.get("pdf.colors"), "workdir": str(task.workdir / "img")}
        info = await run_cpu(app, "render", task.task_id, render_exam_pdf, ctx, str(out), est_mb=150)
        progress.set(f"📤 <b>Sending {what}</b> · {info['pages']} pages{count}…")
        who = f"👤 <code>{task.user_id}</code> · task <code>{task.task_id}</code>"
        rec_out = await deliver(app, task, out, caption=f"{caption}\n{who}"[:1024], user_caption=caption)
        task.outputs.append(rec_out)
        if not mine and rec_out.get("ref"):
            await app.repo.exams.update(key, {"$set": {f"outputs.{cache_key}": rec_out["ref"]}})
    await progress.finish("✅ <b>Done!</b> Your scorecard and PDF are above. Send another sheet any time.")


async def run_original(app, task) -> None:
    """Lane runner (heavy) for Marks + Original PDF (spec §14.2): scorecard first, then the sheet itself in its own
    format (candidate details removed, parts joined in question order). Not cached: the sheet shows this
    candidate's own answers."""
    progress = ProgressMessage(app.sender, task.chat_id, interval_s=app.settings.get("edit_interval_s"), task=task)
    progress.attach(task.data["ui_message_id"])
    try:
        await _marks(app, task, progress, last=False)
        await _original(app, task, progress)
    finally:
        progress.close()


async def _original(app, task, progress: ProgressMessage) -> None:
    from bot.render.original_pdf import render_original_pdf
    from bot.render.theme import app_theme

    m = task.data.get("meta")
    files = [str(p) for p in input_files(_input_dir(task))]
    progress.set("📄 <b>Building your PDF</b> · same format as the sheet…")
    name = pdf_file_name(m)
    out = task.workdir / "out" / name
    out.parent.mkdir(parents=True, exist_ok=True)
    ctx = {"files": files, "hints": task.data.get("hints") or {}, "title": name[:-4],
           "theme": app_theme(app)}
    info = await run_cpu(app, "render", task.task_id, render_original_pdf, ctx, str(out), est_mb=300)
    if info["missing"]:
        log.info("original pdf %s: %d images not in the archive", task.task_id, info["missing"])
    caption = (f"📄 <b>{html.escape(m.exam_name or 'Exam')}</b>\n<i>{html.escape(meta_line(m))}</i>\n"
               f"Original PDF · {info['pages']} pages\n{html.escape(app.config.branding)}")
    progress.set(f"📤 <b>Sending your PDF</b> · {info['pages']} pages…")
    who = f"👤 <code>{task.user_id}</code> · task <code>{task.task_id}</code>"
    task.outputs.append(await deliver(app, task, out, caption=f"{caption}\n{who}"[:1024], user_caption=caption))
    await progress.finish("✅ <b>Done!</b> Your scorecard and PDF are above. Send another sheet any time.")


def scorecard_caption(m, sc, branding: str) -> str:
    """Telegram caption (≤ 1024 chars): score, accuracy, counts; section-wise detail in an expandable quote with
    short section names, one line each (owner 2026-10-13: "caption bahut messy")."""
    attempted = sc.right + sc.wrong
    head = [f"📊 <b>{html.escape(m.exam_name or 'Scorecard')}</b>"]
    if meta_line(m):
        head.append(f"<i>{html.escape(meta_line(m))}</i>")
    head += ["", f"🏁 Score  <b>{sc.marks:g} / {sc.max_marks:g}</b>"]
    if attempted:
        head.append(f"🎯 Accuracy  <b>{sc.right * 100 / attempted:.1f}%</b>")
    head.append(f"✅ <b>{sc.right}</b> right  ·  ❌ <b>{sc.wrong}</b> wrong  ·  ⭕ <b>{sc.unattempted}</b> skipped")
    tail = [f"📝 Marking  {html.escape(sc.marking_label)}", html.escape(branding)]
    rows = []
    if len(sc.sections) > 1:
        for i, s in enumerate(sc.sections, 1):
            row = (f"{i}. {html.escape(short_section(s.name, 30))}  <b>{s.marks:g}/{s.max_marks:g}</b>\n"
                   f"     ✅ {s.right}  ❌ {s.wrong}  ⭕ {s.unattempted}")
            if len("\n".join(head + rows + tail)) + len(row) + 60 > 1000:
                rows.append("…")
                break
            rows.append(row)
    quote = "<blockquote expandable>📚 <b>Section-wise</b>\n" + "\n".join(rows) + "</blockquote>"
    body = head + ([quote] if rows else [""]) + tail
    return "\n".join(body)[:1024]
