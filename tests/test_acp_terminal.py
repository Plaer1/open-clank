import sys

import pytest

from src.openclank.acp_terminal import ACPTerminalManager


def _manager(tmp_path, *, incognito=False, admin=True):
    contexts = {
        "alice": {
            "owner": "alice",
            "workspace": str(tmp_path),
            "incognito": incognito,
            "is_admin": admin,
        },
        "bob": {
            "owner": "bob",
            "workspace": str(tmp_path),
            "incognito": False,
            "is_admin": True,
        },
    }
    return ACPTerminalManager(lambda session_id: contexts.get(session_id, {}))


async def test_terminal_lifecycle_output_redaction_and_owner_scope(monkeypatch, tmp_path):
    monkeypatch.setattr("src.tool_execution.vet_workspace", lambda path: path)
    manager = _manager(tmp_path)
    created = await manager.create({
        "sessionId": "alice",
        "command": sys.executable,
        "args": ["-c", "print('TOKEN=abc123'); print('done')"],
        "cwd": str(tmp_path),
        "outputByteLimit": 4096,
    })
    params = {"sessionId": "alice", "terminalId": created["terminalId"]}
    status = await manager.wait_for_exit(params)
    output = await manager.output(params)

    assert status == {"exitCode": 0, "signal": None}
    assert "TOKEN=[redacted]" in output["output"]
    assert "abc123" not in output["output"]
    with pytest.raises(KeyError):
        await manager.output({"sessionId": "bob", "terminalId": created["terminalId"]})
    await manager.release(params)


async def test_terminal_truncates_and_cleans_live_process(monkeypatch, tmp_path):
    monkeypatch.setattr("src.tool_execution.vet_workspace", lambda path: path)
    manager = _manager(tmp_path)
    created = await manager.create({
        "sessionId": "alice",
        "command": sys.executable,
        "args": ["-c", "print('x' * 5000)"],
        "cwd": str(tmp_path),
        "outputByteLimit": 1024,
    })
    params = {"sessionId": "alice", "terminalId": created["terminalId"]}
    await manager.wait_for_exit(params)
    output = await manager.output(params)
    assert output["truncated"] is True
    assert len(output["output"].encode()) <= 1024
    await manager.release(params)

    live = await manager.create({
        "sessionId": "alice",
        "command": sys.executable,
        "args": ["-c", "import time; time.sleep(60)"],
        "cwd": str(tmp_path),
    })
    await manager.cleanup_session("alice")
    with pytest.raises(KeyError):
        await manager.output({"sessionId": "alice", "terminalId": live["terminalId"]})


async def test_terminal_rejects_incognito_non_admin_and_cwd_escape(monkeypatch, tmp_path):
    monkeypatch.setattr("src.tool_execution.vet_workspace", lambda path: path)
    base = {
        "sessionId": "alice",
        "command": sys.executable,
        "args": ["-c", "print('no')"],
        "cwd": str(tmp_path),
    }
    with pytest.raises(PermissionError):
        await _manager(tmp_path, incognito=True).create(base)
    with pytest.raises(PermissionError):
        await _manager(tmp_path, admin=False).create(base)
    with pytest.raises(PermissionError):
        await _manager(tmp_path).create({**base, "cwd": "/"})
