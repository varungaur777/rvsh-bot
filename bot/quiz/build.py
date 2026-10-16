"""Quiz questions from an exam bundle (spec §16.3) — pure functions (pool jobs; no Telegram / DB).

Per question, for the quiz's language (``en`` / ``hi`` / ``both``):
- fits Telegram's poll limits and has no image → **native quiz poll** (``mode="poll"``, no card): question ≤
  ``POLL_Q`` characters, every option ≤ ``POLL_O``; ``both`` → ``English\\nHindi`` and ``English / Hindi``;
- an image, a formula picture or too long → **card + poll** with options ``A`` ``B`` ``C`` ``D`` (``mode="card"``;
  the card is drawn by ``render.quiz_card``);
- no answer (dropped by the board), no options, or more options than a poll takes → skipped (counted).

Superscripts / subscripts become Unicode where possible (``x²``, ``H₂O``), else ``^(…)`` / ``_(…)``.

Explanations (``explain=True``; question JSON files — owner 2026-10-13): plain text ≤ ``EXPL_MAX`` characters with at
most 2 line breaks → the poll's own explanation (``ex``, shown by Telegram after answering); longer or with an image
→ an **explanation card** (``xcard: True``) sent after the question is answered. A language's own answer key wins.

Public API
----------
- ``build_questions(bundle, lang, section=None, explain=False) -> dict`` — ``{"questions": [...], "skipped", "sections",
  "cards"}``. A question: ``{"ref", "no", "sec", "c", "n", "mode", "poll"?: {"q", "o"}, "text", "ex"?, "xcard"?}`` (``text`` = a short plain
  version for reviews); ``cards`` = what ``render.quiz_card.render_cards`` must draw ``[{qref, number, section}]``.
- ``poll_text(markup) -> str | None`` — plain poll text, ``None`` when the markup holds an image.
- ``fingerprint(exam_fp, lang, section) -> str`` — same exam + language + scope → the same quiz (dedup).

Depends on: ``exam.bundle`` (read), ``parsers.models.plain``. Used by: ``quiz.maker``.
"""

from __future__ import annotations

import hashlib
import html
import re

POLL_Q = 285  # Telegram allows 300; "Q12/100 · " is added when the poll is sent
POLL_O = 100
EXPL_MAX = 200  # Telegram: a quiz explanation is ≤ 200 characters, ≤ 2 line feeds
MAX_OPTIONS = 10
_IMG = re.compile(r'<img id="[^"]+"\s*/>')
_SUP = str.maketrans("0123456789+-=()nix", "⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻⁼⁽⁾ⁿⁱˣ")
_SUB = str.maketrans("0123456789+-=()aeox", "₀₁₂₃₄₅₆₇₈₉₊₋₌₍₎ₐₑₒₓ")
_SUP_OK = set("0123456789+-=()nix")
_SUB_OK = set("0123456789+-=()aeox")


def _script(m: re.Match) -> str:
    kind, body = m.group(1).lower(), html.unescape(re.sub(r"<[^>]+>", "", m.group(2)))
    ok, table, mark = (_SUP_OK, _SUP, "^") if kind == "sup" else (_SUB_OK, _SUB, "_")
    if body and set(body) <= ok:
        return html.escape(body.translate(table))
    return html.escape(f"{mark}({body})" if len(body) > 1 else f"{mark}{body}")


def poll_text(markup: str) -> str | None:
    """Mini-markup → plain text for a poll; ``None`` when it holds an image (needs a card)."""
    if _IMG.search(markup or ""):
        return None
    s = re.sub(r"<(sup|sub)>(.*?)</\1>", _script, markup or "", flags=re.I | re.S)
    s = s.replace("<br/>", "\n")
    s = html.unescape(re.sub(r"<[^>]+>", "", s))
    s = "\n".join(re.sub(r"[ \t]+", " ", line).strip() for line in s.split("\n"))
    return re.sub(r"\n{2,}", "\n", s).strip()


def short_text(markup: str, limit: int = 160) -> str:
    """A short readable version for reviews (images shown as 🖼)."""
    s = poll_text(_IMG.sub(" 🖼 ", markup or "")) or ""
    s = re.sub(r"\s+", " ", s).strip()
    return s if len(s) <= limit else s[:limit - 1].rstrip() + "…"


def fingerprint(exam_fp: str, lang: str, section: int | None) -> str:
    return hashlib.sha1(f"{exam_fp}|{lang}|{'all' if section is None else section}|q1".encode()).hexdigest()


def _langs_of(q: dict, lang: str) -> list[str]:
    if lang == "both":
        return [x for x in ("en", "hi") if x in q["lang"]] or [next(iter(q["lang"]))]
    return [lang] if lang in q["lang"] else [next(iter(q["lang"]))]


def _poll(q: dict, langs: list[str]) -> dict | None:
    """Poll texts or ``None`` (a card is needed)."""
    qs, opts = [], []
    for lang in langs:
        c = q["lang"][lang]
        t = poll_text(c["question"])
        if t is None:
            return None
        if t not in qs:
            qs.append(t)
        o = [poll_text(x) for x in c["options"]]
        if any(x is None for x in o):
            return None
        opts.append(o)
    question = "\n".join(qs)
    if not question or len(question) > POLL_Q or "\n\n" in question:
        return None
    n = max(len(o) for o in opts)
    merged = []
    for i in range(n):
        parts = []
        for o in opts:
            if i < len(o) and o[i] and o[i] not in parts:
                parts.append(o[i])
        text = " / ".join(p.replace("\n", " ") for p in parts)
        if not text or len(text) > POLL_O:
            return None
        merged.append(text)
    if len(set(merged)) != len(merged):  # two options that read the same → the poll would be ambiguous
        return None
    return {"q": question, "o": merged}


def explanation_of(q: dict, lang: str) -> str:
    c = q["lang"].get(lang) or next(iter(q["lang"].values()))
    return c.get("explanation") or ""


def build_questions(bundle_path: str, lang: str, section: int | None = None, explain: bool = False) -> dict:
    from bot.exam.bundle import read_doc
    from bot.exam.schema import correct_of

    doc = read_doc(bundle_path)
    out, cards, skipped = [], [], 0
    names = [s["name"] for s in doc["sections"]]
    for si, sec in enumerate(doc["sections"]):
        if section is not None and si != section:
            continue
        for q in sec["questions"]:
            if not q.get("lang"):
                skipped += 1
                continue
            langs = _langs_of(q, lang)
            first = q["lang"][langs[0]]
            n = len(first["options"])
            c = correct_of(q, langs[0]) if len(langs) == 1 else q.get("correct")
            if c is None or not (2 <= n <= MAX_OPTIONS) or not (0 <= c < n):
                skipped += 1
                continue
            poll = _poll(q, langs)
            item = {"ref": q["qref"], "no": q.get("number"), "sec": si, "c": c, "n": n,
                    "mode": "poll" if poll else "card", "text": short_text(first["question"])}
            if poll:
                item["poll"] = poll
            else:
                cards.append({"qref": q["qref"], "number": q.get("number"), "section": sec["name"]})
            ex = explanation_of(q, langs[0]) if explain else ""
            if ex:
                t = poll_text(ex)
                if t is not None and t and len(t) <= EXPL_MAX and t.count("\n") <= 2:
                    item["ex"] = t
                else:
                    item["xcard"] = True
            out.append(item)
    return {"questions": out, "skipped": skipped, "sections": names, "cards": cards,
            "meta": doc.get("meta") or {}, "languages": doc["meta"].get("languages") or []}
