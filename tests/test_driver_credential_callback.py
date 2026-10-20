from __future__ import annotations

import base64
import hashlib
import json
import asyncio
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import jsonschema
import pytest

from src.openclank.driver_credential_callback import (
    CredentialCallbackBroker,
    CredentialCallbackError,
    CredentialCallbackRequest,
    CredentialCallbackServer,
    DriverCallbackRegistration,
    provider_credential_loader,
)


ROOT = __import__("pathlib").Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class Grant:
    lease_id: str
    expires_at: datetime
    credentials: dict[str, str]


def request(request_id: str = "a" * 32) -> CredentialCallbackRequest:
    unsigned = {
        "schema_version": 1,
        "request_id": request_id,
        "lease_id": "lease-1",
        "owner_subject_id": "subject-1",
        "runtime_id": "runtime-1",
        "runtime_epoch": "b" * 32,
        "runtime_generation": 2,
        "driver_pid": 42,
        "driver_start_token": "start-1",
        "operation": "provider.http",
    }
    digest = hashlib.sha256(json.dumps(unsigned, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return CredentialCallbackRequest.from_wire({**unsigned, "request_sha256": digest})


def test_one_shot_response_is_canonical_and_replay_does_not_disclose():
    current = datetime(2026, 1, 1, tzinfo=timezone.utc)
    broker = CredentialCallbackBroker()
    req = request()
    response = broker.resolve(req, lambda _: Grant("lease-1", current + timedelta(seconds=10), {"token": "secret", "z": "last"}), now=current)
    assert response["ok"] is True
    decoded = json.loads(base64.urlsafe_b64decode(response["credential_json_base64url"] + "=="))
    assert decoded == {"token": "secret", "z": "last"}
    replay = broker.resolve(req, lambda _: pytest.fail("loader must not run twice"), now=current)
    assert replay["error"]["code"] == "credential_lease_used"


def test_hash_binding_expiry_and_loader_fail_closed():
    req = request()
    malformed = req.to_wire()
    malformed["operation"] = "provider.other"
    with pytest.raises(CredentialCallbackError, match="binding"):
        CredentialCallbackRequest.from_wire(malformed)
    nul = req.to_wire()
    nul["driver_start_token"] = "bad\x00token"
    with pytest.raises(CredentialCallbackError, match="malformed"):
        CredentialCallbackRequest.from_wire(nul)
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    broker = CredentialCallbackBroker()
    expired = broker.resolve(req, lambda _: Grant("lease-1", now, {"token": "secret"}), now=now)
    assert expired["error"]["code"] == "credential_expired"
    unavailable = broker.resolve(request("e" * 32), lambda _: (_ for _ in ()).throw(RuntimeError()), now=now)
    assert unavailable["error"]["code"] == "credential_unavailable"
    oversized = broker.resolve(request("d" * 32), lambda _: Grant("lease-1", now + timedelta(seconds=1), {"token": "x" * (46 * 1024)}), now=now)
    assert oversized["error"]["code"] == "credential_too_large"


def test_provider_loader_binds_subject_holder_and_operation():
    class Store:
        def __init__(self):
            self.calls = []

        def consume_credential_lease(self, **kwargs):
            self.calls.append(kwargs)
            return Grant(
                kwargs["lease_id"],
                datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=10),
                {"token": "opaque"},
            )

    store = Store()
    loader = provider_credential_loader(
        store,
        resolve_owner_subject=lambda subject: {"subject-1": "alice"}.get(subject),
        holder_id="driver-holder",
    )
    grant = loader(request())
    assert grant.credentials == {"token": "opaque"}
    assert store.calls == [
        {"owner": "alice", "lease_id": "lease-1", "holder_id": "driver-holder"}
    ]

    unsupported = request("c" * 32)
    unsupported = CredentialCallbackRequest(
        **{**unsupported.__dict__, "operation": "provider.refresh"},
    )
    with pytest.raises(CredentialCallbackError, match="operation"):
        loader(unsupported)

    unknown = request("b" * 32)
    unknown = CredentialCallbackRequest(
        **{**unknown.__dict__, "owner_subject_id": "subject-unknown"},
    )
    with pytest.raises(CredentialCallbackError, match="owner"):
        loader(unknown)


def test_callback_schema_accepts_request_success_and_failure():
    schema = json.loads((ROOT / "contracts/openclank/driver-credential-callback-v1.schema.json").read_text())
    for path in (
        {"schema_version": 1, "request_id": "a" * 32, "lease_id": "l", "owner_subject_id": "o", "runtime_id": "r", "runtime_epoch": "b" * 32, "runtime_generation": 0, "driver_pid": 1, "driver_start_token": "s", "operation": "provider.http", "request_sha256": "c" * 64},
        {"schema_version": 1, "request_id": "a" * 32, "lease_id": "l", "ok": True, "credential_encoding": "canonical-json-utf8-base64url-nopad", "credential_json_base64url": "e30", "expires_unix_ms": 1, "uses_remaining": 1, "request_sha256": "c" * 64},
        {"schema_version": 1, "request_id": "a" * 32, "lease_id": "l", "ok": False, "error": {"code": "credential_expired", "safe_message": "expired", "retryable": False}, "request_sha256": "c" * 64},
    ):
        jsonschema.validate(path, schema)


def test_private_uds_server_enforces_identity_and_one_shot(tmp_path):
    async def scenario():
        current = datetime.now(timezone.utc)
        request_value = request("f" * 32)
        callback_parent = __import__("pathlib").Path(f"/tmp/ocb-{os.getpid()}")
        callback_parent.mkdir(mode=0o700, exist_ok=True)
        socket_path = callback_parent / "callback.sock"
        try:
            socket_path.unlink()
        except FileNotFoundError:
            pass
        server = CredentialCallbackServer(
            socket_path,
            owner_subject_id=request_value.owner_subject_id,
            runtime_id=request_value.runtime_id,
            runtime_epoch=request_value.runtime_epoch,
            runtime_generation=request_value.runtime_generation,
            driver_pid=request_value.driver_pid,
            driver_start_token=request_value.driver_start_token,
            loader=lambda _: Grant("lease-1", current + timedelta(seconds=10), {"token": "secret"}),
        )
        await server.start()
        assert socket_path.stat().st_mode & 0o777 == 0o600
        async def call(value):
            reader, writer = await asyncio.open_unix_connection(socket_path)
            body = json.dumps(value.to_wire(), separators=(",", ":")).encode()
            writer.write(len(body).to_bytes(4, "big") + body)
            await writer.drain()
            header = await reader.readexactly(4)
            payload = await reader.readexactly(int.from_bytes(header, "big"))
            writer.close()
            await writer.wait_closed()
            return json.loads(payload)
        result = await call(request_value)
        assert result["ok"] is True
        replay = await call(request_value)
        assert replay["error"]["code"] == "credential_lease_used"
        stale = request("e" * 32)
        stale = CredentialCallbackRequest(
            **{**stale.__dict__, "runtime_generation": 99},
        )
        unsigned = stale.unsigned()
        stale = CredentialCallbackRequest.from_wire({**unsigned, "request_sha256": hashlib.sha256(json.dumps(unsigned, sort_keys=True, separators=(",", ":")).encode()).hexdigest()})
        assert (await call(stale))["error"]["code"] == "stale_generation"
        await server.close()
        assert not socket_path.exists()
        callback_parent.rmdir()
    asyncio.run(scenario())


def test_registration_starts_bound_listener_and_returns_activation_metadata(tmp_path):
    class Store:
        def consume_credential_lease(self, **kwargs):
            return Grant(
                kwargs["lease_id"],
                datetime.now(timezone.utc) + timedelta(seconds=10),
                {"token": "registration-secret"},
            )

    async def scenario():
        callback_parent = __import__("pathlib").Path(f"/tmp/oc-registration-{os.getpid()}")
        callback_parent.mkdir(mode=0o700, exist_ok=True)
        registration = await DriverCallbackRegistration.create(
            callback_parent,
            owner_subject_id="subject-1",
            runtime_id="runtime-1",
            runtime_epoch="b" * 32,
            runtime_generation=2,
            driver_pid=42,
            driver_start_token="start-1",
            registration_id="e" * 32,
            provider_store=Store(),
            resolve_owner_subject=lambda subject: {"subject-1": "alice"}.get(subject),
            holder_id="driver-holder",
        )
        try:
            activation = registration.activation_payload()
            assert set(activation) == {
                "registration_id",
                "callback_endpoint",
                "callback_nonce",
                "callback_binding_sha256",
            }
            assert activation["registration_id"] == "e" * 32
            assert len(activation["callback_nonce"]) == 43
            assert len(activation["callback_binding_sha256"]) == 64
            endpoint = __import__("pathlib").Path(activation["callback_endpoint"])
            assert endpoint.exists()
            assert endpoint.stat().st_mode & 0o777 == 0o600
        finally:
            await registration.close()
        assert not endpoint.exists()
        callback_parent.rmdir()

    asyncio.run(scenario())
