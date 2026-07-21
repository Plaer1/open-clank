"""MiMo-drives-agent slice 01: the endpoint registry projects into mimo.

Every visible endpoint/model pair becomes a mimo provider model.
Keys ride the credential dict (pipe FD at spawn), never config content.
"""
import json
import uuid

import pytest

import core.database as cdb
from core.database import ModelEndpoint
from src.openclank.mimo_supervisor import (
    ENDPOINT_PROVIDER_PREFIX,
    _endpoint_registry_providers,
)


def SessionLocal():
    # Late-bound like the projection code itself (`from core.database
    # import SessionLocal` at call time) so fixture writes always land in
    # the same database the code under test reads, even when another test
    # module rebinds core.database.SessionLocal to a temp DB.
    return cdb.SessionLocal()


@pytest.fixture
def endpoint_factory():
    created = []

    def _make(**overrides):
        capability = overrides.pop("capability", True)
        db = SessionLocal()
        try:
            row = ModelEndpoint(
                id=overrides.pop("id", f"test-{uuid.uuid4().hex[:8]}"),
                name=overrides.pop("name", "Test Endpoint"),
                base_url=overrides.pop("base_url", "https://api.example.test/v1"),
                api_key=overrides.pop("api_key", "sk-test-key"),
                is_enabled=overrides.pop("is_enabled", True),
                cached_models=overrides.pop("cached_models", json.dumps(["glm-5.2"])),
                **overrides,
            )
            db.add(row)
            db.flush()
            if capability is not None:
                from src.model_capabilities import declare_current_models
                declare_current_models(db, row, capability)
            db.commit()
            created.append(row.id)
            return row.id
        finally:
            db.close()

    yield _make
    db = SessionLocal()
    try:
        for ep_id in created:
            db.query(ModelEndpoint).filter(ModelEndpoint.id == ep_id).delete()
        db.commit()
    finally:
        db.close()


def test_enabled_endpoint_projects_with_models_and_credential(endpoint_factory):
    ep_id = endpoint_factory(
        cached_models=json.dumps(["glm-5.2", "glm-4.5"]),
        pinned_models=json.dumps(["glm-pinned"]),
        hidden_models=json.dumps(["glm-4.5"]),
    )
    config, credentials = _endpoint_registry_providers("")
    provider_id = f"{ENDPOINT_PROVIDER_PREFIX}{ep_id}"
    provider = config["provider"][provider_id]
    assert provider["npm"] == "@ai-sdk/openai-compatible"
    assert provider["api"].startswith("https://api.example.test")
    assert set(provider["models"]) == {"glm-5.2", "glm-pinned"}, (
        "cached + pinned − hidden, same list Settings shows"
    )
    assert provider["only_configured_models"] is True
    assert credentials[provider_id] == "sk-test-key"
    # The key must never appear anywhere in the config content itself.
    assert "sk-test-key" not in json.dumps(config)


def test_chatgpt_subscription_projects_responses_adapter_and_scoped_headers(
    monkeypatch, endpoint_factory,
):
    access_token = "x" * 2280
    monkeypatch.setattr(
        "src.chatgpt_subscription.extract_account_id",
        lambda _tokens: "account-test",
    )
    ep_id = endpoint_factory(
        base_url="https://chatgpt.com/backend-api/codex",
        api_key=access_token,
        cached_models=json.dumps(["gpt-5.6-luna"]),
    )

    config, credentials = _endpoint_registry_providers("")
    provider = config["provider"][f"{ENDPOINT_PROVIDER_PREFIX}{ep_id}"]

    assert provider["npm"] == "@ai-sdk/openai"
    assert provider["options"]["baseURL"] == "https://chatgpt.com/backend-api/codex"
    assert provider["options"]["headers"]["ChatGPT-Account-Id"] == "account-test"
    assert provider["models"]["gpt-5.6-luna"]["provider"]["npm"] == "@ai-sdk/openai"
    assert access_token not in json.dumps(config)
    assert credentials[f"{ENDPOINT_PROVIDER_PREFIX}{ep_id}"] == access_token


def test_skips_disabled_and_modelless_but_keeps_tools_off(endpoint_factory):
    disabled = endpoint_factory(is_enabled=False)
    toolless = endpoint_factory(capability=False)
    modelless = endpoint_factory(cached_models=None)
    config, _ = _endpoint_registry_providers("")
    providers = (config.get("provider") or {})
    assert f"{ENDPOINT_PROVIDER_PREFIX}{toolless}" in providers
    for ep_id in (disabled, modelless):
        assert f"{ENDPOINT_PROVIDER_PREFIX}{ep_id}" not in providers


def test_unknown_tool_support_defaults_on_and_projects(endpoint_factory):
    ep_id = endpoint_factory(capability=None)
    config, _ = _endpoint_registry_providers("")
    assert f"{ENDPOINT_PROVIDER_PREFIX}{ep_id}" in (config.get("provider") or {})


def test_owner_scoping_hides_foreign_private_endpoints(endpoint_factory):
    private = endpoint_factory(owner="mallory")
    shared = endpoint_factory(owner=None)
    config, _ = _endpoint_registry_providers("alice")
    providers = config["provider"]
    assert f"{ENDPOINT_PROVIDER_PREFIX}{shared}" in providers
    assert f"{ENDPOINT_PROVIDER_PREFIX}{private}" not in providers


def test_legacy_kill_switch_no_longer_changes_projection(monkeypatch, endpoint_factory):
    ep_id = endpoint_factory()
    monkeypatch.setenv("ODYSSEUS_PROJECT_ENDPOINTS", "0")
    config, _ = _endpoint_registry_providers("")
    assert f"{ENDPOINT_PROVIDER_PREFIX}{ep_id}" in config["provider"]


def test_mixed_model_capability_projects_independently(endpoint_factory):
    ep_id = endpoint_factory(capability=None, cached_models=json.dumps(["yes", "no", "unknown"]))
    db = SessionLocal()
    try:
        ep = db.get(ModelEndpoint, ep_id)
        from src.model_capabilities import set_declared
        set_declared(db, ep, "yes", True)
        set_declared(db, ep, "no", False)
        db.commit()
    finally:
        db.close()
    config, _ = _endpoint_registry_providers("")
    assert set(config["provider"][f"{ENDPOINT_PROVIDER_PREFIX}{ep_id}"]["models"]) == {
        "yes", "no", "unknown",
    }


def test_small_model_pick_skips_non_chat_models(monkeypatch):
    """Live failure 2026-07-18: with small_model unset, mimo's title agent
    picked xiaomi's TTS voicedesign model from our injected provider list and
    every title call 400'd. The projection pins the first chat-capable model."""
    from src.openclank.mimo_supervisor import _pick_small_model

    providers = {
        "xiaomi": {"models": {
            "mimo-v2.5-tts-voicedesign": {},
            "mimo-embed-large": {},
            "mimo-auto": {},
        }},
        "ody-abc": {"models": ["glm-5.2"]},
    }
    assert _pick_small_model(providers) == "xiaomi/mimo-auto"
    assert _pick_small_model({"x": {"models": {"a-tts": {}, "b-embed": {}}}}) is None
    monkeypatch.setenv("ODYSSEUS_SMALL_MODEL", "ody-abc/glm-5.2")
    assert _pick_small_model({}) == "ody-abc/glm-5.2"


def test_spawn_pins_small_model_and_print_logs():
    import inspect

    from src.openclank import mimo_supervisor as ms

    source = inspect.getsource(ms)
    assert 'config_content["small_model"] = small_model' in source
    assert '"--print-logs"' in source, "mimo log stream must fold into app.log"


def test_mutation_routes_schedule_reprojection():
    import pathlib

    source = pathlib.Path("routes/model_routes.py").read_text()
    assert source.count("_schedule_mimo_reprojection(request)") >= 4, (
        "create/patch-models/patch/delete must all recycle mimo workers"
    )
