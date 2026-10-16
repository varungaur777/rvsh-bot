"""Data classes every parser produces (spec §12).

Depends on: stdlib only.
Used by: ``parsers/*``, ``exam/*`` (JSON build, marks), renderers, tests.

Rich text ("mini-markup", D-003)
--------------------------------
Question and option content is a string of plain text plus only these tags:
``<sup>…</sup> <sub>…</sub> <b>…</b> <i>…</i> <u>…</u> <br/>`` and ``<img id="ASSET_ID"/>``.
Text is XML-escaped (``&amp; &lt; &gt;``). The same string renders in ReportLab Paragraphs,
PyMuPDF ``insert_htmlbox`` and the HTML quiz. ``plain()`` turns it into poll/hash text.

Indices
-------
``Question.correct`` and ``Question.chosen`` are **0-based** indices into the displayed options
(``None`` = unknown / not answered).
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass, field
from typing import Iterator

LANGS = ("en", "hi")

# question status values (normalised from portal wording)
ANSWERED = "answered"
NOT_ANSWERED = "not_answered"
MARKED_ANSWERED = "marked_answered"          # answered + marked for review → evaluated
MARKED_NOT_ANSWERED = "marked_not_answered"  # not answered + marked → not evaluated

_TAG = re.compile(r"<[^>]+>")
_IMG = re.compile(r'<img id="([^"]+)"\s*/>')
_WS = re.compile(r"\s+")


def plain(markup: str, image_token: str = "") -> str:
    """Mini-markup → plain text (``<br/>`` → space, images → ``image_token``), whitespace collapsed."""
    s = _IMG.sub(f" {image_token} " if image_token else " ", markup)
    s = s.replace("<br/>", " ")
    s = _TAG.sub("", s)
    return _WS.sub(" ", html.unescape(s)).strip()


def image_ids(markup: str) -> list[str]:
    """Asset ids referenced by ``<img id="…"/>`` tags, in order."""
    return _IMG.findall(markup)


@dataclass(slots=True)
class Asset:
    """An image taken from the sheet. ``id`` is content-based (same bytes → same id)."""

    id: str
    data: bytes
    mime: str
    sha256: str
    url: str | None = None
    kind: str = "image"  # image | formula | strip (EDQT text-strip image)
    lang: str | None = None  # language of the text inside the image, when known (EDQT _EN/_HI)


@dataclass(slots=True)
class LangContent:
    """Question + options in one language (mini-markup). Question JSON files (``/json``) may add an
    ``explanation`` (mini-markup) and their own ``correct`` index for this language (their English and Hindi option
    order can differ)."""

    question: str
    options: list[str]
    explanation: str = ""
    correct: int | None = None


@dataclass(slots=True)
class Question:
    """One MCQ as printed on the sheet, plus this candidate's response."""

    number: int
    lang: dict[str, LangContent]
    correct: int | None
    chosen: int | None = None
    status: str = NOT_ANSWERED
    qid: str | None = None  # portal question id (TCS "Question ID")
    option_ids: list[str] | None = None  # portal option ids in displayed order
    qtype: str = "MCQ"
    issues: list[str] = field(default_factory=list)

    @property
    def n_options(self) -> int:
        """Number of options (taken from the first language present)."""
        return len(next(iter(self.lang.values())).options) if self.lang else 0

    @property
    def evaluated(self) -> bool:
        """True when the response counts for marks (answered, or answered + marked for review)."""
        return self.chosen is not None and self.status in (ANSWERED, MARKED_ANSWERED)


@dataclass(slots=True)
class Section:
    name: str
    questions: list[Question] = field(default_factory=list)


@dataclass(slots=True)
class Marking:
    """Marks for one question: ``plus`` for right, ``minus`` (absolute marks) deducted for wrong."""

    plus: float
    minus: float
    source: str = "file"  # file | user | preset | default
    label: str = ""  # human text, e.g. "+1 / −1/3"


@dataclass(slots=True)
class ExamMeta:
    exam_name: str | None
    level: str | None = None
    date: str | None = None  # ISO YYYY-MM-DD
    time: str | None = None  # as printed, e.g. "12:45 PM - 2:15 PM"
    start_hhmm: str | None = None  # "1245" — part of exam_key (D-005)
    shift: int | None = None
    shift_source: str | None = None  # printed | time | hint
    source: str = ""  # tcs | edqt
    url: str | None = None
    exam_code: str | None = None  # portal exam code from the URL (e.g. 1258O25307)
    languages: list[str] = field(default_factory=list)
    name_source: str | None = None  # subject | heading | header | hint


@dataclass(slots=True)
class Candidate:
    """Candidate details — kept for ``exam_results`` only, never printed in outputs."""

    roll_no: str | None = None
    name: str | None = None
    other: dict[str, str] = field(default_factory=dict)


@dataclass(slots=True)
class ExamData:
    """Everything parsed from one candidate's sheet(s)."""

    meta: ExamMeta
    candidate: Candidate
    sections: list[Section]
    assets: dict[str, Asset] = field(default_factory=dict)
    marking: Marking | None = None  # global marking printed on the sheet, if any
    warnings: list[str] = field(default_factory=list)

    def questions(self) -> Iterator[Question]:
        """All questions in section order."""
        for s in self.sections:
            yield from s.questions

    @property
    def n_questions(self) -> int:
        return sum(len(s.questions) for s in self.sections)

    def summary(self) -> dict:
        """PII-free summary used by golden tests and logs."""
        status: dict[str, int] = {}
        for q in self.questions():
            status[q.status] = status.get(q.status, 0) + 1
        return {
            "source": self.meta.source,
            "exam_name": self.meta.exam_name,
            "level": self.meta.level,
            "date": self.meta.date,
            "time": self.meta.time,
            "start_hhmm": self.meta.start_hhmm,
            "shift": self.meta.shift,
            "languages": self.meta.languages,
            "sections": [[s.name, len(s.questions)] for s in self.sections],
            "questions": self.n_questions,
            "status": dict(sorted(status.items())),
            "with_images": sum(1 for q in self.questions() if any(image_ids(lc.question) or any(image_ids(o) for o in lc.options) for lc in q.lang.values())),
            "assets": len(self.assets),
            "marking": None if not self.marking else [self.marking.plus, round(self.marking.minus, 4)],
            "has_roll_no": bool(self.candidate.roll_no),
        }
