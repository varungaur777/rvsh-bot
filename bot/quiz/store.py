"""Quizzes and their stats (spec §9.1, §9.4) — one ``quizzes`` doc (questions only, rarely rewritten) and one
``quiz_stats`` doc per quiz holding every player.

Ranking (like the official @QuizBot): a user's **first** attempt — private, Web App or a group quiz played to the
end — is their ranked result, frozen; later attempts are practice (counted: ``attempts``, ``last_score``,
``best_practice_score``). Group play (owner 2026-10-19: "quiz mai group score bhi global ranking mai dalo"; chose:
the first attempt anywhere, only a group quiz that ran to its last question) also adds ``plays``, ``groups_played``
and each player's ``group`` counter; a group quiz stopped early ranks nobody (``ranks=False``). Sort: score ↓,
time ↑, earlier ``at`` first. ``top100`` is cached in the doc.

Writes happen once per finished session (never per answer), under a per-quiz ``asyncio.Lock`` (one process owns the
data — instance lease), as **one** ``update`` with ``$inc`` / ``$set`` / ``$max`` — ``top100`` included: a ranked
result never changes, so the new top 100 = the old one + the new ranked results, sorted, cut (no re-sort of every
player). A player's rank is one pass over the players (``position``). Load test 2026-10-13: 10 000 plays of one quiz
(10 000 players) — before ~0.5 s per play at the end (whole-document copies + full sorts), now flat (spec §17.9).

Public API
----------
- ``new_id() -> "qz_xxxxxx"`` · ``await get(app, quiz_id)`` (read-only, cached) · ``await find_by_fp(app, fp)``
- ``await record(app, quiz_id, results, mode, group=None, ranks=True) -> {uid: {"ranked": bool, "rank": int | None,
  "of": int}}`` — ``results`` [{uid, name, correct, wrong, skipped, time_s}]; ``mode``: private | webapp | group |
  practice (practice: counted, never ranked); ``ranks=False`` = counted, never ranked (a group quiz stopped early).
- ``ranking(stats) -> list[(uid, entry)]`` · ``rank_of(stats, uid) -> (rank, total) | None`` · ``board_text(stats, viewer)``
- ``await delete(app, quiz_id)``.

Depends on: ``db.repo``. Used by: quiz engine, Web App, handlers.
"""

from __future__ import annotations

import asyncio
import html
import secrets
import string
import time

_ALPHA = string.ascii_letters + string.digits
_locks: dict[str, asyncio.Lock] = {}


def new_id() -> str:
    return "qz_" + "".join(secrets.choice(_ALPHA) for _ in range(6))


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


async def get(app, quiz_id: str) -> dict | None:
    if not quiz_id or not quiz_id.startswith("qz_"):
        return None
    return await app.repo.quizzes.get(quiz_id, copy=False)


async def find_by_fp(app, fp: str) -> dict | None:
    found = await app.repo.quizzes.find({"fingerprint": fp}, limit=1)
    return found[0] if found else None


def _key(entry: dict) -> tuple:
    r = entry["ranked"]
    return (-r.get("score", 0), r.get("time_s", 0), r.get("at", ""))


def ranking(stats: dict | None) -> list[tuple[str, dict]]:
    players = (stats or {}).get("players") or {}
    rows = [(uid, p) for uid, p in players.items() if p.get("ranked")]
    rows.sort(key=lambda x: _key(x[1]))
    return rows


def _flat_key(e: dict) -> tuple:
    return (-e.get("score", 0), e.get("time_s", 0), e.get("at", ""))


def position(stats: dict | None, uid: int | str) -> tuple[int | None, int]:
    """(rank or ``None``, ranked players) in one pass — the same order as ``ranking`` (ties: earlier first)."""
    players = (stats or {}).get("players") or {}
    mine = (players.get(str(uid)) or {}).get("ranked")
    if not mine:
        return None, sum(1 for p in players.values() if p.get("ranked"))
    me, score, t, at = str(uid), mine.get("score", 0), mine.get("time_s", 0), mine.get("at", "")
    better = total = 0
    seen = False
    for u, p in players.items():
        r = p.get("ranked")
        if not r:
            continue
        total += 1
        sc = r.get("score", 0)
        if sc != score:
            better += sc > score
            continue
        if u == me:
            seen = True
            continue
        k = (r.get("time_s", 0), r.get("at", ""))
        if k < (t, at) or (k == (t, at) and not seen):
            better += 1
    return better + 1, total


def rank_of(stats: dict | None, uid: int | str) -> tuple[int, int] | None:
    rank, total = position(stats, uid)
    return (rank, total) if rank else None


async def record(app, quiz_id: str, results: list[dict], mode: str, group: dict | None = None,
                 ranks: bool = True) -> dict:
    """One finished session → ``quiz_stats`` (see module doc). Returns each player's ranked flag and rank."""
    lock = _locks.setdefault(quiz_id, asyncio.Lock())
    async with lock:
        stats = await app.repo.quiz_stats.get(quiz_id, copy=False) or {}  # read only
        players = stats.get("players") or {}
        upd: dict = {"$inc": {"plays": 1}, "$set": {}, "$max": {}}
        ranked_now = set()
        fresh: list[dict] = []
        now = now_iso()
        for r in results:
            uid = str(r["uid"])
            p = f"players.{uid}"
            score = int(r.get("correct", 0))
            upd["$set"][f"{p}.name"] = (r.get("name") or "")[:64]
            upd["$set"][f"{p}.last_score"] = score
            upd["$set"][f"{p}.last_played"] = now
            upd["$inc"][f"{p}.attempts"] = 1
            counter = {"private": "private", "practice": "private", "webapp": "webapp", "group": "group"}[mode]
            upd["$inc"][f"{p}.{counter}"] = 1
            if ranks and mode in ("private", "webapp", "group") and not (players.get(uid) or {}).get("ranked"):
                upd["$set"][f"{p}.ranked"] = {"score": score, "correct": int(r.get("correct", 0)),
                                              "wrong": int(r.get("wrong", 0)), "skipped": int(r.get("skipped", 0)),
                                              "time_s": round(float(r.get("time_s", 0)), 1), "at": now}
                ranked_now.add(uid)
                fresh.append({"uid": int(uid), "name": (r.get("name") or "")[:64], **upd["$set"][f"{p}.ranked"]})
            else:
                upd["$max"][f"{p}.best_practice_score"] = score
        if group:
            gp = [g for g in stats.get("groups_played") or [] if g.get("chat_id") != group.get("chat_id")]
            upd["$set"]["groups_played"] = ([group] + gp)[:200]
        if not upd["$max"]:
            del upd["$max"]
        if fresh:
            top = stats.get("top100")
            if top is None:  # a document from before top100 was kept: built once from every player
                top = [{"uid": int(u), "name": p.get("name", ""), **p["ranked"]} for u, p in ranking(stats)[:100]]
            upd["$set"]["top100"] = sorted([*top, *fresh], key=_flat_key)[:100]
        new = await app.repo.quiz_stats.update(quiz_id, upd, copy=False)
        await app.repo.quizzes.update(quiz_id, {"$inc": {"plays": 1}}, upsert=False, copy=False)
        for r in results:
            if mode != "group" or str(r["uid"]) in ranked_now:  # 👤 My Stats lists the quizzes with a result
                await app.repo.users.update(r["uid"], {"$addToSet": {"quiz_db.attempted": quiz_id}}, upsert=False)
    out = {}
    if mode == "group":  # only the players ranked by this game get their rank (the group board shows it)
        return {r["uid"]: ({"ranked": True, **dict(zip(("rank", "of"), position(new, r["uid"])))}
                           if str(r["uid"]) in ranked_now else {"ranked": False, "rank": None, "of": None})
                for r in results}
    for r in results:
        rank, total = position(new, r["uid"])
        out[r["uid"]] = {"ranked": str(r["uid"]) in ranked_now, "rank": rank, "of": total}
    return out


def board_text(stats: dict | None, viewer: int | None = None, n: int = 10) -> str:
    top = (stats or {}).get("top100")
    if isinstance(top, list) and n <= 100:  # kept sorted on every ranked play: no sort of every player per view
        rows = [(str(e.get("uid")), {"name": e.get("name"), "ranked": e}) for e in top]
    else:
        rows = ranking(stats)
    if not rows:
        return "<i>No ranked players yet — the first attempt of each player counts (private, Web App or a whole group quiz).</i>"
    medals = {1: "🥇", 2: "🥈", 3: "🥉"}
    lines = []
    for i, (uid, p) in enumerate(rows[:n], 1):
        r = p["ranked"]
        me = " ← you" if viewer is not None and str(uid) == str(viewer) else ""
        lines.append(f"{medals.get(i, f'{i}.')} {html.escape(p.get('name') or 'Player')} — <b>{r['score']}</b> · "
                     f"{_dur(r.get('time_s', 0))}{me}")
    if viewer is not None:
        rk = rank_of(stats, viewer)
        if rk and rk[0] > n:
            pct = 100 - (rk[0] - 1) * 100 // max(1, rk[1])
            lines.append(f"…\n#{rk[0]} of {rk[1]} — you (top {max(1, 100 - pct + 1)}%)")
    return "\n".join(lines)


def _dur(s: float) -> str:
    s = int(round(s or 0))
    return f"{s // 60}m {s % 60:02d}s" if s >= 60 else f"{s}s"


async def delete(app, quiz_id: str) -> None:
    await app.repo.quizzes.delete(quiz_id)
    await app.repo.quiz_stats.delete(quiz_id)
