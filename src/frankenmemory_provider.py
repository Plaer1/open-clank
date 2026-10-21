"""Frankenmemory provider — thin adapter over fm-mcp MCP server."""

from __future__ import annotations

import asyncio
import copy
import json
import logging
import os
from contextlib import AsyncExitStack
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

from core.middleware import (
    INTERNAL_TOOL_HEADER,
    INTERNAL_TOOL_OWNER_HEADER,
    INTERNAL_TOOL_WORKSPACE_HEADER,
)
from src.memory_provider import (
    MemoryProvider,
    MemoryRequestRejectedError,
    MemoryRecord,
    MemoryScope,
    MemorySearchHit,
    MemoryTransportError,
)
from src.memory_scope import chat_workspace, memory_owner
from src.openclank.code_performance import (
    CodeStage,
    code_performance_span,
    code_request_service,
    mark_code_request_queued,
    measured_code_query,
)

logger = logging.getLogger(__name__)

_SOURCE_TYPE_MAP = {
    "user": "human",
    "user_created": "human",
    "ai_agent": "ai",
    "agent_explicit": "ai",
    "auto_extracted": "auto_extracted",
}

# Exact contract available through the private owner-scoped broker. Global
# maintenance and code-index tools remain app-only.
BROKER_MEMORY_TOOLS = frozenset(
    {
        "code_query",
        "capture",
        "delete_memory",
        "digest",
        "get_memory",
        "handler_display_label",
        "graph_walk",
        "ingest_authored",
        "list_candidates",
        "list_memories",
        "list_quarantine",
        "mark_conversation_source_removed",
        "memory_explain",
        "memory_export",
        "memory_forget",
        "memory_quality",
        "memory_retention",
        "recall",
        "versioned_detail",
        "versioned_transition",
        "assign_trust",
        "get_trust",
        "resolve_question",
        "record_memory_access",
        "reopen_memory",
        "resolve_memory",
        "review_candidate",
        "search",
        "update_candidate",
        "update_memory",
    }
)


def _render_digest_identity(digest: Dict[str, Any], handler_label: str) -> None:
    """Render %USER% tokens in a digest payload's display fields, in place.

    The digest is a read-only projection rebuilt per call, so rendering its
    output never touches stored claims.  Any rendered field keeps a ``raw_*``
    companion so edit surfaces can round-trip the stored form.
    """
    if not handler_label:
        return

    def render_field(entry: Any, key: str) -> None:
        if not isinstance(entry, dict):
            return
        value = entry.get(key)
        if not isinstance(value, str) or "%" not in value:
            return
        from services.memory.principal_context import render_identity_template

        rendered = render_identity_template(value, handler_label=handler_label)
        if rendered != value:
            entry[key] = rendered
            entry[f"raw_{key}"] = value

    for pinned in digest.get("pinned") or []:
        render_field(pinned, "headline")
        render_field(pinned, "content")
    for question in digest.get("open_questions") or []:
        render_field(question, "content")
    for recent in digest.get("recent") or []:
        render_field(recent, "topic")


def _legacy_overlay_rows(
    db_path: str, owner: str, workspace_id: str, ids: List[str]
) -> List[Dict[str, Any]]:
    """Read curated rows in the exact wire shape ``get_memory`` emits.

    Mirrors the Rust store's curated projection (same filters: owner,
    unarchived, workspace-or-global) so overlay reads batch into one
    indexed query instead of paying one transport round-trip per record.
    A missing table or unreadable store yields no rows — callers treat
    that like the legacy row being absent.
    """
    import sqlite3

    placeholders = ",".join("?" for _ in ids)
    try:
        with sqlite3.connect(db_path, timeout=30) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT id,content,kind,priority,trust_score,confidence_score,"
                "importance_score,scene_name,source,source_type,owner,workspace_id,"
                "session_key,session_id,tags,source_message_ids,timestamps,"
                "created_at,updated_at,archived,last_accessed_at,exempt_from_decay,"
                "exempt_from_dedup,metadata,workspace_path FROM curated "
                "WHERE owner=? AND archived=0 "
                "AND (workspace_id=? OR workspace_id='global') "
                f"AND id IN ({placeholders})",
                (owner, workspace_id, *ids),
            ).fetchall()
    except (OSError, sqlite3.Error):
        return []
    result = []
    for row in rows:
        data = dict(row)
        for key in ("tags", "source_message_ids", "timestamps"):
            try:
                parsed = json.loads(data.get(key) or "[]")
            except (TypeError, ValueError):
                parsed = []
            data[key] = parsed if isinstance(parsed, list) else []
        try:
            metadata = json.loads(data.get("metadata") or "{}")
        except (TypeError, ValueError):
            metadata = {}
        data["metadata"] = metadata if isinstance(metadata, dict) else {}
        for key in ("archived", "exempt_from_decay", "exempt_from_dedup"):
            data[key] = bool(data.get(key))
        result.append(data)
    return result


def _live_legacy_ids(db_path: str, owner: str, workspace_id: str) -> set[str]:
    """IDs of live legacy curated rows for the scope, in one indexed query.

    Replaces the ``list_memories`` transport call used for legacy/v2
    question reconciliation: the transport clamps page sizes, so its
    result was silently a truncated subset; this SQL is the correct
    superset and costs one index scan instead of a full wire page.
    """
    import sqlite3

    with sqlite3.connect(db_path, timeout=30) as conn:
        rows = conn.execute(
            "SELECT id FROM curated WHERE owner=? AND archived=0 "
            "AND (workspace_id=? OR workspace_id='global')",
            (owner, workspace_id),
        ).fetchall()
    return {str(row[0]) for row in rows}


class FrankenmemoryProvider(MemoryProvider):
    """MemoryProvider backed by the fm-mcp Rust engine over MCP stdio."""

    provider_id = "frankenmemory"
    display_name = "Frankenmemory (Rust)"

    def __init__(
        self,
        command: Optional[str] = None,
        workspace_id: str = "",
        env: Optional[Dict[str, str]] = None,
        broker_url: Optional[str] = None,
        broker_token: Optional[str] = None,
    ):
        self._command = command or os.environ.get("FM_MCP_COMMAND", "fm-mcp")
        self._env = {"FM_SCOPE_AUTHORITY": "trusted-caller", **(env or {})}
        # An authenticated process scope is a binding, not a hint. Resolve it
        # before constructing MemoryScope so a child cannot silently use the
        # host's default workspace when FM_WORKSPACE_ID was supplied.
        self._workspace_id = str(
            workspace_id or self._env.get("FM_WORKSPACE_ID") or chat_workspace()
        ).strip()
        # The MCP child and the Python v2 projection must use the same
        # database.  Reading the process-wide constant here would leak test,
        # workspace, or per-instance providers into the live install.
        from src.constants import APP_DB, AUTH_FILE, FM_DB_PATH
        # Canonical FilePolicy uses APP_DB when the operator has no override.
        # Pin that same trusted path in the native child rather than letting
        # lifecycle requests fall back to a different/default authority.
        self._env.setdefault("OPEN_CLANK_AUTHORITY_DB_PATH", os.environ.get("OPEN_CLANK_AUTHORITY_DB_PATH") or APP_DB)
        self._env.setdefault("OPEN_CLANK_AUTHORITY_AUTH_PATH", os.environ.get("OPEN_CLANK_AUTHORITY_AUTH_PATH") or AUTH_FILE)
        self._fm_db_path = str(self._env.get("FM_DB_PATH") or FM_DB_PATH)
        self._broker_url = (
            broker_url
            if broker_url is not None
            else os.environ.get("OPEN_CLANK_MEMORY_BROKER_URL", "")
        ).strip()
        self._broker_token = (
            broker_token
            if broker_token is not None
            else os.environ.get("OPEN_CLANK_MEMORY_BROKER_TOKEN", "")
        ).strip()
        self._broker_client = None
        self._broker_ready = False
        self._broker_init_lock = asyncio.Lock()
        self._broker_owner = memory_owner(self._env.get("FM_OWNER"))
        self._broker_workspace = str(
            self._env.get("FM_WORKSPACE_ID") or self._workspace_id
        ).strip()
        if self._broker_url:
            parsed = urlparse(self._broker_url)
            if (
                parsed.scheme != "http"
                or parsed.hostname not in {"127.0.0.1", "::1", "localhost"}
                or not self._broker_token
            ):
                raise ValueError(
                    "Frankenmemory broker must be authenticated plain HTTP on loopback"
                )
        # The MCP stdio transport pins anyio cancel scopes to the task that
        # entered them. The server initializes in its startup task and calls
        # from request-handler tasks, which breaks those scopes (the infamous
        # empty-str() ClosedResourceError / cancel-scope RuntimeError). So a
        # single owner task holds the session end-to-end and everything else
        # talks to it through a queue.
        self._owner_task: Optional[asyncio.Task] = None
        self._requests: Optional[asyncio.Queue] = None
        self._ready: Optional[asyncio.Future] = None

    def _local_db_path(self) -> Optional[str]:
        """The sqlite path this process may read directly, or None.

        In broker mode the broker owns the store; unless the instance env
        pins ``FM_DB_PATH`` explicitly, opening the process-default path
        could touch a different database than the broker serves. Direct
        v2 sqlite reads/writes must be skipped when this returns None.
        """
        if self._broker_url and not str(self._env.get("FM_DB_PATH") or "").strip():
            return None
        return self._fm_db_path

    async def initialize(self) -> None:
        if self._broker_url:
            async with self._broker_init_lock:
                if self._broker_ready:
                    return
                import httpx

                self._broker_client = httpx.AsyncClient(timeout=45.0)
                try:
                    health_data = await self._broker_request(
                        "memory_quality", {"rebuild_graph_fts": False}
                    )
                    if int(health_data.get("schema_version", 0)) < 10:
                        raise RuntimeError(
                            "frankenmemory broker schema is older than the lifecycle contract"
                        )
                except BaseException:
                    await self._broker_client.aclose()
                    self._broker_client = None
                    raise
                self._broker_ready = True
                logger.info("FrankenmemoryProvider connected to Open Clank memory broker")
                return
        if self._owner_task and not self._owner_task.done():
            await self._ready
            return
        loop = asyncio.get_running_loop()
        self._requests = asyncio.Queue()
        self._ready = loop.create_future()
        self._owner_task = asyncio.create_task(self._owner_loop(), name="frankenmemory-mcp-owner")
        await self._ready

    async def _owner_loop(self) -> None:
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        requests = self._requests
        try:
            async with AsyncExitStack() as stack:
                server_env = {**os.environ, **(self._env or {})}
                # Provider-bearing embedding environment shims were removed
                # by the managed-provider hard cut.  Frankenmemory retains
                # exact/FTS memory when its unfingerprinted vector schema
                # cannot safely consume managed embeddings.
                for name in tuple(server_env):
                    if name.startswith("FM_EMBED_"):
                        server_env.pop(name, None)
                # A database path and its identity are one atomic binding.
                # Callers that deliberately select another database must not
                # inherit the identity of the process-wide production store.
                if "FM_DB_PATH" in self._env and "FM_DB_ID" not in self._env:
                    server_env.pop("FM_DB_ID", None)
                server_params = StdioServerParameters(
                    command=self._command,
                    args=[],
                    env=server_env,
                )
                # fm-mcp stderr goes to a log file, never the operator's
                # terminal — inherited stderr kept printing after server exit.
                from src.constants import DATA_DIR
                _errlog_path = os.path.join(DATA_DIR, "logs", "fm-mcp.stderr.log")
                os.makedirs(os.path.dirname(_errlog_path), exist_ok=True)
                errlog = open(_errlog_path, "a", encoding="utf-8")
                stack.callback(errlog.close)
                read_stream, write_stream = await stack.enter_async_context(stdio_client(server_params, errlog=errlog))
                session = await stack.enter_async_context(ClientSession(read_stream, write_stream))
                await session.initialize()
                health = await session.call_tool("memory_quality", {"rebuild_graph_fts": False})
                health_text = health.content[0].text if health.content else "{}"
                health_data = json.loads(health_text)
                expected_id = server_env.get("FM_DB_ID")
                if expected_id and health_data.get("database_id") != expected_id:
                    raise RuntimeError(
                        "frankenmemory database identity mismatch during provider handshake"
                    )
                if int(health_data.get("schema_version", 0)) < 10:
                    raise RuntimeError("frankenmemory schema is older than the lifecycle contract")
                # Restore global indexing configuration during this normal
                # owner handshake. An old engine may still serve memory; code
                # admission separately requires an acknowledged scheduler.
                try:
                    from src.settings import load_settings
                    from src.openclank.code_index_settings import configuration_request, validate_status, configured, configured_limits
                    saved = load_settings()
                    # Bound only this optional preference acknowledgment. A
                    # stalled/old engine must not prevent ordinary memory
                    # readiness; actual indexing requests have no deadline.
                    restored = await session.call_tool(
                        "code_index_scheduler",
                        {"origin": "app", **configuration_request(saved)},
                        read_timeout_seconds=timedelta(seconds=1.5),
                    )
                    if getattr(restored, "isError", False):
                        raise ValueError("Indexing scheduler unavailable")
                    status = validate_status(json.loads(restored.content[0].text))
                    requested_limits = configured_limits(saved)
                    if ((status["desired"], status["revision"]) != configured(saved)
                            or status["desired_limits"] != requested_limits
                            or status["effective_limits"] != requested_limits):
                        raise ValueError("Indexing scheduler not acknowledged")
                except Exception:
                    logger.warning("Indexing scheduler restore pending; ordinary memory remains available")
                logger.info("FrankenmemoryProvider connected to fm-mcp")
                if not self._ready.done():
                    self._ready.set_result(None)
                while True:
                    item = await requests.get()
                    if item is None:
                        return
                    name, arguments, fut = item
                    if name == "code_index_scheduler" and fut.cancelled():
                        # A timed-out queued observation need not monopolize
                        # the owner. Persisted configuration is reconciled at
                        # the next explicit read/normal index admission.
                        continue
                    try:
                        with code_request_service(fut):
                            # Caller-side readback timeout alone cannot release
                            # this owner loop while an RPC is suspended.
                            options = {"read_timeout_seconds": timedelta(seconds=1.5)} if name == "code_index_scheduler" else {}
                            result = await session.call_tool(name, arguments, **options)
                    except Exception as exc:
                        if not fut.done():
                            fut.set_exception(exc)
                    else:
                        if not fut.done():
                            fut.set_result(result)
        except BaseException as exc:
            if self._ready and not self._ready.done():
                self._ready.set_exception(
                    exc if isinstance(exc, Exception) else ConnectionError(f"fm-mcp owner task died: {exc!r}")
                )
            raise
        finally:
            # Fail anything still queued so no caller awaits forever.
            while requests and not requests.empty():
                pending = requests.get_nowait()
                if pending is not None:
                    _, _, fut = pending
                    if not fut.done():
                        fut.set_exception(ConnectionError("fm-mcp connection closed"))

    async def shutdown(self) -> None:
        self._code_query_closing = True
        # A cancelled caller may still own a start RPC whose exact job token
        # has not arrived. Drain its one-shot cleanup before closing the owner;
        # do not restart a closed provider just to send cancellation.
        for start, binding in list(self.__dict__.get("_code_query_pending_starts", {}).items()):
            self._cancel_code_query_start(start, binding)
        cleanups = list(self.__dict__.get("_code_query_cleanups", ()))
        if cleanups:
            await asyncio.shield(asyncio.gather(*cleanups, return_exceptions=True))
        if self._broker_client is not None:
            await self._broker_client.aclose()
            self._broker_client = None
            self._broker_ready = False
        if self._owner_task:
            if self._requests is not None:
                await self._requests.put(None)
            try:
                await self._owner_task
            except Exception as exc:
                logger.warning("frankenmemory shutdown: owner task ended with %r", exc)
            self._owner_task = None
            self._requests = None
            self._ready = None

    async def _broker_request(
        self, name: str, arguments: Dict[str, Any]
    ) -> Dict[str, Any]:
        if self._broker_client is None:
            raise MemoryTransportError("frankenmemory broker is not initialized")
        try:
            response = await self._broker_client.post(
                self._broker_url,
                headers={
                    INTERNAL_TOOL_HEADER: self._broker_token,
                    INTERNAL_TOOL_OWNER_HEADER: self._broker_owner,
                    INTERNAL_TOOL_WORKSPACE_HEADER: self._broker_workspace,
                    **({"X-OpenClank-Code-Binding": str(self._env.get("OPEN_CLANK_CODE_BINDING_TOKEN") or os.environ.get("OPEN_CLANK_CODE_BINDING_TOKEN") or "")} if name == "code_query" else {}),
                    "Content-Type": "application/json",
                },
                json={"name": name, "arguments": arguments},
            )
            if getattr(response, "status_code", 200) in {
                400,
                401,
                403,
                404,
                405,
                409,
                410,
                415,
                422,
                429,
            }:
                if name == "memory_forget" and response.status_code == 409:
                    try:
                        rejected = response.json()
                    except Exception:
                        rejected = None
                    detail = rejected.get("detail") if isinstance(rejected, dict) else None
                    if isinstance(detail, dict) and detail.get("code") == "code_node_forget_requires_workspace_erase":
                        from src.memory_provider import code_node_forget_refusal_detail
                        refusal = MemoryRequestRejectedError(code="code_node_forget_requires_workspace_erase")
                        public = code_node_forget_refusal_detail(refusal)
                        raise MemoryRequestRejectedError(public["message"], code=public["code"])
                raise MemoryRequestRejectedError(
                    f"frankenmemory broker {name} rejected the request"
                )
            response.raise_for_status()
            payload = response.json()
        except MemoryRequestRejectedError:
            raise
        except Exception as exc:
            raise MemoryTransportError(
                f"frankenmemory broker {name} failed: {exc}"
            ) from exc
        if not isinstance(payload, dict):
            raise MemoryTransportError(
                f"frankenmemory broker {name} returned a non-object response"
            )
        return payload

    async def invoke_tool(
        self, name: str, arguments: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Internal typed transport boundary shared by app, lifetools, and MiMo."""
        from src.memory_versioned import VersionedMemoryError
        from src.frankenmemory_v2 import V2OperationError
        try:
            return await self._invoke_scoped_tool(name, arguments)
        except (VersionedMemoryError, V2OperationError) as exc:
            # Admission and optimistic-concurrency rejections are definite,
            # not transport failures that might encourage a blind retry.
            raise MemoryRequestRejectedError(str(exc)) from exc

    async def _invoke_scoped_tool(
        self, name: str, arguments: Dict[str, Any]
    ) -> Dict[str, Any]:
        if name in {"handler_display_label", "mark_conversation_source_removed", "capture", "versioned_detail", "versioned_transition", "assign_trust", "get_trust", "resolve_question"}:
            # Python-owned current revisions/trust must execute at the broker's
            # pinned authority, never at an unpinned agent's default SQLite.
            from copy import copy
            scoped = copy(self)
            scoped._workspace_id = str(arguments.get("workspace_id") or self._workspace_id)
            owner = arguments.get("owner")
            if name == "handler_display_label":
                return {"label": await scoped.handler_display_label(owner=owner)}
            if name == "mark_conversation_source_removed":
                return await scoped.mark_conversation_source_removed(owner=owner, session_id=str(arguments.get("session_id") or ""))
            if name == "capture":
                return await scoped.capture(
                    str(arguments.get("user_text") or ""), str(arguments.get("assistant_text") or ""),
                    owner=owner, session_id=arguments.get("session_id"),
                    capture_mode=str(arguments.get("capture_mode") or "candidate"),
                    source=str(arguments.get("source") or "odysseus"),
                )
            if name == "versioned_detail":
                return await scoped.versioned_detail(str(arguments.get("memory_id") or ""), owner=owner)
            if name == "versioned_transition":
                changes = {key: arguments[key] for key in (
                    "value", "value_type", "expected_value_type", "kind", "tags",
                    "target_revision", "reason",
                ) if key in arguments}
                return await scoped.versioned_transition(
                    str(arguments.get("memory_id") or ""), str(arguments.get("action") or ""),
                    expected_revision=arguments["expected_revision"], owner=owner, **changes,
                )
            if name == "resolve_question":
                return {"resolved": await scoped.resolve_question(
                    str(arguments.get("memory_id") or ""), owner=owner,
                    resolved_by=arguments.get("resolved_by"), answer=arguments.get("answer"),
                    expected_revision=arguments.get("expected_revision"),
                )}
            trust_args = {key: arguments[key] for key in (
                "subject_kind", "subject_id", "subject_revision", "project_id",
            ) if key in arguments}
            trust_args.update(owner=owner, workspace_id=scoped._workspace_id)
            if name == "assign_trust":
                trust_args.update({key: arguments[key] for key in (
                    "trust", "state", "reason_code", "rationale", "evidence_ids", "expected_assignment_id",
                ) if key in arguments})
                return await scoped.assign_trust(**trust_args)
            return await scoped.get_trust(**trust_args)
        return await self._call_tool(name, arguments)

    async def _call_tool(self, name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        from src.openclank.attachment_admission import admit_references, settle_references
        admitted = []
        if name in {"capture", "update_memory", "versioned_transition", "review_candidate", "resolve_memory", "resolve_question"}:
            admitted = admit_references(arguments.get("owner") or self._env.get("FM_OWNER"), "memery", arguments)
        result = await self._call_tool_transport(name, arguments)
        settle_references(admitted)
        if name == "owner_lifecycle" and arguments.get("action") in {"purge", "reset_commit"} and self._local_db_path():
            from src.openclank.attachment_admission import refresh_domain_references
            from src.openclank.attachment_inventory import memery_inventory
            await asyncio.to_thread(refresh_domain_references, "memery", lambda: memery_inventory(self._local_db_path()))
        return result

    async def _call_tool_transport(self, name: str, arguments: Dict[str, Any], *, _ready_only=False) -> Dict[str, Any]:
        from mcp.shared.exceptions import McpError

        if _ready_only and (self._broker_url or not self._owner_task or self._owner_task.done()
                            or not self._ready or not self._ready.done() or self._ready.cancelled()
                            or self._ready.exception() is not None):
            raise MemoryTransportError("Indexing scheduler owner is unavailable")
        if self._broker_url:
            if not self._broker_ready:
                await self.initialize()
            return await self._broker_request(name, arguments)
        if not self._owner_task or self._owner_task.done():
            await self.initialize()
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        if name in {"code_query", "code_index"}:
            mark_code_request_queued(fut)
        await self._requests.put((name, arguments, fut))
        try:
            result = await fut
        except McpError as exc:
            if exc.error.code == -32602:
                data = exc.error.data
                code = (data.get("code") if isinstance(data, dict) and (data.get("code") == "code_node_forget_requires_workspace_erase" or (name == "code_query" and data.get("code") in {"busy", "denied", "erased", "stale_cursor", "cancelled", "invalid"})) else None)
                raise MemoryRequestRejectedError(
                    f"frankenmemory {name} rejected the request: {exc.error.message}", code=code,
                ) from exc
            raise MemoryTransportError(f"frankenmemory {name} failed: {exc}") from exc
        except Exception as exc:
            raise MemoryTransportError(f"frankenmemory {name} failed: {exc}") from exc
        if getattr(result, "isError", False):
            detail = result.content[0].text if result.content else "unknown MCP error"
            raise MemoryRequestRejectedError(
                f"frankenmemory {name} rejected the request: {detail}"
            )
        text = result.content[0].text if result.content else "{}"
        try:
            if name in {"code_query", "code_index"}:
                with code_performance_span(CodeStage.DECODE):
                    return json.loads(text)
            return json.loads(text)
        except json.JSONDecodeError:
            return {"raw": text}

    async def code_index_scheduler(self, *, action, revision=None, max_threads=None, limits=None, ready_only=True):
        """Private app bridge. Settings readback cannot start/restart an owner."""
        from src.openclank.code_index_settings import integer, MAX_REVISION, validate_status, validate_limits
        if self._broker_url:
            raise MemoryRequestRejectedError("Indexing scheduler is app-only")
        arguments = {"action": action, "origin": "app"}
        if action == "configure":
            arguments.update(revision=integer(revision, "revision", 1, MAX_REVISION),
                             max_threads=integer(max_threads, "max_threads", 1, 32))
            if limits is not None:
                arguments["limits"] = validate_limits(limits)
        elif action != "status" or revision is not None or max_threads is not None or limits is not None:
            raise MemoryRequestRejectedError("Invalid indexing scheduler action")
        return validate_status(await self._call_tool_transport("code_index_scheduler", arguments, _ready_only=ready_only))

    async def _restore_index_scheduler(self):
        from src.settings import load_settings
        from src.openclank.code_index_settings import configuration_request, configured, configured_limits
        if self._broker_url:
            raise MemoryRequestRejectedError("Indexing scheduler is app-only")
        lock = self.__dict__.setdefault("_index_scheduler_lock", asyncio.Lock())
        async with lock:
            # This is a normal index admission, not a Settings GET. Reconnection
            # remains demand-owned, with no automatic relaunch/watchdog.
            await self.initialize()
            saved = load_settings()
            status = await self.code_index_scheduler(**configuration_request(saved), ready_only=True)
            requested_limits = configured_limits(saved)
            if ((status["desired"], status["revision"]) != configured(saved)
                    or status["desired_limits"] != requested_limits
                    or status["effective_limits"] != requested_limits):
                raise MemoryTransportError("Indexing configuration is pending")

    async def code_index_lifecycle(
        self, *, action: str, owner: str, owner_subject_id: str,
        workspace_id: str, location_id: str, workspace_revision: int,
        policy_generation: int, root: Optional[str], subscriber_id: str, origin: str,
    ) -> Dict[str, Any]:
        """App-only operation; native admission queues work in this MCP owner."""
        if self._broker_url:
            raise MemoryRequestRejectedError("Code lifecycle is app-only")
        self._scope(owner)
        if action not in {"ensure", "status", "release", "cancel", "remove", "admit"}:
            raise MemoryRequestRejectedError("Unsupported code lifecycle action")
        if origin not in {"app", "agent"} or not all(
            (owner, owner_subject_id, workspace_id, location_id, subscriber_id)
        ) or (action not in {"release", "cancel", "remove"} and not root):
            raise MemoryRequestRejectedError("Code lifecycle requires a validated Workspace")
        if action in {"ensure", "admit"}:
            await self._restore_index_scheduler()
        return await self._call_tool("code_index", {
            "action": action, "owner": owner, "owner_subject_id": owner_subject_id,
            "workspace_id": workspace_id, "location_id": location_id,
            "workspace_revision": workspace_revision, "policy_generation": policy_generation,
            **({"root": root} if root is not None else {}),
            "subscriber_id": subscriber_id, "origin": origin,
        })

    def _own_code_query_cleanup(self, operation):
        """Request cleanup ownership survives a second caller cancellation."""
        tasks = self.__dict__.setdefault("_code_query_cleanups", set())
        task = asyncio.create_task(operation)
        tasks.add(task)
        def finished(done):
            tasks.discard(done)
            if not done.cancelled():
                done.exception()  # Retrieve errors even if both waiters left.
        task.add_done_callback(finished)
        return task

    async def _cancel_code_query_token(self, binding, token):
        if not self._owner_task or self._owner_task.done():
            return  # A closed native owner cannot retain a running job.
        try:
            await self._call_tool("code_query", {"action": "cancel", "binding": binding, "token": token})
        except Exception:
            logger.debug("Code query cancellation owner unavailable")

    def _cancel_code_query_start(self, start, binding):
        known = self.__dict__.setdefault("_code_query_start_cleanups", {})
        if start in known:
            return known[start]
        async def after_ack():
            try:
                ticket = await start
                token = ticket.get("token")
                if isinstance(token, str) and token:
                    await self._cancel_code_query_token(binding, token)
            except Exception:
                logger.debug("Code query start owner unavailable during cleanup")
        cleanup = self._own_code_query_cleanup(after_ack())
        known[start] = cleanup
        cleanup.add_done_callback(lambda done: known.pop(start, None))
        return cleanup

    @measured_code_query
    async def code_query(self, *, binding: Dict[str, Any], query: Optional[Dict[str, Any]], source_claim=None, _read_fence_only=False, _selection_policy_fence=False, _visual_request=None) -> Dict[str, Any]:
        """Private app-owned read job. Polls never admit/rebuild a workspace.

        Each RPC returns promptly to the single owner queue; CPU/parse/source
        work is performed by the existing native process outside that queue.
        """
        if self._broker_url:
            raise MemoryRequestRejectedError("Trusted code binding is app-only")
        visual_mode = None
        if _visual_request is not None:
            if binding.get("origin") != "app":
                raise MemoryRequestRejectedError("Code visualization is app-only", code="denied")
            if (query is not None or source_claim is not None or _read_fence_only or _selection_policy_fence
                    or not isinstance(_visual_request, dict) or set(_visual_request) != {"mode", "request"}
                    or _visual_request.get("mode") not in ("map", "selection")
                    or not isinstance(_visual_request.get("request"), dict)):
                raise MemoryRequestRejectedError("Invalid visual request mode", code="invalid")
            visual_mode = _visual_request["mode"]
        if self.__dict__.get("_code_query_closing", False):
            return {"error": {"code": "cancelled", "message": "Code query owner closing"}, "cost": {"native_rpc_calls_completed": 0}}
        self._scope(binding.get("owner"))
        arguments = {"action": "start", "binding": dict(binding)}
        if visual_mode is not None:
            arguments["visual_request"] = copy.deepcopy(_visual_request)
        else:
            arguments["query"] = dict(query)
        if source_claim is not None:
            arguments["source_claim"] = source_claim
        if _selection_policy_fence and not _read_fence_only:
            raise MemoryRequestRejectedError("Selection policy fence requires read fence mode", code="invalid")
        if _read_fence_only:
            arguments["read_fence_only"] = True
        if _selection_policy_fence:
            arguments["selection_policy_fence"] = True
        start = asyncio.create_task(self._call_tool("code_query", arguments))
        pending = self.__dict__.setdefault("_code_query_pending_starts", {})
        pending[start] = dict(binding)
        try:
            with code_performance_span(CodeStage.START):
                ticket = await asyncio.shield(start)
        except asyncio.CancelledError:
            cleanup = self._cancel_code_query_start(start, dict(binding))
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                pass  # Still provider-owned and drained by shutdown.
            raise
        finally:
            pending.pop(start, None)
        completed_rpcs = 1
        token = ticket.get("token")
        if not isinstance(token, str) or not token:
            raise MemoryTransportError("Invalid code query job receipt")
        if self.__dict__.get("_code_query_closing", False):
            # Shutdown owns the same pending start. Do not race its sentinel
            # with a poll or implicitly initialize a replacement native owner.
            await asyncio.shield(self._cancel_code_query_start(start, dict(binding)))
            return {"error": {"code": "cancelled", "message": "Code query owner closing"}, "cost": {"native_rpc_calls_completed": completed_rpcs}}
        observation_delay = 0.001
        try:
            while True:
                if self.__dict__.get("_code_query_closing", False):
                    await asyncio.shield(self._own_code_query_cleanup(self._cancel_code_query_token(dict(binding), token)))
                    return {"error": {"code": "cancelled", "message": "Code query owner closing"}, "cost": {"native_rpc_calls_completed": completed_rpcs}}
                with code_performance_span(CodeStage.POLL):
                    reply = await self._call_tool("code_query", {"action": "poll", "binding": binding, "token": token})
                completed_rpcs += 1
                state = reply.get("state")
                if state == "completed" and isinstance(reply.get("result"), dict):
                    result = reply["result"]
                    if visual_mode is not None:
                        if (set(result) != {"mode", "result"} or result.get("mode") != visual_mode
                                or not isinstance(result.get("result"), dict)
                                or not isinstance(result["result"].get("cost"), dict)):
                            raise MemoryTransportError("Invalid code visual job result")
                        result = result["result"]
                    result.setdefault("cost", {})["native_rpc_calls_completed"] = completed_rpcs
                    return result
                if state in {"failed", "cancelled"}:
                    return {"error": reply.get("error") or {"code": "unavailable", "message": "Code query unavailable"}, "cost": {"native_rpc_calls_completed": completed_rpcs}}
                if state != "running":
                    raise MemoryTransportError("Invalid code query job state")
                # This is asynchronous observation, not a job timeout/lease or
                # a worker restart mechanism. Total work duration is unknown.
                with code_performance_span(CodeStage.OBSERVATION):
                    await asyncio.sleep(observation_delay)
                # Keep short queries responsive, but pending reads need not
                # consume forty owner-queue RPCs per second indefinitely.
                # Sleep remains cancellable; this cap is not a job deadline.
                observation_delay = min(observation_delay * 2, 0.100)
        except asyncio.CancelledError:
            cleanup = self._own_code_query_cleanup(self._cancel_code_query_token(dict(binding), token))
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                pass  # The one-shot cleanup is not cancelled by its waiter.
            raise

    async def code_visual(self, *, binding, visual_request):
        """App map/selection uses the existing cancellable native read owner.

        Full request/result schema and final workspace validation belong to
        the trusted visual accessor and native producer, not an agent bridge.
        """
        if visual_request is None:
            raise MemoryRequestRejectedError("Visual request required", code="invalid")
        return await self.code_query(binding=binding, query=None, _visual_request=visual_request)

    async def code_read_fence(self, *, binding, expected_receipt=None, selection_policy=False):
        result = await self.code_query(binding=binding, query={"operation": "capabilities", "fields": [], "channels": [], "expected_receipt": expected_receipt}, _read_fence_only=True, _selection_policy_fence=selection_policy)
        if isinstance(result.get("private_read_fence"), str):
            return {"fence": result["private_read_fence"], "cost": result.get("cost", {})}
        return result

    async def code_query_session(self, *, query: Dict[str, Any], session_id: str, authority_workspace_id: str) -> Dict[str, Any]:
        """Friendly bridge lane: trusted process binding, never public roots."""
        if not self._broker_url:
            raise MemoryRequestRejectedError("Code session broker is unavailable")
        process_session = str(self._env.get("SESSION_ID") or os.environ.get("SESSION_ID") or "")
        process_workspace = str(self._env.get("OPEN_CLANK_AUTHORITY_WORKSPACE_ID") or os.environ.get("OPEN_CLANK_AUTHORITY_WORKSPACE_ID") or "")
        token = str(self._env.get("OPEN_CLANK_CODE_BINDING_TOKEN") or os.environ.get("OPEN_CLANK_CODE_BINDING_TOKEN") or "")
        revision = str(self._env.get("OPEN_CLANK_SESSION_BINDING_REVISION") or os.environ.get("OPEN_CLANK_SESSION_BINDING_REVISION") or "")
        if not token or not process_session or session_id != process_session or authority_workspace_id != process_workspace or not revision.isdigit():
            raise MemoryRequestRejectedError("Code chat binding is unavailable")
        return await self._broker_request("code_query", {"query": query, "session_id": process_session, "binding_revision": int(revision)})

    def _scope(self, owner: Optional[str], session_id: Optional[str] = None) -> MemoryScope:
        bound_owner = str(self._env.get("FM_OWNER") or "").strip()
        requested_owner = str(owner or "").strip()
        if bound_owner and requested_owner and requested_owner != bound_owner:
            raise MemoryRequestRejectedError(
                "request scope conflicts with authenticated process scope"
            )
        return MemoryScope(
            owner=memory_owner(bound_owner or requested_owner),
            workspace_id=self._workspace_id,
            workspace_path=self._workspace_id,
            session_id=session_id,
            session_key=session_id,
        )

    @staticmethod
    def _record(data: Dict[str, Any]) -> MemoryRecord:
        metadata = data.get("metadata") if isinstance(data.get("metadata"), dict) else {}
        updated_at = data.get("updated_at") or data.get("created_at")
        timestamp = 0
        if isinstance(updated_at, str):
            try:
                timestamp = int(datetime.fromisoformat(updated_at.replace("Z", "+00:00")).timestamp())
            except ValueError:
                pass
        def _score(key: str) -> Optional[float]:
            value = data.get(key)
            try:
                return float(value) if value is not None else None
            except (TypeError, ValueError):
                return None

        wire_kind = str(data.get("kind") or "fact")
        is_open_question = wire_kind == "open_question"
        return MemoryRecord(
            id=data.get("id", ""),
            text=data.get("content", data.get("text", "")),
            timestamp=timestamp,
            category=metadata.get("category", "unknown" if is_open_question else data.get("kind", "episodic")),
            source=data.get("source", ""),
            owner=data.get("owner"),
            session_id=data.get("session_id"),
            metadata=metadata,
            pinned=bool(metadata.get("pinned", False) or data.get("source_label") == "pinned"),
            workspace_id=data.get("workspace_id"),
            created_at=data.get("created_at"),
            updated_at=data.get("updated_at"),
            uses=int(metadata.get("uses", 0) or 0),
            kind="unknown" if is_open_question else wire_kind,
            # Frankenmemory is an external wire boundary. Missing provenance
            # must not silently turn an engine record into user-authored truth.
            source_type=str(data.get("source_type") or "auto_extracted"),
            priority=int(data["priority"]) if isinstance(data.get("priority"), (int, float)) else None,
            trust_score=_score("trust_score"),
            confidence_score=_score("confidence_score"),
            importance_score=_score("importance_score"),
            scene_name=data.get("scene_name"),
            tags=[str(t) for t in data.get("tags") or [] if isinstance(t, (str, int))],
            source_message_ids=[str(m) for m in data.get("source_message_ids") or [] if isinstance(m, (str, int))],
            workspace_path=data.get("workspace_path"),
            archived=bool(data.get("archived", False)),
            exempt_from_decay=bool(data.get("exempt_from_decay", False)),
            exempt_from_dedup=bool(data.get("exempt_from_dedup", False)),
            last_accessed_at=data.get("last_accessed_at"),
            source_uri=data.get("source_uri") or metadata.get("source_uri"),
            source_revision=data.get("source_revision", metadata.get("source_revision")),
            content_hash=data.get("content_hash") or metadata.get("content_hash"),
            recall_explanation=(
                dict(metadata.get("recall_explanation"))
                if isinstance(metadata.get("recall_explanation"), dict)
                else {}
            ),
            provenance_conflict=bool(
                data.get("provenance_conflict", metadata.get("provenance_conflict", False))
            ),
            trust=(
                dict(data.get("trust"))
                if isinstance(data.get("trust"), dict)
                else (
                    dict(metadata.get("trust"))
                    if isinstance(metadata.get("trust"), dict)
                    else None
                )
            ),
        )

    async def _legacy_overlay_map(
        self, ids: List[str], *, owner: Optional[str]
    ) -> Dict[str, MemoryRecord]:
        """Batch-read legacy overlay rows in one local sqlite query.

        The legacy wire contract still owns pinned/uses/archived counters;
        one MCP ``get_memory`` round-trip per record made v2 list/recall
        reads O(n) on the transport. Returns {} when no local store is
        available (broker mode without a pinned FM_DB_PATH); callers then
        keep the per-record transport path.
        """
        local_db = self._local_db_path()
        wanted = [str(value) for value in dict.fromkeys(ids) if value]
        if local_db is None or not wanted:
            return {}
        scope = self._scope(owner)
        rows = await asyncio.to_thread(
            _legacy_overlay_rows, local_db, scope.owner, scope.workspace_id, wanted
        )
        return {str(row["id"]): self._record(row) for row in rows}

    async def _overlay_legacy_fields(
        self,
        record: Optional[MemoryRecord],
        *,
        owner: Optional[str],
        legacy_map: Optional[Dict[str, MemoryRecord]] = None,
    ) -> Optional[MemoryRecord]:
        """Overlay counters still owned by the legacy wire contract."""
        if record is None:
            return None
        if legacy_map is not None:
            legacy = legacy_map.get(str(record.id))
        elif self._local_db_path() is not None:
            legacy = (await self._legacy_overlay_map([record.id], owner=owner)).get(
                str(record.id)
            )
        else:
            legacy = await self._get_legacy_current(record.id, owner=owner)
        if legacy is None:
            return record
        record.pinned = legacy.pinned
        record.uses = legacy.uses
        record.last_accessed_at = legacy.last_accessed_at
        record.archived = legacy.archived
        record.metadata = {**record.metadata, **legacy.metadata}
        return record

    async def capture(
        self,
        user_text: str,
        assistant_text: str,
        *,
        owner: Optional[str] = None,
        session_id: Optional[str] = None,
        source: str = "odysseus",
        capture_mode: str = "candidate",
    ) -> Dict[str, Any]:
        """Automatic per-turn capture through the candidates-tier pipeline.
        ``review_only`` stages eligible content without auto-admission. The
        engine derives a stable event id from the scope and texts, so retries
        deduplicate."""
        scope = self._scope(owner, session_id)
        args: Dict[str, Any] = {
            "user_text": user_text or "",
            "assistant_text": assistant_text or "",
            "capture_mode": capture_mode,
            "workspace_id": scope.workspace_id,
            "workspace_path": scope.workspace_path,
            "owner": scope.owner,
            "source": source,
        }
        if session_id:
            args["session_id"] = session_id
            args["session_key"] = session_id
        result = await self._call_tool("capture", args)
        # Keep the v2 job envelope in the same install.  The legacy engine
        # still owns admission during compatibility rollout, but automatic
        # capture must not disappear from the versioned pipeline.
        local_db = self._local_db_path()
        if local_db is None:
            return result
        try:
            from src.frankenmemory_v2 import mirror_capture_job
            mirror_capture_job(
                owner=scope.owner,
                session_id=session_id,
                user_text=user_text,
                assistant_text=assistant_text,
                workspace_id=scope.workspace_id,
                db_path=local_db,
            )
            # Auto-capture may admit one or more curated records.  Refresh the
            # exact legacy rows before mirroring so a preferred v2 read can
            # never lag a successful turn capture.
            for record_id in result.get("record_ids") or []:
                if str(record_id).startswith("m_"):
                    current = await self._get_legacy_current(record_id, owner=scope.owner)
                    if current is not None:
                        from src.frankenmemory_v2 import mirror_record
                        mirror_record(
                            current,
                            owner=scope.owner,
                            workspace_id=scope.workspace_id,
                            db_path=local_db,
                        )
        except Exception:
            logger.debug("v2 capture mirror unavailable", exc_info=True)
        return result

    async def remember(
        self,
        text: str,
        *,
        owner: Optional[str] = None,
        session_id: Optional[str] = None,
        category: str = "fact",
        source: str = "user",
        metadata: Optional[Dict[str, Any]] = None,
        workspace_id: Optional[str] = None,
        capture_mode: str = "manual",
        source_type: Optional[str] = None,
    ) -> MemoryRecord:
        scope = self._scope(owner, session_id)
        local_db = self._local_db_path()
        if local_db is not None:
            try:
                from services.memory.principal_context import ensure_principal_context_cached
                await asyncio.to_thread(
                    ensure_principal_context_cached,
                    owner=scope.owner,
                    workspace_id=workspace_id or scope.workspace_id,
                    db_path=local_db,
                )
            except Exception:
                logger.debug("v2 principal bootstrap unavailable", exc_info=True)
        resolved_source_type = str(
            source_type or _SOURCE_TYPE_MAP.get(source, "auto_extracted")
        ).strip().lower()
        if resolved_source_type not in {
            "human",
            "ai",
            "auto_extracted",
            "procedural",
        }:
            resolved_source_type = "auto_extracted"
        args: Dict[str, Any] = {
            "content": text,
            "capture_mode": capture_mode,
            "workspace_id": workspace_id or scope.workspace_id,
            "workspace_path": scope.workspace_path,
            "owner": scope.owner,
            "source": source,
            "category": category,
            "source_type": resolved_source_type,
        }
        if session_id:
            args["session_id"] = session_id
            args["session_key"] = session_id
        if metadata:
            args["metadata"] = metadata

        result = await self._call_tool("capture", args)
        record_ids = result.get("record_ids") or []
        if not record_ids:
            raise RuntimeError("frankenmemory capture returned no durable record id")
        if capture_mode == "review_only":
            record_id = next(
                (rid for rid in record_ids if str(rid).startswith("candidate_")),
                None,
            )
        else:
            record_id = next(
                (rid for rid in record_ids if str(rid).startswith("m_")),
                None,
            )
        if not record_id:
            raise RuntimeError(
                "frankenmemory capture did not admit the requested memory"
            )
        record_metadata = dict(metadata or {})
        if capture_mode == "review_only":
            record_metadata["pending_review"] = True
        record = MemoryRecord(
            id=record_id,
            text=text,
            category=category,
            kind=category,
            source=source,
            source_type=resolved_source_type,
            owner=scope.owner,
            session_id=session_id,
            metadata=record_metadata,
            pinned=bool(record_metadata.get("pinned", False)),
            workspace_id=workspace_id or scope.workspace_id,
        )
        if capture_mode != "review_only" and local_db is not None:
            try:
                from src.frankenmemory_v2 import mirror_record
                mirror_record(
                    record,
                    owner=scope.owner,
                    workspace_id=workspace_id or scope.workspace_id,
                    db_path=local_db,
                )
            except Exception:
                logger.debug("v2 memory mirror unavailable", exc_info=True)
        return record

    async def recall(
        self,
        query: str,
        *,
        owner: Optional[str] = None,
        top_k: int = 5,
    ) -> List[MemorySearchHit]:
        scope = self._scope(owner)
        local_db = self._local_db_path()
        if local_db is not None:
            try:
                from services.memory.principal_context import ensure_principal_context_cached
                await asyncio.to_thread(
                    ensure_principal_context_cached,
                    owner=scope.owner,
                    workspace_id=scope.workspace_id,
                    db_path=local_db,
                )
            except Exception:
                logger.debug("v2 principal bootstrap unavailable", exc_info=True)
        v2_read_mode = os.environ.get("FM_V2_READ_MODE", "preferred").strip().lower()
        if v2_read_mode in {"preferred", "strict"} and local_db is not None:
            from src.frankenmemory_v2 import search_current_records
            rows = await asyncio.to_thread(
                search_current_records,
                query,
                owner=scope.owner,
                workspace_id=scope.workspace_id,
                top_k=max(1, int(top_k)) * (2 if v2_read_mode == "preferred" else 1),
                require_legacy_presence=True,
                db_path=local_db,
                )
            if rows or v2_read_mode == "strict":
                # Preferred mode is a shadow-read rollout, not a lossy
                # replacement: records written before the v2 mirror (or by
                # an older process) must remain recallable.  The v2 row wins
                # for an ID it owns; legacy-only rows fill the remainder.
                hits = []
                by_id: dict[str, MemorySearchHit] = {}
                legacy_map = (
                    await self._legacy_overlay_map(
                        [str(row.get("id")) for row in rows], owner=owner
                    )
                    if local_db is not None
                    else None
                )
                for row in rows:
                    record = await self._overlay_legacy_fields(
                        self._record(row), owner=owner, legacy_map=legacy_map
                    )
                    hit = MemorySearchHit(
                        memory=record,
                        provider_id=self.provider_id,
                        score=row.get("recall_score"),
                    )
                    hits.append(hit)
                    by_id[str(record.id)] = hit
                if v2_read_mode == "strict":
                    return hits
                merged_top_k = max(1, int(top_k))
                legacy_result = await self._call_tool("recall", {
                    "query": query,
                    "top_k": merged_top_k * 2,
                    "tier": "curated",
                    "workspace_id": scope.workspace_id,
                    "owner": scope.owner,
                })
                ranks = {str(hit.memory.id): 1.0 / (60 + rank)
                         for rank, hit in enumerate(hits, 1)}
                seen_legacy: set[str] = set()
                for rank, mem in enumerate(legacy_result.get("memories", []), 1):
                    record = self._record(mem)
                    record_id = str(record.id)
                    if record_id in seen_legacy:
                        continue
                    seen_legacy.add(record_id)
                    ranks[record_id] = ranks.get(record_id, 0.0) + 1.0 / (60 + rank)
                    if record_id not in by_id:
                        hit = MemorySearchHit(memory=record, provider_id=self.provider_id)
                        hits.append(hit)
                        by_id[record_id] = hit
                for hit in hits:
                    hit.score = ranks[str(hit.memory.id)]
                hits.sort(key=lambda hit: (-hit.score, str(hit.memory.id)))
                return hits[:merged_top_k]
        args: Dict[str, Any] = {
            "query": query,
            "top_k": top_k,
            "tier": "curated",
            "workspace_id": scope.workspace_id,
            "owner": scope.owner,
        }
        result = await self._call_tool("recall", args)
        hits: List[MemorySearchHit] = []
        for mem in result.get("memories", []):
            record = self._record(mem)
            hits.append(MemorySearchHit(
                memory=record,
                provider_id=self.provider_id,
                score=mem.get("score"),
            ))
        return hits

    async def list_memories(
        self,
        *,
        owner: Optional[str] = None,
        limit: int = 100,
    ) -> List[MemoryRecord]:
        records: List[MemoryRecord] = []
        cursor: Optional[str] = None
        while len(records) < limit:
            page, cursor = await self.list_page(
                owner=owner,
                limit=min(1000, limit - len(records)),
                cursor=cursor,
            )
            records.extend(page)
            if cursor is None:
                break
        return records

    async def list_page(
        self,
        *,
        owner: Optional[str] = None,
        limit: int = 100,
        cursor: Optional[str] = None,
    ) -> tuple[List[MemoryRecord], Optional[str]]:
        scope = self._scope(owner)
        local_db = self._local_db_path()
        if local_db is not None:
            try:
                from services.memory.principal_context import ensure_principal_context_cached
                await asyncio.to_thread(
                    ensure_principal_context_cached,
                    owner=scope.owner,
                    workspace_id=scope.workspace_id,
                    db_path=local_db,
                )
            except Exception:
                logger.debug("v2 principal bootstrap unavailable", exc_info=True)
        # Transitional cutover: v2 is preferred only when explicitly enabled,
        # and rows must still have a live legacy source until all writers are
        # retired. This keeps stale shadow/test rows out of user reads while
        # letting production traffic exercise the canonical projection.
        v2_read_mode = os.environ.get("FM_V2_READ_MODE", "preferred").strip().lower()
        if v2_read_mode in {"preferred", "strict"} and local_db is not None:
            v2_records = await self.list_v2_records(
                owner=owner,
                limit=limit,
                require_legacy_presence=True,
            )
            if v2_read_mode == "strict":
                return v2_records, None
            # Do not hide legacy-only records while the mirror is still
            # catching up.  The legacy cursor remains authoritative for page
            # boundaries; v2 rows replace matching IDs in that page.
            legacy_result = await self._call_tool(
                "list_memories",
                {
                    "owner": scope.owner,
                    "workspace_id": scope.workspace_id,
                    "limit": limit,
                    "cursor": cursor,
                },
            )
            legacy_records = [
                self._record(record)
                for record in legacy_result.get("records", [])
            ]
            preferred = {str(record.id): record for record in v2_records}
            merged = [preferred.get(str(record.id), record) for record in legacy_records]
            if cursor is None:
                # A resolved question archives its compatibility row by
                # design, so the legacy-cursor-driven merge would drop it.
                # Surface resolved answers once, on the first page.
                merged_ids = {str(record.id) for record in merged}
                for record in v2_records:
                    if (
                        str(record.id) not in merged_ids
                        and getattr(record, "kind", "") == "unknown"
                        and record.metadata.get("v2_status") == "active"
                    ):
                        merged.append(record)
            return merged, legacy_result.get("next_cursor")
        result = await self._call_tool(
            "list_memories",
            {
                "owner": scope.owner,
                "workspace_id": scope.workspace_id,
                "limit": limit,
                "cursor": cursor,
            },
        )
        return [self._record(record) for record in result.get("records", [])], result.get("next_cursor")

    async def list_v2_records(
        self,
        *,
        owner: Optional[str] = None,
        project_id: Optional[str] = None,
        limit: int = 1000,
        require_legacy_presence: bool = False,
    ) -> List[MemoryRecord]:
        """Read the typed v2 projection for parity/cutover checks.

        This method is intentionally explicit while legacy MCP reads remain
        the compatibility authority. It gives Brain/Agent integration tests a
        real v2-native path without silently changing existing user results.
        """
        scope = self._scope(owner)
        local_db = self._local_db_path()
        if local_db is None:
            # Broker mode without a pinned FM_DB_PATH: the broker owns the
            # store and direct sqlite would read the wrong database.
            return []
        from src.frankenmemory_v2 import list_current_records
        rows = await asyncio.to_thread(
            list_current_records,
            owner=scope.owner,
            workspace_id=scope.workspace_id,
            project_id=project_id,
            limit=limit,
            require_legacy_presence=require_legacy_presence,
            db_path=local_db,
        )
        records = []
        legacy_map = await self._legacy_overlay_map(
            [str(row.get("id")) for row in rows], owner=owner
        )
        for row in rows:
            record = await self._overlay_legacy_fields(
                self._record(row), owner=owner, legacy_map=legacy_map
            )
            # list_page needs the canonical status to tell a resolved
            # question (active, legacy row archived by design) from a stale
            # open one; the wire kind alone cannot.
            record.metadata = {**record.metadata, "v2_status": row.get("status")}
            records.append(record)
        return records

    async def inspect_tier(
        self,
        tier: str,
        *,
        owner: Optional[str] = None,
        status: Optional[str] = None,
        limit: int = 500,
    ) -> List[Dict[str, Any]]:
        scope = self._scope(owner)
        if tier == "candidate":
            args = {
                "owner": scope.owner,
                "workspace_id": scope.workspace_id,
                "status": status,
                "limit": limit,
            }
            result = await self._call_tool("list_candidates", args)
            return list(result.get("candidates") or [])
        if tier == "quarantine":
            args = {
                "owner": scope.owner,
                "workspace_id": scope.workspace_id,
                "limit": limit,
            }
            result = await self._call_tool("list_quarantine", args)
            return list(result.get("quarantine") or [])
        if tier not in {"raw", "curated"}:
            raise ValueError("tier must be raw, candidate, curated, or quarantine")
        args: Dict[str, Any] = {
            "query": "",
            "tier": tier,
            "limit": limit,
            "owner": scope.owner,
            "workspace_id": scope.workspace_id,
        }
        result = await self._call_tool("search", args)
        return [dict(row.get("record", row)) for row in result.get("results", [])]

    async def digest(self, *, owner: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """Index-card digest of the bank for the provider scope: counts,
        pinned headlines, top clusters, newest topics. Cheap and read-only —
        meant for per-turn injection, with recall as the deep-dive path.

        Display text is identity-rendered (%USER% → the Handler label) so the
        injected card and the Brain preview never show the literal token;
        stored claims are untouched, and any rendered field carries a
        ``raw_*`` companion for edit round-trips."""
        scope = self._scope(owner)
        local_db = self._local_db_path()
        if local_db is not None:
            try:
                from services.memory.principal_context import ensure_principal_context_cached
                await asyncio.to_thread(
                    ensure_principal_context_cached,
                    owner=scope.owner,
                    workspace_id=scope.workspace_id,
                    db_path=local_db,
                )
            except Exception:
                logger.debug("v2 principal bootstrap unavailable", exc_info=True)
        result = await self._call_tool(
            "digest",
            {"owner": scope.owner, "workspace_id": scope.workspace_id},
        )
        if not (isinstance(result, dict) and "counts" in result):
            return None
        try:
            label = await self.handler_display_label(owner=scope.owner)
            _render_digest_identity(result, label)
        except Exception:
            logger.debug("digest identity rendering unavailable", exc_info=True)
        return result

    async def graph(
        self,
        op: str,
        *,
        owner: Optional[str] = None,
        query: Optional[str] = None,
        node_id: Optional[str] = None,
        to_node_id: Optional[str] = None,
        tag: Optional[str] = None,
        direction: Optional[str] = None,
        limit: int = 25,
    ) -> Dict[str, Any]:
        """Read v2 knowledge plus explicit Frankenmemory entity relationships."""
        scope = self._scope(owner)
        local_db = self._local_db_path()
        if local_db is not None:
            try:
                from services.memory.principal_context import ensure_principal_context_cached
                await asyncio.to_thread(
                    ensure_principal_context_cached,
                    owner=scope.owner,
                    workspace_id=scope.workspace_id,
                    db_path=local_db,
                )
            except Exception:
                logger.debug("v2 principal bootstrap unavailable", exc_info=True)
        canonical: Dict[str, Any] = {}
        if local_db is not None:
            try:
                from src.memory_versioned import canonical_graph

                canonical = await asyncio.to_thread(
                    canonical_graph,
                    op,
                    owner=scope.owner,
                    workspace_id=scope.workspace_id,
                    query=query,
                    node_id=node_id,
                    to_node_id=to_node_id,
                    tag=tag,
                    direction=direction,
                    limit=limit,
                    db_path=local_db,
                )
            except Exception:
                logger.debug("canonical v2 graph unavailable; using Rust graph", exc_info=True)
        args: Dict[str, Any] = {
            "op": op,
            "owner": scope.owner,
            "workspace_id": scope.workspace_id,
            "limit": limit,
        }
        if query is not None:
            args["query"] = query
        if node_id is not None:
            args["node_id"] = node_id
        if to_node_id is not None:
            args["dst_id"] = to_node_id
        if tag is not None:
            args["tag"] = tag
        if direction is not None:
            args["direction"] = direction
        has_canonical = {
            "overview": bool(canonical.get("node_total")),
            "cues": bool(canonical.get("hits")),
            "rank": bool(canonical.get("hits")),
            "tags": bool(canonical.get("tags")),
            "expand": bool(canonical.get("hits")),
            "fetch": bool(canonical.get("node")),
            "trace": bool(canonical.get("paths")),
        }.get(op, False)
        try:
            legacy = await self._call_tool("graph_walk", args)
        except Exception:
            if has_canonical:
                logger.debug(
                    "explicit Frankenmemory graph unavailable; using canonical v2 graph",
                    exc_info=True,
                )
                return canonical
            raise
        if not has_canonical:
            return legacy

        def merged_rows(key: str) -> List[Dict[str, Any]]:
            rows: List[Dict[str, Any]] = []
            seen = set()
            for row in [*(canonical.get(key) or []), *(legacy.get(key) or [])]:
                marker = str(row.get("id") or json.dumps(row, sort_keys=True, default=str))
                if marker in seen:
                    continue
                seen.add(marker)
                rows.append(row)
            return rows

        if op == "overview":
            nodes = merged_rows("nodes")
            edges = merged_rows("edges")
            visible_nodes = nodes[:limit]
            visible_node_ids = {str(row.get("id")) for row in visible_nodes}
            visible_edges = [
                edge
                for edge in edges
                if str(edge.get("src_id")) in visible_node_ids
                and str(edge.get("dst_id")) in visible_node_ids
            ][: max(limit * 3, limit)]
            return {
                **legacy,
                **canonical,
                "nodes": visible_nodes,
                "edges": visible_edges,
                "node_total": max(
                    len(nodes),
                    int(canonical.get("node_total") or 0)
                    + int(legacy.get("node_total") or 0),
                ),
                "edge_total": max(
                    len(edges),
                    int(canonical.get("edge_total") or 0)
                    + int(legacy.get("edge_total") or 0),
                ),
                "canonical": True,
                "explicit_relationships": True,
            }
        if op == "tags":
            counts: Dict[str, int] = {}
            for row in [*(canonical.get("tags") or []), *(legacy.get("tags") or [])]:
                name = str(row.get("tag") or "")
                if name:
                    counts[name] = counts.get(name, 0) + int(row.get("count") or 0)
            return {
                **legacy,
                **canonical,
                "tags": [
                    {"tag": name, "count": count}
                    for name, count in sorted(
                        counts.items(), key=lambda item: (-item[1], item[0])
                    )
                ][:limit],
                "canonical": True,
                "explicit_relationships": True,
            }
        if op == "fetch":
            return {
                **legacy,
                **canonical,
                "node": canonical.get("node") or legacy.get("node"),
                "canonical": True,
                "explicit_relationships": True,
            }
        key = "paths" if op == "trace" else "hits"
        return {
            **legacy,
            **canonical,
            key: merged_rows(key)[:limit],
            "canonical": True,
            "explicit_relationships": True,
        }

    async def memory_quality(self, *, rebuild_graph_fts: bool = False) -> Dict[str, Any]:
        return await self._call_tool("memory_quality", {"rebuild_graph_fts": rebuild_graph_fts})

    async def review_candidate(
        self,
        candidate_id: str,
        *,
        accept: bool,
        reason: str,
        owner: str,
        workspace_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        scope = self._scope(owner)
        result = await self._call_tool("review_candidate", {
            "id": candidate_id,
            "accept": accept,
            "reason": reason,
            "owner": scope.owner,
            "workspace_id": workspace_id or scope.workspace_id,
        })
        curated_id = result.get("curated_id") if accept else None
        if curated_id and self._local_db_path() is not None:
            try:
                record = await self._get_legacy_current(
                    str(curated_id),
                    owner=owner,
                    workspace_id=workspace_id or scope.workspace_id,
                )
                if record is not None:
                    from src.frankenmemory_v2 import mirror_record
                    mirror_record(
                        record,
                        owner=scope.owner,
                        workspace_id=workspace_id or scope.workspace_id,
                        action="review_candidate",
                        db_path=self._fm_db_path,
                    )
            except Exception:
                logger.debug("v2 candidate review mirror unavailable", exc_info=True)
        return result

    async def update_candidate(
        self,
        candidate_id: str,
        *,
        text: str,
        category: Optional[str] = None,
        reason: str = "edited_by_user",
        owner: str,
        workspace_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Edit one exact-tenant pending Rust candidate without activation."""
        scope = self._scope(owner)
        result = await self._call_tool(
            "update_candidate",
            {
                "id": candidate_id,
                "content": str(text),
                "category": category,
                "reason": str(reason or "edited_by_user")[:500],
                "owner": scope.owner,
                "workspace_id": workspace_id or scope.workspace_id,
            },
        )
        candidate = result.get("candidate")
        if not result.get("updated") or not isinstance(candidate, dict):
            raise MemoryRequestRejectedError("pending candidate edit was rejected")
        return candidate

    async def handler_display_label(self, *, owner: Optional[str] = None) -> str:
        scope = self._scope(owner)
        local_db = self._local_db_path()
        if local_db is None:
            result = await self._call_tool("handler_display_label", {
                "owner": scope.owner, "workspace_id": scope.workspace_id,
            })
            return str(result.get("label") or "Handler")
        from services.memory.principal_context import resolve_handler_display_label
        return await asyncio.to_thread(resolve_handler_display_label, scope.owner,
                                       workspace_id=scope.workspace_id, db_path=local_db)

    async def mark_conversation_source_removed(self, *, owner: str, session_id: str) -> Dict[str, Any]:
        """Detach one erased chat's provenance without erasing independent content."""
        scope = self._scope(owner)
        local_db = self._local_db_path()
        if local_db is None:
            return await self._call_tool("mark_conversation_source_removed", {
                "owner": scope.owner, "session_id": session_id,
                "workspace_id": scope.workspace_id,
            })
        from src.frankenmemory_v2 import mark_conversation_source_removed, conversation_source_workspaces
        bindings = await asyncio.to_thread(conversation_source_workspaces,
            owner=scope.owner, session_id=session_id, db_path=local_db)
        receipts = []
        for workspace in bindings:
            receipts.append(await asyncio.to_thread(mark_conversation_source_removed,
                owner=scope.owner, session_id=session_id, workspace_id=workspace, db_path=local_db))
        return {
            "complete": all(receipt.get("complete") is True for receipt in receipts),
            "owner": scope.owner, "session_id": session_id,
            "workspace_bindings": bindings, "receipts": receipts,
            "detached": {key: sum(int(receipt.get("detached", {}).get(key, 0)) for receipt in receipts) for key in ("curated", "raw", "candidates")},
            "independent_content_retained": True,
            "already_applied": bool(receipts) and all(receipt.get("already_applied") for receipt in receipts),
        }

    async def versioned_detail(
        self,
        memory_id: str,
        *,
        owner: Optional[str] = None,
    ) -> Dict[str, Any]:
        scope = self._scope(owner)
        if self._local_db_path() is None:
            return await self._call_tool("versioned_detail", {"memory_id": memory_id, "owner": scope.owner, "workspace_id": scope.workspace_id})
        from src.memory_versioned import get_knowledge

        return await asyncio.to_thread(
            get_knowledge,
            memory_id,
            owner=scope.owner,
            workspace_id=scope.workspace_id,
            db_path=self._fm_db_path,
        )

    async def versioned_list(
        self,
        *,
        owner: Optional[str] = None,
        statuses: Optional[List[str]] = None,
        kinds: Optional[List[str]] = None,
        limit: int = 500,
    ) -> List[Dict[str, Any]]:
        scope = self._scope(owner)
        local_db = self._local_db_path()
        if local_db is None:
            # v2-native read; broker mode without a pinned FM_DB_PATH has
            # no safe direct store to project from.
            return []
        from src.memory_versioned import list_knowledge

        details = await asyncio.to_thread(
            list_knowledge,
            owner=scope.owner,
            workspace_id=scope.workspace_id,
            statuses=statuses,
            kinds=kinds,
            limit=limit,
            db_path=local_db,
        )
        # Rust's passive fuzzy resolver can archive a legacy unknown before
        # the compatibility mirror is transitioned.  Never expose that split
        # as an active v2 question: hide only the stale OPEN-question row until
        # the canonical resolution/forget coordinator reconciles its lineage.
        # A resolved question (status active) archives its compatibility row
        # by design and must stay visible — its answer is the current value.
        try:
            live_ids = await asyncio.to_thread(
                _live_legacy_ids, local_db, scope.owner, scope.workspace_id
            )
            details = [
                item for item in details
                if not (
                    item.get("current", {}).get("kind") == "open_question"
                    and item.get("current", {}).get("status") == "open"
                    and str(item.get("id")) not in live_ids
                )
            ]
        except Exception:
            logger.debug("legacy/v2 question reconciliation unavailable", exc_info=True)
        return details

    async def versioned_transition(
        self,
        memory_id: str,
        action: str,
        *,
        expected_revision: int,
        owner: Optional[str] = None,
        **changes: Any,
    ) -> Dict[str, Any]:
        scope = self._scope(owner)
        if self._local_db_path() is None:
            return await self._call_tool("versioned_transition", {"memory_id": memory_id, "action": action, "expected_revision": expected_revision, **changes, "owner": scope.owner, "workspace_id": scope.workspace_id})
        from src.memory_versioned import transition_knowledge

        return await asyncio.to_thread(
            transition_knowledge,
            memory_id,
            action,
            owner=scope.owner,
            workspace_id=scope.workspace_id,
            expected_revision=expected_revision,
            db_path=self._fm_db_path,
            **changes,
        )

    async def assign_trust(
        self,
        *,
        owner: str,
        subject_kind: str,
        subject_id: str,
        subject_revision: int,
        trust: Optional[float] = None,
        state: str = "assigned",
        workspace_id: Optional[str] = None,
        project_id: Optional[str] = None,
        reason_code: str = "owner_review",
        rationale: str = "",
        evidence_ids: Optional[list[str]] = None,
        expected_assignment_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Append an owner-relative Trust assignment in the v2 authority."""
        scope = self._scope(owner)
        if self._local_db_path() is None:
            return await self._call_tool("assign_trust", {"subject_kind": subject_kind, "subject_id": subject_id, "subject_revision": subject_revision, "project_id": project_id, "trust": trust, "state": state, "reason_code": reason_code, "rationale": rationale, "evidence_ids": evidence_ids, "expected_assignment_id": expected_assignment_id, "owner": scope.owner, "workspace_id": workspace_id or scope.workspace_id})
        from src.frankenmemory_v2 import V2Repository

        return await asyncio.to_thread(
            V2Repository(self._fm_db_path).assign_trust,
            owner=owner,
            subject_kind=subject_kind,
            subject_id=subject_id,
            subject_revision=subject_revision,
            trust=trust,
            state=state,
            workspace_id=workspace_id,
            project_id=project_id,
            actor_type="owner",
            actor_id=owner,
            reason_code=reason_code,
            rationale=rationale,
            evidence_ids=evidence_ids,
            expected_assignment_id=expected_assignment_id,
        )

    async def get_trust(
        self,
        *,
        owner: str,
        subject_kind: str,
        subject_id: str,
        subject_revision: int,
        workspace_id: Optional[str] = None,
        project_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Read the current owner-relative Trust assignment."""
        scope = self._scope(owner)
        if self._local_db_path() is None:
            return await self._call_tool("get_trust", {"subject_kind": subject_kind, "subject_id": subject_id, "subject_revision": subject_revision, "project_id": project_id, "owner": scope.owner, "workspace_id": workspace_id or scope.workspace_id})
        from src.frankenmemory_v2 import V2Repository

        return await asyncio.to_thread(
            V2Repository(self._fm_db_path).get_trust,
            owner=owner,
            subject_kind=subject_kind,
            subject_id=subject_id,
            subject_revision=subject_revision,
            workspace_id=workspace_id,
            project_id=project_id,
        )

    async def get(self, memory_id: str, *, owner: Optional[str] = None) -> Optional[MemoryRecord]:
        scope = self._scope(owner)
        local_db = self._local_db_path()
        v2_read_mode = os.environ.get("FM_V2_READ_MODE", "preferred").strip().lower()
        if v2_read_mode in {"preferred", "strict"} and local_db is not None:
            from src.frankenmemory_v2 import get_current_record
            row = await asyncio.to_thread(
                get_current_record,
                memory_id,
                owner=scope.owner,
                workspace_id=scope.workspace_id,
                require_legacy_presence=True,
                db_path=local_db,
            )
            if row is not None:
                return await self._overlay_legacy_fields(self._record(row), owner=owner)
            if v2_read_mode == "strict":
                return None
        result = await self._call_tool(
            "get_memory",
            {"id": memory_id, "owner": scope.owner, "workspace_id": scope.workspace_id},
        )
        record = result.get("record")
        return self._record(record) if isinstance(record, dict) else None

    async def _get_legacy_current(
        self,
        memory_id: str,
        *,
        owner: Optional[str] = None,
        workspace_id: Optional[str] = None,
    ) -> Optional[MemoryRecord]:
        """Read the post-mutation legacy row without the v2 read preference.

        During the compatibility window the legacy Rust provider is still the
        mutation transport.  Calling ``get`` after an update would therefore
        return the older v2 shadow row and mirror stale content back over the
        successful edit.  This private path is deliberately only used to
        snapshot the transport result before the v2 projection is refreshed.
        """
        scope = self._scope(owner)
        result = await self._call_tool(
            "get_memory",
            {
                "id": memory_id,
                "owner": scope.owner,
                "workspace_id": workspace_id or scope.workspace_id,
            },
        )
        record = result.get("record")
        return self._record(record) if isinstance(record, dict) else None

    async def delete(self, memory_id: str, *, owner: Optional[str] = None) -> bool:
        scope = self._scope(owner)
        prior = None
        try:
            prior = await self.get(memory_id, owner=owner)
        except Exception:
            logger.debug("could not snapshot memory before delete", exc_info=True)
        result = await self._call_tool("delete_memory", {
            "id": memory_id,
            "owner": scope.owner,
            "workspace_id": scope.workspace_id,
        })
        deleted = bool(result.get("deleted"))
        if deleted and prior is not None and self._local_db_path() is not None:
            try:
                from src.frankenmemory_v2 import mirror_record
                mirror_record(prior, owner=scope.owner, workspace_id=scope.workspace_id, action="delete", db_path=self._fm_db_path)
            except Exception:
                logger.debug("v2 delete mirror unavailable", exc_info=True)
        return deleted

    async def update(
        self,
        memory_id: str,
        *,
        text: Optional[str] = None,
        category: Optional[str] = None,
        owner: Optional[str] = None,
    ) -> Optional[MemoryRecord]:
        scope = self._scope(owner)
        result = await self._call_tool("update_memory", {
            "id": memory_id,
            "content": text,
            "category": category,
            "owner": scope.owner,
            "workspace_id": scope.workspace_id,
        })
        if not result.get("updated"):
            return None
        record = await self._get_legacy_current(memory_id, owner=owner)
        if record is not None and self._local_db_path() is not None:
            try:
                from src.frankenmemory_v2 import mirror_record
                mirror_record(record, owner=scope.owner, workspace_id=scope.workspace_id, db_path=self._fm_db_path)
            except Exception:
                logger.debug("v2 update mirror unavailable", exc_info=True)
        return record

    async def pin(self, memory_id: str, pinned: bool, *, owner: Optional[str] = None) -> bool:
        scope = self._scope(owner)
        result = await self._call_tool("update_memory", {
            "id": memory_id,
            "pinned": bool(pinned),
            "owner": scope.owner,
            "workspace_id": scope.workspace_id,
        })
        updated = bool(result.get("updated"))
        if updated and self._local_db_path() is not None:
            try:
                record = await self._get_legacy_current(memory_id, owner=owner)
                if record is not None:
                    metadata = dict(record.metadata or {})
                    metadata["pinned"] = bool(pinned)
                    record.metadata = metadata
                    record.pinned = bool(pinned)
                    from src.frankenmemory_v2 import mirror_record
                    mirror_record(record, owner=scope.owner, workspace_id=scope.workspace_id, db_path=self._fm_db_path)
            except Exception:
                logger.debug("v2 pin mirror unavailable", exc_info=True)
        return updated

    async def resolve_question(
        self,
        memory_id: str,
        *,
        resolved_by: Optional[str] = None,
        answer: Optional[str] = None,
        expected_revision: Optional[int] = None,
        owner: Optional[str] = None,
    ) -> bool:
        scope = self._scope(owner)
        if self._local_db_path() is None:
            result = await self._call_tool("resolve_question", {
                "memory_id": memory_id, "resolved_by": resolved_by, "answer": answer,
                "expected_revision": expected_revision, "owner": scope.owner,
                "workspace_id": scope.workspace_id,
            })
            return bool(result.get("resolved"))
        prior = await self._get_legacy_current(memory_id, owner=owner)
        if prior is None:
            return False
        if prior.archived or prior.kind not in {"unknown", "open_question"}:
            return False
        answer_text = str(answer or "").strip()
        if not answer_text and resolved_by:
            answer_record = await self._get_legacy_current(resolved_by, owner=owner)
            if answer_record is not None:
                answer_text = answer_record.text.strip()
        if not answer_text:
            raise MemoryRequestRejectedError("resolving an open question requires its answer")
        try:
            detail = await self.versioned_detail(memory_id, owner=owner)
        except Exception:
            from src.frankenmemory_v2 import mirror_record

            mirrored = await asyncio.to_thread(
                mirror_record,
                prior,
                owner=scope.owner,
                workspace_id=scope.workspace_id,
                db_path=self._fm_db_path,
            )
            if not mirrored:
                raise MemoryRequestRejectedError("open question has no canonical knowledge block")
            detail = await self.versioned_detail(memory_id, owner=owner)
        current_revision = int(detail["current_revision"])
        if expected_revision is not None and int(expected_revision) != current_revision:
            from src.memory_versioned import VersionedMemoryConflict

            raise VersionedMemoryConflict(int(expected_revision), detail["current"])
        resolved_detail = await self.versioned_transition(
            memory_id,
            "resolve",
            expected_revision=current_revision,
            value=answer_text,
            actor_type="ai" if resolved_by else "user",
            actor_id=resolved_by or scope.owner,
            reason="answer open question",
            owner=owner,
        )
        args: Dict[str, Any] = {
            "id": memory_id,
            # Newer fm-mcp builds validate that an archive transition carries
            # the answer; older builds ignore this additive field.
            "answer": answer_text,
            "owner": scope.owner,
            "workspace_id": scope.workspace_id,
        }
        if resolved_by:
            args["resolved_by"] = resolved_by
        try:
            result = await self._call_tool("resolve_memory", args)
        except Exception:
            try:
                await self.versioned_transition(
                    memory_id,
                    "reopen",
                    expected_revision=int(resolved_detail["current_revision"]),
                    actor_type="system",
                    actor_id="resolve-compensation",
                    reason="legacy resolve failed",
                    owner=owner,
                )
            except Exception:
                logger.exception("failed to compensate canonical question resolution")
            raise
        if not bool(result.get("resolved")):
            await self.versioned_transition(
                memory_id,
                "reopen",
                expected_revision=int(resolved_detail["current_revision"]),
                actor_type="system",
                actor_id="resolve-compensation",
                reason="legacy resolve rejected",
                owner=owner,
            )
            return False
        return True

    async def reopen_question(
        self,
        memory_id: str,
        *,
        expected_revision: int,
        owner: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Reopen the same canonical block and its compatibility row."""
        scope = self._scope(owner)
        reopened_detail = await self.versioned_transition(
            memory_id,
            "reopen",
            expected_revision=expected_revision,
            actor_type="user",
            actor_id=scope.owner,
            reason="reopen question",
            owner=owner,
        )
        try:
            result = await self._call_tool(
                "reopen_memory",
                {
                    "id": memory_id,
                    "owner": scope.owner,
                    "workspace_id": scope.workspace_id,
                },
            )
        except Exception:
            try:
                await self.versioned_transition(
                    memory_id,
                    "revert",
                    expected_revision=int(reopened_detail["current_revision"]),
                    target_revision=int(expected_revision),
                    actor_type="system",
                    actor_id="reopen-compensation",
                    reason="legacy reopen failed",
                    owner=owner,
                )
            except Exception:
                logger.exception("failed to compensate canonical question reopen")
            raise
        if not bool(result.get("reopened")):
            await self.versioned_transition(
                memory_id,
                "revert",
                expected_revision=int(reopened_detail["current_revision"]),
                target_revision=int(expected_revision),
                actor_type="system",
                actor_id="reopen-compensation",
                reason="legacy reopen rejected",
                owner=owner,
            )
            raise MemoryRequestRejectedError("resolved question could not be reopened")
        return reopened_detail

    async def record_access(
        self,
        memory_ids: List[str],
        *,
        owner: Optional[str] = None,
    ) -> int:
        scope = self._scope(owner)
        result = await self._call_tool(
            "record_memory_access",
            {"ids": memory_ids, "owner": scope.owner, "workspace_id": scope.workspace_id},
        )
        return int(result.get("updated", 0))

    async def owner_stats(self, *, owner: Optional[str] = None) -> Dict[str, Any]:
        scope = self._scope(owner)
        return await self._call_tool(
            "owner_lifecycle",
            {"action": "stats", "owner": scope.owner, "workspace_id": scope.workspace_id},
        )

    async def purge_owner(self, *, owner: Optional[str] = None) -> Dict[str, Any]:
        scope = self._scope(owner)
        result = await self._call_tool(
            "owner_lifecycle",
            {"action": "purge", "owner": scope.owner, "workspace_id": scope.workspace_id},
        )
        if isinstance(result, dict) and result.get("purged") is True:
            await self._delete_principal_bindings(scope.owner)
        return result

    async def reset_owner(
        self,
        action: str,
        *,
        owner: Optional[str] = None,
        components: list[str],
        expected_counts: Optional[dict] = None,
    ) -> dict:
        scope = self._scope(owner)
        arguments: Dict[str, Any] = {
            "action": action,
            "owner": scope.owner,
            "workspace_id": scope.workspace_id,
            "components": list(components),
        }
        if expected_counts is not None:
            arguments["expected_counts"] = expected_counts
        result = await self._call_tool("owner_lifecycle", arguments)
        if (
            action == "reset_commit"
            and isinstance(result, dict)
            and result.get("complete") is True
            and "memories" in components
        ):
            await self._delete_principal_bindings(scope.owner)
        return result

    async def _delete_principal_bindings(self, owner: str) -> None:
        """Drop the owner's Python-side principal bindings after a reset/purge.

        ``fm_v2_principal_bindings`` is created by the Python principal
        bootstrap and unknown to the Rust schema, so the store's owner-scoped
        delete closure cannot know it. Left behind, a stale binding would
        resurrect references to entities the reset just erased.
        """
        db_path = self._local_db_path()
        if db_path is None:
            return

        def _delete() -> None:
            import sqlite3

            with sqlite3.connect(db_path, timeout=30) as conn:
                exists = conn.execute(
                    "SELECT EXISTS(SELECT 1 FROM sqlite_master WHERE type='table' AND name='fm_v2_principal_bindings')"
                ).fetchone()
                if not exists or not exists[0]:
                    return
                conn.execute(
                    "DELETE FROM fm_v2_principal_bindings WHERE owner_id=?",
                    (owner,),
                )

        await asyncio.to_thread(_delete)

    async def rename_owner(self, new_owner: str, *, owner: Optional[str] = None) -> Dict[str, Any]:
        scope = self._scope(owner)
        return await self._call_tool(
            "owner_lifecycle",
            {
                "action": "rename",
                "owner": scope.owner,
                "workspace_id": scope.workspace_id,
                "new_owner": new_owner,
            },
        )

    async def groom(
        self,
        op: str,
        *,
        owner: Optional[str] = None,
        workspace_id: Optional[str] = None,
        dry_run: bool = False,
    ) -> Dict[str, Any]:
        scope = MemoryScope(
            owner=owner or "",
            workspace_id=workspace_id or self._workspace_id,
            workspace_path=self._workspace_id,
        )
        args: Dict[str, Any] = {
            "op": op,
            "dry_run": dry_run,
            "owner": scope.owner,
            "workspace_id": scope.workspace_id,
        }
        return await self._call_tool("groom", args)

    async def retention(
        self,
        action: str,
        *,
        owner: Optional[str] = None,
        workspace_id: Optional[str] = None,
        **policy: Any,
    ) -> Dict[str, Any]:
        scope = self._scope(owner)
        args: Dict[str, Any] = {
            "action": action,
            "owner": scope.owner,
            "workspace_id": workspace_id or scope.workspace_id,
            **policy,
        }
        return await self._call_tool("memory_retention", args)

    async def forget(
        self,
        action: str,
        *,
        owner: Optional[str] = None,
        workspace_id: Optional[str] = None,
        selector_kind: Optional[str] = None,
        selector: Optional[str] = None,
        preview_token: Optional[str] = None,
        tombstone_id: Optional[str] = None,
        operation_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        scope = self._scope(owner)
        args: Dict[str, Any] = {
            "action": action,
            "owner": scope.owner,
            "workspace_id": workspace_id or scope.workspace_id,
        }
        for key, value in {
            "selector_kind": selector_kind,
            "selector": selector,
            "preview_token": preview_token,
            "tombstone_id": tombstone_id,
            "operation_id": operation_id,
        }.items():
            if value is not None:
                args[key] = value
        return await self._call_tool("memory_forget", args)

    async def export_scope(
        self,
        *,
        owner: Optional[str] = None,
        workspace_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        scope = self._scope(owner)
        return await self._call_tool(
            "memory_export",
            {
                "owner": scope.owner,
                "workspace_id": workspace_id or scope.workspace_id,
            },
        )

    async def explain(
        self,
        memory_id: str,
        *,
        owner: Optional[str] = None,
        workspace_id: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        scope = self._scope(owner)
        result = await self._call_tool(
            "memory_explain",
            {
                "id": memory_id,
                "owner": scope.owner,
                "workspace_id": workspace_id or scope.workspace_id,
            },
        )
        explanation = result.get("explanation")
        return dict(explanation) if isinstance(explanation, dict) else None
