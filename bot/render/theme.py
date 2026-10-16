"""The shared "glass banner" PDF theme (spec §15.3, §15.4) for every ReportLab PDF — result table now, custom
exam / JSON PDFs next — so all PDFs look the same and follow ``/ps`` settings.

- ``pdf_theme(settings, branding, cache_dir) -> dict`` (``app_theme(app)`` for the running bot) — plain, picklable theme built in the bot process from settings:
  colours (``pdf.colors``), header / footer text + **clickable** links (defaults: the branding's own link, else
  ``https://t.me/<branding handle>``), watermark (``watermark.*``; text defaults to the branding; ``cache_dir`` =
  where its image is kept).
- ``web_link(value) -> str`` — a link setting as a real URL: ``@handle`` / ``t.me/x`` → ``https://t.me/…`` (owner
  2026-10-13: "branding ka hyperlink nhi chal rha hai json command vali pdf mai" — ``channel_link`` held ``@handle``,
  which is no link) · ``brand_url(settings, branding) -> str`` — **the link of what ``BRANDING`` says** (owner
  2026-10-13: "hyperlink usi ka bnaya kro jo banding mai dala gya hai": ``@NotesHub_Ssc`` → ``t.me/NotesHub_Ssc``);
  ``channel_link`` only when the branding is no link (plain words).
- Canvas helpers (run in pool workers; ReportLab imported lazily): ``register_fonts()``, ``banner(c, …)`` (bold
  border, slightly rounded corners, translucent light-grey glass fill), ``watermark(c, theme, w, h)`` (the tiled
  image of ``render.watermark``, drawn first, under the content), ``furniture(c, theme, page, pages, w, h, margin, title="")`` (header link, footer link, page
  number bottom-right).

Depends on: ``render.fonts``. Used by: ``render/results_pdf.py`` (and the P4 custom PDF builder).
"""

from __future__ import annotations

from bot.render.fonts import FILES, FONT_DIR

DEFAULT_COLORS = {"banner_border": "#000000", "banner_fill": "#eeeeee", "header": "#1a237e", "footer": "#1a237e",
                  "zebra": "#f7f7f9", "rule": "#d0d0d0", "muted": "#666666", "text": "#111111"}
_FONTS = False


def web_link(value) -> str:
    v = str(value or "").strip()
    if not v:
        return ""
    if v.startswith("@"):
        return f"https://t.me/{v[1:]}" if len(v) > 1 else ""
    low = v.lower()
    if low.startswith(("http://", "https://", "tg://", "mailto:")):
        return v
    if low.startswith(("t.me/", "telegram.me/", "www.")) or ("." in v.split("/", 1)[0] and " " not in v):
        return "https://" + v
    return ""  # not a link (plain words): no dead hyperlink


def brand_url(settings, branding: str = "") -> str:
    return web_link((branding or "").strip()) or web_link(settings.get("channel_link"))


def pdf_theme(settings, branding: str = "", cache_dir: str = "") -> dict:
    g = settings.get
    link = brand_url(settings, branding)
    wm = g("watermark") or {}
    return {
        "colors": {**DEFAULT_COLORS, **{k: v for k, v in (g("pdf.colors") or {}).items() if v}},
        "header_text": g("pdf.header_text") or branding, "header_url": web_link(g("pdf.header_url")) or link,
        "footer_text": g("pdf.footer_text") or branding, "footer_url": web_link(g("pdf.footer_url")) or link,
        "watermark": {"enabled": bool(wm.get("enabled", True)), "text": wm.get("text") or branding,
                      "position": wm.get("position") or "diagonal", "opacity": float(wm.get("opacity") or 0.08),
                      "color": wm.get("color") or "#9e9e9e", "cache_dir": str(cache_dir or "")},
        "branding": branding,
    }


def app_theme(app) -> dict:
    """``pdf_theme`` for the running bot: its settings, branding, and the watermark image cache under the download
    root."""
    return pdf_theme(app.settings, app.config.branding, app.config.download_root / "cache" / "watermark")


def register_fonts() -> None:
    """Noto Sans R / SemiBold / Bold as ``NS`` / ``NS-SB`` / ``NS-B`` (once per worker)."""
    global _FONTS
    if _FONTS:
        return
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    for name, role in (("NS", "latin"), ("NS-SB", "latin_sb"), ("NS-B", "latin_b")):
        pdfmetrics.registerFont(TTFont(name, str(FONT_DIR / FILES[role])))
    _FONTS = True


def color(hex_color: str):
    from reportlab.lib.colors import HexColor
    return HexColor(hex_color)


def banner(c, theme: dict, x: float, y: float, w: float, h: float, radius: float = 9.0) -> None:
    """Glass banner box with its bottom-left corner at (x, y)."""
    col = theme["colors"]
    c.saveState()
    c.setStrokeColor(color(col["banner_border"]))
    c.setLineWidth(2.0)
    c.setFillColor(color(col["banner_fill"]), alpha=0.6)
    c.roundRect(x, y, w, h, radius, stroke=1, fill=1)
    c.restoreState()


def watermark(c, theme: dict, w: float, h: float) -> None:
    """The tiled watermark image (``render.watermark``, made once, cached on disk) over the whole page; ReportLab
    stores it once per PDF (same file → same image object)."""
    from bot.render.watermark import watermark_file

    path = watermark_file(theme.get("watermark") or {}, w, h)
    if path is not None:
        c.drawImage(str(path), 0, 0, w, h, mask="auto")


def _link_text(c, x: float, y: float, text: str, url: str, font: str, size: float, align: str) -> None:
    from reportlab.pdfbase.pdfmetrics import stringWidth
    tw = stringWidth(text, font, size)
    x0 = x - tw if align == "r" else (x - tw / 2 if align == "c" else x)
    c.drawString(x0, y, text)
    if url:
        c.linkURL(url, (x0, y - 2, x0 + tw, y + size), relative=0)


def furniture(c, theme: dict, page: int, pages: int | None, w: float, h: float, margin: float, title: str = "") -> None:
    """Header link (top centre), footer link (bottom left), title (bottom centre), ``n / N`` (or ``n`` when the
    total is unknown — platypus documents) bottom right."""
    col = theme["colors"]
    c.saveState()
    if theme.get("header_text"):
        c.setFont("NS-SB", 8)
        c.setFillColor(color(col["header"]))
        _link_text(c, w / 2, h - margin + 9, theme["header_text"], theme.get("header_url") or "", "NS-SB", 8, "c")
    c.setFont("NS", 7.5)
    if theme.get("footer_text"):
        c.setFillColor(color(col["footer"]))
        _link_text(c, margin, margin - 15, theme["footer_text"], theme.get("footer_url") or "", "NS", 7.5, "l")
    c.setFillColor(color(col["muted"]))
    if title:
        c.drawCentredString(w / 2, margin - 15, title)
    c.setFont("NS-SB", 8)
    c.drawRightString(w - margin, margin - 15, f"{page} / {pages}" if pages else str(page))
    c.restoreState()
