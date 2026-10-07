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


class FilePolicyRepository(CanonicalCompatibility):
    """SQLite repository and monotonic resolver for canonical file authority."""

    def __init__(self, db_path: str | os.PathLike[str] | None = None) -> None:
        self.db_path = str(db_path or os.environ.get("OPEN_CLANK_AUTHORITY_DB_PATH") or APP_DB)
        path = Path(self.db_path).expanduser().resolve()
        existing = False
        # Admission is read-only: even switching SQLite journal mode is a
        # mutation and must wait until the store passes the current contract.
        if path.is_file() and path.stat().st_size:
            with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, factory=_ClosingConnection) as admission:
                existing = bool(admission.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='file_policy_meta'").fetchone())
                if not existing and admission.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='permission_grants'").fetchone():
                    raise FilePolicyError("Files permission store does not match this release. Stop writers and keep a complete backup; restore the matching release or prepare an offline conversion.", code="policy_store_unavailable")
                if existing:
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
            connection.execute("SELECT lifecycle_phase FROM file_operations LIMIT 0")
            version = int(connection.execute(
                "SELECT schema_version FROM file_policy_meta WHERE singleton=1"
            ).fetchone()[0])
            if version != SCHEMA_VERSION:
                raise FilePolicyError("unsupported file-policy schema", code="policy_store_unavailable")

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
                    incoming = dict(receipt)
                    incoming["lease_owner"] = current_owner
                    duration = max(1_000, min(int(current.get("lease_duration_ms") or 30_000), 120_000))
                    incoming["lease_duration_ms"] = duration
                    incoming["lease_expires_unix_ms"] = _now_ms() + duration
                    payload = encrypt(_json(incoming))
                connection.execute(
                    "UPDATE file_operations SET generation=?,receipt_ciphertext=?,lifecycle_phase=COALESCE(?, lifecycle_phase) WHERE owner_subject_id=? AND operation_id=? AND request_digest=?",
                    (int(generation), payload, str(phase) if phase is not None else None, str(owner_subject_id), str(operation_id), str(request_digest)),
                )

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
    ) -> EffectiveScope:
        """Resolve and, when needed, atomically consume one Once approval."""
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
        self.expire_bindings()
        now = _now_ms()
        with self._connect() as connection:
            self._begin(connection)
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
