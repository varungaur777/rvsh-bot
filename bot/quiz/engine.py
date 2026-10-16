"""Quiz play engine (spec §17.2–§17.6): one ``Session`` per chat, all in memory, driven by poll votes and timers.

Hot path (must stay cheap): **zero disk / DB I/O per answer** — the quiz doc is read once at start (repo LRU), votes
land in a dict, ``poll_id → (session, position)`` is a dict; scores are written once at the end (``quiz.store``).
Every send goes through the central sender (``POLL`` priority; button clean-ups ``BULK``).

Per question: the card(s) (one photo, or an album of two — sent by ``file_id`` read once per session from the dump
messages; ``copy_message`` if that fails) and then a **non-anonymous quiz poll** with ``open_period`` = the chosen
time. ``⏸ Pause`` ``⏹ Stop`` are on the start message and on the current poll only; the previous poll's buttons are
removed when the next one is sent (in a group only when the chat's rate budget has room — the start message always
works).

- **Private:** the next question comes right after the user answers; the timer only covers no answer. 3 questions
  in a row without an answer → paused (no spam to someone who left).
- **Group:** the timer moves on; the per-question time is raised to stay under ~17 messages / minute; 3 questions
  without any answer → stopped. Stop / Pause / Resume (``handlers.quiz._can_control``): the starter, the group's
  admins / owner (anonymous included) and the bot's owner / admins (owner 2026-10-14); a quiz started by an anonymous
  admin (``starter`` 0) has no single starter. Anonymous admins' votes count when Telegram delivers them (as the group: one row named after the group, on
  the group's board only — never in the global ranking).
- **Pause:** no new question; the open poll closes by itself; ``⏸ Paused at Q12/50`` + Resume / Stop on the start
  message; after ``quiz.pause_timeout_min`` the quiz stops and the results are posted. One active quiz per chat and
  per starter (a paused one counts) — a second start is refused with a ``⏹ Stop`` button for the first one (owner:
  "dusri quiz start krne se phle, phli vali ko band kre").
- **End:** private → result (correct / wrong / skipped / time), ranked or practice (``quiz.store.record``), global
  top 10 + own rank; Review, Retry wrong (practice, not saved), Play again, Leaderboard, Share. Group → session board
  (🥇🥈🥉, correct, time) + global leaderboard link; group play is never ranked globally.
- **Restart:** ``running_quizzes`` holds the chats with a quiz; on boot they get "stopped due to a restart".
- **Explanations** (quizzes from question JSON files, owner 2026-10-13): a short one is the poll's own explanation
  (``ex``); a long one is a card (``xc``, caption "💡 Explanation · Q3/50") sent **after the question is answered,
  right or wrong, with no waiting and no button** ("rukna nhi hai, next question ka button nhi lgana") — private:
  right after the answer, then the next question; group: when the time is up (if anyone answered), then the next.

Public API
----------
- ``QuizEngine(app)`` · ``await start(chat, quiz, starter, time_s, qlist=None, mode="normal") -> Session``
  (``starter`` None = an anonymous group admin; ``QuizError`` with a user-facing message and maybe ``markup``) · ``await on_vote(poll_id, uid, name, positions)`` ·
  ``await raw_update(client, update, users, chats)`` (Kurigram ``RawUpdateHandler``) · ``await pause(chat_id)`` ·
  ``await resume(chat_id)`` · ``await stop(chat_id, reason)`` · ``session_for(chat_id)`` · ``active_for(uid)`` ·
  ``result_of(uid, quiz_id)`` (Review / Retry) · ``await boot_notify()`` · ``shutdown()`` · ``stop_chat(chat_id)``
  (bot removed) · ``stats()``.

Depends on: ``core.sender``, ``quiz.store``, Kurigram raw types. Used by: ``handlers.quiz``, ``quiz.autoquiz``, app.
"""

from __future__ import annotations

import asyncio
import html
import logging
import math
import random
import secrets
import time
from dataclasses import dataclass, field

from pyrogram import enums, raw

from bot.core.sender import BULK, POLL
from bot.quiz import store
from bot.ui.keyboards import btn, cb, kb

log = logging.getLogger(__name__)

LABELS = "ABCDEFGHIJ"
GRACE_S = 1.5
READY_S = 1.0  # "get ready" pause before Q1
NEXT_S = 0.7  # private: the next question this long after an answer (the poll shows right / wrong first)
NO_ANSWER_LIMIT = 3
GROUP_MSGS_PER_MIN = 17
TIMES = (10, 15, 20, 30, 45, 60)
ANON_ADMIN_BOT = 1087968824  # Telegram's "Group" user that presses buttons for anonymous admins


def branded(app, question: str) -> str:
    """Owner 2026-10-13 ("poll mai brand k username bhi dal dena"): the brand (``BRANDING``, e.g. ``@channel``) as
    the poll question's last line whenever it fits Telegram's 300 characters."""
    b = (app.config.branding or "").strip()
    return f"{question}\n{b}" if b and len(question) + len(b) + 1 <= 300 else question[:300]


class QuizError(Exception):
    """A reason the quiz cannot start (shown to the user as is; ``markup``: buttons to send with it)."""

    def __init__(self, text: str, markup=None) -> None:
        super().__init__(text)
        self.markup = markup


@dataclass(eq=False)
class Session:
    sid: str
    chat_id: int
    group: bool
    quiz: dict
    qlist: list[int]
    time_s: int
    starter: int
    starter_name: str
    mode: str = "normal"  # normal | retry (practice, never saved)
    state: str = "ready"  # ready | running | paused | done
    pos: int = -1
    start_msg: int | None = None
    ctrl_msg: int | None = None
    sent_at: dict = field(default_factory=dict)  # pos → monotonic time the poll was sent
    polls: list = field(default_factory=list)  # poll ids of this session
    answers: dict = field(default_factory=dict)  # uid → {pos: (choice, ms)}
    names: dict = field(default_factory=dict)
    started: float = field(default_factory=time.time)
    timer: asyncio.TimerHandle | None = None
    pause_timer: asyncio.TimerHandle | None = None
    silent: int = 0  # questions in a row nobody answered
    chat_title: str = ""
    chat_link: str = ""
    busy: asyncio.Lock = field(default_factory=asyncio.Lock)

    @property
    def total(self) -> int:
        return len(self.qlist)

    def question(self, pos: int) -> dict:
        return self.quiz["questions"][self.qlist[pos]]


class QuizEngine:
    def __init__(self, app) -> None:
        self.app = app
        self.sessions: dict[int, Session] = {}
        self.by_starter: dict[int, int] = {}
        self.polls: dict[str, tuple[Session, int]] = {}
        self.finished: dict[tuple[int, str], dict] = {}
        self.file_ids: dict[tuple[int, int], str] = {}
        self.counters = {"started": 0, "finished": 0, "votes": 0}

    # ------------------------------------------------------------ queries
    def session_for(self, chat_id: int) -> Session | None:
        return self.sessions.get(chat_id)

    def active_for(self, uid: int) -> Session | None:
        chat = self.by_starter.get(uid)
        return self.sessions.get(chat) if chat is not None else None

    def by_sid(self, sid: str) -> Session | None:
        return next((s for s in self.sessions.values() if s.sid == sid), None)

    def result_of(self, uid: int, quiz_id: str) -> dict | None:
        r = self.finished.get((uid, quiz_id))
        return r if r and time.time() - r["at"] < 3600 else None

    def stats(self) -> dict:
        return {"running": sum(1 for s in self.sessions.values() if s.state == "running"),
                "paused": sum(1 for s in self.sessions.values() if s.state == "paused"), **self.counters}

    # ------------------------------------------------------------ start
    async def start(self, chat_id: int, quiz: dict, starter, time_s: int, *, group: bool = False,
                    qlist: list[int] | None = None, mode: str = "normal", chat_title: str = "",
                    chat_link: str = "") -> Session:
        sid_of = starter.id if starter is not None else 0
        if sid_of and self.active_for(sid_of) is not None:
            raise QuizError(*self.busy_note(sid_of))
        if chat_id in self.sessions:
            raise QuizError("⏳ A quiz is already running here — wait for it to end (only the person who started it "
                            "can stop it).")
        qlist = list(range(len(quiz["questions"]))) if qlist is None else list(qlist)
        if not qlist:
            raise QuizError("This quiz has no questions to play.")
        time_s = max(5, min(600, int(time_s)))
        if group:  # stay under ~17 messages / minute: cards + poll + explanation cards per question
            most = max(len(quiz["questions"][i].get("cards") or []) + len(quiz["questions"][i].get("xc") or [])
                       for i in qlist) + 1
            time_s = max(time_s, math.ceil(most * 60 / GROUP_MSGS_PER_MIN) + 2)
        name = (" ".join(x for x in (getattr(starter, "first_name", ""), getattr(starter, "last_name", "")) if x)
                or "Player") if starter is not None else "an anonymous admin"
        s = Session(sid=secrets.token_hex(3), chat_id=chat_id, group=group, quiz=quiz, qlist=qlist, time_s=time_s,
                    starter=sid_of, starter_name=name, mode=mode, chat_title=chat_title, chat_link=chat_link)
        self.sessions[chat_id] = s
        if sid_of:
            self.by_starter[sid_of] = chat_id
        self.counters["started"] += 1
        try:
            await self._prefetch_cards(s)
            s.start_msg = (await self.app.sender.send_message(chat_id, self._start_text(s),
                                                               reply_markup=self.controls(s), priority=POLL)).id
            await self.app.repo.running_quizzes.put(chat_id, {"quiz_id": quiz["_id"], "title": quiz.get("title", ""),
                                                             "started_at": store.now_iso()})
        except Exception:
            self._forget(s)
            raise
        s.state = "running"
        await asyncio.sleep(READY_S)
        await self._next(s)
        return s

    def busy_note(self, uid: int) -> tuple[str, object]:
        """Owner 2026-10-13 ("dusri quiz start krne se phle, phli vali ko band kre"): the running / paused quiz of
        ``uid`` and a ⏹ Stop button for it."""
        s = self.active_for(uid)
        if s is None:
            return "", None
        where = f" in <b>{html.escape(s.chat_title or 'a group')}</b>" if s.group else ""
        text = (f"⏳ You already have a quiz {'paused' if s.state == 'paused' else 'running'}{where}: "
                f"<b>{html.escape(s.quiz.get('title') or 'Quiz')}</b> (Q{max(s.pos, 0) + 1}/{s.total}).\n"
                "Stop it first, then start the new one.")
        return text, kb([btn("⏹ Stop that quiz", cb("qzc", 0, s.sid, "stop"), style="danger")])

    def _start_text(self, s: Session) -> str:
        q = s.quiz
        meta = html.escape(q.get("meta_line") or "")
        kind = " · 🔁 wrong / skipped only (practice)" if s.mode == "retry" else ""
        who = (f"\n👤 Started by {html.escape(s.starter_name)}"
               + (" — the group's admins can pause / stop it" if not s.starter else " — they or the group's admins can pause / stop it")
               if s.group else "")
        how = ("Answer each poll — the next one comes when the time is up." if s.group
               else "Answer each poll — the next one comes as soon as you answer.")
        return (f"🎯 <b>{html.escape(q.get('title') or 'Quiz')}</b>\n" + (f"<i>{meta}</i>\n" if meta else "")
                + f"\n🧩 <b>{s.total}</b> questions · ⏱ <b>{s.time_s} s</b> each{kind}{who}\n<i>{how}</i>")

    def controls(self, s: Session, paused: bool = False):
        if paused:
            return kb([btn("▶️ Resume", cb("qzc", 0, s.sid, "resume"), style="success"),
                       btn("⏹ Stop", cb("qzc", 0, s.sid, "stop"), style="danger")])
        return kb([btn("⏸ Pause", cb("qzc", 0, s.sid, "pause")), btn("⏹ Stop", cb("qzc", 0, s.sid, "stop"), style="danger")])

    async def _prefetch_cards(self, s: Session) -> None:
        """The cards' ``file_id``s for the main bot, read once per session (≤ 200 messages per call)."""
        want: dict[int, set[int]] = {}
        for i in s.qlist:
            q = s.quiz["questions"][i]
            for ref in (q.get("cards") or []) + (q.get("xc") or []):
                if (ref["chat_id"], ref["message_id"]) not in self.file_ids:
                    want.setdefault(ref["chat_id"], set()).add(ref["message_id"])
        for chat, ids in want.items():
            ids = sorted(ids)
            for k in range(0, len(ids), 200):
                try:
                    msgs = await self.app.client.get_messages(chat, ids[k:k + 200])
                except Exception as e:
                    log.warning("quiz cards: get_messages(%s) failed: %r — copy_message will be used", chat, e)
                    break
                for m in msgs if isinstance(msgs, list) else [msgs]:
                    photo = getattr(m, "photo", None) if m is not None else None
                    if photo is not None:
                        self.file_ids[(chat, m.id)] = photo.file_id
        if len(self.file_ids) > 50000:  # bounded: forget the oldest half
            for key in list(self.file_ids)[:25000]:
                del self.file_ids[key]

    # ------------------------------------------------------------ the loop
    async def _next(self, s: Session) -> None:
        async with s.busy:
            if s.state != "running":
                return
            if s.pos + 1 >= s.total:
                return await self._finish(s, "done")
            s.pos += 1
            pos = s.pos
            try:
                await self._send(s, pos)
            except Exception as e:
                log.warning("quiz %s: question %d could not be sent: %r", s.sid, pos + 1, e)
                if s.chat_id > 0 and e.__class__.__name__ == "UserBlockedBot":
                    return await self._finish(s, "blocked", post=False)
                self.app.dump.report_error(e, f"quiz {s.quiz.get('_id')} Q{pos + 1}")
            self._arm(s, s.time_s + GRACE_S, pos)

    def _arm(self, s: Session, delay: float, pos: int) -> None:
        if s.timer:
            s.timer.cancel()
        loop = asyncio.get_running_loop()
        s.timer = loop.call_later(delay, lambda: loop.create_task(self._timeout(s, pos)))

    async def _timeout(self, s: Session, pos: int) -> None:
        if s.state != "running" or s.pos != pos:
            return
        answered = any(pos in a for a in s.answers.values())
        s.silent = 0 if answered else s.silent + 1
        if s.silent >= NO_ANSWER_LIMIT:
            if s.group:
                return await self._finish(s, "no answers")
            return await self.pause(s.chat_id, note="⏸ <b>Paused</b> — no answers for a while. Tap ▶️ Resume when ready.")
        if s.group and answered and s.question(pos).get("xc"):
            return await self._explain(s, pos)
        await self._next(s)

    # ------------------------------------------------------------ explanation cards
    async def _explain(self, s: Session, pos: int) -> None:
        """The question's explanation card(s) — caption "💡 Explanation · Q3/50" — then the next question at once
        (owner 2026-10-13: "sahi ho ya galat, rukna nhi hai, next question ka button nhi lgana")."""
        if s.state != "running" or s.pos != pos:
            return
        if s.timer:
            s.timer.cancel()
        try:
            await self._send_cards(s, s.question(pos)["xc"], caption=f"💡 <b>Explanation</b> · Q{pos + 1}/{s.total}")
        except Exception as e:
            log.info("quiz %s: explanation of Q%d not sent: %r", s.sid, pos + 1, e)
            if s.chat_id > 0 and e.__class__.__name__ == "UserBlockedBot":
                return await self._finish(s, "blocked", post=False)
        if s.state == "running" and s.pos == pos:
            await self._next(s)

    async def _send(self, s: Session, pos: int) -> None:
        app, q = self.app, s.question(pos)
        tag = f"[{pos + 1}/{s.total}]"
        if q.get("mode") == "card" and q.get("cards"):
            await self._send_cards(s, q["cards"])
            question, options = f"{tag} Answer from the card above ☝️", list(LABELS[:q["n"]])
        else:
            poll = q.get("poll") or {"q": q.get("text") or "Question", "o": list(LABELS[:q["n"]])}
            question, options = f"{tag} {poll['q']}"[:300], poll["o"]
        question = branded(app, question)
        branding = app.config.branding
        expl = q.get("ex") or (branding if branding and random.random() < float(app.settings.get("quiz.branding_probability") or 0)
                               else None)
        msg = await app.sender.send_poll(s.chat_id, question, options, is_anonymous=False, type=enums.PollType.QUIZ,
                                         correct_option_ids=[int(q["c"])], open_period=s.time_s, explanation=expl,
                                         reply_markup=self.controls(s), priority=POLL)
        s.sent_at[pos] = time.monotonic()
        pid = str(msg.poll.id)
        s.polls.append(pid)
        self.polls[pid] = (s, pos)
        old, s.ctrl_msg = s.ctrl_msg, msg.id
        if old:
            bucket = app.sender.bucket(s.chat_id)
            if not s.group or (bucket is not None and bucket.tokens >= 2):
                asyncio.get_running_loop().create_task(self._quiet(app.sender.edit_markup(s.chat_id, old, None, priority=BULK)))

    async def _send_cards(self, s: Session, refs: list[dict], caption: str | None = None) -> None:
        """One photo, or an album; ``caption`` on the (first) photo."""
        from pyrogram.types import InputMediaPhoto
        app = self.app
        ids = [self.file_ids.get((r["chat_id"], r["message_id"])) for r in refs]
        if all(ids):
            if len(ids) == 1:
                await app.sender.send_photo(s.chat_id, ids[0], caption=caption, priority=POLL)
                return
            await app.sender.call(s.chat_id, app.client.send_media_group, s.chat_id,
                                  [InputMediaPhoto(i, caption=caption if k == 0 else None) for k, i in enumerate(ids)],
                                  priority=POLL)
            return
        for k, r in enumerate(refs):  # no file_id (dump unreadable at start): server-side copy
            await app.sender.copy(s.chat_id, r["chat_id"], r["message_id"], caption=caption if k == 0 else None,
                                  priority=POLL)

    # ------------------------------------------------------------ votes
    async def raw_update(self, client, update, users, chats) -> None:
        if not isinstance(update, raw.types.UpdateMessagePollVote):
            return
        peer = update.peer
        if isinstance(peer, raw.types.PeerUser):
            uid = peer.user_id
            u = users.get(uid)
            name = " ".join(x for x in (getattr(u, "first_name", None), getattr(u, "last_name", None)) if x) if u else ""
        else:  # an anonymous admin (voter = the group itself) / a channel voting in a group
            cid = getattr(peer, "channel_id", None) or getattr(peer, "chat_id", 0)
            c = chats.get(cid)
            uid = -int(f"100{cid}") if getattr(peer, "channel_id", None) else -cid
            hit = self.polls.get(str(update.poll_id))
            if hit and hit[0].chat_id == uid:  # owner 2026-10-13: "anonymous admin ko chat name se rakhlo leaderboard mai"
                name = "🕶 " + (getattr(c, "title", "") or hit[0].chat_title or "Anonymous admin")
            else:
                name = getattr(c, "title", "") or "Anonymous"
        positions = list(getattr(update, "positions", None) or [])
        if not positions and update.options:  # older layers: option bytes are the option index
            positions = [o[0] if isinstance(o, (bytes, bytearray)) and o else 0 for o in update.options]
        await self.on_vote(str(update.poll_id), uid, name or "Player", positions)

    async def on_vote(self, poll_id: str, uid: int, name: str, positions: list[int]) -> None:
        hit = self.polls.get(poll_id)
        if hit is None or not positions:
            return
        s, pos = hit
        if s.state == "done":
            return
        mine = s.answers.setdefault(uid, {})
        if pos in mine:
            return
        self.counters["votes"] += 1
        ms = int((time.monotonic() - s.sent_at.get(pos, time.monotonic())) * 1000)
        mine[pos] = (int(positions[0]), ms)
        s.names[uid] = name[:64]
        s.silent = 0
        if not s.group and uid == s.starter and pos == s.pos and s.state == "running":
            if s.question(pos).get("xc"):  # its explanation card, then the next question (owner: "attempt hone k baad")
                if s.timer:
                    s.timer.cancel()
                loop = asyncio.get_running_loop()
                s.timer = loop.call_later(NEXT_S, lambda: loop.create_task(self._explain(s, pos)))
            else:
                self._arm(s, NEXT_S, pos)

    # ------------------------------------------------------------ pause / resume / stop
    async def pause(self, chat_id: int, note: str = "") -> bool:
        s = self.sessions.get(chat_id)
        if s is None or s.state != "running":
            return False
        s.state = "paused"
        if s.timer:
            s.timer.cancel()
        mins = int(self.app.settings.get("quiz.pause_timeout_min") or 30)
        loop = asyncio.get_running_loop()
        s.pause_timer = loop.call_later(mins * 60, lambda: loop.create_task(self.stop(chat_id, "pause timeout")))
        text = (note or f"⏸ <b>Paused</b> at Q{s.pos + 1}/{s.total}") + f"\n<i>Stops by itself after {mins} min.</i>"
        await self._quiet(self.app.sender.edit_text(chat_id, s.start_msg, self._start_text(s) + "\n\n" + text,
                                                    reply_markup=self.controls(s, paused=True)))
        if s.ctrl_msg:
            await self._quiet(self.app.sender.edit_markup(chat_id, s.ctrl_msg, None, priority=BULK))
            s.ctrl_msg = None
        return True

    async def resume(self, chat_id: int) -> bool:
        s = self.sessions.get(chat_id)
        if s is None or s.state != "paused":
            return False
        if s.pause_timer:
            s.pause_timer.cancel()
        s.state, s.silent = "running", 0
        await self._quiet(self.app.sender.edit_text(chat_id, s.start_msg, self._start_text(s), reply_markup=self.controls(s)))
        await self._next(s)
        return True

    async def stop(self, chat_id: int, reason: str = "stopped") -> bool:
        s = self.sessions.get(chat_id)
        if s is None or s.state == "done":
            return False
        async with s.busy:
            await self._finish(s, reason)
        return True

    async def stop_chat(self, chat_id: int) -> None:
        """The bot was removed / demoted in a group: end its quiz without posting there."""
        s = self.sessions.get(chat_id)
        if s is not None and s.state != "done":
            await self._finish(s, "bot removed", post=False)

    # ------------------------------------------------------------ end
    def _forget(self, s: Session) -> None:
        if self.sessions.get(s.chat_id) is s:
            del self.sessions[s.chat_id]
        if self.by_starter.get(s.starter) == s.chat_id:
            del self.by_starter[s.starter]
        for pid in s.polls:
            self.polls.pop(pid, None)

    def _player(self, s: Session, uid: int) -> dict:
        mine = s.answers.get(uid) or {}
        shown = s.pos + 1 if s.state != "ready" else 0
        correct = sum(1 for p, (ch, _ms) in mine.items() if ch == int(s.question(p)["c"]))
        wrong = len(mine) - correct
        t = sum(ms for _ch, ms in mine.values()) / 1000 + (shown - len(mine)) * s.time_s
        return {"uid": uid, "name": s.names.get(uid) or "Player", "correct": correct, "wrong": wrong,
                "skipped": max(0, shown - len(mine)), "time_s": round(t, 1)}

    async def _finish(self, s: Session, reason: str, post: bool = True) -> None:
        if s.state == "done":
            return
        s.state = "done"
        for h in (s.timer, s.pause_timer):
            if h:
                h.cancel()
        self._forget(s)
        self.counters["finished"] += 1
        app = self.app
        await self._quiet(app.repo.running_quizzes.delete(s.chat_id))
        if s.ctrl_msg:
            asyncio.get_running_loop().create_task(self._quiet(app.sender.edit_markup(s.chat_id, s.ctrl_msg, None, priority=BULK)))
        if s.start_msg:
            end = {"done": "✅ Finished", "no answers": "⏹ Stopped — nobody answered", "pause timeout": "⏹ Stopped after a long pause"}
            await self._quiet(app.sender.edit_text(s.chat_id, s.start_msg, self._start_text(s) + f"\n\n<b>{end.get(reason, '⏹ Stopped')}</b>",
                                                   reply_markup=None, priority=BULK))
        quiz = s.quiz
        try:
            if s.group:
                players = [self._player(s, uid) for uid in s.answers]
                users = [p for p in players if p["uid"] > 0]
                if users:
                    await store.record(app, quiz["_id"], users, "group",
                                       {"chat_id": s.chat_id, "title": s.chat_title, "link": s.chat_link})
                if post:
                    await app.sender.send_message(s.chat_id, self._board(s, players), reply_markup=self._group_end_markup(s))
                return
            me = self._player(s, s.starter)
            self.finished[(s.starter, quiz["_id"])] = {"at": time.time(), "qlist": list(s.qlist[:s.pos + 1]),
                                                       "answers": dict(s.answers.get(s.starter) or {}), "mode": s.mode}
            if len(self.finished) > 2000:
                for k in list(self.finished)[:1000]:
                    del self.finished[k]
            info = None
            if s.mode != "retry" and s.pos >= 0:
                info = (await store.record(app, quiz["_id"], [me], "private")).get(s.starter)
            if post:
                stats = await app.repo.quiz_stats.get(quiz["_id"], copy=False)
                await app.sender.send_message(s.chat_id, self._result_text(s, me, info, stats),
                                              reply_markup=self.result_markup(s.starter, quiz, me))
        except Exception as e:
            app.dump.report_error(e, f"quiz finish {quiz.get('_id')}")

    def _result_text(self, s: Session, me: dict, info: dict | None, stats: dict | None) -> str:
        q = s.quiz
        lines = [f"🏁 <b>{html.escape(q.get('title') or 'Quiz')}</b>", ""]
        lines.append(f"✅ <b>{me['correct']}</b> correct · ❌ <b>{me['wrong']}</b> wrong · ⭕ <b>{me['skipped']}</b> skipped")
        lines.append(f"⏱ {store._dur(me['time_s'])}" + (f" · stopped at Q{s.pos + 1}/{s.total}" if s.pos + 1 < s.total else ""))
        if s.mode == "retry":
            lines.append("\n🔁 <i>Practice of your wrong / skipped questions — not saved.</i>")
        elif info and info.get("ranked"):
            lines.append(f"\n🏆 <b>Ranked attempt</b> — you are <b>#{info['rank']}</b> of {info['of']}")
        else:
            rk = store.rank_of(stats, s.starter)
            mine = ((stats or {}).get("players") or {}).get(str(s.starter), {}).get("ranked") or {}
            lines.append("\n🔁 <i>Practice attempt — not ranked</i>"
                         + (f" · your ranked score <b>{mine.get('score')}</b> (#{rk[0]} of {rk[1]})" if rk else ""))
        lines += ["", "🏆 <b>Top players</b>", store.board_text(stats, s.starter)]
        return "\n".join(lines)

    def result_markup(self, uid: int, quiz: dict, me: dict):
        qid = quiz["_id"]
        rows = [[btn("📝 Review", cb("qz", uid, qid, "rv_0")), btn("▶️ Play again", cb("qz", uid, qid, "play"))]]
        if me["wrong"] + me["skipped"]:
            rows[0].insert(1, btn("🔁 Retry wrong", cb("qz", uid, qid, "retry")))
        rows.append([btn("🏆 Leaderboard", cb("qz", uid, qid, "lb")),
                     btn("📤 Share", switch_inline_query=qid)])
        return kb(*rows)

    def _board(self, s: Session, players: list[dict]) -> str:
        players.sort(key=lambda p: (-p["correct"], p["time_s"]))
        medals = {1: "🥇", 2: "🥈", 3: "🥉"}
        q = s.quiz
        head = f"🏁 <b>{html.escape(q.get('title') or 'Quiz')}</b> — finished\n🧩 {s.pos + 1} of {s.total} questions played\n"
        if not players:
            return head + "\n<i>Nobody answered.</i>"
        rows = [f"{medals.get(i, f'{i}.')} {html.escape(p['name'])} — <b>{p['correct']}</b> ✅ · {store._dur(p['time_s'])}"
                for i, p in enumerate(players[:30], 1)]
        more = f"\n… and {len(players) - 30} more" if len(players) > 30 else ""
        return head + "\n" + "\n".join(rows) + more + "\n\n<i>Group scores are not ranked globally.</i>"

    def _group_end_markup(self, s: Session):
        me = self.app.me.username
        return kb([btn("🏆 Global leaderboard", url=f"https://t.me/{me}?start=lb_{s.quiz['_id']}"),
                   btn("▶️ Play privately", url=f"https://t.me/{me}?start={s.quiz['_id']}")])

    # ------------------------------------------------------------ lifecycle
    async def boot_notify(self) -> None:
        """Chats whose quiz was running when the bot stopped get one note; the list is then cleared."""
        try:
            docs = [d async for d in self.app.repo.running_quizzes.scan()]
            if self.app.repo.mongo_ok:
                docs = await self.app.repo.running_quizzes.find({})
        except Exception as e:
            return log.warning("running_quizzes read failed: %r", e)
        for d in docs:
            chat = d["_id"]
            try:
                await self.app.sender.send_message(
                    int(chat), f"⚠️ <b>Quiz stopped</b> — the bot restarted.\nStart it again: /quiz {d.get('quiz_id', '')}",
                    priority=BULK)
            except Exception:
                pass
            await self._quiet(self.app.repo.running_quizzes.delete(chat))

    def shutdown(self) -> None:
        for s in list(self.sessions.values()):
            for h in (s.timer, s.pause_timer):
                if h:
                    h.cancel()
            s.state = "done"

    @staticmethod
    async def _quiet(coro) -> None:
        try:
            await coro
        except Exception as e:
            log.debug("quiz: ignored %r", e)
