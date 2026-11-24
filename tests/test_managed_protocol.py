from __future__ import annotations

import asyncio

import pytest

from src.openclank.acp_client import ACPClient
from src.openclank.managed_protocol import (
    SCHEMA_ID,
    SCHEMA_SHA256,
    SCHEMA_VERSION,
    METHOD_DIRECTIONS,
    ManagedProtocolError,
    MODEL_OPERATIONS,
    client_capability_offer,
    validate_engine_method_result,
    validate_managed_method_request,
    validate_managed_method_result,
    validate_initialize_result,
)


def _model_route():
    return {
        "modelID": "route-model",
        "displayName": "Route model",
        "operations": ["chat.complete"],
        "capabilities": {},
        "provenance": {"authority": "managed-engine"},
    }


def _account_result(discovery):
    return {
        "authMethod": "api_key",
        "acquisitionMethod": "manual-key",
        "credentialKind": "api",
        "authClass": "metered",
        "credential": {"type": "api", "key": "test-key"},
        "safeIdentity": {},
        "modelRoutes": discovery["models"],
        "accountID": discovery["accountID"],
        "credentialRevision": discovery["credentialRevision"],
        "discovery": discovery,
    }


def _account_request(auth_method="api_key"):
    credential = {"type": "api", "key": "test-key"}
    if auth_method == "oauth":
        credential = {"type": "oauth", "refresh": "refresh-token", "access": "access-token", "expires": 1}
    return {
        "connection": {
            "familyID": "openai",
            "adapterID": "openai-chat",
            "kind": "official",
            "billingLane": "metered_api",
            "settings": {},
        },
        "authMethod": auth_method,
        "acquisitionMethod": "browser" if auth_method == "oauth" else "manual-key",
        "credentialKind": "oauth" if auth_method == "oauth" else "api",
        "credential": credential,
        "accountID": "account-1",
        "credentialRevision": 1,
    }


def _result(overrides=None):
    declaration = client_capability_offer()
    declaration.update(overrides or {})
    return {
        "protocolVersion": 1,
        "agentInfo": {"name": "Open Clank Engine"},
        "_meta": {"openclankManaged": declaration},
    }


def test_exact_managed_capability_declaration_is_required():
    parsed = validate_initialize_result(_result())
    assert parsed.schemaHash == SCHEMA_SHA256
    assert parsed.providerAuthContract == 1


def test_old_capability_offer_without_provider_auth_revision_fails_closed():
    result = _result()
    result["_meta"]["openclankManaged"].pop("providerAuthContract")
    with pytest.raises(ManagedProtocolError):
        validate_initialize_result(result)


def test_generated_contract_preserves_all_methods_and_operations():
    methods = client_capability_offer()["methods"]
    expected = {
        "_openclank/provider-store/v1/actor-route/preflight", "_openclank/provider-store/v1/account/bind", "_openclank/provider-store/v1/account/commit",
        "_openclank/provider-store/v1/account/attempt", "_openclank/provider-store/v1/credential/lease",
        "_openclank/provider-store/v1/credential/replace", "_openclank/provider-store/v1/refresh/acquire",
        "_openclank/provider-store/v1/refresh/renew", "_openclank/provider-store/v1/refresh/commit",
        "_openclank/provider-store/v1/refresh/abort", "_openclank/provider-control/v1/catalog",
        "_openclank/provider-control/v1/connection/validate", "_openclank/provider-control/v1/account/validate",
        "_openclank/provider-control/v1/oauth/start", "_openclank/provider-control/v1/oauth/poll",
        "_openclank/provider-control/v1/oauth/callback", "_openclank/provider-control/v1/oauth/cancel",
        "_openclank/operations/v1/journal/cas", "_openclank/operations/v1/artifact/read",
        "_openclank/operations/v1/artifact/write", "_openclank/operations/v1/executor/invoke",
        "_openclank/operations/v1/execute", "_openclank/operations/v1/cancel",
        "_openclank/session/v1/cwd/change", "_openclank/session/v1/binding/read",
        "_openclank/session/v1/code-context/read", "_openclank/session/v1/settings/effective",
        "_openclank/session/v1/turn/status", "_openclank/session/v1/tool/admit", "_openclank/session/v1/execution/finish", "_openclank/session/v1/turn/start", "_openclank/session/v1/turn/finish",
        "_openclank/session/v1/input-policy/resolve",
        "_openclank/history/v1/query", "_openclank/history/v1/mutate",
        "_openclank/session/v1/git-index/recheck", "_openclank/session/v1/git-index/finish",
        "_openclank/session/v1/git-commit/recheck", "_openclank/session/v1/git-commit/finish",
        "_openclank/session/v1/git-cleanup/recheck", "_openclank/session/v1/git-cleanup/finish",
        "_openclank/logging/v1/admit", "_openclank/logging/v1/events", "_openclank/session/v1/goal/completed",
        "_openclank/session/v1/actor/settlement-read", "_openclank/session/v1/attachment/source",
        "_openclank/session/v1/background-job/read", "_openclank/session/v1/background-job/retire",
        "_openclank/session/v1/background-job/start", "_openclank/session/v1/background-job/terminate",
        "_openclank/session/v1/code-activity/observe", "_openclank/session/v1/context/augment",
        "_openclank/session/v1/context/delivered", "_openclank/session/v1/maintenance/ack",
        "_openclank/session/v1/maintenance/source", "_openclank/session/v1/maintenance/stage",
        "_openclank/session/v1/maintenance/summary-read", "_openclank/session/v1/mcp/health",
        "_openclank/session/v1/memory/prepared", "_openclank/session/v1/native-analysis/source",
        "_openclank/session/v1/python-kernel/call", "_openclank/session/v1/queued-source/fence",
        "_openclank/session/v1/queued-source/forward", "_openclank/session/v1/queued-source/markers",
        "_openclank/session/v1/queued-source/read", "_openclank/session/v1/queued-source/retire-marker",
        "_openclank/session/v1/recall/admitted", "_openclank/session/v1/skill/metadata-current",
        "_openclank/session/v1/task/create", "_openclank/session/v1/task/status", "_openclank/session/v1/task/read", "_openclank/session/v1/task/control", "_openclank/session/v1/task/run", "_openclank/session/v1/task/edit",
        "_openclank/session/v1/theme-emoji/call", "_openclank/session/v1/tools/capabilities",
    }
    assert len(methods) == len(expected) == 76
    assert set(methods) == expected == set(METHOD_DIRECTIONS)
    assert len(MODEL_OPERATIONS) == 16
    assert "chat.stream" in MODEL_OPERATIONS
    assert sum(direction == "host_to_engine" for direction in METHOD_DIRECTIONS.values()) == 17
    assert sum(direction == "engine_to_host" for direction in METHOD_DIRECTIONS.values()) == 53


def test_account_discovery_distinguishes_complete_from_unavailable():
    provenance = {"source": "provider-api", "observedAt": 1}
    complete = {
        "status": "complete",
        "accountID": "account-1",
        "credentialRevision": 1,
        "models": [_model_route()],
        "authoritative": True,
        "provenance": provenance,
        "freshness": "fresh",
    }
    unavailable = {
        "status": "unavailable",
        "accountID": "account-1",
        "credentialRevision": 1,
        "models": [],
        "authoritative": False,
        "provenance": provenance,
        "freshness": "unknown",
        "errorCode": "discovery_unavailable",
    }
    assert validate_engine_method_result(
        "_openclank/provider-control/v1/account/validate", _account_result(complete)
    )
    assert validate_engine_method_result(
        "_openclank/provider-control/v1/account/validate", _account_result(unavailable)
    )
    with pytest.raises(ManagedProtocolError):
        validate_engine_method_result(
            "_openclank/provider-control/v1/account/validate",
            _account_result({**unavailable, "authoritative": True}),
        )


def test_account_validation_request_supplies_authoritative_identity():
    request = _account_request()
    assert validate_managed_method_request(
        "_openclank/provider-control/v1/account/validate", request
    ) == request
    for missing in ("accountID", "credentialRevision"):
        incomplete = _account_request()
        incomplete.pop(missing)
        with pytest.raises(ManagedProtocolError):
            validate_managed_method_request(
                "_openclank/provider-control/v1/account/validate", incomplete
            )


def test_account_validation_request_accepts_post_auth_oauth():
    request = _account_request("oauth")
    assert validate_managed_method_request(
        "_openclank/provider-control/v1/account/validate", request
    ) == request


def test_account_validation_result_accepts_api_subscription_classification():
    discovery = {
        "status": "complete",
        "accountID": "account-mimo-plan",
        "credentialRevision": 1,
        "models": [],
        "authoritative": True,
        "provenance": {"source": "provider-api", "observedAt": 1},
        "freshness": "fresh",
    }
    result = {
        **_account_result(discovery),
        "authClass": "subscription",
        "acquisitionMethod": "browser",
        "credentialKind": "api",
        "endpointPolicy": "mimo-token-plan-v1",
        "credential": {
            "type": "api",
            "key": "synthetic-token-plan-key",
            "metadata": {"base_url": "https://token-plan-sgp.xiaomimimo.com/v1"},
        },
    }
    assert validate_engine_method_result(
        "_openclank/provider-control/v1/account/validate", result
    ) == result


def test_account_validation_result_preserves_unknown_legacy_acquisition():
    discovery = {
        "status": "complete",
        "accountID": "legacy-account",
        "credentialRevision": 4,
        "models": [],
        "authoritative": True,
        "provenance": {"source": "provider-api", "observedAt": 1},
        "freshness": "fresh",
    }
    result = {**_account_result(discovery), "acquisitionMethod": None}
    assert validate_engine_method_result(
        "_openclank/provider-control/v1/account/validate", result
    ) == result


@pytest.mark.parametrize("acquisition_method", ["browser", "device"])
def test_oauth_callback_wire_accepts_browser_api_key_and_oauth_device(acquisition_method):
    credential = (
        {
            "type": "api",
            "key": "synthetic-token-plan-key",
            "metadata": {"base_url": "https://token-plan-cn.xiaomimimo.com/v1"},
        }
        if acquisition_method == "browser"
        else {
            "type": "oauth",
            "refresh": "synthetic-refresh",
            "access": "synthetic-access",
            "expires": 2_000_000_000_000,
        }
    )
    result = {
        "flowID": "synthetic-flow-00000001",
        "connectionID": "connection-1",
        "providerID": "xiaomi" if acquisition_method == "browser" else "openai",
        "billingLane": "subscription",
        "mode": "add",
        "status": "complete",
        "credential": credential,
        "authMethod": "api_key" if credential["type"] == "api" else "oauth",
        "acquisitionMethod": acquisition_method,
        "credentialKind": credential["type"],
        "authClass": "subscription",
        "safeIdentity": {},
        **({"endpointPolicy": "mimo-token-plan-v1"} if credential["type"] == "api" else {}),
    }
    assert validate_engine_method_result(
        "_openclank/provider-control/v1/oauth/callback", result
    ) == result


def test_account_validation_rejects_unknown_route_authority():
    provenance = {"source": "provider-api", "observedAt": 1}
    discovery = {
        "status": "complete",
        "accountID": "account-1",
        "credentialRevision": 1,
        "models": [{**_model_route(), "provenance": {"authority": "account-discovery"}}],
        "authoritative": True,
        "provenance": provenance,
        "freshness": "fresh",
    }
    with pytest.raises(ManagedProtocolError):
        validate_engine_method_result(
            "_openclank/provider-control/v1/account/validate", _account_result(discovery)
        )


@pytest.mark.parametrize("field", ["accountID", "credentialRevision", "modelRoutes"])
def test_account_validation_rejects_discovery_echo_mismatch(field):
    provenance = {"source": "provider-api", "observedAt": 1}
    discovery = {
        "status": "complete",
        "accountID": "account-1",
        "credentialRevision": 1,
        "models": [_model_route()],
        "authoritative": True,
        "provenance": provenance,
        "freshness": "fresh",
    }
    result = _account_result(discovery)
    result[field] = {
        "accountID": "other-account",
        "credentialRevision": 2,
        "modelRoutes": [],
    }[field]
    with pytest.raises(ManagedProtocolError):
        validate_engine_method_result(
            "_openclank/provider-control/v1/account/validate", result
        )


@pytest.mark.parametrize(
    ("status", "error_code"),
    [("complete", "discovery_unavailable"), ("partial", "discovery_unavailable"), ("unavailable", None)],
)
def test_account_discovery_error_code_matches_status(status, error_code):
    discovery = {
        "status": status,
        "accountID": "account-1",
        "credentialRevision": 1,
        "models": [] if status != "complete" else [_model_route()],
        "authoritative": status == "complete",
        "provenance": {"source": "provider-api", "observedAt": 1},
        "freshness": "unknown" if status != "complete" else "fresh",
    }
    if error_code is not None:
        discovery["errorCode"] = error_code
    with pytest.raises(ManagedProtocolError):
        validate_engine_method_result(
            "_openclank/provider-control/v1/account/validate", _account_result(discovery)
        )


@pytest.mark.parametrize("missing", ["accountID", "credentialRevision", "discovery"])
def test_account_validation_rejects_legacy_missing_discovery_fields(missing):
    provenance = {"source": "provider-api", "observedAt": 1}
    complete = {
        "status": "complete",
        "accountID": "account-1",
        "credentialRevision": 1,
        "models": [_model_route()],
        "authoritative": True,
        "provenance": provenance,
        "freshness": "fresh",
    }
    result = _account_result(complete)
    result.pop(missing)
    with pytest.raises(ManagedProtocolError):
        validate_engine_method_result(
            "_openclank/provider-control/v1/account/validate", result
        )


def test_zero_byte_artifact_put_accepts_empty_chunks_and_nonzero_requires_chunks():
    empty = {
        "action": "put",
        "mediaType": "application/octet-stream",
        "contentSHA256": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
        "sizeBytes": 0,
        "chunks": [],
    }
    assert validate_managed_method_request("_openclank/operations/v1/artifact/write", empty) == empty
    with pytest.raises(ManagedProtocolError):
        validate_managed_method_request(
            "_openclank/operations/v1/artifact/write", {**empty, "sizeBytes": 1}
        )


@pytest.mark.parametrize(
    "override",
    [
        {"schemaHash": "0" * 64},
        {"providerStoreVersion": 2},
        {"operations": ["chat.stream"]},
        {"methods": []},
        {"artifactTransfer": False},
        {"methods": [*client_capability_offer()["methods"], "_openclank/future/v2"]},
        {"operations": [*client_capability_offer()["operations"], "chat.future"]},
        {"methods": [*client_capability_offer()["methods"], client_capability_offer()["methods"][0]]},
    ],
)
def test_mismatched_or_incomplete_engine_fails_closed(override):
    with pytest.raises(ManagedProtocolError):
        validate_initialize_result(_result(override))


def test_stale_v1_capability_envelope_is_refused():
    with pytest.raises(ManagedProtocolError):
        validate_initialize_result(
            _result(
                {
                    "schemaID": "https://openclank.dev/contracts/managed-provider/v1",
                    "schemaVersion": 1,
                    "schemaHash": "f9b9c7eb4dd50fa5d00f65f3662c9e9aa2702de63de94ada1e13321d131d0bbb",
                }
            )
        )
    assert SCHEMA_ID.endswith("/v2")
    assert SCHEMA_VERSION == 2


class _Writer:
    def __init__(self):
        self.value = b""

    def write(self, value):
        self.value += value

    async def drain(self):
        return None


@pytest.mark.asyncio
async def test_acp_initialize_sends_open_clank_offer_and_validates_response(monkeypatch):
    reader = asyncio.StreamReader()
    writer = _Writer()
    client = ACPClient(reader, writer)

    async def fake_send(method, params):
        assert method == "initialize"
        assert params["clientInfo"]["name"] == "openclank"
        assert params["clientCapabilities"]["_meta"]["openclankManaged"]["schemaHash"] == SCHEMA_SHA256
        return _result()

    monkeypatch.setattr(client, "_send_request", fake_send)
    response = await client.initialize()
    assert response["agentInfo"]["name"] == "Open Clank Engine"


@pytest.mark.asyncio
async def test_acp_initialize_rejects_unmanaged_binary(monkeypatch):
    client = ACPClient(asyncio.StreamReader(), _Writer())

    async def fake_send(_method, _params):
        return {"protocolVersion": 1, "agentInfo": {"name": "unknown"}}

    monkeypatch.setattr(client, "_send_request", fake_send)
    with pytest.raises(ManagedProtocolError):
        await client.initialize()


@pytest.mark.asyncio
async def test_acp_managed_operation_cancellation_emits_bound_cancel(monkeypatch):
    client = ACPClient(asyncio.StreamReader(), _Writer())
    seen = []

    async def fake_send(method, params):
        seen.append((method, params))
        if method.endswith("/execute"):
            raise asyncio.CancelledError()
        assert method.endswith("/cancel")
        return {"operationID": "pending", "rootOperationID": params["rootOperationID"], "state": "cancelled"}

    monkeypatch.setattr(client, "_send_request", fake_send)
    with pytest.raises(asyncio.CancelledError):
        await client.execute_managed_operation({"rootOperationID": "root-search", "idempotencyKey": "operation-id-123456"})
    assert seen == [
        ("_openclank/operations/v1/execute", {"rootOperationID": "root-search", "idempotencyKey": "operation-id-123456"}),
        ("_openclank/operations/v1/cancel", {"rootOperationID": "root-search", "idempotencyKey": "operation-id-123456"}),
    ]


@pytest.mark.asyncio
async def test_acp_managed_operation_cancellation_notification_is_bounded(monkeypatch):
    client = ACPClient(asyncio.StreamReader(), _Writer())

    async def fake_send(method, _params):
        if method.endswith("/execute"):
            raise asyncio.CancelledError()
        await asyncio.sleep(10)
        return {}

    monkeypatch.setattr(client, "_send_request", fake_send)
    started = asyncio.get_running_loop().time()
    with pytest.raises(asyncio.CancelledError):
        await client.execute_managed_operation({"rootOperationID": "root-search", "idempotencyKey": "operation-id-123456"})
    assert asyncio.get_running_loop().time() - started < 1


def test_actor_route_preflight_is_route_only_and_c2_uses_actor_turn_identity():
    preflight = {
        "sessionID": "ses_root",
        "rootOperationID": "root-1",
        "actorID": "worker-1",
        "parentActorID": "main",
        "expectedExecutionRevision": 4,
        "model": {"providerID": "provider-a", "modelID": "model-a"},
        "modelSource": "explicit",
        "grantID": "grant-1",
        "inheritedAccountID": "account-1",
    }
    assert validate_managed_method_request(
        "_openclank/provider-store/v1/actor-route/preflight", preflight
    ) == preflight
    with pytest.raises(ManagedProtocolError):
        validate_managed_method_request(
            "_openclank/provider-store/v1/actor-route/preflight",
            {**preflight, "owner": "model-supplied"},
        )

    identity = {
        "sessionID": "ses_root",
        "rootOperationID": "root-1",
        "actorID": "worker-1",
        "executionRevision": 5,
        "innerTurnID": "turn-1",
    }
    assert validate_managed_method_request("_openclank/session/v1/turn/start", identity) == identity
    with pytest.raises(ManagedProtocolError):
        validate_managed_method_request(
            "_openclank/session/v1/turn/start", {**identity, "hostTurnID": "stale"}
        )
    start_result = {"accepted": True, "replayed": False, **identity}
    assert validate_managed_method_result(
        "_openclank/session/v1/turn/start", start_result
    ) == start_result
    finish = {**identity, "outcome": "completed"}
    assert validate_managed_method_request("_openclank/session/v1/turn/finish", finish) == finish


def test_account_binding_scope_is_explicit_and_c2_policy_carries_native_projection():
    common = {
        "rootOperationID": "root-1",
        "scopeKind": "actor_execution",
        "sessionID": "ses_root",
        "actorID": "main",
        "executionRevision": 3,
        "modelRouteID": "route-1",
        "connectionID": "connection-1",
        "providerID": "provider-a",
        "billingLane": "subscription",
        "modelID": "model-a",
        "bootstrap": {"stableChatID": "chat-1", "owner": "alice", "rootSessionID": "ses_root",
            "sessionID": "ses_root", "actorID": "main", "rootOperationID": "root-1",
            "expectedExecutionRevision": 2, "mapRevision": 2, "mappingRevision": 1,
            "workspaceRevision": 4, "modelRouteID": "route-1"},
    }
    with pytest.raises(ManagedProtocolError):
        validate_managed_method_request("_openclank/provider-store/v1/account/bind",
            {key: value for key, value in common.items() if key != "bootstrap"})
    assert validate_managed_method_request(
        "_openclank/provider-store/v1/account/bind", common
    ) == common
    with pytest.raises(ManagedProtocolError):
        validate_managed_method_request(
            "_openclank/provider-store/v1/account/bind",
            {**common, "operationID": "aux-1"},
        )
    auxiliary = {
        key: value for key, value in common.items()
        if key not in {"scopeKind", "sessionID", "actorID", "executionRevision", "modelRouteID", "bootstrap"}
    }
    auxiliary.update({"scopeKind": "auxiliary_operation", "operationID": "aux-1"})
    assert validate_managed_method_request(
        "_openclank/provider-store/v1/account/bind", auxiliary
    ) == auxiliary
    with pytest.raises(ManagedProtocolError):
        validate_managed_method_request(
            "_openclank/provider-store/v1/account/bind", {**auxiliary, "actorID": "main"}
        )

    decision = {
        "requestID": "input-1",
        "payloadHash": "a" * 64,
        "executionRevision": 3,
        "mapRevision": 2,
        "mappingRevision": 1,
        "workspaceRevision": 4,
        "state": "ready",
        "choiceSnapshot": None,
        "guideOnly": False,
        "toolProjection": {
            "blockAllToolCalls": False,
            "disabledTools": [],
            "hiddenTools": [],
            "allowedTools": None,
        },
    }
    result = validate_managed_method_result(
        "_openclank/session/v1/input-policy/resolve",
        {
            "sessionID": "ses_root", "rootOperationID": "root-1", "actorID": "main",
            "executionRevision": 3, "innerTurnID": "turn-1", "decisions": [decision],
            "batchDisposition": "ready", "batchMode": "agent", "batchGuideOnly": False,
            "toolProjection": decision["toolProjection"],
            "nativeToolPolicy": {"*": False, "lifetools_*_read_file": True},
        },
    )
    assert result["nativeToolPolicy"] == {"*": False, "lifetools_*_read_file": True}
