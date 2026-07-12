"""Frankenmemory provider — thin adapter over fm-mcp MCP server."""

from __future__ import annotations

import asyncio
import json
import logging
import os
from contextlib import AsyncExitStack
from typing import Any, Dict, List, Optional

from src.memory_provider import MemoryProvider, MemoryRecord, MemorySearchHit

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
        workspace_id: str = "global",
        env: Optional[Dict[str, str]] = None,
    ):
        self._command = command or os.environ.get("FM_MCP_COMMAND", "fm-mcp")
        self._workspace_id = workspace_id
        self._env = env
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
        result = await fut
        text = result.content[0].text if result.content else "{}"
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return {"raw": text}

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
        args: Dict[str, Any] = {
            "content": text,
            "capture_mode": "manual",
            "workspace_id": self._workspace_id,
            "source": source,
            "category": category,
            "source_type": _SOURCE_TYPE_MAP.get(source, "auto_extracted"),
        }
        if owner:
            args["owner"] = owner
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
        args: Dict[str, Any] = {
            "query": query,
            "top_k": top_k,
            "tier": "curated",
            "workspace_id": self._workspace_id,
        }
        if owner:
            args["owner"] = owner

        try:
            result = await self._call_tool("recall", args)
        except Exception as e:
            logger.warning("frankenmemory recall failed: %r", e)
            return []
        hits: List[MemorySearchHit] = []
        for mem in result.get("memories", []):
            record = MemoryRecord(
                id=mem.get("id", ""),
                text=mem.get("content", ""),
                category=mem.get("kind", "episodic"),
                source=mem.get("source", ""),
                owner=mem.get("owner"),
                session_id=mem.get("session_id"),
                metadata=mem.get("metadata", {}),
                pinned=bool((mem.get("metadata") or {}).get("pinned", False)),
            )
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
        args: Dict[str, Any] = {
            "query": "",
            "limit": limit,
            "tier": "curated",
            "workspace_id": self._workspace_id,
        }
        if owner:
            args["owner"] = owner

        try:
            result = await self._call_tool("search", args)
        except Exception as e:
            logger.warning("frankenmemory list_memories failed: %r", e)
            raise
        records: List[MemoryRecord] = []
        for r in result.get("results", []):
            rec = r.get("record", r)
            records.append(MemoryRecord(
                id=rec.get("id", ""),
                text=rec.get("content", ""),
                category=rec.get("kind", "episodic"),
                source=rec.get("source", ""),
                owner=rec.get("owner"),
                session_id=rec.get("session_id"),
                metadata=rec.get("metadata", {}),
                pinned=bool((rec.get("metadata") or {}).get("pinned", False)),
            ))
        return records

    async def inspect_tier(
        self,
        tier: str,
        *,
        owner: Optional[str] = None,
        status: Optional[str] = None,
        limit: int = 500,
    ) -> List[Dict[str, Any]]:
        if tier == "candidate":
            args = {
                "owner": owner,
                "status": status,
                "limit": limit,
            }
            if self._workspace_id != "global":
                args["workspace_id"] = self._workspace_id
            result = await self._call_tool("list_candidates", args)
            return list(result.get("candidates") or [])
        if tier == "quarantine":
            args = {
                "owner": owner,
                "limit": limit,
            }
            if self._workspace_id != "global":
                args["workspace_id"] = self._workspace_id
            result = await self._call_tool("list_quarantine", args)
            return list(result.get("quarantine") or [])
        if tier not in {"raw", "curated"}:
            raise ValueError("tier must be raw, candidate, curated, or quarantine")
        args: Dict[str, Any] = {
            "query": "",
            "tier": tier,
            "limit": limit,
        }
        if self._workspace_id != "global":
            args["workspace_id"] = self._workspace_id
        if owner:
            args["owner"] = owner
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
        return await self._call_tool("review_candidate", {
            "id": candidate_id,
            "accept": accept,
            "reason": reason,
            "owner": owner,
            "workspace_id": workspace_id or self._workspace_id,
        })

    async def get(self, memory_id: str, *, owner: Optional[str] = None) -> Optional[MemoryRecord]:
        for record in await self.list_memories(owner=owner, limit=1000):
            if record.id == memory_id:
                return record
        return None

    async def delete(self, memory_id: str, *, owner: Optional[str] = None) -> bool:
        try:
            result = await self._call_tool("delete_memory", {
                "id": memory_id,
                "owner": owner,
                "workspace_id": self._workspace_id,
            })
        except Exception as e:
            logger.warning("frankenmemory delete failed: %r", e)
            return False
        return bool(result.get("deleted"))

    async def update(
        self,
        memory_id: str,
        *,
        text: Optional[str] = None,
        category: Optional[str] = None,
        owner: Optional[str] = None,
    ) -> Optional[MemoryRecord]:
        result = await self._call_tool("update_memory", {
            "id": memory_id,
            "content": text,
            "owner": owner,
            "workspace_id": self._workspace_id,
        })
        if not result.get("updated"):
            return None
        for record in await self.list_memories(owner=owner, limit=1000):
            if record.id == memory_id:
                return record
        return None

    async def pin(self, memory_id: str, pinned: bool, *, owner: Optional[str] = None) -> bool:
        result = await self._call_tool("update_memory", {
            "id": memory_id,
            "pinned": bool(pinned),
            "owner": owner,
            "workspace_id": self._workspace_id,
        })
        return bool(result.get("updated"))

    async def groom(
        self,
        op: str,
        *,
        owner: Optional[str] = None,
        workspace_id: Optional[str] = None,
        dry_run: bool = False,
    ) -> Dict[str, Any]:
        args: Dict[str, Any] = {"op": op, "dry_run": dry_run}
        if owner:
            args["owner"] = owner
        if workspace_id:
            args["workspace_id"] = workspace_id
        return await self._call_tool("groom", args)
