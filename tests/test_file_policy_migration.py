from __future__ import annotations

import json
import sqlite3

from src.openclank.file_policy import FilePolicyRepository
from src.openclank.file_policy_migration import (
    apply_legacy_policy_plan,
    plan_legacy_policy_import,
)


ADMIN = "account-admin"
ALICE = "account-alice"


def _write_registry(tmp_path):
    ceiling = tmp_path / "host"
    owned = ceiling / "alice"
    owned.mkdir(parents=True)
    registry = tmp_path / "roots.json"
    registry.write_text(
        json.dumps(
            {
                "version": 1,
                "generation": 7,
                "roots": {
                    "root-admin": {
                        "id": "root-admin",
                        "owner_id": "admin",
                        "kind": "recursive_directory",
                        "canonical_path": str(ceiling),
                        "display_path": "Host",
                        "enabled": True,
                        "capabilities": ["read", "write"],
                        "platform_identity": {"device": 1, "inode": 10},
                        "availability": "available",
                    },
                    "root-alice": {
                        "id": "root-alice",
                        "owner_id": "alice",
                        "kind": "recursive_directory",
                        "canonical_path": str(owned),
                        "display_path": "Alice",
                        "enabled": True,
                        "capabilities": ["read", "write"],
                        "platform_identity": {"device": 1, "inode": 11},
                        "availability": "available",
                    },
                },
                "visibility_assignments": {
                    "visible-alice": {
                        "id": "visible-alice",
                        "root_id": "root-admin",
                        "subject_id": "alice",
                        "subject_kind": "user",
                        "issuer_id": "admin",
                        "enabled": True,
                        "capabilities": ["read"],
                    },
                    "fake-group": {
                        "id": "fake-group",
                        "root_id": "root-admin",
                        "subject_id": "alice",
                        "subject_kind": "group",
                        "issuer_id": "admin",
                        "enabled": True,
                        "capabilities": ["write"],
                    },
                },
            }
        ),
        encoding="utf-8",
    )
    return registry, ceiling, owned


def _write_grants(tmp_path, owned):
    database = tmp_path / "legacy.db"
    connection = sqlite3.connect(database)
    connection.executescript(
        """
        CREATE TABLE permission_grants (
            id INTEGER PRIMARY KEY,
            owner TEXT NOT NULL,
            session_id TEXT NOT NULL DEFAULT '',
            permission_type TEXT NOT NULL,
            pattern TEXT NOT NULL,
            workspace TEXT NOT NULL DEFAULT '',
            resource TEXT NOT NULL DEFAULT '',
            expires_at TEXT,
            revoked_at TEXT
        );
        """
    )
    connection.execute(
        "INSERT INTO permission_grants VALUES (1,'alice','chat-1','trash',?,?,?,NULL,NULL)",
        (str(owned), str(owned), "file-hash"),
    )
    connection.execute(
        "INSERT INTO permission_grants VALUES (2,'alice','','read','*','','',NULL,NULL)"
    )
    connection.execute(
        "INSERT INTO permission_grants VALUES (3,'alice','chat-2','bash',?,'','',NULL,NULL)",
        (str(owned),),
    )
    connection.commit()
    connection.close()
    return database


def test_dry_run_intersects_legacy_caps_and_keeps_ambiguous_rows_out(tmp_path):
    registry, _ceiling, owned = _write_registry(tmp_path)
    grants = _write_grants(tmp_path, owned)
    plan = plan_legacy_policy_import(
        registry_path=registry,
        grant_db_path=grants,
        subject_ids={"admin": ADMIN, "alice": ALICE},
        admin_subject_ids=(ADMIN,),
        raw_workspaces=(
            {"owner": "alice", "path": str(owned), "name": "Alice source", "legacy_key": "browser:alice"},
        ),
    )

    assert plan.source_generation == 7
    assert len(plan.locations) == 2
    assert len(plan.workspaces) == 1
    alice_agents = [
        row for row in plan.bindings
        if row["binding_class"] == "agent" and row["subject_id"] == ALICE
    ]
    assert len(alice_agents) == 1
    assert alice_agents[0]["capabilities"] == ["read"]
    derived_people = [
        row for row in plan.bindings
        if row["binding_class"] == "people"
        and row["subject_id"] == ALICE
        and row["location_id"] == alice_agents[0]["location_id"]
    ]
    assert derived_people and derived_people[0]["capabilities"] == ["read"]

    operations = [row for row in plan.bindings if row["binding_class"] == "operation"]
    assert len(operations) == 1
    assert operations[0]["lifetime"] == "chat"
    assert operations[0]["chat_id"] == "chat-1"
    assert operations[0]["workspace_id"] == plan.workspaces[0]["id"]
    assert operations[0]["resource_ref"].startswith("legacy-resource-")
    reasons = {(row["kind"], row["reason"]) for row in plan.unresolved}
    assert ("visibility", "group_membership_unavailable") in reasons
    assert ("grant", "ambiguous_subject_resource_or_operation") in reasons


def test_apply_is_idempotent_and_new_resolver_never_broadens(tmp_path):
    registry, _ceiling, owned = _write_registry(tmp_path)
    grants = _write_grants(tmp_path, owned)
    plan = plan_legacy_policy_import(
        registry_path=registry,
        grant_db_path=grants,
        subject_ids={"admin": ADMIN, "alice": ALICE},
        admin_subject_ids=(ADMIN,),
        raw_workspaces=(
            {"owner": "alice", "path": str(owned), "name": "Alice source", "legacy_key": "browser:alice"},
        ),
    )
    repository = FilePolicyRepository(tmp_path / "canonical.db")

    first = apply_legacy_policy_plan(repository, plan, create_backup=False)
    generation = first["generation"]
    second = apply_legacy_policy_plan(repository, plan, create_backup=False)
    assert second["generation"] == generation

    workspace = plan.workspaces[0]
    alice_location = next(
        row["location_id"]
        for row in plan.bindings
        if row["binding_class"] == "agent" and row["subject_id"] == ALICE
    )
    read = repository.resolve(
        subject_id=ALICE,
        is_admin=False,
        origin="agent",
        location_id=alice_location,
        workspace_id=workspace["id"],
        capability="read",
    )
    write = repository.resolve(
        subject_id=ALICE,
        is_admin=False,
        origin="agent",
        location_id=alice_location,
        workspace_id=workspace["id"],
        capability="write",
    )
    assert read.allowed
    assert not write.allowed and write.reason == "app_visibility_denied"

    approved = repository.resolve(
        subject_id=ALICE,
        is_admin=False,
        origin="agent",
        location_id=alice_location,
        workspace_id=workspace["id"],
        chat_id="chat-1",
        capability="write",
        required_operation="trash",
        resource_ref=next(
            row["resource_ref"] for row in plan.bindings if row["binding_class"] == "operation"
        ),
    )
    # A destructive approval cannot mint Write through the Read-only People /
    # Agent base scope, even when the legacy grant itself existed.
    assert not approved.allowed
    assert approved.reason == "app_visibility_denied"


def test_unknown_owner_root_is_reported_and_not_imported(tmp_path):
    registry, _ceiling, _owned = _write_registry(tmp_path)
    data = json.loads(registry.read_text(encoding="utf-8"))
    data["roots"]["root-alice"]["owner_id"] = "deleted-user"
    registry.write_text(json.dumps(data), encoding="utf-8")

    plan = plan_legacy_policy_import(
        registry_path=registry,
        subject_ids={"admin": ADMIN},
        admin_subject_ids=(ADMIN,),
    )

    assert any(
        row == {"kind": "root", "legacy_id": "root-alice", "reason": "unknown_owner"}
        for row in plan.unresolved
    )
    assert all("deleted-user" not in str(row) for row in plan.bindings)


def test_stable_workspace_grant_is_never_imported_without_its_identity(tmp_path):
    registry, _ceiling, owned = _write_registry(tmp_path)
    grants = tmp_path / "stable-grants.db"
    with sqlite3.connect(grants) as connection:
        connection.executescript(
            """
            CREATE TABLE permission_grants (
                id INTEGER PRIMARY KEY,
                owner TEXT NOT NULL,
                session_id TEXT NOT NULL DEFAULT '',
                permission_type TEXT NOT NULL,
                pattern TEXT NOT NULL,
                workspace TEXT NOT NULL DEFAULT '',
                workspace_id TEXT NOT NULL DEFAULT '',
                resource TEXT NOT NULL DEFAULT '',
                expires_at TEXT,
                revoked_at TEXT
            );
            """
        )
        connection.execute(
            "INSERT INTO permission_grants VALUES "
            "(1,'alice','chat-1','trash',?,'','workspace-live','hash',NULL,NULL)",
            (str(owned),),
        )
    plan = plan_legacy_policy_import(
        registry_path=registry,
        grant_db_path=grants,
        subject_ids={"admin": ADMIN, "alice": ALICE},
        admin_subject_ids=(ADMIN,),
    )
    assert not any(
        row["binding_class"] == "operation" for row in plan.bindings
    )
    assert {
        "kind": "grant",
        "legacy_id": "1",
        "reason": "stable_workspace_grant_requires_runtime_resolver",
    } in plan.unresolved
