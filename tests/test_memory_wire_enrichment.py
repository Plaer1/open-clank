"""Slice 01 (memory trust + Brain metaplan): the full fm record reaches
the API.

Three layers under test:
1. FrankenmemoryProvider._record maps every wire field (they used to
   fall on the floor).
2. /api/memory serializes the enriched record; /api/memory/graph and
   /api/memory/digest-preview exist, validate, and owner-scope.
3. Live fm-mcp: enriched digest pinned entries + graph overview op
   (real binary, no fakes — house order).
"""
import asyncio
import os
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException

import routes.memory_routes as mr
from src.frankenmemory_provider import FrankenmemoryProvider
from src.memory_digest import render_digest
from src.memory_provider import MemoryRecord

FM_BIN = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "mcp_servers/frankenmemory/target/release/fm-mcp",
)
needs_fm = pytest.mark.skipif(not os.path.exists(FM_BIN), reason="fm-mcp release binary not built")

WIRE_RECORD = {
    "id": "mem_1",
    "content": "always answer in metric units",
    "kind": "instruction",
    "priority": 7,
    "trust_score": 0.9,
    "confidence_score": 0.8,
    "importance_score": 0.7,
    "scene_name": "workshop",
    "source": "chat",
    "source_type": "human",
    "owner": "alice",
    "workspace_id": "global",
    "workspace_path": "/home/alice/repo",
    "session_id": "sess-1",
    "tags": ["units", "style"],
    "source_message_ids": ["msg-1", "msg-2"],
    "created_at": "2026-07-17T00:00:00Z",
    "updated_at": "2026-07-17T01:00:00Z",
    "archived": False,
    "last_accessed_at": "2026-07-17T02:00:00Z",
    "exempt_from_decay": True,
    "exempt_from_dedup": False,
    "metadata": {"pinned": True},
}


def test_record_mapping_carries_full_signal_set():
    record = FrankenmemoryProvider._record(WIRE_RECORD)
    assert record.kind == "instruction"
    assert record.source_type == "human"
    assert record.priority == 7
    assert record.trust_score == pytest.approx(0.9)
    assert record.confidence_score == pytest.approx(0.8)
    assert record.importance_score == pytest.approx(0.7)
    assert record.scene_name == "workshop"
    assert record.tags == ["units", "style"]
    assert record.source_message_ids == ["msg-1", "msg-2"]
    assert record.workspace_path == "/home/alice/repo"
    assert record.archived is False
    assert record.exempt_from_decay is True
    assert record.exempt_from_dedup is False
    assert record.last_accessed_at == "2026-07-17T02:00:00Z"
    assert record.pinned is True


def test_record_mapping_defaults_when_fields_absent():
    record = FrankenmemoryProvider._record({"id": "m", "content": "x"})
    assert record.kind == "fact"
    assert record.source_type == "human"
    assert record.priority is None
    assert record.trust_score is None
    assert record.tags == []
    assert record.source_message_ids == []
    assert record.archived is False
    assert record.exempt_from_decay is False
    assert record.last_accessed_at is None


def test_native_record_defaults_read_as_hand_authored():
    record = MemoryRecord(id="n1", text="native note")
    assert record.kind == "fact"
    assert record.source_type == "human"
    assert record.tags == []


# ---------------------------------------------------------------- routes


class _StubProvider:
    provider_id = "stub"

    def __init__(self, records=None, digest=None):
        self.records = records or []
        self._digest = digest
        self.graph_calls = []

    async def list_memories(self, *, owner=None, limit=1000):
        return list(self.records)

    async def digest(self, *, owner=None):
        return self._digest

    async def graph(self, op, *, owner=None, **kwargs):
        self.graph_calls.append((op, owner, kwargs))
        return {"op": op, "nodes": [], "edges": [], "node_total": 0, "edge_total": 0}


def _route(router, path, method):
    for r in router.routes:
        if r.path == path and method in getattr(r, "methods", set()):
            return r.endpoint
    raise AssertionError(path)


def _router(monkeypatch, caller, provider):
    monkeypatch.setattr(mr, "get_current_user", lambda request: caller, raising=False)
    sm = MagicMock()
    sm.sessions = {}
    mem = MagicMock()
    mem.load = lambda owner=None: []
    return mr.setup_memory_routes(mem, sm, memory_provider=provider)


def _full_record():
    return MemoryRecord(
        id="m1",
        text="always answer in metric units",
        timestamp=1,
        category="fact",
        source="chat",
        owner="alice",
        kind="instruction",
        source_type="human",
        priority=7,
        trust_score=0.9,
        confidence_score=0.8,
        importance_score=0.7,
        scene_name="workshop",
        tags=["units"],
        source_message_ids=["msg-1"],
        workspace_id="global",
        workspace_path="/home/alice/repo",
        archived=False,
        exempt_from_decay=True,
        exempt_from_dedup=False,
        last_accessed_at="2026-07-17T02:00:00Z",
        created_at="2026-07-17T00:00:00Z",
        updated_at="2026-07-17T01:00:00Z",
        uses=3,
        metadata={"pinned": True},
        pinned=True,
    )


def test_list_payload_serializes_enriched_record(monkeypatch):
    provider = _StubProvider(records=[_full_record()])
    router = _router(monkeypatch, "alice", provider)
    endpoint = _route(router, "/api/memory", "GET")
    payload = asyncio.run(endpoint(request=None))
    row = payload["memory"][0]
    for key in (
        "kind", "source_type", "priority", "trust_score", "confidence_score",
        "importance_score", "scene_name", "tags", "source_message_ids",
        "workspace_id", "workspace_path", "archived", "exempt_from_decay",
        "exempt_from_dedup", "last_accessed_at", "created_at", "updated_at",
        "uses",
    ):
        assert key in row, key
    assert row["kind"] == "instruction"
    assert row["source_type"] == "human"
    assert row["trust_score"] == pytest.approx(0.9)
    assert row["tags"] == ["units"]
    assert row["exempt_from_decay"] is True


def test_graph_endpoint_scopes_to_caller_and_validates_op(monkeypatch):
    provider = _StubProvider()
    router = _router(monkeypatch, "alice", provider)
    endpoint = _route(router, "/api/memory/graph", "GET")

    result = asyncio.run(endpoint(request=None, op="overview", limit=10))
    assert result["op"] == "overview"
    op, owner, kwargs = provider.graph_calls[0]
    assert op == "overview"
    assert owner == "alice", "graph must be owner-scoped to the caller"

    with pytest.raises(HTTPException) as exc:
        asyncio.run(endpoint(request=None, op="drop_tables"))
    assert exc.value.status_code == 400


class _GraphlessProvider:
    """Native-provider shape: no graph attribute at all."""

    provider_id = "native"

    async def list_memories(self, *, owner=None, limit=1000):
        return []


def test_graph_endpoint_503_when_provider_lacks_graph(monkeypatch):
    router = _router(monkeypatch, "alice", _GraphlessProvider())
    endpoint = _route(router, "/api/memory/graph", "GET")
    with pytest.raises(HTTPException) as exc:
        asyncio.run(endpoint(request=None, op="overview"))
    assert exc.value.status_code == 503


def test_digest_preview_matches_shared_renderer(monkeypatch):
    import routes.prefs_routes as prefs_routes
    from src.memory_digest import render_split

    monkeypatch.setattr(prefs_routes, "_load_for_user", lambda user=None: {})
    digest = {
        "counts": {"by_kind": {"fact": 1}, "by_tier": {"curated": 1, "raw": 0}, "candidates_pending": 0},
        "pinned": [{
            "id": "m1", "headline": "always answer in metric units",
            "content": "always answer in metric units", "kind": "instruction",
            "source_type": "human", "pinned": True,
        }],
        "clusters": [{"label": "units", "size": 2, "last_touched": "now"}],
        "recent": [{"topic": "boilers"}],
        "generated_at": "now",
    }
    provider = _StubProvider(digest=digest)
    router = _router(monkeypatch, "alice", provider)
    endpoint = _route(router, "/api/memory/digest-preview", "GET")
    payload = asyncio.run(endpoint(request=None))
    assert payload["digest"] == digest
    assert payload["rendered"] == render_digest(digest), "byte-identical, no drift"
    # The preview shows the SAME split injection produces (slice 05).
    trusted_block, untrusted_card = render_split(digest, {})
    assert payload["trusted_block"] == trusted_block
    assert payload["untrusted_card"] == untrusted_card
    assert "metric units" in payload["trusted_block"]
    assert "metric units" not in payload["untrusted_card"]


# ------------------------------------------------------------- live fm


@needs_fm
async def test_live_digest_pinned_entries_are_enriched(tmp_path):
    provider = FrankenmemoryProvider(command=FM_BIN, env={"FM_DB_PATH": str(tmp_path / "fm.db")})
    await asyncio.create_task(provider.initialize())
    try:
        record = await provider.remember(
            "the boiler reset code is 4711",
            owner="alice",
            metadata={"pinned": True},
        )
        assert record is not None
        digest = await provider.digest(owner="alice")
        assert digest and digest["pinned"], "pinned entry must appear in digest"
        entry = digest["pinned"][0]
        for key in ("id", "headline", "content", "kind", "source_type", "pinned"):
            assert key in entry, key
        assert entry["content"] == "the boiler reset code is 4711"
        assert entry["pinned"] is True
    finally:
        await asyncio.create_task(provider.shutdown())


@needs_fm
async def test_live_graph_overview_scoped_round_trip(tmp_path):
    provider = FrankenmemoryProvider(command=FM_BIN, env={"FM_DB_PATH": str(tmp_path / "fm.db")})
    await asyncio.create_task(provider.initialize())
    try:
        await provider._call_tool(
            "graph_upsert",
            {
                "owner": "alice",
                "workspace_id": provider._workspace_id,
                "nodes": [],
                "edges": [{
                    "src": {"kind": "person", "name": "e"},
                    "tag": "works_on",
                    "dst": {"kind": "project", "name": "open-clank"},
                    "fact": "e works on open-clank",
                }],
                "cues": [],
            },
        )
        overview = await provider.graph("overview", owner="alice", limit=50)
        assert overview["op"] == "overview"
        assert overview["node_total"] == 2
        assert len(overview["nodes"]) == 2
        assert overview["edges"], "edge between returned nodes must ride"
        node_ids = {n["id"] for n in overview["nodes"]}
        for edge in overview["edges"]:
            assert edge["src_id"] in node_ids and edge["dst_id"] in node_ids

        foreign = await provider.graph("overview", owner="mallory", limit=50)
        assert foreign["nodes"] == [] and foreign["node_total"] == 0
    finally:
        await asyncio.create_task(provider.shutdown())
