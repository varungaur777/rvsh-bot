"""Application container: boot, wiring, startup report, graceful shutdown (spec §6.7, §6.10).

Depends on: every core module, ``db.repo``, ``tasks``, ``handlers``. Used by: ``bot/__main__.py``.

Public API
----------
- ``App(config, runtime)`` — holds: config, runtime, repo, settings, client (main bot), uploaders, sender,
  dump, tasks (``TaskManager``), sessions, access, forcesub, tasklog, looplag, me (bot user), started_at.
- ``await app.start()`` · ``await app.run_until_stopped()`` · ``await app.stop(reason)`` ·
  ``app.request_stop(reason, restart=False)``
- hooks used by other modules: ``on_user_blocked(user_id)``, ``on_group_lost(chat_id)`` (set by quiz in P5).

Exceptions → outcome: a failure before the bot answers (bad token, no network) is logged and re-raised;
``__main__`` exits with code 1. Failures after start are reported to dump and never stop the bot.
"""

from __future__ import annotations

import asyncio
import contextlib
import html
import logging
import platform
import signal
import time
from pathlib import Path

import psutil
import pyrogram
from pyrogram import Client, enums

from bot import __version__
from bot.core import tgfix
from bot.core.access import Access
from bot.core.config import Config
from bot.core.dump import DumpNotifier
from bot.core.forcesub import ForceSub
from bot.core.lease import Lease
from bot.core.logging import setup_logging
from bot.core.fastdl import FastDownloader
from bot.compute import Compute
from bot.core.looplag import LoopLag
from bot.tasks.gate import Gate, Gates
from bot.core.runtime import RuntimeInfo, available_ram_mb
from bot.core.sender import Sender
from bot.core.settings import DEFAULTS, Settings
from bot.core.tasklog import TaskLog
from bot.db.repo import Repo
from bot.tasks.lanes import TaskManager
from bot.uploader.workers import UploaderPool
from bot.tasks.session import SessionStore
from bot.ui.texts import t

log = logging.getLogger(__name__)
tgfix.apply()  # Kurigram fixes (quiz polls with an explanation)


class App:
    def __init__(self, config: Config, runtime: RuntimeInfo) -> None:
        self.config, self.runtime = config, runtime
        self.root = config.download_root
        self.started_at = time.time()
        self.boot_ms = 0
        self.restart_requested = False
        self.code_update = False  # /update: leave as a process so the new code loads
        self.stop_reason = ""
        self._stop = asyncio.Event()
        self.on_group_lost = None  # the quiz engine's stop_chat (bot removed / demoted in a group)
        self.quiz = None  # quiz.engine.QuizEngine
        self.web = None  # web.server.WebApp
        self.autoquiz = None
        self.broadcast = None  # the running /bc (broadcast.bc.Broadcast), one at a time
        self.broadcast_job: asyncio.Task | None = None
        self.uploaders: list[Client] = []
        self.uploader_status: list[str] = []

        self.log_handler = setup_logging(self.root / "logs")
        self.repo = Repo(self.root / "_db", config.mongo_url)
        self.settings = Settings(self.repo, runtime)
        self.sessions = SessionStore()
        self.looplag = LoopLag()
        self.ready = asyncio.Event()
        self.boot_timings: dict[str, int] = {}
        self.max_update_lag = 0.0

    # ------------------------------------------------------------ boot
    async def start(self) -> None:
        """Concurrent boot (owner 2026-10-12): DB + settings + lease, the main bot and every uploader bot start
        together with asyncio. Handlers wait on ``self.ready`` until the data side is ready, so an early update is
        delayed by milliseconds, never lost or handled half-initialised."""
        t0 = time.perf_counter()
        for sub in ("tasks", "logs", "sessions"):
            (self.root / sub).mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread(_clear_dir, self.root / "tasks")  # leftovers of a crash/restart (tasks never survive)
        await asyncio.to_thread(_clear_dir, self.root / "_webcache")

        self.client = Client(
            "main", api_id=self.config.api_id, api_hash=self.config.api_hash, bot_token=self.config.bot_token,
            workdir=str(self.root / "sessions"), parse_mode=enums.ParseMode.HTML, sleep_threshold=10,
            max_concurrent_transmissions=DEFAULTS["tg"]["max_concurrent_transmissions"],
        )
        self.client.app = self
        self.sender = Sender(self.client, on_blocked=self.on_user_blocked)
        self.fastdl = FastDownloader(self.client, DEFAULTS["tg"]["fast_download_connections"], DEFAULTS["tg"]["fast_download_part_kb"])
        self.uploads = UploaderPool(self.client, self.sender, DEFAULTS["uploader"]["per_bot_concurrency"])
        self.dump = DumpNotifier(self.uploads, self.config.dump_channel_id)  # dump notices ride the uploader team too
        self.access = Access(self)
        self.forcesub = ForceSub(self.client, self.settings, self.dump, extra=self.config.force_sub_channels)
        self.tasklog = TaskLog(self)
        self.lease = Lease(None)
        from bot.quiz.autoquiz import AutoQuiz
        from bot.quiz.engine import QuizEngine
        from bot.web.server import WebApp
        self.quiz = QuizEngine(self)
        self.on_group_lost = self.quiz.stop_chat
        self.web = WebApp(self)
        self.autoquiz = AutoQuiz(self)

        from bot import handlers
        handlers.register(self)

        async def data_side() -> bool:
            await self.repo.start()
            await self.settings.load()
            self.repo.json.flush_s = self.settings.get("db.json_flush_ms") / 1000
            self.repo.cache.size = self.settings.get("db.lru_size")
            self.lease = Lease(self.repo.mongo)
            return await self.lease.acquire()

        uploaders = asyncio.get_running_loop().create_task(self._start_uploaders())
        timings: dict[str, int] = {}

        async def timed(name, coro):
            t = time.perf_counter()
            res = await coro
            timings[name] = int((time.perf_counter() - t) * 1000)
            return res

        ok, _ = await asyncio.gather(timed("db+lease", data_side()), timed("main bot", self.client.start()))
        if not ok:
            await self.client.stop()
            raise RuntimeError("another instance is running and did not hand over the lease")
        self.boot_timings = timings
        self.me = self.client.me or await self.client.get_me()

        self.tasks = TaskManager(self.root, {
            "light": self.settings.value("queue.light_concurrent"),
            "heavy": self.settings.value("queue.heavy_concurrent"),
            "background": self.settings.value("queue.background_concurrent"),
        })
        self.gates = Gates(prepare=Gate("prepare", self.settings.value("queue.prepare_concurrent")),
                           fetch=Gate("fetch", self.settings.get("fetch.parallel_links")))
        self.settings.subscribe("fetch", self._fetch_setting_changed)
        self.settings.subscribe("force_sub", lambda _k, _v: self.forcesub.clear_cache())
        self.tasks.on_start = self.tasklog.started
        self.tasks.on_close = self._task_closed
        self.tasks.on_error = self._task_error
        self.settings.subscribe("queue", self._queue_setting_changed)
        self.settings.subscribe("pool", self._queue_setting_changed)
        self.compute = Compute(self.settings, self.runtime, self.root, alert=self.dump.notify)
        for prefix in ("pool", "render", "ocr", "merge"):
            self.settings.subscribe(prefix, self._compute_setting_changed)
        self.repo.on_mongo_state = self._mongo_state
        self.dump.start()
        self.tasks.start()
        self.looplag.start()
        self.lease.start_heartbeat(self._lease_lost)
        loop = asyncio.get_running_loop()
        self.log_handler.on_rotate = lambda p: loop.call_soon_threadsafe(
            self.dump.send_file, p, f"🗒 Log rotated · {p.name}", True)
        self._install_signals()
        self.boot_ms = int((time.perf_counter() - t0) * 1000)
        self.compute.start()  # governor sampling; the pool itself starts on the first job (or the warm-up below)
        self.ready.set()  # handlers may run now
        loop.create_task(self._set_commands())  # not needed to answer /start
        self.fastdl.connections = int(self.settings.get("tg.fast_download_connections") or 0)
        self._prewarm = loop.create_task(self.fastdl.prewarm(self.settings.get("tg.prewarm_dcs") or []))
        if self.settings.get("pool.warm_up"):  # spawn + import in the background, after the bot already answers
            self.compute.warm()
        loop.create_task(self._results_catalogue())
        loop.create_task(self._check_forcesub())
        loop.create_task(self.quiz.boot_notify())  # chats whose quiz the last stop interrupted
        loop.create_task(self._start_web())
        from bot.handlers.update import boot_report
        loop.create_task(boot_report(self))  # after a /update: "back on the new code"
        self.autoquiz.start()
        uploaders.add_done_callback(lambda _: self.dump.notify(self.startup_report()))
        log.info("bot @%s ready in %d ms (%s)", self.me.username, self.boot_ms,
                 ", ".join(f"{k} {v} ms" for k, v in timings.items()))

    async def _start_uploaders(self) -> None:
        """Start uploader bots concurrently in the background (no updates needed; the main bot does not wait
        for them) and check their admin rights in both dumps. The startup report is sent when they are up."""
        async def one(i: int, token: str) -> None:
            c = Client(f"up{i}", api_id=self.config.api_id, api_hash=self.config.api_hash, bot_token=token,
                       workdir=str(self.root / "sessions"), no_updates=True, parse_mode=enums.ParseMode.HTML,
                       sleep_threshold=0,  # every FloodWait reaches the pool (stay on the bot or hand over)
                       max_concurrent_transmissions=self.settings.get("tg.max_concurrent_transmissions"))
            try:
                await c.start()
                me = await c.get_me()
                missing = []
                for ch in {self.config.dump_channel_id, self.config.image_dump_channel_id}:
                    try:
                        m = await c.get_chat_member(ch, me.id)
                        if m.status not in (enums.ChatMemberStatus.ADMINISTRATOR, enums.ChatMemberStatus.OWNER):
                            missing.append(str(ch))
                    except Exception:
                        missing.append(str(ch))
                c.app = self
                self.uploaders.append(c)
                if not missing:
                    self.uploads.add(c, f"@{me.username}")  # joins the pool as soon as it is up
                self.uploader_status.append(f"@{me.username}" + (f" ⚠️ not admin in {', '.join(missing)}" if missing else " ✅"))
            except Exception as e:
                self.uploader_status.append(f"#{i} ❌ {e.__class__.__name__}")
                log.warning("uploader bot #%d failed to start: %r", i, e)

        await asyncio.gather(*(one(i, tok) for i, tok in enumerate(self.config.image_bot_tokens, 1)))

    async def _start_web(self) -> None:
        """The Web App server + tunnel (spec §17.7) — optional: a failure only hides the Web App buttons."""
        try:
            await self.web.start()
        except Exception as e:
            log.warning("web app failed to start: %r", e)
            self.dump.notify(f"⚠️ <b>Web App off</b> — the server did not start: {html.escape(repr(e))[:200]}")

    async def _set_commands(self) -> None:
        from pyrogram.types import BotCommand, BotCommandScopeAllGroupChats, BotCommandScopeChat, BotCommandScopeDefault
        user_cmds = [BotCommand("start", "Home menu"), BotCommand("help", "How to use"),
                     BotCommand("quizzes", "Browse quizzes"), BotCommand("quiz", "Your quizzes · /quiz <id>"),
                     BotCommand("json", "PDF / quiz from a question JSON file"),
                     BotCommand("json_batch", "Reply to a .zip of JSON files: all of them"),
                     BotCommand("cancel", "Cancel your task"), BotCommand("q", "Your queue position")]
        admin_cmds = user_cmds + [BotCommand("results", "Result PDF of an exam (averages)"),
                                  BotCommand("bc", "Broadcast to users or groups"), BotCommand("stopbc", "Stop the broadcast"),
                                  BotCommand("msg", "Message a user"),
                                  BotCommand("task", "Running and waiting tasks"), BotCommand("cq", "Clear queue / cancel all"),
                                  BotCommand("admin", "Admins"), BotCommand("groups", "Groups the bot is in"),
                                  BotCommand("user", "Everything about a user (groups too)"),
                                  BotCommand("qz", "Quiz settings"),
                                  BotCommand("load_quiz", "Reply to a quiz / exam file: load it"),
                                  BotCommand("ls", "Files in the download folder"), BotCommand("clean", "Delete leftovers"),
                                  BotCommand("es", "Exam settings"), BotCommand("ps", "PDF settings"),
                                  BotCommand("wm", "Watermark settings"),
                                  BotCommand("stats", "Live resources"), BotCommand("log", "Send the log file"),
                                  BotCommand("on", "Open to the public"), BotCommand("off", "Admins only"),
                                  BotCommand("restart", "Graceful restart")]
        owner_cmds = admin_cmds + [BotCommand("bot", "Bot settings and config.env"), BotCommand("db", "Database panel"),
                                   BotCommand("rm", "Delete a file or folder (backup first in _db)"),
                                   BotCommand("auth", "Authorise a group (works while /off)"),
                                   BotCommand("deauth", "Remove a group's authorisation"),
                                   BotCommand("update", "Reply to a code zip: update the bot")]
        group_cmds = [BotCommand("quizzes", "Quiz library"), BotCommand("quiz", "Quizzes · /quiz <id> plays one here")]
        try:
            await self.client.set_bot_commands(user_cmds, scope=BotCommandScopeDefault())
            await self.client.set_bot_commands(group_cmds, scope=BotCommandScopeAllGroupChats())
            for uid in {self.config.owner_id, *(self.settings.get("admins") or [])}:
                try:
                    await self.client.set_bot_commands(owner_cmds if uid == self.config.owner_id else admin_cmds,
                                                       scope=BotCommandScopeChat(uid))
                except Exception:
                    pass  # the admin has not started the bot yet
        except Exception as e:
            log.warning("set_bot_commands failed: %r", e)

    # ------------------------------------------------------------ reports
    def startup_report(self) -> str:
        proc = psutil.Process()
        vm = psutil.virtual_memory()
        disk = psutil.disk_usage(str(self.root))
        rt, st = self.runtime, self.settings
        mongo = "✅ connected" if self.repo.mongo_ok else ("❌ unreachable (JSON mode)" if self.repo.mongo else "— not configured")
        ups = "\n".join(f"   • {s}" for s in self.uploader_status) or "   • none (main bot uploads)"
        return (
            f"🚀 <b>Bot started</b> · v{__version__} · <b>{self.boot_ms} ms</b>\n"
            f"🤖 @{self.me.username} · profile <code>{rt.profile}</code>{' (ephemeral)' if rt.ephemeral else ''}\n"
            f"🖥 {html.escape(platform.node())} · {rt.os_name} · Python {rt.python} · Kurigram {pyrogram.__version__}\n"
            f"⚙️ CPU {psutil.cpu_percent(interval=None):.0f}% · {rt.cpus} effective cores\n"
            f"🧠 RAM {(vm.total - vm.available) // 2**20} / {rt.ram_total_mb} MB used · free {available_ram_mb(rt)} MB · "
            f"bot RSS {proc.memory_info().rss // 2**20} MB\n"
            f"💾 Disk free {disk.free // 2**30} GB\n"
            f"🗄 Mongo {mongo} · JSON mirror ✅ · outbox {self.repo.outbox.size()}\n"
            f"🧮 Pool workers <b>{st.workers()}</b> ({st.get('pool.workers')}) · lanes light {st.value('queue.light_concurrent')}"
            f" / heavy {st.value('queue.heavy_concurrent')} / background {st.value('queue.background_concurrent')}\n"
            f"📤 Uploader bots ({len(self.uploaders)}/{len(self.config.image_bot_tokens)} online):\n{ups}\n"
            f"🌐 Public: {'on' if st.get('public_enabled') else 'off'} · 📱 Web App: starting (see /stats)"
        )

    def stats_text(self, detail: bool = False) -> str:
        from bot.ui import stats_view
        return stats_view.render(stats_view.collect(self), detail)

    # ------------------------------------------------------------ hooks
    async def on_user_blocked(self, user_id: int) -> None:
        """User blocked the bot: cancel their task, clear their session, remember it (spec §6.6)."""
        task = self.tasks.active_for(user_id)
        if task:
            await self.tasks.cancel(task.task_id, "user blocked the bot", status="blocked")
        self.sessions.clear(user_id)
        if self.quiz is not None:  # their private quiz ends quietly (its score so far is kept)
            with contextlib.suppress(Exception):
                await self.quiz.stop_chat(user_id)
        if self.web is not None:  # an open Web App attempt can still be submitted — nothing to send them
            self.web.active = {k: v for k, v in self.web.active.items() if k[0] != user_id}
        try:
            await self.repo.users.update(user_id, {"$set": {"tg_db.blocked_bot": True}}, upsert=False)
        except Exception:
            pass

    async def _task_closed(self, task) -> None:
        self.sessions.clear(task.user_id)
        await self.tasklog.finished(task)

    async def _task_error(self, task, exc: BaseException) -> None:
        from bot.core.errors import BotError
        key = getattr(exc, "key", "")
        text = t(f"error_{key}") if isinstance(exc, BotError) else ""
        if not text or text == f"error_{key}":
            task.error_id = self.dump.report_error(exc, f"task {task.task_id} ({task.kind})")
            text = t("error_generic", error_id=task.error_id)
        if task.chat_id:
            try:  # replace the task's status message when there is one (no extra message)
                if task.data.get("ui_message_id"):
                    await self.sender.edit_text(task.chat_id, task.data["ui_message_id"], text)
                else:
                    await self.sender.send_message(task.chat_id, text)
            except Exception:
                pass

    async def _queue_setting_changed(self, key: str, value) -> None:
        for lane, k in (("light", "queue.light_concurrent"), ("heavy", "queue.heavy_concurrent"),
                        ("background", "queue.background_concurrent")):
            await self.tasks.set_limit(lane, self.settings.value(k))
        self.gates.prepare.set_limit(self.settings.value("queue.prepare_concurrent"))

    async def _check_forcesub(self) -> None:
        """Boot: a must-join channel the bot cannot check (not admin, gone) is reported to dump at once."""
        from bot.core.forcesub import check_text
        try:
            rows = await self.forcesub.check(self.me.id)
        except Exception as e:
            return log.warning("force-sub boot check failed: %r", e)
        if rows:
            log.info("force-sub: %d channel(s), %d working", len(rows), sum(r["ok"] for r in rows))
        if any(not r["ok"] for r in rows):
            self.dump.notify(check_text(rows))

    async def _results_catalogue(self) -> None:
        """One-time backfill: results saved before the ``exams`` catalogue existed become visible in /results."""
        from bot.exam.results import rebuild_catalogue
        try:
            if await self.repo.exams.count({"results": {"$gt": 0}}) == 0 and await self.repo.exam_results.count() > 0:
                log.info("results catalogue rebuilt: %d shift(s)", await rebuild_catalogue(self.repo))
        except Exception as e:
            log.warning("results catalogue backfill failed: %r", e)
        from bot.exam.urls import migrate_ids
        try:
            if n := await migrate_ids(self.repo):
                log.info("exam_urls: %d link(s) moved into per-exam folders", n)
        except Exception as e:
            log.warning("exam_urls migration failed: %r", e)

    async def _compute_setting_changed(self, key: str, value) -> None:
        self.compute.reload()

    async def _fetch_setting_changed(self, key: str, value) -> None:
        self.gates.fetch.set_limit(self.settings.get("fetch.parallel_links"))

    def _mongo_state(self, healthy: bool, replayed: int) -> None:
        self.dump.notify(f"🗄 Mongo {'back online ✅ (replayed ' + str(replayed) + ' ops)' if healthy else 'unreachable ❌ — serving from JSON'}")

    async def _lease_lost(self, reason: str) -> None:
        log.warning("stopping: %s", reason)
        self.request_stop(reason)

    # ------------------------------------------------------------ shutdown
    def request_stop(self, reason: str, restart: bool = False) -> None:
        self.stop_reason = reason
        self.restart_requested = self.restart_requested or restart
        self._stop.set()

    def _install_signals(self) -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, self.request_stop, f"signal {sig.name}")
            except (NotImplementedError, RuntimeError, ValueError):
                signal.signal(sig, lambda s, f: loop.call_soon_threadsafe(self.request_stop, f"signal {s}"))

    async def run_until_stopped(self) -> None:
        max_up = self.settings.get("runtime.max_uptime_min") or (340 if self.runtime.profile == "github_actions" else 0)
        if max_up:
            asyncio.get_running_loop().call_later(max_up * 60, self.request_stop, "planned restart (max uptime)")
        await self._stop.wait()
        await self.stop(self.stop_reason)

    async def stop(self, reason: str) -> None:
        """Graceful shutdown (spec §6.10): intake off → grace → cancel → flush → report → release lease."""
        log.info("shutting down: %s", reason)
        grace = self.settings.get("shutdown.grace_s")
        for task in self.tasks.running() + self.tasks.queued():
            if task.chat_id:
                try:
                    await self.sender.send_message(task.chat_id, t("restarting"))
                except Exception:
                    pass
        await self.tasks.shutdown(grace)
        if self.autoquiz is not None:
            self.autoquiz.stop()
        if self.quiz is not None:
            self.quiz.shutdown()  # running quizzes end; their chats are told on the next boot (running_quizzes)
        if self.web is not None:
            with contextlib.suppress(Exception):
                await self.web.stop()
        if self.broadcast is not None:  # stop it and let it post its report (sent so far)
            self.broadcast.stop("restart")
            if self.broadcast_job is not None:
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(self.broadcast_job, 15)
        if self.restart_requested:  # restart = fresh caches: task leftovers + web cache; DB and sessions untouched
            await asyncio.to_thread(_clear_dir, self.root / "tasks")
            await asyncio.to_thread(_clear_dir, self.root / "_webcache")
            await asyncio.to_thread(clear_pycache)  # owner 2026-10-13: stale bytecode goes too
        up = int(time.time() - self.started_at)
        self.dump.notify(f"🛑 <b>Bot stopping</b> · {html.escape(reason)} · uptime {up // 3600}h {up % 3600 // 60}m")
        await self.dump.stop()
        self.looplag.stop()
        self.compute.stop()
        await self.lease.release()  # before repo.close(): the lease lives in Mongo (B-002)
        await self.repo.close()
        try:
            await self.fastdl.close()
        except Exception:
            pass
        for c in [*self.uploaders, self.client]:
            try:
                await c.stop()
            except Exception:
                pass


def _clear_dir(path: Path) -> None:
    """Delete everything inside ``path`` (the folder itself stays). Only for tasks/ and _webcache/ — never _db/."""
    import shutil
    if path.name not in ("tasks", "_webcache") or not path.exists():
        return
    for child in path.iterdir():
        if child.is_dir():
            shutil.rmtree(child, ignore_errors=True)
        else:
            child.unlink(missing_ok=True)


def clear_pycache(base: Path | None = None) -> int:
    """Delete every ``__pycache__`` folder of the project's own code (``bot/``, ``tests/``, ``tools/``) — never a
    virtualenv or site-packages. Python rebuilds what it needs. Returns how many folders went."""
    import shutil
    base = base or Path(__file__).resolve().parent.parent
    n = 0
    for top in ("bot", "tests", "tools"):
        root = base / top
        if not root.is_dir():
            continue
        for d in list(root.rglob("__pycache__")):
            if d.is_dir():
                shutil.rmtree(d, ignore_errors=True)
                n += 1
    return n
