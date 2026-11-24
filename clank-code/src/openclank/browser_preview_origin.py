"""Loopback-only artifact origin for active, revision-bound browser previews.

Only HTML and explicitly admitted dependency bytes are held in memory. The
listener never translates URL paths to filesystem paths and never accepts
unknown artifact Host names.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import socket
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import MappingProxyType
from typing import Callable, Mapping
from urllib.parse import unquote, urlsplit

from core.atomic_io import atomic_write_json


class BrowserPreviewOriginError(RuntimeError):
    def __init__(self, code: str, message: str):
        self.code = str(code)
        super().__init__(message)


@dataclass(frozen=True)
class PreviewAsset:
    data: bytes
    media_type: str
    digest: str


@dataclass(frozen=True)
class PreviewLease:
    session_id: str
    artifact_id: str
    owner_subject_id: str
    workspace_id: str
    resource_key: str
    revision: str
    digest: str
    html: bytes
    app_origin: str
    dependencies: Mapping[str, PreviewAsset] = field(default_factory=dict)
    client_reported_warnings: tuple[str, ...] = ()
    expires_at: float = 0.0
    validate_current: Callable[[str], bool] | None = None


class _ReusableBoundServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False
    request_queue_size = 32

    def __init__(self, bound_socket: socket.socket, handler):
        super().__init__(bound_socket.getsockname(), handler, bind_and_activate=False)
        self.socket.close()
        self.socket = bound_socket
        self.server_address = bound_socket.getsockname()
        self.server_activate()


class BrowserPreviewOrigin:
    """Stable-port HTTP origin with opaque per-artifact hostnames."""

    STATE_VERSION = 1
    ARTIFACT_HOST_SUFFIX = ".artifacts.localhost"
    _ARTIFACT_ID = re.compile(r"^[a-f0-9]{32}$")
    _SCOPE_KEY = re.compile(r"^[a-f0-9]{64}$")

    def __init__(self, data_dir: str | os.PathLike[str], *, max_asset_bytes: int = 8 * 1024 * 1024):
        self.data_dir = Path(data_dir).expanduser().absolute()
        self.state_dir = self.data_dir / "browser-preview"
        self.state_path = self.state_dir / "origin.json"
        self.max_asset_bytes = max(1, int(max_asset_bytes))
        self._lock = threading.RLock()
        self._state: dict = {"version": self.STATE_VERSION, "port": None, "artifacts": {}}
        self._leases: dict[str, PreviewLease] = {}
        self._session_to_artifact: dict[str, str] = {}
        self._session_status: dict[str, dict] = {}
        self._server: _ReusableBoundServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def port(self) -> int | None:
        return int(self._state["port"]) if self._state.get("port") else None

    @property
    def base_origin(self) -> str | None:
        return f"http://127.0.0.1:{self.port}" if self.port else None

    def start(self) -> int:
        with self._lock:
            if self._server is not None:
                return int(self._server.server_address[1])
            self._prepare_private_state_dir()
            loaded = self._load_state()
            port = loaded.get("port")
            listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                listener.bind(("127.0.0.1", int(port or 0)))
                chosen_port = int(listener.getsockname()[1])
                listener.listen(32)
                if port is None:
                    loaded["port"] = chosen_port
                    self._save_state(loaded)
                handler = self._handler_type()
                server = _ReusableBoundServer(listener, handler)
            except OSError as exc:
                listener.close()
                if port is not None:
                    raise BrowserPreviewOriginError(
                        "origin_port_unavailable",
                        "The saved browser artifact origin port is unavailable; no replacement origin was selected.",
                    ) from exc
                raise BrowserPreviewOriginError("origin_bind_failed", "A loopback browser artifact origin could not be bound.") from exc
            except Exception:
                listener.close()
                raise
            self._state = loaded
            self._server = server
            self._thread = threading.Thread(target=server.serve_forever, name="browser-preview-origin", daemon=True)
            self._thread.start()
            return chosen_port

    def stop(self) -> None:
        with self._lock:
            server, thread = self._server, self._thread
            self._server = None
            self._thread = None
            for lease in tuple(self._leases.values()):
                self._retire_locked(lease, "revoked")
            self._leases.clear()
            self._session_to_artifact.clear()
        if server is not None:
            server.shutdown()
            server.server_close()
        if thread is not None:
            thread.join(timeout=3)

    def artifact_id_for(self, scope_key: str) -> str:
        scope_key = str(scope_key or "")
        if not self._SCOPE_KEY.fullmatch(scope_key):
            raise BrowserPreviewOriginError("invalid_scope", "Artifact scope key is invalid.")
        with self._lock:
            artifact_id = self._state["artifacts"].get(scope_key)
            if artifact_id:
                return str(artifact_id)
            artifact_id = secrets.token_hex(16)
            updated = {**self._state, "artifacts": {**self._state["artifacts"], scope_key: artifact_id}}
            self._save_state(updated)
            self._state = updated
            return artifact_id

    def publish(
        self,
        *,
        scope_key: str,
        owner_subject_id: str,
        workspace_id: str,
        resource_key: str,
        revision: str,
        digest: str,
        html: bytes,
        app_origin: str,
        dependencies: Mapping[str, PreviewAsset] | None = None,
        client_reported_warnings: tuple[str, ...] = (),
        expires_at: float,
        validate_current: Callable[[str], bool] | None = None,
    ) -> PreviewLease:
        if len(html) > self.max_asset_bytes or not html:
            raise BrowserPreviewOriginError("preview_too_large", "Browser preview HTML exceeds its byte limit or is empty.")
        if not self._valid_app_origin(app_origin):
            raise BrowserPreviewOriginError("unsupported_app_origin", "Browser previews require a loopback HTTP application origin.")
        dependency_rows = dict(dependencies or {})
        if len(dependency_rows) > 32 or any(not self._safe_dependency_path(key) for key in dependency_rows):
            raise BrowserPreviewOriginError("invalid_dependency_manifest", "The dependency manifest is invalid.")
        if any(len(asset.data) > self.max_asset_bytes for asset in dependency_rows.values()):
            raise BrowserPreviewOriginError("preview_too_large", "A browser preview dependency exceeds its byte limit.")
        if self._server is None:
            self.start()
        artifact_id = self.artifact_id_for(scope_key)
        lease = PreviewLease(
            session_id=secrets.token_urlsafe(24),
            artifact_id=artifact_id,
            owner_subject_id=str(owner_subject_id),
            workspace_id=str(workspace_id),
            resource_key=str(resource_key),
            revision=str(revision),
            digest=str(digest),
            html=bytes(html),
            app_origin=app_origin,
            dependencies=MappingProxyType(dependency_rows),
            client_reported_warnings=tuple(client_reported_warnings),
            expires_at=float(expires_at),
            validate_current=validate_current,
        )
        with self._lock:
            prior = self._leases.get(artifact_id)
            if prior:
                self._retire_locked(prior, "stale")
            self._leases[artifact_id] = lease
            self._session_to_artifact[lease.session_id] = artifact_id
            self._session_status[lease.session_id] = {
                "owner_subject_id": lease.owner_subject_id, "status": "active",
                "delivered": False, "expires_at": lease.expires_at,
            }
        return lease

    def url_for(self, lease: PreviewLease) -> str:
        port = self.port or self.start()
        return f"http://{lease.artifact_id}{self.ARTIFACT_HOST_SUFFIX}:{port}/index.html"

    def revoke(self, *, session_id: str, owner_subject_id: str) -> bool:
        with self._lock:
            artifact_id = self._session_to_artifact.get(str(session_id))
            lease = self._leases.get(artifact_id or "")
            if lease is None or lease.owner_subject_id != str(owner_subject_id):
                return False
            self._retire_locked(lease, "revoked")
            return True

    def current_lease(self, *, session_id: str, owner_subject_id: str) -> PreviewLease | None:
        with self._lock:
            artifact_id = self._session_to_artifact.get(str(session_id))
            lease = self._leases.get(artifact_id or "")
            if lease is None or lease.owner_subject_id != str(owner_subject_id):
                self._expire_status_locked(str(session_id))
                return None
            if lease.expires_at <= time.time():
                self._retire_locked(lease, "expired")
                return None
            return lease

    def session_status(self, *, session_id: str, owner_subject_id: str) -> dict | None:
        with self._lock:
            self._expire_status_locked(str(session_id))
            row = self._session_status.get(str(session_id))
            if row is None or row.get("owner_subject_id") != str(owner_subject_id):
                return None
            return {"status": str(row["status"]), "delivered": bool(row.get("delivered"))}

    def mark_delivered(self, lease: PreviewLease) -> None:
        with self._lock:
            if self._leases.get(lease.artifact_id) is not lease:
                return
            row = self._session_status.get(lease.session_id)
            if row and row.get("status") == "active":
                row["status"] = "delivered"
                row["delivered"] = True

    def retire(self, lease: PreviewLease, status: str) -> None:
        with self._lock:
            self._retire_locked(lease, status)

    def _retire_locked(self, lease: PreviewLease, status: str) -> None:
        if self._leases.get(lease.artifact_id) is lease:
            self._leases.pop(lease.artifact_id, None)
            self._session_to_artifact.pop(lease.session_id, None)
        row = self._session_status.get(lease.session_id)
        if row:
            row["status"] = status
            row["delivered"] = False

    def _expire_status_locked(self, session_id: str) -> None:
        row = self._session_status.get(session_id)
        if row and row.get("status") in {"active", "delivered"} and float(row.get("expires_at") or 0) <= time.time():
            row["status"] = "expired"
            row["delivered"] = False

    def _handler_type(self):
        origin = self

        class Handler(BaseHTTPRequestHandler):
            server_version = "OpenClankArtifactOrigin"
            sys_version = ""

            def log_message(self, _format, *_args):
                return

            def do_GET(self):
                self._serve(send_body=True)

            def do_HEAD(self):
                self._serve(send_body=False)

            def _send(self, status: int, body: bytes, *, headers: Mapping[str, str] | None = None, send_body: bool = True) -> bool:
                self.send_response(status)
                for key, value in (headers or {}).items():
                    self.send_header(key, value)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                if send_body and body:
                    self.wfile.write(body)
                return True

            def _serve(self, *, send_body: bool):
                expected_port = origin.port
                expected_host_suffix = f"{origin.ARTIFACT_HOST_SUFFIX}:{expected_port}"
                host = str(self.headers.get("Host") or "").lower()
                if not host.endswith(expected_host_suffix):
                    self._send(421, b"unknown artifact host\n", send_body=send_body)
                    return
                artifact_id = host[: -len(expected_host_suffix)]
                if not origin._ARTIFACT_ID.fullmatch(artifact_id):
                    self._send(421, b"unknown artifact host\n", send_body=send_body)
                    return
                path = urlsplit(self.path).path
                try:
                    decoded_path = unquote(path, errors="strict")
                except (UnicodeDecodeError, ValueError):
                    self._send(400, b"invalid path\n", send_body=send_body)
                    return
                if "\\" in decoded_path or "\x00" in decoded_path or any(part in {".", ".."} for part in decoded_path.split("/")):
                    self._send(400, b"invalid path\n", send_body=send_body)
                    return
                with origin._lock:
                    lease = origin._leases.get(artifact_id)
                    if lease is not None and lease.expires_at <= time.time():
                        origin._retire_locked(lease, "expired")
                        lease = None
                if lease is None:
                    self._send(410, b"preview lease expired\n", headers={"Cache-Control":"no-store"}, send_body=send_body)
                    return
                asset_key = "" if decoded_path == "/index.html" else decoded_path.lstrip("/")
                if asset_key and asset_key not in lease.dependencies:
                    self._send(404, b"resource is not in the preview manifest\n", headers={"Cache-Control":"no-store"}, send_body=send_body)
                    return
                if lease.validate_current is not None:
                    try:
                        current = bool(lease.validate_current(asset_key))
                    except Exception:
                        current = False
                    if not current:
                        with origin._lock:
                            if origin._leases.get(artifact_id) is lease:
                                origin._retire_locked(lease, "stale")
                        self._send(410, b"preview resource changed or access expired\n", headers={"Cache-Control":"no-store"}, send_body=send_body)
                        return
                with origin._lock:
                    active = origin._leases.get(artifact_id)
                    if active is not lease or lease.expires_at <= time.time():
                        if active is lease:
                            origin._leases.pop(artifact_id, None)
                            origin._session_to_artifact.pop(lease.session_id, None)
                        active = None
                    if active is not None and decoded_path == "/index.html":
                        body, media_type, digest = lease.html, "text/html; charset=utf-8", lease.digest
                    elif active is not None:
                        relative = decoded_path.lstrip("/")
                        asset = lease.dependencies.get(relative)
                        if asset is not None:
                            body, media_type, digest = asset.data, asset.media_type, asset.digest
                        else:
                            body = None
                if active is None:
                    self._send(410, b"preview lease expired or revoked\n", headers={"Cache-Control":"no-store"}, send_body=send_body)
                    return
                if body is None:
                    self._send(404, b"resource is not in the preview manifest\n", headers={"Cache-Control":"no-store"}, send_body=send_body)
                    return
                headers = {
                    "Content-Type": media_type,
                    "ETag": f'"{digest}"',
                    "Cache-Control": "private, no-store",
                    "X-Content-Type-Options": "nosniff",
                    "Referrer-Policy": "no-referrer",
                    "Content-Security-Policy": self._csp(lease.app_origin),
                }
                if self._send(200, body, headers=headers, send_body=send_body) and send_body and decoded_path == "/index.html":
                    origin.mark_delivered(lease)

            @staticmethod
            def _csp(app_origin: str) -> str:
                return (
                    "default-src 'none'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
                    "img-src 'self' data: blob:; media-src 'self' blob:; connect-src 'none'; "
                    "frame-ancestors " + app_origin + "; object-src 'none'; base-uri 'none'; form-action 'none'; "
                    "sandbox allow-scripts allow-same-origin allow-downloads"
                )

        return Handler

    @staticmethod
    def _safe_dependency_path(value: str) -> bool:
        value = str(value or "")
        if not value or value.startswith("/") or "\\" in value or "%" in value or "?" in value or "#" in value:
            return False
        parts = value.split("/")
        return all(part not in {"", ".", ".."} for part in parts) and len(value.encode("utf-8")) <= 256

    @staticmethod
    def _valid_app_origin(value: str) -> bool:
        try:
            parsed = urlsplit(str(value))
            host = str(parsed.hostname or "").lower()
            if parsed.scheme != "http" or parsed.username or parsed.password or parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
                return False
            if host == "localhost":
                return True
            import ipaddress
            return ipaddress.ip_address(host).is_loopback
        except (ValueError, TypeError):
            return False

    def _prepare_private_state_dir(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.state_dir.is_symlink() or not self.state_dir.is_dir():
            raise BrowserPreviewOriginError("origin_state_unavailable", "Browser preview state directory is unsafe.")
        try:
            os.chmod(self.state_dir, 0o700)
        except OSError as exc:
            raise BrowserPreviewOriginError("origin_state_unavailable", "Browser preview state permissions could not be secured.") from exc

    def _load_state(self) -> dict:
        if not self.state_path.exists():
            return {"version": self.STATE_VERSION, "port": None, "artifacts": {}}
        if self.state_path.is_symlink() or not self.state_path.is_file():
            raise BrowserPreviewOriginError("origin_state_unavailable", "Browser preview state file is unsafe.")
        try:
            os.chmod(self.state_path, 0o600)
            value = json.loads(self.state_path.read_text("utf-8"))
            port = value.get("port")
            artifacts = value.get("artifacts")
            if value.get("version") != self.STATE_VERSION or type(port) is not int or not 1 <= port <= 65535 or not isinstance(artifacts, dict):
                raise ValueError("invalid state schema")
            for scope_key, artifact_id in artifacts.items():
                if not self._SCOPE_KEY.fullmatch(str(scope_key)) or not self._ARTIFACT_ID.fullmatch(str(artifact_id)):
                    raise ValueError("invalid artifact mapping")
            if len(set(artifacts.values())) != len(artifacts):
                raise ValueError("duplicate artifact host")
            return {"version": self.STATE_VERSION, "port": port, "artifacts": dict(artifacts)}
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            raise BrowserPreviewOriginError("origin_state_unavailable", "Browser preview origin state is unreadable or invalid.") from exc

    def _save_state(self, state: dict) -> None:
        try:
            atomic_write_json(str(self.state_path), state)
            os.chmod(self.state_path, 0o600)
        except OSError as exc:
            raise BrowserPreviewOriginError("origin_state_unavailable", "Browser preview origin state could not be saved.") from exc


__all__ = ["BrowserPreviewOrigin", "BrowserPreviewOriginError", "PreviewAsset", "PreviewLease"]
