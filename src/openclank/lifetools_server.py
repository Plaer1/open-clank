"""lifetools_server.py — MCP bridge exposing odysseus life-tools to mimo.

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

# Add odysseus source to path for tool_schemas/tool_execution imports
_ODYSSEUS_ROOT = Path(__file__).resolve().parents[2]
if _ODYSSEUS_ROOT.exists():
    sys.path.insert(0, str(_ODYSSEUS_ROOT))

from src.tool_schemas import FUNCTION_TOOL_SCHEMAS, OPENTHESIUS_BRIDGE_EXCLUDED_TOOLS

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

# A1.3: skill usage sidecar — direct write to odysseus _usage.json.
# Both odysseus and this MCP server share the filesystem. odysseus reads
# _usage.json on every load; we just append. JSON isn't safe for
# concurrent writes, but the risk is a lost increment (not corruption)
# because odysseus reads with json.load which is atomic-enough on small
# files. Best-effort, fail-open.
_USAGE_FILE = (
    os.getenv("ODYSSEUS_DATA_DIR", str(_ODYSSEUS_ROOT / "data"))
    + "/skills/_usage.json"
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
    return _BRIDGED_TOOLS + [_RECORD_USAGE_TOOL]


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    # A1.3: handle usage recording directly (not via execute_tool_block)
    if name == "record_skill_usage":
        _record_usage(arguments.get("name", ""), arguments.get("owner", _OWNER))
        return [TextContent(type="text", text=json.dumps({"ok": True}))]

    from src.agent_tools import ToolBlock
    from src.tool_execution import execute_tool_block

    if name in _ALL_EXCLUDED:
        return [TextContent(
            type="text",
            text=json.dumps({"error": f"Tool '{name}' is not available via the bridge.", "exit_code": 1}),
        )]

    block = ToolBlock(tool_type=name, content=json.dumps(arguments))

    try:
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
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream)


if __name__ == "__main__":
    asyncio.run(main())
