"""Input collection for exam tasks: files and links → task workdir + dump channel (spec §13.1, owner 2026-10-12).

Flow
----
1. The **first** file/link of a user creates a held ``TaskContext`` (one task per user; /cancel and timeouts
   close it → single cleanup path). Everything below lives in its workdir ``…/in/``.
2. A file is **only counted** on arrival and downloaded **after the user taps Done** (owner 2026-10-12), all
   at once with several connections each (``core.fastdl``).
   A link is **first posted to dump** (kept even if the fetch fails), then fetched
   (``net.service``: direct / remote / auto) into ``.mht`` file(s).
3. A file is **forwarded to dump the moment it arrives** (owner 2026-10-12), flood-safe: files sent together go in
   one ``forwardMessages`` call within the main bot's channel budget (``_Forwarder``). If that budget is busy or
   Telegram asks to wait, the file is **re-uploaded by the uploader pool** after its download instead, with a
   caption: input no., task id, file name + size, link (if any), user name/id/username, time. Fetched link pages
   are always uploaded by the uploader pool. Every such upload is **named after the exam** (``exam_file_names``:
   ``(date) exam name [- EDQT section] (shift)``) instead of the portal's file name. The first dump post (file or link)
   is the **anchor** — claimed synchronously, so it is always the first input to arrive; everything else
   (other inputs, fetched pages, outputs, task log) replies to it.

Depends on: ``net.service``, ``exam.urls``, ``core.tgref``, ``uploader.workers`` (``app.uploads``).
Used by: ``handlers/exam.py`` (and later PDF tools / batch with other kinds).

Public API
----------
- ``start_or_get(app, chat_id, user) -> TaskContext | None`` — ``None`` means another task is active.
- ``register_file(app, task, message)`` (sync: counts it, queues its batched forward to dump) + ``await download_file(app, task, item, message)``;
  ``await add_file(app, task, message)`` does both · ``await add_urls(app, task, urls, progress)``
- ``await anchor_id(task, timeout=120) -> int | None`` — waits for the first dump upload.
- ``exam_file_names(paths, hints)`` — "(dd-mm-yyyy) Exam name [- EDQT section] (Shift N).mht" for dump uploads.
- ``inputs_summary(task)`` — lines for the task log (name, size, private link, failure reason).
- ``await download_received(app, task, transfers=None)`` — after Done: download the counted files.
- ``stored_count(task)`` (usable: stored + received) · ``received_count`` · ``pending_count`` (downloading /
  fetching now) · ``failed(task)``
- ``await settled(task, timeout) -> bool`` — wait until no input is still downloading/fetching.

Exceptions → outcome: ``SourceUnavailable`` for a link is caught per link (its ``reason`` is kept on the item so
the user gets a precise hint; other inputs continue); download/upload errors are reported to dump and the input
is marked failed.
"""

from __future__ import annotations

import asyncio
import contextlib
import html
import logging
import re
import time
from pathlib import Path

from bot.compute import run_cpu
from bot.core.bg import spawn
from bot.core.errors import SourceUnavailable
from bot.core.tgref import private_link
from bot.exam.jobs import exam_file_names
from bot.exam.urls import add_mht_page, hints_from_text
from bot.net import service
from bot.tasks.lanes import TaskExists
from bot.ui.progress import human_bytes

log = logging.getLogger(__name__)


_background = spawn  # fire-and-forget dump posts (strong refs, errors logged)


def _safe(name: str) -> str:
    return re.sub(r"[^\w.\- ()]+", "_", name)[:100] or "file"


def start_or_get(app, chat_id: int, user):
    """The user's collecting task, a new one, or ``None`` when another task is running/queued."""
    task = app.tasks.active_for(user.id)
    sess = app.sessions.get(user.id)
    if task is not None:
        return task if (sess.step == "exam:collect" and sess.task_id == task.task_id) else None
    try:
        task = app.tasks.create(user.id, "exam", "light", app.access.priority(user.id), title="exam", chat_id=chat_id)
        app.tasks.hold(task)  # synchronous with the check above: two files at once cannot open two tasks
    except TaskExists:
        return None
    if sess.step or sess.ui_message_id is not None:  # another menu was open (e.g. /results): it closes, its card
        app.sessions.clear(user.id)                   # is not reused for this exam
        sess = app.sessions.get(user.id)
    loop = asyncio.get_running_loop()
    task.data.update(inputs=[], seq=0, hints={}, anchor_fut=loop.create_future(), anchor_owner=None, settled=asyncio.Event(),
                     user_name=" ".join(x for x in (user.first_name, user.last_name) if x) or str(user.id),
                     username=user.username or "")
    sess.step, sess.task_id = "exam:collect", task.task_id
    task.spawn(task.prepare())
    return task


def _new_item(task, kind: str, name: str, size: int | None = None, status: str = "receiving", **extra) -> dict:
    """Statuses: ``received`` (file counted, not downloaded yet) · ``receiving`` (downloading / fetching) ·
    ``stored`` · ``failed``."""
    task.data["seq"] += 1
    item = {"seq": task.data["seq"], "kind": kind, "name": name, "size": size, "paths": [], "status": status, **extra}
    task.data["inputs"].append(item)
    if status == "receiving":
        task.data["settled"].clear()
    return item


def _finish_item(task, item: dict, status: str, **extra) -> None:
    item.update(status=status, **extra)
    if pending_count(task) == 0:
        task.data["settled"].set()


def _claim_anchor(task, what: str) -> bool:
    """Synchronously decide whether this dump post is the task's anchor (the first one wins)."""
    if task.data["anchor_owner"] is None:
        task.data["anchor_owner"] = what
        return True
    return False


def add_hints(task, text: str | None) -> None:
    for k, v in hints_from_text(text).items():
        task.data["hints"].setdefault(k, v)


async def add_file(app, task, message, progress=None) -> dict:
    """``register_file`` + ``download_file``."""
    item = register_file(app, task, message)
    await download_file(app, task, item, message, progress)
    return item


def register_file(app, task, message) -> dict:
    """Count a received document (status ``received``) and queue its forward to dump. **Nothing is downloaded
    here** (owner 2026-10-12): files are only counted while the user sends them; ``download_received`` fetches
    them after the user taps Done."""
    doc = message.document
    item = _new_item(task, "file", doc.file_name or f"input_{task.data['seq'] + 1}.mht", doc.file_size or 0,
                     status="received", orig_msg_id=message.id)
    item["_message"] = message  # kept in memory only (task.data is never stored)
    add_hints(task, message.caption)
    _forwarder(app, task, message.chat.id).add(item, message.id)
    return item


async def download_received(app, task, transfers=None, parallel: int = 3) -> int:
    """Download every counted-but-not-downloaded file (after Done), ``parallel`` at a time (each one already
    uses several connections, ``core.fastdl``). ``transfers``: a ``ui.progress.Transfers`` for the progress
    block. Returns how many were stored."""
    todo = [it for it in task.data.get("inputs", []) if it["status"] == "received"]
    if not todo:
        return 0
    for it in todo:
        it["status"] = "receiving"
    task.data["settled"].clear()
    gate = asyncio.Semaphore(parallel)
    bars = {it["seq"]: transfers.add("down", it["name"], it.get("size") or None) if transfers else None for it in todo}

    async def one(it: dict) -> None:
        async with gate:
            bar = bars[it["seq"]]
            await download_file(app, task, it, it.pop("_message"), progress=bar.kurigram if bar else None)
            if bar:
                bar.done(it["status"] == "stored")

    await asyncio.gather(*(one(it) for it in todo))
    return sum(1 for it in todo if it["status"] == "stored")


# ------------------------------------------------------------------ forward to dump (flood-safe)
class _Forwarder:
    """Forwards a task's input messages to dump in batches — **one** ``forwardMessages`` call per batch.

    Telegram limits (Bot API FAQ "My bot is hitting limits"): ≈ 1 message/s per chat, ≈ 20 messages/min per
    group or channel, ≈ 30 messages/s overall; ``forwardMessages`` takes up to 100 ids. So:
    - files sent together (an album / a quick burst) are gathered for ``dump.forward_window_s`` and forwarded
      in one call (≤ 100 ids), as a reply to the task's anchor;
    - the call uses the main bot's dump-channel budget at PROGRESS priority (before dump log events);
    - it never waits long and never retries a flood: if no budget is free within ``dump.forward_max_wait_s``
      or Telegram answers FloodWait, the batch is marked "not forwarded" and each file is **re-uploaded by the
      uploader bots** after its download (they have their own per-bot budgets) — so nothing queues up and
      nothing is lost.
    """

    def __init__(self, app, task, from_chat: int) -> None:
        self.app, self.task, self.from_chat = app, task, from_chat
        self.batch: list[tuple[dict, int]] = []
        self.timer: asyncio.TimerHandle | None = None

    def add(self, item: dict, msg_id: int) -> None:
        loop = asyncio.get_running_loop()
        item["fwd"] = loop.create_future()
        item["anchor_owner"] = _claim_anchor(self.task, f"fwd:{item['seq']}")
        self.batch.append((item, msg_id))
        if len(self.batch) >= 100:
            self._flush()
        elif self.timer is None:
            self.timer = loop.call_later(float(self.app.settings.get("dump.forward_window_s") or 1.0), self._flush)

    def _flush(self) -> None:
        if self.timer is not None:
            self.timer.cancel()
            self.timer = None
        batch, self.batch = self.batch, []
        if batch:
            self.task.keep(self._send(batch))

    async def _send(self, batch: list[tuple[dict, int]]) -> None:
        from pyrogram import errors as tg_errors

        from bot.core.sender import PROGRESS
        app, task = self.app, self.task
        dump = app.config.dump_channel_id
        owner = batch[0][0]["anchor_owner"]
        reply_to = None if owner else await anchor_id(task)
        ids = [mid for _, mid in batch]
        kw = {"reply_to_message_id": reply_to} if reply_to else {}
        msgs = None
        try:
            msgs = await app.sender.call(dump, app.client.forward_messages, dump, self.from_chat, ids, priority=PROGRESS,
                                         retries=0, max_wait=float(app.settings.get("dump.forward_max_wait_s") or 10), **kw)
        except asyncio.TimeoutError:
            log.info("dump busy: %d input(s) of %s go through the uploader bots", len(ids), task.task_id)
        except tg_errors.FloodWait as e:
            log.info("forward flood-wait %ss: %d input(s) of %s go through the uploader bots", e.value, len(ids), task.task_id)
        except Exception as e:  # e.g. a deleted original: the re-upload path still has the downloaded file
            log.warning("forward to dump failed (%r): %d input(s) re-uploaded instead", e, len(ids))
        msgs = msgs if isinstance(msgs, list) else ([msgs] if msgs else [])
        for (item, _), msg in zip(batch, msgs):
            item.setdefault("dump", []).append({"chat_id": msg.chat.id, "message_id": msg.id, "bot": "main",
                                                "link": private_link(msg.chat.id, msg.id), "forwarded": True})
        for i, (item, _) in enumerate(batch):
            if not item["fwd"].done():
                item["fwd"].set_result(i < len(msgs))
        if owner and msgs:
            task.data["anchor_id"] = msgs[0].id
            if not task.data["anchor_fut"].done():
                task.data["anchor_fut"].set_result(msgs[0].id)


def _forwarder(app, task, from_chat: int) -> _Forwarder:
    f = task.data.get("forwarder")
    if f is None:
        f = task.data["forwarder"] = _Forwarder(app, task, from_chat)
    return f


async def _ensure_dump(app, task, item: dict, path: Path | None) -> None:
    """After the download: if the forward did not happen, re-upload the file through the uploader bots."""
    fut = item.get("fwd")
    forwarded = await fut if fut is not None else False
    if forwarded:
        return
    if path is not None:
        await _to_dump(app, task, item, path, item.get("anchor_owner", False))
    elif item.get("anchor_owner") and not task.data["anchor_fut"].done():
        task.data["anchor_fut"].set_result(None)  # neither forwarded nor downloaded: nobody is the anchor


async def download_file(app, task, item: dict, message, progress=None) -> dict:
    """Download a registered document into the workdir now (``progress``: Kurigram ``progress=`` callback).
    Its dump copy is the forward queued by ``register_file``; only if that did not happen is the file
    re-uploaded by the uploader bots (``_ensure_dump``)."""
    dest = task.workdir / "in" / f"{item['seq']:03d}_{_safe(item['name'])}"
    path = None
    try:
        await asyncio.to_thread((task.workdir / "in").mkdir, parents=True, exist_ok=True)
        fast = getattr(app, "fastdl", None)  # several connections per file (core.fastdl), normal download otherwise
        got = await (fast.download(message, str(dest), progress) if fast else
                     app.client.download_media(message, file_name=str(dest), progress=progress))
        path = Path(got)
        item["paths"].append(str(path))
        _finish_item(task, item, "stored")
    except asyncio.CancelledError:
        raise
    except Exception as e:
        _finish_item(task, item, "failed", error="download")
        app.dump.report_error(e, "inputs.download_file")
    if "fwd" in item:
        task.keep(_ensure_dump(app, task, item, path))
    elif path is not None:  # no forward queued (direct callers): plain re-upload
        task.keep(_to_dump(app, task, item, path, _claim_anchor(task, str(path))))
    return item


async def add_urls(app, task, urls: list[str], progress, on_assets=None, gate=None, priority: int = 2) -> list[dict]:
    """Post each link to dump first, then fetch it (all EDQT sections) into ``.mht`` files; each fetched page is
    uploaded to dump in the background as a reply to the anchor. ``progress(text)`` shows a status line;
    ``on_assets(item, done, total)`` reports page files (images/CSS) for the progress bar. ``gate``
    (``tasks.gate.Gate``): links are opened only a few at a time across all users — while waiting the user sees
    their queue position (the dump post is not delayed)."""
    items = [_new_item(task, "url", url.split("?")[0].rsplit("/", 1)[-1] or "link", None, url=url) for url in urls]
    for item in items:  # dump first, before the fetch (owner 2026-10-12); not a task child: kept even if the task closes
        _background(_link_to_dump(app, task, item, _claim_anchor(task, item["url"])))
    await asyncio.sleep(0)  # let the dump posts start before the first request goes out
    for n, item in enumerate(items, 1):
        label = f"🌐 <b>Opening link {n} of {len(items)}…</b>" if len(items) > 1 else "🌐 <b>Opening the link…</b>"
        cb = (lambda d, total, item=item: on_assets(item, d, total)) if on_assets else None
        slot = gate.slot(priority, on_wait=lambda pos: progress(f"⏳ <b>In the queue</b> — #{pos} to open the link…")) \
            if gate else contextlib.nullcontext()
        try:
            async with slot:
                progress(label)
                pages = await service.fetch_url(app, item["url"], on_assets=cb,
                                                progress=lambda s, label=label: progress(f"{label}\n<i>{s}</i>"))
        except SourceUnavailable as e:
            _finish_item(task, item, "failed", error=e.reason or "unavailable")
            continue
        except asyncio.CancelledError:
            raise
        except Exception as e:  # a bug, not the site: report it, keep the other inputs going
            _finish_item(task, item, "failed", error="internal")
            app.dump.report_error(e, "inputs.add_urls")
            continue
        await asyncio.to_thread((task.workdir / "in").mkdir, parents=True, exist_ok=True)
        for i, page in enumerate(pages):
            p = task.workdir / "in" / f"{item['seq']:03d}_{i:02d}_{page.kind}.mht"
            await asyncio.to_thread(p.write_bytes, page.mht)
            item["paths"].append(str(p))
        _finish_item(task, item, "stored", pages=len(pages), size=sum(len(pg.mht) for pg in pages),
                     sections=[pg.section for pg in pages if pg.section],
                     missing_assets=sum(len(pg.missing) for pg in pages))
        paths = [Path(p) for p in item["paths"]]
        names = await run_cpu(app, "parse", task.task_id, exam_file_names, paths, task.data.get("hints"))
        for k, (p, name) in enumerate(zip(paths, names)):
            task.keep(_to_dump(app, task, item, p, False, name, (k, len(paths), len(pages[k].missing))))
    return items


async def anchor_id(task, timeout: float = 120) -> int | None:
    fut = task.data.get("anchor_fut")
    if fut is None:
        return task.data.get("anchor_id")
    try:
        return await asyncio.wait_for(asyncio.shield(fut), timeout)
    except asyncio.TimeoutError:
        return None


def _who(task) -> str:
    who = f'<a href="tg://user?id={task.user_id}">{html.escape(task.data.get("user_name", ""))}</a> · <code>{task.user_id}</code>'
    return who + (f" · @{task.data['username']}" if task.data.get("username") else "")


def _caption(task, item: dict, path: Path, name: str | None = None) -> str:
    original = item["name"] if item["kind"] == "file" else path.name
    lines = [f"📥 <b>Input {item['seq']}</b> · task <code>{task.task_id}</code>",
             f"📄 <b>{html.escape(name or original)}</b> · {human_bytes(path.stat().st_size)}"]
    if name and name != original:
        lines.append(f"🗂 sent as <code>{html.escape(original)}</code>")
    if item.get("url"):
        lines.append(f"🌐 {html.escape(item['url'])}")
    lines += [f"👤 {_who(task)}", f"🕒 {time.strftime('%d %b %Y %H:%M:%S')}"]
    return "\n".join(lines)


async def _post(app, task, item: dict, owner: bool, send) -> dict | None:
    """One dump post through the uploader pool (``send(reply_to) -> (bot, msg)``); the anchor resolves the future.
    Returns the dump entry (``None`` when the post failed)."""
    fut = task.data["anchor_fut"]
    reply_to = None if owner else await anchor_id(task)
    try:
        bot, msg = await send(reply_to)
        doc = getattr(msg, "document", None)
        entry = {"chat_id": msg.chat.id, "message_id": msg.id, "bot": bot, "link": private_link(msg.chat.id, msg.id),
                 "file_unique_id": getattr(doc, "file_unique_id", None) if doc else None,
                 "size": getattr(doc, "file_size", None) if doc else None}
        item.setdefault("dump", []).append(entry)
        if owner:
            task.data["anchor_id"] = msg.id
            if not fut.done():
                fut.set_result(msg.id)
        return entry
    except asyncio.CancelledError:
        if owner and not fut.done():
            fut.set_result(None)
        raise
    except Exception as e:
        app.dump.report_error(e, "inputs.dump_post")
        if owner and not fut.done():
            fut.set_result(None)
        return None


async def _to_dump(app, task, item: dict, path: Path, owner: bool, name: str | None = None,
                   page: tuple[int, int, int] | None = None) -> None:
    """Upload one stored input file to dump, **named after the exam** (``exam_file_names``; the file on disk keeps
    its name — Telegram gets ``file_name=``). ``name`` given → used as is (all pages of a link named together).
    ``page`` = (index, pages, files missing) of a link's fetched page: its dump copy is remembered for that link
    (``urls.add_mht_page`` — a better copy replaces a worse one)."""
    if name is None:
        name = (await run_cpu(app, "parse", task.task_id, exam_file_names, [path], task.data.get("hints")))[0]
    entry = await _post(app, task, item, owner, lambda reply_to: app.uploads.send_document(
        app.config.dump_channel_id, str(path), _caption(task, item, path, name), reply_to=reply_to,
        file_name=name or path.name))
    if entry and page is not None and item.get("url"):
        try:
            await add_mht_page(app.repo, item["url"], {**entry, "name": name or path.name}, page[0], page[1], task.user_id,
                               missing=page[2])
        except Exception as e:  # the archive never costs the user the task
            log.warning("mht archive of %s failed: %r", item["url"][:60], e)


def _link_to_dump(app, task, item: dict, owner: bool):
    """Post a received link to dump (before it is fetched)."""
    text = (f"🌐 <b>Input {item['seq']}</b> (link) · task <code>{task.task_id}</code>\n{html.escape(item['url'])}\n"
            f"👤 {_who(task)}\n🕒 {time.strftime('%d %b %Y %H:%M:%S')}")
    return _post(app, task, item, owner, lambda reply_to: app.uploads.send_message(
        app.config.dump_channel_id, text, reply_to=reply_to))


def inputs_summary(task) -> list[str]:
    out = []
    for it in task.data.get("inputs", []):
        links = " ".join(f'<a href="{d["link"]}">dump</a>' for d in it.get("dump", []))
        size = human_bytes(it["size"]) if it.get("size") else "—"
        what = html.escape(it["url"] if it["kind"] == "url" else it["name"])
        extra = f" · {it.get('pages')} page(s)" if it["kind"] == "url" and it.get("pages") else ""
        flag = "" if it["status"] != "failed" else f" · ❌ failed ({html.escape(it.get('error') or '?')})"
        out.append(f"{it['seq']}. {'🌐' if it['kind'] == 'url' else '📄'} {what} · {size}{extra}{flag} {links}".rstrip())
    return out


def stored_count(task) -> int:
    """Usable inputs: downloaded/fetched (``stored``) + counted files waiting for Done (``received``)."""
    return sum(1 for it in task.data.get("inputs", []) if it["status"] in ("stored", "received"))


def received_count(task) -> int:
    """Files counted but not downloaded yet."""
    return sum(1 for it in task.data.get("inputs", []) if it["status"] == "received")


def pending_count(task) -> int:
    return sum(1 for it in task.data.get("inputs", []) if it["status"] == "receiving")


def failed(task) -> list[dict]:
    return [it for it in task.data.get("inputs", []) if it["status"] == "failed"]


async def settled(task, timeout: float) -> bool:
    """Wait until every input finished downloading/fetching; ``False`` on timeout."""
    ev = task.data.get("settled")
    if ev is None or pending_count(task) == 0:
        return True
    try:
        await asyncio.wait_for(ev.wait(), timeout)
        return True
    except asyncio.TimeoutError:
        return False
