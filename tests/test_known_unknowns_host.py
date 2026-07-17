"""Known-unknowns slice 03: open questions flow through both hosts.

Layers under test:
1. Renderer: digest.open_questions renders INSIDE the endorsed guidance
   block with weave-in framing (U6); human-only defense (K5); the
   untrusted card never carries the questions.
2. manage_memory: add with category unknown/question normalizes and
   stores an open question (U7b directed add); resolve closes one with
   provenance; recall_memory annotates a pulled question as a question.
3. Live fm-mcp (real binary, house order): manual unknown add surfaces
   in digest.open_questions and recall; auto-capture with the category
   is rejected end-to-end (U2 firewall); passive resolution closes the
   question when the answering fact is remembered (U7c); the resolve
   surface archives with provenance (U7a).
"""
import asyncio
import os

import pytest

from src.frankenmemory_provider import FrankenmemoryProvider
from src.memory_digest import (
    DIGEST_SENTINEL,
    TRUST_SENTINEL,
    render_split,
    render_trusted_block,
)

FM_BIN = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "mcp_servers/frankenmemory/target/release/fm-mcp",
)
needs_fm = pytest.mark.skipif(not os.path.exists(FM_BIN), reason="fm-mcp release binary not built")

PREFS = {"memory_trust_auto": False, "memory_trust_auto_kinds": {}}


def _digest(open_questions=None, pinned=None):
    return {
        "counts": {"by_tier": {"curated": 2, "raw": 0}, "candidates_pending": 0},
        "pinned": pinned or [],
        "clusters": [],
        "recent": [],
        "open_questions": open_questions if open_questions is not None else [],
    }


QUESTION = {
    "id": "q1",
    "content": "user's name?",
    "workspace_id": "global",
    "source_type": "human",
}


# ------------------------------------------------------------- renderer


def test_open_questions_render_inside_endorsed_block():
    block = render_trusted_block(_digest(open_questions=[QUESTION]), PREFS)
    assert block.startswith(TRUST_SENTINEL)
    assert "Open questions the user wants answered:" in block
    assert "- user's name?" in block
    assert "weave the question in" in block.lower() or "weave" in block.lower()
    assert "never interrogate" in block.lower()


def test_open_questions_stand_alone_without_pinned_entries():
    # No trusted pinned entries at all — the questions still make a block.
    block = render_trusted_block(_digest(open_questions=[QUESTION]), PREFS)
    assert block
    assert "endorsed these memories" not in block


def test_no_questions_no_section():
    assert render_trusted_block(_digest(), PREFS) == ""
    block = render_trusted_block(
        _digest(pinned=[{
            "id": "p1", "kind": "instruction", "source_type": "human",
            "headline": "answer in metric", "content": "answer in metric",
            "pinned": True,
        }], open_questions=[]),
        PREFS,
    )
    assert "Open questions" not in block


def test_non_human_question_is_refused_k5():
    smuggled = dict(QUESTION, source_type="ai")
    assert render_trusted_block(_digest(open_questions=[smuggled]), PREFS) == ""
    blank = dict(QUESTION, source_type="")
    assert render_trusted_block(_digest(open_questions=[blank]), PREFS) == ""


def test_split_keeps_questions_out_of_the_untrusted_card():
    block, card = render_split(_digest(open_questions=[QUESTION]), PREFS)
    assert "user's name?" in block
    assert "user's name?" not in card
    assert card.startswith(DIGEST_SENTINEL)


def test_digest_fetch_timeout_is_shared_and_not_hair_trigger():
    """The old 250ms digest budget dropped the whole endorsed block
    (guidance + open questions) SILENTLY whenever fm stalled past it —
    observed live as 'the open question sometimes just isn't there'.
    Both hosts must share one constant, and it must not be hair-trigger."""
    import pathlib

    from src.memory_digest import DIGEST_FETCH_TIMEOUT_SECONDS

    assert DIGEST_FETCH_TIMEOUT_SECONDS >= 1.0
    for path in ("src/chat_processor.py", "src/openclank/acp_bridge.py"):
        source = pathlib.Path(path).read_text()
        assert "DIGEST_FETCH_TIMEOUT_SECONDS" in source, path
        assert "timeout=0.25" not in source, path


# ------------------------------------------------------- manage_memory


class _StubProvider:
    def __init__(self):
        self.remember_calls = []
        self.resolved = []
        self.resolve_result = True

    async def remember(self, text, *, owner=None, session_id=None,
                       category="fact", source="user", metadata=None,
                       workspace_id=None):
        from types import SimpleNamespace
        self.remember_calls.append({"text": text, "category": category})
        return SimpleNamespace(id="m_new", text=text, category=category)

    async def resolve_id(self, display_id, *, owner=None):
        return display_id

    async def resolve_question(self, memory_id, *, resolved_by=None, owner=None):
        self.resolved.append({"id": memory_id, "resolved_by": resolved_by})
        return self.resolve_result


@pytest.fixture
def stub_provider(monkeypatch):
    import src.ai_interaction as ai
    stub = _StubProvider()
    monkeypatch.setattr(ai, "_memory_provider", stub)
    return stub


async def test_manage_memory_add_question_normalizes(stub_provider):
    from src.ai_interaction import do_manage_memory
    result = await do_manage_memory("add\nusers   name??\nquestion", owner="alice")
    assert "error" not in result
    call = stub_provider.remember_calls[0]
    assert call["category"] == "unknown"
    assert call["text"] == "users name?"


async def test_manage_memory_resolve_round_trip(stub_provider):
    from src.ai_interaction import do_manage_memory
    result = await do_manage_memory("resolve\nq_123\nm_answer", owner="alice")
    assert result.get("action") == "resolve"
    assert stub_provider.resolved == [{"id": "q_123", "resolved_by": "m_answer"}]


async def test_manage_memory_resolve_refuses_non_questions(stub_provider):
    from src.ai_interaction import do_manage_memory
    stub_provider.resolve_result = False
    result = await do_manage_memory("resolve\nm_fact", owner="alice")
    assert "error" in result
    assert "open question" in result["error"]


async def test_recall_memory_marks_pulled_questions(monkeypatch):
    from types import SimpleNamespace
    import src.ai_interaction as ai

    hit = SimpleNamespace(memory=SimpleNamespace(
        id="q1", kind="unknown", category="unknown",
        text="user's name?", source_type="human", metadata={}, pinned=False,
    ))

    class _RecallProvider:
        async def recall(self, query, *, owner=None, top_k=5):
            return [hit]

    monkeypatch.setattr(ai, "_memory_provider", _RecallProvider())
    result = await ai.do_recall_memory("name", owner="alice")
    assert "[open question]" in result["results"]


# ------------------------------------------------------------- live fm


@needs_fm
async def test_live_unknown_lifecycle_add_recall_resolve(tmp_path):
    provider = FrankenmemoryProvider(command=FM_BIN, env={"FM_DB_PATH": str(tmp_path / "fm.db")})
    await asyncio.create_task(provider.initialize())
    try:
        record = await provider.remember(
            "users favorite editor", owner="alice", category="unknown",
        )
        assert record is not None

        digest = await provider.digest(owner="alice")
        questions = digest.get("open_questions") or []
        assert len(questions) == 1
        assert questions[0]["content"] == "users favorite editor?"
        assert questions[0]["source_type"] == "human"

        # Recall parity (U6): the question comes back like any record.
        hits = await provider.recall("favorite editor", owner="alice")
        assert any(h.memory.kind == "unknown" for h in hits)

        # Direct resolve (U7a): archived with provenance, leaves digest.
        question_id = questions[0]["id"]
        assert await provider.resolve_question(question_id, owner="alice")
        digest_after = await provider.digest(owner="alice")
        assert not (digest_after.get("open_questions") or [])
        # Double-resolve is a clean failure.
        assert not await provider.resolve_question(question_id, owner="alice")
    finally:
        await asyncio.create_task(provider.shutdown())


@needs_fm
async def test_live_auto_capture_cannot_mint_unknown(tmp_path):
    provider = FrankenmemoryProvider(command=FM_BIN, env={"FM_DB_PATH": str(tmp_path / "fm.db")})
    await asyncio.create_task(provider.initialize())
    try:
        # The auto lane never sends category; even a hostile caller that
        # does gets rejected by the engine (U2).
        result = await provider._call_tool("capture", {
            "user_text": "what is the favorite color of the user",
            "assistant_text": "",
            "capture_mode": "candidate",
            "category": "unknown",
            "owner": "alice",
            "workspace_id": provider._workspace_id,
            "source": "test",
        })
        assert not any(str(rid).startswith("m_") for rid in result.get("record_ids") or [])
        digest = await provider.digest(owner="alice")
        assert not (digest.get("open_questions") or [])
    finally:
        await asyncio.create_task(provider.shutdown())


@needs_fm
async def test_live_passive_resolution_listens_first(tmp_path):
    provider = FrankenmemoryProvider(command=FM_BIN, env={"FM_DB_PATH": str(tmp_path / "fm.db")})
    await asyncio.create_task(provider.initialize())
    try:
        await provider.remember("user's name", owner="alice", category="unknown")
        digest = await provider.digest(owner="alice")
        assert len(digest.get("open_questions") or []) == 1

        # Remembering the answering fact closes the question (U7c) — no
        # one ever asked.
        await provider.remember("the user's name is E", owner="alice", category="fact")
        digest_after = await provider.digest(owner="alice")
        assert not (digest_after.get("open_questions") or [])

        # The unrelated question stays open.
        await provider.remember("preferred shell", owner="alice", category="unknown")
        await provider.remember("lunch was soup today", owner="alice", category="fact")
        final = await provider.digest(owner="alice")
        assert len(final.get("open_questions") or []) == 1
        assert final["open_questions"][0]["content"] == "preferred shell?"
    finally:
        await asyncio.create_task(provider.shutdown())
