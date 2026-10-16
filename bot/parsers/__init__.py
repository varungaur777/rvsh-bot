"""Parsers entry point: detect the portal family, parse, and merge several files into one ``ExamData`` (spec §12.2).

Depends on: ``parsers.tcs``, ``parsers.edqt``, ``parsers.models``, ``parsers.source``, ``core.errors``.
Used by: exam pipeline (P2b), ``/batch``, ``/gen_quiz``, tests. **Frozen after golden tests pass (spec §0.4).**

Public API
----------
- ``detect_family(source) -> "tcs" | "edqt" | None``
- ``parse_source(source) -> ExamData``
- ``parse_sources(sources) -> ExamData`` — several files of **one** candidate/exam (e.g. EDQT parts).
  Section order: ``Hints.order`` → part letter/number in the section name (``PART-A``, ``Section 2``) → send order.
  The same section sent twice is kept once.
- ``hints_from_path(path) -> Hints`` — ``<Exam>/<DD-MM-YYYY>/Shift N/Section_K_<name>.mht`` (batch folders, tests).

Exceptions → outcome
--------------------
- ``ParseError("unknown_format")``: neither TCS nor EDQT → "this file is not a supported response sheet".
- ``ParseError("mixed_sources")``: TCS and EDQT files together → ask the user to send one exam at a time.
- ``ParseError("different_exams")``: files from different exams/dates/candidates → same message.
- ``ParseError("no_questions")`` (from a parser).
"""

from __future__ import annotations

import re
from pathlib import Path

from bot.core.errors import ParseError
from bot.parsers import edqt, tcs
from bot.parsers.models import ExamData, Section
from bot.parsers.source import ExamSource, Hints

_FAMILIES = {"tcs": tcs, "edqt": edqt}


def detect_family(source: ExamSource) -> str | None:
    """Which parser understands this source (TCS checked first: its markers are the most specific)."""
    for name, mod in _FAMILIES.items():
        if mod.detect(source):
            return name
    return None


def parse_source(source: ExamSource) -> ExamData:
    """Parse one file/page with the detected parser."""
    family = detect_family(source)
    if family is None:
        raise ParseError("unknown_format", f"url={source.url!r}")
    return _FAMILIES[family].parse(source)


def parse_sources(sources: list[ExamSource]) -> ExamData:
    """Parse and merge several files of one exam sheet into a single ``ExamData``."""
    if not sources:
        raise ParseError("no_questions", "no sources")
    parsed = [(i, s, parse_source(s)) for i, s in enumerate(sources)]
    if len(parsed) == 1:
        return parsed[0][2]
    families = {d.meta.source for _, _, d in parsed}
    if len(families) > 1:
        raise ParseError("mixed_sources", ",".join(sorted(families)))
    _check_same_exam([d for _, _, d in parsed])

    parsed.sort(key=lambda t: _section_order_key(t[0], t[1], t[2]))
    base = parsed[0][2]
    seen_sections = {_norm(s.name) for s in base.sections}
    for _, _, d in parsed[1:]:
        for f in ("exam_name", "level", "date", "time", "start_hhmm", "shift", "shift_source", "url", "exam_code", "name_source"):
            if getattr(base.meta, f) is None and getattr(d.meta, f) is not None:
                setattr(base.meta, f, getattr(d.meta, f))
        if not base.candidate.roll_no and d.candidate.roll_no:
            base.candidate = d.candidate
        base.marking = base.marking or d.marking
        base.assets.update(d.assets)
        base.warnings.extend(d.warnings)
        for s in d.sections:
            if _norm(s.name) in seen_sections:
                base.warnings.append(f"duplicate_section:{s.name}")
                continue
            seen_sections.add(_norm(s.name))
            base.sections.append(s)
    base.meta.languages = sorted({lang for q in base.questions() for lang in q.lang})
    return base


def _norm(s: str) -> str:
    return re.sub(r"\W+", "", s.lower())


def _check_same_exam(items: list[ExamData]) -> None:
    """Refuse to merge sheets that clearly belong to different exams or candidates."""
    for attr in ("exam_code", "date", "start_hhmm"):
        values = {getattr(d.meta, attr) for d in items if getattr(d.meta, attr)}
        if len(values) > 1:
            raise ParseError("different_exams", f"{attr}={sorted(values)}")
    rolls = {d.candidate.roll_no for d in items if d.candidate.roll_no}
    if len(rolls) > 1:
        raise ParseError("different_exams", "roll numbers differ")


def _section_order_key(index: int, source: ExamSource, data: ExamData) -> tuple:
    hint = source.hints.order if source.hints.order is not None else 10_000
    name = data.sections[0].name if data.sections else ""
    m = re.search(r"\bPART[\s\-_]*([A-Z])\b", name, re.I) or re.search(r"\bSection[\s\-_]*(\d+)\b", name, re.I)
    part = (ord(m.group(1).upper()) - 64 if m.group(1).isalpha() else int(m.group(1))) if m else 10_000
    return hint, part, index


_PATH = re.compile(r"(?P<exam>[^/\\]+)[/\\](?P<date>\d{1,2}-\d{1,2}-\d{4})[/\\]Shift\s*(?P<shift>\d+)[/\\](?:Section_(?P<order>\d+)_)?(?P<section>[^/\\]+?)\.mht", re.I)


def hints_from_path(path: str | Path) -> Hints:
    """Hints from the batch download layout ``<Exam>/<DD-MM-YYYY>/Shift N/Section_K_<name>.mht``."""
    m = _PATH.search(str(path))
    if not m:
        return Hints()
    d, mo, y = (int(x) for x in m.group("date").split("-"))
    return Hints(
        exam_name=m.group("exam").strip(),
        date=f"{y:04d}-{mo:02d}-{d:02d}",
        shift=int(m.group("shift")),
        section=m.group("section").strip(),
        order=int(m.group("order")) if m.group("order") else None,
    )


__all__ = ["ExamData", "ExamSource", "Hints", "Section", "detect_family", "hints_from_path", "parse_source", "parse_sources"]
