"""Canonical operation consent for ACP, native file and shell approvals.

Only FilePolicyRepository is consulted. Consent never grants path authority;
process confinement remains the OS boundary.
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Container, Iterable, Optional

from src.constants import APP_DB, AUTH_FILE
from src.openclank.file_policy import (
    FilePolicyError,
    FilePolicyRepository,
    Location,
)

_READ_CAPABILITIES = {"read", "read_file", "list", "ls", "glob", "grep", "search"}
_WRITE_CAPABILITIES = {
    "write",
    "write_file",
    "edit",
    "delete",
    "trash",
    "move",
    "rename",
    "manage_files",
    "apply_patch",
    "native-file-mutation",
}
_EXECUTE_CAPABILITIES = {"execute", "bash", "shell", "process", "native-shell-destructive"}


def capability_for_permission(permission_type: str) -> Optional[str]:
    """Map an approval-lane permission type onto one canonical capability."""
    value = str(permission_type or "").strip().lower()
    if value in _READ_CAPABILITIES:
        return "read"
    if value in _WRITE_CAPABILITIES:
        return "write"
    if value in _EXECUTE_CAPABILITIES:
        return "execute"
    return None


def approval_resource_ref(permission_type: str, pattern: str, resource: str = "") -> str:
    """Deterministic resource identity shared with the legacy grant migrator."""
    digest = hashlib.sha256(
        (str(permission_type) + "\0" + str(pattern) + "\0" + str(resource or "")).encode("utf-8")
    ).hexdigest()
    return f"legacy-resource-{digest}"


class ReadOnlyAuthSnapshot:
    """Minimal read-only AuthManager surface for authorization checks.

    Constructing ``AuthManager`` here would also load/prune session tokens and
    may run legacy migrations.  Approval lanes need only the current immutable
    account ID and admin bit, so reading one fresh snapshot avoids turning an
    authorization check into a second auth writer.
    """

    def __init__(self, auth_path: str) -> None:
        with open(auth_path, "r", encoding="utf-8") as handle:
            document = json.load(handle)
        users = document.get("users") if isinstance(document, dict) else None
        if not isinstance(users, dict):
            raise ValueError("authentication identity store is unavailable")
        self._users = {
            str(username).strip().lower(): record
            for username, record in users.items()
            if isinstance(record, dict)
        }

    def account_id(self, username: str) -> Optional[str]:
        record = self._users.get(str(username or "").strip().lower())
        value = str((record or {}).get("account_id") or "").strip()
        return value or None

    def is_admin(self, username: str) -> bool:
        record = self._users.get(str(username or "").strip().lower())
        return bool((record or {}).get("is_admin") is True)


def _authority_paths() -> tuple[str, str]:
    db_path = str(
        os.environ.get("OPEN_CLANK_AUTHORITY_DB_PATH") or APP_DB
    ).strip()
    auth_path = str(
        os.environ.get("OPEN_CLANK_AUTHORITY_AUTH_PATH") or AUTH_FILE
    ).strip()
    return db_path, auth_path


def _authority_context(
    owner: str,
    *,
    repository: FilePolicyRepository | None = None,
    auth: Any = None,
) -> tuple[FilePolicyRepository, str] | None:
    """Resolve the canonical repository plus immutable owner subject ID."""
    owner_key = str(owner or "").strip().lower()
    if not owner_key:
        return None
    db_path, auth_path = _authority_paths()
    if repository is None:
        if not os.path.isabs(db_path):
            return None
        repository = FilePolicyRepository(db_path)
    if auth is None:
        if os.path.isabs(auth_path) and os.path.isfile(auth_path):
            auth = ReadOnlyAuthSnapshot(auth_path)
    users = getattr(auth, "users", getattr(auth, "_users", None))
    if isinstance(users, dict):
        repository.sync_subjects(users)
    account_id = getattr(auth, "account_id", None)
    subject_id = account_id(owner_key) if callable(account_id) else None
    if not subject_id:
        return None
    return repository, str(subject_id)


def canonical_workspace_id(owner: str, identifier: str) -> str:
    """Normalize a pending approval's owner-qualified Workspace alias.

    This supplies no consent or path authority. Unknown context retains the
    request identifier; persistence and execution still resolve it separately.
    """
    if not identifier:
        return ""
    try:
        context = _authority_context(owner)
        if context:
            repository, subject = context
            return repository.legacy_target("workspace", subject + ":" + identifier, fallback=identifier)
    except (FilePolicyError, OSError, ValueError, OverflowError):
        pass
    return identifier


def _path_within(root: str, target: str) -> bool:
    try:
        normalized_root = os.path.normcase(os.path.abspath(str(root)))
        normalized_target = os.path.normcase(os.path.abspath(str(target)))
        return os.path.commonpath([normalized_root, normalized_target]) == normalized_root
    except (OSError, ValueError):
        return False


def location_for_path(
    repository: FilePolicyRepository,
    path: str,
    capability: str,
) -> Location | None:
    """Deepest enabled Location containing ``path`` that allows ``capability``."""
    target = str(path or "").strip()
    if not target:
        return None
    candidates: list[Location] = []
    for location in repository.list_locations():
        if not location.enabled or location.availability != "available":
            continue
        if capability not in location.capabilities:
            continue
        if location.kind == "exact_file":
            if _path_within(location.canonical_path, target) and _path_within(
                target, location.canonical_path
            ):
                candidates.append(location)
        elif _path_within(location.canonical_path, target):
            candidates.append(location)
    if not candidates:
        return None
    return max(candidates, key=lambda row: len(row.canonical_path))


def _location_for_scope(
    repository: FilePolicyRepository,
    *,
    subject_id: str,
    capability: str,
    workspace_id: str = "",
    target_path: str = "",
) -> Location | None:
    """Location for one approval, preferring the stable Workspace identity."""
    identifier = str(workspace_id or "").strip()
    if identifier:
        identifier = repository.legacy_target("workspace", subject_id + ":" + identifier, fallback=identifier)
        try:
            workspace = repository.get_workspace(identifier)
        except FilePolicyError:
            return None
        if workspace.archived or workspace.owner_subject_id != subject_id:
            return None
        try:
            location = repository.get_location(workspace.location_id)
        except FilePolicyError:
            return None
        if not location.enabled or location.availability != "available":
            return None
        if capability not in location.capabilities:
            return None
        return location
    return location_for_path(repository, target_path, capability)


def _expiry_unix_ms(value: Any) -> int | None:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return int(value)
    try:
        parsed = datetime.fromisoformat(str(value))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return int(parsed.timestamp() * 1000)
    except (TypeError, ValueError) as error:
        raise ValueError("Approval expiry is invalid") from error


def record_operation_approval(
    *,
    owner: str,
    permission_type: str,
    pattern: str,
    resource: str = "",
    lifetime: str,
    session_id: str = "",
    workspace_id: str = "",
    target_path: str = "",
    expires_at: Any = None,
    repository: FilePolicyRepository | None = None,
    auth: Any = None,
) -> bool:
    """Persist one typed canonical OperationApproval.

    Returns False when the context cannot be persisted safely. Callers retain
    only the current Once approval; no legacy writer is permitted.
    """
    lifetime = str(lifetime or "").strip().lower()
    if lifetime not in {"chat", "workspace", "always"}:
        return False
    chat_id = str(session_id or "").strip() if lifetime == "chat" else ""
    stable_workspace = str(workspace_id or "").strip()
    if lifetime == "chat" and not chat_id:
        return False
    if lifetime == "workspace" and not stable_workspace:
        return False
    if lifetime == "always":
        # Always carries no hidden chat/workspace dimension.
        chat_id = ""
        stable_workspace = ""
    capability = capability_for_permission(permission_type)
    if not capability:
        return False
    try:
        context = _authority_context(owner, repository=repository, auth=auth)
        if context is None:
            return False
        repo, subject_id = context
        location = _location_for_scope(
            repo,
            subject_id=subject_id,
            capability=capability,
            workspace_id=stable_workspace,
            target_path=target_path,
        )
        if location is None and capability != "execute":
            return False
        if stable_workspace:
            stable_workspace = repo.legacy_target("workspace", subject_id + ":" + stable_workspace, fallback=stable_workspace)
        binding = repo.create_binding(
            actor_subject_id=subject_id,
            binding_class="operation",
            subject_kind="user",
            subject_id=subject_id,
            location_id=location.id if location else None,
            workspace_id=stable_workspace or None,
            chat_id=chat_id or None,
            resource_ref=approval_resource_ref(permission_type, pattern, resource),
            operation=str(permission_type),
            capabilities=[capability],
            lifetime=lifetime,
            expires_unix_ms=_expiry_unix_ms(expires_at),
        )
        repo.store_approval_details(binding.id, str(permission_type), str(pattern), str(resource or ""))
        return True
    except (FilePolicyError, OSError, ValueError, OverflowError):
        return False


def _pattern_candidates(
    permission_type: str,
    *,
    filepath: str,
    resource: str,
    location: Location,
) -> set[str]:
    """Resource identities that could legitimately approve ``filepath``.

    Compatibility grants match a file when its directory equals or descends
    from the stored pattern, so the canonical candidates are the digests of
    every ancestor directory inside the Location plus the per-location ``*``
    row.  Digests outside the Location root are never produced, keeping a grant
    from one Location invisible to another.
    """
    candidates = {approval_resource_ref(permission_type, "*", resource)}
    target = str(filepath or "").strip()
    if target:
        current = os.path.dirname(os.path.normcase(os.path.abspath(target)))
        root = os.path.normcase(os.path.abspath(location.canonical_path))
        while current and _path_within(root, current):
            candidates.add(approval_resource_ref(permission_type, current, resource))
            parent = os.path.dirname(current)
            if parent == current:
                break
            current = parent
    return candidates


def match_operation_approval(
    *,
    owner: str,
    permission_type: str,
    filepath: str = "",
    resource: str = "",
    session_id: str = "",
    workspace_id: str = "",
    workspace_path: str = "",
    repository: FilePolicyRepository | None = None,
    auth: Any = None,
) -> bool:
    """True when a canonical OperationApproval covers this exact request.

    Resolution failure means no durable approval. No fallback authority exists.
    """
    capability = capability_for_permission(permission_type)
    if not capability:
        return False
    try:
        context = _authority_context(owner, repository=repository, auth=auth)
        if context is None:
            return False
        repo, subject_id = context
        stable_workspace = str(workspace_id or "").strip()
        if stable_workspace:
            stable_workspace = repo.legacy_target("workspace", subject_id + ":" + stable_workspace, fallback=stable_workspace)
        if repo.approval_match(subject_id=subject_id, permission_type=str(permission_type), filepath=filepath,
            resource=resource, session_id=str(session_id or ""), workspace_id=stable_workspace):
            return True
        location = _location_for_scope(
            repo,
            subject_id=subject_id,
            capability=capability,
            workspace_id=stable_workspace,
            target_path=filepath or workspace_path,
        )
        if location is None:
            return False
        candidates = _pattern_candidates(
            permission_type,
            filepath=filepath,
            resource=resource,
            location=location,
        )
        return repo.match_operation_approval(
            subject_id=subject_id,
            location_id=location.id,
            operation=str(permission_type),
            resource_refs=candidates,
            capability=capability,
            workspace_id=stable_workspace or None,
            chat_id=str(session_id or "").strip() or None,
        )
    except (FilePolicyError, OSError, ValueError, OverflowError):
        return False
