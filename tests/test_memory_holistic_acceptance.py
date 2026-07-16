"""SLICE-08 — cross-host acceptance against the real fm-mcp binary.

One bank, two windows, one index card. This test walks the whole story in
a single engine instance:

  1. Brain add (Odysseus provider.remember) → visible to a mimo-scoped
     search (the engine unions the session workspace with "global").
  2. A mimo capture-shaped turn (candidate mode, canonical workspace) →
     lands as raw rows + a pending candidate the Brain candidates tier sees.
  3. An authored MEMORY.md section (mimo reconcile projection) → recallable
     from the Odysseus side.
  4. The digest reflects all of it immediately — counts, pinned, and the
     renderer produces exactly one index card for the preface.
"""

import asyncio
import os

import pytest

from src.frankenmemory_provider import FrankenmemoryProvider
from src.memory_digest import DIGEST_SENTINEL, render_digest
from src.memory_scope import CHAT_WORKSPACE

FM_BIN = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "mcp_servers/frankenmemory/target/release/fm-mcp",
)

needs_fm = pytest.mark.skipif(not os.path.exists(FM_BIN), reason="fm-mcp release binary not built")


@needs_fm
async def test_one_bank_two_windows_and_the_index_card(tmp_path):
    provider = FrankenmemoryProvider(command=FM_BIN, env={"FM_DB_PATH": str(tmp_path / "fm.db")})
    await asyncio.create_task(provider.initialize())
    try:
        # 1. Brain add → mimo-scoped search hit.
        brain_record = await provider.remember(
            "The greenhouse ledger is kept in amber ink.",
            owner="alice",
            category="persona",
        )
        mimo_view = await provider._call_tool(
            "search",
            {
                "query": "greenhouse ledger amber",
                "tier": "curated",
                "limit": 10,
                "owner": "alice",
                "workspace_id": "some-mimo-session-workspace",
            },
        )
        mimo_ids = {row.get("record", row).get("id") for row in mimo_view.get("results", [])}
        assert brain_record.id in mimo_ids

        # 2. Direct-endpoint / mimo capture parity → pending candidate.
        capture = await provider.capture(
            "the observatory door code changed to 7741 last tuesday",
            "Noted — door code 7741 since tuesday.",
            owner="alice",
            session_id="ses_holistic",
        )
        assert capture.get("record_ids")
        candidates = await provider.inspect_tier("candidate", owner="alice")
        assert candidates, "captured turn must surface in the Brain candidates tier"

        # 3. Authored MEMORY.md section → Odysseus recall.
        await provider._call_tool(
            "ingest_authored",
            {
                "source_path": "/data/memory/global/MEMORY.md",
                "sections": [{"anchor": "Build", "content": "Build\nthe cobalt pipeline needs FM_DB_PATH"}],
                "owner": "alice",
                "workspace_id": CHAT_WORKSPACE,
            },
        )
        authored_hits = await provider.recall("cobalt pipeline", owner="alice", top_k=10)
        assert authored_hits and authored_hits[0].memory.source == "authored"

        # 4. Digest reflects everything, and renders one index card.
        digest = await provider.digest(owner="alice")
        assert digest["counts"]["by_tier"]["curated"] >= 2
        assert digest["counts"]["by_tier"]["raw"] >= 2
        assert digest["counts"]["candidates_pending"] >= 1
        assert any(
            "amber ink" in (p.get("headline") or "") for p in digest["pinned"]
        ), "persona memory must surface as a pinned headline"

        card = render_digest(digest)
        assert card.count(DIGEST_SENTINEL) == 1
        assert "candidates pending review" in card

        # Owner wall holds end to end.
        bob = await provider.digest(owner="bob")
        assert bob["counts"]["by_tier"]["curated"] == 0
    finally:
        await asyncio.create_task(provider.shutdown())
