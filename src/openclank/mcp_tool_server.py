"""Small compatibility boundary for MCP tool-only stdio servers.

MCP 2 registers low-level request callbacks at construction time and removed
the ``Server.list_tools`` / ``Server.call_tool`` decorators used by MCP 1.
Open Clank's stdio servers keep their simple, directly testable handlers while
this adapter translates them to the current request/result envelopes.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from mcp import types
from mcp.server import Server as _Server


if hasattr(_Server, "list_tools"):
    ToolServer = _Server
else:

    class ToolServer(_Server):
        """MCP 2 ``Server`` with the MCP 1 tool decorator surface."""

        def list_tools(self) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
            def register(handler: Callable[..., Any]) -> Callable[..., Any]:
                async def on_list_tools(_context, _params):
                    result = await handler()
                    if isinstance(result, types.ListToolsResult):
                        return result
                    return types.ListToolsResult(tools=list(result or []))

                self.add_request_handler(
                    "tools/list",
                    types.PaginatedRequestParams,
                    on_list_tools,
                )
                return handler

            return register

        def call_tool(self) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
            def register(handler: Callable[..., Any]) -> Callable[..., Any]:
                async def on_call_tool(_context, params):
                    result = await handler(params.name, params.arguments or {})
                    if isinstance(result, types.CallToolResult):
                        return result
                    return types.CallToolResult(content=list(result or []))

                self.add_request_handler(
                    "tools/call",
                    types.CallToolRequestParams,
                    on_call_tool,
                )
                return handler

            return register


__all__ = ["ToolServer"]
