"""Import users from another MongoDB database (the old bot's ``noteshub``) into the bot's store (Mongo + JSON mirror).

Usage (stop the bot first — it caches documents this tool writes)::

    python -m tools.import_users [--db noteshub] [--dry-run]

Owner 2026-10-13: "usko apne current db mai load krlo, users mai se userid ko jror krna … only users ka telegram data
lo" — **only Telegram data** of each user: the id (always), name, username, joined / last seen, blocked the bot,
banned, language. No quizzes, no exam data. A user already here keeps everything; only missing Telegram fields
are filled and ``joined_at`` becomes the earlier date. Imported users get ``tg_db.imported_from``; re-running is safe.
Prints counts only (personal data stays in the database).
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time

from pymongo import AsyncMongoClient

from bot.core.config import load_config
from bot.db.repo import Repo

FIELDS = ("name", "username", "joined_at", "last_seen", "blocked_bot", "banned", "lang_pref")


def telegram_fields(old: dict) -> dict:
    tg = old.get("tg_db") if isinstance(old.get("tg_db"), dict) else {}
    out = {k: tg.get(k) for k in FIELDS if tg.get(k) not in (None, "")}
    out["blocked_bot"] = bool(out.get("blocked_bot", False))
    out["banned"] = bool(out.get("banned", False))
    return out


def update_for(cur: dict | None, tg: dict, source: str) -> dict | None:
    """The repo update that brings ``tg`` into ``cur`` (``None`` = nothing to change)."""
    if cur is None:
        return {"$setOnInsert": {"tg_db": {**{"name": "—", "username": "", "lang_pref": "ask"}, **tg,
                                           "imported_from": source},
                                 "quiz_db": {"created": [], "attempted": []}, "exam_db": {}}}
    have = cur.get("tg_db") or {}
    sets = {f"tg_db.{k}": v for k, v in tg.items() if k not in ("blocked_bot", "banned") and not have.get(k)}
    if tg.get("joined_at") and have.get("joined_at") and tg["joined_at"] < have["joined_at"]:
        sets["tg_db.joined_at"] = tg["joined_at"]
    return {"$set": sets} if sets else None


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--db", default="noteshub", help="source database on the same Mongo server")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    cfg = load_config()
    if not cfg.mongo_url:
        print("MONGO_URL is not set")
        return 2
    src = AsyncMongoClient(cfg.mongo_url, serverSelectionTimeoutMS=15000)
    repo = Repo(cfg.download_root / "_db", cfg.mongo_url)
    await repo.start()
    if not repo.mongo_ok:
        print("the bot's Mongo is not reachable — not importing (it would only reach the JSON mirror)")
        await repo.close()
        return 2
    t0 = time.perf_counter()
    seen = new = filled = same = bad = 0
    async for old in src[a.db]["users"].find({}, {"tg_db": 1}):
        seen += 1
        try:
            uid = int(old["_id"])
        except (TypeError, ValueError):
            bad += 1
            continue
        if uid <= 0:
            bad += 1
            continue
        cur = await repo.users.get(uid)
        upd = update_for(cur, telegram_fields(old), a.db)
        if upd is None:
            same += 1
            continue
        if cur is None:
            new += 1
        else:
            filled += 1
        if not a.dry_run:
            await repo.users.update(uid, upd)
    await repo.json.flush()
    total = await repo.users.count()
    print(f"{a.db}.users: {seen} read · {new} new · {filled} existing users filled in · {same} unchanged · {bad} bad ids"
          f"{' (dry run — nothing written)' if a.dry_run else ''}")
    print(f"users in the bot now: {total} · {time.perf_counter() - t0:.1f}s")
    await repo.close()
    await src.close()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
