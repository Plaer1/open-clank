"""Inspectable loose-file Copal repository.

This is a storage-neutral compatibility seam for the existing ``CopalBridge``
operation vocabulary. Ordinary document files are canonical; ``.copal`` only
contains identity, revision, history, and trash metadata. Redb remains an
explicit rollback backend while the loose-file bridge is exercised.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import shutil
import stat
import tempfile
import time
import uuid
from pathlib import Path, PurePosixPath
from typing import Any

from src.openclank.copal_bridge import CopalBridgeError
from src.openclank.copal_commit_lock import copal_commit_lock
from src.openclank.copal_guarded import GuardedCommitCoordinator
from src.openclank.media_ownership import (
    MovePreflight,
    binary_digest,
    document_media_dir,
    plan_document_move,
    recover_move_plan,
    scan_references,
)


class LooseCopalRepository:
    VERSION = 1
    LIFECYCLE_VERSION = 1
    supports_task_index_lookup = True
    supports_keyed_task_index = True
    _OWNER_MARKER = ".copal-owner-lifecycle.json"

    def __init__(self, data_dir: str | os.PathLike[str]):
        self.data_dir = Path(data_dir).expanduser().resolve()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self._guarded = GuardedCommitCoordinator(self.data_dir)

    @staticmethod
    def _segment(value: str) -> str:
        raw = str(value or "").encode("utf-8", errors="strict")
        encoded = base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
        return encoded or "empty"

    def _vault(self, owner: str, workspace: str) -> Path:
        if not owner or not workspace:
            raise CopalBridgeError("owner and workspace are required")
        return self.data_dir / self._segment(owner) / self._segment(workspace)

    @staticmethod
    def _is_reserved_document_name(name: str, kind: str) -> bool:
        """Allow only Copal-owned logical records inside the metadata tree."""
        exact = {
            ("planning", ".copal/planning.json"),
            ("copal-tracks", ".copal/tracks.json"),
            ("copal-migration", ".copal/planning-migration.json"),
            ("treehouse-state", ".copal/treehouse-state.json"),
        }
        if (kind, name) in exact:
            return True
        path = PurePosixPath(name)
        if kind in {"copal-event", "copal-operation"}:
            expected_parent = {
                "copal-event": {"events"},
                "copal-operation": {"operations", "task-index", "task-actions"},
            }[kind]
            return (
                len(path.parts) == 3
                and path.parts[0] == ".copal"
                and path.parts[1] in expected_parent
                and path.suffix == ".json"
            )
        return (
            kind == "calendar-projection"
            and len(path.parts) == 2
            and path.parent.as_posix() == ".copal"
            and path.name.startswith("calendar-projection-")
            and path.suffix == ".json"
        )

    @classmethod
    def _safe_name(cls, name: str, kind: str = "") -> str:
        value = str(name or "").strip().replace("\\", "/")
        path = PurePosixPath(value)
        if (
            not value
            or path.is_absolute()
            or any(part in {"", ".", ".."} for part in path.parts)
            or (path.parts[0] == ".copal" and not cls._is_reserved_document_name(path.as_posix(), str(kind or "")))
        ):
            raise CopalBridgeError("invalid loose Copal document path")
        return path.as_posix()

    @staticmethod
    def _fingerprint(content: str) -> str:
        raw = content.encode("utf-8")
        return f"sha256:{hashlib.sha256(raw).hexdigest()}:{len(raw)}"

    @staticmethod
    def _validate_mutable_owner(owner: str) -> str:
        value = str(owner or "")
        if not value or value.strip() != value:
            raise CopalBridgeError("owner is required")
        if value.casefold() == "shared":
            raise CopalBridgeError("shared owner is immutable")
        return value

    def _owner_root(self, owner: str) -> Path:
        return self.data_dir / self._segment(owner)

    @staticmethod
    def _fsync_directory(path: Path) -> None:
        try:
            descriptor = os.open(path, os.O_RDONLY)
        except OSError:
            return
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    @staticmethod
    def _read_json_file(path: Path, *, label: str) -> dict[str, Any]:
        try:
            metadata = path.lstat()
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
                raise CopalBridgeError(f"{label} is not a regular file")
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise CopalBridgeError(f"{label} is unreadable") from exc
        if not isinstance(value, dict):
            raise CopalBridgeError(f"{label} is invalid")
        return value

    def _empty_owner_inventory(self, owner: str) -> dict[str, Any]:
        stable = json.dumps([], separators=(",", ":"))
        return {
            "schema_version": self.LIFECYCLE_VERSION,
            "owner": owner,
            "present": False,
            "workspaces": 0,
            "documents": 0,
            "active_documents": 0,
            "deleted_documents": 0,
            "files": 0,
            "bytes": 0,
            "record_owner_mismatches": 0,
            "fingerprint": self._fingerprint(stable),
            "content_included": False,
        }

    def _owner_inventory_at(self, owner: str, root: Path) -> dict[str, Any]:
        if not os.path.lexists(root):
            return self._empty_owner_inventory(owner)
        root_metadata = root.lstat()
        if stat.S_ISLNK(root_metadata.st_mode) or not stat.S_ISDIR(root_metadata.st_mode):
            raise CopalBridgeError("loose Copal owner root must be a real directory")

        stable_entries: list[Any] = []
        workspaces = 0
        documents = 0
        active_documents = 0
        deleted_documents = 0
        files = 0
        byte_count = 0
        owner_mismatches = 0
        for child in sorted(root.iterdir(), key=lambda item: item.name):
            metadata = child.lstat()
            if stat.S_ISLNK(metadata.st_mode):
                raise CopalBridgeError("symbolic links are not permitted in Copal owner data")
            if stat.S_ISDIR(metadata.st_mode):
                workspaces += 1
            elif child.name != self._OWNER_MARKER:
                raise CopalBridgeError("unexpected file in loose Copal owner root")

        for path in sorted(root.rglob("*"), key=lambda item: item.as_posix()):
            metadata = path.lstat()
            if stat.S_ISLNK(metadata.st_mode):
                raise CopalBridgeError("symbolic links are not permitted in Copal owner data")
            if stat.S_ISDIR(metadata.st_mode):
                continue
            if not stat.S_ISREG(metadata.st_mode):
                raise CopalBridgeError("special files are not permitted in Copal owner data")
            relative = path.relative_to(root).as_posix()
            if relative == self._OWNER_MARKER:
                continue
            files += 1
            byte_count += metadata.st_size
            if path.name == "manifest.json" and path.parent.name == ".copal":
                manifest = self._read_json_file(path, label="loose Copal manifest")
                if (
                    manifest.get("schemaVersion") != self.VERSION
                    or not isinstance(manifest.get("documents"), dict)
                    or not isinstance(manifest.get("operations", []), list)
                ):
                    raise CopalBridgeError("loose Copal manifest schema is unsupported")
                stable_documents = []
                for document_id, record in sorted(manifest["documents"].items()):
                    if not isinstance(record, dict):
                        raise CopalBridgeError("loose Copal manifest document is invalid")
                    documents += 1
                    if record.get("trashed"):
                        deleted_documents += 1
                    else:
                        active_documents += 1
                    if str(record.get("owner") or "") != owner:
                        owner_mismatches += 1
                    sanitized = {
                        key: value
                        for key, value in record.items()
                        if key != "owner"
                    }
                    stable_documents.append([str(document_id), sanitized])
                stable_entries.append(
                    [
                        "manifest",
                        relative,
                        stable_documents,
                        manifest.get("operations", []),
                    ]
                )
            else:
                # Hash bytes into the opaque inventory fingerprint. The digest
                # catches same-size out-of-band changes while receipts remain
                # content-free and owner renames never expose document bodies.
                digest = hashlib.sha256()
                with path.open("rb") as handle:
                    while block := handle.read(1024 * 1024):
                        digest.update(block)
                stable_entries.append(
                    ["file", relative, metadata.st_size, digest.hexdigest()]
                )

        encoded = json.dumps(
            stable_entries,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return {
            "schema_version": self.LIFECYCLE_VERSION,
            "owner": owner,
            "present": bool(files or documents),
            "workspaces": workspaces,
            "documents": documents,
            "active_documents": active_documents,
            "deleted_documents": deleted_documents,
            "files": files,
            "bytes": byte_count,
            "record_owner_mismatches": owner_mismatches,
            "fingerprint": self._fingerprint(encoded),
            "content_included": False,
        }

    def owner_inventory(self, owner: str) -> dict[str, Any]:
        value = self._validate_mutable_owner(owner)
        return self._owner_inventory_at(value, self._owner_root(value))

    def preview_owner_rename(self, old_owner: str, new_owner: str) -> dict[str, Any]:
        source_owner = self._validate_mutable_owner(old_owner)
        target_owner = self._validate_mutable_owner(new_owner)
        if source_owner == target_owner:
            raise CopalBridgeError("source and destination owners must differ")
        source = self.owner_inventory(source_owner)
        target = self.owner_inventory(target_owner)
        if source["present"] and target["present"]:
            raise CopalBridgeError("source and destination both contain Copal owner state")
        return {
            "schema_version": self.LIFECYCLE_VERSION,
            "source": source,
            "target": target,
            "content_included": False,
        }

    @staticmethod
    def _validate_expected_inventory(
        actual: dict[str, Any],
        expected: Any,
        *,
        label: str,
        expected_owner: str,
    ) -> None:
        if expected is None:
            return
        if not isinstance(expected, dict):
            raise CopalBridgeError(f"{label} lifecycle inventory is invalid")
        if (
            expected.get("schema_version") != LooseCopalRepository.LIFECYCLE_VERSION
            or expected.get("owner") != expected_owner
            or expected.get("content_included") is not False
        ):
            raise CopalBridgeError(f"{label} lifecycle inventory is invalid")
        stable_fields = (
            "present",
            "workspaces",
            "documents",
            "active_documents",
            "deleted_documents",
            "files",
            "fingerprint",
        )
        if any(expected.get(key) != actual.get(key) for key in stable_fields):
            raise CopalBridgeError(f"{label} Copal owner inventory changed")

    def _remove_empty_owner_root(self, root: Path) -> bool:
        if not os.path.lexists(root):
            return True
        for path in sorted(root.rglob("*"), key=lambda item: len(item.parts), reverse=True):
            metadata = path.lstat()
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
                return False
            path.rmdir()
        root.rmdir()
        self._fsync_directory(self.data_dir)
        return True

    def _rewrite_owner_manifests(self, root: Path, old_owner: str, new_owner: str) -> None:
        for path in sorted(root.glob("*/.copal/manifest.json")):
            manifest = self._read_json_file(path, label="loose Copal manifest")
            documents = manifest.get("documents")
            if manifest.get("schemaVersion") != self.VERSION or not isinstance(documents, dict):
                raise CopalBridgeError("loose Copal manifest schema is unsupported")
            changed = False
            for record in documents.values():
                if not isinstance(record, dict):
                    raise CopalBridgeError("loose Copal manifest document is invalid")
                record_owner = str(record.get("owner") or "")
                if record_owner == old_owner:
                    record["owner"] = new_owner
                    changed = True
                elif record_owner != new_owner:
                    raise CopalBridgeError("loose Copal manifest contains split owner state")
            if changed:
                self._atomic_write(
                    path,
                    json.dumps(
                        manifest,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                )

    def reconcile_owner(
        self,
        old_owner: str,
        new_owner: str,
        *,
        expected_source: Any = None,
        expected_target: Any = None,
    ) -> dict[str, Any]:
        source_owner = self._validate_mutable_owner(old_owner)
        target_owner = self._validate_mutable_owner(new_owner)
        if source_owner == target_owner:
            raise CopalBridgeError("source and destination owners must differ")
        source_root = self._owner_root(source_owner)
        target_root = self._owner_root(target_owner)

        # Pure reads can leave empty scope directories behind. They carry no
        # owner state and must not make a later lifecycle move look split.
        if os.path.lexists(target_root):
            target_probe = self._owner_inventory_at(target_owner, target_root)
            if not target_probe["present"] and target_probe["files"] == 0:
                self._remove_empty_owner_root(target_root)

        source_exists = os.path.lexists(source_root)
        target_exists = os.path.lexists(target_root)
        if source_exists and target_exists:
            raise CopalBridgeError("source and destination both contain Copal owner state")

        if not source_exists and not target_exists:
            source_before = self._empty_owner_inventory(source_owner)
            target_before = self._empty_owner_inventory(target_owner)
            self._validate_expected_inventory(
                source_before,
                expected_source,
                label="source",
                expected_owner=source_owner,
            )
            self._validate_expected_inventory(
                target_before,
                expected_target,
                label="destination",
                expected_owner=target_owner,
            )
            return {
                "schema_version": self.LIFECYCLE_VERSION,
                "operation": "reconcile_owner",
                "state": "empty",
                "documents": 0,
                "changed_documents": 0,
                "source_before": source_before,
                "source_after": source_before,
                "target_before": target_before,
                "target_after": target_before,
                "content_included": False,
            }

        if not source_exists:
            marker_path = target_root / self._OWNER_MARKER
            if not marker_path.is_file():
                raise CopalBridgeError("destination owner already has Copal data")
            marker = self._read_json_file(marker_path, label="Copal owner lifecycle marker")
            if (
                marker.get("schemaVersion") != self.LIFECYCLE_VERSION
                or marker.get("operation") != "reconcile_owner"
                or marker.get("oldOwner") != source_owner
                or marker.get("newOwner") != target_owner
            ):
                raise CopalBridgeError("destination Copal owner state is not a recognized replay")
            target_before = self._owner_inventory_at(target_owner, target_root)
            if marker.get("sourceFingerprint") != target_before["fingerprint"]:
                raise CopalBridgeError("destination Copal owner state changed after lifecycle move")
            self._validate_expected_inventory(
                target_before,
                expected_source,
                label="source",
                expected_owner=source_owner,
            )
            self._validate_expected_inventory(
                self._empty_owner_inventory(target_owner),
                expected_target,
                label="destination",
                expected_owner=target_owner,
            )
            self._rewrite_owner_manifests(target_root, source_owner, target_owner)
            target_after = self._owner_inventory_at(target_owner, target_root)
            marker["state"] = "complete"
            marker["targetFingerprint"] = target_after["fingerprint"]
            self._atomic_write(
                marker_path,
                json.dumps(marker, sort_keys=True, separators=(",", ":")),
            )
            source_after = self._empty_owner_inventory(source_owner)
            return {
                "schema_version": self.LIFECYCLE_VERSION,
                "operation": "reconcile_owner",
                "state": "already_applied",
                "documents": target_after["documents"],
                "changed_documents": 0,
                "source_before": target_before,
                "source_after": source_after,
                "target_before": self._empty_owner_inventory(target_owner),
                "target_after": target_after,
                "content_included": False,
            }

        source_before = self._owner_inventory_at(source_owner, source_root)
        target_before = self._empty_owner_inventory(target_owner)
        self._validate_expected_inventory(
            source_before,
            expected_source,
            label="source",
            expected_owner=source_owner,
        )
        self._validate_expected_inventory(
            target_before,
            expected_target,
            label="destination",
            expected_owner=target_owner,
        )
        marker_path = source_root / self._OWNER_MARKER
        if marker_path.exists():
            prior = self._read_json_file(marker_path, label="Copal owner lifecycle marker")
            if (
                prior.get("state") == "pending"
                and (
                    prior.get("oldOwner") != source_owner
                    or prior.get("newOwner") != target_owner
                    or prior.get("sourceFingerprint") != source_before["fingerprint"]
                )
            ):
                raise CopalBridgeError("another Copal owner lifecycle move is unfinished")
        marker = {
            "schemaVersion": self.LIFECYCLE_VERSION,
            "operation": "reconcile_owner",
            "state": "pending",
            "oldOwner": source_owner,
            "newOwner": target_owner,
            "sourceFingerprint": source_before["fingerprint"],
            "documents": source_before["documents"],
        }
        self._atomic_write(
            marker_path,
            json.dumps(marker, sort_keys=True, separators=(",", ":")),
        )
        os.replace(source_root, target_root)
        self._fsync_directory(self.data_dir)
        self._rewrite_owner_manifests(target_root, source_owner, target_owner)
        target_after = self._owner_inventory_at(target_owner, target_root)
        if target_after["fingerprint"] != source_before["fingerprint"]:
            raise CopalBridgeError("Copal owner move did not preserve its inventory")
        marker["state"] = "complete"
        marker["targetFingerprint"] = target_after["fingerprint"]
        self._atomic_write(
            target_root / self._OWNER_MARKER,
            json.dumps(marker, sort_keys=True, separators=(",", ":")),
        )
        return {
            "schema_version": self.LIFECYCLE_VERSION,
            "operation": "reconcile_owner",
            "state": "applied",
            "documents": target_after["documents"],
            "changed_documents": source_before["documents"],
            "source_before": source_before,
            "source_after": self._empty_owner_inventory(source_owner),
            "target_before": target_before,
            "target_after": target_after,
            "content_included": False,
        }

    rename_owner = reconcile_owner

    def compensate_owner_rename(
        self,
        old_owner: str,
        new_owner: str,
        manifest: dict[str, Any],
    ) -> dict[str, Any]:
        """Restore a complete or interrupted reconcile to its frozen source."""
        source_owner = self._validate_mutable_owner(old_owner)
        target_owner = self._validate_mutable_owner(new_owner)
        expected = dict((manifest or {}).get("source") or {})
        expected_target = dict((manifest or {}).get("target") or {})
        if source_owner == target_owner or not expected or not expected_target:
            raise CopalBridgeError("invalid Copal owner lifecycle manifest")
        source_root = self._owner_root(source_owner)
        target_root = self._owner_root(target_owner)
        source_exists = os.path.lexists(source_root)
        target_exists = os.path.lexists(target_root)
        if source_exists and target_exists:
            raise CopalBridgeError("source and destination both contain Copal owner state")

        expected_present = bool(expected.get("present"))
        if not expected_present:
            source = self.owner_inventory(source_owner)
            target = self.owner_inventory(target_owner)
            self._validate_expected_inventory(
                source,
                expected,
                label="source",
                expected_owner=source_owner,
            )
            self._validate_expected_inventory(
                target,
                expected_target,
                label="destination",
                expected_owner=target_owner,
            )
            return {
                "schema_version": self.LIFECYCLE_VERSION,
                "operation": "compensate_owner",
                "state": "empty",
                "source": source,
                "target": target,
                "content_included": False,
            }

        if target_exists:
            target = self._owner_inventory_at(target_owner, target_root)
            self._validate_expected_inventory(
                target,
                expected,
                label="source",
                expected_owner=source_owner,
            )
            marker_path = target_root / self._OWNER_MARKER
            if not marker_path.is_file():
                raise CopalBridgeError("destination Copal state is not a recognized lifecycle move")
            marker = self._read_json_file(marker_path, label="Copal owner lifecycle marker")
            if (
                marker.get("schemaVersion") != self.LIFECYCLE_VERSION
                or marker.get("operation") != "reconcile_owner"
                or marker.get("oldOwner") != source_owner
                or marker.get("newOwner") != target_owner
                or marker.get("sourceFingerprint") != expected.get("fingerprint")
            ):
                raise CopalBridgeError("destination Copal lifecycle marker does not match")
            self._rewrite_owner_manifests(target_root, target_owner, source_owner)
            os.replace(target_root, source_root)
            self._fsync_directory(self.data_dir)
            state = "compensated"
        elif source_exists:
            state = "already_compensated"
        else:
            raise CopalBridgeError("Copal owner state disappeared during compensation")

        self._rewrite_owner_manifests(source_root, target_owner, source_owner)
        marker_path = source_root / self._OWNER_MARKER
        if marker_path.exists():
            marker = self._read_json_file(marker_path, label="Copal owner lifecycle marker")
            if (
                marker.get("oldOwner") != source_owner
                or marker.get("newOwner") != target_owner
                or marker.get("sourceFingerprint") != expected.get("fingerprint")
            ):
                raise CopalBridgeError("Copal lifecycle marker changed before compensation")
            marker_path.unlink()
            self._fsync_directory(source_root)
        source_after = self.owner_inventory(source_owner)
        target_after = self.owner_inventory(target_owner)
        self._validate_expected_inventory(
            source_after,
            expected,
            label="source",
            expected_owner=source_owner,
        )
        self._validate_expected_inventory(
            target_after,
            expected_target,
            label="destination",
            expected_owner=target_owner,
        )
        return {
            "schema_version": self.LIFECYCLE_VERSION,
            "operation": "compensate_owner",
            "state": state,
            "source": source_after,
            "target": target_after,
            "content_included": False,
        }

    def _purge_journal_path(self, owner: str) -> tuple[Path, Path]:
        token = hashlib.sha256(f"copal-purge\0{owner}".encode("utf-8")).hexdigest()
        journal_dir = self.data_dir / ".copal-lifecycle"
        return journal_dir, journal_dir / f"{token}.json"

    def purge_owner(self, owner: str, *, expected: Any = None) -> dict[str, Any]:
        target_owner = self._validate_mutable_owner(owner)
        source_root = self._owner_root(target_owner)
        stage_root = self.data_dir / f".copal-purge-{self._segment(target_owner)}"
        journal_dir, journal_path = self._purge_journal_path(target_owner)
        source_exists = os.path.lexists(source_root)
        stage_exists = os.path.lexists(stage_root)
        journal_exists = os.path.lexists(journal_path)
        if source_exists and stage_exists:
            raise CopalBridgeError("Copal purge source and staging state are both present")

        journal = None
        if journal_exists:
            journal = self._read_json_file(journal_path, label="Copal purge journal")
            if (
                journal.get("schemaVersion") != self.LIFECYCLE_VERSION
                or journal.get("operation") != "purge_owner"
                or journal.get("owner") != target_owner
                or journal.get("stage") != stage_root.name
            ):
                raise CopalBridgeError("Copal purge journal is invalid")

        if source_exists:
            before = self._owner_inventory_at(target_owner, source_root)
            self._validate_expected_inventory(
                before,
                expected,
                label="source",
                expected_owner=target_owner,
            )
            if journal is None:
                journal = {
                    "schemaVersion": self.LIFECYCLE_VERSION,
                    "operation": "purge_owner",
                    "owner": target_owner,
                    "stage": stage_root.name,
                    "before": before,
                }
                journal_dir.mkdir(parents=True, exist_ok=True)
                self._atomic_write(
                    journal_path,
                    json.dumps(journal, sort_keys=True, separators=(",", ":")),
                )
                self._fsync_directory(journal_dir)
            elif journal.get("before", {}).get("fingerprint") != before["fingerprint"]:
                raise CopalBridgeError("Copal purge source changed after journal creation")
            os.replace(source_root, stage_root)
            self._fsync_directory(self.data_dir)
        elif stage_exists:
            if journal is None:
                raise CopalBridgeError("unrecognized Copal purge staging directory")
            before = dict(journal.get("before") or {})
            self._validate_expected_inventory(
                before,
                expected,
                label="source",
                expected_owner=target_owner,
            )
        else:
            before = dict((journal or {}).get("before") or self._empty_owner_inventory(target_owner))
            self._validate_expected_inventory(
                before,
                expected,
                label="source",
                expected_owner=target_owner,
            )
            if journal_exists:
                journal_path.unlink()
                self._fsync_directory(journal_dir)
                try:
                    journal_dir.rmdir()
                except OSError:
                    pass
            return {
                "schema_version": self.LIFECYCLE_VERSION,
                "operation": "purge_owner",
                "state": "already_applied" if journal else "empty",
                "documents": int(before.get("documents") or 0),
                "deleted_files": int(before.get("files") or 0),
                "deleted_bytes": int(before.get("bytes") or 0),
                "before": before,
                "after": self._empty_owner_inventory(target_owner),
                "content_included": False,
                "physical_compaction": True,
                "history_retained": False,
                "history_policy": "physically_deleted",
            }

        # Inventory traversal rejects symlinks and special files before the
        # recursive removal, so lifecycle purges never follow an external path.
        self._owner_inventory_at(target_owner, stage_root)
        shutil.rmtree(stage_root)
        self._fsync_directory(self.data_dir)
        journal_path.unlink()
        self._fsync_directory(journal_dir)
        try:
            journal_dir.rmdir()
        except OSError:
            pass
        return {
            "schema_version": self.LIFECYCLE_VERSION,
            "operation": "purge_owner",
            "state": "applied",
            "documents": int(before.get("documents") or 0),
            "deleted_files": int(before.get("files") or 0),
            "deleted_bytes": int(before.get("bytes") or 0),
            "before": before,
            "after": self._empty_owner_inventory(target_owner),
            "content_included": False,
            "physical_compaction": True,
            "history_retained": False,
            "history_policy": "physically_deleted",
        }

    @staticmethod
    def _atomic_write(path: Path, content: str | bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
        try:
            mode = "wb" if isinstance(content, bytes) else "w"
            with os.fdopen(fd, mode, encoding=None if mode == "wb" else "utf-8", newline="" if mode == "w" else None) as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
            LooseCopalRepository._fsync_directory(path.parent)
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass

    def _manifest_path(self, vault: Path) -> Path:
        return vault / ".copal" / "manifest.json"

    @staticmethod
    def _task_index_dir(vault: Path) -> Path:
        return vault / ".copal" / "task-index-keyed"

    @classmethod
    def _recover_task_index(cls, vault: Path) -> None:
        """Finish or roll back an interrupted staged index replacement."""
        parent = vault / ".copal"
        root = cls._task_index_dir(vault)
        backup = parent / f"{root.name}.old"
        stages = sorted(parent.glob(f"{root.name}.rebuild-*"), key=lambda item: item.name)
        if not root.exists() and backup.exists():
            os.replace(backup, root)
        if root.exists() and backup.exists():
            shutil.rmtree(backup)
        for stage in stages:
            if stage.exists():
                shutil.rmtree(stage)

    @staticmethod
    def _task_index_document_name(document_id: str) -> str:
        return f"doc-{base64.urlsafe_b64encode(document_id.encode()).decode().rstrip('=') or 'empty'}.json"

    @staticmethod
    def _task_index_row_key(item: dict[str, Any]) -> str:
        label = str(item.get("label") or item.get("text") or "").casefold()
        item_id = str(item.get("id") or "")
        return f"{label.encode('utf-8').hex()}--{item_id.encode('utf-8').hex()}"

    def _task_index_get(self, vault: Path, *, ids: list[str] | None = None) -> dict[str, Any]:
        self._recover_task_index(vault)
        root = self._task_index_dir(vault)
        generation_path = root / "generation.json"
        generation = ""
        total = 0
        counts: dict[str, dict[str, int]] = {}
        if generation_path.is_file():
            try:
                value = json.loads(generation_path.read_text(encoding="utf-8"))
                if isinstance(value, dict):
                    generation = str(value.get("sourceRevision") or "")
                    total = int(value.get("total") or 0)
                    counts = value.get("counts") if isinstance(value.get("counts"), dict) else {}
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                generation = ""
        wanted = ids if ids is not None else [
            path.stem.removeprefix("doc-")
            for path in root.glob("doc-*.json")
        ]
        records: dict[str, Any] = {}
        for encoded_id in wanted:
            if ids is None:
                padding = "=" * (-len(encoded_id) % 4)
                try:
                    document_id = base64.urlsafe_b64decode((encoded_id + padding).encode()).decode()
                except (ValueError, UnicodeDecodeError):
                    continue
            else:
                document_id = encoded_id
            path = root / self._task_index_document_name(document_id)
            if not path.is_file():
                continue
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise CopalBridgeError("loose task index record is unreadable") from exc
            if isinstance(value, dict):
                records[document_id] = value
        return {"schemaVersion": 1, "sourceRevision": generation, "total": total, "counts": counts, "documents": records}

    def _task_index_generation(self, vault: Path) -> dict[str, Any]:
        state = self._task_index_get(vault)
        return {"schemaVersion": 1, "sourceRevision": state["sourceRevision"], "total": state.get("total", 0), "counts": state.get("counts") or {}}

    def _task_index_resolve(self, vault: Path, resource_id: str) -> dict[str, Any]:
        path = self._task_index_dir(vault) / "resources" / f"{self._segment(resource_id)}.json"
        if not path.is_file():
            return {}
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise CopalBridgeError("loose task index resource record is unreadable") from exc
        return value if isinstance(value, dict) else {}

    def _task_index_update(
        self,
        vault: Path,
        generation: str,
        records: dict[str, Any],
        removed: list[str],
        rebuild: bool,
        source_reads: int = 0,
    ) -> dict[str, Any]:
        self._recover_task_index(vault)
        root = self._task_index_dir(vault)
        target_root = root.with_name(f"{root.name}.rebuild-{uuid.uuid4().hex}")
        target_root.mkdir(parents=True, exist_ok=True)
        rows_dir = target_root / "rows"
        rows_dir.mkdir(parents=True, exist_ok=True)
        resources_dir = target_root / "resources"
        resources_dir.mkdir(parents=True, exist_ok=True)
        old_rows_dir = root / "rows"
        old_resources_dir = root / "resources"
        if not rebuild and root.exists():
            shutil.rmtree(target_root)
            shutil.copytree(root, target_root, copy_function=os.link)
            rows_dir = target_root / "rows"
            resources_dir = target_root / "resources"
        rewritten_rows = 0
        rewritten_bytes = 0
        prior_total = int(self._task_index_get(vault).get("total") or 0)
        task_total = 0 if rebuild else prior_total
        counts = {str(source): {str(checked): int(value) for checked, value in values.items()} for source, values in (self._task_index_get(vault).get("counts") or {}).items()} if not rebuild else {}
        changed = {str(document_id): record for document_id, record in records.items() if isinstance(record, dict)}
        for document_id, record in {**changed, **{value: None for value in removed}}.items():
            old_path = root / self._task_index_document_name(document_id)
            if not rebuild and old_path.is_file():
                try:
                    old = json.loads(old_path.read_text(encoding="utf-8"))
                except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                    old = {}
                for item in old.get("items") or [] if isinstance(old, dict) else []:
                    if isinstance(item, dict):
                        task_total = max(0, task_total - 1)
                        source = str(item.get("source") or "")
                        checked = str(bool(item.get("checked"))).lower()
                        counts.setdefault(source, {}).setdefault(checked, 0)
                        counts[source][checked] = max(0, counts[source][checked] - 1)
                        old_row = old_rows_dir / f"{self._task_index_row_key(item)}.json"
                        if old_row.is_file():
                            rewritten_rows += 1
                            rewritten_bytes += old_row.stat().st_size
                        (rows_dir / old_row.name).unlink(missing_ok=True)
                old_resource = str(old.get("resourceId") or "") if isinstance(old, dict) else ""
                if old_resource:
                    (resources_dir / f"{self._segment(old_resource)}.json").unlink(missing_ok=True)
            if document_id in removed or document_id not in changed:
                if not rebuild and old_path.is_file():
                    rewritten_rows += 1
                    rewritten_bytes += old_path.stat().st_size
                (target_root / old_path.name).unlink(missing_ok=True)
        for document_id, record in changed.items():
            encoded = json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            self._atomic_write(target_root / self._task_index_document_name(document_id), encoded)
            rewritten_rows += 1
            rewritten_bytes += len(encoded.encode("utf-8"))
            for item in record.get("items") or []:
                if not isinstance(item, dict):
                    continue
                row = json.dumps(item, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                self._atomic_write(rows_dir / f"{self._task_index_row_key(item)}.json", row)
                rewritten_rows += 1
                rewritten_bytes += len(row.encode("utf-8"))
                task_total += 1
                source = str(item.get("source") or "")
                checked = str(bool(item.get("checked"))).lower()
                counts.setdefault(source, {}).setdefault(checked, 0)
                counts[source][checked] += 1
            resource_id = str(record.get("resourceId") or "")
            if resource_id:
                self._atomic_write(resources_dir / f"{self._segment(resource_id)}.json", json.dumps({"id": document_id}, separators=(",", ":")))
                rewritten_rows += 1
                rewritten_bytes += len(document_id.encode("utf-8"))
        for document_id in removed:
            # Resource mappings are keyed by opaque resource id, so remove
            # the old mapping by reading the prior document record only.
            old_path = root / self._task_index_document_name(document_id)
            if old_path.is_file():
                try:
                    old = json.loads(old_path.read_text(encoding="utf-8"))
                except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                    old = {}
                resource_id = str(old.get("resourceId") or "") if isinstance(old, dict) else ""
                if resource_id:
                    (resources_dir / f"{self._segment(resource_id)}.json").unlink(missing_ok=True)
        generation_record = {"sourceRevision": generation, "total": task_total, "counts": counts}
        generation_encoded = json.dumps(generation_record, separators=(",", ":"))
        self._atomic_write(target_root / "generation.json", generation_encoded)
        rewritten_rows += 1
        rewritten_bytes += len(generation_encoded.encode("utf-8"))
        backup = root.with_name(f"{root.name}.old")
        if backup.exists():
            shutil.rmtree(backup)
        if root.exists():
            os.replace(root, backup)
        os.replace(target_root, root)
        if backup.exists():
            shutil.rmtree(backup)
        self._fsync_directory(root.parent)
        return {"outcome": "applied", "sourceRevision": generation, "sourceReads": int(source_reads), "rewrittenRows": rewritten_rows, "rewrittenBytes": rewritten_bytes}

    def _task_index_page(self, vault: Path, args: dict[str, Any]) -> dict[str, Any]:
        state = self._task_index_generation(vault)
        generation = str(args.get("generation") or "")
        if state["sourceRevision"] != generation:
            raise CopalBridgeError("stale_cursor")
        root = self._task_index_dir(vault) / "rows"
        query = str(args.get("query") or "").casefold()
        source = str(args.get("source") or "all")
        completed = args.get("completed")
        cursor = str(args.get("cursor") or "")
        limit = max(1, min(int(args.get("limit") or 100), 500))
        after = not cursor
        cursor_seen = not cursor
        items: list[dict[str, Any]] = []
        scanned_rows = scanned_bytes = 0
        next_cursor = None
        matched_total = None
        if not query:
            sources = [source] if source != "all" else list((state.get("counts") or {}).keys())
            matched_total = sum(
                int((state.get("counts") or {}).get(value, {}).get(str(bool(completed)).lower(), 0))
                if completed is not None
                else sum(int(item) for item in (state.get("counts") or {}).get(value, {}).values())
                for value in sources
            )
        for path in sorted(root.glob("*.json"), key=lambda item: item.name):
            if not after:
                if path.stem == cursor:
                    after = True
                    cursor_seen = True
                continue
            try:
                raw = path.read_bytes()
                item = json.loads(raw.decode("utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise CopalBridgeError("loose task index row is unreadable") from exc
            scanned_rows += 1
            scanned_bytes += len(raw)
            if source != "all" and item.get("source") != source:
                continue
            if completed is not None and item.get("checked") != (str(completed).lower() == "true"):
                continue
            if query and query not in f"{item.get('text', '')} {item.get('label', '')}".casefold():
                continue
            if len(items) >= limit:
                # The cursor is the last row returned, treated as an
                # exclusive anchor on the next request. Using the current
                # candidate would silently skip that candidate.
                next_cursor = items[-1].get("_taskIndexKey")
                break
            if isinstance(item, dict):
                item["_taskIndexKey"] = path.stem
                items.append(item)
        if not cursor_seen:
            raise CopalBridgeError("stale_cursor")
        # The filename is an implementation cursor and must never leak into
        # task DTOs returned to the route/UI.
        for item in items:
            item.pop("_taskIndexKey", None)
        indexed_total = int(state.get("total") or 0)
        return {"items": items, "nextCursor": next_cursor, "sourceRevision": generation, "total": matched_total if matched_total is not None else indexed_total, "indexedTotal": indexed_total, "matchedTotal": matched_total, "totalExact": matched_total is not None, "scannedRows": scanned_rows, "scannedBytes": scanned_bytes, "returnedRows": len(items), "sourceReads": 0, "rewrittenRows": 0, "rewrittenBytes": 0}

    def _load(self, vault: Path) -> dict[str, Any]:
        path = self._manifest_path(vault)
        if not path.exists():
            return {"schemaVersion": self.VERSION, "documents": {}, "operations": []}
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise CopalBridgeError("loose Copal manifest is unreadable") from exc
        if not isinstance(value, dict) or value.get("schemaVersion") != self.VERSION or not isinstance(value.get("documents"), dict):
            raise CopalBridgeError("loose Copal manifest schema is unsupported")
        value.setdefault("operations", [])
        return value

    def _save(self, vault: Path, manifest: dict[str, Any]) -> None:
        self._atomic_write(self._manifest_path(vault), json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")))

    @staticmethod
    def _read_content(path: Path) -> str:
        with path.open("r", encoding="utf-8", newline="") as handle:
            return handle.read()

    def _record_doc(self, vault: Path, record: dict[str, Any], *, include_body: bool) -> dict[str, Any]:
        storage_path = record.get("trashPath") if record.get("trashed") else record.get("path")
        path = vault / str(storage_path or record["path"])
        result = dict(record)
        result.pop("path", None)
        result["name"] = record["name"]
        result["hidden"] = any(
            component.startswith(".") and len(component) > 1
            for component in PurePosixPath(str(record["name"])).parts
        )
        result["storage"] = "files"
        result["format"] = "copal-loose-v1"
        try:
            result["size"] = path.stat().st_size
        except FileNotFoundError as exc:
            raise CopalBridgeError("loose Copal document file is missing") from exc
        if include_body:
            try:
                result["text"] = self._read_content(path)
                result["head"] = self._fingerprint(result["text"])
            except FileNotFoundError as exc:
                raise CopalBridgeError("loose Copal document file is missing") from exc
        return result

    def _scope(self, args: dict[str, Any]) -> tuple[Path, dict[str, Any]]:
        owner = str(args.get("owner") or "")
        workspace = str(args.get("workspace_id") or "")
        vault = self._vault(owner, workspace)
        vault.mkdir(parents=True, exist_ok=True)
        return vault, self._load(vault)

    @staticmethod
    def _operation(manifest: dict[str, Any], kind: str, description: str, *, document_id: str | None = None, action_id: str | None = None) -> None:
        operations = manifest.setdefault("operations", [])
        operation_id = f"op_{uuid.uuid4().hex}"
        entry = {
            "id": operation_id,
            "op": operation_id,
            "parent": operations[-1].get("id") if operations else None,
            "kind": kind,
            "description": description,
            "documentId": document_id,
            "changedIds": [document_id] if document_id else [],
            "createdAt": time.time(),
        }
        if action_id: entry["actionId"] = action_id
        operations.append(entry)
        if len(operations) > 1000:
            del operations[:-1000]

    def _doc(self, manifest: dict[str, Any], document_id: str) -> dict[str, Any]:
        record = manifest.get("documents", {}).get(document_id)
        if not isinstance(record, dict) or record.get("trashed"):
            raise CopalBridgeError("document not found in this scope")
        return record

    @staticmethod
    def _history_dir(vault: Path, record: dict[str, Any]) -> Path:
        return vault / ".copal" / "history" / str(record["id"])

    def _history_changes(self, vault: Path, record: dict[str, Any]) -> list[dict[str, Any]]:
        changes: list[dict[str, Any]] = [{
            "commit": str(record.get("head") or ""),
            "ts": record.get("updatedAt") or record.get("createdAt") or time.time(),
            "name": record.get("name"),
            "message": None,
            "amends": [],
        }]
        history_dir = self._history_dir(vault, record)
        if history_dir.is_dir():
            snapshots = []
            for path in history_dir.glob("*.json"):
                try:
                    snapshot = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                    continue
                if not isinstance(snapshot, dict) or not isinstance(snapshot.get("content"), str):
                    continue
                snapshots.append({
                    "commit": str(snapshot.get("commit") or path.stem),
                    "ts": snapshot.get("ts") or path.stat().st_mtime,
                    "name": snapshot.get("name") or record.get("name"),
                    "message": snapshot.get("message"),
                    "amends": [],
                })
            changes.extend(sorted(snapshots, key=lambda item: float(item.get("ts") or 0), reverse=True))
        return changes

    def _write_record(self, vault: Path, manifest: dict[str, Any], record: dict[str, Any], content: str, *, operation: str, action_id: str | None = None) -> dict[str, Any]:
        if record.get("readOnly"):
            raise CopalBridgeError("document is read-only")
        path = vault / str(record["path"])
        previous = record.get("head")
        history_dir = self._history_dir(vault, record)
        if previous and path.exists():
            self._atomic_write(
                history_dir / f"{previous}.json",
                json.dumps({"head": previous, "commit": previous, "content": self._read_content(path), "name": record.get("name"), "ts": time.time(), "message": None}, ensure_ascii=False, separators=(",", ":")),
            )
        self._atomic_write(path, content)
        record["head"] = self._fingerprint(content)
        try:
            record["mtime_ns"] = path.stat().st_mtime_ns
        except OSError:
            record.pop("mtime_ns", None)
        record["updatedAt"] = time.time()
        self._operation(manifest, operation, f"{operation} {record['name']}", document_id=record["id"], action_id=action_id)
        self._save(vault, manifest)
        return self._record_doc(vault, record, include_body=True)

    def _plan_and_apply_media_move(
        self,
        vault: Path,
        manifest: dict[str, Any],
        record: dict[str, Any],
        destination_name: str,
    ) -> dict[str, Any]:
        """Repair app-known media references during a document rename.

        Uses the S14 move planner so the mirrored ``media/<stem>/`` folder
        follows the document and known authorized references are rewritten
        surgically.  A protected or read-only reference leaves the move
        unapplied with a concrete conflict instead of being broken.  Returns
        one resource-change receipt describing what was staged.
        """
        source_path = str(record.get("path") or "")
        if not source_path:
            return {"status": "noop"}
        # Loose Copal treats the vault as the document's canonical root.
        try:
            source_layout = document_media_dir(
                document_path=source_path,
                canonical_root=str(vault),
                origin="workspace",
                owner_subject_id=str(record.get("owner") or ""),
            )
            destination_layout = document_media_dir(
                document_path=destination_name,
                canonical_root=str(vault),
                origin="workspace",
                owner_subject_id=str(record.get("owner") or ""),
            )
        except Exception:
            return {"status": "noop"}
        source_media_abs = Path(source_layout.absolute_media_dir())
        destination_media_abs = Path(destination_layout.absolute_media_dir())
        assets: list[dict[str, str]] = []
        if source_media_abs.is_dir():
            for child in sorted(source_media_abs.iterdir()):
                if not child.is_file():
                    continue
                try:
                    data = child.read_bytes()
                except OSError:
                    continue
                assets.append({
                    "name": child.name,
                    "digest": binary_digest(data),
                    "asset_id": child.name,
                    "owner_document_id": str(record.get("id") or ""),
                })
        # App-known authorized references are the other live documents in this
        # vault.  Trashed/read-only documents are treated as protected so a
        # rename never silently breaks them.
        referencing: list[tuple[str, str, bool]] = []
        incoming: list = []
        source_rel = source_path
        source_media_rel = source_layout.media_dir
        for other_id, other in (manifest.get("documents") or {}).items():
            if not isinstance(other, dict) or other.get("trashed") or other.get("id") == record.get("id"):
                continue
            other_path = vault / str(other.get("path") or "")
            if not other_path.is_file():
                continue
            try:
                text = other_path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            sites = scan_references(str(other_id), text, protected=bool(other.get("readOnly")))
            touched = False
            for site in sites:
                target = str(site.target or "").replace("\\", "/").lstrip("./")
                if not target:
                    continue
                points_at_document = target in {source_rel, PurePosixPath(source_rel).name, "/" + source_rel}
                # Only this document's own media/<stem>/ counts; a reference to
                # some other document's media/... is unrelated to this move.
                points_at_media = target == source_media_rel or target.startswith(source_media_rel + "/")
                if not (points_at_document or points_at_media):
                    continue
                touched = True
                from src.openclank.media_ownership import IncomingReference
                incoming.append(IncomingReference(
                    document_id=str(other_id),
                    source=str(other.get("path") or ""),
                    target=str(site.target or ""),
                    protected=bool(other.get("readOnly")),
                    writable=not bool(other.get("readOnly")),
                ))
            if touched:
                referencing.append((str(other_id), text, bool(other.get("readOnly"))))
        expected = str(record.get("head") or "")
        current = expected
        source_file = vault / source_path
        if source_file.is_file():
            try:
                current = self._fingerprint(source_file.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError):
                current = expected
        from src.openclank.media_ownership import MovePhase
        plan = plan_document_move(
            operation_id=f"rename-{record.get('id')}-{uuid.uuid4().hex[:8]}",
            document_id=str(record.get("id") or ""),
            source_document_path=source_path,
            destination_document_path=destination_name,
            canonical_root=str(vault),
            origin="workspace",
            owner_subject_id=str(record.get("owner") or ""),
            preflight=MovePreflight(
                expected_revision=expected,
                current_revision=current,
                incoming=tuple(incoming),
                assets=tuple(assets),
            ),
            referencing_documents=referencing,
        )
        if plan.conflicts:
            reasons = "; ".join(
                f"{item.code} in {item.document_id}: {item.reason}" for item in plan.conflicts
            )
            raise CopalBridgeError(f"rename would break references; move left unapplied ({reasons})")
        if plan.phase == MovePhase.ABORTED:
            raise CopalBridgeError("rename preflight failed; move left unapplied")
        # A previous attempt can strand the rename between the media relocate
        # and the document relocate.  Detect that half-applied state and resume
        # it rather than refusing the retry; both-sides-present stays a conflict.
        stranded = (
            source_media_abs != destination_media_abs
            and not source_media_abs.exists()
            and destination_media_abs.is_dir()
        )
        if stranded:
            plan.phase = MovePhase.RECOVERING
        elif source_media_abs.is_dir() and source_media_abs != destination_media_abs:
            # Stage: relocate owned asset bytes only.  A missing source media dir
            # is a no-op; another document's bytes are never moved.
            destination_media_abs.parent.mkdir(parents=True, exist_ok=True)
            if destination_media_abs.exists():
                raise CopalBridgeError("destination media directory already exists")
            os.replace(source_media_abs, destination_media_abs)
            plan.phase = MovePhase.COMMITTED
        # Apply surgical reference rewrites.  Each document is rewritten once
        # from its captured preimage; no global string replacement.
        for doc_id, preimage in plan.preimages.items():
            record_other = (manifest.get("documents") or {}).get(doc_id)
            if not isinstance(record_other, dict) or record_other.get("trashed"):
                continue
            if record_other.get("readOnly"):
                continue
            edits = [item for item in plan.reference_edits if item.document_id == doc_id]
            if not edits:
                continue
            rebuilt = preimage
            for item in sorted(edits, key=lambda row: row.site.start, reverse=True):
                rebuilt = rebuilt[: item.site.start] + item.new_text + rebuilt[item.site.end :]
            other_path = vault / str(record_other.get("path") or "")
            self._atomic_write(other_path, rebuilt)
            record_other["head"] = self._fingerprint(rebuilt)
            record_other["updatedAt"] = time.time()
        # Publish through the shared recovery transition so a stranded or
        # freshly applied move commits the same idempotent receipt.
        recover_move_plan(plan)
        receipt = plan.as_receipt()
        self._operation(
            manifest,
            "media_move",
            f"media move {source_path} -> {destination_name}",
            document_id=str(record.get("id") or ""),
        )
        return receipt

    def _refresh_external_files(self, vault: Path, manifest: dict[str, Any]) -> None:
        """Turn an out-of-band file edit into one durable source operation.

        Loose files can be edited by another process. The manifest stores the
        last observed mtime, so normal task queries inspect metadata only and
        read/hash just files whose mtime changed. The resulting operation feeds
        the same incremental task projection path as native writes.
        """
        changed = False
        for record in manifest.get("documents", {}).values():
            if not isinstance(record, dict) or record.get("trashed"):
                continue
            path = vault / str(record.get("path") or "")
            try:
                stat_result = path.stat()
            except OSError:
                continue
            mtime_ns = stat_result.st_mtime_ns
            previous_mtime = record.get("mtime_ns")
            if previous_mtime is None:
                record["mtime_ns"] = mtime_ns
                changed = True
                continue
            if int(previous_mtime) == mtime_ns:
                continue
            try:
                content = self._read_content(path)
            except (OSError, UnicodeDecodeError):
                continue
            fingerprint = self._fingerprint(content)
            record["mtime_ns"] = mtime_ns
            if fingerprint == record.get("head"):
                continue
            record["head"] = fingerprint
            record["updatedAt"] = time.time()
            self._operation(
                manifest,
                "external_refresh",
                f"external refresh {record.get('name')}",
                document_id=str(record.get("id") or ""),
            )
            changed = True
        if changed:
            self._save(vault, manifest)

    async def call(self, operation: str, args: dict[str, Any] | None = None, *, timeout: float = 20) -> Any:
        del timeout
        return await asyncio.to_thread(self._call_sync, operation, args or {})

    def _call_sync(self, operation: str, args: dict[str, Any]) -> Any:
        # Separate repository instances and bridge processes must share the
        # same check/write boundary; an asyncio mutex cannot provide that.
        with copal_commit_lock(self.data_dir):
            return self._call_locked(operation, args)

    def _call_locked(self, operation: str, args: dict[str, Any]) -> Any:
        self._guarded.reconcile(self)
        if operation == "commit_guarded":
            return self._guarded.execute(self, args)
        if operation == "owner_inventory":
            return self.owner_inventory(str(args.get("owner") or ""))
        if operation == "preflight_rename_owner":
            return self.preview_owner_rename(
                str(args.get("old_owner") or ""),
                str(args.get("new_owner") or ""),
            )
        if operation in {"rename_owner", "reconcile_owner"}:
            return self.reconcile_owner(
                str(args.get("old_owner") or ""),
                str(args.get("new_owner") or ""),
                expected_source=args.get("expected_source")
                or (args.get("manifest") or {}).get("source"),
                expected_target=args.get("expected_target")
                or (args.get("manifest") or {}).get("target"),
            )
        if operation == "compensate_owner_rename":
            return self.compensate_owner_rename(
                str(args.get("old_owner") or ""),
                str(args.get("new_owner") or ""),
                dict(args.get("manifest") or {}),
            )
        if operation == "purge_owner":
            return self.purge_owner(
                str(args.get("owner") or ""),
                expected=args.get("expected"),
            )
        vault, manifest = self._scope(args)
        action_id = str(args.get("action_id") or args.get("actionId") or args.get("commandId") or "").strip() or None
        documents = manifest["documents"]
        self._refresh_external_files(vault, manifest)
        if operation == "task_index_get":
            ids = args.get("ids")
            if ids is not None and not isinstance(ids, list):
                raise CopalBridgeError("task index ids must be a list")
            return self._task_index_get(vault, ids=[str(value) for value in ids] if ids is not None else None)
        if operation == "task_index_generation":
            return self._task_index_generation(vault)
        if operation == "task_index_resolve":
            return self._task_index_resolve(vault, str(args.get("resourceId") or ""))
        if operation == "task_index_page":
            return self._task_index_page(vault, args)
        if operation == "task_index_update":
            records = args.get("records") or {}
            if not isinstance(records, dict):
                raise CopalBridgeError("task index records must be an object")
            removed = args.get("removed") or []
            if not isinstance(removed, list):
                raise CopalBridgeError("task index removals must be a list")
            return self._task_index_update(
                vault,
                str(args.get("generation") or ""),
                records,
                [str(value) for value in removed],
                bool(args.get("rebuild")),
                int(args.get("sourceReads") or 0),
            )
        if operation in {"metadata_page", "metadata_get"}:
            state = str(args.get("state") or "active")
            if state not in {"active", "trash"}:
                raise CopalBridgeError("unsupported metadata state")
            hidden = str(args.get("hidden") or "exclude")
            if hidden not in {"exclude", "include", "only"}:
                raise CopalBridgeError("unsupported hidden filter")
            rows = [
                self._record_doc(vault, record, include_body=False)
                for record in documents.values()
                if isinstance(record, dict)
                and bool(record.get("trashed")) is (state == "trash")
                and (not args.get("kind") or record.get("kind") == args.get("kind"))
                and (not args.get("corpus") or args.get("corpus") == "all" or record.get("corpus") == args.get("corpus"))
            ]
            if hidden != "include":
                rows = [row for row in rows if bool(row.get("hidden")) is (hidden == "only")]
            if operation == "metadata_get":
                wanted = str(args.get("id") or "")
                row = next((row for row in rows if str(row.get("id") or "") == wanted), None)
                if row is None:
                    raise CopalBridgeError("document not found in this scope")
                return row
            query = str(args.get("query") or "").casefold()
            if query:
                rows = [row for row in rows if query in str(row.get("name") or "").casefold()]
            sort_key = str(args.get("sort_key") or "name")
            direction = str(args.get("sort_direction") or "asc")
            if sort_key not in {"name", "kind", "size", "modified"} or direction not in {"asc", "desc"}:
                raise CopalBridgeError("unsupported metadata sort")
            field = {"name": "name", "kind": "kind", "size": "size", "modified": "updatedAt"}[sort_key]

            def sort_value(row: dict[str, Any]) -> str | int | float:
                if sort_key == "size":
                    return int(row.get(field) or 0)
                if sort_key == "modified":
                    return float(row.get(field) or 0)
                return str(row.get(field) or "").casefold()

            rows.sort(
                key=lambda row: (
                    sort_value(row),
                    str(row.get("id") or ""),
                ),
                reverse=direction == "desc",
            )
            snapshot = self._fingerprint(json.dumps([
                [row.get("id"), row.get("head"), row.get("name"), row.get("updatedAt"), row.get("trashed")]
                for row in rows
            ], ensure_ascii=False, separators=(",", ":")))
            expected_snapshot = str(args.get("snapshot") or "")
            if expected_snapshot and expected_snapshot != snapshot:
                raise CopalBridgeError("stale_cursor")
            try:
                offset = max(0, int(args.get("cursor") or 0))
                limit = max(1, min(int(args.get("limit") or 100), 200))
            except (TypeError, ValueError) as exc:
                raise CopalBridgeError("invalid metadata cursor") from exc
            page = rows[offset:offset + limit]
            next_cursor = str(offset + len(page)) if offset + len(page) < len(rows) else None
            return {
                "docs": page,
                "total": len(rows),
                "next_cursor": next_cursor,
                "snapshot": snapshot,
                "storage": "files",
            }
        if operation in {"status", "scoped_status"}:
            docs = [record for record in documents.values() if isinstance(record, dict) and not record.get("trashed")]
            document_count = len(docs)
            return {"ready": True, "storage": "files", "storage_namespace": str(args.get("owner") or ""), "owner": args.get("owner"), "workspace_id": args.get("workspace_id"), "documents": document_count, "visible_documents": document_count, "head": self._fingerprint(json.dumps(manifest, sort_keys=True))}
        if operation == "find_by_name":
            wanted = str(args.get("name") or "")
            corpus = str(args.get("corpus") or "all")
            record = next(
                (
                    record
                    for record in documents.values()
                    if isinstance(record, dict)
                    and not record.get("trashed")
                    and record.get("name") == wanted
                    and (corpus == "all" or record.get("corpus") == corpus)
                ),
                None,
            )
            if record is None:
                return None
            return self._record_doc(vault, record, include_body=True)
        if operation in {"list", "index", "search"}:
            rows = [self._record_doc(vault, record, include_body=True) for record in documents.values() if isinstance(record, dict) and not record.get("trashed") and (not args.get("kind") or record.get("kind") == args.get("kind")) and (not args.get("corpus") or args.get("corpus") == "all" or record.get("corpus") == args.get("corpus"))]
            query = str(args.get("query") or "").casefold()
            if operation == "search" and query:
                rows = [row for row in rows if query in str(row.get("name") or "").casefold() or query in str(row.get("text") or "").casefold()]
            rows.sort(key=lambda row: str(row.get("name") or "").casefold())
            return {"docs": rows, "total": len(rows), "storage": "files"}
        if operation == "get":
            return self._record_doc(vault, self._doc(manifest, str(args.get("id") or "")), include_body=True)
        if operation == "put_asset_scoped":
            encoded = str(args.get("base64") or "")
            try:
                data = base64.b64decode(encoded, validate=True)
            except (ValueError, TypeError) as exc:
                raise CopalBridgeError("asset base64 is invalid") from exc
            if len(data) > 10 * 1024 * 1024:
                raise CopalBridgeError("asset exceeds the configured limit")
            raw_name = str(args.get("name") or "")
            name = self._safe_name(raw_name, "asset")
            existing = next((row for row in documents.values() if isinstance(row, dict) and row.get("kind") == "asset" and row.get("name") == name and not row.get("trashed")), None)
            # Asset heads must hash the original bytes.  The text fingerprint
            # re-encodes a Latin-1 decode as UTF-8, which disagrees with the
            # verification path for non-ASCII payloads.
            digest = binary_digest(data)
            if existing is not None:
                if existing.get("head") == digest:
                    return {"doc": self._record_doc(vault, existing, include_body=False)}
                raise CopalBridgeError("asset already exists")
            document_id = f"asset_{uuid.uuid4().hex}"
            record = {"id": document_id, "owner": args.get("owner"), "workspace_id": args.get("workspace_id"), "kind": "asset", "corpus": "system", "name": name, "path": name, "head": digest, "createdAt": time.time(), "updatedAt": time.time(), "readOnly": True, "trashed": False}
            documents[document_id] = record
            self._atomic_write(vault / name, data)
            self._operation(manifest, "put_asset", f"put asset {name}", document_id=document_id, action_id=str(args.get("action_id") or "") or None)
            self._save(vault, manifest)
            return {"doc": self._record_doc(vault, record, include_body=False)}
        if operation == "history":
            record = self._doc(manifest, str(args.get("id") or ""))
            return {"doc": record["id"], "changes": self._history_changes(vault, record)}
        if operation == "checkpoint":
            record = self._doc(manifest, str(args.get("id") or ""))
            path = vault / str(record["path"])
            if not path.is_file():
                raise CopalBridgeError("loose Copal document file is missing")
            commit = f"checkpoint-{uuid.uuid4().hex}"
            self._atomic_write(
                self._history_dir(vault, record) / f"{commit}.json",
                json.dumps({"commit": commit, "content": path.read_text(encoding="utf-8"), "name": record.get("name"), "ts": time.time(), "message": args.get("message")}, ensure_ascii=False, separators=(",", ":")),
            )
            self._operation(manifest, "checkpoint", f"checkpoint {record['name']}", document_id=record["id"], action_id=action_id)
            self._save(vault, manifest)
            return {"doc": self._record_doc(vault, record, include_body=True)}
        if operation == "create":
            if action_id:
                replay = next((item for item in manifest.get("operations", []) if isinstance(item, dict) and item.get("actionId") == action_id and item.get("documentId")), None)
                if replay:
                    return {"outcome": "created", "doc": self._record_doc(vault, documents[str(replay["documentId"])], include_body=True), "replayed": True}
            kind = str(args.get("kind") or "markdown")
            name = self._safe_name(str(args.get("name") or ""), kind)
            if any(record.get("path") == name and not record.get("trashed") for record in documents.values() if isinstance(record, dict)):
                raise CopalBridgeError("document already exists")
            document_id = f"doc_{uuid.uuid4().hex}"
            content = str(args.get("content") or "")
            record = {"id": document_id, "owner": args.get("owner"), "workspace_id": args.get("workspace_id"), "kind": kind, "corpus": str(args.get("corpus") or "notes"), "name": name, "path": name, "head": self._fingerprint(content), "createdAt": time.time(), "updatedAt": time.time(), "readOnly": bool(args.get("read_only")), "trashed": False}
            documents[document_id] = record
            self._atomic_write(vault / name, content)
            try:
                record["mtime_ns"] = (vault / name).stat().st_mtime_ns
            except OSError:
                pass
            self._operation(manifest, "create", f"create {name}", document_id=document_id, action_id=action_id)
            self._save(vault, manifest)
            return {"outcome": "created", "doc": self._record_doc(vault, record, include_body=True)}
        if operation == "write":
            record = self._doc(manifest, str(args.get("id") or ""))
            if record.get("readOnly"):
                raise CopalBridgeError("document is read-only")
            base = args.get("base")
            # A user may have changed the visible file outside Copal. Compare
            # against its current bytes rather than an older manifest head.
            current = self._record_doc(vault, record, include_body=True)
            record["head"] = current["head"]
            if base and base != record.get("head"):
                return {"outcome": "stale", "doc": current}
            return {"outcome": "committed", "doc": self._write_record(vault, manifest, record, str(args.get("content") or ""), operation="write", action_id=action_id)}
        if operation == "restore":
            record = self._doc(manifest, str(args.get("id") or ""))
            commit = str(args.get("commit") or "")
            if commit == record.get("head"):
                return {"outcome": "committed", "doc": self._record_doc(vault, record, include_body=True)}
            snapshot_path = self._history_dir(vault, record) / f"{commit}.json"
            try:
                snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise CopalBridgeError("history commit was not found") from exc
            if not isinstance(snapshot, dict) or not isinstance(snapshot.get("content"), str):
                raise CopalBridgeError("history commit is invalid")
            return {"outcome": "committed", "doc": self._write_record(vault, manifest, record, snapshot["content"], operation="restore", action_id=action_id)}
        if operation == "rename":
            record = self._doc(manifest, str(args.get("id") or ""))
            name = self._safe_name(str(args.get("name") or ""), str(record.get("kind") or ""))
            destination = vault / name
            if destination.exists() or any(other.get("path") == name and other.get("id") != record["id"] and not other.get("trashed") for other in documents.values() if isinstance(other, dict)):
                raise CopalBridgeError("document already exists")
            source = vault / str(record["path"])
            # Repair media links first so a protected reference refuses the
            # rename before any document or asset byte is relocated.
            media_receipt = self._plan_and_apply_media_move(vault, manifest, record, name)
            destination.parent.mkdir(parents=True, exist_ok=True)
            os.replace(source, destination)
            record["name"] = name
            record["path"] = name
            record["head"] = self._fingerprint(destination.read_text(encoding="utf-8"))
            record["updatedAt"] = time.time()
            self._operation(manifest, "rename", f"rename {name}", document_id=record["id"], action_id=action_id)
            self._save(vault, manifest)
            result = {"outcome": "committed", "doc": self._record_doc(vault, record, include_body=True)}
            if media_receipt.get("status") != "noop" or media_receipt.get("asset_moves") is not None:
                result["media_move"] = media_receipt
            return result
        if operation == "trash" and not args.get("id"):
            return {"docs": [self._record_doc(vault, record, include_body=False) for record in documents.values() if isinstance(record, dict) and record.get("trashed") and (not args.get("corpus") or record.get("corpus") == args.get("corpus"))]}
        if operation in {"delete", "trash"}:
            record = self._doc(manifest, str(args.get("id") or ""))
            source = vault / str(record["path"])
            trash = vault / ".copal" / "trash" / f"{record['id']}-{Path(record['path']).name}"
            trash.parent.mkdir(parents=True, exist_ok=True)
            if source.exists():
                os.replace(source, trash)
            record["trashed"] = True
            record["trashPath"] = str(trash.relative_to(vault))
            record["updatedAt"] = time.time()
            self._operation(manifest, "trash", f"trash {record['name']}", document_id=record["id"], action_id=action_id)
            self._save(vault, manifest)
            return {"outcome": "deleted", "doc": self._record_doc(vault, record, include_body=False)}
        if operation == "restore_deleted":
            record = manifest.get("documents", {}).get(str(args.get("id") or ""))
            if not isinstance(record, dict) or not record.get("trashed"):
                raise CopalBridgeError("document not found in trash")
            source = vault / str(record.get("trashPath") or "")
            destination = vault / str(record.get("path") or "")
            if not source.is_file():
                raise CopalBridgeError("trash entry is unavailable")
            if destination.exists():
                raise CopalBridgeError("document already exists")
            destination.parent.mkdir(parents=True, exist_ok=True)
            os.replace(source, destination)
            record["trashed"] = False
            record.pop("trashPath", None)
            record["updatedAt"] = time.time()
            self._operation(manifest, "restore_deleted", f"restore {record['name']}", document_id=record["id"], action_id=action_id)
            self._save(vault, manifest)
            return {"outcome": "restored", "doc": self._record_doc(vault, record, include_body=True)}
        if operation in {"ops", "operations"}:
            limit = max(1, min(int(args.get("limit") or 50), 500))
            before = str(args.get("before") or "")
            rows = []
            past_before = not before
            for entry in reversed(manifest.get("operations") or []):
                operation_id = str(entry.get("id") or entry.get("op") or "")
                # Operations are UUID-backed IDs and are returned newest first;
                # match the Redb bridge's exclusive before cursor contract.
                if not past_before:
                    if operation_id == before:
                        past_before = True
                    continue
                rows.append({
                    "op": operation_id,
                    "parent": entry.get("parent"),
                    "kind": entry.get("kind"),
                    "description": entry.get("description"),
                    "ts": entry.get("createdAt"),
                    "changedIds": [str(value) for value in entry.get("changedIds") or [] if value],
                })
                if len(rows) >= limit:
                    break
            return {"ops": rows}
        if operation == "trash_list":
            return {"docs": [self._record_doc(vault, record, include_body=False) for record in documents.values() if isinstance(record, dict) and record.get("trashed")]}
        if operation in {"export_snapshot", "export"}:
            return {"docs": [self._record_doc(vault, record, include_body=True) for record in documents.values() if isinstance(record, dict) and not record.get("trashed")]}
        if operation == "asset_path":
            record = self._doc(manifest, str(args.get("id") or ""))
            path = vault / str(record["path"])
            if not path.is_file():
                raise CopalBridgeError("asset not found in this scope")
            return {"path": str(path), "name": record["name"]}
        raise CopalBridgeError(f"loose Copal operation is not implemented: {operation}")


class LooseCopalBridge(LooseCopalRepository):
    """Lifecycle-compatible facade selected by ``COPAL_STORAGE=files``."""

    def is_alive(self) -> bool:
        return True

    async def start(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)

    async def stop(self) -> None:
        return None
