"""Process-bound readiness proof for the macOS desktop-shell admission spike.

The secret is supplied only to the backend process spawned by the shell and is
removed from the process environment as soon as this module captures it.  The
browser never receives the secret and this module exposes no native capability.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import hmac
import os
import re
from collections.abc import MutableMapping
from urllib.parse import urlsplit


DESKTOP_SHELL_READY_PATH = "/api/desktop-shell/ready"
DESKTOP_SHELL_CHALLENGE_HEADER = "X-Open-Clank-Desktop-Challenge"
DESKTOP_SHELL_NONCE_ENV = "OPEN_CLANK_DESKTOP_READINESS_NONCE"
DESKTOP_SHELL_ORIGIN_ENV = "OPEN_CLANK_DESKTOP_EXPECTED_ORIGIN"
DESKTOP_SHELL_SCHEMA_VERSION = 1

_HEX_32_BYTES = re.compile(r"^[0-9a-f]{64}$")
_PROOF_DOMAIN = "open-clank-desktop-shell-v1"


def _canonical_exact_loopback_origin(value: str) -> str:
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise ValueError("desktop shell origin has an invalid port") from exc
    if (
        parsed.scheme != "http"
        or parsed.hostname != "127.0.0.1"
        or port is None
        or not 1 <= port <= 65535
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError(
            "desktop shell origin must be exactly http://127.0.0.1:<port>"
        )
    return f"http://127.0.0.1:{port}"


@dataclass(frozen=True)
class DesktopShellBinding:
    """One backend process's private desktop-shell readiness binding."""

    secret: bytes
    origin: str
    pid: int

    @classmethod
    def capture(
        cls,
        environment: MutableMapping[str, str] | None = None,
        *,
        pid: int | None = None,
    ) -> "DesktopShellBinding | None":
        target = os.environ if environment is None else environment
        nonce = str(target.pop(DESKTOP_SHELL_NONCE_ENV, "") or "").strip()
        origin = str(target.pop(DESKTOP_SHELL_ORIGIN_ENV, "") or "").strip()
        if not nonce and not origin:
            return None
        if not nonce or not origin:
            raise ValueError("desktop shell readiness nonce and origin must be set together")
        if not _HEX_32_BYTES.fullmatch(nonce):
            raise ValueError("desktop shell readiness nonce must be 32-byte lowercase hex")
        return cls(
            secret=bytes.fromhex(nonce),
            origin=_canonical_exact_loopback_origin(origin),
            pid=os.getpid() if pid is None else int(pid),
        )

    @property
    def expected_host_header(self) -> str:
        return self.origin.removeprefix("http://")

    @staticmethod
    def challenge_is_valid(challenge: str) -> bool:
        return bool(_HEX_32_BYTES.fullmatch(str(challenge or "")))

    def proof_message(
        self,
        challenge: str,
        *,
        ready: bool,
        auth_enabled: bool,
    ) -> bytes:
        if not self.challenge_is_valid(challenge):
            raise ValueError("desktop shell challenge must be 32-byte lowercase hex")
        return (
            f"{_PROOF_DOMAIN}\n{challenge}\n{self.origin}\n{self.pid}\n"
            f"{int(bool(ready))}\n{int(bool(auth_enabled))}"
        ).encode("ascii")

    def payload(
        self,
        challenge: str,
        *,
        ready: bool,
        auth_enabled: bool,
    ) -> dict[str, object]:
        message = self.proof_message(
            challenge,
            ready=ready,
            auth_enabled=auth_enabled,
        )
        proof = hmac.new(self.secret, message, hashlib.sha256).hexdigest()
        return {
            "schema_version": DESKTOP_SHELL_SCHEMA_VERSION,
            "ready": bool(ready),
            "auth_enabled": bool(auth_enabled),
            "origin": self.origin,
            "pid": self.pid,
            "proof": proof,
        }
