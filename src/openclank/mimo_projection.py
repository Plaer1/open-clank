"""Canonical, owner-scoped MiMo provider projection and durable generation."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

from core.database import MimoProjectionState, SessionLocal, utcnow_naive
from src.secret_storage import keyed_digest


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


@dataclass(frozen=True)
class ProjectionSnapshot:
    owner: str
    providers: dict[str, dict]
    small_model: str | None
    fingerprint: str
    credential_digests: dict[str, str]
    source_endpoints: dict[str, str]
    native_auth_digest: str | None
    credentials: dict[str, str] = field(repr=False, compare=False)

    @property
    def public_id(self) -> str:
        return self.fingerprint[:12]

    def run_closure(self, provider_id: str, model_id: str) -> str | None:
        provider = self.providers.get(provider_id)
        if provider is None and not provider_id.startswith("ody-") and self.native_auth_digest:
            return _canonical({
                "native_provider": provider_id,
                "model": model_id,
                "auth_digest": self.native_auth_digest,
                "small_model": self.small_model,
            })
        models = (provider or {}).get("models") or {}
        if provider is None or model_id not in models:
            return None
        return _canonical({
            "provider": {key: value for key, value in provider.items() if key != "models"},
            "model": models[model_id] if isinstance(models, dict) else model_id,
            "credential_digest": self.credential_digests.get(provider_id),
            "small_model": self.small_model,
            "source_endpoint": self.source_endpoints.get(provider_id),
        })


def build_projection_snapshot(owner: str, *, inherit_host_providers: bool = False) -> ProjectionSnapshot:
    """Build the exact effective spawn projection; raw secrets stay in-memory."""
    from src.openclank.mimo_supervisor import (
        ENDPOINT_PROVIDER_PREFIX,
        _endpoint_registry_providers,
        _load_openclaw_providers,
        _load_stored_auth,
        _pick_small_model,
    )

    providers: dict[str, dict] = {}
    credentials: dict[str, str] = {}
    if inherit_host_providers:
        config, secret_values = _load_openclaw_providers()
        providers.update(config.get("provider") or {})
        credentials.update(secret_values)
    config, secret_values = _endpoint_registry_providers(owner)
    for provider_id, provider in (config.get("provider") or {}).items():
        providers.setdefault(provider_id, provider)
    for provider_id, secret in secret_values.items():
        credentials.setdefault(provider_id, secret)

    digests = {
        provider_id: keyed_digest(secret, context=f"mimo-projection:{owner}:{provider_id}")
        for provider_id, secret in credentials.items()
        if provider_id in providers
    }
    sources = {
        provider_id: provider_id[len(ENDPOINT_PROVIDER_PREFIX):]
        for provider_id in providers
        if provider_id.startswith(ENDPOINT_PROVIDER_PREFIX)
    }
    small_model = _pick_small_model(providers)
    stored_auth, _ = _load_stored_auth(owner)
    native_auth_digest = (
        keyed_digest(stored_auth, context=f"mimo-native-auth:{owner}")
        if stored_auth else None
    )
    material = {
        "owner": owner,
        "providers": providers,
        "credential_digests": digests,
        "source_endpoints": sources,
        "small_model": small_model,
        "native_auth_digest": native_auth_digest,
    }
    fingerprint = hashlib.sha256(_canonical(material).encode("utf-8")).hexdigest()
    return ProjectionSnapshot(
        owner=owner,
        providers=providers,
        small_model=small_model,
        fingerprint=fingerprint,
        credential_digests=digests,
        source_endpoints=sources,
        native_auth_digest=native_auth_digest,
        credentials=credentials,
    )


def reconcile_projection(snapshot: ProjectionSnapshot, *, materializing: bool) -> dict[str, Any]:
    """Advance generation only for a changed committed effective projection."""
    db = SessionLocal()
    try:
        row = db.get(MimoProjectionState, snapshot.owner)
        changed = row is None or row.desired_fingerprint != snapshot.fingerprint
        if row is None:
            row = MimoProjectionState(owner_id=snapshot.owner, generation=1)
            db.add(row)
        elif changed:
            row.generation += 1
        if changed:
            row.desired_fingerprint = snapshot.fingerprint
            row.status = "pending" if materializing else "not_materialized"
            row.last_error_code = None
            row.requested_at = utcnow_naive()
            row.installed_at = None
        elif materializing and row.status != "installed":
            row.status = "pending"
            row.requested_at = utcnow_naive()
        db.commit()
        return projection_public(row)
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def mark_projection(
    owner: str,
    fingerprint: str,
    generation: int,
    *,
    status: str,
    error_code: str | None = None,
) -> None:
    """Update status only if it still describes the current desired generation."""
    db = SessionLocal()
    try:
        row = db.get(MimoProjectionState, owner)
        if row and row.desired_fingerprint == fingerprint and row.generation == generation:
            row.status = status
            row.last_error_code = error_code
            if status == "installed":
                row.installed_at = utcnow_naive()
            db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def projection_public(row: MimoProjectionState) -> dict[str, Any]:
    return {
        "owner": row.owner_id,
        "desired_fingerprint": row.desired_fingerprint[:12],
        "generation": row.generation,
        "status": row.status,
        "last_error_code": row.last_error_code,
    }


def safe_additive_delta(old: ProjectionSnapshot, new: ProjectionSnapshot) -> bool:
    """True only when old authority is byte-identical and the delta adds models/providers."""
    if (
        old.owner != new.owner
        or old.small_model != new.small_model
        or old.native_auth_digest != new.native_auth_digest
    ):
        return False
    for provider_id, old_provider in old.providers.items():
        new_provider = new.providers.get(provider_id)
        if new_provider is None:
            return False
        if old.credential_digests.get(provider_id) != new.credential_digests.get(provider_id):
            return False
        if _canonical({k: v for k, v in old_provider.items() if k != "models"}) != _canonical(
            {k: v for k, v in new_provider.items() if k != "models"}
        ):
            return False
        old_models = old_provider.get("models") or {}
        new_models = new_provider.get("models") or {}
        if not isinstance(old_models, dict) or not isinstance(new_models, dict):
            return False
        for model_id, model in old_models.items():
            if model_id not in new_models or _canonical(model) != _canonical(new_models[model_id]):
                return False
    return True
