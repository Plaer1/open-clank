"""Owner management and opaque download routes for agent-published files."""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel

from src.auth_helpers import effective_user
from src.published_files import PublishedFileError, PublishedFileService
from src.settings import load_settings


class GrantRequest(BaseModel):
    audience: str = "owner"
    expires_in_hours: Optional[int] = None


def _origin() -> str:
    return str(load_settings().get("app_public_url") or "").strip()


def _owner(request: Request) -> str:
    return str(effective_user(request) or "").strip().lower()


def setup_published_file_routes(service: Optional[PublishedFileService] = None) -> APIRouter:
    service = service or PublishedFileService()
    router = APIRouter(prefix="/api/files", tags=["files"])

    @router.get("/library")
    async def list_files(
        request: Request,
        search: str = Query(""),
        limit: int = Query(200, ge=1, le=500),
    ):
        files = service.list(owner=_owner(request), search=search, limit=limit)
        return {"files": files, "total": len(files)}

    @router.get("/{file_id}/content")
    async def owner_download(request: Request, file_id: str):
        info = service.get_owned(file_id, owner=_owner(request))
        if not info:
            raise HTTPException(404, "File not found")
        return FileResponse(
            info["path"],
            media_type=info["mime_type"],
            filename=info["filename"],
            headers={"X-Content-Type-Options": "nosniff", "Cache-Control": "private, no-store"},
        )

    @router.get("/download/{token}")
    async def granted_download(request: Request, token: str):
        info = service.resolve_grant(token)
        if not info:
            raise HTTPException(404, "File not found")
        if info["audience"] == "owner":
            user = _owner(request)
            auth_manager = getattr(getattr(request.app, "state", None), "auth_manager", None)
            is_admin = bool(auth_manager and user and auth_manager.is_admin(user))
            if user != info["owner"] and not is_admin:
                raise HTTPException(404, "File not found")
        return FileResponse(
            info["path"],
            media_type=info["mime_type"],
            filename=info["filename"],
            headers={"X-Content-Type-Options": "nosniff", "Cache-Control": "private, no-store"},
        )

    @router.post("/{file_id}/grants")
    async def create_grant(request: Request, file_id: str, body: GrantRequest):
        try:
            return service.create_grant(
                file_id,
                owner=_owner(request),
                audience=body.audience,
                expires_in_hours=body.expires_in_hours,
                public_origin=_origin(),
            )
        except PublishedFileError as exc:
            status = 404 if str(exc) == "File not found." else 400
            raise HTTPException(status, str(exc)) from exc

    @router.post("/{file_id}/revoke")
    async def revoke_links(request: Request, file_id: str):
        try:
            return {"revoked": service.revoke_all(file_id, owner=_owner(request))}
        except PublishedFileError as exc:
            raise HTTPException(404, "File not found") from exc

    @router.delete("/{file_id}")
    async def delete_file(request: Request, file_id: str):
        if not service.delete(file_id, owner=_owner(request)):
            raise HTTPException(404, "File not found")
        return {"deleted": True}

    return router
