"""Bounded credential callback contract for the retained engine driver.

The codec, one-shot broker, private UDS listener, and generation-bound
registration live here. A registration still has to be attached to a real
runtime actor before activation; this module never logs, persists, or returns
a credential-derived diagnostic.
"""

from __future__ import annotations

import base64
import asyncio
import ctypes
import hashlib
import json
import os
import re
import secrets
import socket
import stat
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Mapping


# Base64url expands the value before the fixed 64 KiB callback frame cap; keep
# enough headroom for the response metadata and JSON envelope.
MAX_CREDENTIAL_BYTES = 46 * 1024
MAX_CALLBACK_FRAME_BYTES = 64 * 1024
_HEX32 = re.compile(r"^[0-9a-f]{32}$")
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_TEXT = re.compile(r"^(?!.*\x00).{1,128}$", re.DOTALL)
_OPERATION = re.compile(r"^[a-z0-9_.:-]{1,128}$")
_PROVIDER_OPERATIONS = frozenset({"provider.http"})


class CredentialCallbackError(ValueError):
    def __init__(self, code: str, safe_message: str, retryable: bool) -> None:
        super().__init__(safe_message)
        self.code = code
        self.safe_message = safe_message
        self.retryable = retryable


def _canonical(value: Mapping[str, Any]) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


@dataclass(frozen=True)
class CredentialCallbackRequest:
    request_id: str
    lease_id: str
    owner_subject_id: str
    runtime_id: str
    runtime_epoch: str
    runtime_generation: int
    driver_pid: int
    driver_start_token: str
    operation: str
    request_sha256: str

    def unsigned(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "request_id": self.request_id,
            "lease_id": self.lease_id,
            "owner_subject_id": self.owner_subject_id,
            "runtime_id": self.runtime_id,
            "runtime_epoch": self.runtime_epoch,
            "runtime_generation": self.runtime_generation,
            "driver_pid": self.driver_pid,
            "driver_start_token": self.driver_start_token,
            "operation": self.operation,
        }

    def to_wire(self) -> dict[str, Any]:
        return {**self.unsigned(), "request_sha256": self.request_sha256}

    def verify_hash(self) -> None:
        expected = hashlib.sha256(_canonical(self.unsigned())).hexdigest()
        if not _HEX64.fullmatch(self.request_sha256) or self.request_sha256 != expected:
            raise CredentialCallbackError("request_hash_mismatch", "credential request binding is invalid", False)

    @classmethod
    def from_wire(cls, value: Mapping[str, Any]) -> "CredentialCallbackRequest":
        expected = {
            "schema_version", "request_id", "lease_id", "owner_subject_id", "runtime_id",
            "runtime_epoch", "runtime_generation", "driver_pid", "driver_start_token",
            "operation", "request_sha256",
        }
        if set(value) != expected or value.get("schema_version") != 1:
            raise CredentialCallbackError("malformed_request", "credential request is malformed", False)
        fields = {key: value[key] for key in expected if key != "schema_version"}
        if not isinstance(fields["request_id"], str) or not _HEX32.fullmatch(fields["request_id"]):
            raise CredentialCallbackError("malformed_request", "credential request is malformed", False)
        if not isinstance(fields["runtime_epoch"], str) or not _HEX32.fullmatch(fields["runtime_epoch"]):
            raise CredentialCallbackError("malformed_request", "credential request is malformed", False)
        for key in ("lease_id", "owner_subject_id", "runtime_id", "driver_start_token"):
            if not isinstance(fields[key], str) or not _TEXT.fullmatch(fields[key]):
                raise CredentialCallbackError("malformed_request", "credential request is malformed", False)
        if not isinstance(fields["operation"], str) or not _OPERATION.fullmatch(fields["operation"]):
            raise CredentialCallbackError("malformed_request", "credential request is malformed", False)
        if not isinstance(fields["runtime_generation"], int) or fields["runtime_generation"] < 0:
            raise CredentialCallbackError("malformed_request", "credential request is malformed", False)
        if not isinstance(fields["driver_pid"], int) or fields["driver_pid"] < 1:
            raise CredentialCallbackError("malformed_request", "credential request is malformed", False)
        if not isinstance(fields["request_sha256"], str):
            raise CredentialCallbackError("malformed_request", "credential request is malformed", False)
        request = cls(**fields)
        request.verify_hash()
        return request


def _error(request: CredentialCallbackRequest, code: str, safe_message: str, retryable: bool) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "request_id": request.request_id,
        "lease_id": request.lease_id,
        "ok": False,
        "error": {"code": code, "safe_message": safe_message, "retryable": retryable},
        "request_sha256": request.request_sha256,
    }


class CredentialCallbackBroker:
    """Resolve a credential lease at most once per request ID."""

    def __init__(self) -> None:
        self._used_request_ids: set[str] = set()

    def resolve(
        self,
        request: CredentialCallbackRequest,
        loader: Callable[[CredentialCallbackRequest], Any],
        *,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        if request.request_id in self._used_request_ids:
            return _error(request, "credential_lease_used", "credential request has already been used", False)
        self._used_request_ids.add(request.request_id)
        try:
            grant = loader(request)
            if grant is None or getattr(grant, "lease_id", None) != request.lease_id:
                return _error(request, "credential_unavailable", "credential lease is unavailable", True)
            credentials = getattr(grant, "credentials", None)
            if not isinstance(credentials, Mapping):
                return _error(request, "credential_unavailable", "credential lease is unavailable", True)
            encoded_json = _canonical(credentials)
            if len(encoded_json) > MAX_CREDENTIAL_BYTES:
                return _error(request, "credential_too_large", "credential lease is unavailable", False)
            expires_at = getattr(grant, "expires_at", None)
            if not isinstance(expires_at, datetime):
                return _error(request, "credential_unavailable", "credential lease is unavailable", True)
            timestamp = now or datetime.now(timezone.utc)
            if expires_at <= timestamp:
                return _error(request, "credential_expired", "credential lease has expired", False)
            expires_unix_ms = int(expires_at.timestamp() * 1000)
            return {
                "schema_version": 1,
                "request_id": request.request_id,
                "lease_id": request.lease_id,
                "ok": True,
                "credential_encoding": "canonical-json-utf8-base64url-nopad",
                "credential_json_base64url": base64.urlsafe_b64encode(encoded_json).decode("ascii").rstrip("="),
                "expires_unix_ms": expires_unix_ms,
                "uses_remaining": 1,
                "request_sha256": request.request_sha256,
            }
        except Exception:
            return _error(request, "credential_unavailable", "credential lease is unavailable", True)


def provider_credential_loader(
    provider_store: Any,
    *,
    resolve_owner_subject: Callable[[str], str | None],
    holder_id: str,
) -> Callable[[CredentialCallbackRequest], Any]:
    """Build the narrow host loader used by an activated provider driver.

    The callback request contains an immutable account subject, while the
    provider repository is keyed by the current username.  The host supplies
    the authoritative subject→username resolver; this adapter never accepts a
    username from the driver.  The registered holder is fixed by activation,
    and the provider store performs the lease/credential-revision CAS and
    consumes the lease before returning plaintext.
    """

    holder_id = str(holder_id or "").strip()
    if not holder_id or "\x00" in holder_id:
        raise CredentialCallbackError("invalid_holder", "credential callback is unavailable", False)
    if not callable(resolve_owner_subject):
        raise CredentialCallbackError("invalid_owner_resolver", "credential callback is unavailable", False)

    def load(request: CredentialCallbackRequest) -> Any:
        if request.operation not in _PROVIDER_OPERATIONS:
            raise CredentialCallbackError(
                "unsupported_operation",
                "credential callback operation is unavailable",
                False,
            )
        owner = resolve_owner_subject(request.owner_subject_id)
        if not isinstance(owner, str) or not owner.strip():
            raise CredentialCallbackError(
                "owner_unavailable",
                "credential callback owner is unavailable",
                False,
            )
        return provider_store.consume_credential_lease(
            owner=owner,
            lease_id=request.lease_id,
            holder_id=holder_id,
        )

    return load


def _peer_uid(writer: asyncio.StreamWriter) -> int | None:
    sock = writer.get_extra_info("socket")
    if sock is None:
        return None
    try:
        getpeereid = getattr(sock, "getpeereid", None)
        if callable(getpeereid):
            uid, _gid = getpeereid()
            return int(uid)
        if hasattr(socket, "SO_PEERCRED"):
            raw = sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
            return int.from_bytes(raw[4:8], "little")
        if hasattr(socket, "LOCAL_PEERCRED"):
            raw = sock.getsockopt(socket.SOL_SOCKET, socket.LOCAL_PEERCRED, 12)
            uid = int.from_bytes(raw[4:8], "little")
            if uid:
                return uid
        if sys.platform == "darwin":
            uid = ctypes.c_uint()
            gid = ctypes.c_uint()
            getpeereid = getattr(ctypes.CDLL(None), "getpeereid", None)
            if getpeereid is not None and getpeereid(sock.fileno(), ctypes.byref(uid), ctypes.byref(gid)) == 0:
                return int(uid.value)
    except OSError:
        return None
    return None


class CredentialCallbackServer:
    """Private one-request-per-connection UDS callback server.

    Registration is intentionally explicit: the caller supplies the exact
    owner/runtime/driver identity and loader. A later Rust activation seam can
    wrap this class with the paused-child PID/start-token registration flow.
    """

    def __init__(
        self,
        socket_path: str | os.PathLike[str],
        *,
        owner_subject_id: str,
        runtime_id: str,
        runtime_epoch: str,
        runtime_generation: int,
        driver_pid: int,
        driver_start_token: str,
        loader: Callable[[CredentialCallbackRequest], Any],
    ) -> None:
        self.socket_path = os.fspath(socket_path)
        self.owner_subject_id = owner_subject_id
        self.runtime_id = runtime_id
        self.runtime_epoch = runtime_epoch
        self.runtime_generation = runtime_generation
        self.driver_pid = driver_pid
        self.driver_start_token = driver_start_token
        self.loader = loader
        self.broker = CredentialCallbackBroker()
        self._server: asyncio.AbstractServer | None = None

    async def start(self) -> None:
        parent = os.path.dirname(self.socket_path)
        if not parent or len(os.fsencode(self.socket_path)) > 100:
            raise CredentialCallbackError("unsafe_callback_path", "callback endpoint is invalid", False)
        os.makedirs(parent, mode=0o700, exist_ok=True)
        os.chmod(parent, 0o700)
        try:
            mode = os.lstat(self.socket_path).st_mode
        except FileNotFoundError:
            mode = None
        if mode is not None:
            if not stat.S_ISSOCK(mode):
                raise CredentialCallbackError("callback_endpoint_exists", "callback endpoint is unavailable", False)
            os.unlink(self.socket_path)
        self._server = await asyncio.start_unix_server(self._handle, path=self.socket_path)
        os.chmod(self.socket_path, 0o600)

    async def close(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        try:
            os.unlink(self.socket_path)
        except FileNotFoundError:
            pass

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            if _peer_uid(writer) != os.getuid():
                return
            header = await reader.readexactly(4)
            size = int.from_bytes(header, "big")
            if size == 0 or size > MAX_CALLBACK_FRAME_BYTES:
                return
            body = await reader.readexactly(size)
            try:
                raw = json.loads(body.decode("utf-8"))
                request = CredentialCallbackRequest.from_wire(raw)
            except (UnicodeDecodeError, json.JSONDecodeError, CredentialCallbackError):
                return
            if (
                request.owner_subject_id != self.owner_subject_id
                or request.runtime_id != self.runtime_id
                or request.runtime_epoch != self.runtime_epoch
                or request.runtime_generation != self.runtime_generation
                or request.driver_pid != self.driver_pid
                or request.driver_start_token != self.driver_start_token
            ):
                response = _error(request, "stale_generation", "credential request is stale", False)
            else:
                response = self.broker.resolve(request, self.loader)
            encoded = _canonical(response)
            if len(encoded) > MAX_CALLBACK_FRAME_BYTES:
                return
            writer.write(len(encoded).to_bytes(4, "big") + encoded)
            await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError, OSError):
            return
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except OSError:
                pass


@dataclass
class DriverCallbackRegistration:
    """A generation-bound host registration ready for driver activation.

    Creation starts the private listener and returns only opaque activation
    metadata.  The registered loader is fixed to the supplied holder and
    subject resolver; callers cannot replace it after the endpoint is live.
    This object intentionally does not spawn or resume a driver.
    """

    registration_id: str
    callback_nonce: str
    callback_binding_sha256: str
    callback_endpoint: str
    owner_subject_id: str
    runtime_id: str
    runtime_epoch: str
    runtime_generation: int
    driver_pid: int
    driver_start_token: str
    _server: CredentialCallbackServer

    @classmethod
    async def create(
        cls,
        runtime_dir: str | os.PathLike[str],
        *,
        owner_subject_id: str,
        runtime_id: str,
        runtime_epoch: str,
        runtime_generation: int,
        driver_pid: int,
        driver_start_token: str,
        registration_id: str | None = None,
        provider_store: Any,
        resolve_owner_subject: Callable[[str], str | None],
        holder_id: str,
    ) -> "DriverCallbackRegistration":
        runtime_dir = os.fspath(runtime_dir)
        if not os.path.isabs(runtime_dir):
            raise CredentialCallbackError("unsafe_callback_path", "callback endpoint is invalid", False)
        if not isinstance(owner_subject_id, str) or not _TEXT.fullmatch(owner_subject_id):
            raise CredentialCallbackError("invalid_owner", "callback registration is invalid", False)
        if not isinstance(runtime_id, str) or not _TEXT.fullmatch(runtime_id):
            raise CredentialCallbackError("invalid_runtime", "callback registration is invalid", False)
        if not isinstance(runtime_epoch, str) or not _HEX32.fullmatch(runtime_epoch):
            raise CredentialCallbackError("invalid_runtime", "callback registration is invalid", False)
        if not isinstance(runtime_generation, int) or runtime_generation < 0:
            raise CredentialCallbackError("invalid_runtime", "callback registration is invalid", False)
        if not isinstance(driver_pid, int) or driver_pid < 1:
            raise CredentialCallbackError("invalid_driver", "callback registration is invalid", False)
        if not isinstance(driver_start_token, str) or not _TEXT.fullmatch(driver_start_token):
            raise CredentialCallbackError("invalid_driver", "callback registration is invalid", False)
        if registration_id is None:
            registration_id = secrets.token_hex(16)
        elif not isinstance(registration_id, str) or not _HEX32.fullmatch(registration_id):
            raise CredentialCallbackError("invalid_registration", "callback registration is invalid", False)
        callback_nonce = secrets.token_urlsafe(32)
        callback_endpoint = os.path.join(runtime_dir, f"driver-callback-{registration_id}.sock")
        binding = {
            "schema_version": 1,
            "registration_id": registration_id,
            "driver_pid": driver_pid,
            "driver_start_token": driver_start_token,
            "callback_endpoint": callback_endpoint,
            "callback_nonce": callback_nonce,
            "owner_subject_id": owner_subject_id,
            "runtime_id": runtime_id,
            "runtime_epoch": runtime_epoch,
            "runtime_generation": runtime_generation,
        }
        callback_binding_sha256 = hashlib.sha256(_canonical(binding)).hexdigest()
        loader = provider_credential_loader(
            provider_store,
            resolve_owner_subject=resolve_owner_subject,
            holder_id=holder_id,
        )
        server = CredentialCallbackServer(
            callback_endpoint,
            owner_subject_id=owner_subject_id,
            runtime_id=runtime_id,
            runtime_epoch=runtime_epoch,
            runtime_generation=runtime_generation,
            driver_pid=driver_pid,
            driver_start_token=driver_start_token,
            loader=loader,
        )
        try:
            await server.start()
        except Exception:
            await server.close()
            raise
        return cls(
            registration_id=registration_id,
            callback_nonce=callback_nonce,
            callback_binding_sha256=callback_binding_sha256,
            callback_endpoint=callback_endpoint,
            owner_subject_id=owner_subject_id,
            runtime_id=runtime_id,
            runtime_epoch=runtime_epoch,
            runtime_generation=runtime_generation,
            driver_pid=driver_pid,
            driver_start_token=driver_start_token,
            _server=server,
        )

    def activation_payload(self) -> dict[str, Any]:
        return {
            "registration_id": self.registration_id,
            "callback_endpoint": self.callback_endpoint,
            "callback_nonce": self.callback_nonce,
            "callback_binding_sha256": self.callback_binding_sha256,
        }

    async def close(self) -> None:
        await self._server.close()


__all__ = [
    "CredentialCallbackBroker",
    "CredentialCallbackError",
    "CredentialCallbackRequest",
    "CredentialCallbackServer",
    "DriverCallbackRegistration",
    "provider_credential_loader",
]
