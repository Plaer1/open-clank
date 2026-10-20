"""Session model changes persist only normalized, secret-free route identity."""

from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from core.database import Session as DbSession
import routes.session_routes as session_routes
from src.openclank.chat_routing import MANAGED_ENGINE_PUBLIC_URL, ChatRouteUnavailable


def _route():
    return SimpleNamespace(
        provider_model_id="model-a",
        model_route_id="pmr-a",
        provider_grant_id=None,
        connection_id="pcn-a",
        public_endpoint_id="pcn-a",
    )


def _request(*, privileges=None):
    auth_manager = SimpleNamespace(
        get_privileges=lambda _user: dict(privileges or {}),
    )
    return SimpleNamespace(
        state=SimpleNamespace(current_user="alice"),
        app=SimpleNamespace(state=SimpleNamespace(auth_manager=auth_manager)),
    )


def _handler(router, path, method):
    return next(
        route.endpoint
        for route in reversed(router.routes)
        if getattr(route, "path", "") == path
        and method in getattr(route, "methods", set())
    )


@pytest.mark.asyncio
async def test_new_session_persists_normalized_route_and_no_headers(monkeypatch):
    class Manager:
        def __init__(self):
            self.created = None

        def create_session(self, **kwargs):
            self.created = kwargs
            return SimpleNamespace(name=kwargs["name"], headers={})

    manager = Manager()
    monkeypatch.setattr(session_routes, "effective_user", lambda _request: "alice")
    monkeypatch.setattr(session_routes, "resolve_chat_route", lambda **_kwargs: _route())
    monkeypatch.setattr("src.event_bus.fire_event", lambda *_args, **_kwargs: None)
    create = _handler(session_routes.setup_session_routes(manager, {}), "/api/session", "POST")

    result = await create(
        _request(),
        name="Managed chat",
        endpoint_url="https://attacker.invalid/v1",
        model="model-a",
        rag=None,
        skip_validation=None,
        incognito=None,
        api_key="",
        endpoint_id="pcn-a",
    )

    assert manager.created["owner"] == "alice"
    assert manager.created["endpoint_url"] == MANAGED_ENGINE_PUBLIC_URL
    assert manager.created["endpoint_id"] == "pcn-a"
    assert manager.created["provider_model_route_id"] == "pmr-a"
    assert result.endpoint_url == MANAGED_ENGINE_PUBLIC_URL
    assert result.endpoint_id == "pcn-a"


@pytest.mark.asyncio
async def test_raw_url_without_normalized_route_fails_closed(monkeypatch):
    monkeypatch.setattr(session_routes, "effective_user", lambda _request: "alice")

    def reject(**_kwargs):
        raise ChatRouteUnavailable("Choose a provider connection and model")

    monkeypatch.setattr(session_routes, "resolve_chat_route", reject)
    create = _handler(
        session_routes.setup_session_routes(SimpleNamespace(), {}),
        "/api/session",
        "POST",
    )
    with pytest.raises(HTTPException) as rejected:
        await create(
            _request(),
            name="Raw",
            endpoint_url="http://169.254.169.254/latest/meta-data",
            model="model-a",
            rag=None,
            skip_validation=None,
            incognito=None,
            api_key="",
            endpoint_id="",
        )
    assert rejected.value.status_code == 400


@pytest.mark.asyncio
async def test_patch_atomically_publishes_normalized_route(monkeypatch):
    session = SimpleNamespace(
        id="session-1",
        owner="alice",
        model="old",
        endpoint_url=MANAGED_ENGINE_PUBLIC_URL,
        endpoint_id="pcn-old",
        provider_model_route_id="pmr-old",
        headers={"Authorization": "stale"},
    )
    persisted = SimpleNamespace(
        model=session.model,
        endpoint_url=session.endpoint_url,
        endpoint_id=session.endpoint_id,
        provider_model_route_id=session.provider_model_route_id,
        headers=session.headers,
        updated_at=None,
    )

    class Query:
        def filter(self, *_args):
            return self

        def first(self):
            return persisted

    class Db:
        def query(self, model_cls):
            assert model_cls is DbSession
            return Query()

        def commit(self):
            return None

        def close(self):
            return None

    async def prepared(_request, _sid):
        return None

    monkeypatch.setattr(session_routes, "SessionLocal", Db)
    monkeypatch.setattr(session_routes, "effective_user", lambda _request: "alice")
    monkeypatch.setattr(session_routes, "_verify_session_owner", lambda *_args: None)
    monkeypatch.setattr(session_routes, "_prepare_context_mutation", prepared)
    monkeypatch.setattr(session_routes, "resolve_chat_route", lambda **_kwargs: _route())
    manager = SimpleNamespace(get_session=lambda _sid: session)
    patch_session = _handler(
        session_routes.setup_session_routes(manager, {}),
        "/api/session/{sid}",
        "PATCH",
    )

    result = await patch_session(
        _request(),
        "session-1",
        name=None,
        folder=None,
        model="model-a",
        endpoint_url="https://ignored.invalid/v1",
        endpoint_id="pcn-a",
    )

    assert result["endpoint_url"] == MANAGED_ENGINE_PUBLIC_URL
    assert session.endpoint_id == persisted.endpoint_id == "pcn-a"
    assert session.provider_model_route_id == persisted.provider_model_route_id == "pmr-a"
    assert session.headers == persisted.headers == {}


@pytest.mark.asyncio
async def test_failed_route_commit_does_not_publish_in_memory_route(monkeypatch):
    session = SimpleNamespace(
        id="session-1",
        owner="alice",
        model="old",
        endpoint_url=MANAGED_ENGINE_PUBLIC_URL,
        endpoint_id="pcn-old",
        provider_model_route_id="pmr-old",
        headers={},
    )
    persisted = SimpleNamespace(
        model=session.model,
        endpoint_url=session.endpoint_url,
        endpoint_id=session.endpoint_id,
        provider_model_route_id=session.provider_model_route_id,
        headers={},
        updated_at=None,
    )

    class Query:
        def filter(self, *_args):
            return self

        def first(self):
            return persisted

    class FailingDb:
        def query(self, model_cls):
            assert model_cls is DbSession
            return Query()

        def commit(self):
            raise RuntimeError("disk full")

        def close(self):
            return None

    async def prepared(_request, _sid):
        return None

    monkeypatch.setattr(session_routes, "SessionLocal", FailingDb)
    monkeypatch.setattr(session_routes, "effective_user", lambda _request: "alice")
    monkeypatch.setattr(session_routes, "_verify_session_owner", lambda *_args: None)
    monkeypatch.setattr(session_routes, "_prepare_context_mutation", prepared)
    monkeypatch.setattr(session_routes, "resolve_chat_route", lambda **_kwargs: _route())
    patch_session = _handler(
        session_routes.setup_session_routes(
            SimpleNamespace(get_session=lambda _sid: session),
            {},
        ),
        "/api/session/{sid}",
        "PATCH",
    )

    with pytest.raises(RuntimeError, match="disk full"):
        await patch_session(
            _request(),
            "session-1",
            name=None,
            folder=None,
            model="model-a",
            endpoint_url=MANAGED_ENGINE_PUBLIC_URL,
            endpoint_id="pcn-a",
        )

    assert session.model == "old"
    assert session.endpoint_id == "pcn-old"
    assert session.provider_model_route_id == "pmr-old"


@pytest.mark.asyncio
async def test_patch_binds_and_unbinds_stable_workspace_after_validation(monkeypatch):
    session = SimpleNamespace(id="session-1", owner="alice", workspace_id=None)
    updates = []
    preparations = []

    class Manager:
        def get_session(self, _sid):
            return session

        def update_session_workspace(self, sid, workspace_id):
            updates.append((sid, workspace_id))
            session.workspace_id = workspace_id
            return True

    async def prepared(_request, sid):
        preparations.append(sid)

    validated = []
    monkeypatch.setattr(session_routes, "effective_user", lambda _request: "alice")
    monkeypatch.setattr(session_routes, "_verify_session_owner", lambda *_args: None)
    monkeypatch.setattr(session_routes, "_prepare_context_mutation", prepared)
    monkeypatch.setattr(
        session_routes,
        "_validated_session_workspace",
        lambda _request, owner, workspace_id: validated.append((owner, workspace_id)),
    )
    patch_session = _handler(
        session_routes.setup_session_routes(Manager(), {}),
        "/api/session/{sid}",
        "PATCH",
    )

    bound = await patch_session(
        _request(),
        "session-1",
        name=None,
        folder=None,
        model=None,
        endpoint_url=None,
        endpoint_id=None,
        workspace_id="workspace-a",
    )
    cleared = await patch_session(
        _request(),
        "session-1",
        name=None,
        folder=None,
        model=None,
        endpoint_url=None,
        endpoint_id=None,
        workspace_id="",
    )

    assert validated == [("alice", "workspace-a")]
    assert updates == [("session-1", "workspace-a"), ("session-1", None)]
    assert preparations == ["session-1", "session-1"]
    assert bound["workspace_id"] == "workspace-a"
    assert cleared["workspace_id"] is None


def test_session_and_chat_sources_have_no_legacy_provider_authority():
    root = Path(__file__).resolve().parents[1]
    session_source = (root / "routes" / "session_routes.py").read_text(encoding="utf-8")
    chat_source = (root / "routes" / "chat_routes.py").read_text(encoding="utf-8")
    helpers_source = (root / "routes" / "chat_helpers.py").read_text(encoding="utf-8")

    for source in (session_source, chat_source, helpers_source):
        assert "ModelEndpoint" not in source
        assert "routes.model_routes" not in source
        assert "resolve_chat_fallback_candidates" not in source
    assert "resolve_chat_route(" in session_source
    assert "resolve_chat_route(" in chat_source
    assert "def resolve_session_auth(" not in helpers_source
    assert "def try_fallback_endpoint(" not in helpers_source
