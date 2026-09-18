"""Persistent owner-scoped agent filesystem roots.

This is the app/settings authority for the registry. The Rust service consumes
its serialized snapshot for enforcement; it is intentionally separate from
per-operation approval grants and Copal content.
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

from src.constants import DATA_DIR


class FilesystemRegistryError(ValueError):
    def __init__(self, message: str, *, code: str = "invalid_root") -> None:
        super().__init__(message)
        self.code = code


class FilesystemRootRegistry:
    VERSION = 1
    KINDS = {"exact_file", "recursive_directory"}
    CAPABILITIES = {"read", "write", "execute"}

    def __init__(self, path: str | os.PathLike[str] | None = None) -> None:
        self.path = Path(path) if path else Path(DATA_DIR) / "odysseus-file-roots.json"
        self._lock = threading.RLock()

    def _load_locked(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"version": self.VERSION, "generation": 0, "roots": {}, "visibility_assignments": {}}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise FilesystemRegistryError("filesystem root registry is unreadable", code="registry_unavailable") from error
        if not isinstance(data, dict) or data.get("version") != self.VERSION or not isinstance(data.get("roots"), dict):
            raise FilesystemRegistryError("filesystem root registry has an unsupported format", code="registry_unavailable")
        data.setdefault("generation", 0)
        data.setdefault("visibility_assignments", {})
        if not isinstance(data["visibility_assignments"], dict) or not isinstance(data["generation"], int):
            raise FilesystemRegistryError("filesystem root registry has an unsupported format", code="registry_unavailable")
        return data

    @staticmethod
    def _bump_generation(data: dict[str, Any]) -> int:
        data["generation"] = int(data.get("generation") or 0) + 1
        return data["generation"]

    def _write_locked(self, data: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=f".{self.path.name}.", dir=str(self.path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(data, handle, indent=2, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass

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
        """Move registry ownership and user assignments during account rename.

        Account names are still a compatibility wire field today; updating all
        references in one locked generation prevents a renamed account from
        losing its roots or a recreated name from inheriting stale rows.
        """
        old, new = str(old_owner), str(new_owner)
        if not old or not new or old == new:
            return
        with self._lock:
            data = self._load_locked()
            changed = False
            for root in data.get("roots", {}).values():
                if root.get("owner_id") == old:
                    root["owner_id"] = new
                    changed = True
            for assignment in data.get("visibility_assignments", {}).values():
                for key in ("subject_id", "issuer_id"):
                    if assignment.get(key) == old:
                        assignment[key] = new
                        changed = True
            if changed:
                self._bump_generation(data)
                self._write_locked(data)

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
        material = json.dumps(
            {"roots": root_ids, "assignments": assignment_ids},
            sort_keys=True,
            separators=(",", ":"),
        )
        return {
            "owner": target,
            "count": len(root_ids) + len(assignment_ids),
            "root_ids": root_ids,
            "assignment_ids": assignment_ids,
            "fingerprint": hashlib.sha256(material.encode("utf-8")).hexdigest(),
        }

    def preview_owner_rename(self, source_owner: str, target_owner: str) -> dict[str, Any]:
        source = self.owner_inventory(source_owner)
        target = self.owner_inventory(target_owner)
        if source["count"] and target["count"]:
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
        if expected is not None and not self._inventory_matches(before, expected):
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
        """Remove an account's roots and dependent visibility assignments."""
        target = str(owner)
        with self._lock:
            data = self._load_locked()
            roots = data.setdefault("roots", {})
            removed_root_ids = {
                root_id for root_id, root in roots.items()
                if root.get("owner_id") == target
            }
            changed = bool(removed_root_ids)
            for root_id in removed_root_ids:
                roots.pop(root_id, None)
            assignments = data.setdefault("visibility_assignments", {})
            for assignment_id, assignment in list(assignments.items()):
                if (
                    assignment.get("subject_id") == target
                    or assignment.get("issuer_id") == target
                    or assignment.get("root_id") in removed_root_ids
                ):
                    assignments.pop(assignment_id, None)
                    changed = True
            if changed:
                self._bump_generation(data)
                self._write_locked(data)

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
        with self._lock:
            generation = int(self._load_locked().get("generation") or 0)
        if is_admin:
            return {
                "host": True,
                "visible_root_ids": [],
                "capabilities": [],
                "root_capabilities": {},
                "generation": generation,
                "active_folder": None,
            }
        assignments = [
            assignment
            for assignment in self.visibility_for_subject(subject_id, groups)
            if assignment.get("root", {}).get("enabled")
            and assignment.get("root", {}).get("availability") == "available"
        ]
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

    def rust_snapshot_path(self) -> str:
        return str(self.path)

    def generation(self) -> int:
        """Return the current policy generation without projecting any scope."""
        with self._lock:
            return int(self._load_locked().get("generation") or 0)

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

    def agent_scope(
        self,
        owner: str,
        active_workspace: str | None = None,
        app_visibility: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Build the narrow Rust ``AgentScope`` for one authenticated turn.

        An active workspace never expands permissions: it selects the most
        specific enabled recursive root that contains the folder and binds an
        active-folder narrowing. If no root contains the folder, the returned
        approved set is empty, which makes the Rust service deny the request.
        With no active workspace, all enabled roots remain available for the
        caller that intentionally operates without a workspace binding.
        """
        roots = [root for root in self.list(owner) if root.get("enabled") and root.get("availability") == "available"]
        scoped_capabilities: dict[str, set[str]] = {
            str(root["id"]): set(str(value) for value in (root.get("capabilities") or []))
            for root in roots
        }
        if app_visibility is not None:
            # A non-admin agent root may only be a narrowing inside an
            # administrator-issued user-visible root, with no capability
            # elevation. The app scope is a ceiling; it is never inferred
            # from the active folder or from the request payload.
            ceilings = [
                item
                for item in app_visibility
                if isinstance(item, dict)
                and isinstance(item.get("root"), dict)
                and item["root"].get("enabled")
                and item["root"].get("availability") == "available"
            ]
            narrowed = []
            for root in roots:
                root_path = str(root.get("canonical_path") or "")
                root_caps = set(root.get("capabilities") or [])
                effective: set[str] | None = None
                for assignment in ceilings:
                    ceiling = assignment["root"]
                    ceiling_path = str(ceiling.get("canonical_path") or "")
                    contains = (
                        ceiling.get("kind") == "recursive_directory"
                        and self._contains(ceiling_path, root_path)
                    ) or (
                        ceiling.get("kind") == "exact_file"
                        and root.get("kind") == "exact_file"
                        and root_path == ceiling_path
                    )
                    if not contains:
                        continue
                    assignment_caps = set(str(value) for value in (assignment.get("capabilities") or []))
                    ceiling_caps = set(str(value) for value in (ceiling.get("capabilities") or []))
                    candidate = root_caps & assignment_caps & ceiling_caps
                    effective = candidate if effective is None else effective | candidate
                if effective:
                    narrowed.append(root)
                    scoped_capabilities[str(root["id"])] = effective
            roots = narrowed
        if not active_workspace:
            return {
                "approved_root_ids": sorted(str(root["id"]) for root in roots),
                "root_capabilities": {
                    root_id: sorted(scoped_capabilities[root_id])
                    for root_id in sorted(scoped_capabilities)
                    if root_id in {str(root["id"]) for root in roots}
                },
                "active_folder": None,
            }

        workspace = str(Path(active_workspace).expanduser().resolve(strict=False))
        candidates = [
            root for root in roots
            if root.get("kind") == "recursive_directory"
            and self._contains(str(root.get("canonical_path") or ""), workspace)
        ]
        if not candidates:
            return {"approved_root_ids": [], "root_capabilities": {}, "active_folder": None}
        root = max(candidates, key=lambda value: len(str(value.get("canonical_path") or "")))
        root_id = str(root["id"])
        capabilities = sorted(scoped_capabilities.get(root_id, set()))
        if not capabilities:
            return {"approved_root_ids": [], "root_capabilities": {}, "active_folder": None}
        return {
            "approved_root_ids": [root_id],
            "root_capabilities": {root_id: capabilities},
            "active_folder": {
                "id": f"folder-{root_id}-{uuid.uuid5(uuid.NAMESPACE_URL, workspace).hex}",
                "root_id": root_id,
                "canonical_path": workspace,
                "capabilities": capabilities,
            },
        }
