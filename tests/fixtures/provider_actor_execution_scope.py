"""Test fixture for the historical actor-scoped provider binding table rebuild.

Exercises the original SQLite conversion against a caller-supplied synthetic database.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path


TABLE = "provider_operation_bindings"
BASE_COLUMNS = (
    "id", "owner", "credential_owner", "root_operation_id", "connection_id",
    "provider_id", "billing_lane", "model_id", "grant_id", "selected_account_id",
    "credential_revision", "preferred_account_id", "source", "attempt", "state",
    "committed_at", "fallback_reason", "attempts", "revision", "created_at", "updated_at",
)
NEW_COLUMNS = {
    "actor_scope_key", "engine_session_id", "actor_id", "execution_revision",
    "operation_id", "model_route_id",
}
INDEX_SQL = (
    'CREATE UNIQUE INDEX uq_provider_operation_binding_affinity ON provider_operation_bindings '
    '(owner, root_operation_id, actor_scope_key, connection_id, billing_lane, model_id)',
    'CREATE INDEX ix_provider_operation_binding_root ON provider_operation_bindings (owner, root_operation_id)',
    'CREATE INDEX ix_provider_operation_binding_account ON provider_operation_bindings (selected_account_id, created_at)',
)


def _columns(db: sqlite3.Connection) -> set[str]:
    return {str(row[1]) for row in db.execute(f'PRAGMA table_info("{TABLE}")')}


def _has_actor_affinity_index(db: sqlite3.Connection) -> bool:
    for row in db.execute(f'PRAGMA index_list("{TABLE}")'):
        if not bool(row[2]):
            continue
        names = [str(item[2]) for item in db.execute(f'PRAGMA index_info("{row[1]}")')]
        if names == ["owner", "root_operation_id", "actor_scope_key", "connection_id", "billing_lane", "model_id"]:
            return True
    return False


def _counts(db: sqlite3.Connection) -> dict[str, int]:
    return {
        name: int(db.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0])
        for name in ("provider_connections", "provider_accounts", TABLE, "provider_credential_leases")
    }


def _inspect(path: Path) -> tuple[dict[str, int], list[str]]:
    if not path.is_file() or path.is_symlink():
        raise ValueError("database must be an existing regular copied SQLite file")
    db = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    try:
        tables = {str(row[0]) for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        required = {"provider_connections", "provider_accounts", TABLE, "provider_credential_leases"}
        if not required.issubset(tables):
            raise ValueError("database is missing required provider tables")
        missing = sorted(set(BASE_COLUMNS) - _columns(db))
        if missing:
            raise ValueError("provider binding table is missing baseline columns: " + ", ".join(missing))
        missing = sorted(NEW_COLUMNS - _columns(db))
        if not _has_actor_affinity_index(db):
            missing.append("actor-scoped affinity index")
        return _counts(db), missing
    finally:
        db.close()


def _backup(path: Path, backup_dir: Path) -> Path:
    if backup_dir.exists():
        raise ValueError("backup directory must be new and not already exist")
    backup_dir.mkdir(parents=True)
    backup_path = backup_dir / "provider-actor-execution-preimage.sqlite3"
    source = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    target = sqlite3.connect(backup_path)
    try:
        source.backup(target)
        if target.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ValueError("database backup failed integrity verification")
    finally:
        target.close()
        source.close()
    return backup_path


def _create_table(db: sqlite3.Connection) -> None:
    db.execute(f'''CREATE TABLE "{TABLE}_actor_scope_new" (
        id VARCHAR NOT NULL PRIMARY KEY,
        owner VARCHAR NOT NULL,
        credential_owner VARCHAR NOT NULL,
        root_operation_id VARCHAR NOT NULL,
        actor_scope_key VARCHAR NOT NULL DEFAULT 'legacy',
        engine_session_id VARCHAR,
        actor_id VARCHAR,
        execution_revision INTEGER,
        operation_id VARCHAR,
        connection_id VARCHAR NOT NULL REFERENCES provider_connections(id) ON DELETE CASCADE,
        provider_id VARCHAR NOT NULL,
        billing_lane VARCHAR NOT NULL,
        model_id VARCHAR NOT NULL,
        model_route_id VARCHAR,
        grant_id VARCHAR,
        selected_account_id VARCHAR REFERENCES provider_accounts(id) ON DELETE RESTRICT,
        credential_revision INTEGER,
        preferred_account_id VARCHAR,
        source VARCHAR NOT NULL,
        attempt INTEGER NOT NULL,
        state VARCHAR NOT NULL,
        committed_at DATETIME,
        fallback_reason VARCHAR,
        attempts JSON NOT NULL,
        policy_snapshot JSON,
        revision INTEGER NOT NULL,
        created_at DATETIME NOT NULL,
        updated_at DATETIME NOT NULL
    )''')


def _apply(path: Path, backup_dir: Path) -> tuple[dict[str, int], list[str], Path]:
    before, missing = _inspect(path)
    backup = _backup(path, backup_dir)
    db = sqlite3.connect(path, timeout=30, isolation_level=None)
    try:
        db.execute("PRAGMA foreign_keys=OFF")
        db.execute("BEGIN IMMEDIATE")
        existing = _columns(db)
        if NEW_COLUMNS.issubset(existing) and _has_actor_affinity_index(db):
            db.rollback()
            return before, [], backup
        if NEW_COLUMNS & existing:
            raise ValueError("partially migrated provider bindings require manual review; refusing to discard scope data")
        _create_table(db)
        source_columns = list(BASE_COLUMNS)
        if "policy_snapshot" in existing:
            source_columns.append("policy_snapshot")
        else:
            source_columns.append("NULL AS policy_snapshot")
        target_columns = list(BASE_COLUMNS) + ["policy_snapshot"]
        db.execute(
            f'INSERT INTO "{TABLE}_actor_scope_new" '
            f'({", ".join(target_columns)}) '
            f'SELECT {", ".join(source_columns)} FROM "{TABLE}"'
        )
        db.execute(f'DROP TABLE "{TABLE}"')
        db.execute(f'ALTER TABLE "{TABLE}_actor_scope_new" RENAME TO "{TABLE}"')
        for statement in INDEX_SQL:
            db.execute(statement)
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()

    check = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    try:
        after = _counts(check)
        missing_after = NEW_COLUMNS - _columns(check)
        fk_errors = list(check.execute("PRAGMA foreign_key_check"))
        integrity = check.execute("PRAGMA integrity_check").fetchone()[0]
        if before != after or missing_after or not _has_actor_affinity_index(check) or fk_errors or integrity != "ok":
            raise ValueError("provider actor scope migration verification failed; restore the backup")
    finally:
        check.close()
    return before, sorted(NEW_COLUMNS), backup
