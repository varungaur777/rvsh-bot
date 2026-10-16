"""Bootstrap configuration from the environment / ``.env`` (spec §7, layer 1). Changes need a restart.

Depends on: python-dotenv. Used by: ``bot.app`` (boot), ``db``, ``core.runtime``, uploader, web.

Public API
----------
- ``Config`` — frozen dataclass with every bootstrap value.
- ``load_config(env_file="", environ=None) -> Config`` · ``env_file_path()`` (``$ENV_FILE`` or ``.env``)
- ``Config.masked()`` — dict for display with secrets masked (``/bot``, startup report).

Exceptions → outcome
--------------------
- ``ConfigError`` (missing/invalid required value): raised at boot; ``bot.app`` prints the message and exits
  with code 2 — nothing starts with a half-valid configuration.
"""

from __future__ import annotations

import os
from dataclasses import asdict, dataclass, field
from pathlib import Path

from dotenv import dotenv_values

_SECRET_KEYS = {"api_hash", "bot_token", "image_bot_tokens", "mongo_url", "cloudflared_token", "session_string", "http_proxy",
                "fetch_api_token"}


class ConfigError(Exception):
    """A required setting is missing or malformed."""


@dataclass(frozen=True, slots=True)
class Config:
    api_id: int
    api_hash: str
    bot_token: str
    owner_id: int
    dump_channel_id: int
    image_dump_channel_id: int
    quiz_dump_channel_id: int = 0  # QUIZ_DUMP_CHANNEL_ID: every new quiz is posted there (0 = the dump channel)
    image_bot_tokens: tuple[str, ...] = ()
    branding: str = "@NotesHub_Ssc"
    mongo_url: str = ""
    download_root: Path = Path("downloads")
    tesseract_cmd: str = ""
    http_proxy: str = ""
    webapp_port: int = 8080
    webapp_host: str = "127.0.0.1"  # 0.0.0.0 on hosts that give HTTPS themselves (WEBAPP_BASE_URL)
    webapp_base_url: str = ""
    cloudflared_token: str = ""
    cloudflared_cmd: str = ""
    runtime_profile: str = ""
    data_persistent: bool | None = None
    session_string: str = ""
    fetch_api_url: str = ""
    fetch_api_token: str = ""
    force_sub_channels: tuple[str, ...] = ()  # FORCE_SUB_CHANNELS: ids or @names, comma-separated
    extra: dict[str, str] = field(default_factory=dict)

    @property
    def bot_id(self) -> int:
        """Numeric id of the main bot (the token prefix)."""
        return int(self.bot_token.split(":", 1)[0])

    def masked(self) -> dict[str, str]:
        """All values as strings with secrets masked (keeps the last 4 characters)."""
        out = {}
        for k, v in asdict(self).items():
            if k == "extra":
                continue
            s = ",".join(v) if isinstance(v, tuple) else str(v)
            if k in _SECRET_KEYS and s:
                s = "•" * 6 + s[-4:] if k != "image_bot_tokens" else f"{len(v)} token(s)"
            out[k] = s
        return out


def _int(env: dict, key: str, required: bool = True, default: int = 0) -> int:
    raw = (env.get(key) or "").strip()
    if not raw:
        if required:
            raise ConfigError(f"{key} is required")
        return default
    try:
        return int(raw)
    except ValueError as e:
        raise ConfigError(f"{key} must be a number, got {raw!r}") from e


def _str(env: dict, key: str, required: bool = False, default: str = "") -> str:
    raw = (env.get(key) or "").strip().strip('"').strip("'")
    if required and not raw:
        raise ConfigError(f"{key} is required")
    return raw or default


def env_file_path() -> Path:
    """The ``.env`` file: ``$ENV_FILE`` when set (Docker keeps it in the data volume, so ``/bot`` edits survive a new
    container), else ``.env`` in the working folder."""
    return Path(os.environ.get("ENV_FILE") or ".env")


def load_config(env_file: str | Path | None = "", environ: dict | None = None) -> Config:
    """Read the ``.env`` file (if present; ``""`` = ``env_file_path()``, ``None`` = none) overlaid by real environment
    variables (env vars win, as on Colab/Actions)."""
    if env_file == "":
        env_file = env_file_path()
    env: dict[str, str] = {}
    if env_file and Path(env_file).exists():
        env.update({k: v for k, v in dotenv_values(env_file).items() if v is not None})
    env.update(os.environ if environ is None else environ)

    token = _str(env, "BOT_TOKEN", required=True)
    if ":" not in token:
        raise ConfigError("BOT_TOKEN looks invalid (expected '<id>:<secret>')")
    dump = _int(env, "DUMP_CHANNEL_ID")
    persistent_raw = _str(env, "DATA_PERSISTENT").lower()
    return Config(
        api_id=_int(env, "API_ID"),
        api_hash=_str(env, "API_HASH", required=True),
        bot_token=token,
        owner_id=_int(env, "OWNER_ID"),
        dump_channel_id=dump,
        image_dump_channel_id=_int(env, "IMAGE_DUMP_CHANNEL_ID", required=False, default=dump),
        quiz_dump_channel_id=_int(env, "QUIZ_DUMP_CHANNEL_ID", required=False, default=0),
        image_bot_tokens=tuple(t.strip() for t in _str(env, "IMAGE_BOT_TOKENS").split(",") if ":" in t),
        branding=_str(env, "BRANDING", default="@NotesHub_Ssc"),
        mongo_url=_str(env, "MONGO_URL") or _str(env, "MONGO_URI"),
        download_root=Path(_str(env, "DOWNLOAD_ROOT", default="downloads")).resolve(),
        tesseract_cmd=_str(env, "TESSERACT_CMD"),
        http_proxy=_str(env, "HTTP_PROXY"),
        webapp_port=_int(env, "WEBAPP_PORT", required=False, default=8080),
        webapp_host=_str(env, "WEBAPP_HOST") or "127.0.0.1",
        webapp_base_url=_str(env, "WEBAPP_BASE_URL").rstrip("/"),
        cloudflared_token=_str(env, "CLOUDFLARED_TOKEN"),
        cloudflared_cmd=_str(env, "CLOUDFLARED_CMD"),
        runtime_profile=_str(env, "RUNTIME_PROFILE").lower(),
        data_persistent=None if not persistent_raw else persistent_raw in ("1", "true", "yes"),
        session_string=_str(env, "SESSION_STRING"),
        fetch_api_url=_str(env, "FETCH_API_URL").rstrip("/"),
        fetch_api_token=_str(env, "FETCH_API_TOKEN"),
        force_sub_channels=tuple(c.strip() for c in _str(env, "FORCE_SUB_CHANNELS").replace(";", ",").split(",") if c.strip()),
    )
