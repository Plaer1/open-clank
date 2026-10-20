"""Sudo escalation: after a destructive command is approved, the UI can supply
the sudo password through a second, separate prompt. The secret is single-use,
TTL-bound, and never lands in the command line, the job spec, or the audit log.
"""

import asyncio
import json
import time
from pathlib import Path

import pytest

from src import bg_jobs, shell_policy


@pytest.fixture(autouse=True)
def _clean_sudo_state(tmp_path, monkeypatch):
    monkeypatch.setattr("src.constants.DATA_DIR", str(tmp_path / "data"))
    shell_policy._approval_grant_store.cache_clear()
    shell_policy._SUDO_SECRETS.clear()
    shell_policy._PENDING_SUDO_PASSWORDS.clear()
    yield
    shell_policy._SUDO_SECRETS.clear()
    shell_policy._PENDING_SUDO_PASSWORDS.clear()
    shell_policy._approval_grant_store.cache_clear()


def _ctx(workspace, progress=None):
    ctx = {
        "session_id": "chat-a",
        "owner": "alice",
        "workspace": str(workspace),
    }
    if progress is not None:
        ctx["progress_cb"] = progress
    return ctx


def test_command_needs_sudo_password_detection():
    assert shell_policy.command_needs_sudo_password("sudo rm stale.txt")
    assert shell_policy.command_needs_sudo_password("ls | sudo tee out.txt")
    assert shell_policy.command_needs_sudo_password("true && sudo apt update")
    # Quoted text is not a command position.
    assert not shell_policy.command_needs_sudo_password('echo "sudo rm x"')
    # doas/pkexec/su still gate on the privilege action but have no askpass lane.
    assert not shell_policy.command_needs_sudo_password("doas rm x")
    assert not shell_policy.command_needs_sudo_password("rm x")


def test_inject_sudo_askpass_rewrites_command_positions():
    assert shell_policy.inject_sudo_askpass("sudo rm x") == "sudo -A rm x"
    assert shell_policy.inject_sudo_askpass("a && sudo b") == "a && sudo -A b"
    assert shell_policy.inject_sudo_askpass("a; sudo b") == "a; sudo -A b"
    assert shell_policy.inject_sudo_askpass("a | sudo b") == "a | sudo -A b"
    # Not command position: left alone.
    assert shell_policy.inject_sudo_askpass("echo sudo done") == "echo sudo done"


def test_sudo_password_prompt_follows_approval_and_is_single_use(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    events = []

    async def scenario():
        async def progress(payload):
            events.append(payload)
            data = payload["data"]
            if payload["type"] == "permission_request":
                assert shell_policy.resolve_shell_approval(
                    data["request_id"], "once", owner="alice", session_id="chat-a"
                )
            elif payload["type"] == "sudo_password_request":
                assert data["sudo_password"] is True
                assert data["options"] == ["once", "reject"]
                # A foreign owner cannot answer the prompt.
                assert not shell_policy.resolve_sudo_password(
                    data["request_id"], "once",
                    secret="wrong", owner="bob", session_id="chat-a",
                )
                assert shell_policy.resolve_sudo_password(
                    data["request_id"], "once",
                    secret="hunter2", owner="alice", session_id="chat-a",
                )

        return await shell_policy.require_shell_approval(
            "sudo rm stale.txt",
            ctx=_ctx(workspace, progress),
            cwd=str(workspace),
            containment="bwrap",
        )

    binding = asyncio.run(scenario())
    assert [event["type"] for event in events] == [
        "permission_request",
        "sudo_password_request",
    ]
    # The password prompt never carries the secret, and the command detail is
    # the redacted approved command.
    sudo_event = events[1]["data"]
    assert "hunter2" not in json.dumps(events)
    assert "sudo rm stale.txt" in sudo_event["detail"]["command"]

    assert shell_policy.pop_sudo_secret(binding) == "hunter2"
    assert shell_policy.pop_sudo_secret(binding) is None  # single-use


def test_sudo_password_rejection_runs_without_secret(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    async def scenario():
        async def progress(payload):
            data = payload["data"]
            if payload["type"] == "permission_request":
                shell_policy.resolve_shell_approval(
                    data["request_id"], "once", owner="alice", session_id="chat-a"
                )
            else:
                assert shell_policy.resolve_sudo_password(
                    data["request_id"], "reject", owner="alice", session_id="chat-a"
                )

        return await shell_policy.require_shell_approval(
            "sudo rm stale.txt",
            ctx=_ctx(workspace, progress),
            cwd=str(workspace),
            containment="bwrap",
        )

    binding = asyncio.run(scenario())
    assert shell_policy.pop_sudo_secret(binding) is None


def test_sudo_password_prompt_needs_interactive_surface(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    events = []

    async def scenario():
        async def progress(payload):
            events.append(payload)
            if payload["type"] == "permission_request":
                shell_policy.resolve_shell_approval(
                    payload["data"]["request_id"], "always",
                    owner="alice", session_id="chat-a",
                )
            else:
                shell_policy.resolve_sudo_password(
                    payload["data"]["request_id"], "reject",
                    owner="alice", session_id="chat-a",
                )

        # First run approves with an "always" grant and declines the password.
        binding = await shell_policy.require_shell_approval(
            "sudo rm stale.txt",
            ctx=_ctx(workspace, progress),
            cwd=str(workspace),
            containment="bwrap",
        )
        assert shell_policy.pop_sudo_secret(binding) is None
        # Second run reuses the durable grant: with no progress_cb there is no
        # password prompt either — the command runs and sudo fails naturally.
        again = await shell_policy.require_shell_approval(
            "sudo rm stale.txt",
            ctx=_ctx(workspace),
            cwd=str(workspace),
            containment="bwrap",
        )
        assert again == binding
        assert shell_policy.pop_sudo_secret(binding) is None

    asyncio.run(scenario())
    assert [event["type"] for event in events] == [
        "permission_request",
        "sudo_password_request",
    ]


def test_sudo_secret_expires(tmp_path):
    shell_policy._SUDO_SECRETS["binding-x"] = ("hunter2", time.time() - 1)
    assert shell_policy.pop_sudo_secret("binding-x") is None


def test_auto_permission_mode_still_prompts_for_password(tmp_path, monkeypatch):
    # Auto mode grants the destructive approval itself, but credential delivery
    # is never auto-approved: the password prompt still appears.
    monkeypatch.setattr("src.permission_mode.auto_approves", lambda owner: True)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    events = []

    async def scenario():
        async def progress(payload):
            events.append(payload)
            if payload["type"] == "sudo_password_request":
                shell_policy.resolve_sudo_password(
                    payload["data"]["request_id"], "once",
                    secret="hunter2", owner="alice", session_id="chat-a",
                )

        return await shell_policy.require_shell_approval(
            "sudo rm stale.txt",
            ctx=_ctx(workspace, progress),
            cwd=str(workspace),
            containment="bwrap",
        )

    binding = asyncio.run(scenario())
    assert [event["type"] for event in events] == ["sudo_password_request"]
    assert shell_policy.pop_sudo_secret(binding) == "hunter2"


def test_launch_delivers_secret_via_env_only(tmp_path, monkeypatch):
    monkeypatch.setenv("OPEN_CLANK_SHELL_SANDBOX", "off")
    jobs = tmp_path / "jobs"
    workspace = tmp_path / "workspace"
    jobs.mkdir()
    workspace.mkdir()
    monkeypatch.setattr(bg_jobs, "_STORE", tmp_path / "jobs.json")
    monkeypatch.setattr(bg_jobs, "_JOBS_DIR", jobs)

    # The worker consumes and deletes its spec file, so capture it at write
    # time to prove the secret is never persisted with the job spec.
    written_specs = []
    real_write = bg_jobs.atomic_write_json

    def spy_write(path, data, *args, **kwargs):
        if str(path).endswith(".spec.json"):
            written_specs.append(json.dumps(data))
        return real_write(path, data, *args, **kwargs)

    monkeypatch.setattr(bg_jobs, "atomic_write_json", spy_write)

    record = bg_jobs.launch(
        "printenv OPEN_CLANK_SUDO_SECRET",
        session_id="chat-a",
        owner="alice",
        workspace=str(workspace),
        cwd=str(workspace),
        extra_env={
            "SUDO_ASKPASS": str(jobs / "sudo-askpass.sh"),
            "OPEN_CLANK_SUDO_SECRET": "hunter2",
        },
    )
    deadline = time.time() + 15
    current = record
    while time.time() < deadline and current["status"] == "running":
        time.sleep(0.05)
        current = bg_jobs.refresh()[record["id"]]
    assert current["status"] == "done"
    log_text = Path(record["log_path"]).read_text(encoding="utf-8")
    # The env var reached the worker (printenv found it), and the worker's own
    # StreamingRedactor masked the exact value before it hit the log file.
    assert "<redacted>" in log_text
    assert "hunter2" not in log_text

    assert written_specs
    assert "hunter2" not in written_specs[0]
