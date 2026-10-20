"""Best-effort history capture at Python mutation owners.

The live filesystem remains authoritative.  This adapter only talks to the local
history worker when it is explicitly configured, and records a truthful status
when the worker is unavailable or a capture phase fails.  Callers must keep the
returned status with their live receipt; a paused/failed capture is never a
protected-history acknowledgement.
"""

from __future__ import annotations

import contextlib
import contextvars
import functools
import base64
import hashlib
import json
import os
import pathlib
import stat
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Iterable, Iterator, Mapping

from src.openclank.history_client import HistoryClient, HistoryClientError


_ACTIVE_ACTION: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "openclank_history_action", default=None
)
_STATUS: contextvars.ContextVar[dict[str, Any]] = contextvars.ContextVar(
    "openclank_history_status",
    default={"history_status": "unconfigured", "capture_phase": "unavailable"},
)
_OPEN_SUPPORTS_DIR_FD = os.open in getattr(os, "supports_dir_fd", ())
_LSTAT_SUPPORTS_DIR_FD = os.lstat in getattr(os, "supports_dir_fd", ())
_SCANDIR_SUPPORTS_FD = os.scandir in getattr(os, "supports_fd", ())


@dataclass
class HistoryContext:
    """Authenticated, request-scoped identity for one mutation owner."""

    actor_id: str
    account_id: str
    workspace_id: str
    socket_path: str = ""
    token: str = ""
    actor_kind: str = "agent"
    session_id: str = ""
    run_id: str = ""
    task_id: str = ""
    tool_id: str = "filesystem"
    roots: tuple[str, ...] = ()
    root_bindings: tuple[tuple[str, str], ...] = ()
    root_identities: tuple[tuple[str, int, int], ...] = ()
    client: Any = None
    status: dict[str, Any] = field(default_factory=dict)

    def allowed(self, path: str) -> bool:
        return self.root_for(path) is not None

    def root_for(self, path: str) -> tuple[str, str, str] | None:
        """Return the trusted root id, canonical root, and relative path."""
        target = os.path.realpath(path)
        candidates = list(self.root_bindings)
        if not candidates:
            candidates = [
                (f"root:{hashlib.sha256(os.path.realpath(root).encode()).hexdigest()[:24]}", root)
                for root in self.roots
            ]
        selected: tuple[str, str] | None = None
        for root_id, root in candidates:
            root_path = os.path.realpath(root)
            try:
                if os.path.commonpath([target, root_path]) == root_path:
                    if selected is None or len(root_path) > len(os.path.realpath(selected[1])):
                        selected = (str(root_id), root_path)
            except ValueError:
                continue
        if selected is None:
            return None
        root_id, root_path = selected
        return root_id, root_path, os.path.relpath(target, root_path).replace(os.sep, "/")


def trusted_tool_context(
    *,
    actor_id: str,
    account_id: str,
    workspace_id: str,
    workspace_root: str,
    actor_kind: str = "agent",
    session_id: str = "",
    run_id: str = "",
    task_id: str = "",
    tool_id: str = "filesystem",
) -> HistoryContext | None:
    """Construct a request-scoped context at the trusted tool dispatcher.

    The model-facing tool payload cannot create this context.  The dispatcher
    supplies the authenticated account and the workspace root after its normal
    ownership/containment checks; the worker socket remains configuration, not
    identity.  A missing identity yields ordinary live-only behavior.
    """
    actor = str(actor_id or "").strip()
    account = str(account_id or "").strip()
    workspace = os.path.realpath(str(workspace_root or "").strip())
    stable_workspace = str(workspace_id or "").strip()
    if not actor or not account or not stable_workspace or not workspace or not os.path.isdir(workspace):
        return None
    return HistoryContext(
        actor_id=actor,
        account_id=account,
        workspace_id=stable_workspace,
        socket_path=str(os.environ.get("OPENCLANK_HISTORY_SOCKET") or "").strip(),
        token="",
        actor_kind=actor_kind,
        session_id=str(session_id or "").strip(),
        run_id=str(run_id or "").strip(),
        task_id=str(task_id or "").strip(),
        tool_id=str(tool_id or "filesystem").strip(),
        roots=(workspace,),
        root_bindings=((f"root:{hashlib.sha256(workspace.encode()).hexdigest()[:24]}", workspace),),
    )


_SECRET_NAMES = frozenset({
    ".env", ".env.local", "auth.json", "settings.json", "tokens.json",
    "credentials.json", "secrets.json", "api_keys.json",
})
_SECRET_PARTS = frozenset({".secrets", "auth", "credentials", "tokens", "secrets"})


def _excluded_secret(path: str) -> bool:
    name = os.path.basename(os.path.normpath(path)).lower()
    if name in _SECRET_NAMES:
        return True
    if name.startswith(("auth.", "token.", "credential.", "secret.", "api_key.", "settings.")):
        return True
    return any(part.lower() in _SECRET_PARTS for part in pathlib.Path(path).parts)


def context_from_mapping(values: Mapping[str, Any]) -> HistoryContext | None:
    """Build identity only from authenticated request context, never owner env."""
    if values.get("history_capture") is not True:
        return None
    actor_id = str(values.get("actor_id") or "").strip()
    account_id = str(values.get("account_id") or values.get("owner_account_id") or "").strip()
    workspace_id = str(values.get("workspace_id") or "").strip()
    workspace = str(values.get("workspace") or "").strip()
    raw_roots = values.get("history_roots") or ([workspace] if workspace else [])
    roots: list[str] = []
    root_bindings: list[tuple[str, str]] = []
    for raw_root in raw_roots:
        if isinstance(raw_root, Mapping):
            root_path = str(raw_root.get("canonical_path") or raw_root.get("path") or "").strip()
            root_id = str(raw_root.get("root_id") or raw_root.get("id") or "").strip()
        else:
            root_path = str(raw_root).strip()
            root_id = ""
        if not root_path:
            continue
        root_path = os.path.realpath(root_path)
        root_id = root_id or f"root:{hashlib.sha256(root_path.encode()).hexdigest()[:24]}"
        roots.append(root_path)
        root_bindings.append((root_id, root_path))
    roots = tuple(dict.fromkeys(roots))
    root_bindings = list(dict.fromkeys(root_bindings))
    if not actor_id or not account_id or not workspace_id or not roots:
        return None
    return HistoryContext(
        actor_id=actor_id,
        account_id=account_id,
        workspace_id=workspace_id,
        socket_path=str(values.get("history_socket") or os.environ.get("OPENCLANK_HISTORY_SOCKET") or "").strip(),
        token=str(values.get("history_token") or ""),
        actor_kind=str(values.get("actor_kind") or "agent"),
        roots=roots,
        root_bindings=tuple(root_bindings),
        session_id=str(values.get("session_id") or values.get("chat_id") or "").strip(),
        run_id=str(values.get("run_id") or "").strip(),
        task_id=str(values.get("task_id") or "").strip(),
        tool_id=str(values.get("tool_id") or "filesystem").strip(),
    )


def _fingerprint(data: bytes | None) -> str:
    if data is None:
        return "missing"
    return f"sha256:{hashlib.sha256(data).hexdigest()}:{len(data)}"


def _identity(info: os.stat_result) -> tuple[int, int]:
    return int(info.st_dev), int(info.st_ino)


def _same_identity(left: os.stat_result, right: os.stat_result) -> bool:
    return _identity(left) == _identity(right)


def _same_stability(left: os.stat_result, right: os.stat_result) -> bool:
    return (
        _identity(left),
        stat.S_IFMT(left.st_mode),
        int(left.st_size),
        int(left.st_mtime_ns),
        int(left.st_ctime_ns),
    ) == (
        _identity(right),
        stat.S_IFMT(right.st_mode),
        int(right.st_size),
        int(right.st_mtime_ns),
        int(right.st_ctime_ns),
    )


def _read_strict_fd(name: str, dir_fd: int, expected: os.stat_result) -> bytes:
    """Read one regular file without following a raced final symlink."""
    nofollow = os.O_NOFOLLOW
    if not nofollow or not os.O_CLOEXEC:
        raise ValueError("strict capture requires O_NOFOLLOW")
    fd = os.open(
        name,
        os.O_RDONLY | nofollow | os.O_CLOEXEC,
        dir_fd=dir_fd,
    )
    try:
        opened = os.fstat(fd)
        if stat.S_ISLNK(opened.st_mode) or not stat.S_ISREG(opened.st_mode):
            raise ValueError(f"unsupported file replaced during shell capture: {name}")
        if (opened.st_dev, opened.st_ino) != (expected.st_dev, expected.st_ino):
            raise ValueError(f"file replaced during shell capture: {name}")
        chunks: list[bytes] = []
        while True:
            chunk = os.read(fd, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        finished = os.fstat(fd)
        if not _same_stability(finished, opened):
            raise ValueError(f"file changed during shell capture: {name}")
        return b"".join(chunks)
    finally:
        os.close(fd)


def _read_strict_file(path: str, expected: os.stat_result) -> bytes:
    if os.name == "nt":
        from .windows_history_io import read_file
        return read_file(path, expected, _same_stability)
    parent = os.path.dirname(path) or os.curdir
    parent_fd = _open_strict_directory(parent)
    try:
        return _read_strict_fd(os.path.basename(path), parent_fd, expected)
    finally:
        os.close(parent_fd)


def _open_strict_directory(path: str) -> int:
    """Open one directory by descriptor without following its final name."""
    directory = os.path.abspath(path)
    parent = os.path.dirname(directory) or os.curdir
    name = os.path.basename(directory) or os.curdir
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    parent_fd = os.open(parent, flags)
    try:
        return os.open(name, flags, dir_fd=parent_fd)
    finally:
        os.close(parent_fd)


def _strict_capture_supported() -> bool:
    """Whether this host can enforce no-follow reads for strict capture."""
    if os.name == "nt":
        return True
    required = (
        getattr(os, "O_NOFOLLOW", None),
        getattr(os, "O_DIRECTORY", None),
        getattr(os, "O_CLOEXEC", None),
        getattr(os, "scandir", None),
        getattr(os, "fstat", None),
        getattr(os, "lstat", None),
        getattr(os, "read", None),
        getattr(os, "close", None),
    )
    return bool(
        all(required)
        and _OPEN_SUPPORTS_DIR_FD
        and _LSTAT_SUPPORTS_DIR_FD
        and _SCANDIR_SUPPORTS_FD
        and callable(os.open)
        and callable(os.lstat)
        and callable(os.scandir)
    )


def _capture_payload(
    path: str,
    *,
    strict: bool = False,
    expected_identity: tuple[int, int] | None = None,
) -> bytes | None:
    """Read the exact provider payload for a file, directory, or symlink."""
    stat_result = os.lstat(path)
    if expected_identity is not None and _identity(stat_result) != expected_identity:
        raise ValueError(f"path replaced during shell capture: {path}")
    if stat.S_ISLNK(stat_result.st_mode):
        if strict:
            raise ValueError(f"symlink boundary appeared during shell capture: {path}")
        return os.readlink(path).encode("utf-8", "surrogateescape")
    if stat.S_ISDIR(stat_result.st_mode):
        return _directory_manifest(path, strict=strict, expected=stat_result)
    if strict:
        return _read_strict_file(path, stat_result)
    return pathlib.Path(path).read_bytes()


def _validate_strict_capture_tree(
    paths: Iterable[str],
    *,
    expected_identities: Mapping[str, tuple[int, int]] | None = None,
) -> None:
    """Validate a shell after-state without opening file bytes."""
    for raw_path in paths:
        path = os.path.abspath(str(raw_path))
        try:
            info = os.lstat(path)
        except FileNotFoundError:
            continue
        expected = (expected_identities or {}).get(path)
        if expected is not None and _identity(info) != expected:
            raise ValueError(f"path replaced during shell capture: {path}")
        if _excluded_secret(path):
            raise ValueError(f"protected path appeared during shell capture: {path}")
        if stat.S_ISLNK(info.st_mode):
            raise ValueError(f"symlink boundary appeared during shell capture: {path}")
        if not stat.S_ISDIR(info.st_mode):
            continue
        _strict_directory_walk(path, expected=info, include_content=False)


def _opaque_root_label(root: str) -> str:
    canonical = os.path.realpath(str(root).strip())
    return f"root:{hashlib.sha256(canonical.encode('utf-8')).hexdigest()[:24]}"


def _resource_handle(
    client: Any,
    context: HistoryContext,
    path: str,
) -> tuple[str, str]:
    resolved = context.root_for(path)
    if resolved is None:
        raise ValueError("path is outside the authorized history roots")
    root_id, root_path, relative_path = resolved
    register = getattr(client, "register_resource", None)
    if callable(register):
        response = register(
            workspace_id=context.workspace_id,
            root_id=root_id,
            root_path=root_path,
            relative_path=relative_path,
        )
        handle = response.get("Resource", {}).get("handle", {}) if isinstance(response, Mapping) else {}
        resource_id = str(handle.get("resource_id") or "").strip()
        if not resource_id:
            raise ValueError("history service returned no opaque resource handle")
        if str(handle.get("account_id") or context.account_id) != context.account_id:
            raise ValueError("history service returned a resource for another account")
        if str(handle.get("workspace_id") or context.workspace_id) != context.workspace_id:
            raise ValueError("history service returned a resource for another workspace")
        return resource_id, root_id
    # Small in-process fakes and older development clients still exercise the
    # envelope shape. Keep their fallback opaque and path-free; production
    # HistoryClient always takes the service registry branch above.
    material = f"{context.account_id}\0{context.workspace_id}\0{root_id}\0{relative_path}"
    return f"file:python:{hashlib.sha256(material.encode('utf-8')).hexdigest()}", root_id


def _configured_client(context: HistoryContext | None) -> HistoryClient | Any | None:
    if context is None or context.client is not None:
        return context.client if context is not None else None
    if not context.socket_path:
        return None
    return HistoryClient(
        context.socket_path,
        actor_id=context.actor_id,
        account_id=context.account_id,
        # An empty context token means the trusted service-client credential
        # provider may supply its configured supervisor token.  Never use an
        # actor/account environment value as identity.
        token=context.token or None,
    )


def _set_status(context: HistoryContext | None = None, **values: Any) -> None:
    if context is not None:
        context.status.clear()
        context.status.update(values)
    _STATUS.set(dict(values))


def last_history_status(context: HistoryContext | None = None) -> dict[str, Any]:
    """Return a copy of the last owner hook status for a live tool receipt."""
    return dict(context.status if context is not None and context.status else _STATUS.get())


def declared_root_baseline(roots: Iterable[str], *, max_files: int = 4096) -> dict[str, Any]:
    """Hash a configured root inventory for a subprocess coverage receipt.

    This is deliberately a baseline, not a watcher or an atomic write set.  It
    reports only configured roots and leaves concurrent external changes as an
    explicit limitation for the caller.
    """
    entries: list[tuple[str, str, int]] = []
    normalized: list[str] = []
    truncated = False
    for raw in roots:
        root = os.path.realpath(str(raw).strip())
        if not root or root in normalized or not os.path.isdir(root):
            continue
        normalized.append(root)
        for current, dirs, files in os.walk(root, followlinks=False):
            dirs[:] = sorted(name for name in dirs if name not in {".git", ".venv", "node_modules", "target", "__pycache__"})
            for name in sorted(files):
                path = os.path.join(current, name)
                try:
                    stat = os.stat(path, follow_symlinks=False)
                except OSError:
                    continue
                if not os.path.isfile(path) or len(entries) >= max_files:
                    truncated = len(entries) >= max_files
                    break
                try:
                    value = _fingerprint(pathlib.Path(path).read_bytes())
                except OSError:
                    value = "unreadable"
                entries.append((os.path.relpath(path, root), value, int(stat.st_size)))
            if truncated:
                break
        if truncated:
            break
    serialized = json.dumps(entries, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return {
        "kind": "DeclaredRootsBaseline",
        "roots": [_opaque_root_label(root) for root in normalized],
        "file_count": len(entries),
        "truncated": truncated,
        "manifest_digest": f"sha256:{hashlib.sha256(serialized).hexdigest()}:{len(serialized)}",
    }


def _envelope(
    *,
    action_id: str,
    operation: str,
    path: str,
    before: bytes | None,
    context: HistoryContext,
    client: Any | None = None,
    paths: Iterable[str] = (),
) -> dict[str, Any]:
    account_id = context.account_id
    workspace_id = context.workspace_id
    paths = list(paths)
    modified = [path, *paths]
    handles = [_resource_handle(client or context.client, context, item) for item in dict.fromkeys(modified)]
    resource_keys = [
        {
            "account_id": account_id,
            "workspace_id": workspace_id,
            "provider": "filesystem",
            "resource_id": resource_id,
        }
        for resource_id, _root_id in handles
    ]
    root_ids = list(dict.fromkeys(root_id for _resource_id, root_id in handles))
    first_resource_id = resource_keys[0]["resource_id"]
    display_name = os.path.basename(os.path.normpath(path)) or "<unnamed>"
    extra_paths = paths
    coverage = {
        "kind": "KnownMutationHooks" if not extra_paths else "ObservedAfterOnly",
        "captured_at_millis": int(time.time() * 1000),
        "roots": root_ids,
        "exclusions": [] if not extra_paths else ["non-primary batch targets lack exact byte payloads"],
    }
    return {
        "schema_version": 1,
        "action_id": action_id,
        "actor_account_id": account_id,
        "resource_key": resource_keys[0],
        "guard_resource_ids": [],
        "modified_resource_ids": resource_keys,
        "operation": operation,
        "expected_revision": None,
        "actor_id": context.actor_id,
        "actor_kind": context.actor_kind,
        "session_id": context.session_id or None,
        "run_id": context.run_id or None,
        "task_id": context.task_id or None,
        "tool_id": context.tool_id or "filesystem",
        "before_revision": {"Opaque": {"kind": "fingerprint", "value": _fingerprint(before)}} if before is not None else None,
        "expected_after_revision": None,
        "original_locator": {
            "display_name": display_name,
            "location_label": "authorized-files-root",
            "opaque_ref": first_resource_id,
        },
        "destination_locator": None,
        "timestamp_millis": coverage["captured_at_millis"],
        "coverage": coverage,
        "per_resource_outcomes": None,
    }


def _directory_manifest(
    path: str,
    *,
    strict: bool = False,
    expected: os.stat_result | None = None,
) -> bytes:
    """Return a deterministic recursive directory preimage.

    Directory metadata alone cannot restore a deleted tree. The manifest binds
    every relative entry, type, mode, link target, size, and file digest to the
    captured bytes, while keeping the provider locator out of the payload.
    """
    root = os.path.abspath(path)
    if strict:
        if _excluded_secret(root):
            raise ValueError(f"protected path appeared during shell capture: {root}")
        return _strict_directory_manifest(root, expected=expected)
    entries: list[dict[str, Any]] = []
    for current, dirs, files in os.walk(root, followlinks=False):
        dirs.sort()
        files.sort()
        for name in [*dirs, *files]:
            item = os.path.join(current, name)
            relative = os.path.relpath(item, root).replace(os.sep, "/")
            stat = os.lstat(item)
            record: dict[str, Any] = {
                "path": relative,
                "mode": stat.st_mode & 0o7777,
                "mtime_millis": int(stat.st_mtime_ns // 1_000_000),
            }
            if os.path.islink(item):
                record.update({"type": "symlink", "target": os.readlink(item)})
            elif os.path.isdir(item):
                record["type"] = "directory"
            else:
                content = pathlib.Path(item).read_bytes()
                record.update({
                    "type": "file",
                    "size": len(content),
                    "sha256": hashlib.sha256(content).hexdigest(),
                    "content": base64.b64encode(content).decode("ascii"),
                    "content_encoding": "base64",
                })
            entries.append(record)
    return json.dumps(
        {"version": 1, "root_type": "directory", "entries": entries},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def _strict_directory_walk(
    root: str,
    *,
    expected: os.stat_result | None = None,
    include_content: bool,
) -> list[dict[str, Any]]:
    """Walk a tree through explicitly pinned, no-follow directory descriptors."""
    if os.name == "nt":
        from .windows_history_io import directory_walk
        return directory_walk(root, expected=expected, include_content=include_content, same=_same_stability, excluded=_excluded_secret)
    root = os.path.abspath(root)
    root_fd = _open_strict_directory(root)
    entries: list[dict[str, Any]] = []
    child_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC

    def visit(current_path: pathlib.Path, current_fd: int, relative_current: str, expected_dir: os.stat_result) -> None:
        opened_dir = os.fstat(current_fd)
        if not _same_stability(opened_dir, expected_dir) or not stat.S_ISDIR(opened_dir.st_mode):
            raise ValueError(f"directory replaced during shell capture: {current_path}")
        with os.scandir(current_fd) as iterator:
            names = sorted(entry.name for entry in iterator)
        for name in names:
            relative = os.path.normpath(os.path.join(relative_current, name))
            item = current_path / name
            if _excluded_secret(str(item)):
                raise ValueError(f"protected path appeared during shell capture: {item}")
            try:
                entry_stat = os.lstat(name, dir_fd=current_fd)
            except FileNotFoundError as exc:
                raise ValueError(f"entry changed during shell capture: {item}") from exc
            if stat.S_ISLNK(entry_stat.st_mode):
                raise ValueError(f"symlink boundary appeared during shell capture: {item}")
            record: dict[str, Any] = {
                "path": relative.replace(os.sep, "/"),
                "mode": entry_stat.st_mode & 0o7777,
                "mtime_millis": int(entry_stat.st_mtime_ns // 1_000_000),
            }
            if stat.S_ISDIR(entry_stat.st_mode):
                record["type"] = "directory"
                child_fd = os.open(name, child_flags, dir_fd=current_fd)
                try:
                    opened_child = os.fstat(child_fd)
                    if not _same_stability(opened_child, entry_stat) or not stat.S_ISDIR(opened_child.st_mode):
                        raise ValueError(f"directory replaced during shell capture: {item}")
                    entries.append(record)
                    visit(item, child_fd, relative, entry_stat)
                finally:
                    os.close(child_fd)
                try:
                    after_child = os.lstat(name, dir_fd=current_fd)
                except FileNotFoundError as exc:
                    raise ValueError(f"directory changed during shell capture: {item}") from exc
                if not _same_stability(after_child, entry_stat) or not stat.S_ISDIR(after_child.st_mode):
                    raise ValueError(f"directory replaced during shell capture: {item}")
            elif stat.S_ISREG(entry_stat.st_mode):
                record["type"] = "file"
                if include_content:
                    content = _read_strict_fd(name, current_fd, entry_stat)
                    record.update({
                        "size": len(content),
                        "sha256": hashlib.sha256(content).hexdigest(),
                        "content": base64.b64encode(content).decode("ascii"),
                        "content_encoding": "base64",
                    })
                entries.append(record)
                try:
                    after_file = os.lstat(name, dir_fd=current_fd)
                except FileNotFoundError as exc:
                    raise ValueError(f"file changed during shell capture: {item}") from exc
                if not _same_stability(after_file, entry_stat) or not stat.S_ISREG(after_file.st_mode):
                    raise ValueError(f"file replaced during shell capture: {item}")
            else:
                raise ValueError(f"unsupported entry during shell capture: {item}")
        with os.scandir(current_fd) as iterator:
            final_names = sorted(entry.name for entry in iterator)
        final_dir = os.fstat(current_fd)
        if not _same_stability(final_dir, opened_dir):
            raise ValueError(f"directory changed during shell capture: {current_path}")
        if final_names != names:
            for name in sorted(set(final_names) - set(names)):
                item = current_path / name
                if _excluded_secret(str(item)):
                    raise ValueError(f"protected path appeared during shell capture: {item}")
                try:
                    info = os.lstat(name, dir_fd=current_fd)
                except FileNotFoundError:
                    continue
                if stat.S_ISLNK(info.st_mode):
                    raise ValueError(f"symlink boundary appeared during shell capture: {item}")
            raise ValueError(f"directory entry set changed during shell capture: {current_path}")

    try:
        opened_root = os.fstat(root_fd)
        if not stat.S_ISDIR(opened_root.st_mode) or (
            expected is not None and not _same_identity(opened_root, expected)
        ):
            raise ValueError(f"directory replaced during shell capture: {root}")
        visit(pathlib.Path(root), root_fd, "", opened_root)
        try:
            final_root = os.lstat(root)
        except FileNotFoundError as exc:
            raise ValueError(f"root changed during shell capture: {root}") from exc
        if not _same_stability(final_root, opened_root) or not stat.S_ISDIR(final_root.st_mode):
            raise ValueError(f"root replaced during shell capture: {root}")
    finally:
        os.close(root_fd)
    return entries


def _strict_directory_manifest(
    root: str,
    *,
    expected: os.stat_result | None = None,
) -> bytes:
    """Manifest a tree with dir-fd and no-follow checks before each read."""
    entries = _strict_directory_walk(root, expected=expected, include_content=True)
    return json.dumps(
        {"version": 1, "root_type": "directory", "entries": entries},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def _batch_entries(
    *,
    path: str,
    paths: list[str],
    envelope: Mapping[str, Any],
    context: HistoryContext,
    strict_tree: bool = False,
    expected_identities: Mapping[str, tuple[int, int]] | None = None,
) -> tuple[list[dict[str, Any]], bytes | None]:
    """Read every declared before-state for a recoverable parent mutation."""
    targets = [path, *paths]
    resource_keys = [envelope["resource_key"], *envelope.get("modified_resource_ids", [])[1:]]
    entries: list[dict[str, Any]] = []
    primary_before: bytes | None = None
    for index, target in enumerate(targets):
        try:
            info = os.lstat(target)
        except FileNotFoundError:
            if strict_tree:
                raise ValueError(f"authorized shell capture root disappeared: {target}")
            before = None
            existence = "Absent"
            resource_type = "File"
            mode = size = modified = None
            exact = True
        except OSError as exc:
            raise ValueError(f"cannot inspect batch preimage {target}: {exc}") from exc
        else:
            existence = "Present"
            expected = (expected_identities or {}).get(os.path.abspath(target))
            if expected is not None and _identity(info) != expected:
                raise ValueError(f"path replaced during shell capture: {target}")
            mode = info.st_mode & 0o7777
            size = info.st_size
            modified = int(info.st_mtime_ns // 1_000_000)
            if stat.S_ISLNK(info.st_mode):
                if strict_tree:
                    raise ValueError(f"symlink boundary appeared during shell capture: {target}")
                resource_type = "Symlink"
                before = os.readlink(target).encode("utf-8", "surrogateescape")
                exact = True
            elif stat.S_ISDIR(info.st_mode):
                resource_type = "Directory"
                before = _directory_manifest(
                    target,
                    strict=strict_tree,
                    expected=info if strict_tree else None,
                )
                exact = True
            elif stat.S_ISREG(info.st_mode):
                resource_type = "File"
                before = _capture_payload(
                    target,
                    strict=strict_tree,
                    expected_identity=expected,
                )
                exact = True
            else:
                raise ValueError(f"unsupported entry during shell capture: {target}")
        if index == 0:
            primary_before = before
        key = resource_keys[index]
        resolved = context.root_for(target)
        if resolved is None:
            raise ValueError("path is outside the authorized history roots")
        _root_id, _root_path, relative = resolved
        coverage = {
            "metadata": {"exact_preimage": exact},
            "byte_len": len(before) if before is not None else 0,
            "content_digest": (
                f"sha256:{hashlib.sha256(before).hexdigest()}"
                if before is not None and resource_type == "Directory"
                else f"sha256:{hashlib.sha256(before).hexdigest()}:{len(before)}"
                if before is not None
                else None
            ),
        }
        entries.append(
            {
                "resource_key": key,
                "old_locator": {
                    "display_name": os.path.basename(os.path.normpath(target)) or "<unnamed>",
                    "location_label": relative,
                    "opaque_ref": key["resource_id"],
                },
                "new_locator": {
                    "display_name": os.path.basename(os.path.normpath(target)) or "<unnamed>",
                    "location_label": relative,
                    "opaque_ref": key["resource_id"],
                },
                "expected_revision": {
                    "Opaque": {"kind": "fingerprint", "value": _fingerprint(before)}
                },
                "existence": existence,
                "resource_type": resource_type,
                "metadata": {
                    "mode": mode,
                    "size": None if resource_type == "Directory" else size,
                    "modified_millis": modified,
                    "opaque": {"manifest_digest": coverage["content_digest"]} if resource_type == "Directory" else None,
                },
                "content": before,
                "fingerprint": _fingerprint(before),
                "coverage": coverage,
            }
        )
    return entries, primary_before


def _validate_strict_batch_preimage(
    *,
    targets: Iterable[str],
    entries: list[Mapping[str, Any]],
    expected_identities: Mapping[str, tuple[int, int]],
) -> None:
    """Re-read every strict preimage immediately before PrepareBatch.

    The initial manifest establishes the bytes that the service will protect.
    This second descriptor-anchored read closes the window between that
    manifest and the durable prepare call: a new protected entry, replacement,
    deletion, or same-inode content change must be rejected before prepare.
    """
    target_list = list(targets)
    if len(target_list) != len(entries):
        raise ValueError("strict shell capture target manifest is incomplete")
    for target, entry in zip(target_list, entries):
        current = _capture_payload(
            target,
            strict=True,
            expected_identity=expected_identities.get(os.path.abspath(target)),
        )
        if current != entry.get("content") or _fingerprint(current) != entry.get("fingerprint"):
            raise ValueError(f"tree changed before PrepareBatch: {target}")


def _validate_strict_batch_after(
    *,
    targets: Iterable[str],
    entries: list[Mapping[str, Any]],
    expected_identities: Mapping[str, tuple[int, int]],
) -> None:
    """Validate exact after payloads immediately before CompleteBatch."""
    target_list = list(targets)
    if len(target_list) != len(entries):
        raise ValueError("strict shell capture after manifest is incomplete")
    for target, entry in zip(target_list, entries):
        current = _capture_payload(
            target,
            strict=True,
            expected_identity=expected_identities.get(os.path.abspath(target)),
        )
        if current != entry.get("content") or _fingerprint(current) != entry.get("fingerprint"):
            raise ValueError(f"after-state changed before CompleteBatch: {target}")


@dataclass
class CaptureHandle:
    action_id: str
    client: HistoryClient | None
    envelope: dict[str, Any]
    before: bytes | None
    context: HistoryContext | None
    status: str
    capture_phase: str
    error: str | None = None
    receipt: dict[str, Any] | None = None
    _token: contextvars.Token[str | None] | None = field(default=None, repr=False)
    batch_entries: list[dict[str, Any]] | None = None
    paths: tuple[str, ...] = ()
    finish_result: dict[str, Any] | None = None
    strict_tree: bool = False
    expected_identities: dict[str, tuple[int, int]] = field(default_factory=dict)

    @property
    def available(self) -> bool:
        return self.client is not None and self.status == "prepared"

    def finish(
        self,
        *,
        committed: bool = True,
        after: bytes | None = None,
        after_read_error: str | None = None,
    ) -> dict[str, Any]:
        if self.finish_result is not None:
            return dict(self.finish_result)
        if self.client is None or self.status != "prepared":
            result = {
                "action_id": self.action_id,
                "history_status": self.status,
                "capture_phase": self.capture_phase,
                "error": self.error,
            }
            _set_status(self.context, **result)
            self.finish_result = result
            return result
        try:
            if committed and after_read_error is None and self.strict_tree:
                try:
                    _validate_strict_capture_tree(
                        self.paths,
                        expected_identities=self.expected_identities,
                    )
                except (OSError, ValueError) as exc:
                    after_read_error = str(exc)
            live_fingerprint = None if after_read_error is not None else _fingerprint(after)
            live_status = "Committed" if committed else "NotCommitted"
            live = self.client.record_live(
                self.action_id,
                {
                    "action_id": self.action_id,
                    "status": live_status,
                    "fingerprint": live_fingerprint,
                    "after_unavailable": after_read_error is not None,
                },
            )
            if not committed:
                result = {
                    "action_id": self.action_id,
                    "history_status": "failed",
                    "capture_phase": "live_not_committed",
                    "before_fingerprint": _fingerprint(self.before),
                    "live": live,
                }
                _set_status(self.context, **result)
                self.finish_result = result
                return result
            if after_read_error is not None:
                result = {
                    "action_id": self.action_id,
                    "history_status": "failed",
                    "capture_phase": "after_failed",
                    "before_fingerprint": _fingerprint(self.before),
                    "error": after_read_error,
                    "live": live,
                }
                _set_status(self.context, **result)
                self.finish_result = result
                return result
            if self.batch_entries is not None and callable(getattr(self.client, "complete_batch", None)):
                after_entries: list[dict[str, Any]] = []
                all_targets = (self.paths[0] if self.paths else "", *self.paths[1:])
                for index, before_entry in enumerate(self.batch_entries):
                    target = all_targets[index] if index < len(all_targets) else ""
                    if index == 0:
                        target_after = after
                    else:
                        try:
                            if self.strict_tree:
                                _validate_strict_capture_tree(
                                    (target,),
                                    expected_identities=self.expected_identities,
                                )
                            target_after = _capture_payload(
                                target,
                                strict=self.strict_tree,
                                expected_identity=self.expected_identities.get(os.path.abspath(target)),
                            )
                        except FileNotFoundError:
                            target_after = None
                    item = dict(before_entry)
                    item.pop("old_locator", None)
                    item["locator"] = before_entry.get("new_locator")
                    item["content"] = target_after
                    item["existence"] = "Present" if target_after is not None else "Absent"
                    item["fingerprint"] = _fingerprint(target_after)
                    if target_after is None:
                        item["metadata"] = {"mode": None, "size": None, "modified_millis": None, "opaque": None}
                    else:
                        try:
                            after_info = os.lstat(target)
                        except OSError:
                            after_info = None
                        if after_info is None:
                            item["metadata"] = {"mode": None, "size": None, "modified_millis": None, "opaque": None}
                        else:
                            item["metadata"] = {
                                "mode": after_info.st_mode & 0o7777,
                                "size": None if before_entry.get("resource_type") == "Directory" else after_info.st_size,
                                "modified_millis": int(after_info.st_mtime_ns // 1_000_000),
                                "opaque": None,
                            }
                    item["outcome"] = {
                        "resource_id": before_entry["resource_key"]["resource_id"],
                        "status": "Committed",
                        "revision": {"Opaque": {"kind": "fingerprint", "value": item["fingerprint"]}},
                    }
                    coverage = dict(before_entry.get("coverage") or {})
                    coverage["metadata"] = {
                        **dict(coverage.get("metadata") or {}),
                        "exact_after": True,
                    }
                    coverage["byte_len"] = len(target_after) if target_after is not None else 0
                    coverage["content_digest"] = (
                        f"sha256:{hashlib.sha256(target_after).hexdigest()}"
                        if target_after is not None and before_entry.get("resource_type") == "Directory"
                        else f"sha256:{hashlib.sha256(target_after).hexdigest()}:{len(target_after)}"
                        if target_after is not None
                        else None
                    )
                    item["coverage"] = coverage
                    after_entries.append(item)
                if self.strict_tree:
                    _validate_strict_batch_after(
                        targets=all_targets,
                        entries=after_entries,
                        expected_identities=self.expected_identities,
                    )
                self.receipt = self.client.complete_batch(self.action_id, after_entries)
            else:
                self.receipt = self.client.complete(
                    self.action_id, content=after, fingerprint=_fingerprint(after)
                )
            result = {
                "action_id": self.action_id,
                "history_status": "complete",
                "capture_phase": "complete",
                "before_fingerprint": _fingerprint(self.before),
                "after_fingerprint": _fingerprint(after),
                "receipt": self.receipt,
                "live": live,
            }
        except Exception as exc:  # live writes must remain usable
            paused = "history_paused_budget" in str(exc)
            result = {
                "action_id": self.action_id,
                "history_status": "paused" if paused else "failed",
                "capture_phase": "budget" if paused else "after_failed",
                "before_fingerprint": _fingerprint(self.before),
                "after_fingerprint": _fingerprint(after),
                "error": str(exc),
            }
        _set_status(self.context, **result)
        self.finish_result = result
        return result

    def abort(self) -> dict[str, Any]:
        if self.finish_result is not None:
            return dict(self.finish_result)
        if self.client is not None and self.status == "prepared":
            try:
                self.client.abort(self.action_id)
            except Exception:
                pass
        result = {
            "action_id": self.action_id,
            "history_status": "aborted",
            "capture_phase": "aborted",
        }
        _set_status(self.context, **result)
        return result

    def close(self) -> None:
        if self._token is not None:
            _ACTIVE_ACTION.reset(self._token)
            self._token = None


def begin_file_capture(
    path: str,
    *,
    operation: str,
    context: HistoryContext | None = None,
    action_id: str | None = None,
    paths: Iterable[str] = (),
    strict_tree: bool = False,
    expected_identities: Mapping[str, tuple[int, int]] | None = None,
) -> CaptureHandle:
    """Prepare one file mutation, or return an honest unavailable handle."""
    action_id = action_id or _ACTIVE_ACTION.get() or f"file-{uuid.uuid4().hex}"
    paths = list(paths)
    all_paths = [path, *paths]
    expected_identities = {
        os.path.abspath(str(target)): (int(identity[0]), int(identity[1]))
        for target, identity in (expected_identities or {}).items()
    }
    if context is None:
        handle = CaptureHandle(action_id, None, {}, None, None, "paused", "unavailable", "no authenticated capture context", strict_tree=strict_tree)
        _set_status(action_id=action_id, history_status="paused", capture_phase="unavailable", error=handle.error)
        return handle
    excluded = next((item for item in all_paths if _excluded_secret(item)), None)
    outside = next((item for item in all_paths if not context.allowed(item)), None)
    if excluded is not None or outside is not None:
        reason = "secret path excluded" if excluded is not None else "path is outside the authorized history root"
        handle = CaptureHandle(action_id, None, {}, None, context, "paused", "excluded", reason, strict_tree=strict_tree)
        _set_status(context, action_id=action_id, history_status="paused", capture_phase="excluded", error=reason)
        return handle
    if strict_tree and not _strict_capture_supported():
        reason = "strict shell capture unavailable: race-safe no-follow reader is unsupported"
        handle = CaptureHandle(action_id, None, {}, None, context, "unavailable", "unavailable", reason, strict_tree=strict_tree)
        _set_status(context, action_id=action_id, history_status="unavailable", capture_phase="unavailable", error=reason)
        return handle
    try:
        if strict_tree:
            for target in all_paths:
                expected = expected_identities.get(os.path.abspath(target))
                if expected is None:
                    continue
                info = os.lstat(target)
                if _identity(info) != expected:
                    raise ValueError(f"path replaced during shell capture: {target}")
        before = _capture_payload(
            path,
            strict=strict_tree,
            expected_identity=expected_identities.get(os.path.abspath(path)),
        )
    except FileNotFoundError:
        if strict_tree:
            reason = f"authorized shell capture root disappeared: {path}"
            handle = CaptureHandle(
                action_id,
                None,
                {},
                None,
                context,
                "failed",
                "before_failed",
                reason,
                strict_tree=strict_tree,
            )
            _set_status(
                context,
                action_id=action_id,
                history_status="failed",
                capture_phase="before_failed",
                error=reason,
            )
            return handle
        before = None
    except (OSError, ValueError) as exc:
        before = None
        handle = CaptureHandle(action_id, None, {}, before, context, "failed", "before_failed", str(exc), strict_tree=strict_tree)
        _set_status(context, action_id=action_id, history_status="failed", capture_phase="before_failed", error=str(exc))
        return handle
    client = _configured_client(context)
    if client is None:
        handle = CaptureHandle(action_id, None, {}, before, context, "paused", "unavailable", "history worker is not configured", strict_tree=strict_tree)
        _set_status(context, action_id=action_id, history_status="paused", capture_phase="unavailable", error=handle.error)
        return handle
    envelope: dict[str, Any] = {}
    try:
        envelope = _envelope(
            action_id=action_id,
            operation=operation,
            path=path,
            before=before,
            paths=paths,
            context=context,
            client=client,
        )
        batch_prepare = getattr(client, "prepare_batch", None)
        if callable(batch_prepare):
            envelope["coverage"]["kind"] = "KnownMutationHooks"
            envelope["coverage"]["exclusions"] = []
            entries, before = _batch_entries(
                path=path,
                paths=paths,
                envelope=envelope,
                context=context,
                strict_tree=strict_tree,
                expected_identities=expected_identities,
            )
            if strict_tree:
                _validate_strict_batch_preimage(
                    targets=all_paths,
                    entries=entries,
                    expected_identities=expected_identities,
                )
            batch_prepare(envelope, entries)
            batch_entries = entries
        else:
            client.prepare(envelope, content=before, fingerprint=_fingerprint(before))
            batch_entries = None
    except HistoryClientError as exc:
        # The local exact preimage has already been validated. A configured
        # Lore transport/service/budget failure is truthful unavailable
        # coverage, not authority to reject the separately approved live
        # mutation.
        paused = "history_paused_budget" in str(exc)
        status = "paused" if paused else "unavailable"
        phase = "budget" if paused else "unavailable"
        handle = CaptureHandle(action_id, client, envelope, before, context, status, phase, str(exc), strict_tree=strict_tree)
        _set_status(context, action_id=action_id, history_status=status, capture_phase=phase, error=str(exc))
        return handle
    except Exception as exc:
        # This branch covers local envelope/preimage/integrity failures. Those
        # happen before a capture is durable and must still stop a strict shell
        # rather than make a false coverage claim.
        paused = "history_paused_budget" in str(exc)
        status = "paused" if paused else "failed"
        phase = "budget" if paused else "before_failed"
        handle = CaptureHandle(action_id, client, envelope, before, context, status, phase, str(exc), strict_tree=strict_tree)
        _set_status(context, action_id=action_id, history_status=status, capture_phase=phase, error=str(exc))
        return handle
    token = _ACTIVE_ACTION.set(action_id)
    handle = CaptureHandle(
        action_id,
        client,
        envelope,
        before,
        context,
        "prepared",
        "before_durable",
        _token=token,
        batch_entries=batch_entries,
        paths=tuple(all_paths),
        strict_tree=strict_tree,
        expected_identities=expected_identities,
    )
    _set_status(context, action_id=action_id, history_status="prepared", capture_phase="before_durable")
    return handle


@contextlib.contextmanager
def file_capture(
    path: str,
    *,
    operation: str,
    context: HistoryContext | None = None,
    action_id: str | None = None,
    paths: Iterable[str] = (),
) -> Iterator[CaptureHandle]:
    handle = begin_file_capture(path, operation=operation, context=context, action_id=action_id, paths=paths)
    try:
        yield handle
    except BaseException:
        handle.abort()
        raise
    finally:
        handle.close()


def complete_file_capture(handle: CaptureHandle, path: str, *, committed: bool = True) -> dict[str, Any]:
    read_error = None
    try:
        if committed and handle.strict_tree:
            _validate_strict_capture_tree(
                handle.paths or (path,),
                expected_identities=handle.expected_identities,
            )
        after = _capture_payload(
            path,
            strict=handle.strict_tree,
            expected_identity=handle.expected_identities.get(os.path.abspath(path)),
        )
    except FileNotFoundError:
        if handle.strict_tree:
            after = None
            read_error = f"authorized shell capture root disappeared: {path}"
            handle.error = read_error
        else:
            after = None
    except (OSError, ValueError) as exc:
        after = None
        read_error = str(exc)
        handle.error = read_error
    try:
        return handle.finish(committed=committed, after=after, after_read_error=read_error)
    finally:
        handle.close()


class RootJournalError(RuntimeError):
    """Root journal ownership, handoff, or terminalization is invalid."""


_ROOT_JOURNAL_VERSION = 1
_ROOT_JOURNAL_OPEN = "open"
_ROOT_JOURNAL_SETTLED = "settled"
_ROOT_JOURNAL_ABANDONED = "abandoned"
_ROOT_JOURNAL_TERMINAL = frozenset({_ROOT_JOURNAL_SETTLED, _ROOT_JOURNAL_ABANDONED})


def _journal_locked(method):
    """Serialize a journal read/modify/write across worker restarts."""
    @functools.wraps(method)
    def wrapped(self, *args, **kwargs):
        with self._locked():
            return method(self, *args, **kwargs)
    return wrapped


def _writer_owner_text(value: Any, field_name: str) -> str:
    text = str(value or "").strip()
    if not text or "\x00" in text:
        raise RootJournalError(f"writer owner {field_name} is required")
    return text


def _optional_writer_owner_text(value: Any, field_name: str) -> str | None:
    """Normalize an optional trusted identity without inventing one."""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if "\x00" in text:
        raise RootJournalError(f"writer owner {field_name} is invalid")
    return text


@dataclass(frozen=True)
class WriterOwner:
    """Persistent identity for the writer that owns a capture root journal.

    The identity is durable owner/session/action fields, never a bare PID, so
    a capture worker's journal can hand off cleanly across restart to the same
    persistent writer owner.
    """

    owner_id: str
    session_id: str
    action_id: str
    run_id: str | None = None
    task_id: str | None = None
    tool_id: str = "shell"

    def __post_init__(self) -> None:
        object.__setattr__(self, "owner_id", _writer_owner_text(self.owner_id, "owner_id"))
        object.__setattr__(self, "session_id", _writer_owner_text(self.session_id, "session_id"))
        object.__setattr__(self, "action_id", _writer_owner_text(self.action_id, "action_id"))
        # A run or scheduled task can genuinely be absent for a manually
        # initiated chat action. Preserve that absence rather than minting a
        # value that looks authenticated in a durable journal.
        object.__setattr__(self, "run_id", _optional_writer_owner_text(
            self.run_id, "run_id"
        ))
        object.__setattr__(self, "task_id", _optional_writer_owner_text(
            self.task_id, "task_id"
        ))
        object.__setattr__(self, "tool_id", _writer_owner_text(
            self.tool_id or "shell", "tool_id"
        ))

    def identity(self) -> tuple[str, str, str | None, str | None, str, str]:
        """The complete immutable authorization identity for one writer."""
        return (
            self.owner_id, self.session_id, self.run_id, self.task_id,
            self.tool_id, self.action_id,
        )

    def to_mapping(self) -> dict[str, str | None]:
        return {
            "owner_id": self.owner_id,
            "session_id": self.session_id,
            "action_id": self.action_id,
            "run_id": self.run_id,
            "task_id": self.task_id,
            "tool_id": self.tool_id,
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any] | None) -> WriterOwner:
        data = value if isinstance(value, Mapping) else {}
        return cls(
            owner_id=str(data.get("owner_id") or ""),
            session_id=str(data.get("session_id") or ""),
            action_id=str(data.get("action_id") or ""),
            run_id=data.get("run_id"),
            task_id=data.get("task_id"),
            tool_id=str(data.get("tool_id") or ""),
        )


class RootJournal:
    """Durable journal that owns every granted root for one capture action.

    An active journal is the continuing owner required before a persistent
    writer (tmux/editor/daemon) handoff is accepted. Its generation is a
    cross-process lease: a restarted worker cannot take an open batch merely
    by repeating an identity, and recovery fences a vanished worker before it
    can terminalize. Terminalization is single-use and honest: ``abandon``
    never records success, and a restart-shaped open journal is never reported
    as a completed capture.
    """

    def __init__(self, path: str):
        self.path = str(path or "")
        if not self.path:
            raise RootJournalError("root journal path is required")
        self._lease: int | None = None
        self._owner: WriterOwner | None = None

    @property
    def generation(self) -> int | None:
        """The generation this instance currently owns, if it has a lease."""
        return self._lease

    @contextlib.contextmanager
    def _locked(self):
        lock_path = self.path + ".lock"
        pathlib.Path(lock_path).parent.mkdir(parents=True, exist_ok=True)
        with open(lock_path, "a+b") as handle:
            if os.name == "nt":
                import msvcrt
                handle.seek(0, os.SEEK_END)
                if handle.tell() == 0:
                    handle.write(b"\0")
                    handle.flush()
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                if os.name == "nt":
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def read(self) -> dict[str, Any] | None:
        try:
            raw = pathlib.Path(self.path).read_text(encoding="utf-8")
        except (OSError, TypeError, ValueError):
            return None
        try:
            state = json.loads(raw)
        except ValueError:
            return None
        return state if isinstance(state, dict) else None

    @_journal_locked
    def open(
        self,
        *,
        action_id: str,
        writer_owner: WriterOwner,
        roots: Iterable[str],
    ) -> dict[str, Any]:
        """Create a new journal and its first generation lease."""
        action = _writer_owner_text(action_id, "action_id")
        root_list = tuple(
            dict.fromkeys(
                os.path.realpath(str(raw).strip())
                for raw in roots
                if str(raw or "").strip()
            )
        )
        if not root_list:
            raise RootJournalError("root journal requires at least one granted root")
        if writer_owner.action_id != action:
            raise RootJournalError("root journal writer owner must match the action identity")
        existing = self.read()
        if existing is not None:
            phase = str(existing.get("phase") or "")
            if phase in _ROOT_JOURNAL_TERMINAL:
                raise RootJournalError("root journal is already terminal")
            if phase != _ROOT_JOURNAL_OPEN:
                raise RootJournalError("root journal state is not open or terminal")
            if str(existing.get("action_id") or "") != action:
                raise RootJournalError("root journal action identity mismatch")
            current = WriterOwner.from_mapping(existing.get("writer_owner"))
            if current.identity() != writer_owner.identity():
                raise RootJournalError("root journal writer-owner mismatch")
            if tuple(existing.get("roots") or ()) != root_list:
                raise RootJournalError("root journal granted roots changed")
            raise RootJournalError(
                "root journal already has a live lease; recovery must first fence the old worker"
            )
        state = {
            "version": _ROOT_JOURNAL_VERSION,
            "action_id": action,
            "roots": list(root_list),
            "writer_owner": writer_owner.to_mapping(),
            "phase": _ROOT_JOURNAL_OPEN,
            "generation": 0,
            "handoffs": [
                {
                    "generation": 0,
                    "at": time.time(),
                    "writer_owner": writer_owner.to_mapping(),
                }
            ],
            "result": None,
            "reason": None,
            "created_at": time.time(),
            "updated_at": time.time(),
        }
        self._write(state)
        self._claim(state, writer_owner)
        return state

    @_journal_locked
    def handoff(self, writer_owner: WriterOwner) -> dict[str, Any]:
        """Record a persistent-writer handoff against an open journal."""
        existing = self.read()
        if existing is None:
            raise RootJournalError(
                "persistent-owner handoff requires a continuing root journal"
            )
        if str(existing.get("phase") or "") != _ROOT_JOURNAL_OPEN:
            raise RootJournalError("root journal is not open for handoff")
        if str(existing.get("action_id") or "") != writer_owner.action_id:
            raise RootJournalError("root journal action identity mismatch")
        current = WriterOwner.from_mapping(existing.get("writer_owner"))
        if current.identity() != writer_owner.identity():
            raise RootJournalError("root journal writer-owner mismatch")
        self._require_current_lease(existing, current)
        state = self._append_handoff(existing, writer_owner)
        self._claim(state, writer_owner)
        return state

    @_journal_locked
    def settle(self, result: Mapping[str, Any]) -> dict[str, Any]:
        """Record an honest terminal capture result (single-use)."""
        return self._close(_ROOT_JOURNAL_SETTLED, result=dict(result or {}), reason=None)

    @_journal_locked
    def abandon(
        self,
        reason: str,
        result: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Record an honest terminal failure; never records success."""
        payload = dict(result or {})
        if payload.get("history_status") == "complete":
            payload = {
                "history_status": "failed",
                "capture_phase": "after_failed",
                "error": str(reason or "root journal abandoned"),
            }
        return self._close(
            _ROOT_JOURNAL_ABANDONED,
            result=payload
            or {"history_status": "failed", "capture_phase": "after_failed"},
            reason=str(reason or "root journal abandoned"),
        )

    @_journal_locked
    def claim_recovery(self, writer_owner: WriterOwner) -> dict[str, Any]:
        """Fence a confirmed-dead worker before scheduler terminalization."""
        state = self.read()
        if state is None or str(state.get("phase") or "") != _ROOT_JOURNAL_OPEN:
            raise RootJournalError("root journal is not open for recovery")
        current = WriterOwner.from_mapping(state.get("writer_owner"))
        if current.identity() != writer_owner.identity():
            raise RootJournalError("root journal writer-owner mismatch")
        recovered = self._append_handoff(state, current, recovery=True)
        self._claim(recovered, current)
        return recovered

    def _claim(self, state: Mapping[str, Any], owner: WriterOwner) -> None:
        self._lease = int(state.get("generation") or 0)
        self._owner = owner

    def _require_current_lease(
        self, state: Mapping[str, Any], owner: WriterOwner
    ) -> None:
        if self._lease is None or self._owner is None:
            raise RootJournalError("root journal handoff requires the current owner lease")
        if int(state.get("generation") or 0) != self._lease:
            raise RootJournalError("root journal lease is stale")
        if self._owner.identity() != owner.identity():
            raise RootJournalError("root journal owner changed")

    def _append_handoff(
        self,
        state: Mapping[str, Any],
        writer_owner: WriterOwner,
        *,
        recovery: bool = False,
    ) -> dict[str, Any]:
        generation = int(state.get("generation") or 0) + 1
        updated = dict(state)
        updated["writer_owner"] = writer_owner.to_mapping()
        updated["generation"] = generation
        updated["handoffs"] = list(state.get("handoffs") or []) + [
            {
                "generation": generation,
                "at": time.time(),
                "writer_owner": writer_owner.to_mapping(),
                "recovery": recovery,
            }
        ]
        updated["updated_at"] = time.time()
        self._write(updated)
        return updated

    def _close(
        self,
        phase: str,
        *,
        result: Mapping[str, Any],
        reason: str | None,
    ) -> dict[str, Any]:
        existing = self.read()
        if existing is None:
            # Never opened (bg_jobs sets root_journal_path unconditionally):
            # do not mint a phantom terminal journal on disk.
            return {
                "version": _ROOT_JOURNAL_VERSION,
                "action_id": "",
                "roots": [],
                "writer_owner": {},
                "phase": phase,
                "generation": 0,
                "handoffs": [],
                "result": dict(result),
                "reason": reason,
                "created_at": time.time(),
                "updated_at": time.time(),
            }
        if self._lease is None or self._owner is None:
            raise RootJournalError("root journal terminalization requires an owner lease")
        if int(existing.get("generation") or 0) != self._lease:
            raise RootJournalError("root journal lease is stale")
        current = WriterOwner.from_mapping(existing.get("writer_owner"))
        if current.identity() != self._owner.identity():
            raise RootJournalError("root journal owner changed")
        if str(existing.get("phase") or "") in _ROOT_JOURNAL_TERMINAL:
            return dict(existing)
        closed = dict(existing)
        closed["phase"] = phase
        closed["result"] = dict(result)
        closed["reason"] = reason
        closed["updated_at"] = time.time()
        self._write(closed)
        return closed

    def _write(self, state: Mapping[str, Any]) -> None:
        # Lazy: core.atomic_io imports this module for history-completion checks.
        from core.atomic_io import atomic_write_json

        atomic_write_json(self.path, dict(state), indent=2)
