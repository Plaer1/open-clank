"""Resume of a claimed terminal-S08 reset after an app restart: the per-process witness is re-obtained
via the same refresh the preview uses; a provider that is still not ready keeps the original refusal."""
import asyncio
import hashlib
import json
from types import SimpleNamespace

import pytest

from services.memory.nuke_coordinator import MemoryNukeCoordinator, MemoryNukeUnavailable
from src.openclank.account_request_barrier import AccountRequestBarrier

REFUSAL = "reset S08 live capability changed"


class _Provider:
    _guarded_v17 = True

    def __init__(self, ready_after_refresh):
        self.ready_after_refresh = ready_after_refresh
        self._terminal_s08_witness = None
        self.refreshes = 0

    def _terminal_s08_ready_local(self):
        return self._terminal_s08_witness is not None


def _run(tmp_path, ready_after_refresh):
    provider = _Provider(ready_after_refresh)
    coordinator = MemoryNukeCoordinator(provider, data_dir=tmp_path)
    token, confirmation = "token", "confirm"
    row = {"operation_id": "nuke_1", "owner": "alice", "provider_owner": "alice", "components": ["memories"],
           "expected": {}, "queued_expected": {"count": 0}, "queued_account_id": "acct", "state": "partial",
           "result": None, "reset_s08_mode": "terminal-s08-v1", "reset_native_started": True,
           "preview_token_sha256": hashlib.sha256(token.encode()).hexdigest(), "confirmation": confirmation,
           "account_id": "acct", "guarded_reset": True}
    coordinator._save_journal({"nuke_1": row})

    async def refresh(*, state, owner, account_id, provider, terminal_s08):
        assert terminal_s08 is True and account_id == "acct"
        provider.refreshes += 1
        if provider.ready_after_refresh:
            provider._terminal_s08_witness = (object(), object())
        return provider.ready_after_refresh

    state = SimpleNamespace(refresh_reset_capability_for_owner=refresh)
    auth = SimpleNamespace(account_id=lambda owner: "acct", username_for_account_id=lambda account: "alice")
    barrier = AccountRequestBarrier(lambda owner: False)

    async def go():
        async with barrier.admitted("alice"):
            await coordinator.commit(owner="alice", provider_owner="alice", operation_id="nuke_1",
                preview_token=token, confirmation=confirmation, reset_barrier=barrier,
                reset_state=state, reset_auth=auth)
    try:
        asyncio.run(go())
        error = None
    except Exception as exc:  # anything past the check is out of scope for this test
        error = exc
    return provider, error


def test_fresh_process_reobtains_the_witness_and_passes_the_check(tmp_path):
    provider, error = _run(tmp_path, ready_after_refresh=True)
    assert provider.refreshes == 1
    assert REFUSAL not in str(error)


def test_provider_not_ready_after_refresh_keeps_the_refusal(tmp_path):
    provider, error = _run(tmp_path, ready_after_refresh=False)
    assert provider.refreshes == 1
    assert isinstance(error, MemoryNukeUnavailable) and REFUSAL in str(error)
