"""Cross-user regressions for normalized model-route projections."""

import asyncio
from types import SimpleNamespace

from services.stt.stt_service import STTService
from services.tts.tts_service import TTSService
from src.agent_tools.admin_tools import do_manage_endpoints


def test_agent_endpoint_tool_lists_only_callers_normalized_routes(monkeypatch):
    import src.openclank.chat_routing as chat_routing

    alice = SimpleNamespace(
        public_endpoint_id="conn-alice",
        connection_label="Alice provider",
        shared=False,
        provider_model_id="alice-model",
        model_route_id="pmr-alice",
    )
    shared = SimpleNamespace(
        public_endpoint_id="share:grant-1",
        connection_label="Shared provider",
        shared=True,
        provider_model_id="shared-model",
        model_route_id="pmr-shared",
    )
    monkeypatch.setattr(
        chat_routing,
        "list_chat_routes",
        lambda owner: ([alice], [shared]) if owner == "alice" else ([], []),
    )

    result = asyncio.run(do_manage_endpoints('{"action":"list"}', owner="alice"))
    assert [item["id"] for item in result["endpoints"]] == [
        "conn-alice",
        "share:grant-1",
    ]
    assert result["endpoints"][0]["models"] == [
        {"id": "alice-model", "route_id": "pmr-alice"}
    ]

    outsider = asyncio.run(do_manage_endpoints('{"action":"list"}', owner="bob"))
    assert outsider["endpoints"] == []


def test_agent_endpoint_tool_refuses_provider_mutation(monkeypatch):
    import src.openclank.chat_routing as chat_routing

    monkeypatch.setattr(chat_routing, "list_chat_routes", lambda owner: ([], []))
    denied = asyncio.run(
        do_manage_endpoints(
            '{"action":"delete","endpoint_id":"conn-alice"}',
            owner="alice",
        )
    )
    assert denied["exit_code"] == 1
    assert "Providers interface" in denied["error"]


def test_speech_services_fail_closed_for_retired_endpoint_selectors(tmp_path, monkeypatch):
    tts = TTSService(cache_dir=str(tmp_path / "tts"))
    stt = STTService()
    monkeypatch.setattr(
        tts,
        "_load_settings",
        lambda owner=None: {
            "tts_enabled": True,
            "tts_provider": "endpoint:bob-ep",
            "tts_model": "tts-1",
            "tts_voice": "alloy",
            "tts_speed": "1",
        },
    )
    monkeypatch.setattr(
        stt,
        "_load_settings",
        lambda owner=None: {
            "stt_enabled": True,
            "stt_provider": "endpoint:bob-ep",
            "stt_model": "whisper-1",
            "stt_language": "",
        },
    )

    assert tts.is_available("alice") is False
    assert tts.synthesize("private", use_cache=False, owner="alice") is None
    assert stt.is_available("alice") is False
    assert stt.transcribe(b"private", owner="alice") is None
