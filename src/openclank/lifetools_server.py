"""lifetools_server.py — MCP bridge exposing Open Clank life-tools.

Per-session spawned server (env-baked context). Exposes all FUNCTION_TOOL_SCHEMAS
minus the Phase 2 exclusion set (9 coding-overlap + web_search/web_fetch/ask_user/
update_plan). Tools surface as lifetools:<tool> in mimo via the sanitized namespace.
"""

import asyncio
import json
import logging
import os
import sys
import time
from pathlib import Path

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent

# Add the Open Clank source root for tool_schemas/tool_execution imports.
_OPEN_CLANK_ROOT = Path(__file__).resolve().parents[2]
if _OPEN_CLANK_ROOT.exists():
    sys.path.insert(0, str(_OPEN_CLANK_ROOT))

from src.tool_schemas import (
    FUNCTION_TOOL_SCHEMAS,
    OPENTHESIUS_BRIDGE_EXCLUDED_TOOLS,
    function_call_to_tool_block,
)

logger = logging.getLogger(__name__)

# Additional tools excluded from the bridge beyond the 9 coding-overlap.
# These overlap with mimo native capabilities or are ACP-internal.
_BRIDGE_EXTRA_EXCLUDED = {
    "web_search",    # mimo has its own web_search
    "web_fetch",     # mimo has its own web_fetch
    "ask_user",      # mimo handles via ACP permission ask
    "update_plan",   # mimo plan agent handles natively
}

_ALL_EXCLUDED = OPENTHESIUS_BRIDGE_EXCLUDED_TOOLS | _BRIDGE_EXTRA_EXCLUDED

# Session context from env (baked at spawn time)
_SESSION_ID = os.environ.get("SESSION_ID", "")
_OWNER = os.environ.get("OWNER", "")
_WORKSPACE = os.environ.get("WORKSPACE", "")
_MEMORY_PROVIDER = None
_MEMORY_PROVIDER_LOCK = None

# Skill usage sidecar — direct write to Open Clank's _usage.json.
# Open Clank and this MCP server share the filesystem. Open Clank reads
# _usage.json on every load; we just append. JSON isn't safe for
# concurrent writes, but the risk is a lost increment (not corruption)
# because odysseus reads with json.load which is atomic-enough on small
# files. Best-effort, fail-open.
_USAGE_FILE = str(
    Path(
        os.getenv("OPEN_CLANK_DATA_DIR")
        or os.getenv("ODYSSEUS_DATA_DIR")
        or _OPEN_CLANK_ROOT / "data"
    )
    / "skills"
    / "_usage.json"
)

server = Server("lifetools")


def _build_tool_list() -> list[Tool]:
    """Convert FUNCTION_TOOL_SCHEMAS minus exclusion set to mcp.types.Tool."""
    tools = []
    for schema in FUNCTION_TOOL_SCHEMAS:
        func = schema.get("function", {})
        name = func.get("name", "")
        if not name or name in _ALL_EXCLUDED:
            continue
        tools.append(
            Tool(
                name=name,
                description=func.get("description", ""),
                inputSchema=func.get("parameters", {"type": "object", "properties": {}}),
            )
        )
    return tools


_BRIDGED_TOOLS = _build_tool_list()

# `recall_memory` predates the OpenAI-function schema inventory, but strict
# Agent reaches life-tools exclusively through this MCP server. Advertise the
# existing read-only handler here rather than creating another memory backend.
_RECALL_MEMORY_TOOL = Tool(
    name="recall_memory",
    description=(
        "Read-only recall from the user's persistent Frankenmemory bank. "
        "Search by query, or pass memory_id to fetch one exact memory."
    ),
    inputSchema={
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Terms to search for in persistent memory",
            },
            "memory_id": {
                "type": "string",
                "description": "Exact memory id to fetch instead of searching",
            },
        },
    },
)

# A1.3: dedicated usage-recording tool — not from FUNCTION_TOOL_SCHEMAS.
_RECORD_USAGE_TOOL = Tool(
    name="record_skill_usage",
    description="Record that a skill was loaded/used. Called by mimo's skill tool after loading a skill body. Best-effort; failures are silently ignored.",
    inputSchema={
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "The skill name (slug)"},
            "owner": {"type": "string", "description": "The skill owner (optional)"},
        },
        "required": ["name"],
    },
)


async def _ensure_memory_provider():
    """Install one owner-scoped Frankenmemory provider in this MCP process."""
    global _MEMORY_PROVIDER, _MEMORY_PROVIDER_LOCK

    if not _OWNER.strip():
        raise RuntimeError("lifetools memory requires an authenticated owner")
    if _MEMORY_PROVIDER is not None:
        from src.ai_interaction import set_memory_manager

        set_memory_manager(None, provider=_MEMORY_PROVIDER)
        return _MEMORY_PROVIDER

    if _MEMORY_PROVIDER_LOCK is None:
        _MEMORY_PROVIDER_LOCK = asyncio.Lock()
    async with _MEMORY_PROVIDER_LOCK:
        if _MEMORY_PROVIDER is None:
            from src.frankenmemory_provider import FrankenmemoryProvider
            from src.memory_scope import chat_workspace

            workspace_id = (
                os.environ.get("FM_WORKSPACE_ID", "").strip()
                or chat_workspace()
            )
            provider_env = {
                "FM_SCOPE_AUTHORITY": "trusted-caller",
                "FM_OWNER": _OWNER.strip(),
                "FM_WORKSPACE_ID": workspace_id,
            }
            for key in ("FM_DB_PATH", "FM_DB_ID"):
                value = os.environ.get(key, "").strip()
                if value:
                    provider_env[key] = value
            provider = FrankenmemoryProvider(
                command=os.environ.get("FM_MCP_COMMAND", "fm-mcp"),
                workspace_id=workspace_id,
                env=provider_env,
            )
            await provider.initialize()
            _MEMORY_PROVIDER = provider

        from src.ai_interaction import set_memory_manager

        set_memory_manager(None, provider=_MEMORY_PROVIDER)
        return _MEMORY_PROVIDER


def _structured_memory_block(name: str, arguments: dict):
    """Adapt both structured memory tools to their existing text handlers."""
    from src.agent_tools import ToolBlock

    if name == "manage_memory":
        block = function_call_to_tool_block(name, json.dumps(arguments))
        if block is None:
            raise ValueError("Invalid manage_memory arguments")
        return block
    if name == "recall_memory":
        memory_id = str(arguments.get("memory_id") or "").strip()
        query = str(arguments.get("query") or "").strip()
        content = f"id: {memory_id}" if memory_id else query
        return ToolBlock(tool_type=name, content=content)
    raise ValueError(f"Unsupported structured memory tool: {name}")


def _record_usage(name: str, owner: str = "") -> None:
    """Best-effort write to odysseus _usage.json sidecar."""
    try:
        usage = {}
        if os.path.exists(_USAGE_FILE):
            with open(_USAGE_FILE, encoding="utf-8") as f:
                usage = json.load(f)
            if not isinstance(usage, dict):
                usage = {}
        key = f"{owner}::{name}" if owner else name
        entry = usage.setdefault(key, {"uses": 0, "last_used": None})
        entry["uses"] = int(entry.get("uses", 0)) + 1
        entry["last_used"] = int(time.time())
        tmp = _USAGE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(usage, f, indent=2)
        os.replace(tmp, _USAGE_FILE)
    except Exception as e:
        logger.debug("record_skill_usage failed (non-fatal): %s", e)


@server.list_tools()
async def list_tools() -> list[Tool]:
    return _BRIDGED_TOOLS + [_RECALL_MEMORY_TOOL, _RECORD_USAGE_TOOL]


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    # A1.3: handle usage recording directly (not via execute_tool_block)
    if name == "record_skill_usage":
        _record_usage(arguments.get("name", ""), arguments.get("owner", _OWNER))
        return [TextContent(type="text", text=json.dumps({"ok": True}))]

    from src.tool_execution import execute_tool_block

    if name in _ALL_EXCLUDED:
        return [TextContent(
            type="text",
            text=json.dumps({"error": f"Tool '{name}' is not available via the bridge.", "exit_code": 1}),
        )]

    try:
        if name in {"manage_memory", "recall_memory"}:
            await _ensure_memory_provider()
            block = _structured_memory_block(name, arguments)
        else:
            from src.agent_tools import ToolBlock

            block = ToolBlock(tool_type=name, content=json.dumps(arguments))
        description, result = await execute_tool_block(
            block,
            session_id=_SESSION_ID,
            disabled_tools=set(),  # mimo is the sole permission gate
            owner=_OWNER,
            workspace=_WORKSPACE,
        )
    except Exception as e:
        logger.error("lifetools dispatch error for %s: %s", name, e, exc_info=True)
        result = {"error": str(e), "exit_code": 1}

    return [TextContent(type="text", text=json.dumps(result))]


async def main():
    try:
        async with stdio_server() as (read_stream, write_stream):
            await server.run(read_stream, write_stream, server.create_initialization_options())
    finally:
        if _MEMORY_PROVIDER is not None:
            await _MEMORY_PROVIDER.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
