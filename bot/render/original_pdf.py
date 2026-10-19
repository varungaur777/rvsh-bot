"""Original PDF — the response sheet in its own format (spec §14.2). Pool job (``render`` kind): sheet files in,
one PDF out.

Steps
-----
1. Each file is parsed with lxml and **cleaned**:
   - **candidate details removed** — inside the info block (TCS ``div.main-info-pnl``, EDQT header table) a row stays
     only when its label is an exam detail (``_KEEP``: subject, exam name / level, test date / time / shift, post,
     stream…); everything else (name, roll, application / registration no., hall ticket, centre, community, photo,
     signature, unknown labels) goes, with every image of the block. Outside it, label rows that look personal
     (``_PERSONAL``) go too. IDs and full names taken from removed rows are scrubbed from any text / attribute left
     (``_scrub``), e.g. a roll number inside a ``<title>``;
   - portal watermarks (class / id ``*watermark*`` — they repeat the roll number) and scripts removed;
   - **everything else as the portal shows it** (owner 2026-10-13: "as it is rakho, bas candidate details htani
     hai, css mai edit kro bas"): the sheet's own CSS and colours (grey filler, EDQT green / red / yellow option
     cells via ``bgcolor`` — presentational hints on); print / challenge / language buttons, the EDQT menu bar
     and form controls only **hidden** by our CSS (``rvsh-hide`` → ``visibility: hidden``: the place stays);
   - **every hyperlink and button opens the owner's link** (``theme.header_url``) — a hidden button keeps its
     clickable area; a form button outside a link is wrapped in one. Our own CSS adds only fonts, page size,
     ``overflow: visible``, image max width and "don't split a question".
2. WeasyPrint lays out each file with the sheet's own CSS (page 9.1 × 12.87 in = A4 ratio, wide enough for the
   portals' 8.5 in layout). **Resources come only from the MHT archive** (and our bundled fonts): no network,
   no local files — a sheet cannot make the bot fetch anything.
3. Several files (EDQT parts) are joined in question order (``Hints.order`` → ``PART-A`` / ``Section 2`` in the
   section name → send order); the same section sent twice is kept once.
4. PyMuPDF stamps every page: clickable header link (top centre), footer link (bottom left), ``n / N`` (bottom
   right), the watermark image of ``render.watermark`` (one text in the middle, over the content; one image
   object for the whole PDF) — from ``render.theme.pdf_theme``. **No banner** (spec §14.2).
5. Saved through ``render.compress`` (§15.8; no image pass — sheet images are web images, ≤ 180 DPI on the page).

Public API: ``render_original_pdf(ctx, out_path) -> dict`` (``{"pages", "parts", "missing", "bytes", "ms"}``).
``ctx``: ``files`` (sheet paths), ``hints`` (``Hints`` fields), ``theme`` (``pdf_theme``), ``title``, ``section``
(optional: only that section of the paper, 0-based across the parts in order -- an EDQT part is one section, a TCS
sheet's other ``section-cntnr`` boxes are dropped; ``/batch`` "each section + full").
``groups`` instead of ``files`` (``/batch`` merged sections as in the sheet, owner 2026-10-19: "as it is style mai
bhi merge kroge na sections" → "har page ke upar chhoti line"): ``[{"files", "hints", "section", "label"}]`` — each
group (one shift's sheet) rendered like above, all joined in order, page numbers across the whole PDF, and every page
of a group gets its ``label`` (``Exam • 17 Sep 2026 • Shift 1``) as a small line at the top left. With ``groups``,
page 1 is an **index** (owner 2026-10-19: "merged vali sabhi pdf mai index page bhi bnao, usme shifts likh do konsi
konsi hai, aur kis page se hai, page ka link bhi"): every shift, its pages, a link to its first page; the PDF's
bookmarks list them too. ``oneliner`` (in ``ctx`` or a group; ``/batch`` ⚡ One-liners as in the sheet, owner
2026-10-19): each question keeps only its **right option** (``_one_liners``) — TCS: the ``rightAns`` row (✓, its own
number, the ``Ans`` label moved onto it); EDQT: the green / yellow cell's row, shown green with ``(b)`` written in it.
A question without exactly one right option (dropped by the board, numerical) stays as it is.
``render_original_slides(ctx, out_path) -> dict`` — ``/slides`` as in the sheet (owner 2026-10-19: "jodo slides mai";
colours kept: "Rang rehne do"): every question's box cut from the sheet (marked while laying it out: a bookmark at its
start and one at its end) and placed, scaled to fit, on its page of the slides frame PDF (``render_exam_pdf`` layout
``slides`` with ``frame_only``: cover, ``Q n / N`` badge, header / footer, answer key) — vector, nothing re-typed;
the watermark over it like every original PDF. ``ctx``: ``files``, ``hints``, ``theme``, ``frames`` (path),
``body`` ([x0, y0, x1, y1] of a question page's free area), ``first`` (page of Q1), ``questions``. A sheet whose
question count differs from the frames raises ``ValueError`` (the caller makes custom slides instead).
Also ``clean(root, family, link='') -> list[str]`` (removed values; tests).

Depends on: WeasyPrint, PyMuPDF, lxml, ``parsers`` (source loading + family detection), ``render.fonts``,
``render.compress``, ``render.watermark``. Used by: ``exam.pipeline`` (``run_original``, ``/batch`` as-it-is).
"""

from __future__ import annotations

import logging
import re
import time

from bot.exam.imgproc import cut_off, repair_cut_off
from bot.render.fonts import FILES, FONT_DIR

PAGE_W_IN, PAGE_H_IN = 9.1, 12.87
MARGIN_TB_IN, MARGIN_LR_IN = 0.42, 0.3
FONT_SCHEME = "rvshfont:"

# labels of exam details (normalised: lower case, no trailing ':' / '.'); every other label of the info block goes
_KEEP = re.compile(
    r"^(subject|exam(ination)?( name| level| date| time)?|test (name|date|time)( and shift)?|"
    r"(date|time)( of (the )?(exam|examination|test))?|exam (date|time)|shift|slot|session|batch|"
    r"post( name| applied( for)?)?|entry \(post name\)|stream( applied)?|level|paper( name)?|course|trade|"
    r"discipline|medium|language|assessment( name)?)$")
_PERSONAL = re.compile(
    r"\b(name|roll|regn|registration|application|hall ?ticket|admit|candidate|participant|father|mother|birth|dob|"
    r"gender|category|community|caste|photo|signature|sign|centre|center|venue|mobile|phone|e-?mail|aadha+r|"
    r"address|id)\b")
_BUTTON_TEXT = re.compile(r"challenge|print|save\s*/|log\s?out|hindi version|english version|click here|grievance", re.I)
_DROP_TAGS = ("script", "noscript", "iframe", "object", "embed", "video", "audio", "base")
_DROP_CLASSES = ("print-pnl", "grn-btn", "challenge-btn", "language-switch", "switch-btn")
_SCRUB_ATTRS = ("title", "alt", "value", "style", "content", "aria-label", "data-text")

_CSS = f"""
@font-face {{ font-family: "Noto Sans"; src: url({FONT_SCHEME}{FILES['latin']}); }}
@font-face {{ font-family: "Noto Sans"; src: url({FONT_SCHEME}{FILES['latin_b']}); font-weight: bold; }}
@font-face {{ font-family: "Noto Sans Devanagari"; src: url({FONT_SCHEME}{FILES['deva']}); }}
@font-face {{ font-family: "Noto Sans Devanagari"; src: url({FONT_SCHEME}{FILES['deva_b']}); font-weight: bold; }}
@page {{ size: {PAGE_W_IN}in {PAGE_H_IN}in; margin: {MARGIN_TB_IN}in {MARGIN_LR_IN}in; }}
html, body {{ background: #fff !important; overflow: visible !important; }}
.grp-cntnr, .grid-wrapper {{ background: none !important; }}  /* TCS dark grey filler (owner: "vo grey area") */
.rvsh-hide {{ visibility: hidden !important; }}
.rvsh-hide-row {{ display: none !important; }}
input.rvsh-hide {{ width: auto !important; }}  /* EDQT PART buttons: their own width widened the tables in WeasyPrint */
img {{ max-width: 7.5in !important; height: auto; }}  /* only images wider than the page shrink (in a table cell a % max-width is ignored) */
img, .rvsh-q tr {{ break-inside: avoid; }}
.rvsh-qrow {{ break-after: avoid; }}
.question-pnl table.menu-tbl {{ display: none !important; }}  /* TCS Question ID / Status box: never shown (owner 2026-10-19) */
.question-pnl table.questionRowTbl {{ width: 100% !important; float: none !important; }}  /* the question: full width */
.rvsh-ans {{ color: #fff !important; font-weight: bold !important; white-space: nowrap; padding: 0 4px; }}  /* EDQT one-liner: (b) */
"""


# ------------------------------------------------------------------ cleaning
def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").replace("\xa0", " ")).strip()


def _label(td) -> str:
    return _norm(td.text_content()).rstrip(" :.").lower()


def _drop(el) -> None:
    if el.getparent() is not None:
        el.drop_tree()


def _info_blocks(root, family: str) -> list:
    """The candidate/exam info block(s): TCS ``main-info-pnl``; EDQT the table holding the ``Roll No.`` label."""
    if family == "tcs":
        return root.xpath('//div[contains(@class,"main-info-pnl")]')
    blocks = []
    for td in root.xpath("//td"):
        if _label(td) in ("roll no", "candidate name", "centre name", "test date") and not td.xpath(".//table"):
            table = next(td.iterancestors("table"), None)
            if table is not None and table not in blocks:
                blocks.append(table)
    return blocks


def _question_start(root, family: str):
    """First element of the question area (rows before it form the header zone)."""
    if family == "tcs":
        hit = root.xpath('(//div[contains(@class,"section-lbl") or contains(@class,"question-pnl")])[1]')
        return hit[0] if hit else None
    for f in root.xpath('//font[contains(@class,"tdtext")]'):
        if re.match(r"\s*Q\.?\s*No", f.text_content() or "", re.I):
            return f
    return None


def _values(tds) -> list[str]:
    out = []
    for td in tds:
        v = _norm(td.text_content()).lstrip(":").strip()
        if v:
            out.append(v)
    return out


def clean(root, family: str, link: str = "") -> list[str]:
    """Clean a parsed sheet in place (see the module doc). Returns the values taken out of removed rows. ``link``:
    the URL every hyperlink / button opens (none → not clickable)."""
    removed: list[str] = []
    # 1) candidate rows of the info block(s) — allowlist; photos / signatures with them
    for block in _info_blocks(root, family):
        for tr in block.xpath(".//tr"):
            tds = tr.xpath("./td")
            if len(tds) >= 2 and not _KEEP.match(_label(tds[0])):
                removed += _values(tds[1:])
                _drop(tr)
        for img in block.xpath(".//img"):
            _drop(img)
    # 2) personal-looking label rows elsewhere in the header zone
    start = _question_start(root, family)
    header_rows = []
    for el in root.iter():
        if el is start:
            break
        if el.tag == "tr":
            header_rows.append(el)
    for tr in header_rows:
        tds = tr.xpath("./td")
        if len(tds) < 2 or tr.getparent() is None:
            continue
        lbl = _label(tds[0])
        if 0 < len(lbl) <= 40 and not tds[0].xpath(".//table") and not _KEEP.match(lbl) and _PERSONAL.search(lbl):
            removed += _values(tds[1:])
            _drop(tr)
    # 3) portal watermarks (they repeat the roll number) and things that never show: scripts, frames, media
    for el in root.xpath('//*[contains(translate(@class,"WATERMARK","watermark"),"watermark") or '
                         'contains(translate(@id,"WATERMARK","watermark"),"watermark")]'):
        _drop(el)
    for el in list(root.iter(*_DROP_TAGS)):
        _drop(el)
    # what a browser never shows: ASP.NET hidden fields (a long __VIEWSTATE value widened EDQT tables off the page
    # in WeasyPrint)
    for el in root.xpath("//input[translate(@type,'HIDEN','hiden')='hidden'] | //*[contains(@class,'aspNetHidden')]"):
        _drop(el)
    for sel in root.xpath("//select"):  # WeasyPrint draws no dropdown: the chosen value as plain text
        opt = (sel.xpath(".//option[@selected]") or sel.xpath(".//option") or [None])[0]
        span = sel.makeelement("span", {})
        span.text = _norm(opt.text_content()) if opt is not None else ""
        span.tail = sel.tail
        sel.getparent().replace(sel, span)
    for el in root.xpath("//link"):
        if "stylesheet" not in (el.get("rel") or "").lower():
            _drop(el)
    for meta in root.xpath("//meta[@http-equiv]"):
        _drop(meta)
    # 4) buttons hidden by CSS (``rvsh-hide``: their place stays, the layout is the portal's); every hyperlink and
    #    button opens the owner's link (owner 2026-10-13) — a button not inside a link is wrapped in one
    hide = [el for el in root.xpath("//*[@class]")
            if any(c in el.get("class").lower().split() for c in _DROP_CLASSES)]
    hide += root.xpath("//*[@id='lblmenu']") + list(root.iter("input", "button", "textarea"))
    hide += [a for a in root.iter("a")
             if _BUTTON_TEXT.search(a.text_content() or "") or "inline-block" in (a.get("style") or "")]
    for el in hide:
        el.set("class", ((el.get("class") or "") + " rvsh-hide").strip())
    for el in list(root.iter("input", "button", "textarea")):
        if (el.get("type") or "").lower() != "hidden" and next(el.iterancestors("a"), None) is None:
            wrap = el.makeelement("a", {})
            el.addprevious(wrap)
            wrap.append(el)  # moves the element (its tail goes with it, as before)
    for a in root.iter("a"):
        for attr in ("href", "onclick", "target", "name"):
            a.attrib.pop(attr, None)
        if link:
            a.set("href", link)
    # 5) a table row holding nothing but hidden buttons takes no room (EDQT: the "Challenge" row of every question)
    for tr in root.iter("tr"):
        if _only_hidden(tr):
            tr.set("class", ((tr.get("class") or "") + " rvsh-hide-row").strip())
    # 6) EDQT question tables (owner 2026-10-13: "pages bahut jyada ban rhe hai edqt mai"): a page may break between
    #    option rows (never inside one; the question row stays with its first option); the two empty lines (<br><br>)
    #    the portal leaves after every question / option image are dropped
    if family == "edqt" and start is not None:
        for f in root.xpath('//font[contains(@class,"tdtext")]'):
            table = next(f.iterancestors("table"), None)
            if table is None:
                continue
            table.set("class", ((table.get("class") or "") + " rvsh-q").strip())
            row = next(f.iterancestors("tr"), None)
            if row is not None:
                row.set("class", ((row.get("class") or "") + " rvsh-qrow").strip())
            for td in table.iter("td"):
                _drop_trailing_breaks(td)
    _scrub(root, removed)
    return removed


def _hidden(el) -> bool:
    return any("rvsh-hide" in (a.get("class") or "").split() for a in (el, *el.iterancestors()))


def _only_hidden(tr) -> bool:
    """True when every text / image of the row is inside a hidden button (and it has at least one)."""
    if not any("rvsh-hide" in (e.get("class") or "").split() for e in tr.iter() if isinstance(e.tag, str)):
        return False
    for e in tr.iter():
        if not isinstance(e.tag, str):
            continue
        if e.tag == "img" and not _hidden(e):
            return False
        if (e.text or "").strip() and not _hidden(e):
            return False
        if (e.tail or "").strip() and e is not tr and not _hidden(e.getparent()):
            return False
    return True


def _drop_trailing_breaks(el) -> None:
    """Remove ``<br>``s that end ``el`` (or its last child, recursively) with nothing after them."""
    while len(el):
        last = el[-1]
        if (last.tail or "").strip():
            return
        if isinstance(last.tag, str) and last.tag == "br":
            el.remove(last)
            continue
        if isinstance(last.tag, str) and last.tag in ("font", "span", "b", "strong", "p", "div"):
            _drop_trailing_breaks(last)
        return


def _scrub(root, values: list[str]) -> None:
    """Remove leftovers of removed values: IDs (≥ 4 digits) and full names (≥ 2 words) — short single words are
    not scrubbed (they could be ordinary words of a question)."""
    keys = set()
    for v in values:
        if len(v) >= 6 and sum(ch.isdigit() for ch in v) >= 4:
            keys.add(v)
        elif len(v) >= 6 and len(v.split()) >= 2 and not any(ch.isdigit() for ch in v):
            keys.add(v)
    if not keys:
        return
    pat = re.compile("|".join(re.escape(k) for k in sorted(keys, key=len, reverse=True)))
    for el in root.iter():
        if not isinstance(el.tag, str):
            continue
        if el.text and pat.search(el.text):
            el.text = pat.sub("", el.text)
        if el.tail and pat.search(el.tail):
            el.tail = pat.sub("", el.tail)
        for attr in _SCRUB_ATTRS:
            v = el.get(attr)
            if v and pat.search(v):
                el.set(attr, pat.sub("", v))


def _section_names(root, family: str) -> list[str]:
    if family == "edqt":
        name = _norm(" ".join(e.text_content() for e in root.xpath('//*[@id="lblsubject"]')))
        return [name] if name else []
    return [_norm(e.text_content()) for e in root.xpath('//div[contains(@class,"section-lbl")]')]


def _order_key(index: int, hint_order, names: list[str]) -> tuple:
    """Same order as ``parsers.parse_sources``: hint → part letter / number in the section name → send order."""
    name = names[0] if names else ""
    m = re.search(r"\bPART[\s\-_]*([A-Z])\b", name, re.I) or re.search(r"\bSection[\s\-_]*(\d+)\b", name, re.I)
    part = (ord(m.group(1).upper()) - 64 if m.group(1).isalpha() else int(m.group(1))) if m else 10_000
    return (hint_order if hint_order is not None else 10_000), part, index


# ------------------------------------------------------------------ rendering
# answer icons drawn when the sheet's own tick / cross image is missing or broken (browsers often save cross.png
# truncated — WeasyPrint then paints a black box)
_ICONS = {
    "tick": b'<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 16 16">'
            b'<path d="M2.5 8.5l3.5 3.5 7.5-8" fill="none" stroke="#2e9e44" stroke-width="2.6" '
            b'stroke-linecap="round" stroke-linejoin="round"/></svg>',
    "cross": b'<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 16 16">'
             b'<path d="M3.5 3.5l9 9M12.5 3.5l-9 9" fill="none" stroke="#d93025" stroke-width="2.6" '
             b'stroke-linecap="round"/></svg>',
}
_BLANK_PNG = bytes.fromhex("89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
                           "0000000d49444154789c6360606060000000050001a5f645400000000049454e44ae426082")  # 1×1 clear


def _icon_for(url: str) -> bytes | None:
    name = url.rsplit("/", 1)[-1].split("?", 1)[0].lower()
    if name.startswith(("tick", "right", "correct")):
        return _ICONS["tick"]
    if name.startswith(("cross", "wrong", "wrng")):
        return _ICONS["cross"]
    return None


_broken, _repair = cut_off, repair_cut_off  # (exam.imgproc; also used by the exam bundle)


def _fetcher_class():
    from weasyprint.urls import URLFetcher, URLFetcherResponse

    class ArchiveFetcher(URLFetcher):
        """Serves the sheet's own archive and our fonts; refuses everything else (no network, no files)."""

        def __init__(self, src):
            super().__init__(fail_on_errors=False)
            self.src, self.missing = src, 0

        def fetch(self, url, headers=None):
            if url.startswith(FONT_SCHEME):
                name = url[len(FONT_SCHEME):]
                if name in FILES.values():
                    return URLFetcherResponse(url, (FONT_DIR / name).read_bytes(), {"Content-Type": "font/ttf"})
            elif not url.lower().startswith("file:"):
                got = self.src.asset(url)
                if got is not None and not _broken(*got):
                    return URLFetcherResponse(url, got[0], {"Content-Type": got[1]})
                icon = _icon_for(url)
                if icon is not None:
                    return URLFetcherResponse(url, icon, {"Content-Type": "image/svg+xml"})
                if got is not None:  # cut-off image: its readable part on white — never a grey / black box
                    self.missing += 1
                    return URLFetcherResponse(url, _repair(got[0]) or _BLANK_PNG, {"Content-Type": "image/png"})
            self.missing += 1
            raise ValueError("not in the archive")

    return ArchiveFetcher


def _hex(c: str) -> tuple[float, float, float]:
    c = (c or "#000000").lstrip("#")
    if len(c) == 3:
        c = "".join(ch * 2 for ch in c)
    try:
        return tuple(int(c[i:i + 2], 16) / 255 for i in (0, 2, 4))
    except ValueError:
        return 0.0, 0.0, 0.0


_WM_NAME = "RvshWm"


def _overlay_stream(doc, png: bytes, w: float, h: float) -> tuple[int, int]:
    """(image xref, content-stream xref) of the watermark, made once per PDF and page size: the image is added on a
    scratch page (cheap: empty page) that is then deleted, and one small stream draws it over a whole page.
    ``Page.insert_image`` per page would re-scan every page's fonts — 90 s on a 39-page EDQT sheet."""
    page = doc.new_page(width=w, height=h)
    page.insert_image(page.rect, stream=png)
    img = page.get_images()[0][0]
    doc.delete_page(page.number)
    stream = doc.get_new_xref()
    doc.update_object(stream, "<<>>")
    doc.update_stream(stream, f"q {w:.2f} 0 0 {h:.2f} 0 0 cm /{_WM_NAME} Do Q".encode())
    return img, stream


def _add_overlay(doc, page, img: int, stream: int) -> None:
    """Point the page's ``/XObject`` resources at the watermark image and append the drawing stream to its
    contents (resolving indirect dictionaries by hand — ``xref_set_key`` paths cannot cross them)."""
    kind, val = doc.xref_get_key(page.xref, "Resources")
    if kind == "xref":
        res, pre = int(val.split()[0]), ""
    elif kind == "dict":
        res, pre = page.xref, "Resources/"
    else:
        doc.xref_set_key(page.xref, "Resources", f"<</XObject<</{_WM_NAME} {img} 0 R>>>>")
        res = None
    if res is not None:
        kind, val = doc.xref_get_key(res, pre + "XObject")
        if kind == "xref":
            doc.xref_set_key(int(val.split()[0]), _WM_NAME, f"{img} 0 R")
        else:
            doc.xref_set_key(res, f"{pre}XObject/{_WM_NAME}", f"{img} 0 R")
    parts = " ".join(f"{x} 0 R" for x in [*page.get_contents(), stream])
    doc.xref_set_key(page.xref, "Contents", f"[{parts}]")


def _stamp(doc, theme: dict, labels: list[str] | None = None) -> None:
    """Header / footer links, page numbers and the watermark on every page (``TextWriter``: ``insert_text`` re-scans
    each page's resources per call — 0.1 s a call on image-heavy EDQT pages, 100× slower). The watermark is the
    cached tiled image of ``render.watermark``, stored once in the PDF. ``labels``: one per page, printed small at the
    top left (merged sections: which shift the page is from)."""
    import pymupdf

    f_reg = pymupdf.Font(fontfile=str(FONT_DIR / FILES["latin"]))
    f_bold = pymupdf.Font(fontfile=str(FONT_DIR / FILES["latin_sb"]))
    from bot.render.watermark import watermark_png

    col, wm = theme.get("colors") or {}, theme.get("watermark") or {}
    wm_xref: dict[tuple[int, int], tuple[int, int] | None] = {}
    for w, h in {(p.rect.width, p.rect.height) for p in doc}:  # before the page loop: it adds / deletes a page
        png = watermark_png(wm, w, h)
        wm_xref[(round(w), round(h))] = _overlay_stream(doc, png, w, h) if png else None
    total = len(doc)
    top, side = MARGIN_TB_IN * 72, MARGIN_LR_IN * 72

    def text(page, x, y, s, size, font, color, align="l", url=""):
        tw = font.text_length(s, fontsize=size)
        x0 = x - tw if align == "r" else (x - tw / 2 if align == "c" else x)
        writer = pymupdf.TextWriter(page.rect, color=_hex(color))
        writer.append((x0, y), s, font=font, fontsize=size)
        writer.write_text(page)
        if url:
            page.insert_link({"kind": pymupdf.LINK_URI, "from": pymupdf.Rect(x0, y - size, x0 + tw, y + 2), "uri": url})

    for i, page in enumerate(doc, 1):
        if not page.is_wrapped:  # WeasyPrint leaves its flipped CTM open: stamps would come out mirrored
            page.wrap_contents()
        w, h = page.rect.width, page.rect.height
        if wm_xref.get((round(w), round(h))):
            _add_overlay(doc, page, *wm_xref[(round(w), round(h))])
        if theme.get("header_text"):
            text(page, w / 2, top / 2 + 4, theme["header_text"], 9, f_bold, col.get("header"), "c",
                 theme.get("header_url") or "")
        if theme.get("footer_text"):
            text(page, side, h - top / 2 + 4, theme["footer_text"], 8.5, f_reg, col.get("footer"), "l",
                 theme.get("footer_url") or "")
        text(page, w - side, h - top / 2 + 4, f"{i} / {total}", 9, f_bold, col.get("muted"), "r")
        if labels and i <= len(labels) and labels[i - 1]:  # left of the centred header link, never over it
            room = w / 2 - side - 14 - (f_bold.text_length(theme["header_text"], fontsize=9) / 2
                                        if theme.get("header_text") else 0)
            label, size = labels[i - 1], 8.5
            while size > 6.5 and f_bold.text_length(label, fontsize=size) > room:
                size -= 0.5
            head, sep, tail = label.partition(" • ")  # too long: the exam name gives way, date / shift stay whole
            while sep and f_bold.text_length(label, fontsize=size) > room:
                head = head[:-2].rstrip() if len(head) > 6 else ""
                label = f"{head}… • {tail}" if head else tail
                sep = sep if head else ""
            text(page, side, top / 2 + 4, label, size, f_bold, col.get("muted"), "l")


def _index(doc, spans: list[tuple[str, int, int]], theme: dict, title: str) -> int:
    """Index page(s) at the front of a merged PDF: per group (shift) its label, pages and a link to its first page;
    the PDF bookmarks too. ``spans``: (label, first page, last page), 0-based before the index. → index pages added."""
    import pymupdf

    col = theme.get("colors") or {}
    f_reg = pymupdf.Font(fontfile=str(FONT_DIR / FILES["latin"]))
    f_bold = pymupdf.Font(fontfile=str(FONT_DIR / FILES["latin_b"]))
    w, h = (doc[0].rect.width, doc[0].rect.height) if len(doc) else (PAGE_W_IN * 72, PAGE_H_IN * 72)
    side, top, row = MARGIN_LR_IN * 72 + 14, MARGIN_TB_IN * 72 + 26, 24.0
    per = max(1, int((h - top - 120 - MARGIN_TB_IN * 72 - 20) // row))
    n_idx = max(1, -(-len(spans) // per))
    accent, muted, text_c = _hex(col.get("header") or "#1a237e"), _hex(col.get("muted") or "#666666"), _hex(col.get("text") or "#111111")

    def put(page, x, y, s, size, font, color, align="l"):
        tw = font.text_length(s, fontsize=size)
        x0 = x - tw if align == "r" else (x - tw / 2 if align == "c" else x)
        wr = pymupdf.TextWriter(page.rect, color=color)
        wr.append((x0, y), s, font=font, fontsize=size)
        wr.write_text(page)
        return pymupdf.Rect(x0, y - size, x0 + tw, y + 3)

    def fit(s: str, size: float, font, room: float) -> str:
        while len(s) > 8 and font.text_length(s, fontsize=size) > room:
            s = s[:-2].rstrip() + "…"
        return s

    for k in range(n_idx):
        page = doc.new_page(k, width=w, height=h)
        y = top + 26
        if k == 0:
            put(page, w / 2, y, fit(title, 17, f_bold, w - 2 * side), 17, f_bold, text_c, "c")
            y += 26
            put(page, w / 2, y, f"Index  ·  {len(spans)} shift{'s' if len(spans) != 1 else ''}  ·  tap a shift to go there",
                10, f_reg, muted, "c")
            y += 30
        page.draw_rect(pymupdf.Rect(side - 6, y - 15, w - side + 6, y + 7), color=accent, fill=accent)
        for x, s, al in ((side, "#", "l"), (side + 30, "Shift", "l"), (w - side - 110, "Pages", "r"), (w - side, "Starts on", "r")):
            put(page, x, y, s, 10, f_bold, (1, 1, 1), al)
        y += row
        for j, (label, a, b) in enumerate(spans[k * per:(k + 1) * per], k * per + 1):
            first, last = a + n_idx, b + n_idx  # 0-based after the index pages
            if j % 2 == 0:
                page.draw_rect(pymupdf.Rect(side - 6, y - 16, w - side + 6, y + 8), color=None, fill=(0.97, 0.97, 0.98))
            put(page, side, y, str(j), 10, f_reg, text_c)
            r1 = put(page, side + 30, y, fit(label, 10, f_bold, w - 2 * side - 190), 10, f_bold, accent)
            put(page, w - side - 110, y, f"{first + 1}–{last + 1}" if last > first else f"{first + 1}", 10, f_reg, text_c, "r")
            r2 = put(page, w - side, y, f"page {first + 1}", 10, f_bold, accent, "r")
            for r in (r1, r2):
                page.insert_link({"kind": pymupdf.LINK_GOTO, "from": r, "page": first, "to": pymupdf.Point(0, 0)})
            y += row
    doc.set_toc([[1, label or f"Part {j}", a + n_idx + 1] for j, (label, a, _b) in enumerate(spans, 1)])
    return n_idx


def render_original_pdf(ctx: dict, out_path: str) -> dict:
    import pymupdf

    from bot.render.compress import save_compressed

    t0 = time.perf_counter()
    for name in ("weasyprint", "fontTools"):  # missing remote images / font notes: counted, not logged per item
        logging.getLogger(name).setLevel(logging.CRITICAL)
    theme = ctx.get("theme") or {}
    out = pymupdf.open()
    missing = n_parts = 0
    labels: list[str] = []
    try:
        spans = []
        for g in ctx.get("groups") or [ctx]:
            before = len(out)
            m, n = _render_into(out, {"oneliner": ctx.get("oneliner"), **g, "title": ctx.get("title")}, theme)
            missing, n_parts = missing + m, n_parts + n
            labels += [g.get("label") or ""] * (len(out) - before)
            spans.append((g.get("label") or "", before, len(out) - 1))
        if ctx.get("groups"):
            k = _index(out, spans, theme, ctx.get("title") or "")
            labels = [""] * k + labels
        _stamp(out, theme, labels if ctx.get("groups") else None)
        out.set_metadata({"title": ctx.get("title") or "", "author": theme.get("branding") or "",
                          "creator": theme.get("branding") or "", "producer": theme.get("branding") or ""})
        pages = len(out)
        size = save_compressed(out, out_path, rewrite_images=False)  # sheet images: web DPI
    finally:
        out.close()
    return {"pages": pages, "parts": n_parts, "missing": missing, "bytes": size,
            "ms": int((time.perf_counter() - t0) * 1000)}


_MARK_CSS = """
.rvsh-qmark { bookmark-level: 1; bookmark-label: "@q"; }
.rvsh-qend { bookmark-level: 1; bookmark-label: "@e"; display: block; height: 0; margin: 0; padding: 0; }
"""


def _mark_questions(root, family: str) -> int:
    """Slides: each question's container gets a start mark and an empty end element after it (bookmarks → positions)."""
    if family == "tcs":
        boxes = root.xpath('//div[contains(@class,"question-pnl")]')
    else:
        boxes = []
        for f in root.xpath('//font[contains(@class,"tdtext")]'):
            if re.match(r"\s*Q\.?\s*No", f.text_content() or "", re.I):
                table = next(f.iterancestors("table"), None)
                if table is not None and table not in boxes:
                    boxes.append(table)
    for box in boxes:
        box.set("class", ((box.get("class") or "") + " rvsh-qmark").strip())
        end = box.makeelement("div", {"class": "rvsh-qend"})
        end.tail, box.tail = box.tail, None
        box.addnext(end)
    return len(boxes)


def _one_liners(root, family: str) -> int:
    """``/batch`` ⚡ One-liners as in the sheet: every question keeps only its right option (module doc). Returns how
    many questions were trimmed."""
    n = 0
    if family == "tcs":
        for table in root.xpath('//table[contains(@class,"questionRowTbl")]'):
            cells = [td for td in table.xpath('.//td[contains(@class,"rightAns") or contains(@class,"wrngAns")]')
                     if next(td.iterancestors("table"), None) is table]
            right = [td for td in cells if "rightAns" in (td.get("class") or "").split()]
            if len(right) != 1 or len(cells) < 2:
                continue
            keep = right[0].getparent()
            for td in cells:
                row = td.getparent()
                if row is keep:
                    continue
                label = next((c for c in row.xpath("./td") if (c.text_content() or "").strip() == "Ans"), None)
                first = keep.xpath("./td")[0]
                if label is not None and first is not right[0] and not (first.text_content() or "").strip():
                    first.text, first.attrib["class"], first.attrib["align"] = "Ans", "bold", "center"
                row.getparent().remove(row)
            n += 1
        return n
    for f in root.xpath('//font[contains(@class,"tdtext")]'):
        if not re.match(r"\s*Q\.?\s*No", f.text_content() or "", re.I):
            continue
        table = next(f.iterancestors("table"), None)
        if table is None:
            continue
        rows = [r for r in table.xpath("./tr|./tbody/tr")]
        start = next((i for i, r in enumerate(rows) if f in r.iter()), None)
        if start is None:
            continue
        opts = []
        for r in rows[start + 1:]:
            tds = r.xpath("./td")
            if len(tds) == 2 and tds[0].get("width") == "2%" and tds[0].xpath('.//font[contains(@class,"bld")]'):
                opts.append(r)
        right = [k for k, r in enumerate(opts) if (r.xpath("./td")[0].get("bgcolor") or "").lower() in ("green", "yellow")]
        if len(right) != 1 or len(opts) < 2:
            continue
        for r in rows[rows.index(opts[-1]) + 1:]:  # the candidate's "Not Answered" line under the options
            if re.match(r"\s*(not\s+)?(answered|attempted|visited)|\s*marked", r.text_content() or "", re.I):
                r.getparent().remove(r)
        for k, r in enumerate(opts):
            if k != right[0]:
                r.getparent().remove(r)
        cell = opts[right[0]].xpath("./td")[0]
        cell.set("bgcolor", "green")  # yellow = right but not chosen: the candidate's answer is not shown here
        mark = cell.xpath('.//font[contains(@class,"bld")]')[0]
        mark.text, mark.attrib["class"] = f"({'abcdefghij'[right[0]]})", "bld rvsh-ans"
        for child in list(mark):
            mark.remove(child)
        n += 1
    return n


def _render_into(out, ctx: dict, theme: dict, marks: list | None = None) -> tuple[int, int]:
    """One sheet (``files``, ``hints``, ``section``) laid out and appended to ``out`` → (missing resources, parts).
    ``marks``: a list → question start / end bookmarks laid out and collected there as ``(label, page, y)``
    (``render_original_slides``)."""
    import lxml.html
    import pymupdf
    from weasyprint import HTML

    from bot.parsers import common, detect_family, source

    hints = source.Hints(**(ctx.get("hints") or {}))
    parts, seen = [], set()
    for i, path in enumerate(ctx["files"]):
        src = source.load_path(path, hints)
        family = detect_family(src) or "tcs"
        root = common.parse_html(src)
        names = _section_names(root, family)
        sig = re.sub(r"\W+", "", " ".join(names).lower())
        if sig and sig in seen:  # the same section sent twice
            continue
        seen.add(sig)
        parts.append((_order_key(i, src.hints.order, names), path, family, max(1, len(names))))
    parts.sort(key=lambda p: p[0])
    only = ctx.get("section")  # /batch "each section": the paper's section number (0-based, all parts in order)
    if only is not None:
        start, keep = 0, []
        for p in parts:
            if start <= only < start + p[3]:
                keep.append((p, only - start))
            start += p[3]
        if not keep:
            raise ValueError(f"no section {only} in this sheet")
        parts = [p for p, _ in keep]
        within = {p[1]: k for p, k in keep}

    link = theme.get("header_url") or theme.get("footer_url") or ""
    fetcher = _fetcher_class()
    missing = 0
    for _, path, family, _n in parts:  # one file at a time: only one layout tree in memory
        src = source.load_path(path, hints)
        root = common.parse_html(src)
        if only is not None and family != "edqt":  # a TCS sheet holds every section: keep the one asked for
            for k, box in enumerate(root.xpath('//div[contains(@class,"section-cntnr")]')):
                if k != within[path]:
                    _drop(box)
        if ctx.get("oneliner"):
            _one_liners(root, family)
        clean(root, family, link)
        if marks is not None:
            _mark_questions(root, family)
        head = root.find("head")
        if head is None:
            head = root.makeelement("head", {})
            root.insert(0, head)
        for t in head.findall("title"):
            head.remove(t)
        title = head.makeelement("title", {})
        title.text = ctx.get("title") or "Response sheet"
        head.insert(0, title)
        style = head.makeelement("style", {})
        style.text = _CSS + (_MARK_CSS if marks is not None else "")
        head.append(style)  # last: wins over the sheet's own rules
        html = lxml.html.tostring(root, encoding="unicode", method="html", doctype="<!DOCTYPE html>")
        del root
        f = fetcher(src)
        pdf = HTML(string=html, base_url=src.url, url_fetcher=f).write_pdf(presentational_hints=True)  # bgcolor / align / width: EDQT marks answers with bgcolor
        missing += f.missing
        with pymupdf.open(stream=pdf, filetype="pdf") as part:
            if marks is not None:  # insert_pdf drops the outline: the marks are read here, pages moved on
                base = len(out)
                for _lvl, label, pno, dest in part.get_toc(simple=False):
                    if label in ("@q", "@e"):
                        to = dest.get("to") if isinstance(dest, dict) else None
                        marks.append((label, base + pno - 1, float(to.y) if to is not None else 0.0))
            out.insert_pdf(part)
    return missing, len(parts)


SLIDE_GROW = 1.6  # a short question box may grow this much on its slide


def _question_boxes(doc, marks: list) -> list[list[tuple[int, object]]]:
    """Per question, its pieces ``[(page, Rect)]`` between its start and end mark (a box broken over a page has
    two), trimmed to what is drawn there."""
    import pymupdf

    side, tb = MARGIN_LR_IN * 72, MARGIN_TB_IN * 72
    boxes, start = [], None
    for title, pno, y in marks:
        if title == "@q":
            start = (pno, y)
            continue
        if start is None:
            continue
        pieces = []
        for p in range(start[0], pno + 1):
            page = doc[p]
            y0 = start[1] if p == start[0] else tb
            y1 = y if p == pno else page.rect.height - tb
            if y1 - y0 < 2:
                continue
            area = pymupdf.Rect(side - 4, y0, page.rect.width - side + 4, y1)
            drawn = pymupdf.Rect()
            for _kind, bbox in page.get_bboxlog():
                r = pymupdf.Rect(bbox)
                if (r.y0 <= area.y0 + 2 and r.y1 >= area.y1 - 2
                        and (r.y0 < area.y0 - 2 or r.y1 > area.y1 + 2)):  # a section's side line / filler around it
                    continue
                if r.intersects(area) and r.width < page.rect.width * 0.99:
                    drawn |= r & area
            if not drawn.is_empty:
                pieces.append((p, pymupdf.Rect(drawn.x0 - 1, drawn.y0 - 1, drawn.x1 + 1, drawn.y1 + 1) & area))
        if pieces:
            boxes.append(pieces)
        start = None
    return boxes


def render_original_slides(ctx: dict, out_path: str) -> dict:
    import pymupdf

    from bot.render.compress import save_compressed
    from bot.render.watermark import watermark_png

    t0 = time.perf_counter()
    for name in ("weasyprint", "fontTools"):
        logging.getLogger(name).setLevel(logging.CRITICAL)
    theme = ctx.get("theme") or {}
    sheet = pymupdf.open()
    frames = pymupdf.open(ctx["frames"])
    try:
        marks: list = []
        missing, _parts = _render_into(sheet, {"files": ctx["files"], "hints": ctx.get("hints"), "title": "",
                                               "section": ctx.get("section")}, theme, marks=marks)
        boxes = _question_boxes(sheet, marks)
        if len(boxes) != int(ctx["questions"]):
            raise ValueError(f"{len(boxes)} question boxes in the sheet, {ctx['questions']} in the exam")
        x0, y0, x1, y1 = ctx["body"]
        bw, bh = x1 - x0, y1 - y0
        first = int(ctx.get("first") or 1)
        overlay = {}
        for w, h in {(pg.rect.width, pg.rect.height) for pg in frames}:  # before the page loop: it adds / deletes a page
            png = watermark_png(theme.get("watermark") or {}, w, h)
            overlay[(round(w), round(h))] = _overlay_stream(frames, png, w, h) if png else None
        for k, pieces in enumerate(boxes):
            page = frames[first + k]
            w = max(r.width for _p, r in pieces)
            h = sum(r.height for _p, r in pieces) + 4 * (len(pieces) - 1)
            scale = min(bw / w, bh / h, SLIDE_GROW)
            y = y0
            for p, r in pieces:
                target = pymupdf.Rect(x0, y, x0 + r.width * scale, y + r.height * scale)
                page.show_pdf_page(target, sheet, p, clip=r)
                y = target.y1 + 4 * scale
            size = (round(page.rect.width), round(page.rect.height))
            if overlay.get(size):  # the watermark over the sheet's box, as on every original PDF
                _add_overlay(frames, page, *overlay[size])
        pages = len(frames)
        size_b = save_compressed(frames, out_path, rewrite_images=False)
    finally:
        sheet.close()
        frames.close()
    return {"pages": pages, "questions": len(boxes), "missing": missing, "bytes": size_b,
            "ms": int((time.perf_counter() - t0) * 1000)}
