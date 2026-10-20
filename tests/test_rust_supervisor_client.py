from __future__ import annotations

import asyncio

from src.openclank.generated.openclank_agent_supervisor_v1_pb2 import (
    ActivateDriverCallbackResponse,
    AckSemanticResponse,
    AttachSemanticRequest,
    Capability,
    CloseSessionResponse,
    NegotiateResponse,
    OpenOwnerRuntimeResponse,
    OpenSessionResponse,
    OwnerRef,
    ProtocolVersion,
    RuntimeBinding,
    SessionBinding,
    SemanticEvent,
    SemanticStreamItem,
    SupervisorError,
    TurnBinding,
    TurnStreamItem,
)
from src.openclank.rust_supervisor_client import RustSupervisorClient


def test_negotiate_sends_checked_proto_identity_and_maps_capabilities(monkeypatch):
    seen = {}

    class Stub:
        async def Negotiate(self, request, metadata):
            seen["request"] = request
            seen["metadata"] = metadata
            return NegotiateResponse(
                protocol=ProtocolVersion(
                    major=1,
                    minor=0,
                    schema_sha256=b"a" * 64,
                ),
                server_build="fixture",
                capabilities=[Capability(name="health", supported=True, detail_code="phased")],
            )

    client = RustSupervisorClient("/private/supervisor.sock", "session-binding")
    client._stub = Stub()
    monkeypatch.setattr(client, "connect", lambda: asyncio.sleep(0))
    result = asyncio.run(
        client.negotiate(client_build="test-client", required_features=("health",))
    )
    request = seen["request"]
    assert request.request_id and len(request.request_id) == 32
    assert request.client_build == "test-client"
    assert list(request.required_features) == ["health"]
    assert request.protocol.major == 1
    assert len(request.protocol.schema_sha256) == 64
    assert dict(result["capabilities"][0]) == {
        "name": "health",
        "supported": True,
        "detail_code": "phased",
    }
    assert result["error"] is None
    assert seen["metadata"] == (("x-open-clank-session", "session-binding"),)


def test_negotiate_preserves_typed_safe_error_without_claiming_ready(monkeypatch):
    class Stub:
        async def Negotiate(self, request, metadata):
            return NegotiateResponse(
                protocol=ProtocolVersion(major=1, minor=0, schema_sha256=b"b" * 64),
                server_build="fixture",
                error=SupervisorError(
                    code="feature_missing",
                    safe_message="feature unavailable",
                    retryable=False,
                    phase="protocol",
                    request_id=request.request_id,
                ),
            )

    client = RustSupervisorClient("/private/supervisor.sock", "session-binding")
    client._stub = Stub()
    monkeypatch.setattr(client, "connect", lambda: asyncio.sleep(0))
    result = asyncio.run(client.negotiate(required_features=("driver",)))
    assert result["error"] == {
        "code": "feature_missing",
        "safe_message": "feature unavailable",
        "retryable": False,
        "phase": "protocol",
        "request_id": result["error"]["request_id"],
    }


def test_open_owner_runtime_binds_subject_generation_and_fingerprint(monkeypatch):
    seen = {}

    class Stub:
        async def OpenOwnerRuntime(self, request, metadata):
            seen["request"] = request
            seen["metadata"] = metadata
            return OpenOwnerRuntimeResponse(
                runtime=RuntimeBinding(
                    owner=OwnerRef(
                        subject_id="subject-a",
                        username="alice",
                        auth_generation=7,
                    ),
                    runtime_id="rt-1",
                    epoch=b"e" * 16,
                    generation=4,
                    fingerprint=b"f" * 32,
                    adapter_id="mimo",
                ),
                capabilities=[Capability(name="runtime_actor", supported=True)],
            )

    client = RustSupervisorClient("/private/supervisor.sock", "session-binding")
    client._stub = Stub()
    monkeypatch.setattr(client, "connect", lambda: asyncio.sleep(0))
    result = asyncio.run(
        client.open_owner_runtime(
            subject_id="subject-a",
            username="alice",
            auth_generation=7,
            adapter_id="mimo",
            projection_fingerprint=b"p" * 32,
            expected_generation=4,
            request_id="request-1",
        )
    )
    request = seen["request"]
    assert request.meta.owner.subject_id == "subject-a"
    assert request.meta.owner.auth_generation == 7
    assert request.meta.request_id == request.meta.idempotency_key == "request-1"
    assert request.projection_fingerprint == b"p" * 32
    assert result["runtime"]["runtime_id"] == "rt-1"
    assert result["runtime"]["fingerprint"] == b"f" * 32
    assert seen["metadata"] == (("x-open-clank-session", "session-binding"),)


def test_activate_driver_callback_preserves_typed_not_ready(monkeypatch):
    seen = {}

    class Stub:
        async def ActivateDriverCallback(self, request, metadata):
            seen["request"] = request
            return ActivateDriverCallbackResponse(
                driver_ready=False,
                error=SupervisorError(
                    code="not_ready",
                    safe_message="driver process is not attached",
                    retryable=True,
                    phase="driver",
                    request_id=request.meta.request_id,
                ),
            )

    client = RustSupervisorClient("/private/supervisor.sock", "session-binding")
    client._stub = Stub()
    monkeypatch.setattr(client, "connect", lambda: asyncio.sleep(0))
    runtime = RuntimeBinding(
        owner=OwnerRef(subject_id="subject-a", username="alice", auth_generation=7),
        runtime_id="rt-1",
        epoch=b"e" * 16,
        generation=4,
        fingerprint=b"f" * 32,
        adapter_id="mimo",
    )
    result = asyncio.run(
        client.activate_driver_callback(
            subject_id="subject-a",
            username="alice",
            auth_generation=7,
            runtime=runtime,
            callback_registration_id="a" * 32,
            driver_pid=42,
            driver_start_token="start-token",
            callback_endpoint="/private/callback.sock",
            callback_nonce=b"n" * 32,
            callback_binding_sha256=b"b" * 32,
            request_id="request-2",
        )
    )
    assert result["driver_ready"] is False
    assert result["error"]["code"] == "not_ready"
    assert seen["request"].runtime.runtime_id == "rt-1"


def test_open_session_hashes_snapshot_and_preserves_runtime_binding(monkeypatch):
    seen = {}

    class Stub:
        async def OpenSession(self, request, metadata):
            seen["request"] = request
            return OpenSessionResponse(
                session=SessionBinding(
                    runtime=request.runtime,
                    session_id=request.session_id,
                    workspace_id=request.workspace_id,
                ),
                semantic_epoch=b"s" * 16,
            )

    client = RustSupervisorClient("/private/supervisor.sock", "session-binding")
    client._stub = Stub()
    monkeypatch.setattr(client, "connect", lambda: asyncio.sleep(0))
    runtime = RuntimeBinding(
        owner=OwnerRef(subject_id="subject-a", username="alice", auth_generation=7),
        runtime_id="rt-1",
        epoch=b"e" * 16,
        generation=4,
        fingerprint=b"f" * 32,
        adapter_id="mimo",
    )
    snapshot = b'{"transcript":[]}'
    result = asyncio.run(
        client.open_session(
            subject_id="subject-a",
            username="alice",
            auth_generation=7,
            runtime=runtime,
            session_id="session-1",
            workspace_id="workspace-1",
            snapshot_json=snapshot,
            transcript_revision=8,
            request_id="request-session",
        )
    )
    request = seen["request"]
    assert request.meta.protocol.schema_sha256
    assert request.snapshot_sha256 == __import__("hashlib").sha256(snapshot).digest()
    assert request.transcript_revision == 8
    assert result["session"]["session_id"] == "session-1"
    assert result["session"]["workspace_id"] == "workspace-1"
    assert result["semantic_epoch"] == b"s" * 16


def test_close_session_preserves_exact_owner_and_runtime_binding(monkeypatch):
    seen = {}

    class Stub:
        async def CloseSession(self, request, metadata):
            seen["request"] = request
            seen["metadata"] = metadata
            return CloseSessionResponse(closed=True)

    client = RustSupervisorClient("/private/supervisor.sock", "session-binding")
    client._stub = Stub()
    monkeypatch.setattr(client, "connect", lambda: asyncio.sleep(0))
    runtime = RuntimeBinding(
        owner=OwnerRef(subject_id="subject-a", username="alice", auth_generation=7),
        runtime_id="rt-1",
        epoch=b"e" * 16,
        generation=4,
        fingerprint=b"f" * 32,
        adapter_id="mimo",
    )
    session = SessionBinding(runtime=runtime, session_id="session-1", workspace_id="workspace-1")
    result = asyncio.run(
        client.close_session(
            subject_id="subject-a",
            username="alice",
            auth_generation=7,
            session=session,
            request_id="request-close",
        )
    )
    request = seen["request"]
    assert request.meta.owner.subject_id == "subject-a"
    assert request.meta.owner.auth_generation == 7
    assert request.meta.request_id == request.meta.idempotency_key == "request-close"
    assert request.session.session_id == "session-1"
    assert request.session.runtime.runtime_id == "rt-1"
    assert result == {"closed": True, "error": None}
    assert seen["metadata"] == (("x-open-clank-session", "session-binding"),)


def test_attach_semantic_maps_stream_events_and_errors(monkeypatch):
    seen = {}

    class Stream:
        def __init__(self, items):
            self.items = iter(items)

        def __aiter__(self):
            return self

        async def __anext__(self):
            try:
                return next(self.items)
            except StopIteration:
                raise StopAsyncIteration

    class Stub:
        def AttachSemantic(self, request, metadata):
            assert isinstance(request, AttachSemanticRequest)
            seen["request"] = request
            return Stream(
                [
                    SemanticStreamItem(
                        event=SemanticEvent(
                            session_id="session-1",
                            semantic_epoch=b"s" * 16,
                            seq=1,
                            op="snapshot_reset",
                            payload_json=b"{}",
                        )
                    )
                ]
            )

    client = RustSupervisorClient("/private/supervisor.sock", "session-binding")
    client._stub = Stub()
    monkeypatch.setattr(client, "connect", lambda: asyncio.sleep(0))
    runtime = RuntimeBinding(runtime_id="rt-1", epoch=b"e" * 16, generation=4, fingerprint=b"f" * 32, adapter_id="mimo")
    session = SessionBinding(runtime=runtime, session_id="session-1")

    async def collect():
        return [
            item
            async for item in client.attach_semantic(
                subject_id="subject-a",
                username="alice",
                auth_generation=7,
                session=session,
                semantic_epoch=b"s" * 16,
            )
        ]

    result = asyncio.run(collect())
    assert result == [
        {
            "type": "event",
            "owner_subject_id": "",
            "session_id": "session-1",
            "runtime_id": "",
            "runtime_epoch": "",
            "runtime_generation": 0,
            "run_id": "",
            "turn_id": "",
            "semantic_epoch": b"s" * 16,
            "seq": 1,
            "emitted_unix_ms": 0,
            "op": "snapshot_reset",
            "entity_kind": "",
            "entity_id": "",
            "payload_json": b"{}",
            "payload_sha256": b"",
            "append_offset_utf8_bytes": 0,
        }
    ]
    assert seen["request"].meta.owner.subject_id == "subject-a"


def test_ack_semantic_sends_authenticated_cursor_and_maps_error(monkeypatch):
    seen = {}

    class Stub:
        async def AckSemantic(self, request, metadata):
            seen["request"] = request
            return AckSemanticResponse(accepted_seq=1)

    client = RustSupervisorClient("/private/supervisor.sock", "session-binding")
    client._stub = Stub()
    monkeypatch.setattr(client, "connect", lambda: asyncio.sleep(0))
    runtime = RuntimeBinding(runtime_id="rt-1", epoch=b"e" * 16, generation=4, fingerprint=b"f" * 32, adapter_id="mimo")
    session = SessionBinding(runtime=runtime, session_id="session-1")
    result = asyncio.run(
        client.ack_semantic(
            subject_id="subject-a",
            username="alice",
            auth_generation=7,
            session=session,
            semantic_epoch=b"s" * 16,
            highest_contiguous_seq=1,
            request_id="request-ack",
        )
    )
    assert result == {"accepted_seq": 1, "error": None}
    assert seen["request"].meta.request_id == "request-ack"
    assert seen["request"].highest_contiguous_seq == 1


def test_run_turn_maps_typed_driver_not_ready(monkeypatch):
    class Stream:
        def __aiter__(self):
            return self

        async def __anext__(self):
            if getattr(self, "done", False):
                raise StopAsyncIteration
            self.done = True
            return TurnStreamItem(
                error=SupervisorError(
                    code="not_ready",
                    safe_message="driver turn execution is not ready",
                    retryable=True,
                    phase="driver",
                    request_id="request-turn",
                )
            )

    class Stub:
        def RunTurn(self, request, metadata):
            assert request.turn.run_id == "run-1"
            assert request.prompt_sha256
            return Stream()

    client = RustSupervisorClient("/private/supervisor.sock", "session-binding")
    client._stub = Stub()
    monkeypatch.setattr(client, "connect", lambda: asyncio.sleep(0))
    turn = TurnBinding(run_id="run-1", turn_id="turn-1")

    async def collect():
        return [
            item
            async for item in client.run_turn(
                subject_id="subject-a",
                username="alice",
                auth_generation=7,
                turn=turn,
                prompt_json=b'{"prompt":"hi"}',
                request_id="request-turn",
            )
        ]

    assert asyncio.run(collect()) == [
        {
            "type": "error",
            "error": {
                "code": "not_ready",
                "safe_message": "driver turn execution is not ready",
                "retryable": True,
                "phase": "driver",
                "request_id": "request-turn",
            },
        }
    ]
