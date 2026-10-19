"""Exam-URL registry + link-line helpers (spec §14.6). URLs are saved from links users send, from ``/batch`` lines
and from uploaded ``.mht`` files (their page URL is in the MHT header).

Depends on: ``net.fetcher`` (URL normalisation), ``exam.keys``, ``db.repo``.
Used by: exam input flow, ``/links`` + ``/batch`` (P7).

Public API
----------
- ``canonical(url)`` — normalised URL; EDQT section pages (``ViewCandResponse3.aspx``) collapse to the base page and
  ``enckey``/``EncKey`` is unified, so one candidate's sheet is stored once.
- ``shift_code(url)`` — TCS paper code + session read from the URL (``33128O26183S9``): one shift, both languages.
- ``url_id(url)`` — SHA-1 of the canonical URL. The document id is ``"<exam_name_slug>|<sha1>"``, so the JSON
  mirror keeps one folder per exam: ``_db/exam_urls/<exam_name_slug>/<sha1>.json`` (owner 2026-10-13: "exam_urls ka
  folder exam ke naam se banao").
- ``await register(repo, url, meta, user_id, status="ok", langs=None, mix=None) -> id`` — upsert; never duplicates
  (the same link filed under another exam name before is moved). ``langs`` = the sheet's language(s) (``["hi"]``,
  ``["en"]``, ``["en", "hi"]`` for an EDQT link fetched with both versions or a TCS sheet half Hindi half English,
  ``["bn"]`` … for another Indian language) — the **medium of that candidate's sheet** (owner 2026-10-19: "tcs and
  edqt exam link extract language wise krna hai, hindi and english alag"; a TCS sheet holds only the candidate's own
  language, so one Hindi and one English link per shift give both versions). ``mix`` = ``schema.lang_census``
  (questions per language) — a link without it was read before 2026-10-19's check (Bengali was taken for English,
  mixed sheets were not seen) and counts as **not checked** (``needs_check``).
- ``sheet_langs(summary) -> (langs, mix)`` — what ``register`` stores, from an ``inspect_sheets`` summary.
- ``mixed_note(mix) -> str`` — ``"mixed: 55 Hindi + 45 English"`` / ``"Bengali"`` / ``""``.
- ``await migrate_ids(repo) -> int`` — boot: links stored under the old bare-hash id move to the per-exam id.
- ``parse_line(text) -> dict | None`` — ``(16/09/2026) Exam Name (Shift 2): URL`` → ``{date, exam_name, shift, url}``
  (URL optional, so the same parser reads captions).
- ``hints_from_text(text) -> dict`` — ``Hints`` fields from a caption/line.
- ``await shift_links(repo, exam_name_slug, lang=None)`` — one URL per shift, no repeats; with ``lang`` only links
  whose sheet has that language (the one with the fewest questions in another language first).
- ``await links_by_lang(repo, slugs=None) -> {"hi": [...], "en": [...], "unknown": [...], "other": [...]}`` — one
  URL per exam + shift **per language** (``/links``, ``/results`` → 🔗; Hindi and English only); ``unknown``: saved
  links not checked yet (``needs_check`` — ``/links`` checks them on request); ``other``: links of another language
  (kept, never in the files).
- ``needs_check(doc) -> bool`` — read before the census (no ``mix``) and not yet found ``unreadable``.
- ``unreadable(doc) -> bool`` — no census, and the last check found no saved copy and a dead link (``mark_status(...,
  unreadable=True)``): its language was never read, so it is never in the files nor checked again (owner 2026-10-19:
  "alag gino, files se bahar"); ``register`` clears the mark when the sheet is read after all.
- ``await exams_with_links(repo) -> [{slug, name, total, hi, en, unknown, other, mixed, dead}]`` — the ``/links``
  exam list (``dead``: ``unreadable`` links, not in ``hi`` / ``en``).
- ``links_txt(links)`` — those as ``(dd/mm/yyyy) Exam (Shift N): URL`` lines (sorted exam → date → time); a mixed
  sheet gets a ``# mixed: 55 Hindi + 45 English`` line above it; a sheet without an exam name → ``# NAME NEEDED
  [exam code] …`` (``/batch`` skips ``#`` lines).
- ``await add_mht_page(repo, url, ref, page, pages)`` / ``await saved_mht(repo, url) -> [ref] | []`` — the dump copy
  of each ``.mht`` page fetched from a link (owner 2026-10-19: "dono ki mht ko dump mai save krkr unki file unique id,
  file ka link dump se sab db mai save krlia kro bina repeation, link to expire ho jate hai link na kam kre to mht kam
  kregi"): collection ``exam_mht``, id = ``url_id``, ``{url, pages, files: {"<page>": {file_unique_id, chat_id,
  message_id, link, name, size, missing}}}``. A page already saved is replaced only by a better copy (fewer page
  files missing); ``saved_mht`` returns the pages in order only when every page is there (an EDQT link = several
  section pages, each with both languages); ``mht_state(repo, urls)`` = which links have a complete copy.
- ``read_links(text) -> [(url, hints)]`` — the links of a pasted text / ``.txt`` (``/links`` filter): a ``(date) Exam
  (Shift N): URL`` line gives hints, bare URLs too; ``#`` lines skipped; de-duplicated by canonical URL.

These URLs are candidate-specific → admin-only views (spec §25).
"""

from __future__ import annotations

import hashlib
import re
import time

from bot.exam.keys import exam_key, name_slug
from bot.net.fetcher import detect_kind, normalize_url
from bot.parsers.common import OTHER_LANGS, parse_date
from bot.parsers.models import ExamMeta

_URL = re.compile(r"https?://[^\s<>\"']+", re.I)
_LINE = re.compile(r"^\s*\(?\s*(\d{1,2}[/\-.]\d{1,2}[/\-.]\d{2,4})\s*\)?\s+(.+?)\s*\(\s*Shift\s*[-:]?\s*(\d+)\s*\)\s*:?\s*(https?://\S+)?\s*$", re.I)


def canonical(url: str) -> str:
    u = normalize_url(url)
    u = re.sub(r"(?i)ViewCandResponse\d+\.aspx", "ViewCandResponse.aspx", u)
    return re.sub(r"(?i)([?&])enckey=", r"\1EncKey=", u)


def url_id(url: str) -> str:
    return hashlib.sha1(canonical(url).encode()).hexdigest()


_TCS_SHIFT = re.compile(r"/(\d+O[0-9A-Za-z]+)/\1S(\d+)D\d+/")


def shift_code(url: str) -> str | None:
    """TCS: paper code + session from the URL alone — ``…/33128O26183/33128O26183S9D10523/…`` → ``33128O26183S9`` —
    the same for the Hindi- and the English-medium sheet of one shift (owner 2026-10-19: "ache se match krne ka trika
    bnana fast"; checked on the 86 saved links: 17 codes, each exactly one exam + date + start time). No request,
    no parsing. ``None`` for EDQT and other shapes."""
    m = _TCS_SHIFT.search(canonical(url))
    return f"{m.group(1)}S{m.group(2)}" if m else None


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


LANG_NAMES = {"hi": "Hindi", "en": "English"}
ALL_NAMES = {**LANG_NAMES, **OTHER_LANGS}


def sheet_langs(summary: dict | None) -> tuple[list[str], dict | None]:
    """``(langs, mix)`` to save for a link from its ``inspect_sheets`` summary: the medium question by question
    (``schema.medium``); a sheet without letters (all images) keeps the parser's languages."""
    summary = summary or {}
    med = summary.get("medium") or {}
    langs = list(med.get("langs") or summary.get("languages") or [])
    return langs, summary.get("census")


def mixed_note(mix: dict | None) -> str:
    """``"mixed: 55 Hindi + 45 English"`` for a mixed sheet, ``"Bengali"`` for another language, else ``""``."""
    from bot.exam.schema import medium
    if not mix:
        return ""
    med = medium(mix)
    if med["other"]:
        return ALL_NAMES.get(med["other"], med["other"])
    if not med["mixed"]:
        return ""
    if len(med["langs"]) > 1:
        return "mixed: " + " + ".join(f"{mix.get(k, 0)} {ALL_NAMES[k]}" for k in ("hi", "en"))
    major = med["langs"][0]
    return "mixed: " + " + ".join(f"{n} {ALL_NAMES.get(k, k)}" for k, n in
                                  ((major, mix.get(major, 0)), *med["mixed"].items()))


def needs_check(d: dict) -> bool:
    """A saved link to read (again): read before the census (``mix``) — its language may be wrong (Bengali was taken
    for English) and its mix is not known — unless a check already found it ``unreadable``. A dead link is read too:
    its dump ``.mht`` may be there."""
    return "mix" not in d and not d.get("unreadable")


def unreadable(d: dict) -> bool:
    """A link whose language could not be read: no census, no saved copy, and the link is dead (owner 2026-10-19).
    Never in the files, never checked again (a new request would fail the same way)."""
    return "mix" not in d and bool(d.get("unreadable"))


def _unverified_dead(d: dict) -> bool:
    """Not working and its language never read question by question: kept out of the files (owner 2026-10-19)."""
    return "mix" not in d and d.get("status") not in (None, "ok") and not d.get("mht_good")


async def register(repo, url: str, meta: ExamMeta, user_id: int | None, status: str = "ok",
                   langs: list[str] | None = None, mix: dict | None = None) -> str:
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
                 "exam_code": meta.exam_code, "status": status, "last_checked": now,
                 **({"langs": sorted(set(langs))} if langs else {}), **({"mix": mix} if mix is not None else {})},
        "$setOnInsert": first,
        "$unset": {"unreadable": ""},  # the sheet was read after all
    })
    return uid


async def mark_status(repo, url: str, status: str, unreadable: bool = False) -> int:
    """A saved link was checked again and failed (``http_404``, ``not_exam`` …): its ``status`` is updated (``/links``
    and ``shift_links`` prefer working links); ``unreadable`` — no saved copy either, its language cannot be read
    (``unreadable()``). Returns how many saved copies were updated."""
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    docs = await repo.exam_urls.find({"url": canonical(url)}, projection={"_id": 1})
    for d in docs:
        await repo.exam_urls.update(d["_id"], {"$set": {"status": status, "last_checked": now,
                                                        **({"unreadable": True} if unreadable else {})}}, upsert=False)
    return len(docs)


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


_PROJ = {"url": 1, "date": 1, "shift": 1, "time": 1, "start_hhmm": 1, "exam_name": 1, "exam_name_slug": 1,
         "exam_code": 1, "status": 1, "last_checked": 1, "langs": 1, "mix": 1, "unreadable": 1}


def _share(d: dict, lang: str | None) -> float:
    """How much of the sheet is in ``lang`` (1.0 = all): from its census; a link not checked yet (no census) by its
    saved languages — ``_best_per_shift`` puts every checked link first anyway (an unchecked "English" may be Bengali)."""
    langs = d.get("langs") or []
    if lang is None:
        return 1.0
    mix = d.get("mix")
    if mix is None:
        return 0.8 if len(langs) == 1 else 0.4
    total = sum(mix.values())
    return (mix.get(lang, 0) + mix.get("both", 0)) / total if total else (1.0 if len(langs) == 1 else 0.5)


def _best_per_shift(docs: list[dict], lang: str | None = None, per_exam: bool = False) -> list[dict]:
    """Per (exam,) date, start time / shift: the working link, the sheet with the most questions in ``lang`` (fewest
    in another — owner 2026-10-19: mixed sheets), then the most recently checked. Sorted exam → date → time; one
    candidate's URL never twice. Only links whose ``langs`` hold ``lang`` (another language's links never); a dead
    link whose language was never read question by question never (``_unverified_dead``)."""
    best: dict[tuple, tuple] = {}
    for d in docs:
        langs = d.get("langs") or []
        if not d.get("url") or (lang is not None and lang not in langs) or _unverified_dead(d):
            continue
        key = ((d.get("exam_name") or "~", d.get("exam_name_slug") or "") if per_exam else ()) + \
            (d.get("date") or "", d.get("start_hhmm") or f"s{d.get('shift') or ''}")
        # ``mht_good`` (set by /batch only): a complete saved .mht — usable even after the link expired
        rank = (d.get("status") == "ok" or bool(d.get("mht_good")), "mix" in d or lang is None, round(_share(d, lang), 3),
                bool(d.get("mht_good")), d.get("last_checked") or "")
        if key not in best or rank > best[key][0]:
            best[key] = (rank, d)
    seen: set[str] = set()
    out = []
    for key in sorted(best):
        d = best[key][1]
        if d["url"] in seen:  # one candidate's URL saved under two shifts: keep it once
            continue
        seen.add(d["url"])
        out.append(_row(d))
    return out


def _row(d: dict) -> dict:
    return {"date": d.get("date"), "shift": d.get("shift"), "time": d.get("time"), "exam_name": d.get("exam_name"),
            "exam_code": d.get("exam_code"), "url": d["url"], "status": d.get("status"), "langs": d.get("langs") or [],
            "start_hhmm": d.get("start_hhmm"), "exam_name_slug": d.get("exam_name_slug"), "mix": d.get("mix")}


async def shift_links(repo, exam_name_slug: str, lang: str | None = None) -> list[dict]:
    """One URL per shift of an exam (no repeats): per (date, start time / shift) the most recently checked ``ok`` URL,
    else the most recent one. Sorted by date and time. ``[{date, shift, time, exam_name, url, status, langs}]``.
    ``lang``: only links whose sheet has that language."""
    docs = await repo.exam_urls.find({"exam_name_slug": exam_name_slug}, projection=_PROJ)
    return _best_per_shift(docs, lang)


async def links_by_lang(repo, slugs: list[str] | None = None) -> dict[str, list[dict]]:
    """``{"hi": [...], "en": [...], "unknown": [...], "other": [...], "dead": [...]}`` for the exams ``slugs`` (None =
    every exam): one link per exam + date + start time per language; ``unknown`` = every saved link (one per URL) to
    check (``needs_check``); ``other`` = links of another language; ``dead`` = ``unreadable`` links (one per URL)."""
    flt = {"exam_name_slug": {"$in": list(slugs)}} if slugs else {}
    docs = await repo.exam_urls.find(flt, projection=_PROJ)
    out = {lang: _best_per_shift(docs, lang, per_exam=True) for lang in LANG_NAMES}
    seen: set[str] = set()
    unknown, other, dead = [], [], []
    for d in sorted(docs, key=lambda d: (d.get("exam_name") or "~", d.get("date") or "", d.get("start_hhmm") or "")):
        if not d.get("url") or d["url"] in seen:
            continue
        if needs_check(d):
            seen.add(d["url"])
            unknown.append(_row(d))
        elif unreadable(d):
            seen.add(d["url"])
            dead.append(_row(d))
        elif not set(d.get("langs") or []) & set(LANG_NAMES):
            seen.add(d["url"])
            other.append(_row(d))
    out["unknown"], out["other"], out["dead"] = unknown, other, dead
    return out


async def exams_with_links(repo) -> list[dict]:
    """Every exam with saved links: ``[{slug, name, total, hi, en, unknown, other, mixed, dead}]`` (newest name
    spelling wins), sorted by name. ``hi`` / ``en`` count links whose sheet has that language, ``other`` links of
    another language, ``mixed`` checked links with both Hindi and English questions, ``unknown`` links to check,
    ``dead`` links whose language could not be read (``unreadable`` — not in ``hi`` / ``en``)."""
    docs = await repo.exam_urls.find({}, projection={"exam_name_slug": 1, "exam_name": 1, "langs": 1, "exam_code": 1,
                                                     "mix": 1, "status": 1, "unreadable": 1})
    out: dict[str, dict] = {}
    for d in docs:
        slug = d.get("exam_name_slug") or "unknown"
        e = out.setdefault(slug, {"slug": slug, "name": d.get("exam_name") or "", "total": 0, "hi": 0, "en": 0,
                                  "unknown": 0, "other": 0, "mixed": 0, "dead": 0, "code": d.get("exam_code") or ""})
        e["name"] = e["name"] or d.get("exam_name") or ""
        e["total"] += 1
        if unreadable(d):
            e["dead"] += 1
            continue
        langs = d.get("langs") or []
        for lang in LANG_NAMES:
            e[lang] += lang in langs
        e["unknown"] += needs_check(d)
        e["other"] += bool(langs) and not set(langs) & set(LANG_NAMES)
        e["mixed"] += mixed_note(d.get("mix")).startswith("mixed")
    return sorted(out.values(), key=lambda e: ((e["name"] or "~").lower(), e["slug"]))


def links_txt(links: list[dict]) -> str:
    """``(17/09/2026) Exam name (Shift 1): URL`` — one line per shift, the format ``parse_line`` / ``/batch`` read. A
    sheet without an exam name: ``# NAME NEEDED [exam code] (date) (Shift N): URL`` (``/batch`` skips it until named)."""
    lines = []
    for x in links:
        d = "/".join(reversed((x.get("date") or "").split("-"))) or "??/??/????"
        shift = f" (Shift {x['shift']})" if x.get("shift") else ""
        if note := mixed_note(x.get("mix")):
            lines.append(f"# {note}")
        if x.get("exam_name"):
            lines.append(f"({d}) {x['exam_name']}{shift}: {x['url']}")
        else:
            lines.append(f"# NAME NEEDED [{x.get('exam_code') or '?'}] ({d}){shift}: {x['url']}")
    return "\n".join(lines) + ("\n" if lines else "")


async def add_mht_page(repo, url: str, ref: dict, page: int, pages: int, by: int | None = None,
                       missing: int = 0) -> bool:
    """Remember the dump copy of page ``page`` (0-based) of ``pages`` fetched from ``url``; ``missing`` = page files
    (images, CSS) the fetch could not get. A page already saved is replaced only by a **better** copy (fewer missing —
    owner 2026-10-19: "mht ager phle khrab aai baad mai … dubara achi mht aati hai, to usko db mai update krdena");
    the same or a worse copy is not saved again. True = saved / replaced."""
    if not ref or not ref.get("file_unique_id"):
        return False
    key = url_id(url)
    doc = await repo.exam_mht.get(key) or {}
    files = doc.get("files") or {}
    old = files.get(str(page))
    if old is not None and int(old.get("missing") or 0) <= int(missing):
        return False
    if old is None and any(f.get("file_unique_id") == ref["file_unique_id"] for f in files.values()):
        return False
    keep = {k: ref.get(k) for k in ("file_unique_id", "chat_id", "message_id", "link", "name", "size")}
    keep["missing"] = int(missing)
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    update = {"$set": {"url": canonical(url), "pages": max(int(pages), int(doc.get("pages") or 0)),
                       f"files.{page}": keep, "updated_at": now},
              "$setOnInsert": {"saved_at": now, "by": by}}
    if old is not None:
        update["$inc"] = {"replaced": 1}
    await repo.exam_mht.update(key, update)
    return True


def _pages(doc: dict) -> list[dict]:
    files, pages = doc.get("files") or {}, int(doc.get("pages") or 0)
    if not pages or any(str(i) not in files for i in range(pages)):
        return []
    return [files[str(i)] for i in range(pages)]


async def saved_mht(repo, url: str) -> list[dict]:
    """The saved dump copies of every page of ``url`` in page order — ``[]`` unless all of them are saved. Each has
    ``missing`` (page files the fetch could not get; 0 = a complete copy)."""
    return _pages(await repo.exam_mht.get(url_id(url)) or {})


async def mht_state(repo, urls) -> dict[str, int]:
    """``{canonical url: missing page files}`` for the links with a complete saved copy (0 = good) — ``/batch`` picks,
    per shift and language, a link whose saved copy is good."""
    out: dict[str, int] = {}
    for u in urls:
        pages = _pages(await repo.exam_mht.get(url_id(u)) or {})
        if pages:
            out[canonical(u)] = sum(int(p.get("missing") or 0) for p in pages)
    return out


def read_links(text: str) -> list[tuple[str, dict]]:
    """Links of a pasted list / ``.txt`` (owner's raw lists: duplicates, stray spaces, ``#``, ``///``): ``[(url, hints)]``
    in their order, one per canonical URL. A ``(date) Exam (Shift N): URL`` line also gives its hints (date, exam name,
    shift) — used when the sheet itself does not print them (EDQT admin view)."""
    out: list[tuple[str, dict]] = []
    seen: set[str] = set()
    for line in (text or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        hints = {}
        parsed = parse_line(line)
        if parsed and parsed.get("url"):
            hints = {k: v for k, v in parsed.items() if k in ("date", "exam_name", "shift") and v}
        for u in find_urls(line):
            c = canonical(u)
            if c not in seen:
                seen.add(c)
                out.append((u, hints))
    return out
