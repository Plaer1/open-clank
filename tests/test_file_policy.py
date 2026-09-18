from __future__ import annotations

import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from src.openclank.file_policy import (
    FilePolicyError,
    FilePolicyRepository,
    deterministic_legacy_id,
)
from src.openclank.filesystem_registry import FilesystemRootRegistry


ADMIN = "account-admin"
ALICE = "account-alice"
BOB = "account-bob"


def _repository(tmp_path) -> FilePolicyRepository:
    return FilePolicyRepository(tmp_path / "policy.db")


def _location(repository: FilePolicyRepository, tmp_path, name="root", capabilities=("read", "write")):
    path = tmp_path / name
    path.mkdir()
    return repository.create_location(
        actor_subject_id=ADMIN,
        path=str(path),
        kind="directory",
        capabilities=capabilities,
        platform_identity={"device": 1, "inode": name},
    )


def test_location_identity_is_deduplicated_but_creates_no_authority(tmp_path):
    repository = _repository(tmp_path)
    location = _location(repository, tmp_path)
    duplicate = repository.create_location(
        actor_subject_id=ADMIN,
        path=location.canonical_path,
        kind="directory",
        capabilities=("read", "write"),
    )

    assert duplicate.id == location.id
    assert len(repository.list_locations()) == 1
    assert repository.resolve(
        subject_id=ALICE,
        is_admin=False,
        origin="app",
        location_id=location.id,
        capability="read",
    ).reason == "app_visibility_denied"
    assert repository.resolve(
        subject_id=ADMIN,
        is_admin=True,
        origin="agent",
        location_id=location.id,
        capability="read",
    ).reason == "agent_binding_denied"


def test_operation_phase_filter_reaches_pending_after_large_terminal_history(tmp_path):
    repository = _repository(tmp_path)
    for index in range(2050):
        repository.record_operation(
            owner_subject_id=ALICE,
            operation_id=f"__copal_attachment_lifecycle__terminal-{index}",
            request_digest=f"digest-terminal-{index}",
            generation=1,
            receipt={"action_id": f"terminal-{index}", "phase": "reaped"},
            phase="reaped",
        )
    repository.record_operation(
        owner_subject_id=ALICE,
        operation_id="__copal_attachment_lifecycle__pending",
        request_digest="digest-pending",
        generation=2,
        receipt={"action_id": "pending", "phase": "pending"},
        phase="pending",
    )

    pending = repository.list_operations(
        owner_subject_id=ALICE,
        operation_prefix="__copal_attachment_lifecycle__",
        phase="pending",
        limit=8,
    )
    assert [row["operation_id"] for row in pending] == ["__copal_attachment_lifecycle__pending"]


def test_location_removal_revokes_all_authority_and_restore_never_resurrects_it(tmp_path):
    repository = _repository(tmp_path)
    location = _location(repository, tmp_path)
    workspace = repository.create_workspace(
        actor_subject_id=ADMIN,
        owner_subject_id=ALICE,
        location_id=location.id,
        name="Source",
    )
    people = repository.create_binding(
        actor_subject_id=ADMIN,
        binding_class="people",
        subject_id=ALICE,
        location_id=location.id,
        capabilities=("read", "write"),
    )
    agent = repository.create_binding(
        actor_subject_id=ALICE,
        binding_class="agent",
        subject_id=ALICE,
        location_id=location.id,
        workspace_id=workspace.id,
        lifetime="workspace",
        capabilities=("read",),
    )
    approval = repository.create_binding(
        actor_subject_id=ALICE,
        binding_class="operation",
        subject_id=ALICE,
        location_id=location.id,
        workspace_id=workspace.id,
        lifetime="workspace",
        resource_ref="resource-1",
        operation="trash",
        capabilities=("write",),
    )

    before = repository.generation()
    removed = repository.disable_location(
        location.id,
        actor_subject_id=ADMIN,
        reason_code="test_location_removal",
    )

    assert removed["generation"] == before + 1
    assert removed["bindings_revoked"] == 3
    assert removed["workspaces_archived"] == 1
    assert repository.get_location(location.id).enabled is False
    assert repository.get_workspace(workspace.id).archived is True
    assert repository.get_binding(people.id).status == "revoked"
    assert repository.get_binding(agent.id).status == "revoked"
    assert repository.get_binding(approval.id).status == "revoked"

    unchanged = repository.disable_location(location.id, actor_subject_id=ADMIN)
    assert unchanged["generation"] == removed["generation"]
    assert unchanged["bindings_revoked"] == 0
    assert unchanged["workspaces_archived"] == 0

    restored = repository.restore_location(
        location.id,
        actor_subject_id=ADMIN,
        capabilities=("read",),
        display_path=location.display_path,
        platform_identity={"device": 9, "inode": "replacement"},
    )
    assert restored.enabled is True
    assert restored.capabilities == ("read",)
    assert restored.platform_identity == {"device": 9, "inode": "replacement"}
    assert repository.get_workspace(workspace.id).archived is True
    assert repository.get_binding(people.id).status == "revoked"
    assert repository.get_binding(agent.id).status == "revoked"
    assert repository.get_binding(approval.id).status == "revoked"
    removal = next(
        event for event in repository.audit_events()
        if event["target_id"] == location.id and event["event"] == "remove"
    )
    assert removal["details"] == {"bindings_revoked": 3, "workspaces_archived": 1}


def test_admin_app_host_ceiling_does_not_enter_agent_scope(tmp_path):
    repository = _repository(tmp_path)
    location = _location(repository, tmp_path)

    app = repository.resolve(
        subject_id=ADMIN,
        is_admin=True,
        origin="app",
        location_id=location.id,
        capability="write",
    )
    assert app.allowed and set(app.capabilities) == {"read", "write"}

    denied = repository.resolve(
        subject_id=ADMIN,
        is_admin=True,
        origin="agent",
        location_id=location.id,
        capability="write",
    )
    assert not denied.allowed and denied.reason == "agent_binding_denied"

    repository.create_binding(
        actor_subject_id=ADMIN,
        binding_class="agent",
        subject_id=ADMIN,
        location_id=location.id,
        capabilities=("read",),
    )
    assert repository.resolve(
        subject_id=ADMIN,
        is_admin=True,
        origin="agent",
        location_id=location.id,
        capability="read",
    ).allowed
    assert not repository.resolve(
        subject_id=ADMIN,
        is_admin=True,
        origin="agent",
        location_id=location.id,
        capability="write",
    ).allowed


def test_nonadmin_agent_is_exact_people_intersection_per_location(tmp_path):
    repository = _repository(tmp_path)
    location_a = _location(repository, tmp_path, "a")
    location_b = _location(repository, tmp_path, "b")
    people_a = repository.create_binding(
        actor_subject_id=ADMIN,
        binding_class="people",
        subject_id=ALICE,
        location_id=location_a.id,
        capabilities=("read",),
    )
    repository.create_binding(
        actor_subject_id=ADMIN,
        binding_class="people",
        subject_id=ALICE,
        location_id=location_b.id,
        capabilities=("write",),
    )
    repository.create_binding(
        actor_subject_id=ADMIN,
        binding_class="agent",
        subject_id=ALICE,
        location_id=location_a.id,
        capabilities=("read", "write"),
    )
    repository.create_binding(
        actor_subject_id=ADMIN,
        binding_class="agent",
        subject_id=ALICE,
        location_id=location_b.id,
        capabilities=("read", "write"),
    )

    assert repository.resolve(
        subject_id=ALICE,
        is_admin=False,
        origin="agent",
        location_id=location_a.id,
        capability="read",
    ).allowed
    assert repository.resolve(
        subject_id=ALICE,
        is_admin=False,
        origin="agent",
        location_id=location_a.id,
        capability="write",
    ).reason == "app_visibility_denied"
    assert repository.resolve(
        subject_id=ALICE,
        is_admin=False,
        origin="agent",
        location_id=location_b.id,
        capability="write",
    ).allowed
    assert repository.resolve(
        subject_id=ALICE,
        is_admin=False,
        origin="agent",
        location_id=location_b.id,
        capability="read",
    ).reason == "app_visibility_denied"

    before = repository.generation()
    narrowed = repository.update_binding(
        people_a.id,
        actor_subject_id=ADMIN,
        capabilities=("read",),
    )
    assert narrowed.generation > before


def test_people_downgrade_immediately_narrows_existing_agent_binding(tmp_path):
    repository = _repository(tmp_path)
    location = _location(repository, tmp_path)
    people = repository.create_binding(
        actor_subject_id=ADMIN,
        binding_class="people",
        subject_id=ALICE,
        location_id=location.id,
        capabilities=("read", "write"),
    )
    repository.create_binding(
        actor_subject_id=ADMIN,
        binding_class="agent",
        subject_id=ALICE,
        location_id=location.id,
        capabilities=("read", "write"),
    )
    initial = repository.resolve(
        subject_id=ALICE,
        is_admin=False,
        origin="agent",
        location_id=location.id,
        capability="write",
    )
    assert initial.allowed

    repository.update_binding(people.id, actor_subject_id=ADMIN, capabilities=("read",))
    narrowed = repository.resolve(
        subject_id=ALICE,
        is_admin=False,
        origin="agent",
        location_id=location.id,
        capability="write",
    )
    assert not narrowed.allowed
    assert narrowed.reason == "app_visibility_denied"
    assert narrowed.policy_generation > initial.policy_generation


def test_workspace_is_stable_identity_and_only_narrows(tmp_path):
    repository = _repository(tmp_path)
    location = _location(repository, tmp_path)
    workspace = repository.create_workspace(
        actor_subject_id=ADMIN,
        owner_subject_id=ALICE,
        location_id=location.id,
        name="Source",
        relative_folder="projects/open-clank",
    )
    repository.create_binding(
        actor_subject_id=ADMIN,
        binding_class="people",
        subject_id=ALICE,
        location_id=location.id,
        capabilities=("read",),
    )
    repository.create_binding(
        actor_subject_id=ADMIN,
        binding_class="agent",
        subject_id=ALICE,
        location_id=location.id,
        workspace_id=workspace.id,
        lifetime="workspace",
        capabilities=("read",),
    )

    inside = repository.resolve(
        subject_id=ALICE,
        is_admin=False,
        origin="agent",
        location_id=location.id,
        workspace_id=workspace.id,
        relative_resource="projects/open-clank/src/app.py",
        capability="read",
    )
    assert inside.allowed
    outside = repository.resolve(
        subject_id=ALICE,
        is_admin=False,
        origin="agent",
        location_id=location.id,
        workspace_id=workspace.id,
        relative_resource="projects/sibling/secret.txt",
        capability="read",
    )
    assert not outside.allowed and outside.reason == "outside_workspace"
    forged = repository.resolve(
        subject_id=BOB,
        is_admin=False,
        origin="agent",
        location_id=location.id,
        workspace_id=workspace.id,
        relative_resource="projects/open-clank/src/app.py",
        capability="read",
    )
    assert not forged.allowed and forged.reason == "workspace_unavailable"


def test_once_operation_approval_is_consumed_by_exactly_one_concurrent_call(tmp_path):
    repository = _repository(tmp_path)
    location = _location(repository, tmp_path)
    repository.create_binding(
        actor_subject_id=ADMIN,
        binding_class="people",
        subject_id=ALICE,
        location_id=location.id,
        capabilities=("write",),
    )
    repository.create_binding(
        actor_subject_id=ADMIN,
        binding_class="agent",
        subject_id=ALICE,
        location_id=location.id,
        capabilities=("write",),
    )
    approval = repository.create_binding(
        actor_subject_id=ALICE,
        binding_class="operation",
        subject_id=ALICE,
        location_id=location.id,
        resource_ref="resource-1",
        operation="trash",
        lifetime="once",
        capabilities=("write",),
    )
    barrier = threading.Barrier(8)

    def resolve_once():
        barrier.wait()
        return FilePolicyRepository(repository.db_path).resolve(
            subject_id=ALICE,
            is_admin=False,
            origin="agent",
            location_id=location.id,
            capability="write",
            required_operation="trash",
            resource_ref="resource-1",
        )

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: resolve_once(), range(8)))

    allowed = [result for result in results if result.allowed]
    assert len(allowed) == 1
    assert allowed[0].consumed_binding_id == approval.id
    assert repository.get_binding(approval.id).status == "consumed"


def test_group_label_never_matches_username_without_server_membership(tmp_path):
    repository = _repository(tmp_path)
    location = _location(repository, tmp_path)
    repository.create_binding(
        actor_subject_id=ADMIN,
        binding_class="people",
        subject_kind="group",
        subject_id=ALICE,
        location_id=location.id,
        capabilities=("read",),
    )
    denied = repository.resolve(
        subject_id=ALICE,
        is_admin=False,
        origin="app",
        location_id=location.id,
        capability="read",
    )
    assert not denied.allowed
    allowed = repository.resolve(
        subject_id=BOB,
        is_admin=False,
        origin="app",
        location_id=location.id,
        capability="read",
        group_ids=(ALICE,),
    )
    assert allowed.allowed


def test_invalid_lifetime_shapes_and_capability_escalation_fail_closed(tmp_path):
    repository = _repository(tmp_path)
    location = _location(repository, tmp_path, capabilities=("read",))
    with pytest.raises(FilePolicyError) as exc:
        repository.create_binding(
            actor_subject_id=ADMIN,
            binding_class="people",
            subject_id=ALICE,
            location_id=location.id,
            lifetime="always",
            chat_id="chat-1",
            capabilities=("read",),
        )
    assert exc.value.code == "invalid_lifetime_scope"
    with pytest.raises(FilePolicyError) as exc:
        repository.create_binding(
            actor_subject_id=ADMIN,
            binding_class="agent",
            subject_id=ALICE,
            location_id=location.id,
            lifetime="once",
            capabilities=("read",),
        )
    assert exc.value.code == "invalid_lifetime"
    with pytest.raises(FilePolicyError) as exc:
        repository.create_binding(
            actor_subject_id=ADMIN,
            binding_class="people",
            subject_id=ALICE,
            location_id=location.id,
            capabilities=("write",),
        )
    assert exc.value.code == "capability_escalation"


def test_scoped_agent_resets_are_atomic_and_never_remove_people_access(tmp_path):
    repository = _repository(tmp_path)
    location = _location(repository, tmp_path)
    workspace = repository.create_workspace(
        actor_subject_id=ADMIN,
        owner_subject_id=ALICE,
        location_id=location.id,
        name="Source",
    )
    people = repository.create_binding(
        actor_subject_id=ADMIN,
        binding_class="people",
        subject_id=ALICE,
        location_id=location.id,
        capabilities=("read", "write"),
    )
    chat_agent = repository.create_binding(
        actor_subject_id=ALICE,
        binding_class="agent",
        subject_id=ALICE,
        location_id=location.id,
        workspace_id=workspace.id,
        chat_id="chat-1",
        lifetime="chat",
        capabilities=("read",),
    )
    other_chat = repository.create_binding(
        actor_subject_id=ALICE,
        binding_class="agent",
        subject_id=ALICE,
        location_id=location.id,
        workspace_id=workspace.id,
        chat_id="chat-2",
        lifetime="chat",
        capabilities=("read",),
    )
    workspace_approval = repository.create_binding(
        actor_subject_id=ALICE,
        binding_class="operation",
        subject_id=ALICE,
        location_id=location.id,
        workspace_id=workspace.id,
        lifetime="workspace",
        resource_ref="resource-workspace",
        operation="trash",
        capabilities=("write",),
    )
    always_agent = repository.create_binding(
        actor_subject_id=ALICE,
        binding_class="agent",
        subject_id=ALICE,
        location_id=location.id,
        lifetime="always",
        capabilities=("write",),
    )
    bob_agent = repository.create_binding(
        actor_subject_id=BOB,
        binding_class="agent",
        subject_id=BOB,
        location_id=location.id,
        lifetime="always",
        capabilities=("read",),
    )

    generation = repository.generation()
    preview = repository.preview_agent_reset(
        subject_id=ALICE,
        scope="chat",
        chat_id="chat-1",
    )
    assert preview == {
        "scope": "chat",
        "matched": 1,
        "by_class": {"agent": 1, "operation": 0},
        "by_lifetime": {"always": 0, "chat": 1, "once": 0, "workspace": 0},
        "people_preserved": True,
        "generation": generation,
    }
    assert repository.generation() == generation

    reset_chat = repository.reset_agent_permissions(
        actor_subject_id=ALICE,
        subject_id=ALICE,
        scope="chat",
        chat_id="chat-1",
    )
    assert reset_chat["matched"] == 1
    assert repository.get_binding(chat_agent.id).status == "revoked"
    assert repository.get_binding(other_chat.id).status == "active"
    assert repository.get_binding(workspace_approval.id).status == "active"
    assert repository.get_binding(people.id).status == "active"

    reset_workspace = repository.reset_agent_permissions(
        actor_subject_id=ALICE,
        subject_id=ALICE,
        scope="workspace",
        workspace_id=workspace.id,
    )
    assert reset_workspace["matched"] == 2
    assert reset_workspace["by_class"] == {"agent": 1, "operation": 1}
    assert repository.get_binding(other_chat.id).status == "revoked"
    assert repository.get_binding(workspace_approval.id).status == "revoked"
    assert repository.get_binding(always_agent.id).status == "active"

    reset_all = repository.reset_agent_permissions(
        actor_subject_id=ALICE,
        subject_id=ALICE,
        scope="all_agent",
    )
    assert reset_all["matched"] == 1
    assert repository.get_binding(always_agent.id).status == "revoked"
    assert repository.get_binding(people.id).status == "active"
    assert repository.get_binding(bob_agent.id).status == "active"
    events = [event for event in repository.audit_events() if event["event"] == "reset"]
    assert [event["details"]["scope"] for event in events] == ["chat", "workspace", "all_agent"]
    assert all(event["details"]["people_preserved"] is True for event in events)


def test_agent_reset_validates_scope_and_noop_does_not_bump_generation(tmp_path):
    repository = _repository(tmp_path)
    before = repository.generation()
    result = repository.reset_agent_permissions(
        actor_subject_id=ADMIN,
        subject_id=ALICE,
        scope="all_agent",
    )
    assert result["matched"] == 0
    assert repository.generation() == before

    for scope, kwargs, code in [
        ("chat", {}, "chat_required"),
        ("workspace", {}, "workspace_required"),
        ("location", {}, "location_required"),
        ("everything", {}, "invalid_reset_scope"),
    ]:
        with pytest.raises(FilePolicyError) as exc:
            repository.preview_agent_reset(subject_id=ALICE, scope=scope, **kwargs)
        assert exc.value.code == code


def test_authenticated_reset_api_previews_then_revokes_only_self_agent_state(tmp_path, monkeypatch):
    from routes.file_policy_routes import setup_file_policy_routes
    import routes.file_policy_routes as policy_routes

    repository = _repository(tmp_path)
    alice_location = _location(repository, tmp_path, "alice-root")
    bob_location = _location(repository, tmp_path, "bob-root")
    alice_people = repository.create_binding(
        actor_subject_id=ADMIN,
        binding_class="people",
        subject_id=ALICE,
        location_id=alice_location.id,
        capabilities=("read",),
    )
    alice_agent = repository.create_binding(
        actor_subject_id=ALICE,
        binding_class="agent",
        subject_id=ALICE,
        location_id=alice_location.id,
        capabilities=("read",),
    )
    bob_agent = repository.create_binding(
        actor_subject_id=BOB,
        binding_class="agent",
        subject_id=BOB,
        location_id=bob_location.id,
        capabilities=("read",),
    )

    class Auth:
        def account_id(self, username):
            return {"alice": ALICE, "bob": BOB}.get(username)

        def is_admin(self, username):
            return False

    app = FastAPI()
    app.state.auth_manager = Auth()
    pending = []

    class Handler:
        def reject_scope(self, **scope):
            pending.append(scope)
            return 2

    compatibility = []

    class CompatibilityStore:
        def preview_agent_reset(self, **scope):
            compatibility.append(("preview", scope))
            return 3

        def reset_agent_permissions(self, **scope):
            compatibility.append(("reset", scope))
            return 3

    runtime_invalidations = []

    class Supervisor:
        def permission_handler_for(self, owner):
            return Handler()

        def grant_store_for(self, owner):
            return CompatibilityStore()

        async def invalidate_owner_projection(self, owner):
            runtime_invalidations.append(owner)

    app.state.mimo_supervisor = Supervisor()

    @app.middleware("http")
    async def authenticate(request: Request, call_next):
        request.state.current_user = "alice"
        return await call_next(request)

    closed = []
    monkeypatch.setattr(policy_routes, "close_all_clients", lambda: closed.append(True))
    app.include_router(setup_file_policy_routes(repository=repository))

    with TestClient(app) as client:
        state = client.get("/api/file-policy/state")
        assert state.status_code == 200
        payload = state.json()
        assert {row["id"] for row in payload["locations"]} == {alice_location.id}
        assert {row["id"] for row in payload["bindings"]} == {alice_people.id, alice_agent.id}
        assert bob_location.canonical_path not in state.text
        assert bob_agent.id not in state.text

        foreign_location = client.post(
            "/api/file-policy/resets/preview",
            json={"scope": "location", "location_id": bob_location.id},
        )
        assert foreign_location.status_code == 404
        assert foreign_location.json()["detail"]["code"] == "location_not_found"
        assert compatibility == []

        preview = client.post("/api/file-policy/resets/preview", json={"scope": "all_agent"})
        assert preview.status_code == 200
        assert preview.json()["matched"] == 1
        assert preview.json()["compatibility_matched"] == 3
        assert preview.json()["total_matched"] == 4
        assert preview.json()["people_preserved"] is True
        assert closed == []

        reset = client.post("/api/file-policy/resets", json={"scope": "all_agent"})
        assert reset.status_code == 200
        assert reset.json()["matched"] == 1
        assert reset.json()["compatibility_revoked"] == 3
        assert reset.json()["total_revoked"] == 4
        assert reset.json()["pending_rejected"] == 2
        assert reset.json()["runtime_invalidated"] is True
        assert runtime_invalidations == ["alice"]
        assert pending == [{"all_pending": True}]
        assert compatibility == [
            ("preview", {
                "owner": "alice",
                "scope": "all_agent",
                "chat_id": "",
                "workspace_id": "",
                "legacy_workspace": "",
                "location_workspace_ids": (),
                "legacy_location_path": "",
            }),
            ("reset", {
                "owner": "alice",
                "scope": "all_agent",
                "chat_id": "",
                "workspace_id": "",
                "legacy_workspace": "",
                "location_workspace_ids": (),
                "legacy_location_path": "",
            }),
        ]
        assert closed == [True]
        assert repository.get_binding(alice_agent.id).status == "revoked"
        assert repository.get_binding(alice_people.id).status == "active"
        assert repository.get_binding(bob_agent.id).status == "active"

        malformed = client.post("/api/file-policy/resets", json={"scope": "chat"})
        assert malformed.status_code == 400
        assert malformed.json()["detail"]["code"] == "chat_required"


def test_admin_add_location_makes_whole_disk_explicit_and_projects_only_agent_bindings(tmp_path, monkeypatch):
    from routes.file_policy_routes import setup_file_policy_routes
    import routes.file_policy_routes as policy_routes

    repository = _repository(tmp_path)
    registry = FilesystemRootRegistry(tmp_path / "legacy-roots.json")
    owner = {"value": "admin"}

    class Auth:
        def account_id(self, username):
            return {"admin": ADMIN, "alice": ALICE}.get(username)

        def is_admin(self, username):
            return username == "admin"

    app = FastAPI()
    app.state.auth_manager = Auth()

    @app.middleware("http")
    async def authenticate(request: Request, call_next):
        request.state.current_user = owner["value"]
        return await call_next(request)

    closed = []
    monkeypatch.setattr(policy_routes, "close_all_clients", lambda: closed.append(True))
    app.include_router(setup_file_policy_routes(repository=repository, filesystem_registry=registry))

    with TestClient(app) as client:
        system_root = str(Path(tmp_path.anchor))
        whole = client.post("/api/file-policy/locations", json={
            "path": system_root,
            "kind": "whole_root",
            "capabilities": ["read", "write"],
            "agent_access": True,
        })
        assert whole.status_code == 200, whole.text
        payload = whole.json()
        assert payload["location"]["kind"] == "whole_root"
        assert payload["location"]["canonical_path"] == system_root
        assert payload["agent_binding_id"]
        assert payload["compatibility_projected"] is True
        assert payload["os_managed"] is True
        assert len(registry.list("admin")) == 1
        assert registry.list("admin")[0]["kind"] == "recursive_directory"
        assert registry.list("admin")[0]["canonical_path"] == system_root
        assert len(repository.list_bindings(subject_id=ADMIN, binding_class="agent")) == 1

        repeated = client.post("/api/file-policy/locations", json={
            "path": system_root,
            "kind": "whole_root",
            "capabilities": ["read", "write"],
            "agent_access": True,
        })
        assert repeated.status_code == 200
        assert repeated.json()["location"]["id"] == payload["location"]["id"]
        assert len(repository.list_bindings(subject_id=ADMIN, binding_class="agent")) == 1
        assert len(registry.list("admin")) == 1

        workspace = repository.create_workspace(
            actor_subject_id=ADMIN,
            owner_subject_id=ALICE,
            location_id=payload["location"]["id"],
            name="Whole disk review",
        )
        people = repository.create_binding(
            actor_subject_id=ADMIN,
            binding_class="people",
            subject_id=ALICE,
            location_id=payload["location"]["id"],
            capabilities=("read",),
        )
        projection_root = registry.add(
            "__openclank_app_visibility__",
            system_root,
            "recursive_directory",
            ["read"],
        )
        projection = registry.assign_visibility(
            "__openclank_app_visibility__",
            "alice",
            projection_root["id"],
            ["read"],
        )

        owner["value"] = "alice"
        denied_remove = client.delete(f"/api/file-policy/locations/{payload['location']['id']}")
        assert denied_remove.status_code == 403
        owner["value"] = "admin"

        removed = client.delete(f"/api/file-policy/locations/{payload['location']['id']}")
        assert removed.status_code == 200, removed.text
        removal = removed.json()
        assert removal["bindings_revoked"] == 2  # Agent + People
        assert removal["workspaces_archived"] == 1
        assert removal["compatibility"] == {"roots_disabled": 2, "assignments_disabled": 1}
        assert repository.get_location(payload["location"]["id"]).enabled is False
        assert repository.get_binding(people.id).status == "revoked"
        assert repository.get_workspace(workspace.id).archived is True
        assert registry.list_visibility()[0]["id"] == projection["id"]
        assert registry.list_visibility()[0]["enabled"] is False

        restored = client.post("/api/file-policy/locations", json={
            "path": system_root,
            "kind": "whole_root",
            "capabilities": ["read", "write"],
            "agent_access": True,
        })
        assert restored.status_code == 200, restored.text
        assert restored.json()["location"]["id"] == payload["location"]["id"]
        assert repository.get_binding(people.id).status == "revoked"
        assert repository.get_workspace(workspace.id).archived is True
        assert len(repository.list_bindings(subject_id=ADMIN, binding_class="agent")) == 1
        assert registry.list("__openclank_app_visibility__")[0]["enabled"] is False

        browse_only = tmp_path / "browse-only"
        browse_only.mkdir()
        no_agent = client.post("/api/file-policy/locations", json={
            "path": str(browse_only),
            "kind": "directory",
            "capabilities": ["read"],
            "agent_access": False,
        })
        assert no_agent.status_code == 200
        assert no_agent.json()["agent_binding_id"] is None
        assert no_agent.json()["compatibility_projected"] is False
        assert len(registry.list("admin")) == 1

        owner["value"] = "alice"
        denied = client.post("/api/file-policy/locations", json={
            "path": str(browse_only),
            "kind": "directory",
            "capabilities": ["read"],
            "agent_access": True,
        })
        assert denied.status_code == 403

    assert len(closed) == 4  # create, refresh, remove, and restored projection


def test_admin_people_access_projects_without_minting_admin_agent_authority(tmp_path, monkeypatch):
    from routes.file_policy_routes import setup_file_policy_routes
    import routes.file_policy_routes as policy_routes

    repository = _repository(tmp_path)
    registry = FilesystemRootRegistry(tmp_path / "legacy-people.json")
    location = _location(repository, tmp_path, "shared-location")
    owner = {"value": "admin"}

    class Auth:
        identities = {"admin": ADMIN, "alice": ALICE, "bob": BOB}

        def account_id(self, username):
            return self.identities.get(username)

        def username_for_account_id(self, account_id):
            return next((name for name, value in self.identities.items() if value == account_id), None)

        def is_admin(self, username):
            return username == "admin"

    app = FastAPI()
    app.state.auth_manager = Auth()

    @app.middleware("http")
    async def authenticate(request: Request, call_next):
        request.state.current_user = owner["value"]
        return await call_next(request)

    closed = []
    monkeypatch.setattr(policy_routes, "close_all_clients", lambda: closed.append(True))
    app.include_router(setup_file_policy_routes(repository=repository, filesystem_registry=registry))

    with TestClient(app) as client:
        created = client.post("/api/file-policy/people", json={
            "subject_username": "alice",
            "location_id": location.id,
            "capabilities": ["read", "write"],
        })
        assert created.status_code == 200, created.text
        binding_id = created.json()["binding"]["id"]
        binding = repository.get_binding(binding_id)
        assert binding.binding_class == "people"
        assert binding.subject_id == ALICE
        assert set(binding.capabilities) == {"read", "write"}

        # The derived root belongs only to the reserved projection principal;
        # People sharing must not silently create an administrator Agent root.
        assert registry.list("admin") == []
        projected_roots = registry.list("__openclank_app_visibility__")
        assert len(projected_roots) == 1
        assert projected_roots[0]["canonical_path"] == location.canonical_path
        scope = registry.app_scope("alice", is_admin=False)
        assert scope["visible_root_ids"] == [projected_roots[0]["id"]]
        assert scope["root_capabilities"] == {
            projected_roots[0]["id"]: ["read", "write"],
        }

        # Narrowing reaches the live compatibility lane immediately.
        narrowed = client.patch(f"/api/file-policy/people/{binding_id}", json={
            "capabilities": ["read"],
        })
        assert narrowed.status_code == 200, narrowed.text
        assert narrowed.json()["binding"]["capabilities"] == ["read"]
        assert registry.app_scope("alice", is_admin=False)["capabilities"] == ["read"]

        disabled = client.patch(f"/api/file-policy/people/{binding_id}", json={
            "enabled": False,
        })
        assert disabled.status_code == 200
        assert disabled.json()["binding"]["status"] == "revoked"
        assert registry.app_scope("alice", is_admin=False)["visible_root_ids"] == []

        # POST is idempotent for the same principal/location and revives the
        # canonical row instead of manufacturing a parallel grant.
        revived = client.post("/api/file-policy/people", json={
            "subject_username": "alice",
            "location_id": location.id,
            "capabilities": ["read"],
        })
        assert revived.status_code == 200
        assert revived.json()["binding"]["id"] == binding_id
        assert len(repository.list_bindings(subject_id=ALICE, binding_class="people")) == 1
        assert registry.app_scope("alice", is_admin=False)["capabilities"] == ["read"]

        removed = client.delete(f"/api/file-policy/people/{binding_id}")
        assert removed.status_code == 200
        assert repository.get_binding(binding_id).status == "revoked"
        assert registry.app_scope("alice", is_admin=False)["visible_root_ids"] == []
        assert not any(row["subject_id"] == "alice" for row in registry.list_visibility())

        owner["value"] = "alice"
        denied = client.post("/api/file-policy/people", json={
            "subject_username": "bob",
            "location_id": location.id,
            "capabilities": ["read"],
        })
        assert denied.status_code == 403

    assert len(closed) == 5


def test_self_agent_access_controls_intersect_people_ceiling_and_project_rust_scope(tmp_path, monkeypatch):
    from routes.file_policy_routes import setup_file_policy_routes
    import routes.file_policy_routes as policy_routes

    repository = _repository(tmp_path)
    registry = FilesystemRootRegistry(tmp_path / "legacy-agent-access.json")
    location = _location(repository, tmp_path, "assigned-agent-location")
    repository.create_binding(
        actor_subject_id=ADMIN,
        binding_class="people",
        subject_id=ALICE,
        location_id=location.id,
        capabilities=("read",),
    )

    class Auth:
        def account_id(self, username):
            return {"admin": ADMIN, "alice": ALICE}.get(username)

        def is_admin(self, username):
            return username == "admin"

    app = FastAPI()
    app.state.auth_manager = Auth()

    @app.middleware("http")
    async def authenticate(request: Request, call_next):
        request.state.current_user = "alice"
        return await call_next(request)

    closed = []
    monkeypatch.setattr(policy_routes, "close_all_clients", lambda: closed.append(True))
    app.include_router(setup_file_policy_routes(repository=repository, filesystem_registry=registry))

    with TestClient(app) as client:
        state = client.get("/api/file-policy/state")
        assert state.status_code == 200
        assert state.json()["subject_id"] == ALICE
        assert state.json()["is_admin"] is False

        created = client.post("/api/file-policy/agent-access", json={
            "location_id": location.id,
            "capabilities": ["read"],
        })
        assert created.status_code == 200, created.text
        binding_id = created.json()["binding"]["id"]
        assert created.json()["binding"]["lifetime"] == "always"
        assert repository.get_binding(binding_id).subject_id == ALICE
        projected = registry.list("alice")
        assert len(projected) == 1
        assert projected[0]["canonical_path"] == location.canonical_path
        assert projected[0]["capabilities"] == ["read"]

        widened = client.patch(f"/api/file-policy/agent-access/{binding_id}", json={
            "capabilities": ["read", "write"],
        })
        assert widened.status_code == 403
        assert widened.json()["detail"]["code"] == "capability_escalation"
        assert registry.list("alice")[0]["capabilities"] == ["read"]

        disabled = client.patch(f"/api/file-policy/agent-access/{binding_id}", json={
            "enabled": False,
        })
        assert disabled.status_code == 200
        assert disabled.json()["binding"]["status"] == "revoked"
        assert registry.list("alice")[0]["enabled"] is False

        revived = client.post("/api/file-policy/agent-access", json={
            "location_id": location.id,
            "capabilities": ["read"],
        })
        assert revived.status_code == 200
        assert revived.json()["binding"]["id"] == binding_id
        assert registry.list("alice")[0]["enabled"] is True

        removed = client.delete(f"/api/file-policy/agent-access/{binding_id}")
        assert removed.status_code == 200
        assert repository.get_binding(binding_id).status == "revoked"
        assert registry.list("alice")[0]["enabled"] is False

    assert len(closed) == 4


def test_stable_workspace_ids_only_narrow_existing_authority(tmp_path):
    from routes.file_policy_routes import setup_file_policy_routes

    repository = _repository(tmp_path)
    owner = {"value": "admin"}

    class Auth:
        identities = {"admin": ADMIN, "alice": ALICE, "bob": BOB}

        def account_id(self, username):
            return self.identities.get(username)

        def username_for_account_id(self, account_id):
            return next((name for name, value in self.identities.items() if value == account_id), None)

        def is_admin(self, username):
            return username == "admin"

    app = FastAPI()
    app.state.auth_manager = Auth()

    @app.middleware("http")
    async def authenticate(request: Request, call_next):
        request.state.current_user = owner["value"]
        return await call_next(request)

    app.include_router(setup_file_policy_routes(repository=repository))
    admin_folder = tmp_path / "admin-code"
    admin_folder.mkdir()

    with TestClient(app) as client:
        presets = client.get("/api/file-policy/location-presets")
        assert presets.status_code == 200
        assert presets.json()["home"]
        assert presets.json()["whole_roots"]
        assert presets.json()["os_managed"] is True
        # Admin app browsing may catalog a physical folder, but that creates no
        # Agent binding.  The returned opaque ID is the durable browser state.
        app_workspace = client.post("/api/file-policy/workspaces/from-path", json={
            "path": str(admin_folder),
            "purpose": "app_folder",
            "name": "Admin code",
        })
        assert app_workspace.status_code == 200, app_workspace.text
        admin_workspace_id = app_workspace.json()["workspace"]["id"]
        admin_location_id = app_workspace.json()["workspace"]["location_id"]
        assert app_workspace.json()["workspace"]["path"] == str(admin_folder)
        assert repository.list_bindings(subject_id=ADMIN, binding_class="agent") == []

        denied_agent = client.post("/api/file-policy/workspaces/from-path", json={
            "path": str(admin_folder),
            "purpose": "agent_workspace",
        })
        assert denied_agent.status_code == 403
        repository.create_binding(
            actor_subject_id=ADMIN,
            binding_class="agent",
            subject_id=ADMIN,
            location_id=admin_location_id,
            capabilities=("read", "write"),
            lifetime="always",
        )
        agent_workspace = client.post("/api/file-policy/workspaces/from-path", json={
            "path": str(admin_folder),
            "purpose": "agent_workspace",
        })
        assert agent_workspace.status_code == 200
        assert agent_workspace.json()["workspace"]["id"] == admin_workspace_id

        resolved = client.get(
            f"/api/file-policy/workspaces/{admin_workspace_id}/resolve",
            params={"purpose": "agent_workspace"},
        )
        assert resolved.status_code == 200
        assert resolved.json()["workspace"]["path"] == str(admin_folder)

        alice_root = tmp_path / "alice-shared"
        alice_root.mkdir()
        child = alice_root / "project"
        child.mkdir()
        location = repository.create_location(
            actor_subject_id=ADMIN,
            path=str(alice_root),
            kind="directory",
            capabilities=("read", "write"),
        )
        repository.create_binding(
            actor_subject_id=ADMIN,
            binding_class="people",
            subject_id=ALICE,
            location_id=location.id,
            capabilities=("read",),
        )
        repository.create_binding(
            actor_subject_id=ADMIN,
            binding_class="agent",
            subject_id=ALICE,
            location_id=location.id,
            capabilities=("read",),
            lifetime="always",
        )
        owner["value"] = "alice"
        alice = client.post("/api/file-policy/workspaces/from-path", json={
            "path": str(child),
            "purpose": "agent_workspace",
        })
        assert alice.status_code == 200, alice.text
        alice_workspace = alice.json()["workspace"]
        assert alice_workspace["relative_folder"] == "project"

        owner["value"] = "bob"
        assert client.get("/api/file-policy/location-presets").status_code == 403
        hidden = client.get(
            f"/api/file-policy/workspaces/{alice_workspace['id']}/resolve",
            params={"purpose": "app_folder"},
        )
        assert hidden.status_code == 404
        outside = client.post("/api/file-policy/workspaces/from-path", json={
            "path": str(child),
            "purpose": "app_folder",
        })
        assert outside.status_code == 403


def test_audit_uses_opaque_ids_and_deterministic_legacy_ids(tmp_path):
    repository = _repository(tmp_path)
    location = _location(repository, tmp_path)
    events = repository.audit_events()
    assert events and events[-1]["target_id"] == location.id
    assert location.canonical_path not in str(events)
    first = deterministic_legacy_id("location", "root:old-1")
    assert first == deterministic_legacy_id("location", "root:old-1")
    assert first != deterministic_legacy_id("location", "root:old-2")


def test_subject_purge_removes_bindings_and_workspaces_but_preserves_location(tmp_path):
    repository = _repository(tmp_path)
    location = _location(repository, tmp_path)
    workspace = repository.create_workspace(
        actor_subject_id=ADMIN,
        owner_subject_id=ALICE,
        location_id=location.id,
        name="Alice",
    )
    repository.create_binding(
        actor_subject_id=ADMIN,
        binding_class="people",
        subject_id=ALICE,
        location_id=location.id,
        capabilities=("read",),
    )
    repository.save_place(
        owner_subject_id=ALICE,
        provider="host",
        stable_resource_id="resource-home",
        origin_id="host:v1:opaque-home",
        kind="folder",
        display_name="Home",
    )
    repository.touch_recent(
        owner_subject_id=ALICE,
        provider="host",
        stable_resource_id="resource-recent",
        origin_id="host:v1:private-recent-origin",
        kind="file",
        display_name="Recent.txt",
    )
    repository.save_search(
        owner_subject_id=ALICE,
        name="Recent reports",
        provider_scope="host",
        query="quarterly report",
        sort={"key": "modified", "direction": "desc", "directories_first": True},
    )
    repository.create_binding(
        actor_subject_id=ADMIN,
        binding_class="agent",
        subject_id=ALICE,
        location_id=location.id,
        workspace_id=workspace.id,
        lifetime="workspace",
        capabilities=("read",),
    )

    removed = repository.purge_subject(ALICE, actor_subject_id=ADMIN)

    assert removed == {
        "bindings": 2,
        "workspaces": 1,
        "places": 1,
        "recents": 1,
        "saved_searches": 1,
    }
    assert repository.list_places(ALICE) == []
    assert repository.list_recents(ALICE) == []
    assert repository.list_saved_searches(ALICE) == []
    assert repository.list_bindings(subject_id=ALICE, include_inactive=True) == []
    assert repository.get_location(location.id).id == location.id
    with pytest.raises(FilePolicyError) as exc:
        repository.get_workspace(workspace.id)
    assert exc.value.code == "workspace_not_found"


def test_file_places_are_owner_scoped_idempotent_and_do_not_bump_authority(tmp_path):
    repository = _repository(tmp_path)
    generation = repository.generation()
    first = repository.save_place(
        owner_subject_id=ALICE,
        provider="host",
        stable_resource_id="resource-project",
        origin_id="host:v1:opaque-project",
        kind="folder",
        display_name="Project",
    )
    updated = repository.save_place(
        owner_subject_id=ALICE,
        provider="host",
        stable_resource_id="resource-project",
        origin_id="host:v1:opaque-project-moved",
        kind="folder",
        display_name="Renamed Project",
    )
    repository.save_place(
        owner_subject_id=BOB,
        provider="host",
        stable_resource_id="resource-project",
        origin_id="host:v1:bob-project",
        kind="folder",
        display_name="Bob Project",
    )

    assert first.id == updated.id
    assert repository.generation() == generation
    assert [(row.display_name, row.origin_id) for row in repository.list_places(ALICE)] == [
        ("Renamed Project", "host:v1:opaque-project-moved"),
    ]
    assert [row.display_name for row in repository.list_places(BOB)] == ["Bob Project"]
    assert repository.remove_place(owner_subject_id=BOB, place_id=first.id) is False
    assert repository.remove_place(owner_subject_id=ALICE, place_id=first.id) is True
    assert repository.list_places(ALICE) == []


def test_file_recents_are_encrypted_owner_scoped_bounded_and_clearable(tmp_path, monkeypatch):
    repository = _repository(tmp_path)
    generation = repository.generation()
    clock = iter((1_000, 2_000, 3_000, 4_000, 5_000))
    monkeypatch.setattr("src.openclank.file_policy._now_ms", lambda: next(clock))

    first = repository.touch_recent(
        owner_subject_id=ALICE,
        provider="host",
        stable_resource_id="resource-one",
        origin_id="host:v1:/private/alice/one.txt",
        kind="file",
        display_name="one.txt",
        max_entries=2,
    )
    refreshed = repository.touch_recent(
        owner_subject_id=ALICE,
        provider="host",
        stable_resource_id="resource-one",
        origin_id="host:v2:/private/alice/renamed-one.txt",
        kind="file",
        display_name="renamed-one.txt",
        max_entries=2,
    )
    repository.touch_recent(
        owner_subject_id=ALICE,
        provider="gallery",
        stable_resource_id="resource-two",
        origin_id="gallery-private-two",
        kind="image",
        display_name="two.png",
        max_entries=2,
    )
    repository.touch_recent(
        owner_subject_id=ALICE,
        provider="library",
        stable_resource_id="resource-three",
        origin_id="library-private-three",
        kind="document",
        display_name="three.md",
        max_entries=2,
    )
    repository.touch_recent(
        owner_subject_id=BOB,
        provider="host",
        stable_resource_id="resource-bob",
        origin_id="host:v1:/private/bob/only.txt",
        kind="file",
        display_name="bob.txt",
        max_entries=2,
    )

    assert first.id == refreshed.id
    assert repository.generation() == generation
    assert [row.stable_resource_id for row in repository.list_recents(ALICE)] == [
        "resource-three",
        "resource-two",
    ]
    assert [row.display_name for row in repository.list_recents(BOB)] == ["bob.txt"]

    with sqlite3.connect(repository.db_path) as connection:
        rows = connection.execute(
            "SELECT owner_subject_id, origin_ciphertext FROM file_recents ORDER BY owner_subject_id"
        ).fetchall()
    assert rows
    assert all(str(ciphertext).startswith("enc:") for _owner, ciphertext in rows)
    serialized_store = repr(rows)
    assert "/private/alice" not in serialized_store
    assert "gallery-private-two" not in serialized_store
    assert "library-private-three" not in serialized_store
    assert "/private/bob" not in serialized_store

    assert repository.clear_recents(owner_subject_id=ALICE) == 2
    assert repository.clear_recents(owner_subject_id=ALICE) == 0
    assert repository.list_recents(ALICE) == []
    assert [row.stable_resource_id for row in repository.list_recents(BOB)] == ["resource-bob"]


def test_saved_searches_are_owner_scoped_recipes_without_result_authority(tmp_path):
    repository = _repository(tmp_path)
    generation = repository.generation()
    recipe = repository.save_search(
        owner_subject_id=ALICE,
        name="Quarterly reports",
        provider_scope="all",
        query="quarterly report",
        sort={"key": "modified", "direction": "desc", "directories_first": False},
    )
    bob_recipe = repository.save_search(
        owner_subject_id=BOB,
        name="Bob images",
        provider_scope="gallery",
        query="sunset",
        sort={"key": "name", "direction": "asc", "directories_first": True},
    )

    assert repository.generation() == generation
    assert [row.id for row in repository.list_saved_searches(ALICE)] == [recipe.id]
    assert [row.id for row in repository.list_saved_searches(BOB)] == [bob_recipe.id]
    assert recipe.provider_scope == "all"
    assert recipe.query == "quarterly report"
    assert dict(recipe.sort) == {
        "key": "modified",
        "direction": "desc",
        "directories_first": False,
    }
    assert not hasattr(recipe, "resource_ref")
    assert not hasattr(recipe, "results")
    assert not hasattr(recipe, "capabilities")

    with sqlite3.connect(repository.db_path) as connection:
        columns = {
            row[1]
            for row in connection.execute("PRAGMA table_info(file_saved_searches)").fetchall()
        }
    assert columns == {
        "id",
        "owner_subject_id",
        "name",
        "provider_scope",
        "query",
        "sort_json",
        "created_unix_ms",
        "updated_unix_ms",
    }
    assert not {"origin_id", "resource_ref", "results_json", "capabilities_json"} & columns

    assert repository.remove_saved_search(owner_subject_id=BOB, search_id=recipe.id) is False
    assert [row.id for row in repository.list_saved_searches(ALICE)] == [recipe.id]
    assert repository.remove_saved_search(owner_subject_id=ALICE, search_id=recipe.id) is True
    assert repository.remove_saved_search(owner_subject_id=ALICE, search_id=recipe.id) is False

    with pytest.raises(FilePolicyError) as invalid_provider:
        repository.save_search(
            owner_subject_id=ALICE,
            name="Bad",
            provider_scope="raw-filesystem",
            query="anything",
            sort={},
        )
    assert invalid_provider.value.code == "invalid_saved_search"
    with pytest.raises(FilePolicyError) as empty_query:
        repository.save_search(
            owner_subject_id=ALICE,
            name="Empty",
            provider_scope="all",
            query="   ",
            sort={},
        )
    assert empty_query.value.code == "invalid_saved_search"
