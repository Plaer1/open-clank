"""Mounted REST plumbing; fake C04 accessor, NOT native authority qualification."""
import asyncio
import os
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI


@pytest.fixture
def mounted(monkeypatch):
    # Validate startup isolation BEFORE importing application route owners.
    root = Path(os.environ["OPEN_CLANK_DATA_DIR"]).resolve()
    assert root.name.startswith("openclank-s03-s06-projection.")
    for key in ("ODYSSEUS_DATA_DIR", "OPEN_CLANK_AUTHORITY_DB_PATH", "OPEN_CLANK_AUTHORITY_AUTH_PATH", "FM_DB_PATH", "OPENCLANK_CONVERSATION_ARCHIVE_DB"):
        assert Path(os.environ[key]).resolve().is_relative_to(root)
    assert os.environ["ODYSSEUS_INPROCESS_POLLERS"] == "0"
    assert os.environ["PYTHONDONTWRITEBYTECODE"] == "1"
    assert os.environ["DATABASE_URL"] == "sqlite:///:memory:"
    assert "OPEN_CLANK_AGENT_HOME" not in os.environ and "THESIUS_AGENT_HOME" not in os.environ
    from routes.memory.memory_routes import setup_memory_routes

    calls = []
    response = {"schema": "open-clank.code-query/v1", "operation": "overview", "receipt": None,
        "hits": [], "edges": [], "paths": [], "facets": {}, "counts": [], "omissions": [],
        "capabilities": [], "operation_capabilities": [], "channels": [], "reasons": [], "partial": False, "degraded": False,
        "next_cursor": None, "error": None, "cost": {}}
    from src.openclank import code_query

    async def invoke(**kwargs):
        calls.append(kwargs)
        return response

    # Replace only the accessor seam, preserving the real request-budget
    # constants imported by the mounted projection (including standalone runs).
    monkeypatch.setattr(code_query, "query_code", invoke)
    app = FastAPI()
    app.state.auth_manager = SimpleNamespace(users={"alice": {}, "bob": {}})
    app.state.files_policy_repository = object()  # Explicit fake seam, never default DB.
    provider = object()

    @app.middleware("http")
    async def identity(request, call_next):
        request.state.current_user = request.headers.get("x-fixture-owner", "alice")
        request.state.authenticated = request.headers.get("x-fixture-auth", "yes") == "yes"
        return await call_next(request)

    app.include_router(setup_memory_routes(SimpleNamespace(), SimpleNamespace(), memory_provider=provider,
        memory_lifecycle=SimpleNamespace(skill_forget=None)))
    return app, calls, response, provider


def post(app, body, **kwargs):
    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://fixture") as client:
            return await client.post("/api/memory/code/graph", json=body, **kwargs)
    return asyncio.run(run())


def test_real_mounted_route_uses_trusted_identity_repository_and_reserved_budget(mounted):
    app, calls, _, provider = mounted
    response = post(app, {"workspace_id": "selected-workspace", "query": {"operation": "catalog"}}, headers={"x-fixture-owner": "bob"})
    assert response.status_code == 200
    assert len(calls) == 1
    call = calls[0]
    assert call["owner"] == "bob" and call["workspace_id"] == "selected-workspace"
    assert call["repository"] is app.state.files_policy_repository and call["provider"] is provider
    assert call["auth_manager"] is app.state.auth_manager
    assert call["purpose"] == "app_folder" and call["session"] is None
    assert call["query"]["budgets"]["bytes"] == 65536 - 1024
    assert call["query"]["budgets"]["tokens"] == 8192 - 1024
    assert response.json()["domain"] == "code"
    assert response.json()["cost"]["serialized_bytes"] == len(response.content)


@pytest.mark.parametrize("body", [
    {"workspace_id": "ws", "query": {}, "owner": "bob"},
    {"workspace_id": "ws", "query": {"root": "/private/root"}},
    {"workspace_id": "ws", "query": {"operation": "admit"}},
    {"workspace_id": "ws", "query": {"fields": ["credentials"]}},
    {"workspace_id": "ws", "query": {"budgets": {"bytes": 1024}}},
])
def test_route_refuses_client_authority_and_invalid_requests_before_accessor(mounted, body):
    app, calls, _, _ = mounted
    assert post(app, body).status_code == 422
    assert calls == []


def test_route_requires_authenticated_account_and_existing_authority(mounted):
    app, calls, _, _ = mounted
    body = {"workspace_id": "ws", "query": {}}
    assert post(app, body, headers={"x-fixture-auth": "no"}).status_code == 401
    assert post(app, body, headers={"x-fixture-owner": "unknown"}).status_code == 401
    app.state.files_policy_repository = None
    assert post(app, body).status_code == 503
    assert calls == []


def test_denied_and_cancelled_envelopes_do_not_turn_into_empty_success(mounted):
    app, calls, result, _ = mounted
    for code in ("denied", "cancelled", "stale_cursor", "unsupported", "unavailable"):
        result["error"] = {"code": code, "message": "Bounded public guidance"}
        response = post(app, {"workspace_id": "ws", "query": {}})
        assert response.status_code == 200
        assert response.json()["error"]["code"] == code and response.json()["receipt"] is None
    assert len(calls) == 5


def test_cursor_receipt_filter_and_fields_are_forwarded_without_new_authority(mounted):
    app, calls, _, _ = mounted
    query = {"operation": "facets", "cursor": "opaque-bound-cursor", "expected_receipt": {"head_generation": "g2"}, "fields": [], "filter": {"path_prefix": "src/"}}
    assert post(app, {"workspace_id": "ws", "query": query}).status_code == 200
    for key in ("cursor", "expected_receipt", "fields", "filter"):
        assert calls[0]["query"][key] == query[key]


@pytest.mark.parametrize("key,value", [("paths", [None]), ("counts", [None]), ("cost", []), ("edges", [{}])])
def test_malformed_backend_data_is_bounded_typed_invalid_evidence(mounted, key, value):
    app, _, result, _ = mounted
    result[key] = value
    response = post(app, {"workspace_id": "ws", "query": {}})
    assert response.status_code == 502
    assert response.json()["detail"]["code"] == "invalid_evidence"
    assert "/private" not in response.text
