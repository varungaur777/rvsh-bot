"""Custom exam PDF from the exam bundle (spec §14.3, §15.4) — ReportLab platypus, shaped Hindi/English, glass-banner
theme. Pool job (``render`` kind): bundle path + preferences in, one PDF out (one language per PDF).

Layout
------
- A4 portrait; **2 columns, always hybrid** (owner 2026-10-13: "2 coulmn hmesha highbrid hi bane"), or 1 column
  (``/ps``); page number bottom-right, watermark, clickable header/footer (``render.theme``).
- **Hybrid 2 columns (owner 2026-10-13):** a question whose image would have to shrink below ``SHRINK_MIN`` of its
  size to fit a column (a wide table, diagram, formula — not a text strip, which is re-wrapped) is printed across the
  **full width**; the questions around it stay in 2 columns (``TwoCol``: column 1 fills, then column 2; a run that
  ends before a wide question or a section end is balanced, the left column never shorter than the right — owner
  2026-10-13: a lone question after a wide one went to the right column and left the left one blank).
- A light rule after every question (owner 2026-10-13).
- Title banner (exam name, date • shift • time, language); a glass-banner strip per section.
- A question never splits from its options (``KeepTogether``). Colours (owner 2026-10-13): number black, question
  **navy, bold**; options black, regular, **labels (a)–(d) bold**; ``Answer: (b)`` **green**. The correct option
  itself is **never highlighted** ("jab correct option niche likh rahe ho to use highlight karne ki need nhi") —
  only a candidate's chosen option. Every colour comes from ``/ps`` → Colours (``colors``): question number,
  question, options, option labels (a)–(d), answer line, chosen right / wrong.
- Images: cropped to ``content_bbox`` (+ 4 px), scaled to the column; small images inline in the text, big ones as
  blocks (also under an option's text). A cut-off image (``broken`` in the bundle) prints the part that is left and
  "image could not be read" under it (only the note when nothing is left).
- Answers (``answers``): ``after`` — under each question ``Answer: (b)``; ``grid`` — an answer
  key table at the end of each section; ``key`` — only that answer key: no answer line and **no explanations**
  (owner 2026-10-15: "only anskey grid ka bh option" — "explanation and answer nhi likhna"); ``none``.
- Explanations (``explain``; question JSON files — ``/json``, owner 2026-10-13): ``after`` — under each question
  (after its answer line), ``end`` — an "Answers & explanations" part after the last section (every question's answer
  + its explanation), ``none``. Explanation images are placed like question images (small ones inline). A language's
  own answer key (``lang.<code>.correct``) wins over the question's.
- Candidate's choices (``chosen``: ``{q_hash: index}``, ``after`` mode only): the chosen option is printed **green
  (right) / red (wrong)** in a box that **hugs the content** — the text lines only, or the image.

Public API: ``render_exam_pdf(ctx, out_path) -> dict`` (``{"pages", "questions", "columns", "wide", "ms"}``;
``wide`` = questions printed full width). ``ctx``:
``bundle`` (zip path), ``lang`` (``en``/``hi``), ``columns`` (``1``/``2``; old ``"auto"`` = 2), ``answers``, ``explain``, ``chosen`` (or
``responses`` → mapped with ``schema.chosen_in``),
``sections`` (indices or ``None``; a section PDF keeps the paper's question numbers), ``title``, ``meta_line``, ``lang_label``, ``theme``, ``colors`` (``pdf.colors``),
``workdir`` (images are extracted there).

Depends on: reportlab + uharfbuzz, Pillow, ``render.rl_text``, ``render.theme``, ``exam.bundle``.
Used by: exam pipeline (Marks + Custom PDF), batch (P7), ``/gen_pdf``.
"""

from __future__ import annotations

import html as html_mod
import io
import re
import time
import zipfile
from pathlib import Path

from bot.render import rl_text, theme as T

PAGE_W, PAGE_H = 595.0, 842.0
MARGIN = 32.0
GAP = 14.0
PX = 0.75  # pt per image pixel (96 dpi)
FONT = 9.5
INLINE_H = 24.0  # pt: taller images are placed as blocks
LABELS = "abcdefghij"
MAX_IMG_H = 560.0  # pt: a taller image is shrunk (a question block must fit one page)
SHRINK_MIN = 0.8  # a column image smaller than this share of its size → that question goes full width
DEFAULTS = {"question_no": "#000000", "question": "#1a237e", "options": "#000000", "label": "#000000",
            "correct": "#2e7d32", "chosen_right": "#2e7d32", "chosen_wrong": "#c62828",
            "explain_fill": "#e3f2fd", "explain_head": "#1a237e"}


def split_br(markup: str) -> list[str]:
    """Mini-markup cut at its top-level ``<br/>`` (never inside an open ``<b>`` / ``<i>`` … or a ``<tbl>`` table) →
    paragraphs."""
    out, cur, depth = [], [], 0
    for i, tok in enumerate(re.split(r"(<br/>|</?(?:sup|sub|b|i|u|tbl)>)", markup or "")):
        if not tok:
            continue
        if tok == "<br/>" and depth == 0:
            out.append("".join(cur))
            cur = []
            continue
        if i % 2:  # a tag (odd parts are the separators); text starting with "<img …/>" is not one (B-035)
            if tok.startswith("</"):
                depth = max(0, depth - 1)
            elif tok != "<br/>":
                depth += 1
        cur.append(tok)
    out.append("".join(cur))
    return [p for p in (x.strip() for x in out) if p]


def segments(markup: str) -> list[tuple[str, object]]:
    """Mini-markup cut around its tables (``<tbl>`` rows, cells joined by `` | `` — ``jsonq.parse``):
    ``[("text", markup) | ("table", [[cell markup, …], …])]``; rows are padded to the same number of cells."""
    out, pos, s = [], 0, markup or ""

    def text(part: str) -> None:
        part = re.sub(r"^(\s|<br/>)+|(\s|<br/>)+$", "", part)
        if part:
            out.append(("text", part))

    for m in re.finditer(r"<tbl>(.*?)</tbl>", s, re.S):
        text(s[pos:m.start()])
        pos = m.end()
        rows = [[c.strip() for c in re.split(r"(?<=\s)\|(?=\s)", f" {row} ")]
                for row in m.group(1).split("<br/>") if row.strip()]
        if rows:
            n = max(len(r) for r in rows)
            out.append(("table", [r + [""] * (n - len(r)) for r in rows]))
    text(s[pos:])
    return out


def underline_to_bold(markup: str) -> str:
    """Underlined words of an explanation are printed bold (owner 2026-10-15: "explanations mai ager koi text
    underlined hai to usko bold krdo")."""
    return (markup or "").replace("<u>", "<b>").replace("</u>", "</b>")


# ------------------------------------------------------------------ images
class _Images:
    """The bundle images a PDF needs — extracted once; processed bundles (``exam.imgproc``) are used as they are,
    older bundles are cropped to ``content_bbox`` here. ``fit()`` wraps a text strip wider than its column at word
    gaps (reflow) instead of shrinking its text."""

    def __init__(self, zip_path: str, workdir: Path) -> None:
        self.z = zipfile.ZipFile(zip_path)
        self.dir = workdir
        self.dir.mkdir(parents=True, exist_ok=True)
        self.cache: dict[str, tuple[str, int, int]] = {}

    def get(self, img: dict) -> tuple[str, int, int]:
        """→ (path, width_px, height_px)."""
        if img["id"] in self.cache:
            return self.cache[img["id"]]
        from PIL import Image

        raw = self.z.read(img["file"])
        ext = Path(img["file"]).suffix.lower()
        path = self.dir / f"{img['id']}{ext if ext in ('.jpg', '.png') else '.png'}"
        with Image.open(io.BytesIO(raw)) as im:
            w, h = im.size
            box = img.get("content_bbox")
            if "scale" not in img and box and (box[2] - box[0]) * (box[3] - box[1]) < 0.95 * w * h:
                x0, y0, x1, y1 = max(0, box[0] - 4), max(0, box[1] - 4), min(w, box[2] + 4), min(h, box[3] + 4)
                im = im.crop((x0, y0, x1, y1))
                w, h = im.size
                im.convert("RGB").save(path, quality=93) if path.suffix == ".jpg" else im.save(path)
            elif ext == path.suffix:
                path.write_bytes(raw)  # as stored: no re-encoding
            else:
                im.save(path)
        self.cache[img["id"]] = (str(path), w, h)
        return self.cache[img["id"]]

    def fit(self, img: dict, max_w_pt: float) -> tuple[str, float, float]:
        """→ (path, width_pt, height_pt) at the image's own scale (text as big as the PDF text); a text strip wider
        than ``max_w_pt`` is reflowed at its word gaps, anything else is shrunk to fit."""
        return self.fit_k(img, max_w_pt)[:3]

    def fit_k(self, img: dict, max_w_pt: float) -> tuple[str, float, float, float]:
        """``fit`` plus the shrink factor (1.0 = the image keeps its size; a reflowed strip keeps its text size)."""
        path, w, h = self.get(img)
        s = img.get("scale") or PX
        if w * s <= max_w_pt:
            return path, w * s, h * s, 1.0
        lines = img.get("lines") or []
        max_px = int(max_w_pt / s)
        if lines and max_px > 40:
            key = f"{img['id']}@{max_px}"
            if key not in self.cache:
                self.cache[key] = self._reflow(path, lines, max_px, key)
            if self.cache[key]:
                rpath, rw, rh = self.cache[key]
                k = min(1.0, max_w_pt / (rw * s))  # a table kept whole may still be wider than the column
                return rpath, rw * s * k, rh * s * k, k
        k = max_w_pt / (w * s)
        return path, w * s * k, h * s * k, k

    def _reflow(self, path: str, lines: list, max_px: int, key: str):
        """The strip's words re-flowed into lines of at most ``max_px`` (owner 2026-10-13: lines broke inside words —
        "philosop|hy" — and every source line left a short stub). Words are found here from the ink (letter gaps vs
        word gaps told apart per image), filled line after line and only broken where a source paragraph ends (a
        source line ending short); every word sits on its line's baseline. A table inside the image
        (``imgproc.table_band``) is kept whole between the text above and below it. ``None`` (the image is shrunk
        instead) when some ink is outside the known text lines — nothing is ever dropped."""
        import numpy as np
        from PIL import Image

        from bot.exam.imgproc import table_band

        with Image.open(path) as src:
            im = src.convert("L") if src.mode not in ("L", "RGB") else src.copy()
        g = np.asarray(im.convert("L"))
        ink = g < 200
        band = table_band(g)
        lines = sorted((int(a), int(b)) for a, b, *_c in lines)
        covered = np.zeros(len(g), bool)
        for y0, y1 in lines:
            covered[max(0, y0 - 2):y1 + 3] = True
        if band:
            covered[max(0, band[0] - 2):band[1] + 3] = True
        if (ink.any(1) & ~covered).sum() > 2:
            return None

        def runs(mask):
            out, start = [], None
            for i, v in enumerate(mask.tolist() + [False]):
                if v and start is None:
                    start = i
                elif not v and start is not None:
                    out.append((start, i))
                    start = None
            return out

        info = []  # per text line: (y0, y1, baseline, glyph segments)
        for y0, y1 in lines:
            if band and band[0] <= y0 < band[1]:
                continue
            rows = ink[y0:y1].sum(1)
            if not rows.any():
                continue
            thr = 0.2 * rows.max()
            base = y0 + int(np.flatnonzero(rows >= thr)[-1]) + 1
            segs = runs(ink[y0:y1].any(0))
            if segs:
                info.append((y0, y1, base, segs))
        if not info:
            return None
        gaps = sorted(b[0] - a[1] for *_x, segs in info for a, b in zip(segs, segs[1:]))
        lh = sorted(y1 - y0 for y0, y1, _b, _s in info)[len(info) // 2]
        word_gap = max(3, 0.25 * lh)  # one group of gaps only: every gap this wide is a word gap
        if len(gaps) >= 6:  # letter gaps vs word gaps: the split with the clearest two groups
            best = None
            for t in sorted(set(gaps))[1:]:
                lo, hi = [x for x in gaps if x < t], [x for x in gaps if x >= t]
                m1, m2 = sum(lo) / len(lo), sum(hi) / len(hi)
                score = len(lo) * len(hi) * (m2 - m1) ** 2
                if best is None or score > best[0]:
                    best = (score, t, m1, m2)
            if best and best[3] >= 1.8 * max(1.0, best[2]) and best[1] <= 0.9 * lh:
                word_gap = max(2, best[1])
        right = max(segs[-1][1] for *_x, segs in info)
        space = max(3, int(round(min(0.45 * lh, max(word_gap, 0.3 * lh)))))

        def words_of(y0, y1, base, segs):
            top, bottom = max(0, y0 - 2), min(im.height, y1 + 2)
            out, a = [], segs[0][0]
            for (s0, s1), nxt in zip(segs, segs[1:] + [None]):
                if nxt is None or nxt[0] - s1 >= word_gap:
                    out.append((im.crop((a, top, s1, bottom)), base - top))
                    if nxt is not None:
                        a = nxt[0]
            return out

        def first_word(segs):
            end = segs[0][1]
            for (_s0, s1), nxt in zip(segs, segs[1:]):
                end = s1
                if nxt[0] - s1 >= word_gap:
                    break
            return end - segs[0][0]

        def flow(part):
            """→ output lines: lists of (word image, baseline offset)."""
            out, cur, cur_w = [], [], 0
            for i, (y0, y1, base, segs) in enumerate(part):
                nxt = part[i + 1] if i + 1 < len(part) else None
                # the source broke this line on purpose (a paragraph / list item ends) when the next line's first
                # word would still have fitted on it
                ends = nxt is not None and segs[-1][1] + space + first_word(nxt[3]) < right - 2
                for wimg, boff in words_of(y0, y1, base, segs):
                    while wimg.width > max_px:  # a word wider than the column: cut it (rare)
                        if cur:
                            out.append(cur)
                        out.append([(wimg.crop((0, 0, max_px, wimg.height)), boff)])
                        cur, cur_w = [], 0
                        wimg = wimg.crop((max_px, 0, wimg.width, wimg.height))
                    if cur and cur_w + space + wimg.width > max_px:
                        out.append(cur)
                        cur, cur_w = [], 0
                    cur_w += (space if cur else 0) + wimg.width
                    cur.append((wimg, boff))
                if ends and cur:
                    out.append(cur)
                    cur, cur_w = [], 0
            if cur:
                out.append(cur)
            return out

        rows_out = []  # (image, height) blocks top-down
        for ln in flow([x for x in info if not band or x[1] <= band[0]]):
            rows_out.append(ln)
        if band:
            block = im.crop((0, max(0, band[0] - 2), im.width, min(im.height, band[1] + 2)))
            bbox = block.convert("L").point(lambda v: 255 if v < 225 else 0).getbbox()
            if bbox:
                rows_out.append(block.crop((bbox[0], 0, bbox[2], block.height)))
        rows_out += flow([x for x in info if band and x[0] >= band[1]])

        def height(r):
            if not isinstance(r, list):
                return r.height
            return max(b for _w, b in r) + max(w.height - b for w, b in r)

        def width(r):
            return r.width if not isinstance(r, list) else sum(w.width for w, _b in r) + space * (len(r) - 1)

        if not rows_out:
            return None
        gap = max(2, int(0.35 * lh))
        out_w = max(width(r) for r in rows_out) + 4
        out_h = sum(height(r) for r in rows_out) + gap * (len(rows_out) - 1) + 4
        out = Image.new(im.mode, (out_w, out_h), 255 if im.mode == "L" else (255, 255, 255))
        y = 2
        for r in rows_out:
            if isinstance(r, list):
                asc = max(b for _w, b in r)
                x = 2
                for wimg, boff in r:
                    out.paste(wimg, (x, y + asc - boff))
                    x += wimg.width + space
            else:
                out.paste(r, (2, y))
            y += height(r) + gap
        rpath = self.dir / (key.replace("@", "_r") + ".png")
        out.save(rpath, compress_level=6)
        return str(rpath), out.width, out.height

    def close(self) -> None:
        self.z.close()


# ------------------------------------------------------------------ flowables
def _flowables():
    from reportlab.platypus import Flowable

    class HugBox(Flowable):
        """A box that hugs its content: the real width of a paragraph's lines, or an image (spec §15.4)."""

        def __init__(self, inner, color, pad: float = 2.5) -> None:
            super().__init__()
            self.inner, self.color, self.pad = inner, color, pad

        def wrap(self, aw, ah):
            w, h = self.inner.wrap(aw - 2 * self.pad, ah)
            widths = getattr(self.inner, "getActualLineWidths0", None)
            used = max(widths()) if widths and widths() else w
            self.box_w, self.h_in = min(w, used), h
            self.width, self.height = w + 2 * self.pad, h + 2 * self.pad
            return self.width, self.height

        def draw(self):
            c = self.canv
            self.inner.drawOn(c, self.pad, self.pad)
            c.saveState()
            c.setStrokeColor(self.color)
            c.setLineWidth(1.3)
            c.roundRect(0.5, 0.5, self.box_w + 2 * self.pad - 1, self.h_in + 2 * self.pad - 1, 3, stroke=1, fill=0)
            c.restoreState()

    class Banner(Flowable):
        """Glass banner, every line centred (owner 2026-10-15: "heading ko center mai, uske niche uski extra info"):
        the heading, a short accent rule with dots at its ends (``rule``), then the details in smaller type.
        ``lines``: ``[(text, weight, size, colour)]``."""

        def __init__(self, th: dict, lines: list, pad: float = 10.0, rule: bool = False, radius: float = 9.0) -> None:
            super().__init__()
            self.th, self.lines, self.pad, self.rule, self.radius = th, [x for x in lines if x[0]], pad, rule, radius

        def wrap(self, aw, ah):
            from reportlab.lib.styles import ParagraphStyle
            from reportlab.platypus import Paragraph

            self.paras = [Paragraph(rl_text.to_para(text, weight),
                                    ParagraphStyle("b", fontName=rl_text.NAMES[weight]["latin"], fontSize=size,
                                                   leading=size * 1.28, textColor=colour, alignment=1, shaping=1))
                          for text, weight, size, colour in self.lines]
            self.width = aw
            self.inner_h = [p.wrap(aw - 2 * self.pad - 8, ah)[1] for p in self.paras]
            self.rule_h = 9.0 if self.rule and len(self.paras) > 1 else 1.5 if len(self.paras) > 1 else 0.0
            self.height = sum(self.inner_h) + self.rule_h + 2 * self.pad
            return self.width, self.height

        def draw(self):
            c, w, h = self.canv, self.width, self.height
            T.banner(c, self.th, 1, 1, w - 2, h - 2, radius=self.radius)
            c.saveState()  # glass highlight: a thin white line just inside the border
            c.setStrokeColor(T.color("#ffffff"), alpha=0.9)
            c.setLineWidth(0.8)
            c.roundRect(4, 4, w - 8, h - 8, max(2.0, self.radius - 3), stroke=1, fill=0)
            c.restoreState()
            y = h - self.pad
            for i, (p, ph) in enumerate(zip(self.paras, self.inner_h)):
                y -= ph
                p.drawOn(c, self.pad + 4, y)
                if i == 0 and self.rule_h:
                    y -= self.rule_h
                    if self.rule:
                        mid, half = w / 2, 34.0
                        accent = T.color(self.th["colors"].get("header") or "#1a237e")
                        c.saveState()
                        c.setStrokeColor(accent)
                        c.setFillColor(accent)
                        c.setLineWidth(1.2)
                        c.line(mid - half, y + 4.5, mid + half, y + 4.5)
                        for x in (mid - half - 4, mid + half + 4):
                            c.circle(x, y + 4.5, 1.6, stroke=0, fill=1)
                        c.restoreState()

    class Strip(Flowable):
        """A see-through tinted strip across the column holding one paragraph (owner 2026-10-15: "answer ko bhi ek
        transparent strip mai rkhna")."""

        def __init__(self, para, color, pad: float = 2.5) -> None:
            super().__init__()
            self.para, self.color, self.pad = para, color, pad
            self.spaceBefore, self.spaceAfter = 2.0, 1.0

        def wrap(self, aw, ah):
            _w, h = self.para.wrap(aw - 2 * self.pad - 4, ah)
            self.width, self.height = aw, h + 2 * self.pad
            return self.width, self.height

        def draw(self):
            c = self.canv
            c.saveState()
            c.setFillColor(self.color, alpha=0.12)
            c.setStrokeColor(self.color, alpha=0.35)
            c.setLineWidth(0.6)
            c.roundRect(0.3, 0.3, self.width - 0.6, self.height - 0.6, 3.5, stroke=1, fill=1)
            c.restoreState()
            self.para.drawOn(c, self.pad + 4, self.pad)

    class Boxed(Flowable):
        """A see-through light box behind an explanation (owner 2026-10-15: "explanation hai to uska ek background mai
        light colour ka box") with a thin accent bar on its left. It splits between its parts and inside a paragraph
        or table, so a long explanation goes on in the next column instead of leaving this one half empty ("khali
        jagah mat chhodo") — never with its heading alone."""

        splittable = True
        PAD, BAR = 5.0, 2.6
        INSET = 2 * PAD + BAR + 2  # width taken by the box itself

        def __init__(self, parts, fill, bar, headed: int = 1) -> None:
            super().__init__()  # headed: the first parts are headings, never left alone at a column's end
            self.parts, self.fill, self.bar, self.headed = list(parts), fill, bar, headed
            self.spaceBefore, self.spaceAfter = 3.0, 1.0

        def _sizes(self, iw):
            out = []
            for p in self.parts:
                _w, h = p.wrap(iw, 10_000)
                out.append((p, p.getSpaceBefore(), h, p.getSpaceAfter()))
            return out

        def wrap(self, aw, ah):
            self.sizes = self._sizes(aw - self.INSET)
            self.width, self.height = aw, sum(b + h + a for _p, b, h, a in self.sizes) + 2 * self.PAD
            return self.width, self.height

        def split(self, aw, ah):
            iw, room = aw - self.INSET, ah - 2 * self.PAD
            sizes, y, k = self._sizes(iw), 0.0, 0
            while k < len(sizes) and y + sum(sizes[k][1:]) <= room:
                y += sum(sizes[k][1:])
                k += 1
            if k == len(sizes):
                return [self]
            head, tail = self.parts[:k], self.parts[k:]
            rest = room - y - sizes[k][1]
            if rest > FONT * 2.2 and hasattr(self.parts[k], "split"):
                self.parts[k].wrap(iw, rest)
                sub = self.parts[k].split(iw, rest)
                if len(sub) >= 2:
                    head, tail = self.parts[:k] + [sub[0]], list(sub[1:]) + self.parts[k + 1:]
            if len(head) <= self.headed and all(a is b for a, b in zip(head, self.parts)):
                return []  # not even the heading and one line fit here
            return [Boxed(head, self.fill, self.bar, self.headed), Boxed(tail, self.fill, self.bar, 0)]

        def draw(self):
            c, w, h = self.canv, self.width, self.height
            c.saveState()
            path = c.beginPath()
            path.roundRect(0, 0, w, h, 4)
            c.clipPath(path, stroke=0, fill=0)
            c.setFillColor(self.fill, alpha=0.75)
            c.rect(0, 0, w, h, stroke=0, fill=1)
            c.setFillColor(self.bar, alpha=0.85)
            c.rect(0, 0, self.BAR, h, stroke=0, fill=1)
            c.restoreState()
            y = h - self.PAD
            for p, b, ph, a in self.sizes:
                y -= b + ph
                p.drawOn(c, self.BAR + self.PAD + 2, y)
                y -= a

    class Indent(Flowable):
        """``inner`` moved right by ``left`` pt (an image under an option's text, in line with that text)."""

        def __init__(self, inner, left: float) -> None:
            super().__init__()
            self.inner, self.left = inner, left

        def wrap(self, aw, ah):
            w, h = self.inner.wrap(aw - self.left, ah)
            self.width, self.height = w + self.left, h
            return self.width, self.height

        def draw(self):
            self.inner.drawOn(self.canv, self.left, 0)

    class Noted(Flowable):
        """A cut-off image: the part that is left (or nothing) with a small grey note under it."""

        def __init__(self, img, note: str, font: str, size: float, color) -> None:
            from reportlab.pdfbase.pdfmetrics import stringWidth

            super().__init__()
            self.img, self.note, self.font, self.size, self.color = img, note, font, size, color
            self.ih = img.drawHeight + 2 if img is not None else 0
            self.width = max(img.drawWidth if img is not None else 0, stringWidth(note, font, size))
            self.height = self.ih + size * 1.35
            self.hAlign = "LEFT"

        def wrap(self, aw, ah):
            return self.width, self.height

        def minWidth(self):
            return self.width

        def draw(self):
            c = self.canv
            if self.img is not None:
                self.img.drawOn(c, 0, self.height - self.ih + 2)
            c.saveState()
            c.setFont(self.font, self.size)
            c.setFillColor(self.color)
            c.drawString(0, self.size * 0.3, self.note)
            c.restoreState()

    class Stack(Flowable):
        """One question's parts drawn top-down as one block (never split, unless taller than a whole column)."""

        def __init__(self, parts) -> None:
            super().__init__()
            self.parts = parts

        def _sizes(self, aw):
            out = []
            for p in self.parts:
                _w, h = p.wrap(aw, 10_000)
                out.append((p, p.getSpaceBefore(), h, p.getSpaceAfter()))
            return out

        def wrap(self, aw, ah):
            self.sizes = self._sizes(aw)
            self.width, self.height = aw, sum(b + h + a for _p, b, h, a in self.sizes)
            return self.width, self.height

        def split(self, aw, ah):
            sizes, y, k = self._sizes(aw), 0.0, 0
            while k < len(sizes) and y + sum(sizes[k][1:]) <= ah:
                y += sum(sizes[k][1:])
                k += 1
            if k == 0 or k == len(sizes):
                return []
            return [Stack(self.parts[:k]), Stack(self.parts[k:])]

        def draw(self):
            y = self.height
            for p, b, h, a in self.sizes:
                y -= b + h
                p.drawOn(self.canv, 0, y)
                y -= a

    class TwoCol(Flowable):
        """Question blocks in two columns (owner 2026-10-13, hybrid layout): column 1 is filled, then column 2;
        when the whole run fits it is balanced (both columns end level), so a full-width question or the next
        section starts right under it. ``split`` gives the part that fits the rest of the page. A ``splittable``
        block (an explanation box) is cut where a column ends, so no column is left half empty (owner 2026-10-15)."""

        MIN_SPLIT = 30.0  # pt: less room than this at a column's end is not worth a cut

        def __init__(self, items, col_w: float, gap: float, full_h: float, cut: int | None = None) -> None:
            super().__init__()
            self.items, self.col_w, self.gap, self.full_h, self.cut = list(items), col_w, gap, full_h, cut
            self.base = list(items)  # as given: a page split starts from these, not from a balancing cut
            self._h = None

        def _one(self, it) -> float:
            _w, h = it.wrap(self.col_w, 10_000)
            return it.getSpaceBefore() + h + it.getSpaceAfter()

        def heights(self):
            if self._h is None:
                self._h = [self._one(it) for it in self.items]
            return self._h

        def _cut_item(self, it, room: float):
            """``[head, tail]`` of a splittable block whose head fits ``room``, else ``None``."""
            room -= it.getSpaceBefore() + it.getSpaceAfter()
            if not getattr(it, "splittable", False) or room < self.MIN_SPLIT:
                return None
            parts = it.split(self.col_w, room)
            if len(parts) != 2 or self._one(parts[0]) > room + it.getSpaceBefore() + it.getSpaceAfter() + 0.01:
                return None
            return parts

        def wrap(self, aw, ah):
            hs = self.heights()
            if self.cut is None:  # balance: the first split where the left column is at least as tall as the right
                total, run, k = sum(hs), 0.0, 0
                while k < len(hs) and run < total - run:
                    run += hs[k]
                    k += 1
                self.cut = k
                # the left column ends in a long explanation: cut it so both columns end about level (the left one
                # a line longer, never shorter) — only when the run is printed here (else ``split`` cuts it)
                if k and run - (total - run) > 2 * self.MIN_SPLIT and run <= ah:
                    prior = run - hs[k - 1]
                    parts = self._cut_item(self.items[k - 1], total / 2 - prior + 2 * self.MIN_SPLIT / 3)
                    if parts:
                        self.items[k - 1:k] = parts
                        self._h = None
                        hs = self.heights()
            self.width = aw
            self.height = max(sum(hs[:self.cut]), sum(hs[self.cut:]))
            return self.width, self.height

        def split(self, aw, ah):
            items = list(self.base)
            hs = [self._one(it) for it in items]

            def fill(start):
                y, i = 0.0, start
                while i < len(items):
                    if y + hs[i] <= ah:
                        y += hs[i]
                        i += 1
                        continue
                    parts = self._cut_item(items[i], ah - y)
                    if parts:
                        items[i:i + 1] = parts
                        hs[i:i + 1] = [self._one(parts[0]), self._one(parts[1])]
                        i += 1
                    break
                return i

            k = fill(0)
            j = fill(k)
            if j == 0:  # not even one block fits here
                if ah < 0.9 * self.full_h:
                    return []  # → next page
                first = items[0]  # a block taller than a whole column: cut it at its parts
                parts = first.split(self.col_w, ah) if hasattr(first, "split") else []
                if len(parts) < 2:
                    k = j = 1  # cannot be cut: printed as it is
                else:
                    rest = TwoCol(parts[1:] + items[1:], self.col_w, self.gap, self.full_h)
                    return [TwoCol(parts[:1], self.col_w, self.gap, self.full_h, cut=1), rest]
            if j >= len(items):
                return [TwoCol(items, self.col_w, self.gap, self.full_h, cut=k)]
            return [TwoCol(items[:j], self.col_w, self.gap, self.full_h, cut=k),
                    TwoCol(items[j:], self.col_w, self.gap, self.full_h)]

        def draw(self):
            hs = self.heights()
            for col, (a, b) in enumerate(((0, self.cut), (self.cut, len(self.items)))):
                x, y = col * (self.col_w + self.gap), self.height
                for it, h in zip(self.items[a:b], hs[a:b]):
                    it.wrap(self.col_w, 10_000)
                    y -= h
                    it.drawOn(self.canv, x, y + it.getSpaceAfter())

    return HugBox, Banner, Stack, TwoCol, Indent, Noted, Boxed, Strip


# ------------------------------------------------------------------ builder
def render_exam_pdf(ctx: dict, out_path: str) -> dict:
    t0 = time.perf_counter()
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.platypus import (BaseDocTemplate, Frame, HRFlowable, Image, KeepTogether, NextPageTemplate,
                                    PageTemplate, Paragraph, Spacer, Table, TableStyle)

    from bot.exam.bundle import read_doc
    from bot.exam.schema import correct_of

    rl_text.register()
    rl_text.set_font(ctx.get("font") or "poppins")  # /ps → pdf.font (Hindi is always Noto Sans Devanagari)
    LR, LSB, LB = (rl_text.NAMES[w]["latin"] for w in ("r", "sb", "b"))
    T.register_fonts()
    HugBox, Banner, Stack, TwoCol, Indent, Noted, Boxed, Strip = _flowables()
    th = ctx.get("theme") or {"colors": dict(T.DEFAULT_COLORS)}
    colors = {**DEFAULTS, **{k: v for k, v in (ctx.get("colors") or {}).items() if v}}
    C = {k: T.color(v) for k, v in colors.items()}
    doc = read_doc(ctx["bundle"])
    lang = ctx.get("lang") or (doc["meta"].get("languages") or ["en"])[0]
    answers = ctx.get("answers") or "after"
    explain = ctx.get("explain") or "none"
    if answers == "key":  # only the answer key grid: no answer lines, no explanations
        answers, explain = "grid", "none"
    chosen = ctx.get("chosen")
    if chosen is None and ctx.get("responses"):
        from bot.exam.schema import chosen_in
        chosen = chosen_in(doc, ctx["responses"])
    chosen = chosen if answers == "after" else None
    first_no, n = [], 0  # a section PDF keeps the paper's own question numbers (Q26–50, not Q1–25)
    for s in doc["sections"]:
        first_no.append(n)
        n += len(s["questions"])
    picked = [i for i in range(len(doc["sections"])) if ctx.get("sections") is None or i in ctx["sections"]]
    secs = [doc["sections"][i] for i in picked]
    images = _Images(ctx["bundle"], Path(ctx.get("workdir") or Path(out_path).parent / "_img"))

    def content(q: dict) -> dict:
        return q["lang"].get(lang) or next(iter(q["lang"].values()))

    cols = 1 if str(ctx.get("columns") or 2) == "1" else 2  # 2 = hybrid (a wide question spans the page)
    usable_w = PAGE_W - 2 * MARGIN
    body_h = PAGE_H - 2 * MARGIN - 6
    col_w = usable_w if cols == 1 else (usable_w - GAP) / 2

    st_q = ParagraphStyle("q", fontName=LB, fontSize=FONT, leading=FONT * 1.38, textColor=C["question"], shaping=1)
    st_o = ParagraphStyle("o", fontName=LR, fontSize=FONT, leading=FONT * 1.35, textColor=C["options"], shaping=1,
                          leftIndent=14, firstLineIndent=-14)
    st_ans = ParagraphStyle("a", fontName=LSB, fontSize=FONT - 0.5, leading=FONT * 1.3, textColor=C["correct"], shaping=1)
    st_x = ParagraphStyle("x", fontName=LR, fontSize=FONT - 0.5, leading=(FONT - 0.5) * 1.4, textColor=C["options"], shaping=1)

    def fit(st, markup: str, inline: dict):
        """A paragraph holding an inline picture taller than its line (a fraction): lines grow to fit it and a
        little room above / below, so it never overlaps the next or previous line. Others keep ``st``."""
        tall = max((inline[a][2] for a in inline if f'"{a}"' in (markup or "")), default=0.0)
        if tall <= st.leading * 0.95:
            return st
        pad = max(0.0, (tall - st.fontSize) / 2)
        return ParagraphStyle(st.name + "t", parent=st, autoLeading="max", spaceBefore=st.spaceBefore + pad,
                              spaceAfter=st.spaceAfter + pad)
    muted = T.color(th["colors"]["muted"])

    def img_map(q: dict, c: dict, max_w: float) -> tuple[dict, list[str]]:
        """Small images (formulas, symbols — at most ``INLINE_H`` tall) go inline in the text; image-only parts and
        big images become blocks scaled to the column."""
        inline, blocks = {}, []
        for im in q.get("images") or []:
            path, w, h = images.get(im)
            sc = im.get("scale") or PX
            markup = c["question"] if im["role"] == "question" else (c.get("explanation") or "") if im["role"] == "explanation" else (
                c["options"][int(im["role"].split("_")[1]) - 1] if im["role"].startswith("option_")
                and int(im["role"].split("_")[1]) <= len(c["options"]) else "")
            if f'"{im["id"]}"' not in markup:
                continue  # an image of the other language
            if not im.get("broken") and rl_text.plain_has_text(markup) and h * sc <= INLINE_H and w * sc <= max_w:
                inline[im["id"]] = (path, w * sc, h * sc)  # formulas / symbols at text size, in the line
            else:
                blocks.append(im["id"])
        return inline, blocks

    def block_image(q: dict, aid: str, max_w: float):
        im = next(i for i in q["images"] if i["id"] == aid)
        if im.get("broken") == "all":
            return Noted(None, "[image could not be read]", LR, 7.5, muted)
        path, w_pt, h_pt = images.fit(im, max_w)
        if h_pt > MAX_IMG_H:  # never taller than most of a page (a column block must fit a page)
            w_pt, h_pt = w_pt * MAX_IMG_H / h_pt, MAX_IMG_H
        flow = Image(path, width=w_pt, height=h_pt)
        flow.hAlign = "LEFT"
        return Noted(flow, "[the rest of this image could not be read]", LR, 7.5, muted) if im.get("broken") else flow

    def valign(img) -> str:
        """An option image of one text line: its label in the middle; taller (2+ lines, a figure): at the top, level
        with the first line."""
        return "MIDDLE" if getattr(img, "drawHeight", 99) <= FONT * 2 else "TOP"

    zebra = T.color(th["colors"].get("zebra") or "#f7f7f9")
    rule = T.color(th["colors"].get("rule") or "#d0d0d0")
    st_c = ParagraphStyle("c", fontName=LR, fontSize=FONT - 1, leading=(FONT - 1) * 1.32, textColor=C["options"],
                          shaping=1)

    def text_w(text: str, size: float) -> float:
        """Width of plain text in the fonts ``rl_text`` would pick (marks / joiners: the current font)."""
        from reportlab.pdfbase.pdfmetrics import stringWidth

        names, w, cur = rl_text.NAMES["r"], 0.0, "latin"
        for ch in text:
            sc = rl_text.script_of(ch)
            cur = "math" if sc == "missing" else (sc or cur)
            w += stringWidth(ch, names[cur], size)
        return w

    def cell_images(q: dict, cell: str, max_w: float) -> dict:
        """Every picture of a table cell inline in it, fitted to the cell."""
        out = {}
        for aid in re.findall(r'<img id="([^"]+)"', cell):
            im = next((i for i in q.get("images") or [] if i["id"] == aid), None)
            if im is None or im.get("broken") == "all":
                continue
            path, w_pt, h_pt = images.fit(im, max(20.0, max_w))
            if h_pt > MAX_IMG_H / 2:
                w_pt, h_pt = w_pt * MAX_IMG_H / 2 / h_pt, MAX_IMG_H / 2
            out[aid] = (path, w_pt, h_pt)
        return out

    def table_widths(q: dict, rows: list, avail: float) -> tuple[list, list]:
        """Per column: (natural width on one line, narrowest width — its longest word or 50 pt of a picture)."""
        n, pad, size = len(rows[0]), 8.0, FONT - 1
        nat, least = [pad] * n, [pad] * n
        for r in rows:
            for i, cell in enumerate(r):
                text = html_mod.unescape(re.sub(r"<[^>]+>", " ", cell))
                k = 1.07 if "<b>" in cell else 1.0
                pics = sum(w for _p, w, _h in cell_images(q, cell, avail).values())
                nat[i] = max(nat[i], text_w(" ".join(text.split()), size) * k + pics + pad)
                word = max((text_w(x, size) * k for x in text.split()), default=0.0)
                least[i] = max(least[i], max(word, min(pics, 50.0)) + pad)
        return nat, least

    def table_flow(q: dict, rows: list, avail: float):
        """A table of the markup (``segments``) as a grid: columns as wide as their text, narrowed to ``avail`` the
        way a browser does (each column keeps at least its longest word); first row shaded, then every second."""
        nat, least = table_widths(q, rows, avail)
        if sum(nat) <= avail:
            widths = nat
        elif sum(least) >= avail:
            widths = [avail * x / sum(least) for x in least]
        else:
            flex = [a - b for a, b in zip(nat, least)]
            widths = [b + (avail - sum(least)) * f / (sum(flex) or 1) for b, f in zip(least, flex)]
        data = []
        for r in rows:
            row = []
            for i, cell in enumerate(r):
                inl = cell_images(q, cell, widths[i] - 8)
                row.append(Paragraph(rl_text.to_para(cell, "r", inl), fit(st_c, cell, inl)) if cell else "")
            data.append(row)
        t = Table(data, colWidths=widths, hAlign="LEFT")
        t.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.5, rule), ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                               ("LEFTPADDING", (0, 0), (-1, -1), 4), ("RIGHTPADDING", (0, 0), (-1, -1), 4),
                               ("TOPPADDING", (0, 0), (-1, -1), 2), ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
                               ("ROWBACKGROUNDS", (0, 0), (-1, -1), [zebra, None])]))
        t.spaceBefore, t.spaceAfter = 2, 3
        return t

    def wide(q: dict, only_expl: bool = False) -> bool:
        """2 columns: would one of this question's block images shrink below ``SHRINK_MIN`` in a column, or does a
        table of it not fit a column even with its columns narrowed to their longest words?
        ``only_expl``: its explanation's images / tables (the "Answers & explanations" part)."""
        c = content(q)
        _inline, blocks = img_map(q, c, col_w - 16)
        ex = (c.get("explanation") or "") if explain != "none" else ""
        in_tables = "".join(str(v) for m in (ex, c["question"]) if "<tbl>" in m for k, v in segments(m) if k == "table")
        for aid in blocks:
            in_ex = f'"{aid}"' in ex
            if (only_expl and not in_ex) or (in_ex and not only_expl and explain != "after"):
                continue
            if f'"{aid}"' in in_tables:
                continue  # a picture in a table cell is fitted to its cell
            im = next(i for i in q["images"] if i["id"] == aid)
            limit = col_w - 4 if f'"{aid}"' in c["question"] or in_ex else col_w - 30
            if images.fit_k(im, limit)[3] < SHRINK_MIN:
                return True
        marks = ([] if only_expl else [(c["question"], col_w - 4)]) + (
            [(ex, col_w - Boxed.INSET)] if ex and (only_expl or explain == "after") else [])
        for markup, avail in marks:
            for kind, rows in segments(markup) if "<tbl>" in markup else []:
                if kind == "table" and sum(table_widths(q, rows, avail)[1]) > avail:
                    return True
        return False

    def answer_text(q: dict, c: dict) -> str:
        k = correct_of(q, lang)
        return f"({LABELS[k]})" if k is not None and k < len(c["options"]) else "dropped by the board"

    def expl_box(q: dict, w: float, head=None):
        """The explanation in its light box (``Boxed``): ``head`` (default "Explanation"), its paragraphs (a long
        one can break between them and inside them), tables and block images; underlined words bold. ``None`` when
        there is no explanation."""
        c = content(q)
        ex = underline_to_bold(c.get("explanation") or "")
        if not ex:
            return None
        iw = w - Boxed.INSET
        inline, blocks = img_map(q, c, iw - 16)
        body = []
        for kind, val in segments(ex):
            if kind == "table":
                body.append(table_flow(q, val, iw))
                continue
            for para in split_br(val):
                mine_blocks = [b for b in blocks if f'"{b}"' in para]
                text = para
                for b in mine_blocks:
                    text = text.replace(f'<img id="{b}"/>', " ")
                if rl_text.plain_has_text(text):
                    body.append(Paragraph(rl_text.to_para(text, "r", inline), fit(st_x, text, inline)))
                for b in mine_blocks:
                    body.append(block_image(q, b, iw - 4))
        if not body:
            return None
        label = Paragraph(f'<font name="{LB}" color="{colors["explain_head"]}">Explanation</font>', st_x)
        heads = [label] if head is None else [head, label]
        return Boxed([*heads, *body], C["explain_fill"], C["explain_head"], headed=len(heads))

    def question_flow(n: int, q: dict, col_w: float = col_w, in_cols: bool = False) -> list:
        c = content(q)
        inline, blocks = img_map(q, c, col_w - 16)
        num = f'<font name="{LB}" color="{colors["question_no"]}">Q{n}.</font> '
        parts = []
        for kind, val in segments(c["question"]) or [("text", "")]:
            if kind == "table":
                if not parts:
                    parts.append(Paragraph(num, st_q))
                parts.append(table_flow(q, val, col_w - 4))
                continue
            parts.append(Paragraph((num if not parts else "") + rl_text.to_para(val, "b", inline),
                                   fit(st_q, val, inline)))
            for aid in [b for b in blocks if f'"{b}"' in val]:
                parts.append(block_image(q, aid, col_w - 4))
        correct = correct_of(q, lang)
        mine = (chosen or {}).get(q.get("qref") or q["q_hash"]) if chosen is not None else None
        for i, opt in enumerate(c["options"]):
            color = (colors["chosen_right"] if i == correct else colors["chosen_wrong"]) if mine == i else colors["options"]
            label = f'<font name="{LB}" color="{color if mine == i else colors["label"]}">({LABELS[i]})</font>'
            text = rl_text.to_para(opt, "r", inline)
            opt_blocks = [b for b in blocks if f'"{b}"' in opt]
            if opt_blocks and not rl_text.plain_has_text(opt):
                img = block_image(q, opt_blocks[0], col_w - 30)
                row = Table([[Paragraph(label, st_o), img]], colWidths=[20, None],
                            hAlign="LEFT")
                row.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), valign(img)), ("LEFTPADDING", (0, 0), (-1, -1), 0),
                                         ("RIGHTPADDING", (0, 0), (-1, -1), 2), ("TOPPADDING", (0, 0), (-1, -1), 1),
                                         ("BOTTOMPADDING", (0, 0), (-1, -1), 1)]))
                flow = row
                if mine == i:
                    flow = Table([[Paragraph(label, st_o),
                                   HugBox(img, C["chosen_right"] if i == correct else C["chosen_wrong"])]],
                                 colWidths=[20, None], hAlign="LEFT")
                    flow.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), valign(img)), ("LEFTPADDING", (0, 0), (-1, -1), 0),
                                              ("TOPPADDING", (0, 0), (-1, -1), 1), ("BOTTOMPADDING", (0, 0), (-1, -1), 1)]))
            else:
                para = Paragraph(f'{label} <font color="{color}">{text}</font>', fit(st_o, opt, inline))
                flow = HugBox(para, C["chosen_right"] if i == correct else C["chosen_wrong"]) if mine == i else para
                parts.append(flow)
                for b in opt_blocks:  # a big image under the option's text, in line with that text
                    parts.append(Indent(block_image(q, b, col_w - 30), 14))
                continue
            parts.append(flow)
        if answers == "after":  # in a see-through strip of the answer colour (owner 2026-10-15)
            parts.append(Strip(Paragraph(f"Answer: {answer_text(q, c)}", st_ans), C["correct"]))
        line = HRFlowable(width="100%", thickness=0.4, color=rule, spaceBefore=4, spaceAfter=5)
        box = expl_box(q, col_w) if explain == "after" else None
        if box is not None:
            return [Stack(parts), box, line] if in_cols else [KeepTogether(parts), box, line]
        return [Stack(parts + [line])] if in_cols else [KeepTogether(parts), line]

    story: list = []
    border, text_c = T.color(th["colors"]["banner_border"]), T.color(th["colors"].get("text") or "#111111")
    n_all = sum(len(s["questions"]) for s in secs)
    meta = ctx.get("meta_line") or ""
    info = "  ·  ".join(x for x in (ctx.get("lang_label") or "",
                                     "" if "question" in meta.lower() else f"{n_all} questions") if x)
    title = Banner(th, [(ctx.get("title") or doc["meta"].get("exam_name") or "Exam", "b", 16, border),
                        (meta, "sb", 9.5, text_c), (info, "r", 8.5, muted)], pad=11, rule=True, radius=10)
    story += [NextPageTemplate("later"), title, Spacer(1, 9)]
    n_q, n_wide = 0, 0
    for idx, sec in zip(picked, secs):
        a, b = first_no[idx] + 1, first_no[idx] + len(sec["questions"])
        story.append(Banner(th, [(sec["name"], "b", 10.5, border),
                                 (f"{b - a + 1} questions  ·  Q{a}–Q{b}" if b > a else f"1 question  ·  Q{a}", "r", 7.5, muted)],
                            pad=5, radius=7))
        story.append(Spacer(1, 6))
        key, run = [], []
        for k, q in enumerate(sec["questions"], first_no[idx] + 1):
            n_q += 1
            key.append((k, correct_of(q, lang)))
            if cols == 1:
                story += question_flow(k, q)
            elif wide(q):  # hybrid: this one across the page, the run before it in 2 balanced columns
                if run:
                    story.append(TwoCol(run, col_w, GAP, body_h))
                    run = []
                story += question_flow(k, q, usable_w)
                n_wide += 1
            else:
                run += question_flow(k, q, in_cols=True)
        if run:
            story.append(TwoCol(run, col_w, GAP, body_h))
        if answers == "grid":
            per = 10
            cells = [f"{n} – ({LABELS[c]})" if c is not None else f"{n} – ×" for n, c in key]
            rows = [cells[i:i + per] + [""] * (per - len(cells[i:i + per])) for i in range(0, len(cells), per)]
            grid = Table(rows, colWidths=[usable_w / per] * per, hAlign="LEFT")
            grid.setStyle(TableStyle([("FONT", (0, 0), (-1, -1), LSB, 8.5), ("TEXTCOLOR", (0, 0), (-1, -1), C["correct"]),
                                      ("GRID", (0, 0), (-1, -1), 0.4, T.color(th["colors"]["rule"])),
                                      ("BACKGROUND", (0, 0), (-1, -1), T.color(th["colors"]["zebra"]))]))
            story += [Paragraph("Answer key", ParagraphStyle("k", parent=st_q, textColor=T.color(th["colors"]["banner_border"]))),
                      Spacer(1, 3), grid, Spacer(1, 10)]

    if explain == "end":  # every question's answer + explanation after the last section
        story.append(Banner(th, [("Answers & explanations", "b", 10.5, border),
                                 (f"{n_all} questions", "r", 7.5, muted)], pad=5, radius=7))
        story.append(Spacer(1, 6))
        run = []
        for idx, sec in zip(picked, secs):
            for k, q in enumerate(sec["questions"], first_no[idx] + 1):
                c = content(q)
                full = cols == 1 or wide(q, only_expl=True)
                w = usable_w if full else col_w
                head = Paragraph(f'<font name="{LB}" color="{colors["question_no"]}">Q{k}.</font> '
                                 f'<font name="{LSB}" color="{colors["correct"]}">Answer: {answer_text(q, c)}</font>', st_x)
                box = expl_box(q, w, head) if c.get("explanation") else None
                line = HRFlowable(width="100%", thickness=0.4, color=rule, spaceBefore=4, spaceAfter=5)
                items = [box, line] if box is not None else [Stack([Strip(head, C["correct"]), line])]
                if full:
                    if run:
                        story.append(TwoCol(run, col_w, GAP, body_h))
                        run = []
                    story += items
                else:
                    run += items
        if run:
            story.append(TwoCol(run, col_w, GAP, body_h))

    def on_page(canv, d):
        T.watermark(canv, th, PAGE_W, PAGE_H)

    def on_page_end(canv, d):
        T.furniture(canv, th, canv.getPageNumber(), None, PAGE_W, PAGE_H, MARGIN)

    # one full-width frame: 2 columns are made by ``TwoCol`` runs, so a wide question can span the page
    frames_first = frames_later = [Frame(MARGIN, MARGIN + 6, usable_w, body_h, 0, 0, 0, 0, id="c")]

    d = BaseDocTemplate(out_path, pagesize=(PAGE_W, PAGE_H), leftMargin=MARGIN, rightMargin=MARGIN, topMargin=MARGIN,
            bottomMargin=MARGIN, title=ctx.get("title") or "", author=th.get("branding") or "", pageCompression=1)
    d.addPageTemplates([PageTemplate("first", frames_first, onPage=on_page, onPageEnd=on_page_end),
                        PageTemplate("later", frames_later, onPage=on_page, onPageEnd=on_page_end)])
    try:
        d.build(story)
    finally:
        images.close()
    return {"pages": d.page, "questions": n_q, "columns": cols, "wide": n_wide, "ms": int((time.perf_counter() - t0) * 1000)}
