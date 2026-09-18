"""
rag_server.py

MCP server exposing RAG document management (list, add_directory, remove_directory).
"""

import asyncio
import os
import sys
from pathlib import Path

from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.openclank.mcp_tool_server import ToolServer


server = ToolServer("rag")

_rag_manager = None
_personal_docs_manager = None
_initialized = False
_OWNER_ARG = "_open_clank_owner"
_WORKSPACE_ARG = "_open_clank_workspace_id"
_PROJECT_ARG = "_open_clank_project_id"


def _text(text: str) -> list[TextContent]:
    return [TextContent(type="text", text=text)]


def _scope(arguments: dict) -> tuple[str, str | None, str | None, str | None]:
    owner = str(arguments.get(_OWNER_ARG) or "").strip()
    workspace = str(arguments.get(_WORKSPACE_ARG) or "").strip() or None
    project = str(arguments.get(_PROJECT_ARG) or "").strip() or None
    if not owner:
        return "", workspace, project, "Error: manage_rag requires an authenticated owner"
    if project and not workspace:
        return owner, workspace, project, "Error: project scope requires workspace scope"
    return owner, workspace, project, None


def _ensure_init():
    """Lazy-init the canonical RAG manager on first use."""
    global _rag_manager, _initialized
    if _initialized:
        return
    _initialized = True

    try:
        from src.rag_singleton import get_rag_manager
        _rag_manager = get_rag_manager()
    except Exception:
        pass

@server.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="manage_rag",
            description="Manage canonical Frankenmemory RAG documents and derived embedding generations.",
            inputSchema={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["list", "status", "add_directory", "remove_directory", "rebuild_embeddings", "rollback_embedding"],
                        "description": "The action to perform",
                    },
                    "directory": {"type": "string", "description": "Directory path (for add/remove)"},
                    "generation_id": {"type": "string", "description": "Retained generation ID (for rollback_embedding)"},
                },
                "required": ["action"],
            },
        )
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    if name != "manage_rag":
        return _text(f"Unknown tool: {name}")

    arguments = arguments if isinstance(arguments, dict) else {}
    owner, workspace_id, project_id, scope_error = _scope(arguments)
    if scope_error:
        return _text(scope_error)
    _ensure_init()
    action = arguments.get("action", "")
    if not _rag_manager:
        return _text("Error: RAG manager not available")

    if action == "status":
        try:
            import json

            return _text(json.dumps(_rag_manager.get_stats(owner=owner), indent=2))
        except Exception as e:
            return _text(f"Error: {e}")

    if action == "list":
        try:
            sources = _rag_manager.list_sources(
                owner=owner,
                workspace_id=workspace_id,
                project_id=project_id,
            )
            if not sources:
                return _text("No documents indexed in this RAG scope.")
            lines = [f"**Indexed sources ({len(sources)}):**"]
            lines.extend(f"  - `{row['source_uri']}`" for row in sources[:50])
            if len(sources) > 50:
                lines.append(f"  ... and {len(sources) - 50} more")
            return _text("\n".join(lines))
        except Exception as e:
            return _text(f"Error: {e}")

    elif action == "add_directory":
        _dir = arguments.get("directory")
        directory = _dir.strip() if isinstance(_dir, str) else ""
        if not directory:
            return _text("Error: add_directory needs a directory path")
        # Store an absolute path so indexed `source` metadata is absolute and
        # remove_directory (which abspath-normalizes) can match it later (#1660).
        directory = os.path.abspath(os.path.expanduser(directory))
        if not os.path.isdir(directory):
            return _text(f"Error: Directory not found: {directory}")
        try:
            result = _rag_manager.index_personal_documents(
                directory,
                owner=owner,
                workspace_id=workspace_id,
                project_id=project_id,
            )
            if not isinstance(result, dict) or not result.get("success"):
                message = result.get("message", "indexing failed") if isinstance(result, dict) else "indexing failed"
                return _text(f"Error: Failed to index directory: {message}")
            indexed = result.get("indexed_count", 0) if isinstance(result, dict) else 0
            return _text(f"Directory '{directory}' added to RAG index ({indexed} chunks indexed)")
        except Exception as e:
            return _text(f"Error: Failed to index directory: {e}")

    elif action == "remove_directory":
        _dir = arguments.get("directory")
        directory = _dir.strip() if isinstance(_dir, str) else ""
        if not directory:
            return _text("Error: remove_directory needs a directory path")
        # Expand ~ to match add_directory, which indexes the expanded path.
        # Without this, removing "~/docs" never matches the stored absolute path.
        directory = os.path.abspath(os.path.expanduser(directory))
        try:
            result = _rag_manager.remove_directory(
                directory,
                owner=owner,
                workspace_id=workspace_id,
                project_id=project_id,
            )
            if not isinstance(result, dict) or not result.get("success"):
                message = result.get("message", "removal failed") if isinstance(result, dict) else "removal failed"
                return _text(f"Error: Failed to remove directory: {message}")
            return _text(
                f"Directory '{directory}' removed from RAG index "
                f"({result.get('removed', 0)} documents removed)"
            )
        except Exception as e:
            return _text(f"Error: Failed to remove directory: {e}")

    elif action == "rebuild_embeddings":
        try:
            result = _rag_manager.build_embedding_generation(
                owner=owner,
                workspace_id=workspace_id,
                project_id=project_id,
                publish=True,
            )
            return _text(
                f"Embedding generation {result['generation_id']} finished as "
                f"{result['state']}."
            )
        except Exception as e:
            return _text(f"Error: Failed to rebuild embeddings: {e}")

    elif action == "rollback_embedding":
        generation_id = str(arguments.get("generation_id") or "").strip()
        if not generation_id:
            return _text("Error: rollback_embedding needs generation_id")
        try:
            result = _rag_manager.rollback_generation(
                owner=owner, generation_id=generation_id
            )
            return _text(
                f"Embedding pointer rolled back to {result['generation_id']} "
                f"({result['currentness']})."
            )
        except Exception as e:
            return _text(f"Error: Failed to roll back embeddings: {e}")

    else:
        return _text(
            f"Error: Unknown action '{action}'. Use: list, status, add_directory, "
            "remove_directory, rebuild_embeddings, rollback_embedding"
        )


async def run():
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(run())
