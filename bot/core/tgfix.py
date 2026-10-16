"""Small fixes for Kurigram 2.2.26 bugs, applied once at import (``import bot.core.tgfix``).

* ``InputMediaPoll.write`` sets flag bit 1 for ``solution`` but writes the ``solution_entities`` vector only when it
  is non-empty, so a quiz poll with a plain explanation (no entities) is rejected by Telegram with
  ``INPUT_CONSTRUCTOR_INVALID_00``. The schema has one flag for both: write the (maybe empty) vector with the text.

Public API: ``apply()`` (idempotent; run on import). Used by: ``bot.app``, tests.
"""

from __future__ import annotations

from io import BytesIO

from pyrogram import raw
from pyrogram.raw.core import Int, String, Vector


def _input_media_poll_write(self, *args) -> bytes:
    b = BytesIO()
    b.write(Int(self.ID, False))
    flags = 0
    flags |= (1 << 0) if self.correct_answers else 0
    flags |= (1 << 3) if self.attached_media is not None else 0
    flags |= (1 << 1) if self.solution is not None else 0
    flags |= (1 << 2) if self.solution_media is not None else 0
    b.write(Int(flags))
    b.write(self.poll.write())
    if self.correct_answers:
        b.write(Vector(self.correct_answers, Int))
    if self.attached_media is not None:
        b.write(self.attached_media.write())
    if self.solution is not None:
        b.write(String(self.solution))
        b.write(Vector(self.solution_entities or []))
    if self.solution_media is not None:
        b.write(self.solution_media.write())
    return b.getvalue()


def apply() -> None:
    raw.types.InputMediaPoll.write = _input_media_poll_write


apply()
