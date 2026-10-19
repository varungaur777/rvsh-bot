"""``/load_quiz`` — put stored quizzes back (spec §16.1): the ``qz_….json`` files the bot sends to the main dump with
every new quiz (one file, or a zip of many) become playable quizzes again, e.g. on a new server / database.

A quiz file holds everything a quiz needs: questions, answers, poll texts, the **dump references** of its cards
(image dump) and of its question data (``json_ref``, main dump). Nothing is drawn or uploaded again — so the cards
must still be readable by this bot: one card per channel is checked (``get_messages``) before loading.

Rules
-----
- Checked first (``check``): every question has its answer, 2–10 options, poll texts within Telegram's limits, card
  references as numbers; anything else → the file is not loaded and the reason is told.
- Its **quiz ID is kept** (old links and posts keep working) unless that ID is taken by another quiz → a new ID.
- The same quiz already here (same ID and content — ``maker.dump_sig`` — or the same ``fingerprint``) → not loaded
  again; its page is shown.
- Plays start at 0 (the leaderboard is not in the file); who made it (``created_by``) is kept; ``loaded_by`` /
  ``loaded_at`` / ``loaded_from`` are added. A quiz without ``json_ref`` still plays here, but has no HTML file /
  Web App (told).
- Posted once to the quiz dump (``maker._post_once``: a quiz already posted there is not posted again), in the
  background, after the report.

Public API: ``check(obj) -> str`` ('' = fine) · ``await restore(app, obj, uid, reach) -> (status, quiz | None, why)``
(status ``loaded`` | ``new_id`` | ``here`` | ``failed``; ``reach`` = a dict shared by one load, channel → readable)
· ``read_zip(path) -> [(name, bytes)]`` · ``post_later(app, quizzes)``.

Depends on: ``quiz.store``, ``quiz.maker`` (``dump_sig``, ``_post_once``). Used by: ``handlers.loadquiz``.
"""

from __future__ import annotations

import html
import logging
import zipfile
from pathlib import Path

from bot.core.bg import spawn
from bot.quiz import store

log = logging.getLogger(__name__)
MAX_QUESTIONS = 2000
ZIP_FILES = 1000
FILE_MAX = 20 * 2**20
_KEEP_OUT = ("_id", "plays")


def _ref_ok(r) -> bool:
    return isinstance(r, dict) and isinstance(r.get("chat_id"), int) and isinstance(r.get("message_id"), int)


def check(obj) -> str:
    """'' when the quiz file can be loaded, else why not (plain words for the admin)."""
    if not isinstance(obj, dict) or not str(obj.get("_id") or "").startswith("qz_"):
        return "not a quiz file"
    qs = obj.get("questions")
    if not isinstance(qs, list) or not qs:
        return "no questions"
    if len(qs) > MAX_QUESTIONS:
        return f"more than {MAX_QUESTIONS} questions"
    if not isinstance(obj.get("title"), str) or not obj["title"].strip():
        return "no title"
    for i, q in enumerate(qs, 1):
        if not isinstance(q, dict) or not isinstance(q.get("ref"), str):
            return f"question {i} is broken"
        n, c = q.get("n"), q.get("c")
        if not (isinstance(n, int) and 2 <= n <= 10 and isinstance(c, int) and 0 <= c < n):
            return f"question {i}: answer / options missing"
        if q.get("mode") == "poll":
            p = q.get("poll") or {}
            o = p.get("o")
            if not (isinstance(p.get("q"), str) and p["q"] and len(p["q"]) <= 300 and isinstance(o, list) and len(o) == n
                    and all(isinstance(x, str) and 0 < len(x) <= 100 for x in o)):
                return f"question {i}: poll text too long or missing"
        elif q.get("mode") == "card":
            cards = q.get("cards")
            if not (isinstance(cards, list) and 1 <= len(cards) <= 10 and all(_ref_ok(r) for r in cards)):
                return f"question {i}: card missing"
        else:
            return f"question {i}: unknown kind"
        if q.get("xc") is not None and not (isinstance(q["xc"], list) and all(_ref_ok(r) for r in q["xc"])):
            return f"question {i}: explanation card broken"
        if q.get("ex") is not None and not (isinstance(q["ex"], str) and len(q["ex"]) <= 200):
            return f"question {i}: explanation too long"
    if obj.get("json_ref") is not None and not _ref_ok(obj["json_ref"]):
        return "question data reference broken"
    return ""


async def _readable(app, ref: dict, reach: dict) -> bool:
    """One card per channel: can this bot read that channel? (cached in ``reach`` for one load)."""
    chat = ref["chat_id"]
    if chat not in reach:
        try:
            m = await app.client.get_messages(chat, ref["message_id"])
            reach[chat] = bool(m) and not getattr(m, "empty", False)
        except Exception as e:
            log.info("load_quiz: channel %s not readable: %r", chat, e)
            reach[chat] = False
    return reach[chat]


async def restore(app, obj: dict, uid: int, reach: dict) -> tuple[str, dict | None, str]:
    from bot.quiz.maker import dump_sig

    why = check(obj)
    if why:
        return "failed", None, why
    qid = obj["_id"]
    old = await store.get(app, qid)
    if old is not None and dump_sig(old) == dump_sig(obj):
        return "here", old, ""
    if obj.get("fingerprint"):
        twin = await store.find_by_fp(app, obj["fingerprint"])
        if twin is not None:
            return "here", twin, ""
    firsts: dict[int, dict] = {}
    for q in obj["questions"]:
        for r in (q.get("cards") or []) + (q.get("xc") or []):
            firsts.setdefault(r["chat_id"], r)
    for r in firsts.values():
        if not await _readable(app, r, reach):
            return "failed", None, f"its cards are in a channel I can't read (<code>{r['chat_id']}</code>) — add me there"
    doc = {k: v for k, v in obj.items() if k not in _KEEP_OUT}
    doc.update(total=len(doc["questions"]), cards=sum(1 for q in doc["questions"] if q["mode"] == "card"), plays=0,
               loaded_by=uid, loaded_at=store.now_iso(), loaded_from=qid)
    doc.setdefault("created_by", uid)
    doc.setdefault("created_at", store.now_iso())
    doc.setdefault("default_time", int(app.settings.get("quiz.default_question_time_s") or 30))
    doc.setdefault("lang", "en")
    status = "loaded"
    if old is not None:
        status = "new_id"
        qid = store.new_id()
        while await app.repo.quizzes.get(qid, copy=False):
            qid = store.new_id()
    saved = await app.repo.quizzes.put(qid, doc)
    if doc.get("exam_key"):
        await app.repo.exams.update(doc["exam_key"], {"$addToSet": {"quiz_ids": qid}})
    if isinstance(doc.get("created_by"), int):
        await app.repo.users.update(doc["created_by"], {"$addToSet": {"quiz_db.created": qid}}, upsert=False)
    log.info("quiz %s loaded by %s (from %s, %d q)", qid, uid, obj["_id"], doc["total"])
    return status, saved, ""


def read_zip(path) -> list[tuple[str, bytes]]:
    """The ``.json`` files of a zip (natural order, ≤ ``ZIP_FILES``, each ≤ ``FILE_MAX``; bigger ones skipped)."""
    from bot.jsonq.parse import _natural

    out = []
    with zipfile.ZipFile(path) as z:
        entries = [i for i in z.infolist() if not i.is_dir() and i.filename.lower().endswith(".json")
                   and "__MACOSX/" not in i.filename and not Path(i.filename).name.startswith(".")]
        entries.sort(key=lambda i: _natural(i.filename))
        for i in entries[:ZIP_FILES]:
            if i.file_size <= FILE_MAX:
                with z.open(i) as f:
                    data = f.read(FILE_MAX + 1)
                if len(data) <= FILE_MAX:
                    out.append((Path(i.filename).name, data))
    return out


def post_later(app, quizzes: list[dict]) -> None:
    """Each loaded quiz to the quiz dump, one after another, after the report (never twice in one channel)."""
    if not quizzes:
        return

    async def run():
        from bot.quiz.maker import _announce_post
        for q in quizzes:
            try:
                await _announce_post(app, q)
            except Exception as e:
                log.info("load_quiz: dump post of %s failed: %r", q.get("_id"), e)

    spawn(run(), "load-quiz-posts")


def line(status: str, quiz: dict | None, name: str, why: str, me: str) -> str:
    """One report line."""
    if quiz is None:
        return f"❌ <i>{html.escape(name)}</i> — {why}"
    link = f"<a href=\"https://t.me/{me}?start={quiz['_id']}\">{html.escape((quiz.get('title') or 'Quiz')[:70])}</a>"
    tail = {"loaded": "", "new_id": f" · new ID <code>{quiz['_id']}</code> (its old ID is taken)",
            "here": " · ♻️ already here"}.get(status, "")
    return f"{'✅' if status != 'here' else '♻️'} {link}{tail}" + ("" if quiz.get("json_ref") else " · <i>no HTML / Web App</i>")
