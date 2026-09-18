from types import SimpleNamespace
import asyncio
import os
from pathlib import Path
import subprocess
import sys
import tempfile

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import src.openclank.history_client as history_client_module
import src.openclank.file_policy as file_policy_module
import src.openclank.filesystem_registry as filesystem_registry_module
import routes.history.history_routes as history_routes


class _Auth:
    is_configured = True

    def __init__(self):
        self.accounts = {"alice": "acct-alice", "bob": "acct-bob"}

    def is_admin(self, user):
        return user == "admin"

    def account_id(self, user):
        return self.accounts.get(user, user)


class _Registry:
    def __init__(self, *_args, **_kwargs):
        pass

    def list(self, owner):
        return []

    def visibility_for_subject(self, owner):
        return []


class _PolicyClient:
    policy = {
        "revision": 4,
        "global": {"revision": 4, "total_bytes": 1024, "enabled": True},
        "scopes": [
            {"scope_id": "alice-work", "kind": "Workspace", "owner_account_id": "acct-alice", "workspace_id": "default", "limit_bytes": 100, "revision": 4, "enabled": True},
            {"scope_id": "bob-work", "kind": "Workspace", "owner_account_id": "acct-bob", "workspace_id": "default", "limit_bytes": 200, "revision": 4, "enabled": True},
        ],
    }

    def __init__(self, *_args, **_kwargs):
        pass

    def get_policy(self):
        return {"Policy": self.policy}

    def get_usage(self):
        return {"Usage": {"physical_allocated_bytes": 12, "logical_retained_bytes": 12, "measurement_quality": "Allocated"}}

    def get_status(self):
        return {"Status": {"state": "ready", "history_paused": False}}

    def set_policy(self, policy, *, expected_revision):
        if expected_revision != self.policy["revision"]:
            raise history_client_module.HistoryClientError("PolicyRevisionMismatch")
        self.policy = {**policy, "revision": expected_revision + 1}
        return {"Policy": self.policy}


class _CanonicalWorkspace:
    def __init__(self, workspace_id, owner_subject_id, archived=False):
        self.id = workspace_id
        self.owner_subject_id = owner_subject_id
        self.archived = archived


class _CanonicalFilePolicyRepository:
    workspaces = [
        _CanonicalWorkspace("alice-canonical", "acct-alice"),
        _CanonicalWorkspace("alice-archived", "acct-alice", archived=True),
        _CanonicalWorkspace("bob-canonical", "acct-bob"),
        _CanonicalWorkspace("admin-canonical", "acct-admin"),
    ]

    def __init__(self, *_args, **_kwargs):
        pass

    def list_workspaces(self, *, owner_subject_id=None, include_archived=False):
        return [
            workspace for workspace in self.workspaces
            if (owner_subject_id is None or workspace.owner_subject_id == owner_subject_id)
            and (include_archived or not workspace.archived)
        ]


@pytest.fixture
def history_app(monkeypatch):
    monkeypatch.setenv("OPENCLANK_HISTORY_SOCKET", "/tmp/test-history.sock")
    monkeypatch.setattr(filesystem_registry_module, "FilesystemRootRegistry", _Registry)
    monkeypatch.setattr(history_client_module, "HistoryClient", _PolicyClient)
    app = FastAPI()
    app.state.auth_manager = _Auth()

    @app.middleware("http")
    async def identity(request, call_next):
        request.state.current_user = request.headers.get("X-User", "alice")
        return await call_next(request)

    app.include_router(history_routes.setup_history_settings_routes())
    return app


def test_authenticated_settings_filters_other_account_and_keeps_workspace_distinct(history_app):
    client = TestClient(history_app)
    response = client.get("/api/history/settings", headers={"X-User": "alice"})
    assert response.status_code == 200
    body = response.json()
    assert [scope["scope_id"] for scope in body["policy"]["scopes"]] == ["alice-work"]
    assert body["policy"]["global"]["inherited"] is True
    assert "default" in body["workspace_options"]


def test_workspace_options_use_active_canonical_file_policy_workspaces(history_app, monkeypatch):
    monkeypatch.setattr(file_policy_module, "FilePolicyRepository", _CanonicalFilePolicyRepository)
    client = TestClient(history_app)

    alice = client.get("/api/history/settings", headers={"X-User": "alice"})
    assert alice.status_code == 200
    assert alice.json()["workspace_options"] == ["alice-canonical", "default"]

    admin = client.get("/api/history/settings", headers={"X-User": "admin"})
    assert admin.status_code == 200
    assert admin.json()["workspace_options"] == [
        "admin-canonical", "alice-canonical", "bob-canonical", "default"
    ]

    denied = client.put(
        "/api/history/settings",
        headers={"X-User": "alice"},
        json={"expected_revision": 4, "policy": {"scopes": [{"kind": "workspace", "workspace_id": "default", "owner_account_id": "acct-bob", "limit_bytes": 1}]}},
    )
    assert denied.status_code == 403


def test_settings_revision_conflict_is_reported_without_remount(history_app):
    client = TestClient(history_app)
    response = client.put(
        "/api/history/settings",
        headers={"X-User": "alice"},
        json={"expected_revision": 3, "policy": {"scopes": []}},
    )
    assert response.status_code == 409


def test_account_rename_keeps_scope_partition_and_workspace_name_is_not_identity(history_app):
    auth = history_app.state.auth_manager
    auth.accounts["alice-renamed"] = "acct-alice"
    client = TestClient(history_app)
    response = client.get("/api/history/settings", headers={"X-User": "alice-renamed"})
    assert response.status_code == 200
    assert [scope["scope_id"] for scope in response.json()["policy"]["scopes"]] == ["alice-work"]
    response = client.get("/api/history/settings", headers={"X-User": "bob"})
    assert [scope["scope_id"] for scope in response.json()["policy"]["scopes"]] == ["bob-work"]


def test_mounted_app_registers_literal_settings_before_session_parameter_route(monkeypatch):
    """The mounted app must dispatch /settings before /{session_id}.

    FastAPI's included routers retain their registration order.  The legacy
    session history route accepts ``settings`` as a session id, so testing the
    settings router in isolation misses the production collision.
    """
    script = """
import app
positions = {}
for index, included in enumerate(app.app.routes):
    original = getattr(included, "original_router", None)
    if original is None:
        continue
    paths = {route.path for route in original.routes if getattr(route, "path", None)}
    if "/api/history/settings" in paths:
        positions["settings"] = index
    if "/api/history/{sid}" in paths:
        positions["session"] = index
assert positions["settings"] < positions["session"], positions
print("mounted-order-ok")
"""
    environment = os.environ.copy()
    environment.pop("DEBUG", None)
    environment.pop("OPENCLANK_DEBUG", None)
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=Path(__file__).parents[1],
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )
    assert "mounted-order-ok" in result.stdout


def test_mounted_settings_without_service_reports_unavailable(monkeypatch):
    """A no-auth/dev app reports the history outage from the settings handler."""
    script = """
from fastapi.testclient import TestClient
import app
response = TestClient(app.app).get("/api/history/settings")
assert response.status_code == 503, response.text
assert response.json()["detail"] == "history service is unavailable"
print("mounted-unavailable-ok")
"""
    environment = os.environ.copy()
    environment.pop("DEBUG", None)
    environment.pop("OPENCLANK_DEBUG", None)
    environment["AUTH_ENABLED"] = "false"
    environment.pop("OPENCLANK_HISTORY_SOCKET", None)
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=Path(__file__).parents[1],
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )
    assert "mounted-unavailable-ok" in result.stdout


def test_settings_route_uses_real_worker_and_survives_restart(monkeypatch):
    """Exercise the mounted settings handlers against the Rust worker IPC."""
    configured_binary = os.environ.get("OPENCLANK_HISTORY_TEST_BIN", "").strip()
    if not configured_binary or not Path(configured_binary).is_file():
        return

    from fastapi import Request

    from src.openclank.history_client import HistoryServiceSupervisor, ScopedHistoryCredential

    credentials = [
        ScopedHistoryCredential(actor_id="*", account_id="acct-alice", capabilities=frozenset({"read", "capture", "restore", "settings-read", "settings-write"})),
        ScopedHistoryCredential(actor_id="*", account_id="acct-bob", capabilities=frozenset({"read", "capture", "restore", "settings-read", "settings-write"})),
        ScopedHistoryCredential(actor_id="*", account_id="acct-admin", capabilities=frozenset({"admin", "read", "capture", "restore", "settings-read", "settings-write"})),
    ]
    with tempfile.TemporaryDirectory(prefix="hroute-", dir="/tmp") as disposable:
        root = Path(disposable)
        supervisor = HistoryServiceSupervisor(
            configured_binary,
            socket_path=root / "history.sock",
            catalog_path=root / "history.redb",
            lore_root=root / "lore",
            credential_file=root / "credentials.json",
            credentials=credentials,
        )

        async def start():
            await supervisor.start()

        async def stop():
            await supervisor.stop()

        asyncio.run(start())
        monkeypatch.setenv("OPENCLANK_HISTORY_SOCKET", str(root / "history.sock"))
        app = FastAPI()
        app.state.auth_manager = _Auth()
        app.state.auth_manager.accounts["admin"] = "acct-admin"

        @app.middleware("http")
        async def identity(request: Request, call_next):
            request.state.current_user = request.headers.get("X-User", "alice")
            return await call_next(request)

        app.include_router(history_routes.setup_history_settings_routes())
        client = TestClient(app)
        try:
            alice = client.get("/api/history/settings", headers={"X-User": "alice"})
            assert alice.status_code == 200
            assert alice.json()["policy"]["global"]["total_bytes"] > 0

            admin = client.put(
                "/api/history/settings",
                headers={"X-User": "admin"},
                json={"expected_revision": 1, "policy": {"global": {"total_bytes": 4096}}},
            )
            assert admin.status_code == 200
            revision = admin.json()["policy"]["revision"]

            own_scope = client.put(
                "/api/history/settings",
                headers={"X-User": "alice"},
                json={
                    "expected_revision": revision,
                    "policy": {"scopes": [{"kind": "workspace", "workspace_id": "default", "limit_bytes": 1024}]},
                },
            )
            assert own_scope.status_code == 200, own_scope.text
            assert own_scope.json()["policy"]["scopes"][0]["owner_account_id"] == "acct-alice"

            bob = client.get("/api/history/settings", headers={"X-User": "bob"})
            assert bob.status_code == 200
            assert bob.json()["policy"]["scopes"] == []
            restore = client.post(
                "/api/history/restore",
                headers={"X-User": "alice"},
                json={
                    "request": {
                        "restore_id": "route-probe",
                        "account_id": "acct-alice",
                        "source_action_id": "missing-action",
                        "source_version_id": "missing-version",
                        "destination": {"account_id": "acct-alice", "workspace_id": "host", "provider": "host", "resource_id": "unknown"},
                        "expected_destination_fingerprint": None,
                        "require_current_capture": False,
                    },
                    "source": {"version_id": "missing-version", "content": "Empty", "fingerprint": "missing"},
                    "destination_path": "/tmp/route-probe",
                },
            )
            assert restore.status_code == 422
            assert "accountmismatch" in restore.json()["detail"].lower()
            denied = client.put(
                "/api/history/settings",
                headers={"X-User": "bob"},
                json={"expected_revision": own_scope.json()["policy"]["revision"], "policy": {"scopes": [{"owner_account_id": "acct-alice", "kind": "workspace", "workspace_id": "default", "limit_bytes": 1}]}},
            )
            assert denied.status_code == 403

            stale = client.put(
                "/api/history/settings",
                headers={"X-User": "alice"},
                json={"expected_revision": 1, "policy": {"scopes": []}},
            )
            assert stale.status_code == 409
        finally:
            asyncio.run(stop())

        asyncio.run(start())
        try:
            reopened = client.get("/api/history/settings", headers={"X-User": "alice"})
            assert reopened.status_code == 200
            assert reopened.json()["policy"]["global"]["total_bytes"] == 4096
            assert reopened.json()["policy"]["scopes"][0]["owner_account_id"] == "acct-alice"
        finally:
            asyncio.run(stop())
