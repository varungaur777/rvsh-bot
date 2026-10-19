"""Exam flow UI (spec §13): files and/or links (no command needed) → Done → summary + action menu →
preference card → queue.

Depends on: ``core.access.guard``, ``exam.inputs``, ``exam.pipeline``, ``exam.urls``, ``ui``.
Used by: ``handlers/__init__``.

Callback data: ``exm:<uid>:<task_id>:<arg>`` — args: ``done``, ``cancel``, ``a_marks``/``a_orig``/``a_custom``/
``a_quiz`` (actions), ``p_<n>`` (marks per question), ``n_<fraction>`` (negative), ``l_<both|en|hi>`` (PDF
language), ``ap_<after|grid>`` (answer placement), ``mine`` (mark my answers), ``ql_<both|en|hi>`` / ``qs_<full|each>`` /
``qt_<s>`` (quiz language, one quiz or one per section, seconds per question), ``ok``, ``back``.

Session steps: ``exam:collect`` → (Done) ``exam:inspect`` → ``exam:menu`` → ``exam:prefs`` → (Confirm) queued.
``/slides`` (``handlers.slides``, admins): the task carries ``goal = "slides"`` → after Done straight to the slides
card (``output: slides`` — look, section, language) → ``pipeline.run_slides``.
The held task exists from the first input; every timeout or /cancel closes it (workdir deleted by
``TaskContext``). Timeouts: ``collect_idle_s`` after the last input → Done offered; ``input_timeout_s`` per step.
"""

from __future__ import annotations

import asyncio
import html
import logging
import time

from bot.core.access import guard
from bot.core.errors import BotError
from bot.core.sender import NO_PREVIEW
from bot.exam import inputs, marking, pipeline
from bot.exam.marks import fraction_text, scheme_text, short_section
from bot.exam.urls import find_urls
from bot.handlers import helpvideo
from bot.ui.keyboards import btn, cb, kb, parse_cb
from bot.ui.progress import ProgressMessage, Transfers, human_bytes
from bot.ui.texts import t

log = logging.getLogger(__name__)
EXAM_EXT = (".mht", ".mhtml", ".html", ".htm")
MAX_INPUT_BYTES = 30 * 1024 * 1024
SLIDES_WANT: dict[int, float] = {}  # admin → when /slides asked for the next exam input (``handlers.slides``)
SLIDES_WANT_S = 600.0
_FRACTIONS = {0: "0", 0.25: "¼", 0.3333: "⅓", 0.5: "½"}


def is_exam_file(doc) -> bool:
    name = (doc.file_name or "").lower()
    return name.endswith(EXAM_EXT) or (doc.mime_type or "") in ("multipart/related", "message/rfc822", "text/html")


# ------------------------------------------------------------------ collection
class _Collector:
    """The **one** status message of a task's collection step (owner 2026-10-12: no message per file) and its
    universal transfer block (downloads ⬇️ / link fetches 🌐). Files sent together are handled concurrently: the
    first handler creates the message under a lock, the others only edit it (throttled by ``ProgressMessage``).
    After "Done" is offered, a late input edits that same message again (a new collector attaches to it)."""

    def __init__(self, app, sess, chat_id: int, task) -> None:
        self.app, self.sess, self.chat_id, self.task = app, sess, chat_id, task
        self.lock = asyncio.Lock()
        self.msg = ProgressMessage(app.sender, chat_id, interval_s=app.settings.get("edit_interval_s"))
        if sess.ui_message_id is not None:
            self.msg.attach(sess.ui_message_id)
        self.transfers = Transfers(self.refresh)
        self.note: str | None = None

    def text(self) -> str:
        task = self.task
        stored, pending, failed = inputs.stored_count(task), inputs.pending_count(task), len(inputs.failed(task))
        if not (stored or pending or failed):
            return "📥 <b>Receiving…</b>"
        parts = [f"✅ <b>{stored}</b> received" if stored else "", f"⏳ <b>{pending}</b> on the way" if pending else "",
                 f"❌ <b>{failed}</b> failed" if failed else ""]
        lines = ["📥 <b>Collecting your response sheet</b>", "  ·  ".join(x for x in parts if x)]
        block = self.transfers.render()
        if block:
            lines += ["", block]
        if self.note:
            lines += ["", self.note]
        if not pending:
            lines += ["", "<i>Send more files or links of the same exam — or just wait, I'll ask when ready.</i>"]
        return "\n".join(lines)

    def refresh(self) -> None:
        """Sync: re-render into the (throttled) message — called by every transfer update."""
        if self.msg.message_id is not None:
            self.msg.set(self.text())

    async def show(self) -> None:
        async with self.lock:
            if self.msg.message_id is None:
                self.sess.ui_message_id = await self.msg.start(self.text())
                return
        self.refresh()

    async def final(self, text: str, **kw) -> None:
        """Last text of this message (always delivered); the collector is then retired."""
        async with self.lock:
            self.transfers.close()
            await self.msg.finish(text, **kw)
            self.sess.ui_message_id = self.msg.message_id
            if self.sess.data.get("collector") is self:
                self.sess.data.pop("collector")

    def close(self) -> None:  # session cleared (task closed)
        self.transfers.close()
        self.msg.close()


def _goal(app, task, user, message=None) -> None:
    """``/slides`` sent just before (or as the file's caption) → this exam is made as slides (admins)."""
    asked = SLIDES_WANT.pop(user.id, None)
    caption = (getattr(message, "caption", None) or "") if message is not None else ""
    if ((asked is not None and time.monotonic() - asked < SLIDES_WANT_S) or caption.strip().startswith("/slides")) \
            and app.access.is_admin(user.id):
        task.data["goal"] = "slides"


def _collector(app, sess, chat_id: int, task) -> _Collector:
    c = sess.data.get("collector")
    if c is None:
        c = sess.data["collector"] = _Collector(app, sess, chat_id, task)
    return c


def link_error_text(reason: str | None) -> str:
    """User text for a link that could not be fetched (reason from ``net.fetcher``)."""
    r = reason or ""
    if r in ("http_404", "http_410"):
        key = "link_expired"
    elif r in ("http_401", "http_403", "http_451"):
        key = "link_blocked"
    elif r.startswith("http_5"):
        key = "link_site_down"
    elif r in ("network", "remote"):
        key = "link_network"
    elif r == "not_exam":
        key = "link_not_exam"
    else:
        key = "link_failed"
    return t(key, code=r.removeprefix("http_")) + "\n" + t("link_save_hint")


def _link_errors(task) -> list[str]:
    return [it.get("error") or "" for it in inputs.failed(task) if it["kind"] == "url"]


def _help(app, uid: int, task) -> dict:
    """``reply_markup`` for a message saying the inputs failed: 🎬 help video (a link the site refused or that did
    not answer) + 👤 Contact admin — none when nothing applies."""
    rows = helpvideo.rows(app, uid, _link_errors(task))
    return {"reply_markup": kb(*rows)} if rows else {}


@guard(private_only=True)
async def on_document(client, message) -> None:
    app, user = client.app, message.from_user
    doc = message.document
    if not is_exam_file(doc):
        return  # other document kinds belong to other tools (PDF tools, P6)
    if (doc.file_size or 0) > MAX_INPUT_BYTES:
        return await app.sender.send_message(message.chat.id, t("file_too_large", limit=MAX_INPUT_BYTES // 2**20),
                                             reply_to_message_id=message.id)
    task = inputs.start_or_get(app, message.chat.id, user)
    if task is None:
        return await app.sender.send_message(message.chat.id, t("task_exists"), reply_to_message_id=message.id)
    _goal(app, task, user, message)
    sess = app.sessions.get(user.id)
    app.sessions.disarm(user.id)
    inputs.register_file(app, task, message)  # only counted + forwarded to dump; downloaded after Done
    await _collector(app, sess, message.chat.id, task).show()
    app.sessions.arm_timeout(user.id, app.settings.get("collect_idle_s"), lambda u: _offer_done(app, message.chat.id, u))


@guard(private_only=True)
async def on_text(client, message) -> None:
    """Exam links pasted as text (any number per message). Other links get a short "not supported" reply."""
    app, user = client.app, message.from_user
    text = message.text or ""
    hidden = " ".join(e.url for e in (getattr(message, "entities", None) or []) if getattr(e, "url", None))
    urls = find_urls(f"{text} {hidden}")  # also links hidden behind text (text_link entities)
    if not urls:
        if "http" in text.lower():
            await app.sender.send_message(message.chat.id, t("link_unsupported"), reply_to_message_id=message.id)
        return
    task = inputs.start_or_get(app, message.chat.id, user)
    if task is None:
        return await app.sender.send_message(message.chat.id, t("task_exists"), reply_to_message_id=message.id)
    _goal(app, task, user)
    sess = app.sessions.get(user.id)
    app.sessions.disarm(user.id)
    inputs.add_hints(task, text.split("http")[0].removeprefix("/slides"))
    chat_id = message.chat.id
    col = _collector(app, sess, chat_id, task)
    bars: dict[int, object] = {}

    def status(line: str) -> None:
        col.note = line
        if not task.closed:
            task.spawn(col.show())

    def assets(item: dict, done: int, total: int) -> None:
        bar = bars.get(item["seq"])
        if bar is None:
            bar = bars[item["seq"]] = col.transfers.add("fetch", "page files", total, unit="items")
        bar.update(done, total)

    items = await inputs.add_urls(app, task, urls, status, assets, gate=app.gates.fetch,
                                  priority=app.access.priority(user.id))
    for bar in bars.values():
        bar.done()
    if task.closed:
        return
    bad = [it for it in items if it["status"] == "failed"]
    col = _collector(app, sess, chat_id, task)
    col.note = link_error_text(bad[0].get("error")) if bad else None
    if inputs.stored_count(task) == 0 and inputs.pending_count(task) == 0:
        await col.final(col.note or t("inputs_all_failed"), **_help(app, user.id, task))
        await app.tasks.cancel(task.task_id, "system: link not reachable")
        return
    await col.show()
    app.sessions.arm_timeout(user.id, app.settings.get("collect_idle_s"), lambda u: _offer_done(app, chat_id, u))


def _inputs_list(task, limit: int = 8) -> str:
    rows = []
    for it in task.data.get("inputs", []):
        if it["status"] not in ("stored", "received"):
            continue
        if it["kind"] == "url":
            pages = it.get("pages") or 1
            rows.append(f"🌐 Link · {pages} page{'s' if pages > 1 else ''}")
        else:
            rows.append(f"📄 {html.escape(it['name'][:40])} · {human_bytes(it.get('size') or 0)}")
    more = len(rows) - limit
    out = [f"{i}. {r}" for i, r in enumerate(rows[:limit], 1)]
    return "\n".join(out + ([f"… and {more} more"] if more > 0 else []))


def _signature(task) -> tuple:
    return tuple(it["seq"] for it in task.data.get("inputs", []) if it["status"] == "stored")


async def _offer_done(app, chat_id: int, uid: int) -> None:
    sess = app.sessions.peek(uid)
    task = app.tasks.active_for(uid)
    if not sess or not task or sess.step != "exam:collect":
        return
    if inputs.pending_count(task):  # still downloading/fetching: its handler re-arms this timer when done
        return
    col = _collector(app, sess, chat_id, task)
    if inputs.stored_count(task) == 0:  # everything failed → nothing to wait for
        await col.final(t("inputs_all_failed"), **_help(app, uid, task))
        await app.tasks.cancel(task.task_id, "system: no usable input")
        return
    if not inputs.received_count(task):  # links only (already fetched): read ahead so "Done" answers at once
        job = task.spawn(pipeline.inspect(app, task))
        job.add_done_callback(lambda f: f.cancelled() or f.exception())  # an error is shown on Done, not here
        sess.data["inspect_job"] = (_signature(task), job)
    n, failed = inputs.stored_count(task), len(inputs.failed(task))
    text = (f"📥 <b>{n} input{'s' if n > 1 else ''} ready</b>" + (f"  ·  ❌ {failed} failed" if failed else "")
            + f"\n<blockquote>{_inputs_list(task)}</blockquote>\n"
            + "Tap <b>✅ Done</b> when you've sent everything — more files or links of the same exam are welcome.")
    markup = kb([btn(t("btn_done"), cb("exm", uid, task.task_id, "done"), style="success"),
                 btn(t("btn_cancel"), cb("exm", uid, task.task_id, "cancel"), style="danger")],
                *(helpvideo.rows(app, uid, _link_errors(task)) if _link_errors(task) else []))
    await col.final(text, reply_markup=markup)
    app.sessions.arm_timeout(uid, app.settings.get("input_timeout_s"), lambda u: _timeout(app, chat_id, u))


async def _timeout(app, chat_id: int, uid: int) -> None:
    """No answer for ``input_timeout_s`` on any step before the task is queued → close it (cleanup path)."""
    sess = app.sessions.peek(uid)
    task = app.tasks.active_for(uid)
    if not sess or not task or sess.step not in ("exam:collect", "exam:menu", "exam:prefs"):
        return
    msg_id = sess.ui_message_id
    await app.tasks.cancel(task.task_id, "timeout", status="timeout")
    if msg_id:
        await app.sender.edit_text(chat_id, msg_id, t("timeout"))


# ------------------------------------------------------------------ buttons
@guard()
async def on_button(client, query) -> None:
    app, uid = client.app, query.from_user.id
    data = parse_cb(query.data)
    sess = app.sessions.peek(uid)
    task = app.tasks.tasks.get(data.obj)
    chat_id, msg_id = query.message.chat.id, query.message.id
    if sess is None or task is None or task.user_id != uid or sess.ui_message_id != msg_id:
        return await app.sender.answer(query.id, "This menu has expired.", show_alert=True)
    arg = data.arg
    if sess.lock.locked() and (arg in _SLOW or arg.startswith("a_")):  # a double tap while the first one works
        return await app.sender.answer(query.id, "⏳ Please wait…")
    async with sess.lock:
        if sess.step not in _steps_for(arg) or task.closed:  # a stale button (the card has moved on)
            return await app.sender.answer(query.id)
        if arg == "done":
            sess.step = "exam:inspect"  # before any await: a second "Done" is now stale
            await app.sender.answer(query.id)
        elif arg == "ok":
            sess.step = "exam:queued"
            await app.sender.answer(query.id)
            return await _confirm(app, chat_id, uid, sess, task)
        else:
            return await _press(app, query, uid, sess, task, arg, chat_id, msg_id)
    await _inspect(app, chat_id, uid, sess, task)  # long (download + read): outside the lock


_SLOW = ("done", "ok", "cancel")


def _steps_for(arg: str) -> tuple[str, ...]:
    """Where a button is valid — anything else is a stale tap on an old card."""
    if arg == "done":
        return ("exam:collect",)
    if arg == "cancel":
        return ("exam:collect", "exam:menu", "exam:prefs")
    if arg.startswith("a_"):
        return ("exam:menu",)
    return ("exam:prefs",)  # h_ p_ n_ ps_ l_ ap_ sp_ mine back ok


async def _press(app, query, uid: int, sess, task, arg: str, chat_id: int, msg_id: int) -> None:
    if arg == "cancel":
        sess.step = "exam:cancelled"
        await app.sender.answer(query.id)
        await app.tasks.cancel(task.task_id, "user")
        return await app.sender.edit_text(chat_id, msg_id, t("cancelled"))
    if arg.startswith("a_"):
        if arg not in ("a_marks", "a_orig", "a_custom", "a_quiz"):
            return await app.sender.answer(query.id)
        if arg == "a_custom" and not (app.settings.get("feature.custom_pdf_public") or app.access.is_admin(uid)):
            return await app.sender.answer(query.id, "🔒 Custom PDF is for admins right now.", show_alert=True)
        if arg == "a_quiz" and not (app.settings.get("feature.quiz_public") or app.access.is_admin(uid)):
            return await app.sender.answer(query.id, "🔒 Making quizzes is for admins right now.", show_alert=True)
        prefs = await marking.suggest(app, sess.data["summary"])
        prefs["neg"] = min(app.settings.get("marks.negative") or list(_FRACTIONS), key=lambda f: abs(f - prefs["neg"]))
        prefs["output"] = {"a_custom": "custom", "a_orig": "original", "a_quiz": "quiz"}.get(arg, "marks")
        if arg == "a_quiz":  # spec §16.3: language, one quiz or one per section, seconds per question
            langs = _langs(sess)
            prefs.update(lang="both" if len(langs) > 1 else langs[0], scope="full",
                         time=int(app.settings.get("quiz.default_question_time_s") or 30))
        if arg == "a_custom":  # spec §13.3: both languages → two PDFs by default
            prefs.update(lang="both", answers="after", mine=True,
                         split="each" if app.access.is_admin(uid) else "full")  # admins: sections too (owner)
        sess.prefs, sess.step = prefs, "exam:prefs"
    elif arg.startswith("h_"):  # a group's title: says what the buttons under it do
        return await app.sender.answer(query.id, _HINTS.get(arg[2:], ""))
    elif arg == "ps_on" and app.access.is_admin(uid):  # admins: "Different marks per section" (spec §13.3)
        p = sess.prefs
        sess.prefs["per_section"] = [[p["plus"], p["neg"]] for _ in sess.data["summary"]["sections"]]
        sess.data["ps_sel"] = 0
    elif arg == "ps_off":
        sess.prefs.pop("per_section", None)
    elif arg.startswith("ps_") and sess.prefs.get("per_section"):  # which section the marks rows change
        sess.data["ps_sel"] = min(int(arg[3:]), len(sess.prefs["per_section"]) - 1)
    elif arg[:2] in ("p_", "n_"):
        j, v = (0 if arg[0] == "p" else 1), float(arg[2:])
        if sess.prefs.get("per_section"):
            sess.prefs["per_section"][sess.data.get("ps_sel", 0)][j] = v
        else:
            sess.prefs["plus" if j == 0 else "neg"] = v
        sess.prefs["source"] = "user"
    elif arg.startswith("l_"):
        sess.prefs["lang"] = arg[2:]
    elif arg.startswith("ql_"):
        sess.prefs["lang"] = arg[3:]
    elif arg in ("qs_full", "qs_each"):
        sess.prefs["scope"] = arg[3:]
    elif arg.startswith("qt_"):
        sess.prefs["time"] = int(arg[3:])
    elif arg in ("ss_custom", "ss_original") and sess.prefs.get("output") == "slides":  # /slides look
        sess.prefs["style"] = arg[3:]
    elif arg.startswith("sc_") and sess.prefs.get("output") == "slides":  # /slides: all sections or one
        n = len(sess.data["summary"]["sections"])
        k = arg[3:]  # sc_all · sc_<i> (anything else from an odd client = all, never an error)
        sess.prefs["section"] = min(int(k), n - 1) if k.isdigit() else None
    elif arg in ("ap_after", "ap_grid"):
        sess.prefs["answers"] = arg[3:]
    elif arg in ("sp_each", "sp_full") and app.access.is_admin(uid):
        sess.prefs["split"] = arg[3:]
    elif arg == "mine":
        sess.prefs["mine"] = not sess.prefs.get("mine", True)
    elif arg == "back":
        sess.step = "exam:menu"
        await app.sender.answer(query.id)
        app.sessions.arm_timeout(uid, app.settings.get("input_timeout_s"), lambda u: _timeout(app, chat_id, u))
        return await app.sender.edit_text(chat_id, msg_id, _menu_text(sess), reply_markup=_menu_markup(uid, task.task_id))
    await app.sender.answer(query.id)
    app.sessions.arm_timeout(uid, app.settings.get("input_timeout_s"), lambda u: _timeout(app, chat_id, u))
    await app.sender.edit_text(chat_id, msg_id, _prefs_text(sess), reply_markup=_prefs_markup(app, uid, task.task_id, sess))


async def _inspect(app, chat_id: int, uid: int, sess, task) -> None:
    app.sessions.disarm(uid)
    sess.step = "exam:inspect"
    task.data["ui_message_id"] = sess.ui_message_id
    progress = ProgressMessage(app.sender, chat_id, interval_s=app.settings.get("edit_interval_s"), task=task)
    progress.attach(sess.ui_message_id)
    try:
        # queue (tasks.gate "prepare"): only a few users download + parse at once; the others see their position
        async with app.gates.prepare.slot(app.access.priority(uid),
                                          on_wait=lambda pos: progress.set(t("prepare_queue", pos=pos))):
            if inputs.received_count(task):  # files were only counted so far: download them now (owner)
                tr = Transfers(lambda: progress.set(t("downloading_files") + "\n\n" + tr.render()))
                progress.set(t("downloading_files"))
                if not await inputs.download_received(app, task, tr):
                    raise BotError("download_failed", "no input could be downloaded")
            if not await inputs.settled(task, timeout=180):  # a link still being fetched
                raise BotError("source_unavailable", "inputs still pending after 180 s")
            progress.set(t("reading_sheet"))
            ahead = sess.data.pop("inspect_job", None)  # parsed while the user was deciding (same inputs)?
            if ahead and ahead[0] == _signature(task):
                summary = await ahead[1]
            else:
                if ahead:
                    ahead[1].cancel()
                summary = await pipeline.inspect(app, task)
    except Exception as e:  # error id first, so the task log (written on close) shows it
        text = t(f"error_{e.key}") if isinstance(e, BotError) else ""
        if not text or text == f"error_{getattr(e, 'key', '')}":
            task.error_id = app.dump.report_error(e, "exam.inspect")
            text = t("error_generic", error_id=task.error_id)
        await progress.finish(text)
        await app.tasks.cancel(task.task_id, "system: unreadable input", status="error")
        return
    finally:
        progress.close()
    if task.closed:
        return
    sess.data["summary"] = summary
    if task.data.get("goal") == "slides":  # /slides: no action menu — the slides card at once
        langs = _langs(sess)
        sess.prefs = {"output": "slides", "lang": "both" if len(langs) > 1 else langs[0], "style": "custom",
                      "section": None}
        sess.step = "exam:prefs"
        await progress.finish(_prefs_text(sess), reply_markup=_prefs_markup(app, uid, task.task_id, sess))
        app.sessions.arm_timeout(uid, app.settings.get("input_timeout_s"), lambda u: _timeout(app, chat_id, u))
        return
    sess.step = "exam:menu"
    await progress.finish(_menu_text(sess), reply_markup=_menu_markup(uid, task.task_id))
    app.sessions.arm_timeout(uid, app.settings.get("input_timeout_s"), lambda u: _timeout(app, chat_id, u))


def _menu_text(sess) -> str:
    return pipeline.summary_text(sess.data["summary"]) + "\n\n👇 <b>What would you like?</b>"


def _menu_markup(uid: int, tid: str):
    return kb([btn("📊 Marks", cb("exm", uid, tid, "a_marks"), style="primary")],
              [btn("📄 Marks + Original PDF", cb("exm", uid, tid, "a_orig")), btn("🧾 Marks + Custom PDF", cb("exm", uid, tid, "a_custom"))],
              [btn("🎯 Make Quiz", cb("exm", uid, tid, "a_quiz"))],
              [btn(t("btn_cancel"), cb("exm", uid, tid, "cancel"), style="danger")])


def _prefs_text(sess) -> str:
    """Owner 2026-10-13 ("caption bahut messy"): the exam in two lines, then only what will be made — marking, and
    the PDF when there is one. The section list is on the menu card before this one."""
    p = sess.prefs
    if p.get("output") == "quiz":
        return (pipeline.summary_text(sess.data["summary"], sections=False) + _quiz_prefs_text(sess)
                + "\n\n👇 Pick below — <b>✅ green</b> is chosen. Then tap <b>Confirm</b>.")
    if p.get("output") == "slides":
        langs = _langs(sess)
        original = p.get("style") == "original"
        lang = ("as in the sheet" if original else
                f"{_LANGS.get(langs[0], langs[0])}  <i>(only language in the sheet)</i>" if len(langs) == 1 else
                "Hindi + English  <i>(2 separate PDFs)</i>" if p["lang"] == "both" else _LANGS.get(p["lang"], p["lang"]))
        look = ("As in the sheet — the portal's own question boxes, colours and ticks kept" if original else
                "Custom — the bot's clean layout, bigger text")
        secs = sess.data["summary"]["sections"]
        sel = p.get("section")
        part = (f"\n📚 {html.escape(short_section(secs[sel][0]))}  <i>· {secs[sel][1]} Qs</i>" if sel is not None
                else f"\n📚 All {len(secs)} sections" if len(secs) > 1 else "")
        return (pipeline.summary_text(sess.data["summary"], sections=False)
                + f"\n\n🖼 <b>Slides PDF</b>\n<blockquote>🎨 {look}{part}\n🌐 {lang}\n📄 A4 landscape · one question per page · "
                  "number in the top-right corner\n📘 Page 1: exam details · 🗝 answer key grid at the end</blockquote>"
                + "\n\n👇 Pick below — <b>✅ green</b> is chosen. Then tap <b>Confirm</b>.")
    src = {"file": "from the sheet", "admin": "set for this exam", "rule": "official scheme",
           "preset": "used for this exam before", "default": "default — please check",
           "user": "your choice"}.get(p.get("source", "user"), "your choice")
    if p.get("per_section"):  # admins: one scheme per section, shown exactly as chosen (+2 / −¼)
        sel = sess.data.get("ps_sel", 0)
        names = [n for n, _ in sess.data["summary"]["sections"]]
        rows = "\n".join(f"{'👉' if i == sel else '▫️'} {i + 1}. {html.escape(short_section(n))}  <b>{scheme_text(pl, ng)}</b>"
                         for i, (n, (pl, ng)) in enumerate(zip(names, p["per_section"])))
        marking_block = f"\n\n📝 <b>Marking</b>  ·  <i>different per section</i>\n<blockquote>{rows}</blockquote>"
    else:
        wrong = f"<b>−{fraction_text(p['neg'])}</b>  <i>of the marks</i>" if p["neg"] else "<b>no minus</b>"
        marking_block = (f"\n\n📝 <b>Marking</b>  ·  <i>{src}</i>\n"
                         f"<blockquote>✅ Right answer  <b>+{p['plus']:g}</b>\n"
                         f"❌ Wrong answer  {wrong}\n"
                         f"⭕ Skipped  <b>0</b></blockquote>")
    pdf = ""
    if p.get("output") == "custom":
        pdf = _pdf_prefs_text(sess)
    elif p.get("output") == "original":
        pdf = "\n\n📄 <b>PDF</b>\n<blockquote>Same look as your sheet · your name, roll no., centre and photo removed</blockquote>"
    return (pipeline.summary_text(sess.data["summary"], sections=False) + marking_block + pdf
            + "\n\n👇 Pick below — <b>✅ green</b> is chosen. Then tap <b>Confirm</b>.")


_LANGS = {"both": "Both", "en": "English", "hi": "Hindi"}
_HINTS = {"ql": "Language of the quiz questions", "qs": "One quiz of the whole exam, or one quiz per section",
          "qt": "How long each question stays open (players can change it when they start)",
          "r": "Marks for each right answer", "w": "Marks cut for each wrong answer (part of that question's marks)",
          "s": "Pick a section, then set its marks in the rows below", "l": "Language of the PDF",
          "a": "Where the right answers are printed",
          "sl": "Custom: the bot's clean layout · As in the sheet: the portal's own question boxes (colours kept)",
          "sc": "Slides of the whole exam, or of one section only", "f": "Admins: one full PDF, or the full PDF plus one PDF per section"}


def _langs(sess) -> list[str]:
    """Languages a whole PDF can be made in (``schema.main_languages``: a Hindi-medium sheet whose English part is
    only in English → Hindi only). Both appear only when the sheet has every question in both — e.g. an EDQT link
    (the fetcher also takes each question's other-language version)."""
    return [x for x in (sess.data["summary"].get("languages") or ["en"]) if x in ("en", "hi")] or ["en"]


def _quiz_prefs_text(sess) -> str:
    p, n_sec = sess.prefs, len(sess.data["summary"]["sections"])
    lang = {"both": "English + Hindi (both on each question)", "en": "English", "hi": "Hindi"}.get(p["lang"], p["lang"])
    scope = f"One quiz per section ({n_sec} quizzes)" if p.get("scope") == "each" and n_sec > 1 else "One quiz of the whole exam"
    return (f"\n\n🎯 <b>Quiz</b>\n<blockquote>🌐 {lang}\n📚 {scope}\n⏱ {p['time']} s per question"
            "\n🖼 Questions with pictures come as an image card + poll</blockquote>\n"
            "<i>You get a quiz page: play here, in a group, as a Web App or an HTML file, and share it.</i>")


def _pdf_prefs_text(sess) -> str:
    p, langs = sess.prefs, _langs(sess)
    if len(langs) == 1:
        lang = f"{_LANGS.get(langs[0], langs[0])}  <i>(only language in the sheet)</i>"
    elif p["lang"] == "both":
        lang = "Hindi + English  <i>(2 separate PDFs)</i>"
    else:
        lang = _LANGS.get(p["lang"], p["lang"])
    if p["answers"] == "after":
        place = "Answer under each question" + ("  ·  <i>yours marked green/red</i>" if p.get("mine", True) else "")
    else:
        place = "Answer key at the end of each section"
    n_sec = len(sess.data["summary"]["sections"])
    files = f"\n📚 Full PDF + {n_sec} section PDFs" if p.get("split") == "each" and n_sec > 1 else ""
    return f"\n\n🧾 <b>PDF</b>\n<blockquote>🌐 {lang}\n🗝 {place}{files}</blockquote>"


def _prefs_markup(app, uid: int, tid: str, sess):
    """Owner 2026-10-13 ("sab ek hi card par, color dalo, sab ache se pdhe jae"): every choice is on the card under a
    title button; the chosen one is green with ✅, the others plain; Confirm is blue. Admins' "Different marks per
    section": pick a section, the same two marks rows then change that section."""
    p = sess.prefs
    secs = sess.data["summary"]["sections"]
    admin = app.access.is_admin(uid)
    multi = len(secs) > 1 and admin

    def title(text, key):
        return [btn(f"— {text} —", cb("exm", uid, tid, f"h_{key}"))]

    def pick(text, arg, on):
        return btn(("✅ " if on else "") + text, cb("exm", uid, tid, arg), style="success" if on else None)

    rows = []
    if p.get("output") == "slides":
        langs = _langs(sess)
        original = p.get("style") == "original"
        rows.append(title("🎨 Look", "sl"))
        rows.append([pick("Custom", "ss_custom", not original), pick("As in the sheet", "ss_original", original)])
        if len(secs) > 1:  # owner 2026-10-19: "kisi ek section ki bhi slide" — all sections or one
            sel = p.get("section")
            rows.append(title("📚 Section", "sc"))
            row = [pick("All sections", "sc_all", sel is None)]
            for i, (n, _c) in enumerate(secs):
                row.append(pick(f"{i + 1}. {short_section(n, 18)}", f"sc_{i}", sel == i))
            rows += [row[i:i + 2] for i in range(0, len(row), 2)]
        if len(langs) > 1 and not original:
            rows.append(title("🌐 PDF language", "l"))
            rows.append([pick("Both (2 PDFs)" if v == "both" else _LANGS[v], f"l_{v}", p["lang"] == v) for v in ("both", *langs)])
        return kb(*rows, [btn("▶️ Confirm", cb("exm", uid, tid, "ok"), style="primary")],
                  [btn(t("btn_cancel"), cb("exm", uid, tid, "cancel"), style="danger")])
    if p.get("output") == "quiz":
        langs = _langs(sess)
        if len(langs) > 1:
            rows.append(title("🌐 Quiz language", "ql"))
            rows.append([pick("Both" if v == "both" else _LANGS[v], f"ql_{v}", p["lang"] == v) for v in ("both", *langs)])
        if len(secs) > 1:
            rows.append(title("📚 Quizzes to make", "qs"))
            rows.append([pick("Whole exam", "qs_full", p.get("scope") != "each"),
                         pick(f"Each section ({len(secs)})", "qs_each", p.get("scope") == "each")])
        rows.append(title("⏱ Seconds per question", "qt"))
        times = [pick(f"{v}s", f"qt_{v}", p["time"] == v) for v in (10, 15, 20, 30, 45, 60)]
        rows += [times[:3], times[3:]]
        return kb(*rows, [btn("▶️ Confirm", cb("exm", uid, tid, "ok"), style="primary")],
                  [btn(t("btn_back"), cb("exm", uid, tid, "back")), btn(t("btn_cancel"), cb("exm", uid, tid, "cancel"), style="danger")])
    if p.get("per_section"):
        sel = sess.data.get("ps_sel", 0)
        plus, neg = p["per_section"][sel]
        rows.append(title("🎚 Section to change", "s"))
        names = [pick(f"{i + 1}. {short_section(n, 18)}", f"ps_{i}", i == sel) for i, (n, _) in enumerate(secs)]
        rows += [names[i:i + 2] for i in range(0, len(names), 2)]
        where = f" · section {sel + 1}"
    else:
        plus, neg, where = p["plus"], p["neg"], ""
    rows.append(title(f"➕ Marks for a right answer{where}", "r"))
    rows.append([pick(f"+{v:g}", f"p_{v:g}", plus == v) for v in app.settings.get("marks.per_question")])
    rows.append(title(f"➖ Minus for a wrong answer{where}", "w"))
    rows.append([pick(f"−{fraction_text(v)}" if v else "No minus", f"n_{v:g}", abs(neg - v) < 1e-3)
                 for v in app.settings.get("marks.negative")])
    if p.get("per_section"):
        rows.append([btn("↩️ Same marks for all sections", cb("exm", uid, tid, "ps_off"))])
    elif multi:
        rows.append([btn("🎚 Different marks per section", cb("exm", uid, tid, "ps_on"))])
    if p.get("output") == "custom":
        langs = _langs(sess)
        if len(langs) > 1:
            rows.append(title("🌐 PDF language", "l"))
            rows.append([pick("Both (2 PDFs)" if v == "both" else _LANGS[v], f"l_{v}", p["lang"] == v) for v in ("both", *langs)])
        after = p["answers"] == "after"
        rows.append(title("🗝 Answers in the PDF", "a"))
        rows.append([pick("Under each question", "ap_after", after), pick("Key at the end", "ap_grid", not after)])
        if after:
            mine = p.get("mine", True)
            rows.append([btn(("✅" if mine else "⬜") + " Mark my answers (green/red)", cb("exm", uid, tid, "mine"),
                             style="success" if mine else None)])
        if multi:  # spec §13.3: admins only
            each = p.get("split") == "each"
            rows.append(title("📚 PDFs to make", "f"))
            rows.append([pick(f"Full + {len(secs)} sections", "sp_each", each), pick("Full PDF only", "sp_full", not each)])
    return kb(*rows,
              [btn("▶️ Confirm", cb("exm", uid, tid, "ok"), style="primary")],
              [btn(t("btn_back"), cb("exm", uid, tid, "back")), btn(t("btn_cancel"), cb("exm", uid, tid, "cancel"), style="danger")])


async def _confirm(app, chat_id: int, uid: int, sess, task) -> None:
    """``sess.step`` is already ``exam:queued`` (set by the button, so a second tap is stale)."""
    app.sessions.disarm(uid)
    output = sess.prefs.get("output") or "marks"
    task.kind = {"custom": "custom_pdf", "original": "original_pdf", "quiz": "quiz", "slides": "slides_pdf"}.get(output, "marks")
    if output != "marks":
        task.lane = "heavy"  # PDF rendering (spec §10)
    task.data["prefs"] = dict(sess.prefs)
    task.data["sections"] = [list(x) for x in sess.data["summary"]["sections"]]  # names for per-section marking
    from bot.quiz.maker import run_make
    runner = {"custom": pipeline.run_custom, "original": pipeline.run_original, "quiz": run_make,
              "slides": pipeline.run_slides}.get(output, pipeline.run_marks)
    try:
        if output not in ("quiz", "slides"):  # a quiz / slides have no marking step on this card
            await marking.remember(app, sess.data["summary"], sess.prefs)
        pos = await app.tasks.submit(task, lambda tk: runner(app, tk))
    except Exception:
        sess.step = "exam:prefs"  # the card keeps working: Confirm can be tapped again
        app.sessions.arm_timeout(uid, app.settings.get("input_timeout_s"), lambda u: _timeout(app, chat_id, u))
        raise
    first = {"quiz": "🎯 Making your quiz…", "slides": "🖼 Making your slides…"}.get(output, "🧮 Calculating your marks…")
    text = first if pos <= 1 else t("queued", pos=pos)
    await app.sender.edit_text(chat_id, sess.ui_message_id, text)


@guard(anon=True)
async def exam_command(client, message) -> None:
    """/tcs, /edqt — the command is only a hint (auto-detect decides)."""
    user = message.from_user  # None: an anonymous group admin (no one to tie the 🎬 button to)
    rows = helpvideo.rows(client.app, user.id) if user else []
    await client.app.sender.send_message(message.chat.id, t("exam_how_to"), link_preview_options=NO_PREVIEW,
                                         **({"reply_markup": kb(*rows)} if rows else {}))
