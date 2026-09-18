import asyncio
import codecs
import json
import os
import pathlib

import pytest

from core.atomic_io import file_fingerprint
from src.agent_tools.filesystem_tools import (
    ApplyPatchTool,
    EditFileTool,
    GlobTool,
    GrepTool,
    FileToolResources,
    LsTool,
    ManageFilesTool,
    ReadFileTool,
    WriteFileTool,
    file_approval_binding,
    file_tool_resources,
    reject_file_approval_scope,
    resolve_file_approval,
    schedule_file_resources,
)
from src.constants import MAX_READ_CHARS
from src import tool_execution
from src.project_hex import activate_hex, register_project, resolve_hex

_FILE_CONTRACT = json.loads(
    (pathlib.Path(__file__).parent / "fixtures" / "file_tool_contract_v1.json")
    .read_text(encoding="utf-8")
)


async def _run(tool, args, ctx):
    return await tool.execute(json.dumps(args), ctx)


def _assert_file_contract(result, operation):
    contract = result["metadata"]["file"]
    assert set(_FILE_CONTRACT["required"]) <= set(contract)
    assert set(_FILE_CONTRACT["page_required"]) <= set(contract["page"])
    assert contract["contract"] == _FILE_CONTRACT["contract"]
    assert contract["operation"] == operation
    assert contract["operation"] in _FILE_CONTRACT["operations"]
    assert contract["kind"] in _FILE_CONTRACT["kinds"]
    assert contract["page"]["unit"] in _FILE_CONTRACT["page_units"]
    if contract["truncation_reason"] is not None:
        assert contract["truncation_reason"] in _FILE_CONTRACT["truncation_reasons"]
    if contract["search_mode"] is not None:
        assert contract["search_mode"] in _FILE_CONTRACT["search_modes"]
    return contract


def _approve_file_mutations(ctx):
    async def progress(payload):
        request = payload["data"]
        assert resolve_file_approval(
            request["request_id"],
            "once",
            owner=ctx["owner"],
            session_id=ctx["session_id"],
        )

    return {**ctx, "progress_cb": progress}


@pytest.mark.asyncio
async def test_file_workspace_approval_uses_stable_identity(tmp_path, monkeypatch):
    from src.agent_tools.filesystem_tools import _require_file_approval

    data_dir = tmp_path / "data"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    source = workspace / "note.txt"
    source.write_text("note", encoding="utf-8")
    monkeypatch.setattr("src.constants.DATA_DIR", str(data_dir))
    surfaced = []

    async def approve(payload):
        surfaced.append(payload)
        request = payload["data"]
        assert resolve_file_approval(
            request["request_id"],
            "workspace",
            owner="alice",
            session_id="chat-a",
        )

    base = {
        "owner": "alice",
        "session_id": "chat-a",
        "workspace": str(workspace),
        "authority_workspace_id": "workspace-a",
        "progress_cb": approve,
    }
    binding = await _require_file_approval(
        "delete",
        ctx=base,
        owner="alice",
        workspace=str(workspace),
        source=str(source),
    )
    assert surfaced
    assert await _require_file_approval(
        "delete",
        ctx={key: value for key, value in base.items() if key != "progress_cb"},
        owner="alice",
        workspace=str(workspace),
        source=str(source),
    ) == binding
    with pytest.raises(PermissionError, match="interactive approval"):
        await _require_file_approval(
            "delete",
            ctx={
                **{key: value for key, value in base.items() if key != "progress_cb"},
                "authority_workspace_id": "workspace-b",
            },
            owner="alice",
            workspace=str(workspace),
            source=str(source),
        )


@pytest.mark.asyncio
async def test_file_workspace_reset_rejects_pending_approval(tmp_path, monkeypatch):
    from src.agent_tools.filesystem_tools import _require_file_approval

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    source = workspace / "note.txt"
    source.write_text("note", encoding="utf-8")
    monkeypatch.setattr("src.constants.DATA_DIR", str(tmp_path / "data"))
    surfaced = asyncio.Event()

    async def wait_for_reset(_payload):
        surfaced.set()

    task = asyncio.create_task(_require_file_approval(
        "delete",
        ctx={
            "owner": "alice",
            "session_id": "chat-a",
            "workspace": str(workspace),
            "authority_workspace_id": "workspace-a",
            "progress_cb": wait_for_reset,
        },
        owner="alice",
        workspace=str(workspace),
        source=str(source),
    ))
    await surfaced.wait()
    assert reject_file_approval_scope(
        owner="alice", authority_workspace_id="workspace-a"
    ) == 1
    with pytest.raises(PermissionError, match="rejected"):
        await task


@pytest.fixture(autouse=True)
def _inline_to_thread(monkeypatch):
    async def run(func, *args, **kwargs):
        return func(*args, **kwargs)

    monkeypatch.setattr(asyncio, "to_thread", run)


@pytest.mark.asyncio
async def test_recoverable_delete_and_restore_are_workspace_owner_scoped(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "note.txt"
    target.write_text("keep me\n", encoding="utf-8")
    monkeypatch.setattr("src.constants.DATA_DIR", str(tmp_path / "data"))
    ctx = _approve_file_mutations({
        "owner": "alice",
        "workspace": str(workspace),
        "session_id": "chat-a",
    })
    token = tool_execution._active_workspace.set(str(workspace))
    try:
        deleted = await _run(
            ManageFilesTool(),
            {
                "action": "delete",
                "path": "note.txt",
                "expected_fingerprint": file_fingerprint(str(target)),
            },
            ctx,
        )
        assert deleted["exit_code"] == 0
        assert deleted["recoverable"] is True
        assert not target.exists()

        listed = await _run(ManageFilesTool(), {"action": "list_trash"}, ctx)
        assert [item["id"] for item in listed["items"]] == [deleted["trash_id"]]
        bob = {**ctx, "owner": "bob"}
        assert (await _run(ManageFilesTool(), {"action": "list_trash"}, bob))["items"] == []
        denied = await _run(
            ManageFilesTool(),
            {"action": "restore", "trash_id": deleted["trash_id"]},
            bob,
        )
        assert denied["exit_code"] == 1

        restored = await _run(
            ManageFilesTool(),
            {"action": "restore", "trash_id": deleted["trash_id"]},
            ctx,
        )
        assert restored["exit_code"] == 0
        assert target.read_text(encoding="utf-8") == "keep me\n"
        assert (await _run(ManageFilesTool(), {"action": "list_trash"}, ctx))["items"] == []
    finally:
        tool_execution._active_workspace.reset(token)


@pytest.mark.skipif(
    __import__("src.shell_policy", fromlist=["_working_network_bwrap"])
    ._working_network_bwrap() is None,
    reason="active Open Clank Hexes mutation checks require parser containment",
)
@pytest.mark.asyncio
async def test_active_hex_blocks_native_write_before_bytes_change(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / ".git").mkdir()
    target = workspace / "note.md"
    target.write_text("one\n", encoding="utf-8")
    (workspace / ".hex").write_text(
        "henxels:\n"
        "  - henxel: notes stay short\n"
        "    in: ./*.md\n"
        "    max_lines: 2\n",
        encoding="utf-8",
    )
    db = str(tmp_path / "fm.db")
    monkeypatch.setattr("src.constants.FM_DB_PATH", db)
    register_project(
        workspace,
        owner="alice",
        workspace_id="workspace",
        project_id="project",
        db_path=db,
    )
    activate_hex(
        resolve_hex(workspace),
        owner="alice",
        project_id="project",
        db_path=db,
    )
    ctx = {"owner": "alice", "workspace": str(workspace), "session_id": "chat-a"}
    token = tool_execution._active_workspace.set(str(workspace))
    try:
        blocked = await _run(
            WriteFileTool(),
            {"path": "note.md", "content": "one\ntwo\nthree\n"},
            ctx,
        )
        assert blocked["exit_code"] == 1
        assert blocked["blocked"] is True
        assert target.read_text(encoding="utf-8") == "one\n"
        allowed = await _run(
            WriteFileTool(),
            {"path": "note.md", "content": "one\ntwo\n"},
            ctx,
        )
        assert allowed["exit_code"] == 0
        assert target.read_text(encoding="utf-8") == "one\ntwo\n"
    finally:
        tool_execution._active_workspace.reset(token)


@pytest.mark.skipif(
    __import__("src.shell_policy", fromlist=["_working_network_bwrap"])
    ._working_network_bwrap() is None,
    reason="active Open Clank Hexes mutation checks require parser containment",
)
@pytest.mark.asyncio
async def test_active_hex_covers_edit_patch_move_and_delete_boundaries(
    tmp_path,
    monkeypatch,
):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / ".git").mkdir()
    target = workspace / "note.md"
    target.write_text("one\n", encoding="utf-8")
    (workspace / ".hex").write_text(
        "henxels:\n"
        "  - henxel: the note stays present\n"
        "    required_files: note.md\n"
        "  - henxel: notes stay short\n"
        "    in: ./*.md\n"
        "    max_lines: 2\n",
        encoding="utf-8",
    )
    db = str(tmp_path / "fm.db")
    monkeypatch.setattr("src.constants.FM_DB_PATH", db)
    monkeypatch.setattr("src.constants.DATA_DIR", str(tmp_path / "data"))
    register_project(
        workspace,
        owner="alice",
        workspace_id="workspace",
        project_id="project",
        db_path=db,
    )
    activate_hex(
        resolve_hex(workspace), owner="alice", project_id="project", db_path=db
    )
    ctx = _approve_file_mutations(
        {"owner": "alice", "workspace": str(workspace), "session_id": "chat-a"}
    )
    token = tool_execution._active_workspace.set(str(workspace))
    try:
        edited = await _run(
            EditFileTool(),
            {"path": "note.md", "old_string": "one", "new_string": "one\ntwo\nthree"},
            ctx,
        )
        assert edited["blocked"] is True

        patched = await _run(
            ApplyPatchTool(),
            {
                "patch_text": """*** Begin Patch
*** Update File: note.md
@@
-one
+one
+two
+three
*** End Patch"""
            },
            ctx,
        )
        assert patched["blocked"] is True

        moved = await _run(
            ManageFilesTool(),
            {"action": "move", "path": "note.md", "destination": "moved.md"},
            ctx,
        )
        assert moved["blocked"] is True
        deleted = await _run(
            ManageFilesTool(), {"action": "delete", "path": "note.md"}, ctx
        )
        assert deleted["blocked"] is True
        assert target.read_text(encoding="utf-8") == "one\n"
        assert not (workspace / "moved.md").exists()
    finally:
        tool_execution._active_workspace.reset(token)


@pytest.mark.asyncio
async def test_move_denial_is_owner_session_bound_and_has_no_side_effects(
    tmp_path,
):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    source = workspace / "source.txt"
    destination = workspace / "destination.txt"
    source.write_text("source", encoding="utf-8")
    seen = []

    async def progress(payload):
        request = payload["data"]
        seen.append(request)
        assert request["detail"] == {
            "action": "move",
            "source": str(source),
            "destination": str(destination),
            "source_fingerprint": file_fingerprint(str(source)),
        }
        assert source.read_text(encoding="utf-8") == "source"
        assert not destination.exists()
        assert not resolve_file_approval(
            request["request_id"],
            "once",
            owner="bob",
            session_id="chat-a",
        )
        assert not resolve_file_approval(
            request["request_id"],
            "once",
            owner="alice",
            session_id="other-chat",
        )
        assert resolve_file_approval(
            request["request_id"],
            "reject",
            owner="alice",
            session_id="chat-a",
        )

    ctx = {
        "owner": "alice",
        "workspace": str(workspace),
        "session_id": "chat-a",
        "progress_cb": progress,
    }
    token = tool_execution._active_workspace.set(str(workspace))
    try:
        result = await _run(
            ManageFilesTool(),
            {
                "action": "move",
                "path": "source.txt",
                "destination": "destination.txt",
            },
            ctx,
        )
    finally:
        tool_execution._active_workspace.reset(token)

    assert result["exit_code"] == 1
    assert "rejected" in result["error"]
    assert len(seen) == 1
    assert source.read_text(encoding="utf-8") == "source"
    assert not destination.exists()


@pytest.mark.asyncio
async def test_delete_approval_exposes_exact_trash_targets_before_mutation(
    tmp_path,
    monkeypatch,
):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    source = workspace / "source.txt"
    source.write_text("source", encoding="utf-8")
    data_dir = tmp_path / "data"
    monkeypatch.setattr("src.constants.DATA_DIR", str(data_dir))
    seen = []

    async def progress(payload):
        request = payload["data"]
        detail = request["detail"]
        seen.append(detail)
        assert detail["action"] == "delete"
        assert detail["source"] == str(source)
        assert detail["trash_target"].endswith(".data")
        assert detail["trash_manifest"].endswith(".json")
        assert source.read_text(encoding="utf-8") == "source"
        assert not pathlib.Path(detail["trash_target"]).exists()
        assert not pathlib.Path(detail["trash_manifest"]).exists()
        assert not data_dir.exists()
        assert resolve_file_approval(
            request["request_id"],
            "once",
            owner="alice",
            session_id="chat-a",
        )

    ctx = {
        "owner": "alice",
        "workspace": str(workspace),
        "session_id": "chat-a",
        "progress_cb": progress,
    }
    token = tool_execution._active_workspace.set(str(workspace))
    try:
        result = await _run(
            ManageFilesTool(),
            {"action": "delete", "path": "source.txt"},
            ctx,
        )
    finally:
        tool_execution._active_workspace.reset(token)

    assert result["exit_code"] == 0
    assert not source.exists()
    assert pathlib.Path(seen[0]["trash_target"]).read_text(encoding="utf-8") == "source"
    assert pathlib.Path(seen[0]["trash_manifest"]).is_file()


def test_file_approval_binding_covers_the_exact_operation_tuple(tmp_path):
    workspace = str(tmp_path / "workspace")
    base = file_approval_binding(
        "move",
        workspace=workspace,
        source=str(tmp_path / "source.txt"),
        destination=str(tmp_path / "destination.txt"),
        source_fingerprint="one",
    )
    variants = {
        file_approval_binding(
            "delete",
            workspace=workspace,
            source=str(tmp_path / "source.txt"),
            destination=str(tmp_path / "destination.txt"),
            source_fingerprint="one",
        ),
        file_approval_binding(
            "move",
            workspace=workspace,
            source=str(tmp_path / "other.txt"),
            destination=str(tmp_path / "destination.txt"),
            source_fingerprint="one",
        ),
        file_approval_binding(
            "move",
            workspace=workspace,
            source=str(tmp_path / "source.txt"),
            destination=str(tmp_path / "other-destination.txt"),
            source_fingerprint="one",
        ),
        file_approval_binding(
            "move",
            workspace=workspace,
            source=str(tmp_path / "source.txt"),
            destination=str(tmp_path / "destination.txt"),
            source_fingerprint="two",
        ),
    }
    assert base not in variants
    assert len(variants) == 4


@pytest.mark.asyncio
async def test_recoverable_file_actions_fail_closed_without_an_owner(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setattr("src.constants.DATA_DIR", str(tmp_path / "data"))
    token = tool_execution._active_workspace.set(str(workspace))
    try:
        result = await _run(
            ManageFilesTool(),
            {"action": "list_trash"},
            {"workspace": str(workspace), "session_id": "chat-a"},
        )
        assert result["exit_code"] == 1
        assert "authenticated owner" in result["error"]
    finally:
        tool_execution._active_workspace.reset(token)


@pytest.mark.asyncio
async def test_move_refuses_existing_destination_without_side_effects(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    source = workspace / "source.txt"
    destination = workspace / "destination.txt"
    source.write_text("source", encoding="utf-8")
    destination.write_text("destination", encoding="utf-8")
    ctx = {"owner": "alice", "workspace": str(workspace), "session_id": "chat-a"}
    token = tool_execution._active_workspace.set(str(workspace))
    try:
        result = await _run(
            ManageFilesTool(),
            {
                "action": "move",
                "path": "source.txt",
                "destination": "destination.txt",
                "expected_fingerprint": file_fingerprint(str(source)),
            },
            ctx,
        )
        assert result["conflict"] is True
        assert source.read_text(encoding="utf-8") == "source"
        assert destination.read_text(encoding="utf-8") == "destination"
    finally:
        tool_execution._active_workspace.reset(token)


@pytest.mark.asyncio
async def test_list_and_search_tools_return_cursor_envelopes(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    for index in range(3):
        (workspace / f"file-{index}.txt").write_text(f"needle {index}\n", encoding="utf-8")
    for target in workspace.iterdir():
        os.utime(target, (1_700_000_000, 1_700_000_000))
    ctx = {"owner": "alice", "workspace": str(workspace), "session_id": "chat-a"}
    token = tool_execution._active_workspace.set(str(workspace))
    try:
        listing = await _run(LsTool(), {"path": ".", "limit": 2}, ctx)
        assert len(listing["entries"]) == 2
        assert listing["page"]["has_more"] is True
        listing_contract = _assert_file_contract(listing, "list")
        assert listing_contract["page"]["returned"] == 2
        assert all(item["kind"] == "file" for item in listing_contract["items"])

        paths = await _run(GlobTool(), {"pattern": "*.txt", "limit": 2}, ctx)
        assert len(paths["paths"]) == 2
        assert paths["page"]["next_cursor"] == 2
        glob_contract = _assert_file_contract(paths, "glob")
        assert glob_contract["truncation_reason"] == "result_limit"
        remaining_paths = await _run(
            GlobTool(),
            {"pattern": "*.txt", "cursor": paths["page"]["next_cursor"], "limit": 2},
            ctx,
        )
        assert paths["paths"] + remaining_paths["paths"] == sorted(
            str(path) for path in workspace.glob("*.txt")
        )

        matches = await _run(GrepTool(), {"pattern": "needle", "limit": 2}, ctx)
        assert len(matches["matches"]) == 2
        assert all(item["line"] == 1 for item in matches["matches"])
        assert matches["page"]["next_cursor"] == 2
        grep_contract = _assert_file_contract(matches, "grep")
        assert grep_contract["search_mode"] == "regex"
        assert grep_contract["items"] == matches["matches"]
        remaining_matches = await _run(
            GrepTool(),
            {"pattern": "needle", "cursor": matches["page"]["next_cursor"], "limit": 2},
            ctx,
        )
        assert [
            item["path"] for item in matches["matches"] + remaining_matches["matches"]
        ] == sorted(str(path) for path in workspace.glob("*.txt"))

        (workspace / "literal.txt").write_text("a+b\nab\n", encoding="utf-8")
        literal = await _run(
            GrepTool(),
            {"pattern": "a+b", "mode": "literal"},
            ctx,
        )
        assert literal["search_mode"] == "literal"
        assert [item["text"] for item in literal["matches"]] == ["a+b"]
    finally:
        tool_execution._active_workspace.reset(token)


@pytest.mark.asyncio
async def test_write_and_edit_return_the_shared_file_contract(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "note.txt"
    ctx = {"owner": "alice", "workspace": str(workspace), "session_id": "chat-a"}
    token = tool_execution._active_workspace.set(str(workspace))
    try:
        written = await _run(
            WriteFileTool(),
            {"path": "note.txt", "content": "alpha\nbeta\n"},
            ctx,
        )
        write_contract = _assert_file_contract(written, "write")
        assert write_contract["path"] == str(target)
        assert write_contract["page"] == {
            "unit": "byte",
            "cursor": 0,
            "next_cursor": None,
            "has_more": False,
            "returned": 11,
            "total": 11,
        }
        assert write_contract["fingerprint"] == file_fingerprint(str(target))

        edited = await _run(
            EditFileTool(),
            {
                "path": "note.txt",
                "old_string": "beta",
                "new_string": "BETA",
                "expected_fingerprint": write_contract["fingerprint"],
            },
            ctx,
        )
        edit_contract = _assert_file_contract(edited, "edit")
        assert edit_contract["page"]["returned"] == 11
        assert edit_contract["fingerprint"] == file_fingerprint(str(target))
        assert target.read_text(encoding="utf-8") == "alpha\nBETA\n"
    finally:
        tool_execution._active_workspace.reset(token)


@pytest.mark.asyncio
async def test_read_cursor_and_binary_write_refusal(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    large = workspace / "large.txt"
    large.write_text("x" * (MAX_READ_CHARS + 50), encoding="utf-8")
    binary = workspace / "binary.txt"
    binary.write_bytes(b"text\x00binary")
    ctx = {"owner": "alice", "workspace": str(workspace), "session_id": "chat-a"}
    token = tool_execution._active_workspace.set(str(workspace))
    try:
        first = await _run(ReadFileTool(), {"path": "large.txt"}, ctx)
        assert first["page"]["has_more"] is True
        first_contract = _assert_file_contract(first, "read")
        assert first_contract["page"]["unit"] == "character"
        assert first_contract["truncation_reason"] == "character_limit"
        second = await _run(
            ReadFileTool(),
            {"path": "large.txt", "cursor": first["page"]["next_cursor"]},
            ctx,
        )
        assert second["output"].startswith("x" * 50)
        assert second["page"]["has_more"] is False

        binary_read = await _run(ReadFileTool(), {"path": "binary.txt"}, ctx)
        binary_contract = _assert_file_contract(binary_read, "read")
        assert binary_read["exit_code"] == 1
        assert binary_contract["kind"] == "binary"
        assert binary_contract["diagnostics"][0]["code"] == "unsupported_media"

        refused = await _run(
            WriteFileTool(),
            {"path": "binary.txt", "content": "replacement"},
            ctx,
        )
        assert refused["exit_code"] == 1
        assert "binary file" in refused["error"]
        assert binary.read_bytes() == b"text\x00binary"
    finally:
        tool_execution._active_workspace.reset(token)


@pytest.mark.asyncio
async def test_apply_patch_preserves_crlf_and_reports_fingerprints(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "note.txt"
    target.write_bytes(b"alpha\r\nbeta\r\n")
    ctx = {"owner": "alice", "workspace": str(workspace), "session_id": "chat-a"}
    token = tool_execution._active_workspace.set(str(workspace))
    try:
        result = await _run(
            ApplyPatchTool(),
            {
                "patch_text": """*** Begin Patch
*** Update File: note.txt
@@
 alpha
-beta
+BETA
*** End Patch"""
            },
            ctx,
        )
        assert result["exit_code"] == 0
        assert target.read_bytes() == b"alpha\r\nBETA\r\n"
        assert result["files"][0]["old_fingerprint"]
        assert result["files"][0]["fingerprint"] == file_fingerprint(str(target))
    finally:
        tool_execution._active_workspace.reset(token)


@pytest.mark.parametrize(
    ("bom", "encoding"),
    [
        (codecs.BOM_UTF8, "utf-8"),
        (codecs.BOM_UTF16_LE, "utf-16-le"),
        (codecs.BOM_UTF16_BE, "utf-16-be"),
    ],
)
@pytest.mark.asyncio
async def test_edit_preserves_bom_encoding_crlf_and_executable_mode(
    tmp_path,
    bom,
    encoding,
):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "script.txt"
    target.write_bytes(bom + "alpha\r\nbeta\r\n".encode(encoding))
    target.chmod(0o751)
    ctx = {"owner": "alice", "workspace": str(workspace), "session_id": "chat-a"}
    token = tool_execution._active_workspace.set(str(workspace))
    try:
        result = await _run(
            EditFileTool(),
            {
                "path": "script.txt",
                "old_string": "beta",
                "new_string": "BETA",
                "expected_fingerprint": file_fingerprint(str(target)),
            },
            ctx,
        )

        assert result["exit_code"] == 0
        assert target.read_bytes() == bom + "alpha\r\nBETA\r\n".encode(encoding)
        assert target.stat().st_mode & 0o777 == 0o751
    finally:
        tool_execution._active_workspace.reset(token)


@pytest.mark.asyncio
async def test_file_resource_scheduler_serializes_overlaps_and_runs_disjoint_paths(tmp_path):
    first = tmp_path / "first.txt"
    second = tmp_path / "second.txt"
    first.write_text("first", encoding="utf-8")
    second.write_text("second", encoding="utf-8")

    first_started = asyncio.Event()
    first_release = asyncio.Event()
    overlap_started = asyncio.Event()
    overlap_release = asyncio.Event()

    async def hold(resources, started, release):
        async with schedule_file_resources(resources):
            started.set()
            await release.wait()

    first_task = asyncio.create_task(
        hold(FileToolResources(reads=(str(first),)), first_started, first_release)
    )
    await first_started.wait()
    overlap_task = asyncio.create_task(
        hold(FileToolResources(writes=(str(first),)), overlap_started, overlap_release)
    )
    await asyncio.sleep(0)
    assert not overlap_started.is_set()
    first_release.set()
    await overlap_started.wait()
    overlap_release.set()
    await asyncio.gather(first_task, overlap_task)

    left_started = asyncio.Event()
    right_started = asyncio.Event()
    release_disjoint = asyncio.Event()
    left = asyncio.create_task(
        hold(FileToolResources(writes=(str(first),)), left_started, release_disjoint)
    )
    right = asyncio.create_task(
        hold(FileToolResources(writes=(str(second),)), right_started, release_disjoint)
    )
    await asyncio.gather(left_started.wait(), right_started.wait())
    release_disjoint.set()
    await asyncio.gather(left, right)


@pytest.mark.asyncio
async def test_registered_file_handler_waits_for_its_declared_resource(tmp_path):
    from src.agent_tools import TOOL_HANDLERS

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "target.txt"
    target.write_text("old\n", encoding="utf-8")
    ctx = {"owner": "alice", "workspace": str(workspace), "session_id": "chat-a"}
    held = asyncio.Event()
    release = asyncio.Event()

    async def hold_target():
        async with schedule_file_resources(FileToolResources(writes=(str(target),))):
            held.set()
            await release.wait()

    token = tool_execution._active_workspace.set(str(workspace))
    try:
        holder = asyncio.create_task(hold_target())
        await held.wait()
        reader = asyncio.create_task(
            TOOL_HANDLERS["read_file"](json.dumps({"path": "target.txt"}), ctx)
        )
        await asyncio.sleep(0)
        assert not reader.done()

        release.set()
        result = await reader
        await holder
        assert result["output"] == "old\n"
    finally:
        tool_execution._active_workspace.reset(token)


@pytest.mark.asyncio
async def test_file_resource_declarations_canonicalize_symlinks_and_cover_move_and_patch(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "target.txt"
    target.write_text("old\n", encoding="utf-8")
    alias = workspace / "alias.txt"
    alias.symlink_to(target)
    ctx = {"owner": "alice", "workspace": str(workspace), "session_id": "chat-a"}
    token = tool_execution._active_workspace.set(str(workspace))
    try:
        read = file_tool_resources("read_file", json.dumps({"path": "alias.txt"}), ctx)
        write = file_tool_resources("write_file", json.dumps({"path": "target.txt", "content": "new"}), ctx)
        assert read.reads == write.reads == write.writes == (str(target),)

        move = file_tool_resources(
            "manage_files",
            json.dumps({"action": "move", "path": "target.txt", "destination": "moved.txt"}),
            ctx,
        )
        assert move.reads == (str(target),)
        assert set(move.writes) == {str(target), str(workspace / "moved.txt")}

        patch = file_tool_resources(
            "apply_patch",
            """*** Begin Patch
*** Update File: target.txt
@@
-old
+new
*** Add File: added.txt
+added
*** End Patch""",
            ctx,
        )
        assert patch.reads == (str(target),)
        assert set(patch.writes) == {str(target), str(workspace / "added.txt")}
    finally:
        tool_execution._active_workspace.reset(token)
