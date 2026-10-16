"""Exam JSON (spec §12.3) — the question bank of one exam: exam info, sections, questions, options, correct answers,
image references. **No candidate data, no chosen options.** Plus the signatures for strict reuse (§12.6).

Pure (no Telegram / DB): runs in pool workers. Depends on: ``parsers.models``, ``exam.keys``.
Used by: ``exam.bundle`` (build / read), ``exam.registry`` (reuse check), PDF / quiz builders.

Signatures (schema 2)
---------------------
- Normalised text: NFC, lower-case, punctuation/symbols dropped, whitespace collapsed; an image counts as its SHA-256.
- ``qkey`` — language-independent question id: TCS Question ID, or the EDQT question-image GUID shared by the
  ``_EN`` and ``_HI`` images. ``qref`` = ``qkey`` or ``h:<q_hash>``.
- ``lang_hash[lang]`` = question + **sorted** options of one language (option / question order may differ between
  candidates — the hash does not change); ``option_hashes[lang]`` per displayed option; ``answer id`` = portal option
  id (TCS) or position (EDQT) — compares answer keys across languages.
- ``fingerprint`` = SHA-1 of the sorted ``qref``s. ``q_hash`` (all languages) is kept for reference.
- ``decide(new, stored)`` → **reuse** (same questions + answers, languages already stored) · **merge** (same
  questions + answers, another language — owner 2026-10-12) · **variant** (anything changed).

Public API
----------
- ``build(data, ocr_mode="lazy") -> dict`` — the exam JSON (images listed per question; files live in the bundle).
- ``signature(data) -> dict`` — what ``exams`` stores to decide **without downloading the JSON**:
  ``{exam, sections, languages, qs: [{k, lh, ah, a}], fingerprint, questions, sig_version}``.
- ``responses(data) -> {qref: {lang, oh, idx, oid} | None}`` — this candidate's choices (memory / the task only).
- ``decide(new_sig, stored) -> (action, failed, added_languages)`` · ``compare(...)`` → failed checks only.
- ``chosen_in(doc, responses) -> {qref: index}`` — the candidate's choices as option indices of a stored JSON.

Example (abridged)::

    {"schema_version": 1,
     "meta": {"exam_key": "ssc-selection-post-phase-14-graduate-level|2026-09-17|1000", "exam_name": "…",
              "level": "Graduate Level", "date": "2026-09-17", "shift": 1, "time": "10:00 AM", "start_hhmm": "1000",
              "source": "edqt", "exam_code": "…", "languages": ["en"], "fingerprint": "…", "version": 1,
              "ocr_mode": "lazy", "created_at": "2026-10-12T12:00:00Z", "questions": 100, "images": 463},
     "marking": {"plus": 1, "neg": 0.3333, "source": "file"},          # only when the sheet prints it
     "sections": [{"name": "PART-A (General Intelligence)", "questions": [
        {"number": 1, "qid": null, "q_hash": "…", "lang": {"en": {"question": "<img id=\\"img_…\\"/>", "options": […]}},
         "option_hashes": […], "correct": 2, "option_ids": null, "explanation": null, "confidence": 1.0, "issues": [],
         "images": [{"id": "img_…", "role": "question", "lang": "en", "file": "img/img_….jpg", "mime": "image/jpeg",
                     "kind": "strip", "w": 0, "h": 0, "content_bbox": null, "ocr": {"text": null, "confidence": null}}]}]}]}

``w``/``h``/``content_bbox`` are filled by ``exam.bundle`` (Pillow) when the bundle is written.
"""

from __future__ import annotations

import hashlib
import re
import time
import unicodedata

from bot.exam.keys import exam_key
from bot.parsers.models import ExamData, Question, image_ids, plain  # noqa: F401 (image_ids re-exported)

SCHEMA_VERSION = 2  # 2: per-language hashes, qkey / qref, language merge
_IMG_TAG = re.compile(r'<img id="([^"]+)"\s*/>')
_WS = re.compile(r"\s+")
_EXT = {"image/jpeg": "jpg", "image/png": "png", "image/gif": "gif", "image/webp": "webp", "image/svg+xml": "svg",
        "image/bmp": "bmp"}


# ------------------------------------------------------------------ normalisation + hashes
def _norm(markup: str, assets: dict) -> str:
    """Comparable text: images → their SHA-256, NFC, lower-case, no punctuation/symbols, collapsed spaces."""
    def img(m):
        a = assets.get(m.group(1))
        return f" #img{a.sha256 if a else m.group(1)} "
    s = plain(_IMG_TAG.sub(img, markup or ""))
    s = unicodedata.normalize("NFC", s).lower()
    s = "".join(ch if not unicodedata.category(ch).startswith(("P", "S")) or ch == "#" else " " for ch in s)
    return _WS.sub(" ", s).strip()


def _sha1(s: str) -> str:
    return hashlib.sha1(s.encode("utf-8")).hexdigest()


def q_hash(q: Question, assets: dict) -> str:
    parts = []
    for lang in sorted(q.lang):
        c = q.lang[lang]
        parts.append(lang + "\x1d" + _norm(c.question, assets) + "\x1e" + "\x1f".join(sorted(_norm(o, assets) for o in c.options)))
    return _sha1("\x1c".join(parts))


def option_hashes(q: Question, assets: dict) -> list[str]:
    """One hash per displayed option over every language (same option, other order → same hash)."""
    n = q.n_options
    return [_sha1("\x1c".join(lang + "\x1d" + _norm(q.lang[lang].options[i], assets) if i < len(q.lang[lang].options) else ""
                              for lang in sorted(q.lang))) for i in range(n)]


def answer_sig(q: Question, assets: dict) -> str:
    if q.correct is None:
        return "none"
    return option_hashes(q, assets)[q.correct] if q.correct < q.n_options else "none"


_GUID = re.compile(r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})_(?:EN|HI)\.", re.I)


def qkey(q: Question, assets: dict) -> str | None:
    """Language-independent question identity: TCS Question ID, or the EDQT question-image id that the English and
    Hindi images share (``0197b012-…-06921a8907c7_EN.jpg`` / ``…_HI.jpg``). ``None`` → only content hashes."""
    if q.qid:
        return f"id:{q.qid}"
    for lang in sorted(q.lang):
        for aid in image_ids(q.lang[lang].question):
            a = assets.get(aid)
            m = _GUID.search(a.url or "") if a is not None else None
            if m:
                return f"g:{m.group(1).lower()}"
    return None


def qref(q: Question, assets: dict) -> str:
    return qkey(q, assets) or f"h:{q_hash(q, assets)}"


def lang_hash(q: Question, lang: str, assets: dict) -> str:
    c = q.lang[lang]
    return _sha1(_norm(c.question, assets) + "\x1e" + "\x1f".join(sorted(_norm(o, assets) for o in c.options)))


def lang_option_hashes(q: Question, lang: str, assets: dict) -> list[str]:
    return [_sha1(_norm(o, assets)) for o in q.lang[lang].options]


def answer_id(q: Question) -> str | None:
    """Correct answer independent of language: the portal option id (TCS), else its position (EDQT keeps the same
    option order in both languages); ``none`` = dropped by the board."""
    if q.correct is None:
        return "none"
    if q.option_ids and q.correct < len(q.option_ids):
        return f"id:{q.option_ids[q.correct]}"
    return f"ix:{q.correct}"


def _q_sig(q: Question, assets: dict) -> dict:
    lh, ah = {}, {}
    for lang in sorted(q.lang):
        lh[lang] = lang_hash(q, lang, assets)
        ohs = lang_option_hashes(q, lang, assets)
        ah[lang] = ohs[q.correct] if q.correct is not None and q.correct < len(ohs) else "none"
    keyed = qkey(q, assets) is not None
    return {"k": qref(q, assets), "lh": lh, "ah": ah, "a": answer_id(q) if keyed else None}


# ------------------------------------------------------------------ PDF languages
MAIN_SHARE = 0.6


_LANG_SUBJECT = re.compile(r"(?i)\b(english|hindi)\b[^)]*\b(language|comprehension)\b|\bgeneral\s+(english|hindi)\b")


def main_languages(per_question: list, sections: list | None = None) -> list[str]:
    """The languages a whole PDF can be made in (owner 2026-10-13: a Hindi-medium sheet gave a "mixed" English PDF).
    A language counts when at least 60 % of the **language-bearing** questions have it: a Hindi-medium sheet (75
    Hindi + a 25-question English-language part) → ``["hi"]``; a sheet with both versions of every question →
    ``["en", "hi"]``.

    Not language-bearing (owner 2026-10-13: the Steno sheet "both language hai, lekin only english bol rha hai"):
    a language-subject section (English / Hindi Language, Comprehension — printed in its own language in every PDF)
    and, in a section where other questions have both languages, a question with one only (an image-only figure,
    the same in both). ``sections``: ``[(name, count)]`` in question order; without it the whole sheet is one
    section. The English part of a Hindi PDF is still printed in English (no other version)."""
    total = len(per_question)
    if not total:
        return []
    groups, i = [], 0
    for name, count in (sections or [("", total)]):
        groups.append((name, per_question[i:i + count]))
        i += count
    if i < total:
        groups.append(("", per_question[i:]))
    bearing = []
    for name, qs in groups:
        if _LANG_SUBJECT.search(name or ""):
            continue
        bilingual = any(len(langs) > 1 for langs in qs)
        bearing += [langs for langs in qs if len(langs) > 1 or not bilingual]
    pool = bearing or per_question
    counts: dict[str, int] = {}
    for langs in pool:
        for lang in langs:
            counts[lang] = counts.get(lang, 0) + 1
    main = sorted(lang for lang, n in counts.items() if n >= MAIN_SHARE * len(pool))
    return main or [max(counts, key=counts.get)]


# ------------------------------------------------------------------ signature / responses / compare
def signature(data: ExamData) -> dict:
    """What ``exams`` stores to decide reuse / merge / variant without downloading the JSON (schema 2)."""
    m, assets = data.meta, data.assets
    qs = [_q_sig(q, assets) for q in data.questions()]
    langs = sorted({lang for q in data.questions() for lang in q.lang})
    refs = sorted(x["k"] for x in qs)
    return {
        "exam": {"exam_key": exam_key(m), "exam_name": m.exam_name, "level": m.level, "date": m.date, "shift": m.shift,
                 "time": m.time, "start_hhmm": m.start_hhmm, "source": m.source},
        "sections": [{"name": s.name, "count": len(s.questions)} for s in data.sections],
        "languages": langs,
        "qs": qs,
        "fingerprint": _sha1("\n".join(refs)),
        "questions": len(qs),
        "sig_version": 2,
    }


def responses(data: ExamData) -> dict[str, dict | None]:
    """``{qref: {"lang", "oh", "idx", "oid"} | None}`` — this candidate's evaluated choices (memory only)."""
    out = {}
    for q in data.questions():
        ref = qref(q, data.assets)
        if not (q.evaluated and q.chosen is not None and q.chosen < q.n_options):
            out[ref] = None
            continue
        lang = sorted(q.lang)[0]
        out[ref] = {"lang": lang, "oh": lang_option_hashes(q, lang, data.assets)[q.chosen], "idx": q.chosen,
                    "oid": q.option_ids[q.chosen] if q.option_ids and q.chosen < len(q.option_ids) else None}
    return out


def decide(new: dict, stored: dict) -> tuple[str, list[str], list[str]]:
    """Strict reuse (§12.6) with language merge (owner 2026-10-12): → ``("reuse" | "merge" | "variant", failed checks,
    languages the new sheets add)``. Reuse: same exam and every question of every language already stored identical.
    Merge: the same questions (same keys) and answer key, only another language. Anything else: variant."""
    failed: list[str] = []
    a, b = new.get("exam") or {}, stored.get("exam") or {}
    # start time is part of the exam_key already (same base key ⇒ same start); the shift number is display-only
    if any((a.get(k) or None) != (b.get(k) or None) for k in ("exam_name", "level", "date", "time")):
        failed.append("exam details")
    if sorted((s["name"], s["count"]) for s in new.get("sections") or []) != \
            sorted((s["name"], s["count"]) for s in stored.get("sections") or []):
        failed.append("sections")
    old = {x["k"]: x for x in stored.get("qs") or []}
    if set(old) != {x["k"] for x in new.get("qs") or []}:
        failed.append("questions")
        return "variant", failed, []
    added: set[str] = set()
    content = answers = 0
    for x in new["qs"]:
        y = old[x["k"]]
        if x.get("a") and y.get("a") and x["a"] != y["a"]:
            answers += 1
            continue
        for lang, h in x["lh"].items():
            if lang not in y["lh"]:
                added.add(lang)
            elif h != y["lh"][lang]:
                content += 1
            elif x["ah"].get(lang) != y["ah"].get(lang):
                answers += 1
    if content:
        failed.append(f"questions ({content} changed)")
    if answers:
        failed.append(f"answer key ({answers} changed)")
    if failed:
        return "variant", failed, []
    return ("merge" if added else "reuse"), [], sorted(added)


def compare(new: dict, stored: dict) -> list[str]:
    """Failed checks of ``decide`` (a language the stored exam lacks counts as ``languages`` here)."""
    action, failed, added = decide(new, stored)
    return failed + (["languages"] if action == "merge" else [])


def chosen_in(doc: dict, resp: dict[str, dict | None]) -> dict[str, int]:
    """The candidate's choices as option indices of ``doc``: by portal option id, else by the option's content in that
    language, else by position (keyed questions only). Keyed by the doc question's ``qref``."""
    out = {}
    for sec in doc.get("sections") or []:
        for q in sec["questions"]:
            r = resp.get(q["qref"])
            if not r:
                continue
            ohs = (q.get("option_hashes") or {}).get(r["lang"], [])
            if r.get("oid") and q.get("option_ids") and r["oid"] in q["option_ids"]:
                out[q["qref"]] = q["option_ids"].index(r["oid"])
            elif r["oh"] in ohs:
                out[q["qref"]] = ohs.index(r["oh"])
            elif q.get("qkey"):
                out[q["qref"]] = r["idx"]
    return out


# ------------------------------------------------------------------ build
def image_file(asset) -> str:
    return f"img/{asset.id}.{_EXT.get(asset.mime, 'bin')}"


def _images(q: Question, assets: dict) -> list[dict]:
    out, seen = [], set()
    for lang, c in q.lang.items():
        parts = [("question", c.question)] + [(f"option_{i + 1}", o) for i, o in enumerate(c.options)]
        if c.explanation:
            parts.append(("explanation", c.explanation))
        for role, markup in parts:
            for aid in image_ids(markup):
                a = assets.get(aid)
                if a is None or (aid, role, lang) in seen:
                    continue
                seen.add((aid, role, lang))
                out.append({"id": aid, "role": role, "lang": a.lang or lang, "file": image_file(a), "mime": a.mime,
                            "kind": a.kind, "sha256": a.sha256, "w": 0, "h": 0, "content_bbox": None,
                            "ocr": {"text": None, "confidence": None}})
    return out


def _lang_doc(c) -> dict:
    """One language of a question in the exam JSON; ``explanation`` / ``correct`` only when the source has them
    (question JSON files) — sheets' bundles stay as they were."""
    d = {"question": c.question, "options": list(c.options)}
    if c.explanation:
        d["explanation"] = c.explanation
    if c.correct is not None:
        d["correct"] = c.correct
    return d


def correct_of(q: dict, lang: str | None = None):
    """The correct option index of a bundle question in ``lang`` (its own key when the language has one)."""
    c = q["lang"].get(lang) if lang else None
    if c is not None and c.get("correct") is not None:
        return c["correct"]
    return q.get("correct")


def build(data: ExamData, ocr_mode: str = "lazy") -> dict:
    m, assets = data.meta, data.assets
    sig = signature(data)
    sections = []
    for s in data.sections:
        qs = []
        for q in s.questions:
            qs.append({"number": q.number, "qid": q.qid, "qkey": qkey(q, assets), "qref": qref(q, assets),
                       "q_hash": q_hash(q, assets),
                       "lang": {k: _lang_doc(v) for k, v in q.lang.items()},
                       "lang_hash": {k: lang_hash(q, k, assets) for k in q.lang},
                       "option_hashes": {k: lang_option_hashes(q, k, assets) for k in q.lang},
                       "correct": q.correct, "option_ids": q.option_ids,
                       "qtype": q.qtype, "explanation": None, "confidence": 1.0, "issues": list(q.issues),
                       "images": _images(q, assets)})
        sections.append({"name": s.name, "questions": qs})
    doc = {
        "schema_version": SCHEMA_VERSION,
        "meta": {**sig["exam"], "exam_code": m.exam_code, "languages": sig["languages"], "fingerprint": sig["fingerprint"],
                 "version": 1, "ocr_mode": ocr_mode, "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                 "questions": sig["questions"], "images": len({i["id"] for sec in sections for q in sec["questions"] for i in q["images"]})},
        "sections": sections,
    }
    if data.marking:
        p = data.marking.plus
        doc["marking"] = {"plus": p, "neg": round(data.marking.minus / p, 4) if p else 0.0, "source": "file"}
    return doc
