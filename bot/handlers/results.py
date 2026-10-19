"""``/results`` — result PDF of an exam's candidates for admins/owner (spec §14.5).

Flow (one message, edited): ``/results [search]`` → pick the exam (paginated) → scope: all shifts or one shift →
marking card (prefilled like the exam flow; admins can set it **per section**) → ✅ Make PDF → a ``results`` task on
the heavy lane: candidates streamed from the DB, marks recomputed, ranked, overall + **shift-wise averages**,
PDF drawn in the process pool (``render``), delivered (dump copy + user) with the averages in the caption.

Callback data: ``res:<uid>:<page|index>:<arg>`` — args ``e`` (exam), ``s`` (scope; obj ``all`` or index),
``pg`` / ``sp`` (exam / shift page), ``links`` (🔗 one URL per shift as .txt), ``t`` (section being edited; obj ``all`` or index), ``p_<v>`` / ``n_<v>``
(marks / negative fraction), ``ok``, ``back``, ``x`` (close).

Depends on: ``exam.results``, ``exam.marking``, ``render.results_pdf``, ``compute``, ``core.delivery``, ``ui``.
Used by: ``handlers/__init__``.
"""

from __future__ import annotations

import html
import logging
import re
import time

from bot.compute import run_cpu
from bot.core.access import guard
from bot.core.delivery import deliver
from bot.exam import marking as marking_mod
from bot.exam import results as R
from bot.exam.marks import fraction_text, scheme_text
from bot.exam import linkcheck
from bot.exam.urls import links_by_lang
from bot.render.results_pdf import render_results_pdf
from bot.render.theme import app_theme
from bot.tasks.lanes import TaskExists
from bot.ui.keyboards import btn, cb, kb, parse_cb
from bot.ui.progress import ProgressMessage
from bot.ui.texts import t

log = logging.getLogger(__name__)
PAGE = 8
_FRACTIONS = {0: "0", 0.25: "¼", 0.3333: "⅓", 0.5: "½"}


def _busy(app, uid: int) -> bool:
    sess = app.sessions.peek(uid)
    return bool(app.tasks.active_for(uid)) or bool(sess and sess.step.startswith("exam:"))


@guard("admin", force_sub=False)
async def results_command(client, message) -> None:
    app, uid = client.app, message.from_user.id
    if _busy(app, uid):
        return await app.sender.send_message(message.chat.id, t("task_exists"), reply_to_message_id=message.id)
    query = (message.text or "").partition(" ")[2].strip()
    groups = await R.catalogue(app.repo, query)
    if not groups:
        return await app.sender.send_message(message.chat.id, t("results_none", query=html.escape(query)) if query else t("results_empty"))
    sess = app.sessions.get(uid)
    sess.step, sess.data = "results:exam", {"res_groups": groups, "res_query": query}
    msg = await app.sender.send_message(message.chat.id, _exams_text(sess, 0), reply_markup=_exams_markup(uid, groups, 0))
    sess.ui_message_id = msg.id
    _arm(app, message.chat.id, uid)


def _arm(app, chat_id: int, uid: int) -> None:
    app.sessions.arm_timeout(uid, app.settings.get("input_timeout_s"), lambda u: _timeout(app, chat_id, u))


async def _timeout(app, chat_id: int, uid: int) -> None:
    sess = app.sessions.peek(uid)
    if not sess or not sess.step.startswith("results:") or sess.step == "results:queued":
        return
    msg_id = sess.ui_message_id
    app.sessions.clear(uid)
    if msg_id:
        await app.sender.edit_text(chat_id, msg_id, t("timeout"))


# ------------------------------------------------------------------ screens
def _exams_text(sess, page: int) -> str:
    groups = sess.data["res_groups"]
    q = sess.data.get("res_query")
    head = "📊 <b>Results</b> — pick an exam" + (f" · 🔎 <i>{html.escape(q)}</i>" if q else "")
    pages = (len(groups) + PAGE - 1) // PAGE
    tail = f"\n<i>Page {page + 1} / {pages} · search: /results &lt;words&gt;</i>" if pages > 1 else \
        "\n<i>Search: /results &lt;words&gt;</i>"
    return f"{head}\n{len(groups)} exam{'s' if len(groups) != 1 else ''} with stored results.{tail}"


def _exams_markup(uid: int, groups, page: int):
    rows = [[btn(f"{g.name[:40]} · 👥 {g.count}", cb("res", uid, str(i), "e"))]
            for i, g in enumerate(groups[page * PAGE:(page + 1) * PAGE], page * PAGE)]
    nav = []
    if page:
        nav.append(btn("◀️", cb("res", uid, str(page - 1), "pg")))
    if (page + 1) * PAGE < len(groups):
        nav.append(btn("▶️", cb("res", uid, str(page + 1), "pg")))
    return kb(*rows, nav, [btn(t("btn_close"), cb("res", uid, "", "x"), style="danger")])


def _scope_text(g: R.ExamGroup) -> str:
    return (f"📊 <b>{html.escape(g.name)}</b>\n👥 {g.count} candidates · 🗓 {len(g.shifts)} shift{'s' if len(g.shifts) != 1 else ''}"
            "\n\nWhich candidates?")


def _scope_markup(uid: int, g: R.ExamGroup, page: int):
    rows = [[btn(f"📚 All shifts · 👥 {g.count}", cb("res", uid, "all", "s"), style="primary")]]
    per = 10
    shown = list(enumerate(g.shifts))[page * per:(page + 1) * per]
    for j in range(0, len(shown), 2):
        rows.append([btn(f"{s.label()} · {s.count}", cb("res", uid, str(i), "s")) for i, s in shown[j:j + 2]])
    nav = []
    if page:
        nav.append(btn("◀️", cb("res", uid, str(page - 1), "sp")))
    if (page + 1) * per < len(g.shifts):
        nav.append(btn("▶️", cb("res", uid, str(page + 1), "sp")))
    return kb(*rows, nav, [btn("🔗 Shift links (.txt)", cb("res", uid, "", "links"))],
              [btn(t("btn_back"), cb("res", uid, "", "back")), btn(t("btn_close"), cb("res", uid, "", "x"), style="danger")])


async def _send_links(app, chat_id: int, uid: int, g: R.ExamGroup) -> None:
    """🔗 One exam URL per shift (no repeats), **a Hindi and an English .txt** (owner 2026-10-19) — admin only (URLs
    are candidate-specific, spec §25). Links whose language is not known yet: ``/links`` → this exam → 🔍."""
    per_lang = await links_by_lang(app.repo, [g.slug])
    unknown = len(per_lang.get("unknown") or [])
    note = (f"{unknown} saved link{'s' if unknown != 1 else ''} without a language yet — /links → this exam → 🔍"
            if unknown else "")
    if not await linkcheck.send_files(app, chat_id, uid, per_lang, g.name, note=note):
        await app.sender.send_message(chat_id, f"🔗 No saved links with a known language for <b>{html.escape(g.name)}</b>"
                                      + (f" — {unknown} to check: /links" if unknown else " yet") + ".")


def _marking(sess) -> dict[str, tuple[float, float]]:
    """Session marking per section ("" = default): (plus, negative **fraction**) — converted only when scoring."""
    return dict(sess.data["res_mark"])


def _scope_label(sess) -> str:
    g, shifts = sess.data["res_group"], sess.data["res_shifts"]
    return "All shifts" if len(shifts) == len(g.shifts) and len(shifts) > 1 else shifts[0].label()


def _mark_text(sess) -> str:
    g = sess.data["res_group"]
    codes = R.section_codes(g.sections)
    mk = sess.data["res_mark"]
    lines = []
    for code, name in zip(codes, g.sections):
        p, f = mk.get(name) or mk[""]
        lines.append(f"{code} · {html.escape(name[:46])} — <b>{scheme_text(p, f)}</b>")
    editing = sess.data.get("res_edit")
    who = "every section" if editing is None else f"section <b>{codes[editing]}</b>"
    src = {"admin": "set for this exam", "rule": "official scheme", "preset": "used before", "file": "from a sheet",
           "default": "default — please check"}.get(sess.data.get("res_src", ""), "")
    return (f"📊 <b>{html.escape(g.name)}</b>\n🗓 {html.escape(_scope_label(sess))}\n\n"
            f"📝 <b>Marking</b>{f'  ·  <i>{src}</i>' if src else ''}\n<blockquote>" + "\n".join(lines) + "</blockquote>\n"
            f"Editing: {who}. Negative = the part of the question's marks lost on a wrong answer.\n"
            "Tap <b>✅ Make PDF</b> when ready.")


def _mark_markup(app, uid: int, sess):
    g = sess.data["res_group"]
    mk = sess.data["res_mark"]
    editing = sess.data.get("res_edit")
    p, f = (mk.get(g.sections[editing]) or mk[""]) if editing is not None else mk[""]
    rows = []
    if len(g.sections) > 1:
        sec_row = [btn(("✅ " if editing is None else "") + "All", cb("res", uid, "all", "t"))]
        sec_row += [btn(("✅ " if editing == i else "") + c, cb("res", uid, str(i), "t")) for i, c in enumerate(R.section_codes(g.sections))]
        rows += [sec_row[i:i + 5] for i in range(0, len(sec_row), 5)]
    rows.append([btn(("✅ " if p == v else "") + f"+{v:g}", cb("res", uid, "", f"p_{v:g}")) for v in app.settings.get("marks.per_question")])
    rows.append([btn(("✅ " if abs(f - v) < 1e-3 else "") + "−" + fraction_text(v), cb("res", uid, "", f"n_{v:g}"))
                 for v in app.settings.get("marks.negative")])
    rows.append([btn("✅ Make PDF", cb("res", uid, "", "ok"), style="success")])
    rows.append([btn(t("btn_back"), cb("res", uid, "", "back")), btn(t("btn_close"), cb("res", uid, "", "x"), style="danger")])
    return kb(*rows)


# ------------------------------------------------------------------ buttons
@guard("admin", force_sub=False)
async def on_button(client, query) -> None:
    app, uid = client.app, query.from_user.id
    data = parse_cb(query.data)
    sess = app.sessions.peek(uid)
    chat_id, msg_id = query.message.chat.id, query.message.id
    if sess is None or not sess.step.startswith("results:") or sess.ui_message_id != msg_id or sess.step == "results:queued":
        return await app.sender.answer(query.id, "This menu has expired.", show_alert=True)
    arg, obj = data.arg, data.obj
    if sess.lock.locked() and arg in ("ok", "links", "s"):  # a double tap while the first one works
        return await app.sender.answer(query.id, "⏳ Please wait…")
    async with sess.lock:
        if sess.step not in _steps_for(arg) or sess.ui_message_id != msg_id:  # stale button (card moved on)
            return await app.sender.answer(query.id)
        await _press(app, query, uid, sess, arg, obj, chat_id, msg_id)


def _steps_for(arg: str) -> tuple[str, ...]:
    if arg in ("pg", "e"):
        return ("results:exam",)
    if arg in ("sp", "s", "links"):
        return ("results:scope",)
    if arg == "back":
        return ("results:scope", "results:marking")
    if arg == "x":
        return ("results:exam", "results:scope", "results:marking")
    return ("results:marking",)  # t p_ n_ ok


async def _press(app, query, uid: int, sess, arg: str, obj: str, chat_id: int, msg_id: int) -> None:
    groups = sess.data["res_groups"]
    if arg == "x":
        await app.sender.answer(query.id)
        app.sessions.clear(uid)
        return await app.sender.edit_text(chat_id, msg_id, "✖️ Closed.")
    if arg == "ok":
        sess.step = "results:queued"  # before any await: a second tap is stale
        await app.sender.answer(query.id)
        return await _submit(app, chat_id, uid, sess)
    if arg == "links":
        await app.sender.answer(query.id, "Preparing the links…")
        _arm(app, chat_id, uid)
        return await _send_links(app, chat_id, uid, sess.data["res_group"])
    await app.sender.answer(query.id)
    _arm(app, chat_id, uid)
    text = markup = None
    if arg == "pg":
        text, markup = _exams_text(sess, int(obj)), _exams_markup(uid, groups, int(obj))
    elif arg == "e":
        g = groups[int(obj)]
        sess.data["res_group"], sess.step = g, "results:scope"
        text, markup = _scope_text(g), _scope_markup(uid, g, 0)
    elif arg == "sp":
        g = sess.data["res_group"]
        text, markup = _scope_text(g), _scope_markup(uid, g, int(obj))
    elif arg == "s":
        g = sess.data["res_group"]
        sess.data["res_shifts"] = list(g.shifts) if obj == "all" else [g.shifts[int(obj)]]
        if "res_mark" not in sess.data or sess.data.get("res_mark_for") != g.slug:
            sug = await marking_mod.suggest(app, {"exam_name": g.name})
            neg = min(app.settings.get("marks.negative") or list(_FRACTIONS), key=lambda f: abs(f - sug["neg"]))
            sess.data.update(res_mark={"": (float(sug["plus"]), float(neg))}, res_src=sug.get("source"), res_mark_for=g.slug,
                             res_edit=None)
        sess.step = "results:marking"
    elif arg == "t":
        sess.data["res_edit"] = None if obj == "all" else int(obj)
    elif arg.startswith(("p_", "n_")):
        _set_marking(sess, plus=float(arg[2:]) if arg.startswith("p_") else None,
                     neg=float(arg[2:]) if arg.startswith("n_") else None)
    elif arg == "back":
        if sess.step == "results:marking":
            g = sess.data["res_group"]
            sess.step = "results:scope"
            text, markup = _scope_text(g), _scope_markup(uid, g, 0)
        else:
            sess.step = "results:exam"
            text, markup = _exams_text(sess, 0), _exams_markup(uid, groups, 0)
    if text is None:
        text, markup = _mark_text(sess), _mark_markup(app, uid, sess)
    await app.sender.edit_text(chat_id, msg_id, text, reply_markup=markup)


def _set_marking(sess, plus: float | None = None, neg: float | None = None) -> None:
    g, mk, editing = sess.data["res_group"], sess.data["res_mark"], sess.data.get("res_edit")
    sess.data["res_src"] = "user"
    if editing is None:  # every section: one scheme, per-section overrides dropped
        p, f = mk[""]
        sess.data["res_mark"] = {"": (plus if plus is not None else p, neg if neg is not None else f)}
        return
    name = g.sections[editing]
    p, f = mk.get(name) or mk[""]
    mk[name] = (plus if plus is not None else p, neg if neg is not None else f)


# ------------------------------------------------------------------ task
async def _submit(app, chat_id: int, uid: int, sess) -> None:
    app.sessions.disarm(uid)
    try:
        task = app.tasks.create(uid, "results", "heavy", app.access.priority(uid), title="results", chat_id=chat_id)
    except TaskExists:
        sess.step = "results:marking"  # the card stays usable once the other task ends
        _arm(app, chat_id, uid)
        return await app.sender.send_message(chat_id, t("task_exists"))
    g = sess.data["res_group"]
    task.data.update(ui_message_id=sess.ui_message_id, group=g, shifts=sess.data["res_shifts"], marking=_marking(sess),
                     scope=_scope_label(sess))
    sess.step, sess.task_id = "results:queued", task.task_id
    pos = await app.tasks.submit(task, lambda tk: run_results(app, tk))
    text = "📊 <b>Building the result PDF…</b>" if pos <= 1 else t("queued", pos=pos)
    await app.sender.edit_text(chat_id, sess.ui_message_id, text)


def _file_name(name: str, scope: str) -> str:
    base = re.sub(r'[\\/:*?"<>|\r\n\t]+', " ", f"Results - {name} ({scope})").strip()
    return re.sub(r"\s+", " ", base)[:140] + ".pdf"


async def run_results(app, task) -> None:
    """Heavy-lane runner: stream + rank (bot process, compact rows) → PDF (pool, ``render``) → deliver."""
    progress = ProgressMessage(app.sender, task.chat_id, interval_s=app.settings.get("edit_interval_s"), task=task)
    progress.attach(task.data["ui_message_id"])
    try:
        g, shifts, mk, scope = task.data["group"], task.data["shifts"], task.data["marking"], task.data["scope"]
        progress.set("📥 <b>Reading the candidates…</b>")
        show_name = bool(app.settings.get("results.show_name"))
        table = await R.collect(app.repo, g, shifts, mk, show_name=show_name)
        if not table.rows:
            await progress.finish("📭 No stored results for this choice.")
            return
        stats = table.stats()
        progress.set(f"🧾 <b>Drawing the PDF…</b>  ·  👥 {len(table.rows)} candidates")
        ctx = R.pdf_context(table, stats, scope=scope, generated=time.strftime("%d-%m-%Y %H:%M"),
                            theme=app_theme(app), show_name=show_name,
                            name_mask=app.settings.get("privacy.mask_name"))
        task.workdir.mkdir(parents=True, exist_ok=True)
        out = task.workdir / _file_name(g.name, scope)
        est = 40 + len(table.rows) // 200  # MB: fonts + page buffers; grows slowly with rows
        info = await run_cpu(app, "render", task.task_id, render_results_pdf, ctx, str(out), est_mb=est)
        del ctx
        caption = R.results_caption(table, stats, scope)
        progress.set("📤 <b>Sending…</b>")
        who = f"👤 <code>{task.user_id}</code> · task <code>{task.task_id}</code> · {info['pages']} pages"
        task.outputs.append(await deliver(app, task, out, caption=f"{caption}\n{who}"[:1024], user_caption=caption))
        await progress.finish(f"✅ <b>Done!</b> {len(table.rows)} candidates · {info['pages']} pages — the PDF is above.")
    finally:
        progress.close()
