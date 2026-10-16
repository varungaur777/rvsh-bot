"""``/update`` — the owner updates the bot's code from a zip (owner 2026-10-13: "sare code ko command se bhi update
kia ja ske zip code deke aur jo extra requirements hui vo bhi install ho jae (owner only)").

Send the code ``.zip`` (the deploy zip, a ``git archive``, a GitHub download, or a few changed files in their folders)
and reply to it with ``/update`` → a card: whole bot or a patch, files, Python files checked, packages added /
removed → ``✅ Apply & restart`` → backup → files written → ``pip install -r requirements.txt`` when it changed →
a fresh Python must import the new code (else the backup is put back and the bot keeps running the old code) → the
bot restarts as a new process; after the restart the owner gets "✅ Running the new code". Callback data:
``upd:<owner>:<token>:ok|no``. The card expires after 10 minutes.

Depends on: ``core.codeupdate``. Used by: ``handlers/__init__``, ``bot.app`` (``boot_report`` after the restart).
"""

from __future__ import annotations

import asyncio
import html
import logging
import secrets
import shutil
import time
from pathlib import Path

import orjson

from bot.core import codeupdate as cu
from bot.core.access import guard
from bot.ui.keyboards import btn, cb, kb, parse_cb

log = logging.getLogger(__name__)
EXPIRE_S = 600
HOW = ("🔄 <b>Update the bot's code</b>\n\nSend the code as a <code>.zip</code> (the deploy zip, a GitHub download, "
       "or only the changed files in their folders, like <code>bot/quiz/ui.py</code>), then <b>reply to it</b> with "
       "<code>/update</code>.\nI check it, show what changes, and after ✅ I back the old code up, write the new one, "
       "install new packages from <code>requirements.txt</code>, test that it starts and restart.\n"
       "<i>Your .env, downloads and database are never touched. A backup zip from </i><code>_backup/</code><i> rolls "
       "back the same way.</i>")
_PENDING: dict[str, dict] = {}


def _is_zip(doc) -> bool:
    return bool(doc) and ((doc.file_name or "").lower().endswith(".zip")
                          or (doc.mime_type or "") in ("application/zip", "application/x-zip-compressed"))


@guard("owner", force_sub=False, private_only=True)
async def update_command(client, message) -> None:
    app = client.app
    reply = message.reply_to_message
    if reply is None or not _is_zip(reply.document):
        return await app.sender.send_message(message.chat.id, HOW, reply_to_message_id=message.id)
    if (reply.document.file_size or 0) > cu.UPDATE_MAX:
        return await app.sender.send_message(message.chat.id, f"📦 Too large — a code zip is at most "
                                             f"{cu.UPDATE_MAX // 2**20} MB.", reply_to_message_id=message.id)
    status = await app.sender.send_message(message.chat.id, "📥 <b>Reading the code zip…</b>", reply_to_message_id=message.id)
    work = app.root / "_update" / time.strftime("%Y%m%d_%H%M%S")
    work.mkdir(parents=True, exist_ok=True)
    path = work / "update.zip"
    await app.client.download_media(reply, file_name=str(path))
    root = str(cu.code_dir())
    p = await asyncio.to_thread(cu.plan, str(path), root)
    if not p["ok"]:
        bad = "\n".join(f"• <code>{html.escape(f)}</code> — {html.escape(e)}" for f, e in p["bad_py"][:10])
        return await app.sender.edit_text(message.chat.id, status.id, f"❌ <b>Not applied:</b> {html.escape(p['problem'])}"
                                          + (f"\n{bad}" if bad else "") + "\n<i>The bot keeps running its current code.</i>")
    token = secrets.token_hex(4)
    _PENDING[token] = {"zip": str(path), "plan": p, "at": time.time(), "chat_id": message.chat.id,
                       "name": reply.document.file_name or "update.zip"}
    uid = message.from_user.id
    await app.sender.edit_text(message.chat.id, status.id, card_text(p, _PENDING[token]["name"]),
                               reply_markup=kb([btn("✅ Apply & restart", cb("upd", uid, token, "ok"), style="success")],
                                               [btn("✖️ Cancel", cb("upd", uid, token, "no"), style="danger")]))


def card_text(p: dict, name: str) -> str:
    kind = "the <b>whole bot</b> (bot/ and templates/ are replaced)" if p["full"] else "a <b>patch</b> (only these files)"
    tops: dict[str, int] = {}
    for f in p["files"]:
        k = f.split("/", 1)[0]
        tops[k] = tops.get(k, 0) + 1
    lines = [f"🔄 <b>Code update</b> · <code>{html.escape(name)}</code>", f"📦 {kind}",
             "🗂 " + " · ".join(f"{k} {n}" for k, n in sorted(tops.items())) + f" — {len(p['files'])} files, "
             f"{p['py']} Python files compile ✅"]
    if not p["full"] and len(p["files"]) <= 15:
        lines.append("<blockquote>" + "\n".join(html.escape(f) for f in p["files"]) + "</blockquote>")
    if p["req_changed"]:
        lines.append("📥 Packages to install: " + (", ".join(f"<code>{html.escape(x)}</code>" for x in p["req_added"][:10]) or "—")
                     + (f" · no longer listed: {len(p['req_removed'])}" if p["req_removed"] else ""))
    else:
        lines.append("📥 requirements.txt unchanged — nothing to install")
    if p["skipped"]:
        lines.append(f"⏭ {len(p['skipped'])} other file(s) in the zip are not bot code — left out")
    lines += ["", "<i>✅ = back up the current code → write → install → test it starts → restart. Running tasks get the "
              "usual restart grace; quizzes in progress end.</i>"]
    return "\n".join(lines)[:4000]


@guard("owner", force_sub=False)
async def on_button(client, query) -> None:
    app = client.app
    d = parse_cb(query.data)
    job = _PENDING.pop(d.obj, None)
    chat_id, msg_id = query.message.chat.id, query.message.id
    if job is None or time.time() - job["at"] > EXPIRE_S:
        await app.sender.answer(query.id, "This update card has expired — send /update again.", show_alert=True)
        return await app.sender.edit_markup(chat_id, msg_id, None)
    if d.arg != "ok":
        await app.sender.answer(query.id)
        return await app.sender.edit_text(chat_id, msg_id, "✖️ <b>Update cancelled.</b> Nothing was changed.")
    await app.sender.answer(query.id, "Updating…")
    await apply_update(app, job, chat_id, msg_id)


async def apply_update(app, job: dict, chat_id: int, msg_id: int) -> bool:
    root, p = str(cu.code_dir()), job["plan"]
    say = lambda text: app.sender.edit_text(chat_id, msg_id, text)  # noqa: E731
    await say("🗄 <b>Backing up the current code…</b>")
    bak = await asyncio.to_thread(cu.backup, root, str(app.root / "_backup"))
    await say("✍️ <b>Writing the new code…</b>")
    try:
        n = await asyncio.to_thread(cu.apply, job["zip"], root, p)
    except Exception as e:
        await asyncio.to_thread(cu.restore, bak, root, p)
        await say(f"❌ <b>Writing failed</b> ({html.escape(repr(e))[:200]}) — the old code is back.")
        return False
    note = ""
    if p["req_changed"]:
        await say(f"📥 <b>Installing packages…</b> ({len(p['req_added'])} new) — this can take a few minutes.")
        ok, out = await cu.pip_install(root)
        if not ok:
            await asyncio.to_thread(cu.restore, bak, root, p)
            await say("❌ <b>Package install failed</b> — the old code is back.\n<pre>" + html.escape(out)[-1500:] + "</pre>")
            return False
        note = f" · 📥 {len(p['req_added'])} package(s) installed"
    await say("🧪 <b>Testing that the new code starts…</b>")
    ok, out = await cu.verify(root)
    if not ok:
        await asyncio.to_thread(cu.restore, bak, root, p)
        await say("❌ <b>The new code does not start</b> — the old code is back, nothing restarted.\n<pre>"
                  + html.escape(out)[-1500:] + "</pre>")
        return False
    marker = app.root / cu.MARKER
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_bytes(orjson.dumps({"chat_id": chat_id, "files": n, "full": p["full"], "note": note,
                                     "backup": Path(bak).name, "at": time.time()}))
    await say(f"✅ <b>{n} files written</b>{note} · backup <code>{html.escape(Path(bak).name)}</code>\n♻️ Restarting with "
              "the new code… I'll message you when it's up.")
    app.dump.notify(f"🔄 <b>Code update</b> by the owner: {n} files{note} — restarting")
    app.code_update = True
    app.request_stop("code update (/update)")
    return True


async def boot_report(app) -> None:
    """After a ``/update`` restart: tell the owner the new code runs (the marker is read once)."""
    marker = app.root / cu.MARKER
    for d in (app.root / "_update").glob("*"):  # downloaded zips of earlier updates
        if d.is_dir():
            await asyncio.to_thread(shutil.rmtree, d, True)
    try:
        if not marker.exists():
            return
        info = orjson.loads(marker.read_bytes())
        marker.unlink(missing_ok=True)
        took = int(time.time() - info.get("at", time.time()))
        await app.sender.send_message(info["chat_id"], f"✅ <b>The bot is back on the new code</b> ({info.get('files')} "
                                      f"files{info.get('note', '')}) · restart took {took} s.\n<i>Roll back: reply "
                                      f"/update to the backup zip</i> <code>{html.escape(info.get('backup', ''))}</code> "
                                      "<i>(in the download folder's _backup/ — /ls).</i>")
    except Exception as e:
        log.warning("update boot report failed: %r", e)
