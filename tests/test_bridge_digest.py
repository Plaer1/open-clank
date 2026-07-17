"""The memory index card + trusted guidance split at the MiMo bridge seam.

Odysseus-prefaced turns already carry the digest block; the bridge covers
every other path into mimo — resume flows, future mimo-first paths — by
prepending one untrusted card when none is present. The "[Memory Index]"
sentinel is the dedup contract between the two hosts.

Trust split (SLICE-02): _maybe_inject_digest returns (messages, trusted
block); the caller rides the block on envelope.system_prompt (true system
tier) while the card stays behind the untrusted wrapper. The
"[Endorsed Memory Guidance]" sentinel marks the block so prompt-part
building can skip the demoted in-message copy.
"""

from unittest.mock import MagicMock

import pytest

import routes.prefs_routes as prefs_routes
import src.openclank.acp_bridge as acp_bridge
from src.memory_digest import DIGEST_SENTINEL, TRUST_SENTINEL
from src.openclank.acp_bridge import ACPBridge, _split_memory_digest

SAMPLE_DIGEST = {
    "counts": {
        "by_kind": {"persona": 1},
        "by_tier": {"raw": 2, "curated": 1, "candidates_pending": 0},
        "candidates_pending": 0,
    },
    "pinned": [{
        "id": "mem_p1",
        "headline": "Keeper of the amber ledger",
        "content": "Keeper of the amber ledger",
        "kind": "persona",
        "source_type": "auto_extracted",
        "pinned": False,
    }],
    "clusters": [],
    "recent": [],
    "generated_at": "2026-07-15T00:00:00Z",
}

TRUSTED_DIGEST = {
    "counts": {
        "by_kind": {"instruction": 1},
        "by_tier": {"raw": 0, "curated": 1, "candidates_pending": 0},
        "candidates_pending": 0,
    },
    "pinned": [{
        "id": "mem_t1",
        "headline": "always answer in metric units",
        "content": "always answer in metric units",
        "kind": "instruction",
        "source_type": "human",
        "pinned": True,
    }],
    "clusters": [],
    "recent": [],
    "generated_at": "2026-07-15T00:00:00Z",
}


@pytest.fixture(autouse=True)
def _hermetic_prefs(monkeypatch):
    """Bridge trust prefs must come from the test, never the dev machine."""
    monkeypatch.setattr(prefs_routes, "_load_for_user", lambda user=None: {})


class _Provider:
    def __init__(self, digest=SAMPLE_DIGEST, fail=False):
        self._digest = digest
        self._fail = fail
        self.calls = 0

    async def digest(self, *, owner=None):
        self.calls += 1
        if self._fail:
            raise ConnectionError("fm down")
        return self._digest


def _bridge(provider):
    return ACPBridge(MagicMock(), cwd="/tmp", owner="alice", memory_provider=provider)


def _messages():
    return [
        {"role": "system", "content": "You are Odysseus."},
        {"role": "user", "content": "what about the greenhouse?"},
    ]


@pytest.mark.asyncio
async def test_injects_one_block_before_current_user_message():
    provider = _Provider()
    bridge = _bridge(provider)
    out, trusted = await bridge._maybe_inject_digest(_messages(), owner="alice", incognito=False)

    assert provider.calls == 1
    assert len(out) == 3
    injected = out[1]
    assert DIGEST_SENTINEL in str(injected.get("content"))
    assert out[2]["role"] == "user"
    # auto-captured persona headline, toggles off → nothing trusted (F7)
    assert trusted == ""


@pytest.mark.asyncio
async def test_sentinel_present_means_untouched():
    provider = _Provider()
    bridge = _bridge(provider)
    messages = [
        {"role": "system", "content": f"{DIGEST_SENTINEL}\nMemory bank: 1 curated."},
        {"role": "user", "content": "hello"},
    ]
    out, trusted = await bridge._maybe_inject_digest(messages, owner="alice", incognito=False)
    assert out is messages
    assert trusted == ""
    assert provider.calls == 0


@pytest.mark.asyncio
async def test_prefaced_trusted_block_is_reused_not_refetched():
    """Odysseus preface already produced the split: the bridge reuses the
    exact block text for the envelope, zero digest calls, no drift."""
    provider = _Provider()
    bridge = _bridge(provider)
    block_text = f"{TRUST_SENTINEL}\n- always answer in metric units"
    messages = [
        {"role": "system", "content": "You are Odysseus."},
        {"role": "system", "content": block_text},
        {"role": "system", "content": f"{DIGEST_SENTINEL}\nMemory bank: 1 curated."},
        {"role": "user", "content": "hello"},
    ]
    out, trusted = await bridge._maybe_inject_digest(messages, owner="alice", incognito=False)
    assert out is messages
    assert trusted == block_text
    assert provider.calls == 0


@pytest.mark.asyncio
async def test_trusted_entry_rides_block_not_card():
    bridge = _bridge(_Provider(digest=TRUSTED_DIGEST))
    out, trusted = await bridge._maybe_inject_digest(_messages(), owner="alice", incognito=False)
    assert trusted.startswith(TRUST_SENTINEL)
    assert "always answer in metric units" in trusted
    card_texts = [str(m.get("content")) for m in out]
    card = next((t for t in card_texts if DIGEST_SENTINEL in t), "")
    assert card, "untrusted card still injected (counts remain)"
    assert "always answer in metric units" not in card, (
        "a trusted memory must never ALSO appear behind the firewall"
    )


@pytest.mark.asyncio
async def test_fail_open_on_digest_error():
    bridge = _bridge(_Provider(fail=True))
    messages = _messages()
    out, trusted = await bridge._maybe_inject_digest(messages, owner="alice", incognito=False)
    assert out is messages
    assert trusted == ""


@pytest.mark.asyncio
async def test_incognito_and_providerless_turns_untouched():
    provider = _Provider()
    bridge = _bridge(provider)
    messages = _messages()
    out, trusted = await bridge._maybe_inject_digest(messages, owner="alice", incognito=True)
    assert out is messages and trusted == ""
    assert provider.calls == 0

    bare = _bridge(None)
    out, trusted = await bare._maybe_inject_digest(messages, owner="alice", incognito=False)
    assert out is messages and trusted == ""


@pytest.mark.asyncio
async def test_empty_bank_injects_nothing():
    empty = {
        "counts": {"by_kind": {}, "by_tier": {"raw": 0, "curated": 0}, "candidates_pending": 0},
        "pinned": [],
        "clusters": [],
        "recent": [],
        "generated_at": "now",
    }
    bridge = _bridge(_Provider(digest=empty))
    messages = _messages()
    out, trusted = await bridge._maybe_inject_digest(messages, owner="alice", incognito=False)
    assert out is messages
    assert trusted == ""


def test_split_memory_digest_renders_bare_card():
    trusted, card = _split_memory_digest(SAMPLE_DIGEST, "alice")
    assert card.startswith(DIGEST_SENTINEL)
    # Trust framing lives in the untrusted_context_message wrapper applied
    # at injection; the card itself must not duplicate it.
    assert "untrusted" not in card.lower()
    assert trusted == ""
    assert _split_memory_digest(None, "alice") == ("", "")


def test_gt_recall_formatter_is_gone():
    assert not hasattr(acp_bridge, "_format_gt_recall")
