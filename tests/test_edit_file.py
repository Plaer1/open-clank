"""edit_file: filesystem-write permission policy + behavior."""
import json
import os
import tempfile

import pytest

from src import tool_security
from src.tool_security import (
    NON_ADMIN_BLOCKED_TOOLS,
    is_public_blocked_tool,
    blocked_tools_for_owner,
)
from src.agent_tools.filesystem_tools import EditFileTool
from src.agent_tools import ToolBlock


# ── Permission policy ─────────────────────────────────────────────────────
def test_edit_file_is_sensitive_write_tool():
    # Must be blocked for non-admins exactly like write_file.
    assert "edit_file" in NON_ADMIN_BLOCKED_TOOLS
    assert is_public_blocked_tool("edit_file") is True


def test_blocked_tools_for_owner_includes_edit_file_for_non_admin(monkeypatch):
    monkeypatch.setattr(tool_security, "owner_is_admin_or_single_user", lambda owner: False)
    blocked = blocked_tools_for_owner("bob")
    assert "edit_file" in blocked and "write_file" in blocked
    # Admin / single-user gets nothing blocked.
    monkeypatch.setattr(tool_security, "owner_is_admin_or_single_user", lambda owner: True)
    assert blocked_tools_for_owner("admin") == set()


def test_non_admin_file_tools_open_only_for_assigned_visibility(tmp_path, monkeypatch):
    """An assignment changes tool advertisement; the Rust lane remains the authority."""
    from src.openclank import filesystem_registry

    monkeypatch.setattr(filesystem_registry, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(tool_security, "owner_is_admin_or_single_user", lambda owner: False)
    registry = filesystem_registry.FilesystemRootRegistry()
    root = tmp_path / "workspace"
    root.mkdir()
    record = registry.add("admin", str(root), "recursive_directory", ["read", "write"])
    registry.assign_visibility("admin", "bob", record["id"], ["read"])

    blocked = blocked_tools_for_owner("bob")
    assert {"read_file", "ls", "glob", "grep"}.isdisjoint(blocked)
    # A read-only assignment is still allowed to reach the handler; Rust will
    # deny write/edit at the capability check rather than falling back locally.
    assert "write_file" not in blocked
    assert "edit_file" not in blocked


def test_non_admin_without_assignment_stays_closed(tmp_path, monkeypatch):
    from src.openclank import filesystem_registry

    monkeypatch.setattr(filesystem_registry, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(tool_security, "owner_is_admin_or_single_user", lambda owner: False)
    filesystem_registry.FilesystemRootRegistry().path.parent.mkdir(parents=True)
    filesystem_registry.FilesystemRootRegistry().path.write_text(
        '{"version": 1, "generation": 0, "roots": {}, "visibility_assignments": {}}',
        encoding="utf-8",
    )

    blocked = blocked_tools_for_owner("bob")
    assert {"read_file", "write_file", "edit_file", "ls", "glob", "grep"}.issubset(blocked)


@pytest.mark.asyncio
async def test_edit_file_blocked_at_execution_for_non_admin(monkeypatch):
    # Execution-level gate: a non-admin owner must be refused even if the tool
    # reaches execute_tool_block. edit_file stays admin-gated by tool_security
    # after #2684 (ALWAYS_AVAILABLE only changed advertisement, not execution).
    #
    # Resolve execute_tool_block from the live module object (te) rather than a
    # top-level import: other test modules pop src.tool_execution from
    # sys.modules and re-import it, so a stale top-level reference would call a
    # different module's function than the one monkeypatch targets — silently
    # bypassing the admin gate.
    import src.tool_execution as te
    monkeypatch.setattr(te, "_owner_is_admin", lambda owner: False)
    ws = tempfile.mkdtemp()
    p = os.path.join("/tmp", "ef_block.txt")
    open(p, "w").write("a\n")
    _desc, result = await te.execute_tool_block(
        ToolBlock("edit_file", json.dumps({"path": p, "old_string": "a", "new_string": "b"})),
        owner="bob",
    )
    assert result.get("exit_code") == 1 and "admin" in result.get("error", "").lower()
    os.unlink(p)


@pytest.mark.asyncio
async def test_model_process_is_admitted_under_the_os_boundary(tmp_path, monkeypatch):
    """2026-08-14 owner ruling: a configured policy registry no longer denies
    model-directed process tools; confinement is the OS account's job and
    destructive commands still pass through interactive approval."""
    import src.tool_execution as te

    registry_path = tmp_path / "roots.json"
    registry_path.write_text(
        '{"version": 1, "generation": 0, "roots": {}, "visibility_assignments": {}}',
        encoding="utf-8",
    )
    monkeypatch.setenv("ODYSSEUS_FILES_REGISTRY", str(registry_path))
    monkeypatch.setattr(te, "_owner_is_admin", lambda owner: True)
    _desc, result = await te.execute_tool_block(
        ToolBlock("bash", "printf admission-ok"),
        owner="admin",
        session_id="chat-a",
    )
    assert result.get("exit_code") == 0, result
    assert "admission-ok" in str(result.get("output", ""))


# ── Behavior ──────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_edit_file_success():
    p = os.path.join("/tmp", "ef_ok.py")
    open(p, "w").write("def f():\n    return 1\n")
    res = await EditFileTool().execute(json.dumps({"path": p, "old_string": "return 1", "new_string": "return 2"}), {})
    assert res["exit_code"] == 0
    assert open(p).read() == "def f():\n    return 2\n"
    assert res["diff"]["added"] == 1 and res["diff"]["removed"] == 1 and res["diff"]["file"] == "ef_ok.py"
    os.unlink(p)


@pytest.mark.asyncio
async def test_edit_file_not_found():
    p = os.path.join("/tmp", "ef_nf.txt")
    open(p, "w").write("hello\n")
    res = await EditFileTool().execute(json.dumps({"path": p, "old_string": "nope", "new_string": "x"}), {})
    assert res["exit_code"] == 1 and "not found" in res["error"]
    os.unlink(p)


@pytest.mark.asyncio
async def test_edit_file_non_unique():
    p = os.path.join("/tmp", "ef_dup.txt")
    open(p, "w").write("x\nx\n")
    res = await EditFileTool().execute(json.dumps({"path": p, "old_string": "x", "new_string": "y"}), {})
    assert res["exit_code"] == 1 and "not unique" in res["error"]
    # replace_all resolves it
    res = await EditFileTool().execute(json.dumps({"path": p, "old_string": "x", "new_string": "y", "replace_all": True}), {})
    assert res["exit_code"] == 0 and open(p).read() == "y\ny\n"
    os.unlink(p)


@pytest.mark.asyncio
async def test_edit_file_outside_allowed_roots():
    res = await EditFileTool().execute(json.dumps({"path": "/etc/hosts", "old_string": "x", "new_string": "y"}), {})
    assert res["exit_code"] == 1 and ("outside the allowed roots" in res["error"] or "sensitive" in res["error"])
