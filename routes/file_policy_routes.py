"""Authenticated canonical file-policy state and scoped reset API."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from src.auth_helpers import get_current_user
from src.openclank.file_policy import FilePolicyError, FilePolicyRepository
from src.openclank.files_service_client import close_all_clients
from src.openclank.filesystem_registry import FilesystemRegistryError, FilesystemRootRegistry
from src.openclank.media_attachment_targets import adopt_loose_media_for_workspace
from src.openclank.history_capture import trusted_tool_context
from src.openclank.permission_grants import GrantStore
from src.openclank.workspace_policy_service import (
    WorkspacePolicyServiceError,
    bind_workspace_path,
    resolve_workspace_binding,
    workspace_view,
)


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ResetRequest(_StrictModel):
    scope: Literal["chat", "workspace", "location", "all_agent"]
    chat_id: str | None = Field(default=None, min_length=1, max_length=256)
    workspace_id: str | None = Field(default=None, min_length=1, max_length=256)
    location_id: str | None = Field(default=None, min_length=1, max_length=256)


class CreateLocationRequest(_StrictModel):
    path: str = Field(min_length=1, max_length=4096)
    kind: Literal["exact_file", "directory", "whole_root"]
    capabilities: list[Literal["read", "write"]] = Field(min_length=1, max_length=2)
    agent_access: bool = True


class PeopleAccessRequest(_StrictModel):
    subject_username: str = Field(min_length=1, max_length=128)
    location_id: str = Field(min_length=1, max_length=256)
    capabilities: list[Literal["read", "write"]] = Field(min_length=1, max_length=2)


class UpdatePeopleAccessRequest(_StrictModel):
    capabilities: list[Literal["read", "write"]] | None = Field(default=None, min_length=1, max_length=2)
    enabled: bool | None = None


class AgentAccessRequest(_StrictModel):
    location_id: str = Field(min_length=1, max_length=256)
    capabilities: list[Literal["read", "write"]] = Field(min_length=1, max_length=2)


class UpdateAgentAccessRequest(_StrictModel):
    capabilities: list[Literal["read", "write"]] | None = Field(default=None, min_length=1, max_length=2)
    enabled: bool | None = None


class WorkspacePathRequest(_StrictModel):
    path: str = Field(min_length=1, max_length=4096)
    purpose: Literal["app_folder", "agent_workspace"]
    name: str | None = Field(default=None, min_length=1, max_length=200)


def setup_file_policy_routes(
    *,
    repository: FilePolicyRepository | None = None,
    filesystem_registry: FilesystemRootRegistry | None = None,
) -> APIRouter:
    router = APIRouter(prefix="/api/file-policy", tags=["file-policy"])
    policy = repository or FilePolicyRepository()
    legacy_registry = filesystem_registry or FilesystemRootRegistry()
    visibility_projection_owner = "__openclank_app_visibility__"

    def principal(request: Request) -> tuple[str, str, bool]:
        username = str(get_current_user(request) or "").strip().lower()
        if not username:
            raise HTTPException(401, "Authentication required")
        auth_manager = getattr(getattr(request.app, "state", None), "auth_manager", None)
        subject_id = auth_manager.account_id(username) if auth_manager and hasattr(auth_manager, "account_id") else None
        if not subject_id:
            raise HTTPException(503, "Immutable account identity is unavailable")
        return str(subject_id), username, bool(auth_manager and auth_manager.is_admin(username))

    def location_view(location) -> dict:
        return {
            "id": location.id,
            "kind": location.kind,
            "display_path": location.display_path,
            "canonical_path": location.canonical_path,
            "capabilities": list(location.capabilities),
            "availability": location.availability,
            "enabled": location.enabled,
            "generation": location.generation,
            "revision": location.revision,
        }

    def binding_view(binding) -> dict:
        return {
            "id": binding.id,
            "binding_class": binding.binding_class,
            "subject_kind": binding.subject_kind,
            "subject_id": binding.subject_id,
            "location_id": binding.location_id,
            "workspace_id": binding.workspace_id,
            "chat_id": binding.chat_id,
            "operation": binding.operation,
            "capabilities": list(binding.capabilities),
            "lifetime": binding.lifetime,
            "status": binding.status,
            "remaining_uses": binding.remaining_uses,
            "expires_unix_ms": binding.expires_unix_ms,
            "generation": binding.generation,
            "revision": binding.revision,
            "imported": bool(binding.migration_source),
        }

    @router.get("/state")
    async def state(request: Request, include_inactive: bool = False):
        subject_id, _username, is_admin = principal(request)
        bindings = policy.list_bindings(
            subject_id=None if is_admin else subject_id,
            include_inactive=bool(include_inactive),
        )
        if is_admin:
            locations = policy.list_locations(include_disabled=bool(include_inactive))
            workspaces = policy.list_workspaces(include_archived=bool(include_inactive))
        else:
            location_ids = {binding.location_id for binding in bindings if binding.location_id}
            locations = []
            for location_id in sorted(location_ids):
                try:
                    locations.append(policy.get_location(location_id))
                except FilePolicyError as error:
                    if error.code != "location_not_found":
                        raise
            workspaces = policy.list_workspaces(
                owner_subject_id=subject_id,
                include_archived=bool(include_inactive),
            )
        return {
            "version": 1,
            "generation": policy.generation(),
            "subject_id": subject_id,
            "is_admin": is_admin,
            "reset_scopes": ["chat", "workspace", "location", "all_agent"],
            "locations": [location_view(location) for location in locations],
            "workspaces": [
                {
                    "id": workspace.id,
                    "name": workspace.name,
                    "location_id": workspace.location_id,
                    "relative_folder": workspace.relative_folder,
                    "archived": workspace.archived,
                    "generation": workspace.generation,
                    "revision": workspace.revision,
                }
                for workspace in workspaces
            ],
            "bindings": [binding_view(binding) for binding in bindings],
        }

    @router.get("/location-presets")
    async def location_presets(request: Request):
        """Return admin-only host presets; final creation still revalidates."""

        _subject_id, _username, is_admin = principal(request)
        if not is_admin:
            raise HTTPException(403, "Only an administrator can inspect host Location presets")
        home = str(Path.home().expanduser().resolve(strict=False))
        roots: list[dict[str, str]] = []
        if os.name == "nt":
            list_drives = getattr(os, "listdrives", None)
            drives = list_drives() if callable(list_drives) else [
                f"{letter}:\\" for letter in "ABCDEFGHIJKLMNOPQRSTUVWXYZ" if Path(f"{letter}:\\").is_dir()
            ]
            roots.extend({"name": str(drive), "path": str(Path(drive).resolve(strict=False))} for drive in drives)
        else:
            roots.append({"name": "Whole disk /", "path": "/"})
            if Path("/Volumes").is_dir():
                try:
                    roots.extend(
                        {"name": volume.name, "path": str(volume.resolve(strict=False))}
                        for volume in sorted(Path("/Volumes").iterdir(), key=lambda item: item.name.casefold())
                        if volume.is_dir()
                    )
                except OSError:
                    pass
        return {
            "version": 1,
            "home": home,
            "whole_roots": roots,
            "os_managed": True,
        }

    def reset_args(body: ResetRequest) -> dict:
        return {
            "scope": body.scope,
            "chat_id": body.chat_id,
            "workspace_id": body.workspace_id,
            "location_id": body.location_id,
        }

    def compatibility_grant_store(request: Request, username: str) -> GrantStore:
        supervisor = getattr(
            getattr(request.app, "state", None), "mimo_supervisor", None
        )
        store = (
            supervisor.grant_store_for(username)
            if supervisor and hasattr(supervisor, "grant_store_for")
            else None
        )
        return store or GrantStore(str(policy.db_path))

    def compatibility_reset_args(
        body: ResetRequest,
        *,
        subject_id: str,
        username: str,
        is_admin: bool,
    ) -> dict:
        result = {
            "owner": username,
            "scope": body.scope,
            "chat_id": str(body.chat_id or ""),
            "workspace_id": str(body.workspace_id or ""),
            "legacy_workspace": "",
            "location_workspace_ids": (),
            "legacy_location_path": "",
        }
        if body.scope == "workspace":
            try:
                workspace = policy.get_workspace(str(body.workspace_id or ""))
            except FilePolicyError as error:
                raise_policy(error)
            if workspace.owner_subject_id != subject_id:
                raise HTTPException(404, detail={
                    "code": "workspace_not_found",
                    "message": "Workspace was not found",
                })
            try:
                location = policy.get_location(workspace.location_id)
            except FilePolicyError:
                # Stable revocation remains valid after Location removal; the
                # old raw-path compatibility dimension simply has no mapping.
                return result
            result["legacy_workspace"] = os.path.join(
                location.canonical_path,
                *(
                    workspace.relative_folder.split("/")
                    if workspace.relative_folder
                    else ()
                ),
            )
        elif body.scope == "location":
            try:
                location = policy.get_location(str(body.location_id or ""))
            except FilePolicyError as error:
                raise_policy(error)
            if not is_admin and not any(
                binding.location_id == location.id
                for binding in policy.list_bindings(
                    subject_id=subject_id,
                    include_inactive=True,
                )
            ):
                raise HTTPException(404, detail={
                    "code": "location_not_found",
                    "message": "Location was not found",
                })
            result["legacy_location_path"] = location.canonical_path
            result["location_workspace_ids"] = tuple(
                workspace.id
                for workspace in policy.list_workspaces(
                    owner_subject_id=subject_id,
                    include_archived=True,
                )
                if workspace.location_id == location.id
            )
        return result

    def raise_policy(error: FilePolicyError) -> None:
        status = 404 if error.code.endswith("_not_found") else 400
        raise HTTPException(status, detail={"code": error.code, "message": str(error)}) from error

    def raise_workspace(error: WorkspacePolicyServiceError, *, resolving: bool = False) -> None:
        if error.code == "workspace_denied":
            status = 403
        elif resolving or error.code == "workspace_missing":
            status = 404
        else:
            status = 400
        raise HTTPException(
            status,
            detail={"code": error.code, "message": str(error)},
        ) from error

    def find_projection_root(location):
        legacy_kind = "exact_file" if location.kind == "exact_file" else "recursive_directory"
        return next((
            item for item in legacy_registry.list(visibility_projection_owner)
            if item.get("kind") == legacy_kind and item.get("canonical_path") == location.canonical_path
        ), None)

    def projection_root(location):
        legacy_kind = "exact_file" if location.kind == "exact_file" else "recursive_directory"
        root = find_projection_root(location)
        try:
            if root is None:
                return legacy_registry.add(
                    visibility_projection_owner,
                    location.canonical_path,
                    legacy_kind,
                    list(location.capabilities),
                    display_path=location.display_path,
                )
            return legacy_registry.update(
                visibility_projection_owner,
                str(root["id"]),
                enabled=True,
                capabilities=list(location.capabilities),
            )
        except FilesystemRegistryError as error:
            raise HTTPException(
                503,
                detail={"code": "compatibility_projection_failed", "message": "People access was saved, but the current Rust projection could not be refreshed"},
            ) from error

    def projection_assignment(subject_username: str, root_id: str):
        return next((
            item for item in legacy_registry.list_visibility()
            if item.get("subject_kind") == "user"
            and item.get("subject_id") == subject_username
            and item.get("root_id") == root_id
        ), None)

    def project_people_state(
        *,
        location_id: str,
        subject_username: str,
        capabilities: list[str] | tuple[str, ...],
        active: bool,
    ):
        location = policy.get_location(str(location_id))
        root = projection_root(location)
        assignment = projection_assignment(subject_username, str(root["id"]))
        try:
            if assignment is None:
                if not active:
                    return None
                return legacy_registry.assign_visibility(
                    visibility_projection_owner,
                    subject_username,
                    str(root["id"]),
                    list(capabilities),
                )
            return legacy_registry.update_visibility(
                visibility_projection_owner,
                str(assignment["id"]),
                enabled=active,
                capabilities=list(capabilities),
            )
        except FilesystemRegistryError as error:
            raise HTTPException(
                503,
                detail={"code": "compatibility_projection_failed", "message": "People access was saved, but the current Rust assignment could not be refreshed"},
            ) from error

    def project_people_binding(binding, subject_username: str):
        return project_people_state(
            location_id=str(binding.location_id),
            subject_username=subject_username,
            capabilities=list(binding.capabilities),
            active=binding.status == "active",
        )

    def update_people_binding_safely(
        *,
        actor_id: str,
        binding,
        subject_username: str,
        capabilities: list[str] | tuple[str, ...] | None,
        enabled: bool | None,
    ):
        """Update two stores without ever leaving runtime broader than policy."""
        current_caps = set(binding.capabilities)
        desired_caps = set(capabilities) if capabilities is not None else set(current_caps)
        current_active = binding.status == "active"
        desired_active = bool(enabled) if enabled is not None else current_active
        desired_list = sorted(desired_caps)

        if not desired_active:
            # Revoke the live projection before canonical authority.
            project_people_state(
                location_id=str(binding.location_id),
                subject_username=subject_username,
                capabilities=desired_list,
                active=False,
            )
            return policy.update_binding(
                binding.id,
                actor_subject_id=actor_id,
                capabilities=desired_list,
                enabled=False,
            )

        if not current_active or current_caps.issubset(desired_caps):
            # A widening first becomes canonical; projection failure stays narrow.
            updated = policy.update_binding(
                binding.id,
                actor_subject_id=actor_id,
                capabilities=desired_list,
                enabled=True,
            )
            project_people_binding(updated, subject_username)
            return updated

        if desired_caps.issubset(current_caps):
            # A narrowing reaches the runtime before canonical authority.
            project_people_state(
                location_id=str(binding.location_id),
                subject_username=subject_username,
                capabilities=desired_list,
                active=True,
            )
            return policy.update_binding(
                binding.id,
                actor_subject_id=actor_id,
                capabilities=desired_list,
                enabled=True,
            )

        # Incomparable capability sets are transitioned through disabled so a
        # failure cannot leave either the old or new live projection too broad.
        project_people_state(
            location_id=str(binding.location_id),
            subject_username=subject_username,
            capabilities=sorted(current_caps),
            active=False,
        )
        updated = policy.update_binding(
            binding.id,
            actor_subject_id=actor_id,
            capabilities=desired_list,
            enabled=True,
        )
        project_people_binding(updated, subject_username)
        return updated

    def find_agent_projection(subject_username: str, location):
        legacy_kind = "exact_file" if location.kind == "exact_file" else "recursive_directory"
        return next((
            item for item in legacy_registry.list(subject_username)
            if item.get("kind") == legacy_kind
            and item.get("canonical_path") == location.canonical_path
        ), None)

    def project_agent_state(
        *,
        location_id: str,
        subject_username: str,
        capabilities: list[str] | tuple[str, ...],
        active: bool,
    ):
        location = policy.get_location(str(location_id))
        legacy_kind = "exact_file" if location.kind == "exact_file" else "recursive_directory"
        root = find_agent_projection(subject_username, location)
        try:
            if root is None:
                if not active:
                    return None
                return legacy_registry.add(
                    subject_username,
                    location.canonical_path,
                    legacy_kind,
                    list(capabilities),
                    display_path=location.display_path,
                )
            return legacy_registry.update(
                subject_username,
                str(root["id"]),
                enabled=active,
                capabilities=list(capabilities),
            )
        except FilesystemRegistryError as error:
            raise HTTPException(
                503,
                detail={
                    "code": "compatibility_projection_failed",
                    "message": "Agent access was saved, but the current Rust projection could not be refreshed",
                },
            ) from error

    def project_agent_binding(binding, subject_username: str):
        return project_agent_state(
            location_id=str(binding.location_id),
            subject_username=subject_username,
            capabilities=list(binding.capabilities),
            active=binding.status == "active",
        )

    def update_agent_binding_safely(
        *,
        actor_id: str,
        binding,
        subject_username: str,
        capabilities: list[str] | tuple[str, ...] | None,
        enabled: bool | None,
    ):
        current_caps = set(binding.capabilities)
        desired_caps = set(capabilities) if capabilities is not None else set(current_caps)
        current_active = binding.status == "active"
        desired_active = bool(enabled) if enabled is not None else current_active
        desired_list = sorted(desired_caps)

        if not desired_active:
            project_agent_state(
                location_id=str(binding.location_id),
                subject_username=subject_username,
                capabilities=desired_list,
                active=False,
            )
            return policy.update_binding(
                binding.id,
                actor_subject_id=actor_id,
                capabilities=desired_list,
                enabled=False,
            )
        if not current_active or current_caps.issubset(desired_caps):
            updated = policy.update_binding(
                binding.id,
                actor_subject_id=actor_id,
                capabilities=desired_list,
                enabled=True,
            )
            project_agent_binding(updated, subject_username)
            return updated
        if desired_caps.issubset(current_caps):
            project_agent_state(
                location_id=str(binding.location_id),
                subject_username=subject_username,
                capabilities=desired_list,
                active=True,
            )
            return policy.update_binding(
                binding.id,
                actor_subject_id=actor_id,
                capabilities=desired_list,
                enabled=True,
            )
        project_agent_state(
            location_id=str(binding.location_id),
            subject_username=subject_username,
            capabilities=sorted(current_caps),
            active=False,
        )
        updated = policy.update_binding(
            binding.id,
            actor_subject_id=actor_id,
            capabilities=desired_list,
            enabled=True,
        )
        project_agent_binding(updated, subject_username)
        return updated

    def validate_agent_ceiling(subject_id: str, is_admin: bool, location_id: str, capabilities: set[str]):
        location = policy.get_location(location_id)
        ceiling = set(location.capabilities)
        if not is_admin:
            people = [
                binding for binding in policy.list_bindings(subject_id=subject_id, binding_class="people")
                if binding.location_id == location.id and binding.status == "active"
            ]
            ceiling &= set().union(*(set(binding.capabilities) for binding in people)) if people else set()
        if not capabilities or not capabilities.issubset(ceiling):
            raise HTTPException(
                403,
                detail={"code": "capability_escalation", "message": "Agent access must stay inside current People/Location access"},
            )
        return location

    @router.post("/locations")
    async def create_location(body: CreateLocationRequest, request: Request):
        subject_id, username, is_admin = principal(request)
        if not is_admin:
            raise HTTPException(403, "Only an administrator can register a host location")
        canonical = Path(body.path).expanduser().resolve(strict=False)
        try:
            metadata = canonical.stat()
        except (FileNotFoundError, PermissionError, OSError) as error:
            raise HTTPException(400, detail={"code": "location_unavailable", "message": "The OS cannot access that location"}) from error
        if body.kind == "exact_file" and not canonical.is_file():
            raise HTTPException(400, detail={"code": "not_file", "message": "The location is not a regular file"})
        if body.kind in {"directory", "whole_root"} and not canonical.is_dir():
            raise HTTPException(400, detail={"code": "not_directory", "message": "The location is not a directory"})
        if body.kind == "whole_root" and canonical != Path(canonical.anchor):
            raise HTTPException(400, detail={"code": "invalid_whole_root", "message": "Whole disk must name an OS filesystem root"})
        capabilities = tuple(sorted(set(body.capabilities)))
        platform_identity = {
            "device": getattr(metadata, "st_dev", None),
            "inode": getattr(metadata, "st_ino", None),
            "volume_id": str(getattr(metadata, "st_dev", "")) or None,
            "file_id": None,
            "case_sensitive": os.name != "nt",
        }
        try:
            location = policy.create_location(
                actor_subject_id=subject_id,
                path=str(canonical),
                kind=body.kind,
                capabilities=capabilities,
                display_path=body.path,
                platform_identity=platform_identity,
            )
            if not location.enabled:
                # Re-adding revives physical identity only.  The prior People,
                # Agent, Operation, and Workspace rows were revoked/archived by
                # removal and are deliberately not resurrected.
                location = policy.restore_location(
                    location.id,
                    actor_subject_id=subject_id,
                    capabilities=capabilities,
                    display_path=body.path,
                    platform_identity=platform_identity,
                )
        except FilePolicyError as error:
            raise_policy(error)

        binding = None
        legacy_root = None
        if body.agent_access:
            binding = next((
                item for item in policy.list_bindings(subject_id=subject_id, binding_class="agent")
                if item.location_id == location.id and item.lifetime == "always" and item.status == "active"
            ), None)
            if binding is None:
                try:
                    binding = policy.create_binding(
                        actor_subject_id=subject_id,
                        binding_class="agent",
                        subject_id=subject_id,
                        location_id=location.id,
                        capabilities=capabilities,
                        lifetime="always",
                    )
                except FilePolicyError as error:
                    raise_policy(error)

            # The legacy root is a derived compatibility projection only after
            # the canonical Agent binding exists. Failure leaves the effective
            # runtime narrower than policy; it can never create an unrecorded
            # broad legacy grant.
            legacy_kind = "exact_file" if body.kind == "exact_file" else "recursive_directory"
            legacy_root = next((
                root for root in legacy_registry.list(username)
                if root.get("kind") == legacy_kind and root.get("canonical_path") == str(canonical)
            ), None)
            try:
                if legacy_root is None:
                    legacy_root = legacy_registry.add(
                        username,
                        str(canonical),
                        legacy_kind,
                        list(capabilities),
                        display_path=body.path,
                    )
                else:
                    legacy_root = legacy_registry.update(
                        username,
                        str(legacy_root["id"]),
                        enabled=True,
                        capabilities=list(capabilities),
                    )
            except FilesystemRegistryError as error:
                raise HTTPException(
                    503,
                    detail={
                        "code": "compatibility_projection_failed",
                        "message": "The Location was saved, but its current Rust projection could not be refreshed",
                    },
                ) from error
            close_all_clients()

        return {
            "version": 1,
            "generation": policy.generation(),
            "location": location_view(location),
            "agent_binding_id": binding.id if binding else None,
            "compatibility_projected": bool(legacy_root),
            "os_managed": body.kind == "whole_root",
        }

    @router.delete("/locations/{location_id}")
    async def remove_location(location_id: str, request: Request):
        actor_id, _username, is_admin = principal(request)
        if not is_admin:
            raise HTTPException(403, "Only an administrator can remove a host location")
        try:
            location = policy.get_location(location_id)
        except FilePolicyError as error:
            raise_policy(error)

        # Revoke the currently consumed JSON/Rust projection before canonical
        # policy. If canonical persistence then fails, runtime remains narrower
        # rather than retaining an unrecorded grant.
        try:
            projected = legacy_registry.disable_projection(location.canonical_path, location.kind)
        except FilesystemRegistryError as error:
            raise HTTPException(
                503,
                detail={
                    "code": "compatibility_projection_failed",
                    "message": "The current Rust projection could not be revoked",
                },
            ) from error
        try:
            result = policy.disable_location(
                location.id,
                actor_subject_id=actor_id,
                reason_code="location_removed_by_admin",
            )
        except FilePolicyError as error:
            raise_policy(error)
        close_all_clients()
        return {
            "ok": True,
            "generation": result["generation"],
            "bindings_revoked": result["bindings_revoked"],
            "workspaces_archived": result["workspaces_archived"],
            "compatibility": projected,
        }

    @router.post("/workspaces/from-path")
    async def create_workspace_from_path(body: WorkspacePathRequest, request: Request):
        subject_id, _username, is_admin = principal(request)
        try:
            binding = bind_workspace_path(
                policy,
                actor_subject_id=subject_id,
                owner_subject_id=subject_id,
                is_admin=is_admin,
                path=body.path,
                purpose=body.purpose,
                name=body.name,
                media_adoption=lambda *, workspace_root, workspace_id, owner_subject_id: adopt_loose_media_for_workspace(
                    operation_store=policy,
                    owner_subject_id=owner_subject_id,
                    workspace_root=workspace_root,
                    workspace_id=workspace_id,
                    history_context=trusted_tool_context(
                        actor_id=_username,
                        account_id=subject_id,
                        workspace_id=workspace_id,
                        workspace_root=workspace_root,
                    ),
                ),
                media_receipt=getattr(getattr(request, "state", None), "history_capture", None),
            )
        except WorkspacePolicyServiceError as error:
            raise_workspace(error)
        return {
            "version": 1,
            "generation": policy.generation(),
            "workspace": workspace_view(binding),
        }

    @router.get("/workspaces/{workspace_id}/resolve")
    async def resolve_workspace(workspace_id: str, request: Request, purpose: Literal["app_folder", "agent_workspace"]):
        subject_id, _username, is_admin = principal(request)
        try:
            workspace = policy.get_workspace(workspace_id)
        except FilePolicyError as error:
            raise_policy(error)
        if workspace.owner_subject_id != subject_id:
            raise HTTPException(404, "Workspace was not found")
        try:
            binding = resolve_workspace_binding(
                policy,
                workspace,
                subject_id=subject_id,
                is_admin=is_admin,
                purpose=purpose,
            )
        except WorkspacePolicyServiceError as error:
            raise_workspace(error, resolving=True)
        return {
            "version": 1,
            "generation": policy.generation(),
            "workspace": workspace_view(binding),
        }

    @router.post("/people")
    async def create_people_access(body: PeopleAccessRequest, request: Request):
        actor_id, _username, is_admin = principal(request)
        if not is_admin:
            raise HTTPException(403, "Only an administrator can change People access")
        auth_manager = getattr(getattr(request.app, "state", None), "auth_manager", None)
        subject_username = body.subject_username.strip().lower()
        subject_id = auth_manager.account_id(subject_username) if auth_manager and hasattr(auth_manager, "account_id") else None
        if not subject_id:
            raise HTTPException(404, "Account was not found")
        try:
            location = policy.get_location(body.location_id)
            capabilities = tuple(sorted(set(body.capabilities)))
            existing = next((
                item for item in policy.list_bindings(subject_id=str(subject_id), binding_class="people", include_inactive=True)
                if item.location_id == location.id
            ), None)
            binding = update_people_binding_safely(
                actor_id=actor_id,
                binding=existing,
                subject_username=subject_username,
                capabilities=capabilities,
                enabled=True,
            ) if existing else policy.create_binding(
                actor_subject_id=actor_id,
                binding_class="people",
                subject_id=str(subject_id),
                location_id=location.id,
                capabilities=capabilities,
                lifetime="always",
            )
        except FilePolicyError as error:
            raise_policy(error)
        if not existing:
            project_people_binding(binding, subject_username)
        close_all_clients()
        return {"version": 1, "generation": policy.generation(), "binding": binding_view(binding)}

    @router.post("/agent-access")
    async def create_agent_access(body: AgentAccessRequest, request: Request):
        subject_id, username, is_admin = principal(request)
        capabilities = set(body.capabilities)
        validate_agent_ceiling(subject_id, is_admin, body.location_id, capabilities)
        existing = next((
            item for item in policy.list_bindings(
                subject_id=subject_id,
                binding_class="agent",
                include_inactive=True,
            )
            if item.location_id == body.location_id and item.lifetime == "always"
        ), None)
        try:
            if existing:
                binding = update_agent_binding_safely(
                    actor_id=subject_id,
                    binding=existing,
                    subject_username=username,
                    capabilities=sorted(capabilities),
                    enabled=True,
                )
            else:
                binding = policy.create_binding(
                    actor_subject_id=subject_id,
                    binding_class="agent",
                    subject_id=subject_id,
                    location_id=body.location_id,
                    capabilities=tuple(sorted(capabilities)),
                    lifetime="always",
                )
                project_agent_binding(binding, username)
        except FilePolicyError as error:
            raise_policy(error)
        close_all_clients()
        return {"version": 1, "generation": policy.generation(), "binding": binding_view(binding)}

    def own_agent_binding(binding_id: str, request: Request):
        subject_id, username, is_admin = principal(request)
        try:
            binding = policy.get_binding(binding_id)
        except FilePolicyError as error:
            raise_policy(error)
        if binding.binding_class != "agent" or binding.subject_id != subject_id:
            raise HTTPException(404, "Agent access was not found")
        return subject_id, username, is_admin, binding

    @router.patch("/agent-access/{binding_id}")
    async def update_agent_access(binding_id: str, body: UpdateAgentAccessRequest, request: Request):
        subject_id, username, is_admin, binding = own_agent_binding(binding_id, request)
        capabilities = set(body.capabilities or binding.capabilities)
        validate_agent_ceiling(subject_id, is_admin, str(binding.location_id), capabilities)
        try:
            updated = update_agent_binding_safely(
                actor_id=subject_id,
                binding=binding,
                subject_username=username,
                capabilities=sorted(capabilities),
                enabled=body.enabled,
            )
        except FilePolicyError as error:
            raise_policy(error)
        close_all_clients()
        return {"version": 1, "generation": policy.generation(), "binding": binding_view(updated)}

    @router.delete("/agent-access/{binding_id}")
    async def remove_agent_access(binding_id: str, request: Request):
        subject_id, username, _is_admin, binding = own_agent_binding(binding_id, request)
        project_agent_state(
            location_id=str(binding.location_id),
            subject_username=username,
            capabilities=list(binding.capabilities),
            active=False,
        )
        try:
            policy.revoke_binding(
                binding.id,
                actor_subject_id=subject_id,
                reason_code="agent_access_removed",
            )
        except FilePolicyError as error:
            raise_policy(error)
        close_all_clients()
        return {"ok": True, "generation": policy.generation()}

    def people_binding(binding_id: str, request: Request):
        actor_id, _username, is_admin = principal(request)
        if not is_admin:
            raise HTTPException(403, "Only an administrator can change People access")
        try:
            binding = policy.get_binding(binding_id)
        except FilePolicyError as error:
            raise_policy(error)
        if binding.binding_class != "people":
            raise HTTPException(404, "People access was not found")
        return actor_id, binding

    @router.patch("/people/{binding_id}")
    async def update_people_access(binding_id: str, body: UpdatePeopleAccessRequest, request: Request):
        actor_id, binding = people_binding(binding_id, request)
        auth_manager = getattr(getattr(request.app, "state", None), "auth_manager", None)
        subject_username = auth_manager.username_for_account_id(binding.subject_id) if auth_manager and hasattr(auth_manager, "username_for_account_id") else None
        if not subject_username:
            raise HTTPException(404, "Account was not found")
        try:
            updated = update_people_binding_safely(
                actor_id=actor_id,
                binding=binding,
                subject_username=subject_username,
                capabilities=body.capabilities,
                enabled=body.enabled,
            )
        except FilePolicyError as error:
            raise_policy(error)
        close_all_clients()
        return {"version": 1, "generation": policy.generation(), "binding": binding_view(updated)}

    @router.delete("/people/{binding_id}")
    async def remove_people_access(binding_id: str, request: Request):
        actor_id, binding = people_binding(binding_id, request)
        auth_manager = getattr(getattr(request.app, "state", None), "auth_manager", None)
        subject_username = auth_manager.username_for_account_id(binding.subject_id) if auth_manager and hasattr(auth_manager, "username_for_account_id") else None
        if not subject_username:
            raise HTTPException(404, "Account was not found")
        location = policy.get_location(str(binding.location_id))
        root = find_projection_root(location)
        assignment = projection_assignment(subject_username, str(root["id"])) if root else None
        if assignment:
            try:
                legacy_registry.remove_visibility(visibility_projection_owner, str(assignment["id"]))
            except FilesystemRegistryError as error:
                raise HTTPException(
                    503,
                    detail={"code": "compatibility_projection_failed", "message": "People access could not be removed from the current Rust projection"},
                ) from error
        policy.revoke_binding(binding.id, actor_subject_id=actor_id, reason_code="people_access_removed")
        close_all_clients()
        return {"ok": True, "generation": policy.generation()}

    @router.post("/resets/preview")
    async def preview_reset(body: ResetRequest, request: Request):
        subject_id, username, is_admin = principal(request)
        try:
            result = policy.preview_agent_reset(
                subject_id=subject_id,
                **reset_args(body),
            )
        except FilePolicyError as error:
            raise_policy(error)
        compatibility = compatibility_grant_store(
            request, username
        ).preview_agent_reset(**compatibility_reset_args(
            body,
            subject_id=subject_id,
            username=username,
            is_admin=is_admin,
        ))
        result["compatibility_matched"] = compatibility
        result["total_matched"] = int(result["matched"]) + compatibility
        return result

    @router.post("/resets")
    async def reset(body: ResetRequest, request: Request):
        subject_id, username, is_admin = principal(request)
        compatibility_args = compatibility_reset_args(
            body,
            subject_id=subject_id,
            username=username,
            is_admin=is_admin,
        )
        # Compatibility approval revocation happens first. If the canonical
        # mutation subsequently fails, the only possible partial state is
        # narrower authority; retrying this endpoint is idempotent.
        compatibility_revoked = compatibility_grant_store(
            request, username
        ).reset_agent_permissions(**compatibility_args)
        try:
            result = policy.reset_agent_permissions(
                actor_subject_id=subject_id,
                subject_id=subject_id,
                **reset_args(body),
            )
        except FilePolicyError as error:
            raise_policy(error)
        result["compatibility_revoked"] = compatibility_revoked
        result["total_revoked"] = int(result["matched"]) + compatibility_revoked
        task_cascade = {
            "selected": 0,
            "paused": 0,
            "runs_aborted": 0,
            "executions_cancelled": 0,
            "executing_cleared": 0,
            "chat_mapped": 0,
        }
        task_scheduler = getattr(
            getattr(request.app, "state", None), "task_scheduler", None
        )
        if task_scheduler and hasattr(task_scheduler, "reset_file_authority"):
            task_cascade = await task_scheduler.reset_file_authority(
                username,
                scope=body.scope,
                chat_id=body.chat_id,
                workspace_id=body.workspace_id,
                # Derived above from owned canonical Workspace records. Never
                # pass the compatibility raw Location path into task authority.
                location_workspace_ids=tuple(
                    compatibility_args["location_workspace_ids"]
                ),
            )
        result["task_cascade"] = task_cascade
        if result["matched"] or compatibility_revoked:
            # Compatibility Rust clients carry a generation-stamped scope and
            # must not survive a reset while the canonical projection lands.
            close_all_clients()
        pending_rejected = 0
        supervisor = getattr(getattr(request.app, "state", None), "mimo_supervisor", None)
        handler = supervisor.permission_handler_for(username) if supervisor and hasattr(supervisor, "permission_handler_for") else None
        if handler and hasattr(handler, "reject_scope"):
            if body.scope == "chat":
                pending_rejected = handler.reject_scope(session_id=str(body.chat_id or ""))
            elif body.scope == "workspace":
                pending_rejected = handler.reject_scope(
                    authority_workspace_id=str(body.workspace_id or "")
                )
            else:
                # Location/All-agent resets cover multiple Workspaces. Reject
                # every compatibility prompt for this owner rather than risk a
                # just-approved request landing after the policy generation.
                pending_rejected = handler.reject_scope(all_pending=True)
        from src.agent_tools.filesystem_tools import reject_file_approval_scope
        from src.shell_policy import reject_shell_approval_scope

        if body.scope == "chat":
            pending_rejected += reject_file_approval_scope(
                owner=username, session_id=str(body.chat_id or "")
            )
            pending_rejected += reject_shell_approval_scope(
                owner=username, session_id=str(body.chat_id or "")
            )
        elif body.scope == "workspace":
            pending_rejected += reject_file_approval_scope(
                owner=username,
                authority_workspace_id=str(body.workspace_id or ""),
            )
            pending_rejected += reject_shell_approval_scope(
                owner=username,
                authority_workspace_id=str(body.workspace_id or ""),
            )
        else:
            pending_rejected += reject_file_approval_scope(
                owner=username, all_pending=True
            )
            pending_rejected += reject_shell_approval_scope(
                owner=username, all_pending=True
            )
        result["pending_rejected"] = int(pending_rejected)
        runtime_invalidated = False
        if (
            result["matched"]
            or compatibility_revoked
            or pending_rejected
            or task_cascade["paused"]
            or task_cascade["executions_cancelled"]
        ) and supervisor and hasattr(supervisor, "invalidate_owner_projection"):
            # Stop the old child before this response announces success. The
            # next turn must materialize a current policy projection; warming a
            # replacement in parallel would leave a narrowing window.
            await supervisor.invalidate_owner_projection(username)
            runtime_invalidated = True
        result["runtime_invalidated"] = runtime_invalidated
        return result

    return router


__all__ = ["setup_file_policy_routes"]
