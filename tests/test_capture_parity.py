"""SLICE-02 — capture parity for direct-endpoint chats.

Under frankenmemory the old gate disabled ALL Odysseus background memory
extraction (it required provider_id == "native"), so chats on direct
endpoints produced zero memories. Now run_post_response_tasks routes those
turns into the provider's capture pipeline — the same candidates-tier flow
mimo turns take — while mimo-transport turns stay excluded (the child
captures them itself) and the native path keeps its legacy every-4th-turn
LLM extraction untouched.
"""

import asyncio
import importlib
import os
import sys
import types
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

FM_BIN = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "mcp_servers/frankenmemory/target/release/fm-mcp",
)

needs_fm = pytest.mark.skipif(not os.path.exists(FM_BIN), reason="fm-mcp release binary not built")


def _chat_helpers(monkeypatch):
    for mod_name in [
        "starlette.middleware",
        "starlette.middleware.base",
        "core.models",
        "core.database",
        "routes.prefs_routes",
        "routes.research_routes",
        "src.llm_core",
        "src.context_compactor",
        "src.model_context",
        "src.auth_helpers",
    ]:
        if mod_name not in sys.modules:
            monkeypatch.setitem(sys.modules, mod_name, MagicMock())
    return importlib.import_module("routes.chat_helpers")


class _CaptureProvider:
    provider_id = "frankenmemory"

    def __init__(self):
        self.calls = []

    def capture(self, user_text, assistant_text, *, owner=None, session_id=None):
        async def _run():
            self.calls.append(
                {
                    "user_text": user_text,
                    "assistant_text": assistant_text,
                    "owner": owner,
                    "session_id": session_id,
                }
            )
            return {"record_ids": ["r1"]}

        return _run()


def _stub_legacy_extractor(monkeypatch, calls):
    mem_extractor_mod = types.ModuleType("services.memory.memory_extractor")

    async def fake_extract_and_store(*a, **k):
        calls["legacy"] += 1

    mem_extractor_mod.extract_and_store = fake_extract_and_store
    monkeypatch.setitem(sys.modules, "services.memory.memory_extractor", mem_extractor_mod)

    task_endpoint_mod = types.ModuleType("src.task_endpoint")
    task_endpoint_mod.resolve_task_endpoint = lambda url, model, headers, owner=None: (url, model, headers)
    monkeypatch.setitem(sys.modules, "src.task_endpoint", task_endpoint_mod)


def _sess():
    return SimpleNamespace(
        endpoint_url="https://api.deepseek.com/v1",
        model="deepseek-chat",
        headers={},
        history=[object()] * 8,
        name="My session title",
    )


def _run(chat_helpers, provider, *, incognito=False, compare_mode=False,
         captured_by_runtime=False, auto_memory=True, allow=True):
    chat_helpers.run_post_response_tasks(
        _sess(), SimpleNamespace(save_sessions=lambda: None), "sess-cap",
        "what is the observatory code?", "It is 7741.", None,
        {"auto_memory": auto_memory, "auto_skills": False},
        memory_manager=MagicMock(), memory_vector=MagicMock(),
        webhook_manager=None,
        incognito=incognito, compare_mode=compare_mode,
        owner="alice",
        allow_background_extraction=allow,
        memory_provider=provider,
        captured_by_runtime=captured_by_runtime,
    )


@pytest.fixture
def harness(monkeypatch):
    chat_helpers = _chat_helpers(monkeypatch)
    calls = {"legacy": 0}
    _stub_legacy_extractor(monkeypatch, calls)
    monkeypatch.setattr(chat_helpers, "needs_auto_name", lambda name: False)

    async def run_jobs_inline(session_id, jobs, max_wait_s=120.0):
        for _, job in jobs:
            await job

    monkeypatch.setattr(chat_helpers, "_run_extraction_jobs_sequentially", run_jobs_inline)
    return chat_helpers, calls


@pytest.mark.asyncio
async def test_direct_endpoint_turn_captures_via_provider(harness):
    chat_helpers, calls = harness
    provider = _CaptureProvider()
    _run(chat_helpers, provider)
    await asyncio.sleep(0.05)

    assert len(provider.calls) == 1
    call = provider.calls[0]
    assert call["user_text"] == "what is the observatory code?"
    assert call["assistant_text"] == "It is 7741."
    assert call["owner"] == "alice"
    assert call["session_id"] == "sess-cap"
    assert calls["legacy"] == 0


@pytest.mark.asyncio
async def test_mimo_transport_turn_is_captured_child_side_only(harness):
    chat_helpers, calls = harness
    provider = _CaptureProvider()
    _run(chat_helpers, provider, captured_by_runtime=True)
    await asyncio.sleep(0.05)

    assert provider.calls == []
    assert calls["legacy"] == 0


@pytest.mark.asyncio
async def test_incognito_and_compare_never_capture(harness):
    chat_helpers, calls = harness
    provider = _CaptureProvider()
    _run(chat_helpers, provider, incognito=True)
    _run(chat_helpers, provider, compare_mode=True)
    await asyncio.sleep(0.05)

    assert provider.calls == []
    assert calls["legacy"] == 0


@pytest.mark.asyncio
async def test_auto_memory_pref_and_kill_switch_respected(harness):
    chat_helpers, calls = harness
    provider = _CaptureProvider()
    _run(chat_helpers, provider, auto_memory=False)
    _run(chat_helpers, provider, allow=False)
    await asyncio.sleep(0.05)

    assert provider.calls == []


@pytest.mark.asyncio
async def test_native_mode_keeps_legacy_extraction(harness):
    chat_helpers, calls = harness
    _run(chat_helpers, None)
    await asyncio.sleep(0.05)

    assert calls["legacy"] == 1


@pytest.mark.asyncio
async def test_native_provider_object_also_takes_legacy_path(harness):
    chat_helpers, calls = harness
    native = SimpleNamespace(provider_id="native")
    _run(chat_helpers, native)
    await asyncio.sleep(0.05)

    assert calls["legacy"] == 1


@needs_fm
@pytest.mark.asyncio
async def test_provider_capture_lands_candidate_in_canonical_workspace(tmp_path):
    """SLICE-02 acceptance shape against the real engine: one direct-endpoint
    turn → raw rows plus an auto-admitted candidate, all in "global"."""
    from src.frankenmemory_provider import FrankenmemoryProvider
    from src.memory_scope import CHAT_WORKSPACE

    provider = FrankenmemoryProvider(command=FM_BIN, env={"FM_DB_PATH": str(tmp_path / "fm.db")})
    await asyncio.create_task(provider.initialize())
    try:
        result = await provider.capture(
            "i prefer green tea over coffee in the mornings",
            "Noted — green tea first thing.",
            owner="alice",
            session_id="ses_direct",
        )
        assert result.get("record_ids"), f"capture bounced: {result}"

        raw_rows = await provider.inspect_tier("raw", owner="alice")
        assert raw_rows and all(r.get("workspace_id") == CHAT_WORKSPACE for r in raw_rows)

        candidates = await provider.inspect_tier("candidate", owner="alice")
        assert candidates, "auto_preference_claim should admit a candidate"
    finally:
        await asyncio.create_task(provider.shutdown())
