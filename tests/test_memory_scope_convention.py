"""SLICE-01 — canonical chat-workspace convention.

Conversational memory writes must carry the canonical workspace ("global",
fm's own default scope) no matter which entry point produced them. These
tests pin every deriver to the one symbol (src.memory_scope.chat_workspace)
and prove the cross-entry-point round trip against the real fm-mcp binary:
a record written through the Odysseus provider must be visible to a reader
scoped the way a mimo session is (include_global union).
"""

import asyncio
import os

import pytest

import mcp_servers.memory_server as memory_server
from src.frankenmemory_provider import FrankenmemoryProvider
from src.memory_scope import CHAT_WORKSPACE, chat_workspace
from src.openclank.acp_bridge import frankenmemory_mcp_descriptor

FM_BIN = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "mcp_servers/frankenmemory/target/release/fm-mcp",
)

needs_fm = pytest.mark.skipif(not os.path.exists(FM_BIN), reason="fm-mcp release binary not built")


@pytest.fixture(autouse=True)
def _clean_workspace_env(monkeypatch):
    monkeypatch.delenv("FM_WORKSPACE_ID", raising=False)
    monkeypatch.delenv("ODYSSEUS_MCP_MEMORY_WORKSPACE", raising=False)


def _descriptor_env(descriptor):
    return {item["name"]: item["value"] for item in descriptor["env"]}


def test_chat_workspace_defaults_to_engine_global():
    assert chat_workspace() == CHAT_WORKSPACE == "global"


def test_chat_workspace_env_override(monkeypatch):
    monkeypatch.setenv("FM_WORKSPACE_ID", "repo-under-test")
    assert chat_workspace() == "repo-under-test"


def test_provider_default_workspace_is_canonical_not_cwd():
    provider = FrankenmemoryProvider(command="/nonexistent/fm-mcp")
    assert provider._workspace_id == CHAT_WORKSPACE
    assert provider._workspace_id != os.getcwd()


def test_provider_explicit_workspace_wins():
    provider = FrankenmemoryProvider(command="/nonexistent/fm-mcp", workspace_id="repo-x")
    assert provider._workspace_id == "repo-x"


def test_fm_descriptor_carries_canonical_workspace(monkeypatch):
    monkeypatch.delenv("FM_DB_PATH", raising=False)
    monkeypatch.delenv("FM_DB_ID", raising=False)
    descriptor = frankenmemory_mcp_descriptor(owner="alice", session_id="s1")
    assert _descriptor_env(descriptor)["FM_WORKSPACE_ID"] == CHAT_WORKSPACE


def test_fm_descriptor_explicit_workspace_honored(monkeypatch):
    monkeypatch.delenv("FM_DB_PATH", raising=False)
    monkeypatch.delenv("FM_DB_ID", raising=False)
    descriptor = frankenmemory_mcp_descriptor(workspace="repo-x", owner="alice", session_id="s1")
    assert _descriptor_env(descriptor)["FM_WORKSPACE_ID"] == "repo-x"


def test_fm_descriptor_still_requires_owner():
    with pytest.raises(ValueError):
        frankenmemory_mcp_descriptor(session_id="s1")


def test_memory_server_workspace_fallback_is_canonical():
    assert memory_server._configured_workspace() == CHAT_WORKSPACE


def test_memory_server_env_still_wins(monkeypatch):
    monkeypatch.setenv("ODYSSEUS_MCP_MEMORY_WORKSPACE", "repo-y")
    assert memory_server._configured_workspace() == "repo-y"


@needs_fm
async def test_chat_writes_carry_canonical_workspace(tmp_path):
    """D3 guardrail: a stray path-workspace on chat records is a regression."""
    provider = FrankenmemoryProvider(command=FM_BIN, env={"FM_DB_PATH": str(tmp_path / "fm.db")})
    await asyncio.create_task(provider.initialize())
    try:
        await provider.remember("The greenhouse thermostat lives on channel 4.", owner="alice")
        rows = await provider.inspect_tier("curated", owner="alice")
        assert rows
        stray = [row for row in rows if row.get("workspace_id") != CHAT_WORKSPACE]
        assert stray == []
    finally:
        await asyncio.create_task(provider.shutdown())


@needs_fm
async def test_cross_entry_point_round_trip(tmp_path):
    """Write through the Odysseus provider (canonical workspace); read the way
    a mimo session does — its own session workspace, include_global union on
    the engine side — and the record must surface."""
    provider = FrankenmemoryProvider(command=FM_BIN, env={"FM_DB_PATH": str(tmp_path / "fm.db")})
    await asyncio.create_task(provider.initialize())
    try:
        record = await provider.remember("Odysseus stores the amber ledger.", owner="alice")

        mimo_scoped = await provider._call_tool(
            "search",
            {
                "query": "amber ledger",
                "tier": "curated",
                "limit": 10,
                "owner": "alice",
                "workspace_id": "some-mimo-session-workspace",
            },
        )
        hits = {row.get("record", row).get("id") for row in mimo_scoped.get("results", [])}
        assert record.id in hits
    finally:
        await asyncio.create_task(provider.shutdown())
