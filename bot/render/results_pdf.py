"""Result PDF for admins (spec §14.5, §15.6) — ReportLab canvas, drawn row by row (no flowables), so thousands of
candidates take flat RAM and little CPU. Same "glass banner" theme as every other PDF (``render.theme``).

Runs in the process pool (kind ``render``): plain data in, a PDF file out. No Telegram / DB imports.

Layout (A4 landscape)
---------------------
- Page 1: glass banner (exam name, scope · marking · generated); **Overall** (candidates, average / max, highest,
  lowest, median, average right / wrong / skipped); **section-wise averages** (code legend); **shift-wise averages**
  (one row per date + shift, then an "All" row).
- Ranking pages: ``# · Name (lightly masked) · Roll (masked) · Date · Shift · section marks … · Total · R · W · U`` —
  the header row repeats on every page, zebra rows.
- Every page: watermark under the content, clickable header / footer links, ``n / N`` bottom-right.

Depends on: reportlab (+ ``rl_accel``), ``render.theme``. Used by: ``handlers/results`` through ``compute``.

Public API: ``render_results_pdf(ctx: dict, out_path: str) -> dict`` (``{"pages", "rows", "ms"}``). ``ctx`` keys:
``title``, ``scope``, ``marking``, ``generated``, ``sections`` (full names), ``codes``, ``stats`` (overall + shifts
as plain dicts), ``rows`` (``[name, roll, date, shift, [marks…], total, r, w, u]``), ``show_name``, ``max_marks``,
``theme`` (``render.theme.pdf_theme``).
"""

from __future__ import annotations

import time

from bot.render import theme as T

PAGE_W, PAGE_H = 842.0, 595.0  # A4 landscape (pt)
MARGIN = 30.0
ROW_H = 14.0
HEAD_H = 18.0
FOOT_H = 20.0


def _num(x) -> str:
    if x is None:
        return "–"
    return f"{x:.2f}".rstrip("0").rstrip(".") if isinstance(x, float) else str(x)


class _Pdf:
    def __init__(self, path: str, ctx: dict) -> None:
        from reportlab.pdfbase.pdfmetrics import stringWidth
        from reportlab.pdfgen.canvas import Canvas
        self.ctx = ctx
        self.theme = ctx.get("theme") or {"colors": dict(T.DEFAULT_COLORS)}
        col = self.theme["colors"]
        self.c = Canvas(path, pagesize=(PAGE_W, PAGE_H), pageCompression=1)
        self.c.setTitle(f"Results · {ctx['title']}")
        self.c.setAuthor(self.theme.get("branding") or "")
        self.sw = stringWidth
        self.border, self.fill = T.color(col["banner_border"]), T.color(col["banner_fill"])
        self.zebra, self.rule = T.color(col["zebra"]), T.color(col["rule"])
        self.text_c, self.muted = T.color(col["text"]), T.color(col["muted"])
        self.page = 0
        self.pages = 1

    # ------------------------------------------------------------ pages
    def new_page(self) -> None:
        if self.page:
            self.end_page()
        self.page += 1
        T.watermark(self.c, self.theme, PAGE_W, PAGE_H)  # under everything drawn after it

    def end_page(self) -> None:
        T.furniture(self.c, self.theme, self.page, self.pages, PAGE_W, PAGE_H, MARGIN,
                    title=self.fit(f"{self.ctx['title']} · {self.ctx['scope']}", "NS", 7.5, 330))
        self.c.showPage()

    # ------------------------------------------------------------ text
    def fit(self, text: str, font: str, size: float, width: float) -> str:
        if self.sw(text, font, size) <= width:
            return text
        while text and self.sw(text + "…", font, size) > width:
            text = text[:-1]
        return text + "…"

    def heading(self, y: float, text: str) -> float:
        self.c.setFont("NS-B", 11)
        self.c.setFillColor(self.border)
        self.c.drawString(MARGIN, y, text)
        return y - 6

    # ------------------------------------------------------------ tables
    def table(self, y: float, cols: list[tuple[str, float, str]], rows: list[list[str]], bold_last: bool = False) -> float:
        """Header in the banner colours, zebra rows; returns the y below the table."""
        c = self.c
        x0 = MARGIN
        total_w = sum(w for _, w, _ in cols)
        c.setFillColor(self.fill)
        c.rect(x0, y - HEAD_H, total_w, HEAD_H, stroke=0, fill=1)
        c.setStrokeColor(self.border)
        c.setLineWidth(1.2)
        c.line(x0, y - HEAD_H, x0 + total_w, y - HEAD_H)
        self._cells(x0, y - HEAD_H + 5.5, cols, [t for t, _, _ in cols], "NS-B", 7.5, self.border)
        y -= HEAD_H
        last = len(rows) - 1
        for i, r in enumerate(rows):
            if i % 2:
                c.setFillColor(self.zebra)
                c.rect(x0, y - ROW_H, total_w, ROW_H, stroke=0, fill=1)
            if bold_last and i == last:
                c.setStrokeColor(self.border)
                c.setLineWidth(1.0)
                c.line(x0, y, x0 + total_w, y)
            self._cells(x0, y - ROW_H + 4, cols, r, "NS-B" if (bold_last and i == last) else "NS", 8, self.text_c)
            y -= ROW_H
        c.setStrokeColor(self.rule)
        c.setLineWidth(0.6)
        c.line(x0, y, x0 + total_w, y)
        return y

    def _cells(self, x: float, y: float, cols, values, font: str, size: float, fill) -> None:
        """One text object per row (far fewer PDF operators and Python calls than one drawString per cell)."""
        to = self.c.beginText()
        to.setFont(font, size)
        to.setFillColor(fill)
        sw = self.sw
        for (_title, w, align), v in zip(cols, values):
            s, room = str(v), w - 8
            tw = sw(s, font, size)
            if tw > room:
                s = self.fit(s, font, size, room)
                tw = sw(s, font, size)
            dx = 4 if align == "l" else (w - 4 - tw if align == "r" else (w - tw) / 2)
            to.setTextOrigin(x + dx, y)
            to.textOut(s)
            x += w
        self.c.drawText(to)


def _rank_cols(ctx: dict) -> list[tuple[str, float, str]]:
    fixed = [("#", 34, "r")]
    tail = [("Roll No", 82, "l"), ("Date", 62, "c"), ("Shift", 40, "c")]
    tail += [(c, 44, "r") for c in ctx["codes"]]
    tail += [("Total", 50, "r"), ("R", 28, "r"), ("W", 28, "r"), ("U", 28, "r")]
    used = sum(w for _, w, _ in fixed + tail)
    if ctx.get("show_name", True):
        return fixed + [("Name", max(90.0, PAGE_W - 2 * MARGIN - used), "l")] + tail
    return fixed + tail


def _shift_cols(codes: list[str]) -> list[tuple[str, float, str]]:
    cols = [("Date", 70, "c"), ("Shift", 60, "c"), ("Candidates", 66, "r"), ("Average", 60, "r"),
            ("Highest", 56, "r"), ("Lowest", 56, "r"), ("Median", 56, "r")]
    return cols + [(f"{c} avg", 50, "r") for c in codes]


def render_results_pdf(ctx: dict, out_path: str) -> dict:
    t0 = time.perf_counter()
    T.register_fonts()
    pdf = _Pdf(out_path, ctx)
    c = pdf.c
    rows, codes, sections, stats = ctx["rows"], ctx["codes"], ctx["sections"], ctx["stats"]
    top = PAGE_H - MARGIN - 4
    per_page = int((top - MARGIN - FOOT_H - HEAD_H) // ROW_H)
    pdf.pages = 1 + (len(rows) + per_page - 1) // per_page

    # ---------------------------------------------------------------- page 1: banner + summary
    pdf.new_page()
    bh = 46.0
    T.banner(c, pdf.theme, MARGIN, top - bh, PAGE_W - 2 * MARGIN, bh)
    c.setFillColor(pdf.border)
    c.setFont("NS-B", 15)
    c.drawString(MARGIN + 12, top - 20, pdf.fit(ctx["title"], "NS-B", 15, PAGE_W - 2 * MARGIN - 24))
    meta = " · ".join(x for x in (ctx.get("scope"), f"Marking {ctx['marking']}" if ctx.get("marking") else "",
                                  f"Generated {ctx['generated']}" if ctx.get("generated") else "") if x)
    c.setFont("NS-SB", 9)
    c.drawString(MARGIN + 12, top - 36, pdf.fit(meta, "NS-SB", 9, PAGE_W - 2 * MARGIN - 24))
    y = top - bh - 20

    o = stats["overall"]
    mx = ctx.get("max_marks") or 0
    y = pdf.heading(y, "Overall")
    cols = [("Candidates", 80, "r"), ("Average", 90, "r"), ("Highest", 70, "r"), ("Lowest", 70, "r"), ("Median", 70, "r"),
            ("Avg right", 70, "r"), ("Avg wrong", 70, "r"), ("Avg skipped", 76, "r")]
    avg = f"{_num(o['avg'])} / {_num(mx)}" if mx else _num(o["avg"])
    y = pdf.table(y, cols, [[str(o["n"]), avg, _num(o["high"]), _num(o["low"]), _num(o["median"]),
                             _num(o["right"]), _num(o["wrong"]), _num(o["unattempted"])]]) - 18

    y = pdf.heading(y, "Section-wise average")
    y = pdf.table(y, [("Code", 44, "c"), ("Section", 430, "l"), ("Average", 70, "r")],
                  [[cd, n, _num(a)] for cd, n, a in zip(codes, sections, o["sections"])]) - 18

    shift_rows = [[s["date"], s["shift"], str(s["n"]), _num(s["avg"]), _num(s["high"]), _num(s["low"]), _num(s["median"]),
                   *[_num(a) for a in s["sections"]]] for s in stats["shifts"]]
    if shift_rows:
        y = pdf.heading(y, "Shift-wise average")
        scols = _shift_cols(codes)
        overall_row = ["All", "–", str(o["n"]), _num(o["avg"]), _num(o["high"]), _num(o["low"]), _num(o["median"]),
                       *[_num(a) for a in o["sections"]]]
        all_rows = shift_rows + [overall_row]
        while all_rows:  # the shift table may run over pages (many shifts)
            fit = int((y - MARGIN - FOOT_H - HEAD_H) // ROW_H)
            if fit < 2:  # no room left on this page for a header + rows
                pdf.pages += 1
                pdf.new_page()
                y = top
                continue
            chunk, all_rows = all_rows[:fit], all_rows[fit:]
            y = pdf.table(y, scols, chunk, bold_last=not all_rows)
            if all_rows:
                pdf.pages += 1
                pdf.new_page()
                y = top

    # ---------------------------------------------------------------- ranking pages
    rcols = _rank_cols(ctx)
    show_name = ctx.get("show_name", True)
    for start in range(0, len(rows), per_page):
        pdf.new_page()
        page_rows = []
        for i, r in enumerate(rows[start:start + per_page], start + 1):
            name, roll, date, shift, marks, total, right, wrong, unatt = r
            vals = [str(i)] + ([name] if show_name else []) + [roll, date, shift]
            page_rows.append(vals + [_num(m) for m in marks] + [_num(total), str(right), str(wrong), str(unatt)])
        pdf.table(top, rcols, page_rows)
    pdf.end_page()
    c.save()
    return {"pages": pdf.page, "rows": len(rows), "ms": int((time.perf_counter() - t0) * 1000)}
