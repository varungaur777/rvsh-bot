"""EDQT (``sscexams.cbexams.com`` "ViewCandResponse") response-sheet parser (spec §12.2, verified facts there).

Depends on: lxml, ``parsers.common``, ``parsers.models``, ``parsers.source``, ``core.errors``.
Used by: ``parsers/__init__.py``.

Public API
----------
- ``detect(source) -> bool``
- ``parse(source) -> ExamData`` — one page = one section.

Sheet layout
------------
Header rows ``Roll No. : … Candidate Name : … Exam Level : … Test Date : … Test Time and Shift : …
Centre Name : …`` (empty in admin views) · ``#ddltest`` (exam level) · ``#lblsubject`` (section) ·
per question a table: row ``font.tdtext "Q.No: n"`` + question image(s), then 4 option rows whose label
cell colour marks the response: green = chosen & correct, red = chosen & wrong, yellow = correct
(not chosen); a row with a gray cell "Not Answered" marks an unanswered question. Images are
``qimg/<n>/[EN|HI/]<uuid>_<EN|HI>.jpg``; a cell may hold both the English and the Hindi image.

Exceptions → outcome
--------------------
- ``ParseError("no_questions")``: no ``Q.No`` rows → the user is told the file isn't an EDQT response sheet.
"""

from __future__ import annotations

import re

from bot.core.errors import ParseError
from bot.parsers import common
from bot.parsers.common import RichText, norm_space
from bot.parsers.models import (
    ANSWERED, NOT_ANSWERED, Candidate, ExamData, ExamMeta, LangContent, Question, Section, image_ids,
)
from bot.parsers.source import ExamSource

_LANG_IN_SRC = re.compile(r"_(EN|HI)\.(?:jpe?g|png|gif)", re.I)
_QNO = re.compile(r"Q\.?\s*No\s*[:.]?\s*(\d+)", re.I)
_HEADER_LABELS = ("Roll No.", "Candidate Name", "Exam Level", "Test Date", "Test Time and Shift", "Centre Name")


def detect(source: ExamSource) -> bool:
    """True for EDQT/cbexams sheets."""
    head = source.html
    return (b"lblsubject" in head and b"Q.No" in head) or (b"cbexams" in (source.url or "").encode() and b"Q.No" in head)


def parse(source: ExamSource) -> ExamData:
    """Parse one EDQT section page."""
    root = common.parse_html(source)
    for wm in root.xpath('//*[contains(@class,"watermark")]'):  # roll-number watermark repeated over the page
        wm.getparent().remove(wm)

    data = ExamData(meta=ExamMeta(exam_name=None, source="edqt", url=source.url), candidate=Candidate(), sections=[])
    _parse_header(root, source, data)
    rich = RichText(source, data.assets, lang_of_src=_lang_of_src)

    section_name = norm_space(" ".join(e.text_content() for e in root.xpath('//*[@id="lblsubject"]'))) or source.hints.section
    section = Section(name=section_name or "Section 1")
    for qfont in root.xpath('//font[contains(@class,"tdtext")]'):
        m = _QNO.match(norm_space(qfont.text_content()))
        if not m:
            continue
        table = next((a for a in qfont.iterancestors("table")), None)
        if table is None:
            continue
        q = _parse_question(table, qfont, int(m.group(1)), rich)
        if q is not None:
            section.questions.append(q)
    if not section.questions:
        raise ParseError("no_questions", "EDQT page without Q.No rows")
    data.sections = [section]
    data.meta.languages = sorted({lang for q in section.questions for lang in q.lang})
    return data


def _lang_of_src(src: str) -> str | None:
    m = _LANG_IN_SRC.search(src or "")
    return m.group(1).lower() if m else None


# ---------------------------------------------------------------- header
def _parse_header(root, source: ExamSource, data: ExamData) -> None:
    text = norm_space(root.xpath("//body")[0].text_content()) if root.xpath("//body") else ""
    info = _header_values(text)
    meta, cand, hints = data.meta, data.candidate, source.hints

    level = norm_space(" ".join(o.text_content() for o in root.xpath('//*[@id="ddltest"]//option[@selected]'))) \
        or norm_space(" ".join(o.text_content() for o in root.xpath('//*[@id="ddltest"]//option'))) or info.get("Exam Level")
    meta.level = level or None
    # exam name: hint (batch line / caption / path) is the full name; the header level is used when it already
    # is a full exam name (e.g. "Stenographer Grade C & D Examination, 2026") and no hint exists
    if hints.exam_name:
        meta.exam_name, meta.name_source = hints.exam_name, "hint"
    elif level:
        meta.exam_name, meta.name_source = common.clean_exam_name(level), "header"

    meta.date = common.parse_date(info.get("Test Date")) or hints.date \
        or common.to_date_from_url_token(source.url, source.saved_at)
    meta.time, meta.start_hhmm, printed_shift = common.parse_time_range(info.get("Test Time and Shift"))
    if printed_shift:
        meta.shift, meta.shift_source = printed_shift, "printed"
    elif hints.shift:
        meta.shift, meta.shift_source = hints.shift, "hint"
    elif meta.start_hhmm:
        meta.shift, meta.shift_source = common.shift_from_start(meta.start_hhmm), "time"
    meta.exam_code = _exam_code(source.url)

    cand.roll_no = info.get("Roll No.") or None
    cand.name = info.get("Candidate Name") or None
    if info.get("Centre Name"):
        cand.other["centre name"] = info["Centre Name"]


def _header_values(text: str) -> dict[str, str]:
    """Values between the fixed header labels (labels may be followed by empty values in admin views)."""
    out: dict[str, str] = {}
    pattern = "|".join(re.escape(lbl) for lbl in _HEADER_LABELS)
    # lookahead tolerates zero whitespace: in admin views the value is empty and the next label follows at once
    stop = r"(?=\s*(?:" + pattern + r")\s*:|\s*Correct Option selected|\s*Save\s*/\s*Print|$)"
    for lbl in _HEADER_LABELS:
        m = re.search(re.escape(lbl) + r"\s*:\s*(.*?)" + stop, text)
        if m:
            out[lbl] = m.group(1).strip()
    return out


def _exam_code(url: str | None) -> str | None:
    """``https://sscexams.cbexams.com/Phase14SP26ytdQchallengeytccxSep17/ViewCandResponse.aspx`` → ``Phase14SP26ytdQchallengeytccxSep17``."""
    m = re.search(r"cbexams\.com/+([^/]+)/", url or "", re.I)
    return m.group(1) if m else None


# ---------------------------------------------------------------- questions
def _split_by_lang(markup: str, assets) -> dict[str, str]:
    """Split a cell holding ``<img EN/><br/><br/><img HI/>`` into per-language markup."""
    ids = image_ids(markup)
    langs = {assets[i].lang for i in ids if i in assets and assets[i].lang}
    if len(langs) <= 1:
        return {(langs.pop() if langs else "en"): markup}
    out: dict[str, list[str]] = {}
    for i in ids:
        lang = assets[i].lang if i in assets else None
        if lang:
            out.setdefault(lang, []).append(f'<img id="{i}"/>')
    return {k: "<br/>".join(v) for k, v in out.items()}


def _parse_question(table, qfont, number: int, rich: RichText) -> Question | None:
    rows = table.xpath("./tr | ./tbody/tr")
    qrow = next((r for r in rows if qfont in r.iter()), None)
    if qrow is None:
        return None
    qtds = qrow.xpath("./td")
    q_markup = "<br/>".join(rich.build(td) for td in qtds[1:])
    for asset_id in image_ids(q_markup):
        rich.assets[asset_id].kind = "strip"

    options: list[str] = []
    correct = chosen = None
    not_answered = False
    for r in rows[rows.index(qrow) + 1:]:
        tds = r.xpath("./td")
        if not tds:
            continue
        colour = (tds[0].get("bgcolor") or "").lower()
        if len(tds) == 1 or "not answered" in norm_space(r.text_content()).lower():
            not_answered = not_answered or "not answered" in norm_space(r.text_content()).lower()
            continue
        body = rich.build(tds[1])
        if not body:
            continue
        idx = len(options)
        options.append(body)
        for asset_id in image_ids(body):
            rich.assets[asset_id].kind = "strip"
        if colour == "green":
            correct = chosen = idx
        elif colour == "red":
            chosen = idx
        elif colour == "yellow":
            correct = idx

    issues = rich.take_issues()
    if not options:
        issues.append("no_options")
    if correct is None:
        issues.append("correct_count:0")

    # per-language content (bilingual pages carry EN + HI images in the same cells)
    q_by_lang = _split_by_lang(q_markup, rich.assets)
    opts_by_lang = [_split_by_lang(o, rich.assets) for o in options]
    langs = [lang for lang in ("en", "hi") if lang in q_by_lang and all(lang in o for o in opts_by_lang)] or list(q_by_lang)[:1]
    lang_content = {
        lang: LangContent(question=q_by_lang.get(lang, q_markup), options=[o.get(lang) or next(iter(o.values())) for o in opts_by_lang])
        for lang in langs
    }
    status = ANSWERED if chosen is not None else NOT_ANSWERED
    if not_answered and chosen is not None:
        issues.append("chosen_and_not_answered")
    return Question(number=number, lang=lang_content, correct=correct, chosen=chosen, status=status, issues=issues)
