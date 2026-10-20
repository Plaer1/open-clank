"""Workspace confinement.

The agent's per-turn workspace is a single context-local binding set in
execute_tool_block. The shared path resolvers (_resolve_tool_path /
_resolve_search_root) and the subprocess cwd helper (agent_cwd) read it, so
confinement is enforced in ONE place: a tool that uses the shared helpers is
confined automatically and a new tool cannot accidentally bypass it.

Covers: the resolver helper, the central binding (the safety net), end-to-end
confinement of read/write/edit/grep/ls + subprocess cwd via execute_tool_block,
the get_workspace tool, no-leak across calls, and the admin-gated browse route.
"""
import json
import os
import stat
import tempfile
from types import SimpleNamespace

import pytest

from src.tool_execution import (
    _AGENT_WORKDIR,
    _active_workspace,
    _resolve_search_root,
    _resolve_tool_path,
    _resolve_tool_path_in_workspace,
    agent_cwd,
    execute_tool_block,
    get_active_workspace,
)


def _block(tool, content=""):
    return SimpleNamespace(tool_type=tool, content=content)


def _patch_managed_agent_route(monkeypatch, agent_loop):
    monkeypatch.setattr(
        agent_loop,
        "resolve_chat_route",
        lambda **kwargs: SimpleNamespace(
            provider_model_id=kwargs.get("model_id") or "gpt-test",
            model_route_id="route-test",
            provider_grant_id=None,
            connection_id="connection-test",
            runtime_model=f"connection-test/{kwargs.get('model_id') or 'gpt-test'}",
            capabilities={"tools": True},
        ),
    )


@pytest.fixture
def ws():
    d = tempfile.mkdtemp()
    with open(os.path.join(d, "a.txt"), "w") as f:
        f.write("x")
    return d


@pytest.fixture
def admin(monkeypatch):
    """Pass the public-tool gate so file tools dispatch in tests."""
    monkeypatch.setattr(
        "src.tool_execution.owner_is_admin_or_single_user", lambda owner: True
    )


# ── the resolver helper ────────────────────────────────────────────────

def test_resolver_confines(ws):
    real = os.path.realpath(os.path.join(ws, "a.txt"))
    assert _resolve_tool_path_in_workspace(ws, "a.txt") == real          # relative
    assert _resolve_tool_path_in_workspace(ws, os.path.join(ws, "a.txt")) == real  # abs inside
    outside = tempfile.mkdtemp()
    with pytest.raises(ValueError):                                       # abs outside
        _resolve_tool_path_in_workspace(ws, os.path.join(outside, "x.txt"))
    with pytest.raises(ValueError):                                       # parent escape
        _resolve_tool_path_in_workspace(ws, os.path.join("..", "..", "escape.txt"))


def test_resolver_blocks_sensitive_inside_workspace(ws):
    os.makedirs(os.path.join(ws, ".ssh"), exist_ok=True)
    with pytest.raises(ValueError):
        _resolve_tool_path_in_workspace(ws, ".ssh/authorized_keys")


# ── the central binding: the safety net ─────────────────────────────────

def test_active_binding_confines_shared_resolvers(ws):
    """ANY tool resolving paths through the shared helpers is confined while the
    binding is active, without doing anything workspace-specific itself. This is
    what stops a newly added tool from accidentally ignoring the workspace."""
    token = _active_workspace.set(ws)
    try:
        assert get_active_workspace() == ws
        assert agent_cwd() == ws
        assert _resolve_tool_path("a.txt") == os.path.realpath(os.path.join(ws, "a.txt"))
        with pytest.raises(ValueError):          # normally-allowed root, now outside ws
            _resolve_tool_path("/tmp/whatever.txt")
        assert _resolve_search_root("") == os.path.realpath(ws)
    finally:
        _active_workspace.reset(token)


def test_no_binding_uses_default_roots():
    assert get_active_workspace() is None
    assert agent_cwd() == _AGENT_WORKDIR
    with pytest.raises(ValueError):
        _resolve_tool_path("/etc/hosts")


# ── end-to-end via execute_tool_block (sets + resets the binding) ───────

@pytest.mark.asyncio
async def test_read_write_edit_confined_e2e(ws, admin):
    _, r = await execute_tool_block(_block("write_file", "note.txt\nhello"), owner="a", workspace=ws)
    assert r["exit_code"] == 0 and os.path.isfile(os.path.join(ws, "note.txt"))
    _, r = await execute_tool_block(_block("read_file", "note.txt"), owner="a", workspace=ws)
    assert r["exit_code"] == 0 and r["output"] == "hello"

    with open(os.path.join(ws, "f.txt"), "w") as f:
        f.write("foo bar")
    _, r = await execute_tool_block(
        _block("edit_file", json.dumps({"path": "f.txt", "old_string": "foo", "new_string": "baz"})),
        owner="a", workspace=ws,
    )
    assert r["exit_code"] == 0
    with open(os.path.join(ws, "f.txt")) as f:
        assert f.read() == "baz bar"

    # outside the workspace is rejected, and nothing is created
    outside = tempfile.mkdtemp()
    of = os.path.join(outside, "secret.txt")
    with open(of, "w") as f:
        f.write("nope")
    _, r = await execute_tool_block(_block("read_file", of), owner="a", workspace=ws)
    assert r["exit_code"] == 1 and "outside the workspace" in r["error"]
    escape = os.path.join(outside, "_esc.txt")
    _, r = await execute_tool_block(_block("write_file", f"{escape}\nx"), owner="a", workspace=ws)
    assert r["exit_code"] == 1 and "outside the workspace" in r["error"]
    assert not os.path.exists(escape)


@pytest.mark.asyncio
async def test_file_tools_preserve_format_mode_and_reject_stale_fingerprint(ws, admin):
    target = os.path.join(ws, "script.txt")
    raw = b"\xef\xbb\xbfalpha\r\nbeta\r\n"
    with open(target, "wb") as handle:
        handle.write(raw)
    os.chmod(target, 0o751)

    _, read = await execute_tool_block(
        _block("read_file", json.dumps({"path": "script.txt"})),
        owner="a",
        workspace=ws,
    )
    assert read["exit_code"] == 0
    assert read["encoding"] == "utf-8"
    assert read["newline"] == "crlf"
    fingerprint = read["fingerprint"]

    _, edited = await execute_tool_block(
        _block(
            "edit_file",
            json.dumps({
                "path": "script.txt",
                "old_string": "beta",
                "new_string": "BETA",
                "expected_fingerprint": fingerprint,
            }),
        ),
        owner="a",
        workspace=ws,
    )
    assert edited["exit_code"] == 0
    assert open(target, "rb").read() == b"\xef\xbb\xbfalpha\r\nBETA\r\n"
    assert stat.S_IMODE(os.stat(target).st_mode) == 0o751

    with open(target, "ab") as handle:
        handle.write(b"external\r\n")
    _, stale = await execute_tool_block(
        _block(
            "edit_file",
            json.dumps({
                "path": "script.txt",
                "old_string": "alpha",
                "new_string": "ALPHA",
                "expected_fingerprint": fingerprint,
            }),
        ),
        owner="a",
        workspace=ws,
    )
    assert stale["exit_code"] == 1
    assert stale["conflict"] is True
    assert b"ALPHA" not in open(target, "rb").read()


@pytest.mark.asyncio
async def test_apply_patch_confined_e2e(ws, admin):
    with open(os.path.join(ws, "patchme.txt"), "w") as f:
        f.write("alpha\nbeta\ngamma\n")
    patch = """*** Begin Patch
*** Update File: patchme.txt
@@
 alpha
-beta
+BETA
 gamma
*** Add File: added.txt
+new file
*** End Patch"""
    _, r = await execute_tool_block(_block("apply_patch", patch), owner="a", workspace=ws)
    assert r["exit_code"] == 0
    assert r["diff"]["added"] >= 2
    with open(os.path.join(ws, "patchme.txt")) as f:
        assert f.read() == "alpha\nBETA\ngamma\n"
    with open(os.path.join(ws, "added.txt")) as f:
        assert f.read() == "new file\n"

    outside = tempfile.mkdtemp()
    outside_file = os.path.join(outside, "x.txt")
    with open(outside_file, "w") as f:
        f.write("x\n")
    escape_patch = f"""*** Begin Patch
*** Update File: {outside_file}
@@
-x
+y
*** End Patch"""
    _, r = await execute_tool_block(_block("apply_patch", escape_patch), owner="a", workspace=ws)
    assert r["exit_code"] == 1 and "outside the workspace" in r["error"]
    with open(outside_file) as f:
        assert f.read() == "x\n"


@pytest.mark.asyncio
async def test_todowrite_persists_session_list(tmp_path, monkeypatch, admin):
    import src.agent_tools.coding_tools as coding_tools

    monkeypatch.setattr(coding_tools, "_TODO_DIR", str(tmp_path))
    payload = {
        "todos": [
            {"content": "Inspect code", "status": "completed", "priority": "high"},
            {"content": "Patch code", "status": "in_progress", "priority": "high"},
        ]
    }
    _, r = await execute_tool_block(
        _block("todowrite", json.dumps(payload)),
        session_id="chat/one",
        owner="a",
        workspace=str(tmp_path),
    )
    assert r["exit_code"] == 0
    assert "[>] Patch code" in r["output"]
    saved = json.load(open(tmp_path / "chat_one.json", encoding="utf-8"))
    assert saved["todos"][1]["status"] == "in_progress"


@pytest.mark.asyncio
async def test_grep_and_ls_confined_e2e(ws, admin):
    with open(os.path.join(ws, "doc.txt"), "w") as f:
        f.write("hello workspace\n")
    _, r = await execute_tool_block(_block("grep", json.dumps({"pattern": "hello"})), owner="a", workspace=ws)
    assert r["exit_code"] == 0 and "doc.txt" in r["output"]
    outside = tempfile.mkdtemp()
    _, r = await execute_tool_block(_block("grep", json.dumps({"pattern": "x", "path": outside})), owner="a", workspace=ws)
    assert r["exit_code"] == 1 and "outside the workspace" in r["error"]
    _, r = await execute_tool_block(_block("ls", ""), owner="a", workspace=ws)
    assert r["exit_code"] == 0 and "doc.txt" in r["output"]
    _, r = await execute_tool_block(_block("ls", outside), owner="a", workspace=ws)
    assert r["exit_code"] == 1 and "outside the workspace" in r["error"]


@pytest.mark.asyncio
async def test_glob_confined_e2e(ws, admin):
    """glob's literal fast-path must stay inside the workspace. A pattern with
    ../ or an absolute path outside the root would otherwise leak the existence
    and full path of arbitrary host files (an oracle), even though read_file
    blocks reading them."""
    with open(os.path.join(ws, "found.py"), "w") as f:
        f.write("x")
    _, r = await execute_tool_block(_block("glob", json.dumps({"pattern": "found.py"})), owner="a", workspace=ws)
    assert r["exit_code"] == 0 and "found.py" in r["output"]

    # a secret outside the workspace must not be discoverable via glob
    outside = os.path.realpath(tempfile.mkdtemp())
    secret = os.path.realpath(os.path.join(outside, "secret.txt"))
    with open(secret, "w") as f:
        f.write("nope")
    # An escaping pattern must come back as "No files" (the not-found message),
    # not as a match that returns the file's path. The not-found message echoes
    # the pattern the model supplied, so the signal is the absence of a match,
    # not the absence of the path string.
    rel = os.path.relpath(secret, os.path.realpath(ws))
    _, r = await execute_tool_block(_block("glob", json.dumps({"pattern": rel})), owner="a", workspace=ws)
    assert r["exit_code"] == 0 and "No files" in r["output"] and secret not in r["output"]
    _, r = await execute_tool_block(_block("glob", json.dumps({"pattern": secret})), owner="a", workspace=ws)
    assert r["exit_code"] == 0 and "No files" in r["output"]


@pytest.mark.asyncio
async def test_glob_skips_sensitive_files_in_workspace(ws, admin):
    """glob must not enumerate deny-listed sensitive files that live inside the
    workspace. read_file/write_file/edit_file refuse them and grep skips them,
    so glob surfacing their paths is an enumeration oracle for prompt-injection.
    """
    with open(os.path.join(ws, "keep.py"), "w") as f:
        f.write("x")
    with open(os.path.join(ws, ".env"), "w") as f:
        f.write("AWS_SECRET=xxx")
    with open(os.path.join(ws, "id_rsa"), "w") as f:  # non-dotfile key at root
        f.write("KEY")
    os.makedirs(os.path.join(ws, ".ssh"), exist_ok=True)
    with open(os.path.join(ws, ".ssh", "authorized_keys"), "w") as f:
        f.write("ssh-rsa AAAA")

    # A recursive wildcard returns ordinary files but none of the sensitive
    # ones. The pattern "**/*" contains no secret names, so a secret basename
    # appearing in the output is a real leak (not the echoed not-found pattern).
    _, r = await execute_tool_block(_block("glob", json.dumps({"pattern": "**/*"})), owner="a", workspace=ws)
    assert r["exit_code"] == 0
    assert "keep.py" in r["output"]
    for leak in (".env", "id_rsa", "authorized_keys"):
        assert leak not in r["output"], f"glob leaked sensitive file: {leak}"

    # Directly targeting a sensitive file (literal fast-path and wildcard) must
    # come back as the not-found message, never a match with the file's path.
    for pat in (".env", "**/id_rsa", "**/authorized_keys"):
        _, r = await execute_tool_block(_block("glob", json.dumps({"pattern": pat})), owner="a", workspace=ws)
        assert r["exit_code"] == 0 and "No files" in r["output"]


@pytest.mark.asyncio
async def test_subprocess_cwd_is_workspace_e2e(ws, admin):
    """python tool runs with cwd = workspace (OS-agnostic probe)."""
    from src.shell_policy import _working_bwrap

    if _working_bwrap() is None:
        pytest.skip("subprocess containment is unavailable on this platform")

    _, r = await execute_tool_block(_block("python", "import os; print(os.getcwd())"), owner="a", workspace=ws)
    assert r["exit_code"] == 0
    assert os.path.realpath(r["output"].strip()) == os.path.realpath(ws)


# ── get_workspace tool ──────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_get_workspace_tool(ws, admin):
    _, r = await execute_tool_block(_block("get_workspace", ""), owner="a", workspace=ws)
    assert r["exit_code"] == 0 and r["output"].startswith(os.path.realpath(ws)) and "not sandboxed" in r["output"]
    _, r = await execute_tool_block(_block("get_workspace", ""), owner="a")  # none active
    assert r["exit_code"] == 0 and "No workspace" in r["output"]


# ── no leak across calls ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_binding_does_not_leak(ws, admin):
    await execute_tool_block(_block("ls", ""), owner="a", workspace=ws)
    assert get_active_workspace() is None


# ── tool selection: an active workspace is the file-work signal ─────────
# A vague ("low-signal") message like "look at the local project" matches no
# domain keywords, so retrieval is normally skipped. When a workspace is set it
# must still surface the file tools, otherwise the agent says it has no file
# access (the bug this guards against).

def _sent_tool_names(monkeypatch, *, workspace, message="look at the local project", force_keyword_fallback=False):
    import asyncio
    import src.agent_loop as al

    monkeypatch.setattr(al, "get_setting", lambda key, default=None: default, raising=False)
    monkeypatch.setattr(al, "get_mcp_manager", lambda: None, raising=False)
    monkeypatch.setattr(al, "estimate_tokens", lambda *a, **k: 10, raising=False)
    # Isolate the selection logic from owner gating (tested separately).
    monkeypatch.setattr(al, "blocked_tools_for_owner", lambda owner: set(), raising=False)
    _patch_managed_agent_route(monkeypatch, al)
    if force_keyword_fallback:
        import src.tool_index as ti

        def _raise_get_tool_index():
            raise RuntimeError("skip vector retrieval")

        monkeypatch.setattr(ti, "get_tool_index", _raise_get_tool_index, raising=False)

    captured = []

    async def _fake_stream(_candidates, messages, **kwargs):
        captured.append(kwargs.get("tools"))
        yield "data: " + json.dumps({"delta": "ok"}) + "\n\n"
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(al, "stream_agent_target", _fake_stream, raising=False)

    async def _run():
        gen = al.stream_agent_loop(
            "https://api.openai.com/v1", "gpt-test",
            [{"role": "user", "content": message}],
            max_rounds=1, relevant_tools=None, owner="admin", workspace=workspace,
        )
        return [c async for c in gen]

    asyncio.run(_run())
    schemas = captured[0] or []
    return {t["function"]["name"] for t in schemas if isinstance(t, dict) and "function" in t}


def test_low_signal_with_workspace_surfaces_readonly_file_tools(monkeypatch):
    names = _sent_tool_names(monkeypatch, workspace="/tmp")
    # read-only nav tools surface so the agent can explore
    assert "read_file" in names
    assert "get_workspace" in names
    assert "grep" in names
    # write/shell tools do NOT surface on a vague message
    assert "write_file" not in names
    assert "edit_file" not in names
    assert "bash" not in names
    assert "python" not in names


def test_workspace_coding_request_surfaces_edit_and_verify_tools(monkeypatch):
    names = _sent_tool_names(
        monkeypatch,
        workspace="/tmp",
        message="fix the failing frontend test in this repo",
        force_keyword_fallback=True,
    )
    assert "get_workspace" in names
    assert "read_file" in names
    assert "grep" in names
    assert "edit_file" in names
    assert "write_file" in names
    assert "apply_patch" in names
    assert "todowrite" in names
    assert "bash" in names
    assert "python" in names


def test_low_signal_without_workspace_excludes_file_tools(monkeypatch):
    names = _sent_tool_names(monkeypatch, workspace=None)
    assert "read_file" not in names
    assert "get_workspace" not in names


def test_explicit_workspace_request_without_workspace_stops(monkeypatch):
    import asyncio
    import src.agent_loop as al

    monkeypatch.setattr(al, "get_setting", lambda key, default=None: default, raising=False)
    monkeypatch.setattr(al, "get_mcp_manager", lambda: None, raising=False)
    monkeypatch.setattr(al, "estimate_tokens", lambda *a, **k: 10, raising=False)
    monkeypatch.setattr(al, "blocked_tools_for_owner", lambda owner: set(), raising=False)
    _patch_managed_agent_route(monkeypatch, al)

    async def _should_not_stream(*args, **kwargs):
        raise AssertionError("LLM should not be called when explicit workspace is missing")
        yield ""

    monkeypatch.setattr(al, "stream_agent_target", _should_not_stream, raising=False)

    async def _run():
        gen = al.stream_agent_loop(
            "https://api.openai.com/v1", "gpt-test",
            [{"role": "user", "content": "In this workspace, fix a typo and verify it."}],
            max_rounds=1, relevant_tools=None, owner="admin", workspace=None,
        )
        return [c async for c in gen]

    chunks = asyncio.run(_run())
    text = "".join(chunks)
    assert "No active workspace is set" in text
    assert "/workspace set /absolute/path" in text
    assert '"missing_workspace": true' in text


def test_workspace_coding_mode_prompt_is_injected(monkeypatch):
    import src.agent_loop as al

    monkeypatch.setattr(al, "get_setting", lambda key, default=None: default, raising=False)
    monkeypatch.setattr(al, "get_mcp_manager", lambda: None, raising=False)
    monkeypatch.setattr(al, "blocked_tools_for_owner", lambda owner: set(), raising=False)
    al._cached_base_prompt = None
    al._cached_base_prompt_key = None

    messages, _ = al._build_system_prompt(
        messages=[{"role": "user", "content": "fix the bug"}],
        model="gpt-test",
        active_document=None,
        mcp_mgr=None,
        relevant_tools={"get_workspace", "read_file", "grep", "edit_file", "write_file", "apply_patch", "todowrite", "bash"},
        workspace="/tmp/example-repo",
    )
    system_text = "\n\n".join(m.get("content", "") for m in messages if m.get("role") == "system")
    assert "## Workspace coding mode" in system_text
    assert "Active workspace: `/tmp/example-repo`" in system_text
    assert "call `todowrite`" in system_text
    assert "Change repo files with `apply_patch`" in system_text


# ── browse route is admin-gated ─────────────────────────────────────────

def test_browse_is_admin_gated(monkeypatch):
    from fastapi import HTTPException
    import routes.workspace_routes as wr

    router = wr.setup_workspace_routes()
    browse = next(r.endpoint for r in router.routes if r.path == "/api/workspace/browse")

    monkeypatch.setattr(wr, "get_current_user", lambda req: "bob")
    monkeypatch.setattr(wr, "owner_is_admin_or_single_user", lambda owner: False)
    with pytest.raises(HTTPException) as ei:
        browse(request=object(), path="/")
    assert ei.value.status_code == 403

    monkeypatch.setattr(wr, "owner_is_admin_or_single_user", lambda owner: True)
    out = browse(request=object(), path=os.path.expanduser("~"))
    assert "dirs" in out and "path" in out
    assert all("name" in d and "path" in d for d in out["dirs"])


def test_app_folder_picker_includes_hidden_directories(monkeypatch, tmp_path):
    import routes.workspace_routes as wr
    import src.openclank.files_service_client as files_client

    (tmp_path / ".config").mkdir()
    (tmp_path / "project").mkdir()
    router = wr.setup_workspace_routes()
    browse = next(r.endpoint for r in router.routes if r.path == "/api/workspace/browse")
    monkeypatch.setattr(wr, "get_current_user", lambda req: "admin")
    monkeypatch.setattr(wr, "owner_is_admin_or_single_user", lambda owner: True)

    agent_names = {
        item["name"]
        for item in browse(
            request=object(), path=str(tmp_path), selection_kind="agent_workspace"
        )["dirs"]
    }
    app_names = {
        item["name"]
        for item in browse(
            request=object(), path=str(tmp_path), selection_kind="app_folder"
        )["dirs"]
    }
    assert agent_names == {"project"}
    assert app_names == {".config", "project"}

    # Keep the same selection semantics when directory metadata comes from
    # the optional Rust adapter.
    monkeypatch.setenv("ODYSSEUS_FILES_WORKSPACE_ADAPTER", "1")
    monkeypatch.setattr(files_client, "client_for_owner", lambda *args, **kwargs: object())
    monkeypatch.setattr(
        wr,
        "_run_service_browse",
        lambda client, target: {
            "data": {
                "entries": [
                    {"name": ".config", "kind": "Directory"},
                    {"name": "project", "kind": "Directory"},
                ]
            }
        },
    )
    adapted_agent = browse(
        request=object(), path=str(tmp_path), selection_kind="agent_workspace"
    )
    adapted_app = browse(
        request=object(), path=str(tmp_path), selection_kind="app_folder"
    )
    assert {item["name"] for item in adapted_agent["dirs"]} == {"project"}
    assert {item["name"] for item in adapted_app["dirs"]} == {".config", "project"}


def test_non_admin_workspace_picker_starts_at_assigned_roots(monkeypatch, tmp_path):
    from fastapi import HTTPException
    import routes.workspace_routes as wr
    import src.openclank.files_service_client as files_client
    from src.openclank.filesystem_registry import FilesystemRootRegistry

    workspace = tmp_path / "assigned"
    workspace.mkdir()
    nested = workspace / "nested"
    nested.mkdir()
    registry = FilesystemRootRegistry(tmp_path / "roots.json")
    root = registry.add("admin", str(workspace), "recursive_directory", ["read"])
    registry.assign_visibility("admin", "bob", root["id"], ["read"])
    monkeypatch.setattr(wr, "FilesystemRootRegistry", lambda: registry)
    router = wr.setup_workspace_routes()
    browse = next(r.endpoint for r in router.routes if r.path == "/api/workspace/browse")
    monkeypatch.setattr(wr, "get_current_user", lambda req: "bob")
    monkeypatch.setattr(wr, "owner_is_admin_or_single_user", lambda owner: False)

    start = browse(request=object(), path="")
    assert [item["path"] for item in start["dirs"]] == [str(workspace.resolve())]
    for selection_kind in ("agent_workspace", "app_folder"):
        assigned_root = browse(
            request=object(), path=str(workspace), selection_kind=selection_kind
        )
        assert assigned_root["path"] == str(workspace.resolve())
        assert assigned_root["parent"] is None
        assert assigned_root["selectable"] is True

        assigned_child = browse(
            request=object(), path=str(nested), selection_kind=selection_kind
        )
        assert assigned_child["path"] == str(nested.resolve())
        assert assigned_child["parent"] == str(workspace.resolve())

    # The optional service adapter must not receive an unassigned path. Its
    # different error shape could otherwise reveal host-path existence.
    adapter_calls = []
    monkeypatch.setenv("ODYSSEUS_FILES_WORKSPACE_ADAPTER", "1")
    monkeypatch.setattr(files_client, "client_for_owner", lambda *args, **kwargs: object())
    monkeypatch.setattr(
        wr,
        "_run_service_browse",
        lambda client, target: adapter_calls.append(target) or {"data": {"entries": []}},
    )
    with pytest.raises(HTTPException) as error:
        browse(request=object(), path=str(tmp_path), selection_kind="app_folder")
    assert error.value.status_code == 403
    assert adapter_calls == []

    # Rust-backed responses observe the same virtual-root boundary for both
    # workspace modes; navigating upward is allowed only within the grant.
    for selection_kind in ("agent_workspace", "app_folder"):
        assigned_root = browse(
            request=object(), path=str(workspace), selection_kind=selection_kind
        )
        assert assigned_root["parent"] is None
        assigned_child = browse(
            request=object(), path=str(nested), selection_kind=selection_kind
        )
        assert assigned_child["parent"] == str(workspace.resolve())


# ── bind-time vetting of the workspace root ─────────────────────────────

def test_vet_workspace_accepts_normal_dir(ws):
    from src.tool_execution import vet_workspace
    assert vet_workspace(ws) == os.path.realpath(ws)


def test_vet_workspace_rejects_sensitive_root(tmp_path):
    # The resolver deny-lists sensitive paths inside the workspace, but the
    # empty-path search root is the workspace itself - a sensitive root must
    # be rejected before it is bound or `ls` with no path would list it.
    from src.tool_execution import vet_workspace
    ssh_dir = tmp_path / ".ssh"
    ssh_dir.mkdir()
    assert vet_workspace(str(ssh_dir)) is None


def test_vet_workspace_rejects_nondir_and_empty(ws):
    from src.tool_execution import vet_workspace
    assert vet_workspace(os.path.join(ws, "a.txt")) is None  # file, not dir
    assert vet_workspace("/nonexistent/path/xyz") is None
    assert vet_workspace("") is None
    assert vet_workspace("   ") is None


def test_vet_workspace_rejects_filesystem_root():
    # Binding / would make every absolute path "inside" the workspace,
    # collapsing confinement into host-wide file access.
    from src.tool_execution import vet_workspace
    assert vet_workspace("/") is None


def _stable_workspace_authority(
    monkeypatch,
    tmp_path,
    *,
    target: str,
    kind: str,
):
    """Install one canonical Workspace + current owner policy for dispatch."""
    from src.openclank.file_policy import FilePolicyRepository

    authority_dir = tmp_path / "authority"
    authority_dir.mkdir()
    auth_path = authority_dir / "auth.json"
    auth_path.write_text(
        json.dumps(
            {
                "users": {
                    "alice": {"account_id": "alice-id", "is_admin": False},
                    "bob": {"account_id": "bob-id", "is_admin": False},
                }
            }
        ),
        encoding="utf-8",
    )
    repository = FilePolicyRepository(authority_dir / "app.db")
    location = repository.create_location(
        actor_subject_id="admin-id",
        path=target,
        kind=kind,
        capabilities=("read", "write"),
    )
    repository.create_binding(
        actor_subject_id="admin-id",
        binding_class="people",
        subject_id="alice-id",
        location_id=location.id,
        capabilities=("read",),
    )
    agent_binding = repository.create_binding(
        actor_subject_id="admin-id",
        binding_class="agent",
        subject_id="alice-id",
        location_id=location.id,
        capabilities=("read",),
    )
    workspace = repository.create_workspace(
        actor_subject_id="alice-id",
        owner_subject_id="alice-id",
        location_id=location.id,
        name="Trusted workspace",
    )
    monkeypatch.setenv(
        "OPEN_CLANK_AUTHORITY_DB_PATH", str(authority_dir / "app.db")
    )
    monkeypatch.setenv("OPEN_CLANK_AUTHORITY_AUTH_PATH", str(auth_path))
    return repository, workspace, agent_binding


@pytest.mark.asyncio
async def test_stable_workspace_id_allows_explicit_whole_root(
    monkeypatch, tmp_path, admin
):
    filesystem_root = os.path.abspath(os.sep)
    _repository, workspace, _agent = _stable_workspace_authority(
        monkeypatch,
        tmp_path,
        target=filesystem_root,
        kind="whole_root",
    )

    _, result = await execute_tool_block(
        _block("get_workspace"),
        owner="alice",
        workspace=filesystem_root,
        authority_workspace_id=workspace.id,
    )

    assert result["exit_code"] == 0
    assert result["output"].splitlines()[0] == os.path.realpath(filesystem_root)
    assert get_active_workspace() is None


@pytest.mark.asyncio
async def test_stable_workspace_id_rejects_cwd_mismatch(
    monkeypatch, tmp_path, admin
):
    project = tmp_path / "project"
    project.mkdir()
    _repository, workspace, _agent = _stable_workspace_authority(
        monkeypatch,
        tmp_path,
        target=str(project),
        kind="directory",
    )

    _, result = await execute_tool_block(
        _block("get_workspace"),
        owner="alice",
        workspace=str(tmp_path),
        authority_workspace_id=workspace.id,
    )

    assert result["exit_code"] == 1
    assert result["blocked"] is True
    assert "workspace was rejected" in result["error"]


@pytest.mark.asyncio
async def test_stable_workspace_id_rechecks_owner_and_revocation(
    monkeypatch, tmp_path, admin
):
    project = tmp_path / "project"
    project.mkdir()
    repository, workspace, agent_binding = _stable_workspace_authority(
        monkeypatch,
        tmp_path,
        target=str(project),
        kind="directory",
    )

    _, cross_owner = await execute_tool_block(
        _block("get_workspace"),
        owner="bob",
        workspace=str(project),
        authority_workspace_id=workspace.id,
    )
    assert cross_owner["exit_code"] == 1
    assert cross_owner["blocked"] is True

    repository.revoke_binding(agent_binding.id, actor_subject_id="admin-id")
    _, revoked = await execute_tool_block(
        _block("get_workspace"),
        owner="alice",
        workspace=str(project),
        authority_workspace_id=workspace.id,
    )
    assert revoked["exit_code"] == 1
    assert revoked["blocked"] is True


@pytest.mark.asyncio
async def test_stable_workspace_id_preserves_non_root_and_raw_folder_behavior(
    monkeypatch, tmp_path, admin
):
    project = tmp_path / "project"
    project.mkdir()
    _repository, workspace, _agent = _stable_workspace_authority(
        monkeypatch,
        tmp_path,
        target=str(project),
        kind="directory",
    )

    _, stable = await execute_tool_block(
        _block("get_workspace"),
        owner="alice",
        workspace=str(project),
        authority_workspace_id=workspace.id,
    )
    _, legacy = await execute_tool_block(
        _block("get_workspace"),
        owner="alice",
        workspace=str(project),
    )

    assert stable["exit_code"] == 0
    assert legacy["exit_code"] == 0
    assert stable["output"].splitlines()[0] == os.path.realpath(str(project))
    assert legacy["output"].splitlines()[0] == os.path.realpath(str(project))


def test_lifetools_descriptor_projects_canonical_workspace_authority_stores():
    from src.constants import APP_DB, AUTH_FILE
    from src.openclank.acp_bridge import lifetools_mcp_descriptor

    descriptor = lifetools_mcp_descriptor(
        owner="alice",
        session_id="chat-1",
        workspace="/workspace",
        authority_workspace_id="workspace-a",
    )
    env = {item["name"]: item["value"] for item in descriptor["env"]}

    assert env["OPEN_CLANK_AUTHORITY_DB_PATH"] == os.path.abspath(APP_DB)
    assert env["OPEN_CLANK_AUTHORITY_AUTH_PATH"] == os.path.abspath(AUTH_FILE)


def test_chat_stable_root_bypasses_only_the_legacy_raw_path_validator(monkeypatch):
    import routes.chat_routes as cr

    raw_calls = []
    monkeypatch.setattr(
        cr,
        "_canonical_workspace_path",
        lambda request, workspace_id, owner=None: os.path.abspath(os.sep),
    )
    monkeypatch.setattr(
        cr,
        "_resolve_request_workspace",
        lambda request, raw, owner=None: raw_calls.append(raw) or ("legacy", ""),
    )

    stable, rejected = cr._resolve_chat_workspace_binding(
        object(),
        selected_workspace_id="workspace-root",
        stored_workspace_id="workspace-root",
        workspace_id_supplied=False,
        requested_legacy_workspace="/untrusted",
        owner="alice",
    )
    assert stable == os.path.abspath(os.sep)
    assert rejected == ""
    assert raw_calls == []

    legacy, rejected = cr._resolve_chat_workspace_binding(
        object(),
        selected_workspace_id="",
        stored_workspace_id="",
        workspace_id_supplied=False,
        requested_legacy_workspace="/untrusted",
        owner="alice",
    )
    assert (legacy, rejected) == ("legacy", "")
    assert raw_calls == ["/untrusted"]


@pytest.mark.asyncio
async def test_dispatcher_rejects_filesystem_root_workspace(admin):
    _, result = await execute_tool_block(
        _block("read_file", "/etc/hosts"),
        owner="a",
        workspace="/",
    )

    assert result["exit_code"] == 1
    assert result["blocked"] is True
    assert "workspace was rejected" in result["error"]
    assert get_active_workspace() is None


@pytest.mark.asyncio
async def test_dispatcher_rejects_control_data_workspace(monkeypatch, tmp_path, admin):
    control = tmp_path / "data"
    control.mkdir()
    monkeypatch.setattr("src.constants.DATA_DIR", str(control))
    monkeypatch.setattr("src.constants.AUTH_FILE", str(control / "auth.json"))
    monkeypatch.setattr("src.constants.FM_DB_PATH", str(control / "frankenmemory.db"))

    _, result = await execute_tool_block(
        _block("get_workspace"),
        owner="a",
        workspace=str(control),
    )

    assert result["exit_code"] == 1
    assert result["blocked"] is True
    assert "workspace was rejected" in result["error"]
    assert get_active_workspace() is None


def test_browse_marks_root_unselectable_and_vet_endpoint(monkeypatch):
    import routes.workspace_routes as wr

    router = wr.setup_workspace_routes()
    browse = next(r.endpoint for r in router.routes if r.path == "/api/workspace/browse")
    vet = next(r.endpoint for r in router.routes if r.path == "/api/workspace/vet")

    monkeypatch.setattr(wr, "get_current_user", lambda req: "admin")
    monkeypatch.setattr(wr, "owner_is_admin_or_single_user", lambda owner: True)

    out = browse(request=object(), path="/", selection_kind="agent_workspace")
    assert out["selectable"] is False
    app_root = browse(request=object(), path="/", selection_kind="app_folder")
    assert app_root["path"] == os.path.realpath("/")
    assert app_root["selectable"] is True
    out = browse(request=object(), path=os.path.expanduser("~"))
    assert out["selectable"] is True

    from fastapi import HTTPException
    with pytest.raises(HTTPException) as invalid:
        browse(request=object(), path="/", selection_kind="unknown")
    assert invalid.value.status_code == 400

    assert vet(request=object(), path="/") == {"ok": False, "path": None}
    home = os.path.realpath(os.path.expanduser("~"))
    assert vet(request=object(), path="~") == {"ok": True, "path": home}

    monkeypatch.setattr(wr, "owner_is_admin_or_single_user", lambda owner: False)
    with pytest.raises(HTTPException) as ei:
        vet(request=object(), path="/tmp")
    assert ei.value.status_code == 403


# ── send-time privilege gate (no path oracle for non-admins) ────────────

def test_request_workspace_gate(ws, monkeypatch):
    """Non-admin chat callers must get a uniform drop with no vetting: the
    workspace_rejected signal would otherwise reveal which host paths exist."""
    import routes.chat_routes as cr

    monkeypatch.setattr(cr, "get_current_user", lambda req: "bob")
    vet_calls = []
    import src.tool_execution as te
    real_vet = te.vet_workspace
    monkeypatch.setattr(te, "vet_workspace", lambda p: vet_calls.append(p) or real_vet(p))

    import src.tool_security as ts
    monkeypatch.setattr(ts, "owner_is_admin_or_single_user", lambda owner: False)
    # Valid and invalid paths are indistinguishable for a non-admin: both
    # drop silently, and the path never reaches the filesystem.
    assert cr._resolve_request_workspace(object(), ws) == ("", "")
    assert cr._resolve_request_workspace(object(), "/nonexistent/xyz") == ("", "")
    assert vet_calls == []

    monkeypatch.setattr(ts, "owner_is_admin_or_single_user", lambda owner: True)
    assert cr._resolve_request_workspace(object(), ws) == (os.path.realpath(ws), "")
    assert cr._resolve_request_workspace(object(), "/nonexistent/xyz") == ("", "/nonexistent/xyz")


def test_chat_resolves_stable_workspace_id_server_side(monkeypatch, tmp_path):
    import routes.chat_routes as cr
    from src.openclank.file_policy import FilePolicyRepository

    repository = FilePolicyRepository(tmp_path / "workspace-policy.db")
    root = tmp_path / "assigned"
    root.mkdir()
    child = root / "project"
    child.mkdir()
    location = repository.create_location(
        actor_subject_id="admin-id",
        path=str(root),
        kind="directory",
        capabilities=("read", "write"),
    )
    repository.create_binding(
        actor_subject_id="admin-id",
        binding_class="people",
        subject_id="alice-id",
        location_id=location.id,
        capabilities=("read",),
    )
    agent = repository.create_binding(
        actor_subject_id="admin-id",
        binding_class="agent",
        subject_id="alice-id",
        location_id=location.id,
        capabilities=("read",),
        lifetime="always",
    )
    workspace = repository.create_workspace(
        actor_subject_id="alice-id",
        owner_subject_id="alice-id",
        location_id=location.id,
        name="Project",
        relative_folder="project",
    )

    class Auth:
        def account_id(self, username):
            return {"alice": "alice-id", "bob": "bob-id"}.get(username)

        def is_admin(self, username):
            return False

    request = type("Request", (), {
        "app": type("App", (), {"state": type("State", (), {"auth_manager": Auth()})()})(),
    })()
    owner = {"value": "alice"}
    monkeypatch.setattr(cr, "FilePolicyRepository", lambda: repository)
    monkeypatch.setattr(cr, "get_current_user", lambda _request: owner["value"])

    assert cr._canonical_workspace_path(request, workspace.id) == str(child)
    assert cr._canonical_workspace_path(request, "workspace-forged") == ""

    owner["value"] = "bob"
    assert cr._canonical_workspace_path(request, workspace.id) == ""

    owner["value"] = "alice"
    repository.revoke_binding(agent.id, actor_subject_id="admin-id")
    assert cr._canonical_workspace_path(request, workspace.id) == ""


def test_bound_chat_workspace_cannot_be_overridden_by_turn_input():
    import routes.chat_routes as cr
    from fastapi import HTTPException

    assert cr._selected_chat_workspace_id(
        "workspace-a",
        supplied=False,
        requested_workspace_id="",
    ) == "workspace-a"
    assert cr._selected_chat_workspace_id(
        None,
        supplied=True,
        requested_workspace_id="workspace-b",
    ) == "workspace-b"
    assert cr._selected_chat_workspace_id(
        None,
        supplied=True,
        requested_workspace_id="",
    ) == ""
    with pytest.raises(HTTPException) as mismatch:
        cr._selected_chat_workspace_id(
            "workspace-a",
            supplied=True,
            requested_workspace_id="workspace-b",
        )
    assert mismatch.value.status_code == 409


def test_session_workspace_migration_is_idempotent_and_preserves_rows(monkeypatch, tmp_path):
    import sqlite3
    import core.database as database

    path = tmp_path / "legacy-sessions.db"
    connection = sqlite3.connect(path)
    try:
        connection.execute(
            "CREATE TABLE sessions (id TEXT PRIMARY KEY, name TEXT NOT NULL)"
        )
        connection.execute(
            "INSERT INTO sessions(id, name) VALUES ('chat-a', 'A')"
        )
        connection.commit()
    finally:
        connection.close()

    monkeypatch.setattr(database, "DATABASE_URL", f"sqlite:///{path}")
    database._migrate_add_session_workspace_id_column()
    database._migrate_add_session_workspace_id_column()

    connection = sqlite3.connect(path)
    try:
        columns = {
            row[1]
            for row in connection.execute("PRAGMA table_info(sessions)").fetchall()
        }
        indexes = {
            row[1]
            for row in connection.execute("PRAGMA index_list(sessions)").fetchall()
        }
        row = connection.execute(
            "SELECT id, name, workspace_id FROM sessions"
        ).fetchone()
    finally:
        connection.close()
    assert "workspace_id" in columns
    assert "ix_sessions_workspace_id" in indexes
    assert row == ("chat-a", "A", None)
