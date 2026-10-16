"""Mongo-style update operators and filters applied to plain dicts (spec §9.2).

The repository sends the **same** update document to MongoDB and to the JSON mirror; this module is what
applies it on the JSON side, so both stores always end in the same state.

Depends on: stdlib. Used by: ``db/repo.py``, ``db/json_backend.py`` (fallback queries), tests.

Public API
----------
- ``apply_update(doc, update, *, is_insert) -> dict`` — returns a new dict (input untouched). **Copy-on-write**
  (load test 2026-10-13: a quiz with 10 000 players cost ~0.5 s per finished play when every update deep-copied the
  whole document): only the dicts / lists on the changed paths are copied; the rest is shared with ``doc``. Neither
  is ever changed in place afterwards (the repository treats stored documents as read-only), so sharing is safe.
  Operators: ``$set $unset $inc $max $min $setOnInsert $push($each,$slice) $addToSet($each) $pull``.
  A document without ``$`` keys is a full replacement (``_id`` kept).
- ``matches(doc, flt) -> bool`` — equality on dotted paths (an array field matches a scalar it contains, as in
  Mongo) plus ``$exists $ne $in $nin $gt $gte $lt $lte``.
- ``get_path(doc, "a.b.c")``

Exceptions → outcome
--------------------
- ``UpdateError``: unknown operator / type clash (e.g. ``$inc`` on a string) — a programming error; the
  repository logs it and re-raises, the task is cancelled with an error ID.
"""

from __future__ import annotations

import copy
from typing import Any

_MISSING = object()


class UpdateError(ValueError):
    pass


def get_path(doc: dict, path: str, default: Any = None) -> Any:
    """Value at a dotted path (``players.123.ranked.score``) or ``default``."""
    cur: Any = doc
    for part in path.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        elif isinstance(cur, list) and part.isdigit() and int(part) < len(cur):
            cur = cur[int(part)]
        else:
            return default
    return cur


def _own(parent: Any, key: Any, owned: set | None) -> Any:
    """``parent[key]`` as a container this update may change: copied (shallow) once when it is shared with the old
    document. ``owned`` = ids of the containers already copied (``None``: everything is ours)."""
    child = parent[key]
    if owned is not None and id(child) not in owned and isinstance(child, (dict, list)):
        child = dict(child) if isinstance(child, dict) else list(child)
        parent[key] = child
        owned.add(id(child))
    return child


def _parent(doc: dict, path: str, create: bool, owned: set | None = None) -> tuple[Any, str]:
    parts = path.split(".")
    cur: Any = doc
    for part in parts[:-1]:
        if isinstance(cur, list) and part.isdigit():
            cur = _own(cur, int(part), owned)
            continue
        if part not in cur or not isinstance(cur[part], (dict, list)):
            if not create:
                return None, parts[-1]
            cur[part] = {}
            if owned is not None:
                owned.add(id(cur[part]))
        cur = _own(cur, part, owned)
    return cur, parts[-1]


def _set(doc: dict, path: str, value: Any, owned: set | None = None) -> None:
    parent, key = _parent(doc, path, create=True, owned=owned)
    if isinstance(parent, list):
        parent[int(key)] = value
    else:
        parent[key] = value


def apply_update(doc: dict | None, update: dict, *, is_insert: bool = False) -> dict:
    """Apply ``update`` to a copy of ``doc`` (``None`` = new document) — copy-on-write, see the module doc."""
    if not any(k.startswith("$") for k in update):
        new = copy.deepcopy(update)
        if doc and "_id" in doc:
            new["_id"] = doc["_id"]
        return new
    out = dict(doc) if doc else {}
    owned = {id(out)}
    for op, fields in update.items():
        if not isinstance(fields, dict):
            raise UpdateError(f"{op} expects a document")
        for path, value in fields.items():
            _apply_op(out, op, path, value, is_insert, owned)
    return out


def _apply_op(out: dict, op: str, path: str, value: Any, is_insert: bool, owned: set | None = None) -> None:
    current = get_path(out, path, _MISSING)
    if op == "$set":
        _set(out, path, copy.deepcopy(value), owned)
    elif op == "$setOnInsert":
        if is_insert:
            _set(out, path, copy.deepcopy(value), owned)
    elif op == "$unset":
        parent, key = _parent(out, path, create=False, owned=owned)
        if isinstance(parent, dict):
            parent.pop(key, None)
    elif op == "$inc":
        if current is _MISSING:
            _set(out, path, value, owned)
        elif isinstance(current, (int, float)) and not isinstance(current, bool):
            _set(out, path, current + value, owned)
        else:
            raise UpdateError(f"$inc on non-number at {path}")
    elif op in ("$max", "$min"):
        if current is _MISSING or (value > current if op == "$max" else value < current):
            _set(out, path, value, owned)
    elif op in ("$push", "$addToSet"):
        arr = [] if current is _MISSING else current
        if not isinstance(arr, list):
            raise UpdateError(f"{op} on non-array at {path}")
        arr = list(arr)
        each = value["$each"] if isinstance(value, dict) and "$each" in value else [value]
        for v in each:
            if op == "$push" or v not in arr:
                arr.append(copy.deepcopy(v))
        if op == "$push" and isinstance(value, dict) and "$slice" in value:
            n = value["$slice"]
            arr = arr[n:] if n < 0 else arr[:n]
        _set(out, path, arr, owned)
    elif op == "$pull":
        if isinstance(current, list):
            _set(out, path, [v for v in current if not (matches(v, value) if isinstance(value, dict) and isinstance(v, dict) else v == value)])
    else:
        raise UpdateError(f"unsupported operator {op}")


def matches(doc: dict, flt: dict | None) -> bool:
    """Small Mongo filter subset used by conditional updates and JSON-fallback queries."""
    if not flt:
        return True
    for path, cond in flt.items():
        value = get_path(doc, path, _MISSING)
        if isinstance(cond, dict) and cond and all(k.startswith("$") for k in cond):
            for op, arg in cond.items():
                if not _cmp(op, value, arg):
                    return False
        elif value is _MISSING or not _equal(value, cond):
            return False
    return True


def _equal(value: Any, cond: Any) -> bool:
    """Mongo equality: an array field also matches when one of its elements equals a scalar (B-028:
    ``{"submitted_by": uid}`` found nothing in the JSON mirror)."""
    return value == cond or (isinstance(value, list) and not isinstance(cond, list) and cond in value)


def _cmp(op: str, value: Any, arg: Any) -> bool:
    if op == "$exists":
        return (value is not _MISSING) == bool(arg)
    if op == "$ne":
        return value is _MISSING or value != arg
    if op == "$in":
        return value is not _MISSING and value in arg
    if op == "$nin":
        return value is _MISSING or value not in arg
    if value is _MISSING or value is None:
        return False
    try:
        return {"$gt": value > arg, "$gte": value >= arg, "$lt": value < arg, "$lte": value <= arg}[op]
    except KeyError as e:
        raise UpdateError(f"unsupported filter operator {op}") from e
    except TypeError:
        return False
