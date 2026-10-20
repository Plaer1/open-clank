from types import SimpleNamespace

import pytest
from fastapi import HTTPException

import routes.session_routes as session_routes


@pytest.mark.asyncio
async def test_projection_purge_uses_verified_stored_owner_when_auth_disabled(monkeypatch):
    captured = []
    monkeypatch.setattr(session_routes, "_reject_compact_during_active_run", lambda _sid: None)
    monkeypatch.setattr(session_routes, "_verify_session_owner", lambda _request, _sid: "stored-owner")

    async def purge(_supervisor, session_id, *, owner):
        captured.append((session_id, owner))
        return True

    import src.openclank.transcript_projection as projection
    monkeypatch.setattr(projection, "purge_execution_projection", purge)
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(mimo_supervisor=None)))
    owner = await session_routes._prepare_context_mutation(request, "session-1")
    assert owner == "stored-owner"
    assert captured == [("session-1", "stored-owner")]


@pytest.mark.asyncio
async def test_owner_mismatch_fails_before_projection_purge(monkeypatch):
    purged = False
    monkeypatch.setattr(session_routes, "_reject_compact_during_active_run", lambda _sid: None)

    def reject(_request, _sid):
        raise HTTPException(404, "Session not found")

    monkeypatch.setattr(session_routes, "_verify_session_owner", reject)

    async def purge(*_args, **_kwargs):
        nonlocal purged
        purged = True

    import src.openclank.transcript_projection as projection
    monkeypatch.setattr(projection, "purge_execution_projection", purge)
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(mimo_supervisor=None)))
    with pytest.raises(HTTPException) as error:
        await session_routes._prepare_context_mutation(request, "session-1")
    assert error.value.status_code == 404
    assert purged is False
