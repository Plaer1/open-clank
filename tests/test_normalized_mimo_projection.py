"""Normalized provider rows are the sole, secret-free worker catalog."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import core.database as core_database
from core.provider_models import (
    ProviderAccount,
    ProviderAccountEntitlement,
    ProviderBase,
    ProviderConnection,
    ProviderModelRoute,
    ProviderRouteBinding,
    ProviderShareGrant,
)
from src.openclank.mimo_projection import (
    ProjectionConfigurationError,
    build_projection_snapshot,
    build_shared_projection_snapshot,
    reconcile_projection,
    safe_additive_delta,
)


@pytest.fixture()
def normalized_db(monkeypatch):
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    ProviderBase.metadata.create_all(bind=engine)
    factory = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    monkeypatch.setattr(core_database, "SessionLocal", factory)
    yield factory
    engine.dispose()


def _connection(
    db,
    connection_id: str,
    *,
    owner: str = "alice",
    family: str = "openai",
    adapter: str = "openai-responses",
    lane: str = "metered_api",
    kind: str = "official",
    url: str | None = None,
    settings: dict | None = None,
) -> ProviderConnection:
    row = ProviderConnection(
        id=connection_id,
        owner=owner,
        family_id=family,
        adapter_id=adapter,
        kind=kind,
        billing_lane=lane,
        label=f"Connection {connection_id}",
        normalized_url=url,
        settings=settings or {},
        enabled=True,
    )
    db.add(row)
    db.flush()
    db.add(
        ProviderAccount(
            id=f"pac_fixture_{connection_id}",
            connection_id=connection_id,
            owner=owner,
            label="Fixture account",
            auth_method="api_key",
            auth_class="metered",
            sort_order=0,
            enabled=True,
            credential_envelope="fixture-envelope",
            credential_fingerprint="a" * 64,
            credential_version=1,
            safe_identity={},
        )
    )
    db.flush()
    return row


def _route(
    db,
    connection: ProviderConnection,
    route_id: str,
    model_id: str,
    *,
    operations: list[str] | None = None,
    capabilities: dict | None = None,
) -> ProviderModelRoute:
    row = ProviderModelRoute(
        id=route_id,
        connection_id=connection.id,
        owner=connection.owner,
        provider_model_id=model_id,
        display_name=f"Model {model_id}",
        operations=operations or ["chat.stream", "chat.complete"],
        capabilities=capabilities or {},
        visibility="visible",
        provenance={},
        enabled=True,
    )
    db.add(row)
    db.flush()
    db.add(
        ProviderAccountEntitlement(
            account_id=f"pac_fixture_{connection.id}",
            model_route_id=route_id,
            eligible=True,
            evidence={"authority": "managed-engine", "fixture": True},
        )
    )
    db.flush()
    return row


def test_projection_uses_normalized_connection_ids_and_never_credentials(normalized_db):
    with normalized_db() as db:
        connection = _connection(
            db,
            "pcn_openai_api",
            owner="local-installation",
            family="openai-compatible",
            adapter="openai-responses",
            lane="custom",
            kind="custom_gateway",
            url="https://gateway.example.test/v1/",
            settings={
                "timeout": 12_000,
                "api_key": "must-not-project",
                "headers": {
                    "Authorization": "Bearer must-not-project",
                    "X-Safe": "catalog-only",
                },
            },
        )
        _route(
            db,
            connection,
            "pmr_chat",
            "gpt-test",
            capabilities={"reasoning": True, "attachment": True},
        )
        db.add(
            ProviderAccount(
                id="pac_secret",
                connection_id=connection.id,
                owner=connection.owner,
                label="Account",
                auth_method="api_key",
                auth_class="metered",
                sort_order=0,
                enabled=True,
                credential_envelope="encrypted-secret-envelope",
                credential_fingerprint="f" * 64,
                credential_version=1,
                safe_identity={},
            )
        )
        db.commit()

    snapshot = build_projection_snapshot("")
    assert snapshot.owner == "local-installation"
    assert set(snapshot.providers) == {"pcn_openai_api"}
    assert all(not provider_id.startswith("ody-") for provider_id in snapshot.providers)
    provider = snapshot.providers["pcn_openai_api"]
    assert provider["npm"] == "@ai-sdk/openai-compatible"
    assert provider["api"] == "https://gateway.example.test/v1"
    assert provider["options"]["baseURL"] == provider["api"]
    assert provider["options"]["_openclankAdapterID"] == "openai-responses"
    assert provider["options"]["timeout"] == 12_000
    assert provider["models"]["gpt-test"]["options"] == {
        "_openclankConnectionID": "pcn_openai_api",
        "_openclankModelRouteID": "pmr_chat",
        "_openclankFamilyID": "openai-compatible",
        "_openclankAdapterID": "openai-responses",
        "_openclankBillingLane": "custom",
        "_openclankOperations": ["chat.complete", "chat.stream"],
        "_openclankCatalogRevision": 1,
    }
    assert provider["models"]["gpt-test"]["reasoning"] is True
    assert snapshot.source_connections == {"pcn_openai_api": "pcn_openai_api"}
    assert snapshot.credential_digests == {}
    assert snapshot.credentials == {}
    assert snapshot.native_auth_digest is None
    wire = json.dumps(snapshot.providers, sort_keys=True)
    assert "must-not-project" not in wire
    assert "encrypted-secret-envelope" not in wire


def test_recipient_projection_contains_only_active_granted_topology(normalized_db):
    with normalized_db() as db:
        connection = _connection(
            db,
            "pcn_shared",
            owner="bob",
            family="deepseek",
            adapter="openai-chat",
            lane="metered_api",
        )
        connection.label = "Bob private endpoint label"
        route_a = _route(db, connection, "pmr_shared_a", "deepseek-a")
        _route(db, connection, "pmr_shared_b", "deepseek-b")
        db.add(
            ProviderShareGrant(
                id="psg_shared",
                owner="bob",
                recipient="alice",
                connection_id=connection.id,
                billing_lane=connection.billing_lane,
                label="private share label",
                account_selector={"mode": "all_live_accounts"},
                model_selector={
                    "mode": "explicit_models",
                    "model_route_ids": [route_a.id],
                },
                disclosure_fields=[],
                state="active",
                revision=1,
                accepted_revision=None,
            )
        )
        db.commit()

    active = build_projection_snapshot("alice")
    assert set(active.providers) == {"pcn_shared"}
    assert set(active.providers["pcn_shared"]["models"]) == {"deepseek-a"}
    assert active.providers["pcn_shared"]["name"] == "Shared deepseek"
    assert active.small_model is None
    wire = json.dumps(active.providers, sort_keys=True)
    assert "Bob private endpoint label" not in wire
    assert "private share label" not in wire
    assert "credential" not in wire.lower()

    with normalized_db() as db:
        grant = db.get(ProviderShareGrant, "psg_shared")
        grant.state = "revoked"
        db.commit()

    revoked = build_projection_snapshot("alice")
    assert revoked.providers == {}
    assert revoked.fingerprint != active.fingerprint


@pytest.mark.parametrize(
    ("family", "adapter", "expected_npm"),
    [
        ("anthropic", "anthropic-messages", "@ai-sdk/anthropic"),
        ("openai", "openai-responses", "@ai-sdk/openai"),
        ("openai", "openai-chat", "@ai-sdk/openai"),
        ("openrouter", "openai-chat", "@openrouter/ai-sdk-provider"),
        ("openai-compatible", "openai-chat", "@ai-sdk/openai-compatible"),
        ("openai-compatible", "openai-responses", "@ai-sdk/openai-compatible"),
        ("deepseek", "openai-chat", "@ai-sdk/openai-compatible"),
        ("github-copilot", "copilot-chat", "@ai-sdk/github-copilot"),
        ("xiaomi", "mimo-native", "@ai-sdk/openai-compatible"),
        ("google", "google-generative-ai", "@ai-sdk/google"),
        ("google", "google-vertex", "@ai-sdk/google-vertex"),
        ("xai", "xai-responses", "@ai-sdk/xai"),
        ("ollama", "ollama", "@ai-sdk/openai-compatible"),
        ("groq", "models-dev-openai-compatible", "@ai-sdk/openai-compatible"),
        ("fireworks-ai", "models-dev-anthropic", "@ai-sdk/anthropic"),
    ],
)
def test_adapter_metadata_matches_managed_engine(
    normalized_db,
    family,
    adapter,
    expected_npm,
):
    with normalized_db() as db:
        connection = _connection(
            db,
            "pcn_adapter",
            family=family,
            adapter=adapter,
            lane="local" if family == "ollama" else "metered_api",
            kind="local" if family == "ollama" else "official",
            url="http://127.0.0.1:11434/api" if family == "ollama" else "https://provider.example.test/v1",
        )
        _route(db, connection, "pmr_adapter", "model-a")
        db.commit()

    provider = build_projection_snapshot("alice").providers["pcn_adapter"]
    assert provider["npm"] == expected_npm
    assert provider["models"]["model-a"]["provider"]["npm"] == expected_npm
    if family == "ollama":
        assert provider["api"] == "http://127.0.0.1:11434/v1"


@pytest.mark.parametrize(
    (
        "family",
        "adapter",
        "kind",
        "lane",
        "url",
        "runtime_family",
        "runtime_adapter",
        "runtime_api",
    ),
    [
        (
            "compatible",
            "openai-compatible",
            "custom_gateway",
            "metered_api",
            "https://api.deepseek.com/v1",
            "openai-compatible",
            "openai-chat",
            "https://api.deepseek.com/v1",
        ),
        (
            "openai",
            "openai",
            "subscription",
            "subscription",
            None,
            "openai",
            "openai-responses",
            "https://chatgpt.com/backend-api/codex",
        ),
        (
            "deepseek",
            "deepseek",
            "official_api",
            "metered_api",
            None,
            "deepseek",
            "openai-chat",
            "https://api.deepseek.com",
        ),
    ],
)
def test_pre_release_migration_aliases_project_canonical_engine_metadata(
    normalized_db,
    family,
    adapter,
    kind,
    lane,
    url,
    runtime_family,
    runtime_adapter,
    runtime_api,
):
    with normalized_db() as db:
        connection = _connection(
            db,
            "pcn_migrated_alias",
            family=family,
            adapter=adapter,
            kind=kind,
            lane=lane,
            url=url,
        )
        _route(db, connection, "pmr_migrated_alias", "legacy-model")
        db.commit()

    provider = build_projection_snapshot("alice").providers["pcn_migrated_alias"]
    expected_npm = "@ai-sdk/openai-compatible" if runtime_family in {
        "deepseek",
        "openai-compatible",
    } else "@ai-sdk/openai"
    assert provider["npm"] == expected_npm
    assert provider["api"] == runtime_api
    assert provider["options"]["_openclankFamilyID"] == runtime_family
    assert provider["options"]["_openclankAdapterID"] == runtime_adapter
    model_options = provider["models"]["legacy-model"]["options"]
    assert model_options["_openclankFamilyID"] == runtime_family
    assert model_options["_openclankAdapterID"] == runtime_adapter


def test_utility_binding_pins_small_model_and_non_model_routes_do_not_leak(normalized_db):
    with normalized_db() as db:
        first = _connection(
            db,
            "pcn_a",
            family="openai",
            adapter="openai-responses",
        )
        second = _connection(
            db,
            "pcn_b",
            family="anthropic",
            adapter="anthropic-messages",
        )
        first_route = _route(db, first, "pmr_a", "gpt-a")
        second_route = _route(db, second, "pmr_b", "claude-b")
        local = _connection(
            db,
            "pcn_local_executor",
            family="local-executor",
            adapter="openclank-local-executor",
            lane="local",
            kind="local",
        )
        _route(
            db,
            local,
            "pmr_image",
            "diffusion-local",
            operations=["image.generate"],
        )
        db.add_all(
            [
                ProviderRouteBinding(
                    id="prb_chat",
                    owner="alice",
                    purpose="chat",
                    ordinal=0,
                    model_route_id=first_route.id,
                    enabled=True,
                ),
                ProviderRouteBinding(
                    id="prb_utility",
                    owner="alice",
                    purpose="utility",
                    ordinal=0,
                    model_route_id=second_route.id,
                    enabled=True,
                ),
                ProviderRouteBinding(
                    id="prb_memory",
                    owner="alice",
                    purpose="memory",
                    ordinal=0,
                    model_route_id=first_route.id,
                    enabled=True,
                ),
            ]
        )
        db.commit()

    snapshot = build_projection_snapshot("ALICE")
    assert snapshot.small_model == "pcn_b/claude-b"
    assert set(snapshot.providers) == {"pcn_a", "pcn_b"}
    assert "pcn_local_executor" not in snapshot.providers


def test_remote_non_chat_modalities_are_present_for_typed_engine_operations(normalized_db):
    with normalized_db() as db:
        connection = _connection(db, "pcn_modalities")
        _route(
            db,
            connection,
            "pmr_image",
            "image-test",
            operations=["image.generate", "image.edit"],
        )
        _route(
            db,
            connection,
            "pmr_speech",
            "speech-test",
            operations=["audio.synthesize", "audio.transcribe"],
        )
        _route(
            db,
            connection,
            "pmr_embed",
            "embed-test",
            operations=["embeddings.create"],
        )
        db.commit()

    snapshot = build_projection_snapshot("alice")
    assert set(snapshot.providers["pcn_modalities"]["models"]) == {
        "embed-test",
        "image-test",
        "speech-test",
    }
    assert snapshot.small_model is None


def test_projection_generation_is_deterministic_without_projection_state(normalized_db):
    with normalized_db() as db:
        connection = _connection(db, "pcn_generation")
        _route(db, connection, "pmr_generation", "gpt-a")
        db.commit()

    first = build_projection_snapshot("alice")
    second = build_projection_snapshot("alice")
    first_state = reconcile_projection(first, materializing=True)
    second_state = reconcile_projection(second, materializing=True)
    assert first.fingerprint == second.fingerprint
    assert first_state == second_state
    assert first_state["generation"] > 0
    assert first_state["desired_fingerprint"] == first.fingerprint[:12]

    with normalized_db() as db:
        route = db.get(ProviderModelRoute, "pmr_generation")
        route.display_name = "Renamed model"
        route.revision += 1
        db.commit()
    changed = build_projection_snapshot("alice")
    assert changed.fingerprint != first.fingerprint
    assert reconcile_projection(changed, materializing=True)["generation"] != first_state["generation"]
    assert safe_additive_delta(first, changed) is False


def test_unsupported_chat_adapter_fails_closed_but_modality_only_route_does_not(normalized_db):
    with normalized_db() as db:
        bad = _connection(
            db,
            "pcn_bad",
            family="local-executor",
            adapter="openclank-local-executor",
            lane="local",
            kind="local",
        )
        _route(db, bad, "pmr_bad", "invalid-chat")
        db.commit()
    with pytest.raises(ProjectionConfigurationError, match="unsupported managed adapter"):
        build_projection_snapshot("alice")

    with normalized_db() as db:
        route = db.get(ProviderModelRoute, "pmr_bad")
        route.operations = ["image.generate"]
        db.commit()
    snapshot = build_projection_snapshot("alice")
    assert snapshot.providers == {}
    assert snapshot.small_model is None


def test_legacy_shared_projection_is_secret_free_and_cannot_run(normalized_db):
    access = SimpleNamespace(
        actor_owner="alice",
        credential_owner="bob",
        share_id="legacy-share",
        revision=3,
        model_id="openai/gpt-secret",
    )
    snapshot = build_shared_projection_snapshot(access)
    assert snapshot.owner == "alice"
    assert snapshot.providers == {}
    assert snapshot.credentials == {}
    assert snapshot.credential_digests == {}
    assert snapshot.run_closure("openai", "gpt-secret") is None
