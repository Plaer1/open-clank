from __future__ import annotations

import asyncio

import pytest

from src.openclank.acp_client import ACPClient
from src.openclank.managed_protocol import (
    SCHEMA_SHA256,
    METHOD_DIRECTIONS,
    ManagedProtocolError,
    MODEL_OPERATIONS,
    client_capability_offer,
    validate_engine_method_result,
    validate_managed_method_request,
    validate_initialize_result,
)


def _model_route():
    return {
        "modelID": "route-model",
        "displayName": "Route model",
        "operations": ["chat.complete"],
        "capabilities": {},
        "provenance": {},
    }


def _account_result(discovery):
    return {
        "authMethod": "api_key",
        "authClass": "metered",
        "credential": {"type": "api", "key": "test-key"},
        "safeIdentity": {},
        "modelRoutes": [_model_route()],
        "accountID": "account-1",
        "credentialRevision": 1,
        "discovery": discovery,
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


def test_generated_contract_preserves_all_methods_and_operations():
    assert len(client_capability_offer()["methods"]) == 21
    assert len(MODEL_OPERATIONS) == 15
    assert "chat.stream" in MODEL_OPERATIONS
    assert sum(direction == "host_to_engine" for direction in METHOD_DIRECTIONS.values()) == 8
    assert sum(direction == "engine_to_host" for direction in METHOD_DIRECTIONS.values()) == 13


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
