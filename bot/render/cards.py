"""PNG cards rendered with PyMuPDF (D-001): scorecard now; quiz cards in P5 (spec §15.3, §15.5, §15.7).

Depends on: PyMuPDF (lazy import), ``render.fonts``. Used by: exam pipeline (scorecard), quiz builder (P5).
Pure functions (dict in, file out) so they run unchanged in pool workers.

Public API
----------
- ``render_scorecard(ctx, out_path, scale=2.0) -> {"w", "h", "ms"}`` — ``ctx`` keys: exam_name, meta_line,
  candidate_line, sections [{name, right, wrong, unattempted, marks, max_marks}], right, wrong,
  unattempted, accuracy, marks, max_marks, marking, branding, colors (optional overrides).

Layout ("glass banner" theme, spec §15.3): white card · banner with bold rounded border, translucent
light-grey fill, text in the border colour · stat chips · section table with banner-coloured header ·
marking line · branding. Only fields that exist are drawn. Width 540 pt × scale 2 = 1080 px.

Exceptions → outcome: ``RenderError`` if the content cannot fit (never silently shrinks text).
"""

from __future__ import annotations

import html
import time

from bot.core.errors import RenderError
from bot.render import fonts

W = 540.0
M = 18.0
DEFAULT_COLORS = {"banner_border": "#000000", "banner_fill": "#eeeeee", "right": "#2e7d32", "wrong": "#c62828",
                  "na": "#616161", "accent": "#1a237e", "text": "#111111", "muted": "#666666"}


def _rgb(hex_color: str) -> tuple[float, float, float]:
    h = hex_color.lstrip("#")
    return tuple(int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))  # type: ignore[return-value]


def _e(s) -> str:
    return html.escape(str(s if s is not None else ""))


def _fmt(x: float) -> str:
    return f"{x:.2f}".rstrip("0").rstrip(".") if isinstance(x, float) else str(x)


def _box(page, html_text: str, rect, css: str) -> float:
    """Insert HTML into ``rect`` without scaling; returns the height used."""
    spare, _scale = page.insert_htmlbox(rect, html_text, css=css, archive=fonts.archive(), scale_low=1)
    if spare < 0:
        raise RenderError("render_error", "content does not fit")
    return rect.height - spare


def _rounded(page, rect, *, stroke: str | None, fill: str | None, width: float = 2.0, radius_pt: float = 10.0,
             fill_opacity: float = 1.0) -> None:
    import pymupdf
    r = pymupdf.Rect(rect)
    rad = min(0.5, radius_pt / max(1.0, min(r.width, r.height)))
    page.draw_rect(r, color=_rgb(stroke) if stroke else None, fill=_rgb(fill) if fill else None, width=width,
                   radius=rad, fill_opacity=fill_opacity, overlay=False)


def render_scorecard(ctx: dict, out_path: str, scale: float = 2.0) -> dict:
    import pymupdf

    t0 = time.perf_counter()
    c = {**DEFAULT_COLORS, **{k: v for k, v in (ctx.get("colors") or {}).items() if v}}  # "" = default (B-028)
    border = c["banner_border"]
    css = fonts.htmlbox_css() + f"""
      body {{ color: {c['text']}; }}
      .title {{ font-size: 19px; font-weight: bold; color: {border}; }}
      .meta {{ font-size: 12.5px; font-weight: 600; color: {border}; margin-top: 3px; }}
      .cand {{ font-size: 12.5px; color: {c['muted']}; }}
      .num {{ font-size: 22px; font-weight: bold; text-align: center; }}
      .lbl {{ font-size: 10px; font-weight: 600; text-align: center; color: {c['muted']}; }}
      table {{ width: 100%; border-collapse: collapse; font-size: 11.5px; }}
      th {{ background-color: {c['banner_fill']}; color: {border}; font-weight: bold; padding: 5px 4px;
            border-bottom: 1.5px solid {border}; text-align: center; }}
      th.l, td.l {{ text-align: left; }}
      td {{ padding: 5px 4px; text-align: center; border-bottom: 0.6px solid #d0d0d0; }}
      tr.alt td {{ background-color: #f7f7f9; }}
      tr.tot td {{ font-weight: bold; border-top: 1.5px solid {border}; border-bottom: none; }}
      .g {{ color: {c['right']}; }} .r {{ color: {c['wrong']}; }} .n {{ color: {c['na']}; }}
      .foot {{ font-size: 10.5px; color: {c['muted']}; }}
      .brand {{ font-size: 11px; font-weight: bold; color: {c['accent']}; text-align: right; }}
    """
    doc = pymupdf.open()
    page = doc.new_page(width=W, height=3000)
    y = M

    # banner
    parts = [f'<div class="title">{_e(ctx.get("exam_name") or "Scorecard")}</div>']
    if ctx.get("meta_line"):
        parts.append(f'<div class="meta">{_e(ctx["meta_line"])}</div>')
    h = _box(page, "".join(parts), pymupdf.Rect(M + 14, y + 11, W - M - 14, y + 400), css)
    _rounded(page, (M, y, W - M, y + h + 22), stroke=border, fill=c["banner_fill"], width=2.2, radius_pt=12, fill_opacity=0.6)
    y += h + 22 + 10

    if ctx.get("candidate_line"):
        y += _box(page, f'<div class="cand">{_e(ctx["candidate_line"])}</div>', pymupdf.Rect(M + 4, y, W - M, y + 60), css) + 8

    # chips
    chips = [("Right", ctx.get("right", 0), c["right"]), ("Wrong", ctx.get("wrong", 0), c["wrong"]),
             ("Not attempted", ctx.get("unattempted", 0), c["na"]), ("Accuracy", f'{ctx.get("accuracy", 0)}%', c["accent"])]
    gap = 8.0
    cw = (W - 2 * M - gap * (len(chips) - 1)) / len(chips)
    chip_h = 0.0
    for i, (label, val, col) in enumerate(chips):
        x0 = M + i * (cw + gap)
        used = _box(page, f'<div class="num" style="color:{col}">{_e(val)}</div><div class="lbl">{_e(label)}</div>',
                    pymupdf.Rect(x0 + 4, y + 8, x0 + cw - 4, y + 120), css)
        chip_h = max(chip_h, used)
    for i, (_, _, col) in enumerate(chips):
        x0 = M + i * (cw + gap)
        _rounded(page, (x0, y, x0 + cw, y + chip_h + 16), stroke=col, fill="#ffffff", width=1.4, radius_pt=10)
    y += chip_h + 16 + 12

    # section table
    rows = []
    for i, s in enumerate(ctx.get("sections") or []):
        cls = ' class="alt"' if i % 2 else ""
        rows.append(f'<tr{cls}><td class="l">{_e(s["name"])}</td><td class="g">{s["right"]}</td><td class="r">{s["wrong"]}</td>'
                    f'<td class="n">{s["unattempted"]}</td><td><b>{_fmt(s["marks"])}</b> / {_fmt(s["max_marks"])}</td></tr>')
    rows.append(f'<tr class="tot"><td class="l">Total</td><td class="g">{ctx.get("right", 0)}</td><td class="r">{ctx.get("wrong", 0)}</td>'
                f'<td class="n">{ctx.get("unattempted", 0)}</td><td>{_fmt(ctx.get("marks", 0))} / {_fmt(ctx.get("max_marks", 0))}</td></tr>')
    table = ('<table><tr><th class="l">Section</th><th>Right</th><th>Wrong</th><th>N/A</th><th>Marks</th></tr>'
             + "".join(rows) + "</table>")
    th = _box(page, table, pymupdf.Rect(M + 10, y + 8, W - M - 10, y + 2400), css)
    _rounded(page, (M, y, W - M, y + th + 16), stroke=border, fill=None, width=1.2, radius_pt=10)
    y += th + 16 + 10

    foot = []
    if ctx.get("marking"):
        foot.append(f'<div class="foot">Marking: {_e(ctx["marking"])} per question</div>')
    if ctx.get("branding"):
        foot.append(f'<div class="brand">{_e(ctx["branding"])}</div>')
    if foot:
        y += _box(page, "".join(foot), pymupdf.Rect(M + 4, y, W - M - 4, y + 80), css) + 4
    y += M

    clip = pymupdf.Rect(0, 0, W, y)
    pix = page.get_pixmap(matrix=pymupdf.Matrix(scale, scale), clip=clip, alpha=False)
    pix.save(out_path)
    doc.close()
    return {"w": pix.width, "h": pix.height, "ms": int((time.perf_counter() - t0) * 1000)}
