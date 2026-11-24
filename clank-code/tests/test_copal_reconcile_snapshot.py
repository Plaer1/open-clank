"""Reconcile reuses exactly the authorized snapshot, not a second vault read."""

import pytest
from types import SimpleNamespace

import routes.copal_routes as routes
from test_copal_routes import canonical_planning_client


@pytest.mark.asyncio
async def test_reconcile_reads_one_scoped_snapshot(tmp_path, monkeypatch):
    projector = routes._project_canonical_workspace
    http, bridge = canonical_planning_client(tmp_path, monkeypatch)
    http.app.state.auth_manager = SimpleNamespace(users={"alice": {}})

    @http.app.middleware("http")
    async def verified_test_session(request, call_next):
        request.state.current_user = "alice"
        request.state.authenticated = True
        return await call_next(request)

    monkeypatch.setattr(routes, "_project_canonical_workspace", projector)
    bridge.calls.clear()
    result = await http.post("/api/copal/calendar/reconcile?workspace=sample", json={})
    assert result.status_code == 200, result.text
    assert result.json()["ok"] is True
    indexes = [call[1] for call in bridge.calls if call[0] == "index"]
    assert len(indexes) == 1
    assert indexes[0]["owner"] == routes.copal_owner_for_user("alice")
    assert indexes[0]["workspace_id"] == "sample"


@pytest.mark.asyncio
async def test_projector_empty_supplied_snapshot_does_not_fetch(monkeypatch):
    async def forbidden(*args, **kwargs):
        raise AssertionError("supplied empty snapshot must not trigger a fetch")

    monkeypatch.setattr(routes, "_indexed_documents", forbidden)
    assert await routes._project_canonical_workspace(None, {}, documents=[]) is None


@pytest.mark.asyncio
async def test_projector_other_callers_still_fetch_scoped_snapshot(monkeypatch):
    calls = []

    async def read(request, scope):
        calls.append((request, scope))
        return []

    monkeypatch.setattr(routes, "_indexed_documents", read)
    request, scope = object(), {"owner":"alice", "workspace_id":"one"}
    assert await routes._project_canonical_workspace(request, scope) is None
    assert calls == [(request, scope)]
