"""Actual S09 C04→Files opaque ref→existing Editor owner, synthetic only."""
import asyncio
import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from tests.test_code_query_transport_s05 import prepared
from tests import test_code_index_lifecycle as lifecycle
from src.openclank.code_query import query_code
from src.openclank.code_resource_bridge import resolve_resource
from src.openclank.files_service_client import close_all_clients

assert hashlib.sha256(Path(os.environ["OPEN_CLANK_TEST_FM_MCP_BIN"]).read_bytes()).hexdigest() == os.environ["OPEN_CLANK_TEST_FM_MCP_SHA256"]
scope = lifecycle.scope


async def selection(scope, provider, *, owner="alice"):
    value = await query_code(repository=scope[0], provider=provider, auth_manager=lifecycle.Auth(), owner=owner,
        workspace_id=scope[1][owner].id, purpose="app_folder", query=dict(operation="search", query="target", fields=["source", "properties"], channels=["identifier"]))
    assert value["error"] is None, value
    hit = next(h for h in value["hits"] if h["kind"] == "declaration")
    return dict(workspace_id=scope[1][owner].id, operation="resolve", reference=dict(kind="code", id=hit["id"], receipt=value["receipt"]))


def args(scope, provider, owner="alice"):
    return dict(repository=scope[0], provider=provider, auth_manager=lifecycle.Auth(), owner=owner)


async def test_current_opaque_destination_opens_exact_existing_editor_source(scope, tmp_path, monkeypatch):
    provider, root = await prepared(scope, tmp_path, monkeypatch)
    try:
        body = await selection(scope, provider)
        value = await resolve_resource(**args(scope, provider), body=body)
        assert value["state"] == "current", value
        assert value["current_source_verified"] and value["validation_kind"] == "current_source"
        assert "def target" in value["excerpt"] and value["source"]["rel_path"] == "source.py"
        assert str(root) not in json.dumps(value)
        resource = value["destination"]["resource"]
        assert set(resource["capabilities"]) == {"open", "stat"}
        from src.openclank.resource_refs import resolve_resource_ref
        ref = resolve_resource_ref(resource["ref"], expected_owner_subject_id=scope[1]["alice"].owner_subject_id, current_policy_generation=scope[0].generation())
        from src.openclank.files_host_provider import HostFilesProvider, _path
        assert _path(ref.origin_id) == str(root / "source.py")
        from src.openclank.filesystem_registry import FilesystemRootRegistry
        from src.openclank.files_facade import FilesFacade, ProviderContext
        host = HostFilesProvider(registry=FilesystemRootRegistry(repository=scope[0]))
        context = ProviderContext(owner_subject_id=scope[1]["alice"].owner_subject_id, owner_username="alice", policy_generation=scope[0].generation(), workspace_id=scope[1]["alice"].id)
        facade = FilesFacade([host])
        opened = await facade.open(context, resource_ref=resource["ref"])
        assert opened["target"]["app"] == "editor"
        payload = await facade.open_payload(context, resource_ref=opened["resource"]["ref"])
        assert "def target" in json.dumps(payload) and str(root) not in json.dumps(payload)
    finally:
        close_all_clients(); await provider.shutdown()


@pytest.mark.parametrize("change", ["same_size_edit", "delete", "rename", "erase", "wrong_owner", "wrong_workspace"])
async def test_native_source_invalidations_never_open_or_heal(scope, tmp_path, monkeypatch, change):
    provider, root = await prepared(scope, tmp_path, monkeypatch)
    try:
        body = await selection(scope, provider)
        owner = "alice"
        if change == "same_size_edit":
            file = root / "source.py"; old = file.stat(); file.write_bytes(file.read_bytes().replace(b"return 1", b"return 2")); os.utime(file, ns=(old.st_atime_ns, old.st_mtime_ns))
        elif change == "delete": (root / "source.py").unlink()
        elif change == "rename": (root / "source.py").rename(root / "moved.py")
        elif change == "erase": await provider.code_index_lifecycle(**lifecycle.native_request(scope, action="remove"))
        elif change == "wrong_owner": owner = "bob"
        else: body["workspace_id"] = scope[1]["bob"].id
        value = await resolve_resource(**args(scope, provider, owner), body=body)
        assert value["state"] == {"same_size_edit": "stale", "delete": "missing", "rename": "missing", "erase": "erased", "wrong_owner": "denied", "wrong_workspace": "invalid"}[change], value
        assert value["destination"] is None and value["excerpt"] is None and not value["current_source_verified"]
        assert str(root) not in json.dumps(value)
    finally:
        close_all_clients(); await provider.shutdown()


async def test_mounted_bridge_closed_input_and_full_serialized_bound(scope, tmp_path, monkeypatch):
    provider, root = await prepared(scope, tmp_path, monkeypatch)
    try:
        from fastapi import FastAPI
        from routes.memory.memory_routes import setup_memory_routes
        app = FastAPI()
        app.state.auth_manager = lifecycle.Auth(); app.state.files_policy_repository = scope[0]
        @app.middleware("http")
        async def identity(request, call_next):
            request.state.current_user = "alice"; request.state.authenticated = True
            return await call_next(request)
        app.include_router(setup_memory_routes(SimpleNamespace(), SimpleNamespace(), memory_provider=provider, memory_lifecycle=SimpleNamespace(skill_forget=None)))
        selected = await selection(scope, provider)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://fixture") as client:
            for body in [selected, {**selected, "path": str(root)}, {"workspace_id": scope[1]["alice"].id, "operation": []}, {**selected, "excerpt_bytes": 8193}]:
                response = await client.post("/api/memory/code/resource", json=body)
                value = response.json()
                assert value["cost"]["serialized_bytes"] == len(response.content) <= 32768
                assert str(root) not in response.text
                if body == selected: assert value["state"] == "current", value
                else: assert value["state"] == "invalid", value
            response = await client.post("/api/memory/code/resource", content=b"x" * 16385)
            assert response.status_code == 413 and response.json()["state"] == "invalid"
    finally:
        close_all_clients(); await provider.shutdown()


async def test_late_policy_change_after_real_query_refuses_destination(scope, tmp_path, monkeypatch):
    provider, _ = await prepared(scope, tmp_path, monkeypatch)
    try:
        body = await selection(scope, provider)
        import src.openclank.code_resource_bridge as bridge
        original = bridge.query_code
        async def changed(**kw):
            result = await original(**kw)
            scope[0].update_workspace(scope[1]["alice"].id, actor_subject_id=scope[1]["alice"].owner_subject_id, archived=True, expected_revision=scope[1]["alice"].revision)
            return result
        monkeypatch.setattr(bridge, "query_code", changed)
        result = await bridge.resolve_resource(**args(scope, provider), body=body)
        assert result["state"] == "denied" and result["destination"] is None
    finally:
        close_all_clients(); await provider.shutdown()


async def test_existing_memory_and_document_producers_keep_real_revision_identity(scope, tmp_path, monkeypatch):
    provider, root = await prepared(scope, tmp_path, monkeypatch)
    try:
        from src.frankenmemory_v2 import mirror_record
        import sqlite3
        record = dict(id="s09-record", text="A persisted claim, not source bytes", category="fact")
        assert mirror_record(record, owner="alice", workspace_id=scope[1]["alice"].id, db_path=provider._fm_db_path)
        from src.frankenmemory_rag import FrankenmemoryRAG
        rag = FrankenmemoryRAG(db_path=provider._fm_db_path)
        assert rag.add_document("# Original\n\nA document paragraph.", dict(owner="alice", workspace_id=scope[1]["alice"].id, source_uri="file:///private/not-code-authority"))
        with sqlite3.connect(provider._fm_db_path) as c:
            evidence, revision = c.execute("SELECT evidence_id,source_revision FROM fm_v2_evidence WHERE owner_id='alice' ORDER BY created_at DESC LIMIT 1").fetchone()
            document, chunk, doc_revision = c.execute("SELECT d.document_id,c.chunk_id,d.source_revision FROM fm_v2_documents d JOIN fm_v2_chunks c ON c.owner_id=d.owner_id AND c.document_id=d.document_id WHERE d.owner_id='alice' LIMIT 1").fetchone()
        for reference in [dict(kind="memory_evidence", evidence_id=evidence, source_revision=revision), dict(kind="document_chunk", document_id=document, chunk_id=chunk, source_revision=doc_revision)]:
            result = await resolve_resource(**args(scope, provider), body=dict(workspace_id=scope[1]["alice"].id, operation="resolve", reference=reference))
            assert result["state"] == "current" and result["validation_kind"] == "persisted_revision", result
            assert not result["current_source_verified"] and result["destination"] is None
            assert "file:///private" not in json.dumps(result)
        assert mirror_record({**record, "text": "New claim revision"}, owner="alice", workspace_id=scope[1]["alice"].id, db_path=provider._fm_db_path)
        stale = await resolve_resource(**args(scope, provider), body=dict(workspace_id=scope[1]["alice"].id, operation="resolve", reference=dict(kind="memory_evidence", evidence_id=evidence, source_revision=revision)))
        assert stale["state"] == "stale" and stale["excerpt"] == record["text"]
    finally:
        close_all_clients(); await provider.shutdown()


async def test_tiny_current_resolve_runtime_budget_after_warmup(scope, tmp_path, monkeypatch):
    import time
    provider, root = await prepared(scope, tmp_path, monkeypatch)
    try:
        selected = await selection(scope, provider)
        first_started = time.perf_counter()
        first = await resolve_resource(**args(scope, provider), body=selected)
        first_ms = (time.perf_counter() - first_started) * 1000
        assert first["state"] == "current", first
        await resolve_resource(**args(scope, provider), body=selected)
        samples, rpcs = [], []
        for _ in range(20):
            started = time.perf_counter()
            result = await resolve_resource(**args(scope, provider), body=selected)
            samples.append((time.perf_counter() - started) * 1000)
            rpcs.append(result["cost"]["native_rpc_calls_completed"])
            assert result["state"] == "current" and result["source"] == first["source"] and result["receipt"] == first["receipt"]
            assert len(json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode()) <= 32768
            assert len(result["excerpt"].encode()) <= 8192
        values = sorted(samples)
        print(json.dumps(dict(s09_tiny_adapter_ms=dict(first=round(first_ms, 3), p50=round(values[9], 3), p95=round(values[18], 3), maximum=round(values[-1], 3), samples=20, warmups=1, source_bytes=(root / "source.py").stat().st_size), native_rpc_range=[min(rpcs), max(rpcs)], cost=first["cost"])))
        assert values[18] <= 500, "Frozen C08 tiny-source adapter target; not an operation timeout"
    finally:
        close_all_clients(); await provider.shutdown()


@pytest.mark.parametrize("kind", ["file", "declaration", "occurrence"])
async def test_file_declaration_occurrence_keep_distinct_current_source_locators(scope, tmp_path, monkeypatch, kind):
    provider, _ = await prepared(scope, tmp_path, monkeypatch, body=b"def target():\n return 1\ntarget()\n")
    try:
        value = await query_code(**args(scope, provider), workspace_id=scope[1]["alice"].id, purpose="app_folder", query=dict(operation="catalog" if kind == "file" else "search", query="target", fields=["source", "properties"], channels=["identifier"]))
        hit = next(h for h in value["hits"] if h["kind"] == kind)
        result = await resolve_resource(**args(scope, provider), body=dict(workspace_id=scope[1]["alice"].id, operation="resolve", reference=dict(kind="code", id=hit["id"], receipt=value["receipt"])))
        assert result["state"] == "current" and result["reference"]["id"] == hit["id"], result
        assert result["source"]["rel_path"] == "source.py" and result["current_source_verified"]
    finally:
        close_all_clients(); await provider.shutdown()


async def test_actual_ambiguous_duplicate_decl_does_not_become_current_source(scope, tmp_path, monkeypatch):
    provider, _ = await prepared(scope, tmp_path, monkeypatch, body=b"def target():\n return 1\ndef target():\n return 2\n")
    try:
        selected = await selection(scope, provider)
        result = await resolve_resource(**args(scope, provider), body=selected)
        assert result["state"] == "ambiguous" and result["destination"] is None, result
    finally:
        close_all_clients(); await provider.shutdown()


async def test_capability_discovery_rechecks_erased_head_without_readmission(scope, tmp_path, monkeypatch):
    provider, _ = await prepared(scope, tmp_path, monkeypatch)
    try:
        body = dict(workspace_id=scope[1]["alice"].id, operation="capabilities")
        first = await resolve_resource(**args(scope, provider), body=body)
        assert next(c for c in first["capabilities"] if c["kind"] == "code")["available"]
        await provider.code_index_lifecycle(**lifecycle.native_request(scope, action="remove"))
        erased = await resolve_resource(**args(scope, provider), body=body)
        cap = next(c for c in erased["capabilities"] if c["kind"] == "code")
        assert cap["available"] is False and "erased" in cap["reason"] and erased["receipt"] is None
        assert (await provider.code_index_lifecycle(**lifecycle.native_request(scope, action="status")))["lifecycle"] == "erased"
    finally:
        close_all_clients(); await provider.shutdown()


async def test_transient_same_bytes_symlink_stat_cannot_redirect_current_destination(scope, tmp_path, monkeypatch):
    provider, root = await prepared(scope, tmp_path, monkeypatch)
    try:
        selected = await selection(scope, provider)
        from src.openclank import code_query as cq
        original_factory = cq._files_client
        target, saved, other = root / "source.py", root / "temporarily-saved.py", root / "other.py"
        other.write_bytes(target.read_bytes())
        stat_paths = []
        class SwapStat:
            def __init__(self, client): self.client = client
            def __getattr__(self, name): return getattr(self.client, name)
            async def request(self, operation, path, payload):
                if operation != "stat": return await self.client.request(operation, path, payload)
                target.rename(saved); target.symlink_to(other)
                try:
                    result = await self.client.request(operation, path, payload)
                    stat_paths.append(result["data"]["path"])
                    return result
                finally:
                    target.unlink(); saved.rename(target)
        monkeypatch.setattr(cq, "_files_client", lambda **kw: SwapStat(original_factory(**kw)))
        value = await resolve_resource(**args(scope, provider), body=selected)
        assert stat_paths == [str(other)], "Actual authorized Files stat observed the transient target"
        assert value["state"] == "stale" and value["destination"] is None, value
    finally:
        close_all_clients(); await provider.shutdown()
