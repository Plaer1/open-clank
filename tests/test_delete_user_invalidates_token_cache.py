"""Deleting a user must invalidate the bearer-token cache.

delete_user removes the user's ApiToken rows from the DB, but the bearer-auth
middleware in app.py serves from an in-memory prefix->token cache that only
rebuilds when flagged dirty (app.state.invalidate_token_cache). If the admin
delete route does not flag it, a deleted user's already-cached token keeps
authenticating until some unrelated token op or a process restart clears the
cache. The DELETE /api/auth/users handler now calls the invalidator on a
successful delete (and only then), so the next bearer request rebuilds the
cache from the DB, where the rows are already gone, and the token is rejected.
"""
import asyncio
import types

import pytest

import routes.prefs_routes as prefs_routes
from routes.auth_routes import setup_auth_routes, DeleteUserRequest


def _handler(router):
    for route in router.routes:
        if getattr(route, "path", "") == "/api/auth/users" and "DELETE" in getattr(route, "methods", set()):
            return route.endpoint
    raise AssertionError("DELETE /api/auth/users handler not found")


def _fake_request(invalidations):
    state = types.SimpleNamespace(invalidate_token_cache=lambda: invalidations.append(True))
    app = types.SimpleNamespace(state=state)
    return types.SimpleNamespace(cookies={"_dummy": "x"}, app=app)


def _auth_manager(delete_result):
    return types.SimpleNamespace(
        get_username_for_token=lambda token: "admin",
        is_admin=lambda user: True,
        delete_user=lambda username, requesting_user: delete_result,
    )


def _auth_manager_raising():
    def _delete_user(_username, _requesting_user):
        raise RuntimeError("auth save failed after token purge")

    return types.SimpleNamespace(
        get_username_for_token=lambda token: "admin",
        is_admin=lambda user: True,
        delete_user=_delete_user,
    )


@pytest.fixture(autouse=True)
def _isolated_prefs(tmp_path, monkeypatch):
    monkeypatch.setattr(prefs_routes, "PREFS_FILE", str(tmp_path / "user_prefs.json"))


def test_successful_delete_invalidates_cache():
    invalidations = []
    router = setup_auth_routes(_auth_manager(delete_result=True))
    handler = _handler(router)
    result = asyncio.run(handler(DeleteUserRequest(username="bob"), _fake_request(invalidations)))
    assert result == {"ok": True}
    assert invalidations == [True], "successful delete must flag the token cache stale"


def test_delete_drains_provider_login_before_auth_mutation(monkeypatch):
    import routes.mimo_provider_routes as provider_routes

    events = []

    async def drain(_supervisor, owner):
        events.append(("drain", owner))

    manager = _auth_manager(delete_result=True)

    def delete(username, actor):
        events.append(("delete", username, actor))
        return True

    manager.delete_user = delete
    monkeypatch.setattr(provider_routes, "purge_owner_provider_flows", drain)
    result = asyncio.run(
        _handler(setup_auth_routes(manager))(
            DeleteUserRequest(username="bob"),
            _fake_request([]),
        )
    )

    assert result == {"ok": True}
    assert events[:2] == [
        ("drain", "bob"),
        ("delete", "bob", "admin"),
    ]


def test_refused_delete_does_not_invalidate_cache():
    invalidations = []
    router = setup_auth_routes(_auth_manager(delete_result=False))
    handler = _handler(router)
    try:
        asyncio.run(handler(DeleteUserRequest(username="admin"), _fake_request(invalidations)))
        raised = False
    except Exception:
        raised = True
    assert raised, "a refused delete should raise (HTTP 400)"
    assert invalidations == [], "a refused delete must not touch the token cache"


def test_delete_exception_invalidates_cache_for_partial_token_purge(monkeypatch):
    import routes.model_routes as model_routes

    invalidations = []
    model_invalidations = []
    monkeypatch.setattr(
        model_routes,
        "invalidate_model_catalogue_revision",
        model_invalidations.append,
    )
    router = setup_auth_routes(_auth_manager_raising())
    handler = _handler(router)
    try:
        asyncio.run(handler(DeleteUserRequest(username="bob"), _fake_request(invalidations)))
        raised = False
    except RuntimeError:
        raised = True
    assert raised, "delete_user exception should still propagate"
    assert invalidations == [True], "partial token purge must dirty the bearer cache"
    assert model_invalidations == ["bob"]


def test_successful_delete_removes_only_target_user_prefs():
    prefs_routes._save({
        "_users": {
            "bob": {"default_endpoint_id": "old-provider"},
            "alice": {"default_endpoint_id": "alice-provider"},
        }
    })
    router = setup_auth_routes(_auth_manager(delete_result=True))
    result = asyncio.run(
        _handler(router)(
            DeleteUserRequest(username="bob"),
            _fake_request([]),
        )
    )
    assert result == {"ok": True}
    assert prefs_routes._load() == {
        "_users": {"alice": {"default_endpoint_id": "alice-provider"}}
    }


def test_refused_delete_restores_target_user_prefs():
    original = {"_users": {"bob": {"default_endpoint_id": "old-provider"}}}
    prefs_routes._save(original)
    router = setup_auth_routes(_auth_manager(delete_result=False))
    with pytest.raises(Exception):
        asyncio.run(
            _handler(router)(
                DeleteUserRequest(username="bob"),
                _fake_request([]),
            )
        )
    assert prefs_routes._load() == original
