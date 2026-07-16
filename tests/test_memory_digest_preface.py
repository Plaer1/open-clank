"""SLICE-04 — the per-turn memory index card at the Odysseus preface seam.

D1 locked: in provider mode the digest block REPLACES the old top_k=3
auto-recall preface; pull-based recall (the memory search tool) is the only
retrieval path. Digest failure degrades to no block. Native mode (no
provider) keeps its legacy pinned + hybrid-retrieve preface untouched.
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from src.memory_digest import DIGEST_SENTINEL, render_digest


SAMPLE_DIGEST = {
    "counts": {
        "by_kind": {"persona": 1, "wiki": 2},
        "by_tier": {"raw": 12, "curated": 3, "candidates_pending": 2},
        "candidates_pending": 2,
    },
    "pinned": [{"headline": "Keeper of the amber greenhouse ledger", "kind": "persona"}],
    "clusters": [{"label": "keeps", "size": 4, "last_touched": "2026-07-15T00:00:00Z"}],
    "recent": [{"topic": "observatory code", "at": "2026-07-15T00:00:00Z"}],
    "generated_at": "2026-07-15T00:00:00Z",
}


class TestRenderDigest:
    def test_renders_all_sections(self):
        block = render_digest(SAMPLE_DIGEST)
        assert block.startswith(DIGEST_SENTINEL)
        assert "3 curated, 12 raw, 2 candidates pending review" in block
        assert "- Keeper of the amber greenhouse ledger" in block
        assert "Threads: keeps (4)" in block
        assert "Recent topics: observatory code" in block
        assert "memory search tool" in block

    def test_empty_bank_renders_nothing(self):
        empty = {
            "counts": {"by_kind": {}, "by_tier": {"raw": 0, "curated": 0}, "candidates_pending": 0},
            "pinned": [],
            "clusters": [],
            "recent": [],
            "generated_at": "now",
        }
        assert render_digest(empty) == ""

    def test_malformed_input_renders_nothing(self):
        assert render_digest(None) == ""
        assert render_digest({"counts": "garbage-string"} if False else {"pinned": "x"}) == ""


def _processor(provider):
    from src.chat_processor import ChatProcessor

    processor = ChatProcessor.__new__(ChatProcessor)
    processor.memory_provider = provider
    processor.memory_manager = MagicMock()
    processor.memory_vector = None
    processor.personal_docs_manager = SimpleNamespace(rag_manager=None)
    processor.skills_manager = None
    return processor


def _preface(processor, **kwargs):
    return asyncio.get_event_loop().run_until_complete(
        processor.build_context_preface(
            "what about the greenhouse?",
            SimpleNamespace(history=[]),
            use_web=False,
            use_rag=False,
            use_skills=False,
            owner="alice",
            **kwargs,
        )
    )


class _DigestProvider:
    provider_id = "frankenmemory"

    def __init__(self, digest=SAMPLE_DIGEST, fail=False):
        self._digest = digest
        self._fail = fail
        self.recall_calls = 0
        self.digest_calls = 0

    async def digest(self, *, owner=None):
        self.digest_calls += 1
        if self._fail:
            raise ConnectionError("fm is down")
        return self._digest

    async def recall(self, query, *, owner=None, top_k=5):
        self.recall_calls += 1
        return []


@pytest.mark.asyncio
async def test_provider_mode_injects_digest_and_skips_auto_recall():
    provider = _DigestProvider()
    processor = _processor(provider)
    preface, _, _ = await processor.build_context_preface(
        "hello", SimpleNamespace(history=[]),
        use_web=False, use_rag=False, use_skills=False, owner="alice",
    )
    blocks = [m for m in preface if DIGEST_SENTINEL in str(m.get("content", ""))]
    assert len(blocks) == 1
    assert provider.digest_calls == 1
    assert provider.recall_calls == 0, "D1: digest replaces auto-recall"
    assert processor._last_used_memories == [], "an index is not a used memory"


@pytest.mark.asyncio
async def test_digest_failure_degrades_to_no_block():
    provider = _DigestProvider(fail=True)
    processor = _processor(provider)
    preface, _, _ = await processor.build_context_preface(
        "hello", SimpleNamespace(history=[]),
        use_web=False, use_rag=False, use_skills=False, owner="alice",
    )
    assert not [m for m in preface if DIGEST_SENTINEL in str(m.get("content", ""))]


@pytest.mark.asyncio
async def test_use_memory_false_means_no_digest():
    provider = _DigestProvider()
    processor = _processor(provider)
    preface, _, _ = await processor.build_context_preface(
        "hello", SimpleNamespace(history=[]),
        use_web=False, use_rag=False, use_skills=False, owner="alice",
        use_memory=False,
    )
    assert provider.digest_calls == 0
    assert not [m for m in preface if DIGEST_SENTINEL in str(m.get("content", ""))]


@pytest.mark.asyncio
async def test_provider_without_digest_keeps_legacy_recall():
    class _LegacyProvider:
        provider_id = "native"

        def __init__(self):
            self.recall_calls = 0

        async def recall(self, query, *, owner=None, top_k=5):
            self.recall_calls += 1
            return []

    provider = _LegacyProvider()
    processor = _processor(provider)
    await processor.build_context_preface(
        "hello", SimpleNamespace(history=[]),
        use_web=False, use_rag=False, use_skills=False, owner="alice",
    )
    assert provider.recall_calls == 1


@pytest.mark.asyncio
async def test_pull_recall_tool_routes_through_provider(monkeypatch):
    """The deep-recall half of the overlay: the agent memory tool's search
    action must hit provider.recall (top_k=20) — pinned so the pull path
    can't silently regress to the native manager."""
    import src.ai_interaction as ai_interaction

    calls = {}

    class _Provider:
        async def recall(self, query, *, owner=None, top_k=5):
            calls["query"] = query
            calls["owner"] = owner
            calls["top_k"] = top_k
            return []

    monkeypatch.setattr(ai_interaction, "_memory_provider", _Provider())
    result = await ai_interaction.do_manage_memory("search\namber ledger", owner="alice")
    assert calls == {"query": "amber ledger", "owner": "alice", "top_k": 20}
    assert "No memories found" in result["results"]


@pytest.mark.asyncio
async def test_native_mode_unchanged():
    processor = _processor(None)
    processor.memory_manager.load.return_value = []
    preface, _, _ = await processor.build_context_preface(
        "hello", SimpleNamespace(history=[]),
        use_web=False, use_rag=False, use_skills=False, owner="alice",
    )
    processor.memory_manager.load.assert_called_once_with(owner="alice")
    assert not [m for m in preface if DIGEST_SENTINEL in str(m.get("content", ""))]
