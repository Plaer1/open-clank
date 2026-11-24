"""Host document attachment target for the Files preparation seam.

Host documents are Files resources addressed by opaque resource refs, never by
synthetic Copal IDs.  This adapter materializes an authorized source into the
approved workspace-relative ``media/<stem>/<collision-safe name>`` layout (or
the loose document's containing folder) and records provenance in the existing
Files operation store.  The target document itself is never edited here; the
insertion descriptor is returned to the Editor for its own revision-checked
write.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import posixpath
import re
import tempfile
import threading
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from src.openclank.files_facade import FilesFacadeError, ProviderContext, ProviderResource
from src.openclank.history_capture import begin_file_capture, complete_file_capture
from src.openclank.media_ownership import (
    MediaOwnershipError,
    MediaProvenance,
    WorkspaceRootRecord,
    allocate_media_name,
    binary_digest,
    classify_adoption_candidates,
    classify_document_origin,
    document_stem as _document_stem,
    layout_for_absolute,
    rewrite_reference,
    scan_references,
    text_fingerprint,
)

_LIMIT = 10 * 1024 * 1024
_ADOPTION_MUTEX = threading.RLock()


def _path_within(root: str, candidate: str) -> bool:
    """Return true only for a real path contained by the authorized root."""
    try:
        return os.path.commonpath([os.path.realpath(root), os.path.realpath(candidate)]) == os.path.realpath(root)
    except ValueError:
        return False


def _assert_workspace_media_path(root: str, candidate: str) -> None:
    """Fail closed when an authorized workspace media path crosses symlinks."""
    canonical_root = os.path.realpath(root)
    absolute = os.path.abspath(candidate)
    try:
        relative = os.path.relpath(absolute, root)
        if relative == os.pardir or relative.startswith(os.pardir + os.sep):
            raise FilesFacadeError("media path is outside the authorized workspace", code="invalid_destination")
        cursor = os.path.abspath(root)
        for part in relative.split(os.sep):
            if part in {"", "."}:
                continue
            cursor = os.path.join(cursor, part)
            if os.path.islink(cursor):
                raise FilesFacadeError("media path contains a symlink", code="invalid_destination")
        if not _path_within(canonical_root, absolute):
            raise FilesFacadeError("media path is outside the authorized workspace", code="invalid_destination")
    except ValueError as exc:
        raise FilesFacadeError("media path is outside the authorized workspace", code="invalid_destination") from exc


def _safe_receipt_path(root: str, value: Any, *, must_exist: bool = True) -> str | None:
    """Resolve a recovery path without trusting receipt-controlled traversal."""
    raw = str(value or "")
    if not raw:
        return None
    candidate = raw if os.path.isabs(raw) else os.path.join(root, raw)
    resolved = os.path.realpath(candidate)
    if not _path_within(root, resolved) or os.path.islink(candidate):
        return None
    if must_exist and (not os.path.isfile(resolved) or os.path.islink(resolved)):
        return None
    return resolved


def _relative_reference(document_id: str, destination_rel: str) -> str:
    """Render a workspace destination relative to its source document."""
    document = str(document_id or "").replace("\\", "/")
    parent = posixpath.dirname(document) or "."
    return posixpath.relpath(str(destination_rel).replace("\\", "/"), parent)


def _digest_json(payload: Mapping[str, Any]) -> str:
    import json

    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    ).hexdigest()


class HostDocumentAttachmentTarget:
    """Prepare a source as media beside one authorized writable Host document."""

    name = "host_document"

    def __init__(self, *, provider: Any, operation_store: Any, workspace_roots: Any | None = None):
        self.provider = provider
        self.operation_store = operation_store
        self._workspace_roots = workspace_roots

    # -- provenance persistence -------------------------------------------

    @staticmethod
    def _provenance_operation_id(operation_id: str) -> str:
        return f"__host_media__{operation_id}"

    def _load(self, context: ProviderContext, operation_id: str) -> dict[str, Any] | None:
        getter = getattr(self.operation_store, "get_operation", None)
        if not callable(getter):
            return None
        loaded = getter(
            owner_subject_id=context.owner_subject_id,
            operation_id=self._provenance_operation_id(operation_id),
        )
        return dict(loaded.get("receipt") or {}) if isinstance(loaded, Mapping) else None

    def _save(self, context: ProviderContext, operation_id: str, digest: str, receipt: Mapping[str, Any]) -> None:
        recorder = getattr(self.operation_store, "record_operation", None)
        if not callable(recorder):
            return
        recorder(
            owner_subject_id=context.owner_subject_id,
            operation_id=self._provenance_operation_id(operation_id),
            request_digest=digest,
            generation=int(context.policy_generation),
            receipt=dict(receipt),
            phase=str(receipt.get("phase") or "complete"),
        )

    def _registry_roots(self, owner_subject_id: str, *, require_complete: bool = False) -> list[WorkspaceRootRecord]:
        # A failed registry read is not an empty registry: classification must
        # see every root or it would absorb a neighbor.  Fail closed.
        if self._workspace_roots is not None:
            try:
                rows = self._workspace_roots(owner_subject_id)
            except Exception as exc:
                raise MediaOwnershipError(
                    "workspace registry is unavailable", code="provider_unavailable"
                ) from exc
            if require_complete and (not isinstance(rows, (list, tuple)) or any(not isinstance(row, Mapping) or not row.get("canonical_root", row.get("path")) for row in rows)):
                raise MediaOwnershipError("workspace registry is incomplete", code="provider_unavailable")
            return [
                WorkspaceRootRecord(
                    str(row.get("workspace_id") or row.get("id") or ""),
                    str(row.get("owner_subject_id") or row.get("owner") or ""),
                    str(row.get("canonical_root") or row.get("path") or ""),
                    bool(row.get("archived")),
                )
                for row in rows
                if isinstance(row, Mapping) and row.get("canonical_root", row.get("path"))
            ]
        getter = getattr(self.operation_store, "list_workspaces", None)
        if not callable(getter):
            if require_complete:
                raise MediaOwnershipError("workspace registry is unavailable", code="provider_unavailable")
            return []
        location_getter = getattr(self.operation_store, "get_location", None)
        records: list[WorkspaceRootRecord] = []
        # Server-side enumeration includes every account and archived row;
        # classification must see them all or it would absorb a neighbor.  A
        # failed registry read is not an empty registry: fail closed.
        try:
            workspaces = getter(owner_subject_id=None, include_archived=True)
        except Exception as exc:
            raise MediaOwnershipError(
                "workspace registry is unavailable", code="provider_unavailable"
            ) from exc
        for workspace in workspaces:
            location_id = getattr(workspace, "location_id", None)
            relative = str(getattr(workspace, "relative_folder", "") or "")
            if not location_id or not callable(location_getter):
                if require_complete:
                    raise MediaOwnershipError("workspace registry is incomplete", code="provider_unavailable")
                continue
            try:
                location = location_getter(str(location_id))
            except Exception:
                if require_complete:
                    raise MediaOwnershipError("workspace registry is incomplete", code="provider_unavailable")
                continue
            root = os.path.normpath(str(getattr(location, "canonical_path", "") or ""))
            if not root:
                if require_complete:
                    raise MediaOwnershipError("workspace registry is incomplete", code="provider_unavailable")
                continue
            if relative:
                root = os.path.normpath(os.path.join(root, *relative.split("/")))
            records.append(
                WorkspaceRootRecord(
                    str(getattr(workspace, "id", "") or ""),
                    str(getattr(workspace, "owner_subject_id", "") or ""),
                    root,
                    bool(getattr(workspace, "archived", False)),
                )
            )
        return records

    def _selected_roots(self, owner_subject_id: str) -> list[WorkspaceRootRecord]:
        try:
            return self._registry_roots(owner_subject_id, require_complete=True)
        except MediaOwnershipError as exc:
            raise FilesFacadeError("workspace registry is unavailable", code="provider_unavailable") from exc

    # -- typed attachment seam --------------------------------------------

    async def operation_status(self, context: ProviderContext, *, operation_id: str) -> Mapping[str, Any] | None:
        planned = self._load(context, operation_id)
        if not planned:
            return None
        if (
            planned.get("operation_id") != operation_id
            or planned.get("account_id") != context.owner_subject_id
            or int(planned.get("generation", -1)) != int(context.policy_generation)
        ):
            return None
        # A pending name/hash does not prove that this operation published it.
        # Legacy pending rows have no owned publication witness either.
        if planned.get("phase") != "complete":
            return None
        asset_path = str(planned.get("asset_path") or "")
        if not asset_path or not os.path.isfile(asset_path):
            return None
        expected = str(planned.get("source_digest") or "")
        try:
            with open(asset_path, "rb") as handle:
                data = handle.read(_LIMIT + 1)
        except OSError:
            return None
        if len(data) > _LIMIT:
            return None
        if binary_digest(data) != expected:
            return None
        complete = _public_receipt(planned)
        complete["generation"] = int(context.policy_generation)
        complete["asset"] = {
            **dict(planned.get("asset") or {}),
            "revision": {"kind": "contentDigest", "value": str(planned.get("digest_hex") or "")},
        }
        return complete

    async def prepare_attachment(
        self,
        context: ProviderContext,
        *,
        source: Mapping[str, Any],
        target: Mapping[str, Any],
        mode: str,
        operation_id: str,
        source_provider: Any | None = None,
        source_origin_id: str | None = None,
        source_entry: ProviderResource | None = None,
        target_origin_id: str | None = None,
        **extra: Any,
    ) -> Mapping[str, Any]:
        target_ref = str(target.get("resource_ref") or "").strip()
        resolved = str(target_origin_id or "").strip() or _origin_from_ref(target_ref)
        entry = await self.provider.stat(context, origin_id=resolved)
        if entry.origin_id != resolved or "write" not in entry.capabilities:
            raise FilesFacadeError("attachment target is not writable", code="resource_unavailable")
        document = _document_path(entry, resolved)
        roots = self._selected_roots(context.owner_subject_id) if extra.get("selected_root_current") is not None else self._registry_roots(context.owner_subject_id)
        origin = classify_document_origin(
            absolute_document_path=document,
            registered_roots=roots,
            owner_subject_id=context.owner_subject_id,
        )
        root_current = extra.get("selected_root_current")
        if root_current is not None:
            root_provider, root_ref, root_entry = await root_current()
            if root_provider is not self.provider or origin.origin != "loose":
                raise FilesFacadeError("selected root is unavailable", code="invalid_destination")
            root = _document_path(root_entry, root_ref.origin_id)
            if (os.path.realpath(root) != os.path.abspath(root) or not _path_within(root, document)
                    or any(_path_within(record.canonical_root, document) for record in roots)):
                raise FilesFacadeError("selected root does not own this document", code="invalid_destination")
            _assert_workspace_media_path(root, document)
            from src.openclank.media_ownership import DocumentOrigin
            origin = DocumentOrigin("selected_root", root, os.path.relpath(document, root).replace(os.sep, "/"))
        version = 2 if origin.origin in {"workspace", "selected_root"} else 1
        layout = layout_for_absolute(absolute_document_path=document, canonical_root=origin.canonical_root,
            origin=origin.origin, owner_subject_id=context.owner_subject_id, workspace_id=origin.workspace_id,
            layout_version=version)
        if root_current is not None and any(_path_within(record.canonical_root, layout.absolute_media_dir()) or _path_within(layout.absolute_media_dir(), record.canonical_root) for record in roots):
            raise FilesFacadeError("selected media path overlaps a registered workspace", code="invalid_destination")
        runner = getattr(self.provider, "_run_claimed_host_mutation", None)
        if not callable(runner):
            raise FilesFacadeError("Host mutation authority is unavailable", code="provider_unavailable")
        return await runner(context, [document, layout.absolute_media_dir()],
            lambda begin, _no_effect: self._prepare_attachment_claimed(context, source=source, target=target, mode=mode,
                operation_id=operation_id, source_provider=source_provider, source_origin_id=source_origin_id,
                source_entry=source_entry, target_origin_id=target_origin_id, _begin_mutation=begin,
                _claimed_document=document, _claimed_media_dir=layout.absolute_media_dir(), _selected_origin=origin if root_current is not None else None, **extra),
            delayed_start=True)

    async def _prepare_attachment_claimed(
        self,
        context: ProviderContext,
        *,
        source: Mapping[str, Any],
        target: Mapping[str, Any],
        mode: str,
        operation_id: str,
        source_provider: Any | None = None,
        source_origin_id: str | None = None,
        source_entry: ProviderResource | None = None,
        target_origin_id: str | None = None,
        _begin_mutation: Callable[[], None],
        _claimed_document: str,
        _claimed_media_dir: str,
        _selected_origin: Any = None,
        selected_root_current: Any = None,
        **_: Any,
    ) -> Mapping[str, Any]:
        normalized_mode = str(mode or "").strip().lower()
        if normalized_mode not in {"link", "embed"}:
            raise FilesFacadeError("attachment mode is invalid", code="invalid_resource_request")
        if source_provider is None or source_entry is None or not source_origin_id:
            raise FilesFacadeError("attachment source is unavailable", code="resource_unavailable")
        target_ref = str(target.get("resource_ref") or "").strip()
        if not target_ref:
            raise FilesFacadeError("attachment target reference is required", code="invalid_resource_request")

        resolved_target_origin = str(target_origin_id or "").strip() or _origin_from_ref(target_ref)
        target_entry = await self.provider.stat(context, origin_id=resolved_target_origin)
        if "write" not in target_entry.capabilities:
            raise FilesFacadeError("attachment target is not writable", code="resource_unavailable")
        expected_target_revision = target.get("expected_revision")
        if expected_target_revision is not None and dict(target_entry.revision or {}) != dict(expected_target_revision):
            raise FilesFacadeError("attachment target changed during materialization", code="resource_changed")

        source_current = await source_provider.stat(context, origin_id=source_origin_id)
        if source_current.origin_id != source_origin_id:
            raise FilesFacadeError("attachment source is unavailable", code="resource_unavailable")
        expected_source_revision = source.get("expected_revision")
        if _selected_origin is not None and (not expected_source_revision or not source_current.revision):
            raise FilesFacadeError("selected root requires a current source revision", code="resource_changed")
        if expected_source_revision is not None and dict(source_current.revision or {}) != dict(expected_source_revision):
            raise FilesFacadeError("attachment source changed during materialization", code="resource_changed")

        request_digest = _digest_json({
            "account_id": str(context.owner_subject_id),
            "workspace_id": str(context.workspace_id or "default"),
            "operation_id": str(operation_id),
            "generation": int(context.policy_generation),
            "source": ({key:source[key] for key in ('kind','name','mime','sha256','size','expected_revision')} if source.get('kind') == 'client_upload' else {
                key: source.get(key)
                for key in ("resource_ref", "import_receipt_id", "item_id", "expected_revision")
                if source.get(key) is not None
            }),
            "target": {"kind": "host_document", "resource_ref": target_ref, "expected_revision": target.get("expected_revision"), **({"root_ref":target.get("root_ref"), "expected_root_revision":target.get("expected_root_revision")} if target.get("root_ref") is not None else {})},
            "mode": normalized_mode,
        })
        previous = self._load(context, str(operation_id))
        if previous is not None:
            if previous.get("request_digest") != request_digest:
                raise FilesFacadeError("attachment operation conflicts", code="idempotency_conflict")
            if previous.get("phase") == "complete":
                return _public_receipt(previous)
            recovered = await self.operation_status(context, operation_id=str(operation_id))
            if recovered is not None:
                return recovered
            raise FilesFacadeError("attachment operation is pending reconciliation", code="operation_pending")

        data, filename = await _read_source_bytes_async(source_provider, context, source_origin_id)
        source_after = await source_provider.stat(context, origin_id=source_origin_id)
        if source_after.origin_id != source_origin_id or dict(source_after.revision or {}) != dict(source_current.revision or {}):
            raise FilesFacadeError("attachment source changed during materialization", code="resource_changed")

        digest = binary_digest(data)
        digest_hex = digest.split(":", 2)[1]
        document_path = _document_path(target_entry, resolved_target_origin)
        origin = classify_document_origin(
            absolute_document_path=document_path,
            registered_roots=self._registry_roots(context.owner_subject_id),
            owner_subject_id=context.owner_subject_id,
        )
        if _selected_origin is not None:
            root_provider, root_ref, root_entry = await selected_root_current()
            root = _document_path(root_entry, root_ref.origin_id)
            if (root_provider is not self.provider or origin.origin != "loose" or os.path.abspath(root) != os.path.normpath(_selected_origin.canonical_root)
                    or os.path.realpath(root) != os.path.abspath(root) or not _path_within(root, document_path)
                    or any(_path_within(record.canonical_root, document_path) for record in self._selected_roots(context.owner_subject_id))):
                raise FilesFacadeError("selected root or document changed", code="resource_changed")
            _assert_workspace_media_path(root, document_path)
            origin = _selected_origin
        # V2 is defined against an established project/workspace root. Loose
        # documents retain their current containing-folder policy until the
        # caller supplies an authorized destination root.
        layout_version = 2 if origin.origin in {"workspace", "selected_root"} else 1
        layout = layout_for_absolute(
            absolute_document_path=document_path,
            canonical_root=origin.canonical_root,
            origin=origin.origin,
            owner_subject_id=context.owner_subject_id,
            workspace_id=origin.workspace_id,
            layout_version=layout_version,
        )
        media_dir_abs = layout.absolute_media_dir()
        if _selected_origin is not None and any(_path_within(record.canonical_root, media_dir_abs) or _path_within(media_dir_abs, record.canonical_root) for record in self._selected_roots(context.owner_subject_id)):
            raise FilesFacadeError("selected media path overlaps a registered workspace", code="invalid_destination")
        generation = getattr(self.operation_store, "generation", None)
        if (os.path.normpath(document_path) != os.path.normpath(_claimed_document)
                or os.path.normpath(media_dir_abs) != os.path.normpath(_claimed_media_dir)
                or not callable(generation) or int(generation()) != int(context.policy_generation)):
            raise FilesFacadeError("attachment authority changed", code="policy_generation_changed")
        if layout_version == 2:
            _assert_workspace_media_path(origin.canonical_root, media_dir_abs)
        fresh_source = await source_provider.stat(context, origin_id=source_origin_id)
        if fresh_source.origin_id != source_origin_id or dict(fresh_source.revision or {}) != dict(source_current.revision or {}) or not {"read", "download", "open", "copy"}.intersection(fresh_source.capabilities):
            raise FilesFacadeError("attachment source changed", code="resource_changed")
        fresh_target = await self.provider.stat(context, origin_id=resolved_target_origin)
        if fresh_target.origin_id != resolved_target_origin or dict(fresh_target.revision or {}) != dict(target_entry.revision or {}) or "write" not in fresh_target.capabilities:
            raise FilesFacadeError("attachment target changed", code="resource_changed")
        if selected_root_current is not None:
            await selected_root_current()
        if int(generation()) != int(context.policy_generation):
            raise FilesFacadeError("attachment authority changed", code="policy_generation_changed")
        _begin_mutation()
        os.makedirs(media_dir_abs, exist_ok=True)
        if layout_version == 2:
            _assert_workspace_media_path(origin.canonical_root, media_dir_abs)
        existing = [entry for entry in os.listdir(media_dir_abs) if os.path.isfile(os.path.join(media_dir_abs, entry))]
        desired = _download_name(filename or source_current.name)
        asset_name = allocate_media_name(desired, existing)
        asset_path = os.path.join(media_dir_abs, asset_name)
        if layout_version == 2:
            _assert_workspace_media_path(origin.canonical_root, asset_path)

        source_revision = source_current.revision or {"kind": "contentDigest", "value": digest_hex}
        target_revision = target_entry.revision or {"kind": "hostFingerprint", "value": ""}
        relative_link = os.path.relpath(
            asset_path, os.path.dirname(document_path) or origin.canonical_root
        ).replace(os.sep, "/")

        provenance = MediaProvenance(
            canonical_root=origin.canonical_root,
            origin=origin.origin,
            owner_subject_id=str(context.owner_subject_id),
            workspace_id=origin.workspace_id,
            document_id=target_ref,
            document_path=origin.document_path,
            asset_name=asset_name,
            asset_digest=digest,
            references=(target_ref,),
            layout_version=layout.layout_version,
        )
        planned = {
            "operation_id": str(operation_id),
            "account_id": str(context.owner_subject_id),
            "generation": int(context.policy_generation),
            "phase": "pending",
            "workspace_id": str(context.workspace_id or "default"),
            "preparation_receipt_id": f"prep-{digest_hex[:32]}",
            "source_revision": dict(source_revision),
            "target_identity": {"resource_ref": target_ref, "kind": "host_document", **({"root_ref":target["root_ref"], "root_revision":dict(target["expected_root_revision"])} if target.get("root_ref") is not None else {})},
            "target_revision": dict(target_revision),
            "insertion": {
                "format": "markdown",
                "link_target": relative_link,
                "label": desired,
                "media_kind": str(source_entry.mime_type or "application/octet-stream"),
            },
            "asset": {
                "mime_type": str(source_entry.mime_type or "application/octet-stream"),
                "name": asset_name,
                "revision": {"kind": "contentDigest", "value": digest_hex},
            },
            "source_digest": digest,
            "digest_hex": digest_hex,
            "request_digest": request_digest,
            "asset_size": len(data),
            "asset_path": asset_path,
            "layout_version": layout.layout_version,
            "provenance": provenance.as_receipt(),
            "source_identity": ({key:source[key] for key in ('kind','name','mime','sha256','size')} if source.get('kind') == 'client_upload' else {
                "resource_ref": str(source.get("resource_ref") or ""),
                "provider": str(getattr(source_provider, "name", "")),
            }),
            "mode": normalized_mode,
        }
        self._save(context, str(operation_id), request_digest, planned)
        # Asset bytes are durable before the receipt is complete, so a link is
        # never reported ahead of the asset's durability.
        if layout_version == 2:
            _assert_workspace_media_path(origin.canonical_root, asset_path)
        def staged(stage_path: str, stat: os.stat_result) -> None:
            self._save(context, str(operation_id), request_digest, {**planned, "stage_path":stage_path, "stage_device":stat.st_dev, "stage_inode":stat.st_ino})
        stage_path, stage_identity = _publish_attachment(asset_path, data, staged=staged)
        receipt = {
            **planned,
            "phase": "complete",
            "action_receipt": {
                "action_id": f"attachment-{operation_id}",
                "status": "complete",
                "phase": "complete",
                "durable": True,
            },
        }
        try:
            self._save(context, str(operation_id), request_digest, receipt)
        except Exception as exc:
            raise FilesFacadeError('attachment publication requires reconciliation', code='operation_pending') from exc
        try:
            current_stage = os.lstat(stage_path)
            if (current_stage.st_dev, current_stage.st_ino) == stage_identity:
                os.unlink(stage_path)
        except OSError:
            pass
        return _public_receipt(receipt)


async def _read_source_bytes_async(source_provider: Any, context: ProviderContext, source_origin_id: str) -> tuple[bytes, str | None]:
    try:
        content = await source_provider.content(context, origin_id=source_origin_id)
    except FilesFacadeError:
        raise
    except Exception as exc:
        raise FilesFacadeError("attachment source content is unavailable", code="provider_unavailable") from exc
    declared = None
    try:
        declared = None if content.size is None else int(content.size)
    except (TypeError, ValueError) as exc:
        raise FilesFacadeError("attachment source content is invalid", code="provider_unavailable") from exc
    if declared is not None and (declared < 0 or declared > _LIMIT):
        raise FilesFacadeError("attachment exceeds the configured limit", code="upload_too_large")
    if content.data is not None:
        data = bytes(content.data)
    elif content.path is not None:
        def read_bounded(path: str) -> bytes:
            chunks: list[bytes] = []
            total = 0
            with Path(path).open("rb") as handle:
                while total <= _LIMIT:
                    chunk = handle.read(min(512 * 1024, _LIMIT - total + 1))
                    if not chunk:
                        break
                    chunks.append(chunk)
                    total += len(chunk)
                    if total > _LIMIT:
                        break
            return b"".join(chunks)

        data = await asyncio.to_thread(read_bounded, str(content.path))
    elif content.stream is not None:
        chunks: list[bytes] = []
        total = 0
        try:
            async for chunk in content.stream(0, _LIMIT + 1):
                if not isinstance(chunk, (bytes, bytearray, memoryview)):
                    raise FilesFacadeError("attachment source content is invalid", code="provider_unavailable")
                piece = bytes(chunk)
                remaining = _LIMIT - total + 1
                chunks.append(piece[:remaining])
                total += min(len(piece), remaining)
                if total > _LIMIT:
                    break
        except FilesFacadeError:
            raise
        except Exception as exc:
            raise FilesFacadeError("attachment source content is unavailable", code="provider_unavailable") from exc
        data = b"".join(chunks)
    else:
        raise FilesFacadeError("attachment source content is unavailable", code="resource_unavailable")
    if len(data) > _LIMIT:
        raise FilesFacadeError("attachment exceeds the configured limit", code="upload_too_large")
    if declared is not None and len(data) != declared:
        raise FilesFacadeError("attachment source changed during materialization", code="resource_changed")
    return data, str(content.filename or "") or None


def _write_atomic(path: str, data: bytes) -> None:
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    temp = f"{path}.tmp-{os.getpid()}"
    with open(temp, "wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp, path)


def _publish_attachment(path: str, data: bytes, *, staged: Callable[[str, os.stat_result], None]) -> tuple[str, tuple[int, int]]:
    directory = os.path.dirname(path) or "."
    if not hasattr(os, 'O_DIRECTORY'):
        raise FilesFacadeError('exclusive publication is unavailable', code='provider_unavailable')
    fd, temp = tempfile.mkstemp(prefix='.openclank-attachment-', dir=directory)
    entered = False
    try:
        with os.fdopen(fd, 'wb') as handle:
            fd = -1
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
            info = os.fstat(handle.fileno())
        staged(temp, info)
        try:
            os.link(temp, path, follow_symlinks=False)
        except FileExistsError as exc:
            raise FilesFacadeError('attachment destination already exists', code='destination_conflict') from exc
        entered = True
        directory_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except FilesFacadeError:
        raise
    except Exception as exc:
        if entered:
            raise FilesFacadeError('attachment publication requires reconciliation', code='operation_pending') from exc
        raise FilesFacadeError('exclusive publication is unavailable', code='provider_unavailable') from exc
    finally:
        if fd >= 0:
            os.close(fd)
        if not entered:
            try:
                os.unlink(temp)
            except FileNotFoundError:
                pass
    # Keep the owned stage through the terminal receipt; it is not an asset.
    return temp, (info.st_dev, info.st_ino)


_INTERNAL_RECEIPT_KEYS = frozenset({"asset_path", "digest_hex", "phase", "stage_path", "stage_device", "stage_inode"})


def _public_receipt(value: Mapping[str, Any]) -> dict[str, Any]:
    """Drop storage-internal fields from a typed preparation DTO.

    ``asset_path`` is a local filesystem path used only for crash recovery; it
    never belongs in the browser-facing receipt.  ``phase``/``digest_hex`` are
    likewise recovery bookkeeping.
    """
    return {key: item for key, item in value.items() if key not in _INTERNAL_RECEIPT_KEYS}


def _document_path(entry: Any, target_origin_id: str | None = None) -> str:
    """Absolute filesystem path of a Host resource entry.

    Decode the provider's opaque Host origin; legacy absolute Host origins
    remain readable. The caller must already have authorized the resource.
    """
    for candidate in (target_origin_id, getattr(entry, "origin_id", None)):
        if isinstance(candidate, str) and candidate.startswith("host:v1:"):
            from src.openclank.files_host_provider import _path
            return os.path.normpath(_path(candidate))
        if isinstance(candidate, str) and candidate.startswith("host:"):
            path = candidate[len("host:") :]
            if path and os.path.isabs(path) and os.path.basename(path):
                return os.path.normpath(path)
    for attribute in ("path", "absolute_path"):
        value = getattr(entry, attribute, None)
        if isinstance(value, str) and value and os.path.isabs(value) and os.path.basename(value):
            return os.path.normpath(value)
    raise FilesFacadeError("attachment target path is unavailable", code="resource_unavailable")


def _origin_from_ref(target_ref: str) -> str:
    """Accept either an opaque origin id or a sealed resource ref token.

    The facade resolves sealed refs before dispatch; adapters that are handed a
    bare origin id can use it directly.
    """
    token = str(target_ref or "").strip()
    if token.startswith("host:"):
        return token
    return token


def _download_name(name: str) -> str:
    value = str(name or "attachment").replace("\\", "/").split("/")[-1].strip()
    cleaned = "".join(ch for ch in value if ch.isalnum() or ch in "._- ()").strip() or "attachment"
    return cleaned[:180]


# ---------------------------------------------------------------------------
# Workspace-creation orphan adoption
# ---------------------------------------------------------------------------


def collect_media_provenance(operation_store: Any, *, owner_subject_id: str) -> list[MediaProvenance]:
    """Load this owner's persisted media provenance rows from the operation store."""
    lister = getattr(operation_store, "list_operations", None)
    if not callable(lister):
        return []
    try:
        rows = lister(owner_subject_id=str(owner_subject_id), operation_prefix="__host_media__", limit=256)
    except Exception:
        return []
    records: list[MediaProvenance] = []
    for row in rows:
        receipt = row.get("receipt") if isinstance(row, Mapping) else None
        if not isinstance(receipt, Mapping):
            continue
        provenance = receipt.get("provenance")
        if not isinstance(provenance, Mapping):
            continue
        try:
            records.append(MediaProvenance.from_receipt(provenance))
        except MediaOwnershipError:
            continue
    # Workspace provenance supersedes the loose record for the same asset. Keep
    # one active identity so a later workspace creation cannot re-adopt bytes
    # that have already moved into an owned workspace.
    workspace_keys = {
        (item.asset_id or item.asset_digest, item.asset_name, item.document_id)
        for item in records
        if item.origin == "workspace"
    }
    return [
        item for item in records
        if item.origin == "workspace"
        or (item.asset_id or item.asset_digest, item.asset_name, item.document_id) not in workspace_keys
    ]


def _scan_workspace_references(root: str) -> list[tuple[str, str, bool]]:
    """Collect Markdown/wiki reference sources under a workspace root.

    Only text documents are read.  A document that cannot be decoded is skipped
    rather than guessed at, and the scan never leaves the workspace root.
    """
    found: list[tuple[str, str, bool]] = []
    root_norm = os.path.normpath(str(root))
    for directory, dirnames, filenames in os.walk(root_norm):
        dirnames[:] = [name for name in dirnames if not name.startswith(".")]
        for filename in filenames:
            if not filename.lower().endswith((".md", ".markdown", ".rs", ".txt", ".py", ".js", ".ts", ".c", ".cpp", ".h", ".cs", ".go", ".java")):
                continue
            path = os.path.join(directory, filename)
            try:
                with open(path, "r", encoding="utf-8") as handle:
                    text = handle.read(2 * 1024 * 1024)
            except (OSError, UnicodeDecodeError):
                continue
            relative = os.path.relpath(path, root_norm).replace(os.sep, "/")
            found.append((relative, text, False))
    return found


def _adopt_loose_media_for_workspace(
    *,
    operation_store: Any,
    owner_subject_id: str,
    workspace_root: str,
    workspace_id: str,
    operation_id: str | None = None,
    document_sources: Any | None = None,
    lore_capture: Any | None = None,
    history_context: Any | None = None,
    phase_hook: Any | None = None,
    post_terminal_hook: Any | None = None,
    lease_ms: int = 30_000,
) -> dict[str, Any]:
    """Adopt provenance-backed loose assets into a newly created workspace.

    Only loose assets whose canonical physical root is not owned by another
    registered workspace (nested, other-owner and archived rows all count) are
    moved.  Each adoptable asset is relocated into the workspace's
    ``media/<stem>/<name>`` mirror and its known references are repaired
    surgically.  A protected reference leaves that asset's move unapplied and is
    reported as a concrete conflict.  Preimages are captured through ``lore_capture``
    when supplied so the consolidation is recoverable through Lore.
    """
    root = os.path.normpath(str(workspace_root or "").strip())
    if not root:
        raise FilesFacadeError("workspace root is required", code="invalid_media_request")

    op_id = str(operation_id or f"adopt-{workspace_id}")
    lease_owner = f"{os.getpid()}-{threading.get_ident()}-{op_id}"
    existing_getter = getattr(operation_store, "get_operation", None)
    if callable(existing_getter):
        existing = existing_getter(owner_subject_id=str(owner_subject_id), operation_id=f"__host_media_adopt__{op_id}")
        existing_receipt = existing.get("receipt") if isinstance(existing, Mapping) else None
        if isinstance(existing_receipt, Mapping):
            # Validate the operation identity before honoring any terminal or
            # recovery replay.  A reused operation id must never become an
            # oracle for a different workspace request.
            recorded_root = existing_receipt.get("workspace_root")
            recorded_workspace = existing_receipt.get("workspace_id")
            recorded_operation = existing_receipt.get("operation_id")
            if (
                (recorded_root and os.path.realpath(str(recorded_root)) != os.path.realpath(root))
                or (recorded_workspace and str(recorded_workspace) != str(workspace_id))
                or (recorded_operation and str(recorded_operation) != op_id)
            ):
                raise FilesFacadeError("adoption operation conflicts", code="idempotency_conflict")
            # Claim before inspecting a recovery phase.  This is the durable
            # fence that prevents a live foreign claimant from touching the
            # filesystem, and lets only an expired lease enter recovery.
            claimer = getattr(operation_store, "claim_operation", None)
            if callable(claimer):
                claimed = claimer(
                    owner_subject_id=str(owner_subject_id),
                    operation_id=f"__host_media_adopt__{op_id}",
                    request_digest=str(existing.get("digest") or ""),
                    generation=0,
                    receipt=dict(existing_receipt),
                    lease_owner=lease_owner,
                    lease_ms=lease_ms,
                )
                if claimed is not None:
                    previous = claimed.get("receipt") if isinstance(claimed, Mapping) else {}
                    if isinstance(previous, Mapping) and str(previous.get("phase") or "") in {"complete", "recovery_required"} and isinstance(previous.get("resource_receipt"), Mapping):
                        resource = dict(previous["resource_receipt"])
                        return {"status": str(resource.get("status") or "conflict"), "adopted": list(previous.get("adopted") or []), "conflicts": list(previous.get("conflicts") or []), "rejected": list(previous.get("rejected") or []), "resource_receipt": resource}
                    raise FilesFacadeError("adoption operation is pending reconciliation", code="operation_pending")
            if existing_receipt.get("phase") == "staged":
                # A claimant must observe the durable lease before candidate
                # scanning can turn the same operation into a false collision.
                raise FilesFacadeError("adoption operation is pending reconciliation", code="operation_pending")
            if existing_receipt.get("phase") == "source_removal_pending":
                stored_resource = dict(existing_receipt.get("resource_receipt") or {})
                stored_adopted = list(existing_receipt.get("adopted") or [])
                stored_conflicts = list(existing_receipt.get("conflicts") or [])
                stored_rejected = list(existing_receipt.get("rejected") or [])
                if bool(existing_receipt.get("history_expected")) and existing_receipt.get("history_state") not in ("complete", "recovery_settled"):
                    history_conflict = {"code": "history_capture_failed", "reason": "history completion could not be resumed"}
                    recovery_conflicts = stored_conflicts if history_conflict in stored_conflicts else stored_conflicts + [history_conflict]
                    recovery = {**stored_resource, "status": "conflict", "phase": "recovery_required", "durable": False, "recovery": "history_capture_failed"}
                    recorder = getattr(operation_store, "record_operation", None)
                    if callable(recorder):
                        recorder(owner_subject_id=str(owner_subject_id), operation_id=f"__host_media_adopt__{op_id}", request_digest=str(existing.get("digest") or ""), generation=0, receipt={**dict(existing_receipt), "phase": "recovery_required", "lease_owner": lease_owner, "adopted": stored_adopted, "conflicts": recovery_conflicts, "rejected": stored_rejected, "resource_receipt": recovery}, phase="recovery_required")
                    return {"status": "conflict", "adopted": stored_adopted, "conflicts": recovery_conflicts, "rejected": stored_rejected, "resource_receipt": recovery}
                destinations = list(existing_receipt.get("destinations") or [])
                valid = True
                for item in destinations:
                    destination = _safe_receipt_path(root, item.get("destination"))
                    if destination is None:
                        valid = False
                        break
                    with open(destination, "rb") as handle:
                        if binary_digest(handle.read()) != str(item.get("digest") or ""):
                            valid = False
                            break
                for item in list(existing_receipt.get("documents") or []):
                    path = _safe_receipt_path(root, item.get("document_id"))
                    if path is None:
                        valid = False
                        break
                    with open(path, "r", encoding="utf-8") as handle:
                        if handle.read() != str(item.get("after") or ""):
                            valid = False
                            break
                if valid:
                    for item in destinations:
                        source = _safe_receipt_path(root, item.get("source"), must_exist=False)
                        if source is None:
                            valid = False
                            break
                        if os.path.exists(source):
                            try:
                                os.remove(source)
                            except OSError:
                                valid = False
                                break
                if not valid:
                    raise FilesFacadeError("adoption receipt requires reconciliation", code="resource_changed")
                _ensure_workspace_provenance(operation_store, owner_subject_id=str(owner_subject_id), operation_id=op_id, request_digest=str(existing.get("digest") or ""), records=list(existing_receipt.get("provenance_records") or []))
                terminal = {**stored_resource, "action_id": stored_resource.get("action_id") or f"adopt-{op_id}", "status": "complete", "phase": "complete", "durable": True, "operation_id": op_id, "workspace_id": str(workspace_id), "adopted_count": len(stored_adopted) or len(destinations), "conflict_count": len(stored_conflicts)}
                recorder = getattr(operation_store, "record_operation", None)
                if callable(recorder):
                    recorder(owner_subject_id=str(owner_subject_id), operation_id=f"__host_media_adopt__{op_id}", request_digest=str(existing.get("digest") or ""), generation=0, receipt={**dict(existing_receipt), "phase": "complete", "lease_owner": lease_owner, "resource_receipt": terminal}, phase="complete")
                return {"status": "complete", "adopted": stored_adopted, "conflicts": stored_conflicts, "rejected": stored_rejected, "resource_receipt": terminal}
            if existing_receipt.get("phase") in {"destination_published", "documents_rewritten"}:
                receipt_destination = _safe_receipt_path(root, existing_receipt.get("destination"), must_exist=False)
                receipt_source = _safe_receipt_path(root, existing_receipt.get("source"), must_exist=False)
                if receipt_destination is None or receipt_source is None:
                    raise FilesFacadeError("adoption receipt is corrupt", code="idempotency_conflict")
            multi_destinations = list(existing_receipt.get("destinations") or [])
            planned_assets = list(existing_receipt.get("assets") or [])
            known_destinations = {str(item.get("destination") or "") for item in multi_destinations}
            for item in planned_assets:
                if str(item.get("destination") or "") not in known_destinations:
                    multi_destinations.append(item)
            if existing_receipt.get("phase") in {"destination_published", "documents_rewritten"} and len(multi_destinations) >= 1:
                valid = True
                for item in multi_destinations:
                    destination = _safe_receipt_path(root, item.get("destination"), must_exist=False)
                    source = _safe_receipt_path(root, item.get("source"), must_exist=False)
                    if destination is None or source is None:
                        valid = False
                        break
                    if os.path.isfile(destination):
                        with open(destination, "rb") as handle:
                            if binary_digest(handle.read()) != str(item.get("digest") or ""):
                                valid = False
                                break
                    else:
                        if not os.path.isfile(source):
                            valid = False
                            break
                        with open(source, "rb") as handle:
                            data = handle.read(_LIMIT + 1)
                        if binary_digest(data) != str(item.get("digest") or ""):
                            valid = False
                            break
                        os.makedirs(os.path.dirname(destination), exist_ok=True)
                        _write_atomic(destination, data)
                documents = list(existing_receipt.get("documents") or [])
                if valid:
                    for item in documents:
                        path = _safe_receipt_path(root, item.get("document_id"))
                        if path is None:
                            valid = False
                            break
                        with open(path, "r", encoding="utf-8") as handle:
                            current = handle.read()
                            if current == str(item.get("after") or ""):
                                continue
                            if current != str(item.get("before") or ""):
                                valid = False
                                break
                            _write_atomic_text(path, str(item.get("after") or ""))
                sources = [_safe_receipt_path(root, item.get("source"), must_exist=False) for item in multi_destinations]
                if valid:
                    for source in sources:
                        if source is not None and os.path.exists(source):
                            try:
                                os.remove(source)
                            except OSError:
                                valid = False
                                break
                if valid:
                    stored_resource = dict(existing_receipt.get("resource_receipt") or {})
                    terminal = {**stored_resource, "action_id": stored_resource.get("action_id") or f"adopt-{op_id}", "status": "complete", "phase": "complete", "durable": True, "operation_id": op_id, "workspace_id": str(workspace_id), "adopted_count": len(existing_receipt.get("adopted") or multi_destinations), "conflict_count": len(existing_receipt.get("conflicts") or [])}
                    recorder = getattr(operation_store, "record_operation", None)
                    if callable(recorder):
                        _ensure_workspace_provenance(operation_store, owner_subject_id=str(owner_subject_id), operation_id=op_id, request_digest=str(existing.get("digest") or ""), records=list(existing_receipt.get("provenance_records") or []))
                        recorder(owner_subject_id=str(owner_subject_id), operation_id=f"__host_media_adopt__{op_id}", request_digest=str(existing.get("digest") or ""), generation=0, receipt={**dict(existing_receipt), "phase": "complete", "lease_owner": lease_owner, "resource_receipt": terminal}, phase="complete")
                    return {"status": "complete", "adopted": list(existing_receipt.get("adopted") or []), "conflicts": list(existing_receipt.get("conflicts") or []), "rejected": list(existing_receipt.get("rejected") or []), "resource_receipt": terminal}
                raise FilesFacadeError("adoption receipt requires reconciliation", code="resource_changed")
        if isinstance(existing_receipt, Mapping) and existing_receipt.get("phase") == "destination_published":
            destination = _safe_receipt_path(root, existing_receipt.get("destination"))
            source = _safe_receipt_path(root, existing_receipt.get("source"))
            valid = destination is not None
            if valid:
                with open(destination, "rb") as handle:
                    valid = binary_digest(handle.read()) == str(existing_receipt.get("digest") or "")
            documents = list(existing_receipt.get("documents") or [])
            if valid and documents:
                for item in documents:
                    path = _safe_receipt_path(root, item.get("document_id"))
                    if path is None:
                        valid = False
                        break
                    with open(path, "r", encoding="utf-8") as handle:
                        current = handle.read()
                    if current == str(item.get("after") or ""):
                        continue
                    if current != str(item.get("before") or ""):
                        valid = False
                        break
                    _write_atomic_text(path, str(item.get("after") or ""))
            if valid and documents:
                recorder = getattr(operation_store, "record_operation", None)
                if callable(recorder):
                    recorder(owner_subject_id=str(owner_subject_id), operation_id=f"__host_media_adopt__{op_id}", request_digest=str(existing.get("digest") or ""), generation=0, receipt={**dict(existing_receipt), "phase": "documents_rewritten", "documents": documents}, phase="documents_rewritten")
                try:
                    if source is not None and os.path.exists(source):
                        os.remove(source)
                    terminal = {"action_id": f"adopt-{op_id}", "status": "complete", "phase": "complete", "durable": True, "operation_id": op_id, "workspace_id": str(workspace_id), "adopted_count": 1, "conflict_count": 0}
                    if callable(recorder):
                        recorder(owner_subject_id=str(owner_subject_id), operation_id=f"__host_media_adopt__{op_id}", request_digest=str(existing.get("digest") or ""), generation=0, receipt={"operation_id": op_id, "phase": "complete", "lease_owner": lease_owner, "resource_receipt": terminal}, phase="complete")
                    return {"status": "complete", "adopted": [], "conflicts": [], "rejected": [], "resource_receipt": terminal}
                except OSError:
                    return {"status": "conflict", "adopted": [], "conflicts": [{"code": "retained_duplicate", "reason": "source removal failed"}], "rejected": [], "resource_receipt": {"action_id": f"adopt-{op_id}", "status": "conflict", "phase": "recovery_required", "recovery": "retained_duplicate"}}
        if isinstance(existing_receipt, Mapping) and existing_receipt.get("phase") == "documents_rewritten":
            destination = _safe_receipt_path(root, existing_receipt.get("destination"))
            source = _safe_receipt_path(root, existing_receipt.get("source"), must_exist=False)
            manifest = existing_receipt.get("documents") or []
            valid = destination is not None
            if valid:
                with open(destination, "rb") as handle:
                    valid = binary_digest(handle.read()) == str(existing_receipt.get("digest") or "")
            for item in manifest:
                path = _safe_receipt_path(root, item.get("document_id"))
                if path is None:
                    valid = False
                    break
                with open(path, "r", encoding="utf-8") as handle:
                    current_text = handle.read()
                current_fingerprint = text_fingerprint(current_text)
                expected_after = str(item.get("after_digest") or item.get("after") or "")
                if current_text != str(item.get("after") or "") and current_fingerprint != expected_after:
                    valid = False
                    break
            if valid and source is not None and os.path.isfile(source):
                try:
                    os.remove(source)
                except OSError:
                    valid = False
            if valid:
                replay_receipt = {"action_id": f"adopt-{op_id}", "status": "complete", "phase": "complete", "durable": True, "operation_id": op_id, "workspace_id": str(workspace_id), "adopted_count": 1, "conflict_count": 0}
                recorder = getattr(operation_store, "record_operation", None)
                if callable(recorder):
                    recorder(owner_subject_id=str(owner_subject_id), operation_id=f"__host_media_adopt__{op_id}", request_digest=str(existing.get("digest") or ""), generation=0, receipt={"operation_id": op_id, "phase": "complete", "lease_owner": lease_owner, "resource_receipt": replay_receipt}, phase="complete")
                return {"status": "complete", "adopted": [], "conflicts": [], "rejected": [], "resource_receipt": replay_receipt}
        if isinstance(existing_receipt, Mapping) and isinstance(existing_receipt.get("resource_receipt"), Mapping) and existing_receipt["resource_receipt"].get("status") in {"complete", "conflict"}:
            _ensure_workspace_provenance(operation_store, owner_subject_id=str(owner_subject_id), operation_id=op_id, request_digest=str(existing.get("digest") or ""), records=list(existing_receipt.get("provenance_records") or []))
            return {
                "status": str(existing_receipt["resource_receipt"].get("status")),
                "adopted": list(existing_receipt.get("adopted") or []),
                "conflicts": list(existing_receipt.get("conflicts") or []),
                "rejected": list(existing_receipt.get("rejected") or []),
                "resource_receipt": dict(existing_receipt["resource_receipt"]),
            }
    candidates = collect_media_provenance(operation_store, owner_subject_id=owner_subject_id)
    if not candidates:
        return {"status": "noop", "adopted": [], "conflicts": [], "rejected": []}

    registered = _all_registered_roots(operation_store)
    plan = classify_adoption_candidates(
        candidates=candidates,
        registered_roots=registered,
        new_workspace_root=root,
        new_workspace_id=str(workspace_id),
        new_owner_subject_id=str(owner_subject_id),
    )
    if not plan.adoptable:
        return {
            "status": "noop",
            "adopted": [],
            "conflicts": [],
            "rejected": list(plan.rejected),
        }

    if callable(document_sources):
        sources = list(document_sources())
    else:
        sources = _scan_workspace_references(root)
    # Registered nested, archived, or other-owner roots remain independent
    # authorities.  Do not scan their documents through the new workspace,
    # including aliases that resolve through symlinks.
    authorized_sources: list[tuple[str, str, bool]] = []
    for doc_id, text, protected in sources:
        document_abs = os.path.realpath(os.path.join(root, str(doc_id).replace("/", os.sep)))
        if not _path_within(root, document_abs):
            continue
        nested_owner = any(
            os.path.realpath(record.canonical_root) != os.path.realpath(root)
            and _path_within(record.canonical_root, document_abs)
            for record in registered
        )
        if not nested_owner:
            authorized_sources.append((str(doc_id), str(text), bool(protected)))
    sources = authorized_sources

    adopted: list[dict[str, Any]] = []
    sources_to_remove: list[str] = []
    published_for_rollback: list[tuple[str, dict[str, str]]] = []
    history_handles: list[tuple[Any, str]] = []
    published_manifest: list[dict[str, str]] = []
    conflicts: list[dict[str, str]] = []
    preimages: dict[str, str] = {}
    # Reference rewrites are collected first so a protected reference blocks
    # that asset's move before any byte is relocated.
    pending: list[tuple[Any, list[Any], dict[str, str]]] = []

    for candidate in plan.adoptable:
        record = candidate.provenance
        old_rel = os.path.relpath(candidate.source_path, record.canonical_root).replace(os.sep, "/")
        new_rel = candidate.destination_rel
        edits: list[Any] = []
        local_conflicts: list[dict[str, str]] = []
        for doc_id, text, protected in sources:
            for site in scan_references(str(doc_id), str(text), protected=bool(protected)):
                normalized = site.target.replace("\\", "/")
                if not normalized or re.match(r"^[a-z][a-z\d+.-]*:", normalized, re.IGNORECASE):
                    continue
                matched = False
                # Resolve each reference from its own document.  Equal names
                # in sibling loose folders are unrelated assets.
                document_abs = os.path.realpath(os.path.join(root, str(doc_id).replace("/", os.sep)))
                if not _path_within(root, document_abs) or not os.path.isfile(document_abs):
                    continue
                resolved = os.path.realpath(os.path.join(os.path.dirname(document_abs), normalized))
                matched = resolved == os.path.realpath(candidate.source_path)
                if not matched:
                    continue
                if site.protected:
                    local_conflicts.append({
                        "code": "protected_reference",
                        "document_id": str(doc_id),
                        "reason": "protected reference cannot be updated; asset left unapplied",
                        "reference_target": site.target,
                        "asset_name": record.asset_name,
                    })
                    continue
                edits.append((str(doc_id), site, _relative_reference(str(doc_id), new_rel), text))
        if local_conflicts:
            conflicts.extend(local_conflicts)
            continue
        pending.append((candidate, edits, preimages))

    # Every destination and source must be safe before the first byte or
    # document write.  The staged receipt is the durable preflight boundary.
    # Existing unrelated bytes are never replaced; allocation is handled by
    # the plan only when the exact destination is free.
    if callable(existing_getter):
        raced = existing_getter(owner_subject_id=str(owner_subject_id), operation_id=f"__host_media_adopt__{op_id}")
        raced_receipt = raced.get("receipt") if isinstance(raced, Mapping) else None
        if isinstance(raced_receipt, Mapping):
            raced_phase = str(raced_receipt.get("phase") or "")
            if raced_phase in {"complete", "recovery_required"} and isinstance(raced_receipt.get("resource_receipt"), Mapping):
                resource = dict(raced_receipt["resource_receipt"])
                return {"status": str(resource.get("status") or "conflict"), "adopted": list(raced_receipt.get("adopted") or []), "conflicts": list(raced_receipt.get("conflicts") or []), "rejected": list(raced_receipt.get("rejected") or []), "resource_receipt": resource}
            if raced_phase in {"staged", "destination_published", "documents_rewritten", "source_removal_pending"} and str(raced_receipt.get("lease_owner") or "") != f"{os.getpid()}-{threading.get_ident()}-{op_id}":
                raise FilesFacadeError("adoption operation is pending reconciliation", code="operation_pending")
    for candidate, _edits, _preimages in pending:
        destination_abs = os.path.join(root, candidate.destination_rel.replace("/", os.sep))
        if os.path.lexists(destination_abs):
            conflicts.append({
                "code": "destination_exists",
                "document_id": candidate.provenance.document_id,
                "reason": "adoption destination already exists",
                "asset_name": candidate.provenance.asset_name,
            })
    if conflicts:
        if operation_id is not None and any(item.get("code") == "destination_exists" for item in conflicts):
            # An explicit operation racing a publisher can observe the
            # destination before SQLite exposes the publisher's staged row.
            # Return the same typed pending result rather than converting the
            # rival's publication into a terminal collision receipt.
            raise FilesFacadeError("adoption operation is pending reconciliation", code="operation_pending")
        pending = []

    staged_preimages: dict[str, str] = {}
    staged_assets: list[dict[str, str]] = []
    staged_documents: list[dict[str, str]] = []
    for candidate, edits, _preimages in pending:
        if not os.path.isfile(candidate.source_path):
            conflicts.append({
                "code": "missing_asset",
                "document_id": candidate.provenance.document_id,
                "reason": "loose asset disappeared before adoption",
                "asset_name": candidate.provenance.asset_name,
            })
            continue
        with open(candidate.source_path, "rb") as handle:
            staged_data = handle.read(_LIMIT + 1)
        if len(staged_data) > _LIMIT or not candidate.provenance.digest_matches(staged_data):
            conflicts.append({
                "code": "digest_mismatch",
                "document_id": candidate.provenance.document_id,
                "reason": "loose asset bytes changed since provenance was recorded",
                "asset_name": candidate.provenance.asset_name,
            })
            continue
        staged_assets.append({
            "document_id": str(candidate.provenance.document_id),
            "asset_name": str(candidate.provenance.asset_name),
            "digest": str(candidate.provenance.asset_digest),
            "destination": str(candidate.destination_rel),
            "source": str(candidate.source_path),
        })
        for doc_id, _site, _new_rel, text in edits:
            staged_preimages.setdefault(doc_id, text)
        for doc_id in sorted({item[0] for item in edits}):
            doc_edits = [item for item in edits if item[0] == doc_id]
            after = next(item[3] for item in doc_edits)
            for _doc, site, new_rel, _text in sorted(doc_edits, key=lambda item: item[1].start, reverse=True):
                after = after[:site.start] + rewrite_reference(site, new_rel) + after[site.end:]
            staged_documents.append({
                "document_id": doc_id,
                "before": next(item[3] for item in doc_edits),
                "before_digest": text_fingerprint(next(item[3] for item in doc_edits)),
                "after": after,
                "after_digest": text_fingerprint(after),
            })
    if conflicts:
        pending = []
    if pending:
        recorder = getattr(operation_store, "record_operation", None)
        reserver = getattr(operation_store, "reserve_operation", None)
        request_digest = _digest_json({
            "owner_subject_id": str(owner_subject_id),
            "workspace_root": os.path.realpath(root),
            "workspace_id": str(workspace_id),
            "operation_id": op_id,
            "assets": sorted(staged_assets, key=lambda item: (item["document_id"], item["asset_name"])),
            "documents": sorted(
                {key: text_fingerprint(value) for key, value in staged_preimages.items()}.items(),
            ),
        })
        staged_receipt = {
            "operation_id": op_id,
            "workspace_id": str(workspace_id),
            "workspace_root": os.path.realpath(root),
            "phase": "staged",
            "preimages": staged_preimages,
            "assets": staged_assets,
            "documents": staged_documents,
            "lease_owner": f"{os.getpid()}-{threading.get_ident()}-{op_id}",
        }
        claimer = getattr(operation_store, "claim_operation", None)
        lease_owner = str(staged_receipt["lease_owner"])
        if callable(claimer):
            existing = claimer(
                owner_subject_id=str(owner_subject_id),
                operation_id=f"__host_media_adopt__{op_id}",
                request_digest=request_digest,
                generation=0,
                receipt=staged_receipt,
                lease_owner=lease_owner,
                lease_ms=lease_ms,
            )
            if existing is not None:
                previous = existing.get("receipt") if isinstance(existing, Mapping) else {}
                if isinstance(previous, Mapping) and previous.get("phase") in {"complete", "recovery_required"}:
                    return {
                        "status": str((previous.get("resource_receipt") or {}).get("status") or "conflict"),
                        "adopted": list(previous.get("adopted") or []),
                        "conflicts": list(previous.get("conflicts") or []),
                        "rejected": list(previous.get("rejected") or []),
                        "resource_receipt": dict(previous.get("resource_receipt") or {}),
                    }
                raise FilesFacadeError("adoption operation is pending reconciliation", code="operation_pending")
        elif callable(reserver):
            existing = reserver(
                owner_subject_id=str(owner_subject_id),
                operation_id=f"__host_media_adopt__{op_id}",
                request_digest=request_digest,
                generation=0,
                receipt=staged_receipt,
                phase="staged",
            )
            if existing is not None:
                previous = existing.get("receipt") if isinstance(existing, Mapping) else {}
                if isinstance(previous, Mapping) and previous.get("phase") == "complete":
                    return {
                        "status": "complete",
                        "adopted": list(previous.get("adopted") or []),
                        "conflicts": list(previous.get("conflicts") or []),
                        "rejected": list(previous.get("rejected") or []),
                        "resource_receipt": dict(previous.get("resource_receipt") or {}),
                    }
                raise FilesFacadeError("adoption operation is pending reconciliation", code="operation_pending")
        elif not callable(recorder):
            raise MediaOwnershipError("operation storage is unavailable", code="provider_unavailable")
        else:
            recorder(
                owner_subject_id=str(owner_subject_id),
                operation_id=f"__host_media_adopt__{op_id}",
                request_digest=request_digest,
                generation=0,
                receipt=staged_receipt,
                phase="staged",
            )

    for candidate, edits, preimages in pending:
        record = candidate.provenance
        # Capture preimages through Lore before mutating anything.
        by_document: dict[str, str] = {}
        for doc_id, site, _new_rel, text in edits:
            by_document.setdefault(doc_id, text)
        if by_document and callable(lore_capture):
            for doc_id, text in by_document.items():
                try:
                    lore_capture(document_id=doc_id, preimage=text, operation_id=op_id)
                except Exception:
                    conflicts.append({
                        "code": "lore_capture_failed",
                        "document_id": doc_id,
                        "reason": "preimage capture failed; asset left unapplied",
                        "asset_name": record.asset_name,
                    })
            if any(item.get("code") == "lore_capture_failed" for item in conflicts):
                continue
        if by_document and history_context is not None:
            for doc_id in by_document:
                document_abs = os.path.realpath(os.path.join(root, str(doc_id).replace("/", os.sep)))
                handle = begin_file_capture(
                    document_abs,
                    operation="workspace-media-adoption",
                    required_recovery=True,
                    context=history_context,
                    action_id=f"adopt-{op_id}-{hashlib.sha256(document_abs.encode()).hexdigest()[:16]}",
                )
                if getattr(handle, "status", "") != "prepared":
                    raise MediaOwnershipError("history preimage capture failed", code="lore_capture_failed")
                history_handles.append((handle, document_abs))
        preimages.update(by_document)

        # Stage: verify the loose asset bytes before touching the destination.
        destination_abs = os.path.join(root, candidate.destination_rel.replace("/", os.sep))
        if not os.path.isfile(candidate.source_path):
            conflicts.append({
                "code": "missing_asset",
                "document_id": record.document_id,
                "reason": "loose asset disappeared before adoption",
                "asset_name": record.asset_name,
            })
            continue
        with open(candidate.source_path, "rb") as handle:
            data = handle.read(_LIMIT + 1)
        if len(data) > _LIMIT or not record.digest_matches(data):
            conflicts.append({
                "code": "digest_mismatch",
                "document_id": record.document_id,
                "reason": "loose asset bytes changed since provenance was recorded",
                "asset_name": record.asset_name,
            })
            continue
        original_documents: dict[str, str] = {}
        document_manifest: list[dict[str, str]] = []
        try:
            if os.path.lexists(destination_abs):
                raise FilesFacadeError("adoption operation is pending reconciliation", code="operation_pending")
            os.makedirs(os.path.dirname(destination_abs), exist_ok=True)
            _write_atomic(destination_abs, data)
            published_manifest.append({
                "destination": candidate.destination_rel,
                "source": candidate.source_path,
                "digest": record.asset_digest,
            })
            if callable(recorder) and "request_digest" in locals():
                recorder(
                    owner_subject_id=str(owner_subject_id),
                    operation_id=f"__host_media_adopt__{op_id}",
                    request_digest=request_digest,
                    generation=0,
                    receipt={"operation_id": op_id, "workspace_root": os.path.realpath(root), "lease_owner": f"{os.getpid()}-{threading.get_ident()}-{op_id}", "phase": "destination_published", "destination": candidate.destination_rel, "digest": record.asset_digest, "source": candidate.source_path, "destinations": list(published_manifest), "assets": list(staged_assets), "preimages": original_documents, "documents": staged_documents},
                    phase="destination_published",
                )
            if callable(phase_hook):
                phase_hook("destination_published", destination_abs)

            # Repair references surgically, never with global string replacement.
            for doc_id, site, new_rel, text in edits:
                new_text = rewrite_reference(site, new_rel)
                document_abs = os.path.join(root, doc_id.replace("/", os.sep))
                if not os.path.isfile(document_abs):
                    continue
                with open(document_abs, "r", encoding="utf-8") as handle:
                    current = handle.read()
                original_documents.setdefault(document_abs, current)
                # Spans refer to the captured preimage; recompute against current.
                sites_now = scan_references(doc_id, current)
                match = next(
                    (item for item in sites_now if item.start == site.start and item.target == site.target),
                    None,
                )
                if match is None:
                    match = next((item for item in sites_now if item.target == site.target), None)
                if match is None:
                    raise MediaOwnershipError(
                        "document reference changed during adoption",
                        code="resource_changed",
                    )
                updated = current[: match.start] + rewrite_reference(match, new_rel) + current[match.end :]
                _write_atomic_text(document_abs, updated)
                document_manifest.append({"document_id": str(doc_id), "before": text_fingerprint(current), "after": text_fingerprint(updated)})
            if callable(recorder) and "request_digest" in locals():
                recorder(
                    owner_subject_id=str(owner_subject_id),
                    operation_id=f"__host_media_adopt__{op_id}",
                    request_digest=request_digest,
                    generation=0,
                    receipt={"operation_id": op_id, "workspace_root": os.path.realpath(root), "lease_owner": f"{os.getpid()}-{threading.get_ident()}-{op_id}", "phase": "documents_rewritten", "destination": candidate.destination_rel, "digest": record.asset_digest, "source": candidate.source_path, "destinations": list(published_manifest), "assets": list(staged_assets), "preimages": original_documents, "documents": staged_documents},
                    phase="documents_rewritten",
                )
            if callable(phase_hook):
                phase_hook("documents_rewritten", [item[0] for item in edits])

            # Keep the original until the final operation receipt is durable.
            sources_to_remove.append(candidate.source_path)
            published_for_rollback.append((destination_abs, original_documents))
        except Exception as error:
            if isinstance(error, FilesFacadeError) and getattr(error, "code", None) == "operation_pending":
                raise
            # A mid-publication failure leaves the original asset and links
            # usable.  The staged receipt remains available for reconciliation.
            for document_abs, original in original_documents.items():
                try:
                    _write_atomic_text(document_abs, original)
                except OSError:
                    pass
            try:
                if os.path.isfile(destination_abs):
                    os.remove(destination_abs)
            except OSError:
                pass
            conflicts.append({
                "code": "publication_failed",
                "document_id": record.document_id,
                "reason": str(error) or "media publication failed",
                "asset_name": record.asset_name,
            })
            continue

        adopted.append({
            "asset_name": record.asset_name,
            "destination_rel": candidate.destination_rel,
            "digest": record.asset_digest,
            "document_id": record.document_id,
        })

    # A rival may have committed while this claimant was doing its read-only
    # preflight. Never overwrite that terminal receipt with a collision result.
    if callable(existing_getter):
        raced = existing_getter(owner_subject_id=str(owner_subject_id), operation_id=f"__host_media_adopt__{op_id}")
        raced_receipt = raced.get("receipt") if isinstance(raced, Mapping) else None
        if isinstance(raced_receipt, Mapping) and str(raced_receipt.get("phase") or "") in {"complete", "recovery_required"} and isinstance(raced_receipt.get("resource_receipt"), Mapping):
            resource = dict(raced_receipt["resource_receipt"])
            return {"status": str(resource.get("status") or "conflict"), "adopted": list(raced_receipt.get("adopted") or []), "conflicts": list(raced_receipt.get("conflicts") or []), "rejected": list(raced_receipt.get("rejected") or []), "resource_receipt": resource}
        if isinstance(raced_receipt, Mapping) and str(raced_receipt.get("phase") or "") in {"staged", "destination_published", "documents_rewritten", "source_removal_pending"} and str(raced_receipt.get("lease_owner") or "") != f"{os.getpid()}-{threading.get_ident()}-{op_id}":
            raise FilesFacadeError("adoption operation is pending reconciliation", code="operation_pending")

    receipt = {
        "action_id": f"adopt-{op_id}",
        "status": "complete" if adopted and not conflicts else ("conflict" if conflicts else "noop"),
        "phase": "complete",
        "durable": True,
        "operation_id": op_id,
        "workspace_id": str(workspace_id),
        "adopted_count": len(adopted),
        "conflict_count": len(conflicts),
    }
    recorder = getattr(operation_store, "record_operation", None)
    if callable(recorder):
        try:
            recorder(
                owner_subject_id=str(owner_subject_id),
                operation_id=f"__host_media_adopt__{op_id}",
                request_digest=locals().get("request_digest") or _digest_json({"operation_id": op_id, "workspace_id": str(workspace_id)}),
                generation=0,
                receipt={
                    "operation_id": op_id,
                    "workspace_id": str(workspace_id),
                    "workspace_root": os.path.realpath(root),
                    "phase": "source_removal_pending",
                    "lease_owner": f"{os.getpid()}-{threading.get_ident()}-{op_id}",
                    "adopted": adopted,
                    "conflicts": conflicts,
                    "rejected": list(plan.rejected),
                    "preimages": preimages,
                    "destinations": list(published_manifest),
                    "assets": list(staged_assets),
                    "documents": list(staged_documents),
                    "history_state": "pending",
                    "history_expected": bool(history_handles),
                    "provenance_expected_count": len(pending),
                    "resource_receipt": {**receipt, "status": "pending", "phase": "source_removal_pending", "durable": False},
                    "provenance_records": [
                        {
                            **candidate.provenance.as_receipt(), **({"media_dir": posixpath.dirname(candidate.destination_rel)} if candidate.provenance.media_dir is not None else {}),
                            "canonical_root": os.path.realpath(root),
                            "document_path": os.path.relpath(os.path.join(candidate.provenance.canonical_root, candidate.provenance.document_path), root).replace(os.sep, "/"),
                            "origin": "workspace",
                            "owner_subject_id": str(owner_subject_id),
                            "workspace_id": str(workspace_id),
                            "references": [str(item[0]) for item in edits],
                        }
                        for candidate, edits, _ in pending
                    ],
                },
                phase="source_removal_pending",
            )
        except Exception:
            for destination_abs, original_documents in published_for_rollback:
                for document_abs, original in original_documents.items():
                    try:
                        _write_atomic_text(document_abs, original)
                    except OSError:
                        pass
                try:
                    if os.path.isfile(destination_abs):
                        os.remove(destination_abs)
                except OSError:
                    pass
            raise
    removal_failures: list[str] = []
    for source_path in sources_to_remove:
        try:
            os.remove(source_path)
            parent = os.path.dirname(source_path)
            if parent and parent != root and not os.listdir(parent):
                os.rmdir(parent)
        except OSError:
            removal_failures.append(source_path)
        if not removal_failures and callable(phase_hook):
            phase_hook("source_removed", source_path)
    if not removal_failures and callable(phase_hook):
        phase_hook("sources_removed", list(sources_to_remove))
    history_results: list[Mapping[str, Any]] = []
    if history_handles:
        for handle, document_abs in history_handles:
            if receipt.get("status") == "complete":
                history_results.append(complete_file_capture(handle, document_abs, committed=True))
            else:
                history_results.append(handle.abort())
    recovery_settled = any(result.get("capture_phase") == "recovery_settled" for result in history_results)
    history_failure = any(
        result.get("history_status") != "complete"
        and (result.get("history_status"), result.get("capture_phase")) != ("skipped", "recovery_settled")
        for result in history_results
    )
    if not removal_failures and not history_failure and history_handles and callable(recorder):
        recorder(
            owner_subject_id=str(owner_subject_id),
            operation_id=f"__host_media_adopt__{op_id}",
            request_digest=locals().get("request_digest") or _digest_json({"operation_id": op_id, "workspace_id": str(workspace_id)}),
            generation=0,
            receipt={"operation_id": op_id, "workspace_id": str(workspace_id), "workspace_root": os.path.realpath(root), "phase": "source_removal_pending", "lease_owner": lease_owner, "history_state": "recovery_settled" if recovery_settled else "complete", "history_expected": True, "provenance_expected_count": len(pending), "adopted": adopted, "conflicts": conflicts, "rejected": list(plan.rejected), "resource_receipt": receipt, "provenance_records": [{**candidate.provenance.as_receipt(), **({"media_dir": posixpath.dirname(candidate.destination_rel)} if candidate.provenance.media_dir is not None else {}), "canonical_root": os.path.realpath(root), "origin": "workspace", "owner_subject_id": str(owner_subject_id), "workspace_id": str(workspace_id), "document_path": os.path.relpath(os.path.join(candidate.provenance.canonical_root, candidate.provenance.document_path), root).replace(os.sep, "/"), "references": [str(item[0]) for item in edits]} for candidate, edits, _ in pending], "destinations": list(published_manifest), "assets": list(staged_assets), "documents": list(staged_documents), "preimages": preimages},
            phase="source_removal_pending",
        )
        if callable(phase_hook):
            phase_hook("history_completed", op_id)
    if not removal_failures and not history_failure and callable(recorder):
        _persist_workspace_provenance(
            operation_store,
            owner_subject_id=str(owner_subject_id),
            operation_id=op_id,
            request_digest=locals().get("request_digest") or _digest_json({"operation_id": op_id, "workspace_id": str(workspace_id)}),
            records=[
                {
                    **candidate.provenance.as_receipt(), **({"media_dir": posixpath.dirname(candidate.destination_rel)} if candidate.provenance.media_dir is not None else {}),
                    "canonical_root": os.path.realpath(root),
                    "document_path": os.path.relpath(os.path.join(candidate.provenance.canonical_root, candidate.provenance.document_path), root).replace(os.sep, "/"),
                    "origin": "workspace",
                    "owner_subject_id": str(owner_subject_id),
                    "workspace_id": str(workspace_id),
                    "references": [str(item[0]) for item in edits],
                }
                for candidate, edits, _ in pending
            ],
        )
        recorder(
            owner_subject_id=str(owner_subject_id),
            operation_id=f"__host_media_adopt__{op_id}",
            request_digest=locals().get("request_digest") or _digest_json({"operation_id": op_id, "workspace_id": str(workspace_id)}),
            generation=0,
            receipt={
                "operation_id": op_id,
                "workspace_id": str(workspace_id),
                "workspace_root": os.path.realpath(root),
                "phase": "complete",
                "lease_owner": lease_owner,
                "adopted": adopted,
                "conflicts": conflicts,
                "rejected": list(plan.rejected),
                "preimages": preimages,
                "resource_receipt": receipt,
            },
            phase="complete",
        )
        _persist_workspace_provenance(
            operation_store,
            owner_subject_id=str(owner_subject_id),
            operation_id=op_id,
            request_digest=locals().get("request_digest") or _digest_json({"operation_id": op_id, "workspace_id": str(workspace_id)}),
            records=[
                {
                    **candidate.provenance.as_receipt(), **({"media_dir": posixpath.dirname(candidate.destination_rel)} if candidate.provenance.media_dir is not None else {}),
                    "canonical_root": os.path.realpath(root),
                    "document_path": os.path.relpath(os.path.join(candidate.provenance.canonical_root, candidate.provenance.document_path), root).replace(os.sep, "/"),
                    "origin": "workspace",
                    "owner_subject_id": str(owner_subject_id),
                    "workspace_id": str(workspace_id),
                    "references": [str(item[0]) for item in edits],
                }
                for candidate, edits, _ in pending
            ],
        )
    if history_failure and not removal_failures:
        receipt = {**receipt, "status": "conflict", "durable": False, "recovery": "history_capture_failed"}
        if callable(recorder):
            _persist_workspace_provenance(operation_store, owner_subject_id=str(owner_subject_id), operation_id=op_id, request_digest=locals().get("request_digest") or _digest_json({"operation_id": op_id, "workspace_id": str(workspace_id)}), records=[{**candidate.provenance.as_receipt(), **({"media_dir": posixpath.dirname(candidate.destination_rel)} if candidate.provenance.media_dir is not None else {}), "canonical_root": os.path.realpath(root), "document_path": os.path.relpath(os.path.join(candidate.provenance.canonical_root, candidate.provenance.document_path), root).replace(os.sep, "/"), "origin": "workspace", "owner_subject_id": str(owner_subject_id), "workspace_id": str(workspace_id), "references": [str(item[0]) for item in edits]} for candidate, edits, _ in pending])
            recorder(owner_subject_id=str(owner_subject_id), operation_id=f"__host_media_adopt__{op_id}", request_digest=locals().get("request_digest") or _digest_json({"operation_id": op_id, "workspace_id": str(workspace_id)}), generation=0, receipt={"operation_id": op_id, "workspace_id": str(workspace_id), "workspace_root": os.path.realpath(root), "phase": "recovery_required", "lease_owner": lease_owner, "adopted": adopted, "conflicts": conflicts, "rejected": list(plan.rejected), "preimages": preimages, "destinations": list(published_manifest), "assets": list(staged_assets), "documents": list(staged_documents), "provenance_records": [{**candidate.provenance.as_receipt(), **({"media_dir": posixpath.dirname(candidate.destination_rel)} if candidate.provenance.media_dir is not None else {}), "canonical_root": os.path.realpath(root), "document_path": os.path.relpath(os.path.join(candidate.provenance.canonical_root, candidate.provenance.document_path), root).replace(os.sep, "/"), "origin": "workspace", "owner_subject_id": str(owner_subject_id), "workspace_id": str(workspace_id), "references": [str(item[0]) for item in edits]} for candidate, edits, _ in pending], "resource_receipt": receipt, "history_results": history_results}, phase="recovery_required")
    elif removal_failures:
        receipt = {
            **receipt,
            "status": "conflict",
            "durable": False,
            "recovery": "retained_duplicate",
            "removal_failures": removal_failures,
        }
        if callable(recorder):
            recorder(
                owner_subject_id=str(owner_subject_id),
                operation_id=f"__host_media_adopt__{op_id}",
                request_digest=locals().get("request_digest") or _digest_json({"operation_id": op_id, "workspace_id": str(workspace_id)}),
                generation=0,
                receipt={"operation_id": op_id, "phase": "recovery_required", "lease_owner": lease_owner, "resource_receipt": receipt, "adopted": adopted, "conflicts": conflicts, "rejected": list(plan.rejected), "preimages": preimages},
                phase="recovery_required",
            )
    elif receipt.get("status") == "complete" and callable(post_terminal_hook):
        # This hook is deliberately after the durable terminal receipt and all
        # source disposition.  Crash fixtures use it to model a lost response;
        # replay must therefore return the stored terminal receipt without
        # publishing another destination or rewriting a document.
        post_terminal_hook("terminal", dict(receipt))
    return {
        "status": receipt["status"],
        "adopted": adopted,
        "conflicts": conflicts,
        "rejected": list(plan.rejected),
        "resource_receipt": receipt,
    }


def adopt_loose_media_for_workspace(**kwargs: Any) -> dict[str, Any]:
    """Serialize adoption publication while the durable operation is claimed.

    The Files repository remains the cross-process authority; this mutex closes
    the same-process race between two workspace creation requests sharing one
    operation id, so the second caller observes the completed receipt.
    """
    store = kwargs.get("operation_store")
    owner = str(kwargs.get("owner_subject_id") or "")
    workspace_id = str(kwargs.get("workspace_id") or "")
    root = os.path.normpath(str(kwargs.get("workspace_root") or ""))
    if not all(callable(getattr(store, name, None)) for name in
               ("get_workspace", "get_location", "generation", "claim_host_mutation", "release_host_mutation")):
        raise FilesFacadeError("workspace adoption authority is unavailable", code="provider_unavailable")
    try:
        workspace = store.get_workspace(workspace_id)
        location = store.get_location(workspace.location_id)
        canonical_root = os.path.normpath(os.path.join(str(location.canonical_path), str(workspace.relative_folder or "")))
        if (not os.path.isabs(root) or root != canonical_root or str(workspace.owner_subject_id) != owner
                or workspace.archived or not location.enabled or location.availability != "available"):
            raise ValueError("workspace identity changed")
        claim = store.claim_host_mutation(owner_subject_id=owner, workspace_id=workspace_id,
                                          generation=int(store.generation()), paths=[root])
    except Exception as exc:
        raise FilesFacadeError("workspace adoption conflicts with another mutation", code=getattr(exc, "code", "provider_unavailable")) from exc
    with _ADOPTION_MUTEX:
        result = _adopt_loose_media_for_workspace(**kwargs)
    receipt = result.get("resource_receipt") if isinstance(result, Mapping) else None
    if isinstance(receipt, Mapping) and (receipt.get("recovery") or receipt.get("phase") in {"recovery_required", "pending"}):
        return result
    store.release_host_mutation(owner_subject_id=owner, **claim)
    return result


def _write_atomic_text(path: str, text: str) -> None:
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    temp = f"{path}.tmp-{os.getpid()}"
    with open(temp, "w", encoding="utf-8") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp, path)


def _persist_workspace_provenance(operation_store: Any, *, owner_subject_id: str, operation_id: str, request_digest: str, records: Sequence[Mapping[str, Any]]) -> None:
    recorder = getattr(operation_store, "record_operation", None)
    if not callable(recorder):
        return
    for index, provenance in enumerate(records):
        recorder(
            owner_subject_id=str(owner_subject_id),
            operation_id=f"__host_media__adopted-{operation_id}-{index}",
            request_digest=f"{request_digest}:provenance:{index}",
            generation=0,
            receipt={"provenance": dict(provenance), "phase": "complete"},
            phase="complete",
        )


def _ensure_workspace_provenance(operation_store: Any, *, owner_subject_id: str, operation_id: str, request_digest: str, records: Sequence[Mapping[str, Any]]) -> None:
    """Repair missing per-asset provenance before honoring terminal replay."""
    lister = getattr(operation_store, "list_operations", None)
    expected = [f"__host_media__adopted-{operation_id}-{index}" for index, _ in enumerate(records)]
    present: set[str] = set()
    if callable(lister):
        for row in lister(owner_subject_id=str(owner_subject_id), operation_prefix="__host_media__adopted-", limit=256):
            if isinstance(row, Mapping) and str(row.get("operation_id") or "") in expected:
                present.add(str(row["operation_id"]))
    if len(present) != len(expected):
        _persist_workspace_provenance(operation_store, owner_subject_id=owner_subject_id, operation_id=operation_id, request_digest=request_digest, records=records)


def _all_registered_roots(operation_store: Any) -> list[WorkspaceRootRecord]:
    getter = getattr(operation_store, "list_workspaces", None)
    if not callable(getter):
        return []
    location_getter = getattr(operation_store, "get_location", None)
    records: list[WorkspaceRootRecord] = []
    # A failed registry read is not an empty registry: classification must see
    # every root or it would absorb a neighbor.  Fail closed.
    try:
        workspaces = getter(owner_subject_id=None, include_archived=True)
    except Exception as exc:
        raise MediaOwnershipError(
            "workspace registry is unavailable", code="provider_unavailable"
        ) from exc
    for workspace in workspaces:
        location_id = getattr(workspace, "location_id", None)
        relative = str(getattr(workspace, "relative_folder", "") or "")
        if not location_id or not callable(location_getter):
            continue
        try:
            location = location_getter(str(location_id))
        except Exception:
            continue
        root = os.path.normpath(str(getattr(location, "canonical_path", "") or ""))
        if not root:
            continue
        if relative:
            root = os.path.normpath(os.path.join(root, *relative.split("/")))
        records.append(
            WorkspaceRootRecord(
                str(getattr(workspace, "id", "") or ""),
                str(getattr(workspace, "owner_subject_id", "") or ""),
                root,
                bool(getattr(workspace, "archived", False)),
            )
        )
    return records
