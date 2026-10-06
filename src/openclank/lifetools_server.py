"""lifetools_server.py — MCP bridge exposing Open Clank life-tools.

Per-session spawned server (env-baked context). Exposes all FUNCTION_TOOL_SCHEMAS
minus direct process/unbrokered native overlaps and ACP-internal tools. File
read/write/edit/list/search are the Rust-backed AgentScope lane and surface as
lifetools:<tool>; mimo's direct OS implementations stay disabled.
"""

import asyncio
import base64
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any

# This file is launched directly by the per-session MCP descriptor.  In that
# mode Python adds ``src/openclank`` (not the repository root) to sys.path, so
# every ``src.*`` import below would fail unless the root is installed first.
_OPEN_CLANK_ROOT = Path(__file__).resolve().parents[2]
if _OPEN_CLANK_ROOT.exists() and str(_OPEN_CLANK_ROOT) not in sys.path:
    sys.path.insert(0, str(_OPEN_CLANK_ROOT))

from mcp.server.stdio import stdio_server
from mcp.types import CallToolResult, Tool, TextContent

from src.openclank.mcp_tool_server import ToolServer

from src.tool_schemas import (
    FUNCTION_TOOL_SCHEMAS,
    OPENTHESIUS_BRIDGE_EXCLUDED_TOOLS,
    function_call_to_tool_block,
)
from src.frankenmemory_provider import BROKER_MEMORY_TOOLS

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
_SKILL_OWNER = (
    os.environ.get("OPEN_CLANK_SKILL_OWNER", "").strip()
    if "OPEN_CLANK_SKILL_OWNER" in os.environ
    else _OWNER.strip()
)
_WORKSPACE = os.environ.get("WORKSPACE", "")
_AUTHORITY_WORKSPACE_ID = os.environ.get(
    "OPEN_CLANK_AUTHORITY_WORKSPACE_ID", ""
).strip()
_COPAL_WORKSPACE = os.environ.get("COPAL_WORKSPACE", "default").strip() or "default"
_MEMORY_PROVIDER = None
_MEMORY_LIFECYCLE = None
_MEMORY_PROVIDER_LOCK = None
_MEMORY_RECONCILE_LOCK = None
_MEMORY_RECONCILED_LIFECYCLE = None

_PROJECT_MUTATION_POLICY_TOOL = "project_mutation_policy"

_EXPLICIT_SKILLS_DIR = os.getenv("OPEN_CLANK_SKILLS_DIR", "").strip()
_DATA_DIR = str(
    Path(_EXPLICIT_SKILLS_DIR).parent
    if _EXPLICIT_SKILLS_DIR
    else Path(
        os.getenv("OPEN_CLANK_DATA_DIR")
        or os.getenv("ODYSSEUS_DATA_DIR")
        or _OPEN_CLANK_ROOT / "data"
    )
)

server = ToolServer("lifetools")


async def _render_search_identity(result: Any, *, owner: str, workspace_id: str) -> None:
    """Render reserved identity tokens in a search projection, never storage.

    MiMo consumes the private broker's ``search`` wire directly.  Keep a raw
    companion for diagnostics/edit round-trips while making the ordinary
    snippet say the Handler's current reviewed name instead of ``%USER%``.
    """
    if not isinstance(result, dict) or not isinstance(result.get("results"), list):
        return
    try:
        from services.memory.principal_context import render_identity_template
        provider = await _ensure_memory_provider()
        label = await provider.handler_display_label(owner=owner)
    except Exception:
        logger.debug("lifetools search identity rendering unavailable", exc_info=True)
        return
    for hit in result["results"]:
        record = hit.get("record") if isinstance(hit, dict) else None
        if not isinstance(record, dict):
            continue
        content = record.get("content")
        if not isinstance(content, str) or "%" not in content:
            continue
        rendered = render_identity_template(content, handler_label=label)
        if rendered != content:
            record["raw_content"] = content
            record["content"] = rendered


async def _ensure_memory_lifecycle_reconciled() -> None:
    """Reconcile shared lifecycle journals once per installed coordinator."""
    global _MEMORY_RECONCILE_LOCK, _MEMORY_RECONCILED_LIFECYCLE

    lifecycle = _MEMORY_LIFECYCLE
    if lifecycle is None or _MEMORY_RECONCILED_LIFECYCLE is lifecycle:
        return
    if _MEMORY_RECONCILE_LOCK is None:
        _MEMORY_RECONCILE_LOCK = asyncio.Lock()
    async with _MEMORY_RECONCILE_LOCK:
        lifecycle = _MEMORY_LIFECYCLE
        if lifecycle is None or _MEMORY_RECONCILED_LIFECYCLE is lifecycle:
            return
        reconcile = getattr(lifecycle, "reconcile", None)
        if callable(reconcile):
            counts = await reconcile()
            if isinstance(counts, dict) and int(counts.get("errors") or 0):
                raise RuntimeError(
                    "memory lifecycle reconciliation has unresolved errors"
                )
        _MEMORY_RECONCILED_LIFECYCLE = lifecycle


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
    description="Validate and record an exact active Open Clank skill revision.",
    inputSchema={
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "The skill name (slug)"},
            "skill_id": {"type": "string"},
            "revision": {"type": "integer", "minimum": 1},
            "content_hash": {"type": "string"},
        },
        "required": ["name", "skill_id", "revision", "content_hash"],
    },
)

# Internal memory contract used by MiMo's memory layer. These names are
# intentionally absent from list_tools(): the model sees the friendly
# recall/manage tools, while trusted runtime plumbing reuses the already
# connected lifetools transport instead of spawning another fm-mcp process.
async def _ensure_memory_provider():
    """Install one owner-scoped Frankenmemory provider in this MCP process."""
    global _MEMORY_LIFECYCLE, _MEMORY_PROVIDER, _MEMORY_PROVIDER_LOCK

    if not _OWNER.strip():
        raise RuntimeError("lifetools memory requires an authenticated owner")
    if _MEMORY_PROVIDER is not None:
        if _MEMORY_LIFECYCLE is None:
            from services.memory.forget_coordinator import (
                MemoryLifecycleCoordinator,
            )
            from services.memory.skills import SkillsManager

            _MEMORY_LIFECYCLE = MemoryLifecycleCoordinator(
                _MEMORY_PROVIDER,
                SkillsManager(_DATA_DIR),
                skill_owner=_SKILL_OWNER,
            )
        from src.ai_interaction import set_memory_manager

        set_memory_manager(
            None,
            provider=_MEMORY_PROVIDER,
            lifecycle=_MEMORY_LIFECYCLE,
        )
        await _ensure_memory_lifecycle_reconciled()
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
            from services.memory.forget_coordinator import (
                MemoryLifecycleCoordinator,
            )
            from services.memory.skills import SkillsManager

            _MEMORY_LIFECYCLE = MemoryLifecycleCoordinator(
                provider,
                SkillsManager(_DATA_DIR),
                skill_owner=_SKILL_OWNER,
            )

        from src.ai_interaction import set_memory_manager

        set_memory_manager(
            None,
            provider=_MEMORY_PROVIDER,
            lifecycle=_MEMORY_LIFECYCLE,
        )
        await _ensure_memory_lifecycle_reconciled()
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


def _record_usage(
    name: str,
    *,
    skill_id: str = "",
    revision: int | None = None,
    content_hash: str = "",
) -> dict:
    """Record usage through the owner-scoped immutable lifecycle contract."""
    try:
        if not _OWNER.strip() or not str(name or "").strip():
            return {"ok": False}
        from services.memory.skills import SkillsManager

        manager = SkillsManager(_DATA_DIR)
        requested = str(name).strip()
        expected_id = str(skill_id or "").strip()
        expected_hash = str(content_hash or "").strip()
        if not expected_id or revision is None or not expected_hash:
            return {"ok": False}
        skill = next(
            (
                row for row in manager.load_published(owner=_SKILL_OWNER)
                if requested in (
                    str(row.get("name") or ""),
                    str(row.get("skill_id") or ""),
                )
            ),
            None,
        )
        if skill is None:
            return {"ok": False}
        if (
            expected_id != str(skill.get("skill_id") or "")
        ) or (
            int(revision) != int(skill["revision"])
        ) or (
            expected_hash != str(skill.get("content_hash") or "")
        ):
            return {"ok": False}
        result = manager.record_active_use(
            str(skill.get("skill_id") or skill["name"]),
            owner=_SKILL_OWNER,
            revision=int(revision),
            content_hash=expected_hash,
        )
        return result or {"ok": False}
    except Exception as e:
        logger.debug("record_skill_usage failed (non-fatal): %s", e)
        return {"ok": False}


@server.list_tools()
async def list_tools() -> list[Tool]:
    return _BRIDGED_TOOLS + [_RECALL_MEMORY_TOOL, _RECORD_USAGE_TOOL]


@server.call_tool()
async def call_tool(
    name: str, arguments: dict
) -> list[TextContent] | CallToolResult:
    # Private runtime bridge used by MiMo's native mutators.  It is deliberately
    # absent from list_tools(): the model cannot select scope or invoke policy;
    # the session-bound tool implementations submit exact candidate bytes.
    if name == _PROJECT_MUTATION_POLICY_TOOL:
        try:
            if not _OWNER.strip() or not _WORKSPACE.strip():
                raise PermissionError(
                    "project policy requires an authenticated owner and workspace"
                )
            mode = str((arguments or {}).get("mode") or "").strip()
            from src.constants import FM_DB_PATH

            if mode == "files":
                from src.project_hex import validate_registered_project_candidates

                rows = (arguments or {}).get("candidates")
                if not isinstance(rows, list) or not rows:
                    raise ValueError("file policy requires exact candidates")
                candidates = {}
                for row in rows:
                    if not isinstance(row, dict):
                        raise ValueError("file policy candidate must be an object")
                    path = str(row.get("path") or "").strip()
                    if not path or path in candidates:
                        raise ValueError("file policy candidate path is missing or duplicated")
                    if row.get("deleted") is True:
                        if row.get("content_base64") is not None:
                            raise ValueError("deleted candidate cannot include content")
                        candidates[path] = None
                        continue
                    encoded = row.get("content_base64")
                    if not isinstance(encoded, str):
                        raise ValueError("file policy candidate content is missing")
                    candidates[path] = base64.b64decode(encoded, validate=True)
                result = validate_registered_project_candidates(
                    owner=_OWNER.strip(),
                    workspace=_WORKSPACE,
                    db_path=FM_DB_PATH,
                    candidates=candidates,
                )
            elif mode == "shell":
                from src.project_hex import inspect_registered_project_mutation_policy

                result = inspect_registered_project_mutation_policy(
                    owner=_OWNER.strip(),
                    workspace=_WORKSPACE,
                    db_path=FM_DB_PATH,
                )
                result = {
                    **result,
                    "allowed": not bool(result.get("enforced")),
                    "reason": (
                        "active project policy blocks MiMo shell execution; use native file tools"
                        if result.get("enforced")
                        else ""
                    ),
                }
            else:
                raise ValueError("project policy mode must be files or shell")
            from src.project_hex import global_policy_context

            # This is a policy/system projection, never a memory or RAG hit.
            # Keep it alongside the private mutation response so the agent can
            # explain the canonical boundary without being able to alter it.
            result = {
                **result,
                "policy_context": global_policy_context(
                    owner=_OWNER.strip(),
                    workspace=_WORKSPACE,
                    db_path=FM_DB_PATH,
                ),
            }
        except Exception as exc:
            logger.error("lifetools project policy dispatch failed: %s", exc)
            return CallToolResult(
                content=[
                    TextContent(
                        type="text",
                        text=json.dumps(
                            {"error": "project policy unavailable", "exit_code": 1}
                        ),
                    )
                ],
                isError=True,
            )
        return [TextContent(type="text", text=json.dumps(result))]

    # A1.3: handle usage recording directly (not via execute_tool_block)
    if name == "record_skill_usage":
        result = _record_usage(
            arguments.get("name", ""),
            skill_id=arguments.get("skill_id", ""),
            revision=arguments.get("revision"),
            content_hash=arguments.get("content_hash", ""),
        )
        return [TextContent(type="text", text=json.dumps(result))]

    if name in BROKER_MEMORY_TOOLS:
        try:
            provider = await _ensure_memory_provider()
            scoped = dict(arguments or {})
            requested_owner = str(scoped.get("owner") or "").strip()
            requested_workspace = str(scoped.get("workspace_id") or "").strip()
            from src.memory_scope import chat_workspace

            expected_workspace = (
                os.environ.get("FM_WORKSPACE_ID", "").strip()
                or chat_workspace()
            )
            if requested_owner and requested_owner != _OWNER:
                raise PermissionError("memory owner does not match the lifetools session")
            if (
                requested_workspace
                and expected_workspace
                and requested_workspace != expected_workspace
            ):
                raise PermissionError(
                    "memory workspace does not match the lifetools session"
                )
            if name != "memory_quality":
                scoped["owner"] = _OWNER
                scoped["workspace_id"] = expected_workspace
            elif bool(scoped.get("rebuild_graph_fts")):
                raise PermissionError(
                    "global memory maintenance is unavailable through a session bridge"
                )
            if name == "delete_memory":
                result = await _MEMORY_LIFECYCLE.delete(
                    str(scoped.get("id") or ""),
                    owner=_OWNER,
                    workspace_id=expected_workspace or None,
                )
                result = {"deleted": result is not None, "result": result}
            elif name == "review_candidate":
                if not isinstance(scoped.get("accept"), bool):
                    raise ValueError("candidate review requires a boolean accept value")
                result = await provider.review_candidate(
                    str(scoped.get("id") or ""),
                    accept=scoped["accept"],
                    reason=str(scoped.get("reason") or "reviewed_by_agent"),
                    owner=_OWNER,
                    workspace_id=expected_workspace,
                )
            elif name == "update_candidate":
                candidate = await provider.update_candidate(
                    str(scoped.get("id") or ""),
                    text=str(scoped.get("content") or ""),
                    category=scoped.get("category"),
                    reason=str(scoped.get("reason") or "edited_by_agent"),
                    owner=_OWNER,
                    workspace_id=expected_workspace,
                )
                result = {"updated": True, "candidate": candidate}
            elif name == "resolve_memory":
                resolved = await provider.resolve_question(
                    str(scoped.get("id") or ""),
                    answer=str(scoped.get("answer") or "").strip() or None,
                    resolved_by=str(scoped.get("resolved_by") or "").strip() or None,
                    expected_revision=scoped.get("expected_revision"),
                    owner=_OWNER,
                )
                result = {"resolved": bool(resolved)}
            elif name == "reopen_memory":
                expected_revision = scoped.get("expected_revision")
                if expected_revision is None:
                    detail = await provider.versioned_detail(
                        str(scoped.get("id") or ""), owner=_OWNER
                    )
                    expected_revision = detail["current_revision"]
                detail = await provider.reopen_question(
                    str(scoped.get("id") or ""),
                    expected_revision=int(expected_revision),
                    owner=_OWNER,
                )
                result = {"reopened": True, "memory": detail}
            elif name == "memory_forget":
                result = await _MEMORY_LIFECYCLE.forget(
                    str(scoped.get("action") or ""),
                    owner=_OWNER,
                    workspace_id=expected_workspace or None,
                    selector_kind=scoped.get("selector_kind"),
                    selector=scoped.get("selector"),
                    preview_token=scoped.get("preview_token"),
                    tombstone_id=scoped.get("tombstone_id"),
                    operation_id=scoped.get("operation_id"),
                )
            elif name == "memory_retention" and scoped.get("action") in {
                "preview_expire",
                "expire",
                "status",
            }:
                action = str(scoped.get("action"))
                if action == "expire" and not scoped.get("preview_token"):
                    result = await _MEMORY_LIFECYCLE.expire_retention(
                        owner=_OWNER,
                        workspace_id=expected_workspace,
                    )
                else:
                    result = await _MEMORY_LIFECYCLE.retention(
                        action,
                        owner=_OWNER,
                        workspace_id=expected_workspace,
                        preview_token=scoped.get("preview_token"),
                        operation_id=scoped.get("operation_id"),
                    )
            elif (
                name == "owner_lifecycle"
                and scoped.get("action") in {"purge", "rename"}
            ):
                raise PermissionError(
                    "owner mutation is unavailable through a session bridge"
                )
            else:
                result = await provider.invoke_tool(name, scoped)
                if name == "search":
                    await _render_search_identity(
                        result,
                        owner=_OWNER,
                        workspace_id=expected_workspace,
                    )
        except Exception as exc:
            logger.error("lifetools memory dispatch error for %s: %s", name, exc)
            return CallToolResult(
                content=[
                    TextContent(
                        type="text",
                        text=json.dumps(
                            {"error": "memory transport failed", "exit_code": 1}
                        ),
                    )
                ],
                isError=True,
            )
        return [TextContent(type="text", text=json.dumps(result))]

    if name in {"read_copal", "manage_copal"}:
        scoped = dict(arguments or {})
        requested_workspace = str(scoped.get("workspace") or "").strip()
        if requested_workspace and requested_workspace != _COPAL_WORKSPACE:
            return [TextContent(text=json.dumps({
                "error": "Copal workspace does not match the execution's pinned workspace",
                "code": "copal_workspace_mismatch",
                "expected": _COPAL_WORKSPACE,
                "received": requested_workspace,
                "exit_code": 1,
            }))]
        scoped["workspace"] = _COPAL_WORKSPACE
        arguments = scoped

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
            authority_workspace_id=_AUTHORITY_WORKSPACE_ID,
            copal_workspace=_COPAL_WORKSPACE,
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
