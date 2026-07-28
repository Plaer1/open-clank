from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from core.database import ModelEndpoint, Session as DbSession

# Import the route helper during collection so sibling session tests that use
# partial import stubs do not become the first loader of core.session_manager.
import routes.session_routes as session_routes
from routes.session_routes import _reject_raw_endpoint_url_for_non_admin


def _request(user, *, admin=False):
    auth_manager = SimpleNamespace(is_admin=lambda username: bool(admin))
    return SimpleNamespace(
        state=SimpleNamespace(current_user=user),
        app=SimpleNamespace(state=SimpleNamespace(auth_manager=auth_manager)),
    )


def test_non_admin_session_create_rejects_raw_endpoint_url_without_endpoint_id():
    with pytest.raises(HTTPException) as exc:
        _reject_raw_endpoint_url_for_non_admin(
            _request("alice", admin=False),
            "alice",
            "",
            "http://169.254.169.254/latest/meta-data",
        )

    assert exc.value.status_code == 403


def test_admin_and_registered_endpoint_can_use_endpoint_url():
    _reject_raw_endpoint_url_for_non_admin(
        _request("alice", admin=False),
        "alice",
        "endpoint-id",
        "http://127.0.0.1:8000/v1/chat/completions",
    )
    _reject_raw_endpoint_url_for_non_admin(
        _request("admin", admin=True),
        "admin",
        "",
        "http://127.0.0.1:8000/v1/chat/completions",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("requested_endpoint_id", "persisted_endpoint_id"),
    [
        ("mimo", "mimo:auto"),
        ("mimo:xiaomi", "mimo:xiaomi"),
    ],
)
async def test_existing_session_can_switch_to_native_connection(
    monkeypatch,
    requested_endpoint_id,
    persisted_endpoint_id,
):
    session = SimpleNamespace(
        id="session-1",
        owner="alice",
        model="old-model",
        endpoint_url="https://old.example/v1/chat/completions",
        endpoint_id="old-endpoint",
        headers={"Authorization": "old"},
    )
    persisted = SimpleNamespace(
        model=session.model,
        endpoint_url=session.endpoint_url,
        endpoint_id=session.endpoint_id,
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
            assert model_cls is DbSession, "virtual MiMo must not query ModelEndpoint"
            return Query()

        def commit(self):
            pass

        def close(self):
            pass

    manager = SimpleNamespace(get_session=lambda sid: session)
    worker = SimpleNamespace(
        available_models=lambda: [{"modelId": "xiaomi/mimo-v2.5-pro"}],
    )

    class ColdSupervisor:
        def __init__(self):
            self.started = []

        def available_models(self, owner=None):
            return []

        async def for_owner(self, owner):
            self.started.append(owner)
            return worker

    supervisor = ColdSupervisor()
    auth_manager = SimpleNamespace(
        is_admin=lambda user: False,
        get_privileges=lambda user: {},
    )
    request = SimpleNamespace(
        state=SimpleNamespace(current_user="alice"),
        app=SimpleNamespace(state=SimpleNamespace(
            auth_manager=auth_manager,
            mimo_supervisor=supervisor,
        )),
    )

    monkeypatch.setattr(session_routes, "SessionLocal", Db)
    monkeypatch.setattr(session_routes, "effective_user", lambda request: "alice")
    monkeypatch.setattr(session_routes, "_verify_session_owner", lambda request, sid: None)
    monkeypatch.setattr(
        "routes.model_routes._connected_mimo_provider_ids",
        lambda owner: {"xiaomi"},
    )

    async def prepare_context_mutation(request, sid):
        return None

    monkeypatch.setattr(session_routes, "_prepare_context_mutation", prepare_context_mutation)
    router = session_routes.setup_session_routes(manager, {})
    patch_session = next(
        route.endpoint
        for route in reversed(router.routes)
        if getattr(route, "path", "") == "/api/session/{sid}"
        and "PATCH" in getattr(route, "methods", set())
    )

    result = await patch_session(
        request,
        "session-1",
        name=None,
        folder=None,
        model="xiaomi/mimo-v2.5-pro",
        endpoint_url="mimo://acp",
        endpoint_id=requested_endpoint_id,
    )

    assert result["model"] == "xiaomi/mimo-v2.5-pro"
    assert result["endpoint_url"] == "mimo://acp"
    assert result["endpoint_id"] == persisted_endpoint_id
    assert session.model == persisted.model == "xiaomi/mimo-v2.5-pro"
    assert session.endpoint_url == persisted.endpoint_url == "mimo://acp"
    assert session.endpoint_id == persisted.endpoint_id == persisted_endpoint_id
    assert session.headers == persisted.headers == {}
    assert supervisor.started == ["alice"]


@pytest.mark.asyncio
async def test_existing_session_can_switch_back_to_owned_direct_endpoint(monkeypatch):
    session = SimpleNamespace(
        id="session-1",
        owner="alice",
        model="xiaomi/mimo-v2.5-pro",
        endpoint_url="mimo://acp",
        endpoint_id="mimo:xiaomi",
        headers={},
    )
    persisted = SimpleNamespace(
        model=session.model,
        endpoint_url=session.endpoint_url,
        endpoint_id=session.endpoint_id,
        headers=session.headers,
        updated_at=None,
    )
    endpoint = SimpleNamespace(
        id="alice-direct",
        owner="alice",
        base_url="https://models.example/v1",
        api_key="alice-key",
        is_enabled=True,
        cached_models='["direct-chat"]',
        hidden_models=None,
        pinned_models=None,
    )

    class Query:
        def __init__(self, result):
            self.result = result

        def filter(self, *_args):
            return self

        def first(self):
            return self.result

    class Db:
        def query(self, model_cls):
            return Query(endpoint if model_cls is ModelEndpoint else persisted)

        def commit(self):
            pass

        def close(self):
            pass

    manager = SimpleNamespace(get_session=lambda sid: session)
    auth_manager = SimpleNamespace(
        is_admin=lambda user: False,
        get_privileges=lambda user: {},
    )
    request = SimpleNamespace(
        state=SimpleNamespace(current_user="alice"),
        app=SimpleNamespace(state=SimpleNamespace(auth_manager=auth_manager)),
    )
    monkeypatch.setattr(session_routes, "SessionLocal", Db)
    monkeypatch.setattr(session_routes, "effective_user", lambda request: "alice")
    monkeypatch.setattr(session_routes, "_verify_session_owner", lambda request, sid: None)
    monkeypatch.setattr(
        "src.auth_helpers.owner_filter",
        lambda query, *_args, **_kwargs: query,
    )

    async def prepare_context_mutation(request, sid):
        return None

    monkeypatch.setattr(
        session_routes,
        "_prepare_context_mutation",
        prepare_context_mutation,
    )
    router = session_routes.setup_session_routes(manager, {})
    patch_session = next(
        route.endpoint
        for route in reversed(router.routes)
        if getattr(route, "path", "") == "/api/session/{sid}"
        and "PATCH" in getattr(route, "methods", set())
    )

    result = await patch_session(
        request,
        "session-1",
        name=None,
        folder=None,
        model="direct-chat",
        endpoint_url="https://ignored.example/v1",
        endpoint_id="alice-direct",
    )

    assert result["endpoint_id"] == "alice-direct"
    assert result["endpoint_url"] == "https://models.example/v1/chat/completions"
    assert session.endpoint_id == persisted.endpoint_id == "alice-direct"
    assert session.model == persisted.model == "direct-chat"
    assert session.headers == persisted.headers == {
        "Authorization": "Bearer alice-key",
    }


def test_direct_endpoint_model_pair_must_exist_in_visible_catalog():
    endpoint = SimpleNamespace(
        cached_models='["model-a", "model-b"]',
        hidden_models='["model-b"]',
        pinned_models=None,
    )

    session_routes._validate_direct_endpoint_model(endpoint, "model-a")
    with pytest.raises(HTTPException) as exc:
        session_routes._validate_direct_endpoint_model(endpoint, "model-b")
    assert exc.value.status_code == 400
    with pytest.raises(HTTPException):
        session_routes._validate_direct_endpoint_model(endpoint, "model-from-another-endpoint")


@pytest.mark.asyncio
async def test_failed_route_commit_does_not_publish_new_in_memory_route(monkeypatch):
    session = SimpleNamespace(
        id="session-1",
        owner="alice",
        model="old-model",
        endpoint_url="https://old.example/chat",
        endpoint_id="old-endpoint",
        headers={"Authorization": "old"},
    )
    persisted = SimpleNamespace(
        model=session.model,
        endpoint_url=session.endpoint_url,
        endpoint_id=session.endpoint_id,
        headers=session.headers,
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
            pass

    async def native_models(_request, _owner, _endpoint_id):
        return "mimo:xiaomi", ["xiaomi/mimo-v2.5-pro"]

    async def prepare_context_mutation(_request, _sid):
        return None

    auth_manager = SimpleNamespace(
        is_admin=lambda _user: False,
        get_privileges=lambda _user: {},
    )
    request = SimpleNamespace(
        state=SimpleNamespace(current_user="alice"),
        app=SimpleNamespace(state=SimpleNamespace(auth_manager=auth_manager)),
    )
    monkeypatch.setattr(session_routes, "SessionLocal", FailingDb)
    monkeypatch.setattr(session_routes, "effective_user", lambda _request: "alice")
    monkeypatch.setattr(session_routes, "_verify_session_owner", lambda _request, _sid: None)
    monkeypatch.setattr(session_routes, "_native_connection_models", native_models)
    monkeypatch.setattr(session_routes, "_prepare_context_mutation", prepare_context_mutation)
    router = session_routes.setup_session_routes(
        SimpleNamespace(get_session=lambda _sid: session),
        {},
    )
    patch_session = next(
        route.endpoint
        for route in reversed(router.routes)
        if getattr(route, "path", "") == "/api/session/{sid}"
        and "PATCH" in getattr(route, "methods", set())
    )

    with pytest.raises(RuntimeError, match="disk full"):
        await patch_session(
            request,
            "session-1",
            name=None,
            folder=None,
            model="xiaomi/mimo-v2.5-pro",
            endpoint_url="mimo://acp",
            endpoint_id="mimo:xiaomi",
        )

    assert session.model == "old-model"
    assert session.endpoint_url == "https://old.example/chat"
    assert session.endpoint_id == "old-endpoint"
    assert session.headers == {"Authorization": "old"}


@pytest.mark.asyncio
async def test_new_session_accepts_provider_scoped_native_connection(monkeypatch):
    class Manager:
        def __init__(self):
            self.created = None

        def create_session(self, **kwargs):
            self.created = kwargs
            return SimpleNamespace(name=kwargs["name"], headers={})

    manager = Manager()
    supervisor = SimpleNamespace(
        available_models=lambda owner=None: [
            {"modelId": "deepseek/deepseek-chat"},
            {"modelId": "xiaomi/mimo-v2.5-pro"},
        ],
    )
    auth_manager = SimpleNamespace(
        is_admin=lambda user: False,
        get_privileges=lambda user: {},
    )
    request = SimpleNamespace(
        state=SimpleNamespace(current_user="alice"),
        app=SimpleNamespace(state=SimpleNamespace(
            auth_manager=auth_manager,
            mimo_supervisor=supervisor,
        )),
    )
    monkeypatch.setattr(session_routes, "effective_user", lambda request: "alice")
    monkeypatch.setattr(
        "routes.model_routes._connected_mimo_provider_ids",
        lambda owner: {"xiaomi", "deepseek"},
    )
    monkeypatch.setattr("src.event_bus.fire_event", lambda *args, **kwargs: None)
    router = session_routes.setup_session_routes(manager, {})
    create_session = next(
        route.endpoint
        for route in reversed(router.routes)
        if getattr(route, "path", "") == "/api/session"
        and "POST" in getattr(route, "methods", set())
    )

    result = await create_session(
        request,
        name="Phone chat",
        endpoint_url="mimo://acp",
        model="xiaomi/mimo-v2.5-pro",
        rag=None,
        skip_validation=None,
        incognito=None,
        api_key="",
        endpoint_id="mimo:xiaomi",
    )

    assert manager.created["owner"] == "alice"
    assert manager.created["endpoint_url"] == "mimo://acp"
    assert manager.created["endpoint_id"] == "mimo:xiaomi"
    assert manager.created["model"] == "xiaomi/mimo-v2.5-pro"
    assert result.model == "xiaomi/mimo-v2.5-pro"
    assert result.endpoint_url == "mimo://acp"
    assert result.endpoint_id == "mimo:xiaomi"


def test_chat_endpoint_recovery_paths_are_owner_scoped():
    root = Path(__file__).resolve().parents[1]
    chat_routes = (root / "routes" / "chat_routes.py").read_text(encoding="utf-8")
    chat_helpers = (root / "routes" / "chat_helpers.py").read_text(encoding="utf-8")

    assert "def _clear_orphaned_session_endpoint(sess, owner:" in chat_routes
    assert "def _recover_empty_session_model(sess, session_id: str, owner:" in chat_routes
    assert 'q = owner_filter(q, ModelEndpoint, owner or "", include_shared=False)' in chat_routes
    assert "resolve_session_auth(sess, session, owner=effective_user(request))" in chat_routes
    assert "def resolve_session_auth(sess, session_id: str, owner:" in chat_helpers
    assert "update_q = update_q.filter(DBSession.owner == owner)" in chat_helpers
