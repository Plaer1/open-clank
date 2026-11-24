"""Authenticated Browser preview-session endpoints.

This router is intentionally not registered by app.py in this packet. The
parent-owned shell/server hook must inject the existing FilesFacade factory and
own its application lifecycle before exposing these routes.
"""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import os
import stat
from dataclasses import replace
from typing import Annotated, Any, Callable, Mapping
from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from src.constants import DATA_DIR
from src.openclank.browser_preview import BrowserPreviewError, BrowserPreviewService
from src.openclank.browser_preview_origin import PreviewAsset
from src.openclank.file_policy import FilePolicyRepository
from src.openclank.files_facade import FilesFacade, FilesFacadeError, ProviderContext
from src.openclank.files_principal import files_principal
from src.openclank.resource_refs import ResourceRefError, resolve_resource_ref


MAX_PREVIEW_BYTES = 8 * 1024 * 1024
MAX_TOTAL_PREVIEW_BYTES = 16 * 1024 * 1024
_CONTENT_BOUND_REVISION_KINDS = frozenset({"hostFingerprint", "imageSha256", "contentDigest"})


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RevisionRequest(_StrictModel):
    kind: str = Field(min_length=1, max_length=128)
    value: str = Field(min_length=1, max_length=512)


class DependencyRequest(_StrictModel):
    path: str = Field(min_length=1, max_length=256)
    resource_ref: str = Field(min_length=8, max_length=16_384)
    expected_revision: RevisionRequest | None = None


class CreatePreviewRequest(_StrictModel):
    resource_ref: str = Field(min_length=8, max_length=16_384)
    expected_revision: RevisionRequest | None = None
    dependencies: list[DependencyRequest] = Field(default_factory=list, max_length=32)
    dependency_warnings: list[Annotated[str, Field(max_length=256)]] = Field(default_factory=list, max_length=16)
    client_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{32}$")
    client_generation: int | None = Field(default=None, ge=1)


class CancelPreviewRequest(_StrictModel):
    client_id: str = Field(min_length=32, max_length=32, pattern=r"^[0-9a-f]{32}$")
    client_generation: int = Field(ge=1)


def setup_browser_preview_routes(
    *,
    facade_factory: Callable[[Request, ProviderContext], FilesFacade],
    policy_repository: FilePolicyRepository | None = None,
    data_dir: str | os.PathLike[str] = DATA_DIR,
    principal_resolver: Callable[..., tuple[str, str, bool]] = files_principal,
    preview_service: BrowserPreviewService | None = None,
    session_cookie: str = "open_clank_session",
) -> APIRouter:
    """Create preview session API and its stable loopback artifact origin."""
    router = APIRouter(prefix="/api/browser-previews", tags=["browser-preview"])
    repository = policy_repository or FilePolicyRepository()
    service = preview_service or BrowserPreviewService(data_dir)
    router.state = type("BrowserPreviewRouterState", (), {"preview_service": service})()

    def principal_context(request: Request, token: str, *, capability: str) -> tuple[ProviderContext, Any]:
        try:
            owner_id, username, is_admin = principal_resolver(request, repository=repository)
            generation = repository.generation()
            ref = resolve_resource_ref(
                token,
                expected_owner_subject_id=owner_id,
                current_policy_generation=generation,
                required_capability=capability,
            )
        except HTTPException:
            raise
        except ResourceRefError as exc:
            status = 409 if exc.code == "resource_ref_stale" else 404
            raise HTTPException(status, detail={"code": exc.code, "message": str(exc)}) from exc
        workspace = str(ref.workspace_id or "default")
        context = ProviderContext(
            owner_subject_id=str(owner_id), owner_username=str(username),
            policy_generation=int(generation), is_admin=bool(is_admin), workspace_id=workspace,
        )
        return context, ref

    async def read_resource(
        files: FilesFacade,
        context: ProviderContext,
        ref_token: str,
        expected_revision: Mapping[str, str] | None,
        *,
        html_document: bool = True,
    ) -> tuple[dict, bytes, dict[str, str], str]:
        try:
            before = await files.stat(context, resource_ref=ref_token)
            if not isinstance(before, Mapping):
                raise FilesFacadeError("resource metadata is unavailable", code="provider_unavailable")
            source = await files.content(context, resource_ref=ref_token, capability="preview")
            data = await _read_provider_content(source, max_bytes=MAX_PREVIEW_BYTES)
            after = await files.stat(context, resource_ref=ref_token)
        except FilesFacadeError as exc:
            status = 409 if exc.code in {"resource_changed", "resource_ref_stale", "policy_generation_changed"} else 403 if exc.code == "resource_unavailable" else 503 if exc.code == "provider_unavailable" else 404
            raise HTTPException(status, detail={"code": exc.code, "message": str(exc)}) from exc
        before_revision = _normalized_revision(before, data)
        after_revision = _normalized_revision(after, data)
        if before_revision != after_revision:
            raise HTTPException(409, detail={"code":"resource_changed", "message":"Resource changed while the preview was being prepared."})
        if expected_revision is not None and dict(expected_revision) != after_revision:
            raise HTTPException(409, detail={"code":"resource_changed", "message":"The selected resource revision is stale."})
        actual_type = str(source.media_type or "").split(";", 1)[0].strip().lower()
        allowed_types = {"text/html", "application/xhtml+xml"} if html_document else {
            "text/css", "text/javascript", "application/javascript",
            "image/png", "image/jpeg", "image/gif", "image/webp", "image/svg+xml",
        }
        if actual_type not in allowed_types:
            code = "unsupported_preview_type" if html_document else "unsupported_dependency_type"
            raise HTTPException(415, detail={"code":code, "message":"The Files resource type is not allowed in this Browser preview."})
        if len(data) > MAX_PREVIEW_BYTES:
            raise HTTPException(413, detail={"code":"preview_too_large", "message":"HTML preview exceeds its byte limit."})
        # FilesFacade publishes the stable equality identity as ``id``.  Keep
        # the session's internal resource_key name, but never derive identity
        # from a path or from the opaque access reference.
        resource_key = str(after.get("id") or "").strip()
        if not resource_key or not str(before.get("id") or "").strip():
            raise HTTPException(503, detail={"code":"resource_identity_unavailable", "message":"Stable Files resource identity is unavailable."})
        if (str(before.get("id") or "").strip() != resource_key
                or before.get("policy_generation") != context.policy_generation
                or after.get("policy_generation") != context.policy_generation):
            raise HTTPException(409, detail={"code":"resource_changed", "message":"Resource identity or policy changed while the preview was being prepared."})
        return {**dict(after), "resource_key": resource_key}, data, after_revision, actual_type

    def app_origin(request: Request) -> str:
        scheme = str(request.url.scheme or "").lower()
        host = str(request.headers.get("host") or "").strip()
        candidate = f"{scheme}://{host}"
        parsed = urlsplit(candidate)
        hostname = str(parsed.hostname or "").lower()
        if scheme != "http" or parsed.username or parsed.password:
            raise HTTPException(503, detail={"code":"browser_preview_unavailable", "message":"Interactive Browser previews require an available loopback HTTP origin."})
        try:
            if hostname != "localhost" and not ipaddress.ip_address(hostname).is_loopback:
                raise ValueError("non-loopback")
            parsed.port
        except (ValueError, TypeError):
            raise HTTPException(503, detail={"code":"browser_preview_unavailable", "message":"Interactive Browser previews are unavailable for this application origin."})
        return candidate

    def session_is_current(request: Request, owner_subject_id: str, token: str) -> bool:
        manager = getattr(getattr(request.app, "state", None), "auth_manager", None)
        if manager is None or not token:
            return False
        try:
            username = manager.get_username_for_token(token)
            account_id = manager.account_id(username) if username else None
            return bool(username and str(account_id or "") == str(owner_subject_id))
        except Exception:
            return False

    def build_validator(
        *, request: Request, session_token: str,
        files: FilesFacade,
        context: ProviderContext,
        resources: dict[str, tuple[str, dict[str, str], str, bool]],
        event_loop: asyncio.AbstractEventLoop,
    ) -> Callable[[str], bool]:
        async def current_resource(spec: tuple[str, dict[str, str], str, bool], fresh_context: ProviderContext, generation: int) -> bool:
            ref_token, expected_revision, expected_digest, html_document = spec
            try:
                resolve_resource_ref(
                    ref_token,
                    expected_owner_subject_id=context.owner_subject_id,
                    current_policy_generation=generation,
                    required_capability="preview",
                )
                current = await files.stat(fresh_context, resource_ref=ref_token)
                if not isinstance(current, Mapping):
                    return False
                capabilities = {str(value) for value in current.get("capabilities", ())}
                if not {"preview", "stat"}.issubset(capabilities):
                    return False
                allowed_types = {"text/html", "application/xhtml+xml"} if html_document else {
                    "text/css", "text/javascript", "application/javascript",
                    "image/png", "image/jpeg", "image/gif", "image/webp", "image/svg+xml",
                }
                current_type = str(current.get("mime_type") or current.get("media_type") or "").split(";", 1)[0].strip().lower()
                current_revision = current.get("revision")
                if (
                    isinstance(current_revision, Mapping)
                    and str(current_revision.get("kind") or "") in _CONTENT_BOUND_REVISION_KINDS
                    and dict(current_revision) == expected_revision
                    and current_type in allowed_types
                ):
                    return True
                source = await files.content(fresh_context, resource_ref=ref_token, capability="preview")
                data = await _read_provider_content(source, max_bytes=MAX_PREVIEW_BYTES)
                actual_type = str(source.media_type or "").split(";", 1)[0].strip().lower()
                if actual_type not in allowed_types:
                    return False
                digest = "sha256:" + hashlib.sha256(data).hexdigest()
                return digest == expected_digest
            except Exception:
                return False

        async def check(asset_key: str) -> bool:
            try:
                if not session_is_current(request, context.owner_subject_id, session_token):
                    return False
                generation = repository.generation()
                fresh_context = replace(context, policy_generation=int(generation))
                root = resources.get("")
                requested = resources.get(asset_key) if asset_key else root
                if root is None or requested is None:
                    return False
                if not await current_resource(root, fresh_context, generation):
                    return False
                if requested is not root and not await current_resource(requested, fresh_context, generation):
                    return False
                return True
            except Exception:
                return False

        def validate(asset_key: str) -> bool:
            if event_loop.is_closed():
                return False
            future = asyncio.run_coroutine_threadsafe(check(str(asset_key or "")), event_loop)
            try:
                return bool(future.result(timeout=8))
            except Exception:
                future.cancel()
                return False
        return validate

    @router.post("")
    async def create_preview(body: CreatePreviewRequest, request: Request):
        context, resource_ref = principal_context(request, body.resource_ref, capability="preview")
        session_token = str(request.cookies.get(session_cookie) or "")
        if not session_is_current(request, context.owner_subject_id, session_token):
            raise HTTPException(401, detail={"code":"authentication_required", "message":"A current authenticated Files owner session is required."})
        if (body.client_id is None) != (body.client_generation is None):
            raise HTTPException(422, detail={"code":"invalid_preview_request", "message":"Preview client identity and generation must be supplied together."})
        if body.client_id is not None and not service.note_client_generation(
            owner_subject_id=context.owner_subject_id, client_id=body.client_id,
            generation=body.client_generation,
        ):
            raise HTTPException(409, detail={"code":"stale_preview_request", "message":"A newer Browser preview request superseded this one."})
        if "stat" not in resource_ref.capabilities:
            raise HTTPException(403, detail={"code":"resource_unavailable", "message":"Revision-bound Browser previews require Files stat capability."})
        files = facade_factory(request, context)
        resource, html, revision, _media_type = await read_resource(files, context, body.resource_ref, body.expected_revision.model_dump() if body.expected_revision else None)
        scope_workspace = str(resource_ref.workspace_id or "default")
        dependency_assets: dict[str, PreviewAsset] = {}
        dependency_specs: dict[str, tuple[str, dict[str, str], str, bool]] = {}
        if not _safe_dependency_path_for_route(body.dependencies):
            raise HTTPException(422, detail={"code":"invalid_dependency_manifest", "message":"Dependency paths must be normalized relative URLs."})
        total_bytes = len(html)
        for dependency in body.dependencies:
            if dependency.path in dependency_assets:
                raise HTTPException(422, detail={"code":"invalid_dependency_manifest", "message":"Dependency paths must be unique."})
            context_dep, dep_ref = principal_context(request, dependency.resource_ref, capability="preview")
            if str(dep_ref.workspace_id or "default") != scope_workspace:
                raise HTTPException(403, detail={"code":"resource_unavailable", "message":"Preview dependencies must belong to the same workspace."})
            if "stat" not in dep_ref.capabilities:
                raise HTTPException(403, detail={"code":"resource_unavailable", "message":"Preview dependencies require Files stat capability."})
            _dep_resource, dep_data, dep_revision, dep_type = await read_resource(
                files, context_dep, dependency.resource_ref,
                dependency.expected_revision.model_dump() if dependency.expected_revision else None,
                html_document=False,
            )
            total_bytes += len(dep_data)
            if total_bytes > MAX_TOTAL_PREVIEW_BYTES:
                raise HTTPException(413, detail={"code":"preview_too_large", "message":"Preview and dependency bytes exceed the configured limit."})
            digest = "sha256:" + hashlib.sha256(dep_data).hexdigest()
            dependency_assets[dependency.path] = PreviewAsset(data=dep_data, media_type=dep_type, digest=digest)
            dependency_specs[dependency.path] = (dependency.resource_ref, dep_revision, digest, False)
        revision_digest = "sha256:" + hashlib.sha256(html).hexdigest()
        all_specs = {"": (body.resource_ref, revision, revision_digest, True), **dependency_specs}
        try:
            session = service.create(
                owner_subject_id=context.owner_subject_id,
                workspace_id=scope_workspace,
                resource_key=str(resource.get("resource_key") or ""),
                revision=revision,
                html=html,
                app_origin=app_origin(request),
                dependencies=dependency_assets,
                client_reported_warnings=tuple(body.dependency_warnings),
                validate_current=build_validator(
                    request=request, session_token=session_token, files=files, context=context, resources=all_specs,
                    event_loop=asyncio.get_running_loop(),
                ),
                client_id=body.client_id, client_generation=body.client_generation,
            )
        except BrowserPreviewError as exc:
            raise HTTPException(exc.status_code, detail={"code":exc.code, "message":str(exc)}) from exc
        return {"version":1, **session}

    @router.post("/cancel")
    async def cancel_preview_operation(body: CancelPreviewRequest, request: Request):
        try:
            owner_id, _username, _admin = principal_resolver(request, repository=repository)
        except HTTPException:
            raise
        token = str(request.cookies.get(session_cookie) or "")
        if not session_is_current(request, str(owner_id), token):
            raise HTTPException(401, detail={"code":"authentication_required", "message":"A current authenticated Files owner session is required."})
        accepted = service.cancel_client_generation(
            owner_subject_id=str(owner_id), client_id=body.client_id,
            generation=body.client_generation,
        )
        return {"version":1, "cancelled":bool(accepted)}

    @router.post("/{session_id}/export-source-bundle")
    async def export_source_bundle(session_id: str, request: Request):
        owner_id, _username, _admin = principal_resolver(request, repository=repository)
        owner = str(owner_id)
        token = str(request.cookies.get(session_cookie) or "")
        if not session_is_current(request, owner, token):
            raise HTTPException(401, detail={"code":"authentication_required", "message":"A current authenticated Files owner session is required."})
        try:
            bundle, lease = await asyncio.to_thread(service.export_source_bundle, session_id=session_id, owner_subject_id=owner)
        except BrowserPreviewError as exc:
            raise HTTPException(exc.status_code, detail={"code":exc.code, "message":str(exc)}) from exc
        current_owner, _username, _admin = principal_resolver(request, repository=repository)
        if (str(current_owner) != owner or not session_is_current(request, owner, token)
                or service.origin.current_lease(session_id=session_id, owner_subject_id=owner) is not lease):
            raise HTTPException(409, detail={"code":"preview_stale", "message":"The preview or authenticated session changed."})
        return Response(content=bundle, media_type="application/zip", headers={
            "Content-Disposition": 'attachment; filename="open-clank-source-bundle.zip"',
            "Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff",
            "X-OpenClank-Bundle": "declared_source_bundle", "X-OpenClank-Coverage": "partial_static",
        })

    @router.get("/{session_id}/status")
    async def preview_status(session_id: str, request: Request):
        try:
            owner_id, _username, _admin = principal_resolver(request, repository=repository)
        except HTTPException:
            raise
        token = str(request.cookies.get(session_cookie) or "")
        if not session_is_current(request, str(owner_id), token):
            raise HTTPException(401, detail={"code":"authentication_required", "message":"A current authenticated Files owner session is required."})
        status = await asyncio.to_thread(
            service.check_status, session_id=session_id, owner_subject_id=str(owner_id),
        )
        if status is None:
            raise HTTPException(404, detail={"code":"preview_unavailable", "message":"Browser preview session is unavailable."})
        return {"version":1, **status}

    @router.delete("/{session_id}")
    async def revoke_preview(session_id: str, request: Request):
        try:
            owner_id, _username, _admin = principal_resolver(request, repository=repository)
        except HTTPException:
            raise
        if not service.revoke(session_id=session_id, owner_subject_id=str(owner_id)):
            raise HTTPException(404, detail={"code":"preview_unavailable", "message":"Browser preview session is unavailable."})
        return {"version":1, "revoked":True}

    return router


def _safe_dependency_path_for_route(dependencies: list[DependencyRequest]) -> bool:
    seen: set[str] = set()
    for item in dependencies:
        value = str(item.path or "")
        if value.startswith("/") or "\\" in value or "%" in value or "?" in value or "#" in value:
            return False
        parts = value.split("/")
        if any(part in {"", ".", ".."} for part in parts) or value in seen:
            return False
        seen.add(value)
    return True


async def _read_provider_content(source: Any, *, max_bytes: int) -> bytes:
    sources = sum(value is not None for value in (getattr(source, "data", None), getattr(source, "path", None), getattr(source, "stream", None)))
    if sources != 1:
        raise HTTPException(503, detail={"code":"provider_unavailable", "message":"Files returned an invalid preview source."})
    if source.data is not None:
        data = bytes(source.data)
        if len(data) > max_bytes:
            raise HTTPException(413, detail={"code":"preview_too_large", "message":"Preview exceeds its byte limit."})
        return data
    if source.path is not None:
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        descriptor = None
        try:
            descriptor = os.open(source.path, flags)
            current = os.fstat(descriptor)
            identity = (int(current.st_dev), int(current.st_ino), int(current.st_size), int(current.st_mtime_ns))
            if not stat.S_ISREG(current.st_mode) or (source.expected_identity and identity != tuple(source.expected_identity)):
                raise HTTPException(409, detail={"code":"resource_changed", "message":"Preview resource changed before it could be read."})
            if current.st_size > max_bytes:
                raise HTTPException(413, detail={"code":"preview_too_large", "message":"Preview exceeds its byte limit."})
            with os.fdopen(descriptor, "rb", closefd=True) as handle:
                descriptor = None
                data = handle.read(max_bytes + 1)
            if len(data) > max_bytes:
                raise HTTPException(413, detail={"code":"preview_too_large", "message":"Preview exceeds its byte limit."})
            return data
        except OSError as exc:
            raise HTTPException(404, detail={"code":"resource_unavailable", "message":"Files preview resource is unavailable."}) from exc
        finally:
            if descriptor is not None:
                os.close(descriptor)
    size = getattr(source, "size", None)
    stream = source.stream
    if type(size) is not int or size < 0 or size > max_bytes:
        raise HTTPException(413, detail={"code":"preview_too_large", "message":"Files preview stream size is unavailable or exceeds its byte limit."})
    pieces: list[bytes] = []
    total = 0
    try:
        async for chunk in stream(0, size):
            data = bytes(chunk)
            total += len(data)
            if total > max_bytes:
                raise HTTPException(413, detail={"code":"preview_too_large", "message":"Preview exceeds its byte limit."})
            pieces.append(data)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(503, detail={"code":"provider_unavailable", "message":"Files preview stream failed."}) from exc
    if total != size:
        raise HTTPException(409, detail={"code":"resource_changed", "message":"Files preview stream length changed."})
    return b"".join(pieces)


def _normalized_revision(resource: Mapping[str, Any], data: bytes) -> dict[str, str]:
    revision = resource.get("revision")
    if isinstance(revision, Mapping) and revision.get("kind") and revision.get("value") is not None:
        return {"kind":str(revision["kind"]), "value":str(revision["value"])}
    digest = hashlib.sha256(data).hexdigest()
    return {"kind":"contentDigest", "value":digest}


__all__ = ["setup_browser_preview_routes"]
