"""Settings panels (spec §7, §23): which settings each panel shows and how one page of a panel is drawn.

Depends on: ``core.settings`` (values, ``CHOICES``), ``ui.keyboards``. Used by: ``handlers/settings.py``.

Public API
----------
- ``PANELS`` — ``{"es" | "ps" | "wm" | "bot": {"title", "role", "pages": [(name, [Field, …]), …]}}``.
- ``Field(key, label, kind, choices=None, names=None, hint="")`` — kind: ``bool`` (On/Off) · ``choice`` (a row of
  values) · ``color`` (🎨 → a grid of named colours + ✏️ any #hex; owner 2026-10-13 "colour grid bhi dalo") ·
  ``number`` / ``text`` / ``list`` (✏️ → type a value).
- ``render(app, uid, panel, page) -> (text, markup)``; ``render_colors(app, uid, panel, page, i)`` — the colour grid
  of one ``color`` field; ``show(value, field)`` — a value as the admin reads it.
- ``parse(field, default, text)`` — a typed value → what ``settings.set`` takes (``SettingError`` on nonsense).

Only settings the bot really uses are shown: OCR modes come with P-OCR, PDF-tool limits with P6, the watermark
image with its renderer (owner: no dead buttons).

Card style (owner 2026-10-13, the prefs card): a title button above each setting, the current value **green with
✅**, others plain; page tabs on top the same way. Numbers and texts show their value on the ✏️ button.
"""

from __future__ import annotations

import html
from dataclasses import dataclass

from bot.core.settings import CHOICES, DEFAULTS, SettingError
from bot.db.updates import get_path
from bot.ui.keyboards import btn, cb, kb
from bot.ui.texts import t


@dataclass(frozen=True)
class Field:
    key: str
    label: str
    kind: str  # bool | choice | number | text | list
    choices: tuple = ()
    names: tuple = ()  # display names for ``choices``
    hint: str = ""


F = Field
# colour grids: (hex, name) — dark colours for text, light ones for backgrounds
INK = (("#000000", "⬛ Black"), ("#616161", "◾ Grey"), ("#1a237e", "🟦 Navy blue"), ("#1565c0", "🔵 Blue"),
       ("#2e7d32", "🟩 Green"), ("#c62828", "🟥 Red"), ("#6a1b9a", "🟪 Purple"), ("#e65100", "🟧 Orange"),
       ("#5d4037", "🟫 Brown"))
PALE = (("#ffffff", "⬜ White"), ("#eeeeee", "🩶 Light grey"), ("#e3f2fd", "🩵 Light blue"), ("#e8f5e9", "💚 Light green"),
        ("#fffde7", "💛 Light yellow"), ("#fff3e0", "🧡 Light orange"), ("#fce4ec", "🩷 Light pink"),
        ("#f3e5f5", "💜 Light purple"))
WM = (("#9e9e9e", "🩶 Light grey"),) + INK


def C(key: str, label: str, palette=INK, hint: str = "") -> Field:
    return Field(key, label, "color", tuple(h for h, _n in palette), tuple(n for _h, n in palette), hint)


PANELS: dict[str, dict] = {
    "es": {"title": "🧾 Exam settings", "role": "admin", "pages": [
        ("Exam", [
            F("feature.custom_pdf_public", "Custom PDF for everyone", "bool", hint="Off = only admins can make Custom PDFs"),
            F("fetch.other_language", "EDQT links: also fetch the other language", "bool",
              hint="Hindi + English versions of every question (Both = 2 PDFs)"),
            F("input_timeout_s", "Close an unanswered card after (seconds)", "number"),
        ]),
        ("Marks", [
            F("marks.per_question", "Marks buttons", "list", hint="Numbers, e.g. 1, 2, 3"),
            F("marks.negative", "Minus buttons (part of the marks)", "list", hint="Fractions as decimals, e.g. 0, 0.25, 0.3333, 0.5"),
        ]),
        ("Privacy", [
            F("privacy.mask_name", "Names on scorecards", "choice", ("light", "strong", "off"),
              ("Light ARU*", "Strong A***", "Full name")),
            F("privacy.mask_roll", "Hide roll numbers", "bool"),
            F("results.show_name", "Names in result PDFs", "bool"),
        ]),
    ]},
    "ps": {"title": "📄 PDF settings", "role": "admin", "pages": [
        ("📐 Layout", [
            F("pdf.columns", "Columns in custom PDFs (for users)", "choice", (2, 1),
              ("2 columns", "1 column"),
              hint="2 columns: a question with a wide picture goes across the page"),
            F("pdf.font", "English font in custom PDFs", "choice", ("poppins", "noto"), ("Poppins", "Noto Sans"),
              hint="Hindi always uses Noto Sans Devanagari (never distorted)"),
            F("pdf.header_text", "Header text", "text"), F("pdf.header_url", "Header link", "text"),
            F("pdf.footer_text", "Footer text", "text"), F("pdf.footer_url", "Footer link", "text"),
        ]),
        ("🎨 Colours", [
            C("pdf.colors.question_no", "Question number"),
            C("pdf.colors.question", "Question"),
            C("pdf.colors.options", "Options"),
            C("pdf.colors.label", "Option labels (a) (b) (c) (d)"),
            C("pdf.colors.correct", "Answer line (Answer: b) and its strip"),
            C("pdf.colors.chosen_right", "Chosen option, right"),
            C("pdf.colors.chosen_wrong", "Chosen option, wrong"),
            C("pdf.colors.explain_fill", "Explanation box", PALE, hint="A light colour: the box is see-through"),
            C("pdf.colors.explain_head", "Explanation heading and bar"),
            C("pdf.colors.banner_border", "Banner border"),
            C("pdf.colors.banner_fill", "Banner background", PALE),
            C("pdf.colors.header", "Header link"),
            C("pdf.colors.footer", "Footer link"),
        ]),
        ("🃏 Quiz cards", [
            C("card.colors.title", "Card title and branding"),
            C("card.colors.accent", "Letters A–D, Q number, EXPLANATION"),
            C("card.colors.header_fill", "Top strip background", PALE),
            C("card.colors.pill", "Option strips", PALE),
            C("card.colors.answer", "Right answer (explanation card)"),
            C("card.colors.explain_fill", "Explanation box", PALE, hint="A light colour: the box is see-through"),
        ]),
    ]},
    "wm": {"title": "💧 Watermark", "role": "admin", "pages": [
        ("Watermark", [
            F("watermark.enabled", "Watermark", "bool"),
            F("watermark.position", "Direction", "choice", ("diagonal", "horizontal", "vertical"), ("↗️ Diagonal", "➡️ Level", "⬆️ Up")),
            F("watermark.text", "Text", "text", hint="Empty = the branding name"),
            F("watermark.opacity", "Strength (0.02 – 0.3)", "number", hint="0.08 is light"),
            C("watermark.color", "Colour", WM),
        ]),
    ]},
    "qz": {"title": "🎯 Quiz settings", "role": "admin", "pages": [
        ("Quiz", [
            F("feature.quiz_public", "Everyone can make quizzes", "bool", hint="Off = only admins can tap 🎯 Make Quiz"),
            F("quiz.default_question_time_s", "Seconds per question (default)", "number"),
            F("quiz.pause_timeout_min", "A paused quiz stops after (minutes)", "number"),
            F("quiz.branding_probability", "Branding under a poll (0 – 1)", "number", hint="0.15 = about 1 poll in 7"),
            F("feature.json_public", "Everyone can use /json", "bool", hint="Off = only admins make PDFs / quizzes from JSON files"),
        ]),
        ("Auto quiz", [
            F("autoquiz.enabled", "Auto quiz in groups", "bool",
              hint="A random question every few minutes in groups where the bot is admin (each group: /groups)"),
            F("autoquiz.interval_min", "Every (minutes)", "number"),
            F("autoquiz.open_period_s", "Poll open for (seconds, 0 = until closed)", "number"),
        ]),
        ("Web App", [
            F("webapp.enabled", "Web App buttons", "bool", hint="Play inside Telegram (needs the tunnel — see /stats)"),
            F("webapp.tunnel", "Tunnel", "choice", ("quick", "named", "off"), ("Quick (no domain)", "Named", "Off"),
              hint="Changes apply after /restart"),
            F("webapp.miniapp", "Mini App short name", "text",
              hint="The short name you gave BotFather (/newapp), e.g. quiz — see 📱 Mini App setup"),
        ]),
    ]},
    "bot": {"title": "🤖 Bot settings", "role": "owner", "pages": [
        ("General", [
            F("public_enabled", "Open to everyone", "bool", hint="Off = admins only (same as /on, /off)"),
            F("channel_link", "Channel link on /start", "text"),
            F("admin_contact", "Admin contact on /start", "text"),
            F("force_sub.channels", "Must-join channels", "list", hint="IDs (-100…) or @usernames, comma-separated; empty = none"),
        ]),
        ("Speed", [
            F("pool.workers", "Worker processes", "number", hint="A number, or auto"),
            F("queue.heavy_concurrent", "PDF tasks at once", "number", hint="A number, or auto (never below 2)"),
            F("queue.light_concurrent", "Light tasks at once", "number", hint="A number, or auto"),
            F("fetch.parallel_links", "Links opened at once", "number"),
            F("broadcast.concurrency", "Broadcast speed (parallel sends)", "number"),
        ]),
        ("Network", [
            F("fetch.mode", "Fetching links", "choice", ("direct", "remote", "auto"), ("Direct", "Remote", "Auto")),
            F("fetch.engine", "Download engine", "choice", ("auto", "chrome", "wget", "aiohttp"),
              ("Auto (Chrome)", "Chrome", "wget + Chrome", "Python")),
            F("http.proxy", "Proxy", "text", hint="http://host:port, or empty"),
            F("tg.fast_download_connections", "Connections per Telegram download", "number"),
        ]),
        ("Limits", [
            F("governor.free_soft_mb", "Slow down below free RAM (MB)", "number"),
            F("governor.free_hard_mb", "Stop new work below free RAM (MB)", "number"),
            F("governor.disk_min_free_mb", "Keep free disk (MB)", "number"),
            F("governor.job_timeout_s", "One CPU job at most (seconds)", "number"),
            F("shutdown.grace_s", "Restart: wait for tasks (seconds)", "number"),
        ]),
    ]},
}
for _k, _v in CHOICES.items():  # every choice field is checked against the settings' own list
    for _p in PANELS.values():
        for _name, _fields in _p["pages"]:
            for _f in _fields:
                assert _f.key != _k or set(_f.choices) <= set(_v), _f.key


def show(value, field: Field) -> str:
    if field.kind == "bool":
        return "on" if value else "off"
    if field.kind == "choice":
        return dict(zip(field.choices, field.names)).get(value, str(value))
    if field.kind == "color":
        value = str(value or get_path(DEFAULTS, field.key, "") or "").lower()  # "" (cleared) = the default colour
        return dict(zip(field.choices, field.names)).get(value, value or "default")
    if field.kind == "list":
        return ", ".join(f"{x:g}" if isinstance(x, float) else str(x) for x in value) if value else "none"
    if value in ("", None):
        return "—"
    return f"{value:g}" if isinstance(value, float) else str(value)


def parse(field: Field, text: str):
    """A typed value → the stored value. ``-`` or empty clears a text; ``auto`` where the default is auto."""
    default = get_path(DEFAULTS, field.key)
    text = (text or "").strip()
    if field.kind in ("text", "color"):  # a colour is checked by settings.set
        return "" if text in ("", "-") else text
    if field.kind == "list":
        parts = [p.strip() for p in text.replace(";", ",").split(",") if p.strip() and p.strip() != "-"]
        sample = default[0] if default else None
        out = []
        for p in parts:
            if isinstance(sample, (int, float)) and not isinstance(sample, bool):
                try:
                    v = float(p)
                except ValueError as e:
                    raise SettingError(f"'{p}' is not a number") from e
                out.append(int(v) if v.is_integer() and isinstance(sample, int) and all(isinstance(x, int) for x in default) else v)
            else:  # channels: numeric IDs as numbers, @names as text
                out.append(int(p) if p.lstrip("-").isdigit() else p)
        return out
    if text.lower() == "auto" and default == "auto":
        return "auto"
    try:
        v = float(text)
    except ValueError as e:
        raise SettingError("send a number" + (" or auto" if default == "auto" else "")) from e
    if v < 0:
        raise SettingError("send a number of 0 or more")
    return v if isinstance(default, float) else int(v)


def render(app, uid: int, panel: str, page: int = 0):
    p = PANELS[panel]
    page = max(0, min(page, len(p["pages"]) - 1))
    name, fields = p["pages"][page]
    s = app.settings
    lines = [f"{html.escape(f.label)}: <b>{html.escape(show(s.get(f.key), f))}</b>" for f in fields]
    others = [n for i, (n, _f) in enumerate(p["pages"]) if i != page]
    text = (f"<b>{p['title']}</b>" + (f"  ·  {name}" if len(p["pages"]) > 1 else "")
            + "\n<blockquote>" + "\n".join(lines) + "</blockquote>\n"
            "👇 <b>✅ green</b> is the current value — tap another to change it; ✏️ asks you to type one; 🎨 opens a "
            "colour grid." + (f"\n<i>More on the {' / '.join(others)} tab above.</i>" if others else ""))

    def pick(label, arg, on):
        return btn(("✅ " if on else "") + label, cb("set", uid, panel, arg), style="success" if on else None)

    rows = []
    if len(p["pages"]) > 1:
        tabs = [pick(n, f"pg_{i}", i == page) for i, (n, _) in enumerate(p["pages"])]
        rows += [tabs[i:i + 4] for i in range(0, len(tabs), 4)]
    for i, f in enumerate(fields):
        v = s.get(f.key)
        if f.kind == "bool":
            rows.append([btn(f"— {f.label} —", cb("set", uid, panel, f"h_{page}_{i}"))])
            rows.append([pick("On", f"b_{page}_{i}_1", bool(v)), pick("Off", f"b_{page}_{i}_0", not v)])
        elif f.kind == "choice":
            rows.append([btn(f"— {f.label} —", cb("set", uid, panel, f"h_{page}_{i}"))])
            rows.append([pick(n, f"c_{page}_{i}_{j}", v == c) for j, (c, n) in enumerate(zip(f.choices, f.names))])
        elif f.kind == "color":
            rows.append([btn(f"🎨 {f.label}: {show(v, f)}"[:60], cb("set", uid, panel, f"k_{page}_{i}"))])
        else:
            rows.append([btn(f"✏️ {f.label}: {show(v, f)}"[:60], cb("set", uid, panel, f"e_{page}_{i}"))])
    return text, kb(*rows, [btn(t("btn_home"), cb("nav", uid, "home"))])


def render_colors(app, uid: int, panel: str, page: int, i: int):
    """One colour setting as a grid of named colours (the current one ✅ green), ✏️ any #hex, ⬅️ back."""
    f = PANELS[panel]["pages"][page][1][i]
    v = str(app.settings.get(f.key) or get_path(DEFAULTS, f.key) or "").lower()  # "" = the default colour
    text = (f"🎨 <b>{html.escape(f.label)}</b>\nNow: <b>{html.escape(show(v, f))}</b>"
            + (f" <code>{html.escape(v)}</code>" if v else "")
            + "\n👇 Tap a colour — it is saved at once. ✏️ lets you type any colour like <code>#1a237e</code>.")
    per_row = 2 if max(len(n) for n in f.names) > 13 else 3
    cells = [btn(("✅ " if c == v else "") + n, cb("set", uid, panel, f"g_{page}_{i}_{j}"), style="success" if c == v else None)
             for j, (c, n) in enumerate(zip(f.choices, f.names))]
    rows = [cells[k:k + per_row] for k in range(0, len(cells), per_row)]
    rows.append([btn("✏️ Other colour (#hex)", cb("set", uid, panel, f"e_{page}_{i}"))])
    return text, kb(*rows, [btn(t("btn_back"), cb("set", uid, panel, f"pg_{page}"))])
