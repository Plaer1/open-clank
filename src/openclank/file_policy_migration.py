"""Dry-run and apply migration from legacy file roots/grants to canonical policy.

Migration is deliberately monotonic: legacy authorities are intersected, never
unioned across physical roots. Ambiguous rows are reported for review instead of
being guessed into broader bindings.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from src.openclank.file_policy import (
    FilePolicyError,
    FilePolicyRepository,
    deterministic_legacy_id,
)


@dataclass
class LegacyPolicyPlan:
    source_generation: int
    locations: list[dict[str, Any]] = field(default_factory=list)
    workspaces: list[dict[str, Any]] = field(default_factory=list)
    bindings: list[dict[str, Any]] = field(default_factory=list)
    unresolved: list[dict[str, str]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _canonical(value: str) -> str:
    return str(Path(str(value or "")).expanduser().resolve(strict=False))


def _contains(root: str, target: str) -> bool:
    try:
        return os.path.commonpath([root, target]) == root
    except ValueError:
        return False


def _subject(subject_ids: Mapping[str, str], username: str) -> str | None:
    return subject_ids.get(str(username or "").strip().lower())


def _legacy_location_kind(kind: str, path: str) -> str:
    if kind == "exact_file":
        return "exact_file"
    canonical = Path(path)
    return "whole_root" if canonical == Path(canonical.anchor) else "directory"


def _capability_for_permission(permission_type: str) -> str | None:
    value = str(permission_type or "").strip().lower()
    if value in {"read", "read_file", "list", "glob", "grep", "search"}:
        return "read"
    if value in {
        "write",
        "write_file",
        "edit",
        "delete",
        "trash",
        "move",
        "rename",
        "manage_files",
    }:
        return "write"
    # Shell/execute approvals cannot be imported before the restricted process
    # broker exists. Unknown permission types are review items, never Always.
    return None


def _expiry_ms(value: Any) -> int | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return int(parsed.timestamp() * 1000)
    except (TypeError, ValueError):
        return None


def _legacy_grants(db_path: str | os.PathLike[str] | None) -> list[dict[str, Any]]:
    if not db_path or not Path(db_path).is_file():
        return []
    connection = sqlite3.connect(str(db_path), timeout=10)
    connection.row_factory = sqlite3.Row
    try:
        exists = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='permission_grants'"
        ).fetchone()
        if not exists:
            return []
        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(permission_grants)")
        }
        required = {"id", "owner", "session_id", "permission_type", "pattern", "workspace", "resource"}
        if not required.issubset(columns):
            return []
        expiry = "expires_at" if "expires_at" in columns else "NULL AS expires_at"
        workspace_id = (
            "workspace_id"
            if "workspace_id" in columns
            else "'' AS workspace_id"
        )
        revoked = "AND revoked_at IS NULL" if "revoked_at" in columns else ""
        return [
            dict(row)
            for row in connection.execute(
                f"""SELECT id,owner,session_id,permission_type,pattern,workspace,
                            {workspace_id},resource,{expiry}
                     FROM permission_grants WHERE 1=1 {revoked}
                     ORDER BY id"""
            )
        ]
    finally:
        connection.close()


def plan_legacy_policy_import(
    *,
    registry_path: str | os.PathLike[str],
    subject_ids: Mapping[str, str],
    admin_subject_ids: Sequence[str] = (),
    grant_db_path: str | os.PathLike[str] | None = None,
    raw_workspaces: Sequence[Mapping[str, Any]] = (),
) -> LegacyPolicyPlan:
    """Build a deterministic no-write plan; ambiguous rows remain unresolved."""
    path = Path(registry_path)
    if not path.is_file():
        legacy: dict[str, Any] = {
            "version": 1,
            "generation": 0,
            "roots": {},
            "visibility_assignments": {},
        }
    else:
        try:
            legacy = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise FilePolicyError("legacy filesystem registry is unreadable", code="migration_source_unavailable") from exc
    if legacy.get("version") != 1 or not isinstance(legacy.get("roots"), dict):
        raise FilePolicyError("legacy filesystem registry format is unsupported", code="migration_source_unavailable")

    plan = LegacyPolicyPlan(source_generation=int(legacy.get("generation") or 0))
    admins = {str(value) for value in admin_subject_ids}
    roots = legacy.get("roots") or {}
    assignments = legacy.get("visibility_assignments") or {}
    root_to_location: dict[str, str] = {}
    location_by_key: dict[tuple[str, str], dict[str, Any]] = {}

    # First create physical identities with the union of physical/root ceilings.
    # Bindings below retain each subject's actual narrower capabilities.
    for root_id, root in sorted(roots.items()):
        if not isinstance(root, dict):
            plan.unresolved.append({"kind": "root", "legacy_id": str(root_id), "reason": "malformed"})
            continue
        owner_subject = _subject(subject_ids, root.get("owner_id", ""))
        if not owner_subject:
            plan.unresolved.append({"kind": "root", "legacy_id": str(root_id), "reason": "unknown_owner"})
            continue
        canonical = _canonical(root.get("canonical_path") or "")
        kind = _legacy_location_kind(str(root.get("kind") or ""), canonical)
        key = (kind, os.path.normcase(canonical))
        location = location_by_key.get(key)
        if location is None:
            location = {
                "id": deterministic_legacy_id("location", f"{kind}:{key[1]}"),
                "path": canonical,
                "kind": kind,
                "display_path": str(root.get("display_path") or canonical),
                "capabilities": [],
                "platform_identity": dict(root.get("platform_identity") or {}),
                "availability": str(root.get("availability") or "unavailable"),
                "actor_subject_id": owner_subject,
                "migration_source": "filesystem-registry-v1",
                "migration_key": f"location:{kind}:{key[1]}",
            }
            location_by_key[key] = location
        location["capabilities"] = sorted(
            set(location["capabilities"]) | set(str(value) for value in root.get("capabilities") or [])
        )
        if root.get("availability") == "available":
            location["availability"] = "available"
        root_to_location[str(root_id)] = location["id"]
    plan.locations = sorted(location_by_key.values(), key=lambda row: row["id"])
    locations_by_id = {row["id"]: row for row in plan.locations}

    # Visibility assignments become People bindings. Group rows remain disabled
    # until the auth layer supplies real immutable membership.
    people_caps: dict[tuple[str, str], set[str]] = {}
    ceilings_by_username: dict[str, list[tuple[dict[str, Any], set[str]]]] = {}
    for assignment_id, assignment in sorted(assignments.items()):
        if not isinstance(assignment, dict) or not assignment.get("enabled"):
            continue
        subject_kind = str(assignment.get("subject_kind") or "user")
        if subject_kind != "user":
            plan.unresolved.append({"kind": "visibility", "legacy_id": str(assignment_id), "reason": "group_membership_unavailable"})
            continue
        username = str(assignment.get("subject_id") or "").strip().lower()
        subject_id = _subject(subject_ids, username)
        location_id = root_to_location.get(str(assignment.get("root_id") or ""))
        root = roots.get(str(assignment.get("root_id") or ""))
        if not subject_id or not location_id or not isinstance(root, dict):
            plan.unresolved.append({"kind": "visibility", "legacy_id": str(assignment_id), "reason": "unknown_subject_or_root"})
            continue
        caps = set(str(value) for value in assignment.get("capabilities") or [])
        caps &= set(locations_by_id[location_id]["capabilities"])
        if not caps:
            plan.unresolved.append({"kind": "visibility", "legacy_id": str(assignment_id), "reason": "empty_capability_intersection"})
            continue
        people_caps.setdefault((subject_id, location_id), set()).update(caps)
        ceilings_by_username.setdefault(username, []).append((root, caps))

    # Every legacy owner root was agent authority. For nonadmins, retain only
    # the part within a current People ceiling and add a derived People binding
    # on the exact owner Location so the new same-Location resolver is monotonic.
    agent_caps: dict[tuple[str, str], set[str]] = {}
    for root_id, root in sorted(roots.items()):
        if not isinstance(root, dict) or not root.get("enabled"):
            continue
        username = str(root.get("owner_id") or "").strip().lower()
        subject_id = _subject(subject_ids, username)
        location_id = root_to_location.get(str(root_id))
        if not subject_id or not location_id:
            continue
        caps = set(str(value) for value in root.get("capabilities") or [])
        caps &= set(locations_by_id[location_id]["capabilities"])
        if subject_id not in admins:
            root_path = _canonical(root.get("canonical_path") or "")
            effective: set[str] = set()
            for ceiling, ceiling_caps in ceilings_by_username.get(username, []):
                ceiling_path = _canonical(ceiling.get("canonical_path") or "")
                contains = (
                    ceiling.get("kind") == "recursive_directory" and _contains(ceiling_path, root_path)
                ) or (
                    ceiling.get("kind") == "exact_file"
                    and root.get("kind") == "exact_file"
                    and ceiling_path == root_path
                )
                if contains:
                    effective |= caps & ceiling_caps & set(ceiling.get("capabilities") or [])
            caps = effective
            if caps:
                people_caps.setdefault((subject_id, location_id), set()).update(caps)
        if not caps:
            plan.unresolved.append({"kind": "agent_root", "legacy_id": str(root_id), "reason": "no_app_ceiling"})
            continue
        agent_caps.setdefault((subject_id, location_id), set()).update(caps)

    for (subject_id, location_id), caps in sorted(people_caps.items()):
        key = f"people:{subject_id}:{location_id}"
        plan.bindings.append(
            {
                "id": deterministic_legacy_id("binding", key),
                "binding_class": "people",
                "subject_kind": "user",
                "subject_id": subject_id,
                "location_id": location_id,
                "capabilities": sorted(caps),
                "lifetime": "always",
                "actor_subject_id": next(iter(admins), subject_id),
                "migration_source": "filesystem-registry-v1",
                "migration_key": key,
            }
        )
    for (subject_id, location_id), caps in sorted(agent_caps.items()):
        key = f"agent:{subject_id}:{location_id}"
        plan.bindings.append(
            {
                "id": deterministic_legacy_id("binding", key),
                "binding_class": "agent",
                "subject_kind": "user",
                "subject_id": subject_id,
                "location_id": location_id,
                "capabilities": sorted(caps),
                "lifetime": "always",
                "actor_subject_id": subject_id,
                "migration_source": "filesystem-registry-v1",
                "migration_key": key,
            }
        )

    # Raw workspace selections can only bind to an already imported Location;
    # they never create a Location or policy from browser state.
    for index, raw in enumerate(raw_workspaces):
        username = str(raw.get("owner") or "").strip().lower()
        subject_id = _subject(subject_ids, username)
        value = str(raw.get("path") or "").strip()
        legacy_key = str(raw.get("legacy_key") or f"workspace:{username}:{index}")
        if not subject_id or not value:
            plan.unresolved.append({"kind": "workspace", "legacy_id": legacy_key, "reason": "unknown_owner_or_path"})
            continue
        target = _canonical(value)
        candidates = [
            location for location in plan.locations
            if location["kind"] in {"directory", "volume", "whole_root"}
            and _contains(location["path"], target)
        ]
        if not candidates:
            plan.unresolved.append({"kind": "workspace", "legacy_id": legacy_key, "reason": "outside_imported_location"})
            continue
        location = max(candidates, key=lambda row: len(row["path"]))
        relative = os.path.relpath(target, location["path"])
        relative = "" if relative == "." else relative.replace(os.sep, "/")
        plan.workspaces.append(
            {
                "id": deterministic_legacy_id("workspace", legacy_key),
                "actor_subject_id": subject_id,
                "owner_subject_id": subject_id,
                "location_id": location["id"],
                "name": str(raw.get("name") or Path(target).name or "Workspace"),
                "relative_folder": relative,
                "migration_source": "browser-workspace-v1",
                "migration_key": legacy_key,
                "legacy_path": target,
            }
        )

    workspace_by_legacy_path = {
        row["legacy_path"]: row for row in plan.workspaces
    }
    # Existing permission rows only migrate when both the resource and lifetime
    # map unambiguously. Bare/global rows are review items, never true Always.
    for grant in _legacy_grants(grant_db_path):
        legacy_id = str(grant.get("id"))
        subject_id = _subject(subject_ids, grant.get("owner", ""))
        capability = _capability_for_permission(grant.get("permission_type", ""))
        pattern = str(grant.get("pattern") or "")
        if str(grant.get("workspace_id") or ""):
            # This planner imports raw legacy authorities. A stable Workspace
            # identity must be revalidated against the live policy repository;
            # discarding it here would broaden a chat approval across
            # Workspaces, so leave it for the runtime compatibility resolver.
            plan.unresolved.append({
                "kind": "grant",
                "legacy_id": legacy_id,
                "reason": "stable_workspace_grant_requires_runtime_resolver",
            })
            continue
        if not subject_id or not capability or not pattern or pattern == "*":
            plan.unresolved.append({"kind": "grant", "legacy_id": legacy_id, "reason": "ambiguous_subject_resource_or_operation"})
            continue
        target = _canonical(pattern)
        candidates = [
            location for location in plan.locations
            if capability in location["capabilities"]
            and (
                (location["kind"] == "exact_file" and location["path"] == target)
                or (location["kind"] != "exact_file" and _contains(location["path"], target))
            )
        ]
        if not candidates:
            plan.unresolved.append({"kind": "grant", "legacy_id": legacy_id, "reason": "outside_imported_location"})
            continue
        location = max(candidates, key=lambda row: len(row["path"]))
        session_id = str(grant.get("session_id") or "")
        raw_workspace = str(grant.get("workspace") or "")
        workspace = workspace_by_legacy_path.get(_canonical(raw_workspace)) if raw_workspace else None
        if session_id:
            lifetime = "chat"
        elif workspace:
            lifetime = "workspace"
        else:
            plan.unresolved.append({"kind": "grant", "legacy_id": legacy_id, "reason": "unscoped_grant_not_promoted_to_always"})
            continue
        resource_digest = hashlib.sha256(
            (str(grant.get("permission_type")) + "\0" + pattern + "\0" + str(grant.get("resource") or "")).encode("utf-8")
        ).hexdigest()
        key = f"grant:{legacy_id}"
        plan.bindings.append(
            {
                "id": deterministic_legacy_id("binding", key),
                "binding_class": "operation",
                "subject_kind": "user",
                "subject_id": subject_id,
                "location_id": location["id"],
                "workspace_id": workspace["id"] if workspace else None,
                "chat_id": session_id or None,
                "resource_ref": f"legacy-resource-{resource_digest}",
                "operation": str(grant.get("permission_type") or ""),
                "capabilities": [capability],
                "lifetime": lifetime,
                "expires_unix_ms": _expiry_ms(grant.get("expires_at")),
                "actor_subject_id": subject_id,
                "migration_source": "permission-grants-v1",
                "migration_key": key,
            }
        )

    plan.bindings.sort(key=lambda row: row["id"])
    plan.workspaces.sort(key=lambda row: row["id"])
    plan.unresolved.sort(key=lambda row: (row["kind"], row["legacy_id"], row["reason"]))
    return plan


def backup_policy_database(db_path: str | os.PathLike[str]) -> Path | None:
    source = Path(db_path)
    if not source.is_file():
        return None
    destination = source.with_name(f"{source.name}.file-policy-{int(time.time())}.bak")
    # sqlite backup is consistent even when the source uses WAL.
    src = sqlite3.connect(str(source), timeout=30)
    dst = sqlite3.connect(str(destination), timeout=30)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    return destination


def apply_legacy_policy_plan(
    repository: FilePolicyRepository,
    plan: LegacyPolicyPlan,
    *,
    create_backup: bool = True,
) -> dict[str, Any]:
    """Apply an already reviewed plan idempotently; unresolved rows stay out."""
    backup = backup_policy_database(repository.db_path) if create_backup else None
    applied = {"locations": 0, "workspaces": 0, "bindings": 0}
    for row in plan.locations:
        repository.create_location(
            actor_subject_id=row["actor_subject_id"],
            path=row["path"],
            kind=row["kind"],
            capabilities=row["capabilities"],
            display_path=row["display_path"],
            platform_identity=row["platform_identity"],
            availability=row["availability"],
            location_id=row["id"],
            migration_source=row["migration_source"],
            migration_key=row["migration_key"],
        )
        applied["locations"] += 1
    for row in plan.workspaces:
        try:
            repository.get_workspace(row["id"])
        except FilePolicyError as error:
            if error.code != "workspace_not_found":
                raise
            repository.create_workspace(
                actor_subject_id=row["actor_subject_id"],
                owner_subject_id=row["owner_subject_id"],
                location_id=row["location_id"],
                name=row["name"],
                relative_folder=row["relative_folder"],
                workspace_id=row["id"],
                migration_source=row["migration_source"],
                migration_key=row["migration_key"],
            )
        applied["workspaces"] += 1
    for row in plan.bindings:
        try:
            repository.get_binding(row["id"])
        except FilePolicyError as error:
            if error.code != "binding_not_found":
                raise
            repository.create_binding(
                actor_subject_id=row["actor_subject_id"],
                binding_class=row["binding_class"],
                subject_kind=row["subject_kind"],
                subject_id=row["subject_id"],
                location_id=row["location_id"],
                workspace_id=row.get("workspace_id"),
                chat_id=row.get("chat_id"),
                resource_ref=row.get("resource_ref"),
                operation=row.get("operation"),
                capabilities=row["capabilities"],
                lifetime=row["lifetime"],
                expires_unix_ms=row.get("expires_unix_ms"),
                binding_id=row["id"],
                migration_source=row["migration_source"],
                migration_key=row["migration_key"],
            )
        applied["bindings"] += 1
    return {
        "applied": applied,
        "unresolved": len(plan.unresolved),
        "backup": str(backup) if backup else None,
        "generation": repository.generation(),
    }
