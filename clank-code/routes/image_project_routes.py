"""Imps managed image project routes — Save with Lore recovery.

Exposes the managed project substrate as the Imps (Image Processing Suite)
save surface: versioned editable projects bound to a stable image resource
identity, Save as a recoverable image+project operation with Lore preimages,
Save a copy as a separate allocation, and explicit portable project export.

Ownership is enforced per row. The writable original keeps its identity across
Save; only Save a copy allocates a new image resource.

The ``image_writer`` on this surface writes real image bytes when the request
supplies them for a Files-managed resource. When the caller owns the byte
write (no bytes supplied), the response says ``image_write: "caller-owned"``
rather than claiming this endpoint published pixels.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import logging
from typing import Any, Dict, Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, Field

from core.database import FilesImageResource, SessionLocal
from src.generated_images import (
    discard_staged_gallery_image, publish_staged_gallery_image,
    resolve_gallery_image_path, stage_gallery_image_bytes,
)
from src.openclank.files_image_store import FilesImageStore
from src.openclank.image_projects import (
    EXPORT_KIND,
    EXPORT_SCHEMA_VERSION,
    ImageProjectError,
    ImageProjectRepository,
    ImageResourceIdentity,
    LoreCaptureFailed,
    PartialMutation,
    ProjectNotFound,
    StaleImageRevision,
    StaleProjectRevision,
    complete_portable_export,
    validate_portable_import,
)

logger = logging.getLogger(__name__)


class ProjectCreate(BaseModel):
    operation_id: str = Field(min_length=1, max_length=128)
    provider: str = "files"
    resource_id: str
    name: str = "Untitled"
    width: Optional[int] = None
    height: Optional[int] = None
    state: Dict[str, Any] = Field(default_factory=dict)
    expected_image_revision: str = ""


class ProjectUpdate(BaseModel):
    operation_id: str = Field(min_length=1, max_length=128)
    state: Dict[str, Any]
    expected_project_revision: int
    expected_image_revision: Optional[str] = None
    name: Optional[str] = None
    width: Optional[int] = None
    height: Optional[int] = None


class ProjectSave(BaseModel):
    """Recoverable Save of the writable original + its managed project."""

    expected_project_revision: int
    expected_image_revision: str
    new_image_revision: str
    state: Optional[Dict[str, Any]] = None
    name: Optional[str] = None
    width: Optional[int] = None
    height: Optional[int] = None
    operation_id: str = Field(min_length=1, max_length=128)
    # New image bytes (base64 or a data URL). When supplied for a
    # Files-managed resource this endpoint writes them; when omitted the
    # response honestly reports the byte write as caller-owned.
    image_bytes: Optional[str] = None


class ProjectRestore(BaseModel):
    operation_id: str = Field(min_length=1, max_length=128)
    """Replay a captured Lore preimage to undo a Save."""

    action_id: str
    expected_project_revision: Optional[int] = None


class ProjectCopy(BaseModel):
    operation_id: str = Field(min_length=1, max_length=128)
    """Save a copy — allocates a separate image resource and project."""

    provider: str = "files"
    resource_id: str
    expected_image_revision: str
    state: Optional[Dict[str, Any]] = None
    name: Optional[str] = None
    width: Optional[int] = None
    height: Optional[int] = None
    operation_key: Optional[str] = None


class ProjectOperationStatus(BaseModel):
    model_config = ConfigDict(extra='forbid')
    operation_id: str = Field(min_length=1, max_length=128)


class ProjectImport(BaseModel):
    operation_id: str = Field(min_length=1, max_length=128)
    provider: str = "files"
    resource_id: str
    bundle: Dict[str, Any]
    expected_image_revision: str = ""


def _identity(body_provider: str, body_resource_id: str) -> ImageResourceIdentity:
    provider = str(body_provider or "").strip()
    resource_id = str(body_resource_id or "").strip()
    if not provider or not resource_id:
        raise HTTPException(400, "image resource identity is required")
    return ImageResourceIdentity(provider, resource_id)


def _record_dict(record) -> Dict[str, Any]:
    return {
        "id": record.id,
        "owner": record.owner,
        "image": record.image_identity.as_key(),
        "expected_image_revision": record.expected_image_revision,
        "project_revision": record.project_revision,
        "name": record.name,
        "width": record.width,
        "height": record.height,
        "state": record.state,
        "is_active": record.is_active,
        "created_at": record.created_at.isoformat() if record.created_at else None,
        "updated_at": record.updated_at.isoformat() if record.updated_at else None,
        **({"lore_receipt": record.lore_receipt} if getattr(record, "lore_receipt", None) is not None else {}),
    }


def _user(request: Request) -> Optional[str]:
    from src.auth_helpers import get_current_user

    user = get_current_user(request)
    return str(user) if user is not None else None


def _require_owner(request: Request) -> str:
    original = getattr(request.state, 'image_project_original_owner', None)
    if original is not None: return original
    user = _user(request)
    # Auth-disabled installations still stamp a durable principal.
    if user is None:
        from src.generated_images import gallery_owner_key

        owner = gallery_owner_key(None)
        if owner is None:
            raise HTTPException(401, "authentication required")
        return owner
    return user


def _map_error(exc: ImageProjectError) -> HTTPException:
    if exc.code == "copy_conflict":
        return HTTPException(409, str(exc))
    if exc.code == "invalid_export":
        return HTTPException(422, str(exc))
    if isinstance(exc, ProjectNotFound):
        return HTTPException(404, str(exc))
    if isinstance(exc, (StaleProjectRevision, StaleImageRevision)):
        return HTTPException(409, str(exc))
    if isinstance(exc, LoreCaptureFailed):
        # Recovery state could not be captured — the save was refused.
        return HTTPException(503, str(exc))
    if isinstance(exc, PartialMutation):
        return HTTPException(503, {"code": exc.code, "message": str(exc), "recovery_action_id": exc.recovery_action_id})
    return HTTPException(500, str(exc))


def _decode_image_bytes(value: Optional[str]) -> Optional[bytes]:
    """Decode base64 or a data URL payload into raw image bytes."""
    raw = str(value or "").strip()
    if not raw:
        return None
    if raw.startswith("data:") and "," in raw:
        raw = raw.split(",", 1)[1]
    try:
        data = base64.b64decode(raw, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise HTTPException(400, "image_bytes must be base64 or a data URL") from exc
    if not data:
        raise HTTPException(400, "image_bytes is empty")
    return data


def _files_image(resource_id: str, owner: str):
    """Resolve an owner-scoped Files image for a managed resource."""
    rid = str(resource_id or "").strip()
    if rid.startswith("image:"):
        rid = rid.split(":", 1)[1]
    if not rid:
        return None
    try:
        return FilesImageStore(session_factory=SessionLocal).image(owner, rid)
    except Exception:
        return None


class _FilesProjectPixels:
    """Bind disk I/O and a transaction-scoped metadata update to one Files image."""

    def __init__(self, row: FilesImageResource):
        self.row = row

    def read(self) -> bytes:
        return resolve_gallery_image_path(self.row.locator, require_exists=True).read_bytes()

    def publish(self, content: bytes) -> str:
        staged = stage_gallery_image_bytes(content)
        try:
            publish_staged_gallery_image(staged, self.row.locator, replace=True)
        finally:
            discard_staged_gallery_image(staged)
        return "sha256:" + hashlib.sha256(content).hexdigest()

    def stage_metadata(self, db, expected_revision: str, content: bytes) -> None:
        # Reserve the same active Files revision before publishing pixels. Both
        # this update and the project CAS commit in the repository's transaction.
        row = self.row
        changed = db.query(FilesImageResource).filter_by(
            id=row.id, owner=row.owner, kind="image", is_active=True,
            locator=row.locator, revision=row.revision,
            digest=expected_revision.removeprefix("sha256:"),
        ).update({
            FilesImageResource.digest: hashlib.sha256(content).hexdigest(),
            FilesImageResource.size: len(content),
            FilesImageResource.revision: row.revision + 1,
        }, synchronize_session=False)
        if changed != 1:
            raise StaleImageRevision("Files image metadata changed; reload before writing")


def _files_image_io(provider: str, resource_id: str, owner: str) -> _FilesProjectPixels | None:
    if str(provider or "").strip().lower() != "files":
        return None
    row = _files_image(resource_id, owner)
    return _FilesProjectPixels(row) if row is not None and row.locator else None


async def _project_owned(function):
    """Join existing blocking project work even when its caller is cancelled."""
    from src.openclank.media_jobs import MediaCoordinator
    return await MediaCoordinator._settled_thread(function)


def image_project_repository(state, *, owner: str, account: str, current) -> ImageProjectRepository:
    """Construct the existing project owner with an actual scoped History client."""
    from src.openclank.image_project_history import ImageProjectHistory
    auth = getattr(state, 'auth_manager', None)
    supervisor = getattr(state, 'history_supervisor', None)
    if (not auth or not supervisor or not callable(current) or current() is not True
            or auth.account_id(owner) != account or auth.username_for_account_id(account) != owner):
        raise ValueError('image project recovery unavailable')
    client = supervisor.client_for(owner, account)
    history = ImageProjectHistory(client, account_id=account, actor_id=owner)
    if getattr(state, 'history_supervisor', None) is not supervisor or current() is not True:
        raise ValueError('image project recovery unavailable')
    return ImageProjectRepository(SessionLocal, history=history, account_id=account, current=current)


class _ProjectMutationRoute(APIRoute):
    """Capture original identity and hold its lifecycle admission before body parsing."""
    _mutations = {'create_project','update_project','save_project','save_copy','restore_project','import_project','project_operation_status'}

    def get_route_handler(self):
        handler = super().get_route_handler()
        if self.name not in self._mutations:
            return handler
        async def protected(request: Request):
            from src.auth_helpers import require_user, effective_user, is_internal_tool_identity
            user = require_user(request); state = request.app.state
            auth = getattr(state, 'auth_manager', None); barrier = getattr(state, 'account_request_barrier', None)
            factory = getattr(state, 'capture_original_credential', None); supervisor = getattr(state, 'history_supervisor', None)
            if (not auth or not barrier or not supervisor or not callable(factory) or is_internal_tool_identity(user)
                    or getattr(request.state, 'api_token', False) or getattr(request.state, 'internal_tool_authenticated', False)
                    or effective_user(request) != user):
                raise HTTPException(403, 'A named user is required')
            account = auth.account_id(user)
            if not account or auth.username_for_account_id(account) != user: raise HTTPException(403, 'Account unavailable')
            credential = factory(request, user, account)
            if not callable(credential) or credential() is not True: raise HTTPException(403, 'Session unavailable')
            def current():
                try:
                    return (getattr(state,'auth_manager',None) is auth and getattr(state,'account_request_barrier',None) is barrier
                        and getattr(state,'history_supervisor',None) is supervisor and getattr(state,'capture_original_credential',None) is factory
                        and not barrier._fenced(user) and auth.account_id(user)==account and auth.username_for_account_id(account)==user
                        and credential() is True)
                except Exception: return False
            receive = request._receive; size = 0
            async def bounded_receive():
                nonlocal size
                message = await receive()
                if message.get('type') == 'http.request':
                    size += len(message.get('body', b''))
                    if size > 64 * 1024 * 1024: raise HTTPException(413, 'Request too large')
                return message
            request._receive = bounded_receive
            async with barrier.admitted(user):
                if not current(): raise HTTPException(503, 'Image project authority unavailable')
                request.state.image_project_original_owner = user
                try:
                    request.state.image_project_repository = image_project_repository(state, owner=user, account=account, current=current)
                except Exception as exc:
                    raise HTTPException(503, 'Image project recovery unavailable') from exc
                response = await handler(request)
                if not current(): raise HTTPException(503, 'Image project outcome unavailable; inspect before retrying')
                return response
        return protected


def setup_image_project_routes() -> APIRouter:
    router = APIRouter(tags=["imps-projects"], route_class=_ProjectMutationRoute)

    def repo(request: Request | None = None) -> ImageProjectRepository:
        if request is not None:
            return request.state.image_project_repository
        return ImageProjectRepository(SessionLocal)

    @router.get("/api/imps/projects")
    async def list_projects(request: Request) -> Dict[str, Any]:
        owner = _require_owner(request)
        records = repo().list_projects(owner=owner)
        return {"projects": [_record_dict(r) for r in records]}

    @router.get("/api/imps/images/{resource_id}")
    async def get_files_image(request: Request, resource_id: str) -> Dict[str, Any]:
        """Files-owned image metadata for the Imps source picker."""
        owner = _require_owner(request)
        try:
            row = FilesImageStore(session_factory=SessionLocal).image(owner, resource_id)
        except Exception as exc:
            raise HTTPException(404, "Files image not found") from exc
        provenance = row.provenance or {}
        return {
            "provider": "files", "resource_id": f"image:{row.id}",
            "filename": row.display_name, "prompt": str(provenance.get("prompt") or ""),
            "caption": str(provenance.get("caption") or ""), "model": str(provenance.get("model") or ""),
            "favorite": bool(row.favorite), "created_at": row.created_at.isoformat() if row.created_at else None,
            "updated_at": row.updated_at.isoformat() if row.updated_at else None,
            "width": provenance.get("width"), "height": provenance.get("height"),
            "file_size": row.size, "media_type": row.mime_type, "read_only": False,
        }

    @router.get("/api/imps/images/{resource_id}/content")
    async def get_files_image_content(request: Request, resource_id: str):
        owner = _require_owner(request)
        try:
            row = FilesImageStore(session_factory=SessionLocal).image(owner, resource_id)
            from src.generated_images import resolve_gallery_image_path
            path = resolve_gallery_image_path(row.locator, require_exists=True)
        except Exception as exc:
            raise HTTPException(404, "Files image not found") from exc
        return FileResponse(path, media_type=row.mime_type or "application/octet-stream", filename=row.display_name)

    @router.post("/api/imps/images/{resource_id}/transform")
    async def transform_files_image(request: Request, resource_id: str, body: Dict[str, Any]) -> Dict[str, Any]:
        """Run the retained Imps transform capability against a Files image."""
        owner = _require_owner(request)
        action = str(body.get("action") or "").strip().lower()
        if action == "style-transfer":
            action = "harmonize"
        from src.tools.image import do_edit_image
        result = await do_edit_image(
            json.dumps({"image_id": resource_id, "action": action, "prompt": body.get("prompt") or ""}),
            owner=owner,
        )
        if result.get("exit_code"):
            raise HTTPException(400, str(result.get("error") or "Files image transform failed"))
        return result

    @router.get("/api/imps/projects/for-image/{provider}/{resource_id:path}")
    async def project_for_image(
        request: Request, provider: str, resource_id: str
    ) -> Dict[str, Any]:
        """Newest managed project bound to an image resource, if any.

        The editor uses this to reattach to existing editable work when an
        image is opened through Files/Imps.
        """
        owner = _require_owner(request)
        identity = _identity(provider, resource_id)
        record = repo().find_for_image(owner=owner, image_identity=identity)
        return {"project": _record_dict(record) if record is not None else None}

    @router.get("/api/imps/projects/{project_id}")
    async def get_project(request: Request, project_id: str) -> Dict[str, Any]:
        owner = _require_owner(request)
        try:
            record = repo().get_project(project_id=project_id, owner=owner)
            return _record_dict(record)
        except ImageProjectError as exc:
            raise _map_error(exc) from exc

    @router.post("/api/imps/projects")
    async def create_project(request: Request, body: ProjectCreate) -> Dict[str, Any]:
        owner = _require_owner(request)
        try:
            record = await _project_owned(lambda: repo(request).create_project(
                owner=owner,
                operation_id=body.operation_id,
                image_identity=_identity(body.provider, body.resource_id),
                name=body.name,
                width=body.width,
                height=body.height,
                state=body.state,
                expected_image_revision=body.expected_image_revision,
            ))
        except ImageProjectError as exc:
            raise _map_error(exc) from exc
        return _record_dict(record)

    @router.put("/api/imps/projects/{project_id}")
    async def update_project(request: Request, project_id: str, body: ProjectUpdate) -> Dict[str, Any]:
        owner = _require_owner(request)
        try:
            record = await _project_owned(lambda: repo(request).update_state(
                project_id=project_id,
                owner=owner,
                operation_id=body.operation_id,
                state=body.state,
                expected_project_revision=body.expected_project_revision,
                name=body.name,
                width=body.width,
                height=body.height,
                expected_image_revision=body.expected_image_revision,
            ))
        except ImageProjectError as exc:
            raise _map_error(exc) from exc
        from src.openclank.achievement_producers import record_activity
        layers = [layer for layer in (record.state.get("layers") or []) if isinstance(layer, dict)]
        record_activity(request, "imps.project.saved", f"{project_id}:{record.project_revision}", {
            "projectId": project_id, "revisionId": str(record.project_revision),
            "layerIds": [str(layer["id"]) for layer in layers if layer.get("id") is not None],
            "editableLayerCount": sum(1 for layer in layers if not layer.get("locked", False) and layer.get("dataUrl")),
        })
        return _record_dict(record)

    @router.post("/api/imps/projects/{project_id}/save")
    async def save_project(request: Request, project_id: str, body: ProjectSave) -> Dict[str, Any]:
        """Recoverable Save: Lore preimages, then image + project commit.

        When ``image_bytes`` is supplied for a Files-managed resource this
        endpoint writes them through the managed image store and returns the
        content-hash revision. Otherwise the byte write stays caller-owned and
        the response says so. Refused before mutation if preimage capture fails.
        """
        owner = _require_owner(request)
        content = await _project_owned(lambda: _decode_image_bytes(body.image_bytes))
        # The project's bound resource identifies the byte store, not the
        # project id. Resolve the reader/writer from the record.
        record = None
        try:
            record = repo(request).get_project(project_id=project_id, owner=owner)
        except ImageProjectError as exc:
            raise _map_error(exc) from exc
        pixels = _files_image_io(record.image_provider, record.image_resource_id, owner)
        before_bytes: Optional[bytes] = None
        if content is not None and pixels is None:
            raise HTTPException(409, "image bytes cannot be published for this provider")
        def image_reader():
            nonlocal before_bytes
            try:
                observed = pixels.read() if pixels is not None else None
            except Exception as exc:  # noqa: BLE001 — no preimage means no safe overwrite
                raise HTTPException(409, "image preimage is unavailable; reload before saving") from exc
            if observed is None:
                raise HTTPException(409, "image preimage is unavailable; reload before saving")
            if before_bytes is None:
                before_bytes = observed
            return observed

        written = {"value": body.new_image_revision, "bytes_written": False}

        def image_writer():
            if content is not None and pixels is not None:
                written["value"] = pixels.publish(content)
                written["bytes_written"] = True
                return written["value"]
            # Honest stub: this endpoint does not publish pixels without
            # bytes. The client's revision is recorded under CAS only.
            return body.new_image_revision

        def image_rollback(committed_revision: str):
            if before_bytes is None or pixels is None:
                raise RuntimeError("pixel preimage is unavailable")
            current = pixels.read()
            observed = "sha256:" + hashlib.sha256(current).hexdigest()
            if observed != committed_revision:
                raise RuntimeError("published pixels changed after Save; refusing compensation")
            pixels.publish(before_bytes)

        try:
            outcome = await _project_owned(lambda: repo(request).save_image_and_project(
                owner=owner,
                project_id=project_id,
                expected_project_revision=body.expected_project_revision,
                expected_image_revision=body.expected_image_revision,
                new_image_revision=body.new_image_revision,
                image_writer=image_writer,
                image_reader=image_reader if content is not None else None,
                image_rollback=image_rollback if content is not None else None,
                stage_image_metadata=(lambda db: pixels.stage_metadata(db, body.expected_image_revision, content))
                if pixels is not None and content is not None else None,
                state=body.state,
                name=body.name,
                width=body.width,
                height=body.height,
                operation_id=body.operation_id,
                desired_image_sha256=hashlib.sha256(content).hexdigest() if content is not None else None,
                image_bytes=before_bytes if content is not None else None,
            ))
        except ImageProjectError as exc:
            raise _map_error(exc) from exc
        from src.openclank.achievement_producers import record_activity
        try:
            saved_record = repo(request).get_project(project_id=project_id, owner=owner)
            if saved_record.project_revision == outcome.project_revision:
                layers = [layer for layer in (saved_record.state.get("layers") or []) if isinstance(layer, dict)]
                record_activity(request, "imps.project.saved", str(outcome.action_id), {
                    "projectId": project_id, "revisionId": str(outcome.project_revision),
                    "layerIds": [str(layer["id"]) for layer in layers if layer.get("id") is not None],
                    "editableLayerCount": sum(1 for layer in layers if not layer.get("locked", False) and layer.get("dataUrl")),
                })
        except Exception:
            pass  # Committed Save must not become a retryable failure over award delivery.
        receipt = dict(outcome.refresh_receipt)
        if written["bytes_written"]:
            receipt["image_write"] = "written-by-imps"
            receipt["image_revision"] = written["value"]
        return {
            "project_id": outcome.project_id,
            "project_revision": outcome.project_revision,
            "image": outcome.image_identity.as_key(),
            "action_id": outcome.action_id,
            "allocated": outcome.allocated,
            "lore_receipt": outcome.lore_receipt,
            "refresh_receipt": receipt,
        }

    @router.post("/api/imps/projects/save-copy")
    @router.post("/api/imps/projects/{project_id}/save-copy")
    async def save_copy(request: Request, body: ProjectCopy, project_id: Optional[str] = None) -> Dict[str, Any]:
        owner = _require_owner(request)
        # A copy binds an already-published owner Files image; this route does not publish pixels.
        if body.provider != "files" or (project_id is None and not body.operation_key):
            raise HTTPException(422, "copy requires an owned Files image and operation identity")
        row = _files_image(body.resource_id, owner)
        if row is None or not row.locator:
            raise HTTPException(404, "allocated copy image is unavailable to this owner")
        reader = _FilesProjectPixels(row).read
        try:
            digest = await _project_owned(lambda: "sha256:" + hashlib.sha256(reader()).hexdigest())
        except Exception as exc:
            raise HTTPException(409, "allocated copy bytes are unavailable") from exc
        if digest != body.expected_image_revision or digest != "sha256:" + str(row.digest or ""):
            raise HTTPException(409, "allocated copy image revision changed")
        repository = repo(request); original_current = repository.current
        def copy_current():
            try:
                fresh = _files_image(body.resource_id, owner)
                return (original_current() is True and fresh is not None and fresh.revision == row.revision
                    and fresh.digest == row.digest and "sha256:" + hashlib.sha256(reader()).hexdigest() == digest
                    and original_current() is True)
            except Exception: return False
        repository.current = copy_current

        try:
            outcome = await _project_owned(lambda: repository.save_copy(
                owner=owner,
                operation_id=body.operation_id,
                project_id=project_id,
                image_identity=_identity(body.provider, body.resource_id),
                expected_image_revision=body.expected_image_revision,
                state=body.state,
                name=body.name,
                width=body.width,
                height=body.height,
                operation_key=body.operation_key,
            ))
        except ImageProjectError as exc:
            raise _map_error(exc) from exc
        from src.openclank.achievement_producers import record_activity
        try:
            saved_record = repo(request).get_project(project_id=outcome.project_id, owner=owner)
            if saved_record.project_revision == outcome.project_revision:
                layers = [layer for layer in (saved_record.state.get("layers") or []) if isinstance(layer, dict)]
                record_activity(request, "imps.project.saved", f"copy:{outcome.project_id}:{outcome.project_revision}", {
                    "projectId": outcome.project_id, "revisionId": str(outcome.project_revision),
                    "layerIds": [str(layer["id"]) for layer in layers if layer.get("id") is not None],
                    "editableLayerCount": sum(1 for layer in layers if not layer.get("locked", False) and layer.get("dataUrl")),
                })
        except Exception:
            pass  # The committed copy remains successful if receipt delivery is unavailable.
        return {
            "project_id": outcome.project_id,
            "project_revision": outcome.project_revision,
            "image": outcome.image_identity.as_key(),
            "action_id": outcome.action_id,
            "allocated": outcome.allocated,
            "lore_receipt": outcome.lore_receipt,
            "refresh_receipt": {
                **outcome.refresh_receipt,
                "image_write": "caller-owned",
            },
        }

    @router.post("/api/imps/projects/{project_id}/restore")
    async def restore_project(request: Request, project_id: str, body: ProjectRestore) -> Dict[str, Any]:
        """Replay a captured Lore preimage to undo a Save.

        Restores the project state and, when the preimage captured image bytes
        for a Files-managed resource, those bytes too. The exact original
        preimage is read from authenticated History.
        """
        owner = _require_owner(request)
        record = None
        try:
            record = repo(request).get_project(project_id=project_id, owner=owner)
        except ImageProjectError as exc:
            raise _map_error(exc) from exc
        pixels = _files_image_io(record.image_provider, record.image_resource_id, owner)
        try:
            payload = await _project_owned(lambda: repo(request).read_preimage(owner=owner, project_id=project_id, action_id=body.action_id))
        except ImageProjectError as exc:
            raise _map_error(exc) from exc
        if payload:
            bound = payload.get("image_resource") or {}
            if (str(bound.get("provider") or "") != record.image_provider
                    or str(bound.get("resource_id") or "") != record.image_resource_id):
                raise HTTPException(409, "preimage belongs to a different image resource")
            if payload.get("image_bytes_captured") and pixels is None:
                raise HTTPException(409, "captured pixels cannot be restored for this provider")
        restore_writer = None
        restore_rollback = None
        current_bytes = None
        rollback_bytes = None
        if pixels is not None and payload and payload.get("image_bytes_captured"):
            try:
                current_bytes = await _project_owned(pixels.read)
            except Exception as exc:  # noqa: BLE001 — restore must not overwrite an unknown source
                raise HTTPException(409, "current image is unavailable; reload before restoring") from exc
            if current_bytes is None:
                raise HTTPException(409, "current image is unavailable; reload before restoring")
            current_revision = str(record.expected_image_revision or "")
            if not current_revision.startswith("sha256:"):
                raise HTTPException(409, "image has no content-hash revision; reload and bind before restoring")
            if "sha256:" + hashlib.sha256(current_bytes).hexdigest() != current_revision:
                raise HTTPException(409, "image changed externally; reload before restoring")
            captured = await _project_owned(lambda: base64.b64decode(str(payload.get("image_bytes") or ""), validate=True))

            def restore_writer() -> str:
                return pixels.publish(captured)

            def restore_reader() -> bytes:
                nonlocal current_bytes, rollback_bytes
                observed = pixels.read()
                if rollback_bytes is None:
                    rollback_bytes = observed
                    current_bytes = observed
                return observed

            def restore_rollback(committed_revision: str):
                latest = pixels.read()
                if "sha256:" + hashlib.sha256(latest).hexdigest() != committed_revision:
                    raise RuntimeError("restored pixels changed after Restore; refusing compensation")
                pixels.publish(rollback_bytes)

        try:
            outcome = await _project_owned(lambda: repo(request).replay_preimage(
                owner=owner,
                operation_id=body.operation_id,
                project_id=project_id,
                action_id=body.action_id,
                image_writer=restore_writer,
                image_reader=restore_reader if restore_writer is not None else None,
                image_rollback=restore_rollback,
                stage_image_metadata=(lambda db: pixels.stage_metadata(db, current_revision, captured))
                if restore_writer is not None else None,
                expected_project_revision=body.expected_project_revision,
                current_image_bytes=current_bytes,
            ))
        except ImageProjectError as exc:
            raise _map_error(exc) from exc
        return {
            "project_id": outcome.project_id,
            "project_revision": outcome.project_revision,
            "image": outcome.image_identity.as_key(),
            "action_id": outcome.action_id,
            "allocated": outcome.allocated,
            "lore_receipt": outcome.lore_receipt,
            "refresh_receipt": outcome.refresh_receipt,
        }

    def completed_export(request, bundle):
        from src.openclank.achievement_producers import record_activity
        artifact = complete_portable_export(bundle)
        digest = hashlib.sha256(json.dumps(artifact, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
        record_activity(request, "imps.project.exported", digest, {
            "editableExport": True, "manifestValid": True, "assetSetValid": True,
            "exportArtifactId": digest,
        })
        return artifact

    @router.post("/api/imps/projects/export-bundle")
    async def export_unbound_project(request: Request, body: Dict[str, Any]) -> Dict[str, Any]:
        _require_owner(request)
        try:
            return completed_export(request, body)
        except ImageProjectError as exc:
            raise _map_error(exc) from exc

    @router.post("/api/imps/projects/validate-bundle")
    async def validate_project_bundle(request: Request, body: Dict[str, Any]) -> Dict[str, Any]:
        _require_owner(request)
        try:
            validate_portable_import(body)
        except ImageProjectError as exc:
            raise _map_error(exc) from exc
        return body

    @router.get("/api/imps/projects/{project_id}/export")
    async def export_project(request: Request, project_id: str) -> Dict[str, Any]:
        owner = _require_owner(request)
        try:
            bundle = completed_export(request, repo().export_project(project_id=project_id, owner=owner))
        except ImageProjectError as exc:
            raise _map_error(exc) from exc
        return bundle

    @router.post("/api/imps/projects/import")
    async def import_project(request: Request, body: ProjectImport) -> Dict[str, Any]:
        owner = _require_owner(request)
        try:
            record = await _project_owned(lambda: repo(request).import_project(
                owner=owner,
                operation_id=body.operation_id,
                image_identity=_identity(body.provider, body.resource_id),
                bundle=body.bundle,
                expected_image_revision=body.expected_image_revision,
            ))
        except ImageProjectError as exc:
            raise _map_error(exc) from exc
        return _record_dict(record)

    @router.post("/api/imps/project-operations/status")
    async def project_operation_status(request: Request, body: ProjectOperationStatus) -> Dict[str, Any]:
        owner = _require_owner(request); repository = repo(request)
        try:
            def lookup():
                original = repository.history.original(action_id=body.operation_id)
                if not isinstance(original, dict) or repository.current() is not True:
                    return {'state':'unavailable'}
                record = repository.get_project(project_id=original['project_id'], owner=owner)
                if repository.current() is not True:
                    return {'state':'unavailable'}
                return {'state':'available', 'project_id':record.id, 'project_revision':record.project_revision,
                        'action_id':original['action_id'], 'outcome':original['outcome'], 'history_status':original['history_status']}
            return await _project_owned(lookup)
        except Exception:
            return {'state':'unavailable'}

    @router.get("/api/imps/export-schema")
    async def export_schema() -> Dict[str, Any]:
        return {
            "kind": EXPORT_KIND,
            "schema_version": EXPORT_SCHEMA_VERSION,
        }

    return router
