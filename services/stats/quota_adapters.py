"""Pure passive quota adapters and the trusted terminal admission seam.

Adapters consume already normalized provider fields supplied by a trusted host
producer. They never parse browser headers or retain raw provider material.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import math
from typing import Any, Mapping

from services.stats.quota import QuotaError, admit_quota_observation


@dataclass(frozen=True)
class NormalizedQuotaObservation:
    """Content-free provider capacity observation, without tenant identity."""

    window_id: str
    window_kind: str
    observed_at: datetime
    source_kind: str
    adapter_revision: str
    utilization_numerator: int | None = None
    utilization_denominator: int | None = None
    limit_value: int | None = None
    remaining_value: int | None = None
    reset_at: datetime | None = None
    error_class: str | None = None
    label: str | None = None


def _parse_observed(value: Any, name: str) -> datetime:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise QuotaError(f"quota {name} must include timezone")
        return value.astimezone(timezone.utc)
    if not isinstance(value, str) or len(value) > 64:
        raise QuotaError(f"invalid quota {name}")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise QuotaError(f"invalid quota {name}") from exc
    if parsed.tzinfo is None:
        raise QuotaError(f"quota {name} must include timezone")
    return parsed.astimezone(timezone.utc)


def normalize_terminal_quota(payload: Mapping[str, Any]) -> tuple[NormalizedQuotaObservation, ...]:
    """Decode the private terminal DTO without accepting tenant identity.

    Native producers may attach this bounded envelope to a validated terminal
    result. Account, provider, connection, and operation identity are resolved
    from the committed host binding by ``admit_terminal_quota_payload``.
    """
    if not isinstance(payload, Mapping) or set(payload) - {"adapter_revision", "observed_at", "observations"}:
        raise QuotaError("quota terminal envelope is malformed")
    revision = payload.get("adapter_revision")
    if not isinstance(revision, str) or not revision or len(revision) > 128:
        raise QuotaError("invalid quota adapter revision")
    observed_at = _parse_observed(payload.get("observed_at"), "observed_at")
    observations = payload.get("observations")
    if not isinstance(observations, list) or not observations or len(observations) > 16:
        raise QuotaError("invalid quota observations")
    result = []
    allowed = {"window_id", "window_kind", "source_kind", "limit_value", "remaining_value",
               "utilization_numerator", "utilization_denominator", "reset_at", "label", "error_class"}
    for item in observations:
        if not isinstance(item, Mapping) or set(item) - allowed or "window_id" not in item or "window_kind" not in item:
            raise QuotaError("quota observation is malformed")
        reset_at = item.get("reset_at")
        result.append(NormalizedQuotaObservation(
            window_id=item["window_id"], window_kind=item["window_kind"], observed_at=observed_at,
            source_kind=item.get("source_kind", "official"), adapter_revision=revision,
            utilization_numerator=item.get("utilization_numerator"),
            utilization_denominator=item.get("utilization_denominator"),
            limit_value=item.get("limit_value"), remaining_value=item.get("remaining_value"),
            reset_at=_parse_observed(reset_at, "reset_at") if reset_at is not None else None,
            error_class=item.get("error_class"), label=item.get("label")))
    return tuple(result)


def _metric(payload: Mapping[str, Any], name: str, *, observed_at: datetime,
            adapter_revision: str, source_kind: str = "official") -> NormalizedQuotaObservation:
    value = payload.get(name)
    if not isinstance(value, Mapping):
        raise QuotaError(f"{name} quota metric is unavailable")
    allowed = {"limit", "remaining", "reset_at"}
    if any(key not in allowed for key in value):
        raise QuotaError(f"{name} quota metric is malformed")
    def bounded_int(item, field):
        if item is None:
            return None
        if isinstance(item, bool) or not isinstance(item, int) or item < 0 or item > 2**63 - 1:
            raise QuotaError(f"{name} quota {field} is invalid")
        return item
    limit = bounded_int(value.get("limit"), "limit")
    remaining = bounded_int(value.get("remaining"), "remaining")
    if remaining is not None and limit is not None and remaining > limit:
        raise QuotaError(f"{name} quota remaining is invalid")
    reset_at = value.get("reset_at")
    if reset_at is not None:
        reset_at = _parse_observed(reset_at, "reset_at")
    return NormalizedQuotaObservation(
        window_id=name, window_kind="continuous", observed_at=observed_at,
        source_kind=source_kind, adapter_revision=adapter_revision,
        limit_value=limit, remaining_value=remaining,
        reset_at=reset_at, label=name,
    )


def adapt_openai_capacity(payload: Mapping[str, Any], *, observed_at: datetime,
                          adapter_revision: str = "openai-capacity-v1") -> tuple[NormalizedQuotaObservation, ...]:
    """Adapt documented request/token capacity fields; no active probe occurs."""
    if not isinstance(payload, Mapping) or payload.get("transport") == "unsupported":
        raise QuotaError("openai subscription quota is unavailable")
    if set(payload) - {"transport", "requests", "tokens", "inputTokens", "outputTokens"}:
        raise QuotaError("quota payload contains unsupported sensitive fields")
    return tuple(_metric(payload, name, observed_at=observed_at,
                         adapter_revision=adapter_revision)
                 for name in ("requests", "tokens", "inputTokens", "outputTokens") if name in payload)


def adapt_anthropic_capacity(payload: Mapping[str, Any], *, observed_at: datetime,
                             adapter_revision: str = "anthropic-capacity-v1") -> tuple[NormalizedQuotaObservation, ...]:
    """Adapt documented request/token capacity fields without retaining headers."""
    if not isinstance(payload, Mapping) or payload.get("transport") == "unsupported":
        raise QuotaError("anthropic subscription quota is unavailable")
    if set(payload) - {"transport", "requests", "tokens", "inputTokens", "outputTokens"}:
        raise QuotaError("quota payload contains unsupported sensitive fields")
    return tuple(_metric(payload, name, observed_at=observed_at,
                         adapter_revision=adapter_revision)
                 for name in ("requests", "tokens", "inputTokens", "outputTokens") if name in payload)


def adapt_capacity_error(status: int | str, *, observed_at: datetime,
                         adapter_revision: str = "capacity-error-v1") -> NormalizedQuotaObservation:
    """Represent safe terminal failure classes without retaining response data."""
    labels = {401: "unauthorized", 403: "forbidden", 429: "rate_limited",
              "timeout": "timeout", "malformed": "malformed",
              "entitlement": "entitlement", "unavailable": "unavailable"}
    error_class = labels.get(status)
    if error_class is None:
        raise QuotaError("unsupported quota error class")
    return NormalizedQuotaObservation(
        window_id="provider", window_kind="unavailable", observed_at=observed_at,
        source_kind="error", adapter_revision=adapter_revision,
        error_class=error_class, label="provider",
    )


def native_capacity_envelope(provider_id: str, payload: Mapping[str, Any], *,
                             observed_at: datetime) -> dict[str, Any] | None:
    """Extract only documented capacity fields from a terminal response.

    This is intentionally limited to API throughput metadata. It never reads
    headers, credentials, subscription pages, or arbitrary provider payloads.
    """
    if provider_id not in {"openai", "anthropic"} or not isinstance(payload, Mapping):
        return None
    revision = payload.get("adapterRevision")
    if revision is not None and (not isinstance(revision, str) or not revision or len(revision) > 80 or any(ord(char) < 32 for char in revision)):
        raise QuotaError("terminal capacity has invalid adapter revision")
    if set(payload) - {"transport", "adapterRevision", "requests", "tokens"}:
        raise QuotaError("terminal capacity contains unsupported fields")
    adapter = adapt_openai_capacity if provider_id == "openai" else adapt_anthropic_capacity
    adapter_payload = {key: payload[key] for key in ("transport", "requests", "tokens", "inputTokens", "outputTokens") if key in payload}
    for metric in ("requests", "tokens", "inputTokens", "outputTokens"):
        value = adapter_payload.get(metric)
        if isinstance(value, Mapping) and "resetAt" in value:
            adapter_payload[metric] = {("reset_at" if key == "resetAt" else key): item for key, item in value.items()}
    rows = adapter(adapter_payload, observed_at=observed_at,
                   adapter_revision=revision or f"{provider_id}-capacity-v1")
    if not rows:
        return None
    return {"adapter_revision": rows[0].adapter_revision,
            "observed_at": observed_at.isoformat(),
            "observations": [{"window_id": row.window_id, "window_kind": row.window_kind,
                               "source_kind": row.source_kind, "limit_value": row.limit_value,
                               "remaining_value": row.remaining_value,
                               "reset_at": row.reset_at.isoformat() if row.reset_at else None,
                               "label": row.label} for row in rows]}


def admit_terminal_quota(db, *, owner: str, operation_id: str,
                         observation: NormalizedQuotaObservation,
                         root_operation_id: str | None = None,
                         connection_id: str | None = None,
                         provider_id: str | None = None,
                         billing_lane: str | None = None,
                         account_id: str | None = None):
    """Admit a trusted terminal observation using the committed binding.

    The caller supplies only normalized metric data. Account, provider, and
    connection identity are loaded from the committed operation binding and
    cannot be selected by a browser or producer payload.
    """
    from core.provider_models import ProviderAccount, ProviderConnection, ProviderOperationBinding

    binding_query = db.query(ProviderOperationBinding).filter(
        ProviderOperationBinding.owner == owner,
        ProviderOperationBinding.credential_owner == owner,
        ProviderOperationBinding.root_operation_id == (root_operation_id or operation_id),
        ProviderOperationBinding.state == "committed",
        ProviderOperationBinding.committed_at.isnot(None),
    )
    if connection_id is not None:
        binding_query = binding_query.filter(ProviderOperationBinding.connection_id == connection_id)
    if provider_id is not None:
        binding_query = binding_query.filter(ProviderOperationBinding.provider_id == provider_id)
    if billing_lane is not None:
        binding_query = binding_query.filter(ProviderOperationBinding.billing_lane == billing_lane)
    if account_id is not None:
        binding_query = binding_query.filter(ProviderOperationBinding.selected_account_id == account_id)
    bindings = binding_query.order_by(ProviderOperationBinding.committed_at.desc()).limit(2).all()
    if len(bindings) > 1:
        raise QuotaError("quota operation binding is ambiguous")
    binding = bindings[0] if bindings else None
    if binding is None or binding.selected_account_id is None:
        raise QuotaError("quota operation binding is not committed")
    account = db.query(ProviderAccount).filter(
        ProviderAccount.id == binding.selected_account_id,
        ProviderAccount.owner == owner,
        ProviderAccount.connection_id == binding.connection_id,
        ProviderAccount.enabled.is_(True), ProviderAccount.deleted_at.is_(None),
    ).first()
    connection = db.query(ProviderConnection).filter(
        ProviderConnection.id == binding.connection_id,
        ProviderConnection.owner == owner,
        ProviderConnection.enabled.is_(True), ProviderConnection.deleted_at.is_(None),
    ).first()
    if account is None or connection is None or binding.provider_id != connection.family_id:
        raise QuotaError("quota operation binding is not canonical")
    return admit_quota_observation(
        db, owner=owner, account_id=account.id, provider_id=connection.family_id,
        operation_id=operation_id, window_id=observation.window_id,
        window_kind=observation.window_kind, observed_at=observation.observed_at,
        source_kind=observation.source_kind, adapter_revision=observation.adapter_revision,
        utilization_numerator=observation.utilization_numerator,
        utilization_denominator=observation.utilization_denominator,
        limit_value=observation.limit_value, remaining_value=observation.remaining_value,
        reset_at=observation.reset_at, label=observation.label,
        error_class=observation.error_class,
        binding_operation_id=root_operation_id or operation_id,
    )


def admit_terminal_quota_payload(db, *, owner: str, operation_id: str,
                                 payload: Mapping[str, Any], root_operation_id: str | None = None,
                                 connection_id: str | None = None, provider_id: str | None = None,
                                 billing_lane: str | None = None, account_id: str | None = None):
    """Admit a trusted native terminal envelope through the host binding."""
    rows = []
    for observation in normalize_terminal_quota(payload):
        row = admit_terminal_quota(db, owner=owner, operation_id=operation_id,
                                   observation=observation, root_operation_id=root_operation_id,
                                   connection_id=connection_id, provider_id=provider_id,
                                   billing_lane=billing_lane, account_id=account_id)
        if row is not None:
            rows.append(row)
    return tuple(rows)
