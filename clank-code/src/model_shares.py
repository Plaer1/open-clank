"""Named-user model grants and their runtime projection."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timezone
import json
import threading
from typing import Any, Optional

from core.database import (
    MimoAuthStore,
    ModelEndpoint,
    ModelShare,
    ModelShareSubscription,
)


SHARED_ENDPOINT_PREFIX = "shared:"
_AUTH_MERGE_LOCK = threading.RLock()
_MISSING = object()


@dataclass(frozen=True)
class SharedModelAccess:
    share_id: str
    actor_owner: str
    credential_owner: str
    source_kind: str
    source_id: str
    model_id: str
    revision: int


def _owner(value: object) -> str:
    return str(value or "").strip().lower()


def _json_record(payload: object) -> dict[str, Any]:
    if not isinstance(payload, str) or not payload.strip():
        return {}
    try:
        value = json.loads(payload)
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def native_provider_id(model_id: object) -> str:
    """Return the bundled runtime provider behind one public native model."""
    from src.openclank.mimo_projection import runtime_native_model_id

    return runtime_native_model_id(str(model_id or "")).partition("/")[0]


def shared_runtime_provider_id(share_id: str) -> str:
    """Collision-free direct-provider ID used only inside one recipient worker."""
    from src.endpoint_resolver import direct_runtime_provider_id

    return direct_runtime_provider_id(f"shared-{share_id}")


def shared_endpoint_id(share_id: str) -> str:
    return f"{SHARED_ENDPOINT_PREFIX}{share_id}"


def share_id_from_endpoint(endpoint_id: object) -> Optional[str]:
    value = str(endpoint_id or "").strip()
    if not value.startswith(SHARED_ENDPOINT_PREFIX):
        return None
    share_id = value[len(SHARED_ENDPOINT_PREFIX):]
    return share_id or None


def resolve_shared_model_access(
    db,
    *,
    actor_owner: str,
    endpoint_id: object,
    model_id: str | None = None,
) -> Optional[SharedModelAccess]:
    """Resolve an opted-in share without exposing or copying its credential."""
    share_id = share_id_from_endpoint(endpoint_id)
    if share_id is None:
        return None
    actor = str(actor_owner or "").strip().lower()
    if not actor:
        return None
    row = (
        db.query(ModelShare, ModelShareSubscription)
        .join(
            ModelShareSubscription,
            ModelShareSubscription.share_id == ModelShare.id,
        )
        .filter(
            ModelShare.id == share_id,
            ModelShare.active == True,  # noqa: E712
            ModelShareSubscription.subscriber == actor,
            ModelShareSubscription.enabled == True,  # noqa: E712
        )
        .first()
    )
    if row is None:
        return None
    share, _subscription = row
    if share.owner == actor:
        return None
    if model_id is not None and share.model_id != model_id:
        return None
    return SharedModelAccess(
        share_id=share.id,
        actor_owner=actor,
        credential_owner=share.owner,
        source_kind=share.source_kind,
        source_id=share.source_id,
        model_id=share.model_id,
        revision=int(share.revision or 1),
    )


def enabled_shared_accesses(db, actor_owner: str) -> list[SharedModelAccess]:
    """Return every exact model grant the named recipient opted into."""
    actor = _owner(actor_owner)
    if not actor:
        return []
    rows = (
        db.query(ModelShare, ModelShareSubscription)
        .join(
            ModelShareSubscription,
            ModelShareSubscription.share_id == ModelShare.id,
        )
        .filter(
            ModelShare.active == True,  # noqa: E712
            ModelShare.owner != actor,
            ModelShareSubscription.subscriber == actor,
            ModelShareSubscription.enabled == True,  # noqa: E712
        )
        .all()
    )
    return [
        SharedModelAccess(
            share_id=share.id,
            actor_owner=actor,
            credential_owner=share.owner,
            source_kind=share.source_kind,
            source_id=share.source_id,
            model_id=share.model_id,
            revision=int(share.revision or 1),
        )
        for share, _subscription in rows
    ]


def own_native_auth(db, owner: str) -> dict[str, Any]:
    row = db.query(MimoAuthStore).filter(MimoAuthStore.owner == _owner(owner)).first()
    return _json_record(row.payload if row is not None else None)


def source_native_auth(
    db,
    access: SharedModelAccess,
) -> tuple[str, Any] | None:
    """Return the exact native provider credential behind one trusted grant."""
    if access.source_kind != "native":
        return None
    provider_id = native_provider_id(access.model_id)
    credential = own_native_auth(db, access.credential_owner).get(provider_id)
    return (provider_id, credential) if credential is not None else None


def source_endpoint_for_access(db, access: SharedModelAccess):
    if access.source_kind != "endpoint":
        return None
    endpoint = db.query(ModelEndpoint).filter(
        ModelEndpoint.id == access.source_id,
        ModelEndpoint.owner == access.credential_owner,
        ModelEndpoint.is_enabled == True,  # noqa: E712
    ).first()
    if endpoint is None or (endpoint.model_type or "llm") != "llm":
        return None
    from src.endpoint_resolver import _endpoint_enabled_models

    return endpoint if access.model_id in _endpoint_enabled_models(endpoint) else None


def runtime_model_for_access(
    db,
    access: SharedModelAccess,
) -> str | None:
    """Resolve one live grant to the model ID used inside its private worker."""
    if access.source_kind == "endpoint":
        if source_endpoint_for_access(db, access) is None:
            return None
        return f"{shared_runtime_provider_id(access.share_id)}/{access.model_id}"
    if access.source_kind != "native":
        return None
    public_provider = access.model_id.split("/", 1)[0]
    if access.source_id not in {"mimo:auto", f"mimo:{public_provider}"}:
        return None
    if source_native_auth(db, access) is None:
        return None
    from src.openclank.mimo_projection import runtime_native_model_id

    return runtime_native_model_id(access.model_id)


def source_tools_enabled(
    db,
    access: SharedModelAccess,
) -> bool | None:
    """Return the source route's current manual Tools policy."""
    if access.source_kind == "native":
        return True
    endpoint = source_endpoint_for_access(db, access)
    if endpoint is None:
        return None
    from src.model_capabilities import endpoint_capability_states

    state = next(
        (
            item
            for item in endpoint_capability_states(db, endpoint)
            if item["model_id"] == access.model_id
        ),
        None,
    )
    return state.get("tools_enabled", True) if state is not None else True


def merge_runtime_auth_changes(
    db,
    *,
    payload: dict[str, Any],
    baseline: dict[str, Any],
    provider_sources: dict[str, str],
    file_updated_at: float,
) -> None:
    """Merge only credentials this worker changed since it was seeded."""
    with _AUTH_MERGE_LOCK:
        rows: dict[str, MimoAuthStore | None] = {}
        records: dict[str, dict[str, Any]] = {}
        changed_rows: set[str] = set()
        for provider_id, source_owner in provider_sources.items():
            source_owner = _owner(source_owner)
            if not provider_id:
                continue
            file_has = provider_id in payload
            seed_has = provider_id in baseline
            file_value = payload.get(provider_id, _MISSING)
            seed_value = baseline.get(provider_id, _MISSING)
            if file_has == seed_has and file_value == seed_value:
                continue

            if source_owner not in rows:
                row = db.query(MimoAuthStore).filter(
                    MimoAuthStore.owner == source_owner
                ).first()
                rows[source_owner] = row
                records[source_owner] = _json_record(
                    row.payload if row is not None else None
                )
            row = rows[source_owner]
            record = records[source_owner]
            current_has = provider_id in record
            current_value = record.get(provider_id, _MISSING)
            db_changed_since_seed = (
                current_has != seed_has or current_value != seed_value
            )
            if db_changed_since_seed:
                updated = getattr(row, "updated_at", None) if row is not None else None
                db_updated_at = (
                    updated.replace(tzinfo=timezone.utc).timestamp()
                    if updated is not None
                    else 0.0
                )
                if not file_updated_at or db_updated_at >= file_updated_at:
                    continue

            if file_has:
                record[provider_id] = file_value
            else:
                record.pop(provider_id, None)
            changed_rows.add(source_owner)

        for source_owner in changed_rows:
            text = json.dumps(
                records[source_owner],
                sort_keys=True,
                separators=(",", ":"),
            )
            row = rows[source_owner]
            if row is None:
                db.add(MimoAuthStore(owner=source_owner, payload=text))
            else:
                row.payload = text
        if changed_rows:
            db.commit()


def persist_shared_native_auth(
    db,
    *,
    payload: dict[str, Any],
    shared_sources: dict[str, str],
) -> None:
    """Write refreshes from a dedicated shared worker back to its source."""
    for provider_id, source_owner in shared_sources.items():
        if provider_id not in payload:
            continue
        source_row = db.query(MimoAuthStore).filter(
            MimoAuthStore.owner == source_owner
        ).first()
        source_payload = _json_record(
            source_row.payload if source_row is not None else None
        )
        source_payload[provider_id] = payload[provider_id]
        source_text = json.dumps(
            source_payload,
            sort_keys=True,
            separators=(",", ":"),
        )
        if source_row is None:
            db.add(MimoAuthStore(owner=source_owner, payload=source_text))
        else:
            source_row.payload = source_text
