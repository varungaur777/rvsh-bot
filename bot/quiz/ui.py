"""Quiz screens (spec §17.1): the quiz page, the list, the time picker — text + buttons, no sending.

Depends on: ``quiz.store``, ``ui.keyboards``. Used by: ``handlers.quiz``, ``quiz.maker``.

Callback data: ``qz:<uid>:<quiz_id>:<arg>`` — ``open`` · ``play`` · ``t_<s>`` · ``go_<s>`` · ``html`` · ``lb`` · ``del`` ·
``dely`` · ``rv_<page>`` · ``retry`` · ``wa``; ``qzl:<uid>:<page>:<filter>`` — the list (``all`` | ``mine``);
``qzg:<uid>:<quiz_id>:<arg>`` — the group start card (``t_<s>`` · ``go_<s>`` · ``x``); ``qzc:0:<session>:<arg>`` —
``pause`` · ``resume`` · ``stop`` (shared: the starter, group admins).
"""

from __future__ import annotations

import html
import re

from pyrogram.types import WebAppInfo

from bot.quiz import store
from bot.ui.keyboards import btn, cb, kb, nav_row

LANGS = {"en": "English", "hi": "Hindi", "both": "English + Hindi"}
TIMES = (10, 15, 20, 30, 45, 60)


def share_link(app, quiz_id: str) -> str:
    return f"https://t.me/{app.me.username}?start={quiz_id}"


def webapp_url(app, quiz_id: str) -> str | None:
    web = getattr(app, "web", None)
    base = web.base_url if web is not None and app.settings.get("webapp.enabled") else None
    return f"{base}/app?q={quiz_id}" if base else None


MINIAPP_NAME = re.compile(r"^[A-Za-z0-9_]{3,64}$")


def miniapp_link(app, quiz_id: str) -> str | None:
    """The BotFather Mini App link ``t.me/<bot>/<short name>?startapp=<quiz>`` (spec §17.7.1) — it opens the quiz
    inside Telegram from **anywhere** (groups, channels, other chats), unlike a Web App button (private chats only).
    Only while the Web App is on, its address is **fixed** (named tunnel / own address — BotFather keeps one URL) and
    up, and ``webapp.miniapp`` holds the short name."""
    name = str(app.settings.get("webapp.miniapp") or "").strip().strip("/")
    web = getattr(app, "web", None)
    if not MINIAPP_NAME.match(name) or web is None or not app.settings.get("webapp.enabled"):
        return None
    if not web.base_url or not getattr(web, "fixed_url", False):
        return None
    return f"https://t.me/{app.me.username}/{name}?startapp={quiz_id}"


def page_text(app, quiz: dict, stats: dict | None, viewer: int | None = None) -> str:
    n = len(quiz.get("questions") or [])
    lines = [f"🎯 <b>{html.escape(quiz.get('title') or 'Quiz')}</b>"]
    if quiz.get("meta_line"):
        lines.append(f"<i>{html.escape(quiz['meta_line'])}</i>")
    if quiz.get("section") is not None and quiz.get("scope"):
        lines.append(f"📚 {html.escape(quiz['scope'])}")
    lines.append("")
    lines.append(f"🧩 <b>{n}</b> questions · 🌐 {LANGS.get(quiz.get('lang'), quiz.get('lang') or '')} · "
                 f"⏱ {quiz.get('default_time', 30)} s · ▶️ {int((stats or {}).get('plays') or quiz.get('plays') or 0)} plays")
    lines += ["", "🏆 <b>Top players</b>", store.board_text(stats, viewer, n=5)]
    lines += ["", f"🔗 {share_link(app, quiz['_id'])}"]
    mini = miniapp_link(app, quiz["_id"])
    if mini:
        lines.append(f"📱 {mini}")
    return "\n".join(lines)


def page_markup(app, uid: int, quiz: dict, back: str | None = "qzl"):
    qid = quiz["_id"]
    me = app.me.username
    rows = [[btn("▶️ Play here", cb("qz", uid, qid, "play"), style="success")]]
    url = webapp_url(app, qid)
    if url:
        rows[0].append(btn("📱 Web App", web_app=WebAppInfo(url=url), style="primary"))
    rows.append([btn("🌐 HTML file", cb("qz", uid, qid, "html")),
                 btn("👥 Play in a group", url=f"https://t.me/{me}?startgroup={qid}")])
    rows.append([btn("🏆 Leaderboard", cb("qz", uid, qid, "lb")), btn("📤 Share", switch_inline_query=qid)])
    if app.access.is_admin(uid):
        rows.append([btn("🗑 Delete quiz", cb("qz", uid, qid, "del"), style="danger")])
    nav = [btn("⬅️ Quizzes", cb("qzl", uid, "0", "all"))] if back == "qzl" else []
    nav.append(nav_row(uid)[-1])
    rows.append(nav)
    return kb(*rows)


def time_markup(app, uid: int, quiz: dict, chosen: int, prefix: str = "qz"):
    qid = quiz["_id"]
    row = [btn(("✅ " if s == chosen else "") + f"{s}s", cb(prefix, uid, qid, f"t_{s}"), style="success" if s == chosen else None)
           for s in TIMES]
    rows = [[btn("— ⏱ Seconds per question —", cb(prefix, uid, qid, "h_t"))], row[:3], row[3:],
            [btn("▶️ Start quiz", cb(prefix, uid, qid, f"go_{chosen}"), style="primary")]]
    rows.append([btn("✖️ Cancel", cb(prefix, uid, qid, "x" if prefix == "qzg" else "open"))])
    return kb(*rows)


def setup_text(quiz: dict, time_s: int, group: bool = False) -> str:
    n = len(quiz.get("questions") or [])
    how = ("Everyone in the group can answer; the next question comes when the time is up."
           if group else "The next question comes as soon as you answer.")
    return (f"🎯 <b>{html.escape(quiz.get('title') or 'Quiz')}</b>\n🧩 {n} questions · ⏱ <b>{time_s} s</b> each\n\n"
            f"<i>{how}</i>\nPick the time, then tap <b>▶️ Start quiz</b>.")


def list_text(app, rows: list[dict], page: int, pages: int, mine: bool, total: int, group: bool = False,
              words: str = "") -> str:
    """The quiz list as a post (owner 2026-10-13: "post mai quiz list post mai hypertest link se link lgao, both group
    and personal, post ko beautifully likho" · "title ko black hi rhne do, uske dono link do"): each quiz's title in
    plain bold, then one line ``▶️ Play`` (``?start=``) · ``👥 Play in a group`` (``?startgroup=``) — each link
    once; in a group also ``/quiz qz_…`` (tap to copy) to start it right there."""
    me = app.me.username
    head = "👤 <b>Your quizzes</b>" if mine else "🎯 <b>Quiz library</b>"
    if words:
        head += f" · 🔎 <i>{html.escape(words)}</i>"
    if not rows:
        return (head + "\n\n<i>No quiz found.</i>" if words else
                head + "\n\n<i>No quizzes yet.</i> Send a response sheet and tap <b>🎯 Make Quiz</b> to make one.")
    count = f"{total} quiz{'zes' if total != 1 else ''}" + (f" · page {page + 1}/{pages}" if pages > 1 else "")
    how = ("Tap a quiz's number below to start it here · ▶️ Play = with me in private · 👥 = in another group."
           if group else "▶️ Play = here with me · 👥 = in a group.")
    lines = [head, f"<i>{count}</i>", "━━━━━━━━━━━━━━━━", ""]
    for i, q in enumerate(rows, page * 8 + 1):
        qid = q["_id"]
        play = f"https://t.me/{me}?start={qid}"
        n = q.get("total") or len(q.get("questions") or [])
        lines.append(f"<b>{i}. {html.escape(q.get('title') or 'Quiz')}</b>")
        meta = " · ".join(x for x in (q.get("meta_line"), q.get("scope") if q.get("section") is not None else "") if x)
        if meta:
            lines.append(f"<i>{html.escape(meta)}</i>")
        lines.append(f"🧩 {n} Qs · 🌐 {LANGS.get(q.get('lang'), q.get('lang') or '—')} · ⏱ {q.get('default_time', 30)}s"
                     + (" · 💡" if q.get("explain") else "") + f" · ▶️ {int(q.get('plays') or 0)} plays")
        # one line, each link once (owner 2026-10-13: "quiz post mai group mai play krne ka option dalo … private play
        # ka hyperlink tumne 2 bar bna dia hai" · "title ko black hi rhne do, uske dono link do")
        mini = miniapp_link(app, qid)
        lines.append(f"▶️ <a href=\"{play}\">Play</a>  ·  👥 <a href=\"https://t.me/{me}?startgroup={qid}\">Play in a group</a>"
                     + (f"  ·  📱 <a href=\"{mini}\">Web App</a>" if mini else "")
                     + (f"  ·  here: <code>/quiz {qid}</code>" if group else ""))
        lines.append("")
    lines += ["━━━━━━━━━━━━━━━━", f"💡 <i>{how}</i>"]
    if app.config.branding:
        lines.append(html.escape(app.config.branding))
    return "\n".join(lines)


def list_markup(uid: int, rows: list[dict], page: int, pages: int, flt: str, group: bool = False):
    """Private: a number button per quiz (its page), pages, mine / all, home. Group (owner 2026-10-14: "/quiz,
    /quizzes command group mai buttons nhi dikhati"): a number button per quiz (its start card, here), pages and
    mine / all — for whoever sent the command; an anonymous admin's (uid 0): for the group's admins, no mine / all."""
    nav = []
    if page > 0:
        nav.append(btn("⬅️ Prev", cb("qzl", uid, str(page - 1), flt)))
    if page + 1 < pages:
        nav.append(btn("Next ➡️", cb("qzl", uid, str(page + 1), flt)))
    other = ("all", "🎯 All quizzes") if flt == "mine" else ("mine", "👤 My quizzes")
    if group:
        nums = [btn(str(i), cb("qzg", uid, q["_id"], "card")) for i, q in enumerate(rows, page * 8 + 1)]
        return kb(nums[:4], nums[4:], nav, [btn(other[1], cb("qzl", uid, "0", other[0]))] if uid else [])
    nums = [btn(str(i), cb("qz", uid, q["_id"], "open")) for i, q in enumerate(rows, page * 8 + 1)]
    return kb(nums[:4], nums[4:], nav, [btn(other[1], cb("qzl", uid, "0", other[0]))], nav_row(uid))
