import asyncio

import pytest
from fastapi import HTTPException, Request
from mcp.types import CallToolResult

from core.middleware import (
    INTERNAL_TOOL_HEADER,
    INTERNAL_TOOL_OWNER_HEADER,
    INTERNAL_TOOL_TOKEN,
    INTERNAL_TOOL_WORKSPACE_HEADER,
)
from services.memory.forget_coordinator import require_memory_lifecycle_convergence
from src.frankenmemory_provider import FrankenmemoryProvider
from src.memory_provider import MemoryRequestRejectedError
from src.openclank.acp_bridge import frankenmemory_broker_token


@pytest.fixture(autouse=True)
def _valid_app_debug_env(monkeypatch):
    """Broker tests must not inherit a non-boolean developer DEBUG label."""
    monkeypatch.setenv("DEBUG", "false")


class _Response:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class _BrokerClient:
    def __init__(self, schema_version=10, reject_name=None, reject_status=409):
        self.calls = []
        self.closed = False
        self.schema_version = schema_version
        self.reject_name = reject_name
        self.reject_status = reject_status

    async def post(self, url, *, headers, json):
        self.calls.append((url, headers, json))
        if json["name"] == "memory_quality":
            return _Response({"schema_version": self.schema_version})
        if json["name"] == self.reject_name:
            return _Response(
                {"detail": "rejected"},
                status_code=self.reject_status,
            )
        return _Response({"ok": True, "name": json["name"]})

    async def aclose(self):
        self.closed = True


def _request(
    *,
    owner="alice",
    workspace_id="global",
    token=None,
    forwarded=False,
):
    token = token or frankenmemory_broker_token(
        INTERNAL_TOOL_TOKEN, owner, workspace_id
    )
    headers = [
        (INTERNAL_TOOL_HEADER.lower().encode("ascii"), token.encode("ascii")),
        (
            INTERNAL_TOOL_OWNER_HEADER.lower().encode("ascii"),
            owner.encode("ascii"),
        ),
        (
            INTERNAL_TOOL_WORKSPACE_HEADER.lower().encode("ascii"),
            workspace_id.encode("ascii"),
        ),
    ]
    if forwarded:
        headers.append((b"cf-connecting-ip", b"203.0.113.9"))
    request = Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": "/api/internal/frankenmemory/tool",
            "raw_path": b"/api/internal/frankenmemory/tool",
            "query_string": b"",
            "headers": headers,
            "client": ("127.0.0.1", 12345),
            "server": ("127.0.0.1", 7000),
        }
    )
    return request


def test_memory_reconciliation_errors_fail_the_readiness_boundary():
    require_memory_lifecycle_convergence({
        "rolled_back": 1,
        "committed": 0,
        "restored": 0,
        "errors": 0,
    })
    with pytest.raises(RuntimeError, match="did not converge"):
        require_memory_lifecycle_convergence({
            "rolled_back": 0,
            "committed": 0,
            "restored": 0,
            "errors": 1,
        })


@pytest.mark.parametrize(
    ("url", "token"),
    [
        ("https://127.0.0.1:7000/api/internal/frankenmemory/tool", "secret"),
        ("http://example.com/api/internal/frankenmemory/tool", "secret"),
        ("http://127.0.0.1:7000/api/internal/frankenmemory/tool", ""),
    ],
)
def test_broker_rejects_non_loopback_or_unauthenticated_configuration(url, token):
    with pytest.raises(ValueError, match="authenticated plain HTTP on loopback"):
        FrankenmemoryProvider(broker_url=url, broker_token=token)


@pytest.mark.asyncio
async def test_broker_initializes_once_and_never_spawns_fm_mcp(monkeypatch):
    import httpx

    client = _BrokerClient()
    constructed = 0

    def make_client(*, timeout):
        nonlocal constructed
        assert timeout == 45.0
        constructed += 1
        return client

    monkeypatch.setattr(httpx, "AsyncClient", make_client)
    provider = FrankenmemoryProvider(
        broker_url="http://127.0.0.1:7000/api/internal/frankenmemory/tool",
        broker_token="broker-secret",
    )

    await asyncio.gather(provider.initialize(), provider.initialize())
    result = await provider.invoke_tool("search", {"query": "blue notebook"})

    assert result == {"ok": True, "name": "search"}
    assert constructed == 1
    assert provider._owner_task is None
    assert [call[2]["name"] for call in client.calls] == ["memory_quality", "search"]
    assert client.calls[-1][1]["X-Odysseus-Internal-Token"] == "broker-secret"
    assert client.calls[-1][1]["X-Open-Clank-Owner"] == "local"
    assert client.calls[-1][1]["X-Open-Clank-Workspace"] == "global"

    await provider.shutdown()
    assert client.closed is True


@pytest.mark.asyncio
async def test_broker_rejects_pre_lifecycle_schema(monkeypatch):
    import httpx

    client = _BrokerClient(schema_version=9)
    monkeypatch.setattr(httpx, "AsyncClient", lambda **_kwargs: client)
    provider = FrankenmemoryProvider(
        broker_url="http://127.0.0.1:7000/api/internal/frankenmemory/tool",
        broker_token="broker-secret",
    )

    with pytest.raises(RuntimeError, match="older than the lifecycle contract"):
        await provider.initialize()

    assert client.closed is True


@pytest.mark.parametrize("status_code", (403, 404, 409, 422))
@pytest.mark.asyncio
async def test_broker_preserves_definite_rejection_as_non_ambiguous(
    monkeypatch,
    status_code,
):
    import httpx

    client = _BrokerClient(
        reject_name="memory_forget",
        reject_status=status_code,
    )
    monkeypatch.setattr(httpx, "AsyncClient", lambda **_kwargs: client)
    provider = FrankenmemoryProvider(
        broker_url="http://127.0.0.1:7000/api/internal/frankenmemory/tool",
        broker_token="broker-secret",
    )
    await provider.initialize()

    with pytest.raises(MemoryRequestRejectedError, match="rejected"):
        await provider.invoke_tool(
            "memory_forget",
            {
                "action": "commit",
                "owner": "alice",
                "workspace_id": "global",
            },
        )


def test_lifetools_descriptor_carries_scoped_broker_credentials(monkeypatch):
    from src.openclank import acp_bridge

    monkeypatch.setattr(acp_bridge, "_MEMORY_BROKER_URL", "")
    monkeypatch.setattr(acp_bridge, "_MEMORY_BROKER_TOKEN", "")
    acp_bridge.configure_frankenmemory_broker(
        "http://127.0.0.1:7000/api/internal/frankenmemory/tool",
        "broker-secret",
    )

    descriptor = acp_bridge.lifetools_mcp_descriptor(
        owner="alice",
        session_id="chat-1",
        workspace="/workspace",
    )
    env = {item["name"]: item["value"] for item in descriptor["env"]}
    assert env["OPEN_CLANK_MEMORY_BROKER_URL"].startswith("http://127.0.0.1:")
    assert env["OPEN_CLANK_MEMORY_BROKER_TOKEN"] == frankenmemory_broker_token(
        "broker-secret", "alice", "global"
    )
    assert env["OPEN_CLANK_MEMORY_BROKER_TOKEN"] != "broker-secret"
    assert env["FM_OWNER"] == "alice"


@pytest.mark.asyncio
async def test_lifetools_marks_internal_memory_transport_failures_as_mcp_errors(
    monkeypatch,
):
    from src.openclank import lifetools_server

    class _BrokenProvider:
        async def invoke_tool(self, _name, _arguments):
            raise RuntimeError("transport details stay private")

    monkeypatch.setattr(lifetools_server, "_OWNER", "alice")
    monkeypatch.setattr(lifetools_server, "_MEMORY_PROVIDER", _BrokenProvider())
    monkeypatch.setenv("FM_WORKSPACE_ID", "global")

    result = await lifetools_server.call_tool(
        "search",
        {"owner": "alice", "workspace_id": "global", "query": "x"},
    )
    assert isinstance(result, CallToolResult)
    assert result.is_error is True
    assert "transport details" not in result.content[0].text


@pytest.mark.asyncio
async def test_internal_broker_requires_direct_loopback_even_with_valid_token(monkeypatch):
    monkeypatch.setenv("DEBUG", "false")
    import app as app_module

    class _Provider:
        async def invoke_tool(self, name, arguments):
            return {"name": name, "arguments": arguments}

    monkeypatch.setattr(app_module, "memory_provider", _Provider())
    monkeypatch.setattr(app_module, "AUTH_ENABLED", False)
    request = _request(forwarded=True)

    with pytest.raises(HTTPException) as raised:
        await app_module.internal_frankenmemory_tool(
            request, {"name": "search", "arguments": {}}
        )
    assert raised.value.status_code == 403


@pytest.mark.asyncio
async def test_internal_broker_works_in_auth_disabled_mode_and_overwrites_scope(
    monkeypatch,
):
    monkeypatch.setenv("DEBUG", "false")
    import app as app_module

    class _Provider:
        async def invoke_tool(self, name, arguments):
            return {"name": name, "arguments": arguments}

    monkeypatch.setattr(app_module, "AUTH_ENABLED", False)
    monkeypatch.setattr(app_module, "memory_provider", _Provider())
    request = _request()

    assert await app_module.internal_frankenmemory_tool(
        request,
        {
            "name": "search",
            "arguments": {
                "owner": "mallory",
                "workspace_id": "other",
                "query": "safe",
            },
        },
    ) == {
        "name": "search",
        "arguments": {
            "owner": "alice",
            "workspace_id": "global",
            "query": "safe",
        },
    }


@pytest.mark.asyncio
async def test_internal_broker_keeps_memory_quality_read_only(monkeypatch):
    monkeypatch.setenv("DEBUG", "false")
    import app as app_module

    calls = []

    class _Provider:
        async def invoke_tool(self, name, arguments):
            calls.append((name, arguments))
            return {
                "schema_version": 10,
                "database_id": "db-test",
                "global_detail": "must stay private",
            }

    monkeypatch.setattr(app_module, "AUTH_ENABLED", False)
    monkeypatch.setattr(app_module, "memory_provider", _Provider())
    request = _request()

    with pytest.raises(HTTPException) as raised:
        await app_module.internal_frankenmemory_tool(
            request,
            {
                "name": "memory_quality",
                "arguments": {"rebuild_graph_fts": True},
            },
        )
    assert raised.value.status_code == 400
    assert calls == []

    assert await app_module.internal_frankenmemory_tool(
        request,
        {
            "name": "memory_quality",
            "arguments": {
                "rebuild_graph_fts": False,
                "owner": "mallory",
                "future_global_control": True,
            },
        },
    ) == {"schema_version": 10, "database_id": "db-test"}
    assert calls == [
        ("memory_quality", {"rebuild_graph_fts": False}),
    ]


@pytest.mark.asyncio
async def test_internal_broker_rejects_cross_owner_token_reuse(monkeypatch):
    monkeypatch.setenv("DEBUG", "false")
    import app as app_module

    monkeypatch.setattr(app_module, "AUTH_ENABLED", False)
    alice_token = frankenmemory_broker_token(
        INTERNAL_TOOL_TOKEN, "alice", "global"
    )
    request = _request(owner="bob", token=alice_token)

    with pytest.raises(HTTPException) as raised:
        await app_module.internal_frankenmemory_tool(
            request, {"name": "search", "arguments": {}}
        )
    assert raised.value.status_code == 403


@pytest.mark.asyncio
async def test_internal_broker_requires_a_known_owner_when_auth_is_enabled(
    monkeypatch,
):
    monkeypatch.setenv("DEBUG", "false")
    import app as app_module

    monkeypatch.setattr(app_module, "AUTH_ENABLED", True)
    monkeypatch.setattr(
        app_module,
        "auth_manager",
        type("_Auth", (), {"users": {"alice": {}}})(),
    )

    unknown = _request(owner="mallory")
    with pytest.raises(HTTPException) as raised:
        await app_module.internal_frankenmemory_tool(
            unknown, {"name": "search", "arguments": {}}
        )
    assert raised.value.status_code == 403


@pytest.mark.asyncio
async def test_internal_broker_is_blocked_by_account_lifecycle_fence(monkeypatch):
    monkeypatch.setenv("DEBUG", "false")
    import app as app_module
    from src.openclank.account_request_barrier import AccountRequestBarrier

    class _Provider:
        async def invoke_tool(self, _name, _arguments):
            raise AssertionError("fenced broker request reached memory provider")

    monkeypatch.setattr(app_module, "AUTH_ENABLED", False)
    monkeypatch.setattr(app_module, "memory_provider", _Provider())
    monkeypatch.setattr(
        app_module,
        "_account_request_barrier",
        AccountRequestBarrier(lambda owner: owner == "alice"),
    )

    with pytest.raises(HTTPException) as raised:
        await app_module.internal_frankenmemory_tool(
            _request(owner="alice"),
            {"name": "search", "arguments": {"query": "blocked"}},
        )
    assert raised.value.status_code == 409


@pytest.mark.asyncio
async def test_internal_broker_rejects_tools_outside_scoped_contract(monkeypatch):
    monkeypatch.setenv("DEBUG", "false")
    import app as app_module

    monkeypatch.setattr(app_module, "AUTH_ENABLED", False)
    request = _request()

    with pytest.raises(HTTPException) as raised:
        await app_module.internal_frankenmemory_tool(
            request, {"name": "groom", "arguments": {}}
        )
    assert raised.value.status_code == 400
