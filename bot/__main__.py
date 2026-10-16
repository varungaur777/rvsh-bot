"""Entry point: ``python -m bot``.

Loads config, detects the runtime profile, uses uvloop where available (Linux/macOS/Colab/Actions),
runs the app until a stop request. ``/restart`` builds a fresh ``App`` on a fresh event loop **inside the same
process** (``os.execv`` is unreliable on Windows/Colab: it spawns a new process and the parent exits — B-003).
After ``/update`` the new code needs a **new process**: under a supervisor (systemd ``INVOCATION_ID``, Docker
``/.dockerenv``, or ``RVSH_SUPERVISED=1``) the bot exits with 3 and is started again; run by hand, it starts its
successor (same command, same folder) and exits.
Exit codes: 0 normal stop · 1 boot failure · 2 configuration error · 3 restart me (code updated).
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys

from bot.core.config import ConfigError, load_config
from bot.core.runtime import detect


def main() -> int:
    try:
        config = load_config()
    except ConfigError as e:
        print(f"Configuration error: {e}", file=sys.stderr)
        return 2
    runtime = detect(config)
    if runtime.use_uvloop:
        try:
            import uvloop  # type: ignore
            uvloop.install()
        except ImportError:
            pass

    from bot.app import App

    async def run() -> tuple[bool, bool]:
        app = App(config, runtime)
        try:
            await app.start()
        except Exception:
            logging.getLogger("bot").exception("boot failed")
            try:
                await app.repo.close()
            except Exception:
                pass
            raise
        await app.run_until_stopped()
        return app.restart_requested, app.code_update

    while True:
        try:
            restart, updated = asyncio.run(run())
        except KeyboardInterrupt:
            return 0
        except Exception:
            return 1
        if updated:
            return relaunch()
        if not restart:
            return 0
        config = load_config()  # pick up .env changes made through /bot


def relaunch() -> int:
    if os.environ.get("INVOCATION_ID") or os.environ.get("RVSH_SUPERVISED") or os.path.exists("/.dockerenv"):
        return 3  # systemd / Docker start it again
    import subprocess
    subprocess.Popen([sys.executable, "-m", "bot", *sys.argv[1:]], cwd=os.getcwd())
    return 0


if __name__ == "__main__":
    sys.exit(main())
