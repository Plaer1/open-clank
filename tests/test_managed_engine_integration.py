"""Disposable managed-engine lifecycle qualification for S33.

The fixture below models only the supervisor's owner worker and its private
HTTP client.  The route under test remains the production ``/api/tools``
handler, so every lifecycle state uses the same owner lookup, metadata/IDs
fallback, and projection code used by the host.  No model provider or browser
is contacted.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import Request

import routes.provider_v1_routes as provider_routes
from routes.provider_v1_routes import setup_provider_v1_routes


class _Response:
    def __init__(self, status_code: int, payload: object):
        self.status_code = status_code
        self._payload = payload

    @property
    def is_success(self) -> bool:
        return 200 <= self.status_code < 400

    def json(self) -> object:
        return self._payload


class _EngineClient:
    def __init__(self, worker: "_Worker"):
        self.worker = worker
        self.closed = False

    async def __aenter__(self) -> "_EngineClient":
        return self

    async def __aexit__(self, *_args: object) -> None:
        self.closed = True

    async def get(self, path: str) -> _Response:
        if self.closed:
            raise AssertionError("managed route reused a closed engine client")
        self.worker.requests.append((self.worker.generation, path))
        if path.endswith("/metadata"):
            return _Response(self.worker.metadata_status, self.worker.metadata)
        return _Response(self.worker.ids_status, self.worker.ids)


class _Worker:
    def __init__(self, metadata: list[dict[str, object]] | None = None):
        self.generation = 0
        self.alive = False
        self.metadata = metadata or []
        self.metadata_status = 200
        self.ids = [item["id"] for item in self.metadata]
        self.ids_status = 200
        self.requests: list[tuple[int, str]] = []

    def start(self, metadata: list[dict[str, object]]) -> None:
        self.generation += 1
        self.alive = True
        self.metadata = metadata
        self.ids = [item["id"] for item in metadata]
        self.metadata_status = 200
        self.ids_status = 200

    def stop(self) -> None:
        self.alive = False

    def restart(self, metadata: list[dict[str, object]]) -> None:
        self.stop()
        self.start(metadata)

    def is_alive(self) -> bool:
        return self.alive

    def internal_http_client(self, *, timeout: float) -> _EngineClient:
        del timeout
        return _EngineClient(self)


class _Supervisor:
    def __init__(self, workers: dict[str, _Worker]):
        self.workers = workers

    def worker_for_owner(self, owner: str) -> _Worker | None:
        return self.workers.get(owner)


def _request(supervisor: _Supervisor, owner: str = "alice") -> Request:
    app = SimpleNamespace(
        state=SimpleNamespace(mimo_supervisor=supervisor),
    )
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/api/tools",
            "headers": [],
            "query_string": b"",
            "server": ("test", 80),
            "client": ("127.0.0.1", 1),
            "scheme": "http",
            "app": app,
        }
    )
    request.state.current_user = owner
    request.state.api_token = False
    return request


def _list_tools_endpoint():
    router = setup_provider_v1_routes(object(), object())
    pending = list(router.routes)
    while pending:
        route = pending.pop()
        if getattr(route, "path", None) == "/api/tools" and "GET" in getattr(route, "methods", set()):
            return route.endpoint
        pending.extend(getattr(route, "routes", ()))
        original = getattr(route, "original_router", None)
        if original is not None:
            pending.extend(original.routes)
    raise AssertionError("missing /api/tools route")


def _by_id(payload: dict[str, object], tool_id: str) -> dict[str, object]:
    return next(item for item in payload["tools"] if item["id"] == tool_id)


@pytest.mark.asyncio
async def test_managed_tools_projection_tracks_owner_lifecycle_and_extensions(monkeypatch):
    disabled = {"native-disabled"}
    monkeypatch.setattr(
        provider_routes,
        "load_settings",
        lambda: {"disabled_tools": sorted(disabled)},
    )
    worker = _Worker()
    supervisor = _Supervisor({"alice": worker})
    endpoint = _list_tools_endpoint()

    stopped = await endpoint(_request(supervisor))
    assert stopped["catalog"]["freshness"] == "engine-stopped"
    assert all(item["source"] == "first-party-adapter" for item in stopped["tools"])

    worker.start(
        [
            {
                "id": "native-extension",
                "source": "extension",
                "registered": True,
                "reason": "Loaded by test extension",
            },
            {
                "id": "native-disabled",
                "source": "builtin",
                "registered": True,
                "reason": "Registered native built-in",
            },
            {
                "id": "native-unavailable",
                "source": "builtin",
                "registered": False,
                "reason": "Extension registration failed",
            },
        ]
    )
    running = await endpoint(_request(supervisor))
    assert running["catalog"]["freshness"] == "fresh"
    assert _by_id(running, "native-extension")["availability"] == "available"
    assert _by_id(running, "native-disabled")["availability"] == "disabled"
    assert _by_id(running, "native-unavailable")["availability"] == "unavailable"

    worker.restart(
        [
            {
                "id": "native-reloaded",
                "source": "extension",
                "registered": True,
                "reason": "Loaded after restart",
            }
        ]
    )
    reloaded = await endpoint(_request(supervisor))
    assert reloaded["catalog"]["freshness"] == "fresh"
    assert "native-extension" not in {item["id"] for item in reloaded["tools"]}
    assert _by_id(reloaded, "native-reloaded")["reason"] == "Loaded after restart"

    worker.stop()
    stopped_again = await endpoint(_request(supervisor))
    assert stopped_again["catalog"]["freshness"] == "engine-stopped"


@pytest.mark.asyncio
async def test_managed_tools_projection_uses_owner_worker_and_truthful_fallbacks(monkeypatch):
    monkeypatch.setattr(provider_routes, "load_settings", lambda: {"disabled_tools": []})
    alice = _Worker(
        [{"id": "alice-tool", "source": "extension", "registered": True, "reason": "Alice"}]
    )
    bob = _Worker(
        [{"id": "bob-tool", "source": "extension", "registered": True, "reason": "Bob"}]
    )
    alice.start(alice.metadata)
    bob.start(bob.metadata)
    supervisor = _Supervisor({"alice": alice, "bob": bob})
    endpoint = _list_tools_endpoint()

    alice_result = await endpoint(_request(supervisor, "alice"))
    bob_result = await endpoint(_request(supervisor, "bob"))
    assert "alice-tool" in {item["id"] for item in alice_result["tools"]}
    assert "bob-tool" not in {item["id"] for item in alice_result["tools"]}
    assert "bob-tool" in {item["id"] for item in bob_result["tools"]}
    assert "alice-tool" not in {item["id"] for item in bob_result["tools"]}

    alice.metadata_status = 404
    alice.ids = ["ids-only-tool"]
    ids_only = await endpoint(_request(supervisor, "alice"))
    assert ids_only["catalog"]["freshness"] == "ids-only"
    assert _by_id(ids_only, "ids-only-tool")["availability"] == "available"

    alice.ids_status = 503
    failed = await endpoint(_request(supervisor, "alice"))
    assert failed["catalog"]["freshness"] == "engine-query-failed"
    assert any(path.endswith("/experimental/tool/metadata") for _, path in alice.requests)
    assert any(path.endswith("/experimental/tool/ids") for _, path in alice.requests)
