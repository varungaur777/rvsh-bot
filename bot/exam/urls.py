"""Exam-URL registry + link-line helpers (spec §14.6). URLs are saved from links users send, from ``/batch`` lines
and from uploaded ``.mht`` files (their page URL is in the MHT header).

Depends on: ``net.fetcher`` (URL normalisation), ``exam.keys``, ``db.repo``.
Used by: exam input flow, ``/links`` + ``/batch`` (P7).

Public API
----------
- ``canonical(url)`` — normalised URL; EDQT section pages (``ViewCandResponse3.aspx``) collapse to the base page and
  ``enckey``/``EncKey`` is unified, so one candidate's sheet is stored once.
- ``url_id(url)`` — SHA-1 of the canonical URL. The document id is ``"<exam_name_slug>|<sha1>"``, so the JSON
  mirror keeps one folder per exam: ``_db/exam_urls/<exam_name_slug>/<sha1>.json`` (owner 2026-10-13: "exam_urls ka
  folder exam ke naam se banao").
- ``await register(repo, url, meta, user_id, status="ok") -> id`` — upsert; never duplicates (the same link filed
  under another exam name before is moved).
- ``await migrate_ids(repo) -> int`` — boot: links stored under the old bare-hash id move to the per-exam id.
- ``parse_line(text) -> dict | None`` — ``(16/09/2026) Exam Name (Shift 2): URL`` → ``{date, exam_name, shift, url}``
  (URL optional, so the same parser reads captions).
- ``hints_from_text(text) -> dict`` — ``Hints`` fields from a caption/line.
- ``await shift_links(repo, exam_name_slug)`` — one URL per shift, no repeats (``/results`` → 🔗 button, ``/links``).
- ``links_txt(links)`` — those as ``(dd/mm/yyyy) Exam (Shift N): URL`` lines.

These URLs are candidate-specific → admin-only views (spec §25).
"""

from __future__ import annotations

import hashlib
import re
import time

from bot.exam.keys import exam_key, name_slug
from bot.net.fetcher import detect_kind, normalize_url
from bot.parsers.common import parse_date
from bot.parsers.models import ExamMeta

_URL = re.compile(r"https?://[^\s<>\"']+", re.I)
_LINE = re.compile(r"^\s*\(?\s*(\d{1,2}[/\-.]\d{1,2}[/\-.]\d{2,4})\s*\)?\s+(.+?)\s*\(\s*Shift\s*[-:]?\s*(\d+)\s*\)\s*:?\s*(https?://\S+)?\s*$", re.I)


def canonical(url: str) -> str:
    u = normalize_url(url)
    u = re.sub(r"(?i)ViewCandResponse\d+\.aspx", "ViewCandResponse.aspx", u)
    return re.sub(r"(?i)([?&])enckey=", r"\1EncKey=", u)


def url_id(url: str) -> str:
    return hashlib.sha1(canonical(url).encode()).hexdigest()


def find_urls(text: str) -> list[str]:
    """Exam URLs (TCS/EDQT) in a message, de-duplicated by canonical form."""
    out, seen = [], set()
    for m in _URL.finditer(text or ""):
        u = m.group(0).rstrip(").,;")
        if detect_kind(u) and canonical(u) not in seen:
            seen.add(canonical(u))
            out.append(u)
    return out


def doc_id(url: str, exam_name_slug: str | None) -> str:
    return f"{exam_name_slug or 'unknown'}|{url_id(url)}"


async def register(repo, url: str, meta: ExamMeta, user_id: int | None, status: str = "ok") -> str:
    uid = doc_id(url, name_slug(meta.exam_name))
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    first = {"first_seen": now, "added_by": user_id}
    for old in await repo.exam_urls.find({"url": canonical(url)}, projection={"_id": 1, "first_seen": 1, "added_by": 1}):
        if old["_id"] != uid:  # filed under another exam name (or the old id): keep when / by whom it came first
            first = {"first_seen": old.get("first_seen") or now, "added_by": old.get("added_by", user_id)}
            await repo.exam_urls.delete(old["_id"])
    await repo.exam_urls.update(uid, {
        "$set": {"url": canonical(url), "source": meta.source or detect_kind(url), "exam_key": exam_key(meta),
                 "exam_name": meta.exam_name, "exam_name_slug": name_slug(meta.exam_name), "level": meta.level,
                 "date": meta.date, "start_hhmm": meta.start_hhmm, "shift": meta.shift, "time": meta.time,
                 "exam_code": meta.exam_code, "status": status, "last_checked": now},
        "$setOnInsert": first,
    })
    return uid


async def migrate_ids(repo) -> int:
    """Links stored before the per-exam folders (id = bare hash) get the id ``<exam_name_slug>|<hash>``."""
    n = 0
    for d in await repo.exam_urls.find({}, projection={"_id": 1}):
        old = d["_id"]
        if "|" in str(old):
            continue
        doc = await repo.exam_urls.get(old)
        if doc is None:
            continue
        doc.pop("_id", None)
        await repo.exam_urls.put(f"{doc.get('exam_name_slug') or 'unknown'}|{old}", doc)
        await repo.exam_urls.delete(old)
        n += 1
    if n:  # the old hash-prefix folders are empty now
        await repo.json.flush()
        base = repo.json.root / "exam_urls"
        for d in sorted(base.iterdir()) if base.is_dir() else []:
            if d.is_dir() and not any(d.iterdir()):
                d.rmdir()
    return n


def parse_line(text: str) -> dict | None:
    m = _LINE.match(text or "")
    if not m:
        return None
    return {"date": parse_date(m.group(1)), "exam_name": m.group(2).strip(), "shift": int(m.group(3)), "url": m.group(4)}


def hints_from_text(text: str | None) -> dict:
    """Best-effort hints from a caption: a full ``(date) name (Shift N)`` line, or just a date / shift."""
    if not text:
        return {}
    first = text.strip().splitlines()[0]
    line = parse_line(first)
    if line:
        return {k: v for k, v in line.items() if k in ("date", "exam_name", "shift") and v}
    out: dict = {}
    if d := parse_date(first):
        out["date"] = d
    if s := re.search(r"shift\s*[-:]?\s*(\d+)", first, re.I):
        out["shift"] = int(s.group(1))
    return out


async def shift_links(repo, exam_name_slug: str) -> list[dict]:
    """One URL per shift of an exam (no repeats): per (date, start time / shift) the most recently checked ``ok`` URL,
    else the most recent one. Sorted by date and time. ``[{date, shift, time, exam_name, url, status}]``."""
    docs = await repo.exam_urls.find({"exam_name_slug": exam_name_slug},
                                     projection={"url": 1, "date": 1, "shift": 1, "time": 1, "start_hhmm": 1,
                                                 "exam_name": 1, "status": 1, "last_checked": 1})
    best: dict[tuple, dict] = {}
    for d in docs:
        if not d.get("url"):
            continue
        key = (d.get("date") or "", d.get("start_hhmm") or f"s{d.get('shift') or ''}")
        rank = (d.get("status") == "ok", d.get("last_checked") or "")
        if key not in best or rank > best[key][0]:
            best[key] = (rank, d)
    seen: set[str] = set()
    out = []
    for key in sorted(best):
        d = best[key][1]
        if d["url"] in seen:  # one candidate's URL saved under two shifts: keep it once
            continue
        seen.add(d["url"])
        out.append({"date": d.get("date"), "shift": d.get("shift"), "time": d.get("time"), "exam_name": d.get("exam_name"),
                    "url": d["url"], "status": d.get("status")})
    return out


def links_txt(links: list[dict]) -> str:
    """``(17/09/2026) Exam name (Shift 1): URL`` — one line per shift, the format ``parse_line`` / ``/batch`` read."""
    lines = []
    for x in links:
        d = "/".join(reversed((x.get("date") or "").split("-"))) or "??/??/????"
        shift = f" (Shift {x['shift']})" if x.get("shift") else ""
        lines.append(f"({d}) {x.get('exam_name') or 'Exam'}{shift}: {x['url']}")
    return "\n".join(lines) + ("\n" if lines else "")
