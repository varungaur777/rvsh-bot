"""PDF compression without visible quality loss (spec §15.8) — PyMuPDF, pool workers only.

- Images are re-encoded only when their effective resolution on the page is above ``dpi_threshold`` (downscaled to
  ``dpi_target``, JPEG q≈85 for photos); lossless images (text strips, line art) are never turned into JPEG.
- ``save(garbage=4)`` drops unused objects and de-duplicates identical ones (the same image / font stream used on
  many pages is stored once); streams, images and fonts are deflated; fonts added whole (PyMuPDF page stamps)
  are subset to the glyphs used.

Public API: ``save_compressed(doc, out_path, *, dpi_target=200, rewrite_images=True) -> int`` (bytes written) ·
``compress_file(path) -> {"before", "after"}`` — the same on a PDF file, in place (kept only when smaller).
``doc`` is an open ``pymupdf.Document`` (closed by the caller). ``rewrite_images=False`` skips the image pass —
for PDFs whose images are known to be web images (response sheets: 72–180 DPI on the page): the pass, and even
measuring image DPI, costs ≈ 2 s on a 50-page EDQT PDF and saves nothing there.

Depends on: PyMuPDF. Used by: ``render/original_pdf.py``, ``/batch`` merged sections / one-liners (``exam.batch``).
"""

from __future__ import annotations

import logging
import os

log = logging.getLogger(__name__)


def save_compressed(doc, out_path: str, *, dpi_target: int = 200, rewrite_images: bool = True) -> int:
    if rewrite_images:
        try:
            doc.rewrite_images(dpi_threshold=dpi_target + 20, dpi_target=dpi_target, quality=85, lossless=False)
        except Exception as e:  # an odd image never blocks the PDF: keep it as it is
            log.info("image rewrite skipped: %r", e)
    try:
        doc.subset_fonts()  # fonts added by PyMuPDF (page stamps) are embedded whole until subset
    except Exception as e:
        log.info("font subsetting skipped: %r", e)
    doc.save(out_path, garbage=4, deflate=True, deflate_images=True, deflate_fonts=True, clean=True, use_objstms=1)
    return os.path.getsize(out_path)


def compress_file(path: str, *, dpi_target: int = 200) -> dict:
    import pymupdf

    before = os.path.getsize(path)
    tmp = f"{path}.z.pdf"
    doc = pymupdf.open(path)
    try:
        after = save_compressed(doc, tmp, dpi_target=dpi_target)
    finally:
        doc.close()
    if after < before:
        os.replace(tmp, path)
    else:
        os.remove(tmp)
        after = before
    return {"before": before, "after": after}
