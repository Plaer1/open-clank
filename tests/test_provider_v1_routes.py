"""Security and revision contracts for the normalized provider HTTP API."""

from datetime import datetime, timedelta

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
import pytest

from core.provider_models import (
    ProviderAccount,
    ProviderBase,
    ProviderIdempotencyRecord,
)
from routes.provider_v1_routes import setup_provider_v1_routes
from src import secret_storage
from src.openclank.provider_store import AccountSelector, ModelSelector, ProviderStore
from src.openclank.provider_control import OAuthHostFlow, OAuthHostFlowStore


NOW = datetime(2026, 8, 9, 15, 0, 0)


def test_oauth_flow_store_detaches_one_owner_atomically():
    flows = OAuthHostFlowStore()
    engine = object()
    for owner in ("alice", "alice", "bob"):
        flows.add(OAuthHostFlow.create(
            owner=owner,
            connection_id=f"pcn-{owner}",
            provider_id="openai",
            billing_lane="subscription",
            mode="add",
            label="Account",
            method=0,
            engine=engine,
            now=NOW,
        ))

    removed = flows.remove_owner("ALICE")

    assert len(removed) == 2
    assert {flow.owner for flow in removed} == {"alice"}
    assert len(flows.remove_owner("bob")) == 1


class FakeEngineControl:
    def __init__(self):
        self.connection_model_ids: list[str] = []
        self.account_model_ids: list[str] = []
        self.catalog_owners: list[str] = []

    @staticmethod
    def _model_routes(model_ids):
        return [
            {
                "modelID": model_id,
                "displayName": model_id,
                "operations": ["chat.stream", "chat.complete"],
                "capabilities": {},
                "provenance": {
                    "authority": "managed-engine",
                    "catalog": "test-catalog",
                },
            }
            for model_id in model_ids
        ]

    async def call(self, *, method, payload, **kwargs):
        if method.endswith("/catalog"):
            self.catalog_owners.append(kwargs["owner"])
            return {
                "schemaVersion": 1,
                "families": [
                    {
                        "id": "openai",
                        "displayName": "OpenAI",
                        "adapters": ["openai-responses", "openai-chat"],
                        "kinds": ["official", "subscription", "custom_gateway"],
                        "billingLanes": ["metered_api", "subscription", "custom"],
                        "authMethods": [
                            {"id": "api_key", "type": "api", "label": "API key"},
                            {"id": "oauth:0", "type": "oauth", "label": "Browser login"},
                        ],
                        "modelCount": 3,
                    }
                ],
            }
        if method.endswith("/connection/validate"):
            if payload["familyID"] not in {"openai", "openai-compatible", "ollama"}:
                raise RuntimeError("unknown family")
            return {
                "familyID": payload["familyID"],
                "adapterID": payload["adapterID"],
                "kind": payload["kind"],
                "billingLane": payload["billingLane"],
                "settings": payload["settings"],
                "credentialRequired": True,
                "modelRoutes": self._model_routes(self.connection_model_ids),
                **({"normalizedURL": payload["url"]} if payload.get("url") else {}),
            }
        if method.endswith("/account/validate"):
            return {
                "authMethod": "api_key",
                "authClass": (
                    "local"
                    if payload["connection"]["billingLane"] == "local"
                    else "metered"
                ),
                "credential": payload["credential"],
                "safeIdentity": {},
                "modelRoutes": self._model_routes(self.account_model_ids),
            }
        raise AssertionError(method)


@pytest.fixture()
def provider_api(monkeypatch, tmp_path):
    monkeypatch.setattr(secret_storage, "_KEY_PATH", tmp_path / ".app_key")
    monkeypatch.setattr(secret_storage, "_fernet", None)
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    ProviderBase.metadata.create_all(bind=engine)
    factory = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    store = ProviderStore(factory, clock=lambda: NOW)
    app = FastAPI()

    @app.middleware("http")
    async def test_identity(request: Request, call_next):
        api_owner = request.headers.get("x-test-api-owner")
        if api_owner:
            request.state.current_user = "api"
            request.state.api_token = True
            request.state.api_token_owner = api_owner
            request.state.api_token_client_kind = request.headers.get(
                "x-test-client-kind",
                "api",
            )
            request.state.api_token_scopes = [
                item.strip()
                for item in request.headers.get("x-test-scopes", "").split(",")
                if item.strip()
            ]
        else:
            request.state.current_user = request.headers.get("x-test-user", "alice")
            request.state.api_token = False
        return await call_next(request)

    control = FakeEngineControl()
    app.state.fake_provider_control = control
    app.include_router(setup_provider_v1_routes(store, control))
    with TestClient(app) as client:
        yield client, store, factory
    engine.dispose()


class FakeOAuthLease:
    def __init__(self, control):
        self.control = control
        self.released = False
        self.successful = None

    async def call(self, method, payload):
        if method.endswith("/oauth/start"):
            self.control.flows[payload["flowID"]] = dict(payload)
            return {
                "flowID": payload["flowID"],
                "url": f"https://login.example/authorize?state={payload['state']}",
                "method": "code",
                "instructions": "Sign in and paste the code",
                "expiresAt": payload["expiresAt"],
            }
        if method.endswith("/oauth/poll"):
            return self.control.completion(payload)
        if method.endswith("/oauth/callback"):
            return self.control.completion(payload)
        if method.endswith("/oauth/cancel"):
            self.control.cancelled.add(payload["flowID"])
            return {"flowID": payload["flowID"], "status": "cancelled"}
        raise AssertionError(method)

    async def release(self, *, successful=False):
        self.released = True
        self.successful = successful


class FakeOAuthControl(FakeEngineControl):
    def __init__(self):
        super().__init__()
        self.flows = {}
        self.leases = []
        self.cancelled = set()

    async def bind(self, **_kwargs):
        lease = FakeOAuthLease(self)
        self.leases.append(lease)
        return lease

    def completion(self, payload):
        started = self.flows[payload["flowID"]]
        result = {
            "flowID": started["flowID"],
            "connectionID": started["connectionID"],
            "providerID": started["providerID"],
            "billingLane": started["billingLane"],
            "mode": started["mode"],
            "status": "complete",
            "credential": {
                "type": "oauth",
                "refresh": f"refresh-{started['flowID']}",
                "access": f"access-{started['flowID']}",
                "expires": 2_000_000_000_000,
                "accountId": f"external-{started['flowID'][-8:]}",
            },
            "authMethod": "oauth",
            "authClass": "subscription",
            "safeIdentity": {
                "provider_display_identity": f"external-{started['flowID'][-8:]}"
            },
        }
        if started["mode"] == "reauth":
            result["targetAccountID"] = started["targetAccountID"]
            result["expectedRevision"] = started["expectedRevision"]
        return result


@pytest.fixture()
def oauth_provider_api(monkeypatch, tmp_path):
    monkeypatch.setattr(secret_storage, "_KEY_PATH", tmp_path / ".app_key")
    monkeypatch.setattr(secret_storage, "_fernet", None)
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    ProviderBase.metadata.create_all(bind=engine)
    factory = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    store = ProviderStore(factory, clock=lambda: NOW)
    control = FakeOAuthControl()
    app = FastAPI()

    @app.middleware("http")
    async def test_identity(request: Request, call_next):
        request.state.current_user = request.headers.get("x-test-user", "alice")
        request.state.api_token = False
        return await call_next(request)

    app.include_router(setup_provider_v1_routes(store, control))
    with TestClient(app) as client:
        yield client, store, control
    engine.dispose()


def _create_connection(client: TestClient, *, key: str = "connection-create"):
    return client.post(
        "/api/v1/providers/connections",
        headers={"Idempotency-Key": key},
        json={
            "family_id": "openai",
            "adapter_id": "openai-responses",
            "kind": "official",
            "billing_lane": "metered_api",
            "label": "OpenAI API",
            "settings": {"organization": "example"},
        },
    )


def _seed_route(store: ProviderStore, connection_id: str, *, owner: str = "alice"):
    return store.create_model_route(
        owner=owner,
        connection_id=connection_id,
        provider_model_id="gpt-test",
        display_name="GPT Test",
        operations=("chat.stream", "chat.complete"),
    )


def test_family_catalog_is_bundled_and_never_calls_owner_engine(provider_api):
    client, _, _ = provider_api
    control = client.app.state.fake_provider_control

    first = client.get("/api/v1/providers/families")
    second = client.get("/api/v1/providers/families")
    bob = client.get(
        "/api/v1/providers/families",
        headers={"x-test-user": "bob"},
    )

    assert first.status_code == second.status_code == bob.status_code == 200
    assert first.json() == second.json() == bob.json()
    assert control.catalog_owners == []
    assert first.json()["source"] == "bundled-pinned-model-catalog"
    assert first.json()["catalog_key"].startswith("pfcat_")
    assert first.json()["auth_method_enrichment"] == {
        "status": "deferred",
        "source": "managed-engine",
        "oauth_methods_complete": False,
    }
    assert first.headers["cache-control"] == "private, max-age=300"
    assert first.headers["vary"] == "Cookie, Authorization"


def test_family_catalog_does_not_touch_provider_store_or_control():
    class ForbiddenEngineControl:
        async def call(self, **_kwargs):
            raise AssertionError("family reads must not cross the engine boundary")

    app = FastAPI()

    @app.middleware("http")
    async def test_identity(request: Request, call_next):
        request.state.current_user = "alice"
        request.state.api_token = False
        return await call_next(request)

    app.include_router(setup_provider_v1_routes(object(), ForbiddenEngineControl()))
    with TestClient(app) as client:
        response = client.get("/api/v1/providers/families")

    assert response.status_code == 200
    assert len(response.json()["families"]) == 82


def test_family_auth_method_enrichment_is_explicit_validated_and_owner_scoped(
    provider_api,
):
    client, _, _ = provider_api
    control = client.app.state.fake_provider_control

    unknown = client.post("/api/v1/providers/families/not-real/auth-methods")
    assert unknown.status_code == 404
    assert control.catalog_owners == []

    response = client.post("/api/v1/providers/families/openai/auth-methods")

    assert response.status_code == 200
    assert response.headers["cache-control"] == "private, no-store"
    assert response.headers["vary"] == "Cookie, Authorization"
    assert response.json() == {
        "family_id": "openai",
        "source": "managed-engine",
        "auth_methods": [
            {"id": "api_key", "type": "api", "label": "API key"},
            {"id": "oauth:0", "type": "oauth", "label": "Browser login"},
        ],
        "auth_methods_complete": True,
    }
    assert control.catalog_owners == ["alice"]


def test_management_snapshot_is_one_nonsecret_supervisor_free_response(provider_api):
    client, store, _ = provider_api
    control = client.app.state.fake_provider_control
    connection = _create_connection(client, key="snapshot-connection").json()
    account = store.create_account(
        owner="alice",
        connection_id=connection["id"],
        account_id="snapshot-account",
        label="Private account label",
        auth_method="api_key",
        auth_class="metered",
        credentials={"type": "api_key", "key": "snapshot-secret"},
        safe_identity={"provider_display_identity": "alice@example.test"},
    )
    route = _seed_route(store, connection["id"])
    store.set_entitlement(
        owner="alice",
        account_id=account.id,
        model_route_id=route.id,
        eligible=True,
        capability_fingerprint="chat-v1",
    )
    store.put_route_binding(
        owner="alice",
        purpose="chat",
        model_route_ids=[route.id],
        expected_revision=0,
    )
    before_catalog_calls = list(control.catalog_owners)

    response = client.get("/api/v1/providers/management-snapshot")

    assert response.status_code == 200
    assert response.headers["cache-control"] == "private, no-store"
    assert response.headers["vary"] == "Cookie, Authorization"
    assert response.headers["server-timing"].startswith("provider-db;dur=")
    assert control.catalog_owners == before_catalog_calls
    body = response.json()
    assert body["schema_version"] == 1
    assert [item["id"] for item in body["connections"]] == [connection["id"]]
    assert body["connections"][0]["account_count"] == 1
    assert [item["id"] for item in body["models"]] == [route.id]
    assert body["shares"] == {"received": []}
    assert body["deferred"] == [
        "accounts",
        "bindings",
        "owned_shares",
        "recipients",
    ]
    assert "accounts" not in body
    assert "bindings" not in body
    assert "snapshot-secret" not in response.text
    assert "credential_envelope" not in response.text


def test_model_compatibility_routes_project_only_normalized_state(provider_api):
    client, store, _ = provider_api
    connection = _create_connection(client, key="catalog-parent").json()
    route = _seed_route(store, connection["id"])
    store.put_route_binding(
        owner="alice",
        purpose="chat",
        model_route_ids=[route.id],
        expected_revision=0,
    )

    catalogue = client.get("/api/models")
    assert catalogue.status_code == 200
    assert catalogue.json()["items"][0]["endpoint_id"] == connection["id"]
    assert catalogue.json()["items"][0]["url"] == "openclank://engine"
    assert "base_url" not in catalogue.text

    default = client.get("/api/default-chat")
    assert default.status_code == 200
    assert default.json() == {
        "endpoint_id": connection["id"],
        "endpoint_url": "openclank://engine",
        "model": "gpt-test",
    }


def test_model_catalog_requires_chat_scope_for_api_tokens(provider_api):
    client, _, _ = provider_api
    rejected = client.get(
        "/api/models",
        headers={
            "x-test-api-owner": "alice",
            "x-test-scopes": "providers:read",
        },
    )
    assert rejected.status_code == 403

    accepted = client.get(
        "/api/models",
        headers={"x-test-api-owner": "alice", "x-test-scopes": "chat"},
    )
    assert accepted.status_code == 200


def test_connection_crud_is_owner_scoped_revisioned_and_idempotent(provider_api):
    client, store, _ = provider_api
    created = _create_connection(client)
    assert created.status_code == 201
    assert created.headers["etag"] == '"1"'
    assert created.headers["idempotency-replayed"] == "false"
    connection = created.json()

    replay = _create_connection(client)
    assert replay.status_code == 201
    assert replay.json() == connection
    assert replay.headers["idempotency-replayed"] == "true"
    assert len(store.list_connections(owner="alice")) == 1

    mismatch = client.post(
        "/api/v1/providers/connections",
        headers={"Idempotency-Key": "connection-create"},
        json={
            "family_id": "openai",
            "adapter_id": "openai-responses",
            "kind": "official",
            "billing_lane": "metered_api",
            "label": "Different body",
        },
    )
    assert mismatch.status_code == 409

    secret_setting = client.post(
        "/api/v1/providers/connections",
        headers={"Idempotency-Key": "connection-secret-setting"},
        json={
            "family_id": "openai",
            "adapter_id": "openai-responses",
            "kind": "official",
            "billing_lane": "metered_api",
            "label": "Unsafe",
            "settings": {"api_key": "must-not-live-here"},
        },
    )
    assert secret_setting.status_code == 422

    other_owner = client.get(
        f"/api/v1/providers/connections/{connection['id']}",
        headers={"x-test-user": "bob"},
    )
    assert other_owner.status_code == 404

    missing_fence = client.patch(
        f"/api/v1/providers/connections/{connection['id']}",
        headers={"Idempotency-Key": "connection-update"},
        json={"label": "Primary"},
    )
    assert missing_fence.status_code == 428

    updated = client.patch(
        f"/api/v1/providers/connections/{connection['id']}",
        headers={
            "Idempotency-Key": "connection-update",
            "If-Match": 'W/"1"',
        },
        json={"label": "Primary"},
    )
    assert updated.status_code == 200
    assert updated.json()["label"] == "Primary"
    assert updated.json()["revision"] == 2

    stale = client.patch(
        f"/api/v1/providers/connections/{connection['id']}",
        headers={
            "Idempotency-Key": "connection-stale",
            "If-Match": '"1"',
        },
        json={"label": "Stale"},
    )
    assert stale.status_code == 412
    assert stale.headers["etag"] == '"2"'


def test_api_key_is_write_only_and_rotation_is_atomic(provider_api):
    client, store, factory = provider_api
    connection = _create_connection(client, key="account-parent").json()
    original = "sk-secret-original-value"
    created = client.post(
        f"/api/v1/providers/connections/{connection['id']}/accounts",
        headers={"Idempotency-Key": "account-create"},
        json={"label": "Primary account", "api_key": original},
    )
    assert created.status_code == 201
    assert original not in created.text
    account = created.json()
    assert "credential_envelope" not in account
    assert "credential_fingerprint" not in account
    assert account["has_credential"] is True

    rejected_secret = "rejected-secret-must-not-echo"
    rejected = client.post(
        f"/api/v1/providers/connections/{connection['id']}/accounts",
        headers={"Idempotency-Key": "account-rejected-extra"},
        json={
            "label": "Rejected",
            "api_key": "otherwise-valid",
            "credentials": rejected_secret,
        },
    )
    assert rejected.status_code == 422
    assert rejected_secret not in rejected.text

    oversized_secret = "s" * 131_073
    oversized = client.post(
        f"/api/v1/providers/connections/{connection['id']}/accounts",
        headers={"Idempotency-Key": "account-rejected-length"},
        json={"label": "Rejected", "api_key": oversized_secret},
    )
    assert oversized.status_code == 422
    assert oversized_secret[:128] not in oversized.text

    listing = client.get(
        f"/api/v1/providers/connections/{connection['id']}/accounts"
    )
    assert listing.status_code == 200
    assert original not in listing.text
    assert "credential_fingerprint" not in listing.text

    with factory() as db:
        raw = db.query(ProviderAccount).filter_by(id=account["id"]).one()
        assert original not in raw.credential_envelope
        records = db.query(ProviderIdempotencyRecord).all()
        assert original not in repr([row.__dict__ for row in records])

    rotated_secret = "sk-secret-rotated-value"
    rotated = client.patch(
        f"/api/v1/providers/accounts/{account['id']}",
        headers={
            "If-Match": f'"{account["revision"]}"',
            "Idempotency-Key": "account-rotate",
        },
        json={"label": "Renamed", "api_key": rotated_secret},
    )
    assert rotated.status_code == 200
    assert rotated_secret not in rotated.text
    assert rotated.json()["credential_revision"] == 2
    assert rotated.json()["revision"] == account["revision"] + 1
    assert store.credential_access(
        owner="alice",
        account_id=account["id"],
    ).credentials == {"type": "api", "key": rotated_secret}

    replay = client.patch(
        f"/api/v1/providers/accounts/{account['id']}",
        headers={
            "If-Match": f'"{account["revision"]}"',
            "Idempotency-Key": "account-rotate",
        },
        json={"label": "Renamed", "api_key": rotated_secret},
    )
    assert replay.status_code == 200
    assert replay.headers["idempotency-replayed"] == "true"


def test_engine_models_publish_on_connection_creation(provider_api):
    client, store, _ = provider_api
    client.app.state.fake_provider_control.connection_model_ids = ["gpt-published"]

    created = _create_connection(client, key="connection-with-models")

    assert created.status_code == 201
    assert [item["model_id"] for item in created.json()["models"]] == [
        "gpt-published"
    ]
    persisted = store.list_model_routes(
        owner="alice",
        connection_id=created.json()["id"],
    )
    assert [item.provider_model_id for item in persisted] == ["gpt-published"]


def test_protected_local_account_publishes_and_entitles_discovered_models(provider_api):
    client, store, _ = provider_api
    control = client.app.state.fake_provider_control
    control.connection_model_ids = []
    connection = client.post(
        "/api/v1/providers/connections",
        headers={"Idempotency-Key": "protected-local-connection"},
        json={
            "family_id": "openai-compatible",
            "adapter_id": "openai-chat",
            "kind": "local",
            "billing_lane": "local",
            "label": "Protected local gateway",
            "url": "http://127.0.0.1:8080",
        },
    ).json()
    control.account_model_ids = ["private-model"]
    secret = "private-local-key"

    created = client.post(
        f"/api/v1/providers/connections/{connection['id']}/accounts",
        headers={"Idempotency-Key": "protected-local-account"},
        json={"label": "Local key", "api_key": secret},
    )

    assert created.status_code == 201
    assert secret not in created.text
    routes = store.list_model_routes(
        owner="alice",
        connection_id=connection["id"],
    )
    assert [route.provider_model_id for route in routes] == ["private-model"]
    eligibility = store.model_eligibility(
        owner="alice",
        model_route_id=routes[0].id,
    )
    assert eligibility[0]["account_id"] == created.json()["id"]
    assert eligibility[0]["eligible"] is True

    control.account_model_ids = ["private-model-v2"]
    replacement = "replacement-local-key"
    rotated = client.patch(
        f"/api/v1/providers/accounts/{created.json()['id']}",
        headers={
            "If-Match": f'"{created.json()["revision"]}"',
            "Idempotency-Key": "protected-local-rotate",
        },
        json={"api_key": replacement},
    )
    assert rotated.status_code == 200
    assert replacement not in rotated.text
    assert [
        route.provider_model_id
        for route in store.list_model_routes(
            owner="alice",
            connection_id=connection["id"],
        )
    ] == ["private-model-v2"]


def test_refresh_preserves_catalog_on_empty_and_syncs_with_revision_fence(provider_api):
    client, store, _ = provider_api
    control = client.app.state.fake_provider_control
    control.connection_model_ids = ["model-before"]
    connection = _create_connection(client, key="refresh-parent").json()
    account = client.post(
        f"/api/v1/providers/connections/{connection['id']}/accounts",
        headers={"Idempotency-Key": "refresh-existing-account"},
        json={"label": "Existing account", "api_key": "refresh-account-key"},
    ).json()
    connection = client.get(
        f"/api/v1/providers/connections/{connection['id']}"
    ).json()

    control.connection_model_ids = []
    unavailable = client.post(
        f"/api/v1/providers/connections/{connection['id']}/models/refresh",
        headers={
            "If-Match": f'"{connection["revision"]}"',
            "Idempotency-Key": "refresh-empty-preserves",
        },
        json={},
    )
    assert unavailable.status_code == 503
    assert [
        route.provider_model_id
        for route in store.list_model_routes(
            owner="alice",
            connection_id=connection["id"],
        )
    ] == ["model-before"]

    control.connection_model_ids = ["model-after"]
    refreshed = client.post(
        f"/api/v1/providers/connections/{connection['id']}/models/refresh",
        headers={
            "If-Match": f'"{connection["revision"]}"',
            "Idempotency-Key": "refresh-model-catalog",
        },
        json={},
    )
    assert refreshed.status_code == 200
    assert [item["model_id"] for item in refreshed.json()["models"]] == [
        "model-after"
    ]
    refreshed_route = store.list_model_routes(
        owner="alice",
        connection_id=connection["id"],
    )[0]
    assert {
        item["account_id"]
        for item in store.model_eligibility(
            owner="alice",
            model_route_id=refreshed_route.id,
        )
        if item["eligible"]
    } == {account["id"]}
    replay = client.post(
        f"/api/v1/providers/connections/{connection['id']}/models/refresh",
        headers={
            "If-Match": f'"{connection["revision"]}"',
            "Idempotency-Key": "refresh-model-catalog",
        },
        json={},
    )
    assert replay.status_code == 200
    assert replay.headers["idempotency-replayed"] == "true"


def test_oauth_add_flows_append_accounts_and_ack_only_after_persistence(oauth_provider_api):
    client, store, control = oauth_provider_api
    control.connection_model_ids = ["subscription-model"]
    connection = client.post(
        "/api/v1/providers/connections",
        headers={"Idempotency-Key": "oauth-parent-create"},
        json={
            "family_id": "openai",
            "adapter_id": "openai-responses",
            "kind": "subscription",
            "billing_lane": "subscription",
            "label": "ChatGPT subscriptions",
        },
    ).json()

    starts = []
    for index in range(2):
        response = client.post(
            f"/api/v1/providers/connections/{connection['id']}/oauth/start",
            headers={"Idempotency-Key": f"oauth-add-account-{index}"},
            json={"label": f"Subscription {index}", "method": 0, "inputs": {}},
        )
        assert response.status_code == 201
        assert response.json()["status"] == "pending"
        starts.append(response.json())

    assert store.list_accounts(owner="alice", connection_id=connection["id"]) == []

    account_ids = []
    for started in starts:
        flow_id = started["flow_id"]
        state = control.flows[flow_id]["state"]
        completed = client.post(
            f"/api/v1/providers/oauth/flows/{flow_id}/callback",
            json={"state": state, "code": f"code-{flow_id}"},
        )
        assert completed.status_code == 200
        assert completed.json()["status"] == "complete"
        assert "access-" not in completed.text
        account_ids.append(completed.json()["account"]["id"])

    assert len(set(account_ids)) == 2
    rows = store.list_accounts(owner="alice", connection_id=connection["id"])
    assert [row.label for row in rows] == ["Subscription 0", "Subscription 1"]
    assert all(row.auth_method == "oauth" for row in rows)
    route = store.list_model_routes(
        owner="alice",
        connection_id=connection["id"],
    )[0]
    assert {
        item["account_id"]
        for item in store.model_eligibility(
            owner="alice",
            model_route_id=route.id,
        )
        if item["eligible"]
    } == set(account_ids)
    assert all(lease.released and lease.successful for lease in control.leases)
    assert set(control.cancelled) == {item["flow_id"] for item in starts}


def test_oauth_reauth_cas_replaces_only_the_stable_target(oauth_provider_api):
    client, store, control = oauth_provider_api
    connection = store.create_connection(
        owner="alice",
        family_id="openai",
        adapter_id="openai-responses",
        kind="subscription",
        billing_lane="subscription",
        label="ChatGPT subscriptions",
    )
    target = store.create_account(
        owner="alice",
        connection_id=connection.id,
        label="Target",
        auth_method="oauth",
        auth_class="subscription",
        credentials={
            "type": "oauth",
            "refresh": "target-old-refresh",
            "access": "target-old-access",
            "expires": 1,
        },
    )
    untouched = store.create_account(
        owner="alice",
        connection_id=connection.id,
        label="Untouched",
        auth_method="oauth",
        auth_class="subscription",
        credentials={
            "type": "oauth",
            "refresh": "other-refresh",
            "access": "other-access",
            "expires": 1,
        },
    )

    started = client.post(
        f"/api/v1/providers/accounts/{target.id}/oauth/reauth",
        headers={
            "If-Match": f'"{target.revision}"',
            "Idempotency-Key": "oauth-reauth-target",
        },
        json={"method": 0, "inputs": {}},
    )
    assert started.status_code == 201
    flow_id = started.json()["flow_id"]
    completed = client.post(
        f"/api/v1/providers/oauth/flows/{flow_id}/callback",
        json={"state": control.flows[flow_id]["state"], "code": "new-code"},
    )
    assert completed.status_code == 200
    assert completed.json()["account"]["id"] == target.id
    assert completed.json()["account"]["revision"] == target.revision + 1
    target_secret = store.credential_access(owner="alice", account_id=target.id).credentials
    other_secret = store.credential_access(owner="alice", account_id=untouched.id).credentials
    assert target_secret["refresh"] == f"refresh-{flow_id}"
    assert other_secret["refresh"] == "other-refresh"
    assert len(store.list_accounts(owner="alice", connection_id=connection.id)) == 2


def test_api_token_scopes_and_tui_tokens_are_confined(provider_api):
    client, _, _ = provider_api
    denied = client.get(
        "/api/v1/providers/connections",
        headers={
            "x-test-api-owner": "alice",
            "x-test-scopes": "chat",
        },
    )
    assert denied.status_code == 403

    allowed = client.get(
        "/api/v1/providers/connections",
        headers={
            "x-test-api-owner": "alice",
            "x-test-scopes": "providers:read",
        },
    )
    assert allowed.status_code == 200

    read_only_write = _create_connection(
        TestClientProxy(
            client,
            {
                "x-test-api-owner": "alice",
                "x-test-scopes": "providers:read",
            },
        ),
        key="scope-denied-write",
    )
    assert read_only_write.status_code == 403

    tui = client.get(
        "/api/v1/providers/connections",
        headers={
            "x-test-api-owner": "alice",
            "x-test-scopes": "providers:read,providers:write",
            "x-test-client-kind": "tui",
        },
    )
    assert tui.status_code == 403


class TestClientProxy:
    """Add default headers while preserving the tiny TestClient API we use."""

    def __init__(self, client: TestClient, headers: dict[str, str]):
        self.client = client
        self.headers = headers

    def post(self, path: str, **kwargs):
        headers = {**self.headers, **kwargs.pop("headers", {})}
        return self.client.post(path, headers=headers, **kwargs)


def test_route_bindings_require_one_route_and_reject_cross_owner_and_wrong_capability(
    provider_api,
):
    client, store, _ = provider_api
    connection = _create_connection(client, key="binding-parent").json()
    first = _seed_route(store, connection["id"])
    second = store.create_model_route(
        owner="alice",
        connection_id=connection["id"],
        provider_model_id="gpt-fallback",
        display_name="GPT Fallback",
        operations=("chat.complete",),
    )

    created = client.put(
        "/api/v1/providers/bindings/chat",
        headers={"If-Match": '"0"', "Idempotency-Key": "binding-create"},
        json={"routes": [{"model_route_id": first.id, "enabled": True}]},
    )
    assert created.status_code == 200
    assert created.headers["etag"] == '"1"'
    assert [row["ordinal"] for row in created.json()["routes"]] == [0]

    multiple = client.put(
        "/api/v1/providers/bindings/chat",
        headers={"If-Match": '"1"', "Idempotency-Key": "binding-too-many"},
        json={
            "routes": [
                {"model_route_id": first.id},
                {"model_route_id": second.id},
            ]
        },
    )
    assert multiple.status_code == 422

    fetched = client.get("/api/v1/providers/bindings/chat")
    assert fetched.status_code == 200
    assert fetched.json() == created.json()

    wrong_capability = client.put(
        "/api/v1/providers/bindings/images",
        headers={"If-Match": '"0"', "Idempotency-Key": "binding-wrong-op"},
        json={"routes": [{"model_route_id": first.id}]},
    )
    assert wrong_capability.status_code == 422

    bob_connection = store.create_connection(
        owner="bob",
        family_id="openai",
        adapter_id="openai-responses",
        kind="official",
        billing_lane="metered_api",
        label="Bob",
    )
    bob_route = _seed_route(store, bob_connection.id, owner="bob")
    cross_owner = client.put(
        "/api/v1/providers/bindings/chat",
        headers={"If-Match": '"1"', "Idempotency-Key": "binding-cross-owner"},
        json={"routes": [{"model_route_id": bob_route.id}]},
    )
    assert cross_owner.status_code == 404


def test_pool_order_use_next_and_quota_reset_eligibility(provider_api):
    client, store, _ = provider_api
    connection = _create_connection(client, key="pool-parent").json()
    account_ids = []
    for index in range(2):
        response = client.post(
            f"/api/v1/providers/connections/{connection['id']}/accounts",
            headers={"Idempotency-Key": f"pool-account-{index}"},
            json={"label": f"Account {index}", "api_key": f"secret-{index}"},
        )
        assert response.status_code == 201
        account_ids.append(response.json()["id"])
    current = client.get(
        f"/api/v1/providers/connections/{connection['id']}"
    ).json()
    ordered = client.put(
        f"/api/v1/providers/connections/{connection['id']}/pool/order",
        headers={
            "If-Match": f'"{current["revision"]}"',
            "Idempotency-Key": "pool-reorder",
        },
        json={"account_ids": list(reversed(account_ids))},
    )
    assert ordered.status_code == 200
    assert [row["id"] for row in ordered.json()["accounts"]] == list(
        reversed(account_ids)
    )

    revision = ordered.json()["revision"]
    unscoped = client.post(
        f"/api/v1/providers/connections/{connection['id']}/pool/use-next",
        headers={
            "If-Match": f'"{revision}"',
            "Idempotency-Key": "pool-use-next-unscoped",
        },
        json={},
    )
    assert unscoped.status_code == 422

    route = _seed_route(store, connection["id"])
    for account_id in account_ids:
        store.set_entitlement(
            owner="alice",
            account_id=account_id,
            model_route_id=route.id,
            eligible=True,
        )
    advanced = client.post(
        f"/api/v1/providers/connections/{connection['id']}/pool/use-next",
        headers={
            "If-Match": f'"{revision}"',
            "Idempotency-Key": "pool-use-next",
        },
        json={"model_route_id": route.id},
    )
    assert advanced.status_code == 200
    assert advanced.json()["next_account"]["id"] == account_ids[0]

    store.set_account_health(
        owner="alice",
        account_id=account_ids[0],
        state="quota",
        quota_reset_at=NOW + timedelta(minutes=10),
        last_error_code="Authorization: Bearer must-not-surface",
    )
    eligibility = client.get(
        f"/api/v1/providers/models/{route.id}/eligibility"
    ).json()["accounts"]
    by_id = {row["account_id"]: row for row in eligibility}
    assert by_id[account_ids[0]]["eligible"] is False
    assert by_id[account_ids[0]]["account_last_error_code"] == "provider_error"
    assert "must-not-surface" not in repr(eligibility)
    assert by_id[account_ids[1]]["eligible"] is True

    connection_projection = client.get(
        f"/api/v1/providers/connections/{connection['id']}/eligibility"
    )
    assert connection_projection.status_code == 200
    assert connection_projection.json()["connection_id"] == connection["id"]
    projected_models = connection_projection.json()["models"]
    assert [item["model_route_id"] for item in projected_models] == [route.id]
    assert projected_models[0]["accounts"] == eligibility
    assert "must-not-surface" not in connection_projection.text


def test_direct_model_share_is_immediate_attributed_revocable_and_secret_free(
    provider_api,
    monkeypatch,
):
    client, store, _ = provider_api
    client.app.state.auth_manager = type(
        "ShareDirectory",
        (),
        {
            "is_configured": True,
            "list_users": lambda _self: [
                {
                    "username": "alice",
                    "account_id": "account-must-not-surface",
                    "privileges": {"admin": True},
                },
                {"username": "BOB", "account_id": "bob-private-id"},
                {"name": "Carol", "is_admin": False},
            ],
        },
    )()

    recipients = client.get("/api/v1/providers/share-recipients")
    assert recipients.status_code == 200
    assert recipients.json() == {
        "recipients": [{"username": "bob"}, {"username": "carol"}]
    }
    assert "account_id" not in recipients.text
    assert "privileges" not in recipients.text
    directory_read_token = client.get(
        "/api/v1/providers/share-recipients",
        headers={
            "x-test-api-owner": "alice",
            "x-test-scopes": "providers:read",
        },
    )
    assert directory_read_token.status_code == 403

    connection = _create_connection(client, key="share-parent").json()
    account_response = client.post(
        f"/api/v1/providers/connections/{connection['id']}/accounts",
        headers={"Idempotency-Key": "share-account"},
        json={"label": "Owner private label", "api_key": "share-secret"},
    )
    account = account_response.json()
    route = _seed_route(store, connection["id"])
    store.set_entitlement(
        owner="alice",
        account_id=account["id"],
        model_route_id=route.id,
        eligible=True,
    )

    unknown_recipient = client.put(
        f"/api/v1/providers/models/{route.id}/shares/nobody",
        headers={"Idempotency-Key": "share-unknown"},
        json={"enabled": True},
    )
    assert unknown_recipient.status_code == 422

    self_share = client.put(
        f"/api/v1/providers/models/{route.id}/shares/alice",
        headers={"Idempotency-Key": "share-self"},
        json={"enabled": True},
    )
    assert self_share.status_code == 422

    selector_injection = client.put(
        f"/api/v1/providers/models/{route.id}/shares/bob",
        headers={"Idempotency-Key": "share-selector-injection"},
        json={"enabled": True, "model_selector": {"mode": "all_live_models"}},
    )
    assert selector_injection.status_code == 422

    cross_owner = client.put(
        f"/api/v1/providers/models/{route.id}/shares/carol",
        headers={
            "x-test-user": "bob",
            "Idempotency-Key": "share-cross-owner",
        },
        json={"enabled": True},
    )
    assert cross_owner.status_code == 404

    enabled = client.put(
        f"/api/v1/providers/models/{route.id}/shares/bob",
        headers={"Idempotency-Key": "share-model-enable"},
        json={"enabled": True},
    )
    assert enabled.status_code == 200
    assert enabled.headers["idempotency-replayed"] == "false"
    assert enabled.json()["enabled"] is True
    grant = enabled.json()["share"]
    assert grant["recipient"] == "bob"
    assert grant["model_selector"] == {
        "mode": "explicit_models",
        "model_route_ids": [route.id],
    }
    assert "accepted" not in grant

    replay = client.put(
        f"/api/v1/providers/models/{route.id}/shares/bob",
        headers={"Idempotency-Key": "share-model-enable"},
        json={"enabled": True},
    )
    assert replay.status_code == 200
    assert replay.headers["idempotency-replayed"] == "true"
    assert replay.json() == enabled.json()

    received = client.get(
        "/api/v1/providers/shares/received",
        headers={"x-test-user": "bob"},
    )
    assert received.status_code == 200
    assert account["id"] not in received.text
    assert route.id not in received.text
    assert "Owner private label" not in received.text
    assert "share-secret" not in received.text
    received_grant = received.json()["shares"][0]
    assert received_grant["shared_by"] == "alice"
    assert received_grant["family_id"] == "openai"
    assert received_grant["provider_family_id"] == "openai"
    assert received_grant["provider_display_name"] == "OpenAI"
    assert received_grant["provider_group_id"].startswith("shared_provider_")
    assert received_grant["provider_group_label"] == "alice's OpenAI"
    assert "accepted" not in received_grant
    assert "preferred_account_slot_id" not in received_grant
    assert received_grant["accounts"][0]["slot_id"].startswith("account_")
    received_get = client.get(
        f"/api/v1/providers/shares/received/{grant['id']}",
        headers={"x-test-user": "bob"},
    )
    assert received_get.status_code == 200
    assert received_get.json() == received_grant

    catalogue = client.get("/api/models", headers={"x-test-user": "bob"})
    assert catalogue.status_code == 200
    shared = catalogue.json()["items"][0]
    assert shared["shared"] is True
    assert shared["shared_by"] == "alice"
    assert "shared by alice" in shared["endpoint_name"]
    assert shared["provider_family_id"] == "openai"
    assert shared["provider_display_name"] == "OpenAI"
    assert shared["catalog"][0]["family"] == "OpenAI"
    wire = catalogue.text
    assert account["id"] not in wire
    assert route.id not in wire
    assert connection["id"] not in wire
    assert "Owner private label" not in wire
    assert "share-secret" not in wire

    selected_settings = {
        "default_endpoint_id": shared["endpoint_id"],
        "default_model": route.provider_model_id,
    }
    monkeypatch.setattr(
        "src.settings.get_user_setting",
        lambda key, _owner, default="": selected_settings.get(key, default),
    )
    selected_default = client.get(
        "/api/default-chat",
        headers={"x-test-user": "bob"},
    )
    assert selected_default.status_code == 200
    assert selected_default.json() == {
        "endpoint_id": shared["endpoint_id"],
        "endpoint_url": "openclank://engine",
        "model": route.provider_model_id,
    }

    retired_accept = client.post(
        f"/api/v1/providers/shares/{grant['id']}/accept",
        headers={"x-test-user": "bob"},
    )
    assert retired_accept.status_code == 410

    retired_preference = client.put(
        f"/api/v1/providers/shares/{grant['id']}/preferred-account",
        headers={"x-test-user": "bob"},
        json={"account_slot_id": received_grant["accounts"][0]["slot_id"]},
    )
    assert retired_preference.status_code == 410

    disabled = client.put(
        f"/api/v1/providers/models/{route.id}/shares/bob",
        headers={"Idempotency-Key": "share-model-disable"},
        json={"enabled": False},
    )
    assert disabled.status_code == 200
    assert disabled.json() == {"enabled": False, "share": None}
    assert client.get(
        "/api/v1/providers/shares/received",
        headers={"x-test-user": "bob"},
    ).json() == {"shares": []}
    assert client.get(
        "/api/models",
        headers={"x-test-user": "bob"},
    ).json()["items"] == []


def test_shared_catalog_uses_authoritative_family_for_bare_duplicate_models(provider_api):
    client, store, _ = provider_api
    families = (
        ("openai", "openai-responses", "OpenAI"),
        ("anthropic", "anthropic-messages", "Anthropic"),
    )
    grants = []
    for index, (family_id, adapter_id, _display_name) in enumerate(families):
        connection = store.create_connection(
            owner="alice",
            connection_id=f"same-model-connection-{index}",
            family_id=family_id,
            adapter_id=adapter_id,
            kind="official",
            billing_lane="metered_api",
            label=f"Private {family_id}",
        )
        route = store.create_model_route(
            owner="alice",
            connection_id=connection.id,
            model_route_id=f"same-model-route-{index}",
            provider_model_id="same-bare-model",
            display_name="Same bare model",
            operations=("chat.stream", "chat.complete"),
        )
        grants.append(store.create_share_grant(
            owner="alice",
            recipient="bob",
            connection_id=connection.id,
            label=f"Custom {family_id} share",
            account_selector=AccountSelector.all_live(),
            model_selector=ModelSelector.explicit((route.id,)),
            grant_id=f"same-model-grant-{index}",
        ))

    response = client.get("/api/models", headers={"x-test-user": "bob"})

    assert response.status_code == 200
    shared = response.json()["items"]
    assert len(shared) == len(grants)
    by_family = {item["provider_family_id"]: item for item in shared}
    assert set(by_family) == {"openai", "anthropic"}
    assert by_family["openai"]["provider_display_name"] == "OpenAI"
    assert by_family["anthropic"]["provider_display_name"] == "Anthropic"
    assert all(item["catalog"][0]["family"] in {"OpenAI", "Anthropic"} for item in shared)
    assert {item["catalog"][0]["model_id"] for item in shared} == {"same-bare-model"}
    assert all(item["share_label"].startswith("Custom ") for item in shared)
    wire = response.text
    assert "same-model-connection" not in wire
    assert "same-model-route" not in wire
