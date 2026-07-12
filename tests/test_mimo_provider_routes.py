from types import SimpleNamespace

import pytest
from fastapi import HTTPException

import routes.mimo_provider_routes as routes


class Supervisor:
    http_base_url = "http://127.0.0.1:32123"

    def is_alive(self):
        return True

    async def refresh_model_catalog(self):
        return []


def request(*, admin=True, base_url=None):
    supervisor = Supervisor()
    if base_url is not None:
        supervisor.http_base_url = base_url
    auth = SimpleNamespace(is_configured=True, is_admin=lambda _user: admin)
    return SimpleNamespace(
        headers={},
        state=SimpleNamespace(current_user="admin" if admin else "bob"),
        app=SimpleNamespace(state=SimpleNamespace(mimo_supervisor=supervisor, auth_manager=auth)),
    )


def endpoint(method, suffix):
    for route in routes.setup_mimo_provider_routes().routes:
        if method in (route.methods or set()) and route.path.endswith(suffix):
            return route.endpoint
    raise AssertionError(f"missing {method} {suffix}")


def catalog():
    return (
        {"all": [{"id": "openai", "name": "OpenAI", "models": {"secret": "ignored"}}], "connected": []},
        {"openai": [{"type": "oauth", "label": "ChatGPT Plus"}, {"type": "api", "label": "API key"}]},
    )


@pytest.mark.asyncio
async def test_provider_list_requires_admin(monkeypatch):
    monkeypatch.setenv("AUTH_ENABLED", "true")
    with pytest.raises(HTTPException) as exc:
        await endpoint("GET", "/api/mimo/providers")(request=request(admin=False))
    assert exc.value.status_code == 403


def test_supervisor_target_must_be_loopback(monkeypatch):
    monkeypatch.setenv("AUTH_ENABLED", "false")
    with pytest.raises(HTTPException) as exc:
        routes._supervisor(request(base_url="http://example.test:80"))
    assert exc.value.status_code == 503


@pytest.mark.asyncio
async def test_list_sanitizes_native_provider_payload(monkeypatch):
    monkeypatch.setenv("AUTH_ENABLED", "false")

    async def fake_catalog(_supervisor):
        return catalog()

    monkeypatch.setattr(routes, "_catalog", fake_catalog)
    result = await endpoint("GET", "/api/mimo/providers")(request=request())
    assert result["providers"] == [{
        "id": "openai",
        "name": "OpenAI",
        "connected": False,
        "methods": [
            {"index": 0, "type": "oauth", "label": "ChatGPT Plus"},
            {"index": 1, "type": "api", "label": "API key"},
        ],
    }]
    assert "models" not in result["providers"][0]


@pytest.mark.asyncio
async def test_api_key_is_forwarded_once_and_never_echoed(monkeypatch):
    monkeypatch.setenv("AUTH_ENABLED", "false")
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
        request=request(),
    )
    assert calls == [("PUT", "/auth/openai", {"type": "api", "key": "do-not-echo"})]
    assert "do-not-echo" not in repr(result)


@pytest.mark.asyncio
async def test_oauth_rejects_wrong_method_and_provider_id(monkeypatch):
    monkeypatch.setenv("AUTH_ENABLED", "false")

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
