from pathlib import Path

import inspect

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from starlette.requests import Request

import routes.cookbook_routes as cookbook_routes
from core.provider_models import ProviderBase
from src.openclank.provider_store import ProviderStore


ROOT = Path(__file__).resolve().parents[1]
COOKBOOK_RUNNING = ROOT / "static" / "js" / "cookbookRunning.js"
COOKBOOK_ROUTES = ROOT / "routes" / "cookbook_routes.py"
COOKBOOK_TOOLS = ROOT / "src" / "tools" / "cookbook.py"


def _source() -> str:
    return COOKBOOK_RUNNING.read_text(encoding="utf-8")


def test_cookbook_uses_only_normalized_provider_control_plane():
    src = _source()
    routes = COOKBOOK_ROUTES.read_text(encoding="utf-8")
    tools = COOKBOOK_TOOLS.read_text(encoding="utf-8")

    for source in (src, routes, tools):
        assert "ModelEndpoint" not in source
        assert "/api/model-endpoints" not in source
    assert "/api/cookbook/provider-connections" in src
    assert "/api/cookbook/provider-connections" in tools
    assert 'method="_openclank/provider-control/v1/connection/validate"' in routes
    assert "provider_store.create_connection(" in routes
    assert "provider_store.create_model_route(" in routes


def test_cookbook_does_not_use_local_as_provider_hostname():
    src = _source()
    assert "function _connectHostFromRemote" in src
    assert "if (!host || host === 'local') return fallback;" in src
    assert "const rawHost = task.remoteHost || 'localhost';" not in src


def test_cookbook_advertised_bind_urls_keep_connectable_host():
    src = _source()
    assert "function _endpointFromAdvertisedUrl" in src
    assert "_isAnyBindHost(u.hostname) ? currentHost" in src
    assert "host = u.hostname || host;" not in src


def test_cookbook_provider_cleanup_is_owner_and_provenance_scoped():
    routes = COOKBOOK_ROUTES.read_text(encoding="utf-8")

    assert 'settings.get("managed_by") != "cookbook"' in routes
    assert "owner=_cookbook_provider_owner(request)" in routes
    assert 'session_id=session_id' in routes
    assert '"status": "retained"' in routes
    assert "provider_store.delete_connection(" in routes


def test_cookbook_model_selection_uses_managed_catalog_identity():
    src = _source()

    assert "item.endpoint_id === connection.id" in src
    assert "window.sessionModule.createDirectChat(item.url, mid, item.endpoint_id)" in src
    assert "url.includes(host) || url.includes(port)" not in src
    assert "do_manage_endpoints" not in COOKBOOK_TOOLS.read_text(encoding="utf-8")


class _ManagedControl:
    def __init__(self):
        self.calls = []

    async def call(self, *, request, owner, method, payload):
        self.calls.append((owner, method, payload))
        return {
            "familyID": payload["familyID"],
            "adapterID": payload["adapterID"],
            "kind": payload["kind"],
            "billingLane": payload["billingLane"],
            "normalizedURL": payload["url"].rstrip("/"),
            "settings": {},
            "credentialRequired": False,
        }


@pytest.fixture()
def provider_router(monkeypatch):
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    ProviderBase.metadata.create_all(bind=engine)
    factory = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    store = ProviderStore(factory)
    control = _ManagedControl()
    monkeypatch.setattr(cookbook_routes, "require_admin", lambda request: None)
    monkeypatch.setattr(cookbook_routes, "effective_user", lambda request: "Alice")
    router = cookbook_routes.setup_cookbook_routes(store, control)
    yield router, store, control
    engine.dispose()


def _route(router, path, method):
    return next(
        route.endpoint
        for route in router.routes
        if route.path == path and method in route.methods
    )


def _request(path="/api/cookbook/provider-connections"):
    return Request({"type": "http", "method": "POST", "path": path, "headers": []})


@pytest.mark.asyncio
async def test_registration_creates_keyless_connection_and_stable_route(provider_router):
    router, store, control = provider_router
    register = _route(router, "/api/cookbook/provider-connections", "POST")
    payload_type = inspect.signature(register).parameters["payload"].annotation
    payload = payload_type(
        url="http://localhost:8123/v1/",
        model_id="org/model-a",
        name="Model A",
        model_type="llm",
        session_id="serve-a",
        supports_tools=True,
    )

    result = await register(_request(), payload)

    connection = store.get_connection(owner="alice", connection_id=result["id"])
    route = store.get_model_route(owner="alice", model_route_id=result["model_route_id"])
    assert connection.normalized_url == "http://localhost:8123/v1"
    assert connection.kind == "local"
    assert connection.billing_lane == "local"
    assert connection.settings == {
        "managed_by": "cookbook",
        "cookbook_sessions": ["serve-a"],
    }
    assert route.provider_model_id == "org/model-a"
    assert set(route.operations) == {"chat.stream", "chat.complete"}
    assert route.capabilities["tool_call"] is True
    assert control.calls == [
        (
            "alice",
            "_openclank/provider-control/v1/connection/validate",
            {
                "familyID": "openai-compatible",
                "adapterID": "openai-chat",
                "kind": "local",
                "billingLane": "local",
                "url": "http://localhost:8123/v1/",
                "settings": {},
            },
        )
    ]


@pytest.mark.asyncio
async def test_cleanup_retains_shared_cookbook_connection_until_last_session(provider_router):
    router, store, _control = provider_router
    register = _route(router, "/api/cookbook/provider-connections", "POST")
    remove = _route(
        router,
        "/api/cookbook/provider-connections/{connection_id}",
        "DELETE",
    )
    payload_type = inspect.signature(register).parameters["payload"].annotation

    first = await register(
        _request(),
        payload_type(
            url="http://localhost:8124/v1",
            model_id="org/model-b",
            session_id="serve-one",
        ),
    )
    second = await register(
        _request(),
        payload_type(
            url="http://localhost:8124/v1",
            model_id="org/model-b",
            session_id="serve-two",
        ),
    )
    assert second["id"] == first["id"]

    retained = remove(first["id"], _request(), session_id="serve-one")
    assert retained["status"] == "retained"
    connection = store.get_connection(owner="alice", connection_id=first["id"])
    assert connection.settings["cookbook_sessions"] == ["serve-two"]

    deleted = remove(first["id"], _request(), session_id="serve-two")
    assert deleted["status"] == "deleted"
