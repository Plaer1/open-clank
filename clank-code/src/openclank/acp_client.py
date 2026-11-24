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
        *, private_diagnostics: bool = False,
    ) -> None:
        self._reader = reader
        self._writer = writer
        self._private_diagnostics = private_diagnostics
        self._next_id = 1
        self._pending: Dict[int, asyncio.Future] = {}
        self._callbacks: Dict[str, Callback] = {}
        self._session_update_handler: Optional[Callable[[str, dict], Coroutine[Any, Any, None]]] = None
        self._reader_task: Optional[asyncio.Task] = None
        self._callback_tasks: set[asyncio.Task] = set()
        self._closed = False
        self._cleanup_task: Optional[asyncio.Task] = None
        self._close_event = asyncio.Event()
        self.agent_info: Optional[dict] = None
        self.initialize_result: dict = {}
        self._tool_timing_verifier: Optional[dict] = None

    # ── public API ──────────────────────────────────────────────

    def register_callback(self, method: str, handler: Callback) -> None:
        self._callbacks[method] = handler

    def on_session_update(self, handler: Callable[[str, dict], Coroutine[Any, Any, None]]) -> None:
        self._session_update_handler = handler

    async def start_reader(self) -> None:
        self._reader_task = asyncio.create_task(self._read_loop())

    async def initialize(self, *, gcf_artifact=None) -> dict:
        from core.constants import APP_VERSION
        from src.openclank.managed_protocol import (
            client_capability_offer,
            validate_initialize_result,
        )

        result = await self._send_request("initialize", {
            "protocolVersion": 1,
            "clientCapabilities": {
                "fs": {
                    "readTextFile": True,
                    "writeTextFile": True,
                },
                "terminal": True,
                "_meta": {"openclankManaged": client_capability_offer(private_runtime=self._private_diagnostics),
                          **({'openclankGcfArtifact': dict(gcf_artifact)} if gcf_artifact is not None else {})},
            },
            "clientInfo": {
                "name": "openclank",
                "version": APP_VERSION,
            },
        })
        validate_initialize_result(result, private_runtime=self._private_diagnostics)
        # Optional producer identity belongs only to this exact live transport.
        # A malformed/old peer remains usable for ordinary history.
        self._tool_timing_verifier = None
        metadata = result.get("_meta")
        timing = metadata.get("openclankToolTiming") if isinstance(metadata, dict) else None
        if isinstance(timing, dict) and set(timing) == {"version", "algorithm", "publicKey", "clockID"}:
            try:
                import base64
                import re
                from cryptography.hazmat.primitives.serialization import load_der_public_key
                from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
                raw = base64.b64decode(timing["publicKey"], validate=True)
                if (type(timing["version"]) is int and timing["version"] == 1
                        and timing["algorithm"] == "ed25519"
                        and isinstance(timing["clockID"], str)
                        and re.fullmatch(r"[0-9a-f]{32}", timing["clockID"])
                        and len(raw) == 44 and isinstance(load_der_public_key(raw), Ed25519PublicKey)
                        and base64.b64encode(raw).decode("ascii") == timing["publicKey"]):
                    self._tool_timing_verifier = dict(timing)
            except (TypeError, ValueError, KeyError):
                pass
        self.initialize_result = result
        self.agent_info = result.get("agentInfo")
        return result

    async def reserve_session(self) -> dict:
        """Reserve a provisional engine ``ses_…`` without creating a session row.

        The host persists its projection-first candidate against this exact
        identity, then create-and-publishes it via ``session/new`` with
        ``_meta.provisionalSessionID``. Interruptions before SDK create delete
        nothing and name no orphan.
        """
        result = await self._send_request("_odysseus/session/reserve", {})
        if not isinstance(result, dict):
            raise RuntimeError("engine session reserve returned a non-object result")
        session_id = result.get("sessionID")
        if (
            not isinstance(session_id, str)
            or not session_id.startswith("ses_")
            or session_id != session_id.strip()
        ):
            raise RuntimeError("engine session reserve did not return a provisional ses_ id")
        if result.get("provisional") is not True:
            raise RuntimeError("engine session reserve must confirm provisional identity")
        return {"sessionID": session_id, "provisional": True}

    async def new_session(
        self,
        cwd: str,
        mcp_servers: Optional[List[dict]] = None,
        provisional_session_id: Optional[str] = None,
    ) -> dict:
        """Create a new mimo session. Returns the full ACP response dict
        (sessionId, models, configOptions, modes, _meta).

        When ``provisional_session_id`` is provided it is wired as the
        non-breaking ACP extension seam ``_meta.provisionalSessionID`` so
        create-and-publish keeps one reserved identity.
        """
        params: Dict[str, Any] = {
            "cwd": cwd,
            "mcpServers": mcp_servers or [],
        }
        if provisional_session_id is not None:
            params["_meta"] = {"provisionalSessionID": provisional_session_id}
        return await self._send_request("session/new", params)

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

    async def discard_session(self, session_id: str, cwd: str) -> None:
        """Delete a privately admitted engine session after a failed host CAS.

        Ordinary release only unloads the ACP/MCP scope and deliberately keeps
        resumable engine state. Candidate admission uses this separate,
        destructive extension so an unexposed engine cannot remain durable.
        """
        if not isinstance(cwd, str) or not cwd or not Path(cwd).is_absolute() or str(Path(cwd).resolve(strict=False)) != cwd:
            raise ValueError("private session discard requires a canonical absolute cwd")
        result = await self._send_request(
            "_odysseus/session/discard",
            {"sessionId": session_id, "cwd": cwd},
        )
        if result != {"deleted": True}:
            raise RuntimeError("engine did not confirm private session discard")

    async def managed_engine_call(self, method: str, params: dict, *, queued_pending=None) -> dict:
        """Call one pinned host-to-engine extension with exact wire checks."""

        from src.openclank.managed_protocol import (
            validate_engine_method_request,
            validate_engine_method_result,
        )

        request = validate_engine_method_request(method, params)
        result = await self._send_request(method, request, queued_pending=queued_pending)
        return validate_engine_method_result(method, result)

    async def execute_managed_operation(self, params: dict, *, utility=None) -> dict:
        """Execute one typed, host-authorized model operation in the engine."""

        if not isinstance(params, dict):
            raise TypeError("managed operation parameters must be an object")
        try:
            return await self._send_request("_openclank/operations/v1/execute", params, utility=utility)
        except asyncio.CancelledError:
            cancel = {
                "rootOperationID": params.get("rootOperationID"),
                "idempotencyKey": params.get("idempotencyKey"),
            }
            if all(isinstance(value, str) and value for value in cancel.values()):
                async def notify_cancel():
                    return await asyncio.wait_for(self._send_request("_openclank/operations/v1/cancel", cancel), timeout=0.25)
                cleanup = asyncio.create_task(notify_cancel())
                try:
                    while not cleanup.done():
                        try:
                            await asyncio.shield(cleanup)
                        except asyncio.CancelledError:
                            continue
                    cleanup.result()
                except asyncio.TimeoutError:
                    logger.warning("managed operation cancellation notification timed out")
                except Exception:
                    logger.warning("managed operation cancellation could not reach the engine")
            raise

    async def notify_utility_cancel(self, root: str, key: str) -> None:
        """Best-effort cancellation only; never settles the execute request."""
        try:
            await asyncio.wait_for(self._send_request('_openclank/operations/v1/cancel',
                {'rootOperationID':root, 'idempotencyKey':key}), timeout=0.25)
        except Exception:
            logger.warning('managed operation cancellation notification unavailable')

    async def close(self) -> None:
        self._start_cleanup()
        await asyncio.shield(self._cleanup_task)

    def _start_cleanup(self) -> None:
        self._closed = True
        self._tool_timing_verifier = None
        if self._cleanup_task is None:
            self._cleanup_task = asyncio.create_task(self._finish_close())

    async def _finish_close(self) -> None:
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

    def tool_timing_verifier(self) -> Optional[dict]:
        """Public key/clock for this still-open initialized transport only."""
        return dict(self._tool_timing_verifier) if not self._closed and self._tool_timing_verifier else None

    @property
    def is_closed(self) -> bool:
        return self._closed

    # ── wire-level ──────────────────────────────────────────────

    def _next_request_id(self) -> int:
        rid = self._next_id
        self._next_id += 1
        return rid

    async def _send_request(self, method: str, params: dict, *, utility=None, queued_pending=None) -> dict:
        if queued_pending is not None and (queued_pending.client is not self or queued_pending.method!=method
            or queued_pending.request!=params or queued_pending.issued):
            raise RuntimeError('queued mutation authority changed')
        if utility is not None:
            from src.openclank.operation_journal import canonical_request_hash
            material = {key:params[key] for key in ('operation','routes','input','artifactInputs','options')}
            worker, lease = utility.worker, utility.lease
            if (utility.cancel_requested or not utility.account_current()
                or not await utility.barrier.is_live(utility.owner, utility.admission)
                or params.get('rootOperationID') != utility.root or params.get('idempotencyKey') != utility.idempotency_key
                or canonical_request_hash(material) != utility.request_hash or lease is None
                or getattr(worker, '_proc', None) is not utility.process or getattr(worker, '_bridge', None) is not utility.bridge
                or getattr(utility.bridge, '_managed_provider_generation', None) != utility.holder
                or getattr(worker, 'installed_generation', None) != utility.generation):
                raise RuntimeError('utility authority changed before dispatch')
            lease._pool._assert_owner_lifecycle(utility.owner, utility.epoch)
        rid = self._next_request_id()
        msg = {"jsonrpc": "2.0", "id": rid, "method": method, "params": params}
        fut: asyncio.Future = asyncio.get_event_loop().create_future()
        self._pending[rid] = fut
        if utility is not None:
            fut._openclank_utility = utility
            utility.request_id = rid
        if queued_pending is not None:
            fut._openclank_queued_pending = queued_pending
            queued_pending.request_id = rid
        try:
            if utility is not None and getattr(utility,'owned_send',None) is None:
                utility.issued = True
            if queued_pending is not None: queued_pending.issued = True
            if utility is not None and getattr(utility,'owned_send',None) is not None:
                await utility.owned_send._wait_for_arm(utility,self,msg)
                # The worker was prepared outside source locks. Recheck its
                # actual lifetime after arm, immediately before the frame.
                if (utility.cancel_requested or not utility.account_current()
                    or not await utility.barrier.is_live(utility.owner,utility.admission)
                    or utility.lease is not lease or utility.worker is not worker
                    or getattr(worker,'_proc',None) is not utility.process
                    or getattr(worker,'_bridge',None) is not utility.bridge
                    or getattr(utility.bridge,'_managed_provider_generation',None)!=utility.holder
                    or getattr(worker,'installed_generation',None)!=utility.generation):
                    raise RuntimeError('owned utility authority changed before frame')
                lease._pool._assert_owner_lifecycle(utility.owner,utility.epoch)
                await self._write(msg,owned_utility=utility)
            else:
                await self._write(msg)
            return await fut
        except asyncio.CancelledError:
            pending = self._pending.pop(rid, None)
            if pending is not None and not pending.done():
                pending.cancel()
            raise
        except Exception:
            self._pending.pop(rid, None)
            raise

    def _send_notification(self, method: str, params: dict) -> None:
        msg = {"jsonrpc": "2.0", "method": method, "params": params}
        asyncio.ensure_future(self._write(msg))

    async def _write(self, obj: dict, *, owned_utility=None) -> None:
        line = json.dumps(obj, separators=(",", ":")) + "\n"
        data=line.encode()
        if owned_utility is None:
            self._writer.write(data)
        else:
            # No await occurs between the trusted source check and write.
            owned=owned_utility.owned_send
            owned._before_frame(owned_utility,self,obj)
            try:
                self._writer.write(data)
            except BaseException:
                # A throwing writer may have accepted bytes; never infer unsent.
                owned_utility.issued=True
                owned._frame_unknown(owned_utility,self)
                raise
            owned_utility.issued=True
            owned._frame_issued(owned_utility,self)
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
                    self._start_cleanup()
                    self._close_event.set()
                    return
                line_str = line.decode(errors="replace").strip()
                if not line_str:
                    continue
                try:
                    msg = json.loads(line_str)
                except json.JSONDecodeError:
                    if self._private_diagnostics:
                        logger.warning("private ACP reader received invalid JSON")
                    else:
                        logger.warning("ACP reader: non-JSON line: %s", line_str[:200])
                    continue
                if type(msg) is dict and msg.get("method") == "_openclank/session/v1/git-worktree/phase":
                    params = msg.get("params")
                    event = params.get("event") if type(params) is dict else None
                    kind = event.get("kind") if type(event) is dict else None
                    maximum = 4 * 1024 * 1024 if kind == "begin" else 256 * 1024
                    if len(line) > maximum:
                        rid = msg.get("id")
                        if type(rid) in (str, int):
                            await self._write({"jsonrpc":"2.0", "id":rid, "error":{"code":-32600,"message":"worktree staging frame exceeds limit"}})
                        continue
                await self._dispatch(msg)
        except asyncio.CancelledError:
            return
        except Exception as e:
            if self._private_diagnostics:
                logger.error("private ACP reader failed")
            else:
                logger.error("ACP reader error: %s", e, exc_info=True)
            self._closed = True
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_exception(TransportError("private ACP reader failed" if self._private_diagnostics else f"reader error: {e}"))
            self._pending.clear()
            self._start_cleanup()
            self._close_event.set()

    async def _dispatch(self, msg: dict) -> None:
        has_id = "id" in msg
        has_method = "method" in msg

        if has_id and not has_method:
            # Response to our request
            rid = msg["id"]
            fut = self._pending.get(rid) if type(rid) is int else None
            utility = getattr(fut, '_openclank_utility', None) if fut else None
            queued_pending = getattr(fut, '_openclank_queued_pending', None) if fut else None
            if queued_pending is not None:
                error=msg.get('error')
                valid=(msg.get('jsonrpc')=='2.0' and rid==queued_pending.request_id
                    and set(msg) in ({'jsonrpc','id','result'},{'jsonrpc','id','error'})
                    and ('error' not in msg or (type(error) is dict and type(error.get('code')) is int
                         and isinstance(error.get('message'),str) and {'code','message'}<=set(error)
                         and set(error)<={'code','message','data'})))
                if not valid:
                    self._pending.pop(rid,None)
                    if not fut.done(): fut.set_exception(TransportError('invalid queued mutation response'))
                    return
                queued_pending.response_seen=True
                queued_pending.response_error='error' in msg
            if utility is not None:
                owned=getattr(utility,'owned_send',None)
                if owned is not None and utility.issued is not True:
                    owned._reject_preissue(utility,self)
                    self._pending.pop(rid,None)
                    if not fut.done():
                        fut.set_exception(TransportError('managed response preceded its frame'))
                        fut.exception()  # The disarmed writer will not await this response.
                    return
                error = msg.get('error')
                valid = (msg.get('jsonrpc') == '2.0' and rid == utility.request_id
                    and set(msg) in ({'jsonrpc','id','result'}, {'jsonrpc','id','error'})
                    and ('error' not in msg or (type(error) is dict and type(error.get('code')) is int
                         and isinstance(error.get('message'), str) and {'code','message'} <= set(error)
                         and set(error) <= {'code','message','data'})))
                if not valid:
                    self._pending.pop(rid, None)
                    if not fut.done(): fut.set_exception(TransportError('invalid managed operation response'))
                    return
                utility.response_seen = True
            fut = self._pending.pop(rid, None) if type(rid) is int else None
            if fut and not fut.done():
                if "error" in msg:
                    err = msg["error"]
                    fut.set_exception(
                        RPCError(
                            err.get("code", -1),
                            err.get("message", "unknown"),
                            err.get("data"),
                        )
                    )
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
                    if self._private_diagnostics:
                        logger.error("private ACP update handler failed")
                    else:
                        logger.error("ACP session/update handler error: %s", exc, exc_info=True)
            else:
                if not self._private_diagnostics:
                    logger.debug("ACP: unhandled notification %s", method)

    async def _dispatch_callback(self, msg: dict) -> None:
        method = msg["method"]
        params = msg.get("params", {})
        rid = msg["id"]
        handler = self._callbacks.get(method)
        if handler is None:
            if self._private_diagnostics:
                logger.warning("private ACP callback unavailable")
            else:
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
            if self._private_diagnostics:
                logger.error("private ACP callback failed")
            else:
                logger.error("ACP callback %s error: %s", method, exc, exc_info=True)
            await self._write({
                "jsonrpc": "2.0",
                "id": rid,
                "error": {"code": -32000, "message": str(exc)},
            })


class TransportError(Exception):
    pass


class RPCError(Exception):
    def __init__(self, code: int, message: str, data: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.data = data
        self.session_missing = (
            isinstance(data, dict)
            and data.get("code") == "OPENCLANK_SESSION_MISSING"
        )
