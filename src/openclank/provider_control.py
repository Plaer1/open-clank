"""Owner-bound host-to-engine provider control and OAuth flow fencing."""

from __future__ import annotations

import base64
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import hashlib
import secrets
import threading
from typing import Any, Mapping, Optional

from src.openclank.managed_protocol import (
    ManagedMethodValidationError,
    validate_engine_method_request,
    validate_engine_method_result,
)


OAUTH_FLOW_TTL_SECONDS = 10 * 60


class ProviderEngineControlError(RuntimeError):
    """Secret-free engine-control failure safe for an HTTP error boundary."""


class ProviderOAuthFlowError(ProviderEngineControlError):
    pass


class ProviderEngineValidationError(ProviderEngineControlError):
    pass


def _required(value: Any, label: str) -> str:
    text = str(value or "").strip()
    if not text or "\x00" in text:
        raise ProviderOAuthFlowError(f"{label} is required")
    return text


class BoundProviderEngine:
    """One generation-pinned owner worker used for an OAuth flow."""

    def __init__(self, lease: Any):
        self._lease = lease
        self._released = False

    async def call(self, method: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        if self._released:
            raise ProviderEngineControlError("managed provider worker lease has ended")
        try:
            request = validate_engine_method_request(method, dict(payload))
            result = await self._lease.worker.managed_engine_call(method, request)
            return validate_engine_method_result(method, result)
        except ManagedMethodValidationError as exc:
            raise ProviderEngineControlError(
                "managed provider engine returned an incompatible response"
            ) from exc
        except Exception as exc:
            from src.openclank.acp_client import RPCError

            if isinstance(exc, RPCError):
                raise ProviderEngineValidationError(str(exc)) from exc
            raise

    async def release(self, *, successful: bool = False) -> None:
        if self._released:
            return
        self._released = True
        await self._lease.release(successful_terminal=successful)


class ManagedProviderEngineControl:
    """Resolve the supervisor lazily and never accept a child-supplied owner."""

    async def bind(self, *, request: Any, owner: str) -> BoundProviderEngine:
        supervisor = getattr(getattr(request.app, "state", None), "mimo_supervisor", None)
        if supervisor is None:
            raise ProviderEngineControlError("managed provider engine is unavailable")
        try:
            lease = await supervisor.admit_provider_control(_required(owner, "provider owner"))
        except Exception as exc:
            raise ProviderEngineControlError("managed provider engine is unavailable") from exc
        return BoundProviderEngine(lease)

    async def call(
        self,
        *,
        request: Any,
        owner: str,
        method: str,
        payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        lease = await self.bind(request=request, owner=owner)
        try:
            result = await lease.call(method, payload)
        except Exception:
            await lease.release(successful=False)
            raise
        await lease.release(successful=True)
        return result


def oauth_verifier_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("utf-8")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


@dataclass
class OAuthHostFlow:
    flow_id: str
    owner: str
    connection_id: str
    provider_id: str
    billing_lane: str
    mode: str
    label: str
    method: int
    expires_at: datetime
    engine: BoundProviderEngine = field(repr=False)
    state: str = field(repr=False)
    nonce: str = field(repr=False)
    code_verifier: str = field(repr=False)
    target_account_id: Optional[str] = None
    expected_revision: Optional[int] = None
    status: str = "pending"
    public_start: Optional[dict[str, Any]] = field(default=None, repr=False)
    account_public: Optional[dict[str, Any]] = None
    error_code: Optional[str] = None

    @classmethod
    def create(
        cls,
        *,
        owner: str,
        connection_id: str,
        provider_id: str,
        billing_lane: str,
        mode: str,
        label: str,
        method: int,
        engine: BoundProviderEngine,
        target_account_id: Optional[str] = None,
        expected_revision: Optional[int] = None,
        now: Optional[datetime] = None,
    ) -> "OAuthHostFlow":
        timestamp = now or datetime.now(timezone.utc)
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=timezone.utc)
        return cls(
            flow_id=f"pof_{secrets.token_urlsafe(32)}",
            owner=_required(owner, "provider owner").lower(),
            connection_id=_required(connection_id, "provider connection"),
            provider_id=_required(provider_id, "provider family"),
            billing_lane=_required(billing_lane, "provider billing lane"),
            mode=mode,
            label=_required(label, "provider account label"),
            method=int(method),
            expires_at=timestamp.astimezone(timezone.utc)
            + timedelta(seconds=OAUTH_FLOW_TTL_SECONDS),
            engine=engine,
            state=secrets.token_urlsafe(32),
            nonce=secrets.token_urlsafe(32),
            code_verifier=secrets.token_urlsafe(64),
            target_account_id=target_account_id,
            expected_revision=expected_revision,
        )

    def _identity(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "flowID": self.flow_id,
            "connectionID": self.connection_id,
            "providerID": self.provider_id,
            "billingLane": self.billing_lane,
            "mode": self.mode,
            "nonce": self.nonce,
        }
        if self.target_account_id is not None:
            result["targetAccountID"] = self.target_account_id
        if self.expected_revision is not None:
            result["expectedRevision"] = int(self.expected_revision)
        return result

    def start_payload(
        self,
        *,
        redirect_uri: str,
        inputs: Mapping[str, str],
    ) -> dict[str, Any]:
        return {
            **self._identity(),
            "method": int(self.method),
            "redirectURI": _required(redirect_uri, "OAuth redirect URI"),
            "state": self.state,
            "codeVerifierChallenge": oauth_verifier_challenge(self.code_verifier),
            "expiresAt": int(self.expires_at.timestamp() * 1000),
            "inputs": {str(key): str(value) for key, value in inputs.items()},
        }

    def completion_payload(self, *, code: Optional[str] = None) -> dict[str, Any]:
        result = {
            **self._identity(),
            "codeVerifier": self.code_verifier,
        }
        if code is not None:
            result["code"] = code
        return result

    def public(self, *, start: Optional[Mapping[str, Any]] = None) -> dict[str, Any]:
        result: dict[str, Any] = {
            "flow_id": self.flow_id,
            "status": self.status,
            "mode": self.mode,
            "expires_at": self.expires_at.isoformat().replace("+00:00", "Z"),
        }
        if self.target_account_id:
            result["target_account_id"] = self.target_account_id
        visible_start = start if start is not None else self.public_start
        if visible_start is not None and self.status in {"pending", "running"}:
            result.update(
                {
                    "url": visible_start.get("url"),
                    "method": visible_start.get("method"),
                    "instructions": visible_start.get("instructions", ""),
                }
            )
            if visible_start.get("userCode") is not None:
                result["user_code"] = visible_start["userCode"]
        if self.account_public is not None:
            result["account"] = dict(self.account_public)
        if self.error_code:
            result["error_code"] = self.error_code
        return result

    def state_matches(self, value: str) -> bool:
        return secrets.compare_digest(self.state, str(value or ""))

    def expired(self, *, now: Optional[datetime] = None) -> bool:
        timestamp = now or datetime.now(timezone.utc)
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=timezone.utc)
        return timestamp.astimezone(timezone.utc) >= self.expires_at


class OAuthHostFlowStore:
    """Owner-indexed transient flow metadata; credentials never enter it."""

    def __init__(self) -> None:
        self._flows: dict[str, OAuthHostFlow] = {}
        self._lock = threading.RLock()

    def add(self, flow: OAuthHostFlow) -> None:
        with self._lock:
            if flow.flow_id in self._flows:
                raise ProviderOAuthFlowError("OAuth flow already exists")
            self._flows[flow.flow_id] = flow

    def get(self, flow_id: str, *, owner: Optional[str] = None) -> OAuthHostFlow:
        with self._lock:
            flow = self._flows.get(_required(flow_id, "OAuth flow"))
            if flow is None or (owner is not None and flow.owner != owner.strip().lower()):
                raise ProviderOAuthFlowError("OAuth flow was not found")
            return flow

    def remove(self, flow_id: str) -> Optional[OAuthHostFlow]:
        with self._lock:
            return self._flows.pop(str(flow_id or ""), None)

    def remove_owner(self, owner: str) -> list[OAuthHostFlow]:
        """Atomically detach every transient OAuth flow for one owner."""
        normalized = _required(owner, "provider owner").lower()
        with self._lock:
            rows = [flow for flow in self._flows.values() if flow.owner == normalized]
            for flow in rows:
                self._flows.pop(flow.flow_id, None)
            return rows

    def expired(self, *, now: Optional[datetime] = None) -> list[OAuthHostFlow]:
        timestamp = now or datetime.now(timezone.utc)
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=timezone.utc)
        timestamp = timestamp.astimezone(timezone.utc)
        retention = timedelta(seconds=OAUTH_FLOW_TTL_SECONDS)
        terminal = {"complete", "failed", "cancelled", "expired"}
        with self._lock:
            rows: list[OAuthHostFlow] = []
            for flow in list(self._flows.values()):
                if flow.status in terminal:
                    if timestamp >= flow.expires_at + retention:
                        self._flows.pop(flow.flow_id, None)
                    continue
                if flow.expired(now=timestamp):
                    rows.append(flow)
            return rows


__all__ = [
    "BoundProviderEngine",
    "ManagedProviderEngineControl",
    "OAuthHostFlow",
    "OAuthHostFlowStore",
    "ProviderEngineControlError",
    "ProviderEngineValidationError",
    "ProviderOAuthFlowError",
    "oauth_verifier_challenge",
]
