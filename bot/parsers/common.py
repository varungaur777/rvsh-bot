"""Helpers shared by the TCS and EDQT parsers (spec §12.2).

Depends on: lxml, ``parsers.models``, ``parsers.mathml``, ``parsers.source``.
Used by: ``parsers/tcs.py``, ``parsers/edqt.py``, ``parsers/__init__.py``.

Public API
----------
- text: ``norm_space``, ``detect_lang``, ``clean_section_name``, ``clean_exam_name``
- dates/times: ``parse_date`` → ISO, ``parse_time_range`` → (display, start_hhmm, printed_shift), ``shift_from_start``
- ``parse_html(source)`` → lxml root
- ``RichText`` — converts an lxml node into mini-markup, registering images in ``ExamData.assets``
- ``strip_option_label(markup, index)`` — removes a leading ``A.``/``1.``/``(a)`` label for that index

Nothing here raises for bad content; problems are returned as issues/warnings.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from datetime import date, datetime
from html import escape

from lxml import html as lxml_html

from bot.parsers import mathml
from bot.parsers.models import Asset
from bot.parsers.source import ExamSource

_WS = re.compile(r"[ \t\r\n\f\v  -​  　]+")
_DEV = re.compile(r"[ऀ-ॿ]")
_LAT = re.compile(r"[A-Za-z]")
_MONTHS = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}


# ---------------------------------------------------------------- text
def norm_space(s: str | None) -> str:
    """Collapse all whitespace (incl. NBSP) to single spaces, NFC-normalise, strip."""
    return unicodedata.normalize("NFC", _WS.sub(" ", s or "")).strip()


def detect_lang(text: str) -> str | None:
    """``"hi"`` when Devanagari letters make up ≥ 30 % of letters, ``"en"`` when Latin, ``None`` when no letters."""
    dev, lat = len(_DEV.findall(text)), len(_LAT.findall(text))
    if dev + lat == 0:
        return None
    return "hi" if dev >= 0.3 * (dev + lat) else "en"


def clean_section_name(s: str) -> str:
    """``"Section :\xa0General  Science"`` → ``"General Science"``."""
    s = norm_space(s)
    return re.sub(r"^section\s*:?\s*", "", s, flags=re.I).strip(" :-")


_DATE_TAIL = re.compile(
    r"[\s\-–,]*(\d{1,2}(st|nd|rd|th)?[\s\-/.]+[A-Za-z]{3,9}\.?[\s\-/.,]+\d{4}|\d{1,2}[/\-.]\d{1,2}[/\-.]\d{2,4})\s*$"
)


def clean_exam_name(s: str | None) -> str | None:
    """Tidy an exam name and drop a trailing date (``"Agniveervayu Male 22-Sep-2026"`` → ``"Agniveervayu Male"``)."""
    s = norm_space(s)
    if not s:
        return None
    s = _DATE_TAIL.sub("", s).strip(" -–,:")
    return s or None


# ---------------------------------------------------------------- dates & times
def parse_date(s: str | None) -> str | None:
    """Parse ``19/03/2026``, ``18-09-2025``, ``22-Sep-2026``, ``25th Sep 2026``, ``2026-09-22`` → ISO date."""
    s = norm_space(s)
    if not s:
        return None
    m = re.search(r"(\d{4})-(\d{1,2})-(\d{1,2})", s)
    if m:
        y, mo, d = map(int, m.groups())
        return _iso(y, mo, d)
    m = re.search(r"(\d{1,2})[/\-.](\d{1,2})[/\-.](\d{2,4})", s)
    if m:
        d, mo, y = map(int, m.groups())
        return _iso(y + 2000 if y < 100 else y, mo, d)
    m = re.search(r"(\d{1,2})(?:st|nd|rd|th)?[\s\-/.]+([A-Za-z]{3,9})\.?[\s\-/.,]+(\d{4})", s)
    if m and m.group(2)[:3].lower() in _MONTHS:
        return _iso(int(m.group(3)), _MONTHS[m.group(2)[:3].lower()], int(m.group(1)))
    return None


def _iso(y: int, mo: int, d: int) -> str | None:
    try:
        return date(y, mo, d).isoformat()
    except ValueError:
        return None


_TIME = r"(\d{1,2})[:.](\d{2})\s*([AaPp]\.?\s?[Mm]\.?)?"


def parse_time_range(s: str | None) -> tuple[str | None, str | None, int | None]:
    """``"12:45 PM - 2:15 PM"`` → (display text, start ``"1245"`` in 24 h, printed shift number or None)."""
    s = norm_space(s)
    if not s:
        return None, None, None
    shift = None
    if m := re.search(r"shift\s*[-:]?\s*(\d+)", s, re.I):
        shift = int(m.group(1))
    times = re.findall(_TIME, s)
    if not times:
        return s or None, None, shift
    h, mi, mer = times[0]
    if not mer and len(times) > 1 and times[1][2]:
        end_mer = times[1][2]
        # "9:00 - 10:30 AM" → AM; "11:30 - 1:00 PM" → AM start (crosses noon)
        mer = end_mer if int(h) <= int(times[1][0]) or int(h) == 12 else ("AM" if end_mer.upper().startswith("P") else "PM")
    hh = int(h) % 12 + (12 if mer and mer.upper().startswith("P") else 0) if mer else int(h)
    display = re.sub(r"\s*\(?shift\s*[-:]?\s*\d+\)?", "", s, flags=re.I).strip(" ,-")
    return display or None, f"{hh:02d}{int(mi):02d}", shift


def shift_from_start(start_hhmm: str | None) -> int | None:
    """Display-only shift number from the start time (before 11:00 → 1, before 15:00 → 2, else 3)."""
    if not start_hhmm:
        return None
    hm = int(start_hhmm)
    return 1 if hm < 1100 else 2 if hm < 1500 else 3


# ---------------------------------------------------------------- html
def parse_html(source: ExamSource):
    """Parse the main HTML of a source with the right encoding."""
    parser = lxml_html.HTMLParser(encoding=source.encoding or "utf-8", remove_comments=True)
    return lxml_html.fromstring(source.html, parser=parser)


def strip_option_label(markup: str, index: int) -> str:
    """Remove a leading label for option ``index`` (``A.``, ``a)``, ``(A)``, ``1.``, ``(1)``) — only that exact label."""
    labels = {chr(65 + index), chr(97 + index), str(index + 1)}
    m = re.match(r"^\s*\(?([A-Za-z0-9])\)?[.)]?(?=\s|$|<)", markup)
    if m and m.group(1) in labels and (m.group(0).rstrip().endswith((".", ")"))):
        return markup[m.end():].lstrip()
    return markup


_INLINE = {"sup": "sup", "sub": "sub", "b": "b", "strong": "b", "i": "i", "em": "i", "u": "u"}
_BLOCK = {"p", "div", "li", "tr", "table", "tbody", "ul", "ol", "h1", "h2", "h3", "h4", "h5", "h6", "center"}
_SKIP = {"script", "style", "input", "button", "select", "noscript"}


class RichText:
    """Builds mini-markup from lxml nodes and registers images as ``Asset``s.

    ``issues`` collects problems (missing images…) for the current question; call ``take_issues()``
    after each question.
    """

    def __init__(self, source: ExamSource, assets: dict[str, Asset], *, skip_img=None, lang_of_src=None) -> None:
        self.source = source
        self.assets = assets
        self.skip_img = skip_img or (lambda img: False)
        self.lang_of_src = lang_of_src or (lambda src: None)
        self.issues: list[str] = []

    def take_issues(self) -> list[str]:
        out, self.issues = self.issues, []
        return out

    def build(self, node) -> str:
        """Mini-markup for the children (and text) of ``node``."""
        return tidy(self._walk(node, _invisible_text(node)))

    def _walk(self, el, hide_text: bool = False) -> str:
        """``hide_text``: drop text inside elements styled invisible (white anti-copy numbers) but keep images."""
        parts: list[str] = []
        if el.text and not hide_text:
            parts.append(escape(el.text, quote=False))
        for child in el:
            tag = child.tag.lower() if isinstance(child.tag, str) else ""
            if tag and tag not in _SKIP and not _not_rendered(child):
                child_hidden = hide_text or _invisible_text(child)
                if tag == "br":
                    parts.append("<br/>")
                elif tag == "img":
                    parts.append(self.image(child))
                elif tag in _INLINE:
                    inner = self._walk(child, child_hidden)
                    if inner.strip():
                        t = _INLINE[tag]
                        parts.append(f"<{t}>{inner}</{t}>")
                elif tag in ("td", "th"):
                    parts.append(self._walk(child, child_hidden) + " | ")
                elif tag in _BLOCK:
                    parts.append("<br/>" + self._walk(child, child_hidden) + "<br/>")
                else:
                    parts.append(self._walk(child, child_hidden))
            if child.tail and not hide_text:
                parts.append(escape(child.tail, quote=False))
        return "".join(parts)

    def image(self, img) -> str:
        """``<img id=…/>`` for an image node (formulas become text when possible); ``""`` when skipped."""
        if self.skip_img(img):
            return ""
        src = img.get("src") or img.get("data-src") or ""
        if "spacer" in src.lower():
            return ""
        cls = (img.get("class") or "").lower()
        if "wiris" in cls and img.get("data-mathml"):
            text = mathml.to_markup(mathml.decode_wiris(img.get("data-mathml")))
            if text:
                return text
        found = self.source.asset(src)
        if not found:
            self.issues.append("missing_image")
            return ""
        data, mime = found
        if len(data) < 64:  # tracking pixels / empty images
            return ""
        sha = hashlib.sha256(data).hexdigest()
        aid = f"img_{sha[:16]}"
        if aid not in self.assets:
            kind = "formula" if "wiris" in cls else "image"
            self.assets[aid] = Asset(id=aid, data=data, mime=mime, sha256=sha, url=src if not src.startswith("data:") else None,
                                     kind=kind, lang=self.lang_of_src(src))
        return f'<img id="{aid}"/>'


_WHITE = re.compile(r"(?:^|;)\s*color\s*:\s*(?:#fff(?:fff)?|white|rgb\(\s*255\s*,\s*255\s*,\s*255\s*\))\s*(?:;|$)", re.I)
_NOT_RENDERED = re.compile(r"display\s*:\s*none|visibility\s*:\s*hidden", re.I)


def _invisible_text(el) -> bool:
    """Text styled white or with zero font size — portals plant such numbers to spoil copied text."""
    style = el.get("style") or ""
    font_color = (el.get("color") or "").lower() if el.tag == "font" else ""
    return bool(_WHITE.search(style) or re.search(r"font-size\s*:\s*0(px|pt|em)?\s*(;|$)", style)) or font_color in ("#ffffff", "#fff", "white")


def _not_rendered(el) -> bool:
    """Elements the browser does not show at all (skipped with their images)."""
    return bool(_NOT_RENDERED.search(el.get("style") or ""))


def tidy(markup: str) -> str:
    """Normalise built markup: whitespace, NFC, ≤ 1 consecutive ``<br/>``, no leading/trailing breaks, no empty cells."""
    s = unicodedata.normalize("NFC", markup)
    s = _WS.sub(" ", s)
    s = re.sub(r"\s*\|\s*(<br/>|$)", r"\1", s)  # trailing cell separators
    s = re.sub(r"\s*<br/>\s*", "<br/>", s)
    s = re.sub(r"(<br/>)+", "<br/>", s)
    s = re.sub(r"<(sup|sub|b|i|u)>\s*</\1>", "", s)
    s = re.sub(r"^(<br/>|\s)+|(<br/>|\s)+$", "", s)
    return s.strip()


def to_date_from_url_token(url: str | None, year_hint: str | None) -> str | None:
    """EDQT URLs often end their exam folder with ``Sep17`` → ISO date using ``year_hint`` (YYYY-…)."""
    if not url:
        return None
    m = re.search(r"(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*(\d{1,2})(?=/|$)", url, re.I)
    if not m:
        return None
    year = int(year_hint[:4]) if year_hint else datetime.now().year
    return _iso(year, _MONTHS[m.group(1)[:3].lower()], int(m.group(2)))
