"""Concrete managed-data adapters for the unified Files facade.

These adapters query the existing domain owners. They do not copy bodies or
bytes into a Files database and they never return raw provider IDs to clients;
``FilesFacade`` seals those IDs in owner-bound ResourceRefs.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import mimetypes
import os
import re
import stat as stat_module
import uuid
from datetime import datetime, timezone
from dataclasses import replace
from pathlib import Path
from typing import Any, Callable, Mapping

from sqlalchemy import LargeBinary, cast, func, literal, or_

from core.database import (
    Document,
    ChatMessage,
    EditorDraft,
    GalleryAlbum,
    GalleryImage,
    PublishedFile,
    Session as DbSession,
    SessionLocal,
)
from core.atomic_io import AtomicWriteConflict, atomic_write_bytes, fingerprint_bytes
from src.auth_helpers import copal_owner_for_user
from src.constants import DEEP_RESEARCH_DIR
from core.session_manager import _parse_msg_content
from src.generated_images import resolve_gallery_image_path
from src.openclank.copal_bridge import CopalBridgeError
from src.openclank.copal_resources import copal_resource_descriptor
from src.openclank.chat_lifecycle import ChatLifecycleError, ChatLifecycleService
from src.openclank.files_facade import (
    FilesFacadeError,
    ProviderContent,
    ProviderContext,
    ProviderPage,
    ProviderResource,
    SORT_KEYS,
)
from src.openclank.history_client import HistoryClient, HistoryClientError
from src.openclank.files_service_client import history_binding_for
from src.published_files import PublishedFileService


def _millis(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        numeric = float(value)
        return int(numeric if abs(numeric) >= 100_000_000_000 else numeric * 1000)
    if isinstance(value, datetime):
        current = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        return int(current.timestamp() * 1000)
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    current = parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    return int(current.timestamp() * 1000)


def _snapshot(parts: list[Any]) -> str:
    return hashlib.sha256(
        json.dumps(parts, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    ).hexdigest()


def _offset(cursor: str | None) -> int:
    try:
        value = int(cursor or 0)
    except (TypeError, ValueError) as exc:
        raise FilesFacadeError("provider cursor is malformed", code="stale_cursor") from exc
    if value < 0:
        raise FilesFacadeError("provider cursor is malformed", code="stale_cursor")
    return value


def _check_snapshot(expected: str | None, current: str) -> None:
    if expected is not None and expected != current:
        raise FilesFacadeError("provider listing changed", code="stale_cursor")


def _merge_search_pages(
    pages: list[ProviderPage],
    *,
    limit: int,
    sort: Mapping[str, Any],
) -> ProviderPage:
    by_origin: dict[str, ProviderResource] = {}
    for page in pages:
        for entry in page.entries:
            by_origin.setdefault(entry.origin_id, entry)
    rows = list(_sort_resources(list(by_origin.values()), sort))
    bounded = rows[:limit]
    snapshot = _snapshot([
        "search-v1",
        [page.snapshot for page in pages],
        [(row.origin_id, row.kind, row.size, row.modified_unix_ms) for row in bounded],
    ])
    return ProviderPage(
        tuple(bounded),
        next_cursor="truncated" if len(rows) > limit or any(page.next_cursor for page in pages) else None,
        total=len(rows),
        snapshot=snapshot,
    )


_DIRECTORY_KINDS = frozenset({"folder", "virtual_folder", "provider_root", "album"})


def _sort_resources(
    rows: tuple[ProviderResource, ...] | list[ProviderResource],
    sort: Mapping[str, Any],
) -> tuple[ProviderResource, ...]:
    """Sort an in-memory provider page with deterministic opaque-ID ties."""

    key_name = str(sort.get("key") or "name")

    def value(row: ProviderResource):
        if key_name == "kind":
            return str(row.sort_kind or row.mime_type or row.kind or "").casefold()
        if key_name == "size":
            return (row.size is None, int(row.size or 0))
        if key_name == "modified":
            return (row.modified_unix_ms is None, int(row.modified_unix_ms or 0))
        return row.name.casefold()

    def ordered(group: list[ProviderResource]) -> list[ProviderResource]:
        group.sort(
            key=lambda row: (value(row), row.name.casefold(), row.origin_id),
            reverse=str(sort.get("direction") or "asc") == "desc",
        )
        return group

    material = list(rows)
    if bool(sort.get("directories_first", True)):
        directories = ordered([row for row in material if row.kind in _DIRECTORY_KINDS])
        leaves = ordered([row for row in material if row.kind not in _DIRECTORY_KINDS])
        return tuple([*directories, *leaves])
    return tuple(ordered(material))


def _search_sort(sort: Mapping[str, Any], supported: tuple[str, ...]) -> dict[str, Any]:
    """Use the public tie field when a folder lacks the global primary."""

    if sort["key"] in supported:
        return dict(sort)
    return {**dict(sort), "key": "name"}


def _download_name(value: Any, extension: str = "") -> str:
    name = Path(str(value or "resource").replace("\\", "/")).name.strip() or "resource"
    name = "".join(character if ord(character) >= 32 and ord(character) != 127 else "_" for character in name)
    name = name[:240].strip() or "resource"
    if extension and "." not in name.rsplit(" ", 1)[-1]:
        name += extension
    return name


def _bytes_content(
    origin_id: str,
    *,
    filename: str,
    media_type: str,
    data: bytes,
    modified_unix_ms: int | None = None,
) -> ProviderContent:
    return ProviderContent(
        origin_id=origin_id,
        filename=filename,
        media_type=media_type,
        data=data,
        size=len(data),
        modified_unix_ms=modified_unix_ms,
        etag=hashlib.sha256(data).hexdigest(),
    )


def _path_content(
    origin_id: str,
    *,
    filename: str,
    media_type: str,
    path: str | Path,
    etag: str | None = None,
) -> ProviderContent:
    resolved = Path(path).resolve()
    try:
        current = os.stat(resolved, follow_symlinks=False)
    except OSError as exc:
        raise FilesFacadeError("resource content is unavailable", code="resource_unavailable") from exc
    if not stat_module.S_ISREG(current.st_mode):
        raise FilesFacadeError("resource content is unavailable", code="resource_unavailable")
    return ProviderContent(
        origin_id=origin_id,
        filename=filename,
        media_type=media_type or "application/octet-stream",
        path=resolved,
        size=int(current.st_size),
        modified_unix_ms=int(current.st_mtime_ns // 1_000_000),
        etag=etag,
        expected_identity=(
            int(current.st_dev),
            int(current.st_ino),
            int(current.st_size),
            int(current.st_mtime_ns),
        ),
    )


def _confined_path_content(
    origin_id: str,
    *,
    filename: str,
    media_type: str,
    path: str | Path,
    provider_root: str | Path | None,
    etag: str | None = None,
) -> ProviderContent:
    """Return provider-owned bytes only when the descriptor stays in its store.

    Provider path descriptors are an internal compatibility seam, not
    authority. The provider has already rechecked owner scope by opaque ID;
    this second check prevents a corrupt or compromised bridge response from
    turning that ID into an arbitrary host-file read.
    """

    if provider_root is None or not str(provider_root).strip() or not str(path).strip():
        raise FilesFacadeError("resource content is unavailable", code="resource_unavailable")
    try:
        root = Path(provider_root).expanduser().resolve()
        resolved = Path(path).expanduser().resolve(strict=True)
        relative = resolved.relative_to(root)
    except (OSError, RuntimeError, ValueError):
        raise FilesFacadeError("resource content is unavailable", code="resource_unavailable") from None
    if not relative.parts:
        raise FilesFacadeError("resource content is unavailable", code="resource_unavailable")
    return _path_content(
        origin_id,
        filename=filename,
        media_type=media_type,
        path=resolved,
        etag=etag,
    )


def _message_text(value: Any) -> str:
    content = _parse_msg_content(value)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        chunks: list[str] = []
        attachment_labels = {
            "image": "[Image attachment omitted from export]",
            "image_url": "[Image attachment omitted from export]",
            "input_image": "[Image attachment omitted from export]",
            "audio": "[Audio attachment omitted from export]",
            "input_audio": "[Audio attachment omitted from export]",
            "document": "[File attachment omitted from export]",
            "file": "[File attachment omitted from export]",
        }
        for block in content:
            if not isinstance(block, Mapping):
                continue
            block_type = str(block.get("type") or "").lower()
            text = block.get("text")
            if block_type == "text" and isinstance(text, str) and text:
                chunks.append(text)
            elif block_type in attachment_labels:
                chunks.append(attachment_labels[block_type])
        return "\n\n".join(chunks)
    return ""


def _preview_kind_for_mime(media_type: Any) -> str | None:
    normalized = str(media_type or "").split(";", 1)[0].strip().lower()
    if normalized.startswith("image/") and normalized != "image/svg+xml":
        return "image"
    if normalized.startswith("audio/"):
        return "audio"
    if normalized.startswith("text/") or normalized in {
        "application/json",
        "application/markdown",
        "application/x-yaml",
    }:
        return "text"
    return None


def _document_extension(language: Any) -> str:
    return {
        "javascript": ".js",
        "typescript": ".ts",
        "python": ".py",
        "markdown": ".md",
        "md": ".md",
        "html": ".html",
        "css": ".css",
        "json": ".json",
        "yaml": ".yml",
        "rust": ".rs",
        "sql": ".sql",
        "pdf": ".md",
        "text": ".txt",
    }.get(str(language or "text").lower(), ".txt")


def _document_download_name(value: Any, language: Any) -> str:
    normalized = str(language or "text").lower()
    extension = _document_extension(normalized)
    name = _download_name(value)
    if normalized == "pdf":
        # The editor's legacy `pdf` language is Markdown source for a PDF
        # wrapper, not a PDF byte stream. Never label those UTF-8 bytes .pdf.
        if name.lower().endswith(".pdf"):
            name = name[:-4]
        if not name.lower().endswith(".md"):
            name += ".md"
        return name
    return _download_name(name, extension)


_GALLERY_STORAGE_NAME = re.compile(r"^[A-Za-z0-9._-]{1,128}$")


def _safe_gallery_storage_name(value: Any) -> str | None:
    filename = str(value or "")
    if (
        not _GALLERY_STORAGE_NAME.fullmatch(filename)
        or filename in {".", ".."}
        or Path(filename).name != filename
    ):
        return None
    return filename


def _research_report_bytes(data: Mapping[str, Any]) -> bytes:
    return str(data.get("raw_report") or data.get("result") or "").encode("utf-8")


class CopalFilesProvider:
    name = "copal"
    _WORKSPACE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
    _ROOT_SORTS = ("name",)
    _DOCUMENT_SORTS = SORT_KEYS

    def __init__(self, bridge: Any, *, workspace_id: str = "default", operation_store: Any | None = None) -> None:
        self.bridge = bridge
        self.workspace_id = self._workspace(workspace_id)
        self.operation_store = operation_store
        self._preparations: dict[tuple[str, str, str], dict[str, Any]] = {}
        self._preparation_lock = asyncio.Lock()

    @staticmethod
    def _attachment_operation_id(operation_id: str) -> str:
        return f"__copal_attachment__{operation_id}"

    def _attachment_load(self, context: ProviderContext, operation_id: str) -> dict[str, Any] | None:
        getter = getattr(self.operation_store, "get_operation", None)
        if not callable(getter):
            return None
        loaded = getter(owner_subject_id=context.owner_subject_id, operation_id=self._attachment_operation_id(operation_id))
        return dict(loaded.get("receipt") or {}) if isinstance(loaded, Mapping) else None

    def _attachment_save(self, context: ProviderContext, operation_id: str, digest: str, descriptor: Mapping[str, Any]) -> None:
        recorder = getattr(self.operation_store, "record_operation", None)
        if not callable(recorder):
            return
        try:
            recorder(owner_subject_id=context.owner_subject_id, operation_id=self._attachment_operation_id(operation_id), request_digest=digest, generation=int(context.policy_generation), receipt=dict(descriptor), phase=str(descriptor.get("phase") or "complete"))
        except TypeError:
            recorder(owner_subject_id=context.owner_subject_id, operation_id=self._attachment_operation_id(operation_id), request_digest=digest, generation=int(context.policy_generation), receipt=dict(descriptor))

    async def attachment_status(self, context: ProviderContext, *, operation_id: str) -> Mapping[str, Any] | None:
        """Reconstruct a prepared asset after a provider/facade crash window."""
        planned = self._attachment_load(context, operation_id)
        if not planned:
            return None
        if planned.get("operation_id") != operation_id or planned.get("account_id") != context.owner_subject_id or planned.get("generation") != int(context.policy_generation):
            return None
        asset = planned.get("asset") if isinstance(planned.get("asset"), Mapping) else {}
        asset_name = str(asset.get("name") or "").strip()
        workspace = self._workspace(planned.get("workspace_id") or self.workspace_id)
        if workspace != str(context.workspace_id or "default"):
            return None
        if not asset_name:
            return None
        try:
            page = await self.bridge.call("metadata_page", {
                **self._scope(context, workspace), "state": "active", "hidden": "include", "corpus": "all",
                "query": asset_name, "limit": 20, "sort_key": "name", "sort_direction": "asc",
            })
        except Exception:
            return None
        docs = page.get("docs") if isinstance(page, Mapping) else None
        row = next((item for item in docs or () if isinstance(item, Mapping) and str(item.get("name") or "") == asset_name and str(item.get("kind") or "") == "asset"), None)
        if row is None:
            return None
        asset_id = str(row.get("id") or "").strip()
        if not asset_id:
            return None
        try:
            if int(row.get("size")) != int(planned.get("asset_size")):
                return None
        except (TypeError, ValueError):
            return None
        expected_digest = str(planned.get("source_digest") or "").lower()
        head = str(row.get("head") or "")
        if not head.startswith("sha256:") or head.split(":", 2)[1].lower() != expected_digest:
            return None
        complete = dict(planned)
        complete["generation"] = int(context.policy_generation)
        complete["asset"] = {**asset, "resource_key": {"provider": "copal", "account_id": context.owner_subject_id, "workspace_id": workspace, "resource_id": asset_id}, "revision": {"kind": "contentDigest", "value": str(planned.get("source_digest") or "").removeprefix("sha256:")}}
        complete.pop("phase", None)
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
    ) -> Mapping[str, Any]:
        """Materialize an authorized source into a Copal asset.

        The facade supplies the already authorized source adapter and origin;
        this provider rechecks both revisions and the writable target before
        asking Copal's scoped asset primitive to write bytes.  The target
        document is never edited here.  The small process-local receipt map
        makes retries and lost HTTP replies idempotent during one service
        lifetime; Copal's asset operation remains owner/workspace scoped.
        """
        if str(mode or "").lower() not in {"link", "embed"}:
            raise FilesFacadeError("attachment mode is invalid", code="invalid_resource_request")
        if source_provider is None or source_entry is None or not source_origin_id:
            raise FilesFacadeError("attachment source is unavailable", code="resource_unavailable")
        target_ref = str(target.get("resource_ref") or "").strip()
        target_origin = str(target_origin_id or "").strip()
        _prefix, workspace, document_id = self._origin_parts(target_origin, "document")
        if document_id is None or workspace != self.workspace_id:
            raise FilesFacadeError("attachment target is unavailable", code="resource_unavailable")
        target_entry = await self.stat(context, origin_id=target_origin)
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
        content = await source_provider.content(context, origin_id=source_origin_id)
        limit = 10 * 1024 * 1024
        try:
            declared_size = None if content.size is None else int(content.size)
        except (TypeError, ValueError) as exc:
            raise FilesFacadeError("attachment source content is invalid", code="provider_unavailable") from exc
        if declared_size is not None and (declared_size < 0 or declared_size > limit):
            raise FilesFacadeError("attachment exceeds the configured limit", code="upload_too_large")
        if content.data is not None:
            data = bytes(content.data)
        elif content.path is not None:
            def read_bounded(path: str | Path) -> bytes:
                chunks: list[bytes] = []
                total = 0
                with Path(path).open("rb") as handle:
                    while total <= limit:
                        chunk = handle.read(min(512 * 1024, limit - total + 1))
                        if not chunk:
                            break
                        chunks.append(chunk)
                        total += len(chunk)
                        if total > limit:
                            break
                return b"".join(chunks)
            data = await asyncio.to_thread(read_bounded, content.path)
        elif content.stream is not None:
            chunks: list[bytes] = []
            total = 0
            try:
                async for chunk in content.stream(0, limit + 1):
                    if not isinstance(chunk, (bytes, bytearray, memoryview)):
                        raise FilesFacadeError("attachment source content is invalid", code="provider_unavailable")
                    piece = bytes(chunk)
                    remaining = limit - total + 1
                    chunks.append(piece[:remaining])
                    total += min(len(piece), remaining)
                    if total > limit:
                        break
            except FilesFacadeError:
                raise
            except Exception as exc:
                raise FilesFacadeError("attachment source content is unavailable", code="provider_unavailable") from exc
            data = b"".join(chunks)
        else:
            raise FilesFacadeError("attachment source content is unavailable", code="resource_unavailable")
        source_after = await source_provider.stat(context, origin_id=source_origin_id)
        if source_after.origin_id != source_origin_id or dict(source_after.revision or {}) != dict(source_current.revision or {}):
            raise FilesFacadeError("attachment source changed during materialization", code="resource_changed")
        if len(data) > limit:
            raise FilesFacadeError("attachment exceeds the configured limit", code="upload_too_large")
        if declared_size is not None and len(data) != declared_size:
            raise FilesFacadeError("attachment source changed during materialization", code="resource_changed")
        digest = hashlib.sha256(data).hexdigest()
        request_digest = hashlib.sha256(json.dumps({
            "account_id": str(context.owner_subject_id), "workspace_id": workspace,
            "operation_id": str(operation_id), "generation": int(context.policy_generation),
            "source": {key: source.get(key) for key in ("resource_ref", "import_receipt_id", "item_id", "expected_revision") if source.get(key) is not None},
            "target": {key: target.get(key) for key in ("kind", "resource_ref", "course_id", "lesson_id", "expected_revision") if target.get(key) is not None},
            "mode": str(mode or "").lower(), "source_digest": digest,
        }, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")).hexdigest()
        key = (str(context.owner_subject_id), workspace, str(operation_id))
        async with self._preparation_lock:
            previous = self._preparations.get(key)
            if previous is not None:
                if previous.get("request_digest") != request_digest:
                    raise FilesFacadeError("attachment operation conflicts", code="idempotency_conflict")
                return previous
            durable = self._attachment_load(context, str(operation_id))
            if durable is not None:
                if durable.get("request_digest") != request_digest:
                    raise FilesFacadeError("attachment operation conflicts", code="idempotency_conflict")
                if durable.get("phase") == "complete":
                    self._preparations[key] = durable
                    return durable
                recovered = await self.attachment_status(context, operation_id=str(operation_id))
                if recovered is not None:
                    self._preparations[key] = recovered
                    return recovered
                raise FilesFacadeError("attachment operation is pending reconciliation", code="operation_pending")
            name = _download_name(content.filename or source_current.name)
            _stem, dot, ext = name.rpartition(".")
            asset_name = f".files-attachment-{str(operation_id)[:96]}-{digest[:16]}"
            asset_ext = (ext if dot else "bin").lower()
            source_revision = source_current.revision or {"kind": "contentDigest", "value": digest}
            target_revision = target_entry.revision or {"kind": "copalHead", "value": ""}
            planned = {
                "operation_id": str(operation_id), "account_id": str(context.owner_subject_id), "generation": int(context.policy_generation), "phase": "pending", "workspace_id": workspace,
                "preparation_receipt_id": f"prep-{digest[:32]}", "source_revision": dict(source_revision),
                "target_identity": {"resource_ref": target_ref, "kind": "copal_document"}, "target_revision": dict(target_revision),
                "insertion": {"format": "markdown", "link_target": asset_name, "label": name, "media_kind": str(content.media_type or "application/octet-stream")},
                "asset": {"mime_type": str(content.media_type or "application/octet-stream"), "name": asset_name, "revision": {"kind": "contentDigest", "value": digest}},
                "source_digest": digest, "request_digest": request_digest, "asset_size": len(data),
                "source_identity": {"resource_ref": str(source.get("resource_ref") or ""), "provider": str(getattr(source_provider, "name", ""))},
                "mode": str(mode or "").lower(),
            }
            self._attachment_save(context, str(operation_id), request_digest, planned)
            result = await self.bridge.call("put_asset_scoped", {
                **self._scope(context, workspace),
                "name": asset_name,
                "ext": asset_ext,
                "base64": __import__("base64").b64encode(data).decode("ascii"),
            })
            doc = result.get("doc") if isinstance(result, Mapping) else None
            if not isinstance(doc, Mapping) or not str(doc.get("id") or ""):
                raise FilesFacadeError("Copal returned an invalid asset", code="provider_unavailable")
            asset_id = str(doc["id"])
            asset_key = {"provider": "copal", "account_id": context.owner_subject_id, "workspace_id": workspace, "resource_id": asset_id}
            receipt = {
                "operation_id": str(operation_id),
                "generation": int(context.policy_generation),
                "preparation_receipt_id": f"prep-{digest[:32]}",
                "source_revision": dict(source_revision),
                "target_identity": {"resource_ref": target_ref, "kind": "copal_document"},
                "target_revision": dict(target_revision),
                "insertion": {"format": "markdown", "link_target": asset_name, "label": name, "media_kind": str(content.media_type or "application/octet-stream")},
                "asset": {"resource_key": asset_key, "mime_type": str(content.media_type or "application/octet-stream"), "name": asset_name, "revision": {"kind": "contentDigest", "value": digest}},
                "source_digest": digest,
                "request_digest": request_digest, "asset_size": len(data),
                "account_id": str(context.owner_subject_id), "workspace_id": workspace,
                "source_identity": {"resource_ref": str(source.get("resource_ref") or ""), "provider": str(getattr(source_provider, "name", ""))},
                "mode": str(mode or "").lower(),
                "action_receipt": {"action_id": f"attachment-{operation_id}", "status": "complete", "phase": "complete", "durable": True},
            }
            self._attachment_save(context, str(operation_id), request_digest, receipt)
            self._preparations[key] = receipt
            return receipt

    @classmethod
    def _workspace(cls, value: Any) -> str:
        workspace = str(value or "default").strip()
        if not cls._WORKSPACE.fullmatch(workspace):
            raise FilesFacadeError("Copal workspace is invalid")
        return workspace

    @classmethod
    def _origin_parts(cls, origin_id: str, expected_prefix: str | None = None) -> tuple[str, str, str | None]:
        parts = str(origin_id or "").split(":", 2)
        if len(parts) < 2 or (expected_prefix is not None and parts[0] != expected_prefix):
            raise FilesFacadeError("Copal resource is unavailable", code="resource_unavailable")
        prefix = parts[0]
        workspace = cls._workspace(parts[1])
        suffix = parts[2] if len(parts) == 3 and parts[2] else None
        return prefix, workspace, suffix

    def _scope(self, context: ProviderContext, workspace_id: str) -> dict[str, str]:
        # The auth-disabled deployment uses the long-lived local installation
        # principal for Files, while Copal's legacy bridge stores that same
        # scope under its canonical ``local`` owner segment.
        owner_username = "" if context.owner_username == "local-installation" and context.owner_subject_id == "local-installation" else context.owner_username
        return {
            "owner": copal_owner_for_user(owner_username),
            "workspace_id": self._workspace(workspace_id),
        }

    async def resolve_resource_key(self, context: ProviderContext, *, resource_key: Mapping[str, Any]) -> ProviderResource:
        """Resolve an existing Copal descriptor through the bridge identity map.

        The descriptor's document ID is accepted only as a provider-owned
        stable key and is immediately reauthorized by ``metadata_get``.  No
        host path or display-name lookup is involved.
        """
        if str(resource_key.get("provider") or "").strip().lower() != "copal":
            raise FilesFacadeError("Copal resource key is unavailable", code="resource_unavailable")
        account = str(resource_key.get("account_id") or resource_key.get("accountId") or "").strip()
        if account and account != str(context.owner_subject_id):
            raise FilesFacadeError("Copal resource key is unavailable", code="resource_unavailable")
        workspace = self._workspace(resource_key.get("workspace_id") or resource_key.get("workspaceId") or self.workspace_id)
        if workspace != self.workspace_id:
            raise FilesFacadeError("Copal resource key is unavailable", code="resource_unavailable")
        document_id = str(resource_key.get("resource_id") or resource_key.get("resourceId") or "").strip()
        if not document_id or len(document_id) > 256:
            raise FilesFacadeError("Copal resource key is unavailable", code="resource_unavailable")
        try:
            row = await self.bridge.call("metadata_get", {
                **self._scope(context, workspace), "id": document_id,
                "state": "active", "hidden": "include", "corpus": "all",
            })
        except CopalBridgeError as exc:
            raise FilesFacadeError("Copal resource key is unavailable", code="resource_unavailable") from exc
        if not isinstance(row, Mapping):
            raise FilesFacadeError("Copal resource key is unavailable", code="resource_unavailable")
        return self._document(row, f"active:{workspace}:all", workspace)

    async def _action_with_history(
        self,
        context: ProviderContext,
        *,
        operation: str,
        args: dict[str, Any],
        action_id: str | None,
    ) -> tuple[Mapping[str, Any], dict[str, Any]]:
        """Apply one Files mutation with the trusted provider boundary receipt.

        Files routes do not have a Copal ``Request`` to pass through the route
        capture wrapper. Capture therefore belongs here, after the opaque ref
        has been authorized and before the provider bridge write. The receipt
        intentionally contains only the account-scoped action identity and
        terminal status; Copal IDs remain sealed inside the ResourceRef.
        """
        scope = self._scope(context, str(args.get("workspace_id") or self.workspace_id))
        supplied = str(action_id or "").strip()
        # A caller-provided ID is the idempotency key.  An omitted ID is a
        # fresh user action: deriving it from the resource would make a later
        # rename-back collide with the earlier receipt.
        mutation_id = supplied or "files-" + uuid.uuid4().hex
        history: dict[str, Any] = {"action_id": mutation_id, "status": "unconfigured", "durable": False, "phase": "unavailable"}
        client = None
        binding = history_binding_for(context.owner_username or "local-installation", "human")
        fallback_allowed = not isinstance(binding, Mapping)
        capture_ready = False
        if isinstance(binding, Mapping):
            binding_account = str(binding.get("account_id") or "").strip()
            if binding_account != str(context.owner_subject_id or "").strip():
                history.update(status="failed", phase="identity", error="history binding account does not match Files owner")
            elif binding.get("socket") and binding.get("token"):
                client = HistoryClient(
                    str(binding["socket"]),
                    actor_id=str(binding.get("actor_id") or context.owner_username or context.owner_subject_id),
                    account_id=binding_account,
                    token=str(binding["token"]),
                )
            else:
                history.update(status="failed", phase="before", error="history binding is configured but unavailable")
        before = None
        if client is not None:
            try:
                before = await self.bridge.call("get", {**scope, "id": args["id"]})
                resource_key = {
                    "account_id": context.owner_subject_id,
                    "workspace_id": str(args.get("workspace_id") or self.workspace_id),
                    "provider": "copal",
                    "resource_id": str(args["id"]),
                }
                envelope = {
                    "schema_version": 1,
                    "action_id": mutation_id,
                    "actor_account_id": context.owner_subject_id,
                    "resource_key": resource_key,
                    "guard_resource_ids": [resource_key], "modified_resource_ids": [resource_key], "operation": operation,
                    "expected_revision": None, "actor_id": context.owner_username or context.owner_subject_id, "actor_kind": "user",
                    "session_id": None, "run_id": None, "task_id": None, "tool_id": "files-facade",
                    "before_revision": str(before.get("head") or "") if isinstance(before, Mapping) else None,
                    "expected_after_revision": None,
                    "original_locator": {"provider": "copal", "workspace_id": resource_key["workspace_id"], "name": str(before.get("name") or "")} if isinstance(before, Mapping) else None,
                    "destination_locator": {"provider": "copal", "workspace_id": resource_key["workspace_id"], "name": str(args.get("name") or before.get("name") or "")} if isinstance(before, Mapping) else None,
                    "timestamp_millis": int(__import__("time").time() * 1000),
                    "coverage": {"metadata": {"coverage_kind": "KnownMutationHooks", "roots": [str(args.get("workspace_id") or self.workspace_id)], "exclusions": []}},
                    "per_resource_outcomes": None,
                }
                encoded = json.dumps(before, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8") if isinstance(before, Mapping) else None
                fingerprint = "missing" if encoded is None else "sha256:" + hashlib.sha256(encoded).hexdigest()
                await asyncio.to_thread(client.prepare, envelope, content=encoded, fingerprint=fingerprint)
                capture_ready = True
                history.update(status="prepared", phase="before")
            except HistoryClientError as exc:
                history.update(status="paused" if "history_paused_budget" in str(exc) else "failed", phase="before", error=str(exc))
            except Exception as exc:
                history.update(status="failed", phase="before", error=str(exc))
        try:
            result = await self.bridge.call(operation, args)
        except Exception:
            if client is not None:
                if capture_ready:
                    try: await asyncio.to_thread(client.record_live, mutation_id, {"action_id": mutation_id, "status": "NotCommitted", "after_unavailable": True})
                    except Exception: pass
                try: await asyncio.to_thread(client.abort, mutation_id)
                except Exception: pass
            raise
        if not capture_ready and fallback_allowed:
            # The Rust Copal bridge is itself the durable history authority.
            # Keep Files useful in installations without the optional history
            # worker by reading the committed version through that boundary.
            # Only opaque revision metadata crosses the facade; the provider
            # document id and history descriptions stay server-side.
            try:
                history_result = await self.bridge.call("history", {**scope, "id": args["id"]})
                changes = history_result.get("changes") if isinstance(history_result, Mapping) else None
                latest = changes[0] if isinstance(changes, list) and changes else {}
                history.update(
                    status="complete",
                    durable=True,
                    phase="complete",
                    receipt={
                        "action_id": mutation_id,
                        "status": "complete",
                        "revision": str(latest.get("commit") or "") if isinstance(latest, Mapping) else "",
                        "change_count": len(changes) if isinstance(changes, list) else 0,
                    },
                )
            except Exception as exc:
                history.update(status="failed", phase="after", error=str(exc))
        if capture_ready and client is not None:
            after = result.get("doc") if isinstance(result, Mapping) else None
            encoded_after = json.dumps(after, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8") if isinstance(after, Mapping) else None
            fingerprint_after = "missing" if encoded_after is None else "sha256:" + hashlib.sha256(encoded_after).hexdigest()
            try:
                await asyncio.to_thread(client.record_live, mutation_id, {"action_id": mutation_id, "status": "Committed", "fingerprint": fingerprint_after, "after_unavailable": encoded_after is None})
                receipt = await asyncio.to_thread(client.complete, mutation_id, content=encoded_after, fingerprint=fingerprint_after)
                history.update(status="complete", durable=True, phase="complete", receipt=receipt if isinstance(receipt, Mapping) else {})
            except Exception as exc:
                # A committed provider mutation remains successful when the
                # history worker disconnects during its after receipt.
                history.update(status="paused" if "history_paused_budget" in str(exc) else "failed", phase="after", error=str(exc))
        return result if isinstance(result, Mapping) else {}, history

    def _asset_root(self, context: ProviderContext, workspace_id: str) -> Path | None:
        scope = self._scope(context, workspace_id)
        scoped_vault = getattr(self.bridge, "_vault", None)
        if callable(scoped_vault):
            try:
                return Path(scoped_vault(scope["owner"], scope["workspace_id"]))
            except (OSError, TypeError, ValueError):
                return None
        data_dir = getattr(self.bridge, "data_dir", None)
        return Path(data_dir).expanduser() / "assets" if data_dir is not None else None

    def supported_sort_keys(self, *, parent_origin_id: str) -> tuple[str, ...]:
        prefix, _workspace, suffix = self._origin_parts(parent_origin_id)
        if prefix == "workspace" and suffix is None:
            return self._ROOT_SORTS
        if prefix in {"active", "system", "trash"} and suffix is not None:
            return self._DOCUMENT_SORTS
        raise FilesFacadeError("Copal resource is unavailable", code="resource_unavailable")

    async def roots(self, context: ProviderContext):
        if self.bridge is None:
            raise FilesFacadeError("Copal provider is unavailable", code="provider_unavailable")
        return [ProviderResource(
            f"workspace:{self.workspace_id}",
            "Copal",
            "provider_root",
            ("children", "stat", "search"),
            workspace_id=self.workspace_id,
            provenance={"domain": "copal", "workspace": self.workspace_id},
            open_target={"app": "copal_notes"},
            child_sort_keys=self._ROOT_SORTS,
        )]

    @staticmethod
    def _folder(origin_id: str, name: str, workspace_id: str) -> ProviderResource:
        return ProviderResource(
            origin_id,
            name,
            "virtual_folder",
            ("children", "stat", "search"),
            parent_origin_id=f"workspace:{workspace_id}",
            workspace_id=workspace_id,
            provenance={"domain": "copal", "view": origin_id.split(":", 1)[0]},
            child_sort_keys=CopalFilesProvider._DOCUMENT_SORTS,
        )

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
        prefix, workspace, suffix = self._origin_parts(parent_origin_id)
        root = f"workspace:{workspace}"
        if prefix == "workspace" and suffix is None:
            if cursor or snapshot:
                raise FilesFacadeError("provider cursor is stale", code="stale_cursor")
            rows = (
                self._folder(f"active:{workspace}:all", "Documents", workspace),
                self._folder(f"active:{workspace}:notes", "Notes", workspace),
                self._folder(f"active:{workspace}:wiki", "Wiki", workspace),
                self._folder(f"system:{workspace}:all", "System", workspace),
                self._folder(f"trash:{workspace}:all", "Trash", workspace),
            )
            rows = _sort_resources(rows, sort)
            return ProviderPage(rows, total=len(rows), snapshot="copal-folders-v1")

        if prefix not in {"active", "system", "trash"} or suffix is None:
            raise FilesFacadeError("Copal resource is unavailable", code="resource_unavailable")
        state, corpus_view = prefix, suffix
        corpus = {"notes": "notes", "wiki": "wiki"}.get(corpus_view, "all")
        args: dict[str, Any] = {
            **self._scope(context, workspace),
            "state": "trash" if state == "trash" else "active",
            "hidden": "only" if state == "system" else "exclude",
            "corpus": corpus,
            "query": query,
            "cursor": cursor,
            "snapshot": snapshot,
            "limit": limit,
            "sort_key": sort["key"],
            "sort_direction": sort["direction"],
        }
        try:
            result = await self.bridge.call("metadata_page", args)
        except CopalBridgeError as exc:
            code = "stale_cursor" if str(exc) == "stale_cursor" else "provider_unavailable"
            raise FilesFacadeError("Copal listing is unavailable", code=code) from exc
        rows = tuple(self._document(row, parent_origin_id, workspace) for row in result.get("docs") or ())
        return ProviderPage(
            rows,
            next_cursor=result.get("next_cursor"),
            total=int(result.get("total") or 0),
            snapshot=str(result.get("snapshot") or ""),
        )

    async def search(
        self,
        context: ProviderContext,
        *,
        query: str,
        limit: int,
        sort: Mapping[str, Any],
    ) -> ProviderPage:
        workspace = self.workspace_id
        pages = []
        for parent in (
            f"active:{workspace}:all",
            f"system:{workspace}:all",
            f"trash:{workspace}:all",
        ):
            pages.append(await self.children(
                context,
                parent_origin_id=parent,
                cursor=None,
                snapshot=None,
                limit=limit,
                sort=sort,
                query=query,
            ))
        return _merge_search_pages(pages, limit=limit, sort=sort)

    def _document(self, row: Mapping[str, Any], parent: str, workspace: str) -> ProviderResource:
        document_id = str(row.get("id") or "")
        if not document_id:
            raise FilesFacadeError("Copal returned invalid metadata", code="provider_unavailable")
        read_only = bool(row.get("builtin"))
        kind = str(row.get("kind") or "document").lower()
        asset = kind == "asset"
        mime_type = mimetypes.guess_type(str(row.get("name") or ""))[0] if asset else None
        preview_kind = (
            "image" if str(mime_type or "").startswith("image/")
            else "audio" if str(mime_type or "").startswith("audio/")
            else "text" if not asset else None
        )
        trashed = parent.startswith("trash:")
        read_only = bool(row.get("builtin") or row.get("readOnly"))
        capabilities = ["stat", "open", "read"]
        if trashed:
            if not read_only: capabilities.append("restore")
        elif not read_only:
            # Copal's native documents remain writable after exact-open. Keep
            # this capability in the sealed handle so the shared Editor can
            # retain a dirty buffer while Files actions rename the resource.
            capabilities.append("write")
            capabilities.append("download")
            if preview_kind:
                capabilities.append("preview")
            capabilities.extend(("rename", "move", "trash"))
        return ProviderResource(
            f"document:{workspace}:{document_id}",
            str(row.get("name") or "Untitled"),
            "asset" if asset else "document",
            tuple(capabilities),
            parent_origin_id=parent,
            workspace_id=workspace,
            mime_type=mime_type,
            size=int(row.get("size") or 0),
            modified_unix_ms=_millis(row.get("updatedAt") or row.get("ts")),
            provenance={"domain": "copal", "corpus": row.get("corpus"), "document_kind": row.get("kind")},
            open_target={"app": "copal_notes"},
            download_name=str(row.get("name") or "document.md"),
            preview_kind=preview_kind,
            sort_kind=kind,
            revision=(
                {"kind": "copalHead", "value": str(row.get("head") or row.get("revision"))}
                if row.get("head") is not None or row.get("revision") is not None else None
            ),
        )

    async def stat(self, context: ProviderContext, *, origin_id: str) -> ProviderResource:
        prefix, workspace, suffix = self._origin_parts(origin_id)
        if prefix == "workspace" and suffix is None:
            return ProviderResource(
                origin_id, "Copal", "provider_root", ("children", "stat", "search"),
                workspace_id=workspace,
                provenance={"domain": "copal", "workspace": workspace},
                open_target={"app": "copal_notes"},
                child_sort_keys=self._ROOT_SORTS,
            )
        if prefix in {"active", "system", "trash"} and suffix is not None:
            label = {
                "active": {"notes": "Notes", "wiki": "Wiki"}.get(suffix, "Documents"),
                "system": "System",
                "trash": "Trash",
            }[prefix]
            return self._folder(origin_id, label, workspace)
        if prefix != "document" or suffix is None:
            raise FilesFacadeError("Copal resource is unavailable", code="resource_unavailable")
        row = None
        state = "active"
        for candidate in ("active", "trash"):
            try:
                row = await self.bridge.call("metadata_get", {
                    **self._scope(context, workspace),
                    "id": suffix,
                    "state": candidate,
                    "hidden": "include",
                    "corpus": "all",
                })
                state = candidate
                break
            except CopalBridgeError:
                continue
        if not isinstance(row, Mapping):
            raise FilesFacadeError("Copal resource is unavailable", code="resource_unavailable")
        parent = f"trash:{workspace}:all" if state == "trash" else (
            f"system:{workspace}:all" if row.get("hidden") else f"active:{workspace}:all"
        )
        return self._document(row, parent, workspace)

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
        """Return one bounded, already-authorized Copal corpus snapshot.

        Filtering remains S04's typed parser. This adapter only binds the
        Base/corpus identities to the Copal metadata index and exposes provider
        rows for the facade to seal.
        """
        _base_prefix, workspace, _base_suffix = self._origin_parts(base_origin_id)
        corpus_prefix, corpus_workspace, corpus_suffix = self._origin_parts(corpus_origin_id)
        if workspace != corpus_workspace or corpus_prefix not in {"workspace", "active", "system", "trash"}:
            raise FilesFacadeError("Copal Base corpus is unavailable", code="resource_unavailable")
        if corpus_prefix == "workspace":
            state, hidden, corpus = "active", "exclude", "all"
        else:
            state = "trash" if corpus_prefix == "trash" else "active"
            hidden = "only" if corpus_prefix == "system" else "exclude"
            corpus = {"notes": "notes", "wiki": "wiki"}.get(corpus_suffix or "all", "all")
        offset = int(page) * int(page_size)
        wanted = offset + int(page_size)
        docs: list[Mapping[str, Any]] = []
        cursor = None
        snapshot = None
        complete = True
        result: Mapping[str, Any] = {}
        for _ in range(64):
            try:
                result = await self.bridge.call("metadata_page", {
                    **self._scope(context, workspace), "state": state, "hidden": hidden,
                    "corpus": corpus, "query": "", "cursor": cursor, "snapshot": snapshot,
                    "limit": 500, "sort_key": "name", "sort_direction": "asc",
                })
            except CopalBridgeError as exc:
                raise FilesFacadeError("Copal Base corpus is unavailable", code="resource_unavailable") from exc
            page_docs = result.get("docs") or []
            docs.extend(row for row in page_docs if isinstance(row, Mapping))
            cursor = result.get("next_cursor")
            snapshot = result.get("snapshot") or snapshot
            if not cursor or len(docs) >= wanted:
                break
        else:
            complete = False
        if cursor:
            complete = False
        rows = []
        for row in docs[offset:offset + int(page_size)]:
            if not isinstance(row, Mapping) or not row.get("id"):
                continue
            document_id = str(row["id"])
            rows.append({
                "origin_id": f"document:{workspace}:{document_id}",
                "resource_key": {"account_id": context.owner_subject_id, "workspace_id": workspace, "provider": "copal", "resource_id": document_id},
                "logical_path": str(row.get("name") or ""),
                "metadata": {"name": str(row.get("name") or "Untitled"), "kind": str(row.get("kind") or "document"), "corpus": str(row.get("corpus") or corpus), "capabilities": ["stat", "read", "open"]},
                "properties": dict(row.get("properties") or {}), "relations": [],
                "revision": {"kind": "copalHead", "value": str(row.get("head") or row.get("revision") or "")},
            })
        return {
            "snapshot_id": str(snapshot or "copal-base-snapshot"),
            "complete": bool(complete), "indexing": not bool(complete),
            "truncated": not bool(complete),
            "status": "complete" if complete else "indexing",
            "progress": {"visited": len(docs), "requested": wanted},
            "rows": rows, "total": int(result.get("total") or len(docs)),
        }

    async def content(self, context: ProviderContext, *, origin_id: str) -> ProviderContent:
        prefix, workspace, document_id = self._origin_parts(origin_id, "document")
        if prefix != "document" or document_id is None:
            raise FilesFacadeError("Copal resource is unavailable", code="resource_unavailable")
        try:
            metadata = await self.bridge.call("metadata_get", {
                **self._scope(context, workspace),
                "id": document_id,
                "state": "active",
                "hidden": "include",
                "corpus": "all",
            })
        except CopalBridgeError as exc:
            raise FilesFacadeError("Copal resource is unavailable", code="resource_unavailable") from exc
        kind = str(metadata.get("kind") or "note").lower()
        if kind == "asset":
            try:
                asset = await self.bridge.call("asset_path", {
                    **self._scope(context, workspace),
                    "id": document_id,
                })
            except CopalBridgeError as exc:
                raise FilesFacadeError("Copal resource is unavailable", code="resource_unavailable") from exc
            name = str(asset.get("name") or metadata.get("name") or "asset")
            return _confined_path_content(
                origin_id,
                filename=_download_name(name),
                media_type=mimetypes.guess_type(name)[0] or "application/octet-stream",
                path=str(asset.get("path") or ""),
                provider_root=self._asset_root(context, workspace),
            )
        try:
            row = await self.bridge.call("get", {
                **self._scope(context, workspace),
                "id": document_id,
            })
        except CopalBridgeError as exc:
            raise FilesFacadeError("Copal resource is unavailable", code="resource_unavailable") from exc
        markdown = kind in {"note", "markdown", "wiki"}
        text = str(row.get("text") or row.get("content") or "")
        if markdown:
            from routes.copal_routes import _note_markdown, _note_view

            normalized = _note_view(dict(row))
            text = text if normalized.get("rawPreserved") and row.get("storage") == "files" else _note_markdown(normalized)
        extension = ".md" if markdown else ".json"
        return _bytes_content(
            origin_id,
            filename=_download_name(row.get("name") or row.get("title"), extension),
            media_type="text/markdown; charset=utf-8" if markdown else "application/json; charset=utf-8",
            data=text.encode("utf-8"),
            modified_unix_ms=_millis(row.get("updatedAt") or row.get("ts")),
        )

    async def open_resource(self, context: ProviderContext, *, origin_id: str) -> Mapping[str, Any] | None:
        prefix, workspace, document_id = self._origin_parts(origin_id, "document")
        if prefix != "document" or document_id is None:
            return None
        try:
            metadata = await self.bridge.call("metadata_get", {
                **self._scope(context, workspace),
                "id": document_id,
                "state": "active",
                "hidden": "include",
                "corpus": "all",
            })
            row = await self.bridge.call("get", {
                **self._scope(context, workspace),
                "id": document_id,
            })
        except CopalBridgeError as exc:
            raise FilesFacadeError("Copal resource is unavailable", code="resource_unavailable") from exc
        kind = str(metadata.get("kind") or row.get("kind") or "note").lower()
        if kind == "asset":
            return None
        if kind in {"note", "wiki", "markdown"}:
            from routes.copal_routes import _note_view

            view = _note_view(dict(row))
            text = (
                str(row.get("text") or "")
                if view.get("rawPreserved") and row.get("storage") == "files"
                else str(view.get("text") or "")
            )
            return {
                "name": str(view.get("name") or metadata.get("name") or "Untitled"),
                "kind": str(view.get("kind") or kind),
                "corpus": str(view.get("corpus") or metadata.get("corpus") or "notes"),
                "text": text,
                "properties": dict(view.get("properties") or {}),
                # Related note targets are provider origin IDs. Exact-open is
                # intentionally a read-only first seam, so omit those targets
                # instead of leaking them to the browser.
                "relations": [],
                "tags": list(view.get("tags") or []),
                "read_only": bool(view.get("readOnly") or view.get("builtin") or metadata.get("readOnly") or metadata.get("builtin")),
                "resource": copal_resource_descriptor(
                    {**view, "id": document_id},
                    owner_account_id=context.owner_subject_id,
                    workspace_id=workspace,
                    writable_adapter=True,
                ),
            }
        return None

    async def action(
        self,
        context: ProviderContext,
        *,
        origin_id: str,
        action: str,
        args: Mapping[str, Any],
        action_id: str | None = None,
    ) -> ProviderResource:
        prefix, workspace, document_id = self._origin_parts(origin_id, "document")
        if prefix != "document" or document_id is None:
            raise FilesFacadeError("Copal resource is unavailable", code="resource_unavailable")
        if action in {"rename", "move"}:
            name = str(args.get("name") or "").strip()
            if not name: raise FilesFacadeError("document name is required")
            operation_args = {**self._scope(context, workspace), "id": document_id, "name": name, "action_id": action_id}
            operation = "rename"
        elif action == "trash":
            operation_args = {**self._scope(context, workspace), "id": document_id, "action_id": action_id}
            # ``trash`` is the read-only deleted-document listing operation in
            # the Copal bridge.  Files' destructive action is the scoped
            # versioned delete, which creates the tombstone and history op.
            operation = "delete"
        elif action == "restore":
            operation_args = {**self._scope(context, workspace), "id": document_id, "action_id": action_id}
            operation = "restore_deleted"
        else:
            raise FilesFacadeError("Copal action is unavailable", code="resource_unavailable")
        try:
            result, history = await self._action_with_history(context, operation=operation, args=operation_args, action_id=action_id)
        except CopalBridgeError as exc:
            code = "resource_unavailable" if "not found" in str(exc) else "provider_unavailable"
            raise FilesFacadeError("Copal resource action failed", code=code) from exc
        if not isinstance(result, Mapping):
            raise FilesFacadeError("Copal resource action returned invalid metadata", code="provider_unavailable")
        updated = await self.stat(context, origin_id=origin_id)
        return replace(updated, action_receipt=history)


class GalleryFilesProvider:
    name = "gallery"
    _ROOT_SORTS = ("name",)
    _MEDIA_SORTS = SORT_KEYS
    _ALBUM_SORTS = ("name", "modified")
    _DRAFT_SORTS = ("name", "modified")

    def __init__(
        self,
        session_factory: Callable = SessionLocal,
        *,
        image_resolver: Callable[[str], Path] = resolve_gallery_image_path,
    ) -> None:
        self.session_factory = session_factory
        self.image_resolver = image_resolver

    def supported_sort_keys(self, *, parent_origin_id: str) -> tuple[str, ...]:
        if parent_origin_id == "root":
            return self._ROOT_SORTS
        if parent_origin_id in {"photos", "favorites"} or parent_origin_id.startswith("album:"):
            return self._MEDIA_SORTS
        if parent_origin_id == "albums":
            return self._ALBUM_SORTS
        if parent_origin_id == "drafts":
            return self._DRAFT_SORTS
        raise FilesFacadeError("Gallery resource is unavailable", code="resource_unavailable")

    async def roots(self, context: ProviderContext):
        return [ProviderResource(
            "root",
            "Gallery",
            "provider_root",
            ("children", "stat", "search"),
            provenance={"domain": "gallery"},
            open_target={"app": "gallery"},
            child_sort_keys=self._ROOT_SORTS,
        )]

    @staticmethod
    def _folder(origin_id: str, name: str) -> ProviderResource:
        child_sort_keys = {
            "photos": GalleryFilesProvider._MEDIA_SORTS,
            "favorites": GalleryFilesProvider._MEDIA_SORTS,
            "albums": GalleryFilesProvider._ALBUM_SORTS,
            "drafts": GalleryFilesProvider._DRAFT_SORTS,
        }[origin_id]
        return ProviderResource(
            origin_id,
            name,
            "virtual_folder",
            ("children", "stat", "search"),
            parent_origin_id="root",
            provenance={"domain": "gallery", "view": origin_id},
            child_sort_keys=child_sort_keys,
        )

    async def children(self, context: ProviderContext, **kwargs) -> ProviderPage:
        return await asyncio.to_thread(self._children_sync, context, **kwargs)

    async def search(
        self,
        context: ProviderContext,
        *,
        query: str,
        limit: int,
        sort: Mapping[str, Any],
    ) -> ProviderPage:
        pages = []
        for parent in ("photos", "albums", "drafts"):
            pages.append(await self.children(
                context,
                parent_origin_id=parent,
                cursor=None,
                snapshot=None,
                limit=limit,
                sort=_search_sort(sort, self.supported_sort_keys(parent_origin_id=parent)),
                query=query,
            ))
        return _merge_search_pages(pages, limit=limit, sort=sort)

    def _children_sync(
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
        if parent_origin_id == "root":
            if cursor or snapshot:
                raise FilesFacadeError("provider cursor is stale", code="stale_cursor")
            rows = tuple(self._folder(origin, label) for origin, label in (
                ("photos", "Photos"),
                ("favorites", "Favorites"),
                ("albums", "Albums"),
                ("drafts", "Saved Projects"),
            ))
            rows = _sort_resources(rows, sort)
            return ProviderPage(rows, total=len(rows), snapshot="gallery-folders-v1")
        db = self.session_factory()
        try:
            if parent_origin_id in {"photos", "favorites"} or parent_origin_id.startswith("album:"):
                query_obj = db.query(
                    GalleryImage.id,
                    GalleryImage.filename,
                    GalleryImage.favorite,
                    GalleryImage.album_id,
                    GalleryImage.file_size,
                    GalleryImage.updated_at,
                    GalleryImage.created_at,
                ).filter(
                    GalleryImage.owner == context.owner_username,
                    GalleryImage.is_active == True,
                )
                if parent_origin_id == "favorites":
                    query_obj = query_obj.filter(GalleryImage.favorite == True)
                if parent_origin_id.startswith("album:"):
                    album_id = parent_origin_id.split(":", 1)[1]
                    owned = db.query(GalleryAlbum.id).filter(
                        GalleryAlbum.id == album_id,
                        GalleryAlbum.owner == context.owner_username,
                    ).first()
                    if not owned:
                        raise FilesFacadeError("Gallery resource is unavailable", code="resource_unavailable")
                    query_obj = query_obj.filter(GalleryImage.album_id == album_id)
                if query:
                    term = f"%{query}%"
                    query_obj = query_obj.filter(or_(
                        GalleryImage.filename.ilike(term),
                        GalleryImage.prompt.ilike(term),
                        GalleryImage.caption.ilike(term),
                        GalleryImage.tags.ilike(term),
                    ))
                total, latest = query_obj.with_entities(
                    func.count(GalleryImage.id), func.max(GalleryImage.updated_at)
                ).one()
                current_snapshot = _snapshot([parent_origin_id, total, latest])
                _check_snapshot(snapshot, current_snapshot)
                if sort["key"] == "kind":
                    # MIME/kind is normalized from the stored filename. SQL
                    # has no portable MIME collation, so select metadata only,
                    # sort it honestly, and then apply the bound offset.
                    all_entries = _sort_resources(
                        tuple(self._image(row, parent_origin_id) for row in query_obj.all()),
                        sort,
                    )
                    offset = _offset(cursor)
                    entries = all_entries[offset:offset + limit]
                else:
                    field = {
                        "name": func.lower(GalleryImage.filename),
                        "size": GalleryImage.file_size,
                        "modified": GalleryImage.updated_at,
                    }[sort["key"]]
                    order = field.desc() if sort["direction"] == "desc" else field.asc()
                    rows = query_obj.order_by(order, GalleryImage.id.asc()).offset(_offset(cursor)).limit(limit).all()
                    entries = tuple(self._image(row, parent_origin_id) for row in rows)
            elif parent_origin_id == "albums":
                query_obj = db.query(
                    GalleryAlbum.id,
                    GalleryAlbum.name,
                    GalleryAlbum.description,
                    GalleryAlbum.updated_at,
                    GalleryAlbum.created_at,
                ).filter(GalleryAlbum.owner == context.owner_username)
                if query:
                    query_obj = query_obj.filter(GalleryAlbum.name.ilike(f"%{query}%"))
                total, latest = query_obj.with_entities(
                    func.count(GalleryAlbum.id), func.max(GalleryAlbum.updated_at)
                ).one()
                current_snapshot = _snapshot([parent_origin_id, total, latest])
                _check_snapshot(snapshot, current_snapshot)
                field = {
                    "name": func.lower(GalleryAlbum.name),
                    "kind": literal("album"),
                    "size": literal(0),
                    "modified": GalleryAlbum.updated_at,
                }[sort["key"]]
                order = field.desc() if sort["direction"] == "desc" else field.asc()
                rows = query_obj.order_by(order, GalleryAlbum.id.asc()).offset(_offset(cursor)).limit(limit).all()
                entries = tuple(ProviderResource(
                    f"album:{row.id}",
                    row.name,
                    "album",
                    ("children", "stat", "open"),
                    parent_origin_id="albums",
                    modified_unix_ms=_millis(row.updated_at),
                    created_unix_ms=_millis(row.created_at),
                    provenance={"domain": "gallery", "description": row.description or ""},
                    open_target={"app": "gallery"},
                    child_sort_keys=self._MEDIA_SORTS,
                ) for row in rows)
            elif parent_origin_id == "drafts":
                query_obj = db.query(
                    EditorDraft.id,
                    EditorDraft.name,
                    EditorDraft.updated_at,
                    EditorDraft.created_at,
                ).filter(
                    EditorDraft.owner == context.owner_username,
                    EditorDraft.is_active == True,
                )
                if query:
                    query_obj = query_obj.filter(EditorDraft.name.ilike(f"%{query}%"))
                total, latest = query_obj.with_entities(
                    func.count(EditorDraft.id), func.max(EditorDraft.updated_at)
                ).one()
                current_snapshot = _snapshot([parent_origin_id, total, latest])
                _check_snapshot(snapshot, current_snapshot)
                field = {
                    "name": func.lower(EditorDraft.name),
                    "kind": literal("document"),
                    "size": literal(0),
                    "modified": EditorDraft.updated_at,
                }[sort["key"]]
                order = field.desc() if sort["direction"] == "desc" else field.asc()
                rows = query_obj.order_by(order, EditorDraft.id.asc()).offset(_offset(cursor)).limit(limit).all()
                entries = tuple(ProviderResource(
                    f"draft:{row.id}",
                    row.name or "Untitled",
                    "document",
                    ("stat", "open"),
                    parent_origin_id="drafts",
                    modified_unix_ms=_millis(row.updated_at),
                    created_unix_ms=_millis(row.created_at),
                    provenance={"domain": "gallery", "draft": True},
                    open_target={"app": "gallery"},
                ) for row in rows)
            else:
                raise FilesFacadeError("Gallery resource is unavailable", code="resource_unavailable")
            offset = _offset(cursor)
            next_cursor = str(offset + len(entries)) if offset + len(entries) < int(total or 0) else None
            return ProviderPage(entries, next_cursor=next_cursor, total=int(total or 0), snapshot=current_snapshot)
        finally:
            db.close()

    @staticmethod
    def _image(row: GalleryImage, parent: str) -> ProviderResource:
        stored_name = _safe_gallery_storage_name(row.filename)
        display_name = _download_name(row.filename)
        mime_type = mimetypes.guess_type(display_name)[0]
        preview_kind = _preview_kind_for_mime(mime_type)
        capabilities = ["stat", "open", "favorite"]
        if stored_name is not None:
            capabilities.append("download")
        if stored_name is not None and preview_kind in {"image", "audio"}:
            capabilities.append("preview")
        return ProviderResource(
            f"image:{row.id}",
            display_name,
            "image" if preview_kind == "image" else "file",
            tuple(capabilities),
            parent_origin_id=parent,
            mime_type=mime_type,
            size=row.file_size,
            modified_unix_ms=_millis(row.updated_at),
            created_unix_ms=_millis(row.created_at),
            provenance={"domain": "gallery", "favorite": bool(row.favorite), "album_id": row.album_id},
            open_target={"app": "gallery"},
            download_name=display_name if stored_name is not None else None,
            preview_kind=preview_kind if stored_name is not None else None,
            sort_kind=preview_kind or mime_type or ("image" if preview_kind == "image" else "file"),
        )

    async def stat(self, context: ProviderContext, *, origin_id: str) -> ProviderResource:
        if origin_id == "root":
            return (await self.roots(context))[0]
        if origin_id in {"photos", "favorites", "albums", "drafts"}:
            return self._folder(origin_id, origin_id.replace("-", " ").title())
        return await asyncio.to_thread(self._stat_sync, context, origin_id)

    def _stat_sync(self, context: ProviderContext, origin_id: str) -> ProviderResource:
        db = self.session_factory()
        try:
            if origin_id.startswith("image:"):
                row = db.query(
                    GalleryImage.id,
                    GalleryImage.filename,
                    GalleryImage.favorite,
                    GalleryImage.album_id,
                    GalleryImage.file_size,
                    GalleryImage.updated_at,
                    GalleryImage.created_at,
                ).filter(
                    GalleryImage.id == origin_id.split(":", 1)[1],
                    GalleryImage.owner == context.owner_username,
                    GalleryImage.is_active == True,
                ).first()
                if row:
                    return self._image(row, "photos")
            if origin_id.startswith("album:"):
                row = db.query(
                    GalleryAlbum.id,
                    GalleryAlbum.name,
                    GalleryAlbum.description,
                    GalleryAlbum.updated_at,
                    GalleryAlbum.created_at,
                ).filter(
                    GalleryAlbum.id == origin_id.split(":", 1)[1],
                    GalleryAlbum.owner == context.owner_username,
                ).first()
                if row:
                    return ProviderResource(
                        origin_id,
                        row.name,
                        "album",
                        ("children", "stat", "open"),
                        parent_origin_id="albums",
                        modified_unix_ms=_millis(row.updated_at),
                        created_unix_ms=_millis(row.created_at),
                        provenance={"domain": "gallery", "description": row.description or ""},
                        open_target={"app": "gallery"},
                        child_sort_keys=self._MEDIA_SORTS,
                    )
            if origin_id.startswith("draft:"):
                row = db.query(
                    EditorDraft.id,
                    EditorDraft.name,
                    EditorDraft.updated_at,
                    EditorDraft.created_at,
                ).filter(
                    EditorDraft.id == origin_id.split(":", 1)[1],
                    EditorDraft.owner == context.owner_username,
                    EditorDraft.is_active == True,
                ).first()
                if row:
                    return ProviderResource(
                        origin_id,
                        row.name or "Untitled",
                        "document",
                        ("stat", "open"),
                        parent_origin_id="drafts",
                        modified_unix_ms=_millis(row.updated_at),
                        created_unix_ms=_millis(row.created_at),
                        provenance={"domain": "gallery", "draft": True},
                        open_target={"app": "gallery"},
                    )
            raise FilesFacadeError("Gallery resource is unavailable", code="resource_unavailable")
        finally:
            db.close()

    async def content(self, context: ProviderContext, *, origin_id: str) -> ProviderContent:
        return await asyncio.to_thread(self._content_sync, context, origin_id)

    async def open_resource(self, context: ProviderContext, *, origin_id: str) -> Mapping[str, Any] | None:
        if not origin_id.startswith("image:"):
            return None
        return await asyncio.to_thread(self._open_image_sync, context, origin_id)

    async def action(
        self,
        context: ProviderContext,
        *,
        origin_id: str,
        action: str,
        args: Mapping[str, Any],
    ) -> ProviderResource:
        if action != "favorite.set" or not origin_id.startswith("image:"):
            raise FilesFacadeError("Gallery action is unavailable", code="resource_unavailable")
        return await asyncio.to_thread(
            self._set_favorite_sync,
            context,
            origin_id.split(":", 1)[1],
            bool(args["value"]),
        )

    def _set_favorite_sync(
        self,
        context: ProviderContext,
        image_id: str,
        value: bool,
    ) -> ProviderResource:
        db = self.session_factory()
        try:
            row = db.query(GalleryImage).filter(
                GalleryImage.id == image_id,
                GalleryImage.owner == context.owner_username,
                GalleryImage.is_active == True,
            ).first()
            if row is None:
                raise FilesFacadeError("Gallery resource is unavailable", code="resource_unavailable")
            row.favorite = bool(value)
            db.commit()
            db.refresh(row)
            return self._image(row, "favorites" if value else "photos")
        except FilesFacadeError:
            db.rollback()
            raise
        except Exception as exc:
            db.rollback()
            raise FilesFacadeError("Gallery action failed", code="provider_unavailable") from exc
        finally:
            db.close()

    def _content_sync(self, context: ProviderContext, origin_id: str) -> ProviderContent:
        if not origin_id.startswith("image:"):
            raise FilesFacadeError("Gallery resource is unavailable", code="resource_unavailable")
        db = self.session_factory()
        try:
            row = db.query(
                GalleryImage.id,
                GalleryImage.filename,
            ).filter(
                GalleryImage.id == origin_id.split(":", 1)[1],
                GalleryImage.owner == context.owner_username,
                GalleryImage.is_active == True,
            ).first()
        finally:
            db.close()
        if not row:
            raise FilesFacadeError("Gallery resource is unavailable", code="resource_unavailable")
        stored_name = _safe_gallery_storage_name(row.filename)
        if stored_name is None:
            raise FilesFacadeError("Gallery resource is unavailable", code="resource_unavailable")
        try:
            path = self.image_resolver(stored_name)
        except Exception as exc:
            raise FilesFacadeError("Gallery resource is unavailable", code="resource_unavailable") from exc
        media_type = mimetypes.guess_type(stored_name)[0] or "application/octet-stream"
        return _path_content(
            origin_id,
            filename=_download_name(stored_name),
            media_type=media_type,
            path=path,
        )

    def _open_image_sync(self, context: ProviderContext, origin_id: str) -> Mapping[str, Any]:
        image_id = origin_id.split(":", 1)[1]
        db = self.session_factory()
        try:
            row = db.query(GalleryImage).filter(
                GalleryImage.id == image_id,
                GalleryImage.owner == context.owner_username,
                GalleryImage.is_active == True,
            ).first()
            if row is None:
                raise FilesFacadeError("Gallery resource is unavailable", code="resource_unavailable")
            stored_name = _safe_gallery_storage_name(row.filename)
            media_type = mimetypes.guess_type(stored_name or "")[0]
            if stored_name is None or not str(media_type or "").startswith("image/"):
                raise FilesFacadeError("Gallery image preview is unavailable", code="resource_unavailable")
            # Verify that the same owner-scoped provider content which the
            # opaque response will load is present before returning metadata.
            self._content_sync(context, origin_id)
            camera = " ".join(part for part in (row.camera_make, row.camera_model) if part).strip()
            return {
                "filename": _download_name(row.filename),
                "prompt": str(row.prompt or ""),
                "caption": str(row.caption or ""),
                "model": str(row.model or ""),
                "size": str(row.size or ""),
                "quality": str(row.quality or ""),
                "tags": str(row.tags or ""),
                "ai_tags": str(row.ai_tags or ""),
                "favorite": bool(row.favorite),
                "taken_at": _millis(row.taken_at),
                "created_at": _millis(row.created_at),
                "updated_at": _millis(row.updated_at),
                "camera": camera,
                "width": int(row.width) if row.width is not None else None,
                "height": int(row.height) if row.height is not None else None,
                "file_size": int(row.file_size) if row.file_size is not None else None,
                "media_type": str(media_type),
                # Mutations in the legacy Gallery detail still require a raw
                # database ID. Exact opaque open is deliberately read-only
                # until those actions move behind the facade.
                "read_only": True,
            }
        finally:
            db.close()


class LibraryFilesProvider:
    """Metadata-only namespace over the existing Library domain owners.

    SQL-backed resources select only metadata columns. Research is still stored
    as one JSON document per report, so that branch reads the existing records
    but returns only normalized metadata; a future research index can replace
    that internal implementation without changing ResourceRefs.
    """

    name = "library"
    _ROOT_SORTS = ("name",)
    _DOCUMENT_SORTS = SORT_KEYS
    _PUBLISHED_SORTS = SORT_KEYS
    _CHAT_SORTS = ("name", "modified")
    _RESEARCH_SORTS = ("name", "modified", "size")

    def __init__(
        self,
        session_factory: Callable = SessionLocal,
        *,
        research_root: str | Path = DEEP_RESEARCH_DIR,
        published_service: PublishedFileService | None = None,
        chat_lifecycle: ChatLifecycleService | None = None,
    ) -> None:
        self.session_factory = session_factory
        self.research_root = Path(research_root)
        self.published_service = published_service or PublishedFileService(session_factory=session_factory)
        self.chat_lifecycle = chat_lifecycle

    def supported_sort_keys(self, *, parent_origin_id: str) -> tuple[str, ...]:
        if parent_origin_id in {"root", "archive"}:
            return self._ROOT_SORTS
        if parent_origin_id.startswith("documents:"):
            return self._DOCUMENT_SORTS
        if parent_origin_id == "published":
            return self._PUBLISHED_SORTS
        if parent_origin_id.startswith("chats:"):
            return self._CHAT_SORTS
        if parent_origin_id.startswith("research:"):
            return self._RESEARCH_SORTS
        raise FilesFacadeError("Library resource is unavailable", code="resource_unavailable")

    async def roots(self, context: ProviderContext):
        return [ProviderResource(
            "root",
            "Library",
            "provider_root",
            ("children", "stat", "search"),
            provenance={"domain": "library"},
            open_target={"app": "library"},
            child_sort_keys=self._ROOT_SORTS,
        )]

    @staticmethod
    def _folder(origin_id: str, name: str, *, parent: str = "root") -> ProviderResource:
        child_sort_keys = (
            LibraryFilesProvider._ROOT_SORTS if origin_id == "archive"
            else LibraryFilesProvider._DOCUMENT_SORTS if origin_id.startswith("documents:")
            else LibraryFilesProvider._PUBLISHED_SORTS if origin_id == "published"
            else LibraryFilesProvider._CHAT_SORTS if origin_id.startswith("chats:")
            else LibraryFilesProvider._RESEARCH_SORTS if origin_id.startswith("research:")
            else ()
        )
        return ProviderResource(
            origin_id,
            name,
            "virtual_folder",
            ("children", "stat", "search"),
            parent_origin_id=parent,
            provenance={"domain": "library", "view": origin_id},
            open_target={"app": "library"},
            child_sort_keys=child_sort_keys,
        )

    async def children(self, context: ProviderContext, **kwargs) -> ProviderPage:
        return await asyncio.to_thread(self._children_sync, context, **kwargs)

    async def search(
        self,
        context: ProviderContext,
        *,
        query: str,
        limit: int,
        sort: Mapping[str, Any],
    ) -> ProviderPage:
        pages = []
        for parent in (
            "documents:active",
            "published",
            "chats:active",
            "research:active",
            "documents:archived",
            "chats:archived",
            "research:archived",
        ):
            supported = self.supported_sort_keys(parent_origin_id=parent)
            pages.append(await self.children(
                context,
                parent_origin_id=parent,
                cursor=None,
                snapshot=None,
                limit=limit,
                sort=_search_sort(sort, supported),
                query=query,
            ))
        return _merge_search_pages(pages, limit=limit, sort=sort)

    def _children_sync(
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
        if parent_origin_id == "root":
            if cursor or snapshot:
                raise FilesFacadeError("provider cursor is stale", code="stale_cursor")
            rows = (
                self._folder("documents:active", "Documents"),
                self._folder("published", "Published Downloads"),
                self._folder("chats:active", "Chats"),
                self._folder("research:active", "Research"),
                self._folder("archive", "Archive"),
            )
            rows = _sort_resources(rows, sort)
            return ProviderPage(rows, total=len(rows), snapshot="library-folders-v1")
        if parent_origin_id == "archive":
            if cursor or snapshot:
                raise FilesFacadeError("provider cursor is stale", code="stale_cursor")
            rows = (
                self._folder("documents:archived", "Documents", parent="archive"),
                self._folder("chats:archived", "Chats", parent="archive"),
                self._folder("research:archived", "Research", parent="archive"),
            )
            rows = _sort_resources(rows, sort)
            return ProviderPage(rows, total=len(rows), snapshot="library-archive-folders-v1")
        if parent_origin_id.startswith("documents:"):
            return self._documents(
                context, parent_origin_id, cursor, snapshot, limit, sort, query,
            )
        if parent_origin_id == "published":
            return self._published(context, cursor, snapshot, limit, sort, query)
        if parent_origin_id.startswith("chats:"):
            return self._chats(
                context, parent_origin_id, cursor, snapshot, limit, sort, query,
            )
        if parent_origin_id.startswith("research:"):
            return self._research(
                context, parent_origin_id, cursor, snapshot, limit, sort, query,
            )
        raise FilesFacadeError("Library resource is unavailable", code="resource_unavailable")

    @staticmethod
    def _next_cursor(cursor: str | None, count: int, total: int) -> str | None:
        next_offset = _offset(cursor) + count
        return str(next_offset) if next_offset < total else None

    @staticmethod
    def _order(field: Any, sort: Mapping[str, Any], tie: Any):
        primary = field.desc() if sort["direction"] == "desc" else field.asc()
        return primary, tie.asc()

    def _documents(
        self,
        context: ProviderContext,
        parent: str,
        cursor: str | None,
        snapshot: str | None,
        limit: int,
        sort: Mapping[str, Any],
        query: str,
    ) -> ProviderPage:
        archived = parent.endswith(":archived")
        db = self.session_factory()
        try:
            content_bytes = func.length(cast(Document.current_content, LargeBinary))
            q = db.query(
                Document.id,
                Document.title,
                Document.language,
                Document.version_count,
                Document.archived,
                Document.created_at,
                Document.updated_at,
                content_bytes.label("content_size"),
            ).filter(
                Document.owner == context.owner_username,
                Document.is_active == True,
            )
            archive_filter = Document.archived == True if archived else or_(
                Document.archived == False,
                Document.archived.is_(None),
            )
            q = q.filter(archive_filter)
            if query:
                q = q.filter(Document.title.ilike(f"%{query}%"))
            total, latest = q.with_entities(func.count(Document.id), func.max(Document.updated_at)).one()
            current = _snapshot([parent, query, total, latest])
            _check_snapshot(snapshot, current)
            field = {
                "name": func.lower(Document.title),
                "kind": func.lower(Document.language),
                "size": content_bytes,
                "modified": Document.updated_at,
            }[sort["key"]]
            rows = q.order_by(*self._order(field, sort, Document.id)).offset(_offset(cursor)).limit(limit).all()
            entries = tuple(self._document_entry(row, parent, archived) for row in rows)
            return ProviderPage(entries, self._next_cursor(cursor, len(entries), int(total or 0)), int(total or 0), current)
        finally:
            db.close()

    @staticmethod
    def _document_entry(row: Any, parent: str, archived: bool) -> ProviderResource:
        language = str(row.language or "text").lower()
        return ProviderResource(
            f"document:{row.id}",
            row.title or "Untitled",
            "document",
            ("stat", "open", "preview", "download", "restore" if archived else "archive"),
            parent_origin_id=parent,
            mime_type="text/markdown" if language in {"markdown", "md", "pdf"} else "text/plain",
            size=int(row.content_size or 0),
            modified_unix_ms=_millis(row.updated_at),
            created_unix_ms=_millis(row.created_at),
            provenance={"domain": "documents", "language": row.language or "text", "versions": int(row.version_count or 0)},
            open_target={"app": "document_editor"},
            download_name=_document_download_name(row.title or "document", language),
            preview_kind="text",
            sort_kind=language,
        )

    def _published(
        self,
        context: ProviderContext,
        cursor: str | None,
        snapshot: str | None,
        limit: int,
        sort: Mapping[str, Any],
        query: str,
    ) -> ProviderPage:
        db = self.session_factory()
        try:
            q = db.query(
                PublishedFile.id,
                PublishedFile.filename,
                PublishedFile.mime_type,
                PublishedFile.size,
                PublishedFile.source,
                PublishedFile.created_at,
            ).filter(PublishedFile.owner == context.owner_username)
            if query:
                q = q.filter(PublishedFile.filename.ilike(f"%{query}%"))
            total, latest = q.with_entities(func.count(PublishedFile.id), func.max(PublishedFile.created_at)).one()
            current = _snapshot(["published", query, total, latest])
            _check_snapshot(snapshot, current)
            field = {
                "name": func.lower(PublishedFile.filename),
                "kind": func.lower(PublishedFile.mime_type),
                "size": PublishedFile.size,
                "modified": PublishedFile.created_at,
            }[sort["key"]]
            rows = q.order_by(*self._order(field, sort, PublishedFile.id)).offset(_offset(cursor)).limit(limit).all()
            entries = tuple(self._published_entry(row) for row in rows)
            return ProviderPage(entries, self._next_cursor(cursor, len(entries), int(total or 0)), int(total or 0), current)
        finally:
            db.close()

    @staticmethod
    def _published_entry(row: Any) -> ProviderResource:
        preview_kind = _preview_kind_for_mime(row.mime_type)
        capabilities = ["stat", "open", "download"]
        if preview_kind:
            capabilities.append("preview")
        return ProviderResource(
            f"published:{row.id}",
            row.filename,
            "file",
            tuple(capabilities),
            parent_origin_id="published",
            mime_type=row.mime_type,
            size=int(row.size or 0),
            modified_unix_ms=_millis(row.created_at),
            created_unix_ms=_millis(row.created_at),
            provenance={"domain": "published", "source": row.source},
            open_target={"app": "library"},
            download_name=row.filename,
            preview_kind=preview_kind,
            sort_kind=str(row.mime_type or "file").lower(),
        )

    def _chats(
        self,
        context: ProviderContext,
        parent: str,
        cursor: str | None,
        snapshot: str | None,
        limit: int,
        sort: Mapping[str, Any],
        query: str,
    ) -> ProviderPage:
        archived = parent.endswith(":archived")
        db = self.session_factory()
        try:
            q = db.query(
                DbSession.id,
                DbSession.name,
                DbSession.model,
                DbSession.message_count,
                DbSession.created_at,
                DbSession.updated_at,
                DbSession.last_message_at,
            ).filter(
                DbSession.owner == context.owner_username,
                DbSession.archived == archived,
            )
            if query:
                q = q.filter(DbSession.name.ilike(f"%{query}%"))
            total, latest = q.with_entities(func.count(DbSession.id), func.max(DbSession.updated_at)).one()
            current = _snapshot([parent, query, total, latest])
            _check_snapshot(snapshot, current)
            field = {
                "name": func.lower(DbSession.name),
                "kind": DbSession.model,
                "size": DbSession.message_count,
                "modified": DbSession.last_message_at,
            }[sort["key"]]
            rows = q.order_by(*self._order(field, sort, DbSession.id)).offset(_offset(cursor)).limit(limit).all()
            entries = tuple(self._chat_entry(row, parent, archived) for row in rows)
            return ProviderPage(entries, self._next_cursor(cursor, len(entries), int(total or 0)), int(total or 0), current)
        finally:
            db.close()

    def _chat_entry(self, row: Any, parent: str, archived: bool) -> ProviderResource:
        capabilities = ["stat", "open", "download"]
        if self.chat_lifecycle is not None:
            capabilities.append("restore" if archived else "archive")
        return ProviderResource(
            f"chat:{row.id}",
            row.name or "Untitled chat",
            "chat",
            tuple(capabilities),
            parent_origin_id=parent,
            modified_unix_ms=_millis(row.last_message_at or row.updated_at),
            created_unix_ms=_millis(row.created_at),
            provenance={"domain": "chats", "model": row.model or "", "message_count": int(row.message_count or 0)},
            open_target={"app": "chat"},
            download_name=row.name or "chat",
            sort_kind="chat",
        )

    def _research(
        self,
        context: ProviderContext,
        parent: str,
        cursor: str | None,
        snapshot: str | None,
        limit: int,
        sort: Mapping[str, Any],
        query: str,
    ) -> ProviderPage:
        archived = parent.endswith(":archived")
        root = self.research_root.resolve()
        records: list[dict[str, Any]] = []
        if root.is_dir():
            for path in root.glob("*.json"):
                try:
                    resolved = path.resolve()
                    resolved.relative_to(root)
                    stat = resolved.stat()
                    stored_bytes = resolved.read_bytes()
                    data = json.loads(stored_bytes.decode("utf-8"))
                    if data.get("owner") != context.owner_username or bool(data.get("archived")) != archived:
                        continue
                    title = str(data.get("query") or "Research")
                    if query and query.casefold() not in title.casefold():
                        continue
                    report_bytes = _research_report_bytes(data)
                    records.append({
                        "id": resolved.stem,
                        "name": title,
                        "created": _millis(data.get("started_at")) or 0,
                        "modified": _millis(data.get("completed_at") or data.get("started_at")) or 0,
                        "source_count": len(data.get("sources") or ()),
                        "content_size": len(report_bytes),
                        "category": str(data.get("category") or ""),
                        "file_size": stat.st_size,
                        "file_mtime_ns": stat.st_mtime_ns,
                        "file_sha256": hashlib.sha256(stored_bytes).hexdigest(),
                    })
                except (OSError, ValueError, TypeError, json.JSONDecodeError):
                    continue
        current = _snapshot([
            parent,
            query,
            sorted(
                (
                    row["id"],
                    row["file_size"],
                    row["file_mtime_ns"],
                    row["file_sha256"],
                    row["content_size"],
                )
                for row in records
            ),
        ])
        _check_snapshot(snapshot, current)
        sort_key = {
            "name": lambda row: row["name"].casefold(),
            "kind": lambda row: row["category"].casefold(),
            "size": lambda row: row["content_size"],
            "modified": lambda row: row["modified"],
        }[sort["key"]]
        # Keep the opaque origin as a stable ascending tie-breaker in both
        # directions. Reversing a compound tuple also reverses its tie, which
        # makes equal-size pages needlessly change order when direction flips.
        records.sort(key=lambda row: row["id"])
        records.sort(key=sort_key, reverse=sort["direction"] == "desc")
        offset = _offset(cursor)
        page = records[offset:offset + limit]
        entries = tuple(self._research_entry(row, parent, archived) for row in page)
        return ProviderPage(entries, self._next_cursor(cursor, len(entries), len(records)), len(records), current)

    @staticmethod
    def _research_entry(row: Mapping[str, Any], parent: str, archived: bool) -> ProviderResource:
        return ProviderResource(
            f"research:{row['id']}",
            str(row["name"]),
            "research",
            ("stat", "open", "preview", "download", "restore" if archived else "archive"),
            parent_origin_id=parent,
            size=int(row.get("content_size") or 0),
            modified_unix_ms=_millis(row["modified"]),
            created_unix_ms=_millis(row["created"]),
            provenance={"domain": "research", "category": row["category"], "source_count": int(row["source_count"])},
            open_target={"app": "research"},
            download_name=_download_name(row["name"], ".md"),
            preview_kind="text",
            sort_kind="research",
        )

    async def stat(self, context: ProviderContext, *, origin_id: str) -> ProviderResource:
        if origin_id == "root":
            return (await self.roots(context))[0]
        labels = {
            "documents:active": ("Documents", "root"),
            "published": ("Published Downloads", "root"),
            "chats:active": ("Chats", "root"),
            "research:active": ("Research", "root"),
            "archive": ("Archive", "root"),
            "documents:archived": ("Documents", "archive"),
            "chats:archived": ("Chats", "archive"),
            "research:archived": ("Research", "archive"),
        }
        if origin_id in labels:
            label, parent = labels[origin_id]
            return self._folder(origin_id, label, parent=parent)
        return await asyncio.to_thread(self._stat_sync, context, origin_id)

    def _stat_sync(self, context: ProviderContext, origin_id: str) -> ProviderResource:
        prefix, separator, item_id = origin_id.partition(":")
        if not separator or not item_id:
            raise FilesFacadeError("Library resource is unavailable", code="resource_unavailable")
        if prefix == "research":
            root = self.research_root.resolve()
            path = (root / f"{item_id}.json").resolve()
            try:
                path.relative_to(root)
                stat = path.stat()
                stored_bytes = path.read_bytes()
                data = json.loads(stored_bytes.decode("utf-8"))
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                raise FilesFacadeError("Library resource is unavailable", code="resource_unavailable") from None
            if data.get("owner") != context.owner_username:
                raise FilesFacadeError("Library resource is unavailable", code="resource_unavailable")
            archived = bool(data.get("archived"))
            report_bytes = _research_report_bytes(data)
            row = {
                "id": item_id,
                "name": str(data.get("query") or "Research"),
                "created": _millis(data.get("started_at")) or 0,
                "modified": _millis(data.get("completed_at") or data.get("started_at")) or 0,
                "source_count": len(data.get("sources") or ()),
                "content_size": len(report_bytes),
                "category": str(data.get("category") or ""),
                "file_size": stat.st_size,
                "file_mtime_ns": stat.st_mtime_ns,
                "file_sha256": hashlib.sha256(stored_bytes).hexdigest(),
            }
            return self._research_entry(row, f"research:{'archived' if archived else 'active'}", archived)

        db = self.session_factory()
        try:
            if prefix == "document":
                content_bytes = func.length(cast(Document.current_content, LargeBinary))
                row = db.query(
                    Document.id,
                    Document.title,
                    Document.language,
                    Document.version_count,
                    Document.archived,
                    Document.created_at,
                    Document.updated_at,
                    content_bytes.label("content_size"),
                ).filter(
                    Document.id == item_id,
                    Document.owner == context.owner_username,
                    Document.is_active == True,
                ).first()
                if row:
                    archived = bool(row.archived)
                    return self._document_entry(row, f"documents:{'archived' if archived else 'active'}", archived)
            elif prefix == "published":
                row = db.query(
                    PublishedFile.id,
                    PublishedFile.filename,
                    PublishedFile.mime_type,
                    PublishedFile.size,
                    PublishedFile.source,
                    PublishedFile.created_at,
                ).filter(
                    PublishedFile.id == item_id,
                    PublishedFile.owner == context.owner_username,
                ).first()
                if row:
                    return self._published_entry(row)
            elif prefix == "chat":
                row = db.query(
                    DbSession.id,
                    DbSession.name,
                    DbSession.model,
                    DbSession.message_count,
                    DbSession.archived,
                    DbSession.created_at,
                    DbSession.updated_at,
                    DbSession.last_message_at,
                ).filter(
                    DbSession.id == item_id,
                    DbSession.owner == context.owner_username,
                ).first()
                if row:
                    archived = bool(row.archived)
                    return self._chat_entry(row, f"chats:{'archived' if archived else 'active'}", archived)
        finally:
            db.close()
        raise FilesFacadeError("Library resource is unavailable", code="resource_unavailable")

    async def content(self, context: ProviderContext, *, origin_id: str) -> ProviderContent:
        return await asyncio.to_thread(self._content_sync, context, origin_id)

    async def open_resource(self, context: ProviderContext, *, origin_id: str) -> Mapping[str, Any] | None:
        if origin_id.startswith("document:"):
            return await asyncio.to_thread(self._open_document_sync, context, origin_id)
        if origin_id.startswith("chat:"):
            return await asyncio.to_thread(self._open_chat_sync, context, origin_id)
        if origin_id.startswith("research:"):
            return await asyncio.to_thread(self._open_research_sync, context, origin_id)
        return None

    async def action(
        self,
        context: ProviderContext,
        *,
        origin_id: str,
        action: str,
        args: Mapping[str, Any],
    ) -> ProviderResource:
        if action != "archive.set":
            raise FilesFacadeError("Library action is unavailable", code="resource_unavailable")
        archived = bool(args["value"])
        if origin_id.startswith("document:"):
            return await asyncio.to_thread(
                self._set_document_archived_sync,
                context,
                origin_id.split(":", 1)[1],
                archived,
            )
        if origin_id.startswith("research:"):
            return await asyncio.to_thread(
                self._set_research_archived_sync,
                context,
                origin_id.split(":", 1)[1],
                archived,
            )
        if origin_id.startswith("chat:") and self.chat_lifecycle is not None:
            try:
                await self.chat_lifecycle.set_archived(
                    owner=context.owner_username,
                    session_id=origin_id.split(":", 1)[1],
                    archived=archived,
                )
            except ChatLifecycleError as exc:
                code = "resource_changed" if exc.code in {"active_run", "projection_busy"} else "resource_unavailable"
                raise FilesFacadeError(str(exc), code=code) from exc
            return await self.stat(context, origin_id=origin_id)
        raise FilesFacadeError("Library action is unavailable", code="resource_unavailable")

    def _set_document_archived_sync(
        self,
        context: ProviderContext,
        document_id: str,
        archived: bool,
    ) -> ProviderResource:
        db = self.session_factory()
        try:
            row = db.query(Document).filter(
                Document.id == document_id,
                Document.owner == context.owner_username,
                Document.is_active == True,
            ).first()
            if row is None:
                raise FilesFacadeError("Library resource is unavailable", code="resource_unavailable")
            row.archived = bool(archived)
            db.commit()
        except FilesFacadeError:
            db.rollback()
            raise
        except Exception as exc:
            db.rollback()
            raise FilesFacadeError("Library action failed", code="provider_unavailable") from exc
        finally:
            db.close()
        return self._stat_sync(context, f"document:{document_id}")

    def _set_research_archived_sync(
        self,
        context: ProviderContext,
        report_id: str,
        archived: bool,
    ) -> ProviderResource:
        if not re.fullmatch(r"[A-Za-z0-9-]{1,128}", report_id):
            raise FilesFacadeError("Library resource is unavailable", code="resource_unavailable")
        root = self.research_root.resolve()
        path = (root / f"{report_id}.json").resolve()
        try:
            path.relative_to(root)
            stored = path.read_bytes()
            data = json.loads(stored.decode("utf-8"))
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            raise FilesFacadeError("Library resource is unavailable", code="resource_unavailable") from None
        if data.get("owner") != context.owner_username:
            raise FilesFacadeError("Library resource is unavailable", code="resource_unavailable")
        data["archived"] = bool(archived)
        replacement = json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        try:
            atomic_write_bytes(
                str(path),
                replacement,
                expected_fingerprint=fingerprint_bytes(stored),
            )
        except AtomicWriteConflict as exc:
            raise FilesFacadeError("Library resource changed", code="resource_changed") from exc
        except OSError as exc:
            raise FilesFacadeError("Library action failed", code="provider_unavailable") from exc
        return self._stat_sync(context, f"research:{report_id}")

    def _open_document_sync(self, context: ProviderContext, origin_id: str) -> Mapping[str, Any]:
        item_id = origin_id.partition(":")[2]
        if not item_id:
            raise FilesFacadeError("Library resource is unavailable", code="resource_unavailable")
        db = self.session_factory()
        try:
            row = db.query(
                Document.title,
                Document.language,
                Document.current_content,
                Document.version_count,
                Document.session_id,
                Document.archived,
            ).filter(
                Document.id == item_id,
                Document.owner == context.owner_username,
                Document.is_active == True,
            ).first()
            if not row:
                raise FilesFacadeError("Library resource is unavailable", code="resource_unavailable")
            return {
                "title": str(row.title or "Untitled"),
                "language": str(row.language or "text"),
                "content": str(row.current_content or ""),
                "version": int(row.version_count or 1),
                "session_ref": None,
                "archived": bool(row.archived),
                # The exact Files adapter has no raw document id and mutations
                # have not moved behind ResourceRefs yet.  Claiming this is
                # editable would make the existing editor post the opaque ref
                # to a legacy id route, so this compatibility view is honest
                # and read-only until provider actions own those writes.
                "read_only": True,
            }
        finally:
            db.close()

    def _open_chat_sync(self, context: ProviderContext, origin_id: str) -> Mapping[str, Any]:
        item_id = origin_id.partition(":")[2]
        if not item_id:
            raise FilesFacadeError("Library resource is unavailable", code="resource_unavailable")
        db = self.session_factory()
        try:
            row = db.query(
                DbSession.name,
                DbSession.model,
                DbSession.archived,
                DbSession.message_count,
            ).filter(
                DbSession.id == item_id,
                DbSession.owner == context.owner_username,
            ).first()
            if not row:
                raise FilesFacadeError("Library resource is unavailable", code="resource_unavailable")

            # Exact-open is a bounded read-only compatibility view.  The full
            # transcript remains available through the opaque content stream;
            # never materialize an unbounded chat in this JSON control plane.
            latest = db.query(
                ChatMessage.role,
                ChatMessage.content,
                ChatMessage.timestamp,
            ).filter(ChatMessage.session_id == item_id).order_by(
                ChatMessage.timestamp.desc(), ChatMessage.id.desc(),
            ).limit(50).all()
            latest.reverse()
            remaining = 192_000
            messages: list[dict[str, Any]] = []
            clipped = False
            for message in latest:
                text = _message_text(message.content)
                if len(text) > 12_000:
                    text = text[:12_000]
                    clipped = True
                if len(text) > remaining:
                    text = text[:max(0, remaining)]
                    clipped = True
                remaining -= len(text)
                messages.append({
                    "role": str(message.role or "message")[:32],
                    "text": text,
                    "timestamp": _millis(message.timestamp),
                })
                if remaining <= 0:
                    break
            total = max(int(row.message_count or 0), len(latest))
            return {
                "title": str(row.name or "Untitled chat")[:500],
                "model": str(row.model or "")[:500],
                "archived": bool(row.archived),
                "message_count": total,
                "messages": messages,
                "truncated": clipped or total > len(messages),
                "read_only": True,
            }
        finally:
            db.close()

    def _open_research_sync(self, context: ProviderContext, origin_id: str) -> Mapping[str, Any]:
        item_id = origin_id.partition(":")[2]
        if not re.fullmatch(r"[A-Za-z0-9-]{1,128}", item_id):
            raise FilesFacadeError("Library resource is unavailable", code="resource_unavailable")
        root = self.research_root.resolve()
        path = (root / f"{item_id}.json").resolve()
        try:
            path.relative_to(root)
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            raise FilesFacadeError("Library resource is unavailable", code="resource_unavailable") from None
        if data.get("owner") != context.owner_username:
            raise FilesFacadeError("Library resource is unavailable", code="resource_unavailable")

        full_report = str(data.get("raw_report") or data.get("result") or "")
        report = full_report[:240_000]
        source_rows: list[dict[str, str]] = []
        raw_sources = data.get("sources") if isinstance(data.get("sources"), list) else []
        for source in raw_sources[:50]:
            if not isinstance(source, Mapping):
                continue
            source_rows.append({
                "title": str(source.get("title") or source.get("url") or "Source")[:1_000],
                "url": str(source.get("url") or "")[:4_096],
            })
        return {
            "title": str(data.get("query") or "Research")[:500],
            "category": str(data.get("category") or "")[:200],
            "archived": bool(data.get("archived")),
            "report": report,
            "sources": source_rows,
            "source_count": len(raw_sources),
            "truncated": len(report) < len(full_report) or len(source_rows) < len(raw_sources),
            "read_only": True,
        }

    def _content_sync(self, context: ProviderContext, origin_id: str) -> ProviderContent:
        prefix, separator, item_id = origin_id.partition(":")
        if not separator or not item_id:
            raise FilesFacadeError("Library resource is unavailable", code="resource_unavailable")
        if prefix == "published":
            info = self.published_service.get_owned(item_id, owner=context.owner_username)
            if not info:
                raise FilesFacadeError("Library resource is unavailable", code="resource_unavailable")
            return _path_content(
                origin_id,
                filename=_download_name(info.get("filename")),
                media_type=str(info.get("mime_type") or "application/octet-stream"),
                path=str(info["path"]),
                etag=str(info.get("sha256") or "") or None,
            )
        if prefix == "research":
            root = self.research_root.resolve()
            path = (root / f"{item_id}.json").resolve()
            try:
                path.relative_to(root)
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
                raise FilesFacadeError("Library resource is unavailable", code="resource_unavailable") from exc
            if data.get("owner") != context.owner_username:
                raise FilesFacadeError("Library resource is unavailable", code="resource_unavailable")
            serialized = _research_report_bytes(data)
            return _bytes_content(
                origin_id,
                filename=_download_name(data.get("query") or "research", ".md"),
                media_type="text/markdown; charset=utf-8",
                data=serialized,
                modified_unix_ms=_millis(data.get("completed_at") or data.get("started_at")),
            )

        db = self.session_factory()
        try:
            if prefix == "document":
                row = db.query(
                    Document.title,
                    Document.language,
                    Document.current_content,
                    Document.updated_at,
                ).filter(
                    Document.id == item_id,
                    Document.owner == context.owner_username,
                    Document.is_active == True,
                ).first()
                if not row:
                    raise FilesFacadeError("Library resource is unavailable", code="resource_unavailable")
                language = str(row.language or "text").lower()
                media_type = "text/markdown; charset=utf-8" if language in {"markdown", "md", "pdf"} else "text/plain; charset=utf-8"
                return _bytes_content(
                    origin_id,
                    filename=_document_download_name(row.title or "document", language),
                    media_type=media_type,
                    data=str(row.current_content or "").encode("utf-8"),
                    modified_unix_ms=_millis(row.updated_at),
                )
            if prefix == "chat":
                session = db.query(
                    DbSession.id,
                    DbSession.name,
                    DbSession.model,
                    DbSession.updated_at,
                ).filter(
                    DbSession.id == item_id,
                    DbSession.owner == context.owner_username,
                ).first()
                if not session:
                    raise FilesFacadeError("Library resource is unavailable", code="resource_unavailable")
                messages = db.query(
                    ChatMessage.role,
                    ChatMessage.content,
                ).filter(ChatMessage.session_id == item_id).order_by(
                    ChatMessage.timestamp.asc(), ChatMessage.id.asc(),
                ).yield_per(200)
                lines = [f"# Conversation: {session.name or 'Untitled chat'}", "", f"Model: {session.model or ''}", ""]
                for message in messages:
                    lines.extend((
                        f"## {str(message.role or 'message').upper()}",
                        "",
                        _message_text(message.content),
                        "",
                    ))
                return _bytes_content(
                    origin_id,
                    filename=_download_name(session.name or "chat", ".md"),
                    media_type="text/markdown; charset=utf-8",
                    data="\n".join(lines).encode("utf-8"),
                    modified_unix_ms=_millis(session.updated_at),
                )
        finally:
            db.close()
        raise FilesFacadeError("Library resource is unavailable", code="resource_unavailable")


__all__ = ["CopalFilesProvider", "GalleryFilesProvider", "LibraryFilesProvider"]
