"""TCS iON (``*.digialm.com``) response-sheet parser (spec §12.2, verified facts there).

Depends on: lxml, ``parsers.common``, ``parsers.models``, ``parsers.source``, ``core.errors``.
Used by: ``parsers/__init__.py``.

Public API
----------
- ``detect(source) -> bool``
- ``parse(source) -> ExamData``

Sheet layout
------------
``div.main-info-pnl`` → rows ``label | value`` (exam + candidate info, optional ``<strong>`` heading and
marking note) · ``div.section-lbl`` "Section : X" · per question ``div.question-pnl`` with
``table.questionRowTbl`` (``Q.n`` + text, options ``td.rightAns`` / ``td.wrngAns`` with a tick image)
and ``table.menu-tbl`` (Question ID, Option n ID, Status, Chosen Option).

Exceptions → outcome
--------------------
- ``ParseError("no_questions")``: no ``question-pnl`` → the user is told the file isn't a TCS response sheet.
"""

from __future__ import annotations

import re
from fractions import Fraction

from bot.core.errors import ParseError
from bot.parsers import common
from bot.parsers.common import RichText, norm_space
from bot.parsers.models import (
    ANSWERED, MARKED_ANSWERED, MARKED_NOT_ANSWERED, NOT_ANSWERED,
    Candidate, ExamData, ExamMeta, LangContent, Marking, Question, Section, plain,
)
from bot.parsers.source import ExamSource

_DATE_KEYS = ("test date", "exam date", "date of exam")
_TIME_KEYS = ("test time", "exam time", "time of exam")
_NAME_KEYS = ("subject", "exam name", "examination")
_CAND_NAME_KEYS = ("candidate name", "candidate's name", "applicant name", "participant name", "name")
_ROLL_KEYS = ("roll number", "roll no", "roll no.", "hall ticket no", "participant id")
_STATUS = {
    "answered": ANSWERED,
    "not answered": NOT_ANSWERED,
    "marked for review": MARKED_ANSWERED,
    "answered and marked for review": MARKED_ANSWERED,
    "not attempted and marked for review": MARKED_NOT_ANSWERED,
    "not visited": NOT_ANSWERED,
}


def detect(source: ExamSource) -> bool:
    """True for TCS iON response sheets (class names are stable across boards)."""
    head = source.html
    return b"question-pnl" in head and (b"questionRowTbl" in head or b"menu-tbl" in head)


def parse(source: ExamSource) -> ExamData:
    """Parse one TCS sheet (all sections are on one page)."""
    root = common.parse_html(source)
    data = ExamData(meta=ExamMeta(exam_name=None, source="tcs", url=source.url), candidate=Candidate(), sections=[])
    _parse_header(root, source, data)
    rich = RichText(source, data.assets, skip_img=lambda img: "tick" in (img.get("class") or "").lower())

    current: Section | None = None
    for el in root.xpath('//div[contains(concat(" ", normalize-space(@class), " "), " section-lbl ") '
                         'or contains(concat(" ", normalize-space(@class), " "), " question-pnl ")]'):
        cls = el.get("class") or ""
        if "section-lbl" in cls:
            current = Section(name=common.clean_section_name(el.text_content()) or f"Section {len(data.sections) + 1}")
            data.sections.append(current)
            continue
        if current is None:
            current = Section(name=data.meta.exam_name or "Section 1")
            data.sections.append(current)
        q = _parse_question(el, rich, len(current.questions) + 1)
        if q is not None:
            current.questions.append(q)

    data.sections = [s for s in data.sections if s.questions]
    if not data.sections:
        raise ParseError("no_questions", "TCS sheet without questions")
    for s in data.sections:
        _unify_section_language(s)
    data.meta.languages = sorted({lang for q in data.questions() for lang in q.lang})
    return data


def _unify_section_language(section: Section) -> None:
    """A TCS sheet shows each section in one language; per-question detection can be fooled by formulas
    or quoted words, so every question takes its section's majority language."""
    votes: dict[str, int] = {}
    for q in section.questions:
        lang = next(iter(q.lang))
        votes[lang] = votes.get(lang, 0) + 1
    major = max(votes, key=votes.get)
    for q in section.questions:
        lang = next(iter(q.lang))
        if lang != major:
            q.lang = {major: q.lang[lang]}


# ---------------------------------------------------------------- header
def _parse_header(root, source: ExamSource, data: ExamData) -> None:
    info: dict[str, str] = {}
    panel = root.xpath('//div[contains(@class,"main-info-pnl")]')
    panel = panel[0] if panel else None
    if panel is not None:
        for tr in panel.xpath(".//tr"):
            tds = tr.xpath("./td")
            if len(tds) >= 2:
                key = norm_space(tds[0].text_content()).rstrip(" :").lower()
                if key and "photo" not in key:
                    info[key] = norm_space(tds[1].text_content())

    meta, cand = data.meta, data.candidate
    date_raw = next((info[k] for k in _DATE_KEYS if info.get(k)), None)
    time_raw = next((info[k] for k in _TIME_KEYS if info.get(k)), None)
    meta.date = common.parse_date(date_raw) or source.hints.date
    meta.time, meta.start_hhmm, printed_shift = common.parse_time_range(time_raw)
    if printed_shift:
        meta.shift, meta.shift_source = printed_shift, "printed"
    elif source.hints.shift:
        meta.shift, meta.shift_source = source.hints.shift, "hint"
    elif meta.start_hhmm:
        meta.shift, meta.shift_source = common.shift_from_start(meta.start_hhmm), "time"

    # exam name: Subject row → <strong> heading → hint (spec §12.2)
    subject = next((info[k] for k in _NAME_KEYS if info.get(k)), None)
    headings = _headings(panel) if panel is not None else []
    if subject:
        meta.exam_name, meta.name_source = common.clean_exam_name(subject), "subject"
    elif headings:
        meta.exam_name, meta.name_source = common.clean_exam_name(headings[0]), "heading"
    elif source.hints.exam_name:
        meta.exam_name, meta.name_source = source.hints.exam_name, "hint"
    meta.exam_code = _exam_code(source.url)
    data.marking = _marking_note(panel) if panel is not None else None

    exam_keys = set(_DATE_KEYS + _TIME_KEYS + _NAME_KEYS)
    for k, v in info.items():
        if k in exam_keys or not v:
            continue
        if k in _CAND_NAME_KEYS and not cand.name:
            cand.name = v
        elif k in _ROLL_KEYS and not cand.roll_no:
            cand.roll_no = v
        else:
            cand.other[k] = v


def _headings(panel) -> list[str]:
    """``<strong>`` texts in the info panel that look like an exam title (not the marking/legend notes)."""
    out = []
    for s in panel.xpath(".//strong"):
        t = norm_space(s.text_content())
        if not t or t.startswith(("*", "Correct", "Incorrect")) or re.match(r"^\d+\.", t):
            continue
        out.append(t)
    return out


def _marking_note(panel) -> Marking | None:
    """Read "Correct Answer will carry 1 mark…" / "Incorrect Answer will carry 1/3 Negative mark…"."""
    text = norm_space(panel.text_content())
    plus = re.search(r"Correct Answer will carry\s+([\d./]+)\s*mark", text, re.I)
    minus = re.search(r"Incorrect Answer will carry\s+([\d./]+)\s*Negative", text, re.I)
    if not plus:
        return None
    p = float(Fraction(plus.group(1)))
    m = float(Fraction(minus.group(1))) if minus else 0.0
    label = f"+{plus.group(1)} / −{minus.group(1) if minus else '0'}"
    return Marking(plus=p, minus=m, source="file", label=label)


def _exam_code(url: str | None) -> str | None:
    if not url or "AssessmentQPHTMLMode1" not in url:
        return None
    tail = url.split("AssessmentQPHTMLMode1", 1)[1]
    m = re.search(r"/+([A-Za-z0-9]+)/", tail)
    return m.group(1) if m else None


# ---------------------------------------------------------------- questions
def _parse_question(panel, rich: RichText, fallback_no: int) -> Question | None:
    row_tbl = panel.xpath('.//table[contains(@class,"questionRowTbl")]')
    if not row_tbl:
        return None
    row_tbl = row_tbl[0]

    number, q_parts, options, correct = fallback_no, [], [], []
    for tr in row_tbl.xpath("./tr | ./tbody/tr"):
        tds = tr.xpath("./td")
        opt_td = [td for td in tds if any(c in (td.get("class") or "") for c in ("rightAns", "wrngAns"))]
        if opt_td:
            td = opt_td[0]
            if "rightAns" in (td.get("class") or ""):
                correct.append(len(options))
            options.append(common.strip_option_label(rich.build(td), len(options)))
            continue
        if not tds:
            continue
        label = norm_space(tds[0].text_content())
        if m := re.match(r"^Q\.?\s*(\d+)$", label):
            number = int(m.group(1))
            body = [td for td in tds[1:] if norm_space(td.text_content()) or td.xpath(".//img")]
            q_parts.extend(rich.build(td) for td in body)
        elif label.lower() != "ans":
            # extra rows before the options (comprehension passages, statements)
            q_parts.extend(rich.build(td) for td in tds if norm_space(td.text_content()) or td.xpath(".//img"))

    meta = _side_table(panel)

    question = "<br/>".join(p for p in q_parts if p)
    issues = rich.take_issues()
    if not options:
        issues.append("no_options")
    if len(correct) != 1:
        issues.append(f"correct_count:{len(correct)}")

    chosen = _chosen_index(meta.get("chosen option"), len(options))
    status = _STATUS.get((meta.get("status") or "").lower(), ANSWERED if chosen is not None else NOT_ANSWERED)
    if chosen is None and status in (ANSWERED, MARKED_ANSWERED):
        status = NOT_ANSWERED if status == ANSWERED else MARKED_NOT_ANSWERED

    lang = common.detect_lang(plain(question) + " " + " ".join(plain(o) for o in options)) or "en"
    option_ids = [meta[f"option {i + 1} id"] for i in range(len(options)) if meta.get(f"option {i + 1} id")]
    return Question(
        number=number,
        lang={lang: LangContent(question=question, options=options)},
        correct=correct[0] if correct else None,
        chosen=chosen,
        status=status,
        qid=meta.get("question id"),
        option_ids=option_ids if len(option_ids) == len(options) else None,
        qtype=meta.get("question type") or "MCQ",
        issues=issues,
    )


def _side_table(panel) -> dict[str, str]:
    """``Label : | value`` pairs of the side table, read as consecutive cells — live sheets omit ``<tr>``
    around some cells (e.g. "Chosen Option"), so row-based reading would lose them (B-004)."""
    meta: dict[str, str] = {}
    cells = panel.xpath('.//table[contains(@class,"menu-tbl")]//td')
    for i, td in enumerate(cells[:-1]):
        label = norm_space(td.text_content())
        if label.endswith(":") and "bold" in (cells[i + 1].get("class") or ""):
            meta.setdefault(label.rstrip(" :").lower(), norm_space(cells[i + 1].text_content()))
    return meta


def _chosen_index(raw: str | None, n_options: int) -> int | None:
    """``"B"`` / ``"2"`` → 1; ``"--"`` / empty / out of range → None."""
    raw = (raw or "").strip()
    if not raw or raw.startswith("-"):
        return None
    if raw.isdigit():
        i = int(raw) - 1
    elif len(raw) == 1 and raw.isalpha():
        i = ord(raw.upper()) - 65
    else:
        return None
    return i if 0 <= i < n_options else None
