"""Root integrated lifecycle→real tool→context→Graph→source, no live/model data."""
import json
import os
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

# This existing guard pins all startup stores and the native fixture hash before
# application imports. Run with its explicit s03-s06-projection temp prefix.
from tests.test_code_graph_native import _assert_startup, mounted, post

_assert_startup()
from tests import test_code_index_lifecycle as lifecycle
from src.openclank.code_index_lifecycle import workspace_index_event
from src.openclank.code_context import append_code_context, SENTINEL
from src.openclank.files_service_client import close_all_clients

scope = lifecycle.scope


async def test_authorized_lazy_workspace_to_current_evidence_and_no_resurrection(scope, tmp_path, monkeypatch):
    from core.models import Session
    from src import ai_interaction
    from src.frankenmemory_provider import FrankenmemoryProvider
    from src.memory_provider import MemoryRequestRejectedError
    from src.tool_schemas import function_call_to_tool_block
    from src.tool_execution import execute_tool_block

    repository, workspaces, _, _ = scope
    provider, gate = await lifecycle.native_provider(scope, tmp_path, monkeypatch)
    root = Path(lifecycle.native_request(scope)["root"])
    text = "def target():\n    return 1\n\ndef caller():\n    target()\n"
    for owner in ("alice", "bob"):
        (Path(lifecycle.native_request(scope, owner=owner)["root"]) / "source.py").write_text(text, encoding="utf8")
    session = Session(id="integrated-chat", name="synthetic", owner="alice", endpoint_url="", model="", workspace_id=workspaces["alice"].id)
    old_sessions = ai_interaction.get_session_manager()
    old_memory = tuple(getattr(ai_interaction, key) for key in ("_memory_manager", "_memory_vector", "_memory_provider", "_memory_lifecycle"))
    ai_interaction.set_session_manager(SimpleNamespace(get_session=lambda sid: session if sid == session.id else None))
    ai_interaction.set_memory_manager(None, provider=provider)

    async def event(owner="alice", action="ensure"):
        return await workspace_index_event(repository=repository, provider=provider, auth_manager=lifecycle.Auth(),
            owner=owner, workspace_id=workspaces[owner].id, subscriber_id="session:integrated-" + owner,
            action=action, session=session if owner == "alice" else None)

    async def ready(owner="alice", previous=None):
        async def predicate():
            state = await provider.code_index_lifecycle(**lifecycle.native_request(scope, owner=owner, action="status"))
            return state if state["lifecycle"] == "ready" and state["head_generation"] != previous else False
        return await lifecycle.wait_native(predicate, owner + " integrated ready generation")

    async def tool():
        block = function_call_to_tool_block("search_code", json.dumps({"operation": "search", "query": "target",
            "channels": ["exact", "identifier"], "fields": ["label", "source", "properties"]}))
        description, result = await execute_tool_block(block, owner="alice", session_id=session.id,
            workspace=str(root), authority_workspace_id=workspaces["alice"].id)
        assert description.startswith("registry: search_code"), description
        return json.loads(result["output"])

    async def context(**override):
        args = dict(messages=[{"role": "user", "content": "Explain function target"}], message="Explain function target",
            session=session, provider=provider, repository=repository, auth_manager=lifecycle.Auth(),
            prefs={"code_context_enabled": True}, owner="alice", context_length=32000)
        args.update(override)
        result = await append_code_context(**args)
        return args["messages"], result

    async def resource(app, graph):
        selected = next(hit for hit in graph["hits"] if hit["kind"] == "declaration")
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://fixture") as client:
            reply = await client.post("/api/memory/code/resource", json={"workspace_id": workspaces["alice"].id,
                "operation": "resolve", "excerpt_bytes": 1024,
                "reference": {"kind": "code", "id": selected["id"], "receipt": graph["receipt"]}})
        assert reply.status_code == 200
        value = reply.json()
        assert value["cost"]["serialized_bytes"] == len(reply.content) <= 32768
        assert str(root) not in reply.text
        return value

    query = {"operation": "search", "query": "target", "channels": ["exact", "identifier"], "fields": ["label", "source", "properties"]}
    try:
        # Lazy event owns admission; a second subscriber reuses the same held job.
        await event()
        private = await provider.code_index_lifecycle(**lifecycle.native_request(scope, action="status"))
        await workspace_index_event(repository=repository, provider=provider, auth_manager=lifecycle.Auth(), owner="alice",
            workspace_id=workspaces["alice"].id, subscriber_id="app:integrated-graph", purpose="app_folder")
        shared = await provider.code_index_lifecycle(**lifecycle.native_request(scope, action="status"))
        assert private["task_token"] and private["task_token"] == shared["task_token"]
        await event("bob")
        (gate / "release").touch()
        first = await ready()
        bob = await ready("bob")
        assert first["checkout_id"] != bob["checkout_id"]

        manual = await tool()  # No mounted browser, model or fake handler.
        app = mounted(scope, provider)
        graph = await post(app, workspaces["alice"].id, query)
        assert manual["error"] is None and graph["error"] is None
        assert manual["receipt"] == graph["receipt"]
        assert {h["id"] for h in manual["hits"]} == {h["id"] for h in graph["hits"]}
        current = await resource(app, graph)
        assert current["state"] == "current" and current["current_source_verified"] is True
        assert current["receipt"] == graph["receipt"] and current["destination"]["resource"]["ref"]
        assert "return 1" in current["excerpt"]
        _, augmented = await context()
        assert len(augmented) == 2 and SENTINEL in augmented[0]["content"]
        assert current["source"]["source_hash"] in augmented[0]["content"]
        assert workspaces["alice"].id in augmented[0]["content"]
        retained, duplicate = await context(messages=augmented)
        assert duplicate is retained
        for override in ({"prefs": {}}, {"incognito": True}, {"owner": "bob"}, {"message": "hello"}, {"message": "Explain function absent_symbol_xyz"}):
            original, unchanged = await context(**override)
            assert unchanged is original, override
        denied = await post(app, workspaces["alice"].id, query, owner="bob")
        assert denied["error"]["code"] == "denied" and not denied["hits"]

        # Equal size and mtime do not make old indexed bytes current.
        source = root / "source.py"
        before = source.stat()
        source.write_bytes(source.read_bytes().replace(b"return 1", b"return 2"))
        os.utime(source, ns=(before.st_atime_ns, before.st_mtime_ns))
        stale = await resource(app, graph)
        assert stale["state"] == "stale" and stale["destination"] is None and stale["excerpt"] is None
        original, unchanged = await context()
        assert unchanged is original
        await event()
        updated = await ready(previous=first["head_generation"])
        new_graph = await post(app, workspaces["alice"].id, query)
        assert new_graph["receipt"]["head_generation"] == updated["head_generation"]
        fresh = await resource(app, new_graph)
        assert fresh["state"] == "current" and "return 2" in fresh["excerpt"]
        assert fresh["source"]["source_hash"] != current["source"]["source_hash"]
        assert (await resource(app, graph))["state"] == "stale"

        # Restart the exact same isolated owner/database, not an installed fallback.
        command, environment = provider._command, dict(provider._env)
        await provider.shutdown()
        provider = FrankenmemoryProvider(command=command, env=environment)
        await provider.initialize()
        ai_interaction.set_memory_manager(None, provider=provider)
        app = mounted(scope, provider)
        restarted = await tool()
        assert restarted["receipt"] == new_graph["receipt"]
        assert (await resource(app, new_graph))["state"] == "current"

        await event(action="remove")
        assert (await resource(app, new_graph))["state"] == "erased"
        erased = await tool()
        assert erased["error"]["code"] == "erased" and not erased["hits"]
        original, unchanged = await context()
        assert unchanged is original
        # Native rejects ordinary reopen; it does not return a successful ensure.
        with pytest.raises(MemoryRequestRejectedError, match="erased: explicit admit required"):
            await event()
        state = await provider.code_index_lifecycle(**lifecycle.native_request(scope, action="status"))
        assert state["lifecycle"] == "erased"
        assert (await ready("bob"))["head_generation"] == bob["head_generation"]
        await event(action="admit")  # Explicit fixture-only rebuild is distinct.
        rebuilt = await ready(previous=updated["head_generation"])
        assert rebuilt["admission_epoch"] > updated["admission_epoch"]
        assert (await tool())["error"] is None
        assert (await resource(app, new_graph))["state"] == "stale"
    finally:
        ai_interaction.set_session_manager(old_sessions)
        ai_interaction.set_memory_manager(old_memory[0], vector=old_memory[1], provider=old_memory[2], lifecycle=old_memory[3])
        (gate / "release").touch()
        close_all_clients()
        await provider.shutdown()
