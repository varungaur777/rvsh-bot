"""Deliver a task's output file to its user — in parallel, without blocking the main bot (owner 2026-10-12).

How
---
1. An **uploader bot** uploads the file to the dump channel (that copy is needed anyway — it replies to the
   task's anchor). With no uploader bots the pool uses the main bot for this step.
2. The **main bot copies** that dump message to the user (``copy_message``: server-side, no upload by the main
   bot, one light API call through the central ``Sender`` → per-chat and global limits respected).

So many users receive files at the same time, the main bot's upload slots stay free, and quiz polls / button
answers never wait behind file uploads (they also have higher ``Sender`` priority). Fallback — dump upload or
copy failed: the main bot uploads straight to the user, at most ``tg.direct_upload_slots`` at once.

Depends on: ``uploader.workers`` (``app.uploads``), ``core.sender``, ``core.tgref``, ``exam.inputs.anchor_id``.
Used by: every output runner (scorecard now; PDFs, quiz files later).

Public API
----------
- ``await deliver(app, task, path, caption=…, user_caption=None, kind="document"|"photo", progress=None) -> dict``
  — returns the output record for the task log ``{"name", "size_h", "link"?, "ref"?}`` (``ref`` = the dump copy); ``progress`` is a Kurigram
  ``progress=`` callback (the universal progress bar's ``item.kurigram``).

Exceptions → outcome: ``UserBlockedBot`` propagates (task cleanup); any other failure of the direct fallback
propagates to the task runner (friendly error to the user, report to dump).
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from bot.core.errors import UserBlockedBot
from bot.core.tgref import private_link
from bot.ui.progress import human_bytes

log = logging.getLogger(__name__)


def _slots(app) -> asyncio.Semaphore:
    sem = getattr(app, "_direct_upload_slots", None)
    if sem is None:
        sem = app._direct_upload_slots = asyncio.Semaphore(int(app.settings.get("tg.direct_upload_slots") or 2))
    return sem


async def deliver(app, task, path, *, caption: str, user_caption: str | None = None, kind: str = "document",
                  progress=None) -> dict:
    from bot.exam.inputs import anchor_id

    path = Path(path)
    out = {"name": path.name, "size_h": human_bytes(path.stat().st_size)}
    dump = app.config.dump_channel_id
    text = user_caption if user_caption is not None else caption
    msg = None
    try:
        send = app.uploads.send_photo if kind == "photo" else app.uploads.send_document
        _bot, msg = await send(dump, str(path), caption, reply_to=await anchor_id(task, timeout=30), progress=progress)
        out["link"] = private_link(msg.chat.id, msg.id)
        out["ref"] = {"chat_id": msg.chat.id, "message_id": msg.id}  # the dump copy (output cache, §14.3)
    except asyncio.CancelledError:
        raise
    except Exception as e:  # dump problems must not cost the user their file
        log.warning("dump upload of %s failed (%r) — uploading straight to the user", path.name, e)
    if msg is not None:
        try:
            await app.sender.copy(task.chat_id, dump, msg.id, caption=text)
            return out
        except UserBlockedBot:
            raise
        except Exception as e:
            log.warning("copy of %s to the user failed (%r) — uploading straight to the user", path.name, e)
    async with _slots(app):
        if kind == "photo":
            await app.sender.send_photo(task.chat_id, str(path), caption=text, progress=progress)
        else:
            await app.sender.send_document(task.chat_id, str(path), caption=text, progress=progress)
    return out
