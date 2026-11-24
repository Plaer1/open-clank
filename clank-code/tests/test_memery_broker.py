import asyncio
import os
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException, Request

from core.middleware import (
    INTERNAL_TOOL_HEADER,
    INTERNAL_TOOL_OWNER_HEADER,
    INTERNAL_TOOL_TOKEN,
    INTERNAL_TOOL_WORKSPACE_HEADER,
)
from services.memory.forget_coordinator import require_memory_lifecycle_convergence
from src.memery_provider import MemeryProvider
from src.memory_provider import MemoryRequestRejectedError
from src.openclank.acp_bridge import memery_broker_token


@pytest.fixture(autouse=True)
def _isolated_host_store(monkeypatch):
    """Keep app imports and database preparation inside the disposable test store."""
    monkeypatch.setenv("DEBUG", "false")
    monkeypatch.setenv("OPENCLANK_MEMORY_STORE_PROFILE", "legacy-v16")
    monkeypatch.setenv("FM_DB_ID", "")
    monkeypatch.setenv("OPEN_CLANK_INPROCESS_POLLERS", "0")
    data = Path(os.environ["OPEN_CLANK_DATA_DIR"]).resolve()
    assert data.is_relative_to(Path("/tmp").resolve()) and data.name.startswith("openclank-s03-")
    for name in ("OPEN_CLANK_DATA_DIR", "OPEN_CLANK_AUTHORITY_DB_PATH", "OPEN_CLANK_AUTHORITY_AUTH_PATH", "OPENCLANK_CONVERSATION_ARCHIVE_DB", "FM_DB_PATH"):
        assert Path(os.environ[name]).resolve().is_relative_to(data), name
    assert os.environ["DATABASE_URL"] == "sqlite:///:memory:"
    assert Path(os.environ["OPEN_CLANK_MEMERY_DB_PATH"]).resolve().is_relative_to(data)
    from src import app_initializer, memery_database
    prepare_native = memery_database.prepare_memery_database
    def prepare(path=None):
        if path is not None:
            assert Path(path).resolve().is_relative_to(data)
        return "s03-broker-synthetic-database"
    monkeypatch.setattr(app_initializer, "prepare_memery_database", prepare)
    monkeypatch.setattr(memery_database, "prepare_memery_database", prepare)
    return prepare_native


class _Response:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class _BrokerClient:
    def __init__(self, ready=True, reject_name=None, reject_status=409):
        self.calls = []
        self.closed = False
        self.ready = ready
        self.reject_name = reject_name
        self.reject_status = reject_status

    async def post(self, url, *, headers, json):
        self.calls.append((url, headers, json))
        if json["name"] == "memory_quality":
            return _Response({"ready": self.ready, "profile": "legacy"})
        if json["name"] == self.reject_name:
            return _Response(
                {"detail": "rejected"},
                status_code=self.reject_status,
            )
        return _Response({"ok": True, "name": json["name"]})

    async def aclose(self):
        self.closed = True


@pytest.fixture
def broker_app(monkeypatch, _isolated_host_store):
    import app

    auth = SimpleNamespace(users={"alice": {"account_id": "s03-broker-alice"}},
                           is_account_lifecycle_fenced=lambda _owner: False)
    monkeypatch.setattr(app, "auth_manager", auth)
    return app


def _broker_endpoint(app_module):
    return next(route.endpoint for route in app_module.app.routes
                if getattr(route, "path", None) == "/api/internal/memery/tool")


def _request(
    *,
    owner="alice",
    workspace_id="global",
    token=None,
    forwarded=False,
):
    token = token or memery_broker_token(
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
            "path": "/api/internal/memery/tool",
            "raw_path": b"/api/internal/memery/tool",
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
        ("https://127.0.0.1:7000/api/internal/memery/tool", "secret"),
        ("http://example.com/api/internal/memery/tool", "secret"),
        ("http://127.0.0.1:7000/api/internal/memery/tool", ""),
    ],
)
def test_broker_rejects_non_loopback_or_unauthenticated_configuration(url, token):
    with pytest.raises(ValueError, match="authenticated plain HTTP on loopback"):
        MemeryProvider(broker_url=url, broker_token=token)


@pytest.mark.asyncio
async def test_broker_initializes_once_without_launching_a_worker(monkeypatch):
    import httpx

    client = _BrokerClient()
    constructed = 0

    def make_client(*, timeout):
        nonlocal constructed
        assert timeout == 45.0
        constructed += 1
        return client

    monkeypatch.setattr(httpx, "AsyncClient", make_client)
    provider = MemeryProvider(
        broker_url="http://127.0.0.1:7000/api/internal/memery/tool",
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
async def test_broker_rejects_an_unready_host(monkeypatch):
    import httpx

    client = _BrokerClient(ready=False)
    monkeypatch.setattr(httpx, "AsyncClient", lambda **_kwargs: client)
    provider = MemeryProvider(
        broker_url="http://127.0.0.1:7000/api/internal/memery/tool",
        broker_token="broker-secret",
    )

    with pytest.raises(RuntimeError, match="not ready"):
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
    provider = MemeryProvider(
        broker_url="http://127.0.0.1:7000/api/internal/memery/tool",
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


@pytest.mark.asyncio
@pytest.mark.parametrize("code", ["code_node_forget_requires_workspace_erase", None, "unknown_refusal"])
async def test_code_node_refusal_survives_scoped_app_broker_without_private_diagnostics(broker_app, monkeypatch, code):
    data = Path(os.environ["OPEN_CLANK_DATA_DIR"]).resolve()
    assert data.is_relative_to(Path("/tmp").resolve()) and data.name.startswith("openclank-s03-")
    assert Path(os.environ["OPEN_CLANK_AUTHORITY_DB_PATH"]).resolve().is_relative_to(data)
    assert Path(os.environ["OPENCLANK_CONVERSATION_ARCHIVE_DB"]).resolve().is_relative_to(data)
    from src.memory_provider import code_node_forget_refusal_detail
    calls = []
    class Provider:
        async def invoke_tool(self, name, arguments):
            calls.append((name, arguments))
            raise MemoryRequestRejectedError("/private/index/root debug", code=code)

    monkeypatch.setattr(broker_app, "memory_provider", Provider())

    class Client:
        async def post(self, url, *, headers, json):
            try:
                result = await _broker_endpoint(broker_app)(_request(), json)
                return _Response(result)
            except HTTPException as exc:
                return _Response({"detail": exc.detail}, exc.status_code)

    provider = MemeryProvider(broker_url="http://127.0.0.1:7000/api/internal/memery/tool", broker_token="synthetic")
    provider._broker_client = Client()
    provider._broker_ready = True
    with pytest.raises(MemoryRequestRejectedError) as caught:
        await provider.invoke_tool("memory_forget", {"action": "preview", "owner": "mallory", "workspace_id": "other", "selector_kind": "record_id", "selector": "code-1"})
    assert calls == [("memory_forget", {"action": "preview", "owner": "alice", "workspace_id": "global", "selector_kind": "record_id", "selector": "code-1"})]
    assert "/private/index/root" not in str(caught.value)
    assert caught.value.code == (code if code == "code_node_forget_requires_workspace_erase" else None)
    if caught.value.code:
        assert str(caught.value) == code_node_forget_refusal_detail(caught.value)["message"]


def test_lifetools_descriptor_carries_scoped_broker_credentials(monkeypatch):
    from src.openclank import acp_bridge

    monkeypatch.setattr(acp_bridge, "_MEMORY_BROKER_URL", "")
    monkeypatch.setattr(acp_bridge, "_MEMORY_BROKER_TOKEN", "")
    acp_bridge.configure_memery_broker(
        "http://127.0.0.1:7000/api/internal/memery/tool",
        "broker-secret",
    )

    descriptor = acp_bridge.lifetools_mcp_descriptor(
        owner="alice",
        session_id="chat-1",
        workspace="/workspace",
    )
    env = {item["name"]: item["value"] for item in descriptor["env"]}
    assert env["OPEN_CLANK_MEMORY_BROKER_URL"].startswith("http://127.0.0.1:")
    assert env["OPEN_CLANK_MEMORY_BROKER_TOKEN"] == memery_broker_token(
        "broker-secret", "alice", "global"
    )
    assert env["OPEN_CLANK_MEMORY_BROKER_TOKEN"] != "broker-secret"
    assert env["FM_OWNER"] == "alice"


@pytest.mark.asyncio
async def test_internal_broker_requires_direct_loopback_even_with_valid_token(broker_app, monkeypatch):
    monkeypatch.setenv("DEBUG", "false")

    class _Provider:
        async def invoke_tool(self, name, arguments):
            return {"name": name, "arguments": arguments}

    monkeypatch.setattr(broker_app, "memory_provider", _Provider())
    request = _request(forwarded=True)

    with pytest.raises(HTTPException) as raised:
        await _broker_endpoint(broker_app)(
            request, {"name": "search", "arguments": {}}
        )
    assert raised.value.status_code == 403


@pytest.mark.asyncio
async def test_internal_broker_overwrites_scope_for_known_owner(
    broker_app,
    monkeypatch,
):
    monkeypatch.setenv("DEBUG", "false")

    class _Provider:
        async def invoke_tool(self, name, arguments):
            return {"name": name, "arguments": arguments}

    monkeypatch.setattr(broker_app, "memory_provider", _Provider())
    request = _request()

    assert await _broker_endpoint(broker_app)(
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
async def test_internal_broker_keeps_memory_quality_read_only(broker_app, monkeypatch):
    monkeypatch.setenv("DEBUG", "false")

    calls = []

    class _Provider:
        def broker_health(self):
            return {"ready": True, "profile": "guarded"}

        async def invoke_tool(self, name, arguments):
            calls.append((name, arguments))
            raise AssertionError("the broker handshake should use the host readiness receipt")

    monkeypatch.setattr(broker_app, "memory_provider", _Provider())
    request = _request()

    with pytest.raises(HTTPException) as raised:
        await _broker_endpoint(broker_app)(
            request,
            {
                "name": "memory_quality",
                "arguments": {"rebuild_graph_fts": True},
            },
        )
    assert raised.value.status_code == 400
    assert calls == []

    assert await _broker_endpoint(broker_app)(
        request,
        {
            "name": "memory_quality",
            "arguments": {
                "rebuild_graph_fts": False,
                "owner": "mallory",
                "future_global_control": True,
            },
        },
    ) == {"ready": True, "profile": "guarded"}
    assert calls == []


@pytest.mark.asyncio
async def test_internal_broker_rejects_cross_owner_token_reuse(broker_app):

    alice_token = memery_broker_token(
        INTERNAL_TOOL_TOKEN, "alice", "global"
    )
    request = _request(owner="bob", token=alice_token)

    with pytest.raises(HTTPException) as raised:
        await _broker_endpoint(broker_app)(
            request, {"name": "search", "arguments": {}}
        )
    assert raised.value.status_code == 403


@pytest.mark.asyncio
async def test_internal_broker_requires_a_known_owner(
    broker_app,
    monkeypatch,
):
    monkeypatch.setenv("DEBUG", "false")

    monkeypatch.setattr(
        broker_app,
        "auth_manager",
        type("_Auth", (), {"users": {"alice": {}}})(),
    )

    unknown = _request(owner="mallory")
    with pytest.raises(HTTPException) as raised:
        await _broker_endpoint(broker_app)(
            unknown, {"name": "search", "arguments": {}}
        )
    assert raised.value.status_code == 403


@pytest.mark.asyncio
async def test_internal_broker_is_blocked_by_account_lifecycle_fence(broker_app, monkeypatch):
    from src.openclank.account_request_barrier import AccountRequestBarrier

    class _Provider:
        async def invoke_tool(self, _name, _arguments):
            raise AssertionError("fenced broker request reached memory provider")

    monkeypatch.setattr(broker_app, "memory_provider", _Provider())
    monkeypatch.setattr(
        broker_app,
        "_account_request_barrier",
        AccountRequestBarrier(lambda owner: owner == "alice"),
    )

    with pytest.raises(HTTPException) as raised:
        await _broker_endpoint(broker_app)(
            _request(owner="alice"),
            {"name": "search", "arguments": {"query": "blocked"}},
        )
    assert raised.value.status_code == 409


@pytest.mark.asyncio
async def test_internal_broker_rejects_tools_outside_scoped_contract(broker_app):

    request = _request()

    with pytest.raises(HTTPException) as raised:
        await _broker_endpoint(broker_app)(
            request, {"name": "groom", "arguments": {}}
        )
    assert raised.value.status_code == 400


@pytest.mark.asyncio
@pytest.mark.parametrize(("session_id", "text", "category", "expected"), [
    ("broker-chat", "The juniper notebook is green.", "fact", "The juniper notebook is green."),
    (None, "The juniper notebook is green.", "fact", "The juniper notebook is green."),
    pytest.param(None, "Where  is the juniper notebook ?? ", "unknown", "Where is the juniper notebook?",
                 id="question_normalization"),
])
async def test_broker_reads_from_the_running_host_worker(
    broker_app, monkeypatch, _isolated_host_store, session_id, text, category, expected,
):
    import sqlite3
    import tempfile
    import httpx
    from fastapi import FastAPI
    from src.runtime_paths import resolve_memery_worker

    worker = resolve_memery_worker().path
    url = "http://127.0.0.1:7000/api/internal/memery/tool"
    web = FastAPI()
    web.add_api_route("/api/internal/memery/tool", _broker_endpoint(broker_app), methods=["POST"])
    transport = httpx.ASGITransport(app=web, client=("127.0.0.1", 12345))
    client_class = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: client_class(transport=transport, **kwargs))
    with tempfile.TemporaryDirectory(dir=os.environ["OPEN_CLANK_DATA_DIR"]) as directory:
        database_path = str(Path(directory) / "memery.db")
        database_id = _isolated_host_store(database_path)
        host = MemeryProvider(command=worker, env={"FM_DB_PATH": database_path, "FM_DB_ID": database_id},
                              broker_url="", host_worker=True)
        broker = MemeryProvider(broker_url=url, broker_token=memery_broker_token(INTERNAL_TOOL_TOKEN, "alice", "global"),
                                env={"FM_OWNER": "alice", "FM_WORKSPACE_ID": "global"})
        monkeypatch.setattr(broker_app, "memory_provider", host)
        try:
            await host.initialize()
            process = host._owned_transport.process
            await broker.initialize()
            record = await broker.remember(text, owner="alice", session_id=session_id, category=category)
            assert record.text == expected
            assert any(row.id == record.id for row in await host.list_memories(owner="alice"))
            assert not any(row.id == record.id for row in await host.list_memories(owner="another-owner"))
            result = await broker.invoke_tool("get_memory", {"id": record.id, "owner": "mallory"})
            assert result["record"]["content"] == expected
            assert any(row.id == record.id for row in await host.list_v2_records(owner="alice"))
            with sqlite3.connect(database_path) as db:
                evidence = db.execute(
                    "SELECT e.evidence_id FROM fm_v2_revision_evidence r JOIN fm_v2_evidence e "
                    "ON e.owner_id=r.owner_id AND e.evidence_id=r.evidence_id "
                    "WHERE r.owner_id='alice' AND e.locator_type='native_current_event'").fetchall()
                assert len(evidence) == 1
                db.execute("UPDATE raw SET content='changed retained source' WHERE owner='alice'")
            assert not any(row.id == record.id for row in await host.list_v2_records(owner="alice"))
            assert (await broker.invoke_tool("get_memory", {"id": record.id}))["record"] is None
            assert host._owned_transport.process is process and process.returncode is None
            assert broker._owner_task is None
        finally:
            await broker.shutdown()
            await host.shutdown()
        assert process.returncode == 0
