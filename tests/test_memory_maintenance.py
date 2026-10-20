import asyncio

from src.frankenmemory_provider import FrankenmemoryProvider
from src.memory_maintenance import (
    GROOM_OPS,
    groom_all_owners,
    groom_interval_hours,
    groom_loop,
    groom_once,
)


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

        async def groom(self, op, *, owner, workspace_id):
            self.calls.append((op, owner, workspace_id))
            if op == "dedup":
                raise RuntimeError("fixture failure")
            return {"op": op}

    provider = Provider()
    results = asyncio.run(
        groom_once(provider, owner="alice", workspace_id="global")
    )

    assert [op for op, _, _ in provider.calls] == list(GROOM_OPS)
    assert all(
        owner == "alice" and workspace == "global"
        for _, owner, workspace in provider.calls
    )
    assert results[1]["ok"] is False
    assert results[2]["ok"] is True


def test_groom_all_owners_never_falls_back_to_ownerless_scope():
    class Provider:
        def __init__(self):
            self.calls = []

        async def groom(self, op, *, owner, workspace_id):
            self.calls.append((op, owner, workspace_id))
            return {"op": op}

    provider = Provider()
    results = asyncio.run(
        groom_all_owners(
            provider,
            ["bob", "", None, "alice", "bob"],
            workspace_id="global",
        )
    )

    assert len(results) == len(GROOM_OPS) * 2
    assert {owner for _, owner, _ in provider.calls} == {"alice", "bob"}
    assert all(workspace == "global" for _, _, workspace in provider.calls)


def test_groom_loop_refreshes_owners_and_keeps_workspace_scope(monkeypatch):
    class Provider:
        def __init__(self):
            self.calls = []

        async def groom(self, op, *, owner, workspace_id):
            self.calls.append((op, owner, workspace_id))
            return {"op": op}

    sleeps = 0

    async def stop_after_one_pass(_seconds):
        nonlocal sleeps
        sleeps += 1
        if sleeps > 1:
            raise asyncio.CancelledError

    owner_reads = 0

    def owners():
        nonlocal owner_reads
        owner_reads += 1
        return ["alice"]

    monkeypatch.setattr("src.memory_maintenance.asyncio.sleep", stop_after_one_pass)
    provider = Provider()
    try:
        asyncio.run(
            groom_loop(
                provider,
                1,
                owners=owners,
                workspace_id="global",
            )
        )
    except asyncio.CancelledError:
        pass

    assert owner_reads == 1
    assert len(provider.calls) == len(GROOM_OPS)
    assert all(
        owner == "alice" and workspace == "global"
        for _, owner, workspace in provider.calls
    )


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
