"""E0.1a — FrankenmemoryProvider must survive cross-task usage.

The server initializes the provider in its startup task and calls recall from
request-handler tasks. The MCP stdio transport pins anyio cancel scopes to the
task that entered them, so a session owned by no single task explodes
(RuntimeError: cancel scope exited in a different task / ClosedResourceError
with an empty str()). These tests drive the provider exactly like the server
does. Real fm-mcp binary, no fakes (management order).
"""

import asyncio
import logging
import os
from types import SimpleNamespace

import pytest

from src.frankenmemory_provider import FrankenmemoryProvider
from src.memory_provider import MemoryRecord, MemoryRequestRejectedError

FM_BIN = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "mcp_servers/frankenmemory/target/release/fm-mcp",
)

needs_fm = pytest.mark.skipif(not os.path.exists(FM_BIN), reason="fm-mcp release binary not built")


@needs_fm
async def test_cross_task_lifecycle_works(tmp_path):
    """initialize in one task, call from another, shutdown from a third —
    the exact shape the server produces. Must not raise."""
    provider = FrankenmemoryProvider(command=FM_BIN, env={"FM_DB_PATH": str(tmp_path / "fm.db")})
    await asyncio.create_task(provider.initialize())

    result = await provider._call_tool(
        "recall", {
            "query": "anything",
            "top_k": 3,
            "tier": "curated",
            "workspace_id": provider._workspace_id,
            "owner": "alice",
        }
    )
    assert isinstance(result, dict)

    await asyncio.create_task(provider.shutdown())


@needs_fm
async def test_recall_cross_task_returns_list_without_warnings(tmp_path, caplog):
    provider = FrankenmemoryProvider(command=FM_BIN, env={"FM_DB_PATH": str(tmp_path / "fm.db")})
    await asyncio.create_task(provider.initialize())

    with caplog.at_level(logging.WARNING, logger="src.frankenmemory_provider"):
        hits = await asyncio.create_task(provider.recall("nothing stored yet", owner="alice"))

    assert hits == []
    assert "recall failed" not in caplog.text

    await asyncio.create_task(provider.shutdown())


@needs_fm
async def test_concurrent_recalls_from_separate_tasks(tmp_path):
    provider = FrankenmemoryProvider(command=FM_BIN, env={"FM_DB_PATH": str(tmp_path / "fm.db")})
    await asyncio.create_task(provider.initialize())

    results = await asyncio.gather(
        *(asyncio.create_task(provider.recall(f"query {i}", owner="alice")) for i in range(4))
    )
    assert all(r == [] for r in results)

    await asyncio.create_task(provider.shutdown())


async def test_failure_logs_are_never_empty(tmp_path, caplog):
    """When the transport is genuinely broken, the reason must be visible —
    the empty-`str()` class of exceptions produced the infamous
    'frankenmemory recall failed: ' log with nothing after the colon."""
    provider = FrankenmemoryProvider(command="/nonexistent/fm-mcp", env={"FM_DB_PATH": str(tmp_path / "fm.db")})

    with pytest.raises(Exception) as raised:
        await provider.recall("boom", owner="alice")
    assert str(raised.value).strip()


async def test_stdio_tool_rejection_is_not_labeled_as_transport_ambiguity():
    provider = FrankenmemoryProvider(command="/unused")

    class OwnerTask:
        @staticmethod
        def done():
            return False

    class ImmediateQueue:
        async def put(self, item):
            _name, _arguments, future = item
            future.set_result(SimpleNamespace(
                isError=True,
                content=[SimpleNamespace(text="operation tuple mismatch")],
            ))

    provider._owner_task = OwnerTask()
    provider._requests = ImmediateQueue()

    with pytest.raises(MemoryRequestRejectedError, match="tuple mismatch"):
        await provider._call_tool("memory_forget", {"action": "commit"})


async def test_capture_passes_explicit_review_only_mode(monkeypatch):
    provider = FrankenmemoryProvider(command="/nonexistent/fm-mcp")
    calls = []

    async def fake_call_tool(name, args):
        calls.append((name, args))
        return {"record_ids": ["candidate_1"]}

    monkeypatch.setattr(provider, "_call_tool", fake_call_tool)

    result = await provider.capture(
        "My name is Alice",
        "Nice to meet you.",
        owner="alice",
        session_id="ses-review",
        capture_mode="review_only",
    )

    assert result == {"record_ids": ["candidate_1"]}
    assert calls == [
        (
            "capture",
            {
                "user_text": "My name is Alice",
                "assistant_text": "Nice to meet you.",
                "capture_mode": "review_only",
                "workspace_id": provider._workspace_id,
                "workspace_path": provider._workspace_id,
                "owner": "alice",
                "source": "odysseus",
                "session_id": "ses-review",
                "session_key": "ses-review",
            },
        )
    ]


async def test_preferred_v2_reads_do_not_hide_legacy_only_rows(monkeypatch, tmp_path):
    """The shadow projection must enrich migrated rows, not truncate the bank."""
    provider = FrankenmemoryProvider(command="/unused", env={"FM_DB_PATH": str(tmp_path / "fm.db")})
    monkeypatch.setenv("FM_V2_READ_MODE", "preferred")
    v2 = SimpleNamespace(id="m_v2", text="new", category="fact")
    legacy = [
        {"id": "m_v2", "content": "old", "kind": "fact"},
        {"id": "m_legacy", "content": "still visible", "kind": "fact"},
    ]

    async def fake_v2(**_kwargs):
        return [v2]

    async def fake_call(name, _args):
        assert name == "list_memories"
        return {"records": legacy, "next_cursor": "next"}

    monkeypatch.setattr(provider, "list_v2_records", fake_v2)
    monkeypatch.setattr(provider, "_call_tool", fake_call)
    records, cursor = await provider.list_page(owner="alice", limit=10)
    assert [record.id for record in records] == ["m_v2", "m_legacy"]
    assert records[0].text == "new"
    assert cursor == "next"


async def test_preferred_v2_recall_merges_legacy_only_rows(monkeypatch, tmp_path):
    provider = FrankenmemoryProvider(command="/unused", env={"FM_DB_PATH": str(tmp_path / "fm.db")})
    monkeypatch.setenv("FM_V2_READ_MODE", "preferred")

    def fake_v2(*_args, **_kwargs):
        return [{"id": "m_v2", "text": "new", "kind": "fact", "recall_score": 1.0}]

    async def fake_call(name, _args):
        if name == "get_memory":
            return {"record": {"id": "m_v2", "content": "old", "kind": "fact"}}
        assert name == "recall"
        return {"memories": [
            {"id": "m_v2", "content": "old", "kind": "fact", "score": 0.4},
            {"id": "m_legacy", "content": "still visible", "kind": "fact", "score": 0.3},
        ]}

    monkeypatch.setattr("src.frankenmemory_v2.search_current_records", fake_v2)
    monkeypatch.setattr(provider, "_call_tool", fake_call)
    hits = await provider.recall("visible", owner="alice", top_k=10)
    assert [hit.memory.id for hit in hits] == ["m_v2", "m_legacy"]
    assert hits[0].memory.text == "new"


async def test_preferred_v2_recall_ranks_the_merged_result(monkeypatch, tmp_path):
    provider = FrankenmemoryProvider(command="/unused", env={"FM_DB_PATH": str(tmp_path / "fm.db")})
    monkeypatch.setenv("FM_V2_READ_MODE", "preferred")

    monkeypatch.setattr(
        "src.frankenmemory_v2.search_current_records",
        lambda *_args, **_kwargs: [
            {"id": "m_v2", "text": "weak lexical hit", "kind": "fact", "recall_score": 0.1}
        ],
    )

    async def fake_call(name, _args):
        if name == "get_memory":
            return {"record": {"id": "m_v2", "content": "weak lexical hit", "kind": "fact"}}
        assert name == "recall"
        return {"memories": [
            {"id": "m_legacy", "content": "strong semantic hit", "kind": "fact", "score": 0.99}
        ]}

    monkeypatch.setattr(provider, "_call_tool", fake_call)
    hits = await provider.recall("query", owner="alice", top_k=1)
    assert [(hit.memory.id, hit.score) for hit in hits] == [("m_legacy", 0.99)]


async def test_preferred_v2_recall_tolerates_malformed_legacy_scores(monkeypatch, tmp_path):
    """A non-numeric legacy score over the wire degrades to unscored, not a crash."""
    provider = FrankenmemoryProvider(command="/unused", env={"FM_DB_PATH": str(tmp_path / "fm.db")})
    monkeypatch.setenv("FM_V2_READ_MODE", "preferred")

    def fake_v2(*_args, **_kwargs):
        return [{"id": "m_v2", "text": "new", "kind": "fact", "recall_score": 1.0}]

    async def fake_call(name, _args):
        if name == "get_memory":
            return {"record": {"id": "m_v2", "content": "old", "kind": "fact"}}
        assert name == "recall"
        return {"memories": [
            {"id": "m_v2", "content": "old", "kind": "fact", "score": "not-a-number"},
            {"id": "m_legacy", "content": "still visible", "kind": "fact", "score": {"bad": "wire"}},
        ]}

    monkeypatch.setattr("src.frankenmemory_v2.search_current_records", fake_v2)
    monkeypatch.setattr(provider, "_call_tool", fake_call)
    hits = await provider.recall("visible", owner="alice", top_k=10)
    assert [(hit.memory.id, hit.score) for hit in hits] == [("m_v2", 1.0), ("m_legacy", None)]


async def test_remember_sets_v2_kind_from_requested_category(monkeypatch, tmp_path):
    provider = FrankenmemoryProvider(command="/unused", env={"FM_DB_PATH": str(tmp_path / "fm.db")})

    async def fake_call(_name, _args):
        return {"record_ids": ["m_project"]}

    mirrored = []
    monkeypatch.setattr(provider, "_call_tool", fake_call)
    monkeypatch.setattr("src.frankenmemory_v2.mirror_record", lambda record, **_kwargs: mirrored.append(record) or True)
    record = await provider.remember("ships Friday", owner="alice", category="project")
    assert record.kind == "project"
    assert mirrored[0].kind == "project"


async def test_review_only_remember_does_not_activate_a_v2_block(monkeypatch, tmp_path):
    provider = FrankenmemoryProvider(command="/unused", env={"FM_DB_PATH": str(tmp_path / "fm.db")})

    async def fake_call(_name, _args):
        return {"record_ids": ["candidate_project"]}

    mirrored = []
    monkeypatch.setattr(provider, "_call_tool", fake_call)
    monkeypatch.setattr("src.frankenmemory_v2.mirror_record", lambda record, **_kwargs: mirrored.append(record) or True)
    record = await provider.remember(
        "possible project fact",
        owner="alice",
        category="project",
        capture_mode="review_only",
    )
    assert record.metadata["pending_review"] is True
    assert mirrored == []


async def test_accepted_candidate_is_mirrored_to_v2(monkeypatch, tmp_path):
    provider = FrankenmemoryProvider(command="/unused", env={"FM_DB_PATH": str(tmp_path / "fm.db")})

    async def fake_call(_name, _args):
        return {"reviewed": True, "accepted": True, "curated_id": "m_curated"}

    async def fake_current(memory_id, *, owner=None, workspace_id=None):
        assert memory_id == "m_curated"
        assert workspace_id == provider._workspace_id
        return MemoryRecord(id=memory_id, text="approved", owner=owner, workspace_id=workspace_id)

    mirrored = []
    monkeypatch.setattr(provider, "_call_tool", fake_call)
    monkeypatch.setattr(provider, "_get_legacy_current", fake_current)
    monkeypatch.setattr("src.frankenmemory_v2.mirror_record", lambda record, **kwargs: mirrored.append((record, kwargs)) or True)
    result = await provider.review_candidate(
        "candidate-1", accept=True, reason="approved", owner="alice"
    )
    assert result["curated_id"] == "m_curated"
    assert mirrored[0][0].id == "m_curated"
    assert mirrored[0][1]["action"] == "review_candidate"


async def test_remember_never_reports_a_raw_capture_as_saved(monkeypatch):
    provider = FrankenmemoryProvider(command="/nonexistent/fm-mcp")

    async def fake_call_tool(_name, _args):
        return {"record_ids": ["raw_1", "candidate_1"]}

    monkeypatch.setattr(provider, "_call_tool", fake_call_tool)

    with pytest.raises(RuntimeError, match="did not admit"):
        await provider.remember("short fact", owner="alice")


async def test_remember_reports_agent_provenance(monkeypatch):
    provider = FrankenmemoryProvider(command="/nonexistent/fm-mcp")

    async def fake_call_tool(_name, _args):
        return {"record_ids": ["m_1"]}

    monkeypatch.setattr(provider, "_call_tool", fake_call_tool)

    record = await provider.remember(
        "Agent-discovered fact",
        owner="alice",
        source="ai_agent",
    )
    assert record.source_type == "ai"


async def test_lifecycle_contracts_keep_authenticated_scope(monkeypatch):
    provider = FrankenmemoryProvider(command="/nonexistent/fm-mcp")
    calls = []

    async def fake_call_tool(name, args):
        calls.append((name, args))
        if name == "memory_explain":
            return {"explanation": {"id": args["id"], "source_uri": "message://alice/1"}}
        return {"ok": True}

    monkeypatch.setattr(provider, "_call_tool", fake_call_tool)

    await provider.retention(
        "set",
        owner="alice",
        raw_days=14,
        candidate_days=45,
        recovery_seconds=60,
    )
    await provider.forget(
        "commit",
        owner="alice",
        selector_kind="source_message_id",
        selector="message-1",
        preview_token="preview-token",
        operation_id="0123456789abcdef0123456789abcdef",
    )
    await provider.export_scope(owner="alice")
    explanation = await provider.explain("memory-1", owner="alice")

    assert explanation == {"id": "memory-1", "source_uri": "message://alice/1"}
    assert calls == [
        (
            "memory_retention",
            {
                "action": "set",
                "owner": "alice",
                "workspace_id": provider._workspace_id,
                "raw_days": 14,
                "candidate_days": 45,
                "recovery_seconds": 60,
            },
        ),
        (
            "memory_forget",
            {
                "action": "commit",
                "owner": "alice",
                "workspace_id": provider._workspace_id,
                "selector_kind": "source_message_id",
                "selector": "message-1",
                "preview_token": "preview-token",
                "operation_id": "0123456789abcdef0123456789abcdef",
            },
        ),
        (
            "memory_export",
            {"owner": "alice", "workspace_id": provider._workspace_id},
        ),
        (
            "memory_explain",
            {
                "id": "memory-1",
                "owner": "alice",
                "workspace_id": provider._workspace_id,
            },
        ),
    ]


@needs_fm
async def test_recall_round_trip_returns_seeded_hits(tmp_path):
    """E0.1b: fm-mcp recall must return structured memories the provider can
    parse. Today it answers prose ('Strategy: ... Results: N memories'), the
    JSON parse falls back to {'raw': ...} and recall yields zero hits forever."""
    provider = FrankenmemoryProvider(command=FM_BIN, env={"FM_DB_PATH": str(tmp_path / "fm.db")})
    await asyncio.create_task(provider.initialize())

    await provider.remember(
        "The lemon cake recipe is written in the blue notebook on the third shelf.",
        owner="alice",
        session_id="ses_transport_test",
        source="user",
    )
    hits = await provider.recall("blue notebook lemon cake recipe", owner="alice", top_k=5)

    assert hits, "recall returned no hits for content that was just captured"
    assert any("blue notebook" in h.memory.text for h in hits)
    assert hits[0].memory.id, "memories must carry their record id"

    await asyncio.create_task(provider.shutdown())


@needs_fm
async def test_provider_browse_update_pin_delete_round_trip(tmp_path):
    provider = FrankenmemoryProvider(command=FM_BIN, env={"FM_DB_PATH": str(tmp_path / "fm.db")})
    await asyncio.create_task(provider.initialize())

    record = await provider.remember(
        "The blue notebook is on the third shelf.",
        owner="alice",
        category="fact",
    )
    assert record.id.startswith("m_")
    listed = await provider.list_memories(owner="alice")
    assert any(item.id == record.id for item in listed)
    fetched = await provider.get(record.id, owner="alice")
    assert fetched is not None
    assert fetched.text == record.text

    updated = await provider.update(
        record.id,
        text="The blue notebook is on shelf three.",
        category="preference",
        owner="alice",
    )
    assert updated is not None
    assert updated.text == "The blue notebook is on shelf three."
    assert updated.category == "preference"
    assert await provider.pin(record.id, True, owner="alice")
    pinned_hits = await provider.recall("unrelated query", owner="alice", top_k=1)
    assert any(hit.memory.id == record.id and hit.memory.pinned for hit in pinned_hits)
    assert await provider.record_access([record.id], owner="alice") == 1
    accessed = await provider.get(record.id, owner="alice")
    assert accessed is not None and accessed.uses == 1
    assert await provider.delete(record.id, owner="alice")
    assert not any(item.id == record.id for item in await provider.list_memories(owner="alice"))

    await asyncio.create_task(provider.shutdown())


@needs_fm
async def test_graph_tools_round_trip(tmp_path):
    """E1.3: graph_upsert + graph_walk (cues -> tags -> expand -> trace)."""
    provider = FrankenmemoryProvider(command=FM_BIN, env={"FM_DB_PATH": str(tmp_path / "fm.db")})
    await asyncio.create_task(provider.initialize())

    up = await provider._call_tool(
        "graph_upsert",
        {
            "owner": "alice",
            "workspace_id": provider._workspace_id,
            "nodes": [{"kind": "person", "name": "e", "label": "owner", "trust": 4}],
            "edges": [
                {
                    "src": {"kind": "person", "name": "e"},
                    "tag": "works_on",
                    "dst": {"kind": "project", "name": "open-clank"},
                    "fact": "e works on the open-clank workspace",
                },
                {
                    "src": {"kind": "project", "name": "open-clank"},
                    "tag": "uses",
                    "dst": {"kind": "tool", "name": "frankenmemory"},
                    "fact": "open-clank uses frankenmemory for memory",
                },
            ],
            "cues": [{"cue": "memory engine", "node": {"kind": "tool", "name": "frankenmemory"}}],
        },
    )
    assert up["edges_upserted"] == 2

    cues = await provider._call_tool(
        "graph_walk",
        {"op": "cues", "query": "memory engine details", "owner": "alice", "workspace_id": provider._workspace_id},
    )
    assert cues["hits"], "cue lookup must find the entry node"
    fm_node = cues["hits"][0]["node"]
    assert fm_node["name"] == "frankenmemory"

    tags = await provider._call_tool(
        "graph_walk",
        {"op": "tags", "node_id": fm_node["id"], "owner": "alice", "workspace_id": provider._workspace_id},
    )
    assert any(t["tag"] == "uses" and t["direction"] == "in" for t in tags["tags"])

    hits = await provider._call_tool(
        "graph_walk",
        {"op": "expand", "node_id": fm_node["id"], "direction": "in", "owner": "alice", "workspace_id": provider._workspace_id},
    )
    assert hits["hits"][0]["other"]["name"] == "open-clank"
    assert "memory" in hits["hits"][0]["edge"]["fact"]

    trace = await provider._call_tool(
        "graph_walk",
        {"op": "trace", "node_id": fm_node["id"], "dst_id": cues["hits"][0]["node"]["id"], "owner": "alice", "workspace_id": provider._workspace_id},
    )
    assert trace["op"] == "trace"

    await asyncio.create_task(provider.shutdown())


@needs_fm
async def test_graph_groom_ops_round_trip(tmp_path):
    """G2: edge_decay + tag_normalize reachable through the groom tool."""
    provider = FrankenmemoryProvider(command=FM_BIN, env={"FM_DB_PATH": str(tmp_path / "fm.db")})
    await asyncio.create_task(provider.initialize())

    await provider._call_tool(
        "graph_upsert",
        {
            "owner": "alice",
            "workspace_id": provider._workspace_id,
            "edges": [
                {
                    "src": {"kind": "person", "name": "e"},
                    "tag": "usess",
                    "dst": {"kind": "tool", "name": "frankenmemory"},
                    "fact": "e usess frankenmemory",
                }
            ]
        },
    )
    normalized = await provider._call_tool(
        "groom", {"op": "tag_normalize", "owner": "alice", "workspace_id": provider._workspace_id}
    )
    assert normalized["records_merged"] == 1

    decayed = await provider._call_tool(
        "groom", {"op": "edge_decay", "dry_run": True, "owner": "alice", "workspace_id": provider._workspace_id}
    )
    assert "records_archived" in decayed

    hits = await provider._call_tool(
        "graph_walk", {"op": "cues", "query": "frankenmemory", "owner": "alice", "workspace_id": provider._workspace_id}
    )
    assert hits["op"] == "cues"

    await asyncio.create_task(provider.shutdown())


@needs_fm
async def test_code_index_round_trip(tmp_path):
    """G3: opt-in code graph — index, search via cues, impact, remove."""
    repo = tmp_path / "mini-repo"
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "lib.py").write_text("def orbital_planner():\n    return 1\n")
    (repo / "src" / "app.py").write_text(
        "from lib import orbital_planner\ndef main():\n    orbital_planner()\n"
    )

    provider = FrankenmemoryProvider(command=FM_BIN, env={"FM_DB_PATH": str(tmp_path / "fm.db")})
    await asyncio.create_task(provider.initialize())

    scope = {"owner": "alice", "workspace_id": provider._workspace_id}
    indexed = await provider._call_tool("code_index", {"action": "index", "path": str(repo), **scope})
    assert indexed["files_indexed"] == 2
    assert indexed["symbols"] == 2

    status = await provider._call_tool("code_index", {"action": "status", "path": str(repo), **scope})
    assert status["files"] == 2

    hits = await provider._call_tool("graph_walk", {"op": "cues", "query": "orbital planner", **scope})
    assert any(h["node"]["name"].endswith("::orbital_planner") for h in hits["hits"])

    impact = await provider._call_tool(
        "code_index", {"action": "impact", "path": str(repo), "rel_path": "src/lib.py", **scope}
    )
    assert any(f.endswith("src/app.py") for f in impact["impacted_files"])

    removed = await provider._call_tool("code_index", {"action": "remove", "path": str(repo), **scope})
    assert removed["files_removed"] == 2

    await asyncio.create_task(provider.shutdown())


@needs_fm
async def test_two_process_owner_scope_isolates_curated_raw_and_graph(tmp_path):
    """Two authenticated fm-mcp processes may share one DB, never one scope."""
    db_path = str((tmp_path / "shared" / "fm.db").resolve())
    workspace = "shared-workspace"
    alice = FrankenmemoryProvider(
        command=FM_BIN,
        workspace_id=workspace,
        env={
            "FM_DB_PATH": db_path,
            "FM_OWNER": "alice",
            "FM_WORKSPACE_ID": workspace,
        },
    )
    bob = FrankenmemoryProvider(
        command=FM_BIN,
        workspace_id=workspace,
        env={
            "FM_DB_PATH": db_path,
            "FM_OWNER": "bob",
            "FM_WORKSPACE_ID": workspace,
        },
    )
    await asyncio.gather(alice.initialize(), bob.initialize())

    try:
        alice_record = await alice.remember("Alice keeps the amber key.", owner="alice")
        bob_record = await bob.remember("Bob keeps the cobalt key.", owner="bob")

        alice_hits, bob_hits = await asyncio.gather(
            alice.recall("keeps key", owner="alice", top_k=10),
            bob.recall("keeps key", owner="bob", top_k=10),
        )
        assert {hit.memory.id for hit in alice_hits} == {alice_record.id}
        assert {hit.memory.id for hit in bob_hits} == {bob_record.id}

        alice_raw, bob_raw = await asyncio.gather(
            alice.inspect_tier("raw", owner="alice"),
            bob.inspect_tier("raw", owner="bob"),
        )
        assert all(row.get("owner") == "alice" for row in alice_raw)
        assert all(row.get("owner") == "bob" for row in bob_raw)

        graph_input = {
            "nodes": [{"kind": "person", "name": "shared-name"}],
            "edges": [
                {
                    "src": {"kind": "person", "name": "shared-name"},
                    "tag": "keeps",
                    "dst": {"kind": "object", "name": "private-key"},
                    "fact": "private scoped fact",
                }
            ],
            "cues": [
                {"cue": "private key", "node": {"kind": "object", "name": "private-key"}}
            ],
        }
        await alice._call_tool(
            "graph_upsert", {**graph_input, "owner": "alice", "workspace_id": workspace}
        )
        await bob._call_tool(
            "graph_upsert", {**graph_input, "owner": "bob", "workspace_id": workspace}
        )
        alice_cues = await alice._call_tool(
            "graph_walk",
            {"op": "cues", "query": "private key", "owner": "alice", "workspace_id": workspace},
        )
        bob_cues = await bob._call_tool(
            "graph_walk",
            {"op": "cues", "query": "private key", "owner": "bob", "workspace_id": workspace},
        )
        alice_node = alice_cues["hits"][0]["node"]["id"]
        bob_node = bob_cues["hits"][0]["node"]["id"]
        assert alice_node != bob_node

        foreign_fetch = await bob._call_tool(
            "graph_walk",
            {"op": "fetch", "node_id": alice_node, "owner": "bob", "workspace_id": workspace},
        )
        foreign_rank = await bob._call_tool(
            "graph_walk",
            {"op": "rank", "node_id": alice_node, "owner": "bob", "workspace_id": workspace},
        )
        assert foreign_fetch["node"] is None
        assert foreign_rank["scores"] == []

        with pytest.raises(Exception, match="conflicts with authenticated process scope"):
            await bob.recall("key", owner="alice")
    finally:
        await asyncio.gather(alice.shutdown(), bob.shutdown())


async def test_resolved_question_stays_visible_in_provider_reads(tmp_path, monkeypatch):
    """S02 — a resolved question archives its compatibility row by design.

    The provider read paths must still surface the answer: ``versioned_list``
    hides only stale OPEN questions whose legacy row is gone, and ``list_page``
    surfaces the resolved answer on the first page even though the legacy
    cursor no longer returns the archived row.
    """
    from tests.test_memory_versioned import (
        _curated,
        _curated_add,
        _curated_archive,
        _question_block,
    )
    from src.memory_versioned import transition_knowledge

    path = str(tmp_path / "fm.db")
    _question_block(path, "name", "question-name")
    _question_block(path, "stale", "question-stale")
    _curated(path)
    _curated_add(path, "question-name")
    _curated_add(path, "question-stale", archived=1)
    transition_knowledge(
        "question-name", "resolve", owner="alice", expected_revision=1,
        value="E", db_path=path,
    )
    # The legacy resolve archives the compatibility row; the stale question's
    # row was archived without resolution.  From the wire side both are gone.
    _curated_archive(path, "question-name", 1)

    provider = FrankenmemoryProvider(
        command="/unused",
        env={"FM_DB_PATH": path, "FM_WORKSPACE_ID": "global"},
    )

    async def fake_call_tool(name, arguments):
        if name == "list_memories":
            return {"records": [], "next_cursor": None}
        if name == "get_memory":
            return {}
        raise AssertionError(f"unexpected tool call: {name}")

    monkeypatch.setattr(provider, "_call_tool", fake_call_tool)

    listed = await provider.versioned_list(owner="alice")
    listed_ids = {str(item.get("id")) for item in listed}
    assert "question-name" in listed_ids
    assert "question-stale" not in listed_ids

    page, _cursor = await provider.list_page(owner="alice", cursor=None)
    page_by_id = {str(record.id): record for record in page}
    assert "question-name" in page_by_id
    assert page_by_id["question-name"].text == "E"
    assert "question-stale" not in page_by_id


_BINDINGS_SCHEMA = """
CREATE TABLE fm_v2_principal_bindings (
    owner_id TEXT NOT NULL,
    workspace_key TEXT NOT NULL DEFAULT '',
    project_key TEXT NOT NULL DEFAULT '',
    binding_key TEXT NOT NULL,
    assistant_entity_id TEXT NOT NULL,
    handler_entity_id TEXT NOT NULL,
    revision INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(owner_id, workspace_key, project_key, binding_key)
)
"""


async def test_reset_and_purge_drop_python_principal_bindings(tmp_path, monkeypatch):
    """S03 — fm_v2_principal_bindings is Python-created and unknown to the
    Rust schema, so the provider wrapper closes that gap after a successful
    owner reset/purge; a stale binding would otherwise resurrect references
    to entities the reset just erased."""
    import sqlite3

    path = str(tmp_path / "fm.db")
    with sqlite3.connect(path) as conn:
        conn.execute(_BINDINGS_SCHEMA)
        conn.execute(
            "INSERT INTO fm_v2_principal_bindings VALUES ('alice','','','binding','a','h',1,'t','t')"
        )
        conn.execute(
            "INSERT INTO fm_v2_principal_bindings VALUES ('bob','','','binding','a','h',1,'t','t')"
        )

    provider = FrankenmemoryProvider(
        command="/unused",
        env={"FM_DB_PATH": path, "FM_WORKSPACE_ID": "global"},
    )

    async def fake_call_tool(name, arguments):
        assert name == "owner_lifecycle"
        if arguments["action"] == "purge":
            return {"purged": True, "counts": {}}
        return {"complete": True, "categories": {}}

    monkeypatch.setattr(provider, "_call_tool", fake_call_tool)

    await provider.reset_owner(
        "reset_commit", owner="alice", components=["memories"], expected_counts={}
    )
    with sqlite3.connect(path) as conn:
        remaining = {
            row[0]
            for row in conn.execute("SELECT owner_id FROM fm_v2_principal_bindings")
        }
    assert remaining == {"bob"}

    await provider.purge_owner(owner="bob")
    with sqlite3.connect(path) as conn:
        remaining = conn.execute(
            "SELECT count(*) FROM fm_v2_principal_bindings"
        ).fetchone()
    assert remaining == (0,)
