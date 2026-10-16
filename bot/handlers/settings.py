"""Settings commands (spec §7, §23): /es (exam), /ps (PDF), /wm (watermark), /qz (quiz) for admins, /bot for the owner.

Depends on: ``ui.settings_panel`` (fields + card), ``core.settings`` (validation, live subscribers),
``handlers.waits`` (typed values). Used by: ``handlers/__init__.py``, the Admin Panel.

A tap on On/Off or a choice saves at once (``settings.set`` → Mongo + JSON + live subscribers) and redraws the
card. ✏️ asks for a typed value (``set:edit``): a bad value gets the reason and the question stays open.
/bot also has `🔑 config.env` (owner): bootstrap values — secrets are only shown as "set" / "not set"; a new value
is written to ``.env``, the owner's message holding it is deleted, and the change needs a restart (button) —
except ``LIVE_ENV`` (fetch server URL / token, proxy: owner 2026-10-14 "han kro"), which apply at once.
``🧪 Test fetch server`` (``fetchck``): reachable? token accepted? (``net.service.check_server``).
`🧪 Check must-join channels` (and saving the channel list) reports each channel: working, or why not (the bot
must be an admin there).

Callback data: ``set:<uid>:<panel>:<arg>`` — ``pg_<page>`` · ``h_<page>_<i>`` (title: hint) · ``b_<page>_<i>_<0|1>``
· ``c_<page>_<i>_<choice>`` · ``k_<page>_<i>`` (colour grid) · ``g_<page>_<i>_<colour>`` (a grid colour → back to
the page) · ``e_<page>_<i>`` (type a value) · ``env`` · ``ev_<i>`` · ``fetchck`` · ``restart`` · ``mini`` / ``minick`` (/qz → Web App
→ 📱 Mini App setup: what is missing for BotFather Mini App links, the steps, a live check of the address).
"""

from __future__ import annotations

import dataclasses
import html
import os

from bot.core.access import guard
from bot.core.config import env_file_path
from bot.core.sender import NO_PREVIEW
from bot.core.settings import SettingError
from bot.handlers.waits import WAITERS
from bot.ui import settings_panel as sp
from bot.ui.keyboards import btn, cb, kb, parse_cb
from bot.ui.texts import t

ENV_FILE = env_file_path()  # $ENV_FILE (Docker: in the data volume) or .env
# (key, label, secret) — what /bot → config.env may change. OWNER_ID and DOWNLOAD_ROOT are left to the file itself.
ENV_KEYS = (("BOT_TOKEN", "Main bot token", True), ("IMAGE_BOT_TOKENS", "Uploader bot tokens (comma-separated)", True),
            ("API_ID", "API ID", False), ("API_HASH", "API hash", True), ("MONGO_URI", "MongoDB URI", True),
            ("DUMP_CHANNEL_ID", "Dump channel ID", False), ("IMAGE_DUMP_CHANNEL_ID", "Image dump channel ID", False),
            ("QUIZ_DUMP_CHANNEL_ID", "Quiz dump channel ID (new quizzes; empty = dump)", False),
            ("BRANDING", "Branding name", False), ("HTTP_PROXY", "Proxy", True), ("FETCH_API_URL", "Fetch server URL", False),
            ("FETCH_API_TOKEN", "Fetch server token", True), ("TESSERACT_CMD", "Tesseract path", False),
            ("FORCE_SUB_CHANNELS", "Must-join channels (IDs or @names)", False),
            ("WEBAPP_BASE_URL", "Web App URL", False), ("CLOUDFLARED_TOKEN", "Cloudflare tunnel token", True),
            ("RUNTIME_PROFILE", "Runtime profile (empty = auto)", False))
# read on every use (net.service) → a new value is used at once, no restart
LIVE_ENV = {"FETCH_API_URL": "fetch_api_url", "FETCH_API_TOKEN": "fetch_api_token", "HTTP_PROXY": "http_proxy"}


def _allowed(app, uid: int, panel: str) -> bool:
    role = sp.PANELS[panel]["role"]
    return uid == app.config.owner_id if role == "owner" else app.access.is_admin(uid)


async def _open(client, message, panel: str) -> None:
    app, uid = client.app, message.from_user.id
    if not _allowed(app, uid, panel):
        return await app.sender.send_message(message.chat.id, "🔒 Only the owner changes these settings.")
    text, markup = _card(app, uid, panel, 0)
    await app.sender.send_message(message.chat.id, text, reply_markup=markup)


def _card(app, uid: int, panel: str, page: int):
    text, markup = sp.render(app, uid, panel, page)
    if panel == "bot":
        rows = markup.inline_keyboard
        if page == 0:
            rows.insert(len(rows) - 1, [btn("🧪 Check must-join channels", cb("set", uid, "bot", "fsub"))])
        rows.insert(len(rows) - 1, [btn("🔑 config.env (tokens, IDs)", cb("set", uid, "bot", "env"))])
    if panel == "qz" and page == 1:  # owner 2026-10-13: "auto quiz test krne ka button"
        rows = markup.inline_keyboard
        rows.insert(len(rows) - 1, [btn("🧪 Send one question now (test)", cb("set", uid, "qz", "aqtest"), style="primary")])
    if panel == "qz" and page == 2:
        rows = markup.inline_keyboard
        rows.insert(len(rows) - 1, [btn("📱 Mini App setup", cb("set", uid, "qz", "mini"), style="primary")])
    return text, markup


async def _health(url: str) -> str:
    """'' when ``<url>/api/health`` answers, else what went wrong."""
    import aiohttp
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as s:
            async with s.get(f"{url}/api/health") as r:
                return "" if r.status == 200 else f"HTTP {r.status}"
    except Exception as e:
        return e.__class__.__name__


def _miniapp_card(app, uid: int, check: str | None = None):
    """/qz → Web App → 📱 Mini App setup (spec §17.7.1): a BotFather Mini App link opens a quiz inside Telegram
    from any chat; it needs a fixed address (named tunnel / own HTTPS address) and the short name."""
    from bot.quiz import ui
    web = getattr(app, "web", None)
    mode = app.settings.get("webapp.tunnel") or "quick"
    running = getattr(getattr(web, "tunnel", None), "mode", None)
    base = web.base_url if web is not None else None
    fixed = bool(web is not None and web.fixed_url)
    name = str(app.settings.get("webapp.miniapp") or "").strip()
    me = app.me.username
    port = getattr(web, "port", None) or app.config.webapp_port
    missing = []
    if not app.settings.get("webapp.enabled"):
        missing.append("Web App buttons are off (above)")
    if not fixed:
        missing.append("a fixed address — the quick tunnel's address changes" if (running or mode) == "quick"
                       else "the Web App URL")
    elif not base:
        missing.append("the address is not up")
    if not ui.MINIAPP_NAME.match(name):
        missing.append("the short name")
    lines = ["📱 <b>Mini App link</b>",
             f"<i>A link like</i> <code>t.me/{me}/{html.escape(name or 'quiz')}?startapp=qz_…</code> <i>opens a quiz inside "
             "Telegram from anywhere — groups, channels, any chat (a 📱 Web App button works only in private chats).</i>",
             "", "<b>Now</b>",
             f"• Tunnel: <b>{html.escape(mode)}</b>" + (" ✅" if fixed else " ⚠️")
             + (f" <i>(running: {html.escape(running)} — /restart applies it)</i>" if running and running != mode else ""),
             f"• Address: {html.escape(base) if base else '—'}",
             f"• Short name: {html.escape(name) if name else '—'}" + ("" if not name or ui.MINIAPP_NAME.match(name) else " ⚠️ letters, digits, _ only")]
    if check is not None:
        lines.append(f"• Check: {'✅ the address answers' if not check else '❌ ' + html.escape(check)}")
    lines.append("✅ <b>Mini App links are on</b> — quiz pages, the quiz list, quiz dump posts and sharing show them."
                 if not missing else "⚠️ <b>Off</b> — missing: " + "; ".join(missing) + ".")
    lines += ["", "<b>Set up once</b>",
              "1️⃣ <b>Fixed address</b> (free; your domain on Cloudflare): Cloudflare Zero Trust → Networks → Tunnels → "
              "Create a tunnel (Cloudflared) → copy its <b>token</b>; Public hostname e.g. <code>quiz.your-domain</code> → "
              f"service <code>HTTP 127.0.0.1:{port}</code>. Owner: /bot → 🔑 config.env → <b>Cloudflare tunnel token</b> "
              "and <b>Web App URL</b> (<code>https://quiz.your-domain</code>); here: Tunnel → <b>Named</b>; then /restart.",
              f"2️⃣ <b>@BotFather</b> → /newapp → @{me} → title, description, a 640×360 photo (GIF: /empty) → Web App URL "
              f"<code>{html.escape(base or 'https://quiz.your-domain')}/app</code> → short name, e.g. <code>quiz</code>.",
              "3️⃣ Here: ✏️ <b>Mini App short name</b> → send it."]
    markup = kb([btn("🧪 Check the address", cb("set", uid, "qz", "minick"))],
                [btn("⬅️ Back", cb("set", uid, "qz", "pg_2"))])
    return "\n".join(lines), markup


@guard("admin", force_sub=False)
async def es_command(client, message) -> None:
    await _open(client, message, "es")


@guard("admin", force_sub=False)
async def ps_command(client, message) -> None:
    await _open(client, message, "ps")


@guard("admin", force_sub=False)
async def wm_command(client, message) -> None:
    await _open(client, message, "wm")


@guard("admin", force_sub=False)
async def qz_command(client, message) -> None:
    await _open(client, message, "qz")


@guard("owner", force_sub=False)
async def bot_command(client, message) -> None:
    await _open(client, message, "bot")


def _field(panel: str, page: int, i: int) -> sp.Field | None:
    try:
        return sp.PANELS[panel]["pages"][page][1][i]
    except IndexError:  # a card from before an update changed the panel
        return None


@guard("admin", force_sub=False)
async def on_button(client, query) -> None:
    app, uid = client.app, query.from_user.id
    data = parse_cb(query.data)
    panel, arg = data.obj, data.arg
    chat_id, msg_id = query.message.chat.id, query.message.id
    if panel not in sp.PANELS or not _allowed(app, uid, panel):
        return await app.sender.answer(query.id, "🔒 Only the owner changes these settings.", show_alert=True)
    parts = arg.split("_")
    page = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
    note = None
    if arg == "env":
        await app.sender.answer(query.id)
        text, markup = _env_card(app, uid)
        return await app.sender.edit_text(chat_id, msg_id, text, reply_markup=markup)
    if arg.startswith("ev_"):
        key, label, secret = ENV_KEYS[int(arg[3:])]
        sess = app.sessions.get(uid)
        sess.step, sess.data["env_key"], sess.ui_message_id = "set:env", key, msg_id
        await app.sender.answer(query.id)
        return await app.sender.send_message(chat_id, f"🔑 Send the new <b>{html.escape(label)}</b> (<code>{key}</code>), or "
                                             "<code>-</code> to clear it." + ("\n<i>Your message is deleted right after I "
                                                                             "read it.</i>" if secret else "") + "\n/cancel to stop.")
    if arg == "fetchck":
        await app.sender.answer(query.id, "Checking…")
        return await app.sender.send_message(chat_id, await _fetch_check_text(app),
                                             reply_markup=kb([btn("🔁 Test again", cb("set", uid, "bot", "fetchck"))]))
    if arg == "fsub":
        await app.sender.answer(query.id, "Checking…")
        return await _send_fsub_check(app, chat_id)
    if arg in ("mini", "minick") and panel == "qz":
        check = None
        if arg == "minick":
            web = getattr(app, "web", None)
            if web is None or not web.base_url:
                return await app.sender.answer(query.id, "No Web App address yet — see step 1.", show_alert=True)
            await app.sender.answer(query.id, "Checking…")
            check = await _health(web.base_url)
        else:
            await app.sender.answer(query.id)
        text, markup = _miniapp_card(app, uid, check)
        return await app.sender.edit_text(chat_id, msg_id, text, reply_markup=markup, link_preview_options=NO_PREVIEW)
    if arg == "aqtest":
        if app.autoquiz is None:
            return await app.sender.answer(query.id, "Auto quiz is not running.", show_alert=True)
        await app.sender.answer(query.id, "🎲 Sending to the groups…")
        try:
            await app.autoquiz.tick()
        except Exception as e:
            app.dump.report_error(e, "autoquiz test")
        r = app.autoquiz.last
        if not r.get("groups"):
            text = ("🎲 <b>Auto quiz test</b> — no group qualifies.\n<i>A group needs: the bot as admin, auto quiz not "
                    "turned off for it (/groups → its card)" + (", and /auth while the bot is /off" if not app.settings.get("public_enabled") else "")
                    + ".</i>")
        elif not r.get("quizzes"):
            text = "🎲 <b>Auto quiz test</b> — there is no quiz yet. Make one first (🎯 Make Quiz or /json)."
        else:
            text = (f"🎲 <b>Auto quiz test</b>\n✅ Sent to <b>{r['sent']}</b> of {r['groups']} groups"
                    + (f" · ⏳ {r['busy']} busy with a quiz" if r.get("busy") else "")
                    + (f" · ❌ {r['failed']} failed (see the log)" if r.get("failed") else ""))
        return await app.sender.send_message(chat_id, text)
    if arg == "restart":
        await app.sender.answer(query.id)
        await app.sender.edit_text(chat_id, msg_id, "♻️ Restarting to apply config.env… (database untouched)")
        return app.request_stop(f"config.env changed by {uid}", restart=True)
    keyed = arg[:2] in ("h_", "b_", "c_", "e_", "k_", "g_")
    f = _field(panel, page, int(parts[2])) if keyed else None
    if keyed and f is None:
        return await app.sender.answer(query.id, "This menu has changed — open it again.", show_alert=True)
    if arg.startswith("h_"):
        return await app.sender.answer(query.id, f.hint or f.label)
    if arg.startswith("k_"):
        await app.sender.answer(query.id)
        text, markup = sp.render_colors(app, uid, panel, page, int(parts[2]))
        return await app.sender.edit_text(chat_id, msg_id, text, reply_markup=markup)
    if arg.startswith(("b_", "c_", "g_")):
        value = parts[3] == "1" if arg[0] == "b" else f.choices[int(parts[3])]
        try:
            await app.settings.set(f.key, value, by=uid)
            note = f"{f.label}: {sp.show(value, f)}"
            app.dump.notify(f"⚙️ <code>{f.key}</code> → <b>{html.escape(sp.show(value, f))}</b> by <code>{uid}</code>")
        except SettingError as e:
            return await app.sender.answer(query.id, f"⚠️ {e}", show_alert=True)
    elif arg.startswith("e_"):
        sess = app.sessions.get(uid)
        sess.step, sess.ui_message_id = "set:edit", msg_id
        sess.data["set_edit"] = (panel, page, int(parts[2]))
        await app.sender.answer(query.id)
        cur = sp.show(app.settings.get(f.key), f)
        return await app.sender.send_message(chat_id, f"✏️ <b>{html.escape(f.label)}</b>\nNow: <code>{html.escape(cur)}</code>\n"
                                             + (f"<i>{html.escape(f.hint)}</i>\n" if f.hint else "")
                                             + ("Send the new text, or <code>-</code> to clear it." if f.kind == "text"
                                                else "Send a colour like <code>#1a237e</code>, or <code>-</code> for the "
                                                "default." if f.kind == "color" else "Send the new value.")
                                             + "\n/cancel to stop.")
    await app.sender.answer(query.id, note)
    text, markup = _card(app, uid, panel, page)
    await app.sender.edit_text(chat_id, msg_id, text, reply_markup=markup)


async def _take_value(app, message, sess) -> None:
    panel, page, i = sess.data["set_edit"]
    f = _field(panel, page, i)
    uid = message.from_user.id
    try:
        value = await app.settings.set(f.key, sp.parse(f, message.text or ""), by=uid)
    except SettingError as e:
        return await app.sender.send_message(message.chat.id, f"⚠️ {html.escape(str(e))} — try again, or /cancel.")
    card_id = sess.ui_message_id
    app.sessions.clear(uid)
    app.dump.notify(f"⚙️ <code>{f.key}</code> → <b>{html.escape(sp.show(value, f))}</b> by <code>{uid}</code>")
    text, markup = _card(app, uid, panel, page)
    await app.sender.send_message(message.chat.id, f"✅ {html.escape(f.label)}: <b>{html.escape(sp.show(value, f))}</b>")
    if f.key == "force_sub.channels":  # tell at once whether the bot can check them
        await _send_fsub_check(app, message.chat.id)
    if card_id:
        await app.sender.edit_text(message.chat.id, card_id, text, reply_markup=markup)


async def _send_fsub_check(app, chat_id: int) -> None:
    from bot.core.forcesub import check_text
    rows = await app.forcesub.check(app.me.id)
    await app.sender.send_message(chat_id, check_text(rows))


# ------------------------------------------------------------------ config.env (owner)
def _env_values() -> dict[str, str]:
    from dotenv import dotenv_values
    return {k: v or "" for k, v in dotenv_values(ENV_FILE).items()} if ENV_FILE.exists() else {}


def _env_card(app, uid: int):
    vals = _env_values()
    lines = []
    for key, label, secret in ENV_KEYS:
        v = vals.get(key, "")
        shown = ("✅ set" if v else "— not set") if secret else (html.escape(v) if v else "— not set")
        over = "  <i>(host env var wins)</i>" if key in os.environ and os.environ.get(key) != v else ""
        lines.append(f"{html.escape(label)}: <b>{shown}</b>{over}")
    text = ("🔑 <b>config.env</b>\n<blockquote>" + "\n".join(lines) + "</blockquote>\n"
            "Secrets are never shown. A change is saved to <code>.env</code>; the fetch server URL / token and the "
            "proxy work at once, the rest after a restart.")
    rows = [[btn(f"✏️ {label}"[:40], cb("set", uid, "bot", f"ev_{i}"))] for i, (_k, label, _s) in enumerate(ENV_KEYS)]
    pairs = [rows[i] + rows[i + 1] if i + 1 < len(rows) else rows[i] for i in range(0, len(rows), 2)]
    return text, kb(*pairs, [btn("🧪 Test fetch server", cb("set", uid, "bot", "fetchck"))],
                    [btn("♻️ Restart to apply", cb("set", uid, "bot", "restart"), style="primary")],
                    [btn(t("btn_back"), cb("set", uid, "bot", "pg_0"))])


async def _take_env(app, message, sess) -> None:
    from dotenv import set_key, unset_key
    uid = message.from_user.id
    key = sess.data["env_key"]
    secret = next(s for k, _l, s in ENV_KEYS if k == key)
    value = (message.text or "").strip()
    if secret:
        try:
            await app.sender.delete(message.chat.id, message.id)
        except Exception:
            pass
    if not value:
        return await app.sender.send_message(message.chat.id, "Send the value as text, or <code>-</code> to clear it.")
    ENV_FILE.touch(exist_ok=True)
    if value == "-":
        unset_key(ENV_FILE, key)
    else:
        set_key(ENV_FILE, key, value, quote_mode="never")
    card_id = sess.ui_message_id
    app.sessions.clear(uid)
    done = 'cleared' if value == '-' else 'saved'
    live = _apply_live(app, key, "" if value == "-" else value)
    app.dump.notify(f"🔑 config.env <code>{key}</code> {'cleared' if value == '-' else 'changed'} by owner — "
                    + ("in use now" if live else "restart to apply"))
    if key in LIVE_ENV:
        await app.sender.send_message(
            message.chat.id,
            f"✅ <code>{key}</code> {done} — " + ("<b>in use now</b>, no restart needed." if live else
                                                "but the host's own <code>{key}</code> env var is used (it wins).")
            + ("\n<i>Links use the server only in /bot → Network → Fetching links: Remote or Auto (now: "
               f"{html.escape(str(app.settings.get('fetch.mode') or 'direct')).title()}).</i>" if key != "HTTP_PROXY" else ""),
            reply_markup=kb([btn("🧪 Test fetch server", cb("set", uid, "bot", "fetchck"))]) if key != "HTTP_PROXY" else None)
    else:
        await app.sender.send_message(message.chat.id, f"✅ <code>{key}</code> {done}. "
                                      "Tap <b>♻️ Restart to apply</b> when you're done.")
    if card_id:
        text, markup = _env_card(app, uid)
        await app.sender.edit_text(message.chat.id, card_id, text, reply_markup=markup)


def _apply_live(app, key: str, value: str) -> bool:
    """A ``LIVE_ENV`` key → ``app.config`` now (as the next boot would read it); False when it needs a restart or
    the host's env var overrides the file."""
    attr = LIVE_ENV.get(key)
    if attr is None or (key in os.environ and os.environ[key] != value):
        return False
    app.config = dataclasses.replace(app.config, **{attr: value.rstrip("/") if key == "FETCH_API_URL" else value})
    return True


async def _fetch_check_text(app) -> str:
    from bot.net.service import check_server
    r = await check_server(app.config.fetch_api_url, app.config.fetch_api_token)
    mode = str(app.settings.get("fetch.mode") or "direct")
    head = "🧪 <b>Fetch server test</b>\n"
    if r["step"] == "url":
        return head + "— No server address yet: set <b>Fetch server URL</b> in 🔑 config.env."
    lines = [head + f"🌐 <code>{html.escape(app.config.fetch_api_url)}</code>"]
    if r["step"] == "reach":
        lines.append(f"❌ <b>Not reachable</b> — {html.escape(r['detail'])}\n<i>On the PC that opens exam links, run "
                     "<code>tools\\fetch_server\\start_fetch_server.bat</code> (or <code>python server.py --tunnel</code>) "
                     "— it prints the URL and token to put here. A new start gives a new URL.</i>")
    else:
        lines.append(f"✅ Reachable ({r['ms']} ms)")
        lines.append(f"✅ Token accepted · {r['busy']} fetch(es) running there" if r["ok"] else
                     f"❌ <b>Token</b> — {html.escape(r['detail'])}\n<i>Fetch server token here must equal FETCH_TOKEN "
                     "on the server.</i>")
    if mode == "direct":
        lines.append("\n⚠️ Fetching links is <b>Direct</b> — the server is not used. /bot → Network → Fetching "
                     "links: <b>Auto</b> (server when the site refuses) or <b>Remote</b> (always).")
    else:
        lines.append(f"\nFetching links: <b>{mode.title()}</b>" + (" — every link goes through this server."
                     if mode == "remote" else " — this server is used when a site refuses the bot."))
    return "\n".join(lines)


async def _take_env_guarded(app, message, sess) -> None:
    if message.from_user.id != app.config.owner_id:  # only the owner reaches this step; checked again anyway
        app.sessions.clear(message.from_user.id)
        return
    await _take_env(app, message, sess)


WAITERS.update({"set:edit": _take_value, "set:env": _take_env_guarded})
