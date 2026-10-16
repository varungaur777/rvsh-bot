"""HTML quiz (spec §15.9): one page (``templates/quiz.html``), two modes.

| mode | where | data | scoring |
|---|---|---|---|
| ``offline`` | a self-contained ``.html`` file sent to the user | questions + answers + images (base64) + the Hindi font | in the browser; not ranked |
| ``webapp`` | served by the bot (``web.server``) inside Telegram | questions **without answers**; images from ``/img/`` | on the server; ranked |

Pure functions (pool / thread): bundle + quiz in, payload / file out.

Images keep their size relative to the text (``scale`` → CSS px at the page's 16 px text); a strip wider than the
screen scrolls sideways in its box; tap to zoom. Only the mini-markup tags survive (D-003).

Public API
----------
- ``payload(bundle, quiz, *, answers, img_url=None) -> dict`` — what the page runs on (``img_url``: ``None`` →
  data URIs, else a prefix: ``<prefix><img_id>``). Questions in **the quiz's order** with its numbers (a shuffled
  JSON quiz). With ``answers`` and a quiz made with explanations, each language carries ``e`` (shown in the review).
- ``explanations(bundle, quiz, img_url) -> {"expl": {ref: {lang: markup}}, "images"}`` — the Web App gets them only
  with the result (after submit), like the answers.
- ``image_files(bundle) -> {img_id: (file, mime)}`` — the Web App serves images by id.
- ``write_offline(bundle, quiz, out_path, branding) -> {"bytes", "questions", "ms"}``.
- ``page(mode, data=None) -> str`` — the template with its data (``None`` → the Web App loads it from the API).
- ``score(quiz, answers) -> dict`` — server-side scoring of a submitted attempt (Web App).
"""

from __future__ import annotations

import base64
import json
import re
import time
from functools import lru_cache
from pathlib import Path

from bot.render.fonts import FONT_DIR

TEMPLATE = Path(__file__).resolve().parents[2] / "templates" / "quiz.html"
PX_PER_PT_AT_16 = 16 / 9.5 * 1.0  # ``scale`` is pt / px for 9.5 pt text → CSS px for 16 px text
_TAGS = re.compile(r"<(/?)(\w+)[^>]*?/?>")
_KEEP = {"sup", "sub", "b", "i", "u", "br", "img"}
_IMG = re.compile(r'<img id="([^"]+)"\s*/>')


def _clean(markup: str) -> str:
    from bot.render.quiz_card import dedupe_images
    return _TAGS.sub(lambda m: m.group(0) if m.group(2).lower() in _KEEP else "", dedupe_images(markup or ""))


def image_files(bundle: str) -> dict[str, tuple[str, str]]:
    from bot.exam.bundle import read_doc
    doc = read_doc(bundle)
    return {im["id"]: (im["file"], im.get("mime") or "image/jpeg")
            for s in doc["sections"] for q in s["questions"] for im in q.get("images") or []}


def _images(bundle: str, used: dict, img_url: str | None) -> dict:
    import zipfile

    images = {}
    with zipfile.ZipFile(bundle) as z:
        for iid, im in used.items():
            if im.get("broken") == "all":
                images[iid] = {"src": "", "w": 0, "broken": True}
                continue
            w = round((im.get("w") or 0) * (im.get("scale") or 0.75) * PX_PER_PT_AT_16) or None
            if img_url is None:
                raw = z.read(im["file"])
                src = f"data:{im.get('mime') or 'image/jpeg'};base64,{base64.b64encode(raw).decode()}"
            else:
                src = f"{img_url}{iid}"
            images[iid] = {"src": src, "w": w, **({"broken": True} if im.get("broken") else {})}
    return images


def _ordered(doc: dict, quiz: dict):
    """(section index, bundle question, quiz item) in the quiz's order."""
    by_ref = {q["qref"]: (si, q) for si, sec in enumerate(doc["sections"]) for q in sec["questions"]}
    for item in quiz["questions"]:
        hit = by_ref.get(item["ref"])
        if hit is not None:
            yield hit[0], hit[1], item


def _langs(quiz: dict) -> list[str]:
    lang = quiz.get("lang") or "en"
    return ["en", "hi"] if lang == "both" else [lang]


def payload(bundle: str, quiz: dict, *, answers: bool, img_url: str | None = None) -> dict:
    """The quiz's questions (its scope and languages) in the page's format."""
    from bot.exam.bundle import read_doc

    doc = read_doc(bundle)
    langs = _langs(quiz)
    with_expl = answers and bool(quiz.get("explain"))
    sections, used = [], {}
    for si, q, item in _ordered(doc, quiz):
        content = {}
        for lg in langs:
            c = q["lang"].get(lg)
            if c is None:
                continue
            content[lg] = {"q": _clean(c["question"]), "o": [_clean(o) for o in c["options"]]}
            if with_expl and c.get("explanation"):
                content[lg]["e"] = _clean(c["explanation"])
        if not content:  # a question only in the other language (e.g. the English part of a Hindi paper)
            lg, c = next(iter(q["lang"].items()))
            content[lg] = {"q": _clean(c["question"]), "o": [_clean(o) for o in c["options"]]}
            if with_expl and c.get("explanation"):
                content[lg]["e"] = _clean(c["explanation"])
        row = {"ref": q["qref"], "no": item.get("no") or q.get("number"), "lang": content}
        if answers:
            row["a"] = int(item["c"])
        for im in q.get("images") or []:
            if any(f'"{im["id"]}"' in x["q"] + "".join(x["o"]) + x.get("e", "") for x in content.values()):
                used[im["id"]] = im
        if not sections or sections[-1]["_si"] != si:
            sections.append({"name": doc["sections"][si]["name"], "q": [], "_si": si})
        sections[-1]["q"].append(row)
    for s in sections:
        del s["_si"]
    images = _images(bundle, used, img_url)
    meta = quiz.get("meta") or {}
    return {"id": quiz["_id"], "title": quiz.get("title") or meta.get("exam_name") or "Quiz",
            "meta_line": quiz.get("meta_line") or "", "langs": [x for x in langs if any(x in q["lang"] for s in sections for q in s["q"])] or langs,
            "time_per_q": int(quiz.get("default_time") or 30), "sections": sections, "images": images,
            "marking": quiz.get("marking") or {"plus": 1, "neg": 0}, "answers": answers}


def explanations(bundle: str, quiz: dict, img_url: str | None = None) -> dict:
    """``{"expl": {ref: {lang: markup}}, "images": {...}}`` of a quiz made with explanations (else empty)."""
    from bot.exam.bundle import read_doc

    if not quiz.get("explain"):
        return {"expl": {}, "images": {}}
    doc = read_doc(bundle)
    expl, used = {}, {}
    for _si, q, _item in _ordered(doc, quiz):
        row = {}
        for lg in _langs(quiz):
            c = q["lang"].get(lg)
            if c is not None and c.get("explanation"):
                row[lg] = _clean(c["explanation"])
        if row:
            expl[q["qref"]] = row
            for im in q.get("images") or []:
                if any(f'"{im["id"]}"' in x for x in row.values()):
                    used[im["id"]] = im
    return {"expl": expl, "images": _images(bundle, used, img_url)}


@lru_cache(maxsize=1)
def _template() -> str:
    return TEMPLATE.read_text(encoding="utf-8")


@lru_cache(maxsize=1)
def _font_b64() -> str:
    return base64.b64encode((FONT_DIR / "NotoSansDevanagari-Regular.ttf").read_bytes()).decode()


def page(mode: str, data: dict | None = None, *, branding: str = "", brand_url: str = "", font_url: str = "") -> str:
    """The template filled in. ``font_url``: the Web App serves the font; offline embeds it."""
    blob = json.dumps(data, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/") if data else "null"
    font = font_url or f"data:font/ttf;base64,{_font_b64()}"
    tg = '<script src="https://telegram.org/js/telegram-web-app.js"></script>' if mode == "webapp" else ""
    return (_template().replace("<!--__TG__-->", tg).replace("__MODE__", mode).replace("__FONT__", font)
            .replace("__BRAND_URL__", json.dumps(brand_url)[1:-1]).replace("__BRAND__", json.dumps(branding)[1:-1])
            .replace("/*__DATA__*/null", blob))  # the data last: quiz text never meets a placeholder


def write_offline(bundle: str, quiz: dict, out_path: str, branding: str = "", brand_url: str = "") -> dict:
    t0 = time.perf_counter()
    data = payload(bundle, quiz, answers=True)
    html_text = page("offline", data, branding=branding, brand_url=brand_url)
    Path(out_path).write_text(html_text, encoding="utf-8")
    return {"bytes": Path(out_path).stat().st_size, "questions": sum(len(s["q"]) for s in data["sections"]),
            "ms": int((time.perf_counter() - t0) * 1000)}


def score(quiz: dict, answers: dict, times: dict | None = None) -> dict:
    """A Web App attempt scored on the server: ``answers`` {ref: option index}. → counts, per section, the key."""
    names = quiz.get("sections") or []
    per: dict[int, dict] = {}
    key, right, wrong = {}, 0, 0
    for q in quiz["questions"]:
        c = int(q["c"])
        key[q["ref"]] = c
        sec = per.setdefault(q.get("sec", 0), {"name": names[q.get("sec", 0)] if q.get("sec", 0) < len(names) else "",
                                                "total": 0, "right": 0, "wrong": 0, "skipped": 0})
        sec["total"] += 1
        a = answers.get(q["ref"])
        if a is None or not isinstance(a, int) or not 0 <= a < int(q["n"]):
            sec["skipped"] += 1
        elif a == c:
            sec["right"] += 1
            right += 1
        else:
            sec["wrong"] += 1
            wrong += 1
    total = len(quiz["questions"])
    t = sum(int(v) for v in (times or {}).values() if isinstance(v, (int, float)) and 0 <= v < 86_400_000) / 1000
    return {"right": right, "wrong": wrong, "skipped": total - right - wrong, "total": total, "time_s": round(t, 1),
            "sections": [per[k] for k in sorted(per)], "key": key}
