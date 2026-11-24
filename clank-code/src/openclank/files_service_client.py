"""Small app-principal client for the long-lived Rust filesystem service.

The client owns process framing and lifecycle only. Path containment, encoding,
atomicity, and search semantics remain in the Rust service.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import queue
import re
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


class _PreparedCheckpoint:
    """Process-local custody, never serialized or accepted from a caller."""
    __slots__ = ("client", "process", "selector", "used")
    def __init__(self, client, process, selector):
        self.client, self.process, self.selector, self.used = client, process, selector, False


class _PreparedOrdinaryReplace:
    """One original framed process, binding and single write attempt."""
    __slots__ = ("client", "process", "path", "binding_json", "history", "used", "settled")

    def __init__(self, client, process, path, binding_json, history):
        self.client, self.process, self.path = client, process, path
        self.binding_json, self.history = binding_json, history
        self.used = False
        self.settled = False

    @property
    def capture_identity(self) -> dict[str, str]:
        """Content-free original capture principal for the durable Host receipt."""
        return {name: str(self.history.get(env) or "") for name, env in (
            ("account_id", "OPENCLANK_HISTORY_ACCOUNT_ID"),
            ("actor_id", "OPENCLANK_HISTORY_ACTOR_ID"),
            ("workspace_id", "OPENCLANK_HISTORY_WORKSPACE_ID"),
        )}


class _PreparedOrdinaryCreate:
    """One original framed process, binding and single create attempt."""
    __slots__ = ("client", "process", "path", "binding_json", "history", "used", "settled")

    def __init__(self, client, process, path, binding_json, history):
        self.client, self.process, self.path = client, process, path
        self.binding_json, self.history = binding_json, history
        self.used = False
        self.settled = False

    @property
    def capture_identity(self) -> dict[str, str]:
        """Content-free original capture principal for the durable Host receipt."""
        return {name: str(self.history.get(env) or "") for name, env in (
            ("account_id", "OPENCLANK_HISTORY_ACCOUNT_ID"),
            ("actor_id", "OPENCLANK_HISTORY_ACTOR_ID"),
            ("workspace_id", "OPENCLANK_HISTORY_WORKSPACE_ID"),
        )}


async def _checkpoint_owned_thread(call):
    task = asyncio.create_task(asyncio.to_thread(call))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        while not task.done():
            try: await asyncio.shield(task)
            except asyncio.CancelledError: continue
            except Exception: break
        if task.done() and not task.cancelled(): task.exception()
        raise


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
        self.binary = binary or os.environ.get("OPEN_CLANK_FILES_SERVICE_BIN")
        selected_transport = (transport or os.environ.get("OPEN_CLANK_FILES_TRANSPORT") or "framed").strip().lower()
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
        self._policy_generation = int(self.app_scope.get("generation") or 0) or None
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
            "Rust filesystem service is not packaged or built; set OPEN_CLANK_FILES_SERVICE_BIN",
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
        if sys.platform not in {"darwin", "linux", "win32"}:
            raise FilesServiceError(
                "Tonic filesystem transport is available only on macOS, Linux or Windows",
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
                "OPEN_CLANK_FILES_GRPC_SOCKET": str(socket_path),
                "OPEN_CLANK_FILES_GRPC_SESSION": session_binding,
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
        registry_path = os.environ.get("OPEN_CLANK_FILES_REGISTRY")
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
        required = isinstance(self, AgentFilesClient) and os.environ.get("OPEN_CLANK_MANAGED_AGENT_HISTORY_REQUIRED") == "1"
        unavailable = {"OPENCLANK_HISTORY_SOCKET": "history-binding-unavailable"} if required else environment
        if not callable(_HISTORY_BINDING_PROVIDER):
            return unavailable
        try:
            lane = "agent" if isinstance(self, AgentFilesClient) else "human"
            try:
                binding = _HISTORY_BINDING_PROVIDER(self.owner, lane, dict(self.app_scope or {}), files_child=True)
            except TypeError:
                binding = history_binding_for(self.owner, lane, self.app_scope)
        except Exception:
            # A configured lookup outage must not masquerade as intentionally
            # unconfigured capture. The native engine keeps reads available,
            # but its required-capture admission blocks every live mutation.
            return {"OPENCLANK_HISTORY_SOCKET": "history-binding-unavailable"}
        if not binding:
            return unavailable
        if required and (not isinstance(binding, dict) or
                not all(isinstance(binding.get(key), str) and binding[key].strip() for key in
                        ("socket", "account_id", "actor_id", "token", "workspace_id", "workspace_root")) or
                not os.path.isabs(binding["socket"]) or not os.path.isabs(binding["workspace_root"]) or
                not isinstance(binding.get("root_bindings"), list) or not binding["root_bindings"]):
            return unavailable
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
        checkpoint_tokens = binding.get("checkpoint_tokens")
        if isinstance(checkpoint_tokens, dict) and checkpoint_tokens:
            environment["OPENCLANK_HISTORY_CHECKPOINT_TOKENS"] = json.dumps(
                checkpoint_tokens, separators=(",", ":"), sort_keys=True
            )
        worktree_tokens = binding.get("worktree_tokens")
        if isinstance(worktree_tokens, dict) and worktree_tokens:
            environment["OPENCLANK_HISTORY_WORKTREE_TOKENS"] = json.dumps(
                worktree_tokens, separators=(",", ":"), sort_keys=True
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
        # Private one-invocation provenance is injected only by its Host owner.
        environment.pop("OPENCLANK_GRAPH_CAPTURE_CONTEXT", None)
        environment.pop("OPENCLANK_ORDINARY_WRITER_CONTEXT", None)
        from src.constants import DATA_DIR
        owner_key = hashlib.sha256(str(self.owner).encode("utf-8")).hexdigest()
        environment["OPEN_CLANK_FILES_MANAGED_JOURNAL_ROOT"] = str(Path(DATA_DIR) / "files-transactions-v1" / owner_key)
        environment.update(
            {
                "OPEN_CLANK_FILES_LANE": "app",
                "OPEN_CLANK_FILES_OWNER_ID": self.owner,
                "OPEN_CLANK_FILES_PRINCIPAL_ID": "odysseus-app",
                "OPEN_CLANK_FILES_SESSION_ID": uuid.uuid4().hex,
                # The signed scope contains root IDs, so the Rust process
                # must load the same server-owned snapshot to resolve those
                # IDs. It is never accepted from a browser request.
                "OPEN_CLANK_FILES_APP_SCOPE": json.dumps(self.app_scope, separators=(",", ":"), sort_keys=True),
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
            environment["OPEN_CLANK_FILES_REGISTRY"] = registry_path
        else:
            environment.pop("OPEN_CLANK_FILES_REGISTRY", None)
        history_names = (
            "OPENCLANK_HISTORY_SOCKET",
            "OPENCLANK_HISTORY_ACCOUNT_ID",
            "OPENCLANK_HISTORY_ACTOR_ID",
            "OPENCLANK_HISTORY_TOKEN",
            "OPENCLANK_HISTORY_WORKSPACE_ID",
            "OPENCLANK_HISTORY_WORKSPACE_ROOT",
            "OPENCLANK_HISTORY_ROOT_BINDINGS",
            "OPENCLANK_HISTORY_CHECKPOINT_TOKENS",
            "OPENCLANK_HISTORY_WORKTREE_TOKENS",
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

    async def request(self, operation: str, path: str, payload: dict[str, Any] | None = None, *, root_operation_id: str | None = None) -> dict[str, Any]:
        request = self._request_envelope(operation, path=path, payload=payload, root_operation_id=root_operation_id)
        return await self._dispatch_request(request)

    async def prepare_ordinary_replace(self, path: str, *, binding: dict[str, Any]) -> _PreparedOrdinaryReplace:
        """Negotiate the original framed process before the Host issues an effect."""
        if self.transport != "framed" or not isinstance(binding, dict):
            raise FilesServiceError("ordinary write unavailable", code="ordinary_write_unsent")
        try:
            frozen = json.dumps(binding, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
        except (TypeError, ValueError) as error:
            raise FilesServiceError("ordinary write unavailable", code="ordinary_write_unsent") from error
        def run():
            with self._lock:
                try:
                    self._assert_policy_generation()
                    self._refresh_history_binding_locked()
                    self._start_locked()
                    process = self._process
                    history = dict(self._history_environment_snapshot or {})
                    caps = self._exchange_locked(self._request_envelope("capabilities", path=path, payload={}))
                    if (process is None or process.poll() is not None or self._process is not process
                            or self._history_environment() != history
                            or not {"ordinary_replace", "ordinary_replace_status"}.issubset(set(caps.get("operations") or ()))):
                        raise ValueError("unsupported or changed")
                    self._assert_policy_generation()
                    return _PreparedOrdinaryReplace(self, process, str(path), frozen, history)
                except Exception as error:
                    raise FilesServiceError("ordinary write unavailable", code="ordinary_write_unsent") from error
        return await _checkpoint_owned_thread(run)

    async def ordinary_replace(self, prepared: _PreparedOrdinaryReplace, *, content: bytes) -> dict[str, Any]:
        """Consume one original process handle immediately before the actual exchange."""
        if not isinstance(prepared, _PreparedOrdinaryReplace) or prepared.client is not self:
            raise FilesServiceError("ordinary write unavailable", code="ordinary_write_unsent")
        def run():
            with self._lock:
                if prepared.used:
                    raise FilesServiceError("ordinary write already issued; inspect original status", code="ordinary_write_already_issued")
                try:
                    self._assert_policy_generation()
                    if (self._history_environment() != prepared.history or self._process is not prepared.process
                            or prepared.process.poll() is not None):
                        raise ValueError("original process changed")
                    binding = json.loads(prepared.binding_json)
                    request = self._request_envelope("ordinary_replace", path=prepared.path,
                        payload={"binding": binding, "bytes": list(content)}, root_operation_id=binding["operation_id"])
                except Exception as error:
                    prepared.used = True
                    prepared.settled = True
                    raise FilesServiceError("ordinary write unavailable", code="ordinary_write_unsent") from error
                prepared.used = True
                try:
                    return self._exchange_locked(request)
                finally:
                    prepared.settled = True
        return await _checkpoint_owned_thread(run)

    async def ordinary_replace_status(self, prepared: _PreparedOrdinaryReplace) -> dict[str, Any]:
        """Only read the original receipt after the owned exchange settles or process exits."""
        if (not isinstance(prepared, _PreparedOrdinaryReplace) or prepared.client is not self
                or not prepared.used or (not prepared.settled and prepared.process.poll() is None)):
            raise FilesServiceError("original write remains in flight", code="ordinary_write_unknown")
        return await self.ordinary_replace_status_exact(
            prepared.path, binding=json.loads(prepared.binding_json), capture_identity=prepared.capture_identity)

    async def ordinary_replace_status_exact(self, path: str, *, binding: dict[str, Any],
                                            capture_identity: dict[str, str]) -> dict[str, Any]:
        """Read only an original action after caller-proven IO settlement or process exit.

        The caller owns that custody proof and its durable operation receipt. Missing
        or unavailable status never grants permission to issue the original write.
        """
        names = {"account_id": "OPENCLANK_HISTORY_ACCOUNT_ID", "actor_id": "OPENCLANK_HISTORY_ACTOR_ID",
                 "workspace_id": "OPENCLANK_HISTORY_WORKSPACE_ID"}
        if (self.transport != "framed" or type(capture_identity) is not dict
                or set(capture_identity) != set(names)
                or any(type(capture_identity[k]) is not str or not capture_identity[k] for k in names)
                or type(binding) is not dict):
            raise FilesServiceError("original write status unavailable", code="ordinary_write_unknown")
        try:
            frozen = json.dumps(binding, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
        except (TypeError, ValueError) as error:
            raise FilesServiceError("original write status unavailable", code="ordinary_write_unknown") from error
        original = dict(capture_identity)
        def run():
            with self._lock:
                try:
                    def matches():
                        current = self._history_environment()
                        return all(current.get(env) == original[name] for name, env in names.items())
                    self._assert_policy_generation()
                    if not matches(): raise ValueError("capture owner changed")
                    self._refresh_history_binding_locked()
                    self._start_locked()
                    if not matches(): raise ValueError("capture owner changed")
                    caps = self._exchange_locked(self._request_envelope("capabilities", path=path, payload={}))
                    if not {"ordinary_replace", "ordinary_replace_status"}.issubset(set(caps.get("operations") or ())):
                        raise ValueError("capture status unsupported")
                    if not matches(): raise ValueError("capture owner changed")
                    self._assert_policy_generation()
                    exact_binding = json.loads(frozen)
                    request = self._request_envelope("ordinary_replace_status", path=path,
                        payload={"binding": exact_binding}, root_operation_id=exact_binding["operation_id"])
                    response = self._exchange_locked(request)
                    self._assert_policy_generation()
                    if not matches(): raise ValueError("capture owner changed")
                    return response
                except Exception as error:
                    raise FilesServiceError("original write status unavailable", code="ordinary_write_unknown") from error
        return await _checkpoint_owned_thread(run)

    async def prepare_ordinary_create(self, path: str, *, binding: dict[str, Any]) -> _PreparedOrdinaryCreate:
        """Negotiate the original framed process before the Host issues an effect."""
        if self.transport != "framed" or not isinstance(binding, dict):
            raise FilesServiceError("ordinary create unavailable", code="ordinary_create_unsent")
        try:
            frozen = json.dumps(binding, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
        except (TypeError, ValueError) as error:
            raise FilesServiceError("ordinary create unavailable", code="ordinary_create_unsent") from error
        def run():
            with self._lock:
                try:
                    self._assert_policy_generation()
                    self._refresh_history_binding_locked()
                    self._start_locked()
                    process = self._process
                    history = dict(self._history_environment_snapshot or {})
                    caps = self._exchange_locked(self._request_envelope("capabilities", path=path, payload={}))
                    if (process is None or process.poll() is not None or self._process is not process
                            or self._history_environment() != history
                            or not {"ordinary_create", "ordinary_create_status"}.issubset(set(caps.get("operations") or ()))):
                        raise ValueError("unsupported or changed")
                    self._assert_policy_generation()
                    return _PreparedOrdinaryCreate(self, process, str(path), frozen, history)
                except Exception as error:
                    raise FilesServiceError("ordinary create unavailable", code="ordinary_create_unsent") from error
        return await _checkpoint_owned_thread(run)

    async def ordinary_create(self, prepared: _PreparedOrdinaryCreate, *, content: bytes) -> dict[str, Any]:
        """Consume one original process handle immediately before the actual exchange."""
        if not isinstance(prepared, _PreparedOrdinaryCreate) or prepared.client is not self:
            raise FilesServiceError("ordinary create unavailable", code="ordinary_create_unsent")
        def run():
            with self._lock:
                if prepared.used:
                    raise FilesServiceError("ordinary create already issued; inspect original status", code="ordinary_create_already_issued")
                try:
                    self._assert_policy_generation()
                    if (self._history_environment() != prepared.history or self._process is not prepared.process
                            or prepared.process.poll() is not None):
                        raise ValueError("original process changed")
                    binding = json.loads(prepared.binding_json)
                    request = self._request_envelope("ordinary_create", path=prepared.path,
                        payload={"binding": binding, "bytes": list(content)}, root_operation_id=binding["operation_id"])
                except Exception as error:
                    prepared.used = True
                    prepared.settled = True
                    raise FilesServiceError("ordinary create unavailable", code="ordinary_create_unsent") from error
                prepared.used = True
                try:
                    return self._exchange_locked(request)
                finally:
                    prepared.settled = True
        return await _checkpoint_owned_thread(run)

    async def ordinary_create_status(self, prepared: _PreparedOrdinaryCreate) -> dict[str, Any]:
        """Only read the original receipt after the owned exchange settles or process exits."""
        if (not isinstance(prepared, _PreparedOrdinaryCreate) or prepared.client is not self
                or not prepared.used or (not prepared.settled and prepared.process.poll() is None)):
            raise FilesServiceError("original create remains in flight", code="ordinary_create_unknown")
        return await self.ordinary_create_status_exact(
            prepared.path, binding=json.loads(prepared.binding_json), capture_identity=prepared.capture_identity)

    async def ordinary_create_status_exact(self, path: str, *, binding: dict[str, Any],
                                            capture_identity: dict[str, str]) -> dict[str, Any]:
        """Read only an original action after caller-proven IO settlement or process exit.

        The caller owns that custody proof and its durable operation receipt. Missing
        or unavailable status never grants permission to issue the original create.
        """
        names = {"account_id": "OPENCLANK_HISTORY_ACCOUNT_ID", "actor_id": "OPENCLANK_HISTORY_ACTOR_ID",
                 "workspace_id": "OPENCLANK_HISTORY_WORKSPACE_ID"}
        if (self.transport != "framed" or type(capture_identity) is not dict
                or set(capture_identity) != set(names)
                or any(type(capture_identity[k]) is not str or not capture_identity[k] for k in names)
                or type(binding) is not dict):
            raise FilesServiceError("original create status unavailable", code="ordinary_create_unknown")
        try:
            frozen = json.dumps(binding, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
        except (TypeError, ValueError) as error:
            raise FilesServiceError("original create status unavailable", code="ordinary_create_unknown") from error
        original = dict(capture_identity)
        def run():
            with self._lock:
                try:
                    def matches():
                        current = self._history_environment()
                        return all(current.get(env) == original[name] for name, env in names.items())
                    self._assert_policy_generation()
                    if not matches(): raise ValueError("capture owner changed")
                    self._refresh_history_binding_locked()
                    self._start_locked()
                    if not matches(): raise ValueError("capture owner changed")
                    caps = self._exchange_locked(self._request_envelope("capabilities", path=path, payload={}))
                    if not {"ordinary_create", "ordinary_create_status"}.issubset(set(caps.get("operations") or ())):
                        raise ValueError("capture status unsupported")
                    if not matches(): raise ValueError("capture owner changed")
                    self._assert_policy_generation()
                    exact_binding = json.loads(frozen)
                    request = self._request_envelope("ordinary_create_status", path=path,
                        payload={"binding": exact_binding}, root_operation_id=exact_binding["operation_id"])
                    response = self._exchange_locked(request)
                    self._assert_policy_generation()
                    if not matches(): raise ValueError("capture owner changed")
                    return response
                except Exception as error:
                    raise FilesServiceError("original create status unavailable", code="ordinary_create_unknown") from error
        return await _checkpoint_owned_thread(run)

    async def prepare_selected_checkpoint(self, *, request_id: str, root_id: str, items: list[dict[str, str]]) -> _PreparedCheckpoint:
        """Claim and stage once; returned selector stays in this exact process."""
        if self.transport != "framed" or not items:
            raise FilesServiceError("selected checkpoint unavailable", code="root_unavailable")
        request = self._request_envelope("checkpoint_prepare", path=items[0]["path"], payload={"request_id":request_id,"root_id":root_id,"items":items})
        def run():
            with self._lock:
                try:
                    self._assert_policy_generation(); self._refresh_history_binding_locked(); self._start_locked()
                    process = self._process
                    caps=self._exchange_locked(self._request_envelope("capabilities",path=items[0]["path"],payload={}))
                    if not {"checkpoint_prepare","checkpoint_finalize"}.issubset(set(caps.get("operations") or ())):
                        raise ValueError("unsupported")
                except Exception as error:
                    raise FilesServiceError("selected checkpoint unavailable",code="checkpoint_unsent") from error
                try:
                    response = self._exchange_locked(request)
                    self._assert_policy_generation()
                except Exception as error:
                    raise FilesServiceError("checkpoint outcome unavailable",code="checkpoint_unknown") from error
                data = response.get("data") or {}
                selector = data.get("selector")
                if data.get("state") != "prepared" or not isinstance(selector,str) or not re.fullmatch(r"[0-9a-f]{64}",selector) or self._process is not process:
                    raise FilesServiceError("checkpoint outcome unavailable", code="checkpoint_unknown")
                return _PreparedCheckpoint(self,process,selector)
        return await _checkpoint_owned_thread(run)

    async def finalize_selected_checkpoint(self, prepared: _PreparedCheckpoint, *, target_path: str) -> dict[str, Any]:
        if not isinstance(prepared,_PreparedCheckpoint) or prepared.client is not self or prepared.used:
            raise FilesServiceError("selected checkpoint unavailable", code="root_unavailable")
        def run():
            with self._lock:
                if prepared.used: raise FilesServiceError("selected checkpoint unavailable", code="root_unavailable")
                self._assert_policy_generation(); self._refresh_history_binding_locked()
                if self._process is not prepared.process or prepared.process is None or prepared.process.poll() is not None:
                    raise FilesServiceError("selected checkpoint unavailable", code="root_unavailable")
                prepared.used = True
                request=self._request_envelope("checkpoint_finalize",path=target_path,payload={"selector":prepared.selector})
                return self._exchange_locked(request)
        return await _checkpoint_owned_thread(run)

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

    async def stage_finish(self, stage_id: str, target: str, *, length: int, digest: str, root_operation_id: str | None = None, expected_fingerprint: str | None = None) -> dict[str, Any]:
        return await self.request("stage_finish", stage_id, {
            "stage_id": str(stage_id), "target": str(target),
            "length": int(length), "digest": str(digest),
            **({"expected_fingerprint": {"algorithm": "sha256", "value": expected_fingerprint}} if expected_fingerprint is not None else {}),
        }, root_operation_id=root_operation_id)

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
        root_operation_id: str | None = None,
    ) -> dict[str, Any]:
        if (path is None) == (handle is None):
            raise FilesServiceError("exactly one filesystem target is required", code="malformed_request")
        return {
            "protocol": {"major": 1, "minor": 0},
            "request_id": uuid.uuid4().hex,
            "session_id": "odysseus-http",
            "root_operation_id": str(root_operation_id) if root_operation_id is not None else uuid.uuid4().hex,
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
                "streaming reads require OPEN_CLANK_FILES_TRANSPORT=grpc",
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
    return (transport or os.environ.get("OPEN_CLANK_FILES_TRANSPORT") or "framed").strip().lower()


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
                old_scope = json.loads(old_key[1])
                new_scope = json.loads(key[1])
                # Files can use gRPC while legacy app reads use framed with
                # identical authority. Keeping each transport's process also
                # keeps a live directory watch from being disconnected by a
                # read in the other transport.
                if old_scope == new_scope:
                    continue
                # Explicit root-bound clients may coexist with other scopes
                # at the same generation; do not cancel unrelated in-flight
                # requests merely because one move chose a History root.
                root_bound = old_scope.get("selected_history_root_id") or new_scope.get("selected_history_root_id")
                old_generation = int(old_scope.get("generation") or 0)
                new_generation = int(new_scope.get("generation") or 0)
                same_epoch = old_generation == new_generation or (old_scope.get("host") and old_generation == 0) or (new_scope.get("host") and new_generation == 0)
                if root_bound and same_epoch:
                    continue
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

    def __init__(self, owner: str, active_workspace: str | None, *, binary: str | None = None,
                 workspace_id: str = "", chat_id: str = "", policy_generation: int | None = None,
                 registered_only: bool = False) -> None:
        super().__init__(owner, binary=binary)
        self._registered_only = registered_only
        self.workspace_id = str(workspace_id)
        self.chat_id = str(chat_id)
        self.active_workspace = str(Path(active_workspace).expanduser().resolve(strict=False)) if active_workspace else None
        self._agent_policy_generation = policy_generation

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
        try:
            scope = registry.agent_scope(self.owner, self.active_workspace,
                workspace_id=self.workspace_id, chat_id=self.chat_id, registered_only=self._registered_only)
            generation = int(scope.pop("generation"))
            if self._agent_policy_generation is not None and self._agent_policy_generation != generation:
                raise FilesServiceError("Filesystem access changed", code="policy_generation_changed")
            self._agent_policy_generation = self._policy_generation = generation
            registry_path = registry.rust_snapshot_path(
                expected_generation=self._agent_policy_generation, full_agent_roots=scope.pop("full_agent_roots", None),
            )
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
                "OPEN_CLANK_FILES_LANE": "agent",
                "OPEN_CLANK_FILES_PRINCIPAL_ID": "odysseus-agent",
                "OPEN_CLANK_FILES_REGISTRY": registry_path,
                "OPEN_CLANK_FILES_SCOPE": json.dumps(scope, separators=(",", ":"), sort_keys=True),
            }
        )
        return environment


def agent_client_for(owner: str, active_workspace: str | None, *, workspace_id: str = "", chat_id: str = "") -> AgentFilesClient:
    registry_path = os.environ.get("OPEN_CLANK_FILES_REGISTRY")
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
            client = AgentFilesClient(key[0], key[1], workspace_id=key[2], chat_id=key[3], policy_generation=generation)
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

class _WorktreeStageCustody:
    """One private Files process and original stage, never a reusable client token."""
    def __init__(self, client, process, path, history, operation_id, before_issue, profile):
        self.client,self.process,self.path=client,process,path
        self.history,self.operation_id,self.before_issue,self.profile=history,operation_id,before_issue,profile
        self.issued=False;self.uncertain=False;self.inflight=False;self.prepared=False

    @classmethod
    async def acquire(cls, client, path, operation_id, *, before_issue, profile='stage'):
        if (profile not in ('stage','inverse','redo','lookup') or client.transport!='framed' or type(path) is not str or not path.startswith('/')
                or type(operation_id) is not str or not operation_id or not callable(before_issue)):
            raise FilesServiceError('worktree stage unavailable',code='worktree_unsent')
        def run():
            with client._lock:
                try:
                    client._assert_policy_generation();client._refresh_history_binding_locked();client._start_locked()
                    process=client._process;history=dict(client._history_environment_snapshot or {})
                    if process is None or process.poll() is not None or client._history_environment()!=history: raise ValueError('original process changed')
                    client._assert_policy_generation();return cls(client,process,path,history,operation_id,before_issue,profile)
                except Exception as error: raise FilesServiceError('worktree stage unavailable',code='worktree_unsent') from error
        return await _checkpoint_owned_thread(run)

    async def exchange(self, operation, payload, *, target=None):
        allowed={'worktree_stage_begin','worktree_stage_chunk','worktree_stage_end','worktree_stage_seal','worktree_stage_status','worktree_private_prepare','worktree_private_accept','worktree_private_materialize','worktree_private_prepare_history','worktree_private_begin_publication','worktree_private_publish','worktree_private_check_published','worktree_private_finish_publication'}
        if self.profile=='inverse':
            allowed={'worktree_inverse_begin','worktree_inverse_prepare_history','worktree_inverse_stage_history','worktree_inverse_verify','worktree_inverse_quarantine','worktree_inverse_relocate_admin','worktree_inverse_finish','worktree_stage_status'}
        if self.profile=='redo':
            allowed={'worktree_redo_begin','worktree_redo_prepare_history','worktree_redo_restore_admin','worktree_redo_restore_tree','worktree_redo_finish','worktree_stage_status'}
        if self.profile=='lookup':
            allowed={'worktree_lookup'}
        else:
            allowed.add('worktree_release_completed')
        begin='worktree_lookup' if self.profile=='lookup' else 'worktree_redo_begin' if self.profile=='redo' else 'worktree_inverse_begin' if self.profile=='inverse' else 'worktree_stage_begin'
        if operation not in allowed or type(payload) is not dict or payload.get('operation_id')!=self.operation_id:
            raise FilesServiceError('worktree stage unavailable',code='worktree_unsent')
        try: frozen=json.dumps(payload,sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False)
        except (TypeError,ValueError) as error: raise FilesServiceError('worktree stage unavailable',code='worktree_unsent') from error
        if target is not None and (operation!='worktree_private_prepare' or type(target) is not str or not target.startswith('/') or len(target.encode('utf-8','strict'))>4096):
            raise FilesServiceError('worktree target unavailable',code='worktree_unsent')
        status=operation=='worktree_stage_status'
        if self.inflight or (status and not self.issued) or (not status and self.uncertain):
            raise FilesServiceError('inspect original worktree stage',code='worktree_unknown')
        if not status and ((operation==begin)==self.prepared):
            raise FilesServiceError('worktree phase unavailable',code='worktree_unsent')
        self.inflight=True
        if not status: self.issued=True;self.uncertain=True
        def run():
            with self.client._lock:
                try:
                    self.client._assert_policy_generation()
                    if (self.client._process is not self.process or self.process.poll() is not None
                            or self.client._history_environment()!=self.history): raise ValueError('original process changed')
                    if self.before_issue() is not True: raise ValueError('worktree authority changed')
                    request=self.client._request_envelope(operation,path=target or self.path,payload=json.loads(frozen),root_operation_id=self.operation_id)
                    if len(json.dumps(request,separators=(',',':'),ensure_ascii=False,allow_nan=False).encode('utf-8'))>4*1024*1024:
                        raise ValueError('worktree frame bound')
                    return self.client._exchange_locked(request)
                except Exception as error: raise FilesServiceError('original worktree stage outcome unavailable',code='worktree_unknown') from error
        try:
            result=await _checkpoint_owned_thread(run)
            if not status:
                data=result.get('data') if type(result) is dict else None
                if type(data) is dict and data.get('state') in ('accepted','sealed','ready','proceed','registered','materialized','history_prepared','verified','quarantined','admin_relocated','admin_restored','tree_restored','published','completed','released'):
                    self.uncertain=False
                    if operation==begin:self.prepared=True
            return result
        finally: self.inflight=False
