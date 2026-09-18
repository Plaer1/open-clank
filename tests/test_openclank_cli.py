from __future__ import annotations

import json
import sys
from types import SimpleNamespace

import pytest

from src.openclank import cli
from src.openclank import server_manager
from src.openclank.client_profiles import ClientCredentialVault, ClientProfile, ProfileStore
from src.openclank.server_manager import LocalServerManager, ServerManagerError
from src.openclank.tui_client import TuiClientError


def test_cli_defaults_to_real_tui(monkeypatch, tmp_path):
    store = ProfileStore(tmp_path / "profiles.json")
    monkeypatch.setattr(cli, "ProfileStore", lambda: store)
    monkeypatch.setattr(cli, "ClientCredentialVault", lambda: object())
    seen = {}
    monkeypatch.setattr(cli, "_run_tui", lambda args, profile_store, vault: seen.setdefault("ran", 7))
    assert cli.main([]) == 7
    assert seen["ran"] == 7


def test_profile_commands_never_write_credentials(monkeypatch, tmp_path):
    store = ProfileStore(tmp_path / "profiles.json")
    monkeypatch.setattr(cli, "ProfileStore", lambda: store)
    monkeypatch.setattr(cli, "ClientCredentialVault", ClientCredentialVault)
    assert cli.main(["profile", "add", "remote", "https://clank.example.test", "--use"]) == 0
    text = (tmp_path / "profiles.json").read_text()
    assert "clank.example.test" in text
    assert "oct_" not in text


def test_private_automation_is_under_openclank_without_brand_in_help():
    help_text = cli._parser().format_help()
    assert "openclank" in help_text
    assert all(word not in help_text for word in ("MiMo", "Xiaomi", "OpenCode", "Odysseus"))


def test_server_manager_rejects_remote_lifecycle(tmp_path):
    manager = LocalServerManager(repo_root=tmp_path, state_dir=tmp_path / "state")
    with pytest.raises(Exception, match="loopback"):
        manager.status(ClientProfile.create("remote", "https://clank.example.test"))


def test_server_stop_refuses_unverified_pid(tmp_path):
    manager = LocalServerManager(repo_root=tmp_path, state_dir=tmp_path / "state")
    manager.state_dir.mkdir()
    manager.pid_path.write_text(json.dumps({"pid": 99999999}))
    with pytest.raises(ServerManagerError, match="no verified"):
        manager.stop(ClientProfile.create("local", "http://127.0.0.1:7777"))


def test_engine_doctor_maps_to_verifier(monkeypatch):
    seen = {}
    monkeypatch.setattr(cli.subprocess, "call", lambda argv, cwd=None: seen.setdefault("argv", argv) and 0)
    assert cli._run_engine(SimpleNamespace(engine_args=["doctor", "--json"])) == 0
    assert seen["argv"][-2:] == ["verify", "--json"]


def test_local_auto_start_requires_explicit_connection_refusal(monkeypatch):
    profile = ClientProfile.create(
        "local", "http://127.0.0.1:7777", auto_start=True
    )
    started = []

    class FailedClient:
        def __init__(self, *_args, **_kwargs):
            self.token = None

        def info(self):
            raise TuiClientError("timed out", reason="connection_failed")

        def close(self):
            pass

    monkeypatch.setattr(cli, "OpenClankTuiClient", FailedClient)
    monkeypatch.setattr(
        cli.LocalServerManager,
        "start",
        lambda *_args, **_kwargs: started.append(True),
    )
    with pytest.raises(TuiClientError, match="timed out"):
        cli._connected_client(profile, SimpleNamespace(get=lambda _name: None), auto_start=True)
    assert started == []


def test_packaged_server_bootstrap_failure_precedes_app_import(monkeypatch):
    import scripts.openclank_bootstrap as bootstrap

    imported_app = []
    real_import = __import__

    def guarded_import(name, *args, **kwargs):
        if name == "app":
            imported_app.append(name)
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(
        bootstrap,
        "prepare_service",
        lambda: (_ for _ in ()).throw(RuntimeError("portable-integrity-fence")),
    )
    monkeypatch.setattr("builtins.__import__", guarded_import)

    assert cli._run_managed_server(["--host", "127.0.0.1", "--port", "7788"]) == 1
    assert imported_app == []


def test_packaged_private_server_rejects_non_loopback_before_bootstrap(monkeypatch):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    assert cli._run_managed_server(["--host", "0.0.0.0", "--port", "7788"]) == 2


def test_server_readiness_probe_rejects_an_unrelated_http_200(monkeypatch):
    response = SimpleNamespace(
        status_code=200,
        json=lambda: {"ok": True},
        text="ok",
    )
    monkeypatch.setattr(server_manager.httpx, "get", lambda *_args, **_kwargs: response)
    assert LocalServerManager._probe("http://127.0.0.1:7777", "/api/ready") == (
        False,
        "unexpected readiness response",
    )


def test_server_status_reports_authenticated_app_as_running(monkeypatch, tmp_path):
    manager = LocalServerManager(repo_root=tmp_path, state_dir=tmp_path / "state")
    profile = ClientProfile.create("local", "http://127.0.0.1:7777")

    def fake_get(url, **_kwargs):
        if url.endswith("/api/ready"):
            return SimpleNamespace(
                status_code=401,
                json=lambda: {"error": "Not authenticated"},
                text="Not authenticated",
            )
        assert url.endswith("/api/auth/status")
        return SimpleNamespace(
            status_code=200,
            json=lambda: {"configured": True, "authenticated": False},
            text="",
        )

    monkeypatch.setattr(server_manager.httpx, "get", fake_get)
    status = manager.status(profile)

    assert status.running is True
    assert status.ready is False
    assert status.pid is None
    assert "reachable" in status.detail
    assert "without authentication" in status.detail


def test_failed_start_terminates_child_and_cleans_runtime_state(monkeypatch, tmp_path):
    manager = LocalServerManager(repo_root=tmp_path, state_dir=tmp_path / "state")
    profile = ClientProfile.create("local", "http://127.0.0.1:7777")

    class Process:
        pid = 4242
        returncode = None
        terminated = False

        def poll(self):
            return self.returncode

        def terminate(self):
            self.terminated = True
            self.returncode = 0

        def wait(self, timeout=None):
            return self.returncode

        def kill(self):
            self.returncode = -9

    process = Process()
    times = iter((0.0, 0.0, 0.0, 2.0))
    monkeypatch.setattr(server_manager.time, "monotonic", lambda: next(times))
    monkeypatch.setattr(manager, "_probe", lambda *_args, **_kwargs: (False, "connection refused"))
    monkeypatch.setattr(manager, "_presence_probe", lambda *_args, **_kwargs: (False, "connection refused"))
    monkeypatch.setattr(server_manager.subprocess, "Popen", lambda *_args, **_kwargs: process)

    with pytest.raises(ServerManagerError, match="did not become ready"):
        manager.start(profile, wait_seconds=1)
    assert process.terminated
    assert not manager.pid_path.exists()
    assert not manager.start_lock_path.exists()
