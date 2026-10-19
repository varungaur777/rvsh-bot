"""/db — the owner's database panel (spec §9, §23).

Depends on: ``db.repo`` (collections, Mongo + JSON mirror, LRU), ``uploader`` (exports to dump), ``handlers.waits``.
Used by: ``handlers/__init__.py``, the Admin Panel.

- Home: Mongo state, JSON writes waiting, outbox, and every collection with its document count; **sizes** (owner
  2026-10-19: "db command mai db ka size bhi dikhe"): Mongo data + indexes (share of the Atlas free 512 MB) and on
  disk (``dbStats``), the JSON mirror folder (bytes, files; walked off the loop, kept a minute), and every
  collection's size, biggest first, in an expandable quote (Mongo's ``$collStats``, else the mirror's folders).
- A collection: newest documents first (10 per page) → tap one to see it (shortened when long; `📎` sends the
  whole one as a ``.json`` file), `🗑 Delete` → "Sure?" (both stores, cache dropped); `🔎 Open by ID`;
  `📤 Export to dump`. Home: `📦 Export everything`. An export is a zip laid out like ``_db`` — a folder per
  collection, one JSON file per document (owner 2026-10-13) — streamed, never a whole collection in RAM.
- Everything happens on **one** panel message, edited in place (owner 2026-10-13: "db panel replicate krta rhta
  hai"); only `📎` sends a new message (the file).
- `🧨 Delete all` (a collection; owner 2026-10-13: "db mai koi bhi pannel mai delete kr pau, jaise quiz ka db pura
  delete krna ho kar pau") → a warning with the count → `📤 Back up to dump, then delete` (the export below first; a
  failed backup deletes nothing) or `🗑 Delete without a backup` → every document from Mongo and the JSON mirror.
  ``quizzes`` takes ``quiz_stats`` (their leaderboards) with it; ``settings`` → the defaults come back at the next
  restart. Refused while Mongo is down.
- `🔁 Rebuild JSON from Mongo` (Mongo healthy only): every collection streamed from Mongo into the JSON mirror in
  batches, then the cache is dropped — for a lost or damaged ``_db`` folder.
Documents can hold personal data (names, roll numbers): this panel is owner-only, like the spec says.

Callback data: ``db:<owner>:<collection or "">:<arg>``.
"""

from __future__ import annotations

import asyncio
import html
import json
import logging
import os
import time
import zipfile
from pathlib import Path

from bot.core.access import guard
from bot.db.repo import COLLECTIONS
from bot.handlers.waits import WAITERS
from bot.ui.keyboards import btn, cb, kb, parse_cb
from bot.ui.progress import human_bytes
from bot.ui.texts import t

log = logging.getLogger(__name__)
PAGE = 10
_INLINE_MAX = 3200


def _dump_json(doc) -> str:
    return json.dumps(doc, ensure_ascii=False, indent=1, default=str)


ATLAS_FREE_MB = 512  # Atlas free tier (M0) storage limit
_FOLDER_TTL_S = 60.0
_folder_cache: dict[str, tuple[float, tuple]] = {}


def _folder_sizes(root: Path) -> tuple[int, int, dict[str, int]]:
    """The JSON mirror on disk: (bytes, files, {collection folder: bytes}) — one walk."""
    total, files, per = 0, 0, {}
    stack = [(root, None)]
    while stack:
        folder, top = stack.pop()
        try:
            entries = list(os.scandir(folder))
        except OSError:
            continue
        for e in entries:
            try:
                if e.is_dir(follow_symlinks=False):
                    stack.append((e.path, top or e.name))
                elif e.is_file(follow_symlinks=False):
                    n = e.stat(follow_symlinks=False).st_size
                    total, files = total + n, files + 1
                    if top:
                        per[top] = per.get(top, 0) + n
            except OSError:
                continue
    return total, files, per


async def _json_sizes(root: Path) -> tuple[int, int, dict[str, int]]:
    """``_folder_sizes`` off the event loop, remembered for a minute (a big mirror has many files)."""
    key, now = str(root), time.monotonic()
    hit = _folder_cache.get(key)
    if hit and now - hit[0] < _FOLDER_TTL_S:
        return hit[1]
    got = await asyncio.to_thread(_folder_sizes, root)
    _folder_cache[key] = (now, got)
    return got


async def sizes_block(app) -> list[str]:
    """Lines for the panel (owner 2026-10-19: "db command mai db ka size bhi dikhe"): Mongo's size (data +
    indexes, vs the Atlas free 512 MB) and on disk, the JSON mirror's size, and the biggest collections."""
    repo, lines = app.repo, []
    per: dict[str, int] = {}
    src = "Mongo"
    if repo.mongo_ok:
        try:
            s = await repo.mongo.sizes(list(COLLECTIONS))
            used = s["data"] + s["indexes"]
            lines.append(f"💾 Mongo size: <b>{human_bytes(used)}</b> (data {human_bytes(s['data'])} + indexes "
                         f"{human_bytes(s['indexes'])}) · {used / (ATLAS_FREE_MB * 1024 * 1024):.1%} of the Atlas free "
                         f"{ATLAS_FREE_MB} MB · on disk {human_bytes(s['storage'])}")
            per = {c: v["data"] + v["indexes"] for c, v in s["colls"].items()}
        except Exception as e:  # sizes are a nice-to-have: the panel works without them
            log.info("db sizes: %r", e)
            lines.append("💾 Mongo size: <i>could not be read</i>")
    try:
        total, files, folders = await _json_sizes(repo.json.root)
        lines.append(f"📁 JSON mirror: <b>{human_bytes(total)}</b> · {files:,} files")
        if not per:
            per, src = {c: n for c, n in folders.items() if c in COLLECTIONS}, "JSON mirror"
    except Exception as e:
        log.info("json mirror size: %r", e)
    if per:
        top = sorted(per.items(), key=lambda x: -x[1])
        rows = "\n".join(f"{c} · {human_bytes(n)}" for c, n in top)
        lines.append(f"📊 <b>By collection</b> <i>({src})</i>\n<blockquote expandable>{rows}</blockquote>")
    return lines


async def home(app, uid: int):
    repo = app.repo
    counts = await asyncio.gather(*(repo.collection(c).count() for c in COLLECTIONS), return_exceptions=True)
    mongo = "✅ connected" if repo.mongo_ok else ("⚠️ down — using the JSON mirror" if repo.mongo is not None else "— not configured")
    outbox = repo.outbox.size() if getattr(repo, "outbox", None) else 0
    lines = [f"🍃 Mongo: <b>{mongo}</b>", f"📝 JSON writes waiting: <b>{repo.json.pending_count}</b>  ·  "
             f"outbox: <b>{human_bytes(outbox)}</b>"]
    size = await sizes_block(app)
    by_coll = size.pop() if size and size[-1].startswith("📊") else ""
    text = ("🗄 <b>Database</b>\n<blockquote>" + "\n".join(lines + size) + "</blockquote>\n"
            + (by_coll + "\n" if by_coll else "") + "Tap a collection to look inside.")
    buttons = [btn(f"{c} · {n:,}" if isinstance(n, int) else f"{c} · ?", cb("db", uid, c, "open"))
               for c, n in zip(COLLECTIONS, counts)]
    rows = [buttons[i:i + 2] for i in range(0, len(buttons), 2)]
    rows.append([btn("📦 Export everything to dump", cb("db", uid, "", "exportall"))])
    if repo.mongo_ok:
        rows.append([btn("🔁 Rebuild JSON from Mongo", cb("db", uid, "", "rebuild"))])
    return text, kb(*rows, [btn(t("btn_home"), cb("nav", uid, "home"))])


async def coll_screen(app, uid: int, coll: str, page: int = 0):
    c = app.repo.collection(coll)
    docs = await c.find({}, sort=[("updated_at", -1)], limit=(page + 1) * PAGE + 1, projection={"_id": 1, "updated_at": 1})
    more = len(docs) > (page + 1) * PAGE
    docs = docs[page * PAGE:(page + 1) * PAGE]
    sess = app.sessions.get(uid)
    sess.data["db_ids"] = [d["_id"] for d in docs]
    total = await c.count()
    text = (f"🗄 <b>{coll}</b> · {total:,} document{'s' if total != 1 else ''}\n<i>Newest first.</i>"
            if total else f"🗄 <b>{coll}</b>\nEmpty.")
    rows = [[btn(f"📄 {str(d['_id'])[:40]}", cb("db", uid, coll, f"d_{i}"))] for i, d in enumerate(docs)]
    nav = []
    if page:
        nav.append(btn("◀️", cb("db", uid, coll, f"p_{page - 1}")))
    if more:
        nav.append(btn("▶️", cb("db", uid, coll, f"p_{page + 1}")))
    tools = [btn("🔎 Open by ID", cb("db", uid, coll, "find"))]
    if total:
        tools.append(btn("📤 Export to dump", cb("db", uid, coll, "export")))
    danger = [btn(f"🧨 Delete all {total:,}", cb("db", uid, coll, "wipe"), style="danger")] if total else []
    return text, kb(*rows, nav, tools, danger, [btn(t("btn_back"), cb("db", uid, "", "home"))])


WIPE_WITH = {"quizzes": ("quiz_stats",)}  # deleted together (a leaderboard without its quiz is useless)
WIPE_NOTE = {"quizzes": "Their leaderboards (<b>quiz_stats</b>) go too. Old quiz links will say \"deleted\"; cards stay in "
                        "the image dump.",
             "settings": "Every setting goes back to its default (after /restart).",
             "users": "Everyone becomes a new user again (their exam / quiz history is gone).",
             "exams": "Exam data references go; the next sheet / JSON builds them again (uploads again).",
             "running_quizzes": "Only the restart notices of running quizzes."}


async def wipe_screen(app, uid: int, coll: str):
    n = await app.repo.collection(coll).count()
    extra = WIPE_WITH.get(coll, ())
    more = ""
    for c in extra:
        k = await app.repo.collection(c).count()
        more += f" + <b>{c}</b> ({k:,})"
    text = (f"🧨 <b>Delete ALL of {coll}</b> — {n:,} document{'s' if n != 1 else ''}{more}\n"
            "From Mongo and the JSON mirror. <b>This can't be undone.</b>\n"
            + (f"<i>{WIPE_NOTE[coll]}</i>\n" if coll in WIPE_NOTE else "")
            + "\n📤 <b>Back up first</b> = the export zip goes to the dump (one file per document), then the delete.")
    return text, kb([btn("📤 Back up to dump, then delete", cb("db", uid, coll, "wipeb"), style="primary")],
                    [btn("🗑 Delete without a backup", cb("db", uid, coll, "wipey"), style="danger")],
                    [btn(t("btn_cancel"), cb("db", uid, coll, "p_0"))])


async def wipe(app, coll: str, backup: bool) -> str:
    """Delete every document of ``coll`` (+ ``WIPE_WITH``); a backup export first when asked — a failed backup
    deletes nothing. Returns the note for the panel."""
    from bot.db.mongo_backend import MongoUnavailable
    colls = [coll, *WIPE_WITH.get(coll, ())]
    link = None
    if backup:
        link, _n = await export(app, colls)
        if not link:
            return f"⚠️ The backup of <b>{coll}</b> failed (details in dump) — <b>nothing was deleted</b>."
    try:
        counts = [(c, await app.repo.collection(c).delete_all()) for c in colls]
    except MongoUnavailable:
        return "⚠️ Mongo is down — <b>nothing was deleted</b>. Try again when it is connected (see /db)."
    if coll == "settings":
        app.dump.notify("⚙️ Settings collection emptied — the defaults apply after /restart.")
    done = " · ".join(f"<b>{c}</b> {n:,}" for c, n in counts)
    return (f"🧨 Deleted all: {done}" + (f"\n📤 Backup: <a href=\"{link}\">zip in dump</a>" if link else "\n<i>No backup.</i>"))


async def doc_screen(app, uid: int, coll: str, doc_id):
    """A document on the panel message: inline (shortened when long — `📎` sends the whole one as a file)."""
    back = [btn(t("btn_back"), cb("db", uid, coll, "p_0"))]
    doc = await app.repo.collection(coll).get(doc_id)
    if doc is None:
        return f"No document <code>{html.escape(str(doc_id))}</code> in <b>{coll}</b>.", kb(back)
    app.sessions.get(uid).data["db_doc"] = (coll, doc_id)
    body = _dump_json(doc)
    text = f"📄 <b>{coll}</b> / <code>{html.escape(str(doc_id))}</code>\n"
    rows = []
    if len(body) > _INLINE_MAX:
        text += (f"<pre>{html.escape(body[:_INLINE_MAX])}\n…</pre>\n<i>Shortened — the whole document is "
                 f"{human_bytes(len(body.encode()))}.</i>")
        rows.append([btn("📎 Send the whole document as a file", cb("db", uid, coll, "file"))])
    else:
        text += f"<pre>{html.escape(body)}</pre>"
    if coll == "exams":  # the page links are kept per candidate in exam_urls, tied by exam_key
        n = await app.repo.exam_urls.count({"exam_key": doc_id})
        if n:
            rows.append([btn(f"🔗 Links of this exam · {n}", cb("db", uid, coll, "urls"))])
    elif coll == "exam_urls" and doc.get("exam_key"):
        rows.append([btn("📘 Open its exam", cb("db", uid, coll, "exam"))])
    rows.append([btn("🗑 Delete", cb("db", uid, coll, "del"), style="danger")])
    return text, kb(*rows, back)


async def urls_screen(app, uid: int, exam_key: str):
    """Every page link registered for one exam (``exam_urls`` with this ``exam_key``) — owner-only, personal data."""
    docs = await app.repo.exam_urls.find({"exam_key": exam_key}, sort=[("first_seen", 1)], limit=40)
    lines = [f"{i}. <code>{html.escape(d.get('url', ''))}</code> · {html.escape(d.get('status') or '?')}"
             for i, d in enumerate(docs, 1)]
    text = (f"🔗 <b>Links of</b> <code>{html.escape(str(exam_key))}</code> · {len(docs)}\n"
            "<i>Each link is one candidate's answer sheet, so it is kept in <b>exam_urls</b> (one file per link, "
            "with its exam_key), not inside the exam itself.</i>\n\n" + "\n".join(lines))
    return text[:4000], kb([btn(t("btn_back"), cb("db", uid, "exams", "back"))])


async def _send_doc_file(app, chat_id: int, coll: str, doc_id) -> None:
    doc = await app.repo.collection(coll).get(doc_id)
    if doc is None:
        return
    path = app.config.download_root / "tasks" / f"db_{coll}_{int(time.time())}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        await asyncio.to_thread(path.write_text, _dump_json(doc), "utf-8")
        await app.sender.send_document(chat_id, str(path), caption=f"📄 <b>{coll}</b> / <code>{html.escape(str(doc_id))}</code>")
    finally:
        path.unlink(missing_ok=True)


@guard("owner", force_sub=False, private_only=True)
async def db_command(client, message) -> None:
    text, markup = await home(client.app, message.from_user.id)
    await client.app.sender.send_message(message.chat.id, text, reply_markup=markup)


@guard("owner", force_sub=False)
async def on_button(client, query) -> None:
    app, uid = client.app, query.from_user.id
    data = parse_cb(query.data)
    coll, arg = data.obj, data.arg
    chat_id, msg_id = query.message.chat.id, query.message.id
    if coll and coll not in COLLECTIONS:
        return await app.sender.answer(query.id)
    sess = app.sessions.get(uid)
    if arg == "home":
        await app.sender.answer(query.id)
        text, markup = await home(app, uid)
        return await app.sender.edit_text(chat_id, msg_id, text, reply_markup=markup)
    if arg == "open" or arg.startswith("p_"):
        await app.sender.answer(query.id)
        text, markup = await coll_screen(app, uid, coll, int(arg[2:]) if arg.startswith("p_") else 0)
        return await app.sender.edit_text(chat_id, msg_id, text, reply_markup=markup)
    if arg.startswith("d_"):
        ids = sess.data.get("db_ids") or []
        i = int(arg[2:])
        if i >= len(ids):
            return await app.sender.answer(query.id, "This list has expired — open the collection again.", show_alert=True)
        await app.sender.answer(query.id)
        text, markup = await doc_screen(app, uid, coll, ids[i])
        return await app.sender.edit_text(chat_id, msg_id, text, reply_markup=markup)
    if arg in ("urls", "exam", "back"):
        target = sess.data.get("db_doc")
        if not target:
            return await app.sender.answer(query.id, "Open the document again first.", show_alert=True)
        await app.sender.answer(query.id)
        if arg == "urls" and target[0] == "exams":
            text, markup = await urls_screen(app, uid, target[1])
        elif arg == "exam" and target[0] == "exam_urls":
            link = await app.repo.exam_urls.get(target[1], copy=False) or {}
            text, markup = await doc_screen(app, uid, "exams", link.get("exam_key"))
        else:
            text, markup = await doc_screen(app, uid, *target)
        return await app.sender.edit_text(chat_id, msg_id, text, reply_markup=markup)
    if arg == "file":
        target = sess.data.get("db_doc")
        if not target or target[0] != coll:
            return await app.sender.answer(query.id, "Open the document again first.", show_alert=True)
        await app.sender.answer(query.id, "Sending…")
        return await _send_doc_file(app, chat_id, coll, target[1])
    if arg == "find":
        sess.step, sess.data["db_coll"], sess.data["db_msg"] = "db:find", coll, msg_id
        await app.sender.answer(query.id)
        return await app.sender.edit_text(chat_id, msg_id, f"🔎 Send the document ID in <b>{coll}</b> (e.g. a user ID or "
                                          "an exam key).", reply_markup=kb([btn(t("btn_back"), cb("db", uid, coll, "p_0"))]))
    if arg == "del":
        target = sess.data.get("db_doc")
        if not target or target[0] != coll:
            return await app.sender.answer(query.id, "Open the document again first.", show_alert=True)
        await app.sender.answer(query.id)
        return await app.sender.edit_text(
            chat_id, msg_id, f"🗑 Delete <b>{coll}</b> / <code>{html.escape(str(target[1]))}</code> from Mongo and the JSON mirror?\n"
            "<i>This can't be undone (export the collection first if unsure).</i>",
            reply_markup=kb([btn("🗑 Yes, delete it", cb("db", uid, coll, "dely"), style="danger")],
                            [btn(t("btn_cancel"), cb("db", uid, coll, "p_0"))]))
    if arg == "dely":
        target = sess.data.pop("db_doc", None)
        if not target or target[0] != coll:
            return await app.sender.answer(query.id, "Nothing to delete.", show_alert=True)
        await app.repo.collection(coll).delete(target[1])
        app.dump.notify(f"🗄 Deleted <b>{coll}</b> / <code>{html.escape(str(target[1]))}</code> by owner")
        await app.sender.answer(query.id, "Deleted.")
        text, markup = await coll_screen(app, uid, coll)
        return await app.sender.edit_text(chat_id, msg_id, text, reply_markup=markup)
    if arg == "wipe" and coll:
        await app.sender.answer(query.id)
        text, markup = await wipe_screen(app, uid, coll)
        return await app.sender.edit_text(chat_id, msg_id, text, reply_markup=markup)
    if arg in ("wipeb", "wipey") and coll:
        await app.sender.answer(query.id, "Deleting…")
        await app.sender.edit_text(chat_id, msg_id, f"🧨 Deleting all of <b>{coll}</b>"
                                   + (" (backing up first)…" if arg == "wipeb" else "…"))
        try:
            note = await wipe(app, coll, backup=arg == "wipeb")
        except Exception as e:
            note = t("error_generic", error_id=app.dump.report_error(e, f"db.wipe {coll}"))
        app.dump.notify(f"🗄 /db {note} · by owner")
        return await app.sender.edit_text(chat_id, msg_id, note, reply_markup=kb([btn(t("btn_back"), cb("db", uid, coll, "p_0"))]))
    if arg in ("export", "exportall"):
        colls, back = ([coll], cb("db", uid, coll, "p_0")) if coll else (list(COLLECTIONS), cb("db", uid, "", "home"))
        name = coll or "the whole database"
        await app.sender.answer(query.id, "Exporting…")
        await app.sender.edit_text(chat_id, msg_id, f"📤 Exporting <b>{name}</b>…")
        link, n = await export(app, colls)
        note = (f"📤 <b>{name}</b>: {n:,} documents, one file each → <a href=\"{link}\">zip in dump</a>" if link
                else f"⚠️ Export of {name} failed (details in dump).")
        if link:
            app.dump.notify(note)
        return await app.sender.edit_text(chat_id, msg_id, note, reply_markup=kb([btn(t("btn_back"), back)]))
    if arg == "rebuild":
        if not app.repo.mongo_ok:
            return await app.sender.answer(query.id, "Mongo is not connected.", show_alert=True)
        await app.sender.answer(query.id)
        return await app.sender.edit_text(
            chat_id, msg_id, "🔁 <b>Rebuild JSON from Mongo</b>\nEvery collection is copied from Mongo into the JSON mirror "
            "(<code>_db</code>), overwriting the files there. Mongo itself is not changed.",
            reply_markup=kb([btn("🔁 Yes, rebuild", cb("db", uid, "", "rebuildy"), style="danger")],
                            [btn(t("btn_back"), cb("db", uid, "", "home"))]))
    if arg == "rebuildy":
        await app.sender.answer(query.id, "Rebuilding…")
        await app.sender.edit_text(chat_id, msg_id, "🔁 Rebuilding the JSON mirror from Mongo…")
        try:
            n = await rebuild_json(app)
            note = f"✅ <b>JSON mirror rebuilt</b> · {n:,} documents copied from Mongo."
        except Exception as e:
            note = t("error_generic", error_id=app.dump.report_error(e, "db.rebuild"))
        app.dump.notify(note)
        return await app.sender.edit_text(chat_id, msg_id, note, reply_markup=kb([btn(t("btn_back"), cb("db", uid, "", "home"))]))
    await app.sender.answer(query.id)


async def _take_id(app, message, sess) -> None:
    coll = sess.data.get("db_coll")
    raw = (message.text or "").strip()
    sess.step = ""
    doc_id = int(raw) if raw.lstrip("-").isdigit() else raw
    if await app.repo.collection(coll).get(doc_id, copy=False) is None and isinstance(doc_id, int):
        doc_id = raw  # an exam key or quiz id made of digits
    text, markup = await doc_screen(app, message.from_user.id, coll, doc_id)
    panel = sess.data.pop("db_msg", None)
    if panel:  # the answer goes on the panel message; the typed ID is removed
        try:
            await app.sender.delete(message.chat.id, [message.id])
        except Exception:
            pass
        return await app.sender.edit_text(message.chat.id, panel, text, reply_markup=markup)
    await app.sender.send_message(message.chat.id, text, reply_markup=markup)


async def export(app, colls: list[str]) -> tuple[str | None, int]:
    """Collections → one zip laid out like ``_db`` (a folder per collection, **one JSON file per document**),
    uploaded to the dump; (link, documents). Streamed in batches (never a whole collection in RAM) — from Mongo
    when it is up, else from the JSON mirror."""
    base = app.config.download_root / "tasks"
    base.mkdir(parents=True, exist_ok=True)
    name = colls[0] if len(colls) == 1 else "all"
    zp = base / f"db_{name}_{time.strftime('%Y%m%d_%H%M%S')}.zip"
    n = 0
    try:
        await asyncio.to_thread(_zip_add, zp, [], "w")
        for coll in colls:
            batch: list[tuple[str, bytes]] = []
            async for doc in app.repo.collection(coll).stream({}):
                rel = app.repo.json.path_for(coll, doc["_id"]).relative_to(app.repo.json.root).as_posix()
                batch.append((rel, _dump_json(doc).encode()))
                n += 1
                if len(batch) >= 200:  # written off the event loop
                    await asyncio.to_thread(_zip_add, zp, batch)
                    batch = []
            await asyncio.to_thread(_zip_add, zp, batch)
        from bot.core.tgref import private_link
        dump = app.config.dump_channel_id
        what = ", ".join(colls) if len(colls) <= 3 else "all collections"
        _bot, msg = await app.uploads.send_document(dump, str(zp), caption=f"📤 /db export · {what} · {n:,} documents")
        return private_link(dump, msg.id), n
    except Exception as e:
        app.dump.report_error(e, "db.export")
        return None, n
    finally:
        zp.unlink(missing_ok=True)


def _zip_add(out: Path, files: list[tuple[str, bytes]], mode: str = "a") -> None:
    with zipfile.ZipFile(out, mode, zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for rel, data in files:
            z.writestr(rel, data)


async def rebuild_json(app) -> int:
    """Copy every collection from Mongo into the JSON mirror (batches of 500, flushed as it goes)."""
    repo, n = app.repo, 0
    for coll in COLLECTIONS:
        async for doc in repo.mongo.iterate(coll, {}):
            repo.json.put(coll, doc["_id"], doc)
            n += 1
            if n % 500 == 0:
                await repo.json.flush()
        repo.cache.drop_collection(coll)
    await repo.json.flush()
    return n


WAITERS.update({"db:find": _take_id})
