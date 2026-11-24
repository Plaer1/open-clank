"""Provider-neutral, lazy resource namespace used by the Files application.

Providers own their records and bytes.  This facade owns only dispatch,
owner/policy binding, opaque references, cursor integrity, and the normalized
metadata/action vocabulary.  It intentionally has no content shadow database.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import sys
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass, field, replace
from pathlib import Path
import stat
from typing import Any, AsyncIterator, Callable, Mapping, Protocol, Sequence

from src.openclank.file_policy import FilePolicyError
from src.openclank.host_apps import host_apps, HostAppsError
from src.openclank.resource_refs import (
    PROVIDERS,
    RESOURCE_CAPABILITIES,
    RESOURCE_KINDS,
    ResourceRef,
    ResourceRefError,
    issue_resource_ref,
    resolve_resource_ref,
    resolve_resource_ref_for_reissue,
    stable_resource_id,
)
from src.secret_storage import decrypt, encrypt
from src.upload_limits import FILES_IMPORT_MAX_BYTES


FACADE_VERSION = 1
FILES_SERVICE_MAX_CHUNK_BYTES = 512 * 1024
FILES_SERVICE_MAX_TOTAL_BYTES = 10 * 1024 * 1024
CURSOR_TTL_SECONDS = 15 * 60
MAX_PAGE_SIZE = 200
MAX_IMPORT_RELATIVE_BYTES = 1024
MAX_IMPORT_RELATIVE_DEPTH = 16
SORT_KEYS = ("name", "kind", "modified", "size")
OPEN_TARGET_APPS = frozenset(
    {
        "chat",
        "copal_notes",
        "document_editor",
        "editor",
        "imps",
        "library",
        "research",
    }
)
EXACT_REISSUE_PROVIDERS = frozenset({"copal", "files", "library"})
MAX_REVEAL_ANCESTORS = 32
PUBLIC_PROVENANCE_KEYS = frozenset(
    {
        "category",
        "corpus",
        "description",
        "document_kind",
        "domain",
        "draft",
        "favorite",
        "language",
        "message_count",
        "model",
        "source",
        "source_count",
        "source_digest",
        "selected_name",
        "declared_mime",
        "detected_mime",
        "versions",
        "view",
        "workspace",
    }
)
_OPERATION_STORE_LOCKS: dict[int, threading.RLock] = {}
_OPERATION_STORE_LOCKS_GUARD = threading.Lock()


class FilesFacadeError(ValueError):
    def __init__(self, message: str, *, code: str = "invalid_resource_request") -> None:
        super().__init__(message)
        self.code = code


def _strict_nonnegative_int(value: Any, field: str) -> int:
    """Validate an integer request field without accepting JSON coercions."""
    if type(value) is not int or value < 0:
        raise FilesFacadeError(f"{field} is invalid", code="invalid_resource_request")
    return value


class _BoundedUpload:
    """UploadFile-compatible reader that fails while bytes are streamed."""

    def __init__(self, upload: Any, limit: int, generation_check: Callable[[], bool] | None = None) -> None:
        self._upload = upload
        self._limit = int(limit)
        self._generation_check = generation_check
        self.total = 0
        self.filename = getattr(upload, "filename", None)
        self.content_type = getattr(upload, "content_type", None)

    async def read(self, size: int = -1) -> bytes:
        if self._generation_check is not None and not self._generation_check():
            raise FilesFacadeError("file policy generation changed", code="policy_generation_changed")
        requested = int(size)
        if requested < 0:
            requested = self._limit - self.total + 1
        requested = max(1, min(requested, self._limit - self.total + 1))
        chunk = await self._upload.read(requested)
        self.total += len(chunk or b"")
        if self.total > self._limit:
            raise FilesFacadeError("import exceeds the configured byte limit", code="upload_too_large")
        return chunk


class _SpooledUpload:
    """Replayable, bounded upload reader used between reservation and staging.

    Import idempotency needs the content digest before a provider side effect
    starts.  The upload is therefore copied to a private temporary file first;
    provider adapters still receive an UploadFile-shaped async reader and no
    whole upload is retained in Python memory.
    """

    def __init__(self, upload: Any) -> None:
        self._file = tempfile.TemporaryFile(mode="w+b")
        self.filename = getattr(upload, "filename", None)
        self.content_type = getattr(upload, "content_type", None)
        self.length = 0
        self.digest = hashlib.sha256()

    async def copy_from(
        self,
        upload: Any,
        *,
        limit: int,
        generation_check: Callable[[], bool] | None = None,
    ) -> None:
        while True:
            if generation_check is not None and not generation_check():
                raise FilesFacadeError("file policy generation changed", code="policy_generation_changed")
            chunk = await upload.read(min(FILES_SERVICE_MAX_CHUNK_BYTES, limit - self.length + 1))
            if not chunk:
                break
            if not isinstance(chunk, (bytes, bytearray)):
                raise FilesFacadeError("import stream is invalid", code="invalid_resource_request")
            chunk = bytes(chunk)
            self.length += len(chunk)
            if self.length > limit:
                raise FilesFacadeError("import exceeds the configured byte limit", code="upload_too_large")
            self.digest.update(chunk)
            self._file.write(chunk)
        self._file.flush()
        self._file.seek(0)

    async def read(self, size: int = -1) -> bytes:
        return self._file.read() if size < 0 else self._file.read(max(0, int(size)))

    def close(self) -> None:
        self._file.close()


@dataclass(frozen=True)
class ProviderContext:
    owner_subject_id: str
    owner_username: str
    policy_generation: int
    is_admin: bool = False
    workspace_id: str = "default"


@dataclass(frozen=True)
class ProviderResource:
    """Internal provider record. ``origin_id`` is never serialized directly."""

    origin_id: str
    name: str
    kind: str
    capabilities: tuple[str, ...]
    parent_origin_id: str | None = None
    location_id: str | None = None
    workspace_id: str | None = None
    mime_type: str | None = None
    size: int | None = None
    modified_unix_ms: int | None = None
    created_unix_ms: int | None = None
    provenance: Mapping[str, Any] = field(default_factory=dict)
    open_target: Mapping[str, Any] | None = None
    download_name: str | None = None
    preview_kind: str | None = None
    thumbnail_url: str | None = None
    # Presentation-only capability; this is never an authority grant.  None
    # preserves compatibility with providers that have not negotiated a
    # platform adapter yet, while Host explicitly reports false off macOS.
    native_thumbnail_available: bool | None = None
    native_icon_available: bool = False
    sort_kind: str | None = None
    child_sort_keys: tuple[str, ...] = ()
    # Provider-owned mutation receipt. This stays internal until FilesFacade
    # deliberately projects the action id/status; provider origin IDs never
    # belong in the browser receipt.
    action_receipt: Mapping[str, Any] | None = None
    # Optional provider-native revision used by transfer/query adapters.  Old
    # providers may omit it and continue to enforce their own CAS contract.
    revision: Mapping[str, Any] | None = None


@dataclass(frozen=True)
class ProviderPage:
    entries: tuple[ProviderResource, ...]
    next_cursor: str | None = None
    total: int | None = None
    snapshot: str | None = None
    complete: bool | None = None


@dataclass(frozen=True)
class ProviderContent:
    """Internal, already owner-authorized content descriptor.

    Exactly one of ``data``, ``path``, or ``stream`` is populated. Paths never leave the
    server: the HTTP adapter opens them with no-follow semantics and validates
    ``expected_identity`` before sending headers. This compatibility descriptor
    is intentionally narrower than the future Rust-owned transfer handle.
    """

    origin_id: str
    filename: str
    media_type: str
    data: bytes | None = None
    path: Path | None = None
    size: int | None = None
    modified_unix_ms: int | None = None
    etag: str | None = None
    expected_identity: tuple[int, int, int, int] | None = None
    stream: Callable[[int, int], AsyncIterator[bytes]] | None = None
    preview_evidence: Mapping[str, str] | None = None


@dataclass(frozen=True)
class ProviderWorkspaceTarget:
    """Internal host-to-Workspace handoff; its path is never serialized."""

    origin_id: str
    directory_path: str
    open_relative: str = ""
    name: str = "Workspace"


class FilesProvider(Protocol):
    name: str

    async def operation_status(self, context: ProviderContext, *, operation_id: str) -> Mapping[str, Any] | None: ...

    async def roots(self, context: ProviderContext) -> Sequence[ProviderResource]: ...

    def supported_sort_keys(self, *, parent_origin_id: str) -> Sequence[str]: ...

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
    ) -> ProviderPage: ...

    async def stat(self, context: ProviderContext, *, origin_id: str) -> ProviderResource: ...

    async def search(
        self,
        context: ProviderContext,
        *,
        query: str,
        limit: int,
        sort: Mapping[str, Any],
    ) -> ProviderPage: ...

    async def content(self, context: ProviderContext, *, origin_id: str) -> ProviderContent: ...

    async def open_resource(self, context: ProviderContext, *, origin_id: str) -> Mapping[str, Any] | None: ...

    async def create_directory(
        self,
        context: ProviderContext,
        *,
        parent_origin_id: str,
        name: str,
        operation_id: str | None = None,
    ) -> ProviderResource: ...

    async def action(
        self,
        context: ProviderContext,
        *,
        origin_id: str,
        action: str,
        args: Mapping[str, Any],
    ) -> ProviderResource: ...

    def watch(
        self,
        context: ProviderContext,
        *,
        origin_id: str,
    ) -> AsyncIterator[Mapping[str, Any]]: ...


def _normalized_sort(value: Mapping[str, Any] | None) -> dict[str, Any]:
    raw = dict(value or {})
    key = str(raw.get("key") or "name").strip().lower()
    direction = str(raw.get("direction") or "asc").strip().lower()
    directories_first = bool(raw.get("directories_first", True))
    if key not in SORT_KEYS or direction not in {"asc", "desc"}:
        raise FilesFacadeError("unsupported provider sort")
    return {"key": key, "direction": direction, "directories_first": directories_first}


def _supported_sort_keys(provider: FilesProvider, *, parent_origin_id: str) -> tuple[str, ...]:
    """Return the provider's honest ordering contract for one folder.

    Older third-party adapters predate negotiation and retain the original
    four-key contract. First-party adapters declare narrower sets for virtual
    collections whose rows have no meaningful type or byte size.
    """

    callback = getattr(provider, "supported_sort_keys", None)
    raw = callback(parent_origin_id=parent_origin_id) if callable(callback) else SORT_KEYS
    normalized = tuple(dict.fromkeys(str(key or "").strip().lower() for key in raw))
    if not normalized or any(key not in SORT_KEYS for key in normalized) or "name" not in normalized:
        raise FilesFacadeError("provider returned an invalid sort contract", code="provider_unavailable")
    return normalized


def _public_provenance(value: Mapping[str, Any]) -> dict[str, Any]:
    """Keep display provenance useful without serializing provider identities.

    Origin IDs, database keys, host paths, and URLs remain sealed in the
    ResourceRef. Providers may contribute only the fixed presentation vocabulary
    below, with bounded scalar values.
    """
    result: dict[str, Any] = {}
    for key, raw in value.items():
        normalized = str(key or "").strip().lower()
        if normalized not in PUBLIC_PROVENANCE_KEYS or not isinstance(raw, (str, int, float, bool)):
            continue
        if isinstance(raw, str):
            encoded = raw.encode("utf-8")
            result[normalized] = encoded[:512].decode("utf-8", "ignore")
        else:
            result[normalized] = raw
    return result


def _public_action_receipt(value: Mapping[str, Any] | None, *, _depth: int = 0) -> dict[str, Any]:
    """Project provider mutation receipts without exposing provider payloads."""
    if not isinstance(value, Mapping):
        return {}
    result: dict[str, Any] = {}
    for key in ("action_id", "receipt_id", "status", "phase", "outcome", "selected_name"):
        raw = value.get(key)
        if isinstance(raw, (str, int, float, bool)):
            result[key] = str(raw)[:256] if isinstance(raw, str) else raw
    raw_history = value.get("history")
    if isinstance(raw_history, Mapping) and raw_history.get("state") in {"complete", "incomplete", "unavailable"}:
        history = {"state": raw_history["state"]}
        action_id = raw_history.get("actionId")
        if isinstance(action_id, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,128}", action_id):
            history["actionId"] = action_id
        result["history"] = history
    raw_revision = value.get("revision")
    if isinstance(raw_revision, Mapping) and raw_revision.get("kind") and raw_revision.get("value") is not None:
        result["revision"] = {"kind": str(raw_revision["kind"])[:128], "value": str(raw_revision["value"])[:256]}
    raw_receipt = value.get("receipt")
    if _depth < 1 and isinstance(raw_receipt, Mapping):
        nested = _public_action_receipt(raw_receipt, _depth=_depth + 1)
        if nested:
            result["receipt"] = nested
    return result


def _validated_receipt(
    value: Mapping[str, Any], *, operation_id: str, item_id: str,
    expected_item_ids: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Accept only the facade receipt vocabulary from a provider."""
    if not isinstance(value, Mapping) or set(value) - {"state", "items"} or not isinstance(value.get("state"), str) or not isinstance(value.get("items"), Sequence) or isinstance(value.get("items"), (str, bytes)) or len(value["items"]) > 200:
        raise FilesFacadeError("provider returned an invalid operation receipt", code="provider_unavailable")
    allowed_item_keys = {
        "item_id", "outcome", "code", "resource_ref", "resource_key",
        "receipt_id", "revision", "provenance", "action_receipt", "history", "cleanup", "undo",
    }
    safe_items = []
    seen_ids: set[str] = set()
    for raw in value["items"]:
        if not isinstance(raw, Mapping):
            raise FilesFacadeError("provider returned an invalid operation receipt", code="provider_unavailable")
        if set(raw) - allowed_item_keys:
            raise FilesFacadeError("provider returned an invalid operation receipt", code="provider_unavailable")
        if not isinstance(raw.get("outcome"), str):
            raise FilesFacadeError("provider returned an invalid operation receipt", code="provider_unavailable")
        outcome = raw["outcome"].lower()
        if outcome not in {"committed", "unchanged", "failed", "denied", "conflict", "stale", "pending"}:
            raise FilesFacadeError("provider returned an invalid operation receipt", code="provider_unavailable")
        if raw.get("item_id") is not None and (not isinstance(raw.get("item_id"), str) or not raw.get("item_id").strip()):
            raise FilesFacadeError("provider returned an invalid operation receipt", code="provider_unavailable")
        if expected_item_ids is not None and raw.get("item_id") is None:
            raise FilesFacadeError("provider returned an invalid operation receipt", code="provider_unavailable")
        item_id_value = str(raw.get("item_id") or item_id)
        if not item_id_value or item_id_value in seen_ids:
            raise FilesFacadeError("provider returned duplicate operation items", code="provider_unavailable")
        seen_ids.add(item_id_value)
        item = {"item_id": item_id_value, "outcome": outcome}
        if raw.get("cleanup") is not None:
            if raw["cleanup"] not in {"pending", "complete"} or outcome != "committed":
                raise FilesFacadeError("provider returned an invalid cleanup receipt", code="provider_unavailable")
            item["cleanup"] = raw["cleanup"]
        if raw.get("undo") is not None:
            if raw["undo"] != "available" or outcome != "committed":
                raise FilesFacadeError("provider returned an invalid undo receipt", code="provider_unavailable")
            item["undo"] = "available"
        if raw.get("code") is not None:
            if not isinstance(raw["code"], str) or not raw["code"].strip():
                raise FilesFacadeError("provider returned an invalid operation receipt", code="provider_unavailable")
            item["code"] = str(raw["code"])[:128]
        for key in ("resource_ref", "resource_key", "receipt_id"):
            if raw.get(key) is not None:
                if not isinstance(raw[key], str) or not raw[key].strip() or len(raw[key].encode("utf-8")) > 16_384:
                    raise FilesFacadeError("provider returned an invalid operation receipt", code="provider_unavailable")
                item[key] = raw[key]
        if raw.get("revision") is not None:
            revision = raw["revision"]
            if not isinstance(revision, Mapping) or set(revision) != {"kind", "value"} or not isinstance(revision["kind"], str) or not isinstance(revision["value"], str) or not revision["kind"].strip() or not revision["value"].strip() or len(revision["kind"].encode("utf-8")) > 128 or len(revision["value"].encode("utf-8")) > 512:
                raise FilesFacadeError("provider returned an invalid operation receipt", code="provider_unavailable")
            item["revision"] = {"kind": revision["kind"], "value": revision["value"]}
        for nested_key in ("provenance", "action_receipt", "history"):
            if raw.get(nested_key) is not None and not isinstance(raw[nested_key], Mapping):
                raise FilesFacadeError("provider returned an invalid operation receipt", code="provider_unavailable")
        if isinstance(raw.get("provenance"), Mapping):
            if set(raw["provenance"]) - PUBLIC_PROVENANCE_KEYS:
                raise FilesFacadeError("provider returned an invalid operation receipt", code="provider_unavailable")
            item["provenance"] = _public_provenance(raw["provenance"])
        history = _public_action_receipt(raw.get("action_receipt") if isinstance(raw.get("action_receipt"), Mapping) else raw.get("history") if isinstance(raw.get("history"), Mapping) else None)
        if history:
            item["history"] = history
        safe_items.append(item)
    if expected_item_ids is not None and (len(safe_items) != len(expected_item_ids) or seen_ids != set(expected_item_ids)):
        raise FilesFacadeError("provider returned an incomplete operation receipt", code="provider_unavailable")
    state = str(value.get("state") or "partial").lower()
    if state not in {"complete", "partial", "pending"}:
        raise FilesFacadeError("provider returned an invalid operation receipt", code="provider_unavailable")
    if state == "complete" and any(item["outcome"] not in {"committed", "unchanged"} for item in safe_items):
        raise FilesFacadeError("provider operation state is inconsistent", code="provider_unavailable")
    return {"operation_id": operation_id, "state": state, "items": safe_items}


def _validated_preparation(value: Mapping[str, Any], *, operation_id: str, expected_generation: int | None = None) -> dict[str, Any]:
    """Validate the typed attachment preparation descriptor."""
    if not isinstance(value, Mapping):
        raise FilesFacadeError("provider returned an invalid preparation", code="provider_unavailable")
    allowed_keys = {
        "operation_id", "generation", "preparation_receipt_id", "source_revision", "target_identity", "target_revision",
        "insertion", "asset", "source_digest", "request_digest", "asset_size", "account_id", "workspace_id",
        "source_identity", "mode", "action_receipt", "receipt", "history", "provenance", "layout_version",
    }
    if set(value) - allowed_keys:
        raise FilesFacadeError("provider returned an invalid preparation", code="provider_unavailable")
    returned_operation = value.get("operation_id")
    if returned_operation is not None and (not isinstance(returned_operation, str) or not returned_operation.strip() or returned_operation.strip() != operation_id):
        raise FilesFacadeError("provider preparation operation identity is invalid", code="provider_unavailable")
    returned_generation = value.get("generation")
    if returned_generation is not None and (not isinstance(returned_generation, int) or isinstance(returned_generation, bool) or returned_generation < 0 or (expected_generation is not None and returned_generation != int(expected_generation))):
        raise FilesFacadeError("provider preparation generation is invalid", code="provider_unavailable")
    preparation_id = str(value.get("preparation_receipt_id") or "").strip()
    if not preparation_id or len(preparation_id.encode("utf-8")) > 128:
        raise FilesFacadeError("provider returned an invalid preparation receipt", code="provider_unavailable")

    def revision(raw: Any, field: str) -> dict[str, str]:
        if not isinstance(raw, Mapping) or set(raw) != {"kind", "value"} or not isinstance(raw.get("kind"), str) or not isinstance(raw.get("value"), str) or not raw.get("kind") or not raw.get("value"):
            raise FilesFacadeError(f"provider preparation {field} is invalid", code="provider_unavailable")
        kind, token = raw["kind"].strip(), raw["value"].strip()
        if not kind or len(kind.encode("utf-8")) > 128 or not token or len(token.encode("utf-8")) > 512:
            raise FilesFacadeError(f"provider preparation {field} is invalid", code="provider_unavailable")
        return {"kind": kind, "value": token}

    def resource_key(raw: Any, field: str) -> str | dict[str, str]:
        if isinstance(raw, str):
            token = raw.strip()
            if not token or len(token.encode("utf-8")) > 16_384 or "\x00" in token:
                raise FilesFacadeError(f"provider preparation {field} is invalid", code="provider_unavailable")
            return token
        if not isinstance(raw, Mapping) or set(raw) - {"provider", "account_id", "workspace_id", "resource_id"}:
            raise FilesFacadeError(f"provider preparation {field} is invalid", code="provider_unavailable")
        if not raw.get("provider") or not raw.get("resource_id"):
            raise FilesFacadeError(f"provider preparation {field} is invalid", code="provider_unavailable")
        result: dict[str, str] = {}
        for key in ("provider", "account_id", "workspace_id", "resource_id"):
            if raw.get(key) is None:
                continue
            if not isinstance(raw[key], str):
                raise FilesFacadeError(f"provider preparation {field} is invalid", code="provider_unavailable")
            token = raw[key].strip()
            if not token or len(token.encode("utf-8")) > 16_384 or "\x00" in token or "/" in token or "\\" in token:
                raise FilesFacadeError(f"provider preparation {field} is invalid", code="provider_unavailable")
            result[key] = token
        return result

    source_revision = revision(value.get("source_revision"), "source_revision")
    target_revision = revision(value.get("target_revision"), "target_revision")
    source_digest = value.get("source_digest")
    if source_digest is not None:
        if not isinstance(source_digest, str) or not source_digest.strip() or len(source_digest.encode("utf-8")) > 128 or "\x00" in source_digest:
            raise FilesFacadeError("provider preparation source digest is invalid", code="provider_unavailable")
        source_digest = source_digest.strip()
    request_digest = value.get("request_digest")
    if request_digest is not None and (not isinstance(request_digest, str) or not request_digest.strip() or len(request_digest.encode("utf-8")) > 128 or "\x00" in request_digest):
        raise FilesFacadeError("provider preparation request digest is invalid", code="provider_unavailable")
    asset_size = value.get("asset_size")
    if asset_size is not None and (isinstance(asset_size, bool) or not isinstance(asset_size, int) or asset_size < 0 or asset_size > 10 * 1024 * 1024):
        raise FilesFacadeError("provider preparation asset size is invalid", code="provider_unavailable")
    for identity_key in ("account_id", "workspace_id"):
        if value.get(identity_key) is not None and (not isinstance(value[identity_key], str) or not value[identity_key].strip() or len(value[identity_key].encode("utf-8")) > 256):
            raise FilesFacadeError("provider preparation scope identity is invalid", code="provider_unavailable")
    source_identity = value.get("source_identity")
    safe_source_identity = None
    if source_identity is not None:
        if not isinstance(source_identity, Mapping):
            raise FilesFacadeError("provider preparation source identity is invalid", code="provider_unavailable")
        if source_identity.get('kind') == 'client_upload':
            if (set(source_identity) != {'kind','name','mime','sha256','size'} or not isinstance(source_identity.get('name'), str)
                or not source_identity['name'] or len(source_identity['name'].encode('utf-8')) > 240 or any(c in source_identity['name'] for c in ('/', '\\', '\x00'))
                or not isinstance(source_identity.get('mime'), str) or not source_identity['mime'] or len(source_identity['mime'].encode('utf-8')) > 128
                or any(ord(c) < 32 or ord(c) == 127 for c in source_identity['name'] + source_identity['mime'])
                or not re.fullmatch(r'[a-f0-9]{64}', str(source_identity.get('sha256') or ''))
                or type(source_identity.get('size')) is not int or not 0 < source_identity['size'] <= min(FILES_IMPORT_MAX_BYTES, 10 * 1024 * 1024)):
                raise FilesFacadeError("provider preparation source identity is invalid", code="provider_unavailable")
            safe_source_identity = dict(source_identity)
        else:
            if set(source_identity) - {"resource_ref", "provider"}:
                raise FilesFacadeError("provider preparation source identity is invalid", code="provider_unavailable")
            safe_source_identity = {}
            for key in ("resource_ref", "provider"):
                if source_identity.get(key) is not None:
                    if not isinstance(source_identity[key], str) or not source_identity[key].strip() or len(source_identity[key].encode("utf-8")) > 16_384 or "\x00" in source_identity[key]:
                        raise FilesFacadeError("provider preparation source identity is invalid", code="provider_unavailable")
                    safe_source_identity[key] = source_identity[key].strip()
    mode = value.get("mode")
    if mode is not None and (not isinstance(mode, str) or mode.strip().lower() not in {"link", "embed"}):
        raise FilesFacadeError("provider preparation mode is invalid", code="provider_unavailable")
    for nested_key in ("action_receipt", "receipt"):
        if value.get(nested_key) is not None and not isinstance(value[nested_key], Mapping):
            raise FilesFacadeError("provider preparation receipt is invalid", code="provider_unavailable")
    provenance = value.get("provenance")
    safe_provenance = None
    if provenance is not None:
        if not isinstance(provenance, Mapping):
            raise FilesFacadeError("provider preparation provenance is invalid", code="provider_unavailable")
        provenance_allowed = {
            "canonical_root", "origin", "owner_subject_id", "workspace_id", "document_id", "document_path",
            "asset_name", "asset_id", "asset_digest", "references", "created_unix_ms", "layout_version", "media_dir",
        }
        if set(provenance) - provenance_allowed:
            raise FilesFacadeError("provider preparation provenance is invalid", code="provider_unavailable")
        origin = str(provenance.get("origin") or "").strip()
        if origin not in {"workspace", "loose", "selected_root"}:
            raise FilesFacadeError("provider preparation provenance is invalid", code="provider_unavailable")
        digest_value = str(provenance.get("asset_digest") or "")
        if not digest_value.startswith("sha256:") or len(digest_value) > 128:
            raise FilesFacadeError("provider preparation provenance is invalid", code="provider_unavailable")
        references = provenance.get("references")
        if references is not None and (
            not isinstance(references, (list, tuple))
            or any(not isinstance(item, str) or not item.strip() or len(item) > 1024 for item in references)
        ):
            raise FilesFacadeError("provider preparation provenance is invalid", code="provider_unavailable")
        version = provenance.get("layout_version", 1)
        if type(version) is not int or version not in (1, 2):
            raise FilesFacadeError("provider preparation provenance is invalid", code="provider_unavailable")
        safe_provenance = {"origin":origin, "asset_digest":digest_value, "layout_version":version}
    target_identity = value.get("target_identity")
    if not isinstance(target_identity, Mapping):
        raise FilesFacadeError("provider preparation target identity is invalid", code="provider_unavailable")
    if set(target_identity) - {"resource_key", "resource_ref", "kind", "course_id", "lesson_id", "assignment_id", "root_ref", "root_revision"}:
        raise FilesFacadeError("provider preparation target identity is invalid", code="provider_unavailable")
    safe_target: dict[str, str] = {}
    for key, raw in target_identity.items():
        if key == "root_revision":
            safe_target[key] = revision(raw, "root revision")
            continue
        if key == "resource_key":
            safe_target[key] = resource_key(raw, "target identity")
            continue
        if not isinstance(raw, str):
            raise FilesFacadeError("provider preparation target identity is invalid", code="provider_unavailable")
        token = raw
        if not token or len(token.encode("utf-8")) > 16_384 or "\x00" in token or "/" in token or "\\" in token:
            raise FilesFacadeError("provider preparation target identity is invalid", code="provider_unavailable")
        safe_target[key] = token
    if ("root_ref" in safe_target) != ("root_revision" in safe_target) or ("root_ref" in safe_target and safe_target.get("kind") != "host_document"):
        raise FilesFacadeError("provider root identity is invalid", code="provider_unavailable")
    insertion = value.get("insertion")
    if not isinstance(insertion, Mapping) or set(insertion) != {"format", "link_target", "label", "media_kind"}:
        raise FilesFacadeError("provider preparation insertion is invalid", code="provider_unavailable")
    if insertion.get("format") != "markdown":
        raise FilesFacadeError("provider preparation format is invalid", code="provider_unavailable")
    if any(not isinstance(insertion[key], str) for key in ("format", "link_target", "label", "media_kind")):
        raise FilesFacadeError("provider preparation insertion is invalid", code="provider_unavailable")
    safe_insertion = {key: insertion[key].strip() for key in ("format", "link_target", "label", "media_kind")}
    if any(not item or len(item.encode("utf-8")) > 512 or "\x00" in item for item in safe_insertion.values()):
        raise FilesFacadeError("provider preparation insertion is invalid", code="provider_unavailable")
    asset = value.get("asset")
    safe_asset = None
    if asset is not None:
        if not isinstance(asset, Mapping) or set(asset) - {"resource_key", "resource_ref", "revision", "mime_type", "name"}:
            raise FilesFacadeError("provider preparation asset is invalid", code="provider_unavailable")
        safe_asset = {}
        for key in ("resource_key", "resource_ref", "mime_type", "name"):
            if asset.get(key) is not None:
                if key == "resource_key":
                    safe_asset[key] = resource_key(asset[key], "asset resource key")
                    continue
                if not isinstance(asset[key], str):
                    raise FilesFacadeError("provider preparation asset is invalid", code="provider_unavailable")
                token = asset[key].strip()
                if not token or len(token.encode("utf-8")) > (16_384 if key == "resource_ref" else 512) or "\x00" in token or (key in {"resource_key", "resource_ref"} and ("/" in token or "\\" in token)):
                    raise FilesFacadeError("provider preparation asset is invalid", code="provider_unavailable")
                safe_asset[key] = token
        if "revision" in asset:
            safe_asset["revision"] = revision(asset["revision"], "asset revision")
    result = {"operation_id": operation_id, "preparation_receipt_id": preparation_id, "source_revision": source_revision, "target_identity": safe_target, "target_revision": target_revision, "insertion": safe_insertion}
    if returned_generation is not None:
        result["generation"] = returned_generation
    if source_digest is not None:
        result["source_digest"] = source_digest
    if request_digest is not None:
        result["request_digest"] = request_digest.strip()
    if asset_size is not None:
        result["asset_size"] = asset_size
    for identity_key in ("account_id", "workspace_id"):
        if value.get(identity_key) is not None:
            result[identity_key] = value[identity_key].strip()
    if safe_source_identity is not None:
        result["source_identity"] = safe_source_identity
    if mode is not None:
        result["mode"] = mode.strip().lower()
    if safe_asset is not None:
        result["asset"] = safe_asset
    if safe_provenance is not None:
        result["provenance"] = safe_provenance
    history = _public_action_receipt(
        value.get("action_receipt") if isinstance(value.get("action_receipt"), Mapping)
        else value.get("receipt") if isinstance(value.get("receipt"), Mapping)
        else value.get("history") if isinstance(value.get("history"), Mapping)
        else None
    )
    if history:
        result["history"] = history
    return result


def _public_operation(value: Mapping[str, Any] | None) -> dict[str, Any]:
    """Remove reservation-only routing metadata from operation responses."""
    if not isinstance(value, Mapping):
        return {}
    return {key: item for key, item in value.items() if not str(key).startswith("_")}


_DIRECTORY_KINDS = frozenset({"folder", "virtual_folder", "provider_root", "album"})


def _sorted_search_rows(
    rows: Sequence[tuple[str, ProviderResource]],
    sort: Mapping[str, Any],
) -> list[tuple[str, ProviderResource]]:
    """Apply the public Files sort to heterogeneous provider metadata."""

    key_name = str(sort.get("key") or "name")

    def value(item: tuple[str, ProviderResource]):
        _provider, row = item
        if key_name == "kind":
            return str(row.sort_kind or row.mime_type or row.kind or "").casefold()
        if key_name == "size":
            return (row.size is None, int(row.size or 0))
        if key_name == "modified":
            return (row.modified_unix_ms is None, int(row.modified_unix_ms or 0))
        return row.name.casefold()

    def ordered(group: list[tuple[str, ProviderResource]]) -> list[tuple[str, ProviderResource]]:
        group.sort(
            key=lambda item: (
                value(item),
                item[1].name.casefold(),
                item[0],
                item[1].origin_id,
            ),
            reverse=str(sort.get("direction") or "asc") == "desc",
        )
        return group

    material = list(rows)
    if bool(sort.get("directories_first", True)):
        directories = ordered([item for item in material if item[1].kind in _DIRECTORY_KINDS])
        leaves = ordered([item for item in material if item[1].kind not in _DIRECTORY_KINDS])
        return [*directories, *leaves]
    return ordered(material)


def _seal_cursor(payload: Mapping[str, Any]) -> str:
    sealed = encrypt(json.dumps(dict(payload), ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    if not sealed.startswith("enc:"):
        raise FilesFacadeError("provider cursor could not be sealed", code="provider_unavailable")
    return f"fc{FACADE_VERSION}.{sealed[4:]}"


def _open_cursor(
    token: str,
    *,
    context: ProviderContext,
    provider: str,
    parent_id: str,
    sort: Mapping[str, Any],
    sort_keys: Sequence[str],
    query: str,
    now_unix_ms: int | None = None,
) -> tuple[str | None, str | None]:
    prefix = f"fc{FACADE_VERSION}."
    raw = str(token or "").strip()
    if not raw.startswith(prefix):
        raise FilesFacadeError("provider cursor is malformed", code="stale_cursor")
    plaintext = decrypt("enc:" + raw[len(prefix) :])
    try:
        payload = json.loads(plaintext)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise FilesFacadeError("provider cursor is malformed", code="stale_cursor") from exc
    now = int(now_unix_ms if now_unix_ms is not None else time.time() * 1000)
    expected = {
        "v": FACADE_VERSION,
        "owner": context.owner_subject_id,
        "provider": provider,
        "parent": parent_id,
        "generation": int(context.policy_generation),
        "sort": dict(sort),
        "sort_keys": list(sort_keys),
        "query": query,
    }
    if any(payload.get(key) != value for key, value in expected.items()):
        raise FilesFacadeError("provider cursor is stale", code="stale_cursor")
    try:
        if int(payload["exp"]) <= now:
            raise FilesFacadeError("provider cursor expired", code="stale_cursor")
    except (KeyError, TypeError, ValueError) as exc:
        if isinstance(exc, FilesFacadeError):
            raise
        raise FilesFacadeError("provider cursor is malformed", code="stale_cursor") from exc
    value = payload.get("cursor")
    snapshot = payload.get("snapshot")
    return (
        str(value) if value is not None else None,
        str(snapshot) if snapshot is not None else None,
    )


_UNDO_MESSAGES = {
    "managed_undo_journal_missing": "This move can no longer be undone: its record is gone.",
    "resource_changed": "Something at the new place changed since the move, so nothing was touched.",
    "managed_undo_of_undo_unsupported": "An undo cannot be undone again.",
    "managed_undo_move_not_complete": "The move has not finished yet.",
    "managed_undo_requires_cross_root": "Only a move between two folders roots can be undone this way.",
    "managed_undo_journal_unreadable": "The record of this move cannot be read, so nothing was touched.",
    "managed_undo_unavailable_platform": "Undo is not available on this platform.",
    "managed_undo_unavailable": "This move cannot be undone right now; nothing was changed.",
}


class FilesFacade:
    _MANAGED_BATCH_READY = sys.platform == "linux"
    def __init__(
        self,
        providers: Sequence[FilesProvider] = (),
        *,
        place_repository: Any | None = None,
        operation_store: dict[str, dict[str, Any]] | None = None,
        attachment_targets: Mapping[str, Any] | None = None,
    ) -> None:
        self._providers: dict[str, FilesProvider] = {}
        self._attachment_targets = {str(key).strip().lower(): value for key, value in (attachment_targets or {}).items() if str(key).strip()}
        self._place_repository = place_repository
        # The route owns this mapping for the lifetime of the application.  It
        # is deliberately a receipt store only: provider bytes and identities
        # remain owned by their existing services.  Tests and embedded callers
        # can supply a store to make lost-response reconciliation deterministic.
        self._operation_store = operation_store if operation_store is not None else {}
        # Reservation happens synchronously before the first await. A shared
        # lock also makes the test/embedded dictionary fallback safe when two
        # coroutines submit the same operation at once.
        with _OPERATION_STORE_LOCKS_GUARD:
            self._operation_lock = _OPERATION_STORE_LOCKS.setdefault(id(self._operation_store), threading.RLock())
        for provider in providers:
            self.register(provider)

    def register(self, provider: FilesProvider) -> None:
        name = str(getattr(provider, "name", "") or "").strip().lower()
        if name not in PROVIDERS:
            raise FilesFacadeError("unsupported provider")
        if name in self._providers:
            raise FilesFacadeError("provider is already registered")
        self._providers[name] = provider

    @staticmethod
    def _bounded_id(value: Any, field: str) -> str:
        result = str(value or "").strip()
        if not result or len(result.encode("utf-8")) > 128:
            raise FilesFacadeError(f"{field} is invalid", code="invalid_resource_request")
        return result

    @staticmethod
    def _digest(value: Mapping[str, Any]) -> str:
        encoded = json.dumps(dict(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    def _load_operation(self, context: ProviderContext, operation_id: str) -> dict[str, Any] | None:
        getter = getattr(self._operation_store, "get_operation", None)
        if callable(getter):
            loaded = getter(owner_subject_id=context.owner_subject_id, operation_id=operation_id)
            if loaded is None:
                return None
            return {"owner": context.owner_subject_id, **dict(loaded)}
        loaded = self._operation_store.get((context.owner_subject_id, operation_id))
        return dict(loaded) if loaded is not None else None

    def _save_operation(self, context: ProviderContext, operation_id: str, digest: str, generation: int, receipt: Mapping[str, Any]) -> None:
        # The operation table is owner-scoped for generic receipts, but
        # attachment recovery also has a target workspace boundary. Keep that
        # binding private to the durable receipt so the public typed DTO stays
        # unchanged and a complete preparation cannot be replayed via another
        # workspace query.
        persisted = dict(receipt)
        if isinstance(receipt.get("target_identity"), Mapping) or receipt.get("_workspace_id"):
            persisted["_workspace_id"] = str(context.workspace_id or "default")
        recorder = getattr(self._operation_store, "record_operation", None)
        if callable(recorder):
            try:
                recorder(owner_subject_id=context.owner_subject_id, operation_id=operation_id, request_digest=digest, generation=generation, receipt=persisted)
            except FilePolicyError as exc:
                raise FilesFacadeError(str(exc), code=exc.code) from exc
            return
        key = (context.owner_subject_id, operation_id)
        self._operation_store[key] = {"owner": context.owner_subject_id, "digest": digest, "generation": generation, "receipt": persisted}

    def _reserve_operation(
        self,
        context: ProviderContext,
        operation_id: str,
        digest: str,
        generation: int,
        receipt: Mapping[str, Any],
    ) -> tuple[bool, dict[str, Any] | None]:
        """Reserve an owner+operation key before provider dispatch.

        The durable repository performs the insert under SQLite's write lock;
        the fallback uses the facade lock. A false result means a peer already
        owns the operation and its pending/completed receipt is returned.
        """
        reserver = getattr(self._operation_store, "reserve_operation", None)
        with self._operation_lock:
            if callable(reserver):
                try:
                    existing = reserver(
                        owner_subject_id=context.owner_subject_id,
                        operation_id=operation_id,
                        request_digest=digest,
                        generation=generation,
                        receipt=receipt,
                    )
                except FilePolicyError as exc:
                    raise FilesFacadeError(str(exc), code=exc.code) from exc
                return existing is None, (dict(existing["receipt"]) if existing else None)
            existing = self._operation_store.get((context.owner_subject_id, operation_id))
            if existing is None:
                existing = self._operation_store.get((context.owner_subject_id, operation_id))
            if existing is not None:
                if existing.get("owner") != context.owner_subject_id or existing.get("digest") != digest:
                    raise FilesFacadeError("operation id was already used for a different request", code="idempotency_conflict")
                return False, dict(existing.get("receipt") or {})
            self._operation_store[(context.owner_subject_id, operation_id)] = {
                "owner": context.owner_subject_id,
                "digest": digest,
                "generation": generation,
                "receipt": dict(receipt),
            }
        return True, None

    def _generation_current(self, context: ProviderContext) -> bool:
        getter = getattr(self._operation_store, "generation", None)
        if callable(getter):
            try:
                current = int(getter(context.owner_subject_id))
                return current == 0 or current == int(context.policy_generation)
            except TypeError:
                try:
                    current = int(getter())
                    return current == 0 or current == int(context.policy_generation)
                except Exception:
                    return False
            except Exception:
                return False
        return True

    @staticmethod
    def _provider_revision(entry: ProviderResource) -> Mapping[str, Any] | None:
        raw = getattr(entry, "revision", None)
        if isinstance(raw, Mapping) and raw.get("kind") and raw.get("value") is not None:
            return {"kind": str(raw["kind"]), "value": str(raw["value"])}
        candidate = entry.provenance.get("revision") if isinstance(entry.provenance, Mapping) else None
        if isinstance(candidate, Mapping) and candidate.get("kind") and candidate.get("value") is not None:
            return {"kind": str(candidate["kind"]), "value": str(candidate["value"])}
        return None

    @classmethod
    def _check_revision(cls, entry: ProviderResource, expected: Mapping[str, Any] | None) -> None:
        if expected is None:
            return
        if not isinstance(expected, Mapping) or not expected.get("kind") or expected.get("value") is None:
            raise FilesFacadeError("resource revision is invalid", code="invalid_resource_request")
        current = cls._provider_revision(entry)
        # Providers that do not expose a normalized revision retain their
        # existing CAS/action machinery.  Providers that do expose one must
        # never let a stale drag silently overwrite current state.
        if current is not None and (
            str(current.get("kind")) != str(expected.get("kind"))
            or str(current.get("value")) != str(expected.get("value"))
        ):
            raise FilesFacadeError("resource revision is stale", code="resource_changed")

    @staticmethod
    def _import_relative_parts(relative_path: Any, name: str) -> tuple[str, ...]:
        """Validate a browser supplied relative name without normalizing it.

        The destination remains an opaque ResourceRef.  These components are
        labels used to walk that already authorized tree; they are never
        joined into a host path by the facade.
        """
        if relative_path is not None and not isinstance(relative_path, str):
            raise FilesFacadeError("import relative path is invalid", code="invalid_resource_request")
        raw = str(relative_path if relative_path is not None else name).strip().replace("\\", "/")
        if (
            not raw
            or len(raw.encode("utf-8")) > MAX_IMPORT_RELATIVE_BYTES
            or raw.startswith("/")
            or raw.endswith("/")
            or raw.startswith("//")
            or (len(raw) >= 2 and raw[1] == ":")
            or "\x00" in raw
        ):
            raise FilesFacadeError("import relative path is invalid", code="invalid_resource_request")
        parts = tuple(raw.split("/"))
        if (
            not parts
            or len(parts) > MAX_IMPORT_RELATIVE_DEPTH
            or any(not part or part in {".", ".."} or "/" in part or "\\" in part or "\x00" in part for part in parts)
            or any(len(part.encode("utf-8")) > 240 for part in parts)
            or parts[-1] != name
        ):
            raise FilesFacadeError("import relative path is invalid", code="invalid_resource_request")
        return parts

    @staticmethod
    def _import_child_operation_id(operation_id: str, parent_stable_id: str, depth: int, name: str) -> str:
        material = f"{operation_id}\0{parent_stable_id}\0{depth}\0{name}".encode("utf-8")
        suffix = hashlib.sha256(material).hexdigest()[:32]
        return f"{operation_id[:64]}-dir-{depth}-{suffix}"[:128]

    @staticmethod
    def _validate_import_directory(entry: ProviderResource, *, expected_origin: str | None = None) -> None:
        if (
            expected_origin is not None and entry.origin_id != expected_origin
        ) or entry.kind not in _DIRECTORY_KINDS or "children" not in entry.capabilities or "write" not in entry.capabilities:
            raise FilesFacadeError("import directory is unavailable", code="resource_unavailable")

    async def _ensure_import_parent(
        self,
        context: ProviderContext,
        *,
        provider: FilesProvider,
        parent: ResourceRef,
        directory_parts: Sequence[str],
        operation_id: str,
    ) -> ResourceRef:
        """Walk/create nested folders through the provider contract.

        Every new folder gets a deterministic, owner-scoped reservation before
        the provider mutation. Existing folders are re-statted and symlinks or
        non-writable virtual rows are rejected.
        """
        current = parent
        for depth, name in enumerate(directory_parts):
            if not self._generation_current(context):
                raise FilesFacadeError("file policy generation changed", code="policy_generation_changed")
            current_entry = await provider.stat(context, origin_id=current.origin_id)
            self._validate_import_directory(current_entry, expected_origin=current.origin_id)
            page = await provider.children(
                context,
                parent_origin_id=current.origin_id,
                cursor=None,
                snapshot=None,
                limit=MAX_PAGE_SIZE,
                sort={"key": "name", "direction": "asc", "directories_first": True},
                query=name,
            )
            matches = [entry for entry in page.entries if entry.name == name]
            if len(matches) > 1:
                raise FilesFacadeError("import directory identity is ambiguous", code="resource_changed")
            if matches:
                child = matches[0]
                self._validate_import_directory(child)
                checked = await provider.stat(context, origin_id=child.origin_id)
                self._validate_import_directory(checked, expected_origin=child.origin_id)
                if checked.parent_origin_id is not None and checked.parent_origin_id != current.origin_id:
                    raise FilesFacadeError("import directory ancestry is invalid", code="provider_unavailable")
                current = issue_resource_ref(
                    owner_subject_id=context.owner_subject_id,
                    provider=parent.provider,
                    origin_id=checked.origin_id,
                    kind=checked.kind,
                    capabilities=checked.capabilities,
                    policy_generation=context.policy_generation,
                    location_id=checked.location_id,
                    workspace_id=checked.workspace_id,
                    parent_stable_id=current.stable_id,
                )
                continue

            creator = getattr(provider, "create_directory", None)
            if not callable(creator):
                raise FilesFacadeError("provider cannot create import directories", code="unsupported_provider_kind")
            child_operation = self._import_child_operation_id(operation_id, current.stable_id, depth, name)
            child_digest = self._digest({
                "kind": "import-directory",
                "account_id": context.owner_subject_id,
                "workspace_id": context.workspace_id,
                "generation": int(context.policy_generation),
                "operation_id": operation_id,
                "parent_resource": current.stable_id,
                "depth": depth,
                "name": name,
            })
            pending = {
                "operation_id": child_operation,
                "generation": int(context.policy_generation),
                "state": "pending",
                "items": [{"item_id": f"directory-{depth}", "outcome": "pending", "code": "operation_in_progress"}],
                "_request_kind": "import-directory",
                "_item_ids": [f"directory-{depth}"],
                "_provider_names": [str(getattr(provider, "name", ""))],
                "_workspace_id": str(context.workspace_id or "default"),
            }
            owner, previous = self._reserve_operation(
                context, child_operation, child_digest, context.policy_generation, pending,
            )
            child_resource: ProviderResource | None = None
            if owner:
                try:
                    child_resource = await creator(
                        context,
                        parent_origin_id=current.origin_id,
                        name=name,
                        operation_id=child_operation,
                        request_digest=child_digest,
                        parent_revision=self._provider_revision(current_entry),
                        collision="reuse",
                    )
                except TypeError as error:
                    if not any(field in str(error) for field in ("request_digest", "parent_revision", "collision")):
                        raise
                    child_resource = await creator(context, parent_origin_id=current.origin_id, name=name, operation_id=child_operation)
                if not isinstance(child_resource, ProviderResource):
                    raise FilesFacadeError("provider returned an invalid import directory", code="provider_unavailable")
                self._validate_import_directory(child_resource)
                checked = await provider.stat(context, origin_id=child_resource.origin_id)
                self._validate_import_directory(checked, expected_origin=child_resource.origin_id)
                if checked.parent_origin_id is not None and checked.parent_origin_id != current.origin_id:
                    raise FilesFacadeError("import directory ancestry is invalid", code="provider_unavailable")
                public = self._resource(context, parent.provider, checked, parent_stable_id=current.stable_id)
                self._save_operation(
                    context,
                    child_operation,
                    child_digest,
                    context.policy_generation,
                    {
                        "operation_id": child_operation,
                        "generation": int(context.policy_generation),
                        "state": "complete",
                        "items": [{
                            "item_id": f"directory-{depth}",
                            "outcome": "committed",
                            "resource_ref": public["ref"],
                        }],
                        "_request_kind": "import-directory",
                        "_workspace_id": str(context.workspace_id or "default"),
                    },
                )
            else:
                prior_item = (previous or {}).get("items", [{}])[0]
                prior_ref = prior_item.get("resource_ref") if isinstance(prior_item, Mapping) else None
                if not prior_ref:
                    status = getattr(provider, "operation_status", None)
                    recovered = None
                    if callable(status):
                        try:
                            recovered = await status(context, operation_id=child_operation)
                        except Exception:
                            recovered = None
                    if isinstance(recovered, Mapping) and recovered.get("state") == "complete" and recovered.get("_request_digest") == child_digest:
                        recovered_item = (recovered.get("items") or [{}])[0]
                        if isinstance(recovered_item, Mapping) and recovered_item.get("resource_ref"):
                            prior_ref = recovered_item["resource_ref"]
                            self._save_operation(
                                context,
                                child_operation,
                                child_digest,
                                context.policy_generation,
                                {
                                    "operation_id": child_operation,
                                    "generation": int(context.policy_generation),
                                    "state": "complete",
                                    "items": [{
                                        "item_id": f"directory-{depth}",
                                        "outcome": "committed",
                                        "resource_ref": prior_ref,
                                    }],
                                    "_request_kind": "import-directory",
                                    "_workspace_id": str(context.workspace_id or "default"),
                                },
                            )
                if prior_ref:
                    _prior_provider, prior = self._provider_for_ref(context, prior_ref, capability="stat")
                    if _prior_provider is not provider:
                        raise FilesFacadeError("import directory provider changed", code="provider_unavailable")
                    child_resource = await provider.stat(context, origin_id=prior.origin_id)
                else:
                    raise FilesFacadeError("import directory operation is still pending", code="operation_pending")
                self._validate_import_directory(child_resource)
            current = issue_resource_ref(
                owner_subject_id=context.owner_subject_id,
                provider=parent.provider,
                origin_id=child_resource.origin_id,
                kind=child_resource.kind,
                capabilities=child_resource.capabilities,
                policy_generation=context.policy_generation,
                location_id=child_resource.location_id,
                workspace_id=child_resource.workspace_id,
                parent_stable_id=current.stable_id,
            )
        return current

    async def resolve_resource(self, context: ProviderContext, *, resource_key: Mapping[str, Any] | str) -> dict[str, Any]:
        """Resolve a stable Copal/resource key through its provider adapter.

        A key is a lookup request, never a grant.  Only providers that already
        own a stable-key mapping may participate; host paths are intentionally
        not reconstructed from a key.
        """
        if isinstance(resource_key, Mapping):
            key = dict(resource_key)
        else:
            key = {"resource_id": str(resource_key or "").strip()}
        provider_name = str(key.get("provider") or "").strip().lower()
        provider = self._providers.get(provider_name)
        if provider is None:
            raise FilesFacadeError("resource key is unavailable", code="resource_unavailable")
        resolver = getattr(provider, "resolve_resource_key", None)
        if not callable(resolver):
            raise FilesFacadeError("resource key is unavailable", code="resource_unavailable")
        entry = await resolver(context, resource_key=key)
        if not isinstance(entry, ProviderResource):
            raise FilesFacadeError("resource key is unavailable", code="resource_unavailable")
        current = await provider.stat(context, origin_id=entry.origin_id)
        if current.origin_id != entry.origin_id:
            raise FilesFacadeError("resource key is unavailable", code="resource_unavailable")
        return self._resource(context, provider_name, current, parent_stable_id=None)

    async def resolve_binder_folder(self, context: ProviderContext, *, source: Mapping[str, Any]) -> dict[str, Any]:
        provider = self._providers.get("copal")
        resolver = getattr(provider, "resolve_binder_folder", None)
        if not callable(resolver):
            raise FilesFacadeError("Binder provider is unavailable", code="provider_unavailable")
        entry = await resolver(context, workspace=source["workspaceId"], corpus=source["corpus"], root=tuple(source["root"]))
        return self._resource(context, "copal", entry)

    async def describe_binder_folder(self, context: ProviderContext, *, resource_ref: str) -> dict[str, Any]:
        provider, ref = self._provider_for_ref(context, resource_ref, capability="stat")
        resolver = getattr(provider, "describe_binder_folder", None)
        if ref.provider != "copal" or not callable(resolver):
            raise FilesFacadeError("Choose an active folder under Workspace collections.", code="resource_unavailable")
        return await resolver(context, origin_id=ref.origin_id)

    async def transfer_root_requirement(self, context: ProviderContext, *, generation: int,
                                        sources: Sequence[Mapping[str, Any]], destination_ref: str) -> dict[str, bool]:
        """Classify a captured move without reserving or mutating it."""
        if int(context.policy_generation) != _strict_nonnegative_int(generation, "generation") or not self._generation_current(context):
            raise FilesFacadeError("File policy generation changed", code="policy_generation_changed")
        destination_provider, destination = self._provider_for_ref(context, destination_ref, capability="write")
        destination_entry = await destination_provider.stat(context, origin_id=destination.origin_id)
        if destination_entry.origin_id != destination.origin_id or destination_entry.kind not in _DIRECTORY_KINDS or "write" not in destination_entry.capabilities:
            raise FilesFacadeError("Transfer destination is unavailable", code="resource_unavailable")
        required = False
        destination_required = False
        for source in sources:
            provider, ref = self._provider_for_ref(context, str(source.get("resource_ref") or ""), capability="stat")
            if provider is not destination_provider:
                raise FilesFacadeError("Cross-provider transfer is unavailable", code="unsupported_provider_kind")
            entry = await provider.stat(context, origin_id=ref.origin_id)
            if entry.origin_id != ref.origin_id or entry.kind != ref.kind or not ({"move", "write"} & set(entry.capabilities)):
                raise FilesFacadeError("Transfer source is unavailable", code="resource_unavailable")
            self._check_revision(entry, source.get("expected_revision"))
            classifier = getattr(provider, "managed_root_required", None)
            if callable(classifier):
                item_required = bool(classifier(context, source_origin_id=ref.origin_id, destination_origin_id=destination.origin_id,
                                                source_is_directory=entry.kind in _DIRECTORY_KINDS))
                selected_classifier = getattr(provider, "selected_folder_required", None)
                selected_directory = entry.kind in _DIRECTORY_KINDS and callable(selected_classifier) and selected_classifier(context, source_origin_id=ref.origin_id, source_is_directory=True)
                folder_cross_root = getattr(provider, "managed_cross_root_folder_available", None)
                cross_folder_available = (entry.kind in _DIRECTORY_KINDS and callable(folder_cross_root)
                    and folder_cross_root(context, source_origin_id=ref.origin_id, destination_origin_id=destination.origin_id))
                if item_required and entry.kind in _DIRECTORY_KINDS and not selected_directory and not cross_folder_available:
                    raise FilesFacadeError("Managed folder moves are unavailable", code="unsupported_operation")
                required |= item_required
            # A managed document leaving its root needs a second, explicitly chosen destination root.
            destination_classifier = getattr(provider, "managed_destination_root_required", None)
            if callable(destination_classifier):
                destination_required |= bool(destination_classifier(context, source_origin_id=ref.origin_id,
                    destination_origin_id=destination.origin_id, source_is_directory=entry.kind in _DIRECTORY_KINDS))
        if not self._generation_current(context):
            raise FilesFacadeError("File policy generation changed", code="policy_generation_changed")
        return {"root_required": required, "destination_root_required": destination_required}

    async def transfer_resources(
        self,
        context: ProviderContext,
        *,
        operation_id: str,
        kind: str,
        sources: Sequence[Mapping[str, Any]],
        destination_ref: str,
        collision: str = "fail",
        generation: int | None = None,
        managed_root: Mapping[str, Any] | None = None,
        destination_root: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Move/copy bounded source refs to an authorized folder ref.

        The provider receives origin IDs only after both ends have been
        resolved and re-statted under this immutable account/generation.
        Receipts are replay-safe and preserve partial outcomes.
        """
        operation = self._bounded_id(operation_id, "operation_id")
        requested_generation = _strict_nonnegative_int(
            generation if generation is not None else context.policy_generation,
            "generation",
        )
        if int(context.policy_generation) != requested_generation:
            raise FilesFacadeError("file policy generation is stale", code="resource_ref_stale")
        normalized_kind = str(kind or "").strip().lower()
        if normalized_kind not in {"move", "copy"}:
            raise FilesFacadeError("transfer kind is unsupported", code="unsupported_operation")
        normalized_collision = str(collision or "fail").strip().lower()
        if normalized_collision not in {"fail", "rename"}:
            raise FilesFacadeError("transfer collision policy is unsupported", code="unsupported_operation")
        if not isinstance(sources, Sequence) or isinstance(sources, (str, bytes)) or not sources or len(sources) > 200:
            raise FilesFacadeError("transfer source count is outside the allowed bound", code="invalid_resource_request")
        if managed_root is not None and (normalized_kind != "move" or (len(sources) != 1 and not self._MANAGED_BATCH_READY)):
            raise FilesFacadeError("Choose one loose document for this root", code="unsupported_operation")
        if destination_root is not None and (normalized_kind != "move" or len(sources) != 1):
            raise FilesFacadeError("Choose one document for a destination root", code="unsupported_operation")
        normalized_sources: list[dict[str, Any]] = []
        seen_items: set[str] = set()
        seen_refs: set[str] = set()
        for source in sources:
            if not isinstance(source, Mapping):
                raise FilesFacadeError("transfer source is invalid", code="invalid_resource_request")
            item_id = self._bounded_id(source.get("item_id"), "item_id")
            ref_token = str(source.get("resource_ref") or source.get("ref") or "").strip()
            if not ref_token:
                raise FilesFacadeError("transfer source reference is required", code="invalid_resource_request")
            if item_id in seen_items or ref_token in seen_refs:
                raise FilesFacadeError("transfer contains duplicate items", code="invalid_resource_request")
            seen_items.add(item_id)
            seen_refs.add(ref_token)
            normalized_sources.append({
                "item_id": item_id,
                "resource_ref": ref_token,
                "expected_revision": source.get("expected_revision"),
            })
        digest_payload = {
            "generation": int(context.policy_generation), "kind": normalized_kind,
            "sources": normalized_sources, "destination_ref": str(destination_ref or ""),
            "collision": normalized_collision,
        }
        if managed_root is not None:
            if set(managed_root) - {"resource_ref", "expected_revision"} or not str(managed_root.get("resource_ref") or "").strip():
                raise FilesFacadeError("Selected root is invalid", code="invalid_resource_request")
            digest_payload["managed_root"] = dict(managed_root)
        if destination_root is not None:
            if set(destination_root) - {"resource_ref", "expected_revision"} or not str(destination_root.get("resource_ref") or "").strip():
                raise FilesFacadeError("Destination root is invalid", code="invalid_resource_request")
            digest_payload["destination_root"] = dict(destination_root)
        digest = self._digest(digest_payload)
        batch_preparing = managed_root is not None and len(normalized_sources) > 1
        stored = self._load_operation(context, operation) if batch_preparing else None
        if stored and (stored.get("digest") != digest or int(stored.get("generation", -1)) != int(context.policy_generation)):
            raise FilesFacadeError("Operation identity changed", code="idempotency_conflict")
        if stored and (stored.get("receipt") or {}).get("_managed_batch_ready") is not False:
            return await self.operation_receipt(context, operation_id=operation)
        resume_historical = bool(stored)

        def transfer_ref(token: str, capability: str) -> tuple[FilesProvider, ResourceRef]:
            if not resume_historical:
                return self._provider_for_ref(context, token, capability=capability)
            try:
                ref = resolve_resource_ref_for_reissue(token, expected_owner_subject_id=context.owner_subject_id,
                                                       required_capability=capability)
                if ref.provider != "host" or ref.policy_generation != int(context.policy_generation):
                    raise ValueError("historical identity changed")
                provider = self._providers.get(ref.provider)
                if provider is None:
                    raise ValueError("provider unavailable")
                return provider, ref
            except Exception as exc:
                raise FilesFacadeError("Transfer identity is unavailable", code="resource_unavailable") from exc

        selected_root = None
        if managed_root is not None:
            # A historical token may identify a reservation but cannot grant
            # a new selected-folder mutation.
            root_provider, root_ref = self._provider_for_ref(context, str(managed_root["resource_ref"]), capability="write")
            source_provider, source_ref = transfer_ref(normalized_sources[0]["resource_ref"], "stat")
            classifier = getattr(source_provider, "selected_folder_required", None)
            selected_profile = len(normalized_sources) == 1 and callable(classifier) and classifier(context, source_origin_id=source_ref.origin_id, source_is_directory=source_ref.kind in _DIRECTORY_KINDS)
            resolver = getattr(root_provider, "authorize_selected_folder" if selected_profile else "authorize_managed_root", None)
            if root_ref.provider != "host" or not callable(resolver):
                raise FilesFacadeError("Selected root is unavailable", code="resource_unavailable")
            selected_root = await resolver(context, origin_id=root_ref.origin_id, expected_revision=managed_root.get("expected_revision"))
        authorized_destination = None
        if destination_root is not None:
            # Same authorisation as a managed root, through the provider's destination rule.
            destination_root_provider, destination_root_ref = self._provider_for_ref(context, str(destination_root["resource_ref"]), capability="write")
            destination_resolver = getattr(destination_root_provider, "authorize_destination_root", None)
            if destination_root_ref.provider != "host" or not callable(destination_resolver):
                raise FilesFacadeError("Destination root is unavailable", code="resource_unavailable")
            authorized_destination = await destination_resolver(context, origin_id=destination_root_ref.origin_id,
                                                                expected_revision=destination_root.get("expected_revision"))
        pending_receipt = {
            "operation_id": operation, "generation": int(context.policy_generation),
            "state": "pending",
            "items": [{"item_id": item["item_id"], "outcome": "pending", "code": "operation_in_progress"} for item in normalized_sources],
        }
        if batch_preparing:
            pending_receipt["_managed_batch_ready"] = False
        # Resolve the destination before reserving so the durable pending row
        # records exactly which provider may reconcile it.
        destination_provider, destination = transfer_ref(destination_ref, "write")
        pending_receipt["_provider_names"] = [str(getattr(destination_provider, "name", ""))]
        pending_receipt["_request_kind"] = "transfer"
        pending_receipt["_item_ids"] = [item["item_id"] for item in normalized_sources]
        owner, previous_receipt = self._reserve_operation(context, operation, digest, context.policy_generation, pending_receipt)
        if not owner and not (batch_preparing and isinstance(previous_receipt, Mapping) and previous_receipt.get("_managed_batch_ready") is False):
            return _public_operation(previous_receipt or pending_receipt)

        try:
            if not self._generation_current(context):
                raise FilesFacadeError("file policy generation changed", code="policy_generation_changed")
            destination_provider, destination = transfer_ref(destination_ref, "write")
            destination_entry = await destination_provider.stat(context, origin_id=destination.origin_id)
            if not self._generation_current(context):
                raise FilesFacadeError("file policy generation changed", code="policy_generation_changed")
            if destination_entry.origin_id != destination.origin_id or destination_entry.kind != destination.kind or destination_entry.kind not in _DIRECTORY_KINDS or "children" not in destination_entry.capabilities or "write" not in destination_entry.capabilities:
                raise FilesFacadeError("transfer destination is unavailable", code="resource_unavailable")
        except FilesFacadeError as error:
            if batch_preparing:
                try:
                    self._operation_store.abort_managed_transfer_batch(owner_subject_id=context.owner_subject_id, operation_id=operation, claim_tokens={})
                except Exception:
                    return _public_operation(previous_receipt or pending_receipt)
            if error.code == "policy_generation_changed":
                failed_items = [{"item_id": normalized_sources[0]["item_id"], "outcome": "stale", "code": error.code}]
                failed_items.extend({"item_id": item["item_id"], "outcome": "pending", "code": error.code} for item in normalized_sources[1:])
            else:
                failed_items = [{"item_id": item["item_id"], "outcome": "denied", "code": error.code} for item in normalized_sources]
            failed = {"operation_id": operation, "generation": int(context.policy_generation), "state": "partial", "items": failed_items}
            self._save_operation(context, operation, digest, context.policy_generation, failed)
            return failed
        except Exception:
            if batch_preparing:
                try:
                    self._operation_store.abort_managed_transfer_batch(owner_subject_id=context.owner_subject_id, operation_id=operation, claim_tokens={})
                except Exception:
                    return _public_operation(previous_receipt or pending_receipt)
            failed = {"operation_id": operation, "generation": int(context.policy_generation), "state": "partial", "items": [{"item_id": item["item_id"], "outcome": "failed", "code": "provider_unavailable"} for item in normalized_sources]}
            self._save_operation(context, operation, digest, context.policy_generation, failed)
            return failed
        if batch_preparing:
            claim_tokens: dict[str, str] = {}
            try:
                prepare = getattr(destination_provider, "prepare_managed_loose_item", None)
                if destination_provider is not root_provider or not callable(prepare) or not getattr(destination_provider, "_LOOSE_MANAGED_MOVE_READY", False):
                    raise FilesFacadeError("Managed batch is unavailable", code="unsupported_operation")
                frozen = []
                reserved_targets: set[str] = set()
                for source in normalized_sources:
                    provider, ref = transfer_ref(source["resource_ref"], "stat")
                    if provider is not destination_provider:
                        raise FilesFacadeError("Managed batch providers differ", code="unsupported_provider_kind")
                    entry = await provider.stat(context, origin_id=ref.origin_id)
                    if entry.origin_id != ref.origin_id or entry.kind != ref.kind or entry.kind != "file" or "move" not in entry.capabilities:
                        raise FilesFacadeError("Managed batch source is unavailable", code="resource_unavailable")
                    self._check_revision(entry, source.get("expected_revision"))
                    item = await prepare(context, source_origin_id=ref.origin_id, destination_origin_id=destination.origin_id,
                        expected_revision=source.get("expected_revision"), operation_id=operation, item_id=source["item_id"],
                        collision=normalized_collision, managed_root=selected_root, reserved_targets=reserved_targets)
                    frozen.append((source["item_id"], item["plan"]))
                for item_id, plan in frozen:
                    claim = self._operation_store.claim_managed_transfer_item(owner_subject_id=context.owner_subject_id,
                        operation_id=operation, item_id=item_id, native_operation_id=plan["native_operation_id"], generation=context.policy_generation)
                    if not claim.get("acquired"):
                        raise FilesFacadeError("Managed batch is already executing", code="operation_pending")
                    claim_tokens[item_id] = str(claim["claim_token"])
                for _item_id, plan in frozen:
                    await destination_provider.verify_managed_loose_item_proofs(context, plan)
                renewed = await root_provider.authorize_managed_root(context, origin_id=root_ref.origin_id, expected_revision=managed_root.get("expected_revision"))
                if dict(renewed) != dict(selected_root):
                    raise FilesFacadeError("Selected root changed", code="resource_changed")
                self._operation_store.activate_managed_transfer_batch(owner_subject_id=context.owner_subject_id,
                    operation_id=operation, generation=context.policy_generation, claim_tokens=claim_tokens)
            except Exception as error:
                try:
                    self._operation_store.abort_managed_transfer_batch(owner_subject_id=context.owner_subject_id, operation_id=operation, claim_tokens=claim_tokens)
                except Exception:
                    return _public_operation(previous_receipt or pending_receipt)
                code = str(getattr(error, "code", "provider_unavailable"))
                failed = {"operation_id":operation, "generation":int(context.policy_generation), "state":"partial",
                          "items":[{"item_id":item["item_id"], "outcome":"failed", "code":code} for item in normalized_sources]}
                self._save_operation(context, operation, digest, context.policy_generation, failed)
                return failed
        items: list[dict[str, Any]] = []
        for source in normalized_sources:
            item = {"item_id": source["item_id"], "outcome": "failed", "code": "provider_unavailable"}
            try:
                if not self._generation_current(context):
                    item["outcome"] = "stale"
                    item["code"] = "policy_generation_changed"
                    items.append(item)
                    for pending_source in normalized_sources[len(items):]:
                        items.append({"item_id": pending_source["item_id"], "outcome": "pending", "code": "policy_generation_changed"})
                    break
                # Resolve the sealed identity first, then enforce the exact
                # kind capability after the provider re-stats it.  Host's
                # legacy file rows advertise download (the read contract)
                # while writable directories advertise write, so requiring a
                # synthetic `copy` capability here would lie to consumers.
                source_provider, source_ref = transfer_ref(source["resource_ref"], "stat")
                current = await source_provider.stat(context, origin_id=source_ref.origin_id)
                if not self._generation_current(context):
                    raise FilesFacadeError("file policy generation changed", code="policy_generation_changed")
                if current.origin_id != source_ref.origin_id or current.kind != source_ref.kind or current.kind not in RESOURCE_KINDS:
                    raise FilesFacadeError("transfer source is unavailable", code="resource_unavailable")
                if normalized_kind == "move":
                    allowed = ("write" in current.capabilities) if current.kind in _DIRECTORY_KINDS else ("move" in current.capabilities)
                else:
                    allowed = bool({"read", "download", "open", "copy"}.intersection(current.capabilities))
                if not allowed:
                    raise FilesFacadeError("resource capability is unavailable", code="resource_unavailable")
                self._check_revision(current, source.get("expected_revision"))
                if source_provider is not destination_provider or source_ref.provider != destination.provider:
                    raise FilesFacadeError("cross-provider transfer is unsupported", code="unsupported_provider_kind")
                if source_ref.stable_id == destination.stable_id:
                    raise FilesFacadeError("resource cannot be moved into itself", code="invalid_destination")
                if normalized_kind == "move" and current.kind in _DIRECTORY_KINDS:
                    boundary = None
                    cross_available = getattr(source_provider, "managed_cross_root_folder_available", None)
                    if (authorized_destination is not None and selected_root is not None
                            and source_provider is destination_root_provider and callable(cross_available)
                            and cross_available(context, source_origin_id=source_ref.origin_id, destination_origin_id=destination.origin_id)):
                        from src.openclank.files_host_provider import host_origin_for_path
                        selected = authorized_destination.get("selected_folder")
                        bound_path = selected.get("path") if isinstance(selected, Mapping) else authorized_destination.get("root")
                        if not isinstance(bound_path, str) or host_origin_for_path(bound_path) != destination_root_ref.origin_id:
                            raise FilesFacadeError("Destination root identity changed", code="resource_unavailable")
                        boundary = destination_root_ref.origin_id
                    parent_origin = None if boundary == destination.origin_id else destination_entry.parent_origin_id
                    seen: set[str] = set()
                    while parent_origin:
                        if parent_origin in seen:
                            raise FilesFacadeError("resource ancestry is invalid", code="provider_unavailable")
                        seen.add(parent_origin)
                        if parent_origin == source_ref.origin_id:
                            raise FilesFacadeError("resource cannot be moved into a descendant", code="invalid_destination")
                        if (selected_root is not None and parent_origin == root_ref.origin_id) or parent_origin == boundary:
                            break
                        parent_entry = await destination_provider.stat(context, origin_id=parent_origin)
                        parent_origin = parent_entry.parent_origin_id
                        if len(seen) > MAX_REVEAL_ANCESTORS:
                            raise FilesFacadeError("resource ancestry is invalid", code="provider_unavailable")
                transfer = getattr(source_provider, "transfer", None)
                if not callable(transfer):
                    raise FilesFacadeError("provider transfer is unavailable", code="unsupported_provider_kind")
                transfer_args = dict(
                    source_origin_id=source_ref.origin_id,
                    destination_origin_id=destination.origin_id,
                    operation=normalized_kind,
                    collision=normalized_collision,
                    expected_revision=source.get("expected_revision"),
                    item_id=source["item_id"],
                    operation_id=operation,
                )
                if selected_root is not None:
                    if source_provider is not root_provider:
                        raise FilesFacadeError("Selected root does not match this provider", code="resource_unavailable")
                    transfer_args["managed_root"] = selected_root
                    if batch_preparing:
                        transfer_args["prepared_managed"] = True
                if authorized_destination is not None:
                    if source_provider is not destination_root_provider:
                        raise FilesFacadeError("Destination root does not match this provider", code="resource_unavailable")
                    transfer_args["destination_root"] = authorized_destination
                result = await transfer(context, **transfer_args)
                if not self._generation_current(context):
                    raise FilesFacadeError("file policy generation changed", code="policy_generation_changed")
                provider_history = None
                if isinstance(result, ProviderResource):
                    updated = result
                    outcome = "committed"
                    code = None
                    provider_receipt = None
                elif isinstance(result, Mapping):
                    provider_item = None
                    if "items" in result:
                        validated = _validated_receipt(
                            result,
                            operation_id=operation,
                            item_id=source["item_id"],
                            expected_item_ids=[source["item_id"]],
                        )
                        provider_item = validated["items"][0]
                    outcome = str((provider_item or result).get("outcome") or "committed").lower()
                    if outcome not in {"committed", "unchanged", "failed", "denied", "conflict", "stale", "pending"}:
                        outcome = "failed"
                    code = str((provider_item or result).get("code") or "") or None
                    updated = result.get("resource")
                    provider_receipt = (provider_item or result).get("receipt_id")
                    provider_history = result.get("action_receipt") if isinstance(result.get("action_receipt"), Mapping) else result.get("history") if isinstance(result.get("history"), Mapping) else None
                else:
                    raise FilesFacadeError("provider returned an invalid transfer result", code="provider_unavailable")
                item["outcome"] = outcome
                if code:
                    item["code"] = code
                else:
                    item.pop("code", None)
                if isinstance(updated, ProviderResource):
                    item["resource_key"] = stable_resource_id(owner_subject_id=context.owner_subject_id, provider=destination.provider, origin_id=updated.origin_id)
                    item["resource_ref"] = self._resource(context, destination.provider, updated, parent_stable_id=destination.stable_id)["ref"]
                    revision = self._provider_revision(updated)
                    if revision is not None:
                        item["revision"] = dict(revision)
                    history = _public_action_receipt(updated.action_receipt)
                    if history:
                        item["history"] = history
                        if history.get("receipt_id") or history.get("action_id"):
                            item["receipt_id"] = history.get("receipt_id") or history.get("action_id")
                if provider_receipt:
                    item["receipt_id"] = str(provider_receipt)
                history = _public_action_receipt(provider_history)
                if history:
                    item["history"] = history
            except FilesFacadeError as error:
                item["outcome"] = {
                    "resource_changed": "conflict",
                    "resource_ref_stale": "stale",
                    "resource_unavailable": "denied",
                    "invalid_destination": "denied",
                }.get(error.code, "failed")
                item["code"] = error.code
            except Exception:
                item["outcome"] = "failed"
                item["code"] = "provider_unavailable"
            items.append(item)
            if item.get("code") in {"resource_ref_stale", "policy_generation_changed"}:
                # A generation reset invalidates the queued command. Preserve
                # explicit pending outcomes for the unattempted tail so a
                # consumer can reconcile without assuming rollback.
                for pending_source in normalized_sources[len(items):]:
                    items.append({"item_id": pending_source["item_id"], "outcome": "pending", "code": "policy_generation_changed"})
                break
        outcomes = {str(item["outcome"]) for item in items}
        state = "complete" if outcomes.issubset({"committed", "unchanged"}) else "partial"
        receipt = {"operation_id": operation, "generation": int(context.policy_generation), "state": state, "items": items}
        self._save_operation(context, operation, digest, context.policy_generation, receipt)
        return receipt

    # Short backend spelling retained for provider/consumer adapters that use
    # the operation family name rather than the HTTP client method name.
    transfer = transfer_resources

    async def operation_receipt(self, context: ProviderContext, *, operation_id: str) -> dict[str, Any]:
        operation = self._bounded_id(operation_id, "operation_id")
        stored = self._load_operation(context, operation)
        if stored is None or stored.get("owner") != context.owner_subject_id:
            raise FilesFacadeError("operation is unavailable", code="resource_unavailable")
        receipt = dict(stored["receipt"])
        managed_items = receipt.get("_managed_items")
        managed_pending = isinstance(managed_items, Mapping) and any(
            isinstance(item, Mapping) and item.get("phase") != "complete"
            for item in managed_items.values()
        )
        if str(receipt.get("state") or "").lower() == "pending" or managed_pending:
            item_ids = [str(item.get("item_id")) for item in receipt.get("items", []) if isinstance(item, Mapping)]
            expected_ids = receipt.get("_item_ids") or item_ids
            provider_names = receipt.get("_provider_names") or []
            for provider_name in provider_names:
                provider = self._providers.get(str(provider_name))
                if provider is None:
                    continue
                status = getattr(provider, "operation_status", None)
                if not callable(status):
                    continue
                try:
                    candidate = await status(context, operation_id=operation)
                    if not isinstance(candidate, Mapping):
                        continue
                    reconciled = _validated_receipt(candidate, operation_id=operation, item_id=operation, expected_item_ids=expected_ids)
                    # Provider status is untrusted even though it comes from
                    # the bound adapter. Reauthorize every returned ref under
                    # the current owner; an invalid or revoked ref is removed
                    # from the receipt rather than echoed to the caller.
                    for item in reconciled.get("items", []):
                        token = item.get("resource_ref")
                        if token:
                            try:
                                _status_provider, _status_ref = self._provider_for_ref(context, str(token), capability="stat")
                                if _status_provider is not provider:
                                    raise FilesFacadeError("operation resource provider changed", code="resource_unavailable")
                            except Exception:
                                for key in ("resource_ref", "resource_key", "revision", "history", "provenance"):
                                    item.pop(key, None)
                    original_generation = int(stored.get("generation", receipt.get("generation", -1)))
                    if original_generation == int(context.policy_generation) and self._generation_current(context):
                        reconciled["generation"] = original_generation
                        for private_key in ("_import_binding", "_workspace_id", "_provider_names", "_request_kind", "_item_ids"):
                            if private_key in receipt:
                                reconciled[private_key] = receipt[private_key]
                        self._save_operation(context, operation, str(stored.get("digest") or ""), original_generation, reconciled)
                        return _public_operation(reconciled)
                    # Status may be reconciled after a revocation, but the
                    # old generation's references and provider history stay
                    # hidden and the durable original row is untouched.
                    return {"operation_id": operation, "generation": original_generation, "state": reconciled.get("state", "partial"), "items": [{"item_id": item["item_id"], "outcome": item.get("outcome", "pending"), "code": item.get("code")} for item in reconciled.get("items", [])]}
                except Exception:
                    continue
        # A revoked/currently changed policy may reconcile status, but must not
        # disclose refs from a generation that is no longer authorized.
        if int(stored.get("generation", -1)) != int(context.policy_generation):
            return {"operation_id": operation, "generation": int(stored.get("generation", -1)), "state": receipt.get("state", "partial"), "items": [{"item_id": item["item_id"], "outcome": item.get("outcome", "pending"), "code": item.get("code")} for item in receipt.get("items", []) if isinstance(item, Mapping)]}
        if isinstance(managed_items, Mapping) and isinstance(receipt.get("items"), list):
            public_items = []
            for value in receipt["items"]:
                if not isinstance(value, Mapping):
                    continue
                public = dict(value)
                private = managed_items.get(str(value.get("item_id") or ""))
                from src.openclank.file_policy import is_closed_managed_file_plan
                if (isinstance(private, Mapping) and private.get("phase") == "complete"
                        and isinstance(private.get("plan"), Mapping) and (private["plan"].get("kind") == "folder" or is_closed_managed_file_plan(private["plan"]))):
                    public["cleanup"] = "complete" if private.get("cleanup_phase") == "complete" else "pending"
                    plan = private["plan"]
                    if (plan.get("kind") != "folder" and plan.get("destination_frame") is not None and plan.get("selected_folder") is None
                            and public.get("outcome") == "committed"):
                        public["undo"] = "available"
                public_items.append(public)
            receipt["items"] = public_items
        return {key: value for key, value in receipt.items() if not str(key).startswith("_")}

    async def cleanup_managed_folder(self, context: ProviderContext, *, operation_id: str, item_id: str,
                                     managed_root: Mapping[str, Any] | None = None) -> dict[str, Any]:
        operation = self._bounded_id(operation_id, "operation_id")
        item = self._bounded_id(item_id, "item_id")
        stored = self._load_operation(context, operation)
        receipt = stored.get("receipt") if isinstance(stored, Mapping) else None
        managed = receipt.get("_managed_items") if isinstance(receipt, Mapping) else None
        selected = managed.get(item) if isinstance(managed, Mapping) else None
        from src.openclank.file_policy import is_closed_managed_file_plan
        if (not isinstance(stored, Mapping) or stored.get("owner") != context.owner_subject_id
                or int(stored.get("generation", -1)) != int(context.policy_generation) or not self._generation_current(context)
                or not isinstance(selected, Mapping) or not ((selected.get("plan") or {}).get("kind") == "folder" or is_closed_managed_file_plan(selected.get("plan")))):
            raise FilesFacadeError("Managed folder cleanup is unavailable", code="resource_unavailable")
        provider = self._providers.get("host")
        cleanup = getattr(provider, "cleanup_managed_folder_item", None)
        if not callable(cleanup):
            raise FilesFacadeError("Managed folder cleanup is unavailable", code="provider_unavailable")
        selected_witness = (selected.get("plan") or {}).get("selected_folder")
        fresh_root = None
        if selected_witness is not None:
            if (not isinstance(managed_root, Mapping) or set(managed_root) != {"resource_ref", "expected_revision"}
                    or not isinstance(managed_root.get("expected_revision"), Mapping)):
                raise FilesFacadeError("Selected folder cleanup requires fresh authority", code="operation_pending")
            async def fresh_root():
                root_provider, root_ref = self._provider_for_ref(context, str(managed_root["resource_ref"]), capability="write")
                if root_provider is not provider or root_ref.provider != "host" or not self._generation_current(context):
                    raise FilesFacadeError("Selected folder is unavailable", code="resource_unavailable")
                return await provider.authorize_selected_folder(context, origin_id=root_ref.origin_id,
                                                                expected_revision=managed_root["expected_revision"])
            await fresh_root()
        elif managed_root is not None:
            raise FilesFacadeError("Unexpected cleanup root", code="invalid_resource_request")
        result = await cleanup(context, operation_id=operation, item_id=item, fresh_root=fresh_root)
        return {"operation_id":operation, "item_id":item, "outcome":"committed",
                "cleanup":"complete" if result.get("cleanup") == "complete" else "pending"}

    async def undo_managed_move(self, context: ProviderContext, *, operation_id: str, item_id: str) -> dict[str, Any]:
        """Undo one completed cross-root file move (BQ-23). The undo operation id is derived from the account, the move and the item,
        so a retry is the SAME undo (Native replays it) and the answer is read back by calling this again."""
        operation = self._bounded_id(operation_id, "operation_id")
        item = self._bounded_id(item_id, "item_id")
        stored = self._load_operation(context, operation)
        receipt = stored.get("receipt") if isinstance(stored, Mapping) else None
        managed = receipt.get("_managed_items") if isinstance(receipt, Mapping) else None
        selected = managed.get(item) if isinstance(managed, Mapping) else None
        plan = (selected or {}).get("plan") if isinstance(selected, Mapping) else None
        if (not isinstance(stored, Mapping) or stored.get("owner") != context.owner_subject_id
                or int(stored.get("generation", -1)) != int(context.policy_generation) or not self._generation_current(context)
                or not isinstance(plan, Mapping) or selected.get("phase") != "complete"
                or plan.get("destination_frame") is None or plan.get("selected_folder") is not None):
            raise FilesFacadeError("This move cannot be undone", code="resource_unavailable")
        provider = self._providers.get("host")
        undo = getattr(provider, "undo_managed_cross_root_move", None)
        if not callable(undo):
            raise FilesFacadeError("Undo is unavailable", code="provider_unavailable")
        import hashlib
        undo_id = "undo_" + hashlib.sha256(f"{context.owner_subject_id}|{operation}|{item}".encode()).hexdigest()[:48]
        result = await undo(context, operation_id=operation, item_id=item, undo_native_operation_id=undo_id)
        outcome, code = str(result.get("outcome") or ""), str(result.get("code") or "")
        if outcome == "committed":
            return {"operation_id": operation, "item_id": item, "undo": "committed"}
        if outcome == "pending":
            pending = {"operation_id": operation, "item_id": item, "undo": "pending"}
            if code == "managed_partial_needs_review": pending["code"] = code
            return pending
        raise FilesFacadeError(_UNDO_MESSAGES.get(code, "This move could not be undone; nothing was changed."),
                               code=code if code in _UNDO_MESSAGES else "resource_unavailable")

    async def attachment_receipt(self, context: ProviderContext, *, operation_id: str) -> dict[str, Any]:
        """Return a typed, owner-scoped preparation descriptor for recovery."""
        operation = self._bounded_id(operation_id, "operation_id")
        stored = self._load_operation(context, operation)
        if stored is None or stored.get("owner") != context.owner_subject_id:
            raise FilesFacadeError("attachment operation is unavailable", code="resource_unavailable")
        stored_receipt = stored.get("receipt") if isinstance(stored.get("receipt"), Mapping) else {}
        stored_workspace = str(stored_receipt.get("_workspace_id") or "").strip()
        if not stored_workspace:
            # Older rows predate the private workspace binding. Derive it only
            # from a typed resource identity; an untyped row is fail-closed.
            for identity in (
                stored_receipt.get("target_identity"),
                stored_receipt.get("asset"),
            ):
                if not isinstance(identity, Mapping):
                    continue
                key = identity.get("resource_key") if isinstance(identity.get("resource_key"), Mapping) else identity
                value = key.get("workspace_id") if isinstance(key, Mapping) else None
                if value:
                    stored_workspace = str(value).strip()
                    break
        if not stored_workspace or stored_workspace != str(context.workspace_id or "default"):
            raise FilesFacadeError("attachment operation is unavailable", code="resource_unavailable")
        original_generation = int(stored.get("generation", -1))
        candidate = stored_receipt if stored_receipt.get("insertion") else None
        if candidate is None:
            pending = stored_receipt
            for provider_name in pending.get("_provider_names") or []:
                provider_key = str(provider_name).strip().lower()
                typed_target = self._attachment_targets.get(provider_key)
                provider = self._providers.get(str(provider_name)) or typed_target
                status = getattr(provider, "attachment_status", None) if provider is not None else None
                if not callable(status) and typed_target is not None and provider is typed_target:
                    status = getattr(provider, "operation_status", None)
                if not callable(status):
                    continue
                try:
                    candidate = await status(context, operation_id=operation)
                except Exception:
                    candidate = None
                if candidate is not None:
                    break
        if candidate is None:
            stored_items = pending.get("items") if isinstance(pending, Mapping) else []
            if isinstance(stored_items, Sequence) and any(
                isinstance(item, Mapping) and str(item.get("outcome") or "").lower() in {"failed", "denied", "conflict", "stale"}
                for item in stored_items
            ):
                return {"operation_id": operation, "generation": original_generation, "state": "failed", "preparation": None}
            return {"operation_id": operation, "generation": original_generation, "state": "pending", "preparation": None}
        try:
            # The durable row carries a private workspace binding for the
            # authorization check above. It is routing metadata, not part of
            # the provider's typed preparation contract.
            public_candidate = {
                key: value for key, value in candidate.items() if str(key) != "_workspace_id"
            }
            validated = _validated_preparation(public_candidate, operation_id=operation, expected_generation=original_generation)
        except FilesFacadeError:
            return {"operation_id": operation, "generation": original_generation, "state": "failed", "preparation": None}
        if original_generation != int(context.policy_generation) or not self._generation_current(context):
            return {"operation_id": operation, "generation": original_generation, "state": "stale", "preparation": None}
        if validated.get("generation") not in {None, original_generation}:
            return {"operation_id": operation, "generation": original_generation, "state": "failed", "preparation": None}
        validated["generation"] = original_generation
        target_kind = validated.get("target_identity", {}).get("kind") if isinstance(validated.get("target_identity"), Mapping) else None
        if target_kind not in {"treehouse_lesson", "treehouse_submission"} and (not isinstance(validated.get("asset"), Mapping) or not validated["asset"].get("resource_key")):
            return {"operation_id": operation, "generation": original_generation, "state": "pending", "preparation": None}
        return {"operation_id": operation, "generation": original_generation, "state": "complete", "preparation": validated}

    async def _video_frame_source(self, context: ProviderContext, value: Any, current: Callable[[], Any]):
        """Validate frame-copy metadata; source bytes do not attest pixel correspondence."""
        fields = {'version','kind','sourceRef','expectedRevision','timeHintMs'}
        if (not isinstance(value,dict) or set(value) != fields or value.get('version') != 1 or type(value.get('version')) is not int
            or value.get('kind') != 'video_frame_copy' or not isinstance(value.get('sourceRef'),str)
            or not 8 <= len(value['sourceRef']) <= 16384 or type(value.get('timeHintMs')) is not int
            or not 0 <= value['timeHintMs'] <= 2**53-1 or not callable(current)):
            raise FilesFacadeError('Video frame source is invalid',code='invalid_resource_request')
        source, still_current = await self._host_video_source(context,source_ref=value['sourceRef'],
            expected_revision=value['expectedRevision'],current=current)
        evidence={'version':1,'kind':'video_frame_copy','source_resource_id':source['source_resource_id'],
            'source_revision':source['source_revision'],'source_sha256':source['source_sha256'],'source_size':source['source_size'],
            'time_hint_ms':value['timeHintMs'],'correspondence':'unverified'}
        return evidence, still_current

    async def _host_video_source(self, context: ProviderContext, *, source_ref: str, expected_revision: Mapping[str, Any],
                                 current: Callable[[], Any], snapshot_fd: int | None = None):
        """Bounded current Host-video stream and optional privately owned snapshot sink."""
        if not isinstance(source_ref,str) or not 8 <= len(source_ref) <= 16384 or not callable(current):
            raise FilesFacadeError('Video source is invalid',code='invalid_resource_request')
        revision = expected_revision
        if (not isinstance(revision,dict) or set(revision) != {'kind','value'} or revision.get('kind') != 'hostFingerprint'
            or not isinstance(revision.get('value'),str) or not re.fullmatch(r'(?:[0-9a-f]{64}|sha256:[0-9a-f]{64}:[0-9]{1,9})',revision['value'])):
            raise FilesFacadeError('Video source revision is unavailable',code='resource_changed')
        ref_token = source_ref
        async def authorize():
            if not await current() or not self._generation_current(context):
                raise FilesFacadeError('Video source authority changed',code='resource_changed')
            provider, ref = self._provider_for_ref(context,ref_token,capability='download')
            if (ref.provider != 'host' or ref.kind != 'file' or (ref.workspace_id and ref.workspace_id != context.workspace_id)
                or not callable(getattr(provider,'stat_inspection',None))):
                raise FilesFacadeError('Video source unavailable',code='resource_unavailable')
            return provider, ref
        try:
            async with asyncio.timeout(30):
                provider, ref = await authorize()
                observed = await provider.stat_inspection(context,origin_id=ref.origin_id)
                tag = (observed.revision or {}).get('value') if isinstance(observed.revision,Mapping) else None
                if (observed.origin_id != ref.origin_id or observed.kind != 'file' or not str(observed.name).lower().endswith('.webm')
                    or type(observed.size) is not int or not 0 < observed.size <= 64*1024*1024
                    or (observed.revision or {}).get('kind') != 'hostObjectIdentity' or not isinstance(tag,str)
                    or not re.fullmatch(r'[0-9a-f]{64}',tag)):
                    raise FilesFacadeError('Video source unavailable',code='resource_unavailable')
                source = await provider.content(context,origin_id=ref.origin_id)
                if (source.origin_id != ref.origin_id or source.size != observed.size or source.etag != tag
                    or source.stream is None or source.data is not None or source.path is not None):
                    raise FilesFacadeError('Video source changed',code='resource_changed')
                digest=hashlib.sha256(); total=0
                async for chunk in source.stream(0,observed.size):
                    if not isinstance(chunk,bytes): raise FilesFacadeError('Video source stream is invalid',code='provider_unavailable')
                    total += len(chunk)
                    if total > observed.size or total > 64*1024*1024: raise FilesFacadeError('Video source exceeds limit',code='resource_changed')
                    digest.update(chunk)
                    if snapshot_fd is not None:
                        def write_all():
                            import os
                            view=memoryview(chunk)
                            while view:
                                written=os.write(snapshot_fd,view)
                                if written <= 0: raise OSError('snapshot write failed')
                                view=view[written:]
                        task=asyncio.create_task(asyncio.to_thread(write_all)); cancelled=False
                        while not task.done():
                            try: await asyncio.shield(task)
                            except asyncio.CancelledError: cancelled=True
                        task.result()
                        if cancelled: raise asyncio.CancelledError
                sha=digest.hexdigest()
                if total != observed.size or revision['value'] not in (sha,f'sha256:{sha}:{total}'):
                    raise FilesFacadeError('Video source changed',code='resource_changed')
                async def still_current():
                    try:
                        p,r=await authorize()
                        if p is not provider or r.origin_id != ref.origin_id or r.stable_id != ref.stable_id: return False
                        latest=await provider.stat_inspection(context,origin_id=ref.origin_id)
                        return bool(latest.origin_id == ref.origin_id and latest.kind == 'file' and latest.size == total
                            and latest.revision == observed.revision and await current() and self._generation_current(context))
                    except Exception: return False
                if not await still_current(): raise FilesFacadeError('Video source changed',code='resource_changed')
                evidence={'source_resource_id':stable_resource_id(owner_subject_id=context.owner_subject_id,provider='host',origin_id=ref.origin_id),
                    'source_revision':dict(revision),'source_sha256':sha,'source_size':total,'source_object_tag':tag}
                return evidence, still_current
        except TimeoutError as exc:
            raise FilesFacadeError('Video source read timed out',code='provider_unavailable') from exc


    async def import_file(self, context: ProviderContext, *, upload: Any, metadata: Mapping[str, Any], source_evidence: Mapping[str, Any] | None = None, before_commit: Callable[[], Any] | None = None, expected_destination_revision: Mapping[str, Any] | None = None, video_current: Callable[[], Any] | None = None) -> dict[str, Any]:
        """Stream one upload through a provider-owned staging adapter."""
        if not isinstance(metadata, Mapping):
            raise FilesFacadeError("import metadata is invalid", code="invalid_resource_request")
        if metadata.get("generation") is None:
            raise FilesFacadeError("file policy generation is required", code="resource_ref_stale")
        generation = _strict_nonnegative_int(metadata.get("generation"), "generation")
        if generation != int(context.policy_generation):
            raise FilesFacadeError("file policy generation is stale", code="resource_ref_stale")
        frame_source = metadata.get('videoFrameSource')
        frame_check = None
        if frame_source is not None:
            if source_evidence is not None or before_commit is not None or not callable(video_current):
                raise FilesFacadeError('Video frame import authority is unavailable',code='invalid_resource_request')
            source_evidence, frame_check = await self._video_frame_source(context,frame_source,video_current)
        elif 'videoFrameSource' in metadata:
            raise FilesFacadeError('Video frame source is invalid',code='invalid_resource_request')
        import_fields = ("operation_id", "item_id", "destination_ref", "name", "collision")
        if any(not isinstance(metadata.get(field), str) for field in import_fields):
            raise FilesFacadeError("import metadata is invalid", code="invalid_resource_request")
        operation = self._bounded_id(metadata.get("operation_id"), "operation_id")
        item_id = self._bounded_id(metadata.get("item_id"), "item_id")
        destination_ref = metadata["destination_ref"].strip()
        name = metadata["name"].strip()
        if not destination_ref or not name or len(name.encode("utf-8")) > 240 or any(part in name for part in ("/", "\\", "\x00")) or name in {".", ".."}:
            raise FilesFacadeError("import name or destination is invalid", code="invalid_resource_request")
        relative_parts = self._import_relative_parts(metadata.get("relative_path"), name)
        collision = metadata["collision"].strip().lower()
        if collision not in {"fail", "rename"}:
            raise FilesFacadeError("import collision policy is unsupported", code="unsupported_operation")
        digest = None
        pending_receipt = {"operation_id": operation, "generation": int(context.policy_generation), "state": "pending", "items": [{"item_id": item_id, "outcome": "pending", "code": "operation_in_progress"}], "_workspace_id": str(context.workspace_id or "default")}
        provider = destination = None
        stage = None
        result = None
        binding = None
        spooled = _SpooledUpload(upload)

        def retain_import_binding(receipt: Mapping[str, Any]) -> dict[str, Any]:
            retained = dict(receipt)
            retained["_workspace_id"] = str(context.workspace_id or "default")
            if binding is not None:
                retained["_import_binding"] = dict(binding)
            return retained

        try:
            provider, destination = self._provider_for_ref(context, destination_ref, capability="write")
            destination_entry = await provider.stat(context, origin_id=destination.origin_id)
            if not self._generation_current(context):
                raise FilesFacadeError("file policy generation changed", code="policy_generation_changed")
            if destination_entry.kind != destination.kind or destination_entry.kind not in _DIRECTORY_KINDS or "children" not in destination_entry.capabilities or "write" not in destination_entry.capabilities:
                raise FilesFacadeError("import destination is unavailable", code="resource_unavailable")
            stager = getattr(provider, "stage_import", None)
            finisher = getattr(provider, "finish_import", None)
            if not callable(stager) or not callable(finisher):
                raise FilesFacadeError("provider lacks the required staged import contract", code="unsupported_provider_kind")
            declared_type = str(getattr(upload, "content_type", "") or "").strip()
            if len(declared_type.encode("utf-8")) > 256 or any(ord(ch) < 32 for ch in declared_type):
                raise FilesFacadeError("import MIME metadata is invalid", code="invalid_resource_request")
            await spooled.copy_from(upload, limit=FILES_IMPORT_MAX_BYTES, generation_check=lambda: self._generation_current(context))
            if frame_check is not None:
                if not name.lower().endswith('.png') or declared_type != 'image/png' or spooled.length > 32*1024*1024:
                    raise FilesFacadeError('Video frame PNG is invalid',code='invalid_resource_request')
                signature = await spooled.read(8)
                spooled._file.seek(0)
                if signature != b'\x89PNG\r\n\x1a\n':
                    raise FilesFacadeError('Video frame PNG is invalid',code='invalid_resource_request')
            content_digest = "sha256:" + spooled.digest.hexdigest()
            destination_revision = self._provider_revision(destination_entry)
            if expected_destination_revision is not None and destination_revision != dict(expected_destination_revision):
                raise FilesFacadeError("import destination changed", code="resource_changed")
            binding = {
                "account_id": str(context.owner_subject_id),
                "workspace_id": str(context.workspace_id or "default"),
                "generation": int(context.policy_generation),
                "operation_id": operation,
                "item_id": item_id,
                "destination_resource": destination.stable_id,
                "destination_revision": dict(destination_revision) if destination_revision is not None else None,
                "name": name,
                "relative_path": "/".join(relative_parts),
                "collision": collision,
                "upload_digest": content_digest,
                "upload_size": int(spooled.length),
                "declared_type": declared_type or None,
            }
            if source_evidence is not None:
                binding["derived_source"] = dict(source_evidence)
            digest = self._digest({"kind": "import", "binding": binding})
            pending_receipt["_import_binding"] = binding
            pending_receipt["_provider_names"] = [str(getattr(provider, "name", ""))]
            pending_receipt["_request_kind"] = "import"
            pending_receipt["_item_ids"] = [item_id]
            # This reservation is deliberately before stage_import.  A retry
            # can therefore reconcile an already committed provider operation
            # without creating a second provider stage or destination file.
            owner, previous_receipt = self._reserve_operation(context, operation, digest, context.policy_generation, pending_receipt)
            if not owner:
                status_fn = getattr(provider, "operation_status", None)
                recovered = None
                if callable(status_fn):
                    try:
                        recovered = await status_fn(
                            context,
                            operation_id=operation,
                            name=name,
                            collision=collision,
                            digest=content_digest,
                            length=int(spooled.length),
                            request_digest=digest,
                        )
                    except TypeError:
                        recovered = None
                    except Exception:
                        recovered = None
                if isinstance(recovered, Mapping) and recovered.get("_request_digest") == digest:
                    try:
                        reconciled = _validated_receipt(
                            {key: value for key, value in recovered.items() if key != "_request_digest"},
                            operation_id=operation,
                            item_id=item_id,
                            expected_item_ids=[item_id],
                        )
                    except FilesFacadeError:
                        reconciled = None
                    if reconciled is not None:
                        reconciled["generation"] = int(context.policy_generation)
                        reconciled = retain_import_binding(reconciled)
                        self._save_operation(context, operation, digest, context.policy_generation, reconciled)
                        return _public_operation(reconciled)
                return _public_operation(previous_receipt or pending_receipt)
            # The original destination ref is the only browser authority. A
            # nested relative path is walked one segment at a time after the
            # file operation reservation; providers decide how folders are
            # created and reauthorize each segment.
            if len(relative_parts) > 1:
                destination = await self._ensure_import_parent(
                    context,
                    provider=provider,
                    parent=destination,
                    directory_parts=relative_parts[:-1],
                    operation_id=operation,
                )
                destination_entry = await provider.stat(context, origin_id=destination.origin_id)
                self._validate_import_directory(destination_entry, expected_origin=destination.origin_id)
                if not self._generation_current(context):
                    raise FilesFacadeError("file policy generation changed", code="policy_generation_changed")
                destination_revision = self._provider_revision(destination_entry)
                binding["destination_resource"] = destination.stable_id
                binding["destination_revision"] = destination_revision
                pending_receipt["_import_binding"] = binding
                self._save_operation(context, operation, digest, context.policy_generation, pending_receipt)
            bounded_upload = spooled
            if callable(stager) and callable(finisher):
                stage = await stager(context, destination_origin_id=destination.origin_id, name=name, upload=bounded_upload, operation_id=operation, item_id=item_id)
                if not isinstance(stage, Mapping) or not stage.get("stage_id") or stage.get("digest") != content_digest or type(stage.get("length")) is not int or stage.get("length") != spooled.length:
                    raise FilesFacadeError("provider returned an invalid import stage", code="provider_unavailable")
                if not self._generation_current(context):
                    raise FilesFacadeError("file policy generation changed", code="policy_generation_changed")
                current_destination = await provider.stat(context, origin_id=destination.origin_id)
                if not self._generation_current(context):
                    raise FilesFacadeError("file policy generation changed", code="policy_generation_changed")
                if current_destination.origin_id != destination.origin_id or current_destination.kind != destination.kind or current_destination.kind not in _DIRECTORY_KINDS or "children" not in current_destination.capabilities or "write" not in current_destination.capabilities:
                    raise FilesFacadeError("import destination changed", code="resource_changed")
                if self._provider_revision(current_destination) != destination_revision:
                    raise FilesFacadeError("import destination changed", code="resource_changed")
                # A trusted derived-output producer can reauthorize its source
                # and session after staging, immediately before publication.
                # The provider still owns its destination CAS and receipt.
                if frame_check is not None and not await frame_check():
                    raise FilesFacadeError('Video source changed',code='resource_changed')
                if before_commit is not None:
                    if not await before_commit():
                        raise FilesFacadeError("import source changed", code="resource_changed")
                    if not self._generation_current(context):
                        raise FilesFacadeError("file policy generation changed", code="policy_generation_changed")
                result = await finisher(context, destination_origin_id=destination.origin_id, destination_revision=destination_revision, name=name, collision=collision, stage=stage, operation_id=operation, item_id=item_id, request_digest=digest)
                if not self._generation_current(context):
                    raise FilesFacadeError("file policy generation changed", code="policy_generation_changed")
            else:
                raise FilesFacadeError("provider lacks the required staged import contract", code="unsupported_provider_kind")
        except FilesFacadeError as error:
            uncertain = error.code == "mutation_outcome_unknown"
            if not uncertain and stage is not None and provider is not None:
                abort = getattr(provider, "abort_import", None)
                if callable(abort):
                    try: await abort(context, stage=stage)
                    except Exception: pass
            failed = ({**pending_receipt, "items": [{"item_id": item_id, "outcome": "pending", "code": error.code}]}
                      if uncertain else {"operation_id": operation, "generation": int(context.policy_generation), "state": "partial", "items": [{"item_id": item_id, "outcome": "failed", "code": error.code}]})
            if digest is not None:
                self._save_operation(context, operation, digest, context.policy_generation, retain_import_binding(failed))
            raise
        except Exception:
            if stage is not None and provider is not None:
                abort = getattr(provider, "abort_import", None)
                if callable(abort):
                    try: await abort(context, stage=stage)
                    except Exception: pass
            failed = {"operation_id": operation, "generation": int(context.policy_generation), "state": "partial", "items": [{"item_id": item_id, "outcome": "failed", "code": "provider_unavailable"}]}
            if digest is not None:
                self._save_operation(context, operation, digest, context.policy_generation, retain_import_binding(failed))
            return failed
        finally:
            spooled.close()
        if isinstance(result, ProviderResource):
            try:
                public_resource = self._resource(context, destination.provider, result, parent_stable_id=destination.stable_id)
                provenance = _public_provenance(result.provenance)
                item = {"item_id": item_id, "outcome": "committed", "resource_key": stable_resource_id(owner_subject_id=context.owner_subject_id, provider=destination.provider, origin_id=result.origin_id), "resource_ref": public_resource["ref"]}
            except Exception as error:
                failed = {"operation_id": operation, "generation": int(context.policy_generation), "state": "partial", "items": [{"item_id": item_id, "outcome": "failed", "code": "provider_unavailable"}]}
                self._save_operation(context, operation, digest, context.policy_generation, retain_import_binding(failed))
                raise FilesFacadeError("provider returned an invalid import resource", code="provider_unavailable") from error
            if provenance:
                item["provenance"] = provenance
            revision = self._provider_revision(result)
            if revision is not None:
                item["revision"] = dict(revision)
            history = _public_action_receipt(result.action_receipt)
            if history:
                item["history"] = history
                if history.get("receipt_id") or history.get("action_id"):
                    item["receipt_id"] = history.get("receipt_id") or history.get("action_id")
            receipt = retain_import_binding({"operation_id": operation, "generation": context.policy_generation, "state": "complete", "items": [item]})
            self._save_operation(context, operation, digest, context.policy_generation, receipt)
            return _public_operation(receipt)
        if not isinstance(result, Mapping):
            failed = {"operation_id": operation, "generation": int(context.policy_generation), "state": "partial", "items": [{"item_id": item_id, "outcome": "failed", "code": "provider_unavailable"}]}
            self._save_operation(context, operation, digest, context.policy_generation, retain_import_binding(failed))
            raise FilesFacadeError("provider returned an invalid import result", code="provider_unavailable")
        try:
            receipt = _validated_receipt(result, operation_id=operation, item_id=item_id)
        except FilesFacadeError as error:
            failed = {"operation_id": operation, "generation": int(context.policy_generation), "state": "partial", "items": [{"item_id": item_id, "outcome": "failed", "code": error.code}]}
            self._save_operation(context, operation, digest, context.policy_generation, retain_import_binding(failed))
            raise
        # Provider receipts are normalized without transport identity; the
        # browser uses the operation ID to reconcile a response that was lost
        # after the provider committed the import.
        receipt["operation_id"] = operation
        receipt["generation"] = int(context.policy_generation)
        receipt = retain_import_binding(receipt)
        self._save_operation(context, operation, digest, context.policy_generation, receipt)
        return _public_operation(receipt)

    async def prepare_attachment(self, context: ProviderContext, *, operation_id: str, generation: int, source: Mapping[str, Any], target: Mapping[str, Any], mode: str, upload_bytes: bytes | None = None) -> dict[str, Any]:
        requested_generation = _strict_nonnegative_int(generation, "generation")
        if requested_generation != int(context.policy_generation):
            raise FilesFacadeError("file policy generation is stale", code="resource_ref_stale")
        operation = self._bounded_id(operation_id, "operation_id")
        if not isinstance(source, Mapping) or not isinstance(target, Mapping):
            raise FilesFacadeError("attachment source or target is invalid", code="invalid_resource_request")
        normalized_mode = str(mode or "").strip().lower()
        if normalized_mode not in {"link", "embed"}:
            raise FilesFacadeError("attachment mode is invalid", code="invalid_resource_request")
        source_keys = set(source)
        if source.get('kind') == 'client_upload':
            if (set(source) != {'kind','name','mime','sha256','size'} or not isinstance(upload_bytes, bytes)
                or not 0 < len(upload_bytes) <= min(FILES_IMPORT_MAX_BYTES, 10 * 1024 * 1024)
                or type(source.get('size')) is not int or source['size'] != len(upload_bytes)
                or not isinstance(source.get('name'), str) or not source['name'] or len(source['name'].encode('utf-8')) > 240
                or any(c in source['name'] for c in ('/', '\\', '\x00')) or source['name'] in ('.','..')
                or not isinstance(source.get('mime'), str) or not source['mime'] or len(source['mime'].encode('utf-8')) > 128
                or any(ord(c) < 32 or ord(c) == 127 for c in source['name'] + source['mime'])
                or source.get('sha256') != hashlib.sha256(upload_bytes).hexdigest() or target.get('kind') != 'host_document'):
                raise FilesFacadeError('attachment upload is invalid', code='invalid_resource_request')
        elif upload_bytes is not None:
            raise FilesFacadeError('attachment source is invalid', code='invalid_resource_request')
        elif "resource_ref" in source:
            if source_keys - {"resource_ref", "expected_revision"} or not str(source.get("resource_ref") or "").strip():
                raise FilesFacadeError("attachment source descriptor is invalid", code="invalid_resource_request")
        elif "import_receipt_id" in source:
            if source_keys - {"import_receipt_id", "item_id", "expected_revision"} or not str(source.get("import_receipt_id") or "").strip() or not str(source.get("item_id") or "").strip():
                raise FilesFacadeError("attachment source descriptor is invalid", code="invalid_resource_request")
        else:
            raise FilesFacadeError("attachment source must have exactly one variant", code="invalid_resource_request")
        target_kind = str(target.get("kind") or "").strip()
        if target_kind in {"copal_document", "host_document"}:
            permitted = {"kind", "resource_ref", "expected_revision"} | ({"root_ref", "expected_root_revision"} if target_kind == "host_document" else set())
            if set(target) - permitted or not str(target.get("resource_ref") or "").strip():
                raise FilesFacadeError("attachment target descriptor is invalid", code="invalid_resource_request")
            if (target.get("root_ref") is None) != (target.get("expected_root_revision") is None) or (target.get("root_ref") is not None and target.get("expected_revision") is None):
                raise FilesFacadeError("selected root requires current revisions", code="invalid_resource_request")
        elif target_kind in {"treehouse_lesson", "treehouse_submission"}:
            if set(target) - {"kind", "course_id", "lesson_id", "assignment_id", "expected_revision"} or not str(target.get("course_id") or "").strip() or not str(target.get("assignment_id" if target_kind == "treehouse_submission" else "lesson_id") or "").strip():
                raise FilesFacadeError("attachment target descriptor is invalid", code="invalid_resource_request")
        else:
            raise FilesFacadeError("attachment target kind is unsupported", code="unsupported_provider_kind")
        for descriptor in (source, target):
            expected = descriptor.get("expected_revision")
            if expected is not None and (not isinstance(expected, Mapping) or set(expected) != {"kind", "value"} or not expected.get("kind") or expected.get("value") is None):
                raise FilesFacadeError("attachment revision is invalid", code="invalid_resource_request")
        root_revision = target.get("expected_root_revision")
        if root_revision is not None and (not isinstance(root_revision, Mapping) or set(root_revision) != {"kind", "value"} or not root_revision.get("kind") or not root_revision.get("value")):
            raise FilesFacadeError("attachment root revision is invalid", code="invalid_resource_request")
        async def selected_root_current():
            root_provider, root_ref = self._provider_for_ref(context, str(target["root_ref"]), capability="write")
            if root_ref.provider != "host" or (root_ref.workspace_id and root_ref.workspace_id != str(context.workspace_id or "default")):
                raise FilesFacadeError("attachment root unavailable", code="resource_unavailable")
            root = await root_provider.stat(context, origin_id=root_ref.origin_id)
            if (root.origin_id != root_ref.origin_id or root.kind not in _DIRECTORY_KINDS
                    or not {"write", "children"}.issubset(root.capabilities) or not self._generation_current(context)
                    or self._provider_revision(root) is None):
                raise FilesFacadeError("attachment root unavailable", code="resource_unavailable")
            self._check_revision(root, root_revision)
            return root_provider, root_ref, root
        if root_revision is not None:
            await selected_root_current()
        target_provider = None
        target_adapter = None
        target_ref = None
        if target_kind in {"copal_document", "host_document"}:
            target_token = str(target.get("resource_ref") or target.get("ref") or "").strip()
            if not target_token:
                raise FilesFacadeError("attachment target reference is required", code="invalid_resource_request")
            target_provider, target_ref = self._provider_for_ref(context, target_token, capability="stat")
            target_entry = await target_provider.stat(context, origin_id=target_ref.origin_id)
            if not self._generation_current(context):
                raise FilesFacadeError("file policy generation changed", code="policy_generation_changed")
            if target_entry.origin_id != target_ref.origin_id or "write" not in target_entry.capabilities:
                raise FilesFacadeError("attachment target is not writable", code="resource_unavailable")
            self._check_revision(target_entry, target.get("expected_revision"))
            if target_kind == "host_document":
                # Host targets dispatch through the registered adapter so the
                # receipt keeps its own identity; synthetic Host ids are never
                # presented as Copal ids.
                target_adapter = self._attachment_targets.get(target_kind)
                if target_adapter is None:
                    raise FilesFacadeError("attachment representation is unavailable", code="unsupported_provider_kind")
        elif target_kind in {"treehouse_lesson", "treehouse_submission"}:
            target_adapter = self._attachment_targets.get(target_kind)
        # Workspace and account are part of the idempotency identity.  A
        # sealed Copal token may be reused only from the same tenant context;
        # leaving that scope out would let a lost-response retry enter the
        # provider recovery branch before the imported-receipt checks below.
        digest = self._digest({
            "kind": "attach",
            "account_id": str(context.owner_subject_id),
            "workspace_id": str(context.workspace_id or "default"),
            "generation": int(context.policy_generation),
            "source": dict(source),
            "target": dict(target),
            "mode": normalized_mode,
        })
        pending_receipt = {"operation_id": operation, "generation": int(context.policy_generation), "state": "pending", "items": [{"item_id": operation, "outcome": "pending", "code": "operation_in_progress"}]}
        pending_receipt["_workspace_id"] = str(context.workspace_id or "default")
        pending_receipt["_provider_names"] = [str(getattr(target_provider or target_adapter, "name", ""))] if (target_provider or target_adapter) is not None else []
        pending_receipt["_request_kind"] = "attachment"
        pending_receipt["_item_ids"] = [operation]
        # Validate an imported receipt before reserving or recovering an
        # existing operation.  This keeps a same-account, wrong-workspace
        # retry non-disclosing even when its operation ID already exists.
        if source.get("import_receipt_id"):
            imported = self._load_operation(context, str(source["import_receipt_id"]).strip())
            imported_receipt = imported.get("receipt") if isinstance(imported, Mapping) and isinstance(imported.get("receipt"), Mapping) else {}
            if (
                imported is None
                or imported.get("owner") != context.owner_subject_id
                or str(imported_receipt.get("_workspace_id") or "") != str(context.workspace_id or "default")
                or int(imported.get("generation", -1)) != int(context.policy_generation)
            ):
                raise FilesFacadeError("import receipt is unavailable", code="resource_unavailable")
        if not self._generation_current(context):
            raise FilesFacadeError("file policy generation changed", code="policy_generation_changed")
        owner, previous_receipt = self._reserve_operation(context, operation, digest, context.policy_generation, pending_receipt)
        if not owner:
            if str((previous_receipt or {}).get("_workspace_id") or "") != str(context.workspace_id or "default"):
                raise FilesFacadeError("attachment operation is unavailable", code="resource_unavailable")
            # A provider may have durably committed its preparation before a
            # response was lost. Recover that typed preparation rather than
            # returning the facade's transient pending envelope.
            recovery_owner = target_adapter or target_provider
            recovery = getattr(recovery_owner, "attachment_status", None) if recovery_owner is not None else None
            # TreeHouse's established adapter uses the generic operation
            # status spelling; Files providers use the typed attachment
            # spelling so Copal can return its complete preparation DTO.
            if not callable(recovery) and target_adapter is not None:
                recovery = getattr(recovery_owner, "operation_status", None)
            if callable(recovery):
                recovered = await recovery(context, operation_id=operation)
                if isinstance(recovered, Mapping) and recovered.get("preparation_receipt_id"):
                    receipt = _validated_preparation(recovered, operation_id=operation, expected_generation=context.policy_generation)
                    receipt["generation"] = int(context.policy_generation)
                    self._save_operation(context, operation, digest, context.policy_generation, receipt)
                    return receipt
            return _public_operation(previous_receipt or pending_receipt)
        source_ref = None
        source_provider = None
        source_entry = None
        resolved_source: dict[str, Any] = dict(source)
        try:
            if source.get('kind') == 'client_upload':
                upload_revision = {'kind':'contentDigest','value':source['sha256']}
                class UploadSource:
                    name = 'client_upload'
                    async def stat(self, _context, *, origin_id):
                        return ProviderResource(origin_id, source['name'], 'file', ('stat','read'), mime_type=source['mime'], size=source['size'], revision=upload_revision)
                    async def content(self, _context, *, origin_id):
                        return ProviderContent(origin_id, source['name'], source['mime'], data=upload_bytes, size=source['size'])
                source_provider = UploadSource()
                class UploadRef:
                    origin_id = 'client-upload'
                source_ref = UploadRef()
                source_entry = await source_provider.stat(context, origin_id=source_ref.origin_id)
                resolved_source = {**source, 'expected_revision':upload_revision}
            if source.get("import_receipt_id"):
                imported = self._load_operation(context, str(source["import_receipt_id"]).strip())
                if imported is None or imported.get("owner") != context.owner_subject_id:
                    raise FilesFacadeError("import receipt is unavailable", code="resource_unavailable")
                imported_receipt = imported.get("receipt") if isinstance(imported.get("receipt"), Mapping) else {}
                imported_workspace = str(imported_receipt.get("_workspace_id") or "").strip()
                if not imported_workspace or imported_workspace != str(context.workspace_id or "default"):
                    raise FilesFacadeError("import receipt is unavailable", code="resource_unavailable")
                if int(imported.get("generation", -1)) != int(context.policy_generation):
                    raise FilesFacadeError("import receipt is unavailable", code="resource_unavailable")
                imported_items = imported_receipt.get("items", [])
                imported_item = next((item for item in imported_items if str(item.get("item_id")) == str(source.get("item_id") or "")), None)
                if not isinstance(imported_item, Mapping) or imported_item.get("outcome") != "committed" or not imported_item.get("resource_ref"):
                    raise FilesFacadeError("import receipt is not ready", code="resource_unavailable")
                resolved_source = {"resource_ref": imported_item["resource_ref"], "expected_revision": source.get("expected_revision")}
            if resolved_source.get("resource_ref"):
                source_provider, source_ref = self._provider_for_ref(context, str(resolved_source["resource_ref"]), capability="stat")
                source_entry = await source_provider.stat(context, origin_id=source_ref.origin_id)
                if not self._generation_current(context):
                    raise FilesFacadeError("file policy generation changed", code="policy_generation_changed")
                if not {"read", "download", "open", "copy"}.intersection(source_entry.capabilities):
                    raise FilesFacadeError("attachment source is unreadable", code="resource_unavailable")
                self._check_revision(source_entry, resolved_source.get("expected_revision"))
            if not self._generation_current(context):
                raise FilesFacadeError("file policy generation changed", code="policy_generation_changed")
        except FilesFacadeError as error:
            failed = {"operation_id": operation, "generation": int(context.policy_generation), "state": "partial", "items": [{"item_id": operation, "outcome": "failed", "code": error.code}]}
            self._save_operation(context, operation, digest, context.policy_generation, failed)
            raise
        except Exception:
            failed = {"operation_id": operation, "generation": int(context.policy_generation), "state": "partial", "items": [{"item_id": operation, "outcome": "failed", "code": "provider_unavailable"}]}
            self._save_operation(context, operation, digest, context.policy_generation, failed)
            return failed
        # Materialization is owned by the target provider.  This permits a
        # Host source to become a Copal asset without granting the Host
        # adapter authority to mutate a Copal document.  A registered target
        # adapter (TreeHouse, Host media) owns the representation instead of
        # the provider whenever one is bound to the target kind.
        preparer_owner = target_adapter if target_adapter is not None else target_provider
        preparer = getattr(preparer_owner, "prepare_attachment", None) if preparer_owner is not None else None
        if not callable(preparer):
            failed = {"operation_id": operation, "generation": int(context.policy_generation), "state": "partial", "items": [{"item_id": operation, "outcome": "failed", "code": "unsupported_provider_kind"}]}
            self._save_operation(context, operation, digest, context.policy_generation, failed)
            raise FilesFacadeError("attachment representation is unavailable", code="unsupported_provider_kind")
        try:
            if target_adapter is not None:
                result = await preparer(
                    context,
                    source=resolved_source,
                    target=target,
                    mode=normalized_mode,
                    operation_id=operation,
                    source_provider=source_provider,
                    source_origin_id=source_ref.origin_id if source_ref is not None else None,
                    source_entry=source_entry,
                    target_origin_id=target_ref.origin_id if target_ref is not None else None,
                    **({"selected_root_current":selected_root_current} if target_kind == "host_document" and root_revision is not None else {}),
                )
            elif target_provider is not None and (
                target_provider is not source_provider
                or str(getattr(target_provider, "name", "")) == "copal"
            ):
                result = await preparer(
                    context,
                    source=resolved_source,
                    target=target,
                    mode=normalized_mode,
                    operation_id=operation,
                    source_provider=source_provider,
                    source_origin_id=source_ref.origin_id if source_ref is not None else None,
                    source_entry=source_entry,
                    target_origin_id=target_ref.origin_id if target_ref is not None else None,
                )
            else:
                result = await preparer(context, source=resolved_source, target=target, mode=normalized_mode, operation_id=operation)
            if not self._generation_current(context):
                raise FilesFacadeError("attachment outcome requires reconciliation", code="operation_pending" if target_kind == "host_document" else "policy_generation_changed")
        except FilesFacadeError as error:
            if error.code == "operation_pending":
                raise
            failed = {"operation_id": operation, "generation": int(context.policy_generation), "state": "partial", "items": [{"item_id": operation, "outcome": "failed", "code": error.code}]}
            self._save_operation(context, operation, digest, context.policy_generation, failed)
            raise
        except Exception:
            if target_kind == "host_document":
                raise FilesFacadeError("attachment outcome requires reconciliation", code="operation_pending")
            failed = {"operation_id": operation, "generation": int(context.policy_generation), "state": "partial", "items": [{"item_id": operation, "outcome": "failed", "code": "provider_unavailable"}]}
            self._save_operation(context, operation, digest, context.policy_generation, failed)
            return failed
        if not isinstance(result, Mapping):
            if target_kind == "host_document":
                raise FilesFacadeError("attachment outcome requires reconciliation", code="operation_pending")
            failed = {"operation_id": operation, "generation": int(context.policy_generation), "state": "partial", "items": [{"item_id": operation, "outcome": "failed", "code": "provider_unavailable"}]}
            self._save_operation(context, operation, digest, context.policy_generation, failed)
            raise FilesFacadeError("provider returned an invalid attachment preparation", code="provider_unavailable")
        try:
            receipt = _validated_preparation(result, operation_id=operation, expected_generation=context.policy_generation)
        except FilesFacadeError as error:
            if target_kind == "host_document":
                raise FilesFacadeError("attachment outcome requires reconciliation", code="operation_pending") from error
            failed = {"operation_id": operation, "generation": int(context.policy_generation), "state": "partial", "items": [{"item_id": operation, "outcome": "failed", "code": error.code}]}
            self._save_operation(context, operation, digest, context.policy_generation, failed)
            raise
        if not self._generation_current(context):
            if target_kind == "host_document":
                raise FilesFacadeError("attachment outcome requires reconciliation", code="operation_pending")
            failed = {"operation_id": operation, "generation": int(context.policy_generation), "state": "partial", "items": [{"item_id": operation, "outcome": "stale", "code": "policy_generation_changed"}]}
            self._save_operation(context, operation, digest, context.policy_generation, failed)
            raise FilesFacadeError("file policy generation changed", code="policy_generation_changed")
        receipt["generation"] = int(context.policy_generation)
        self._save_operation(context, operation, digest, context.policy_generation, receipt)
        return receipt

    async def query_base_resource(self, context: ProviderContext, *, base_ref: str, expected_revision: Mapping[str, Any] | None, corpus_ref: str, generation: int, view_id: str | None = None, query: Mapping[str, Any] | None = None, page: int = 0, page_size: int = 100, context_ref: str | None = None, draft_definition: str | None = None) -> dict[str, Any]:
        requested_generation = _strict_nonnegative_int(generation, "generation")
        if requested_generation != int(context.policy_generation):
            raise FilesFacadeError("file policy generation is stale", code="resource_ref_stale")
        requested_page = _strict_nonnegative_int(page, "page")
        requested_page_size = _strict_nonnegative_int(page_size, "page_size")
        if requested_page_size < 1 or requested_page_size > 500:
            raise FilesFacadeError("Base page size is invalid", code="invalid_resource_request")
        base_provider, base = self._provider_for_ref(context, base_ref, capability="read")
        corpus_provider, corpus = self._provider_for_ref(context, corpus_ref, capability="children")
        context_origin_id = None
        if context_ref:
            context_provider, context_resource = self._provider_for_ref(context, context_ref, capability="stat")
            if context_provider is not base_provider:
                raise FilesFacadeError("Base context provider is unsupported", code="unsupported_provider_kind")
            context_origin_id = context_resource.origin_id
        if base_provider is not corpus_provider:
            raise FilesFacadeError("Base corpus provider is unsupported", code="unsupported_provider_kind")
        base_entry = await base_provider.stat(context, origin_id=base.origin_id)
        corpus_entry = await corpus_provider.stat(context, origin_id=corpus.origin_id)
        if not self._generation_current(context):
            raise FilesFacadeError("file policy generation changed", code="policy_generation_changed")
        self._check_revision(base_entry, expected_revision)
        query_fn = getattr(base_provider, "query_base", None)
        if not callable(query_fn):
            raise FilesFacadeError("Base corpus query is unavailable", code="unsupported_provider_kind")
        if not self._generation_current(context):
            raise FilesFacadeError("file policy generation changed", code="policy_generation_changed")
        result = await query_fn(context, base_origin_id=base.origin_id, corpus_origin_id=corpus.origin_id, view_id=view_id, query=dict(query or {}), page=requested_page, page_size=requested_page_size, context_origin_id=context_origin_id, draft_definition=draft_definition)
        if not isinstance(result, Mapping):
            raise FilesFacadeError("provider returned an invalid Base result", code="provider_unavailable")
        if not self._generation_current(context):
            raise FilesFacadeError("file policy generation changed", code="policy_generation_changed")
        rows = result.get("rows") or []
        if not isinstance(rows, Sequence) or len(rows) > 500:
            raise FilesFacadeError("Base result is outside the allowed bound", code="provider_unavailable")
        safe_rows: list[dict[str, Any]] = []
        for raw_row in rows:
            if not isinstance(raw_row, Mapping):
                continue
            row = {}
            for key in ("logical_path", "metadata", "properties", "relations", "revision", "kind", "capabilities"):
                if key in raw_row:
                    value = raw_row[key]
                    if key == "metadata" and isinstance(value, Mapping):
                        value = {str(k): value[k] for k in ("name", "kind", "mime_type", "size", "modified_unix_ms", "capabilities") if k in value}
                    elif key == "properties":
                        value = {}
                    elif key == "relations":
                        value = []
                    row[key] = value
            row_ref = str(raw_row.get("resource_ref") or "").strip()
            row_origin = str(raw_row.get("origin_id") or "").strip()
            if row_ref:
                try:
                    _row_provider, decoded = self._provider_for_ref(context, row_ref, capability="stat")
                    row["resource_key"] = decoded.stable_id
                    row["resource_ref"] = row_ref
                except FilesFacadeError:
                    continue
            elif row_origin:
                try:
                    metadata = raw_row.get("metadata") if isinstance(raw_row.get("metadata"), Mapping) else {}
                    kind = str(raw_row.get("kind") or metadata.get("kind") or "file").strip().lower()
                    raw_caps = raw_row.get("capabilities") or metadata.get("capabilities") or (("children", "stat") if kind in _DIRECTORY_KINDS else ("stat", "read"))
                    capabilities = tuple(str(cap).strip().lower() for cap in raw_caps if str(cap).strip())
                    if kind not in RESOURCE_KINDS or not capabilities or not set(capabilities).issubset(RESOURCE_CAPABILITIES):
                        continue
                    row["resource_key"] = stable_resource_id(owner_subject_id=context.owner_subject_id, provider=base_provider.name, origin_id=row_origin)
                    row["resource_ref"] = issue_resource_ref(owner_subject_id=context.owner_subject_id, provider=base_provider.name, origin_id=row_origin, kind=kind, capabilities=capabilities, policy_generation=context.policy_generation).token
                except FilesFacadeError:
                    continue
            safe_rows.append(row)
        return {**dict(result), "rows": safe_rows, "generation": int(context.policy_generation), "corpus": {"resource_key": corpus.stable_id, "resource_ref": self._resource(context, corpus.provider, corpus_entry)["ref"]}, "base": {"resource_key": base.stable_id, "resource_ref": self._resource(context, base.provider, base_entry)["ref"]}}

    def _resource(
        self,
        context: ProviderContext,
        provider: str,
        entry: ProviderResource,
        *,
        parent_stable_id: str | None = None,
    ) -> dict[str, Any]:
        ref = issue_resource_ref(
            owner_subject_id=context.owner_subject_id,
            provider=provider,
            origin_id=entry.origin_id,
            kind=entry.kind,
            capabilities=entry.capabilities,
            policy_generation=context.policy_generation,
            location_id=entry.location_id,
            workspace_id=entry.workspace_id,
            parent_stable_id=parent_stable_id,
        )
        public = ref.public_dict()
        sort_kind = "".join(
            character for character in str(entry.sort_kind or "")[:128]
            if ord(character) >= 32 and ord(character) != 127
        )
        child_sort_keys = (
            _supported_sort_keys(self._providers[provider], parent_origin_id=entry.origin_id)
            if "children" in entry.capabilities
            else ()
        )
        public.update(
            {
                "name": str(entry.name),
                "mime_type": entry.mime_type,
                "size": entry.size,
                "modified_unix_ms": entry.modified_unix_ms,
                "created_unix_ms": entry.created_unix_ms,
                "provenance": _public_provenance(entry.provenance),
                "download_name": entry.download_name,
                "preview_kind": entry.preview_kind,
                "native_thumbnail_available": entry.native_thumbnail_available,
                "native_icon_available": entry.native_icon_available,
                "sort_kind": sort_kind or None,
                "sort_keys": list(child_sort_keys),
            }
        )
        revision = self._provider_revision(entry)
        if revision is not None:
            public["revision"] = dict(revision)
        from src.openclank.format_capabilities import project_format_capability
        public["format"] = project_format_capability(
            resource_ref=ref.token, policy_generation=context.policy_generation,
            provider_revision=revision, kind=entry.kind, name=entry.name,
            mime_type=entry.mime_type, preview_kind=entry.preview_kind,
            capabilities=entry.capabilities, native_thumbnail_available=entry.native_thumbnail_available, provider=provider,
        )
        return public

    async def retained_checkpoint_restore_source(self, context: ProviderContext, *, request_id: str, resource_id: str,
                                                 auth, barrier, history_supervisor, current):
        """Reauthorize one exact saved checkpoint without old browser refs."""
        from src.openclank.checkpoint_restore_source import retained_checkpoint_source
        return await retained_checkpoint_source(self, context, request_id=request_id, resource_id=resource_id,
            auth=auth, barrier=barrier, history_supervisor=history_supervisor, current=current)

    async def checkpoint_comparison_source(self, context: ProviderContext, *, root_ref: str, original_root_revision: Mapping[str, Any],
            items: Sequence[Mapping[str, Any]], request_id: str, resource_id: str, auth, barrier, history_supervisor, current):
        """Private request-local handoff, bound to the original complete action."""
        from src.openclank.history_client import HistoryClient
        async def checked():
            private=[]
            summary=await self.selected_checkpoint_operation(context,root_ref=root_ref,root_revision=original_root_revision,
                items=items,request_id=request_id,auth=auth,barrier=barrier,history_supervisor=history_supervisor,current=current,
                status_only=True,persist_status=False,receipt_out=private)
            return summary,private[0] if private else None
        summary,receipt=await checked()
        if summary.get("state")!="complete" or summary.get("current_mapping")!="current":raise FilesFacadeError("Checkpoint comparison unavailable",code="resource_unavailable")
        positions=[i for i,row in enumerate(summary.get("items") or []) if row.get("resource_id")==resource_id]
        if len(positions)!=1:raise FilesFacadeError("Checkpoint comparison unavailable",code="resource_unavailable")
        index=positions[0]
        binding=receipt.get("_binding") if isinstance(receipt,Mapping) else None
        native=receipt.get("_native") if isinstance(receipt,Mapping) else None
        if (not isinstance(binding,Mapping) or not isinstance(binding.get("items"),list) or index>=len(binding["items"])
            or not isinstance(native,Mapping) or native.get("state")!="complete" or not isinstance(native.get("items"),list) or index>=len(native["items"])):
            raise FilesFacadeError("Checkpoint comparison unavailable",code="resource_unavailable")
        original=dict(binding["items"][index]);version=dict(native["items"][index])
        if (set(original)!={"resource_id","fingerprint"} or set(version)!={"resource_id","version_ref","fingerprint","size"}
            or version["resource_id"]!=original["resource_id"] or version["fingerprint"]!=original["fingerprint"]):
            raise FilesFacadeError("Checkpoint comparison unavailable",code="resource_unavailable")
        provider,root=self._provider_for_ref(context,root_ref,capability="children")
        selected_provider,selected=self._provider_for_ref(context,items[index]["resource_ref"],capability="download")
        if selected_provider is not provider or selected.provider!="host" or selected.kind!="file":raise FilesFacadeError("Checkpoint comparison unavailable",code="resource_unavailable")
        current_root=await provider.stat(context,origin_id=root.origin_id)
        if current_root.origin_id!=root.origin_id or not current_root.revision:raise FilesFacadeError("Checkpoint comparison unavailable",code="resource_unavailable")
        authorized=await provider.authorize_checkpoint_root(context,origin_id=root.origin_id,expected_revision=current_root.revision)
        if any(binding.get(k)!=authorized.get(k) for k in ("root_id","history_actor","history_workspace")):raise FilesFacadeError("Checkpoint comparison unavailable",code="resource_unavailable")
        process=history_supervisor.process
        history=HistoryClient(authorized["history_socket"],actor_id=authorized["history_actor"],account_id=context.owner_subject_id,
            token=history_supervisor.credential_token(authorized["history_actor"],context.owner_subject_id),timeout=1.5)
        async def still_current():
            try:
                if (not await current() or auth.account_id(context.owner_username)!=context.owner_subject_id
                    or auth.username_for_account_id(context.owner_subject_id)!=context.owner_username or not self._generation_current(context)
                    or process is None or history_supervisor.process is not process or process.poll() is not None):return False
                p,r=self._provider_for_ref(context,root_ref,capability="children")
                if p is not provider or r.origin_id!=root.origin_id:return False
                for item in items:
                    candidate,ref=self._provider_for_ref(context,item["resource_ref"],capability="download")
                    if candidate is not provider or ref.provider!="host" or ref.kind!="file":return False
                observed_root=await provider.stat(context,origin_id=root.origin_id)
                if observed_root.origin_id!=root.origin_id or not observed_root.revision:return False
                current_root=await provider.authorize_checkpoint_root(context,origin_id=root.origin_id,expected_revision=observed_root.revision)
                return all(binding.get(k)==current_root.get(k) for k in ("root_id","history_actor","history_workspace"))
            except Exception:return False
        async def source_current():
            try:
                if not await still_current():return False
                value,latest_receipt=await checked()
                if value.get("state")!="complete" or value.get("current_mapping")!="current":return False
                return bool(isinstance(latest_receipt,Mapping) and latest_receipt.get("_binding")==binding and latest_receipt.get("_native")==native and await still_current())
            except Exception:return False
        async def open_current():
            if not await still_current():raise FilesFacadeError("Checkpoint comparison unavailable",code="resource_unavailable")
            p,ref=self._provider_for_ref(context,items[index]["resource_ref"],capability="download")
            if p is not provider or ref.origin_id!=selected.origin_id:raise FilesFacadeError("Checkpoint comparison unavailable",code="resource_unavailable")
            observed=await provider.stat_inspection(context,origin_id=ref.origin_id)
            if (observed.origin_id!=ref.origin_id or observed.kind!="file" or type(observed.size) is not int or observed.size<0
                or not isinstance(observed.revision,Mapping) or observed.revision.get("kind")!="hostObjectIdentity"
                or not re.fullmatch(r"[0-9a-f]{64}",str(observed.revision.get("value") or ""))):raise FilesFacadeError("Checkpoint comparison unavailable",code="resource_unavailable")
            if observed.size>256*1024:
                return {"oversize":True,"origin_id":ref.origin_id,"size":observed.size,"etag":observed.revision["value"]}
            content=await provider.content(context,origin_id=ref.origin_id)
            if content.origin_id!=ref.origin_id or content.size!=observed.size or content.etag!=observed.revision["value"]:
                raise FilesFacadeError("Checkpoint comparison unavailable",code="resource_unavailable")
            return content
        async def current_file(content):
            try:
                if isinstance(content,Mapping):
                    if set(content)!={"oversize","origin_id","size","etag"} or content.get("oversize") is not True:return False
                    origin,size,tag=content.get("origin_id"),content.get("size"),content.get("etag")
                else:origin,size,tag=content.origin_id,content.size,content.etag
                if not await still_current() or origin!=selected.origin_id or not re.fullmatch(r"[0-9a-f]{64}",str(tag or "")) or type(size) is not int:return False
                observed=await provider.stat_inspection(context,origin_id=selected.origin_id)
                return bool(observed.origin_id==selected.origin_id and observed.kind=="file" and isinstance(observed.revision,Mapping)
                    and observed.revision.get("kind")=="hostObjectIdentity" and observed.revision.get("value")==tag
                    and observed.size==size and await still_current())
            except Exception:return False
        return {"resource_id":resource_id,"request_id":request_id,"native_resource_id":version["resource_id"],"version_ref":version["version_ref"],
            "fingerprint":version["fingerprint"],"size":version["size"],"history":history,"open_current":open_current,"current":still_current,"source_current":source_current,"current_file":current_file}

    async def selected_checkpoint_operation(self, context: ProviderContext, *, root_ref: str, root_revision: Mapping[str, Any],
            items: Sequence[Mapping[str, Any]], request_id: str, auth, barrier, history_supervisor, current, status_only: bool=False,
            persist_status: bool=True, receipt_out=None):
        """One public operation identity; native binding and receipt remain private."""
        if (not isinstance(request_id,str) or not re.fullmatch(r"[A-Za-z0-9_-]{16,80}",request_id)
            or not isinstance(items,(list,tuple)) or not 1<=len(items)<=16 or not callable(current)
            or auth is None or barrier is None or history_supervisor is None):raise FilesFacadeError("Checkpoint unavailable",code="resource_unavailable")
        if auth.account_id(context.owner_username)!=context.owner_subject_id or auth.username_for_account_id(context.owner_subject_id)!=context.owner_username:
            raise FilesFacadeError("Checkpoint unavailable",code="resource_unavailable")
        provider,root=self._provider_for_ref(context,root_ref,capability="children")
        if root.provider!="host" or root.kind not in {"folder","directory"}:raise FilesFacadeError("Checkpoint unavailable",code="resource_unavailable")
        selected=[];public=[];original=[]
        for item in items:
            if type(item) is not dict or set(item)!={"resource_ref","expected_revision"} or not isinstance(item["expected_revision"],Mapping):raise FilesFacadeError("Checkpoint unavailable",code="resource_unavailable")
            p,ref=self._provider_for_ref(context,item["resource_ref"],capability="download")
            if p is not provider or ref.provider!="host" or ref.kind!="file" or any(row["origin_id"]==ref.origin_id for row in selected):raise FilesFacadeError("Checkpoint unavailable",code="resource_unavailable")
            selected.append({"origin_id":ref.origin_id,"expected_revision":dict(item["expected_revision"])})
            public.append(stable_resource_id(owner_subject_id=context.owner_subject_id,provider="host",origin_id=ref.origin_id));original.append((item["resource_ref"],ref.origin_id))
        async def current_refs():
            if not await current():return False
            try:
                p,r=self._provider_for_ref(context,root_ref,capability="children")
                if p is not provider or r.origin_id!=root.origin_id:return False
                return all((lambda pair:pair[0] is provider and pair[1].origin_id==origin)(self._provider_for_ref(context,token,capability="download")) for token,origin in original)
            except Exception:return False
        if not await current_refs():raise FilesFacadeError("Checkpoint unavailable",code="resource_unavailable")
        canonical={"version":1,"kind":"selected_checkpoint_v1","owner":context.owner_subject_id,"workspace":context.workspace_id,
            "generation":context.policy_generation,"root":root.origin_id,"root_revision":dict(root_revision),"items":selected}
        def hash_request():return hashlib.sha256(json.dumps(canonical,sort_keys=True,separators=(",",":"),ensure_ascii=False).encode()).hexdigest()
        digest=hash_request();store=self._operation_store
        if store is None:raise FilesFacadeError("Checkpoint unavailable",code="provider_unavailable")
        def project(receipt, mapping=None):
            if not isinstance(receipt,Mapping) or receipt.get("kind")!="selected_checkpoint_v1":raise FilesFacadeError("Checkpoint unavailable",code="resource_unavailable")
            native=receipt.get("_native") or {}; state=native.get("state") if isinstance(native,Mapping) else None
            if state not in {"pending","failed","complete"}:state="pending"
            out={"request_id":request_id,"state":state,"items":[{"resource_id":value} for value in public],"current_mapping":"current" if mapping is True else "unavailable"}
            if state=="complete":
                out.update(consistency=native.get("consistency"),quiescence=native.get("quiescence"),started_millis=native.get("started_millis"),finished_millis=native.get("finished_millis"))
            return out
        async def status(saved):
            receipt=dict(saved.get("receipt") or {});binding=receipt.get("_binding");mapping=None
            if binding:
                try:
                    native,mapping=await provider.read_selected_checkpoint_status(context,root_origin_id=root.origin_id,root_revision=root_revision,
                        items=selected,request_id=request_id,binding=binding,auth=auth,barrier=barrier,history_supervisor=history_supervisor,current=current_refs)
                    if native.get("state") in {"complete","failed"}:
                        receipt["_native"]=dict(native)
                        if persist_status:
                            store.record_operation(owner_subject_id=context.owner_subject_id,operation_id=request_id,request_digest=saved["digest"],generation=saved["generation"],receipt=receipt,phase="selected_checkpoint")
                except Exception:pass
            if not await current_refs():raise FilesFacadeError("Checkpoint unavailable",code="resource_unavailable")
            if receipt_out is not None:receipt_out.append(dict(receipt))
            return project(receipt,mapping)
        if status_only:
            saved=store.get_operation(owner_subject_id=context.owner_subject_id,operation_id=request_id)
            if not saved:raise FilesFacadeError("Checkpoint unavailable",code="resource_unavailable")
            canonical["generation"]=saved.get("generation")
            if saved.get("digest")!=hash_request():raise FilesFacadeError("Checkpoint unavailable",code="resource_unavailable")
            return await status(saved)
        initial={"kind":"selected_checkpoint_v1","_native":{"state":"pending"}}
        try:existing=store.reserve_operation(owner_subject_id=context.owner_subject_id,operation_id=request_id,request_digest=digest,generation=context.policy_generation,receipt=initial,phase="selected_checkpoint")
        except Exception as exc:raise FilesFacadeError("Checkpoint unavailable",code=getattr(exc,"code","provider_unavailable")) from exc
        if existing is not None:return await status(existing)
        async def bind(binding):
            initial["_binding"]=dict(binding)
            store.record_operation(owner_subject_id=context.owner_subject_id,operation_id=request_id,request_digest=digest,generation=context.policy_generation,receipt=initial,phase="selected_checkpoint")
        async def run():
            native=await self.capture_selected_checkpoint(context,root_ref=root_ref,root_revision=root_revision,items=items,request_id=request_id,
                auth=auth,barrier=barrier,history_supervisor=history_supervisor,current=current,bind=bind)
            if native.get("state") in {"complete","failed"}:
                initial["_native"]=dict(native)
                store.record_operation(owner_subject_id=context.owner_subject_id,operation_id=request_id,request_digest=digest,generation=context.policy_generation,receipt=initial,phase="selected_checkpoint")
            return native
        task=asyncio.create_task(run())
        try:await asyncio.shield(task)
        except asyncio.CancelledError:
            task.add_done_callback(lambda done:done.exception() if not done.cancelled() else None);raise
        except Exception:pass
        saved=store.get_operation(owner_subject_id=context.owner_subject_id,operation_id=request_id)
        return await status(saved) if saved else project(initial)

    async def register_selected_checkpoint_files(self, context: ProviderContext, *, root_ref: str,
            root_revision: Mapping[str, Any], items: Sequence[Mapping[str, Any]], request_id: str,
            auth, barrier, history_supervisor, current, status_only: bool = False) -> Mapping[str, Any]:
        """Reserve one explicit set; repeat requests can observe only the original receipt."""
        if (not isinstance(request_id,str) or not re.fullmatch(r"[A-Za-z0-9_-]{16,80}",request_id)
            or not isinstance(items,(list,tuple)) or not 1<=len(items)<=16 or not callable(current)
            or auth is None or barrier is None or history_supervisor is None):
            raise FilesFacadeError("Registration unavailable",code="resource_unavailable")
        if auth.account_id(context.owner_username)!=context.owner_subject_id or auth.username_for_account_id(context.owner_subject_id)!=context.owner_username:
            raise FilesFacadeError("Registration unavailable",code="resource_unavailable")
        provider,root=self._provider_for_ref(context,root_ref,capability="children")
        if root.provider!="host" or root.kind not in {"folder","directory"} or not callable(getattr(provider,"register_selected_checkpoint_files",None)):
            raise FilesFacadeError("Registration unavailable",code="resource_unavailable")
        selected=[];public=[];original=[]
        for item in items:
            if type(item) is not dict or set(item)!={"resource_ref","expected_revision"}:
                raise FilesFacadeError("Registration unavailable",code="resource_unavailable")
            child_provider,ref=self._provider_for_ref(context,item["resource_ref"],capability="download")
            if child_provider is not provider or ref.provider!="host" or ref.kind!="file" or any(x["origin_id"]==ref.origin_id for x in selected):
                raise FilesFacadeError("Registration unavailable",code="resource_unavailable")
            selected.append({"origin_id":ref.origin_id,"expected_revision":dict(item["expected_revision"])})
            public.append(stable_resource_id(owner_subject_id=context.owner_subject_id,provider="host",origin_id=ref.origin_id))
            original.append((item["resource_ref"],ref.origin_id))
        async def current_refs():
            if not await current():return False
            try:
                p,r=self._provider_for_ref(context,root_ref,capability="children")
                if p is not provider or r.origin_id!=root.origin_id:return False
                return all((lambda pair:pair[0] is provider and pair[1].origin_id==origin)(self._provider_for_ref(context,token,capability="download")) for token,origin in original)
            except Exception:return False
        if not await current_refs():raise FilesFacadeError("Registration unavailable",code="resource_unavailable")
        canonical={"version":1,"owner":context.owner_subject_id,"workspace":context.workspace_id,"generation":context.policy_generation,
            "root":root.origin_id,"root_revision":dict(root_revision),"items":selected}
        digest=hashlib.sha256(json.dumps(canonical,sort_keys=True,separators=(",",":"),ensure_ascii=False).encode("utf-8")).hexdigest()
        store=self._operation_store
        if store is None:raise FilesFacadeError("Registration unavailable",code="provider_unavailable")
        def project(receipt):
            if not isinstance(receipt,Mapping) or receipt.get("kind")!="checkpoint_registration_v1":raise FilesFacadeError("Registration unavailable",code="resource_unavailable")
            rows=receipt.get("items") or []
            if not isinstance(rows,list) or len(rows)>len(public):raise FilesFacadeError("Registration unavailable",code="resource_unavailable")
            return {"request_id":request_id,"state":str(receipt.get("state") or "pending"),"items":[{"resource_id":public[i],"state":str(rows[i].get("state") or "pending") if i<len(rows) and isinstance(rows[i],Mapping) else "pending"} for i in range(len(public))]}
        if status_only:
            saved=store.get_operation(owner_subject_id=context.owner_subject_id,operation_id=request_id)
            if not saved:raise FilesFacadeError("Registration unavailable",code="resource_unavailable")
            canonical["generation"]=saved.get("generation")
            original_digest=hashlib.sha256(json.dumps(canonical,sort_keys=True,separators=(",",":"),ensure_ascii=False).encode("utf-8")).hexdigest()
            if saved.get("digest")!=original_digest:raise FilesFacadeError("Registration unavailable",code="resource_unavailable")
            result=project(saved.get("receipt"))
            try:
                observed=await provider.observe_selected_checkpoint_registration(context,root_origin_id=root.origin_id,root_revision=root_revision,items=selected,
                    auth=auth,barrier=barrier,history_supervisor=history_supervisor,current=current_refs)
                result["observed"]=[{"resource_id":public[i],"state":value} for i,value in enumerate(observed)]
            except Exception:
                result["observed"]=None
            return result
        initial={"kind":"checkpoint_registration_v1","state":"pending","items":[]}
        try: existing=store.reserve_operation(owner_subject_id=context.owner_subject_id,operation_id=request_id,request_digest=digest,
            generation=context.policy_generation,receipt=initial,phase="checkpoint_registration")
        except Exception as exc:raise FilesFacadeError("Registration unavailable",code=getattr(exc,"code","provider_unavailable")) from exc
        if existing is not None:return project(existing.get("receipt"))
        async def update(rows, *, state="pending"):
            store.record_operation(owner_subject_id=context.owner_subject_id,operation_id=request_id,request_digest=digest,
                generation=context.policy_generation,receipt={"kind":"checkpoint_registration_v1","state":state,"items":[dict(row) for row in rows]},phase="checkpoint_registration")
        await provider.register_selected_checkpoint_files(context,root_origin_id=root.origin_id,root_revision=root_revision,items=selected,
            auth=auth,barrier=barrier,history_supervisor=history_supervisor,current=current_refs,update=update)
        saved=store.get_operation(owner_subject_id=context.owner_subject_id,operation_id=request_id)
        return project(saved.get("receipt") if saved else None)

    async def capture_selected_checkpoint(self, context: ProviderContext, *, root_ref: str,
            root_revision: Mapping[str, Any], items: Sequence[Mapping[str, Any]], request_id: str,
            auth, barrier, history_supervisor, current, bind=None) -> Mapping[str, Any]:
        """Internal selected capture: sealed Files refs select; they never grant extra authority."""
        if not callable(current) or not isinstance(items,(list,tuple)) or not 1<=len(items)<=16:
            raise FilesFacadeError("Checkpoint unavailable",code="resource_unavailable")
        provider,root=self._provider_for_ref(context,root_ref,capability="children")
        if root.provider!="host" or root.kind not in {"folder","directory"} or not callable(getattr(provider,"capture_selected_checkpoint",None)):
            raise FilesFacadeError("Checkpoint root unavailable",code="resource_unavailable")
        selected=[];original=[]
        for item in items:
            if type(item) is not dict or set(item)!={"resource_ref","expected_revision"}:
                raise FilesFacadeError("Checkpoint selection unavailable",code="resource_unavailable")
            child_provider,ref=self._provider_for_ref(context,item["resource_ref"],capability="download")
            if child_provider is not provider or ref.provider!="host" or ref.kind!="file":
                raise FilesFacadeError("Checkpoint selection unavailable",code="resource_unavailable")
            selected.append({"origin_id":ref.origin_id,"expected_revision":item["expected_revision"]})
            original.append((item["resource_ref"],ref.origin_id))
        async def current_refs():
            if not await current():return False
            try:
                fresh_provider,fresh_root=self._provider_for_ref(context,root_ref,capability="children")
                if fresh_provider is not provider or fresh_root.origin_id!=root.origin_id:return False
                for token,origin in original:
                    child_provider,child=self._provider_for_ref(context,token,capability="download")
                    if child_provider is not provider or child.origin_id!=origin:return False
                return True
            except Exception:return False
        return await provider.capture_selected_checkpoint(context,root_origin_id=root.origin_id,root_revision=root_revision,
            items=selected,request_id=request_id,auth=auth,barrier=barrier,history_supervisor=history_supervisor,current=current_refs,bind=bind)

    def _provider_for_ref(
        self,
        context: ProviderContext,
        token: str,
        *,
        capability: str,
    ) -> tuple[FilesProvider, ResourceRef]:
        try:
            ref = resolve_resource_ref(
                token,
                expected_owner_subject_id=context.owner_subject_id,
                current_policy_generation=context.policy_generation,
                required_capability=capability,
            )
        except ResourceRefError as exc:
            raise FilesFacadeError(str(exc), code=exc.code) from exc
        provider = self._providers.get(ref.provider)
        if provider is None:
            raise FilesFacadeError("resource provider is unavailable", code="provider_unavailable")
        if ref.provider == "copal" and ref.workspace_id:
            origin_parts = str(ref.origin_id or "").split(":", 2)
            if len(origin_parts) < 2 or origin_parts[1] != ref.workspace_id:
                raise FilesFacadeError("resource provider identity is invalid", code="provider_unavailable")
            if ref.workspace_id != str(context.workspace_id or "default"):
                raise FilesFacadeError("resource is unavailable", code="resource_unavailable")
        return provider, ref

    @staticmethod
    def _is_exact_open(entry: ProviderResource) -> bool:
        target = entry.open_target if isinstance(entry.open_target, Mapping) else {}
        app = str(target.get("app") or "").strip().lower()
        return (
            (app == "copal_notes" and entry.kind in {"document", "note", "wiki"})
            or (app == "editor" and entry.kind == "file")
            or (app == "imps" and entry.kind == "image" and entry.preview_kind == "image")
            or (app == "document_editor" and entry.kind == "document")
            or (app == "chat" and entry.kind == "chat")
            or (app == "research" and entry.kind == "research")
        )

    async def reissue_exact(self, context: ProviderContext, *, resource_ref: str) -> dict[str, Any]:
        """Reauthorize an expired exact-view ref without reviving its authority.

        Only the three managed providers used by the exact Copal/Gallery/Library
        viewers participate. The sealed ref supplies an owner-bound identity;
        the provider must still stat that owner's current record and advertise
        the current ``stat``/``open`` capabilities before a new-generation ref
        is issued. Host paths and refs intentionally have no renewal lane here.
        """

        try:
            ref = resolve_resource_ref_for_reissue(
                resource_ref,
                expected_owner_subject_id=context.owner_subject_id,
                required_capability="open",
            )
        except ResourceRefError as exc:
            raise FilesFacadeError(str(exc), code=exc.code) from exc
        if ref.provider not in EXACT_REISSUE_PROVIDERS or "stat" not in ref.capabilities:
            raise FilesFacadeError("resource renewal is unavailable", code="resource_unavailable")
        provider = self._providers.get(ref.provider)
        if provider is None:
            raise FilesFacadeError("resource provider is unavailable", code="provider_unavailable")
        if ref.provider == "copal" and ref.workspace_id:
            origin_parts = str(ref.origin_id or "").split(":", 2)
            if len(origin_parts) < 2 or origin_parts[1] != ref.workspace_id:
                raise FilesFacadeError("resource provider identity is invalid", code="provider_unavailable")
        current = await provider.stat(context, origin_id=ref.origin_id)
        if (
            current.origin_id != ref.origin_id
            or "stat" not in current.capabilities
            or "open" not in current.capabilities
            or not self._is_exact_open(current)
        ):
            raise FilesFacadeError("resource renewal is unavailable", code="resource_unavailable")
        resource = self._resource(
            context,
            ref.provider,
            replace(current, provenance={}, thumbnail_url=None),
            parent_stable_id=ref.parent_stable_id,
        )
        resource.pop("provenance", None)
        return {"version": FACADE_VERSION, "resource": resource}

    async def roots(self, context: ProviderContext) -> dict[str, Any]:
        entries: list[dict[str, Any]] = []
        health: dict[str, dict[str, Any]] = {}
        for name, provider in self._providers.items():
            try:
                roots = await provider.roots(context)
                entries.extend(self._resource(context, name, entry) for entry in roots)
                health[name] = {"available": True}
            except Exception:
                # A broken provider cannot erase or disclose counts for peers.
                health[name] = {"available": False, "code": "provider_unavailable"}
        try:
            # Cheap gate only (pref on + helper is an executable regular file, same path as recording_child.readiness); prepare_recording still runs the full probe.
            from src.desktop_capture import capture_enabled
            if getattr(sys, "frozen", False):
                from src.runtime_paths import get_app_root
                helper = Path(get_app_root()) / "bin" / "openclank-native-capture-frame"
            else:
                helper = Path(__file__).resolve().parents[2] / "native/capture/target/release/openclank-native-capture-frame"
            helper_stat = helper.stat()
            screen_record = bool(capture_enabled(context.owner_username)) and stat.S_ISREG(helper_stat.st_mode) and bool(helper_stat.st_mode & 0o111)
        except Exception:
            screen_record = False
        return {
            "version": FACADE_VERSION,
            "policy_generation": context.policy_generation,
            "entries": entries,
            "providers": health,
            "import_capabilities": {
                "screen_record": screen_record,
                "host": {
                    "available": True,
                    "create_directory": True,
                    "max_chunk_bytes": FILES_SERVICE_MAX_CHUNK_BYTES,
                    "max_total_bytes": FILES_SERVICE_MAX_TOTAL_BYTES,
                }
            },
        }

    async def places(self, context: ProviderContext) -> dict[str, Any]:
        if self._place_repository is None:
            return {"version": FACADE_VERSION, "entries": []}
        try:
            stored = await asyncio.to_thread(
                self._place_repository.list_places,
                context.owner_subject_id,
                limit=24,
            )
        except Exception as exc:
            raise FilesFacadeError("Files places are unavailable", code="provider_unavailable") from exc
        entries: list[dict[str, Any]] = []
        for place in stored:
            provider = self._providers.get(str(place.provider))
            if provider is None:
                continue
            try:
                current = await provider.stat(context, origin_id=str(place.origin_id))
                resource = self._resource(context, str(place.provider), current)
            except Exception:
                # Revoked, missing, or temporarily unavailable places never
                # become an existence oracle and cannot hide valid peers.
                continue
            if resource["id"] != str(place.stable_resource_id):
                continue
            resource["place_id"] = str(place.id)
            entries.append(resource)
        return {"version": FACADE_VERSION, "entries": entries}

    async def save_place(self, context: ProviderContext, *, resource_ref: str) -> dict[str, Any]:
        if self._place_repository is None:
            raise FilesFacadeError("Files places are unavailable", code="provider_unavailable")
        provider, ref = self._provider_for_ref(context, resource_ref, capability="stat")
        current = await provider.stat(context, origin_id=ref.origin_id)
        if current.origin_id != ref.origin_id or "stat" not in current.capabilities:
            raise FilesFacadeError("resource capability is unavailable", code="resource_unavailable")
        try:
            place = await asyncio.to_thread(
                self._place_repository.save_place,
                owner_subject_id=context.owner_subject_id,
                provider=ref.provider,
                stable_resource_id=ref.stable_id,
                origin_id=ref.origin_id,
                kind=current.kind,
                display_name=current.name,
            )
        except Exception as exc:
            raise FilesFacadeError("Files place could not be saved", code="provider_unavailable") from exc
        resource = self._resource(context, ref.provider, current, parent_stable_id=ref.parent_stable_id)
        resource["place_id"] = str(place.id)
        return {"version": FACADE_VERSION, "resource": resource}

    async def remove_place(self, context: ProviderContext, *, place_id: str) -> dict[str, Any]:
        if self._place_repository is None:
            raise FilesFacadeError("Files places are unavailable", code="provider_unavailable")
        try:
            removed = await asyncio.to_thread(
                self._place_repository.remove_place,
                owner_subject_id=context.owner_subject_id,
                place_id=str(place_id),
            )
        except Exception as exc:
            raise FilesFacadeError("Files place could not be removed", code="provider_unavailable") from exc
        if not removed:
            raise FilesFacadeError("Files place is unavailable", code="resource_unavailable")
        return {"version": FACADE_VERSION, "removed": True}

    async def _touch_recent(
        self,
        context: ProviderContext,
        *,
        ref: ResourceRef,
        kind: str,
        display_name: str,
    ) -> None:
        """Best-effort recent metadata after a fully authorized user action.

        The repository stores the encrypted provider origin so a later listing
        can re-stat it. A preference-store outage must never turn a successful
        open/download into an application error.
        """

        if self._place_repository is None:
            return
        try:
            await asyncio.to_thread(
                self._place_repository.touch_recent,
                owner_subject_id=context.owner_subject_id,
                provider=ref.provider,
                stable_resource_id=ref.stable_id,
                origin_id=ref.origin_id,
                kind=str(kind),
                display_name=str(display_name),
            )
        except Exception:
            return

    async def recents(self, context: ProviderContext) -> dict[str, Any]:
        if self._place_repository is None:
            return {"version": FACADE_VERSION, "entries": []}
        try:
            stored = await asyncio.to_thread(
                self._place_repository.list_recents,
                context.owner_subject_id,
                limit=24,
            )
        except Exception as exc:
            raise FilesFacadeError("Files recents are unavailable", code="provider_unavailable") from exc
        entries: list[dict[str, Any]] = []
        for recent in stored:
            provider = self._providers.get(str(recent.provider))
            if provider is None:
                continue
            try:
                current = await provider.stat(context, origin_id=str(recent.origin_id))
                resource = self._resource(context, str(recent.provider), current)
            except Exception:
                continue
            if resource["id"] != str(recent.stable_resource_id):
                continue
            resource["recent_id"] = str(recent.id)
            resource["accessed_unix_ms"] = int(recent.accessed_unix_ms)
            entries.append(resource)
        return {"version": FACADE_VERSION, "entries": entries}

    async def clear_recents(self, context: ProviderContext) -> dict[str, Any]:
        if self._place_repository is None:
            raise FilesFacadeError("Files recents are unavailable", code="provider_unavailable")
        try:
            removed = await asyncio.to_thread(
                self._place_repository.clear_recents,
                owner_subject_id=context.owner_subject_id,
            )
        except Exception as exc:
            raise FilesFacadeError("Files recents could not be cleared", code="provider_unavailable") from exc
        return {"version": FACADE_VERSION, "removed": int(removed)}

    async def saved_searches(self, context: ProviderContext) -> dict[str, Any]:
        if self._place_repository is None:
            return {"version": FACADE_VERSION, "entries": []}
        try:
            stored = await asyncio.to_thread(
                self._place_repository.list_saved_searches,
                context.owner_subject_id,
                limit=50,
            )
        except Exception as exc:
            raise FilesFacadeError("Saved searches are unavailable", code="provider_unavailable") from exc
        return {
            "version": FACADE_VERSION,
            "entries": [
                {
                    "id": row.id,
                    "name": row.name,
                    "provider": row.provider_scope,
                    "query": row.query,
                    "sort": dict(row.sort),
                    "updated_unix_ms": row.updated_unix_ms,
                }
                for row in stored
            ],
        }

    async def save_search(
        self,
        context: ProviderContext,
        *,
        name: str,
        provider: str,
        query: str,
        sort: Mapping[str, Any] | None,
    ) -> dict[str, Any]:
        if self._place_repository is None:
            raise FilesFacadeError("Saved searches are unavailable", code="provider_unavailable")
        normalized_provider = str(provider or "").strip().lower()
        if normalized_provider not in {"all", *self._providers.keys()}:
            raise FilesFacadeError("saved-search provider is unavailable")
        normalized_sort = _normalized_sort(sort)
        try:
            row = await asyncio.to_thread(
                self._place_repository.save_search,
                owner_subject_id=context.owner_subject_id,
                name=str(name),
                provider_scope=normalized_provider,
                query=str(query),
                sort=normalized_sort,
            )
        except FilePolicyError as exc:
            raise FilesFacadeError(str(exc), code=exc.code) from exc
        except Exception as exc:
            raise FilesFacadeError("Saved search could not be stored", code="provider_unavailable") from exc
        return {
            "version": FACADE_VERSION,
            "search": {
                "id": row.id,
                "name": row.name,
                "provider": row.provider_scope,
                "query": row.query,
                "sort": dict(row.sort),
                "updated_unix_ms": row.updated_unix_ms,
            },
        }

    async def remove_saved_search(self, context: ProviderContext, *, search_id: str) -> dict[str, Any]:
        if self._place_repository is None:
            raise FilesFacadeError("Saved searches are unavailable", code="provider_unavailable")
        try:
            removed = await asyncio.to_thread(
                self._place_repository.remove_saved_search,
                owner_subject_id=context.owner_subject_id,
                search_id=str(search_id),
            )
        except Exception as exc:
            raise FilesFacadeError("Saved search could not be removed", code="provider_unavailable") from exc
        if not removed:
            raise FilesFacadeError("Saved search is unavailable", code="resource_unavailable")
        return {"version": FACADE_VERSION, "removed": True}

    async def children(
        self,
        context: ProviderContext,
        *,
        parent_ref: str,
        cursor: str | None = None,
        limit: int = 100,
        sort: Mapping[str, Any] | None = None,
        query: str = "",
    ) -> dict[str, Any]:
        page_size = max(1, min(int(limit), MAX_PAGE_SIZE))
        normalized_sort = _normalized_sort(sort)
        normalized_query = str(query or "")[:512]
        provider, parent = self._provider_for_ref(context, parent_ref, capability="children")
        sort_keys = _supported_sort_keys(provider, parent_origin_id=parent.origin_id)
        if normalized_sort["key"] not in sort_keys:
            raise FilesFacadeError(
                f"{normalized_sort['key']} sort is unavailable for this folder",
                code="unsupported_sort",
            )
        provider_cursor = None
        provider_snapshot = None
        if cursor:
            provider_cursor, provider_snapshot = _open_cursor(
                cursor,
                context=context,
                provider=parent.provider,
                parent_id=parent.stable_id,
                sort=normalized_sort,
                sort_keys=sort_keys,
                query=normalized_query,
            )
        page = await provider.children(
            context,
            parent_origin_id=parent.origin_id,
            cursor=provider_cursor,
            snapshot=provider_snapshot,
            limit=page_size,
            sort=normalized_sort,
            query=normalized_query,
        )
        entries = [
            self._resource(context, parent.provider, entry, parent_stable_id=parent.stable_id)
            for entry in page.entries
        ]
        next_cursor = None
        if page.next_cursor is not None:
            now = int(time.time() * 1000)
            next_cursor = _seal_cursor(
                {
                    "v": FACADE_VERSION,
                    "owner": context.owner_subject_id,
                    "provider": parent.provider,
                    "parent": parent.stable_id,
                    "generation": int(context.policy_generation),
                    "sort": normalized_sort,
                    "sort_keys": list(sort_keys),
                    "query": normalized_query,
                    "cursor": str(page.next_cursor),
                    "snapshot": page.snapshot,
                    "iat": now,
                    "exp": now + CURSOR_TTL_SECONDS * 1000,
                }
            )
        result = {
            "version": FACADE_VERSION,
            "parent_id": parent.stable_id,
            "entries": entries,
            "next_cursor": next_cursor,
            "total": page.total,
            "snapshot": page.snapshot,
            "sort": normalized_sort,
            "sort_keys": list(sort_keys),
        }
        if page.complete is not None:
            result["search_complete"] = page.complete
        return result

    async def stat(self, context: ProviderContext, *, resource_ref: str, purpose: str | None = None) -> dict[str, Any]:
        provider, ref = self._provider_for_ref(context, resource_ref, capability="stat")
        if purpose == 'checkpoint':
            self._provider_for_ref(context,resource_ref,capability='download')
            method=getattr(provider,'stat_checkpoint',None)
            if ref.provider!='host' or not callable(method):raise FilesFacadeError('Checkpoint identity unavailable',code='resource_unavailable')
            entry=await method(context,origin_id=ref.origin_id)
        elif purpose == 'snapshot':
            self._provider_for_ref(context, resource_ref, capability='download')
            method=getattr(provider,'stat_inspection',None)
            if ref.provider!='host' or not callable(method):raise FilesFacadeError('Snapshot identity unavailable',code='resource_unavailable')
            entry=await method(context,origin_id=ref.origin_id)
            if entry.kind!='file':raise FilesFacadeError('Snapshot file unavailable',code='resource_unavailable')
        else:
            entry = await self._inspection_stat(provider, ref, context, allow_elf=purpose in {'inspect_elf','inspect_pe','inspect_macho','inspect_sqlite'}) if purpose in {'inspect', 'inspect_elf', 'inspect_pe', 'inspect_macho', 'inspect_sqlite'} else await provider.stat(context, origin_id=ref.origin_id)
        if entry.origin_id != ref.origin_id:
            raise FilesFacadeError("provider returned a mismatched resource", code="provider_unavailable")
        if purpose in {'inspect', 'inspect_elf', 'inspect_pe', 'inspect_macho', 'inspect_sqlite','snapshot','checkpoint'}:
            if not self._generation_current(context):
                raise FilesFacadeError('Inspection access changed', code='resource_changed')
            self._provider_for_ref(context, resource_ref, capability='stat')
            if purpose in {'snapshot','checkpoint'}:self._provider_for_ref(context,resource_ref,capability='download')
        return self._resource(context, ref.provider, entry, parent_stable_id=ref.parent_stable_id)

    async def reveal(
        self, context: ProviderContext, *, resource_ref: str,
        expected_revision: Mapping[str, str] | None = None, workspace_root: Path | None = None,
    ) -> dict[str, Any]:
        """Re-stat a resource and its containing provider folder for Files.

        Provider origin IDs remain inside this process.  The caller receives
        fresh owner/generation-bound ResourceRefs and can therefore navigate to
        the parent without learning a database key or host path.
        """

        provider, ref = self._provider_for_ref(context, resource_ref, capability="stat")
        stop_origin = None
        if expected_revision is not None:
            from src.openclank.files_host_provider import _path, host_origin_for_path

            if (workspace_root is None or ref.provider != "host" or ref.kind != "file"
                    or ref.workspace_id != context.workspace_id or not ref.location_id or context.is_admin):
                raise FilesFacadeError("Workspace match is unavailable", code="resource_unavailable")
            stop_origin = host_origin_for_path(str(workspace_root))
            native_root = Path(_path(stop_origin))
            native_path = Path(_path(ref.origin_id))
            if native_path == native_root or not native_path.is_relative_to(native_root):
                raise FilesFacadeError("Workspace match is unavailable", code="resource_unavailable")

        entry = await provider.stat(context, origin_id=ref.origin_id)
        if entry.origin_id != ref.origin_id or "stat" not in entry.capabilities:
            raise FilesFacadeError("provider returned a mismatched resource", code="provider_unavailable")
        if stop_origin and (entry.kind != "file" or self._provider_revision(entry) != expected_revision):
            raise FilesFacadeError("The captured match changed", code="resource_changed")
        parent_origin = entry.parent_origin_id
        if not parent_origin:
            if stop_origin:
                raise FilesFacadeError("Workspace match is unavailable", code="resource_unavailable")
            if "children" not in entry.capabilities:
                raise FilesFacadeError("resource parent is unavailable", code="resource_unavailable")
            public = self._resource(context, ref.provider, entry)
            return {
                "version": FACADE_VERSION,
                "provider": ref.provider,
                "ancestors": [public],
                "parent": public,
                "resource": public,
            }

        # Provider parents are the only navigation authority. Walk them to the
        # provider root with a hard depth/cycle ceiling, then serialize root to
        # leaf so every ref carries the correct opaque parent stable identity.
        ancestor_entries: list[ProviderResource] = []
        seen = {entry.origin_id}
        while parent_origin:
            if stop_origin and not Path(_path(parent_origin)).is_relative_to(native_root):
                raise FilesFacadeError("Workspace match is unavailable", code="resource_unavailable")
            if parent_origin in seen or len(ancestor_entries) >= MAX_REVEAL_ANCESTORS:
                raise FilesFacadeError("resource ancestry is invalid", code="provider_unavailable")
            seen.add(parent_origin)
            parent_entry = await provider.stat(context, origin_id=parent_origin)
            if parent_entry.origin_id != parent_origin or "children" not in parent_entry.capabilities:
                raise FilesFacadeError("resource parent is unavailable", code="resource_unavailable")
            ancestor_entries.append(parent_entry)
            if parent_origin == stop_origin:
                break
            parent_origin = parent_entry.parent_origin_id

        if stop_origin and ancestor_entries[-1].origin_id != stop_origin:
            raise FilesFacadeError("Workspace match is unavailable", code="resource_unavailable")
        ancestors: list[dict[str, Any]] = []
        parent_stable_id: str | None = None
        for ancestor_entry in reversed(ancestor_entries):
            public_ancestor = self._resource(
                context,
                ref.provider,
                ancestor_entry,
                parent_stable_id=parent_stable_id,
            )
            ancestors.append(public_ancestor)
            parent_stable_id = str(public_ancestor["id"])
        parent = ancestors[-1]
        resource = self._resource(
            context,
            ref.provider,
            entry,
            parent_stable_id=parent_stable_id,
        )
        return {
            "version": FACADE_VERSION,
            "provider": ref.provider,
            "ancestors": ancestors,
            "parent": parent,
            "resource": resource,
        }

    async def search(
        self,
        context: ProviderContext,
        *,
        query: str,
        limit: int = 100,
        sort: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Search every available provider without exposing provider counts.

        Providers retain ownership of traversal/indexing. Each contributes at
        most the requested page size; the facade then applies the advertised
        cross-provider ordering to normalized metadata before sealing ordinary
        child resources. A slow or unavailable provider is isolated rather
        than erasing successful peers.
        """
        normalized_query = str(query or "").strip()[:512]
        if not normalized_query:
            raise FilesFacadeError("search query is required")
        page_size = max(1, min(int(limit), MAX_PAGE_SIZE))
        normalized_sort = _normalized_sort(sort)

        async def run(name: str, provider: FilesProvider):
            provider_search = getattr(provider, "search", None)
            if not callable(provider_search):
                raise FilesFacadeError("provider search is unavailable", code="provider_unavailable")
            page = await asyncio.wait_for(
                provider_search(
                    context,
                    query=normalized_query,
                    limit=page_size,
                    sort=normalized_sort,
                ),
                timeout=5.0,
            )
            return name, page

        names = list(self._providers)
        outcomes = await asyncio.gather(
            *(run(name, self._providers[name]) for name in names),
            return_exceptions=True,
        )
        pages: dict[str, ProviderPage] = {}
        health: dict[str, dict[str, Any]] = {}
        for name, outcome in zip(names, outcomes):
            if isinstance(outcome, Exception):
                health[name] = {"available": False, "code": "provider_unavailable"}
                continue
            returned_name, page = outcome
            pages[returned_name] = page
            health[returned_name] = {"available": True}

        candidates: list[tuple[str, ProviderResource]] = []
        seen: set[tuple[str, str]] = set()
        for name in names:
            page = pages.get(name)
            if page is None:
                continue
            for entry in page.entries:
                identity = (name, entry.origin_id)
                if identity in seen:
                    continue
                seen.add(identity)
                candidates.append((name, entry))
        ordered = _sorted_search_rows(candidates, normalized_sort)
        serialized = [
            self._resource(context, name, entry)
            for name, entry in ordered[:page_size]
        ]
        truncated = len(ordered) > page_size or any(page.next_cursor is not None for page in pages.values())
        return {
            "version": FACADE_VERSION,
            "query": normalized_query,
            "entries": serialized,
            "truncated": truncated,
            "providers": health,
            "sort": normalized_sort,
            "sort_keys": list(SORT_KEYS),
        }

    async def open(self, context: ProviderContext, *, resource_ref: str, purpose: str | None = None) -> dict[str, Any]:
        """Resolve a resource to one trusted first-party application.

        The sealed reference is owner, generation, and capability checked before
        provider code runs. The provider then re-authorizes the current record
        through ``stat``. Only an allowlisted application enum is returned; any
        provider-specific target arguments, identifiers, paths, or URLs are
        deliberately discarded.
        """
        provider, ref = self._provider_for_ref(context, resource_ref, capability="open")
        entry = await self._inspection_stat(provider, ref, context) if purpose == "inspect" else await provider.stat(context, origin_id=ref.origin_id)
        if entry.origin_id != ref.origin_id:
            raise FilesFacadeError("provider returned a mismatched resource", code="provider_unavailable")
        if purpose == 'inspect':
            if not self._generation_current(context):
                raise FilesFacadeError('Inspection access changed', code='resource_changed')
            self._provider_for_ref(context, resource_ref, capability='open')
        if "open" not in entry.capabilities:
            raise FilesFacadeError("resource capability is unavailable", code="resource_unavailable")
        target = entry.open_target if isinstance(entry.open_target, Mapping) else {}
        app = str(target.get("app") or "").strip().lower()
        iwi_preview = ref.provider == "host" and self._is_iwi_preview(entry)
        model_inspect = ref.provider == "host" and self._inspection_format(entry) is not None
        host_image = ref.provider == "host" and self._is_host_image_open(entry)
        if iwi_preview or model_inspect or host_image:
            app = "editor"
        if app not in OPEN_TARGET_APPS:
            raise FilesFacadeError("resource open target is unavailable", code="provider_unavailable")

        # `_resource` never serializes provider navigation URLs/arguments.
        # The action response additionally drops provenance so a provider
        # cannot smuggle an origin ID or path into the navigation instruction.
        # Reissuing still refreshes the opaque ref.
        resource = self._resource(
            context,
            ref.provider,
            replace(entry, open_target={"app": app}, provenance={}, thumbnail_url=None),
            parent_stable_id=ref.parent_stable_id,
        )
        resource.pop("provenance", None)
        provider_open = getattr(provider, "open_resource", None)
        exact = iwi_preview or model_inspect or host_image or bool(callable(provider_open) and self._is_exact_open(entry))
        await self._touch_recent(
            context,
            ref=ref,
            kind=entry.kind,
            display_name=entry.name,
        )

        return {
            "version": FACADE_VERSION,
            "action": "open",
            "target": {"app": app},
            "resource": resource,
            "exact": exact,
        }

    async def host_applications(self, context: ProviderContext, *, resource_ref: str) -> dict[str, Any]:
        provider, ref = self._provider_for_ref(context, resource_ref, capability="open")
        if getattr(provider, "name", "") == "host":
            discover = getattr(provider, "host_applications", None)
            if not callable(discover):
                raise FilesFacadeError("Host app discovery is unavailable", code="provider_unavailable")
            applications = await discover(context, origin_id=ref.origin_id)
        else:
            path = await self._native_content_path(provider, context, ref.origin_id)
            try:
                applications = await host_apps().discover_async(path)
            except HostAppsError as error:
                raise FilesFacadeError(str(error), code=error.code) from error
        return {"version": FACADE_VERSION, "resource_ref": resource_ref, "applications": applications}

    async def open_on_host(self, context: ProviderContext, *, resource_ref: str, app_id: str) -> dict[str, Any]:
        provider, ref = self._provider_for_ref(context, resource_ref, capability="open")
        if getattr(provider, "name", "") == "host":
            launch = getattr(provider, "open_on_host", None)
            if not callable(launch):
                raise FilesFacadeError("Host app launch is unavailable", code="provider_unavailable")
            application = await launch(context, origin_id=ref.origin_id, app_id=app_id)
        else:
            path = await self._native_content_path(provider, context, ref.origin_id)
            try:
                application = await host_apps().launch_async(path, app_id)
            except HostAppsError as error:
                raise FilesFacadeError(str(error), code=error.code) from error
        return {"version": FACADE_VERSION, "action": "open-host", "application": application}

    async def _native_content_path(self, provider: FilesProvider, context: ProviderContext, origin_id: str) -> str:
        entry = await provider.stat(context, origin_id=origin_id)
        if entry.origin_id != origin_id or "open" not in entry.capabilities:
            raise FilesFacadeError("resource capability is unavailable", code="resource_unavailable")
        content_method = getattr(provider, "content", None)
        if not callable(content_method):
            raise FilesFacadeError("This managed resource has no native host representation", code="native_open_unsupported")
        # Provider authorization failures must remain visible to the facade;
        # only the returned source shape determines host-open support.
        content = await content_method(context, origin_id=origin_id)
        path = getattr(content, "path", None)
        if path is None:
            raise FilesFacadeError("This managed resource has no native host representation", code="native_open_unsupported")
        candidate = Path(path)
        try:
            info = candidate.lstat()
        except (FileNotFoundError, OSError):
            raise FilesFacadeError("This managed resource has no native host representation", code="native_open_unsupported")
        if not candidate.is_absolute() or stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise FilesFacadeError("This managed resource has no native host representation", code="native_open_unsupported")
        expected = getattr(content, "expected_identity", None)
        if expected is not None:
            actual = (int(info.st_dev), int(info.st_ino), int(info.st_size), int(info.st_mtime_ns))
            if tuple(expected) != actual:
                raise FilesFacadeError("The managed resource changed before it could be opened", code="resource_unavailable")
        return str(candidate)

    async def workspace_target(
        self,
        context: ProviderContext,
        *,
        resource_ref: str,
    ) -> ProviderWorkspaceTarget:
        """Resolve an opaque Host resource to an internal Workspace target.

        The provider path never crosses the facade's HTTP boundary.  The route
        immediately hands this descriptor to the canonical Workspace service,
        which independently requires current App/Agent authority before it
        records or resolves a Workspace.
        """

        provider, ref = self._provider_for_ref(context, resource_ref, capability="stat")
        if ref.provider != "host":
            raise FilesFacadeError("Only Host resources can become a Workspace", code="resource_unavailable")
        entry = await provider.stat(context, origin_id=ref.origin_id)
        if entry.origin_id != ref.origin_id or "stat" not in entry.capabilities:
            raise FilesFacadeError("resource capability is unavailable", code="resource_unavailable")
        resolver = getattr(provider, "workspace_target", None)
        if not callable(resolver):
            raise FilesFacadeError("Host workspace integration is unavailable", code="provider_unavailable")
        target = await resolver(context, origin_id=ref.origin_id)
        if not isinstance(target, ProviderWorkspaceTarget) or target.origin_id != ref.origin_id:
            raise FilesFacadeError("provider returned a mismatched resource", code="provider_unavailable")
        return target

    async def host_resource_for_path(
        self,
        context: ProviderContext,
        *,
        path: str,
    ) -> dict[str, Any]:
        """Publish a server-resolved Workspace path as an opaque Host ref."""

        provider = self._providers.get("host")
        resolver = getattr(provider, "resource_for_path", None)
        if provider is None or not callable(resolver):
            raise FilesFacadeError("Host workspace integration is unavailable", code="provider_unavailable")
        entry = await resolver(context, path=path)
        if not isinstance(entry, ProviderResource) or entry.kind not in {"folder", "file"}:
            raise FilesFacadeError("Workspace resource is unavailable", code="resource_unavailable")
        return self._resource(context, "host", entry)

    async def action_root_requirement(self, context: ProviderContext, *, generation: int, resource_ref: str,
                                      name: str, expected_revision: Mapping[str, Any] | None) -> dict[str, bool]:
        if int(context.policy_generation) != _strict_nonnegative_int(generation, "generation") or not self._generation_current(context):
            raise FilesFacadeError("File policy generation changed", code="policy_generation_changed")
        if not isinstance(name, str) or not name.strip() or "/" in name or "\\" in name or name.strip() in {".", ".."}:
            raise FilesFacadeError("Resource name is invalid", code="invalid_resource_request")
        provider, ref = self._provider_for_ref(context, resource_ref, capability="rename")
        entry = await provider.stat(context, origin_id=ref.origin_id)
        if entry.origin_id != ref.origin_id or entry.kind != ref.kind or entry.kind not in ({"file"} | _DIRECTORY_KINDS) or "rename" not in entry.capabilities:
            raise FilesFacadeError("Resource is unavailable", code="resource_unavailable")
        self._check_revision(entry, expected_revision)
        classifier = getattr(provider, "managed_rename_root_required", None)
        required = bool(classifier(context, source_origin_id=ref.origin_id, name=name.strip(), source_is_directory=entry.kind in _DIRECTORY_KINDS)) if callable(classifier) else False
        if not self._generation_current(context):
            raise FilesFacadeError("File policy generation changed", code="policy_generation_changed")
        return {"root_required":required}

    _TRASH_ENTRY_LIMITS = {"id": 128, "root_id": 128, "fingerprint": 128, "original_path": 4096, "trashed_path": 4096}

    async def _host_restore(self, context: ProviderContext, *, resource_ref: str, args: Mapping[str, Any],
                            action_id: str | None, expected_revision: Mapping[str, Any] | None) -> dict[str, Any]:
        """Undo a Host trash: authorised by the entry the trash returned, not by an entry capability.

        The trashed file is gone, so the origin is NOT statted. The ref must still be this owner's
        current-generation Host ref carrying `trash` and naming the entry's original path; the parent
        folder must still be writable (the same check create applies). The provider then verifies the
        entry against Native and the root. Not idempotency-replayed: a lost reply is mutation_outcome_unknown.
        """
        entry = args.get("entry") if set(args) == {"entry"} else None
        if (not isinstance(entry, Mapping) or set(entry) != set(self._TRASH_ENTRY_LIMITS)
                or any(not isinstance(entry[key], str) or not entry[key] or len(entry[key]) > limit or "\x00" in entry[key]
                       for key, limit in self._TRASH_ENTRY_LIMITS.items())):
            raise FilesFacadeError("resource action arguments are invalid")
        provider, ref = self._provider_for_ref(context, resource_ref, capability="trash")
        from src.openclank.files_host_provider import _origin  # lazy: the provider module imports this one
        original = entry["original_path"]
        if ref.provider != "host" or not Path(original).is_absolute() or ref.origin_id != _origin(original):
            raise FilesFacadeError("resource action arguments are invalid")
        parent = await provider.stat(context, origin_id=_origin(str(Path(original).parent)))
        if parent.kind not in _DIRECTORY_KINDS or "write" not in parent.capabilities or not self._generation_current(context):
            raise FilesFacadeError("resource capability is unavailable", code="resource_unavailable")
        kwargs: dict[str, Any] = {}
        if action_id is not None:
            kwargs["action_id"] = self._bounded_id(action_id, "action_id")
        if expected_revision is not None:
            kwargs["expected_revision"] = expected_revision
        updated = await provider.action(context, origin_id=ref.origin_id, action="restore",
                                        args={"entry": {key: entry[key] for key in self._TRASH_ENTRY_LIMITS}}, **kwargs)
        if not isinstance(updated, ProviderResource) or updated.origin_id != ref.origin_id:
            raise FilesFacadeError("provider returned a mismatched resource", code="provider_unavailable")
        resource = self._resource(context, "host", replace(updated, provenance=dict(updated.provenance), thumbnail_url=None),
                                  parent_stable_id=ref.parent_stable_id)
        return {"version": FACADE_VERSION, "action": "restore", "resource": resource,
                **({"history": dict(updated.action_receipt)} if updated.action_receipt else {}),
                "state": {"trashed": False}}

    async def action(
        self,
        context: ProviderContext,
        *,
        resource_ref: str,
        action: str,
        args: Mapping[str, Any] | None = None,
        action_id: str | None = None,
        expected_revision: Mapping[str, Any] | None = None,
        managed_root: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Run one closed, idempotent provider action through an opaque ref.

        The action chooses its required capability before the provider sees an
        origin identifier.  ``stat`` then rechecks current ownership and
        capability, and the provider must return the same logical resource.
        This prevents a stale/reforged action from becoming a generic database
        mutation lane.
        """
        normalized_action = str(action or "").strip().lower()
        normalized_args = dict(args or {})
        if managed_root is not None and (normalized_action != "rename" or not expected_revision):
            raise FilesFacadeError("Managed rename requires a source revision", code="invalid_resource_request")
        if normalized_action == "open":
            if normalized_args not in ({}, {"purpose": "inspect"}):
                raise FilesFacadeError("open does not accept arguments")
            return await self.open(context, resource_ref=resource_ref, purpose=normalized_args.get('purpose'))
        if normalized_action == "favorite.set":
            capability = "favorite"
            state_key = "favorite"
        elif normalized_action == "archive.set":
            capability = "archive" if normalized_args.get("value") is True else "restore"
            state_key = "archived"
        elif normalized_action in {"rename", "move"}:
            capability = normalized_action
            state_key = "name"
        elif normalized_action == "trash":
            capability = "trash"
            state_key = "trashed"
        elif normalized_action == "restore":
            capability = "restore"
            state_key = "trashed"
        else:
            raise FilesFacadeError("resource action is unsupported")
        if normalized_action in {"favorite.set", "archive.set"} and (set(normalized_args) != {"value"} or not isinstance(normalized_args.get("value"), bool)):
            raise FilesFacadeError("resource action arguments are invalid")
        if normalized_action in {"rename", "move"} and (set(normalized_args) != {"name"} or not isinstance(normalized_args.get("name"), str) or not normalized_args["name"].strip()):
            raise FilesFacadeError("resource action arguments are invalid")
        if normalized_action == "trash" and normalized_args:
            raise FilesFacadeError("resource action arguments are invalid")
        if normalized_action == "restore" and normalized_args:
            # Only a Host undo of a trash carries args; any other provider still takes none.
            return await self._host_restore(context, resource_ref=resource_ref, args=normalized_args,
                                            action_id=action_id, expected_revision=expected_revision)

        # An exact managed rename retry must find its existing owner-bound
        # operation before attempting to resolve an expired old source ref.
        if managed_root is not None and action_id is not None:
            operation = self._bounded_id(action_id, "action_id")
            digest = self._digest({"kind":"host-action", "resource_ref":resource_ref, "action":normalized_action,
                "args":normalized_args, "generation":int(context.policy_generation),
                "workspace_id":str(context.workspace_id or "default"), "expected_revision":dict(expected_revision or {}),
                "managed_root":dict(managed_root)})
            existing = self._load_operation(context, operation)
            if existing is not None:
                if existing.get("digest") != digest or int(existing.get("generation", -1)) != int(context.policy_generation):
                    raise FilesFacadeError("Action ID was already used for a different request", code="idempotency_conflict")
                status = await self.operation_receipt(context, operation_id=operation)
                item = next((value for value in status.get("items", []) if value.get("item_id") == "action"), {})
                if item.get("outcome") in {"committed", "unchanged"} and item.get("resource_ref"):
                    try:
                        private = (existing.get("receipt") or {}).get("_managed_items", {}).get("action", {})
                        frozen_root = (private.get("plan") or {}).get("managed_root")
                        old_root = resolve_resource_ref_for_reissue(str(managed_root["resource_ref"]),
                            expected_owner_subject_id=context.owner_subject_id, required_capability="write")
                        host = self._providers["host"]
                        if old_root.provider != "host" or old_root.policy_generation != int(context.policy_generation) or not isinstance(frozen_root, Mapping):
                            raise ValueError("root identity changed")
                        renewed_root = await host.authorize_managed_root(context, origin_id=old_root.origin_id, expected_revision=None)
                        if dict(renewed_root) != dict(frozen_root):
                            raise ValueError("root authority changed")
                    except Exception as error:
                        raise FilesFacadeError("Committed action root is unavailable", code="resource_unavailable") from error
                    try:
                        target_provider, target_ref = self._provider_for_ref(context, str(item["resource_ref"]), capability="stat")
                    except FilesFacadeError as exc:
                        if exc.code != "resource_ref_stale":
                            raise
                        try:
                            target_ref = resolve_resource_ref_for_reissue(str(item["resource_ref"]),
                                expected_owner_subject_id=context.owner_subject_id, required_capability="stat")
                            if target_ref.provider != "host" or target_ref.policy_generation != int(context.policy_generation):
                                raise ValueError("result scope changed")
                            target_provider = self._providers["host"]
                        except Exception as error:
                            raise FilesFacadeError("Committed action resource is unavailable", code="resource_unavailable") from error
                    target = await target_provider.stat(context, origin_id=target_ref.origin_id)
                    if target.origin_id != target_ref.origin_id or "stat" not in target.capabilities:
                        raise FilesFacadeError("Committed action resource is unavailable", code="resource_unavailable")
                    resource = self._resource(context, target_ref.provider, target, parent_stable_id=target_ref.parent_stable_id)
                    return {"version":FACADE_VERSION, "action":normalized_action, "resource":resource,
                            "state":{state_key:normalized_args.get("name")}}
                if item.get("outcome") in {"failed", "denied", "conflict", "stale"}:
                    raise FilesFacadeError(f"Action {operation} did not complete", code="resource_changed")
                raise FilesFacadeError(f"Action {operation} requires reconciliation", code="operation_pending")

        provider, ref = self._provider_for_ref(context, resource_ref, capability=capability)
        action_operation = None
        action_digest = None
        action_pending = None
        if ref.provider == "host" and normalized_action in {"rename", "move"}:
            action_operation = self._bounded_id(action_id or f"files-action-{uuid.uuid4().hex}", "action_id")
            action_digest_payload = {"kind": "host-action", "resource_ref": resource_ref,
                "action": normalized_action, "args": normalized_args, "generation": int(context.policy_generation),
                "workspace_id": str(context.workspace_id or "default")}
            if expected_revision is not None: action_digest_payload["expected_revision"] = dict(expected_revision)
            if managed_root is not None: action_digest_payload["managed_root"] = dict(managed_root)
            action_digest = self._digest(action_digest_payload)
            previous = self._load_operation(context, action_operation)
            if previous is not None:
                if previous.get("digest") != action_digest:
                    raise FilesFacadeError("Action ID was already used for a different request", code="idempotency_conflict")
                status = await self.operation_receipt(context, operation_id=action_operation)
                item = next((value for value in status.get("items", []) if value.get("item_id") == "action"), {})
                if item.get("outcome") in {"committed", "unchanged"} and item.get("resource_ref"):
                    target_provider, target_ref = self._provider_for_ref(context, str(item["resource_ref"]), capability="stat")
                    target = await target_provider.stat(context, origin_id=target_ref.origin_id)
                    if target.origin_id != target_ref.origin_id:
                        raise FilesFacadeError("Committed action resource is unavailable", code="resource_unavailable")
                    resource = self._resource(context, target_ref.provider, target, parent_stable_id=target_ref.parent_stable_id)
                    return {"version": FACADE_VERSION, "action": normalized_action, "resource": resource,
                            "state": {state_key: normalized_args.get("name")}}
                if item.get("outcome") in {"failed", "denied", "conflict", "stale"}:
                    raise FilesFacadeError(f"Action {action_operation} did not complete", code="resource_changed")
                raise FilesFacadeError(f"Action {action_operation} requires reconciliation", code="operation_pending")
        current = await provider.stat(context, origin_id=ref.origin_id)
        if current.origin_id != ref.origin_id or capability not in current.capabilities:
            raise FilesFacadeError("resource capability is unavailable", code="resource_unavailable")
        self._check_revision(current, expected_revision)
        selected_root = None
        if managed_root is not None:
            if set(managed_root) - {"resource_ref", "expected_revision"} or not str(managed_root.get("resource_ref") or "").strip():
                raise FilesFacadeError("Selected root is invalid", code="invalid_resource_request")
            root_provider, root_ref = self._provider_for_ref(context, str(managed_root["resource_ref"]), capability="write")
            classifier = getattr(provider, "selected_folder_required", None)
            selected_profile = callable(classifier) and classifier(context, source_origin_id=ref.origin_id, source_is_directory=ref.kind in _DIRECTORY_KINDS)
            resolver = getattr(root_provider, "authorize_selected_folder" if selected_profile else "authorize_managed_root", None)
            if root_provider is not provider or root_ref.provider != "host" or not callable(resolver):
                raise FilesFacadeError("Selected root is unavailable", code="resource_unavailable")
            selected_root = await resolver(context, origin_id=root_ref.origin_id, expected_revision=managed_root.get("expected_revision"))
        provider_action = getattr(provider, "action", None)
        if not callable(provider_action):
            raise FilesFacadeError("resource action is unavailable", code="resource_unavailable")
        if action_operation is not None:
            action_pending = {"operation_id": action_operation, "generation": int(context.policy_generation),
                "state": "pending", "items": [{"item_id": "action", "outcome": "pending", "code": "operation_in_progress"}],
                "_provider_names": ["host"], "_request_kind": "action", "_item_ids": ["action"],
                "_workspace_id": str(context.workspace_id or "default")}
            owner, _existing = self._reserve_operation(context, action_operation, action_digest, context.policy_generation, action_pending)
            if not owner:
                raise FilesFacadeError(f"Action {action_operation} requires reconciliation", code="operation_pending")
        provider_kwargs = {"context": context, "origin_id": ref.origin_id, "action": normalized_action, "args": normalized_args}
        if action_operation is not None: provider_kwargs["action_id"] = action_operation
        elif action_id is not None: provider_kwargs["action_id"] = action_id
        if selected_root is not None:
            provider_kwargs["managed_root"] = selected_root
            provider_kwargs["expected_revision"] = expected_revision
        try:
            updated = await provider_action(**provider_kwargs)
        except TypeError as error:
            if action_id is not None and "action_id" in str(error):
                updated = await provider_action(context, origin_id=ref.origin_id, action=normalized_action, args=normalized_args)
            else:
                raise
        except FilesFacadeError as error:
            if action_operation is not None:
                uncertain = error.code in {"mutation_outcome_unknown", "operation_pending"}
                saved = ({**action_pending, "items": [{"item_id": "action", "outcome": "pending", "code": error.code}]} if uncertain
                         else {**action_pending, "state": "partial", "items": [{"item_id": "action", "outcome": "failed", "code": error.code}]})
                self._save_operation(context, action_operation, action_digest, context.policy_generation, saved)
                if uncertain:
                    raise FilesFacadeError(f"Action {action_operation} requires reconciliation", code="operation_pending") from error
            raise
        if action_operation is not None and isinstance(updated, Mapping):
            outcome = str(updated.get("outcome") or "pending")
            code = str(updated.get("code") or "managed_reconciliation_required")
            saved = {**action_pending, "state": "pending" if outcome == "pending" else "partial",
                     "items": [{"item_id": "action", "outcome": "pending" if outcome == "pending" else "failed", "code": code}]}
            self._save_operation(context, action_operation, action_digest, context.policy_generation, saved)
            if outcome == "pending":
                raise FilesFacadeError(f"Action {action_operation} requires reconciliation", code="operation_pending")
            raise FilesFacadeError(f"Action {action_operation} did not complete", code="resource_changed")
        if (
            not isinstance(updated, ProviderResource)
            or not str(updated.origin_id or "").strip()
            or (updated.origin_id != ref.origin_id and not (ref.provider == "host" and normalized_action in {"rename", "move"}))
        ):
            raise FilesFacadeError("provider returned a mismatched resource", code="provider_unavailable")
        resource = self._resource(
            context,
            ref.provider,
            replace(updated, provenance=dict(updated.provenance), thumbnail_url=None),
            parent_stable_id=ref.parent_stable_id,
        )
        if action_operation is not None:
            completed = {**action_pending, "state": "complete", "items": [{"item_id": "action", "outcome": "committed", "resource_ref": resource["ref"]}]}
            self._save_operation(context, action_operation, action_digest, context.policy_generation, completed)
        return {
            "version": FACADE_VERSION,
            "action": normalized_action,
            "resource": resource,
            **({"history": dict(updated.action_receipt)} if updated.action_receipt else {}),
            "state": ({state_key: bool(normalized_args["value"])} if state_key in {"favorite", "archived"} else {state_key: normalized_args.get("name") if state_key == "name" else normalized_action == "trash"}),
        }

    async def create(
        self,
        context: ProviderContext,
        *,
        parent_ref: str,
        name: str,
        text: str,
        action_id: str | None = None,
        before_dispatch: Callable[..., Any] | None = None,
        operation_id: str | None = None,
        expected_parent_revision: Mapping[str, Any] | None = None,
        status_only: bool = False,
    ) -> dict[str, Any]:
        """Create a provider-owned ordinary document beneath an opaque folder ref."""
        if operation_id is not None or expected_parent_revision is not None or status_only:
            if not operation_id or expected_parent_revision is None:
                raise FilesFacadeError('Ordinary create identity unavailable',code='invalid_resource_request')
            from src.openclank.ordinary_create import _identity
            if not isinstance(operation_id,str) or not operation_id or operation_id != operation_id.strip():
                raise FilesFacadeError('Ordinary create identity unavailable',code='invalid_resource_request')
            original_id = _identity(str(context.owner_subject_id),str(context.workspace_id),'','human',operation_id)
            getter = getattr(self._operation_store,'get_operation',None)
            if not callable(getter): raise FilesFacadeError('Ordinary create authority unavailable',code='provider_unavailable')
            previous = getter(owner_subject_id=context.owner_subject_id,operation_id=original_id)
            read_only = status_only or previous is not None
            provider, parent = self._provider_for_ref(context, parent_ref, capability='stat' if read_only else 'write')
            if parent.provider != 'host' or not callable(getattr(provider,'ordinary_create_resource',None)):
                raise FilesFacadeError('Ordinary create unavailable',code='unsupported_operation')
            current = await provider.stat(context,origin_id=parent.origin_id)
            if current.origin_id != parent.origin_id or current.kind not in _DIRECTORY_KINDS or 'children' not in current.capabilities:
                raise FilesFacadeError('Ordinary create parent unavailable',code='resource_unavailable')
            if not read_only:
                if 'write' not in current.capabilities: raise FilesFacadeError('Ordinary create parent unavailable',code='resource_unavailable')
                self._check_revision(current,expected_parent_revision)
            result = dict(await provider.ordinary_create_resource(context,parent_origin_id=parent.origin_id,name=name,text=text,
                operation_id=operation_id,expected_revision=expected_parent_revision,status_only=read_only,before_dispatch=before_dispatch))
            observed=result.pop('resource',None)
            if observed is not None:
                if not isinstance(observed,ProviderResource): raise FilesFacadeError('Ordinary create receipt unavailable',code='provider_unavailable')
                result['resource']=self._resource(context,parent.provider,observed,parent_stable_id=parent.stable_id)
            return {'version':FACADE_VERSION,'action':'create',**result}
        provider, parent = self._provider_for_ref(context, parent_ref, capability="write")
        current = await provider.stat(context, origin_id=parent.origin_id)
        if current.origin_id != parent.origin_id or "children" not in current.capabilities or "write" not in current.capabilities:
            raise FilesFacadeError("resource creation is unavailable", code="resource_unavailable")
        creator = getattr(provider, "create_resource", None)
        if not callable(creator):
            raise FilesFacadeError("resource creation is unavailable", code="resource_unavailable")
        if not isinstance(name, str) or not name.strip() or not isinstance(text, str):
            raise FilesFacadeError("resource creation arguments are invalid", code="invalid_resource_request")
        try:
            extra = {'before_dispatch': before_dispatch} if before_dispatch is not None and parent.provider == 'host' else {}
            if before_dispatch is not None and parent.provider != 'host':
                raise FilesFacadeError('provider cannot enforce source currentness', code='unsupported_operation')
            created = await creator(
                context,
                parent_origin_id=parent.origin_id,
                name=name,
                text=text,
                action_id=action_id,
                **extra,
            )
        except TypeError as error:
            if action_id is not None and "action_id" in str(error):
                created = await creator(context, parent_origin_id=parent.origin_id, name=name, text=text)
            else:
                raise
        if parent.provider == 'host' and isinstance(created, Mapping):
            allowed = {'outcome', 'revision', 'history', 'readback', 'changedAfterCreate', 'resource'}
            revision, history = created.get('revision'), created.get('history')
            if (set(created) - allowed or created.get('outcome') != 'created'
                or not isinstance(revision, Mapping) or set(revision) != {'kind', 'value'} or revision.get('kind') != 'hostFingerprint'
                or not isinstance(revision.get('value'), str) or not re.fullmatch(r'sha256:[0-9a-f]{64}:[0-9]+', revision['value'])
                or not isinstance(history, Mapping) or set(history) - {'state', 'actionId'} or history.get('state') not in {'complete', 'incomplete', 'unavailable'}
                or created.get('readback') not in {'current', 'unavailable', 'changed'}):
                raise FilesFacadeError('Host create receipt is unavailable', code='mutation_outcome_unknown')
            action = history.get('actionId')
            if (history['state'] in {'complete', 'incomplete'}) != (isinstance(action, str) and bool(re.fullmatch(r'[A-Za-z0-9_-]{1,128}', action))):
                raise FilesFacadeError('Host create receipt is unavailable', code='mutation_outcome_unknown')
            observed = created.get('resource')
            if ((created['readback'] == 'current') != isinstance(observed, ProviderResource)
                or (created['readback'] != 'current' and observed is not None)
                or (created['readback'] == 'changed') != (created.get('changedAfterCreate') is True)):
                raise FilesFacadeError('Host create receipt is unavailable', code='mutation_outcome_unknown')
            if observed is not None and (observed.kind != 'file' or observed.parent_origin_id != parent.origin_id
                    or observed.name != name.strip() or self._provider_revision(observed) != dict(revision)):
                raise FilesFacadeError('Host create receipt is unavailable', code='mutation_outcome_unknown')
            result = {'version': FACADE_VERSION, 'action': 'create', 'outcome':'created', 'revision':dict(revision),
                      'history':dict(history), 'readback':created['readback']}
            if created.get('changedAfterCreate') is True:
                result['changedAfterCreate'] = True
            if observed is not None:
                result['resource'] = self._resource(context, parent.provider, observed, parent_stable_id=parent.stable_id)
            return result
        if not isinstance(created, ProviderResource) or not str(created.origin_id or "").strip():
            raise FilesFacadeError("provider returned an invalid created resource", code="provider_unavailable")
        resource = self._resource(
            context,
            parent.provider,
            created,
            parent_stable_id=parent.stable_id,
        )
        return {
            "version": FACADE_VERSION,
            "action": "create",
            "resource": resource,
            **({"history": dict(created.action_receipt)} if created.action_receipt else {}),
        }

    async def create_directory(
        self,
        context: ProviderContext,
        *,
        parent_ref: str,
        name: str,
        operation_id: str,
        generation: int,
        expected_revision: Mapping[str, Any] | None = None,
        collision: str = "fail",
    ) -> dict[str, Any]:
        """Create one authorized child directory with a durable receipt."""
        requested_generation = _strict_nonnegative_int(generation, "generation")
        if requested_generation != int(context.policy_generation):
            raise FilesFacadeError("file policy generation is stale", code="resource_ref_stale")
        operation = self._bounded_id(operation_id, "operation_id")
        if not isinstance(parent_ref, str) or not parent_ref.strip():
            raise FilesFacadeError("directory parent reference is invalid", code="invalid_resource_request")
        normalized_name = str(name or "").strip()
        if (
            not normalized_name or len(normalized_name.encode("utf-8")) > 240
            or "\x00" in normalized_name or "/" in normalized_name or "\\" in normalized_name
            or normalized_name in {".", ".."} or Path(normalized_name).name != normalized_name
        ):
            raise FilesFacadeError("directory name is invalid", code="invalid_resource_request")
        normalized_collision = str(collision or "fail").strip().lower()
        if normalized_collision not in {"fail", "reuse"}:
            raise FilesFacadeError("directory collision policy is unsupported", code="unsupported_operation")

        provider, parent = self._provider_for_ref(context, parent_ref.strip(), capability="write")
        parent_entry = await provider.stat(context, origin_id=parent.origin_id)
        if parent_entry.origin_id != parent.origin_id or parent_entry.kind not in _DIRECTORY_KINDS or "children" not in parent_entry.capabilities or "write" not in parent_entry.capabilities:
            raise FilesFacadeError("directory parent is unavailable", code="resource_unavailable")
        self._check_revision(parent_entry, expected_revision)
        parent_revision = self._provider_revision(parent_entry)
        digest = self._digest({
            "kind": "create-directory",
            "account_id": context.owner_subject_id,
            "workspace_id": context.workspace_id,
            "generation": int(context.policy_generation),
            "operation_id": operation,
            "parent_resource": parent.stable_id,
            "parent_revision": parent_revision,
            "name": normalized_name,
            "collision": normalized_collision,
        })
        item_id = operation
        pending = {
            "operation_id": operation,
            "generation": int(context.policy_generation),
            "state": "pending",
            "items": [{"item_id": item_id, "outcome": "pending", "code": "operation_in_progress"}],
            "_request_kind": "create-directory",
            "_provider_names": [str(getattr(provider, "name", ""))],
            "_item_ids": [item_id],
            "_workspace_id": str(context.workspace_id or "default"),
        }
        owner, previous = self._reserve_operation(context, operation, digest, context.policy_generation, pending)

        async def response_for(receipt: Mapping[str, Any]) -> dict[str, Any] | None:
            items = receipt.get("items") if isinstance(receipt, Mapping) else None
            item = items[0] if isinstance(items, Sequence) and items and isinstance(items[0], Mapping) else None
            token = str(item.get("resource_ref") or "") if item else ""
            if not token:
                return None
            resolved_provider, resolved = self._provider_for_ref(context, token, capability="stat")
            if resolved_provider is not provider:
                raise FilesFacadeError("directory provider changed", code="provider_unavailable")
            created = await provider.stat(context, origin_id=resolved.origin_id)
            if created.origin_id != resolved.origin_id or created.parent_origin_id != parent.origin_id or created.kind not in _DIRECTORY_KINDS:
                return None
            resource = self._resource(context, provider.name, created, parent_stable_id=parent.stable_id)
            return {"version": FACADE_VERSION, "action": "create-directory", "resource": resource, "history": item.get("history") or {}}

        if not owner:
            if str((previous or {}).get("_workspace_id") or "") != str(context.workspace_id or "default"):
                raise FilesFacadeError("directory operation is unavailable", code="resource_unavailable")
            recovered = None
            status = getattr(provider, "operation_status", None)
            if callable(status):
                try:
                    recovered = await status(context, operation_id=operation)
                except Exception:
                    recovered = None
            if isinstance(recovered, Mapping):
                try:
                    checked = _validated_receipt(
                        {key: value for key, value in recovered.items() if key != "_request_digest"},
                        operation_id=operation,
                        item_id=item_id,
                        expected_item_ids=[item_id],
                    )
                except FilesFacadeError:
                    checked = None
                if checked is not None and checked.get("state") == "complete":
                    checked["generation"] = int(context.policy_generation)
                    self._save_operation(context, operation, digest, context.policy_generation, checked)
                    result = await response_for(checked)
                    if result is not None:
                        return result
            result = await response_for(previous or {})
            if result is not None:
                return result
            raise FilesFacadeError("directory operation is still pending", code="operation_pending")

        creator = getattr(provider, "create_directory", None)
        if not callable(creator):
            raise FilesFacadeError("provider cannot create directories", code="unsupported_provider_kind")
        try:
            try:
                created = await creator(
                    context,
                    parent_origin_id=parent.origin_id,
                    name=normalized_name,
                    operation_id=operation,
                    request_digest=digest,
                    parent_revision=parent_revision,
                    collision=normalized_collision,
                )
            except TypeError as error:
                if not any(field in str(error) for field in ("request_digest", "parent_revision", "collision")):
                    raise
                created = await creator(context, parent_origin_id=parent.origin_id, name=normalized_name, operation_id=operation)
            if not isinstance(created, ProviderResource):
                raise FilesFacadeError("provider returned an invalid directory", code="provider_unavailable")
            checked = await provider.stat(context, origin_id=created.origin_id)
            if checked.origin_id != created.origin_id or checked.parent_origin_id != parent.origin_id or checked.kind not in _DIRECTORY_KINDS:
                raise FilesFacadeError("provider returned an invalid directory", code="provider_unavailable")
            resource = self._resource(context, provider.name, checked, parent_stable_id=parent.stable_id)
            history = _public_action_receipt(checked.action_receipt or created.action_receipt)
            item = {"item_id": item_id, "outcome": "committed", "resource_key": stable_resource_id(owner_subject_id=context.owner_subject_id, provider=provider.name, origin_id=checked.origin_id), "resource_ref": resource["ref"]}
            revision = self._provider_revision(checked)
            if revision is not None:
                item["revision"] = dict(revision)
            if history:
                item["history"] = history
            receipt = {"operation_id": operation, "generation": int(context.policy_generation), "state": "complete", "items": [item], "_request_kind": "create-directory", "_workspace_id": str(context.workspace_id or "default"), "_item_ids": [item_id], "_provider_names": [str(getattr(provider, "name", ""))]}
            self._save_operation(context, operation, digest, context.policy_generation, receipt)
            return {"version": FACADE_VERSION, "action": "create-directory", "resource": resource, **({"history": history} if history else {})}
        except FilesFacadeError:
            raise
        except Exception as error:
            raise FilesFacadeError("directory creation failed", code="provider_unavailable") from error

    async def open_payload(self, context: ProviderContext, *, resource_ref: str,
                           expected_revision: Mapping[str, Any] | None = None, purpose: str | None = None) -> dict[str, Any]:
        """Load the exact item for a trusted first-party app via its opaque ref.

        The browser never supplies or receives a provider origin ID. The sealed
        ref is checked first, provider ``stat`` re-authorizes the current item,
        and the provider's app payload is admitted through a narrow vocabulary.
        """
        provider, ref = self._provider_for_ref(context, resource_ref, capability="open")
        entry = await self._inspection_stat(provider, ref, context) if purpose == "inspect" else await provider.stat(context, origin_id=ref.origin_id)
        if entry.origin_id != ref.origin_id:
            raise FilesFacadeError("provider returned a mismatched resource", code="provider_unavailable")
        if purpose == 'inspect':
            if not self._generation_current(context):
                raise FilesFacadeError('Inspection access changed', code='resource_changed')
            self._provider_for_ref(context, resource_ref, capability='open')
        if "open" not in entry.capabilities:
            raise FilesFacadeError("resource capability is unavailable", code="resource_unavailable")
        if expected_revision is not None and self._provider_revision(entry) != dict(expected_revision):
            raise FilesFacadeError("Resource changed before Editor opened it", code="resource_changed")
        target = entry.open_target if isinstance(entry.open_target, Mapping) else {}
        app = str(target.get("app") or "").strip().lower()
        iwi_preview = ref.provider == "host" and self._is_iwi_preview(entry)
        model_inspect = ref.provider == "host" and self._inspection_format(entry) is not None
        video_preview = ref.provider == 'host' and entry.kind == 'file' and str(entry.name).lower().endswith('.webm') and str(entry.mime_type or '').lower() == 'video/webm' and 'preview' in entry.capabilities
        if ref.provider == "host" and self._is_host_image_open(entry):
            revision = self._provider_revision(entry)
            if not revision or revision.get("kind") != "hostFingerprint" or not self._generation_current(context):
                raise FilesFacadeError("Image requires a current stable source revision", code="resource_unavailable")
            resource = self._resource(
                context, ref.provider, replace(entry, open_target={"app": "editor"}, provenance={}, thumbnail_url=None),
                parent_stable_id=ref.parent_stable_id,
            )
            return {"version": FACADE_VERSION, "target": {"app": "editor"}, "resource": resource,
                    "payload": {"kind": "host-image", "read_only": True}}
        if iwi_preview or model_inspect or video_preview:
            app = "editor"
        if app not in OPEN_TARGET_APPS:
            raise FilesFacadeError("resource open target is unavailable", code="provider_unavailable")
        if iwi_preview or model_inspect or video_preview:
            revision = self._provider_revision(entry)
            if revision is None or not self._generation_current(context):
                raise FilesFacadeError("Inspection requires a current stable source revision", code="resource_unavailable")
            resource = self._resource(context, ref.provider, replace(entry, open_target={"app": "editor"}, provenance={}, thumbnail_url=None), parent_stable_id=ref.parent_stable_id)
            handle = {
                "key": {"accountId": str(context.owner_subject_id), "workspaceId": "host" if ref.provider == "host" else str(context.workspace_id or "default"),
                        "provider": ref.provider, "resourceId": resource["id"]},
                "revision": dict(revision), "representation": "asset" if video_preview else "text",
                "locator": {"displayName": str(entry.name), "locationLabel": str(entry.name), "opaqueRef": resource["ref"]},
                "capabilities": {"read": True, "edit": False, "write": False, "preview": True},
            }
            return {"version": FACADE_VERSION, "target": {"app": "editor"}, "resource": resource,
                    "payload": {"name": str(entry.name), "kind": "video-preview" if video_preview else "derived-iwi-preview" if iwi_preview else "model-header-inspect", "corpus": ref.provider,
                                "text": "", "read_only": True, "resource": handle, "format": resource["format"]}}
        provider_open = getattr(provider, "open_resource", None)
        if not callable(provider_open):
            raise FilesFacadeError("exact resource open is unavailable", code="resource_unavailable")
        payload = await provider_open(context, origin_id=ref.origin_id)
        if not isinstance(payload, Mapping):
            raise FilesFacadeError("exact resource open is unavailable", code="resource_unavailable")
        allowed = {
            "copal_notes": {"name", "kind", "corpus", "text", "properties", "relations", "tags", "read_only", "resource"},
            "editor": {
                "name", "kind", "corpus", "text", "properties", "relations", "tags", "read_only", "resource",
                "encoding", "newline", "bom_bytes", "mode", "language", "representation", "parent_resource_ref",
            },
            "document_editor": {
                "title", "language", "content", "version", "session_ref", "archived", "read_only",
            },
            "imps": {
                "provider", "resource_id",
                "filename", "prompt", "caption", "model", "size", "quality", "tags", "ai_tags",
                "favorite", "taken_at", "created_at", "updated_at", "camera", "width", "height",
                "file_size", "media_type", "read_only",
            },
            "chat": {
                "title", "model", "archived", "message_count", "messages", "truncated", "read_only",
            },
            "research": {
                "title", "category", "archived", "report", "sources", "source_count", "truncated",
                "read_only",
            },
        }.get(app)
        if allowed is None or set(payload) - allowed:
            raise FilesFacadeError("resource open payload is invalid", code="provider_unavailable")
        safe_payload = dict(payload)
        if app == "document_editor":
            # Session is useful only as an opaque UI grouping hint. Provider IDs
            # remain server-side, so exact-open tabs are intentionally detached.
            safe_payload["session_ref"] = None
        # Host editors need the sealed capability again when they save. Keep
        # it nested in the canonical ResourceHandle; the provider origin/path
        # never crosses the facade boundary.
        if ref.provider == "host" and isinstance(safe_payload.get("resource"), Mapping):
            descriptor = dict(safe_payload["resource"])
            locator = dict(descriptor.get("locator") or {})
            locator["opaqueRef"] = resource_ref
            descriptor["locator"] = locator
            safe_payload["resource"] = descriptor
            # A parent ref is an optional capability handoff. It is minted
            # only after the provider authorizes the containing folder, so a
            # read-only/exact-file grant cannot turn into a create grant.
            if entry.parent_origin_id:
                try:
                    parent_entry = await provider.stat(context, origin_id=entry.parent_origin_id)
                    if "children" in parent_entry.capabilities and "write" in parent_entry.capabilities:
                        parent_resource = self._resource(context, ref.provider, parent_entry)
                        safe_payload["parent_resource_ref"] = parent_resource["ref"]
                except FilesFacadeError:
                    pass
        resource = self._resource(
            context,
            ref.provider,
            replace(entry, provenance={}, thumbnail_url=None),
            parent_stable_id=ref.parent_stable_id,
        )
        resource.pop("provenance", None)
        await self._touch_recent(
            context,
            ref=ref,
            kind=entry.kind,
            display_name=entry.name,
        )
        return {
            "version": FACADE_VERSION,
            "target": {"app": app},
            "resource": resource,
            "payload": safe_payload,
        }

    async def save_resource(
        self,
        context: ProviderContext,
        *,
        resource_ref: str,
        expected_revision: Mapping[str, Any],
        text: str,
        before_dispatch: Callable[..., Any] | None = None,
    ) -> Mapping[str, Any]:
        """CAS-save an Editor resource behind its owner-bound opaque ref."""
        provider, ref = self._provider_for_ref(context, resource_ref, capability="open")
        entry = await provider.stat(context, origin_id=ref.origin_id)
        if entry.origin_id != ref.origin_id or "open" not in entry.capabilities:
            raise FilesFacadeError("resource capability is unavailable", code="resource_unavailable")
        target = entry.open_target if isinstance(entry.open_target, Mapping) else {}
        app = str(target.get("app") or "").strip().lower()
        if app != "editor" or entry.kind != "file":
            raise FilesFacadeError("resource is not an Editor text file", code="resource_unavailable")
        saver = getattr(provider, "save_resource", None)
        if not callable(saver):
            raise FilesFacadeError("resource save is unavailable", code="resource_unavailable")
        if not isinstance(expected_revision, Mapping):
            raise FilesFacadeError("resource save revision is invalid", code="invalid_resource_request")
        if before_dispatch is not None and ref.provider != 'host':
            raise FilesFacadeError('provider cannot enforce source currentness', code='unsupported_operation')
        extra = {'before_dispatch': before_dispatch} if before_dispatch is not None else {}
        result = await saver(
            context,
            origin_id=ref.origin_id,
            expected_revision=expected_revision,
            text=text,
            **extra,
        )
        if not isinstance(result, Mapping) or str(result.get("outcome") or "") not in {"applied", "conflict"}:
            raise FilesFacadeError("provider returned an invalid save result", code="provider_unavailable")
        if ref.provider == 'host' and result.get('outcome') == 'applied':
            history = result.get('history')
            if not isinstance(history, Mapping) or set(history) - {'state', 'actionId'} or history.get('state') not in {'complete', 'incomplete', 'unavailable', 'not_applicable'}:
                raise FilesFacadeError('Host save receipt is unavailable', code='mutation_outcome_unknown')
            action = history.get('actionId')
            if (history['state'] in {'complete', 'incomplete'}) != (isinstance(action, str) and bool(re.fullmatch(r'[A-Za-z0-9_-]{1,128}', action))):
                raise FilesFacadeError('Host save receipt is unavailable', code='mutation_outcome_unknown')
        return dict(result)

    @staticmethod
    def _is_host_image_open(entry: ProviderResource) -> bool:
        return (
            entry.kind == "file"
            and str(entry.mime_type or "").lower() in {"image/png", "image/jpeg", "image/gif", "image/webp"}
            and "download" in entry.capabilities
        )

    @staticmethod
    def _is_iwi_preview(entry: ProviderResource) -> bool:
        return entry.kind == "file" and str(entry.name or "").lower().endswith(".iwi") and "preview" in entry.capabilities

    async def _iwi_preview(self, context: ProviderContext, *, resource_ref: str,
                           expected_revision: Mapping[str, Any] | None = None,
                           thumbnail: tuple[int, int, float] | None = None) -> ProviderContent:
        from src.openclank.format_adapters import iwi
        provider, ref = self._provider_for_ref(context, resource_ref, capability="preview")
        try:
            async with asyncio.timeout(15):
                entry = await provider.stat(context, origin_id=ref.origin_id)
                revision = self._provider_revision(entry)
                if entry.origin_id != ref.origin_id or not self._is_iwi_preview(entry) or revision is None:
                    raise FilesFacadeError("IWI source is unavailable", code="resource_unavailable")
                if expected_revision is not None and dict(revision) != dict(expected_revision):
                    raise FilesFacadeError("IWI source changed; reopen its preview", code="resource_changed")
                if entry.size is None or not (0 < int(entry.size) <= iwi.MAX_INPUT_BYTES):
                    raise FilesFacadeError("IWI input exceeds the supported preview bounds", code="resource_unavailable")
                source = await provider.content(context, origin_id=ref.origin_id)
                if source.origin_id != ref.origin_id or source.size is None or not (0 < int(source.size) <= iwi.MAX_INPUT_BYTES):
                    raise FilesFacadeError("IWI input exceeds the supported preview bounds", code="resource_unavailable")
                if source.data is not None and source.path is None and source.stream is None:
                    if not isinstance(source.data, bytes):
                        raise FilesFacadeError("IWI provider returned invalid bytes", code="provider_unavailable")
                    data = source.data
                elif source.stream is not None and source.data is None and source.path is None:
                    chunks = []
                    total = 0
                    async for chunk in source.stream(0, int(source.size) + 1):
                        if not isinstance(chunk, bytes):
                            raise FilesFacadeError("IWI provider returned invalid bytes", code="provider_unavailable")
                        total += len(chunk)
                        if total > int(source.size) or total > iwi.MAX_INPUT_BYTES:
                            raise FilesFacadeError("IWI source changed or exceeds preview bounds", code="resource_changed")
                        chunks.append(bytes(chunk))
                    data = b"".join(chunks)
                else:
                    raise FilesFacadeError("IWI provider cannot supply a bounded authorized stream", code="provider_unavailable")
                if len(data) != int(source.size) or len(data) != int(entry.size):
                    raise FilesFacadeError("IWI source changed during preview", code="resource_changed")
                decode = iwi.preview_png if thumbnail is None else lambda value: iwi.thumbnail_png(value, *thumbnail)
                info, png = await asyncio.to_thread(decode, data)
                current = await provider.stat(context, origin_id=ref.origin_id)
                if (current.origin_id != ref.origin_id or not self._is_iwi_preview(current)
                    or self._provider_revision(current) != revision or not self._generation_current(context)):
                    raise FilesFacadeError("IWI source or access changed during preview", code="resource_changed")
                self._provider_for_ref(context, resource_ref, capability="preview")
                import re
                if not re.fullmatch(r"[A-Za-z0-9:_-]{1,64}", str(revision["kind"])) or not re.fullmatch(r"[A-Za-z0-9:._-]{1,256}", str(revision["value"])):
                    raise FilesFacadeError("IWI revision cannot be represented safely", code="provider_unavailable")
                evidence = {"source_sha256": info.source_sha256, "decoder": info.decoder_generation,
                            "variant": info.variant, "revision_kind": str(revision["kind"]), "revision_value": str(revision["value"])}
                etag = hashlib.sha256(json.dumps(evidence, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
                return ProviderContent(origin_id=ref.origin_id, filename=f"{entry.name}.png", media_type="image/png",
                                       data=png, size=len(png), etag=f"iwi-{etag}", preview_evidence=evidence)
        except TimeoutError as exc:
            raise FilesFacadeError("IWI preview exceeded its time limit", code="provider_unavailable") from exc
        except iwi.IwiUnsupported as exc:
            raise FilesFacadeError(str(exc), code="resource_unavailable") from exc

    @staticmethod
    def _inspection_format(entry: ProviderResource) -> str | None:
        from src.openclank.format_capabilities import inspection_hint
        if entry.kind != 'file' or 'preview' not in entry.capabilities:
            return None
        hint = inspection_hint(entry.name)
        return hint[0] if hint is not None else None

    async def _inspection_stat(self, provider, ref, context, *, allow_elf=False):
        method = getattr(provider, 'stat_inspection', None)
        if ref.provider != 'host' or not callable(method):
            raise FilesFacadeError('Bounded inspection identity unavailable', code='provider_unavailable')
        entry = await method(context, origin_id=ref.origin_id)
        if entry.origin_id != ref.origin_id or not (allow_elf and entry.kind == 'file' and 'preview' in entry.capabilities) and self._inspection_format(entry) is None:
            raise FilesFacadeError('Inspection source unavailable', code='resource_unavailable')
        return entry

    async def inspect_safetensors(self, context: ProviderContext, *, resource_ref: str, expected_revision: Mapping[str, Any], format_id: str = 'safetensors') -> dict[str, Any]:
        from src.openclank.format_adapters import safetensors, gguf, asar, fastfile, wasm, elf, pe, macho, sqlite_metadata
        if format_id not in {'safetensors', 'gguf', 'asar', 'fastfile', 'wasm', 'elf', 'pe', 'macho', 'sqlite'}:
            raise FilesFacadeError("Unknown inspection format", code="resource_unavailable")
        matches = lambda entry: (entry.kind == 'file' and 'preview' in entry.capabilities and (format_id in {'elf', 'pe', 'macho', 'sqlite'} or self._inspection_format(entry) == format_id))
        provider, ref = self._provider_for_ref(context, resource_ref, capability="preview")
        if ref.provider != "host":
            raise FilesFacadeError("Provider range semantics are unavailable", code="provider_unavailable")
        try:
            async with asyncio.timeout(15):
                entry = await self._inspection_stat(provider, ref, context, allow_elf=format_id in {'elf','pe','macho','sqlite'})
                revision = self._provider_revision(entry)
                if entry.origin_id != ref.origin_id or not matches(entry) or revision is None or revision != dict(expected_revision):
                    raise FilesFacadeError("Inspection source changed", code="resource_changed")
                if type(entry.size) is not int or entry.size < 0:
                    raise FilesFacadeError("Source size is unavailable", code="provider_unavailable")
                source = await provider.content(context, origin_id=ref.origin_id)
                if source.origin_id != ref.origin_id or source.size != entry.size or source.stream is None or source.data is not None or source.path is not None:
                    raise FilesFacadeError("Provider does not support bounded ranges", code="provider_unavailable")
                if revision.get('kind') != 'hostObjectIdentity' or source.etag != revision.get('value'):
                    raise FilesFacadeError('Inspection stream identity changed', code='resource_changed')
                async def read_range(offset: int, length: int) -> bytes:
                    chunks = []; total = 0
                    async for chunk in source.stream(offset, length):
                        if not isinstance(chunk, bytes):
                            raise FilesFacadeError("Provider returned invalid range", code="provider_unavailable")
                        total += len(chunk)
                        if total > length:
                            raise FilesFacadeError("Provider exceeded requested range", code="provider_unavailable")
                        chunks.append(chunk)
                    if total != length:
                        raise FilesFacadeError("Source range is truncated", code="resource_changed")
                    return b"".join(chunks)
                if format_id == 'safetensors':
                    prefix = await read_range(0, 8)
                    length = safetensors.header_length(prefix, entry.size)
                    header = await read_range(8, length)
                    result = safetensors.inspect(prefix, header, entry.size)
                elif format_id == 'asar':
                    prefix = await read_range(0, 8)
                    length = asar.header_length(prefix, entry.size)
                    header = await read_range(8, length)
                    result = asar.inspect(prefix, header, entry.size)
                elif format_id == 'fastfile':
                    result = await fastfile.inspect(read_range, entry.size)
                elif format_id == 'wasm':
                    result = await wasm.inspect(read_range, entry.size)
                elif format_id == 'elf':
                    result = await elf.inspect(read_range, entry.size)
                elif format_id == 'pe':
                    result = await pe.inspect(read_range, entry.size)
                elif format_id == 'macho':
                    result = await macho.inspect(read_range, entry.size)
                elif format_id == 'sqlite':
                    result = await sqlite_metadata.inspect(read_range, entry.size)
                else:
                    result = await gguf.inspect(read_range, entry.size)
                current = await self._inspection_stat(provider, ref, context, allow_elf=format_id in {'elf','pe','macho','sqlite'})
                if current.origin_id != ref.origin_id or not matches(current) or self._provider_revision(current) != revision or not self._generation_current(context):
                    raise FilesFacadeError("Inspection source or access changed", code="resource_changed")
                self._provider_for_ref(context, resource_ref, capability="preview")
                receipt = {"version":1, "resourceRef":resource_ref, "providerRevision":dict(revision), "policyGeneration":context.policy_generation, **result}
                if format_id == 'sqlite' and len(json.dumps(receipt, ensure_ascii=True, separators=(',', ':')).encode('ascii')) > sqlite_metadata.MAX_OUTPUT:
                    raise FilesFacadeError('SQLite inspection receipt limit exceeded', code='resource_unavailable')
                return receipt
        except (safetensors.Unsupported, gguf.Unsupported, asar.AsarUnsupported, fastfile.FastfileUnsupported, wasm.WasmUnsupported, elf.ElfUnsupported, pe.PeUnsupported, macho.MachoUnsupported, sqlite_metadata.SqliteUnsupported, TimeoutError) as exc:
            raise FilesFacadeError(str(exc) or "Inspection deadline exceeded", code="resource_unavailable") from exc

    async def save_iwi_png_copy(self, context: ProviderContext, *, request: Mapping[str, Any]) -> dict[str, Any]:
        """Revalidate the captured preview, then use the existing staged import receipt."""
        import re
        required = {"operation_id", "item_id", "generation", "source_ref", "revision", "source_sha256", "decoder", "png_sha256", "destination_ref", "name"}
        if set(request) != required or request.get("generation") != context.policy_generation:
            raise FilesFacadeError("Conversion request or policy is invalid", code="invalid_resource_request")
        operation = self._bounded_id(request.get("operation_id"), "operation_id")
        self._bounded_id(request.get("item_id"), "item_id")
        if (not isinstance(request.get("revision"), Mapping) or set(request["revision"]) != {"kind", "value"}
            or any(not isinstance(request.get(key), str) or not re.fullmatch(r"[a-f0-9]{64}", request[key]) for key in ("source_sha256", "png_sha256"))
            or not isinstance(request.get("decoder"), str) or not re.fullmatch(r"[A-Za-z0-9:._-]{1,128}", request["decoder"])
            or not isinstance(request.get("name"), str) or not request["name"].lower().endswith(".png")
            or not isinstance(request.get("source_ref"), str) or not isinstance(request.get("destination_ref"), str)):
            raise FilesFacadeError("Conversion evidence is invalid", code="invalid_resource_request")
        evidence = {key: request[key] for key in ("source_ref", "revision", "source_sha256", "decoder", "png_sha256", "destination_ref", "name", "item_id", "generation")}
        existing = self._load_operation(context, operation)
        if existing is not None:
            prior = (existing.get("receipt") or {}).get("_import_binding", {}).get("derived_source")
            if prior != evidence or existing.get("generation") != context.policy_generation:
                raise FilesFacadeError("Operation identity was already used for another request", code="idempotency_conflict")
            return await self.operation_receipt(context, operation_id=operation)
        preview = await self._iwi_preview(context, resource_ref=request["source_ref"], expected_revision=request["revision"])
        actual = preview.preview_evidence or {}
        if (actual.get("source_sha256") != request["source_sha256"] or actual.get("decoder") != request["decoder"]
            or hashlib.sha256(preview.data or b"").hexdigest() != request["png_sha256"]):
            raise FilesFacadeError("The captured preview changed; reopen it", code="resource_changed")
        class Upload:
            filename = request["name"]
            content_type = "image/png"
            def __init__(self, data): self.data, self.offset = data, 0
            async def read(self, size):
                chunk = self.data[self.offset:self.offset + size]
                self.offset += len(chunk)
                return chunk
        return await self.import_file(context, upload=Upload(preview.data), source_evidence=evidence,
            metadata={"operation_id": operation, "item_id": request["item_id"], "generation": context.policy_generation,
                      "destination_ref": request["destination_ref"], "name": request["name"], "collision": "fail"})

    async def capture_media_destination(self, context: ProviderContext, *, resource_ref: str) -> dict[str, Any]:
        provider, ref = self._provider_for_ref(context, resource_ref, capability="write")
        entry = await provider.stat(context, origin_id=ref.origin_id)
        revision = self._provider_revision(entry)
        if (entry.origin_id != ref.origin_id or entry.kind not in _DIRECTORY_KINDS
            or "write" not in entry.capabilities or "children" not in entry.capabilities
            or revision is None or not self._generation_current(context)):
            raise FilesFacadeError("media destination is unavailable", code="resource_unavailable")
        return {"destination_ref": resource_ref, "destination_revision": dict(revision), "destination_label":str(entry.name)[:240]}

    async def capture_media_recipe(self, context: ProviderContext, *, resource_ref: str,
                                   expected_revision: Mapping[str, Any] | None = None, include_resource_id: bool = False) -> tuple[dict[str, Any], bytes]:
        """Capture a bounded, current Files recipe for a private media submission."""
        if type(include_resource_id) is not bool: raise FilesFacadeError('invalid media source', code='resource_unavailable')
        if not include_resource_id:
            return await self._capture_media_source(context, resource_ref=resource_ref, expected_revision=expected_revision, limit=1024 * 1024)
        provider, ref = self._provider_for_ref(context, resource_ref, capability='download')
        evidence, data = await self._capture_media_source(context, resource_ref=resource_ref, expected_revision=expected_revision, limit=1024 * 1024)
        current_provider, current = self._provider_for_ref(context, resource_ref, capability='download')
        if (current.provider != ref.provider or current.stable_id != ref.stable_id or current.origin_id != ref.origin_id
                or not isinstance(ref.stable_id,str) or not ref.stable_id or len(ref.stable_id.encode('utf-8'))>256
                or current_provider is not provider or not self._generation_current(context)):
            raise FilesFacadeError('media source changed', code='resource_changed')
        return {**evidence, 'recipe_resource_id':ref.stable_id}, data

    async def capture_sprite_frame(self, context: ProviderContext, *, resource_ref: str,
                                   expected_revision: Mapping[str, Any] | None = None):
        _provider, ref = self._provider_for_ref(context, resource_ref, capability='download')
        evidence, data = await self._capture_media_source(context, resource_ref=resource_ref, expected_revision=expected_revision, limit=8 * 1024 * 1024)
        _current_provider, current = self._provider_for_ref(context, resource_ref, capability='download')
        if current.stable_id != ref.stable_id or current.provider != ref.provider:
            raise FilesFacadeError('sprite source changed', code='resource_changed')
        return {'ref':resource_ref,'resourceID':ref.stable_id,'revision':evidence['recipe_revision'],
                'sha256':evidence['recipe_sha256']}, data

    async def capture_media_image(self, context: ProviderContext, *, resource_ref: str,
                                  expected_revision: Mapping[str, Any] | None = None) -> tuple[dict[str, Any], bytes]:
        """Capture one exact PNG/JPEG through the existing bounded Files owner."""
        import io
        import warnings
        from PIL import Image
        def valid_text(value, limit):
            return (type(value) is str and bool(value) and len(value.encode('utf-8')) <= limit
                    and not any(ord(c) < 32 for c in value) and '://' not in value)
        provider, ref = self._provider_for_ref(context, resource_ref, capability='download')
        evidence, data = await self._capture_media_source(context, resource_ref=resource_ref,
            expected_revision=expected_revision, limit=4 * 1024 * 1024)
        revision = evidence['recipe_revision']
        if (not valid_text(resource_ref, 16384) or not valid_text(ref.stable_id, 512)
                or not valid_text(evidence['recipe_name'], 240) or type(revision) is not dict
                or set(revision) != {'kind','value'} or any(not valid_text(revision[k], 512) for k in revision)):
            raise FilesFacadeError('image source unavailable', code='resource_unavailable')
        def decode():
            with warnings.catch_warnings():
                warnings.simplefilter('error', Image.DecompressionBombWarning)
                with Image.open(io.BytesIO(data)) as image:
                    kind, (width, height) = image.format, image.size
                    if (kind not in ('PNG','JPEG') or not 1 <= width <= 4096 or not 1 <= height <= 4096
                            or width * height > 16777216 or getattr(image, 'n_frames', 1) != 1
                            or getattr(image, 'is_animated', False)):
                        raise ValueError('unsupported image')
                    image.verify()
                with Image.open(io.BytesIO(data)) as image:
                    image.load()
                return ('image/png' if kind == 'PNG' else 'image/jpeg'), width, height
        task = asyncio.create_task(asyncio.to_thread(decode)); cancelled = False
        while True:
            try:
                mime, width, height = await asyncio.shield(task)
                break
            except asyncio.CancelledError:
                cancelled = True
                if task.done():
                    try: task.result()
                    except Exception: pass
                    raise
            except Exception as exc:
                if cancelled: raise asyncio.CancelledError from None
                raise FilesFacadeError('unsupported image', code='resource_unavailable') from exc
        if cancelled: raise asyncio.CancelledError
        current_provider, current = self._provider_for_ref(context, resource_ref, capability='download')
        current_entry = await current_provider.stat(context, origin_id=current.origin_id)
        if (current.provider != ref.provider or current.stable_id != ref.stable_id or current.origin_id != ref.origin_id
                or current_entry.origin_id != ref.origin_id or self._provider_revision(current_entry) != revision
                or not self._generation_current(context)):
            raise FilesFacadeError('image source changed', code='resource_changed')
        return {'ref':resource_ref, 'resourceID':ref.stable_id, 'revision':revision,
                'sha256':evidence['recipe_sha256'], 'mime_type':mime, 'width':width, 'height':height,
                'name':evidence['recipe_name']}, data

    async def capture_media_glb(self, context: ProviderContext, *, resource_ref: str,
                                expected_revision: Mapping[str, Any] | None = None, textured: bool = False) -> tuple[dict[str, Any], bytes]:
        """Capture the exact bounded GLB under the same Files authority."""
        if type(textured) is not bool:
            raise FilesFacadeError('unsupported GLB', code='resource_unavailable')
        from src.openclank.blender_child import validate_glb
        evidence, data = await self._capture_media_source(context, resource_ref=resource_ref,
            expected_revision=expected_revision, limit=64 * 1024 * 1024)
        try:
            validate_glb(data, textured=textured)
        except (ValueError, UnicodeError, RecursionError) as exc:
            raise FilesFacadeError('unsupported GLB', code='resource_unavailable') from exc
        return evidence, data

    async def _capture_media_source(self, context: ProviderContext, *, resource_ref: str,
                                    expected_revision: Mapping[str, Any] | None, limit: int) -> tuple[dict[str, Any], bytes]:
        provider, ref = self._provider_for_ref(context, resource_ref, capability="download")
        entry = await provider.stat(context, origin_id=ref.origin_id)
        revision = self._provider_revision(entry)
        if (entry.origin_id != ref.origin_id or entry.kind != "file" or revision is None
            or entry.size is None or type(entry.size) is not int or not 0 < entry.size <= limit
            or (expected_revision is not None and dict(expected_revision) != dict(revision))):
            raise FilesFacadeError("media recipe is unavailable or changed", code="resource_changed")
        source = await provider.content(context, origin_id=ref.origin_id)
        if source.origin_id != ref.origin_id or source.size != entry.size:
            raise FilesFacadeError("media recipe changed", code="resource_changed")
        if isinstance(source.data, bytes) and source.path is None and source.stream is None:
            data = source.data
        elif source.stream is not None and source.data is None and source.path is None:
            chunks = []
            total = 0
            async for chunk in source.stream(0, entry.size + 1):
                if not isinstance(chunk, bytes):
                    raise FilesFacadeError("invalid media recipe stream", code="provider_unavailable")
                total += len(chunk)
                if total > entry.size:
                    raise FilesFacadeError("media recipe changed", code="resource_changed")
                chunks.append(chunk)
            data = b"".join(chunks)
        else:
            raise FilesFacadeError("bounded media recipe read unavailable", code="unsupported_provider_kind")
        current = await provider.stat(context, origin_id=ref.origin_id)
        if (len(data) != entry.size or current.origin_id != ref.origin_id
            or self._provider_revision(current) != revision or not self._generation_current(context)):
            raise FilesFacadeError("media recipe or access changed", code="resource_changed")
        return {"recipe_ref": resource_ref, "recipe_name": str(entry.name)[:240], "recipe_revision": dict(revision),
                "recipe_sha256": hashlib.sha256(data).hexdigest()}, data

    async def content(
        self,
        context: ProviderContext,
        *,
        resource_ref: str,
        capability: str = "download",
        expected_revision: Mapping[str, Any] | None = None,
    ) -> ProviderContent:
        if capability not in {"preview", "download"}:
            raise FilesFacadeError("unsupported content purpose")
        provider, ref = self._provider_for_ref(context, resource_ref, capability=capability)
        if capability == "preview":
            entry = await provider.stat(context, origin_id=ref.origin_id)
            if self._is_iwi_preview(entry):
                return await self._iwi_preview(context, resource_ref=resource_ref, expected_revision=expected_revision)
        try:
            content = await provider.content(context, origin_id=ref.origin_id)
        except AttributeError as exc:
            raise FilesFacadeError("resource content is unavailable", code="provider_unavailable") from exc
        if content.origin_id != ref.origin_id:
            raise FilesFacadeError("provider returned mismatched content", code="provider_unavailable")
        # WebM range playback needs the opened stream and revision observation
        # to describe the same Host object, not merely the same path.
        if expected_revision is not None and str(content.filename or '').lower().endswith('.webm'):
            if ref.provider != 'host' or not callable(getattr(provider,'stat_inspection',None)):
                raise FilesFacadeError('Revision-bound video stream is unavailable', code='resource_unavailable')
            first = await provider.stat_inspection(context, origin_id=ref.origin_id)
            current = await provider.stat(context, origin_id=ref.origin_id)
            after = await provider.stat_inspection(context, origin_id=ref.origin_id)
            if (content.origin_id != first.origin_id or content.size != first.size or content.etag != (first.revision or {}).get('value')
                or first.revision != after.revision or first.size != after.size or self._provider_revision(current) != dict(expected_revision)
                or not self._generation_current(context)):
                raise FilesFacadeError('Revision-bound video stream changed', code='resource_changed')
        sources = sum(value is not None for value in (content.data, content.path, content.stream))
        if sources != 1:
            raise FilesFacadeError("provider returned an invalid content source", code="provider_unavailable")
        if not str(content.filename or "").strip() or not str(content.media_type or "").strip():
            raise FilesFacadeError("provider returned invalid content metadata", code="provider_unavailable")
        if content.data is not None and content.size not in {None, len(content.data)}:
            raise FilesFacadeError("provider returned a mismatched content size", code="provider_unavailable")
        await self._touch_recent(
            context,
            ref=ref,
            kind=ref.kind,
            display_name=content.filename,
        )
        return content

    async def thumbnail(
        self,
        context: ProviderContext,
        *,
        resource_ref: str,
        width: int,
        height: int,
        scale: float,
        icon: bool = False,
        expected_revision: Mapping[str, Any] | None = None,
    ) -> bytes:
        provider, ref = self._provider_for_ref(context, resource_ref, capability="preview")
        thumbnail_stat = getattr(provider, "stat_for_thumbnail", provider.stat)
        entry = await thumbnail_stat(context, origin_id=ref.origin_id)
        if entry.origin_id != ref.origin_id or "preview" not in entry.capabilities:
            raise FilesFacadeError("Preview source is unavailable", code="resource_unavailable")
        if not icon and self._is_iwi_preview(entry):
            return (await self._iwi_preview(context, resource_ref=resource_ref, expected_revision=expected_revision,
                                            thumbnail=(width, height, scale))).data or b""
        render = getattr(provider, "thumbnail", None)
        available = bool(entry.native_icon_available) if icon else entry.native_thumbnail_available is not False
        if entry.kind != "file" or not callable(render) or not available:
            raise FilesFacadeError("Native preview is unavailable", code="resource_unavailable")
        png = await render(
            context,
            origin_id=ref.origin_id,
            width=width,
            height=height,
            scale=scale,
            **({"icon": True} if icon else {}),
        )
        if len(png) > 4 * 1024 * 1024 or not png.startswith(b"\x89PNG\r\n\x1a\n"):
            raise FilesFacadeError("native content thumbnail is invalid", code="provider_unavailable")
        return png

    async def watch(
        self,
        context: ProviderContext,
        *,
        resource_ref: str,
    ) -> AsyncIterator[Mapping[str, Any]]:
        provider, ref = self._provider_for_ref(context, resource_ref, capability="watch")
        current = await provider.stat(context, origin_id=ref.origin_id)
        if current.origin_id != ref.origin_id or current.kind != "folder" or "watch" not in current.capabilities:
            raise FilesFacadeError("resource watch is unavailable", code="resource_unavailable")
        method = getattr(provider, "watch", None)
        if not callable(method):
            raise FilesFacadeError("resource watch is unavailable", code="provider_unavailable")
        return method(context, origin_id=ref.origin_id)


__all__ = [
    "CURSOR_TTL_SECONDS",
    "FACADE_VERSION",
    "MAX_PAGE_SIZE",
    "OPEN_TARGET_APPS",
    "FilesFacade",
    "FilesFacadeError",
    "FilesProvider",
    "ProviderContent",
    "ProviderContext",
    "ProviderPage",
    "ProviderResource",
    "ProviderWorkspaceTarget",
]
