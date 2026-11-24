"""Canonical transactional authority for Open Clank file locations/workspaces.

This module is the sole writable permission store. Legacy APIs are adapters
and Rust consumes a derived read-only snapshot.  A physical Location is
identity only; People bindings govern app visibility, Agent bindings narrow
agent activity, and Operation bindings approve a risky action without ever
minting path authority.

The browser never supplies an EffectiveScope.  Server code resolves one from an
authenticated immutable account id, a server-assigned origin, and opaque record
ids.  Every mutation shares one SQLite generation and audit transaction.
"""

from __future__ import annotations

import json
import hashlib
import os
import sqlite3
import time
import uuid
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Mapping, Optional, Sequence

from src.constants import APP_DB
from src.secret_storage import decrypt, encrypt
from src.openclank.file_policy_compat import CanonicalCompatibility, COMPAT_SCHEMA


def is_closed_managed_file_plan(plan: Any) -> bool:
    """Recognize only the existing frozen managed-file wire shape."""
    if not isinstance(plan, Mapping) or plan.get("kind") is not None:
        return False
    payload = plan.get("payload")
    if not isinstance(payload, Mapping) or set(payload) not in ({"operation_id", "participants", "guards"}, {"operation_id", "participants", "guards", "directory_guard"}):
        return False
    parts, guards = payload.get("participants"), payload.get("guards")
    if (payload.get("operation_id") != plan.get("native_operation_id") or not isinstance(parts, list) or not parts
            or not isinstance(guards, list) or len(parts) + len(guards) > 128):
        return False
    if any(not isinstance(part, Mapping) or set(part) != {"kind", "source", "destination", "expected_fingerprint", "replacement"}
           or part.get("kind") not in {"document", "asset", "reference"} or not isinstance(part.get("source"), str)
           or not isinstance(part.get("expected_fingerprint"), str) for part in parts):
        return False
    if any(not isinstance(guard, Mapping) or set(guard) != {"source", "expected_fingerprint"}
           or not isinstance(guard.get("source"), str) or not isinstance(guard.get("expected_fingerprint"), str) for guard in guards):
        return False
    documents = [part for part in parts if part.get("kind") == "document"]
    if len(documents) != 1 or documents[0].get("source") != plan.get("source") or documents[0].get("destination") != plan.get("destination"):
        return False
    directory_guard = payload.get("directory_guard")
    if directory_guard is not None and (not isinstance(directory_guard, Mapping) or set(directory_guard) != {"path", "revision"}
            or not isinstance(directory_guard.get("path"), str) or not isinstance(directory_guard.get("revision"), str)):
        return False
    return isinstance(plan.get("native_operation_id"), str) and type(plan.get("generation")) is int


SCHEMA_VERSION = 1
LOCATION_KINDS = frozenset({"exact_file", "directory", "volume", "whole_root"})
CAPABILITIES = frozenset({"read", "write", "execute"})
BINDING_CLASSES = frozenset({"people", "agent", "operation"})
SUBJECT_KINDS = frozenset({"user", "group"})
LIFETIMES = frozenset({"once", "chat", "workspace", "always"})
ORIGINS = frozenset({"app", "agent"})
RESET_SCOPES = frozenset({"chat", "workspace", "location", "all_agent"})
_UNSET = object()


class FilePolicyError(ValueError):
    def __init__(self, message: str, *, code: str = "invalid_policy") -> None:
        super().__init__(message)
        self.code = code


def _now_ms() -> int:
    return int(time.time() * 1000)


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _parse_json(value: str | None, fallback: Any) -> Any:
    if not value:
        return fallback
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return fallback


def _capabilities(values: Iterable[str]) -> tuple[str, ...]:
    normalized = tuple(sorted({str(value).strip().lower() for value in values if str(value).strip()}))
    if not normalized or not set(normalized).issubset(CAPABILITIES):
        raise FilePolicyError("capabilities must contain read, write, or execute", code="invalid_capabilities")
    return normalized


def _canonical_location(path: str, kind: str) -> tuple[str, str]:
    if kind not in LOCATION_KINDS:
        raise FilePolicyError("unsupported location kind", code="invalid_location_kind")
    value = str(path or "").strip()
    if not value:
        raise FilePolicyError("location path is required", code="path_required")
    canonical = str(Path(value).expanduser().resolve(strict=False))
    if kind == "whole_root" and Path(canonical) != Path(Path(canonical).anchor):
        raise FilePolicyError("whole-root location must name a filesystem root", code="invalid_whole_root")
    # normcase is a no-op on case-sensitive Unix and folds on Windows.  The
    # captured platform identity remains the stronger collision/rebind check.
    return canonical, os.path.normcase(canonical)


def _relative_folder(value: str) -> str:
    raw = str(value or "").replace("\\", "/").strip()
    if raw in {"", "."}:
        return ""
    path = PurePosixPath(raw)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise FilePolicyError("workspace folder must be a canonical relative path", code="invalid_workspace_folder")
    return path.as_posix()


def _relative_contains(parent: str, child: str) -> bool:
    parent_parts = PurePosixPath(parent).parts if parent else ()
    child_parts = PurePosixPath(child).parts if child else ()
    return child_parts[: len(parent_parts)] == parent_parts


@dataclass(frozen=True)
class Location:
    id: str
    kind: str
    canonical_path: str
    display_path: str
    capabilities: tuple[str, ...]
    platform_identity: Mapping[str, Any]
    availability: str
    enabled: bool
    generation: int
    revision: int
    created_by_subject_id: str
    migration_source: Optional[str]
    migration_key: Optional[str]


@dataclass(frozen=True)
class Workspace:
    id: str
    owner_subject_id: str
    name: str
    location_id: str
    relative_folder: str
    archived: bool
    generation: int
    revision: int


@dataclass(frozen=True)
class PolicyBinding:
    id: str
    binding_class: str
    subject_kind: str
    subject_id: str
    location_id: Optional[str]
    workspace_id: Optional[str]
    chat_id: Optional[str]
    resource_ref: Optional[str]
    operation: Optional[str]
    capabilities: tuple[str, ...]
    lifetime: str
    status: str
    remaining_uses: Optional[int]
    expires_unix_ms: Optional[int]
    created_by_subject_id: str
    generation: int
    revision: int
    migration_source: Optional[str]
    migration_key: Optional[str]


@dataclass(frozen=True)
class FilePlace:
    """Owner-scoped navigation bookmark; never an authority grant."""

    id: str
    owner_subject_id: str
    provider: str
    stable_resource_id: str
    origin_id: str
    kind: str
    display_name: str
    created_unix_ms: int
    updated_unix_ms: int


@dataclass(frozen=True)
class FileRecent:
    """Owner-scoped recently used resource; never an authority grant."""

    id: str
    owner_subject_id: str
    provider: str
    stable_resource_id: str
    origin_id: str
    kind: str
    display_name: str
    accessed_unix_ms: int


@dataclass(frozen=True)
class FileSavedSearch:
    """Owner-scoped query recipe. Results and authority are never persisted."""

    id: str
    owner_subject_id: str
    name: str
    provider_scope: str
    query: str
    sort: Mapping[str, Any]
    created_unix_ms: int
    updated_unix_ms: int


@dataclass(frozen=True)
class EffectiveScope:
    allowed: bool
    subject_id: str
    origin: str
    location_id: str
    workspace_id: Optional[str]
    capabilities: tuple[str, ...]
    policy_generation: int
    reason: str
    consumed_binding_id: Optional[str] = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "allowed": self.allowed,
            "subject_id": self.subject_id,
            "origin": self.origin,
            "location_id": self.location_id,
            "workspace_id": self.workspace_id,
            "capabilities": list(self.capabilities),
            "policy_generation": self.policy_generation,
            "reason": self.reason,
            "consumed_binding_id": self.consumed_binding_id,
        }


_SCHEMA = """
CREATE TABLE IF NOT EXISTS file_policy_meta (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    schema_version INTEGER NOT NULL,
    generation INTEGER NOT NULL DEFAULT 0
);
INSERT OR IGNORE INTO file_policy_meta(singleton, schema_version, generation)
VALUES (1, 1, 0);

CREATE TABLE IF NOT EXISTS file_policy_locations (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    canonical_path TEXT NOT NULL,
    canonical_key TEXT NOT NULL,
    display_path TEXT NOT NULL,
    capabilities_json TEXT NOT NULL,
    platform_identity_json TEXT NOT NULL,
    availability TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    generation INTEGER NOT NULL,
    revision INTEGER NOT NULL DEFAULT 1,
    created_by_subject_id TEXT NOT NULL,
    created_unix_ms INTEGER NOT NULL,
    updated_unix_ms INTEGER NOT NULL,
    migration_source TEXT,
    migration_key TEXT,
    UNIQUE(kind, canonical_key),
    UNIQUE(migration_source, migration_key)
);

CREATE TABLE IF NOT EXISTS file_policy_workspaces (
    id TEXT PRIMARY KEY,
    owner_subject_id TEXT NOT NULL,
    name TEXT NOT NULL,
    location_id TEXT NOT NULL REFERENCES file_policy_locations(id) ON DELETE CASCADE,
    relative_folder TEXT NOT NULL DEFAULT '',
    archived INTEGER NOT NULL DEFAULT 0,
    generation INTEGER NOT NULL,
    revision INTEGER NOT NULL DEFAULT 1,
    created_unix_ms INTEGER NOT NULL,
    updated_unix_ms INTEGER NOT NULL,
    migration_source TEXT,
    migration_key TEXT,
    UNIQUE(owner_subject_id, location_id, relative_folder),
    UNIQUE(migration_source, migration_key)
);

CREATE TABLE IF NOT EXISTS file_policy_bindings (
    id TEXT PRIMARY KEY,
    binding_class TEXT NOT NULL,
    subject_kind TEXT NOT NULL,
    subject_id TEXT NOT NULL,
    location_id TEXT REFERENCES file_policy_locations(id) ON DELETE CASCADE,
    workspace_id TEXT REFERENCES file_policy_workspaces(id) ON DELETE CASCADE,
    chat_id TEXT,
    resource_ref TEXT,
    operation TEXT,
    capabilities_json TEXT NOT NULL,
    lifetime TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    remaining_uses INTEGER,
    expires_unix_ms INTEGER,
    created_by_subject_id TEXT NOT NULL,
    generation INTEGER NOT NULL,
    revision INTEGER NOT NULL DEFAULT 1,
    created_unix_ms INTEGER NOT NULL,
    updated_unix_ms INTEGER NOT NULL,
    migration_source TEXT,
    migration_key TEXT,
    UNIQUE(migration_source, migration_key)
);
CREATE INDEX IF NOT EXISTS ix_file_policy_bindings_subject
ON file_policy_bindings(subject_kind, subject_id, binding_class, status);
CREATE INDEX IF NOT EXISTS ix_file_policy_bindings_target
ON file_policy_bindings(location_id, workspace_id, chat_id);

CREATE TABLE IF NOT EXISTS file_policy_audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    generation INTEGER NOT NULL,
    event TEXT NOT NULL,
    actor_subject_id TEXT NOT NULL,
    target_type TEXT NOT NULL,
    target_id TEXT NOT NULL,
    reason_code TEXT NOT NULL,
    details_json TEXT NOT NULL DEFAULT '{}',
    created_unix_ms INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_file_policy_audit_generation
ON file_policy_audit(generation, id);

CREATE TABLE IF NOT EXISTS file_places (
    id TEXT PRIMARY KEY,
    owner_subject_id TEXT NOT NULL,
    provider TEXT NOT NULL,
    stable_resource_id TEXT NOT NULL,
    origin_ciphertext TEXT NOT NULL,
    kind TEXT NOT NULL,
    display_name TEXT NOT NULL,
    created_unix_ms INTEGER NOT NULL,
    updated_unix_ms INTEGER NOT NULL,
    UNIQUE(owner_subject_id, provider, stable_resource_id)
);
CREATE INDEX IF NOT EXISTS ix_file_places_owner
ON file_places(owner_subject_id, updated_unix_ms DESC, id);

CREATE TABLE IF NOT EXISTS file_recents (
    id TEXT PRIMARY KEY,
    owner_subject_id TEXT NOT NULL,
    provider TEXT NOT NULL,
    stable_resource_id TEXT NOT NULL,
    origin_ciphertext TEXT NOT NULL,
    kind TEXT NOT NULL,
    display_name TEXT NOT NULL,
    accessed_unix_ms INTEGER NOT NULL,
    UNIQUE(owner_subject_id, provider, stable_resource_id)
);
CREATE INDEX IF NOT EXISTS ix_file_recents_owner
ON file_recents(owner_subject_id, accessed_unix_ms DESC, id);

CREATE TABLE IF NOT EXISTS file_saved_searches (
    id TEXT PRIMARY KEY,
    owner_subject_id TEXT NOT NULL,
    name TEXT NOT NULL,
    provider_scope TEXT NOT NULL,
    query TEXT NOT NULL,
    sort_json TEXT NOT NULL,
    created_unix_ms INTEGER NOT NULL,
    updated_unix_ms INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_file_saved_searches_owner
ON file_saved_searches(owner_subject_id, updated_unix_ms DESC, id);

CREATE TABLE IF NOT EXISTS file_operations (
    owner_subject_id TEXT NOT NULL,
    operation_id TEXT NOT NULL,
    request_digest TEXT NOT NULL,
    generation INTEGER NOT NULL,
    receipt_ciphertext TEXT NOT NULL,
    created_unix_ms INTEGER NOT NULL,
    lifecycle_phase TEXT,
    PRIMARY KEY(owner_subject_id, operation_id)
);
CREATE INDEX IF NOT EXISTS ix_file_operations_owner
ON file_operations(owner_subject_id, created_unix_ms DESC);
"""


class _ClosingConnection(sqlite3.Connection):
    """Retain SQLite transaction context semantics and close on every exit."""

    def __exit__(self, exc_type, exc_value, traceback):
        try:
            return super().__exit__(exc_type, exc_value, traceback)
        finally:
            self.close()


_FILESYSTEM_ACCESS_SCHEMA = """
CREATE TABLE IF NOT EXISTS file_policy_full_filesystem_access (
    subject_id TEXT PRIMARY KEY,
    human INTEGER NOT NULL DEFAULT 0 CHECK (human IN (0, 1)),
    agent INTEGER NOT NULL DEFAULT 0 CHECK (agent IN (0, 1))
)
"""


class FilePolicyRepository(CanonicalCompatibility):
    """SQLite repository and monotonic resolver for canonical file authority."""

    def __init__(self, db_path: str | os.PathLike[str] | None = None) -> None:
        self.db_path = str(db_path or os.environ.get("OPEN_CLANK_AUTHORITY_DB_PATH") or APP_DB)
        path = Path(self.db_path).expanduser().resolve()
        existing = False
        has_filesystem_access = False
        # Admission is read-only: even switching SQLite journal mode is a
        # mutation and must wait until the store passes the current contract.
        if path.is_file() and path.stat().st_size:
            with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, factory=_ClosingConnection) as admission:
                existing = bool(admission.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='file_policy_meta'").fetchone())
                if not existing and admission.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='permission_grants'").fetchone():
                    raise FilePolicyError("Files permission store does not match this release. Stop writers and keep a complete backup; restore the matching release or prepare an offline conversion.", code="policy_store_unavailable")
                if existing:
                    has_filesystem_access = bool(admission.execute(
                        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='file_policy_full_filesystem_access'"
                    ).fetchone())
                    required = {'file_policy_meta': ['singleton', 'schema_version', 'generation'], 'file_policy_locations': ['id', 'kind', 'canonical_path', 'canonical_key', 'display_path', 'capabilities_json', 'platform_identity_json', 'availability', 'enabled', 'generation', 'revision', 'created_by_subject_id', 'created_unix_ms', 'updated_unix_ms', 'migration_source', 'migration_key'], 'file_policy_workspaces': ['id', 'owner_subject_id', 'name', 'location_id', 'relative_folder', 'archived', 'generation', 'revision', 'created_unix_ms', 'updated_unix_ms', 'migration_source', 'migration_key'], 'file_policy_bindings': ['id', 'binding_class', 'subject_kind', 'subject_id', 'location_id', 'workspace_id', 'chat_id', 'resource_ref', 'operation', 'capabilities_json', 'lifetime', 'status', 'remaining_uses', 'expires_unix_ms', 'created_by_subject_id', 'generation', 'revision', 'created_unix_ms', 'updated_unix_ms', 'migration_source', 'migration_key'], 'file_policy_audit': ['id', 'generation', 'event', 'actor_subject_id', 'target_type', 'target_id', 'reason_code', 'details_json', 'created_unix_ms'], 'sqlite_sequence': ['name', 'seq'], 'file_places': ['id', 'owner_subject_id', 'provider', 'stable_resource_id', 'origin_ciphertext', 'kind', 'display_name', 'created_unix_ms', 'updated_unix_ms'], 'file_recents': ['id', 'owner_subject_id', 'provider', 'stable_resource_id', 'origin_ciphertext', 'kind', 'display_name', 'accessed_unix_ms'], 'file_saved_searches': ['id', 'owner_subject_id', 'name', 'provider_scope', 'query', 'sort_json', 'created_unix_ms', 'updated_unix_ms'], 'file_operations': ['owner_subject_id', 'operation_id', 'request_digest', 'generation', 'receipt_ciphertext', 'created_unix_ms', 'lifecycle_phase'], 'file_policy_subject_aliases': ['username', 'subject_id', 'is_admin'], 'file_policy_legacy_aliases': ['kind', 'legacy_id', 'target_id'], 'file_policy_import_sources': ['id', 'source_hash', 'source_ciphertext', 'complete', 'created_unix_ms'], 'file_policy_import_items': ['id', 'kind', 'legacy_id', 'subject_id', 'owner_username', 'source_json', 'status', 'reason', 'canonical_id'], 'file_policy_approval_details': ['binding_id', 'permission_type', 'pattern', 'resource']}
                    for table, names in required.items():
                        found = {row[1] for row in admission.execute(f"PRAGMA table_info({table})")}
                        if not set(names).issubset(found):
                            raise FilePolicyError("Files store schema does not match this release. Stop writers and keep a complete backup; restore the matching release or prepare an offline conversion.", code="policy_store_unavailable")
                    columns = {row[1] for row in admission.execute("PRAGMA table_info(file_operations)")}
                    version = admission.execute("SELECT schema_version FROM file_policy_meta WHERE singleton=1").fetchone()
                    if "lifecycle_phase" not in columns or not version or int(version[0]) != SCHEMA_VERSION:
                        raise FilePolicyError("Files store schema does not match this release. Stop writers and keep a complete backup; restore the matching release or prepare an offline conversion.", code="policy_store_unavailable")
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            if not existing:
                connection.executescript(_SCHEMA)
                connection.executescript(COMPAT_SCHEMA)
                connection.execute("CREATE INDEX IF NOT EXISTS ix_file_operations_lifecycle ON file_operations(owner_subject_id,lifecycle_phase,created_unix_ms ASC)")
            if not has_filesystem_access:
                connection.execute(_FILESYSTEM_ACCESS_SCHEMA)
            connection.execute("SELECT lifecycle_phase FROM file_operations LIMIT 0")
            version = int(connection.execute(
                "SELECT schema_version FROM file_policy_meta WHERE singleton=1"
            ).fetchone()[0])
            if version != SCHEMA_VERSION:
                raise FilePolicyError("unsupported file-policy schema", code="policy_store_unavailable")
            # Shared writable roots require a phase-leading lookup across all
            # accounts. This additive index contains active claims only.
            connection.execute("CREATE INDEX IF NOT EXISTS ix_file_operations_active_claim ON file_operations(lifecycle_phase,owner_subject_id) WHERE lifecycle_phase='host_path_claim'")

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=10.0, isolation_level=None, factory=_ClosingConnection)
        try:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA busy_timeout=10000")
            connection.execute("PRAGMA journal_mode=WAL")
        except BaseException:
            connection.close()
            raise
        return connection

    @staticmethod
    def _begin(connection: sqlite3.Connection) -> None:
        connection.execute("BEGIN IMMEDIATE")

    @staticmethod
    def _generation(connection: sqlite3.Connection) -> int:
        return int(connection.execute(
            "SELECT generation FROM file_policy_meta WHERE singleton=1"
        ).fetchone()[0])

    def get_operation(self, *, owner_subject_id: str, operation_id: str) -> dict[str, Any] | None:
        """Load one owner-scoped Files receipt for lost-response replay."""
        with self._connect() as connection:
            row = connection.execute(
                "SELECT request_digest,generation,receipt_ciphertext FROM file_operations WHERE owner_subject_id=? AND operation_id=?",
                (str(owner_subject_id), str(operation_id)),
            ).fetchone()
        if row is None:
            return None
        try:
            receipt = _parse_json(decrypt(str(row["receipt_ciphertext"])), None)
        except Exception as exc:
            raise FilePolicyError("Files operation receipt is unavailable", code="provider_unavailable") from exc
        if not isinstance(receipt, Mapping):
            raise FilePolicyError("Files operation receipt is unavailable", code="provider_unavailable")
        return {"digest": str(row["request_digest"]), "generation": int(row["generation"]), "receipt": dict(receipt)}

    def list_operations(self, *, owner_subject_id: str, operation_prefix: str = "", phase: str | None = None, offset: int = 0, limit: int = 256) -> list[dict[str, Any]]:
        """Load bounded owner-scoped operation markers for lifecycle reaping."""
        with self._connect() as connection:
            query = (
                "SELECT operation_id,request_digest,generation,receipt_ciphertext,created_unix_ms "
                "FROM file_operations WHERE owner_subject_id=? AND operation_id LIKE ?"
            )
            params: list[Any] = [str(owner_subject_id), f"{str(operation_prefix)}%"]
            if phase is not None:
                query += " AND lifecycle_phase=?"
                params.append(str(phase))
            query += " ORDER BY created_unix_ms ASC LIMIT ? OFFSET ?"
            params.extend([max(0, min(int(limit), 256)), max(0, int(offset))])
            rows = connection.execute(query, tuple(params)).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            try:
                receipt = _parse_json(decrypt(str(row["receipt_ciphertext"])), None)
            except Exception:
                continue
            if isinstance(receipt, Mapping):
                result.append({
                    "operation_id": str(row["operation_id"]),
                    "digest": str(row["request_digest"]),
                    "generation": int(row["generation"]),
                    "created_unix_ms": int(row["created_unix_ms"]),
                    "receipt": dict(receipt),
                })
        return result

    def list_host_media_operations(self, *, owner_subject_id: str, select) -> list[dict[str, Any]]:
        """Select affected receipts from a consistent, paged owner snapshot."""
        result = []
        with self._connect() as connection:
            connection.execute("BEGIN")
            cursor = 0
            while True:
                rows = connection.execute(
                    "SELECT rowid,operation_id,request_digest,generation,receipt_ciphertext FROM file_operations "
                    "WHERE owner_subject_id=? AND substr(operation_id,1,14)=? AND rowid>? ORDER BY rowid LIMIT 128",
                    (str(owner_subject_id), "__host_media__", cursor),
                ).fetchall()
                if not rows:
                    break
                for row in rows:
                    cursor = int(row["rowid"])
                    try:
                        receipt = _parse_json(decrypt(str(row["receipt_ciphertext"])), None)
                    except Exception as exc:
                        raise FilePolicyError("Host media provenance is unavailable", code="provider_unavailable") from exc
                    if not isinstance(receipt, Mapping):
                        raise FilePolicyError("Host media provenance is invalid", code="provider_unavailable")
                    entry = {"operation_id": str(row["operation_id"]), "digest": str(row["request_digest"]),
                             "generation": int(row["generation"]), "receipt": dict(receipt)}
                    if select(entry):
                        result.append(entry)
                        if len(result) > 256:
                            raise FilePolicyError("Host media move exceeds its safe participant bound", code="provider_unavailable")
            connection.commit()
        return result

    def freeze_managed_transfer_item(self, *, owner_subject_id: str, operation_id: str,
                                     item_id: str, plan: Mapping[str, Any], provenance_selector=None) -> dict[str, Any]:
        """Freeze server-built item intent in the existing parent operation receipt."""
        with self._connect() as connection:
            self._begin(connection)
            row = connection.execute(
                "SELECT receipt_ciphertext FROM file_operations WHERE owner_subject_id=? AND operation_id=?",
                (str(owner_subject_id), str(operation_id)),
            ).fetchone()
            if row is None:
                raise FilePolicyError("transfer reservation is unavailable", code="provider_unavailable")
            receipt = _parse_json(decrypt(str(row["receipt_ciphertext"])), None)
            if not isinstance(receipt, dict):
                raise FilePolicyError("transfer reservation is invalid", code="provider_unavailable")
            if receipt.get("_managed_batch_ready") == "aborted":
                raise FilePolicyError("Managed batch was aborted", code="operation_pending")
            if provenance_selector is not None:
                expected_rows = plan.get("provenance_rows")
                if not callable(provenance_selector) or not isinstance(expected_rows, list) or len(expected_rows) > 256:
                    raise FilePolicyError("managed provenance plan is invalid", code="provider_unavailable")
                expected = {str(item.get("operation_id")): (str(item.get("digest")), item.get("before"))
                            for item in expected_rows if isinstance(item, Mapping)}
                if len(expected) != len(expected_rows):
                    raise FilePolicyError("managed provenance plan is invalid", code="provider_unavailable")
                seen = {}
                cursor = 0
                while True:
                    page = connection.execute(
                        "SELECT rowid,operation_id,request_digest,generation,receipt_ciphertext FROM file_operations "
                        "WHERE owner_subject_id=? AND substr(operation_id,1,14)=? AND rowid>? ORDER BY rowid LIMIT 128",
                        (str(owner_subject_id), "__host_media__", cursor),
                    ).fetchall()
                    if not page:
                        break
                    for provenance_row in page:
                        cursor = int(provenance_row["rowid"])
                        value = _parse_json(decrypt(str(provenance_row["receipt_ciphertext"])), None)
                        if not isinstance(value, Mapping):
                            raise FilePolicyError("managed provenance is invalid", code="provider_unavailable")
                        candidate = {"operation_id": str(provenance_row["operation_id"]),
                                     "digest": str(provenance_row["request_digest"]),
                                     "generation": int(provenance_row["generation"]), "receipt": dict(value)}
                        if provenance_selector(candidate):
                            seen[candidate["operation_id"]] = (candidate["digest"], candidate["receipt"])
                            if len(seen) > 256:
                                raise FilePolicyError("managed provenance exceeds its safe bound", code="provider_unavailable")
                if seen != expected:
                    raise FilePolicyError("managed provenance changed", code="resource_changed")
            items = dict(receipt.get("_managed_items") or {})
            current = items.get(str(item_id))
            if current is not None:
                if not isinstance(current, dict) or current.get("plan") != dict(plan):
                    raise FilePolicyError("managed transfer item changed", code="idempotency_conflict")
                connection.commit()
                return dict(current)
            if str(item_id) not in (receipt.get("_item_ids") or []):
                raise FilePolicyError("transfer item is not reserved", code="idempotency_conflict")
            current = {"plan": dict(plan), "phase": "prepared"}
            items[str(item_id)] = current
            receipt["_managed_items"] = items
            connection.execute("UPDATE file_operations SET receipt_ciphertext=? WHERE owner_subject_id=? AND operation_id=?",
                               (encrypt(_json(receipt)), str(owner_subject_id), str(operation_id)))
            connection.commit()
            return dict(current)

    @staticmethod
    def _host_claim_paths(paths: Sequence[str]) -> list[str]:
        result = set()
        for path in paths:
            if not isinstance(path, str) or not path or "\x00" in path or not os.path.isabs(path) or os.path.normpath(path) != path:
                raise FilePolicyError("Host mutation claim path is invalid", code="invalid_operation")
            result.add(path)
        if not result:
            raise FilePolicyError("Host mutation claim is empty", code="invalid_operation")
        return sorted(result)

    @classmethod
    def _host_claim_conflict(cls, connection: sqlite3.Connection, owner: str, paths: list[str],
                             *, skip_operation: str = "", skip_item: str = "", include_legacy_cleanup: bool = False) -> bool:
        """Check active claims; rare Git cleanup also checks bounded legacy custody."""
        # Completed managed operations historically stored cleanup custody in
        # encrypted NULL-phase receipts. Scan those bounded legacy candidates
        # too, so a pre-upgrade unknown cleanup cannot be silently bypassed.
        sql = "SELECT owner_subject_id,operation_id,receipt_ciphertext FROM file_operations WHERE lifecycle_phase='host_path_claim'"
        if include_legacy_cleanup:
            sql += " OR lifecycle_phase IS NULL LIMIT 4097"
        rows = connection.execute(sql)
        if include_legacy_cleanup:
            rows = rows.fetchall()
            if len(rows) > 4096:
                raise FilePolicyError("Host mutation inventory exceeds its safe bound", code="provider_unavailable")
        for row in rows:
            receipt = _parse_json(decrypt(str(row["receipt_ciphertext"])), None)
            if not isinstance(receipt, Mapping):
                raise FilePolicyError("Host mutation claim is unavailable", code="provider_unavailable")
            existing = []
            writer = receipt.get("_host_writer_claim")
            if isinstance(writer, Mapping):
                existing.append(writer.get("paths"))
            managed = receipt.get("_managed_items")
            if isinstance(managed, Mapping):
                for item_id, item in managed.items():
                    if str(row["owner_subject_id"]) == owner and str(row["operation_id"]) == skip_operation and str(item_id) == skip_item:
                        continue
                    if isinstance(item, Mapping) and item.get("phase") in {"claimed", "dispatched", "metadata_pending", "native_committed", "needs_review"}:
                        existing.append(item.get("claim_paths"))
                    if isinstance(item, Mapping) and item.get("cleanup_phase") in {"claimed", "dispatched", "pending"}:
                        existing.append(item.get("cleanup_claim_paths"))
                    if isinstance(item, Mapping) and item.get("undo_phase") in {"claimed", "dispatched", "metadata_pending"}:
                        existing.append(item.get("undo_claim_paths"))
            for candidate in existing:
                if not isinstance(candidate, list):
                    raise FilePolicyError("Host mutation claim is invalid", code="provider_unavailable")
                for old in cls._host_claim_paths(candidate):
                    for new in paths:
                        try:
                            common = os.path.commonpath((old, new))
                        except ValueError:
                            continue
                        if common == old or common == new:
                            return True
        return False

    def has_unsettled_host_mutation(self, *, owner_subject_id: str) -> bool:
        """Report only whether this immutable subject has an active claim."""
        owner = str(owner_subject_id or '').strip()
        if not owner:
            raise FilePolicyError('subject is required', code='subject_required')
        with self._connect() as connection:
            return connection.execute(
                "SELECT 1 FROM file_operations WHERE lifecycle_phase='host_path_claim' AND owner_subject_id=? LIMIT 1",
                (owner,),
            ).fetchone() is not None

    def claim_host_mutation(self, *, owner_subject_id: str, workspace_id: str | None,
                            generation: int, paths: Sequence[str], recovery: Mapping[str, Any] | None = None) -> dict[str, str]:
        """Claim authorized canonical paths for one cooperating ordinary writer."""
        return self._claim_host_mutation(owner_subject_id=owner_subject_id, workspace_id=workspace_id,
            generation=generation, paths=paths, recovery=recovery, include_legacy_cleanup=False)

    def claim_git_cleanup_host_mutation(self, *, owner_subject_id: str, workspace_id: str | None,
                                        generation: int, paths: Sequence[str], recovery: Mapping[str, Any] | None = None) -> dict[str, str]:
        """Private rare cleanup claim; the caller independently owns Files authority."""
        return self._claim_host_mutation(owner_subject_id=owner_subject_id, workspace_id=workspace_id,
            generation=generation, paths=paths, recovery=recovery, include_legacy_cleanup=True)

    def claim_collaboration_text(self, *, owner_subject_id: str, operation_id: str, request_digest: str,
                                 generation: int, workspace_id: str, path: str, binding: Mapping[str, Any],
                                 capture_identity: Mapping[str, str]) -> dict[str, str]:
        """Atomically bind the original edit, native identity and exact Host claim."""
        import re
        owner, operation = str(owner_subject_id), str(operation_id)
        fields={'operation_id','action_id','request_digest','timestamp_millis','expected_fingerprint','desired_sha256','desired_size'}
        checked=self._host_claim_paths([path])
        if (not isinstance(capture_identity,Mapping) or set(capture_identity)!={'account_id','actor_id','workspace_id'}
            or capture_identity.get('account_id')!=owner or any(not isinstance(value,str) or not value or len(value)>256 for value in capture_identity.values())):
            raise FilePolicyError('Collaboration capture unavailable',code='invalid_operation')
        if (not isinstance(binding,Mapping) or set(binding)!=fields or len(checked)!=1 or checked[0]!=path
            or binding.get('operation_id')!=operation or not isinstance(binding.get('action_id'),str)
            or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}',binding['action_id'])
            or not isinstance(binding.get('expected_fingerprint'),str)
            or not re.fullmatch(r'sha256:[0-9a-f]{64}:(0|[1-9][0-9]*)',binding['expected_fingerprint'])
            or not isinstance(binding.get('desired_sha256'),str) or not re.fullmatch(r'[0-9a-f]{64}',binding['desired_sha256'])
            or type(binding.get('desired_size')) is not int or not 0<=binding['desired_size']<=262144
            or type(binding.get('timestamp_millis')) is not int or not 0<=binding['timestamp_millis']<=2**63-1):
            raise FilePolicyError('Collaboration binding unavailable',code='invalid_operation')
        raw=json.dumps(['openclank.files.ordinary-replace.v1',owner,path,binding['expected_fingerprint'],
            binding['desired_sha256'],operation,binding['action_id']],ensure_ascii=False,separators=(',',':')).encode('utf-8')
        if binding.get('request_digest')!=hashlib.sha256(raw).hexdigest():
            raise FilePolicyError('Collaboration binding unavailable',code='invalid_operation')
        token=uuid.uuid4().hex; claim_operation='__host_path_claim__'+token
        with self._connect() as connection:
            self._begin(connection)
            row=connection.execute('SELECT request_digest,generation,lifecycle_phase,receipt_ciphertext FROM file_operations WHERE owner_subject_id=? AND operation_id=?',(owner,operation)).fetchone()
            receipt=_parse_json(decrypt(str(row['receipt_ciphertext'])),None) if row else None
            kind=row['lifecycle_phase'] if row else None
            if (not row or row['request_digest']!=request_digest or row['generation']!=generation or self._generation(connection)!=generation
                or kind not in {'collaboration.text.v1','collaboration.workbook.v1','collaboration.slides.v1','workflow.artifact.v1','ordinary.model.v1','git.discard.v1'} or not isinstance(receipt,dict) or receipt.get('kind')!=kind
                or receipt.get('state')!='pending' or receipt.get('claim') is not None):
                raise FilePolicyError('Collaboration reservation changed',code='operation_pending')
            if self._host_claim_conflict(connection,owner,checked):raise FilePolicyError('Host resource has an active mutation',code='operation_pending')
            claim={'operation_id':claim_operation,'token':token}
            marker={'_host_writer_claim':{'token':token,'workspace_id':workspace_id,'paths':checked,
                'recovery':{'kind':kind,'operation_id':operation,'request_digest':request_digest}}}
            connection.execute('INSERT INTO file_operations(owner_subject_id,operation_id,request_digest,generation,receipt_ciphertext,created_unix_ms,lifecycle_phase) VALUES (?,?,?,?,?,?,?)',
                (owner,claim_operation,token,generation,encrypt(_json(marker)),_now_ms(),'host_path_claim'))
            receipt.update(claim=claim,native_binding=dict(binding),capture_identity=dict(capture_identity),issued=False,retired=False)
            connection.execute('UPDATE file_operations SET receipt_ciphertext=? WHERE owner_subject_id=? AND operation_id=? AND request_digest=?',
                (encrypt(_json(receipt)),owner,operation,request_digest))
            connection.commit();return claim

    def _collaboration_claimed_row(self, connection, *, owner: str, operation: str, digest: str, claim: Mapping[str, str]):
        row=connection.execute('SELECT request_digest,lifecycle_phase,receipt_ciphertext FROM file_operations WHERE owner_subject_id=? AND operation_id=?',(owner,operation)).fetchone()
        kind=row['lifecycle_phase'] if row else None
        receipt=_parse_json(decrypt(str(row['receipt_ciphertext'])),None) if row else None
        held=connection.execute("SELECT request_digest,receipt_ciphertext FROM file_operations WHERE owner_subject_id=? AND operation_id=? AND lifecycle_phase='host_path_claim'",(owner,claim.get('operation_id'))).fetchone()
        payload=_parse_json(decrypt(str(held['receipt_ciphertext'])),None) if held else None
        marker=payload.get('_host_writer_claim') if isinstance(payload,Mapping) else None
        if (not row or row['request_digest']!=digest or kind not in {'collaboration.text.v1','collaboration.workbook.v1','collaboration.slides.v1','workflow.artifact.v1','ordinary.model.v1','git.discard.v1'} or not isinstance(receipt,dict)
            or receipt.get('kind')!=kind or receipt.get('state')!='pending' or receipt.get('claim')!=dict(claim)
            or not held or held['request_digest']!=claim.get('token') or not isinstance(marker,Mapping)
            or marker.get('token')!=claim.get('token') or marker.get('recovery')!={'kind':kind,'operation_id':operation,'request_digest':digest}):
            raise FilePolicyError('Collaboration custody changed',code='operation_pending')
        return receipt

    def mark_collaboration_issued(self, *, owner_subject_id: str, operation_id: str, request_digest: str, claim: Mapping[str,str]) -> None:
        owner,operation=str(owner_subject_id),str(operation_id)
        with self._connect() as connection:
            self._begin(connection)
            receipt=self._collaboration_claimed_row(connection,owner=owner,operation=operation,digest=request_digest,claim=claim)
            if receipt.get('issued') is not False:raise FilePolicyError('Collaboration already issued',code='operation_pending')
            receipt['issued']=True
            connection.execute('UPDATE file_operations SET receipt_ciphertext=? WHERE owner_subject_id=? AND operation_id=? AND request_digest=?',
                (encrypt(_json(receipt)),owner,operation,request_digest));connection.commit()

    def retire_collaboration_io(self, *, owner_subject_id: str, operation_id: str, request_digest: str, claim: Mapping[str,str]) -> None:
        """Original producer records positively settled exchange or process exit."""
        owner,operation=str(owner_subject_id),str(operation_id)
        with self._connect() as connection:
            self._begin(connection)
            receipt=self._collaboration_claimed_row(connection,owner=owner,operation=operation,digest=request_digest,claim=claim)
            if receipt.get('issued') is not True:raise FilePolicyError('Collaboration was not issued',code='operation_pending')
            receipt['retired']=True
            connection.execute('UPDATE file_operations SET receipt_ciphertext=? WHERE owner_subject_id=? AND operation_id=? AND request_digest=?',
                (encrypt(_json(receipt)),owner,operation,request_digest));connection.commit()

    def refuse_collaboration_unclaimed(self, *, owner_subject_id: str, operation_id: str, request_digest: str, state: str) -> None:
        if state not in {'unavailable','conflict'}:raise FilePolicyError('Collaboration outcome unavailable',code='invalid_operation')
        owner,operation=str(owner_subject_id),str(operation_id)
        with self._connect() as connection:
            self._begin(connection)
            row=connection.execute('SELECT request_digest,lifecycle_phase,receipt_ciphertext FROM file_operations WHERE owner_subject_id=? AND operation_id=?',(owner,operation)).fetchone()
            receipt=_parse_json(decrypt(str(row['receipt_ciphertext'])),None) if row else None
            if (not row or row['request_digest']!=request_digest or row['lifecycle_phase'] not in {'collaboration.text.v1','collaboration.workbook.v1','collaboration.slides.v1','workflow.artifact.v1','ordinary.model.v1','git.discard.v1'}
                or not isinstance(receipt,dict) or receipt.get('kind')!=row['lifecycle_phase']
                or receipt.get('state')!='pending' or receipt.get('claim') is not None):
                raise FilePolicyError('Collaboration custody changed',code='operation_pending')
            receipt['state']=state
            connection.execute('UPDATE file_operations SET receipt_ciphertext=? WHERE owner_subject_id=? AND operation_id=? AND request_digest=?',
                (encrypt(_json(receipt)),owner,operation,request_digest));connection.commit()

    def settle_collaboration_text(self, *, owner_subject_id: str, operation_id: str, request_digest: str,
                                  claim: Mapping[str,str], native_status: Mapping[str,Any] | None = None,
                                  known_unsent: bool = False) -> dict:
        """Release exact claim only for known-unsent or matching committed status."""
        owner,operation=str(owner_subject_id),str(operation_id)
        with self._connect() as connection:
            self._begin(connection)
            receipt=self._collaboration_claimed_row(connection,owner=owner,operation=operation,digest=request_digest,claim=claim)
            binding=receipt.get('native_binding') or {}
            if native_status is None and (receipt.get('issued') is False or known_unsent is True):
                receipt['state']='unavailable'
            elif (receipt.get('issued') is True and receipt.get('retired') is True and isinstance(native_status,Mapping) and set(native_status)=={'state','fingerprint'}
                  and native_status.get('state')=='committed'
                  and native_status.get('fingerprint')==f"sha256:{binding.get('desired_sha256')}:{binding.get('desired_size')}"):
                receipt.update(state='complete',revision={'kind':'hostFingerprint','value':native_status['fingerprint']},
                    history={'state':'complete','actionId':binding['action_id']})
            else:
                raise FilePolicyError('Collaboration outcome unknown',code='operation_pending')
            connection.execute('UPDATE file_operations SET receipt_ciphertext=? WHERE owner_subject_id=? AND operation_id=? AND request_digest=?',
                (encrypt(_json(receipt)),owner,operation,request_digest))
            connection.execute('DELETE FROM file_operations WHERE owner_subject_id=? AND operation_id=? AND request_digest=?',
                (owner,claim['operation_id'],claim['token']));connection.commit();return receipt

    def claim_checkpoint_restore(self, *, owner_subject_id: str, operation_id: str, request_digest: str,
                                 generation: int, workspace_id: str | None, path: str) -> dict[str, str]:
        return self._claim_restore_v1(owner_subject_id=owner_subject_id,operation_id=operation_id,request_digest=request_digest,generation=generation,workspace_id=workspace_id,path=path,kind="selected_checkpoint_restore_v1")

    def claim_regular_file_restore(self, *, owner_subject_id: str, operation_id: str, request_digest: str,
                                   generation: int, workspace_id: str | None, path: str) -> dict[str, str]:
        return self._claim_restore_v1(owner_subject_id=owner_subject_id,operation_id=operation_id,request_digest=request_digest,generation=generation,workspace_id=workspace_id,path=path,kind="regular_file_restore_v1")

    def _claim_restore_v1(self, *, owner_subject_id: str, operation_id: str, request_digest: str,
                          generation: int, workspace_id: str | None, path: str, kind: str) -> dict[str, str]:
        """Join the original reserved restore and its exact Host claim atomically."""
        if kind not in {"selected_checkpoint_restore_v1","regular_file_restore_v1"}:raise FilePolicyError("Restore kind unavailable",code="invalid_operation")
        owner, operation = str(owner_subject_id), str(operation_id)
        checked = self._host_claim_paths([path]); token = uuid.uuid4().hex
        claim_operation = "__host_path_claim__" + token
        with self._connect() as connection:
            self._begin(connection)
            row = connection.execute("SELECT request_digest,generation,lifecycle_phase,receipt_ciphertext FROM file_operations WHERE owner_subject_id=? AND operation_id=?", (owner, operation)).fetchone()
            receipt = _parse_json(decrypt(str(row["receipt_ciphertext"])), None) if row else None
            if (not row or row["request_digest"] != request_digest or row["generation"] != generation
                    or row["lifecycle_phase"] != kind or self._generation(connection) != generation
                    or not isinstance(receipt, dict) or receipt.get("kind") != kind
                    or receipt.get("state") != "pending" or receipt.get("claim") is not None):
                raise FilePolicyError("Checkpoint restore reservation changed", code="operation_pending")
            if self._host_claim_conflict(connection, owner, checked):
                raise FilePolicyError("Host resource has an active mutation", code="operation_pending")
            claim = {"operation_id": claim_operation, "token": token}
            marker = {"_host_writer_claim": {"token": token, "workspace_id": workspace_id, "paths": checked,
                "recovery": {"kind": kind, "operation_id": operation, "request_digest": request_digest}}}
            connection.execute("INSERT INTO file_operations(owner_subject_id,operation_id,request_digest,generation,receipt_ciphertext,created_unix_ms,lifecycle_phase) VALUES (?,?,?,?,?,?,?)",
                (owner, claim_operation, token, generation, encrypt(_json(marker)), _now_ms(), "host_path_claim"))
            receipt["claim"] = claim
            connection.execute("UPDATE file_operations SET receipt_ciphertext=? WHERE owner_subject_id=? AND operation_id=? AND request_digest=?",
                (encrypt(_json(receipt)), owner, operation, request_digest))
            connection.commit()
            return claim

    def settle_checkpoint_restore(self, *, owner_subject_id: str, operation_id: str, request_digest: str,
                                  claim: Mapping[str, str], state: str, verified: bool) -> None:
        return self._settle_restore_v1(owner_subject_id=owner_subject_id,operation_id=operation_id,request_digest=request_digest,claim=claim,state=state,verified=verified,kind="selected_checkpoint_restore_v1")

    def settle_regular_file_restore(self, *, owner_subject_id: str, operation_id: str, request_digest: str,
                                    claim: Mapping[str, str], state: str, verified: bool) -> None:
        return self._settle_restore_v1(owner_subject_id=owner_subject_id,operation_id=operation_id,request_digest=request_digest,claim=claim,state=state,verified=verified,kind="regular_file_restore_v1")

    def _settle_restore_v1(self, *, owner_subject_id: str, operation_id: str, request_digest: str,
                           claim: Mapping[str, str], state: str, verified: bool, kind: str) -> None:
        """Settle only an exact original claim after positive original evidence."""
        if kind not in {"selected_checkpoint_restore_v1","regular_file_restore_v1"}:raise FilePolicyError("Restore kind unavailable",code="invalid_operation")
        if state not in {"complete", "conflict", "partial", "refused"} or type(verified) is not bool:
            raise FilePolicyError("Checkpoint restore outcome unavailable", code="invalid_operation")
        owner, operation = str(owner_subject_id), str(operation_id)
        with self._connect() as connection:
            self._begin(connection)
            row = connection.execute("SELECT request_digest,lifecycle_phase,receipt_ciphertext FROM file_operations WHERE owner_subject_id=? AND operation_id=?", (owner, operation)).fetchone()
            receipt = _parse_json(decrypt(str(row["receipt_ciphertext"])), None) if row else None
            saved = receipt.get("claim") if isinstance(receipt, dict) else None
            if (not row or row["request_digest"] != request_digest or row["lifecycle_phase"] != kind
                    or not isinstance(receipt, dict) or receipt.get("kind") != kind or saved != dict(claim)):
                raise FilePolicyError("Checkpoint restore custody changed", code="operation_pending")
            if receipt.get("state") == state and receipt.get("verified") is verified:
                connection.commit()
                return
            if receipt.get("state") != "pending":
                raise FilePolicyError("Checkpoint restore custody changed", code="operation_pending")
            claimed = connection.execute("SELECT request_digest,receipt_ciphertext FROM file_operations WHERE owner_subject_id=? AND operation_id=? AND lifecycle_phase='host_path_claim'", (owner, claim.get("operation_id"))).fetchone()
            payload = _parse_json(decrypt(str(claimed["receipt_ciphertext"])), None) if claimed else None
            writer = payload.get("_host_writer_claim") if isinstance(payload, Mapping) else None
            if (not claimed or claimed["request_digest"] != claim.get("token") or not isinstance(writer, Mapping)
                    or writer.get("token") != claim.get("token") or writer.get("recovery") != {"kind": kind, "operation_id": operation, "request_digest": request_digest}):
                raise FilePolicyError("Checkpoint restore custody changed", code="operation_pending")
            receipt.update(state=state, verified=verified)
            connection.execute("UPDATE file_operations SET receipt_ciphertext=? WHERE owner_subject_id=? AND operation_id=? AND request_digest=?", (encrypt(_json(receipt)), owner, operation, request_digest))
            connection.execute("DELETE FROM file_operations WHERE owner_subject_id=? AND operation_id=? AND request_digest=?", (owner, claim["operation_id"], claim["token"]))
            connection.commit()

    def refuse_unclaimed_checkpoint_restore(self, *, owner_subject_id: str, operation_id: str, request_digest: str) -> None:
        return self._refuse_unclaimed_restore_v1(owner_subject_id=owner_subject_id,operation_id=operation_id,request_digest=request_digest,kind="selected_checkpoint_restore_v1")

    def refuse_unclaimed_regular_file_restore(self, *, owner_subject_id: str, operation_id: str, request_digest: str) -> None:
        return self._refuse_unclaimed_restore_v1(owner_subject_id=owner_subject_id,operation_id=operation_id,request_digest=request_digest,kind="regular_file_restore_v1")

    def _refuse_unclaimed_restore_v1(self, *, owner_subject_id: str, operation_id: str, request_digest: str, kind: str) -> None:
        """Record refusal only while the original reservation never acquired custody."""
        if kind not in {"selected_checkpoint_restore_v1","regular_file_restore_v1"}:raise FilePolicyError("Restore kind unavailable",code="invalid_operation")
        owner, operation = str(owner_subject_id), str(operation_id)
        with self._connect() as connection:
            self._begin(connection)
            row = connection.execute("SELECT request_digest,lifecycle_phase,receipt_ciphertext FROM file_operations WHERE owner_subject_id=? AND operation_id=?", (owner, operation)).fetchone()
            receipt = _parse_json(decrypt(str(row["receipt_ciphertext"])), None) if row else None
            if (not row or row["request_digest"] != request_digest or row["lifecycle_phase"] != kind or not isinstance(receipt, dict)
                    or receipt.get("kind") != kind or receipt.get("state") != "pending"
                    or receipt.get("claim") is not None):
                raise FilePolicyError("Checkpoint restore custody changed", code="operation_pending")
            receipt.update(state="refused", verified=False)
            connection.execute("UPDATE file_operations SET receipt_ciphertext=? WHERE owner_subject_id=? AND operation_id=? AND request_digest=?", (encrypt(_json(receipt)), owner, operation, request_digest))
            connection.commit()

    def _claim_host_mutation(self, *, owner_subject_id: str, workspace_id: str | None,
                             generation: int, paths: Sequence[str], recovery: Mapping[str, Any] | None,
                             include_legacy_cleanup: bool) -> dict[str, str]:
        owner = str(owner_subject_id)
        checked = self._host_claim_paths(paths)
        token = uuid.uuid4().hex
        operation = "__host_path_claim__" + token
        receipt = {"_host_writer_claim": {"token": token, "workspace_id": None if workspace_id is None else str(workspace_id), "paths": checked}}
        if recovery is not None:
            receipt["_host_writer_claim"]["recovery"] = dict(recovery)
        with self._connect() as connection:
            self._begin(connection)
            if self._generation(connection) != int(generation):
                raise FilePolicyError("File policy generation changed", code="policy_generation_changed")
            if self._host_claim_conflict(connection, owner, checked, include_legacy_cleanup=include_legacy_cleanup):
                raise FilePolicyError("Host resource has an active mutation", code="operation_pending")
            connection.execute(
                "INSERT INTO file_operations(owner_subject_id,operation_id,request_digest,generation,receipt_ciphertext,created_unix_ms,lifecycle_phase) VALUES (?,?,?,?,?,?,?)",
                (owner, operation, token, int(generation), encrypt(_json(receipt)), _now_ms(), "host_path_claim"),
            )
            connection.commit()
        return {"operation_id": operation, "token": token}

    def inspect_host_mutation(self, *, owner_subject_id: str, operation_id: str, token: str) -> Mapping[str, Any] | None:
        """Read the exact owner-bound pending claim; absence proves no outcome."""
        with self._connect() as connection:
            row = connection.execute("SELECT request_digest,receipt_ciphertext FROM file_operations WHERE owner_subject_id=? AND operation_id=? AND lifecycle_phase='host_path_claim'", (str(owner_subject_id), str(operation_id))).fetchone()
            if row is None or str(row["request_digest"]) != str(token):
                return None
            receipt = _parse_json(decrypt(str(row["receipt_ciphertext"])), None)
            claim = receipt.get("_host_writer_claim") if isinstance(receipt, Mapping) else None
            return dict(claim) if isinstance(claim, Mapping) and claim.get("token") == str(token) else None

    def release_host_mutation(self, *, owner_subject_id: str, operation_id: str, token: str) -> None:
        """Release only the exact ordinary claim after definite I/O completion."""
        with self._connect() as connection:
            self._begin(connection)
            row = connection.execute(
                "SELECT request_digest,receipt_ciphertext FROM file_operations WHERE owner_subject_id=? AND operation_id=? AND lifecycle_phase=?",
                (str(owner_subject_id), str(operation_id), "host_path_claim"),
            ).fetchone()
            if row is None:
                raise FilePolicyError("Host mutation claim is unavailable", code="operation_pending")
            receipt = _parse_json(decrypt(str(row["receipt_ciphertext"])), None)
            claim = receipt.get("_host_writer_claim") if isinstance(receipt, Mapping) else None
            if not isinstance(claim, Mapping) or claim.get("token") != str(token) or str(row["request_digest"]) != str(token):
                raise FilePolicyError("Host mutation claim identity changed", code="idempotency_conflict")
            connection.execute("DELETE FROM file_operations WHERE owner_subject_id=? AND operation_id=? AND request_digest=?",
                               (str(owner_subject_id), str(operation_id), str(token)))
            connection.commit()

    @classmethod
    def _managed_plan_claim_paths(cls, plan: Mapping[str, Any]) -> list[str]:
        paths = [plan.get("source"), plan.get("destination")]
        for proof in [*(plan.get("asset_proofs") or []), *(plan.get("reference_proofs") or [])]:
            paths.append(proof.get("path") if isinstance(proof, Mapping) else None)
            if plan.get("kind") == "folder" and isinstance(proof, Mapping) and proof.get("destination") is not None:
                paths.append(proof.get("destination"))
        for participant in (plan.get("payload") or {}).get("participants", []):
            if not isinstance(participant, Mapping):
                raise FilePolicyError("Managed transfer plan is invalid", code="provider_unavailable")
            paths.append(participant.get("source"))
            if participant.get("destination") is not None:
                paths.append(participant.get("destination"))
        return cls._host_claim_paths(paths)

    @staticmethod
    def _managed_items_claimed(items: Mapping[str, Any]) -> bool:
        return any(isinstance(value, Mapping) and (
            value.get("phase") in {"claimed", "dispatched", "metadata_pending", "native_committed", "needs_review"}
            or value.get("cleanup_phase") in {"claimed", "dispatched", "pending"}
            or value.get("undo_phase") in {"claimed", "dispatched", "metadata_pending"}
        ) for value in items.values())

    def claim_managed_folder_cleanup(self, *, owner_subject_id: str, operation_id: str, item_id: str,
                                     native_operation_id: str, generation: int) -> dict[str, Any]:
        """Recover the same exact settled folder's cleanup identity."""
        owner, operation, item_id = str(owner_subject_id), str(operation_id), str(item_id)
        with self._connect() as connection:
            self._begin(connection)
            row = connection.execute("SELECT generation,receipt_ciphertext FROM file_operations WHERE owner_subject_id=? AND operation_id=?", (owner, operation)).fetchone()
            if row is None or int(row["generation"]) != int(generation) or self._generation(connection) != int(generation):
                raise FilePolicyError("Managed cleanup authority changed", code="policy_generation_changed")
            receipt = _parse_json(decrypt(str(row["receipt_ciphertext"])), None)
            items = receipt.get("_managed_items") if isinstance(receipt, dict) else None
            item = items.get(item_id) if isinstance(items, dict) else None
            plan = item.get("plan") if isinstance(item, dict) else None
            if (not isinstance(plan, dict) or not (plan.get("kind") == "folder" or is_closed_managed_file_plan(plan)) or item.get("phase") != "complete"
                    or plan.get("native_operation_id") != str(native_operation_id) or plan.get("generation") != int(generation)):
                raise FilePolicyError("Managed cleanup identity changed", code="idempotency_conflict")
            if item.get("undo_phase") in {"claimed", "dispatched", "metadata_pending"}:
                raise FilePolicyError("Managed Undo is pending", code="operation_pending")
            phase = item.get("cleanup_phase")
            if phase == "complete":
                connection.commit(); return {**item, "acquired": False}
            checked = self._managed_plan_claim_paths(plan)
            if phase in {"claimed", "dispatched", "pending"}:
                if not item.get("cleanup_token") or item.get("cleanup_claim_paths") != checked:
                    raise FilePolicyError("Managed cleanup claim changed", code="idempotency_conflict")
            else:
                if phase is not None:
                    raise FilePolicyError("Managed cleanup identity changed", code="idempotency_conflict")
                if self._host_claim_conflict(connection, owner, checked, skip_operation=operation, skip_item=item_id):
                    raise FilePolicyError("Host resource has an active mutation", code="operation_pending")
                item["cleanup_token"] = uuid.uuid4().hex
                item["cleanup_claim_paths"] = checked
                item["cleanup_phase"] = "claimed"
                connection.execute("UPDATE file_operations SET receipt_ciphertext=?,lifecycle_phase='host_path_claim' WHERE owner_subject_id=? AND operation_id=?",
                                   (encrypt(_json(receipt)), owner, operation))
            connection.commit(); return {**item, "acquired": True}

    def mark_managed_folder_cleanup(self, *, owner_subject_id: str, operation_id: str, item_id: str,
                                    native_operation_id: str, token: str, phase: str) -> None:
        if phase not in {"dispatched", "pending", "complete"}:
            raise FilePolicyError("Managed cleanup phase is invalid", code="idempotency_conflict")
        owner, operation, item_id = str(owner_subject_id), str(operation_id), str(item_id)
        with self._connect() as connection:
            self._begin(connection)
            row = connection.execute("SELECT receipt_ciphertext FROM file_operations WHERE owner_subject_id=? AND operation_id=?", (owner, operation)).fetchone()
            receipt = _parse_json(decrypt(str(row["receipt_ciphertext"])), None) if row else None
            items = receipt.get("_managed_items") if isinstance(receipt, dict) else None
            item = items.get(item_id) if isinstance(items, dict) else None
            plan = item.get("plan") if isinstance(item, dict) else None
            if (not isinstance(plan, dict) or plan.get("native_operation_id") != str(native_operation_id)
                    or item.get("cleanup_token") != str(token) or item.get("cleanup_phase") not in {"claimed", "dispatched", "pending", "complete"}
                    or item.get("cleanup_claim_paths") != self._managed_plan_claim_paths(plan)):
                raise FilePolicyError("Managed cleanup claim changed", code="idempotency_conflict")
            if item.get("cleanup_phase") == "complete":
                connection.commit(); return
            if phase == "dispatched" and self._generation(connection) != int(plan.get("generation", -1)):
                raise FilePolicyError("Managed cleanup authority changed", code="policy_generation_changed")
            if phase == "dispatched" and self._host_claim_conflict(connection, owner, item["cleanup_claim_paths"], skip_operation=operation, skip_item=item_id):
                raise FilePolicyError("Host resource has an active mutation", code="operation_pending")
            if phase != "dispatched" and item.get("cleanup_phase") not in {"dispatched", "pending"}:
                raise FilePolicyError("Managed cleanup was not dispatched", code="idempotency_conflict")
            item["cleanup_phase"] = phase
            connection.execute("UPDATE file_operations SET receipt_ciphertext=?,lifecycle_phase=? WHERE owner_subject_id=? AND operation_id=?",
                               (encrypt(_json(receipt)), "host_path_claim" if self._managed_items_claimed(items) else None, owner, operation))
            connection.commit()

    def claim_managed_transfer_item(self, *, owner_subject_id: str, operation_id: str,
                                    item_id: str, native_operation_id: str, generation: int) -> dict[str, Any]:
        """Fence a frozen managed item without holding a transaction across I/O."""
        owner, operation, item_id = str(owner_subject_id), str(operation_id), str(item_id)
        with self._connect() as connection:
            self._begin(connection)
            if self._generation(connection) != int(generation):
                raise FilePolicyError("File policy generation changed", code="policy_generation_changed")
            row = connection.execute("SELECT generation,receipt_ciphertext FROM file_operations WHERE owner_subject_id=? AND operation_id=?",
                                     (owner, operation)).fetchone()
            if row is None or int(row["generation"]) != int(generation):
                raise FilePolicyError("Managed transfer reservation changed", code="resource_changed")
            receipt = _parse_json(decrypt(str(row["receipt_ciphertext"])), None)
            items = receipt.get("_managed_items") if isinstance(receipt, dict) else None
            if isinstance(receipt, dict) and receipt.get("_managed_batch_ready") == "aborted":
                raise FilePolicyError("Managed batch was aborted", code="operation_pending")
            item = items.get(item_id) if isinstance(items, dict) else None
            plan = item.get("plan") if isinstance(item, dict) else None
            if not isinstance(plan, dict) or plan.get("native_operation_id") != str(native_operation_id) or plan.get("generation") != int(generation):
                raise FilePolicyError("Managed transfer identity changed", code="idempotency_conflict")
            checked = self._managed_plan_claim_paths(plan)
            if item.get("phase") == "claimed":
                if item.get("claim_paths") != checked:
                    raise FilePolicyError("Managed transfer claim changed", code="idempotency_conflict")
                # Preflight is read-only. Replacing its token atomically fences
                # any older preflight before it can dispatch or reject.
                item["claim_token"] = uuid.uuid4().hex
                connection.execute("UPDATE file_operations SET receipt_ciphertext=? WHERE owner_subject_id=? AND operation_id=?",
                                   (encrypt(_json(receipt)), owner, operation))
                connection.commit()
                return {**item, "acquired": True}
            if item.get("phase") in {"dispatched", "metadata_pending", "native_committed", "needs_review"}:
                if item.get("claim_paths") != checked:
                    raise FilePolicyError("Managed transfer claim changed", code="idempotency_conflict")
                connection.commit()
                return {**item, "acquired": False}
            if item.get("phase") != "prepared" or self._host_claim_conflict(connection, owner, checked, skip_operation=operation, skip_item=item_id):
                raise FilePolicyError("Host resource has an active mutation", code="operation_pending")
            # Recheck the complete app-known set in the same transaction that
            # publishes the claim. A new asset between freeze and claim must
            # not escape the frozen plan merely because document bytes match.
            from src.openclank.media_ownership import MediaProvenance
            expected_rows = plan.get("provenance_rows")
            if not isinstance(expected_rows, list) or len(expected_rows) > 256:
                raise FilePolicyError("managed provenance plan is invalid", code="provider_unavailable")
            expected = {str(value.get("operation_id")): (str(value.get("digest")), value.get("before"))
                        for value in expected_rows if isinstance(value, Mapping)}
            if len(expected) != len(expected_rows):
                raise FilePolicyError("managed provenance plan is invalid", code="provider_unavailable")
            seen = {}
            cursor = 0
            while True:
                page = connection.execute(
                    "SELECT rowid,operation_id,request_digest,receipt_ciphertext FROM file_operations "
                    "WHERE owner_subject_id=? AND substr(operation_id,1,14)=? AND rowid>? ORDER BY rowid LIMIT 128",
                    (owner, "__host_media__", cursor),
                ).fetchall()
                if not page:
                    break
                for provenance_row in page:
                    cursor = int(provenance_row["rowid"])
                    value = _parse_json(decrypt(str(provenance_row["receipt_ciphertext"])), None)
                    if not isinstance(value, Mapping):
                        raise FilePolicyError("managed provenance is invalid", code="provider_unavailable")
                    provenance = value.get("provenance")
                    if not isinstance(provenance, Mapping):
                        continue
                    try:
                        record = MediaProvenance.from_receipt(provenance)
                        document = os.path.normpath(os.path.join(record.canonical_root, record.document_path))
                    except Exception as exc:
                        raise FilePolicyError("managed provenance is invalid", code="provider_unavailable") from exc
                    source = plan.get("source")
                    affected = document == source
                    if not affected and plan.get("kind") == "folder" and isinstance(source, str):
                        try:
                            affected = os.path.commonpath((source, document)) == source
                        except ValueError:
                            affected = False
                    if affected:
                        seen[str(provenance_row["operation_id"])] = (str(provenance_row["request_digest"]), dict(value))
                        if len(seen) > 256:
                            raise FilePolicyError("managed provenance exceeds its safe bound", code="provider_unavailable")
            if seen != expected:
                raise FilePolicyError("managed provenance changed before claim", code="resource_changed")
            item["claim_paths"] = checked
            item["claim_token"] = uuid.uuid4().hex
            item["phase"] = "claimed"
            connection.execute("UPDATE file_operations SET receipt_ciphertext=?,lifecycle_phase=? WHERE owner_subject_id=? AND operation_id=?",
                               (encrypt(_json(receipt)), "host_path_claim", owner, operation))
            connection.commit()
            return {**item, "acquired": True}

    def activate_managed_transfer_batch(self, *, owner_subject_id: str, operation_id: str,
                                        generation: int, claim_tokens: Mapping[str, str]) -> None:
        """Publish the existing batch barrier only for the exact frozen claims."""
        with self._connect() as connection:
            self._begin(connection)
            row = connection.execute("SELECT generation,receipt_ciphertext FROM file_operations WHERE owner_subject_id=? AND operation_id=?",
                                     (str(owner_subject_id), str(operation_id))).fetchone()
            if row is None or int(row["generation"]) != int(generation) or self._generation(connection) != int(generation):
                raise FilePolicyError("Managed batch authority changed", code="policy_generation_changed")
            receipt = _parse_json(decrypt(str(row["receipt_ciphertext"])), None)
            items = receipt.get("_managed_items") if isinstance(receipt, dict) else None
            expected = receipt.get("_item_ids") if isinstance(receipt, dict) else None
            if (receipt.get("_managed_batch_ready") is not False or not isinstance(expected, list) or len(expected) < 2 or len(set(expected)) != len(expected)
                    or set(expected) != set(claim_tokens) or not isinstance(items, dict)):
                raise FilePolicyError("Managed batch identity changed", code="idempotency_conflict")
            roots = set()
            for item_id in expected:
                item = items.get(item_id)
                plan = item.get("plan") if isinstance(item, dict) else None
                if not isinstance(plan, dict) or item.get("phase") != "claimed" or item.get("claim_token") != claim_tokens[item_id]:
                    raise FilePolicyError("Managed batch claim changed", code="idempotency_conflict")
                selected = plan.get("managed_root")
                if not isinstance(selected, Mapping):
                    raise FilePolicyError("Managed batch root is unavailable", code="idempotency_conflict")
                roots.add((str(selected.get("root_id")), str(selected.get("root")), int(selected.get("generation", -1))))
            if len(roots) != 1:
                raise FilePolicyError("Managed batch roots differ", code="idempotency_conflict")
            receipt["_managed_batch_ready"] = True
            connection.execute("UPDATE file_operations SET receipt_ciphertext=? WHERE owner_subject_id=? AND operation_id=?",
                               (encrypt(_json(receipt)), str(owner_subject_id), str(operation_id)))
            connection.commit()

    def abort_managed_transfer_batch(self, *, owner_subject_id: str, operation_id: str,
                                     claim_tokens: Mapping[str, str]) -> None:
        """Abort only an unactivated batch without stealing another caller's claim."""
        with self._connect() as connection:
            self._begin(connection)
            row = connection.execute("SELECT receipt_ciphertext FROM file_operations WHERE owner_subject_id=? AND operation_id=?",
                                     (str(owner_subject_id), str(operation_id))).fetchone()
            receipt = _parse_json(decrypt(str(row["receipt_ciphertext"])), None) if row is not None else None
            if not isinstance(receipt, dict) or receipt.get("_managed_batch_ready") is not False:
                raise FilePolicyError("Managed batch is no longer abortable", code="operation_pending")
            items = receipt.get("_managed_items") or {}
            if not isinstance(items, dict):
                raise FilePolicyError("Managed batch identity changed", code="idempotency_conflict")
            for item_id, item in items.items():
                if not isinstance(item, dict) or item.get("phase") not in {"prepared", "claimed"}:
                    raise FilePolicyError("Managed batch is no longer abortable", code="operation_pending")
                if item.get("phase") == "claimed" and item.get("claim_token") != claim_tokens.get(item_id):
                    raise FilePolicyError("Managed batch claim changed", code="operation_pending")
            for item in items.values():
                item["phase"] = "rejected"
            receipt["_managed_batch_ready"] = "aborted"
            connection.execute("UPDATE file_operations SET receipt_ciphertext=?,lifecycle_phase=? WHERE owner_subject_id=? AND operation_id=?",
                               (encrypt(_json(receipt)), "host_path_claim" if self._managed_items_claimed(items) else None, str(owner_subject_id), str(operation_id)))
            connection.commit()

    def mark_managed_transfer_item(self, *, owner_subject_id: str, operation_id: str, item_id: str,
                                   native_operation_id: str, claim_token: str, phase: str,
                                   review_outcome: str | None = None) -> dict[str, Any]:
        """Persist the exact dispatch/commit boundary without releasing its claim."""
        if phase not in {"dispatched", "native_committed", "needs_review"}:
            raise FilePolicyError("Managed transfer phase is invalid", code="invalid_operation")
        if phase == "needs_review" and review_outcome not in {"rejected_needs_review", "partial_needs_review", "committed_needs_review"}:
            raise FilePolicyError("Managed review outcome is invalid", code="invalid_operation")
        owner, operation, item_id = str(owner_subject_id), str(operation_id), str(item_id)
        with self._connect() as connection:
            self._begin(connection)
            row = connection.execute("SELECT receipt_ciphertext FROM file_operations WHERE owner_subject_id=? AND operation_id=?",
                                     (owner, operation)).fetchone()
            receipt = _parse_json(decrypt(str(row["receipt_ciphertext"])), None) if row is not None else None
            items = receipt.get("_managed_items") if isinstance(receipt, dict) else None
            item = items.get(item_id) if isinstance(items, dict) else None
            plan = item.get("plan") if isinstance(item, dict) else None
            if (not isinstance(plan, dict) or plan.get("native_operation_id") != str(native_operation_id)
                    or not isinstance(item.get("claim_paths"), list) or item.get("claim_token") != str(claim_token)):
                raise FilePolicyError("Managed transfer identity changed", code="idempotency_conflict")
            previous = item.get("phase")
            if previous in {"complete", "metadata_pending"} and phase == "native_committed":
                connection.commit()
                return dict(item)
            if previous == phase:
                if phase == "needs_review" and item.get("review_outcome") != review_outcome:
                    raise FilePolicyError("Managed review outcome changed", code="idempotency_conflict")
                connection.commit()
                return dict(item)
            if (previous, phase) not in {("claimed", "dispatched"), ("dispatched", "native_committed"), ("dispatched", "needs_review")}:
                raise FilePolicyError("Managed transfer phase changed", code="idempotency_conflict")
            if phase == "dispatched" and "_managed_batch_ready" in receipt and receipt.get("_managed_batch_ready") is not True:
                raise FilePolicyError("Managed batch is not ready", code="operation_pending")
            if phase == "dispatched" and self._generation(connection) != int(plan.get("generation", -1)):
                raise FilePolicyError("File policy generation changed", code="policy_generation_changed")
            item["phase"] = phase
            if phase == "needs_review": item["review_outcome"] = review_outcome
            connection.execute("UPDATE file_operations SET receipt_ciphertext=?,lifecycle_phase=? WHERE owner_subject_id=? AND operation_id=?",
                               (encrypt(_json(receipt)), "host_path_claim", owner, operation))
            connection.commit()
            return dict(item)

    def reject_unstarted_managed_transfer_item(self, *, owner_subject_id: str, operation_id: str, item_id: str,
                                               native_operation_id: str, claim_token: str) -> None:
        """Retire only the acquiring caller's item before any native dispatch."""
        owner, operation, item_id = str(owner_subject_id), str(operation_id), str(item_id)
        with self._connect() as connection:
            self._begin(connection)
            row = connection.execute("SELECT receipt_ciphertext FROM file_operations WHERE owner_subject_id=? AND operation_id=?",
                                     (owner, operation)).fetchone()
            receipt = _parse_json(decrypt(str(row["receipt_ciphertext"])), None) if row is not None else None
            items = receipt.get("_managed_items") if isinstance(receipt, dict) else None
            item = items.get(item_id) if isinstance(items, dict) else None
            plan = item.get("plan") if isinstance(item, dict) else None
            if (not isinstance(plan, dict) or plan.get("native_operation_id") != str(native_operation_id)
                    or item.get("claim_token") != str(claim_token) or item.get("phase") != "claimed"):
                raise FilePolicyError("Managed transfer is not safely rejectable", code="operation_pending")
            item["phase"] = "rejected"
            still_claimed = self._managed_items_claimed(items)
            connection.execute("UPDATE file_operations SET receipt_ciphertext=?,lifecycle_phase=? WHERE owner_subject_id=? AND operation_id=?",
                               (encrypt(_json(receipt)), "host_path_claim" if still_claimed else None, owner, operation))
            connection.commit()

    def claim_managed_folder_undo(self, *, owner_subject_id: str, operation_id: str,
                                  item_id: str, native_operation_id: str, undo_operation_id: str,
                                  generation: int) -> dict[str, Any]:
        """Fence an inverse using the settled forward receipt and exact provenance."""
        owner, operation, item_id = str(owner_subject_id), str(operation_id), str(item_id)
        with self._connect() as connection:
            self._begin(connection)
            row = connection.execute("SELECT generation,receipt_ciphertext FROM file_operations WHERE owner_subject_id=? AND operation_id=?", (owner, operation)).fetchone()
            if row is None or int(row["generation"]) != int(generation) or self._generation(connection) != int(generation):
                raise FilePolicyError("Managed Undo authority changed", code="policy_generation_changed")
            receipt = _parse_json(decrypt(str(row["receipt_ciphertext"])), None)
            items = receipt.get("_managed_items") if isinstance(receipt, dict) else None
            item = items.get(item_id) if isinstance(items, dict) else None
            plan = item.get("plan") if isinstance(item, dict) else None
            if (not isinstance(plan, dict) or plan.get("kind") != "folder" or plan.get("destination_frame") is None
                    or plan.get("native_operation_id") != str(native_operation_id) or item.get("phase") != "complete"
                    or item.get("cleanup_phase") in {"claimed", "dispatched", "pending"}):
                raise FilePolicyError("Managed Undo identity changed", code="idempotency_conflict")
            paths = self._managed_plan_claim_paths(plan)
            phase = item.get("undo_phase")
            if phase is not None:
                if item.get("undo_operation_id") != str(undo_operation_id) or item.get("undo_claim_paths") != paths:
                    raise FilePolicyError("Managed Undo identity changed", code="idempotency_conflict")
                if phase not in {"claimed", "dispatched", "metadata_pending", "complete", "refused"}:
                    raise FilePolicyError("Managed Undo state changed", code="idempotency_conflict")
                connection.commit(); return {"phase": phase}
            if self._host_claim_conflict(connection, owner, paths, skip_operation=operation, skip_item=item_id):
                raise FilePolicyError("Host resource has an active mutation", code="operation_pending")
            rows = plan.get("provenance_rows")
            if not isinstance(rows, list) or len(rows) > 256:
                raise FilePolicyError("Managed provenance plan is invalid", code="provider_unavailable")
            seen = set()
            for expected in rows:
                if not isinstance(expected, dict):
                    raise FilePolicyError("Managed provenance plan is invalid", code="provider_unavailable")
                key = str(expected.get("operation_id") or "")
                if not key.startswith("__host_media__") or key in seen or key == operation:
                    raise FilePolicyError("Managed provenance identity is invalid", code="provider_unavailable")
                seen.add(key)
                current = connection.execute("SELECT request_digest,receipt_ciphertext FROM file_operations WHERE owner_subject_id=? AND operation_id=?", (owner, key)).fetchone()
                if (current is None or str(current["request_digest"]) != str(expected.get("digest") or "")
                        or not isinstance(expected.get("before"), dict) or not isinstance(expected.get("after"), dict)
                        or _parse_json(decrypt(str(current["receipt_ciphertext"])), None) != expected["after"]):
                    raise FilePolicyError("Managed provenance changed", code="resource_changed")
            # Reject a newly introduced managed document in the moved subtree;
            # the frozen inverse cannot account for its provenance or assets.
            from src.openclank.media_ownership import MediaProvenance
            expected_ids = {str(value["operation_id"]) for value in rows}
            seen_ids, cursor = set(), 0
            while True:
                page = connection.execute(
                    "SELECT rowid,operation_id,receipt_ciphertext FROM file_operations "
                    "WHERE owner_subject_id=? AND substr(operation_id,1,14)=? AND rowid>? ORDER BY rowid LIMIT 128",
                    (owner, "__host_media__", cursor),
                ).fetchall()
                if not page: break
                for provenance_row in page:
                    cursor = int(provenance_row["rowid"])
                    value = _parse_json(decrypt(str(provenance_row["receipt_ciphertext"])), None)
                    if not isinstance(value, Mapping):
                        raise FilePolicyError("Managed provenance is invalid", code="provider_unavailable")
                    provenance = value.get("provenance")
                    if not isinstance(provenance, Mapping): continue
                    try:
                        record = MediaProvenance.from_receipt(provenance)
                        document = os.path.normpath(os.path.join(record.canonical_root, record.document_path))
                        affected = os.path.commonpath((str(plan["destination"]), document)) == str(plan["destination"])
                    except Exception as exc:
                        raise FilePolicyError("Managed provenance is invalid", code="provider_unavailable") from exc
                    if affected:
                        seen_ids.add(str(provenance_row["operation_id"]))
                        if len(seen_ids) > 256:
                            raise FilePolicyError("Managed provenance exceeds its safe bound", code="provider_unavailable")
            if seen_ids != expected_ids:
                raise FilePolicyError("Managed provenance changed", code="resource_changed")
            item.update(undo_phase="claimed", undo_operation_id=str(undo_operation_id), undo_claim_paths=paths)
            connection.execute("UPDATE file_operations SET receipt_ciphertext=?,lifecycle_phase='host_path_claim' WHERE owner_subject_id=? AND operation_id=?", (encrypt(_json(receipt)), owner, operation))
            connection.commit(); return {"phase": "claimed"}

    def finish_managed_folder_undo(self, *, owner_subject_id: str, operation_id: str, item_id: str,
                                   undo_operation_id: str, outcome: str) -> bool:
        """Settle all exact inverse provenance rows atomically after Native commits."""
        if outcome not in {"dispatched", "committed", "refused"}:
            raise FilePolicyError("Managed Undo outcome is invalid", code="idempotency_conflict")
        owner, operation, item_id = str(owner_subject_id), str(operation_id), str(item_id)
        with self._connect() as connection:
            self._begin(connection)
            row = connection.execute("SELECT receipt_ciphertext FROM file_operations WHERE owner_subject_id=? AND operation_id=?", (owner, operation)).fetchone()
            receipt = _parse_json(decrypt(str(row["receipt_ciphertext"])), None) if row else None
            items = receipt.get("_managed_items") if isinstance(receipt, dict) else None
            item = items.get(item_id) if isinstance(items, dict) else None
            plan = item.get("plan") if isinstance(item, dict) else None
            if (not isinstance(plan, dict) or plan.get("kind") != "folder" or item.get("phase") != "complete"
                    or item.get("undo_operation_id") != str(undo_operation_id)
                    or item.get("undo_claim_paths") != self._managed_plan_claim_paths(plan)
                    or item.get("undo_phase") not in {"claimed", "dispatched", "metadata_pending", "complete", "refused"}):
                raise FilePolicyError("Managed Undo identity changed", code="idempotency_conflict")
            phase = item["undo_phase"]
            if phase in {"complete", "refused"}:
                connection.commit(); return phase == "complete"
            if outcome == "dispatched":
                if phase == "claimed": item["undo_phase"] = "dispatched"
            elif outcome == "refused":
                if phase == "metadata_pending":
                    raise FilePolicyError("Managed Undo already committed", code="idempotency_conflict")
                item["undo_phase"] = "refused"
            else:
                rows = plan.get("provenance_rows")
                if not isinstance(rows, list) or len(rows) > 256:
                    raise FilePolicyError("Managed provenance plan is invalid", code="provider_unavailable")
                checked, seen, valid = [], set(), True
                for expected in rows:
                    if not isinstance(expected, dict):
                        raise FilePolicyError("Managed provenance plan is invalid", code="provider_unavailable")
                    key = str(expected.get("operation_id") or "")
                    if not key.startswith("__host_media__") or key in seen or key == operation:
                        raise FilePolicyError("Managed provenance identity is invalid", code="provider_unavailable")
                    seen.add(key)
                    current = connection.execute("SELECT request_digest,receipt_ciphertext FROM file_operations WHERE owner_subject_id=? AND operation_id=?", (owner, key)).fetchone()
                    before, after = expected.get("before"), expected.get("after")
                    if current is None or str(current["request_digest"]) != str(expected.get("digest") or "") or not isinstance(before, dict) or not isinstance(after, dict):
                        valid = False; continue
                    value = _parse_json(decrypt(str(current["receipt_ciphertext"])), None)
                    if value not in (before, after): valid = False
                    checked.append((key, value, before, after))
                if valid:
                    for key, value, before, after in checked:
                        if value == after:
                            connection.execute("UPDATE file_operations SET receipt_ciphertext=? WHERE owner_subject_id=? AND operation_id=?", (encrypt(_json(before)), owner, key))
                item["undo_phase"] = "complete" if valid else "metadata_pending"
            connection.execute("UPDATE file_operations SET receipt_ciphertext=?,lifecycle_phase=? WHERE owner_subject_id=? AND operation_id=?", (encrypt(_json(receipt)), "host_path_claim" if self._managed_items_claimed(items) else None, owner, operation))
            connection.commit(); return item["undo_phase"] == "complete"

    def settle_managed_transfer_item(self, *, owner_subject_id: str, operation_id: str,
                                     item_id: str, native_operation_id: str, claim_token: str) -> bool:
        """Atomically settle frozen provenance after the exact native commit.

        A conflicting receipt retains the known native commit and frozen plan
        for explicit reconciliation; no partial metadata update is published.
        """
        owner, operation, item = str(owner_subject_id), str(operation_id), str(item_id)
        with self._connect() as connection:
            self._begin(connection)
            parent = connection.execute(
                "SELECT receipt_ciphertext FROM file_operations WHERE owner_subject_id=? AND operation_id=?",
                (owner, operation),
            ).fetchone()
            if parent is None:
                raise FilePolicyError("transfer reservation is unavailable", code="provider_unavailable")
            receipt = _parse_json(decrypt(str(parent["receipt_ciphertext"])), None)
            items = receipt.get("_managed_items") if isinstance(receipt, dict) else None
            entry = items.get(item) if isinstance(items, dict) else None
            plan = entry.get("plan") if isinstance(entry, dict) else None
            if (not isinstance(plan, dict) or plan.get("native_operation_id") != str(native_operation_id)
                    or entry.get("claim_token") != str(claim_token)):
                raise FilePolicyError("managed transfer identity changed", code="idempotency_conflict")
            if entry.get("phase") not in {"native_committed", "metadata_pending", "complete"}:
                raise FilePolicyError("managed transfer was not claimed", code="operation_pending")
            rows = plan.get("provenance_rows")
            if not isinstance(rows, list) or len(rows) > 256:
                raise FilePolicyError("managed provenance plan is invalid", code="provider_unavailable")
            checked = []
            seen = set()
            valid = True
            for expected in rows:
                if not isinstance(expected, dict):
                    raise FilePolicyError("managed provenance plan is invalid", code="provider_unavailable")
                key = str(expected.get("operation_id") or "")
                if not key.startswith("__host_media__") or key in seen or key == operation:
                    raise FilePolicyError("managed provenance identity is invalid", code="provider_unavailable")
                seen.add(key)
                row = connection.execute(
                    "SELECT request_digest,receipt_ciphertext FROM file_operations WHERE owner_subject_id=? AND operation_id=?",
                    (owner, key),
                ).fetchone()
                if row is None or str(row["request_digest"]) != str(expected.get("digest") or ""):
                    valid = False
                    continue
                current = _parse_json(decrypt(str(row["receipt_ciphertext"])), None)
                before, after = expected.get("before"), expected.get("after")
                if not isinstance(before, dict) or not isinstance(after, dict) or current not in (before, after):
                    valid = False
                checked.append((key, current, before, after))
            if valid:
                for key, current, before, after in checked:
                    if current == before:
                        connection.execute("UPDATE file_operations SET receipt_ciphertext=? WHERE owner_subject_id=? AND operation_id=?",
                                           (encrypt(_json(after)), owner, key))
            entry["phase"] = "complete" if valid else "metadata_pending"
            still_claimed = self._managed_items_claimed(items)
            connection.execute("UPDATE file_operations SET receipt_ciphertext=?,lifecycle_phase=? WHERE owner_subject_id=? AND operation_id=?",
                               (encrypt(_json(receipt)), "host_path_claim" if still_claimed else None, owner, operation))
            connection.commit()
            return valid

    def reserve_operation(
        self,
        *,
        owner_subject_id: str,
        operation_id: str,
        request_digest: str,
        generation: int,
        receipt: Mapping[str, Any],
        phase: str | None = None,
    ) -> dict[str, Any] | None:
        """Atomically claim an operation before any provider effect.

        ``None`` means this caller owns the newly inserted pending receipt.
        An existing row is returned without replacing it, so concurrent
        requests can only have one provider dispatcher.  The receipt itself
        is the durable crash marker; a process restart therefore exposes the
        operation as pending for reconciliation instead of replaying it.
        """
        payload = encrypt(_json(dict(receipt)))
        with self._connect() as connection:
            try:
                connection.execute(
                    "INSERT INTO file_operations(owner_subject_id,operation_id,request_digest,generation,receipt_ciphertext,created_unix_ms,lifecycle_phase) VALUES (?,?,?,?,?,?,?)",
                    (str(owner_subject_id), str(operation_id), str(request_digest), int(generation), payload, _now_ms(), str(phase) if phase is not None else None),
                )
                return None
            except sqlite3.IntegrityError:
                row = connection.execute(
                    "SELECT request_digest,generation,receipt_ciphertext FROM file_operations WHERE owner_subject_id=? AND operation_id=?",
                    (str(owner_subject_id), str(operation_id)),
                ).fetchone()
        if row is None:
            raise FilePolicyError("operation reservation disappeared", code="provider_unavailable")
        if str(row["request_digest"]) != str(request_digest):
            raise FilePolicyError("operation id was already used for a different request", code="idempotency_conflict")
        try:
            existing = _parse_json(decrypt(str(row["receipt_ciphertext"])), None)
        except Exception as exc:
            raise FilePolicyError("Files operation receipt is unavailable", code="provider_unavailable") from exc
        if not isinstance(existing, Mapping):
            raise FilePolicyError("Files operation receipt is unavailable", code="provider_unavailable")
        return {"digest": str(row["request_digest"]), "generation": int(row["generation"]), "receipt": dict(existing)}

    def record_operation(
        self,
        *,
        owner_subject_id: str,
        operation_id: str,
        request_digest: str,
        generation: int,
        receipt: Mapping[str, Any],
        phase: str | None = None,
    ) -> None:
        """Update one owner-scoped Files receipt atomically after reservation."""
        payload = encrypt(_json(dict(receipt)))
        with self._connect() as connection:
            self._begin(connection)
            try:
                connection.execute(
                    "INSERT INTO file_operations(owner_subject_id,operation_id,request_digest,generation,receipt_ciphertext,created_unix_ms,lifecycle_phase) VALUES (?,?,?,?,?,?,?)",
                    (str(owner_subject_id), str(operation_id), str(request_digest), int(generation), payload, _now_ms(), str(phase) if phase is not None else None),
                )
            except sqlite3.IntegrityError:
                existing = connection.execute(
                    "SELECT request_digest,receipt_ciphertext FROM file_operations WHERE owner_subject_id=? AND operation_id=?",
                    (str(owner_subject_id), str(operation_id)),
                ).fetchone()
                if existing is None or str(existing["request_digest"]) != str(request_digest):
                    raise FilePolicyError("operation id was already used for a different request", code="idempotency_conflict")
                current = _parse_json(decrypt(str(existing["receipt_ciphertext"])), None)
                if not isinstance(current, Mapping):
                    raise FilePolicyError("Files operation receipt is unavailable", code="provider_unavailable")
                if "_host_writer_claim" in current:
                    raise FilePolicyError("Host mutation claim requires its exact release", code="operation_pending")
                incoming_receipt = dict(receipt)
                if "_managed_items" in current:
                    incoming_receipt["_managed_items"] = current["_managed_items"]
                    for key in ("_provider_names", "_request_kind", "_item_ids", "_managed_batch_ready"):
                        if key in current:
                            incoming_receipt[key] = current[key]
                else:
                    incoming_receipt.pop("_managed_items", None)
                payload = encrypt(_json(incoming_receipt))
                current_owner = str(current.get("lease_owner") or "")
                incoming_owner = str(receipt.get("lease_owner") or "")
                # Adoption receipts are fenced by the claimant that acquired
                # the durable lease.  A stale process must not publish a
                # terminal or recovery receipt after another process has
                # taken over an expired lease.
                if current_owner and incoming_owner != current_owner:
                    raise FilePolicyError("operation lease is owned by another claimant", code="operation_pending")
                if current_owner and not incoming_owner:
                    raise FilePolicyError("operation lease owner is required", code="operation_pending")
                if current_owner:
                    incoming = incoming_receipt
                    incoming["lease_owner"] = current_owner
                    duration = max(1_000, min(int(current.get("lease_duration_ms") or 30_000), 120_000))
                    incoming["lease_duration_ms"] = duration
                    incoming["lease_expires_unix_ms"] = _now_ms() + duration
                    payload = encrypt(_json(incoming))
                connection.execute(
                    "UPDATE file_operations SET generation=?,receipt_ciphertext=?,lifecycle_phase=COALESCE(?, lifecycle_phase) WHERE owner_subject_id=? AND operation_id=? AND request_digest=?",
                    (int(generation), payload, str(phase) if phase is not None else None, str(owner_subject_id), str(operation_id), str(request_digest)),
                )
            connection.commit()

    def claim_operation(
        self,
        *,
        owner_subject_id: str,
        operation_id: str,
        request_digest: str,
        generation: int,
        receipt: Mapping[str, Any],
        lease_owner: str,
        lease_ms: int = 30_000,
    ) -> dict[str, Any] | None:
        """Atomically claim or renew a non-terminal Files operation lease.

        ``None`` means this caller owns publication.  A live lease returns the
        stored receipt; an expired lease is replaced under BEGIN IMMEDIATE.
        Terminal receipts are returned unchanged for replay.
        """
        now = _now_ms()
        bounded_lease = max(1_000, min(int(lease_ms), 120_000))
        owner = str(lease_owner or "").strip()
        if not owner:
            raise FilePolicyError("operation lease owner is required", code="invalid_operation")
        proposed = dict(receipt)
        proposed["lease_owner"] = owner
        proposed["lease_duration_ms"] = bounded_lease
        proposed["lease_expires_unix_ms"] = now + bounded_lease
        payload = encrypt(_json(proposed))
        with self._connect() as connection:
            self._begin(connection)
            row = connection.execute(
                "SELECT request_digest,generation,receipt_ciphertext FROM file_operations WHERE owner_subject_id=? AND operation_id=?",
                (str(owner_subject_id), str(operation_id)),
            ).fetchone()
            if row is None:
                connection.execute(
                    "INSERT INTO file_operations(owner_subject_id,operation_id,request_digest,generation,receipt_ciphertext,created_unix_ms,lifecycle_phase) VALUES (?,?,?,?,?,?,?)",
                    (str(owner_subject_id), str(operation_id), str(request_digest), int(generation), payload, now, str(proposed.get("phase") or "staged")),
                )
                connection.commit()
                return None
            if str(row["request_digest"]) != str(request_digest):
                connection.rollback()
                raise FilePolicyError("operation id was already used for a different request", code="idempotency_conflict")
            existing = _parse_json(decrypt(str(row["receipt_ciphertext"])), None)
            if not isinstance(existing, Mapping):
                connection.rollback()
                raise FilePolicyError("Files operation receipt is unavailable", code="provider_unavailable")
            phase = str(existing.get("phase") or "")
            if phase in {"complete", "recovery_required"}:
                connection.rollback()
                return {"digest": str(row["request_digest"]), "generation": int(row["generation"]), "receipt": dict(existing)}
            expires = int(existing.get("lease_expires_unix_ms") or 0)
            if expires > now and str(existing.get("lease_owner") or "") != owner:
                connection.rollback()
                return {"digest": str(row["request_digest"]), "generation": int(row["generation"]), "receipt": dict(existing)}
            connection.execute(
                "UPDATE file_operations SET generation=?,receipt_ciphertext=?,lifecycle_phase=? WHERE owner_subject_id=? AND operation_id=? AND request_digest=?",
                (int(generation), payload, str(proposed.get("phase") or "staged"), str(owner_subject_id), str(operation_id), str(request_digest)),
            )
            connection.commit()
            return None
    @staticmethod
    def _bump(connection: sqlite3.Connection) -> int:
        connection.execute(
            "UPDATE file_policy_meta SET generation=generation+1 WHERE singleton=1"
        )
        return FilePolicyRepository._generation(connection)

    @staticmethod
    def _audit(
        connection: sqlite3.Connection,
        *,
        generation: int,
        event: str,
        actor_subject_id: str,
        target_type: str,
        target_id: str,
        reason_code: str,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        # Details deliberately contain opaque ids/capabilities only. Paths and
        # outside-scope existence never belong in policy audit payloads.
        connection.execute(
            """
            INSERT INTO file_policy_audit(
                generation,event,actor_subject_id,target_type,target_id,
                reason_code,details_json,created_unix_ms
            ) VALUES (?,?,?,?,?,?,?,?)
            """,
            (
                generation,
                event,
                actor_subject_id,
                target_type,
                target_id,
                reason_code,
                _json(dict(details or {})),
                _now_ms(),
            ),
        )

    def ensure_self_location_access(self, subject_id: str, location: Location, capabilities: Iterable[str],
                                    *, binding_class: str) -> str:
        """Keep a current compatible personal grant, or create a fresh one."""
        caps = set(_capabilities(capabilities))
        if binding_class not in {"people", "agent"} or not subject_id:
            raise FilePolicyError("Invalid personal location access", code="invalid_scope")
        with self._connect() as connection:
            self._begin(connection)
            row = connection.execute("SELECT * FROM file_policy_locations WHERE id=?", (location.id,)).fetchone()
            if (not row or not row["enabled"] or row["availability"] != "available"
                    or int(row["revision"]) != location.revision
                    or not caps.issubset(set(_parse_json(row["capabilities_json"], [])))):
                raise FilePolicyError("Current location cannot grant the chosen capabilities", code="capability_escalation")
            active = connection.execute(
                "SELECT id,capabilities_json FROM file_policy_bindings "
                "WHERE binding_class=? AND subject_kind='user' AND subject_id=? AND location_id=? "
                "AND lifetime='always' AND status='active' AND expires_unix_ms IS NULL ORDER BY id",
                (binding_class, subject_id, location.id),
            ).fetchall()
            compatible = next((part["id"] for part in active
                               if caps.issubset(set(_parse_json(part["capabilities_json"], [])))), None)
            if compatible:
                connection.rollback()
                return str(compatible)
            # Extend this explicit personal grant without reviving revoked rows.
            generation = self._bump(connection)
            now = _now_ms()
            if active:
                base = active[0]
                identifier = str(base["id"])
                combined_caps = caps | set(_parse_json(base["capabilities_json"], []))
                connection.execute(
                    "UPDATE file_policy_bindings SET capabilities_json=?,generation=?,revision=revision+1,updated_unix_ms=? WHERE id=?",
                    (_json(sorted(combined_caps)), generation, now, identifier),
                )
                event = "update"
            else:
                identifier = f"binding-{uuid.uuid4().hex}"
                connection.execute(
                    "INSERT INTO file_policy_bindings(id,binding_class,subject_kind,subject_id,location_id,"
                    "capabilities_json,lifetime,status,created_by_subject_id,generation,revision,created_unix_ms,updated_unix_ms) "
                    "VALUES (?,?,'user',?,?,?,'always','active',?,?,1,?,?)",
                    (identifier, binding_class, subject_id, location.id, _json(sorted(caps)), subject_id, generation, now, now),
                )
                event = "create"
            self._audit(connection, generation=generation, event=event, actor_subject_id=subject_id,
                        target_type="binding", target_id=identifier, reason_code="personal_location_access",
                        details={"location_id": location.id, "binding_class": binding_class})
            connection.commit()
            return identifier

    def full_filesystem_access(self, subject_id: str) -> dict[str, bool]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT human,agent FROM file_policy_full_filesystem_access WHERE subject_id=?", (subject_id,),
            ).fetchone()
        return {scope: bool(row and row[scope] == 1) for scope in ("human", "agent")}

    def set_full_filesystem_access(self, subject_id: str, scope: str, enabled: bool):
        if not subject_id or scope not in {"human", "agent"} or type(enabled) is not bool:
            raise FilePolicyError("Invalid full filesystem access choice", code="invalid_scope")
        with self._connect() as connection:
            self._begin(connection)
            row = connection.execute(
                "SELECT human,agent FROM file_policy_full_filesystem_access WHERE subject_id=?", (subject_id,),
            ).fetchone()
            choices = {name: bool(row and row[name] == 1) for name in ("human", "agent")}
            changed = choices[scope] != enabled
            generation = self._generation(connection)
            if changed:
                choices[scope] = enabled
                connection.execute(
                    "INSERT INTO file_policy_full_filesystem_access(subject_id,human,agent) VALUES (?,?,?) "
                    "ON CONFLICT(subject_id) DO UPDATE SET human=excluded.human,agent=excluded.agent",
                    (subject_id, int(choices["human"]), int(choices["agent"])),
                )
                generation = self._bump(connection)
                self._audit(connection, generation=generation, event="full_filesystem_access_changed",
                            actor_subject_id=subject_id, target_type="subject", target_id=subject_id,
                            reason_code=scope, details={"enabled": enabled})
            connection.commit()
        return choices, generation, changed

    def retire_full_filesystem_access(self, subject_id: str) -> bool:
        with self._connect() as connection:
            self._begin(connection)
            changed = connection.execute(
                "DELETE FROM file_policy_full_filesystem_access WHERE subject_id=?", (subject_id,),
            ).rowcount > 0
            if changed:
                generation = self._bump(connection)
                self._audit(connection, generation=generation, event="full_filesystem_access_retired",
                            actor_subject_id=subject_id, target_type="subject", target_id=subject_id,
                            reason_code="eligibility_lost")
            connection.commit()
        return changed

    def invalidate_account_access(self, subject_id: str) -> None:
        with self._connect() as connection:
            self._begin(connection)
            generation = self._bump(connection)
            self._audit(
                connection, generation=generation, event="account_access_changed",
                actor_subject_id=subject_id, target_type="subject", target_id=subject_id,
                reason_code="owner_access_mode",
            )
            connection.commit()

    def generation(self) -> int:
        self.expire_bindings()
        with self._connect() as connection:
            return self._generation(connection)

    @staticmethod
    def _place(row: sqlite3.Row) -> FilePlace:
        origin = decrypt(str(row["origin_ciphertext"] or ""))
        if not origin:
            raise FilePolicyError("file place could not be opened", code="policy_store_unavailable")
        return FilePlace(
            id=str(row["id"]),
            owner_subject_id=str(row["owner_subject_id"]),
            provider=str(row["provider"]),
            stable_resource_id=str(row["stable_resource_id"]),
            origin_id=origin,
            kind=str(row["kind"]),
            display_name=str(row["display_name"]),
            created_unix_ms=int(row["created_unix_ms"]),
            updated_unix_ms=int(row["updated_unix_ms"]),
        )

    def list_places(self, owner_subject_id: str, *, limit: int = 24) -> list[FilePlace]:
        owner = str(owner_subject_id or "").strip()
        if not owner:
            raise FilePolicyError("place owner is required", code="invalid_subject")
        bounded = max(1, min(int(limit), 100))
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM file_places
                WHERE owner_subject_id=?
                ORDER BY updated_unix_ms DESC, id ASC
                LIMIT ?
                """,
                (owner, bounded),
            ).fetchall()
        places: list[FilePlace] = []
        for row in rows:
            try:
                places.append(self._place(row))
            except FilePolicyError:
                # One unreadable bookmark cannot disclose or erase its peers.
                # It remains stored for an administrator's recovery tooling.
                continue
        return places

    def save_place(
        self,
        *,
        owner_subject_id: str,
        provider: str,
        stable_resource_id: str,
        origin_id: str,
        kind: str,
        display_name: str,
    ) -> FilePlace:
        owner = str(owner_subject_id or "").strip()
        provider_name = str(provider or "").strip().lower()
        stable_id = str(stable_resource_id or "").strip()
        origin = str(origin_id or "").strip()
        resource_kind = str(kind or "").strip().lower()
        name = str(display_name or "").strip()[:512]
        if not owner or not provider_name or not stable_id or not origin or not resource_kind or not name:
            raise FilePolicyError("file place is incomplete", code="invalid_place")
        ciphertext = encrypt(origin)
        if not ciphertext.startswith("enc:"):
            raise FilePolicyError("file place could not be sealed", code="policy_store_unavailable")
        now = _now_ms()
        with self._connect() as connection:
            self._begin(connection)
            existing = connection.execute(
                """
                SELECT id,created_unix_ms FROM file_places
                WHERE owner_subject_id=? AND provider=? AND stable_resource_id=?
                """,
                (owner, provider_name, stable_id),
            ).fetchone()
            place_id = str(existing["id"]) if existing else f"place-{uuid.uuid4().hex}"
            created = int(existing["created_unix_ms"]) if existing else now
            connection.execute(
                """
                INSERT INTO file_places(
                    id,owner_subject_id,provider,stable_resource_id,
                    origin_ciphertext,kind,display_name,created_unix_ms,updated_unix_ms
                ) VALUES (?,?,?,?,?,?,?,?,?)
                ON CONFLICT(owner_subject_id,provider,stable_resource_id) DO UPDATE SET
                    origin_ciphertext=excluded.origin_ciphertext,
                    kind=excluded.kind,
                    display_name=excluded.display_name,
                    updated_unix_ms=excluded.updated_unix_ms
                """,
                (place_id, owner, provider_name, stable_id, ciphertext, resource_kind, name, created, now),
            )
            row = connection.execute("SELECT * FROM file_places WHERE id=?", (place_id,)).fetchone()
            connection.commit()
        return self._place(row)

    def remove_place(self, *, owner_subject_id: str, place_id: str) -> bool:
        owner = str(owner_subject_id or "").strip()
        identifier = str(place_id or "").strip()
        if not owner or not identifier:
            return False
        with self._connect() as connection:
            self._begin(connection)
            changed = connection.execute(
                "DELETE FROM file_places WHERE id=? AND owner_subject_id=?",
                (identifier, owner),
            ).rowcount
            connection.commit()
        return bool(changed)

    @staticmethod
    def _recent(row: sqlite3.Row) -> FileRecent:
        origin = decrypt(str(row["origin_ciphertext"] or ""))
        if not origin:
            raise FilePolicyError("recent resource could not be opened", code="policy_store_unavailable")
        return FileRecent(
            id=str(row["id"]),
            owner_subject_id=str(row["owner_subject_id"]),
            provider=str(row["provider"]),
            stable_resource_id=str(row["stable_resource_id"]),
            origin_id=origin,
            kind=str(row["kind"]),
            display_name=str(row["display_name"]),
            accessed_unix_ms=int(row["accessed_unix_ms"]),
        )

    def list_recents(self, owner_subject_id: str, *, limit: int = 24) -> list[FileRecent]:
        owner = str(owner_subject_id or "").strip()
        if not owner:
            raise FilePolicyError("recent owner is required", code="invalid_subject")
        bounded = max(1, min(int(limit), 100))
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM file_recents
                WHERE owner_subject_id=?
                ORDER BY accessed_unix_ms DESC,id ASC
                LIMIT ?
                """,
                (owner, bounded),
            ).fetchall()
        result: list[FileRecent] = []
        for row in rows:
            try:
                result.append(self._recent(row))
            except FilePolicyError:
                continue
        return result

    def touch_recent(
        self,
        *,
        owner_subject_id: str,
        provider: str,
        stable_resource_id: str,
        origin_id: str,
        kind: str,
        display_name: str,
        max_entries: int = 100,
    ) -> FileRecent:
        owner = str(owner_subject_id or "").strip()
        provider_name = str(provider or "").strip().lower()
        stable_id = str(stable_resource_id or "").strip()
        origin = str(origin_id or "").strip()
        resource_kind = str(kind or "").strip().lower()
        name = str(display_name or "").strip()[:512]
        if not owner or not provider_name or not stable_id or not origin or not resource_kind or not name:
            raise FilePolicyError("recent resource is incomplete", code="invalid_recent")
        ciphertext = encrypt(origin)
        if not ciphertext.startswith("enc:"):
            raise FilePolicyError("recent resource could not be sealed", code="policy_store_unavailable")
        now = _now_ms()
        bounded = max(1, min(int(max_entries), 500))
        with self._connect() as connection:
            self._begin(connection)
            existing = connection.execute(
                """
                SELECT id FROM file_recents
                WHERE owner_subject_id=? AND provider=? AND stable_resource_id=?
                """,
                (owner, provider_name, stable_id),
            ).fetchone()
            recent_id = str(existing["id"]) if existing else f"recent-{uuid.uuid4().hex}"
            connection.execute(
                """
                INSERT INTO file_recents(
                    id,owner_subject_id,provider,stable_resource_id,
                    origin_ciphertext,kind,display_name,accessed_unix_ms
                ) VALUES (?,?,?,?,?,?,?,?)
                ON CONFLICT(owner_subject_id,provider,stable_resource_id) DO UPDATE SET
                    origin_ciphertext=excluded.origin_ciphertext,
                    kind=excluded.kind,
                    display_name=excluded.display_name,
                    accessed_unix_ms=excluded.accessed_unix_ms
                """,
                (recent_id, owner, provider_name, stable_id, ciphertext, resource_kind, name, now),
            )
            connection.execute(
                """
                DELETE FROM file_recents
                WHERE owner_subject_id=? AND id NOT IN (
                    SELECT id FROM file_recents
                    WHERE owner_subject_id=?
                    ORDER BY accessed_unix_ms DESC,id ASC
                    LIMIT ?
                )
                """,
                (owner, owner, bounded),
            )
            row = connection.execute("SELECT * FROM file_recents WHERE id=?", (recent_id,)).fetchone()
            connection.commit()
        return self._recent(row)

    def clear_recents(self, *, owner_subject_id: str) -> int:
        owner = str(owner_subject_id or "").strip()
        if not owner:
            return 0
        with self._connect() as connection:
            self._begin(connection)
            changed = int(connection.execute(
                "DELETE FROM file_recents WHERE owner_subject_id=?",
                (owner,),
            ).rowcount or 0)
            connection.commit()
        return changed

    @staticmethod
    def _saved_search(row: sqlite3.Row) -> FileSavedSearch:
        sort = _parse_json(str(row["sort_json"] or ""), {})
        if not isinstance(sort, dict):
            raise FilePolicyError("saved search could not be opened", code="policy_store_unavailable")
        return FileSavedSearch(
            id=str(row["id"]),
            owner_subject_id=str(row["owner_subject_id"]),
            name=str(row["name"]),
            provider_scope=str(row["provider_scope"]),
            query=str(row["query"]),
            sort=sort,
            created_unix_ms=int(row["created_unix_ms"]),
            updated_unix_ms=int(row["updated_unix_ms"]),
        )

    def list_saved_searches(self, owner_subject_id: str, *, limit: int = 50) -> list[FileSavedSearch]:
        owner = str(owner_subject_id or "").strip()
        if not owner:
            raise FilePolicyError("saved-search owner is required", code="invalid_subject")
        bounded = max(1, min(int(limit), 100))
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM file_saved_searches
                WHERE owner_subject_id=?
                ORDER BY updated_unix_ms DESC,id ASC
                LIMIT ?
                """,
                (owner, bounded),
            ).fetchall()
        result: list[FileSavedSearch] = []
        for row in rows:
            try:
                result.append(self._saved_search(row))
            except FilePolicyError:
                continue
        return result

    def save_search(
        self,
        *,
        owner_subject_id: str,
        name: str,
        provider_scope: str,
        query: str,
        sort: Mapping[str, Any],
    ) -> FileSavedSearch:
        owner = str(owner_subject_id or "").strip()
        label = str(name or "").strip()[:200]
        provider = str(provider_scope or "").strip().lower()
        needle = str(query or "").strip()[:512]
        if not owner or not label or provider not in {"all", "host", "copal", "files", "library"} or not needle:
            raise FilePolicyError("saved search is incomplete", code="invalid_saved_search")
        normalized_sort = dict(sort or {})
        now = _now_ms()
        identifier = f"search-{uuid.uuid4().hex}"
        with self._connect() as connection:
            self._begin(connection)
            connection.execute(
                """
                INSERT INTO file_saved_searches(
                    id,owner_subject_id,name,provider_scope,query,sort_json,
                    created_unix_ms,updated_unix_ms
                ) VALUES (?,?,?,?,?,?,?,?)
                """,
                (identifier, owner, label, provider, needle, _json(normalized_sort), now, now),
            )
            row = connection.execute(
                "SELECT * FROM file_saved_searches WHERE id=?", (identifier,)
            ).fetchone()
            connection.commit()
        return self._saved_search(row)

    def remove_saved_search(self, *, owner_subject_id: str, search_id: str) -> bool:
        owner = str(owner_subject_id or "").strip()
        identifier = str(search_id or "").strip()
        if not owner or not identifier:
            return False
        with self._connect() as connection:
            self._begin(connection)
            changed = connection.execute(
                "DELETE FROM file_saved_searches WHERE id=? AND owner_subject_id=?",
                (identifier, owner),
            ).rowcount
            connection.commit()
        return bool(changed)

    @staticmethod
    def _location(row: sqlite3.Row) -> Location:
        return Location(
            id=row["id"],
            kind=row["kind"],
            canonical_path=row["canonical_path"],
            display_path=row["display_path"],
            capabilities=tuple(_parse_json(row["capabilities_json"], [])),
            platform_identity=_parse_json(row["platform_identity_json"], {}),
            availability=row["availability"],
            enabled=bool(row["enabled"]),
            generation=int(row["generation"]),
            revision=int(row["revision"]),
            created_by_subject_id=row["created_by_subject_id"],
            migration_source=row["migration_source"],
            migration_key=row["migration_key"],
        )

    @staticmethod
    def _workspace(row: sqlite3.Row) -> Workspace:
        return Workspace(
            id=row["id"],
            owner_subject_id=row["owner_subject_id"],
            name=row["name"],
            location_id=row["location_id"],
            relative_folder=row["relative_folder"],
            archived=bool(row["archived"]),
            generation=int(row["generation"]),
            revision=int(row["revision"]),
        )

    @staticmethod
    def _binding(row: sqlite3.Row) -> PolicyBinding:
        return PolicyBinding(
            id=row["id"],
            binding_class=row["binding_class"],
            subject_kind=row["subject_kind"],
            subject_id=row["subject_id"],
            location_id=row["location_id"],
            workspace_id=row["workspace_id"],
            chat_id=row["chat_id"],
            resource_ref=row["resource_ref"],
            operation=row["operation"],
            capabilities=tuple(_parse_json(row["capabilities_json"], [])),
            lifetime=row["lifetime"],
            status=row["status"],
            remaining_uses=row["remaining_uses"],
            expires_unix_ms=row["expires_unix_ms"],
            created_by_subject_id=row["created_by_subject_id"],
            generation=int(row["generation"]),
            revision=int(row["revision"]),
            migration_source=row["migration_source"],
            migration_key=row["migration_key"],
        )

    def create_location(
        self,
        *,
        actor_subject_id: str,
        path: str,
        kind: str,
        capabilities: Iterable[str],
        display_path: str | None = None,
        platform_identity: Mapping[str, Any] | None = None,
        availability: str = "available",
        location_id: str | None = None,
        migration_source: str | None = None,
        migration_key: str | None = None,
    ) -> Location:
        actor = str(actor_subject_id or "").strip()
        if not actor:
            raise FilePolicyError("actor subject is required", code="subject_required")
        canonical, canonical_key = _canonical_location(path, kind)
        caps = _capabilities(capabilities)
        identity = dict(platform_identity or {})
        identifier = str(location_id or f"location-{uuid.uuid4().hex}")
        now = _now_ms()
        with self._connect() as connection:
            self._begin(connection)
            existing = connection.execute(
                "SELECT * FROM file_policy_locations WHERE kind=? AND canonical_key=?",
                (kind, canonical_key),
            ).fetchone()
            if existing:
                connection.rollback()
                return self._location(existing)
            generation = self._bump(connection)
            try:
                connection.execute(
                    """
                    INSERT INTO file_policy_locations(
                        id,kind,canonical_path,canonical_key,display_path,
                        capabilities_json,platform_identity_json,availability,
                        enabled,generation,revision,created_by_subject_id,
                        created_unix_ms,updated_unix_ms,migration_source,migration_key
                    ) VALUES (?,?,?,?,?,?,?,?,1,?,1,?,?,?,?,?)
                    """,
                    (
                        identifier,
                        kind,
                        canonical,
                        canonical_key,
                        str(display_path or path),
                        _json(caps),
                        _json(identity),
                        str(availability),
                        generation,
                        actor,
                        now,
                        now,
                        migration_source,
                        migration_key,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                connection.rollback()
                raise FilePolicyError("location identity already exists", code="duplicate_location") from exc
            self._audit(
                connection,
                generation=generation,
                event="create",
                actor_subject_id=actor,
                target_type="location",
                target_id=identifier,
                reason_code="location_created",
                details={"kind": kind, "capabilities": list(caps)},
            )
            connection.commit()
            return self.get_location(identifier)

    def get_location(self, location_id: str) -> Location:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM file_policy_locations WHERE id=?", (str(location_id),)
            ).fetchone()
        if not row:
            raise FilePolicyError("location was not found", code="location_not_found")
        return self._location(row)

    def list_locations(self, *, include_disabled: bool = False) -> list[Location]:
        query = "SELECT * FROM file_policy_locations"
        if not include_disabled:
            query += " WHERE enabled=1"
        query += " ORDER BY display_path COLLATE NOCASE,id"
        with self._connect() as connection:
            return [self._location(row) for row in connection.execute(query)]

    def disable_location(
        self,
        location_id: str,
        *,
        actor_subject_id: str,
        reason_code: str = "location_removed",
    ) -> dict[str, Any]:
        """Soft-remove one physical Location and every authority below it.

        A Location is durable physical identity, so removal never deletes its
        audit row.  Active People/Agent/Operation bindings are revoked and
        Workspaces are archived in the same generation.  Re-adding the same
        path may revive the Location identity, but never these old grants.
        """
        identifier = str(location_id or "").strip()
        actor = str(actor_subject_id or "").strip()
        if not identifier or not actor:
            raise FilePolicyError("location and actor are required", code="location_required")
        with self._connect() as connection:
            self._begin(connection)
            row = connection.execute(
                "SELECT * FROM file_policy_locations WHERE id=?", (identifier,)
            ).fetchone()
            if not row:
                connection.rollback()
                raise FilePolicyError("location was not found", code="location_not_found")
            if not bool(row["enabled"]):
                connection.rollback()
                return {
                    "location": self._location(row),
                    "bindings_revoked": 0,
                    "workspaces_archived": 0,
                    "generation": self.generation(),
                }

            active_bindings = connection.execute(
                "SELECT COUNT(*) FROM file_policy_bindings WHERE location_id=? AND status='active'",
                (identifier,),
            ).fetchone()[0]
            active_workspaces = connection.execute(
                "SELECT COUNT(*) FROM file_policy_workspaces WHERE location_id=? AND archived=0",
                (identifier,),
            ).fetchone()[0]
            generation = self._bump(connection)
            now = _now_ms()
            connection.execute(
                """
                UPDATE file_policy_locations
                SET enabled=0,generation=?,revision=revision+1,updated_unix_ms=?
                WHERE id=?
                """,
                (generation, now, identifier),
            )
            connection.execute(
                """
                UPDATE file_policy_bindings
                SET status='revoked',generation=?,revision=revision+1,updated_unix_ms=?
                WHERE location_id=? AND status='active'
                """,
                (generation, now, identifier),
            )
            connection.execute(
                """
                UPDATE file_policy_workspaces
                SET archived=1,generation=?,revision=revision+1,updated_unix_ms=?
                WHERE location_id=? AND archived=0
                """,
                (generation, now, identifier),
            )
            self._audit(
                connection,
                generation=generation,
                event="remove",
                actor_subject_id=actor,
                target_type="location",
                target_id=identifier,
                reason_code=str(reason_code or "location_removed"),
                details={
                    "bindings_revoked": int(active_bindings or 0),
                    "workspaces_archived": int(active_workspaces or 0),
                },
            )
            connection.commit()
        return {
            "location": self.get_location(identifier),
            "bindings_revoked": int(active_bindings or 0),
            "workspaces_archived": int(active_workspaces or 0),
            "generation": generation,
        }

    def restore_location(
        self,
        location_id: str,
        *,
        actor_subject_id: str,
        capabilities: Iterable[str],
        display_path: str,
        platform_identity: Mapping[str, Any],
        availability: str = "available",
    ) -> Location:
        """Revive physical identity only; revoked grants stay revoked."""
        identifier = str(location_id or "").strip()
        actor = str(actor_subject_id or "").strip()
        caps = _capabilities(capabilities)
        if not identifier or not actor:
            raise FilePolicyError("location and actor are required", code="location_required")
        with self._connect() as connection:
            self._begin(connection)
            row = connection.execute(
                "SELECT * FROM file_policy_locations WHERE id=?", (identifier,)
            ).fetchone()
            if not row:
                connection.rollback()
                raise FilePolicyError("location was not found", code="location_not_found")
            if bool(row["enabled"]):
                connection.rollback()
                return self._location(row)
            generation = self._bump(connection)
            connection.execute(
                """
                UPDATE file_policy_locations
                SET enabled=1,capabilities_json=?,display_path=?,platform_identity_json=?,
                    availability=?,generation=?,revision=revision+1,updated_unix_ms=?
                WHERE id=?
                """,
                (
                    _json(caps),
                    str(display_path or row["display_path"]),
                    _json(dict(platform_identity or {})),
                    str(availability or "available"),
                    generation,
                    _now_ms(),
                    identifier,
                ),
            )
            self._audit(
                connection,
                generation=generation,
                event="restore",
                actor_subject_id=actor,
                target_type="location",
                target_id=identifier,
                reason_code="location_restored",
                details={"capabilities": list(caps), "bindings_restored": 0, "workspaces_restored": 0},
            )
            connection.commit()
        return self.get_location(identifier)

    def create_workspace(
        self,
        *,
        actor_subject_id: str,
        owner_subject_id: str,
        location_id: str,
        name: str,
        relative_folder: str = "",
        workspace_id: str | None = None,
        migration_source: str | None = None,
        migration_key: str | None = None,
    ) -> Workspace:
        actor = str(actor_subject_id or "").strip()
        owner = str(owner_subject_id or "").strip()
        title = str(name or "").strip()
        if not actor or not owner or not title:
            raise FilePolicyError("actor, owner, and workspace name are required", code="workspace_required")
        folder = _relative_folder(relative_folder)
        identifier = str(workspace_id or f"workspace-{uuid.uuid4().hex}")
        now = _now_ms()
        with self._connect() as connection:
            self._begin(connection)
            if not connection.execute(
                "SELECT 1 FROM file_policy_locations WHERE id=? AND enabled=1", (str(location_id),)
            ).fetchone():
                connection.rollback()
                raise FilePolicyError("workspace location was not found", code="location_not_found")
            existing = connection.execute(
                """SELECT * FROM file_policy_workspaces
                   WHERE owner_subject_id=? AND location_id=? AND relative_folder=?""",
                (owner, str(location_id), folder),
            ).fetchone()
            if existing:
                connection.rollback()
                return self._workspace(existing)
            generation = self._bump(connection)
            try:
                connection.execute(
                    """
                    INSERT INTO file_policy_workspaces(
                        id,owner_subject_id,name,location_id,relative_folder,
                        archived,generation,revision,created_unix_ms,updated_unix_ms,
                        migration_source,migration_key
                    ) VALUES (?,?,?,?,?,0,?,1,?,?,?,?)
                    """,
                    (
                        identifier,
                        owner,
                        title,
                        str(location_id),
                        folder,
                        generation,
                        now,
                        now,
                        migration_source,
                        migration_key,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                connection.rollback()
                raise FilePolicyError("workspace identity already exists", code="duplicate_workspace") from exc
            self._audit(
                connection,
                generation=generation,
                event="create",
                actor_subject_id=actor,
                target_type="workspace",
                target_id=identifier,
                reason_code="workspace_created",
                details={"location_id": str(location_id)},
            )
            connection.commit()
            return self.get_workspace(identifier)

    def get_workspace(self, workspace_id: str) -> Workspace:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM file_policy_workspaces WHERE id=?", (str(workspace_id),)
            ).fetchone()
        if not row:
            raise FilePolicyError("workspace was not found", code="workspace_not_found")
        return self._workspace(row)

    def list_workspaces(
        self,
        *,
        owner_subject_id: str | None = None,
        include_archived: bool = False,
    ) -> list[Workspace]:
        clauses: list[str] = []
        params: list[Any] = []
        if owner_subject_id is not None:
            clauses.append("owner_subject_id=?")
            params.append(str(owner_subject_id))
        if not include_archived:
            clauses.append("archived=0")
        query = "SELECT * FROM file_policy_workspaces"
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY name COLLATE NOCASE,id"
        with self._connect() as connection:
            return [self._workspace(row) for row in connection.execute(query, params)]

    def update_workspace(
        self,
        workspace_id: str,
        *,
        actor_subject_id: str,
        name: str | None = None,
        archived: bool | None = None,
        expected_revision: int | None = None,
    ) -> Workspace:
        """Rename/archive a Workspace without restoring or widening access."""

        identifier = str(workspace_id or "").strip()
        actor = str(actor_subject_id or "").strip()
        if not identifier or not actor:
            raise FilePolicyError("workspace and actor are required", code="workspace_required")
        if name is None and archived is None:
            raise FilePolicyError("workspace update is empty", code="empty_update")
        title = None if name is None else str(name).strip()
        if title is not None and (not title or len(title) > 200):
            raise FilePolicyError("workspace name is invalid", code="invalid_workspace_name")
        with self._connect() as connection:
            self._begin(connection)
            row = connection.execute(
                "SELECT * FROM file_policy_workspaces WHERE id=?", (identifier,)
            ).fetchone()
            if not row:
                connection.rollback()
                raise FilePolicyError("workspace was not found", code="workspace_not_found")
            if expected_revision is not None and int(row["revision"]) != int(expected_revision):
                connection.rollback()
                raise FilePolicyError("workspace changed", code="revision_conflict")
            desired_name = title if title is not None else str(row["name"])
            desired_archived = int(bool(archived)) if archived is not None else int(row["archived"])
            if desired_name == row["name"] and desired_archived == int(row["archived"]):
                connection.rollback()
                return self._workspace(row)
            if desired_archived == 0:
                location = connection.execute(
                    "SELECT enabled,availability FROM file_policy_locations WHERE id=?",
                    (str(row["location_id"]),),
                ).fetchone()
                if not location or not location["enabled"] or location["availability"] != "available":
                    connection.rollback()
                    raise FilePolicyError("workspace location is unavailable", code="location_not_found")
            generation = self._bump(connection)
            now = _now_ms()
            connection.execute(
                """
                UPDATE file_policy_workspaces
                SET name=?,archived=?,generation=?,revision=revision+1,updated_unix_ms=?
                WHERE id=?
                """,
                (desired_name, desired_archived, generation, now, identifier),
            )
            revoked = 0
            if desired_archived and not int(row["archived"]):
                revoked = int(connection.execute(
                    """
                    SELECT COUNT(*) FROM file_policy_bindings
                    WHERE workspace_id=? AND status='active'
                    """,
                    (identifier,),
                ).fetchone()[0])
                connection.execute(
                    """
                    UPDATE file_policy_bindings
                    SET status='revoked',generation=?,revision=revision+1,updated_unix_ms=?
                    WHERE workspace_id=? AND status='active'
                    """,
                    (generation, now, identifier),
                )
            self._audit(
                connection,
                generation=generation,
                event="archive" if desired_archived and not int(row["archived"])
                    else "restore" if not desired_archived and int(row["archived"])
                    else "update",
                actor_subject_id=actor,
                target_type="workspace",
                target_id=identifier,
                reason_code="workspace_archived" if desired_archived and not int(row["archived"])
                    else "workspace_restored" if not desired_archived and int(row["archived"])
                    else "workspace_renamed",
                details={"name": desired_name, "bindings_revoked": revoked},
            )
            connection.commit()
        return self.get_workspace(identifier)

    @staticmethod
    def _git_once_profile(profile: str) -> tuple[str, str, str, str, str, str]:
        # Closed internal profiles: public entry points select one fixed domain.
        profiles = {
            'index': ('git-once-', 'git-index-action:sha256:', 'git_index_pending', 'git_index_update', 'git_index_once_created', 'git_index_once_consumed'),
            'commit': ('git-commit-once-', 'git-commit-action:sha256:', 'git_commit_pending', 'git_commit', 'git_commit_once_created', 'git_commit_once_consumed'),
            'cleanup': ('git-cleanup-once-', 'git-cleanup-action:sha256:', 'git_cleanup_pending', 'git_worktree_cleanup', 'git_cleanup_once_created', 'git_cleanup_once_consumed'),
            'branch_create': ('git-branch-create-once-', 'git-branch-create-action:sha256:', 'git_branch_create_pending', 'git_branch_create', 'git_branch_create_once_created', 'git_branch_create_once_consumed'),
            'fetch': ('git-fetch-once-', 'git-fetch-action:sha256:', 'git_fetch_pending', 'git_fetch', 'git_fetch_once_created', 'git_fetch_once_consumed'),
            'push': ('git-push-once-', 'git-push-action:sha256:', 'git_push_pending', 'git_push', 'git_push_once_created', 'git_push_once_consumed'),
            'checkout': ('git-checkout-once-', 'git-checkout-action:sha256:', 'git_checkout_pending', 'git_checkout', 'git_checkout_once_created', 'git_checkout_once_consumed'),
            'worktree_stage': ('git-worktree-stage-once-', 'git-worktree-stage-action:sha256:', 'git_worktree_stage_pending', 'git_worktree_stage', 'git_worktree_stage_once_created', 'git_worktree_stage_once_consumed'),
            'worktree_inverse': ('git-worktree-inverse-once-', 'git-worktree-inverse-action:sha256:', 'git_worktree_inverse_pending', 'git_worktree_inverse', 'git_worktree_inverse_once_created', 'git_worktree_inverse_once_consumed'),
            'worktree_redo': ('git-worktree-redo-once-', 'git-worktree-redo-action:sha256:', 'git_worktree_redo_pending', 'git_worktree_redo', 'git_worktree_redo_once_created', 'git_worktree_redo_once_consumed'),
        }
        if profile not in profiles:
            raise ValueError('unsupported Git approval profile')
        return profiles[profile]

    def _create_git_once(self, profile: str, *, subject_id: str, location_id: str, workspace_id: str | None, chat_id: str,
                              operation_id: str, request_digest: str, expected_generation: int, expires_unix_ms: int) -> dict[str, Any]:
        """Create one exact Git action approval against its already-reserved request."""
        identifier_prefix, resource_prefix, phase, operation, created_reason, _ = self._git_once_profile(profile)
        values=(subject_id,location_id,chat_id,operation_id)
        if (any(type(v) is not str or not v or v!=v.strip() or len(v.encode('utf-8'))>512 for v in values)
            or (workspace_id is not None and (type(workspace_id) is not str or not workspace_id or workspace_id!=workspace_id.strip() or len(workspace_id.encode('utf-8'))>512))
            or type(request_digest) is not str or len(request_digest)!=64 or any(c not in '0123456789abcdef' for c in request_digest)
            or type(expected_generation) is not int or expected_generation<0 or type(expires_unix_ms) is not int
            or not _now_ms()<expires_unix_ms<=_now_ms()+300000):
            raise FilePolicyError('Git approval unavailable',code='invalid_request')
        identifier=identifier_prefix+hashlib.sha256(_json([subject_id,operation_id]).encode()).hexdigest()
        resource=resource_prefix+request_digest; now=_now_ms()
        with self._connect() as connection:
            self._begin(connection)
            if self._generation(connection)!=expected_generation:raise FilePolicyError('File policy generation changed',code='policy_generation_changed')
            reservation=connection.execute("SELECT request_digest,generation,lifecycle_phase FROM file_operations WHERE owner_subject_id=? AND operation_id=?",(subject_id,operation_id)).fetchone()
            location=connection.execute("SELECT capabilities_json FROM file_policy_locations WHERE id=? AND enabled=1",(location_id,)).fetchone()
            workspace=connection.execute("SELECT location_id,owner_subject_id FROM file_policy_workspaces WHERE id=? AND archived=0",(workspace_id,)).fetchone() if workspace_id is not None else None
            if (not reservation or reservation['request_digest']!=request_digest or reservation['generation']!=expected_generation
                or reservation['lifecycle_phase']!=phase or not location or 'write' not in _parse_json(location['capabilities_json'],[])
                or (workspace_id is not None and (not workspace or workspace['owner_subject_id']!=subject_id or workspace['location_id']!=location_id))):
                raise FilePolicyError('Git approval unavailable',code='provider_unavailable')
            generation=self._bump(connection)
            try:
                connection.execute("INSERT INTO file_policy_bindings(id,binding_class,subject_kind,subject_id,location_id,workspace_id,chat_id,resource_ref,operation,capabilities_json,lifetime,status,remaining_uses,expires_unix_ms,created_by_subject_id,generation,revision,created_unix_ms,updated_unix_ms,migration_source,migration_key) VALUES (?,'operation','user',?,?,?,?,?,?,?,'once','active',1,?,?,?,1,?,?,NULL,NULL)",
                    (identifier,subject_id,location_id,workspace_id,chat_id,resource,operation,_json(['write']),expires_unix_ms,subject_id,generation,now,now))
            except sqlite3.IntegrityError as exc:raise FilePolicyError('Git approval already exists',code='duplicate_binding') from exc
            self._audit(connection,generation=generation,event='create',actor_subject_id=subject_id,target_type='binding',target_id=identifier,reason_code=created_reason,details={'operation':operation,'location_id':location_id})
            connection.commit();return {'binding_id':identifier,'generation':generation}

    def _consume_git_once(self, profile: str, *, subject_id: str, location_id: str, workspace_id: str | None, chat_id: str,
                               operation_id: str, request_digest: str, binding_id: str, expected_generation: int) -> dict[str, Any]:
        """Consume only the exact previously created approval, never a broad match."""
        identifier_prefix, resource_prefix, phase, operation, _, consumed_reason = self._git_once_profile(profile)
        values=(subject_id,location_id,chat_id,operation_id,binding_id)
        if (any(type(v) is not str or not v or v!=v.strip() or len(v.encode('utf-8'))>512 for v in values)
            or (workspace_id is not None and (type(workspace_id) is not str or not workspace_id or workspace_id!=workspace_id.strip() or len(workspace_id.encode('utf-8'))>512))
            or type(request_digest) is not str or len(request_digest)!=64 or any(c not in '0123456789abcdef' for c in request_digest)
            or type(expected_generation) is not int or expected_generation<0):raise FilePolicyError('Git approval unavailable',code='invalid_request')
        expected_id=identifier_prefix+hashlib.sha256(_json([subject_id,operation_id]).encode()).hexdigest()
        with self._connect() as connection:
            self._begin(connection)
            if self._generation(connection)!=expected_generation:raise FilePolicyError('File policy generation changed',code='policy_generation_changed')
            reservation=connection.execute("SELECT request_digest,generation,lifecycle_phase FROM file_operations WHERE owner_subject_id=? AND operation_id=?",(subject_id,operation_id)).fetchone()
            row=connection.execute("SELECT * FROM file_policy_bindings WHERE id=?",(binding_id,)).fetchone()
            workspace=connection.execute("SELECT location_id,owner_subject_id FROM file_policy_workspaces WHERE id=? AND archived=0",(workspace_id,)).fetchone() if workspace_id is not None else None
            if (binding_id!=expected_id or not reservation or reservation['request_digest']!=request_digest or reservation['generation']!=expected_generation-1 or reservation['lifecycle_phase']!=phase
                or (workspace_id is not None and (not workspace or workspace['owner_subject_id']!=subject_id or workspace['location_id']!=location_id))
                or not row or row['binding_class']!='operation' or row['subject_kind']!='user' or row['subject_id']!=subject_id
                or row['location_id']!=location_id or row['workspace_id']!=workspace_id or row['chat_id']!=chat_id
                or row['resource_ref']!=resource_prefix+request_digest or row['operation']!=operation
                or _parse_json(row['capabilities_json'],[])!=['write'] or row['lifetime']!='once' or row['status']!='active'
                or row['remaining_uses']!=1 or row['generation']!=expected_generation or row['revision']!=1 or row['created_by_subject_id']!=subject_id or row['expires_unix_ms'] is None or row['expires_unix_ms']<=_now_ms()):
                raise FilePolicyError('Git approval unavailable',code='provider_unavailable')
            generation=self._bump(connection)
            changed=connection.execute("UPDATE file_policy_bindings SET status='consumed',remaining_uses=0,generation=?,revision=revision+1,updated_unix_ms=? WHERE id=? AND status='active' AND remaining_uses=1",(generation,_now_ms(),binding_id))
            if changed.rowcount!=1:raise FilePolicyError('Git approval unavailable',code='provider_unavailable')
            self._audit(connection,generation=generation,event='consume',actor_subject_id=subject_id,target_type='binding',target_id=binding_id,reason_code=consumed_reason,details={'operation':operation,'location_id':location_id})
            connection.commit();return {'binding_id':binding_id,'generation':generation}

    def create_git_index_once(self, *, subject_id: str, location_id: str, workspace_id: str | None, chat_id: str,
                              operation_id: str, request_digest: str, expected_generation: int, expires_unix_ms: int) -> dict[str, Any]:
        return self._create_git_once('index', subject_id=subject_id, location_id=location_id, workspace_id=workspace_id,
            chat_id=chat_id, operation_id=operation_id, request_digest=request_digest,
            expected_generation=expected_generation, expires_unix_ms=expires_unix_ms)

    def consume_git_index_once(self, *, subject_id: str, location_id: str, workspace_id: str | None, chat_id: str,
                               operation_id: str, request_digest: str, binding_id: str, expected_generation: int) -> dict[str, Any]:
        return self._consume_git_once('index', subject_id=subject_id, location_id=location_id, workspace_id=workspace_id,
            chat_id=chat_id, operation_id=operation_id, request_digest=request_digest,
            binding_id=binding_id, expected_generation=expected_generation)

    def create_git_commit_once(self, *, subject_id: str, location_id: str, workspace_id: str | None, chat_id: str,
                              operation_id: str, request_digest: str, expected_generation: int, expires_unix_ms: int) -> dict[str, Any]:
        return self._create_git_once('commit', subject_id=subject_id, location_id=location_id, workspace_id=workspace_id,
            chat_id=chat_id, operation_id=operation_id, request_digest=request_digest,
            expected_generation=expected_generation, expires_unix_ms=expires_unix_ms)

    def consume_git_commit_once(self, *, subject_id: str, location_id: str, workspace_id: str | None, chat_id: str,
                               operation_id: str, request_digest: str, binding_id: str, expected_generation: int) -> dict[str, Any]:
        return self._consume_git_once('commit', subject_id=subject_id, location_id=location_id, workspace_id=workspace_id,
            chat_id=chat_id, operation_id=operation_id, request_digest=request_digest,
            binding_id=binding_id, expected_generation=expected_generation)

    def create_git_cleanup_once(self, *, subject_id: str, location_id: str, workspace_id: str | None, chat_id: str,
                                operation_id: str, request_digest: str, expected_generation: int, expires_unix_ms: int) -> dict[str, Any]:
        return self._create_git_once('cleanup', subject_id=subject_id, location_id=location_id, workspace_id=workspace_id,
            chat_id=chat_id, operation_id=operation_id, request_digest=request_digest,
            expected_generation=expected_generation, expires_unix_ms=expires_unix_ms)

    def consume_git_cleanup_once(self, *, subject_id: str, location_id: str, workspace_id: str | None, chat_id: str,
                                 operation_id: str, request_digest: str, binding_id: str, expected_generation: int) -> dict[str, Any]:
        return self._consume_git_once('cleanup', subject_id=subject_id, location_id=location_id, workspace_id=workspace_id,
            chat_id=chat_id, operation_id=operation_id, request_digest=request_digest,
            binding_id=binding_id, expected_generation=expected_generation)

    def create_git_branch_once(self, *, subject_id: str, location_id: str, workspace_id: str | None, chat_id: str,
                               operation_id: str, request_digest: str, expected_generation: int, expires_unix_ms: int) -> dict[str, Any]:
        return self._create_git_once('branch_create', subject_id=subject_id, location_id=location_id, workspace_id=workspace_id,
            chat_id=chat_id, operation_id=operation_id, request_digest=request_digest,
            expected_generation=expected_generation, expires_unix_ms=expires_unix_ms)

    def consume_git_branch_once(self, *, subject_id: str, location_id: str, workspace_id: str | None, chat_id: str,
                                operation_id: str, request_digest: str, binding_id: str, expected_generation: int) -> dict[str, Any]:
        return self._consume_git_once('branch_create', subject_id=subject_id, location_id=location_id, workspace_id=workspace_id,
            chat_id=chat_id, operation_id=operation_id, request_digest=request_digest,
            binding_id=binding_id, expected_generation=expected_generation)

    def create_git_fetch_once(self, *, subject_id: str, location_id: str, workspace_id: str | None, chat_id: str,
                              operation_id: str, request_digest: str, expected_generation: int, expires_unix_ms: int) -> dict[str, Any]:
        return self._create_git_once('fetch', subject_id=subject_id, location_id=location_id, workspace_id=workspace_id,
            chat_id=chat_id, operation_id=operation_id, request_digest=request_digest,
            expected_generation=expected_generation, expires_unix_ms=expires_unix_ms)

    def consume_git_fetch_once(self, *, subject_id: str, location_id: str, workspace_id: str | None, chat_id: str,
                               operation_id: str, request_digest: str, binding_id: str, expected_generation: int) -> dict[str, Any]:
        return self._consume_git_once('fetch', subject_id=subject_id, location_id=location_id, workspace_id=workspace_id,
            chat_id=chat_id, operation_id=operation_id, request_digest=request_digest,
            binding_id=binding_id, expected_generation=expected_generation)

    def create_git_push_once(self, *, subject_id: str, location_id: str, workspace_id: str | None, chat_id: str,
                             operation_id: str, request_digest: str, expected_generation: int, expires_unix_ms: int) -> dict[str, Any]:
        return self._create_git_once('push', subject_id=subject_id, location_id=location_id, workspace_id=workspace_id,
            chat_id=chat_id, operation_id=operation_id, request_digest=request_digest,
            expected_generation=expected_generation, expires_unix_ms=expires_unix_ms)

    def consume_git_push_once(self, *, subject_id: str, location_id: str, workspace_id: str | None, chat_id: str,
                              operation_id: str, request_digest: str, binding_id: str, expected_generation: int) -> dict[str, Any]:
        return self._consume_git_once('push', subject_id=subject_id, location_id=location_id, workspace_id=workspace_id,
            chat_id=chat_id, operation_id=operation_id, request_digest=request_digest,
            binding_id=binding_id, expected_generation=expected_generation)

    def create_git_worktree_stage_once(self, *, subject_id: str, location_id: str, workspace_id: str | None, chat_id: str,
                                       operation_id: str, request_digest: str, expected_generation: int, expires_unix_ms: int) -> dict[str, Any]:
        return self._create_git_once('worktree_stage', subject_id=subject_id, location_id=location_id, workspace_id=workspace_id,
            chat_id=chat_id, operation_id=operation_id, request_digest=request_digest, expected_generation=expected_generation, expires_unix_ms=expires_unix_ms)

    def consume_git_worktree_stage_once(self, *, subject_id: str, location_id: str, workspace_id: str | None, chat_id: str,
                                        operation_id: str, request_digest: str, binding_id: str, expected_generation: int) -> dict[str, Any]:
        return self._consume_git_once('worktree_stage', subject_id=subject_id, location_id=location_id, workspace_id=workspace_id,
            chat_id=chat_id, operation_id=operation_id, request_digest=request_digest, binding_id=binding_id, expected_generation=expected_generation)

    def create_git_worktree_inverse_once(self, *, subject_id: str, location_id: str, workspace_id: str | None, chat_id: str,
                                         operation_id: str, request_digest: str, expected_generation: int, expires_unix_ms: int) -> dict[str, Any]:
        return self._create_git_once('worktree_inverse', subject_id=subject_id, location_id=location_id, workspace_id=workspace_id,
            chat_id=chat_id, operation_id=operation_id, request_digest=request_digest, expected_generation=expected_generation, expires_unix_ms=expires_unix_ms)

    def consume_git_worktree_inverse_once(self, *, subject_id: str, location_id: str, workspace_id: str | None, chat_id: str,
                                          operation_id: str, request_digest: str, binding_id: str, expected_generation: int) -> dict[str, Any]:
        return self._consume_git_once('worktree_inverse', subject_id=subject_id, location_id=location_id, workspace_id=workspace_id,
            chat_id=chat_id, operation_id=operation_id, request_digest=request_digest, binding_id=binding_id, expected_generation=expected_generation)

    def create_git_worktree_redo_once(self, *, subject_id: str, location_id: str, workspace_id: str | None, chat_id: str,
                                      operation_id: str, request_digest: str, expected_generation: int, expires_unix_ms: int) -> dict[str, Any]:
        return self._create_git_once('worktree_redo', subject_id=subject_id, location_id=location_id, workspace_id=workspace_id,
            chat_id=chat_id, operation_id=operation_id, request_digest=request_digest, expected_generation=expected_generation, expires_unix_ms=expires_unix_ms)

    def consume_git_worktree_redo_once(self, *, subject_id: str, location_id: str, workspace_id: str | None, chat_id: str,
                                       operation_id: str, request_digest: str, binding_id: str, expected_generation: int) -> dict[str, Any]:
        return self._consume_git_once('worktree_redo', subject_id=subject_id, location_id=location_id, workspace_id=workspace_id,
            chat_id=chat_id, operation_id=operation_id, request_digest=request_digest, binding_id=binding_id, expected_generation=expected_generation)

    def create_git_checkout_once(self, *, subject_id: str, location_id: str, workspace_id: str | None, chat_id: str,
                                 operation_id: str, request_digest: str, expected_generation: int, expires_unix_ms: int) -> dict[str, Any]:
        return self._create_git_once('checkout', subject_id=subject_id, location_id=location_id, workspace_id=workspace_id,
            chat_id=chat_id, operation_id=operation_id, request_digest=request_digest,
            expected_generation=expected_generation, expires_unix_ms=expires_unix_ms)

    def consume_git_checkout_once(self, *, subject_id: str, location_id: str, workspace_id: str | None, chat_id: str,
                                  operation_id: str, request_digest: str, binding_id: str, expected_generation: int) -> dict[str, Any]:
        return self._consume_git_once('checkout', subject_id=subject_id, location_id=location_id, workspace_id=workspace_id,
            chat_id=chat_id, operation_id=operation_id, request_digest=request_digest,
            binding_id=binding_id, expected_generation=expected_generation)

    def create_binding(
        self,
        *,
        actor_subject_id: str,
        binding_class: str,
        subject_id: str,
        capabilities: Iterable[str],
        lifetime: str = "always",
        subject_kind: str = "user",
        location_id: str | None = None,
        workspace_id: str | None = None,
        chat_id: str | None = None,
        resource_ref: str | None = None,
        operation: str | None = None,
        expires_unix_ms: int | None = None,
        binding_id: str | None = None,
        migration_source: str | None = None,
        migration_key: str | None = None,
    ) -> PolicyBinding:
        actor = str(actor_subject_id or "").strip()
        subject = str(subject_id or "").strip()
        binding_class = str(binding_class or "").strip().lower()
        subject_kind = str(subject_kind or "").strip().lower()
        lifetime = str(lifetime or "").strip().lower()
        if not actor or not subject:
            raise FilePolicyError("actor and subject are required", code="subject_required")
        if binding_class not in BINDING_CLASSES:
            raise FilePolicyError("unsupported binding class", code="invalid_binding_class")
        if subject_kind not in SUBJECT_KINDS:
            raise FilePolicyError("unsupported subject kind", code="invalid_subject_kind")
        if lifetime not in LIFETIMES:
            raise FilePolicyError("unsupported policy lifetime", code="invalid_lifetime")
        caps = _capabilities(capabilities)
        if not location_id and (binding_class != "operation" or set(caps) != {"execute"}):
            raise FilePolicyError("every binding requires a location", code="location_required")
        if binding_class == "operation" and (not resource_ref or not operation):
            raise FilePolicyError("operation bindings require a resource and operation", code="operation_required")
        if lifetime == "once" and binding_class != "operation":
            raise FilePolicyError("once lifetime is reserved for operation approvals", code="invalid_lifetime")
        if lifetime == "chat" and not chat_id:
            raise FilePolicyError("chat lifetime requires a chat id", code="chat_required")
        if lifetime == "workspace" and not workspace_id:
            raise FilePolicyError("workspace lifetime requires a workspace id", code="workspace_required")
        if lifetime == "always" and (chat_id or workspace_id):
            raise FilePolicyError("always lifetime cannot hide a chat/workspace constraint", code="invalid_lifetime_scope")
        identifier = str(binding_id or f"binding-{uuid.uuid4().hex}")
        now = _now_ms()
        with self._connect() as connection:
            self._begin(connection)
            location = None
            if location_id:
                location = connection.execute(
                    "SELECT capabilities_json FROM file_policy_locations WHERE id=? AND enabled=1",
                    (str(location_id),),
                ).fetchone()
                if not location:
                    connection.rollback()
                    raise FilePolicyError("binding location was not found", code="location_not_found")
                if not set(caps).issubset(set(_parse_json(location["capabilities_json"], []))):
                    connection.rollback()
                    raise FilePolicyError("binding exceeds location capabilities", code="capability_escalation")
            if workspace_id:
                workspace = connection.execute(
                    "SELECT location_id,owner_subject_id FROM file_policy_workspaces WHERE id=? AND archived=0",
                    (str(workspace_id),),
                ).fetchone()
                if not workspace:
                    connection.rollback()
                    raise FilePolicyError("binding workspace was not found", code="workspace_not_found")
                if subject_kind != "user" or workspace["owner_subject_id"] != subject:
                    connection.rollback()
                    raise FilePolicyError("workspace belongs to another subject", code="workspace_owner_mismatch")
                if location_id and workspace["location_id"] != str(location_id):
                    connection.rollback()
                    raise FilePolicyError("workspace does not belong to location", code="workspace_location_mismatch")
            generation = self._bump(connection)
            remaining = 1 if lifetime == "once" else None
            try:
                connection.execute(
                    """
                    INSERT INTO file_policy_bindings(
                        id,binding_class,subject_kind,subject_id,location_id,
                        workspace_id,chat_id,resource_ref,operation,
                        capabilities_json,lifetime,status,remaining_uses,
                        expires_unix_ms,created_by_subject_id,generation,revision,
                        created_unix_ms,updated_unix_ms,migration_source,migration_key
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,'active',?,?,?,?,1,?,?,?,?)
                    """,
                    (
                        identifier,
                        binding_class,
                        subject_kind,
                        subject,
                        str(location_id) if location_id else None,
                        str(workspace_id) if workspace_id else None,
                        str(chat_id) if chat_id else None,
                        str(resource_ref) if resource_ref else None,
                        str(operation) if operation else None,
                        _json(caps),
                        lifetime,
                        remaining,
                        int(expires_unix_ms) if expires_unix_ms is not None else None,
                        actor,
                        generation,
                        now,
                        now,
                        migration_source,
                        migration_key,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                connection.rollback()
                raise FilePolicyError("binding identity already exists", code="duplicate_binding") from exc
            self._audit(
                connection,
                generation=generation,
                event="create",
                actor_subject_id=actor,
                target_type="binding",
                target_id=identifier,
                reason_code="binding_created",
                details={
                    "binding_class": binding_class,
                    "lifetime": lifetime,
                    "location_id": str(location_id) if location_id else None,
                    "workspace_id": str(workspace_id) if workspace_id else None,
                    "capabilities": list(caps),
                },
            )
            connection.commit()
            return self.get_binding(identifier)

    def get_binding(self, binding_id: str) -> PolicyBinding:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM file_policy_bindings WHERE id=?", (str(binding_id),)
            ).fetchone()
        if not row:
            raise FilePolicyError("binding was not found", code="binding_not_found")
        return self._binding(row)

    def create_resource_role(self, *, owner_subject_id: str, recipient_subject_id: str,
                             location_id: str, resource_id: str, role: str,
                             owner_is_admin: bool, expected_generation: int,
                             expires_unix_ms: int | None = None, owner_workspace_id: str | None = None,
                             relative_resource: str = '') -> PolicyBinding:
        """Create one exact-resource known-user role, never a location grant.

        Caller separately proves the server-resolved resource belongs to this
        location and pins its revision. The generation CAS joins that proof to
        the existing policy transaction; a string selector grants nothing.
        """
        import re
        owner = str(owner_subject_id or '').strip(); recipient = str(recipient_subject_id or '').strip()
        location = str(location_id or '').strip(); selected = str(resource_id or '').strip()
        if (not owner or not recipient or not location or not re.fullmatch(r'resource-[0-9a-f]{64}', selected)
                or role not in {'viewer','commenter','editor'} or type(owner_is_admin) is not bool
                or type(expected_generation) is not int or expected_generation < 0
                or (expires_unix_ms is not None and (type(expires_unix_ms) is not int or expires_unix_ms <= _now_ms()))):
            raise FilePolicyError('resource role unavailable', code='invalid_binding')
        relative = _relative_folder(relative_resource)
        caps = ('read','write') if role == 'editor' else ('read',)
        identifier = uuid.uuid4().hex; now = _now_ms()
        with self._connect() as connection:
            self._begin(connection)
            if self._generation(connection) != expected_generation:
                raise FilePolicyError('File access changed; retry', code='policy_generation_changed')
            loc = connection.execute('SELECT * FROM file_policy_locations WHERE id=?', (location,)).fetchone()
            if not loc or not loc['enabled'] or loc['availability'] != 'available':
                raise FilePolicyError('resource location unavailable', code='location_unavailable')
            if owner_workspace_id is not None:
                workspace = connection.execute('SELECT * FROM file_policy_workspaces WHERE id=? AND archived=0', (owner_workspace_id,)).fetchone()
                if (not workspace or workspace['owner_subject_id'] != owner or workspace['location_id'] != location
                        or not _relative_contains(workspace['relative_folder'], relative)):
                    raise FilePolicyError('owner workspace unavailable', code='workspace_unavailable')
            ceiling = set(_parse_json(loc['capabilities_json'], []))
            available = ceiling if owner_is_admin else self.people_capabilities(owner, location, workspace_id=owner_workspace_id, connection=connection) & ceiling
            if not set(caps).issubset(available):
                raise FilePolicyError('resource role exceeds owner access', code='capability_denied')
            if not connection.execute('SELECT 1 FROM file_policy_subject_aliases WHERE subject_id=? LIMIT 1', (recipient,)).fetchone():
                raise FilePolicyError('recipient unavailable', code='subject_required')
            generation = self._bump(connection)
            connection.execute("""INSERT INTO file_policy_bindings(id,binding_class,subject_kind,subject_id,location_id,
                workspace_id,chat_id,resource_ref,operation,capabilities_json,lifetime,status,remaining_uses,
                expires_unix_ms,created_by_subject_id,generation,revision,created_unix_ms,updated_unix_ms,migration_source,migration_key)
                VALUES (?,'people','user',?,?,NULL,NULL,?,?,?,'always','active',NULL,?,?,?,1,?,?,NULL,NULL)""",
                (identifier, recipient, location, selected, 'share-role:v1:'+role, _json(list(caps)), expires_unix_ms, owner, generation, now, now))
            self._audit(connection,generation=generation,event='create',actor_subject_id=owner,target_type='binding',
                target_id=identifier,reason_code='resource_role_created',details={'role':role})
            connection.commit()
        return self.get_binding(identifier)

    def list_owned_resource_roles(self, *, owner_subject_id: str, location_id: str,
                                  resource_id: str) -> tuple[list[dict], bool]:
        """Bounded exact-resource active role metadata; caller owns current source proof."""
        import re
        owner = str(owner_subject_id or '').strip(); location = str(location_id or '').strip()
        if not owner or not location or not re.fullmatch(r'resource-[0-9a-f]{64}', str(resource_id or '')):
            raise FilePolicyError('Resource roles unavailable', code='invalid_binding')
        with self._connect() as connection:
            rows = connection.execute("""SELECT id,subject_id,operation,capabilities_json,expires_unix_ms
                FROM file_policy_bindings WHERE created_by_subject_id=? AND location_id=? AND resource_ref=?
                  AND binding_class='people' AND subject_kind='user' AND status='active' AND lifetime='always'
                  AND workspace_id IS NULL AND chat_id IS NULL
                  AND operation IN ('share-role:v1:viewer','share-role:v1:commenter','share-role:v1:editor')
                  AND (expires_unix_ms IS NULL OR expires_unix_ms>?) ORDER BY id LIMIT 101""",
                (owner,location,resource_id,_now_ms())).fetchall()
        result=[]; partial=len(rows)>100
        for row in rows[:100]:
            role = row['operation'][len('share-role:v1:'):]
            expected = {'read','write'} if role == 'editor' else {'read'}
            if (not row['subject_id'] or set(_parse_json(row['capabilities_json'], [])) != expected):
                partial=True; continue
            result.append({'binding_id':row['id'],'recipient_subject_id':row['subject_id'],
                           'role':role,'expires_unix_ms':row['expires_unix_ms']})
        return result, partial

    def recipient_share_role_owners(self, *, recipient_subject_id: str) -> tuple[list[str], bool]:
        """Bounded creator candidates; exact target authority is checked separately."""
        recipient = str(recipient_subject_id or '').strip()
        if not recipient: return [], False
        with self._connect() as connection:
            rows = connection.execute("""SELECT DISTINCT created_by_subject_id FROM file_policy_bindings
                WHERE subject_kind='user' AND subject_id=? AND binding_class='people' AND status='active'
                  AND lifetime='always' AND workspace_id IS NULL AND chat_id IS NULL
                  AND resource_ref IS NOT NULL AND created_by_subject_id IS NOT NULL
                  AND operation IN ('share-role:v1:viewer','share-role:v1:commenter','share-role:v1:editor')
                  AND (expires_unix_ms IS NULL OR expires_unix_ms>?)
                ORDER BY created_by_subject_id LIMIT 33""", (recipient, _now_ms())).fetchall()
        return [str(row['created_by_subject_id']) for row in rows[:32]], len(rows) > 32

    def resolve_resource_role(self, *, owner_subject_id: str, recipient_subject_id: str,
                              location_id: str, resource_id: str, connection: sqlite3.Connection | None = None) -> str | None:
        """Return only an active exact resource role; never expand path access."""
        import re
        owner = str(owner_subject_id or '').strip(); recipient = str(recipient_subject_id or '').strip()
        location = str(location_id or '').strip(); selected = str(resource_id or '').strip()
        if not owner or not recipient or not location or not re.fullmatch(r'resource-[0-9a-f]{64}', selected): return None
        if connection is None:
            with self._connect() as owned:
                owned.execute('BEGIN')
                return self.resolve_resource_role(owner_subject_id=owner, recipient_subject_id=recipient, location_id=location, resource_id=selected, connection=owned)
        if connection is not None:
            loc = connection.execute('SELECT enabled,availability,capabilities_json FROM file_policy_locations WHERE id=?',(location,)).fetchone()
            if not loc or not loc['enabled'] or loc['availability'] != 'available': return None
            rows = connection.execute("""SELECT operation,capabilities_json FROM file_policy_bindings
                WHERE binding_class='people' AND subject_kind='user' AND subject_id=? AND created_by_subject_id=?
                  AND location_id=? AND resource_ref=? AND status='active' AND lifetime='always'
                  AND workspace_id IS NULL AND chat_id IS NULL AND (expires_unix_ms IS NULL OR expires_unix_ms>?)""",
                (recipient,owner,location,selected,_now_ms())).fetchall()
            ranks = {'viewer':1,'commenter':2,'editor':3}; best = None
            for row in rows:
                op = row['operation']; role = op[len('share-role:v1:'):] if isinstance(op,str) and op.startswith('share-role:v1:') else None
                expected = {'read','write'} if role == 'editor' else {'read'}
                if (role in ranks and set(_parse_json(row['capabilities_json'], [])) == expected
                        and expected.issubset(set(_parse_json(loc['capabilities_json'], [])))
                        and (best is None or ranks[role] > ranks[best])): best = role
            return best

    def list_bindings(
        self,
        *,
        subject_id: str | None = None,
        binding_class: str | None = None,
        include_inactive: bool = False,
    ) -> list[PolicyBinding]:
        self.expire_bindings()
        clauses: list[str] = []
        params: list[Any] = []
        if subject_id is not None:
            clauses.append("subject_id=?")
            params.append(str(subject_id))
        if binding_class is not None:
            clauses.append("binding_class=?")
            params.append(str(binding_class))
        if not include_inactive:
            clauses.append("status='active'")
        query = "SELECT * FROM file_policy_bindings"
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY id"
        with self._connect() as connection:
            return [self._binding(row) for row in connection.execute(query, params)]

    def update_binding(
        self,
        binding_id: str,
        *,
        actor_subject_id: str,
        capabilities: Iterable[str] | None = None,
        expires_unix_ms: Any = _UNSET,
        enabled: bool | None = None,
    ) -> PolicyBinding:
        """Narrow/change one binding and advance the shared policy generation."""
        with self._connect() as connection:
            self._begin(connection)
            row = connection.execute(
                "SELECT * FROM file_policy_bindings WHERE id=?", (str(binding_id),)
            ).fetchone()
            if not row:
                connection.rollback()
                raise FilePolicyError("binding was not found", code="binding_not_found")
            new_capabilities = tuple(_parse_json(row["capabilities_json"], []))
            if capabilities is not None:
                new_capabilities = _capabilities(capabilities)
                location = connection.execute(
                    "SELECT capabilities_json FROM file_policy_locations WHERE id=?",
                    (row["location_id"],),
                ).fetchone()
                if not location or not set(new_capabilities).issubset(
                    set(_parse_json(location["capabilities_json"], []))
                ):
                    connection.rollback()
                    raise FilePolicyError("binding exceeds location capabilities", code="capability_escalation")
            status = row["status"]
            if enabled is True:
                if row["lifetime"] == "once" and row["remaining_uses"] != 1:
                    raise FilePolicyError("Consumed approvals cannot be reactivated", code="once_consumed")
                if row["location_id"]:
                    location = connection.execute("SELECT enabled,availability FROM file_policy_locations WHERE id=?", (row["location_id"],)).fetchone()
                    if not location or not location["enabled"] or location["availability"] != "available":
                        raise FilePolicyError("Location is unavailable", code="location_not_found")
                if row["workspace_id"]:
                    workspace = connection.execute("SELECT archived,owner_subject_id FROM file_policy_workspaces WHERE id=?", (row["workspace_id"],)).fetchone()
                    if not workspace or workspace["archived"] or workspace["owner_subject_id"] != row["subject_id"]:
                        raise FilePolicyError("Workspace is unavailable", code="workspace_not_found")
            if enabled is not None:
                status = "active" if enabled else "revoked"
            generation = self._bump(connection)
            connection.execute(
                """
                UPDATE file_policy_bindings
                SET capabilities_json=?,expires_unix_ms=?,status=?,generation=?,
                    revision=revision+1,updated_unix_ms=?
                WHERE id=?
                """,
                (
                    _json(new_capabilities),
                    row["expires_unix_ms"] if expires_unix_ms is _UNSET else int(expires_unix_ms) if expires_unix_ms is not None else None,
                    status,
                    generation,
                    _now_ms(),
                    str(binding_id),
                ),
            )
            self._audit(
                connection,
                generation=generation,
                event="change",
                actor_subject_id=str(actor_subject_id),
                target_type="binding",
                target_id=str(binding_id),
                reason_code="binding_changed",
                details={"capabilities": list(new_capabilities), "status": status},
            )
            connection.commit()
            return self.get_binding(str(binding_id))

    @staticmethod
    def _matching_bindings(
        connection: sqlite3.Connection,
        *,
        binding_class: str,
        subject_id: str,
        group_ids: Sequence[str],
        location_id: str,
        workspace_id: str | None,
        chat_id: str | None,
        resource_ref: str | None = None,
        operation: str | None = None,
        now_ms: int,
    ) -> list[sqlite3.Row]:
        rows = connection.execute(
            """
            SELECT * FROM file_policy_bindings
            WHERE binding_class=? AND status='active' AND location_id=?
              AND (expires_unix_ms IS NULL OR expires_unix_ms>?)
            ORDER BY id
            """,
            (binding_class, location_id, now_ms),
        ).fetchall()
        groups = {str(value) for value in group_ids}
        matched: list[sqlite3.Row] = []
        for row in rows:
            if row["subject_kind"] == "user":
                if row["subject_id"] != subject_id:
                    continue
            elif row["subject_kind"] == "group":
                if row["subject_id"] not in groups:
                    continue
            else:
                continue
            lifetime = row["lifetime"]
            if lifetime == "once" and int(row["remaining_uses"] or 0) <= 0:
                continue
            if lifetime == "chat" and row["chat_id"] != chat_id:
                continue
            if lifetime == "workspace" and row["workspace_id"] != workspace_id:
                continue
            if lifetime == "always" and (row["chat_id"] or row["workspace_id"]):
                continue
            if row["workspace_id"] and row["workspace_id"] != workspace_id:
                continue
            if row["chat_id"] and row["chat_id"] != chat_id:
                continue
            # Exact share roles use only their closed reader. Other legacy
            # resource-scoped bindings may match only an explicit resource.
            if binding_class in {"people", "agent"}:
                if isinstance(row["operation"], str) and row["operation"].startswith("share-role:v1:"):
                    continue
                if resource_ref is None and row["resource_ref"] is not None:
                    continue
            if resource_ref is not None and row["resource_ref"] != resource_ref:
                continue
            if operation is not None and row["operation"] != operation:
                continue
            matched.append(row)
        return matched

    def resolve(
        self,
        *,
        subject_id: str,
        is_admin: bool,
        origin: str,
        location_id: str,
        capability: str,
        workspace_id: str | None = None,
        relative_resource: str = "",
        chat_id: str | None = None,
        group_ids: Sequence[str] = (),
        required_operation: str | None = None,
        resource_ref: str | None = None,
        observation_only: bool = False,
    ) -> EffectiveScope:
        """Resolve and, when needed, atomically consume one Once approval."""
        if observation_only:
            if required_operation is not None:
                raise FilePolicyError("Observation cannot consume an operation approval", code="invalid_observation")
        subject = str(subject_id or "").strip()
        origin = str(origin or "").strip().lower()
        capability = str(capability or "").strip().lower()
        location_id = str(location_id or "").strip()
        if not subject or not location_id:
            raise FilePolicyError("subject and location are required", code="scope_required")
        if origin not in ORIGINS:
            raise FilePolicyError("origin must be app or agent", code="invalid_origin")
        if capability not in CAPABILITIES:
            raise FilePolicyError("unsupported capability", code="invalid_capability")
        relative = _relative_folder(relative_resource)
        if not observation_only:
            self.expire_bindings()
        now = _now_ms()
        with self._connect() as connection:
            if required_operation:
                self._begin(connection)
            else:
                # Pure scope resolution needs one coherent generation/grant
                # snapshot, not the authority writer used by publication.
                connection.execute("BEGIN")
            generation = self._generation(connection)
            location = connection.execute(
                "SELECT * FROM file_policy_locations WHERE id=?", (location_id,)
            ).fetchone()

            def denied(reason: str) -> EffectiveScope:
                connection.rollback()
                return EffectiveScope(
                    False,
                    subject,
                    origin,
                    location_id,
                    workspace_id,
                    (),
                    generation,
                    reason,
                )

            if not location or not location["enabled"] or location["availability"] != "available":
                return denied("location_unavailable")
            location_caps = set(_parse_json(location["capabilities_json"], []))
            if capability not in location_caps:
                return denied("capability_denied")

            workspace = None
            if workspace_id:
                workspace = connection.execute(
                    "SELECT * FROM file_policy_workspaces WHERE id=? AND archived=0",
                    (str(workspace_id),),
                ).fetchone()
                if not workspace or workspace["location_id"] != location_id:
                    return denied("workspace_unavailable")
                if workspace["owner_subject_id"] != subject and not is_admin:
                    return denied("workspace_unavailable")
                if not _relative_contains(workspace["relative_folder"], relative):
                    return denied("outside_workspace")

            if is_admin:
                app_caps = set(location_caps)
            else:
                people = self._matching_bindings(
                    connection,
                    binding_class="people",
                    subject_id=subject,
                    group_ids=group_ids,
                    location_id=location_id,
                    workspace_id=workspace_id,
                    chat_id=chat_id,
                    now_ms=now,
                )
                app_caps = self.people_capabilities(subject, location_id, workspace_id=workspace_id,
                    chat_id=chat_id, connection=connection) & location_caps
                # Authenticated group membership still uses the same-location
                # resolver; migration never invents group membership.
                app_caps |= {value for row in people if row["subject_kind"] == "group"
                    for value in _parse_json(row["capabilities_json"], [])} & location_caps
            if capability not in app_caps:
                return denied("app_visibility_denied")

            effective = set(app_caps)
            if origin == "agent":
                agents = self._matching_bindings(
                    connection,
                    binding_class="agent",
                    subject_id=subject,
                    group_ids=group_ids,
                    location_id=location_id,
                    workspace_id=workspace_id,
                    chat_id=chat_id,
                    now_ms=now,
                )
                agent_caps = self.people_capabilities(subject, location_id, workspace_id=workspace_id,
                    chat_id=chat_id, connection=connection, binding_class="agent") & location_caps
                agent_caps |= {value for row in agents if row["subject_kind"] == "group"
                    for value in _parse_json(row["capabilities_json"], [])} & location_caps
                effective &= agent_caps
                if capability not in effective:
                    return denied("agent_binding_denied")

            consumed: Optional[str] = None
            if required_operation:
                if not resource_ref:
                    return denied("operation_resource_required")
                approvals = self._matching_bindings(
                    connection,
                    binding_class="operation",
                    subject_id=subject,
                    group_ids=group_ids,
                    location_id=location_id,
                    workspace_id=workspace_id,
                    chat_id=chat_id,
                    resource_ref=resource_ref,
                    operation=str(required_operation),
                    now_ms=now,
                )
                approvals = [
                    row for row in approvals
                    if capability in set(_parse_json(row["capabilities_json"], []))
                ]
                if not approvals:
                    return denied("operation_approval_required")
                once = next((row for row in approvals if row["lifetime"] == "once"), None)
                if once is not None:
                    updated = connection.execute(
                        """
                        UPDATE file_policy_bindings
                        SET remaining_uses=0,status='consumed',revision=revision+1,
                            updated_unix_ms=?
                        WHERE id=? AND status='active' AND remaining_uses=1
                        """,
                        (now, once["id"]),
                    )
                    if updated.rowcount != 1:
                        return denied("operation_approval_required")
                    generation = self._bump(connection)
                    consumed = once["id"]
                    self._audit(
                        connection,
                        generation=generation,
                        event="consume",
                        actor_subject_id=subject,
                        target_type="binding",
                        target_id=consumed,
                        reason_code="once_consumed",
                        details={"operation": str(required_operation), "location_id": location_id},
                    )
            connection.commit()
            return EffectiveScope(
                True,
                subject,
                origin,
                location_id,
                workspace_id,
                tuple(sorted(effective)),
                generation,
                "allowed",
                consumed,
            )

    def match_operation_approval(
        self,
        *,
        subject_id: str,
        location_id: str,
        operation: str,
        resource_refs: Iterable[str],
        capability: str,
        workspace_id: str | None = None,
        chat_id: str | None = None,
    ) -> bool:
        """Check one active canonical approval, consuming Once atomically.

        This is the canonical consent-ledger lookup used by the interactive
        approval lanes after the authority door has already resolved People /
        Agent ceilings; it consumes a Once binding and never broadens
        beyond the recorded lifetime dimensions.
        """
        subject = str(subject_id or "").strip()
        location = str(location_id or "").strip()
        operation = str(operation or "")
        refs = {str(value) for value in resource_refs}
        capability = str(capability or "").strip().lower()
        if not subject or not location or not operation or not refs or not capability:
            return False
        self.expire_bindings()
        with self._connect() as connection:
            self._begin(connection)
            rows = self._matching_bindings(connection, binding_class="operation", subject_id=subject,
                group_ids=(), location_id=location, workspace_id=workspace_id, chat_id=chat_id,
                operation=operation, now_ms=_now_ms())
            rows = [row for row in rows if row["resource_ref"] in refs and capability in set(_parse_json(row["capabilities_json"], []))]
            if not rows:
                connection.rollback()
                return False
            row = next((candidate for candidate in rows if candidate["lifetime"] != "once"), rows[0])
            if row["lifetime"] == "once":
                generation = self._bump(connection)
                connection.execute("UPDATE file_policy_bindings SET status='consumed',remaining_uses=0,generation=?,revision=revision+1,updated_unix_ms=? WHERE id=? AND remaining_uses=1", (generation, _now_ms(), row["id"]))
                self._audit(connection, generation=generation, event="consume", actor_subject_id=subject,
                    target_type="binding", target_id=row["id"], reason_code="once_consumed")
            connection.commit()
            return True

    def revoke_binding(
        self,
        binding_id: str,
        *,
        actor_subject_id: str,
        reason_code: str = "binding_revoked",
    ) -> bool:
        with self._connect() as connection:
            self._begin(connection)
            row = connection.execute(
                "SELECT status FROM file_policy_bindings WHERE id=?", (str(binding_id),)
            ).fetchone()
            if not row or row["status"] != "active":
                connection.rollback()
                return False
            generation = self._bump(connection)
            connection.execute(
                """UPDATE file_policy_bindings
                   SET status='revoked',generation=?,revision=revision+1,updated_unix_ms=?
                   WHERE id=?""",
                (generation, _now_ms(), str(binding_id)),
            )
            self._audit(
                connection,
                generation=generation,
                event="revoke",
                actor_subject_id=str(actor_subject_id),
                target_type="binding",
                target_id=str(binding_id),
                reason_code=reason_code,
            )
            connection.commit()
            return True

    @staticmethod
    def _reset_selection(
        connection: sqlite3.Connection,
        *,
        subject_id: str,
        scope: str,
        chat_id: str | None,
        workspace_id: str | None,
        location_id: str | None,
    ) -> list[sqlite3.Row]:
        """Select active agent/operation authority for one reset domain.

        People bindings are deliberately absent: resetting what an agent may do
        must never silently revoke another person's ability to browse/download.
        Workspace reset includes its chat-scoped descendants because those rows
        carry the same stable workspace id; chat reset remains the narrowest.
        """
        clauses = [
            "subject_kind='user'",
            "subject_id=?",
            "binding_class IN ('agent','operation')",
            "status='active'",
        ]
        params: list[Any] = [subject_id]
        if scope == "chat":
            clauses.append("lifetime='chat'")
            clauses.append("chat_id=?")
            params.append(chat_id)
        elif scope == "workspace":
            clauses.append("workspace_id=?")
            params.append(workspace_id)
        elif scope == "location":
            clauses.append("location_id=?")
            params.append(location_id)
        elif scope != "all_agent":
            raise FilePolicyError("unsupported reset scope", code="invalid_reset_scope")
        return connection.execute(
            "SELECT * FROM file_policy_bindings WHERE " + " AND ".join(clauses) + " ORDER BY id",
            params,
        ).fetchall()

    def preview_agent_reset(
        self,
        *,
        subject_id: str,
        scope: str,
        chat_id: str | None = None,
        workspace_id: str | None = None,
        location_id: str | None = None,
    ) -> dict[str, Any]:
        """Return exact reset counts without changing policy generation."""
        subject, normalized_scope = self._validate_reset_scope(
            subject_id=subject_id,
            scope=scope,
            chat_id=chat_id,
            workspace_id=workspace_id,
            location_id=location_id,
        )
        with self._connect() as connection:
            rows = self._reset_selection(
                connection,
                subject_id=subject,
                scope=normalized_scope,
                chat_id=chat_id,
                workspace_id=workspace_id,
                location_id=location_id,
            )
        return self._reset_summary(normalized_scope, rows, generation=self.generation())

    @staticmethod
    def _validate_reset_scope(
        *,
        subject_id: str,
        scope: str,
        chat_id: str | None,
        workspace_id: str | None,
        location_id: str | None,
    ) -> tuple[str, str]:
        subject = str(subject_id or "").strip()
        normalized_scope = str(scope or "").strip().lower()
        if not subject:
            raise FilePolicyError("subject is required", code="subject_required")
        if normalized_scope not in RESET_SCOPES:
            raise FilePolicyError("unsupported reset scope", code="invalid_reset_scope")
        required = {
            "chat": (chat_id, "chat id", "chat_required"),
            "workspace": (workspace_id, "workspace id", "workspace_required"),
            "location": (location_id, "location id", "location_required"),
        }.get(normalized_scope)
        if required and not str(required[0] or "").strip():
            raise FilePolicyError(f"{required[1]} is required", code=required[2])
        return subject, normalized_scope

    @staticmethod
    def _reset_summary(scope: str, rows: Sequence[sqlite3.Row], *, generation: int) -> dict[str, Any]:
        by_class = {"agent": 0, "operation": 0}
        by_lifetime = {lifetime: 0 for lifetime in sorted(LIFETIMES)}
        for row in rows:
            if row["binding_class"] in by_class:
                by_class[row["binding_class"]] += 1
            if row["lifetime"] in by_lifetime:
                by_lifetime[row["lifetime"]] += 1
        return {
            "scope": scope,
            "matched": len(rows),
            "by_class": by_class,
            "by_lifetime": by_lifetime,
            "people_preserved": True,
            "generation": int(generation),
        }

    def reset_agent_permissions(
        self,
        *,
        actor_subject_id: str,
        subject_id: str,
        scope: str,
        chat_id: str | None = None,
        workspace_id: str | None = None,
        location_id: str | None = None,
        reason_code: str = "agent_permissions_reset",
    ) -> dict[str, Any]:
        """Soft-revoke one reset domain in one generation/audit transaction."""
        actor = str(actor_subject_id or "").strip()
        if not actor:
            raise FilePolicyError("actor subject is required", code="subject_required")
        subject, normalized_scope = self._validate_reset_scope(
            subject_id=subject_id,
            scope=scope,
            chat_id=chat_id,
            workspace_id=workspace_id,
            location_id=location_id,
        )
        with self._connect() as connection:
            self._begin(connection)
            rows = self._reset_selection(
                connection,
                subject_id=subject,
                scope=normalized_scope,
                chat_id=chat_id,
                workspace_id=workspace_id,
                location_id=location_id,
            )
            if not rows:
                current = self._generation(connection)
                connection.rollback()
                return self._reset_summary(normalized_scope, (), generation=current)
            generation = self._bump(connection)
            identifiers = [str(row["id"]) for row in rows]
            placeholders = ",".join("?" for _ in identifiers)
            connection.execute(
                f"""UPDATE file_policy_bindings
                    SET status='revoked',generation=?,revision=revision+1,updated_unix_ms=?
                    WHERE id IN ({placeholders}) AND status='active'""",
                [generation, _now_ms(), *identifiers],
            )
            self._audit(
                connection,
                generation=generation,
                event="reset",
                actor_subject_id=actor,
                target_type="subject",
                target_id=subject,
                reason_code=str(reason_code or "agent_permissions_reset"),
                details={
                    "scope": normalized_scope,
                    "matched": len(rows),
                    "chat_id": str(chat_id) if chat_id else None,
                    "workspace_id": str(workspace_id) if workspace_id else None,
                    "location_id": str(location_id) if location_id else None,
                    "people_preserved": True,
                },
            )
            connection.commit()
        return self._reset_summary(normalized_scope, rows, generation=generation)

    def purge_subject(
        self,
        subject_id: str,
        *,
        actor_subject_id: str,
        reason_code: str = "account_deleted",
    ) -> dict[str, int]:
        """Remove an account's mutable authority without deleting Locations.

        Locations are system-managed physical identities; deleting an account
        removes its Workspaces, Places, and every binding where it is the
        subject. Audit history and unrelated administrators' records remain
        intact.
        """
        subject = str(subject_id or "").strip()
        if not subject:
            raise FilePolicyError("subject is required", code="subject_required")
        with self._connect() as connection:
            self._begin(connection)
            identities = int(connection.execute("DELETE FROM file_policy_subject_aliases WHERE subject_id=?", (subject,)).rowcount or 0)
            migration_items = int(connection.execute("UPDATE file_policy_import_items SET status='revoked',reason='account_deleted' WHERE subject_id=? AND status!='revoked'", (subject,)).rowcount or 0)
            bindings = int(connection.execute(
                "DELETE FROM file_policy_bindings WHERE subject_id=?", (subject,)
            ).rowcount or 0)
            workspaces = int(connection.execute(
                "DELETE FROM file_policy_workspaces WHERE owner_subject_id=?", (subject,)
            ).rowcount or 0)
            places = int(connection.execute(
                "DELETE FROM file_places WHERE owner_subject_id=?", (subject,)
            ).rowcount or 0)
            recents = int(connection.execute(
                "DELETE FROM file_recents WHERE owner_subject_id=?", (subject,)
            ).rowcount or 0)
            saved_searches = int(connection.execute(
                "DELETE FROM file_saved_searches WHERE owner_subject_id=?", (subject,)
            ).rowcount or 0)
            if not bindings and not workspaces and not places and not recents and not saved_searches and not identities and not migration_items:
                connection.rollback()
                return {
                    "bindings": 0,
                    "workspaces": 0,
                    "places": 0,
                    "recents": 0,
                    "saved_searches": 0,
                }
            generation = self._bump(connection)
            self._audit(
                connection,
                generation=generation,
                event="purge",
                actor_subject_id=str(actor_subject_id),
                target_type="subject",
                target_id=subject,
                reason_code=reason_code,
                details={
                    "bindings": bindings,
                    "workspaces": workspaces,
                    "places": places,
                    "recents": recents,
                    "saved_searches": saved_searches,
                },
            )
            connection.commit()
            return {
                "bindings": bindings,
                "workspaces": workspaces,
                "places": places,
                "recents": recents,
                "saved_searches": saved_searches,
            }

    def audit_events(self, *, after_generation: int = -1) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT generation,event,actor_subject_id,target_type,target_id,
                          reason_code,details_json,created_unix_ms
                   FROM file_policy_audit WHERE generation>? ORDER BY id""",
                (int(after_generation),),
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["details"] = _parse_json(item.pop("details_json"), {})
            result.append(item)
        return result


def deterministic_legacy_id(kind: str, legacy_key: str) -> str:
    """Stable, provenance-preserving ID for an unambiguous legacy record."""
    kind = str(kind or "record").strip().lower()
    key = str(legacy_key or "").strip()
    if not key:
        raise FilePolicyError("legacy key is required", code="migration_key_required")
    return f"{kind}-{uuid.uuid5(uuid.NAMESPACE_URL, 'openclank:file-policy:' + kind + ':' + key).hex}"
