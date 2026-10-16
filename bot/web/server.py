"""The Web App (Telegram Mini App) server — spec §17.7. ``aiohttp`` in the bot's own process and loop, bound to
``127.0.0.1``; HTTPS comes from the tunnel (``web.tunnel``). Static page + small JSON only.

Routes
------
| route | what |
|---|---|
| ``GET /app`` | the quiz page (``templates/quiz.html``, webapp mode) — from memory, gzip |
| ``GET /font.ttf`` | Noto Sans Devanagari for Hindi (long cache) |
| ``POST /api/start`` ``{initData, quiz_id}`` | ``initData`` checked (HMAC per Telegram, max age), access rules as in the bot; → the questions **without answers** + a signed session token |
| ``GET /img/<quiz_id>/<image_id>`` | a question image from the exam bundle (``_webcache``, size-capped); ``immutable`` |
| ``POST /api/submit`` ``{token, answers, times}`` | scored **on the server**; the user's first private / Web App attempt is ranked (``quiz.store``), else practice → the result + the answer key |
| ``GET /api/health`` | ``ok`` |

Security: initData HMAC (secret = HMAC-SHA256("WebAppData", bot token)), ``webapp.init_data_max_age_s``; session
tokens HMAC-signed with a key derived from the bot token (they survive a restart), valid
``webapp.session_ttl_min``, used once, one active per user and quiz; per-user rate limit
(``webapp.rate_limit_per_min``); body ≤ 256 KB; only these routes. Each attempt → one summary line to dump.

Public API: ``WebApp(app)`` · ``await start()`` · ``await stop()`` · ``base_url`` (``None`` = no Web App buttons) ·
``check_init_data(init_data, bot_token, max_age_s) -> dict | None`` · ``sign(...)`` / ``unsign(...)``.

Depends on: aiohttp, ``quiz.html``, ``quiz.store``, ``core.tgref``, ``web.tunnel``. Used by: ``bot.app``, quiz screens.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import html as _html
import json
import logging
import time
import zipfile
from collections import OrderedDict
from pathlib import Path
from urllib.parse import parse_qsl

from aiohttp import web

from bot.core.ratelimit import TokenBucket
from bot.quiz import html as qhtml
from bot.quiz import store
from bot.render.fonts import FONT_DIR
from bot.web.tunnel import Tunnel

log = logging.getLogger(__name__)
MAX_BODY = 256 * 1024


def check_init_data(init_data: str, bot_token: str, max_age_s: int = 86400) -> dict | None:
    """Telegram Web App ``initData`` → its fields (``user`` decoded), or ``None`` when forged / too old."""
    if not init_data:
        return None
    try:
        data = dict(parse_qsl(init_data, keep_blank_values=True, strict_parsing=True))
    except ValueError:
        return None
    got = data.pop("hash", "")
    check = "\n".join(f"{k}={v}" for k, v in sorted(data.items()))
    secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    if not got or not hmac.compare_digest(hmac.new(secret, check.encode(), hashlib.sha256).hexdigest(), got):
        return None
    try:
        if max_age_s and time.time() - int(data.get("auth_date", "0")) > max_age_s:
            return None
        data["user"] = json.loads(data.get("user") or "{}")
    except (ValueError, TypeError):
        return None
    return data if data["user"].get("id") else None


def _key(bot_token: str) -> bytes:
    return hashlib.sha256(f"{bot_token}|rvsh-webapp-session".encode()).digest()


def sign(bot_token: str, uid: int, quiz_id: str, started: int) -> str:
    body = f"{uid}.{quiz_id}.{started}"
    return body + "." + hmac.new(_key(bot_token), body.encode(), hashlib.sha256).hexdigest()[:32]


def unsign(bot_token: str, token: str) -> tuple[int, str, int] | None:
    try:
        uid, qid, started, sig = str(token).split(".")
        body = f"{uid}.{qid}.{started}"
        if not hmac.compare_digest(hmac.new(_key(bot_token), body.encode(), hashlib.sha256).hexdigest()[:32], sig):
            return None
        return int(uid), qid, int(started)
    except (ValueError, TypeError):
        return None


class WebApp:
    def __init__(self, app) -> None:
        self.app = app
        self.runner: web.AppRunner | None = None
        self.port: int | None = None
        self.tunnel: Tunnel | None = None
        self._page: bytes | None = None
        self._payloads: OrderedDict[str, dict] = OrderedDict()
        self._files: dict[str, dict] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._buckets: dict[int, TokenBucket] = {}
        self.active: dict[tuple[int, str], str] = {}
        self.used: OrderedDict[str, float] = OrderedDict()
        self.counters = {"starts": 0, "submits": 0, "rejected": 0}

    @property
    def base_url(self) -> str | None:
        return self.tunnel.url if self.tunnel is not None else None

    @property
    def fixed_url(self) -> bool:
        """The address never changes (named tunnel / own address) — what a BotFather Mini App needs."""
        return self.tunnel is not None and self.tunnel.mode in ("named", "off") and bool(self.tunnel.base_url)

    @property
    def cache_dir(self) -> Path:
        return self.app.config.download_root / "_webcache"

    # ------------------------------------------------------------ lifecycle
    def make_app(self) -> web.Application:
        a = web.Application(client_max_size=MAX_BODY)
        a.router.add_get("/app", self.page)
        a.router.add_get("/font.ttf", self.font)
        a.router.add_post("/api/start", self.api_start)
        a.router.add_post("/api/submit", self.api_submit)
        a.router.add_get("/img/{qid}/{iid}", self.image)
        a.router.add_get("/api/health", self.health)
        return a

    async def start(self) -> None:
        cfg = self.app.config
        self.runner = web.AppRunner(self.make_app(), access_log=None)
        await self.runner.setup()
        for port in range(cfg.webapp_port, cfg.webapp_port + 10):
            try:
                await web.TCPSite(self.runner, cfg.webapp_host, port).start()
                self.port = port
                break
            except OSError:
                continue
        if self.port is None:
            self.app.dump.notify("⚠️ <b>Web App off</b> — no free local port near "
                                 f"{cfg.webapp_port}.")
            return
        self.tunnel = Tunnel(self.app.settings.get("webapp.tunnel") or "quick", self.port,
                             token=cfg.cloudflared_token, base_url=cfg.webapp_base_url, cmd=cfg.cloudflared_cmd,
                             alert=self.app.dump.notify, pid_file=str(cfg.download_root / "sessions" / "cloudflared.pid"))
        await self.tunnel.start()
        log.info("web app server on %s:%d (tunnel %s)", cfg.webapp_host, self.port, self.tunnel.mode)

    async def stop(self) -> None:
        if self.tunnel is not None:
            await self.tunnel.stop()
        if self.runner is not None:
            await self.runner.cleanup()

    # ------------------------------------------------------------ helpers
    def _err(self, text: str, status: int = 400) -> web.Response:
        self.counters["rejected"] += 1
        return web.json_response({"error": text}, status=status)

    def _rate_ok(self, uid: int) -> bool:
        per_min = float(self.app.settings.get("webapp.rate_limit_per_min") or 60)
        b = self._buckets.get(uid)
        if b is None:
            if len(self._buckets) > 20000:
                self._buckets = {k: v for k, v in self._buckets.items() if v.idle_for < 600}
            b = self._buckets[uid] = TokenBucket(per_min / 60, max(5, per_min / 6))
        b._refill()
        if b.tokens >= 1:
            b.tokens -= 1
            b.last_used = time.monotonic()
            return True
        return False

    async def _allowed(self, uid: int) -> str:
        """'' when this user may play here, else the reason (same rules as the bot, spec §6.4)."""
        app = self.app
        doc = await app.repo.users.get(uid, copy=False)
        if doc and (doc.get("tg_db") or {}).get("banned"):
            return "You can't use this bot."
        if not app.settings.get("public_enabled") and not app.access.is_admin(uid):
            return "The bot is open to admins only right now."
        return ""

    async def _bundle(self, quiz: dict) -> Path:
        """The quiz's exam bundle in ``_webcache`` (downloaded once; the cache stays under ``webapp.img_cache_mb``)."""
        from bot.core.tgref import download

        ref = quiz.get("json_ref") or {}
        name = hashlib.sha1(f"{ref.get('chat_id')}/{ref.get('message_id')}".encode()).hexdigest()[:16]
        target = self.cache_dir / "bundles" / f"{name}.zip"
        if target.exists():
            target.touch()
            return target
        lock = self._locks.setdefault(name, asyncio.Lock())
        async with lock:
            if not target.exists():
                tmp = self.cache_dir / "dl" / name
                path = await download(self.app, dict(ref), tmp)
                target.parent.mkdir(parents=True, exist_ok=True)
                path.replace(target)
                await asyncio.to_thread(self._trim)
        return target

    def _trim(self) -> None:
        cap = int(self.app.settings.get("webapp.img_cache_mb") or 500) * 2**20
        files = sorted((self.cache_dir / "bundles").glob("*.zip"), key=lambda p: p.stat().st_mtime)
        total = sum(p.stat().st_size for p in files)
        while files and total > cap and len(files) > 1:
            p = files.pop(0)
            total -= p.stat().st_size
            p.unlink(missing_ok=True)

    async def _payload(self, quiz: dict) -> dict:
        qid = quiz["_id"]
        # its content, not updated_at: every finished play bumps ``plays`` (and updated_at) — keyed on that, each
        # start rebuilt the payload from the bundle (load test 2026-10-13)
        from bot.quiz.maker import dump_sig
        ref = quiz.get("json_ref") or {}
        key = f"{qid}|{ref.get('chat_id')}/{ref.get('message_id')}|{dump_sig(quiz)}"
        got = self._payloads.get(key)
        if got is None:
            bundle = await self._bundle(quiz)
            got = await asyncio.to_thread(qhtml.payload, str(bundle), quiz, answers=False, img_url=f"img/{qid}/")
            self._payloads[key] = got
            while len(self._payloads) > 64:
                self._payloads.popitem(last=False)
        self._payloads.move_to_end(key)
        return got

    async def _json(self, request) -> dict:
        try:
            body = await request.json()
            return body if isinstance(body, dict) else {}
        except Exception:
            return {}

    # ------------------------------------------------------------ routes
    async def health(self, request) -> web.Response:
        return web.json_response({"ok": True, "url": self.base_url})

    async def page(self, request) -> web.Response:
        if self._page is None:
            from bot.render.theme import brand_url
            link = brand_url(self.app.settings, self.app.config.branding)
            self._page = qhtml.page("webapp", None, branding=self.app.config.branding, brand_url=link,
                                    font_url="font.ttf").encode()
        resp = web.Response(body=self._page, content_type="text/html", charset="utf-8",
                            headers={"Cache-Control": "no-cache"})
        resp.enable_compression()
        return resp

    async def font(self, request) -> web.FileResponse:
        return web.FileResponse(FONT_DIR / "NotoSansDevanagari-Regular.ttf",
                                headers={"Cache-Control": "public, max-age=31536000, immutable",
                                         "Content-Type": "font/ttf"})

    async def api_start(self, request) -> web.Response:
        body = await self._json(request)
        app = self.app
        data = check_init_data(str(body.get("initData") or ""), app.config.bot_token,
                               int(app.settings.get("webapp.init_data_max_age_s") or 86400))
        if data is None:
            return self._err("Open this from the bot's button in Telegram.", 401)
        user = data["user"]
        uid = int(user["id"])
        if not self._rate_ok(uid):
            return self._err("Too many requests — wait a moment.", 429)
        why = await self._allowed(uid)
        if why:
            return self._err(why, 403)
        qid = str(body.get("quiz_id") or "")[:20]
        if not qid:  # the Mini App opened from the bot's profile / menu, without a quiz
            return self._err("Pick a quiz in the bot (/quizzes) and open it from its 📱 button or link.", 400)
        quiz = await store.get(app, qid)
        if quiz is None:
            return self._err("This quiz doesn't exist (any more).", 404)
        try:
            payload = await self._payload(quiz)
        except Exception as e:
            app.dump.report_error(e, f"webapp start {qid}")
            return self._err("The quiz could not be loaded — try again in a minute.", 503)
        token = sign(app.config.bot_token, uid, qid, int(time.time()))
        self.active[(uid, qid)] = token
        self.counters["starts"] += 1
        name = " ".join(x for x in (user.get("first_name"), user.get("last_name")) if x) or "Player"
        try:
            await app.repo.users.update(uid, {"$setOnInsert": {"tg_db.name": name, "tg_db.username": user.get("username") or "",
                                                               "tg_db.joined_at": store.now_iso(), "tg_db.banned": False,
                                                               "quiz_db": {"created": [], "attempted": []}, "exam_db": {}}})
        except Exception:
            pass
        resp = web.json_response({"quiz": payload, "token": token})
        resp.enable_compression()
        return resp

    async def image(self, request) -> web.Response:
        qid, iid = request.match_info["qid"], request.match_info["iid"]
        quiz = await store.get(self.app, qid)
        if quiz is None:
            raise web.HTTPNotFound()
        try:
            bundle = await self._bundle(quiz)
        except Exception:
            raise web.HTTPServiceUnavailable()
        files = self._files.get(str(bundle))
        if files is None:
            files = self._files[str(bundle)] = await asyncio.to_thread(qhtml.image_files, str(bundle))
            if len(self._files) > 64:
                self._files.pop(next(iter(self._files)))
        entry = files.get(iid)
        if entry is None:
            raise web.HTTPNotFound()

        def read() -> bytes:
            with zipfile.ZipFile(bundle) as z:
                return z.read(entry[0])
        raw = await asyncio.to_thread(read)
        return web.Response(body=raw, content_type=entry[1],
                            headers={"Cache-Control": "public, max-age=31536000, immutable"})

    async def api_submit(self, request) -> web.Response:
        app = self.app
        body = await self._json(request)
        token = str(body.get("token") or "")
        got = unsign(app.config.bot_token, token)
        if got is None:
            return self._err("This attempt is not valid — open the quiz again.", 401)
        uid, qid, started = got
        if not self._rate_ok(uid):
            return self._err("Too many requests — wait a moment.", 429)
        ttl = int(app.settings.get("webapp.session_ttl_min") or 240) * 60
        if time.time() - started > ttl:
            return self._err("This attempt has expired — open the quiz again.", 410)
        if token in self.used:
            return self._err("Already submitted.", 409)
        if self.active.get((uid, qid), token) != token:
            return self._err("You opened this quiz again somewhere else — submit there.", 409)
        quiz = await store.get(app, qid)
        if quiz is None:
            return self._err("This quiz doesn't exist (any more).", 404)
        refs = {q["ref"] for q in quiz["questions"]}
        raw_answers = body.get("answers") if isinstance(body.get("answers"), dict) else {}
        answers = {k: v for k, v in raw_answers.items() if k in refs and isinstance(v, int) and not isinstance(v, bool)}
        times = body.get("times") if isinstance(body.get("times"), dict) else {}
        res = qhtml.score(quiz, answers, {k: v for k, v in times.items() if k in refs})
        if quiz.get("explain"):  # explanations only now, with the answers
            try:
                ex = await asyncio.to_thread(qhtml.explanations, str(await self._bundle(quiz)), quiz, f"img/{qid}/")
                res["expl"], res["expl_images"] = ex["expl"], ex["images"]
            except Exception as e:
                log.info("web app: explanations of %s not sent: %r", qid, e)
        res["time_s"] = round(min(res["time_s"] or (time.time() - started), time.time() - started), 1)
        self.used[token] = time.time()
        while len(self.used) > 20000:
            self.used.popitem(last=False)
        self.active.pop((uid, qid), None)
        doc = await app.repo.users.get(uid, copy=False) or {}
        name = (doc.get("tg_db") or {}).get("name") or "Player"
        info = (await store.record(app, qid, [{"uid": uid, "name": name, "correct": res["right"], "wrong": res["wrong"],
                                               "skipped": res["skipped"], "time_s": res["time_s"]}], "webapp")).get(uid) or {}
        self.counters["submits"] += 1
        app.dump.notify(f"📱 Web App · <a href=\"tg://user?id={uid}\">{_html.escape(name)}</a> · <code>{qid}</code> · "
                        f"{res['right']}/{res['total']} right · {'ranked #' + str(info.get('rank')) if info.get('ranked') else 'practice'}")
        return web.json_response({**res, "ranked": bool(info.get("ranked")), "rank": info.get("rank"), "of": info.get("of")})
