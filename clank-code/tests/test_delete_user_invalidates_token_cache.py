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
from fastapi import HTTPException

import routes.prefs_routes as prefs_routes
from routes.auth_routes import setup_auth_routes, DeleteUserRequest


def _handler(router):
    for route in router.routes:
        if getattr(route, "path", "") == "/api/auth/users" and "DELETE" in getattr(route, "methods", set()):
            return route.endpoint
    raise AssertionError("DELETE /api/auth/users handler not found")


def test_file_domain_store_constructor_returns_adapter(tmp_path, monkeypatch):
    import routes.auth_routes as auth_routes
    from src.openclank.account_file_lifecycle import AccountFileOwnerLifecycle

    monkeypatch.setattr(prefs_routes, "PREFS_FILE", str(tmp_path / "prefs.json"))
    monkeypatch.setattr(auth_routes, "DEEP_RESEARCH_DIR", str(tmp_path / "research"))
    monkeypatch.setattr(auth_routes, "MEMORY_FILE", str(tmp_path / "memory.json"))
    monkeypatch.setattr(auth_routes, "SKILLS_DIR", str(tmp_path / "skills"))

    assert isinstance(auth_routes._account_file_domain_store(), AccountFileOwnerLifecycle)


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


def test_successful_delete_tombstones_and_purges_normalized_account_state():
    events = []

    class Lifecycle:
        def rename_owner(self, old_owner, new_owner):
            events.append(("rename", old_owner, new_owner))

        def purge_owner(self, owner):
            events.append(("purge", owner))

    manager = _auth_manager(delete_result=True)
    manager.account_id = lambda username: {
        "admin": "account-admin",
        "bob": "account-bob-stable",
    }.get(username)
    router = setup_auth_routes(manager, account_lifecycle=Lifecycle())

    result = asyncio.run(
        _handler(router)(
            DeleteUserRequest(username="bob"),
            _fake_request([]),
        )
    )

    assert result == {"ok": True}
    assert events == [
        ("rename", "bob", "deleted:account-bob-stable"),
        ("purge", "deleted:account-bob-stable"),
    ]


def test_rejected_delete_restores_normalized_account_tombstone():
    events = []

    class Lifecycle:
        def rename_owner(self, old_owner, new_owner):
            events.append(("rename", old_owner, new_owner))

        def purge_owner(self, owner):
            events.append(("purge", owner))

    manager = _auth_manager(delete_result=False)
    manager.account_id = lambda username: (
        "account-bob-stable" if username == "bob" else "account-admin"
    )
    router = setup_auth_routes(manager, account_lifecycle=Lifecycle())

    with pytest.raises(HTTPException) as caught:
        asyncio.run(
            _handler(router)(
                DeleteUserRequest(username="bob"),
                _fake_request([]),
            )
        )

    assert caught.value.status_code == 400
    assert events == [
        ("rename", "bob", "deleted:account-bob-stable"),
        ("rename", "deleted:account-bob-stable", "bob"),
    ]


def test_delete_drains_provider_login_before_auth_mutation(monkeypatch):
    import routes.provider_v1_routes as provider_routes

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


@pytest.mark.parametrize(
    ("target", "status_code"),
    [("", 400), ("admin", 400), ("ghost", 404)],
)
def test_invalid_delete_target_is_rejected_before_provider_flow_drain(
    monkeypatch,
    target,
    status_code,
):
    import routes.provider_v1_routes as provider_routes

    events = []

    async def drain(_supervisor, owner):
        events.append(("drain", owner))

    manager = _auth_manager(delete_result=True)
    manager.users = {"admin": {}, "bob": {}}
    manager.delete_user = lambda username, actor: events.append(
        ("delete", username, actor)
    ) or True
    monkeypatch.setattr(provider_routes, "purge_owner_provider_flows", drain)

    with pytest.raises(HTTPException) as exc:
        asyncio.run(
            _handler(setup_auth_routes(manager))(
                DeleteUserRequest(username=target),
                _fake_request([]),
            )
        )

    assert exc.value.status_code == status_code
    assert events == []


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


def test_delete_exception_invalidates_cache_for_partial_token_purge():
    invalidations = []
    router = setup_auth_routes(_auth_manager_raising())
    handler = _handler(router)
    try:
        asyncio.run(handler(DeleteUserRequest(username="bob"), _fake_request(invalidations)))
        raised = False
    except RuntimeError:
        raised = True
    assert raised, "delete_user exception should still propagate"
    assert invalidations == [True], "partial token purge must dirty the bearer cache"


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
