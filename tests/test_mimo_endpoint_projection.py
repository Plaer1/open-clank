"""MiMo-drives-agent slice 01: the endpoint registry projects into mimo.

Every enabled OpenAI-compatible ModelEndpoint becomes a mimo provider
(`ody-<endpoint_id>`) with the exact model list the Settings UI shows;
keys ride the credential dict (pipe FD at spawn), never the config
content. supports_tools=False, disabled, and model-less endpoints stay
out; owner scoping holds.
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


def test_skips_disabled_toolless_and_modelless_endpoints(endpoint_factory):
    disabled = endpoint_factory(is_enabled=False)
    toolless = endpoint_factory(supports_tools=False)
    modelless = endpoint_factory(cached_models=None)
    config, _ = _endpoint_registry_providers("")
    providers = (config.get("provider") or {})
    for ep_id in (disabled, toolless, modelless):
        assert f"{ENDPOINT_PROVIDER_PREFIX}{ep_id}" not in providers


def test_unknown_tool_support_still_projects(endpoint_factory):
    ep_id = endpoint_factory(supports_tools=None)
    config, _ = _endpoint_registry_providers("")
    assert f"{ENDPOINT_PROVIDER_PREFIX}{ep_id}" in config["provider"]


def test_owner_scoping_hides_foreign_private_endpoints(endpoint_factory):
    private = endpoint_factory(owner="mallory")
    shared = endpoint_factory(owner=None)
    config, _ = _endpoint_registry_providers("alice")
    providers = config["provider"]
    assert f"{ENDPOINT_PROVIDER_PREFIX}{shared}" in providers
    assert f"{ENDPOINT_PROVIDER_PREFIX}{private}" not in providers


def test_kill_switch(monkeypatch, endpoint_factory):
    endpoint_factory()
    monkeypatch.setenv("ODYSSEUS_PROJECT_ENDPOINTS", "0")
    config, credentials = _endpoint_registry_providers("")
    assert config == {} and credentials == {}


def test_mutation_routes_schedule_reprojection():
    import pathlib

    source = pathlib.Path("routes/model_routes.py").read_text()
    assert source.count("_schedule_mimo_reprojection(request)") >= 4, (
        "create/patch-models/patch/delete must all recycle mimo workers"
    )
