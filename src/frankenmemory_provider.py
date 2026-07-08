"""Frankenmemory provider — thin adapter over fm-mcp MCP server."""

from __future__ import annotations

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
        self._stack: Optional[AsyncExitStack] = None
        self._session = None

    async def initialize(self) -> None:
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        self._stack = AsyncExitStack()
        server_params = StdioServerParameters(
            command=self._command,
            args=[],
            env={**os.environ, **(self._env or {})},
        )
        transport = await self._stack.enter_async_context(stdio_client(server_params))
        read_stream, write_stream = transport
        self._session = await self._stack.enter_async_context(
            ClientSession(read_stream, write_stream)
        )
        await self._session.initialize()
        logger.info("FrankenmemoryProvider connected to fm-mcp")

    async def shutdown(self) -> None:
        if self._stack:
            await self._stack.aclose()
            self._stack = None
            self._session = None

    async def _call_tool(self, name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        if not self._session:
            await self.initialize()
        result = await self._session.call_tool(name, arguments)
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
        record_id = f"fm_{result.get('records_captured', 0)}"
        return MemoryRecord(
            id=record_id,
            text=text,
            category=category,
            source=source,
            owner=owner,
            session_id=session_id,
            metadata=metadata or {},
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
            logger.warning("frankenmemory recall failed: %s", e)
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
            logger.warning("frankenmemory list_memories failed: %s", e)
            return []
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
            ))
        return records

    async def delete(self, memory_id: str, *, owner: Optional[str] = None) -> bool:
        try:
            result = await self._call_tool("groom", {
                "op": "delete",
                "workspace_id": self._workspace_id,
            })
        except Exception as e:
            logger.warning("frankenmemory delete failed: %s", e)
            return False
        return result.get("records_archived", 0) > 0 or "raw" in str(result)
