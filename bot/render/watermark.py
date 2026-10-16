"""Page watermark as one transparent PNG (owner 2026-10-13): the ``watermark`` text from ``/ps`` settings, **once,
in the middle of the page**, along the page's own diagonal (``position: diagonal``, ≈ 55° on a portrait page),
upright (``vertical``) or level (anything else); about 70 % of that line long; in the configured colour with the
configured opacity baked into the alpha channel.

The image is made **once** and kept on disk; its file name carries a hash of everything it depends on (text,
colour, opacity, position, pixel size, style version), so a settings change gives a new name → a new image, and
the old one for that page size is deleted. Inside a worker the bytes are also memoised, so a PDF (and every later
PDF of that worker) reads the file at most once. Writes go through a temp file + ``os.replace`` — two workers
making the same image at once cannot leave a half-written file.

Public API: ``watermark_file(wm, w_pt, h_pt, cache_dir=None) -> Path | None`` and ``watermark_png(...) -> bytes |
None`` (None when the watermark is off or has no text). ``wm`` is ``pdf_theme(...)["watermark"]``; ``cache_dir``
defaults to ``wm["cache_dir"]``, then the system temp folder.

Depends on: Pillow, ``render.fonts``. Used by: ``render/original_pdf.py`` (PyMuPDF), ``render/theme.watermark``
(ReportLab PDFs).
"""

from __future__ import annotations

import hashlib
import io
import math
import os
import tempfile
from pathlib import Path

from bot.render.fonts import FILES, FONT_DIR

DPI = 110            # sharp enough at 2–30 % opacity; ≈ 15–30 KB per page size
STYLE = 2            # bump when the drawing below changes → every cached image is made again
_MEMO: dict[str, bytes] = {}


def _rgb(c: str) -> tuple[int, int, int]:
    c = (c or "#9e9e9e").lstrip("#")
    if len(c) == 3:
        c = "".join(ch * 2 for ch in c)
    try:
        return int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16)
    except ValueError:
        return 158, 158, 158


def _angle(position: str, w: int, h: int) -> float:
    """``diagonal`` → along the page's own diagonal (≈ 55° on a portrait page), ``vertical`` → 90°, else level."""
    if position == "vertical":
        return 90.0
    if position == "diagonal":
        return math.degrees(math.atan2(h, w))
    return 0.0


def _draw(text: str, rgb: tuple[int, int, int], alpha: int, position: str, w: int, h: int) -> bytes:
    from PIL import Image, ImageDraw, ImageFont

    angle = _angle(position, w, h)
    span = {90.0: h, 0.0: w}.get(angle, math.hypot(w, h)) * 0.7   # the text runs over ~70 % of its line
    probe = ImageFont.truetype(str(FONT_DIR / FILES["latin_b"]), size=100)
    size = max(12, min(round(100 * span / max(1, probe.getlength(text))), round(w / 6)))
    font = ImageFont.truetype(str(FONT_DIR / FILES["latin_b"]), size=size)
    left, top, right, bottom = font.getbbox(text)
    mask = Image.new("L", (right - left + 8, bottom - top + 8), 0)  # text shape; colour + opacity applied after
    ImageDraw.Draw(mask).text((4 - left, 4 - top), text, font=font, fill=255)
    mask = mask.rotate(angle, resample=Image.BICUBIC, expand=True)
    mask = mask.point(lambda v: v * alpha // 255)                  # opacity baked in, never above the setting
    page = Image.new("RGBA", (w, h), (*rgb, 0))
    layer = Image.new("RGBA", mask.size, (*rgb, 0))
    layer.putalpha(mask)
    page.alpha_composite(layer, ((w - mask.width) // 2, (h - mask.height) // 2))
    buf = io.BytesIO()
    page.save(buf, "PNG", optimize=True)
    return buf.getvalue()


def watermark_file(wm: dict, w_pt: float, h_pt: float, cache_dir: str | os.PathLike | None = None) -> Path | None:
    """Path of the cached image (made now when missing); None when the watermark is off or has no text."""
    text = (wm or {}).get("text") or ""
    if not wm or not wm.get("enabled") or not text.strip():
        return None
    w, h = round(w_pt / 72 * DPI), round(h_pt / 72 * DPI)
    rgb = _rgb(wm.get("color"))
    alpha = round(255 * max(0.02, min(0.5, float(wm.get("opacity") or 0.08))))
    position = wm.get("position") or "diagonal"
    key = hashlib.sha1(repr((STYLE, text, rgb, alpha, position, w, h)).encode()).hexdigest()[:16]
    name = f"wm-{w}x{h}-{key}.png"
    for folder in (cache_dir or wm.get("cache_dir"), Path(tempfile.gettempdir()) / "rvsh-watermark"):
        if not folder:
            continue
        folder, path = Path(folder), Path(folder) / name
        if path.is_file():
            return path
        try:
            folder.mkdir(parents=True, exist_ok=True)
            tmp = folder / f".{name}.{os.getpid()}.tmp"
            tmp.write_bytes(_draw(text, rgb, alpha, position, w, h))
            os.replace(tmp, path)
        except OSError:  # read-only folder: try the next one
            continue
        for old in folder.glob(f"wm-{w}x{h}-*.png"):  # the settings changed: the old image is not needed any more
            if old.name != name:
                old.unlink(missing_ok=True)
        return path
    return None


def watermark_png(wm: dict, w_pt: float, h_pt: float, cache_dir: str | os.PathLike | None = None) -> bytes | None:
    """The image bytes (memoised per worker)."""
    path = watermark_file(wm, w_pt, h_pt, cache_dir)
    if path is None:
        return None
    key = str(path)
    if key not in _MEMO:
        _MEMO[key] = path.read_bytes()
    return _MEMO[key]
