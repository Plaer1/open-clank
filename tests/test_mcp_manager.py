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
    assert "restart Odysseus" in msg


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
