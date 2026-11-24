"""Security and revision contracts for the normalized provider HTTP API."""

from datetime import datetime, timedelta, timezone
import asyncio
from concurrent.futures import ThreadPoolExecutor
import time
import threading
from types import SimpleNamespace

from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient
import httpx
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
import pytest

from core.provider_models import (
    ProviderAccount,
    ProviderBase,
    ProviderIdempotencyRecord,
    ProviderModelRoute,
)
from routes.provider_v1_routes import setup_provider_v1_routes
import routes.provider_v1_routes as provider_v1_module
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


def test_host_rejects_mimo_endpoint_policy_that_disagrees_with_subscription_lane():
    connection = SimpleNamespace(
        family_id="xiaomi",
        adapter_id="mimo-native",
        billing_lane="subscription",
    )
    result = {
        "authMethod": "api_key",
        "acquisitionMethod": "browser",
        "credentialKind": "api",
        "endpointPolicy": "mimo-metered-api-v1",
        "credential": {"type": "api", "key": "synthetic-key"},
    }
    with pytest.raises(HTTPException, match="endpoint policy"):
        provider_v1_module._validate_account_auth_result(result, connection=connection)


def test_host_accepts_only_declared_opencode_account_lane_endpoint_pairs():
    for family_id, adapter_id, lane, endpoint_policy in (
        ("opencode-console", "opencode-native", "metered_api", "opencode-console-inference-v1"),
        ("opencode-go", "opencode-native", "subscription", "opencode-go-inference-v1"),
    ):
        connection = SimpleNamespace(
            family_id=family_id,
            adapter_id=adapter_id,
            billing_lane=lane,
        )
        result = {
            "authMethod": "api_key",
            "acquisitionMethod": "manual-key",
            "credentialKind": "api",
            "endpointPolicy": endpoint_policy,
            "credential": {"type": "api", "key": "synthetic-key"},
        }
        provider_v1_module._validate_account_auth_result(result, connection=connection)
        wrong = {**result, "endpointPolicy": "opencode-go-inference-v1" if family_id == "opencode-console" else "opencode-console-inference-v1"}
        with pytest.raises(HTTPException, match="endpoint policy"):
            provider_v1_module._validate_account_auth_result(wrong, connection=connection)


def test_host_accepts_zai_coding_plan_only_as_declared_subscription_api_key():
    connection = SimpleNamespace(
        family_id="zai-coding-plan",
        adapter_id="models-dev-openai-compatible",
        billing_lane="subscription",
    )
    result = {
        "authMethod": "api_key",
        "acquisitionMethod": "manual-key",
        "credentialKind": "api",
        "endpointPolicy": "zai-coding-plan-openai-v1",
        "credential": {"type": "api", "key": "synthetic-coding-plan-key"},
    }
    provider_v1_module._validate_account_auth_result(result, connection=connection)
    with pytest.raises(HTTPException, match="endpoint policy"):
        provider_v1_module._validate_account_auth_result(
            {**result, "endpointPolicy": "opencode-go-inference-v1"},
            connection=connection,
        )


def test_host_accepts_grok_only_as_device_oauth_on_its_declared_proxy_lane():
    connection = SimpleNamespace(
        family_id="grok",
        adapter_id="grok-cli-chat",
        billing_lane="subscription",
    )
    result = {
        "authMethod": "oauth",
        "acquisitionMethod": "device",
        "credentialKind": "oauth",
        "endpointPolicy": "grok-cli-proxy-v1",
        "credential": {"type": "oauth", "access": "synthetic-access", "refresh": "synthetic-refresh"},
    }
    provider_v1_module._validate_account_auth_result(
        result,
        connection=connection,
        expected_acquisition_method="device",
        expected_credential_kind="oauth",
    )
    with pytest.raises(HTTPException, match="endpoint policy"):
        provider_v1_module._validate_account_auth_result(
            {**result, "endpointPolicy": "opencode-go-inference-v1"},
            connection=connection,
        )
    with pytest.raises(HTTPException, match="endpoint policy"):
        provider_v1_module._validate_account_auth_result(
            {**result, "credentialKind": "api", "authMethod": "api_key", "credential": {"type": "api", "key": "synthetic"}},
            connection=connection,
        )


def test_host_auth_method_catalog_accepts_only_opencode_endpoint_policy_declarations():
    for family_id, lane, endpoint_policy in (
        ("opencode-console", "metered_api", {"metered_api": "opencode-console-inference-v1"}),
        ("opencode-go", "subscription", {"subscription": "opencode-go-inference-v1"}),
        ("grok", "subscription", {"subscription": "grok-cli-proxy-v1"}),
    ):
        catalog = {
            "schemaVersion": 1,
            "families": [{
                "id": family_id,
                "billingLanes": [lane],
                "endpointPolicies": endpoint_policy,
                "authMethods": [{
                    "id": "oauth:0" if family_id == "grok" else "api_key",
                    "type": "oauth" if family_id == "grok" else "api",
                    "label": "Grok device sign-in" if family_id == "grok" else "API key",
                    "acquisitionMethod": "device" if family_id == "grok" else "manual-key",
                    "credentialKinds": ["oauth" if family_id == "grok" else "api"],
                    "billingLanes": [lane],
                }],
            }],
        }
        assert provider_v1_module._provider_catalog_auth_methods(catalog, family_id=family_id)[0]["billingLanes"] == [lane]
        catalog["families"][0]["endpointPolicies"] = {lane: "wrong-policy"}
        with pytest.raises(provider_v1_module.ProviderEngineValidationError, match="endpoint policy"):
            provider_v1_module._provider_catalog_auth_methods(catalog, family_id=family_id)


class FakeEngineControl:
    def __init__(self):
        self.connection_model_ids: list[str] = []
        self.account_model_ids: list[str] = []
        self.catalog_owners: list[str] = []
        self.inventory_delay = 0.0
        self.inventory_calls = 0
        self.inventory_active = 0
        self.inventory_max_active = 0
        self.inventory_entered = None
        self.inventory_release = None

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
                            {"id": "api_key", "type": "api", "label": "API key", "acquisitionMethod": "manual-key", "credentialKinds": ["api"], "billingLanes": ["metered_api", "subscription", "custom"]},
                            {"id": "oauth:0", "type": "oauth", "label": "Browser login", "acquisitionMethod": "browser", "credentialKinds": ["oauth"], "billingLanes": ["subscription"]},
                        ],
                        "modelCount": 3,
                    },
                    {
                        "id": "xiaomi",
                        "displayName": "Xiaomi",
                        "adapters": ["mimo-native"],
                        "kinds": ["official", "subscription"],
                        "billingLanes": ["metered_api", "subscription"],
                        "endpointPolicies": {"metered_api": "mimo-metered-api-v1", "subscription": "mimo-token-plan-v1"},
                        "authMethods": [
                            {"id": "api_key", "type": "api", "label": "API key", "acquisitionMethod": "manual-key", "credentialKinds": ["api"], "billingLanes": ["metered_api", "subscription"]},
                            {"id": "oauth:0", "type": "oauth", "label": "Browser login", "acquisitionMethod": "browser", "credentialKinds": ["api"], "billingLanes": ["metered_api", "subscription"]},
                        ],
                        "modelCount": 3,
                    },
                    {
                        "id": "kilo",
                        "displayName": "Kilo Gateway",
                        "adapters": ["models-dev-openai-compatible"],
                        "kinds": ["official"],
                        "billingLanes": ["metered_api"],
                        "authMethods": [
                            {"id": "api_key", "type": "api", "label": "API key", "acquisitionMethod": "manual-key", "credentialKinds": ["api"], "billingLanes": ["metered_api"]},
                            {"id": "oauth:0", "type": "oauth", "label": "Kilo Gateway device sign-in", "acquisitionMethod": "device", "credentialKinds": ["api"], "billingLanes": ["metered_api"]},
                        ],
                        "modelCount": 335,
                    },
                    {
                        "id": "llmgateway",
                        "displayName": "LLM Gateway",
                        "adapters": ["models-dev-openai-compatible"],
                        "kinds": ["official"],
                        "billingLanes": ["metered_api"],
                        "authMethods": [
                            {"id": "api_key", "type": "api", "label": "API key", "acquisitionMethod": "manual-key", "credentialKinds": ["api"], "billingLanes": ["metered_api"]},
                            {"id": "oauth:0", "type": "oauth", "label": "Create Gateway key (choose project)", "acquisitionMethod": "browser", "credentialKinds": ["api"], "billingLanes": ["metered_api"]},
                        ],
                        "modelCount": 203,
                    },
                    {
                        "id": "llmgateway-devpass",
                        "displayName": "LLM Gateway DevPass",
                        "adapters": ["models-dev-openai-compatible"],
                        "kinds": ["subscription"],
                        "billingLanes": ["subscription"],
                        "authMethods": [
                            {"id": "api_key", "type": "api", "label": "DevPass API key", "acquisitionMethod": "manual-key", "credentialKinds": ["api"], "billingLanes": ["subscription"]},
                            {"id": "oauth:0", "type": "oauth", "label": "Connect DevPass", "acquisitionMethod": "browser", "credentialKinds": ["api"], "billingLanes": ["subscription"]},
                        ],
                        "modelCount": 0,
                    },
                ],
            }
        if method.endswith("/connection/validate"):
            if payload["familyID"] not in {"openai", "openai-compatible", "ollama", "xiaomi", "kilo", "llmgateway", "llmgateway-devpass"}:
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
            self.inventory_calls += 1
            self.inventory_active += 1
            self.inventory_max_active = max(
                self.inventory_max_active,
                self.inventory_active,
            )
            try:
                if self.inventory_entered is not None:
                    self.inventory_entered.set()
                if self.inventory_release is not None:
                    await self.inventory_release.wait()
                if self.inventory_delay:
                    await asyncio.sleep(self.inventory_delay)
            finally:
                self.inventory_active -= 1
            auth_method = payload["authMethod"]
            self.last_account_validation = dict(payload)
            credential = payload["credential"]
            model_routes = self._model_routes(self.account_model_ids)
            return {
                "authMethod": auth_method,
                "acquisitionMethod": payload["acquisitionMethod"],
                "credentialKind": payload["credentialKind"],
                "authClass": (
                    "subscription"
                    if payload["connection"]["billingLane"] == "subscription"
                    else "local"
                    if payload["connection"]["billingLane"] == "local"
                    else "metered"
                ),
                "credential": credential,
                "safeIdentity": {},
                "accountID": payload["accountID"],
                "credentialRevision": payload["credentialRevision"],
                "modelRoutes": model_routes,
                **({"endpointPolicy": "mimo-token-plan-v1"} if payload["connection"]["familyID"] == "xiaomi" else {}),
                "discovery": {
                    "status": "complete",
                    "accountID": payload["accountID"],
                    "credentialRevision": payload["credentialRevision"],
                    "models": model_routes,
                    "authoritative": True,
                    "provenance": {"authority": "managed-engine", "catalog": "test"},
                },
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
    app.state.auth_manager = SimpleNamespace(users={"alice": {}, "bob": {}})

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
        request.state.authenticated = True
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
        if method.endswith("/catalog"):
            return {
                "schemaVersion": 1,
                "families": [{
                    "id": "openai",
                    "displayName": "OpenAI",
                    "adapters": ["openai-responses", "openai-chat"],
                    "kinds": ["official", "subscription", "custom_gateway"],
                    "billingLanes": ["metered_api", "subscription", "custom"],
                    "authMethods": [
                        {"id": "api_key", "type": "api", "label": "API key", "acquisitionMethod": "manual-key", "credentialKinds": ["api"], "billingLanes": ["metered_api", "subscription", "custom"]},
                        {"id": "oauth:0", "type": "oauth", "label": "Browser login", "acquisitionMethod": "browser", "credentialKinds": ["oauth"], "billingLanes": ["subscription"]},
                    ],
                    "modelCount": 3,
                }, {
                    "id": "xiaomi",
                    "displayName": "Xiaomi",
                    "adapters": ["mimo-native"],
                    "kinds": ["official", "subscription"],
                    "billingLanes": ["metered_api", "subscription"],
                    "endpointPolicies": {"metered_api": "mimo-metered-api-v1", "subscription": "mimo-token-plan-v1"},
                    "authMethods": [
                        {"id": "oauth:0", "type": "oauth", "label": "Browser login", "acquisitionMethod": "browser", "credentialKinds": ["api"], "billingLanes": ["metered_api", "subscription"]},
                    ],
                    "modelCount": 3,
                }, {
                    "id": "kilo",
                    "displayName": "Kilo Gateway",
                    "adapters": ["models-dev-openai-compatible"],
                    "kinds": ["official"],
                    "billingLanes": ["metered_api"],
                    "authMethods": [
                        {"id": "api_key", "type": "api", "label": "API key", "acquisitionMethod": "manual-key", "credentialKinds": ["api"], "billingLanes": ["metered_api"]},
                        {"id": "oauth:0", "type": "oauth", "label": "Kilo Gateway device sign-in", "acquisitionMethod": "device", "credentialKinds": ["api"], "billingLanes": ["metered_api"]},
                    ],
                    "modelCount": 335,
                }, {
                    "id": "llmgateway",
                    "displayName": "LLM Gateway",
                    "adapters": ["models-dev-openai-compatible"],
                    "kinds": ["official"],
                    "billingLanes": ["metered_api"],
                    "authMethods": [
                        {"id": "api_key", "type": "api", "label": "API key", "acquisitionMethod": "manual-key", "credentialKinds": ["api"], "billingLanes": ["metered_api"]},
                        {"id": "oauth:0", "type": "oauth", "label": "Create Gateway key (choose project)", "acquisitionMethod": "browser", "credentialKinds": ["api"], "billingLanes": ["metered_api"]},
                    ],
                    "modelCount": 203,
                }, {
                    "id": "llmgateway-devpass",
                    "displayName": "LLM Gateway DevPass",
                    "adapters": ["models-dev-openai-compatible"],
                    "kinds": ["subscription"],
                    "billingLanes": ["subscription"],
                    "authMethods": [
                        {"id": "api_key", "type": "api", "label": "DevPass API key", "acquisitionMethod": "manual-key", "credentialKinds": ["api"], "billingLanes": ["subscription"]},
                        {"id": "oauth:0", "type": "oauth", "label": "Connect DevPass", "acquisitionMethod": "browser", "credentialKinds": ["api"], "billingLanes": ["subscription"]},
                    ],
                    "modelCount": 0,
                }],
            }
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
            self.control.callback_calls += 1
            time.sleep(0.05)
            return self.control.completion(payload)
        if method.endswith("/account/validate"):
            return await self.control.call(method=method, payload=payload)
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
        self.callback_calls = 0

    async def bind(self, **_kwargs):
        lease = FakeOAuthLease(self)
        self.leases.append(lease)
        return lease

    def completion(self, payload):
        started = self.flows[payload["flowID"]]
        is_mimo = started["providerID"] == "xiaomi"
        credential = (
            {"type": "api", "key": "synthetic-token-plan-key", "metadata": {"base_url": "https://token-plan-cn.xiaomimimo.com/v1"}}
            if is_mimo
            else {
                "type": "oauth",
                "refresh": f"refresh-{started['flowID']}",
                "access": f"access-{started['flowID']}",
                "expires": 2_000_000_000_000,
                "accountId": f"external-{started['flowID'][-8:]}",
            }
        )
        result = {
            "flowID": started["flowID"],
            "connectionID": started["connectionID"],
            "providerID": started["providerID"],
            "billingLane": started["billingLane"],
            "mode": started["mode"],
            "status": "complete",
            "credential": credential,
            "authMethod": "api_key" if is_mimo else "oauth",
            "acquisitionMethod": started.get("acquisitionMethod", "browser"),
            "credentialKind": credential["type"],
            "authClass": "subscription" if started["billingLane"] == "subscription" else "metered",
            "safeIdentity": {
                "provider_display_identity": f"external-{started['flowID'][-8:]}"
            },
        }
        if is_mimo:
            result["endpointPolicy"] = "mimo-token-plan-v1"
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
    app.state.auth_manager = SimpleNamespace(users={"alice": {}, "bob": {}})

    @app.middleware("http")
    async def test_identity(request: Request, call_next):
        request.state.current_user = request.headers.get("x-test-user", "alice")
        request.state.api_token = False
        request.state.authenticated = True
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
    assert first.headers["cache-control"] == "private, no-store"
    assert first.headers["vary"] == "Cookie, Authorization"


def test_family_catalog_does_not_touch_provider_store_or_control():
    class ForbiddenEngineControl:
        async def call(self, **_kwargs):
            raise AssertionError("family reads must not cross the engine boundary")

    app = FastAPI()
    app.state.auth_manager = SimpleNamespace(users={"alice": {}})

    @app.middleware("http")
    async def test_identity(request: Request, call_next):
        request.state.current_user = "alice"
        request.state.authenticated = True
        request.state.api_token = False
        return await call_next(request)

    app.include_router(setup_provider_v1_routes(object(), ForbiddenEngineControl()))
    with TestClient(app) as client:
        response = client.get("/api/v1/providers/families")

    assert response.status_code == 200
    assert {"openai", "anthropic", "xiaomi"} <= {f["id"] for f in response.json()["families"]}


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
            {"id": "api_key", "type": "api", "label": "API key", "acquisitionMethod": "manual-key", "credentialKinds": ["api"], "billingLanes": ["metered_api", "subscription", "custom"]},
            {"id": "oauth:0", "type": "oauth", "label": "Browser login", "acquisitionMethod": "browser", "credentialKinds": ["oauth"], "billingLanes": ["subscription"]},
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
    client.app.state.fake_provider_control.account_model_ids = ["gpt-test"]
    connection = _create_connection(client, key="catalog-parent").json()
    route = _seed_route(store, connection["id"])
    account = store.create_account(
        owner="alice",
        connection_id=connection["id"],
        account_id="pac_catalog_fixture",
        label="Catalog fixture",
        auth_method="api_key",
        auth_class="metered",
        credentials={"type": "api_key", "key": "catalog-fixture"},
    )
    store.set_entitlement(
        owner="alice",
        account_id=account.id,
        model_route_id=route.id,
        eligible=True,
    )
    store.put_route_binding(
        owner="alice",
        purpose="chat",
        model_route_ids=[route.id],
        expected_revision=0,
    )

    catalogue = client.get("/api/models")
    assert client.app.state.fake_provider_control.inventory_calls == 0
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


def test_model_catalogue_projects_explicit_reasoning_options_with_route_revision(provider_api):
    client, store, _ = provider_api
    connection = _create_connection(client, key="reasoning-catalog-parent").json()
    route = store.create_model_route(
        owner="alice",
        connection_id=connection["id"],
        provider_model_id="copilot-reasoning-fixture",
        display_name="Copilot reasoning fixture",
        operations=("chat.stream", "chat.complete"),
        capabilities={
            "reasoning": True,
            "reasoning_effort": ["low", "none", "0"],
            "reasoning_default_effort": "low",
        },
    )
    account = store.create_account(
        owner="alice",
        connection_id=connection["id"],
        account_id="pac_reasoning_catalog_fixture",
        label="Reasoning fixture",
        auth_method="oauth",
        auth_class="subscription",
        credentials={"type": "oauth", "access": "synthetic", "refresh": "synthetic"},
    )
    store.set_entitlement(
        owner="alice", account_id=account.id, model_route_id=route.id, eligible=True
    )

    response = client.get("/api/models")
    assert response.status_code == 200
    record = next(
        entry
        for item in response.json()["items"]
        for entry in item["catalog"]
        if entry.get("provider_model_id") == "copilot-reasoning-fixture"
    )
    assert record["catalog_revision"] == route.catalog_revision
    assert record["capabilities"]["request_options"] == [{
        "id": "reasoning_effort",
        "type": "enum",
        "label": "Thinking",
        "values": ["low", "none", "0"],
        "default": "inherit",
        "scope": "model",
        "source": "account-discovery",
        "catalog_revision": route.catalog_revision,
        "provider_default": "low",
    }]


def test_inventory_same_key_coalesces_on_one_async_flight(provider_api):
    client, store, _factory = provider_api
    control = client.app.state.fake_provider_control
    control.account_model_ids = ["inventory-model"]
    connection = store.create_connection(
        owner="alice",
        family_id="openai",
        adapter_id="openai-responses",
        kind="official",
        billing_lane="metered_api",
        label="Inventory",
    )
    store.create_account(
        owner="alice",
        connection_id=connection.id,
        account_id="pac_inventory_same",
        label="Inventory",
        auth_method="api_key",
        auth_class="metered",
        credentials={"type": "api_key", "key": "fixture"},
    )
    control.inventory_delay = 0.05

    async def fetch():
        transport = httpx.ASGITransport(app=client.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
            return await asyncio.gather(
                http.get("/api/models?refresh=true", headers={"x-test-user": "alice"}),
                http.get("/api/models?refresh=true", headers={"x-test-user": "alice"}),
            )

    responses = asyncio.run(fetch())

    assert [response.status_code for response in responses] == [200, 200]
    assert control.inventory_calls == 1


def test_inventory_different_keys_run_in_parallel(provider_api):
    client, store, _factory = provider_api
    control = client.app.state.fake_provider_control
    control.account_model_ids = ["inventory-model"]
    connection = store.create_connection(
        owner="alice",
        family_id="openai",
        adapter_id="openai-responses",
        kind="official",
        billing_lane="metered_api",
        label="Inventory",
    )
    for account_id in ("pac_inventory_first", "pac_inventory_second"):
        store.create_account(
            owner="alice",
            connection_id=connection.id,
            account_id=account_id,
            label=account_id,
            auth_method="api_key",
            auth_class="metered",
            credentials={"type": "api_key", "key": account_id},
        )
    control.inventory_delay = 0.05

    async def fetch():
        transport = httpx.ASGITransport(app=client.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
            return await http.get("/api/models?refresh=true", headers={"x-test-user": "alice"})

    response = asyncio.run(fetch())

    assert response.status_code == 200
    assert control.inventory_calls == 2
    assert control.inventory_max_active == 2


def test_inventory_cache_invalidates_on_connection_and_account_lifecycle_changes(
    provider_api,
):
    client, store, _factory = provider_api
    control = client.app.state.fake_provider_control
    control.account_model_ids = ["inventory-model"]
    connection = store.create_connection(
        owner="alice",
        family_id="openai",
        adapter_id="openai-responses",
        kind="official",
        billing_lane="metered_api",
        label="Inventory",
    )
    account = store.create_account(
        owner="alice",
        connection_id=connection.id,
        account_id="pac_inventory_lifecycle",
        label="Lifecycle",
        auth_method="api_key",
        auth_class="metered",
        credentials={"type": "api_key", "key": "fixture"},
    )
    assert client.get("/api/models?refresh=true").status_code == 200
    assert any(key[2] == account.id for key in provider_v1_module._ACCOUNT_INVENTORY_CACHE)

    updated = client.patch(
        f"/api/v1/providers/connections/{connection.id}",
        headers={
            "If-Match": f'"{store.get_connection(owner="alice", connection_id=connection.id).revision}"',
            "Idempotency-Key": "inventory-connection-invalidate",
        },
        json={"label": "Inventory updated"},
    )
    assert updated.status_code == 200
    assert not any(key[2] == account.id for key in provider_v1_module._ACCOUNT_INVENTORY_CACHE)

    assert client.get("/api/models?refresh=true").status_code == 200
    disabled = client.post(
        f"/api/v1/providers/accounts/{account.id}/enable",
        headers={
            "If-Match": f'"{account.revision}"',
            "Idempotency-Key": "inventory-account-disable",
        },
        json={"enabled": False},
    )
    assert disabled.status_code == 200
    assert not any(key[2] == account.id for key in provider_v1_module._ACCOUNT_INVENTORY_CACHE)


def test_stale_inventory_refresh_cannot_reconcile_after_credential_rotation(provider_api):
    client, store, _factory = provider_api
    control = client.app.state.fake_provider_control
    control.account_model_ids = ["inventory-model"]
    connection = store.create_connection(
        owner="alice",
        family_id="openai",
        adapter_id="openai-responses",
        kind="official",
        billing_lane="metered_api",
        label="Inventory",
    )
    account = store.create_account(
        owner="alice",
        connection_id=connection.id,
        account_id="pac_inventory_stale",
        label="Stale",
        auth_method="api_key",
        auth_class="metered",
        credentials={"type": "api_key", "key": "old"},
    )

    async def fetch_with_rotation():
        control.inventory_entered = asyncio.Event()
        control.inventory_release = asyncio.Event()
        transport = httpx.ASGITransport(app=client.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
            pending = asyncio.create_task(
                http.get("/api/models?refresh=true", headers={"x-test-user": "alice"})
            )
            await control.inventory_entered.wait()
            current = store.get_account(owner="alice", account_id=account.id)
            store.persist_account_discovery(
                owner="alice",
                account_id=account.id,
                connection_id=connection.id,
                label=current.label,
                auth_method="api_key",
                auth_class="metered",
                credentials={"type": "api_key", "key": "new"},
                safe_identity={},
                model_routes=[
                    {
                        "id": "pmr_inventory_new",
                        "provider_model_id": "new-model",
                        "display_name": "new-model",
                        "operations": ["chat.complete"],
                        "capabilities": {},
                        "provenance": {"authority": "fixture"},
                    }
                ],
                expected_account_revision=current.revision,
                expected_credential_revision=current.credential_version + 1,
            )
            control.inventory_release.set()
            return await pending, await http.get(
                "/api/models?refresh=true", headers={"x-test-user": "alice"}
            )

    first, second = asyncio.run(fetch_with_rotation())

    assert first.status_code == 200
    assert second.status_code == 200
    assert control.inventory_calls == 2
    assert store.credential_access(owner="alice", account_id=account.id).credentials["key"] == "new"


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


def test_connection_policy_patch_is_revisioned_and_public(provider_api):
    client, store, _ = provider_api
    connection = _create_connection(client).json()
    response = client.patch(
        f"/api/v1/providers/connections/{connection['id']}",
        headers={
            "Idempotency-Key": "connection-pool-policy",
            "If-Match": f'"{connection["revision"]}"',
        },
        json={
            "account_pool_policy": {
                "version": 1,
                "mode": "round_robin",
                "same_model_fallback": True,
            },
        },
    )
    assert response.status_code == 200
    assert response.json()["account_pool_policy"] == {
        "version": 1,
        "mode": "round_robin",
        "same_model_fallback": True,
    }
    assert response.json()["revision"] == connection["revision"] + 1
    assert store.get_connection(owner="alice", connection_id=connection["id"]).account_pool_policy == {
        "version": 1,
        "mode": "round_robin",
        "same_model_fallback": True,
    }


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
    control.account_model_ids = ["model-before"]
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
    } == set()

    control.account_model_ids = ["model-after"]
    catalogue = client.get(
        "/api/models",
        params={"refresh": "true"},
    )
    assert catalogue.status_code == 200
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
    control.account_model_ids = ["subscription-model"]
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


def test_kilo_device_sign_in_requires_the_canonical_gateway_url(oauth_provider_api):
    client, _, control = oauth_provider_api
    legacy = client.post(
        "/api/v1/providers/connections",
        headers={"Idempotency-Key": "kilo-legacy-create"},
        json={
            "family_id": "kilo",
            "adapter_id": "models-dev-openai-compatible",
            "kind": "official",
            "billing_lane": "metered_api",
            "label": "Existing Kilo route",
            "url": "https://api.kilo.ai/api/gateway",
        },
    )
    assert legacy.status_code == 201
    rejected = client.post(
        f"/api/v1/providers/connections/{legacy.json()['id']}/oauth/start",
        headers={"Idempotency-Key": "kilo-legacy-device-login"},
        json={"label": "Kilo account", "method": 0, "inputs": {}},
    )
    assert rejected.status_code == 422
    assert "canonical Gateway endpoint" in rejected.json()["detail"]
    assert control.flows == {}

    canonical = client.post(
        "/api/v1/providers/connections",
        headers={"Idempotency-Key": "kilo-canonical-create"},
        json={
            "family_id": "kilo",
            "adapter_id": "models-dev-openai-compatible",
            "kind": "official",
            "billing_lane": "metered_api",
            "label": "New Kilo route",
            "url": "https://ai-gateway.kilo.ai/api/v1",
        },
    )
    assert canonical.status_code == 201
    started = client.post(
        f"/api/v1/providers/connections/{canonical.json()['id']}/oauth/start",
        headers={"Idempotency-Key": "kilo-canonical-device-login"},
        json={"label": "Kilo account", "method": 0, "inputs": {}},
    )
    assert started.status_code == 201
    assert started.json()["status"] == "pending"


def test_llmgateway_browser_key_families_require_the_canonical_endpoint(oauth_provider_api):
    client, _, control = oauth_provider_api
    for family, lane, kind, key in (
        ("llmgateway", "metered_api", "official", "gateway"),
        ("llmgateway-devpass", "subscription", "subscription", "devpass"),
    ):
        legacy = client.post(
            "/api/v1/providers/connections",
            headers={"Idempotency-Key": f"{key}-legacy-create"},
            json={
                "family_id": family,
                "adapter_id": "models-dev-openai-compatible",
                "kind": kind,
                "billing_lane": lane,
                "label": f"Old {family} route",
                "url": "https://gateway.example/v1",
            },
        )
        assert legacy.status_code == 201
        rejected = client.post(
            f"/api/v1/providers/connections/{legacy.json()['id']}/oauth/start",
            headers={"Idempotency-Key": f"{key}-legacy-oauth"},
            json={"label": f"{family} account", "method": 0, "inputs": {}},
        )
        assert rejected.status_code == 422
        assert "canonical API endpoint" in rejected.json()["detail"]

        canonical = client.post(
            "/api/v1/providers/connections",
            headers={"Idempotency-Key": f"{key}-canonical-create"},
            json={
                "family_id": family,
                "adapter_id": "models-dev-openai-compatible",
                "kind": kind,
                "billing_lane": lane,
                "label": f"New {family} route",
                "url": "https://api.llmgateway.io/v1",
            },
        )
        assert canonical.status_code == 201
        started = client.post(
            f"/api/v1/providers/connections/{canonical.json()['id']}/oauth/start",
            headers={"Idempotency-Key": f"{key}-canonical-oauth"},
            json={"label": f"{family} account", "method": 0, "inputs": {}},
        )
        assert started.status_code == 201
        assert started.json()["status"] == "pending"


def test_mimo_browser_api_credential_persists_on_subscription_lane(oauth_provider_api):
    client, store, control = oauth_provider_api
    connection = store.create_connection(
        owner="alice",
        family_id="xiaomi",
        adapter_id="mimo-native",
        kind="subscription",
        billing_lane="subscription",
        label="MiMo Token Plan",
    )
    started = client.post(
        f"/api/v1/providers/connections/{connection.id}/oauth/start",
        headers={"Idempotency-Key": "mimo-browser-subscription-start"},
        json={"label": "MiMo browser account", "method": 0, "inputs": {}},
    )
    assert started.status_code == 201, started.text
    flow_id = started.json()["flow_id"]
    completed = client.post(
        f"/api/v1/providers/oauth/flows/{flow_id}/callback",
        json={"state": control.flows[flow_id]["state"], "code": "synthetic-mimo-code"},
    )
    assert completed.status_code == 200, completed.text
    assert completed.json()["status"] == "complete", f"{completed.text}; validation={getattr(control, 'last_account_validation', None)}"
    assert "synthetic-token-plan-key" not in completed.text
    accounts = store.list_accounts(owner="alice", connection_id=connection.id)
    assert len(accounts) == 1
    assert accounts[0].auth_method == "api_key"
    assert control.last_account_validation["acquisitionMethod"] == "browser"
    assert control.last_account_validation["credentialKind"] == "api"


def test_mimo_browser_api_reauth_replaces_the_same_account_with_cas(oauth_provider_api):
    client, store, control = oauth_provider_api
    connection = store.create_connection(
        owner="alice",
        family_id="xiaomi",
        adapter_id="mimo-native",
        kind="subscription",
        billing_lane="subscription",
        label="MiMo Token Plan",
    )
    account = store.create_account(
        owner="alice",
        connection_id=connection.id,
        label="Existing MiMo plan",
        auth_method="api_key",
        auth_class="subscription",
        credentials={
            "type": "api",
            "key": "synthetic-old-token-plan-key",
            "metadata": {"base_url": "https://token-plan-cn.xiaomimimo.com/v1"},
        },
    )
    assert account.acquisition_method is None
    assert client.get(f"/api/v1/providers/accounts/{account.id}").json()["acquisition_method"] is None
    response = client.post(
        f"/api/v1/providers/accounts/{account.id}/oauth/reauth",
        headers={"If-Match": f'"{account.revision}"', "Idempotency-Key": "mimo-token-plan-reauth"},
        json={"label": "Existing MiMo plan", "method": 0, "inputs": {}},
    )
    assert response.status_code == 201, response.text
    flow_id = response.json()["flow_id"]
    completed = client.post(
        f"/api/v1/providers/oauth/flows/{flow_id}/callback",
        json={"state": control.flows[flow_id]["state"], "code": "synthetic-mimo-reauth-code"},
    )
    assert completed.status_code == 200, completed.text
    assert completed.json()["status"] == "complete", completed.text
    rows = store.list_accounts(owner="alice", connection_id=connection.id)
    assert len(rows) == 1
    assert rows[0].id == account.id
    assert rows[0].credential_version == 2
    assert rows[0].auth_method == "api_key"
    assert rows[0].acquisition_method == "browser"
    assert completed.json()["account"]["acquisition_method"] == "browser"


def test_oauth_callback_is_single_flight_across_concurrent_endpoint_requests(
    oauth_provider_api,
):
    client, store, control = oauth_provider_api
    connection = store.create_connection(
        owner="alice",
        family_id="openai",
        adapter_id="openai-responses",
        kind="subscription",
        billing_lane="subscription",
        label="ChatGPT subscriptions",
    )
    started = client.post(
        f"/api/v1/providers/connections/{connection.id}/oauth/start",
        headers={"Idempotency-Key": "oauth-concurrent-start"},
        json={"label": "Concurrent", "method": 0, "inputs": {}},
    )
    assert started.status_code == 201
    flow_id = started.json()["flow_id"]
    callback = f"/api/v1/providers/oauth/flows/{flow_id}/callback"
    payload = {"state": control.flows[flow_id]["state"], "code": "same-code"}

    def complete_json():
        return client.post(callback, json=payload)

    def complete_browser():
        return client.get(
            "/api/v1/providers/oauth/callback",
            params={
                "flow_id": flow_id,
                "state": payload["state"],
                "code": payload["code"],
            },
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        responses = list(
            executor.map(
                lambda index: complete_json() if index == 0 else complete_browser(),
                range(2),
            )
        )

    assert [response.status_code for response in responses] == [200, 200]
    assert responses[0].json()["status"] == "complete"
    assert "connected" in responses[1].text
    assert control.callback_calls == 1
    assert len(store.list_accounts(owner="alice", connection_id=connection.id)) == 1


def test_oauth_validation_failure_is_terminal_and_successful_commit_is_idempotent(
    oauth_provider_api,
):
    client, store, control = oauth_provider_api
    connection = store.create_connection(
        owner="alice",
        family_id="openai",
        adapter_id="openai-responses",
        kind="subscription",
        billing_lane="subscription",
        label="ChatGPT subscriptions",
    )
    control.account_model_ids = ["subscription-model"]

    original_call = control.call

    async def malformed_validation(*, method, payload, **kwargs):
        result = await original_call(method=method, payload=payload, **kwargs)
        if method.endswith("/account/validate"):
            result["modelRoutes"][0]["operations"] = ["not-an-operation"]
            result["discovery"]["models"] = result["modelRoutes"]
        return result

    control.call = malformed_validation
    started = client.post(
        f"/api/v1/providers/connections/{connection.id}/oauth/start",
        headers={"Idempotency-Key": "oauth-invalid-result"},
        json={"label": "Invalid", "method": 0, "inputs": {}},
    )
    flow_id = started.json()["flow_id"]
    failed = client.post(
        f"/api/v1/providers/oauth/flows/{flow_id}/callback",
        json={"state": control.flows[flow_id]["state"], "code": "bad-result"},
    )
    assert failed.status_code == 200, failed.text
    assert failed.json()["status"] == "failed"
    assert failed.json()["error_code"] == "oauth_validation_failed"
    assert store.list_accounts(owner="alice", connection_id=connection.id) == []

    control.call = original_call
    started = client.post(
        f"/api/v1/providers/connections/{connection.id}/oauth/start",
        headers={"Idempotency-Key": "oauth-idempotent-result"},
        json={"label": "Idempotent", "method": 0, "inputs": {}},
    )
    flow_id = started.json()["flow_id"]
    callback_payload = {
        "state": control.flows[flow_id]["state"],
        "code": "same-result",
    }
    first = client.post(
        f"/api/v1/providers/oauth/flows/{flow_id}/callback",
        json=callback_payload,
    )
    second = client.post(
        f"/api/v1/providers/oauth/flows/{flow_id}/callback",
        json=callback_payload,
    )
    assert first.status_code == second.status_code == 200
    assert first.json()["status"] == second.json()["status"] == "complete"
    assert first.json()["account"]["id"] == second.json()["account"]["id"]
    assert len(store.list_accounts(owner="alice", connection_id=connection.id)) == 1


def test_oauth_discovery_transient_retries_then_commits_from_engine_result(
    oauth_provider_api,
):
    client, store, control = oauth_provider_api
    connection = store.create_connection(
        owner="alice", family_id="openai", adapter_id="openai-responses",
        kind="subscription", billing_lane="subscription", label="Retryable",
    )
    original_call = control.call
    attempts = 0

    async def transient_then_success(*, method, payload, **kwargs):
        nonlocal attempts
        result = await original_call(method=method, payload=payload, **kwargs)
        if method.endswith("/account/validate") and attempts < 2:
            attempts += 1
            result["modelRoutes"] = []
            result["discovery"] = {
                **result["discovery"], "status": "unavailable", "models": [],
                "authoritative": False, "retryable": True, "retryAfterMs": 0,
            }
        return result

    control.call = transient_then_success
    started = client.post(
        f"/api/v1/providers/connections/{connection.id}/oauth/start",
        headers={"Idempotency-Key": "oauth-transient-success"},
        json={"label": "Retryable", "method": 0, "inputs": {}},
    ).json()
    flow_id = started["flow_id"]
    callback = client.post(
        f"/api/v1/providers/oauth/flows/{flow_id}/callback",
        json={"state": control.flows[flow_id]["state"], "code": "retry-code"},
    )
    assert callback.json()["status"] == "running", callback.text
    for _ in range(2):
        polled = client.get(f"/api/v1/providers/oauth/flows/{flow_id}")
    assert polled.json()["status"] == "complete"
    assert attempts == 2
    assert len(store.list_accounts(owner="alice", connection_id=connection.id)) == 1


def test_oauth_discovery_transient_budget_terminates_on_third_attempt(oauth_provider_api):
    client, store, control = oauth_provider_api
    connection = store.create_connection(
        owner="alice", family_id="openai", adapter_id="openai-responses",
        kind="subscription", billing_lane="subscription", label="Retry budget",
    )
    original_call = control.call
    attempts = 0

    async def always_transient(*, method, payload, **kwargs):
        nonlocal attempts
        result = await original_call(method=method, payload=payload, **kwargs)
        if method.endswith("/account/validate"):
            attempts += 1
            result["modelRoutes"] = []
            result["discovery"] = {
                **result["discovery"], "status": "unavailable", "models": [],
                "authoritative": False, "retryable": True, "retryAfterMs": 0,
            }
        return result

    control.call = always_transient
    started = client.post(
        f"/api/v1/providers/connections/{connection.id}/oauth/start",
        headers={"Idempotency-Key": "oauth-transient-budget"},
        json={"label": "Retry budget", "method": 0, "inputs": {}},
    ).json()
    flow_id = started["flow_id"]
    flow_url = f"/api/v1/providers/oauth/flows/{flow_id}"
    assert client.post(
        flow_url + "/callback",
        json={"state": control.flows[flow_id]["state"], "code": "budget-code"},
    ).json()["status"] == "running", client.get(flow_url).text
    assert client.get(flow_url).json()["status"] == "running"
    terminal = client.get(flow_url).json()
    assert terminal["status"] == "failed"
    assert terminal["error_code"] == "oauth_discovery_unavailable"
    assert attempts == 3
    assert store.list_accounts(owner="alice", connection_id=connection.id) == []


def test_oauth_discovery_long_retry_after_is_terminal_without_next_validation(oauth_provider_api):
    client, store, control = oauth_provider_api
    connection = store.create_connection(
        owner="alice", family_id="openai", adapter_id="openai-responses",
        kind="subscription", billing_lane="subscription", label="Long retry",
    )
    original_call = control.call
    attempts = 0

    async def long_retry(*, method, payload, **kwargs):
        nonlocal attempts
        result = await original_call(method=method, payload=payload, **kwargs)
        if method.endswith("/account/validate"):
            attempts += 1
            result["modelRoutes"] = []
            result["discovery"] = {
                **result["discovery"], "status": "unavailable", "models": [],
                "authoritative": False, "retryable": True, "retryAfterMs": 86_400_000,
            }
        return result

    control.call = long_retry
    started = client.post(
        f"/api/v1/providers/connections/{connection.id}/oauth/start",
        headers={"Idempotency-Key": "oauth-long-retry-after"},
        json={"label": "Long retry", "method": 0, "inputs": {}},
    ).json()
    flow_id = started["flow_id"]
    response = client.post(
        f"/api/v1/providers/oauth/flows/{flow_id}/callback",
        json={"state": control.flows[flow_id]["state"], "code": "long-retry-code"},
    )
    assert response.json()["status"] == "failed"
    assert response.json()["error_code"] == "oauth_discovery_unavailable"
    assert attempts == 1


def test_oauth_flow_deadline_reclaims_abandoned_flow_without_browser_poll(
    oauth_provider_api,
):
    client, store, control = oauth_provider_api
    connection = store.create_connection(
        owner="alice",
        family_id="openai",
        adapter_id="openai-responses",
        kind="subscription",
        billing_lane="subscription",
        label="ChatGPT subscriptions",
    )
    started = client.post(
        f"/api/v1/providers/connections/{connection.id}/oauth/start",
        headers={"Idempotency-Key": "oauth-expiry-owner"},
        json={"label": "Expiry", "method": 0, "inputs": {}},
    )
    flow_id = started.json()["flow_id"]
    flow = provider_v1_module._PROVIDER_OAUTH_FLOWS.get(flow_id, owner="alice")
    flow.expires_at = NOW.replace(tzinfo=timezone.utc) - timedelta(seconds=1)

    expired = client.get(f"/api/v1/providers/oauth/flows/{flow_id}")
    assert expired.status_code == 200
    assert expired.json()["status"] == "expired"
    assert expired.json()["error_code"] == "oauth_expired"
    assert control.leases[-1].released is True


def test_oauth_commit_wins_when_cache_cleanup_fails(oauth_provider_api, monkeypatch):
    client, store, control = oauth_provider_api
    connection = store.create_connection(
        owner="alice",
        family_id="openai",
        adapter_id="openai-responses",
        kind="subscription",
        billing_lane="subscription",
        label="ChatGPT subscriptions",
    )
    control.account_model_ids = ["cleanup-model"]
    started = client.post(
        f"/api/v1/providers/connections/{connection.id}/oauth/start",
        headers={"Idempotency-Key": "oauth-cleanup-owner"},
        json={"label": "Cleanup", "method": 0, "inputs": {}},
    )
    flow_id = started.json()["flow_id"]
    monkeypatch.setattr(
        provider_v1_module,
        "_invalidate_inventory_cache",
        lambda **_: (_ for _ in ()).throw(RuntimeError("cache cleanup failed")),
    )
    completed = client.post(
        f"/api/v1/providers/oauth/flows/{flow_id}/callback",
        json={"state": control.flows[flow_id]["state"], "code": "commit-first"},
    )
    assert completed.status_code == 200
    assert completed.json()["status"] == "complete"
    assert len(store.list_accounts(owner="alice", connection_id=connection.id)) == 1


def test_oauth_cancel_during_validation_wins_before_commit(oauth_provider_api):
    client, store, control = oauth_provider_api
    connection = store.create_connection(
        owner="alice",
        family_id="openai",
        adapter_id="openai-responses",
        kind="subscription",
        billing_lane="subscription",
        label="ChatGPT subscriptions",
    )
    control.account_model_ids = ["cancel-during-validation"]
    validation_started = threading.Event()
    release_validation = threading.Event()
    original_call = control.call

    async def delayed_validation(*, method, payload, **kwargs):
        if method.endswith("/account/validate"):
            validation_started.set()
            await asyncio.to_thread(release_validation.wait, 2)
        return await original_call(method=method, payload=payload, **kwargs)

    control.call = delayed_validation
    started = client.post(
        f"/api/v1/providers/connections/{connection.id}/oauth/start",
        headers={"Idempotency-Key": "oauth-cancel-validation"},
        json={"label": "Cancel validation", "method": 0, "inputs": {}},
    )
    flow_id = started.json()["flow_id"]
    payload = {"state": control.flows[flow_id]["state"], "code": "cancel-before-commit"}
    with ThreadPoolExecutor(max_workers=1) as executor:
        callback = executor.submit(
            lambda: client.post(
                f"/api/v1/providers/oauth/flows/{flow_id}/callback", json=payload
            )
        )
        assert validation_started.wait(2)
        cancelled = client.delete(f"/api/v1/providers/oauth/flows/{flow_id}")
        release_validation.set()
        completed = callback.result(timeout=3)

    assert cancelled.status_code == 200
    assert completed.status_code == 200
    assert completed.json()["status"] in {"cancelled", "expired"}
    assert store.list_accounts(owner="alice", connection_id=connection.id) == []


def test_cancelled_oauth_lock_waiter_reclaims_ownership_reference():
    async def exercise():
        flow_id = "cancelled-lock-waiter"
        lock = await provider_v1_module._acquire_global_oauth_lock(flow_id)
        waiter = asyncio.create_task(
            provider_v1_module._acquire_global_oauth_lock(flow_id)
        )
        await asyncio.sleep(0.02)
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        provider_v1_module._release_global_oauth_lock(flow_id, lock)
        assert flow_id not in provider_v1_module._OAUTH_COMPLETION_LOCKS
        assert flow_id not in provider_v1_module._OAUTH_COMPLETION_LOCK_REFS

    asyncio.run(exercise())


def test_provider_models_hide_invisible_and_unentitled_routes(provider_api):
    client, store, factory = provider_api
    connection = store.create_connection(
        owner="alice",
        family_id="openai",
        adapter_id="openai-responses",
        kind="official",
        billing_lane="metered_api",
        label="OpenAI API",
    )
    account = store.create_account(
        owner="alice",
        connection_id=connection.id,
        account_id="pac_models_filter",
        label="Models filter",
        auth_method="api_key",
        auth_class="metered",
        credentials={"type": "api_key", "key": "fixture"},
    )
    visible = store.create_model_route(
        owner="alice",
        connection_id=connection.id,
        model_route_id="pmr_models_visible",
        provider_model_id="visible-model",
        display_name="Visible",
        operations=("chat.complete",),
    )
    hidden = store.create_model_route(
        owner="alice",
        connection_id=connection.id,
        model_route_id="pmr_models_hidden",
        provider_model_id="hidden-model",
        display_name="Hidden",
        operations=("chat.complete",),
    )
    store.set_entitlement(
        owner="alice", account_id=account.id, model_route_id=visible.id, eligible=True
    )
    with factory() as db:
        db.get(ProviderModelRoute, hidden.id).visibility = "hidden"
        db.commit()

    response = client.get("/api/v1/providers/models")

    assert response.status_code == 200
    assert [row["id"] for row in response.json()["models"]] == [visible.id]


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
            "users": {"alice": {}, "bob": {}, "carol": {}},
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
        account = store.create_account(
            owner="alice",
            connection_id=connection.id,
            account_id=f"pac_same_model_{index}",
            label="Shared fixture",
            auth_method="api_key",
            auth_class="metered",
            credentials={"type": "api_key", "key": f"shared-fixture-{index}"},
        )
        store.set_entitlement(
            owner="alice",
            account_id=account.id,
            model_route_id=route.id,
            eligible=True,
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


def test_claude_oauth_policy_pin_rejects_late_disable(oauth_provider_api, monkeypatch, tmp_path):
    from routes import prefs_routes
    monkeypatch.setattr(prefs_routes, "PREFS_FILE", str(tmp_path / "prefs-policy.json"))
    client, store, control = oauth_provider_api
    original = FakeOAuthLease.call
    async def claude_catalog(self, method, payload):
        result = await original(self, method, payload)
        if method.endswith("/catalog"):
            result["families"].append({"id":"anthropic", "displayName":"Anthropic", "adapters":["anthropic-messages"], "kinds":["subscription"], "billingLanes":["subscription"], "authMethods":[{"id":"oauth:0", "type":"oauth", "label":"Browser login", "acquisitionMethod":"browser", "credentialKinds":["oauth"], "billingLanes":["subscription"]}], "modelCount":1})
        return result
    monkeypatch.setattr(FakeOAuthLease, "call", claude_catalog)
    monkeypatch.setattr(FakeOAuthLease, "require_method_policy_contract", lambda self: None, raising=False)
    enabled = client.get("/api/v1/providers/method-policy")
    assert enabled.status_code == 200
    connection = store.create_connection(owner="alice", family_id="anthropic", adapter_id="anthropic-messages", kind="subscription", billing_lane="subscription", label="Claude")
    started = client.post(f"/api/v1/providers/connections/{connection.id}/oauth/start", headers={"Idempotency-Key":"claude-policy-pin"}, json={"label":"Claude", "method":0, "inputs":{}})
    assert started.status_code == 201, started.text
    flow_id = started.json()["flow_id"]
    disabled = client.put("/api/v1/providers/method-policy", json={"enabled":False, "expected_revision":1})
    assert disabled.status_code == 200
    callback = client.post(f"/api/v1/providers/oauth/flows/{flow_id}/callback", json={"state":control.flows[flow_id]["state"], "code":"synthetic"})
    assert callback.status_code in (200, 409, 410)
    assert store.list_accounts(owner="alice", connection_id=connection.id) == []


def test_comfy_family_requires_installed_media_service(provider_api, monkeypatch):
    from routes import provider_v1_routes

    client, store, _ = provider_api
    monkeypatch.setattr(provider_v1_routes, "bundled_provider_family_catalog", lambda: {
        "families": [{"id": "comfyui"}, {"id": "openai"}],
    })
    state = client.app.state
    assert [f["id"] for f in client.get("/api/v1/providers/families").json()["families"]] == ["openai"]
    state.provider_store = store
    state.account_request_barrier = object()
    state.session_manager = object()
    state.media_human_service_factory = lambda request, workspace: None
    response = client.get("/api/v1/providers/families")
    assert [f["id"] for f in response.json()["families"]] == ["comfyui", "openai"]
    assert response.headers["cache-control"] == "private, no-store"
    state.provider_store = object()
    assert [f["id"] for f in client.get("/api/v1/providers/families").json()["families"]] == ["openai"]
