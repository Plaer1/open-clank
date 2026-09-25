"""Imps managed image project routes — Save with Lore recovery.

Exposes the managed project substrate as the Imps (Image Processing Suite)
save surface: versioned editable projects bound to a stable image resource
identity, Save as a recoverable image+project operation with Lore preimages,
Save a copy as a separate allocation, and explicit portable project export.

Ownership is enforced per row. The writable original keeps its identity across
Save; only Save a copy allocates a new image resource.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from core.database import SessionLocal
from src.openclank.image_projects import (
    EXPORT_KIND,
    EXPORT_SCHEMA_VERSION,
    ImageProjectError,
    ImageProjectRepository,
    ImageResourceIdentity,
    LoreCaptureFailed,
    ProjectNotFound,
    StaleImageRevision,
    StaleProjectRevision,
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


class ProjectCopy(BaseModel):
    """Save a copy — allocates a separate image resource and project."""

    provider: str = "gallery"
    resource_id: str
    expected_image_revision: str
    state: Optional[Dict[str, Any]] = None
    name: Optional[str] = None
    width: Optional[int] = None
    height: Optional[int] = None


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
    return HTTPException(500, str(exc))


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

        The image writer is a callback the caller layers in (byte write +
        revision). Refused before mutation if preimage capture fails.
        """
        owner = _require_owner(request)

        def image_writer():
            # The byte write is owned by the caller's image store; the
            # repository only records the resulting revision under CAS. A
            # future managed image writer can be injected here without
            # changing the recovery contract.
            return body.new_image_revision

        try:
            outcome = repo().save_image_and_project(
                owner=owner,
                project_id=project_id,
                expected_project_revision=body.expected_project_revision,
                expected_image_revision=body.expected_image_revision,
                new_image_revision=body.new_image_revision,
                image_writer=image_writer,
                state=body.state,
                name=body.name,
                width=body.width,
                height=body.height,
                operation_id=body.operation_id,
            )
        except ImageProjectError as exc:
            raise _map_error(exc) from exc
        return {
            "project_id": outcome.project_id,
            "project_revision": outcome.project_revision,
            "image": outcome.image_identity.as_key(),
            "action_id": outcome.action_id,
            "allocated": outcome.allocated,
            "refresh_receipt": outcome.refresh_receipt,
        }

    @router.post("/api/imps/projects/{project_id}/save-copy")
    async def save_copy(request: Request, project_id: str, body: ProjectCopy) -> Dict[str, Any]:
        owner = _require_owner(request)

        def image_writer():
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
            )
        except ImageProjectError as exc:
            raise _map_error(exc) from exc
        return {
            "project_id": outcome.project_id,
            "project_revision": outcome.project_revision,
            "image": outcome.image_identity.as_key(),
            "action_id": outcome.action_id,
            "allocated": outcome.allocated,
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
        return {"kind": EXPORT_KIND, "schema_version": EXPORT_SCHEMA_VERSION}

    return router
