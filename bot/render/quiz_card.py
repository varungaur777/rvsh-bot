"""Quiz cards (PNG) for Telegram quizzes — spec §15.7. Pool job: exam bundle + questions in, PNG files out.

Look (owner's rule): white card like a Telegram quiz poll, **frosted** borders (light, translucent — macOS/iOS style),
HD (540 pt × 2 = 1080 px), auto-sized to the content. A glass header shows only what exists: exam name, date ·
shift · time, section, and the paper's question number (``Q57``; ``1/2`` · ``2/2`` on a split). Options sit in
rounded pills labelled ``A`` ``B`` ``C`` ``D`` — the poll under the card has the same letters.

Language: ``en`` / ``hi`` → that language; ``both`` → **one column**: English, then Hindi under it (question:
English paragraph, Hindi paragraph; each option: ``English / Hindi`` on one line when both are short plain text,
else English with Hindi below). Too tall (> ``max_h_px``) → **two cards**: bilingual → card 1 English, card 2 Hindi;
one language → card 1 the question, card 2 the options.

Images come from the bundle as processed by ``exam.imgproc`` (``scale`` = pt per pixel for 9.5 pt PDF text): on a card
they are drawn at the card's text size (``× FONT / 9.5``); a text strip wider than the card is re-flowed at its word
gaps (``render.exam_pdf._Images``), anything else is shrunk to fit. The same image twice in one option (EDQT puts
the English and Hindi copy of a number there — identical bytes) is drawn once.

**Explanation cards** (``kind: "x"`` — question JSON files, owner 2026-10-13: "explanation badi hai to uska bhi card
bna dena, jo ki question attempt hone k baad bhjna"): the same header (``Q5``), the right answer in a green pill,
then the explanation — **always one card** (owner 2026-10-13: "explanation card ko single hi rakho, use split mat
karo"): a long one gets smaller text (``X_SIZES``) to stay within Telegram's 2560 px photo side; longer still → the
card just grows (a page-high explanation ends with "the rest is in the PDF"). The explanation sits in a light
see-through box; underlined words are bold (owner 2026-10-15).

**Pictures in a line** (owner 2026-10-15: explanation tables and pictures were drawn over each other): MuPDF lays a
``vertical-align: middle`` picture taller than its line half a picture lower than the line it measures, so it ran
into the next line — a paragraph holding such a picture gets a line height of its tallest picture + room
(``_div``). **Tables** (``<tbl>`` of question JSON files, ``render.exam_pdf.segments``) are drawn as HTML tables.

Public API
----------
- ``render_cards(ctx, out_dir) -> {"cards": {qref: [png, …]}, "ms"}`` — ``ctx``: ``bundle`` (zip), ``workdir``,
  ``lang`` (en / hi / both), ``header`` {exam_name, meta_line, branding}, ``questions`` [{qref, number, section,
  kind?}] (the bundle questions to draw; ``kind`` ``"x"`` = its explanation card), ``max_h_px`` (1800), ``colors``.
- ``card_html(markup, imgs, max_w, k)`` — mini-markup → htmlbox HTML (tests).
- ``palette(colors)`` — the card colours: ``ctx["colors"]`` (``/ps`` → 🃏 Quiz cards, ``card.colors``: title, accent,
  header_fill, pill, answer, explain_fill; "" = default) over ``COL``.

Depends on: PyMuPDF, ``render.fonts``, ``render.exam_pdf._Images`` (image fit / reflow). Used by: ``quiz.maker``.
"""

from __future__ import annotations

import html
import os
import re
import time
from pathlib import Path

from bot.render import fonts

W = 540.0
M = 16.0
FONT = 16.0
PDF_FONT = 9.5  # the size ``scale`` was measured for (render.exam_pdf.FONT)
# explanation card text sizes (CSS class, image scale): the first that keeps the card ≤ X_MAX_PT tall is used
X_SIZES = (("x", 1.0), ("x2", 13.5 / 15.5), ("x3", 12.0 / 15.5))
X_MAX_PT = 1280  # 2560 px at 2× — Telegram keeps a photo's long side ≤ 2560 px, so taller cards would shrink
LABELS = "ABCDEFGHIJ"
_IMG = re.compile(r'<img id="([^"]+)"\s*/>')
_TAGS = re.compile(r"<(/?)(\w+)[^>]*?/?>")
_KEEP = {"sup", "sub", "b", "i", "u", "br", "img"}
COL = {"text": "#111111", "muted": "#5f6b7a", "accent": "#2b6cb0", "frost": "#c9d3e0", "glass": "#eef2f8",
       "pill": "#f6f8fb", "pill_line": "#d8dfe9", "brand": "#1a237e", "ok": "#2e7d32", "ok_fill": "#eef7ee",
       "xfill": "#e3f2fd", "table_line": "#b8c4d4", "table_head": "#eef2f8"}
_SIZE = {"q": FONT + 1, "o": FONT, "x": FONT - 0.5, "x2": 13.5, "x3": 12.0}  # px of each text class
_IMG_H = re.compile(r'<img [^>]*?height="([\d.]+)"')
XL, XR = M + 14, W - M - 14  # the explanation's text inside its box
# /ps → 🃏 Quiz cards (``card.colors``; owner 2026-10-15: "explanation card ka colour change ps settings mai dedena,
# bki chize bhi"): setting → palette key
CARD_COLORS = {"title": "brand", "accent": "accent", "header_fill": "glass", "pill": "pill", "answer": "ok",
               "explain_fill": "xfill"}


class _Style(str):
    """The cards' CSS, carrying its palette (``col``) to the drawing code."""

    def __new__(cls, text: str, col: dict) -> "_Style":
        obj = super().__new__(cls, text)
        obj.col = col
        return obj


def palette(colors: dict | None) -> dict:
    """``COL`` with the ``/ps`` card colours over it ("" = the default); the right-answer pill's fill is a pale tint
    of the answer colour."""
    col = dict(COL)
    for key, value in (colors or {}).items():
        if value and key in CARD_COLORS:
            col[CARD_COLORS[key]] = value
    if (colors or {}).get("answer"):
        h = col["ok"].lstrip("#")
        col["ok_fill"] = "#" + "".join(f"{round(int(h[i:i + 2], 16) * 0.1 + 255 * 0.9):02x}" for i in (0, 2, 4))
    return col


def _clean(markup: str) -> str:
    """Only the mini-markup tags survive (D-003); everything else is dropped (text kept)."""
    return _TAGS.sub(lambda m: m.group(0) if m.group(2).lower() in _KEEP else "", markup or "")


def dedupe_images(markup: str) -> str:
    """The same image twice in one markup → once (EDQT: identical English and Hindi copies of a figure/number)."""
    seen: set[str] = set()

    def one(m):
        if m.group(1) in seen:
            return ""
        seen.add(m.group(1))
        return m.group(0)
    s = _IMG.sub(one, markup or "")
    return re.sub(r"(<br/>\s*)+$", "", re.sub(r"^(\s*<br/>)+", "", s))


def plain_text(markup: str) -> str:
    """Visible text of a markup (no images) — decides one-line options."""
    s = _IMG.sub(" ", markup or "").replace("<br/>", " ")
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", "", s))).strip()


def card_html(markup: str, imgs, max_w: float, k: float) -> str:
    """Mini-markup → HTML for ``insert_htmlbox``: images at ``k`` × their PDF size, fitted to ``max_w`` pt; a
    ``<tbl>`` table → an HTML table (pictures fitted to their column)."""
    from bot.render.exam_pdf import segments
    from bot.render.rl_text import glyph_safe

    def img_at(width: float):
        def img(m):
            im = imgs.meta.get(m.group(1))
            if im is None:
                return ""
            if im.get("broken") == "all":
                return '<span class="muted">[image could not be read]</span>'
            path, w, h = imgs.fit(im, width / k)
            rel = os.path.relpath(path, imgs.dir).replace("\\", "/")
            tail = '<br/><span class="muted">[the rest of this image could not be read]</span>' if im.get("broken") else ""
            return f'<img src="{html.escape(rel)}" width="{w * k:.1f}" height="{h * k:.1f}"/>{tail}'
        return img

    s = glyph_safe(dedupe_images(markup))  # no □ for characters the fonts lack
    if "<tbl>" not in s:
        return _IMG.sub(img_at(max_w), _clean(s))
    out = []
    for kind, val in segments(s):
        if kind == "text":
            out.append(f"<div>{_IMG.sub(img_at(max_w), _clean(val))}</div>")
            continue
        cw = max_w / max(1, len(val[0])) - 12
        rows = "".join(('<tr class="h">' if i == 0 else "<tr>")
                       + "".join(f"<td>{_IMG.sub(img_at(cw), _clean(c)) or '&nbsp;'}</td>" for c in r) + "</tr>"
                       for i, r in enumerate(val))
        out.append(f'<table class="t">{rows}</table>')
    return "".join(out)


def _div(cls: str, body: str, extra: str = "") -> str:
    """``<div class=cls>`` of ``body``; a picture taller than the text line → the line height of the tallest picture
    + room (MuPDF would draw it into the next line)."""
    size = _SIZE.get(cls, FONT)
    tall = max((float(h) for h in _IMG_H.findall(body)), default=0.0)
    if tall and not plain_text(re.sub(r"<img [^>]*>", "", body)) and "<table" not in body:
        # pictures only: bottom-aligned, MuPDF measures that line right (middle drops it below the line)
        return f'{extra}<div class="{cls}">{body.replace("<img ", "<img style=\"vertical-align:bottom\" ")}</div>'
    style = f' style="line-height:{max(size * 1.45, tall + 0.45 * size):.1f}px"' if tall > size else ""
    return f'{extra}<div class="{cls}"{style}>{body}</div>'


class _Imgs:
    """``exam_pdf._Images`` + the bundle's image records by id."""

    def __init__(self, bundle: str, workdir: Path, doc: dict) -> None:
        from bot.render.exam_pdf import _Images
        self.inner = _Images(bundle, workdir)
        self.dir = Path(workdir)
        self.meta = {im["id"]: im for s in doc["sections"] for q in s["questions"] for im in q.get("images") or []}

    def fit(self, im: dict, max_w_pt: float):
        return self.inner.fit(im, max_w_pt)

    def close(self) -> None:
        self.inner.close()


def _css(col: dict = COL) -> "_Style":
    return _Style(fonts.htmlbox_css() + f"""
      body {{ color: {col['text']}; font-size: {FONT}px; }}
      img {{ vertical-align: middle; }}
      .title {{ font-size: 14.5px; font-weight: bold; color: {col['brand']}; }}
      .meta {{ font-size: 11px; font-weight: 600; color: {col['muted']}; margin-top: 2px; }}
      .qno {{ font-size: 13px; font-weight: bold; color: {col['accent']}; text-align: right; }}
      .q {{ font-size: {FONT + 1}px; font-weight: 600; line-height: 1.38; }}
      .o {{ font-size: {FONT}px; line-height: 1.35; }}
      .lbl {{ font-size: 15px; font-weight: bold; color: {col['accent']}; text-align: center; }}
      .muted {{ font-size: 11px; color: {col['muted']}; }}
      .lang {{ font-size: 10.5px; font-weight: bold; color: {col['muted']}; }}
      .brand {{ font-size: 11px; font-weight: bold; color: {col['brand']}; text-align: right; }}
      .x {{ font-size: {FONT - 0.5}px; line-height: 1.45; }}
      .x2 {{ font-size: 13.5px; line-height: 1.42; }}
      .x3 {{ font-size: 12px; line-height: 1.4; }}
      .xtag {{ font-size: 10.5px; font-weight: bold; color: {col['accent']}; }}
      .oklbl {{ font-size: 15px; font-weight: bold; color: {col['ok']}; text-align: center; }}
      table.t {{ border-collapse: collapse; margin: 3px 0 5px 0; }}
      table.t td {{ border: 0.8px solid {col['table_line']}; padding: 2px 6px; vertical-align: middle; }}
      table.t tr.h td {{ background-color: {col['table_head']}; }}
    """, col)


class _Card:
    """One PNG being drawn: a tall page; ``y`` grows; ``save`` clips to the content."""

    def __init__(self, archive, css: str) -> None:
        import pymupdf
        self.pm = pymupdf
        self.doc = pymupdf.open()
        self.page = self.doc.new_page(width=W, height=4000)
        self.archive, self.css = archive, css
        self.col = getattr(css, "col", COL)
        self.y = M

    def box(self, html_text: str, x0: float, x1: float, y: float | None = None) -> float:
        y = self.y if y is None else y
        spare, _s = self.page.insert_htmlbox(self.pm.Rect(x0, y, x1, 3990), html_text, css=self.css,
                                             archive=self.archive, scale_low=1)
        if spare < 0:
            raise ValueError("card content does not fit")
        return (3990 - y) - spare

    def rounded(self, rect, *, stroke: str | None, fill: str | None, width: float = 1.2, radius: float = 12,
                fill_opacity: float = 1.0, stroke_opacity: float = 1.0) -> None:
        r = self.pm.Rect(rect)
        rad = min(0.5, radius / max(1.0, min(r.width, r.height)))
        self.page.draw_rect(r, color=_rgb(stroke) if stroke else None, fill=_rgb(fill) if fill else None, width=width,
                            radius=rad, fill_opacity=fill_opacity, stroke_opacity=stroke_opacity, overlay=False)

    def save(self, path: str, scale: float = 2.0) -> tuple[int, int]:
        h = self.y + M
        self.rounded((3, 3, W - 3, h - 3), stroke=self.col["frost"], fill=None, width=2.2, radius=18, stroke_opacity=0.55)
        self.rounded((6.5, 6.5, W - 6.5, h - 6.5), stroke="#ffffff", fill=None, width=1.0, radius=15, stroke_opacity=0.9)
        pix = self.page.get_pixmap(matrix=self.pm.Matrix(scale, scale), clip=self.pm.Rect(0, 0, W, h), alpha=False)
        pix.save(path)
        self.doc.close()
        return pix.width, pix.height


def _rgb(hex_color: str):
    h = hex_color.lstrip("#")
    return tuple(int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))


def _header(card: _Card, head: dict, qno: str) -> None:
    parts = [f'<div class="title">{html.escape(head.get("exam_name") or "Quiz")}</div>']
    meta = " · ".join(x for x in (head.get("meta_line"), head.get("section")) if x)
    if meta:
        parts.append(f'<div class="meta">{html.escape(meta)}</div>')
    x0, x1 = M + 12, W - M - 74
    h = max(card.box("".join(parts), x0, x1, card.y + 9),
            card.box(f'<div class="qno">{html.escape(qno)}</div>', W - M - 72, W - M - 10, card.y + 9))
    card.rounded((M, card.y, W - M, card.y + h + 18), stroke=card.col["frost"], fill=card.col["glass"], width=1.4, radius=12,
                 fill_opacity=0.75)
    card.y += h + 18 + 12


def _question(card: _Card, body: str, tag: str = "") -> None:
    label = f'<div class="lang">{tag}</div>' if tag else ""
    card.y += card.box(_div("q", body, label), M + 6, W - M - 6) + 10


def _option(card: _Card, i: int, body: str) -> None:
    x_lbl, x_txt = M + 8, M + 44
    y0 = card.y
    th = card.box(_div("o", body), x_txt, W - M - 12, y0 + 9)
    lh = card.box(f'<div class="lbl">{LABELS[i]}</div>', x_lbl, x_lbl + 26, y0 + 9 + max(0.0, (th - 20) / 2))
    h = max(th, lh) + 18
    card.rounded((M, y0, W - M, y0 + h), stroke=card.col["pill_line"], fill=card.col["pill"], width=1.1, radius=11)
    card.rounded((x_lbl, y0 + h / 2 - 13, x_lbl + 26, y0 + h / 2 + 13), stroke=card.col["accent"], fill="#ffffff",
                 width=1.2, radius=13, stroke_opacity=0.8)
    card.y = y0 + h + 7


def _answer(card: _Card, i: int, body: str) -> None:
    """The right answer of an explanation card: a green pill with its letter."""
    x_lbl, x_txt = M + 8, M + 44
    y0 = card.y
    th = card.box(_div("o", body or "&nbsp;"), x_txt, W - M - 12, y0 + 9)
    lh = card.box(f'<div class="oklbl">{LABELS[i]}</div>', x_lbl, x_lbl + 26, y0 + 9 + max(0.0, (th - 20) / 2))
    h = max(th, lh) + 18
    card.rounded((M, y0, W - M, y0 + h), stroke=card.col["ok"], fill=card.col["ok_fill"], width=1.3, radius=11, stroke_opacity=0.8,
                 fill_opacity=0.85)
    card.rounded((x_lbl, y0 + h / 2 - 13, x_lbl + 26, y0 + h / 2 + 13), stroke=card.col["ok"], fill="#ffffff",
                 width=1.4, radius=13)
    card.y = y0 + h + 10


def _explanation_cards(base, archive, css, head: dict, qno: str, q: dict, lang: str, imgs, max_h: int) -> list[str]:
    from bot.exam.schema import correct_of
    from bot.render.exam_pdf import split_br, underline_to_bold

    k = FONT / PDF_FONT
    c = _content(q, lang)
    right = correct_of(q, lang)
    ans = card_html(c["options"][right], imgs, W - 2 * M - 56, k) if right is not None and right < len(c["options"]) else None
    raw = [p for p in split_br(underline_to_bold(c.get("explanation") or "")) if p.strip()]
    if not raw:
        return []
    scratch = _Card(archive, css)  # heights: measured once per text size on a scratch page
    _header(scratch, head, qno)
    if ans is not None:
        _answer(scratch, right, ans)
    top = scratch.y + 10 + scratch.box('<div class="xtag">EXPLANATION</div>', XL, XR) + 4
    chosen = None
    for cls, f in X_SIZES:  # the largest text that keeps the card within Telegram's 2560 px photo side
        paras = [x for x in (card_html(p, imgs, XR - XL, k * f) for p in raw) if x.strip()]
        try:
            total = top + sum(scratch.box(_div(cls, p), XL, XR, M) + 5 for p in paras) + 39
        except ValueError:  # one paragraph alone is taller than the page
            total = float("inf")
        chosen = (cls, paras)
        if total <= X_MAX_PT:
            break
    scratch.doc.close()
    cls, paras = chosen
    card = _Card(archive, css)
    _header(card, head, qno)
    if ans is not None:
        _answer(card, right, ans)
    box_top = card.y
    card.y += 10 + card.box('<div class="xtag">EXPLANATION</div>', XL, XR, card.y + 10) + 4
    for p in paras:
        try:
            card.y += card.box(_div(cls, p), XL, XR) + 5
        except ValueError:  # the page is full (a very long explanation): the rest is in the PDF
            card.y += card.box('<div class="xtag">… the rest of the explanation is in the PDF</div>', XL, XR) + 4
            break
    card.y += 5
    # a light see-through box behind the explanation (owner 2026-10-15), drawn under the text
    card.rounded((M, box_top, W - M, card.y), stroke=card.col["frost"], fill=card.col["xfill"], width=1.0, radius=12,
                 fill_opacity=0.55, stroke_opacity=0.8)
    card.rounded((M + 1.5, box_top + 9, M + 5, card.y - 9), stroke=None, fill=card.col["accent"], radius=2, fill_opacity=0.7)
    card.y += 8
    _footer(card, head.get("branding") or "")
    path = f"{base}_x1.png"
    card.save(path)
    return [path]


def _footer(card: _Card, branding: str) -> None:
    if branding:
        card.y += card.box(f'<div class="brand">{html.escape(branding)}</div>', M, W - M - 6) + 2


def _content(q: dict, lang: str) -> dict:
    return q["lang"].get(lang) or next(iter(q["lang"].values()))


def _parts(q: dict, lang: str, imgs: _Imgs) -> dict:
    """HTML of the question and options for one card language: ``{"q": [(tag, html)], "o": [html]}``."""
    avail_q, avail_o = W - 2 * M - 12, W - 2 * M - 56
    k = FONT / PDF_FONT
    langs = [x for x in ("en", "hi") if x in q["lang"]] if lang == "both" else [lang if lang in q["lang"] else next(iter(q["lang"]))]
    if len(langs) < 2:
        c = _content(q, langs[0])
        return {"q": [("", card_html(c["question"], imgs, avail_q, k))],
                "o": [card_html(o, imgs, avail_o, k) for o in c["options"]]}
    en, hi = q["lang"]["en"], q["lang"]["hi"]
    qs = [("ENGLISH", card_html(en["question"], imgs, avail_q, k))]
    if dedupe_images(hi["question"]) != dedupe_images(en["question"]):
        qs.append(("हिन्दी", card_html(hi["question"], imgs, avail_q, k)))
    opts = []
    for i in range(max(len(en["options"]), len(hi["options"]))):
        a = en["options"][i] if i < len(en["options"]) else ""
        b = hi["options"][i] if i < len(hi["options"]) else ""
        ha, hb = card_html(a, imgs, avail_o, k), card_html(b, imgs, avail_o, k)
        if not b or dedupe_images(a) == dedupe_images(b):
            opts.append(ha or hb)
        elif "<img" not in a + b and len(plain_text(a)) + len(plain_text(b)) <= 46:
            opts.append(f"{ha} / {hb}")
        else:
            opts.append(f"{ha}<br/>{hb}")
    return {"q": qs, "o": opts}


def _draw(path: str, archive, css: str, head: dict, qno: str, qparts: list, opts: list, start: int = 0) -> tuple[int, int]:
    card = _Card(archive, css)
    _header(card, head, qno)
    for tag, body in qparts:
        _question(card, body, tag)
    if opts:
        card.y += 2
    for i, body in enumerate(opts):
        _option(card, start + i, body)
    _footer(card, head.get("branding") or "")
    return card.save(path)


def render_cards(ctx: dict, out_dir: str) -> dict:
    import pymupdf

    from bot.exam.bundle import read_doc

    t0 = time.perf_counter()
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    doc = read_doc(ctx["bundle"])
    work = Path(ctx.get("workdir") or out / "_img")
    imgs = _Imgs(ctx["bundle"], work, doc)
    archive = pymupdf.Archive(str(fonts.FONT_DIR))
    archive.add(str(work))
    css = _css(palette(ctx.get("colors")))
    lang = ctx.get("lang") or "en"
    max_h = int(ctx.get("max_h_px") or 1800)
    by_ref = {q["qref"]: (s["name"], q) for s in doc["sections"] for q in s["questions"]}
    cards: dict[str, list[str]] = {}
    try:
        for item in ctx["questions"]:
            sec, q = by_ref[item["qref"]]
            head = {**(ctx.get("header") or {}), "section": item.get("section", sec)}
            base = out / re.sub(r"[^A-Za-z0-9_-]+", "_", item["qref"])[:80]
            qno = f"Q{item.get('number') or q.get('number') or ''}"
            if item.get("kind") == "x":
                files = _explanation_cards(str(base), archive, css, head, qno, q, lang, imgs, max_h)
                if files:
                    cards[item["qref"]] = files
                continue
            parts = _parts(q, lang, imgs)
            one = f"{base}.png"
            _w, h = _draw(one, archive, css, head, qno, parts["q"], parts["o"])
            if h <= max_h:
                cards[item["qref"]] = [one]
                continue
            os.remove(one)
            a, b = f"{base}_1.png", f"{base}_2.png"
            if lang == "both" and len(parts["q"]) > 1:  # card 1 English (question + options), card 2 Hindi
                en, hi = _parts(q, "en", imgs), _parts(q, "hi", imgs)
                _draw(a, archive, css, head, f"{qno} · 1/2", [("ENGLISH", en["q"][0][1])], en["o"])
                _draw(b, archive, css, head, f"{qno} · 2/2", [("हिन्दी", hi["q"][0][1])], hi["o"])
            else:  # card 1 the question, card 2 the options
                _draw(a, archive, css, head, f"{qno} · 1/2", parts["q"], [])
                _draw(b, archive, css, head, f"{qno} · 2/2", [], parts["o"])
            cards[item["qref"]] = [a, b]
    finally:
        imgs.close()
    return {"cards": cards, "ms": int((time.perf_counter() - t0) * 1000)}
