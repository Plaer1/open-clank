"""Explicit per-user configuration for frozen Mac app and command launches."""
from __future__ import annotations

import json
import os
from pathlib import Path


PROFILE_RELATIVE = Path("Library/Application Support/OpenClank/macos-launch-profile.json")
PATH_SELECTORS = frozenset({
    "OPEN_CLANK_DATA_DIR", "FM_DB_PATH", "OPEN_CLANK_FM_DB_PATH",
    "OPENCLANK_CONVERSATION_ARCHIVE_DB", "OPENCLANK_HISTORY_ROOT",
    "ODYSSEUS_FILES_REGISTRY", "COPAL_LOOSE_ROOT", "COPAL_DATA_DIR", "COPAL_WIKI_DATA_DIR",
})
PRIVATE_RUNTIME_SELECTORS = frozenset({
    "PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV", "OPEN_CLANK_PYTHON", "OPEN_CLANK_RUNTIME_PYTHON",
    "FM_MCP_COMMAND", "OPEN_CLANK_FM_MCP", "ODYSSEUS_FILES_SERVICE_BIN",
    "OPENCLANK_HISTORY_SERVICE_BIN", "OPEN_CLANK_ENGINE_BIN",
})


class MacLaunchProfileError(ValueError):
    """Invalid explicit configuration; messages never include profile values."""


def local_app_port(environment=None) -> int:
    raw = (os.environ if environment is None else environment).get("APP_PORT", "7777")
    if not isinstance(raw, str) or not raw or len(raw) > 5 or not raw.isascii() or not raw.isdecimal():
        raise MacLaunchProfileError("Invalid Mac launch port")
    port = int(raw)
    if not 1 <= port <= 65535:
        raise MacLaunchProfileError("Invalid Mac launch port")
    return port


def effective_home(environment=None) -> Path:
    env = os.environ if environment is None else environment
    home = Path(env["HOME"]) if "HOME" in env else Path.home()
    if not home.is_absolute():
        raise MacLaunchProfileError("Invalid Mac launch home")
    return home


def resolve_launch_environment(environment=None) -> dict[str, str]:
    """Defaults < external dotenv < profile selectors < explicit environment.

    No ancestor search or cross-home fallback is performed. The profile never
    contains credentials; an optional existing dotenv file is read in memory.
    """
    original = dict(os.environ if environment is None else environment)
    profile = effective_home(original) / PROFILE_RELATIVE
    resolved = dict(original)
    if profile.exists() or profile.is_symlink():
        try:
            if not profile.is_file() or profile.stat().st_size > 65536:
                raise ValueError
            record = json.loads(profile.read_text(encoding="utf-8"))
            if not isinstance(record, dict) or set(record) - {"schema_version", "config_file", "environment"}:
                raise ValueError
            if type(record.get("schema_version")) is not int or record["schema_version"] != 1:
                raise ValueError
            selectors = record.get("environment")
            if not isinstance(selectors, dict) or "OPEN_CLANK_DATA_DIR" not in selectors:
                raise ValueError
            for key, value in selectors.items():
                if key not in PATH_SELECTORS | {"APP_PORT", "DATABASE_URL"}:
                    raise ValueError
                if not isinstance(value, str) or not value or len(value) > 4096 or any(c in value for c in "\x00\r\n"):
                    raise ValueError
                if key in PATH_SELECTORS and not Path(value).is_absolute():
                    raise ValueError
                if key == "DATABASE_URL" and (not value.startswith("sqlite:///") or not Path(value[len("sqlite:///"):]).is_absolute() or "?" in value or "#" in value):
                    raise ValueError
            if "APP_PORT" in selectors:
                local_app_port(selectors)
            configured = record.get("config_file")
            values = {}
            if configured is not None:
                if not isinstance(configured, str) or not Path(configured).is_absolute() or not Path(configured).is_file():
                    raise ValueError
                from dotenv import dotenv_values
                values = {key: value for key, value in dotenv_values(configured, encoding="utf-8-sig").items() if value is not None}
            resolved = {**values, **selectors, **original}
        except (OSError, UnicodeError, ValueError) as exc:
            raise MacLaunchProfileError("Invalid Mac launch profile") from exc
        # The existing deployment admits normalized provider storage, never
        # retired provider configuration from its legacy dotenv/environment.
        from src.openclank.provider_startup import PROVIDER_ENV_AUTHORITIES
        for key in PROVIDER_ENV_AUTHORITIES:
            resolved.pop(key, None)
    for key in PRIVATE_RUNTIME_SELECTORS:
        resolved.pop(key, None)
    resolved["APP_BIND"] = "127.0.0.1"
    resolved["APP_PORT"] = str(local_app_port(resolved))
    resolved["PATH"] = "/usr/bin:/bin:/usr/sbin:/sbin"
    resolved["PYTHONDONTWRITEBYTECODE"] = "1"
    return resolved


def apply_launch_profile() -> int:
    resolved = resolve_launch_environment()
    os.environ.clear()
    os.environ.update(resolved)
    return local_app_port(resolved)
