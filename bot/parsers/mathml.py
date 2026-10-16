"""MathML (Wiris ``data-mathml``) → mini-markup text (D-004, spec §12.2).

Depends on: lxml. Used by: ``parsers/common.py``.

Public API
----------
- ``decode_wiris(s)`` — Wiris stores MathML with ``« » ¨ § ` `` instead of ``< > " & '``.
- ``to_markup(mathml) -> str | None`` — simple formulas become text with ``<sup>/<sub>``
  (e.g. ``5√2``, ``x<sup>2</sup>``, ``(a+b)/(c)``). Returns ``None`` for anything not covered
  (tables, under/over scripts, unknown tags, parse errors) so the caller keeps the image.

Never raises.
"""

from __future__ import annotations

from html import escape

from lxml import etree

_WIRIS = str.maketrans({"«": "<", "»": ">", "¨": '"', "§": "&", "`": "'"})
_TEXT_TAGS = {"mn", "mi", "mo", "mtext", "ms"}
_GROUP_TAGS = {"math", "mrow", "mstyle", "mpadded", "mphantom", "semantics", "merror"}


class _Unsupported(Exception):
    pass


def decode_wiris(s: str) -> str:
    """Undo Wiris' attribute-safe encoding of MathML."""
    return s.translate(_WIRIS)


def to_markup(mathml: str) -> str | None:
    """Convert a MathML string to mini-markup, or ``None`` when it is not simple enough."""
    try:
        root = etree.fromstring(mathml.encode("utf-8"), parser=etree.XMLParser(recover=False, resolve_entities=False))
        out = _conv(root).strip()
        return out or None
    except (_Unsupported, etree.XMLSyntaxError, ValueError, RecursionError):
        return None


def _local(el) -> str:
    return etree.QName(el).localname if isinstance(el.tag, str) else ""


def _is_atom(markup: str) -> bool:
    """True when ``markup`` needs no brackets in a fraction/root (single number or letter run)."""
    plain = markup.replace("<sup>", "").replace("</sup>", "").replace("<sub>", "").replace("</sub>", "")
    return plain.replace(".", "").isalnum()


def _wrap(markup: str) -> str:
    return markup if _is_atom(markup) else f"({markup})"


def _conv(el) -> str:
    tag = _local(el)
    kids = [c for c in el if isinstance(c.tag, str)]
    if tag in _TEXT_TAGS:
        if kids:
            raise _Unsupported(tag)
        # keep spacing: Wiris writes word gaps as <mo>&#160;</mo>; str.strip() would delete NBSP too
        return escape((el.text or "").replace("\xa0", " ").replace("\n", " "), quote=False)
    if tag == "mspace":
        return " "
    if tag in _GROUP_TAGS:
        if tag == "semantics":
            kids = kids[:1]  # annotations follow the presentation tree
        return "".join(_conv(k) for k in kids)
    if tag == "msqrt":
        return "√" + _wrap("".join(_conv(k) for k in kids))
    if tag == "mroot" and len(kids) == 2:
        return f"<sup>{_conv(kids[1])}</sup>√{_wrap(_conv(kids[0]))}"
    if tag == "msup" and len(kids) == 2:
        return f"{_conv(kids[0])}<sup>{_conv(kids[1])}</sup>"
    if tag == "msub" and len(kids) == 2:
        return f"{_conv(kids[0])}<sub>{_conv(kids[1])}</sub>"
    if tag == "msubsup" and len(kids) == 3:
        return f"{_conv(kids[0])}<sub>{_conv(kids[1])}</sub><sup>{_conv(kids[2])}</sup>"
    if tag == "mfrac" and len(kids) == 2:
        return f"{_wrap(_conv(kids[0]))}/{_wrap(_conv(kids[1]))}"
    if tag == "mfenced":
        opn, cls = el.get("open", "("), el.get("close", ")")
        sep = el.get("separators", ",")
        sep = sep.strip()[:1] if sep.strip() else ""
        return escape(opn, quote=False) + sep.join(_conv(k) for k in kids) + escape(cls, quote=False)
    raise _Unsupported(tag or "?")
