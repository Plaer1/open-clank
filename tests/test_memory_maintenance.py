import asyncio

from src.frankenmemory_provider import FrankenmemoryProvider
from src.memory_maintenance import GROOM_OPS, groom_interval_hours, groom_once


def test_groom_interval_defaults_and_zero_disable(monkeypatch):
    monkeypatch.delenv("FM_GROOM_INTERVAL_HOURS", raising=False)
    assert groom_interval_hours() == 0.0
    assert groom_interval_hours("0") == 0.0
    assert groom_interval_hours("-2") == 0.0
    assert groom_interval_hours("not-a-number") == 0.0


def test_groom_once_runs_all_ops_and_continues_after_failure():
    class Provider:
        def __init__(self):
            self.calls = []

        async def groom(self, op, *, owner=None):
            self.calls.append((op, owner))
            if op == "dedup":
                raise RuntimeError("fixture failure")
            return {"op": op}

    provider = Provider()
    results = asyncio.run(groom_once(provider, owner="alice"))

    assert [op for op, _ in provider.calls] == list(GROOM_OPS)
    assert all(owner == "alice" for _, owner in provider.calls)
    assert results[1]["ok"] is False
    assert results[2]["ok"] is True


def test_frankenmemory_groom_uses_mcp_tool():
    provider = FrankenmemoryProvider(command="fm-mcp")
    calls = []

    async def fake_call(name, args):
        calls.append((name, args))
        return {"op": args["op"]}

    provider._call_tool = fake_call
    result = asyncio.run(provider.groom("edge_decay", owner="alice", workspace_id="ws"))

    assert result == {"op": "edge_decay"}
    assert calls == [("groom", {
        "op": "edge_decay",
        "dry_run": False,
        "owner": "alice",
        "workspace_id": "ws",
    })]
