"""Opaque, owner-bound references for the unified Files namespace.

Resource references are capabilities to *name* an already-authorized provider
resource, not permission grants.  Every consumer must still resolve current
People/Agent policy and provider ownership before performing an operation.

The encrypted wire token deliberately carries provider origin identity so the
browser never has to know which legacy route/table/path owns a resource.  The
stable public identity is a keyed digest: it survives display-name changes but
cannot be used to guess another owner's provider identifiers.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Iterable, Optional

from src.secret_storage import decrypt, encrypt, keyed_digest


RESOURCE_REF_VERSION = 1
DEFAULT_RESOURCE_REF_TTL_SECONDS = 15 * 60
MAX_RESOURCE_REF_TTL_SECONDS = 24 * 60 * 60

PROVIDERS = frozenset({"host", "copal", "gallery", "library", "published", "chat", "research"})
RESOURCE_KINDS = frozenset(
    {
        "provider_root",
        "virtual_folder",
        "folder",
        "exact_file",
        "file",
        "document",
        "image",
        "audio",
        "asset",
        "album",
        "chat",
        "research",
        "archive",
        "trash",
        "history",
        "symlink",
        "special",
    }
)
RESOURCE_CAPABILITIES = frozenset(
    {
        "children",
        "stat",
        "read",
        "preview",
        "download",
        "open",
        "write",
        "rename",
        "move",
        "copy",
        "trash",
        "restore",
        "favorite",
        "tag",
        "archive",
        "publish",
        "search",
        "watch",
    }
)


class ResourceRefError(ValueError):
    def __init__(self, message: str, *, code: str = "invalid_resource_ref") -> None:
        super().__init__(message)
        self.code = code


def _required(value: object, *, field: str) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise ResourceRefError(f"{field} is required")
    return normalized


def _capabilities(values: Iterable[str]) -> tuple[str, ...]:
    normalized = tuple(sorted({str(value).strip().lower() for value in values if str(value).strip()}))
    if not set(normalized).issubset(RESOURCE_CAPABILITIES):
        raise ResourceRefError("resource reference contains an unsupported capability")
    return normalized


def stable_resource_id(*, owner_subject_id: str, provider: str, origin_id: str) -> str:
    """Return a rename-stable, non-enumerable identity for one provider origin."""
    owner = _required(owner_subject_id, field="owner subject")
    provider_name = _required(provider, field="provider").lower()
    origin = _required(origin_id, field="provider origin")
    if provider_name not in PROVIDERS:
        raise ResourceRefError("unsupported resource provider")
    digest = keyed_digest(
        json.dumps([provider_name, owner, origin], ensure_ascii=False, separators=(",", ":")),
        context=f"open-clank/resource-ref/{RESOURCE_REF_VERSION}",
    )
    return f"resource-{digest}"


@dataclass(frozen=True)
class ResourceRef:
    token: str
    stable_id: str
    owner_subject_id: str
    provider: str
    origin_id: str
    kind: str
    capabilities: tuple[str, ...]
    policy_generation: int
    issued_unix_ms: int
    expires_unix_ms: int
    location_id: Optional[str] = None
    workspace_id: Optional[str] = None
    parent_stable_id: Optional[str] = None

    def public_dict(self) -> dict[str, object]:
        """Browser-safe identity. Provider origin and owner never leave the server."""
        return {
            "id": self.stable_id,
            "ref": self.token,
            "provider": self.provider,
            "kind": self.kind,
            "capabilities": list(self.capabilities),
            "policy_generation": self.policy_generation,
            "expires_unix_ms": self.expires_unix_ms,
        }


def issue_resource_ref(
    *,
    owner_subject_id: str,
    provider: str,
    origin_id: str,
    kind: str,
    capabilities: Iterable[str],
    policy_generation: int,
    location_id: str | None = None,
    workspace_id: str | None = None,
    parent_stable_id: str | None = None,
    ttl_seconds: int = DEFAULT_RESOURCE_REF_TTL_SECONDS,
    now_unix_ms: int | None = None,
) -> ResourceRef:
    owner = _required(owner_subject_id, field="owner subject")
    provider_name = _required(provider, field="provider").lower()
    origin = _required(origin_id, field="provider origin")
    resource_kind = _required(kind, field="resource kind").lower()
    if provider_name not in PROVIDERS:
        raise ResourceRefError("unsupported resource provider")
    if resource_kind not in RESOURCE_KINDS:
        raise ResourceRefError("unsupported resource kind")
    caps = _capabilities(capabilities)
    try:
        generation = int(policy_generation)
        ttl = int(ttl_seconds)
    except (TypeError, ValueError) as exc:
        raise ResourceRefError("invalid resource reference lifetime or generation") from exc
    if generation < 0:
        raise ResourceRefError("policy generation cannot be negative")
    if ttl < 1 or ttl > MAX_RESOURCE_REF_TTL_SECONDS:
        raise ResourceRefError("resource reference lifetime is outside the allowed range")
    issued = int(now_unix_ms if now_unix_ms is not None else time.time() * 1000)
    stable_id = stable_resource_id(
        owner_subject_id=owner,
        provider=provider_name,
        origin_id=origin,
    )
    payload = {
        "v": RESOURCE_REF_VERSION,
        "id": stable_id,
        "o": owner,
        "p": provider_name,
        "r": origin,
        "k": resource_kind,
        "c": list(caps),
        "g": generation,
        "iat": issued,
        "exp": issued + ttl * 1000,
        "l": str(location_id) if location_id else None,
        "w": str(workspace_id) if workspace_id else None,
        "parent": str(parent_stable_id) if parent_stable_id else None,
    }
    sealed = encrypt(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    if not sealed.startswith("enc:"):
        raise ResourceRefError("resource reference could not be sealed")
    token = f"rr{RESOURCE_REF_VERSION}.{sealed[4:]}"
    return ResourceRef(
        token=token,
        stable_id=stable_id,
        owner_subject_id=owner,
        provider=provider_name,
        origin_id=origin,
        kind=resource_kind,
        capabilities=caps,
        policy_generation=generation,
        issued_unix_ms=issued,
        expires_unix_ms=issued + ttl * 1000,
        location_id=str(location_id) if location_id else None,
        workspace_id=str(workspace_id) if workspace_id else None,
        parent_stable_id=str(parent_stable_id) if parent_stable_id else None,
    )


def _decode_resource_ref(token: str) -> ResourceRef:
    """Authenticate and decode a ref without granting any capability.

    Expiry, policy-generation, owner, and requested-capability checks belong to
    the public resolver below. Keeping the authenticated envelope parsing in one
    place also lets the narrowly scoped reissue resolver recover the identity of
    an expired managed exact-view ref before its provider reauthorizes it.
    """

    raw_token = str(token or "").strip()
    prefix = f"rr{RESOURCE_REF_VERSION}."
    if not raw_token.startswith(prefix):
        raise ResourceRefError("resource reference is malformed")
    plaintext = decrypt("enc:" + raw_token[len(prefix) :])
    if not plaintext:
        raise ResourceRefError("resource reference could not be opened")
    try:
        payload = json.loads(plaintext)
        version = int(payload["v"])
        owner = _required(payload["o"], field="owner subject")
        provider = _required(payload["p"], field="provider").lower()
        origin = _required(payload["r"], field="provider origin")
        kind = _required(payload["k"], field="resource kind").lower()
        generation = int(payload["g"])
        issued = int(payload["iat"])
        expires = int(payload["exp"])
        stable_id = _required(payload["id"], field="stable resource id")
        caps = _capabilities(payload.get("c") or ())
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ResourceRefError("resource reference payload is malformed") from exc
    if version != RESOURCE_REF_VERSION or provider not in PROVIDERS or kind not in RESOURCE_KINDS:
        raise ResourceRefError("resource reference version or type is unsupported")
    expected_stable = stable_resource_id(
        owner_subject_id=owner,
        provider=provider,
        origin_id=origin,
    )
    if stable_id != expected_stable:
        raise ResourceRefError("resource reference identity is invalid")
    return ResourceRef(
        token=raw_token,
        stable_id=stable_id,
        owner_subject_id=owner,
        provider=provider,
        origin_id=origin,
        kind=kind,
        capabilities=caps,
        policy_generation=generation,
        issued_unix_ms=issued,
        expires_unix_ms=expires,
        location_id=str(payload.get("l")) if payload.get("l") else None,
        workspace_id=str(payload.get("w")) if payload.get("w") else None,
        parent_stable_id=str(payload.get("parent")) if payload.get("parent") else None,
    )


def resolve_resource_ref(
    token: str,
    *,
    expected_owner_subject_id: str,
    current_policy_generation: int | None = None,
    required_capability: str | None = None,
    now_unix_ms: int | None = None,
) -> ResourceRef:
    expected_owner = _required(expected_owner_subject_id, field="owner subject")
    ref = _decode_resource_ref(token)
    if ref.owner_subject_id != expected_owner:
        raise ResourceRefError("resource reference is unavailable", code="resource_unavailable")
    now = int(now_unix_ms if now_unix_ms is not None else time.time() * 1000)
    if ref.expires_unix_ms <= now or ref.issued_unix_ms > now + 60_000:
        raise ResourceRefError("resource reference expired", code="resource_ref_stale")
    if current_policy_generation is not None and ref.policy_generation != int(current_policy_generation):
        raise ResourceRefError("resource reference policy is stale", code="resource_ref_stale")
    capability = str(required_capability or "").strip().lower()
    if capability and (capability not in RESOURCE_CAPABILITIES or capability not in ref.capabilities):
        raise ResourceRefError("resource capability is unavailable", code="resource_unavailable")
    return ref


def resolve_resource_ref_for_reissue(
    token: str,
    *,
    expected_owner_subject_id: str,
    required_capability: str | None = None,
    now_unix_ms: int | None = None,
) -> ResourceRef:
    """Recover an expired ref's identity for current-provider reauthorization.

    This does *not* make the old ref usable again: it deliberately ignores only
    expiry and policy generation. Envelope integrity, immutable owner identity,
    stable identity, supported types, capabilities, and future-issued tokens are
    still rejected. Callers must then re-stat and authorize the provider record
    before issuing any new ResourceRef.
    """

    expected_owner = _required(expected_owner_subject_id, field="owner subject")
    ref = _decode_resource_ref(token)
    if ref.owner_subject_id != expected_owner:
        raise ResourceRefError("resource reference is unavailable", code="resource_unavailable")
    now = int(now_unix_ms if now_unix_ms is not None else time.time() * 1000)
    if ref.issued_unix_ms > now + 60_000:
        raise ResourceRefError("resource reference expired", code="resource_ref_stale")
    capability = str(required_capability or "").strip().lower()
    if capability and (capability not in RESOURCE_CAPABILITIES or capability not in ref.capabilities):
        raise ResourceRefError("resource capability is unavailable", code="resource_unavailable")
    return ref


__all__ = [
    "DEFAULT_RESOURCE_REF_TTL_SECONDS",
    "PROVIDERS",
    "RESOURCE_CAPABILITIES",
    "RESOURCE_KINDS",
    "RESOURCE_REF_VERSION",
    "ResourceRef",
    "ResourceRefError",
    "issue_resource_ref",
    "resolve_resource_ref",
    "resolve_resource_ref_for_reissue",
    "stable_resource_id",
]
