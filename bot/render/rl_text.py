"""ReportLab text helpers (spec §15.1–15.2): shaped Hindi + English + symbols in one paragraph, never a missing glyph.

- ``register()`` — Poppins (R / SemiBold / Bold), Noto Sans (R / SB / B), Noto Sans Devanagari (R / SB / B; all Hindi —
  never Poppins' own Devanagari, so no conjunct is ever distorted: owner 2026-10-13 "hindi mai vo lgana jo kabhi
  distort na ho"), Noto Sans Math, Noto Sans Symbols as **shapable** TTFonts (HarfBuzz via ``uharfbuzz``: correct
  conjuncts, matras, nukta) + font families so ``<b>`` / ``<i>`` inside a run keep working. Once per worker.
- ``set_font("poppins" | "noto")`` — the English font of the next paragraphs (``/ps`` → ``pdf.font``; owner
  2026-10-13: Poppins by default, Noto Sans kept as a choice). ``NAMES[weight]["latin"]`` follows it; Latin letters
  Poppins lacks (Greek, rare accents) still come from Noto Sans.
- ``to_para(markup, weight="r", images=None) -> str`` — mini-markup (``<sup> <sub> <b> <i> <u> <br/> <img id/>``) →
  ReportLab paragraph markup with **font-run splitting by cmap**: each character goes to the first font that has it
  (Devanagari letters → Noto Sans Devanagari, then the English font, Noto Sans, Devanagari font (danda …), Math, Symbols); marks /
  joiners / spaces stay in the current run. ``images``: ``{asset_id: (path, width_pt, height_pt)}`` → inline
  ``<img/>``; unknown images are dropped (callers place block images themselves).
- ``plain_has_text(markup)`` — any text besides images?
- ``glyph_safe(text)`` — characters no bundled font has (owner 2026-10-15: "kai jgh box hai text mai") → a look-alike
  that one has: Word's Wingdings / Symbol bullets (private-use U+F0B7 …) → • ▪ ❖ ➢, emoji ✅ ❌ 👉 🔹 → ✔ ✘ ☞ ◆;
  other emoji are dropped; anything else left (a rare script) still prints □. ``to_para`` uses it; the quiz cards too.

Depends on: reportlab ≥ 4.4 (``shapable``), uharfbuzz, ``render.fonts``. Used by: ``render/exam_pdf.py``.
"""

from __future__ import annotations

import html
import re
import unicodedata
from functools import lru_cache

from bot.render.fonts import FILES, FONT_DIR

# weight → font names per script
NAMES = {
    "r": {"latin": "NP", "noto": "NL", "deva": "ND", "math": "NM", "sym": "NY", "sym2": "NY2", "beng": "NB"},
    "sb": {"latin": "NP-SB", "noto": "NL-SB", "deva": "ND-SB", "math": "NM", "sym": "NY", "sym2": "NY2", "beng": "NB-B"},
    "b": {"latin": "NP-B", "noto": "NL-B", "deva": "ND-B", "math": "NM", "sym": "NY", "sym2": "NY2", "beng": "NB-B"},
}
FAMILIES = {"poppins": {"r": "NP", "sb": "NP-SB", "b": "NP-B"}, "noto": {"r": "NL", "sb": "NL-SB", "b": "NL-B"}}
_font = "poppins"
_TOKEN = re.compile(r"(<[^>]+>)")
_IMG = re.compile(r'<img id="([^"]+)"\s*/>')
_DEVA_RANGES = ((0x0900, 0x097F), (0xA8E0, 0xA8FF), (0x1CD0, 0x1CFF))
# look-alikes for characters no bundled font has (glyph_safe)
SUBST = {"\uf0b7": "•", "\uf0a7": "▪", "\uf076": "❖", "\uf0d8": "➢", "\uf0fc": "✔", "\uf0fb": "✘", "\uf0e0": "➔",
         "\uf0e8": "➔", "\uf06e": "■", "\uf06c": "●", "\uf0a8": "◆", "\uf0d7": "➢",
         "\u2705": "✔", "\u2611": "✔", "\u274c": "✘", "\u274e": "✘", "\u2b55": "○", "\u2b50": "★",
         "\U0001f449": "☞", "\U0001f448": "☜", "\U0001f539": "◆", "\U0001f538": "◆", "\U0001f537": "◆",
         "\U0001f536": "◆", "\U0001f535": "●", "\U0001f534": "●", "\U0001f7e2": "●", "\U0001f7e1": "●",
         "\U0001f31f": "★", "\U0001f4cc": "•", "\U0001f4cd": "•", "\U0001f53a": "▲", "\U0001f53b": "▼"}
_registered = False


def _patch_shaping() -> None:
    """ReportLab 5.0.1 bug (B-015): ``shapeFragWord`` shapes a whole *word* with the font of its first fragment, so a
    word mixing fonts (``कौन-सा`` with a Latin hyphen, ``कारक,``, ``√20``) gets glyph ids of the wrong font → wrong
    glyphs. Fix: shape each same-font part of the word on its own and join the parts."""
    from reportlab.pdfbase import pdfmetrics, ttfonts
    from reportlab.platypus import paragraph

    orig = getattr(ttfonts.shapeFragWord, "__wrapped__", ttfonts.shapeFragWord)
    if getattr(paragraph.shapeFragWord, "__rvsh__", False):
        return

    def shape_word(w, *a, **kw):
        if isinstance(w, ttfonts.ShapedFragWord):
            return w
        frags = w[1:]
        fonts = {f.fontName for f, _s in frags if not hasattr(f, "cbDefn")}
        if len(fonts) <= 1:
            return orig(w, *a, **kw)
        segments, cur, font = [], [], None
        for f, s in frags:
            fn = None if hasattr(f, "cbDefn") else f.fontName
            if fn is not None and font is not None and fn != font:
                segments.append(cur)
                cur = []
            cur.append((f, s))
            if fn is not None:
                font = fn
        segments.append(cur)
        out, total, changed = [], 0.0, False
        for seg in segments:
            width = sum(pdfmetrics.stringWidth(s, f.fontName, f.fontSize) for f, s in seg if not hasattr(f, "cbDefn"))
            if any(hasattr(f, "cbDefn") for f, _s in seg):  # images / specials: never shaped
                out.extend(seg)
                total += width
                continue
            r = orig([width, *seg], *a, **kw)
            changed = changed or r is not seg and isinstance(r, ttfonts.ShapedFragWord)
            total += r[0]
            out.extend(r[1:])
        if not changed:
            return w
        return ttfonts.makeShapedFragWord(w)([total, *out])

    shape_word.__wrapped__ = orig
    shape_word.__rvsh__ = True
    ttfonts.shapeFragWord = shape_word
    paragraph.shapeFragWord = shape_word


def register() -> None:
    global _registered
    if _registered:
        return
    _patch_shaping()
    from reportlab.lib.fonts import addMapping
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont

    files = {"NP": "pdf_latin", "NP-SB": "pdf_latin_sb", "NP-B": "pdf_latin_b", "NL": "latin", "NL-SB": "latin_sb",
             "NL-B": "latin_b", "ND": "deva", "ND-SB": "deva_sb", "ND-B": "deva_b", "NM": "math", "NY": "sym",
             "NY2": "sym2", "NB": "beng", "NB-B": "beng_b"}
    for name, role in files.items():
        pdfmetrics.registerFont(TTFont(name, str(FONT_DIR / FILES[role]), shapable=True))
    for fam, bold in (("NP", "NP-B"), ("NP-SB", "NP-B"), ("NP-B", "NP-B"), ("NL", "NL-B"), ("NL-SB", "NL-B"),
                      ("NL-B", "NL-B"), ("ND", "ND-B"), ("ND-SB", "ND-B"), ("ND-B", "ND-B"), ("NM", "NM"), ("NY", "NY"),
                      ("NY2", "NY2"), ("NB", "NB-B"), ("NB-B", "NB-B")):
        addMapping(fam, 0, 0, fam)
        addMapping(fam, 1, 0, bold)
        addMapping(fam, 0, 1, fam)  # no italic files: italic = upright (never a missing font)
        addMapping(fam, 1, 1, bold)
    _registered = True


def set_font(font: str) -> str:
    """The English font of the paragraphs made from now on in this process: ``poppins`` (default) or ``noto``."""
    global _font
    _font = font if font in FAMILIES else "poppins"
    for weight, name in FAMILIES[_font].items():
        NAMES[weight]["latin"] = name
    return _font


@lru_cache(maxsize=1)
def _cmaps() -> dict[str, frozenset[int]]:
    from reportlab.pdfbase.ttfonts import TTFontFile

    out = {}
    for script, role in (("poppins", "pdf_latin"), ("noto", "latin"), ("deva", "deva"), ("math", "math"), ("sym", "sym"),
                         ("sym2", "sym2"), ("beng", "beng")):
        out[script] = frozenset(TTFontFile(str(FONT_DIR / FILES[role])).charToGlyph)
    return out


def _is_deva_letter(cp: int) -> bool:
    return any(a <= cp <= b for a, b in _DEVA_RANGES)


def script_of(ch: str) -> str | None:
    """Font script for one character; ``None`` = stays in the current run (marks, joiners, spaces)."""
    cp = ord(ch)
    if ch.isspace() or cp in (0x200C, 0x200D) or unicodedata.category(ch).startswith("M"):
        return None
    c = _cmaps()
    if _is_deva_letter(cp) and cp in c["deva"]:
        return "deva"
    if _font == "poppins" and cp in c["poppins"]:
        return "latin"
    if 0x0980 <= cp <= 0x09FF and cp in c["beng"]:
        return "beng"
    for script in ("noto", "deva", "math", "sym", "sym2", "beng"):  # with Noto Sans chosen, "noto" is the English font
        if cp in c[script]:
            return script
    return "missing"


def _emoji(cp: int) -> bool:
    return 0x1F000 <= cp <= 0x1FAFF or 0x2600 <= cp <= 0x27BF or 0x2B00 <= cp <= 0x2BFF


def glyph_safe(text: str) -> str:
    """``text`` with every character no bundled font has replaced by a look-alike (``SUBST``; private-use → •),
    an emoji without one dropped; other characters kept (they print □)."""
    if text.isascii():
        return text
    out = []
    for ch in text:
        if ord(ch) < 0x80 or script_of(ch) != "missing":
            out.append(ch)
            continue
        sub = SUBST.get(ch)
        if sub and script_of(sub) != "missing":
            out.append(sub)
        elif 0xE000 <= ord(ch) <= 0xF8FF:
            out.append("•")
        elif not _emoji(ord(ch)):
            out.append(ch)
    return "".join(out)


def _split(text: str, weight: str) -> str:
    """Plain (unescaped) text → ``<font name=…>`` runs (escaped)."""
    names = NAMES[weight]
    out, run, cur = [], [], None
    text = glyph_safe(text)

    def flush():
        if run:
            out.append(f'<font name="{names[cur or "latin"]}">{html.escape("".join(run), quote=False)}</font>')
            run.clear()

    for ch in text:
        s = script_of(ch)
        if s == "missing":
            flush()
            out.append(f'<font name="{names["math"]}">□</font>')  # visible placeholder, never a crash (§15.2)
            continue
        if s is None or s == cur:
            run.append(ch)
            continue
        if cur is not None:
            flush()
        cur = s
        run.append(ch)
    flush()
    return "".join(out)


def to_para(markup: str, weight: str = "r", images: dict | None = None) -> str:
    """Text inside ``<b>`` gets the bold font names itself: ReportLab ignores ``<b>`` around a ``<font name=…>`` run
    (B-034: bold words of JSON files and sheets were printed regular)."""
    out, bold = [], 0
    for tok in _TOKEN.split(markup or ""):
        if not tok:
            continue
        if tok.startswith("<"):
            m = _IMG.fullmatch(tok)
            if m:
                img = (images or {}).get(m.group(1))
                if img:
                    path, w, h = img
                    out.append(f'<img src="{html.escape(path)}" width="{w:.1f}" height="{h:.1f}" valign="middle"/>')
                continue
            t = tok.lower()
            if t in ("<b>", "</b>"):
                bold = bold + 1 if t == "<b>" else max(0, bold - 1)
            if t in ("<sup>", "</sup>", "<sub>", "</sub>", "<b>", "</b>", "<i>", "</i>", "<u>", "</u>", "<br/>"):
                out.append(t)
            continue
        out.append(_split(html.unescape(tok), "b" if bold else weight))
    return "".join(out)


def plain_has_text(markup: str) -> bool:
    return bool(re.sub(r"\s+", "", html.unescape(_TOKEN.sub("", _IMG.sub("", markup or "")))))
