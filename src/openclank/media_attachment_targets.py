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
import re
from pathlib import Path
from typing import Any, Mapping, Sequence

from src.openclank.files_facade import FilesFacadeError, ProviderContext, ProviderResource
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
)

_LIMIT = 10 * 1024 * 1024


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

    def _registry_roots(self, owner_subject_id: str) -> list[WorkspaceRootRecord]:
        # A failed registry read is not an empty registry: classification must
        # see every root or it would absorb a neighbor.  Fail closed.
        if self._workspace_roots is not None:
            try:
                rows = self._workspace_roots(owner_subject_id)
            except Exception as exc:
                raise MediaOwnershipError(
                    "workspace registry is unavailable", code="provider_unavailable"
                ) from exc
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
        if expected_source_revision is not None and dict(source_current.revision or {}) != dict(expected_source_revision):
            raise FilesFacadeError("attachment source changed during materialization", code="resource_changed")

        request_digest = _digest_json({
            "account_id": str(context.owner_subject_id),
            "workspace_id": str(context.workspace_id or "default"),
            "operation_id": str(operation_id),
            "generation": int(context.policy_generation),
            "source": {
                key: source.get(key)
                for key in ("resource_ref", "import_receipt_id", "item_id", "expected_revision")
                if source.get(key) is not None
            },
            "target": {"kind": "host_document", "resource_ref": target_ref, "expected_revision": target.get("expected_revision")},
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
        layout = layout_for_absolute(
            absolute_document_path=document_path,
            canonical_root=origin.canonical_root,
            origin=origin.origin,
            owner_subject_id=context.owner_subject_id,
            workspace_id=origin.workspace_id,
        )
        media_dir_abs = layout.absolute_media_dir()
        os.makedirs(media_dir_abs, exist_ok=True)
        existing = [entry for entry in os.listdir(media_dir_abs) if os.path.isfile(os.path.join(media_dir_abs, entry))]
        desired = _download_name(filename or source_current.name)
        asset_name = allocate_media_name(desired, existing)
        asset_path = os.path.join(media_dir_abs, asset_name)

        source_revision = source_current.revision or {"kind": "contentDigest", "value": digest_hex}
        target_revision = target_entry.revision or {"kind": "hostFingerprint", "value": ""}
        relative_link = f"{layout.media_dir}/{asset_name}"

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
        )
        planned = {
            "operation_id": str(operation_id),
            "account_id": str(context.owner_subject_id),
            "generation": int(context.policy_generation),
            "phase": "pending",
            "workspace_id": str(context.workspace_id or "default"),
            "preparation_receipt_id": f"prep-{digest_hex[:32]}",
            "source_revision": dict(source_revision),
            "target_identity": {"resource_ref": target_ref, "kind": "host_document"},
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
            "provenance": provenance.as_receipt(),
            "source_identity": {
                "resource_ref": str(source.get("resource_ref") or ""),
                "provider": str(getattr(source_provider, "name", "")),
            },
            "mode": normalized_mode,
        }
        self._save(context, str(operation_id), request_digest, planned)
        # Asset bytes are durable before the receipt is complete, so a link is
        # never reported ahead of the asset's durability.
        _write_atomic(asset_path, data)
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
        self._save(context, str(operation_id), request_digest, receipt)
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


_INTERNAL_RECEIPT_KEYS = frozenset({"asset_path", "digest_hex", "phase"})


def _public_receipt(value: Mapping[str, Any]) -> dict[str, Any]:
    """Drop storage-internal fields from a typed preparation DTO.

    ``asset_path`` is a local filesystem path used only for crash recovery; it
    never belongs in the browser-facing receipt.  ``phase``/``digest_hex`` are
    likewise recovery bookkeeping.
    """
    return {key: item for key, item in value.items() if key not in _INTERNAL_RECEIPT_KEYS}


def _document_path(entry: Any, target_origin_id: str | None = None) -> str:
    """Absolute filesystem path of a Host resource entry.

    Host origin ids are ``host:<absolute path>``; a provider may also publish an
    explicit path.  Synthetic Host ids are never presented as Copal ids.
    """
    for candidate in (target_origin_id, getattr(entry, "origin_id", None)):
        if isinstance(candidate, str) and candidate.startswith("host:"):
            path = candidate[len("host:") :]
            if path and os.path.basename(path):
                return os.path.normpath(path)
    for attribute in ("path", "absolute_path"):
        value = getattr(entry, attribute, None)
        if isinstance(value, str) and value and os.path.basename(value):
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
    return records


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


def adopt_loose_media_for_workspace(
    *,
    operation_store: Any,
    owner_subject_id: str,
    workspace_root: str,
    workspace_id: str,
    operation_id: str | None = None,
    document_sources: Any | None = None,
    lore_capture: Any | None = None,
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

    op_id = str(operation_id or f"adopt-{workspace_id}")
    adopted: list[dict[str, Any]] = []
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
                if normalized.startswith("media/") and normalized.endswith(f"/{record.asset_name}"):
                    matched = normalized == f"media/{_document_stem(record.document_path)}/{record.asset_name}" or normalized == old_rel
                elif normalized.endswith(f"/{record.asset_name}") and "media/" in normalized:
                    matched = normalized.endswith(old_rel)
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
                edits.append((str(doc_id), site, new_rel, text))
        if local_conflicts:
            conflicts.extend(local_conflicts)
            continue
        pending.append((candidate, edits, preimages))

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
        os.makedirs(os.path.dirname(destination_abs), exist_ok=True)
        _write_atomic(destination_abs, data)

        # Repair references surgically, never with global string replacement.
        for doc_id, site, new_rel, text in edits:
            new_text = rewrite_reference(site, new_rel)
            document_abs = os.path.join(root, doc_id.replace("/", os.sep))
            if not os.path.isfile(document_abs):
                continue
            with open(document_abs, "r", encoding="utf-8") as handle:
                current = handle.read()
            # Spans refer to the captured preimage; recompute against current.
            sites_now = scan_references(doc_id, current)
            match = next(
                (item for item in sites_now if item.start == site.start and item.target == site.target),
                None,
            )
            if match is None:
                match = next((item for item in sites_now if item.target == site.target), None)
            if match is None:
                continue
            updated = current[: match.start] + rewrite_reference(match, new_rel) + current[match.end :]
            _write_atomic_text(document_abs, updated)

        # Remove the now-empty loose media file and its directory when empty.
        try:
            os.remove(candidate.source_path)
            parent = os.path.dirname(candidate.source_path)
            if parent and parent != root and not os.listdir(parent):
                os.rmdir(parent)
        except OSError:
            pass

        adopted.append({
            "asset_name": record.asset_name,
            "destination_rel": candidate.destination_rel,
            "digest": record.asset_digest,
            "document_id": record.document_id,
        })

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
                request_digest=_digest_json({"operation_id": op_id, "workspace_id": str(workspace_id)}),
                generation=0,
                receipt={
                    "operation_id": op_id,
                    "workspace_id": str(workspace_id),
                    "phase": "complete",
                    "adopted": adopted,
                    "conflicts": conflicts,
                    "rejected": list(plan.rejected),
                    "preimages": preimages,
                    "resource_receipt": receipt,
                    "provenance": {
                        "canonical_root": root,
                        "origin": "workspace",
                        "owner_subject_id": str(owner_subject_id),
                        "workspace_id": str(workspace_id),
                        "document_id": "",
                        "document_path": "",
                        "asset_name": "",
                        "asset_digest": binary_digest(b""),
                        "references": [],
                    },
                },
                phase="complete",
            )
        except Exception:
            pass
    return {
        "status": receipt["status"],
        "adopted": adopted,
        "conflicts": conflicts,
        "rejected": list(plan.rejected),
        "resource_receipt": receipt,
    }


def _write_atomic_text(path: str, text: str) -> None:
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    temp = f"{path}.tmp-{os.getpid()}"
    with open(temp, "w", encoding="utf-8") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp, path)


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
