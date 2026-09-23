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
import hashlib
import json
import os
import pathlib
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Iterable, Iterator, Mapping

from src.openclank.history_client import HistoryClient


_ACTIVE_ACTION: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "openclank_history_action", default=None
)
_STATUS: contextvars.ContextVar[dict[str, Any]] = contextvars.ContextVar(
    "openclank_history_status",
    default={"history_status": "unconfigured", "capture_phase": "unavailable"},
)


@dataclass
class HistoryContext:
    """Authenticated, request-scoped identity for one mutation owner."""

    actor_id: str
    account_id: str
    workspace_id: str
    socket_path: str = ""
    token: str = ""
    actor_kind: str = "agent"
    roots: tuple[str, ...] = ()
    root_bindings: tuple[tuple[str, str], ...] = ()
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
    )


def _fingerprint(data: bytes | None) -> str:
    if data is None:
        return "missing"
    return f"sha256:{hashlib.sha256(data).hexdigest()}:{len(data)}"


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
        "session_id": None,
        "run_id": None,
        "task_id": None,
        "tool_id": "filesystem",
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


def _directory_manifest(path: str) -> bytes:
    """Return a deterministic recursive directory preimage.

    Directory metadata alone cannot restore a deleted tree. The manifest binds
    every relative entry, type, mode, link target, size, and file digest to the
    captured bytes, while keeping the provider locator out of the payload.
    """
    root = os.path.realpath(path)
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
                })
            entries.append(record)
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
) -> tuple[list[dict[str, Any]], bytes | None]:
    """Read every declared before-state for a recoverable parent mutation."""
    targets = [path, *paths]
    resource_keys = [envelope["resource_key"], *envelope.get("modified_resource_ids", [])[1:]]
    entries: list[dict[str, Any]] = []
    primary_before: bytes | None = None
    for index, target in enumerate(targets):
        try:
            stat = os.lstat(target)
        except FileNotFoundError:
            before = None
            existence = "Absent"
            resource_type = "File"
            mode = size = modified = None
            exact = True
        except OSError as exc:
            raise ValueError(f"cannot inspect batch preimage {target}: {exc}") from exc
        else:
            existence = "Present"
            mode = stat.st_mode & 0o7777
            size = stat.st_size
            modified = int(stat.st_mtime_ns // 1_000_000)
            if os.path.islink(target):
                resource_type = "Symlink"
                before = os.readlink(target).encode("utf-8", "surrogateescape")
                exact = True
            elif os.path.isdir(target):
                resource_type = "Directory"
                before = _directory_manifest(target)
                exact = True
            else:
                resource_type = "File"
                before = pathlib.Path(target).read_bytes()
                exact = True
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
            "content_digest": f"sha256:{hashlib.sha256(before).hexdigest()}:{len(before)}" if before is not None else None,
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
                    "size": size,
                    "modified_millis": modified,
                    "opaque": {"manifest_digest": coverage["content_digest"]} if resource_type == "Directory" else None,
                },
                "content": before,
                "fingerprint": _fingerprint(before),
                "coverage": coverage,
            }
        )
    return entries, primary_before
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
        if self.client is None or self.status != "prepared":
            result = {
                "action_id": self.action_id,
                "history_status": self.status,
                "capture_phase": self.capture_phase,
                "error": self.error,
            }
            _set_status(self.context, **result)
            return result
        try:
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
                            if os.path.isdir(target) and not os.path.islink(target):
                                target_after = _directory_manifest(target)
                            else:
                                target_after = pathlib.Path(target).read_bytes()
                        except FileNotFoundError:
                            target_after = None
                    item = dict(before_entry)
                    item.pop("old_locator", None)
                    item["locator"] = before_entry.get("new_locator")
                    item["content"] = target_after
                    item["existence"] = "Present" if target_after is not None else "Absent"
                    item["fingerprint"] = _fingerprint(target_after)
                    item["outcome"] = {
                        "resource_id": before_entry["resource_key"]["resource_id"],
                        "status": "Committed",
                        "revision": {"Opaque": {"kind": "fingerprint", "value": item["fingerprint"]}},
                    }
                    coverage = dict(before_entry.get("coverage") or {})
                    coverage["byte_len"] = len(target_after) if target_after is not None else 0
                    coverage["content_digest"] = (
                        f"sha256:{hashlib.sha256(target_after).hexdigest()}:{len(target_after)}"
                        if target_after is not None else None
                    )
                    item["coverage"] = coverage
                    after_entries.append(item)
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
        return result

    def abort(self) -> dict[str, Any]:
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
) -> CaptureHandle:
    """Prepare one file mutation, or return an honest unavailable handle."""
    action_id = action_id or _ACTIVE_ACTION.get() or f"file-{uuid.uuid4().hex}"
    paths = list(paths)
    all_paths = [path, *paths]
    if context is None:
        handle = CaptureHandle(action_id, None, {}, None, None, "paused", "unavailable", "no authenticated capture context")
        _set_status(action_id=action_id, history_status="paused", capture_phase="unavailable", error=handle.error)
        return handle
    excluded = next((item for item in all_paths if _excluded_secret(item)), None)
    outside = next((item for item in all_paths if not context.allowed(item)), None)
    if excluded is not None or outside is not None:
        reason = "secret path excluded" if excluded is not None else "path is outside the authorized history root"
        handle = CaptureHandle(action_id, None, {}, None, context, "paused", "excluded", reason)
        _set_status(context, action_id=action_id, history_status="paused", capture_phase="excluded", error=reason)
        return handle
    try:
        if os.path.isdir(path) and not os.path.islink(path):
            before = _directory_manifest(path)
        elif os.path.islink(path):
            before = os.readlink(path).encode("utf-8", "surrogateescape")
        else:
            before = pathlib.Path(path).read_bytes()
    except FileNotFoundError:
        before = None
    except OSError as exc:
        before = None
        handle = CaptureHandle(action_id, None, {}, before, context, "failed", "before_failed", str(exc))
        _set_status(context, action_id=action_id, history_status="failed", capture_phase="before_failed", error=str(exc))
        return handle
    client = _configured_client(context)
    if client is None:
        handle = CaptureHandle(action_id, None, {}, before, context, "paused", "unavailable", "history worker is not configured")
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
            )
            batch_prepare(envelope, entries)
            batch_entries = entries
        else:
            client.prepare(envelope, content=before, fingerprint=_fingerprint(before))
            batch_entries = None
    except Exception as exc:
        paused = "history_paused_budget" in str(exc)
        status = "paused" if paused else "failed"
        phase = "budget" if paused else "before_failed"
        handle = CaptureHandle(action_id, client, envelope, before, context, status, phase, str(exc))
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
        after = pathlib.Path(path).read_bytes()
    except FileNotFoundError:
        after = None
    except OSError as exc:
        after = None
        read_error = str(exc)
        handle.error = read_error
    try:
        return handle.finish(committed=committed, after=after, after_read_error=read_error)
    finally:
        handle.close()
