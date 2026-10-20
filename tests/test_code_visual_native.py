"""Opt-in real provider/native/app map checks, isolated synthetic authority only."""
import asyncio
import hashlib
import json
import os
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI

binary = os.environ.get("OPEN_CLANK_TEST_FM_MCP_BIN")
expected = os.environ.get("OPENCLANK_S02_NATIVE_SHA256")
if not binary or not expected:
    pytest.skip("Explicit matching s03-fixtures binary+SHA required; no installed fallback", allow_module_level=True)
assert hashlib.sha256(Path(binary).resolve(strict=True).read_bytes()).hexdigest() == expected
from tests.test_code_query_root_transport import _assert_isolated_startup
_assert_isolated_startup()
from tests import test_code_index_lifecycle as lifecycle
from src.openclank.code_visual import SCHEMA, query_visual
from src.memory_provider import MemoryRequestRejectedError

scope = lifecycle.scope


def authority(scope, owner="alice", subscriber="view:visual-native"):
    value = lifecycle.native_request(scope, owner=owner, subscriber=subscriber)
    value.pop("action")
    return {**value, "origin":"app"}


def overview(**changes):
    return {"mode":"map", "request":{"schema":SCHEMA, "operation":"overview", **changes}}


async def invoke(scope, provider, request, owner="alice"):
    return await query_visual(repository=scope[0], provider=provider, auth_manager=lifecycle.Auth(),
        owner=owner, workspace_id=scope[1][owner].id, visual_request=request)


async def setup(scope, tmp_path, monkeypatch, *, partition=False, held=False):
    root = Path(authority(scope)["root"])
    for name, body in {"app/caller.py":"from lib.target import target\ndef caller():\n    target()\n    target()\n",
            "lib/target.py":"def target():\n    return 1\n", "lib/__init__.py":"",
            "assets/image.bin":"synthetic asset", ".gitignore":"ignored.py\n", "ignored.py":"pass\n"}.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf8")
    if partition:
        for number in range(129):
            (root / f"noise_{number:03}.py").write_text(f"def unrelated_{number}(): return {number}\n")
    isolated_home = tmp_path / "native-home"
    isolated_home.mkdir()
    monkeypatch.setenv("HOME", str(isolated_home))
    provider, gate = await lifecycle.native_provider(scope, tmp_path, monkeypatch)
    try:
        if not held:
            (gate / "release").touch()
        opened = await provider.code_index_lifecycle(**lifecycle.native_request(scope))
        if held:
            await lifecycle.wait_native(lambda: (gate / (opened["task_token"] + ".held")).exists(), "visual fixture index hold")
        else:
            await ready(scope, provider)
        return provider, gate, opened
    except BaseException:
        (gate / "release").touch()
        await provider.shutdown()
        raise


async def ready(scope, provider):
    async def status():
        value = await provider.code_index_lifecycle(**lifecycle.native_request(scope, action="status"))
        assert value["lifecycle"] not in {"failed", "erased"}, value
        return value if value["lifecycle"] == "ready" else None
    return await lifecycle.wait_native(status, "visual fixture published head")


def counts(provider, tmp_path):
    path = Path(provider._local_db_path()).resolve()
    assert path.is_relative_to(tmp_path.resolve())
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as db:
        return tuple(db.execute("SELECT count(*) FROM " + table).fetchone()[0]
            for table in ("code_registrations", "code_index_runs", "code_index_heads"))


@pytest.mark.parametrize("partition", [False, True])
async def test_actual_app_provider_native_map_selection_and_http_size(scope, tmp_path, monkeypatch, partition):
    provider, gate, _ = await setup(scope, tmp_path, monkeypatch, partition=partition)
    try:
        result = await invoke(scope, provider, overview())
        assert "error" not in result, result
        assert result["authority"]["variant"] == "published" and result["root"]
        assert result["cost"]["source_bytes_read"] == 0
        assert result["cost"]["native_rpc_calls_completed"] >= 2
        edge = next(edge for edge in result["edges"] if edge["relation"] == "call" and edge["state"] == "exact")
        assert edge["count"] == 2
        request = {"mode":"selection", "request":{"schema":SCHEMA, "authority":result["authority"],
            "selection_id":edge["selection_id"], "max_refs":1}}
        first = await invoke(scope, provider, request)
        assert "error" not in first and first["remaining"] == 1 and not first["complete"], first
        second = await invoke(scope, provider, {"mode":"selection", "request":{**request["request"], "cursor":first["next_cursor"]}})
        assert "error" not in second and second["complete"] and second["remaining"] == 0, second
        assert first["references"][0]["occurrence_id"] != second["references"][0]["occurrence_id"]
        assert first["receipt"] == second["receipt"] == result["authority"]["receipt"]
        wrong_owner = await invoke(scope, provider, request, owner="bob")
        assert "error" in wrong_owner and "references" not in wrong_owner
        from routes.memory.memory_routes import setup_memory_routes
        app = FastAPI()
        app.state.auth_manager = lifecycle.Auth()
        app.state.files_policy_repository = scope[0]
        @app.middleware("http")
        async def authenticate_synthetic(request, call_next):
            request.state.current_user = "alice"
            request.state.authenticated = True
            return await call_next(request)
        app.include_router(setup_memory_routes(SimpleNamespace(), SimpleNamespace(), memory_provider=provider,
            memory_lifecycle=SimpleNamespace(skill_forget=None)))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://fixture") as client:
            response = await client.post("/api/memory/code/map", json={"workspace_id":scope[1]["alice"].id,"visual_request":overview()})
        assert response.status_code == 200 and "error" not in response.json(), response.text
        assert response.json()["cost"]["serialized_bytes"] == len(response.content)
        assert response.json()["cost"]["summary_cache_hit"]
        assert str(tmp_path) not in response.text and len(response.content) <= 262144
    finally:
        (gate / "release").touch()
        await provider.shutdown()


async def test_actual_registered_no_head_preview_then_publish_rejects_old_authority(scope, tmp_path, monkeypatch):
    provider, gate, opened = await setup(scope, tmp_path, monkeypatch, held=True)
    try:
        await provider.code_index_lifecycle(**lifecycle.native_request(scope, action="cancel"))
        (gate / "release").touch()
        await lifecycle.wait_native(lambda: (gate / (opened["task_token"] + ".finished")).exists(), "cancelled original index exit")
        before = counts(provider, tmp_path)
        preview = await invoke(scope, provider, overview())
        assert "error" not in preview, preview
        assert preview["authority"]["variant"] == "structure_preview"
        assert preview["coverage"]["indexed_files"] is None and preview["selection"] is None
        assert all(edge["relation"] == "containment" for edge in preview["edges"])
        assert counts(provider, tmp_path) == before and before[2] == 0
        await provider.code_index_lifecycle(**lifecycle.native_request(scope))
        await ready(scope, provider)
        published = await invoke(scope, provider, overview())
        assert "error" not in published and published["authority"]["variant"] == "published", published
        assert {node["id"] for node in preview["nodes"]}.isdisjoint(node["id"] for node in published["nodes"])
        stale = await invoke(scope, provider, overview(expected_authority=preview["authority"]))
        assert stale["error"]["code"] == "stale_cursor" and "nodes" not in stale
    finally:
        (gate / "release").touch()
        await provider.shutdown()


async def test_actual_visual_token_scope_cancel_and_erasure(scope, tmp_path, monkeypatch):
    provider, gate, _ = await setup(scope, tmp_path, monkeypatch)
    binding = authority(scope)
    async def job(action, **changes):
        return await provider._call_tool("code_query", {"action":action,"binding":binding,**changes})
    async def completed(token):
        async def poll():
            result = await job("poll", token=token)
            return result if result["state"] != "running" else None
        return await lifecycle.wait_native(poll, "visual native job")
    try:
        with pytest.raises(MemoryRequestRejectedError):
            await provider._call_tool("code_query", {"action":"start","binding":{**binding,"origin":"agent"},"visual_request":overview()})
        (gate / "release").unlink()
        token = (await job("start", visual_request=overview()))["token"]
        await lifecycle.wait_native(lambda: (gate / (token + ".held")).exists(), "visual query hold")
        assert await asyncio.wait_for(provider.recall("empty synthetic memory", owner="alice"),3) == []
        with pytest.raises(MemoryRequestRejectedError):
            await provider._call_tool("code_query", {"action":"poll","binding":authority(scope, subscriber="different-view"),"token":token})
        assert (await job("cancel", token=token))["state"] == "cancelled"
        (gate / "release").touch()
        cancelled = await completed(token)
        assert cancelled["error"]["code"] == "cancelled" and "result" not in cancelled
        token = (await job("start", visual_request=overview()))["token"]
        assert (await completed(token))["state"] == "completed"
        await provider.code_index_lifecycle(**lifecycle.native_request(scope, action="remove"))
        after = await completed(token)
        assert after["state"] == "failed" and "result" not in after
        state = await provider.code_index_lifecycle(**lifecycle.native_request(scope, action="status"))
        assert state["lifecycle"] == "erased", "visual read must not readmit"
    finally:
        (gate / "release").touch()
        await provider.shutdown()
