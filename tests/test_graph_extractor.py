"""Graph enrichment — the Odysseus port of mimo's graph-extract.ts.

Turn capture moved wholly to the Odysseus post-response seam, so the graph
extraction that used to ride mimo's child-side capture must exist here
feature-for-feature: the per-session throttle policy, the forgiving wire
schema small models actually emit, the acceptance gate (enrich only when
the capture admitted a candidate), and the rule that extraction failures
never touch the captured record.
"""

import sys
import types
from types import SimpleNamespace

import pytest

from services.memory.graph_extractor import (
    capture_turn_and_enrich,
    extract_and_upsert,
    normalize_extraction,
    should_extract,
)


class TestThrottlePolicy:
    def test_defaults_extract_every_turn(self):
        assert should_extract({}, {"turns": 0, "last_extract_ms": 0}, 1000)
        assert should_extract({}, {"turns": 5, "last_extract_ms": 0}, 1000)

    def test_every_n_turns(self):
        cfg = {"every_n_turns": 3}
        assert should_extract(cfg, {"turns": 0, "last_extract_ms": 0}, 0)
        assert not should_extract(cfg, {"turns": 1, "last_extract_ms": 0}, 0)
        assert not should_extract(cfg, {"turns": 2, "last_extract_ms": 0}, 0)
        assert should_extract(cfg, {"turns": 3, "last_extract_ms": 0}, 0)

    def test_min_interval_gates_by_time(self):
        cfg = {"min_interval_seconds": 60}
        assert should_extract(cfg, {"turns": 0, "last_extract_ms": 0}, 1_000_000)
        assert not should_extract(cfg, {"turns": 0, "last_extract_ms": 1_000_000}, 1_030_000)
        assert should_extract(cfg, {"turns": 0, "last_extract_ms": 1_000_000}, 1_061_000)


class TestWireSchemaNormalization:
    def test_canonical_shape(self):
        nodes, edges, cues = normalize_extraction({
            "entities": [{"kind": "person", "name": "Ada"}],
            "cues": ["ada"],
            "edges": [{
                "src": {"kind": "person", "name": "Ada"},
                "tag": "works_on",
                "dst": {"kind": "project", "name": "loom"},
                "fact": "Ada works on loom.",
            }],
        })
        assert nodes == [{"kind": "person", "name": "Ada"}]
        assert edges == [{
            "src": {"kind": "person", "name": "Ada"},
            "tag": "works_on",
            "dst": {"kind": "project", "name": "loom"},
            "fact": "Ada works on loom.",
        }]
        assert cues == ["ada"]

    def test_observed_model_drift_aliases(self):
        """nodes/type/source/target/bare-string sides — all observed live."""
        nodes, edges, cues = normalize_extraction({
            "nodes": [{"type": "tool", "name": "fm-mcp"}],
            "edges": [{
                "source": "fm-mcp",
                "tag": "uses",
                "target": "SQLite",
                "cues": ["fts5"],
            }],
        })
        assert nodes == [{"kind": "tool", "name": "fm-mcp"}]
        assert edges == [{
            "src": {"kind": "tool", "name": "fm-mcp"},
            "tag": "uses",
            "dst": {"kind": "concept", "name": "SQLite"},
            "fact": "",
        }]
        assert cues == ["fts5"]

    def test_edges_missing_a_side_are_dropped(self):
        _, edges, _ = normalize_extraction({"edges": [{"tag": "uses", "src": "x"}]})
        assert edges == []


class _Provider:
    def __init__(self, record_ids):
        self._record_ids = record_ids
        self.capture_calls = []
        self.tool_calls = []

    async def capture(self, user_text, assistant_text, *, owner=None, session_id=None):
        self.capture_calls.append(user_text)
        return {"record_ids": self._record_ids}

    async def _call_tool(self, name, args):
        self.tool_calls.append((name, args))
        return {}


def _stub_llm(monkeypatch, response):
    llm_mod = types.ModuleType("src.llm_core")

    async def fake_llm_call_async(url, model, messages, **kwargs):
        return response

    llm_mod.llm_call_async = fake_llm_call_async
    monkeypatch.setitem(sys.modules, "src.llm_core", llm_mod)


SAMPLE_RESPONSE = (
    '{"entities":[{"kind":"person","name":"Ada"}],"cues":["ada"],'
    '"edges":[{"src":{"kind":"person","name":"Ada"},"tag":"works_on",'
    '"dst":{"kind":"project","name":"loom"},"fact":"Ada works on loom."}]}'
)


@pytest.mark.asyncio
async def test_accepted_capture_triggers_graph_upsert(monkeypatch):
    _stub_llm(monkeypatch, SAMPLE_RESPONSE)
    provider = _Provider(record_ids=["raw_1", "m_123"])
    await capture_turn_and_enrich(
        provider, "ada works on loom", "Noted.",
        session_id="ses_g1", owner="alice",
        endpoint_url="http://task", model="task-model", headers={},
    )
    assert provider.capture_calls == ["ada works on loom"]
    assert len(provider.tool_calls) == 1
    name, args = provider.tool_calls[0]
    assert name == "graph_upsert"
    assert args["owner"] == "alice"
    assert args["workspace_id"] == "global"
    assert args["nodes"] == [{"kind": "person", "name": "Ada"}]
    assert args["cues"] == [{"cue": "ada", "node": {"kind": "person", "name": "Ada"}}]


@pytest.mark.asyncio
async def test_raw_only_capture_skips_enrichment(monkeypatch):
    _stub_llm(monkeypatch, SAMPLE_RESPONSE)
    provider = _Provider(record_ids=["raw_1", "raw_2"])
    await capture_turn_and_enrich(
        provider, "just chatter", "ok",
        session_id="ses_g2", owner="alice",
        endpoint_url="http://task", model="task-model", headers={},
    )
    assert provider.capture_calls == ["just chatter"]
    assert provider.tool_calls == []


@pytest.mark.asyncio
async def test_extraction_failure_never_touches_the_capture(monkeypatch):
    llm_mod = types.ModuleType("src.llm_core")

    async def exploding(*a, **k):
        raise ConnectionError("task endpoint down")

    llm_mod.llm_call_async = exploding
    monkeypatch.setitem(sys.modules, "src.llm_core", llm_mod)

    provider = _Provider(record_ids=["m_1"])
    await capture_turn_and_enrich(
        provider, "ada works on loom", "Noted.",
        session_id="ses_g3", owner="alice",
        endpoint_url="http://task", model="task-model", headers={},
    )
    assert provider.capture_calls == ["ada works on loom"]
    assert provider.tool_calls == []


@pytest.mark.asyncio
async def test_unparseable_output_is_dropped(monkeypatch):
    _stub_llm(monkeypatch, "sorry, I cannot produce JSON today")
    provider = _Provider(record_ids=["m_1"])
    await extract_and_upsert(
        provider, "u", "a",
        session_id="ses_g4", owner="alice",
        endpoint_url="http://task", model="task-model", headers={},
    )
    assert provider.tool_calls == []


@pytest.mark.asyncio
async def test_empty_extraction_skips_upsert(monkeypatch):
    _stub_llm(monkeypatch, '{"entities":[],"cues":[],"edges":[]}')
    provider = _Provider(record_ids=["m_1"])
    await extract_and_upsert(
        provider, "u", "a",
        session_id="ses_g5", owner="alice",
        endpoint_url="http://task", model="task-model", headers={},
    )
    assert provider.tool_calls == []
