"""Secret-free provider, share, and diagnostic projections for the TUI API."""

from datetime import datetime
from types import SimpleNamespace

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from core.provider_models import ProviderBase
from routes.tui_routes import setup_tui_routes
from src import secret_storage
from src.openclank.provider_store import ProviderStore


NOW = datetime(2026, 8, 9, 18, 0, 0)


class _Supervisor:
    def readiness(self):
        return {
            "ok": True,
            "event_loop": True,
            "owner_workers": 2,
            "share_workers": 1,
            "owners": {
                "alice": {"status": "ready", "generation": "generation-a"},
                "bob": {
                    "status": "failed",
                    "generation": "generation-b",
                    "last_failure": "must not cross tenant boundaries",
                },
            },
        }


@pytest.fixture()
def tui_provider_api(monkeypatch, tmp_path):
    monkeypatch.setattr(secret_storage, "_KEY_PATH", tmp_path / ".app_key")
    monkeypatch.setattr(secret_storage, "_fernet", None)
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    ProviderBase.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    store = ProviderStore(factory, clock=lambda: NOW)
    app = FastAPI()

    @app.middleware("http")
    async def identity(request: Request, call_next):
        request.state.api_token = True
        request.state.api_token_client_kind = request.headers.get(
            "x-test-client-kind",
            "tui",
        )
        request.state.api_token_owner = request.headers.get("x-test-owner", "alice")
        request.state.api_token_scopes = tuple(
            item.strip()
            for item in request.headers.get(
                "x-test-scopes",
                "tui:providers,tui:shares,tui:diagnostics",
            ).split(",")
            if item.strip()
        )
        return await call_next(request)

    app.state.engine_verification = SimpleNamespace(
        ok=True,
        version="1.2.3",
        target="linux-x64",
        source_sha256="f" * 64,
        errors=(),
    )
    app.state.mimo_supervisor = _Supervisor()
    app.include_router(setup_tui_routes(store))
    with TestClient(app) as client:
        yield client, store
    engine.dispose()


def _seed_pool(store: ProviderStore, owner: str, label: str):
    connection = store.create_connection(
        owner=owner,
        family_id="openai",
        adapter_id="openai-responses",
        kind="official",
        billing_lane="metered_api",
        label=label,
    )
    first = store.create_account(
        owner=owner,
        connection_id=connection.id,
        label="Primary",
        auth_method="api_key",
        auth_class="openai-api",
        credentials={"api_key": f"secret-{owner}-primary"},
        safe_identity={"provider_display_identity": f"{owner}@example.test"},
    )
    second = store.create_account(
        owner=owner,
        connection_id=connection.id,
        label="Backup",
        auth_method="api_key",
        auth_class="openai-api",
        credentials={"api_key": f"secret-{owner}-backup"},
        sort_order=10,
    )
    route = store.create_model_route(
        owner=owner,
        connection_id=connection.id,
        provider_model_id="gpt-test",
        display_name="GPT Test",
        operations=("chat.stream", "chat.complete"),
    )
    for account in (first, second):
        store.set_entitlement(
            owner=owner,
            account_id=account.id,
            model_route_id=route.id,
            eligible=True,
        )
    store.put_route_binding(
        owner=owner,
        purpose="chat",
        model_route_ids=[route.id],
        expected_revision=0,
    )
    return connection, first, second, route


def test_provider_pool_and_binding_projection_is_secret_free(tui_provider_api):
    client, store = tui_provider_api
    connection, first, second, route = _seed_pool(store, "alice", "Primary API")

    response = client.get("/api/tui/v1/providers")
    assert response.status_code == 200
    assert "secret-alice" not in response.text
    assert "credential_envelope" not in response.text
    assert "credential_fingerprint" not in response.text
    row = response.json()["connections"][0]
    assert row["id"] == connection.id
    assert row["account_count"] == 2
    assert row["healthy_count"] == 2
    assert [item["id"] for item in row["accounts"]] == [first.id, second.id]
    assert row["models"][0]["id"] == route.id

    bindings = client.get("/api/tui/v1/provider-bindings")
    assert bindings.status_code == 200
    assert bindings.json()["bindings"][0]["purpose"] == "chat"
    assert bindings.json()["bindings"][0]["routes"][0]["model_route_id"] == route.id


def test_use_next_is_revision_fenced_idempotent_and_scope_confined(tui_provider_api):
    client, store = tui_provider_api
    connection, _first, _second, route = _seed_pool(store, "alice", "Primary API")
    connection = store.get_connection(owner="alice", connection_id=connection.id)
    body = {
        "expected_revision": connection.revision,
        "model_route_id": route.id,
        "idempotency_key": "tui-next-account-0001",
    }
    first = client.post(
        f"/api/tui/v1/providers/{connection.id}/use-next",
        json=body,
    )
    assert first.status_code == 200
    assert first.json()["idempotency_replayed"] is False
    replay = client.post(
        f"/api/tui/v1/providers/{connection.id}/use-next",
        json=body,
    )
    assert replay.status_code == 200
    assert replay.json()["idempotency_replayed"] is True
    assert replay.json()["next_account"]["id"] == first.json()["next_account"]["id"]

    denied = client.get(
        "/api/tui/v1/providers",
        headers={"x-test-scopes": "tui:shares"},
    )
    assert denied.status_code == 403
    wrong_kind = client.get(
        "/api/tui/v1/providers",
        headers={"x-test-client-kind": "api"},
    )
    assert wrong_kind.status_code == 403


def test_received_share_is_immediately_active_and_has_no_accept_or_pin_api(tui_provider_api):
    client, store = tui_provider_api
    connection, first, second, route = _seed_pool(store, "bob", "Private source")
    grant = store.create_share_grant(
        owner="bob",
        recipient="alice",
        connection_id=connection.id,
        account_selector={
            "mode": "explicit_accounts",
            "account_ids": [first.id, second.id],
        },
        model_selector={
            "mode": "explicit_models",
            "model_route_ids": [route.id],
        },
        disclosure_fields=(),
        label="Shared GPT",
    )

    listing = client.get("/api/tui/v1/shares")
    assert listing.status_code == 200
    body = listing.json()["received"][0]
    assert body.get("accepted", True) is True
    assert first.id not in listing.text
    assert second.id not in listing.text
    assert "bob@example.test" not in listing.text
    assert body["accounts"][0]["slot_id"].startswith("account_")

    retired_accept = client.post(
        f"/api/tui/v1/shares/{grant.id}/accept",
        json={
            "expected_revision": grant.revision,
            "idempotency_key": "tui-accept-share-0001",
        },
    )
    assert retired_accept.status_code == 404

    retired_preference = client.put(
        f"/api/tui/v1/shares/{grant.id}/preferred-account",
        json={
            "expected_revision": grant.revision,
            "idempotency_key": "tui-prefer-share-0001",
            "account_slot_id": body["accounts"][0]["slot_id"],
        },
    )
    assert retired_preference.status_code == 404


def test_diagnostics_omit_other_owner_and_raw_failures(tui_provider_api):
    client, _store = tui_provider_api
    response = client.get("/api/tui/v1/diagnostics")
    assert response.status_code == 200
    body = response.json()
    assert body["ready"] is True
    assert body["engine"]["verified"] is True
    assert body["runtime"]["current_owner"]["status"] == "ready"
    assert "bob" not in response.text
    assert "must not cross tenant boundaries" not in response.text
