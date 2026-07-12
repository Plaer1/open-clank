"""Frankenmemory provider — thin adapter over fm-mcp MCP server."""

from __future__ import annotations

import asyncio
import json
import logging
import os
from contextlib import AsyncExitStack
from datetime import datetime
from typing import Any, Dict, List, Optional

from src.memory_provider import (
    MemoryProvider,
    MemoryRecord,
    MemoryScope,
    MemorySearchHit,
    MemoryTransportError,
)

logger = logging.getLogger(__name__)

_SOURCE_TYPE_MAP = {
    "user": "human",
    "user_created": "human",
    "ai_agent": "ai",
    "agent_explicit": "ai",
    "auto_extracted": "auto_extracted",
}


class FrankenmemoryProvider(MemoryProvider):
    """MemoryProvider backed by the fm-mcp Rust engine over MCP stdio."""

    provider_id = "frankenmemory"
    display_name = "Frankenmemory (Rust)"

    def __init__(
        self,
        command: Optional[str] = None,
        workspace_id: str = "",
        env: Optional[Dict[str, str]] = None,
    ):
        self._command = command or os.environ.get("FM_MCP_COMMAND", "fm-mcp")
        self._workspace_id = workspace_id or os.environ.get("FM_WORKSPACE_ID") or os.getcwd()
        self._env = {"FM_SCOPE_AUTHORITY": "trusted-caller", **(env or {})}
        # The MCP stdio transport pins anyio cancel scopes to the task that
        # entered them. The server initializes in its startup task and calls
        # from request-handler tasks, which breaks those scopes (the infamous
        # empty-str() ClosedResourceError / cancel-scope RuntimeError). So a
        # single owner task holds the session end-to-end and everything else
        # talks to it through a queue.
        self._owner_task: Optional[asyncio.Task] = None
        self._requests: Optional[asyncio.Queue] = None
        self._ready: Optional[asyncio.Future] = None

    async def initialize(self) -> None:
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
                server_params = StdioServerParameters(
                    command=self._command,
                    args=[],
                    env={**os.environ, **(self._env or {})},
                )
                read_stream, write_stream = await stack.enter_async_context(stdio_client(server_params))
                session = await stack.enter_async_context(ClientSession(read_stream, write_stream))
                await session.initialize()
                health = await session.call_tool("memory_quality", {"rebuild_graph_fts": False})
                health_text = health.content[0].text if health.content else "{}"
                health_data = json.loads(health_text)
                expected_id = (self._env or {}).get("FM_DB_ID") or os.environ.get("FM_DB_ID")
                if expected_id and health_data.get("database_id") != expected_id:
                    raise RuntimeError(
                        "frankenmemory database identity mismatch during provider handshake"
                    )
                if int(health_data.get("schema_version", 0)) < 6:
                    raise RuntimeError("frankenmemory schema is older than the scoped-memory contract")
                logger.info("FrankenmemoryProvider connected to fm-mcp")
                if not self._ready.done():
                    self._ready.set_result(None)
                while True:
                    item = await requests.get()
                    if item is None:
                        return
                    name, arguments, fut = item
                    try:
                        result = await session.call_tool(name, arguments)
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

    async def _call_tool(self, name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        if not self._owner_task or self._owner_task.done():
            await self.initialize()
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        await self._requests.put((name, arguments, fut))
        try:
            result = await fut
        except Exception as exc:
            raise MemoryTransportError(f"frankenmemory {name} failed: {exc}") from exc
        if getattr(result, "isError", False):
            detail = result.content[0].text if result.content else "unknown MCP error"
            raise MemoryTransportError(f"frankenmemory {name} rejected the request: {detail}")
        text = result.content[0].text if result.content else "{}"
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return {"raw": text}

    def _scope(self, owner: Optional[str], session_id: Optional[str] = None) -> MemoryScope:
        return MemoryScope(
            owner=owner or "",
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
        return MemoryRecord(
            id=data.get("id", ""),
            text=data.get("content", data.get("text", "")),
            timestamp=timestamp,
            category=metadata.get("category", data.get("kind", "episodic")),
            source=data.get("source", ""),
            owner=data.get("owner"),
            session_id=data.get("session_id"),
            metadata=metadata,
            pinned=bool(metadata.get("pinned", False) or data.get("source_label") == "pinned"),
            workspace_id=data.get("workspace_id"),
            created_at=data.get("created_at"),
            updated_at=data.get("updated_at"),
            uses=int(metadata.get("uses", 0) or 0),
        )

    async def remember(
        self,
        text: str,
        *,
        owner: Optional[str] = None,
        session_id: Optional[str] = None,
        category: str = "fact",
        source: str = "user",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> MemoryRecord:
        scope = self._scope(owner, session_id)
        args: Dict[str, Any] = {
            "content": text,
            "capture_mode": "manual",
            "workspace_id": scope.workspace_id,
            "workspace_path": scope.workspace_path,
            "owner": scope.owner,
            "source": source,
            "category": category,
            "source_type": _SOURCE_TYPE_MAP.get(source, "auto_extracted"),
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
        record_id = record_ids[0]
        return MemoryRecord(
            id=record_id,
            text=text,
            category=category,
            source=source,
            owner=owner,
            session_id=session_id,
            metadata=metadata or {},
            pinned=bool((metadata or {}).get("pinned", False)),
        )

    async def recall(
        self,
        query: str,
        *,
        owner: Optional[str] = None,
        top_k: int = 5,
    ) -> List[MemorySearchHit]:
        scope = self._scope(owner)
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
        return await self._call_tool("review_candidate", {
            "id": candidate_id,
            "accept": accept,
            "reason": reason,
            "owner": scope.owner,
            "workspace_id": workspace_id or scope.workspace_id,
        })

    async def get(self, memory_id: str, *, owner: Optional[str] = None) -> Optional[MemoryRecord]:
        scope = self._scope(owner)
        result = await self._call_tool(
            "get_memory",
            {"id": memory_id, "owner": scope.owner, "workspace_id": scope.workspace_id},
        )
        record = result.get("record")
        return self._record(record) if isinstance(record, dict) else None

    async def delete(self, memory_id: str, *, owner: Optional[str] = None) -> bool:
        scope = self._scope(owner)
        result = await self._call_tool("delete_memory", {
            "id": memory_id,
            "owner": scope.owner,
            "workspace_id": scope.workspace_id,
        })
        return bool(result.get("deleted"))

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
        return await self.get(memory_id, owner=owner)

    async def pin(self, memory_id: str, pinned: bool, *, owner: Optional[str] = None) -> bool:
        scope = self._scope(owner)
        result = await self._call_tool("update_memory", {
            "id": memory_id,
            "pinned": bool(pinned),
            "owner": scope.owner,
            "workspace_id": scope.workspace_id,
        })
        return bool(result.get("updated"))

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
        return await self._call_tool(
            "owner_lifecycle",
            {"action": "purge", "owner": scope.owner, "workspace_id": scope.workspace_id},
        )

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
