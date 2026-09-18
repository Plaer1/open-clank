"""Regression tests for read_file / write_file path confinement.

Covers:
  - /etc/shadow, /etc/passwd, /var/log — blocked (outside roots)
  - ~/.ssh/authorized_keys — blocked (sensitive subpath deny list)
  - Symlink that resolves into .ssh — blocked
  - Relative traversal (~/../../etc/passwd) — blocked
  - Shell rc files (.bashrc, .zshrc, .profile) — blocked
  - SSH key filenames (id_rsa, id_ed25519) — blocked regardless of dir
  - Open Clank control data — blocked, including from a broader workspace
  - Legitimate paths under /tmp and ordinary workspaces — allowed
  - Extra roots via tool_path_extra_roots setting — opt-in
  - Even with $HOME as extra root, sensitive subpaths stay blocked
"""

import json
import os
import pathlib
import sys
from types import SimpleNamespace
from unittest.mock import patch

import pytest


def _make_block(tool_type, content):
    return SimpleNamespace(tool_type=tool_type, content=content)


# ── Unit tests on _is_sensitive_path ──────────────────────────────────

def test_sensitive_ssh_dir():
    from src.tool_execution import _is_sensitive_path
    assert _is_sensitive_path("/home/user/.ssh/authorized_keys")
    assert _is_sensitive_path(os.path.expanduser("~") + "/.ssh/config")


def test_sensitive_gnupg_dir():
    from src.tool_execution import _is_sensitive_path
    assert _is_sensitive_path("/home/user/.gnupg/pubring.kbx")


def test_sensitive_shell_rc():
    from src.tool_execution import _is_sensitive_path
    assert _is_sensitive_path("/home/user/.bashrc")
    assert _is_sensitive_path("/home/user/.zshrc")
    assert _is_sensitive_path("/home/user/.profile")


def test_sensitive_key_filenames():
    from src.tool_execution import _is_sensitive_path
    assert _is_sensitive_path("/tmp/id_rsa")
    assert _is_sensitive_path("/tmp/id_ed25519")
    assert _is_sensitive_path("/tmp/authorized_keys")


def test_non_sensitive_path():
    from src.tool_execution import _is_sensitive_path
    assert not _is_sensitive_path("/tmp/notes.txt")
    assert not _is_sensitive_path("/home/user/projects/file.py")


def test_sensitive_case_insensitive():
    """On case-insensitive filesystems (Windows, default macOS) a case-variant
    name resolves to the same protected file, so the deny-list must match
    regardless of case. Built with os.path.join so the separator is right on
    both POSIX and Windows.
    """
    from src.tool_execution import _is_sensitive_path
    # sensitive directory, varied case
    assert _is_sensitive_path(os.path.join("home", "u", ".SSH", "authorized_keys"))
    assert _is_sensitive_path(os.path.join("home", "u", ".Gnupg", "pubring.kbx"))
    # sensitive filename, varied case
    assert _is_sensitive_path(os.path.join("ws", "AUTHORIZED_KEYS"))
    assert _is_sensitive_path(os.path.join("ws", "Id_Rsa"))
    assert _is_sensitive_path(os.path.join("ws", ".ENV"))
    assert _is_sensitive_path(os.path.join("ws", ".Env"))
    # both dir and file varied
    assert _is_sensitive_path(os.path.join("home", "u", ".SSH", "AUTHORIZED_KEYS"))
    # an ordinary file with none of the sensitive names is still allowed
    assert not _is_sensitive_path(os.path.join("ws", "Readme.md"))


# ── Unit tests on _resolve_tool_path ─────────────────────────────────

def test_blocks_etc_shadow():
    """The motivating example: /etc/shadow must be rejected."""
    from src.tool_execution import _resolve_tool_path
    with pytest.raises(ValueError, match="outside the allowed roots"):
        _resolve_tool_path("/etc/shadow")


def test_blocks_etc_passwd():
    from src.tool_execution import _resolve_tool_path
    with pytest.raises(ValueError, match="outside the allowed roots"):
        _resolve_tool_path("/etc/passwd")


def test_blocks_var_log():
    from src.tool_execution import _resolve_tool_path
    with pytest.raises(ValueError, match="outside the allowed roots"):
        _resolve_tool_path("/var/log/system.log")


def test_blocks_ssh_authorized_keys():
    """~/.ssh/authorized_keys — blocked by sensitive-subpath deny even
    though $HOME is NOT a default root (the deny list fires first)."""
    from src.tool_execution import _resolve_tool_path
    with pytest.raises(ValueError, match="sensitive directory"):
        _resolve_tool_path("~/.ssh/authorized_keys")


def test_blocks_ssh_dir_absolute():
    from src.tool_execution import _resolve_tool_path
    home = os.path.expanduser("~")
    with pytest.raises(ValueError, match="sensitive directory"):
        _resolve_tool_path(os.path.join(home, ".ssh", "config"))


def test_blocks_symlink_into_ssh(tmp_path):
    """A symlink under /tmp that points into ~/.ssh must be caught
    because realpath resolves the link before the deny-list check."""
    from src.tool_execution import _resolve_tool_path
    ssh_dir = os.path.join(os.path.expanduser("~"), ".ssh")
    os.makedirs(ssh_dir, exist_ok=True)
    link = tmp_path / "ssh_link"
    try:
        link.symlink_to(ssh_dir)
    except OSError:
        pytest.skip("cannot create symlink")
    with pytest.raises(ValueError, match="sensitive directory"):
        _resolve_tool_path(str(link))


def test_blocks_traversal_outside_roots():
    """~/../../etc/passwd — after tilde expansion and .. resolution the
    path lands outside every allowed root."""
    from src.tool_execution import _resolve_tool_path
    with pytest.raises(ValueError):
        _resolve_tool_path("~/../../etc/passwd")


def test_blocks_bashrc():
    from src.tool_execution import _resolve_tool_path
    with pytest.raises(ValueError, match="sensitive directory"):
        _resolve_tool_path("~/.bashrc")


def test_blocks_zshrc():
    from src.tool_execution import _resolve_tool_path
    with pytest.raises(ValueError, match="sensitive directory"):
        _resolve_tool_path("~/.zshrc")


def test_blocks_env_file():
    from src.tool_execution import _resolve_tool_path
    with pytest.raises(ValueError, match="sensitive directory"):
        _resolve_tool_path("~/.env")


def test_blocks_netrc():
    from src.tool_execution import _resolve_tool_path
    with pytest.raises(ValueError, match="sensitive directory"):
        _resolve_tool_path("~/.netrc")


def test_blocks_project_control_data(tmp_path):
    """Generic model file tools cannot read or mutate application state."""
    from src.tool_execution import _resolve_tool_path
    from src.constants import DATA_DIR
    with pytest.raises(ValueError, match="Open Clank control data"):
        _resolve_tool_path(os.path.join(DATA_DIR, "app.db"))


def test_workspace_blocks_control_data_and_symlink_alias(monkeypatch, tmp_path):
    from src.tool_execution import _resolve_tool_path_in_workspace

    workspace = tmp_path / "repo"
    control = workspace / "data"
    control.mkdir(parents=True)
    target = control / "app.db"
    target.write_text("secret")
    normal = workspace / "notes.txt"
    normal.write_text("ok")
    alias = workspace / "control-alias"
    alias.symlink_to(control, target_is_directory=True)

    monkeypatch.setattr("src.constants.DATA_DIR", str(control))
    monkeypatch.setattr("src.constants.AUTH_FILE", str(control / "auth.json"))
    monkeypatch.setattr("src.constants.FM_DB_PATH", str(control / "frankenmemory.db"))

    assert _resolve_tool_path_in_workspace(str(workspace), "notes.txt") == str(normal)
    with pytest.raises(ValueError, match="Open Clank control data"):
        _resolve_tool_path_in_workspace(str(workspace), "data/app.db")
    with pytest.raises(ValueError, match="Open Clank control data"):
        _resolve_tool_path_in_workspace(str(workspace), "control-alias/app.db")


def test_control_data_cannot_be_bound_as_workspace(monkeypatch, tmp_path):
    from src.tool_execution import vet_workspace

    control = tmp_path / "data"
    control.mkdir()
    monkeypatch.setattr("src.constants.DATA_DIR", str(control))
    monkeypatch.setattr("src.constants.AUTH_FILE", str(control / "auth.json"))
    monkeypatch.setattr("src.constants.FM_DB_PATH", str(control / "frankenmemory.db"))

    assert vet_workspace(str(control)) is None


def test_runtime_xdg_config_is_control_data(monkeypatch, tmp_path):
    from src.tool_execution import _is_control_data_path

    monkeypatch.setenv("MIMOCODE_HOME", str(tmp_path / "mimo-home"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))

    assert _is_control_data_path(str(tmp_path / "mimo-home" / "auth.json"))
    assert _is_control_data_path(str(tmp_path / "config" / "mimocode" / "config.json"))


def test_external_frankenmemory_sidecars_are_control_data(monkeypatch, tmp_path):
    from src.tool_execution import _is_control_data_path

    monkeypatch.setattr("src.constants.DATA_DIR", str(tmp_path / "agent-data"))
    monkeypatch.setattr("src.constants.AUTH_FILE", str(tmp_path / "auth-data" / "auth.json"))
    db_path = tmp_path / "memory-data" / "frankenmemory.db"
    monkeypatch.setattr("src.constants.FM_DB_PATH", str(db_path))

    assert _is_control_data_path(f"{db_path}-wal")
    assert _is_control_data_path(f"{db_path}-shm")


@pytest.mark.asyncio
async def test_publish_admin_home_fallback_still_blocks_control_data(monkeypatch, tmp_path):
    from src.agent_tools.filesystem_tools import PublishFileTool

    control = tmp_path / "data"
    control.mkdir()
    target = control / "app.db"
    target.write_text("secret")
    monkeypatch.setattr("src.constants.DATA_DIR", str(control))
    monkeypatch.setattr("src.constants.AUTH_FILE", str(control / "auth.json"))
    monkeypatch.setattr("src.constants.FM_DB_PATH", str(control / "frankenmemory.db"))
    monkeypatch.setattr(pathlib.Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(
        "src.tool_security.owner_is_admin_or_single_user",
        lambda owner: True,
    )

    result = await PublishFileTool().execute(
        json.dumps({"path": str(target)}),
        {"owner": "admin"},
    )

    assert result["exit_code"] == 1
    assert "Open Clank control data" in result["error"]


@pytest.mark.asyncio
async def test_code_nav_prunes_control_data(monkeypatch, tmp_path):
    from src.agent_tools.filesystem_tools import GlobTool, GrepTool, LsTool
    from src.tool_execution import _active_workspace

    workspace = tmp_path / "repo"
    control = workspace / "data"
    control.mkdir(parents=True)
    (control / "secret.txt").write_text("CONTROL_SECRET")
    (workspace / "safe.txt").write_text("SAFE_CONTENT")

    monkeypatch.setattr("src.constants.DATA_DIR", str(control))
    monkeypatch.setattr("src.constants.AUTH_FILE", str(control / "auth.json"))
    monkeypatch.setattr("src.constants.FM_DB_PATH", str(control / "frankenmemory.db"))

    token = _active_workspace.set(str(workspace))
    try:
        listed = await LsTool().execute("", {})
        globbed = await GlobTool().execute(
            '{"pattern": "secret.txt", "path": ""}',
            {},
        )
        grepped = await GrepTool().execute(
            '{"pattern": "CONTROL_SECRET", "path": ""}',
            {},
        )
    finally:
        _active_workspace.reset(token)

    assert listed["exit_code"] == 0
    assert "safe.txt" in listed["output"]
    assert "data/" not in listed["output"]
    assert globbed["exit_code"] == 0
    assert globbed["paths"] == []
    assert grepped["exit_code"] == 0
    assert grepped["matches"] == []


def test_allows_tmp(tmp_path):
    """Paths under /tmp (or its realpath) must resolve cleanly."""
    from src.tool_execution import _resolve_tool_path
    f = tmp_path / "confinement-test.txt"
    f.write_text("ok")
    resolved = _resolve_tool_path(str(f))
    assert resolved == os.path.realpath(str(f))


def test_rejects_empty_path():
    from src.tool_execution import _resolve_tool_path
    with pytest.raises(ValueError, match="path is required"):
        _resolve_tool_path("")
    with pytest.raises(ValueError, match="path is required"):
        _resolve_tool_path("   ")


def test_extra_roots_opt_in(tmp_path):
    """When tool_path_extra_roots includes a directory, paths under it
    are allowed (but sensitive subpaths are still blocked)."""
    from src.tool_execution import _resolve_tool_path
    extra_dir = tmp_path / "extra_root"
    extra_dir.mkdir()
    target = extra_dir / "file.txt"
    target.write_text("ok")

    with patch("src.settings.get_setting", return_value=[str(extra_dir)]):
        resolved = _resolve_tool_path(str(target))
        assert resolved == os.path.realpath(str(target))


def test_extra_root_still_blocks_sensitive(tmp_path):
    """Even when $HOME is in tool_path_extra_roots, ~/.ssh/authorized_keys
    must still be rejected by the sensitive-subpath deny list."""
    from src.tool_execution import _resolve_tool_path
    home = os.path.expanduser("~")
    with patch("src.settings.get_setting", return_value=[home]):
        with pytest.raises(ValueError, match="sensitive directory"):
            _resolve_tool_path("~/.ssh/authorized_keys")


# ── Integration: dispatch-level tests ────────────────────────────────

@pytest.mark.asyncio
async def test_read_file_dispatch_blocks_etc_shadow(monkeypatch):
    """End-to-end: read_file dispatch must reject /etc/shadow."""
    auth_mod = sys.modules.get("core.auth")
    if auth_mod is None:
        import core.auth as _real_auth
        auth_mod = _real_auth

    class _AdminAuth:
        is_configured = True
        def is_admin(self, username):
            return True

    monkeypatch.setattr(auth_mod, "AuthManager", lambda: _AdminAuth())
    monkeypatch.setattr(
        "src.tool_execution.owner_is_admin_or_single_user",
        lambda owner: True,
    )

    from src.tool_execution import execute_tool_block
    desc, result = await execute_tool_block(
        _make_block("read_file", "/etc/shadow"),
        owner="admin-user",
    )
    assert "outside the allowed roots" in (result.get("error") or "")
    assert result.get("exit_code") == 1


@pytest.mark.asyncio
async def test_write_file_dispatch_blocks_authorized_keys(monkeypatch):
    """End-to-end: write_file dispatch must reject ~/.ssh/authorized_keys."""
    auth_mod = sys.modules.get("core.auth")
    if auth_mod is None:
        import core.auth as _real_auth
        auth_mod = _real_auth

    class _AdminAuth:
        is_configured = True
        def is_admin(self, username):
            return True

    monkeypatch.setattr(auth_mod, "AuthManager", lambda: _AdminAuth())
    monkeypatch.setattr(
        "src.tool_execution.owner_is_admin_or_single_user",
        lambda owner: True,
    )

    from src.tool_execution import execute_tool_block
    desc, result = await execute_tool_block(
        _make_block("write_file", "~/.ssh/authorized_keys\nssh-rsa AAAAB3..."),
        owner="admin-user",
    )
    assert "sensitive directory" in (result.get("error") or "")
    assert result.get("exit_code") == 1


@pytest.mark.asyncio
async def test_write_file_dispatch_blocks_cron(monkeypatch):
    """End-to-end: write_file to /etc/cron.d must be rejected."""
    auth_mod = sys.modules.get("core.auth")
    if auth_mod is None:
        import core.auth as _real_auth
        auth_mod = _real_auth

    class _AdminAuth:
        is_configured = True
        def is_admin(self, username):
            return True

    monkeypatch.setattr(auth_mod, "AuthManager", lambda: _AdminAuth())
    monkeypatch.setattr(
        "src.tool_execution.owner_is_admin_or_single_user",
        lambda owner: True,
    )

    from src.tool_execution import execute_tool_block
    desc, result = await execute_tool_block(
        _make_block("write_file", "/etc/cron.d/agent-payload\n* * * * * root /tmp/p\n"),
        owner="admin-user",
    )
    assert "outside the allowed roots" in (result.get("error") or "")
    assert result.get("exit_code") == 1
