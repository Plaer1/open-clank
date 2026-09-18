import asyncio
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
    assert "chars omitted" in output
    assert len(output) < 1100


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
            "printf ok > inside; printf nope > /home/e/open-clank-shell-escape-probe",
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
    assert not Path("/home/e/open-clank-shell-escape-probe").exists()


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
    assert "chars omitted" in result["output"]


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
