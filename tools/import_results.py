"""Import candidate results from saved response sheets into the bot's database (``exam_results`` + catalogue).

Usage (stop the bot first — it caches documents this tool writes)::

    python -m tools.import_results <folder-or-file> [...] [--user <id>] [--dry-run]

Sheets are grouped per candidate (same roll no., exam and date — EDQT section parts merge into one result),
scored with the same suggestion the bot uses (file → admin preset → ``marks.rules`` → learned preset → +1/0) and
saved with ``exam.results.save_result`` (unmasked; ``submitted_by`` = ``--user``, default the owner); each sheet's page
URL (MHT header) goes to the ``exam_urls`` registry. Re-running is safe: one document per ``exam_key|roll_no`` / URL.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from pathlib import Path
from types import SimpleNamespace

from bot.core.config import load_config
from bot.core.settings import Settings
from bot.db.repo import Repo
from bot.exam import marking
from bot.exam import results as R
from bot.exam.keys import name_slug
from bot.exam.marks import negative_from_fraction, scheme_text, score
from bot.exam.schema import main_languages
from bot.exam.urls import register as register_url
from bot.net.fetcher import detect_kind
from bot.parsers import hints_from_path, parse_source, parse_sources, source
from bot.parsers.models import Marking

SHEETS = (".mht", ".mhtml", ".html", ".htm")


def _files(args: list[str]) -> list[Path]:
    out = []
    for a in args:
        p = Path(a)
        out += sorted(x for x in p.rglob("*") if x.suffix.lower() in SHEETS) if p.is_dir() else [p]
    return out


def _groups(files: list[Path]) -> list[list[Path]]:
    """One group per candidate: sheets of the same folder + exam (EDQT parts; some parts print no roll no.),
    split by roll no. when one folder holds several candidates."""
    by_folder: dict[tuple, list[tuple[str | None, Path]]] = {}
    for f in files:
        try:
            d = parse_source(source.load_path(f, hints_from_path(f)))
        except Exception as e:
            print(f"  skip {f.name}: {e!r}")
            continue
        # name + date (not the full exam_key: EDQT part A prints no time/roll, the merged result has both)
        by_folder.setdefault((f.parent, name_slug(d.meta.exam_name), d.meta.date), []).append((d.candidate.roll_no, f))
    out: list[list[Path]] = []
    for items in by_folder.values():
        rolls = {r for r, _ in items if r}
        if len(rolls) <= 1:
            out.append([f for _, f in items])
            continue
        for roll in sorted(rolls):
            out.append([f for r, f in items if r == roll])
        out += [[f] for r, f in items if not r]  # several candidates here: a roll-less sheet stays on its own
    return out


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("paths", nargs="+")
    ap.add_argument("--user", type=int, default=0, help="submitted_by user id (default: OWNER_ID)")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    cfg = load_config()
    repo = Repo(cfg.download_root / "_db", cfg.mongo_url)
    await repo.start()
    settings = Settings(repo)
    await settings.load()
    app = SimpleNamespace(repo=repo, settings=settings)
    user = a.user or cfg.owner_id
    t0 = time.perf_counter()
    saved = urls = 0
    for files in _groups(_files(a.paths)):
        srcs = [source.load_path(f, hints_from_path(f)) for f in files]
        d = parse_sources(srcs)
        detected = (d.marking.plus, d.marking.minus) if d.marking else None
        sug = await marking.suggest(app, {"exam_name": d.meta.exam_name, "marking": detected})
        plus = float(sug["plus"])
        sc = score(d, Marking(plus=plus, minus=negative_from_fraction(plus, float(sug["neg"])), source=sug["source"],
                              label=scheme_text(plus, float(sug["neg"]))))
        print(f"  {d.meta.exam_name} · {d.meta.date} · roll {(d.candidate.roll_no or '?')[:4]}… · {len(files)} file(s) · "
              f"{sc.marks:g}/{sc.max_marks:g} ({sug['source']} {scheme_text(plus, float(sug['neg']))})")
        if a.dry_run:
            continue
        if await R.save_result(repo, d, sc, user):
            saved += 1
        for u in {s.url for s in srcs if s.url and detect_kind(s.url)}:  # page URL from the MHT header → /results 🔗
            await register_url(repo, u, d.meta, user, langs=main_languages([set(q.lang) for q in d.questions()],
                                                                            [(x.name, len(x.questions)) for x in d.sections]))
            urls += 1
    if not a.dry_run:
        shifts = await R.rebuild_catalogue(repo)
        await repo.json.flush()
        print(f"saved {saved} result(s), {urls} URL registration(s); catalogue: {shifts} shift(s)")
    print(f"done in {time.perf_counter() - t0:.1f}s")
    await repo.close()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
