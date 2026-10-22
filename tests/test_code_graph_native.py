"""Actual S06 REST/accessor parity; explicit matching child and synthetic authority.

No app import, service, installed-provider fallback or model. Friendly-tool
dispatch parity remains a separate gate until the S05 registry lands.
"""
import asyncio
import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

if not os.environ.get("OPEN_CLANK_TEST_FM_MCP_BIN"):
    pytest.skip("Explicit matching code-query native binary is required", allow_module_level=True)


def _assert_startup():
    root = Path(os.environ["OPEN_CLANK_DATA_DIR"]).resolve()
    assert root.name.startswith("openclank-s03-s06-projection.")
    for key in ("ODYSSEUS_DATA_DIR", "OPEN_CLANK_AUTHORITY_DB_PATH", "OPEN_CLANK_AUTHORITY_AUTH_PATH", "FM_DB_PATH", "OPENCLANK_CONVERSATION_ARCHIVE_DB"):
        assert Path(os.environ[key]).resolve().is_relative_to(root), key
    assert os.environ["DATABASE_URL"] == "sqlite:///:memory:"
    assert os.environ["ODYSSEUS_INPROCESS_POLLERS"] == "0"
    assert os.environ["PYTHONDONTWRITEBYTECODE"] == "1"
    assert "OPEN_CLANK_AGENT_HOME" not in os.environ and "THESIUS_AGENT_HOME" not in os.environ
    binary = Path(os.environ["OPEN_CLANK_TEST_FM_MCP_BIN"]).resolve()
    assert binary == Path(os.environ["FM_MCP_COMMAND"]).resolve() and binary.is_file()
    assert hashlib.sha256(binary.read_bytes()).hexdigest() == os.environ["OPEN_CLANK_TEST_FM_MCP_SHA256"]


_assert_startup()  # BEFORE importing any application owner.

from fastapi import FastAPI  # noqa: E402
from tests import test_code_index_lifecycle as lifecycle  # noqa: E402
from tests.test_code_query_root_transport import ready_provider  # noqa: E402
from routes.memory.memory_routes import setup_memory_routes  # noqa: E402
from src.openclank.code_graph_projection import prepare_graph_query  # noqa: E402
from src.openclank.code_query import query_code  # noqa: E402

scope = lifecycle.scope


def mounted(scope, provider):
    repository, _, _, _ = scope
    app = FastAPI()
    app.state.auth_manager = lifecycle.Auth()
    app.state.files_policy_repository = repository

    @app.middleware("http")
    async def identity(request, call_next):
        request.state.current_user = request.headers.get("x-fixture-owner", "alice")
        request.state.authenticated = True
        return await call_next(request)

    app.include_router(setup_memory_routes(SimpleNamespace(), SimpleNamespace(), memory_provider=provider,
        memory_lifecycle=SimpleNamespace(skill_forget=None)))
    return app


async def post(app, workspace_id, query, *, owner="alice"):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://fixture") as client:
        result = await client.post("/api/memory/code/graph", json={"workspace_id": workspace_id, "query": query}, headers={"x-fixture-owner": owner})
        assert result.status_code == 200, result.text
        assert result.json()["cost"]["serialized_bytes"] == len(result.content)
        return result.json()


async def test_actual_route_matches_shared_accessor_identity_scope_and_omissions(scope, tmp_path, monkeypatch):
    provider, gate = await ready_provider(scope, tmp_path, monkeypatch)
    repository, workspaces, _, _ = scope
    try:
        app = mounted(scope, provider)
        query = {"operation": "search", "query": "target", "fields": ["label", "properties"], "channels": ["exact", "identifier"], "budgets": {"rows": 50, "bytes": 65536, "tokens": 8192}}
        derived, _ = prepare_graph_query(query)
        direct = await query_code(repository=repository, provider=provider, auth_manager=lifecycle.Auth(), owner="alice", workspace_id=workspaces["alice"].id, purpose="app_folder", query=derived)
        result = await post(app, workspaces["alice"].id, query)
        assert direct["error"] is None and result["error"] is None
        assert result["hits"] and {hit["id"] for hit in result["hits"]} == {hit["id"] for hit in direct["hits"]}
        assert result["receipt"] == direct["receipt"]
        assert result["receipt"]["workspace_id"] == workspaces["alice"].id
        assert all("source" not in hit and "excerpt" not in hit and "raw_source" not in hit for hit in result["hits"])
        assert all("source" not in edge and "witnesses" not in edge for edge in result["edges"])
        assert str(tmp_path) not in __import__("json").dumps(result)
        assert result["domain"] == "code" and "owner" not in result["receipt"]
        denied = await post(app, workspaces["alice"].id, query, owner="bob")
        assert denied["error"]["code"] == "denied" and denied["receipt"] is None and not denied["hits"]
        other = await post(app, workspaces["bob"].id, query, owner="bob")
        assert other["error"] is None and other["receipt"]["checkout_id"] != result["receipt"]["checkout_id"]
    finally:
        (gate / "release").touch()
        await provider.shutdown()


async def test_actual_route_cursor_filters_and_erasure_never_readmit(scope, tmp_path, monkeypatch):
    provider, gate = await ready_provider(scope, tmp_path, monkeypatch)
    _, workspaces, _, _ = scope
    try:
        app = mounted(scope, provider)
        query = {"operation": "search", "query": "target", "channels": ["exact", "identifier"], "fields": ["label"], "budgets": {"rows": 1, "bytes": 65536, "tokens": 8192}}
        first = await post(app, workspaces["alice"].id, query)
        assert first["error"] is None and first["next_cursor"]
        continued = await post(app, workspaces["alice"].id, {**query, "cursor": first["next_cursor"], "expected_receipt": first["receipt"]})
        assert continued["error"] is None, continued
        assert continued["receipt"] == first["receipt"]
        assert {hit["id"] for hit in first["hits"]}.isdisjoint(hit["id"] for hit in continued["hits"])
        changed = {**query, "cursor": first["next_cursor"], "expected_receipt": first["receipt"], "filter": {"path_prefix": "other/"}}
        stale = await post(app, workspaces["alice"].id, changed)
        assert stale["error"]["code"] == "stale_cursor" and not stale["hits"]
        await provider.code_index_lifecycle(**lifecycle.native_request(scope, action="remove"))
        erased = await post(app, workspaces["alice"].id, query)
        assert erased["error"]["code"] == "erased" and erased["receipt"] is None and not erased["hits"]
        status = await provider.code_index_lifecycle(**lifecycle.native_request(scope, action="status"))
        assert status["lifecycle"] == "erased"
    finally:
        (gate / "release").touch()
        await provider.shutdown()


async def test_actual_friendly_discovery_dispatch_matches_graph_without_graph_mount(scope, tmp_path, monkeypatch):
    from core.models import Session
    from src import ai_interaction
    from src.agent_tools import TOOL_HANDLERS
    from src.tool_blocks import TOOL_TAGS
    from src.tool_policy import known_tool_names
    from src.tool_schemas import FUNCTION_TOOL_SCHEMAS, function_call_to_tool_block
    from src.tool_execution import execute_tool_block

    provider, gate = await ready_provider(scope, tmp_path, monkeypatch)
    repository, workspaces, _, _ = scope
    session = Session(id="s06-owned-chat", name="synthetic", endpoint_url="", model="", owner="alice", workspace_id=workspaces["alice"].id)
    previous_sessions = ai_interaction.get_session_manager()
    previous_memory = tuple(getattr(ai_interaction, key) for key in ("_memory_manager", "_memory_vector", "_memory_provider", "_memory_lifecycle"))
    ai_interaction.set_session_manager(SimpleNamespace(get_session=lambda sid: session if sid == session.id else None))
    ai_interaction.set_memory_manager(None, provider=provider)
    try:
        assert "search_code" in TOOL_TAGS and "search_code" in known_tool_names()
        assert callable(TOOL_HANDLERS["search_code"])
        schemas = [row["function"] for row in FUNCTION_TOOL_SCHEMAS if row.get("function", {}).get("name") == "search_code"]
        assert len(schemas) == 1
        assert not {"owner", "root", "workspace_id", "action"} & schemas[0]["parameters"]["properties"].keys()
        assert "pipeline" not in schemas[0]["parameters"]["properties"]["operation"]["enum"]
        original = {"operation": "search", "query": "target", "channels": ["exact", "identifier"], "fields": ["label", "source", "properties"]}
        derived, _ = prepare_graph_query(original)
        block = function_call_to_tool_block("search_code", json.dumps(derived))
        assert block is not None and block.tool_type == "search_code"
        # Actual authenticated dispatcher -> registry handler -> shared accessor
        # -> same real child. No Graph view, fake query handler or model exists.
        description, tool = await execute_tool_block(block, owner="alice", session_id=session.id,
            workspace=repository.get_location(workspaces["alice"].location_id).canonical_path,
            authority_workspace_id=workspaces["alice"].id)
        assert description.startswith("registry: search_code"), (description, tool)
        assert tool.get("exit_code") == 0, json.loads(tool["output"]).get("error", tool)
        friendly = json.loads(tool["output"])
        graph = await post(mounted(scope, provider), workspaces["alice"].id, original)
        assert friendly["error"] is None and graph["error"] is None
        assert friendly["receipt"] == graph["receipt"]
        assert friendly["hits"] and {hit["id"] for hit in friendly["hits"]} == {hit["id"] for hit in graph["hits"]}
        assert {json.dumps(hit.get("source"), sort_keys=True) for hit in friendly["hits"]} == {json.dumps(hit.get("source"), sort_keys=True) for hit in graph["hits"]}
        session.owner = "bob"
        _, rejected = await execute_tool_block(block, owner="alice", session_id=session.id,
            workspace=repository.get_location(workspaces["alice"].location_id).canonical_path,
            authority_workspace_id=workspaces["alice"].id)
        assert rejected["exit_code"] != 0
        assert json.loads(rejected["output"])["error"]["code"] == "denied"
    finally:
        ai_interaction.set_session_manager(previous_sessions)
        ai_interaction.set_memory_manager(previous_memory[0], vector=previous_memory[1], provider=previous_memory[2], lifecycle=previous_memory[3])
        (gate / "release").touch()
        await provider.shutdown()


async def test_actual_untyped_legacy_file_forget_refuses_without_erasure(scope, tmp_path, monkeypatch):
    import sqlite3
    from src.memory_provider import MemoryRequestRejectedError

    provider, gate = await ready_provider(scope, tmp_path, monkeypatch)
    _, workspaces, _, _ = scope
    selected = {"owner": "alice", "workspace_id": workspaces["alice"].id}
    try:
        graph = await provider._call_tool("graph_walk", {"op": "overview", "limit": 50, **selected})
        file = next(node for node in graph["nodes"] if node["kind"] == "file")
        assert "domain" not in file and "provenance" not in file, "exercise actual untyped legacy row"
        before = await provider.code_index_lifecycle(**lifecycle.native_request(scope, action="status"))
        with sqlite3.connect("file:" + provider._local_db_path() + "?mode=ro", uri=True) as database:
            files = database.execute("SELECT * FROM code_files ORDER BY codebase,rel_path").fetchall()
        with pytest.raises(MemoryRequestRejectedError) as caught:
            await provider._call_tool("memory_forget", {"action": "preview", "selector_kind": "record_id", "selector": file["id"], **selected})
        assert caught.value.code == "code_node_forget_requires_workspace_erase"
        after = await provider.code_index_lifecycle(**lifecycle.native_request(scope, action="status"))
        assert after["head_generation"] == before["head_generation"] and after["lifecycle"] == "ready"
        with sqlite3.connect("file:" + provider._local_db_path() + "?mode=ro", uri=True) as database:
            assert database.execute("SELECT * FROM code_files ORDER BY codebase,rel_path").fetchall() == files
        assert (await provider._call_tool("graph_walk", {"op": "overview", "limit": 50, **selected}))["nodes"] == graph["nodes"]
    finally:
        (gate / "release").touch()
        await provider.shutdown()
