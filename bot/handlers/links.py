"""``/links`` — saved exam links as ``.txt``, **Hindi and English separately** (spec §14.6; owner 2026-10-19: "tcs and
edqt exam link extract language wise krna hai, hindi and english alag"). Admins, private chat only (the links are
candidates' personal answer sheets, spec §25).

- ``/links`` → the exams with saved links (8 a page, ``📚 All exams``) → an exam's card: links saved, how many are
  Hindi / English / mixed / another language / not checked yet → ``📄 Hindi + English files`` (one link per shift for
  each language, the ``/batch`` line format; another language's links are saved but never listed — owner 2026-10-19)
  · ``🔍 Check the language of N links`` (links saved before languages — or before the question-by-question check —
  were kept: the dump ``.mht`` is read, else each page is opened once, page only, on the background lane —
  ``exam.linkcheck``), then the files and **the card again** with the new counts (owner 2026-10-19: "🔍 dabane ke baad
  vo dubara nhi aaya"). A dead link with no saved copy is counted apart ("❌ expired, language not read") and is in
  no file (``urls.unreadable``).
- ``♻️ Recheck all (DB + dump)`` (the list, the All exams card) / ``♻️ Recheck this exam`` (an exam's card) → a
  confirm screen → every saved link of that scope read again (checked ones too), and for *all* every ``.mht`` in
  dump not yet a link's copy (``exam.recheck``; owner 2026-10-19: "db, dump ki sare links aur mht ko recheck krke db
  mai exams links add kiye ja ske, iska option rkhna"), then the files and the card / list again.
- ``/links`` **as a reply** to a ``.txt`` / a message with links, or with the links after the command (a raw list:
  duplicates, ``#`` lines, bare URLs, ``(date) Exam (Shift N): URL`` lines) → "Check N links?" → every link is opened
  once, registered with its exam / date / shift / language → the Hindi file, the English file and the links that
  did not work.

Callback data ``lnk:<uid>:<obj>:<arg>`` — ``pg`` (list page; obj = page), ``e`` (exam card; obj = exam id, ``all``),
``f`` (files), ``chk`` (check unknown links), ``ra`` / ``rgo`` (recheck: confirm / start), ``rs0`` / ``rsl`` /
``rsi`` (recheck all: dump from the start / after the last scan / a typed message id — session step ``links:from``),
``go`` / ``no`` (pasted list: check / cancel), ``x`` (close).

Also used by ``/batch`` (``handlers.batch``): ``pasted_text`` (the links of the command / reply / ``.txt``, ``TooBig``
over 2 MB), ``exam_id(slug)`` and ``pick(app, eid)`` (an exam of the list → title, slugs, counts).

Depends on: ``exam.urls``, ``exam.linkcheck``, ``tasks``, ``core.access.guard``. Used by: ``handlers/__init__``,
``handlers.batch``.
"""

from __future__ import annotations

import hashlib
import html
import logging
import math
from pathlib import Path

from bot.core.access import guard
from bot.exam import linkcheck, recheck
from bot.handlers.waits import WAITERS
from bot.exam import urls as U
from bot.tasks.lanes import TaskExists
from bot.ui.keyboards import btn, cb, kb, parse_cb
from bot.ui.texts import t

log = logging.getLogger(__name__)
PAGE = 8
MAX_LINKS = 5000
MAX_TXT_BYTES = 2 * 2**20


def exam_id(slug: str) -> str:
    return hashlib.sha1(slug.encode()).hexdigest()[:10]


async def pasted_text(app, message) -> str | None:
    """The text holding links: the command's own text, else the replied-to message / ``.txt`` file. None = none given."""
    own = (message.text or "").partition(" ")[2].strip()
    if own:
        return own
    r = message.reply_to_message
    if r is None:
        return None
    doc = getattr(r, "document", None)
    if doc is not None:
        name = (doc.file_name or "").lower()
        if not (name.endswith((".txt", ".csv", ".text")) or (doc.mime_type or "").startswith("text/")):
            return ""
        if (doc.file_size or 0) > MAX_TXT_BYTES:
            raise TooBig()
        folder = Path(app.root) / "tmp" / str(message.from_user.id)
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / "links_input.txt"
        try:
            await app.client.download_media(r, file_name=str(path))
            return path.read_bytes().decode("utf-8", "replace")
        finally:
            path.unlink(missing_ok=True)
    return r.text or r.caption or ""


class TooBig(Exception):
    pass


@guard("admin", force_sub=False, private_only=True)
async def links_command(client, message) -> None:
    app, uid = client.app, message.from_user.id
    try:
        text = await pasted_text(app, message)
    except TooBig:
        return await app.sender.send_message(message.chat.id, "📄 That file is too big — a link list is at most 2 MB.",
                                             reply_to_message_id=message.id)
    if text is None:
        body, markup = await _list_screen(app, uid, 0)
        return await app.sender.send_message(message.chat.id, body, reply_markup=markup)
    items = U.read_links(text)
    if not items:
        return await app.sender.send_message(
            message.chat.id, "🔎 No TCS (digialm) or EDQT (cbexams) response-sheet links found in that.",
            reply_to_message_id=message.id)
    cut = len(items) > MAX_LINKS
    items = items[:MAX_LINKS]
    tcs = sum(1 for u, _ in items if U.detect_kind(u) == "tcs")
    at_once = max(1, int(app.settings.get("batch.fetch_concurrency") or 2))
    secs = math.ceil(len(items) / at_once * 1.5)
    sess = app.sessions.get(uid)
    sess.step, sess.data = "links:confirm", {"lnk_items": items}
    body = (f"🔍 <b>{len(items)} link{'s' if len(items) != 1 else ''}</b> found"
            f" (TCS {tcs} · EDQT {len(items) - tcs})" + (f" — only the first {MAX_LINKS}" if cut else "") + ".\n\n"
            f"I'll open each page once (the page only, {at_once} at a time — about {_dur(secs)}), read exam · date · "
            "shift · <b>language</b>, save them, and send the <b>Hindi</b> and <b>English</b> lists as separate files "
            "(one link per shift) + the links that don't work.")
    msg = await app.sender.send_message(message.chat.id, body, reply_to_message_id=message.id, reply_markup=kb(
        [btn("▶️ Check them", cb("lnk", uid, "", "go"), style="primary"),
         btn(t("btn_cancel"), cb("lnk", uid, "", "no"), style="danger")]))
    sess.ui_message_id = msg.id
    app.sessions.arm_timeout(uid, app.settings.get("input_timeout_s") or 60, lambda u: _timeout(app, message.chat.id, u))


def _dur(secs: int) -> str:
    return f"{secs // 60} min" if secs >= 120 else f"{max(secs, 5)} s"


async def _timeout(app, chat_id: int, uid: int) -> None:
    sess = app.sessions.peek(uid)
    if not sess or sess.step != "links:confirm":
        return
    msg_id = sess.ui_message_id
    app.sessions.clear(uid)
    if msg_id:
        try:
            await app.sender.edit_text(chat_id, msg_id, t("timeout"))
        except Exception:
            pass


# ------------------------------------------------------------------ screens
async def _list_screen(app, uid: int, page: int):
    exams = await U.exams_with_links(app.repo)
    if not exams:
        return ("🔗 <b>No exam links saved yet</b>\n<i>Links are saved when users send response-sheet links or "
                "files. You can also reply <code>/links</code> to a .txt list of links.</i>", None)
    pages = max(1, math.ceil(len(exams) / PAGE))
    page = max(0, min(page, pages - 1))
    total = sum(e["total"] for e in exams)
    unknown = sum(e["unknown"] for e in exams)
    body = (f"🔗 <b>Saved exam links</b> · {len(exams)} exam{'s' if len(exams) != 1 else ''} · {total} link"
            f"{'s' if total != 1 else ''}\n<i>Pick an exam: one link per shift, <b>Hindi and English as separate "
            "files</b>.</i>" + (f"\n🔍 {unknown} not checked yet (language read question by question) — an exam's "
                                "card can check them." if unknown else "")
            + "\n\n<i>Have a list of links? Reply</i> <code>/links</code> <i>to it (a .txt or a message).</i>"
            + (f"\n<i>Page {page + 1}/{pages}</i>" if pages > 1 else ""))
    rows = [[btn(f"{(e['name'] or 'No name · ' + (e['code'] or '?'))[:34]} · {e['total']}", cb("lnk", uid, exam_id(e["slug"]), "e"))]
            for e in exams[page * PAGE:(page + 1) * PAGE]]
    nav = []
    if page:
        nav.append(btn("◀️", cb("lnk", uid, str(page - 1), "pg")))
    if page + 1 < pages:
        nav.append(btn("▶️", cb("lnk", uid, str(page + 1), "pg")))
    return body, kb(*rows, nav, [btn("📚 All exams", cb("lnk", uid, "all", "e"), style="primary"),
                                 btn(t("btn_close"), cb("lnk", uid, "", "x"), style="danger")],
                    [btn("♻️ Recheck all (DB + dump)", cb("lnk", uid, "all", "ra"))])


async def pick(app, eid: str) -> tuple[str, list[str] | None, dict] | None:
    """(title, slugs or None = all, counts) of an exam id from the list."""
    exams = await U.exams_with_links(app.repo)
    if eid == "all":
        counts = {k: sum(e[k] for e in exams) for k in ("total", "hi", "en", "unknown", "other", "mixed", "dead")}
        return "All exams", None, counts
    e = next((x for x in exams if exam_id(x["slug"]) == eid), None)
    if e is None:
        return None
    return e["name"] or f"No name ({e['code'] or '?'})", [e["slug"]], e


def _exam_screen(uid: int, eid: str, title: str, c: dict):
    body = (f"📘 <b>{html.escape(title)}</b>\n🔗 {c['total']} link{'s' if c['total'] != 1 else ''} saved\n"
            f"🌐 Hindi: <b>{c['hi']}</b> · English: <b>{c['en']}</b> · not checked yet: <b>{c['unknown']}</b>"
            + (f"\n🔀 Mixed (Hindi + English questions): <b>{c.get('mixed', 0)}</b>" if c.get("mixed") else "")
            + (f"\n🚫 Other language (Bengali …): <b>{c.get('other', 0)}</b> — saved, not in the files" if c.get("other") else "")
            + (f"\n❌ Expired, language not read (no saved copy): <b>{c.get('dead', 0)}</b> — not in the files"
               if c.get("dead") else "")
            + "\n\n<i>📄 Files: one working link per shift (the one with the most questions in that language), a Hindi "
            "file and an English file (<code>(date) Exam (Shift N): URL</code> — the /batch format).</i>")
    if c["unknown"]:
        body += (f"\n<i>🔍 {c['unknown']} link{'s were' if c['unknown'] != 1 else ' was'} saved before the language was read "
                 "question by question: checking reads its saved .mht (else opens the page once, page only) and saves "
                 "its language.</i>")
    rows = []
    if c["hi"] or c["en"]:
        rows.append([btn("📄 Hindi + English files", cb("lnk", uid, eid, "f"), style="primary")])
    if c["unknown"]:
        rows.append([btn(f"🔍 Check the language of {c['unknown']} link{'s' if c['unknown'] != 1 else ''}",
                         cb("lnk", uid, eid, "chk"), style="success")])
    rows.append([btn("♻️ Recheck all (DB + dump)" if eid == "all" else "♻️ Recheck this exam", cb("lnk", uid, eid, "ra"))])
    rows.append([btn(t("btn_back"), cb("lnk", uid, "0", "pg")), btn(t("btn_close"), cb("lnk", uid, "", "x"), style="danger")])
    return body, kb(*rows)


async def _recheck_items(app, slugs: list[str] | None) -> list[tuple[str, dict]]:
    """Every saved link of the exams (None = all), once per URL, with its saved exam / date / shift as hints."""
    flt = {"exam_name_slug": {"$in": list(slugs)}} if slugs else {}
    out, seen = [], set()
    for x in await app.repo.exam_urls.find(flt, projection={"url": 1, "date": 1, "exam_name": 1, "shift": 1}):
        if x.get("url") and x["url"] not in seen:
            seen.add(x["url"])
            out.append((x["url"], {k: v for k, v in (("date", x.get("date")), ("exam_name", x.get("exam_name")),
                                                     ("shift", x.get("shift"))) if v}))
    return out


def _recheck_screen(uid: int, eid: str, title: str, n: int, at_once: int, start: int = 1, last: int = 0):
    whole = eid == "all"
    body = (f"♻️ <b>Recheck · {html.escape(title)}</b>\n\n"
            f"• <b>{n}</b> saved link{'s' if n != 1 else ''} read again — checked ones too: the saved .mht in dump first, "
            f"else the page once (page only, the bot's usual route, {at_once} at a time; about "
            f"{_dur(math.ceil(n / at_once * 1.5))} at most)."
            + ("\n• Then <b>the dump channel</b> from message <b>#" + str(start) + "</b> to the latest"
               + (" (from the start)" if start <= 1 else "") + ": every .mht that is not yet a link's copy is downloaded "
               "and read, and every link written in a message is checked; their links are saved with exam, date, shift "
               "and language." + (f"\n<i>Last full scan ended at #{last}.</i>" if last else "") if whole else "")
            + "\n\n<i>Then the Hindi + English files and this card again. /cancel stops it.</i>")
    rows = []
    if whole:
        rows.append([btn(("✅ " if start <= 1 else "") + "From the start", cb("lnk", uid, eid, "rs0"))]
                    + ([btn(("✅ " if last and start == last else "") + f"After last scan (#{last})",
                            cb("lnk", uid, eid, "rsl"))] if last else []))
        rows.append([btn("🔢 Type a message ID" if start <= 1 or start == last else f"✅ From #{start} · change",
                         cb("lnk", uid, eid, "rsi"))])
    rows.append([btn("▶️ Start", cb("lnk", uid, eid, "rgo"), style="primary"), btn(t("btn_back"), cb("lnk", uid, eid, "e"))])
    return body, kb(*rows)


def _dump_from(app, uid: int) -> int:
    """The dump start the admin picked on the recheck screen (1 = from the start)."""
    sess = app.sessions.peek(uid)
    if sess and sess.step in ("links:recheck", "links:from"):
        return int(sess.data.get("lnk_from") or 1)
    return 1


async def _show_recheck(app, uid: int, chat_id: int, msg_id: int, eid: str, start: int) -> None:
    picked = await pick(app, eid)
    if picked is None:
        return
    title, slugs, _counts = picked
    sess = app.sessions.get(uid)
    sess.step, sess.ui_message_id = "links:recheck", msg_id
    sess.data = {"lnk_from": start, "lnk_eid": eid}
    at_once = max(1, int(app.settings.get("batch.fetch_concurrency") or 2))
    items = await _recheck_items(app, slugs)
    body, markup = _recheck_screen(uid, eid, title, len(items), at_once, start, await recheck.last_scanned(app))
    await app.sender.edit_text(chat_id, msg_id, body, reply_markup=markup)


async def _take_from(app, message, sess) -> None:
    """The dump message id typed for ♻️ Recheck all (``links:from``)."""
    n = recheck.parse_message_id(message.text)
    if n is None:
        return await app.sender.send_message(message.chat.id, "🔢 Send a dump message number (like <code>1234</code>) or "
                                             "its link (<code>https://t.me/c/…/1234</code>).",
                                             reply_to_message_id=message.id)
    app.sessions.disarm(message.from_user.id)
    await _show_recheck(app, message.from_user.id, message.chat.id, sess.ui_message_id, sess.data.get("lnk_eid") or "all", n)
    await app.sender.send_message(message.chat.id, f"✅ The dump will be read from message #{n} — tap ▶️ Start above.",
                                  reply_to_message_id=message.id)


WAITERS.update({"links:from": _take_from})


# ------------------------------------------------------------------ buttons
@guard("admin", force_sub=False, private_only=True)
async def on_button(client, query) -> None:
    app, uid = client.app, query.from_user.id
    d = parse_cb(query.data)
    chat_id, msg_id = query.message.chat.id, query.message.id
    arg = d.arg

    async def edit(body, markup=None):
        await app.sender.edit_text(chat_id, msg_id, body, reply_markup=markup)

    if arg == "x":
        await app.sender.answer(query.id)
        sess = app.sessions.peek(uid)
        if sess and sess.step.startswith("links:"):
            app.sessions.clear(uid)
        return await app.sender.remove(chat_id, msg_id, "✖️ Closed.")
    if arg == "pg":
        await app.sender.answer(query.id)
        return await edit(*await _list_screen(app, uid, int(d.obj or 0)))
    if arg in ("go", "no"):
        sess = app.sessions.peek(uid)
        if not sess or sess.step != "links:confirm" or sess.ui_message_id != msg_id:
            return await app.sender.answer(query.id, "This list has expired — send /links again.", show_alert=True)
        app.sessions.disarm(uid)
        items = sess.data["lnk_items"]
        app.sessions.clear(uid)
        if arg == "no":
            await app.sender.answer(query.id)
            return await edit(t("cancelled"))
        return await _start_check(app, query, items, mode="filter", title="Checked links", slugs=None)
    picked = await pick(app, d.obj)
    if picked is None:
        return await app.sender.answer(query.id, "That exam has no saved links any more.", show_alert=True)
    title, slugs, counts = picked
    if arg == "e":
        await app.sender.answer(query.id)
        return await edit(*_exam_screen(uid, d.obj, title, counts))
    if arg == "f":
        await app.sender.answer(query.id, "📄 Sending the files…")
        per_lang = await U.links_by_lang(app.repo, slugs)
        unknown, dead = len(per_lang.get("unknown") or []), len(per_lang.get("dead") or [])
        note = "\n".join(x for x in (
            f"{unknown} saved link{'s' if unknown != 1 else ''} not checked yet — tap 🔍 on the card" if unknown else "",
            f"{dead} expired link{'s' if dead != 1 else ''} without a saved copy (language not read) left out" if dead
            else "") if x)
        if not await linkcheck.send_files(app, chat_id, uid, per_lang, title, note=note):
            await app.sender.send_message(chat_id, "🔗 No link with a known language yet — tap 🔍 to check them first.")
        return
    if arg == "chk":
        per_lang = await U.links_by_lang(app.repo, slugs)
        items = [(x["url"], {k: v for k, v in (("date", x.get("date")), ("exam_name", x.get("exam_name")),
                                                ("shift", x.get("shift"))) if v})
                 for x in per_lang.get("unknown") or []]
        if not items:
            return await app.sender.answer(query.id, "Every saved link is already checked.", show_alert=True)
        return await _start_check(app, query, items[:MAX_LINKS], mode="exam", title=title, slugs=slugs, eid=d.obj)
    if arg in ("ra", "rs0", "rsl"):
        await app.sender.answer(query.id)
        start = 1 if arg in ("ra", "rs0") else (await recheck.last_scanned(app) or 1)
        return await _show_recheck(app, uid, chat_id, msg_id, d.obj, start)
    if arg == "rsi":
        await app.sender.answer(query.id)
        sess = app.sessions.get(uid)
        sess.step, sess.ui_message_id = "links:from", msg_id
        sess.data["lnk_eid"] = d.obj
        app.sessions.arm_timeout(uid, app.settings.get("input_timeout_s") or 60, lambda u: _from_timeout(app, u))
        return await app.sender.send_message(chat_id, "🔢 Send the dump message to start from: its number (like "
                                             "<code>1234</code>) or its link (<code>https://t.me/c/…/1234</code>).")
    if arg == "rgo":
        items = await _recheck_items(app, slugs)
        if not items and d.obj != "all":
            return await app.sender.answer(query.id, "That exam has no saved links any more.", show_alert=True)
        start = _dump_from(app, uid)
        sess = app.sessions.peek(uid)
        if sess and sess.step.startswith("links:"):
            app.sessions.clear(uid)
        return await _start_check(app, query, items, mode="exam", title=title, slugs=slugs, eid=d.obj, recheck_all=True,
                                  dump_from=start)
    await app.sender.answer(query.id)


async def _send_card(app, chat_id: int, uid: int, eid: str) -> None:
    """The exam's card again after a check, with the new counts (and 🔍 while links are left to check)."""
    try:
        picked = await pick(app, eid)
        if picked is not None:
            title, _slugs, counts = picked
            body, markup = _exam_screen(uid, eid, title, counts)
            await app.sender.send_message(chat_id, body, reply_markup=markup)
    except Exception as e:  # the files are sent already: a missing card is not a failed check
        log.warning("links: card after the check not sent: %r", e)


async def _from_timeout(app, uid: int) -> None:
    sess = app.sessions.peek(uid)
    if sess and sess.step == "links:from":  # back to the screen's own choice (from the start)
        sess.step = "links:recheck"


async def _start_check(app, query, items: list, *, mode: str, title: str, slugs: list[str] | None,
                       eid: str | None = None, recheck_all: bool = False, dump_from: int = 1) -> None:
    """The background check of ``items``; ``recheck_all``: ``exam.recheck`` (+ the dump .mht files when ``eid`` is
    ``all``). An exam's card (or the list) comes back after it."""
    uid, chat_id = query.from_user.id, query.message.chat.id
    try:
        task = app.tasks.create(uid, "links", "background", app.access.priority(uid), title="links", chat_id=chat_id)
    except TaskExists:
        return await app.sender.answer(query.id, t("task_exists").replace("<b>", "").replace("</b>", ""), show_alert=True)
    await app.sender.answer(query.id)
    task.data.update(links=items, mode=mode, title=title, slugs=slugs or [], ui_message_id=query.message.id,
                     scan_dump=recheck_all and eid == "all", dump_from=dump_from)

    async def runner(tk) -> None:
        await (recheck.run(app, tk) if recheck_all else linkcheck.run_check(app, tk))
        if eid:  # an exam's card: back with the new counts (owner 2026-10-19: "🔍 dabane ke baad vo dubara nhi aaya")
            await _send_card(app, chat_id, uid, eid)

    pos = await app.tasks.submit(task, runner)
    text = (f"{'♻️ <b>Rechecking' if recheck_all else '🔍 <b>Checking'} {len(items)} link{'s' if len(items) != 1 else ''}…</b>"
            if pos <= 1 else t("queued", pos=pos))
    await app.sender.edit_text(chat_id, query.message.id, text)
