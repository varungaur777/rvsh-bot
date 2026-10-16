"""Telegram file references stored instead of private links (spec §5).

Depends on: Kurigram. Used by: exam pipeline (inputs, outputs, exam JSON), quiz (cards), PDF tools, batch.

Public API
----------
- ``private_link(chat_id, message_id)`` — dump message link for logs/admins.
- ``from_message(msg) -> dict | None`` — ``{chat_id, message_id, file_unique_id, file_id, size, mime, name}``
  for the media in a message (document/photo/video/audio).
- ``await download(app, ref, dest_dir, progress=None) -> Path`` — fast path ``file_id``; on
  ``FileReferenceExpired``/``MEDIA_EMPTY``/bad id re-fetch the message by ``(chat_id, message_id)``, refresh
  ``ref["file_id"]`` in place and retry once.

Exceptions → outcome: ``SourceUnavailable("dump_missing")`` when the dump message is gone → the task is
cancelled with a friendly message (the input was deleted from the dump channel).
"""

from __future__ import annotations

import logging
from pathlib import Path

from pyrogram import errors as tg_errors

from bot.core.errors import SourceUnavailable

log = logging.getLogger(__name__)
_MEDIA = ("document", "photo", "video", "audio", "animation", "voice")


def private_link(chat_id: int, message_id: int) -> str:
    """``https://t.me/c/<id>/<msg>`` for a -100… channel (opens for members/admins only)."""
    cid = str(chat_id)
    return f"https://t.me/c/{cid[4:] if cid.startswith('-100') else cid.lstrip('-')}/{message_id}"


def from_message(msg) -> dict | None:
    """TgRef for the media inside ``msg`` (``None`` if it has none)."""
    for kind in _MEDIA:
        media = getattr(msg, kind, None)
        if media is not None:
            return {
                "chat_id": msg.chat.id, "message_id": msg.id, "file_unique_id": media.file_unique_id,
                "file_id": media.file_id, "size": getattr(media, "file_size", 0) or 0,
                "mime": getattr(media, "mime_type", None) or ("image/jpeg" if kind == "photo" else ""),
                "name": getattr(media, "file_name", None) or f"{kind}_{msg.id}", "kind": kind,
            }
    return None


async def download(app, ref: dict, dest_dir: Path, progress=None) -> Path:
    """Download a referenced file into ``dest_dir`` (keeps a safe version of the original name)."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    name = "".join(ch if ch.isalnum() or ch in "._- ()" else "_" for ch in (ref.get("name") or "file"))[:120]
    target = dest_dir / f"{ref['message_id']}_{name}"
    client = app.client
    try:
        if ref.get("file_id"):
            return Path(await client.download_media(ref["file_id"], file_name=str(target), progress=progress))
    except (tg_errors.FileReferenceExpired, tg_errors.FileIdInvalid, tg_errors.MediaEmpty, ValueError) as e:
        log.info("file_id fast path failed (%s) — re-fetching dump message", e.__class__.__name__)
    msg = await client.get_messages(ref["chat_id"], ref["message_id"])
    fresh = from_message(msg) if msg and not getattr(msg, "empty", False) else None
    if not fresh:
        raise SourceUnavailable("dump_missing", f"{ref['chat_id']}/{ref['message_id']}")
    ref["file_id"] = fresh["file_id"]
    return Path(await client.download_media(msg, file_name=str(target), progress=progress))
