"""Owner-local profile credentials and Unix-socket client/server for Hex policy.

The CLI is deliberately a socket client. Only the independently running worker
opens the policy store or invokes the contained first-party Hexes evaluator.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import socket
import stat
import struct
import subprocess
import time
import uuid
from pathlib import Path
from threading import Event
from typing import Any, Iterable, Mapping, Optional


PROFILE_VERSION = 1
PROFILE_TTL_SECONDS = 15 * 60
MAX_PROFILE_TTL_SECONDS = 60 * 60
MAX_REQUEST_BYTES = 64 * 1024
MAX_RESPONSE_BYTES = 1024 * 1024
MAX_STAGED_FILE_BYTES = 64 * 1024 * 1024
POLICY_USER_UNIT = "open-clank-policy-worker.service"
POLICY_ACTIONS = frozenset({"select", "check", "pre-commit", "pre-push"})
_PROFILE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}\Z")


class PolicyLocalError(RuntimeError):
    pass


def default_policy_dir() -> Path:
    configured = os.environ.get("OPEN_CLANK_POLICY_DIR")
    if configured:
        return Path(configured).expanduser().resolve(strict=False)
    state_home = os.environ.get("XDG_STATE_HOME")
    base = Path(state_home).expanduser() if state_home else Path.home() / ".local" / "state"
    return (base / "open-clank" / "policy").resolve(strict=False)


def default_socket_path() -> Path:
    configured = os.environ.get("OPEN_CLANK_POLICY_SOCKET")
    if configured:
        return Path(configured).expanduser().resolve(strict=False)
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    if runtime:
        return (Path(runtime) / "open-clank" / "policy.sock").resolve(strict=False)
    return default_policy_dir() / "policy.sock"


def _private_directory(path: Path) -> Path:
    path.mkdir(parents=True, mode=0o700, exist_ok=True)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        raise PolicyLocalError(f"policy directory is not owned by uid {os.getuid()}")
    if stat.S_IMODE(info.st_mode) & 0o077:
        os.chmod(path, 0o700)
        info = path.lstat()
        if stat.S_IMODE(info.st_mode) & 0o077:
            raise PolicyLocalError("policy directory permissions are not private")
    return path


def _secure_read(path: Path, *, limit: int) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise PolicyLocalError(f"cannot read private policy file: {path.name}") from exc
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            raise PolicyLocalError(f"private policy file is not owned by uid {os.getuid()}")
        if stat.S_IMODE(info.st_mode) & 0o077:
            raise PolicyLocalError("private policy file permissions must be 0600")
        if info.st_size > limit:
            raise PolicyLocalError("private policy file is too large")
        payload = bytearray()
        while len(payload) <= limit:
            chunk = os.read(descriptor, min(16 * 1024, limit + 1 - len(payload)))
            if not chunk:
                break
            payload.extend(chunk)
        if len(payload) > limit:
            raise PolicyLocalError("private policy file is too large")
        return bytes(payload)
    finally:
        os.close(descriptor)


def _atomic_private_write(path: Path, payload: bytes) -> None:
    parent = _private_directory(path.parent)
    temporary = parent / f".{path.name}.{uuid.uuid4().hex}.tmp"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(temporary, flags, 0o600)
    try:
        offset = 0
        while offset < len(payload):
            offset += os.write(descriptor, payload[offset:])
        os.fchmod(descriptor, 0o600)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    try:
        os.replace(temporary, path)
        os.chmod(path, 0o600, follow_symlinks=False)
        directory_fd = os.open(parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _signing_key(state_dir: Path, *, create: bool) -> bytes:
    state_dir = _private_directory(state_dir)
    path = state_dir / "profile-signing.key"
    if create and not path.exists():
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(path, flags, 0o600)
        except FileExistsError:
            pass
        else:
            try:
                payload = secrets.token_bytes(32)
                os.write(descriptor, payload)
                os.fchmod(descriptor, 0o600)
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
    key = _secure_read(path, limit=64)
    if len(key) != 32:
        raise PolicyLocalError("policy profile signing key is invalid")
    return key


def _profile_path(state_dir: Path, profile_name: str) -> Path:
    if not _PROFILE_NAME.fullmatch(str(profile_name or "")):
        raise PolicyLocalError("profile name must use letters, numbers, dot, dash, or underscore")
    return _private_directory(state_dir / "profiles") / f"{profile_name}.json"


def _canonical_json(value: Mapping[str, Any]) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def issue_local_profile(
    profile_name: str,
    *,
    owner: str,
    project_id: str,
    db_path: str,
    state_dir: str | os.PathLike[str] | None = None,
    actions: Iterable[str] = POLICY_ACTIONS,
    os_uid: Optional[int] = None,
    ttl_seconds: int = PROFILE_TTL_SECONDS,
    now: Optional[float] = None,
) -> dict[str, Any]:
    """Issue one install-signed, short-lived credential after exact activation."""
    from src.project_hex import HexResolutionError, inspect_project_policy, require_hex_activation

    owner = str(owner or "").strip()
    project_id = str(project_id or "").strip()
    if not owner or not project_id:
        raise PolicyLocalError("owner and project are required")
    ttl_seconds = int(ttl_seconds)
    if ttl_seconds <= 0 or ttl_seconds > MAX_PROFILE_TTL_SECONDS:
        raise PolicyLocalError("profile lifetime must be between 1 and 3600 seconds")
    requested_actions = sorted({str(action) for action in actions})
    if not requested_actions or not set(requested_actions).issubset(POLICY_ACTIONS):
        raise PolicyLocalError("profile contains an unsupported policy action")
    try:
        inspected = inspect_project_policy(project_id, owner=owner, db_path=db_path)
        project = inspected["project"]
        projection = inspected.get("projection") or {}
        if projection.get("state") != "active":
            raise PolicyLocalError("project policy is not active")
        require_hex_activation(
            project["canonical_root"],
            owner=owner,
            project_id=project_id,
            db_path=db_path,
            workspace_root=project["canonical_root"],
        )
    except (HexResolutionError, KeyError) as exc:
        raise PolicyLocalError(str(exc)) from exc
    issued_at = int(time.time() if now is None else now)
    credential = {
        "version": PROFILE_VERSION,
        "profile": profile_name,
        "owner": owner,
        "project_id": project_id,
        "project_root": str(Path(project["canonical_root"]).resolve(strict=True)),
        "activation_revision": int(projection["activation_revision"]),
        "actions": requested_actions,
        "os_uid": int(os.getuid() if os_uid is None else os_uid),
        "issued_at": issued_at,
        "expires_at": issued_at + ttl_seconds,
    }
    root = Path(state_dir) if state_dir is not None else default_policy_dir()
    signature = hmac.new(_signing_key(root, create=True), _canonical_json(credential), hashlib.sha256).hexdigest()
    path = _profile_path(root, profile_name)
    _atomic_private_write(path, _canonical_json({"credential": credential, "signature": signature}) + b"\n")
    return {**credential, "path": str(path)}


def _profile_candidates(state_dir: Path, profile_name: Optional[str]) -> list[Path]:
    if profile_name:
        return [_profile_path(state_dir, profile_name)]
    directory = _private_directory(state_dir / "profiles")
    return sorted(path for path in directory.glob("*.json") if path.is_file())


def _verify_profile_file(
    path: Path,
    *,
    state_dir: Path,
    peer_uid: int,
    action: str,
    project_id: str,
    project_root: Path,
    db_path: str,
    now: Optional[float] = None,
) -> dict[str, Any]:
    from src.project_hex import HexResolutionError, inspect_project_policy, require_hex_activation

    try:
        envelope = json.loads(_secure_read(path, limit=32 * 1024))
        credential = envelope["credential"]
        signature = str(envelope["signature"])
    except (KeyError, TypeError, ValueError) as exc:
        raise PolicyLocalError("policy profile is malformed") from exc
    if not isinstance(credential, dict):
        raise PolicyLocalError("policy profile is malformed")
    expected = hmac.new(
        _signing_key(state_dir, create=False),
        _canonical_json(credential),
        hashlib.sha256,
    ).hexdigest()
    if not hmac.compare_digest(signature, expected):
        raise PolicyLocalError("policy profile signature is invalid")
    try:
        if int(credential["version"]) != PROFILE_VERSION:
            raise PolicyLocalError("policy profile version is unsupported")
        if str(credential["profile"]) != path.stem:
            raise PolicyLocalError("policy profile name does not match its credential")
        if int(credential["os_uid"]) != int(peer_uid):
            raise PolicyLocalError("policy profile belongs to a different OS user")
        if str(credential["project_id"]) != project_id:
            raise PolicyLocalError("policy profile belongs to a different project")
        if Path(str(credential["project_root"])).resolve(strict=True) != project_root:
            raise PolicyLocalError("policy profile belongs to a different project root")
        if action not in set(credential["actions"]):
            raise PolicyLocalError("policy profile does not allow this action")
        current_time = int(time.time() if now is None else now)
        issued_at = int(credential["issued_at"])
        expires_at = int(credential["expires_at"])
        if issued_at > current_time + 30 or expires_at <= current_time:
            raise PolicyLocalError("policy profile has expired or is not yet valid")
        if expires_at - issued_at > MAX_PROFILE_TTL_SECONDS:
            raise PolicyLocalError("policy profile lifetime exceeds the local limit")
        owner = str(credential["owner"])
        inspected = inspect_project_policy(project_id, owner=owner, db_path=db_path)
        project = inspected["project"]
        projection = inspected.get("projection") or {}
        if projection.get("state") != "active":
            raise PolicyLocalError("project policy is not active")
        if int(projection["activation_revision"]) != int(credential["activation_revision"]):
            raise PolicyLocalError("policy profile activation revision is stale")
        if Path(project["canonical_root"]).resolve(strict=True) != project_root:
            raise PolicyLocalError("registered project root changed")
        require_hex_activation(
            project_root,
            owner=owner,
            project_id=project_id,
            db_path=db_path,
            workspace_root=project_root,
        )
    except (HexResolutionError, KeyError, OSError, TypeError, ValueError) as exc:
        if isinstance(exc, PolicyLocalError):
            raise
        raise PolicyLocalError(str(exc)) from exc
    return credential


def _peer_uid(connection: socket.socket) -> int:
    if hasattr(socket, "SO_PEERCRED"):
        raw = connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
        return int(struct.unpack("3i", raw)[1])
    getter = getattr(connection, "getpeereid", None)
    if getter is not None:
        return int(getter()[0])
    raise PolicyLocalError("Unix peer credentials are unavailable")


def _receive_json(connection: socket.socket, *, limit: int) -> dict[str, Any]:
    payload = bytearray()
    while len(payload) <= limit:
        chunk = connection.recv(min(16 * 1024, limit + 1 - len(payload)))
        if not chunk:
            break
        payload.extend(chunk)
    if not payload or len(payload) > limit:
        raise PolicyLocalError("policy request is empty or too large")
    try:
        value = json.loads(payload)
    except (TypeError, ValueError) as exc:
        raise PolicyLocalError("policy request is malformed") from exc
    if not isinstance(value, dict):
        raise PolicyLocalError("policy request must be an object")
    return value


def _staged_candidates(project_root: str | os.PathLike[str]) -> dict[str, Optional[bytes]]:
    """Read exact index bytes without checking out or executing project code."""
    root = Path(project_root).resolve(strict=True)
    changed = subprocess.run(
        ["git", "-C", str(root), "diff", "--cached", "--name-status", "-z", "--find-renames"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=20,
        check=False,
    )
    if changed.returncode:
        raise PolicyLocalError("could not inspect the staged project snapshot")
    parts = [part.decode("utf-8", errors="surrogateescape") for part in changed.stdout.split(b"\0") if part]
    candidates: dict[str, Optional[bytes]] = {}
    offset = 0
    while offset < len(parts):
        status_text = parts[offset]
        offset += 1
        status_code = status_text[:1]
        path_count = 2 if status_code in {"R", "C"} else 1
        if offset + path_count > len(parts):
            raise PolicyLocalError("staged project snapshot is malformed")
        paths = parts[offset : offset + path_count]
        offset += path_count
        if status_code == "R":
            candidates[paths[0]] = None
        target = paths[-1]
        if status_code == "D":
            candidates[target] = None
            continue
        size = subprocess.run(
            ["git", "-C", str(root), "cat-file", "-s", f":{target}"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=10,
            check=False,
        )
        try:
            byte_count = int(size.stdout.strip()) if size.returncode == 0 else -1
        except ValueError:
            byte_count = -1
        if byte_count < 0 or byte_count > MAX_STAGED_FILE_BYTES:
            raise PolicyLocalError("staged project file is unavailable or exceeds 64 MiB")
        content = subprocess.run(
            ["git", "-C", str(root), "show", f":{target}"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=20,
            check=False,
        )
        if content.returncode or len(content.stdout) != byte_count:
            raise PolicyLocalError("staged project file changed while it was inspected")
        candidates[target] = content.stdout
    return candidates


class PolicyWorker:
    def __init__(
        self,
        *,
        db_path: str,
        state_dir: str | os.PathLike[str] | None = None,
        socket_path: str | os.PathLike[str] | None = None,
    ):
        self.db_path = str(Path(db_path).expanduser().resolve(strict=False))
        self.state_dir = Path(state_dir) if state_dir is not None else default_policy_dir()
        self.socket_path = Path(socket_path) if socket_path is not None else default_socket_path()

    def _credential(self, request: Mapping[str, Any], peer_uid: int) -> dict[str, Any]:
        action = str(request.get("action") or "")
        project_id = str(request.get("project_id") or "").strip()
        raw_root = str(request.get("project_root") or "").strip()
        if action not in POLICY_ACTIONS or not project_id or not raw_root:
            raise PolicyLocalError("action, project, and project root are required")
        project_root = Path(raw_root).expanduser().resolve(strict=True)
        explicit = str(request.get("profile") or "").strip() or None
        valid: list[dict[str, Any]] = []
        errors: list[PolicyLocalError] = []
        for path in _profile_candidates(self.state_dir, explicit):
            try:
                valid.append(
                    _verify_profile_file(
                        path,
                        state_dir=self.state_dir,
                        peer_uid=peer_uid,
                        action=action,
                        project_id=project_id,
                        project_root=project_root,
                        db_path=self.db_path,
                    )
                )
            except PolicyLocalError as exc:
                errors.append(exc)
        if len(valid) > 1:
            raise PolicyLocalError("multiple valid policy profiles match; run `open-clank policy use`")
        if not valid:
            if explicit and errors:
                raise errors[0]
            raise PolicyLocalError("no valid policy profile matches this project")
        return valid[0]

    def handle(self, request: Mapping[str, Any], *, peer_uid: int) -> dict[str, Any]:
        if int(peer_uid) != os.getuid():
            raise PolicyLocalError("policy worker rejected a different OS user")
        credential = self._credential(request, peer_uid)
        action = str(request["action"])
        if action == "select":
            return {
                "ok": True,
                "profile": credential["profile"],
                "project_id": credential["project_id"],
                "project_root": credential["project_root"],
                "activation_revision": credential["activation_revision"],
            }
        from src.project_hex import HexResolutionError, require_hex_activation, validate_project_file_candidates

        try:
            resolution = require_hex_activation(
                credential["project_root"],
                owner=credential["owner"],
                project_id=credential["project_id"],
                db_path=self.db_path,
                workspace_root=credential["project_root"],
            )
            result = validate_project_file_candidates(
                resolution,
                owner=credential["owner"],
                project_id=credential["project_id"],
                db_path=self.db_path,
                candidates=_staged_candidates(credential["project_root"])
                if action == "pre-commit"
                else {},
                stage=action,
            )
        except HexResolutionError as exc:
            raise PolicyLocalError(str(exc)) from exc
        return {
            "ok": True,
            "allowed": bool(result.get("allowed")),
            "findings": list(result.get("findings") or []),
            "warnings": list(result.get("warnings") or []),
            "profile": credential["profile"],
            "project_id": credential["project_id"],
            "contract_hash": resolution.contract_hash,
            "engine_version": resolution.engine_version,
        }

    def serve_forever(self, stop: Optional[Event] = None) -> None:
        stop = stop or Event()
        socket_path = self.socket_path.expanduser().resolve(strict=False)
        _private_directory(socket_path.parent)
        if os.path.lexists(socket_path):
            info = socket_path.lstat()
            if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid():
                raise PolicyLocalError("refusing to replace an unowned policy socket path")
            probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                probe.settimeout(0.1)
                probe.connect(str(socket_path))
            except OSError:
                socket_path.unlink()
            else:
                raise PolicyLocalError("policy worker is already running")
            finally:
                probe.close()
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        bound_identity: tuple[int, int] | None = None
        try:
            listener.bind(str(socket_path))
            os.chmod(socket_path, 0o600, follow_symlinks=False)
            info = socket_path.lstat()
            if stat.S_IMODE(info.st_mode) != 0o600 or info.st_uid != os.getuid():
                raise PolicyLocalError("policy socket is not owner-only")
            bound_identity = (int(info.st_dev), int(info.st_ino))
            listener.listen(16)
            listener.settimeout(0.2)
            while not stop.is_set():
                try:
                    connection, _ = listener.accept()
                except socket.timeout:
                    continue
                with connection:
                    connection.settimeout(10)
                    try:
                        peer_uid = _peer_uid(connection)
                        request = _receive_json(connection, limit=MAX_REQUEST_BYTES)
                        response = self.handle(request, peer_uid=peer_uid)
                    except Exception as exc:
                        from src.shell_policy import redact_text

                        response = {
                            "ok": False,
                            "error": redact_text(exc)[:2048] or "policy worker failed closed",
                        }
                    payload = _canonical_json(response)
                    if len(payload) > MAX_RESPONSE_BYTES:
                        payload = _canonical_json({"ok": False, "error": "policy response exceeded its limit"})
                    try:
                        connection.sendall(payload)
                    except OSError:
                        pass
        finally:
            listener.close()
            try:
                info = socket_path.lstat()
                if bound_identity == (int(info.st_dev), int(info.st_ino)):
                    socket_path.unlink()
            except FileNotFoundError:
                pass


class PolicyClient:
    def __init__(self, socket_path: str | os.PathLike[str] | None = None):
        self.socket_path = Path(socket_path) if socket_path is not None else default_socket_path()

    def request(self, request: Mapping[str, Any], *, start_worker: bool = False) -> dict[str, Any]:
        try:
            response = self._request_once(request)
        except (FileNotFoundError, ConnectionRefusedError, socket.timeout, OSError) as first:
            if not start_worker:
                raise PolicyLocalError("local policy worker is unavailable") from first
            try:
                subprocess.run(
                    ["systemctl", "--user", "start", POLICY_USER_UNIT],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=10,
                    check=False,
                )
            except (OSError, subprocess.SubprocessError):
                pass
            deadline = time.monotonic() + 2
            while True:
                try:
                    response = self._request_once(request)
                    break
                except (FileNotFoundError, ConnectionRefusedError, socket.timeout, OSError) as exc:
                    if time.monotonic() >= deadline:
                        raise PolicyLocalError("local policy worker is unavailable") from exc
                    time.sleep(0.05)
        if not response.get("ok"):
            raise PolicyLocalError(str(response.get("error") or "local policy request failed"))
        return response

    def _request_once(self, request: Mapping[str, Any]) -> dict[str, Any]:
        payload = _canonical_json(request)
        if len(payload) > MAX_REQUEST_BYTES:
            raise PolicyLocalError("policy request is too large")
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            connection.settimeout(10)
            connection.connect(str(self.socket_path))
            connection.sendall(payload)
            connection.shutdown(socket.SHUT_WR)
            raw = bytearray()
            while len(raw) <= MAX_RESPONSE_BYTES:
                chunk = connection.recv(min(16 * 1024, MAX_RESPONSE_BYTES + 1 - len(raw)))
                if not chunk:
                    break
                raw.extend(chunk)
            if not raw or len(raw) > MAX_RESPONSE_BYTES:
                raise PolicyLocalError("policy worker returned an empty or oversized response")
            response = json.loads(raw)
            if not isinstance(response, dict):
                raise PolicyLocalError("policy worker response is malformed")
            return response
        except (TypeError, ValueError) as exc:
            raise PolicyLocalError("policy worker response is malformed") from exc
        finally:
            connection.close()


def git_project_root(path: str | os.PathLike[str]) -> Path:
    candidate = Path(path).expanduser().resolve(strict=True)
    result = subprocess.run(
        ["git", "-C", str(candidate), "rev-parse", "--show-toplevel"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=10,
        check=False,
    )
    if result.returncode or not result.stdout.strip():
        raise PolicyLocalError("project root is not a Git worktree")
    return Path(result.stdout.strip()).resolve(strict=True)


def git_policy_selection(project_root: str | os.PathLike[str]) -> tuple[Optional[str], Optional[str]]:
    root = git_project_root(project_root)

    def value(key: str) -> Optional[str]:
        result = subprocess.run(
            ["git", "-C", str(root), "config", "--local", "--get", key],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=10,
            check=False,
        )
        return result.stdout.strip() or None if result.returncode in {0, 1} else None

    return value("openclank.policyProfile"), value("openclank.policyProject")


def set_git_policy_selection(
    project_root: str | os.PathLike[str], *, profile: str, project_id: str
) -> None:
    root = git_project_root(project_root)
    if not _PROFILE_NAME.fullmatch(str(profile or "")) or not str(project_id or "").strip():
        raise PolicyLocalError("profile and project are required")
    for key, value in (
        ("openclank.policyProfile", profile),
        ("openclank.policyProject", project_id),
    ):
        result = subprocess.run(
            ["git", "-C", str(root), "config", "--local", key, value],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=10,
            check=False,
        )
        if result.returncode:
            raise PolicyLocalError("could not write the local Git policy selection")
