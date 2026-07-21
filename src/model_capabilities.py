"""Single source of truth for per-model tool capability evidence."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any

from core.database import ModelCapability, ModelEndpoint, SessionLocal
from src.endpoint_resolver import _endpoint_enabled_models, normalize_base


def capability_fingerprint(endpoint: ModelEndpoint, model_id: str) -> str:
    material = {
        "endpoint_id": endpoint.id,
        "base_url": normalize_base(endpoint.base_url or ""),
        "endpoint_kind": getattr(endpoint, "endpoint_kind", None) or "auto",
        "model_type": getattr(endpoint, "model_type", None) or "llm",
        "model_id": str(model_id),
        "provider_auth_id": getattr(endpoint, "provider_auth_id", None) or "",
        "tool_protocol": "openai-tools-v1",
    }
    return hashlib.sha256(
        json.dumps(material, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def capability_state(endpoint: ModelEndpoint, model_id: str, record: ModelCapability | None) -> dict[str, Any]:
    current = capability_fingerprint(endpoint, model_id)
    declared = record.tools_declared if record is not None else None
    verified = (
        record.tools_verified
        if record is not None and record.verification_fingerprint == current
        else None
    )
    tools_enabled = declared is not False
    if record is not None and (
        record.verification_fingerprint
        and record.verification_fingerprint != current
    ):
        probe_status = "stale"
    elif verified is True:
        probe_status = "supported"
    elif verified is False:
        probe_status = "unsupported"
    else:
        probe_status = "unknown"
    return {
        "endpoint_id": endpoint.id,
        "model_id": model_id,
        "tools_declared": declared,
        "tools_enabled": tools_enabled,
        "tools_verified": verified,
        "tools_verified_at": (
            record.tools_verified_at.isoformat() + "Z"
            if record is not None and record.tools_verified_at
            else None
        ),
        "probe_status": probe_status,
        # Compatibility for current projection and route consumers. Admission
        # now follows the manual toggle only; probe evidence is diagnostic.
        "status": "eligible" if tools_enabled else "blocked",
        "eligible": tools_enabled,
    }


def endpoint_capability_states(db, endpoint: ModelEndpoint) -> list[dict[str, Any]]:
    records = {
        row.model_id: row
        for row in db.query(ModelCapability).filter(ModelCapability.endpoint_id == endpoint.id).all()
        if hasattr(row, "model_id")
    }
    return [
        capability_state(endpoint, model_id, records.get(model_id))
        for model_id in _endpoint_enabled_models(endpoint)
    ]


def eligible_model_ids(db, endpoint: ModelEndpoint) -> list[str]:
    return [item["model_id"] for item in endpoint_capability_states(db, endpoint) if item["eligible"]]


def set_declared(db, endpoint: ModelEndpoint, model_id: str, value: bool | None) -> ModelCapability:
    # ModelCapability has no ORM relationship to order pending inserts for us.
    # Materialize a newly-created parent before adding its FK child.
    if endpoint in getattr(db, "new", ()):
        db.flush([endpoint])
    row = db.get(ModelCapability, (endpoint.id, model_id))
    if row is None:
        row = ModelCapability(endpoint_id=endpoint.id, model_id=model_id)
        db.add(row)
    row.tools_declared = value
    # Manual preference belongs to the endpoint/model pair and must survive
    # endpoint URL, auth, or transport changes. Fingerprints are probe-only.
    row.declaration_fingerprint = None
    return row


def set_verified(db, endpoint: ModelEndpoint, model_id: str, value: bool | None) -> ModelCapability:
    if endpoint in getattr(db, "new", ()):
        db.flush([endpoint])
    row = db.get(ModelCapability, (endpoint.id, model_id))
    if row is None:
        row = ModelCapability(endpoint_id=endpoint.id, model_id=model_id)
        db.add(row)
    row.tools_verified = value
    row.tools_verified_at = datetime.now(timezone.utc).replace(tzinfo=None) if value is not None else None
    row.verification_fingerprint = capability_fingerprint(endpoint, model_id) if value is not None else None
    return row


def declare_current_models(db, endpoint: ModelEndpoint, value: bool | None) -> None:
    for model_id in _endpoint_enabled_models(endpoint):
        set_declared(db, endpoint, model_id, value)


def migrate_legacy_model_capabilities() -> None:
    """Retire legacy declarations while preserving probe evidence and new toggles."""
    db = SessionLocal()
    try:
        legacy_rows = db.query(ModelCapability).filter(
            ModelCapability.declaration_fingerprint.is_not(None)
        ).all()
        for row in legacy_rows:
            row.tools_declared = None
            row.declaration_fingerprint = None
        endpoints = db.query(ModelEndpoint).filter(
            ModelEndpoint.supports_tools.is_not(None)
        ).all()
        for endpoint in endpoints:
            endpoint.supports_tools = None
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
