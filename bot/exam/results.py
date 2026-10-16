"""Candidate results (spec §9.1 ``exam_results``, user ``exam_db``, §14.5 result PDF).

Depends on: ``db.repo``, ``exam.keys``, ``exam.marks``. Used by: exam pipeline (save), ``/results`` handler
(catalogue, rows, statistics), ``render.results_pdf`` (table data).

Public API
----------
- ``await save_result(repo, data, score, user_id) -> str | None`` — upserts one document per
  ``exam_key|roll_no`` (same roll again → updated, never duplicated; the sender is added to ``submitted_by``),
  keeps the per-shift catalogue entry in ``exams`` (meta + sections + ``results`` count) and adds the reference
  to the user's ``exam_db``. Returns the result id, or ``None`` when the sheet has no roll number.
  **Stored unmasked** (owner 2026-10-12: name / roll are masked only where they are shown to users).
- ``await rebuild_catalogue(repo) -> int`` — catalogue from stored results (boot backfill, ``tools/import_results``).
- ``await catalogue(repo, query="") -> list[ExamGroup]`` — exams that have results, grouped by exam name, newest
  first; each group lists its shifts (one ``exams`` entry per ``exam_key``).
- ``await collect(repo, shifts, marking, show_name=True) -> ResultTable`` — streams the candidates of the chosen
  shifts (projection; one compact row per candidate, never whole documents), **recomputes marks with the chosen
  marking** (``{section: (plus, negative fraction)}``) and ranks them (total ↓ → fewer wrong → first-section marks ↓).
- ``ResultTable.stats()`` — overall and **shift-wise averages** (candidates, average, highest, lowest, median,
  per-section averages, R/W/U averages). Pure; unit-tested.
- ``section_codes(names)`` — short column labels (``A``, ``B`` … from "PART-A (…)", else ``S1``, ``S2`` …).

Stored: candidate info, exam info, per-section right/wrong/unattempted/dropped + marks under the user's marking.
**Never** questions/options/answers.
"""

from __future__ import annotations

import re
import statistics
from dataclasses import dataclass, field

from bot.exam.keys import exam_key, name_slug
from bot.exam.marks import Score, negative_from_fraction, scheme_text
from bot.parsers.models import ExamData

PROJECTION = {"roll_no": 1, "candidate.name": 1, "exam.date": 1, "exam.shift": 1, "exam.time": 1,
              "exam.exam_key": 1, "sections": 1}


# ------------------------------------------------------------------ save
async def save_result(repo, data: ExamData, score: Score, user_id: int) -> str | None:
    roll = (data.candidate.roll_no or "").strip()
    if not roll:
        return None
    key = exam_key(data.meta)
    rid = f"{key}|{roll}"
    m = data.meta
    exam = {"exam_key": key, "exam_name": m.exam_name, "exam_name_slug": name_slug(m.exam_name),
            "level": m.level, "date": m.date, "shift": m.shift, "time": m.time, "start_hhmm": m.start_hhmm,
            "source": m.source}
    is_new = await repo.exam_results.get(rid, copy=False) is None
    await repo.exam_results.update(rid, {
        "$set": {
            "roll_no": roll,
            "candidate": {"name": data.candidate.name or "", "other": data.candidate.other},
            "exam": exam,
            # a list (ordered, and section names may contain dots — not safe as Mongo field names)
            "sections": [{"name": s.name, "total": s.total, "right": s.right, "wrong": s.wrong,
                          "unattempted": s.unattempted, "dropped": s.dropped, "marks": s.marks,
                          "plus": s.plus, "minus": s.minus} for s in score.sections],
            "aggregate": {"right": score.right, "wrong": score.wrong, "unattempted": score.unattempted,
                          "marks": score.marks, "max_marks": score.max_marks, "marking": score.marking_label},
        },
        "$addToSet": {"submitted_by": user_id},
    })
    cat = {"$set": {"exam": exam, "sections": [{"name": s.name, "count": s.total} for s in score.sections]}}
    if is_new:
        cat["$inc"] = {"results": 1}
    await repo.exams.update(key, cat)
    await repo.users.update(user_id, {
        "$addToSet": {f"exam_db.{key}.roll_nos": roll},
        "$set": {f"exam_db.{key}.last_total": score.marks},
    })
    return rid


async def rebuild_catalogue(repo) -> int:
    """(Re)build the per-shift ``exams`` catalogue from ``exam_results`` (results saved before the catalogue existed).
    Streams results with a small projection; returns the number of shifts written."""
    shifts: dict[str, dict] = {}
    async for d in repo.exam_results.stream({}, projection={"exam": 1, "sections": 1}):
        e = d.get("exam") or {}
        key = e.get("exam_key")
        if not key:
            continue
        x = shifts.setdefault(key, {"exam": e, "sections": [], "results": 0})
        x["results"] += 1
        for s in _doc_sections(d):
            if s["name"] not in [y["name"] for y in x["sections"]]:
                x["sections"].append({"name": s["name"], "count": s.get("total") or
                                      s.get("right", 0) + s.get("wrong", 0) + s.get("unattempted", 0) + s.get("dropped", 0)})
    for key, x in shifts.items():
        await repo.exams.update(key, {"$set": x})
    return len(shifts)


# ------------------------------------------------------------------ catalogue
@dataclass(slots=True)
class Shift:
    key: str
    date: str = ""
    shift: int | None = None
    time: str = ""
    count: int = 0

    def label(self) -> str:
        d = "-".join(reversed(self.date.split("-"))) if self.date else "?"
        return f"{d} · Shift {self.shift}" if self.shift else f"{d} · {self.time or '?'}"


@dataclass(slots=True)
class ExamGroup:
    slug: str
    name: str
    shifts: list[Shift] = field(default_factory=list)
    sections: list[str] = field(default_factory=list)

    @property
    def count(self) -> int:
        return sum(s.count for s in self.shifts)

    @property
    def last_date(self) -> str:
        return max((s.date or "" for s in self.shifts), default="")


async def catalogue(repo, query: str = "") -> list[ExamGroup]:
    q = query.strip().lower()
    docs = await repo.exams.find({"results": {"$gt": 0}}, projection={"exam": 1, "sections": 1, "results": 1})
    groups: dict[str, ExamGroup] = {}
    for d in docs:
        e = d.get("exam") or {}
        name = e.get("exam_name") or "Exam"
        if q and q not in name.lower():
            continue
        slug = e.get("exam_name_slug") or name_slug(name)
        g = groups.setdefault(slug, ExamGroup(slug, name))
        g.shifts.append(Shift(e.get("exam_key") or d["_id"], e.get("date") or "", e.get("shift"), e.get("time") or "",
                              int(d.get("results") or 0)))
        for s in d.get("sections") or []:
            if s["name"] not in g.sections:
                g.sections.append(s["name"])
    for g in groups.values():
        g.shifts.sort(key=lambda s: (s.date, _hhmm(s.time)))
    return sorted(groups.values(), key=lambda g: (g.last_date, g.name), reverse=True)


def _hhmm(t: str) -> str:
    m = re.search(r"(\d{1,2}):(\d{2})\s*([AP]M)?", t or "", re.I)
    if not m:
        return ""
    h = int(m.group(1))
    if m.group(3):
        h = h % 12 + (12 if m.group(3).upper() == "PM" else 0)
    return f"{h:02d}{m.group(2)}"


def section_codes(names: list[str]) -> list[str]:
    """Short column labels: the PART letter when every section has a distinct one, else S1, S2 …"""
    letters = [m.group(1).upper() if (m := re.match(r"\s*PART[\s\-–]*([A-Z])\b", n, re.I)) else None for n in names]
    if all(letters) and len(set(letters)) == len(letters):
        return letters
    return [f"S{i}" for i in range(1, len(names) + 1)]


# ------------------------------------------------------------------ rows + ranking
@dataclass(slots=True)
class Row:
    name: str
    roll: str
    shift_key: str
    marks: list[float | None]  # per section (None: section missing in this candidate's sheet)
    total: float
    right: int
    wrong: int
    unattempted: int


@dataclass(slots=True)
class ResultTable:
    exam_name: str
    sections: list[str]
    shifts: list[Shift]
    marking: dict[str, tuple[float, float]]  # section → (plus, negative fraction); key "" = every other section
    max_marks: float = 0.0
    rows: list[Row] = field(default_factory=list)

    def rank(self) -> None:
        self.rows.sort(key=lambda r: (-r.total, r.wrong, -(r.marks[0] if r.marks and r.marks[0] is not None else 0)))

    def stats(self) -> dict:
        """``{"overall": {...}, "shifts": [{"shift": Shift, ...}], "sections": [...]}`` (averages rounded to 2)."""
        by_shift: dict[str, list[Row]] = {}
        for r in self.rows:
            by_shift.setdefault(r.shift_key, []).append(r)
        out = {"overall": _summary(self.rows, len(self.sections)), "sections": self.sections, "shifts": []}
        for s in self.shifts:
            if by_shift.get(s.key):
                out["shifts"].append({"shift": s, **_summary(by_shift[s.key], len(self.sections))})
        return out


def _summary(rows: list[Row], n_sections: int) -> dict:
    if not rows:
        return {"n": 0, "avg": 0.0, "high": 0.0, "low": 0.0, "median": 0.0, "sections": [None] * n_sections,
                "right": 0.0, "wrong": 0.0, "unattempted": 0.0}
    totals = [r.total for r in rows]
    secs = []
    for i in range(n_sections):
        vals = [r.marks[i] for r in rows if r.marks[i] is not None]
        secs.append(round(sum(vals) / len(vals), 2) if vals else None)
    n = len(rows)
    return {"n": n, "avg": round(sum(totals) / n, 2), "high": max(totals), "low": min(totals),
            "median": round(statistics.median(totals), 2), "sections": secs,
            "right": round(sum(r.right for r in rows) / n, 1), "wrong": round(sum(r.wrong for r in rows) / n, 1),
            "unattempted": round(sum(r.unattempted for r in rows) / n, 1)}


def _doc_sections(doc: dict) -> list[dict]:
    secs = doc.get("sections") or []
    if isinstance(secs, dict):  # results saved before 2026-10-12 (name → counts)
        return [{"name": k, **v} for k, v in secs.items()]
    return secs


def score_row(doc: dict, sections: list[str], marking: dict[str, tuple[float, float]], show_name: bool) -> tuple[Row, float]:
    """One stored result → a ranked-table row under the chosen marking (+ that candidate's maximum marks)."""
    by_name = {s["name"]: s for s in _doc_sections(doc)}
    marks: list[float | None] = []
    right = wrong = unatt = 0
    total = max_marks = 0.0
    for name in sections:
        s = by_name.get(name)
        if s is None:
            marks.append(None)
            continue
        plus, frac = marking.get(name) or marking[""]
        m = round(s.get("right", 0) * plus - s.get("wrong", 0) * negative_from_fraction(plus, frac), 4)
        marks.append(m)
        total += m
        right, wrong, unatt = right + s.get("right", 0), wrong + s.get("wrong", 0), unatt + s.get("unattempted", 0)
        max_marks += (s.get("right", 0) + s.get("wrong", 0) + s.get("unattempted", 0)) * plus
    name = ((doc.get("candidate") or {}).get("name") or "") if show_name else ""
    e = doc.get("exam") or {}
    return Row(name, str(doc.get("roll_no") or ""), e.get("exam_key") or "", marks, round(total, 4),
               right, wrong, unatt), max_marks


async def collect(repo, group: ExamGroup, shifts: list[Shift], marking: dict[str, tuple[float, float]],
                  show_name: bool = True) -> ResultTable:
    table = ResultTable(group.name, list(group.sections), shifts, marking)
    for s in shifts:
        async for doc in repo.exam_results.stream({"exam.exam_key": s.key}, projection=PROJECTION, sub=s.key):
            row, mx = score_row(doc, table.sections, marking, show_name)
            table.rows.append(row)
            table.max_marks = max(table.max_marks, mx)
    table.rank()
    return table


# ------------------------------------------------------------------ output helpers
def _date_dmy(d: str) -> str:
    return "-".join(reversed(d.split("-"))) if d else "?"


def _shift_text(s: Shift) -> str:
    return f"S{s.shift}" if s.shift else (s.time or "?")


def marking_text(marking: dict[str, tuple[float, float]], sections: list[str]) -> str:
    """``+2 / −¼`` (the chosen fraction, never converted) when every section uses the same scheme, else
    ``A +2/−¼ · B +1/−⅓ …``."""
    schemes = [marking.get(n) or marking[""] for n in sections] or [marking[""]]
    if len(set(schemes)) == 1:
        return scheme_text(*schemes[0])
    return " · ".join(f"{c} {scheme_text(*pm).replace(' ', '')}" for c, pm in zip(section_codes(sections), schemes))


def pdf_context(table: ResultTable, stats: dict, *, scope: str, generated: str, theme: dict | None = None,
                show_name: bool = True, name_mask: str | bool = "light") -> dict:
    """Plain, picklable input for ``render.results_pdf``: roll numbers **always masked**, names lightly masked
    (``privacy.mask_name``; owner 2026-10-12 "thoda sa, jyada nhi") — the DB keeps both unmasked."""
    from bot.exam.marks import mask_name, mask_roll

    shifts = {s.key: s for s in table.shifts}
    rows = []
    for r in table.rows:
        s = shifts.get(r.shift_key)
        rows.append([mask_name(r.name, name_mask), mask_roll(r.roll), _date_dmy(s.date) if s else "?", _shift_text(s) if s else "?",
                     r.marks, r.total, r.right, r.wrong, r.unattempted])
    st = {"overall": stats["overall"],
          "shifts": [{**{k: v for k, v in x.items() if k != "shift"}, "date": _date_dmy(x["shift"].date),
                      "shift": _shift_text(x["shift"])} for x in stats["shifts"]]}
    return {"title": table.exam_name, "scope": scope, "marking": marking_text(table.marking, table.sections),
            "generated": generated, "sections": table.sections, "codes": section_codes(table.sections), "stats": st,
            "rows": rows, "show_name": show_name, "max_marks": table.max_marks, "theme": theme}


def results_caption(table: ResultTable, stats: dict, scope: str) -> str:
    """Telegram caption (≤ 1024): overall average + shift-wise averages (expandable quote)."""
    import html

    o = stats["overall"]
    mx = f" / {table.max_marks:g}" if table.max_marks else ""
    head = [f"📊 <b>Results · {html.escape(table.exam_name)}</b>", f"<i>{html.escape(scope)}</i>",
            f"📝 Marking {marking_text(table.marking, table.sections)}", "",
            f"👥 <b>{o['n']}</b> candidates  ·  📈 Average <b>{o['avg']:g}</b>{mx}",
            f"🏆 Highest <b>{o['high']:g}</b>  ·  Lowest {o['low']:g}  ·  Median {o['median']:g}"]
    lines = [f"{_date_dmy(x['shift'].date)} {_shift_text(x['shift'])} — avg <b>{x['avg']:g}</b> · {x['n']} cand."
             for x in stats["shifts"]]
    text = "\n".join(head)
    if lines:
        quote, used = [], len(text) + 80
        for ln in lines:
            if used + len(ln) + 1 > 1000:
                quote.append(f"… {len(lines) - len(quote)} more in the PDF")
                break
            quote.append(ln)
            used += len(ln) + 1
        text += "\n\n🗓 <b>Shift-wise average</b>\n<blockquote expandable>" + "\n".join(quote) + "</blockquote>"
    return text
