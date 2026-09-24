import asyncio
import os
import uuid
from types import SimpleNamespace

import httpx
import pytest
from fastapi import HTTPException
from starlette.datastructures import URL
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import routes.mimo_provider_routes as routes
import core.database as database
import src.openclank.transcript_projection as projection
from core.database import Base, MimoProjection, Session
from src.openclank.session_map import OwnerSessionMap


class Supervisor:
    http_base_url = "http://127.0.0.1:32123"

    def is_alive(self):
        return True

    async def refresh_model_catalog(self):
        return []


def request(
    *,
    user="alice",
    cookie="session-alice",
    base_url=None,
    url="https://openclank.example/settings",
    headers=None,
):
    supervisor = Supervisor()
    if base_url is not None:
        supervisor.http_base_url = base_url
    auth = SimpleNamespace(is_configured=True, is_admin=lambda _user: False)
    return SimpleNamespace(
        headers=headers or {},
        cookies={routes._SESSION_COOKIE: cookie} if cookie else {},
        url=URL(url),
        client=SimpleNamespace(host="203.0.113.10"),
        state=SimpleNamespace(current_user=user),
        app=SimpleNamespace(state=SimpleNamespace(mimo_supervisor=supervisor, auth_manager=auth)),
    )


def endpoint(method, suffix):
    for route in routes.setup_mimo_provider_routes().routes:
        if method in (route.methods or set()) and route.path.endswith(suffix):
            return route.endpoint
    raise AssertionError(f"missing {method} {suffix}")


def catalog(provider_id="openai", methods=None):
    return (
        {"all": [{"id": provider_id, "name": provider_id.title(), "models": {"secret": "ignored"}}], "connected": []},
        {provider_id: methods or [{"type": "oauth", "label": "Login"}, {"type": "api", "label": "API key"}]},
    )


@pytest.fixture(autouse=True)
def fresh_flow_store(monkeypatch):
    store = routes._OAuthFlowStore()
    monkeypatch.setattr(routes, "_oauth_flows", store)
    return store


@pytest.mark.asyncio
async def test_provider_list_allows_regular_authenticated_user(monkeypatch):
    async def fake_catalog(_supervisor):
        return catalog()

    monkeypatch.setattr(routes, "_catalog", fake_catalog)
    result = await endpoint("GET", "/api/mimo/providers")(request=request(user="bob", cookie="session-bob"))
    assert result["available"] is True


@pytest.mark.asyncio
async def test_provider_routes_require_a_browser_session(monkeypatch):
    monkeypatch.setenv("AUTH_ENABLED", "true")
    with pytest.raises(HTTPException) as exc:
        await endpoint("GET", "/api/mimo/providers")(request=request(cookie=None))
    assert exc.value.status_code == 401


def test_supervisor_target_must_be_loopback(monkeypatch):
    monkeypatch.setenv("AUTH_ENABLED", "false")
    with pytest.raises(HTTPException) as exc:
        routes._supervisor(request(base_url="http://example.test:80"))
    assert exc.value.status_code == 503


@pytest.mark.asyncio
async def test_each_worker_gets_unique_child_only_http_credentials(monkeypatch):
    from src.openclank.mimo_supervisor import MimoSupervisor

    monkeypatch.delenv("MIMOCODE_SERVER_PASSWORD", raising=False)
    monkeypatch.delenv("MIMOCODE_SERVER_USERNAME", raising=False)
    first = MimoSupervisor("alice", partitioned=True)
    second = MimoSupervisor("bob", partitioned=True)
    first_env: dict[str, str] = {}
    second_env: dict[str, str] = {}
    first_fd = first._configure_internal_http_auth(first_env)
    second_fd = second._configure_internal_http_auth(second_env)
    try:
        first_password = os.read(first_fd, 4096).decode()
        second_password = os.read(second_fd, 4096).decode()
    finally:
        os.close(first_fd)
        os.close(second_fd)

    assert first_env["MIMOCODE_SERVER_USERNAME"] == "open-clank"
    assert second_env["MIMOCODE_SERVER_USERNAME"] == "open-clank"
    assert first_password
    assert first_password != second_password
    assert "MIMOCODE_SERVER_PASSWORD" not in first_env
    assert "MIMOCODE_SERVER_PASSWORD" not in second_env
    assert first_env["OPEN_CLANK_WORKER_AUTH_FD"] == str(first_fd)
    assert second_env["OPEN_CLANK_WORKER_AUTH_FD"] == str(second_fd)
    assert "MIMOCODE_SERVER_PASSWORD" not in os.environ
    assert "MIMOCODE_SERVER_USERNAME" not in os.environ
    assert "OPEN_CLANK_WORKER_AUTH_FD" not in os.environ

    first_client = first.internal_http_client()
    second_client = second.internal_http_client()
    try:
        first_request = next(first_client._auth.sync_auth_flow(
            httpx.Request("GET", "http://127.0.0.1/provider")
        ))
        second_request = next(second_client._auth.sync_auth_flow(
            httpx.Request("GET", "http://127.0.0.1/provider")
        ))
        expected_first = next(httpx.BasicAuth(
            "open-clank", first_password
        ).sync_auth_flow(httpx.Request("GET", "http://127.0.0.1/provider")))
        expected_second = next(httpx.BasicAuth(
            "open-clank", second_password
        ).sync_auth_flow(httpx.Request("GET", "http://127.0.0.1/provider")))
        assert first_request.headers["Authorization"] == expected_first.headers["Authorization"]
        assert second_request.headers["Authorization"] == expected_second.headers["Authorization"]
    finally:
        await first_client.aclose()
        await second_client.aclose()


@pytest.mark.asyncio
async def test_native_provider_calls_use_supervisor_internal_client():
    calls = []

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"ok": True}

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def request(self, method, path, json=None):
            calls.append((method, path, json))
            return Response()

    class AuthenticatedSupervisor:
        def internal_http_client(self, *, timeout):
            calls.append(("client", timeout))
            return Client()

    result = await routes._native(
        AuthenticatedSupervisor(),
        "PUT",
        "/auth/xiaomi",
        {"type": "api", "key": "secret"},
        timeout=7.0,
    )

    assert result == {"ok": True}
    assert calls == [
        ("client", 7.0),
        ("PUT", "/auth/xiaomi", {"type": "api", "key": "secret"}),
    ]


@pytest.mark.asyncio
async def test_session_delete_uses_supervisor_internal_client():
    from src.openclank.mimo_supervisor import MimoSupervisor

    calls = []

    class Bridge:
        async def cleanup_session(self, session_id):
            calls.append(("cleanup", session_id))

        def mapped_session_id(self, _session_id):
            return "mimo/session"

        def forget_session(self, session_id):
            calls.append(("forget", session_id))

    class Response:
        status_code = 204

        def raise_for_status(self):
            return None

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def delete(self, path):
            calls.append(("delete", path))
            return Response()

    supervisor = MimoSupervisor("alice", partitioned=True)
    supervisor._bridge = Bridge()
    supervisor._proc = SimpleNamespace(returncode=None)

    def internal_http_client(*, timeout):
        calls.append(("client", timeout))
        return Client()

    supervisor.internal_http_client = internal_http_client
    await supervisor.delete_session("odysseus-session")

    assert calls == [
        ("cleanup", "mimo/session"),
        ("client", 10.0),
        ("delete", "/session/mimo%2Fsession"),
        ("forget", "odysseus-session"),
    ]


@pytest.mark.asyncio
async def test_session_http_uses_persisted_binding_over_conflicting_projection(
    tmp_path,
    monkeypatch,
):
    engine = create_engine(f"sqlite:///{tmp_path / 'authority.db'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine)
    monkeypatch.setattr(database, "SessionLocal", sessions)
    monkeypatch.setattr(projection, "SessionLocal", sessions)
    db = sessions()
    db.add(Session(
        id="chat-a",
        name="chat",
        endpoint_url="http://example.test",
        model="model",
        owner="alice",
        mimo_state={},
    ))
    db.flush()
    db.add(MimoProjection(
        odysseus_session_id="chat-a",
        owner="alice",
        mimo_session_id="projection-engine",
        workspace="/projection-must-not-win",
        endpoint_url="http://example.test",
        model="model",
        transcript_revision=0,
        covered_message_ids="[]",
        canonical_digest="digest",
        lifecycle_state="active",
        active_turn_id="turn-1",
    ))
    db.commit()
    db.close()
    bound_cwd = str(tmp_path / "managed")
    (tmp_path / "managed").mkdir()
    binding = {
        "owner": "alice", "stableChatID": "chat-a", "engineSessionID": "engine-a",
        "engineAliases": [], "memoryWorkspaceID": "memory:chat-a",
        "authorityWorkspaceID": "workspace:chat-a", "copalWorkspace": "default",
        "physicalCwd": bound_cwd, "workspaceRevision": 0,
        "mapRevision": 1, "mappingRevision": 1, "memoryEnabled": True,
        "transition": None,
    }
    projection.save_managed_binding(
        "chat-a", binding, owner="alice", expected_workspace_revision=0,
        expected_engine_session_id=None, expected_map_revision=0,
        expected_mapping_revision=0,
    )
    map_path = tmp_path / "session-map.json"
    mapping = OwnerSessionMap(map_path, "alice")
    mapping.bind("chat-a", "engine-a", expected_map_revision=0, expected_current=None, expected_mapping_revision=0)

    from src.openclank.mimo_supervisor import MimoSupervisor

    class ScriptedACPClient:
        def register_callback(self, method, callback):
            self.callbacks = getattr(self, "callbacks", {})
            self.callbacks[method] = callback

        def on_session_update(self, callback):
            self.session_update = callback

    from src.openclank.acp_bridge import ACPBridge

    bridge = ACPBridge(
        ScriptedACPClient(),
        cwd=bound_cwd,
        owner="alice",
        session_map_path=map_path,
    )

    calls = []

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def request(self, method, path, **kwargs):
            calls.append((method, path, kwargs))
            return SimpleNamespace(status_code=200)

    worker = MimoSupervisor("alice", partitioned=True)
    worker._bridge = bridge
    worker._proc = SimpleNamespace(returncode=None)
    worker.internal_http_client = lambda *, timeout: Client()
    await worker.session_http_request("chat-a", "GET", "status", owner="alice")
    assert calls[0][2]["params"]["directory"] == bound_cwd
    assert calls[0][2]["params"]["directory"] != "/projection-must-not-win"
    projection.save_managed_binding(
        "chat-a",
        {**binding, "engineSessionID": "forged-engine", "mapRevision": 2, "mappingRevision": 2},
        owner="alice", expected_workspace_revision=0,
        expected_engine_session_id="engine-a", expected_map_revision=1,
        expected_mapping_revision=1,
    )
    with pytest.raises(RuntimeError, match="binding is stale"):
        await worker.session_http_request("chat-a", "GET", "status", owner="alice")
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_list_sanitizes_payload_and_exposes_explicit_capabilities(monkeypatch):
    async def fake_catalog(_supervisor):
        return catalog()

    monkeypatch.setattr(routes, "_catalog", fake_catalog)
    result = await endpoint("GET", "/api/mimo/providers")(request=request())
    provider = result["providers"][0]
    assert provider == {
        "id": "openai",
        "name": "Openai",
        "connected": False,
        "connection_id": "mimo:openai",
        "methods": [
            {
                "index": 0,
                "type": "oauth",
                "label": "Login",
                "capability": "device_code",
            },
            {
                "index": 1,
                "type": "api",
                "label": "API key",
                "capability": "api_key",
            },
        ],
        "capabilities": {
            "redirect": False,
            "device_code": True,
            "browser_callback": False,
            "paste_code": False,
            "api_key": True,
        },
        "family": None,
        "chat_models": 0,
        "active": False,
        "served_by": None,
        "hidden_model_ids": [],
        "hidden_count": 0,
    }
    assert "models" not in provider


@pytest.mark.asyncio
async def test_list_filters_unsupported_generic_oauth_methods(monkeypatch):
    async def fake_catalog(_supervisor):
        return catalog(
            "synthetic",
            [
                {"type": "oauth", "label": "Unaudited browser login"},
                {"type": "api", "label": "API key"},
            ],
        )

    monkeypatch.setattr(routes, "_catalog", fake_catalog)
    result = await endpoint("GET", "/api/mimo/providers")(request=request())
    provider = result["providers"][0]

    assert provider["methods"] == [{
        "index": 1,
        "type": "api",
        "label": "API key",
        "capability": "api_key",
    }]
    assert provider["capabilities"] == {
        "redirect": False,
        "device_code": False,
        "browser_callback": False,
        "paste_code": False,
        "api_key": True,
    }


@pytest.mark.asyncio
async def test_list_exposes_only_remote_safe_xai_oauth_method(monkeypatch):
    async def fake_catalog(_supervisor):
        return catalog(
            "xai",
            [
                {"type": "oauth", "label": "Loopback browser login"},
                {"type": "oauth", "label": "Device login"},
                {"type": "api", "label": "API key"},
            ],
        )

    monkeypatch.setattr(routes, "_catalog", fake_catalog)
    result = await endpoint("GET", "/api/mimo/providers")(request=request())
    provider = result["providers"][0]

    assert [(method["index"], method["capability"]) for method in provider["methods"]] == [
        (1, "device_code"),
        (2, "api_key"),
    ]
    assert provider["capabilities"]["device_code"] is True
    assert provider["capabilities"]["browser_callback"] is False


@pytest.mark.asyncio
async def test_list_marks_worker_owned_browser_callback(monkeypatch):
    async def fake_catalog(_supervisor):
        return catalog("gitlab", [{"type": "oauth", "label": "GitLab OAuth"}])

    monkeypatch.setattr(routes, "_catalog", fake_catalog)
    result = await endpoint("GET", "/api/mimo/providers")(request=request())
    provider = result["providers"][0]

    assert provider["methods"][0]["capability"] == "browser_callback"
    assert provider["capabilities"]["browser_callback"] is True


@pytest.mark.asyncio
async def test_catalogue_only_provider_without_auth_method_is_not_invented(monkeypatch):
    async def fake_catalog(_supervisor):
        return (
            {"all": [{"id": "zai", "name": "Z.AI"}], "connected": []},
            {},
        )

    monkeypatch.setattr(routes, "_catalog", fake_catalog)
    result = await endpoint("GET", "/api/mimo/providers")(request=request())
    assert result["providers"] == []


@pytest.mark.asyncio
async def test_internal_mimo_provider_is_merged_into_xiaomi_language(monkeypatch):
    async def fake_catalog(_supervisor):
        return (
            {
                "all": [
                    {"id": "mimo", "name": "mimo"},
                    {"id": "xiaomi", "name": "Xiaomi"},
                ],
                "connected": ["mimo"],
            },
            {"xiaomi": [{"type": "oauth", "label": "Browser login"}]},
        )

    monkeypatch.setattr(routes, "_catalog", fake_catalog)
    monkeypatch.setattr(
        "routes.model_routes._mimo_provider_breakdown",
        lambda _supervisor, _owner: [{
            "id": "xiaomi",
            "family": "MiMo",
            "models": 1,
            "chat_models": 1,
            "model_ids": ["xiaomi/mimo-auto"],
            "active": True,
            "served_by": None,
        }],
    )

    result = await endpoint("GET", "/api/mimo/providers")(request=request())

    assert [provider["id"] for provider in result["providers"]] == ["xiaomi"]
    assert result["providers"][0]["name"] == "Xiaomi"
    assert result["providers"][0]["family"] == "MiMo"
    assert result["providers"][0]["models"] == ["xiaomi/mimo-auto"]
    assert result["providers"][0]["connection_id"] == "mimo:xiaomi"
    assert result["providers"][0]["included_free_models"] == 1
    assert "MIMOCODE_HOME" not in result["storage"]


@pytest.mark.asyncio
async def test_api_key_is_owner_scoped_forwarded_once_and_never_echoed(monkeypatch):
    calls = []

    async def fake_catalog(_supervisor):
        return catalog()

    async def fake_native(_supervisor, method, path, body=None, **_kwargs):
        calls.append((method, path, body))
        return True

    monkeypatch.setattr(routes, "_catalog", fake_catalog)
    monkeypatch.setattr(routes, "_native", fake_native)
    result = await endpoint("PUT", "/{provider_id}/api-key")(
        provider_id="openai",
        payload=routes.ApiKeyCredential(key="do-not-echo"),
        request=request(user="bob", cookie="session-bob"),
    )
    assert calls == [("PUT", "/auth/openai", {"type": "api", "key": "do-not-echo"})]
    assert "do-not-echo" not in repr(result)


@pytest.mark.asyncio
async def test_native_disconnect_revokes_shared_workers_and_notifies_recipients(
    monkeypatch,
):
    suffix = uuid.uuid4().hex
    owner = f"owner-{suffix}"
    recipient = f"recipient-{suffix}"
    share_id = f"share-{suffix}"
    connection_id = f"connection-{suffix}"
    revoked = []
    notifications = []

    class Pool(Supervisor):
        async def for_owner(self, requested_owner):
            assert requested_owner == owner
            return self

        async def revoke_shared_access(self, requested_owner, requested_share):
            revoked.append((requested_owner, requested_share))

        async def refresh_endpoint_projection(self):
            return None

    class Notifications:
        def add_notification(self, task_name, status, task_id=None, **kwargs):
            notifications.append({
                "task_name": task_name,
                "status": status,
                "task_id": task_id,
                **kwargs,
            })

    connection = SimpleNamespace(id=connection_id, family_id="xiaomi")
    grant = SimpleNamespace(
        id=share_id,
        owner=owner,
        recipient=recipient,
        connection_id=connection_id,
        revision=3,
        model_selector={"mode": "explicit_models", "model_route_ids": ["route-1"]},
    )
    route_row = SimpleNamespace(id="route-1", provider_model_id="xiaomi/mimo-v2.5-pro")

    class ProviderStore:
        def __init__(self):
            self.expected_owner = owner

        def list_connections(self, *, owner):
            assert owner == self.expected_owner
            return [connection]

        def list_share_grants(self, *, owner):
            assert owner == self.expected_owner
            return [grant]

        def list_model_routes(self, *, owner, connection_id):
            assert owner == self.expected_owner
            assert connection_id == connection.id
            return [route_row]

        def revoke_share_grant(self, *, owner, grant_id, expected_revision):
            assert owner == self.expected_owner
            assert grant_id == share_id
            assert expected_revision == 3
            grant.state = "revoked"
            return grant

    monkeypatch.setattr("src.openclank.provider_store.ProviderStore", ProviderStore)

    async def fake_catalog(_supervisor):
        return catalog("xiaomi", [{"type": "oauth", "label": "Login"}])

    async def fake_native(_supervisor, method, path, body=None, **_kwargs):
        assert (method, path, body) == ("DELETE", "/auth/xiaomi", None)
        assert revoked == [(recipient, share_id)]
        return True

    monkeypatch.setattr(routes, "_catalog", fake_catalog)
    monkeypatch.setattr(routes, "_native", fake_native)
    monkeypatch.setattr(
        "src.event_bus.get_task_scheduler",
        lambda: Notifications(),
    )
    req = request(user=owner, cookie=f"session-{owner}")
    req.app.state.mimo_supervisor = Pool()
    result = await endpoint("DELETE", "/{provider_id}")(
        provider_id="xiaomi",
        request=req,
    )
    await asyncio.sleep(0)

    assert result["revoked_model_shares"] == 1
    assert revoked == [(recipient, share_id)]
    assert [note["owner"] for note in notifications] == [recipient]
    assert notifications[0]["kind"] == "model_share"
    assert "mimo-v2.5-pro" in notifications[0]["body"]
    assert "key" not in repr(notifications).lower()


@pytest.mark.asyncio
async def test_api_key_forwards_selected_method_prompt_inputs_as_metadata(monkeypatch):
    calls = []
    methods = [
        {"type": "oauth", "label": "Browser login"},
        {
            "type": "api",
            "label": "Gateway API key",
            "prompts": [
                {"type": "text", "key": "accountId", "message": "Account ID"},
                {"type": "text", "key": "gatewayId", "message": "Gateway ID"},
            ],
        },
    ]

    async def fake_catalog(_supervisor):
        return catalog("cloudflare-ai-gateway", methods)

    async def fake_native(_supervisor, method, path, body=None, **_kwargs):
        calls.append((method, path, body))
        return True

    monkeypatch.setattr(routes, "_catalog", fake_catalog)
    monkeypatch.setattr(routes, "_native", fake_native)
    await endpoint("PUT", "/{provider_id}/api-key")(
        provider_id="cloudflare-ai-gateway",
        payload=routes.ApiKeyCredential(
            key="gateway-secret",
            method=1,
            inputs={"accountId": "account-1", "gatewayId": "gateway-1"},
        ),
        request=request(),
    )

    assert calls == [(
        "PUT",
        "/auth/cloudflare-ai-gateway",
        {
            "type": "api",
            "key": "gateway-secret",
            "metadata": {
                "accountId": "account-1",
                "gatewayId": "gateway-1",
            },
        },
    )]


@pytest.mark.asyncio
async def test_api_key_rejects_inputs_not_declared_by_selected_method(monkeypatch):
    native_called = False
    methods = [{
        "type": "api",
        "label": "Workers API key",
        "prompts": [{"type": "text", "key": "accountId", "message": "Account ID"}],
    }]

    async def fake_catalog(_supervisor):
        return catalog("cloudflare-workers-ai", methods)

    async def fake_native(*_args, **_kwargs):
        nonlocal native_called
        native_called = True
        return True

    monkeypatch.setattr(routes, "_catalog", fake_catalog)
    monkeypatch.setattr(routes, "_native", fake_native)
    with pytest.raises(HTTPException) as invalid:
        await endpoint("PUT", "/{provider_id}/api-key")(
            provider_id="cloudflare-workers-ai",
            payload=routes.ApiKeyCredential(
                key="workers-secret",
                method=0,
                inputs={"gatewayId": "not-declared"},
            ),
            request=request(),
        )

    assert invalid.value.status_code == 400
    assert native_called is False


@pytest.mark.asyncio
async def test_oauth_rejects_wrong_method_and_provider_id(monkeypatch):
    async def fake_catalog(_supervisor):
        return catalog()

    monkeypatch.setattr(routes, "_catalog", fake_catalog)
    authorize = endpoint("POST", "/{provider_id}/oauth/authorize")
    with pytest.raises(HTTPException) as wrong_method:
        await authorize(
            provider_id="openai",
            payload=routes.OAuthStart(method=1),
            request=request(),
        )
    assert wrong_method.value.status_code == 400

    with pytest.raises(HTTPException) as invalid_id:
        await authorize(
            provider_id="http://attacker.invalid",
            payload=routes.OAuthStart(method=0),
            request=request(),
        )
    assert invalid_id.value.status_code == 400


@pytest.mark.asyncio
async def test_same_provider_flows_complete_out_of_order_and_reject_replay(monkeypatch):
    calls = []

    async def fake_catalog(_supervisor):
        return catalog("xiaomi", [{"type": "oauth", "label": "Browser login"}])

    async def fake_native(_supervisor, method, path, body=None, **_kwargs):
        calls.append((method, path, body))
        if path.endswith("/oauth/authorize"):
            return {
                "url": "https://platform.xiaomimimo.com/authorize",
                "method": "code",
                "instructions": "Paste the code",
            }
        return True

    monkeypatch.setattr(routes, "_catalog", fake_catalog)
    monkeypatch.setattr(routes, "_native", fake_native)
    authorize = endpoint("POST", "/{provider_id}/oauth/authorize")
    finish = endpoint("POST", "/{provider_id}/oauth/callback")
    req = request()
    first = await authorize(provider_id="xiaomi", payload=routes.OAuthStart(method=0), request=req)
    second = await authorize(provider_id="xiaomi", payload=routes.OAuthStart(method=0), request=req)
    assert first["flow_id"] != second["flow_id"]
    authorize_bodies = [body for method, path, body in calls if path.endswith("/oauth/authorize")]
    assert [body["flowID"] for body in authorize_bodies] == [first["flow_id"], second["flow_id"]]

    await finish(
        provider_id="xiaomi",
        payload=routes.OAuthCallback(flow_id=second["flow_id"], code="second-code"),
        request=req,
    )
    await finish(
        provider_id="xiaomi",
        payload=routes.OAuthCallback(flow_id=first["flow_id"], code="first-code"),
        request=req,
    )
    callback_bodies = [body for method, path, body in calls if path.endswith("/oauth/callback")]
    assert [(body["flowID"], body["code"]) for body in callback_bodies] == [
        (second["flow_id"], "second-code"),
        (first["flow_id"], "first-code"),
    ]

    with pytest.raises(HTTPException) as replay:
        await finish(
            provider_id="xiaomi",
            payload=routes.OAuthCallback(flow_id=second["flow_id"], code="replay"),
            request=req,
        )
    assert replay.value.status_code == 409


@pytest.mark.asyncio
async def test_flow_access_is_bound_to_owner_browser_and_provider(monkeypatch):
    callback_calls = 0

    async def fake_catalog(_supervisor):
        return catalog("xiaomi", [{"type": "oauth", "label": "Browser login"}])

    async def fake_native(_supervisor, method, path, body=None, **_kwargs):
        nonlocal callback_calls
        if path.endswith("/oauth/authorize"):
            return {"url": "https://example.test/login", "method": "code", "instructions": ""}
        if path.endswith("/oauth/callback"):
            callback_calls += 1
        return True

    monkeypatch.setattr(routes, "_catalog", fake_catalog)
    monkeypatch.setattr(routes, "_native", fake_native)
    authorize = endpoint("POST", "/{provider_id}/oauth/authorize")
    finish = endpoint("POST", "/{provider_id}/oauth/callback")
    started = await authorize(
        provider_id="xiaomi",
        payload=routes.OAuthStart(method=0),
        request=request(user="alice", cookie="alice-browser"),
    )
    payload = routes.OAuthCallback(flow_id=started["flow_id"], code="secret-code")
    attempts = [
        ("xiaomi", request(user="bob", cookie="bob-browser")),
        ("xiaomi", request(user="alice", cookie="other-alice-browser")),
        ("openai", request(user="alice", cookie="alice-browser")),
    ]
    for provider_id, req in attempts:
        with pytest.raises(HTTPException) as denied:
            await finish(provider_id=provider_id, payload=payload, request=req)
        assert denied.value.status_code == 404
    assert callback_calls == 0


@pytest.mark.asyncio
async def test_expired_flow_is_terminal(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(routes, "_oauth_flows", routes._OAuthFlowStore(time_func=lambda: clock[0]))

    async def fake_catalog(_supervisor):
        return catalog("xiaomi", [{"type": "oauth", "label": "Browser login"}])

    async def fake_native(_supervisor, method, path, body=None, **_kwargs):
        if path.endswith("/oauth/authorize"):
            return {"url": "https://example.test/login", "method": "code", "instructions": ""}
        raise AssertionError("expired flow reached native callback")

    monkeypatch.setattr(routes, "_catalog", fake_catalog)
    monkeypatch.setattr(routes, "_native", fake_native)
    started = await endpoint("POST", "/{provider_id}/oauth/authorize")(
        provider_id="xiaomi",
        payload=routes.OAuthStart(method=0),
        request=request(),
    )
    clock[0] += routes._FLOW_TTL_SECONDS + 1
    with pytest.raises(HTTPException) as expired:
        await endpoint("POST", "/{provider_id}/oauth/callback")(
            provider_id="xiaomi",
            payload=routes.OAuthCallback(flow_id=started["flow_id"], code="too-late"),
            request=request(),
        )
    assert expired.value.status_code == 410


@pytest.mark.asyncio
async def test_stable_configured_redirect_ignores_forwarded_host_and_state_is_one_use(monkeypatch):
    calls = []
    monkeypatch.setenv("APP_PUBLIC_URL", "https://buildweek.openclank.dev/")
    monkeypatch.setitem(routes._AUTH_CAPABILITIES, "synthetic", {0: "redirect"})

    async def fake_catalog(_supervisor):
        return catalog("synthetic", [{"type": "oauth", "label": "Sign in"}])

    async def fake_native(_supervisor, method, path, body=None, **_kwargs):
        calls.append((method, path, body))
        if path.endswith("/oauth/authorize"):
            return {"url": "https://id.example.test/authorize", "method": "code", "instructions": ""}
        return True

    monkeypatch.setattr(routes, "_catalog", fake_catalog)
    monkeypatch.setattr(routes, "_native", fake_native)
    req = request(
        headers={
            "host": "attacker.invalid",
            "x-forwarded-host": "attacker.invalid",
            "x-forwarded-proto": "http",
        },
    )
    started = await endpoint("POST", "/{provider_id}/oauth/authorize")(
        provider_id="synthetic",
        payload=routes.OAuthStart(method=0),
        request=req,
    )
    authorize_body = calls[0][2]
    assert authorize_body["redirectURI"] == "https://buildweek.openclank.dev/api/mimo/providers/oauth/callback"
    assert "attacker.invalid" not in repr(authorize_body)
    assert authorize_body["flowID"] == started["flow_id"]
    assert started["flow_id"] in authorize_body["state"]
    assert "alice" not in authorize_body["state"]
    assert "session-alice" not in authorize_body["state"]

    redirect = endpoint("GET", "/api/mimo/providers/oauth/callback")
    tampered = authorize_body["state"][:-1] + ("A" if authorize_body["state"][-1] != "A" else "B")
    with pytest.raises(HTTPException) as bad_state:
        await redirect(request=req, state=tampered, code="secret-code")
    assert bad_state.value.status_code == 400

    response = await redirect(request=req, state=authorize_body["state"], code="secret-code")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert "secret-code" not in response.body.decode()

    with pytest.raises(HTTPException) as replay:
        await redirect(request=req, state=authorize_body["state"], code="replay")
    assert replay.value.status_code == 409


@pytest.mark.asyncio
async def test_redirect_error_page_does_not_echo_provider_error(monkeypatch):
    monkeypatch.setenv("APP_PUBLIC_URL", "https://buildweek.openclank.dev")
    monkeypatch.setitem(routes._AUTH_CAPABILITIES, "synthetic", {0: "redirect"})
    seen = {}

    async def fake_catalog(_supervisor):
        return catalog("synthetic", [{"type": "oauth", "label": "Sign in"}])

    async def fake_native(_supervisor, method, path, body=None, **_kwargs):
        if path.endswith("/oauth/authorize"):
            seen.update(body)
            return {"url": "https://id.example.test/authorize", "method": "code", "instructions": ""}
        raise AssertionError("provider denial reached token exchange")

    monkeypatch.setattr(routes, "_catalog", fake_catalog)
    monkeypatch.setattr(routes, "_native", fake_native)
    req = request()
    await endpoint("POST", "/{provider_id}/oauth/authorize")(
        provider_id="synthetic",
        payload=routes.OAuthStart(method=0),
        request=req,
    )
    response = await endpoint("GET", "/api/mimo/providers/oauth/callback")(
        request=req,
        state=seen["state"],
        error="provider-secret-description",
    )
    body = response.body.decode()
    assert response.status_code == 400
    assert "provider-secret-description" not in body
    assert "denied this login" in body


@pytest.mark.asyncio
async def test_device_flow_cancel_stops_poll_and_late_finish_cannot_win(monkeypatch):
    callback_started = asyncio.Event()
    callback_cancelled = asyncio.Event()

    async def fake_catalog(_supervisor):
        return catalog("openai", [{"type": "oauth", "label": "Device login"}, {"type": "api", "label": "API key"}])

    async def fake_native(_supervisor, method, path, body=None, **_kwargs):
        if path.endswith("/oauth/authorize"):
            return {
                "url": "https://auth.openai.com/codex/device",
                "method": "auto",
                "instructions": "Enter code: TEST-CODE",
            }
        if method == "DELETE":
            return True
        if path.endswith("/oauth/callback"):
            callback_started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                callback_cancelled.set()
                raise
        return True

    monkeypatch.setattr(routes, "_catalog", fake_catalog)
    monkeypatch.setattr(routes, "_native", fake_native)
    req = request()
    started = await endpoint("POST", "/{provider_id}/oauth/authorize")(
        provider_id="openai",
        payload=routes.OAuthStart(method=0),
        request=req,
    )
    await asyncio.wait_for(callback_started.wait(), timeout=1)
    result = await endpoint("POST", "/api/mimo/providers/oauth/cancel")(
        payload=routes.OAuthFlowAction(flow_id=started["flow_id"]),
        request=req,
    )
    assert result == {"status": "cancelled"}
    await asyncio.wait_for(callback_cancelled.wait(), timeout=1)
    routes._oauth_flows.finish(started["flow_id"], connected=True)
    status = await endpoint("GET", "/api/mimo/providers/oauth/status")(
        flow_id=started["flow_id"],
        request=req,
    )
    assert status == {"status": "cancelled"}


@pytest.mark.asyncio
async def test_owner_flow_purge_drains_callback_task_when_native_cancel_fails(
    monkeypatch,
    fresh_flow_store,
):
    drained = asyncio.Event()

    async def pending_callback():
        try:
            await asyncio.Future()
        finally:
            drained.set()

    flow = routes._OAuthFlow(
        flow_id="flow_identifier_123456",
        provider_id="xiaomi",
        method=0,
        capability="paste_code",
        owner="alice",
        browser_session="session-alice",
        state="state",
        expires_at=fresh_flow_store.now() + 600,
        status="processing",
    )
    task = asyncio.create_task(pending_callback())
    await asyncio.sleep(0)
    fresh_flow_store.add(flow)
    fresh_flow_store.attach_task(flow.flow_id, task)

    async def failed_native(*_args, **_kwargs):
        raise HTTPException(502, "cancel failed")

    monkeypatch.setattr(routes, "_native", failed_native)
    with pytest.raises(HTTPException):
        await routes.purge_owner_provider_flows(Supervisor(), "alice")

    assert task.done()
    assert drained.is_set()
    assert flow.status == "cancelled"
