"""Memory routes must owner-scope caller-supplied session ids.

SessionManager.get_session returns any session by id (no owner scoping). The
/api/memory extract, audit, import, and by-session handlers accept a
caller-supplied session id, so without an ownership gate a user could target
another tenant's session and leak their chat history, session-scoped LLM
credentials, or session title.
"""
import asyncio
import io
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException, UploadFile

import routes.memory_routes as mr
from src.request_models import MemoryAddRequest
from src.openclank.modality_facade import ManagedTextCompletionError


def _route(router, path, method):
    for r in router.routes:
        if r.path == path and method in getattr(r, "methods", set()):
            return r.endpoint
    raise AssertionError(path)


class _StubProvider:
    """Provider-always: routes have no native fallback, so every router
    fixture carries a provider."""

    provider_id = "stub"

    def __init__(self, records=None):
        self.records = records or []
        self.remember_calls = []
        self.review_calls = []

    async def list_memories(self, *, owner=None, limit=1000):
        return list(self.records)

    async def recall(self, query, *, owner=None, top_k=20):
        return []

    async def remember(self, text, **kwargs):
        self.remember_calls.append((text, kwargs))
        return SimpleNamespace(id="m_new")

    async def review_candidate(self, candidate_id, **kwargs):
        self.review_calls.append((candidate_id, kwargs))
        return {"ok": True, "candidate_id": candidate_id}


def _record(**overrides):
    base = dict(
        id="m1", text="Alice note", timestamp=1, category="fact", source="user",
        owner="alice", session_id=None, pinned=False, metadata={},
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _router(monkeypatch, caller, provider=None):
    monkeypatch.setattr(mr, "get_current_user", lambda request: caller, raising=False)
    monkeypatch.setattr(mr, "require_user", lambda request: caller, raising=False)
    sm = MagicMock()
    sm.sessions = {}
    sm.get_session = lambda sid: SimpleNamespace(
        owner="alice", name="Secret project", endpoint_url="http://x", model="m",
        headers={"Authorization": "Bearer victim-secret"},
        get_context_messages=lambda: [],
    )
    mem = MagicMock()
    mem.load = lambda owner=None: []
    return mr.setup_memory_routes(mem, sm, memory_provider=provider or _StubProvider())


def _request(user):
    return SimpleNamespace(
        state=SimpleNamespace(current_user=user),
        app=SimpleNamespace(state=SimpleNamespace(auth_manager=None)),
    )


def _json_request(user, body):
    request = _request(user)

    async def json():
        return body

    request.json = json
    return request


def _upload(name="memories.json"):
    return UploadFile(
        filename=name,
        file=io.BytesIO(b'[{"text": "Project Phoenix uses Python", "category": "project"}]'),
    )


def _allow_memory_management(monkeypatch):
    monkeypatch.setattr("src.auth_helpers.require_privilege", lambda request, privilege: "alice")


def _configure_memory_route(monkeypatch, *, configured=True):
    operation_router = MagicMock()
    operation_router.route_preflight.return_value = {
        "configured": configured,
        "binding_revision": 1 if configured else 0,
        "eligible_routes": [
            {
                "model_route_id": "memory-route",
                "display_name": "memory-model",
                "connection_label": "Memory model",
            }
        ],
    }
    monkeypatch.setattr(
        "src.openclank.modality_facade.managed_route_preflight",
        operation_router.route_preflight,
    )
    return operation_router


def test_extract_rejects_other_users_session(monkeypatch):
    router = _router(monkeypatch, caller="bob")
    extract = _route(router, "/api/memory/extract", "POST")
    with pytest.raises(HTTPException) as exc:
        asyncio.run(extract(request=None, session="alice-sess"))
    assert exc.value.status_code == 404


def test_by_session_rejects_other_users_session(monkeypatch):
    router = _router(monkeypatch, caller="bob")
    gbs = _route(router, "/api/memory/by-session/{session_id}", "GET")
    with pytest.raises(HTTPException) as exc:
        asyncio.run(gbs(request=None, session_id="alice-sess"))
    assert exc.value.status_code == 404


def test_owner_can_access_own_session(monkeypatch):
    router = _router(monkeypatch, caller="alice")
    gbs = _route(router, "/api/memory/by-session/{session_id}", "GET")
    out = asyncio.run(gbs(request=None, session_id="alice-sess"))
    assert out["session_name"] == "Secret project"


def test_provider_memory_list_stays_on_request_event_loop(monkeypatch):
    expected_loop = None

    class Provider:
        provider_id = "frankenmemory"

        async def list_memories(self, *, owner=None, limit=1000):
            assert asyncio.get_running_loop() is expected_loop
            return [SimpleNamespace(
                id="m_1",
                text="Alice note",
                timestamp=1,
                category="fact",
                source="user",
                owner=owner,
                session_id=None,
                pinned=False,
                metadata={},
            )]

    monkeypatch.setattr(mr, "get_current_user", lambda request: "alice")
    router = mr.setup_memory_routes(MagicMock(), MagicMock(), memory_provider=Provider())
    get_memory = _route(router, "/api/memory", "GET")

    async def invoke():
        nonlocal expected_loop
        expected_loop = asyncio.get_running_loop()
        return await get_memory(request=None)

    out = asyncio.run(invoke())
    assert out["provider"] == "frankenmemory"
    assert out["memory"][0]["id"] == "m_1"


def test_audit_owned_session_uses_managed_utility_route(monkeypatch):
    memory_manager = MagicMock()
    provider = _StubProvider()
    session_headers = {"Authorization": "Bearer session"}
    session_manager = MagicMock()
    session_manager.get_session.return_value = SimpleNamespace(
        owner="alice",
        endpoint_url="http://session.example/v1/chat/completions",
        model="session-model",
        headers=session_headers,
    )
    router = mr.setup_memory_routes(memory_manager, session_manager, memory_provider=provider)
    audit_route = _route(router, "/api/memory/audit", "POST")

    audit_calls = []

    async def fake_audit_provider_memories(
        provider_arg,
        owner=None,
        memory_lifecycle=None,
    ):
        audit_calls.append((
            provider_arg,
            owner,
            memory_lifecycle,
        ))
        return {
            "ok": True,
            "status": "applied",
            "before": 2,
            "after": 1,
            "removed": 1,
            "updated": 0,
            "applied": True,
            "already_tidy": False,
        }

    monkeypatch.setattr(mr, "audit_provider_memories", fake_audit_provider_memories)
    monkeypatch.setattr(
        mr,
        "SessionLocal",
        lambda: (_ for _ in ()).throw(AssertionError("legacy provider lookup should not run")),
    )

    out = asyncio.run(audit_route(request=_request("alice"), session="session-1"))

    session_manager.get_session.assert_called_once_with("session-1")
    assert audit_calls == [(
        provider,
        "alice",
        router.memory_lifecycle,
    )]
    assert out["ok"] is True
    assert out["removed"] == 1


def test_audit_failure_is_a_typed_non_success_response(monkeypatch):
    memory_manager = MagicMock()
    provider = _StubProvider()
    session_manager = MagicMock()
    router = mr.setup_memory_routes(memory_manager, session_manager, memory_provider=provider)
    audit_route = _route(router, "/api/memory/audit", "POST")

    async def fake_audit_provider_memories(*_args, **_kwargs):
        return {
            "ok": False,
            "status": "failed",
            "before": 7,
            "after": 7,
            "removed": 0,
            "updated": 0,
            "applied": False,
            "already_tidy": False,
            "error": {
                "code": "empty_model_output",
                "message": "The memory model returned no usable Tidy result. No memories were changed.",
            },
        }

    monkeypatch.setattr(mr, "audit_provider_memories", fake_audit_provider_memories)
    response = asyncio.run(audit_route(request=_request("alice"), session=None))

    assert response.status_code == 502
    payload = json.loads(response.body)
    assert payload["ok"] is False
    assert payload["error"]["code"] == "empty_model_output"


def test_add_memory_rejects_other_users_session(monkeypatch):
    memory_manager = MagicMock()
    session_manager = MagicMock()
    provider = _StubProvider()
    router = mr.setup_memory_routes(
        memory_manager=memory_manager,
        session_manager=session_manager,
        memory_provider=provider,
    )
    add_memory = _route(router, "/api/memory/add", "POST")

    memory_manager.find_duplicates.return_value = False
    session_manager.get_session.return_value = SimpleNamespace(owner="bob", name="Bob session")

    with pytest.raises(HTTPException) as exc:
        asyncio.run(
            add_memory(
                request=_request("alice"),
                memory_data=MemoryAddRequest(
                    text="Alice note",
                    category="fact",
                    source="user",
                    session_id="bob-session",
                ),
            )
        )

    assert exc.value.status_code == 404
    assert exc.value.detail == "Session not found"
    session_manager.get_session.assert_called_once_with("bob-session")
    assert provider.remember_calls == []


def test_add_memory_stages_candidate_in_review_first_mode(monkeypatch):
    import routes.prefs_routes as prefs_mod

    _allow_memory_management(monkeypatch)
    monkeypatch.setattr(
        prefs_mod,
        "_load_for_user",
        lambda _owner: {"memory_mode": "manual"},
    )
    memory_manager = MagicMock()
    memory_manager.find_duplicates.return_value = []
    provider = _StubProvider()
    router = mr.setup_memory_routes(
        memory_manager=memory_manager,
        session_manager=MagicMock(),
        memory_provider=provider,
    )
    add_memory = _route(router, "/api/memory/add", "POST")

    out = asyncio.run(
        add_memory(
            request=_request("alice"),
            memory_data=MemoryAddRequest(
                text="Alice reviewed note",
                category="fact",
                source="user",
            ),
        )
    )

    assert out["pending_review"] is True
    assert out["candidate_id"] == "m_new"
    assert provider.remember_calls[0][1]["capture_mode"] == "review_only"
    assert provider.remember_calls[0][1]["workspace_id"] == "global"


def test_add_memory_rejects_client_workspace_override(monkeypatch):
    _allow_memory_management(monkeypatch)
    provider = _StubProvider()
    router = _router(monkeypatch, caller="alice", provider=provider)
    add_memory = _route(router, "/api/memory/add", "POST")

    with pytest.raises(HTTPException) as exc:
        asyncio.run(
            add_memory(
                request=_request("alice"),
                memory_data=MemoryAddRequest(
                    text="must stay in canonical scope",
                    workspace_id="other-tenant-workspace",
                ),
            )
        )

    assert exc.value.status_code == 400
    assert provider.remember_calls == []


def test_candidate_review_derives_server_workspace(monkeypatch):
    _allow_memory_management(monkeypatch)
    provider = _StubProvider()
    router = _router(monkeypatch, caller="alice", provider=provider)
    review = _route(router, "/api/memory/candidate/{candidate_id}/review", "POST")

    result = asyncio.run(
        review(
            request=_json_request("alice", {"accept": True}),
            candidate_id="candidate-1",
        )
    )

    assert result["ok"] is True
    assert provider.review_calls == [(
        "candidate-1",
        {
            "accept": True,
            "reason": "approved_by_user",
            "owner": "alice",
            "workspace_id": "global",
        },
    )]


def test_candidate_review_rejects_client_workspace_override(monkeypatch):
    _allow_memory_management(monkeypatch)
    provider = _StubProvider()
    router = _router(monkeypatch, caller="alice", provider=provider)
    review = _route(router, "/api/memory/candidate/{candidate_id}/review", "POST")

    with pytest.raises(HTTPException) as exc:
        asyncio.run(
            review(
                request=_json_request(
                    "alice",
                    {"accept": True, "workspace_id": "other-workspace"},
                ),
                candidate_id="candidate-1",
            )
        )

    assert exc.value.status_code == 400
    assert provider.review_calls == []


def test_add_memory_refuses_off_mode(monkeypatch):
    import routes.prefs_routes as prefs_mod

    _allow_memory_management(monkeypatch)
    monkeypatch.setattr(
        prefs_mod,
        "_load_for_user",
        lambda _owner: {"memory_mode": "off"},
    )
    provider = _StubProvider()
    router = mr.setup_memory_routes(
        memory_manager=MagicMock(),
        session_manager=MagicMock(),
        memory_provider=provider,
    )
    add_memory = _route(router, "/api/memory/add", "POST")

    with pytest.raises(HTTPException) as exc:
        asyncio.run(
            add_memory(
                request=_request("alice"),
                memory_data=MemoryAddRequest(text="must not save"),
            )
        )

    assert exc.value.status_code == 403
    assert provider.remember_calls == []


def test_timeline_does_not_expose_other_users_session_name():
    memory_manager = MagicMock()
    session_manager = MagicMock()
    session_manager.sessions = {"bob-session": object()}
    session_manager.get_session.return_value = SimpleNamespace(owner="bob", name="Bob roadmap")
    provider = _StubProvider(records=[_record(session_id="bob-session")])
    router = mr.setup_memory_routes(memory_manager, session_manager, memory_provider=provider)
    timeline = _route(router, "/api/memory/timeline", "GET")

    out = asyncio.run(timeline(request=_request("alice")))

    assert out["timeline"][0]["session_name"] == "Unknown"


def test_import_missing_session_uses_memory_route(monkeypatch):
    _allow_memory_management(monkeypatch)
    route_preflight = _configure_memory_route(monkeypatch)
    memory_manager = MagicMock()
    session_manager = MagicMock()
    session_manager.get_session.side_effect = KeyError
    completion = AsyncMock(
        return_value='[{"text": "Project Phoenix uses Python", "category": "project"}]'
    )
    monkeypatch.setattr(mr, "complete_text", completion)
    router = mr.setup_memory_routes(memory_manager, session_manager)
    import_memories = _route(router, "/api/memory/import", "POST")

    out = asyncio.run(import_memories(request=_request("alice"), session="missing-session", file=_upload("memories.md")))

    assert out == {
        "suggestions": [{"text": "Project Phoenix uses Python", "category": "project"}],
        "filename": "memories.md",
    }
    session_manager.get_session.assert_called_once_with("missing-session")
    completion.assert_awaited_once()
    assert completion.await_args.kwargs["owner"] == "alice"
    assert completion.await_args.kwargs["purpose"] == "memory"
    assert "endpoint_url" not in completion.await_args.kwargs
    assert "headers" not in completion.await_args.kwargs
    prompt = completion.await_args.kwargs["messages"][0]["content"]
    assert "use first-person wording" in prompt
    assert "never a generic label such as 'the AI'" in prompt
    assert "%USER%" in prompt
    assert "user interface" in prompt
    route_preflight.route_preflight.assert_called_once_with(
        owner="alice",
        purpose="memory",
        operation="chat.complete",
    )


def test_import_preserves_actionable_managed_model_failure(monkeypatch):
    _allow_memory_management(monkeypatch)
    _configure_memory_route(monkeypatch)
    monkeypatch.setattr(
        "src.frankenmemory_v2.mirror_import_job",
        MagicMock(),
    )
    completion = AsyncMock(
        side_effect=ManagedTextCompletionError(
            code="model_not_found",
            committed=False,
        )
    )
    monkeypatch.setattr(mr, "complete_text", completion)
    router = mr.setup_memory_routes(MagicMock(), MagicMock())
    import_memories = _route(router, "/api/memory/import", "POST")

    with pytest.raises(HTTPException) as exc:
        asyncio.run(
            import_memories(
                request=_request("alice"),
                session=None,
                file=_upload("memories.md"),
            )
        )

    assert exc.value.status_code == 409
    assert exc.value.detail == {
        "code": "MEMORY_MODEL_UNAVAILABLE",
        "message": "The selected model is not available in the managed runtime.",
        "provider_error_code": "model_not_found",
        "retryable": False,
        "phase": "model_execution",
        "required_purpose": "memory",
        "required_operation": "chat.complete",
        "settings_target": "ai",
    }


def test_import_empty_model_output_is_typed_and_uses_large_extraction_budget(monkeypatch):
    _allow_memory_management(monkeypatch)
    _configure_memory_route(monkeypatch)
    mirror_import_job = MagicMock()
    monkeypatch.setattr(
        "src.frankenmemory_v2.mirror_import_job",
        mirror_import_job,
    )
    completion = AsyncMock(return_value="")
    monkeypatch.setattr(mr, "complete_text", completion)
    router = mr.setup_memory_routes(MagicMock(), MagicMock())
    import_memories = _route(router, "/api/memory/import", "POST")

    with pytest.raises(HTTPException) as exc:
        asyncio.run(
            import_memories(
                request=_request("alice"),
                session=None,
                file=_upload("memories.md"),
            )
        )

    assert exc.value.status_code == 422
    assert exc.value.detail == {
        "code": "MEMORY_EXTRACTION_EMPTY",
        "message": (
            "The selected Memory model finished without returning import "
            "suggestions. Retry the import or choose a different Memory model."
        ),
        "retryable": True,
        "phase": "extraction",
        "required_purpose": "memory",
        "required_operation": "chat.complete",
        "settings_target": "ai",
    }
    assert completion.await_args.kwargs["max_output_tokens"] == 8192
    assert mirror_import_job.call_args.kwargs["state"] == "failed_terminal"


def test_import_pdf_preflights_before_document_processing(monkeypatch):
    _allow_memory_management(monkeypatch)
    route_preflight = _configure_memory_route(monkeypatch, configured=False)
    process_pdf = MagicMock(side_effect=AssertionError("PDF parser must not run"))
    monkeypatch.setattr("src.document_processor._process_pdf", process_pdf)
    session_manager = MagicMock()
    router = mr.setup_memory_routes(MagicMock(), session_manager)
    import_memories = _route(router, "/api/memory/import", "POST")

    with pytest.raises(HTTPException) as exc:
        asyncio.run(
            import_memories(
                request=_request("alice"),
                session="alice-session",
                file=_upload("memories.pdf"),
            )
        )

    assert exc.value.status_code == 409
    assert exc.value.detail == {
        "code": "MEMORY_ROUTE_UNCONFIGURED",
        "message": (
            "Memory import needs a Memory-capable model, but neither Memory "
            "nor its Utility/Chat inheritance resolves to an available route. "
            "Choose Memory or Utility under AI Defaults and retry."
        ),
        "retryable": True,
        "phase": "preflight",
        "required_purpose": "memory",
        "required_operation": "chat.complete",
        "settings_target": "ai",
        "binding_revision": 0,
        "eligible_routes": [
            {
                "model_route_id": "memory-route",
                "display_name": "memory-model",
                "connection_label": "Memory model",
            }
        ],
    }
    process_pdf.assert_not_called()
    session_manager.get_session.assert_not_called()
    route_preflight.route_preflight.assert_called_once_with(
        owner="alice",
        purpose="memory",
        operation="chat.complete",
    )


def test_import_foreign_session_uses_same_memory_route(monkeypatch):
    _allow_memory_management(monkeypatch)
    route_preflight = _configure_memory_route(monkeypatch)
    memory_manager = MagicMock()
    session_manager = MagicMock()
    session_manager.get_session.return_value = SimpleNamespace(
        owner="bob",
        endpoint_url="http://bob-llm",
        model="bob-model",
        headers={"Authorization": "Bearer bob-secret"},
    )
    completion = AsyncMock(
        return_value='[{"text": "Project Phoenix uses Python", "category": "project"}]'
    )
    monkeypatch.setattr(mr, "complete_text", completion)
    router = mr.setup_memory_routes(memory_manager, session_manager)
    import_memories = _route(router, "/api/memory/import", "POST")

    out = asyncio.run(import_memories(request=_request("alice"), session="bob-session", file=_upload("memories.md")))

    assert out["suggestions"] == [{"text": "Project Phoenix uses Python", "category": "project"}]
    session_manager.get_session.assert_called_once_with("bob-session")
    assert completion.await_args.kwargs["owner"] == "alice"
    assert completion.await_args.kwargs["purpose"] == "memory"
    route_preflight.route_preflight.assert_called_once_with(
        owner="alice",
        purpose="memory",
        operation="chat.complete",
    )


def test_import_owned_session_still_uses_managed_memory_route(monkeypatch):
    _allow_memory_management(monkeypatch)
    route_preflight = _configure_memory_route(monkeypatch)
    memory_manager = MagicMock()
    session_manager = MagicMock()
    session_manager.get_session.return_value = SimpleNamespace(
        owner="alice",
        endpoint_url="http://alice-llm",
        model="alice-model",
        headers={"X-Session": "alice"},
    )
    completion = AsyncMock(
        return_value='[{"text": "Project Phoenix uses Python", "category": "project"}]'
    )
    monkeypatch.setattr(mr, "complete_text", completion)
    router = mr.setup_memory_routes(memory_manager, session_manager)
    import_memories = _route(router, "/api/memory/import", "POST")

    out = asyncio.run(import_memories(request=_request("alice"), session="alice-session", file=_upload("memories.md")))

    assert out["suggestions"] == [{"text": "Project Phoenix uses Python", "category": "project"}]
    session_manager.get_session.assert_called_once_with("alice-session")
    assert completion.await_args.kwargs["owner"] == "alice"
    assert completion.await_args.kwargs["purpose"] == "memory"
    assert "endpoint_url" not in completion.await_args.kwargs
    assert "headers" not in completion.await_args.kwargs
    route_preflight.route_preflight.assert_called_once_with(
        owner="alice",
        purpose="memory",
        operation="chat.complete",
    )
