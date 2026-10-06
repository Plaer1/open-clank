import importlib.util
import inspect
import ipaddress
import sys
import types
from pathlib import Path

import pytest


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:8000/v1",
        "http://localhost:8000/v1",
        "http://10.0.0.5/v1",
        "http://172.16.0.1/v1",
        "http://192.168.1.2/v1",
        "http://169.254.169.254/latest/meta-data/",
        "http://metadata.google.internal/",
        "http://[::1]:8000/v1",
        "http://[fc00::1]/v1",
        "http://224.0.0.1/v1",
        "http://0.0.0.0/v1",
        "file:///etc/passwd",
    ],
)
def test_public_url_validator_blocks_internal_targets(url):
    from src.url_security import is_public_http_url

    assert is_public_http_url(url) is False


def test_public_url_validator_allows_public_endpoint(monkeypatch):
    from src import url_security

    monkeypatch.setattr(
        url_security,
        "_resolve_hostname_ips",
        lambda host: [ipaddress.ip_address("93.184.216.34")],
    )
    assert (
        url_security.validate_public_http_url("https://api.example.com/v1")
        == "https://api.example.com/v1"
    )


def test_public_url_validator_blocks_dns_to_private(monkeypatch):
    from src import url_security

    monkeypatch.setattr(
        url_security,
        "_resolve_hostname_ips",
        lambda host: [ipaddress.ip_address("10.0.0.5")],
    )
    with pytest.raises(ValueError):
        url_security.validate_public_http_url("https://api.example.com/v1")


class _ChatMessage:
    def __init__(self, role, content):
        self.role = role
        self.content = content


class _ChatSession:
    def __init__(self, *, owner, endpoint_url, endpoint_id, model, route_id):
        self.owner = owner
        self.endpoint_url = endpoint_url
        self.endpoint_id = endpoint_id
        self.model = model
        self.provider_model_route_id = route_id
        self.headers = {}
        self.history = []

    def add_message(self, message):
        self.history.append(message)


class _SessionManager:
    def __init__(self):
        self.created = []
        self.save_calls = 0

    def create_session(self, **values):
        self.created.append(values)
        return _ChatSession(
            owner=values["owner"],
            endpoint_url=values["endpoint_url"],
            endpoint_id=values["endpoint_id"],
            model=values["model"],
            route_id=values["provider_model_route_id"],
        )

    def save_sessions(self):
        self.save_calls += 1


class _Request:
    def __init__(self, *, owner="alice"):
        self.state = types.SimpleNamespace(
            api_token=True,
            api_token_scopes=["chat"],
            api_token_owner=owner,
        )


class _WebhookManager:
    def __init__(self):
        self.events = []

    def fire_and_forget(self, event, payload):
        self.events.append((event, payload))


def _load_webhook_routes_for_test(monkeypatch):
    core_pkg = types.ModuleType("core")
    core_pkg.__path__ = []
    core_db = types.ModuleType("core.database")
    core_db.SessionLocal = object
    core_db.Webhook = object
    core_models = types.ModuleType("core.models")
    core_models.ChatMessage = _ChatMessage
    core_middleware = types.ModuleType("core.middleware")
    core_middleware.require_admin = lambda request: None
    webhook_manager = types.ModuleType("src.webhook_manager")
    webhook_manager.WebhookManager = object
    webhook_manager.validate_webhook_url = lambda url: url
    webhook_manager.validate_events = lambda events: events

    monkeypatch.setitem(sys.modules, "core", core_pkg)
    monkeypatch.setitem(sys.modules, "core.database", core_db)
    monkeypatch.setitem(sys.modules, "core.models", core_models)
    monkeypatch.setitem(sys.modules, "core.middleware", core_middleware)
    monkeypatch.setitem(sys.modules, "src.webhook_manager", webhook_manager)

    module_name = "routes.webhook_routes_under_test"
    spec = importlib.util.spec_from_file_location(
        module_name,
        Path(__file__).resolve().parent.parent / "routes" / "webhook" / "webhook_routes.py",
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _sync_chat_endpoint(webhook_routes, session_manager, webhook_manager=None):
    router = webhook_routes.setup_webhook_routes(
        webhook_manager or _WebhookManager(),
        auth_manager=None,
        session_manager=session_manager,
    )
    return next(route.endpoint for route in router.routes if route.path == "/api/v1/chat")


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("api_key", "direct-secret"),
        ("base_url", "https://api.example.com/v1"),
        ("provider", "openai"),
    ],
)
@pytest.mark.asyncio
async def test_sync_chat_rejects_direct_provider_authority(monkeypatch, field, value):
    webhook_routes = _load_webhook_routes_for_test(monkeypatch)
    sessions = _SessionManager()
    endpoint = _sync_chat_endpoint(webhook_routes, sessions)
    values = dict(
        message="hello",
        model=None,
        session=None,
        endpoint_id=None,
        model_route_id=None,
        api_key=None,
        base_url=None,
        provider=None,
    )
    values[field] = value

    with pytest.raises(webhook_routes.HTTPException) as exc:
        await endpoint(_Request(), types.SimpleNamespace(**values))

    assert exc.value.status_code == 400
    assert "/api/v1/providers" in exc.value.detail
    assert sessions.created == []


@pytest.mark.asyncio
async def test_sync_chat_uses_normalized_route_and_managed_completion(monkeypatch):
    webhook_routes = _load_webhook_routes_for_test(monkeypatch)
    route = types.SimpleNamespace(
        model_route_id="route-chat-1",
        provider_model_id="model-a",
        public_endpoint_id="connection-a",
        provider_grant_id="grant-a",
    )
    calls = []

    chat_routing = types.ModuleType("src.openclank.chat_routing")
    chat_routing.ChatRouteUnavailable = type("ChatRouteUnavailable", (Exception,), {})
    chat_routing.MANAGED_ENGINE_PUBLIC_URL = "openclank://engine"
    chat_routing.list_chat_routes = lambda owner: ([route], [])
    chat_routing.normalized_provider_owner = lambda owner: owner
    chat_routing.resolve_chat_route = lambda **kwargs: route

    facade = types.ModuleType("src.openclank.modality_facade")

    async def complete_text(**kwargs):
        calls.append(kwargs)
        return "managed response"

    facade.complete_text = complete_text
    provider_store = types.ModuleType("src.openclank.provider_store")
    provider_store.ProviderStore = type(
        "ProviderStore",
        (),
        {"list_route_bindings": lambda self, owner: []},
    )
    monkeypatch.setitem(sys.modules, "src.openclank.chat_routing", chat_routing)
    monkeypatch.setitem(sys.modules, "src.openclank.modality_facade", facade)
    monkeypatch.setitem(sys.modules, "src.openclank.provider_store", provider_store)

    sessions = _SessionManager()
    events = _WebhookManager()
    endpoint = _sync_chat_endpoint(webhook_routes, sessions, events)
    response = await endpoint(
        _Request(),
        types.SimpleNamespace(
            message="hello",
            model="model-a",
            session=None,
            endpoint_id="connection-a",
            model_route_id="route-chat-1",
            api_key=None,
            base_url=None,
            provider=None,
        ),
    )

    assert response["response"] == "managed response"
    assert sessions.created[0]["endpoint_url"] == "openclank://engine"
    assert sessions.created[0]["provider_model_route_id"] == "route-chat-1"
    assert calls[0]["owner"] == "alice"
    assert calls[0]["purpose"] == "chat"
    assert calls[0]["model_route_id"] == "route-chat-1"
    assert calls[0]["grant_id"] == "grant-a"
    assert events.events[0][0] == "chat.completed"


def test_sync_chat_source_has_no_legacy_endpoint_execution(monkeypatch):
    webhook_routes = _load_webhook_routes_for_test(monkeypatch)
    source = inspect.getsource(webhook_routes.setup_webhook_routes)
    assert "ModelEndpoint" not in source
    assert "llm_call_async" not in source
    assert "complete_text(" in source
    assert "list_chat_routes(token_owner)" in source
