"""Mounted canonical map route; fake native provider, real app accessor."""
import asyncio
import json
import os
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI


@pytest.fixture
def mounted(monkeypatch):
    # Refuse an unisolated startup BEFORE importing route/application owners.
    root = Path(os.environ["OPEN_CLANK_DATA_DIR"]).resolve()
    assert root.name.startswith("openclank-s02-visual.")
    for key in ("ODYSSEUS_DATA_DIR", "OPEN_CLANK_AUTHORITY_DB_PATH", "OPEN_CLANK_AUTHORITY_AUTH_PATH", "FM_DB_PATH", "OPENCLANK_CONVERSATION_ARCHIVE_DB"):
        assert Path(os.environ[key]).resolve().is_relative_to(root)
    assert os.environ["ODYSSEUS_INPROCESS_POLLERS"] == "0"
    assert os.environ["PYTHONDONTWRITEBYTECODE"] == "1"
    assert os.environ["DATABASE_URL"] == "sqlite:///:memory:"
    assert "OPEN_CLANK_AGENT_HOME" not in os.environ and "THESIUS_AGENT_HOME" not in os.environ
    from routes.memory.memory_routes import setup_memory_routes
    from src.openclank import code_visual as cv
    calls, state = [], {"changed":False}
    def resolve(**kwargs):
        assert kwargs["purpose"] == "app_folder" and kwargs["session"] is None
        return dict(workspace_id="fixture-workspace", workspace_revision=1, policy_generation=1, origin="app"), (state["changed"],)
    monkeypatch.setattr(cv, "resolve_query_binding", resolve)
    result = json.loads((Path(__file__).resolve().parents[1] / "mcp_servers/frankenmemory/crates/fm-core/tests/fixtures/code_map_preview.json").read_text())
    async def invoke(**kwargs):
        calls.append(kwargs)
        return result
    provider = SimpleNamespace(code_visual=invoke)
    app = FastAPI()
    app.state.auth_manager = SimpleNamespace(users={"alice":{}, "bob":{}})
    app.state.files_policy_repository = object()
    @app.middleware("http")
    async def identity(request, call_next):
        request.state.current_user = request.headers.get("x-fixture-owner", "alice")
        request.state.authenticated = request.headers.get("x-fixture-auth", "yes") == "yes"
        return await call_next(request)
    app.include_router(setup_memory_routes(SimpleNamespace(), SimpleNamespace(), memory_provider=provider,
        memory_lifecycle=SimpleNamespace(skill_forget=None)))
    return app, calls, result, state


def body():
    return {"workspace_id":"fixture-workspace", "visual_request":{"mode":"map", "request":{"schema":"open-clank.code-map/v1", "operation":"overview"}}}


def post(app, value=None, **kwargs):
    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://fixture") as client:
            return await client.post("/api/memory/code/map", **({"json": value} if value is not None else {}), **kwargs)
    return asyncio.run(run())


def test_route_remeasures_actual_response_and_trusted_binding(mounted):
    app, calls, _, _ = mounted
    response = post(app, body())
    assert response.status_code == 200 and response.json()["authority"]["variant"] == "structure_preview"
    assert response.json()["cost"]["serialized_bytes"] == len(response.content)
    assert len(calls) == 1 and calls[0]["binding"]["origin"] == "app"
    assert set(calls[0]) == {"binding", "visual_request"}
    assert calls[0]["visual_request"]["request"]["limits"]["bytes"] == 262144


@pytest.mark.parametrize("change", ["owner", "root", "extra", "query", "bool", "mode"])
def test_route_rejects_authority_mixed_modes_and_types_before_provider(mounted, change):
    app, calls, _, _ = mounted
    value = body()
    if change in {"owner", "root", "extra", "query"}:
        value[change] = "forbidden"
    elif change == "bool":
        value["visual_request"]["request"]["limits"] = {"nodes": True}
    else:
        value["visual_request"]["mode"] = "agent"
    assert post(app, value).status_code == 422 and not calls


@pytest.mark.parametrize("raw", [b'{"workspace_id":"a","workspace_id":"b"}', b'{"a":NaN}', b'[' * 40 + b']' * 40, bytes([255])])
def test_invalid_json_is_bounded_typed_error(mounted, raw):
    app, calls, _, _ = mounted
    response = post(app, content=raw)
    assert response.status_code == 422 and response.json()["error"]["code"] == "invalid" and not calls
    assert "nodes" not in response.json()


def test_stream_byte_limit_auth_and_authority_availability(mounted):
    app, calls, _, _ = mounted
    async def chunks():
        yield b" " * 8192
        yield b" " * 8193
    assert post(app, content=chunks()).status_code == 413
    assert post(app, body(), headers={"x-fixture-auth":"no"}).status_code == 401
    assert post(app, body(), headers={"x-fixture-owner":"missing"}).status_code == 401
    app.state.files_policy_repository = None
    assert post(app, body()).status_code == 503 and not calls


@pytest.mark.parametrize("mutation", ["private", "wrong_scope", "wrong_page", "too_large"])
def test_unsafe_result_never_leaks_or_advances(mounted, mutation):
    app, _, result, _ = mounted
    if mutation == "private":
        result["root_path"] = "/private/secret"
    elif mutation == "wrong_scope":
        result["authority"]["workspace_id"] = "another"
    elif mutation == "wrong_page":
        result["page"]["next_cursor"] = "must-not-advance"
    else:
        result["nodes"][0]["label"] = "x" * 161
    response = post(app, body())
    assert response.status_code == 200 and response.json()["error"]["code"] == "invalid_evidence"
    assert "nodes" not in response.json() and "authority" not in response.json()
    assert "secret" not in response.text and "must-not-advance" not in response.text


def test_backend_error_is_not_success_and_native_message_is_private(mounted):
    app, _, result, _ = mounted
    result.clear()
    result.update(error={"code":"erased", "message":"/private/secret"}, cost={"native_rpc_calls_completed":5})
    response = post(app, body())
    assert response.json()["error"] == {"code":"erased", "message":"Code map unavailable"}
    assert response.json()["cost"]["native_rpc_calls_completed"] == 5
    assert response.json()["cost"]["serialized_bytes"] == len(response.content)


def test_same_route_handles_published_selection_without_agent_query_or_geometry_evidence(mounted):
    app, calls, result, _ = mounted
    selected = json.loads((Path(__file__).resolve().parents[1] / "mcp_servers/frankenmemory/crates/fm-core/tests/fixtures/code_map_selection.json").read_text())
    result.clear()
    result.update(selected)
    value = {"workspace_id":"fixture-workspace", "visual_request":{"mode":"selection", "request":{
        "schema":selected["schema"], "authority":selected["authority"], "selection_id":selected["selection_id"]}}}
    response = post(app, value)
    assert response.status_code == 200 and "error" not in response.json()
    assert response.json()["receipt"] == selected["receipt"]
    assert response.json()["references"][0]["occurrence_id"].startswith("occ:")
    assert calls[0]["visual_request"]["mode"] == "selection" and "query" not in calls[0]
    assert response.json()["cost"]["serialized_bytes"] == len(response.content)

