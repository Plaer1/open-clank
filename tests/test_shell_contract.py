import asyncio
import gc
import io
import json
import os
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest

from src import bg_jobs, shell_policy, shell_worker
from src.agent_tools.subprocess_tools import BashTool, PythonTool, _BoundedCapture
from src.shell_policy import (
    ShellContainmentError,
    StreamingRedactor,
    contained_argv,
    contained_parser_argv,
    minimal_shell_env,
    redact_text,
)
from src.project_hex import activate_hex, register_project, resolve_hex
from src import tool_execution


@pytest.fixture
def foreground_shell_store(tmp_path, monkeypatch):
    jobs_dir = tmp_path / "shell-jobs"
    jobs_dir.mkdir()
    monkeypatch.setattr(bg_jobs, "_STORE", tmp_path / "shell-jobs.json")
    monkeypatch.setattr(bg_jobs, "_JOBS_DIR", jobs_dir)
    monkeypatch.setenv("OPEN_CLANK_DATA_DIR", str(tmp_path / "data"))
    return jobs_dir


def test_minimal_environment_and_redaction():
    env = minimal_shell_env(
        {
            "PATH": "/bin",
            "LANG": "C",
            "OPENAI_API_KEY": "sk-secret-secret-secret-secret",
            "CUSTOM": "ambient",
        },
        cwd="/tmp/work",
    )
    assert env["PATH"] == "/bin"
    assert env["HOME"] == str(Path("/tmp/work").resolve(strict=False))
    assert "OPENAI_API_KEY" not in env
    assert "CUSTOM" not in env
    assert "sk-secret-secret-secret-secret" not in redact_text(
        "token=sk-secret-secret-secret-secret",
        source_env={"OPENAI_API_KEY": "sk-secret-secret-secret-secret"},
    )


def test_direct_fallback_persists_authenticated_run_and_explicit_absent_task(tmp_path, monkeypatch):
    """The fallback path must not invent a task identity for Lore."""
    from src import agent_runs
    from src.agent_tools import TOOL_HANDLERS
    from src.openclank import history_capture
    from src.openclank.history_capture import HistoryContext

    captured = {}

    class Client:
        def prepare(self, envelope, *, content, fingerprint):
            captured["envelope"] = envelope

    def trusted_context(**values):
        return HistoryContext(
            actor_id=values["actor_id"], account_id=values["account_id"],
            workspace_id=values["workspace_id"], session_id=values["session_id"],
            run_id=values["run_id"], task_id=values["task_id"], tool_id=values["tool_id"],
            roots=(str(tmp_path),), client=Client(),
        )

    async def fake_bash(_content, ctx):
        target = tmp_path / "fallback.txt"
        target.write_text("before", encoding="utf-8")
        handle = history_capture.begin_file_capture(
            str(target), operation="shell", context=ctx["history_context"], action_id="action-fallback"
        )
        assert handle.available
        return {"exit_code": 0}

    monkeypatch.setattr(tool_execution, "_copal_account_id", lambda _owner: ("alice", "acct-a", None))
    monkeypatch.setattr(tool_execution, "_owner_is_admin", lambda _owner: True)
    monkeypatch.setattr(agent_runs, "get_run_id", lambda _session: "run-authenticated")
    monkeypatch.setattr(history_capture, "trusted_tool_context", trusted_context)
    monkeypatch.setitem(TOOL_HANDLERS, "bash", fake_bash)

    result = asyncio.run(tool_execution._direct_fallback(
        "bash", "printf changed > fallback.txt", session_id="chat-a", owner="alice"
    ))

    assert result == {"exit_code": 0}
    envelope = captured["envelope"]
    assert envelope["actor_id"] == "alice"
    assert envelope["session_id"] == "chat-a"
    assert envelope["run_id"] == "run-authenticated"
    assert envelope["task_id"] is None
    assert envelope["tool_id"] == "bash"


def test_direct_background_route_persists_run_and_explicit_absent_task(tmp_path, monkeypatch):
    """The marker route carries its identity into the serialized Lore context."""
    from types import SimpleNamespace
    from src import agent_runs
    from src.openclank import history_capture
    from src.openclank.history_capture import HistoryContext

    observed = {}

    async def approved(*_args, **_kwargs):
        return None

    def trusted_context(**values):
        return HistoryContext(
            actor_id=values["actor_id"], account_id=values["account_id"],
            workspace_id=values["workspace_id"], session_id=values["session_id"],
            run_id=values["run_id"], task_id=values["task_id"], tool_id=values["tool_id"],
            roots=(str(tmp_path),),
        )

    def launch(_command, **kwargs):
        observed.update(kwargs)
        return {"id": "job-authenticated", "started_at": time.time()}

    monkeypatch.setattr(tool_execution, "_copal_account_id", lambda _owner: ("alice", "acct-a", None))
    monkeypatch.setattr(tool_execution, "_owner_is_admin", lambda _owner: True)
    monkeypatch.setattr(agent_runs, "get_run_id", lambda _session: "run-authenticated")
    monkeypatch.setattr(history_capture, "trusted_tool_context", trusted_context)
    monkeypatch.setattr(bg_jobs, "launch", launch)
    monkeypatch.setattr(shell_policy, "contained_argv", lambda *args, **kwargs: (args[0], "off"))
    monkeypatch.setattr(shell_policy, "require_shell_approval", approved)

    block = SimpleNamespace(tool_type="bash", content="#!bg\nprintf changed > note.txt")
    description, result = asyncio.run(tool_execution._execute_tool_block_impl(
        block, session_id="chat-a", owner="alice"
    ))

    assert description.startswith("bash (background):")
    assert result["bg_job_id"] == "job-authenticated"
    assert observed["run_id"] == "run-authenticated"
    assert observed["task_id"] is None
    context = history_capture.context_from_mapping(observed["history_context"])
    assert context is not None
    target = tmp_path / "background.txt"
    target.write_text("before", encoding="utf-8")
    envelope = history_capture._envelope(
        action_id="action-background", operation="shell", path=str(target),
        before=b"before", context=context,
    )
    assert envelope["session_id"] == "chat-a"
    assert envelope["run_id"] == "run-authenticated"
    assert envelope["task_id"] is None
    assert envelope["tool_id"] == "bash"


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ("r''m -rf target", "remove"),
        (r"\r\m -rf target", "remove"),
        ("git re''set --hard", "git-reset-hard"),
        ("git clean -fdx", "git-clean"),
        ("git push --force-with-lease", "git-force"),
        ("git checkout -- tracked.txt", "git-checkout"),
        ("git -C repo restore tracked.txt", "git-restore"),
        ("git rebase main", "git-rebase"),
        ("git cherry-pick deadbeef", "git-cherry-pick"),
        ("git revert deadbeef", "git-revert"),
        ("git switch --force main", "git-switch-force"),
        ("git commit --amend --no-edit", "git-commit-amend"),
        ("command r''m -rf target", "indirect-command"),
        ("env -i r''m -rf target", "indirect-command"),
        ("$(printf rm) -rf target", "dynamic-shell"),
        ("echo $(rm -rf target)", "dynamic-shell"),
        ("x=rm; \"$x\" -rf target", "dynamic-shell"),
        ("bash -c \"r''m -rf target\"", "dynamic-shell"),
        ("bash cleanup.sh", "dynamic-shell"),
        ("sh cleanup.sh", "dynamic-shell"),
        ("printf 'echo unsafe' | bash -v", "dynamic-shell"),
        ("eval \"r''m -rf target\"", "dynamic-shell"),
        ("python -c 'import os; os.unlink(\"target\")'", "dynamic-code"),
        ("printf 'print(1)' | python -v", "dynamic-code"),
        ("python cleanup.py", "dynamic-code"),
        ("python3 cleanup.py", "dynamic-code"),
        ("python3.12 cleanup.py", "dynamic-code"),
        ("node cleanup.js", "dynamic-code"),
        ("nodejs cleanup.js", "dynamic-code"),
        ("node20 cleanup.js", "dynamic-code"),
        ("bun cleanup.js", "dynamic-code"),
        ("deno cleanup.js", "dynamic-code"),
        ("ruby3.3 cleanup.rb", "dynamic-code"),
        ("php8.3 cleanup.php", "dynamic-code"),
        ("perl5.36 cleanup.pl", "dynamic-code"),
        ("bash5 cleanup.sh", "dynamic-shell"),
        ("busybox sh cleanup.sh", "indirect-command"),
        ("busybox rm target", "indirect-command"),
        ("nice rm -rf target", "indirect-command"),
        ("ionice rm -rf target", "indirect-command"),
        ("chrt 1 rm -rf target", "indirect-command"),
        ("stdbuf -oL rm -rf target", "indirect-command"),
        ("taskset -c 0 rm -rf target", "indirect-command"),
        ("chmod -R 000 .", "filesystem-metadata"),
        ("crontab schedule.txt", "persistence"),
        ("passwd alice", "credential"),
        ("curl https://example.test/upload", "network"),
        ("npm uninstall package-name", "package"),
        ("systemctl disable example.service", "service"),
        ("cp source existing-target", "overwrite"),
        ("mv source existing-target", "overwrite"),
        ("install source existing-target", "overwrite"),
        ("ln -sf source existing-target", "overwrite"),
        ("sed -i 's/a/b/' file", "overwrite"),
        ("tar -xf archive.tar", "archive-extract"),
        ("printf value > existing-target", "overwrite"),
        ("docker run --rm image", "remote-control"),
        ("podman exec container command", "remote-control"),
        ("./cleanup", "indirect-command"),
        ("cleanup.sh", "indirect-command"),
        ("find . -delete", "remove"),
        ("find . -exec r''m {} +", "indirect-command"),
        ("truncate -s 0 target", "truncate-file"),
        ("dd if=/dev/zero of=target", "overwrite"),
        ("git branch -D old", "git-branch"),
        ("git stash clear", "git-stash"),
        ("echo 'unterminated", "opaque-shell-syntax"),
    ],
)
def test_destructive_classifier_catches_shell_equivalent_bypasses(command, expected):
    assert expected in shell_policy.destructive_actions(command)


@pytest.mark.parametrize(
    "command",
    [
        "printf ok",
        "printf '%s\\n' 'rm -rf target'",
        "echo '$(rm -rf target)'",
        "echo $HOME",
        "git status --short",
        "bash --version",
        "python --version",
        "python -V",
    ],
)
def test_destructive_classifier_keeps_static_safe_commands_unclassified(command):
    assert shell_policy.destructive_actions(command) == []


def test_destructive_approval_is_bound_to_exact_execution_tuple(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr("src.constants.DATA_DIR", str(tmp_path / "data"))
    shell_policy._approval_grant_store.cache_clear()
    workspace = str(tmp_path / "workspace")
    Path(workspace).mkdir()
    execution_cwd = str(Path(workspace) / "nested")
    Path(execution_cwd).mkdir()
    events = []

    async def scenario():
        async def progress(payload):
            events.append(payload)

        ctx = {
            "progress_cb": progress,
            "session_id": "chat-a",
            "owner": "alice",
            "workspace": workspace,
        }
        pending = asyncio.create_task(
            shell_policy.require_shell_approval(
                "rm stale.txt",
                ctx=ctx,
                cwd=execution_cwd,
                containment="bwrap",
                network="enabled",
            )
        )
        await asyncio.sleep(0)
        request = events[0]["data"]
        assert request["detail"]["environment_class"] == "minimal-v1"
        assert request["detail"]["workdir"] == execution_cwd
        target = request["detail"]["target_facts"][0]
        assert target["role"] == "remove_target"
        assert target["path"] == str(Path(execution_cwd) / "stale.txt")
        assert target["exists"] is False
        assert target["within_workspace"] is True
        assert not shell_policy.resolve_shell_approval(
            request["request_id"],
            "always",
            owner="bob",
            session_id="chat-a",
        )
        assert shell_policy.resolve_shell_approval(
            request["request_id"],
            "always",
            owner="alice",
            session_id="chat-a",
        )
        binding = await pending

        # The identical tuple reuses the durable grant without surfacing UI.
        assert await shell_policy.require_shell_approval(
            "rm stale.txt",
            ctx={
                "session_id": "chat-a",
                "owner": "alice",
                "workspace": workspace,
            },
            cwd=execution_cwd,
            containment="bwrap",
            network="enabled",
        ) == binding

        # Any command/context change misses the bound grant and fails closed
        # when there is no interactive surface.
        with pytest.raises(
            shell_policy.ShellApprovalError,
            match="interactive approval",
        ):
            await shell_policy.require_shell_approval(
                "rm other.txt",
                ctx={
                    "session_id": "chat-a",
                    "owner": "alice",
                    "workspace": workspace,
                },
                cwd=execution_cwd,
                containment="bwrap",
                network="enabled",
            )

    try:
        asyncio.run(scenario())
    finally:
        shell_policy._approval_grant_store.cache_clear()


def test_shell_workspace_approval_uses_stable_identity(tmp_path, monkeypatch):
    monkeypatch.setattr("src.constants.DATA_DIR", str(tmp_path / "data"))
    shell_policy._approval_grant_store.cache_clear()
    workspace = str(tmp_path / "workspace")
    Path(workspace).mkdir()
    events = []

    async def scenario():
        async def progress(payload):
            events.append(payload)
            assert shell_policy.resolve_shell_approval(
                payload["data"]["request_id"],
                "workspace",
                owner="alice",
                session_id="chat-a",
            )

        base = {
            "progress_cb": progress,
            "session_id": "chat-a",
            "owner": "alice",
            "workspace": workspace,
            "authority_workspace_id": "workspace-a",
        }
        binding = await shell_policy.require_shell_approval(
            "rm stale.txt", ctx=base, cwd=workspace, containment="bwrap"
        )
        assert events
        assert await shell_policy.require_shell_approval(
            "rm stale.txt",
            ctx={key: value for key, value in base.items() if key != "progress_cb"},
            cwd=workspace,
            containment="bwrap",
        ) == binding
        with pytest.raises(shell_policy.ShellApprovalError, match="interactive approval"):
            await shell_policy.require_shell_approval(
                "rm stale.txt",
                ctx={
                    **{key: value for key, value in base.items() if key != "progress_cb"},
                    "authority_workspace_id": "workspace-b",
                },
                cwd=workspace,
                containment="bwrap",
            )

    try:
        asyncio.run(scenario())
    finally:
        shell_policy._approval_grant_store.cache_clear()


def test_shell_workspace_reset_rejects_pending_approval(tmp_path, monkeypatch):
    monkeypatch.setattr("src.constants.DATA_DIR", str(tmp_path / "data"))
    shell_policy._approval_grant_store.cache_clear()
    workspace = str(tmp_path / "workspace")
    Path(workspace).mkdir()

    async def scenario():
        surfaced = asyncio.Event()

        async def wait_for_reset(_payload):
            surfaced.set()

        task = asyncio.create_task(shell_policy.require_shell_approval(
            "rm stale.txt",
            ctx={
                "progress_cb": wait_for_reset,
                "session_id": "chat-a",
                "owner": "alice",
                "workspace": workspace,
                "authority_workspace_id": "workspace-a",
            },
            cwd=workspace,
            containment="bwrap",
        ))
        await surfaced.wait()
        assert shell_policy.reject_shell_approval_scope(
            owner="alice", authority_workspace_id="workspace-a"
        ) == 1
        with pytest.raises(shell_policy.ShellApprovalError, match="rejected"):
            await task

    try:
        asyncio.run(scenario())
    finally:
        shell_policy._approval_grant_store.cache_clear()


def test_shell_approval_binding_changes_with_every_execution_dimension(tmp_path):
    base = shell_policy.shell_approval_binding(
        "printf ok",
        cwd=str(tmp_path),
        containment="bwrap",
        environment_class="minimal-v1",
    )
    variants = {
        shell_policy.shell_approval_binding(
            "printf changed",
            cwd=str(tmp_path),
            containment="bwrap",
            environment_class="minimal-v1",
        ),
        shell_policy.shell_approval_binding(
            "printf ok",
            cwd=str(tmp_path / "other"),
            containment="bwrap",
            environment_class="minimal-v1",
        ),
        shell_policy.shell_approval_binding(
            "printf ok",
            cwd=str(tmp_path),
            containment="off",
            environment_class="minimal-v1",
        ),
        shell_policy.shell_approval_binding(
            "printf ok",
            cwd=str(tmp_path),
            containment="bwrap",
            environment_class="minimal-v2",
        ),
    }
    assert base not in variants
    assert len(variants) == 4


def test_shell_approval_resolves_symlink_ownership_and_no_clobber_facts(tmp_path):
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    source = workspace / "source.txt"
    source.write_text("source", encoding="utf-8")
    outside_target = outside / "target.txt"
    outside_target.write_text("target", encoding="utf-8")
    link = workspace / "link.txt"
    link.symlink_to(outside_target)

    remove = shell_policy.destructive_target_facts(
        "rm link.txt",
        cwd=str(workspace),
        workspace=str(workspace),
    )[0]
    assert remove["path"] == str(link)
    assert remove["real_path"] == str(outside_target)
    assert remove["is_symlink"] is True
    assert remove["traverses_symlink"] is True
    assert remove["within_workspace"] is False
    assert isinstance(remove["owner_uid"], int)
    assert isinstance(remove["owner_gid"], int)

    destination = workspace / "new.txt"
    copy = shell_policy.destructive_target_facts(
        "cp source.txt new.txt",
        cwd=str(workspace),
        workspace=str(workspace),
    )
    destination_fact = next(
        fact for fact in copy if fact["role"] == "overwrite_destination"
    )
    assert destination_fact["path"] == str(destination)
    assert destination_fact["exists"] is False
    assert destination_fact["no_clobber"] is True
    assert destination_fact["would_clobber"] is False

    before = shell_policy.shell_approval_binding(
        "cp source.txt new.txt",
        cwd=str(workspace),
        containment="bwrap",
        workspace=str(workspace),
    )
    destination.write_text("existing", encoding="utf-8")
    after = shell_policy.shell_approval_binding(
        "cp source.txt new.txt",
        cwd=str(workspace),
        containment="bwrap",
        workspace=str(workspace),
    )
    assert before != after


def test_cancelled_shell_approval_drops_pending_request(tmp_path, monkeypatch):
    monkeypatch.setattr("src.constants.DATA_DIR", str(tmp_path / "data"))
    shell_policy._approval_grant_store.cache_clear()
    workspace = str(tmp_path)
    events = []

    async def scenario():
        async def progress(payload):
            events.append(payload)

        task = asyncio.create_task(
            shell_policy.require_shell_approval(
                "rm stale.txt",
                ctx={
                    "progress_cb": progress,
                    "session_id": "chat-cancel",
                    "owner": "alice",
                    "workspace": workspace,
                },
                cwd=workspace,
                containment="bwrap",
            )
        )
        await asyncio.sleep(0)
        request_id = events[0]["data"]["request_id"]
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not shell_policy.resolve_shell_approval(
            request_id,
            "once",
            owner="alice",
            session_id="chat-cancel",
        )

    try:
        asyncio.run(scenario())
    finally:
        shell_policy._approval_grant_store.cache_clear()


def test_destructive_background_launch_rejects_unbound_approval(
    tmp_path,
    monkeypatch,
):
    jobs = tmp_path / "jobs"
    workspace = tmp_path / "workspace"
    jobs.mkdir()
    workspace.mkdir()
    monkeypatch.setenv("OPEN_CLANK_SHELL_SANDBOX", "off")
    monkeypatch.setattr(bg_jobs, "_STORE", tmp_path / "jobs.json")
    monkeypatch.setattr(bg_jobs, "_JOBS_DIR", jobs)
    with pytest.raises(
        shell_policy.ShellApprovalError,
        match="exact execution tuple",
    ):
        bg_jobs.launch(
            "rm stale.txt",
            session_id="chat-a",
            owner="alice",
            workspace=str(workspace),
            cwd=str(workspace),
        )


@pytest.mark.skipif(
    shell_policy._working_bwrap() is None
    or shell_policy._working_network_bwrap() is None,
    reason="active Open Clank Hexes shell publication requires bubblewrap containment",
)
def test_active_hex_shell_write_is_validated_in_overlay_before_publication(
    tmp_path,
    monkeypatch,
):
    jobs = tmp_path / "jobs"
    workspace = tmp_path / "workspace"
    jobs.mkdir()
    workspace.mkdir()
    (workspace / ".git").mkdir()
    (workspace / ".hex").write_text("settings: {}\nhenxels: []\n", encoding="utf-8")
    db = str(tmp_path / "fm.db")
    monkeypatch.setattr(bg_jobs, "_STORE", tmp_path / "jobs.json")
    monkeypatch.setattr(bg_jobs, "_JOBS_DIR", jobs)
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
    record = bg_jobs.launch(
        "touch note.txt",
        session_id="chat-a",
        owner="alice",
        workspace=str(workspace),
        cwd=str(workspace),
    )
    deadline = time.time() + 10
    current = record
    while time.time() < deadline and current["status"] == "running":
        time.sleep(0.05)
        current = bg_jobs.refresh()[record["id"]]
    assert current["status"] == "done"
    assert current["containment"] == "bwrap-overlay"
    assert (workspace / "note.txt").read_bytes() == b""


@pytest.mark.skipif(
    shell_policy._working_bwrap() is None
    or shell_policy._working_network_bwrap() is None,
    reason="active Open Clank Hexes shell publication requires bubblewrap containment",
)
def test_active_hex_rejects_shell_candidate_without_changing_source(
    tmp_path,
    monkeypatch,
):
    jobs = tmp_path / "jobs"
    workspace = tmp_path / "workspace"
    jobs.mkdir()
    workspace.mkdir()
    (workspace / ".git").mkdir()
    note = workspace / "note.md"
    note.write_text("one\n", encoding="utf-8")
    (workspace / ".hex").write_text(
        "henxels:\n"
        "  - henxel: notes stay short\n"
        "    in: ./*.md\n"
        "    max_lines: 2\n",
        encoding="utf-8",
    )
    db = str(tmp_path / "fm.db")
    monkeypatch.setattr(bg_jobs, "_STORE", tmp_path / "jobs.json")
    monkeypatch.setattr(bg_jobs, "_JOBS_DIR", jobs)
    monkeypatch.setattr("src.constants.FM_DB_PATH", db)
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
    command = "printf 'one\\ntwo\\nthree\\n' > note.md"
    approval = shell_policy.shell_approval_binding(
        command,
        cwd=str(workspace),
        containment="bwrap-overlay",
        workspace=str(workspace),
    )
    record = bg_jobs.launch(
        command,
        session_id="chat-a",
        owner="alice",
        workspace=str(workspace),
        cwd=str(workspace),
        approval_binding=approval,
    )
    deadline = time.time() + 10
    current = record
    while time.time() < deadline and current["status"] == "running":
        time.sleep(0.05)
        current = bg_jobs.refresh()[record["id"]]
    assert current["status"] == "failed"
    assert note.read_text(encoding="utf-8") == "one\n"
    assert "rejected shell changes" in Path(current["log_path"]).read_text(
        encoding="utf-8"
    )


@pytest.mark.skipif(
    shell_policy._working_bwrap() is None
    or shell_policy._working_network_bwrap() is None,
    reason="active Open Clank Hexes shell publication requires bubblewrap containment",
)
def test_active_hex_shell_file_delete_publishes_only_after_validation(
    tmp_path,
    monkeypatch,
):
    jobs = tmp_path / "jobs"
    workspace = tmp_path / "workspace"
    jobs.mkdir()
    workspace.mkdir()
    (workspace / ".git").mkdir()
    note = workspace / "note.txt"
    note.write_text("delete me", encoding="utf-8")
    (workspace / ".hex").write_text("settings: {}\nhenxels: []\n", encoding="utf-8")
    db = str(tmp_path / "fm.db")
    monkeypatch.setattr(bg_jobs, "_STORE", tmp_path / "jobs.json")
    monkeypatch.setattr(bg_jobs, "_JOBS_DIR", jobs)
    monkeypatch.setattr("src.constants.FM_DB_PATH", db)
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
    command = "rm note.txt"
    approval = shell_policy.shell_approval_binding(
        command,
        cwd=str(workspace),
        containment="bwrap-overlay",
        workspace=str(workspace),
    )
    record = bg_jobs.launch(
        command,
        session_id="chat-a",
        owner="alice",
        workspace=str(workspace),
        cwd=str(workspace),
        approval_binding=approval,
    )
    deadline = time.time() + 10
    current = record
    while time.time() < deadline and current["status"] == "running":
        time.sleep(0.05)
        current = bg_jobs.refresh()[record["id"]]
    assert current["status"] == "done"
    assert not note.exists()


def test_overlay_publication_rejects_a_concurrent_source_change(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    upper = tmp_path / "upper"
    workspace.mkdir()
    upper.mkdir()
    note = workspace / "note.txt"
    note.write_text("old", encoding="utf-8")
    (upper / "note.txt").write_text("shell", encoding="utf-8")
    baseline = shell_worker._workspace_state(str(workspace))
    note.write_text("concurrent", encoding="utf-8")
    monkeypatch.setattr(
        "src.project_hex.validate_registered_project_candidates",
        lambda **_kwargs: {"allowed": True},
    )
    with pytest.raises(
        shell_policy.ShellApprovalError,
        match="changed while the isolated shell command",
    ):
        shell_worker._publish_overlay(
            str(workspace),
            str(upper),
            {"owner": "alice", "hex_db_path": str(tmp_path / "fm.db")},
            baseline,
        )
    assert note.read_text(encoding="utf-8") == "concurrent"


def test_background_command_uses_anonymous_worker_transport(
    tmp_path,
    monkeypatch,
):
    jobs = tmp_path / "jobs"
    workspace = tmp_path / "workspace"
    data = tmp_path / "data"
    jobs.mkdir()
    workspace.mkdir()
    monkeypatch.setenv("OPEN_CLANK_SHELL_SANDBOX", "off")
    monkeypatch.setattr(bg_jobs, "_STORE", tmp_path / "jobs.json")
    monkeypatch.setattr(bg_jobs, "_JOBS_DIR", jobs)
    monkeypatch.setattr("src.constants.DATA_DIR", str(data))
    secret = "sk-secret-secret-secret-secret"
    monkeypatch.setenv("OPENAI_API_KEY", secret)
    spawned = {}

    class Input:
        def write(self, value):
            spawned["command"] = bytes(value)

        def close(self):
            spawned["closed"] = True

    class Process:
        pid = 12345
        stdin = Input()

    def fake_popen(argv, **kwargs):
        spawned["argv"] = list(argv)
        spawned["env"] = dict(kwargs["env"])
        return Process()

    monkeypatch.setattr(bg_jobs.subprocess, "Popen", fake_popen)
    rec = bg_jobs.launch(
        f"printf 'token={secret}\\n'",
        session_id="chat-a",
        owner="alice",
        workspace=str(workspace),
        cwd=str(workspace),
    )

    spec = next(jobs.glob("*.spec.json")).read_text(encoding="utf-8")
    durable = (
        spec
        + (tmp_path / "jobs.json").read_text(encoding="utf-8")
        + (data / "shell-audit.jsonl").read_text(encoding="utf-8")
    )
    assert secret not in durable
    assert "command" not in json.loads(spec)
    assert all(secret not in argument for argument in spawned["argv"])
    assert secret not in spawned["env"].values()
    assert spawned["command"] == f"printf 'token={secret}\\n'".encode()
    assert spawned["closed"] is True
    assert secret not in rec["command"]


@pytest.mark.skipif(os.name == "nt", reason="worker fixture uses a POSIX FIFO")
def test_detached_worker_rechecks_bound_approval_before_spawn(
    tmp_path,
    monkeypatch,
):
    import subprocess
    from core.platform_compat import find_bash

    monkeypatch.setenv("OPEN_CLANK_SHELL_SANDBOX", "off")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "keep-me"
    target.write_text("safe", encoding="utf-8")
    spec_path = tmp_path / "worker.spec.json"
    log_path = tmp_path / "worker.log"
    exit_path = tmp_path / "worker.exit"
    spec_path.write_text(
        json.dumps({
            "command_size": len("rm keep-me".encode()),
            "workspace": str(workspace),
            "cwd": str(workspace),
            "log_path": str(log_path),
            "exit_path": str(exit_path),
            "child_pid_path": str(tmp_path / "child.pid"),
            "stdin_path": str(tmp_path / "stdin"),
            "stdin_ready_path": str(tmp_path / "stdin.ready"),
            "shell": find_bash() or "/bin/bash",
            "network": "enabled",
            "owner": "alice",
            "session_id": "chat-a",
            "destructive_actions": ["remove"],
            "approval_binding": "stale-binding",
        }),
        encoding="utf-8",
    )
    result = subprocess.run(
        [sys.executable, "-m", "src.shell_worker", str(spec_path)],
        cwd=str(Path(__file__).resolve().parents[1]),
        capture_output=True,
        text=True,
        input="rm keep-me",
        timeout=10,
    )
    assert result.returncode == 1
    assert target.exists()
    assert "no longer matches" in log_path.read_text(encoding="utf-8")
    assert exit_path.read_text(encoding="utf-8") == "1"


def test_hex_check_executes_verified_bytes_after_path_replacement(
    tmp_path,
    monkeypatch,
):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    contract = workspace / ".hex"
    contract.write_text("name: trusted\n", encoding="utf-8")
    resolution = resolve_hex(workspace, workspace_root=workspace)

    def replace_after_validation(_resolution, **kwargs):
        assert _resolution == resolution
        assert kwargs["candidates"] == {}
        replacement = workspace / ".hex.replacement"
        replacement.write_text("name: attacker\n", encoding="utf-8")
        os.replace(replacement, contract)
        return {"allowed": True, "findings": [], "warnings": []}

    monkeypatch.setattr(
        shell_worker,
        "validate_project_file_candidates",
        replace_after_validation,
    )
    with shell_worker.verified_contract_snapshot(resolution) as snapshot:
        shell_worker._run_hex_check(
            resolution,
            snapshot=snapshot,
            spec={},
            workspace=str(workspace),
            cwd=str(workspace),
        )
        snapshot.seek(0)
        assert snapshot.read() == b"name: trusted\n"
    assert contract.read_text(encoding="utf-8") == "name: attacker\n"


@pytest.mark.skipif(os.name == "nt", reason="verified snapshots use bubblewrap")
def test_hex_verified_bytes_survive_swap_between_check_and_user_spawn(
    tmp_path,
    monkeypatch,
):
    if (
        shell_policy._working_bwrap() is None
        or shell_policy._working_network_bwrap() is None
    ):
        pytest.skip("bubblewrap is unavailable")
    from core.platform_compat import find_bash
    import src.project_hex as project_hex

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    contract = workspace / ".hex"
    contract.write_text("name: trusted\n", encoding="utf-8")
    resolution = resolve_hex(workspace, workspace_root=workspace)
    command = "cat .hex > observed.hex"
    spec_path = tmp_path / "worker.spec.json"
    log_path = tmp_path / "worker.log"
    exit_path = tmp_path / "worker.exit"
    spec_path.write_text(
        json.dumps({
            "command_size": len(command.encode()),
            "workspace": str(workspace),
            "cwd": str(workspace),
            "log_path": str(log_path),
            "exit_path": str(exit_path),
            "child_pid_path": str(tmp_path / "child.pid"),
            "stdin_path": str(tmp_path / "stdin"),
            "stdin_ready_path": str(tmp_path / "stdin.ready"),
            "shell": find_bash() or "/bin/bash",
            "network": "enabled",
            "owner": "alice",
            "project_id": "project-a",
            "hex_target": str(workspace),
            "hex_db_path": str(tmp_path / "unused.db"),
            "hex_check": True,
            "destructive_actions": [],
        }),
        encoding="utf-8",
    )
    monkeypatch.setenv("OPEN_CLANK_SHELL_SANDBOX", "required")
    monkeypatch.setattr("src.constants.DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(shell_worker, "require_hex_activation", lambda *a, **k: resolution)
    monkeypatch.setattr(project_hex, "require_hex_activation", lambda *a, **k: resolution)
    real_popen = shell_worker.subprocess.Popen
    state = {"checked": False, "swapped": False}

    def track_check(_resolution, **kwargs):
        assert _resolution == resolution
        assert kwargs["candidates"] == {}
        state["checked"] = True
        return {"allowed": True, "findings": [], "warnings": []}

    def swap_before_user_spawn(argv, *args, **kwargs):
        if state["checked"] and not state["swapped"] and argv[-1] == command:
            replacement = workspace / ".hex.replacement"
            replacement.write_text("name: attacker\n", encoding="utf-8")
            os.replace(replacement, contract)
            state["swapped"] = True
            assert kwargs["pass_fds"]
        return real_popen(argv, *args, **kwargs)

    monkeypatch.setattr(
        shell_worker,
        "validate_project_file_candidates",
        track_check,
    )
    monkeypatch.setattr(shell_worker.subprocess, "Popen", swap_before_user_spawn)
    monkeypatch.setattr(
        shell_worker.sys,
        "stdin",
        type("Input", (), {"buffer": io.BytesIO(command.encode())})(),
    )

    assert shell_worker.run(str(spec_path)) == 0
    assert state == {"checked": True, "swapped": True}
    assert contract.read_text(encoding="utf-8") == "name: attacker\n"
    assert (workspace / "observed.hex").read_text(encoding="utf-8") == "name: trusted\n"


def test_followup_errors_are_redacted_before_the_durable_store(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(bg_jobs, "_STORE", tmp_path / "jobs.json")
    monkeypatch.setattr(bg_jobs, "_JOBS_DIR", tmp_path / "jobs")
    secret = "sk-secret-secret-secret-secret"
    monkeypatch.setenv("OPENAI_API_KEY", secret)
    bg_jobs._save({
        "job-a": {
            "followup_claim": "claim-a",
            "followup_attempts": 1,
        },
    })

    assert bg_jobs.fail_followup(
        "job-a",
        "claim-a",
        f"provider rejected {secret}",
    ) == "pending"
    stored = (tmp_path / "jobs.json").read_text(encoding="utf-8")
    assert secret not in stored
    assert "<redacted" in stored


def test_streaming_redaction_handles_split_credentials_and_pem_blocks():
    redactor = StreamingRedactor(holdback=8)
    output = "".join(
        [
            redactor.feed("before Bearer abcdef"),
            redactor.feed("ghijklmnopqrstuvwxyz after\n-----BEGIN PRIVATE "),
            redactor.feed("KEY-----\nsecret-body\n"),
            redactor.feed("-----END PRIVATE KEY-----\ndone"),
            redactor.finish(),
        ]
    )
    assert "Bearer <redacted>" in output
    assert "<redacted-pem-block>" in output
    assert "done" in output
    assert "abcdefghijklmnopqrstuvwxyz" not in output
    assert "secret-body" not in output


def test_streaming_redaction_drops_every_byte_of_oversized_unbroken_tokens():
    redactor = StreamingRedactor()
    output = "".join(
        [
            redactor.feed("x" * 70_000),
            redactor.feed(" done"),
            redactor.finish(),
        ]
    )
    assert output == "<redacted-long-token> done"


def test_foreground_capture_keeps_non_overlapping_head_and_tail():
    capture = _BoundedCapture(1024)
    for index in range(300):
        capture.append(f"{index:04d}-" + ("x" * 10))
    output = capture.text()
    assert output.count("0000-") == 1
    assert output.count("0299-") == 1
    assert "bytes omitted" in output
    assert len(output) < 1100


def test_foreground_capture_enforces_utf8_byte_budget():
    capture = _BoundedCapture(1024)
    capture.append_chunk("é" * 1_000)
    output = capture.text()
    assert len(output.encode("utf-8")) <= 1024
    assert "bytes omitted" in output


def test_foreground_capture_keeps_full_utf8_stream_digest_when_inline_is_bounded():
    import hashlib

    source = "😀" * 1_000 + "\nerror tail"
    capture = _BoundedCapture(1024)
    capture.append_chunk(source)

    metadata = capture.metadata()
    assert metadata["source_complete"] is True
    assert metadata["inline_truncated"] is True
    assert metadata["source_bytes"] == len(source.encode("utf-8"))
    assert metadata["value"] == hashlib.sha256(source.encode("utf-8")).hexdigest()
    assert len(capture.text().encode("utf-8")) <= 1024


def test_shell_argv_keeps_bash_powershell_and_cmd_contracts(monkeypatch):
    monkeypatch.setattr(shell_policy, "_IS_WINDOWS", True)
    assert shell_policy.shell_command_argv(
        r"C:\Program Files\Git\bin\bash.exe",
        "printf ok",
    ) == [
        r"C:\Program Files\Git\bin\bash.exe",
        "--noprofile",
        "--norc",
        "-c",
        "printf ok",
    ]
    assert shell_policy.shell_command_argv(
        r"C:\Program Files\PowerShell\7\pwsh.exe",
        "Write-Output ok",
    ) == [
        r"C:\Program Files\PowerShell\7\pwsh.exe",
        "-NoLogo",
        "-NoProfile",
        "-NonInteractive",
        "-Command",
        "Write-Output ok",
    ]
    assert shell_policy.shell_command_argv("cmd.exe", "echo ok") == [
        "cmd.exe",
        "/d",
        "/s",
        "/c",
        "echo ok",
    ]


def test_required_containment_never_silently_downgrades_on_windows(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(shell_policy, "_IS_WINDOWS", True)
    monkeypatch.setenv("OPEN_CLANK_SHELL_SANDBOX", "required")
    with pytest.raises(
        ShellContainmentError,
        match="unavailable on this platform",
    ):
        contained_argv(
            ["cmd.exe", "/c", "echo ok"],
            workspace=str(tmp_path),
            cwd=str(tmp_path),
        )


def test_auto_containment_falls_back_to_the_os_boundary_without_bwrap(
    tmp_path,
    monkeypatch,
):
    """2026-08-14 owner ruling: confinement is the OS boundary. `auto` uses
    bubblewrap when present and otherwise runs under the process account."""
    monkeypatch.setattr(shell_policy, "_IS_WINDOWS", False)
    monkeypatch.setattr(shell_policy, "_working_bwrap", lambda: None)
    monkeypatch.setenv("OPEN_CLANK_SHELL_SANDBOX", "auto")
    command = ["/bin/sh", "-c", "printf ok"]
    argv, mode = contained_argv(
        command,
        workspace=str(tmp_path),
        cwd=str(tmp_path),
    )
    assert argv == command
    assert mode == "off"


def test_required_containment_still_fails_closed_without_bwrap(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(shell_policy, "_IS_WINDOWS", False)
    monkeypatch.setattr(shell_policy, "_working_bwrap", lambda: None)
    monkeypatch.setenv("OPEN_CLANK_SHELL_SANDBOX", "required")
    with pytest.raises(
        ShellContainmentError,
        match="OPEN_CLANK_SHELL_SANDBOX=required",
    ):
        contained_argv(
            ["/bin/sh", "-c", "printf ok"],
            workspace=str(tmp_path),
            cwd=str(tmp_path),
        )


def test_explicit_off_runs_unsandboxed(tmp_path, monkeypatch):
    monkeypatch.setattr(shell_policy, "_IS_WINDOWS", False)
    monkeypatch.setattr(shell_policy, "_working_bwrap", lambda: None)
    monkeypatch.setenv("OPEN_CLANK_SHELL_SANDBOX", "off")
    command = ["/bin/sh", "-c", "printf ok"]
    argv, mode = contained_argv(
        command,
        workspace=str(tmp_path),
        cwd=str(tmp_path),
    )
    assert argv == command
    assert mode == "off"


@pytest.mark.skipif(
    os.name == "nt" or shell_policy._working_bwrap() is None,
    reason="working bubblewrap containment is required",
)
def test_bwrap_workspace_is_write_contained(tmp_path, monkeypatch):
    monkeypatch.setenv("OPEN_CLANK_SHELL_SANDBOX", "required")
    argv, mode = contained_argv(
        [
            "/bin/bash",
            "--noprofile",
            "--norc",
            "-c",
            "printf ok > inside; printf nope > /tmp/open-clank-shell-escape-probe",
        ],
        workspace=str(tmp_path),
        cwd=str(tmp_path),
    )
    import subprocess

    assert ["--tmpfs", "/run"] == argv[
        argv.index("/run") - 1:argv.index("/run") + 1
    ]
    result = subprocess.run(
        argv,
        env=minimal_shell_env(cwd=str(tmp_path)),
        capture_output=True,
        text=True,
    )
    assert mode == "bwrap"
    assert (tmp_path / "inside").read_text(encoding="utf-8") == "ok"
    assert result.returncode != 0
    assert not Path("/tmp/open-clank-shell-escape-probe").exists()


@pytest.mark.skipif(
    os.name == "nt" or shell_policy._working_bwrap() is None,
    reason="working bubblewrap overlay containment is required",
)
def test_bwrap_workspace_overlay_stages_writes_without_touching_source(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("OPEN_CLANK_SHELL_SANDBOX", "required")
    workspace = tmp_path / "workspace"
    upper = tmp_path / "upper"
    work = tmp_path / "work"
    workspace.mkdir()
    upper.mkdir()
    work.mkdir()
    (workspace / "note.txt").write_text("old", encoding="utf-8")
    argv, mode = contained_argv(
        ["/bin/sh", "-c", "printf new > note.txt; printf add > added.txt"],
        workspace=str(workspace),
        cwd=str(workspace),
        workspace_overlay=(str(upper), str(work)),
    )
    result = subprocess.run(
        argv,
        env=minimal_shell_env(cwd=str(workspace)),
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert mode == "bwrap-overlay"
    assert result.returncode == 0
    assert (workspace / "note.txt").read_text(encoding="utf-8") == "old"
    assert not (workspace / "added.txt").exists()
    assert (upper / "note.txt").read_text(encoding="utf-8") == "new"
    assert (upper / "added.txt").read_text(encoding="utf-8") == "add"
    os.chmod(work / "work", 0o700)


@pytest.mark.skipif(
    os.name == "nt" or shell_policy._working_bwrap() is None,
    reason="working bubblewrap containment is required",
)
def test_bwrap_masks_host_runtime_sockets(tmp_path, monkeypatch):
    runtime_dir = Path("/run/user") / str(os.getuid())
    if not runtime_dir.is_dir() or not os.access(runtime_dir, os.W_OK):
        pytest.skip("no writable host runtime directory for socket fixture")
    socket_path = runtime_dir / f"open-clank-containment-{uuid.uuid4().hex}.sock"
    listener = socket.socket(socket.AF_UNIX)
    try:
        listener.bind(str(socket_path))
        monkeypatch.setenv("OPEN_CLANK_SHELL_SANDBOX", "required")
        argv, mode = contained_argv(
            ["/usr/bin/test", "!", "-S", str(socket_path)],
            workspace=str(tmp_path),
            cwd=str(tmp_path),
        )
        import subprocess

        result = subprocess.run(
            argv,
            env=minimal_shell_env(cwd=str(tmp_path)),
            capture_output=True,
            text=True,
            timeout=5,
        )
        assert mode == "bwrap"
        assert result.returncode == 0
    finally:
        listener.close()
        socket_path.unlink(missing_ok=True)


@pytest.mark.skipif(
    os.name == "nt" or shell_policy._working_bwrap() is None,
    reason="working bubblewrap containment is required",
)
def test_bwrap_hides_app_control_data_inside_workspace(
    tmp_path,
    monkeypatch,
):
    workspace = tmp_path / "workspace"
    control = workspace / "data"
    control.mkdir(parents=True)
    app_db = control / "app.db"
    app_db.write_text("original", encoding="utf-8")
    monkeypatch.setenv("OPEN_CLANK_SHELL_SANDBOX", "required")
    monkeypatch.setattr("src.constants.DATA_DIR", str(control))
    monkeypatch.setattr("src.constants.AUTH_FILE", str(control / "auth.json"))
    monkeypatch.setattr("src.constants.FM_DB_PATH", str(control / "frankenmemory.db"))
    argv, mode = contained_argv(
        ["/bin/sh", "-c", "test ! -e data/app.db"],
        workspace=str(workspace),
        cwd=str(workspace),
    )
    import subprocess

    assert ["--tmpfs", str(control.resolve())] == argv[
        argv.index(str(control.resolve())) - 1:argv.index(str(control.resolve())) + 1
    ]
    result = subprocess.run(
        argv,
        env=minimal_shell_env(cwd=str(workspace)),
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert mode == "bwrap"
    assert result.returncode == 0
    assert app_db.read_text(encoding="utf-8") == "original"


@pytest.mark.skipif(
    os.name == "nt" or shell_policy._working_bwrap() is None,
    reason="working bubblewrap containment is required",
)
def test_bwrap_hides_app_control_data_outside_workspace(
    tmp_path,
    monkeypatch,
):
    workspace = tmp_path / "workspace"
    control = tmp_path / "control"
    workspace.mkdir()
    control.mkdir()
    app_db = control / "app.db"
    runtime_cache = control / "mimocode" / "cache"
    runtime_tool = runtime_cache / "bin" / "tool"
    runtime_tool.parent.mkdir(parents=True)
    app_db.write_text("secret", encoding="utf-8")
    runtime_tool.write_text("runtime", encoding="utf-8")
    monkeypatch.setenv("OPEN_CLANK_SHELL_SANDBOX", "required")
    monkeypatch.setenv("MIMOCODE_HOME", str(control / "mimocode"))
    monkeypatch.setattr("src.constants.DATA_DIR", str(control))
    monkeypatch.setattr("src.constants.AUTH_FILE", str(control / "auth.json"))
    monkeypatch.setattr("src.constants.FM_DB_PATH", str(control / "frankenmemory.db"))
    argv, mode = contained_argv(
        [
            "/bin/sh",
            "-c",
            f"test ! -e {app_db} && test \"$(cat {runtime_tool})\" = runtime",
        ],
        workspace=str(workspace),
        cwd=str(workspace),
    )
    import subprocess

    assert ["--tmpfs", str(control.resolve())] == argv[
        argv.index(str(control.resolve())) - 1:argv.index(str(control.resolve())) + 1
    ]
    result = subprocess.run(
        argv,
        env=minimal_shell_env(cwd=str(workspace)),
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert mode == "bwrap"
    assert result.returncode == 0
    assert app_db.read_text(encoding="utf-8") == "secret"


@pytest.mark.skipif(
    os.name == "nt" or shell_policy._working_bwrap() is None,
    reason="working bubblewrap containment is required",
)
def test_bwrap_restores_symlinked_mimo_cache_at_configured_path(
    tmp_path,
    monkeypatch,
):
    workspace = tmp_path / "workspace"
    control = tmp_path / "control"
    runtime_home = control / "mimocode"
    external_cache = tmp_path / "external-cache"
    runtime_tool = external_cache / "bin" / "tool"
    sibling_secret = control / "secret"
    workspace.mkdir()
    runtime_home.mkdir(parents=True)
    runtime_tool.parent.mkdir(parents=True)
    runtime_tool.write_text("runtime", encoding="utf-8")
    sibling_secret.write_text("secret", encoding="utf-8")
    (runtime_home / "cache").symlink_to(external_cache, target_is_directory=True)
    monkeypatch.setenv("OPEN_CLANK_SHELL_SANDBOX", "required")
    monkeypatch.setenv("MIMOCODE_HOME", str(runtime_home))
    monkeypatch.setattr("src.constants.DATA_DIR", str(control))
    monkeypatch.setattr("src.constants.AUTH_FILE", str(control / "auth.json"))
    monkeypatch.setattr("src.constants.FM_DB_PATH", str(control / "frankenmemory.db"))
    configured_tool = runtime_home / "cache" / "bin" / "tool"
    argv, mode = contained_argv(
        [
            "/bin/sh",
            "-c",
            (
                f"test ! -e {sibling_secret} && "
                f"test \"$(cat {configured_tool})\" = runtime"
            ),
        ],
        workspace=str(workspace),
        cwd=str(workspace),
    )
    import subprocess

    result = subprocess.run(
        argv,
        env=minimal_shell_env(cwd=str(workspace)),
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert mode == "bwrap"
    assert result.returncode == 0


@pytest.mark.skipif(
    os.name == "nt" or shell_policy._working_bwrap() is None,
    reason="working bubblewrap containment is required",
)
def test_bwrap_masks_control_siblings_while_preserving_nested_workspace(
    tmp_path,
    monkeypatch,
):
    workspace = tmp_path / "control" / "workspace"
    workspace.mkdir(parents=True)
    control = workspace.parent
    sibling_secret = control / "secret"
    sibling_secret.write_text("secret", encoding="utf-8")
    monkeypatch.setenv("OPEN_CLANK_SHELL_SANDBOX", "required")
    monkeypatch.setattr("src.constants.DATA_DIR", str(control))
    monkeypatch.setattr("src.constants.AUTH_FILE", str(control / "auth.json"))
    monkeypatch.setattr("src.constants.FM_DB_PATH", str(control / "frankenmemory.db"))
    argv, mode = contained_argv(
        [
            "/bin/sh",
            "-c",
            f"test ! -e {sibling_secret} && printf ok > result",
        ],
        workspace=str(workspace),
        cwd=str(workspace),
    )
    import subprocess

    assert ["--tmpfs", str(control.resolve())] in [
        argv[index:index + 2] for index in range(len(argv) - 1)
    ]
    result = subprocess.run(
        argv,
        env=minimal_shell_env(cwd=str(workspace)),
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert mode == "bwrap"
    assert result.returncode == 0
    assert (workspace / "result").read_text(encoding="utf-8") == "ok"


@pytest.mark.skipif(
    os.name == "nt" or shell_policy._working_bwrap() is None,
    reason="working bubblewrap containment is required",
)
def test_bwrap_hides_symlinked_control_data_inside_workspace(
    tmp_path,
    monkeypatch,
):
    workspace = tmp_path / "workspace"
    control = tmp_path / "control"
    workspace.mkdir()
    control.mkdir()
    app_db = control / "app.db"
    app_db.write_text("original", encoding="utf-8")
    link = workspace / "data"
    link.symlink_to(control, target_is_directory=True)
    monkeypatch.setenv("OPEN_CLANK_SHELL_SANDBOX", "required")
    monkeypatch.setattr("src.constants.DATA_DIR", str(link))
    monkeypatch.setattr("src.constants.AUTH_FILE", str(link / "auth.json"))
    monkeypatch.setattr("src.constants.FM_DB_PATH", str(link / "frankenmemory.db"))
    argv, mode = contained_argv(
        ["/bin/sh", "-c", "test ! -e data/app.db"],
        workspace=str(workspace),
        cwd=str(workspace),
    )
    import subprocess

    assert ["--tmpfs", str(control.resolve())] == argv[
        argv.index(str(control.resolve())) - 1:argv.index(str(control.resolve())) + 1
    ]
    result = subprocess.run(
        argv,
        env=minimal_shell_env(cwd=str(workspace)),
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert mode == "bwrap"
    assert result.returncode == 0
    assert app_db.read_text(encoding="utf-8") == "original"


@pytest.mark.skipif(os.name == "nt", reason="bubblewrap is POSIX-only")
def test_bwrap_network_disabled_is_enforced(tmp_path, monkeypatch):
    monkeypatch.setenv("OPEN_CLANK_SHELL_SANDBOX", "required")
    try:
        argv, mode = contained_argv(
            ["/bin/bash", "--noprofile", "--norc", "-c", "cat /proc/net/dev"],
            workspace=str(tmp_path),
            cwd=str(tmp_path),
            network="disabled",
        )
    except ShellContainmentError as exc:
        assert "network-disabled shell containment is unavailable" in str(exc)
        return
    import subprocess

    result = subprocess.run(
        argv,
        env=minimal_shell_env(cwd=str(tmp_path)),
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert mode == "bwrap"
    assert "--unshare-net" in argv
    assert result.returncode == 0
    interfaces = [
        line.split(":", 1)[0].strip()
        for line in result.stdout.splitlines()
        if ":" in line
    ]
    assert interfaces
    assert set(interfaces) == {"lo"}


def test_parser_profile_mounts_only_runtime_and_one_descriptor(tmp_path, monkeypatch):
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    monkeypatch.setattr(shell_policy, "_working_network_bwrap", lambda: "/usr/bin/bwrap")

    argv, mode = contained_parser_argv(
        [str(runtime / "python"), "-I", "-c", "print('ok')"],
        input_descriptor=17,
        runtime_roots=[str(runtime)],
    )

    assert mode == "bwrap"
    assert "--unshare-net" in argv
    assert "--unshare-pid" in argv
    assert "--clearenv" in argv
    assert ["--ro-bind-data", "17", "/input/source"] == argv[
        argv.index("--ro-bind-data") : argv.index("--ro-bind-data") + 3
    ]
    mounts = [
        argv[index + 1 : index + 3]
        for index, value in enumerate(argv)
        if value == "--ro-bind"
    ]
    assert ["/", "/"] not in mounts
    assert [str(runtime.resolve()), str(runtime)] in mounts
    assert not any(".ssh" in part or "data/frankenmemory" in part for part in argv)


def test_parser_profile_fails_closed_without_network_containment(monkeypatch):
    monkeypatch.setattr(shell_policy, "_working_network_bwrap", lambda: None)
    with pytest.raises(ShellContainmentError, match="parser containment is unavailable"):
        contained_parser_argv(["/usr/bin/python"], input_descriptor=3)


def test_foreground_shell_redacts_and_reports_containment(
    tmp_path,
    monkeypatch,
    foreground_shell_store,
):
    monkeypatch.setenv("OPEN_CLANK_SHELL_SANDBOX", "off")
    monkeypatch.setattr("src.constants.DATA_DIR", str(tmp_path / "data"))
    token = tool_execution._active_workspace.set(str(tmp_path))
    try:
        result = asyncio.run(
            BashTool().execute(
                "printf 'Bearer abcdefghijklmnopqrstuvwxyz\\n'",
                {"session_id": "chat-a", "owner": "alice"},
            )
        )
    finally:
        tool_execution._active_workspace.reset(token)
    if "error" in result:
        assert "network-disabled shell containment is unavailable" in result["error"]
        return
    assert result["exit_code"] == 0
    assert "abcdefghijklmnopqrstuvwxyz" not in result["output"]
    assert "<redacted>" in result["output"]
    assert result["containment"] in {"bwrap", "off"}


@pytest.mark.skipif(
    os.name == "nt" or shell_policy._working_bwrap() is None,
    reason="working bubblewrap containment is required",
)
def test_python_runtime_remains_available_for_workspace_under_home(tmp_path, monkeypatch):
    monkeypatch.setenv("OPEN_CLANK_SHELL_SANDBOX", "required")
    monkeypatch.setattr("src.constants.DATA_DIR", str(tmp_path / "data"))
    workspace = str(Path(__file__).resolve().parents[1])
    token = tool_execution._active_workspace.set(workspace)
    try:
        result = asyncio.run(
            PythonTool().execute(
                "print('runtime-ok')",
                {"session_id": "chat-a", "owner": "alice"},
            )
        )
    finally:
        tool_execution._active_workspace.reset(token)

    assert result["exit_code"] == 0
    assert result["output"] == "runtime-ok"
    assert result["containment"] == "bwrap"


def test_foreground_shell_can_disable_network(
    tmp_path,
    monkeypatch,
    foreground_shell_store,
):
    monkeypatch.setenv("OPEN_CLANK_SHELL_SANDBOX", "required")
    monkeypatch.setattr("src.constants.DATA_DIR", str(tmp_path / "data"))
    token = tool_execution._active_workspace.set(str(tmp_path))
    try:
        result = asyncio.run(
            BashTool().execute(
                {"command": "printf ok", "network": "disabled"},
                {"session_id": "chat-a", "owner": "alice"},
            )
        )
    finally:
        tool_execution._active_workspace.reset(token)
    if "error" in result:
        assert "network-disabled shell containment is unavailable" in result["error"]
        return
    assert result["exit_code"] == 0
    assert result["network"] == "disabled"
    assert result["containment"] == "bwrap"


@pytest.mark.skipif(os.name == "nt", reason="fixture uses POSIX seq")
def test_foreground_100k_lines_completes_without_marker_timeout(
    tmp_path,
    monkeypatch,
    foreground_shell_store,
):
    monkeypatch.setenv("OPEN_CLANK_SHELL_SANDBOX", "off")
    monkeypatch.setattr("src.constants.DATA_DIR", str(tmp_path / "data"))
    token = tool_execution._active_workspace.set(str(tmp_path))
    try:
        result = asyncio.run(
            BashTool().execute(
                "seq 1 100001",
                {"session_id": "chat-a", "owner": "alice"},
            )
        )
    finally:
        tool_execution._active_workspace.reset(token)
    assert result["exit_code"] == 0
    assert result["output"].startswith("1\n")
    assert "99999" in result["output"]
    assert "bytes omitted" in result["output"]


@pytest.mark.skipif(os.name == "nt", reason="fixture uses POSIX sleep")
def test_foreground_shell_promotes_to_the_same_durable_session(
    tmp_path,
    monkeypatch,
    foreground_shell_store,
):
    monkeypatch.setenv("OPEN_CLANK_SHELL_SANDBOX", "off")
    monkeypatch.setattr("src.constants.DATA_DIR", str(tmp_path / "data"))
    token = tool_execution._active_workspace.set(str(tmp_path))
    try:
        result = asyncio.run(
            BashTool().execute(
                "printf 'before\\n'; sleep 0.3; printf 'after\\n'",
                {
                    "session_id": "chat-a",
                    "owner": "alice",
                    "shell_foreground_wait_s": 0.02,
                },
            )
        )
    finally:
        tool_execution._active_workspace.reset(token)

    assert result["promoted"] is True
    assert result["state"] == "running"
    assert result["exit_code"] is None
    done = bg_jobs.wait(result["job_id"], timeout_s=10)
    assert done["status"] == "done"
    frame = bg_jobs.tail(
        result["job_id"],
        cursor=0,
        limit=1024,
        session_id="chat-a",
        owner="alice",
        workspace=str(tmp_path),
    )
    assert "before" in frame["output"]
    assert "after" in frame["output"]


@pytest.mark.skipif(os.name == "nt", reason="session stdin uses a POSIX FIFO")
def test_durable_shell_session_supports_cursor_and_stdin(tmp_path, monkeypatch):
    jobs_dir = tmp_path / "jobs"
    jobs_dir.mkdir()
    monkeypatch.setenv("OPEN_CLANK_SHELL_SANDBOX", "off")
    monkeypatch.setattr(bg_jobs, "_STORE", tmp_path / "jobs.json")
    monkeypatch.setattr(bg_jobs, "_JOBS_DIR", jobs_dir)
    monkeypatch.setenv("OPEN_CLANK_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr("src.constants.DATA_DIR", str(tmp_path / "data"))
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    rec = bg_jobs.launch(
        "printf 'ready\\n'; read value; printf 'got:%s\\n' \"$value\"",
        session_id="chat-a",
        owner="alice",
        workspace=str(workspace),
        cwd=str(workspace),
    )
    for _ in range(100):
        if Path(rec["stdin_path"]).exists():
            break
        time.sleep(0.02)
    assert bg_jobs.write(rec["id"], "hello\n") is True
    done = bg_jobs.wait(rec["id"], timeout_s=10)
    assert done["status"] == "done"
    first = bg_jobs.tail(rec["id"], cursor=0, limit=6)
    second = bg_jobs.tail(rec["id"], cursor=first["next_cursor"], limit=1024)
    assert first["truncated"] is True
    assert "ready" in first["output"]
    assert "got:hello" in second["output"]


@pytest.mark.skipif(os.name == "nt", reason="fixture uses POSIX stream tools")
def test_durable_shell_log_is_redacted_and_disk_bounded(tmp_path, monkeypatch):
    jobs_dir = tmp_path / "jobs"
    jobs_dir.mkdir()
    monkeypatch.setenv("OPEN_CLANK_SHELL_SANDBOX", "off")
    monkeypatch.setattr(bg_jobs, "_STORE", tmp_path / "jobs.json")
    monkeypatch.setattr(bg_jobs, "_JOBS_DIR", jobs_dir)
    monkeypatch.setenv("OPEN_CLANK_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr("src.constants.DATA_DIR", str(tmp_path / "data"))
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    secret = "abcdefghijklmnopqrstuvwxyz"
    rec = bg_jobs.launch(
        (
            f"printf 'Bearer {secret}\\n'; "
            "head -c 5242880 /dev/zero | tr '\\0' x"
        ),
        session_id="chat-a",
        owner="alice",
        workspace=str(workspace),
        cwd=str(workspace),
    )
    done = bg_jobs.wait(rec["id"], timeout_s=10)
    assert done["status"] == "done"
    retained = Path(rec["log_path"]).read_bytes()
    assert len(retained) <= (4 * 1024 * 1024) + 64
    assert secret.encode() not in retained
    assert b"<redacted>" in retained
    assert (
        b"output capped at 4 MiB" in retained
        or b"<redacted-long-token>" in retained
    )


class _ShellHistoryClient:
    def __init__(self):
        self.calls = []

    def prepare_batch(self, envelope, entries):
        self.calls.append(("prepare_batch", envelope, entries))
        return {"Accepted": None}

    def record_live(self, action_id, receipt):
        self.calls.append(("record_live", action_id, receipt))
        return {"Accepted": None}

    def complete_batch(self, action_id, entries):
        self.calls.append(("complete_batch", action_id, entries))
        return {"Accepted": None}

    def abort(self, action_id):
        self.calls.append(("abort", action_id))
        return {"Accepted": None}


def _open_fd_count():
    try:
        return len(os.listdir("/dev/fd"))
    except OSError:
        return None


def test_shell_mutation_capture_prepares_exact_root_and_after_manifest(tmp_path, monkeypatch):
    from src.openclank.history_capture import HistoryContext

    root = tmp_path / "workspace"
    root.mkdir()
    before = root / "before.txt"
    before.write_bytes(b"before")
    client = _ShellHistoryClient()
    context = HistoryContext(
        actor_id="actor-1",
        account_id="account-1",
        workspace_id="chat-1",
        roots=(str(root),),
        client=client,
    )
    monkeypatch.setattr(shell_worker, "context_from_mapping", lambda _raw: context)
    capture = shell_worker._prepare_shell_capture(
        {
            "history_context": {"history_capture": True},
            "history_roots": [str(root)],
            "destructive_actions": ["overwrite"],
            "session_id": "chat-1",
            "action_id": "action-1",
        },
        str(root),
        "printf after > before.txt",
    )
    assert capture is not None
    assert client.calls[0][0] == "prepare_batch"
    prepared = client.calls[0][2]
    assert prepared[0]["resource_type"] == "Directory"
    assert prepared[0]["content"]

    (root / "before.txt").write_bytes(b"after")
    (root / "created.txt").write_bytes(b"created")
    status = capture.finish(committed=True)
    assert status["history_status"] == "complete"
    completed = next(call for call in client.calls if call[0] == "complete_batch")
    manifest = json.loads(completed[2][0]["content"])
    entries = {item["path"]: item for item in manifest["entries"]}
    assert entries["before.txt"]["content"] == "YWZ0ZXI="
    assert entries["created.txt"]["content"] == "Y3JlYXRlZA=="


def test_shell_mutation_capture_reconciles_a_late_writer_before_completion(tmp_path, monkeypatch):
    from threading import Thread
    from src.openclank.history_capture import HistoryContext

    root = tmp_path / "workspace"
    root.mkdir()
    target = root / "late.txt"
    target.write_bytes(b"before")
    client = _ShellHistoryClient()
    context = HistoryContext(
        actor_id="actor-1",
        account_id="account-1",
        workspace_id="chat-1",
        roots=(str(root),),
        client=client,
    )
    monkeypatch.setattr(shell_worker, "context_from_mapping", lambda _raw: context)
    capture = shell_worker._prepare_shell_capture(
        {
            "history_context": {"history_capture": True},
            "history_roots": [str(root)],
            "destructive_actions": ["overwrite"],
            "action_id": "action-late",
            "late_writer_grace_ms": 220,
        },
        str(root),
        "writer",
    )
    assert capture is not None
    writer = Thread(target=lambda: (time.sleep(0.07), target.write_bytes(b"late")))
    writer.start()
    status = capture.finish(committed=True)
    writer.join()
    assert status["history_status"] == "complete"
    completed = next(call for call in client.calls if call[0] == "complete_batch")
    manifest = json.loads(completed[2][0]["content"])
    entry = next(item for item in manifest["entries"] if item["path"] == "late.txt")
    assert entry["content"] == "bGF0ZQ=="


def test_shell_capture_keeps_authenticated_worker_alive_for_descendant_reconciliation(tmp_path, monkeypatch):
    from src.openclank.history_capture import HistoryContext

    root = tmp_path / "workspace"
    root.mkdir()
    target = root / "late.txt"
    target.write_bytes(b"before")
    client = _ShellHistoryClient()
    context = HistoryContext(
        actor_id="actor-1", account_id="account-1", workspace_id="chat-1",
        roots=(str(root),), client=client,
    )
    monkeypatch.setattr(shell_worker, "context_from_mapping", lambda _raw: context)
    journal_path = tmp_path / "roots.journal.json"
    state_path = tmp_path / "capture.json"
    capture = shell_worker._prepare_shell_capture(
        {
            "history_context": {"history_capture": True},
            "history_roots": [str(root)],
            "destructive_actions": ["overwrite"],
            "owner": "alice", "session_id": "chat-1", "run_id": "run-1",
            "task_id": "task-1", "tool_id": "bash", "action_id": "action-late",
            "root_journal_path": str(journal_path),
            "capture_state_path": str(state_path),
            "late_writer_grace_ms": 10,
            "reconciliation_timeout_s": 1,
        },
        str(root),
        "writer",
    )
    assert capture is not None
    calls = 0

    def descendant_alive():
        nonlocal calls
        calls += 1
        if calls == 1:
            target.write_bytes(b"late")
            return True
        return False

    monkeypatch.setattr(capture, "_process_group_alive", descendant_alive)
    result = capture.finish(committed=True)
    assert result["history_status"] == "complete"
    assert calls >= 2
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["phase"] == "complete"
    journal = json.loads(journal_path.read_text(encoding="utf-8"))
    assert journal["phase"] == "settled"
    assert journal["generation"] == 1


def test_shell_mutation_capture_rejects_protected_reach_before_history_prepare(tmp_path, monkeypatch):
    from src.openclank.history_capture import HistoryContext

    root = tmp_path / "workspace"
    root.mkdir()
    (root / ".env").write_text("OPENAI_API_KEY=secret\n", encoding="utf-8")
    client = _ShellHistoryClient()
    context = HistoryContext(
        actor_id="actor-1",
        account_id="account-1",
        workspace_id="chat-1",
        roots=(str(root),),
        client=client,
    )
    monkeypatch.setattr(shell_worker, "context_from_mapping", lambda _raw: context)
    with pytest.raises(shell_policy.ShellApprovalError, match="protected path"):
        shell_worker._prepare_shell_capture(
            {
                "history_context": {"history_capture": True},
                "history_roots": [str(root)],
                "destructive_actions": ["overwrite"],
            },
            str(root),
            "rm -rf .",
        )
    assert client.calls == []


def test_shell_mutation_capture_rejects_symlink_reach_before_history_prepare(tmp_path, monkeypatch):
    from src.openclank.history_capture import HistoryContext

    root = tmp_path / "workspace"
    outside = tmp_path / "outside.txt"
    root.mkdir()
    outside.write_text("outside", encoding="utf-8")
    try:
        (root / "link.txt").symlink_to(outside)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"symlinks unavailable: {exc}")
    client = _ShellHistoryClient()
    context = HistoryContext(
        actor_id="actor-1",
        account_id="account-1",
        workspace_id="chat-1",
        roots=(str(root),),
        client=client,
    )
    monkeypatch.setattr(shell_worker, "context_from_mapping", lambda _raw: context)
    with pytest.raises(shell_policy.ShellApprovalError, match="symlink boundary"):
        shell_worker._prepare_shell_capture(
            {
                "history_context": {"history_capture": True},
                "history_roots": [str(root)],
                "destructive_actions": ["overwrite"],
            },
            str(root),
            "rm -rf .",
        )
    assert client.calls == []


@pytest.mark.parametrize("kind", ["protected", "nested-protected", "symlink"])
def test_shell_after_capture_rejects_new_protected_tree_entries(tmp_path, monkeypatch, kind):
    from src.openclank.history_capture import HistoryContext

    root = tmp_path / "workspace"
    root.mkdir()
    target = root / "tracked.txt"
    target.write_text("before", encoding="utf-8")
    outside = tmp_path / "outside.txt"
    outside.write_text("outside", encoding="utf-8")
    client = _ShellHistoryClient()
    context = HistoryContext(
        actor_id="actor-after",
        account_id="account-after",
        workspace_id="chat-after",
        roots=(str(root),),
        client=client,
    )
    monkeypatch.setattr(shell_worker, "context_from_mapping", lambda _raw: context)
    capture = shell_worker._prepare_shell_capture(
        {
            "history_context": {"history_capture": True},
            "history_roots": [str(root)],
            "action_id": f"after-{kind}",
            "late_writer_grace_ms": 120,
        },
        str(root),
        "printf after > tracked.txt",
    )
    assert capture is not None
    target.write_text("after", encoding="utf-8")
    if kind == "protected":
        (root / ".env.local").write_text("secret", encoding="utf-8")
    elif kind == "nested-protected":
        nested = root / "nested"
        nested.mkdir()
        (nested / "credentials.json").write_text("secret", encoding="utf-8")
    else:
        try:
            (root / "new-link").symlink_to(outside)
        except (OSError, NotImplementedError) as exc:
            pytest.skip(f"symlinks unavailable: {exc}")
    result = capture.finish(committed=True)
    assert result["history_status"] == "failed"
    assert result["capture_phase"] == "after_failed"
    assert not any(call[0] == "complete_batch" for call in client.calls)
    assert sum(call[0] == "record_live" for call in client.calls) == 1


@pytest.mark.parametrize("raced_kind", ["protected", "symlink"])
def test_shell_before_capture_rejects_raced_entry_before_prepare(tmp_path, monkeypatch, raced_kind):
    from src.openclank.history_capture import HistoryContext

    root = tmp_path / "workspace"
    root.mkdir()
    (root / "tracked.txt").write_text("before", encoding="utf-8")
    outside = tmp_path / "outside.txt"
    outside.write_text("outside", encoding="utf-8")
    client = _ShellHistoryClient()
    context = HistoryContext(
        actor_id="actor-before-race",
        account_id="account-before-race",
        workspace_id="chat-before-race",
        roots=(str(root),),
        client=client,
    )
    monkeypatch.setattr(shell_worker, "context_from_mapping", lambda _raw: context)
    original = shell_worker._history_capture._capture_payload
    raced = False
    armed = False
    touched: list[str] = []

    def race(path, **kwargs):
        nonlocal armed, raced
        if not raced:
            raced = True
            if raced_kind == "protected":
                (root / ".env").write_text("secret", encoding="utf-8")
            else:
                try:
                    (root / "raced-link").symlink_to(outside)
                except (OSError, NotImplementedError) as exc:
                    pytest.skip(f"symlinks unavailable: {exc}")
            armed = True
        return original(path, **kwargs)

    real_open = os.open

    def tracked_open(file, *args, **kwargs):
        if armed:
            try:
                candidate = os.fspath(file)
            except TypeError:
                candidate = ""
            if candidate.endswith(".env") or candidate.endswith("raced-link"):
                touched.append(candidate)
        return real_open(file, *args, **kwargs)

    monkeypatch.setattr(shell_worker._history_capture, "_capture_payload", race)
    monkeypatch.setattr(os, "open", tracked_open)
    with pytest.raises(shell_policy.ShellApprovalError, match="unavailable before spawn") as caught:
        shell_worker._prepare_shell_capture(
            {
                "history_context": {"history_capture": True},
                "history_roots": [str(root)],
                "action_id": f"before-race-{raced_kind}",
            },
            str(root),
            "printf after > tracked.txt",
        )
    assert raced is True, str(caught.value)
    assert touched == []
    assert client.calls == []


@pytest.mark.parametrize("replacement_kind", ["directory", "symlink"])
def test_shell_before_capture_rejects_nested_directory_replacement_before_prepare(
    tmp_path, monkeypatch, replacement_kind
):
    from src.openclank.history_capture import HistoryContext

    root = tmp_path / "workspace"
    nested = root / "nested"
    root.mkdir()
    nested.mkdir()
    (nested / "tracked.txt").write_text("before", encoding="utf-8")
    replacement = tmp_path / "replacement"
    replacement.mkdir()
    (replacement / "credentials.json").write_text("secret", encoding="utf-8")
    client = _ShellHistoryClient()
    context = HistoryContext(
        actor_id="actor-nested-before",
        account_id="account-nested-before",
        workspace_id="chat-nested-before",
        roots=(str(root),),
        client=client,
    )
    monkeypatch.setattr(shell_worker, "context_from_mapping", lambda _raw: context)
    real_open = os.open
    swapped = False
    payload_opened: list[str] = []
    nested_open_flags: list[int] = []

    def swap_nested(file, *args, **kwargs):
        nonlocal swapped
        if file == "nested" and kwargs.get("dir_fd") is not None and not swapped:
            nested_open_flags.append(int(args[0]))
            swapped = True
            nested.rename(tmp_path / "nested-original")
            if replacement_kind == "symlink":
                try:
                    nested.symlink_to(replacement, target_is_directory=True)
                except (OSError, NotImplementedError) as exc:
                    pytest.skip(f"symlinks unavailable: {exc}")
            else:
                nested.mkdir()
                (nested / "credentials.json").write_text("secret", encoding="utf-8")
        if file == "credentials.json":
            payload_opened.append(str(file))
        return real_open(file, *args, **kwargs)

    gc.collect()
    fd_before = _open_fd_count()
    monkeypatch.setattr(os, "open", swap_nested)
    with pytest.raises(shell_policy.ShellApprovalError, match="unavailable before spawn"):
        shell_worker._prepare_shell_capture(
            {
                "history_context": {"history_capture": True},
                "history_roots": [str(root)],
                "action_id": f"nested-before-{replacement_kind}",
            },
            str(root),
            "printf after > nested/tracked.txt",
        )
    assert swapped is True
    assert nested_open_flags and nested_open_flags[0] & os.O_NOFOLLOW
    assert payload_opened == []
    assert client.calls == []
    gc.collect()
    fd_after = _open_fd_count()
    if fd_before is not None and fd_after is not None:
        assert fd_after == fd_before


@pytest.mark.parametrize("replacement_kind", ["directory", "symlink"])
def test_shell_after_capture_rejects_nested_directory_replacement_before_complete(
    tmp_path, monkeypatch, replacement_kind
):
    from src.openclank.history_capture import HistoryContext

    root = tmp_path / "workspace"
    nested = root / "nested"
    root.mkdir()
    nested.mkdir()
    (nested / "tracked.txt").write_text("before", encoding="utf-8")
    replacement = tmp_path / "replacement"
    replacement.mkdir()
    (replacement / "credentials.json").write_text("secret", encoding="utf-8")
    client = _ShellHistoryClient()
    context = HistoryContext(
        actor_id="actor-nested-after",
        account_id="account-nested-after",
        workspace_id="chat-nested-after",
        roots=(str(root),),
        client=client,
    )
    monkeypatch.setattr(shell_worker, "context_from_mapping", lambda _raw: context)
    capture = shell_worker._prepare_shell_capture(
        {
            "history_context": {"history_capture": True},
            "history_roots": [str(root)],
            "action_id": f"nested-after-{replacement_kind}",
            "late_writer_grace_ms": 0,
        },
        str(root),
        "printf after > nested/tracked.txt",
    )
    assert capture is not None
    real_open = os.open
    swapped = False
    payload_opened: list[str] = []
    nested_open_flags: list[int] = []

    def swap_nested(file, *args, **kwargs):
        nonlocal swapped
        if file == "nested" and kwargs.get("dir_fd") is not None and not swapped:
            nested_open_flags.append(int(args[0]))
            swapped = True
            nested.rename(tmp_path / "nested-original")
            if replacement_kind == "symlink":
                try:
                    nested.symlink_to(replacement, target_is_directory=True)
                except (OSError, NotImplementedError) as exc:
                    pytest.skip(f"symlinks unavailable: {exc}")
            else:
                nested.mkdir()
                (nested / "credentials.json").write_text("secret", encoding="utf-8")
        if file == "credentials.json":
            payload_opened.append(str(file))
        return real_open(file, *args, **kwargs)

    fd_before = _open_fd_count()
    monkeypatch.setattr(os, "open", swap_nested)
    result = capture.finish(committed=True)
    assert swapped is True
    assert nested_open_flags and nested_open_flags[0] & os.O_NOFOLLOW
    assert result["history_status"] == "failed"
    assert result["capture_phase"] == "after_failed"
    assert payload_opened == []
    assert not any(call[0] == "complete_batch" for call in client.calls)
    assert sum(call[0] == "record_live" for call in client.calls) == 1
    fd_after = _open_fd_count()
    if fd_before is not None and fd_after is not None:
        assert fd_after == fd_before


def test_shell_capture_pins_root_identity_between_validation_and_prepare(tmp_path, monkeypatch):
    from src.openclank.history_capture import HistoryContext

    root = tmp_path / "workspace"
    root.mkdir()
    (root / "tracked.txt").write_text("before", encoding="utf-8")
    replacement = tmp_path / "replacement"
    client = _ShellHistoryClient()
    context = HistoryContext(
        actor_id="actor-root-race",
        account_id="account-root-race",
        workspace_id="chat-root-race",
        roots=(str(root),),
        client=client,
    )
    monkeypatch.setattr(shell_worker, "context_from_mapping", lambda _raw: context)
    original_roots = shell_worker._shell_history_roots
    swapped = False

    def validate_then_swap(spec, workspace, current_context):
        nonlocal swapped
        roots = original_roots(spec, workspace, current_context)
        root.rename(tmp_path / "workspace-original")
        replacement.mkdir()
        (replacement / "credentials.json").write_text("secret", encoding="utf-8")
        root.mkdir()
        swapped = True
        return roots

    monkeypatch.setattr(shell_worker, "_shell_history_roots", validate_then_swap)
    real_open = os.open
    payload_opened: list[str] = []

    def tracked_open(file, *args, **kwargs):
        if file == "credentials.json":
            payload_opened.append(str(file))
        return real_open(file, *args, **kwargs)

    fd_before = _open_fd_count()
    monkeypatch.setattr(os, "open", tracked_open)
    with pytest.raises(shell_policy.ShellApprovalError, match="unavailable before spawn"):
        shell_worker._prepare_shell_capture(
            {
                "history_context": {"history_capture": True},
                "history_roots": [str(root)],
                "action_id": "root-race",
            },
            str(root),
            "printf after > tracked.txt",
        )
    assert swapped is True
    assert payload_opened == []
    assert client.calls == []
    fd_after = _open_fd_count()
    if fd_before is not None and fd_after is not None:
        assert fd_after == fd_before


def test_shell_before_capture_rechecks_exact_tree_before_prepare(tmp_path, monkeypatch):
    from src.openclank.history_capture import HistoryContext

    root = tmp_path / "workspace"
    root.mkdir()
    (root / "tracked.txt").write_text("before", encoding="utf-8")
    client = _ShellHistoryClient()
    context = HistoryContext(
        actor_id="actor-prepare-boundary",
        account_id="account-prepare-boundary",
        workspace_id="workspace-prepare-boundary",
        roots=(str(root),),
        client=client,
    )
    monkeypatch.setattr(shell_worker, "context_from_mapping", lambda _raw: context)
    original_batch_entries = shell_worker._history_capture._batch_entries
    injected = False
    armed = False
    touched: list[str] = []

    def inject_after_manifest(*args, **kwargs):
        nonlocal armed, injected
        entries, before = original_batch_entries(*args, **kwargs)
        (root / ".env").write_text("secret", encoding="utf-8")
        injected = True
        armed = True
        return entries, before

    real_open = os.open

    def tracked_open(file, *args, **kwargs):
        if armed:
            try:
                candidate = os.fspath(file)
            except TypeError:
                candidate = ""
            if candidate.endswith(".env"):
                touched.append(candidate)
        return real_open(file, *args, **kwargs)

    monkeypatch.setattr(shell_worker._history_capture, "_batch_entries", inject_after_manifest)
    monkeypatch.setattr(os, "open", tracked_open)
    gc.collect()
    fd_before = _open_fd_count()
    with pytest.raises(shell_policy.ShellApprovalError, match="unavailable before spawn"):
        shell_worker._prepare_shell_capture(
            {
                "history_context": {"history_capture": True},
                "history_roots": [str(root)],
                "action_id": "prepare-boundary-race",
            },
            str(root),
            "printf after > tracked.txt",
        )
    assert injected is True
    assert touched == []
    assert client.calls == []
    gc.collect()
    fd_after = _open_fd_count()
    if fd_before is not None and fd_after is not None:
        assert fd_after == fd_before


def test_shell_after_capture_rechecks_payload_before_complete_batch(tmp_path, monkeypatch):
    from src.openclank.history_capture import HistoryContext

    root = tmp_path / "workspace"
    root.mkdir()
    target = root / "tracked.txt"
    target.write_bytes(b"before")
    client = _ShellHistoryClient()
    context = HistoryContext(
        actor_id="actor-complete-boundary",
        account_id="account-complete-boundary",
        workspace_id="workspace-complete-boundary",
        roots=(str(root),),
        client=client,
    )
    monkeypatch.setattr(shell_worker, "context_from_mapping", lambda _raw: context)
    original_payload = shell_worker._history_capture._capture_payload
    armed = False
    injected = False

    def inject_after_payload(path, **kwargs):
        nonlocal armed, injected
        result = original_payload(path, **kwargs)
        if armed and not injected and os.path.abspath(path) == os.path.abspath(root):
            target.write_bytes(b"raced!")
            injected = True
        return result

    monkeypatch.setattr(shell_worker._history_capture, "_capture_payload", inject_after_payload)
    capture = shell_worker._prepare_shell_capture(
        {
            "history_context": {"history_capture": True},
            "history_roots": [str(root)],
            "action_id": "complete-boundary-race",
            "late_writer_grace_ms": 0,
        },
        str(root),
        "printf after > tracked.txt",
    )
    assert capture is not None
    armed = True
    result = capture.finish(committed=True)
    assert injected is True
    assert result["history_status"] == "failed"
    assert result["capture_phase"] == "after_failed"
    assert not any(call[0] == "complete_batch" for call in client.calls)
    assert sum(call[0] == "record_live" for call in client.calls) == 1


@pytest.mark.parametrize("raced_kind", ["protected", "symlink"])
def test_shell_after_capture_rejects_tree_entry_before_open(tmp_path, monkeypatch, raced_kind):
    from src.openclank.history_capture import HistoryContext

    root = tmp_path / "workspace"
    root.mkdir()
    (root / "tracked.txt").write_text("before", encoding="utf-8")
    outside = tmp_path / "outside.txt"
    outside.write_text("outside", encoding="utf-8")
    client = _ShellHistoryClient()
    context = HistoryContext(
        actor_id="actor-race",
        account_id="account-race",
        workspace_id="chat-race",
        roots=(str(root),),
        client=client,
    )
    monkeypatch.setattr(shell_worker, "context_from_mapping", lambda _raw: context)
    capture = shell_worker._prepare_shell_capture(
        {
            "history_context": {"history_capture": True},
            "history_roots": [str(root)],
            "action_id": "after-race",
        },
        str(root),
        "printf after > tracked.txt",
    )
    assert capture is not None
    original = shell_worker._history_capture._capture_payload
    raced = False
    armed = False
    touched: list[str] = []

    def race(path, **kwargs):
        nonlocal armed, raced
        if not raced:
            raced = True
            if raced_kind == "protected":
                (root / ".env").write_text("secret", encoding="utf-8")
            else:
                try:
                    (root / "raced-link").symlink_to(outside)
                except (OSError, NotImplementedError) as exc:
                    pytest.skip(f"symlinks unavailable: {exc}")
            armed = True
        return original(path, **kwargs)

    real_open = os.open

    def tracked_open(file, *args, **kwargs):
        if armed:
            try:
                candidate = os.fspath(file)
            except TypeError:
                candidate = ""
            if candidate.endswith(".env") or candidate.endswith("raced-link"):
                touched.append(candidate)
        return real_open(file, *args, **kwargs)

    monkeypatch.setattr(shell_worker._history_capture, "_capture_payload", race)
    monkeypatch.setattr(os, "open", tracked_open)
    result = capture.finish(committed=True)
    assert result["history_status"] == "failed"
    assert result["capture_phase"] == "after_failed"
    assert not any(call[0] == "complete_batch" for call in client.calls)
    assert sum(call[0] == "record_live" for call in client.calls) == 1
    assert touched == []


def test_shell_after_capture_rejects_lstat_to_open_symlink_swap(tmp_path, monkeypatch):
    from src.openclank.history_capture import HistoryContext

    root = tmp_path / "workspace"
    root.mkdir()
    target = root / "tracked.txt"
    target.write_text("before", encoding="utf-8")
    outside = tmp_path / "outside.txt"
    outside.write_text("outside", encoding="utf-8")
    client = _ShellHistoryClient()
    context = HistoryContext(
        actor_id="actor-swap",
        account_id="account-swap",
        workspace_id="chat-swap",
        roots=(str(root),),
        client=client,
    )
    monkeypatch.setattr(shell_worker, "context_from_mapping", lambda _raw: context)
    capture = shell_worker._prepare_shell_capture(
        {
            "history_context": {"history_capture": True},
            "history_roots": [str(root)],
            "action_id": "after-symlink-swap",
        },
        str(root),
        "printf after > tracked.txt",
    )
    assert capture is not None
    real_open = os.open
    swapped = False

    def swap_on_open(file, *args, **kwargs):
        nonlocal swapped
        if file == "tracked.txt" and kwargs.get("dir_fd") is not None and not swapped:
            swapped = True
            target.unlink()
            target.symlink_to(outside)
        return real_open(file, *args, **kwargs)

    monkeypatch.setattr(os, "open", swap_on_open)
    result = capture.finish(committed=True)
    assert swapped is True
    assert result["history_status"] == "failed"
    assert result["capture_phase"] == "after_failed"
    assert not any(call[0] == "complete_batch" for call in client.calls)
    assert sum(call[0] == "record_live" for call in client.calls) == 1


def test_shell_after_capture_rejects_same_inode_content_race(tmp_path, monkeypatch):
    from src.openclank.history_capture import HistoryContext

    root = tmp_path / "workspace"
    root.mkdir()
    target = root / "tracked.txt"
    target.write_bytes(b"before")
    client = _ShellHistoryClient()
    context = HistoryContext(
        actor_id="actor-content-race",
        account_id="account-content-race",
        workspace_id="chat-content-race",
        roots=(str(root),),
        client=client,
    )
    monkeypatch.setattr(shell_worker, "context_from_mapping", lambda _raw: context)
    capture = shell_worker._prepare_shell_capture(
        {
            "history_context": {"history_capture": True},
            "history_roots": [str(root)],
            "action_id": "same-inode-content-race",
            "late_writer_grace_ms": 0,
        },
        str(root),
        "printf after > tracked.txt",
    )
    assert capture is not None
    real_open = os.open
    real_fstat = os.fstat
    tracked_fd = None
    fstat_samples = []

    def track_open(file, *args, **kwargs):
        nonlocal tracked_fd
        fd = real_open(file, *args, **kwargs)
        if file == "tracked.txt" and kwargs.get("dir_fd") is not None:
            tracked_fd = fd
        return fd

    def race_after_first_fstat(fd):
        info = real_fstat(fd)
        if fd == tracked_fd:
            fstat_samples.append(info)
            if len(fstat_samples) == 1:
                target.write_bytes(b"raced!")
        return info

    monkeypatch.setattr(os, "open", track_open)
    monkeypatch.setattr(os, "fstat", race_after_first_fstat)
    result = capture.finish(committed=True)
    assert tracked_fd is not None
    assert len(fstat_samples) == 2
    assert fstat_samples[0].st_ino == fstat_samples[1].st_ino
    assert result["history_status"] == "failed"
    assert result["capture_phase"] == "after_failed"
    assert not any(call[0] == "complete_batch" for call in client.calls)
    assert sum(call[0] == "record_live" for call in client.calls) == 1


@pytest.mark.parametrize(
    ("command", "mutating"),
    [
        ("git status", False),
        ("git diff -- README.md", False),
        ("cat README.md", False),
        ("printf 'ready\\n'; sleep 0", False),
        ("git add README.md", True),
        ("git commit -m save", True),
        ("make all", True),
        ("python -c 'open(\"x\", \"w\").write(\"x\")'", True),
        ("unknown-local-tool --write", True),
        ("printf x > output.txt", True),
    ],
)
def test_shell_capture_mutation_classifier_is_conservative(command, mutating):
    assert shell_worker._shell_command_may_mutate({}, command) is mutating


def test_mutating_detached_shell_without_authenticated_context_is_truthfully_unavailable(tmp_path):
    state_path = tmp_path / "capture.json"
    assert shell_worker._prepare_shell_capture(
        {
            "action_id": "action-no-context", "destructive_actions": [],
            "capture_state_path": str(state_path),
        },
        str(tmp_path), "git add README.md",
    ) is None
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["phase"] == "unavailable"
    assert state["history_status"] == "unavailable"


@pytest.mark.skipif(os.name == "nt", reason="fixture invokes the POSIX shell worker")
def test_configured_but_down_history_worker_keeps_approved_shell_live(tmp_path, monkeypatch):
    root = tmp_path / "workspace"
    root.mkdir()
    command = "printf live > live.txt"
    spec = {
        "command_size": len(command.encode()), "workspace": str(root), "cwd": str(root),
        "log_path": str(tmp_path / "worker.log"), "exit_path": str(tmp_path / "worker.exit"),
        "child_pid_path": str(tmp_path / "worker.child.pid"),
        "stdin_path": str(tmp_path / "worker.stdin"),
        "stdin_ready_path": str(tmp_path / "worker.stdin.ready"),
        "capture_state_path": str(tmp_path / "worker.capture.json"),
        "root_journal_path": str(tmp_path / "worker.root-journal.json"),
        "shell": "/bin/bash", "network": "enabled", "owner": "alice", "session_id": "chat-1",
        "run_id": "run-1", "task_id": "task-1", "tool_id": "bash", "action_id": "action-down",
        "history_context": {
            "history_capture": True, "actor_id": "alice", "account_id": "acct-1",
            "workspace_id": "workspace-1", "history_socket": str(tmp_path / "down.sock"),
            "history_roots": [str(root)],
        },
        "history_roots": [str(root)], "destructive_actions": [],
    }
    spec_path = tmp_path / "worker.spec.json"
    spec_path.write_text(json.dumps(spec), encoding="utf-8")
    env = dict(os.environ)
    env["OPEN_CLANK_SHELL_SANDBOX"] = "off"
    result = subprocess.run(
        [sys.executable, "-m", "src.shell_worker", str(spec_path)], input=command.encode(),
        cwd=Path(__file__).parents[1], env=env, capture_output=True, timeout=20, check=False,
    )
    assert result.returncode == 0, result.stderr.decode(errors="replace")
    assert (root / "live.txt").read_text(encoding="utf-8") == "live"
    state = json.loads((tmp_path / "worker.capture.json").read_text(encoding="utf-8"))
    assert state["phase"] == "unavailable"
    assert state["coverage_status"] == "unavailable"


def test_strict_shell_capture_unsupported_host_is_truthfully_unavailable(tmp_path, monkeypatch):
    context = {
        "history_capture": True,
        "actor_id": "actor-platform",
        "account_id": "account-platform",
        "workspace_id": "workspace-platform",
        "history_roots": [str(tmp_path)],
    }
    monkeypatch.setattr(shell_worker._history_capture, "_strict_capture_supported", lambda: False)
    capture = shell_worker._prepare_shell_capture(
        {"history_context": context, "history_roots": [str(tmp_path)], "action_id": "action-platform"},
        str(tmp_path), "printf x > output.txt",
    )
    assert capture is not None
    assert capture.finish(committed=True)["history_status"] == "unavailable"


@pytest.mark.parametrize("missing_flag", ["O_NOFOLLOW", "O_DIRECTORY", "O_CLOEXEC"])
def test_strict_shell_capture_missing_open_flag_fails_before_prepare(
    tmp_path, monkeypatch, missing_flag
):
    context = {
        "history_capture": True,
        "actor_id": "actor-platform-flag",
        "account_id": "account-platform-flag",
        "workspace_id": "workspace-platform-flag",
        "history_roots": [str(tmp_path)],
    }
    monkeypatch.delattr(os, missing_flag, raising=False)
    capture = shell_worker._prepare_shell_capture(
        {"history_context": context, "history_roots": [str(tmp_path)], "action_id": f"action-missing-{missing_flag}"},
        str(tmp_path), "printf x > output.txt",
    )
    assert capture is not None
    assert capture.finish(committed=True)["capture_phase"] == "unavailable"


def test_strict_shell_capture_missing_descriptor_scandir_fails_before_prepare(tmp_path, monkeypatch):
    context = {
        "history_capture": True,
        "actor_id": "actor-platform-scandir",
        "account_id": "account-platform-scandir",
        "workspace_id": "workspace-platform-scandir",
        "history_roots": [str(tmp_path)],
    }
    monkeypatch.setattr(shell_worker._history_capture, "_SCANDIR_SUPPORTS_FD", False)
    capture = shell_worker._prepare_shell_capture(
        {"history_context": context, "history_roots": [str(tmp_path)], "action_id": "action-missing-scandir"},
        str(tmp_path), "printf x > output.txt",
    )
    assert capture is not None
    assert capture.finish(committed=True)["capture_phase"] == "unavailable"


def test_reconciliation_polls_metadata_without_manifest_rereads(tmp_path, monkeypatch):
    from src.openclank.history_capture import HistoryContext

    root = tmp_path / "workspace"
    root.mkdir()
    (root / "note.txt").write_text("before", encoding="utf-8")
    client = _ShellHistoryClient()
    context = HistoryContext(
        actor_id="actor",
        account_id="account",
        workspace_id="chat",
        roots=(str(root),),
        client=client,
    )
    monkeypatch.setattr(shell_worker, "context_from_mapping", lambda _raw: context)
    original = shell_worker._history_capture._capture_payload
    calls = 0

    def counted(path, **kwargs):
        nonlocal calls
        calls += 1
        return original(path, **kwargs)

    monkeypatch.setattr(shell_worker._history_capture, "_capture_payload", counted)
    capture = shell_worker._prepare_shell_capture(
        {
            "history_context": {"history_capture": True},
            "history_roots": [str(root)],
            "action_id": "action-cost",
            "late_writer_grace_ms": 180,
        },
        str(root),
        "git add README.md",
    )
    assert capture is not None
    before_finish = calls
    (root / "note.txt").write_text("after", encoding="utf-8")
    assert capture.finish(committed=True)["history_status"] == "complete"
    # Prepare reads the exact preimage once; completion reads each declared
    # root once for the final after-state.  Polling must not add scans.
    assert calls - before_finish <= 2


@pytest.mark.skipif(os.name == "nt", reason="detached worker fixture uses POSIX FIFO")
def test_real_detached_worker_history_service_journey(tmp_path, monkeypatch):
    """Run the owned worker through its OS boundary against the real service.

    The repository does not ship a service binary in every checkout; the
    externally supplied qualification binary is the only accepted provider
    authority.  When present this exercises prepare/complete, exact subtree
    after-state, service restart, and a second replay action.
    """
    configured_binary = os.environ.get("OPENCLANK_HISTORY_TEST_BIN", "").strip()
    if not configured_binary or not Path(configured_binary).is_file():
        pytest.skip("OPENCLANK_HISTORY_TEST_BIN is required for real-service qualification")
    from src.openclank.history_client import HistoryServiceSupervisor, ScopedHistoryCredential

    root = tmp_path / "workspace"
    root.mkdir()
    (root / "one.txt").write_text("one-before", encoding="utf-8")
    (root / "two.txt").write_text("two-before", encoding="utf-8")
    jobs = tmp_path / "jobs"
    jobs.mkdir()
    root_id = "s03-root"
    history_context = {
        "history_capture": True,
        "actor_id": "actor-s03",
        "account_id": "acct-s03",
        "workspace_id": "chat-s03",
        "history_roots": [{"root_id": root_id, "canonical_path": str(root)}],
        "session_id": "chat-s03",
        "run_id": "run-s03",
        "task_id": "task-s03",
        "tool_id": "mimo-bash",
    }
    socket_path = Path("/tmp") / f"oc-s03-{os.getpid()}-{uuid.uuid4().hex[:6]}.sock"

    async def exercise():
        credential = ScopedHistoryCredential(
            actor_id="*",
            account_id="acct-s03",
            capabilities=frozenset({"admin", "capture", "read", "restore"}),
        )
        supervisor = HistoryServiceSupervisor(
            configured_binary,
            socket_path=socket_path,
            catalog_path=tmp_path / "history.redb",
            lore_root=tmp_path / "lore",
            credential_file=tmp_path / "credentials.json",
            credentials=[credential],
            host_root=root,
            authorized_roots=[
                {
                    "root_id": root_id,
                    "canonical_path": str(root),
                    "account_ids": ["acct-s03"],
                    "workspace_ids": ["chat-s03"],
                }
            ],
        )
        await supervisor.start()
        try:
            history_context["history_socket"] = str(socket_path)
            history_context["history_token"] = credential.token

            async def one(action_id: str):
                spec_path = jobs / f"{action_id}.spec.json"
                spec = {
                    "command_size": 0,
                    "workspace": str(root),
                    "cwd": str(root),
                    "log_path": str(jobs / f"{action_id}.log"),
                    "exit_path": str(jobs / f"{action_id}.exit"),
                    "child_pid_path": str(jobs / f"{action_id}.child.pid"),
                    "stdin_path": str(jobs / f"{action_id}.stdin"),
                    "stdin_ready_path": str(jobs / f"{action_id}.stdin.ready"),
                    "capture_state_path": str(jobs / f"{action_id}.capture.json"),
                    "shell": "/bin/bash",
                    "network": "enabled",
                    "owner": "acct-s03",
                    "session_id": "chat-s03",
                    "run_id": "run-s03",
                    "task_id": "task-s03",
                    "action_id": action_id,
                    "history_context": history_context,
                    "history_roots": [history_context["history_roots"][0]],
                    "destructive_actions": [],
                }
                command = (
                    "printf changed > one.txt; mkdir -p subtree; printf created > subtree/new.txt; mv two.txt replaced.txt"
                    if action_id.endswith("1")
                    else "printf changed-again > one.txt; mv replaced.txt replaced-again.txt"
                )
                spec["command_size"] = len(command.encode())
                spec_path.write_text(json.dumps(spec), encoding="utf-8")
                env = dict(os.environ)
                env["OPEN_CLANK_SHELL_SANDBOX"] = "off"
                result = subprocess.run(
                    [sys.executable, "-m", "src.shell_worker", str(spec_path)],
                    input=command.encode(),
                    cwd=Path(__file__).parents[1],
                    env=env,
                    capture_output=True,
                    timeout=30,
                    check=False,
                )
                assert result.returncode == 0, result.stderr.decode(errors="replace")
                state = json.loads((jobs / f"{action_id}.capture.json").read_text())
                assert state["phase"] == "complete"
                assert state["result"]["receipt"]

            await one("s03-real-1")
            await supervisor.stop()
            await supervisor.start()
            # A restarted provider accepts a fresh durable action and retains
            # the first action in its catalog; this is the replay boundary.
            await one("s03-real-2")
        finally:
            await supervisor.stop()

    asyncio.run(exercise())
