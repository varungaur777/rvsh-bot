"""Exam links per language — ``/links`` (spec §14.6, owner 2026-10-19: "tcs and edqt exam link extract language wise
krna hai, hindi and english alag, mai aur links dunga tcs ke, edqt k abhi nhi mil skte, tcs vale db mai bhi hai").

A TCS sheet holds only the candidate's own language (an EDQT link fetched with both versions holds both), so a
shift needs one Hindi-medium and one English-medium link to give both versions. ``exam_urls.langs`` remembers each
link's language (``exam.urls.register``); links saved before that are **checked** here: the sheet's ``.mht`` saved in
dump is read when there is a complete copy (no request to the exam site), else the page is opened once (the page
alone — no images, CSS or other-language postbacks: ``service.fetch_url(light=True)``); it is parsed in the pool
(``exam.jobs.inspect_sheets``) and registered again with its exam, date, shift, language and **census** (questions
per language, ``schema.lang_census`` — owner 2026-10-19: Bengali sheets had been filed as English; RRB sheets mix
Hindi and English). A dead link keeps its place with the reason as ``status``; one with no saved copy either is
marked ``unreadable`` (its language cannot be read: never in the files, never checked again — owner 2026-10-19).
A dump copy that cannot be parsed falls back to the page.

Languages in the files (owner 2026-10-19): Hindi and English only — another language's link is saved (``langs:
["bn"]``) but never listed ("link store to kro other language ka par … bs hindi and english hi lene hai"); a sheet
mostly in one of them goes to that file, a half-and-half one to both, with a ``# mixed: …`` line above it.

Public API
----------
- ``await run_check(app, task)`` — background-lane runner. ``task.data``: ``links`` ``[(url, hints)]``, ``mode``
  ``"filter"`` (a pasted list → files of exactly those links) or ``"exam"`` (saved links of ``slugs`` → the exams'
  files from the registry), ``title``, ``ui_message_id``. Progress ``🔍 Checking links 7/27 · ✅ 6 · ❌ 1``; pages are
  opened ``batch.fetch_concurrency`` (2) at a time (politeness, spec §12.1).
- ``await send_files(app, chat_id, uid, per_lang, title, failed=None, note="") -> int`` — one ``.txt`` per language
  (``… - Hindi.txt``, ``… - English.txt``; lines ``(dd/mm/yyyy) Exam (Shift N): URL``, the ``/batch`` format), a
  ``… - not working.txt`` for failed links; each sent to the admin + a dump copy. Returns the number of files.
- ``rows_by_lang(rows) -> {"hi": [...], "en": [...], "other": [...]}`` — the checked rows, one per exam + shift per
  language; ``other``: rows of another language (left out).
- ``await check_one(app, task, i, url, hints) -> row`` · ``await check_all(app, task, items, on_row=None) -> [row]`` —
  the check itself (also ``/batch``): rows with ``url``,
  ``langs``, exam / date / shift / ``start_hhmm`` — or ``error`` (``reason_text``).

Exceptions → outcome: a link that fails (HTTP error, not a response sheet, unreadable) is listed with its reason,
never stops the others; cancel (``/cancel``) stops at once — nothing is sent.
Depends on: ``net.service``, ``exam.jobs``, ``exam.urls``, ``compute``, ``ui.progress``. Used by: ``handlers.links``.
"""

from __future__ import annotations

import asyncio
import html
import logging
import re
import shutil
import time
from pathlib import Path

from bot.compute import run_cpu
from bot.core.errors import BotError, SourceUnavailable
from bot.exam import urls as U
from bot.exam.jobs import inspect_sheets
from bot.exam.keys import name_slug
from bot.net import service
from bot.net.fetcher import redact
from bot.ui.progress import ProgressMessage

log = logging.getLogger(__name__)
REASONS = {"http_404": "expired (404)", "http_403": "refused (403)", "http_400": "refused (400)",
           "not_exam": "not a response sheet", "network": "site not reachable", "remote": "fetch server failed",
           "unsupported_url": "not a TCS / EDQT link"}


def reason_text(reason: str | None) -> str:
    r = reason or "?"
    if r.startswith("lang_"):  # batch: a sheet in another language (owner 2026-10-19: Hindi and English only)
        return f"{U.ALL_NAMES.get(r[5:], r[5:])} sheet — only Hindi and English are used"
    return REASONS.get(r) or (f"site error ({r[5:]})" if r.startswith("http_") else r.replace("_", " "))


async def _check_one(app, task, i: int, url: str, hints: dict) -> dict:
    """Read one link — its complete dump ``.mht``, else the page only — and its exam / date / shift / language (+ the
    census), register it. A row with ``error`` when
    it failed (a definite failure — expired, refused, not a sheet — is saved as the link's ``status``)."""
    row: dict = {"url": U.canonical(url), "hints": hints}
    folder = task.workdir / "chk" / f"{i:05d}"
    parsed = None
    saved = await _from_dump(app, url, folder)
    if saved:
        try:
            parsed = await run_cpu(app, "parse", task.task_id, inspect_sheets, str(folder), hints or {})
        except BotError as e:  # a broken copy: the page itself
            log.info("links: saved copy of %s not readable (%s) — opening the link", redact(url), e.key)
            saved = False
        finally:
            await asyncio.to_thread(shutil.rmtree, folder, True)
    if parsed is None:
        try:
            pages = await service.fetch_url(app, url, light=True)
        except SourceUnavailable as e:
            row["error"] = e.reason or "network"
            if row["error"] not in ("network", "remote"):  # the site answered: this link no longer works, and with
                # no saved copy its language cannot be read (owner 2026-10-19: counted apart, not in the files)
                await _quiet(U.mark_status(app.repo, url, row["error"], unreadable=True))
            return row

        def write() -> None:
            folder.mkdir(parents=True, exist_ok=True)
            for k, p in enumerate(pages):
                (folder / f"p{k:02d}.mht").write_bytes(p.mht)
        try:
            await asyncio.to_thread(write)
            parsed = await run_cpu(app, "parse", task.task_id, inspect_sheets, str(folder), hints or {})
        except BotError as e:  # ParseError: the page is not a sheet we can read
            row["error"] = "not_exam" if e.key in ("no_questions", "unknown_format", "parse_error") else e.key
            if row["error"] == "not_exam":
                await _quiet(U.mark_status(app.repo, url, "not_exam", unreadable=True))
            return row
        finally:
            await asyncio.to_thread(shutil.rmtree, folder, True)
    meta, summary = parsed
    langs, mix = U.sheet_langs(summary)
    # a sheet read from its dump copy keeps the link's last status (the copy says nothing about the link today)
    status = "ok" if not saved else (await _status(app, url) or "ok")
    await U.register(app.repo, url, meta, task.user_id, status, langs, mix)
    row.update(langs=langs, mix=mix, exam_name=meta.exam_name, exam_name_slug=name_slug(meta.exam_name), date=meta.date,
               shift=meta.shift, time=meta.time, start_hhmm=meta.start_hhmm, exam_code=meta.exam_code, status=status,
               saved=saved)
    return row


async def _from_dump(app, url: str, folder: Path) -> bool:
    """The link's ``.mht`` copy from dump into ``folder`` (``pNN.mht``, every page) — no request to the exam site. A
    copy with image / CSS files missing still holds every question's text, so it serves the language check (a user's
    copy from ``recheck`` is saved as ``missing=1``). False when there is none or it cannot be read (then the page)."""
    from bot.core.tgref import download
    try:
        refs = await U.saved_mht(app.repo, url)
        if not refs:
            return False
        await asyncio.to_thread(folder.mkdir, parents=True, exist_ok=True)
        for k, ref in enumerate(refs):
            got = await download(app, dict(ref), folder / "dl")
            await asyncio.to_thread(got.replace, folder / f"p{k:02d}.mht")
        return True
    except asyncio.CancelledError:
        raise
    except Exception as e:  # a dump copy deleted / unreadable: the page itself
        log.info("links: saved copy of %s unusable (%r) — opening the link", redact(url), e)
        await asyncio.to_thread(shutil.rmtree, folder, True)
        return False


async def _status(app, url: str) -> str | None:
    try:
        docs = await app.repo.exam_urls.find({"url": U.canonical(url)}, projection={"status": 1})
    except Exception:
        return None
    return next((d.get("status") for d in docs if d.get("status")), None)


async def _quiet(coro) -> None:
    try:
        await coro
    except Exception as e:
        log.debug("links: ignored %r", e)


async def check_one(app, task, i: int, url: str, hints: dict) -> dict:
    """One link checked (``i``: a number unique in this task — its scratch folder); an unexpected failure is a row with
    ``error`` too, never an exception."""
    try:
        return await _check_one(app, task, i, url, hints)
    except asyncio.CancelledError:
        raise
    except Exception as e:  # never stops the other links
        log.warning("link check %s failed: %r", redact(url), e)
        return {"url": U.canonical(url), "error": "error", "hints": hints}


async def check_all(app, task, items: list, on_row=None) -> list[dict]:
    """Every ``(url, hints)`` checked once (``_check_one``), ``batch.fetch_concurrency`` at a time; one failing link never
    stops the others. ``on_row(row)`` after each. Rows in the order of ``items``. Also used by ``/batch``."""
    rows: list = [None] * len(items)
    gate = asyncio.Semaphore(max(1, int(app.settings.get("batch.fetch_concurrency") or 2)))

    async def one(i: int, url: str, hints: dict) -> None:
        async with gate:
            row = await check_one(app, task, i, url, hints)
            rows[i] = row
            if on_row is not None:
                on_row(row)

    await asyncio.gather(*(one(i, u, h) for i, (u, h) in enumerate(items)))
    return rows


def rows_by_lang(rows: list[dict]) -> dict[str, list[dict]]:
    """The links checked in this run, one per exam + date + start time per language (as the registry export)."""
    docs = [r for r in rows if not r.get("error") and r.get("langs")]
    out = {lang: U._best_per_shift(docs, lang, per_exam=True) for lang in U.LANG_NAMES}
    out["other"] = [r for r in docs if not set(r["langs"]) & set(U.LANG_NAMES)]
    return out


async def run_check(app, task) -> None:
    items: list = task.data["links"]
    progress = ProgressMessage(app.sender, task.chat_id, interval_s=app.settings.get("edit_interval_s"), task=task)
    progress.attach(task.data["ui_message_id"])
    try:
        total = len(items)
        state = {"done": 0, "ok": 0, "bad": 0}
        started = time.monotonic()
        at_once = max(1, int(app.settings.get("batch.fetch_concurrency") or 2))

        def show() -> None:
            n = state["done"]
            eta = ""
            if 0 < n < total:
                left = (time.monotonic() - started) / n * (total - n)
                eta = f" · ⏳ {int(left // 60)}m {int(left % 60)}s left" if left >= 60 else f" · ⏳ {int(left)}s left"
            progress.set(f"🔍 <b>Checking links</b> {n}/{total} · ✅ {state['ok']} · ❌ {state['bad']}{eta}\n"
                         f"<i>Each page is opened once (the page only), {at_once} at a time.</i>")

        def done(row: dict) -> None:
            state["done"] += 1
            state["bad" if row.get("error") else "ok"] += 1
            show()

        show()
        rows = await check_all(app, task, items, done)
        failed = [r for r in rows if r and r.get("error")]
        title = task.data.get("title") or "Links"
        if task.data.get("mode") == "exam":  # the exams' files from the registry (now with these links' languages)
            slugs = set(task.data.get("slugs") or []) | {r["exam_name_slug"] for r in rows if r and r.get("exam_name_slug")}
            per_lang = await U.links_by_lang(app.repo, sorted(slugs) or None)
        else:
            per_lang = rows_by_lang([r for r in rows if r])
        progress.set("📤 <b>Sending the files…</b>")
        note = f"🔍 {total} link{'s' if total != 1 else ''} checked · ✅ {state['ok']} · ❌ {state['bad']}"
        n = await send_files(app, task.chat_id, task.user_id, per_lang, title, failed=failed, note=note)
        await progress.finish(f"✅ <b>Done</b> — {note}" + ("" if n else "\n<i>No working link to list.</i>"))
        app.dump.notify(f"🔗 <b>Links checked</b> · {html.escape(title)} · {note} · 👤 <code>{task.user_id}</code>")
    finally:
        progress.close()


def _safe(name: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r'[\\/:*?"<>|\r\n\t]+', " ", name)).strip()[:120]


async def send_files(app, chat_id: int, uid: int, per_lang: dict, title: str, failed: list | None = None,
                     note: str = "") -> int:
    """One ``.txt`` per language (+ the failed links) to the admin, each with a dump copy (URLs are candidate-specific:
    admin-only, spec §25). Returns how many files were sent."""
    folder = Path(app.root) / "tmp" / str(uid)
    await asyncio.to_thread(folder.mkdir, parents=True, exist_ok=True)
    files: list[tuple[Path, str, str]] = []
    for lang, name in U.LANG_NAMES.items():
        links = per_lang.get(lang) or []
        if not links:
            continue
        dead = sum(1 for x in links if x.get("status") not in (None, "ok"))
        both = sum(1 for x in links if len(x.get("langs") or []) > 1 and not U.mixed_note(x.get("mix")))
        mixed = sum(1 for x in links if U.mixed_note(x.get("mix")))
        cap = (f"🔗 <b>{html.escape(title)}</b> · 🌐 <b>{name}</b>\n{len(links)} link{'s' if len(links) != 1 else ''} · one "
               f"per shift" + (f" · {both} with both languages" if both else "")
               + (f" · 🔀 {mixed} mixed (Hindi + English questions, marked # mixed)" if mixed else "")
               + (f" · ⚠️ {dead} last seen not working" if dead else ""))
        files.append((folder / f"{_safe(f'Links - {title} - {name}')}.txt", U.links_txt(links), cap))
    if failed:
        lines = [f"# {reason_text(r.get('error'))}: {r['url']}" for r in failed]
        files.append((folder / f"{_safe(f'Links - {title} - not working')}.txt", "\n".join(lines) + "\n",
                      f"❌ <b>{len(failed)} link{'s' if len(failed) != 1 else ''} not working</b> · {html.escape(title)}"))
    other = per_lang.get("other") or []
    if other and files:  # saved, never listed (owner 2026-10-19: Hindi and English only)
        names = sorted({U.ALL_NAMES.get(lg, lg) for x in other for lg in x.get("langs") or []})
        note = (note + "\n" if note else "") + (f"{len(other)} link{'s' if len(other) != 1 else ''} in another language "
                                                f"({', '.join(names)}) left out")
    sent = 0
    for path, text, cap in files:
        await asyncio.to_thread(path.write_text, text, encoding="utf-8")
        caption = cap + (f"\n<i>{html.escape(note)}</i>" if note else "")
        try:
            await app.sender.send_document(chat_id, str(path), caption=caption[:1024])
        except Exception:
            path.unlink(missing_ok=True)
            raise
        sent += 1
        app.dump.send_file(path, f"{caption}\n👤 <code>{uid}</code>"[:1024], delete_after=True)  # the dump copy removes it
    return sent
