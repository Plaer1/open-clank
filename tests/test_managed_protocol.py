from __future__ import annotations

import asyncio
import json

import pytest

from src.openclank.acp_client import ACPClient
from src.openclank.managed_protocol import (
    SCHEMA_SHA256,
    ManagedProtocolError,
    client_capability_offer,
    validate_initialize_result,
)


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
