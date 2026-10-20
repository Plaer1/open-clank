from __future__ import annotations

import json
import sqlite3

import pytest

from src.openclank.provider_migration import (
    MigrationJournal,
    ProviderMigrationError,
    provider_migration_preflight,
)
from src.secret_storage import encrypt


def _fixture(tmp_path, *, admins=("alice",), owner=None, broken_share=False):
    database = tmp_path / "app.db"
    db = sqlite3.connect(database)
    db.executescript(
        """
        CREATE TABLE model_endpoints (
            id TEXT PRIMARY KEY, owner TEXT, api_key TEXT, is_enabled INTEGER
        );
        CREATE TABLE provider_auth_sessions (
            id TEXT PRIMARY KEY, owner TEXT, access_token TEXT, refresh_token TEXT
        );
        CREATE TABLE mimo_auth_store (owner TEXT PRIMARY KEY, payload TEXT);
        CREATE TABLE model_shares (
            id TEXT PRIMARY KEY, owner TEXT, source_kind TEXT, source_id TEXT,
            active INTEGER
        );
        CREATE TABLE model_share_subscriptions (
            share_id TEXT, subscriber TEXT, enabled INTEGER
        );
        CREATE TABLE scheduled_tasks (
            id TEXT, status TEXT, task_type TEXT, endpoint_id TEXT
        );
        CREATE TABLE sessions (id TEXT, endpoint_id TEXT);
        CREATE TABLE crew_members (id TEXT, endpoint_id TEXT);
        """
    )
    db.execute(
        "INSERT INTO model_endpoints VALUES (?,?,?,1)",
        ("endpoint-1", owner, encrypt("secret")),
    )
    if broken_share:
        db.execute(
            "INSERT INTO model_shares VALUES ('share-1','alice','endpoint','missing',1)"
        )
        db.execute(
            "INSERT INTO model_share_subscriptions VALUES ('share-1','bob',1)"
        )
    db.commit()
    db.close()
    auth = tmp_path / "auth.json"
    auth.write_text(
        json.dumps(
            {
                "users": {
                    name: {"is_admin": True}
                    for name in admins
                }
            }
        )
    )
    return database, auth


def test_single_admin_deterministically_claims_ownerless_credential(tmp_path):
    database, auth = _fixture(tmp_path)
    result = provider_migration_preflight(
        database_path=database,
        auth_file=auth,
        environment={"OPENAI_API_KEY": "do-not-report-this"},
    )
    assert result.ready is True
    assert result.owner_assignment == "alice"
    report = json.dumps(result.safe_report())
    assert "OPENAI_API_KEY" in report
    assert "do-not-report-this" not in report
    assert "secret" not in report


def test_multiple_admins_require_explicit_owner(tmp_path):
    database, auth = _fixture(tmp_path, admins=("alice", "bob"))
    blocked = provider_migration_preflight(database_path=database, auth_file=auth)
    assert blocked.ready is False
    assert "--owner USER" in blocked.blockers[0]
    ready = provider_migration_preflight(
        database_path=database,
        auth_file=auth,
        explicit_owner="bob",
    )
    assert ready.ready is True and ready.owner_assignment == "bob"


def test_auth_disabled_uses_reserved_local_owner_for_ownerless_authority(tmp_path):
    database, auth = _fixture(tmp_path, admins=())
    auth.write_text("not relevant while auth is disabled", encoding="utf-8")
    result = provider_migration_preflight(
        database_path=database,
        auth_file=auth,
        auth_enabled=False,
    )
    assert result.ready is True
    assert result.owner_assignment == "local-installation"


def test_global_defaults_require_the_same_deterministic_owner_assignment(tmp_path):
    database, auth = _fixture(tmp_path, admins=("alice",), owner="alice")
    settings = tmp_path / "settings.json"
    settings.write_text(
        json.dumps(
            {
                "task_endpoint_id": "endpoint-1",
                "task_model": "model-test",
            }
        ),
        encoding="utf-8",
    )
    result = provider_migration_preflight(
        database_path=database,
        auth_file=auth,
        settings_path=settings,
    )
    assert result.ready is True
    assert result.owner_assignment == "alice"

    ambiguous = tmp_path / "ambiguous"
    ambiguous.mkdir()
    database, auth = _fixture(
        ambiguous,
        admins=("alice", "bob"),
        owner="alice",
    )
    settings = database.parent / "settings.json"
    settings.write_text(
        json.dumps({"default_endpoint_id": "endpoint-1", "default_model": "m"}),
        encoding="utf-8",
    )
    blocked = provider_migration_preflight(
        database_path=database,
        auth_file=auth,
        settings_path=settings,
    )
    assert blocked.ready is False
    assert any("--owner USER" in item for item in blocked.blockers)


def test_malformed_encrypted_and_json_credentials_block_without_secret_echo(tmp_path):
    database, auth = _fixture(tmp_path, owner="alice")
    db = sqlite3.connect(database)
    db.execute(
        "UPDATE model_endpoints SET api_key='enc:not-a-valid-fernet-token' "
        "WHERE id='endpoint-1'"
    )
    db.execute(
        "INSERT INTO mimo_auth_store VALUES (?, ?)",
        ("alice", encrypt("not-json-native-secret")),
    )
    db.commit()
    db.close()

    result = provider_migration_preflight(database_path=database, auth_file=auth)
    assert result.ready is False
    assert result.inventory.malformed_credentials == 2
    report = json.dumps(result.safe_report())
    assert "not-a-valid-fernet-token" not in report
    assert "not-json-native-secret" not in report
    assert any("cannot be decrypted or parsed" in item for item in result.blockers)


def test_explicit_owner_must_be_an_admin_when_auth_is_enabled(tmp_path):
    database, auth = _fixture(tmp_path, owner="alice")
    result = provider_migration_preflight(
        database_path=database,
        auth_file=auth,
        explicit_owner="mallory",
    )
    assert result.ready is False
    assert any("not an Open Clank admin" in item for item in result.blockers)


def test_unresolved_accepted_share_blocks_cutover(tmp_path):
    database, auth = _fixture(tmp_path, owner="alice", broken_share=True)
    result = provider_migration_preflight(database_path=database, auth_file=auth)
    assert result.ready is False
    assert any("accepted share" in item for item in result.blockers)


def test_journal_transitions_are_monotonic(tmp_path):
    journal = MigrationJournal(tmp_path / "migration.json")
    journal.advance("preflight", counts={"connections": 1})
    journal.advance("workers_drained")
    with pytest.raises(ProviderMigrationError, match="transition"):
        journal.advance("transaction_committed")
    assert journal.read()["phase"] == "workers_drained"
