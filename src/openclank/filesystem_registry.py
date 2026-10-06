"""Persistent owner-scoped agent filesystem roots.

The legacy class and wire shape are canonical SQLite adapters. Rust consumes a
derived read-only snapshot; JSON is never queried as permission authority.
"""

from __future__ import annotations

import json
import hashlib
import os
import copy
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from src.constants import DATA_DIR, APP_DB
from src.openclank.file_policy import FilePolicyRepository, FilePolicyError


class FilesystemRegistryError(ValueError):
    def __init__(self, message: str, *, code: str = "invalid_root") -> None:
        super().__init__(message)
        self.code = code


class FilesystemRootRegistry:
    VERSION = 1
    KINDS = {"exact_file", "recursive_directory"}
    CAPABILITIES = {"read", "write", "execute"}
    _snapshot_lock = threading.RLock()

    def __init__(self, path: str | os.PathLike[str] | None = None, *, repository: FilePolicyRepository | None = None) -> None:
        # path is a transport/source hint only; it never selects another policy
        # database. Explicit authority overrides support isolated installations.
        self.path = Path(path) if path else Path(DATA_DIR) / "odysseus-file-roots.json"
        self.repository = repository or FilePolicyRepository(os.environ.get("OPEN_CLANK_AUTHORITY_DB_PATH") or APP_DB)
        self._lock = threading.RLock()
        self._preimage = None

    def _load_locked(self) -> dict[str, Any]:
        data = self.repository.registry_projection()
        self._preimage = copy.deepcopy(data)
        return data

    @staticmethod
    def _bump_generation(data: dict[str, Any]) -> int:
        # Only the canonical SQLite mutation may advance the policy generation.
        return int(data["generation"])

    def _write_locked(self, data: dict[str, Any]) -> None:
        if self._preimage is None:
            raise FilesystemRegistryError("missing canonical mutation preimage", code="registry_unavailable")
        try:
            self.repository.apply_registry_projection(self._preimage, data)
        except FilePolicyError as error:
            raise FilesystemRegistryError(str(error), code=error.code) from error
        self._preimage = None

    @staticmethod
    def _canonical(path: str) -> Path:
        value = str(path or "").strip()
        if not value:
            raise FilesystemRegistryError("path is required", code="path_required")
        return Path(value).expanduser().resolve(strict=False)

    @staticmethod
    def _availability(path: Path) -> tuple[str, dict[str, Any]]:
        try:
            stat = path.stat()
        except FileNotFoundError:
            return "missing", {"volume_id": None, "file_id": None, "device": None, "inode": None, "case_sensitive": None}
        except PermissionError:
            return "permission_denied", {"volume_id": None, "file_id": None, "device": None, "inode": None, "case_sensitive": None}
        except OSError:
            return "unavailable", {"volume_id": None, "file_id": None, "device": None, "inode": None, "case_sensitive": None}
        return "available", {
            "volume_id": str(getattr(stat, "st_dev", "")) or None,
            "file_id": None,
            "device": getattr(stat, "st_dev", None),
            "inode": getattr(stat, "st_ino", None),
            "case_sensitive": os.name != "nt",
        }

    def _record(self, owner: str, path: str, kind: str, capabilities: list[str], *, display_path: str | None = None) -> dict[str, Any]:
        if kind not in self.KINDS:
            raise FilesystemRegistryError("kind must be exact_file or recursive_directory", code="invalid_kind")
        requested = sorted({str(value) for value in capabilities})
        if not requested or not set(requested).issubset(self.CAPABILITIES):
            raise FilesystemRegistryError("capabilities must be read, write, or execute", code="invalid_capabilities")
        canonical = self._canonical(path)
        availability, identity = self._availability(canonical)
        if kind == "recursive_directory":
            if availability != "available" or not canonical.is_dir():
                raise FilesystemRegistryError("recursive directory must exist and be readable", code="not_directory")
        elif availability != "available" or not canonical.is_file():
            raise FilesystemRegistryError("exact file must exist and be readable", code="not_file")
        now = int(time.time() * 1000)
        return {
            "id": f"root-{uuid.uuid4().hex}",
            "owner_id": str(owner),
            "kind": kind,
            "canonical_path": str(canonical),
            "display_path": str(display_path or path),
            "enabled": True,
            "capabilities": requested,
            "platform_identity": identity,
            "last_validated_unix_ms": now,
            "availability": availability,
        }

    def list(self, owner: str) -> list[dict[str, Any]]:
        with self._lock:
            roots = self._load_locked()["roots"].values()
            return sorted((dict(root) for root in roots if root.get("owner_id") == str(owner)), key=lambda root: root.get("display_path", "").casefold())

    def add(self, owner: str, path: str, kind: str, capabilities: list[str], *, display_path: str | None = None) -> dict[str, Any]:
        record = self._record(owner, path, kind, capabilities, display_path=display_path)
        with self._lock:
            data = self._load_locked()
            for existing in data["roots"].values():
                if existing.get("owner_id") == str(owner) and existing.get("kind") == kind and existing.get("canonical_path") == record["canonical_path"]:
                    raise FilesystemRegistryError("that root is already registered", code="duplicate_root")
            data["roots"][record["id"]] = record
            self._bump_generation(data)
            self._write_locked(data)
        return dict(record)

    def update(self, owner: str, root_id: str, *, enabled: bool | None = None, capabilities: list[str] | None = None) -> dict[str, Any]:
        with self._lock:
            data = self._load_locked()
            record = data["roots"].get(root_id)
            if not record or record.get("owner_id") != str(owner):
                raise FilesystemRegistryError("root was not found", code="root_not_found")
            if enabled is not None:
                record["enabled"] = bool(enabled)
            if capabilities is not None:
                values = sorted({str(value) for value in capabilities})
                if not values or not set(values).issubset(self.CAPABILITIES):
                    raise FilesystemRegistryError("capabilities must be read, write, or execute", code="invalid_capabilities")
                record["capabilities"] = values
            data["roots"][root_id] = record
            self._bump_generation(data)
            self._write_locked(data)
            return dict(record)

    def remove(self, owner: str, root_id: str) -> None:
        with self._lock:
            data = self._load_locked()
            record = data["roots"].get(root_id)
            if not record or record.get("owner_id") != str(owner):
                raise FilesystemRegistryError("root was not found", code="root_not_found")
            del data["roots"][root_id]
            assignments = data.get("visibility_assignments", {})
            for assignment_id, assignment in list(assignments.items()):
                if assignment.get("root_id") == root_id:
                    del assignments[assignment_id]
            self._bump_generation(data)
            self._write_locked(data)

    def disable_projection(self, canonical_path: str, kind: str) -> dict[str, int]:
        """Fail-narrow every legacy row derived from one canonical Location."""
        canonical = str(self._canonical(canonical_path))
        legacy_kind = "exact_file" if kind == "exact_file" else "recursive_directory"
        with self._lock:
            data = self._load_locked()
            root_ids: set[str] = set()
            roots_disabled = 0
            for root_id, root in data.get("roots", {}).items():
                if root.get("kind") != legacy_kind or root.get("canonical_path") != canonical:
                    continue
                root_ids.add(str(root_id))
                if root.get("enabled"):
                    root["enabled"] = False
                    roots_disabled += 1
            assignments_disabled = 0
            for assignment in data.get("visibility_assignments", {}).values():
                if str(assignment.get("root_id")) not in root_ids or not assignment.get("enabled"):
                    continue
                assignment["enabled"] = False
                assignments_disabled += 1
            if roots_disabled or assignments_disabled:
                self._bump_generation(data)
                self._write_locked(data)
            return {
                "roots_disabled": roots_disabled,
                "assignments_disabled": assignments_disabled,
            }

    def rename_owner(self, old_owner: str, new_owner: str) -> None:
        subject = self.repository.subject_for_username(old_owner) or self.repository.subject_for_username(new_owner)
        if subject:
            self.repository.move_subject_alias(old_owner, new_owner, subject)

    def owner_inventory(self, owner: str) -> dict[str, Any]:
        """Return a content-free CAS inventory for one account owner."""
        target = str(owner or "").strip().lower()
        with self._lock:
            data = self._load_locked()
            root_ids = sorted(
                str(root_id)
                for root_id, root in data.get("roots", {}).items()
                if str(root.get("owner_id") or "").strip().lower() == target
            )
            assignment_ids = sorted(
                str(assignment_id)
                for assignment_id, assignment in data.get("visibility_assignments", {}).items()
                if any(
                    str(assignment.get(key) or "").strip().lower() == target
                    for key in ("subject_id", "issuer_id")
                )
                or str(assignment.get("root_id") or "") in root_ids
            )
        subject = self.repository.subject_for_username(target)
        migration_ids = [row["id"] for row in self.repository.migration_state(subject_id=subject, include_inactive=True)["items"] if row["status"] == "unresolved"] if subject else []
        material = json.dumps(
            {"roots": root_ids, "assignments": assignment_ids, "subject_id": subject, "migration_ids": migration_ids},
            sort_keys=True,
            separators=(",", ":"),
        )
        return {
            "owner": target,
            "count": len(root_ids) + len(assignment_ids) + len(migration_ids) + int(bool(subject)),
            "subject_id": subject,
            "migration_ids": migration_ids,
            "root_ids": root_ids,
            "assignment_ids": assignment_ids,
            "fingerprint": hashlib.sha256(material.encode("utf-8")).hexdigest(),
        }

    def preview_owner_rename(self, source_owner: str, target_owner: str) -> dict[str, Any]:
        source = self.owner_inventory(source_owner)
        target = self.owner_inventory(target_owner)
        if target.get("subject_id") or target["count"]:
            raise FilesystemRegistryError(
                "filesystem registry rename target already contains state",
                code="owner_conflict",
            )
        return {"version": 1, "source": source, "target": target}

    @staticmethod
    def _inventory_matches(actual: dict[str, Any], expected: dict[str, Any]) -> bool:
        return all(
            actual.get(key) == expected.get(key)
            for key in ("count", "root_ids", "assignment_ids", "fingerprint")
        )

    def reconcile_owner_rename(
        self,
        source_owner: str,
        target_owner: str,
        manifest: dict[str, Any],
    ) -> dict[str, Any]:
        expected = dict(manifest.get("source") or {})
        source = self.owner_inventory(source_owner)
        target = self.owner_inventory(target_owner)
        if self._inventory_matches(source, expected) and target["count"] == 0:
            self.rename_owner(source_owner, target_owner)
        elif source["count"] != 0 or not self._inventory_matches(target, expected):
            raise FilesystemRegistryError(
                "filesystem registry owner state changed after preflight",
                code="owner_conflict",
            )
        source = self.owner_inventory(source_owner)
        target = self.owner_inventory(target_owner)
        if source["count"] or not self._inventory_matches(target, expected):
            raise FilesystemRegistryError(
                "filesystem registry rename did not converge",
                code="registry_unavailable",
            )
        return {"state": "staged", "source": source, "target": target}

    def compensate_owner_rename(
        self,
        source_owner: str,
        target_owner: str,
        manifest: dict[str, Any],
    ) -> dict[str, Any]:
        reverse = {"version": 1, "source": dict(manifest.get("source") or {}), "target": {}}
        return self.reconcile_owner_rename(target_owner, source_owner, reverse)

    def purge_owner_lifecycle(
        self,
        owner: str,
        *,
        expected: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        before = self.owner_inventory(owner)
        if expected is not None and before["count"] and not self._inventory_matches(before, expected):
            raise FilesystemRegistryError(
                "filesystem registry owner state changed before purge",
                code="owner_conflict",
            )
        self.delete_owner(owner)
        after = self.owner_inventory(owner)
        if after["count"]:
            raise FilesystemRegistryError(
                "filesystem registry purge did not converge",
                code="registry_unavailable",
            )
        return {"state": "purged", "before": before, "after": after}

    def delete_owner(self, owner: str) -> None:
        subject = self.repository.subject_for_username(owner)
        if subject:
            self.repository.purge_subject(subject, actor_subject_id=subject)

    def list_visibility(self) -> list[dict[str, Any]]:
        """Return administrator-issued assignments, without trusting a caller path."""
        with self._lock:
            assignments = self._load_locked().get("visibility_assignments", {}).values()
            return sorted((dict(item) for item in assignments), key=lambda item: (str(item.get("subject_id", "")).casefold(), str(item.get("root_id", ""))))

    def assign_visibility(
        self,
        issuer_id: str,
        subject_id: str,
        root_id: str,
        capabilities: list[str] | None = None,
        *,
        subject_kind: str = "user",
    ) -> dict[str, Any]:
        subject = str(subject_id or "").strip()
        if not subject:
            raise FilesystemRegistryError("subject_id is required", code="subject_required")
        if subject_kind not in {"user", "group"}:
            raise FilesystemRegistryError("subject_kind must be user or group", code="invalid_subject_kind")
        requested = sorted({str(value) for value in (capabilities or [])})
        if not requested or not set(requested).issubset(self.CAPABILITIES):
            raise FilesystemRegistryError("capabilities must be read, write, or execute", code="invalid_capabilities")
        with self._lock:
            data = self._load_locked()
            root = data["roots"].get(str(root_id))
            if not root:
                raise FilesystemRegistryError("root was not found", code="root_not_found")
            if root.get("owner_id") != str(issuer_id):
                raise FilesystemRegistryError("root was not found", code="root_not_found")
            if not root.get("enabled") or root.get("availability") != "available":
                raise FilesystemRegistryError("root is disabled or unavailable", code="root_unavailable")
            if not set(requested).issubset(set(root.get("capabilities") or [])):
                raise FilesystemRegistryError("assignment exceeds root capabilities", code="invalid_capabilities")
            assignments = data.setdefault("visibility_assignments", {})
            for assignment in assignments.values():
                if assignment.get("subject_id") == subject and assignment.get("subject_kind") == subject_kind and assignment.get("root_id") == str(root_id):
                    raise FilesystemRegistryError("that visibility assignment already exists", code="duplicate_assignment")
            generation = self._bump_generation(data)
            now = int(time.time() * 1000)
            assignment = {
                "id": f"visibility-{uuid.uuid4().hex}",
                "root_id": str(root_id),
                "subject_id": subject,
                "subject_kind": subject_kind,
                "issuer_id": str(issuer_id),
                "enabled": True,
                "capabilities": requested,
                "created_unix_ms": now,
                "updated_unix_ms": now,
                "generation": generation,
            }
            assignments[assignment["id"]] = assignment
            self._write_locked(data)
            return dict(assignment)

    def update_visibility(
        self,
        issuer_id: str,
        assignment_id: str,
        *,
        enabled: bool | None = None,
        capabilities: list[str] | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            data = self._load_locked()
            assignment = data.get("visibility_assignments", {}).get(str(assignment_id))
            if not assignment:
                raise FilesystemRegistryError("visibility assignment was not found", code="assignment_not_found")
            if enabled is not None:
                assignment["enabled"] = bool(enabled)
            if capabilities is not None:
                values = sorted({str(value) for value in capabilities})
                if not values or not set(values).issubset(self.CAPABILITIES):
                    raise FilesystemRegistryError("capabilities must be read, write, or execute", code="invalid_capabilities")
                root = data["roots"].get(str(assignment.get("root_id")))
                if not root or not set(values).issubset(set(root.get("capabilities") or [])):
                    raise FilesystemRegistryError("assignment exceeds root capabilities", code="invalid_capabilities")
                assignment["capabilities"] = values
            assignment["updated_unix_ms"] = int(time.time() * 1000)
            assignment["issuer_id"] = str(issuer_id)
            assignment["generation"] = self._bump_generation(data)
            self._write_locked(data)
            return dict(assignment)

    def remove_visibility(self, issuer_id: str, assignment_id: str) -> None:
        with self._lock:
            data = self._load_locked()
            assignments = data.get("visibility_assignments", {})
            if str(assignment_id) not in assignments:
                raise FilesystemRegistryError("visibility assignment was not found", code="assignment_not_found")
            del assignments[str(assignment_id)]
            self._bump_generation(data)
            self._write_locked(data)

    def visibility_for_subject(self, subject_id: str, groups: list[str] | None = None) -> list[dict[str, Any]]:
        user_subject = str(subject_id or "")
        group_subjects = {str(group) for group in (groups or []) if str(group)}
        with self._lock:
            data = self._load_locked()
            roots = data.get("roots", {})
            result = []
            for assignment in data.get("visibility_assignments", {}).values():
                if not assignment.get("enabled"):
                    continue
                kind = str(assignment.get("subject_kind") or "user")
                assigned_subject = str(assignment.get("subject_id") or "")
                if (kind == "user" and assigned_subject != user_subject) or (kind == "group" and assigned_subject not in group_subjects):
                    continue
                root = roots.get(str(assignment.get("root_id")))
                if not root:
                    continue
                item = dict(assignment)
                item["root"] = dict(root)
                result.append(item)
            return sorted(result, key=lambda item: str(item["root"].get("display_path", "")).casefold())

    def app_scope(self, subject_id: str, *, is_admin: bool, groups: list[str] | None = None) -> dict[str, Any]:
        from src.openclank.operation_approvals import _authority_context
        _authority_context(subject_id, repository=self.repository)
        with self._lock:
            data = self._load_locked()
        generation = int(data["generation"])
        if is_admin:
            return {"host": True, "visible_root_ids": [], "capabilities": [],
                "root_capabilities": {}, "generation": generation, "active_folder": None}
        assignments = []
        for assignment in data["visibility_assignments"].values():
            kind = assignment.get("subject_kind", "user")
            if (kind == "user" and assignment["subject_id"] != subject_id) or (kind == "group" and assignment["subject_id"] not in (groups or [])):
                continue
            root = data["roots"].get(assignment["root_id"])
            if assignment.get("enabled") and root and root.get("enabled") and root.get("availability") == "available":
                assignments.append(dict(assignment, capabilities=sorted(set(assignment["capabilities"]) & set(root["capabilities"]))))
        root_capabilities: dict[str, set[str]] = {}
        for assignment in assignments:
            root_id = str(assignment["root_id"])
            root_capabilities.setdefault(root_id, set()).update(
                str(value) for value in assignment.get("capabilities") or []
            )
        # Keep the aggregate ceiling on the wire for protocol compatibility and
        # inexpensive admission checks, but Rust authorizes each operation using
        # the capability set for the target root. A capability granted on one
        # root must never bleed into a differently scoped assignment.
        capabilities = {
            capability
            for values in root_capabilities.values()
            for capability in values
        }
        return {
            "host": False,
            "visible_root_ids": sorted(root_capabilities),
            "capabilities": sorted(capabilities),
            "root_capabilities": {
                root_id: sorted(root_capabilities[root_id])
                for root_id in sorted(root_capabilities)
            },
            "generation": generation,
            "active_folder": None,
        }

    def rust_snapshot_path(self, *, expected_generation: int | None = None) -> str:
        # Generation-addressed, immutable transport artifact. No request-time
        # publication; a process startup prepares only its current generation.
        with self._snapshot_lock:
            snapshot = self.snapshot()
            if expected_generation is not None and snapshot["generation"] != expected_generation:
                raise FilesystemRegistryError("File access changed; retry with a fresh scope", code="policy_generation_changed")
            destination = Path(self.repository.db_path).parent / f"file-policy-derived-roots-{snapshot['generation']}.json"
            if destination.is_file():
                return str(destination)
            for root in snapshot["roots"].values():
                # Physical metadata alone never authorizes: the signed scope
                # supplies the canonical effective intersection for each root.
                if "physical_capabilities" in root:
                    root["capabilities"] = root["physical_capabilities"]
            destination.parent.mkdir(parents=True, exist_ok=True)
            fd, temporary = tempfile.mkstemp(prefix=".file-policy-roots-", dir=str(destination.parent))
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    json.dump(snapshot, handle, sort_keys=True)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.chmod(temporary, 0o400)
                os.replace(temporary, destination)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)
            return str(destination)

    def generation(self) -> int:
        """Return the current policy generation without projecting any scope."""
        return self.repository.generation()

    def snapshot(self) -> dict[str, Any]:
        """Return one locked, immutable-by-convention registry projection.

        History and Files supervisors use this boundary when publishing root
        authority.  Callers must not reach through ``_load_locked``: a deep
        copy keeps the lock's view coherent while preventing a caller from
        mutating the registry object it received.
        """
        with self._lock:
            return copy.deepcopy(self._load_locked())

    @staticmethod
    def _contains(root: str, target: str) -> bool:
        try:
            return os.path.commonpath([root, target]) == root
        except ValueError:
            return False

    def agent_scope(self, owner: str, active_workspace: str | None = None,
                    app_visibility: list[dict[str, Any]] | None = None, *,
                    workspace_id: str | None = None, chat_id: str | None = None) -> dict[str, Any]:
        """Mint one canonical AgentScope; paths only narrow existing bindings."""
        empty = {"approved_root_ids": [], "root_capabilities": {}, "active_folder": None}
        from src.openclank.operation_approvals import _authority_context
        context = _authority_context(owner, repository=self.repository)
        subject = context[1] if context else None
        generation = self.repository.generation()
        empty["generation"] = generation
        def finish(scope):
            if self.repository.generation() != generation:
                raise FilesystemRegistryError("File access changed; retry with a fresh scope", code="policy_generation_changed")
            return dict(scope, generation=generation)
        if not subject or str(owner).startswith("deleted:"):
            return finish(empty)
        workspace = None
        stable_id = str(workspace_id or "")
        if stable_id:
            stable_id = self.repository.legacy_target("workspace", subject + ":" + stable_id, fallback=stable_id)
            try:
                workspace = self.repository.get_workspace(stable_id)
                location = self.repository.get_location(workspace.location_id)
            except FilePolicyError:
                return finish(empty)
            if workspace.owner_subject_id != subject or workspace.archived or not location.enabled or location.availability != "available":
                return finish(empty)
            bound_path = str(Path(location.canonical_path).joinpath(*workspace.relative_folder.split("/")))
            candidate = str(Path(active_workspace).expanduser().resolve(strict=False)) if active_workspace else bound_path
            if not self._contains(bound_path, candidate):
                return finish(empty)
            active_workspace = candidate
        roots = []
        root_caps = {}
        is_admin = self.repository.subject_is_admin(subject)
        for root in self.list(owner):
            if not root.get("enabled") or root.get("availability") != "available":
                continue
            if root.get("workspace_id") and root["workspace_id"] != stable_id:
                continue
            if root.get("chat_id") and root["chat_id"] != chat_id:
                continue
            if root.get("lifetime", "always") == "workspace" and not stable_id:
                continue
            if root.get("lifetime") == "chat" and not chat_id:
                continue
            target_location = workspace.location_id if workspace else root["location_id"]
            caps = self.repository.people_capabilities(subject, target_location, workspace_id=stable_id or None,
                chat_id=chat_id, binding_class="agent") & set(root.get("physical_capabilities", root.get("capabilities")) or [])
            if not is_admin:
                caps &= self.repository.people_capabilities(subject, target_location, workspace_id=stable_id or None, chat_id=chat_id)
            if caps:
                roots.append(root)
                root_caps[root["id"]] = caps
        if not active_workspace:
            return finish({"approved_root_ids": sorted(root_caps), "root_capabilities": {key: sorted(value) for key, value in root_caps.items()}, "active_folder": None})
        path = str(Path(active_workspace).expanduser().resolve(strict=False))
        candidates = [root for root in roots if root["kind"] == "recursive_directory" and self._contains(root["canonical_path"], path)]
        if not candidates:
            return finish(empty)
        root = max(candidates, key=lambda row: len(row["canonical_path"]))
        identifier = root["id"]
        caps = sorted(root_caps[identifier])
        return finish({"approved_root_ids": [identifier], "root_capabilities": {identifier: caps},
                "active_folder": {"id": "folder-" + uuid.uuid5(uuid.NAMESPACE_URL, (stable_id or path) + ":" + identifier).hex,
                    "root_id": identifier, "canonical_path": path, "capabilities": caps}})
