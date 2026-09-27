"""Imps managed image project routes — Save with Lore recovery.

Exposes the managed project substrate as the Imps (Image Processing Suite)
save surface: versioned editable projects bound to a stable image resource
identity, Save as a recoverable image+project operation with Lore preimages,
Save a copy as a separate allocation, and explicit portable project export.

Ownership is enforced per row. The writable original keeps its identity across
Save; only Save a copy allocates a new image resource.

The ``image_writer`` on this surface writes real image bytes when the request
supplies them for a Gallery-managed resource. When the caller owns the byte
write (no bytes supplied), the response says ``image_write: "caller-owned"``
rather than claiming this endpoint published pixels.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import logging
from typing import Any, Callable, Dict, Optional, Tuple

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from core.database import GalleryImage, SessionLocal
from src.openclank.image_projects import (
    EXPORT_KIND,
    EXPORT_SCHEMA_VERSION,
    L_S19_LORE_RESTORE,
    ImageProjectError,
    ImageProjectRepository,
    ImageResourceIdentity,
    LoreCaptureFailed,
    PartialMutation,
    ProjectNotFound,
    StaleImageRevision,
    StaleProjectRevision,
    read_captured_preimage,
)

logger = logging.getLogger(__name__)


class ProjectCreate(BaseModel):
    provider: str = "gallery"
    resource_id: str
    name: str = "Untitled"
    width: Optional[int] = None
    height: Optional[int] = None
    state: Dict[str, Any] = Field(default_factory=dict)
    expected_image_revision: str = ""


class ProjectUpdate(BaseModel):
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
    operation_id: Optional[str] = None
    # New image bytes (base64 or a data URL). When supplied for a
    # Gallery-managed resource this endpoint writes them; when omitted the
    # response honestly reports the byte write as caller-owned.
    image_bytes: Optional[str] = None


class ProjectRestore(BaseModel):
    """Replay a captured Lore preimage to undo a Save."""

    action_id: str
    expected_project_revision: Optional[int] = None


class ProjectCopy(BaseModel):
    """Save a copy — allocates a separate image resource and project."""

    provider: str = "gallery"
    resource_id: str
    expected_image_revision: str
    state: Optional[Dict[str, Any]] = None
    name: Optional[str] = None
    width: Optional[int] = None
    height: Optional[int] = None
    operation_key: Optional[str] = None


class ProjectImport(BaseModel):
    provider: str = "gallery"
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
    }


def _user(request: Request) -> Optional[str]:
    from src.auth_helpers import get_current_user

    user = get_current_user(request)
    return str(user) if user is not None else None


def _require_owner(request: Request) -> str:
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


def _gallery_row(resource_id: str, owner: str):
    """Resolve an owner-scoped Gallery row for a managed image resource."""
    rid = str(resource_id or "").strip()
    if rid.startswith("image:"):
        rid = rid.split(":", 1)[1]
    if not rid:
        return None
    db = SessionLocal()
    try:
        row = (
            db.query(GalleryImage)
            .filter(
                GalleryImage.id == rid,
                GalleryImage.owner == owner,
                GalleryImage.is_active == True,  # noqa: E712
            )
            .first()
        )
        if row is not None:
            db.expunge(row)
        return row
    finally:
        db.close()


def _gallery_image_io(provider: str, resource_id: str, owner: str) -> Tuple[Optional[Callable[[], bytes]], Optional[Callable[[bytes], str]]]:
    """Return (reader, writer) for a Gallery-managed image, or (None, None).

    Non-Gallery resources (drafts, external providers) have no byte store on
    this surface; callers fall back to the honest caller-owned stub.
    """
    if str(provider or "").strip().lower() != "gallery":
        return None, None
    row = _gallery_row(resource_id, owner)
    if row is None or not row.filename:
        return None, None

    def reader() -> bytes:
        from src.generated_images import resolve_gallery_image_path

        path = resolve_gallery_image_path(row.filename, require_exists=True)
        return path.read_bytes()

    def writer(content: bytes) -> str:
        from src.generated_images import (
            discard_staged_gallery_image,
            publish_staged_gallery_image,
            resolve_gallery_image_path,
            stage_gallery_image_bytes,
        )

        destination = resolve_gallery_image_path(row.filename, require_exists=True)
        previous = destination.read_bytes()
        staged = stage_gallery_image_bytes(content)
        published = False
        try:
            publish_staged_gallery_image(staged, row.filename, replace=True)
            published = True
        except Exception:
            if published:
                restore = None
                try:
                    restore = stage_gallery_image_bytes(previous)
                    publish_staged_gallery_image(restore, row.filename, replace=True)
                finally:
                    discard_staged_gallery_image(restore)
            raise
        finally:
            discard_staged_gallery_image(staged)
        return "sha256:" + hashlib.sha256(content).hexdigest()

    return reader, writer


def setup_image_project_routes() -> APIRouter:
    router = APIRouter(tags=["imps-projects"])

    def repo() -> ImageProjectRepository:
        # The repository is constructed per-request so a configured Lore
        # capture seam can be wired without a process-global.
        from src.openclank import image_projects as _ip

        lore = getattr(_ip, "ACTIVE_LORE_CAPTURE", None)
        return ImageProjectRepository(SessionLocal, lore_capture=lore)

    @router.get("/api/imps/projects")
    async def list_projects(request: Request) -> Dict[str, Any]:
        owner = _require_owner(request)
        records = repo().list_projects(owner=owner)
        return {"projects": [_record_dict(r) for r in records]}

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
            return _record_dict(repo().get_project(project_id=project_id, owner=owner))
        except ImageProjectError as exc:
            raise _map_error(exc) from exc

    @router.post("/api/imps/projects")
    async def create_project(request: Request, body: ProjectCreate) -> Dict[str, Any]:
        owner = _require_owner(request)
        try:
            record = repo().create_project(
                owner=owner,
                image_identity=_identity(body.provider, body.resource_id),
                name=body.name,
                width=body.width,
                height=body.height,
                state=body.state,
                expected_image_revision=body.expected_image_revision,
            )
        except ImageProjectError as exc:
            raise _map_error(exc) from exc
        return _record_dict(record)

    @router.put("/api/imps/projects/{project_id}")
    async def update_project(request: Request, project_id: str, body: ProjectUpdate) -> Dict[str, Any]:
        owner = _require_owner(request)
        try:
            record = repo().update_state(
                project_id=project_id,
                owner=owner,
                state=body.state,
                expected_project_revision=body.expected_project_revision,
                name=body.name,
                width=body.width,
                height=body.height,
                expected_image_revision=body.expected_image_revision,
            )
        except ImageProjectError as exc:
            raise _map_error(exc) from exc
        return _record_dict(record)

    @router.post("/api/imps/projects/{project_id}/save")
    async def save_project(request: Request, project_id: str, body: ProjectSave) -> Dict[str, Any]:
        """Recoverable Save: Lore preimages, then image + project commit.

        When ``image_bytes`` is supplied for a Gallery-managed resource this
        endpoint writes them through the managed image store and returns the
        content-hash revision. Otherwise the byte write stays caller-owned and
        the response says so. Refused before mutation if preimage capture fails.
        """
        owner = _require_owner(request)
        content = _decode_image_bytes(body.image_bytes)
        # The project's bound resource identifies the byte store, not the
        # project id. Resolve the reader/writer from the record.
        record = None
        try:
            record = repo().get_project(project_id=project_id, owner=owner)
        except ImageProjectError as exc:
            raise _map_error(exc) from exc
        reader, writer = _gallery_image_io(record.image_provider, record.image_resource_id, owner)
        before_bytes: Optional[bytes] = None
        if content is not None and (reader is None or writer is None):
            raise HTTPException(409, "image bytes cannot be published for this provider")
        def image_reader():
            nonlocal before_bytes
            try:
                observed = reader() if reader is not None else None
            except Exception as exc:  # noqa: BLE001 — no preimage means no safe overwrite
                raise HTTPException(409, "image preimage is unavailable; reload before saving") from exc
            if observed is None:
                raise HTTPException(409, "image preimage is unavailable; reload before saving")
            if before_bytes is None:
                before_bytes = observed
            return observed

        written = {"value": body.new_image_revision, "bytes_written": False}

        def image_writer():
            if content is not None and writer is not None:
                written["value"] = writer(content)
                written["bytes_written"] = True
                return written["value"]
            # Honest stub: this endpoint does not publish pixels without
            # bytes. The client's revision is recorded under CAS only.
            return body.new_image_revision

        def image_rollback(committed_revision: str):
            if before_bytes is None or reader is None or writer is None:
                raise RuntimeError("pixel preimage is unavailable")
            current = reader()
            observed = "sha256:" + hashlib.sha256(current).hexdigest()
            if observed != committed_revision:
                raise RuntimeError("published pixels changed after Save; refusing compensation")
            writer(before_bytes)

        def metadata_updater(db):
            if record.image_provider != "gallery" or content is None:
                return
            row = db.query(GalleryImage).filter(GalleryImage.id == str(record.image_resource_id).removeprefix("image:"), GalleryImage.owner == owner).one_or_none()
            if row is not None:
                row.file_hash = hashlib.sha256(content).hexdigest()
                row.file_size = len(content)

        try:
            outcome = repo().save_image_and_project(
                owner=owner,
                project_id=project_id,
                expected_project_revision=body.expected_project_revision,
                expected_image_revision=body.expected_image_revision,
                new_image_revision=body.new_image_revision,
                image_writer=image_writer,
                image_reader=image_reader if content is not None else None,
                image_rollback=image_rollback if content is not None else None,
                metadata_updater=metadata_updater if content is not None else None,
                state=body.state,
                name=body.name,
                width=body.width,
                height=body.height,
                operation_id=body.operation_id,
                image_bytes=before_bytes if content is not None else None,
            )
        except ImageProjectError as exc:
            raise _map_error(exc) from exc
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

    @router.post("/api/imps/projects/{project_id}/save-copy")
    async def save_copy(request: Request, project_id: str, body: ProjectCopy) -> Dict[str, Any]:
        owner = _require_owner(request)

        def image_writer():
            # The copy's byte write is caller-owned: this endpoint allocates
            # the managed project record, not a new Gallery file.
            return body.expected_image_revision

        try:
            outcome = repo().save_copy(
                owner=owner,
                project_id=project_id,
                image_identity=_identity(body.provider, body.resource_id),
                expected_image_revision=body.expected_image_revision,
                image_writer=image_writer,
                state=body.state,
                name=body.name,
                width=body.width,
                height=body.height,
                operation_key=body.operation_key,
            )
        except ImageProjectError as exc:
            raise _map_error(exc) from exc
        return {
            "project_id": outcome.project_id,
            "project_revision": outcome.project_revision,
            "image": outcome.image_identity.as_key(),
            "action_id": outcome.action_id,
            "allocated": outcome.allocated,
            "refresh_receipt": {
                **outcome.refresh_receipt,
                "image_write": "caller-owned",
            },
        }

    @router.post("/api/imps/projects/{project_id}/restore")
    async def restore_project(request: Request, project_id: str, body: ProjectRestore) -> Dict[str, Any]:
        """Replay a captured Lore preimage to undo a Save.

        Restores the project state and, when the preimage captured image bytes
        for a Gallery-managed resource, those bytes too. History-worker-side
        discovery of preimages is not orchestrated (``L-S19-LORE-RESTORE``);
        replay reads the local managed preimage store.
        """
        owner = _require_owner(request)
        record = None
        try:
            record = repo().get_project(project_id=project_id, owner=owner)
        except ImageProjectError as exc:
            raise _map_error(exc) from exc
        _reader, writer = _gallery_image_io(record.image_provider, record.image_resource_id, owner)
        payload = read_captured_preimage(body.action_id)
        if payload:
            bound = payload.get("image_resource") or {}
            if (str(bound.get("provider") or "") != record.image_provider
                    or str(bound.get("resource_id") or "") != record.image_resource_id):
                raise HTTPException(409, "preimage belongs to a different image resource")
            if payload.get("image_bytes_captured") and writer is None:
                raise HTTPException(409, "captured pixels cannot be restored for this provider")
        restore_writer = None
        restore_rollback = None
        restore_metadata_updater = None
        current_bytes = None
        rollback_bytes = None
        if writer is not None and payload and payload.get("image_bytes_captured"):
            try:
                current_bytes = _reader() if _reader is not None else None
            except Exception as exc:  # noqa: BLE001 — restore must not overwrite an unknown source
                raise HTTPException(409, "current image is unavailable; reload before restoring") from exc
            if current_bytes is None:
                raise HTTPException(409, "current image is unavailable; reload before restoring")
            current_revision = str(record.expected_image_revision or "")
            if not current_revision.startswith("sha256:"):
                raise HTTPException(409, "image has no content-hash revision; reload and bind before restoring")
            if "sha256:" + hashlib.sha256(current_bytes).hexdigest() != current_revision:
                raise HTTPException(409, "image changed externally; reload before restoring")
            captured = base64.b64decode(str(payload.get("image_bytes") or ""), validate=True)

            def restore_writer() -> str:
                return writer(captured)

            def restore_reader() -> bytes:
                nonlocal current_bytes, rollback_bytes
                observed = _reader()
                if rollback_bytes is None:
                    rollback_bytes = observed
                    current_bytes = observed
                return observed

            def restore_rollback(committed_revision: str):
                latest = _reader()
                if "sha256:" + hashlib.sha256(latest).hexdigest() != committed_revision:
                    raise RuntimeError("restored pixels changed after Restore; refusing compensation")
                writer(rollback_bytes)

            def restore_metadata_updater(db):
                row = db.query(GalleryImage).filter(GalleryImage.id == str(record.image_resource_id).removeprefix("image:"), GalleryImage.owner == owner).one_or_none()
                if row is not None:
                    row.file_hash = hashlib.sha256(captured).hexdigest()
                    row.file_size = len(captured)

        try:
            outcome = repo().replay_preimage(
                owner=owner,
                project_id=project_id,
                action_id=body.action_id,
                image_writer=restore_writer,
                image_reader=restore_reader if writer is not None and payload and payload.get("image_bytes_captured") else None,
                image_rollback=restore_rollback,
                metadata_updater=restore_metadata_updater,
                expected_project_revision=body.expected_project_revision,
                current_image_bytes=current_bytes,
            )
        except ImageProjectError as exc:
            raise _map_error(exc) from exc
        return {
            "project_id": outcome.project_id,
            "project_revision": outcome.project_revision,
            "image": outcome.image_identity.as_key(),
            "action_id": outcome.action_id,
            "allocated": outcome.allocated,
            "limitation": L_S19_LORE_RESTORE,
            "refresh_receipt": outcome.refresh_receipt,
        }

    @router.get("/api/imps/projects/{project_id}/export")
    async def export_project(request: Request, project_id: str) -> Dict[str, Any]:
        owner = _require_owner(request)
        try:
            bundle = repo().export_project(project_id=project_id, owner=owner)
        except ImageProjectError as exc:
            raise _map_error(exc) from exc
        return bundle

    @router.post("/api/imps/projects/import")
    async def import_project(request: Request, body: ProjectImport) -> Dict[str, Any]:
        owner = _require_owner(request)
        try:
            record = repo().import_project(
                owner=owner,
                image_identity=_identity(body.provider, body.resource_id),
                bundle=body.bundle,
                expected_image_revision=body.expected_image_revision,
            )
        except ImageProjectError as exc:
            raise _map_error(exc) from exc
        return _record_dict(record)

    @router.get("/api/imps/export-schema")
    async def export_schema() -> Dict[str, Any]:
        return {
            "kind": EXPORT_KIND,
            "schema_version": EXPORT_SCHEMA_VERSION,
            "lore_restore_limitation": L_S19_LORE_RESTORE,
        }

    return router
