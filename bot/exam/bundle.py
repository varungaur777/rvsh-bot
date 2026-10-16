"""Exam bundle — the exam JSON **and its images in one .zip** (pool jobs; no Telegram / DB imports).

Why one file (owner-visible decision D-038): an EDQT exam has ≈ 460 question images (Steno ≈ 1250). Uploading each
image to the dump channel as its own message would take 25–60 min per exam under Telegram's ≈ 20 msgs/min channel
limit. One bundle = one upload, one download on reuse; images are stored once per content hash, **processed**
(cropped / enhanced only where needed, ``exam.imgproc``); untouched images keep their original bytes.

Layout::

    exam.json            # exam.schema.build(...) — w / h / content_bbox filled here
    img/<asset_id>.<ext> # stored (JPEG/PNG are already compressed); exam.json is deflated

Public API (all pure functions — paths in, small data out)
----------
- ``sheet_signature(folder, hints) -> (sig, responses)`` — reuse check data (``schema.signature``) + the candidate's
  choices (``schema.responses``) from the task's sheets.
- ``write_bundle_data(data, out_zip, ocr_mode="lazy") -> dict`` — the same for an ``ExamData`` (``/json`` files).
- ``write_bundle(folder, hints, out_zip, ocr_mode="lazy") -> dict`` — parse, **process every image**
  (``exam.imgproc``), build the JSON, write the zip; returns ``{"sig", "questions", "images", "kinds", "bytes", "ms"}``.
- ``merge_bundles(stored, new, out, add_langs) -> dict`` — add a language from another candidate's bundle.
- ``read_doc(zip_path) -> dict`` — the exam JSON from a bundle. ``read_image(zip_path, file) -> bytes``.
- ``content_bbox(data) -> (w, h, [x0, y0, x1, y1] | None)`` — non-white area (tight answer boxes, §15.4).
"""

from __future__ import annotations

import io
import os
import time
import zipfile

import orjson

from bot.exam import schema
from bot.exam.jobs import _load

WHITE = 245  # a pixel lighter than this (0–255 grey) counts as background


def _unprocessed(data: bytes, mime: str) -> tuple[bytes, str, dict]:
    """An image ``imgproc.process`` could not handle — it must not stop the exam: kept as it is; a cut-off one → the
    part that is left (``broken: "part"``) or nothing (``broken: "all"``, a 4×4 white PNG); the PDF prints "image
    could not be read" under it (owner 2026-10-13: "image ka jitna hissa bacha vo lga kr uske niche likh do could
    not be read")."""
    from PIL import Image

    from bot.exam.imgproc import cut_off, process, repair_cut_off

    plain = {"kind": "unknown", "text_px": 0, "scale": 0.75, "lines": [], "enhanced": []}
    if not cut_off(data, mime):
        try:
            w, h, _box = content_bbox(data) if mime != "image/svg+xml" else (0, 0, None)
            return data, mime, {**plain, "w": w, "h": h}
        except Exception:
            pass
    part = repair_cut_off(data)
    if part:
        try:
            raw, mime, info = process(part, "image/png")
        except Exception:
            w, h, _box = content_bbox(part)
            raw, mime, info = part, "image/png", {**plain, "w": w, "h": h}
        # a half-decoded text line measures too small: no text-size scale, no reflow — shown as it is, fitted
        return raw, mime, {**info, "kind": "broken", "broken": "part", "text_px": 0, "scale": 0.75, "lines": []}
    buf = io.BytesIO()
    Image.new("L", (4, 4), 255).save(buf, "PNG")
    return buf.getvalue(), "image/png", {**plain, "kind": "broken", "broken": "all", "w": 4, "h": 4}


def content_bbox(data: bytes) -> tuple[int, int, list[int] | None]:
    from PIL import Image

    with Image.open(io.BytesIO(data)) as im:
        w, h = im.size
        try:
            g = im.convert("L")
            box = g.point(lambda p: 255 if p < WHITE else 0).getbbox()
        except Exception:  # odd formats (svg…): size only
            box = None
    return w, h, list(box) if box else None


def sheet_signature(folder, hints: dict | None) -> tuple[dict, dict]:
    data = _load(folder, hints)
    return schema.signature(data), schema.responses(data)


def write_bundle(folder, hints: dict | None, out_zip, ocr_mode: str = "lazy") -> dict:
    """Parse → every image analysed + enhanced once (``exam.imgproc``: text / math / diagram, text size → display
    scale, reflow gaps, crop, contrast, upscale) → exam JSON → zip. The JSON is written **after** the images are
    processed, so PDFs and quiz cards read ready-made images and measurements."""
    return write_bundle_data(_load(folder, hints), out_zip, ocr_mode)


PT_PER_CSS_PX = 9.5 / 16  # a page's 16 px text ↔ the PDF's 9.5 pt text


def _width_px(data: bytes) -> int:
    from PIL import Image
    try:
        with Image.open(io.BytesIO(data)) as im:
            return im.size[0]
    except Exception:
        return 0


def write_bundle_data(data, out_zip, ocr_mode: str = "lazy", display: dict | None = None) -> dict:
    """``write_bundle`` for an ``ExamData`` built elsewhere (question JSON files — ``jsonq``). ``display``:
    ``{asset_id: (css_width, css_height)}`` from the source HTML (``<img width height>``) — such an image is printed
    at that size relative to the text (formula pictures drawn at 4× by Word would otherwise be measured as huge text)
    and is never re-flowed."""
    from bot.exam.imgproc import cut_off, process

    t0 = time.perf_counter()
    doc = schema.build(data, ocr_mode)
    done: dict[str, dict] = {}
    kinds: dict[str, int] = {}
    with zipfile.ZipFile(out_zip, "w") as z:
        for sec in doc["sections"]:
            for q in sec["questions"]:
                for img in q["images"]:
                    if img["id"] not in done:
                        asset = data.assets[img["id"]]
                        try:  # a cut-off image is checked on its bytes (WeasyPrint lets Pillow decode it silently)
                            if cut_off(asset.data, asset.mime):
                                raise ValueError("cut off")
                            raw, mime, info = process(asset.data, asset.mime)
                        except Exception:
                            raw, mime, info = _unprocessed(asset.data, asset.mime)
                        file = f"img/{asset.id}.{schema._EXT.get(mime, 'bin')}"
                        z.writestr(zipfile.ZipInfo(file, date_time=(2026, 1, 1, 0, 0, 0)), raw, compress_type=zipfile.ZIP_STORED)
                        done[img["id"]] = {"file": file, "mime": mime, "w": info["w"], "h": info["h"],
                                           "content_bbox": [0, 0, info["w"], info["h"]], "kind": info["kind"],
                                           "text_px": info["text_px"], "scale": info["scale"], "lines": info["lines"],
                                           "enhanced": info["enhanced"], "bytes": len(raw),
                                           **({"broken": info["broken"]} if info.get("broken") else {})}
                        done[img["id"]]["_group"] = (asset.kind, img.get("lang"))
                        kinds[info["kind"]] = kinds.get(info["kind"], 0) + 1
        _snap_text_size(done)
        for aid, (w_css, _h_css) in (display or {}).items():
            d = done.get(aid)
            if d is None or d.get("broken") or not w_css or aid not in data.assets:
                continue
            w0 = _width_px(data.assets[aid].data)
            if w0:
                d.update(scale=round(w_css / w0 * PT_PER_CSS_PX / _upscaled(d), 5), lines=[], fixed=True)
        for sec in doc["sections"]:
            for q in sec["questions"]:
                for img in q["images"]:
                    img["source_kind"] = img.get("kind")
                    img.update({k: v for k, v in done[img["id"]].items() if not k.startswith("_")})
        doc["meta"]["image_kinds"] = kinds
        z.writestr(zipfile.ZipInfo("exam.json", date_time=(2026, 1, 1, 0, 0, 0)), orjson.dumps(doc),
                   compress_type=zipfile.ZIP_DEFLATED, compresslevel=6)
    return {"sig": schema.signature(data), "questions": doc["meta"]["questions"], "images": len(done), "kinds": kinds,
            "bytes": os.path.getsize(out_zip), "ms": int((time.perf_counter() - t0) * 1000)}


def _snap_text_size(done: dict[str, dict]) -> None:
    """Images of one source (EDQT strips, TCS images …) and language are rendered with one font size; a text image
    measured within ±25 % of its group's median (one line: 0.6–1.67×) takes the median, so question and option text
    print at exactly the same size. Genuinely different sizes stay as measured."""
    from bot.exam.imgproc import TARGET_CAP_PT

    groups: dict[tuple, list[float]] = {}
    for d in done.values():
        if d["kind"] == "text" and d["text_px"]:
            groups.setdefault(d["_group"], []).append(d["text_px"])
    for d in done.values():
        sizes = groups.get(d["_group"]) if d["kind"] == "text" else None
        if not sizes or len(sizes) < 5:
            continue
        med = sorted(sizes)[len(sizes) // 2]
        # one line (a word or two — an option): measured from a few letters only, so a wider band (owner 2026-10-13:
        # options of one question printed in different sizes)
        lo, hi = (0.6, 1.67) if len(d.get("lines") or []) == 1 else (0.8, 1.25)
        if lo * med <= d["text_px"] <= hi * med and d["text_px"] != med:
            d["text_px"], d["scale"] = med, round(TARGET_CAP_PT / med, 4)
    # a text / formula image measured at less than half or more than twice its source's usual size: the measurement
    # failed (owner 2026-10-13, Q66: a fraction read as one tall line printed the question tiny and an option huge),
    # so it takes the source's usual pt per source pixel (one source = one screen zoom)
    ptpx: dict[tuple, list[float]] = {}
    for d in done.values():
        if d["kind"] == "text" and d["text_px"] and not d.get("broken"):
            ptpx.setdefault(d["_group"], []).append(d["scale"] * _upscaled(d))
    for d in done.values():
        vals = ptpx.get(d["_group"]) if d["kind"] in ("text", "math") and not d.get("broken") else None
        if not vals or len(vals) < 5:
            continue
        usual = sorted(vals)[len(vals) // 2]
        f = _upscaled(d)
        if not 0.5 * usual <= d["scale"] * f <= 2 * usual:
            d["scale"] = round(usual / f, 4)
            d["lines"] = []  # its lines were mis-read too: shown whole, not re-flowed
    for d in done.values():  # a cut-off image (its text cannot be measured) prints at its source's text size
        vals = ptpx.get(d["_group"]) if d.get("broken") == "part" else None
        if vals:
            d["scale"] = round(sorted(vals)[len(vals) // 2] / _upscaled(d), 4)


def _upscaled(d: dict) -> int:
    """The ``imgproc`` upscale factor of a processed image (1 when not upscaled)."""
    for e in d.get("enhanced") or []:
        if e.startswith("upscaled x"):
            return int(e[len("upscaled x"):])
    return 1


def merge_bundles(stored_zip, new_zip, out_zip, add_langs: list[str]) -> dict:
    """Add ``add_langs`` from ``new_zip`` to the stored exam (questions paired by ``qref``): text, per-language hashes
    and that language's images (files copied once). → ``{"languages", "questions", "images", "bytes", "ms", "added"}``."""
    t0 = time.perf_counter()
    a, b = read_doc(stored_zip), read_doc(new_zip)
    by_ref = {q["qref"]: q for s in b["sections"] for q in s["questions"]}
    need: set[str] = set()
    added_q = 0
    for sec in a["sections"]:
        for q in sec["questions"]:
            n = by_ref.get(q["qref"])
            if n is None:
                continue
            have = {i["id"] for i in q["images"]}
            for lang in add_langs:
                if lang not in n["lang"] or lang in q["lang"]:
                    continue
                c = n["lang"][lang]
                q["lang"][lang] = c
                q.setdefault("lang_hash", {})[lang] = n["lang_hash"][lang]
                q.setdefault("option_hashes", {})[lang] = n["option_hashes"][lang]
                ids = set(schema.image_ids(c["question"])) | {i for o in c["options"] for i in schema.image_ids(o)}
                for img in n["images"]:
                    if img["id"] in ids and img["id"] not in have:
                        q["images"].append(img)
                        have.add(img["id"])
                        need.add(img["file"])
                added_q += 1
    langs = sorted(set(a["meta"].get("languages") or []) | set(add_langs))
    a["meta"].update(languages=langs, version=int(a["meta"].get("version") or 1) + 1,
                     merged_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
    files = set()
    with zipfile.ZipFile(stored_zip) as zs, zipfile.ZipFile(new_zip) as zn, zipfile.ZipFile(out_zip, "w") as z:
        for info in zs.infolist():
            if info.filename != "exam.json":
                z.writestr(info, zs.read(info.filename), compress_type=zipfile.ZIP_STORED)
                files.add(info.filename)
        for f in sorted(need - files):
            z.writestr(zipfile.ZipInfo(f, date_time=(2026, 1, 1, 0, 0, 0)), zn.read(f), compress_type=zipfile.ZIP_STORED)
            files.add(f)
        z.writestr(zipfile.ZipInfo("exam.json", date_time=(2026, 1, 1, 0, 0, 0)), orjson.dumps(a),
                   compress_type=zipfile.ZIP_DEFLATED, compresslevel=6)
    images = sum(1 for f in files if f.startswith("img/"))
    a["meta"]["images"] = images
    return {"languages": langs, "questions": a["meta"].get("questions"), "images": images, "added": added_q,
            "bytes": os.path.getsize(out_zip), "ms": int((time.perf_counter() - t0) * 1000)}


def read_doc(zip_path) -> dict:
    with zipfile.ZipFile(zip_path) as z:
        return orjson.loads(z.read("exam.json"))


def read_image(zip_path, file: str) -> bytes:
    with zipfile.ZipFile(zip_path) as z:
        return z.read(file)
