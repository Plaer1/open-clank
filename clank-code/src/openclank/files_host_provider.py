"""Rust-backed Host provider for the canonical Files facade.

This compatibility adapter moves Host metadata behind opaque ResourceRefs now.
It never walks, stats, or opens the host filesystem in Python. Every target is
resolved from an encrypted origin and re-authorized by the existing Rust app
lane. Descriptor handles/Tonic remain the production content replacement.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import mimetypes
import os
import re
import sys
import tempfile
import time
import uuid
from contextlib import nullcontext
from dataclasses import replace
from pathlib import Path
from typing import Any, Callable, Mapping

from src.openclank.files_facade import (
    FilesFacadeError,
    ProviderContent,
    ProviderContext,
    ProviderPage,
    ProviderResource,
    ProviderWorkspaceTarget,
    SORT_KEYS,
)
from src.openclank.files_service_client import FilesServiceError, client_for_owner
from src.openclank.filesystem_registry import FilesystemRegistryError, FilesystemRootRegistry
from src.openclank.host_apps import host_apps, HostAppsError
from routes.odysseus_files_routes import project_navigation_roots
from src.openclank.resource_refs import issue_resource_ref, stable_resource_id


_ORIGIN_PREFIX = "host:v1:"
_LOG = logging.getLogger(__name__)


def _native_path_identity(path: str) -> str:
    path = str(path)
    if sys.platform == "win32":
        # Rust canonical stat/list paths are verbatim; registry anchors are DOS.
        # Keep native verbatim I/O semantics and fold only their prefix aliases,
        # never filename case or distinct canonical resources.
        if re.match(r"^[A-Za-z]:\\", path):
            path = "\\\\?\\" + path[0].upper() + path[1:]
        elif re.match(r"^\\\\\?\\[A-Za-z]:\\", path):
            path = path[:4] + path[4].upper() + path[5:]
        elif path.startswith("\\\\") and not path.startswith(("\\\\?\\", "\\\\.\\")):
            path = "\\\\?\\UNC\\" + path[2:]
    return path


def _origin(path: str) -> str:
    payload = json.dumps({"path": _native_path_identity(path)}, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return _ORIGIN_PREFIX + base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def host_origin_for_path(path: str) -> str:
    """Derive the same Host origin used by Files from a trusted server path.

    This is an identity comparison helper only. Callers must still resolve the
    path through ``HostFilesProvider.resource_for_path`` before authorizing it.
    """
    return _origin(os.path.realpath(str(path)))


def _path(origin_id: str) -> str:
    raw = str(origin_id or "")
    if not raw.startswith(_ORIGIN_PREFIX):
        raise FilesFacadeError("Host resource is unavailable", code="resource_unavailable")
    token = raw[len(_ORIGIN_PREFIX):]
    try:
        decoded = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))
        value = json.loads(decoded.decode("utf-8"))
        path = str(value["path"])
    except (KeyError, TypeError, ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FilesFacadeError("Host resource is unavailable", code="resource_unavailable") from exc
    if not path or "\x00" in path or not Path(path).is_absolute():
        raise FilesFacadeError("Host resource is unavailable", code="resource_unavailable")
    return path


_CROSS_DISK_CODE = "managed_cross_filesystem_unsupported"
_CROSS_DISK_MESSAGE = ("This move crosses disks. Moving a document with its media between disks is not supported yet; "
                       "nothing was changed.")


def _device_of(path: str) -> int:
    """Device of a directory, or of its nearest existing ancestor (symlinks followed, as stat does)."""
    while not os.path.isdir(path) and os.path.dirname(path) != path:
        path = os.path.dirname(path)
    return os.stat(path).st_dev


def _is_cross_disk(error: FilesServiceError) -> bool:
    # The native refusal carries the same string as message and details.code.
    return error.code == "unsupported" and str(error) == _CROSS_DISK_CODE


def _service_error(error: FilesServiceError) -> FilesFacadeError:
    if _is_cross_disk(error):
        return FilesFacadeError(_CROSS_DISK_MESSAGE, code=_CROSS_DISK_CODE)
    code = {
        "denied": "resource_unavailable",
        "invalid_path": "resource_unavailable",
        "policy_generation_changed": "resource_ref_stale",
        "conflict": "resource_changed",
        "stale_cursor": "stale_cursor",
        "backpressure": "provider_unavailable",
    }.get(error.code, "provider_unavailable")
    return FilesFacadeError("Host resource is unavailable", code=code)


def _host_history_receipt(response: Mapping[str, Any]) -> dict[str, Any]:
    value = response.get('history')
    if not isinstance(value, Mapping):
        return {'state': 'unavailable'}
    action = value.get('action_id')
    if not isinstance(action, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', action):
        return {'state': 'unavailable'}
    complete = value.get('status') == 'complete' and value.get('phase') == 'complete' and value.get('durable') is True
    return {'state': 'complete' if complete else 'incomplete', 'actionId': action}


def _detected_mime(name: str, first_bytes: bytes) -> str:
    """Classify a small prefix while retaining extension detection provenance."""
    if first_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if first_bytes.startswith(b"%PDF-"):
        return "application/pdf"
    if first_bytes.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if first_bytes.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    return mimetypes.guess_type(str(name))[0] or "application/octet-stream"


# The canonical association hint is shared with the Files format projection.
from src.openclank.format_capabilities import EDITOR_MIME_TYPES, editor_text_hint


class HostFilesProvider:
    name = "host"
    _LOOSE_MANAGED_MOVE_READY = sys.platform == "linux"
    _MANAGED_FOLDER_MOVE_READY = sys.platform == "linux" and _LOOSE_MANAGED_MOVE_READY
    _MANAGED_CROSS_ROOT_FOLDER_MOVE_READY = sys.platform == "linux"  # Native two-root folder coordinator is Linux-only
    _LOOSE_MANAGED_RENAME_READY = sys.platform == "linux" and _LOOSE_MANAGED_MOVE_READY
    _ROOT_SORTS = ("name", "kind")
    _EDITOR_MIME_TYPES = EDITOR_MIME_TYPES

    async def _run_claimed_host_mutation(self, context: ProviderContext, paths: list[str], work: Callable[..., Any], *, preflight: Callable[[], Any] | None = None, delayed_start: bool = False, recovery: Mapping[str, Any] | None = None) -> Any:
        """Keep a cooperating writer claimed until its underlying task finishes."""
        claim_fn = getattr(self.operation_store, "claim_host_mutation", None)
        release_fn = getattr(self.operation_store, "release_host_mutation", None)
        if not callable(claim_fn) or not callable(release_fn):
            raise FilesFacadeError("Host mutation authority is unavailable", code="provider_unavailable")
        try:
            claim_args = dict(owner_subject_id=str(context.owner_subject_id), workspace_id=str(context.workspace_id),
                              generation=int(context.policy_generation), paths=paths)
            if recovery is not None:
                claim_args["recovery"] = recovery
            claim = claim_fn(**claim_args)
        except Exception as exc:
            raise FilesFacadeError("Host resource has an active mutation", code=getattr(exc, "code", "provider_unavailable")) from exc

        dispatched = False

        async def run() -> Any:
            nonlocal dispatched
            scope = self.mutation_scope(context) if self.mutation_scope is not None else nullcontext()
            async with scope:
                if preflight is not None:
                    await preflight()
                if delayed_start:
                    def begin_mutation() -> None:
                        nonlocal dispatched
                        dispatched = True
                    def known_no_effect() -> None:
                        nonlocal dispatched
                        dispatched = False
                    return await work(begin_mutation, known_no_effect)
                dispatched = True
                return await work()

        def release_after_success(task: asyncio.Task) -> bool:
            if task.cancelled():
                return False
            try:
                task.result()
            except FilesServiceError as exc:
                if dispatched and exc.code not in {"denied", "invalid_path", "malformed_request", "policy_generation_changed", "conflict"}:
                    return False
            except BaseException:
                # Without a definite successful response the underlying write
                # may have committed; retain the exact claim for recovery.
                if dispatched:
                    return False
            try:
                release_fn(owner_subject_id=str(context.owner_subject_id), **claim)
            except Exception:
                _LOG.exception("Host mutation claim requires reconciliation")
            return True

        try:
            task = asyncio.create_task(run())
        except BaseException:
            release_fn(owner_subject_id=str(context.owner_subject_id), **claim)
            raise
        try:
            result = await asyncio.shield(task)
        except asyncio.CancelledError:
            # Cancelling a coroutine does not cancel an in-flight worker-thread
            # RPC. Its own completion, not the caller's cancellation, retires
            # a successful claim.
            task.add_done_callback(release_after_success)
            raise
        except Exception as exc:
            # A transport failure can hide a successful write.
            if not release_after_success(task):
                error = FilesFacadeError("Host mutation outcome requires reconciliation", code="mutation_outcome_unknown")
                error.host_claim = claim
                raise error from exc
            raise
        release_after_success(task)
        return result

    async def operation_status(
        self,
        context: ProviderContext,
        *,
        operation_id: str,
        destination_origin_id: str | None = None,
        name: str | None = None,
        collision: str | None = None,
        digest: str | None = None,
        length: int | None = None,
        request_digest: str | None = None,
        destination_revision: Mapping[str, Any] | None = None,
    ) -> Mapping[str, Any] | None:
        """Reconstruct a committed import from the durable provider marker."""
        self._scope(context)
        getter = getattr(self.operation_store, "get_operation", None)
        parent = getter(owner_subject_id=str(context.owner_subject_id), operation_id=str(operation_id)) if callable(getter) else None
        parent_receipt = parent.get("receipt") if isinstance(parent, Mapping) else None
        managed = parent_receipt.get("_managed_items") if isinstance(parent_receipt, Mapping) else None
        if isinstance(managed, Mapping) and managed:
            expected = parent_receipt.get("_item_ids") or []
            previous = {str(value.get("item_id")): value for value in parent_receipt.get("items", []) if isinstance(value, Mapping)}
            result_items = []
            for key in expected:
                key = str(key)
                private = managed.get(key)
                if not isinstance(private, Mapping):
                    old = previous.get(key) or {"item_id": key, "outcome": "pending", "code": "operation_in_progress"}
                    result_items.append({field: old[field] for field in ("item_id", "outcome", "code", "resource_ref", "resource_key", "receipt_id", "revision", "history") if field in old})
                    continue
                phase = str(private.get("phase") or "")
                if parent_receipt.get("_managed_batch_ready") == "aborted":
                    outcome = {"outcome": "failed", "code": "resource_changed"}
                elif (private.get("plan") or {}).get("selected_folder") is not None and phase == "complete":
                    outcome = {"outcome": "committed"}
                elif (private.get("plan") or {}).get("selected_folder") is not None or (
                        (private.get("plan") or {}).get("destination_frame") is not None
                        and phase in {"dispatched", "native_committed", "metadata_pending"}):
                    outcome = await self._observe_selected_move(context, operation_id=operation_id, item_id=key, item=private)
                elif phase == "prepared" or parent_receipt.get("_managed_batch_ready") is False:
                    outcome = {"outcome": "pending", "code": "operation_in_progress"}
                else:
                    outcome = await self.execute_managed_workspace_item(context, operation_id=operation_id, item_id=key)
                item = {"item_id": key, "outcome": str(outcome.get("outcome") or "pending")}
                if outcome.get("code"):
                    item["code"] = str(outcome["code"])
                if item["outcome"] == "committed":
                    plan = private.get("plan") or {}
                    from src.openclank.file_policy import is_closed_managed_file_plan
                    if plan.get("kind") == "folder" or is_closed_managed_file_plan(plan):
                        item["cleanup"] = "complete" if private.get("cleanup_phase") == "complete" else "pending"
                    if ((is_closed_managed_file_plan(plan) or (plan.get("kind") == "folder" and self._MANAGED_CROSS_ROOT_FOLDER_MOVE_READY))
                            and plan.get("destination_frame") is not None and plan.get("selected_folder") is None
                            and not (plan.get("destination_root") or {}).get("selected_folder")):
                        undo_phase = private.get("undo_phase")
                        if undo_phase == "complete": item["undo"] = "committed"
                        elif undo_phase in {"claimed", "dispatched", "metadata_pending"}: item["undo"] = "pending"
                        elif undo_phase is None: item["undo"] = "available"
                    item["receipt_id"] = str(plan.get("native_operation_id") or "")
                    try:
                        resource = await self.stat(context, origin_id=_origin(str(plan["destination"])))
                        issued = issue_resource_ref(owner_subject_id=str(context.owner_subject_id), provider="host",
                            origin_id=resource.origin_id, kind=resource.kind, capabilities=resource.capabilities,
                            policy_generation=int(context.policy_generation), location_id=resource.location_id,
                            workspace_id=resource.workspace_id)
                        item["resource_ref"] = issued.token
                        item["resource_key"] = issued.stable_id
                        if isinstance(resource.revision, Mapping):
                            item["revision"] = dict(resource.revision)
                    except Exception:
                        item["code"] = "committed_resource_unavailable"
                result_items.append(item)
            if len(result_items) != len(expected):
                return None
            return {"state": "complete" if all(item.get("outcome") in {"committed", "unchanged"} for item in result_items) else "partial", "items": result_items}
        loaded = self._operation_load(context, operation_id)
        if not loaded:
            loaded = self._directory_operation_load(context, operation_id)
        if loaded and loaded.get("request_kind") == "create-directory":
            return await self._directory_status(context, loaded)
        if not loaded:
            return None
        if loaded.get("operation_id") != operation_id or loaded.get("generation") != int(context.policy_generation):
            return None
        if loaded.get("workspace_id", "default") != str(getattr(context, "workspace_id", "default") or "default"):
            return None
        if destination_origin_id is not None and loaded.get("destination") != _path(destination_origin_id):
            return None
        if name is not None and loaded.get("name") != name:
            return None
        if collision is not None and loaded.get("collision") != collision:
            return None
        if digest is not None and loaded.get("digest") != digest:
            return None
        if length is not None and loaded.get("length") != length:
            return None
        if request_digest is not None and loaded.get("request_digest") != request_digest:
            return None
        if destination_revision is not None and loaded.get("destination_revision") != dict(destination_revision):
            return None
        if loaded.get("phase") not in {"pending", "complete"}:
            return None
        if loaded.get("phase") == "pending":
            # A matching destination and ownership sidecar cannot prove which
            # native request committed, including for legacy markers without
            # an execution identity. Retain it until exact evidence exists.
            return None
        target = str(loaded.get("target") or "")
        item_id = str(loaded.get("item_id") or "")
        if not target or not item_id:
            return None
        try:
            created = await self.stat(context, origin_id=_origin(target))
        except Exception:
            return None
        if created.kind != "file" or (loaded.get("length") is not None and created.size is not None and int(created.size) != int(loaded["length"])):
            return None
        revision = dict(created.revision) if isinstance(created.revision, Mapping) else None
        before_kind = str(loaded.get("target_before_kind") or "")
        before_revision = loaded.get("target_before_revision")
        if before_kind and before_kind != "file":
            return None
        if before_kind == "file" and isinstance(before_revision, Mapping) and revision == dict(before_revision):
            # The target was already this exact file before the operation's
            # exclusive claim; stage_finish did not commit this operation.
            return None
        expected_digest = str(loaded.get("digest") or "")
        expected_value = expected_digest.removeprefix("sha256:") if expected_digest.startswith("sha256:") else ""
        if expected_value and (not revision or revision.get("kind") != "hostFingerprint" or revision.get("value") != expected_value):
            # A same-sized pre-existing or externally replaced file must not
            # be mistaken for the result of this lost operation.
            return None
        item = {
            "item_id": item_id, "outcome": "committed",
            "resource_key": stable_resource_id(owner_subject_id=context.owner_subject_id, provider=self.name, origin_id=created.origin_id),
            "resource_ref": issue_resource_ref(owner_subject_id=context.owner_subject_id, provider=self.name, origin_id=created.origin_id, kind=created.kind, capabilities=created.capabilities, policy_generation=context.policy_generation).token,
            "receipt_id": operation_id,
        }
        if revision:
            item["revision"] = revision
        item["provenance"] = {
            "domain": "host",
            "source_digest": expected_digest,
            "selected_name": loaded.get("selected_name") or Path(target).name,
            "declared_mime": loaded.get("declared_mime"),
            "detected_mime": loaded.get("detected_mime"),
        }
        item["history"] = {
            "receipt_id": operation_id,
            "status": "complete",
            "phase": "complete",
            "durable": True,
            "receipt": {
                "receipt_id": operation_id,
                "outcome": "committed",
                "selected_name": loaded.get("selected_name") or Path(target).name,
                "revision": revision,
            },
        }
        return {"state": "complete", "items": [item], "_request_digest": loaded.get("request_digest")}
    # Base snapshots are metadata-only and deliberately bounded.  The limit
    # is a safety ceiling, not an indication that the corpus is complete.
    _BASE_MAX_ROWS = 2_000
    _BASE_MAX_DEPTH = 32
    _BASE_MAX_PAGES_PER_FOLDER = 64

    def __init__(
        self,
        *,
        registry: FilesystemRootRegistry | None = None,
        client_factory: Callable[..., Any] = client_for_owner,
        operation_store: Any | None = None,
        mutation_scope: Callable[[ProviderContext], Any] | None = None,
    ) -> None:
        self.registry = registry or FilesystemRootRegistry()
        self.client_factory = client_factory
        self.operation_store = operation_store
        self.mutation_scope = mutation_scope

    @staticmethod
    def _operation_key(operation_id: str) -> str:
        return f"__host_import__{operation_id}"

    def _operation_load(self, context: ProviderContext, operation_id: str) -> dict[str, Any] | None:
        getter = getattr(self.operation_store, "get_operation", None)
        if not callable(getter):
            return None
        loaded = getter(owner_subject_id=context.owner_subject_id, operation_id=self._operation_key(operation_id))
        return dict(loaded.get("receipt") or {}) if isinstance(loaded, Mapping) else None

    def _operation_save(self, context: ProviderContext, operation_id: str, digest: str, marker: Mapping[str, Any]) -> None:
        recorder = getattr(self.operation_store, "record_operation", None)
        if callable(recorder):
            recorder(owner_subject_id=context.owner_subject_id, operation_id=self._operation_key(operation_id), request_digest=digest, generation=int(context.policy_generation), receipt=dict(marker))

    @staticmethod
    def _directory_operation_key(operation_id: str) -> str:
        return f"__host_directory__{operation_id}"

    def _directory_operation_load(self, context: ProviderContext, operation_id: str) -> dict[str, Any] | None:
        getter = getattr(self.operation_store, "get_operation", None)
        if not callable(getter):
            return None
        loaded = getter(owner_subject_id=context.owner_subject_id, operation_id=self._directory_operation_key(operation_id))
        return dict(loaded.get("receipt") or {}) if isinstance(loaded, Mapping) else None

    def _directory_operation_save(self, context: ProviderContext, operation_id: str, digest: str, marker: Mapping[str, Any]) -> None:
        recorder = getattr(self.operation_store, "record_operation", None)
        if callable(recorder):
            recorder(owner_subject_id=context.owner_subject_id, operation_id=self._directory_operation_key(operation_id), request_digest=digest, generation=int(context.policy_generation), receipt=dict(marker))

    async def _directory_status(self, context: ProviderContext, loaded: Mapping[str, Any]) -> Mapping[str, Any] | None:
        if loaded.get("generation") != int(context.policy_generation) or loaded.get("workspace_id", "default") != str(getattr(context, "workspace_id", "default") or "default"):
            return None
        if loaded.get("phase") not in {"pending", "complete"}:
            return None
        target = str(loaded.get("target") or "")
        parent = str(loaded.get("parent") or "")
        name = str(loaded.get("name") or "")
        if not target or not parent or not name or os.path.dirname(target) != parent or os.path.basename(target) != name:
            return None
        if loaded.get("phase") == "pending" and not self._ownership_proven(loaded):
            return None
        try:
            created = await self.stat(context, origin_id=_origin(target))
        except Exception:
            return None
        if created.kind != "folder" or created.parent_origin_id != _origin(parent):
            return None
        before_kind = str(loaded.get("target_before_kind") or "")
        if before_kind and loaded.get("outcome") != "unchanged":
            return None
        revision = dict(created.revision) if isinstance(created.revision, Mapping) else None
        if loaded.get("phase") == "pending":
            complete = {**dict(loaded), "phase": "complete", "revision": revision}
            try:
                self._directory_operation_save(context, str(loaded.get("operation_id") or ""), str(loaded.get("request_digest") or ""), complete)
                self._release_ownership(loaded)
            except Exception:
                pass
        item = {
            "item_id": str(loaded.get("item_id") or loaded.get("operation_id") or ""),
            "outcome": "committed",
            "resource_key": stable_resource_id(owner_subject_id=context.owner_subject_id, provider=self.name, origin_id=created.origin_id),
            "resource_ref": issue_resource_ref(owner_subject_id=context.owner_subject_id, provider=self.name, origin_id=created.origin_id, kind=created.kind, capabilities=created.capabilities, policy_generation=context.policy_generation).token,
            "receipt_id": str(loaded.get("operation_id") or ""),
        }
        if revision:
            item["revision"] = revision
        return {"state": "complete", "items": [item], "_request_digest": loaded.get("request_digest")}

    async def _directory_resource(self, context: ProviderContext, marker: Mapping[str, Any]) -> ProviderResource:
        target = str(marker.get("target") or "")
        created = await self.stat(context, origin_id=_origin(target))
        if created.kind != "folder" or created.parent_origin_id != _origin(str(marker.get("parent") or "")):
            raise FilesFacadeError("Host directory operation is unavailable", code="resource_changed")
        receipt_id = str(marker.get("operation_id") or uuid.uuid4().hex)
        outcome = "unchanged" if marker.get("outcome") == "unchanged" else "created"
        return replace(created, action_receipt={
            "action_id": receipt_id,
            "receipt_id": receipt_id,
            "status": "complete",
            "phase": "complete",
            "durable": True,
            "receipt": {"action_id": receipt_id, "receipt_id": receipt_id, "outcome": outcome},
        })

    def _import_marker(
        self,
        context: ProviderContext,
        *,
        operation_id: str,
        item_id: str,
        target: str,
        length: int,
        digest: str,
        stage: Mapping[str, Any],
        phase: str = "pending",
        revision: Mapping[str, Any] | None = None,
        request_digest: str | None = None,
        destination: str | None = None,
        destination_revision: Mapping[str, Any] | None = None,
        name: str | None = None,
        collision: str | None = None,
        selected_name: str | None = None,
        ownership_path: str | None = None,
        ownership_token: str | None = None,
        target_before_kind: str | None = None,
        target_before_revision: Mapping[str, Any] | None = None,
        native_operation_id: str | None = None,
    ) -> dict[str, Any]:
        """Build the private marker used to recover a committed stage.

        The absolute target is retained only in the provider's encrypted
        operation store.  It lets a fresh provider stat the exact destination
        after ``stage_finish`` succeeds but the facade ledger write is lost.
        """
        marker: dict[str, Any] = {
            "operation_id": str(operation_id),
            "item_id": str(item_id),
            "generation": int(context.policy_generation),
            "workspace_id": str(getattr(context, "workspace_id", "default") or "default"),
            "phase": str(phase),
            "native_operation_id": native_operation_id,
            "destination": str(destination or Path(target).parent),
            "target": str(target),
            "name": str(name or Path(target).name),
            "collision": str(collision or "fail"),
            "length": int(length),
            "digest": str(digest),
            "request_digest": str(request_digest or ""),
            "destination_revision": dict(destination_revision) if destination_revision is not None else None,
            "selected_name": str(selected_name or Path(target).name),
            "declared_mime": stage.get("declared_mime"),
            "detected_mime": stage.get("detected_mime"),
            "ownership_path": str(ownership_path or ""),
            "ownership_token": str(ownership_token or ""),
            "target_before_kind": str(target_before_kind or ""),
            "target_before_revision": dict(target_before_revision) if target_before_revision is not None else None,
        }
        if revision is not None:
            marker["revision"] = dict(revision)
        return marker

    @staticmethod
    def _ownership_sidecar(destination: str, operation_id: str, candidate: str, digest: str, *, owner_id: str = "", workspace_id: str = "default") -> str:
        key = hashlib.sha256(f"{owner_id}\x00{workspace_id}\x00{destination}\x00{operation_id}\x00{candidate}\x00{digest}".encode("utf-8")).hexdigest()[:40]
        root = Path(tempfile.gettempdir()) / ".openclank-host-import-owners-v1"
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        return str(root / f"{key}.owner")

    @staticmethod
    def _claim_ownership(path: str) -> tuple[str, str]:
        token = uuid.uuid4().hex
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        mode = 0o600
        descriptor = os.open(path, flags, mode)
        try:
            with os.fdopen(descriptor, "w", encoding="ascii") as handle:
                descriptor = -1
                handle.write(token)
                handle.flush()
                os.fsync(handle.fileno())
        finally:
            if descriptor >= 0:
                os.close(descriptor)
        return path, token

    @staticmethod
    def _release_ownership(marker: Mapping[str, Any]) -> None:
        path = str(marker.get("ownership_path") or "")
        if path:
            try:
                os.unlink(path)
            except FileNotFoundError:
                pass
            except OSError:
                pass

    @staticmethod
    def _ownership_proven(marker: Mapping[str, Any]) -> bool:
        path = str(marker.get("ownership_path") or "")
        token = str(marker.get("ownership_token") or "")
        if not path or not token:
            return False
        try:
            flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(path, flags)
            try:
                value = os.read(descriptor, 128).decode("ascii")
            finally:
                os.close(descriptor)
            return value == token
        except (OSError, UnicodeDecodeError):
            return False

    def _scope(self, context: ProviderContext) -> dict[str, Any]:
        try:
            scope = self.registry.app_scope(context.owner_username, is_admin=context.is_admin)
        except FilesystemRegistryError as exc:
            raise FilesFacadeError("Host policy is unavailable", code="provider_unavailable") from exc
        if scope.get("generation") != context.policy_generation:
            raise FilesFacadeError("File policy changed", code="policy_generation_changed")
        return scope

    async def _observe_selected_move(self, context: ProviderContext, *, operation_id: str, item_id: str,
                                     item: Mapping[str, Any]) -> Mapping[str, Any]:
        """Observe an exact completed native journal without resuming it."""
        plan = item.get("plan") or {}
        if item.get("phase") not in {"dispatched", "native_committed", "metadata_pending"}:
            return {"outcome": "pending", "code": "managed_needs_authority"}
        if plan.get("destination_frame") is not None:
            if plan.get("kind") == "folder":
                return {"outcome": "pending", "code": "managed_needs_authority"}
            return await self._observe_cross_move(context, operation_id=operation_id, item_id=item_id, item=item)
        try:
            owner = str(context.owner_subject_id)
            loaded = self.load_managed_workspace_item(context, operation_id=operation_id, item_id=item_id)
            if loaded != item:
                raise ValueError("managed item changed")
            chosen = dict(plan["managed_root"])
            chosen.pop("selected_folder", None)
            renewed = await self.authorize_managed_root(context, origin_id=_origin(str(chosen["root"])), expected_revision=None)
            if dict(renewed) != chosen:
                raise ValueError("Files root changed")
            payload = plan["payload"]
            if plan.get("kind") == "folder":
                status_operation = "managed_folder_move_status"
                paths = [payload["source"], payload["destination"]]
                paths += [part["path"] for part in payload["references"]]
                paths += [path for part in payload["assets"] for path in (part["path"], part["destination"])]
            else:
                status_operation = "managed_file_move_status"
                paths = [path for part in payload["participants"] for path in (part.get("source"), part.get("destination")) if path is not None]
                paths += [part["source"] for part in payload.get("guards", [])]
            scope = self._scope(context)
            if any(not self._path_writable(context, path, scope) for path in paths):
                raise ValueError("policy changed")
            client = self._client(context, dict(renewed["scope"]))
            capabilities = await client.request("capabilities", str(chosen["root"]), {})
            if status_operation not in (capabilities.get("operations") or []):
                raise ValueError("status capability unavailable")
            response = await client.request(status_operation, str(plan["source"]), dict(payload), root_operation_id=str(plan["native_operation_id"]))
            data = response.get("data") if isinstance(response, Mapping) else None
            if not isinstance(data, Mapping) or data.get("operation_id") != plan["native_operation_id"] or data.get("status") != "committed":
                return {"outcome": "pending", "code": "managed_reconciliation_required"}
            current_scope = self._scope(context)
            if (int(self.operation_store.generation()) != int(context.policy_generation)
                    or any(not self._path_writable(context, path, current_scope) for path in paths)
                    or dict(await self.authorize_managed_root(context, origin_id=_origin(str(chosen["root"])), expected_revision=None)) != chosen):
                raise ValueError("policy changed")
            token = str(item.get("claim_token") or "")
            if item.get("phase") == "dispatched":
                self.operation_store.mark_managed_transfer_item(owner_subject_id=owner, operation_id=operation_id,
                    item_id=item_id, native_operation_id=str(plan["native_operation_id"]), claim_token=token, phase="native_committed")
            if not self.operation_store.settle_managed_transfer_item(owner_subject_id=owner, operation_id=operation_id,
                    item_id=item_id, native_operation_id=str(plan["native_operation_id"]), claim_token=token):
                raise ValueError("managed settlement incomplete")
            return {"outcome": "committed"}
        except Exception:
            return {"outcome": "pending", "code": "managed_reconciliation_required"}

    async def _observe_cross_move(self, context: ProviderContext, *, operation_id: str, item_id: str,
                                  item: Mapping[str, Any]) -> Mapping[str, Any]:
        """Read-only status of a dispatched cross-root document move: renew BOTH recorded authorities, send the same
        `managed_file_move_status` the same-root branch sends, never move, resume or re-dispatch."""
        plan = item.get("plan") or {}
        owner, store = str(context.owner_subject_id), self.operation_store
        needs_authority = {"outcome": "pending", "code": "managed_needs_authority"}
        try:
            if self.load_managed_workspace_item(context, operation_id=operation_id, item_id=item_id) != item:
                raise ValueError("managed item changed")
            payload = plan["payload"]
            parts = payload["participants"]
            paths = [path for part in parts for path in (part.get("source"), part.get("destination")) if path is not None]
            paths += [part["source"] for part in payload.get("guards", [])]
        except Exception:
            return {"outcome": "pending", "code": "managed_reconciliation_required"}

        async def renew() -> dict[str, Any]:
            chosen = plan.get("managed_root")
            if chosen is not None:
                renewed = await self.authorize_managed_root(context, origin_id=_origin(str(chosen["root"])), expected_revision=None)
                frozen = dict(chosen); frozen.pop("selected_folder", None)
                if dict(renewed) != frozen:
                    raise ValueError("source root changed")
                source_scope = dict(renewed["scope"])
            else:
                workspace = store.get_workspace(str(context.workspace_id))
                location = store.get_location(str(plan["location_id"]))
                if (str(workspace.owner_subject_id) != owner or workspace.archived or workspace.revision != plan.get("workspace_revision")
                        or str(workspace.location_id) != str(location.id) or location.revision != plan.get("location_revision")
                        or not location.enabled or location.availability != "available"):
                    raise ValueError("source workspace changed")
                source_scope = self._scope(context)
            return await self._cross_root_scope(context, plan, owner=owner, source_scope=source_scope,
                                                source_narrowed=chosen is not None, phase=str(item.get("phase") or ""))

        try:
            scope = await renew()
        except Exception:
            return needs_authority
        try:
            ambient = self._scope(context)
            if any(not self._path_writable(context, path, ambient) for path in paths):
                raise ValueError("policy changed")
            client = self._client(context, scope)
            capabilities = await client.request("capabilities", str((plan.get("managed_root") or {}).get("root") or plan["root"]), {})
            if "managed_file_move_status" not in (capabilities.get("operations") or []):
                raise ValueError("status capability unavailable")
            response = await client.request("managed_file_move_status", str(plan["source"]), dict(payload),
                                            root_operation_id=str(plan["native_operation_id"]))
            data = response.get("data") if isinstance(response, Mapping) else None
            if not isinstance(data, Mapping) or data.get("operation_id") != plan["native_operation_id"] or data.get("status") != "committed":
                return {"outcome": "pending", "code": "managed_reconciliation_required"}
            if (int(store.generation()) != int(context.policy_generation)
                    or any(not self._path_writable(context, path, self._scope(context)) for path in paths)):
                raise ValueError("policy changed")
            try:
                await renew()
            except Exception:
                return needs_authority
            token = str(item.get("claim_token") or "")
            if item.get("phase") == "dispatched":
                store.mark_managed_transfer_item(owner_subject_id=owner, operation_id=operation_id, item_id=item_id,
                    native_operation_id=str(plan["native_operation_id"]), claim_token=token, phase="native_committed")
            if not store.settle_managed_transfer_item(owner_subject_id=owner, operation_id=operation_id, item_id=item_id,
                    native_operation_id=str(plan["native_operation_id"]), claim_token=token):
                raise ValueError("managed settlement incomplete")
            return {"outcome": "committed"}
        except Exception:
            return {"outcome": "pending", "code": "managed_reconciliation_required"}

    async def authorize_readonly_root(self, context: ProviderContext, *, origin_id: str,
                                      expected_revision: Mapping[str, Any] | None) -> Mapping[str, Any]:
        """Resolve one exact registered root under the current Files read policy."""
        path = _path(origin_id)
        scope = self._scope(context)
        entry = await self.stat(context, origin_id=origin_id)
        if (entry.origin_id != origin_id or not self._is_directory_kind(entry.kind)
                or "children" not in entry.capabilities
                or (expected_revision is not None and dict(expected_revision) != dict(entry.revision or {}))):
            raise FilesFacadeError("Selected root is unavailable", code="resource_unavailable")
        try:
            store = self.operation_store
            if int(store.generation()) != int(context.policy_generation):
                raise ValueError("policy changed")
            locations = [loc for loc in store.list_locations() if loc.kind == "directory"
                         and loc.enabled and loc.availability == "available"
                         and os.path.normpath(str(loc.canonical_path)) == path
                         and "read" in set(loc.capabilities)]
            if len(locations) != 1:
                raise ValueError("registered root unavailable")
            location = locations[0]
            root_id = str(location.id)
            if not scope.get("host") and (root_id not in set(scope.get("visible_root_ids") or [])
                    or "read" not in set((scope.get("root_capabilities") or {}).get(root_id) or [])):
                raise ValueError("selected root is not granted")
            if int(store.generation()) != int(context.policy_generation):
                raise ValueError("policy changed")
            return {"root_id": root_id, "root": path, "location_revision": location.revision,
                    "generation": int(context.policy_generation)}
        except Exception as exc:
            raise FilesFacadeError("Selected registered root is unavailable", code="resource_unavailable") from exc

    async def authorize_checkpoint_root(self, context: ProviderContext, *, origin_id: str,
                                        expected_revision: Mapping[str, Any]) -> Mapping[str, Any]:
        """Select one current registered read root without granting a new scope."""
        from src.openclank.files_service_client import history_binding_for
        path=_path(origin_id);scope=self._scope(context)
        entry=await self.stat(context,origin_id=origin_id)
        if (entry.origin_id!=origin_id or not self._is_directory_kind(entry.kind)
            or "children" not in entry.capabilities or not isinstance(expected_revision,Mapping)
            or dict(entry.revision or {})!=dict(expected_revision)):
            raise FilesFacadeError("Checkpoint root unavailable",code="resource_unavailable")
        try:
            store=self.operation_store
            if int(store.generation())!=int(context.policy_generation):raise ValueError("policy changed")
            locations=[loc for loc in store.list_locations() if loc.kind=="directory" and loc.enabled
                and loc.availability=="available" and os.path.normpath(str(loc.canonical_path))==path
                and "read" in set(loc.capabilities)]
            if len(locations)!=1:raise ValueError("root unavailable")
            location=locations[0];root_id=str(location.id)
            if not scope.get("host") and (root_id not in set(scope.get("visible_root_ids") or [])
                or "read" not in set((scope.get("root_capabilities") or {}).get(root_id) or [])):
                raise ValueError("root not granted")
            narrowed={"host":False,"visible_root_ids":[root_id],"capabilities":["read"],
                "root_capabilities":{root_id:["read"]},"generation":int(context.policy_generation),
                "active_folder":None,"selected_history_root_id":root_id}
            binding=history_binding_for(context.owner_username,"human",narrowed)
            roots=binding.get("root_bindings") if isinstance(binding,Mapping) else None
            if (not isinstance(roots,list) or len(roots)!=1 or str(binding.get("account_id"))!=str(context.owner_subject_id)
                or str(binding.get("workspace_id"))!=f"files:{root_id}" or str(binding.get("workspace_root"))!=path
                or str(roots[0].get("root_id"))!=root_id or str(roots[0].get("canonical_path"))!=path
                or int(store.generation())!=int(context.policy_generation)):
                raise ValueError("history root changed")
            return {"root_id":root_id,"root":path,"location_revision":location.revision,
                "generation":int(context.policy_generation),"scope":narrowed,"history_actor":str(binding.get("actor_id") or ""),
                "history_workspace":str(binding.get("workspace_id") or ""),"history_socket":str(binding.get("socket") or "")}
        except Exception as exc:
            raise FilesFacadeError("Checkpoint root unavailable",code="resource_unavailable") from exc

    async def capture_selected_checkpoint(self, context: ProviderContext, *, root_origin_id: str,
            root_revision: Mapping[str, Any], items: Sequence[Mapping[str, Any]], request_id: str,
            auth, barrier, history_supervisor, current, bind=None) -> Mapping[str, Any]:
        """Server-only selected capture, with original admission retained through settlement."""
        from src.openclank.files_service_client import OdysseusFilesClient, FilesServiceError, _checkpoint_owned_thread
        from src.openclank.history_client import HistoryClient, registered_resource_pages
        if (not re.fullmatch(r"[A-Za-z0-9_-]{16,80}",str(request_id or "")) or not 1<=len(items)<=16
            or not callable(current) or auth is None or barrier is None or history_supervisor is None):
            raise FilesFacadeError("Checkpoint unavailable",code="resource_unavailable")
        admitted=asyncio.Event()

        async def owned():
            admission=await barrier.acquire(context.owner_username);admitted.set()
            client=None;possible_issue=False;settled=False
            process=history_supervisor.process
            history=None
            async def valid():
                return bool(auth.account_id(context.owner_username)==context.owner_subject_id
                    and auth.username_for_account_id(context.owner_subject_id)==context.owner_username
                    and int(self.operation_store.generation())==int(context.policy_generation)
                    and process is not None and history_supervisor.process is process and process.poll() is None
                    and await barrier.is_live(context.owner_username,admission) and await current())
            async def wait_original_terminal():
                # Read-only status never authorizes a second issue. A live
                # worker with an unresolved action retains this admission.
                while True:
                    try:
                        if process is None or process.poll() is not None:return None
                        if history_supervisor.process is process and history is not None:
                            try:
                                status=await _checkpoint_owned_thread(lambda:history.selected_checkpoint_status(request_id))
                                if status.get("state") in {"complete","failed"}:return status
                            except Exception:pass
                        await asyncio.sleep(1)
                    except asyncio.CancelledError:
                        continue
            try:
                if not await valid():raise ValueError("account unavailable")
                root=dict(await self.authorize_checkpoint_root(context,origin_id=root_origin_id,expected_revision=root_revision))
                actor=root["history_actor"]
                if not actor or not await valid():raise ValueError("history unavailable")
                history=HistoryClient(root["history_socket"],actor_id=actor,account_id=context.owner_subject_id,
                    token=history_supervisor.credential_token(actor,context.owner_subject_id),timeout=1.5)
                paths=[];expected={}
                for item in items:
                    if type(item) is not dict or set(item)!={"origin_id","expected_revision"}:raise ValueError("selection unavailable")
                    path=_path(item["origin_id"]);revision=item["expected_revision"]
                    if (not isinstance(revision,Mapping) or set(revision)!={"kind","value"} or revision.get("kind")!="hostFingerprint"
                        or not isinstance(revision.get("value"),str) or not re.fullmatch(r"sha256:[0-9a-f]{64}:(?:0|[1-9][0-9]*)",revision["value"])
                        or path in expected or os.path.commonpath([root["root"],path])!=root["root"] or path==root["root"]):
                        raise ValueError("selection unavailable")
                    observed=await self.stat_inspection(context,origin_id=item["origin_id"])
                    if observed.origin_id!=item["origin_id"] or observed.kind!="file":raise ValueError("selection changed")
                    paths.append(path);expected[path]=revision["value"]
                found={}
                async def fetch(cursor):
                    return await _checkpoint_owned_thread(lambda:history.list_registered_resources(cursor=cursor,limit=100))
                async for page in registered_resource_pages(fetch):
                    for row in page["items"]:
                        if (row.get("account_id")!=context.owner_subject_id or row.get("workspace_id")!=root["history_workspace"]
                            or row.get("root_path")!=root["root"] or row.get("active") is not True):continue
                        relative=str(row.get("relative_path") or "")
                        candidate=os.path.normpath(os.path.join(root["root"],relative))
                        if (not relative or os.path.isabs(relative) or ".." in Path(relative).parts
                            or os.path.commonpath([root["root"],candidate])!=root["root"] or candidate not in expected):continue
                        if (candidate in found or row.get("root_id")!=root["root_id"]
                            or not isinstance(row.get("actor_ids"),list) or root["history_actor"] not in row["actor_ids"]):raise ValueError("ambiguous registration")
                        found[candidate]=str(row.get("resource_id") or "")
                if set(found)!=set(paths) or len(set(found.values()))!=len(paths) or any(not item.startswith("file:") for item in found.values()):
                    raise ValueError("registered selection unavailable")
                if not await valid():raise ValueError("account unavailable")
                if bind is not None:
                    await bind({"root_id":root["root_id"],"history_actor":actor,"history_workspace":root["history_workspace"],
                        "items":[{"resource_id":found[path],"fingerprint":expected[path]} for path in paths]})
                if not await valid():raise ValueError("account unavailable")
                client=OdysseusFilesClient(context.owner_username,app_scope=dict(root["scope"]),transport="framed")
                native_items=[{"path":path,"resource_id":found[path],"expected_fingerprint":expected[path]} for path in paths]
                possible_issue=True
                try:
                    prepared=await client.prepare_selected_checkpoint(request_id=request_id,root_id=root["root_id"],items=native_items)
                except FilesServiceError as error:
                    if error.code=="checkpoint_unsent":possible_issue=False
                    raise
                if (not await valid() or dict(await self.authorize_checkpoint_root(context,origin_id=root_origin_id,expected_revision=root_revision))!=root):
                    raise ValueError("authority changed")
                for item in items:
                    observed=await self.stat_inspection(context,origin_id=item["origin_id"])
                    if observed.origin_id!=item["origin_id"] or observed.kind!="file":raise ValueError("selection changed")
                if not await valid():raise ValueError("authority changed")
                response=await client.finalize_selected_checkpoint(prepared,target_path=paths[0]);data=response.get("data") or {}
                status=await _checkpoint_owned_thread(lambda:history.selected_checkpoint_status(request_id))
                if (data!=status or status.get("state")!="complete" or len(status.get("items") or [])!=len(paths)
                    or any(item.get("resource_id")!=found[path] or item.get("fingerprint")!=expected[path]
                        for item,path in zip(status["items"],paths))):raise ValueError("checkpoint outcome unavailable")
                settled=True
                if not await valid():raise ValueError("authority changed")
                return dict(status)
            except BaseException:
                if possible_issue and not settled:
                    await wait_original_terminal()
                raise
            finally:
                try:
                    if client is not None:await _checkpoint_owned_thread(client.close)
                finally:
                    await barrier.release(admission)

        task=asyncio.create_task(owned())
        try:
            return await asyncio.wait_for(asyncio.shield(task),timeout=35)
        except asyncio.TimeoutError:
            if not admitted.is_set():
                task.cancel()
                try:await task
                except BaseException:pass
                raise FilesFacadeError("Checkpoint unavailable",code="resource_unavailable")
            def consume(done):
                if not done.cancelled():
                    try:done.exception()
                    except BaseException:pass
            task.add_done_callback(consume)
            return {"state":"pending","request_id":request_id}
        except asyncio.CancelledError:
            if not admitted.is_set():task.cancel()
            def consume(done):
                if not done.cancelled():
                    try:done.exception()
                    except BaseException:pass
            task.add_done_callback(consume)
            raise

    async def read_selected_checkpoint_status(self, context: ProviderContext, *, root_origin_id: str, root_revision,
            items, request_id: str, binding, auth, barrier, history_supervisor, current):
        """Read the original action and separately assess the current mapping."""
        from src.openclank.files_service_client import _checkpoint_owned_thread
        from src.openclank.history_client import HistoryClient, registered_resource_pages
        async with barrier.admitted(context.owner_username):
            process=history_supervisor.process
            async def valid():
                return bool(auth.account_id(context.owner_username)==context.owner_subject_id
                    and auth.username_for_account_id(context.owner_subject_id)==context.owner_username
                    and self.operation_store.generation()==context.policy_generation and process is not None
                    and history_supervisor.process is process and process.poll() is None and await current())
            if not await valid() or not isinstance(binding,Mapping) or set(binding)!={"root_id","history_actor","history_workspace","items"}:
                raise ValueError("checkpoint unavailable")
            current_root=await self.stat(context,origin_id=root_origin_id)
            if current_root.origin_id!=root_origin_id or current_root.kind not in {"folder","directory"} or not current_root.revision:raise ValueError("root unavailable")
            fresh_root_revision=dict(current_root.revision)
            root=dict(await self.authorize_checkpoint_root(context,origin_id=root_origin_id,expected_revision=fresh_root_revision))
            if any(binding.get(k)!=root.get(k) for k in ("root_id","history_actor","history_workspace")) or not isinstance(binding.get("items"),list) or len(binding["items"])!=len(items):raise ValueError("checkpoint binding changed")
            paths=[]
            for item,saved in zip(items,binding["items"]):
                path=_path(item["origin_id"]);revision=item["expected_revision"]
                if (path in paths or os.path.commonpath([root["root"],path])!=root["root"] or path==root["root"]
                    or not isinstance(saved,Mapping) or set(saved)!={"resource_id","fingerprint"} or saved["fingerprint"]!=revision.get("value")):
                    raise ValueError("checkpoint binding changed")
                paths.append(path)
            history=HistoryClient(root["history_socket"],actor_id=root["history_actor"],account_id=context.owner_subject_id,
                token=history_supervisor.credential_token(root["history_actor"],context.owner_subject_id),timeout=1.5)
            status=await _checkpoint_owned_thread(lambda:history.selected_checkpoint_status(request_id))
            if status.get("state")=="complete" and (len(status.get("items") or [])!=len(items) or any(
                row.get("resource_id")!=saved["resource_id"] or row.get("fingerprint")!=saved["fingerprint"]
                for row,saved in zip(status["items"],binding["items"]))):raise ValueError("checkpoint receipt changed")
            matches={path:[] for path in paths}
            async def fetch(cursor):return await _checkpoint_owned_thread(lambda:history.list_registered_resources(cursor=cursor,limit=100))
            try:
                async for page in registered_resource_pages(fetch):
                    for row in page["items"]:
                        if row.get("account_id")!=context.owner_subject_id or row.get("workspace_id")!=root["history_workspace"] or row.get("root_path")!=root["root"] or row.get("active") is not True:continue
                        relative=str(row.get("relative_path") or "")
                        if not relative or os.path.isabs(relative) or ".." in Path(relative).parts:continue
                        path=os.path.normpath(os.path.join(root["root"],relative))
                        if path in matches:matches[path].append(row)
                mapping_current=all(len(matches[path])==1 and matches[path][0].get("resource_id")==saved["resource_id"]
                    and matches[path][0].get("root_id")==root["root_id"] and isinstance(matches[path][0].get("actor_ids"),list)
                    and root["history_actor"] in matches[path][0]["actor_ids"] for path,saved in zip(paths,binding["items"]))
            except Exception:mapping_current=False
            if not await valid() or dict(await self.authorize_checkpoint_root(context,origin_id=root_origin_id,expected_revision=fresh_root_revision))!=root:raise ValueError("authority changed")
            return status, mapping_current

    async def observe_selected_checkpoint_registration(self, context: ProviderContext, *, root_origin_id: str,
            root_revision: Mapping[str, Any], items, auth, barrier, history_supervisor, current):
        """Current unique registry tuples are observations, never a mutation receipt."""
        from src.openclank.files_service_client import _checkpoint_owned_thread
        from src.openclank.history_client import HistoryClient, registered_resource_pages
        async with barrier.admitted(context.owner_username):
            process=history_supervisor.process
            async def valid():
                return bool(auth.account_id(context.owner_username)==context.owner_subject_id
                    and auth.username_for_account_id(context.owner_subject_id)==context.owner_username
                    and self.operation_store.generation()==context.policy_generation
                    and process is not None and history_supervisor.process is process and process.poll() is None and await current())
            if not await valid():raise ValueError("authority unavailable")
            root=dict(await self.authorize_checkpoint_root(context,origin_id=root_origin_id,expected_revision=root_revision))
            paths=[]
            for item in items:
                path=_path(item["origin_id"])
                if path in paths or os.path.commonpath([root["root"],path])!=root["root"] or path==root["root"]:raise ValueError("selection unavailable")
                observed=await self.stat_checkpoint(context,origin_id=item["origin_id"])
                if observed.origin_id!=item["origin_id"] or observed.kind!="file" or dict(observed.revision or {})!=dict(item["expected_revision"]):raise ValueError("selection changed")
                paths.append(path)
            history=HistoryClient(root["history_socket"],actor_id=root["history_actor"],account_id=context.owner_subject_id,
                token=history_supervisor.credential_token(root["history_actor"],context.owner_subject_id),timeout=1.5)
            matches={path:[] for path in paths}
            async def fetch(cursor):return await _checkpoint_owned_thread(lambda:history.list_registered_resources(cursor=cursor,limit=100))
            async for page in registered_resource_pages(fetch):
                for row in page["items"]:
                    if row.get("account_id")!=context.owner_subject_id or row.get("workspace_id")!=root["history_workspace"] or row.get("root_path")!=root["root"] or row.get("active") is not True:continue
                    relative=str(row.get("relative_path") or "")
                    if not relative or os.path.isabs(relative) or ".." in Path(relative).parts:continue
                    path=os.path.normpath(os.path.join(root["root"],relative))
                    if path in matches:matches[path].append(row)
            if not await valid() or dict(await self.authorize_checkpoint_root(context,origin_id=root_origin_id,expected_revision=root_revision))!=root:raise ValueError("authority changed")
            for item in items:
                observed=await self.stat_checkpoint(context,origin_id=item["origin_id"])
                if observed.origin_id!=item["origin_id"] or observed.kind!="file" or dict(observed.revision or {})!=dict(item["expected_revision"]):raise ValueError("selection changed")
            if not await valid():raise ValueError("authority changed")
            return ["not_observed" if not matches[path] else "observed" if len(matches[path])==1 and matches[path][0].get("root_id")==root["root_id"] and isinstance(matches[path][0].get("actor_ids"),list) and root["history_actor"] in matches[path][0]["actor_ids"] and str(matches[path][0].get("resource_id") or "").startswith("file:") else "unavailable" for path in paths]

    async def register_selected_checkpoint_files(self, context: ProviderContext, *, root_origin_id: str,
            root_revision: Mapping[str, Any], items, auth, barrier, history_supervisor, current, update) -> Mapping[str, Any]:
        """Explicit registration owned by the original account task; never retry unknown RPCs."""
        from src.openclank.files_service_client import OdysseusFilesClient, _checkpoint_owned_thread
        from src.openclank.history_client import HistoryClient, registered_resource_pages
        if not 1 <= len(items) <= 16 or not callable(current) or not callable(update):
            raise FilesFacadeError("Registration unavailable", code="resource_unavailable")
        admitted=asyncio.Event()
        async def owned():
            admission=await barrier.acquire(context.owner_username); admitted.set()
            process=history_supervisor.process; client=None; claim=None; issued=False; results=[]
            async def valid():
                return bool(auth.account_id(context.owner_username)==context.owner_subject_id
                    and auth.username_for_account_id(context.owner_subject_id)==context.owner_username
                    and self.operation_store.generation()==context.policy_generation
                    and process is not None and history_supervisor.process is process and process.poll() is None
                    and await barrier.is_live(context.owner_username,admission) and await current())
            async def wait_exit():
                while process is not None and process.poll() is None:
                    try: await asyncio.sleep(1)
                    except asyncio.CancelledError: continue
            try:
                if not await valid(): raise ValueError("authority unavailable")
                root=dict(await self.authorize_checkpoint_root(context,origin_id=root_origin_id,expected_revision=root_revision))
                paths=[]
                for item in items:
                    path=_path(item["origin_id"]); revision=item["expected_revision"]
                    if (path in paths or os.path.commonpath([root["root"],path])!=root["root"] or path==root["root"]
                        or not isinstance(revision,Mapping) or set(revision)!={"kind","value"} or revision.get("kind")!="hostFingerprint"
                        or not isinstance(revision.get("value"),str) or not re.fullmatch(r"sha256:[0-9a-f]{64}:(?:0|[1-9][0-9]*)",revision["value"])):
                        raise ValueError("selection unavailable")
                    paths.append(path)
                claim=self.operation_store.claim_host_mutation(owner_subject_id=context.owner_subject_id,workspace_id=context.workspace_id,
                    generation=context.policy_generation,paths=paths)
                history=HistoryClient(root["history_socket"],actor_id=root["history_actor"],account_id=context.owner_subject_id,
                    token=history_supervisor.credential_token(root["history_actor"],context.owner_subject_id),timeout=1.5)
                async def lookup():
                    matches={path:[] for path in paths}
                    async def fetch(cursor):return await _checkpoint_owned_thread(lambda:history.list_registered_resources(cursor=cursor,limit=100))
                    async for page in registered_resource_pages(fetch):
                        for row in page["items"]:
                            if row.get("account_id")!=context.owner_subject_id or row.get("workspace_id")!=root["history_workspace"] or row.get("root_path")!=root["root"] or row.get("active") is not True:continue
                            relative=str(row.get("relative_path") or "")
                            if not relative or os.path.isabs(relative) or ".." in Path(relative).parts:continue
                            path=os.path.normpath(os.path.join(root["root"],relative))
                            if path in matches:matches[path].append(row)
                    return matches
                matches=await lookup()
                if not await valid(): raise ValueError("authority changed")
                client=OdysseusFilesClient(context.owner_username,app_scope=dict(root["scope"]),transport="framed")
                for path,item in zip(paths,items):
                    if not await valid() or dict(await self.authorize_checkpoint_root(context,origin_id=root_origin_id,expected_revision=root_revision))!=root:raise ValueError("authority changed")
                    observed=await self.stat_checkpoint(context,origin_id=item["origin_id"])
                    if observed.origin_id!=item["origin_id"] or observed.kind!="file" or dict(observed.revision or {})!=dict(item["expected_revision"]):raise ValueError("selection changed")
                    rows=matches[path]
                    if len(rows)>1 or (rows and (rows[0].get("root_id")!=root["root_id"] or not isinstance(rows[0].get("actor_ids"),list) or root["history_actor"] not in rows[0]["actor_ids"] or not str(rows[0].get("resource_id") or "").startswith("file:"))):
                        results.append({"state":"unavailable"});await update(results);continue
                    if rows:
                        results.append({"state":"observed"});await update(results);continue
                    issued=True
                    request=client._request_envelope("register_checkpoint_file",path=path,payload={"root_id":root["root_id"],"expected_fingerprint":item["expected_revision"]["value"]})
                    response=await _checkpoint_owned_thread(lambda:client._request_sync(request))
                    data=response.get("data") if isinstance(response,Mapping) else None
                    if not isinstance(data,Mapping) or set(data)!={"identity","created"} or type(data.get("created")) is not bool or not isinstance(data.get("identity"),Mapping):
                        raise ValueError("registration receipt unavailable")
                    identity=data["identity"]
                    relative=os.path.relpath(path,root["root"]).replace(os.sep,"/")
                    if (set(identity)!={"resource_id","generation","root_id","root_path","relative_path"}
                        or identity.get("root_id")!=root["root_id"] or identity.get("root_path")!=root["root"]
                        or identity.get("relative_path")!=relative or type(identity.get("generation")) is not int or identity["generation"]<1
                        or not str(identity.get("resource_id") or "").startswith("file:")):
                        raise ValueError("registration receipt changed")
                    matches=await lookup(); rows=matches[path]
                    if (len(rows)!=1 or rows[0].get("root_id")!=root["root_id"] or rows[0].get("resource_id")!=identity["resource_id"]
                        or rows[0].get("generation")!=identity["generation"] or not isinstance(rows[0].get("actor_ids"),list)
                        or root["history_actor"] not in rows[0]["actor_ids"]):
                        raise ValueError("registration is ambiguous")
                    issued=False
                    results.append({"state":"registered" if data["created"] else "observed"});await update(results)
                    if not await valid() or dict(await self.authorize_checkpoint_root(context,origin_id=root_origin_id,expected_revision=root_revision))!=root:
                        await update(results,state="partial")
                        return {"state":"partial","items":results,"current":False}
                    observed=await self.stat_checkpoint(context,origin_id=item["origin_id"])
                    if observed.origin_id!=item["origin_id"] or observed.kind!="file" or dict(observed.revision or {})!=dict(item["expected_revision"]):
                        await update(results,state="partial")
                        return {"state":"partial","items":results,"current":False}
                if not await valid() or dict(await self.authorize_checkpoint_root(context,origin_id=root_origin_id,expected_revision=root_revision))!=root:
                    await update(results,state="partial")
                    return {"state":"partial","items":results,"current":False}
                outcome="complete" if all(x["state"] in {"registered","observed"} for x in results) else "partial"
                await update(results, state=outcome)
                return {"state":outcome,"items":results}
            except BaseException:
                if issued:
                    await wait_exit()
                else:
                    try: await update(results, state="partial")
                    except BaseException: pass
                raise
            finally:
                if client is not None:
                    try: await _checkpoint_owned_thread(client.close)
                    except BaseException: pass
                if claim is not None:
                    try:self.operation_store.release_host_mutation(owner_subject_id=context.owner_subject_id,**claim)
                    except Exception:pass
                await barrier.release(admission)
        task=asyncio.create_task(owned())
        try:return await asyncio.wait_for(asyncio.shield(task),timeout=35)
        except asyncio.TimeoutError:
            if not admitted.is_set():task.cancel()
            task.add_done_callback(lambda done: done.exception() if not done.cancelled() else None)
            return {"state":"pending"}
        except asyncio.CancelledError:
            if not admitted.is_set():task.cancel()
            task.add_done_callback(lambda done: done.exception() if not done.cancelled() else None)
            raise

    async def authorize_managed_root(self, context: ProviderContext, *, origin_id: str,
                                     expected_revision: Mapping[str, Any] | None) -> Mapping[str, Any]:
        """Resolve an explicit folder to one existing root without granting access."""
        from src.openclank.files_service_client import history_binding_for
        path = _path(origin_id)
        scope = self._scope(context)
        entry = await self.stat(context, origin_id=origin_id)
        if (entry.origin_id != origin_id or not self._is_directory_kind(entry.kind)
                or "write" not in entry.capabilities or not self._path_writable(context, path, scope)
                or (expected_revision is not None and dict(expected_revision) != dict(entry.revision or {}))):
            raise FilesFacadeError("Selected root is unavailable", code="resource_unavailable")
        try:
            store = self.operation_store
            if int(store.generation()) != int(context.policy_generation):
                raise ValueError("policy changed")
            locations = [loc for loc in store.list_locations() if loc.kind == "directory"
                         and loc.enabled and loc.availability == "available"
                         and os.path.normpath(str(loc.canonical_path)) == path
                         and {"read", "write"}.issubset(set(loc.capabilities))]
            if len(locations) != 1:
                raise ValueError("registered root unavailable")
            location = locations[0]
            root_id = str(location.id)
            if not scope.get("host") and (root_id not in set(scope.get("visible_root_ids") or [])
                    or not {"read", "write"}.issubset(set((scope.get("root_capabilities") or {}).get(root_id) or []))):
                raise ValueError("selected root is not granted")
            narrowed = {"host": False, "visible_root_ids": [root_id], "capabilities": ["read", "write"],
                        "root_capabilities": {root_id: ["read", "write"]},
                        "generation": int(context.policy_generation), "active_folder": None,
                        "selected_history_root_id": root_id}
            binding = history_binding_for(context.owner_username, "human", narrowed)
            roots = binding.get("root_bindings") if isinstance(binding, Mapping) else None
            if (not isinstance(roots, list) or len(roots) != 1
                    or str(binding.get("account_id")) != str(context.owner_subject_id)
                    or str(binding.get("workspace_id")) != f"files:{root_id}"
                    or str(binding.get("workspace_root")) != path
                    or str(roots[0].get("root_id")) != root_id
                    or str(roots[0].get("canonical_path")) != path):
                raise ValueError("History root changed")
            if int(store.generation()) != int(context.policy_generation):
                raise ValueError("policy changed")
            return {"root_id": root_id, "root": path, "location_revision": location.revision,
                    "generation": int(context.policy_generation), "scope": narrowed}
        except Exception as exc:
            raise FilesFacadeError("Selected registered root is unavailable", code="resource_unavailable") from exc

    async def authorize_selected_folder(self, context: ProviderContext, *, origin_id: str,
                                        expected_revision: Mapping[str, Any] | None) -> Mapping[str, Any]:
        """Bind a current selected folder separately from its Files/History root."""
        if not isinstance(expected_revision, Mapping) or not expected_revision:
            raise FilesFacadeError("Selected folder revision is required", code="invalid_resource_request")
        path = _path(origin_id)
        entry = await self.stat(context, origin_id=origin_id)
        if (entry.origin_id != origin_id or not self._is_directory_kind(entry.kind)
                or not {"children", "write"}.issubset(set(entry.capabilities))
                or dict(entry.revision or {}) != dict(expected_revision)
                or os.path.realpath(path) != os.path.abspath(path)):
            raise FilesFacadeError("Selected folder changed", code="resource_changed")
        candidates = []
        for location in self.operation_store.list_locations():
            root = os.path.normpath(str(location.canonical_path))
            try:
                contained = os.path.commonpath([root, path]) == root
            except ValueError:
                contained = False
            if contained and location.kind == "directory" and location.enabled and location.availability == "available":
                candidates.append(root)
        if not candidates:
            raise FilesFacadeError("Selected folder has no Files root", code="resource_unavailable")
        longest = max(map(len, candidates))
        roots = [root for root in candidates if len(root) == longest]
        if len(roots) != 1:
            raise FilesFacadeError("Selected folder root is ambiguous", code="resource_unavailable")
        binding = dict(await self.authorize_managed_root(context, origin_id=_origin(roots[0]), expected_revision=None))
        binding["selected_folder"] = {"path": path, "revision": dict(expected_revision)}
        return binding

    def _validate_selected_move_paths(self, context: ProviderContext, root: str, paths: Sequence[str]) -> None:
        from src.openclank.media_attachment_targets import HostDocumentAttachmentTarget, _assert_workspace_media_path, _path_within
        roots = HostDocumentAttachmentTarget(provider=self, operation_store=self.operation_store)._selected_roots(context.owner_subject_id)
        for path in paths:
            _assert_workspace_media_path(root, path)
            if any(_path_within(record.canonical_root, path) for record in roots):
                raise FilesFacadeError("Selected move overlaps a registered workspace", code="invalid_destination")

    async def inspect_managed_folder(self, context: ProviderContext, *, origin_id: str,
                                     managed_root: Mapping[str, Any]) -> Mapping[str, Any]:
        """Read an exact bounded native subtree proof under the chosen root."""
        witness = managed_root.get("selected_folder")
        root = str(witness.get("path") if isinstance(witness, Mapping) else managed_root.get("root") or "")
        source = _path(origin_id)
        if not root or source == root or os.path.commonpath([root, source]) != root:
            raise FilesFacadeError("Folder is outside the selected root", code="resource_unavailable")
        renewed = (await self.authorize_selected_folder(context, origin_id=_origin(root), expected_revision=witness["revision"])
                   if isinstance(witness, Mapping) else await self.authorize_managed_root(context, origin_id=_origin(root), expected_revision=None))
        if dict(renewed) != dict(managed_root):
            raise FilesFacadeError("Selected root changed", code="resource_changed")
        entry = await self.stat(context, origin_id=origin_id)
        if entry.origin_id != origin_id or not self._is_directory_kind(entry.kind) or "write" not in entry.capabilities:
            raise FilesFacadeError("Folder is unavailable", code="resource_unavailable")
        try:
            response = await self._client(context, dict(renewed["scope"])).request("managed_folder_inspect", source, {})
            proof = response.get("data")
            if (not isinstance(proof, Mapping) or set(proof) != {"digest", "count", "bytes"}
                    or not isinstance(proof.get("digest"), str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", proof["digest"])
                    or type(proof.get("count")) is not int or not 0 <= proof["count"] < 128
                    or type(proof.get("bytes")) is not int or not 0 <= proof["bytes"] <= 64 * 1024 * 1024):
                raise ValueError("folder proof is invalid")
            latest = (await self.authorize_selected_folder(context, origin_id=_origin(root), expected_revision=witness["revision"])
                      if isinstance(witness, Mapping) else await self.authorize_managed_root(context, origin_id=_origin(root), expected_revision=None))
            if dict(latest) != dict(managed_root):
                raise ValueError("selected root changed")
            return dict(proof)
        except FilesServiceError as exc:
            if exc.code == "unsupported":
                raise FilesFacadeError("Folder contains unsupported entries", code="unsupported_operation") from exc
            if exc.code == "backpressure":
                raise FilesFacadeError("Folder exceeds the supported move bound", code="resource_unavailable") from exc
            raise _service_error(exc) from exc
        except (ValueError, KeyError, TypeError) as exc:
            raise FilesFacadeError("Folder inspection is unavailable", code="resource_unavailable") from exc

    def _client(self, context: ProviderContext, scope: dict[str, Any]):
        if self.client_factory is client_for_owner:
            return self.client_factory(
                context.owner_username,
                app_scope=scope,
                transport="grpc" if sys.platform in {"darwin", "linux", "win32"} else "framed",
            )
        return self.client_factory(context.owner_username) if scope.get("host") else self.client_factory(
            context.owner_username, app_scope=scope,
        )

    def _stream_client(self, context: ProviderContext, scope: dict[str, Any]):
        if self.client_factory is client_for_owner:
            return self.client_factory(
                context.owner_username,
                app_scope=scope,
                transport="grpc" if sys.platform in {"darwin", "linux", "win32"} else "framed",
            )
        return self._client(context, scope)

    def supported_sort_keys(self, *, parent_origin_id: str) -> tuple[str, ...]:
        if parent_origin_id == "host:v1:root":
            return self._ROOT_SORTS
        _path(parent_origin_id)
        return SORT_KEYS

    @staticmethod
    def _caps(kind: str, *, writable: bool = False) -> tuple[str, ...]:
        # Capabilities describe facade operations that work today. Text files
        # can be opened through the same Rust read-lines service used by Code;
        # writes still require the caller's existing app-scoped service grant.
        if kind in {"folder", "directory", "recursive_directory"}:
            return ("children", "stat", "search", *(('watch',) if sys.platform in {"darwin", "linux", "win32"} else ()), *(('write',) if writable else ()))
        if kind == "file":
            # trash: cheap facts only (writable regular file, supported platform); _trash_action refuses media, trash folders and the root.
            return ("stat", "preview", "download", "open", *(('write', 'rename', 'move') if writable else ()),
                    *(('trash',) if writable and HostFilesProvider._LOOSE_MANAGED_MOVE_READY else ()))
        return ("stat",)

    def _path_writable(self, context: ProviderContext, path: str, scope: Mapping[str, Any] | None = None, *, assignments=None) -> bool:
        if scope is None:
            scope = self._scope(context)
        if scope.get("host"):
            return True
        if "write" not in set(scope.get("capabilities") or []):
            return False
        visible_root_ids = set(scope.get("visible_root_ids") or [])
        root_capabilities = scope.get("root_capabilities") or {}
        target = _native_path_identity(os.path.realpath(str(path)))
        if assignments is None:
            assignments = self.registry.visibility_for_subject(context.owner_username)
        for assignment in assignments:
            root = assignment.get("root") or {}
            root_id = str(assignment.get("root_id") or "")
            if root_id not in visible_root_ids or "write" not in set(root_capabilities.get(root_id) or []):
                continue
            if not root.get("enabled") or root.get("availability") != "available":
                continue
            root_path = str(root.get("canonical_path") or "")
            if not root_path or "write" not in set(assignment.get("capabilities") or []) or "write" not in set(root.get("capabilities") or []):
                continue
            root_path = _native_path_identity(root_path)
            try:
                if root.get("kind") == "exact_file":
                    if target == root_path:
                        return True
                elif root.get("kind") == "recursive_directory":
                    root_parts = Path(root_path).parts
                    if Path(target).parts[:len(root_parts)] == root_parts:
                        return True
            except ValueError:
                continue
        return False

    @classmethod
    def _editor_file(cls, name: str, mime_type: str | None = None) -> bool:
        return editor_text_hint(name, mime_type)

    @classmethod
    def _representation(cls, name: str, data: Mapping[str, Any]) -> str:
        # Files may make an explicit representation decision. This keeps a
        # provider's supported Markdown classification authoritative while
        # allowing a text adapter to keep a .md source file in source mode.
        explicit = str(data.get("representation") or "").strip()
        if explicit in {"markdown", "text"}:
            return explicit
        mime = str(data.get("media_type") or data.get("mime_type") or mimetypes.guess_type(name)[0] or "").lower()
        if mime == "text/markdown" or Path(name).suffix.lower() in {".markdown", ".mdown", ".md", ".mdx"}:
            return "markdown"
        return "text"

    @staticmethod
    def _fingerprint_value(data: Mapping[str, Any]) -> str:
        fingerprint = data.get("fingerprint")
        if isinstance(fingerprint, Mapping):
            return str(fingerprint.get("value") or "").strip()
        return str(fingerprint or "").strip()

    @staticmethod
    def _metadata(data: Mapping[str, Any]) -> dict[str, Any]:
        metadata = {
            key: data[key]
            for key in ("encoding", "newline", "language", "mode")
            if data.get(key) is not None
        }
        if data.get("bom_bytes") is not None:
            metadata["bomBytes"] = data["bom_bytes"]
        return metadata

    @classmethod
    def _snapshot(cls, name: str, data: Mapping[str, Any], representation: str) -> dict[str, Any]:
        text = data.get("text")
        fingerprint = cls._fingerprint_value(data)
        if not isinstance(text, str) or not fingerprint:
            raise FilesFacadeError("Host text resource snapshot is unavailable", code="provider_unavailable")
        return {
            "revision": {"kind": "hostFingerprint", "value": fingerprint},
            "envelope": {
                "text": text,
                "metadata": cls._metadata(data),
                "representation": representation,
            },
        }

    def _resource(self, path: str, *, name: str, kind: str, size: int | None = None,
                  modified_unix_ms: int | None = None, provenance: Mapping[str, Any] | None = None,
                  writable: bool = False, revision: Mapping[str, Any] | None = None,
                  action_receipt: Mapping[str, Any] | None = None,
                  native_available: bool | None = False, native_icon_available: bool = False) -> ProviderResource:
        normalized_kind = {
            "directory": "folder",
            "recursive_directory": "folder",
            "file": "file",
            "exact_file": "file",
            "symlink": "symlink",
            "other": "special",
        }.get(str(kind).lower(), "special")
        mime_type = None if normalized_kind != "file" else mimetypes.guess_type(name)[0]
        return ProviderResource(
            _origin(path),
            name,
            normalized_kind,
            self._caps(normalized_kind, writable=writable),
            # Physical roots have no parent; serializing / as its own parent
            # creates a cycle in the facade's authorized reveal ancestry.
            parent_origin_id=_origin(str(Path(path).parent)) if normalized_kind in {"file", "folder"} and Path(path).parent != Path(path) else None,
            mime_type=mime_type,
            size=size,
            modified_unix_ms=modified_unix_ms,
            provenance=dict(provenance or {"domain": "host"}),
            open_target={"app": "editor"} if normalized_kind == "file" and HostFilesProvider._editor_file(name, mime_type) else None,
            sort_kind=mime_type or normalized_kind,
            native_thumbnail_available=(sys.platform == "darwin" or native_available) if normalized_kind == "file" else False,
            native_icon_available=native_icon_available if normalized_kind == "file" else False,
            child_sort_keys=SORT_KEYS if "children" in self._caps(normalized_kind, writable=writable) else (),
            action_receipt=action_receipt,
            revision=revision,
        )

    def _selected_folder_rename_capability(self, context: ProviderContext, entry: ProviderResource) -> ProviderResource:
        if entry.kind != "folder" or "write" not in entry.capabilities:
            return entry
        try:
            if self.selected_folder_required(context, source_origin_id=entry.origin_id, source_is_directory=True):
                return replace(entry, capabilities=tuple((*entry.capabilities, "rename")))
        except Exception:
            pass
        return entry

    def _folder_rename_capabilities(self, context: ProviderContext, entries: tuple[ProviderResource, ...]) -> tuple[ProviderResource, ...]:
        from src.openclank.media_ownership import MediaProvenance
        folders = {
            os.path.normcase(os.path.abspath(_path(entry.origin_id))): entry.origin_id
            for entry in entries if entry.kind == "folder" and "write" in entry.capabilities
        }
        if not folders:
            return entries
        lister = getattr(self.operation_store, "list_host_media_operations", None)
        if not callable(lister):
            return entries
        counts: dict[str, int] = {}
        selected: set[str] = set()

        def inspect(row):
            receipt = row.get("receipt")
            value = receipt.get("provenance") if isinstance(receipt, Mapping) else None
            if isinstance(value, Mapping):
                record = MediaProvenance.from_receipt(value)
                path = Path(os.path.normcase(os.path.abspath(os.path.join(
                    record.canonical_root, *record.document_path.replace("\\", "/").split("/")
                ))))
                for parent in (path, *path.parents):
                    origin = folders.get(str(parent))
                    if origin is not None:
                        counts[origin] = counts.get(origin, 0) + 1
                        if record.origin == "selected_root":
                            selected.add(origin)
            # Inspect one consistent owner snapshot. The safety bound belongs
            # to each candidate folder, so there is no combined result set.
            return False

        try:
            lister(owner_subject_id=str(context.owner_subject_id), select=inspect)
        except Exception:
            # Listing remains usable when media provenance can't be confirmed.
            return entries
        return tuple(
            replace(entry, capabilities=(*entry.capabilities, "rename"))
            if entry.origin_id in selected and counts[entry.origin_id] <= 256 else entry
            for entry in entries
        )

    async def create_directory(
        self, context: ProviderContext, *, parent_origin_id: str, name: str,
        operation_id: str | None = None, request_digest: str | None = None,
        parent_revision: Mapping[str, Any] | None = None, collision: str = "fail",
    ) -> ProviderResource:
        parent = _path(parent_origin_id)
        folder = str(name or "").strip()
        if not folder or folder in {".", ".."} or "/" in folder or "\\" in folder or "\x00" in folder:
            raise FilesFacadeError("Host import directory name is invalid", code="invalid_resource_request")
        entry = await self.stat(context, origin_id=parent_origin_id)
        if entry.origin_id != parent_origin_id or entry.kind != "folder" or "write" not in entry.capabilities:
            raise FilesFacadeError("Host directory parent is unavailable", code="resource_unavailable")
        return await self._run_claimed_host_mutation(context, [os.path.join(parent, folder)],
            lambda begin, no_effect: self._create_directory_claimed(context, parent_origin_id=parent_origin_id, name=name,
                operation_id=operation_id, request_digest=request_digest, parent_revision=parent_revision,
                collision=collision, _begin_mutation=begin, _known_no_effect=no_effect), delayed_start=True)

    async def _create_directory_claimed(
        self,
        context: ProviderContext,
        *,
        parent_origin_id: str,
        name: str,
        operation_id: str | None = None,
        request_digest: str | None = None,
        parent_revision: Mapping[str, Any] | None = None,
        collision: str = "fail",
        _begin_mutation: Callable[[], None],
        _known_no_effect: Callable[[], None],
    ) -> ProviderResource:
        """Create one child directory with an ownership marker before mkdir."""
        parent = _path(parent_origin_id)
        folder = str(name or "").strip()
        if (
            not folder or len(folder.encode("utf-8")) > 240 or "\x00" in folder
            or "/" in folder or "\\" in folder or folder in {".", ".."}
            or Path(folder).name != folder
        ):
            raise FilesFacadeError("Host import directory name is invalid", code="invalid_resource_request")
        if collision not in {"fail", "reuse"}:
            raise FilesFacadeError("Host directory collision policy is invalid", code="unsupported_operation")
        scope = self._scope(context)
        if not self._path_writable(context, parent, scope):
            raise FilesFacadeError("Host import directory is read-only", code="resource_unavailable")
        target = os.path.join(parent, folder)
        operation = str(operation_id or f"host-mkdir-{uuid.uuid4().hex}")
        digest = str(request_digest or "")
        loaded = self._directory_operation_load(context, operation)
        if loaded:
            if loaded.get("request_kind") != "create-directory" or loaded.get("request_digest") != digest:
                raise FilesFacadeError("Host directory operation conflicts with an earlier request", code="idempotency_conflict")
            status = await self._directory_status(context, loaded)
            if status and status.get("state") == "complete":
                return await self._directory_resource(context, loaded)
            raise FilesFacadeError("Host directory operation is still pending", code="operation_pending")
        parent_entry = await self.stat(context, origin_id=parent_origin_id)
        if parent_entry.kind != "folder" or "children" not in parent_entry.capabilities or "write" not in parent_entry.capabilities:
            raise FilesFacadeError("Host directory parent is unavailable", code="resource_unavailable")
        if parent_revision is not None and dict(parent_entry.revision or {}) != dict(parent_revision):
            raise FilesFacadeError("Host directory parent is stale", code="resource_changed")
        before_kind = ""
        before_revision = None
        try:
            before = await self.stat(context, origin_id=_origin(target))
            before_kind = str(before.kind)
            before_revision = dict(before.revision) if isinstance(before.revision, Mapping) else None
        except FilesFacadeError as exc:
            if exc.code != "resource_unavailable":
                raise
        if before_kind:
            if collision == "reuse" and before_kind == "folder":
                marker = {
                    "request_kind": "create-directory", "operation_id": operation,
                    "item_id": operation, "generation": int(context.policy_generation),
                    "workspace_id": str(context.workspace_id or "default"), "phase": "complete",
                    "parent": parent, "target": target, "name": folder,
                    "request_digest": digest, "collision": collision,
                    "parent_revision": dict(parent_revision) if parent_revision is not None else None,
                    "target_before_kind": before_kind, "target_before_revision": before_revision,
                    "outcome": "unchanged",
                }
                self._directory_operation_save(context, operation, digest, marker)
                return await self._directory_resource(context, marker)
            raise FilesFacadeError("Host directory destination already exists", code="resource_changed")
        ownership_path = self._ownership_sidecar(
            parent, operation, folder, digest,
            owner_id=str(context.owner_subject_id),
            workspace_id=str(context.workspace_id or "default"),
        )
        try:
            ownership_path, ownership_token = self._claim_ownership(ownership_path)
        except FileExistsError as exc:
            raise FilesFacadeError("Host directory operation already owns this target", code="resource_changed") from exc
        marker = {
            "request_kind": "create-directory", "operation_id": operation,
            "item_id": operation, "generation": int(context.policy_generation),
            "workspace_id": str(context.workspace_id or "default"), "phase": "pending",
            "parent": parent, "target": target, "name": folder,
            "request_digest": digest, "collision": collision,
            "parent_revision": dict(parent_revision) if parent_revision is not None else None,
            "target_before_kind": before_kind, "target_before_revision": before_revision,
            "ownership_path": ownership_path, "ownership_token": ownership_token,
        }
        phase = "save_marker"
        try:
            self._directory_operation_save(context, operation, digest, marker)
            phase = "mkdir"
            _begin_mutation()
            await self._client(context, scope).request("mkdir", target, {})
            phase = "post_mkdir_stat"
            created = await self.stat(context, origin_id=_origin(target))
            if created.kind != "folder" or created.parent_origin_id != parent_origin_id:
                raise FilesFacadeError("Host provider returned an invalid directory", code="provider_unavailable")
            complete = {**marker, "phase": "complete", "revision": dict(created.revision or {})}
            self._directory_operation_save(context, operation, digest, complete)
            self._release_ownership(marker)
            return await self._directory_resource(context, complete)
        except FilesServiceError as exc:
            if exc.code in {"denied", "invalid_path", "malformed_request", "policy_generation_changed", "conflict"}:
                _known_no_effect()
            code = exc.code if exc.code in {"root_unavailable", "denied", "invalid_path", "conflict",
                "deadline_exceeded", "cancelled", "backpressure", "policy_generation_changed", "unauthorized"} else "other"
            recovery = "preimage_unavailable" if str(exc) == "required recovery preimage unavailable; original preserved" else "unknown"
            _LOG.warning("Host directory creation failed phase=%s code=%s recovery=%s", phase, code, recovery)
            if exc.code == "conflict" and collision == "reuse":
                try:
                    existing = await self.stat(context, origin_id=_origin(target))
                except Exception:
                    existing = None
                if existing is not None and existing.kind == "folder" and existing.parent_origin_id == parent_origin_id:
                    complete = {**marker, "phase": "complete", "outcome": "unchanged", "revision": dict(existing.revision or {})}
                    self._directory_operation_save(context, operation, digest, complete)
                    self._release_ownership(marker)
                    return await self._directory_resource(context, complete)
            if exc.code != "conflict":
                # The mkdir response may have been lost after the service
                # committed. Keep ownership and the pending marker so a
                # restarted provider can prove and promote the exact target.
                raise _service_error(exc) from exc
            failed = {**marker, "phase": "failed"}
            try:
                self._directory_operation_save(context, operation, digest, failed)
            finally:
                self._release_ownership(marker)
            raise _service_error(exc) from exc
        except FilesFacadeError as exc:
            code = exc.code if exc.code in {"provider_unavailable", "resource_unavailable", "resource_changed",
                "resource_ref_stale", "operation_pending"} else "other"
            _LOG.warning("Host directory creation failed phase=%s code=%s recovery=unknown", phase, code)
            # A committed target with a lost post-mkdir stat/receipt must stay
            # recoverable through the pending marker. Known validation errors
            # are still surfaced; operation_status will either reconcile the
            # exact folder or leave the operation pending for a later retry.
            raise

    async def roots(self, context: ProviderContext):
        # The visible anchors are children so exact-file grants and Home coexist
        # under one stable provider root, like every other provider namespace.
        self._scope(context)
        return [ProviderResource(
            "host:v1:root",
            "Host locations",
            "provider_root",
            ("children", "stat"),
            provenance={"domain": "host"},
            child_sort_keys=self._ROOT_SORTS,
        )]

    async def children(
        self,
        context: ProviderContext,
        *,
        parent_origin_id: str,
        cursor: str | None,
        snapshot: str | None,
        limit: int,
        sort: Mapping[str, Any],
        query: str,
    ) -> ProviderPage:
        scope = self._scope(context)
        assignments = (
            self.registry.visibility_for_subject(context.owner_username)
            if not scope.get("host") and "write" in (scope.get("capabilities") or ()) else ()
        )
        if parent_origin_id == "host:v1:root":
            try:
                projection = project_navigation_roots(self.registry, context.owner_username, scope)
            except FilesystemRegistryError as exc:
                raise FilesFacadeError("Host policy is unavailable", code="provider_unavailable") from exc
            by_path: dict[str, dict[str, Any]] = {str(item["path"]): item for item in projection["roots"]}
            default_path = str(projection.get("default_path") or "")
            if default_path:
                by_path.setdefault(default_path, {
                    "path": default_path,
                    "name": "Home" if scope.get("host") else Path(default_path).name or default_path,
                    "kind": "recursive_directory",
                    "capabilities": ["read", "write"] if scope.get("host") else ["read"],
                })
            all_rows = tuple(
                self._resource(
                    str(item["path"]),
                    name=str(item.get("name") or Path(str(item["path"])).name or item["path"]),
                    kind=str(item.get("kind") or "recursive_directory"),
                    provenance={
                        "domain": "host",
                        "favorite": str(item["path"]) == default_path,
                    },
                    writable=self._path_writable(context, str(item["path"]), scope, assignments=assignments),
                )
                for item in sorted(by_path.values(), key=lambda item: (
                    str(item.get("path")) != default_path,
                    str(item.get("kind")) == "exact_file",
                    str(item.get("name") or "").casefold(),
                ))
            )
            if query:
                folded = query.casefold()
                all_rows = tuple(row for row in all_rows if folded in row.name.casefold())
            value = (
                (lambda row: str(row.sort_kind or row.kind).casefold())
                if sort["key"] == "kind"
                else (lambda row: row.name.casefold())
            )
            ordered_rows = list(all_rows)
            ordered_rows.sort(key=lambda row: row.origin_id)
            ordered_rows.sort(key=value, reverse=sort["direction"] == "desc")
            if bool(sort.get("directories_first", True)):
                ordered_rows.sort(key=lambda row: row.kind != "folder")
            all_rows = tuple(ordered_rows)
            root_snapshot = f"host-roots-{int(scope.get('generation') or 0)}"
            if snapshot is not None and snapshot != root_snapshot:
                raise FilesFacadeError("provider cursor is stale", code="stale_cursor")
            try:
                offset = int(cursor or 0)
            except (TypeError, ValueError) as exc:
                raise FilesFacadeError("provider cursor is stale", code="stale_cursor") from exc
            if offset < 0 or offset > len(all_rows):
                raise FilesFacadeError("provider cursor is stale", code="stale_cursor")
            rows = all_rows[offset:offset + limit]
            next_offset = offset + len(rows)
            return ProviderPage(
                rows,
                next_cursor=str(next_offset) if next_offset < len(all_rows) else None,
                total=len(all_rows),
                snapshot=root_snapshot,
            )

        path = _path(parent_origin_id)
        if query:
            if cursor or snapshot:
                raise FilesFacadeError("provider cursor is stale", code="stale_cursor")
            client = self._client(context, scope)
            try:
                response = await client.request("filename_search", path, {
                    "query": query,
                    "max_results": limit,
                    "max_entries": 10_000,
                    "max_depth": 32,
                    "max_bytes_per_file": 0,
                    "include_hidden": False,
                    "case_sensitive": False,
                })
                search_data = response.get("data") or {}
                matches = tuple(str(item) for item in search_data.get("matches") or ()
                                if self._TRASH_DIR not in Path(str(item)).parts)
                search_complete = search_data.get("complete")
                if not isinstance(search_complete, bool):
                    search_complete = None
                rows: list[ProviderResource] = []
                for match in matches:
                    metadata = await client.request("stat", match, {"include_fingerprint": False})
                    data = metadata.get("data") or {}
                    canonical = str(data.get("path") or match)
                    rows.append(self._resource(
                        canonical,
                        name=Path(canonical).name or canonical,
                        kind=str(data.get("kind") or "other"),
                        size=int(data.get("size") or 0),
                        modified_unix_ms=int(data["modified_unix_ms"]) if data.get("modified_unix_ms") is not None else None,
                        writable=self._path_writable(context, canonical, scope, assignments=assignments),
                        native_available=bool(metadata.get("native_thumbnails")),
                        native_icon_available=bool(metadata.get("native_icons")),
                    ))
            except FilesServiceError as exc:
                raise _service_error(exc) from exc

            key = str(sort.get("key") or "name")
            descending = str(sort.get("direction") or "asc") == "desc"
            directories_first = bool(sort.get("directories_first", True))
            value = {
                "name": lambda row: row.name.casefold(),
                "kind": lambda row: (row.kind.casefold(), row.name.casefold()),
                "size": lambda row: (int(row.size or 0), row.name.casefold()),
                "modified": lambda row: (int(row.modified_unix_ms or 0), row.name.casefold()),
            }[key]
            rows.sort(key=lambda row: row.origin_id)
            rows.sort(key=value, reverse=descending)
            if directories_first:
                rows.sort(key=lambda row: row.kind != "folder")
            digest = hashlib.sha256()
            digest.update(query.encode("utf-8"))
            for row in rows:
                digest.update(f"\0{row.origin_id}\0{row.kind}\0{row.size}\0{row.modified_unix_ms}".encode("utf-8"))
            return ProviderPage(
                tuple(rows),
                next_cursor=None,
                total=len(rows),
                snapshot=f"host-search-{digest.hexdigest()[:24]}",
                complete=search_complete,
            )

        payload: dict[str, Any] = {
            "sort": {
                "key": sort["key"],
                "direction": sort["direction"],
                "directories_first": bool(sort.get("directories_first", True)),
                "collation": "open-clank-v1",
            },
            "limit": limit,
        }
        if cursor:
            try:
                payload["cursor"] = json.loads(cursor)
            except json.JSONDecodeError as exc:
                raise FilesFacadeError("provider cursor is stale", code="stale_cursor") from exc
        try:
            response = await self._client(context, scope).request("list_directory", path, payload)
        except FilesServiceError as exc:
            raise _service_error(exc) from exc
        data = response.get("data") or {}
        canonical = str(data.get("path") or path)
        entries = tuple(
            self._resource(
                os.path.join(canonical, str(row.get("name") or "")),
                name=str(row.get("name") or ""),
                kind=str(row.get("kind") or "other"),
                size=int(row.get("size") or 0),
                modified_unix_ms=int(row["modified_unix_ms"]) if row.get("modified_unix_ms") is not None else None,
                writable=self._path_writable(context, os.path.join(canonical, str(row.get("name") or "")), scope,
                                             assignments=assignments),
                native_available=bool(response.get("native_thumbnails")),
                native_icon_available=bool(response.get("native_icons")),
            )
            for row in data.get("entries") or ()
            if str(row.get("name") or "") and str(row.get("name")) != self._TRASH_DIR
        )
        if any(entry.kind == "folder" and "write" in entry.capabilities for entry in entries):
            entries = await asyncio.to_thread(self._folder_rename_capabilities, context, entries)
        next_cursor = data.get("next_cursor")
        return ProviderPage(
            entries,
            next_cursor=json.dumps(next_cursor, separators=(",", ":"), sort_keys=True) if next_cursor else None,
            total=None,
            snapshot=str(data.get("generation") or snapshot or ""),
        )

    async def strict_snapshot_directory(self, context: ProviderContext, *, origin_id: str, limit: int) -> Mapping[str, Any]:
        """Private bounded observation; child read policy remains caller-owned."""
        path=_path(origin_id)
        if type(limit) is not int or not 1<=limit<=1024:raise FilesFacadeError('Snapshot limit unavailable',code='resource_unavailable')
        scope=self._scope(context);client=self._client(context,scope)
        try:
            caps=await self._snapshot_owned_request(client,'capabilities',path,{})
            if 'strict_snapshot_directory_v1' not in (caps.get('operations') or []):raise ValueError('unsupported')
            response=await self._snapshot_owned_request(client,'list_directory',path,{'strict_snapshot':True,'limit':limit})
            data=response.get('data') if isinstance(response,Mapping) else None
            if (not isinstance(data,Mapping) or data.get('path')!=path or type(data.get('entries')) is not list
                or len(data['entries'])>limit or type(data.get('complete')) is not bool
                or type(data.get('omitted_count')) is not int or not 0<=data['omitted_count']<=limit+1
                or not re.fullmatch(r'[0-9a-f]{64}',str(data.get('object_tag') or ''))
                or not re.fullmatch(r'[0-9a-f]{64}',str(data.get('directory_id') or ''))
                or type(data.get('reasons')) is not list):raise ValueError('invalid listing')
            reasons=data['reasons']
            if (len(set(reasons))!=len(reasons) or any(reason not in {'overflow','invalid_name','symlink','special','denied'} for reason in reasons)
                or data['complete'] != (data['omitted_count']==0 and not reasons)):
                raise ValueError('invalid listing coverage')
            names=[]
            for entry in data['entries']:
                if (type(entry) is not dict or set(entry)!={'name','kind','size'} or type(entry['name']) is not str
                    or not entry['name'] or entry['kind'] not in {'file','directory'}
                    or type(entry['size']) is not int or entry['size']<0):raise ValueError('invalid listing entry')
                names.append(entry['name'])
            if len(names)!=len(set(names)) or names!=sorted(names,key=lambda name:name.encode('utf-8','strict')):
                raise ValueError('invalid listing order')
            return dict(data)
        except (FilesServiceError,ValueError,TypeError) as exc:
            raise FilesFacadeError('Snapshot directory unavailable',code='resource_unavailable') from exc

    async def snapshot_file_hash(self, context: ProviderContext, *, origin_id: str, max_bytes: int, max_ms: int) -> Mapping[str, Any]:
        """Hash one authorized descriptor without consuming a shared stream handle."""
        path=_path(origin_id)
        if type(max_bytes) is not int or not 1<=max_bytes<=1024**3 or type(max_ms) is not int or not 1<=max_ms<=30000:
            raise FilesFacadeError('Snapshot bounds unavailable',code='resource_unavailable')
        client=self._client(context,self._scope(context))
        try:
            caps=await self._snapshot_owned_request(client,'capabilities',path,{})
            if 'snapshot_file_hash' not in (caps.get('operations') or []):raise ValueError('unsupported')
            response=await self._snapshot_owned_request(client,'snapshot_file_hash',path,{'max_bytes':max_bytes,'max_ms':max_ms})
            data=response.get('data') if isinstance(response,Mapping) else None
            if (not isinstance(data,Mapping) or data.get('path')!=path or type(data.get('size')) is not int
                or not 0<=data['size']<=max_bytes or not re.fullmatch(r'[0-9a-f]{64}',str(data.get('sha256') or ''))
                or not re.fullmatch(r'[0-9a-f]{64}',str(data.get('object_tag') or ''))):raise ValueError('invalid hash')
            return dict(data)
        except (FilesServiceError,ValueError,TypeError) as exc:
            raise FilesFacadeError('Snapshot file unavailable',code='resource_unavailable') from exc

    @staticmethod
    async def _snapshot_owned_request(client, operation: str, path: str, payload: dict):
        task=asyncio.create_task(client.request(operation,path,payload))
        try:return await asyncio.shield(task)
        except asyncio.CancelledError:
            while not task.done():
                try:await asyncio.shield(task)
                except asyncio.CancelledError:continue
                except Exception:break
            if not task.cancelled():
                try:task.result()
                except Exception:pass
            raise

    async def stat(self, context: ProviderContext, *, origin_id: str) -> ProviderResource:
        return await self._stat_for_purpose(context, origin_id=origin_id, inspection=False)

    async def stat_checkpoint(self, context: ProviderContext, *, origin_id: str) -> ProviderResource:
        """Bounded metadata-first fingerprint for an explicit checkpoint selection."""
        before=await self.stat_inspection(context,origin_id=origin_id)
        if (before.origin_id!=origin_id or before.kind!="file" or type(before.size) is not int or not 0<=before.size<=16*1024*1024
            or not isinstance(before.revision,Mapping) or before.revision.get("kind")!="hostObjectIdentity"):
            raise FilesFacadeError("Checkpoint file unavailable",code="resource_unavailable")
        captured=await self.snapshot_file_hash(context,origin_id=origin_id,max_bytes=16*1024*1024,max_ms=30_000)
        after=await self.stat_inspection(context,origin_id=origin_id)
        if (after.origin_id!=origin_id or after.kind!="file" or after.size!=before.size or after.revision!=before.revision
            or captured.get("size")!=before.size or captured.get("object_tag")!=before.revision.get("value")):
            raise FilesFacadeError("Checkpoint file changed",code="resource_changed")
        return replace(after,revision={"kind":"hostFingerprint","value":f"sha256:{captured['sha256']}:{captured['size']}"})

    async def stat_inspection(self, context: ProviderContext, *, origin_id: str) -> ProviderResource:
        return await self._stat_for_purpose(context, origin_id=origin_id, inspection=True)

    async def stat_for_thumbnail(self, context: ProviderContext, *, origin_id: str) -> ProviderResource:
        return await self._stat_for_purpose(
            context, origin_id=origin_id, inspection=False, include_fingerprint=False,
        )

    async def _stat_for_purpose(
        self, context: ProviderContext, *, origin_id: str, inspection: bool,
        include_fingerprint: bool = True,
    ) -> ProviderResource:
        if origin_id == "host:v1:root":
            self._scope(context)
            return ProviderResource(
                origin_id,
                "Host locations",
                "provider_root",
                ("children", "stat"),
                provenance={"domain": "host"},
                child_sort_keys=self._ROOT_SORTS,
            )
        path = _path(origin_id)
        scope = self._scope(context)
        try:
            client = self._client(context, scope)
            if inspection:
                capability = await client.request("capabilities", path, {})
                if "stat_object_identity" not in (capability.get("operations") or []):
                    raise FilesFacadeError("Bounded inspection identity is unavailable", code="provider_unavailable")
            response = await client.request("stat", path, {
                "include_fingerprint": include_fingerprint and not inspection,
                **({"object_identity": True} if inspection else {}),
            })
        except FilesServiceError as exc:
            raise _service_error(exc) from exc
        data = response.get("data") or {}
        object_tag = data.get("object_tag")
        if inspection and (not isinstance(object_tag, str) or not re.fullmatch(r"[0-9a-f]{64}", object_tag)):
            raise FilesFacadeError("Bounded inspection identity is unavailable", code="provider_unavailable")
        canonical = str(data.get("path") or path)
        fingerprint = self._fingerprint_value(data)
        directory_revision = data.get("directory_revision")
        if not isinstance(directory_revision, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", directory_revision):
            directory_revision = None
        native_thumbnails = response.get("native_thumbnails")
        if include_fingerprint or native_thumbnails is not None:
            native_thumbnails = bool(native_thumbnails)
        return self._selected_folder_rename_capability(context, self._resource(
            canonical,
            name=Path(canonical).name or canonical,
            kind=str(data.get("kind") or "other"),
            size=int(data.get("size") or 0),
            modified_unix_ms=int(data["modified_unix_ms"]) if data.get("modified_unix_ms") is not None else None,
            writable=self._path_writable(context, canonical, scope),
            revision=({"kind": "hostObjectIdentity", "value": object_tag} if inspection else
                      {"kind": "hostFingerprint", "value": fingerprint} if fingerprint else
                      {"kind": "hostDirectoryRevision", "value": directory_revision} if directory_revision else None),
            native_available=native_thumbnails,
            native_icon_available=bool(response.get("native_icons")),
        ))

    async def ordinary_create_resource(self, context: ProviderContext, *, parent_origin_id: str,
                                       name: str, text: str, operation_id: str,
                                       expected_revision: Mapping[str, Any], status_only: bool = False,
                                       before_dispatch=None) -> Mapping[str, Any]:
        """One scoped, no-overwrite ordinary text create with original custody."""
        from src.openclank.ordinary_create import ordinary_create_once, create_recovery_binding
        parent = _path(parent_origin_id)
        filename = str(name or '').strip()
        if (not filename or len(filename.encode('utf-8')) > 240 or filename in ('.','..')
                or '/' in filename or '\\' in filename or '\x00' in filename
                or Path(filename).name != filename or Path(filename).suffix.lower() not in ('.md','.markdown','.html','.svg')
                or not isinstance(text,str) or not isinstance(expected_revision,Mapping)
                or set(expected_revision) != {'kind','value'} or expected_revision.get('kind') != 'hostDirectoryRevision'):
            raise FilesFacadeError('Ordinary create unavailable',code='invalid_resource_request')
        scope = self._scope(context)
        target = os.path.join(parent,filename)
        client = self._client(context,scope)
        recovery=create_recovery_binding(str(context.owner_subject_id),str(context.workspace_id),'','human',
            operation_id,target,expected_revision['value'],text.encode('utf-8'))
        async def current():
            try:
                fresh_scope=self._scope(context)
                current_parent=await self.stat(context,origin_id=parent_origin_id)
                if (not self._path_writable(context,parent,fresh_scope) or current_parent.origin_id != parent_origin_id
                        or dict(current_parent.revision or {}) != dict(expected_revision)
                        or 'children' not in current_parent.capabilities or 'write' not in current_parent.capabilities): return False
                if before_dispatch is not None and await before_dispatch() is not True: return False
                return True
            except Exception: return False
        async def perform(before):
            return await ordinary_create_once(client=client,repository=self.operation_store,
                account=str(context.owner_subject_id),workspace=str(context.workspace_id),session='',lane='human',
                key=operation_id,path=target,parent_revision=expected_revision['value'],content=text.encode('utf-8'),
                generation=int(context.policy_generation),before_issue=before,status_only=status_only)
        try:
            if status_only:
                result=await perform(None)
            else:
                async def work(begin_mutation, known_no_effect):
                    async def checked():
                        if not await current(): return False
                        begin_mutation()
                        return True
                    result=await perform(checked)
                    if result.get('state') == 'not_applied':
                        known_no_effect()
                        return result
                    if result.get('state') != 'committed':
                        raise FilesFacadeError('Ordinary create outcome requires reconciliation',code='mutation_outcome_unknown')
                    return result
                result=await self._run_claimed_host_mutation(context,[target],work,delayed_start=True,recovery=recovery)
        except FilesFacadeError: raise
        except FilesServiceError as exc: raise _service_error(exc) from exc
        except Exception as exc:
            raise FilesFacadeError('Ordinary create outcome unavailable',code='resource_unavailable') from exc
        if result['state'] != 'committed': return dict(result)
        result=dict(result)
        if status_only:
            # Native verified the exact original History action after its owned IO
            # settled. Current bytes are irrelevant to release of its old claim.
            try:
                original=self.operation_store.get_operation(owner_subject_id=context.owner_subject_id,operation_id=recovery['operation_id'])
                receipt=original.get('receipt') if isinstance(original,Mapping) else None
                if (not isinstance(receipt,Mapping) or receipt.get('settled') is not True
                        or original.get('digest') != recovery['request_digest']
                        or receipt.get('binding',{}).get('action_id') != recovery['action_id']
                        or receipt.get('binding',{}).get('request_digest') != recovery['native_digest']):
                    raise ValueError('original custody unavailable')
                offset=0; matched=[]
                while offset < 2048:
                    batch=self.operation_store.list_operations(owner_subject_id=context.owner_subject_id,
                        operation_prefix='__host_path_claim__',phase='host_path_claim',offset=offset,limit=256)
                    for item in batch:
                        claim=(item.get('receipt') or {}).get('_host_writer_claim')
                        if (isinstance(claim,Mapping) and claim.get('recovery') == recovery
                                and claim.get('paths') == [target] and claim.get('workspace_id') == str(context.workspace_id)
                                and item.get('generation') == original.get('generation')
                                and claim.get('token') == item.get('digest')):
                            matched.append((item['operation_id'],claim['token']))
                    if len(batch)<256: break
                    offset+=len(batch)
                else: raise ValueError('claim inventory incomplete')
                if len(matched)>1: raise ValueError('ambiguous original claims')
                if matched:
                    self.operation_store.release_host_mutation(owner_subject_id=context.owner_subject_id,
                        operation_id=matched[0][0],token=matched[0][1])
            except Exception:
                result['claim_state']='unavailable'
        try:
            observed=await self.stat(context,origin_id=_origin(target))
            if observed.origin_id == _origin(target) and observed.revision == {'kind':'hostFingerprint','value':result['fingerprint']}:
                result['resource']=observed
        except Exception: pass
        return result

    async def create_resource(
        self,
        context: ProviderContext,
        *,
        parent_origin_id: str,
        name: str,
        text: str,
        action_id: str | None = None,
        before_dispatch: Callable[..., Any] | None = None,
    ) -> ProviderResource | Mapping[str, Any]:
        """Create one ordinary Markdown file beneath an authorized Host folder."""
        parent = _path(parent_origin_id)
        filename = str(name or "").strip()
        if (
            not filename or len(filename) > 240 or "\x00" in filename
            or "/" in filename or "\\" in filename
            or filename in {".", ".."} or Path(filename).name != filename
            or Path(filename).suffix.lower() not in {".md", ".markdown"}
        ):
            raise FilesFacadeError("Host template name must be a Markdown filename", code="invalid_resource_request")
        if not isinstance(text, str):
            raise FilesFacadeError("Host template text is invalid", code="invalid_resource_request")
        scope = self._scope(context)
        if not self._path_writable(context, parent, scope):
            raise FilesFacadeError("Host template folder is read-only", code="resource_unavailable")
        target = os.path.join(parent, filename)
        async def work(begin_mutation, known_no_effect):
            if before_dispatch is not None:
                await before_dispatch()
            begin_mutation()
            try:
                response = await self._client(context, scope).request('create', target, {'text': text})
            except FilesServiceError as exc:
                if exc.code in {'denied', 'invalid_path', 'malformed_request', 'policy_generation_changed', 'conflict', 'destination_exists'}:
                    known_no_effect()
                raise
            data = response.get('data') if isinstance(response, Mapping) else None
            created = data.get('Created') if isinstance(data, Mapping) else None
            fingerprint = created.get('fingerprint') if isinstance(created, Mapping) else None
            if not isinstance(fingerprint, Mapping) or fingerprint.get('algorithm') != 'sha256' or not isinstance(fingerprint.get('value'), str) or not re.fullmatch(r'sha256:[0-9a-f]{64}:[0-9]+', fingerprint['value']):
                raise FilesFacadeError('Host create outcome requires reconciliation', code='mutation_outcome_unknown')
            revision = {'kind': 'hostFingerprint', 'value': fingerprint['value']}
            result = {'outcome': 'created', 'revision': revision, 'history': _host_history_receipt(response), 'readback': 'unavailable'}
            try:
                observed = await self.stat(context, origin_id=_origin(target))
            except Exception:
                return result
            if observed.origin_id != _origin(target) or observed.kind != 'file' or observed.revision != revision:
                return {**result, 'readback': 'changed', 'changedAfterCreate': True}
            return {**result, 'readback': 'current', 'resource': observed}

        try:
            return await self._run_claimed_host_mutation(context, [target], work, delayed_start=True)
        except FilesServiceError as exc:
            if exc.code in {'conflict', 'destination_exists'}:
                raise FilesFacadeError('Host resource already exists', code='resource_changed') from exc
            raise _service_error(exc) from exc
        except FilesFacadeError as exc:
            if exc.code == 'operation_pending':
                raise FilesFacadeError('Host create outcome requires reconciliation', code='mutation_outcome_unknown') from exc
            raise

    async def action(
        self,
        context: ProviderContext,
        *,
        origin_id: str,
        action: str,
        args: Mapping[str, Any],
        action_id: str | None = None,
        expected_revision: Mapping[str, Any] | None = None,
        managed_root: Mapping[str, Any] | None = None,
    ) -> ProviderResource | Mapping[str, Any]:
        if action in {"trash", "restore"}:
            return await self._trash_action(context, origin_id=origin_id, action=action, args=args,
                                            action_id=action_id, expected_revision=expected_revision)
        if action not in {"rename", "move"}:
            raise FilesFacadeError("Host resource action is unavailable", code="resource_unavailable")
        path = _path(origin_id)
        name = str(args.get("name") or "").strip()
        if (
            not name or "/" in name or "\\" in name
            or Path(name).name != name or name in {".", ".."} or "\x00" in name
        ):
            raise FilesFacadeError("Host resource name is invalid", code="invalid_resource_request")
        scope = self._scope(context)
        try:
            current = await self._client(context, scope).request("stat", path, {"include_fingerprint": True})
        except FilesServiceError as exc:
            raise _service_error(exc) from exc
        current_data = current.get("data") or {}
        destination = os.path.join(str(Path(path).parent), name)
        if str(current_data.get("path") or path) != path:
            raise FilesFacadeError("Host source identity changed", code="resource_changed")
        fingerprint = self._fingerprint_value(current_data)
        if managed_root is not None:
            if not self._LOOSE_MANAGED_RENAME_READY or action != "rename" or not action_id or not expected_revision:
                raise FilesFacadeError("Managed loose rename is unavailable", code="managed_media_move_unsupported")
            is_folder = self._is_directory_kind(str(current_data.get("kind") or ""))
            current_revision = ({"kind":"hostDirectoryRevision", "value":current_data.get("directory_revision")} if is_folder
                                else {"kind":"hostFingerprint", "value":fingerprint})
            if not current_revision["value"] or dict(expected_revision) != current_revision:
                raise FilesFacadeError("Managed source changed", code="resource_changed")
            if is_folder and (not self._MANAGED_FOLDER_MOVE_READY or not managed_root.get("selected_folder")):
                raise FilesFacadeError("Managed folder rename unavailable", code="managed_media_move_unsupported")
            prepare = self.prepare_managed_folder_move if is_folder else self.prepare_managed_workspace_move
            await prepare(context, source_origin_id=origin_id,
                destination_origin_id=_origin(destination), expected_revision=expected_revision,
                operation_id=str(action_id), item_id="action", managed_root=managed_root)
            result = await self.execute_managed_workspace_item(context, operation_id=str(action_id), item_id="action", fresh_selected=managed_root)
            if result.get("outcome") != "committed":
                return result
            updated = await self.stat(context, origin_id=_origin(destination))
            return replace(updated, action_receipt={"action_id":str(action_id), "status":"complete", "phase":"complete",
                "durable":True, "receipt":{"action_id":str(action_id), "outcome":action}})
        if not fingerprint:
            raise FilesFacadeError("Host resource revision is unavailable", code="provider_unavailable")
        try:
            async def preflight():
                if os.path.normpath(destination) != os.path.normpath(path):
                    await self._preflight_managed_media_move(
                        context, path, destination,
                        source_is_directory=self._is_directory_kind(str(current_data.get("kind") or "")),
                    )
            await self._run_claimed_host_mutation(context, [path, destination], lambda: self._client(context, scope).request(
                "move",
                path,
                {"destination": destination, "expected_fingerprint": {"algorithm": "sha256", "value": fingerprint}},
            ), preflight=preflight)
        except FilesFacadeError as exc:
            if (exc.code != "managed_media_move_unsupported" or self._is_directory_kind(str(current_data.get("kind") or ""))
                    or not action_id):
                raise
            client = self._client(context, scope)
            try:
                capabilities = await client.request("capabilities", path, {})
            except FilesServiceError as error:
                raise _service_error(error) from error
            if "managed_file_move" not in (capabilities.get("operations") or []):
                raise
            await self.prepare_managed_workspace_move(context, source_origin_id=origin_id,
                destination_origin_id=_origin(destination), expected_revision={"kind": "hostFingerprint", "value": fingerprint},
                operation_id=str(action_id), item_id="action")
            result = await self.execute_managed_workspace_item(context, operation_id=str(action_id), item_id="action")
            if result.get("outcome") != "committed":
                return result
            updated = await self.stat(context, origin_id=_origin(destination))
            return replace(updated, action_receipt={"action_id": str(action_id), "status": "complete", "phase": "complete",
                "durable": True, "receipt": {"action_id": str(action_id), "outcome": action}})
        except FilesServiceError as exc:
            if exc.code == "conflict":
                raise FilesFacadeError("Host resource changed or destination already exists", code="resource_changed") from exc
            raise _service_error(exc) from exc
        updated = await self.stat(context, origin_id=_origin(destination))
        receipt_id = str(action_id or f"host-{action}-{uuid.uuid4().hex}")
        return replace(updated, action_receipt={
            "action_id": receipt_id,
            "status": "complete",
            "durable": True,
            "phase": "complete",
            "receipt": {"action_id": receipt_id, "outcome": action},
        })

    _TRASH_DIR = ".odysseus-trash"
    _TRASH_ID = re.compile(r"trash-[A-Za-z0-9_-]{1,96}")
    _FINGERPRINT = re.compile(r"sha256:[0-9a-f]{64}:[0-9]+")

    def _trash_root(self, context: ProviderContext, path: str, scope: Mapping[str, Any]) -> str | None:
        """Writable recursive root that lexically contains path (Host scope: the filesystem anchor)."""
        if scope.get("host"):
            return Path(path).anchor
        for assignment in self.registry.visibility_for_subject(context.owner_username):
            root = assignment.get("root") or {}
            root_path = os.path.normpath(str(root.get("canonical_path") or ""))
            if root.get("kind") != "recursive_directory" or not os.path.isabs(root_path):
                continue
            try:
                if os.path.commonpath([root_path, path]) == root_path and self._path_writable(context, root_path, scope):
                    return root_path
            except ValueError:
                continue
        return None

    async def _refuse_managed_media_trash(self, context: ProviderContext, path: str) -> None:
        """Refuse a document that owns media, a media asset, or anything in a media folder."""
        # Same provenance check the managed move uses (_managed_move_rows), never filename guessing.
        if self._managed_move_rows(context, path, source_is_directory=False):
            raise FilesFacadeError("This document owns managed media, so it cannot be moved to Trash on its own: that would leave its media links dangling. "
                                   "Nothing was changed. Keep it, or use Move to relocate the document together with its media.",
                                   code="managed_media_move_unsupported")
        from src.openclank.media_ownership import MediaProvenance
        lister = getattr(self.operation_store, "list_host_media_operations", None)
        target = os.path.normpath(os.path.abspath(path))

        def in_media_folder(row):
            receipt = row.get("receipt")
            provenance = receipt.get("provenance") if isinstance(receipt, Mapping) else None
            if not isinstance(provenance, Mapping):
                return False
            record = MediaProvenance.from_receipt(provenance)
            media = os.path.normpath(os.path.join(record.canonical_root, *record.resolved_media_dir().replace("\\", "/").split("/")))
            return os.path.commonpath([media, target]) == media

        try:
            rows = lister(owner_subject_id=str(context.owner_subject_id), select=in_media_folder)
        except Exception as exc:
            raise FilesFacadeError("Host media provenance is unavailable; trash was not started", code="provider_unavailable") from exc
        if rows:
            raise FilesFacadeError("This file is managed media (or sits in a managed media folder), so it cannot be moved to Trash on its own. "
                                   "Nothing was changed. Remove it by editing the document that links to it.",
                                   code="managed_media_move_unsupported")

    async def _trash_action(self, context: ProviderContext, *, origin_id: str, action: str, args: Mapping[str, Any],
                            action_id: str | None, expected_revision: Mapping[str, Any] | None) -> ProviderResource:
        """Move one regular file to the Native trash (History-captured) or put it back.

        trash: args == {}. restore: args == {"entry": <TrashEntry returned in the trash receipt>}.
        Every refusal precedes the single Native call; nothing is ever retried.
        """
        if not self._LOOSE_MANAGED_MOVE_READY:
            raise FilesFacadeError("Host trash is unavailable on this platform", code="managed_media_move_unsupported")
        path = _path(origin_id)
        scope = self._scope(context)
        root = self._trash_root(context, path, scope)
        if (root is None or not self._path_writable(context, path, scope) or self._TRASH_DIR in Path(path).parts
                or os.path.normpath(path) == os.path.normpath(root)):
            raise FilesFacadeError("Host resource cannot be trashed or restored here", code="resource_unavailable")
        client = self._client(context, scope)

        async def observe(target: str) -> Mapping[str, Any]:
            try:
                data = (await client.request("stat", target, {"include_fingerprint": True})).get("data") or {}
            except FilesServiceError as exc:
                raise _service_error(exc) from exc
            # Native returns the canonical path: a symlink or `..` differs from the request.
            if str(data.get("path") or "") != target:
                raise FilesFacadeError("Host source identity changed", code="resource_changed")
            if str(data.get("kind") or "").casefold() != "file" or not self._FINGERPRINT.fullmatch(self._fingerprint_value(data)):
                raise FilesFacadeError("Host trash supports regular files only", code="resource_unavailable")
            return data

        entry: dict[str, Any] | None = None
        if action == "trash":
            if args:
                raise FilesFacadeError("Host trash takes no arguments", code="invalid_resource_request")
            subject = path
            fingerprint = self._fingerprint_value(await observe(path))
            if os.path.commonpath([root, path]) != root:
                raise FilesFacadeError("Host resource is outside its root", code="resource_unavailable")
        else:
            raw = args.get("entry") if set(args) == {"entry"} else None
            keys = {"id", "root_id", "original_path", "trashed_path", "fingerprint"}
            if not isinstance(raw, Mapping) or set(raw) != keys or any(not isinstance(raw[key], str) for key in keys):
                raise FilesFacadeError("Host restore entry is invalid", code="invalid_resource_request")
            entry = {key: raw[key] for key in sorted(keys)}
            trash_parent = os.path.dirname(path) if scope.get("host") else root
            subject = os.path.join(trash_parent, self._TRASH_DIR, entry["id"])
            if (entry["original_path"] != path or entry["trashed_path"] != subject or not self._TRASH_ID.fullmatch(entry["id"])
                    or not entry["root_id"] or (scope.get("host") and entry["root_id"] != "app-host")
                    or not self._FINGERPRINT.fullmatch(entry["fingerprint"])):
                raise FilesFacadeError("Host restore entry is invalid", code="invalid_resource_request")
            try:
                fingerprint = self._fingerprint_value(await observe(subject))
            except FilesFacadeError as exc:
                if exc.code != "resource_unavailable":
                    raise
                # Lost reply: the trashed file is gone. Read the same original ids; never issue a second restore.
                try:
                    back = self._fingerprint_value(await observe(path))
                except FilesFacadeError:
                    raise exc
                if back != entry["fingerprint"]:
                    raise
                resource = await self.stat(context, origin_id=origin_id)
                receipt_id = str(action_id or f"host-{action}-{uuid.uuid4().hex}")
                return replace(resource, action_receipt={
                    "action_id": receipt_id, "status": "complete", "durable": True, "phase": "complete",
                    "receipt": {"action_id": receipt_id, "outcome": action, "reconciled": True,
                                "history": {"state": "unavailable"}, "trash_entry": entry}})
            if fingerprint != entry["fingerprint"]:
                raise FilesFacadeError("Host trashed file changed", code="resource_changed")
        before = await self.stat(context, origin_id=origin_id) if action == "trash" else None
        revision = {"kind": "hostFingerprint", "value": fingerprint}
        if expected_revision is not None and dict(expected_revision) != revision:
            raise FilesFacadeError("Host resource revision changed", code="resource_changed")
        if before is not None and (before.origin_id != origin_id or before.revision != revision):
            raise FilesFacadeError("Host resource changed", code="resource_changed")
        expected = {"algorithm": "sha256", "value": fingerprint}

        async def preflight():
            await self._refuse_managed_media_trash(context, path)

        async def work():
            if entry is None:
                response = await client.request("trash", path, {"expected_fingerprint": expected})
                data = response.get("data") if isinstance(response, Mapping) else None
                if (not isinstance(data, Mapping) or data.get("original_path") != path or data.get("fingerprint") != fingerprint
                        or not isinstance(data.get("id"), str) or not isinstance(data.get("root_id"), str)
                        or not isinstance(data.get("trashed_path"), str)):
                    raise FilesFacadeError("Host trash outcome requires reconciliation", code="mutation_outcome_unknown")
                return response, {key: data[key] for key in ("fingerprint", "id", "original_path", "root_id", "trashed_path")}
            return await client.request("restore", path, {"entry": entry, "expected_fingerprint": expected}), entry

        try:
            response, done = await self._run_claimed_host_mutation(
                context, [path] if entry is None else [path, subject], work, preflight=preflight)
        except FilesServiceError as exc:
            if exc.code == "conflict":
                raise FilesFacadeError("Host resource changed or destination already exists", code="resource_changed") from exc
            raise _service_error(exc) from exc
        receipt_id = str(action_id or f"host-{action}-{uuid.uuid4().hex}")
        resource = before if before is not None else await self.stat(context, origin_id=origin_id)
        return replace(resource, action_receipt={
            "action_id": receipt_id, "status": "complete", "durable": True, "phase": "complete",
            "receipt": {"action_id": receipt_id, "outcome": action, "history": _host_history_receipt(response),
                        "trash_entry": done},
        })

    async def verify_managed_loose_item_proofs(self, context: ProviderContext, plan: Mapping[str, Any]) -> None:
        proofs = [{"path": plan["source"], "fingerprint": plan["source_revision"]["value"]},
                  *plan.get("reference_proofs", []), *plan.get("asset_proofs", [])]
        for proof in proofs:
            origin = _origin(str(proof["path"]))
            entry = await self.stat(context, origin_id=origin)
            if entry.origin_id != origin or (entry.revision or {}).get("value") != proof["fingerprint"]:
                raise FilesFacadeError("Managed batch input changed", code="resource_changed")

    async def prepare_managed_loose_item(self, context: ProviderContext, *, source_origin_id: str,
                                         destination_origin_id: str, expected_revision: Mapping[str, Any] | None,
                                         operation_id: str, item_id: str, collision: str,
                                         managed_root: Mapping[str, Any], reserved_targets: set[str] | None = None,
                                         destination_root: Mapping[str, Any] | None = None) -> Mapping[str, Any]:
        """Freeze one candidate without dispatching any native mutation."""
        source, folder = _path(source_origin_id), _path(destination_origin_id)
        selected_folder = managed_root.get("selected_folder")
        root = str(selected_folder.get("path") if isinstance(selected_folder, Mapping) else managed_root.get("root") or "")
        # A destination in a second, explicitly authorised root is only for one document.
        folder_outside = bool(root) and os.path.commonpath([root, folder]) != root
        if not root or os.path.commonpath([root, source]) != root or (folder_outside and destination_root is None and not self._MANAGED_CROSS_ROOT_FOLDER_MOVE_READY):
            raise FilesFacadeError("Selected root does not contain this transfer", code="resource_unavailable")
        reserved = self.operation_store.get_operation(owner_subject_id=str(context.owner_subject_id), operation_id=str(operation_id))
        items = ((reserved or {}).get("receipt") or {}).get("_managed_items") or {}
        existing = items.get(str(item_id)) if isinstance(items, Mapping) else None
        if existing is not None:
            plan = existing.get("plan") if isinstance(existing, Mapping) else None
            if (not isinstance(plan, Mapping) or plan.get("source") != source
                    or os.path.dirname(str(plan.get("destination") or "")) != folder
                    or plan.get("source_revision") != dict(expected_revision or {})
                    or plan.get("managed_root") != dict(managed_root)
                    or plan.get("generation") != int(context.policy_generation)):
                raise FilesFacadeError("Managed reservation changed", code="idempotency_conflict")
            if plan.get("kind") == "folder" and not self._MANAGED_FOLDER_MOVE_READY:
                raise FilesFacadeError("Managed folder move is not yet available", code="managed_media_move_unsupported")
            return dict(existing)
        current = await self.stat(context, origin_id=source_origin_id)
        is_folder = self._is_directory_kind(current.kind)
        if (current.kind != "file" and not is_folder) or dict(current.revision or {}) != dict(expected_revision or {}):
            raise FilesFacadeError("Managed source changed", code="resource_changed")
        if is_folder and (not self._MANAGED_FOLDER_MOVE_READY or (folder_outside and not self._MANAGED_CROSS_ROOT_FOLDER_MOVE_READY)):
            raise FilesFacadeError("Managed folder move is not yet available", code="managed_media_move_unsupported")
        client = self._client(context, self._scope(context))
        name = Path(source).name
        reserved = reserved_targets if reserved_targets is not None else set()
        for attempt in range(101):
            candidate = name if attempt == 0 else f"{Path(name).stem} ({attempt + 1}){Path(name).suffix}"
            target = os.path.join(folder, candidate)
            occupied = target in reserved
            if not occupied:
                try:
                    await client.request("stat", target, {"include_fingerprint": False})
                except FilesServiceError as exc:
                    if exc.code != "invalid_path":
                        raise
                else:
                    occupied = True
            if occupied:
                if collision == "rename":
                    continue
                raise FilesFacadeError("Host transfer destination already exists", code="resource_changed")
            prepare = self.prepare_managed_folder_move if is_folder else self.prepare_managed_workspace_move
            item = await prepare(context, source_origin_id=source_origin_id,
                destination_origin_id=_origin(target), expected_revision=dict(current.revision or {}),
                operation_id=str(operation_id), item_id=str(item_id), managed_root=managed_root,
                **({"destination_root": destination_root} if destination_root is not None and (not is_folder or folder_outside) else {}))
            reserved.add(target)
            return item
        raise FilesFacadeError("Host transfer destination is unavailable", code="resource_changed")

    async def transfer(
        self,
        context: ProviderContext,
        *,
        source_origin_id: str,
        destination_origin_id: str,
        operation: str,
        collision: str,
        expected_revision: Mapping[str, Any] | None = None,
        item_id: str | None = None,
        operation_id: str | None = None,
        managed_root: Mapping[str, Any] | None = None,
        prepared_managed: bool = False,
        destination_root: Mapping[str, Any] | None = None,
    ) -> ProviderResource | Mapping[str, Any]:
        """Move/copy one authorized host item into an authorized folder.

        Both origins were resolved and capability checked by FilesFacade.  The
        existing Rust/files service remains the mutation authority and receives
        server-derived paths only here, after that check.
        """
        if managed_root is not None:
            if not self._LOOSE_MANAGED_MOVE_READY:
                raise FilesFacadeError("Managed loose move is not yet available", code="managed_media_move_unsupported")
            if operation != "move" or not operation_id or not item_id:
                raise FilesFacadeError("Managed loose move is unavailable", code="managed_media_move_unsupported")
            item = (self.load_managed_workspace_item(context, operation_id=str(operation_id), item_id=str(item_id))
                    if prepared_managed else await self.prepare_managed_loose_item(context, source_origin_id=source_origin_id,
                        destination_origin_id=destination_origin_id, expected_revision=expected_revision,
                        operation_id=str(operation_id), item_id=str(item_id), collision=collision, managed_root=managed_root,
                        destination_root=destination_root))
            target = str(item["plan"]["destination"])
            result = await self.execute_managed_workspace_item(context, operation_id=str(operation_id), item_id=str(item_id),
                fresh_selected=managed_root, fresh_destination=destination_root)
            if result.get("outcome") != "committed":
                return result
            updated = await self.stat(context, origin_id=_origin(target))
            return replace(updated, provenance={**dict(updated.provenance), "selected_name": Path(target).name},
                action_receipt={"receipt_id": str(operation_id), "status": "complete", "phase": "complete",
                                "durable": True, "receipt": {"receipt_id": str(operation_id), "outcome": "committed"}})
        if operation not in {"move", "copy"}:
            raise FilesFacadeError("Host transfer operation is unavailable", code="unsupported_operation")
        source_path = _path(source_origin_id)
        destination_path = _path(destination_origin_id)
        scope = self._scope(context)
        try:
            current = await self._client(context, scope).request("stat", source_path, {"include_fingerprint": True})
            source_data = current.get("data") or {}
            if str(source_data.get("path") or source_path) != source_path:
                raise FilesFacadeError("Host source identity changed", code="resource_changed")
            fingerprint = self._fingerprint_value(source_data)
            if expected_revision and fingerprint and str(expected_revision.get("value") or "") != fingerprint:
                raise FilesFacadeError("Host resource revision is stale", code="resource_changed")
            name = Path(source_path).name
            candidate = name
            client = self._client(context, scope)
            for attempt in range(101):
                target = os.path.join(destination_path, candidate)
                # The service transfer contract only accepts destination and
                # fingerprint. Select Keep Both names here, as for imports;
                # every candidate still passes the service's exclusive write.
                candidate_operation = operation_id
                if attempt:
                    candidate_operation = "host-transfer-" + uuid.uuid5(
                        uuid.NAMESPACE_URL, f"{operation_id}:{item_id}:candidate:{attempt}"
                    ).hex
                payload = {
                    "destination": target,
                    "expected_fingerprint": {"algorithm": "sha256", "value": fingerprint} if fingerprint else None,
                    "operation_id": candidate_operation,
                    "item_id": item_id,
                }
                try:
                    async def preflight():
                        if operation == "move" and os.path.normpath(target) != os.path.normpath(source_path):
                            await self._preflight_managed_media_move(
                                context, source_path, target,
                                source_is_directory=self._is_directory_kind(str(source_data.get("kind") or "")),
                            )
                    response = await self._run_claimed_host_mutation(context, [source_path, target],
                        lambda: client.request(operation, source_path, payload), preflight=preflight)
                    break
                except FilesFacadeError as exc:
                    if (exc.code != "managed_media_move_unsupported" or operation != "move"
                            or self._is_directory_kind(str(source_data.get("kind") or ""))
                            or not operation_id or not item_id or not fingerprint):
                        raise
                    capabilities = await client.request("capabilities", source_path, {})
                    if "managed_file_move" not in (capabilities.get("operations") or []):
                        raise
                    if collision == "rename":
                        try:
                            await client.request("stat", target, {"include_fingerprint": False})
                        except FilesServiceError as stat_error:
                            if stat_error.code != "invalid_path":
                                raise
                        else:
                            if attempt >= 100:
                                raise FilesFacadeError("Host transfer destination already exists", code="resource_changed")
                            stem, suffix = os.path.splitext(name)
                            candidate = f"{stem} ({attempt + 2}){suffix}"
                            continue
                    await self.prepare_managed_workspace_move(context, source_origin_id=source_origin_id,
                        destination_origin_id=_origin(target), expected_revision={"kind": "hostFingerprint", "value": fingerprint},
                        operation_id=str(operation_id), item_id=str(item_id), destination_root=destination_root)
                    managed_result = await self.execute_managed_workspace_item(context, operation_id=str(operation_id), item_id=str(item_id),
                        fresh_destination=destination_root)
                    if managed_result.get("outcome") != "committed":
                        return managed_result
                    updated = await self.stat(context, origin_id=_origin(target))
                    return replace(updated, provenance={**dict(updated.provenance), "selected_name": Path(target).name},
                        action_receipt={"receipt_id": str(operation_id), "status": "complete", "phase": "complete",
                                        "durable": True, "receipt": {"receipt_id": str(operation_id), "outcome": "committed"}})
                except FilesServiceError as exc:
                    if exc.code != "conflict" or collision != "rename" or attempt >= 100:
                        raise
                    # A conflict may also mean a stale source fingerprint.
                    # Retry only a real occupied destination, with the same
                    # source revision; never turn a revision failure into rename.
                    checked = await client.request("stat", source_path, {"include_fingerprint": True})
                    checked_fingerprint = self._fingerprint_value(checked.get("data") or {})
                    if fingerprint and checked_fingerprint != fingerprint:
                        raise FilesFacadeError("Host resource revision is stale", code="resource_changed") from exc
                    try:
                        await client.request("stat", target, {})
                    except FilesServiceError:
                        raise FilesFacadeError("Host transfer conflict could not be resolved", code="resource_changed") from exc
                    stem, suffix = os.path.splitext(name)
                    candidate = f"{stem} ({attempt + 2}){suffix}"
        except FilesServiceError as exc:
            if exc.code == "conflict":
                raise FilesFacadeError("Host resource changed or destination already exists", code="resource_changed") from exc
            raise _service_error(exc) from exc
        result_data = response.get("data") or {}
        resolved = str(result_data.get("path") or target)
        updated = await self.stat(context, origin_id=_origin(resolved))
        receipt_id = str(operation_id or f"host-transfer-{uuid.uuid4().hex}")
        selected_name = Path(resolved).name or Path(source_path).name
        revision = dict(updated.revision) if isinstance(updated.revision, Mapping) else None
        provenance = dict(updated.provenance)
        provenance["selected_name"] = selected_name
        return replace(updated, provenance=provenance, revision=revision, action_receipt={
            "receipt_id": receipt_id,
            "status": "complete",
            "phase": "complete",
            "durable": True,
            "receipt": {"receipt_id": receipt_id, "outcome": "committed", "selected_name": selected_name, "revision": revision},
        })

    @staticmethod
    def _is_directory_kind(kind: str) -> bool:
        return str(kind or "").casefold() in {"directory", "folder", "recursive_directory"}

    def load_managed_workspace_item(self, context: ProviderContext, *, operation_id: str, item_id: str) -> Mapping[str, Any]:
        """Load exact private recovery identity without touching the old source."""
        store = self.operation_store
        getter = getattr(store, "get_operation", None)
        generation = getattr(store, "generation", None)
        if not callable(getter) or not callable(generation):
            raise FilesFacadeError("Managed recovery is unavailable", code="provider_unavailable")
        self._scope(context)
        loaded = getter(owner_subject_id=str(context.owner_subject_id), operation_id=str(operation_id))
        receipt = loaded.get("receipt") if isinstance(loaded, Mapping) else None
        if isinstance(receipt, Mapping) and "_managed_batch_ready" in receipt and receipt.get("_managed_batch_ready") is not True:
            raise FilesFacadeError("Managed batch preparation is pending", code="operation_pending")
        items = receipt.get("_managed_items") if isinstance(receipt, Mapping) else None
        entry = items.get(str(item_id)) if isinstance(items, Mapping) else None
        plan = entry.get("plan") if isinstance(entry, Mapping) else None
        native_id = "host-managed-" + hashlib.sha256(
            json.dumps(["host-managed-v1", str(context.owner_subject_id), str(operation_id), str(item_id)], separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        ).hexdigest()
        if (not isinstance(plan, Mapping) or plan.get("native_operation_id") != native_id
                or plan.get("workspace_id") != str(context.workspace_id)
                or plan.get("generation") != int(context.policy_generation)
                or int(generation()) not in (0, int(context.policy_generation))):
            raise FilesFacadeError("Managed recovery identity is unavailable", code="resource_unavailable")
        return dict(entry)

    async def _cross_root_scope(self, context: ProviderContext, plan: Mapping[str, Any], *, owner: str,
                                source_scope: dict[str, Any], source_narrowed: bool, phase: str = "complete") -> dict[str, Any]:
        """Re-prove the destination root of a cross-root plan; return the scope the native client runs under.

        Raises on any change. A workspace source runs under the ambient scope, which prepare already proved
        writable for every destination path; a narrowed source joins the destination root's own narrowed scope.
        """
        store = self.operation_store
        droot, binding = os.path.normpath(str(plan["destination_frame"]["canonical_root"])), plan.get("destination_root")
        location = store.get_location(str(plan["destination_location_id"]))
        if not location.enabled or location.availability != "available" or location.revision != plan["destination_location_revision"]:
            raise ValueError("destination location changed")
        dest_scope = None
        if binding is None:
            workspace = store.get_workspace(str(context.workspace_id))
            if (str(workspace.owner_subject_id) != owner or workspace.archived or workspace.revision != plan["destination_workspace_revision"]
                    or str(workspace.location_id) != str(location.id)
                    or os.path.normpath(os.path.join(str(location.canonical_path), str(workspace.relative_folder or ""))) != droot):
                raise ValueError("destination workspace changed")
        else:
            witness, expected = binding.get("selected_folder"), dict(binding)
            if witness is not None and phase in {"prepared", "claimed"}:
                renewed = await self.authorize_selected_folder(context, origin_id=_origin(str(witness["path"])), expected_revision=witness["revision"])
            else:
                # The folder's own revision moves with the native commit, so once dispatched only its root is re-proved.
                expected.pop("selected_folder", None)
                renewed = await self.authorize_managed_root(context, origin_id=_origin(str(binding["root"])), expected_revision=None)
            if dict(renewed) != expected:
                raise ValueError("destination root changed")
            dest_scope = dict(renewed["scope"])
        if not source_narrowed:
            return source_scope
        if dest_scope is None:
            dest_scope = dict((await self.authorize_managed_root(context, origin_id=_origin(str(location.canonical_path)), expected_revision=None))["scope"])
        ids = list(dict.fromkeys([*(source_scope.get("visible_root_ids") or []), *(dest_scope.get("visible_root_ids") or [])]))
        return {**source_scope, "visible_root_ids": ids,
                "root_capabilities": {**(dest_scope.get("root_capabilities") or {}), **(source_scope.get("root_capabilities") or {})}}

    async def execute_managed_workspace_item(self, context: ProviderContext, *, operation_id: str, item_id: str,
                                             fresh_selected: Mapping[str, Any] | None = None,
                                             fresh_destination: Mapping[str, Any] | None = None) -> Mapping[str, Any]:
        """Execute or reconcile the exact frozen item without rebuilding its plan."""
        store = self.operation_store
        item = self.load_managed_workspace_item(context, operation_id=operation_id, item_id=item_id)
        plan = item["plan"]
        if plan.get("selected_folder") is not None and item.get("phase") != "complete" and (not isinstance(fresh_selected, Mapping) or
                dict(fresh_selected) != dict(plan.get("managed_root") or {})):
            return {"outcome": "pending", "code": "managed_needs_authority"}
        destination_binding = plan.get("destination_root")
        if (isinstance(destination_binding, Mapping) and destination_binding.get("selected_folder") is not None
                and item.get("phase") in {"prepared", "claimed"} and (not isinstance(fresh_destination, Mapping)
                                                        or dict(fresh_destination) != dict(destination_binding))):
            return {"outcome": "pending", "code": "managed_needs_authority"}
        is_folder = plan.get("kind") == "folder"
        native_operation = "managed_folder_move" if is_folder else "managed_file_move"
        owner, native_id = str(context.owner_subject_id), str(plan["native_operation_id"])
        try:
            chosen = plan.get("managed_root")
            workspace = None if chosen is not None else store.get_workspace(str(context.workspace_id))
            location = store.get_location(str(chosen["root_id"])) if chosen is not None else store.get_location(workspace.location_id)
            root = (str(plan["root"]) if plan.get("selected_folder") is not None else
                    os.path.normpath(os.path.join(str(location.canonical_path), str(workspace.relative_folder or "") if workspace is not None else "")))
            if ((workspace is not None and (str(workspace.owner_subject_id) != owner or workspace.archived)) or not location.enabled
                    or location.availability != "available" or root != plan.get("root")
                    or (workspace.revision if workspace is not None else None) != plan.get("workspace_revision") or str(location.id) != plan.get("location_id")
                    or location.revision != plan.get("location_revision")):
                raise ValueError("workspace changed")
            ambient_scope = self._scope(context)
            if chosen is not None:
                if plan.get("selected_folder") is not None:
                    witness = plan["selected_folder"]
                    renewed = await self.authorize_selected_folder(context, origin_id=_origin(str(witness["path"])), expected_revision=witness["revision"])
                else:
                    renewed = await self.authorize_managed_root(context, origin_id=_origin(root), expected_revision=None)
                if dict(renewed) != dict(chosen):
                    raise ValueError("selected root changed")
                scope = dict(renewed["scope"])
            else:
                scope = ambient_scope
            cross = plan.get("destination_frame") is not None
            if cross:
                scope = await self._cross_root_scope(context, plan, owner=owner, source_scope=scope, source_narrowed=chosen is not None,
                                                     phase=str(item.get("phase") or ""))
            payload = plan["payload"]
            if is_folder:
                references, assets = payload["references"], payload["assets"]
                if not isinstance(references, list) or not isinstance(assets, list):
                    raise ValueError("invalid folder participants")
                paths = [plan["source"], plan["destination"]]
                paths += [part["path"] for part in references]
                paths += [path for part in assets for path in (part["path"], part["destination"])]
                writable = [plan["source"], plan["destination"]]
                writable += [part["path"] for part in references if part.get("replacement") is not None]
                writable += [path for part in assets if part["path"] != part["destination"]
                             for path in (part["path"], part["destination"])]
            else:
                participants = payload["participants"]
                if not isinstance(participants, list) or not participants:
                    raise ValueError("invalid participants")
                paths = [path for part in participants for path in (part.get("source"), part.get("destination")) if path is not None]
                writable = paths
            # Destinations of a cross-root plan belong to the destination root; everything else to the source root.
            dest_paths = {part["destination"] for part in payload.get("participants", []) if part.get("destination") is not None} if cross else set()
            droot = os.path.normpath(str(plan["destination_frame"]["canonical_root"])) if cross else root
            if cross and is_folder:
                dest_paths = {path for path in paths if isinstance(path,str) and os.path.commonpath([droot,path]) == droot}
            if (any(not isinstance(path, str) or os.path.commonpath([droot if path in dest_paths else root, path]) != (droot if path in dest_paths else root)
                    for path in paths)
                    or any(not self._path_writable(context, path, ambient_scope) for path in writable)):
                raise ValueError("scope changed")
            if plan.get("selected_folder") is not None:
                self._validate_selected_move_paths(context, root, [p for p in paths if p not in dest_paths] + [part["source"] for part in payload.get("guards", [])])
            if cross and plan["destination_frame"]["origin"] == "selected_root":
                self._validate_selected_move_paths(context, droot, sorted(dest_paths))
        except Exception as exc:
            raise FilesFacadeError("Managed workspace authority changed", code="resource_unavailable") from exc
        phase = str(item.get("phase") or "")
        if phase == "complete":
            return {"outcome": "committed", "cleanup": "complete" if item.get("cleanup_phase") == "complete" else "pending"}
        if phase == "rejected":
            return {"outcome": "failed", "code": "resource_changed"}
        if phase == "needs_review":
            return {"outcome": "pending", "code": "managed_" + str(item.get("review_outcome") or "needs_review")}
        if phase in {"prepared", "claimed", "dispatched"}:
            try:
                capabilities = await self._client(context, scope).request("capabilities", str(plan["root"]), {})
                if native_operation not in (capabilities.get("operations") or []):
                    return {"outcome": "pending", "code": "managed_capability_unavailable"}
            except Exception:
                return {"outcome": "pending", "code": "managed_capability_unavailable"}
        if phase in {"prepared", "claimed"}:
            try:
                item = store.claim_managed_transfer_item(owner_subject_id=owner, operation_id=str(operation_id),
                    item_id=str(item_id), native_operation_id=native_id, generation=int(context.policy_generation))
            except Exception as exc:
                code = getattr(exc, "code", "provider_unavailable")
                return {"outcome": "pending" if code == "operation_pending" else "failed", "code": code}
            if not item.get("acquired"):
                return {"outcome": "pending", "code": "operation_in_progress"}
            claim_token = str(item.get("claim_token") or "")
            try:
                if is_folder:
                    current = await self.stat(context, origin_id=_origin(str(plan["source"])))
                    if (current.origin_id != _origin(str(plan["source"]))
                            or dict(current.revision or {}) != plan["source_revision"]
                            or dict(await self.inspect_managed_folder(context, origin_id=current.origin_id, managed_root=chosen))
                            != plan["payload"]["expected_snapshot"]):
                        raise FilesFacadeError("Managed folder changed", code="resource_changed")
                    proofs = [*plan.get("reference_proofs", []), *plan.get("asset_proofs", [])]
                else:
                    proofs = [{"path": plan["source"], "fingerprint": plan["source_revision"]["value"]},
                              *plan.get("reference_proofs", []), *plan.get("asset_proofs", [])]
                for proof in proofs:
                    current = await self.stat(context, origin_id=_origin(str(proof["path"])))
                    if current.origin_id != _origin(str(proof["path"])) or (current.revision or {}).get("value") != proof["fingerprint"]:
                        raise FilesFacadeError("Managed input changed", code="resource_changed")
                if plan.get("selected_folder") is not None:
                    witness = plan["selected_folder"]
                    if dict(await self.authorize_selected_folder(context, origin_id=_origin(str(witness["path"])),
                                                                 expected_revision=witness["revision"])) != dict(chosen):
                        raise FilesFacadeError("Selected folder changed", code="resource_changed")
                if cross:
                    # A device change since planning is refused here, before `dispatched`, so the unstarted item is retired.
                    if is_folder:
                        paths = [plan["source"], plan["destination"]]
                        for asset in plan["payload"]["assets"]:
                            paths.extend((asset["path"], asset["destination"]))
                        devices = {_device_of(os.path.dirname(path)) for path in paths}
                    else:
                        parts = plan["payload"]["participants"]
                        devices = {_device_of(os.path.dirname(part["source"])) for part in parts}
                        devices |= {_device_of(os.path.dirname(part["destination"])) for part in parts if part["destination"] is not None}
                    if len(devices) > 1:
                        raise FilesFacadeError(_CROSS_DISK_MESSAGE, code=_CROSS_DISK_CODE)
                store.mark_managed_transfer_item(owner_subject_id=owner, operation_id=str(operation_id), item_id=str(item_id),
                    native_operation_id=native_id, claim_token=claim_token, phase="dispatched")
            except Exception as exc:
                try:
                    store.reject_unstarted_managed_transfer_item(owner_subject_id=owner, operation_id=str(operation_id),
                        item_id=str(item_id), native_operation_id=native_id, claim_token=claim_token)
                except Exception:
                    return {"outcome": "pending", "code": "managed_reconciliation_required"}
                return {"outcome": "failed", "code": getattr(exc, "code", "resource_changed")}
            phase = "dispatched"
        claim_token = str(item.get("claim_token") or "")

        async def settle_exact() -> Mapping[str, Any]:
            current_phase = phase
            if current_phase == "dispatched":
                try:
                    response = await self._client(context, scope).request(native_operation, str(plan["source"]),
                        dict(plan["payload"]), root_operation_id=native_id)
                    data = response.get("data") if isinstance(response, Mapping) else None
                    if not isinstance(data, Mapping) or data.get("operation_id") != native_id:
                        return {"outcome": "pending", "code": "managed_reconciliation_required"}
                    if data.get("status") in {"rejected_needs_review", "partial_needs_review", "committed_needs_review"}:
                        review = str(data["status"])
                        store.mark_managed_transfer_item(owner_subject_id=owner, operation_id=str(operation_id), item_id=str(item_id),
                            native_operation_id=native_id, claim_token=claim_token, phase="needs_review", review_outcome=review)
                        return {"outcome": "pending", "code": "managed_" + review}
                    if data.get("status") != "committed":
                        return {"outcome": "pending", "code": "managed_reconciliation_required"}
                    store.mark_managed_transfer_item(owner_subject_id=owner, operation_id=str(operation_id), item_id=str(item_id),
                        native_operation_id=native_id, claim_token=claim_token, phase="native_committed")
                except Exception:
                    return {"outcome": "pending", "code": "managed_reconciliation_required"}
            elif current_phase not in {"native_committed", "metadata_pending"}:
                return {"outcome": "pending", "code": "managed_reconciliation_required"}
            try:
                complete = store.settle_managed_transfer_item(owner_subject_id=owner, operation_id=str(operation_id),
                    item_id=str(item_id), native_operation_id=native_id, claim_token=claim_token)
            except Exception:
                return {"outcome": "pending", "code": "managed_metadata_pending"}
            if not complete:
                return {"outcome": "pending", "code": "managed_metadata_pending"}
            latest = self.load_managed_workspace_item(context, operation_id=operation_id, item_id=item_id)
            return {"outcome": "committed", "cleanup": "complete" if latest.get("cleanup_phase") == "complete" else "pending"}

        task = asyncio.create_task(settle_exact())
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            task.add_done_callback(lambda finished: finished.exception() if not finished.cancelled() else None)
            raise

    async def undo_managed_cross_root_move(self, context: ProviderContext, *, operation_id: str, item_id: str,
                                           undo_native_operation_id: str) -> Mapping[str, Any]:
        """Undo one COMPLETE cross-root file move. Only the two operation ids go to Native: it derives every participant (document, assets,
        reference edits) from the forward journal and refuses on any drift or when that journal is gone, never from this request."""
        item = self.load_managed_workspace_item(context, operation_id=operation_id, item_id=item_id)
        plan = item["plan"]
        undo_id = str(undo_native_operation_id)
        if plan.get("kind") == "folder":
            if (not self._MANAGED_CROSS_ROOT_FOLDER_MOVE_READY or item.get("phase") != "complete"
                    or plan.get("destination_frame") is None or plan.get("selected_folder") is not None
                    or ((plan.get("destination_root") or {}).get("selected_folder") is not None)
                    or not re.fullmatch(r"[A-Za-z0-9_-]{16,80}", undo_id) or undo_id == plan.get("native_operation_id")):
                return {"outcome": "failed", "code": "managed_undo_unavailable"}
            owner, chosen = str(context.owner_subject_id), plan.get("managed_root")
            store = self.operation_store
            try:
                if not isinstance(chosen, Mapping):
                    raise ValueError("source root unavailable")
                renewed = await self.authorize_managed_root(context, origin_id=_origin(str(plan["root"])), expected_revision=None)
                if dict(renewed) != dict(chosen):
                    raise ValueError("source root changed")
                scope = await self._cross_root_scope(context, plan, owner=owner, source_scope=dict(renewed["scope"]), source_narrowed=True)
                claim = store.claim_managed_folder_undo(owner_subject_id=owner, operation_id=str(operation_id), item_id=str(item_id),
                    native_operation_id=str(plan["native_operation_id"]), undo_operation_id=undo_id, generation=int(context.policy_generation))
            except Exception as exc:
                return {"outcome": "pending" if getattr(exc, "code", "") == "operation_pending" else "failed",
                        "code": getattr(exc, "code", "managed_undo_unavailable")}
            if claim.get("phase") == "complete":
                return {"outcome": "committed"}
            if claim.get("phase") == "refused":
                return {"outcome": "failed", "code": "managed_undo_unavailable"}

            async def settle_inverse() -> Mapping[str, Any]:
                try:
                    store.finish_managed_folder_undo(owner_subject_id=owner, operation_id=str(operation_id), item_id=str(item_id),
                        undo_operation_id=undo_id, outcome="dispatched")
                    request = {"operation_id":undo_id, "source":str(plan["destination"]), "destination":str(plan["source"]),
                        "expected_snapshot":{"digest":"", "count":0, "bytes":0}, "references":[], "assets":[],
                        "inverse_of":str(plan["native_operation_id"])}
                    try:
                        response = await self._client(context, scope).request("managed_folder_move", str(plan["destination"]), request, root_operation_id=undo_id)
                    except FilesServiceError:
                        # A prior partial inverse may now reject execution; the
                        # existing status request can still expose its journal.
                        response = await self._client(context, scope).request("managed_folder_move_status", str(plan["destination"]), request, root_operation_id=undo_id)
                    data = response.get("data") if isinstance(response, Mapping) else None
                    if isinstance(data, Mapping) and data.get("operation_id") == undo_id and data.get("status") == "partial_needs_review":
                        return {"outcome":"pending", "code":"managed_partial_needs_review"}
                    if not isinstance(data, Mapping) or data.get("operation_id") != undo_id or data.get("status") != "committed":
                        return {"outcome":"pending", "code":"managed_reconciliation_required"}
                    complete = store.finish_managed_folder_undo(owner_subject_id=owner, operation_id=str(operation_id), item_id=str(item_id),
                        undo_operation_id=undo_id, outcome="committed")
                    return {"outcome":"committed"} if complete else {"outcome":"pending", "code":"managed_metadata_pending"}
                except Exception:
                    return {"outcome":"pending", "code":"managed_reconciliation_required"}
            task = asyncio.create_task(settle_inverse())
            try:
                return await asyncio.shield(task)
            except asyncio.CancelledError:
                task.add_done_callback(lambda finished: finished.exception() if not finished.cancelled() else None)
                raise
        if (item.get("phase") != "complete" or plan.get("kind") == "folder" or plan.get("destination_frame") is None
                or plan.get("selected_folder") is not None or not re.fullmatch(r"[A-Za-z0-9_-]{16,80}", undo_id) or undo_id == plan.get("native_operation_id")):
            return {"outcome": "failed", "code": "managed_undo_unavailable"}
        owner, chosen = str(context.owner_subject_id), plan.get("managed_root")
        try:
            if chosen is not None:
                renewed = await self.authorize_managed_root(context, origin_id=_origin(str(plan["root"])), expected_revision=None)
                if dict(renewed) != dict(chosen):
                    raise ValueError("selected root changed")
                scope = dict(renewed["scope"])
            else:
                scope = self._scope(context)
            scope = await self._cross_root_scope(context, plan, owner=owner, source_scope=scope, source_narrowed=chosen is not None)
            moved_document = next(part["destination"] for part in plan["payload"]["participants"]
                                  if part.get("kind") == "document" and part.get("destination") is not None)
        except Exception:
            return {"outcome": "failed", "code": "managed_undo_unavailable"}
        try:
            response = await self._client(context, scope).request("managed_file_move", str(moved_document),
                {"operation_id": undo_id, "inverse_of": str(plan["native_operation_id"])}, root_operation_id=undo_id)
        except FilesServiceError as exc:
            if exc.code == "unsupported" and str(exc).startswith("managed_undo_"):
                return {"outcome": "failed", "code": str(exc)}       # a typed refusal: nothing was changed
            if exc.code == "conflict":
                return {"outcome": "failed", "code": "resource_changed"}   # drift since the forward move: refused before any mutation
            return {"outcome": "pending", "code": "managed_reconciliation_required"}
        except Exception:
            return {"outcome": "pending", "code": "managed_reconciliation_required"}
        data = response.get("data") if isinstance(response, Mapping) else None
        if not isinstance(data, Mapping) or data.get("operation_id") != undo_id or data.get("status") != "committed":
            return {"outcome": "pending", "code": "managed_reconciliation_required"}
        return {"outcome": "committed"}

    async def cleanup_managed_folder_item(self, context: ProviderContext, *, operation_id: str, item_id: str,
                                          fresh_root: Any = None) -> Mapping[str, Any]:
        """Explicitly reconcile cleanup for one exact, already committed item."""
        item = self.load_managed_workspace_item(context, operation_id=operation_id, item_id=item_id)
        plan = item.get("plan") or {}
        from src.openclank.file_policy import is_closed_managed_file_plan
        is_file = is_closed_managed_file_plan(plan)
        if (plan.get("kind") != "folder" and not is_file) or item.get("phase") != "complete":
            raise FilesFacadeError("Managed folder cleanup is unavailable", code="resource_unavailable")
        chosen = plan.get("managed_root")
        if not isinstance(chosen, Mapping) and not (is_file and chosen is None):
            raise FilesFacadeError("Managed folder root is unavailable", code="resource_unavailable")
        if plan.get("selected_folder") is not None:
            if not callable(fresh_root):
                raise FilesFacadeError("Selected folder cleanup requires fresh authority", code="operation_pending")
            async def current_authority():
                current = dict(await fresh_root())
                selected = current.pop("selected_folder", None)
                frozen = dict(chosen); old_selected = frozen.pop("selected_folder", None)
                if (not isinstance(selected, Mapping) or not isinstance(old_selected, Mapping)
                        or selected.get("path") != old_selected.get("path") or current != frozen):
                    raise FilesFacadeError("Selected folder changed", code="resource_changed")
                return {**current, "selected_folder":dict(selected)}
            current = await current_authority()
        elif chosen is None:
            if (await self.execute_managed_workspace_item(context, operation_id=operation_id, item_id=item_id)).get("outcome") != "committed":
                raise FilesFacadeError("Managed workspace authority changed", code="resource_unavailable")
            current = {"root":str(self.operation_store.get_location(str(plan["location_id"])).canonical_path), "scope":self._scope(context)}
        else:
            if (await self.execute_managed_workspace_item(context, operation_id=operation_id, item_id=item_id)).get("outcome") != "committed":
                raise FilesFacadeError("Managed folder authority changed", code="resource_unavailable")
            current = await self.authorize_managed_root(context, origin_id=_origin(str(plan["root"])), expected_revision=None)
            if dict(current) != dict(chosen):
                raise FilesFacadeError("Managed folder root changed", code="resource_changed")
        owner, native_id = str(context.owner_subject_id), str(plan["native_operation_id"])
        try:
            claim = self.operation_store.claim_managed_folder_cleanup(owner_subject_id=owner, operation_id=str(operation_id),
                item_id=str(item_id), native_operation_id=native_id, generation=int(context.policy_generation))
        except Exception as exc:
            raise FilesFacadeError("Managed folder cleanup is unavailable", code=getattr(exc, "code", "provider_unavailable")) from exc
        if not claim.get("acquired"):
            return {"outcome":"committed", "cleanup":"complete"}
        token = str(claim.get("cleanup_token") or "")
        try:
            self.operation_store.mark_managed_folder_cleanup(owner_subject_id=owner, operation_id=str(operation_id),
                item_id=str(item_id), native_operation_id=native_id, token=token, phase="dispatched")
        except Exception as exc:
            raise FilesFacadeError("Managed folder cleanup is unavailable", code=getattr(exc, "code", "provider_unavailable")) from exc

        async def run() -> Mapping[str, Any]:
            try:
                if plan.get("selected_folder") is not None:
                    current = await current_authority()
                elif chosen is None:
                    if (await self.execute_managed_workspace_item(context, operation_id=operation_id, item_id=item_id)).get("outcome") != "committed":
                        raise ValueError("workspace changed")
                    current = {"root":str(self.operation_store.get_location(str(plan["location_id"])).canonical_path), "scope":self._scope(context)}
                else:
                    current = await self.authorize_managed_root(context, origin_id=_origin(str(plan["root"])), expected_revision=None)
                    if dict(current) != dict(chosen):
                        raise ValueError("root changed")
                native_scope = dict(current["scope"])
                if is_file and plan.get("destination_frame") is not None:
                    # Cleanup targets the SOURCE root; the native service also pins the destination root.
                    native_scope = await self._cross_root_scope(context, plan, owner=owner, source_scope=native_scope,
                                                                source_narrowed=chosen is not None)
                client = self._client(context, native_scope)
                operation = "managed_file_cleanup" if is_file else "managed_folder_cleanup"
                if is_file:
                    capabilities = await client.request("capabilities", str(current["root"]), {})
                    if operation not in (capabilities.get("operations") or []):
                        raise ValueError("cleanup capability unavailable")
                    if plan.get("selected_folder") is not None:
                        current = await current_authority()
                    elif chosen is None:
                        if (await self.execute_managed_workspace_item(context, operation_id=operation_id, item_id=item_id)).get("outcome") != "committed":
                            raise ValueError("workspace changed")
                    elif dict(await self.authorize_managed_root(context, origin_id=_origin(str(plan["root"])), expected_revision=None)) != dict(chosen):
                        raise ValueError("root changed")
                response = await client.request(operation, str(current["root"]), dict(plan["payload"]) if is_file else {}, root_operation_id=native_id)
                result = response.get("data") if isinstance(response, Mapping) else None
                if (isinstance(result, Mapping) and result.get("operation_id") == native_id
                        and result.get("status") == "committed" and result.get("cleanup") == "complete"):
                    self.operation_store.mark_managed_folder_cleanup(owner_subject_id=owner, operation_id=str(operation_id),
                        item_id=str(item_id), native_operation_id=native_id, token=token, phase="complete")
                    return {"outcome":"committed", "cleanup":"complete"}
            except Exception:
                pass
            # Unknown and incomplete replies retain the exact operation/token;
            # the native journal serializes retries over only its private names.
            try:
                self.operation_store.mark_managed_folder_cleanup(owner_subject_id=owner, operation_id=str(operation_id),
                    item_id=str(item_id), native_operation_id=native_id, token=token, phase="pending")
            except Exception:
                pass
            return {"outcome":"committed", "cleanup":"pending"}

        task = asyncio.create_task(run())
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            task.add_done_callback(lambda finished: finished.exception() if not finished.cancelled() else None)
            raise

    def _destination_move_frame(self, context: ProviderContext, *, owner: str, source_root: str, destination: str,
                                managed_root: Mapping[str, Any] | None, destination_root: Mapping[str, Any] | None) -> dict[str, Any]:
        """Authorise the root a document leaves its own root for, or refuse.

        Only two authorities exist: the context workspace root (for a loose or
        selected-root document) and the explicit second root the client supplied
        and the existing root authorisation resolved. No registry search.
        """
        from src.openclank.media_ownership import MoveFrame
        store = self.operation_store
        refusal = FilesFacadeError("Managed move across roots is unavailable", code="managed_media_move_unsupported")

        def inside(base: str, path: str) -> bool:
            try:
                return os.path.commonpath([base, path]) == base
            except ValueError:
                return False

        found = None
        if managed_root is not None:
            try:
                workspace = store.get_workspace(str(context.workspace_id))
                location = store.get_location(workspace.location_id)
                relative = str(workspace.relative_folder or "").replace("\\", "/")
                if (relative.startswith("/") or any(part == ".." for part in relative.split("/"))
                        or str(workspace.owner_subject_id) != owner or workspace.archived or not location.enabled
                        or location.availability != "available" or not isinstance(workspace.revision, int)
                        or not isinstance(location.revision, int)):
                    raise ValueError("workspace unavailable")
                workspace_root = os.path.normpath(os.path.join(str(location.canonical_path), relative))
                if inside(workspace_root, destination):
                    found = {"origin": "workspace", "root": workspace_root, "workspace": workspace, "location": location, "binding": None}
            except Exception:
                found = None
        if found is None and isinstance(destination_root, Mapping):
            try:
                selected = destination_root.get("selected_folder")
                root = os.path.normpath(str(selected["path"] if isinstance(selected, Mapping) else destination_root.get("root") or ""))
                location = store.get_location(str(destination_root["root_id"]))
                if (not os.path.isabs(root) or not inside(root, destination) or not location.enabled or location.availability != "available"
                        or location.revision != destination_root.get("location_revision")
                        or destination_root.get("generation") != int(context.policy_generation)
                        or not inside(str(location.canonical_path), root)):
                    raise ValueError("destination root unavailable")
                found = {"origin": "selected_root" if selected else "loose", "root": root, "workspace": None,
                         "location": location, "binding": dict(destination_root)}
            except Exception:
                found = None
        if found is None or inside(source_root, found["root"]) or inside(found["root"], source_root):
            raise refusal
        found["frame"] = MoveFrame(found["origin"], found["root"], 1 if found["origin"] == "loose" else 2,
                                   str(context.workspace_id) if found["origin"] == "workspace" else None)
        return found

    async def authorize_destination_root(self, context: ProviderContext, *, origin_id: str,
                                         expected_revision: Mapping[str, Any] | None) -> Mapping[str, Any]:
        """Authorise a client-supplied second root with the existing rules: a registered root exactly is a loose
        root, a folder inside one is a selected folder."""
        path = _path(origin_id)
        registered = any(loc.kind == "directory" and os.path.normpath(str(loc.canonical_path)) == path
                         for loc in self.operation_store.list_locations())
        if registered:
            return await self.authorize_managed_root(context, origin_id=origin_id, expected_revision=expected_revision)
        return await self.authorize_selected_folder(context, origin_id=origin_id, expected_revision=expected_revision)

    def managed_cross_root_folder_available(self, context: ProviderContext, *, source_origin_id: str,
                                            destination_origin_id: str) -> bool:
        return bool(self._MANAGED_FOLDER_MOVE_READY and self._MANAGED_CROSS_ROOT_FOLDER_MOVE_READY
            and self.managed_destination_root_required(context, source_origin_id=source_origin_id,
                destination_origin_id=destination_origin_id, source_is_directory=True))

    def managed_destination_root_required(self, context: ProviderContext, *, source_origin_id: str,
                                          destination_origin_id: str, source_is_directory: bool) -> bool:
        """True when a managed document would leave its root for a place the context workspace does not cover.

        A loose document's root is chosen by the client, so it is not classified here; a move that leaves it
        without a destination root is refused at prepare.
        """
        from src.openclank.media_ownership import MediaProvenance
        if source_is_directory and not self._MANAGED_CROSS_ROOT_FOLDER_MOVE_READY:
            return False
        source, destination = _path(source_origin_id), _path(destination_origin_id)
        rows = self._managed_move_rows(context, source, source_is_directory=source_is_directory)
        records = [MediaProvenance.from_receipt(row["receipt"]["provenance"]) for row in rows]
        if not records or any(record.origin == "loose" for record in records):
            return False

        def inside(base: str, path: str) -> bool:
            try:
                return os.path.commonpath([base, path]) == base
            except ValueError:
                return False

        try:
            workspace = self.operation_store.get_workspace(str(context.workspace_id))
            location = self.operation_store.get_location(workspace.location_id)
            workspace_root = os.path.normpath(os.path.join(str(location.canonical_path), str(workspace.relative_folder or "")))
        except Exception:
            workspace_root = None
        has_selected = any(record.origin == "selected_root" for record in records)
        source_roots = {os.path.normpath(record.canonical_root) for record in records if record.origin == "selected_root"}
        if any(record.origin == "workspace" for record in records) and workspace_root:
            source_roots.add(workspace_root)
        if not source_roots or any(inside(root, destination) for root in source_roots):
            return False
        # A selected-root document may also move into the context workspace without a second root.
        return not (has_selected and workspace_root and inside(workspace_root, destination))

    async def prepare_managed_workspace_move(
        self, context: ProviderContext, *, source_origin_id: str, destination_origin_id: str,
        expected_revision: Mapping[str, Any], operation_id: str, item_id: str,
        managed_root: Mapping[str, Any] | None = None,
        destination_root: Mapping[str, Any] | None = None,
    ) -> Mapping[str, Any]:
        """Freeze an authenticated workspace/v2 file plan; never mutate files.

        Only server-owned provenance may supply historical reference locators.
        The returned private plan belongs to the existing reserved transfer item.
        """
        from src.openclank.media_ownership import MediaProvenance, MovePreflight, plan_document_move
        from src.openclank.resource_refs import resolve_resource_ref_for_reissue
        from src.openclank.files_service_client import OdysseusFilesClient, _MAX_FRAME_BYTES

        owner = str(context.owner_subject_id)
        source, destination = _path(source_origin_id), _path(destination_origin_id)
        source, destination = os.path.normpath(source), os.path.normpath(destination)
        if sys.platform != "linux" or not operation_id or not item_id or source == destination:
            raise FilesFacadeError("Managed move is unavailable", code="managed_media_move_unsupported")
        store = self.operation_store
        if not all(callable(getattr(store, name, None)) for name in
                   ("get_workspace", "get_location", "generation", "get_operation", "list_host_media_operations", "freeze_managed_transfer_item")):
            raise FilesFacadeError("Managed move authority is unavailable", code="provider_unavailable")
        cross_destination = False
        try:
            workspace = None if managed_root is not None else store.get_workspace(str(context.workspace_id))
            location = store.get_location(str(managed_root["root_id"])) if managed_root is not None else store.get_location(workspace.location_id)
            relative = "" if workspace is None else str(workspace.relative_folder or "").replace("\\", "/")
            if relative.startswith("/") or any(part == ".." for part in relative.split("/")):
                raise ValueError("invalid workspace folder")
            selected_folder = managed_root.get("selected_folder") if managed_root is not None else None
            root = (str(selected_folder["path"]) if isinstance(selected_folder, Mapping) else
                    os.path.normpath(os.path.join(str(location.canonical_path), relative)))
            if ((workspace is not None and (str(workspace.owner_subject_id) != owner or workspace.archived)) or not location.enabled
                    or location.availability != "available" or not os.path.isabs(root)
                    or (workspace is not None and not isinstance(workspace.revision, int)) or not isinstance(location.revision, int)
                    or os.path.commonpath([root, source]) != root
                    or os.path.commonpath([str(location.canonical_path), root]) != str(location.canonical_path)):
                raise ValueError("workspace scope changed")
            cross_destination = os.path.commonpath([root, destination]) != root
            if managed_root is not None and ((not selected_folder and managed_root.get("root") != root) or managed_root.get("location_revision") != location.revision
                                             or managed_root.get("generation") != int(context.policy_generation)):
                raise ValueError("selected root changed")
            current_generation = int(store.generation())
            if current_generation not in (0, int(context.policy_generation)):
                raise ValueError("policy changed")
        except Exception as exc:
            raise FilesFacadeError("Managed workspace authority is unavailable", code="provider_unavailable") from exc
        dest_frame = None
        if cross_destination:
            dest_frame = self._destination_move_frame(context, owner=owner, source_root=root, destination=destination,
                                                      managed_root=managed_root, destination_root=destination_root)
        scope = self._scope(context)
        if not self._path_writable(context, source, scope) or not self._path_writable(context, destination, scope):
            raise FilesFacadeError("Managed move is not writable", code="resource_unavailable")
        source_entry = await self.stat(context, origin_id=_origin(source))
        parent_entry = await self.stat(context, origin_id=_origin(os.path.dirname(destination)))
        revision = dict(source_entry.revision or {})
        if (source_entry.origin_id != _origin(source) or source_entry.kind != "file" or "move" not in source_entry.capabilities
                or parent_entry.origin_id != _origin(os.path.dirname(destination)) or not self._is_directory_kind(parent_entry.kind)
                or "write" not in parent_entry.capabilities or dict(expected_revision or {}) != revision):
            raise FilesFacadeError("Managed move source or destination changed", code="resource_changed")

        def selected(row):
            value = row.get("receipt", {}).get("provenance")
            if not isinstance(value, Mapping):
                return False
            record = MediaProvenance.from_receipt(value)
            path = os.path.normpath(os.path.join(record.canonical_root, record.document_path))
            return path == source

        try:
            rows = store.list_host_media_operations(owner_subject_id=owner, select=selected)
            records = [MediaProvenance.from_receipt(row["receipt"]["provenance"]) for row in rows]
        except Exception as exc:
            raise FilesFacadeError("Managed media provenance is unavailable", code="provider_unavailable") from exc
        invalid_record = lambda r: (r.owner_subject_id != owner or
            ((r.origin != "selected_root" or r.layout_version != 2 or r.workspace_id is not None or os.path.normpath(r.canonical_root) != root) if selected_folder else
            ((r.origin != "loose" or r.layout_version != 1 or r.workspace_id is not None
              or os.path.normpath(r.canonical_root) != os.path.dirname(source)) if managed_root is not None else
             (r.origin != "workspace" or r.layout_version != 2 or str(r.workspace_id or "") != str(context.workspace_id)
              or os.path.normpath(r.canonical_root) != root))))
        if not records or any(row["receipt"].get("phase") != "complete" for row in rows) or any(invalid_record(r) for r in records):
            raise FilesFacadeError("Managed workspace media is unavailable", code="managed_media_move_unsupported")
        source_rel = os.path.relpath(source, root).replace(os.sep, "/")
        destination_rel = os.path.relpath(destination, root if dest_frame is None else dest_frame["root"]).replace(os.sep, "/")
        if any(r.document_path != (os.path.basename(source) if managed_root is not None and not selected_folder else source_rel) for r in records):
            raise FilesFacadeError("Managed document location is invalid", code="provider_unavailable")
        same_parent = os.path.dirname(source) == os.path.dirname(destination)
        native_id = "host-managed-" + hashlib.sha256(
            json.dumps(["host-managed-v1", owner, str(operation_id), str(item_id)], separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        ).hexdigest()
        reserved = store.get_operation(owner_subject_id=owner, operation_id=str(operation_id))
        if not isinstance(reserved, Mapping) or not isinstance(reserved.get("receipt"), Mapping):
            raise FilesFacadeError("Managed transfer is not reserved", code="provider_unavailable")
        existing_items = (reserved or {}).get("receipt", {}).get("_managed_items", {})
        existing = existing_items.get(str(item_id)) if isinstance(existing_items, Mapping) else None
        if existing is not None:
            frozen = existing.get("plan") if isinstance(existing, Mapping) else None
            if (not isinstance(frozen, Mapping) or frozen.get("native_operation_id") != native_id
                    or frozen.get("source") != source or frozen.get("destination") != destination
                    or frozen.get("source_revision") != revision or frozen.get("root") != root
                    or frozen.get("workspace_revision") != (workspace.revision if workspace is not None else None)
                    or frozen.get("managed_root") != (dict(managed_root) if managed_root is not None else None)
                    or frozen.get("location_id") != str(location.id) or frozen.get("location_revision") != location.revision
                    or frozen.get("workspace_id") != str(context.workspace_id)
                    or frozen.get("generation") != int(context.policy_generation)
                    or ("destination_frame" in frozen) != (dest_frame is not None)
                    or (dest_frame is not None and frozen.get("destination_root") != (dict(destination_root) if dest_frame["binding"] else None))):
                raise FilesFacadeError("Managed move reservation changed", code="idempotency_conflict")
            try:
                return store.freeze_managed_transfer_item(owner_subject_id=owner, operation_id=str(operation_id),
                    item_id=str(item_id), plan=frozen, provenance_selector=selected)
            except Exception as exc:
                raise FilesFacadeError("Managed provenance changed", code="resource_changed") from exc
        if str(item_id) not in reserved["receipt"].get("_item_ids", []):
            raise FilesFacadeError("Managed transfer item is not reserved", code="idempotency_conflict")

        # A current stat and a handle-bound byte stream prove each input. No
        # Python filesystem reads, guessed references, or text re-encoding.
        snapshots: dict[str, tuple[str, bytes, str, ProviderResource]] = {}
        snapshot_bytes = 0
        async def snapshot(path: str) -> tuple[str, bytes, str, ProviderResource]:
            nonlocal snapshot_bytes
            if path in snapshots:
                return snapshots[path]
            entry = await self.stat(context, origin_id=_origin(path))
            if (entry.origin_id != _origin(path) or entry.kind != "file"
                    or not {"open", "download"}.intersection(entry.capabilities)):
                raise FilesFacadeError("Declared reference is unavailable", code="resource_unavailable")
            content = await self.content(context, origin_id=_origin(path))
            if content.size is None or content.stream is None or int(content.size) < 0 or int(content.size) > 10 * 1024 * 1024:
                raise FilesFacadeError("Managed document exceeds the supported bound", code="resource_unavailable")
            if snapshot_bytes + int(content.size) > 64 * 1024 * 1024:
                raise FilesFacadeError("Managed documents exceed the supported total bound", code="resource_unavailable")
            chunks = bytearray()
            async for chunk in content.stream(0, int(content.size)):
                chunks.extend(chunk)
                if len(chunks) > int(content.size):
                    raise FilesFacadeError("Managed document changed", code="resource_changed")
            raw = bytes(chunks)
            fingerprint = "sha256:" + hashlib.sha256(raw).hexdigest() + ":" + str(len(raw))
            if len(raw) != int(content.size) or (entry.revision or {}).get("value") != fingerprint:
                raise FilesFacadeError("Managed document changed", code="resource_changed")
            bom = b"\xef\xbb\xbf" if raw.startswith(b"\xef\xbb\xbf") else b""
            try:
                text = raw[len(bom):].decode("utf-8", errors="strict")
            except UnicodeDecodeError as exc:
                raise FilesFacadeError("Managed move requires UTF-8 text", code="unsupported_operation") from exc
            if "\x00" in text:
                raise FilesFacadeError("Managed move requires UTF-8 text", code="unsupported_operation")
            snapshot_bytes += len(raw)
            snapshots[path] = (text, bom, fingerprint, entry)
            return snapshots[path]

        await snapshot(source)
        if snapshots[source][2] != revision.get("value"):
            raise FilesFacadeError("Managed move source changed", code="resource_changed")
        declared: dict[str, str] = {}
        for record in records:
            for token in (record.document_id, *record.references):
                try:
                    historical = resolve_resource_ref_for_reissue(str(token), expected_owner_subject_id=owner, required_capability="stat")
                    if historical.provider != "host":
                        raise ValueError("unsupported provider")
                    path = os.path.normpath(_path(historical.origin_id))
                    if os.path.commonpath([root, path]) != root:
                        raise ValueError("reference outside workspace")
                    await snapshot(path)
                    declared[str(token)] = path
                except Exception as exc:
                    raise FilesFacadeError("Declared reference cannot be authorized", code="resource_unavailable") from exc
            if declared.get(record.document_id) != source:
                raise FilesFacadeError("Managed document identity changed", code="resource_changed")

        assets = []
        asset_proofs = []
        for record in records:
            media_dir = record.resolved_media_dir()
            if managed_root is not None and not selected_folder:
                media_dir = os.path.relpath(os.path.join(record.canonical_root, media_dir), root).replace(os.sep, "/")
            name = record.asset_name
            if not name or name in {".", ".."} or "/" in name or "\\" in name:
                raise FilesFacadeError("Managed asset name is invalid", code="provider_unavailable")
            asset_path = os.path.normpath(os.path.join(root, media_dir, name))
            if os.path.commonpath([root, asset_path]) != root:
                raise FilesFacadeError("Managed asset is outside the workspace", code="resource_unavailable")
            asset = await self.stat(context, origin_id=_origin(asset_path))
            fingerprint = str((asset.revision or {}).get("value") or "")
            if (asset.origin_id != _origin(asset_path) or asset.kind != "file" or "stat" not in asset.capabilities
                    or fingerprint.lower() != record.asset_digest.lower()):
                raise FilesFacadeError("Managed asset changed", code="resource_changed")
            assets.append({"name": name, "digest": record.asset_digest, "asset_id": record.asset_id,
                           "owner_document_id": source_rel, "media_dir": media_dir})
            asset_proofs.append({"path": asset_path, "fingerprint": fingerprint, "movable": "move" in asset.capabilities})
        references = [(os.path.relpath(path, root).replace(os.sep, "/"), data[0], "write" not in data[3].capabilities,
                       os.path.relpath(path, root).replace(os.sep, "/")) for path, data in snapshots.items()]
        plan = plan_document_move(operation_id=native_id, document_id=source_rel,
            source_document_path=source_rel, destination_document_path=destination_rel, canonical_root=root,
            origin="selected_root" if selected_folder else ("loose" if managed_root is not None else "workspace"), owner_subject_id=owner,
            source_layout_version=1 if managed_root is not None and not selected_folder else 2,
            preserve_media_location=same_parent,
            preflight=MovePreflight(expected_revision=str(revision["value"]), current_revision=snapshots[source][2], assets=tuple(assets)),
            referencing_documents=references,
            **({} if dest_frame is None else {"destination_frame": dest_frame["frame"],
                "source_workspace_id": str(context.workspace_id) if not selected_folder and managed_root is None else None}))
        if plan.conflicts:
            # A cross-root planner refusal carries its own closed reason code.
            raise FilesFacadeError("Managed references or assets conflict",
                code="resource_changed" if dest_frame is None else plan.conflicts[0].code)
        replacements: dict[str, bytes] = {}
        for path, (text, bom, _fingerprint, _entry) in snapshots.items():
            doc_id = os.path.relpath(path, root).replace(os.sep, "/")
            edits = sorted((edit for edit in plan.reference_edits if edit.document_id == doc_id), key=lambda edit: edit.site.start, reverse=True)
            for edit in edits:
                text = text[:edit.site.start] + edit.new_text + text[edit.site.end:]
            if edits:
                replacements[path] = bom + text.encode("utf-8")
        # JSON byte arrays require at least two bytes per element. Reject an
        # impossible frame before allocating those arrays.
        if sum(len(data) for data in replacements.values()) * 2 > _MAX_FRAME_BYTES:
            raise FilesFacadeError("Managed move exceeds the service frame bound", code="resource_unavailable")
        participants = [{"kind": "document", "source": source, "destination": destination,
                         "expected_fingerprint": snapshots[source][2], "replacement": list(replacements[source]) if source in replacements else None}]
        proof_by_path = {item["path"]: item for item in asset_proofs}
        for item in plan.asset_moves:
            old = os.path.normpath(os.path.join(root, item.source_media_dir, item.asset_name))
            new = os.path.normpath(os.path.join(root if dest_frame is None else dest_frame["root"], item.destination_media_dir, item.asset_name))
            if not proof_by_path[old]["movable"]:
                raise FilesFacadeError("Managed asset is read-only", code="resource_unavailable")
            participants.append({"kind": "asset", "source": old, "destination": new,
                                 "expected_fingerprint": proof_by_path[old]["fingerprint"], "replacement": None})
        for path, replacement in replacements.items():
            if path != source:
                participants.append({"kind": "reference", "source": path, "destination": None,
                                     "expected_fingerprint": snapshots[path][2], "replacement": list(replacement)})
        if dest_frame is not None:
            # Collisions fail before anything is recorded: nothing may already exist at any destination.
            collision_client = self._client(context, self._scope(context))
            for item in participants:
                if item["destination"] is None:
                    continue
                try:
                    await collision_client.request("stat", item["destination"], {"include_fingerprint": False})
                except FilesServiceError as exc:
                    if exc.code != "invalid_path":
                        raise _service_error(exc) from exc
                else:
                    raise FilesFacadeError("Managed move destination already exists", code="resource_changed")
            # A move between disks is refused here, before anything is recorded; the native service refuses it too.
            try:
                devices = {_device_of(os.path.dirname(item["source"])) for item in participants}
                devices |= {_device_of(os.path.dirname(item["destination"])) for item in participants if item["destination"] is not None}
            except OSError as exc:
                raise FilesFacadeError("Managed move devices are unavailable", code="provider_unavailable") from exc
            if len(devices) > 1:
                raise FilesFacadeError(_CROSS_DISK_MESSAGE, code=_CROSS_DISK_CODE)
        mutated = {item["source"] for item in participants}
        destinations = {item["destination"] for item in participants if item["destination"] is not None}
        guard_map = {path: data[2] for path, data in snapshots.items() if path not in mutated}
        for proof in asset_proofs:
            if proof["path"] not in mutated:
                previous = guard_map.setdefault(proof["path"], proof["fingerprint"])
                if previous != proof["fingerprint"]:
                    raise FilesFacadeError("Managed guard proof conflicts", code="resource_changed")
        if destinations.intersection(guard_map):
            raise FilesFacadeError("Managed guard overlaps a destination", code="resource_changed")
        guards = [{"source": path, "expected_fingerprint": guard_map[path]} for path in sorted(guard_map)]
        if len(participants) + len(guards) > 128 or any(len(data) > 10 * 1024 * 1024 for data in replacements.values()):
            raise FilesFacadeError("Managed move exceeds the supported bound", code="resource_unavailable")
        total = 0
        for item in participants:
            try:
                before_length = int(item["expected_fingerprint"].rsplit(":", 1)[1])
            except (ValueError, IndexError) as exc:
                raise FilesFacadeError("Managed fingerprint is invalid", code="provider_unavailable") from exc
            if before_length < 0 or before_length > 10 * 1024 * 1024:
                raise FilesFacadeError("Managed move exceeds the supported asset bound", code="resource_unavailable")
            replacement_length = len(item["replacement"]) if item["replacement"] is not None else before_length
            total += before_length + 2 * replacement_length + (replacement_length if item["replacement"] is not None else 0)
        for guard in guards:
            try:
                length = int(guard["expected_fingerprint"].rsplit(":", 1)[1])
            except (ValueError, IndexError) as exc:
                raise FilesFacadeError("Managed guard fingerprint is invalid", code="provider_unavailable") from exc
            if length < 0 or length > 10 * 1024 * 1024:
                raise FilesFacadeError("Managed guard exceeds the supported bound", code="resource_unavailable")
            total += length
        if total > 64 * 1024 * 1024:
            raise FilesFacadeError("Managed move exceeds the supported total bound", code="resource_unavailable")

        fresh_refs = {}
        for path, (_text, _bom, _fingerprint, entry) in snapshots.items():
            target = destination if path == source else path
            # Across roots the moved document belongs to the destination's location/workspace.
            home = parent_entry if dest_frame is not None and path == source else entry
            fresh_refs[path] = issue_resource_ref(owner_subject_id=owner, provider="host", origin_id=_origin(target),
                kind=entry.kind, capabilities=entry.capabilities, policy_generation=int(context.policy_generation),
                location_id=home.location_id, workspace_id=home.workspace_id).token
        provenance_rows = []
        for row, record in zip(rows, records):
            before = dict(row["receipt"])
            after = dict(before)
            provenance = dict(before["provenance"])
            provenance.update({"document_path": os.path.basename(destination) if managed_root is not None and not selected_folder else destination_rel, "document_id": fresh_refs[source],
                               "references": [fresh_refs[declared[str(token)]] for token in record.references],
                               "media_dir": record.resolved_media_dir() if same_parent else (os.path.relpath(os.path.join(root, plan.destination_media_dir), os.path.dirname(destination)).replace(os.sep, "/") if managed_root is not None and not selected_folder else plan.destination_media_dir)})
            if managed_root is not None and not selected_folder:
                provenance["canonical_root"] = os.path.dirname(destination)
            if dest_frame is not None:
                # The planner states the destination frame; the row carries it (loose roots are the document's folder).
                frame, loose = plan.destination_frame, plan.destination_frame["origin"] == "loose"
                provenance.update({"origin": frame["origin"], "layout_version": frame["layout_version"], "workspace_id": frame["workspace_id"],
                    "canonical_root": os.path.dirname(destination) if loose else frame["canonical_root"],
                    "document_path": os.path.basename(destination) if loose else destination_rel,
                    "media_dir": (os.path.relpath(os.path.join(frame["canonical_root"], plan.destination_media_dir), os.path.dirname(destination))
                                  if loose else plan.destination_media_dir).replace(os.sep, "/")})
                try:
                    moved = MediaProvenance.from_receipt(provenance)
                    valid = ((moved.origin, moved.layout_version, moved.workspace_id) == (frame["origin"], frame["layout_version"], frame["workspace_id"])
                             and moved.owner_subject_id == owner)
                except Exception:
                    valid = False
                if not valid:
                    raise FilesFacadeError("Destination provenance is invalid", code="managed_media_move_unsupported")
            after["provenance"] = provenance
            provenance_rows.append({"operation_id": row["operation_id"], "digest": row["digest"], "before": before, "after": after})
        payload = {"operation_id": native_id, "participants": participants, "guards": guards}
        if selected_folder:
            payload["directory_guard"] = {"path": str(selected_folder["path"]), "revision": str(selected_folder["revision"]["value"])}
        envelope = OdysseusFilesClient._request_envelope("managed_file_move", path=source, payload=payload, root_operation_id=native_id)
        if len(json.dumps(envelope, separators=(",", ":"), ensure_ascii=False).encode("utf-8")) > _MAX_FRAME_BYTES:
            raise FilesFacadeError("Managed move exceeds the service frame bound", code="resource_unavailable")
        try:
            latest_scope = self._scope(context)
            current_workspace = None if managed_root is not None else store.get_workspace(str(context.workspace_id))
            current_location = store.get_location(str(managed_root["root_id"])) if managed_root is not None else store.get_location(current_workspace.location_id)
            dest_paths = [destination, *(item["destination"] for item in participants if item["destination"] is not None)]
            paths = [source, *(item["source"] for item in participants), *dest_paths]
            if selected_folder:
                self._validate_selected_move_paths(context, root, [p for p in paths if dest_frame is None or p not in dest_paths]
                                                   + [guard["source"] for guard in guards])
            if dest_frame is not None:
                # Destinations are judged against the destination root; both witnesses must be unchanged.
                if dest_frame["origin"] == "selected_root":
                    self._validate_selected_move_paths(context, dest_frame["root"], dest_paths)
                if (any(os.path.commonpath([dest_frame["root"], path]) != dest_frame["root"] for path in dest_paths)
                        or store.get_location(str(dest_frame["location"].id)) != dest_frame["location"]
                        or (dest_frame["workspace"] is not None and store.get_workspace(str(context.workspace_id)) != dest_frame["workspace"])):
                    raise ValueError("destination scope changed")
            if (int(store.generation()) not in (0, int(context.policy_generation))
                    or current_workspace != workspace or current_location != location
                    or any(not self._path_writable(context, path, latest_scope) for path in paths)):
                raise ValueError("scope changed")
        except Exception as exc:
            raise FilesFacadeError("File policy or workspace changed", code="policy_generation_changed") from exc
        frozen = {"native_operation_id": native_id, "source": source, "destination": destination,
                  "workspace_id": str(context.workspace_id), "root": root, "generation": int(context.policy_generation), "source_revision": revision,
                  "workspace_revision": workspace.revision if workspace is not None else None, "location_id": str(location.id), "location_revision": location.revision,
                  "managed_root": dict(managed_root) if managed_root is not None else None, "selected_folder": dict(selected_folder) if selected_folder else None,
                  "payload": payload, "asset_proofs": asset_proofs,
                  "reference_proofs": [{"path": path, "fingerprint": data[2]} for path, data in snapshots.items() if path != source],
                  "provenance_rows": provenance_rows,
                  "coverage": "app_known_declared_references"}
        if dest_frame is not None:
            frozen.update({"source_frame": plan.source_frame, "destination_frame": plan.destination_frame,
                           "destination_root": dict(destination_root) if dest_frame["binding"] else None,
                           "destination_location_id": str(dest_frame["location"].id),
                           "destination_location_revision": dest_frame["location"].revision,
                           "destination_workspace_revision": dest_frame["workspace"].revision if dest_frame["workspace"] is not None else None})
        try:
            return store.freeze_managed_transfer_item(owner_subject_id=owner, operation_id=str(operation_id), item_id=str(item_id), plan=frozen, provenance_selector=selected)
        except Exception as exc:
            raise FilesFacadeError("Managed move reservation changed", code="idempotency_conflict") from exc

    async def prepare_managed_folder_move(
        self, context: ProviderContext, *, source_origin_id: str, destination_origin_id: str,
        expected_revision: Mapping[str, Any], operation_id: str, item_id: str,
        managed_root: Mapping[str, Any], destination_root: Mapping[str, Any] | None = None,
    ) -> Mapping[str, Any]:
        """Freeze the owner-authorized, app-known half of a folder transaction.

        Native code must still bind the private subtree and History snapshots,
        enforce its participant budget and carry out the same-ID transaction.
        """
        from src.openclank.media_ownership import MediaProvenance, MoveFrame, document_media_dir, plan_folder_reference_edits
        from src.openclank.resource_refs import resolve_resource_ref_for_reissue
        from src.openclank.files_service_client import OdysseusFilesClient, _MAX_FRAME_BYTES

        owner = str(context.owner_subject_id)
        source = os.path.normpath(_path(source_origin_id))
        destination = os.path.normpath(_path(destination_origin_id))
        selected_folder = managed_root.get("selected_folder")
        root = str(selected_folder.get("path") if isinstance(selected_folder, Mapping) else managed_root.get("root") or "")
        if (sys.platform != "linux" or not operation_id or not item_id or not os.path.isabs(root) or not os.path.isabs(source)
                or not os.path.isabs(destination) or source == root
                or destination == root or source == destination
                or os.path.commonpath((root, source)) != root
                or os.path.commonpath((source, destination)) in (source, destination)):
            raise FilesFacadeError("Managed folder move is unavailable", code="managed_media_move_unsupported")
        renewed = (await self.authorize_selected_folder(context, origin_id=_origin(root), expected_revision=selected_folder["revision"])
                   if isinstance(selected_folder, Mapping) else await self.authorize_managed_root(context, origin_id=_origin(root), expected_revision=None))
        if dict(renewed) != dict(managed_root):
            raise FilesFacadeError("Selected root changed", code="resource_changed")
        store = self.operation_store
        cross = os.path.commonpath((root, destination)) != root
        dest_frame = self._destination_move_frame(context, owner=owner, source_root=root, destination=destination,
            managed_root=managed_root, destination_root=destination_root) if cross else None
        if dest_frame is not None and str(dest_frame["location"].id) == str(managed_root["root_id"]):
            raise FilesFacadeError("Selected folders in one Files root are unavailable", code="managed_media_move_unsupported")
        reserved = store.get_operation(owner_subject_id=owner, operation_id=str(operation_id))
        items = ((reserved or {}).get("receipt") or {}).get("_managed_items") or {}
        native_id = "host-managed-" + hashlib.sha256(
            json.dumps(["host-managed-v1", owner, str(operation_id), str(item_id)], separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        ).hexdigest()
        current = items.get(str(item_id)) if isinstance(items, Mapping) else None
        if current is not None:
            plan = current.get("plan") if isinstance(current, Mapping) else None
            if (not isinstance(plan, Mapping) or plan.get("kind") != "folder" or plan.get("native_operation_id") != native_id
                    or plan.get("source") != source or plan.get("destination") != destination
                    or plan.get("source_revision") != dict(expected_revision or {}) or plan.get("managed_root") != dict(managed_root)
                    or plan.get("generation") != int(context.policy_generation)
                    or (cross and plan.get("destination_root") != (dict(destination_root) if dest_frame["binding"] else None))):
                raise FilesFacadeError("Managed folder reservation changed", code="idempotency_conflict")
            return dict(current)
        if not isinstance(reserved, Mapping) or str(item_id) not in (reserved.get("receipt") or {}).get("_item_ids", []):
            raise FilesFacadeError("Managed folder move is not reserved", code="idempotency_conflict")
        scope = self._scope(context)
        if not self._path_writable(context, source, scope) or not self._path_writable(context, destination, scope):
            raise FilesFacadeError("Managed folder is not writable", code="resource_unavailable")
        source_entry = await self.stat(context, origin_id=_origin(source))
        parent_entry = await self.stat(context, origin_id=_origin(os.path.dirname(destination)))
        if (not self._is_directory_kind(source_entry.kind) or source_entry.origin_id != _origin(source)
                or "write" not in source_entry.capabilities or dict(source_entry.revision or {}) != dict(expected_revision or {})
                or parent_entry.origin_id != _origin(os.path.dirname(destination))
                or not self._is_directory_kind(parent_entry.kind) or "write" not in parent_entry.capabilities):
            raise FilesFacadeError("Managed folder source or destination changed", code="resource_changed")
        proof = await self.inspect_managed_folder(context, origin_id=_origin(source), managed_root=managed_root)
        rows = self._managed_move_rows(context, source, source_is_directory=True)
        if not rows or len(rows) > 128:
            raise FilesFacadeError("Managed folder exceeds the supported provenance bound", code="resource_unavailable")
        records = [MediaProvenance.from_receipt(row["receipt"]["provenance"]) for row in rows]
        if selected_folder and (os.path.relpath(source, root).split(os.sep)[0] == ".media" or os.path.relpath(destination, root).split(os.sep)[0] == ".media"):
            raise FilesFacadeError("Selected media folder moves are unavailable", code="managed_media_move_unsupported")
        if any((record.origin != "selected_root" or record.layout_version != 2 or record.workspace_id is not None or record.canonical_root != root)
               if selected_folder else record.origin == "selected_root" for record in records):
            raise FilesFacadeError("Managed folder provenance differs from selected root", code="managed_media_move_unsupported")
        if any(row["receipt"].get("phase") != "complete" or record.owner_subject_id != owner for row, record in zip(rows, records)):
            raise FilesFacadeError("Managed folder provenance is unavailable", code="resource_unavailable")

        def inside(path: str, parent: str) -> bool:
            try:
                return os.path.commonpath((parent, path)) == parent
            except ValueError:
                return False

        def moved(path: str) -> str:
            return os.path.normpath(os.path.join(destination, os.path.relpath(path, source))) if inside(path, source) else path

        documents: dict[str, list[MediaProvenance]] = {}
        for record in records:
            doc = os.path.normpath(os.path.join(record.canonical_root, record.document_path))
            if not os.path.isabs(record.canonical_root) or not inside(doc, record.canonical_root) or not inside(doc, source) or not inside(record.canonical_root, root):
                raise FilesFacadeError("Managed folder provenance is outside its root", code="resource_unavailable")
            if record.origin == "selected_root":
                if not selected_folder or record.canonical_root != root or (not cross and not inside(moved(doc), root)):
                    raise FilesFacadeError("Selected-root provenance changed", code="resource_changed")
            elif record.origin == "loose":
                if record.layout_version != 1 or record.workspace_id is not None or os.path.dirname(doc) != record.canonical_root:
                    raise FilesFacadeError("Managed loose provenance changed", code="resource_changed")
            else:
                workspace = store.get_workspace(str(record.workspace_id or ""))
                location = store.get_location(workspace.location_id)
                relative_folder = str(workspace.relative_folder or "").replace("\\", "/")
                if relative_folder.startswith("/") or ".." in relative_folder.split("/"):
                    raise FilesFacadeError("Managed workspace provenance changed", code="resource_changed")
                workspace_root = os.path.normpath(os.path.join(str(location.canonical_path), relative_folder))
                if (str(workspace.owner_subject_id) != owner or workspace.archived or not location.enabled
                        or location.availability != "available" or workspace_root != record.canonical_root
                        or (not cross and not inside(moved(doc), workspace_root))):
                    raise FilesFacadeError("Managed workspace provenance changed", code="resource_changed")
            documents.setdefault(doc, []).append(record)

        snapshots: dict[str, tuple[str, bytes, str, ProviderResource]] = {}
        total = 0
        async def read_text(path: str) -> None:
            nonlocal total
            if path in snapshots:
                return
            if not inside(path, root) and not (cross and inside(path, dest_frame["root"])):
                raise FilesFacadeError("Declared reference is outside the root", code="resource_unavailable")
            entry = await self.stat(context, origin_id=_origin(path))
            if entry.kind != "file" or entry.origin_id != _origin(path) or not {"open", "download"}.intersection(entry.capabilities):
                raise FilesFacadeError("Declared reference is unavailable", code="resource_unavailable")
            content = await self.content(context, origin_id=_origin(path))
            if content.size is None or content.stream is None or int(content.size) < 0 or int(content.size) > 10 * 1024 * 1024:
                raise FilesFacadeError("Managed reference exceeds the supported bound", code="resource_unavailable")
            raw = bytearray()
            async for chunk in content.stream(0, int(content.size)):
                raw.extend(chunk)
                if len(raw) > int(content.size):
                    raise FilesFacadeError("Managed reference changed", code="resource_changed")
            data = bytes(raw)
            fingerprint = "sha256:" + hashlib.sha256(data).hexdigest() + ":" + str(len(data))
            if len(data) != int(content.size) or (entry.revision or {}).get("value") != fingerprint:
                raise FilesFacadeError("Managed reference changed", code="resource_changed")
            bom = b"\xef\xbb\xbf" if data.startswith(b"\xef\xbb\xbf") else b""
            try:
                text = data[len(bom):].decode("utf-8", errors="strict")
            except UnicodeDecodeError as exc:
                raise FilesFacadeError("Managed folder references require UTF-8", code="unsupported_operation") from exc
            if "\x00" in text:
                raise FilesFacadeError("Managed folder references require UTF-8", code="unsupported_operation")
            total += len(data)
            if total > 64 * 1024 * 1024:
                raise FilesFacadeError("Managed folder exceeds the supported byte bound", code="resource_unavailable")
            snapshots[path] = (text, bom, fingerprint, entry)

        declared: dict[str, str] = {}
        for doc, group in documents.items():
            for record in group:
                for token in (record.document_id, *record.references):
                    historical = resolve_resource_ref_for_reissue(str(token), expected_owner_subject_id=owner, required_capability="stat")
                    if historical.provider != "host":
                        raise FilesFacadeError("Declared reference is unavailable", code="resource_unavailable")
                    path = os.path.normpath(_path(historical.origin_id))
                    await read_text(path)
                    declared[str(token)] = path
                if declared[record.document_id] != doc:
                    raise FilesFacadeError("Managed document identity changed", code="resource_changed")

        path_mapping: dict[str, str] = {doc: moved(doc) for doc in documents}
        asset_proofs: dict[str, dict[str, Any]] = {}
        for doc, group in documents.items():
            for record in group:
                old_asset = os.path.normpath(os.path.join(record.canonical_root, record.resolved_media_dir(), record.asset_name))
                if old_asset in documents or not inside(old_asset, root) or not record.asset_name or os.path.basename(record.asset_name) != record.asset_name:
                    raise FilesFacadeError("Managed asset is outside the root", code="resource_unavailable")
                if cross:
                    frame = dest_frame["frame"]
                    if frame.origin == "loose":
                        frame = MoveFrame("loose", os.path.dirname(moved(doc)), 1, None)
                    relative_doc = os.path.relpath(moved(doc), frame.canonical_root).replace(os.sep, "/")
                    layout = document_media_dir(document_path=relative_doc, canonical_root=frame.canonical_root,
                        origin=frame.origin, owner_subject_id=owner, workspace_id=frame.workspace_id, layout_version=frame.layout_version)
                    new_asset = os.path.normpath(os.path.join(frame.canonical_root, layout.media_dir, record.asset_name))
                elif inside(old_asset, source):
                    new_asset = moved(old_asset)
                elif record.origin == "selected_root":
                    new_asset = old_asset
                elif record.origin == "workspace":
                    new_document = os.path.relpath(moved(doc), record.canonical_root).replace(os.sep, "/")
                    layout = document_media_dir(document_path=new_document, canonical_root=record.canonical_root,
                        origin="workspace", owner_subject_id=owner, workspace_id=record.workspace_id, layout_version=record.layout_version)
                    new_asset = os.path.normpath(os.path.join(record.canonical_root, layout.media_dir, record.asset_name))
                else:
                    raise FilesFacadeError("Loose media is outside the folder", code="resource_unavailable")
                if not inside(new_asset, dest_frame["root"] if cross else root) or (old_asset in path_mapping and path_mapping[old_asset] != new_asset):
                    raise FilesFacadeError("Managed asset mapping conflicts", code="resource_changed")
                entry = await self.stat(context, origin_id=_origin(old_asset))
                fingerprint = str((entry.revision or {}).get("value") or "")
                if entry.kind != "file" or entry.origin_id != _origin(old_asset) or fingerprint.lower() != record.asset_digest.lower():
                    raise FilesFacadeError("Managed asset changed", code="resource_changed")
                if old_asset != new_asset and not inside(old_asset, source) and "move" not in entry.capabilities:
                    raise FilesFacadeError("Managed asset is read-only", code="resource_unavailable")
                path_mapping[old_asset] = new_asset
                asset_identity = {"path":old_asset, "destination":new_asset, "fingerprint":fingerprint,
                    "document":doc, "document_destination":moved(doc), "workspace_root":record.canonical_root,
                    "origin":record.origin, "layout_version":record.layout_version}
                if cross:
                    asset_identity["source_frame"] = MoveFrame(record.origin,record.canonical_root,record.layout_version,record.workspace_id).as_dict()
                    asset_identity["destination_frame"] = frame.as_dict()
                previous = asset_proofs.get(old_asset)
                if previous and any(previous.get(key) != value for key, value in asset_identity.items()):
                    raise FilesFacadeError("Managed asset proofs conflict", code="resource_changed")
                asset_proofs[old_asset] = {**asset_identity, "entry":entry}

        destinations: dict[str, str] = {}
        for old, new in path_mapping.items():
            if new in destinations and destinations[new] != old:
                raise FilesFacadeError("Managed folder destinations conflict", code="resource_changed")
            destinations[new] = old

        relative = (lambda path: path.replace(os.sep, "/")) if cross else (lambda path: os.path.relpath(path, root).replace(os.sep, "/"))
        edits, conflicts = plan_folder_reference_edits(source_folder=relative(source), destination_folder=relative(destination),
            moved_targets={relative(old):relative(new) for old, new in path_mapping.items()},
            referencing_documents=[(relative(path), relative(path), value[0], "write" not in value[3].capabilities)
                                   for path, value in snapshots.items()], absolute_paths=cross)
        if conflicts:
            raise FilesFacadeError("Protected references prevent the folder move", code="resource_changed")
        replacements: dict[str, bytes] = {}
        for path, (text, bom, _fingerprint, _entry) in snapshots.items():
            selected = sorted((edit for edit in edits if edit.document_id == relative(path)), key=lambda edit: edit.site.start, reverse=True)
            for edit in selected:
                text = text[:edit.site.start] + edit.new_text + text[edit.site.end:]
            if selected:
                replacements[path] = bom + text.encode("utf-8")
        external_paths = {path for path in set(asset_proofs) | set(snapshots) if not inside(path, source)}
        if (len(external_paths) + int(proof["count"]) + 1 > 128
                or any(len(value) > 10 * 1024 * 1024 for value in replacements.values())
                or sum(map(len, replacements.values())) * 2 > _MAX_FRAME_BYTES):
            raise FilesFacadeError("Managed folder exceeds the supported participant bound", code="resource_unavailable")
        scope = self._scope(context)
        for path in [*documents, *replacements]:
            target = moved(path)
            if not self._path_writable(context, path, scope) or not self._path_writable(context, target, scope):
                raise FilesFacadeError("Managed folder participant is read-only", code="resource_unavailable")
        for proof_item in asset_proofs.values():
            if (proof_item["path"] != proof_item["destination"]
                    and (not self._path_writable(context, proof_item["path"], scope)
                         or not self._path_writable(context, proof_item["destination"], scope))):
                raise FilesFacadeError("Managed folder asset is read-only", code="resource_unavailable")

        fresh_refs: dict[str, str] = {}
        for path, (_text, _bom, _fingerprint, entry) in snapshots.items():
            home = parent_entry if cross and inside(path,source) else entry
            fresh_refs[path] = issue_resource_ref(owner_subject_id=owner, provider="host", origin_id=_origin(moved(path)),
                kind=entry.kind, capabilities=entry.capabilities, policy_generation=int(context.policy_generation),
                location_id=home.location_id, workspace_id=home.workspace_id).token
        provenance_rows = []
        for row, record in zip(rows, records):
            doc = os.path.normpath(os.path.join(record.canonical_root, record.document_path))
            old_asset = os.path.normpath(os.path.join(record.canonical_root, record.resolved_media_dir(), record.asset_name))
            new_doc, new_asset = moved(doc), path_mapping[old_asset]
            frame = dest_frame["frame"] if cross else None
            new_root = (os.path.dirname(new_doc) if frame.origin == "loose" else frame.canonical_root) if cross else (os.path.dirname(new_doc) if record.origin == "loose" else record.canonical_root)
            before = dict(row["receipt"]); after = dict(before); provenance = dict(before["provenance"])
            provenance.update({"canonical_root":new_root, "document_path":os.path.relpath(new_doc, new_root).replace(os.sep, "/"),
                "document_id":fresh_refs[doc], "references":[fresh_refs[declared[str(token)]] for token in record.references],
                "media_dir":os.path.relpath(os.path.dirname(new_asset), new_root).replace(os.sep, "/")})
            if cross:
                provenance.update({"origin":frame.origin,"layout_version":frame.layout_version,"workspace_id":frame.workspace_id})
                MediaProvenance.from_receipt(provenance)
            after["provenance"] = provenance
            provenance_rows.append({"operation_id":row["operation_id"], "digest":row["digest"], "before":before, "after":after})
        references = [{"path":path, "fingerprint":value[2], "replacement":list(replacements[path]) if path in replacements else None}
                      for path, value in snapshots.items()]
        assets = [{key:value for key, value in item.items() if key != "entry"} for item in asset_proofs.values()]
        if selected_folder:
            checked = [source, *[path for path in snapshots if inside(path,root)], *(item["path"] for item in assets)] if cross else [source, destination, *snapshots.keys(), *(path for item in assets for path in (item["path"], item["destination"]))]
            self._validate_selected_move_paths(context, root, checked)
        if cross and dest_frame["binding"] and dest_frame["binding"].get("selected_folder"):
            self._validate_selected_move_paths(context, dest_frame["root"], [destination, *[path for path in snapshots if inside(path,dest_frame["root"])], *(item["destination"] for item in assets)])
        payload = {"operation_id":native_id, "source":source, "destination":destination,
                   "expected_snapshot":dict(proof), "references":references, "assets":assets}
        if selected_folder:
            payload["directory_guard"] = {"path":root, "revision":str(selected_folder["revision"]["value"])}
        if cross:
            location=dest_frame["location"]
            witness={"root_id":str(location.id),"root_path":os.path.normpath(str(location.canonical_path))}
            selected_destination=(dest_frame["binding"] or {}).get("selected_folder")
            if selected_destination:
                witness["directory_guard"]={"path":dest_frame["root"],"revision":str(selected_destination["revision"]["value"])}
            payload["destination_root"]=witness
        envelope = OdysseusFilesClient._request_envelope("managed_folder_move", path=source, payload=payload, root_operation_id=native_id)
        if len(json.dumps(envelope, separators=(",", ":"), ensure_ascii=False).encode("utf-8")) > _MAX_FRAME_BYTES:
            raise FilesFacadeError("Managed folder exceeds the service frame bound", code="resource_unavailable")
        if (dict(await self.inspect_managed_folder(context, origin_id=_origin(source), managed_root=managed_root)) != dict(proof)
                or int(store.generation()) not in (0, int(context.policy_generation))):
            raise FilesFacadeError("Managed folder changed", code="resource_changed")
        frozen = {"kind":"folder", "native_operation_id":native_id, "source":source, "destination":destination,
            "source_revision":dict(expected_revision or {}), "root":root, "managed_root":dict(managed_root), "selected_folder":dict(selected_folder) if selected_folder else None,
            "location_id":str(managed_root["root_id"]), "location_revision":managed_root["location_revision"],
            "workspace_revision":None,
            "workspace_id":str(context.workspace_id), "generation":int(context.policy_generation), "payload":payload,
            "asset_proofs":[{key:value for key, value in proof.items() if key != "entry"} for proof in asset_proofs.values()],
            "reference_proofs":[{"path":path, "fingerprint":value[2]} for path, value in snapshots.items()],
            "provenance_rows":provenance_rows, "coverage":"app_known_declared_references"}
        if cross:
            frozen.update({"destination_frame":dest_frame["frame"].as_dict(),"destination_root":dict(destination_root) if dest_frame["binding"] else None,
                "destination_location_id":str(dest_frame["location"].id),"destination_location_revision":dest_frame["location"].revision,
                "destination_workspace_revision":dest_frame["workspace"].revision if dest_frame["workspace"] is not None else None})
            try:
                await self._cross_root_scope(context,frozen,owner=owner,source_scope=dict(managed_root["scope"]),source_narrowed=True,phase="prepared")
            except Exception as exc:
                raise FilesFacadeError("Destination root changed",code="resource_changed") from exc
        def selected(row: Mapping[str, Any]) -> bool:
            value = (row.get("receipt") or {}).get("provenance")
            if not isinstance(value, Mapping):
                return False
            record = MediaProvenance.from_receipt(value)
            return inside(os.path.normpath(os.path.join(record.canonical_root, record.document_path)), source)
        try:
            return store.freeze_managed_transfer_item(owner_subject_id=owner, operation_id=str(operation_id), item_id=str(item_id),
                plan=frozen, provenance_selector=selected)
        except Exception as exc:
            raise FilesFacadeError("Managed folder reservation changed", code="idempotency_conflict") from exc

    def _managed_move_rows(self, context: ProviderContext, source: str, *, source_is_directory: bool) -> list[Mapping[str, Any]]:
        """Select the existing app-known provenance set for one source."""
        lister = getattr(self.operation_store, "list_host_media_operations", None)
        if not callable(lister):
            raise FilesFacadeError("Host media provenance is unavailable; move was not started", code="provider_unavailable")
        from src.openclank.media_ownership import MediaProvenance
        source_norm = os.path.normpath(os.path.abspath(source))

        def affected(row):
            receipt = row.get("receipt")
            provenance = receipt.get("provenance") if isinstance(receipt, Mapping) else None
            if not isinstance(provenance, Mapping):
                return False
            record = MediaProvenance.from_receipt(provenance)
            path = os.path.normpath(os.path.abspath(os.path.join(record.canonical_root, *record.document_path.replace("\\", "/").split("/"))))
            if source_is_directory:
                try:
                    return os.path.commonpath([source_norm, path]) == source_norm
                except ValueError:
                    return False
            return path == source_norm

        try:
            rows = lister(owner_subject_id=str(context.owner_subject_id), select=affected)
        except Exception as exc:
            raise FilesFacadeError("Host media provenance is unavailable; move was not started", code="provider_unavailable") from exc
        if len(rows) > 256:
            raise FilesFacadeError("Host media provenance exceeds the safe move preflight bound", code="provider_unavailable")
        return rows

    def managed_root_required(self, context: ProviderContext, *, source_origin_id: str,
                              destination_origin_id: str, source_is_directory: bool) -> bool:
        from src.openclank.media_ownership import MediaProvenance
        source, destination = _path(source_origin_id), _path(destination_origin_id)
        rows = self._managed_move_rows(context, source, source_is_directory=source_is_directory)
        return any(MediaProvenance.from_receipt(row["receipt"]["provenance"]).origin == "selected_root" or
                   (MediaProvenance.from_receipt(row["receipt"]["provenance"]).origin == "loose" and os.path.dirname(source) != destination) for row in rows)

    def managed_rename_root_required(self, context: ProviderContext, *, source_origin_id: str, name: str, source_is_directory: bool = False) -> bool:
        from src.openclank.media_ownership import MediaProvenance
        source = _path(source_origin_id)
        if name == Path(source).name:
            return False
        rows = self._managed_move_rows(context, source, source_is_directory=source_is_directory)
        return any(MediaProvenance.from_receipt(row["receipt"]["provenance"]).origin in {"loose", "selected_root"} for row in rows)

    def selected_folder_required(self, context: ProviderContext, *, source_origin_id: str, source_is_directory: bool = False) -> bool:
        from src.openclank.media_ownership import MediaProvenance
        return any(MediaProvenance.from_receipt(row["receipt"]["provenance"]).origin == "selected_root"
                   for row in self._managed_move_rows(context, _path(source_origin_id), source_is_directory=source_is_directory))

    async def _preflight_managed_media_move(
        self, context: ProviderContext, source: str, destination: str, *, source_is_directory: bool
    ) -> None:
        """Block Host moves that would strand exact persisted media references."""
        from src.openclank.media_ownership import MediaProvenance
        source_norm = os.path.normpath(os.path.abspath(source))
        destination_norm = os.path.normpath(os.path.abspath(destination))
        rows = self._managed_move_rows(context, source, source_is_directory=source_is_directory)
        # A source directory's descendants move even when its parent is stable.
        for row in rows:
            receipt = row.get("receipt") if isinstance(row, Mapping) else None
            provenance = receipt.get("provenance") if isinstance(receipt, Mapping) else None
            if not isinstance(provenance, Mapping):
                continue
            try:
                record = MediaProvenance.from_receipt(provenance)
            except Exception as exc:
                raise FilesFacadeError("Host media provenance is invalid; move was not started", code="provider_unavailable") from exc
            if record.owner_subject_id != str(context.owner_subject_id):
                continue
            document_path = os.path.normpath(os.path.abspath(os.path.join(record.canonical_root, *record.document_path.replace("\\", "/").split("/"))))
            affected = document_path == source_norm
            if source_is_directory:
                try:
                    affected = os.path.commonpath([source_norm, document_path]) == source_norm
                except ValueError:
                    affected = False
            if not affected:
                continue
            new_document = destination_norm
            if source_is_directory:
                relative = os.path.relpath(document_path, source_norm)
                new_document = os.path.join(destination_norm, relative)
            if os.path.normpath(document_path) != os.path.normpath(new_document):
                raise FilesFacadeError(
                    "Host move would break a document's managed media links; service media transaction is required",
                    code="managed_media_move_unsupported",
                )

    async def stage_import(self, context: ProviderContext, *, destination_origin_id: str, name: str, upload: Any, operation_id: str, item_id: str) -> Mapping[str, Any]:
        """Stream bounded chunks into a service-owned opaque stage."""
        if not isinstance(name, str) or not name or len(name.encode("utf-8")) > 240 or any(part in name for part in ("/", "\\", "\x00")) or name in {".", ".."}:
            raise FilesFacadeError("Host import name is invalid", code="invalid_resource_request")
        destination = _path(destination_origin_id)
        scope = self._scope(context)
        if not self._path_writable(context, destination, scope):
            raise FilesFacadeError("Host import destination is read-only", code="resource_unavailable")
        declared = str(getattr(upload, "content_type", "") or "").strip()
        if len(declared.encode("utf-8")) > 256 or any(ord(ch) < 32 for ch in declared):
            raise FilesFacadeError("Host import MIME metadata is invalid", code="invalid_resource_request")
        client = self._client(context, scope)
        begin, chunk_fn, abort = (getattr(client, key, None) for key in ("stage_begin", "stage_chunk", "stage_abort"))
        if not all(callable(fn) for fn in (begin, chunk_fn, abort)):
            raise FilesFacadeError("Host staging transport is unavailable", code="unsupported_provider_kind")
        response = await begin(destination)
        data = response.get("data") if isinstance(response, Mapping) else None
        stage_id = str(data.get("stage_id") or "") if isinstance(data, Mapping) else ""
        try:
            chunk_limit = int(data.get("max_chunk_bytes", 0)) if isinstance(data, Mapping) else 0
            total_limit = int(data.get("max_total_bytes", 0)) if isinstance(data, Mapping) else 0
        except (TypeError, ValueError, OverflowError) as error:
            if stage_id:
                try:
                    await abort(stage_id)
                except Exception:
                    pass
            raise FilesFacadeError("Host import staging bounds are invalid", code="provider_unavailable") from error
        if not stage_id or len(stage_id.encode("utf-8")) > 256 or any(ord(ch) < 32 for ch in stage_id) or chunk_limit < 1 or chunk_limit > 512 * 1024 or total_limit < 1 or total_limit > 64 * 1024 * 1024:
            if stage_id:
                try:
                    await abort(stage_id)
                except Exception:
                    pass
            raise FilesFacadeError("Host import staging handle is invalid", code="provider_unavailable")
        digest = hashlib.sha256()
        offset = 0
        detected = mimetypes.guess_type(name)[0] or "application/octet-stream"
        try:
            while True:
                raw = await upload.read(min(chunk_limit, 512 * 1024))
                if not raw:
                    break
                if not isinstance(raw, (bytes, bytearray)):
                    raise FilesFacadeError("Host import stream is invalid", code="invalid_resource_request")
                part = bytes(raw)
                if len(part) > chunk_limit:
                    raise FilesFacadeError("Host import chunk exceeds the service staging limit", code="upload_too_large")
                if offset + len(part) > total_limit:
                    raise FilesFacadeError("Host import exceeds the service staging limit", code="upload_too_large")
                if offset == 0:
                    detected = _detected_mime(name, part[:64])
                digest.update(part)
                await chunk_fn(stage_id, part, offset=offset)
                offset += len(part)
        except BaseException:
            try:
                await abort(stage_id)
            except Exception:
                pass
            raise
        return {"stage_id": stage_id, "length": offset, "digest": "sha256:" + digest.hexdigest(), "selected_name": name, "declared_mime": declared or None, "detected_mime": detected, "max_total_bytes": total_limit, "_client": client}

    async def abort_import(self, context: ProviderContext, *, stage: Mapping[str, Any]) -> None:
        client = stage.get("_client")
        abort = getattr(client, "stage_abort", None)
        if callable(abort):
            await abort(str(stage.get("stage_id") or ""))

    async def finish_import(self, context: ProviderContext, *, destination_origin_id: str, name: str, collision: str, stage: Mapping[str, Any], operation_id: str, item_id: str, request_digest: str | None = None, destination_revision: Mapping[str, Any] | None = None) -> ProviderResource:
        destination = _path(destination_origin_id)
        entry = await self.stat(context, origin_id=destination_origin_id)
        if entry.origin_id != destination_origin_id or entry.kind != "folder" or "write" not in entry.capabilities:
            raise FilesFacadeError("Host import destination is unavailable", code="resource_unavailable")
        return await self._run_claimed_host_mutation(context, [destination],
            lambda begin, no_effect: self._finish_import_claimed(context, destination_origin_id=destination_origin_id, name=name,
                collision=collision, stage=stage, operation_id=operation_id, item_id=item_id,
                request_digest=request_digest, destination_revision=destination_revision, _begin_mutation=begin, _known_no_effect=no_effect), delayed_start=True,
            recovery={"kind": "import", "parent_operation_id": str(operation_id), "item_id": str(item_id)})

    async def _finish_import_claimed(self, context: ProviderContext, *, destination_origin_id: str, name: str, collision: str, stage: Mapping[str, Any], operation_id: str, item_id: str, request_digest: str | None = None, destination_revision: Mapping[str, Any] | None = None, _begin_mutation: Callable[[], None], _known_no_effect: Callable[[], None]) -> ProviderResource:
        """Commit a service-owned stage, retrying only destination rename."""
        if collision not in {"fail", "rename"}:
            raise FilesFacadeError("Host import collision policy is invalid", code="unsupported_operation")
        destination = _path(destination_origin_id)
        scope = self._scope(context)
        if not self._path_writable(context, destination, scope):
            raise FilesFacadeError("Host import destination is read-only", code="resource_unavailable")
        client = stage.get("_client") or self._client(context, scope)
        finish = getattr(client, "stage_finish", None)
        stage_id = str(stage.get("stage_id") or "")
        length = int(stage.get("length", -1))
        digest = str(stage.get("digest") or "")
        if not callable(finish) or not stage_id or length < 0 or not digest.startswith("sha256:"):
            raise FilesFacadeError("Host import staging handle is invalid", code="provider_unavailable")
        candidate = str(name)
        for attempt in range(101):
            target = os.path.join(destination, candidate)
            target_before_kind = ""
            target_before_revision = None
            try:
                before = await self.stat(context, origin_id=_origin(target))
                target_before_kind = str(before.kind)
                target_before_revision = dict(before.revision) if isinstance(before.revision, Mapping) else None
            except FilesFacadeError as exc:
                if exc.code != "resource_unavailable":
                    raise
            ownership_path = self._ownership_sidecar(
                destination, operation_id, candidate, digest,
                owner_id=str(context.owner_subject_id),
                workspace_id=str(getattr(context, "workspace_id", "default") or "default"),
            )
            try:
                ownership_path, ownership_token = self._claim_ownership(ownership_path)
            except FileExistsError as exc:
                raise FilesFacadeError("Host import operation already owns this target", code="resource_changed") from exc
            native_id = uuid.uuid4().hex
            marker = self._import_marker(
                context, operation_id=operation_id, item_id=item_id, target=target,
                length=length, digest=digest, stage=stage, request_digest=request_digest,
                destination=destination, name=name, collision=collision, selected_name=candidate,
                destination_revision=destination_revision,
                ownership_path=ownership_path, ownership_token=ownership_token,
                target_before_kind=target_before_kind, target_before_revision=target_before_revision, native_operation_id=native_id,
            )
            # Persist before the external commit.  A fresh provider can then
            # inspect this exact target if the response or completion ledger
            # write is lost after stage_finish.
            try:
                self._operation_save(context, operation_id, digest, marker)
            except BaseException:
                self._release_ownership(marker)
                raise
            try:
                _begin_mutation()
                response = await finish(stage_id, target, length=length, digest=digest, root_operation_id=native_id)
                data = response.get("data") or {}
                resolved = str(data.get("path") or target)
                created = await self.stat(context, origin_id=_origin(resolved))
                selected_name = Path(resolved).name or candidate
                revision = dict(created.revision) if isinstance(created.revision, Mapping) else None
                self._operation_save(
                    context, operation_id, digest,
                    self._import_marker(
                        context, operation_id=operation_id, item_id=item_id,
                        target=resolved, length=length, digest=digest, stage=stage,
                        phase="complete", revision=revision, request_digest=request_digest,
                        destination=destination, name=name, collision=collision, selected_name=selected_name,
                        destination_revision=destination_revision,
                        ownership_path=ownership_path, ownership_token=ownership_token,
                        target_before_kind=target_before_kind, target_before_revision=target_before_revision, native_operation_id=native_id,
                    ),
                )
                self._release_ownership(marker)
                receipt_id = str(operation_id or f"host-import-{uuid.uuid4().hex}")
                return replace(created, provenance={"domain": "host", "source_digest": digest, "selected_name": selected_name, "declared_mime": stage.get("declared_mime"), "detected_mime": stage.get("detected_mime")}, revision=revision, action_receipt={
                    "receipt_id": receipt_id,
                    "status": "complete",
                    "phase": "complete",
                    "durable": True,
                    "history": _host_history_receipt(response),
                    "receipt": {"receipt_id": receipt_id, "outcome": "committed", "selected_name": selected_name, "revision": revision},
                })
            except FilesServiceError as exc:
                if exc.code in {"denied", "invalid_path", "malformed_request", "policy_generation_changed", "conflict"}:
                    _known_no_effect()
                if exc.code != "conflict" or collision != "rename" or attempt >= 100:
                    if exc.code == "conflict":
                        failed_marker = {**marker, "phase": "failed"}
                        try:
                            self._operation_save(context, operation_id, digest, failed_marker)
                        finally:
                            self._release_ownership(marker)
                        raise FilesFacadeError("Host import destination already exists", code="resource_changed") from exc
                    self._release_ownership(marker)
                    raise _service_error(exc) from exc
                self._release_ownership(marker)
                stem, suffix = os.path.splitext(str(name))
                candidate = f"{stem} ({attempt + 2}){suffix}"
        raise FilesFacadeError("Host import collision could not be resolved", code="resource_changed")

    async def import_file(
        self, context: ProviderContext, *, destination_origin_id: str, name: str, collision: str,
        upload: Any, operation_id: str, item_id: str,
    ) -> ProviderResource | Mapping[str, Any]:
        destination = _path(destination_origin_id)
        entry = await self.stat(context, origin_id=destination_origin_id)
        if entry.origin_id != destination_origin_id or entry.kind != "folder" or "write" not in entry.capabilities:
            raise FilesFacadeError("Host import destination is unavailable", code="resource_unavailable")
        return await self._run_claimed_host_mutation(context, [destination],
            lambda begin, no_effect: self._import_file_claimed(context, destination_origin_id=destination_origin_id, name=name,
                collision=collision, upload=upload, operation_id=operation_id, item_id=item_id,
                _begin_mutation=begin, _known_no_effect=no_effect), delayed_start=True,
            recovery={"kind": "import", "parent_operation_id": str(operation_id), "item_id": str(item_id)})

    async def _import_file_claimed(
        self,
        context: ProviderContext,
        *,
        destination_origin_id: str,
        name: str,
        collision: str,
        upload: Any,
        operation_id: str,
        item_id: str,
        _begin_mutation: Callable[[], None],
        _known_no_effect: Callable[[], None],
    ) -> ProviderResource | Mapping[str, Any]:
        """Import browser bytes into one already-authorized Host folder.

        UploadFile is consumed in bounded chunks into an owned temporary file.
        The Rust service then copies that staging file into its atomic create
        transaction. This keeps Python memory bounded and avoids the previous
        whole-file ``bytes -> list[int]`` expansion.
        """
        if not isinstance(name, str) or not name or len(name.encode("utf-8")) > 240 or any(part in name for part in ("/", "\\", "\x00")) or name in {".", ".."}:
            raise FilesFacadeError("Host import name is invalid", code="invalid_resource_request")
        if collision not in {"fail", "rename"}:
            raise FilesFacadeError("Host import collision policy is invalid", code="unsupported_operation")
        destination = _path(destination_origin_id)
        scope = self._scope(context)
        if not self._path_writable(context, destination, scope):
            raise FilesFacadeError("Host import destination is read-only", code="resource_unavailable")
        source_digest = hashlib.sha256()
        declared_mime = str(getattr(upload, "content_type", "") or "").strip()
        if len(declared_mime.encode("utf-8")) > 256 or any(ord(ch) < 32 for ch in declared_mime):
            raise FilesFacadeError("Host import MIME metadata is invalid", code="invalid_resource_request")
        detected_mime = mimetypes.guess_type(str(name))[0] or "application/octet-stream"
        client = self._client(context, scope)
        stage_begin = getattr(client, "stage_begin", None)
        stage_chunk = getattr(client, "stage_chunk", None)
        stage_finish = getattr(client, "stage_finish", None)
        stage_abort = getattr(client, "stage_abort", None)
        if not all(callable(fn) for fn in (stage_begin, stage_chunk, stage_finish, stage_abort)):
            raise FilesFacadeError("Host staging transport is unavailable", code="unsupported_provider_kind")
        candidate_stage = await stage_begin(destination)
        stage_data = candidate_stage.get("data") if isinstance(candidate_stage, Mapping) else None
        stage_id = str(stage_data.get("stage_id") or "") if isinstance(stage_data, Mapping) else ""
        try:
            chunk_limit = int(stage_data.get("max_chunk_bytes", 0)) if isinstance(stage_data, Mapping) else 0
            total_limit = int(stage_data.get("max_total_bytes", 0)) if isinstance(stage_data, Mapping) else 0
        except (TypeError, ValueError, OverflowError) as error:
            if stage_id:
                try:
                    await stage_abort(stage_id)
                except Exception:
                    pass
            raise FilesFacadeError("Host import staging bounds are invalid", code="provider_unavailable") from error
        if not stage_id or len(stage_id.encode("utf-8")) > 256 or any(ord(ch) < 32 for ch in stage_id) or chunk_limit < 1 or chunk_limit > 512 * 1024 or total_limit < 1 or total_limit > 64 * 1024 * 1024:
            if stage_id:
                try:
                    await stage_abort(stage_id)
                except Exception:
                    pass
            raise FilesFacadeError("Host import staging handle is invalid", code="provider_unavailable")
        offset = 0
        try:
            while True:
                chunk = await upload.read(min(chunk_limit, 512 * 1024))
                if not chunk:
                    break
                if not isinstance(chunk, (bytes, bytearray)):
                    raise FilesFacadeError("Host import stream is invalid", code="invalid_resource_request")
                chunk_bytes = bytes(chunk)
                if len(chunk_bytes) > chunk_limit:
                    raise FilesFacadeError("Host import chunk exceeds the service staging limit", code="upload_too_large")
                if offset + len(chunk_bytes) > total_limit:
                    raise FilesFacadeError("Host import exceeds the service staging limit", code="upload_too_large")
                if offset == 0:
                    detected_mime = _detected_mime(str(name), chunk_bytes[:64])
                source_digest.update(chunk_bytes)
                await stage_chunk(stage_id, chunk_bytes, offset=offset)
                offset += len(chunk_bytes)
        except BaseException:
            try: await stage_abort(stage_id)
            except Exception: pass
            raise
        candidate = str(name)
        for attempt in range(101):
            target = os.path.join(destination, candidate)
            digest = "sha256:" + source_digest.hexdigest()
            target_before_kind = ""
            target_before_revision = None
            try:
                before = await self.stat(context, origin_id=_origin(target))
                target_before_kind = str(before.kind)
                target_before_revision = dict(before.revision) if isinstance(before.revision, Mapping) else None
            except FilesFacadeError as exc:
                if exc.code != "resource_unavailable":
                    raise
            ownership_path = self._ownership_sidecar(
                destination, operation_id, candidate, digest,
                owner_id=str(context.owner_subject_id),
                workspace_id=str(getattr(context, "workspace_id", "default") or "default"),
            )
            try:
                ownership_path, ownership_token = self._claim_ownership(ownership_path)
            except FileExistsError as exc:
                raise FilesFacadeError("Host import operation already owns this target", code="resource_changed") from exc
            native_id = uuid.uuid4().hex
            marker = self._import_marker(
                context, operation_id=operation_id, item_id=item_id,
                target=target, length=offset, digest=digest,
                stage={"declared_mime": declared_mime or None, "detected_mime": detected_mime},
                collision=collision, selected_name=candidate,
                ownership_path=ownership_path, ownership_token=ownership_token,
                target_before_kind=target_before_kind, target_before_revision=target_before_revision, native_operation_id=native_id,
            )
            try:
                self._operation_save(context, operation_id, digest, marker)
            except BaseException:
                self._release_ownership(marker)
                raise
            try:
                _begin_mutation()
                response = await stage_finish(stage_id, target, length=offset, digest=digest, root_operation_id=native_id)
                data = response.get("data") or {}
                resolved = str(data.get("path") or target)
                created = await self.stat(context, origin_id=_origin(resolved))
                selected_name = Path(resolved).name or candidate
                revision = dict(created.revision) if isinstance(created.revision, Mapping) else None
                self._operation_save(
                    context, operation_id, digest,
                    self._import_marker(
                        context, operation_id=operation_id, item_id=item_id,
                        target=resolved, length=offset, digest=digest,
                        stage={"declared_mime": declared_mime or None, "detected_mime": detected_mime},
                        phase="complete", revision=revision, collision=collision, selected_name=selected_name,
                        ownership_path=ownership_path, ownership_token=ownership_token,
                        target_before_kind=target_before_kind, target_before_revision=target_before_revision, native_operation_id=native_id,
                    ),
                )
                self._release_ownership(marker)
                receipt_id = str(operation_id or f"host-import-{uuid.uuid4().hex}")
                return replace(created, provenance={
                        "domain": "host",
                        "source_digest": "sha256:" + source_digest.hexdigest(),
                        "selected_name": selected_name,
                        "declared_mime": declared_mime[:256] if declared_mime else None,
                        "detected_mime": detected_mime,
                }, revision=revision, action_receipt={
                    "receipt_id": receipt_id,
                    "status": "complete",
                    "phase": "complete",
                    "durable": True,
                    "history": _host_history_receipt(response),
                    "receipt": {"receipt_id": receipt_id, "outcome": "committed", "selected_name": selected_name, "revision": revision},
                })
            except FilesServiceError as exc:
                if exc.code in {"denied", "invalid_path", "malformed_request", "policy_generation_changed", "conflict"}:
                    _known_no_effect()
                if exc.code != "conflict" or collision != "rename" or attempt >= 100:
                    if exc.code == "conflict":
                        failed_marker = {**marker, "phase": "failed"}
                        try:
                            self._operation_save(context, operation_id, digest, failed_marker)
                        finally:
                            self._release_ownership(marker)
                        raise FilesFacadeError("Host import destination already exists", code="resource_changed") from exc
                    self._release_ownership(marker)
                    raise _service_error(exc) from exc
                self._release_ownership(marker)
                stem, suffix = os.path.splitext(str(name))
                candidate = f"{stem} ({attempt + 2}){suffix}"
        try:
            await stage_abort(stage_id)
        except Exception:
            pass
        raise FilesFacadeError("Host import collision could not be resolved", code="resource_changed")

    async def resource_for_path(self, context: ProviderContext, *, path: str) -> ProviderResource:
        """Mint provider metadata for a server-resolved Workspace path.

        The absolute path stays inside the provider. ``stat`` re-authorizes it
        through the current Rust App scope before the facade issues an opaque
        ResourceRef.
        """

        return await self.stat(context, origin_id=_origin(path))

    async def workspace_target(
        self,
        context: ProviderContext,
        *,
        origin_id: str,
    ) -> ProviderWorkspaceTarget:
        """Resolve one already-opaque Host ref for the Workspace service.

        The canonical absolute path remains server-internal. A regular file is
        opened inside a Workspace rooted at its parent; a directory becomes the
        Workspace root itself. Exact-file-only principals will be denied by the
        canonical Workspace resolver because parent enumeration is not granted.
        """

        path = _path(origin_id)
        scope = self._scope(context)
        try:
            response = await self._client(context, scope).request(
                "stat",
                path,
                {"include_fingerprint": False},
            )
        except FilesServiceError as exc:
            raise _service_error(exc) from exc
        data = response.get("data") or {}
        canonical = str(data.get("path") or path)
        kind = str(data.get("kind") or "other").strip().lower()
        if kind in {"directory", "recursive_directory", "folder"}:
            return ProviderWorkspaceTarget(
                origin_id=origin_id,
                directory_path=canonical,
                name=Path(canonical).name or "Workspace",
            )
        if kind in {"file", "exact_file"}:
            target = Path(canonical)
            return ProviderWorkspaceTarget(
                origin_id=origin_id,
                directory_path=str(target.parent),
                open_relative=target.name,
                name=target.parent.name or "Workspace",
            )
        raise FilesFacadeError("Host resource cannot become a Workspace", code="resource_unavailable")

    async def search(
        self,
        context: ProviderContext,
        *,
        query: str,
        limit: int,
        sort: Mapping[str, Any],
    ) -> ProviderPage:
        anchors = await self.children(
            context,
            parent_origin_id="host:v1:root",
            cursor=None,
            snapshot=None,
            limit=200,
            sort=sort,
            query="",
        )
        scope = self._scope(context)
        search_anchors = anchors.entries
        if scope.get("host"):
            # An administrator's namespace includes `/`, but selecting All
            # Sources must not silently turn an ordinary search into an eager
            # whole-disk crawl. Search the explicit default/Home anchor; the
            # user can enter `/` and search it deliberately when desired.
            favorites = tuple(row for row in search_anchors if row.provenance.get("favorite"))
            search_anchors = favorites or search_anchors[:1]
        by_origin: dict[str, ProviderResource] = {}
        folded = query.casefold()
        for anchor in search_anchors:
            if anchor.kind != "folder":
                if folded in anchor.name.casefold():
                    by_origin.setdefault(anchor.origin_id, anchor)
                continue
            try:
                page = await self.children(
                    context,
                    parent_origin_id=anchor.origin_id,
                    cursor=None,
                    snapshot=None,
                    limit=limit,
                    sort=sort,
                    query=query,
                )
            except FilesFacadeError as exc:
                if exc.code == "resource_unavailable":
                    continue
                raise
            for entry in page.entries:
                by_origin.setdefault(entry.origin_id, entry)
        rows = list(by_origin.values())
        key = str(sort.get("key") or "name")
        value = {
            "name": lambda row: row.name.casefold(),
            "kind": lambda row: (row.kind.casefold(), row.name.casefold()),
            "size": lambda row: (int(row.size or 0), row.name.casefold()),
            "modified": lambda row: (int(row.modified_unix_ms or 0), row.name.casefold()),
        }[key]
        rows.sort(key=lambda row: row.origin_id)
        rows.sort(key=value, reverse=str(sort.get("direction") or "asc") == "desc")
        if bool(sort.get("directories_first", True)):
            rows.sort(key=lambda row: row.kind != "folder")
        bounded = rows[:limit]
        digest = hashlib.sha256()
        digest.update(query.encode("utf-8"))
        for row in bounded:
            digest.update(f"\0{row.origin_id}\0{row.kind}\0{row.size}\0{row.modified_unix_ms}".encode("utf-8"))
        return ProviderPage(
            tuple(bounded),
            next_cursor="truncated" if len(rows) > limit else None,
            total=len(rows),
            snapshot=f"host-global-search-{digest.hexdigest()[:24]}",
        )

    async def query_base(
        self,
        context: ProviderContext,
        *,
        base_origin_id: str,
        corpus_origin_id: str,
        view_id: str | None = None,
        query: Mapping[str, Any] | None = None,
        page: int = 0,
        page_size: int = 100,
        context_origin_id: str | None = None,
        draft_definition: str | None = None,
    ) -> Mapping[str, Any]:
        """Take one bounded, metadata-only recursive Host snapshot.

        Every directory page is authorized by the Rust-backed ``children``
        call.  Paths are converted to corpus-relative POSIX labels only inside
        this provider, and the facade never receives an absolute host path.
        The page argument is applied once, after deterministic snapshot
        ordering; this avoids Copal's former double-slice/empty-page bug.
        """
        base = await self.stat(context, origin_id=base_origin_id)
        corpus = await self.stat(context, origin_id=corpus_origin_id)
        if base.kind not in {"file", "folder"} or corpus.kind not in {"folder", "provider_root"} or "children" not in corpus.capabilities:
            raise FilesFacadeError("Host Base corpus is unavailable", code="resource_unavailable")
        if context_origin_id is not None:
            context_entry = await self.stat(context, origin_id=context_origin_id)
            if context_entry.kind not in {"file", "folder"} or not {"read", "open", "stat"}.intersection(context_entry.capabilities):
                raise FilesFacadeError("Host Base context is unavailable", code="resource_unavailable")
        page_number = int(page)
        size = int(page_size)
        corpus_path = None if corpus_origin_id == "host:v1:root" else _path(corpus_origin_id)
        pending: list[tuple[str, str, int]] = [(corpus_origin_id, "", 0)]
        collected: list[tuple[str, ProviderResource, str]] = []
        complete = True
        folder_page_counts: dict[str, int] = {}
        total_work = 0
        while pending:
            parent_origin, parent_logical, depth = pending.pop(0)
            if depth > self._BASE_MAX_DEPTH:
                complete = False
                continue
            cursor = None
            snapshot = None
            while True:
                folder_page_counts[parent_origin] = folder_page_counts.get(parent_origin, 0) + 1
                total_work += 1
                if total_work > self._BASE_MAX_ROWS:
                    complete = False
                    pending.clear()
                    break
                result = await self.children(
                    context, parent_origin_id=parent_origin, cursor=cursor,
                    snapshot=snapshot, limit=200,
                    sort={"key": "name", "direction": "asc", "directories_first": True}, query="",
                )
                snapshot = result.snapshot or snapshot
                for entry in result.entries:
                    if len(collected) >= self._BASE_MAX_ROWS:
                        complete = False
                        pending.clear()
                        break
                    if corpus_path is None:
                        logical = entry.name
                    else:
                        try:
                            logical = os.path.relpath(_path(entry.origin_id), corpus_path).replace(os.sep, "/")
                        except FilesFacadeError:
                            # An adapter row without a resolvable provider
                            # origin cannot become a Base row.
                            continue
                    if logical in {"", "."} or logical == ".." or logical.startswith("../"):
                        continue
                    collected.append((logical, entry, entry.origin_id))
                    if entry.kind == "folder":
                        pending.append((entry.origin_id, logical, depth + 1))
                if len(collected) >= self._BASE_MAX_ROWS:
                    complete = False
                    pending.clear()
                    break
                if result.next_cursor is None:
                    break
                if folder_page_counts[parent_origin] >= self._BASE_MAX_PAGES_PER_FOLDER:
                    complete = False
                    break
                cursor = result.next_cursor
            if not complete and not pending:
                break

        collected.sort(key=lambda item: (item[0].casefold(), item[0], item[1].origin_id))
        digest = hashlib.sha256()
        digest.update(str(context.owner_subject_id).encode("utf-8"))
        digest.update(str(context.policy_generation).encode("ascii"))
        digest.update(str(corpus_origin_id).encode("utf-8"))
        for logical, entry, _origin_id in collected:
            digest.update(f"\0{logical}\0{entry.kind}\0{entry.size}\0{entry.modified_unix_ms}".encode("utf-8"))
        snapshot_id = f"host-base-{digest.hexdigest()[:32]}"
        offset = page_number * size
        selected = collected[offset:offset + size]
        rows = [{
            "origin_id": origin_id,
            "logical_path": logical,
            "metadata": {"name": entry.name, "kind": entry.kind, "mime_type": entry.mime_type, "size": entry.size, "modified_unix_ms": entry.modified_unix_ms, "capabilities": list(entry.capabilities)},
            "properties": {}, "relations": [],
        } for logical, entry, origin_id in selected]
        return {
            "snapshot_id": snapshot_id,
            "complete": bool(complete),
            "indexing": not bool(complete),
            "truncated": not bool(complete),
            "status": "complete" if complete else "truncated",
            "progress": {"visited": len(collected), "limit": self._BASE_MAX_ROWS},
            "rows": rows,
            "total": len(collected),
        }

    async def content(self, context: ProviderContext, *, origin_id: str) -> ProviderContent:
        path = _path(origin_id)
        scope = self._scope(context)
        client = self._stream_client(context, scope)
        try:
            opened = await client.open_handle(path)
        except FilesServiceError as exc:
            raise _service_error(exc) from exc
        handle = opened.get("handle")
        if not isinstance(handle, dict):
            raise FilesFacadeError("Host content transport is unavailable", code="provider_unavailable")
        size = int(opened.get("size") or 0)
        modified = opened.get("modified_unix_ms")
        object_tag = str(opened.get("object_tag") or "").strip()
        filename = Path(path).name or "download"

        async def stream(start: int, length: int):
            offset = int(start)
            remaining = int(length)
            while remaining > 0:
                window = min(remaining, 250 * 1024 * 1024)
                before = offset
                try:
                    async for chunk in client.stream_read_handle(
                        handle,
                        offset=offset,
                        length=window,
                    ):
                        offset += len(chunk)
                        remaining -= len(chunk)
                        yield chunk
                except FilesServiceError as exc:
                    raise _service_error(exc) from exc
                # A short object stream must not spin another request.
                if offset - before < window:
                    break

        return ProviderContent(
            origin_id=origin_id,
            filename=filename,
            media_type=mimetypes.guess_type(filename)[0] or "application/octet-stream",
            size=size,
            modified_unix_ms=int(modified) if modified is not None else None,
            etag=object_tag or None,
            stream=stream,
        )

    async def open_resource(self, context: ProviderContext, *, origin_id: str) -> Mapping[str, Any] | None:
        """Read one authorized Host text file into the unified Editor DTO.

        The path remains provider-private. Rust performs the authorization,
        decoding, BOM/newline detection, and fingerprinting through its
        ``read_lines`` operation; the browser receives only the immutable text
        snapshot and an opaque ref for subsequent CAS writes.
        """
        path = _path(origin_id)
        scope = self._scope(context)
        try:
            response = await self._client(context, scope).request("read_lines", path, {})
        except FilesServiceError as exc:
            raise _service_error(exc) from exc
        data = response.get("data") or {}
        text = data.get("text")
        fingerprint = data.get("fingerprint")
        if not isinstance(text, str) or not isinstance(fingerprint, Mapping):
            raise FilesFacadeError("Host text resource is unavailable", code="resource_unavailable")
        fingerprint_value = str(fingerprint.get("value") or "").strip()
        if not fingerprint_value:
            raise FilesFacadeError("Host text resource has no stable fingerprint", code="provider_unavailable")
        name = Path(path).name or "Untitled"
        editable = self._path_writable(context, path, scope)
        representation = self._representation(name, data)
        metadata = self._metadata(data)
        return {
            "name": name,
            "kind": "text",
            "corpus": "host",
            "text": text,
            "representation": representation,
            "encoding": data.get("encoding"),
            "newline": data.get("newline"),
            "bom_bytes": data.get("bom_bytes"),
            "mode": data.get("mode"),
            "language": data.get("language"),
            "properties": {},
            "relations": [],
            "tags": [],
            "read_only": not editable,
            "resource": {
                "key": {
                    "accountId": str(context.owner_subject_id),
                    "workspaceId": "host",
                    "provider": "host",
                    "resourceId": stable_resource_id(
                        owner_subject_id=str(context.owner_subject_id),
                        provider="host",
                        origin_id=origin_id,
                    ),
                },
                "revision": {"kind": "hostFingerprint", "value": fingerprint_value},
                # The provider's explicit classification is authoritative. A
                # suffix is never inspected by Notes itself.
                "representation": representation,
                "locator": {"displayName": name, "locationLabel": name},
                "metadata": metadata,
                "capabilities": {
                    "read": True,
                    "edit": editable,
                    "rename": False,
                    "move": False,
                    "trash": False,
                    "attach": False,
                    "reveal": True,
                },
            },
        }

    async def host_applications(self, context: ProviderContext, *, origin_id: str) -> list[dict[str, str]]:
        """Discover installed apps only after the provider reauthorizes the ref."""
        entry = await self.stat(context, origin_id=origin_id)
        if entry.kind != "file" or "open" not in entry.capabilities:
            raise FilesFacadeError("Host resource cannot be opened", code="resource_unavailable")
        try:
            return await host_apps().discover_async(await self._authorized_path(context, origin_id))
        except HostAppsError as error:
            raise FilesFacadeError(str(error), code=error.code) from error

    async def open_on_host(self, context: ProviderContext, *, origin_id: str, app_id: str) -> Mapping[str, str]:
        """Reauthorize and privately resolve the path for one native launch."""
        entry = await self.stat(context, origin_id=origin_id)
        if entry.kind != "file" or "open" not in entry.capabilities:
            raise FilesFacadeError("Host resource cannot be opened", code="resource_unavailable")
        try:
            return await host_apps().launch_async(await self._authorized_path(context, origin_id), app_id)
        except HostAppsError as error:
            raise FilesFacadeError(str(error), code=error.code) from error

    async def _authorized_path(self, context: ProviderContext, origin_id: str) -> str:
        """Return the canonical path from the current authorized service stat."""
        requested = _path(origin_id)
        scope = self._scope(context)
        try:
            response = await self._client(context, scope).request("stat", requested, {"include_fingerprint": True})
        except FilesServiceError as exc:
            raise _service_error(exc) from exc
        canonical = str((response.get("data") or {}).get("path") or "")
        if not canonical or not Path(canonical).is_absolute() or "\x00" in canonical:
            raise FilesFacadeError("Host resource is unavailable", code="resource_unavailable")
        return canonical

    def _collaboration_client(self, context: ProviderContext):
        scope=self._scope(context)
        if self.client_factory is client_for_owner:
            return self.client_factory(context.owner_username,app_scope=None if scope.get('host') else scope,transport='framed')
        return self._client(context,scope)

    async def collaboration_commit(self, target, reservation, *, base: bytes, base_sha256: str, edits: list, before_issue=None) -> Mapping[str, Any]:
        """One reserved text edit; preserve the existing text rebase and custody."""
        from src.openclank.collaboration_text import rebase_text_edits, TextEditConflict
        def prepare(source):
            return rebase_text_edits(base=base,base_sha256=base_sha256,current=source,edits=edits)['result']
        return await self._collaboration_commit_prepared(target,reservation,prepare=prepare,conflict=TextEditConflict,before_issue=before_issue)

    async def collaboration_workbook_commit(self, target, reservation, *, base: bytes, base_sha256: str, cells: list, before_issue=None) -> Mapping[str, Any]:
        """Prepare stable-cell edits against the current workbook, then use the same native write owner."""
        from src.openclank.collaboration_workbook import prepare_workbook, WorkbookConflict
        def prepare(source):
            result=prepare_workbook(base.decode('utf-8'),base_sha256,source.decode('utf-8'),cells)
            desired=result['text'].encode('utf-8')
            if hashlib.sha256(desired).hexdigest()!=result['sha256']:
                raise ValueError('Workbook result unavailable')
            return desired
        return await self._collaboration_commit_prepared(target,reservation,prepare=prepare,conflict=WorkbookConflict,before_issue=before_issue)

    async def collaboration_slides_commit(self, target, reservation, *, base: bytes, base_sha256: str, objects: list, before_issue=None) -> Mapping[str, Any]:
        """Prepare stable slide-text edits, then use the existing native write owner."""
        from src.openclank.collaboration_slides import prepare_slides, SlidesConflict
        def prepare(source):
            result=prepare_slides(base.decode('utf-8'),base_sha256,source.decode('utf-8'),objects)
            desired=result['text'].encode('utf-8')
            if hashlib.sha256(desired).hexdigest()!=result['sha256']:
                raise ValueError('Slide result unavailable')
            return desired
        return await self._collaboration_commit_prepared(target,reservation,prepare=prepare,conflict=SlidesConflict,before_issue=before_issue)

    async def git_discard_commit(self, target, reservation, *, expected_before: bytes, desired: bytes, before_issue=None) -> Mapping[str, Any]:
        """Human-reviewed exact UTF-8 Git discard through the ordinary-replace owner."""
        class DiscardConflict(ValueError): pass
        for value in (expected_before,desired):
            if type(value) is not bytes or len(value)>262144 or b'\x00' in value:
                raise FilesFacadeError('Git discard text unavailable',code='unsupported_operation')
            try: value.decode('utf-8','strict')
            except UnicodeError as exc: raise FilesFacadeError('Git discard text unavailable',code='unsupported_operation') from exc
        def prepare(source):
            if source!=expected_before: raise DiscardConflict('Git file changed')
            return desired
        return await self._collaboration_commit_prepared(target,reservation,prepare=prepare,conflict=DiscardConflict,before_issue=before_issue,human_discard=True)

    async def _collaboration_commit_prepared(self, target, reservation, *, prepare, conflict, before_issue=None, human_discard=False) -> Mapping[str, Any]:
        """One reserved edit; original task retains the target and native custody."""
        store=self.operation_store; owner=target.owner_subject_id; operation=reservation.operation; digest=reservation.digest
        claim=None; prepared=None
        def row():
            value=store.get_operation(owner_subject_id=owner,operation_id=operation)
            if not value or value.get('digest')!=digest:raise FilesFacadeError('Shared edit unavailable',code='resource_unavailable')
            return value['receipt']
        def refuse(state):
            store.refuse_collaboration_unclaimed(owner_subject_id=owner,operation_id=operation,request_digest=digest,state=state)
            return row()
        async def wait_original_exit(process):
            # The existing receipt can be settled by an exact status query.
            # No native call or new issue is retried from this owned task.
            while process.poll() is None:
                try:observed=row()
                except Exception:observed=None
                if observed is not None and observed.get('state') in {'complete','conflict','unavailable'}:return False
                try:await asyncio.sleep(0.5)
                except asyncio.CancelledError:continue
            return True
        if target.provider is not self or reservation.owner!=owner:
            raise FilesFacadeError('Shared editing unavailable',code='resource_unavailable')
        if human_discard:
            if target.role!='owner' or row().get('kind')!='git.discard.v1': raise FilesFacadeError('Git discard unavailable',code='resource_unavailable')
        elif target.role!='editor':
            raise FilesFacadeError('Shared editing unavailable',code='resource_unavailable')
        try:
            current=await (self.git_discard_read(target) if human_discard else self.collaboration_read(target))
            source=current['bytes'] if human_discard else current['text'].encode('utf-8')
            try: desired=prepare(source)
            except conflict:return refuse('conflict')
            if len(desired)>262144 or desired==source:return refuse('unavailable')
            path=_path(target.resource['origin_id']); client=self._collaboration_client(target.context)
            if getattr(client,'transport',None)!='framed':return refuse('unavailable')
            action_id=('git-discard-' if human_discard else 'collab-')+hashlib.sha256(operation.encode('utf-8')).hexdigest()
            expected=current['revision']['value'];desired_sha=hashlib.sha256(desired).hexdigest()
            material=['openclank.files.ordinary-replace.v1',owner,path,expected,desired_sha,operation,action_id]
            native_digest=hashlib.sha256(json.dumps(material,ensure_ascii=False,separators=(',',':')).encode('utf-8')).hexdigest()
            binding={'operation_id':operation,'action_id':action_id,'request_digest':native_digest,'timestamp_millis':int(time.time()*1000),
                'expected_fingerprint':expected,'desired_sha256':desired_sha,'desired_size':len(desired)}
            prepared=await client.prepare_ordinary_replace(path,binding=binding)
            capture=dict(prepared.capture_identity)
            if capture['account_id']!=owner or not all(isinstance(value,str) and value for value in capture.values()) or not await target.current():
                return refuse('unavailable')
            claim=store.claim_collaboration_text(owner_subject_id=owner,operation_id=operation,request_digest=digest,
                generation=target.context.policy_generation,workspace_id=target.context.workspace_id,path=path,binding=binding,capture_identity=capture)
            if before_issue is not None:
                try: allowed=await before_issue()
                except Exception: allowed=False
                if allowed is not True:
                    return store.settle_collaboration_text(owner_subject_id=owner,operation_id=operation,request_digest=digest,claim=claim,known_unsent=True)
            if not await target.current():
                return store.settle_collaboration_text(owner_subject_id=owner,operation_id=operation,request_digest=digest,claim=claim,known_unsent=True)
            store.mark_collaboration_issued(owner_subject_id=owner,operation_id=operation,request_digest=digest,claim=claim)
            try:
                await client.ordinary_replace(prepared,content=desired)
            except FilesServiceError as exc:
                if exc.code=='ordinary_write_unsent':
                    return store.settle_collaboration_text(owner_subject_id=owner,operation_id=operation,request_digest=digest,claim=claim,known_unsent=True)
                raise
            store.retire_collaboration_io(owner_subject_id=owner,operation_id=operation,request_digest=digest,claim=claim)
            try:
                status=await client.ordinary_replace_status(prepared)
                evidence=status.get('data') if isinstance(status,Mapping) else None
                result=store.settle_collaboration_text(owner_subject_id=owner,operation_id=operation,request_digest=digest,claim=claim,native_status=evidence)
                if result.get('state') == 'complete':
                    from src.openclank.resource_event_stream import resource_invalidations
                    resource_invalidations.publish(owner,target.resource['resource_id'])
                return result
            except Exception:
                # The original result is not terminal proof. Keep both account
                # admissions until its original supervised process exits.
                await wait_original_exit(prepared.process)
                return row()
        except BaseException:
            if claim is None:
                try:return refuse('unavailable')
                except Exception:raise
            # The exact native process owns any possibly issued work. Do not
            # release either admitted account until its positive original exit.
            if prepared is not None and prepared.used:
                # The owned exchange can settle even when its reply is lost.
                # That permits exact status, not release or a new issue.
                retired=prepared.settled is True
                if retired:store.retire_collaboration_io(owner_subject_id=owner,operation_id=operation,request_digest=digest,claim=claim)
                exited=await wait_original_exit(prepared.process)
                if exited and not retired:store.retire_collaboration_io(owner_subject_id=owner,operation_id=operation,request_digest=digest,claim=claim)
            else:
                try:return store.settle_collaboration_text(owner_subject_id=owner,operation_id=operation,request_digest=digest,claim=claim,known_unsent=True)
                except Exception:pass
            return row()

    async def collaboration_status(self, target, receipt: Mapping[str, Any]) -> Mapping[str, Any]:
        """Read exact original evidence only after original I/O retirement."""
        if target.provider is not self or receipt.get('state')!='pending' or receipt.get('retired') is not True:
            return receipt
        binding=receipt.get('native_binding');capture=receipt.get('capture_identity');claim=receipt.get('claim')
        if not isinstance(binding,Mapping) or not isinstance(capture,Mapping) or not isinstance(claim,Mapping):return receipt
        client=self._collaboration_client(target.context);path=_path(target.resource['origin_id'])
        if getattr(client,'transport',None)!='framed':return receipt
        try:
            if not await target.current():return receipt
            status=await client.ordinary_replace_status_exact(path,binding=dict(binding),capture_identity=dict(capture))
            # Persist positive original evidence even if access was revoked;
            # the public caller independently rechecks before disclosure.
            evidence=status.get('data') if isinstance(status,Mapping) else None
            row=self.operation_store.get_operation(owner_subject_id=target.owner_subject_id,operation_id=binding['operation_id'])
            if not row:return receipt
            result=self.operation_store.settle_collaboration_text(owner_subject_id=target.owner_subject_id,operation_id=binding['operation_id'],
                request_digest=row['digest'],claim=claim,native_status=evidence)
            if result.get('state') == 'complete':
                from src.openclank.resource_event_stream import resource_invalidations
                resource_invalidations.publish(target.owner_subject_id,target.resource['resource_id'])
            return result
        except Exception:return receipt

    async def git_discard_read(self, target) -> Mapping[str, Any]:
        """Read exact bounded UTF-8 bytes under the human target's object identity."""
        if getattr(target,'provider',None) is not self or not await target.current():
            raise FilesFacadeError('Git discard unavailable',code='resource_unavailable')
        origin=target.resource['origin_id'];content=await self.content(target.context,origin_id=origin)
        if (content.origin_id!=origin or type(content.size) is not int or not 0<=content.size<=262144
                or content.etag!=target.revision.get('value') or content.stream is None):
            raise FilesFacadeError('Git discard unavailable',code='resource_unavailable')
        raw=bytearray()
        async for chunk in content.stream(0,content.size):
            if not isinstance(chunk,bytes) or len(raw)+len(chunk)>content.size or not await target.current():
                raise FilesFacadeError('Git discard changed',code='resource_changed')
            raw.extend(chunk)
        if len(raw)!=content.size or not await target.current() or b'\x00' in raw:
            raise FilesFacadeError('Git discard changed',code='resource_changed')
        try: bytes(raw).decode('utf-8','strict')
        except UnicodeError as exc: raise FilesFacadeError('Git discard unavailable',code='unsupported_operation') from exc
        fingerprint='sha256:'+hashlib.sha256(raw).hexdigest()+':'+str(len(raw))
        return {'revision':{'kind':'hostFingerprint','value':fingerprint},'bytes':bytes(raw)}

    async def collaboration_read(self, target) -> Mapping[str, Any]:
        """Read one current role-qualified shared file as bounded exact UTF-8."""
        from src.openclank.collaboration_text import TextEditError
        if getattr(target, 'provider', None) is not self or not await target.current():
            raise FilesFacadeError('Shared text unavailable', code='resource_unavailable')
        origin=target.resource['origin_id']
        content=await self.content(target.context, origin_id=origin)
        if (content.origin_id!=origin or type(content.size) is not int or not 0<=content.size<=262144
                or content.etag!=target.revision.get('value') or content.stream is None):
            raise FilesFacadeError('Shared text unavailable', code='resource_unavailable')
        raw=bytearray()
        async for chunk in content.stream(0,content.size):
            if not isinstance(chunk,bytes) or len(raw)+len(chunk)>content.size or not await target.current():
                raise FilesFacadeError('Shared text changed', code='resource_changed')
            raw.extend(chunk)
        if len(raw)!=content.size or not await target.current():
            raise FilesFacadeError('Shared text changed', code='resource_changed')
        try:
            data=bytes(raw)
            if b'\x00' in data or b'\r' in data or data.startswith(b'\xef\xbb\xbf'):raise TextEditError('unsupported text')
            text=data.decode('utf-8','strict')
        except (UnicodeError,TextEditError) as exc:
            raise FilesFacadeError('Shared text unavailable',code='resource_unavailable') from exc
        fingerprint='sha256:'+hashlib.sha256(data).hexdigest()+':'+str(len(data))
        return {'version':1,'resource_id':target.resource['resource_id'],'role':target.role,
                'revision':{'kind':'hostFingerprint','value':fingerprint},'text':text}

    async def save_raster(self, context: ProviderContext, *, origin_id: str, expected: str,
                          media_type: str, content: bytes) -> Mapping[str, Any]:
        """Replace pixels through the ordinary native Files claim, History and fingerprint check."""
        path = _path(origin_id)
        scope = self._scope(context)
        revision = {"kind": "hostFingerprint", "value": expected}

        async def preflight():
            entry = await self.stat(context, origin_id=origin_id)
            if (entry.origin_id != origin_id or entry.kind != "file" or entry.mime_type != media_type
                    or "write" not in entry.capabilities or not self._path_writable(context, path, scope)):
                raise FilesFacadeError("Image is unavailable for writing", code="resource_unavailable")
            if entry.revision != revision:
                raise FilesFacadeError("Image changed since opening", code="resource_changed")

        async def write(begin_mutation, known_no_effect):
            client = self._client(context, scope)
            stage = (await client.stage_begin(os.path.dirname(path)))["data"]
            stage_id = stage["stage_id"]
            digest = "sha256:" + hashlib.sha256(content).hexdigest()
            try:
                if len(content) > stage["max_total_bytes"]:
                    raise FilesFacadeError(f"Encoded image exceeds the current Files write limit of {stage['max_total_bytes']} bytes",
                                          code="upload_too_large")
                step = min(stage["max_chunk_bytes"], 512 * 1024)
                if step <= 0:
                    raise FilesFacadeError("Invalid native upload chunk size", code="provider_unavailable")
                for offset in range(0, len(content), step):
                    await client.stage_chunk(stage_id, content[offset:offset + step], offset=offset)
                await preflight()
                begin_mutation()
                response = await client.stage_finish(stage_id, path, length=len(content), digest=digest,
                                                     expected_fingerprint=expected)
            finally:
                try:
                    await client.stage_abort(stage_id)
                except FilesServiceError:
                    pass
            fingerprint = (response.get("data") or {}).get("Replaced", {}).get("fingerprint")
            value = f"{digest}:{len(content)}"
            if fingerprint != {"algorithm": "sha256", "value": value}:
                raise FilesFacadeError("Image write requires reconciliation", code="mutation_outcome_unknown")
            return {"outcome": "applied", "revision": {"kind": "hostFingerprint", "value": value},
                    "history": _host_history_receipt(response)}

        try:
            return await self._run_claimed_host_mutation(context, [path], write, preflight=preflight, delayed_start=True)
        except FilesServiceError as exc:
            raise _service_error(exc) from exc

    async def save_resource(
        self,
        context: ProviderContext,
        *,
        origin_id: str,
        expected_revision: Mapping[str, Any],
        text: str,
        before_dispatch: Callable[..., Any] | None = None,
    ) -> Mapping[str, Any]:
        """CAS-save a Host text snapshot under one path claim."""
        path = _path(origin_id)
        scope = self._scope(context)
        if not self._path_writable(context, path, scope):
            raise FilesFacadeError("Host resource is read-only", code="resource_unavailable")
        expected_kind = str(expected_revision.get("kind") or "").strip()
        expected_value = str(expected_revision.get("value") or "").strip()
        if expected_kind != "hostFingerprint" or not expected_value:
            raise FilesFacadeError("Host save revision is invalid", code="invalid_resource_request")
        if not isinstance(text, str):
            raise FilesFacadeError("Host save text is invalid", code="invalid_resource_request")
        client = self._client(context, scope)
        name = Path(path).name or "Untitled"
        normalize = lambda value: str(value).replace("\r\n", "\n").replace("\r", "\n")

        async def read_snapshot() -> dict[str, Any]:
            response = await client.request("read_lines", path, {})
            data = response.get("data") or {}
            if not isinstance(data, Mapping):
                raise FilesFacadeError("Host text resource snapshot is unavailable", code="provider_unavailable")
            return self._snapshot(name, dict(data), self._representation(name, data))

        async def work(begin_mutation, known_no_effect):
            current = await read_snapshot()
            if current["revision"]["value"] != expected_value:
                return {"outcome": "conflict", "remote": current}
            if normalize(current["envelope"]["text"]) == normalize(text):
                return {"outcome": "applied", "revision": current["revision"], "snapshot": current,
                        "unchanged": True, "history": {"state": "not_applicable"}}
            expected_fingerprint = {"algorithm": "sha256", "value": expected_value}
            raw = current["envelope"]["text"]
            if raw == "":
                # Decode metadata is already validated by _snapshot.
                response = await client.request("read_lines", path, {})
                data = response.get("data") or {}
                check = self._snapshot(name, data, self._representation(name, data))
                if check["revision"]["value"] != expected_value:
                    return {"outcome": "conflict", "remote": check}
                encoding = {"Utf8": "utf-8", "Utf16Le": "utf-16-le", "Utf16Be": "utf-16-be", "Utf32Le": "utf-32-le", "Utf32Be": "utf-32-be"}.get(str(data.get("encoding")), str(data.get("encoding") or ""))
                encodings = {"utf-8": ("utf-8", b"\xef\xbb\xbf"), "utf-16-le": ("utf-16-le", b"\xff\xfe"), "utf-16-be": ("utf-16-be", b"\xfe\xff"), "utf-32-le": ("utf-32-le", b"\xff\xfe\x00\x00"), "utf-32-be": ("utf-32-be", b"\x00\x00\xfe\xff")}
                if encoding not in encodings:
                    raise FilesFacadeError("Host text encoding is unavailable", code="provider_unavailable")
                codec, bom = encodings[encoding]
                encoded = (bom if data.get("bom_bytes") else b"") + text.encode(codec)
                operation, payload = "replace", {"bytes": list(encoded), "expected_fingerprint": expected_fingerprint}
            else:
                operation, payload = "patch", {"old": raw, "new": text, "replace_all": True, "expected_fingerprint": expected_fingerprint}
            if before_dispatch is not None:
                await before_dispatch()
            begin_mutation()
            try:
                response = await client.request(operation, path, payload)
            except FilesServiceError as exc:
                if exc.code in {"denied", "invalid_path", "malformed_request", "policy_generation_changed", "conflict"}:
                    known_no_effect()
                raise
            data = response.get("data") if isinstance(response, Mapping) else None
            if operation == "patch":
                fingerprint = data.get("new_fingerprint") if isinstance(data, Mapping) else None
            else:
                replaced = data.get("Replaced") if isinstance(data, Mapping) else None
                fingerprint = replaced.get("fingerprint") if isinstance(replaced, Mapping) else None
            if not isinstance(fingerprint, Mapping) or fingerprint.get("algorithm") != "sha256" or not isinstance(fingerprint.get("value"), str) or not re.fullmatch(r"sha256:[0-9a-f]{64}:[0-9]+", fingerprint["value"]):
                raise FilesFacadeError("Host mutation outcome requires reconciliation", code="mutation_outcome_unknown")
            revision = {"kind": "hostFingerprint", "value": fingerprint["value"]}
            result = {"outcome": "applied", "revision": revision, "history": _host_history_receipt(response), "readback": "unavailable"}
            try:
                latest = await read_snapshot()
            except Exception:
                return result
            if latest["revision"] != revision:
                return {**result, "readback": "changed", "changedAfterSave": True, "remote": latest}
            return {**result, "readback": "current", "snapshot": latest}

        try:
            return await self._run_claimed_host_mutation(context, [path], work, delayed_start=True)
        except FilesServiceError as exc:
            if exc.code == "conflict":
                return {"outcome": "conflict"}
            raise _service_error(exc) from exc
        except FilesFacadeError as exc:
            if exc.code == 'operation_pending':
                raise FilesFacadeError('Host save outcome requires reconciliation', code='mutation_outcome_unknown') from exc
            raise

    async def thumbnail(
        self,
        context: ProviderContext,
        *,
        origin_id: str,
        width: int,
        height: int,
        scale: float,
        icon: bool = False,
    ) -> bytes:
        path = _path(origin_id)
        scope = self._scope(context)
        client = self._stream_client(context, scope)
        phase = "open_handle"
        try:
            opened = await client.open_handle(path)
            handle = opened.get("handle")
            if not isinstance(handle, dict):
                raise FilesServiceError("Rust filesystem service returned an invalid handle")
            phase = "thumbnail_rpc"
            return await client.thumbnail_handle(
                handle,
                width=width,
                height=height,
                scale=scale,
                **({"icon": True} if icon else {}),
            )
        except FilesServiceError as exc:
            code = exc.code if type(exc.code) is str and exc.code in {
                "denied", "invalid_path", "policy_generation_changed", "conflict",
                "stale_cursor", "backpressure", "stale_handle", "root_unavailable",
                "unsupported", "protocol_mismatch", "partial_stream", "deadline_exceeded",
                "cancelled", "malformed_request", "history_binding_changed", "unauthorized",
            } else "other"
            if code in {"invalid_path", "unsupported"}:
                _LOG.debug("Host native image unavailable phase=%s code=%s kind=%s",
                           phase, code, "icon" if icon else "thumbnail")
                raise FilesFacadeError("Host native image is unavailable", code="resource_unavailable") from exc
            _LOG.warning("Host native image failed phase=%s code=%s kind=%s",
                         phase, code, "icon" if icon else "thumbnail")
            raise _service_error(exc) from exc

    async def watch(self, context: ProviderContext, *, origin_id: str):
        if origin_id == "host:v1:root":
            raise FilesFacadeError("Host resource is unavailable", code="resource_unavailable")
        path = _path(origin_id)
        scope = self._scope(context)
        client = self._stream_client(context, scope)
        try:
            async for event in client.watch_path(path):
                yield event
        except FilesServiceError as exc:
            raise _service_error(exc) from exc


__all__ = ["HostFilesProvider"]
