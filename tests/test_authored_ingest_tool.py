"""SLICE-06 — authored-file ingest tool against the real fm-mcp binary.

The mimo reconcile pass projects agent-authored MEMORY.md sections through
the `ingest_authored` tool; these tests pin the tool contract Brain relies
on: ingested sections are recallable (one bank), re-ingest is a no-op, and
an empty section list wipes the file's projection.
"""

import asyncio
import os

import pytest

from src.frankenmemory_provider import FrankenmemoryProvider
from src.memory_scope import CHAT_WORKSPACE

FM_BIN = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "mcp_servers/frankenmemory/target/release/fm-mcp",
)

needs_fm = pytest.mark.skipif(not os.path.exists(FM_BIN), reason="fm-mcp release binary not built")

PATH = "/data/memory/global/MEMORY.md"


def _sections(*contents):
    return [{"anchor": "", "content": text} for text in contents]


@needs_fm
async def test_ingest_authored_round_trip_and_wipe(tmp_path):
    provider = FrankenmemoryProvider(command=FM_BIN, env={"FM_DB_PATH": str(tmp_path / "fm.db")})
    await asyncio.create_task(provider.initialize())
    try:
        first = await provider._call_tool(
            "ingest_authored",
            {
                "source_path": PATH,
                "sections": _sections("e prefers the cobalt syntax theme"),
                "owner": "alice",
                "workspace_id": CHAT_WORKSPACE,
            },
        )
        assert first.get("upserted") == 1

        hits = await provider.recall("cobalt syntax theme", owner="alice", top_k=5)
        assert hits, "authored section must be recallable from the Brain side"
        assert hits[0].memory.source == "authored"
        assert hits[0].memory.workspace_id == CHAT_WORKSPACE

        again = await provider._call_tool(
            "ingest_authored",
            {
                "source_path": PATH,
                "sections": _sections("e prefers the cobalt syntax theme"),
                "owner": "alice",
                "workspace_id": CHAT_WORKSPACE,
            },
        )
        assert again.get("upserted") == 0
        assert again.get("unchanged") == 1

        wipe = await provider._call_tool(
            "ingest_authored",
            {"source_path": PATH, "sections": [], "owner": "alice", "workspace_id": CHAT_WORKSPACE},
        )
        assert wipe.get("deleted") == 1
        assert await provider.recall("cobalt syntax theme", owner="alice", top_k=5) == []
    finally:
        await asyncio.create_task(provider.shutdown())
