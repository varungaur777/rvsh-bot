"""♻️ Recheck — every saved link and every ``.mht`` in dump read again (owner 2026-10-19: "db, dump ki sare links aur
mht ko recheck krke db mai exams links add kiye ja ske, iska option rkhna" · "Sab, checked bhi" · "kuch indian
specific hote hai unko route krke check krna hota hai, isliye unko bhi recheck kia ja ske" · "jin mht ke caption par
link nhi hai to mht ko download krke usse link nikal le").

1. **Saved links** (``exam_urls`` of the picked exam, or all): every one — checked, dead, ``unreadable`` too — through
   ``linkcheck.check_all``: its dump copy first, else the page once through the bot's fetch route
   (``service.fetch_url``: direct, then the fetch server when the setting says so).
2. **Dump** (whole-registry recheck only): the dump channel is read by message id (``get_messages``, 200 at a time,
   up to a notice posted now); each ``.mht`` / ``.mhtml`` not already in ``exam_mht`` (a link's fetched page — step 1
   read it) is downloaded and parsed: its link is the page URL in the file (else one in the caption), saved with exam,
   date, shift, language and census (``urls.register``; the link's last status kept). A TCS sheet (one page) is also
   remembered as the link's dump copy (``exam_mht``, ``missing=1``: a user's copy, files not counted — used for the
   language and when the link is dead, never instead of a good fetch).
3. **Links written in dump** (owner 2026-10-20: "dump channel mai pde links bhi … vo bhi read krne hai" · ".txt nhi,
   kevel links and mht"): every TCS / EDQT link in a dump message's text or caption (a user's link input, a fetch
   that failed before it was saved) that is not saved yet after steps 1–2 is checked like step 1 (dump copy, else
   the page once) and saved; ``.txt`` files are not read.

The dump is read from message ``dump_from`` (owner 2026-10-20: "isme ye bhi dalo ki konsi message id se search krna
hai dump mai" — from the start, after the last full scan, or a typed id) to the latest; a full scan remembers where
it ended (``settings`` collection, document ``links_scan``: ``{to, at}``).

Then the Hindi / English files and the report. Public API: ``await run(app, task)`` — ``task.data``: ``links``
``[(url, hints)]``, ``slugs`` (None = all), ``title``, ``ui_message_id``, ``scan_dump``, ``dump_from`` (default 1).
``await last_scanned(app) -> int`` — the first message id after the last full scan (0 = none yet).
``parse_message_id(text) -> int | None`` — ``1234`` or a ``t.me/c/…/1234`` link. Cancel (``/cancel``) stops at
once. Depends on: ``exam.linkcheck``, ``exam.urls``, ``core.tgref``. Used by: ``handlers.links``.
"""

from __future__ import annotations

import asyncio
import html
import logging
import re
import shutil
import time

from bot.compute import run_cpu
from bot.core import tgref
from bot.core.errors import BotError, SourceUnavailable
from bot.core.sender import LOG
from bot.exam import linkcheck
from bot.exam import urls as U
from bot.exam.jobs import inspect_sheets
from bot.exam.keys import name_slug
from bot.net.fetcher import detect_kind
from bot.ui.progress import ProgressMessage

log = logging.getLogger(__name__)
MHT_EXT = (".mht", ".mhtml")
STEP = 200
_URL = re.compile(r"https?://[^\s<>\"']+", re.I)


def caption_url(caption: str | None) -> str | None:
    """The first TCS / EDQT link in a dump caption."""
    for m in _URL.finditer(html.unescape(caption or "")):
        u = m.group(0).rstrip(").,;")
        if detect_kind(u) in ("tcs", "edqt"):
            return u
    return None


def mht_ref(msg) -> dict | None:
    """The ``.mht`` document of a dump message (``tgref.from_message``), else None."""
    doc = getattr(msg, "document", None)
    if doc is None or getattr(msg, "empty", False):
        return None
    if not (doc.file_name or "").lower().endswith(MHT_EXT):
        return None
    return tgref.from_message(msg)


async def _known_files(app) -> set[str]:
    """``file_unique_id`` of every page already in ``exam_mht`` (a link's own copy)."""
    out: set[str] = set()
    for d in await app.repo.exam_mht.find({}, projection={"files": 1}):
        out.update(f.get("file_unique_id") for f in (d.get("files") or {}).values() if f.get("file_unique_id"))
    return out


SCAN_DOC = "links_scan"


async def last_scanned(app) -> int:
    """The first dump message id after the last full scan (0 = never scanned)."""
    try:
        return int((await app.repo.settings.get(SCAN_DOC) or {}).get("to") or 0)
    except Exception:
        return 0


def parse_message_id(text: str | None) -> int | None:
    """A dump message id typed by the admin: ``1234``, ``#1234`` or a message link ``https://t.me/c/123/1234``."""
    t = (text or "").strip()
    m = re.fullmatch(r"#?(\d{1,9})", t) or re.search(r"t\.me/(?:c/)?[\w-]+/(\d{1,9})(?:\?\S*)?$", t)
    n = int(m.group(1)) if m else 0
    return n if n > 0 else None


def _readers(app) -> list[tuple[str, object]]:
    """The main bot + every uploader bot in the pool (admins in dump — owner 2026-10-20: "dump scan ka kam image bot
    tokens mai bant do"): each has its own ``GetMessages`` flood limit."""
    out = [("main", app.client)]
    for w in getattr(getattr(app, "uploads", None), "workers", None) or []:
        out.append((w.name, w.client))
    return out


def message_links(msg) -> list[tuple[str, dict]]:
    """``[(url, hints)]`` — the TCS / EDQT links in a dump message's text and caption (``urls.read_links``)."""
    text = "\n".join(x for x in (getattr(msg, "text", None), getattr(msg, "caption", None)) if x)
    return U.read_links(html.unescape(text)) if text else []


class DumpScan:
    """What ``dump_files`` found: ``files`` ``[(ref, caption link)]`` (the ``.mht`` files), ``links`` ``[(url, hints)]``
    (links written in messages / captions, once per canonical URL), both in message order."""

    def __init__(self, files: list, links: list) -> None:
        self.files, self.links = files, links


async def dump_files(app, show, start: int = 1) -> DumpScan:
    """Every ``.mht`` in dump not already a link's saved copy (once per file) + every link written in a message or
    caption, in message order. The id range is read in blocks of 200 shared by the main bot and the uploader bots: a bot told to wait
    (``FloodWait``) hands its block back and rests, another one takes it; an uploader bot that cannot read the dump
    stops (its block goes back). A file read by an uploader bot is downloaded by the main bot (``file_id`` is per bot:
    cleared, ``tgref.download`` re-reads that one message)."""
    from pyrogram.errors import FloodWait
    chat = app.config.dump_channel_id
    probe = await app.sender.send_message(chat, "♻️ <b>Recheck</b> — reading the .mht files in dump…", priority=LOG)
    known = await _known_files(app)
    blocks: asyncio.Queue = asyncio.Queue()
    for first in range(max(1, int(start or 1)), probe.id, STEP):
        blocks.put_nowait(list(range(first, min(first + STEP, probe.id))))
    total, read = blocks.qsize(), {"n": 0}
    found: dict[str, tuple[int, dict, str | None]] = {}
    written: dict[str, tuple[int, str, dict]] = {}
    readers = _readers(app)

    async def reader(name: str, client) -> None:
        while True:
            try:
                ids = blocks.get_nowait()
            except asyncio.QueueEmpty:
                return
            try:
                msgs = await client.get_messages(chat, ids)
            except asyncio.CancelledError:
                raise
            except FloodWait as e:
                blocks.put_nowait(ids)
                log.info("recheck: %s waits %ss (GetMessages) — its block goes to another bot", name, e.value)
                await asyncio.sleep(max(1, int(e.value or 0)))
                continue
            except Exception as e:
                blocks.put_nowait(ids)
                if client is app.client:
                    raise
                log.warning("recheck: %s cannot read dump (%r) — left out of the scan", name, e)
                return
            for msg in msgs if isinstance(msgs, list) else [msgs]:
                if not msg or getattr(msg, "empty", False):
                    continue
                for url, hints in message_links(msg):
                    key = U.canonical(url)
                    if key not in written or msg.id < written[key][0]:
                        written[key] = (msg.id, url, hints)
                ref = mht_ref(msg)
                if not ref or ref["file_unique_id"] in known:
                    continue
                if client is not app.client:
                    ref["file_id"] = ""
                old = found.get(ref["file_unique_id"])
                if old is None or msg.id < old[0]:  # a file forwarded twice: once (its first post)
                    found[ref["file_unique_id"]] = (msg.id, ref, caption_url(getattr(msg, "caption", None)))
            read["n"] += 1
            show(f"🗂 Reading dump {read['n']}/{total} blocks of {STEP} · {len(readers)} bot"
                 f"{'s' if len(readers) != 1 else ''} · {len(found)} .mht · {len(written)} links found")

    await asyncio.gather(*(reader(n, c) for n, c in readers))
    if not blocks.empty():  # every reader stopped (only the main bot raises): never a silent half scan
        raise RuntimeError(f"dump scan: {blocks.qsize()} blocks unread")
    try:  # the next scan can start here ("after the last scan")
        await app.repo.settings.update(SCAN_DOC, {"$set": {"to": probe.id, "from": start,
                                                           "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}})
    except Exception as e:
        log.warning("recheck: scan end not saved: %r", e)
    return DumpScan([(ref, cap) for _id, ref, cap in sorted(found.values(), key=lambda x: x[0])],
                    [(url, hints) for _id, url, hints in sorted(written.values(), key=lambda x: x[0])])


async def check_file(app, task, i: int, ref: dict, cap_url: str | None) -> dict:
    """One dump ``.mht``: downloaded, parsed, its link registered. A row like ``linkcheck``'s (``error`` when the file
    is not a sheet or has no link)."""
    row: dict = {"url": cap_url or ref.get("name") or "?", "file": ref.get("name"), "message_id": ref.get("message_id")}
    folder = task.workdir / "dump" / f"{i:05d}"
    try:
        got = await tgref.download(app, dict(ref), folder / "dl")
        await asyncio.to_thread(got.replace, folder / "p00.mht")
        meta, summary = await run_cpu(app, "parse", task.task_id, inspect_sheets, str(folder), {})
    except asyncio.CancelledError:
        raise
    except (BotError, SourceUnavailable) as e:
        row["error"] = "not_exam" if getattr(e, "key", "") in ("no_questions", "unknown_format", "parse_error") else \
            (getattr(e, "reason", None) or getattr(e, "key", None) or "error")
        return row
    except Exception as e:
        log.warning("recheck: dump file %s failed: %r", ref.get("name"), e)
        row["error"] = "error"
        return row
    finally:
        await asyncio.to_thread(shutil.rmtree, folder, True)
    url = summary.get("url") or meta.url or cap_url
    if not url or detect_kind(url) not in ("tcs", "edqt"):
        row["error"] = "no_link"
        return row
    langs, mix = U.sheet_langs(summary)
    status = await linkcheck._status(app, url) or "ok"
    await U.register(app.repo, url, meta, task.user_id, status, langs, mix)
    if (summary.get("source") or detect_kind(url)) == "tcs":  # one page: the link's copy (EDQT: one section of many)
        await U.add_mht_page(app.repo, url, {**ref, "link": tgref.private_link(ref["chat_id"], ref["message_id"])},
                             0, 1, task.user_id, missing=1)
    row.update(url=U.canonical(url), langs=langs, mix=mix, exam_name_slug=name_slug(meta.exam_name), status=status)
    return row


async def run(app, task) -> None:
    d = task.data
    items: list = d["links"]
    progress = ProgressMessage(app.sender, task.chat_id, interval_s=app.settings.get("edit_interval_s"), task=task)
    progress.attach(d["ui_message_id"])
    try:
        st = {"done": 0, "ok": 0, "bad": 0, "files": 0, "fok": 0, "fbad": 0, "dl": 0, "dok": 0, "dbad": 0}
        started = time.monotonic()

        def show(extra: str = "") -> None:
            progress.set(f"♻️ <b>Recheck</b> · links {st['done']}/{len(items)} · ✅ {st['ok']} · ❌ {st['bad']}"
                         + (f"\n🗂 dump .mht {st['fok'] + st['fbad']}/{st['files']} · ✅ {st['fok']} · ❌ {st['fbad']}"
                            if st["files"] else "")
                         + (f"\n🔗 dump links {st['dok'] + st['dbad']}/{st['dl']} · ✅ {st['dok']} · ❌ {st['dbad']}"
                            if st["dl"] else "") + (f"\n<i>{extra}</i>" if extra else "")
                         + f"\n<i>⏱ {int(time.monotonic() - started)} s</i>")

        def done(row: dict) -> None:
            st["done"] += 1
            st["bad" if row.get("error") else "ok"] += 1
            show("Saved links: the dump copy first, else the page once")

        show("Saved links: the dump copy first, else the page once")
        rows = await linkcheck.check_all(app, task, items, done)
        failed = [r for r in rows if r and r.get("error")]
        file_rows: list[dict] = []
        if d.get("scan_dump"):
            scan = await dump_files(app, show, int(d.get("dump_from") or 1))
            found = scan.files
            st["files"] = len(found)
            gate = asyncio.Semaphore(max(1, int(app.settings.get("batch.fetch_concurrency") or 2)))

            async def one(i: int, ref: dict, cap: str | None) -> None:
                async with gate:
                    r = await check_file(app, task, i, ref, cap)
                    file_rows.append(r)
                    st["fbad" if r.get("error") else "fok"] += 1
                    show("Dump .mht files: downloaded, read, their links saved")

            await asyncio.gather(*(one(i, ref, cap) for i, (ref, cap) in enumerate(found)))
            # links written in dump that are still not saved (a link whose fetch failed back then)
            saved = {x.get("url") for x in await app.repo.exam_urls.find({}, projection={"url": 1})}
            new = [(u, h) for u, h in scan.links if U.canonical(u) not in saved]
            st["dl"] = len(new)

            def done_dump(row: dict) -> None:
                st["dbad" if row.get("error") else "dok"] += 1
                show("Links written in dump, not saved yet: the dump copy first, else the page once")

            drows = await linkcheck.check_all(app, task, new, done_dump)
            failed += [r for r in drows if r and r.get("error")]
        slugs = d.get("slugs") or None
        per_lang = await U.links_by_lang(app.repo, slugs)
        title = d.get("title") or "Links"
        progress.set("📤 <b>Sending the files…</b>")
        note = (f"♻️ {len(items)} saved link{'s' if len(items) != 1 else ''} rechecked · ✅ {st['ok']} · ❌ {st['bad']}"
                + (f" · dump .mht: {st['files']} read, ✅ {st['fok']} · ❌ {st['fbad']} (not a sheet / no link)"
                    f" · new links written in dump: {st['dl']}, ✅ {st['dok']} · ❌ {st['dbad']}"
                   if d.get("scan_dump") else ""))
        n = await linkcheck.send_files(app, task.chat_id, task.user_id, per_lang, title, failed=failed, note=note)
        await progress.finish(f"✅ <b>Recheck done</b> — {html.escape(note)}" + ("" if n else "\n<i>No working link to list.</i>"))
        app.dump.notify(f"♻️ <b>Links rechecked</b> · {html.escape(title)} · {html.escape(note)} · 👤 <code>{task.user_id}</code>")
        bad_files = [r for r in file_rows if r.get("error")]
        if bad_files:
            log.info("recheck: %d dump files skipped (dump message id=reason): %s", len(bad_files),
                     ", ".join(f"{r.get('message_id')}={r['error']}" for r in bad_files))
    finally:
        progress.close()
