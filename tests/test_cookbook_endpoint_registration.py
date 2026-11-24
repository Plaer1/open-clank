import inspect

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from starlette.requests import Request

import routes.cookbook_routes as cookbook_routes
from core.provider_models import ProviderBase
from src.openclank.provider_store import ProviderStore


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
