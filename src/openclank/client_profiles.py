"""Non-secret Open Clank client profiles and OS-backed token storage.

Profile files intentionally contain only connection metadata.  Open Clank
client credentials are stored through the operating-system credential vault;
when no usable vault exists they remain process-local and disappear on exit.
"""

from __future__ import annotations

import json
import os
import re
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from threading import RLock
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from core.atomic_io import atomic_write_json


PROFILE_SCHEMA_VERSION = 1
_PROFILE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_LOOPBACK_NAMES = {"localhost", "127.0.0.1", "::1"}
_VAULT_SERVICE = "Open Clank TUI"


class ProfileError(ValueError):
    """Raised when a client profile is invalid or unavailable."""


def default_config_dir() -> Path:
    """Return the platform-appropriate Open Clank client config directory."""

    if sys.platform == "win32":
        base = os.environ.get("APPDATA")
        return Path(base) / "OpenClank" if base else Path.home() / "AppData/Roaming/OpenClank"
    if sys.platform == "darwin":
        return Path.home() / "Library/Application Support/OpenClank"
    base = os.environ.get("XDG_CONFIG_HOME")
    return Path(base) / "openclank" if base else Path.home() / ".config/openclank"


def normalize_server_url(value: str) -> tuple[str, bool]:
    """Validate a profile URL and return ``(normalized_url, is_loopback)``.

    Remote profiles must use HTTPS.  Plain HTTP is accepted only for a literal
    loopback host; aliases and private-network addresses do not qualify.
    """

    raw = str(value or "").strip()
    if not raw:
        raise ProfileError("server URL is required")
    parts = urlsplit(raw)
    scheme = parts.scheme.lower()
    host = (parts.hostname or "").lower().rstrip(".")
    if scheme not in {"http", "https"} or not host:
        raise ProfileError("server URL must be an absolute http(s) URL")
    if parts.username or parts.password:
        raise ProfileError("server URL must not contain credentials")
    if parts.query or parts.fragment:
        raise ProfileError("server URL must not contain a query or fragment")
    is_loopback = host in _LOOPBACK_NAMES
    if scheme != "https" and not is_loopback:
        raise ProfileError("remote Open Clank profiles require verified HTTPS")
    path = parts.path.rstrip("/")
    normalized = urlunsplit((scheme, parts.netloc, path, "", ""))
    return normalized, is_loopback


@dataclass(frozen=True, slots=True)
class ClientProfile:
    name: str
    url: str
    local: bool
    auto_start: bool = False

    @classmethod
    def create(cls, name: str, url: str, *, auto_start: bool = False) -> "ClientProfile":
        clean_name = str(name or "").strip()
        if not _PROFILE_NAME_RE.fullmatch(clean_name):
            raise ProfileError("profile name must use 1-64 letters, digits, '.', '_' or '-'")
        normalized, local = normalize_server_url(url)
        if auto_start and not local:
            raise ProfileError("only loopback profiles may auto-start a local server")
        return cls(clean_name, normalized, local, bool(auto_start))

    @classmethod
    def from_json(cls, raw: dict[str, Any]) -> "ClientProfile":
        return cls.create(
            str(raw.get("name") or ""),
            str(raw.get("url") or ""),
            auto_start=bool(raw.get("auto_start", False)),
        )


class ProfileStore:
    """Atomic, non-secret profile persistence."""

    def __init__(self, path: Path | None = None):
        self.path = path or (default_config_dir() / "profiles.json")
        self._lock = RLock()

    def _load_raw(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"schema_version": PROFILE_SCHEMA_VERSION, "active": None, "profiles": []}
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ProfileError(f"cannot read client profiles: {exc}") from exc
        if not isinstance(raw, dict) or raw.get("schema_version") != PROFILE_SCHEMA_VERSION:
            raise ProfileError("unsupported Open Clank client profile schema")
        return raw

    def list(self) -> list[ClientProfile]:
        with self._lock:
            rows = self._load_raw().get("profiles", [])
            if not isinstance(rows, list):
                raise ProfileError("client profile list is malformed")
            profiles = [ClientProfile.from_json(row) for row in rows if isinstance(row, dict)]
            return sorted(profiles, key=lambda item: item.name.casefold())

    def active_name(self) -> str | None:
        with self._lock:
            value = self._load_raw().get("active")
            return str(value) if value else None

    def get(self, name: str | None = None) -> ClientProfile:
        chosen = name or self.active_name()
        profiles = self.list()
        if chosen is None and len(profiles) == 1:
            return profiles[0]
        for profile in profiles:
            if profile.name == chosen:
                return profile
        if chosen is None:
            raise ProfileError("no active profile; create one with `openclank profile add`")
        raise ProfileError(f"unknown profile {chosen!r}")

    def put(self, profile: ClientProfile, *, make_active: bool = False) -> None:
        with self._lock:
            raw = self._load_raw()
            current = {item.name: item for item in self.list()}
            current[profile.name] = profile
            active = profile.name if make_active or not raw.get("active") else raw.get("active")
            self._write(active, list(current.values()))

    def use(self, name: str) -> ClientProfile:
        with self._lock:
            profile = self.get(name)
            self._write(profile.name, self.list())
            return profile

    def remove(self, name: str) -> bool:
        with self._lock:
            profiles = self.list()
            kept = [profile for profile in profiles if profile.name != name]
            if len(kept) == len(profiles):
                return False
            active = self.active_name()
            if active == name:
                active = kept[0].name if len(kept) == 1 else None
            self._write(active, kept)
            return True

    def _write(self, active: str | None, profiles: list[ClientProfile]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema_version": PROFILE_SCHEMA_VERSION,
            "active": active,
            "profiles": [asdict(item) for item in sorted(profiles, key=lambda p: p.name.casefold())],
        }
        atomic_write_json(str(self.path), payload, indent=2)
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass


class ClientCredentialVault:
    """Store profile tokens in an OS vault, with memory-only fallback."""

    def __init__(self, *, service: str = _VAULT_SERVICE):
        self.service = service
        self._memory: dict[str, str] = {}
        self._backend: Any | None = None
        self._backend_error: str | None = None
        try:
            import keyring  # type: ignore
            from keyring.errors import KeyringError  # type: ignore

            backend = keyring.get_keyring()
            priority = float(getattr(backend, "priority", 0) or 0)
            if priority <= 0:
                raise KeyringError("no usable operating-system credential vault")
            self._backend = keyring
        except Exception as exc:  # optional integration, fail closed to memory
            self._backend_error = str(exc)

    @property
    def persistent(self) -> bool:
        return self._backend is not None

    @property
    def status(self) -> str:
        if self.persistent:
            return "operating-system credential vault"
        return "memory only; login is required for each new process"

    def get(self, profile_name: str) -> str | None:
        if profile_name in self._memory:
            return self._memory[profile_name]
        if self._backend is None:
            return None
        try:
            value = self._backend.get_password(self.service, profile_name)
        except Exception as exc:
            self._backend_error = str(exc)
            return None
        return str(value) if value else None

    def set(self, profile_name: str, token: str) -> None:
        clean = str(token or "")
        if not clean.startswith("oct_"):
            raise ProfileError("refusing to store a non-TUI client credential")
        self._memory[profile_name] = clean
        if self._backend is None:
            return
        try:
            self._backend.set_password(self.service, profile_name, clean)
        except Exception as exc:
            self._backend_error = str(exc)
            self._backend = None

    def delete(self, profile_name: str) -> None:
        self._memory.pop(profile_name, None)
        if self._backend is None:
            return
        try:
            self._backend.delete_password(self.service, profile_name)
        except Exception:
            pass
