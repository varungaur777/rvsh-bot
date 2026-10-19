"""``/batch`` — response-sheet links in bulk (spec §14.7; owner's list, "prompt for bot.txt" §7, and the blueprint
``download_edqt_sections_mhtml.py``). Admins. Built 2026-10-19: output modes 1–2 (owner: "Mode 1-2 abhi, 3-4 baad
mein"), then modes 3–4 (owner 2026-10-19: "ab bacha hua batch command work pura karo"; chose: every section by
itself, nothing asked mid-run; OCR text later, separately).

Languages (owner 2026-10-19): a TCS sheet holds only its candidate's medium, so a shift's **Hindi link + English
link** are both fetched; in custom style the two sheets become one exam in the bank (``registry.ensure`` merges an
English and a Hindi sheet of the same paper by question id) → a Hindi PDF and an English PDF. An EDQT link brings both
versions itself (the fetcher opens each question's other-language popup) — same code path; live EDQT links could not
be tested yet. A shift with one language only gets that one; the report says which link is missing.
**Hindi and English only** (owner 2026-10-19: "link store to kro other language ka par jb short krne ho links and batch
command mai to bs hindi and english hi lene hai"): a link whose sheet is in another language (Bengali …) is saved with
that language but never planned; a sheet that turns out to be one when fetched is left out (``lang_bn``). Per shift the
link with the most questions in the wanted language is taken (RRB sheets mix Hindi and English — ``urls._share``).

Flow — one background-lane task (``tasks.create(solo=False)``: hours of work never lock the admin out of the bot):
1. **Links → shifts.** Saved links already know exam / date / start time / language (``exam_urls``); any other link
   — and one read before the language census (``urls.needs_check``) — is checked first (its dump ``.mht`` or the page
   only, ``linkcheck.check_all``) and saved; a pasted list gets back its Hindi / English / not
   working ``.txt`` files (the filtered list). One link per exam + date + start time per language
   (``urls._best_per_shift``); a link last seen dead (404, not a sheet …) is not opened again.
2. **Per shift, in order** (the next shifts' pages are fetched meanwhile, ``batch.fetch_concurrency`` at once):
   fetch each link in full (page + images, EDQT section pages, other-language versions) → read it (pool) → save the
   link with its language → **every page `.mht` to dump** with an info caption (exam, date, shift, language,
   sections, source, URL; the shift's first page replies to the batch's dump post, its outputs reply to that page) →
   outputs: ``custom`` (exam bank → ``pipeline.custom_outputs`` per language, cached) or ``original`` (as in the
   sheet → ``pipeline.original_outputs`` per language sheet); ``each`` = full + one PDF per section, ``full`` = full.
   ``merged`` / ``oneliner`` (modes 3–4): the shift only goes into the exam bank here (merged as in the sheet: its
   pages are kept instead) — its PDFs come in step 2b.
2b. **Modes 3–4, after the last shift** (``_merged``): every bank of the run is loaded again (dump), its sections
   grouped per exam by ``bundle.section_key`` → one bundle per section of all shifts (``bundle.merge_sections``: a
   part per shift, each question tagged ``exam • date • shift``) → per language ``merged``: the custom PDF of that
   section, one sub-banner per shift (answers as chosen) · ``oneliner``: question + ``Ans: (b) <option>`` + its tag,
   no options (``render_exam_pdf`` layout ``oneliner``) → compressed (§15.8) → admin + dump (reply to the batch's
   dump post). Names: ``Section - Exam (all shifts).pdf`` / ``Section - One-liner - Exam.pdf`` (+ `` - Hindi``).
   After a restart this step runs again from the banks (nothing of it is half-saved).
   **Merged as in the sheet** (owner 2026-10-19: "as it is style mai bhi merge kroge na sections" → "har page ke upar
   chhoti line"): each shift's sheet pages are kept (``workdir/keep``; after a restart they come back from the saved
   ``.mht`` in dump) → per exam, section and sheet language: ``render_original_pdf(groups=…)`` — that section of every
   shift in the portal's own look, page numbers across the PDF, ``Exam • date • Shift N`` at the top left of each
   page. **One-liners as in the sheet** (owner 2026-10-19: "han kar do"): the same, but each question keeps only its
   right option (``render_original_pdf`` ``oneliner``: TCS ✓ row, EDQT green cell with ``(b)``) →
   ``Section - One-liner - Exam.pdf``.
3. A link or shift that fails is reported with its reason; the batch goes on. The state is saved in ``batches`` after
   every shift → after a restart the admin gets "Resume?" (finished shifts are skipped). ⏹ Stop ends it at once.
4. **Report** ``.txt`` (✅ / ❌ per shift, languages, files) → the admin + dump.

Public API
----------
- ``PREFS`` — defaults ``{"mode": each|full|merged|oneliner, "style": custom|original, "answers": after|grid,
  "lang": both|hi|en, "limit": 0 (all) | N (trial: first N shifts)}``. ``COLLECT`` = the modes made after the last
  shift (merged / oneliner — custom or as in the sheet).
- ``plan_shifts(docs, lang) -> [shift]`` — ``exam_urls``-shaped docs → shifts ``{key, exam_name, date, shift, time,
  start_hhmm, links: {lang: {url, langs, status, hints}}}`` sorted exam → date → time.
- ``shift_label(shift)`` — ``(16/09/2026) Exam (Shift 2)``.
- ``await run(app, task)`` — the runner. ``task.data``: ``prefs``, ``title``, and ``items`` ``[(url, hints)]`` (a
  pasted list) or ``slugs`` (saved links of those exams; ``None`` = all) — or ``resume`` = a ``batches`` id.
- ``await boot_notice(app)`` — batches a restart interrupted → a "Resume?" card to their admin.

Exceptions → outcome: per link / shift: logged + reported, never stops the batch. The admin blocked the bot / can't
be written to → the batch ends (state kept: resumable). Cancel → stopped, report of what was done is sent.
Depends on: ``exam.linkcheck``, ``exam.urls``, ``exam.registry``, ``exam.pipeline``, ``exam.jobs``, ``net.service``,
``compute``, ``ui.progress``. Used by: ``handlers.batch``, ``bot.app`` (boot notice).
"""

from __future__ import annotations

import asyncio
import html
import logging
import re
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path

from bot.compute import run_cpu
from bot.core.errors import BotError, SourceUnavailable, UserBlockedBot
from bot.core.sender import BULK, undeliverable
from bot.core.tgref import from_message, private_link
from bot.exam import linkcheck
from bot.exam import urls as U
from bot.exam.jobs import inspect_sheets
from bot.exam.keys import display_date
from bot.net import service
from bot.net.fetcher import detect_kind, redact
from bot.parsers.models import ExamMeta
from bot.ui.progress import ProgressMessage

log = logging.getLogger(__name__)
PREFS = {"mode": "each", "style": "custom", "answers": "after", "lang": "both", "limit": 0}
COLLECT = ("merged", "oneliner")  # modes 3–4: PDFs of every shift's section together, after the last shift
TRIAL = 2
LANGS = ("hi", "en")
NAMES = {"hi": "Hindi", "en": "English", "both": "Hindi + English"}
_DEAD = ("http_404", "http_410", "not_exam", "unsupported_url")  # last seen like this → not opened again


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def shift_label(s: dict) -> str:
    d = "/".join(reversed((s.get("date") or "").split("-"))) or "??/??/????"
    return f"({d}) {s.get('exam_name') or 'Exam'}" + (f" (Shift {s['shift']})" if s.get("shift") else "")


def _hints(r: dict) -> dict:
    return {k: v for k, v in (("date", r.get("date")), ("exam_name", r.get("exam_name")), ("shift", r.get("shift"))) if v}


def _shift_key(r: dict) -> tuple:
    """Which shift a link belongs to: the TCS paper code + session in its URL (``urls.shift_code`` — identical for the
    Hindi and the English sheet, whatever name each prints), else exam + date + start time."""
    code = U.shift_code(r.get("url") or "")
    return ("tcs", code) if code else (r.get("exam_name_slug") or "", r.get("date") or "",
                                       r.get("start_hhmm") or f"s{r.get('shift') or ''}")


def plan_shifts(docs: list[dict], lang: str = "both") -> list[dict]:
    """Saved-link docs → one entry per shift (``_shift_key``) with its best link per wanted language."""
    groups: dict[tuple, dict] = {}
    for lg in LANGS:
        if lang not in ("both", lg):
            continue
        for r in U._best_per_shift(docs, lg, per_exam=True):
            key = _shift_key(r)
            g = groups.setdefault(key, {"exam_name": r.get("exam_name"), "date": r.get("date"), "shift": r.get("shift"),
                                        "time": r.get("time"), "start_hhmm": r.get("start_hhmm"),
                                        "exam_code": r.get("exam_code"), "links": {}})
            g["links"][lg] = {"url": r["url"], "langs": r.get("langs") or [], "status": r.get("status"), "hints": _hints(r)}
    order = sorted(groups, key=lambda k: ((groups[k]["exam_name"] or "~").lower(), groups[k]["date"] or "",
                                         groups[k]["start_hhmm"] or "", k))
    return [{**groups[k], "key": "|".join(k)} for k in order]


async def _check_needed(app, task, todo: list, known: list[dict], want: list[str], show) -> tuple[list[dict], int]:
    """Read links without a known language — but per shift (``urls.shift_code``) only until every wanted language
    is found: an owner's raw list holds many candidates of one shift, the rest are never opened (owner 2026-10-19:
    "ache se match krne ka trika bnana fast"). Links without a shift code (EDQT) are all read.
    Returns (rows read, links skipped)."""
    have: dict[str, set] = {}
    for x in known:  # languages the saved links already give each shift
        code = U.shift_code(x.get("url") or "")
        if code and x.get("status") in (None, "ok"):
            have.setdefault(code, set()).update(x.get("langs") or [])
    groups: dict[str, list] = {}
    for n, (u, h) in enumerate(todo):
        groups.setdefault(U.shift_code(u) or f"#{n}", []).append((u, h))
    gate = asyncio.Semaphore(max(1, int(app.settings.get("batch.fetch_concurrency") or 2)))
    rows: list[dict] = []
    state = {"read": 0, "skipped": 0, "seq": 0}

    def line() -> None:
        show(f"🔍 <b>Reading the links first</b> · {state['read']} read · {state['skipped']} skipped of {len(todo)}\n"
             "<i>Page only; per shift just until its Hindi and English sheets are found.</i>")

    async def group(code: str, links: list) -> None:
        got = have.setdefault(code, set())
        for j, (u, h) in enumerate(links):
            if not code.startswith("#") and set(want) <= got:
                state["skipped"] += len(links) - j
                line()
                return
            async with gate:
                state["seq"] += 1
                row = await linkcheck.check_one(app, task, state["seq"], u, h)
            rows.append(row)
            got.update(row.get("langs") or [])
            state["read"] += 1
            line()

    line()
    await asyncio.gather(*(group(c, links) for c, links in groups.items()))
    return rows, state["skipped"]


# ------------------------------------------------------------------ links → shifts
_PROJ = {"url": 1, "date": 1, "shift": 1, "time": 1, "start_hhmm": 1, "exam_name": 1, "exam_name_slug": 1,
         "exam_code": 1, "status": 1, "last_checked": 1, "langs": 1, "mix": 1, "unreadable": 1}


async def _saved(app, *, urls: set[str] | None = None, slugs: list[str] | None = None) -> list[dict]:
    flt = {"exam_name_slug": {"$in": list(slugs)}} if slugs else {}
    docs = await app.repo.exam_urls.find(flt, projection=_PROJ)
    return [d for d in docs if urls is None or d.get("url") in urls]


async def _links_to_shifts(app, task, show) -> tuple[list[dict], list[dict]]:
    """(shifts, links that did not work). Links without a known language are checked first (page only)."""
    d = task.data
    prefs = d["prefs"]
    items = d.get("items")
    if items is not None:  # a pasted list
        wanted = {U.canonical(u) for u, _h in items}
        hints = {U.canonical(u): h for u, h in items}
        known = [x for x in await _saved(app, urls=wanted) if not U.needs_check(x)]
        todo = [(u, h) for u, h in items if U.canonical(u) not in {x["url"] for x in known}]
    else:  # saved links of the picked exam(s)
        docs = await _saved(app, slugs=d.get("slugs"))
        wanted = {x["url"] for x in docs}
        hints = {x["url"]: _hints(x) for x in docs}
        known = [x for x in docs if not U.needs_check(x)]
        todo = [(x["url"], _hints(x)) for x in docs if U.needs_check(x)]
    failed: list[dict] = []
    if todo:
        want = list(LANGS) if (prefs.get("lang") or "both") == "both" else [prefs["lang"]]
        rows, d["skipped_links"] = await _check_needed(app, task, todo, known, want, show)
        failed = [r for r in rows if r.get("error")]
    docs = await _saved(app, urls=wanted)
    archive = await U.mht_state(app.repo, [x["url"] for x in docs])
    for x in docs:  # a line's own (date) Exam (Shift N) fills what the sheet does not print (EDQT admin view)
        for k, v in (hints.get(x["url"]) or {}).items():
            if not x.get(k):
                x[k] = v
        x["mht_good"] = archive.get(x["url"]) == 0  # per shift + language: a link with a good saved copy first
    if items is not None:  # the filtered list back (owner's raw lists: one link per shift, per language)
        per_lang = {lg: U._best_per_shift(docs, lg, per_exam=True) for lg in LANGS}
        skipped = d.get("skipped_links") or 0
        await linkcheck.send_files(app, task.chat_id, task.user_id, per_lang, f"{d.get('title') or 'Batch'} (filtered)",
                                   failed=failed, note="the list this batch runs on" + (
                                       f" · {skipped} more links of shifts already found were not opened" if skipped else ""))
    shifts = plan_shifts(docs, prefs.get("lang") or "both")
    return shifts, failed


# ------------------------------------------------------------------ one shift
@dataclass
class _Sheet:
    """One fetched sheet of a shift, shaped like the parts of a ``TaskContext`` that ``registry.ensure``,
    ``core.delivery`` and the ``pipeline`` output helpers read: ``workdir/in`` holds its ``.mht`` pages,
    ``data`` its hints + dump anchor."""
    task_id: str
    user_id: int
    chat_id: int
    workdir: Path
    lang: str  # hi | en | both
    url: str
    data: dict = field(default_factory=dict)
    outputs: list = field(default_factory=list)
    meta: ExamMeta | None = None
    summary: dict | None = None
    pages: int = 0
    error: str | None = None
    saved: list | None = None  # the dump copies used instead of the link (``urls.saved_mht``)
    missing: list = field(default_factory=list)  # per fetched page: page files the fetch could not get


class _Step:
    """``progress.set`` for the pipeline helpers: their line goes under the batch's own header."""

    def __init__(self, show) -> None:
        self.show = show

    def set(self, text: str) -> None:
        self.show(text)


async def _fetch_shift(app, task, i: int, s: dict, want: list[str]) -> list[_Sheet]:
    """Fetch + read every wanted language's sheet of shift ``i`` (one link for both when it holds both)."""
    sdir = task.workdir / f"shift{i:04d}"
    sheets: list[_Sheet] = []
    seen: dict[str, _Sheet] = {}
    for lg in want:
        link = s["links"].get(lg)
        if not link:
            continue
        if link["url"] in seen:  # an EDQT link with both versions: one sheet for both
            seen[link["url"]].lang = "both"
            continue
        sh = _Sheet(task.task_id, task.user_id, task.chat_id, sdir / f"{lg}", lg, link["url"],
                    data={"hints": link.get("hints") or {}})
        seen[link["url"]] = sh
        sheets.append(sh)
        folder = sh.workdir / "in"
        # the sheet's .mht pages saved in dump earlier (owner 2026-10-19: "link to expire ho jate hai link na kam kre
        # to mht kam kregi"): a complete, good copy is used first (no request to the exam site); a copy with page
        # files missing only when the link itself no longer gives a sheet ("… dubara achi mht aati hai, to usko db
        # mai update krdena" — a better fetch replaces it in the archive)
        refs = await _saved_refs(app, link["url"])
        good = bool(refs) and not any(int(r.get("missing") or 0) for r in refs)
        if good and await _saved_pages(app, refs, folder, link["url"]):
            sh.saved, sh.pages = refs, len(refs)
        elif link.get("status") in _DEAD and not refs:
            sh.error = link["status"]
            continue
        else:
            try:
                if link.get("status") in _DEAD:  # dead link, imperfect saved copy: the copy is all there is
                    raise SourceUnavailable(detail=link["status"], reason=link["status"])
                pages = await service.fetch_url(app, link["url"])
            except SourceUnavailable as e:
                reason = e.reason or "network"
                if reason not in ("network", "remote") and link.get("status") not in _DEAD:
                    await _quiet(U.mark_status(app.repo, link["url"], reason))
                if refs and await _saved_pages(app, refs, folder, link["url"]):
                    sh.saved, sh.pages = refs, len(refs)
                else:
                    sh.error = reason
                    continue
            else:
                def write(pages=pages, folder=folder) -> None:
                    folder.mkdir(parents=True, exist_ok=True)
                    for k, p in enumerate(pages):
                        (folder / f"p{k:02d}.mht").write_bytes(p.mht)
                await asyncio.to_thread(write)
                sh.pages, sh.missing = len(pages), [len(p.missing) for p in pages]
        try:
            sh.meta, sh.summary = await run_cpu(app, "parse", task.task_id, inspect_sheets, str(folder), sh.data["hints"])
        except BotError as e:
            sh.error = "not_exam" if e.key in ("no_questions", "unknown_format", "parse_error") else e.key
            continue
        langs, mix = U.sheet_langs(sh.summary)
        await _quiet(U.register(app.repo, link["url"], sh.meta, task.user_id, "ok", langs, mix))
        other = (sh.summary.get("medium") or {}).get("other")
        if other:  # another language after all (a link read before the census): saved as such, not used
            sh.error = f"lang_{other}"
    return sheets


async def _quiet(coro) -> None:
    try:
        await coro
    except Exception as e:
        log.debug("batch: ignored %r", e)


async def _saved_refs(app, url: str) -> list[dict]:
    try:
        return await U.saved_mht(app.repo, url)
    except Exception as e:
        log.debug("batch: saved mht lookup failed: %r", e)
        return []


async def _saved_pages(app, refs: list[dict], folder: Path, url: str) -> list[dict] | None:
    """Download the dump copies ``refs`` of ``url``'s pages into ``folder`` as ``pNN.mht``; ``None`` when one can no
    longer be read (then the link itself is the way)."""
    from bot.core.tgref import download
    try:
        for k, ref in enumerate(refs):
            got = await download(app, dict(ref), folder / "dl")
            await asyncio.to_thread(got.replace, folder / f"p{k:02d}.mht")
        return refs
    except asyncio.CancelledError:
        raise
    except Exception as e:  # a dump copy deleted / unreadable: the live link is the fallback
        log.info("batch: saved copy of %s unusable (%r) — opening the link", redact(url), e)
        await asyncio.to_thread(shutil.rmtree, folder, True)
        return None


def _lang_of(sh: _Sheet) -> str:
    langs = (sh.summary or {}).get("languages") or []
    return "both" if len(langs) > 1 else (langs[0] if langs else sh.lang)


def _dump_caption(task, i: int, total: int, s: dict, sh: _Sheet, k: int) -> str:
    m, summ = sh.meta, sh.summary or {}
    secs = ", ".join(html.escape(n) for n, _c in (summ.get("sections") or [])[:8])
    part = f" · page {k + 1}/{sh.pages}" if sh.pages > 1 else ""
    return (f"📥 <b>Batch</b> · task <code>{task.task_id}</code> · shift {i + 1}/{total}\n"
            f"📘 <b>{html.escape(m.exam_name or s.get('exam_name') or 'Exam')}</b>\n"
            f"🗓 {html.escape(' • '.join(x for x in (display_date(m.date), f'Shift {m.shift}' if m.shift else '', m.time) if x))}\n"
            f"🌐 {NAMES.get(_lang_of(sh), sh.lang)} · {html.escape((m.source or detect_kind(sh.url) or '?').upper())}{part}"
            + (f"\n📚 {secs}" if secs else "") + f"\n🔗 {html.escape(sh.url)}")[:1024]


def _mht_name(s: dict, sh: _Sheet, k: int) -> str:
    base = shift_label({**s, "date": sh.meta.date or s.get("date"), "exam_name": sh.meta.exam_name or s.get("exam_name"),
                        "shift": sh.meta.shift or s.get("shift")})
    base = base.replace("/", "-")
    name = f"{base} - {NAMES.get(_lang_of(sh), sh.lang)}" + (f" - page {k + 1}" if sh.pages > 1 else "")
    return re.sub(r"\s+", " ", re.sub(r'[\\/:*?"<>|\r\n\t]+', " ", name)).strip()[:150] + ".mht"


async def _to_dump(app, task, i: int, total: int, s: dict, sh: _Sheet, anchor: int | None) -> int | None:
    """Every page of a sheet to dump (uploader bot) — the first one's message id (the shift's anchor)."""
    first = None
    for k, path in enumerate(sorted((sh.workdir / "in").glob("p*.mht"))):
        try:
            _bot, msg = await app.uploads.send_document(app.config.dump_channel_id, str(path),
                                                        _dump_caption(task, i, total, s, sh, k),
                                                        reply_to=first or anchor, file_name=_mht_name(s, sh, k))
        except asyncio.CancelledError:
            raise
        except Exception as e:  # dump trouble never costs the admin the PDFs
            log.warning("batch %s: dump upload of %s failed: %r", task.task_id, redact(sh.url), e)
            continue
        first = first or msg.id
        ref = from_message(msg)
        if ref:  # remembered for this link: the sheet stays usable after the link expires (no repeats)
            await _quiet(U.add_mht_page(app.repo, sh.url, {**ref, "link": private_link(msg.chat.id, msg.id),
                                                           "name": _mht_name(s, sh, k)}, k, sh.pages, task.user_id,
                                        missing=sh.missing[k] if k < len(sh.missing) else 0))
    return first


async def _outputs(app, task, prefs: dict, sheets: list[_Sheet], step: _Step) -> tuple[list[str], list[str]]:
    """(files sent, exam bank keys of this shift)."""
    from bot.exam import pipeline
    from bot.exam.registry import ensure

    ok = [sh for sh in sheets if not sh.error]
    want = prefs.get("lang") or "both"
    each = prefs.get("mode") == "each"
    files: list[str] = []
    if _original(prefs) and prefs.get("mode") in COLLECT:  # its pages are kept (``_shifts``): PDFs after the last shift
        return [], []
    if _original(prefs) and prefs.get("mode") not in COLLECT:
        for sh in ok:
            lg = _lang_of(sh)
            names = [n for n, _c in (sh.summary or {}).get("sections") or []] if each else None
            src = [str(p) for p in sorted((sh.workdir / "in").glob("p*.mht"))]
            label = None if lg == "both" else lg
            if want != "both" and lg not in (want, "both"):
                continue
            files += await pipeline.original_outputs(app, sh, step, sh.meta, src, hints=sh.data["hints"], lang=label,
                                                     sections=names)
        return files, []
    banks: dict[str, tuple[dict, _Sheet]] = {}
    for sh in ok:  # the second language's sheet merges into the first one's exam (registry, owner 2026-10-12)
        step.set(f"📚 <b>Exam bank</b> · {NAMES.get(_lang_of(sh), sh.lang)} sheet…")
        bank = await ensure(app, sh)
        banks[bank["key"]] = (bank, sh)
    if prefs.get("mode") in COLLECT:  # modes 3–4: the PDFs come after the last shift (``_merged``)
        return [], list(banks)
    for bank, sh in banks.values():
        files += await pipeline.custom_outputs(app, sh, step, bank, sh.meta, lang=want, answers=prefs.get("answers") or "after",
                                               mine=False, split=each, fallback=False)
    return files, list(banks)


def _original(prefs: dict) -> bool:
    """As in the sheet: every mode (one-liners too since 2026-10-19)."""
    return prefs.get("style") == "original"


def _result(sheets: list[_Sheet], s: dict, want: list[str], files: list[str], error: str = "",
            banks: list[str] | None = None, collect: bool = False, kept: list[dict] | None = None) -> dict:
    """``collect`` (modes 3–4): the shift is done when its exam is in the bank (``banks``) — or, merged as in the
    sheet, when its pages are kept (``kept``: ``[{lang, url, sections, hints}]``); its PDFs come later."""
    errs = {sh.lang: sh.error for sh in sheets if sh.error}
    missing = [lg for lg in want if lg not in s["links"]]
    got = sorted({lg for sh in sheets if not sh.error for lg in (LANGS if _lang_of(sh) == "both" else [_lang_of(sh)])})
    saved = sorted({sh.lang for sh in sheets if sh.saved})
    done = (kept if kept is not None else banks) if collect else files
    return {"ok": bool(done) and not error, "langs": got, "files": files, "errors": errs, "missing": missing,
            "error": error, "saved": saved, "banks": banks or [], **({"kept": kept} if kept is not None else {}),
            "at": _now()}


# ------------------------------------------------------------------ modes 3–4
def _merged_name(section: str, exam: str, oneliner: bool, lang: str | None) -> str:
    name = (f"{section} - One-liner - {exam}" if oneliner else f"{section} - {exam} (all shifts)") + (
        f" - {NAMES.get(lang, lang)}" if lang else "")
    return re.sub(r"\s+", " ", re.sub(r'[\\/:*?"<>|\r\n\t]+', " ", name)).strip()[:150] + ".pdf"


async def _merged(app, task, doc: dict, show) -> dict:
    """Modes 3–4 after the last shift: per exam, each section of all its shifts → one PDF per language (see the module
    doc, step 2b). Returns ``{"files", "errors", "at"}`` (a section that fails is reported, the others go on)."""
    from bot.core.delivery import deliver
    from bot.core.tgref import download
    from bot.exam import bundle as BU
    from bot.exam.marks import short_section
    from bot.exam.schema import _LANG_SUBJECT
    from bot.render.compress import compress_file
    from bot.render.exam_pdf import render_exam_pdf
    from bot.render.theme import app_theme

    prefs = doc["prefs"]
    oneliner = prefs.get("mode") == "oneliner"
    folder = task.workdir / "merge"
    rows, seen = [], set()
    for i, s in enumerate(doc["shifts"]):  # one bank per shift (its Hindi + English sheets are one exam)
        for key in (doc["results"].get(str(i)) or {}).get("banks") or []:
            if key not in seen:
                seen.add(key)
                rows.append((i, s, key))
    files: list[str] = []
    errors: list[str] = []
    zips = []
    for n, (i, s, key) in enumerate(rows):
        show(f"📚 <b>Loading the question papers</b> · {n + 1}/{len(rows)}")
        rec = await app.repo.exams.get(key) or {}
        if not rec.get("json_ref"):
            errors.append(f"{shift_label(s)}: exam data missing")
            continue
        try:
            got = await download(app, rec["json_ref"], folder / "dl")
            path = folder / f"b{n:04d}.zip"
            await asyncio.to_thread(got.replace, path)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.warning("batch %s: bank %s not loaded: %r", task.task_id, key, e)
            errors.append(f"{shift_label(s)}: exam data could not be loaded")
            continue
        zips.append((s, path))
    if not zips:
        return {"files": files, "errors": errors, "at": _now()}
    names = await run_cpu(app, "render", task.task_id, BU.sections_of, [str(p) for _s, p in zips], est_mb=80)
    groups: dict[tuple, dict] = {}
    for (s, path), secs in zip(zips, names):
        exam = s.get("exam_name") or "Exam"
        date = display_date(s.get("date")) if s.get("date") else ""
        shift = f"Shift {s['shift']}" if s.get("shift") else ""
        label = " · ".join(x for x in (date, shift, s.get("time") or "") if x) or "Shift"
        tag = " • ".join(x for x in (exam, date, shift) if x)
        for si, (name, _count) in enumerate(secs):
            g = groups.setdefault((exam.lower(), BU.section_key(name)), {"exam": exam, "name": name, "parts": []})
            g["parts"].append({"zip": str(path), "section": si, "label": label, "tag": tag})
    want = prefs.get("lang") or "both"
    cols = 1 if str(app.settings.get("pdf.columns")) == "1" else 2
    font = "noto" if app.settings.get("pdf.font") == "noto" else "poppins"
    anchor = _Sheet(task.task_id, task.user_id, task.chat_id, folder, "both", "", data={"anchor_id": doc.get("anchor")})
    for gi, g in enumerate(groups.values(), 1):
        sec = short_section(g["name"], 60)
        title = f"{sec} — " + ("one-liners, all shifts" if oneliner else "all shifts")
        show(f"🧩 <b>{html.escape(sec)}</b> · {html.escape(g['exam'])} · section {gi}/{len(groups)} · "
             f"{len(g['parts'])} shift{'s' if len(g['parts']) != 1 else ''}")
        try:
            out_zip = folder / f"m{gi:03d}.zip"
            info = await run_cpu(app, "render", task.task_id, BU.merge_sections, g["parts"], str(out_zip), title, est_mb=120)
            # a language's PDF holds only the shifts that have it (never one mixed PDF — owner 2026-10-13): a shift whose
            # sheet is Hindi only is in the Hindi PDF only; an English / Hindi Language section stays in its language
            plangs = info["part_languages"]
            avail = [lg for lg in LANGS if any(lg in pl for pl in plangs)]
            chosen = avail if want == "both" else [want] if want in avail else []
            subject = not chosen and bool(_LANG_SUBJECT.search(g["name"]))
            if subject:
                chosen = avail[:1] or ["en"]
            if not chosen:
                errors.append(f"{sec} ({g['exam']}): no {NAMES.get(want, want)} questions")
            for lang in chosen:
                sub = g["parts"] if subject else [pt for pt, pl in zip(g["parts"], plangs) if lang in pl]
                zip_path = out_zip
                if len(sub) != len(g["parts"]):
                    zip_path = folder / f"m{gi:03d}_{lang}.zip"
                    await run_cpu(app, "render", task.task_id, BU.merge_sections, sub, str(zip_path), title, est_mb=120)
                name = _merged_name(sec, g["exam"], oneliner, lang if len(chosen) > 1 else None)
                out = folder / "out" / name
                out.parent.mkdir(parents=True, exist_ok=True)
                ctx = {"bundle": str(zip_path), "lang": lang, "columns": cols, "font": font,
                       "answers": prefs.get("answers") or "after", "layout": "oneliner" if oneliner else "paper",
                       "index": True,
                       "title": title, "meta_line": g["exam"],
                       "lang_label": NAMES.get(lang, lang) + f" · {len(sub)} shift{'s' if len(sub) != 1 else ''}",
                       "theme": app_theme(app),
                       "colors": app.settings.get("pdf.colors"), "workdir": str(folder / "img")}
                r = await run_cpu(app, "render", task.task_id, render_exam_pdf, ctx, str(out), est_mb=200)
                await run_cpu(app, "render", task.task_id, compress_file, str(out), est_mb=200)
                kind = "One-liners" if oneliner else "Section of all shifts"
                caption = (f"🧩 <b>{html.escape(sec)}</b> · {kind}\n<i>{html.escape(g['exam'])}</i>\n"
                           f"{len(sub)} shift{'s' if len(sub) != 1 else ''} · {r['questions']} questions · {r['pages']} pages · "
                           f"{NAMES.get(lang, lang)}\n{html.escape(app.config.branding)}")[:1024]
                who = f"👤 <code>{task.user_id}</code> · task <code>{task.task_id}</code>"
                await deliver(app, anchor, out, caption=f"{caption}\n{who}"[:1024], user_caption=caption)
                files.append(name)
                await asyncio.to_thread(out.unlink, True)
        except (asyncio.CancelledError, UserBlockedBot):
            raise
        except Exception as e:
            if undeliverable(e):
                raise
            app.dump.report_error(e, f"batch {task.task_id} merged {sec}")
            errors.append(f"{sec} ({g['exam']}): {e.__class__.__name__}")
    await asyncio.to_thread(shutil.rmtree, folder, True)
    return {"files": files, "errors": errors, "at": _now()}


async def _keep(task, i: int, sheets: list[_Sheet]) -> list[dict]:
    """Merged as in the sheet: shift ``i``'s sheet pages moved to ``workdir/keep/shift<i>/<lang>`` (the shift folder is
    deleted after the shift) → what ``_merged_original`` needs to find them again."""
    out = []
    for sh in sheets:
        dest = task.workdir / "keep" / f"shift{i:04d}" / sh.lang
        await asyncio.to_thread(dest.parent.mkdir, parents=True, exist_ok=True)
        await asyncio.to_thread(shutil.move, str(sh.workdir / "in"), str(dest))
        out.append({"lang": _lang_of(sh), "dir": sh.lang, "url": sh.url, "hints": sh.data.get("hints") or {},
                    "sections": [n for n, _c in (sh.summary or {}).get("sections") or []]})
    return out


async def _merged_original(app, task, doc: dict, show) -> dict:
    """Merged sections as in the sheet, after the last shift (module doc, step 2b): per exam, section and sheet
    language one PDF of that section of every shift (``render_original_pdf`` ``groups``), each page labelled with its
    shift; one-liners: only each question's right option. Pages kept by ``_keep``, else (a restart) downloaded again
    from the saved ``.mht``."""
    from bot.core.delivery import deliver
    from bot.exam import bundle as BU
    from bot.exam.marks import short_section
    from bot.render.original_pdf import render_original_pdf
    from bot.render.theme import app_theme

    want = doc["prefs"].get("lang") or "both"
    oneliner = doc["prefs"].get("mode") == "oneliner"
    folder = task.workdir / "merge"
    files: list[str] = []
    errors: list[str] = []
    groups: dict[tuple, dict] = {}
    for i, s in enumerate(doc["shifts"]):
        for sheet in (doc["results"].get(str(i)) or {}).get("kept") or []:
            pages = task.workdir / "keep" / f"shift{i:04d}" / sheet["dir"]
            if not any(pages.glob("p*.mht")):  # resumed after a restart: from the dump copies
                show(f"📥 <b>Loading the saved sheets</b> · shift {i + 1}/{len(doc['shifts'])}")
                refs = await _saved_refs(app, sheet["url"])
                if not (refs and await _saved_pages(app, refs, pages, sheet["url"])):
                    errors.append(f"{shift_label(s)}: its saved sheet could not be loaded")
                    continue
            exam = s.get("exam_name") or "Exam"
            date = display_date(s.get("date")) if s.get("date") else ""
            label = " • ".join(x for x in (exam, date, f"Shift {s['shift']}" if s.get("shift") else "",
                                           NAMES.get(sheet["lang"]) if sheet["lang"] in LANGS else "") if x)
            src = [str(x) for x in sorted(pages.glob("p*.mht"))]
            for si, name in enumerate(sheet["sections"]):
                g = groups.setdefault((exam.lower(), BU.section_key(name), sheet["lang"]),
                                      {"exam": exam, "name": name, "lang": sheet["lang"], "parts": []})
                g["parts"].append({"files": src, "hints": sheet["hints"], "section": si, "label": label})
    langs = {g["lang"] for g in groups.values()}
    anchor = _Sheet(task.task_id, task.user_id, task.chat_id, folder, "both", "", data={"anchor_id": doc.get("anchor")})
    todo = [g for g in groups.values() if want == "both" or g["lang"] in (want, "both")]
    for gi, g in enumerate(todo, 1):
        sec = short_section(g["name"], 60)
        show(f"🧩 <b>{html.escape(sec)}</b> · {html.escape(g['exam'])} · "
             f"{'one-liners, ' if oneliner else ''}as in the sheet · {gi}/{len(todo)} · "
             f"{len(g['parts'])} shift{'s' if len(g['parts']) != 1 else ''}")
        try:
            lang = g["lang"] if g["lang"] in LANGS and len(langs) > 1 else None
            name = _merged_name(sec, g["exam"], oneliner, lang)
            out = folder / "out" / name
            await asyncio.to_thread(out.parent.mkdir, parents=True, exist_ok=True)
            ctx = {"groups": g["parts"], "theme": app_theme(app), "title": name[:-4], "oneliner": oneliner}
            r = await run_cpu(app, "render", task.task_id, render_original_pdf, ctx, str(out), est_mb=300)
            caption = (f"🧩 <b>{html.escape(sec)}</b> · {'One-liners' if oneliner else 'Section of all shifts'} · "
                       "as in the sheet\n"
                       f"<i>{html.escape(g['exam'])}</i>\n{len(g['parts'])} shift{'s' if len(g['parts']) != 1 else ''} · "
                       f"{r['pages']} pages · {NAMES.get(g['lang'], g['lang'])}\n{html.escape(app.config.branding)}")[:1024]
            who = f"👤 <code>{task.user_id}</code> · task <code>{task.task_id}</code>"
            await deliver(app, anchor, out, caption=f"{caption}\n{who}"[:1024], user_caption=caption)
            files.append(name)
            await asyncio.to_thread(out.unlink, True)
        except (asyncio.CancelledError, UserBlockedBot):
            raise
        except Exception as e:
            if undeliverable(e):
                raise
            app.dump.report_error(e, f"batch {task.task_id} merged original {sec}")
            errors.append(f"{sec} ({g['exam']}): {e.__class__.__name__}")
    if want != "both" and not todo and groups:
        errors.append(f"no {NAMES.get(want, want)} sheet")
    await asyncio.to_thread(shutil.rmtree, folder, True)
    await asyncio.to_thread(shutil.rmtree, task.workdir / "keep", True)
    return {"files": files, "errors": errors, "at": _now()}


# ------------------------------------------------------------------ the runner
async def run(app, task) -> None:
    d = task.data
    progress = ProgressMessage(app.sender, task.chat_id, interval_s=app.settings.get("edit_interval_s"), task=task)
    progress.attach(d["ui_message_id"])
    d["progress"] = progress
    progress.set_markup(_stop_markup(task))
    head = {"text": f"📦 <b>Batch</b> · {html.escape(d.get('title') or '')}"}

    def show(line: str) -> None:
        progress.set(f"{head['text']}\n{line}")

    bid = d.get("resume") or task.task_id
    doc = await app.repo.batches.get(bid) if d.get("resume") else None
    if d.get("resume") and not (doc and doc.get("shifts")):
        await progress.finish("📦 That batch is gone — start a new one with /batch.", reply_markup=None)
        progress.close()
        return
    try:
        if doc is None:
            shifts, failed = await _links_to_shifts(app, task, show)
            prefs = d["prefs"]
            if prefs.get("limit"):
                shifts = shifts[:int(prefs["limit"])]
            title = d.get("title") or _title(shifts)
            doc = {"user_id": task.user_id, "chat_id": task.chat_id, "title": title, "prefs": prefs, "shifts": shifts,
                   "results": {}, "failed_links": [{"url": r["url"], "error": r.get("error")} for r in failed],
                   "status": "running", "created_at": _now()}
            if not shifts:
                await progress.finish(f"{head['text']}\n⚠️ <b>No shift to process</b> — none of the links could be read"
                                      + (f" ({len(failed)} not working, file above)." if failed else "."))
                return
            anchor = await _dump_anchor(app, task, doc)
            doc["anchor"] = anchor
            await app.repo.batches.update(bid, {"$set": doc})
        else:
            await app.repo.batches.update(bid, {"$set": {"status": "running", "resumed_at": _now()}})
        d["title"] = doc["title"]
        head["text"] = f"📦 <b>Batch</b> · {html.escape(doc['title'])}"
        await _shifts(app, task, bid, doc, show)
        if doc["prefs"].get("mode") in COLLECT:
            run_merge = _merged_original if _original(doc["prefs"]) else _merged
            doc["merged"] = await run_merge(app, task, doc, show)
            await app.repo.batches.update(bid, {"$set": {"merged": doc["merged"]}})
        await app.repo.batches.update(bid, {"$set": {"status": "done", "ended_at": _now()}})
        await _report(app, task, doc)
        await progress.finish(f"{head['text']}\n" + _summary(doc) + "\n<i>Report file above.</i>", reply_markup=None)
    except asyncio.CancelledError:
        if task.status != "restart" and doc is not None and doc.get("shifts"):  # stopped by the admin
            await _quiet(app.repo.batches.update(bid, {"$set": {"status": "stopped", "ended_at": _now()}}))
            await _quiet(_report(app, task, doc, stopped=True))
            await _quiet(progress.finish(f"{head['text']}\n⏹ <b>Stopped</b> · " + _summary(doc), reply_markup=None))
        raise
    finally:
        d.pop("progress", None)
        progress.close()


def _title(shifts: list[dict]) -> str:
    names = sorted({s.get("exam_name") or "Exam" for s in shifts})
    return names[0] if len(names) == 1 else f"{len(names)} exams"


def _stop_markup(task):
    from bot.ui.keyboards import btn, cb, kb
    return kb([btn("⏹ Stop", cb("bat", task.user_id, task.task_id, "stop"), style="danger")])


async def _dump_anchor(app, task, doc: dict) -> int | None:
    p = doc["prefs"]
    text = (f"📦 <b>Batch started</b> · task <code>{task.task_id}</code> · 👤 <code>{task.user_id}</code>\n"
            f"<b>{html.escape(doc['title'])}</b> · {len(doc['shifts'])} shift{'s' if len(doc['shifts']) != 1 else ''}\n"
            f"{_prefs_line(p)}")
    try:
        msg = await app.sender.send_message(app.config.dump_channel_id, text, priority=BULK)
        return msg.id
    except Exception as e:
        log.warning("batch %s: dump anchor failed: %r", task.task_id, e)
        return None


MODE_NAMES = {"each": "each section + full", "full": "full PDF", "merged": "each section of all shifts together",
              "oneliner": "one-liners of each section (all shifts)"}


def _prefs_line(p: dict) -> str:
    mode = p.get("mode") or "each"
    original = _original(p)
    return " · ".join([
        MODE_NAMES.get(mode, mode),
        "as in the sheet" if original else "custom",
        *([] if original or mode == "oneliner" else ["answers " + ("under each question" if p.get("answers") != "grid"
                                                                     else "at section end")]),
        NAMES.get(p.get("lang") or "both", "?")] + ([f"trial: first {p['limit']}"] if p.get("limit") else []))


async def _shifts(app, task, bid: str, doc: dict, show) -> None:
    shifts, results, prefs = doc["shifts"], doc["results"], doc["prefs"]
    want = list(LANGS) if (prefs.get("lang") or "both") == "both" else [prefs["lang"]]
    order = [i for i in range(len(shifts)) if str(i) not in results]
    ahead = max(1, int(app.settings.get("batch.fetch_concurrency") or 2))
    started, done_now = time.monotonic(), 0
    pending: dict[int, asyncio.Task] = {}

    def fetch(pos: int) -> None:
        if pos < len(order) and pos not in pending:
            i = order[pos]
            pending[pos] = task.spawn(_fetch_shift(app, task, i, shifts[i], want))

    def line(i: int, what: str) -> None:
        ok = sum(1 for r in results.values() if r.get("ok"))
        bad = len(results) - ok
        eta = ""
        if done_now:
            left = (time.monotonic() - started) / done_now * (len(order) - done_now)
            eta = f" · ⏳ {int(left // 60)}m left" if left >= 60 else f" · ⏳ {int(left)}s left"
        show(f"Shift <b>{i + 1}/{len(shifts)}</b> · ✅ {ok} · ❌ {bad}{eta}\n<i>{html.escape(shift_label(shifts[i]))}</i>\n{what}")

    try:
        for pos, i in enumerate(order):
            for p in range(pos, pos + ahead):
                fetch(p)
            line(i, "🌐 <b>Opening the sheet(s)…</b>")
            s = shifts[i]
            files, error, banks, kept = [], "", [], None
            sheets: list[_Sheet] = []
            try:
                sheets = await pending.pop(pos)
                ok = [sh for sh in sheets if not sh.error]
                if ok:
                    line(i, "📤 <b>Saving the sheet(s) to dump…</b>")
                    anchor = doc.get("anchor")
                    for sh in ok:
                        if sh.saved:  # already in dump: outputs reply to its first page
                            first = sh.saved[0]
                            sh.data["anchor_id"] = (first.get("message_id") if first.get("chat_id") == app.config.dump_channel_id
                                                    else anchor)
                        else:
                            sh.data["anchor_id"] = await _to_dump(app, task, i, len(shifts), s, sh, anchor) or anchor
                        anchor = sh.data["anchor_id"]
                    files, banks = await _outputs(app, task, prefs, sheets, _Step(lambda t, i=i: line(i, t)))
                    if _original(prefs) and prefs.get("mode") in COLLECT:
                        kept = await _keep(task, i, ok)
            except (asyncio.CancelledError, UserBlockedBot):
                raise
            except Exception as e:
                if undeliverable(e):  # the admin's chat refuses the bot: stop here, resumable
                    raise
                error = f"{e.__class__.__name__}"
                app.dump.report_error(e, f"batch {task.task_id} shift {i + 1}")
            finally:
                await asyncio.to_thread(shutil.rmtree, task.workdir / f"shift{i:04d}", True)
            results[str(i)] = _result(sheets, s, want, files, error, banks, prefs.get("mode") in COLLECT, kept)
            done_now += 1
            await app.repo.batches.update(bid, {"$set": {f"results.{i}": results[str(i)], "updated_at": _now()}})
    finally:
        for job in pending.values():
            job.cancel()


def _summary(doc: dict) -> str:
    res = doc["results"]
    ok = sum(1 for r in res.values() if r.get("ok"))
    files = sum(len(r.get("files") or []) for r in res.values()) + len((doc.get("merged") or {}).get("files") or [])
    return (f"✅ <b>{ok}</b> of {len(doc['shifts'])} shifts · ❌ {len(res) - ok} · 📄 {files} PDF{'s' if files != 1 else ''}"
            + (f" · ⏳ {len(doc['shifts']) - len(res)} not reached" if len(res) < len(doc["shifts"]) else ""))


def report_text(doc: dict) -> str:
    lines = [f"# Batch report — {doc['title']}", f"# {_prefs_line(doc['prefs'])}", f"# {_summary(doc)}".replace("<b>", "")
             .replace("</b>", ""), ""]
    for i, s in enumerate(doc["shifts"]):
        r = doc["results"].get(str(i))
        if r is None:
            lines.append(f"⏳ {shift_label(s)} · not reached")
            continue
        why = [f"{NAMES.get(lg, lg)}: {linkcheck.reason_text(e)}" for lg, e in (r.get("errors") or {}).items()]
        why += [f"{NAMES.get(lg, lg)}: no link" for lg in r.get("missing") or []]
        if r.get("error"):
            why.append(f"error ({r['error']}) — see the dump channel")
        langs = " + ".join(NAMES[lg] for lg in r.get("langs") or [] if lg in NAMES) or "—"
        what = (f"{len(r.get('files') or [])} PDFs" if (doc["prefs"].get("mode") or "each") not in COLLECT else
                ("sheet kept for the merge" if r.get("kept") else "sheet not kept") if "kept" in r else
                ("in the exam bank" if r.get("banks") else "not in the exam bank"))
        lines.append(f"{'✅' if r.get('ok') else '❌'} {shift_label(s)} · {langs} · {what}"
                     + (" · from the saved .mht" if r.get("saved") else "") + (f" · {'; '.join(why)}" if why else ""))
        lines += [f"     {f}" for f in r.get("files") or []]
    merged = doc.get("merged")
    if merged is not None:
        lines += ["", f"# {MODE_NAMES.get(doc['prefs'].get('mode'), '')}: {len(merged.get('files') or [])} PDFs"]
        lines += [f"     {f}" for f in merged.get("files") or []]
        lines += [f"     ❌ {x}" for x in merged.get("errors") or []]
    for x in doc.get("failed_links") or []:
        lines.append(f"# not working · {linkcheck.reason_text(x.get('error'))}: {x['url']}")
    return "\n".join(lines) + "\n"


async def _report(app, task, doc: dict, stopped: bool = False) -> None:
    folder = Path(app.root) / "tmp" / str(task.user_id)
    await asyncio.to_thread(folder.mkdir, parents=True, exist_ok=True)
    path = folder / f"{linkcheck._safe('Batch - ' + doc['title'])} - report.txt"
    await asyncio.to_thread(path.write_text, report_text(doc), encoding="utf-8")
    caption = (f"🧾 <b>Batch report</b>{' (stopped)' if stopped else ''} · {html.escape(doc['title'])}\n" + _summary(doc))[:1024]
    try:
        await app.sender.send_document(task.chat_id, str(path), caption=caption)
    except Exception:
        path.unlink(missing_ok=True)
        raise
    app.dump.send_file(path, f"{caption}\n👤 <code>{task.user_id}</code> · task <code>{task.task_id}</code>"[:1024],
                       delete_after=True)


# ------------------------------------------------------------------ restart → resume
async def boot_notice(app) -> int:
    """Batches a restart cut short → "Resume?" to their admin (finished shifts are kept). Returns how many."""
    from bot.ui.keyboards import btn, cb, kb
    try:
        docs = await app.repo.batches.find({"status": "running"})
    except Exception as e:
        log.warning("batch boot notice: %r", e)
        return 0
    n = 0
    for doc in docs:
        bid, uid, chat = doc["_id"], doc.get("user_id"), doc.get("chat_id")
        if not (uid and chat and doc.get("shifts")):
            continue
        done = len(doc.get("results") or {})
        try:
            await app.sender.send_message(
                chat, f"⏸ <b>Batch interrupted by a restart</b>\n<b>{html.escape(doc.get('title') or '')}</b> · {done}/"
                      f"{len(doc['shifts'])} shifts done.\nResume the rest?",
                reply_markup=kb([btn("▶️ Resume", cb("bat", uid, bid, "res"), style="success"),
                                 btn("✖️ Drop it", cb("bat", uid, bid, "drop"), style="danger")]))
            n += 1
        except Exception as e:
            log.info("batch resume notice to %s failed: %r", chat, e)
    return n
