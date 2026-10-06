"""Small app-principal client for the long-lived Rust filesystem service.

The client owns process framing and lifecycle only. Path containment, encoding,
atomicity, and search semantics remain in the Rust service.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import queue
import secrets
import stat
import struct
import subprocess
import sys
import tempfile
import threading
import uuid
from pathlib import Path
from typing import Any, AsyncIterator

from src.openclank.filesystem_registry import FilesystemRootRegistry, FilesystemRegistryError
from src.tool_security import owner_is_admin_or_single_user


_FRAME = struct.Struct("!I")
_MAX_FRAME_BYTES = 8 * 1024 * 1024
_CLIENTS: dict[tuple[str, str, str], "OdysseusFilesClient"] = {}
_AGENT_CLIENTS: dict[tuple[str, str | None, str, str, int, str], "AgentFilesClient"] = {}
_CLIENTS_LOCK = threading.Lock()
_HISTORY_BINDING_PROVIDER: Any = None


class FilesServiceError(RuntimeError):
    def __init__(self, message: str, *, code: str = "root_unavailable") -> None:
        super().__init__(message)
        self.code = code


def set_history_binding_provider(provider: Any) -> None:
    """Install the app supervisor's trusted per-owner Files binding lookup."""
    global _HISTORY_BINDING_PROVIDER
    _HISTORY_BINDING_PROVIDER = provider


def history_binding_for(
    owner: str,
    lane: str,
    scope: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Resolve a trusted binding for a child process without exposing the provider global."""
    if not callable(_HISTORY_BINDING_PROVIDER):
        return None
    provider = _HISTORY_BINDING_PROVIDER
    try:
        return provider(str(owner), str(lane), dict(scope or {}))
    except TypeError:
        # Compatibility for narrow test/install providers while every built-in
        # provider is migrated to the scope-aware contract.
        return provider(str(owner), str(lane))


class OdysseusFilesClient:
    def __init__(
        self,
        owner: str,
        *,
        binary: str | None = None,
        app_scope: dict[str, Any] | None = None,
        transport: str | None = None,
        history_actor_id: str | None = None,
        history_account_id: str | None = None,
        history_token: str | None = None,
    ) -> None:
        self.owner = owner
        self.history_actor_id = history_actor_id
        self.history_account_id = history_account_id
        self.history_token = history_token
        self.binary = binary or os.environ.get("ODYSSEUS_FILES_SERVICE_BIN")
        selected_transport = (transport or os.environ.get("ODYSSEUS_FILES_TRANSPORT") or "framed").strip().lower()
        self.transport = {
            "framed": "framed",
            "stdio": "framed",
            "framed_stdio": "framed",
            "grpc": "grpc",
            "tonic": "grpc",
        }.get(selected_transport, selected_transport)
        self.app_scope = dict(app_scope or {
            "host": True,
            "visible_root_ids": [],
            "capabilities": [],
            "root_capabilities": {},
            "generation": 0,
            "active_folder": None,
        })
        self._policy_generation = (
            int(self.app_scope.get("generation") or 0)
            if (
                not self.app_scope.get("host")
                and (
                    self.app_scope.get("visible_root_ids")
                    or int(self.app_scope.get("generation") or 0) > 0
                )
            )
            else None
        )
        self._process: subprocess.Popen[bytes] | None = None
        self._ready = False
        self._lock = threading.RLock()
        self._grpc_tempdir: tempfile.TemporaryDirectory[str] | None = None
        self._grpc_socket_path: Path | str | None = None
        self._grpc_session: str | None = None
        self.stable_object_handles = False
        self._history_environment_snapshot: dict[str, str] | None = None
        self._active_grpc_operations = 0
        self._grpc_watch_calls: set[tuple[Any, Any]] = set()

    @property
    def process(self) -> subprocess.Popen[bytes] | None:
        return self._process

    def _binary_path(self) -> str:
        if self.binary:
            return str(Path(self.binary).expanduser())
        repository = Path(__file__).resolve().parents[2]
        executable = "odysseus-files-service.exe" if os.name == "nt" else "odysseus-files-service"
        bundle_root = Path(getattr(sys, "_MEIPASS", "")) if getattr(sys, "_MEIPASS", "") else None
        candidates = [
            # Packaged desktop/PyInstaller layouts first: the service is a
            # release artifact beside the Python executable, never a copied
            # Python fallback.
            bundle_root / "bin" / executable if bundle_root else None,
            bundle_root / executable if bundle_root else None,
            bundle_root / "libexec" / "openclank" / executable if bundle_root else None,
            bundle_root / "libexec" / "openclank" / "files" / executable if bundle_root else None,
            Path(sys.executable).resolve().parent / executable,
            Path(sys.executable).resolve().parent / "bin" / executable,
            repository / "packages" / "odysseus-files" / "target" / "release" / executable,
            repository / "packages" / "odysseus-files" / "target" / "debug" / executable,
        ]
        for candidate in candidates:
            if candidate and candidate.is_file():
                return str(candidate)
        raise FilesServiceError(
            "Rust filesystem service is not packaged or built; set ODYSSEUS_FILES_SERVICE_BIN",
            code="root_unavailable",
        )

    def _start_locked(self) -> None:
        if self._process is not None and self._process.poll() is None and self._ready:
            return
        if self._process is not None:
            self.close()
        environment = self._service_environment()
        try:
            self._process = subprocess.Popen(
                [self._binary_path()],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                env=environment,
                bufsize=0,
            )
        except OSError as error:
            raise FilesServiceError(f"could not start Rust filesystem service: {error}") from error
        try:
            response = self._exchange_locked(
                {
                    "protocol": {"major": 1, "minor": 0},
                    "request_id": uuid.uuid4().hex,
                    "session_id": "odysseus-readiness",
                    "root_operation_id": uuid.uuid4().hex,
                    "operation": "health",
                    "target": {"kind": "path", "value": "."},
                    "payload": {},
                }
            )
            data = response.get("data") or {}
            protocol = response.get("protocol") or data.get("protocol")
            ready = response.get("ready") if "ready" in response else data.get("ready")
            if not isinstance(protocol, dict) or protocol.get("major") != 1 or not ready:
                raise FilesServiceError("Rust filesystem service failed readiness negotiation", code="protocol_mismatch")
            self._ready = True
        except FilesServiceError:
            self.close()
            raise

    def _start_grpc_locked(self) -> tuple[Path | str, str]:
        if self.transport != "grpc":
            raise FilesServiceError("Tonic transport was not selected", code="protocol_mismatch")
        if sys.platform not in {"darwin", "win32"}:
            raise FilesServiceError(
                "Tonic filesystem transport is currently available only on macOS",
                code="protocol_mismatch",
            )
        if (
            self._process is not None
            and self._process.poll() is None
            and self._grpc_socket_path is not None
            and self._grpc_session is not None
        ):
            return self._grpc_socket_path, self._grpc_session
        if self._process is not None or self._grpc_tempdir is not None:
            self.close()
        if sys.platform == "win32":
            socket_path: Path | str = "loopback"
        else:
            private_parent = Path("/private/tmp") if Path("/private/tmp").is_dir() else None
            self._grpc_tempdir = tempfile.TemporaryDirectory(prefix="ocf-", dir=str(private_parent) if private_parent else None)
            private_directory = Path(self._grpc_tempdir.name)
            private_directory.chmod(0o700)
            socket_path = private_directory / "files.sock"
        session_binding = secrets.token_hex(32)
        environment = self._service_environment()
        environment.update(
            {
                "ODYSSEUS_FILES_GRPC_SOCKET": str(socket_path),
                "ODYSSEUS_FILES_GRPC_SESSION": session_binding,
            }
        )
        try:
            self._process = subprocess.Popen(
                [self._binary_path()],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE if sys.platform == "win32" else subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env=environment,
                bufsize=0,
                close_fds=True,
            )
        except OSError as error:
            self.close()
            raise FilesServiceError(f"could not start Tonic filesystem service: {error}") from error
        if sys.platform == "win32":
            endpoints: queue.Queue[bytes] = queue.Queue(maxsize=1)
            process = self._process
            def read_endpoint() -> None:
                try:
                    endpoints.put(process.stdout.readline(128) if process.stdout else b"")
                except OSError:
                    endpoints.put(b"")
            threading.Thread(target=read_endpoint, name="files-grpc-readiness", daemon=True).start()
            try:
                endpoint = endpoints.get(timeout=5.0).decode("ascii").strip()
                host, port = endpoint.rsplit(":", 1)
                if host != "127.0.0.1" or not port.isdecimal() or not 1 <= int(port) <= 65535:
                    raise ValueError("invalid endpoint")
                socket_path = endpoint
            except (queue.Empty, UnicodeError, ValueError) as error:
                self.close()
                raise FilesServiceError("Windows Files service failed private endpoint readiness") from error
        self._grpc_socket_path = socket_path
        self._grpc_session = session_binding
        self._ready = False
        return socket_path, session_binding

    def _registry(self) -> FilesystemRootRegistry:
        registry_path = os.environ.get("ODYSSEUS_FILES_REGISTRY")
        return FilesystemRootRegistry(registry_path) if registry_path else FilesystemRootRegistry()

    def _assert_policy_generation(self) -> None:
        """Fail closed when a scoped client outlives its registry projection."""
        expected = self._policy_generation
        if expected is None:
            return
        registry = self._registry()
        try:
            current = registry.generation()
        except Exception as error:
            self.close()
            raise FilesServiceError(
                "filesystem policy generation could not be validated",
                code="policy_generation_changed",
            ) from error
        if current != expected:
            self.close()
            raise FilesServiceError(
                "filesystem policy generation changed; retry with a fresh scope",
                code="policy_generation_changed",
            )

    def _history_environment(self) -> dict[str, str]:
        """Resolve the current trusted History binding for this Files scope."""
        environment: dict[str, str] = {}
        if not callable(_HISTORY_BINDING_PROVIDER):
            return environment
        try:
            binding = history_binding_for(
                self.owner,
                "agent" if isinstance(self, AgentFilesClient) else "human",
                self.app_scope,
            )
        except Exception:
            # A configured lookup outage must not masquerade as intentionally
            # unconfigured capture. The native engine keeps reads available,
            # but its required-capture admission blocks every live mutation.
            return {"OPENCLANK_HISTORY_SOCKET": "history-binding-unavailable"}
        if not binding:
            return environment
        environment.update(
            {
                "OPENCLANK_HISTORY_SOCKET": str(binding["socket"]),
                "OPENCLANK_HISTORY_ACCOUNT_ID": str(binding["account_id"]),
                "OPENCLANK_HISTORY_ACTOR_ID": str(binding["actor_id"]),
                "OPENCLANK_HISTORY_TOKEN": str(binding["token"]),
                "OPENCLANK_HISTORY_WORKSPACE_ID": str(binding.get("workspace_id") or "default"),
                "OPENCLANK_HISTORY_WORKSPACE_ROOT": str(binding.get("workspace_root") or ""),
            }
        )
        root_bindings = binding.get("root_bindings")
        if isinstance(root_bindings, list) and root_bindings:
            environment["OPENCLANK_HISTORY_ROOT_BINDINGS"] = json.dumps(
                root_bindings, separators=(",", ":"), sort_keys=True
            )
        return environment

    def _refresh_history_binding_locked(self) -> None:
        """Respawn before a request when the trusted capture binding changed."""
        current = self._history_environment()
        if current == self._history_environment_snapshot:
            return
        if self._active_grpc_operations:
            raise FilesServiceError(
                "filesystem History binding changed; retry after the active operation",
                code="history_binding_changed",
            )
        # Framed calls hold this lock through their exchange. Tonic calls use
        # the active count below, so closing here cannot interrupt an in-flight
        # request or stream.
        self.close()
        self._history_environment_snapshot = None

    def _end_grpc_operation(self) -> None:
        with self._lock:
            self._active_grpc_operations = max(0, self._active_grpc_operations - 1)

    def _cancel_obsolete_watches_locked(self) -> None:
        # A client may be used by another event loop. Cancel only its watches
        # on their owning loops; reads and mutations retain the old service.
        for loop, call in tuple(self._grpc_watch_calls):
            try:
                loop.call_soon_threadsafe(call.cancel)
            except RuntimeError:
                # A closed owning loop cannot release its stream here. The
                # admission deadline must fail without closing other work.
                pass

    async def _begin_grpc_operation(self) -> None:
        deadline = asyncio.get_running_loop().time() + 5.0
        while True:
            with self._lock:
                current = self._history_environment()
                if self._active_grpc_operations and current != self._history_environment_snapshot:
                    self._cancel_obsolete_watches_locked()
                    wait_for_active = True
                else:
                    if not self._active_grpc_operations:
                        self._refresh_history_binding_locked()
                    self._active_grpc_operations += 1
                    return
            if wait_for_active:
                if asyncio.get_running_loop().time() >= deadline:
                    raise FilesServiceError(
                        "filesystem History binding changed; retry after the active operation",
                        code="history_binding_changed",
                    )
                await asyncio.sleep(0.01)

    def _service_environment(self) -> dict[str, str]:
        environment = os.environ.copy()
        environment.update(
            {
                "ODYSSEUS_FILES_LANE": "app",
                "ODYSSEUS_FILES_OWNER_ID": self.owner,
                "ODYSSEUS_FILES_PRINCIPAL_ID": "odysseus-app",
                "ODYSSEUS_FILES_SESSION_ID": uuid.uuid4().hex,
                # The signed scope contains root IDs, so the Rust process
                # must load the same server-owned snapshot to resolve those
                # IDs. It is never accepted from a browser request.
                "ODYSSEUS_FILES_APP_SCOPE": json.dumps(self.app_scope, separators=(",", ":"), sort_keys=True),
            }
        )
        try:
            registry_path = self._registry().rust_snapshot_path(expected_generation=self._policy_generation)
        except FilesystemRegistryError as error:
            raise FilesServiceError(str(error), code=error.code) from error
        # Host-wide administrator compatibility does not need the registry;
        # omitting a not-yet-created snapshot also lets an unassigned user
        # start and receive a typed deny instead of a transport failure.
        if not self.app_scope.get("host") and Path(registry_path).is_file():
            environment["ODYSSEUS_FILES_REGISTRY"] = registry_path
        else:
            environment.pop("ODYSSEUS_FILES_REGISTRY", None)
        history_names = (
            "OPENCLANK_HISTORY_SOCKET",
            "OPENCLANK_HISTORY_ACCOUNT_ID",
            "OPENCLANK_HISTORY_ACTOR_ID",
            "OPENCLANK_HISTORY_TOKEN",
            "OPENCLANK_HISTORY_WORKSPACE_ID",
            "OPENCLANK_HISTORY_WORKSPACE_ROOT",
            "OPENCLANK_HISTORY_ROOT_BINDINGS",
        )
        for name in history_names:
            environment.pop(name, None)
        history_environment = self._history_environment()
        environment.update(history_environment)
        self._history_environment_snapshot = history_environment
        return environment

    @staticmethod
    def _read_exact(stream: Any, length: int) -> bytes:
        chunks: list[bytes] = []
        remaining = length
        while remaining:
            chunk = stream.read(remaining)
            if not chunk:
                raise FilesServiceError("Rust filesystem service closed its transport")
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)

    def _request_sync(self, request: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            self._assert_policy_generation()
            self._refresh_history_binding_locked()
            self._start_locked()
            return self._exchange_locked(request)

    def _exchange_locked(self, request: dict[str, Any]) -> dict[str, Any]:
        """Exchange one framed request while the per-client transport lock is held."""
        process = self._process
        if process is None or process.stdin is None or process.stdout is None:
            raise FilesServiceError("Rust filesystem service transport is unavailable")
        body = json.dumps(request, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        if len(body) > _MAX_FRAME_BYTES:
            raise FilesServiceError("filesystem request exceeds frame limit", code="backpressure")
        try:
            process.stdin.write(_FRAME.pack(len(body)) + body)
            process.stdin.flush()
            header = self._read_exact(process.stdout, _FRAME.size)
            length = _FRAME.unpack(header)[0]
            if length > _MAX_FRAME_BYTES:
                raise FilesServiceError("filesystem response exceeds frame limit", code="backpressure")
            response = json.loads(self._read_exact(process.stdout, length).decode("utf-8"))
        except (BrokenPipeError, OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            self.close()
            raise FilesServiceError(f"Rust filesystem service request failed: {error}") from error
        if not isinstance(response, dict):
            raise FilesServiceError("Rust filesystem service returned an invalid response")
        if response.get("code"):
            raise FilesServiceError(
                str(response.get("message") or "filesystem request failed"),
                code=str(response["code"]),
            )
        return response

    async def request(self, operation: str, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        request = self._request_envelope(operation, path=path, payload=payload)
        return await self._dispatch_request(request)

    async def stage_begin(self, path: str) -> dict[str, Any]:
        """Begin an identity-bound service-owned upload stage."""
        return await self.request("stage_begin", path, {})

    async def stage_chunk(self, stage_id: str, chunk: bytes, *, offset: int) -> dict[str, Any]:
        if len(chunk) > 512 * 1024:
            raise FilesServiceError("staging chunk exceeds the bound", code="backpressure")
        return await self.request("stage_chunk", stage_id, {
            "stage_id": str(stage_id), "offset": int(offset),
            "chunk": base64.b64encode(chunk).decode("ascii"),
        })

    async def stage_finish(self, stage_id: str, target: str, *, length: int, digest: str) -> dict[str, Any]:
        return await self.request("stage_finish", stage_id, {
            "stage_id": str(stage_id), "target": str(target),
            "length": int(length), "digest": str(digest),
        })

    async def stage_abort(self, stage_id: str) -> dict[str, Any]:
        return await self.request("stage_abort", stage_id, {"stage_id": str(stage_id)})

    async def request_handle(
        self,
        operation: str,
        handle: dict[str, Any],
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        request = self._request_envelope(operation, handle=handle, payload=payload)
        return await self._dispatch_request(request)

    async def _dispatch_request(self, request: dict[str, Any]) -> dict[str, Any]:
        if self.transport == "grpc":
            return await self._grpc_request(request)
        if self.transport != "framed":
            raise FilesServiceError(
                f"unknown filesystem transport: {self.transport}",
                code="protocol_mismatch",
            )
        return await asyncio.to_thread(self._request_sync, request)

    @staticmethod
    def _request_envelope(
        operation: str,
        path: str | None = None,
        payload: dict[str, Any] | None = None,
        *,
        handle: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if (path is None) == (handle is None):
            raise FilesServiceError("exactly one filesystem target is required", code="malformed_request")
        return {
            "protocol": {"major": 1, "minor": 0},
            "request_id": uuid.uuid4().hex,
            "session_id": "odysseus-http",
            "root_operation_id": uuid.uuid4().hex,
            "operation": operation,
            "target": {
                "kind": "handle" if handle is not None else "path",
                "value": dict(handle) if handle is not None else str(path),
            },
            "payload": payload or {},
        }

    async def open_handle(self, path: str) -> dict[str, Any]:
        response = await self.request("open_handle", path, {})
        data = response.get("data") or {}
        handle = data.get("handle")
        if not isinstance(handle, dict) or not str(handle.get("token") or ""):
            raise FilesServiceError("Rust filesystem service returned an invalid handle")
        return data

    @staticmethod
    def _grpc_modules() -> tuple[Any, Any, Any]:
        try:
            import grpc

            from src.openclank import files_transport_pb2 as wire
            from src.openclank import files_transport_pb2_grpc as wire_grpc
        except (ImportError, RuntimeError) as error:
            raise FilesServiceError(
                "Tonic filesystem transport requires pinned grpcio/protobuf runtime packages",
                code="root_unavailable",
            ) from error
        return grpc, wire, wire_grpc

    async def _grpc_connection(self) -> tuple[Any, Any, tuple[tuple[str, str], ...]]:
        self._assert_policy_generation()
        with self._lock:
            socket_path, session_binding = self._start_grpc_locked()
            process = self._process
        deadline = asyncio.get_running_loop().time() + 5.0
        while True:
            if process is None or process.poll() is not None:
                self.close()
                raise FilesServiceError(
                    "Tonic filesystem service exited before readiness",
                    code="root_unavailable",
                )
            try:
                if isinstance(socket_path, str) or stat.S_ISSOCK(socket_path.lstat().st_mode):
                    break
            except FileNotFoundError:
                pass
            if asyncio.get_running_loop().time() >= deadline:
                self.close()
                raise FilesServiceError(
                    "Tonic filesystem service timed out before readiness",
                    code="root_unavailable",
                )
            await asyncio.sleep(0.01)
        grpc, wire, wire_grpc = self._grpc_modules()
        channel = grpc.aio.insecure_channel(
            socket_path if isinstance(socket_path, str) else f"unix:{socket_path}",
            options=(
                ("grpc.max_send_message_length", _MAX_FRAME_BYTES + 4096),
                ("grpc.max_receive_message_length", _MAX_FRAME_BYTES + 4096),
                ("grpc.default_authority", "openclank.local"),
            ),
        )
        try:
            await asyncio.wait_for(channel.channel_ready(), timeout=5.0)
            stub = wire_grpc.FilesTransportStub(channel)
            metadata = (("x-open-clank-session", session_binding),)
            if not self._ready:
                health = await stub.Health(wire.HealthRequest(), metadata=metadata, timeout=2.0)
                if (
                    not health.ready
                    or health.transport != ("authenticated-loopback-tcp" if sys.platform == "win32" else "private-unix-domain-socket")
                    or health.protocol_major != 1
                ):
                    raise FilesServiceError(
                        "Tonic filesystem service failed readiness negotiation",
                        code="protocol_mismatch",
                    )
                self.stable_object_handles = bool(health.stable_object_handles)
                self._ready = True
            return channel, stub, metadata
        except Exception:
            await channel.close()
            raise

    async def _grpc_request(self, request: dict[str, Any]) -> dict[str, Any]:
        grpc, wire, _wire_grpc = self._grpc_modules()
        channel = None
        await self._begin_grpc_operation()
        try:
            channel, stub, metadata = await self._grpc_connection()
            body = json.dumps(request, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
            if len(body) > _MAX_FRAME_BYTES:
                raise FilesServiceError("filesystem request exceeds frame limit", code="backpressure")
            response = await stub.Dispatch(
                wire.DispatchRequest(envelope_json=body),
                metadata=metadata,
                timeout=30.0,
            )
            if len(response.envelope_json) > _MAX_FRAME_BYTES:
                raise FilesServiceError("filesystem response exceeds frame limit", code="backpressure")
            decoded = json.loads(response.envelope_json.decode("utf-8"))
        except grpc.aio.AioRpcError as error:
            raise self._grpc_service_error(error) from error
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise FilesServiceError(f"Tonic filesystem response is invalid: {error}") from error
        finally:
            if channel is not None:
                await channel.close()
            self._end_grpc_operation()
        if not isinstance(decoded, dict):
            raise FilesServiceError("Tonic filesystem service returned an invalid response")
        if decoded.get("code"):
            raise FilesServiceError(
                str(decoded.get("message") or "filesystem request failed"),
                code=str(decoded["code"]),
            )
        return decoded

    async def stream_read(
        self,
        path: str,
        *,
        offset: int = 0,
        length: int,
        chunk_bytes: int = 256 * 1024,
    ) -> AsyncIterator[bytes]:
        """Yield bounded Rust-authorized chunks over the opt-in Tonic lane.

        The current production service reauthorizes the path for every chunk;
        it does not yet promise descriptor-stable identity across a stream.
        """
        if self.transport != "grpc":
            raise FilesServiceError(
                "streaming reads require ODYSSEUS_FILES_TRANSPORT=grpc",
                code="protocol_mismatch",
            )
        if offset < 0 or length <= 0 or length > 250 * 1024 * 1024:
            raise FilesServiceError("stream range is outside the bounded transport contract", code="backpressure")
        if chunk_bytes < 64 * 1024 or chunk_bytes > 256 * 1024:
            raise FilesServiceError("stream chunk size is outside the bounded transport contract", code="backpressure")
        envelope = self._request_envelope("read_range", path=path)
        async for chunk in self._stream_read_envelope(
            envelope,
            offset=offset,
            length=length,
            chunk_bytes=chunk_bytes,
        ):
            yield chunk

    async def stream_read_handle(
        self,
        handle: dict[str, Any],
        *,
        offset: int = 0,
        length: int,
        chunk_bytes: int = 256 * 1024,
    ) -> AsyncIterator[bytes]:
        """Read an already-open Rust object without reopening its pathname."""
        if self.transport == "framed":
            remaining = int(length)
            current = int(offset)
            if current < 0 or remaining <= 0 or remaining > 250 * 1024 * 1024:
                raise FilesServiceError(
                    "stream range is outside the bounded transport contract",
                    code="backpressure",
                )
            while remaining > 0:
                page_size = min(remaining, 1024 * 1024)
                response = await self.request_handle(
                    "read_range",
                    handle,
                    {"offset": current, "length": page_size, "include_fingerprint": False},
                )
                data = response.get("data") or {}
                raw = data.get("bytes")
                if not isinstance(raw, list) or any(not isinstance(value, int) or not 0 <= value <= 255 for value in raw):
                    raise FilesServiceError("filesystem handle stream is invalid", code="partial_stream")
                chunk = bytes(raw)
                if chunk:
                    yield chunk
                current += len(chunk)
                remaining -= len(chunk)
                if bool(data.get("eof")) or len(chunk) < page_size:
                    break
            return
        envelope = self._request_envelope("read_range", handle=handle)
        async for chunk in self._stream_read_envelope(
            envelope,
            offset=offset,
            length=length,
            chunk_bytes=chunk_bytes,
        ):
            yield chunk

    async def thumbnail_handle(
        self,
        handle: dict[str, Any],
        *,
        width: int,
        height: int,
        scale: float = 1.0,
        deadline_ms: int = 2_000,
        icon: bool = False,
    ) -> bytes:
        """Return native content pixels, or explicitly requested Windows Shell icon pixels."""
        if self.transport != "grpc":
            raise FilesServiceError(
                "native thumbnails require the private Tonic transport",
                code="protocol_mismatch",
            )
        if not (1 <= int(width) <= 1024 and 1 <= int(height) <= 1024):
            raise FilesServiceError("thumbnail dimensions are outside the bounded contract", code="backpressure")
        scale_milli = round(float(scale) * 1000)
        if not 1_000 <= scale_milli <= 3_000 or not 1 <= int(deadline_ms) <= 2_000:
            raise FilesServiceError("thumbnail scale or deadline is outside the bounded contract", code="backpressure")
        envelope = self._request_envelope(
            "thumbnail",
            handle=handle,
            payload={
                "width": int(width),
                "height": int(height),
                "scale_milli": scale_milli,
                "deadline_ms": int(deadline_ms),
                "icon": bool(icon),
            },
        )
        grpc, wire, _wire_grpc = self._grpc_modules()
        body = json.dumps(envelope, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        channel = None
        call = None
        await self._begin_grpc_operation()
        try:
            channel, stub, metadata = await self._grpc_connection()
            call = stub.Thumbnail(
                wire.NativeThumbnailRequest(envelope_json=body),
                metadata=metadata,
                timeout=(int(deadline_ms) / 1000) + 1.0,
            )
            response = await call
            png = bytes(response.png)
            if len(png) > 4 * 1024 * 1024 or not png.startswith(b"\x89PNG\r\n\x1a\n"):
                raise FilesServiceError("native thumbnail response is invalid", code="partial_stream")
            return png
        except asyncio.CancelledError:
            if call is not None:
                call.cancel()
            raise
        except grpc.aio.AioRpcError as error:
            raise self._grpc_service_error(error) from error
        finally:
            if channel is not None:
                await channel.close()
            self._end_grpc_operation()

    async def watch_path(self, path: str) -> AsyncIterator[dict[str, Any]]:
        """Yield path-free hints for one authorized, non-recursive directory."""
        if self.transport != "grpc":
            raise FilesServiceError(
                "filesystem watches require the private Tonic transport",
                code="protocol_mismatch",
            )
        envelope = self._request_envelope(
            "watch_subscribe",
            path=path,
            payload={"recursive": False},
        )
        grpc, wire, _wire_grpc = self._grpc_modules()
        body = json.dumps(envelope, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        channel = None
        call = None
        watch_registration = None
        expected_sequence = 0
        allowed_kinds = {"created", "modified", "deleted", "rescan_required"}
        expected_generation = int(self.app_scope.get("generation") or 0)
        await self._begin_grpc_operation()
        try:
            channel, stub, metadata = await self._grpc_connection()
            call = stub.Watch(
                wire.WatchRequest(envelope_json=body),
                metadata=metadata,
            )
            with self._lock:
                watch_registration = (asyncio.get_running_loop(), call)
                self._grpc_watch_calls.add(watch_registration)
                # Binding may rotate between admission and Watch creation.
                if self._history_environment() != self._history_environment_snapshot:
                    self._cancel_obsolete_watches_locked()
            async for event in call:
                self._assert_policy_generation()
                if event.request_id != envelope["request_id"] or event.sequence != expected_sequence:
                    raise FilesServiceError(
                        "Tonic watcher continuity check failed",
                        code="partial_stream",
                    )
                expected_sequence += 1
                if int(event.policy_generation) != expected_generation:
                    raise FilesServiceError(
                        "Tonic watcher policy generation changed",
                        code="policy_generation_changed",
                    )
                kind = str(event.kind or "")
                if kind not in allowed_kinds:
                    raise FilesServiceError(
                        "Tonic watcher returned an invalid event",
                        code="partial_stream",
                    )
                yield {
                    "sequence": int(event.sequence),
                    "kind": kind,
                    "rescan_required": bool(event.rescan_required),
                    "observed_unix_ms": int(event.observed_unix_ms),
                }
        except asyncio.CancelledError:
            if call is not None:
                call.cancel()
            raise
        except grpc.aio.AioRpcError as error:
            raise self._grpc_service_error(error) from error
        finally:
            if watch_registration is not None:
                with self._lock:
                    self._grpc_watch_calls.discard(watch_registration)
            if call is not None:
                call.cancel()
            if channel is not None:
                await channel.close()
            self._end_grpc_operation()

    async def _stream_read_envelope(
        self,
        envelope: dict[str, Any],
        *,
        offset: int,
        length: int,
        chunk_bytes: int,
    ) -> AsyncIterator[bytes]:
        if self.transport != "grpc":
            raise FilesServiceError(
                "streaming reads require the private Tonic transport",
                code="protocol_mismatch",
            )
        if offset < 0 or length <= 0 or length > 250 * 1024 * 1024:
            raise FilesServiceError("stream range is outside the bounded transport contract", code="backpressure")
        if chunk_bytes < 64 * 1024 or chunk_bytes > 256 * 1024:
            raise FilesServiceError("stream chunk size is outside the bounded transport contract", code="backpressure")
        grpc, wire, _wire_grpc = self._grpc_modules()
        body = json.dumps(envelope, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        channel = None
        call = None
        expected_sequence = 0
        expected_offset = offset
        saw_final = False
        await self._begin_grpc_operation()
        try:
            channel, stub, metadata = await self._grpc_connection()
            call = stub.Read(
                wire.ReadRequest(
                    envelope_json=body,
                    offset=offset,
                    length=length,
                    chunk_bytes=chunk_bytes,
                ),
                metadata=metadata,
                timeout=300.0,
            )
            async for chunk in call:
                # The Rust process receives an immutable server-issued scope.
                # Recheck the registry generation before exposing every
                # buffered chunk so a non-admin revocation cancels an active
                # stream instead of waiting for its old process to finish.
                self._assert_policy_generation()
                if chunk.request_id != envelope["request_id"]:
                    raise FilesServiceError("Tonic stream request identity changed", code="partial_stream")
                if chunk.sequence != expected_sequence or chunk.offset != expected_offset:
                    raise FilesServiceError("Tonic stream continuity check failed", code="partial_stream")
                expected_sequence += 1
                expected_offset += len(chunk.data)
                saw_final = bool(chunk.final_chunk)
                yield bytes(chunk.data)
                if saw_final:
                    break
            if not saw_final:
                raise FilesServiceError("Tonic stream ended without a final chunk", code="partial_stream")
        except grpc.aio.AioRpcError as error:
            raise self._grpc_service_error(error) from error
        finally:
            if call is not None and not saw_final:
                call.cancel()
            if channel is not None:
                await channel.close()
            self._end_grpc_operation()

    @staticmethod
    def _grpc_service_error(error: Any) -> FilesServiceError:
        status_name = str(error.code().name).lower()
        code = {
            "invalid_argument": "malformed_request",
            "unauthenticated": "unauthorized",
            "permission_denied": "denied",
            "failed_precondition": "invalid_path",
            "aborted": "conflict",
            "deadline_exceeded": "deadline_exceeded",
            "cancelled": "cancelled",
            "resource_exhausted": "backpressure",
            "unimplemented": "unsupported",
            "unavailable": "root_unavailable",
        }.get(status_name, "root_unavailable")
        return FilesServiceError(str(error.details() or "Tonic filesystem request failed"), code=code)

    def close(self) -> None:
        with self._lock:
            process, self._process = self._process, None
            self._ready = False
            self.stable_object_handles = False
            private_directory, self._grpc_tempdir = self._grpc_tempdir, None
            self._grpc_socket_path = None
            self._grpc_session = None
            if process is not None:
                try:
                    if process.stdin is not None:
                        process.stdin.close()
                except OSError:
                    pass
                try:
                    process.terminate()
                    process.wait(timeout=1)
                except (OSError, subprocess.TimeoutExpired):
                    try:
                        process.kill()
                        process.wait(timeout=1)
                    except (OSError, subprocess.TimeoutExpired):
                        pass
            if private_directory is not None:
                private_directory.cleanup()


def _scope_cache_key(app_scope: dict[str, Any] | None) -> str:
    scope = app_scope or {"host": True}
    return json.dumps(scope, separators=(",", ":"), sort_keys=True)


def _transport_cache_key(transport: str | None = None) -> str:
    return (transport or os.environ.get("ODYSSEUS_FILES_TRANSPORT") or "framed").strip().lower()


def client_for_owner(
    owner: str,
    *,
    app_scope: dict[str, Any] | None = None,
    transport: str | None = None,
) -> OdysseusFilesClient:
    """Return a lazy client bound to one immutable server-issued app scope.

    The scope is part of the cache key so a permission generation change can
    never reuse a process that was started with an older visibility set.
    ``close_all_clients`` remains the eager invalidation path used by settings
    mutations.
    """
    key = (str(owner), _scope_cache_key(app_scope), _transport_cache_key(transport))
    stale: list[OdysseusFilesClient] = []
    with _CLIENTS_LOCK:
        for old_key in list(_CLIENTS):
            if old_key[0] == key[0] and old_key != key:
                stale.append(_CLIENTS.pop(old_key))
        client = _CLIENTS.get(key)
        if client is None:
            client = OdysseusFilesClient(owner, app_scope=app_scope, transport=transport)
            _CLIENTS[key] = client
    for old_client in stale:
        old_client.close()
    return client


class AgentFilesClient(OdysseusFilesClient):
    """Rust service client bound to one agent owner and workspace folder."""

    def __init__(self, owner: str, active_workspace: str | None, *, binary: str | None = None, workspace_id: str = "", chat_id: str = "") -> None:
        super().__init__(owner, binary=binary)
        self.workspace_id = str(workspace_id)
        self.chat_id = str(chat_id)
        self.active_workspace = str(Path(active_workspace).expanduser().resolve(strict=False)) if active_workspace else None
        self._agent_policy_generation: int | None = None

    def _assert_policy_generation(self) -> None:
        expected = self._agent_policy_generation
        if expected is None:
            return
        registry = self._registry()
        try:
            current = registry.generation()
        except Exception as error:
            self.close()
            raise FilesServiceError(
                "filesystem policy generation could not be validated",
                code="policy_generation_changed",
            ) from error
        if current != expected:
            self.close()
            raise FilesServiceError(
                "filesystem policy generation changed; retry with a fresh scope",
                code="policy_generation_changed",
            )

    def _service_environment(self) -> dict[str, str]:
        registry = self._registry()
        is_admin = owner_is_admin_or_single_user(self.owner)
        app_visibility = None if is_admin else registry.visibility_for_subject(self.owner)
        try:
            scope = registry.agent_scope(self.owner, self.active_workspace, app_visibility=app_visibility,
                workspace_id=self.workspace_id, chat_id=self.chat_id)
            self._agent_policy_generation = int(scope.pop("generation"))
            self._policy_generation = self._agent_policy_generation
            registry_path = registry.rust_snapshot_path(expected_generation=self._agent_policy_generation)
        except FilesystemRegistryError as error:
            raise FilesServiceError(str(error), code=error.code) from error
        # Compute the signed root scope before asking the base client for its
        # history binding. This keeps agent history capture tied to the same
        # allowlisted root IDs as the Files operation instead of the base
        # client's host compatibility scope.
        visible_root_ids = list(scope.get("approved_root_ids") or [])
        aggregate_capabilities = sorted(
            {
                str(value)
                for values in (scope.get("root_capabilities") or {}).values()
                for value in (values or [])
            }
        )
        self.app_scope = {
            "host": False,
            "visible_root_ids": visible_root_ids,
            "capabilities": aggregate_capabilities,
            "root_capabilities": scope.get("root_capabilities") or {},
            "generation": self._agent_policy_generation,
            "active_folder": scope.get("active_folder"),
        }
        environment = super()._service_environment()
        environment.update(
            {
                "ODYSSEUS_FILES_LANE": "agent",
                "ODYSSEUS_FILES_PRINCIPAL_ID": "odysseus-agent",
                "ODYSSEUS_FILES_REGISTRY": registry_path,
                "ODYSSEUS_FILES_SCOPE": json.dumps(scope, separators=(",", ":"), sort_keys=True),
            }
        )
        return environment


def agent_client_for(owner: str, active_workspace: str | None, *, workspace_id: str = "", chat_id: str = "") -> AgentFilesClient:
    registry_path = os.environ.get("ODYSSEUS_FILES_REGISTRY")
    registry = FilesystemRootRegistry(registry_path) if registry_path else FilesystemRootRegistry()
    generation = registry.generation()
    key = (
        str(owner),
        str(active_workspace) if active_workspace else None,
        str(workspace_id),
        str(chat_id),
        generation,
        _transport_cache_key(),
    )
    stale: list[AgentFilesClient] = []
    with _CLIENTS_LOCK:
        for old_key in list(_AGENT_CLIENTS):
            if old_key[:4] == key[:4] and old_key != key:
                stale.append(_AGENT_CLIENTS.pop(old_key))
        client = _AGENT_CLIENTS.get(key)
        if client is None:
            client = AgentFilesClient(key[0], key[1], workspace_id=key[2], chat_id=key[3])
            _AGENT_CLIENTS[key] = client
    for old_client in stale:
        old_client.close()
    return client


def close_all_clients() -> None:
    with _CLIENTS_LOCK:
        clients = list(_CLIENTS.values())
        clients.extend(_AGENT_CLIENTS.values())
        _CLIENTS.clear()
        _AGENT_CLIENTS.clear()
    for client in clients:
        client.close()


def close_clients_for_owner(owner: str) -> int:
    """Close cached file-service processes for one owner without collateral."""
    normalized = str(owner or "").strip().lower()
    if not normalized:
        return 0
    clients = []
    with _CLIENTS_LOCK:
        for cache in (_CLIENTS, _AGENT_CLIENTS):
            for key in list(cache):
                if str(key[0]).strip().lower() == normalized:
                    clients.append(cache.pop(key))
    for client in clients:
        client.close()
    return len(clients)
