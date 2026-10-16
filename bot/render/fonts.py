"""Bundled fonts for every renderer (spec §15.2) — Noto Sans + Noto Sans Devanagari (R/SB/B) + math/symbols (Noto Sans
Symbols and Symbols 2: ✔ ✗ ⚠ ➡ ❖ …) + Noto Sans Bengali (R/B; owner 2026-10-15: "kai jgh box hai text mai"), and
Poppins (R/SB/B) for the English text of the custom exam PDF (owner 2026-10-13; its Hindi stays Noto Sans Devanagari
— "hindi mai vo lgana jo kabhi distort na ho").

Depends on: files in ``templates/fonts``. Used by: ``render/cards.py`` (PyMuPDF), ``render/pdf_build.py``
(ReportLab, P4).

Public API
----------
- ``FONT_DIR`` · ``FILES`` (role → filename)
- ``htmlbox_css()`` — ``@font-face`` rules + the base family stack for PyMuPDF ``insert_htmlbox``
  (MuPDF falls back glyph-by-glyph through the stack, so Hindi, English and symbols mix correctly).
- ``archive()`` — ``pymupdf.Archive`` that resolves the font files (lazy import, cached per process).
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

FONT_DIR = Path(__file__).resolve().parents[2] / "templates" / "fonts"
FILES = {
    "latin": "NotoSans-Regular.ttf", "latin_sb": "NotoSans-SemiBold.ttf", "latin_b": "NotoSans-Bold.ttf",
    "deva": "NotoSansDevanagari-Regular.ttf", "deva_sb": "NotoSansDevanagari-SemiBold.ttf", "deva_b": "NotoSansDevanagari-Bold.ttf",
    "math": "NotoSansMath-Regular.ttf", "sym": "NotoSansSymbols-Regular.ttf", "sym2": "NotoSansSymbols2-Regular.ttf",
    "beng": "NotoSansBengali-Regular.ttf", "beng_b": "NotoSansBengali-Bold.ttf",
    "pdf_latin": "Poppins-Regular.ttf", "pdf_latin_sb": "Poppins-SemiBold.ttf", "pdf_latin_b": "Poppins-Bold.ttf",
}
FAMILY = "NLatin, NDeva, NMath, NSym, NSym2, NBeng, sans-serif"


@lru_cache(maxsize=1)
def htmlbox_css() -> str:
    faces = []
    for fam, (reg, sb, bold) in {"NLatin": ("latin", "latin_sb", "latin_b"), "NDeva": ("deva", "deva_sb", "deva_b")}.items():
        faces += [f"@font-face {{font-family: {fam}; src: url({FILES[reg]});}}",
                  f"@font-face {{font-family: {fam}; src: url({FILES[sb]}); font-weight: 600;}}",
                  f"@font-face {{font-family: {fam}; src: url({FILES[bold]}); font-weight: bold;}}"]
    faces += [f"@font-face {{font-family: NMath; src: url({FILES['math']});}}",
              f"@font-face {{font-family: NSym; src: url({FILES['sym']});}}",
              f"@font-face {{font-family: NSym2; src: url({FILES['sym2']});}}",
              f"@font-face {{font-family: NBeng; src: url({FILES['beng']});}}",
              f"@font-face {{font-family: NBeng; src: url({FILES['beng_b']}); font-weight: bold;}}"]
    return "\n".join(faces) + f"\n* {{font-family: {FAMILY};}}\n"


@lru_cache(maxsize=1)
def archive():
    import pymupdf
    return pymupdf.Archive(str(FONT_DIR))
