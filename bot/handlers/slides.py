"""``/slides`` — admins (owner 2026-10-19: "ek seprate command bhi banana jisse esi pdf bnai ja ske exam url/mht se,
jisme 1 question 1 page par ho, anskey grid last mai, page landscape mai ho, question right above corner mai, with
header and footer, 1st page par exam detail ho bhdia se likha ho"; owner chose the name ``/slides``, admins only).

- ``/slides`` with a link after it, or as a reply to a ``.mht`` / a message with a link → that input starts the
  usual exam collection (``handlers.exam``), marked for slides.
- ``/slides`` alone → "send the link or .mht now": the next exam input within ``WANT_S`` is for slides (a ``.mht``
  sent with the caption ``/slides`` too).
- An exam already being collected → it becomes slides.
- After ✅ Done: the slides card (language when the sheet has two) → ▶️ Make slides → heavy lane
  (``pipeline.run_slides``: exam bank → ``render_exam_pdf`` layout ``slides``, cached per exam + language + look).

Depends on: ``handlers.exam`` (collection, ``SLIDES_WANT``). Used by: ``handlers/__init__``.
"""

from __future__ import annotations

import time

from bot.core.access import guard
from bot.exam.urls import find_urls
from bot.handlers import exam

ABOUT = ("🖼 <b>Slides PDF</b> — one question per A4-landscape page (number in the top-right corner), the exam's "
         "details on page 1, the answer key grid at the end; header, footer and watermark as in every PDF. "
         "Look: custom, or as in the sheet (the portal's own question boxes).")


class _As:
    """A replied-to message seen as the admin's own input (``from_user`` = the admin)."""

    def __init__(self, msg, user, text: str | None = None) -> None:
        self._m, self.from_user = msg, user
        if text is not None:  # a link in a caption reads like pasted text
            self.text = text

    def __getattr__(self, name):
        return getattr(self._m, name)


def _inner(fn):
    """The exam handler without its guard (this command's own guard has run)."""
    return getattr(fn, "__wrapped__", fn)


@guard("admin", force_sub=False, private_only=True)
async def slides_command(client, message) -> None:
    app, user = client.app, message.from_user
    task, sess = app.tasks.active_for(user.id), app.sessions.peek(user.id)
    if task is not None and sess is not None and sess.step == "exam:collect" and sess.task_id == task.task_id:
        task.data["goal"] = "slides"
        return await app.sender.send_message(message.chat.id, f"{ABOUT}\n\nThis exam will be made as slides — tap "
                                             "<b>✅ Done</b> when everything is sent.", reply_to_message_id=message.id)
    src = message.reply_to_message
    after = (message.text or "").split(maxsplit=1)
    after = after[1] if len(after) > 1 else ""
    if find_urls(after):  # /slides <link>
        exam.SLIDES_WANT[user.id] = time.monotonic()
        return await _inner(exam.on_text)(client, message)
    if src is not None and src.document is not None and exam.is_exam_file(src.document):
        exam.SLIDES_WANT[user.id] = time.monotonic()
        return await _inner(exam.on_document)(client, _As(src, user))
    if src is not None and find_urls(f"{src.text or src.caption or ''}"):
        exam.SLIDES_WANT[user.id] = time.monotonic()
        return await _inner(exam.on_text)(client, _As(src, user, src.text or src.caption or ""))
    exam.SLIDES_WANT[user.id] = time.monotonic()
    await app.sender.send_message(message.chat.id, f"{ABOUT}\n\n👉 Send the exam link (TCS / EDQT) or the "
                                  "<b>.mht</b> file now.", reply_to_message_id=message.id)
