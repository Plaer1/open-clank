"""Versioned provider-neutral API for the unified Files namespace."""

from __future__ import annotations

import asyncio
import email.utils
import json
import os
import re
import stat
import time
import unicodedata
from dataclasses import replace
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Literal, Mapping
from urllib.parse import quote

from fastapi import APIRouter, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, StrictInt, model_validator

from core.database import SessionLocal
from src.auth_helpers import get_current_user
from src.openclank.file_policy import FilePolicyError, FilePolicyRepository
from src.openclank.chat_lifecycle import ChatLifecycleService
from src.openclank.files_facade import FilesFacade, FilesFacadeError, ProviderContext
from src.openclank.files_host_provider import HostFilesProvider
from src.openclank.files_managed_providers import (
    CopalFilesProvider,
    GalleryFilesProvider,
    LibraryFilesProvider,
)
from src.openclank.files_service_client import client_for_owner, close_all_clients
from src.openclank.filesystem_registry import FilesystemRootRegistry
from src.openclank.resource_refs import resolve_resource_ref
from src.openclank.copal_treehouse_repository import TreeHouseRepository
from src.openclank.media_attachment_targets import (
    HostDocumentAttachmentTarget,
    adopt_loose_media_for_workspace,
)
from src.openclank.history_capture import trusted_tool_context
from src.openclank.treehouse_files_adapter import TreeHouseLessonAttachmentTarget
from src.constants import DATA_DIR
from src.openclank.workspace_policy_service import (
    WorkspacePolicyServiceError,
    bind_workspace_path,
    resolve_workspace_binding,
    workspace_view,
)


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ChildrenRequest(_StrictModel):
    parent_ref: str = Field(min_length=8, max_length=16_384)
    cursor: str | None = Field(default=None, max_length=16_384)
    limit: StrictInt = Field(default=100, ge=1, le=200)
    sort: dict[str, Any] = Field(default_factory=dict)
    query: str = Field(default="", max_length=512)


class StatRequest(_StrictModel):
    resource_ref: str = Field(min_length=8, max_length=16_384)


class SearchRequest(_StrictModel):
    query: str = Field(min_length=1, max_length=512)
    limit: StrictInt = Field(default=100, ge=1, le=200)
    sort: dict[str, Any] = Field(default_factory=dict)


class ActionRequest(_StrictModel):
    resource_ref: str = Field(min_length=8, max_length=16_384)
    action: Literal["open", "favorite.set", "archive.set", "rename", "move", "trash", "restore"]
    action_id: str | None = Field(default=None, min_length=1, max_length=128)
    args: dict[str, Any] = Field(default_factory=dict, max_length=2)


class OpenResourceRequest(_StrictModel):
    resource_ref: str = Field(min_length=8, max_length=16_384)


class HostOpenRequest(_StrictModel):
    resource_ref: str = Field(min_length=8, max_length=16_384)
    app_id: str = Field(min_length=1, max_length=512)


class HostRevisionRequest(_StrictModel):
    kind: Literal["hostFingerprint"]
    value: str = Field(min_length=1, max_length=512)


class SaveResourceRequest(_StrictModel):
    resource_ref: str = Field(min_length=8, max_length=16_384)
    expected_revision: HostRevisionRequest
    text: str = Field(max_length=8 * 1024 * 1024)


class CreateResourceRequest(_StrictModel):
    parent_ref: str = Field(min_length=8, max_length=16_384)
    name: str = Field(min_length=1, max_length=240)
    text: str = Field(max_length=8 * 1024 * 1024)
    action_id: str | None = Field(default=None, min_length=1, max_length=128)


class CreateDirectoryRequest(_StrictModel):
    parent_ref: str = Field(min_length=8, max_length=16_384)
    name: str = Field(min_length=1, max_length=240)
    operation_id: str = Field(min_length=1, max_length=128)
    generation: StrictInt = Field(ge=0)
    expected_revision: HostRevisionRequest | None = None
    collision: Literal["fail", "reuse"] = "fail"


class WorkspaceResourceRequest(_StrictModel):
    resource_ref: str = Field(min_length=8, max_length=16_384)
    purpose: Literal["app_folder", "agent_workspace"]


class WorkspaceUpdateRequest(_StrictModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    archived: bool | None = None
    expected_revision: StrictInt | None = Field(default=None, ge=1)


class WorkspacePathResourceRequest(_StrictModel):
    workspace_id: str = Field(min_length=1, max_length=256)
    relative_path: str = Field(default="", max_length=4096)


class PlaceRequest(_StrictModel):
    resource_ref: str = Field(min_length=8, max_length=16_384)


class SavedSearchRequest(_StrictModel):
    name: str = Field(min_length=1, max_length=200)
    provider: Literal["all", "host", "copal", "gallery", "library"] = "all"
    query: str = Field(min_length=1, max_length=512)
    sort: dict[str, Any] = Field(default_factory=dict)


class WatchResourceRequest(_StrictModel):
    resource_ref: str = Field(min_length=8, max_length=16_384)


class RevisionRequest(_StrictModel):
    kind: str = Field(min_length=1, max_length=128)
    value: str = Field(min_length=1, max_length=512)


class TransferSource(_StrictModel):
    item_id: str = Field(min_length=1, max_length=128)
    resource_ref: str = Field(min_length=8, max_length=16_384)
    expected_revision: RevisionRequest | None = None


class AttachmentRevision(_StrictModel):
    kind: str = Field(min_length=1, max_length=128)
    value: str = Field(min_length=1, max_length=512)


class AttachmentSourceRequest(_StrictModel):
    resource_ref: str | None = Field(default=None, min_length=8, max_length=16_384)
    expected_revision: AttachmentRevision | None = None
    import_receipt_id: str | None = Field(default=None, min_length=1, max_length=128)
    item_id: str | None = Field(default=None, min_length=1, max_length=128)

    @model_validator(mode="after")
    def validate_variant(self):
        has_ref = self.resource_ref is not None
        has_import = self.import_receipt_id is not None or self.item_id is not None
        if has_ref == has_import or (has_import and (self.import_receipt_id is None or self.item_id is None)):
            raise ValueError("attachment source must have exactly one complete variant")
        return self


class AttachmentTargetRequest(_StrictModel):
    kind: Literal["copal_document", "host_document", "treehouse_lesson"]
    resource_ref: str | None = Field(default=None, min_length=8, max_length=16_384)
    course_id: str | None = Field(default=None, min_length=1, max_length=256)
    lesson_id: str | None = Field(default=None, min_length=1, max_length=256)
    expected_revision: AttachmentRevision | None = None

    @model_validator(mode="after")
    def validate_target(self):
        if self.kind in {"copal_document", "host_document"} and (self.resource_ref is None or self.course_id is not None or self.lesson_id is not None):
            raise ValueError(f"{self.kind} attachment target requires a resource ref")
        if self.kind == "treehouse_lesson" and (self.course_id is None or self.lesson_id is None or self.resource_ref is not None):
            raise ValueError("TreeHouse attachment target requires course and lesson IDs")
        return self


class TransferRequest(_StrictModel):
    operation_id: str = Field(min_length=1, max_length=128)
    generation: StrictInt = Field(ge=0)
    kind: Literal["move", "copy"]
    sources: list[TransferSource] = Field(min_length=1, max_length=200)
    destination_ref: str = Field(min_length=8, max_length=16_384)
    collision: Literal["fail", "rename"] = "fail"


class ResourceKeyRequest(_StrictModel):
    provider: Literal["copal"]
    resource_id: str = Field(min_length=1, max_length=256)
    account_id: str | None = Field(default=None, min_length=1, max_length=256)
    workspace_id: str | None = Field(default=None, min_length=1, max_length=64)


class ResolveResourceRequest(_StrictModel):
    resource_key: ResourceKeyRequest


class AttachmentPrepareRequest(_StrictModel):
    operation_id: str = Field(min_length=1, max_length=128)
    generation: StrictInt = Field(ge=0)
    source: AttachmentSourceRequest
    target: AttachmentTargetRequest
    mode: Literal["link", "embed"]


class AttachmentReceiptResponse(_StrictModel):
    operation_id: str = Field(min_length=1, max_length=128)
    generation: int = Field(ge=0)
    state: Literal["pending", "complete", "failed", "stale"]
    preparation: dict[str, Any] | None = None


class BaseQueryRequest(_StrictModel):
    base_ref: str = Field(min_length=8, max_length=16_384)
    expected_revision: RevisionRequest | None = None
    corpus_ref: str = Field(min_length=8, max_length=16_384)
    generation: StrictInt = Field(ge=0)
    view_id: str | None = Field(default=None, max_length=256)
    query: dict[str, Any] = Field(default_factory=dict)
    page: StrictInt = Field(default=0, ge=0)
    page_size: StrictInt = Field(default=100, ge=1, le=500)
    context_ref: str | None = Field(default=None, max_length=16_384)
    draft_definition: str | None = Field(default=None, max_length=256 * 1024)

    @model_validator(mode="after")
    def validate_query_bound(self):
        try:
            encoded = json.dumps(self.query, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        except (TypeError, ValueError):
            raise ValueError("Base query is invalid") from None
        if len(encoded) > 256 * 1024:
            raise ValueError("Base query is too large")
        return self


_ACTIVE_DOWNLOAD_TYPES = {
    "text/html",
    "application/xhtml+xml",
    "image/svg+xml",
    "application/xml",
    "text/xml",
    "application/pdf",
}
_RANGE_RE = re.compile(r"^bytes=(\d*)-(\d*)$")


def _download_filename(value: str) -> tuple[str, str]:
    raw = unicodedata.normalize("NFC", Path(str(value or "resource").replace("\\", "/")).name)
    raw = "".join(character for character in raw if ord(character) >= 32 and character not in {'\x7f', '"', ';', '/', '\\'})
    raw = (raw.strip() or "resource")[:240]
    ascii_value = unicodedata.normalize("NFKD", raw).encode("ascii", "ignore").decode("ascii")
    ascii_value = "".join(character for character in ascii_value if character.isalnum() or character in {".", "_", "-", " ", "(", ")"})
    ascii_value = (ascii_value.strip(" .") or "resource")[:180]
    return ascii_value, quote(raw, safe="")


def _range(value: str | None, size: int) -> tuple[int, int] | None:
    if not value:
        return None
    match = _RANGE_RE.fullmatch(value.strip())
    if not match or size <= 0:
        raise FilesFacadeError("requested range is unavailable", code="invalid_range")
    start_raw, end_raw = match.groups()
    if not start_raw and not end_raw:
        raise FilesFacadeError("requested range is unavailable", code="invalid_range")
    if not start_raw:
        suffix = int(end_raw)
        if suffix <= 0:
            raise FilesFacadeError("requested range is unavailable", code="invalid_range")
        start = max(0, size - suffix)
        end = size - 1
    else:
        start = int(start_raw)
        end = int(end_raw) if end_raw else size - 1
        if start >= size or end < start:
            raise FilesFacadeError("requested range is unavailable", code="invalid_range")
        end = min(end, size - 1)
    return start, end


def _open_path_content(content) -> tuple[int, os.stat_result]:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(content.path, flags)
        current = os.fstat(descriptor)
    except OSError as exc:
        try:
            os.close(descriptor)
        except (NameError, OSError):
            pass
        raise FilesFacadeError("resource content is unavailable", code="resource_unavailable") from exc
    identity = (int(current.st_dev), int(current.st_ino), int(current.st_size), int(current.st_mtime_ns))
    if not stat.S_ISREG(current.st_mode) or (content.expected_identity and identity != content.expected_identity):
        os.close(descriptor)
        raise FilesFacadeError("resource changed before it could be opened", code="resource_changed")
    return descriptor, current


def setup_files_facade_routes(
    *,
    policy_repository: FilePolicyRepository | None = None,
    session_factory: Callable = SessionLocal,
    filesystem_registry: FilesystemRootRegistry | None = None,
    host_client_factory: Callable[..., Any] = client_for_owner,
) -> APIRouter:
    router = APIRouter(prefix="/api/files-v1", tags=["files-v1"])
    repository = policy_repository or FilePolicyRepository()
    host_registry = filesystem_registry or FilesystemRootRegistry()

    def context(request: Request, *, workspace: str = "default") -> ProviderContext:
        username = str(get_current_user(request) or "").strip().lower()
        # AUTH_ENABLED=false is the documented single-user/local mode.  It
        # has no cookie session, but Files still needs a stable principal so
        # opaque refs remain owner-bound across requests.
        local_mode = os.getenv("AUTH_ENABLED", "true").lower() == "false"
        if not username and local_mode:
            username = "local-installation"
        if not username:
            raise HTTPException(401, "Authentication required")
        auth_manager = getattr(getattr(request.app, "state", None), "auth_manager", None)
        account_id = auth_manager.account_id(username) if auth_manager and hasattr(auth_manager, "account_id") else None
        if not account_id and local_mode and username == "local-installation":
            account_id = "local-installation"
        if not account_id:
            # Never fall back to the mutable username; that would let account
            # deletion/recreation inherit old ResourceRefs.
            raise HTTPException(503, "Immutable account identity is unavailable")
        is_admin = bool(auth_manager and auth_manager.is_admin(username))
        return ProviderContext(
            owner_subject_id=str(account_id),
            owner_username=username,
            policy_generation=repository.generation(),
            is_admin=is_admin,
            workspace_id=str(workspace or "default"),
        )

    def context_for_ref(request: Request, token: str) -> ProviderContext:
        """Carry a sealed Copal workspace into navigation requests.

        The roots endpoint accepts an explicit Copal workspace hint, while
        subsequent browser navigation submits only the opaque root ref. Use
        the already owner/generation-validated ref to select that same scope
        before the facade enforces its workspace binding.
        """
        current = context(request)
        try:
            ref = resolve_resource_ref(token, expected_owner_subject_id=current.owner_subject_id, current_policy_generation=current.policy_generation)
            workspace = ref.workspace_id or current.workspace_id
        except Exception:
            workspace = current.workspace_id
        return replace(current, workspace_id=str(workspace or "default"))

    def facade(request: Request, *, copal_workspace: str = "default") -> FilesFacade:
        app_state = getattr(request.app, "state", None)
        bridge = getattr(app_state, "copal_bridge", None)
        chat_lifecycle = ChatLifecycleService(
            session_factory=session_factory,
            session_manager=getattr(app_state, "session_manager", None),
            mimo_supervisor=getattr(app_state, "mimo_supervisor", None),
        )
        host_provider = HostFilesProvider(registry=host_registry, client_factory=host_client_factory, operation_store=repository)
        providers = [
            host_provider,
            GalleryFilesProvider(session_factory),
            LibraryFilesProvider(session_factory, chat_lifecycle=chat_lifecycle),
        ]
        if bridge is not None:
            providers.insert(0, CopalFilesProvider(bridge, workspace_id=copal_workspace, operation_store=repository))
        treehouse_repository = getattr(app_state, "treehouse_repository", None)
        if treehouse_repository is None:
            configured = os.environ.get("TREEHOUSE_REPOSITORY_PATH")
            treehouse_repository = TreeHouseRepository(Path(configured) if configured else Path(DATA_DIR) / "treehouse.sqlite3")
            app_state.treehouse_repository = treehouse_repository
        return FilesFacade(
            providers,
            place_repository=repository,
            operation_store=repository,
            attachment_targets={
                "host_document": HostDocumentAttachmentTarget(provider=host_provider, operation_store=repository),
                "treehouse_lesson": TreeHouseLessonAttachmentTarget(treehouse_repository),
            },
        )

    def _raise(error: FilesFacadeError) -> None:
        if error.code in {"resource_unavailable", "invalid_resource_ref"}:
            status = 404
        elif error.code in {"resource_ref_stale", "stale_cursor", "resource_changed", "policy_generation_changed", "idempotency_conflict", "operation_pending"}:
            status = 409
        elif error.code == "invalid_range":
            status = 416
        elif error.code == "provider_unavailable":
            status = 503
        else:
            status = 400
        # The safe code is stable; raw provider errors/origin IDs are not.
        raise HTTPException(status, detail={"code": error.code, "message": str(error)}) from error

    def _generation_guard(expected: int) -> Callable[[], bool]:
        """Throttled fail-closed policy-generation probe for in-flight streams.

        A scoped reset bumps the policy generation; streams that resolved their
        ref before the reset must not keep flowing afterwards. The probe is
        time-throttled so a large transfer costs at most a handful of SQLite
        reads, and any store error is treated as a change.
        """
        state = {"next_probe": 0.0}

        def intact() -> bool:
            now = time.monotonic()
            if now < state["next_probe"]:
                return True
            state["next_probe"] = now + 0.2
            try:
                return repository.generation() == expected
            except Exception:
                return False

        return intact

    def _policy_aborted() -> FilesFacadeError:
        return FilesFacadeError(
            "file policy changed during transfer; retry with a fresh reference",
            code="policy_generation_changed",
        )

    @router.get("/roots")
    async def roots(
        request: Request,
        copal_workspace: str = Query(default="default", min_length=1, max_length=64),
    ):
        try:
            return await facade(request, copal_workspace=copal_workspace).roots(context(request))
        except FilesFacadeError as error:
            _raise(error)

    @router.post("/children")
    async def children(body: ChildrenRequest, request: Request):
        try:
            return await facade(request).children(
                context_for_ref(request, body.parent_ref),
                parent_ref=body.parent_ref,
                cursor=body.cursor,
                limit=body.limit,
                sort=body.sort,
                query=body.query,
            )
        except FilesFacadeError as error:
            _raise(error)

    @router.post("/stat")
    async def stat(body: StatRequest, request: Request):
        try:
            return await facade(request).stat(context_for_ref(request, body.resource_ref), resource_ref=body.resource_ref)
        except FilesFacadeError as error:
            _raise(error)

    @router.post("/reveal")
    async def reveal(body: StatRequest, request: Request):
        try:
            return await facade(request).reveal(context_for_ref(request, body.resource_ref), resource_ref=body.resource_ref)
        except FilesFacadeError as error:
            _raise(error)

    @router.post("/reissue")
    async def reissue_exact(body: StatRequest, request: Request):
        """Reauthorize one stale managed exact-view identity.

        The submitted ref is still sealed and immutable-owner-bound, but its
        old expiry/generation grants no authority here. The facade re-stats the
        current provider record before minting a fresh ResourceRef.
        """

        try:
            return await facade(request).reissue_exact(
                context_for_ref(request, body.resource_ref),
                resource_ref=body.resource_ref,
            )
        except FilesFacadeError as error:
            _raise(error)

    @router.get("/places")
    async def places(request: Request):
        try:
            return await facade(request).places(context(request))
        except FilesFacadeError as error:
            _raise(error)

    @router.post("/places")
    async def save_place(body: PlaceRequest, request: Request):
        try:
            return await facade(request).save_place(context(request), resource_ref=body.resource_ref)
        except FilesFacadeError as error:
            _raise(error)

    @router.delete("/places/{place_id}")
    async def remove_place(place_id: str, request: Request):
        if not re.fullmatch(r"place-[0-9a-f]{32}", str(place_id or "")):
            raise HTTPException(404, "Files place is unavailable")
        try:
            return await facade(request).remove_place(context(request), place_id=place_id)
        except FilesFacadeError as error:
            _raise(error)

    @router.get("/recents")
    async def recents(request: Request):
        try:
            return await facade(request).recents(context(request))
        except FilesFacadeError as error:
            _raise(error)

    @router.delete("/recents")
    async def clear_recents(request: Request):
        try:
            return await facade(request).clear_recents(context(request))
        except FilesFacadeError as error:
            _raise(error)

    @router.get("/saved-searches")
    async def saved_searches(request: Request):
        try:
            return await facade(request).saved_searches(context(request))
        except FilesFacadeError as error:
            _raise(error)

    @router.post("/saved-searches")
    async def save_search(body: SavedSearchRequest, request: Request):
        try:
            return await facade(request).save_search(
                context(request),
                name=body.name,
                provider=body.provider,
                query=body.query,
                sort=body.sort,
            )
        except FilesFacadeError as error:
            _raise(error)

    @router.delete("/saved-searches/{search_id}")
    async def remove_saved_search(search_id: str, request: Request):
        if not re.fullmatch(r"search-[0-9a-f]{32}", str(search_id or "")):
            raise HTTPException(404, "Saved search is unavailable")
        try:
            return await facade(request).remove_saved_search(
                context(request),
                search_id=search_id,
            )
        except FilesFacadeError as error:
            _raise(error)

    @router.post("/search")
    async def search(
        body: SearchRequest,
        request: Request,
        copal_workspace: str = Query(default="default", min_length=1, max_length=64),
    ):
        try:
            return await facade(request, copal_workspace=copal_workspace).search(
                context(request),
                query=body.query,
                limit=body.limit,
                sort=body.sort,
            )
        except FilesFacadeError as error:
            _raise(error)

    @router.post("/resolve-resource")
    async def resolve_resource(body: ResolveResourceRequest, request: Request):
        try:
            workspace = body.resource_key.workspace_id or "default"
            return await facade(request, copal_workspace=workspace).resolve_resource(context(request), resource_key=body.resource_key.model_dump(exclude_none=True))
        except FilesFacadeError as error:
            _raise(error)

    @router.post("/transfers")
    async def transfer_resources(body: TransferRequest, request: Request):
        try:
            return await facade(request).transfer_resources(
                context(request), operation_id=body.operation_id, generation=body.generation,
                kind=body.kind, sources=[item.model_dump() for item in body.sources],
                destination_ref=body.destination_ref, collision=body.collision,
            )
        except FilesFacadeError as error:
            _raise(error)

    @router.get("/operations/{operation_id}")
    async def operation_receipt(operation_id: str, request: Request):
        try:
            return await facade(request).operation_receipt(context(request), operation_id=operation_id)
        except FilesFacadeError as error:
            _raise(error)

    @router.post("/imports")
    async def import_file(
        request: Request,
        file: UploadFile = File(...),
        metadata: str = Form(...),
    ):
        try:
            parsed = json.loads(metadata)
            if not isinstance(parsed, dict):
                raise FilesFacadeError("import metadata is invalid", code="invalid_resource_request")
            return await facade(request).import_file(context(request), upload=file, metadata=parsed)
        except json.JSONDecodeError as error:
            _raise(FilesFacadeError("import metadata is invalid", code="invalid_resource_request"))
        except FilesFacadeError as error:
            _raise(error)

    @router.post("/attachments/prepare")
    async def prepare_attachment(body: AttachmentPrepareRequest, request: Request, copal_workspace: str = Query(default="default", min_length=1, max_length=64)):
        try:
            return await facade(request, copal_workspace=copal_workspace).prepare_attachment(
                context(request, workspace=copal_workspace), operation_id=body.operation_id, generation=body.generation,
                source=body.source.model_dump(exclude_none=True), target=body.target.model_dump(exclude_none=True), mode=body.mode,
            )
        except FilesFacadeError as error:
            _raise(error)

    @router.get("/attachments/{operation_id}", response_model=AttachmentReceiptResponse)
    async def attachment_receipt(operation_id: str, request: Request, copal_workspace: str = Query(default="default", min_length=1, max_length=64)):
        try:
            return await facade(request, copal_workspace=copal_workspace).attachment_receipt(
                context(request, workspace=copal_workspace), operation_id=operation_id,
            )
        except FilesFacadeError as error:
            _raise(error)

    @router.post("/bases/query")
    async def query_base(body: BaseQueryRequest, request: Request):
        try:
            return await facade(request).query_base_resource(
                context(request), base_ref=body.base_ref, expected_revision=body.expected_revision.model_dump() if body.expected_revision else None,
                corpus_ref=body.corpus_ref, generation=body.generation, view_id=body.view_id,
                query=body.query, page=body.page, page_size=body.page_size,
                context_ref=body.context_ref, draft_definition=body.draft_definition,
            )
        except FilesFacadeError as error:
            _raise(error)

    @router.post("/action")
    async def action(body: ActionRequest, request: Request):
        try:
            if body.action == "open":
                if body.args:
                    raise HTTPException(422, "Open does not accept arguments")
            elif body.action in {"rename", "move"} and (set(body.args) != {"name"} or not isinstance(body.args.get("name"), str) or not body.args["name"].strip()):
                raise HTTPException(422, "Rename and move require one non-empty name")
            elif body.action in {"trash", "restore"} and body.args:
                raise HTTPException(422, "This action does not accept arguments")
            elif body.action in {"favorite.set", "archive.set"} and (set(body.args) != {"value"} or not isinstance(body.args.get("value"), bool)):
                raise HTTPException(422, "This action requires one boolean value")
            return await facade(request).action(
                context_for_ref(request, body.resource_ref),
                resource_ref=body.resource_ref,
                action=body.action,
                args=body.args,
                action_id=body.action_id,
            )
        except FilesFacadeError as error:
            _raise(error)

    @router.post("/open-resource")
    async def open_resource(body: OpenResourceRequest, request: Request):
        try:
            return await facade(request).open_payload(context_for_ref(request, body.resource_ref), resource_ref=body.resource_ref)
        except FilesFacadeError as error:
            _raise(error)

    @router.post("/host-apps")
    async def host_apps(body: OpenResourceRequest, request: Request):
        try:
            return await facade(request).host_applications(
                context_for_ref(request, body.resource_ref), resource_ref=body.resource_ref,
            )
        except FilesFacadeError as error:
            _raise(error)

    @router.post("/open-host")
    async def open_host(body: HostOpenRequest, request: Request):
        try:
            return await facade(request).open_on_host(
                context_for_ref(request, body.resource_ref), resource_ref=body.resource_ref, app_id=body.app_id,
            )
        except FilesFacadeError as error:
            _raise(error)

    @router.post("/save-resource")
    async def save_resource(body: SaveResourceRequest, request: Request):
        try:
            return await facade(request).save_resource(
                context_for_ref(request, body.resource_ref),
                resource_ref=body.resource_ref,
                expected_revision=body.expected_revision.model_dump(),
                text=body.text,
            )
        except FilesFacadeError as error:
            _raise(error)

    @router.post("/create")
    async def create_resource(body: CreateResourceRequest, request: Request):
        try:
            return await facade(request).create(
                context(request),
                parent_ref=body.parent_ref,
                name=body.name,
                text=body.text,
                action_id=body.action_id,
            )
        except FilesFacadeError as error:
            _raise(error)

    @router.post("/create-directory")
    async def create_directory(body: CreateDirectoryRequest, request: Request):
        try:
            return await facade(request).create_directory(
                context_for_ref(request, body.parent_ref),
                parent_ref=body.parent_ref,
                name=body.name,
                operation_id=body.operation_id,
                generation=body.generation,
                expected_revision=body.expected_revision.model_dump() if body.expected_revision else None,
                collision=body.collision,
            )
        except FilesFacadeError as error:
            _raise(error)

    @router.post("/workspace")
    async def workspace_from_resource(body: WorkspaceResourceRequest, request: Request):
        """Bind an opaque Host resource to the one canonical Workspace model."""

        owner = context(request)
        try:
            target = await facade(request).workspace_target(
                owner,
                resource_ref=body.resource_ref,
            )
            binding = bind_workspace_path(
                repository,
                actor_subject_id=owner.owner_subject_id,
                owner_subject_id=owner.owner_subject_id,
                is_admin=owner.is_admin,
                path=target.directory_path,
                purpose=body.purpose,
                name=target.name,
                media_adoption=lambda *, workspace_root, workspace_id, owner_subject_id: adopt_loose_media_for_workspace(
                    operation_store=repository,
                    owner_subject_id=owner_subject_id,
                    workspace_root=workspace_root,
                    workspace_id=workspace_id,
                    history_context=trusted_tool_context(
                        actor_id=owner_subject_id,
                        account_id=owner_subject_id,
                        workspace_id=workspace_id,
                        workspace_root=workspace_root,
                    ),
                ),
                media_receipt=getattr(getattr(request, "state", None), "history_capture", None),
            )
        except FilesFacadeError as error:
            _raise(error)
        except WorkspacePolicyServiceError as error:
            status = 403 if error.code == "workspace_denied" else 404 if error.code == "workspace_missing" else 400
            raise HTTPException(
                status,
                detail={"code": error.code, "message": str(error)},
            ) from error
        return {
            "version": 1,
            "generation": repository.generation(),
            # Deliberately omit the host path. Code/composer resolve this ID
            # through the existing purpose-bound Workspace endpoint.
            "workspace": workspace_view(binding, include_path=False),
            "open_relative": target.open_relative,
        }

    def workspace_public(workspace) -> dict[str, Any]:
        return {
            "id": workspace.id,
            "name": workspace.name,
            "location_id": workspace.location_id,
            "relative_folder": workspace.relative_folder,
            "archived": workspace.archived,
            "generation": workspace.generation,
            "revision": workspace.revision,
        }

    @router.get("/workspaces")
    async def workspaces(request: Request, include_archived: bool = False):
        owner = context(request)
        nonlocal_facade = facade(request)
        rows = []
        for workspace in repository.list_workspaces(
            owner_subject_id=owner.owner_subject_id,
            include_archived=bool(include_archived),
        ):
            if workspace.archived:
                resource, availability = None, "archived"
            else:
                try:
                    binding = resolve_workspace_binding(
                        repository,
                        workspace,
                        subject_id=owner.owner_subject_id,
                        is_admin=owner.is_admin,
                        purpose="app_folder",
                    )
                    resource = await nonlocal_facade.host_resource_for_path(owner, path=binding.path)
                    availability = "available"
                except (WorkspacePolicyServiceError, FilesFacadeError) as error:
                    resource = None
                    availability = getattr(error, "code", "workspace_unavailable")
            rows.append({
                "workspace": workspace_public(workspace),
                "availability": availability,
                "resource": resource,
            })
        return {"version": 1, "generation": repository.generation(), "entries": rows}

    @router.patch("/workspaces/{workspace_id}")
    async def update_workspace(workspace_id: str, body: WorkspaceUpdateRequest, request: Request):
        if body.name is None and body.archived is None:
            raise HTTPException(422, "Workspace update is empty")
        owner = context(request)
        try:
            current = repository.get_workspace(workspace_id)
            if current.owner_subject_id != owner.owner_subject_id:
                raise HTTPException(404, "Workspace was not found")
            updated = repository.update_workspace(
                workspace_id,
                actor_subject_id=owner.owner_subject_id,
                name=body.name,
                archived=body.archived,
                expected_revision=body.expected_revision,
            )
        except FilePolicyError as error:
            status = 409 if error.code == "revision_conflict" else 404 if error.code.endswith("_not_found") else 400
            raise HTTPException(status, detail={"code": error.code, "message": str(error)}) from error
        close_all_clients()
        resource = None
        availability = "archived" if updated.archived else "available"
        if not updated.archived:
            try:
                binding = resolve_workspace_binding(
                    repository,
                    updated,
                    subject_id=owner.owner_subject_id,
                    is_admin=owner.is_admin,
                    purpose="app_folder",
                )
                resource = await facade(request).host_resource_for_path(owner, path=binding.path)
            except (WorkspacePolicyServiceError, FilesFacadeError) as error:
                availability = getattr(error, "code", "workspace_unavailable")
        return {
            "version": 1,
            "generation": repository.generation(),
            "workspace": workspace_public(updated),
            "availability": availability,
            "resource": resource,
        }

    @router.post("/workspace-resource")
    async def workspace_resource(body: WorkspacePathResourceRequest, request: Request):
        """Resolve a Workspace-relative target into opaque Host resources.

        Code and other Workspace-aware apps may know a compatibility path while
        they edit it, but the Files handoff carries only the stable Workspace ID
        and a relative name.  This endpoint rechecks current App authority,
        resolves symlinks before containment, and returns no host path.
        """

        owner = context(request)
        try:
            workspace = repository.get_workspace(body.workspace_id)
            if workspace.owner_subject_id != owner.owner_subject_id or workspace.archived:
                raise HTTPException(404, "Workspace was not found")
            binding = resolve_workspace_binding(
                repository,
                workspace,
                subject_id=owner.owner_subject_id,
                is_admin=owner.is_admin,
                purpose="app_folder",
            )
        except FilePolicyError as error:
            raise HTTPException(404, "Workspace was not found") from error
        except WorkspacePolicyServiceError as error:
            status = 403 if error.code == "workspace_denied" else 404
            raise HTTPException(
                status,
                detail={"code": error.code, "message": "Workspace resource is unavailable"},
            ) from error

        raw_relative = str(body.relative_path or "").replace("\\", "/")
        if "\x00" in raw_relative or raw_relative.startswith("/") or re.match(r"^[A-Za-z]:", raw_relative):
            raise HTTPException(422, "Workspace-relative path is invalid")
        if raw_relative in {"", "."}:
            parts: tuple[str, ...] = ()
        else:
            parts = tuple(PurePosixPath(raw_relative).parts)
            if (
                not parts
                or any(part in {"", ".", ".."} for part in parts)
                or "//" in raw_relative
                or raw_relative.endswith("/")
            ):
                raise HTTPException(422, "Workspace-relative path is invalid")

        try:
            base = Path(binding.path).resolve(strict=True)
            target_path = base.joinpath(*parts).resolve(strict=True)
            target_path.relative_to(base)
            target = await facade(request).host_resource_for_path(owner, path=str(target_path))
            parent_path = target_path if target.get("kind") == "folder" else target_path.parent
            parent_path.relative_to(base)
            parent = target if parent_path == target_path else await facade(request).host_resource_for_path(
                owner,
                path=str(parent_path),
            )
        except (FileNotFoundError, PermissionError, OSError, ValueError, FilesFacadeError) as error:
            raise HTTPException(404, "Workspace resource is unavailable") from error

        return {
            "version": 1,
            "generation": repository.generation(),
            "workspace": workspace_public(workspace),
            "parent": parent,
            "resource": target,
        }

    @router.api_route("/content/{resource_ref}", methods=["GET", "HEAD"])
    async def content(
        resource_ref: str,
        request: Request,
        purpose: Literal["download", "preview"] = "download",
    ):
        """Download one provider resource through its opaque canonical ref.

        This compatibility data plane opens provider-owned files once and keeps
        the descriptor for the response. Host files will use the admitted Rust/
        Tonic handle transport instead; no raw provider ID or path is accepted.
        """
        descriptor = None
        try:
            ctx = context_for_ref(request, resource_ref)
            source = await facade(request).content(
                ctx,
                resource_ref=resource_ref,
                capability=purpose,
            )
            guard = _generation_guard(ctx.policy_generation)
            if source.path is not None:
                descriptor, current = _open_path_content(source)
                size = int(current.st_size)
                modified_ms = int(current.st_mtime_ns // 1_000_000)
            elif source.stream is not None:
                if source.size is None or int(source.size) < 0:
                    raise FilesFacadeError("stream content size is unavailable", code="provider_unavailable")
                size = int(source.size)
                modified_ms = source.modified_unix_ms
            else:
                size = len(source.data or b"")
                modified_ms = source.modified_unix_ms

            etag_value = str(source.etag or "").strip().strip('"')
            if not etag_value:
                identity = source.expected_identity or (0, 0, size, int((modified_ms or 0) * 1_000_000))
                etag_value = "-".join(str(value) for value in identity)
            etag = f'"{etag_value}"'
            range_header = request.headers.get("range")
            if_range = request.headers.get("if-range")
            try:
                selected = None if (if_range and if_range.strip() != etag) else _range(range_header, size)
            except FilesFacadeError as error:
                if descriptor is not None:
                    os.close(descriptor)
                    descriptor = None
                raise HTTPException(
                    416,
                    detail={"code": error.code, "message": str(error)},
                    headers={"Accept-Ranges": "bytes", "Content-Range": f"bytes */{size}"},
                ) from error
            start, end = selected if selected is not None else (0, max(0, size - 1))
            response_size = 0 if size == 0 else end - start + 1
            ascii_name, encoded_name = _download_filename(source.filename)
            declared_type = str(source.media_type or "application/octet-stream").split(";", 1)[0].strip().lower()
            active_type = declared_type in _ACTIVE_DOWNLOAD_TYPES
            media_type = "application/octet-stream" if active_type else source.media_type
            disposition = "inline" if purpose == "preview" and not active_type else "attachment"
            headers = {
                "Content-Disposition": f"{disposition}; filename=\"{ascii_name}\"; filename*=UTF-8''{encoded_name}",
                "Content-Length": str(response_size),
                "Accept-Ranges": "bytes",
                "ETag": etag,
                "X-Content-Type-Options": "nosniff",
                "Cache-Control": "private, no-store",
                "Content-Security-Policy": "default-src 'none'; sandbox",
            }
            if modified_ms is not None:
                headers["Last-Modified"] = email.utils.formatdate(modified_ms / 1000, usegmt=True)
            status_code = 206 if selected is not None else 200
            if selected is not None:
                headers["Content-Range"] = f"bytes {start}-{end}/{size}"
            if request.method == "HEAD":
                if descriptor is not None:
                    os.close(descriptor)
                return Response(status_code=status_code, media_type=media_type, headers=headers)

            if source.stream is not None:
                async def guarded_stream():
                    async for chunk in source.stream(start, response_size):
                        if not guard():
                            raise _policy_aborted()
                        yield chunk

                body = guarded_stream()
            elif descriptor is not None:
                def path_chunks():
                    remaining = response_size
                    try:
                        with os.fdopen(descriptor, "rb", closefd=True) as handle:
                            handle.seek(start)
                            while remaining > 0:
                                if not guard():
                                    raise _policy_aborted()
                                chunk = handle.read(min(256 * 1024, remaining))
                                if not chunk:
                                    break
                                remaining -= len(chunk)
                                yield chunk
                    finally:
                        try:
                            os.close(descriptor)
                        except OSError:
                            pass

                body = path_chunks()
            else:
                data = source.data or b""

                async def data_chunks():
                    for offset in range(start, start + response_size, 256 * 1024):
                        if not guard():
                            raise _policy_aborted()
                        yield data[offset:min(start + response_size, offset + 256 * 1024)]

                body = data_chunks()
            return StreamingResponse(body, status_code=status_code, media_type=media_type, headers=headers)
        except FilesFacadeError as error:
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
            if error.code == "invalid_range":
                # Do not disclose a resource's size unless its owner-bound ref
                # already resolved; the generic 416 body remains machine readable.
                raise HTTPException(416, detail={"code": error.code, "message": str(error)}) from error
            _raise(error)

    @router.get("/thumbnail/{resource_ref}")
    async def thumbnail(
        resource_ref: str,
        request: Request,
        width: int = Query(default=160, ge=1, le=1024),
        height: int = Query(default=160, ge=1, le=1024),
        scale: float = Query(default=1.0, ge=1.0, le=3.0),
    ):
        try:
            png = await facade(request).thumbnail(
                context_for_ref(request, resource_ref),
                resource_ref=resource_ref,
                width=width,
                height=height,
                scale=scale,
            )
            return Response(
                png,
                media_type="image/png",
                headers={
                    "Cache-Control": "private, no-store",
                    "X-Content-Type-Options": "nosniff",
                    "Cross-Origin-Resource-Policy": "same-origin",
                    "Content-Security-Policy": "default-src 'none'; sandbox",
                },
            )
        except FilesFacadeError as error:
            _raise(error)

    @router.post("/watch")
    async def watch(body: WatchResourceRequest, request: Request):
        """Stream path-free change hints for one visible Host directory.

        The ResourceRef is owner/policy bound before response headers. Native
        events are advisory only; consumers re-list through the facade after a
        debounce, and gaps explicitly demand a full reconciliation.
        """
        try:
            events = await facade(request).watch(
                context_for_ref(request, body.resource_ref),
                resource_ref=body.resource_ref,
            )
        except FilesFacadeError as error:
            _raise(error)

        async def event_stream():
            iterator = events.__aiter__()
            pending = asyncio.create_task(anext(iterator))
            try:
                while True:
                    done, _ = await asyncio.wait({pending}, timeout=15.0)
                    if not done:
                        yield b": keepalive\n\n"
                        continue
                    try:
                        event = pending.result()
                    except StopAsyncIteration:
                        return
                    safe = {
                        "sequence": int(event.get("sequence") or 0),
                        "kind": str(event.get("kind") or "rescan_required"),
                        "rescan_required": bool(event.get("rescan_required")),
                        "observed_unix_ms": int(event.get("observed_unix_ms") or 0),
                    }
                    yield (
                        "event: files-change\n"
                        f"data: {json.dumps(safe, separators=(',', ':'))}\n\n"
                    ).encode("utf-8")
                    pending = asyncio.create_task(anext(iterator))
            finally:
                if not pending.done():
                    pending.cancel()
                    try:
                        await pending
                    except (asyncio.CancelledError, StopAsyncIteration):
                        pass
                    except Exception:
                        pass
                close = getattr(iterator, "aclose", None)
                if callable(close):
                    await close()

        return StreamingResponse(
            event_stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "private, no-store",
                "X-Accel-Buffering": "no",
                "X-Content-Type-Options": "nosniff",
            },
        )

    return router


__all__ = ["setup_files_facade_routes"]
