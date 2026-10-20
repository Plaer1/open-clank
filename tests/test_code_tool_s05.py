"""Real broker/registry branches over controlled authority/query seams.

These tests import the actual app only after synthetic startup validation and
database-preparation/native-start stubs. No lifespan, service, native fallback
or model runs. S06 actual-child tests separately qualify backend identity.
"""
import asyncio
from contextlib import asynccontextmanager
import importlib
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException, Request


@pytest.fixture
def broker(monkeypatch, request):
    root = Path(os.environ["OPEN_CLANK_DATA_DIR"]).resolve()
    # Source workspaces live in sibling task-root/tests, not in private app
    # DATA_DIR. The actual dispatcher must keep rejecting control-data roots.
    assert root.name == "data" and root.parent.name.startswith("openclank-s03-s06-projection.")
    assert root.parent.is_relative_to(Path("/tmp").resolve())
    assert Path(os.environ["ODYSSEUS_DATA_DIR"]).resolve() == root
    assert Path(str(request.config.option.basetemp)).resolve() == root.parent / "tests"
    for key in ("ODYSSEUS_DATA_DIR", "OPEN_CLANK_AUTHORITY_DB_PATH", "OPEN_CLANK_AUTHORITY_AUTH_PATH", "FM_DB_PATH", "OPENCLANK_CONVERSATION_ARCHIVE_DB"):
        assert Path(os.environ[key]).resolve().is_relative_to(root), key
    assert os.environ["DATABASE_URL"] == "sqlite:///:memory:"
    assert os.environ["ODYSSEUS_INPROCESS_POLLERS"] == "0"
    assert os.environ["PYTHONDONTWRITEBYTECODE"] == "1"
    assert "OPEN_CLANK_AGENT_HOME" not in os.environ and "THESIUS_AGENT_HOME" not in os.environ
    binary = Path(os.environ["FM_MCP_COMMAND"]).resolve()
    assert binary.is_file() and binary == Path(os.environ["OPEN_CLANK_TEST_FM_MCP_BIN"]).resolve()
    monkeypatch.setenv("DEBUG", "false")
    from src import app_initializer, frankenmemory_database, secret_storage
    from src.frankenmemory_provider import FrankenmemoryProvider

    def prepare(path=None):
        if path is not None:
            assert Path(path).resolve().is_relative_to(root)
        return "s05-code-tool-synthetic-database"

    async def no_native_start(_self):
        raise AssertionError("Mock broker test must never initialize a native provider")

    monkeypatch.setattr(app_initializer, "prepare_frankenmemory_database", prepare)
    monkeypatch.setattr(frankenmemory_database, "prepare_frankenmemory_database", prepare)
    monkeypatch.setattr(FrankenmemoryProvider, "initialize", no_native_start)
    monkeypatch.setattr(secret_storage, "_KEY_PATH", root / "synthetic-secret.key")
    monkeypatch.setattr(secret_storage, "_fernet", None)
    auth_path = Path(os.environ["OPEN_CLANK_AUTHORITY_AUTH_PATH"])
    auth_path.parent.mkdir(parents=True, exist_ok=True)
    auth_path.write_text(json.dumps({"users": {name: {"account_id": "account-s05-fixture-" + name, "is_admin": False} for name in ("alice", "bob")}}))
    app = importlib.import_module("app")  # No lifespan; startup stubs already installed.
    from src.openclank import code_query, transcript_projection

    state = SimpleNamespace(app=app, calls=[], handler=None, barrier_error=False,
        binding={"owner": "alice", "stableChatID": "chat-a", "authorityWorkspaceID": "workspace-a", "workspaceRevision": 7, "transition": None},
        session=SimpleNamespace(id="chat-a", owner="alice", workspace_id="workspace-a", archived=False))

    def managed(sid, *, owner):
        if sid != "chat-a" or owner != "alice":
            raise KeyError("private managed binding")
        return dict(state.binding)

    @asynccontextmanager
    async def admitted(owner):
        assert owner == "alice"
        if state.barrier_error:
            if state.barrier_error == "fenced":
                from src.openclank.account_request_barrier import AccountRequestFenced
                raise AccountRequestFenced("/private/account/fenced diagnostic")
            raise RuntimeError("/private/account/barrier diagnostic")
        yield

    async def query(**kwargs):
        state.calls.append(kwargs)
        validated = code_query.validate_query(kwargs["query"])
        if state.handler:
            return await state.handler(kwargs)
        result = code_query.error_result(validated, "unavailable", "Synthetic unavailable")
        result.update(error=None, degraded=False, reasons=[], operation_capabilities=[],
            receipt={"workspace_id": kwargs["workspace_id"], "registration_id": "r", "checkout_id": "c", "source_snapshot_id": "s", "head_generation": "g", "admission_epoch": 1, "policy_generation": 1, "config_fingerprint": "f"})
        return code_query.measure_envelope(result, validated["budgets"])

    monkeypatch.setattr(transcript_projection, "get_managed_binding", managed)
    monkeypatch.setattr(code_query, "query_code", query)
    monkeypatch.setattr(app, "auth_manager", SimpleNamespace(users={name: {"account_id": "account-s05-fixture-" + name, "is_admin": False} for name in ("alice", "bob")}))
    monkeypatch.setattr(app, "session_manager", SimpleNamespace(get_session=lambda sid: state.session if sid == "chat-a" else None))
    monkeypatch.setattr(app, "_account_request_barrier", SimpleNamespace(admitted=admitted))
    monkeypatch.setattr(app, "memory_provider", object())
    return state


def request(state, *, code_token=None, global_token=None, sid="chat-a", workspace="workspace-a", revision=7, queue=None):
    from core.middleware import INTERNAL_TOOL_HEADER, INTERNAL_TOOL_OWNER_HEADER, INTERNAL_TOOL_WORKSPACE_HEADER
    from src.openclank.acp_bridge import frankenmemory_broker_token
    from src.openclank.code_query import code_binding_token
    global_token = global_token if global_token is not None else frankenmemory_broker_token(state.app.INTERNAL_TOOL_TOKEN, "alice", "global")
    code_token = code_token if code_token is not None else code_binding_token(state.app.INTERNAL_TOOL_TOKEN, owner="alice", session_id=sid, workspace_id=workspace, binding_revision=revision)
    headers = {INTERNAL_TOOL_HEADER: global_token, INTERNAL_TOOL_OWNER_HEADER: "alice", INTERNAL_TOOL_WORKSPACE_HEADER: "global", "X-OpenClank-Code-Binding": code_token}
    async def receive():
        if queue is None:
            await asyncio.Event().wait()
        return await queue.get()
    return Request({"type": "http", "http_version": "1.1", "method": "POST", "scheme": "http", "path": "/api/internal/frankenmemory/tool", "raw_path": b"/api/internal/frankenmemory/tool", "query_string": b"", "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()], "client": ("127.0.0.1", 1234), "server": ("127.0.0.1", 7000), "app": state.app.app}, receive)


def body(*, sid="chat-a", revision=7, query=None):
    return {"name": "code_query", "arguments": {"session_id": sid, "binding_revision": revision, "query": query or {"operation": "catalog"}}}


def assert_bounded(result, maximum=8192):
    encoded = json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode()
    assert len(encoded) <= maximum
    assert result["cost"]["serialized_bytes"] == len(encoded)
    assert "/private" not in encoded.decode()


async def test_actual_broker_uses_owned_live_chat_and_separate_purpose_token(broker):
    result = await broker.app.internal_frankenmemory_tool(request(broker), body())
    assert result["error"] is None
    assert_bounded(result)
    assert len(broker.calls) == 1
    call = broker.calls[0]
    assert call["owner"] == "alice" and call["workspace_id"] == "workspace-a"
    assert call["purpose"] == "agent_workspace" and call["session"] is broker.session


@pytest.mark.parametrize("change", ["missing", "global_as_code", "code_as_global", "wrong_chat", "wrong_workspace", "wrong_revision"])
async def test_actual_broker_rejects_missing_swapped_or_stale_code_credentials(broker, change):
    from src.openclank.acp_bridge import frankenmemory_broker_token
    from src.openclank.code_query import code_binding_token
    global_token = frankenmemory_broker_token(broker.app.INTERNAL_TOOL_TOKEN, "alice", "global")
    code = code_binding_token(broker.app.INTERNAL_TOOL_TOKEN, owner="alice", session_id="chat-a", workspace_id="workspace-a", binding_revision=7)
    kwargs = {"missing": {"code_token": ""}, "global_as_code": {"code_token": global_token}, "code_as_global": {"global_token": code}, "wrong_chat": {"sid": "chat-b"}, "wrong_workspace": {"workspace": "workspace-b"}, "wrong_revision": {"revision": 6}}[change]
    with pytest.raises(HTTPException) as caught:
        await broker.app.internal_frankenmemory_tool(request(broker, **kwargs), body())
    assert caught.value.status_code == 403
    assert broker.calls == []


@pytest.mark.parametrize("change", ["owner", "workspace", "revision", "transition", "session_replaced", "archived", "session_owner", "session_workspace"])
async def test_actual_broker_rechecks_binding_after_held_query_and_measures_denial(broker, change):
    started, release = asyncio.Event(), asyncio.Event()
    async def held(_kwargs):
        started.set()
        await release.wait()
        return {"schema": "open-clank.code-query/v1", "error": None, "hits": [{"id": "decl:bad"}]}
    broker.handler = held
    task = asyncio.create_task(broker.app.internal_frankenmemory_tool(request(broker), body(query={"operation": "catalog", "budgets": {"bytes": 512, "tokens": 512}})))
    await asyncio.wait_for(started.wait(), 2)
    if change == "owner": broker.binding["owner"] = "bob"
    if change == "workspace": broker.binding["authorityWorkspaceID"] = "workspace-b"
    if change == "revision": broker.binding["workspaceRevision"] += 1
    if change == "transition": broker.binding["transition"] = {"phase": "in_flight"}
    if change == "session_replaced": broker.session = SimpleNamespace(id="chat-a", owner="alice", workspace_id="workspace-a", archived=False)
    if change == "archived": broker.session.archived = True
    if change == "session_owner": broker.session.owner = "bob"
    if change == "session_workspace": broker.session.workspace_id = "workspace-b"
    release.set()
    result = await asyncio.wait_for(task, 2)
    assert result["error"]["code"] == "denied" and result["receipt"] is None
    assert not result.get("hits")
    assert_bounded(result, 512)


@pytest.mark.parametrize("failure", ["fenced", "runtime"])
async def test_actual_broker_barrier_entry_failure_is_bounded_not_500(broker, failure):
    broker.barrier_error = failure
    result = await broker.app.internal_frankenmemory_tool(request(broker), body(query={"operation": "catalog", "budgets": {"bytes": 512, "tokens": 512}}))
    assert result["error"]["code"] in {"denied", "unavailable"}
    assert_bounded(result, 512)


async def test_actual_broker_asgi_disconnect_cancels_owned_query_task(broker):
    started, cancelled = asyncio.Event(), asyncio.Event()
    async def held(_kwargs):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()
    broker.handler = held
    queue = asyncio.Queue()
    task = asyncio.create_task(broker.app.internal_frankenmemory_tool(request(broker, queue=queue), body()))
    await asyncio.wait_for(started.wait(), 2)
    # Actual Request receive disconnect, not task.cancel()/fake-function cancel.
    await queue.put({"type": "http.disconnect"})
    try:
        await asyncio.wait_for(cancelled.wait(), 2)
        try:
            result = await asyncio.wait_for(task, 2)
        except asyncio.CancelledError:
            pass  # Disconnected caller may propagate actual cancellation.
        else:
            assert result["error"]["code"] == "cancelled" and not result.get("hits")
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


def test_actual_registry_lifetools_schema_and_plan_mode_are_read_only(broker):
    from src.agent_tools import TOOL_HANDLERS
    from src.tool_policy import known_tool_names
    from src.tool_security import PLAN_MODE_READONLY_TOOLS, plan_mode_disabled_tools
    from src.tool_schemas import FUNCTION_TOOL_SCHEMAS, function_call_to_tool_block
    from src.openclank import lifetools_server
    schemas = [row["function"] for row in FUNCTION_TOOL_SCHEMAS if row.get("function", {}).get("name") == "search_code"]
    assert len(schemas) == 1 and callable(TOOL_HANDLERS["search_code"])
    assert "search_code" in known_tool_names() and "search_code" in PLAN_MODE_READONLY_TOOLS
    assert "search_code" not in plan_mode_disabled_tools()
    advertised = [tool for tool in lifetools_server._build_tool_list() if tool.name == "search_code"]
    assert len(advertised) == 1 and advertised[0].inputSchema == schemas[0]["parameters"]
    assert not {"owner", "root", "workspace_id", "action"} & schemas[0]["parameters"]["properties"].keys()
    assert "pipeline" not in schemas[0]["parameters"]["properties"]["operation"]["enum"]
    assert function_call_to_tool_block("search_code", '{"operation":"catalog"}').tool_type == "search_code"


async def test_actual_plan_mode_dispatch_executes_registered_code_reader(broker, tmp_path):
    from core.models import Session
    from src import ai_interaction
    from src.openclank.file_policy import FilePolicyRepository
    from src.tool_execution import execute_tool_block
    from src.tool_schemas import function_call_to_tool_block
    from src.tool_security import plan_mode_disabled_tools
    repository = FilePolicyRepository()
    assert Path(repository.db_path).resolve().is_relative_to(Path(os.environ["OPEN_CLANK_DATA_DIR"]).resolve())
    repository.sync_subjects(broker.app.auth_manager.users)
    folder = tmp_path / "plan-code"
    folder.mkdir()
    assert folder.resolve().is_relative_to(Path(os.environ["OPEN_CLANK_DATA_DIR"]).resolve().parent)
    assert not folder.resolve().is_relative_to(Path(os.environ["OPEN_CLANK_DATA_DIR"]).resolve())
    location = repository.create_location(actor_subject_id="account-s05-admin", path=str(folder), kind="directory", capabilities=("read",))
    subject = "account-s05-fixture-alice"
    for kind in ("people", "agent"):
        repository.create_binding(actor_subject_id="account-s05-admin", binding_class=kind, subject_id=subject, location_id=location.id, capabilities=("read",))
    workspace = repository.create_workspace(actor_subject_id=subject, owner_subject_id=subject, location_id=location.id, name="plan fixture")
    session = Session(id="plan-chat", name="synthetic", endpoint_url="", model="", owner="alice", workspace_id=workspace.id)
    previous_sessions = ai_interaction.get_session_manager()
    previous_memory = tuple(getattr(ai_interaction, key) for key in ("_memory_manager", "_memory_vector", "_memory_provider", "_memory_lifecycle"))
    ai_interaction.set_session_manager(SimpleNamespace(get_session=lambda sid: session if sid == session.id else None))
    ai_interaction.set_memory_manager(None, provider=object())  # Query seam is controlled, no native child.
    try:
        block = function_call_to_tool_block("search_code", '{"operation":"catalog"}')
        description, result = await execute_tool_block(block, owner="alice", session_id=session.id,
            workspace=str(folder.resolve()), authority_workspace_id=workspace.id, disabled_tools=plan_mode_disabled_tools())
        assert description.startswith("registry: search_code"), (description, result)
        assert result["exit_code"] == 0 and json.loads(result["output"])["error"] is None
        call = broker.calls[-1]
        assert call["owner"] == "alice" and call["workspace_id"] == workspace.id and call["session"] is session
    finally:
        ai_interaction.set_session_manager(previous_sessions)
        ai_interaction.set_memory_manager(previous_memory[0], vector=previous_memory[1], provider=previous_memory[2], lifecycle=previous_memory[3])


def test_actual_acp_descriptor_code_token_binds_chat_workspace_revision(broker, monkeypatch):
    from src.openclank import acp_bridge
    from src.openclank.code_query import code_binding_token
    monkeypatch.setattr(acp_bridge, "_MEMORY_BROKER_URL", "http://127.0.0.1:7000/api/internal/frankenmemory/tool")
    monkeypatch.setattr(acp_bridge, "_MEMORY_BROKER_TOKEN", "synthetic-master")
    def descriptor(**extra):
        arguments = dict(owner="alice", session_id="chat-a", workspace=str(Path(os.environ["OPEN_CLANK_DATA_DIR"]).resolve()), authority_workspace_id="workspace-a", binding_revision=7)
        return {row["name"]: row["value"] for row in acp_bridge.lifetools_mcp_descriptor(**{**arguments, **extra})["env"]}
    first = descriptor()
    assert first["OPEN_CLANK_CODE_BINDING_TOKEN"] == code_binding_token("synthetic-master", owner="alice", session_id="chat-a", workspace_id="workspace-a", binding_revision=7)
    assert first["OPEN_CLANK_CODE_BINDING_TOKEN"] != first["OPEN_CLANK_MEMORY_BROKER_TOKEN"]
    for change in ({"session_id": "chat-b"}, {"authority_workspace_id": "workspace-b"}, {"binding_revision": 8}):
        changed = descriptor(**change)
        assert changed["OPEN_CLANK_CODE_BINDING_TOKEN"] != first["OPEN_CLANK_CODE_BINDING_TOKEN"]
        assert changed["OPEN_CLANK_MEMORY_BROKER_TOKEN"] == first["OPEN_CLANK_MEMORY_BROKER_TOKEN"]


async def test_friendly_broker_outage_is_unavailable_not_authority_denial(broker, monkeypatch):
    from src import ai_interaction
    from src.agent_tools.code_tools import SearchCodeTool
    from src.memory_provider import MemoryTransportError
    class Broken:
        _broker_url = "http://127.0.0.1"
        async def code_query_session(self, **_kwargs):
            raise MemoryTransportError("/private/broker diagnostic")
    monkeypatch.setattr(ai_interaction, "_memory_provider", Broken())
    result = await SearchCodeTool().execute('{"operation":"catalog"}', {"owner": "alice", "session_id": "chat-a", "authority_workspace_id": "workspace-a"})
    parsed = json.loads(result["output"])
    assert parsed["error"]["code"] == "unavailable"
    assert "/private" not in result["output"]


async def test_lifetools_prehandler_code_failure_never_returns_raw_diagnostic(broker, monkeypatch):
    from src.openclank import lifetools_server
    async def broken():
        raise RuntimeError("/private/init diagnostic")
    monkeypatch.setattr(lifetools_server, "_ensure_memory_provider", broken)
    result = await lifetools_server.call_tool("search_code", {"operation": "catalog"})
    content = result.content if hasattr(result, "content") else result
    text = "".join(row.text for row in content)
    assert "/private" not in text
    payload = json.loads(text)
    if isinstance(payload.get("output"), str): payload = json.loads(payload["output"])
    assert payload["error"]["code"] == "unavailable"


@pytest.mark.parametrize("case", ["minimum", "escaped_overhead", "observed_cost", "success"])
async def test_friendly_complete_wrapper_budget_and_observed_cost(broker, monkeypatch, case):
    from src import ai_interaction
    from src.agent_tools.code_tools import SearchCodeTool
    from src.openclank import code_query
    cap = 512 if case == "minimum" else (4096 if case == "success" else 2048)
    query = code_query.validate_query(dict(operation="catalog", budgets=dict(bytes=cap, tokens=cap)))
    response = code_query.error_result(query, "budget_exceeded", "Synthetic bounded failure")
    if case != "minimum":
        response.update(error=None, degraded=False, reasons=[])
    if case == "escaped_overhead":
        # Inner success fits. Encoding its JSON as the tool output string
        # doubles these escapes and must fail closed instead of forwarding it.
        response["hits"] = [dict(id="file:" + "a" * 64, kind="file", properties=dict(text='"\\\n' * 150))]
    if case == "observed_cost":
        response["hits"] = [dict(id="file:" + "a" * 64, kind="file", properties=dict(text="x" * 3000))]
        response["cost"].update(files_bytes_read=123, validation_bytes_read=456, native_rpc_calls_completed=2)
    if case != "observed_cost":
        response = code_query.measure_envelope(response, query["budgets"])
    if case == "escaped_overhead":
        initial = dict(output=json.dumps(response, ensure_ascii=False, separators=(",", ":")), exit_code=0)
        assert len(json.dumps(initial, ensure_ascii=False, separators=(",", ":")).encode()) > cap
    class ControlledBroker:
        _broker_url = "http://127.0.0.1"
        async def code_query_session(self, **kwargs):
            assert kwargs["query"]["budgets"]["bytes"] == cap
            return response
    monkeypatch.setattr(ai_interaction, "_memory_provider", ControlledBroker())
    result = await SearchCodeTool().execute(json.dumps(query),
        dict(owner="alice", session_id="chat-a", authority_workspace_id="workspace-a"))
    size = len(json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode())
    assert size <= cap
    payload = json.loads(result["output"])
    assert payload["cost"]["tool_wrapper_bytes"] == size
    assert payload["cost"]["serialized_bytes"] == len(result["output"].encode())
    if case == "success":
        assert result["exit_code"] == 0 and payload["error"] is None
    else:
        assert result["exit_code"] == 1 and payload["error"]["code"] == "budget_exceeded"
        assert payload["receipt"] is None and not payload.get("next_cursor") and not payload.get("hits")
    if case == "observed_cost":
        assert payload["cost"]["files_bytes_read"] == 123
        assert payload["cost"]["validation_bytes_read"] == 456
        assert payload["cost"]["native_rpc_calls_completed"] == 2
