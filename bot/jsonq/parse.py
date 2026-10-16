"""Question JSON files (``/json``) → an exam bundle (spec §16.8). Pure functions (pool jobs): no Telegram / DB / network.

Format (the owner's ``JSONs/`` folders — 947 files, 35 929 questions, checked 2026-10-13)::

    {"title": "17 Sep 2026 Shift 2 (HSL) Maths", "series_title": …, "exam_name": …, "total_questions": 25,
     "is_shuffled": true, "saved_at": …,
     "questions": [{"serial_no": 1, "answer_number": "2", "correct_option": "B",
                    "en": {"question": "<html>…", "option_1": …, … "option_10": …, "option_image_1": …,
                           "image_link_1": …, "answer": "2", "solution_text": "<p>…", "solution_image_1": …,
                           "question_heading": …, "directive": …, "positive_marks": …, …},
                    "hi": {…the same in Hindi…}}]}

- Text is HTML (``<p>``, ``<span style=…>``, lists, tables; ``@font-face`` ``<style>`` blocks are dropped) → the bot's
  mini-markup (bold / italic / underline from ``style``, ``•`` / ``1.`` for list items).
- **Tables** (owner 2026-10-15: explanation tables came out one cell per line) → ``<tbl>row<br/>row</tbl>``, one line
  per row, cells joined by `` | `` (a cell's own lines joined by spaces; a ``|`` with spaces around it inside a cell →
  ``¦``). The PDF and the quiz cards draw it as a real table (``render.exam_pdf.segments``); everything else drops the
  unknown tag and shows the rows as text lines. A table of one row or one column stays plain lines. ``PARSE_V`` 2 —
  a stored JSON bank of an older version whose file has tables is built again (``jsonq.runner``).
- Images are ``<img src="https://…">`` links: downloaded first by ``jsonq.fetch`` (``files`` = url → (path, mime)) and
  stored in the bundle like a sheet's images (processed by ``exam.imgproc``); one that could not be downloaded prints
  ``[image not available]``.
- **Each language keeps its own answer** (``answer`` of that language — 305 questions have a different option order in
  English and Hindi) and its **explanation** (``solution_text``). When both languages show the very same options but
  name different answers, the English one is taken (the Hindi key was found wrong in such cases).
- Files whose "English" and "Hindi" parts are the same text (both languages already in one) are one language
  (``summary()["same"]``): one PDF / quiz, not two copies.
- Owner 2026-10-13: the file's series name, time and marks are **not used** — only the ``title`` names the paper.
- **Exam name** (owner 2026-10-15: "exam name nhi lia … usko bhi sab jgh dalo"): the file's ``exam_name`` is shown under
  the title (PDF banner and caption, quiz cards and list, the ``/json`` card) as ``exam_label`` — cleaned of sellers'
  words (owner: "mock, test, rbe, parmar, rojgar jaise word hai to hta dena": mock(s), test(s), test series, RBE,
  Parmar, Rojgar (with Ankit), a series version like ``3.0``); nothing when that leaves nothing or the title already
  says it.

Public API
----------
- ``load(path) -> dict`` (``ValueError`` when the file is not a question JSON) · ``detect(obj) -> bool``.
- ``summary(obj) -> dict`` — title, exam_label, questions, languages {lang: count}, ``same`` (English part = Hindi
  part), images, explanations, unusable, tables (questions with a table), fingerprint.
- ``exam_label(obj) -> str`` — the cleaned ``exam_name`` (``""`` when none).
- ``image_urls(obj) -> list[str]`` — every image link, in order, once.
- ``fingerprint(obj) -> str`` — SHA-1 of the questions' content (not the title / series / save time).
- ``to_exam(obj, files) -> ExamData`` · ``build_bundle(json_path, files_json, out_zip) -> dict`` (pool job).
- ``inspect_file(path)`` / ``inspect_zip(zip_path, out_dir, room)`` (pool jobs) — the ``/json`` and ``/json_batch``
  cards; ``zip_json_count(zip_path)`` sizes the ``inspect_zip`` time limit.
- ``inspect_any(path, work_dir)`` (pool job) — question JSON · exam bundle / exam JSON (``inspect_bundle``) · a stored
  quiz (``is_quiz_doc``) — ``/json`` and ``/load_quiz``.

Depends on: lxml, ``parsers.common`` (``RichText``), ``exam.bundle``. Used by: ``jsonq.runner``, ``handlers.jsoncmd``.
"""

from __future__ import annotations

import hashlib
import re
from html import escape
from pathlib import Path

import orjson

from bot.parsers.common import (_BLOCK, _INLINE, _SKIP, RichText, _invisible_text, _not_rendered, strip_option_label,
                                tidy)
from bot.parsers.models import Candidate, ExamData, ExamMeta, LangContent, Question, Section, plain

LANGS = ("en", "hi")
MAX_OPTIONS = 10
SECTION = "Questions"
_IMG_SRC = re.compile(r"""<img\b[^>]*?\bsrc\s*=\s*["']([^"']+)["']""", re.I)
_DROP = re.compile(r"<(style|script|head)\b.*?</\1\s*>", re.I | re.S)
_WRAP = re.compile(r"</?(html|body|!doctype)\b[^>]*>", re.I)
_STYLES = ((re.compile(r"font-weight\s*:\s*(bold|[6-9]00)", re.I), "b"), (re.compile(r"font-style\s*:\s*italic", re.I), "i"),
           (re.compile(r"text-decoration[^;]*underline", re.I), "u"), (re.compile(r"vertical-align\s*:\s*super", re.I), "sup"),
           (re.compile(r"vertical-align\s*:\s*sub\b", re.I), "sub"))
_URL_FIELDS = ("option_image_", "image_link_", "solution_image_")
MISSING = "<i>[image not available]</i>"
PARSE_V = 2  # 2 = tables as <tbl> rows (2026-10-15); banks built by an older version are rebuilt when the file has tables


# ------------------------------------------------------------------ reading
def load(path) -> dict:
    obj = orjson.loads(Path(path).read_bytes())
    if not detect(obj):
        raise ValueError("not a question JSON")
    return obj


def detect(obj) -> bool:
    """A dict with ``questions`` whose items carry an ``en`` / ``hi`` part with a question and options."""
    if not isinstance(obj, dict) or not isinstance(obj.get("questions"), list) or not obj["questions"]:
        return False
    for q in obj["questions"][:5]:
        if not isinstance(q, dict):
            return False
        parts = [q.get(x) for x in LANGS if isinstance(q.get(x), dict)]
        if parts and any("question" in p and "option_1" in p for p in parts):
            return True
    return False


def _parts(q: dict) -> dict[str, dict]:
    return {x: q[x] for x in LANGS if isinstance(q.get(x), dict) and (_s(q[x].get("question")) or _s(q[x].get("option_1")))}


def _s(v) -> str:
    return v.strip() if isinstance(v, str) else ""


def title_of(obj: dict) -> str:
    t = re.sub(r"\s+", " ", _s(obj.get("title")))
    return t[:150] or "Questions"


_SELLER = re.compile(r"\b(?:(?:mock\s*)?tests?\s*series|mock\s*tests?|mocks?|tests?|rbe|parmar|rojgar(?:\s+with\s+ankit)?)\b"
                     r"|\b\d+\.0\b", re.I)


def exam_label(obj: dict) -> str:
    """The file's ``exam_name`` without sellers' words (mock, test, RBE, Parmar, Rojgar …) and series versions."""
    s = re.sub(r"\(\s*\)", " ", _SELLER.sub(" ", _s(obj.get("exam_name"))))
    s = re.sub(r"\s+", " ", s).strip(" -–—|&,:·/")
    s = re.sub(r"\s+", " ", s).strip()
    if not s or s.lower() in title_of(obj).lower():
        return ""
    return s[:80]


def image_urls(obj: dict) -> list[str]:
    seen: dict[str, None] = {}
    for q in obj.get("questions") or []:
        if not isinstance(q, dict):
            continue
        for p in _parts(q).values():
            for k, v in p.items():
                if not isinstance(v, str) or not v:
                    continue
                if k.startswith(_URL_FIELDS):
                    if v.strip().lower().startswith(("http://", "https://")):
                        seen.setdefault(v.strip(), None)
                elif "<img" in v.lower():
                    for u in _IMG_SRC.findall(v):
                        u = u.strip()
                        if u.lower().startswith(("http://", "https://")):
                            seen.setdefault(u, None)
    return list(seen)


def fingerprint(obj: dict) -> str:
    keep = ("question", "answer", "solution_text", "directive", "question_heading")
    rows = []
    for q in obj.get("questions") or []:
        if not isinstance(q, dict):
            continue
        row = {"a": str(q.get("answer_number") or "")}
        for lang, p in sorted(_parts(q).items()):
            row[lang] = {k: v for k, v in sorted(p.items()) if isinstance(v, str) and v and
                         (k in keep or k.startswith(("option_",) + _URL_FIELDS))}
        rows.append(row)
    return hashlib.sha1(orjson.dumps(rows, option=orjson.OPT_SORT_KEYS)).hexdigest()


def _answer(p: dict, q: dict, n: int) -> int | None:
    for v in (p.get("answer"), q.get("answer_number")):
        try:
            i = int(str(v).strip()) - 1
        except (TypeError, ValueError):
            continue
        if 0 <= i < n:
            return i
    return None


def _option_slots(p: dict) -> int:
    last = 0
    for i in range(1, MAX_OPTIONS + 1):
        if _plainish(p.get(f"option_{i}")) or _s(p.get(f"option_image_{i}")):
            last = i
    return last


def _plainish(v) -> bool:
    """Has visible content: text after removing tags / style blocks, or an image."""
    if not isinstance(v, str) or not v.strip():
        return False
    if "<img" in v.lower():
        return True
    return bool(re.sub(r"\s+|&nbsp;", "", re.sub(r"<[^>]+>", "", _DROP.sub("", v))))


def _text(v) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>|&nbsp;", " ", _DROP.sub("", v if isinstance(v, str) else ""))).strip()


def _same(parts: dict) -> bool:
    en, hi = parts.get("en"), parts.get("hi")
    if not (en and hi):
        return False
    return all(_text(en.get(k)) == _text(hi.get(k)) for k in ("question", *(f"option_{i}" for i in range(1, 5))))


def summary(obj: dict) -> dict:
    langs = {x: 0 for x in LANGS}
    expl = unusable = both = same = tables = 0
    for q in obj.get("questions") or []:
        if not isinstance(q, dict):
            unusable += 1
            continue
        parts = _parts(q)
        ok = False
        for lang, p in parts.items():
            n = _option_slots(p)
            if n >= 2 and _answer(p, q, n) is not None:
                langs[lang] += 1
                ok = True
        if not ok:
            unusable += 1
        if any(_plainish(p.get("solution_text")) for p in parts.values()):
            expl += 1
        if any(isinstance(v, str) and "<table" in v.lower() for p in parts.values() for v in p.values()):
            tables += 1
        if len(parts) == 2:
            both += 1
            same += _same(parts)
    return {"title": title_of(obj), "exam_label": exam_label(obj), "questions": len(obj.get("questions") or []), "languages": {k: v for k, v in langs.items() if v},
            "same": bool(both) and same >= 0.9 * both,
            "images": len(image_urls(obj)), "explanations": expl, "unusable": unusable, "tables": tables,
            "fingerprint": fingerprint(obj)}


# ------------------------------------------------------------------ HTML → mini-markup
class _Files:
    """The downloaded images by URL, in the shape ``RichText`` reads (``asset(src) -> (bytes, mime)``)."""

    def __init__(self, files: dict) -> None:
        self.files = files

    def asset(self, src: str | None):
        hit = self.files.get((src or "").strip())
        if not hit:
            return None
        try:
            return Path(hit[0]).read_bytes(), hit[1]
        except OSError:
            return None


class _Rich(RichText):
    """``RichText`` for these files: ``style`` → bold / italic / underline / sup / sub, list items numbered or
    bulleted, a missing image → a visible note."""

    def __init__(self, *a, **kw) -> None:
        super().__init__(*a, **kw)
        self.display: dict[str, tuple[float, float]] = {}  # asset id → the HTML's width / height (CSS px)

    def image(self, img) -> str:
        out = super().image(img)
        if not out and self.issues and self.issues[-1] == "missing_image":
            return MISSING
        m = re.match(r'<img id="([^"]+)"', out)
        if m and m.group(1) not in self.display:
            try:
                w, h = float(img.get("width") or 0), float(img.get("height") or 0)
            except ValueError:
                w = h = 0
            if 0 < w <= 2000:
                self.display[m.group(1)] = (w, h)
        return out

    def _walk(self, el, hide_text: bool = False) -> str:
        parts: list[str] = []
        if el.text and not hide_text:
            parts.append(escape(el.text, quote=False))
        n_li = 0
        parent = el.tag.lower() if isinstance(el.tag, str) else ""
        for child in el:
            tag = child.tag.lower() if isinstance(child.tag, str) else ""
            if tag and tag not in _SKIP and not _not_rendered(child):
                hidden = hide_text or _invisible_text(child)
                if tag == "br":
                    parts.append("<br/>")
                elif tag == "img":
                    parts.append(self.image(child))
                elif tag == "li":
                    n_li += 1
                    mark = f"{n_li}. " if parent == "ol" else "• "
                    parts.append("<br/>" + mark + self._styled(child, self._walk(child, hidden)) + "<br/>")
                elif tag in _INLINE:
                    inner = self._styled(child, self._walk(child, hidden))
                    if inner.strip():
                        t = _INLINE[tag]
                        parts.append(f"<{t}>{inner}</{t}>")
                elif tag in ("td", "th"):
                    parts.append(self._styled(child, self._walk(child, hidden)) + " | ")
                elif tag == "table":
                    parts.append(self._table(child, hidden))
                elif tag in _BLOCK:
                    parts.append("<br/>" + self._styled(child, self._walk(child, hidden)) + "<br/>")
                else:  # span, font, a …
                    parts.append(self._styled(child, self._walk(child, hidden)))
            if child.tail and not hide_text:
                parts.append(escape(child.tail, quote=False))
        return "".join(parts)

    def _table(self, el, hidden: bool) -> str:
        """``<tbl>`` rows (cells joined by `` | ``); one row or one column → plain lines."""
        rows = []
        for tr in el.xpath("./tr|./thead/tr|./tbody/tr|./tfoot/tr"):
            if _not_rendered(tr):
                continue
            cells = []
            for td in tr:
                tag = td.tag.lower() if isinstance(td.tag, str) else ""
                if tag not in ("td", "th") or _not_rendered(td):
                    continue
                inner = tidy(self._styled(td, self._walk(td, hidden or _invisible_text(td))))
                inner = re.sub(r"<tbl>(.*?)</tbl>", lambda m: m.group(1).replace(" | ", ", ").replace("<br/>", "; "), inner)
                inner = re.sub(r"(?<=\s)\|(?=\s)|^\|(?=\s)|(?<=\s)\|$", "¦", inner.replace("<br/>", " ")).strip()
                cells.append(inner)
                try:
                    span = min(int(td.get("colspan") or 1), 20)
                except ValueError:
                    span = 1
                cells += [""] * (span - 1)
            if any(c for c in cells):
                rows.append(cells)
        if not rows:
            return ""
        if len(rows) == 1 or max(len(r) for r in rows) == 1:
            return "<br/>" + "<br/>".join(" | ".join(c for c in r if c) for r in rows) + "<br/>"
        return "<br/><tbl>" + "<br/>".join(" | ".join(r) for r in rows) + "</tbl><br/>"

    @staticmethod
    def _styled(el, inner: str) -> str:
        style = el.get("style") or ""
        if not style or not re.sub(r"<[^>]+>|\s", "", inner):
            return inner
        for pat, t in _STYLES:
            if pat.search(style):
                inner = f"<{t}>{inner}</{t}>"
        return inner


def markup(html_text, rich: _Rich) -> str:
    """One HTML field → mini-markup (``""`` for nothing visible)."""
    from lxml import html as lxml_html

    if not isinstance(html_text, str) or not html_text.strip():
        return ""
    s = _WRAP.sub("", _DROP.sub("", html_text))
    s = re.sub(r"<style\b[^>]*>.*$", "", s, flags=re.I | re.S)  # an unclosed style block
    if not s.strip():
        return ""
    try:
        root = lxml_html.fragment_fromstring(s, create_parent="div")
    except Exception:
        return tidy(escape(re.sub(r"<[^>]+>", " ", s), quote=False))
    out = rich.build(root)
    return re.sub(r"<(sup|sub|b|i|u)>\s*</\1>", "", out)


def _url_img(url: str, rich: _Rich) -> str:
    from lxml import html as lxml_html
    return rich.image(lxml_html.fromstring(f'<img src="{escape(url)}">'))


def to_exam(obj: dict, files: dict, display: dict | None = None) -> ExamData:
    """``files``: image URL → (local path, mime) from ``jsonq.fetch``; ``display`` (optional, filled): asset id →
    the HTML's display size."""
    assets: dict = {}
    rich = _Rich(_Files(files), assets)
    if display is not None:
        rich.display = display
    sec = Section(SECTION)
    langs_seen: set[str] = set()
    for pos, q in enumerate(obj.get("questions") or [], 1):
        if not isinstance(q, dict):
            continue
        lang: dict[str, LangContent] = {}
        for code, p in _parts(q).items():
            n = _option_slots(p)
            if n < 2:
                continue
            head = "<br/>".join(x for x in (markup(p.get("directive"), rich), markup(p.get("question_heading"), rich)) if x)
            body = markup(p.get("question"), rich)
            for i in (1, 2, 3):
                u = _s(p.get(f"image_link_{i}"))
                if u.lower().startswith(("http://", "https://")):
                    body += "<br/>" + _url_img(u, rich)
            question = tidy("<br/>".join(x for x in (head, body) if x))
            options = []
            for i in range(1, n + 1):
                o = strip_option_label(markup(p.get(f"option_{i}"), rich), i - 1)
                u = _s(p.get(f"option_image_{i}"))
                if u.lower().startswith(("http://", "https://")):
                    o = (o + " " if o else "") + _url_img(u, rich)
                options.append(tidy(o))
            expl = markup(p.get("solution_text"), rich)
            for i in (1, 2):
                u = _s(p.get(f"solution_image_{i}"))
                if u.lower().startswith(("http://", "https://")):
                    expl += "<br/>" + _url_img(u, rich)
            if not question:
                continue
            lang[code] = LangContent(question, options, tidy(expl), _answer(p, q, n))
        rich.take_issues()
        if not lang or all(c.correct is None for c in lang.values()):  # no answer anywhere: left out (the card says so)
            continue
        en, hi = lang.get("en"), lang.get("hi")
        same_options = en and hi and [plain(o) for o in en.options] == [plain(o) for o in hi.options]
        if same_options and en.correct is not None and en.correct != hi.correct:
            hi.correct = en.correct
        first = lang.get("en") or next(iter(lang.values()))
        try:
            number = int(q.get("serial_no") or pos)
        except (TypeError, ValueError):
            number = pos
        sec.questions.append(Question(number=number, lang=lang, correct=first.correct, qid=f"j{pos}"))
        langs_seen.update(lang)
    meta = ExamMeta(exam_name=title_of(obj), source="json", languages=sorted(langs_seen), name_source="heading")
    return ExamData(meta=meta, candidate=Candidate(), sections=[sec], assets=assets)


def build_bundle(json_path: str, files_json: str, out_zip: str) -> dict:
    """Pool job: the JSON + its downloaded images → an exam bundle (the same zip a sheet makes)."""
    from bot.exam.bundle import write_bundle_data

    obj = load(json_path)
    raw = orjson.loads(Path(files_json).read_bytes()) if files_json else {}
    files = {u: (v[0], v[1]) for u, v in raw.items()}
    display: dict = {}
    data = to_exam(obj, files, display)
    if not data.n_questions:
        raise ValueError("no usable question")
    info = write_bundle_data(data, out_zip, display=display)
    info["languages"] = data.meta.languages
    info["sections"] = [[s.name, len(s.questions)] for s in data.sections]
    return info


def inspect_file(path: str) -> dict:
    """Pool job for the ``/json`` card: ``summary`` + the image links (``urls``)."""
    obj = load(path)
    s = summary(obj)
    s["urls"] = image_urls(obj)
    return s


ZIP_FILE_MAX = 30 * 1024 * 1024  # one question JSON (a real one is ≤ 2 MB); no limit on the number of files


def _natural(name: str) -> list:
    return [int(x) if x.isdigit() else x.lower() for x in re.split(r"(\d+)", name)]


def _json_entries(z) -> list:
    return [i for i in z.infolist() if not i.is_dir() and i.filename.lower().endswith(".json")
            and "__MACOSX/" not in i.filename and not Path(i.filename).name.startswith(".")]


def zip_json_count(zip_path: str) -> int:
    """How many ``.json`` files ``inspect_zip`` will read (the central directory only — milliseconds)."""
    import zipfile

    with zipfile.ZipFile(zip_path) as z:
        return len(_json_entries(z))


def inspect_zip(zip_path: str, out_dir: str, room: int | None = None) -> dict:
    """Pool job for ``/json_batch``: every ``.json`` in the zip (folders too, in natural name order) is copied out as
    ``<out_dir>/0001.json`` … (entry names are never used as paths) and read like ``inspect_file``.

    Returns ``{"files": [{"name", "path", "info"}], "bad": [names not readable], "big": [names over the limits],
    "dups": [names whose questions repeat an earlier file], "entries": n .json entries}``. Any number of files
    (owner 2026-10-14: "json_batch ki command se 300 json etc ki limit hta do"); ``ZIP_FILE_MAX`` per file and
    ``room`` bytes in all (the free disk the caller leaves; ``None`` = no total) — checked while unpacking, so a
    zip bomb stops early."""
    import zipfile

    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    files, bad, big, dups, seen = [], [], [], [], set()
    total = 0
    with zipfile.ZipFile(zip_path) as z:
        entries = _json_entries(z)
        entries.sort(key=lambda i: _natural(i.filename))
        for k, info in enumerate(entries, 1):
            name = Path(info.filename).name
            if info.file_size > ZIP_FILE_MAX or (room is not None and total + info.file_size > room):
                big.append(name)
                continue
            dest = out / f"{k:04d}.json"
            size, ok = 0, True
            try:
                with z.open(info) as src, open(dest, "wb") as dst:
                    while chunk := src.read(1 << 20):
                        size += len(chunk)
                        if size > ZIP_FILE_MAX or (room is not None and total + size > room):
                            ok = False
                            break
                        dst.write(chunk)
            except Exception:  # encrypted / broken entry
                dest.unlink(missing_ok=True)
                bad.append(name)
                continue
            if not ok:
                dest.unlink(missing_ok=True)
                big.append(name)
                continue
            total += size
            try:
                s = inspect_file(str(dest))
            except Exception:
                dest.unlink(missing_ok=True)
                bad.append(name)
                continue
            if s["fingerprint"] in seen:
                dest.unlink(missing_ok=True)
                dups.append(name)
                continue
            seen.add(s["fingerprint"])
            files.append({"name": name, "path": str(dest), "info": s})
    return {"files": files, "bad": bad, "big": big, "dups": dups, "entries": len(entries)}


# ------------------------------------------------------------------ other files /json and /load_quiz take
def is_quiz_doc(obj) -> bool:
    """A quiz as the bot stores it (the ``qz_….json`` it sends to the main dump)."""
    if not isinstance(obj, dict) or not str(obj.get("_id") or "").startswith("qz_"):
        return False
    qs = obj.get("questions")
    return isinstance(qs, list) and bool(qs) and isinstance(qs[0], dict) and "c" in qs[0] and "mode" in qs[0]


def is_exam_doc(obj) -> bool:
    """An exam JSON (spec §12.3): ``schema_version`` + ``sections`` of questions with ``lang`` parts."""
    if not isinstance(obj, dict) or "schema_version" not in obj or not isinstance(obj.get("sections"), list):
        return False
    return any(isinstance(s, dict) and isinstance(s.get("questions"), list) for s in obj["sections"])


def _bundle_title(meta: dict) -> str:
    bits = [str(meta.get(k) or "").strip() for k in ("exam_name", "date")]
    shift = meta.get("shift")
    if shift not in (None, ""):
        bits.append(f"Shift {shift}")
    return re.sub(r"\s+", " ", " · ".join(b for b in bits if b))[:150] or "Exam"


def inspect_bundle(zip_path: str) -> dict:
    """Pool job: an exam bundle (``exam.json`` + ``img/``, the zip the bot keeps for every exam) → the same summary
    ``inspect_file`` gives, plus ``bundle`` (its path) and ``sections`` [(name, count)]. Its own answer key per
    language counts (``schema.correct_of``)."""
    import zipfile

    from bot.exam.schema import correct_of

    with zipfile.ZipFile(zip_path) as z:
        doc = orjson.loads(z.read("exam.json"))
        names = set(z.namelist())
    if not is_exam_doc(doc):
        raise ValueError("not an exam JSON")
    meta = doc.get("meta") or {}
    langs = {x: 0 for x in LANGS}
    expl = unusable = total = 0
    files, rows, sections = set(), [], []
    for sec in doc["sections"]:
        qs = [q for q in sec.get("questions") or [] if isinstance(q, dict)]
        sections.append([str(sec.get("name") or SECTION), len(qs)])
        for q in qs:
            total += 1
            ok = False
            parts = q.get("lang") if isinstance(q.get("lang"), dict) else {}
            for lang, c in parts.items():
                if lang not in langs or not isinstance(c, dict):
                    continue
                n = len(c.get("options") or [])
                k = correct_of(q, lang)
                if 2 <= n <= MAX_OPTIONS and isinstance(k, int) and 0 <= k < n:
                    langs[lang] += 1
                    ok = True
            unusable += not ok
            expl += any(isinstance(c, dict) and c.get("explanation") for c in parts.values())
            for im in q.get("images") or []:
                if isinstance(im, dict) and im.get("file"):
                    files.add(im["file"])
            rows.append([q.get("qref") or q.get("q_hash"), q.get("correct"), sorted(parts)])
    missing = sorted(f for f in files if f not in names)
    fp = hashlib.sha1(orjson.dumps(["bundle", meta.get("exam_key"), rows])).hexdigest()
    return {"title": _bundle_title(meta), "questions": total, "languages": {k: v for k, v in langs.items() if v},
            "same": False, "images": len(files), "images_missing": len(missing), "explanations": expl,
            "unusable": unusable, "fingerprint": fp, "urls": [], "bundle": str(zip_path), "sections": sections}


def inspect_any(path: str, work_dir: str) -> dict:
    """Pool job behind ``/json`` and ``/load_quiz``: what the sent file is.

    - a question JSON → ``inspect_file`` (``kind="questions"``);
    - an exam bundle ``.zip`` → ``inspect_bundle`` (``kind="exam"``); a bare exam JSON is wrapped into a bundle in
      ``work_dir`` first — only when it needs no image files (``ValueError`` "send the bundle" otherwise);
    - a stored quiz (``qz_….json``) → ``{"kind": "quiz"}`` — ``/load_quiz`` puts it back;
    - else ``ValueError``."""
    import zipfile

    p = Path(path)
    if zipfile.is_zipfile(p):
        with zipfile.ZipFile(p) as z:
            if "exam.json" not in z.namelist():
                raise ValueError("a zip without exam.json")
        return {**inspect_bundle(str(p)), "kind": "exam"}
    obj = orjson.loads(p.read_bytes())
    if detect(obj):
        return {**inspect_file(str(p)), "kind": "questions"}
    if is_quiz_doc(obj):
        return {"kind": "quiz", "title": str(obj.get("title") or "Quiz")[:150], "id": obj["_id"]}
    if is_exam_doc(obj):
        if any(isinstance(im, dict) and im.get("file") for s in obj["sections"] if isinstance(s, dict)
               for q in s.get("questions") or [] if isinstance(q, dict) for im in q.get("images") or []):
            raise ValueError("needs the bundle")
        out = Path(work_dir) / "exam_bundle.zip"
        out.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("exam.json", orjson.dumps(obj))
        return {**inspect_bundle(str(out)), "kind": "exam"}
    raise ValueError("not a question, exam or quiz JSON")
