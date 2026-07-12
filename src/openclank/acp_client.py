"""Hand-rolled ACP client — JSON-RPC over ndJSON stdio."""

import asyncio
import json
import logging
from pathlib import Path
from typing import Any, Callable, Coroutine, Dict, List, Optional

logger = logging.getLogger(__name__)

# Type alias for async callback handlers
Callback = Callable[[dict], Coroutine[Any, Any, dict]]


class ACPClient:
    """Minimal JSON-RPC 2.0 client speaking ndJSON over stdin/stdout to a mimo ACP child."""

    def __init__(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        self._reader = reader
        self._writer = writer
        self._next_id = 1
        self._pending: Dict[int, asyncio.Future] = {}
        self._callbacks: Dict[str, Callback] = {}
        self._session_update_handler: Optional[Callable[[str, dict], Coroutine[Any, Any, None]]] = None
        self._reader_task: Optional[asyncio.Task] = None
        self._callback_tasks: set[asyncio.Task] = set()
        self._closed = False
        self._close_event = asyncio.Event()
        self.agent_info: Optional[dict] = None
        self.initialize_result: dict = {}

    # ── public API ──────────────────────────────────────────────

    def register_callback(self, method: str, handler: Callback) -> None:
        self._callbacks[method] = handler

    def on_session_update(self, handler: Callable[[str, dict], Coroutine[Any, Any, None]]) -> None:
        self._session_update_handler = handler

    async def start_reader(self) -> None:
        self._reader_task = asyncio.create_task(self._read_loop())

    async def initialize(self) -> dict:
        result = await self._send_request("initialize", {
            "protocolVersion": 1,
            "clientCapabilities": {
                "fs": {
                    "readTextFile": True,
                    "writeTextFile": True,
                },
                "terminal": True,
            },
            "clientInfo": {
                "name": "openthesius",
                "version": "0.1.0",
            },
        })
        self.initialize_result = result
        self.agent_info = result.get("agentInfo")
        return result

    async def new_session(self, cwd: str, mcp_servers: Optional[List[dict]] = None) -> dict:
        """Create a new mimo session. Returns the full ACP response dict
        (sessionId, models, configOptions, modes, _meta)."""
        return await self._send_request("session/new", {
            "cwd": cwd,
            "mcpServers": mcp_servers or [],
        })

    async def resume_session(self, session_id: str, cwd: str, mcp_servers: Optional[List[dict]] = None) -> dict:
        return await self._send_request("session/resume", {
            "sessionId": session_id,
            "cwd": cwd,
            "mcpServers": mcp_servers or [],
        })

    async def prompt(
        self,
        session_id: str,
        parts: List[dict],
        metadata: Optional[dict] = None,
    ) -> dict:
        params = {
            "sessionId": session_id,
            "prompt": parts,
        }
        if metadata:
            params["_meta"] = metadata
        return await self._send_request("session/prompt", params)

    async def set_session_config_option(self, session_id: str, config_id: str, value: str) -> dict:
        """Set a session config option (model or mode) in mimo.

        config_id is "model" or "mode". value is e.g. "deepseek/deepseek-v4-pro".
        Returns the full updated config state (models, modes, configOptions).
        """
        return await self._send_request("session/set_config_option", {
            "sessionId": session_id,
            "configId": config_id,
            "value": value,
        })

    async def cancel(self, session_id: str) -> None:
        self._send_notification("session/cancel", {"sessionId": session_id})

    async def release_session(self, session_id: str) -> None:
        await self._send_request(
            "_odysseus/session/release",
            {"sessionId": session_id},
        )

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        # Fail all pending futures
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(TransportError("connection closed"))
        self._pending.clear()
        if self._reader_task and not self._reader_task.done():
            self._reader_task.cancel()
            try:
                await self._reader_task
            except asyncio.CancelledError:
                pass
        callback_tasks = list(self._callback_tasks)
        self._callback_tasks.clear()
        for task in callback_tasks:
            task.cancel()
        if callback_tasks:
            await asyncio.gather(*callback_tasks, return_exceptions=True)
        try:
            self._writer.close()
        except Exception:
            pass
        self._close_event.set()

    @property
    def is_closed(self) -> bool:
        return self._closed

    # ── wire-level ──────────────────────────────────────────────

    def _next_request_id(self) -> int:
        rid = self._next_id
        self._next_id += 1
        return rid

    async def _send_request(self, method: str, params: dict) -> dict:
        rid = self._next_request_id()
        msg = {"jsonrpc": "2.0", "id": rid, "method": method, "params": params}
        fut: asyncio.Future = asyncio.get_event_loop().create_future()
        self._pending[rid] = fut
        await self._write(msg)
        return await fut

    def _send_notification(self, method: str, params: dict) -> None:
        msg = {"jsonrpc": "2.0", "method": method, "params": params}
        asyncio.ensure_future(self._write(msg))

    async def _write(self, obj: dict) -> None:
        line = json.dumps(obj, separators=(",", ":")) + "\n"
        self._writer.write(line.encode())
        await self._writer.drain()

    async def _read_loop(self) -> None:
        try:
            while not self._closed:
                line = await self._reader.readline()
                if not line:
                    # EOF — child crashed or closed
                    logger.warning("ACP reader: EOF (child process closed)")
                    self._closed = True
                    for fut in self._pending.values():
                        if not fut.done():
                            fut.set_exception(TransportError("child process EOF"))
                    self._pending.clear()
                    self._close_event.set()
                    return
                line_str = line.decode(errors="replace").strip()
                if not line_str:
                    continue
                try:
                    msg = json.loads(line_str)
                except json.JSONDecodeError:
                    logger.warning("ACP reader: non-JSON line: %s", line_str[:200])
                    continue
                await self._dispatch(msg)
        except asyncio.CancelledError:
            return
        except Exception as e:
            logger.error("ACP reader error: %s", e, exc_info=True)
            self._closed = True
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_exception(TransportError(f"reader error: {e}"))
            self._pending.clear()
            self._close_event.set()

    async def _dispatch(self, msg: dict) -> None:
        has_id = "id" in msg
        has_method = "method" in msg

        if has_id and not has_method:
            # Response to our request
            rid = msg["id"]
            fut = self._pending.pop(rid, None)
            if fut and not fut.done():
                if "error" in msg:
                    err = msg["error"]
                    fut.set_exception(RPCError(err.get("code", -1), err.get("message", "unknown")))
                else:
                    fut.set_result(msg.get("result", {}))

        elif has_id and has_method:
            # A permission/question callback may wait on a human. Never await
            # it on the only reader loop or responses and updates deadlock.
            task = asyncio.create_task(self._dispatch_callback(msg))
            self._callback_tasks.add(task)
            task.add_done_callback(self._callback_tasks.discard)

        elif not has_id and has_method:
            # Notification from agent
            method = msg["method"]
            params = msg.get("params", {})
            if method == "session/update" and self._session_update_handler:
                update = params.get("update", {})
                session_id = params.get("sessionId", "")
                try:
                    await self._session_update_handler(session_id, update)
                except Exception as exc:
                    logger.error("ACP session/update handler error: %s", exc, exc_info=True)
            else:
                logger.debug("ACP: unhandled notification %s", method)

    async def _dispatch_callback(self, msg: dict) -> None:
        method = msg["method"]
        params = msg.get("params", {})
        rid = msg["id"]
        handler = self._callbacks.get(method)
        if handler is None:
            logger.warning("ACP: no callback for method %s", method)
            await self._write({
                "jsonrpc": "2.0",
                "id": rid,
                "error": {"code": -32601, "message": f"Method not found: {method}"},
            })
            return
        try:
            result = await handler(params)
            await self._write({"jsonrpc": "2.0", "id": rid, "result": result})
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error("ACP callback %s error: %s", method, exc, exc_info=True)
            await self._write({
                "jsonrpc": "2.0",
                "id": rid,
                "error": {"code": -32000, "message": str(exc)},
            })


class TransportError(Exception):
    pass


class RPCError(Exception):
    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code
