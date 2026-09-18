"""Regression: rag_server add/remove_directory must not crash on a non-string path.

`directory = arguments.get("directory", "").strip()` runs before the surrounding
try, so a non-string `directory` in the tool args (e.g. a number) raised
AttributeError out of call_tool. Coerce non-strings to "".
"""
import asyncio
import json
from types import SimpleNamespace

import pytest

pytest.importorskip("mcp")

import mcp_servers.rag_server as rs


def _call(monkeypatch, action, directory):
    monkeypatch.setattr(rs, "_ensure_init", lambda: None)
    monkeypatch.setattr(rs, "_rag_manager", object())
    return asyncio.run(rs.call_tool("manage_rag", {
        "action": action,
        "directory": directory,
        rs._OWNER_ARG: "alice",
    }))


def test_add_directory_non_string_does_not_crash(monkeypatch):
    out = _call(monkeypatch, "add_directory", 123)
    assert "needs a directory path" in out[0].text


def test_remove_directory_non_string_does_not_crash(monkeypatch):
    out = _call(monkeypatch, "remove_directory", ["x"])
    assert "needs a directory path" in out[0].text


def test_rag_server_requires_private_authenticated_owner(monkeypatch, tmp_path):
    monkeypatch.setattr(rs, "_ensure_init", lambda: None)
    monkeypatch.setattr(rs, "_rag_manager", object())

    for action in (
        "list",
        "status",
        "add_directory",
        "remove_directory",
        "rebuild_embeddings",
        "rollback_embedding",
    ):
        out = asyncio.run(rs.call_tool(
            "manage_rag", {"action": action, "directory": str(tmp_path)}
        ))
        assert "requires an authenticated owner" in out[0].text


def test_rag_private_scope_fields_are_not_model_visible():
    tool = asyncio.run(rs.list_tools())[0]
    schema = getattr(tool, "input_schema", None)
    if schema is None:  # MCP 1 exposed the Pydantic alias as an attribute.
        schema = tool.inputSchema
    properties = schema["properties"]
    assert rs._OWNER_ARG not in properties
    assert rs._WORKSPACE_ARG not in properties
    assert rs._PROJECT_ARG not in properties


def test_rag_server_forwards_exact_scope_and_checks_results(monkeypatch, tmp_path):
    class _Rag:
        def __init__(self):
            self.calls = []

        def list_sources(self, **scope):
            self.calls.append(("list", scope))
            return [{"source_uri": str(tmp_path / "a.md")}]

        def index_personal_documents(self, directory, **scope):
            self.calls.append(("add", directory, scope))
            return {"success": True, "indexed_count": 1}

        def remove_directory(self, directory, **scope):
            self.calls.append(("remove", directory, scope))
            return {"success": True, "removed": 1}

    rag = _Rag()
    monkeypatch.setattr(rs, "_ensure_init", lambda: None)
    monkeypatch.setattr(rs, "_rag_manager", rag)
    base = {
        rs._OWNER_ARG: "alice",
        rs._WORKSPACE_ARG: "workspace",
        rs._PROJECT_ARG: "project",
    }
    for action in ("list", "add_directory", "remove_directory"):
        out = asyncio.run(rs.call_tool(
            "manage_rag", {**base, "action": action, "directory": str(tmp_path)}
        ))
        assert not out[0].text.startswith("Error:")

    scope = {"owner": "alice", "workspace_id": "workspace", "project_id": "project"}
    assert rag.calls == [
        ("list", scope),
        ("add", str(tmp_path), scope),
        ("remove", str(tmp_path), scope),
    ]


def test_rag_server_never_reports_failed_mutation_as_success(monkeypatch, tmp_path):
    class _Rag:
        def index_personal_documents(self, *_args, **_kwargs):
            return {"success": False, "message": "index rejected"}

        def remove_directory(self, *_args, **_kwargs):
            return {"success": False, "message": "remove rejected"}

    monkeypatch.setattr(rs, "_ensure_init", lambda: None)
    monkeypatch.setattr(rs, "_rag_manager", _Rag())
    for action in ("add_directory", "remove_directory"):
        out = asyncio.run(rs.call_tool("manage_rag", {
            "action": action,
            "directory": str(tmp_path),
            rs._OWNER_ARG: "alice",
        }))
        assert out[0].text.startswith("Error:")
        assert "rejected" in out[0].text


def test_rag_server_generation_operations_keep_authenticated_scope(monkeypatch):
    class _Rag:
        def __init__(self):
            self.calls = []

        def get_stats(self, **scope):
            self.calls.append(("status", scope))
            return {"healthy": True}

        def build_embedding_generation(self, **scope):
            self.calls.append(("build", scope))
            return {"generation_id": "gen-new", "state": "ready"}

        def rollback_generation(self, **scope):
            self.calls.append(("rollback", scope))
            return {
                "generation_id": scope["generation_id"],
                "currentness": "lagging",
            }

    rag = _Rag()
    monkeypatch.setattr(rs, "_ensure_init", lambda: None)
    monkeypatch.setattr(rs, "_rag_manager", rag)
    private = {
        rs._OWNER_ARG: "alice",
        rs._WORKSPACE_ARG: "workspace",
        rs._PROJECT_ARG: "project",
    }
    for arguments in (
        {**private, "action": "status"},
        {**private, "action": "rebuild_embeddings"},
        {
            **private,
            "action": "rollback_embedding",
            "generation_id": "gen-old",
        },
    ):
        result = asyncio.run(rs.call_tool("manage_rag", arguments))
        assert not result[0].text.startswith("Error:")

    assert rag.calls == [
        ("status", {"owner": "alice"}),
        (
            "build",
            {
                "owner": "alice",
                "workspace_id": "workspace",
                "project_id": "project",
                "publish": True,
            },
        ),
        ("rollback", {"owner": "alice", "generation_id": "gen-old"}),
    ]


def test_tool_execution_overwrites_model_supplied_rag_scope(monkeypatch, tmp_path):
    import src.tool_execution as tool_execution

    class _Mcp:
        def __init__(self):
            self.calls = []

        async def call_tool(self, name, arguments):
            self.calls.append((name, arguments))
            return {"exit_code": 0, "stdout": "ok"}

    mcp = _Mcp()
    monkeypatch.setattr(tool_execution, "get_mcp_manager", lambda: mcp)
    # This test isolates authenticated scope injection. Deployment policy still
    # restricts arbitrary MCP servers to admins before dispatch.
    monkeypatch.setattr(tool_execution, "is_public_blocked_tool", lambda _tool: False)
    block = SimpleNamespace(
        tool_type="mcp__rag__manage_rag",
        content=json.dumps({
            "action": "list",
            rs._OWNER_ARG: "mallory",
            rs._WORKSPACE_ARG: "/spoofed",
            rs._PROJECT_ARG: "spoofed",
        }),
    )
    asyncio.run(tool_execution.execute_tool_block(
        block,
        owner="alice",
        workspace=str(tmp_path),
    ))

    assert mcp.calls == [(
        "mcp__rag__manage_rag",
        {
            "action": "list",
            rs._OWNER_ARG: "alice",
            rs._WORKSPACE_ARG: str(tmp_path),
            rs._PROJECT_ARG: "",
        },
    )]
