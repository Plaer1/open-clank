"""
mcp_manager.py

Manages connections to MCP (Model Context Protocol) tool servers.
Each server exposes tools that are made available to the agent loop.
"""

import json
import logging
import os
import re
import asyncio
from contextlib import AsyncExitStack
from typing import Any, Awaitable, Callable, Dict, List, Optional, Set, Tuple
from src.database import McpServer, SessionLocal

from src.runtime_paths import get_app_root

logger = logging.getLogger(__name__)


def _mcp_child_stderr_log(server_id: str):
    from src.constants import DATA_DIR
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", str(server_id))[:64] or "server"
    path = os.path.join(DATA_DIR, "logs", f"mcp-{safe}.stderr.log")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    return open(path, "a", encoding="utf-8")


def _format_mcp_connection_error(name: str, command: str = "", args: Optional[List[str]] = None, error: Exception = None) -> str:
    """Return a user-actionable MCP connection error message."""
    args = args or []
    raw_error = str(error) if error else "Unknown error"
    command_line = " ".join([command or "", *args]).strip()
    lower_command = command_line.lower()

    if "@playwright/mcp" in lower_command:
        return (
            f"{raw_error}\n\n"
            "Browser MCP could not start. On fresh installs, cache the Playwright MCP package once before connecting:\n\n"
            "npx -y @playwright/mcp@latest --version\n\n"
            "Then restart Odysseus and reconnect the Browser MCP server."
        )

    return raw_error


# Caps for rendering untrusted MCP tool schemas into the agent prompt (issue #2660).
# MCP servers are third-party/user-added, so field names and parameter counts are
# untrusted input — bound them so an odd or hostile schema cannot distort the prompt.
_MCP_PARAM_MAX = 12   # max params rendered per tool
_MCP_TOKEN_MAX = 40   # max chars per rendered name / type token
_MCP_HINT_MAX = 300   # total-length backstop for the whole hint
_MCP_DISCONNECT_TIMEOUT = 5.0


def _sanitize_schema_token(value: Any, limit: int = _MCP_TOKEN_MAX) -> str:
    """Make an untrusted JSON-Schema token safe to splice into the prompt.

    Replaces control chars / newlines with a space, collapses whitespace, and
    length-caps the result, so a weird field name or type cannot inject newlines
    or run on. Normal short identifiers pass through unchanged.
    """
    text = re.sub(r"[\x00-\x1f\x7f]+", " ", str(value))
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > limit:
        text = text[:limit].rstrip() + "…"
    return text


def _format_mcp_params(input_schema: Any) -> str:
    """Render an MCP tool's JSON-Schema inputs as a compact prompt hint.

    Without this the agent only sees a tool's name + description and has to
    guess its arguments (issue #2509). Produces e.g.
    ` Args (JSON): {"path": string (required), "limit": integer}` — names,
    coarse types, and required-ness, kept short so it stays prompt-friendly.
    Returns "" when there are no parameters.

    MCP servers are third-party, so names/types are sanitized and the parameter
    count + total length are capped (issue #2660); normal schemas are unaffected.
    """
    if not isinstance(input_schema, dict):
        return ""
    props = input_schema.get("properties")
    if not isinstance(props, dict) or not props:
        return ""
    required = set(input_schema.get("required") or [])
    parts = []
    for pname, pinfo in list(props.items())[:_MCP_PARAM_MAX]:
        pinfo = pinfo if isinstance(pinfo, dict) else {}
        ptype = pinfo.get("type") or "any"
        if isinstance(ptype, list):
            ptype = "|".join(str(x) for x in ptype)
        tag = f'"{_sanitize_schema_token(pname)}": {_sanitize_schema_token(ptype)}'
        if pname in required:
            tag += " (required)"
        parts.append(tag)
    extra = len(props) - len(parts)
    if extra > 0:
        parts.append(f"…+{extra} more")
    hint = " Args (JSON): {" + ", ".join(parts) + "}"
    if len(hint) > _MCP_HINT_MAX:
        hint = hint[:_MCP_HINT_MAX - 1].rstrip() + "…"
    return hint


# Tool-name prefixes that denote a read-only/inspection operation. Used to
# classify MCP tools for plan mode when the server provides no readOnlyHint.
# These are PREFIXES, not whole words (matched via str.startswith below), so a
# stem like "summar" intentionally covers "summarise"/"summarize"/"summary".
_MCP_READONLY_VERBS = (
    "list", "get", "read", "search", "fetch", "query", "find", "describe",
    "show", "view", "lookup", "count", "status", "info", "inspect", "summar",
)


def mcp_tool_is_readonly(tool: Dict) -> bool:
    """Classify an MCP tool as safe (non-mutating) for plan mode.

    Prefer the server's own annotations (readOnlyHint / destructiveHint). When
    absent, fall back to a tool-name verb heuristic, and FAIL CLOSED (treat as
    write) for anything that doesn't clearly read — plan mode must not run a
    write tool just because its intent is ambiguous.
    """
    ann = tool.get("annotations")
    # annotations may be a dict or a pydantic model
    read_hint = None
    destructive = None
    if ann is not None:
        if isinstance(ann, dict):
            read_hint = ann.get("readOnlyHint")
            destructive = ann.get("destructiveHint")
        else:
            read_hint = getattr(ann, "readOnlyHint", None)
            destructive = getattr(ann, "destructiveHint", None)
    if read_hint is True:
        return True
    if read_hint is False or destructive is True:
        return False
    # No usable hint — heuristic on the tool name's leading verb.
    name = (tool.get("name") or "").lower()
    return name.startswith(_MCP_READONLY_VERBS)


class McpManager:
    """Manages MCP server connections and tool routing."""

    def __init__(self):
        # server_id -> connection state
        self._connections: Dict[str, Dict[str, Any]] = {}
        # server_id -> list of tool schemas
        self._tools: Dict[str, List[Dict]] = {}
        # server_id -> MCP ClientSession
        self._sessions: Dict[str, Any] = {}
        # A persistent owner task enters, serves, and exits each MCP transport.
        # AnyIO cancel scopes must be exited by the task that entered them.
        self._connect_tasks: Dict[str, asyncio.Task] = {}
        self._connect_stops: Dict[str, asyncio.Event] = {}
        self._connect_ready: Dict[str, asyncio.Future] = {}
        # HTTP/OAuth waits can outlive the bounded connect response while the
        # persistent owner task continues toward authorization/readiness.
        self._connect_waiters: Dict[str, asyncio.Task] = {}
        # Tracking updates to tools/connections for RAG indexing / prompt cache
        self._generation = 0

    @staticmethod
    def _tool_records(tools_result: Any) -> List[Dict[str, Any]]:
        records: List[Dict[str, Any]] = []
        for tool in tools_result.tools:
            records.append({
                "name": tool.name,
                "description": tool.description or "",
                "input_schema": tool.inputSchema if hasattr(tool, "inputSchema") else {},
                "annotations": getattr(tool, "annotations", None),
            })
        return records

    async def _run_connection_owner(
        self,
        server_id: str,
        name: str,
        transport: str,
        ready: asyncio.Future,
        stop: asyncio.Event,
        opener: Callable[[AsyncExitStack], Awaitable[Tuple[Any, List[Dict[str, Any]], Dict[str, Any]]]],
    ) -> None:
        """Own one MCP context stack from entry through exit in one task."""
        stack = AsyncExitStack()
        current = asyncio.current_task()
        connected = False
        failed: Optional[BaseException] = None
        try:
            session, tools, metadata = await opener(stack)
            if self._connect_tasks.get(server_id) is not current:
                return
            self._sessions[server_id] = session
            self._tools[server_id] = tools
            self._connections[server_id] = {
                "status": "connected",
                "name": name,
                "transport": transport,
                "tool_count": len(tools),
                **metadata,
            }
            connected = True
            if not ready.done():
                ready.set_result(True)
            logger.info(
                "MCP server connected: %s (%s) - %d tools via %s",
                name,
                server_id,
                len(tools),
                transport,
            )
            await stop.wait()
        except asyncio.CancelledError as exc:
            failed = exc
            if not ready.done():
                ready.set_result(False)
        except Exception as exc:
            failed = exc
            if not ready.done():
                ready.set_exception(exc)
            elif self._connect_tasks.get(server_id) is current:
                self._connections[server_id] = {
                    "status": "error",
                    "error": str(exc),
                    "name": name,
                    "transport": transport,
                }
                logger.error(
                    "MCP server owner failed after connect: %s (%s): %s",
                    name,
                    server_id,
                    exc,
                )
        finally:
            close_started = asyncio.get_running_loop().time()
            close_result = "cancelled" if isinstance(failed, asyncio.CancelledError) else "ok"
            logger.info(
                "MCP shutdown server=%s name=%s transport=%s event=start",
                server_id,
                name,
                transport,
            )
            try:
                await stack.aclose()
            except Exception as exc:
                close_result = "error"
                logger.warning(
                    "Error closing MCP server %s in owner task: %s",
                    server_id,
                    exc,
                )
                if failed is None:
                    failed = exc
            logger.info(
                "MCP shutdown server=%s name=%s transport=%s event=end duration_s=%.3f result=%s",
                server_id,
                name,
                transport,
                asyncio.get_running_loop().time() - close_started,
                close_result,
            )
            if not ready.done():
                ready.set_result(False)
            if self._connect_tasks.get(server_id) is current:
                self._connect_tasks.pop(server_id, None)
                self._connect_stops.pop(server_id, None)
                self._connect_ready.pop(server_id, None)
                self._sessions.pop(server_id, None)
                self._tools.pop(server_id, None)
                if connected and not stop.is_set() and failed is not None:
                    self._connections[server_id] = {
                        "status": "error",
                        "error": str(failed),
                        "name": name,
                        "transport": transport,
                    }

    async def _start_owned_connection(
        self,
        server_id: str,
        name: str,
        transport: str,
        opener: Callable[[AsyncExitStack], Awaitable[Tuple[Any, List[Dict[str, Any]], Dict[str, Any]]]],
    ) -> bool:
        if server_id in self._connect_tasks or server_id in self._sessions:
            raise RuntimeError(f"MCP server {server_id} already has an active owner")
        loop = asyncio.get_running_loop()
        ready = loop.create_future()
        # Retrieve exceptions even when an HTTP authorization waiter is later
        # cancelled, preventing an unobserved-future warning during shutdown.
        ready.add_done_callback(lambda fut: fut.exception() if not fut.cancelled() else None)
        stop = asyncio.Event()
        task = asyncio.create_task(
            self._run_connection_owner(
                server_id,
                name,
                transport,
                ready,
                stop,
                opener,
            ),
            name=f"mcp-owner:{server_id}",
        )
        self._connect_tasks[server_id] = task
        self._connect_stops[server_id] = stop
        self._connect_ready[server_id] = ready
        try:
            return bool(await asyncio.shield(ready))
        except asyncio.CancelledError:
            if self._connect_tasks.get(server_id) is task and not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            raise

    async def connect_server(
        self,
        server_id: str,
        name: str,
        transport: str,
        command: Optional[str] = None,
        args: Optional[List[str]] = None,
        env: Optional[Dict[str, str]] = None,
        url: Optional[str] = None,
    ) -> bool:
        """Connect to an MCP server via stdio, SSE, or Streamable HTTP transport."""
        try:
            if (
                server_id in self._connect_tasks
                or server_id in self._connect_waiters
                or server_id in self._sessions
            ):
                await self.disconnect_server(server_id)
            if transport == "stdio":
                res = await self._connect_stdio(server_id, name, command, args or [], env or {})
            elif transport == "sse":
                res = await self._connect_sse(server_id, name, url)
            elif transport == "http":
                res = await self._start_http_connect(server_id, name, url)
            else:
                logger.error(f"Unknown MCP transport: {transport}")
                res = False
            if res:
                self._generation += 1
            return res
        except Exception as e:
            logger.error(f"Failed to connect MCP server {name} ({server_id}): {e}")
            error_message = _format_mcp_connection_error(name, command or "", args or [], e)
            self._connections[server_id] = {"status": "error", "error": error_message, "name": name}
            self._generation += 1
            return False

    async def _connect_stdio(self, server_id: str, name: str, command: str, args: List[str], env: Dict[str, str]) -> bool:
        """Connect to an MCP server via stdio transport."""
        async def _open(stack: AsyncExitStack):
            from mcp import ClientSession, StdioServerParameters
            from mcp.client.stdio import stdio_client

            server_params = StdioServerParameters(
                command=command,
                args=args,
                env={**os.environ, **env} if env else None,
            )
            # Child stderr goes to a log file, never the operator's terminal.
            errlog = _mcp_child_stderr_log(server_id)
            stack.callback(errlog.close)
            read_stream, write_stream = await stack.enter_async_context(
                stdio_client(server_params, errlog=errlog)
            )
            session = await stack.enter_async_context(ClientSession(read_stream, write_stream))
            await session.initialize()
            tools = self._tool_records(await session.list_tools())

            identity_hints = []
            for key, value in (env or {}).items():
                key_lower = key.lower()
                if any(part in key_lower for part in ("email_address", "account", "user", "username")):
                    identity_hints.append(value)
            identity = ", ".join(identity_hints) if identity_hints else ""
            return session, tools, {"identity": identity}

        return await self._start_owned_connection(server_id, name, "stdio", _open)

    async def _connect_sse(self, server_id: str, name: str, url: str) -> bool:
        """Connect to an MCP server via SSE transport."""
        async def _open(stack: AsyncExitStack):
            from mcp import ClientSession
            from mcp.client.sse import sse_client

            read_stream, write_stream = await stack.enter_async_context(sse_client(url))
            session = await stack.enter_async_context(ClientSession(read_stream, write_stream))
            await session.initialize()
            tools = self._tool_records(await session.list_tools())
            return session, tools, {}

        return await self._start_owned_connection(server_id, name, "sse", _open)

    async def _start_http_connect(self, server_id: str, name: str, url: str, wait: float = 8.0) -> bool:
        """Begin a Streamable HTTP connect in the background. Returns within
        `wait` seconds: True if it connected (cached-token path), otherwise the
        flow is awaiting browser authorization and status becomes 'needs_auth'."""
        self._connections[server_id] = {"status": "connecting", "name": name, "transport": "http"}
        task = asyncio.create_task(self._connect_http(server_id, name, url))
        self._connect_waiters[server_id] = task

        def _forget_waiter(done: asyncio.Task) -> None:
            if self._connect_waiters.get(server_id) is done:
                self._connect_waiters.pop(server_id, None)

        task.add_done_callback(_forget_waiter)
        done, _ = await asyncio.wait({task}, timeout=wait)
        if task in done:
            try:
                return task.result()
            except Exception as e:
                self._connections[server_id] = {"status": "error", "error": str(e), "name": name}
                return False
        # Still running → either awaiting authorization, or discovery/DCR is
        # still in flight. If _on_redirect already published needs_auth+auth_url,
        # leave it; otherwise mark needs_auth (auth_url filled in once it fires).
        from src.mcp_oauth import pop_auth_url
        cur = self._connections.get(server_id, {})
        if cur.get("status") != "needs_auth":
            self._connections[server_id] = {
                "status": "needs_auth", "name": name, "transport": "http",
                "auth_url": pop_auth_url(server_id),
            }
        return False

    async def _connect_http(self, server_id: str, name: str, url: str) -> bool:
        """Connect to a Streamable HTTP MCP server (with automatic OAuth)."""
        try:
            def _on_redirect(auth_url):
                # Publish needs_auth the moment the URL is known, independent of
                # how long discovery/DCR took (may exceed the bounded start wait).
                self._connections[server_id] = {
                    "status": "needs_auth", "name": name, "transport": "http",
                    "auth_url": auth_url,
                }

            async def _open(stack: AsyncExitStack):
                from mcp import ClientSession
                from mcp.client.streamable_http import streamablehttp_client
                from src.mcp_oauth import build_provider

                provider = build_provider(server_id, url, on_redirect=_on_redirect)
                read_stream, write_stream, _get_session_id = await stack.enter_async_context(
                    streamablehttp_client(url, auth=provider)
                )
                session = await stack.enter_async_context(ClientSession(read_stream, write_stream))
                await session.initialize()
                tools = self._tool_records(await session.list_tools())
                return session, tools, {}

            connected = await self._start_owned_connection(server_id, name, "http", _open)
            if not connected:
                return False
            from src.mcp_oauth import clear_auth_url
            clear_auth_url(server_id)
            # Tools changed (this can complete after connect_server already
            # returned, via the background OAuth flow), so bump the generation
            # to invalidate the tool-prompt cache.
            self._generation += 1
            return True
        except ImportError:
            logger.warning("MCP package not installed. Install with: pip install mcp")
            self._connections[server_id] = {"status": "error", "error": "mcp package not installed", "name": name}
            return False
        except Exception as e:
            logger.error(f"Failed to connect HTTP MCP server {name} ({server_id}): {e}")
            self._connections[server_id] = {"status": "error", "error": str(e), "name": name}
            return False

    async def disconnect_server(self, server_id: str):
        """Disconnect from an MCP server."""
        # Stop any bounded HTTP/OAuth readiness waiter first. Its cancellation
        # asks the persistent owner task to unwind partial startup in that same
        # owner task.
        waiter = self._connect_waiters.pop(server_id, None)
        if waiter is not None and waiter is not asyncio.current_task() and not waiter.done():
            waiter.cancel()
            await asyncio.gather(waiter, return_exceptions=True)
        try:
            from src.mcp_oauth import clear_auth_url
            clear_auth_url(server_id)
        except Exception:
            pass

        task = self._connect_tasks.pop(server_id, None)
        stop = self._connect_stops.pop(server_id, None)
        ready = self._connect_ready.pop(server_id, None)
        if task is not None and task is not asyncio.current_task():
            state = self._connections.get(server_id, {}).get("status")
            if not task.done():
                if state == "connected" and stop is not None:
                    self._connections[server_id]["status"] = "disconnecting"
                    stop.set()
                else:
                    # Partial startup may be blocked inside transport/OAuth
                    # setup and has not reached its stop wait. Cancellation is
                    # delivered to the owner so its own finally closes the stack.
                    task.cancel()
            try:
                await asyncio.wait_for(task, timeout=_MCP_DISCONNECT_TIMEOUT)
            except asyncio.TimeoutError:
                logger.warning(
                    "Timed out closing MCP server %s after %.1fs; cancelling owner task",
                    server_id,
                    _MCP_DISCONNECT_TIMEOUT,
                )
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            except asyncio.CancelledError:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                raise
            except Exception as exc:
                logger.warning("MCP owner task failed while closing %s: %s", server_id, exc)
        elif ready is not None and not ready.done():
            ready.set_result(False)

        self._sessions.pop(server_id, None)
        self._tools.pop(server_id, None)
        self._connections.pop(server_id, None)
        self._generation += 1
        logger.info(f"MCP server disconnected: {server_id}")

    async def disconnect_all(self):
        """Disconnect from all MCP servers."""
        ids = sorted({
            *self._sessions.keys(),
            *self._connect_tasks.keys(),
            *self._connect_waiters.keys(),
            *self._connections.keys(),
        })
        if ids:
            await asyncio.gather(
                *(self.disconnect_server(server_id) for server_id in ids)
            )


    async def connect_all_enabled(self):
        db = SessionLocal()
        try:
            servers = db.query(McpServer).filter(McpServer.is_enabled == True).all()

            tasks = [
                asyncio.create_task(self._connect_with_timeout(srv))
                for srv in servers
            ]

            await asyncio.gather(*tasks)
        finally:
            db.close()


    async def _connect_with_timeout(self, srv):
        args = json.loads(srv.args) if srv.args else []
        env = json.loads(srv.env) if srv.env else {}

        try:
            await asyncio.wait_for(
                self.connect_server(
                    server_id=srv.id,
                    name=srv.name,
                    transport=srv.transport,
                    command=srv.command,
                    args=args,
                    env=env,
                    url=srv.url,
                ),
                timeout=20,
            )
        except asyncio.TimeoutError:
            logger.warning("Timed out connecting to %s", srv.name)
            self._connections[srv.id] = {
                "status": "timeout",
                "error": f"Timed out after 20 seconds",
                "name": srv.name,
            }

    async def call_tool(self, qualified_name: str, arguments: Dict) -> Dict:
        """Call an MCP tool by its qualified name (mcp__{server_id}__{tool_name}).

        Returns a result dict compatible with agent_tools format.
        """
        parts = qualified_name.split("__", 2)
        if len(parts) != 3 or parts[0] != "mcp":
            return {"error": f"Invalid MCP tool name: {qualified_name}", "exit_code": 1}

        server_id = parts[1]
        tool_name = parts[2]

        session = self._sessions.get(server_id)
        if not session:
            return {"error": f"MCP server not connected: {server_id}", "exit_code": 1}

        try:
            result = await self._do_call(session, tool_name, arguments)
        except Exception as e:
            # Auto-reconnect for builtin servers whose subprocess may have died
            if self.is_builtin(server_id):
                logger.warning(f"MCP call failed for {qualified_name}, attempting reconnect: {e}")
                reconnected = await self._reconnect_builtin(server_id)
                if reconnected:
                    session = self._sessions.get(server_id)
                    if session:
                        try:
                            result = await self._do_call(session, tool_name, arguments)
                        except Exception as e2:
                            logger.error(f"MCP tool call failed after reconnect: {qualified_name}: {e2}")
                            return {"error": str(e2), "exit_code": 1}
                    else:
                        return {"error": f"Reconnected but no session for {server_id}", "exit_code": 1}
                else:
                    logger.error(f"MCP reconnect failed for {server_id}")
                    return {"error": f"MCP server crashed and reconnect failed: {server_id}", "exit_code": 1}
            else:
                logger.error(f"MCP tool call failed: {qualified_name}: {e}")
                return {"error": str(e), "exit_code": 1}

        return result

    async def _do_call(self, session, tool_name: str, arguments: Dict) -> Dict:
        """Execute a single MCP tool call and return result dict."""
        result = await session.call_tool(tool_name, arguments)
        output_parts = []
        images = []
        for content in result.content:
            if hasattr(content, 'text'):
                output_parts.append(content.text)
            elif getattr(content, 'type', '') == 'image' and hasattr(content, 'data'):
                # Image content (e.g. Playwright screenshots)
                mime = getattr(content, 'mimeType', 'image/png')
                images.append({"data": content.data, "mimeType": mime})
                output_parts.append(f"[Screenshot captured ({mime})]")
            elif hasattr(content, 'data'):
                output_parts.append(str(content.data))

        output = "\n".join(output_parts)
        is_error = getattr(result, 'isError', False)

        result_dict = {
            "stdout": output if not is_error else "",
            "stderr": output if is_error else "",
            "exit_code": 1 if is_error else 0,
        }
        if images:
            result_dict["images"] = images
        return result_dict

    async def _reconnect_builtin(self, server_id: str) -> bool:
        """Tear down and reconnect a crashed builtin MCP server."""
        import sys
        from src.builtin_mcp import _BUILTIN_SERVERS, builtin_python_env

        if server_id not in _BUILTIN_SERVERS:
            return False

        script_rel, name = _BUILTIN_SERVERS[server_id]
        base_dir = get_app_root()
        script_path = os.path.join(base_dir, script_rel)

        # Clean up old connection
        await self.disconnect_server(server_id)

        try:
            ok = await self.connect_server(
                server_id=server_id,
                name=name,
                transport="stdio",
                command=sys.executable,
                args=[script_path],
                env=builtin_python_env(base_dir),
            )
            if ok:
                logger.info(f"Reconnected builtin MCP server: {name}")
            return ok
        except Exception as e:
            logger.error(f"Failed to reconnect builtin MCP server {name}: {e}")
            return False

    def get_all_openai_schemas(self, disabled_map: Optional[Dict[str, set]] = None) -> List[Dict]:
        """Return all MCP tools in OpenAI function-calling format.

        Tool names are namespaced as mcp__{server_id}__{tool_name}.
        disabled_map: optional {server_id: set_of_disabled_tool_names} to filter out.
        """
        schemas = []
        for server_id, tools in self._tools.items():
            # Skip builtin Python servers — they use the code-block tool format
            # But include NPX-based builtins (like browser) which need function calling
            if self.is_builtin(server_id) and server_id != "builtin_browser":
                continue
            conn = self._connections.get(server_id, {})
            server_name = conn.get("name", server_id)
            disabled = (disabled_map or {}).get(server_id, set())

            identity = conn.get("identity", "")
            label = f"{server_name} ({identity})" if identity else server_name

            for tool in tools:
                if tool["name"] in disabled:
                    continue
                qualified = f"mcp__{server_id}__{tool['name']}"
                schema = {
                    "type": "function",
                    "function": {
                        "name": qualified,
                        "description": f"[MCP:{label}] {tool['description']}",
                        "parameters": tool.get("input_schema", {"type": "object", "properties": {}}),
                    },
                }
                schemas.append(schema)

        return schemas

    def get_all_tools(self, disabled_map: Optional[Dict[str, set]] = None) -> List[Dict]:
        """Return a flat list of all discovered tools with server info."""
        result = []
        for server_id, tools in self._tools.items():
            conn = self._connections.get(server_id, {})
            disabled = (disabled_map or {}).get(server_id, set())
            for tool in tools:
                result.append({
                    "server_id": server_id,
                    "server_name": conn.get("name", server_id),
                    "name": tool["name"],
                    "qualified_name": f"mcp__{server_id}__{tool['name']}",
                    "description": tool.get("description", ""),
                    "input_schema": tool.get("input_schema") or {},
                    "is_disabled": tool["name"] in disabled,
                })
        return result

    def plan_mode_blocked_mcp(self) -> Tuple[Dict[str, Set[str]], Set[str]]:
        """Plan mode: block every MCP tool that isn't clearly read-only.

        Returns (disabled_map, qualified_names):
          - disabled_map: {server_id: {tool_name, ...}} to hide write tools from
            the prompt/schemas (merged into the existing mcp_disabled_map).
          - qualified_names: {"mcp__<server>__<tool>", ...} for runtime rejection
            in execute_tool_block (which matches the qualified name).
        """
        disabled_map: Dict[str, Set[str]] = {}
        qualified: Set[str] = set()
        for server_id, tools in self._tools.items():
            for tool in tools:
                if not mcp_tool_is_readonly(tool):
                    disabled_map.setdefault(server_id, set()).add(tool["name"])
                    qualified.add(f"mcp__{server_id}__{tool['name']}")
        return disabled_map, qualified

    def is_builtin(self, server_id: str) -> bool:
        """Check if a server is a built-in (auto-registered) server."""
        return server_id.startswith("builtin_") or server_id in {
            "image_gen",
            "memory",
            "rag",
            "email",
        }

    def get_server_status(self, server_id: str) -> Dict:
        """Get connection status for a server."""
        return self._connections.get(server_id, {"status": "disconnected"})

    def get_all_statuses(self) -> Dict[str, Dict]:
        """Get connection statuses for all servers."""
        return dict(self._connections)

    _cached_prompt_desc = None
    _cached_prompt_desc_key = None

    def get_tool_descriptions_for_prompt(self, disabled_map: Optional[Dict[str, set]] = None) -> str:
        """Generate text describing MCP tools for the agent system prompt. Cached."""
        cache_key = (
            frozenset((k, frozenset(v)) for k, v in (disabled_map or {}).items()),
            len(self._tools),
            self._generation,
        )
        if self._cached_prompt_desc is not None and self._cached_prompt_desc_key == cache_key:
            return self._cached_prompt_desc
        tools = self.get_all_tools(disabled_map)
        if not tools:
            return ""

        lines = ["\n\nYou also have access to external MCP tool servers. These tools are called via native function calling:"]
        by_server = {}
        for t in tools:
            # Skip builtin Python servers — they're already in the agent prompt
            # But include NPX-based builtins (like browser) which aren't hardcoded
            if self.is_builtin(t["server_id"]) and t["server_id"] != "builtin_browser":
                continue
            if t.get("is_disabled"):
                continue
            sn = t["server_name"]
            if sn not in by_server:
                by_server[sn] = []
            by_server[sn].append(t)

        if not by_server:
            return ""

        for server_name, server_tools in by_server.items():
            # Include identity (e.g. email address) if available
            sid = server_tools[0]["server_id"] if server_tools else ""
            identity = self._connections.get(sid, {}).get("identity", "")
            label = f"{server_name} ({identity})" if identity else server_name
            lines.append(f"\n**{label}:**")
            for t in server_tools:
                # Truncate long descriptions
                desc = t['description'][:120] + '...' if len(t['description']) > 120 else t['description']
                # Include the tool's declared inputs so the model calls it with
                # real argument names instead of guessing from the description
                # alone (issue #2509).
                args_hint = _format_mcp_params(t.get("input_schema"))
                lines.append(f"  - {t['qualified_name']}: {desc}{args_hint}")

        result = "\n".join(lines)
        self._cached_prompt_desc = result
        self._cached_prompt_desc_key = cache_key
        return result
