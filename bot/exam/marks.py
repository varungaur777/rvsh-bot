"""Marks calculation (spec §14.1, §8 negative marking rule).

Depends on: ``parsers.models``. Used by: exam pipeline (scorecard, ``exam_results``), result PDF (P4).

Public API
----------
- ``SectionScore`` / ``Score`` dataclasses (``to_dict()`` for storage and the scorecard renderer).
- ``score(data, marking, per_section=None) -> Score`` — ``marking`` applies to every section unless
  ``per_section[section_name]`` overrides it.
- ``fraction_text(frac)`` / ``scheme_text(plus, frac)`` — display: ``+2 / −¼`` (the chosen fraction, never the
  converted absolute marks — owner 2026-10-12).
- ``short_section(name, limit=34)`` — a section name for a card or caption: ``"PART-C (Quantitative Aptitude (Basic
  Arithmetic Skill))"`` → ``"Quantitative Aptitude"`` (owner 2026-10-13: "caption bahut messy").
- ``negative_from_fraction(plus, fraction)`` — owner's rule: a negative of 0.5 means half of that
  question's marks are deducted for a wrong answer.
- ``mask_roll(roll)`` → ``2201****56`` · ``mask_name(name, mode)`` → ``ARU* KUMA*`` (light, default) /
  ``A*** K****`` (strong) / unchanged (off). Masking is for display only — stores keep the real values.

Counting rules: a response is evaluated when the candidate chose an option and the status is answered or
"marked for review" with an answer. Right = chosen == correct, wrong = chosen ≠ correct, everything else is
not attempted. Questions without a correct option (dropped by the board) are counted in ``dropped`` and
score 0.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from fractions import Fraction

from bot.parsers.models import ExamData, Marking


@dataclass(slots=True)
class SectionScore:
    name: str
    total: int = 0
    right: int = 0
    wrong: int = 0
    unattempted: int = 0
    dropped: int = 0
    marks: float = 0.0
    max_marks: float = 0.0
    plus: float = 1.0
    minus: float = 0.0


@dataclass(slots=True)
class Score:
    sections: list[SectionScore] = field(default_factory=list)
    marking_label: str = ""

    @property
    def right(self) -> int:
        return sum(s.right for s in self.sections)

    @property
    def wrong(self) -> int:
        return sum(s.wrong for s in self.sections)

    @property
    def unattempted(self) -> int:
        return sum(s.unattempted for s in self.sections)

    @property
    def total(self) -> int:
        return sum(s.total for s in self.sections)

    @property
    def marks(self) -> float:
        return round(sum(s.marks for s in self.sections), 4)

    @property
    def max_marks(self) -> float:
        return round(sum(s.max_marks for s in self.sections), 4)

    @property
    def accuracy(self) -> float:
        attempted = self.right + self.wrong
        return round(100 * self.right / attempted, 1) if attempted else 0.0

    def to_dict(self) -> dict:
        return {"sections": [asdict(s) for s in self.sections], "right": self.right, "wrong": self.wrong,
                "unattempted": self.unattempted, "total": self.total, "marks": self.marks,
                "max_marks": self.max_marks, "accuracy": self.accuracy, "marking": self.marking_label}


def negative_from_fraction(plus: float, fraction: float) -> float:
    """Absolute marks deducted per wrong answer for a fraction of the question's marks."""
    return float(Fraction(plus).limit_denominator(100) * Fraction(fraction).limit_denominator(100))


_VULGAR = {(1, 4): "¼", (1, 3): "⅓", (1, 2): "½", (2, 3): "⅔", (3, 4): "¾", (1, 5): "⅕", (1, 8): "⅛"}


def fraction_text(frac: float) -> str:
    """Negative-marking fraction as chosen: ``0.25`` → ``¼``, ``0.3333`` → ``⅓``, ``1`` → ``1``."""
    f = Fraction(frac).limit_denominator(12)
    if f.numerator == 0:
        return "0"
    if f.denominator == 1:
        return str(f.numerator)
    return _VULGAR.get((f.numerator, f.denominator), f"{f.numerator}/{f.denominator}")


def scheme_text(plus: float, frac: float) -> str:
    """How a marking scheme is **always** shown (owner 2026-10-12): marks per question and the negative as the
    fraction that was chosen — ``+2 / −¼`` — never converted to absolute marks (``−0.5`` would read like ``½``)."""
    return f"+{_num(plus)} / −{fraction_text(frac)}" if frac else f"+{_num(plus)} / no negative"


_PART = re.compile(r"^\s*part[\s\-–:]*[a-z0-9]{1,3}\s*[\(\-–:]\s*(.+?)\)?\s*$", re.I)
_TAIL = re.compile(r"\s*\([^()]*\)\s*$")


def short_section(name: str, limit: int = 34) -> str:
    """``"PART-A (General Intelligence)"`` → ``"General Intelligence"``; a long name loses its last bracket, then is
    cut with ``…``. Display only — section names are stored and matched in full."""
    s = (name or "").strip()
    m = _PART.match(s)
    if m and m.group(1).strip(" ()"):
        s = m.group(1).strip()
        if s.count("(") > s.count(")"):
            s += ")"
    while len(s) > limit and _TAIL.search(s) and _TAIL.sub("", s):
        s = _TAIL.sub("", s)
    return s if len(s) <= limit else s[:limit - 1].rstrip() + "…"


def marking_label(m: Marking) -> str:
    return m.label or scheme_text(m.plus, m.minus / m.plus if m.plus else 0.0)


def _num(x: float) -> str:
    f = Fraction(x).limit_denominator(12)
    if f.denominator == 1:
        return str(f.numerator)
    return f"{f.numerator}/{f.denominator}" if abs(float(f) - x) < 1e-9 else f"{x:g}"


def score(data: ExamData, marking: Marking, per_section: dict[str, Marking] | None = None) -> Score:
    out = Score(marking_label=marking_label(marking))
    for sec in data.sections:
        m = (per_section or {}).get(sec.name, marking)
        s = SectionScore(name=sec.name, plus=m.plus, minus=m.minus)
        for q in sec.questions:
            s.total += 1
            if q.correct is None:
                s.dropped += 1
                continue
            s.max_marks += m.plus
            if not q.evaluated:
                s.unattempted += 1
            elif q.chosen == q.correct:
                s.right += 1
            else:
                s.wrong += 1
        s.marks = round(s.right * m.plus - s.wrong * m.minus, 4)
        out.sections.append(s)
    if per_section:
        out.marking_label = "different per section"
    return out


NAME_MASKS = ("light", "strong", "off")


def mask_name(name: str | None, mode: str | bool = "light") -> str:
    """Display mask for a candidate name (setting ``privacy.mask_name``):

    - ``light`` (default; owner 2026-10-12 "too much masked … thoda sa, jyada nhi"): about two thirds of every
      word stay — ``"ARUN KUMAR"`` → ``"ARU* KUMA*"`` (words of 1–2 letters unchanged);
    - ``strong``: first letter only — ``"A*** K****"``;
    - ``off`` / ``False``: unchanged. ``True`` (old boolean setting) = ``light``.
    """
    if not name:
        return ""
    mode = "light" if mode is True else ("off" if mode is False else str(mode))
    if mode == "off":
        return str(name)
    keep = (lambda w: 1) if mode == "strong" else (lambda w: len(w) if len(w) <= 2 else (2 * len(w) + 2) // 3)
    return " ".join(w[:keep(w)] + "*" * (len(w) - keep(w)) for w in str(name).split())


def mask_roll(roll: str | None) -> str:
    if not roll:
        return ""
    r = str(roll)
    return r if len(r) <= 6 else r[:4] + "*" * (len(r) - 6) + r[-2:]
