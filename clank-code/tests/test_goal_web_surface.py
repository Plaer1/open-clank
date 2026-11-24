from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

import routes.session_routes as session_routes
from src.openclank.mimo_supervisor import MimoSupervisor


class Response:
    def __init__(self, body, status_code=200):
        self.body = body
        self.status_code = status_code

    def json(self):
        return self.body


class SessionManager:
    def __init__(self, session):
        self.session = session

    def get_session(self, sid):
        if sid != self.session.id:
            raise KeyError(sid)
        return self.session


class Supervisor:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    async def session_http_request(
        self, session_id, method, suffix, *, owner, payload=None, timeout=20.0
    ):
        self.calls.append((session_id, method, suffix, owner, payload, timeout))
        return self.responses.pop(0)


def request(supervisor):
    return SimpleNamespace(
        state=SimpleNamespace(current_user="alice"),
        app=SimpleNamespace(state=SimpleNamespace(mimo_supervisor=supervisor)),
    )


def endpoints(manager):
    router = session_routes.setup_session_routes(manager, {})
    goal_routes = [
        route for route in router.routes
        if route.path == "/api/session/{sid}/goal"
    ]
    get = next(route.endpoint for route in reversed(goal_routes) if "GET" in route.methods)
    post = next(route.endpoint for route in reversed(goal_routes) if "POST" in route.methods)
    return get, post


@pytest.fixture(autouse=True)
def owner_gate(monkeypatch):
    monkeypatch.setattr(session_routes, "_verify_session_owner", lambda *_args: None)


@pytest.mark.asyncio
async def test_goal_proxy_uses_owner_scoped_supervisor():
    manager = SessionManager(SimpleNamespace(
        id="chat-1",
        owner="alice",
        endpoint_url="openclank://engine",
        provider_model_route_id="route-chat-1",
    ))
    supervisor = Supervisor([Response({
        "state": {"active": None, "queue": [], "history": [], "revision": 0}
    })])
    get, _post = endpoints(manager)

    result = await get(request(supervisor), "chat-1")

    assert result["state"]["queue"] == []
    assert supervisor.calls == [
        ("chat-1", "GET", "goal", "alice", None, 20.0)
    ]


@pytest.mark.asyncio
async def test_goal_proxy_rejects_non_agent_session():
    manager = SessionManager(SimpleNamespace(
        id="chat-1",
        owner="alice",
        endpoint_url="https://api.example/v1",
        provider_model_route_id=None,
    ))
    get, _post = endpoints(manager)

    with pytest.raises(HTTPException) as exc:
        await get(request(Supervisor([])), "chat-1")

    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_verify_binds_target_through_existing_goal_command():
    active = {"id": "goal-1", "revision": 3, "objective": "ship", "status": "active"}
    manager = SessionManager(SimpleNamespace(
        id="chat-1",
        owner="alice",
        endpoint_url="openclank://engine",
        provider_model_route_id="route-chat-1",
    ))
    supervisor = Supervisor([
        Response({"info": {}, "parts": []}),
        Response({"state": {"active": {**active, "revision": 4}, "queue": []}}),
    ])
    _get, post = endpoints(manager)

    result = await post(request(supervisor), "chat-1", {
        "action": "verify",
        "target": {"goalID": "goal-1", "expectedRevision": 3},
    })

    assert result["state"]["active"]["revision"] == 4
    assert supervisor.calls[0][1:6] == (
        "POST",
        "command",
        "alice",
        {"command": "goal", "arguments": "verify goal-1 3"},
        240.0,
    )


@pytest.mark.asyncio
async def test_verify_propagates_runtime_stale_target_conflict():
    manager = SessionManager(SimpleNamespace(
        id="chat-1",
        owner="alice",
        endpoint_url="openclank://engine",
        provider_model_route_id="route-chat-1",
    ))
    supervisor = Supervisor([
        Response({"error": {"message": "Goal target is stale; reload before verifying"}}, 409),
    ])
    _get, post = endpoints(manager)

    with pytest.raises(HTTPException) as exc:
        await post(request(supervisor), "chat-1", {
            "action": "verify",
            "target": {"goalID": "goal-1", "expectedRevision": 3},
        })

    assert exc.value.status_code == 409
    assert [call[2] for call in supervisor.calls] == ["command"]
    assert supervisor.calls[0][4] == {
        "command": "goal",
        "arguments": "verify goal-1 3",
    }


@pytest.mark.asyncio
async def test_worker_maps_session_and_workspace_into_private_http_request():
    calls = []
    mapping = {}

    class Bridge:
        def mapped_sessions(self):
            return dict(mapping)

        def mapped_session_id(self, session_id):
            assert session_id == "chat-1"
            return mapping.get(session_id, session_id)

        def mapped_session_workspace(self, session_id):
            assert session_id == "chat-1"
            return "/workspace/alice"

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def request(self, method, path, **kwargs):
            calls.append((method, path, kwargs))
            return Response({"ok": True})

    worker = MimoSupervisor("alice", partitioned=True)
    worker._bridge = Bridge()
    worker._proc = SimpleNamespace(returncode=None)
    async def ensure_session(session_id, *, owner):
        assert (session_id, owner) == ("chat-1", "alice")
        mapping[session_id] = "ses/private"
        return "ses/private"

    worker._bridge.ensure_session = AsyncMock(side_effect=ensure_session)
    worker.delete_session = AsyncMock()
    worker.internal_http_client = lambda *, timeout: Client()

    response = await worker.session_http_request(
        "chat-1",
        "POST",
        "goal",
        owner="alice",
        payload={"action": "pause"},
        timeout=7.0,
    )

    assert response.json() == {"ok": True}
    worker._bridge.ensure_session.assert_awaited_once()
    worker.delete_session.assert_not_awaited()
    assert calls == [(
        "POST",
        "/session/ses%2Fprivate/goal",
        {
            "params": {"directory": "/workspace/alice"},
            "json": {"action": "pause"},
        },
    )]
