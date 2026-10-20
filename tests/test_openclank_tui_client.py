from __future__ import annotations

import io
import json

import httpx
import pytest

from src.openclank.client_profiles import ClientProfile
from src.openclank.tui_app import OpenClankTui
from src.openclank.tui_client import OpenClankTuiClient, TuiClientError


def _profile():
    return ClientProfile.create("test", "https://clank.example.test")


def test_client_sends_tui_token_only_to_tui_api():
    seen = {}

    def handler(request):
        seen["path"] = request.url.path
        seen["authorization"] = request.headers.get("authorization")
        return httpx.Response(200, json={"principal": {"owner": "alice"}, "sessions": []})

    client = OpenClankTuiClient(
        _profile(), token="oct_secret", transport=httpx.MockTransport(handler)
    )
    try:
        assert client.bootstrap()["principal"]["owner"] == "alice"
    finally:
        client.close()
    assert seen == {"path": "/api/tui/v1/bootstrap", "authorization": "Bearer oct_secret"}


def test_public_info_does_not_send_token():
    def handler(request):
        assert "authorization" not in request.headers
        return httpx.Response(200, json={"product": "Open Clank"})

    with OpenClankTuiClient(
        _profile(), token="oct_secret", transport=httpx.MockTransport(handler)
    ) as client:
        assert client.info()["product"] == "Open Clank"


def test_device_flow_polls_pending_then_accepts_token():
    calls = {"token": 0}

    def handler(request):
        if request.url.path.endswith("/device/start"):
            return httpx.Response(
                200,
                json={
                    "device_code": "d" * 48,
                    "user_code": "ABCD-EFGH",
                    "verification_uri": "https://clank.example.test/api/tui/device",
                    "verification_uri_complete": "https://clank.example.test/api/tui/device?user_code=ABCD-EFGH",
                    "expires_in": 600,
                    "interval": 1,
                },
            )
        calls["token"] += 1
        if calls["token"] == 1:
            return httpx.Response(428, json={"detail": "Authorization pending"})
        return httpx.Response(200, json={"access_token": "oct_new", "scopes": []})

    with OpenClankTuiClient(_profile(), transport=httpx.MockTransport(handler)) as client:
        flow = client.start_device_authorization(device_label="Terminal")
        result = client.poll_device_authorization(flow, sleep=lambda _seconds: None)
        assert result["access_token"] == "oct_new"
        assert client.token == "oct_new"


def test_client_refuses_malformed_or_error_responses():
    with OpenClankTuiClient(
        _profile(), token="oct_bad", transport=httpx.MockTransport(
            lambda _request: httpx.Response(403, json={"detail": "wrong scope"})
        )
    ) as client:
        with pytest.raises(TuiClientError, match="wrong scope"):
            client.bootstrap()


class _FakeClient:
    profile = ClientProfile.create("local", "http://localhost:7777")

    def bootstrap(self):
        return {
            "principal": {"owner": "alice"},
            "sessions": [{"id": "s1", "name": "First", "model": "automatic"}],
        }

    def messages(self, session_id, *, limit=100):
        assert session_id == "s1"
        return {"items": [{"role": "user", "content": "hello from Open Clank"}]}

    def tasks(self, *, limit=100):
        return {"items": []}

    def stream_turn(self, session_id, message, *, idempotency_key=None):
        assert session_id == "s1"
        assert message == "say hello"
        yield {"delta": "Hello"}
        yield {"delta": " there"}


def test_tui_renders_canonical_sessions_without_engine_branding():
    output = io.StringIO()
    commands = iter(["open 1", "quit"])
    tui = OpenClankTui(_FakeClient(), input_fn=lambda _prompt: next(commands), output=output)
    assert tui.run() == 0
    rendered = output.getvalue()
    assert "OPEN CLANK" in rendered
    assert "hello from Open Clank" in rendered
    assert all(word not in rendered for word in ("MiMo", "Xiaomi", "OpenCode", "Odysseus"))


def test_tui_submits_and_renders_reconnectable_turn():
    output = io.StringIO()
    commands = iter(["send 1 say hello", "quit"])
    tui = OpenClankTui(_FakeClient(), input_fn=lambda _prompt: next(commands), output=output)
    assert tui.run() == 0
    assert "Hello there" in output.getvalue()
