"""Permission mode (manual / yolo / auto) — QOL-pass slice 5.

Pins: pref parsing fails closed to manual; yolo/auto auto-approve the shell
and file approval lanes with once semantics (no durable grant, no UI wait);
auto additionally suppresses both question channels; manual is unchanged.
"""

import asyncio

import pytest

import routes.prefs_routes as prefs_routes
from src.permission_mode import (
    auto_approves,
    permission_mode_for_owner,
    suppresses_questions,
)


@pytest.fixture
def prefs_file(tmp_path, monkeypatch):
    path = tmp_path / "user_prefs.json"
    monkeypatch.setattr(prefs_routes, "PREFS_FILE", str(path))
    return path


def _set_mode(owner, mode):
    prefs_routes._save_for_user(owner, {"permission_mode": mode})


# ── pref parsing ──


def test_default_is_manual(prefs_file):
    assert permission_mode_for_owner("alice") == "manual"
    assert permission_mode_for_owner("") == "manual"
    assert permission_mode_for_owner(None) == "manual"


def test_mode_roundtrip(prefs_file):
    _set_mode("alice", "yolo")
    assert permission_mode_for_owner("alice") == "yolo"
    assert auto_approves("alice") is True
    assert suppresses_questions("alice") is False
    _set_mode("alice", "auto")
    assert suppresses_questions("alice") is True
    _set_mode("bob", "manual")
    assert auto_approves("bob") is False
    # Modes are per-owner.
    assert permission_mode_for_owner("alice") == "auto"
    assert permission_mode_for_owner("bob") == "manual"


def test_garbage_value_fails_closed(prefs_file):
    _set_mode("alice", "anything-goes")
    assert permission_mode_for_owner("alice") == "manual"
    assert auto_approves("alice") is False


# ── shell lane ──


def _shell_ctx(owner, workspace):
    return {"session_id": "chat-a", "owner": owner, "workspace": workspace}


def test_yolo_auto_approves_destructive_shell(prefs_file, tmp_path):
    from src import shell_policy

    workspace = tmp_path / "ws"
    workspace.mkdir()
    _set_mode("alice", "yolo")

    binding = asyncio.run(
        shell_policy.require_shell_approval(
            "rm stale.txt",
            ctx=_shell_ctx("alice", str(workspace)),
            cwd=str(workspace),
            containment="none",
            network="enabled",
        )
    )
    assert binding  # approved without any progress_cb or durable grant


def test_manual_shell_still_requires_interactive(prefs_file, tmp_path):
    from src import shell_policy

    workspace = tmp_path / "ws"
    workspace.mkdir()
    _set_mode("alice", "manual")

    with pytest.raises(shell_policy.ShellApprovalError, match="interactive approval"):
        asyncio.run(
            shell_policy.require_shell_approval(
                "rm stale.txt",
                ctx=_shell_ctx("alice", str(workspace)),
                cwd=str(workspace),
                containment="none",
                network="enabled",
            )
        )


def test_yolo_writes_no_durable_grant(prefs_file, tmp_path):
    from src import shell_policy

    workspace = tmp_path / "ws"
    workspace.mkdir()
    _set_mode("alice", "yolo")
    ctx = _shell_ctx("alice", str(workspace))

    binding = asyncio.run(
        shell_policy.require_shell_approval(
            "rm stale.txt", ctx=ctx, cwd=str(workspace),
            containment="none", network="enabled",
        )
    )
    store = shell_policy._approval_grant_store()
    assert not store.match(
        shell_policy._SHELL_APPROVAL_PERMISSION,
        owner="alice",
        session_id="chat-a",
        workspace=str(workspace),
        workspace_id="",
        resource=binding,
    )


# ── file-mutation lane ──


def test_auto_approves_file_mutation(prefs_file, tmp_path):
    from src.agent_tools.filesystem_tools import _require_file_approval

    workspace = tmp_path / "ws"
    workspace.mkdir()
    _set_mode("alice", "auto")

    binding = asyncio.run(
        _require_file_approval(
            "move",
            ctx=_shell_ctx("alice", str(workspace)),
            owner="alice",
            workspace=str(workspace),
            source=str(workspace / "a.txt"),
            destination=str(workspace / "b.txt"),
        )
    )
    assert binding


def test_manual_file_mutation_requires_interactive(prefs_file, tmp_path):
    from src.agent_tools.filesystem_tools import _require_file_approval

    workspace = tmp_path / "ws"
    workspace.mkdir()
    _set_mode("alice", "manual")

    with pytest.raises(PermissionError, match="interactive approval"):
        asyncio.run(
            _require_file_approval(
                "move",
                ctx=_shell_ctx("alice", str(workspace)),
                owner="alice",
                workspace=str(workspace),
                source=str(workspace / "a.txt"),
            )
        )


# ── question channels ──


def test_auto_suppresses_native_ask_user(prefs_file):
    from src.agent_tools.interaction_tools import AskUserTool

    _set_mode("alice", "auto")
    content = '{"question": "Which one?", "options": [{"label": "A"}, {"label": "B"}]}'

    _desc, result = asyncio.run(AskUserTool().execute(content, {"owner": "alice"}))
    assert "ask_user" not in result
    assert "Auto mode" in result["output"]

    _set_mode("alice", "yolo")  # yolo still allows questions
    _desc, result = asyncio.run(AskUserTool().execute(content, {"owner": "alice"}))
    assert "ask_user" in result


def test_auto_suppresses_acp_questions(prefs_file):
    from src.openclank.acp_bridge import QuestionHandler

    _set_mode("alice", "auto")
    handler = QuestionHandler()
    handler.set_context_resolver(
        lambda _sid: {"owner": "alice", "odysseus_session_id": "chat-a"}
    )
    out = asyncio.run(
        handler.handle(
            {
                "sessionId": "mimo_sess_1",
                "requestId": "req-1",
                "questions": [{"question": "Which one?"}],
            }
        )
    )
    assert out == {"rejected": True}


def test_manual_acp_permission_still_waits_for_ui(prefs_file):
    from src.openclank.acp_bridge import PermissionHandler

    _set_mode("alice", "manual")
    handler = PermissionHandler()
    handler.set_context_resolver(
        lambda _sid: {"owner": "alice", "odysseus_session_id": "chat-a", "is_admin": True}
    )
    # No UI callback registered: manual mode must reject (not auto-approve).
    out = asyncio.run(
        handler.handle(
            {
                "sessionId": "mimo_sess_1",
                "toolCall": {"toolCallId": "tc1", "title": "bash", "rawInput": {}},
                "options": [{"optionId": "once", "kind": "allow_once"}],
            }
        )
    )
    assert out == {"outcome": {"outcome": "selected", "optionId": "reject"}}


def test_yolo_acp_permission_auto_approves_once(prefs_file):
    from src.openclank.acp_bridge import PermissionHandler

    _set_mode("alice", "yolo")
    handler = PermissionHandler()
    handler.set_context_resolver(
        lambda _sid: {"owner": "alice", "odysseus_session_id": "chat-a", "is_admin": True}
    )
    out = asyncio.run(
        handler.handle(
            {
                "sessionId": "mimo_sess_1",
                "toolCall": {"toolCallId": "tc1", "title": "bash", "rawInput": {}},
                "options": [{"optionId": "once", "kind": "allow_once"}],
            }
        )
    )
    assert out == {"outcome": {"outcome": "selected", "optionId": "once"}}


def test_yolo_never_overrides_non_admin_reject(prefs_file):
    from src.openclank.acp_bridge import PermissionHandler

    _set_mode("bob", "yolo")
    handler = PermissionHandler()
    handler.set_context_resolver(
        lambda _sid: {"owner": "bob", "odysseus_session_id": "chat-b", "is_admin": False}
    )
    out = asyncio.run(
        handler.handle(
            {
                "sessionId": "mimo_sess_2",
                "toolCall": {"toolCallId": "tc1", "title": "bash", "rawInput": {}},
                "options": [{"optionId": "once", "kind": "allow_once"}],
            }
        )
    )
    assert out == {"outcome": {"outcome": "selected", "optionId": "reject"}}
