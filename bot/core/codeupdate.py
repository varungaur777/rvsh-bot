"""Code update from a zip (``/update``, owner only — owner 2026-10-13: "sare code ko command se bhi update kia ja ske
zip code deke aur jo extra requirements hui vo bhi install ho jae (owner only)"). Pure file work + subprocesses; the
Telegram side is ``handlers.update``.

Flow: ``plan(zip, code_dir)`` (what the zip holds, checked: safe paths, Python files compile, requirements changed?)
→ owner confirms → ``backup(code_dir, backup_dir)`` → ``apply(zip, code_dir, plan)`` → ``await pip_install(code_dir)``
when ``requirements.txt`` changed → ``await verify(code_dir)`` (a fresh Python imports the new bot) → on failure
``restore(backup, code_dir, plan)``; on success the bot restarts **as a new process** (``__main__``: exit 3 under
systemd / Docker, else it starts its own successor) — an in-process ``/restart`` would keep the old modules.

- Only project files are written: ``ALLOWED`` top-level names (``bot/``, ``templates/``, ``tools/``, ``deploy/``,
  ``docs/``, ``tests/``, requirements, Dockerfile …). ``.env``, ``downloads/``, ``data/``, sessions, samples are never
  touched. Entry names with ``..`` / absolute paths are refused.
- A zip with the whole bot (``bot/__main__.py`` + ``bot/app.py``) **replaces** ``bot/`` and ``templates/`` (files
  deleted upstream go too); any other zip is a patch laid over the code.
- The zip may hold the files in one top folder (``rvsh-bot/bot/…``, as GitHub / ``git archive --prefix`` make them).
- Backups: ``<backup_dir>/code_<time>.zip``, the newest ``KEEP_BACKUPS`` kept — a backup zip is itself a valid
  ``/update`` (roll back).

Public API: ``plan`` · ``backup`` · ``apply`` · ``restore`` · ``pip_install`` · ``verify`` · ``code_dir()`` ·
``MARKER`` (``<root>/_update/done.json`` — read once after the restart to tell the owner).
"""

from __future__ import annotations

import asyncio
import shutil
import sys
import time
import zipfile
from pathlib import Path, PurePosixPath

ALLOWED = {"bot", "templates", "tools", "deploy", "docs", "tests", "requirements.txt", "requirements-dev.txt",
           "pyproject.toml", "config.env.example", "Dockerfile", "docker-compose.yml", ".dockerignore", ".gitignore",
           ".gitattributes", "prompt_v4.md", "README.md", ".github", "colab_launcher.ipynb"}
FULL_DIRS = ("bot", "templates")
UPDATE_MAX = 150 * 1024 * 1024
KEEP_BACKUPS = 5
MARKER = "_update/done.json"


def code_dir() -> Path:
    """The folder that holds the running ``bot`` package."""
    return Path(__file__).resolve().parents[2]


def _safe(name: str) -> PurePosixPath | None:
    p = PurePosixPath(name.replace("\\", "/"))
    if p.is_absolute() or ".." in p.parts or (p.parts and ":" in p.parts[0]):
        return None
    return p


def _req_lines(text: str) -> list[str]:
    return sorted({ln.split("#", 1)[0].strip() for ln in text.splitlines() if ln.split("#", 1)[0].strip()})


def plan(zip_path: str, root: str) -> dict:
    """What the zip would change. ``{"ok", "problem", "prefix", "full", "files": [rel], "skipped": [rel],
    "py": n, "bad_py": [(rel, error)], "req_changed", "req_added": [lines], "req_removed": [lines]}``."""
    out = {"ok": False, "problem": "", "prefix": "", "full": False, "files": [], "skipped": [], "py": 0,
           "bad_py": [], "req_changed": False, "req_added": [], "req_removed": []}
    try:
        z = zipfile.ZipFile(zip_path)
    except (zipfile.BadZipFile, OSError) as e:
        out["problem"] = f"not a zip ({e.__class__.__name__})"
        return out
    with z:
        names = [i.filename for i in z.infolist() if not i.is_dir()]
        if sum(i.file_size for i in z.infolist()) > 4 * UPDATE_MAX:
            out["problem"] = "too large when unpacked"
            return out
        paths = [_safe(n) for n in names]
        if any(p is None for p in paths):
            out["problem"] = "a file inside has an unsafe path (.. or absolute)"
            return out
        tops = {p.parts[0] for p in paths if p.parts}
        prefix = ""
        main = [p for p in paths if p.name == "__main__.py" and len(p.parts) >= 2 and p.parts[-2] == "bot"]
        if main:
            prefix = "/".join(main[0].parts[:-2])
        elif len(tops) == 1 and not (tops & ALLOWED):
            prefix = next(iter(tops))
        out["prefix"] = prefix
        rels = {}
        for p, n in zip(paths, names):
            s = str(p)
            if prefix:
                if not s.startswith(prefix + "/"):
                    out["skipped"].append(s)
                    continue
                s = s[len(prefix) + 1:]
            rp = PurePosixPath(s)
            if not rp.parts or rp.parts[0] not in ALLOWED or "__pycache__" in rp.parts or rp.suffix == ".pyc":
                out["skipped"].append(s)
                continue
            rels[s] = n
        out["files"] = sorted(rels)
        out["full"] = "bot/__main__.py" in rels and "bot/app.py" in rels
        if not rels:
            out["problem"] = "no bot files in it (bot/…, templates/…, requirements.txt …)"
            return out
        for rel, n in rels.items():
            if rel.endswith(".py"):
                out["py"] += 1
                try:
                    compile(z.read(n), rel, "exec")
                except SyntaxError as e:
                    out["bad_py"].append((rel, f"line {e.lineno}: {e.msg}"))
        if "requirements.txt" in rels:
            new = _req_lines(z.read(rels["requirements.txt"]).decode("utf-8", "replace"))
            cur_p = Path(root) / "requirements.txt"
            cur = _req_lines(cur_p.read_text(encoding="utf-8", errors="replace")) if cur_p.exists() else []
            out["req_added"] = [x for x in new if x not in cur]
            out["req_removed"] = [x for x in cur if x not in new]
            out["req_changed"] = bool(out["req_added"] or out["req_removed"])
    if out["bad_py"]:
        out["problem"] = f"{len(out['bad_py'])} Python file(s) do not compile"
        return out
    out["ok"] = True
    return out


def backup(root: str, backup_dir: str) -> str:
    """Zip of the current code (``ALLOWED`` items, no caches) → its path; older backups beyond ``KEEP_BACKUPS`` go."""
    rootp, bdir = Path(root), Path(backup_dir)
    bdir.mkdir(parents=True, exist_ok=True)
    out = bdir / f"code_{time.strftime('%Y%m%d_%H%M%S')}.zip"
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for name in sorted(ALLOWED):
            p = rootp / name
            if p.is_file():
                z.write(p, name)
            elif p.is_dir():
                for f in sorted(p.rglob("*")):
                    if f.is_file() and "__pycache__" not in f.parts and f.suffix != ".pyc":
                        z.write(f, f.relative_to(rootp).as_posix())
    for old in sorted(bdir.glob("code_*.zip"))[:-KEEP_BACKUPS]:
        old.unlink(missing_ok=True)
    return str(out)


def apply(zip_path: str, root: str, p: dict) -> int:
    """Write the planned files (a full update first removes ``bot/`` and ``templates/``). Returns files written."""
    rootp = Path(root)
    if p["full"]:
        for d in FULL_DIRS:
            if any(f.startswith(d + "/") for f in p["files"]) and (rootp / d).is_dir():
                shutil.rmtree(rootp / d)
    n = 0
    with zipfile.ZipFile(zip_path) as z:
        names = {}
        for i in z.infolist():
            sp = _safe(i.filename)
            if i.is_dir() or sp is None:
                continue
            s = str(sp)
            if p["prefix"]:
                if not s.startswith(p["prefix"] + "/"):
                    continue
                s = s[len(p["prefix"]) + 1:]
            names[s] = i.filename
        for rel in p["files"]:
            dest = rootp / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            with z.open(names[rel]) as src, open(dest, "wb") as dst:
                shutil.copyfileobj(src, dst)
            n += 1
    return n


def restore(backup_zip: str, root: str, p: dict) -> None:
    """Undo ``apply``: files the update added are removed, the backup is laid back (a full update's folders whole)."""
    rootp = Path(root)
    with zipfile.ZipFile(backup_zip) as z:
        had = set(z.namelist())
        if p["full"]:
            for d in FULL_DIRS:
                if (rootp / d).is_dir() and any(n.startswith(d + "/") for n in had):
                    shutil.rmtree(rootp / d)
        for rel in p["files"]:
            if rel not in had:
                (rootp / rel).unlink(missing_ok=True)
        z.extractall(rootp)


async def _run(cmd: list[str], cwd: str, timeout: float) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(*cmd, cwd=cwd, stdout=asyncio.subprocess.PIPE,
                                                stderr=asyncio.subprocess.STDOUT)
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        return -1, f"timed out after {int(timeout)} s"
    return proc.returncode or 0, out.decode("utf-8", "replace")


async def pip_install(root: str, timeout: float = 900) -> tuple[bool, str]:
    """``python -m pip install -r requirements.txt`` with the bot's own Python. (ok, last lines of its output)."""
    code, out = await _run([sys.executable, "-m", "pip", "install", "--disable-pip-version-check", "-r",
                            "requirements.txt"], root, timeout)
    return code == 0, "\n".join(out.strip().splitlines()[-12:])


async def verify(root: str, timeout: float = 180) -> tuple[bool, str]:
    """A fresh Python imports the new code (``bot.app``, every handler, the entry point)."""
    code, out = await _run([sys.executable, "-c", "import bot.app, bot.handlers, bot.__main__; print('import ok')"],
                           root, timeout)
    return code == 0 and "import ok" in out, "\n".join(out.strip().splitlines()[-12:])
