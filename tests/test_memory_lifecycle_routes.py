import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException

import routes.memory.memory_routes as memory_routes
from services.memory.forget_coordinator import (
    MemoryLifecycleCoordinator,
    MemorySkillForgetCoordinator,
    pack_forget_token,
    unpack_forget_token,
)
from services.memory.skills import SkillsManager
from src.memory_provider import MemoryRequestRejectedError


class _Request:
    def __init__(self, body=None):
        self._body = body or {}
        self.state = SimpleNamespace(current_user="alice")

    async def json(self):
        return self._body


class _Provider:
    provider_id = "frankenmemory"

    def __init__(self):
        self.calls = []
        self.ambiguous_commit = False
        self.operations = {}
        self.retention_operations = {}

    async def retention(self, action, **kwargs):
        self.calls.append(("retention", action, kwargs))
        if action == "preview_expire":
            return {
                "token": "retention-preview-token",
                "closure": {
                    "raw_ids": [],
                    "candidate_ids": [],
                    "curated_ids": ["memory-1"],
                    "graph_node_ids": [],
                },
            }
        if action == "expire":
            assert kwargs["preview_token"] == "retention-preview-token"
            operation_id = kwargs["operation_id"]
            closure = {
                "raw_ids": [],
                "candidate_ids": [],
                "curated_ids": ["memory-1"],
                "graph_node_ids": [],
            }
            self.retention_operations[operation_id] = {
                "operation_id": operation_id,
                "state": "committed",
                "closure": closure,
            }
            return closure
        if action == "status":
            return self.retention_operations.get(
                kwargs.get("operation_id"),
                {"state": "absent", "closure": None},
            )
        return {"action": action}

    async def forget(self, action, **kwargs):
        self.calls.append(("forget", action, kwargs))
        if action == "preview":
            return {
                "token": "preview-token",
                "closure": {
                    "raw_ids": [],
                    "candidate_ids": ["candidate-1"],
                    "curated_ids": ["memory-1"],
                    "graph_node_ids": [],
                },
            }
        if action == "commit":
            result = {
                "tombstone_id": "forget-1",
                "recover_until": "2099-01-01T00:00:00+00:00",
            }
            operation_id = kwargs.get("operation_id")
            if operation_id:
                self.operations[operation_id] = {
                    **result,
                    "state": "committed",
                    "closure": {
                        "raw_ids": [],
                        "candidate_ids": ["candidate-1"],
                        "curated_ids": ["memory-1"],
                        "graph_node_ids": [],
                    },
                }
            if self.ambiguous_commit:
                self.ambiguous_commit = False
                raise RuntimeError("response lost after commit")
            return result
        if action == "status":
            return self.operations.get(
                kwargs.get("operation_id"),
                {"state": "absent"},
            )
        if kwargs.get("operation_id") in self.operations:
            self.operations[kwargs["operation_id"]]["state"] = "restored"
        return {"restored": True}

    async def export_scope(self, **kwargs):
        self.calls.append(("export", kwargs))
        return {"owner": kwargs["owner"], "curated": []}

    async def explain(self, memory_id, **kwargs):
        self.calls.append(("explain", memory_id, kwargs))
        return {"id": memory_id, "source_uri": "message://alice/1"}

    async def update_candidate(self, candidate_id, **kwargs):
        self.calls.append(("update_candidate", candidate_id, kwargs))
        return {"id": candidate_id, "content": kwargs["text"], "status": "pending"}

    async def versioned_detail(self, memory_id, **kwargs):
        self.calls.append(("versioned_detail", memory_id, kwargs))
        return {
            "id": memory_id,
            "block_id": "block-question",
            "current_revision": 2,
            "current": {
                "revision": 2,
                "status": "active",
                "kind": "open_question",
                "text": "E",
            },
            "history": [],
        }

    async def versioned_list(self, **kwargs):
        self.calls.append(("versioned_list", kwargs))
        return [await self.versioned_detail("question-1", owner=kwargs.get("owner"))]

    async def resolve_question(self, memory_id, **kwargs):
        self.calls.append(("resolve_question", memory_id, kwargs))
        return True

    async def reopen_question(self, memory_id, **kwargs):
        self.calls.append(("reopen_question", memory_id, kwargs))
        return await self.versioned_detail(memory_id, owner=kwargs.get("owner"))

    async def versioned_transition(self, memory_id, action, **kwargs):
        self.calls.append(("versioned_transition", memory_id, action, kwargs))
        return await self.versioned_detail(memory_id, owner=kwargs.get("owner"))


def _route(router, path, method):
    for route in router.routes:
        if route.path == path and method in getattr(route, "methods", set()):
            return route.endpoint
    raise AssertionError(path)


@pytest.fixture
def routes(monkeypatch):
    import src.auth_helpers as auth_helpers

    provider = _Provider()
    monkeypatch.setattr(memory_routes, "get_current_user", lambda _request: "alice")
    monkeypatch.setattr(memory_routes, "require_user", lambda _request: "alice")
    monkeypatch.setattr(auth_helpers, "require_privilege", lambda _request, _name: None)
    session_manager = MagicMock()
    session_manager.sessions = {}
    router = memory_routes.setup_memory_routes(
        MagicMock(), session_manager, memory_provider=provider
    )
    return router, provider


def test_retention_routes_are_owner_scoped_and_reject_unknown_fields(routes):
    router, provider = routes
    get_retention = _route(router, "/api/memory/retention", "GET")
    set_retention = _route(router, "/api/memory/retention", "PUT")

    assert asyncio.run(get_retention(_Request())) == {"action": "get"}
    assert asyncio.run(
        set_retention(_Request({"raw_days": 14, "recovery_seconds": 60}))
    ) == {"action": "set"}
    assert all(call[-1]["owner"] == "alice" for call in provider.calls)

    with pytest.raises(HTTPException) as raised:
        asyncio.run(set_retention(_Request({"owner": "bob"})))
    assert raised.value.status_code == 400


def test_forget_preview_commit_and_delete_share_the_provenance_closure(routes):
    router, provider = routes
    forget = _route(router, "/api/memory/forget", "POST")
    delete = _route(router, "/api/memory/{memory_id}", "DELETE")

    preview = asyncio.run(
        forget(
            _Request(
                {
                    "action": "preview",
                    "selector_kind": "source_message_id",
                    "selector": "message-1",
                }
            )
        )
    )
    preview_parts = unpack_forget_token(preview["token"], "preview")
    assert preview_parts["provider"] == "preview-token"
    assert len(preview_parts["operation"]) == 32

    deleted = asyncio.run(delete(_Request(), "memory-1"))
    tombstone_parts = unpack_forget_token(
        deleted["tombstone_id"],
        "tombstone",
    )
    assert tombstone_parts["provider"] == "forget-1"
    commit_calls = [
        call
        for call in provider.calls
        if call[0] == "forget" and call[1] == "commit"
    ]
    assert commit_calls[-1][2]["preview_token"] == "preview-token"
    assert len(commit_calls[-1][2]["operation_id"]) == 32
    assert all(
        call[2]["owner"] == "alice"
        for call in provider.calls
        if call[0] == "forget"
    )


def test_export_and_explain_never_accept_a_caller_supplied_owner(routes):
    router, provider = routes
    export = _route(router, "/api/memory/export", "GET")
    explain = _route(router, "/api/memory/{memory_id}/explain", "GET")

    assert asyncio.run(export(_Request()))["owner"] == "alice"
    assert asyncio.run(explain(_Request(), "memory-1")) == {
        "explanation": {
            "id": "memory-1",
            "source_uri": "message://alice/1",
        }
    }
    assert ("export", {"owner": "alice"}) in provider.calls
    assert ("explain", "memory-1", {"owner": "alice"}) in provider.calls


def test_typed_candidate_history_and_question_routes_keep_server_scope(routes):
    router, provider = routes
    edit_candidate = _route(router, "/api/memory/candidate/{candidate_id}", "PUT")
    inspect = _route(router, "/api/memory/inspect", "GET")
    history = _route(router, "/api/memory/{memory_id}/history", "GET")
    resolve = _route(router, "/api/memory/{memory_id}/resolve", "POST")
    reopen = _route(router, "/api/memory/{memory_id}/reopen", "POST")
    retract = _route(router, "/api/memory/{memory_id}/retract", "POST")
    revert = _route(router, "/api/memory/{memory_id}/revert", "POST")

    edited = asyncio.run(
        edit_candidate(
            _Request({"text": "edited answer", "category": "fact"}),
            "candidate-1",
        )
    )
    assert edited["candidate"]["content"] == "edited answer"
    inspected = asyncio.run(inspect(_Request(), tier="history", status="active", limit=10))
    assert inspected["items"][0]["block_id"] == "block-question"
    assert asyncio.run(history(_Request(), "question-1"))["current_revision"] == 2
    assert asyncio.run(
        resolve(
            _Request(),
            "question-1",
            answer="E",
            resolved_by=None,
            expected_revision=1,
        )
    )["memory"]["current"]["text"] == "E"
    assert asyncio.run(reopen(_Request(), "question-1", expected_revision=2))["ok"]
    assert asyncio.run(
        retract(_Request(), "question-1", expected_revision=2, reason="wrong")
    )["ok"]
    assert asyncio.run(
        revert(_Request(), "question-1", expected_revision=3, target_revision=1)
    )["ok"]

    assert (
        "update_candidate",
        "candidate-1",
        {
            "text": "edited answer",
            "category": "fact",
            "reason": "edited_by_user",
            "owner": "alice",
            "workspace_id": "global",
        },
    ) in provider.calls
    resolve_call = next(call for call in provider.calls if call[0] == "resolve_question")
    assert resolve_call[2] == {
        "answer": "E",
        "resolved_by": None,
        "expected_revision": 1,
        "owner": "alice",
    }


def test_project_and_spell_routes_use_real_owner_scoped_storage(
    tmp_path, monkeypatch
):
    import src.auth_helpers as auth_helpers
    from src.project_hex import register_project

    (tmp_path / ".git").mkdir()
    (tmp_path / ".hex").write_text("name: route-test\n", encoding="utf-8")
    db_path = str(tmp_path / "frankenmemory.db")
    project = register_project(
        tmp_path,
        owner="alice",
        workspace_id="global",
        db_path=db_path,
        project_id="project-route-test",
    )
    provider = _Provider()
    provider._fm_db_path = db_path
    monkeypatch.setattr(memory_routes, "get_current_user", lambda _request: "alice")
    monkeypatch.setattr(memory_routes, "require_user", lambda _request: "alice")
    monkeypatch.setattr(auth_helpers, "require_privilege", lambda _request, _name: None)
    session_manager = MagicMock()
    session_manager.sessions = {}
    router = memory_routes.setup_memory_routes(
        MagicMock(), session_manager, memory_provider=provider
    )

    list_projects = _route(router, "/api/memory/projects", "GET")
    inspect_project = _route(router, "/api/memory/projects/{project_id}", "GET")
    list_spells = _route(router, "/api/memory/projects/{project_id}/spells", "GET")
    create_spell = _route(router, "/api/memory/projects/{project_id}/spells", "POST")
    edit_spell = _route(router, "/api/memory/projects/{project_id}/spells/{spell_id}", "PUT")
    review_spell = _route(router, "/api/memory/projects/{project_id}/spells/{spell_id}/review", "POST")
    retract_spell = _route(router, "/api/memory/projects/{project_id}/spells/{spell_id}/retract", "POST")

    assert asyncio.run(list_projects(_Request()))["projects"] == [project]
    assert asyncio.run(inspect_project(_Request(), project["project_id"]))["project"] == project
    created = asyncio.run(
        create_spell(
            _Request({
                "title": "Use the route",
                "suggestion": {"sentence": "Keep this exact project rule."},
                "path_scope": "src/**",
            }),
            project["project_id"],
        )
    )["spell"]
    assert asyncio.run(list_spells(_Request(), project["project_id"]))["spells"] == [created]
    edited = asyncio.run(
        edit_spell(
            _Request({"expected_revision": 1, "rationale": "verified"}),
            project["project_id"],
            created["spell_id"],
        )
    )["spell"]
    accepted = asyncio.run(
        review_spell(
            _Request({"expected_revision": edited["revision"], "accept": True}),
            project["project_id"],
            created["spell_id"],
        )
    )["spell"]
    assert accepted["lifecycle"] == "active"
    retracted = asyncio.run(
        retract_spell(
            _Request({"expected_revision": accepted["revision"]}),
            project["project_id"],
            created["spell_id"],
        )
    )["spell"]
    assert retracted["lifecycle"] == "retracted"


def test_retention_expiry_removes_memory_derived_skills_through_same_boundary(
    tmp_path,
    monkeypatch,
):
    import src.auth_helpers as auth_helpers

    skills = SkillsManager(str(tmp_path))
    skill = skills.add_skill(
        name="expiring-flow",
        description="derived",
        source="memory-promotion",
        source_memory_ids=["memory-1"],
        owner="alice",
    )
    provider = _Provider()
    monkeypatch.setattr(memory_routes, "get_current_user", lambda _request: "alice")
    monkeypatch.setattr(
        auth_helpers,
        "require_privilege",
        lambda _request, _name: None,
    )
    session_manager = MagicMock()
    session_manager.sessions = {}
    router = memory_routes.setup_memory_routes(
        MagicMock(),
        session_manager,
        memory_provider=provider,
        skills_manager=skills,
    )
    expire = _route(router, "/api/memory/retention/expire", "POST")

    result = asyncio.run(expire(_Request()))
    assert result["curated_ids"] == ["memory-1"]
    assert result["skill_ids"] == [skill["skill_id"]]
    assert skills.load(owner="alice") == []
    assert [
        call[1]
        for call in provider.calls
        if call[0] == "retention"
    ] == [
        "preview_expire",
        "status",
        "preview_expire",
        "expire",
    ]
    expire_call = next(
        call
        for call in provider.calls
        if call[0] == "retention" and call[1] == "expire"
    )
    assert expire_call[2]["preview_token"] == "retention-preview-token"
    assert len(expire_call[2]["operation_id"]) == 32


def test_route_forget_includes_and_restores_memory_derived_skills(
    tmp_path,
    monkeypatch,
):
    import src.auth_helpers as auth_helpers

    skills = SkillsManager(str(tmp_path))
    candidate = skills.nominate_promotion(
        owner="alice",
        recommender="user:alice",
        citations=[
            {"memory_id": "memory-1", "content_hash": "one"},
            {"memory_id": "memory-2", "content_hash": "two"},
        ],
        name="remembered-flow",
        scope="owner",
    )
    drafted = skills.draft_promotion(
        candidate["id"],
        owner="alice",
        drafter="user:alice",
        fields={"description": "from memory", "procedure": ["act"]},
    )
    skill_id = drafted["skill"]["skill_id"]
    skills.set_necessity(skill_id, True, owner="alice")
    skills.set_audit(
        skill_id,
        "pass",
        worker_model="quality-judge",
        owner="alice",
        results={"verdict": "pass", "confidence": 0.95, "issues": []},
    )
    skills.evaluate_promotion(
        candidate["id"],
        owner="alice",
        evaluator="audit:quality-judge",
    )
    skills.publish_promotion(
        candidate["id"],
        owner="alice",
        publisher="user:alice",
    )

    provider = _Provider()
    monkeypatch.setattr(memory_routes, "get_current_user", lambda _request: "alice")
    monkeypatch.setattr(memory_routes, "require_user", lambda _request: "alice")
    monkeypatch.setattr(
        auth_helpers,
        "require_privilege",
        lambda _request, _name: None,
    )
    session_manager = MagicMock()
    session_manager.sessions = {}
    router = memory_routes.setup_memory_routes(
        MagicMock(),
        session_manager,
        memory_provider=provider,
        skills_manager=skills,
    )
    forget = _route(router, "/api/memory/forget", "POST")

    preview = asyncio.run(forget(_Request({
        "action": "preview",
        "selector_kind": "record_id",
        "selector": "memory-1",
    })))
    assert preview["closure"]["promotion_ids"] == [candidate["id"]]
    assert preview["closure"]["skill_ids"] == [skill_id]
    preview_parts = unpack_forget_token(preview["token"], "preview")
    assert preview_parts["provider"] == "preview-token"

    provider.ambiguous_commit = True
    committed = asyncio.run(forget(_Request({
        "action": "commit",
        "selector_kind": "record_id",
        "selector": "memory-1",
        "preview_token": preview["token"],
    })))
    tombstone_parts = unpack_forget_token(
        committed["tombstone_id"],
        "tombstone",
    )
    assert tombstone_parts["provider"] == "forget-1"
    assert any(
        call[0] == "forget" and call[1] == "status"
        for call in provider.calls
    )
    assert skills.list_promotions("alice") == []
    assert skills.load_published(owner="alice") == []
    replayed = asyncio.run(forget(_Request({
        "action": "commit",
        "selector_kind": "record_id",
        "selector": "memory-1",
        "preview_token": preview["token"],
    })))
    assert replayed["tombstone_id"] == committed["tombstone_id"]

    mixed = pack_forget_token(
        "tombstone",
        provider="forget-other",
        operation=tombstone_parts["operation"],
        skills=tombstone_parts["skills"],
    )
    restore_calls = len([
        call
        for call in provider.calls
        if call[0] == "forget" and call[1] == "restore"
    ])
    with pytest.raises(HTTPException):
        asyncio.run(forget(_Request({
            "action": "restore",
            "tombstone_id": mixed,
        })))
    assert len([
        call
        for call in provider.calls
        if call[0] == "forget" and call[1] == "restore"
    ]) == restore_calls

    stripped = pack_forget_token(
        "tombstone",
        provider=tombstone_parts["provider"],
        operation=tombstone_parts["operation"],
    )
    with pytest.raises(HTTPException):
        asyncio.run(forget(_Request({
            "action": "restore",
            "tombstone_id": stripped,
        })))
    assert len([
        call
        for call in provider.calls
        if call[0] == "forget" and call[1] == "restore"
    ]) == restore_calls

    original_restore = MemorySkillForgetCoordinator.restore
    fail_local_once = True

    def flaky_local_restore(self, *args, **kwargs):
        nonlocal fail_local_once
        if fail_local_once and not kwargs.get("allow_prepared"):
            fail_local_once = False
            raise OSError("injected local restore failure")
        return original_restore(self, *args, **kwargs)

    monkeypatch.setattr(
        MemorySkillForgetCoordinator,
        "restore",
        flaky_local_restore,
    )
    with pytest.raises(HTTPException):
        asyncio.run(forget(_Request({
            "action": "restore",
            "tombstone_id": committed["tombstone_id"],
        })))
    provider_restore_calls = len([
        call
        for call in provider.calls
        if call[0] == "forget" and call[1] == "restore"
    ])
    restored = asyncio.run(forget(_Request({
        "action": "restore",
        "tombstone_id": committed["tombstone_id"],
    })))
    assert len([
        call
        for call in provider.calls
        if call[0] == "forget" and call[1] == "restore"
    ]) == provider_restore_calls
    assert restored["restored_promotion_ids"] == [candidate["id"]]
    assert restored["restored_skill_ids"] == [skill_id]
    assert [row["id"] for row in skills.list_promotions("alice")] == [
        candidate["id"]
    ]
    assert [row["skill_id"] for row in skills.load_published(owner="alice")] == [
        skill_id
    ]

    delete = _route(router, "/api/memory/{memory_id}", "DELETE")
    deleted = asyncio.run(delete(_Request(), "memory-1"))
    assert unpack_forget_token(
        deleted["tombstone_id"],
        "tombstone",
    )["provider"] == "forget-1"
    assert skills.list_promotions("alice") == []
    assert skills.load_published(owner="alice") == []


@pytest.mark.asyncio
async def test_lost_commit_and_status_keeps_quarantine_for_startup_reconcile(
    tmp_path,
):
    class Provider:
        status_available = False

        def __init__(self):
            self.operations = {}

        async def forget(self, action, **kwargs):
            operation_id = kwargs.get("operation_id")
            closure = {
                "raw_ids": [],
                "candidate_ids": [],
                "curated_ids": ["memory-1"],
                "graph_node_ids": [],
            }
            if action == "preview":
                return {"token": "preview-1", "closure": closure}
            if action == "status":
                if not self.status_available:
                    raise ConnectionError("status temporarily unavailable")
                return self.operations.get(operation_id, {"state": "absent"})
            if action == "commit":
                self.operations[operation_id] = {
                    "state": "committed",
                    "tombstone_id": "provider-tombstone",
                    "recover_until": "2099-01-01T00:00:00+00:00",
                    "closure": closure,
                }
                raise ConnectionError("commit reply was lost")
            if action == "restore":
                self.operations[operation_id]["state"] = "restored"
                return {"restored": True}
            raise AssertionError(action)

    provider = Provider()
    skills = SkillsManager(str(tmp_path))
    created = skills.add_skill(
        name="derived-before-loss",
        description="derived",
        source="memory-promotion",
        source_memory_ids=["memory-1"],
        owner="alice",
    )
    lifecycle = MemoryLifecycleCoordinator(provider, skills)
    preview = await lifecycle.forget(
        "preview",
        owner="alice",
        selector_kind="record_id",
        selector="memory-1",
    )
    operation_id = unpack_forget_token(
        preview["token"],
        "preview",
    )["operation"]

    with pytest.raises(ConnectionError, match="commit reply"):
        await lifecycle.forget(
            "commit",
            owner="alice",
            selector_kind="record_id",
            selector="memory-1",
            preview_token=preview["token"],
        )
    assert skills.load(owner="alice") == []
    assert lifecycle.skill_forget.operation_info(
        operation_id,
        owner="alice",
    )["state"] == "prepared"

    provider.status_available = True
    restarted = MemoryLifecycleCoordinator(
        provider,
        SkillsManager(str(tmp_path)),
    )
    assert await restarted.reconcile() == {
        "rolled_back": 0,
        "committed": 1,
        "restored": 0,
        "errors": 0,
    }
    assert restarted.skill_forget.operation_info(
        operation_id,
        owner="alice",
    )["state"] == "committed"

    restored = await restarted.forget(
        "restore",
        owner="alice",
        tombstone_id=pack_forget_token(
            "tombstone",
            provider="provider-tombstone",
            operation=operation_id,
            skills=operation_id,
        ),
    )
    assert restored["restored_skill_ids"] == [created["skill_id"]]


@pytest.mark.parametrize(
    "restore_reply",
    (
        {"restored": False, "detail": "provider did not restore"},
        {"accepted": True},
    ),
)
@pytest.mark.asyncio
async def test_restore_reply_without_restored_status_keeps_local_quarantine(
    tmp_path,
    restore_reply,
):
    class Provider:
        def __init__(self):
            self.operations = {}

        async def forget(self, action, **kwargs):
            operation_id = kwargs.get("operation_id")
            closure = {
                "raw_ids": [],
                "candidate_ids": [],
                "curated_ids": ["memory-1"],
                "graph_node_ids": [],
            }
            if action == "preview":
                return {"token": "preview-1", "closure": closure}
            if action == "status":
                return self.operations.get(operation_id, {"state": "absent"})
            if action == "commit":
                result = {
                    "state": "committed",
                    "tombstone_id": "provider-tombstone",
                    "recover_until": "2099-01-01T00:00:00+00:00",
                    "closure": closure,
                }
                self.operations[operation_id] = result
                return result
            if action == "restore":
                return dict(restore_reply)
            raise AssertionError(action)

    provider = Provider()
    skills = SkillsManager(str(tmp_path))
    created = skills.add_skill(
        name="derived-before-unconfirmed-restore",
        description="must remain quarantined",
        source="memory-promotion",
        source_memory_ids=["memory-1"],
        owner="alice",
    )
    lifecycle = MemoryLifecycleCoordinator(provider, skills)
    preview = await lifecycle.forget(
        "preview",
        owner="alice",
        selector_kind="record_id",
        selector="memory-1",
    )
    committed = await lifecycle.forget(
        "commit",
        owner="alice",
        selector_kind="record_id",
        selector="memory-1",
        preview_token=preview["token"],
    )
    operation_id = unpack_forget_token(
        committed["tombstone_id"],
        "tombstone",
    )["operation"]

    with pytest.raises(ValueError, match="restore"):
        await lifecycle.forget(
            "restore",
            owner="alice",
            tombstone_id=committed["tombstone_id"],
        )

    assert skills.load(owner="alice") == []
    info = lifecycle.skill_forget.operation_info(
        operation_id,
        owner="alice",
    )
    assert info["state"] == "committed"
    assert info["skill_ids"] == [created["skill_id"]]
    assert provider.operations[operation_id]["state"] == "committed"


@pytest.mark.asyncio
async def test_definite_forget_rejection_rolls_back_new_local_prepare(tmp_path):
    class Provider:
        async def forget(self, action, **kwargs):
            closure = {
                "raw_ids": [],
                "candidate_ids": [],
                "curated_ids": ["memory-1"],
                "graph_node_ids": [],
            }
            if action == "preview":
                return {"token": "preview-1", "closure": closure}
            if action == "status":
                return {"state": "absent"}
            if action == "commit":
                raise MemoryRequestRejectedError("preview rejected")
            raise AssertionError(action)

    skills = SkillsManager(str(tmp_path))
    created = skills.add_skill(
        name="derived-before-definite-rejection",
        description="must be rolled back",
        source="memory-promotion",
        source_memory_ids=["memory-1"],
        owner="alice",
    )
    lifecycle = MemoryLifecycleCoordinator(Provider(), skills)
    preview = await lifecycle.forget(
        "preview",
        owner="alice",
        selector_kind="record_id",
        selector="memory-1",
    )

    with pytest.raises(MemoryRequestRejectedError, match="preview rejected"):
        await lifecycle.forget(
            "commit",
            owner="alice",
            selector_kind="record_id",
            selector="memory-1",
            preview_token=preview["token"],
        )

    assert [row["skill_id"] for row in skills.load(owner="alice")] == [
        created["skill_id"]
    ]
    assert lifecycle.skill_forget.pending_operations() == []


@pytest.mark.asyncio
async def test_definite_retention_rejection_rolls_back_local_prepare(tmp_path):
    class Provider:
        async def retention(self, action, **kwargs):
            if action == "preview_expire":
                return {
                    "token": "retention-preview-1",
                    "closure": {
                        "raw_ids": [],
                        "candidate_ids": [],
                        "curated_ids": ["memory-1"],
                        "graph_node_ids": [],
                    },
                }
            if action == "expire":
                raise MemoryRequestRejectedError("retention rejected")
            raise AssertionError(action)

    skills = SkillsManager(str(tmp_path))
    created = skills.add_skill(
        name="derived-before-retention-rejection",
        description="must be rolled back",
        source="memory-promotion",
        source_memory_ids=["memory-1"],
        owner="alice",
    )
    lifecycle = MemoryLifecycleCoordinator(Provider(), skills)

    with pytest.raises(MemoryRequestRejectedError, match="retention rejected"):
        await lifecycle.expire_retention(owner="alice")

    assert [row["skill_id"] for row in skills.load(owner="alice")] == [
        created["skill_id"]
    ]
    assert lifecycle.skill_forget.pending_operations() == []


@pytest.mark.asyncio
async def test_operation_id_cannot_be_rebound_to_another_forget_tuple(tmp_path):
    class Provider:
        def __init__(self):
            self.operations = {}

        @staticmethod
        def closure(_selector):
            return {
                "raw_ids": [],
                "candidate_ids": [],
                # Distinct selectors may legitimately resolve to one closure.
                "curated_ids": ["shared-memory"],
                "graph_node_ids": [],
            }

        async def forget(self, action, **kwargs):
            operation_id = kwargs.get("operation_id")
            if action == "preview":
                return {
                    "token": f"preview:{kwargs['selector']}",
                    "closure": self.closure(kwargs["selector"]),
                }
            if action == "status":
                return self.operations.get(operation_id, {"state": "absent"})
            if action == "commit":
                binding = (
                    kwargs.get("owner"),
                    kwargs.get("workspace_id"),
                    kwargs.get("selector_kind"),
                    kwargs.get("selector"),
                    kwargs.get("preview_token"),
                )
                existing = self.operations.get(operation_id)
                if existing is not None and existing["binding"] != binding:
                    raise MemoryRequestRejectedError(
                        "operation id belongs to another commit"
                    )
                result = {
                    "state": "committed",
                    "tombstone_id": f"tombstone:{kwargs['selector']}",
                    "recover_until": "2099-01-01T00:00:00+00:00",
                    "closure": self.closure(kwargs["selector"]),
                    "binding": binding,
                }
                self.operations[operation_id] = result
                return result
            raise AssertionError(action)

    provider = Provider()
    skills = SkillsManager(str(tmp_path))
    created = skills.add_skill(
        name="derived-before-operation-rebind",
        description="must stay quarantined after a replay rejection",
        source="memory-promotion",
        source_memory_ids=["shared-memory"],
        owner="alice",
    )
    lifecycle = MemoryLifecycleCoordinator(
        provider,
        skills,
    )
    first = await lifecycle.forget(
        "preview",
        owner="alice",
        selector_kind="record_id",
        selector="memory-1",
    )
    second = await lifecycle.forget(
        "preview",
        owner="alice",
        selector_kind="record_id",
        selector="memory-2",
    )
    await lifecycle.forget(
        "commit",
        owner="alice",
        selector_kind="record_id",
        selector="memory-2",
        preview_token=second["token"],
    )
    first_parts = unpack_forget_token(first["token"], "preview")
    second_parts = unpack_forget_token(second["token"], "preview")
    rebound = pack_forget_token(
        "preview",
        provider=first_parts["provider"],
        skills=first_parts["skills"],
        operation=second_parts["operation"],
    )

    with pytest.raises(
        MemoryRequestRejectedError,
        match="another commit",
    ):
        await lifecycle.forget(
            "commit",
            owner="alice",
            selector_kind="record_id",
            selector="memory-1",
            preview_token=rebound,
        )
    assert provider.operations[second_parts["operation"]]["binding"][3] == (
        "memory-2"
    )
    assert skills.load(owner="alice") == []
    info = lifecycle.skill_forget.operation_info(
        second_parts["operation"],
        owner="alice",
    )
    assert info["state"] == "committed"
    assert info["skill_ids"] == [created["skill_id"]]


@pytest.mark.asyncio
async def test_cancelled_replay_status_never_starts_destructive_commit(tmp_path):
    class Provider:
        commit_called = False

        async def forget(self, action, **kwargs):
            if action == "preview":
                return {
                    "token": "preview-before-cancel",
                    "closure": {
                        "raw_ids": [],
                        "candidate_ids": [],
                        "curated_ids": ["memory-1"],
                        "graph_node_ids": [],
                    },
                }
            if action == "status":
                raise asyncio.CancelledError
            if action == "commit":
                self.commit_called = True
                raise AssertionError("cancelled preflight must not commit")
            raise AssertionError(action)

    provider = Provider()
    skills = SkillsManager(str(tmp_path))
    created = skills.add_skill(
        name="still-live-after-cancel",
        description="preflight cancellation is non-destructive",
        source="memory-promotion",
        source_memory_ids=["memory-1"],
        owner="alice",
    )
    lifecycle = MemoryLifecycleCoordinator(provider, skills)
    preview = await lifecycle.forget(
        "preview",
        owner="alice",
        selector_kind="record_id",
        selector="memory-1",
    )

    with pytest.raises(asyncio.CancelledError):
        await lifecycle.forget(
            "commit",
            owner="alice",
            selector_kind="record_id",
            selector="memory-1",
            preview_token=preview["token"],
        )

    assert provider.commit_called is False
    assert [row["skill_id"] for row in skills.load(owner="alice")] == [
        created["skill_id"]
    ]
    assert lifecycle.skill_forget.pending_operations() == []


@pytest.mark.asyncio
async def test_pending_forget_blocks_new_memory_derived_artifacts(tmp_path):
    class Provider:
        def __init__(self):
            self.commit_entered = asyncio.Event()
            self.release_commit = asyncio.Event()
            self.operations = {}

        async def forget(self, action, **kwargs):
            operation_id = kwargs.get("operation_id")
            closure = {
                "raw_ids": [],
                "candidate_ids": [],
                "curated_ids": ["memory-1"],
                "graph_node_ids": [],
            }
            if action == "preview":
                return {"token": "preview-1", "closure": closure}
            if action == "status":
                return self.operations.get(operation_id, {"state": "absent"})
            if action == "commit":
                self.commit_entered.set()
                await self.release_commit.wait()
                result = {
                    "state": "committed",
                    "tombstone_id": "provider-tombstone",
                    "recover_until": "2099-01-01T00:00:00+00:00",
                    "closure": closure,
                }
                self.operations[operation_id] = result
                return result
            if action == "restore":
                self.operations[operation_id]["state"] = "restored"
                return {"restored": True}
            raise AssertionError(action)

    provider = Provider()
    skills = SkillsManager(str(tmp_path))
    lifecycle = MemoryLifecycleCoordinator(provider, skills)
    preview = await lifecycle.forget(
        "preview",
        owner="alice",
        selector_kind="record_id",
        selector="memory-1",
    )
    committing = asyncio.create_task(lifecycle.forget(
        "commit",
        owner="alice",
        selector_kind="record_id",
        selector="memory-1",
        preview_token=preview["token"],
    ))
    await provider.commit_entered.wait()

    with pytest.raises(ValueError, match="unfinished memory lifecycle"):
        skills.add_skill(
            name="too-late-derived-skill",
            description="must not cross the forget boundary",
            source="memory-promotion",
            source_memory_ids=["memory-1"],
            owner="alice",
        )

    provider.release_commit.set()
    committed = await committing
    await lifecycle.forget(
        "restore",
        owner="alice",
        tombstone_id=committed["tombstone_id"],
    )
    assert skills.add_skill(
        name="derived-after-restore",
        description="allowed after the whole closure is restored",
        source="memory-promotion",
        source_memory_ids=["memory-1"],
        owner="alice",
    )["name"] == "derived-after-restore"


@pytest.mark.asyncio
async def test_lost_retention_reply_reconciles_from_durable_status(tmp_path):
    class Provider:
        status_available = False

        def __init__(self):
            self.operations = {}

        async def retention(self, action, **kwargs):
            closure = {
                "raw_ids": [],
                "candidate_ids": [],
                "curated_ids": ["memory-1"],
                "graph_node_ids": [],
            }
            operation_id = kwargs.get("operation_id")
            if action == "preview_expire":
                return {"token": "retention-token", "closure": closure}
            if action == "expire":
                self.operations[operation_id] = {
                    "operation_id": operation_id,
                    "state": "committed",
                    "closure": closure,
                }
                raise ConnectionError("retention reply was lost")
            if action == "status":
                if not self.status_available:
                    raise ConnectionError("retention status unavailable")
                return self.operations.get(
                    operation_id,
                    {"state": "absent", "closure": None},
                )
            raise AssertionError(action)

    provider = Provider()
    skills = SkillsManager(str(tmp_path))
    skills.add_skill(
        name="retention-derived",
        description="derived",
        source="memory-promotion",
        source_memory_ids=["memory-1"],
        owner="alice",
    )
    lifecycle = MemoryLifecycleCoordinator(provider, skills)

    with pytest.raises(ConnectionError, match="retention reply"):
        await lifecycle.expire_retention(owner="alice")
    assert skills.load(owner="alice") == []
    pending = lifecycle.skill_forget.pending_operations()
    assert len(pending) == 1
    assert pending[0]["operation_kind"] == "retention"
    assert pending[0]["state"] == "prepared"

    provider.status_available = True
    restarted = MemoryLifecycleCoordinator(
        provider,
        SkillsManager(str(tmp_path)),
    )
    assert await restarted.reconcile() == {
        "rolled_back": 0,
        "committed": 1,
        "restored": 0,
        "errors": 0,
    }
    assert restarted.skill_forget.pending_operations() == []


@pytest.mark.asyncio
async def test_nonrecoverable_forget_replay_keeps_local_operation_identity(tmp_path):
    class Provider:
        def __init__(self):
            self.operations = {}

        async def forget(self, action, **kwargs):
            operation_id = kwargs.get("operation_id")
            closure = {
                "raw_ids": [],
                "candidate_ids": [],
                "curated_ids": ["memory-1"],
                "graph_node_ids": [],
            }
            if action == "preview":
                return {"token": "forget-preview", "closure": closure}
            if action == "status":
                return self.operations.get(operation_id, {"state": "absent"})
            if action == "commit":
                result = self.operations.setdefault(
                    operation_id,
                    {
                        "operation_id": operation_id,
                        "state": "committed",
                        "tombstone_id": "provider-forget",
                        "recover_until": None,
                        "closure": closure,
                    },
                )
                return dict(result)
            raise AssertionError(action)

    skills = SkillsManager(str(tmp_path))
    created = skills.add_skill(
        name="nonrecoverable-derived",
        description="derived",
        source_memory_ids=["memory-1"],
        owner="alice",
    )
    lifecycle = MemoryLifecycleCoordinator(Provider(), skills)
    preview = await lifecycle.forget(
        "preview",
        owner="alice",
        selector_kind="record_id",
        selector="memory-1",
    )
    first = await lifecycle.forget(
        "commit",
        owner="alice",
        selector_kind="record_id",
        selector="memory-1",
        preview_token=preview["token"],
    )
    replay = await lifecycle.forget(
        "commit",
        owner="alice",
        selector_kind="record_id",
        selector="memory-1",
        preview_token=preview["token"],
    )

    operation_id = preview["operation_id"]
    assert replay["tombstone_id"] == first["tombstone_id"]
    assert replay["closure"]["skill_ids"] == [created["skill_id"]]
    assert skills.load(owner="alice") == []
    assert lifecycle.skill_forget.pending_operations() == []
    assert lifecycle.skill_forget.operation_info(
        operation_id,
        owner="alice",
    )["state"] == "committed_irrecoverable"


@pytest.mark.asyncio
async def test_retention_replay_keeps_exact_operation_and_derived_closure(tmp_path):
    class Provider:
        def __init__(self):
            self.operations = {}

        async def retention(self, action, **kwargs):
            operation_id = kwargs.get("operation_id")
            closure = {
                "raw_ids": [],
                "candidate_ids": [],
                "curated_ids": ["memory-1"],
                "graph_node_ids": [],
            }
            if action == "preview_expire":
                return {"token": "retention-preview", "closure": closure}
            if action == "status":
                return self.operations.get(operation_id, {"state": "absent"})
            if action == "expire":
                result = self.operations.setdefault(
                    operation_id,
                    {
                        "operation_id": operation_id,
                        "state": "committed",
                        "closure": closure,
                    },
                )
                return dict(result)
            raise AssertionError(action)

    skills = SkillsManager(str(tmp_path))
    created = skills.add_skill(
        name="retention-replay-derived",
        description="derived",
        source_memory_ids=["memory-1"],
        owner="alice",
    )
    lifecycle = MemoryLifecycleCoordinator(Provider(), skills)
    preview = await lifecycle.retention(
        "preview_expire",
        owner="alice",
    )
    first = await lifecycle.retention(
        "expire",
        owner="alice",
        preview_token=preview["token"],
        operation_id=preview["operation_id"],
    )
    replay = await lifecycle.retention(
        "expire",
        owner="alice",
        preview_token=preview["token"],
        operation_id=preview["operation_id"],
    )

    assert replay == first
    assert replay["closure"]["skill_ids"] == [created["skill_id"]]
    assert lifecycle.skill_forget.operation_info(
        preview["operation_id"],
        owner="alice",
    )["state"] == "committed_irrecoverable"


@pytest.mark.asyncio
async def test_cancelled_provider_commit_schedules_live_reconciliation(
    tmp_path,
    monkeypatch,
):
    class Provider:
        def __init__(self):
            self.operations = {}

        async def forget(self, action, **kwargs):
            operation_id = kwargs.get("operation_id")
            closure = {
                "raw_ids": [],
                "candidate_ids": [],
                "curated_ids": ["memory-1"],
                "graph_node_ids": [],
            }
            if action == "preview":
                return {"token": "cancel-preview", "closure": closure}
            if action == "status":
                return self.operations.get(operation_id, {"state": "absent"})
            if action == "commit":
                self.operations[operation_id] = {
                    "operation_id": operation_id,
                    "state": "committed",
                    "tombstone_id": "provider-cancelled",
                    "recover_until": "2099-01-01T00:00:00+00:00",
                    "closure": closure,
                }
                raise asyncio.CancelledError
            raise AssertionError(action)

    monkeypatch.setenv("OPEN_CLANK_MEMORY_RECONCILE_DELAY_SECONDS", "0")
    skills = SkillsManager(str(tmp_path))
    created = skills.add_skill(
        name="cancelled-commit-derived",
        description="derived",
        source_memory_ids=["memory-1"],
        owner="alice",
    )
    lifecycle = MemoryLifecycleCoordinator(Provider(), skills)
    preview = await lifecycle.forget(
        "preview",
        owner="alice",
        selector_kind="record_id",
        selector="memory-1",
    )
    with pytest.raises(asyncio.CancelledError):
        await lifecycle.forget(
            "commit",
            owner="alice",
            selector_kind="record_id",
            selector="memory-1",
            preview_token=preview["token"],
        )
    tasks = list(lifecycle._reconcile_tasks)
    assert tasks
    await asyncio.gather(*tasks)

    assert skills.load(owner="alice") == []
    assert lifecycle.skill_forget.operation_info(
        preview["operation_id"],
        owner="alice",
    )["skill_ids"] == [created["skill_id"]]


@pytest.mark.asyncio
async def test_provider_restored_status_recovers_local_payload_after_deadline(tmp_path):
    class Provider:
        def __init__(self):
            self.operations = {}

        async def forget(self, action, **kwargs):
            operation_id = kwargs.get("operation_id")
            closure = {
                "raw_ids": [],
                "candidate_ids": [],
                "curated_ids": ["memory-1"],
                "graph_node_ids": [],
            }
            if action == "preview":
                return {"token": "expired-preview", "closure": closure}
            if action == "status":
                return self.operations.get(operation_id, {"state": "absent"})
            if action == "commit":
                result = {
                    "operation_id": operation_id,
                    "state": "committed",
                    "tombstone_id": "provider-expired",
                    "recover_until": "2000-01-01T00:00:00+00:00",
                    "closure": closure,
                }
                self.operations[operation_id] = result
                return dict(result)
            raise AssertionError(action)

    provider = Provider()
    skills = SkillsManager(str(tmp_path))
    created = skills.add_skill(
        name="restored-after-lost-reply",
        description="derived",
        source_memory_ids=["memory-1"],
        owner="alice",
    )
    lifecycle = MemoryLifecycleCoordinator(provider, skills)
    preview = await lifecycle.forget(
        "preview",
        owner="alice",
        selector_kind="record_id",
        selector="memory-1",
    )
    committed = await lifecycle.forget(
        "commit",
        owner="alice",
        selector_kind="record_id",
        selector="memory-1",
        preview_token=preview["token"],
    )
    provider.operations[preview["operation_id"]]["state"] = "restored"

    restored = await lifecycle.forget(
        "restore",
        owner="alice",
        tombstone_id=committed["tombstone_id"],
    )
    assert restored["restored"] is True
    assert [row["skill_id"] for row in skills.load(owner="alice")] == [
        created["skill_id"]
    ]
