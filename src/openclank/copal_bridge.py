"""Owned stdio bridge to Copal's Redb rollback backend.

The application selects the loose-file bridge by default. This adapter remains
available for explicit ``COPAL_STORAGE=redb`` rollback and migration checks.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import os
import platform
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

import httpx

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows uses msvcrt below.
    fcntl = None

try:
    import msvcrt
except ImportError:  # pragma: no cover - POSIX uses fcntl.
    msvcrt = None


logger = logging.getLogger(__name__)
_ROOT = Path(__file__).resolve().parents[2]
_CRATE = _ROOT / "packages" / "Copal" / "rust" / "copal-db"
_DEFAULT_COMMAND = _CRATE / "target" / "release" / "copal-bridge"
_DEFAULT_DATA = _ROOT / "packages" / "Copal" / "db"
_BRIDGE_STREAM_LIMIT = 32 * 1024 * 1024
_ARTIFACT_METADATA_SUFFIX = ".copal-build.json"
_ARTIFACT_METADATA_VERSION = 1
_BUILD_ID_VERSION = 1
_PROTOCOL_VERSION = 1
_STORAGE_SCHEMA_VERSION = 3
_REQUIRED_CAPABILITIES = frozenset({
    "scoped-storage", "native-notes", "task-index", "guarded-commit", "wiki-seeds",
})
_BUILD_LOCK = threading.Lock()


def _artifact_metadata_path(command: str | Path) -> Path:
    path = Path(command)
    return path.with_name(path.name + _ARTIFACT_METADATA_SUFFIX)


def _artifact_lock_path(command: str | Path) -> Path:
    path = Path(command)
    return path.with_name(path.name + ".build.lock")


def _artifact_sha256(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return f"sha256:{digest.hexdigest()}"


def _build_target() -> str:
    return os.environ.get("CARGO_BUILD_TARGET", "").strip() or platform.machine()


def _acquire_process_lock(path: str | Path):
    lock = Path(path)
    lock.parent.mkdir(parents=True, exist_ok=True)
    handle = lock.open("a+")
    if fcntl is not None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
    elif msvcrt is not None:
        handle.seek(0)
        handle.write("0")
        handle.flush()
        handle.seek(0)
        while True:
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                break
            except OSError:
                time.sleep(0.05)
    return handle


def _release_process_lock(handle) -> None:
    if fcntl is not None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    elif msvcrt is not None:
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    handle.close()


def _toolchain_identity() -> dict[str, str]:
    values = {"platform": platform.platform(), "machine": platform.machine()}
    for name in ("rustc", "cargo"):
        try:
            values[name] = subprocess.run([name, "-Vv" if name == "rustc" else "-V"], check=True, capture_output=True, text=True, timeout=10).stdout.strip()
        except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
            values[name] = "unavailable"
    return values


def copal_build_identity(crate: str | Path = _CRATE, *, target: str | None = None, toolchain: dict[str, str] | None = None) -> str:
    """Hash only inputs that can change the Copal executable's behavior."""
    root = Path(crate).resolve()
    files = [root / "Cargo.toml", root / "Cargo.lock"]
    files.extend(sorted(path for path in (root / "src").rglob("*") if path.is_file()))
    contract = root.parents[3] / ".clankers" / "hexes" / "contract.yaml"
    if contract.is_file():
        files.append(contract)
    digest = hashlib.sha256()
    for path in sorted(set(files)):
        if not path.is_file():
            continue
        relative = path.relative_to(root.parents[3]) if path.is_relative_to(root.parents[3]) else path.relative_to(root)
        digest.update(str(relative).replace(os.sep, "/").encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    metadata = {"version": _BUILD_ID_VERSION, "target": target or _build_target(), "toolchain": toolchain or _toolchain_identity()}
    digest.update(json.dumps(metadata, sort_keys=True, separators=(",", ":")).encode())
    return f"sha256:{digest.hexdigest()}"


def _read_artifact_metadata(command: str | Path) -> dict[str, Any]:
    path = _artifact_metadata_path(command)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise CopalBridgeError(f"Copal artifact metadata is missing or invalid: {path}") from exc
    if not isinstance(value, dict):
        raise CopalBridgeError(f"Copal artifact metadata is not an object: {path}")
    return value


def _validate_artifact_metadata(command: str | Path, expected_identity: str | None = None) -> dict[str, Any]:
    path = Path(command)
    if not path.is_file() or not os.access(path, os.X_OK):
        raise CopalBridgeError(f"Copal artifact is missing or not executable: {path}")
    metadata = _read_artifact_metadata(path)
    if metadata.get("schema_version") != _ARTIFACT_METADATA_VERSION:
        raise CopalBridgeError(f"Copal artifact metadata schema mismatch: expected {_ARTIFACT_METADATA_VERSION}, found {metadata.get('schema_version')}")
    build_identity = metadata.get("build_identity")
    if not isinstance(build_identity, str) or not build_identity:
        raise CopalBridgeError(f"Copal artifact source identity is missing: {path}")
    artifact_sha256 = metadata.get("artifact_sha256")
    actual_sha256 = _artifact_sha256(path)
    if artifact_sha256 != actual_sha256:
        raise CopalBridgeError(f"Copal artifact bytes do not match its packaging digest: expected {artifact_sha256}, found {actual_sha256}")
    if expected_identity is not None and metadata.get("build_identity") != expected_identity:
        raise CopalBridgeError(f"Copal artifact source identity mismatch: expected {expected_identity}, found {metadata.get('build_identity')}")
    if metadata.get("protocol_version") != _PROTOCOL_VERSION:
        raise CopalBridgeError(f"Copal artifact protocol mismatch: expected {_PROTOCOL_VERSION}, found {metadata.get('protocol_version')}")
    if metadata.get("storage_schema_version") != _STORAGE_SCHEMA_VERSION:
        raise CopalBridgeError(f"Copal artifact storage schema mismatch: expected {_STORAGE_SCHEMA_VERSION}, found {metadata.get('storage_schema_version')}")
    capabilities = metadata.get("capabilities")
    if not isinstance(capabilities, list) or not all(isinstance(item, str) for item in capabilities) or not _REQUIRED_CAPABILITIES.issubset(set(capabilities)):
        raise CopalBridgeError(f"Copal artifact capabilities are incomplete: required {sorted(_REQUIRED_CAPABILITIES)}, found {capabilities}")
    return metadata


def _probe_artifact(command: str | Path, data_dir: str | Path | None = None, expected_identity: str | None = None) -> dict[str, Any]:
    """Verify the executable's protocol against a disposable store."""
    if data_dir is None:
        with tempfile.TemporaryDirectory(prefix="copal-artifact-probe-") as temporary:
            return _probe_artifact(command, temporary, expected_identity)
    root = Path(data_dir)
    root.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env["COPAL_DATA_DIR"] = str(root / "notes")
    env["COPAL_WIKI_DATA_DIR"] = str(root / "wiki")
    request = json.dumps({"id": 1, "op": "status", "args": {}}, separators=(",", ":")) + "\n"
    try:
        result = subprocess.run([str(command)], input=request, capture_output=True, text=True, env=env, timeout=20, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise CopalBridgeError(f"Copal artifact protocol probe failed: {command}") from exc
    if result.returncode != 0:
        raise CopalBridgeError(f"Copal artifact exited during protocol probe ({result.returncode}): {result.stderr[-500:]}")
    try:
        response = json.loads(result.stdout.splitlines()[0])
        status = response["result"] if response.get("ok") else None
    except (IndexError, KeyError, TypeError, ValueError) as exc:
        raise CopalBridgeError(f"Copal artifact returned an invalid protocol response: {command}") from exc
    if response.get("id") != 1 or not isinstance(status, dict):
        raise CopalBridgeError(f"Copal artifact protocol handshake was not acknowledged: {command}")
    protocol = status.get("protocol_version")
    if protocol != _PROTOCOL_VERSION:
        raise CopalBridgeError(f"Copal artifact protocol mismatch: expected {_PROTOCOL_VERSION}, found {protocol}")
    schema = status.get("schema_version")
    if schema != _STORAGE_SCHEMA_VERSION:
        raise CopalBridgeError(f"Copal artifact storage schema mismatch: expected {_STORAGE_SCHEMA_VERSION}, found {schema}")
    capabilities = status.get("capabilities")
    if not isinstance(capabilities, list) or not all(isinstance(item, str) for item in capabilities) or not _REQUIRED_CAPABILITIES.issubset(set(capabilities)):
        raise CopalBridgeError(f"Copal artifact capabilities are incomplete: required {sorted(_REQUIRED_CAPABILITIES)}, found {capabilities}")
    if expected_identity is not None and status.get("source_identity") != expected_identity:
        raise CopalBridgeError(f"Copal artifact running source identity mismatch: expected {expected_identity}, found {status.get('source_identity')}")
    return status


def verify_copal_artifact(command: str | Path, *, expected_identity: str | None = None, data_dir: str | Path | None = None) -> dict[str, Any]:
    metadata = _validate_artifact_metadata(command, expected_identity)
    status = _probe_artifact(command, data_dir, metadata["build_identity"])
    return {"metadata": metadata, "status": status}


class CopalBridgeError(RuntimeError):
    pass


class CopalBridge:
    supports_task_index_lookup = True
    supports_keyed_task_index = True

    def __init__(self, command: str | Path | None = None, data_dir: str | Path | None = None):
        configured_command = command if command is not None else os.environ.get("COPAL_BRIDGE_COMMAND")
        if configured_command == "":
            configured_command = None
        self._custom_command = configured_command is not None
        self.command = Path(configured_command or _DEFAULT_COMMAND).expanduser()
        self.data_dir = Path(data_dir or os.environ.get("COPAL_DATA_DIR", _DEFAULT_DATA)).expanduser()
        self._broker_url = os.environ.get("OPEN_CLANK_COPAL_BROKER_URL", "").strip()
        self._broker_token = os.environ.get("OPEN_CLANK_COPAL_BROKER_TOKEN", "").strip()
        self._broker_workspace = os.environ.get("OPEN_CLANK_COPAL_BROKER_WORKSPACE", "").strip()
        self._broker_started = False
        self._process: asyncio.subprocess.Process | None = None
        self._lock = asyncio.Lock()
        self._start_lock = asyncio.Lock()
        self._request_id = 0
        self._stderr_task: asyncio.Task | None = None
        self._artifact_metadata: dict[str, Any] | None = None

    @property
    def pid(self) -> int | None:
        return self._process.pid if self.is_alive() else None

    def is_alive(self) -> bool:
        if self._broker_url:
            return self._broker_started
        return self._process is not None and self._process.returncode is None

    def _assert_running_artifact(self) -> None:
        metadata = self._artifact_metadata
        if metadata is None:
            return
        expected = metadata.get("artifact_sha256")
        try:
            actual = _artifact_sha256(self.command)
        except OSError as exc:
            raise CopalBridgeError(f"Running Copal artifact cannot be read: {self.command}") from exc
        if actual != expected:
            raise CopalBridgeError(f"Running Copal artifact changed after verification: expected {expected}, found {actual}")

    async def _build(self) -> None:
        if self._custom_command:
            self._artifact_metadata = verify_copal_artifact(self.command, expected_identity=None)["metadata"]
            return
        expected = copal_build_identity()
        try:
            self._artifact_metadata = verify_copal_artifact(self.command, expected_identity=expected)["metadata"]
            return
        except CopalBridgeError:
            pass

        await asyncio.to_thread(_BUILD_LOCK.acquire)
        process_lock = None
        try:
            process_lock = await asyncio.to_thread(_acquire_process_lock, _artifact_lock_path(self.command))
            # Another bridge instance may have completed the checkout build
            # while this instance waited for the shared build lock.
            try:
                self._artifact_metadata = verify_copal_artifact(self.command, expected_identity=expected)["metadata"]
                return
            except CopalBridgeError:
                pass

            staging_root = Path(tempfile.mkdtemp(prefix="copal-build-"))
            try:
                target_dir = staging_root / "target"
                env = os.environ.copy()
                env["CARGO_TARGET_DIR"] = str(target_dir)
                env["COPAL_SOURCE_IDENTITY"] = expected
                result = subprocess.run(
                    ["cargo", "build", "--release", "--bin", "copal-bridge"],
                    cwd=_CRATE,
                    env=env,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    timeout=300,
                    check=False,
                )
                if result.returncode != 0:
                    raise CopalBridgeError(f"Copal bridge build failed: {result.stdout[-2000:]}")
                candidate = target_dir / "release" / "copal-bridge"
                if not candidate.is_file():
                    raise CopalBridgeError(f"Copal bridge candidate missing after build: {candidate}")
                metadata_path = _artifact_metadata_path(candidate)
                metadata_path.write_text(json.dumps({
                    "schema_version": _ARTIFACT_METADATA_VERSION,
                    "build_identity": expected,
                    "protocol_version": _PROTOCOL_VERSION,
                    "storage_schema_version": _STORAGE_SCHEMA_VERSION,
                    "capabilities": sorted(_REQUIRED_CAPABILITIES),
                    "target": _build_target(),
                    "artifact_sha256": _artifact_sha256(candidate),
                }, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")
                self._artifact_metadata = verify_copal_artifact(candidate, expected_identity=expected, data_dir=staging_root / "probe")["metadata"]
                self.command.parent.mkdir(parents=True, exist_ok=True)
                prior = self.command.with_name(self.command.name + ".previous")
                if self.command.is_file():
                    shutil.copy2(self.command, prior)
                    prior_metadata = _artifact_metadata_path(self.command)
                    if prior_metadata.is_file():
                        shutil.copy2(prior_metadata, _artifact_metadata_path(prior))
                os.replace(candidate, self.command)
                os.replace(metadata_path, _artifact_metadata_path(self.command))
            finally:
                try:
                    shutil.rmtree(staging_root)
                except FileNotFoundError:
                    pass
        finally:
            if process_lock is not None:
                await asyncio.to_thread(_release_process_lock, process_lock)
            _BUILD_LOCK.release()

    async def start(self) -> None:
        async with self._start_lock:
            if self._broker_url:
                if self._broker_started:
                    return
                if not self._broker_token or not self._broker_workspace:
                    raise CopalBridgeError("Copal broker configuration is incomplete")
                await self.call("status", timeout=10, _ensure_started=False)
                self._broker_started = True
                logger.info("Copal HTTP broker connected workspace=%s", self._broker_workspace)
                return
            if self.is_alive():
                return
            await self._build()
            self.data_dir.mkdir(parents=True, exist_ok=True)
            env = os.environ.copy()
            env["COPAL_DATA_DIR"] = str(self.data_dir)
            # Wiki store lives beside the notes store in the same data dir.
            env["COPAL_WIKI_DATA_DIR"] = str(self.data_dir)
            self._process = await asyncio.create_subprocess_exec(
                str(self.command),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
                limit=_BRIDGE_STREAM_LIMIT,
            )
            self._stderr_task = asyncio.create_task(self._drain_stderr())
            try:
                self._assert_running_artifact()
            except CopalBridgeError:
                await self._retire_process(self._process, graceful=False)
                raise
            await self.call("status", timeout=10, _ensure_started=False)
            logger.info("Copal Redb bridge started pid=%s", self.pid)

    async def _drain_stderr(self) -> None:
        process = self._process
        if not process or not process.stderr:
            return
        while line := await process.stderr.readline():
            logger.debug("[copal-bridge] %s", line.decode(errors="replace").rstrip())

    async def call(
        self,
        operation: str,
        args: dict[str, Any] | None = None,
        *,
        timeout: float = 20,
        _ensure_started: bool = True,
    ) -> Any:
        if self._broker_url:
            if _ensure_started and not self._broker_started:
                await self.start()
            try:
                async with httpx.AsyncClient() as client:
                    response = await client.post(
                        self._broker_url,
                        json={"operation": operation, "args": args or {}},
                        headers={
                            "X-Open-Clank-Copal-Token": self._broker_token,
                            "X-Open-Clank-Owner": os.environ.get("OWNER", "").strip(),
                            "X-Open-Clank-Copal-Workspace": self._broker_workspace,
                        },
                        timeout=timeout,
                    )
                if response.status_code >= 400:
                    raise CopalBridgeError(
                        f"Copal broker returned HTTP {response.status_code}"
                    )
                payload = response.json()
                if not payload.get("ok"):
                    raise CopalBridgeError(str(payload.get("error") or "Copal operation failed"))
                result = payload.get("result")
                if operation in {"status", "scoped_status"} and isinstance(result, dict):
                    result = dict(result)
                    result.update({
                        "build_identity": "unavailable",
                        "artifact_sha256": "unavailable",
                        "artifact_source": "broker",
                    })
                return result
            except (httpx.HTTPError, ValueError) as exc:
                raise CopalBridgeError("Copal broker request failed") from exc
        while True:
            if _ensure_started:
                await self.start()
            async with self._lock:
                process = self._process
                if not process or not process.stdin or not process.stdout or process.returncode is not None:
                    if _ensure_started:
                        continue
                    raise CopalBridgeError("Copal bridge is not running")
                self._request_id += 1
                request_id = self._request_id
                payload = json.dumps(
                    {"id": request_id, "op": operation, "args": args or {}},
                    separators=(",", ":"),
                ).encode() + b"\n"
                try:
                    process.stdin.write(payload)
                    await process.stdin.drain()
                    line = await asyncio.wait_for(process.stdout.readline(), timeout=timeout)
                    if not line:
                        raise CopalBridgeError("Copal bridge closed its output")
                    try:
                        response = json.loads(line)
                    except json.JSONDecodeError as exc:
                        raise CopalBridgeError("Copal bridge returned invalid JSON") from exc
                    if response.get("id") != request_id:
                        raise CopalBridgeError("Copal bridge response was out of sequence")
                except BaseException:
                    # Once a request may have reached the child, a timeout or caller
                    # cancellation leaves its eventual line ambiguous. Retire that
                    # process before another call so responses can never be paired
                    # with the wrong request. Redb rolls back an interrupted write;
                    # a just-committed import is safe to retry because it is idempotent.
                    await self._retire_process(process, graceful=False)
                    raise
                if not response.get("ok"):
                    raise CopalBridgeError(str(response.get("error") or "Copal operation failed"))
                result = response.get("result")
                if operation in {"status", "scoped_status"} and isinstance(result, dict):
                    try:
                        self._assert_running_artifact()
                        if self._artifact_metadata is not None and result.get("source_identity") != self._artifact_metadata.get("build_identity"):
                            raise CopalBridgeError(
                                "Running Copal source identity does not match the selected artifact"
                            )
                    except BaseException:
                        await self._retire_process(process, graceful=False)
                        raise
                    result = dict(result)
                    if self._artifact_metadata is None:
                        result.update({
                            "build_identity": "unavailable",
                            "artifact_sha256": "unavailable",
                            "artifact_source": "unverified",
                        })
                    else:
                        result.update({
                            "build_identity": self._artifact_metadata.get("build_identity"),
                            "artifact_sha256": self._artifact_metadata.get("artifact_sha256"),
                            "artifact_source": "verified-sidecar",
                        })
                return result

    async def _retire_process(
        self,
        process: asyncio.subprocess.Process,
        *,
        graceful: bool,
    ) -> None:
        if self._process is process:
            self._process = None
        if process and process.returncode is None:
            if graceful and process.stdin:
                with contextlib.suppress(BrokenPipeError, ConnectionResetError):
                    process.stdin.close()
            elif not graceful:
                with contextlib.suppress(ProcessLookupError):
                    process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=3)
            except asyncio.TimeoutError:
                with contextlib.suppress(ProcessLookupError):
                    process.kill()
                await process.wait()
        if self._stderr_task:
            self._stderr_task.cancel()
            await asyncio.gather(self._stderr_task, return_exceptions=True)
            self._stderr_task = None

    async def stop(self) -> None:
        if self._broker_url:
            self._broker_started = False
            logger.info("Copal HTTP broker disconnected")
            return
        process = self._process
        if process:
            await self._retire_process(process, graceful=True)
        logger.info("Copal Redb bridge stopped")
