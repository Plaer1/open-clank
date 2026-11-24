"""Speech routes must dispatch through the managed operation router."""

from types import SimpleNamespace

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

import routes.stt_routes as stt_routes
import routes.tts_routes as tts_routes
from src.openclank.operation_router import ManagedOperationResult


def _result(operation: str, output=None):
    return ManagedOperationResult(
        operation_id="op_test",
        root_operation_id="root_test",
        operation=operation,
        state="complete",
        committed=True,
        replayed=False,
        model_route_id="pmr_speech",
        connection_id="pcn_speech",
        billing_lane="metered_api",
        output=output or {},
        artifacts=(),
    )


class _TTS:
    def __init__(self, tmp_path):
        self.cache_dir = tmp_path
        self.legacy_calls = 0

    def _load_settings(self, owner):
        return {
            "tts_enabled": True,
            "tts_provider": "managed",
            "tts_voice": "alloy",
            "tts_speed": "1",
        }

    def synthesize(self, *args, **kwargs):
        self.legacy_calls += 1
        raise AssertionError("legacy TTS transport was called")

    def clear_cache(self):
        return None


class _STT:
    def __init__(self):
        self.legacy_calls = 0

    def _load_settings(self, owner):
        return {
            "stt_enabled": True,
            "stt_provider": "managed",
            "stt_language": "en",
        }

    def transcribe(self, *args, **kwargs):
        self.legacy_calls += 1
        raise AssertionError("legacy STT transport was called")


def _client(tmp_path):
    tts = _TTS(tmp_path)
    stt = _STT()
    app = FastAPI()
    app.state.auth_manager = SimpleNamespace(users={"alice": {}})

    @app.middleware("http")
    async def identity(request: Request, call_next):
        request.state.current_user = "alice"
        request.state.authenticated = True
        if request.headers.get("authorization"):
            request.state.api_token = True
            request.state.api_token_owner = "alice"
            request.state.api_token_scopes = request.headers.get("x-scopes", "").split(",")
            request.state.api_token_client_kind = request.headers.get("x-client-kind", "api")
        else:
            request.state.api_token = False
        return await call_next(request)

    app.include_router(tts_routes.setup_tts_routes(tts))
    app.include_router(stt_routes.setup_stt_routes(stt))
    return TestClient(app), tts, stt


def _route(**_kwargs):
    return {
        "model_route_id": "pmr_speech",
        "model_id": "speech-test",
        "model_name": "Speech Test",
        "connection_id": "pcn_speech",
    }


def _local_route(**_kwargs):
    return {
        **_route(**_kwargs),
        "model_id": "kokoro/Kokoro-82M",
        "family_id": "local-executor",
        "adapter_id": "openclank-local-executor",
        "billing_lane": "local",
    }


def test_tts_dispatches_through_managed_router(monkeypatch, tmp_path):
    client, service, _ = _client(tmp_path)
    calls = []

    async def synthesize(**kwargs):
        calls.append(kwargs)
        return b"ID3managed", "audio/mpeg", _result("audio.synthesize")

    monkeypatch.setattr(tts_routes, "managed_route_summary", _route)
    monkeypatch.setattr(tts_routes, "synthesize_audio", synthesize)
    response = client.post("/api/tts/synthesize", json={"text": "hello"})

    assert response.status_code == 200
    assert response.content == b"ID3managed"
    assert calls[0]["owner"] == "alice"
    assert calls[0]["model_route_id"] == "pmr_speech"
    assert service.legacy_calls == 0


def test_stt_dispatches_through_managed_router(monkeypatch, tmp_path):
    client, _, service = _client(tmp_path)
    calls = []

    async def transcribe(**kwargs):
        calls.append(kwargs)
        return _result("audio.transcribe", {"text": "managed transcript"})

    monkeypatch.setattr(stt_routes, "managed_route_summary", _route)
    monkeypatch.setattr(stt_routes, "managed_transcribe_audio", transcribe)
    response = client.post(
        "/api/stt/transcribe",
        files={"file": ("clip.webm", b"audio", "audio/webm")},
    )

    assert response.status_code == 200
    assert response.json() == {"text": "managed transcript"}
    assert calls[0]["owner"] == "alice"
    assert calls[0]["media_type"] == "audio/webm"
    assert service.legacy_calls == 0


def test_speech_bearers_require_chat_scope_and_reject_tui_tokens(monkeypatch, tmp_path):
    client, _, _ = _client(tmp_path)
    monkeypatch.setattr(tts_routes, "managed_route_summary", _route)

    missing = client.get(
        "/api/tts/stats",
        headers={"Authorization": "Bearer test", "x-scopes": "providers:read"},
    )
    tui = client.get(
        "/api/tts/stats",
        headers={
            "Authorization": "Bearer test",
            "x-scopes": "chat",
            "x-client-kind": "tui",
        },
    )

    assert missing.status_code == 403
    assert tui.status_code == 403


def test_local_kokoro_uses_wav_and_fixed_defaults(monkeypatch, tmp_path):
    client, _, _ = _client(tmp_path)
    calls = []

    async def synthesize(**kwargs):
        calls.append(kwargs)
        return b"RIFFmanaged", "audio/wav", _result("audio.synthesize")

    monkeypatch.setattr(tts_routes, "managed_route_summary", _local_route)
    monkeypatch.setattr(tts_routes, "synthesize_audio", synthesize)
    response = client.post("/api/tts/synthesize", json={"text": "hello"})

    assert response.status_code == 200
    assert calls[0]["voice"] == "af_heart"
    assert calls[0]["speed"] == 1.0
    assert calls[0]["response_format"] == "wav"
    stats = client.get("/api/tts/stats")
    assert stats.json()["configured"] is True
    assert stats.json()["runtime_status"] == "unknown"
    assert stats.json()["ready"] is False


def test_local_kokoro_rejects_non_default_speed(monkeypatch, tmp_path):
    client, service, _ = _client(tmp_path)
    service._load_settings = lambda _owner: {
        "tts_enabled": True, "tts_provider": "managed", "tts_voice": "alloy", "tts_speed": "1.25",
    }
    monkeypatch.setattr(tts_routes, "managed_route_summary", _local_route)
    response = client.post("/api/tts/synthesize", json={"text": "hello"})
    assert response.status_code == 400
    assert "speed 1" in response.json()["detail"]["message"]


def test_kokoro_model_id_on_remote_route_keeps_remote_defaults(monkeypatch, tmp_path):
    client, _, _ = _client(tmp_path)
    calls = []

    async def synthesize(**kwargs):
        calls.append(kwargs)
        return b"ID3managed", "audio/mpeg", _result("audio.synthesize")

    monkeypatch.setattr(tts_routes, "managed_route_summary", lambda **_kwargs: {
        **_route(), "model_id": "kokoro/Kokoro-82M", "family_id": "openai",
        "adapter_id": "openai-responses", "billing_lane": "metered_api",
    })
    monkeypatch.setattr(tts_routes, "synthesize_audio", synthesize)
    response = client.post("/api/tts/synthesize", json={"text": "remote"})

    assert response.status_code == 200
    assert calls[0]["voice"] == "alloy"
    assert calls[0]["response_format"] == "mp3"
