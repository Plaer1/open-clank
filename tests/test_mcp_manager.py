import asyncio
import time
from unittest.mock import patch

import pytest

from src.mcp_manager import _format_mcp_connection_error, McpManager


def test_playwright_mcp_connection_error_includes_install_hint():
    msg = _format_mcp_connection_error(
        "Browser (Playwright)",
        "npx",
        ["-y", "@playwright/mcp@latest", "--headless"],
        RuntimeError("package not found"),
    )

    assert "package not found" in msg
    assert "Browser MCP could not start" in msg
    assert "npx -y @playwright/mcp@latest --version" in msg
    assert "restart Open Clank" in msg


def test_generic_mcp_connection_error_preserves_original_error():
    msg = _format_mcp_connection_error(
        "Custom MCP",
        "python",
        ["server.py"],
        RuntimeError("boom"),
    )

    assert msg == "boom"


def test_http_transport_routes_to_start_http_connect():
    mgr = McpManager()

    async def fake_start(server_id, name, url):
        return "ROUTED"

    with patch.object(McpManager, "_start_http_connect", side_effect=fake_start) as m:
        result = asyncio.run(mgr.connect_server("id1", "n", "http", url="https://x/mcp"))
    assert result == "ROUTED"
    m.assert_called_once()


class _TaskBoundContext:
    def __init__(self, *, exit_delay: float = 0.0, hang_on_exit: bool = False):
        self.enter_task = None
        self.exit_task = None
        self.exit_delay = exit_delay
        self.hang_on_exit = hang_on_exit
        self.exit_cancelled = False

    async def __aenter__(self):
        self.enter_task = asyncio.current_task()
        return self

    async def __aexit__(self, *_exc):
        self.exit_task = asyncio.current_task()
        try:
            if self.hang_on_exit:
                await asyncio.Event().wait()
            elif self.exit_delay:
                await asyncio.sleep(self.exit_delay)
        except asyncio.CancelledError:
            self.exit_cancelled = True
            raise


def test_owned_connection_exits_context_in_entering_task():
    async def scenario():
        manager = McpManager()
        context = _TaskBoundContext()

        async def opener(stack):
            await stack.enter_async_context(context)
            return object(), [], {}

        caller = asyncio.current_task()
        assert await manager._start_owned_connection("owned", "Owned", "stdio", opener)
        owner = manager._connect_tasks["owned"]
        assert context.enter_task is owner
        assert owner is not caller

        await manager.disconnect_server("owned")

        assert context.exit_task is context.enter_task
        assert "owned" not in manager._connect_tasks
        assert "owned" not in manager._sessions

    asyncio.run(scenario())


def test_partial_startup_closes_acquired_context_in_owner_task():
    async def scenario():
        manager = McpManager()
        context = _TaskBoundContext()

        async def opener(stack):
            await stack.enter_async_context(context)
            raise RuntimeError("initialization failed")

        with pytest.raises(RuntimeError, match="initialization failed"):
            await manager._start_owned_connection("partial", "Partial", "stdio", opener)

        assert context.exit_task is context.enter_task
        assert "partial" not in manager._connect_tasks
        assert "partial" not in manager._sessions

    asyncio.run(scenario())


def test_disconnect_all_closes_independent_owners_concurrently():
    async def scenario():
        manager = McpManager()
        contexts = []
        for index in range(3):
            context = _TaskBoundContext(exit_delay=0.15)
            contexts.append(context)

            async def opener(stack, current=context):
                await stack.enter_async_context(current)
                return object(), [], {}

            assert await manager._start_owned_connection(
                f"server-{index}", f"Server {index}", "stdio", opener
            )

        started = time.perf_counter()
        await manager.disconnect_all()
        elapsed = time.perf_counter() - started

        assert elapsed < 0.35
        assert all(context.exit_task is context.enter_task for context in contexts)
        assert not manager._connect_tasks
        assert not manager._sessions

    asyncio.run(scenario())


def test_hung_owner_close_is_bounded_and_idempotent(monkeypatch):
    async def scenario():
        import src.mcp_manager as mcp_manager

        monkeypatch.setattr(mcp_manager, "_MCP_DISCONNECT_TIMEOUT", 0.05)
        manager = McpManager()
        context = _TaskBoundContext(hang_on_exit=True)

        async def opener(stack):
            await stack.enter_async_context(context)
            return object(), [], {}

        assert await manager._start_owned_connection("hung", "Hung", "stdio", opener)
        started = time.perf_counter()
        await manager.disconnect_server("hung")
        await manager.disconnect_server("hung")
        elapsed = time.perf_counter() - started

        assert elapsed < 0.5
        assert context.exit_task is context.enter_task
        assert context.exit_cancelled is True
        assert not manager._connect_tasks
        assert not manager._sessions

    asyncio.run(scenario())


def test_protected_browser_client_is_hidden_and_cannot_use_generic_calls():
    async def scenario():
        manager = McpManager()
        manager._sessions["builtin_browser"] = object()
        manager._tools["builtin_browser"] = [{"name": "browser_navigate", "description": "nav"}]
        assert manager.get_all_tools() == []
        assert manager.get_all_openai_schemas() == []
        assert "builtin_browser" not in manager.get_all_statuses()
        assert (await manager.call_tool(
            "mcp__builtin_browser__browser_navigate", {"url": "https://example.test"}
        ))["exit_code"] == 1
        with pytest.raises(PermissionError):
            await manager.disconnect_server("builtin_browser")
        assert await manager.connect_server(
            "builtin_browser", "Browser", "stdio", command="npx", args=[]
        ) is False

    asyncio.run(scenario())


def test_private_browser_dispatch_is_allowlisted_and_never_retries():
    async def scenario():
        manager = McpManager()
        async def fake_connect(server_id, name, command, args, env):
            manager._sessions[server_id] = object()
            manager._tools[server_id] = [
                {"name": "browser_click", "description": "click"},
                {"name": "browser_run_code_unsafe", "description": "unsafe"},
                {"name": "browser_network_request", "description": "body"},
            ]
            manager._connections[server_id] = {"status": "connected", "name": name}
            return True

        with patch.object(manager, "_connect_stdio", side_effect=fake_connect):
            handle = await manager.open_browser_client(
                name="Browser", command="npx", args=["private"], env={},
            )
        connection_id = handle["connection_id"]
        assert connection_id not in manager.get_all_statuses()
        assert all(tool["name"] != "browser_run_code_unsafe" for tool in handle["tools"])
        session = manager._sessions[connection_id]
        calls = []

        async def fail_once(_session, name, arguments):
            calls.append((name, arguments))
            raise TimeoutError("unknown remote outcome")

        manager._do_call = fail_once
        result = await manager.call_browser_tool_once(
            connection_id, "browser_click", {"target": "e1"},
        )
        assert result["outcome_unknown"] is True
        assert len(calls) == 1
        rejected = await manager.call_browser_tool_once(
            connection_id, "browser_run_code_unsafe", {"code": ""},
        )
        assert rejected["exit_code"] == 1
        assert len(calls) == 1
        assert (await manager.call_browser_tool_once(
            connection_id, "browser_network_request", {"index": 1},
        ))["exit_code"] == 1
        assert len(calls) == 1
        await manager.close_browser_client(connection_id)
        assert connection_id not in manager._protected_browser_clients

    asyncio.run(scenario())


def test_builtin_lost_response_repairs_connection_without_replaying_action():
    async def scenario():
        manager = McpManager()
        original, replacement = object(), object()
        manager._sessions['synthetic'] = original
        effects = []
        async def call(session, name, arguments):
            if session is original:
                effects.append(arguments['value'])
                raise ConnectionError('response lost after commit')
            return {'stdout': 'available', 'exit_code': 0}
        async def reconnect(server_id):
            manager._sessions[server_id] = replacement
            return True
        with patch.object(manager, 'is_builtin', return_value=True), patch.object(manager, '_do_call', side_effect=call), patch.object(manager, '_reconnect_builtin', side_effect=reconnect):
            result = await manager.call_tool('mcp__synthetic__write', {'value': 'one'})
            assert result['outcome_unknown'] is True
            assert effects == ['one'] and manager._sessions['synthetic'] is original
            assert await manager._reconnect_builtin('synthetic')
            assert (await manager.call_tool('mcp__synthetic__read', {}))['stdout'] == 'available'
    asyncio.run(scenario())
