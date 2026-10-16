"""Files under ``DOWNLOAD_ROOT`` (spec §23, §25): /ls, /clean (admin), /rm (owner).

Depends on: ``core.access.guard``, ``db.repo`` (LRU invalidation), ``uploader`` (backup to dump), ``ui``.
Used by: ``handlers/__init__.py``, the Admin Panel.

- **/ls** — folders and files with counts and sizes, 10 per page; tap a folder to open it, a file to get it.
  ``_db/`` is 🔒: only the owner opens it. Paths never leave ``DOWNLOAD_ROOT`` (resolved, no traversal).
- **/clean** — shows what it would free (leftover task folders of tasks that no longer exist, stray files in
  ``tasks/``, ``_webcache/``), then `🧹 Clean now`. **Never touches ``_db/``** or a live task's folder.
- **/rm <path>** (owner) — what will be deleted (files, size) → "Sure?". Inside ``_db/``: a backup zip is made
  and uploaded to the dump **first** (failure → nothing deleted), then a **second** confirmation; each button
  works for 60 s. After deleting, the DB's in-memory cache of those collections is dropped and the dump gets the
  report with the backup link. Mongo is not touched (`/db → Rebuild JSON from Mongo` restores the mirror).
"""

from __future__ import annotations

import asyncio
import html
import os
import shutil
import time
import zipfile
from pathlib import Path

from bot.core.access import guard
from bot.ui.keyboards import btn, cb, kb, parse_cb
from bot.ui.progress import human_bytes
from bot.ui.texts import t

PAGE = 10
CONFIRM_S = 60
_MAX_COUNT = 300_000  # stop counting a huge folder (the listing says "≥")


def root_of(app) -> Path:
    return Path(app.config.download_root).resolve()


def safe_path(app, rel: str) -> Path | None:
    """``rel`` inside DOWNLOAD_ROOT (not the root itself), resolved; None for anything outside."""
    root = root_of(app)
    p = (root / (rel or "").strip().strip("/\\")).resolve()
    if p == root or root not in p.parents:
        return None
    return p


def _rel(app, p: Path) -> str:
    return p.relative_to(root_of(app)).as_posix() if p != root_of(app) else ""


def du(path: Path) -> tuple[int, int, bool]:
    """(files, bytes, capped) of a file or folder, without following links."""
    if path.is_file():
        return 1, path.stat().st_size, False
    files = size = 0
    stack = [path]
    while stack:
        try:
            with os.scandir(stack.pop()) as it:
                for e in it:
                    if e.is_dir(follow_symlinks=False):
                        stack.append(Path(e.path))
                    elif e.is_file(follow_symlinks=False):
                        files += 1
                        size += e.stat(follow_symlinks=False).st_size
                        if files >= _MAX_COUNT:
                            return files, size, True
        except OSError:
            continue
    return files, size, False


def _in_db(app, p: Path) -> bool:
    db = root_of(app) / "_db"
    return p == db or db in p.parents


def _count(files: int, size: int, capped: bool) -> str:
    return f"{'≥' if capped else ''}{files:,} file{'s' if files != 1 else ''} · {human_bytes(size)}"


# ================================================================== /ls
def _listing(app, folder: Path) -> list[tuple[Path, bool, int, int, bool]]:
    out = []
    for e in sorted(folder.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower())):
        if e.is_dir():
            out.append((e, True, *du(e)))
        elif e.is_file():
            out.append((e, False, 1, e.stat().st_size, False))
    return out


async def ls_screen(app, uid: int, folder: Path, page: int = 0):
    sess = app.sessions.get(uid)
    entries = await asyncio.to_thread(_listing, app, folder)
    sess.data["ls_dir"], sess.data["ls_entries"] = folder, [e[0] for e in entries]
    owner = uid == app.config.owner_id
    pages = max(1, -(-len(entries) // PAGE))
    page = max(0, min(page, pages - 1))
    n_files = sum(e[2] for e in entries)
    total = sum(e[3] for e in entries)
    rel = _rel(app, folder)
    text = (f"📁 <b>/{html.escape(rel) if rel else ''}</b>  <i>(download folder)</i>\n"
            f"{sum(1 for e in entries if e[1])} folders · {_count(n_files, total, any(e[4] for e in entries))}\n"
            + ("🔒 <i>The database mirror — only the owner opens it.</i>\n" if _in_db(app, folder) else "")
            + "\nTap a folder to open it, a file to get it.")
    rows = []
    for i, (p, is_dir, files, size, capped) in enumerate(entries[page * PAGE:(page + 1) * PAGE], page * PAGE):
        locked = is_dir and _in_db(app, p) and not owner
        label = (f"{'🔒' if locked else '📁'} {p.name[:28]} · {_count(files, size, capped)}" if is_dir
                 else f"📄 {p.name[:30]} · {human_bytes(size)}")
        rows.append([btn(label, cb("ls", uid, "", f"o_{i}"))])
    nav = []
    if pages > 1:
        nav = [btn("◀️", cb("ls", uid, "", f"p_{page - 1}")) if page else btn(" ", cb("ls", uid, "", "noop")),
               btn(f"{page + 1} / {pages}", cb("ls", uid, "", "noop")),
               btn("▶️", cb("ls", uid, "", f"p_{page + 1}")) if page < pages - 1 else btn(" ", cb("ls", uid, "", "noop"))]
    bottom = ([btn("⬆️ Up", cb("ls", uid, "", "up"))] if rel else []) + [btn(t("btn_home"), cb("nav", uid, "home"))]
    if owner and rel:
        bottom.insert(1, btn("🗑 Delete this folder", cb("ls", uid, "", "rmdir"), style="danger"))
    return text, kb(*rows, nav, bottom)


@guard("admin", force_sub=False, private_only=True)
async def ls_command(client, message) -> None:
    app, uid = client.app, message.from_user.id
    arg = (message.text or "").split(maxsplit=1)
    folder = safe_path(app, arg[1]) if len(arg) > 1 else root_of(app)
    if folder is None or not folder.is_dir():
        return await app.sender.send_message(message.chat.id, "No such folder inside the download folder.")
    if _in_db(app, folder) and uid != app.config.owner_id:
        return await app.sender.send_message(message.chat.id, "🔒 Only the owner opens the database folder.")
    text, markup = await ls_screen(app, uid, folder)
    await app.sender.send_message(message.chat.id, text, reply_markup=markup)


@guard("admin", force_sub=False)
async def ls_button(client, query) -> None:
    app, uid = client.app, query.from_user.id
    arg = parse_cb(query.data).arg
    chat_id, msg_id = query.message.chat.id, query.message.id
    sess = app.sessions.peek(uid)
    folder = sess.data.get("ls_dir") if sess else None
    if folder is None:
        return await app.sender.answer(query.id, "This list has expired — send /ls again.", show_alert=True)
    if arg == "noop":
        return await app.sender.answer(query.id)
    page = 0
    if arg == "up":
        folder = folder.parent if folder.parent != folder and root_of(app) in (folder.parent, *folder.parent.parents) else folder
    elif arg.startswith("p_"):
        page = int(arg[2:])
    elif arg == "rmdir":
        await app.sender.answer(query.id)
        return await _ask_rm(app, chat_id, uid, folder)
    elif arg.startswith("o_"):
        entries = sess.data.get("ls_entries") or []
        i = int(arg[2:])
        if i >= len(entries) or not entries[i].exists():
            return await app.sender.answer(query.id, "It's gone — refresh with /ls.", show_alert=True)
        p = entries[i]
        if _in_db(app, p) and uid != app.config.owner_id:
            return await app.sender.answer(query.id, "🔒 Only the owner opens the database folder.", show_alert=True)
        if p.is_file():
            if p.stat().st_size > 1950 * 1024**2:
                return await app.sender.answer(query.id, "Too big to send on Telegram (over 1.9 GB).", show_alert=True)
            await app.sender.answer(query.id, "Sending…")
            return await app.sender.send_document(chat_id, str(p), caption=f"📄 <code>{html.escape(_rel(app, p))}</code>")
        folder = p
    await app.sender.answer(query.id)
    text, markup = await ls_screen(app, uid, folder, page)
    await app.sender.edit_text(chat_id, msg_id, text, reply_markup=markup)


# ================================================================== /clean
def _leftovers(app) -> list[Path]:
    root = root_of(app)
    live = set(app.tasks.tasks)
    out = []
    tasks = root / "tasks"
    if tasks.exists():
        now = time.time()
        for e in tasks.iterdir():
            if e.is_dir():
                for tdir in e.iterdir():
                    if tdir.name not in live:
                        out.append(tdir)
            elif now - e.stat().st_mtime > 3600:  # stray file (e.g. a broadcast's ID list) older than an hour
                out.append(e)
    cache = root / "_webcache"
    if cache.exists():
        out += list(cache.iterdir())
    return out


@guard("admin", force_sub=False)
async def clean_command(client, message) -> None:
    app, uid = client.app, message.from_user.id
    items = await asyncio.to_thread(_leftovers, app)
    sizes = [await asyncio.to_thread(du, p) for p in items]
    total = sum(s[1] for s in sizes)
    if not items:
        return await app.sender.send_message(message.chat.id, "✨ Nothing to clean — no leftovers.\n<i>The database folder "
                                             "(_db) is never cleaned.</i>")
    await app.sender.send_message(
        message.chat.id, f"🧹 <b>Clean up</b>\n{len(items)} leftover item{'s' if len(items) != 1 else ''} · "
        f"<b>{human_bytes(total)}</b> (old task folders, web cache)\n<i>Running tasks and the database folder (_db) are "
        "never touched.</i>", reply_markup=kb([btn("🧹 Clean now", cb("cln", uid, "", "go"), style="danger")],
                                               [btn(t("btn_cancel"), cb("cln", uid, "", "no"))]))


@guard("admin", force_sub=False)
async def clean_button(client, query) -> None:
    app = client.app
    chat_id, msg_id = query.message.chat.id, query.message.id
    if parse_cb(query.data).arg != "go":
        await app.sender.answer(query.id)
        return await app.sender.edit_text(chat_id, msg_id, "✖️ Nothing cleaned.")
    await app.sender.answer(query.id, "Cleaning…")
    freed, n = await asyncio.to_thread(_clean, app)
    await app.sender.edit_text(chat_id, msg_id, f"✅ <b>Cleaned</b> · {n} item{'s' if n != 1 else ''} · freed "
                               f"<b>{human_bytes(freed)}</b>")
    app.dump.notify(f"🧹 /clean by <code>{query.from_user.id}</code>: {n} items, {human_bytes(freed)} freed")


def _clean(app) -> tuple[int, int]:
    freed = n = 0
    for p in _leftovers(app):  # listed again now: a task that started meanwhile is not touched
        if _in_db(app, p):
            continue
        freed += du(p)[1]
        if p.is_dir():
            shutil.rmtree(p, ignore_errors=True)
        else:
            p.unlink(missing_ok=True)
        n += 1
    return freed, n


# ================================================================== /rm (owner)
@guard("owner", force_sub=False, private_only=True)
async def rm_command(client, message) -> None:
    app, uid = client.app, message.from_user.id
    arg = (message.text or "").split(maxsplit=1)
    if len(arg) < 2:
        return await app.sender.send_message(message.chat.id, "🗑 <b>Delete</b>\n<code>/rm tasks/123</code> — a path inside "
                                             "the download folder (see /ls). Inside <code>_db</code> a backup goes to the "
                                             "dump first.")
    p = safe_path(app, arg[1])
    if p is None or not p.exists():
        return await app.sender.send_message(message.chat.id, "No such file or folder inside the download folder.")
    await _ask_rm(app, message.chat.id, uid, p)


async def _ask_rm(app, chat_id: int, uid: int, p: Path) -> None:
    if uid != app.config.owner_id:
        return await app.sender.send_message(chat_id, "🔒 Only the owner deletes files.")
    files, size, capped = await asyncio.to_thread(du, p)
    sess = app.sessions.get(uid)
    sess.data["rm"] = {"path": p, "at": time.time(), "stage": 1}
    rel = html.escape(_rel(app, p))
    text = f"🗑 Delete <code>{rel}</code>?\n{'Folder' if p.is_dir() else 'File'} · {_count(files, size, capped)}"
    if _in_db(app, p):
        colls = sorted({x.name for x in p.iterdir() if x.is_dir()} if p.name == "_db" else {p.relative_to(root_of(app) / "_db").parts[0]})
        text += (f"\n\n⚠️ <b>This is the database mirror</b> ({html.escape(', '.join(colls) or '—')}).\n"
                 "1. A backup zip is uploaded to the dump first — if that fails, nothing is deleted.\n"
                 "2. You confirm once more.\n<i>Mongo is not touched: /db → Rebuild JSON from Mongo restores it.</i>")
        yes = "📦 Back up, then ask again"
    else:
        yes = "🗑 Yes, delete"
    await app.sender.send_message(chat_id, text + f"\n\n<i>Buttons work for {CONFIRM_S} s.</i>", reply_markup=kb(
        [btn(yes, cb("rm", uid, "", "y1"), style="danger")], [btn(t("btn_cancel"), cb("rm", uid, "", "no"))]))


@guard("owner", force_sub=False)
async def rm_button(client, query) -> None:
    app, uid = client.app, query.from_user.id
    arg = parse_cb(query.data).arg
    chat_id, msg_id = query.message.chat.id, query.message.id
    sess = app.sessions.peek(uid)
    job = sess.data.get("rm") if sess else None
    if arg == "no" or job is None:
        if sess:
            sess.data.pop("rm", None)
        await app.sender.answer(query.id)
        return await app.sender.edit_text(chat_id, msg_id, "✖️ Nothing deleted.")
    if time.time() - job["at"] > CONFIRM_S:
        sess.data.pop("rm", None)
        await app.sender.answer(query.id, "Too late — buttons work for 60 s. Send /rm again.", show_alert=True)
        return await app.sender.edit_text(chat_id, msg_id, "⌛ Expired — nothing deleted.")
    p: Path = job["path"]
    in_db = _in_db(app, p)
    if arg == "y1" and job["stage"] == 1 and in_db:
        await app.sender.answer(query.id, "Backing up…")
        await app.sender.edit_text(chat_id, msg_id, "📦 Making the backup and uploading it to the dump…")
        link = await _backup(app, p)
        if not link:
            sess.data.pop("rm", None)
            return await app.sender.edit_text(chat_id, msg_id, "⚠️ <b>Backup failed — nothing deleted.</b>")
        job.update(stage=2, at=time.time(), backup=link)
        return await app.sender.edit_text(
            chat_id, msg_id, f"📦 Backup is in the dump: {link}\n\n⚠️ <b>Last chance.</b> Delete "
            f"<code>{html.escape(_rel(app, p))}</code> now?\n<i>Works for {CONFIRM_S} s.</i>",
            reply_markup=kb([btn("🗑 Delete now", cb("rm", uid, "", "y2"), style="danger")],
                            [btn(t("btn_cancel"), cb("rm", uid, "", "no"))]))
    if (arg == "y1" and not in_db) or (arg == "y2" and job["stage"] == 2):
        sess.data.pop("rm", None)
        await app.sender.answer(query.id, "Deleting…")
        files, size, _ = await asyncio.to_thread(du, p)
        await asyncio.to_thread(_delete, p)
        if in_db:
            _drop_cache(app, p)
        rel = html.escape(_rel(app, p))
        note = f"🗑 Deleted <code>{rel}</code> · {_count(files, size, False)}" + (f"\n📦 Backup: {job['backup']}" if job.get("backup") else "")
        await app.sender.edit_text(chat_id, msg_id, note)
        return app.dump.notify(f"{note}\n👤 by owner <code>{uid}</code>")
    await app.sender.answer(query.id)


def _delete(p: Path) -> None:
    if p.is_dir():
        shutil.rmtree(p, ignore_errors=True)
    else:
        p.unlink(missing_ok=True)


def _drop_cache(app, p: Path) -> None:
    cache = getattr(app.repo, "cache", None)
    if cache is None:
        return
    db = root_of(app) / "_db"
    if p == db:
        cache.data.clear()
    else:
        cache.drop_collection(p.relative_to(db).parts[0].removesuffix(".json"))


async def _backup(app, p: Path) -> str | None:
    """Zip ``p`` (in a thread) and upload it to the dump; the message link, or None (nothing is deleted then)."""
    out = root_of(app) / "tasks" / f"backup_{p.name}_{time.strftime('%Y%m%d_%H%M%S')}.zip"
    out.parent.mkdir(parents=True, exist_ok=True)
    try:
        await app.repo.json.flush()  # queued JSON writes land first: the backup is the latest state
        await asyncio.to_thread(_zip, p, out)
        if out.stat().st_size > 1950 * 1024**2:
            return None
        from bot.core.tgref import private_link
        dump = app.config.dump_channel_id
        _bot, msg = await app.uploads.send_document(dump, str(out), caption=f"📦 Backup before /rm of {_rel(app, p)}")
        return private_link(dump, msg.id)
    except Exception as e:
        app.dump.report_error(e, "files.backup")
        return None
    finally:
        out.unlink(missing_ok=True)


def _zip(src: Path, out: Path) -> None:
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        if src.is_file():
            z.write(src, src.name)
            return
        for f in src.rglob("*"):
            if f.suffix == ".tmp" or not f.is_file():  # a JSON write in progress: its finished file is backed up
                continue
            try:
                z.write(f, f.relative_to(src.parent).as_posix())
            except FileNotFoundError:  # replaced meanwhile (atomic write) — the new file is listed separately
                continue
