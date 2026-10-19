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
- ``await run_slides(app, task)`` — ``/slides`` (admins, owner 2026-10-19): no marks; the exam from the bank → one
  question per A4-landscape page, exam details on page 1, answer key grid at the end (``render_exam_pdf`` layout
  ``slides``), one PDF per chosen language, cached like the custom PDF. ``prefs.style == "original"`` (owner
  2026-10-19: "jodo slides mai", colours kept): the sheet's own question boxes on the same slides
  (``original_pdf.render_original_slides``), one PDF in the sheet's language, not cached (it shows the candidate's
  answers); a sheet whose boxes cannot be matched to its questions → the custom slides, with a note.
- ``await custom_outputs(...)`` / ``await original_outputs(...)`` — the PDFs themselves (full + optional per section,
  delivered); shared by the two runners above and ``/batch`` (``exam.batch``).
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
from bot.exam.urls import register as register_url, sheet_langs
from bot.net.fetcher import detect_kind
from bot.parsers import parse_sources, source
from bot.parsers.common import OTHER_LANGS
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
    the marking); register the exam URLs (links sent + page URL inside each MHT) in ``exam_urls`` with the sheet's
    language(s) (the inputs of one task are one candidate's sheet — ``parse_sources`` refuses two candidates — so
    they share it; owner 2026-10-19: links exported per language)."""
    m, summary = await run_cpu(app, "parse", task.task_id, inspect_sheets, str(_input_dir(task)), task.data.get("hints") or {})
    urls = {it["url"] for it in task.data.get("inputs", []) if it.get("url") and it["status"] == "stored"}
    if m.url and detect_kind(m.url):
        urls.add(m.url)
    langs, mix = sheet_langs(summary)  # the sheet's medium question by question (another language is saved as such)
    for u in urls:
        try:
            await register_url(app.repo, u, m, task.user_id, langs=langs, mix=mix)
        except Exception as e:  # registry problems never block the user
            log.warning("url registry failed: %r", e)
    return summary


def summary_text(s: dict, sections: bool = True) -> str:
    lines = [f"📄 <b>{html.escape(s['exam_name'] or 'Exam')}</b>"]
    if s.get("meta_line"):
        lines.append(f"<i>{html.escape(s['meta_line'])}</i>")
    facts = [f"🧩 <b>{s['questions']}</b> questions", f"📚 <b>{len(s['sections'])}</b> section{'s' if len(s['sections']) != 1 else ''}"]
    other = (s.get("medium") or {}).get("other")
    if other:  # a Bengali / Tamil … sheet: the parser files it under English, the census knows better
        facts.append("🌐 " + OTHER_LANGS.get(other, other))
    elif s.get("languages"):
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
SLIDES_LOOK = 1  # bump when the slides PDF looks different (cached copies of the old look are not reused)
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
    from bot.exam.registry import ensure

    prefs = task.data["prefs"]
    progress.set("📚 <b>Preparing the question paper…</b>")
    bank = await ensure(app, task)
    answers = prefs.get("answers") or "after"
    # admins/owner: "Each section + Full" → the full PDF, then one PDF per section (spec §13.3); others: full only
    await custom_outputs(app, task, progress, bank, task.data.get("meta"), lang=prefs.get("lang") or "both",
                         answers=answers, mine=bool(prefs.get("mine", True)) and answers == "after",
                         split=prefs.get("split") == "each" and app.access.is_admin(task.user_id))
    await progress.finish("✅ <b>Done!</b> Your scorecard and PDF are above. Send another sheet any time.")


async def run_slides(app, task) -> None:
    """Lane runner (heavy) for ``/slides``: read the sheet's details, the exam from the bank (reuse, else build), then
    the slides PDF per chosen language (``custom_outputs(layout="slides")``)."""
    from bot.exam.registry import ensure

    progress = ProgressMessage(app.sender, task.chat_id, interval_s=app.settings.get("edit_interval_s"), task=task)
    progress.attach(task.data["ui_message_id"])
    try:
        progress.set("📚 <b>Preparing the question paper…</b>")
        m, _summary = await run_cpu(app, "parse", task.task_id, inspect_sheets, str(_input_dir(task)),
                                    task.data.get("hints") or {})
        bank = await ensure(app, task)
        sec = await _slide_section(app, task, bank, _summary)
        note = ""
        if task.data["prefs"].get("style") == "original":
            try:
                sent = await _slides_original(app, task, progress, bank, m, sec)
            except ValueError as e:  # boxes ≠ questions: the custom slides instead
                log.info("slides as in the sheet %s: %s — custom slides", task.task_id, e)
                note = "\n<i>This sheet's question boxes could not be matched, so the slides are in the custom look.</i>"
                sent = await custom_outputs(app, task, progress, bank, m, lang="both", layout="slides", section=sec)
        else:
            sent = await custom_outputs(app, task, progress, bank, m, lang=task.data["prefs"].get("lang") or "both",
                                        layout="slides", section=sec)
        await progress.finish(f"✅ <b>Done!</b> Your slides PDF{'s are' if len(sent) > 1 else ' is'} above.{note}")
    finally:
        progress.close()


async def _slide_section(app, task, bank: dict, summary: dict) -> int | None:
    """``/slides`` 📚 Section: the card's choice (an index into the summary's sections, ``None`` = all) → the bank's
    section index (matched by name, else the same place)."""
    pick = task.data["prefs"].get("section")
    names = [n for n, _c in (summary or {}).get("sections") or []]
    if pick is None or not 0 <= int(pick) < len(names):
        return None
    rec = await app.repo.exams.get(bank["key"]) or {}
    bank_names = [x.get("name", "") if isinstance(x, dict) else "" for x in rec.get("sections") or []]
    if names[pick] in bank_names:
        return bank_names.index(names[pick])
    return int(pick) if int(pick) < len(bank_names) else None


async def _slides_original(app, task, progress, bank: dict, m, section: int | None = None) -> list[str]:
    """``/slides`` as in the sheet: the slide frames (cover, badges, answer key) from the exam bank, then the sheet's
    own question boxes on them (``section``: only that one). ``ValueError`` when the boxes do not match the
    questions."""
    from bot.core.tgref import download
    from bot.render.exam_pdf import render_exam_pdf
    from bot.render.original_pdf import render_original_slides
    from bot.render.theme import app_theme

    rec = await app.repo.exams.get(bank["key"]) or {}
    lang = next(iter([x for x in (m.languages or []) if x in LANG_NAMES] or rec.get("languages") or ["en"]))
    path = bank.get("path") or str(await download(app, bank["ref"], task.workdir / "bank"))
    secs = rec.get("sections") or []
    sec_name = secs[section]["name"] if section is not None and section < len(secs) else None
    name = pdf_file_name(m, None, sec_name)[:-4] + " - Slides (as in the sheet).pdf"
    out = task.workdir / "out" / name
    out.parent.mkdir(parents=True, exist_ok=True)
    frames = task.workdir / "out" / "frames.pdf"
    progress.set("🖼 <b>Building your slides</b> · as in the sheet…")
    theme = app_theme(app)
    font = "noto" if app.settings.get("pdf.font") == "noto" else "poppins"
    ctx = {"bundle": path, "lang": lang, "font": font, "layout": "slides", "frame_only": True, "title": m.exam_name,
           "meta_line": "", "lang_label": LANG_NAMES.get(lang, lang), "details": _details(m, lang), "theme": theme,
           "colors": app.settings.get("pdf.colors"), "workdir": str(task.workdir / "img"),
           "sections": None if sec_name is None else [section]}
    fr = await run_cpu(app, "render", task.task_id, render_exam_pdf, ctx, str(frames), est_mb=150)
    files = [str(p) for p in input_files(_input_dir(task))]
    info = await run_cpu(app, "render", task.task_id, render_original_slides,
                         {"files": files, "hints": task.data.get("hints") or {}, "theme": theme, "frames": str(frames),
                          "body": fr["slide_body"], "first": 1, "questions": fr["questions"],
                          "section": None if sec_name is None else section}, str(out), est_mb=300)
    what = f" · {html.escape(sec_name)}" if sec_name else ""
    caption = (f"🖼 <b>{html.escape(m.exam_name or 'Exam')}</b>\n<i>{html.escape(meta_line(m))}</i>\n"
               f"Slides PDF{what} · as in the sheet · {info['pages']} pages\n{html.escape(app.config.branding)}")
    progress.set(f"📤 <b>Sending your slides</b> · {info['pages']} pages…")
    who = f"👤 <code>{task.user_id}</code> · task <code>{task.task_id}</code>"
    task.outputs.append(await deliver(app, task, out, caption=f"{caption}\n{who}"[:1024], user_caption=caption))
    return [name]


def _details(m, lang: str) -> list[tuple[str, str]]:
    """The cover facts of a slides PDF (page 1): date, shift, time, language."""
    from bot.exam.keys import display_date
    return [("Exam date", display_date(m.date) if m.date else ""), ("Shift", str(m.shift) if m.shift else ""),
            ("Exam time", m.time or ""), ("Language", LANG_NAMES.get(lang, lang))]


async def custom_outputs(app, task, progress, bank: dict, m, *, lang: str = "both", answers: str = "after",
                         mine: bool = False, split: bool = False, fallback: bool = True, layout: str = "paper",
                         section: int | None = None) -> list[str]:
    """The custom PDFs of one exam from the bank (``registry.ensure`` result): per language the full PDF, then (``split``)
    one per section; each delivered (dump copy + the task's chat) and, without the candidate's answers, cached in
    ``exams.outputs`` (a repeat is a ``copy_message``). ``lang``: ``both`` or one language — missing → the exam's first
    language (``fallback``) or nothing. ``layout="slides"`` (``/slides``): one question per landscape page, the answer
    key grid at the end — no candidate's answers, ``answers`` ignored; ``section`` (the bank's section index) → only that
    section, named after it (owner 2026-10-19: "kisi ek section ki bhi slide"). Returns the file names sent. Used by
    the custom PDF task, ``/slides`` and ``/batch``."""
    slides = layout == "slides"
    if slides:
        answers, mine, split = "key", False, False
    from bot.core.tgref import download
    from bot.render.exam_pdf import render_exam_pdf
    from bot.render.theme import app_theme

    key = bank["key"]
    rec = await app.repo.exams.get(key) or {}
    # languages a whole PDF can be made in (a Hindi-medium sheet with an English part is Hindi only)
    secs = [(x.get("name", ""), x.get("count", 0)) for x in rec.get("sections") or [] if isinstance(x, dict)]
    langs = (main_languages([set(q.get("lh") or {}) for q in rec.get("qs") or []], secs or None)
             or rec.get("languages") or ["en"])
    want = lang or "both"
    chosen_langs = langs if want == "both" else ([want] if want in langs else (langs[:1] if fallback else []))
    mine = mine and answers == "after"
    path = bank.get("path")
    outputs = rec.get("outputs") or {}
    look = pdf_look(app)
    sections = rec.get("sections") or []
    split = split and len(sections) > 1
    jobs = [(lang, idx) for lang in chosen_langs for idx in [None, *(range(len(sections)) if split else ())]]
    if slides and section is not None and section < len(sections):
        jobs = [(lang, section) for lang in chosen_langs]
    sent: list[str] = []
    for n_job, (lang, idx) in enumerate(jobs, 1):
        sec_name = sections[idx]["name"] if idx is not None else None
        name = pdf_file_name(m, lang if len(chosen_langs) > 1 else None, sec_name)
        if slides:
            name = name[:-4] + " - Slides.pdf"
        kind = (("Slides PDF" + (f" · {html.escape(sec_name)}" if sec_name else "")) if slides
                else f"Section: {html.escape(sec_name)}" if sec_name else "Custom PDF")
        caption = (f"🧾 <b>{html.escape(m.exam_name or 'Exam')}</b>\n<i>{html.escape(meta_line(m))}</i>\n"
                   f"{kind} · {LANG_NAMES.get(lang, lang)}" + (" · your answers marked" if mine else "")
                   + f"\n{html.escape(app.config.branding)}")
        cols = 1 if str(app.settings.get("pdf.columns")) == "1" else 2  # 2 = hybrid; an old "auto" = 2
        font = "noto" if app.settings.get("pdf.font") == "noto" else "poppins"
        cache_key = (f"custom_v{rec.get('version', 1)}_{lang}_{answers}_c{cols}_{font}_{look}_l{PDF_LOOK}"  # /ps, /wm → new PDF
                     + (f"_s{idx}" if idx is not None else ""))
        if slides:
            cache_key = (f"slides_v{rec.get('version', 1)}_{lang}_{font}_{look}_l{PDF_LOOK}_k{SLIDES_LOOK}"
                         + (f"_s{idx}" if idx is not None else ""))
        count = f" ({n_job}/{len(jobs)})" if len(jobs) > 1 else ""
        cached = None if mine else outputs.get(cache_key)
        if cached:
            try:
                await app.sender.copy(task.chat_id, cached["chat_id"], cached["message_id"], caption=caption)
                task.outputs.append({"name": name, "size_h": "cached", "link": None})
                sent.append(name)
                continue
            except Exception as e:  # dump copy gone: render again
                log.info("cached output %s unusable (%r) — rendering", cache_key, e)
        if path is None:
            progress.set("📥 <b>Loading the question paper…</b>")
            path = str(await download(app, bank["ref"], task.workdir / "bank"))
        what = html.escape(sec_name) if sec_name else "your slides" if slides else "your PDF"
        progress.set(f"🧾 <b>Building {what}</b> · {LANG_NAMES.get(lang, lang)}{count}…")
        out = task.workdir / "out" / name
        out.parent.mkdir(parents=True, exist_ok=True)
        label = LANG_NAMES.get(lang, lang) + (f" · {sec_name}" if sec_name else "")
        ctx = {"bundle": path, "lang": lang, "columns": cols, "font": font, "answers": answers,
               "responses": task.data.get("responses") if mine else None, "title": m.exam_name, "meta_line": meta_line(m),
               "lang_label": label, "theme": app_theme(app), "sections": None if idx is None else [idx],
               "colors": app.settings.get("pdf.colors"), "workdir": str(task.workdir / "img")}
        if slides:  # page 1 shows the details as cards: the banner keeps only the name and the language line
            ctx.update(layout="slides", meta_line="", lang_label=LANG_NAMES.get(lang, lang), details=_details(m, lang))
        info = await run_cpu(app, "render", task.task_id, render_exam_pdf, ctx, str(out), est_mb=150)
        progress.set(f"📤 <b>Sending {what}</b> · {info['pages']} pages{count}…")
        who = f"👤 <code>{task.user_id}</code> · task <code>{task.task_id}</code>"
        rec_out = await deliver(app, task, out, caption=f"{caption}\n{who}"[:1024], user_caption=caption)
        task.outputs.append(rec_out)
        sent.append(name)
        if not mine and rec_out.get("ref"):
            await app.repo.exams.update(key, {"$set": {f"outputs.{cache_key}": rec_out["ref"]}})
    return sent


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
    files = [str(p) for p in input_files(_input_dir(task))]
    await original_outputs(app, task, progress, task.data.get("meta"), files, hints=task.data.get("hints"))
    await progress.finish("✅ <b>Done!</b> Your scorecard and PDF are above. Send another sheet any time.")


async def original_outputs(app, task, progress, m, files: list[str], *, hints: dict | None = None, lang: str | None = None,
                           sections: list | None = None) -> list[str]:
    """The sheet in its own format (spec §14.2): the full PDF, then (``sections`` = the paper's section names, in order,
    more than one) one PDF per section — ``/batch`` "each section + full". ``lang`` adds `` - Hindi`` / `` - English``
    to the names (a batch makes both from two sheets). Each delivered like ``custom_outputs``; not cached (the sheet
    shows a candidate's answers). Returns the file names sent."""
    from bot.render.original_pdf import render_original_pdf
    from bot.render.theme import app_theme

    jobs = [None, *range(len(sections))] if sections and len(sections) > 1 else [None]
    sent: list[str] = []
    for n_job, idx in enumerate(jobs, 1):
        sec_name = sections[idx] if idx is not None else None
        count = f" ({n_job}/{len(jobs)})" if len(jobs) > 1 else ""
        what = html.escape(sec_name) if sec_name else "your PDF"
        progress.set(f"📄 <b>Building {what}</b> · same format as the sheet{count}…")
        name = pdf_file_name(m, lang, sec_name)
        out = task.workdir / "out" / name
        out.parent.mkdir(parents=True, exist_ok=True)
        ctx = {"files": files, "hints": hints or {}, "title": name[:-4], "theme": app_theme(app), "section": idx}
        info = await run_cpu(app, "render", task.task_id, render_original_pdf, ctx, str(out), est_mb=300)
        if info["missing"]:
            log.info("original pdf %s: %d images not in the archive", task.task_id, info["missing"])
        kind = f"Section: {html.escape(sec_name)}" if sec_name else "Original PDF"
        caption = (f"📄 <b>{html.escape(m.exam_name or 'Exam')}</b>\n<i>{html.escape(meta_line(m))}</i>\n"
                   + kind + (f" · {LANG_NAMES.get(lang, lang)}" if lang else "") + f" · {info['pages']} pages\n"
                   + html.escape(app.config.branding))
        progress.set(f"📤 <b>Sending {what}</b> · {info['pages']} pages{count}…")
        who = f"👤 <code>{task.user_id}</code> · task <code>{task.task_id}</code>"
        task.outputs.append(await deliver(app, task, out, caption=f"{caption}\n{who}"[:1024], user_caption=caption))
        sent.append(name)
    return sent


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
