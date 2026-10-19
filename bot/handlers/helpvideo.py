"""Help when a sheet link does not open (owner 2026-10-19): "koi exam link fetch krte hue link response na de kyuki
indian ip chaiye to help video bhjna taki mht file bheje, aur iski setting bot command mai dalna, contact admin button
bhi".

Depends on: ``core.access.guard``, ``core.tgref``, ``render.theme.web_link``. Used by: ``handlers.exam`` (link
failure messages, /tcs /edqt, 📊 Exam Tools), ``handlers.settings`` (/bot → 🎬 Help video), ``handlers/__init__``.

Public API
----------
- ``wants_video(reason)`` — a fetch failure where saving the page in the user's own browser helps: the site refused
  (401 / 403 / 451 — sheets that open only from India), did not answer (network / fetch server), or an unknown
  error. Not for an expired page (404 / 410), a site that is down (5xx) or a page with no sheet.
- ``rows(app, uid, reasons=None)`` — button rows: `🎬 How to save the page (.mhtml)` (only when the owner set a video
  and a reason wants it, or ``reasons`` is None = a how-to screen) and `👤 Contact admin` (``admin_contact``, when set).
- ``parse_post(link) -> (chat, message_id) | None`` — ``t.me/c/<id>/<msg>`` (private channel) or ``t.me/<name>/<msg>``.
- ``await send(app, chat_id) -> bool`` — copies the video post into the chat.
- ``on_button`` — ``hlp:<uid>:video`` (anyone; one video a minute per user).

Setting ``help_video`` = the post's link. Sending a video to /bot → 🎬 stores a copy in the dump channel and saves its
link; a typed link must be readable by the bot (checked by sending it to the owner once).
"""

from __future__ import annotations

import logging
import re
import time

from bot.core.access import guard
from bot.render.theme import web_link
from bot.ui.keyboards import btn, cb, parse_cb

log = logging.getLogger(__name__)
_POST = re.compile(r"(?:https?://)?(?:t|telegram)\.me/(?:(c)/(\d+)|([A-Za-z]\w{3,31}))/(?:\d+/)?(\d+)/?$")
_NO_VIDEO = ("http_404", "http_410", "not_exam", "unsupported_url")
_COOLDOWN_S = 60
_last: dict[int, float] = {}  # uid → when the video was last sent (button spam)


def wants_video(reason: str | None) -> bool:
    r = reason or ""
    return not (r in _NO_VIDEO or r.startswith("http_5"))


def parse_post(link: str) -> tuple[int | str, int] | None:
    m = _POST.match((link or "").strip())
    if not m:
        return None
    private, cid, name, msg = m.groups()
    return (int(f"-100{cid}") if private else f"@{name}"), int(msg)


def rows(app, uid: int, reasons: list | None = None) -> list[list]:
    out = []
    if app.settings.get("help_video") and (reasons is None or any(wants_video(r) for r in reasons)):
        out.append([btn("🎬 How to save the page (.mhtml)", cb("hlp", uid, "video"), style="primary")])
    admin = web_link(app.settings.get("admin_contact"))
    if admin:
        out.append([btn("👤 Contact admin", url=admin)])
    return out


async def send(app, chat_id: int) -> bool:
    post = parse_post(app.settings.get("help_video") or "")
    if post is None:
        return False
    try:
        await app.sender.copy(chat_id, post[0], post[1])
    except Exception as e:  # post deleted, bot not in that channel …: the owner fixes it in /bot
        log.warning("help video %s not sent: %r", app.settings.get("help_video"), e)
        app.dump.notify(f"⚠️ Help video could not be sent ({e.__class__.__name__}) — /bot → 🎬 Help video")
        return False
    return True


@guard()
async def on_button(client, query) -> None:
    app, uid = client.app, query.from_user.id
    data = parse_cb(query.data)
    if data is None or data.obj != "video":
        return await app.sender.answer(query.id)
    now = time.monotonic()
    if now - _last.get(uid, 0) < _COOLDOWN_S:
        return await app.sender.answer(query.id, "🎬 Sent just now — see above.")
    if len(_last) > 5000:
        _last.clear()
    _last[uid] = now
    if not await send(app, query.message.chat.id):
        _last.pop(uid, None)
        return await app.sender.answer(query.id, "The video is not available right now — please contact the admin.",
                                       show_alert=True)
    await app.sender.answer(query.id)
