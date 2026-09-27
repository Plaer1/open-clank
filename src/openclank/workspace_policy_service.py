"""Canonical Workspace binding shared by path pickers and Files ResourceRefs.

The browser may suggest a path or present an opaque ResourceRef, but neither is
authority.  This module records a Workspace only after the canonical policy
resolver confirms that the current principal already has the requested App or
Agent read scope.  Selecting a Workspace therefore only narrows existing access.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from src.openclank.file_policy import FilePolicyError, FilePolicyRepository, Workspace


WorkspacePurpose = Literal["app_folder", "agent_workspace"]


class WorkspacePolicyServiceError(ValueError):
    def __init__(self, message: str, *, code: str = "workspace_unavailable") -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class WorkspaceBinding:
    workspace: Workspace
    path: str


def resolve_owned_workspace(
    repository: FilePolicyRepository,
    *,
    workspace_id: str,
    owner_username: str,
    auth_manager: object,
    purpose: WorkspacePurpose,
) -> WorkspaceBinding:
    """Resolve a Workspace for an already authenticated effective owner.

    Ownership is compared by immutable account ID even for administrators.
    Admin host visibility does not make another person's saved Workspace a
    valid chat scope.
    """

    identifier = str(workspace_id or "").strip()
    owner = str(owner_username or "").strip().lower()
    account_id = getattr(auth_manager, "account_id", None)
    admin_check = getattr(auth_manager, "is_admin", None)
    subject_id = account_id(owner) if callable(account_id) and owner else None
    if not identifier or not subject_id:
        raise WorkspacePolicyServiceError(
            "Workspace is unavailable",
            code="workspace_unavailable",
        )
    try:
        workspace = repository.get_workspace(identifier)
    except FilePolicyError as error:
        raise WorkspacePolicyServiceError(
            "Workspace is unavailable",
            code="workspace_unavailable",
        ) from error
    if workspace.archived or workspace.owner_subject_id != str(subject_id):
        raise WorkspacePolicyServiceError(
            "Workspace is unavailable",
            code="workspace_unavailable",
        )
    return resolve_workspace_binding(
        repository,
        workspace,
        subject_id=str(subject_id),
        is_admin=bool(callable(admin_check) and admin_check(owner)),
        purpose=purpose,
    )


def _relative_folder(location_path: str, target: Path) -> str | None:
    try:
        relative = target.relative_to(Path(location_path))
    except (ValueError, OSError):
        return None
    return "" if str(relative) == "." else relative.as_posix()


def _platform_identity(metadata: os.stat_result) -> dict[str, object | None]:
    return {
        "device": getattr(metadata, "st_dev", None),
        "inode": getattr(metadata, "st_ino", None),
        "volume_id": str(getattr(metadata, "st_dev", "")) or None,
        "file_id": None,
        "case_sensitive": os.name != "nt",
    }


def resolve_workspace_binding(
    repository: FilePolicyRepository,
    workspace: Workspace,
    *,
    subject_id: str,
    is_admin: bool,
    purpose: WorkspacePurpose,
) -> WorkspaceBinding:
    """Resolve one stable Workspace through the current policy generation."""

    try:
        location = repository.get_location(workspace.location_id)
    except FilePolicyError as error:
        raise WorkspacePolicyServiceError(
            "Workspace folder is unavailable",
            code="workspace_unavailable",
        ) from error
    decision = repository.resolve(
        subject_id=subject_id,
        is_admin=is_admin,
        origin="agent" if purpose == "agent_workspace" else "app",
        location_id=location.id,
        capability="read",
        workspace_id=workspace.id,
        relative_resource=workspace.relative_folder,
    )
    if not decision.allowed:
        raise WorkspacePolicyServiceError(
            "That workspace is not available for this purpose",
            code="workspace_denied",
        )
    target = Path(location.canonical_path)
    if workspace.relative_folder:
        target = target.joinpath(*workspace.relative_folder.split("/"))
    try:
        if not target.is_dir():
            raise WorkspacePolicyServiceError(
                "Workspace folder is unavailable",
                code="workspace_missing",
            )
    except OSError as error:
        raise WorkspacePolicyServiceError(
            "Workspace folder is unavailable",
            code="workspace_missing",
        ) from error
    return WorkspaceBinding(workspace=workspace, path=str(target))


def bind_workspace_path(
    repository: FilePolicyRepository,
    *,
    actor_subject_id: str,
    owner_subject_id: str,
    is_admin: bool,
    path: str,
    purpose: WorkspacePurpose,
    name: str | None = None,
    media_adoption: object | None = None,
    media_receipt: object | None = None,
) -> WorkspaceBinding:
    """Create/reuse a Workspace without granting any new Agent/People access."""

    target = Path(path).expanduser().resolve(strict=False)
    try:
        if not target.is_dir():
            raise WorkspacePolicyServiceError(
                "Workspace must be a directory",
                code="not_directory",
            )
        metadata = target.stat()
    except WorkspacePolicyServiceError:
        raise
    except (FileNotFoundError, PermissionError, OSError) as error:
        raise WorkspacePolicyServiceError(
            "The OS cannot access that workspace",
            code="workspace_unavailable",
        ) from error

    candidates: list[tuple[int, object, str]] = []
    for location in repository.list_locations():
        if location.kind == "exact_file":
            continue
        relative = _relative_folder(location.canonical_path, target)
        if relative is not None:
            candidates.append((len(Path(location.canonical_path).parts), location, relative))

    if candidates:
        _depth, location, relative = max(candidates, key=lambda item: item[0])
    elif is_admin and purpose == "app_folder":
        # Cataloging a physical App folder creates no People or Agent binding.
        # Admin App access remains the existing host lane; Agent use still fails
        # below unless an explicit Agent binding already exists.
        try:
            location = repository.create_location(
                actor_subject_id=actor_subject_id,
                path=str(target),
                kind="directory",
                capabilities=("read", "write"),
                display_path=path,
                platform_identity=_platform_identity(metadata),
            )
        except FilePolicyError as error:
            raise WorkspacePolicyServiceError(str(error), code=error.code) from error
        relative = ""
    else:
        raise WorkspacePolicyServiceError(
            "Register this Location and grant the required access first",
            code="workspace_denied",
        )

    decision = repository.resolve(
        subject_id=owner_subject_id,
        is_admin=is_admin,
        origin="agent" if purpose == "agent_workspace" else "app",
        location_id=location.id,
        capability="read",
        relative_resource=relative,
    )
    if not decision.allowed:
        raise WorkspacePolicyServiceError(
            "That Location is not available for this purpose",
            code="workspace_denied",
        )
    try:
        workspace = repository.create_workspace(
            actor_subject_id=actor_subject_id,
            owner_subject_id=owner_subject_id,
            location_id=location.id,
            name=str(name or target.name or "Workspace"),
            relative_folder=relative,
        )
    except FilePolicyError as error:
        raise WorkspacePolicyServiceError(str(error), code=error.code) from error
    binding = resolve_workspace_binding(
        repository,
        workspace,
        subject_id=owner_subject_id,
        is_admin=is_admin,
        purpose=purpose,
    )
    # Workspace creation adopts only provenance-backed loose media into the new
    # root's mirror.  Adoption is best-effort and never blocks the workspace
    # itself; conflicts are returned to the caller through the recorded receipt.
    if callable(media_adoption):
        try:
            adoption_result = media_adoption(
                workspace_root=binding.path,
                workspace_id=workspace.id,
                owner_subject_id=owner_subject_id,
            )
        except WorkspacePolicyServiceError:
            raise
        except Exception as error:
            # Adoption owns durable preflight and journal capture.  A failed
            # journal must be visible to the caller; reporting a successful
            # workspace creation would otherwise imply that media is safe.
            raise WorkspacePolicyServiceError(
                "Workspace media adoption could not be durably recorded",
                code="media_adoption_failed",
            ) from error
        if callable(media_receipt) and isinstance(adoption_result, dict) and adoption_result.get("status") == "complete":
            try:
                media_receipt(adoption_result)
            except Exception as error:
                raise WorkspacePolicyServiceError(
                    "Workspace media receipt could not be durably recorded",
                    code="media_receipt_failed",
                ) from error
    return binding


def workspace_view(binding: WorkspaceBinding, *, include_path: bool = True) -> dict[str, object]:
    workspace = binding.workspace
    result: dict[str, object] = {
        "id": workspace.id,
        "name": workspace.name,
        "location_id": workspace.location_id,
        "relative_folder": workspace.relative_folder,
        "archived": workspace.archived,
        "generation": workspace.generation,
        "revision": workspace.revision,
    }
    if include_path:
        result["path"] = binding.path
    return result


__all__ = [
    "WorkspaceBinding",
    "WorkspacePolicyServiceError",
    "WorkspacePurpose",
    "bind_workspace_path",
    "resolve_owned_workspace",
    "resolve_workspace_binding",
    "workspace_view",
]
