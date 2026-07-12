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

import pytest

from src.frankenmemory_provider import FrankenmemoryProvider

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
        "recall", {"query": "anything", "top_k": 3, "tier": "curated", "workspace_id": "global"}
    )
    assert isinstance(result, dict)

    await asyncio.create_task(provider.shutdown())


@needs_fm
async def test_recall_cross_task_returns_list_without_warnings(tmp_path, caplog):
    provider = FrankenmemoryProvider(command=FM_BIN, env={"FM_DB_PATH": str(tmp_path / "fm.db")})
    await asyncio.create_task(provider.initialize())

    with caplog.at_level(logging.WARNING, logger="src.frankenmemory_provider"):
        hits = await asyncio.create_task(provider.recall("nothing stored yet"))

    assert hits == []
    assert "recall failed" not in caplog.text

    await asyncio.create_task(provider.shutdown())


@needs_fm
async def test_concurrent_recalls_from_separate_tasks(tmp_path):
    provider = FrankenmemoryProvider(command=FM_BIN, env={"FM_DB_PATH": str(tmp_path / "fm.db")})
    await asyncio.create_task(provider.initialize())

    results = await asyncio.gather(
        *(asyncio.create_task(provider.recall(f"query {i}")) for i in range(4))
    )
    assert all(r == [] for r in results)

    await asyncio.create_task(provider.shutdown())


async def test_failure_logs_are_never_empty(tmp_path, caplog):
    """When the transport is genuinely broken, the reason must be visible —
    the empty-`str()` class of exceptions produced the infamous
    'frankenmemory recall failed: ' log with nothing after the colon."""
    provider = FrankenmemoryProvider(command="/nonexistent/fm-mcp", env={"FM_DB_PATH": str(tmp_path / "fm.db")})

    with caplog.at_level(logging.WARNING, logger="src.frankenmemory_provider"):
        hits = await provider.recall("boom")

    assert hits == []
    failure_lines = [r.getMessage() for r in caplog.records if "recall failed" in r.getMessage()]
    assert failure_lines, "expected a recall-failed warning"
    for line in failure_lines:
        reason = line.split("recall failed:", 1)[1].strip()
        assert reason, f"recall-failed log carries no reason: {line!r}"


@needs_fm
async def test_recall_round_trip_returns_seeded_hits(tmp_path):
    """E0.1b: fm-mcp recall must return structured memories the provider can
    parse. Today it answers prose ('Strategy: ... Results: N memories'), the
    JSON parse falls back to {'raw': ...} and recall yields zero hits forever."""
    provider = FrankenmemoryProvider(command=FM_BIN, env={"FM_DB_PATH": str(tmp_path / "fm.db")})
    await asyncio.create_task(provider.initialize())

    await provider.remember(
        "The lemon cake recipe is written in the blue notebook on the third shelf.",
        session_id="ses_transport_test",
        source="user",
    )
    hits = await provider.recall("blue notebook lemon cake recipe", top_k=5)

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

    updated = await provider.update(record.id, text="The blue notebook is on shelf three.", owner="alice")
    assert updated is not None
    assert updated.text == "The blue notebook is on shelf three."
    assert await provider.pin(record.id, True, owner="alice")
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

    cues = await provider._call_tool("graph_walk", {"op": "cues", "query": "memory engine details"})
    assert cues["hits"], "cue lookup must find the entry node"
    fm_node = cues["hits"][0]["node"]
    assert fm_node["name"] == "frankenmemory"

    tags = await provider._call_tool("graph_walk", {"op": "tags", "node_id": fm_node["id"]})
    assert any(t["tag"] == "uses" and t["direction"] == "in" for t in tags["tags"])

    hits = await provider._call_tool(
        "graph_walk", {"op": "expand", "node_id": fm_node["id"], "direction": "in"}
    )
    assert hits["hits"][0]["other"]["name"] == "open-clank"
    assert "memory" in hits["hits"][0]["edge"]["fact"]

    trace = await provider._call_tool(
        "graph_walk",
        {"op": "trace", "node_id": fm_node["id"], "dst_id": cues["hits"][0]["node"]["id"]},
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
    normalized = await provider._call_tool("groom", {"op": "tag_normalize"})
    assert normalized["records_merged"] == 1

    decayed = await provider._call_tool("groom", {"op": "edge_decay", "dry_run": True})
    assert "records_archived" in decayed

    hits = await provider._call_tool("graph_walk", {"op": "cues", "query": "frankenmemory"})
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

    indexed = await provider._call_tool("code_index", {"action": "index", "path": str(repo)})
    assert indexed["files_indexed"] == 2
    assert indexed["symbols"] == 2

    status = await provider._call_tool("code_index", {"action": "status", "path": str(repo)})
    assert status["files"] == 2

    hits = await provider._call_tool("graph_walk", {"op": "cues", "query": "orbital planner"})
    assert any(h["node"]["name"].endswith("::orbital_planner") for h in hits["hits"])

    impact = await provider._call_tool(
        "code_index", {"action": "impact", "path": str(repo), "rel_path": "src/lib.py"}
    )
    assert any(f.endswith("src/app.py") for f in impact["impacted_files"])

    removed = await provider._call_tool("code_index", {"action": "remove", "path": str(repo)})
    assert removed["files_removed"] == 2

    await asyncio.create_task(provider.shutdown())
