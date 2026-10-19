"""Auto quiz (spec §17.5): every ``autoquiz.interval_min`` one random question from a random quiz to each group
where the bot is admin and auto quiz is on (``groups.auto_quiz``; ``/groups`` → a group's card), staggered through
the sender's lowest-priority lane. Groups with a running quiz are skipped. While the bot is closed (``/off``) only
authorised groups get one (owner 2026-10-13). On / off and timing: ``/qz``.

A test from ``/qz → Auto quiz → 🧪 Send one now`` runs one round at once (even while auto quiz is off);
``last`` tells what it found: ``{"groups", "quizzes", "sent", "failed", "busy"}``.

Public API: ``AutoQuiz(app)`` · ``start()`` · ``stop()`` · ``await tick() -> sent`` · ``last``.
Depends on: ``quiz.engine`` (cards), ``core.sender``. Used by: ``bot.app``.
"""

from __future__ import annotations

import asyncio
import logging
import random

from pyrogram import enums

from bot.core.sender import BULK
from bot.quiz.engine import branded

log = logging.getLogger(__name__)
STAGGER_S = 2.0


class AutoQuiz:
    def __init__(self, app) -> None:
        self.app = app
        self._task: asyncio.Task | None = None
        self.sent = 0
        self.last: dict = {}

    def start(self) -> None:
        self._task = asyncio.get_running_loop().create_task(self._loop())

    def stop(self) -> None:
        if self._task:
            self._task.cancel()

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(max(1, int(self.app.settings.get("autoquiz.interval_min") or 15)) * 60)
            if not self.app.settings.get("autoquiz.enabled"):
                continue
            try:
                await self.tick()
            except Exception as e:
                self.app.dump.report_error(e, "autoquiz")

    async def tick(self) -> int:
        app = self.app
        groups = await app.repo.groups.find({"present": {"$ne": False}, "bot_is_admin": True, "banned": {"$ne": True},
                                             "auto_quiz": {"$ne": False}, "can_write": {"$ne": False}},
                                            projection={"authorised": 1})
        if not app.settings.get("public_enabled"):
            groups = [g for g in groups if g.get("authorised")]
        quizzes = await app.repo.quizzes.find({}, projection={"_id": 1})
        self.last = {"groups": len(groups), "quizzes": len(quizzes), "sent": 0, "failed": 0, "busy": 0}
        if not groups or not quizzes:
            return 0
        sent = 0
        for g in groups:
            chat = g["_id"]
            if app.quiz.session_for(chat) is not None:
                self.last["busy"] += 1
                continue
            for _ in range(3):  # a random quiz, a random question (quizzes are read through the repo LRU)
                quiz = await app.repo.quizzes.get(random.choice(quizzes)["_id"], copy=False)
                if quiz and quiz.get("questions"):
                    break
            else:
                continue
            q = random.choice(quiz["questions"])
            try:
                await self._send(chat, quiz, q)
                sent += 1
            except Exception as e:
                self.last["failed"] += 1
                log.info("auto quiz to %s failed: %r", chat, e)
            await asyncio.sleep(STAGGER_S)
        self.last["sent"] = sent
        self.sent += sent
        return sent

    async def _send(self, chat: int, quiz: dict, q: dict) -> None:
        app = self.app
        eng = app.quiz
        if q.get("mode") == "card" and q.get("cards"):
            missing = [r for r in q["cards"] if (r["chat_id"], r["message_id"]) not in eng.file_ids]
            if missing:
                for r in missing:
                    try:
                        m = await app.client.get_messages(r["chat_id"], r["message_id"])
                        if getattr(m, "photo", None):
                            eng.file_ids[(r["chat_id"], r["message_id"])] = m.photo.file_id
                    except Exception:
                        pass
            for r in q["cards"]:
                fid = eng.file_ids.get((r["chat_id"], r["message_id"]))
                if fid:
                    await app.sender.send_photo(chat, fid, priority=BULK)
                else:
                    await app.sender.copy(chat, r["chat_id"], r["message_id"], priority=BULK)
            question, options = "🎲 Quick question — answer from the card above ☝️", list("ABCDEFGHIJ"[:q["n"]])
        else:
            poll = q.get("poll") or {}
            question, options = f"🎲 {poll.get('q', '')}"[:300], poll.get("o") or list("ABCDEFGHIJ"[:q["n"]])
        period = int(app.settings.get("autoquiz.open_period_s") or 0)
        me = app.me.username
        expl = q.get("ex") or f"From: {quiz.get('title', '')[:120]} · play it: t.me/{me}?start={quiz['_id']}"
        await app.sender.send_poll(chat, branded(app, question), options, is_anonymous=False, type=enums.PollType.QUIZ,
                                   correct_option_ids=[int(q["c"])], open_period=period if 5 <= period <= 600 else None,
                                   explanation=expl[:200], priority=BULK)
