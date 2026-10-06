"""Owner-bound opaque identities for public Stats resources.

Stats queries use canonical identifiers internally.  Browser state and exports
must use these projections so an account, provider, workspace, model, route, or
session identifier never becomes client-side authority.
"""

from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass
from typing import Iterable


IDENTITY_DIMENSIONS = frozenset({
    "account_id",
    "provider_id",
    "workspace_id",
    "route_id",
    "requested_model",
    "actual_model",
    "session_id",
    "operation_id",
})

_LABELS = {
    "account_id": "Account",
    "provider_id": "Provider",
    "workspace_id": "Workspace",
    "route_id": "Route",
    "requested_model": "Requested model",
    "actual_model": "Model",
    "session_id": "Session",
    "operation_id": "Operation",
}


class StatsIdentityError(ValueError):
    pass


def owner_scope(owner: str) -> str:
    value = str(owner or "").strip().lower()
    if not value:
        raise StatsIdentityError("owner is required")
    return hashlib.sha256(f"openclank-stats-owner-v1\0{value}".encode()).hexdigest()[:16]


def identity_handle(owner: str, dimension: str, value: str) -> str:
    if dimension not in IDENTITY_DIMENSIONS:
        raise StatsIdentityError(f"unsupported identity dimension: {dimension}")
    owner_value = str(owner or "").strip().lower()
    identity_value = str(value or "").strip()
    if not owner_value or not identity_value:
        raise StatsIdentityError("owner and identity value are required")
    digest = hmac.new(
        hashlib.sha256(f"openclank-stats-identity-v1\0{owner_value}".encode()).digest(),
        f"{dimension}\0{identity_value}".encode(),
        hashlib.sha256,
    ).hexdigest()[:24]
    return f"{dimension.removesuffix('_id').replace('_', '-')}_{digest}"


@dataclass(frozen=True)
class PublicIdentity:
    handle: str
    label: str

    def as_dict(self) -> dict[str, str]:
        return {"handle": self.handle, "label": self.label}


class IdentityCatalog:
    """A deterministic result-local label catalog with owner-bound handles."""

    def __init__(self, owner: str, dimension: str, values: Iterable[str | None], *, labels: dict[str, str] | None = None):
        if dimension not in IDENTITY_DIMENSIONS:
            raise StatsIdentityError(f"unsupported identity dimension: {dimension}")
        self.owner = str(owner or "").strip().lower()
        if not self.owner:
            raise StatsIdentityError("owner is required")
        self.dimension = dimension
        normalized = sorted({str(value).strip() for value in values if str(value or "").strip()})
        stem = _LABELS[dimension]
        def display_label(raw):
            label = (labels or {}).get(raw)
            # Managed observed model names may be connection-qualified. The
            # suffix is presentation only; the complete value still hashes.
            model = raw.rsplit("/", 1)[-1] if raw.startswith("pcn_") else raw
            if dimension in {"actual_model", "requested_model"}:
                return str(label or (labels or {}).get(model) or model)
            return str(label or f"{stem} unavailable")
        display_names = {raw: display_label(raw) for raw in normalized}
        if dimension in {"actual_model", "requested_model"}:
            from collections import Counter
            repeated = Counter(display_names.values())
            for raw, label in display_names.items():
                if repeated[label] > 1:
                    model_id = raw.rsplit("/", 1)[-1] if raw.startswith("pcn_") else raw
                    connection = (labels or {}).get(raw.split("/", 1)[0]) if raw.startswith("pcn_") else None
                    qualifier = f"{model_id} · {connection}" if connection else model_id
                    display_names[raw] = f"{label} ({qualifier})"
        self._by_raw = {
            raw: PublicIdentity(identity_handle(self.owner, dimension, raw), display_names[raw])
            for index, raw in enumerate(normalized, start=1)
        }
        self._by_handle = {public.handle: raw for raw, public in self._by_raw.items()}

    def project(self, value: str | None) -> dict[str, str] | None:
        raw = str(value or "").strip()
        public = self._by_raw.get(raw)
        return public.as_dict() if public else None

    def choices(self) -> list[dict[str, str]]:
        return [self._by_raw[raw].as_dict() for raw in sorted(self._by_raw)]

    def resolve(self, handle: str) -> str:
        try:
            return self._by_handle[str(handle)]
        except KeyError as exc:
            raise StatsIdentityError("unknown or out-of-scope Stats identity handle") from exc


def safe_scope(owner: str, scope: dict | None, *, catalogs: dict[str, IdentityCatalog] | None = None) -> dict:
    """Project a query scope without returning raw owner or filter identities."""
    source = dict(scope or {})
    source.pop("owner", None)
    filters = dict(source.pop("filters", {}) or {})
    safe_filters: dict[str, object] = {}
    for key, value in filters.items():
        if key in IDENTITY_DIMENSIONS and value not in (None, ""):
            catalog = (catalogs or {}).get(key)
            projected = catalog.project(value) if catalog else None
            if projected is None and catalog:
                try:
                    projected = catalog.project(catalog.resolve(value))
                except StatsIdentityError:
                    pass
            if projected is None:
                raise StatsIdentityError(f"missing public identity catalog for {key}")
            safe_filters[key] = projected
        else:
            safe_filters[key] = value
    source["filters"] = safe_filters
    source["owner_scope"] = owner_scope(owner)
    return source
