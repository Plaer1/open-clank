"""Health-only client for the opt-in Rust supervisor foundation."""

from __future__ import annotations

from pathlib import Path
from typing import Any
import hashlib
import uuid

class RustSupervisorClient:
    """Small private-UDS gRPC client for the opt-in supervisor foundation.

    Runtime opening is intentionally limited to generation-fenced actor
    admission. Driver activation reports the daemon's typed not-ready result
    until a real admitted driver is attached; this client never falls back to
    an unauthenticated process path.
    """

    def __init__(self, socket_path: str | Path, session_binding: str) -> None:
        self.socket_path = str(socket_path)
        self.session_binding = session_binding
        self._channel: Any = None
        self._stub: Any = None

    async def connect(self) -> None:
        import grpc
        from src.openclank.generated.openclank_agent_supervisor_v1_pb2 import HealthRequest
        from src.openclank.generated.openclank_agent_supervisor_v1_pb2_grpc import AgentSupervisorStub

        if self._channel is not None:
            return
        self._channel = grpc.aio.insecure_channel(
            f"unix://{self.socket_path}",
            options=(("grpc.default_authority", "openclank.local"),),
        )
        self._stub = AgentSupervisorStub(self._channel)

    @staticmethod
    def _schema_sha256() -> bytes:
        proto = (
            Path(__file__).resolve().parents[2]
            / "packages/openclank-agent-supervisor/proto/openclank_agent_supervisor_v1.proto"
        )
        return hashlib.sha256(proto.read_bytes()).hexdigest().encode("ascii")

    @classmethod
    def _protocol_version(cls, features: tuple[str, ...] = ()) -> Any:
        from src.openclank.generated.openclank_agent_supervisor_v1_pb2 import ProtocolVersion

        return ProtocolVersion(
            major=1,
            minor=0,
            schema_sha256=cls._schema_sha256(),
            features=list(features),
        )

    async def health(self) -> dict[str, Any]:
        await self.connect()
        from src.openclank.generated.openclank_agent_supervisor_v1_pb2 import HealthRequest

        assert self._stub is not None
        response = await self._stub.Health(
            HealthRequest(),
            metadata=(("x-open-clank-session", self.session_binding),),
        )
        return {
            "protocol_major": response.protocol_major,
            "build_id": response.build_id,
            "schema_sha256": response.schema_sha256,
            "transport": response.transport,
            "protocol": response.protocol,
            "containment": response.containment,
            "runtime_actor": response.runtime_actor,
            "driver": response.driver,
            "tool_policy": response.tool_policy,
            "ready": response.ready,
        }

    async def negotiate(
        self,
        *,
        client_build: str = "openclank-python",
        required_features: tuple[str, ...] = (),
    ) -> dict[str, Any]:
        """Perform the private protocol/feature handshake before runtime RPCs.

        The checked-in proto bytes, rather than a generated Python module
        path, define the schema identity. A server may return a typed error in
        the response while still completing the authenticated gRPC call; the
        caller must not treat that as readiness.
        """
        await self.connect()
        from src.openclank.generated.openclank_agent_supervisor_v1_pb2 import NegotiateRequest

        request = NegotiateRequest(
            protocol=self._protocol_version(("transport", "protocol", "health")),
            request_id=uuid.uuid4().hex,
            client_build=client_build,
            required_features=list(required_features),
        )
        assert self._stub is not None
        response = await self._stub.Negotiate(
            request,
            metadata=(("x-open-clank-session", self.session_binding),),
        )
        return {
            "protocol_major": response.protocol.major if response.protocol else 0,
            "protocol_minor": response.protocol.minor if response.protocol else 0,
            "schema_sha256": bytes(response.protocol.schema_sha256).decode("ascii", "replace")
            if response.protocol
            else "",
            "server_build": response.server_build,
            "capabilities": [
                {
                    "name": capability.name,
                    "supported": capability.supported,
                    "detail_code": capability.detail_code,
                }
                for capability in response.capabilities
            ],
            "error": {
                "code": response.error.code,
                "safe_message": response.error.safe_message,
                "retryable": response.error.retryable,
                "phase": response.error.phase,
                "request_id": response.error.request_id,
            }
            if response.HasField("error")
            else None,
        }

    @staticmethod
    def _error_dict(error: Any) -> dict[str, Any] | None:
        if error is None:
            return None
        return {
            "code": error.code,
            "safe_message": error.safe_message,
            "retryable": error.retryable,
            "phase": error.phase,
            "request_id": error.request_id,
        }

    async def open_owner_runtime(
        self,
        *,
        subject_id: str,
        username: str,
        auth_generation: int,
        adapter_id: str,
        projection_fingerprint: bytes,
        expected_generation: int,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        """Open/reuse one exact owner runtime actor through authenticated IPC."""
        if not subject_id or len(subject_id) > 128:
            raise ValueError("subject_id must be non-empty and bounded")
        if not adapter_id or len(adapter_id) > 128:
            raise ValueError("adapter_id must be non-empty and bounded")
        if len(projection_fingerprint) != 32:
            raise ValueError("projection_fingerprint must be 32 bytes")
        await self.connect()
        from src.openclank.generated.openclank_agent_supervisor_v1_pb2 import (
            OpenOwnerRuntimeRequest,
            OwnerRef,
            RequestMeta,
        )

        assert self._stub is not None
        request_token = request_id or uuid.uuid4().hex
        request = OpenOwnerRuntimeRequest(
            meta=RequestMeta(
                protocol=self._protocol_version(("runtime_actor",)),
                request_id=request_token,
                idempotency_key=request_token,
                owner=OwnerRef(
                    subject_id=subject_id,
                    username=username,
                    auth_generation=auth_generation,
                ),
            ),
            adapter_id=adapter_id,
            projection_fingerprint=projection_fingerprint,
            expected_generation=expected_generation,
        )
        response = await self._stub.OpenOwnerRuntime(
            request,
            metadata=(("x-open-clank-session", self.session_binding),),
        )
        runtime = response.runtime
        return {
            "runtime": {
                "owner": {
                    "subject_id": runtime.owner.subject_id,
                    "username": runtime.owner.username,
                    "auth_generation": runtime.owner.auth_generation,
                },
                "runtime_id": runtime.runtime_id,
                "epoch": bytes(runtime.epoch),
                "generation": runtime.generation,
                "fingerprint": bytes(runtime.fingerprint),
                "adapter_id": runtime.adapter_id,
            }
            if runtime
            else None,
            "capabilities": [
                {
                    "name": capability.name,
                    "supported": capability.supported,
                    "detail_code": capability.detail_code,
                }
                for capability in response.capabilities
            ],
            "driver_pid": response.driver_pid,
            "driver_start_token": response.driver_start_token,
            "callback_registration_id": response.callback_registration_id,
            "driver_ready": response.driver_ready,
            "error": self._error_dict(response.error if response.HasField("error") else None),
        }

    async def activate_driver_callback(
        self,
        *,
        subject_id: str,
        username: str,
        auth_generation: int,
        runtime: Any,
        callback_registration_id: str,
        driver_pid: int,
        driver_start_token: str,
        callback_endpoint: str,
        callback_nonce: bytes,
        callback_binding_sha256: bytes,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        """Deliver a host-created callback binding without exposing secrets."""
        if len(callback_nonce) != 32 or len(callback_binding_sha256) != 32:
            raise ValueError("callback binding digests must be 32 bytes")
        await self.connect()
        from src.openclank.generated.openclank_agent_supervisor_v1_pb2 import (
            ActivateDriverCallbackRequest,
            OwnerRef,
            RequestMeta,
        )

        assert self._stub is not None
        request_token = request_id or uuid.uuid4().hex
        request = ActivateDriverCallbackRequest(
            meta=RequestMeta(
                protocol=self._protocol_version(("runtime_actor", "driver")),
                request_id=request_token,
                idempotency_key=request_token,
                owner=OwnerRef(
                    subject_id=subject_id,
                    username=username,
                    auth_generation=auth_generation,
                ),
            ),
            runtime=runtime,
            callback_registration_id=callback_registration_id,
            driver_pid=driver_pid,
            driver_start_token=driver_start_token,
            callback_endpoint=callback_endpoint,
            callback_nonce=callback_nonce,
            callback_binding_sha256=callback_binding_sha256,
        )
        response = await self._stub.ActivateDriverCallback(
            request,
            metadata=(("x-open-clank-session", self.session_binding),),
        )
        return {
            "driver_ready": response.driver_ready,
            "capabilities": [
                {
                    "name": capability.name,
                    "supported": capability.supported,
                    "detail_code": capability.detail_code,
                }
                for capability in response.capabilities
            ],
            "error": self._error_dict(response.error if response.HasField("error") else None),
        }

    async def open_session(
        self,
        *,
        subject_id: str,
        username: str,
        auth_generation: int,
        runtime: Any,
        session_id: str,
        workspace_id: str = "",
        snapshot_json: bytes = b"",
        transcript_revision: int = 0,
        config_json: bytes = b"",
        request_id: str | None = None,
    ) -> dict[str, Any]:
        """Open/reuse a session bound to one exact runtime and workspace."""
        if not session_id or len(session_id) > 128 or len(workspace_id) > 128:
            raise ValueError("session/workspace binding is malformed")
        if len(snapshot_json) > 1024 * 1024 or len(config_json) > 1024 * 1024:
            raise ValueError("session snapshot/config exceeds the 1 MiB bound")
        await self.connect()
        from src.openclank.generated.openclank_agent_supervisor_v1_pb2 import (
            OpenSessionRequest,
            OwnerRef,
            RequestMeta,
        )

        assert self._stub is not None
        request_token = request_id or uuid.uuid4().hex
        request = OpenSessionRequest(
            meta=RequestMeta(
                protocol=self._protocol_version(("runtime_actor", "session")),
                request_id=request_token,
                idempotency_key=request_token,
                owner=OwnerRef(
                    subject_id=subject_id,
                    username=username,
                    auth_generation=auth_generation,
                ),
            ),
            runtime=runtime,
            session_id=session_id,
            workspace_id=workspace_id,
            snapshot_json=snapshot_json,
            snapshot_sha256=hashlib.sha256(snapshot_json).digest() if snapshot_json else b"",
            transcript_revision=transcript_revision,
            config_json=config_json,
        )
        response = await self._stub.OpenSession(
            request,
            metadata=(("x-open-clank-session", self.session_binding),),
        )
        session = response.session
        session_runtime = session.runtime if session else None
        return {
            "session": {
                "runtime": {
                    "runtime_id": session_runtime.runtime_id,
                    "epoch": bytes(session_runtime.epoch),
                    "generation": session_runtime.generation,
                    "fingerprint": bytes(session_runtime.fingerprint),
                    "adapter_id": session_runtime.adapter_id,
                }
                if session_runtime
                else None,
                "session_id": session.session_id,
                "workspace_id": session.workspace_id,
            }
            if session
            else None,
            "semantic_epoch": bytes(response.semantic_epoch),
            "retained_head": response.retained_head,
            "retained_tail": response.retained_tail,
            "terminals": [
                {
                    "terminal_id": terminal.terminal_id,
                    "epoch": bytes(terminal.epoch),
                    "rows": terminal.rows,
                    "cols": terminal.cols,
                }
                for terminal in response.terminals
            ],
            "error": self._error_dict(response.error if response.HasField("error") else None),
        }

    async def close_session(
        self,
        *,
        subject_id: str,
        username: str,
        auth_generation: int,
        session: Any,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        """Close exactly one owner/runtime session through the supervisor actor."""
        session_id = getattr(session, "session_id", "")
        if not session_id or len(session_id) > 128:
            raise ValueError("session binding is malformed")
        await self.connect()
        from src.openclank.generated.openclank_agent_supervisor_v1_pb2 import (
            CloseSessionRequest,
            OwnerRef,
            RequestMeta,
        )

        assert self._stub is not None
        request_token = request_id or uuid.uuid4().hex
        request = CloseSessionRequest(
            meta=RequestMeta(
                protocol=self._protocol_version(("runtime_actor", "session")),
                request_id=request_token,
                idempotency_key=request_token,
                owner=OwnerRef(
                    subject_id=subject_id,
                    username=username,
                    auth_generation=auth_generation,
                ),
            ),
            session=session,
        )
        response = await self._stub.CloseSession(
            request,
            metadata=(("x-open-clank-session", self.session_binding),),
        )
        return {
            "closed": response.closed,
            "error": self._error_dict(response.error if response.HasField("error") else None),
        }

    async def attach_semantic(
        self,
        *,
        subject_id: str,
        username: str,
        auth_generation: int,
        session: Any,
        semantic_epoch: bytes,
        since_seq: int = 0,
        grade: str = "block",
        request_id: str | None = None,
    ):
        """Replay a bounded semantic suffix, yielding events or one typed error."""
        if len(semantic_epoch) != 16:
            raise ValueError("semantic_epoch must be 16 bytes")
        if grade not in {"off", "turn", "block", "delta"}:
            raise ValueError("invalid semantic subscription grade")
        await self.connect()
        from src.openclank.generated.openclank_agent_supervisor_v1_pb2 import (
            AttachSemanticRequest,
            OwnerRef,
            RequestMeta,
        )

        assert self._stub is not None
        request_token = request_id or uuid.uuid4().hex
        response_stream = self._stub.AttachSemantic(
            AttachSemanticRequest(
                meta=RequestMeta(
                    protocol=self._protocol_version(("runtime_actor", "session", "semantic")),
                    request_id=request_token,
                    idempotency_key=request_token,
                    owner=OwnerRef(
                        subject_id=subject_id,
                        username=username,
                        auth_generation=auth_generation,
                    ),
                ),
                session=session,
                semantic_epoch=semantic_epoch,
                since_seq=since_seq,
                grade=grade,
            ),
            metadata=(("x-open-clank-session", self.session_binding),),
        )
        async for item in response_stream:
            if item.HasField("event"):
                event = item.event
                yield {
                    "type": "event",
                    "owner_subject_id": event.owner_subject_id,
                    "session_id": event.session_id,
                    "runtime_id": event.runtime_id,
                    "runtime_epoch": event.runtime_epoch,
                    "runtime_generation": event.runtime_generation,
                    "run_id": event.run_id,
                    "turn_id": event.turn_id,
                    "semantic_epoch": bytes(event.semantic_epoch),
                    "seq": event.seq,
                    "emitted_unix_ms": event.emitted_unix_ms,
                    "op": event.op,
                    "entity_kind": event.entity_kind,
                    "entity_id": event.entity_id,
                    "payload_json": bytes(event.payload_json),
                    "payload_sha256": bytes(event.payload_sha256),
                    "append_offset_utf8_bytes": event.append_offset_utf8_bytes,
                }
            elif item.HasField("error"):
                yield {"type": "error", "error": self._error_dict(item.error)}

    async def ack_semantic(
        self,
        *,
        subject_id: str,
        username: str,
        auth_generation: int,
        session: Any,
        semantic_epoch: bytes,
        highest_contiguous_seq: int,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        """Advance one authenticated semantic cursor, never beyond the journal tail."""
        if len(semantic_epoch) != 16 or highest_contiguous_seq < 0:
            raise ValueError("semantic ACK binding is malformed")
        await self.connect()
        from src.openclank.generated.openclank_agent_supervisor_v1_pb2 import (
            AckSemanticRequest,
            OwnerRef,
            RequestMeta,
        )

        assert self._stub is not None
        request_token = request_id or uuid.uuid4().hex
        response = await self._stub.AckSemantic(
            AckSemanticRequest(
                meta=RequestMeta(
                    protocol=self._protocol_version(("runtime_actor", "session", "semantic")),
                    request_id=request_token,
                    idempotency_key=request_token,
                    owner=OwnerRef(
                        subject_id=subject_id,
                        username=username,
                        auth_generation=auth_generation,
                    ),
                ),
                session=session,
                semantic_epoch=semantic_epoch,
                highest_contiguous_seq=highest_contiguous_seq,
            ),
            metadata=(("x-open-clank-session", self.session_binding),),
        )
        return {
            "accepted_seq": response.accepted_seq,
            "error": self._error_dict(response.error if response.HasField("error") else None),
        }

    async def run_turn(
        self,
        *,
        subject_id: str,
        username: str,
        auth_generation: int,
        turn: Any,
        prompt_json: bytes = b"",
        mode: int = 3,
        transcript_sha256: bytes = b"",
        transcript_revision: int = 0,
        plan_sha256: bytes = b"",
        plan_revision: int = 0,
        driver_config_json: bytes = b"",
        request_id: str | None = None,
    ):
        """Submit one immutable turn and yield ordered semantic/error items."""
        if len(prompt_json) > 1024 * 1024 or len(driver_config_json) > 1024 * 1024:
            raise ValueError("turn payload exceeds the 1 MiB bound")
        await self.connect()
        from src.openclank.generated.openclank_agent_supervisor_v1_pb2 import (
            OwnerRef,
            RequestMeta,
            TurnRequest,
        )

        assert self._stub is not None
        request_token = request_id or uuid.uuid4().hex
        stream = self._stub.RunTurn(
            TurnRequest(
                meta=RequestMeta(
                    protocol=self._protocol_version(("runtime_actor", "session", "turn")),
                    request_id=request_token,
                    idempotency_key=request_token,
                    owner=OwnerRef(
                        subject_id=subject_id,
                        username=username,
                        auth_generation=auth_generation,
                    ),
                ),
                turn=turn,
                mode=mode,
                prompt_json=prompt_json,
                prompt_sha256=hashlib.sha256(prompt_json).digest() if prompt_json else b"",
                transcript_sha256=transcript_sha256,
                transcript_revision=transcript_revision,
                plan_sha256=plan_sha256,
                plan_revision=plan_revision,
                driver_config_json=driver_config_json,
            ),
            metadata=(("x-open-clank-session", self.session_binding),),
        )
        async for item in stream:
            if item.HasField("error"):
                yield {"type": "error", "error": self._error_dict(item.error)}
            elif item.HasField("event"):
                event = item.event
                yield {
                    "type": "event",
                    "session_id": event.session_id,
                    "runtime_id": event.runtime_id,
                    "semantic_epoch": bytes(event.semantic_epoch),
                    "seq": event.seq,
                    "op": event.op,
                    "entity_kind": event.entity_kind,
                    "entity_id": event.entity_id,
                    "payload_json": bytes(event.payload_json),
                }

    async def close(self) -> None:
        if self._channel is not None:
            await self._channel.close()
        self._channel = None
        self._stub = None
