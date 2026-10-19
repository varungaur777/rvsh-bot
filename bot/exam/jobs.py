"""Exam CPU jobs — pure, picklable functions run in the process pool (``compute``): paths in, small data out.

Kept free of Telegram / DB / network imports so pool workers stay small (they import only the parsers).

Public API
----------
- ``preload()`` — warm-up job (imports the parsers in a fresh worker).
- ``input_files(folder) -> list[Path]`` — the sheets of a task workdir, in arrival order.
- ``inspect_sheets(folder, hints) -> (ExamMeta, dict)`` — meta (for the URL registry) + the summary shown to the user;
  ``census`` / ``medium`` = the sheet's real language question by question (``schema.medium``: saved with the link).
- ``score_sheets(folder, hints, plus, minus, source) -> (ExamData, Score)`` — the ``ExamData`` is slim (meta +
  candidate only: no questions cross the process boundary).
- ``exam_file_names(paths, hints) -> list[str | None]`` — dump file names ``(dd-mm-yyyy) Exam [- section] (Shift N)``.

Exceptions: ``ParseError`` and friends propagate (pickled back to the bot process).
"""

from __future__ import annotations

import re
from pathlib import Path

from bot.exam.keys import exam_key, meta_line
from bot.exam.marks import Score, score
from bot.exam.schema import lang_census, main_languages, medium
from bot.parsers import parse_source, parse_sources, source
from bot.parsers.models import ExamData, ExamMeta, Marking

SHEET_EXT = (".mht", ".mhtml", ".html", ".htm")


def preload() -> int:
    """Pool warm-up job: unpickling it imports this module (parsers, lxml) in the worker."""
    import os
    return os.getpid()


def input_files(folder) -> list[Path]:
    folder = Path(folder)
    if not folder.exists():
        return []
    return sorted((p for p in folder.iterdir() if p.suffix.lower() in SHEET_EXT), key=lambda p: p.name)


def _load(folder, hints: dict | None) -> ExamData:
    h = source.Hints(**(hints or {}))
    return parse_sources([source.load_path(p, h) for p in input_files(folder)])


def inspect_sheets(folder, hints: dict | None) -> tuple[ExamMeta, dict]:
    data = _load(folder, hints)
    m, marking = data.meta, data.marking
    census = lang_census(data)
    return m, {
        "exam_name": m.exam_name, "level": m.level, "date": m.date, "shift": m.shift, "time": m.time,
        "source": m.source, "languages": main_languages([set(q.lang) for q in data.questions()],
                                                         [(x.name, len(x.questions)) for x in data.sections]), "url": m.url,
        "exam_key": exam_key(m),
        "sections": [[s.name, len(s.questions)] for s in data.sections], "questions": data.n_questions,
        "marking": [marking.plus, marking.minus] if marking else None, "has_roll": bool(data.candidate.roll_no),
        "meta_line": meta_line(m), "warnings": data.warnings[:5], "census": census, "medium": medium(census),
    }


def score_sheets(folder, hints: dict | None, plus: float, minus: float, src: str,
                 per_section: dict[str, tuple[float, float]] | None = None) -> tuple[ExamData, Score]:
    """``per_section``: ``{section name: (plus, minus)}`` (admins, "Different per section") — absolute marks."""
    data = _load(folder, hints)
    per = {n: Marking(plus=pl, minus=mi, source=src) for n, (pl, mi) in (per_section or {}).items()}
    sc = score(data, Marking(plus=plus, minus=minus, source=src), per or None)
    return ExamData(meta=data.meta, candidate=data.candidate, sections=[]), sc


_BAD_NAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]+')


def exam_file_names(paths: list[Path], hints: dict | None = None) -> list[str | None]:
    """Dump file names from the sheets themselves, owner's format ``(date) exam name (shift)`` (2026-10-12), e.g.
    ``(29-12-2025) RRB Level 01 Stage I (Shift 3).mht``; an EDQT page adds its section to the name:
    ``(10-09-2026) Stenographer … - PART-A (General Intelligence) (Shift 3).mht``. The pages of one input share
    the date / name / shift — each taken from the first page that prints it (part A may not print the shift).
    Missing date or shift → that part is left out. ``None`` for a sheet that cannot be read (its original name is
    used). Sync (parses the files) — call it in a worker thread."""
    datas = []
    for path in paths:
        try:
            datas.append(parse_source(source.load_path(path, source.Hints(**(hints or {})))))
        except Exception:
            datas.append(None)
    metas = [d.meta for d in datas if d is not None]

    def first(field: str):
        return next((getattr(m, field) for m in metas if getattr(m, field)), None)

    name, level, date, shift = first("exam_name"), first("level"), first("date"), first("shift")
    if not name:
        return [None] * len(paths)
    if level and level.lower() not in name.lower():
        name = f"{name} {level}"
    head = f"({date[8:10]}-{date[5:7]}-{date[:4]}) " if date and len(date) == 10 else ""  # "/" is not allowed
    tail = f" (Shift {shift})" if shift else ""
    out = []
    for path, d in zip(paths, datas):
        if d is None:
            out.append(None)
            continue
        title = f"{name} - {d.sections[0].name}" if d.meta.source == "edqt" and len(d.sections) == 1 else name
        title = re.sub(r"\s+", " ", _BAD_NAME.sub(" ", title)).strip(" .-")[:140]
        out.append(f"{head}{title}{tail}{path.suffix or '.mht'}" if title else None)
    return out
