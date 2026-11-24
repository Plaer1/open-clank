from __future__ import annotations

import importlib.util
import sqlite3
from pathlib import Path


MIGRATION = Path(__file__).resolve().parent / "fixtures/provider_actor_execution_scope.py"
SPEC = importlib.util.spec_from_file_location("provider_actor_scope_migration", MIGRATION)
assert SPEC and SPEC.loader
module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(module)


def _legacy_database(path: Path) -> None:
    db = sqlite3.connect(path)
    db.execute("PRAGMA foreign_keys=ON")
    db.executescript(
        """
        CREATE TABLE provider_connections (id TEXT PRIMARY KEY);
        CREATE TABLE provider_accounts (id TEXT PRIMARY KEY, credential_envelope BLOB, credential_version INTEGER);
        CREATE TABLE provider_operation_bindings (
          id TEXT PRIMARY KEY, owner TEXT NOT NULL, credential_owner TEXT NOT NULL,
          root_operation_id TEXT NOT NULL, connection_id TEXT NOT NULL REFERENCES provider_connections(id) ON DELETE CASCADE,
          provider_id TEXT NOT NULL, billing_lane TEXT NOT NULL, model_id TEXT NOT NULL,
          grant_id TEXT, selected_account_id TEXT REFERENCES provider_accounts(id) ON DELETE RESTRICT,
          credential_revision INTEGER, preferred_account_id TEXT, source TEXT NOT NULL,
          attempt INTEGER NOT NULL, state TEXT NOT NULL, committed_at DATETIME,
          fallback_reason TEXT, attempts JSON NOT NULL, policy_snapshot JSON,
          revision INTEGER NOT NULL, created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL,
          UNIQUE(owner, root_operation_id, connection_id, billing_lane)
        );
        CREATE TABLE provider_credential_leases (
          lease_id_digest TEXT PRIMARY KEY, owner TEXT NOT NULL, credential_owner TEXT NOT NULL,
          holder_id TEXT NOT NULL, root_operation_id TEXT NOT NULL,
          binding_id TEXT NOT NULL REFERENCES provider_operation_bindings(id) ON DELETE CASCADE,
          connection_id TEXT NOT NULL REFERENCES provider_connections(id) ON DELETE CASCADE,
          account_id TEXT NOT NULL REFERENCES provider_accounts(id) ON DELETE CASCADE,
          model_id TEXT NOT NULL, grant_id TEXT, credential_revision INTEGER NOT NULL,
          issued_at DATETIME NOT NULL, expires_at DATETIME NOT NULL
        );
        CREATE INDEX ix_provider_operation_binding_root ON provider_operation_bindings(owner, root_operation_id);
        CREATE INDEX ix_provider_operation_binding_account ON provider_operation_bindings(selected_account_id, created_at);
        CREATE INDEX ix_provider_credential_leases_binding_id ON provider_credential_leases(binding_id);
        INSERT INTO provider_connections VALUES ('conn-1');
        INSERT INTO provider_accounts VALUES ('acct-1', X'7365616C65642D626C6F62', 7);
        INSERT INTO provider_operation_bindings VALUES (
          'binding-1','alice','alice','root-1','conn-1','openai','metered_api','gpt-test',
          NULL,'acct-1',7,NULL,'round_robin',1,'uncommitted',NULL,NULL,'[]','{"version":1}',3,
          '2026-10-28 00:00:00','2026-10-28 00:00:01'
        );
        INSERT INTO provider_credential_leases VALUES (
          'lease-digest','alice','alice','worker-1','root-1','binding-1','conn-1','acct-1',
          'gpt-test',NULL,7,'2026-10-28 00:00:00','2026-10-28 00:00:30'
        );
        """
    )
    db.commit()
    db.close()


def test_actor_scope_rebuild_preserves_rows_credential_bytes_and_foreign_keys(tmp_path: Path):
    database = tmp_path / "copied.sqlite3"
    _legacy_database(database)
    before = sqlite3.connect(database)
    account_before = before.execute(
        "SELECT id, credential_envelope, credential_version FROM provider_accounts"
    ).fetchall()
    binding_before = before.execute(
        "SELECT id, owner, root_operation_id, connection_id, model_id, revision, policy_snapshot "
        "FROM provider_operation_bindings"
    ).fetchall()
    lease_before = before.execute(
        "SELECT lease_id_digest, binding_id, account_id, credential_revision FROM provider_credential_leases"
    ).fetchall()
    before.close()

    counts, changed, backup = module._apply(database, tmp_path / "backup")

    assert backup.is_file()
    assert counts["provider_operation_bindings"] == 1
    assert "actor_scope_key" in changed
    after = sqlite3.connect(database)
    assert after.execute(
        "SELECT id, credential_envelope, credential_version FROM provider_accounts"
    ).fetchall() == account_before
    assert after.execute(
        "SELECT id, owner, root_operation_id, connection_id, model_id, revision, policy_snapshot "
        "FROM provider_operation_bindings"
    ).fetchall() == binding_before
    row = after.execute(
        "SELECT actor_scope_key, engine_session_id, actor_id, execution_revision, operation_id, model_route_id "
        "FROM provider_operation_bindings WHERE id='binding-1'"
    ).fetchone()
    assert row == ("legacy", None, None, None, None, None)
    assert after.execute(
        "SELECT lease_id_digest, binding_id, account_id, credential_revision FROM provider_credential_leases"
    ).fetchall() == lease_before
    assert after.execute("PRAGMA foreign_key_check").fetchall() == []
    after.close()
