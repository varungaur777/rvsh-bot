"""Question-image analysis + enhancement before the exam JSON is built (spec §12.5 step 3; owner 2026-10-12: "phle
analyse kro … text / diagram / math, us hisab se set karo; light hai to enhance karo; dynamic, fast").

Pool job helper (Pillow + numpy only; no Telegram / DB). One call per image, a few ms each.

``process(data, mime) -> (bytes, mime, info)``
1. **Flatten** transparency on white (PNG with alpha would turn black), grey when the image has no real colour.
2. **Crop** to the content (+3 px); blank images are kept as they are.
3. **Enhance only when needed** — faint/washed-out text (darkest ink still light) → levels stretch + gamma; small
   text (cap height < 9 px) → LANCZOS upscale ×2–3 + gentle unsharp mask; soft/blurred text → gentle unsharp mask.
   Readable images are never touched.
4. **Classify** from the row/column ink profile: ``text`` (regular text lines), ``math`` (a fraction bar between
   lines, or lines of mixed heights — super/subscripts), ``diagram`` (shapes / pictures, maybe with labels),
   ``blank``.
5. **Measure the text size** — per text line, top to baseline (≈ cap height, robust to capitals-only words and
   Devanagari) → ``text_px``. The renderer scales every image by ``scale`` = pt per pixel so that image text has the
   same size as the PDF's own text (question images and option images become proportionate) — diagrams keep their
   natural size (labels, if any, bound it).
6. **Reflow data** — word gaps per text line (``lines``: ``[[y0, y1, [cut_x…]], …]``) so a renderer can wrap a strip
   that is wider than its column instead of shrinking it. **Never inside a table** (``table_band``: box/grid lines,
   or columns lined up across rows — owner 2026-10-13: a 3×3 number matrix was cut into pieces): the renderer wraps
   the text above/below and keeps the table whole.

``info``: ``{kind, w, h, text_px, scale, lines, enhanced: [...], src_w, src_h, ms}``.
``cut_off`` / ``repair_cut_off``: a sheet image saved with a broken download (no end marker) → the part that is
left, the decoder's grey fill turned white (the exam bundle and the Original PDF).
Encoding: grey text → PNG (lossless, small); untouched originals keep their bytes; changed colour images → JPEG 92
(from JPEG) or PNG.
"""

from __future__ import annotations

import io
import time

TARGET_CAP_PT = 6.8  # cap height of the PDFs' 9.5 pt Noto Sans body text
INK = 150            # grey level below which a pixel is ink (after enhancement)
PAPER = 245          # lighter than this is background


def cut_off(data: bytes, mime: str = "") -> bool:
    """A cut-off PNG / GIF / JPEG (no end marker). Checked on the bytes: WeasyPrint sets Pillow's
    ``LOAD_TRUNCATED_IMAGES`` globally, so decoding would not complain."""
    if mime == "image/png" or data[:8] == b"\x89PNG\r\n\x1a\n":
        return b"IEND" not in data[-16:]
    if mime == "image/gif" or data[:4] == b"GIF8":
        return not data.rstrip(b"\0").endswith(b";")
    if mime == "image/jpeg" or data[:2] == b"\xff\xd8":
        return not data.rstrip(b"\0\r\n ").endswith(b"\xff\xd9")
    return False


_JPEG_FILL = (128, 128, 128)  # what the JPEG decoder paints where a cut-off file has no data


def repair_cut_off(data: bytes) -> bytes | None:
    """The readable part of a cut-off image, the rest white instead of the decoder's grey (or black) block — as a
    PNG; None when not even the header is there or nothing readable is left. A sheet image is text on white, so its
    lost lower part simply looks empty."""
    from PIL import Image, ImageFile

    keep = ImageFile.LOAD_TRUNCATED_IMAGES
    ImageFile.LOAD_TRUNCATED_IMAGES = True
    try:
        im = Image.open(io.BytesIO(data))
        im.load()
        im = im.convert("RGB")
    except Exception:
        return None
    finally:
        ImageFile.LOAD_TRUNCATED_IMAGES = keep
    w, h = im.size
    px = im.load()
    fills = {_JPEG_FILL, (0, 0, 0)}

    def trailing(y: int) -> int:  # length of the fill-coloured run at the end of row y
        last = px[w - 1, y]
        if last not in fills:
            return 0
        x = w - 1
        while x >= 0 and px[x, y] == last:
            x -= 1
        return w - 1 - x

    y = h - 1
    while y >= 0 and trailing(y) == w:  # rows with no data at all
        y -= 1
    top = y
    while top >= 0 and top > y - 16 and trailing(top) > 0:  # the last, partly decoded block row (≤ 16 px)
        top -= 1
    white = (255, 255, 255)
    for row in range(top + 1, h):
        run = trailing(row)
        for x in range(w - run, w):
            px[x, row] = white
    if im.convert("L").point(lambda v: 255 if v < PAPER else 0).getbbox() is None:
        return None  # nothing readable is left
    out = io.BytesIO()
    im.save(out, "PNG", optimize=True)
    return out.getvalue()


def _open(data: bytes):
    """→ (image in RGB or L, had_transparency)."""
    from PIL import Image

    im = Image.open(io.BytesIO(data))
    im.load()
    if im.mode in ("RGBA", "LA", "PA") or (im.mode == "P" and "transparency" in im.info):
        im = im.convert("RGBA")
        bg = Image.new("RGBA", im.size, (255, 255, 255, 255))
        bg.alpha_composite(im)
        return bg.convert("RGB"), True
    if im.mode not in ("RGB", "L"):
        im = im.convert("RGB")
    return im, False


def _colourful(im) -> bool:
    import numpy as np

    if im.mode == "L":
        return False
    a = np.asarray(im.reduce(2) if min(im.size) > 40 else im, dtype=np.int16)
    spread = a.max(axis=2) - a.min(axis=2)
    return float((spread > 40).mean()) > 0.01


def _runs(mask_1d) -> list[tuple[int, int]]:
    import numpy as np

    m = np.concatenate(([False], mask_1d, [False]))
    d = np.diff(m.astype(np.int8))
    return list(zip(np.flatnonzero(d == 1).tolist(), np.flatnonzero(d == -1).tolist()))


def _lines(g) -> list[dict]:
    """Text-line candidates: row runs of ink with their ascent (top → baseline), segments and word gaps."""
    ink = g < INK
    rows = ink.sum(1)
    out = []
    for y0, y1 in _runs(rows > 0):
        h = y1 - y0
        prof = rows[y0:y1]
        peak = int(prof.max())
        thr = 0.2 * peak
        idx = [i for i, v in enumerate(prof.tolist()) if v >= thr]
        base = y0 + idx[-1] + 1
        top = y0 + next(i for i, v in enumerate(prof.tolist()) if v >= thr * 0.25)
        cols = ink[y0:y1].any(0)
        segs = _runs(cols)
        out.append({"y0": y0, "y1": y1, "h": h, "ascent": max(1, base - top), "segs": segs,
                    "width": (segs[-1][1] - segs[0][0]) if segs else 0, "fill": float(ink[y0:y1].mean())})
    return out


def _is_text_line(ln: dict) -> bool:
    """6–70 px tall, moderate fill (not a solid bar / picture), and either several glyph segments or a short run —
    a single digit (``4``) or a Devanagari word (its headline joins the letters into one segment)."""
    return 6 <= ln["h"] <= 70 and ln["fill"] < 0.45 and (len(ln["segs"]) >= 2 or ln["width"] <= 8 * ln["h"])


def table_band(g) -> tuple[int, int] | None:
    """Rows ``(top, bottom)`` of a table inside an image (``g``: grey array), or None. A table is: vertical rules
    taller than two text lines (cell borders, often light grey — 187), or text lines in a row that share an empty
    column gap wider than 2× the text height between their words (columns without borders). Wrapping a table at word
    gaps would scatter its cells (owner 2026-10-13: a 3×3 number matrix was cut into pieces)."""
    import numpy as np

    ink = g < INK
    lines = [ln for ln in _lines(g) if _is_text_line(ln)]
    line_h = float(np.median([ln["h"] for ln in lines])) if lines else 12.0
    # 1. vertical rules: columns whose longest run of non-paper pixels is taller than two text lines
    col = (g < 225).astype(np.int8)
    if col.shape[0] >= 3 * line_h:
        run = np.zeros(col.shape[1], dtype=np.int32)
        best = np.zeros(col.shape[1], dtype=np.int32)
        for row in col:
            run = (run + 1) * row
            np.maximum(best, run, out=best)
        rules = np.flatnonzero(best > 2.2 * line_h)
        if len(rules) >= 2:
            a, b = max(_runs(col[:, rules].sum(1) >= 2), key=lambda r: r[1] - r[0])  # the table: longest stretch
            return int(a), int(b)
    # 2. borderless columns: neighbouring lines sharing an empty x-range with words on both sides of it
    band = None
    for a, b in zip(lines, lines[1:]):
        if len(a["segs"]) < 2 or len(b["segs"]) < 2:
            continue
        x0, x1 = max(a["segs"][0][0], b["segs"][0][0]), min(a["segs"][-1][1], b["segs"][-1][1])
        if x1 <= x0:
            continue
        empty = ~(ink[a["y0"]:a["y1"], x0:x1].any(0) | ink[b["y0"]:b["y1"], x0:x1].any(0))
        min_gap = 2 * max(a["ascent"], b["ascent"])
        if any(e - s0 >= min_gap for s0, e in _runs(empty)):
            band = (a["y0"] if band is None else band[0], b["y1"])
    return band


def tabular(g) -> bool:
    return table_band(g) is not None


def _is_rule(ln: dict, width: int) -> bool:
    return ln["h"] <= 3 and ln["width"] >= 0.5 * width and len(ln["segs"]) <= 2


def _text_px(text: list[dict]) -> float:
    """Body text size: the lower median ascent of the long lines (a short line holding a fraction or a formula is
    taller and must not decide the size)."""
    if not text:
        return 0.0
    widest = max(ln["width"] for ln in text)
    asc = sorted(ln["ascent"] for ln in text if ln["width"] >= 0.5 * widest)
    return float(asc[(len(asc) - 1) // 2])


def _sharpness(g) -> float:
    import numpy as np

    a = g.astype(np.float32)
    lap = a[1:-1, 1:-1] * 4 - a[:-2, 1:-1] - a[2:, 1:-1] - a[1:-1, :-2] - a[1:-1, 2:]
    return float(lap.var()) if lap.size else 0.0


def _cuts(ln: dict) -> list[int]:
    """Word gaps (centres) of a text line — gaps at least ~0.35 × cap height wide."""
    min_gap = max(3, int(0.35 * ln["ascent"]))
    segs = ln["segs"]
    return [(a_end + b_start) // 2 for (_a0, a_end), (b_start, _b1) in zip(segs, segs[1:]) if b_start - a_end >= min_gap]


def process(data: bytes, mime: str = "") -> tuple[bytes, str, dict]:
    import numpy as np
    from PIL import Image, ImageFilter

    t0 = time.perf_counter()
    im, alpha = _open(data)
    src_w, src_h = im.size
    colour = _colourful(im)
    if not colour and im.mode != "L":
        im = im.convert("L")
    g = np.asarray(im if im.mode == "L" else im.convert("L"))
    enhanced: list[str] = ["flattened"] if alpha else []  # transparent PNGs turn black in some viewers

    # 1. crop to content
    ink_any = g < PAPER
    if not ink_any.any():
        return data, mime, {"kind": "blank", "w": src_w, "h": src_h, "text_px": 0, "scale": 0.75, "lines": [],
                            "enhanced": [], "src_w": src_w, "src_h": src_h, "ms": 0}
    ys, xs = np.flatnonzero(ink_any.any(1)), np.flatnonzero(ink_any.any(0))
    box = (max(0, xs[0] - 3), max(0, ys[0] - 3), min(src_w, xs[-1] + 4), min(src_h, ys[-1] + 4))
    if box != (0, 0, src_w, src_h):
        im = im.crop(box)
        g = g[box[1]:box[3], box[0]:box[2]]
        enhanced.append("cropped")

    # 2. faint ink → levels + gamma (only when the darkest ink is still light)
    dark = float(np.percentile(g[g < PAPER], 5)) if (g < PAPER).any() else 0.0
    if dark > 110:
        lo = float(np.percentile(g, 0.5))
        lut = [max(0, min(255, int(round(255 * (max(0.0, (v - lo)) / max(1.0, 255 - lo)) ** 1.6)))) for v in range(256)]
        im = im.point(lut * (3 if im.mode == "RGB" else 1))
        g = np.asarray(im if im.mode == "L" else im.convert("L"))
        enhanced.append(f"contrast(dark {dark:.0f})")

    # 3. structure
    h, w = g.shape
    lines = _lines(g)
    text = [ln for ln in lines if _is_text_line(ln)]
    rules = [ln for ln in lines if _is_rule(ln, w)]
    text_rows = sum(ln["h"] for ln in text)
    ink_rows = sum(ln["h"] for ln in lines) or 1
    if not text:
        kind = "diagram"
    elif text_rows / ink_rows < 0.6:
        kind = "diagram"  # pictures / shapes with a few labels
    elif rules and any(text[0]["y0"] < r["y0"] < text[-1]["y1"] for r in rules):
        kind = "math"  # a fraction bar between text lines
    elif len(text) >= 2 and max(ln["ascent"] for ln in text) > 1.6 * min(ln["ascent"] for ln in text):
        kind = "math"  # super/subscripts, stacked expressions
    else:
        kind = "text"
    text_px = _text_px(text)
    band = table_band(g) if kind == "text" else None  # a table is kept whole; only the text around it is wrapped

    # 4. small or soft text → upscale / sharpen (diagrams are left alone)
    if kind in ("text", "math") and text_px:
        f = 3 if text_px < 6 else (2 if text_px < 9 else 1)
        if f > 1:
            im = im.resize((im.width * f, im.height * f), Image.LANCZOS)
            im = im.filter(ImageFilter.UnsharpMask(radius=1.2, percent=70, threshold=3))
            enhanced.append(f"upscaled x{f}")
            lines = [{**ln, "y0": ln["y0"] * f, "y1": ln["y1"] * f, "ascent": ln["ascent"] * f,
                      "segs": [(a * f, b * f) for a, b in ln["segs"]]} for ln in lines]
            text = [ln for ln in lines if _is_text_line({**ln, "h": ln["h"]})]
            text_px *= f
            band = (band[0] * f, band[1] * f) if band else None
        elif _sharpness(g) < 120:
            im = im.filter(ImageFilter.UnsharpMask(radius=1.0, percent=60, threshold=3))
            enhanced.append("sharpened")

    # 5. display scale (pt per px)
    if kind == "text" and text_px:
        scale = TARGET_CAP_PT / text_px
    elif kind == "math" and text_px:
        scale = 0.85 * TARGET_CAP_PT / text_px  # fractions / scripts are typeset smaller than body text
    elif kind == "diagram" and text_px:
        scale = max(0.45, min(1.1, TARGET_CAP_PT / text_px))  # labels bound the size
    else:
        scale = 0.75
    wrap = [ln for ln in lines if _is_text_line(ln) and not (band and band[0] <= ln["y0"] < band[1])]
    reflow = [[ln["y0"], ln["y1"], _cuts(ln)] for ln in wrap] if kind == "text" and len(wrap) <= 8 else []

    # 6. encode — grey text: near-white → white and 16 grey levels (cleaner edges, PNG ~2–4× smaller than the noise)
    if kind in ("text", "math") and im.mode == "L" and enhanced:
        im = im.point([255 if v >= 228 else (v // 16) * 17 for v in range(256)])
    if not enhanced:
        out, out_mime = data, mime  # untouched original: no re-encoding
    else:
        buf = io.BytesIO()
        if im.mode == "L" or not mime.endswith("jpeg"):
            im.save(buf, "PNG", optimize=False, compress_level=6)
            out_mime = "image/png"
        else:
            im.convert("RGB").save(buf, "JPEG", quality=92)
            out_mime = "image/jpeg"
        out = buf.getvalue()
    return out, out_mime, {"kind": kind, "w": im.width, "h": im.height, "text_px": round(text_px, 1),
                           "scale": round(scale, 4), "lines": reflow, "enhanced": enhanced, "src_w": src_w,
                           "src_h": src_h, "ms": round((time.perf_counter() - t0) * 1000, 1)}
