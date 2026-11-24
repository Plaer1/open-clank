"""Normalized, owner-scoped provider control-plane API.

Provider credentials enter this module only as write-only request values.  No
serializer in this module reads ``credential_envelope`` or
``credential_fingerprint``; plaintext disclosure remains confined to the
capability-bound managed-engine callback path.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import asyncio
import html
import logging
import os
import re
import secrets
import threading
import time
from time import perf_counter
from typing import Any, Mapping, Optional
from urllib.parse import urlsplit

from fastapi import APIRouter, Header, HTTPException, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, Field, SecretStr

from core.middleware import require_admin
from src.auth_helpers import effective_user, require_user
from src.model_catalog import build_model_catalog
from src.openclank.provider_control import (
    BoundProviderEngine,
    ManagedProviderEngineControl,
    OAuthHostFlow,
    OAuthHostFlowStore,
    ProviderEngineControlError,
    ProviderEngineTimeoutError,
    ProviderEngineValidationError,
    ProviderOAuthFlowError,
)
from src.openclank.provider_family_catalog import (
    ProviderFamilyCatalogError,
    bundled_provider_family_catalog,
)
from src.openclank.provider_request_options import project_request_options
from src.openclank.generated.managed_provider_contract import PROVIDER_ENDPOINT_POLICIES
from src.openclank.chat_routing import (
    ChatRouteUnavailable,
    MANAGED_ENGINE_PUBLIC_URL,
    group_chat_routes,
    list_chat_routes,
    normalized_provider_owner,
    request_option_route_id,
    shared_provider_group_id,
)
from src.openclank.provider_store import (
    AccountSelector,
    BillingLaneMismatch,
    IdempotencyResult,
    ModelSelector,
    NoEligibleAccount,
    ProviderConflict,
    ProviderNotFound,
    ProviderRevisionConflict,
    ProviderStore,
    ProviderStoreError,
    ProviderValidationError,
    provider_family_display_name,
    RefreshLeaseBusy,
    ShareDenied,
)
from src.secret_storage import keyed_digest
from src.settings import load_settings, save_settings
from src.tool_blocks import TOOL_TAGS


_LOCAL_INSTALLATION_OWNER = "local-installation"
_IDEMPOTENCY_KEY = re.compile(r"^[\x21-\x7e]{8,200}$")
_IDENTIFIER = r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$"
_PURPOSES = frozenset(
    {
        "chat",
        "utility",
        "memory",
        "research",
        "search",
        "tasks",
        "vision",
        "images",
        "tts",
        "stt",
        "embeddings",
    }
)
_PURPOSE_OPERATIONS = {
    "chat": frozenset({"chat.stream", "chat.complete"}),
    "utility": frozenset({"chat.stream", "chat.complete"}),
    # Photo Memory uses the same selected route for vision.describe; it never
    # falls back to the separate Vision purpose.
    "memory": frozenset({"chat.complete", "vision.describe"}),
    "research": frozenset({"chat.stream", "chat.complete"}),
    "search": frozenset({"web.search"}),
    "tasks": frozenset({"chat.stream", "chat.complete"}),
    "vision": frozenset({"vision.describe", "chat.stream", "chat.complete"}),
    "images": frozenset(
        {
            "image.generate",
            "image.edit",
            "image.inpaint",
            "image.img2img",
            "image.upscale",
            "image.denoise",
            "image.segment",
            "image.remove_background",
            "image.restore_face",
        }
    ),
    "tts": frozenset({"audio.synthesize"}),
    "stt": frozenset({"audio.transcribe"}),
    "embeddings": frozenset({"embeddings.create"}),
}
_IDENTITY_FIELDS = frozenset(
    {
        "provider_display_identity",
        "organization_tenant",
        "enterprise_host",
    }
)
_SECRET_SETTING_NAMES = frozenset(
    {
        "api_key",
        "apikey",
        "access_token",
        "refresh_token",
        "token",
        "secret",
        "client_secret",
        "password",
        "credential",
        "credentials",
        "authorization",
    }
)

logger = logging.getLogger(__name__)


# The response is an immutable build-keyed bundle.  This private browser cache
# avoids repeat JSON transfer while authorization is still checked on misses.
_PROVIDER_FAMILY_BROWSER_CACHE_SECONDS = 5 * 60
_ACCOUNT_INVENTORY_FRESHNESS_SECONDS = 15 * 60
_ACCOUNT_INVENTORY_CACHE: dict[tuple[str, str, str, int, str, str], dict[str, Any]] = {}
_ACCOUNT_INVENTORY_LOCK = threading.RLock()
_ACCOUNT_INVENTORY_FLIGHTS: dict[tuple[str, str, str, int, str, str], asyncio.Task] = {}
_OAUTH_COMPLETION_LOCKS: dict[str, threading.Lock] = {}
_OAUTH_COMPLETION_LOCK_REFS: dict[str, int] = {}
_OAUTH_COMPLETION_LOCKS_GUARD = threading.Lock()
_OAUTH_START_TIMEOUT_SECONDS = 15
_OAUTH_VALIDATE_TIMEOUT_SECONDS = 10
_OAUTH_POLL_TIMEOUT_SECONDS = 5
_OAUTH_CANCEL_TIMEOUT_SECONDS = 3
_OAUTH_CALLBACK_TIMEOUT_SECONDS = 20
_OAUTH_FLOW_RETENTION_SECONDS = 10 * 60
_OAUTH_SAFE_ERROR_CODES = frozenset(
    {
        "oauth_validation_failed",
        "oauth_worker_lost",
        "oauth_timeout",
        "oauth_exchange_failed",
        "oauth_credential_mismatch",
        "oauth_endpoint_mismatch",
        "oauth_completion_failed",
        "oauth_revision_conflict",
        "oauth_discovery_unavailable",
        "oauth_reauth_required",
        "oauth_cancelled",
        "oauth_expired",
    }
)


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _SecretSafeRoute(APIRoute):
    """Keep rejected request values out of provider validation responses."""

    def get_route_handler(self):
        original = super().get_route_handler()

        async def safe_handler(request: Request):
            try:
                return await original(request)
            except RequestValidationError as exc:
                errors = [
                    {
                        "type": item.get("type", "validation_error"),
                        "loc": list(item.get("loc", ())),
                        "msg": item.get("msg", "Invalid provider request"),
                    }
                    for item in exc.errors()
                ]
                raise HTTPException(422, detail=errors) from exc

        return safe_handler


class ConnectionCreate(_StrictModel):
    family_id: str = Field(min_length=1, max_length=128, pattern=_IDENTIFIER)
    adapter_id: str = Field(min_length=1, max_length=128, pattern=_IDENTIFIER)
    kind: str = Field(min_length=1, max_length=64, pattern=_IDENTIFIER)
    billing_lane: str = Field(min_length=1, max_length=64, pattern=_IDENTIFIER)
    label: str = Field(min_length=1, max_length=160)
    url: Optional[str] = Field(default=None, max_length=2048)
    settings: dict[str, Any] = Field(default_factory=dict)
    enabled: bool = True


class ConnectionUpdate(_StrictModel):
    label: Optional[str] = Field(default=None, min_length=1, max_length=160)
    url: Optional[str] = Field(default=None, max_length=2048)
    settings: Optional[dict[str, Any]] = None
    account_pool_policy: Optional[dict[str, Any]] = None
    enabled: Optional[bool] = None


class AccountCreate(_StrictModel):
    label: str = Field(min_length=1, max_length=160)
    api_key: SecretStr


class AccountUpdate(_StrictModel):
    label: Optional[str] = Field(default=None, min_length=1, max_length=160)
    api_key: Optional[SecretStr] = None


class AccountEnable(_StrictModel):
    enabled: bool


class ModelRefresh(_StrictModel):
    pass


class OAuthStartPayload(_StrictModel):
    label: str = Field(min_length=1, max_length=160)
    method: int = Field(ge=0, le=1024)
    inputs: dict[str, str] = Field(default_factory=dict)


class OAuthReauthPayload(_StrictModel):
    label: Optional[str] = Field(default=None, min_length=1, max_length=160)
    method: int = Field(ge=0, le=1024)
    inputs: dict[str, str] = Field(default_factory=dict)


class OAuthCallbackPayload(_StrictModel):
    state: str = Field(min_length=16, max_length=256)
    code: Optional[str] = Field(default=None, max_length=131_072)


class PoolOrder(_StrictModel):
    account_ids: list[str] = Field(min_length=1, max_length=256)


class UseNext(_StrictModel):
    model_route_id: str = Field(min_length=1, max_length=128)


class RouteBindingItem(_StrictModel):
    model_route_id: str = Field(min_length=1, max_length=128)
    enabled: bool = True


class RouteBindingPut(_StrictModel):
    routes: list[RouteBindingItem] = Field(min_length=1, max_length=1)


class ShareCreate(_StrictModel):
    recipient: str = Field(min_length=1, max_length=128)
    connection_id: str = Field(min_length=1, max_length=128)
    label: str = Field(default="Shared provider", min_length=1, max_length=160)
    account_selector: dict[str, Any]
    model_selector: dict[str, Any]
    disclosure_fields: list[str] = Field(default_factory=list, max_length=16)


class ShareUpdate(_StrictModel):
    account_selector: dict[str, Any]
    model_selector: dict[str, Any]
    disclosure_fields: list[str] = Field(default_factory=list, max_length=16)


class ModelShareToggle(_StrictModel):
    enabled: bool


class ToolsUpdate(_StrictModel):
    disabled: list[str] = Field(default_factory=list, max_length=256)


def _iso(value: Optional[datetime]) -> Optional[str]:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _provider_owner(request: Request, *, write: bool = False) -> str:
    state = getattr(request, "state", None)
    if getattr(state, "api_token", False):
        if getattr(state, "api_token_client_kind", "api") != "api":
            raise HTTPException(403, "This client token cannot use the provider API")
        owner = str(getattr(state, "api_token_owner", "") or "").strip().lower()
        scopes = set(getattr(state, "api_token_scopes", ()) or ())
        required = "providers:write" if write else "providers:read"
        if not owner:
            raise HTTPException(403, "API token has no provider owner")
        if required not in scopes and not (
            not write and "providers:write" in scopes
        ):
            raise HTTPException(403, f"API token requires {required}")
        return owner
    owner = str(require_user(request) or "").strip().lower()
    return owner or _LOCAL_INSTALLATION_OWNER


def _if_match(value: Optional[str]) -> int:
    if value is None:
        raise HTTPException(428, "If-Match is required")
    token = value.strip()
    if token.startswith("W/"):
        token = token[2:].strip()
    if len(token) >= 2 and token[0] == token[-1] == '"':
        token = token[1:-1]
    if not token.isdigit():
        raise HTTPException(400, "If-Match must contain a numeric revision")
    return int(token)


def _idempotency_key(value: Optional[str]) -> str:
    value = str(value or "")
    if not _IDEMPOTENCY_KEY.fullmatch(value):
        raise HTTPException(
            400,
            "Idempotency-Key must be 8-200 visible ASCII characters",
        )
    return value


def _etag(revision: int) -> str:
    return f'"{int(revision)}"'


def _raise_store_error(exc: ProviderStoreError) -> None:
    if isinstance(exc, ProviderRevisionConflict):
        raise HTTPException(
            412,
            str(exc),
            headers={"ETag": _etag(exc.current)},
        ) from exc
    if isinstance(exc, ProviderNotFound):
        raise HTTPException(404, str(exc)) from exc
    if isinstance(exc, ShareDenied):
        raise HTTPException(403, str(exc)) from exc
    if isinstance(exc, (ProviderValidationError, BillingLaneMismatch)):
        raise HTTPException(422, str(exc)) from exc
    if isinstance(exc, (NoEligibleAccount, RefreshLeaseBusy)):
        raise HTTPException(409, str(exc)) from exc
    if isinstance(exc, ProviderConflict):
        raise HTTPException(409, str(exc)) from exc
    raise HTTPException(500, "Provider store operation failed") from exc


def _assert_nonsecret_settings(value: Any) -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            normalized = str(key).strip().lower().replace("-", "_")
            if normalized in _SECRET_SETTING_NAMES:
                raise HTTPException(
                    422,
                    "Secret-bearing provider settings must be stored as account credentials",
                )
            _assert_nonsecret_settings(item)
    elif isinstance(value, list):
        for item in value:
            _assert_nonsecret_settings(item)


def _public_nonsecret_value(value: Any) -> Any:
    """Project settings/provenance defensively even for malformed old rows."""

    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, item in value.items():
            normalized = str(key).strip().lower().replace("-", "_")
            if normalized in _SECRET_SETTING_NAMES:
                continue
            result[str(key)] = _public_nonsecret_value(item)
        return result
    if isinstance(value, list):
        return [_public_nonsecret_value(item) for item in value]
    return value


def _api_key_value(value: SecretStr) -> str:
    secret = value.get_secret_value()
    if not secret or len(secret) > 131_072:
        raise HTTPException(422, "API key length is invalid")
    return secret


def _connection_json(row: Any) -> dict[str, Any]:
    return {
        "id": row.id,
        "family_id": row.family_id,
        "adapter_id": row.adapter_id,
        "kind": row.kind,
        "billing_lane": row.billing_lane,
        "label": row.label,
        "url": row.normalized_url,
        "settings": _public_nonsecret_value({k:v for k,v in dict(row.settings or {}).items() if (k in {"interface", "recipe"} if row.family_id in {"trellis", "hunyuan-shape"} else not (row.family_id == "blender" and k == "executablePath"))}),
        "rotation_policy": row.rotation_policy,
        "account_pool_policy": _public_nonsecret_value(row.account_pool_policy)
        if row.account_pool_policy is not None
        else None,
        "enabled": bool(row.enabled),
        "revision": int(row.revision),
        "created_at": _iso(row.created_at),
        "updated_at": _iso(row.updated_at),
    }


def _account_json(row: Any) -> dict[str, Any]:
    return {
        "id": row.id,
        "connection_id": row.connection_id,
        "label": row.label,
        "auth_method": row.auth_method,
        "acquisition_method": row.acquisition_method,
        "auth_class": row.auth_class,
        "order": int(row.sort_order),
        "enabled": bool(row.enabled),
        "has_credential": bool(row.credential_envelope),
        "credential_revision": int(row.credential_version),
        "identity": {
            str(key): value
            for key, value in dict(row.safe_identity or {}).items()
            if key in _IDENTITY_FIELDS and value is not None
        },
        "revision": int(row.revision),
        "created_at": _iso(row.created_at),
        "updated_at": _iso(row.updated_at),
    }


def _model_json(row: Any) -> dict[str, Any]:
    return {
        "id": row.id,
        "connection_id": row.connection_id,
        "model_id": row.provider_model_id,
        "display_name": row.display_name,
        "operations": list(row.operations or ()),
        "capabilities": dict(row.capabilities or {}),
        "visibility": row.visibility,
        "catalog_revision": int(row.catalog_revision),
        "provenance": _public_nonsecret_value(dict(row.provenance or {})),
        "enabled": bool(row.enabled),
        "revision": int(row.revision),
    }


def _binding_group(purpose: str, rows: list[Any]) -> dict[str, Any]:
    revision = max((int(row.revision) for row in rows), default=0)
    ordered = sorted(rows, key=lambda row: (int(row.ordinal), str(row.id)))
    selected = next((row for row in ordered if row.enabled), None)
    if selected is None and ordered:
        selected = ordered[0]
    return {
        "purpose": purpose,
        "revision": revision,
        "routes": [
            {
                "id": row.id,
                "ordinal": int(row.ordinal),
                "model_route_id": row.model_route_id,
                "enabled": bool(row.enabled),
                "revision": int(row.revision),
            }
            for row in ([selected] if selected is not None else [])
        ],
    }


def _owned_share_json(row: Any) -> dict[str, Any]:
    return {
        "id": row.id,
        "recipient": row.recipient,
        "connection_id": row.connection_id,
        "billing_lane": row.billing_lane,
        "label": row.label,
        "account_selector": dict(row.account_selector or {}),
        "model_selector": dict(row.model_selector or {}),
        "disclosure_fields": list(row.disclosure_fields or ()),
        "state": row.state,
        "revision": int(row.revision),
        "created_at": _iso(row.created_at),
        "updated_at": _iso(row.updated_at),
    }


def _slot_id(kind: str, grant_id: str, source_id: str) -> str:
    return f"{kind}_{keyed_digest(source_id, context=f'provider-share:{grant_id}:{kind}')[:24]}"


def _received_share_json(projection: Any) -> dict[str, Any]:
    row = projection.grant
    connection = projection.connection
    provider_group_id = shared_provider_group_id(
        recipient=row.recipient,
        owner=row.owner,
        connection_id=row.connection_id,
    )
    disclosures = set(row.disclosure_fields or ())
    accounts: list[dict[str, Any]] = []
    for account in projection.accounts:
        account_slot = _slot_id("account", row.id, account.id)
        item: dict[str, Any] = {
            "slot_id": account_slot,
            "name": f"Account {account_slot[-6:].upper()}",
            "enabled": bool(account.enabled),
        }
        if "account_label" in disclosures:
            item["label"] = account.label
        if "auth_billing_class" in disclosures:
            item["auth_class"] = account.auth_class
            item["billing_lane"] = row.billing_lane
        identity = dict(account.safe_identity or {})
        for field in (
            "provider_display_identity",
            "organization_tenant",
            "enterprise_host",
        ):
            if field in disclosures and identity.get(field) is not None:
                item[field] = identity[field]
        if "detailed_health" in disclosures:
            item["health"] = [
                {
                    "model_slot_id": _slot_id(
                        "model",
                        row.id,
                        health["model_route_id"],
                    ),
                    "eligible": bool(health.get("eligible")),
                    "account_state": health.get("account_state"),
                    "model_state": health.get("model_state"),
                    "account_cooldown_until": _iso(
                        health.get("account_cooldown_until")
                    ),
                    "model_cooldown_until": _iso(
                        health.get("model_cooldown_until")
                    ),
                    "account_last_error_code": health.get(
                        "account_last_error_code"
                    ),
                    "model_last_error_code": health.get(
                        "model_last_error_code"
                    ),
                }
                for health in projection.health_by_account.get(account.id, ())
            ]
        accounts.append(item)
    models = []
    for route in projection.model_routes:
        models.append(
            {
                "slot_id": _slot_id("model", row.id, route.id),
                "model_id": route.provider_model_id,
                "display_name": route.display_name,
                "operations": list(route.operations or ()),
                "capabilities": dict(route.capabilities or {}),
            }
        )
    provider_display_name = provider_family_display_name(connection.family_id)
    result = {
        "id": row.id,
        "label": row.label,
        "connection_slot_id": _slot_id("connection", row.id, row.connection_id),
        "provider_group_id": provider_group_id,
        "provider_group_label": f"{row.owner}'s {provider_display_name or connection.family_id}",
        "family_id": connection.family_id,
        "provider_family_id": connection.family_id,
        "provider_display_name": provider_display_name,
        "billing_lane": row.billing_lane,
        "state": row.state,
        "revision": int(row.revision),
        "disclosure_fields": sorted(disclosures),
        "accounts": accounts,
        "models": models,
        # A username is the minimum safe attribution identity.  Source
        # connection/account/model-route IDs remain represented only by opaque
        # recipient-scoped slots.
        "shared_by": row.owner,
    }
    return result


def _request_material(model: BaseModel) -> dict[str, Any]:
    def reveal(value: Any) -> Any:
        if isinstance(value, SecretStr):
            return value.get_secret_value()
        if isinstance(value, BaseModel):
            return {
                key: reveal(getattr(value, key))
                for key in sorted(value.model_fields_set)
            }
        if isinstance(value, Mapping):
            return {str(key): reveal(item) for key, item in value.items()}
        if isinstance(value, list):
            return [reveal(item) for item in value]
        return value

    return reveal(model)


def _provider_catalog_text(
    value: Any,
    field: str,
    *,
    maximum: int,
    allow_empty: bool = False,
) -> str:
    if not isinstance(value, str):
        raise ProviderEngineValidationError(
            f"Managed engine returned an invalid {field}"
        )
    text = value.strip()
    if (not text and not allow_empty) or len(text) > maximum or "\x00" in text:
        raise ProviderEngineValidationError(
            f"Managed engine returned an invalid {field}"
        )
    return text


def _provider_catalog_auth_methods(raw_catalog: Any, *, family_id: str) -> list[dict[str, Any]]:
    """Validate the narrow, nonsecret OAuth UI projection from the engine."""

    if not isinstance(raw_catalog, Mapping) or raw_catalog.get("schemaVersion") != 1:
        raise ProviderEngineValidationError(
            "Managed engine returned an invalid provider catalogue"
        )
    families = raw_catalog.get("families")
    if not isinstance(families, list) or len(families) > 1024:
        raise ProviderEngineValidationError(
            "Managed engine returned an invalid provider catalogue"
        )
    ids = [item.get("id") if isinstance(item, Mapping) else None for item in families]
    if any(not isinstance(item, str) or re.fullmatch(_IDENTIFIER, item) is None for item in ids) or len(set(ids)) != len(ids):
        raise ProviderEngineValidationError("Managed engine returned an invalid provider catalogue")
    family = next(
        (
            item
            for item in families
            if isinstance(item, Mapping) and item.get("id") == family_id
        ),
        None,
    )
    if family is None:
        raise ProviderEngineValidationError(
            "Managed engine did not return the requested provider family"
        )
    methods = family.get("authMethods")
    if not isinstance(methods, list) or len(methods) > 64:
        raise ProviderEngineValidationError(
            "Managed engine returned invalid provider sign-in methods"
        )
    family_lanes = family.get("billingLanes")
    if (
        not isinstance(family_lanes, list)
        or not family_lanes
        or any(lane not in {"metered_api", "subscription", "custom", "local"} for lane in family_lanes)
    ):
        raise ProviderEngineValidationError("Managed engine returned invalid provider billing lanes")
    endpoint_policies = family.get("endpointPolicies", {})
    expected_endpoint_policies = PROVIDER_ENDPOINT_POLICIES.get(family_id)
    if expected_endpoint_policies is None:
        if endpoint_policies:
            raise ProviderEngineValidationError("Managed engine returned an unsupported provider endpoint policy")
    elif endpoint_policies != expected_endpoint_policies:
        raise ProviderEngineValidationError("Managed engine returned an invalid provider endpoint policy")
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw_method in methods:
        if not isinstance(raw_method, Mapping):
            raise ProviderEngineValidationError(
                "Managed engine returned an invalid provider sign-in method"
            )
        method_id = _provider_catalog_text(
            raw_method.get("id"), "provider sign-in method ID", maximum=128
        )
        method_type = _provider_catalog_text(
            raw_method.get("type"), "provider sign-in method type", maximum=16
        )
        label = _provider_catalog_text(
            raw_method.get("label"), "provider sign-in method label", maximum=160
        )
        if (
            method_id in seen
            or re.fullmatch(_IDENTIFIER, method_id) is None
            or method_type not in {"api", "oauth", "none"}
        ):
            raise ProviderEngineValidationError(
                "Managed engine returned an invalid provider sign-in method"
            )
        seen.add(method_id)
        method: dict[str, Any] = {
            "id": method_id,
            "type": method_type,
            "label": label,
        }
        if method_type == "none":
            declared_keyless = any(
                item["id"] == family_id
                and any(method.get("id") == "none" and method.get("type") == "none"
                        for method in item["auth_methods"])
                for item in bundled_provider_family_catalog()["families"]
            )
            if (method_id != "none" or set(raw_method) != {"id", "type", "label"}
                    or not declared_keyless):
                raise ProviderEngineValidationError("Managed engine returned an invalid keyless method")
            result.append(method)
            continue
        acquisition_method = raw_method.get("acquisitionMethod")
        credential_kinds = raw_method.get("credentialKinds")
        billing_lanes = raw_method.get("billingLanes")
        if acquisition_method not in {"manual-key", "browser", "device"}:
            raise ProviderEngineValidationError("Managed engine returned an invalid provider acquisition method")
        if (
            not isinstance(credential_kinds, list)
            or not credential_kinds
            or len(credential_kinds) > 2
            or any(kind not in {"api", "oauth"} for kind in credential_kinds)
            or len(set(credential_kinds)) != len(credential_kinds)
        ):
            raise ProviderEngineValidationError("Managed engine returned invalid provider credential kinds")
        if (
            not isinstance(billing_lanes, list)
            or not billing_lanes
            or len(billing_lanes) > 4
            or any(lane not in {"metered_api", "subscription", "custom", "local"} for lane in billing_lanes)
            or len(set(billing_lanes)) != len(billing_lanes)
        ):
            raise ProviderEngineValidationError("Managed engine returned invalid provider method billing lanes")
        if not set(billing_lanes).issubset(family_lanes):
            raise ProviderEngineValidationError("Managed engine provider method exceeds its family billing lanes")
        if method_type == "oauth" and acquisition_method not in {"browser", "device"}:
            raise ProviderEngineValidationError("Managed engine returned an invalid OAuth acquisition method")
        method["acquisitionMethod"] = acquisition_method
        method["credentialKinds"] = list(credential_kinds)
        method["billingLanes"] = list(billing_lanes)
        if method_type == "api" and acquisition_method != "manual-key":
            raise ProviderEngineValidationError("Managed engine returned an invalid API-key acquisition method")
        raw_prompts = raw_method.get("prompts")
        if raw_prompts is not None:
            if not isinstance(raw_prompts, list) or len(raw_prompts) > 32:
                raise ProviderEngineValidationError(
                    "Managed engine returned invalid provider sign-in prompts"
                )
            prompts: list[dict[str, Any]] = []
            prompt_keys: set[str] = set()
            for raw_prompt in raw_prompts:
                if not isinstance(raw_prompt, Mapping):
                    raise ProviderEngineValidationError(
                        "Managed engine returned an invalid provider sign-in prompt"
                    )
                prompt_type = _provider_catalog_text(
                    raw_prompt.get("type"), "provider prompt type", maximum=16
                )
                key = _provider_catalog_text(
                    raw_prompt.get("key"), "provider prompt key", maximum=128
                )
                message = _provider_catalog_text(
                    raw_prompt.get("message"), "provider prompt message", maximum=256
                )
                if (
                    prompt_type not in {"text", "select"}
                    or key.startswith("__openclank_")
                    or re.fullmatch(_IDENTIFIER, key) is None
                    or key in prompt_keys
                ):
                    raise ProviderEngineValidationError(
                        "Managed engine returned an invalid provider sign-in prompt"
                    )
                prompt_keys.add(key)
                prompt: dict[str, Any] = {
                    "type": prompt_type,
                    "key": key,
                    "message": message,
                }
                if raw_prompt.get("placeholder") is not None:
                    prompt["placeholder"] = _provider_catalog_text(
                        raw_prompt.get("placeholder"),
                        "provider prompt placeholder",
                        maximum=256,
                        allow_empty=True,
                    )
                if prompt_type == "select":
                    raw_options = raw_prompt.get("options")
                    if not isinstance(raw_options, list) or not raw_options or len(raw_options) > 128:
                        raise ProviderEngineValidationError(
                            "Managed engine returned invalid provider prompt options"
                        )
                    prompt["options"] = [
                        {
                            "label": _provider_catalog_text(
                                option.get("label") if isinstance(option, Mapping) else None,
                                "provider prompt option label",
                                maximum=160,
                            ),
                            "value": _provider_catalog_text(
                                option.get("value") if isinstance(option, Mapping) else None,
                                "provider prompt option value",
                                maximum=256,
                            ),
                            **(
                                {
                                    "hint": _provider_catalog_text(
                                        option.get("hint"),
                                        "provider prompt option hint",
                                        maximum=256,
                                        allow_empty=True,
                                    )
                                }
                                if isinstance(option, Mapping) and option.get("hint") is not None
                                else {}
                            ),
                        }
                        for option in raw_options
                    ]
                    option_values = [item["value"] for item in prompt["options"]]
                    if len(set(option_values)) != len(option_values):
                        raise ProviderEngineValidationError(
                            "Managed engine returned duplicate provider prompt options"
                        )
                raw_when = raw_prompt.get("when")
                if raw_when is not None:
                    if not isinstance(raw_when, Mapping):
                        raise ProviderEngineValidationError(
                            "Managed engine returned an invalid provider prompt condition"
                        )
                    condition = {
                        "key": _provider_catalog_text(
                            raw_when.get("key"), "provider prompt condition key", maximum=128
                        ),
                        "op": _provider_catalog_text(
                            raw_when.get("op"), "provider prompt condition operation", maximum=8
                        ),
                        "value": _provider_catalog_text(
                            raw_when.get("value"),
                            "provider prompt condition value",
                            maximum=256,
                            allow_empty=True,
                        ),
                    }
                    if condition["op"] not in {"eq", "neq"}:
                        raise ProviderEngineValidationError(
                            "Managed engine returned an invalid provider prompt condition"
                        )
                    prompt["when"] = condition
                prompts.append(prompt)
            for prompt in prompts:
                condition = prompt.get("when")
                if condition is not None and condition["key"] not in prompt_keys:
                    raise ProviderEngineValidationError(
                        "Managed engine returned an unknown provider prompt condition"
                    )
            method["prompts"] = prompts
        result.append(method)
    return result


def _begin_idempotent(
    store: ProviderStore,
    *,
    owner: str,
    operation: str,
    key_header: Optional[str],
    payload: Mapping[str, Any],
) -> tuple[str, str, Optional[IdempotencyResult]]:
    key = _idempotency_key(key_header)
    digest = store.idempotency_request_digest(
        owner=owner,
        operation=operation,
        payload=payload,
    )
    replay = store.lookup_idempotency(
        owner=owner,
        operation=operation,
        idempotency_key=key,
        request_digest=digest,
    )
    return key, digest, replay


def _mutation_response(
    result: IdempotencyResult,
    *,
    replayed: bool,
) -> JSONResponse:
    body = dict(result.response_body)
    headers = {"Idempotency-Replayed": "true" if replayed else "false"}
    revision = body.get("revision")
    if isinstance(revision, int):
        headers["ETag"] = _etag(revision)
    return JSONResponse(
        status_code=int(result.status_code),
        content=body,
        headers=headers,
    )


def _record_response(
    store: ProviderStore,
    *,
    owner: str,
    operation: str,
    key: str,
    digest: str,
    status_code: int,
    body: Mapping[str, Any],
    resource_id: Optional[str] = None,
) -> JSONResponse:
    result = store.record_idempotency(
        owner=owner,
        operation=operation,
        idempotency_key=key,
        request_digest=digest,
        status_code=status_code,
        response_body=body,
        resource_id=resource_id,
    )
    return _mutation_response(result, replayed=False)


def _known_recipient(request: Request, recipient: str) -> None:
    manager = getattr(getattr(request.app, "state", None), "auth_manager", None)
    if manager is None or not getattr(manager, "is_configured", False):
        return
    list_users = getattr(manager, "list_users", None)
    if not callable(list_users):
        raise HTTPException(503, "Recipient directory is unavailable")
    users = {
        str(item.get("username") or item.get("name") or "").strip().lower()
        for item in (list_users() or ())
        if isinstance(item, Mapping)
    }
    if recipient.strip().lower() not in users:
        raise HTTPException(422, "Provider share recipient does not exist")


def _share_recipient_usernames(request: Request, *, owner: str) -> list[str]:
    """Return only normalized usernames suitable for the sharing chooser."""

    manager = getattr(getattr(request.app, "state", None), "auth_manager", None)
    if manager is None or not getattr(manager, "is_configured", False):
        return []
    list_users = getattr(manager, "list_users", None)
    if not callable(list_users):
        raise HTTPException(503, "Recipient directory is unavailable")
    usernames = {
        str(item.get("username") or item.get("name") or "").strip().lower()
        for item in (list_users() or ())
        if isinstance(item, Mapping)
    }
    return sorted(
        username
        for username in usernames
        if username and username != owner and len(username) <= 128
    )


def _connection_validation_wire(
    *,
    family_id: str,
    adapter_id: str,
    kind: str,
    billing_lane: str,
    url: Optional[str],
    settings: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "familyID": family_id,
        "adapterID": adapter_id,
        "kind": kind,
        "billingLane": billing_lane,
        "url": url,
        "settings": dict(settings),
    }


def _account_validation_wire(
    *,
    connection: Any,
    auth_method: str,
    credential: Mapping[str, Any],
    account_id: str,
    credential_revision: int,
    acquisition_method: Optional[str] = None,
    credential_kind: Optional[str] = None,
) -> dict[str, Any]:
    """Build the host-bound v2 account validation request."""

    return {
        "connection": _connection_validation_wire(
            family_id=connection.family_id,
            adapter_id=connection.adapter_id,
            kind=connection.kind,
            billing_lane=connection.billing_lane,
            url=connection.normalized_url,
            settings=dict(connection.settings or {}),
        ),
        "authMethod": auth_method,
        **({"acquisitionMethod": acquisition_method} if acquisition_method is not None else {}),
        "credentialKind": credential_kind or ("oauth" if auth_method == "oauth" else "api"),
        "credential": dict(credential),
        "accountID": str(account_id),
        "credentialRevision": int(credential_revision),
    }


def _validated_account_discovery(
    result: Mapping[str, Any],
    *,
    account_id: str,
    credential_revision: int,
) -> Mapping[str, Any]:
    """Require the account/revision/discovery identity echo before persistence."""

    expected_account = str(account_id)
    expected_revision = int(credential_revision)
    discovery = result.get("discovery")
    if (
        result.get("accountID") != expected_account
        or result.get("credentialRevision") != expected_revision
        or not isinstance(discovery, Mapping)
        or discovery.get("accountID") != expected_account
        or discovery.get("credentialRevision") != expected_revision
        or result.get("modelRoutes") != discovery.get("models")
    ):
        raise HTTPException(502, "Managed provider account validation changed its binding")
    status = str(discovery.get("status") or "unknown")
    if status not in {"complete", "unavailable", "partial", "reauth_required"}:
        raise HTTPException(502, "Managed provider account discovery is unavailable")
    if status == "complete" and discovery.get("authoritative") is not True:
        raise HTTPException(502, "Managed provider account discovery is not authoritative")
    return discovery


def _validate_account_auth_result(
    result: Mapping[str, Any],
    *,
    connection: Any,
    expected_acquisition_method: Optional[str] = None,
    expected_credential_kind: Optional[str] = None,
) -> None:
    credential = result.get("credential")
    credential_kind = credential.get("type") if isinstance(credential, Mapping) else None
    auth_method = "api_key" if credential_kind == "api" else "oauth" if credential_kind == "oauth" else None
    if (
        auth_method is None
        or result.get("authMethod") != auth_method
        or result.get("credentialKind") != credential_kind
        or result.get("acquisitionMethod") not in {"manual-key", "browser", "device", None}
    ):
        raise HTTPException(502, "Managed provider account validation changed its auth method")
    if expected_acquisition_method is not None and result.get("acquisitionMethod") != expected_acquisition_method:
        raise HTTPException(502, "Managed provider account acquisition method changed")
    if expected_credential_kind is not None and credential_kind != expected_credential_kind:
        raise HTTPException(502, "Managed provider credential kind changed")
    endpoint_policies = PROVIDER_ENDPOINT_POLICIES.get(connection.family_id)
    expected_endpoint_policy = None
    if endpoint_policies is not None:
        family = next((item for item in bundled_provider_family_catalog()["families"]
                       if item["id"] == connection.family_id), None)
        if (family is None or connection.adapter_id not in family["adapters"]
                or connection.billing_lane not in endpoint_policies):
            raise HTTPException(502, "Managed provider endpoint policy does not match its billing lane")
        expected_endpoint_policy = endpoint_policies[connection.billing_lane]
    if expected_endpoint_policy is not None:
        expected_credential_kinds = {"oauth"} if connection.family_id == "grok" else {"api"}
        if connection.family_id in {"kimi-code-plan-cn", "kimi-code-plan-global"}:
            expected_credential_kinds = {"oauth", "api"}
        if result.get("endpointPolicy") != expected_endpoint_policy or credential_kind not in expected_credential_kinds:
            raise HTTPException(502, "Managed provider endpoint policy does not match its billing lane")
    elif result.get("endpointPolicy") is not None:
        raise HTTPException(502, "Managed provider returned an unsupported endpoint policy")


def _discovery_error_code(discovery: Mapping[str, Any]) -> Optional[str]:
    value = str(discovery.get("errorCode") or "").strip().lower()
    return value if value in {"discovery_unavailable", "discovery_partial", "reauth_required"} else None


def _inventory_cache_key(
    *,
    owner: str,
    connection: Any,
    account: Any,
) -> tuple[str, str, str, int, str, str]:
    return (
        str(owner).strip().lower(),
        str(connection.id),
        str(account.id),
        int(account.credential_version),
        str(connection.family_id),
        str(connection.adapter_id),
    )


def _invalidate_inventory_cache(*, owner: str, connection_id: Optional[str] = None) -> None:
    normalized = str(owner).strip().lower()
    flights: list[asyncio.Task] = []
    with _ACCOUNT_INVENTORY_LOCK:
        for key in list(_ACCOUNT_INVENTORY_CACHE):
            if key[0] == normalized and (connection_id is None or key[1] == connection_id):
                _ACCOUNT_INVENTORY_CACHE.pop(key, None)
        for key, task in list(_ACCOUNT_INVENTORY_FLIGHTS.items()):
            if key[0] == normalized and (connection_id is None or key[1] == connection_id):
                _ACCOUNT_INVENTORY_FLIGHTS.pop(key, None)
                flights.append(task)
    for task in flights:
        try:
            task.get_loop().call_soon_threadsafe(task.cancel)
        except RuntimeError:
            pass


def _engine_model_routes(
    *,
    store: ProviderStore,
    owner: str,
    operation: str,
    idempotency_key: str,
    raw_routes: Any,
) -> list[dict[str, Any]]:
    if not isinstance(raw_routes, list) or len(raw_routes) > 4_096:
        raise ProviderEngineValidationError(
            "Managed engine returned invalid model routes"
        )
    allowed_operations = set().union(*_PURPOSE_OPERATIONS.values())
    declared: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in raw_routes:
        if not isinstance(item, Mapping):
            raise ProviderEngineValidationError(
                "Managed engine returned an invalid model route"
            )
        model_id = str(item.get("modelID") or "").strip()
        display_name = str(item.get("displayName") or model_id).strip()
        operations = item.get("operations")
        capabilities = item.get("capabilities") or {}
        provenance = item.get("provenance") or {}
        if (
            not model_id
            or model_id in seen
            or len(model_id) > 256
            or "\x00" in model_id
            or any(character.isspace() for character in model_id)
            or not display_name
            or len(display_name) > 512
            or not isinstance(operations, list)
            or not operations
            or any(value not in allowed_operations for value in operations)
            or not isinstance(capabilities, Mapping)
            or not isinstance(provenance, Mapping)
        ):
            raise ProviderEngineValidationError(
                "Managed engine returned an invalid model route"
            )
        seen.add(model_id)
        _assert_nonsecret_settings(capabilities)
        _assert_nonsecret_settings(provenance)
        declared.append(
            {
                "id": store.deterministic_resource_id(
                    prefix="pmr",
                    owner=owner,
                    operation=f"{operation}:model:{model_id}",
                    idempotency_key=idempotency_key,
                ),
                "provider_model_id": model_id,
                "display_name": display_name,
                "operations": operations,
                "capabilities": dict(capabilities),
                "provenance": dict(provenance),
            }
        )
    return declared


def _raise_engine_error(exc: Exception) -> None:
    if isinstance(exc, HTTPException):
        raise exc
    if isinstance(exc, ProviderOAuthFlowError):
        raise HTTPException(404, "Provider OAuth flow was not found") from exc
    if isinstance(exc, ProviderEngineValidationError):
        raise HTTPException(422, str(exc)) from exc
    if isinstance(exc, ProviderEngineControlError):
        raise HTTPException(503, str(exc)) from exc
    raise HTTPException(502, "Managed provider engine operation failed") from exc


def _provider_public_origin(request: Request) -> str:
    configured = (
        os.getenv("OAUTH_REDIRECT_BASE_URL", "").strip()
        or os.getenv("APP_PUBLIC_URL", "").strip()
    )
    if configured:
        parsed = urlsplit(configured.rstrip("/"))
    else:
        request_url = urlsplit(str(request.url))
        if request_url.hostname not in {"localhost", "127.0.0.1", "::1", "testserver"}:
            raise HTTPException(
                400,
                "Set APP_PUBLIC_URL before using remote provider login",
            )
        # The callback base is an origin.  The current request necessarily has
        # an API path, which must not be mistaken for a configured base path.
        parsed = urlsplit(f"{request_url.scheme}://{request_url.netloc}")
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        raise HTTPException(400, "Invalid configured Open Clank public URL")
    if parsed.scheme != "https" and parsed.hostname not in {
        "localhost",
        "127.0.0.1",
        "::1",
        "testserver",
    }:
        raise HTTPException(400, "Remote provider login requires HTTPS")
    return f"{parsed.scheme}://{parsed.netloc}"


def _safe_authorization_url(value: Any) -> str:
    url = str(value or "")
    if len(url) > 8_192:
        raise HTTPException(502, "Provider authorization URL is invalid")
    parsed = urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise HTTPException(502, "Provider authorization URL is unsafe")
    return url


def _oauth_page(success: bool, message: str, *, status_code: int = 200) -> HTMLResponse:
    title = "Authorization received" if success else "Provider login failed"
    nonce = secrets.token_urlsafe(16)
    close_script = f'<script nonce="{nonce}">setTimeout(() => window.close(), 2500)</script>' if success else ""
    safe_title = html.escape(title)
    safe_message = html.escape(str(message or "")[:512])
    color = "#5bd89a" if success else "#ff6b6b"
    return HTMLResponse(
        f"""<!doctype html><html><head><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{safe_title}</title></head><body style="margin:0;min-height:100vh;display:grid;place-items:center;background:#181a20;color:#f5f1e8;font-family:system-ui,sans-serif">
<main style="max-width:32rem;padding:2rem;text-align:center"><h1 style="color:{color}">{safe_title}</h1><p>{safe_message}</p><p>You can close this tab and return to Open Clank.</p></main>{close_script}</body></html>""",
        status_code=status_code,
        headers={
            "Cache-Control": "no-store",
            "Pragma": "no-cache",
            "Referrer-Policy": "no-referrer",
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": f"default-src 'none'; style-src 'unsafe-inline'; script-src 'nonce-{nonce}'",
        },
    )


_PROVIDER_OAUTH_FLOWS = OAuthHostFlowStore()


async def _acquire_global_oauth_lock(flow_id: str) -> threading.Lock:
    with _OAUTH_COMPLETION_LOCKS_GUARD:
        lock = _OAUTH_COMPLETION_LOCKS.setdefault(flow_id, threading.Lock())
        _OAUTH_COMPLETION_LOCK_REFS[flow_id] = _OAUTH_COMPLETION_LOCK_REFS.get(flow_id, 0) + 1
    try:
        while not lock.acquire(blocking=False):
            await asyncio.sleep(0.01)
        return lock
    except asyncio.CancelledError:
        with _OAUTH_COMPLETION_LOCKS_GUARD:
            _OAUTH_COMPLETION_LOCK_REFS[flow_id] = max(0, _OAUTH_COMPLETION_LOCK_REFS.get(flow_id, 1) - 1)
        raise


def _release_global_oauth_lock(flow_id: str, lock: threading.Lock) -> None:
    lock.release()
    with _OAUTH_COMPLETION_LOCKS_GUARD:
        refs = _OAUTH_COMPLETION_LOCK_REFS.get(flow_id, 0) - 1
        if refs <= 0 and not lock.locked():
            _OAUTH_COMPLETION_LOCK_REFS.pop(flow_id, None)
            if _OAUTH_COMPLETION_LOCKS.get(flow_id) is lock:
                _OAUTH_COMPLETION_LOCKS.pop(flow_id, None)
        else:
            _OAUTH_COMPLETION_LOCK_REFS[flow_id] = refs


async def purge_owner_provider_flows(_supervisor_pool, owner: str) -> None:
    """Cancel owner-bound managed OAuth flows before rename or deletion."""
    flows = _PROVIDER_OAUTH_FLOWS.remove_owner(owner)
    first_error: Exception | None = None
    for flow in flows:
        flow.cancel_requested = True
        lock = await _acquire_global_oauth_lock(flow.flow_id)
        try:
            if flow.status == "complete":
                continue
            if flow.status not in {"failed", "cancelled", "expired"}:
                await asyncio.wait_for(
                    flow.engine.call(
                        "_openclank/provider-control/v1/oauth/cancel",
                        flow.completion_payload(),
                    ),
                    timeout=_OAUTH_CANCEL_TIMEOUT_SECONDS,
                )
        except Exception as exc:
            first_error = first_error or exc
        finally:
            if flow.status != "complete":
                flow.status = "cancelled"
                flow.error_code = "oauth_cancelled"
            if flow.status != "complete" and not flow.released:
                flow.released = True
                if flow.expiry_task is not None and flow.expiry_task is not asyncio.current_task():
                    flow.expiry_task.cancel()
                try:
                    await flow.engine.release(successful=False)
                except Exception as exc:
                    first_error = first_error or exc
            _release_global_oauth_lock(flow.flow_id, lock)
    if first_error is not None:
        raise first_error


async def shutdown_provider_oauth_flows() -> None:
    owners = {flow.owner for flow in _PROVIDER_OAUTH_FLOWS.snapshot()}
    first_error: Exception | None = None
    for owner in owners:
        try:
            await purge_owner_provider_flows(None, owner)
        except Exception as exc:
            first_error = first_error or exc
    if first_error is not None:
        raise first_error


def setup_provider_v1_routes(
    provider_store: Optional[ProviderStore] = None,
    engine_control: Optional[Any] = None,
) -> APIRouter:
    store = provider_store or ProviderStore()
    control = engine_control or ManagedProviderEngineControl()
    oauth_flows = _PROVIDER_OAUTH_FLOWS
    router = APIRouter(
        prefix="/api/v1/providers",
        tags=["providers-v1"],
        route_class=_SecretSafeRoute,
    )

    def _comfy_available(request: Request) -> bool:
        state = request.app.state
        return (callable(getattr(state, "media_human_service_factory", None))
                and getattr(state, "provider_store", None) is store
                and all(getattr(state, key, None) is not None for key in
                        ("auth_manager", "account_request_barrier", "session_manager")))

    def _require_comfy(request: Request, family_id: str) -> None:
        if family_id == 'tripo' and (not callable(getattr(request.app.state, 'tripo_human_service_factory', None))
                or getattr(request.app.state, 'provider_store', None) is not store
                or not all(getattr(request.app.state, key, None) is not None for key in ('auth_manager','account_request_barrier','session_manager','media_artifact_store'))):
            raise HTTPException(503, 'Tripo jobs unavailable')
        if family_id == 'meshy' and (not callable(getattr(request.app.state, 'meshy_human_service_factory', None))
                or getattr(request.app.state, 'provider_store', None) is not store
                or not all(getattr(request.app.state, key, None) is not None for key in ('auth_manager','account_request_barrier','session_manager','media_artifact_store'))):
            raise HTTPException(503, 'Meshy jobs unavailable')
        if family_id == 'sprixen' and (not callable(getattr(request.app.state, 'sprixen_human_service_factory', None))
                or getattr(request.app.state, 'provider_store', None) is not store
                or not all(getattr(request.app.state, key, None) is not None for key in ('auth_manager','account_request_barrier','session_manager','media_artifact_store'))):
            raise HTTPException(503, 'Sprixen jobs unavailable')
        if family_id == "comfyui" and not _comfy_available(request):
            raise HTTPException(503, "Media jobs unavailable")
        if family_id in {"blender", "trellis", "hunyuan-shape"}:
            from core.middleware import require_admin
            from src.auth_helpers import require_user, is_internal_tool_identity
            require_admin(request)
            owner = require_user(request)
            if is_internal_tool_identity(owner) or getattr(request.state, 'internal_tool_authenticated', False):
                raise HTTPException(403, "A named administrator is required")
            if (not callable(getattr(request.app.state, 'trellis_human_service_factory' if family_id == 'trellis' else 'hunyuan_human_service_factory' if family_id == 'hunyuan-shape' else 'blender_human_service_factory', None))
                    or getattr(request.app.state, 'provider_store', None) is not store
                    or not all(getattr(request.app.state, key, None) is not None for key in ('auth_manager','account_request_barrier','session_manager','media_artifact_store'))):
                raise HTTPException(503, "Local media jobs unavailable")

    def _oauth_owner(request: Request, *, write: bool = False) -> str:
        if getattr(getattr(request, "state", None), "api_token", False):
            raise HTTPException(403, "Provider OAuth requires a browser session")
        return _provider_owner(request, write=write)

    def _oauth_inputs(value: Mapping[str, str]) -> dict[str, str]:
        if len(value) > 32:
            raise HTTPException(422, "Too many provider OAuth prompt values")
        result: dict[str, str] = {}
        for key, item in value.items():
            name = str(key)
            text = str(item)
            if (
                not name
                or len(name) > 128
                or name.startswith("__openclank_")
                or len(text) > 4096
            ):
                raise HTTPException(422, "Provider OAuth prompt values are invalid")
            result[name] = text
        return result

    async def _release_terminal_flow(
        flow: OAuthHostFlow,
        *,
        successful: bool,
        remove: bool = False,
    ) -> None:
        if remove:
            oauth_flows.remove(flow.flow_id)
        if flow.released:
            return
        flow.released = True
        expiry_task = flow.expiry_task
        if expiry_task is not None and expiry_task is not asyncio.current_task():
            expiry_task.cancel()
        if not successful:
            try:
                await asyncio.wait_for(
                    flow.engine.call(
                        "_openclank/provider-control/v1/oauth/cancel",
                        flow.completion_payload(),
                    ),
                    timeout=_OAUTH_CANCEL_TIMEOUT_SECONDS,
                )
            except Exception:
                pass
            flow.cancel_requested = True
        try:
            await flow.engine.release(successful=successful)
        except Exception:
            pass
        if flow.retention_task is None:
            flow.retention_task = asyncio.create_task(_retain_terminal_flow(flow))

    def _release_flow_lock(flow_id: str, lock: threading.Lock) -> None:
        lock.release()
        with _OAUTH_COMPLETION_LOCKS_GUARD:
            refs = _OAUTH_COMPLETION_LOCK_REFS.get(flow_id, 0) - 1
            if refs <= 0 and not lock.locked():
                _OAUTH_COMPLETION_LOCK_REFS.pop(flow_id, None)
                if _OAUTH_COMPLETION_LOCKS.get(flow_id) is lock:
                    _OAUTH_COMPLETION_LOCKS.pop(flow_id, None)
            else:
                _OAUTH_COMPLETION_LOCK_REFS[flow_id] = refs

    async def _acquire_flow_lock(flow_id: str) -> threading.Lock:
        with _OAUTH_COMPLETION_LOCKS_GUARD:
            lock = _OAUTH_COMPLETION_LOCKS.setdefault(flow_id, threading.Lock())
            _OAUTH_COMPLETION_LOCK_REFS[flow_id] = _OAUTH_COMPLETION_LOCK_REFS.get(flow_id, 0) + 1
        try:
            while not lock.acquire(blocking=False):
                await asyncio.sleep(0.01)
            return lock
        except asyncio.CancelledError:
            with _OAUTH_COMPLETION_LOCKS_GUARD:
                refs = _OAUTH_COMPLETION_LOCK_REFS.get(flow_id, 1) - 1
                if refs <= 0 and not lock.locked():
                    _OAUTH_COMPLETION_LOCK_REFS.pop(flow_id, None)
                    if _OAUTH_COMPLETION_LOCKS.get(flow_id) is lock:
                        _OAUTH_COMPLETION_LOCKS.pop(flow_id, None)
                else:
                    _OAUTH_COMPLETION_LOCK_REFS[flow_id] = refs
            raise

    def _assert_claude_flow_policy(flow: OAuthHostFlow) -> None:
        if flow.provider_id != "anthropic" or flow.billing_lane != "subscription":
            return
        from src.openclank.provider_method_policy import require_enabled, ProviderMethodPolicyError
        if flow.method_policy_revision is None:
            raise HTTPException(409, "Claude subscription Beta policy proof is missing")
        try:
            require_enabled(flow.owner, flow.method_policy_revision)
        except ProviderMethodPolicyError as exc:
            raise HTTPException(409, {"code":exc.code, "message":str(exc)}) from exc

    async def _flow_call(
        flow: OAuthHostFlow,
        method: str,
        payload: Mapping[str, Any],
        timeout_seconds: float,
    ) -> dict[str, Any]:
        if method != "_openclank/provider-control/v1/oauth/cancel":
            _assert_claude_flow_policy(flow)
        try:
            return await asyncio.wait_for(
                flow.engine.call(method, payload), timeout=timeout_seconds
            )
        except asyncio.TimeoutError as exc:
            raise ProviderEngineTimeoutError("managed provider operation timed out") from exc

    def _safe_oauth_error(exc: Exception) -> str:
        if isinstance(exc, ProviderEngineValidationError):
            return "oauth_validation_failed"
        if isinstance(exc, HTTPException) and exc.status_code == 502:
            return "oauth_validation_failed"
        if isinstance(exc, ProviderEngineTimeoutError):
            return "oauth_timeout"
        if isinstance(exc, asyncio.TimeoutError):
            return "oauth_timeout"
        if isinstance(exc, ProviderEngineControlError):
            return "oauth_worker_lost"
        return "oauth_completion_failed"

    def _result_error_code(result: Mapping[str, Any]) -> str:
        value = str(result.get("errorCode") or "").strip().lower()
        return value if value in _OAUTH_SAFE_ERROR_CODES else "oauth_exchange_failed"

    def _result_retry_after_ms(result: Mapping[str, Any]) -> Optional[int]:
        value = result.get("retryAfterMs")
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        if not 0 <= value <= 86_400_000 or int(value) != value:
            return None
        return int(value)

    def _transient_provider_error(exc: Exception) -> bool:
        return isinstance(exc, ProviderEngineTimeoutError)

    def _schedule_transient_retry(flow: OAuthHostFlow, retry_after_ms: Optional[int] = None) -> bool:
        now = datetime.now(timezone.utc)
        if flow.transient_started_at is None:
            flow.transient_started_at = now
        elapsed = (now - flow.transient_started_at).total_seconds()
        if flow.transient_failures >= 2 or elapsed >= 30 or flow.expired():
            return False
        flow.transient_failures += 1
        remaining = min(30 - elapsed, max(0.0, (flow.expires_at - now).total_seconds()))
        delay = (
            retry_after_ms / 1000
            if retry_after_ms is not None
            else 1.0 * (2 ** (flow.transient_failures - 1))
        )
        if delay > remaining:
            return False
        flow.next_attempt_at = now + timedelta(seconds=max(0.0, delay))
        flow.status = "running"
        flow.retry_after_ms = int(delay * 1000)
        return True

    async def _fail_flow_locked(flow: OAuthHostFlow, exc: Exception) -> dict[str, Any]:
        if flow.status not in {"complete", "failed", "cancelled", "expired"}:
            flow.status = "failed"
            flow.error_code = _safe_oauth_error(exc)
            flow.retry_after_ms = None
            await _release_terminal_flow(flow, successful=False)
        return flow.public()

    async def _fail_flow(flow: OAuthHostFlow, exc: Exception) -> dict[str, Any]:
        lock = await _acquire_flow_lock(flow.flow_id)
        try:
            return await _fail_flow_locked(flow, exc)
        finally:
            _release_flow_lock(flow.flow_id, lock)

    async def _expire_oauth_flows() -> None:
        for flow in oauth_flows.expired():
            flow.cancel_requested = True
            lock = await _acquire_flow_lock(flow.flow_id)
            acquired = True
            try:
                if flow.status in {"complete", "failed", "cancelled", "expired"}:
                    continue
                flow.status = "expired"
                flow.error_code = "oauth_expired"
                flow.retry_after_ms = None
                try:
                    await _flow_call(
                        flow,
                        "_openclank/provider-control/v1/oauth/cancel",
                        flow.completion_payload(),
                        _OAUTH_CANCEL_TIMEOUT_SECONDS,
                    )
                except Exception:
                    pass
                await _release_terminal_flow(flow, successful=False)
            finally:
                if acquired:
                    _release_flow_lock(flow.flow_id, lock)

    async def _flow_deadline(flow: OAuthHostFlow) -> None:
        delay = max(
            0.0,
            (flow.expires_at - datetime.now(timezone.utc)).total_seconds(),
        )
        try:
            await asyncio.sleep(delay)
            await _expire_oauth_flows()
        except asyncio.CancelledError:
            return

    async def _retain_terminal_flow(flow: OAuthHostFlow) -> None:
        try:
            await asyncio.sleep(_OAUTH_FLOW_RETENTION_SECONDS)
            oauth_flows.remove(flow.flow_id)
            with _OAUTH_COMPLETION_LOCKS_GUARD:
                if _OAUTH_COMPLETION_LOCK_REFS.get(flow.flow_id, 0) == 0:
                    _OAUTH_COMPLETION_LOCKS.pop(flow.flow_id, None)
        except asyncio.CancelledError:
            return

    async def _shutdown_oauth_flows() -> None:
        for flow in oauth_flows.snapshot():
            if flow.status == "complete":
                continue
            lock = await _acquire_flow_lock(flow.flow_id)
            try:
                flow.cancel_requested = True
                if flow.status not in {"failed", "cancelled", "expired"}:
                    try:
                        await _flow_call(
                            flow,
                            "_openclank/provider-control/v1/oauth/cancel",
                            flow.completion_payload(),
                            _OAUTH_CANCEL_TIMEOUT_SECONDS,
                        )
                    except Exception:
                        pass
                    flow.status = "cancelled"
                    flow.error_code = "oauth_cancelled"
                await _release_terminal_flow(flow, successful=False)
            finally:
                _release_flow_lock(flow.flow_id, lock)

    router.add_event_handler("shutdown", _shutdown_oauth_flows)

    def _validate_engine_flow_result(
        flow: OAuthHostFlow,
        result: Mapping[str, Any],
    ) -> None:
        expected = {
            "flowID": flow.flow_id,
            "connectionID": flow.connection_id,
            "providerID": flow.provider_id,
            "billingLane": flow.billing_lane,
            "mode": flow.mode,
        }
        if any(result.get(key) != value for key, value in expected.items()):
            raise HTTPException(502, "Managed provider OAuth flow escaped its binding")
        if result.get("targetAccountID") != flow.target_account_id:
            raise HTTPException(502, "Managed provider OAuth target changed")
        if result.get("expectedRevision") != flow.expected_revision:
            raise HTTPException(502, "Managed provider OAuth revision changed")

    async def _persist_oauth_result(
        flow: OAuthHostFlow,
        result: Mapping[str, Any],
    ) -> dict[str, Any]:
        lock = await _acquire_flow_lock(flow.flow_id)
        acquired = True
        try:
            if flow.status in {"complete", "failed", "cancelled", "expired"}:
                return flow.public()
            if flow.next_attempt_at is not None:
                now = datetime.now(timezone.utc)
                if now < flow.next_attempt_at:
                    flow.retry_after_ms = max(
                        0, int((flow.next_attempt_at - now).total_seconds() * 1000)
                    )
                    return flow.public()
                flow.next_attempt_at = None
                flow.retry_after_ms = None
            return await _persist_oauth_result_locked(flow, result)
        except Exception as exc:
            if _transient_provider_error(exc) and _schedule_transient_retry(flow):
                return flow.public()
            raise
        finally:
            if acquired:
                _release_flow_lock(flow.flow_id, lock)

    async def _complete_oauth_callback(
        flow: OAuthHostFlow,
        *,
        code: Optional[str],
    ) -> dict[str, Any]:
        """Serialize callback exchange, validation, persistence, and release."""

        lock = await _acquire_flow_lock(flow.flow_id)
        acquired = True
        try:
            if flow.status in {"complete", "failed", "cancelled", "expired"}:
                return flow.public()
            result = await _flow_call(
                flow,
                "_openclank/provider-control/v1/oauth/callback",
                flow.completion_payload(code=code),
                _OAUTH_CALLBACK_TIMEOUT_SECONDS,
            )
            return await _persist_oauth_result_locked(flow, result)
        except (ProviderEngineControlError, ProviderEngineValidationError, HTTPException) as exc:
            return await _fail_flow_locked(flow, exc)
        except Exception as exc:
            return await _fail_flow_locked(flow, exc)
        finally:
            if acquired:
                _release_flow_lock(flow.flow_id, lock)

    async def _persist_oauth_result_locked(
        flow: OAuthHostFlow,
        result: Mapping[str, Any],
    ) -> dict[str, Any]:
        if flow.status in {"complete", "failed", "cancelled", "expired"}:
            return flow.public()
        if flow.next_attempt_at is not None:
            now = datetime.now(timezone.utc)
            if now < flow.next_attempt_at:
                flow.retry_after_ms = max(0, int((flow.next_attempt_at - now).total_seconds() * 1000))
                return flow.public()
            flow.next_attempt_at = None
            flow.retry_after_ms = None
        now = datetime.now(timezone.utc)
        if flow.expired() or (
            flow.transient_started_at is not None
            and (now - flow.transient_started_at).total_seconds() >= 30
        ):
            flow.status = "expired" if flow.expired() else "failed"
            flow.error_code = "oauth_expired" if flow.expired() else "oauth_discovery_unavailable"
            await _release_terminal_flow(flow, successful=False)
            return flow.public()
        _validate_engine_flow_result(flow, result)
        if flow.cancel_requested or flow.expired():
            flow.status = "expired" if flow.expired() else "cancelled"
            flow.error_code = "oauth_expired" if flow.status == "expired" else "oauth_cancelled"
            await _release_terminal_flow(flow, successful=False)
            return flow.public()
        status = str(result.get("status") or "failed")
        if status not in {"pending", "running", "complete", "failed", "cancelled", "expired"}:
            flow.status = "failed"
            flow.error_code = "oauth_validation_failed"
            await _release_terminal_flow(flow, successful=False)
            return flow.public()
        if status in {"pending", "running"}:
            flow.status = status
            flow.retry_after_ms = _result_retry_after_ms(result)
            return flow.public()
        if status != "complete":
            flow.status = status
            flow.error_code = _result_error_code(result)
            flow.retry_after_ms = None
            await _release_terminal_flow(flow, successful=False)
            return flow.public()

        credential = result.get("credential")
        if not isinstance(credential, Mapping):
            raise HTTPException(502, "Managed provider OAuth credential is missing")
        credential_kind = str(credential.get("type") or "")
        expected_auth_method = "api_key" if credential_kind == "api" else "oauth" if credential_kind == "oauth" else ""
        if (
            credential_kind not in flow.expected_credential_kinds
            or result.get("credentialKind") != credential_kind
            or result.get("authMethod") != expected_auth_method
            or result.get("acquisitionMethod") != flow.acquisition_method
        ):
            raise HTTPException(502, "Managed provider OAuth result disagrees with its selected auth method")
        try:
            connection = store.get_connection(
                owner=flow.owner,
                connection_id=flow.connection_id,
            )
            if flow.mode == "add":
                operation = f"oauth.complete:add:{flow.connection_id}"
                host_account_id = store.deterministic_resource_id(
                    prefix="pac",
                    owner=flow.owner,
                    operation=operation,
                    idempotency_key=flow.flow_id,
                )
                expected_credential_revision = 1
            else:
                if not flow.target_account_id or flow.expected_revision is None:
                    raise HTTPException(502, "Managed provider OAuth target is incomplete")
                current = store.get_account(
                    owner=flow.owner,
                    account_id=flow.target_account_id,
                )
                if current.connection_id != flow.connection_id or current.auth_method not in {"oauth", "api_key"}:
                    raise HTTPException(409, "Provider OAuth target is no longer reconnectable")
                if current.auth_method == "api_key" and not (
                    connection.family_id == "xiaomi" and connection.adapter_id == "mimo-native"
                ):
                    raise HTTPException(409, "Provider account is no longer reconnectable")
                host_account_id = current.id
                expected_credential_revision = int(current.credential_version) + 1

            recovery_remaining = 30.0
            if flow.transient_started_at is not None:
                recovery_remaining = max(
                    0.0,
                    30.0 - (datetime.now(timezone.utc) - flow.transient_started_at).total_seconds(),
                )
            if recovery_remaining <= 0.0:
                flow.status = "failed"
                flow.error_code = "oauth_discovery_unavailable"
                await _release_terminal_flow(flow, successful=False)
                return flow.public()
            validated = await _flow_call(
                flow,
                "_openclank/provider-control/v1/account/validate",
                _account_validation_wire(
                    connection=connection,
                    auth_method=expected_auth_method,
                    credential=dict(credential),
                    account_id=host_account_id,
                    credential_revision=expected_credential_revision,
                    acquisition_method=flow.acquisition_method,
                    credential_kind=credential_kind,
                ),
                min(
                    _OAUTH_VALIDATE_TIMEOUT_SECONDS,
                    recovery_remaining,
                    max(0.001, (flow.expires_at - datetime.now(timezone.utc)).total_seconds()),
                ),
            )
            if flow.transient_started_at is not None and (
                datetime.now(timezone.utc) - flow.transient_started_at
            ).total_seconds() >= 30:
                flow.status = "failed"
                flow.error_code = "oauth_discovery_unavailable"
                await _release_terminal_flow(flow, successful=False)
                return flow.public()
            _validate_account_auth_result(
                validated,
                connection=connection,
                expected_acquisition_method=flow.acquisition_method,
                expected_credential_kind=credential_kind,
            )
            discovery = _validated_account_discovery(
                validated,
                account_id=host_account_id,
                credential_revision=expected_credential_revision,
            )
            if discovery["status"] == "reauth_required":
                flow.status = "failed"
                flow.error_code = "oauth_reauth_required"
                await _release_terminal_flow(flow, successful=False)
                return flow.public()
            if discovery["status"] == "unavailable":
                if discovery.get("retryable") and _schedule_transient_retry(
                    flow, discovery.get("retryAfterMs")
                ):
                    return flow.public()
                flow.status = "failed"
                flow.error_code = "oauth_discovery_unavailable"
                await _release_terminal_flow(flow, successful=False)
                return flow.public()
            if (
                str(validated.get("authMethod") or "") != expected_auth_method
                or validated.get("credentialKind") != credential_kind
                or validated.get("acquisitionMethod") != flow.acquisition_method
            ):
                raise HTTPException(502, "Managed provider OAuth validation changed its method")
            declared_routes = _engine_model_routes(
                store=store,
                owner=flow.owner,
                operation=f"oauth.complete:{flow.mode}:{flow.connection_id}",
                idempotency_key=flow.flow_id,
                raw_routes=validated.get("modelRoutes") or [],
            )
            if flow.cancel_requested or flow.expired():
                flow.status = "expired" if flow.expired() else "cancelled"
                flow.error_code = "oauth_expired" if flow.status == "expired" else "oauth_cancelled"
                await _release_terminal_flow(flow, successful=False)
                return flow.public()
            from contextlib import nullcontext
            from routes.prefs_routes import PREFERENCE_LOCK
            guarded = flow.provider_id == "anthropic" and flow.billing_lane == "subscription"
            with PREFERENCE_LOCK if guarded else nullcontext():
                _assert_claude_flow_policy(flow)
                row = store.persist_account_discovery(
                    owner=flow.owner,
                    account_id=host_account_id,
                    connection_id=flow.connection_id,
                    label=flow.label,
                    auth_method=str(validated["authMethod"]),
                    acquisition_method=flow.acquisition_method,
                    auth_class=str(validated["authClass"]),
                    credentials=dict(validated["credential"]),
                    safe_identity=dict(validated.get("safeIdentity") or {}),
                    model_routes=declared_routes,
                    status=str(discovery["status"]),
                    authoritative=bool(discovery.get("authoritative")),
                    error_code=_discovery_error_code(discovery),
                    provenance=discovery.get("provenance"),
                    expected_account_revision=(
                        None if flow.mode == "add" else flow.expected_revision
                    ),
                    expected_credential_revision=expected_credential_revision,
                )
        except ProviderStoreError as exc:
            if flow.target_account_id and not isinstance(exc, ProviderConflict):
                try:
                    store.set_account_health(
                        owner=flow.owner,
                        account_id=flow.target_account_id,
                        state="reauth_required",
                        last_error_code="oauth_persistence_failed",
                    )
                except ProviderStoreError:
                    pass
                _invalidate_inventory_cache(
                    owner=flow.owner,
                    connection_id=flow.connection_id,
                )
            flow.status = "failed"
            flow.error_code = "oauth_persistence_failed"
            await _release_terminal_flow(flow, successful=False)
            _raise_store_error(exc)

        flow.account_public = _account_json(row)
        flow.status = "complete"
        flow.error_code = None
        flow.retry_after_ms = None
        flow.transient_failures = 0
        flow.transient_started_at = None
        flow.next_attempt_at = None
        try:
            _invalidate_inventory_cache(owner=flow.owner, connection_id=flow.connection_id)
        except Exception:
            # The durable account commit already won; cache cleanup cannot
            # downgrade the committed flow or trigger a second persistence.
            pass
        try:
            await _flow_call(
                flow,
                "_openclank/provider-control/v1/oauth/cancel",
                flow.completion_payload(),
                _OAUTH_CANCEL_TIMEOUT_SECONDS,
            )
        except Exception:
            pass
        await _release_terminal_flow(flow, successful=True)
        return flow.public()

    def _check_claude_start(owner: str, connection: Any) -> Optional[int]:
        if connection.family_id != "anthropic" or connection.billing_lane != "subscription":
            return
        from src.openclank.provider_method_policy import require_enabled, projection, ProviderMethodPolicyError
        try:
            revision = require_enabled(owner)
        except ProviderMethodPolicyError as exc:
            raise HTTPException(409, {"code": exc.code, "message": str(exc)}) from exc
        # The projection permits actor execution only; the bound worker must
        # still prove the method-policy capability before OAuth IPC.
        if not projection(owner)["managed_admission"]["available"]:
            raise HTTPException(503, {"code": "native_gate_unavailable", "message": "Claude subscription Beta admission is not available yet"})
        return revision

    async def _start_oauth_flow(
        *,
        request: Request,
        owner: str,
        connection: Any,
        label: str,
        method: int,
        inputs: Mapping[str, str],
        mode: str,
        target_account_id: Optional[str] = None,
        expected_revision: Optional[int] = None,
    ) -> tuple[OAuthHostFlow, dict[str, Any], dict[str, Any]]:
        await _expire_oauth_flows()
        method_policy_revision = _check_claude_start(owner, connection)
        try:
            engine = await control.bind(request=request, owner=owner)
            if method_policy_revision is not None:
                engine.require_method_policy_contract()
                # Recheck after worker acquisition and before any engine IPC.
                from src.openclank.provider_method_policy import require_enabled, ProviderMethodPolicyError
                try:
                    require_enabled(owner, method_policy_revision)
                except ProviderMethodPolicyError as exc:
                    raise HTTPException(409, {"code": exc.code, "message": str(exc)}) from exc
            if method_policy_revision is not None:
                # The pinned Claude plugin publishes both methods locally. oauth/start
                # still validates the selected method and lane against the live plugin.
                family = next(f for f in bundled_provider_family_catalog()["families"]
                              if f["id"] == "anthropic")
                declared_methods = [
                    {"id": m["id"], "type": m["type"], "billingLanes": m["billing_lanes"],
                     "credentialKinds": m["credential_kinds"], "acquisitionMethod": m["acquisition_method"]}
                    for m in family["auth_methods"] if m["type"] == "oauth"
                ]
            else:
                raw_catalog = await engine.call("_openclank/provider-control/v1/catalog", {})
                declared_methods = _provider_catalog_auth_methods(raw_catalog, family_id=connection.family_id)
            selected = next(
                (item for item in declared_methods if item.get("id") == f"oauth:{method}"),
                None,
            )
            if (
                not isinstance(selected, Mapping)
                or selected.get("type") != "oauth"
                or connection.billing_lane not in selected.get("billingLanes", ())
            ):
                raise HTTPException(422, "Selected provider sign-in method does not support this billing lane")
            if (
                connection.family_id == "kilo"
                and selected.get("acquisitionMethod") == "device"
                and connection.normalized_url != "https://ai-gateway.kilo.ai/api/v1"
            ):
                raise HTTPException(
                    422,
                    "Kilo device sign-in requires a connection using the canonical Gateway endpoint",
                )
            if (
                connection.family_id in {"llmgateway", "llmgateway-devpass"}
                and selected.get("acquisitionMethod") == "browser"
                and connection.normalized_url != "https://api.llmgateway.io/v1"
            ):
                raise HTTPException(
                    422,
                    "LLM Gateway browser sign-in requires a connection using the canonical API endpoint",
                )
            flow = OAuthHostFlow.create(
                owner=owner,
                connection_id=connection.id,
                provider_id=connection.family_id,
                billing_lane=connection.billing_lane,
                mode=mode,
                label=label,
                method=method,
                engine=engine,
                target_account_id=target_account_id,
                expected_revision=expected_revision,
                expected_credential_kinds=tuple(selected["credentialKinds"]),
                acquisition_method=str(selected["acquisitionMethod"]),
                method_policy_revision=method_policy_revision,
            )
            oauth_flows.add(flow)
            flow.expiry_task = asyncio.create_task(_flow_deadline(flow))
            start = await _flow_call(
                flow,
                "_openclank/provider-control/v1/oauth/start",
                flow.start_payload(
                    redirect_uri=(
                        f"{_provider_public_origin(request)}"
                        f"/api/v1/providers/oauth/callback?flow_id={flow.flow_id}"
                    ),
                    inputs=_oauth_inputs(inputs),
                ),
                _OAUTH_START_TIMEOUT_SECONDS,
            )
            if start.get("flowID") != flow.flow_id:
                raise HTTPException(502, "Managed provider OAuth flow ID changed")
            safe_start = dict(start)
            safe_start["url"] = _safe_authorization_url(start.get("url"))
            flow.public_start = {
                "url": safe_start["url"],
                "method": safe_start["method"],
                "instructions": safe_start.get("instructions", ""),
                **(
                    {"userCode": safe_start["userCode"]}
                    if isinstance(safe_start.get("userCode"), str)
                    else {}
                ),
            }
            return flow, safe_start, flow.public(start=safe_start)
        except Exception:
            if "flow" in locals():
                oauth_flows.remove(flow.flow_id)
            if "engine" in locals():
                await engine.release(successful=False)
            raise

    @router.get("/method-policy")
    def get_method_policy(request: Request):
        from src.openclank.provider_method_policy import projection, ProviderMethodPolicyError
        owner = _oauth_owner(request)
        try:
            return projection(owner)
        except ProviderMethodPolicyError as exc:
            raise HTTPException(409, {"code":exc.code, "message":str(exc)}) from exc

    @router.put("/method-policy")
    async def put_method_policy(request: Request, body: dict):
        from src.openclank.provider_method_policy import set_policy, projection, ProviderMethodPolicyError
        owner = _oauth_owner(request, write=True)
        if type(body) is not dict or set(body) != {"enabled", "expected_revision"}:
            raise HTTPException(422, "Boolean enablement and expected revision are required")
        try:
            policy = set_policy(owner, enabled=body["enabled"], expected_revision=body["expected_revision"])
        except ProviderMethodPolicyError as exc:
            raise HTTPException(409 if exc.code == "revision_conflict" else 422, {"code":exc.code, "message":str(exc)}) from exc
        affected = [flow for flow in oauth_flows.snapshot() if flow.owner == owner and flow.provider_id == "anthropic" and flow.billing_lane == "subscription" and flow.status in {"pending", "running"}]
        if not body["enabled"]:
            for flow in affected:
                flow.cancel_requested = True
            for flow in affected:
                await _fail_flow(flow, HTTPException(409, "Claude subscription Beta disabled"))
        return {**projection(owner), "flow_cancellation_requested":len(affected) if not body["enabled"] else 0,
                "active_operation_cancellation":"cooperative_next_admission"}

    @router.get("/families")
    def families(request: Request, response: Response):
        _provider_owner(request)
        try:
            result = bundled_provider_family_catalog()
        except ProviderFamilyCatalogError as exc:
            logger.error("Bundled provider family catalogue is invalid: %s", exc)
            raise HTTPException(503, "Provider family catalogue is unavailable") from exc
        if not _comfy_available(request):
            result = {**result, "families": [family for family in result["families"] if family.get("id") != "comfyui"]}
        if not callable(getattr(request.app.state, 'blender_human_service_factory', None)):
            result = {**result, "families": [family for family in result["families"] if family.get("id") != "blender"]}
        if not callable(getattr(request.app.state, 'trellis_human_service_factory', None)):
            result = {**result, "families": [family for family in result["families"] if family.get("id") != "trellis"]}
        if not callable(getattr(request.app.state, 'hunyuan_human_service_factory', None)):
            result = {**result, "families": [family for family in result["families"] if family.get("id") != "hunyuan-shape"]}
        if not callable(getattr(request.app.state, 'sprixen_human_service_factory', None)):
            result = {**result, "families": [family for family in result["families"] if family.get("id") != "sprixen"]}
        if (not callable(getattr(request.app.state, 'tripo_human_service_factory', None))
                or getattr(request.app.state, 'provider_store', None) is not store
                or not all(getattr(request.app.state, key, None) is not None for key in ('auth_manager','account_request_barrier','session_manager','media_artifact_store'))):
            result = {**result, "families": [family for family in result["families"] if family.get("id") != "tripo"]}
        if (not callable(getattr(request.app.state, 'meshy_human_service_factory', None))
                or getattr(request.app.state, 'provider_store', None) is not store
                or not all(getattr(request.app.state, key, None) is not None for key in ('auth_manager','account_request_barrier','session_manager','media_artifact_store'))):
            result = {**result, "families": [family for family in result["families"] if family.get("id") != "meshy"]}
        # Availability follows the installed app-owned service and must not be cached across its lifetime.
        response.headers["Cache-Control"] = "private, no-store"
        response.headers["Vary"] = "Cookie, Authorization"
        return result

    @router.post("/families/{family_id}/auth-methods")
    async def family_auth_methods(
        family_id: str,
        request: Request,
        response: Response,
    ):
        """Explicitly enrich one family's plugin-discovered sign-in methods.

        This is deliberately a user-triggered POST.  The supervisor-free
        family read never invokes the managed engine; selecting this action in
        the UI may admit the caller's provider-control worker and can therefore
        take an unbounded amount of time.
        """

        owner = _oauth_owner(request, write=True)
        _require_comfy(request, family_id)
        baseline = bundled_provider_family_catalog()
        known_ids = {item["id"] for item in baseline["families"]}
        if family_id not in known_ids:
            raise HTTPException(404, "Unknown provider family")
        try:
            raw_catalog = await control.call(
                request=request,
                owner=owner,
                method="_openclank/provider-control/v1/catalog",
                payload={},
            )
            methods = _provider_catalog_auth_methods(
                raw_catalog,
                family_id=family_id,
            )
        except Exception as exc:
            _raise_engine_error(exc)
        response.headers["Cache-Control"] = "private, no-store"
        response.headers["Vary"] = "Cookie, Authorization"
        return {
            "family_id": family_id,
            "source": "managed-engine",
            "auth_methods": methods,
            "auth_methods_complete": True,
        }

    def _engine_installation_state(request: Request) -> str:
        verification = getattr(request.app.state, "engine_verification", None)
        if verification is None:
            return "unverified"
        if verification.ok:
            return "verified"
        if "managed engine binary is missing" in verification.errors:
            return "missing"
        return "unusable"

    @router.get("/management-snapshot")
    def management_snapshot(request: Request, response: Response):
        """Return all nonsecret Added Models state from one DB transaction."""

        owner = _provider_owner(request)
        started = perf_counter()
        try:
            snapshot = store.management_snapshot(owner=owner)
        except ProviderStoreError as exc:
            _raise_store_error(exc)
        body = {
            "schema_version": 1,
            "engine_installation": _engine_installation_state(request),
            "connections": [
                {
                    **_connection_json(row),
                    "account_count": int(snapshot.account_counts.get(row.id, 0)),
                    "reauth_count": int(snapshot.reauth_counts.get(row.id, 0)),
                }
                for row in snapshot.connections
            ],
            "models": [_model_json(row) for row in snapshot.model_routes],
            "shares": {
                "received": [
                    _received_share_json(item)
                    for item in snapshot.received_shares
                ],
            },
            "deferred": [
                "accounts",
                "bindings",
                "owned_shares",
                "recipients",
            ],
        }
        elapsed_ms = max(0.0, (perf_counter() - started) * 1000.0)
        response.headers["Cache-Control"] = "private, no-store"
        response.headers["Vary"] = "Cookie, Authorization"
        response.headers["Server-Timing"] = f"provider-db;dur={elapsed_ms:.3f}"
        return body

    @router.get("/connections")
    def list_connections(request: Request):
        owner = _provider_owner(request)
        try:
            rows = store.list_connections(owner=owner)
        except ProviderStoreError as exc:
            _raise_store_error(exc)
        return {"connections": [_connection_json(row) for row in rows]}

    @router.post("/connections")
    async def create_connection(
        payload: ConnectionCreate,
        request: Request,
        idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key"),
    ):
        owner = _provider_owner(request, write=True)
        _require_comfy(request, payload.family_id)
        operation = "connections.create"
        try:
            key, digest, replay = _begin_idempotent(
                store,
                owner=owner,
                operation=operation,
                key_header=idempotency_key,
                payload=_request_material(payload),
            )
            if replay is not None:
                return _mutation_response(replay, replayed=True)
            _assert_nonsecret_settings(payload.settings)
            validated = await control.call(
                request=request,
                owner=owner,
                method="_openclank/provider-control/v1/connection/validate",
                payload=_connection_validation_wire(
                    family_id=payload.family_id,
                    adapter_id=payload.adapter_id,
                    kind=payload.kind,
                    billing_lane=payload.billing_lane,
                    url=payload.url,
                    settings=payload.settings,
                ),
            )
            if payload.family_id == 'trellis':
                from src.openclank.trellis_runtime import validate_declaration
                try: validated['settings'] = validate_declaration(validated['settings'])
                except (ValueError, TypeError, UnicodeError, KeyError) as exc: raise ProviderValidationError('TRELLIS declaration unavailable') from exc
            if payload.family_id == 'hunyuan-shape':
                from src.openclank.hunyuan_runtime import validate_declaration
                try: validated['settings'] = validate_declaration(validated['settings'])
                except (ValueError, TypeError, UnicodeError, KeyError) as exc: raise ProviderValidationError('Hunyuan declaration unavailable') from exc
            declared_routes = _engine_model_routes(
                store=store,
                owner=owner,
                operation=operation,
                idempotency_key=key,
                raw_routes=validated.get("modelRoutes") or [],
            )
            row, model_rows = store.create_connection_with_routes(
                owner=owner,
                connection_id=store.deterministic_resource_id(
                    prefix="pcn",
                    owner=owner,
                    operation=operation,
                    idempotency_key=key,
                ),
                family_id=validated["familyID"],
                adapter_id=validated["adapterID"],
                kind=validated["kind"],
                billing_lane=validated["billingLane"],
                label=payload.label,
                normalized_url=validated.get("normalizedURL"),
                settings=validated["settings"],
                enabled=payload.enabled,
                model_routes=declared_routes,
            )
            body = _connection_json(row)
            if model_rows:
                body["models"] = [_model_json(model) for model in model_rows]
            return _record_response(
                store,
                owner=owner,
                operation=operation,
                key=key,
                digest=digest,
                status_code=201,
                body=body,
                resource_id=row.id,
            )
        except ProviderStoreError as exc:
            _raise_store_error(exc)
        except Exception as exc:
            _raise_engine_error(exc)

    @router.get("/connections/{connection_id}")
    def get_connection(connection_id: str, request: Request, response: Response):
        owner = _provider_owner(request)
        try:
            row = store.get_connection(owner=owner, connection_id=connection_id)
        except ProviderStoreError as exc:
            _raise_store_error(exc)
        response.headers["ETag"] = _etag(row.revision)
        return _connection_json(row)

    @router.patch("/connections/{connection_id}")
    async def update_connection(
        connection_id: str,
        payload: ConnectionUpdate,
        request: Request,
        if_match: Optional[str] = Header(default=None, alias="If-Match"),
        idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key"),
    ):
        owner = _provider_owner(request, write=True)
        expected = _if_match(if_match)
        operation = f"connections.update:{connection_id}"
        try:
            early_connection = store.get_connection(owner=owner, connection_id=connection_id)
        except ProviderStoreError as exc:
            _raise_store_error(exc)
        if early_connection.family_id in {'blender', 'trellis', 'hunyuan-shape'}:
            _require_comfy(request, early_connection.family_id)
        material = _request_material(payload)
        material["expected_revision"] = expected
        try:
            key, digest, replay = _begin_idempotent(
                store,
                owner=owner,
                operation=operation,
                key_header=idempotency_key,
                payload=material,
            )
            if replay is not None:
                return _mutation_response(replay, replayed=True)
            current = store.get_connection(owner=owner, connection_id=connection_id)
            _require_comfy(request, current.family_id)
            kwargs: dict[str, Any] = {}
            fields = payload.model_fields_set
            if not fields:
                raise HTTPException(422, "No connection fields were provided")
            if "label" in fields:
                kwargs["label"] = payload.label
            if "enabled" in fields:
                kwargs["enabled"] = payload.enabled
            if "account_pool_policy" in fields:
                kwargs["account_pool_policy"] = payload.account_pool_policy
            candidate_settings = (
                payload.settings or {}
                if "settings" in fields
                else dict(current.settings or {})
            )
            _assert_nonsecret_settings(candidate_settings)
            candidate_url = payload.url if "url" in fields else current.normalized_url
            validated = await control.call(
                request=request,
                owner=owner,
                method="_openclank/provider-control/v1/connection/validate",
                payload=_connection_validation_wire(
                    family_id=current.family_id,
                    adapter_id=current.adapter_id,
                    kind=current.kind,
                    billing_lane=current.billing_lane,
                    url=candidate_url,
                    settings=candidate_settings,
                ),
            )
            if "settings" in fields:
                if current.family_id == 'trellis':
                    from src.openclank.trellis_runtime import validate_declaration
                    try: validated['settings'] = validate_declaration(validated['settings'])
                    except (ValueError, TypeError, UnicodeError, KeyError) as exc: raise ProviderValidationError('TRELLIS declaration unavailable') from exc
                if current.family_id == 'hunyuan-shape':
                    from src.openclank.hunyuan_runtime import validate_declaration
                    try: validated['settings'] = validate_declaration(validated['settings'])
                    except (ValueError, TypeError, UnicodeError, KeyError) as exc: raise ProviderValidationError('Hunyuan declaration unavailable') from exc
                kwargs["settings"] = validated["settings"]
            if "url" in fields:
                kwargs["normalized_url"] = validated.get("normalizedURL")
            row = store.update_connection(
                owner=owner,
                connection_id=connection_id,
                expected_revision=expected,
                **kwargs,
            )
            _invalidate_inventory_cache(owner=owner, connection_id=row.id)
            body = _connection_json(row)
            return _record_response(
                store,
                owner=owner,
                operation=operation,
                key=key,
                digest=digest,
                status_code=200,
                body=body,
                resource_id=row.id,
            )
        except ProviderStoreError as exc:
            _raise_store_error(exc)
        except Exception as exc:
            _raise_engine_error(exc)

    @router.delete("/connections/{connection_id}")
    def delete_connection(
        connection_id: str,
        request: Request,
        if_match: Optional[str] = Header(default=None, alias="If-Match"),
        idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key"),
    ):
        owner = _provider_owner(request, write=True)
        expected = _if_match(if_match)
        operation = f"connections.delete:{connection_id}"
        try:
            key, digest, replay = _begin_idempotent(
                store,
                owner=owner,
                operation=operation,
                key_header=idempotency_key,
                payload={"expected_revision": expected},
            )
            if replay is not None:
                return _mutation_response(replay, replayed=True)
            row = store.delete_connection(
                owner=owner,
                connection_id=connection_id,
                expected_revision=expected,
            )
            _invalidate_inventory_cache(owner=owner, connection_id=row.id)
            body = {
                "id": row.id,
                "status": "deleted",
                "revision": int(row.revision),
            }
            return _record_response(
                store,
                owner=owner,
                operation=operation,
                key=key,
                digest=digest,
                status_code=200,
                body=body,
                resource_id=row.id,
            )
        except ProviderStoreError as exc:
            _raise_store_error(exc)

    @router.get("/connections/{connection_id}/accounts")
    def list_accounts(connection_id: str, request: Request):
        owner = _provider_owner(request)
        try:
            rows = store.list_accounts(owner=owner, connection_id=connection_id)
        except ProviderStoreError as exc:
            _raise_store_error(exc)
        return {"accounts": [_account_json(row) for row in rows]}

    @router.post("/connections/{connection_id}/accounts")
    async def create_account(
        connection_id: str,
        payload: AccountCreate,
        request: Request,
        idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key"),
    ):
        owner = _provider_owner(request, write=True)
        operation = f"accounts.create:{connection_id}"
        api_key_value = _api_key_value(payload.api_key)
        try:
            key, digest, replay = _begin_idempotent(
                store,
                owner=owner,
                operation=operation,
                key_header=idempotency_key,
                payload=_request_material(payload),
            )
            if replay is not None:
                return _mutation_response(replay, replayed=True)
            connection = store.get_connection(owner=owner, connection_id=connection_id)
            account_id = store.deterministic_resource_id(
                prefix="pac",
                owner=owner,
                operation=operation,
                idempotency_key=key,
            )
            validated = await control.call(
                request=request,
                owner=owner,
                method="_openclank/provider-control/v1/account/validate",
                payload=_account_validation_wire(
                    connection=connection,
                    auth_method="api_key",
                    acquisition_method="manual-key",
                    credential={"type": "api", "key": api_key_value},
                    account_id=account_id,
                    credential_revision=1,
                ),
            )
            _validate_account_auth_result(
                validated,
                connection=connection,
                expected_acquisition_method="manual-key",
                expected_credential_kind="api",
            )
            discovery = _validated_account_discovery(
                validated,
                account_id=account_id,
                credential_revision=1,
            )
            if discovery["status"] == "unavailable":
                raise HTTPException(503, "Managed provider account discovery is unavailable")
            declared_routes = _engine_model_routes(
                store=store,
                owner=owner,
                operation=f"{operation}:models",
                idempotency_key=key,
                raw_routes=validated.get("modelRoutes") or [],
            )
            row = store.persist_account_discovery(
                owner=owner,
                connection_id=connection_id,
                account_id=account_id,
                label=payload.label,
                auth_method=str(validated["authMethod"]),
                acquisition_method="manual-key",
                auth_class=str(validated["authClass"]),
                credentials=validated["credential"],
                safe_identity=dict(validated["safeIdentity"]),
                model_routes=declared_routes,
                status=str(discovery["status"]),
                authoritative=bool(discovery.get("authoritative")),
                error_code=_discovery_error_code(discovery),
                provenance=discovery.get("provenance"),
                expected_credential_revision=1,
            )
            _invalidate_inventory_cache(owner=owner, connection_id=connection_id)
            body = _account_json(row)
            return _record_response(
                store,
                owner=owner,
                operation=operation,
                key=key,
                digest=digest,
                status_code=201,
                body=body,
                resource_id=row.id,
            )
        except ProviderStoreError as exc:
            _raise_store_error(exc)
        except Exception as exc:
            _raise_engine_error(exc)

    @router.get("/accounts/{account_id}")
    def get_account(account_id: str, request: Request, response: Response):
        owner = _provider_owner(request)
        try:
            row = store.get_account(owner=owner, account_id=account_id)
        except ProviderStoreError as exc:
            _raise_store_error(exc)
        response.headers["ETag"] = _etag(row.revision)
        return _account_json(row)

    @router.patch("/accounts/{account_id}")
    async def update_account(
        account_id: str,
        payload: AccountUpdate,
        request: Request,
        if_match: Optional[str] = Header(default=None, alias="If-Match"),
        idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key"),
    ):
        owner = _provider_owner(request, write=True)
        expected = _if_match(if_match)
        operation = f"accounts.update:{account_id}"
        api_key_value: Optional[str] = None
        if "api_key" in payload.model_fields_set and payload.api_key is not None:
            api_key_value = _api_key_value(payload.api_key)
        material = _request_material(payload)
        material["expected_revision"] = expected
        try:
            key, digest, replay = _begin_idempotent(
                store,
                owner=owner,
                operation=operation,
                key_header=idempotency_key,
                payload=material,
            )
            if replay is not None:
                return _mutation_response(replay, replayed=True)
            kwargs: dict[str, Any] = {}
            declared_routes: list[dict[str, Any]] = []
            if "label" in payload.model_fields_set:
                kwargs["label"] = payload.label
            if "api_key" in payload.model_fields_set:
                if payload.api_key is None:
                    raise HTTPException(422, "api_key cannot be null")
                current = store.get_account(owner=owner, account_id=account_id)
                if current.auth_method != "api_key":
                    raise HTTPException(
                        422,
                        "OAuth accounts must be reconnected through the OAuth flow",
                    )
                connection = store.get_connection(
                    owner=owner,
                    connection_id=current.connection_id,
                )
                validated = await control.call(
                    request=request,
                    owner=owner,
                    method="_openclank/provider-control/v1/account/validate",
                    payload=_account_validation_wire(
                        connection=connection,
                        auth_method="api_key",
                        acquisition_method="manual-key",
                        credential={"type": "api", "key": api_key_value},
                        account_id=current.id,
                        credential_revision=int(current.credential_version) + 1,
                    ),
                )
                _validate_account_auth_result(
                    validated,
                    connection=connection,
                    expected_acquisition_method="manual-key",
                    expected_credential_kind="api",
                )
                discovery = _validated_account_discovery(
                    validated,
                    account_id=current.id,
                    credential_revision=int(current.credential_version) + 1,
                )
                if discovery["status"] == "unavailable":
                    raise HTTPException(503, "Managed provider account discovery is unavailable")
                kwargs["credentials"] = validated["credential"]
                kwargs["safe_identity"] = validated["safeIdentity"]
                declared_routes = _engine_model_routes(
                    store=store,
                    owner=owner,
                    operation=f"{operation}:models",
                    idempotency_key=key,
                    raw_routes=validated.get("modelRoutes") or [],
                )
            if not kwargs:
                raise HTTPException(422, "No account fields were provided")
            if "api_key" in payload.model_fields_set:
                row = store.persist_account_discovery(
                    owner=owner,
                    account_id=account_id,
                    connection_id=current.connection_id,
                    label=str(kwargs.get("label", current.label)),
                    auth_method="api_key",
                    acquisition_method="manual-key",
                    auth_class=str(validated["authClass"]),
                    credentials=dict(kwargs["credentials"]),
                    safe_identity=dict(kwargs["safe_identity"]),
                    model_routes=declared_routes,
                    status=str(discovery["status"]),
                    authoritative=bool(discovery.get("authoritative")),
                    error_code=_discovery_error_code(discovery),
                    provenance=discovery.get("provenance"),
                    expected_account_revision=expected,
                    expected_credential_revision=int(current.credential_version) + 1,
                )
            else:
                row = store.update_account(
                    owner=owner,
                    account_id=account_id,
                    expected_revision=expected,
                    **kwargs,
                )
            _invalidate_inventory_cache(owner=owner, connection_id=row.connection_id)
            if "api_key" in payload.model_fields_set:
                _invalidate_inventory_cache(owner=owner, connection_id=row.connection_id)
            body = _account_json(row)
            return _record_response(
                store,
                owner=owner,
                operation=operation,
                key=key,
                digest=digest,
                status_code=200,
                body=body,
                resource_id=row.id,
            )
        except ProviderStoreError as exc:
            _raise_store_error(exc)
        except Exception as exc:
            _raise_engine_error(exc)

    @router.post("/accounts/{account_id}/enable")
    def enable_account(
        account_id: str,
        payload: AccountEnable,
        request: Request,
        if_match: Optional[str] = Header(default=None, alias="If-Match"),
        idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key"),
    ):
        owner = _provider_owner(request, write=True)
        expected = _if_match(if_match)
        operation = f"accounts.enable:{account_id}"
        try:
            key, digest, replay = _begin_idempotent(
                store,
                owner=owner,
                operation=operation,
                key_header=idempotency_key,
                payload={"enabled": payload.enabled, "expected_revision": expected},
            )
            if replay is not None:
                return _mutation_response(replay, replayed=True)
            row = store.update_account(
                owner=owner,
                account_id=account_id,
                expected_revision=expected,
                enabled=payload.enabled,
            )
            _invalidate_inventory_cache(owner=owner, connection_id=row.connection_id)
            body = _account_json(row)
            return _record_response(
                store,
                owner=owner,
                operation=operation,
                key=key,
                digest=digest,
                status_code=200,
                body=body,
                resource_id=row.id,
            )
        except ProviderStoreError as exc:
            _raise_store_error(exc)

    @router.delete("/accounts/{account_id}")
    def delete_account(
        account_id: str,
        request: Request,
        if_match: Optional[str] = Header(default=None, alias="If-Match"),
        idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key"),
    ):
        owner = _provider_owner(request, write=True)
        expected = _if_match(if_match)
        operation = f"accounts.delete:{account_id}"
        try:
            key, digest, replay = _begin_idempotent(
                store,
                owner=owner,
                operation=operation,
                key_header=idempotency_key,
                payload={"expected_revision": expected},
            )
            if replay is not None:
                return _mutation_response(replay, replayed=True)
            row = store.delete_account(
                owner=owner,
                account_id=account_id,
                expected_revision=expected,
            )
            _invalidate_inventory_cache(owner=owner, connection_id=row.connection_id)
            body = {
                "id": row.id,
                "status": "deleted",
                "revision": int(row.revision),
            }
            return _record_response(
                store,
                owner=owner,
                operation=operation,
                key=key,
                digest=digest,
                status_code=200,
                body=body,
                resource_id=row.id,
            )
        except ProviderStoreError as exc:
            _raise_store_error(exc)

    @router.post("/connections/{connection_id}/oauth/start")
    async def oauth_add_start(
        connection_id: str,
        payload: OAuthStartPayload,
        request: Request,
        idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key"),
    ):
        owner = _oauth_owner(request, write=True)
        operation = f"oauth.start:add:{connection_id}"
        try:
            key, digest, replay = _begin_idempotent(
                store,
                owner=owner,
                operation=operation,
                key_header=idempotency_key,
                payload=_request_material(payload),
            )
            connection = store.get_connection(owner=owner, connection_id=connection_id)
            _check_claude_start(owner, connection)
            if replay is not None:
                if connection.family_id == "anthropic" and connection.billing_lane == "subscription":
                    try:
                        replay_flow = oauth_flows.get(replay.resource_id, owner=owner)
                    except Exception as exc:
                        raise HTTPException(409, "Claude subscription login replay is unavailable") from exc
                    _assert_claude_flow_policy(replay_flow)
                return _mutation_response(replay, replayed=True)
            flow, _, body = await _start_oauth_flow(
                request=request,
                owner=owner,
                connection=connection,
                label=payload.label,
                method=payload.method,
                inputs=payload.inputs,
                mode="add",
            )
            return _record_response(
                store,
                owner=owner,
                operation=operation,
                key=key,
                digest=digest,
                status_code=201,
                body=body,
                resource_id=flow.flow_id,
            )
        except ProviderStoreError as exc:
            _raise_store_error(exc)
        except Exception as exc:
            _raise_engine_error(exc)

    @router.post("/accounts/{account_id}/oauth/reauth")
    async def oauth_reauth_start(
        account_id: str,
        payload: OAuthReauthPayload,
        request: Request,
        if_match: Optional[str] = Header(default=None, alias="If-Match"),
        idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key"),
    ):
        owner = _oauth_owner(request, write=True)
        expected = _if_match(if_match)
        operation = f"oauth.start:reauth:{account_id}"
        material = _request_material(payload)
        material["expected_revision"] = expected
        try:
            key, digest, replay = _begin_idempotent(
                store,
                owner=owner,
                operation=operation,
                key_header=idempotency_key,
                payload=material,
            )
            account = store.get_account(owner=owner, account_id=account_id)
            connection = store.get_connection(owner=owner, connection_id=account.connection_id)
            _check_claude_start(owner, connection)
            if replay is not None:
                if connection.family_id == "anthropic" and connection.billing_lane == "subscription":
                    try:
                        replay_flow = oauth_flows.get(replay.resource_id, owner=owner)
                    except Exception as exc:
                        raise HTTPException(409, "Claude subscription login replay is unavailable") from exc
                    _assert_claude_flow_policy(replay_flow)
                return _mutation_response(replay, replayed=True)
            if account.revision != expected:
                raise ProviderRevisionConflict(
                    expected=expected,
                    current=int(account.revision),
                )
            connection = store.get_connection(
                owner=owner,
                connection_id=account.connection_id,
            )
            reconnectable_api = (
                account.auth_method == "api_key"
                and connection.family_id == "xiaomi"
                and connection.adapter_id == "mimo-native"
            )
            if account.auth_method != "oauth" and not reconnectable_api:
                raise HTTPException(422, "Only OAuth or browser-issued MiMo API accounts can be reconnected")
            flow, _, body = await _start_oauth_flow(
                request=request,
                owner=owner,
                connection=connection,
                label=payload.label or account.label,
                method=payload.method,
                inputs=payload.inputs,
                mode="reauth",
                target_account_id=account.id,
                expected_revision=expected,
            )
            return _record_response(
                store,
                owner=owner,
                operation=operation,
                key=key,
                digest=digest,
                status_code=201,
                body=body,
                resource_id=flow.flow_id,
            )
        except ProviderStoreError as exc:
            _raise_store_error(exc)
        except Exception as exc:
            _raise_engine_error(exc)

    @router.get("/oauth/active")
    def active_browser_login(request: Request, response: Response):
        owner = _oauth_owner(request)
        response.headers["Cache-Control"] = "private, no-store"
        for flow in reversed(oauth_flows.snapshot()):
            if (flow.owner == owner and flow.status in {"pending", "running"}
                    and not flow.expired() and flow.public_start is not None):
                return {"flow": flow.public()}
        return {"flow": None}

    @router.get("/oauth/flows/{flow_id}")
    async def oauth_poll(flow_id: str, request: Request):
        owner = _oauth_owner(request)
        await _expire_oauth_flows()
        try:
            flow = oauth_flows.get(flow_id, owner=owner)
            if flow.status in {"complete", "failed", "cancelled", "expired"}:
                return flow.public()
            try:
                result = await _flow_call(
                    flow,
                    "_openclank/provider-control/v1/oauth/poll",
                    flow.completion_payload(),
                    _OAUTH_POLL_TIMEOUT_SECONDS,
                )
            except ProviderEngineTimeoutError:
                # A missed observation does not settle the provider's exchange.
                # The normal flow deadline still releases an unresponsive worker.
                public = flow.public()
                if flow.status in {"pending", "running"}:
                    public["retry_after_ms"] = 1500
                return public
            return await _persist_oauth_result(flow, result)
        except (ProviderEngineControlError, ProviderEngineValidationError, HTTPException) as exc:
            return await _fail_flow(flow, exc)
        except Exception as exc:
            _raise_engine_error(exc)

    @router.post("/oauth/flows/{flow_id}/callback")
    async def oauth_callback(
        flow_id: str,
        payload: OAuthCallbackPayload,
        request: Request,
    ):
        owner = _oauth_owner(request, write=True)
        await _expire_oauth_flows()
        try:
            flow = oauth_flows.get(flow_id, owner=owner)
            if not flow.state_matches(payload.state):
                raise HTTPException(400, "Provider OAuth state is invalid")
            if flow.status == "complete":
                return flow.public()
            return await _complete_oauth_callback(flow, code=payload.code)
        except Exception as exc:
            _raise_engine_error(exc)

    @router.get("/oauth/callback", response_class=HTMLResponse)
    async def oauth_browser_callback(
        request: Request,
        flow_id: str = Query(min_length=16, max_length=128),
        state: str = Query(min_length=16, max_length=256),
        code: Optional[str] = Query(default=None, max_length=131_072),
        error: Optional[str] = Query(default=None, max_length=256),
    ):
        try:
            owner = _oauth_owner(request, write=True)
            await _expire_oauth_flows()
            flow = oauth_flows.get(flow_id, owner=owner)
            if not flow.state_matches(state):
                return _oauth_page(False, "Provider OAuth state is invalid", status_code=400)
            if error:
                lock = await _acquire_flow_lock(flow.flow_id)
                acquired = True
                try:
                    if flow.status not in {"complete", "failed", "cancelled", "expired"}:
                        flow.status = "failed"
                        flow.error_code = "oauth_exchange_failed"
                        await _release_terminal_flow(flow, successful=False)
                finally:
                    if acquired:
                        _release_flow_lock(flow.flow_id, lock)
                return _oauth_page(False, "The provider denied login", status_code=400)
            await _complete_oauth_callback(flow, code=code)
            if flow.status == "complete":
                return _oauth_page(True, "Your provider account is connected")
            if flow.status in {"pending", "running"}:
                return _oauth_page(True, "Authorization received. Return to Open Clank.")
            return _oauth_page(False, "Provider login did not complete", status_code=400)
        except HTTPException as exc:
            return _oauth_page(False, str(exc.detail), status_code=exc.status_code)
        except Exception:
            return _oauth_page(False, "Provider login failed", status_code=502)

    @router.delete("/oauth/flows/{flow_id}")
    async def oauth_cancel(flow_id: str, request: Request):
        owner = _oauth_owner(request, write=True)
        await _expire_oauth_flows()
        try:
            flow = oauth_flows.get(flow_id, owner=owner)
            flow.cancel_requested = True
            lock = await _acquire_flow_lock(flow.flow_id)
            acquired = True
            try:
                if flow.status not in {"complete", "failed", "cancelled", "expired"}:
                    await _flow_call(
                        flow,
                        "_openclank/provider-control/v1/oauth/cancel",
                        flow.completion_payload(),
                        _OAUTH_CANCEL_TIMEOUT_SECONDS,
                    )
                    flow.status = "cancelled"
                    flow.error_code = "oauth_cancelled"
                    await _release_terminal_flow(flow, successful=False)
                return flow.public()
            finally:
                if acquired:
                    _release_flow_lock(flow.flow_id, lock)
        except Exception as exc:
            _raise_engine_error(exc)

    @router.post("/connections/{connection_id}/models/refresh")
    async def refresh_connection_models(
        connection_id: str,
        payload: ModelRefresh,
        request: Request,
        if_match: Optional[str] = Header(default=None, alias="If-Match"),
        idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key"),
    ):
        owner = _provider_owner(request, write=True)
        expected = _if_match(if_match)
        operation = f"models.refresh:{connection_id}"
        material = _request_material(payload)
        material["expected_revision"] = expected
        try:
            key, digest, replay = _begin_idempotent(
                store,
                owner=owner,
                operation=operation,
                key_header=idempotency_key,
                payload=material,
            )
            if replay is not None:
                return _mutation_response(replay, replayed=True)
            connection = store.get_connection(
                owner=owner,
                connection_id=connection_id,
            )
            if connection.billing_lane == "subscription":
                if connection.revision != expected:
                    raise ProviderRevisionConflict(expected=expected, current=connection.revision)
                accounts = [a for a in store.list_accounts(owner=owner, connection_id=connection.id) if a.enabled]
                if not accounts:
                    raise HTTPException(409, "Sign in to this provider to refresh its models")
                statuses = await asyncio.gather(*(
                    _refresh_inventory_account(owner=owner, account=a, connection=connection, request=request, force=True)
                    for a in accounts
                ))
                if all(status == "reauth_required" for status in statuses):
                    raise HTTPException(409, "Sign in again to refresh this provider’s models")
                if not any(status in {"complete", "partial"} for status in statuses):
                    raise HTTPException(503, "Provider model discovery is temporarily unavailable")
                updated = store.get_connection(owner=owner, connection_id=connection.id)
                model_rows = store.list_model_routes(owner=owner, connection_id=connection.id)
            else:
                validated = await control.call(
                    request=request,
                    owner=owner,
                    method="_openclank/provider-control/v1/connection/validate",
                    payload=_connection_validation_wire(
                        family_id=connection.family_id,
                        adapter_id=connection.adapter_id,
                        kind=connection.kind,
                        billing_lane=connection.billing_lane,
                        url=connection.normalized_url,
                        settings=dict(connection.settings or {}),
                    ),
                )
                declared_routes = _engine_model_routes(
                    store=store,
                    owner=owner,
                    operation=operation,
                    idempotency_key=key,
                    raw_routes=validated.get("modelRoutes") or [],
                )
                if not declared_routes:
                    raise HTTPException(
                        503,
                        "Managed provider model discovery is temporarily unavailable; existing models were preserved",
                    )
                updated, model_rows = store.sync_model_routes(
                    owner=owner,
                    connection_id=connection.id,
                    expected_revision=expected,
                    model_routes=declared_routes,
                    entitle_all_accounts=False,
                )
            _invalidate_inventory_cache(owner=owner, connection_id=connection.id)
            body = {
                "connection_id": updated.id,
                "revision": int(updated.revision),
                "models": [_model_json(row) for row in model_rows],
            }
            return _record_response(
                store,
                owner=owner,
                operation=operation,
                key=key,
                digest=digest,
                status_code=200,
                body=body,
                resource_id=updated.id,
            )
        except ProviderStoreError as exc:
            _raise_store_error(exc)
        except Exception as exc:
            _raise_engine_error(exc)

    @router.get("/models")
    def list_models(
        request: Request,
        connection_id: Optional[str] = Query(default=None, max_length=128),
    ):
        owner = _provider_owner(request)
        try:
            rows = store.list_model_routes(
                owner=owner,
                connection_id=connection_id,
            )
            rows = [
                row
                for row in rows
                if str(row.visibility or "visible") == "visible"
                and store.route_has_permitted_account(
                    owner=owner,
                    model_route_id=row.id,
                )
            ]
        except ProviderStoreError as exc:
            _raise_store_error(exc)
        return {"models": [_model_json(row) for row in rows]}

    @router.get("/models/{model_route_id}")
    def get_model(model_route_id: str, request: Request, response: Response):
        owner = _provider_owner(request)
        try:
            row = store.get_model_route(owner=owner, model_route_id=model_route_id)
        except ProviderStoreError as exc:
            _raise_store_error(exc)
        response.headers["ETag"] = _etag(row.revision)
        return _model_json(row)

    @router.get('/models/{model_route_id}/cross-model-policy')
    async def get_cross_model_policy(model_route_id: str, request: Request, response: Response):
        from src.openclank.provider_human_policy import capture_human_provider_policy, run_human_provider_policy
        admission = capture_human_provider_policy(request, store)
        try:
            result = await run_human_provider_policy(admission, lambda held: store.human_cross_model_policy(held, model_route_id))
        except ProviderValidationError as exc:
            if str(exc) == 'cross-model profile unavailable': raise HTTPException(503, str(exc)) from None
            _raise_store_error(exc)
        except ProviderStoreError as exc:
            _raise_store_error(exc)
        except ValueError:
            raise HTTPException(409, 'Provider policy authority changed') from None
        response.headers['ETag'] = _etag(result['route_revision'])
        return result

    @router.put('/models/{model_route_id}/cross-model-policy')
    async def put_cross_model_policy(model_route_id: str, request: Request, response: Response,
                                     if_match: Optional[str] = Header(default=None, alias='If-Match')):
        from src.openclank.provider_human_policy import capture_human_provider_policy, run_human_provider_policy
        admission = capture_human_provider_policy(request, store)
        revision = _if_match(if_match)
        import json
        chunks = []; size = 0
        async for chunk in request.stream():
            size += len(chunk)
            if size > 16384: raise HTTPException(413, 'Provider policy is too large')
            chunks.append(chunk)
        try: policy = json.loads(b''.join(chunks).decode('utf-8'))
        except (ValueError, UnicodeError, RecursionError): raise HTTPException(400, 'Invalid provider policy') from None
        try:
            result = await run_human_provider_policy(admission,
                lambda held: store.set_human_cross_model_policy(held, model_route_id, revision, policy))
        except ProviderValidationError as exc:
            if str(exc) == 'cross-model profile unavailable': raise HTTPException(503, str(exc)) from None
            _raise_store_error(exc)
        except ProviderStoreError as exc:
            _raise_store_error(exc)
        except ValueError as exc:
            if str(exc) == 'cross-model policy unavailable': raise HTTPException(422, str(exc)) from None
            raise HTTPException(409, 'Provider policy authority changed') from None
        response.headers['ETag'] = _etag(result['route_revision'])
        return result

    @router.get("/connections/{connection_id}/eligibility")
    def connection_eligibility(connection_id: str, request: Request):
        """Project all model health for one connection in one bounded read."""

        owner = _provider_owner(request)
        try:
            models = store.connection_eligibility(
                owner=owner,
                connection_id=connection_id,
            )
        except ProviderStoreError as exc:
            _raise_store_error(exc)
        return {
            "connection_id": connection_id,
            "models": [
                {
                    "model_route_id": item["model_route_id"],
                    "accounts": [
                        {
                            **row,
                            "account_cooldown_until": _iso(
                                row.get("account_cooldown_until")
                            ),
                            "account_quota_reset_at": _iso(
                                row.get("account_quota_reset_at")
                            ),
                            "model_cooldown_until": _iso(
                                row.get("model_cooldown_until")
                            ),
                            "model_quota_reset_at": _iso(
                                row.get("model_quota_reset_at")
                            ),
                        }
                        for row in item["accounts"]
                    ],
                }
                for item in models
            ],
        }

    @router.get("/models/{model_route_id}/eligibility")
    def model_eligibility(model_route_id: str, request: Request):
        owner = _provider_owner(request)
        try:
            rows = store.model_eligibility(
                owner=owner,
                model_route_id=model_route_id,
            )
        except ProviderStoreError as exc:
            _raise_store_error(exc)
        return {
            "model_route_id": model_route_id,
            "accounts": [
                {
                    **row,
                    "account_cooldown_until": _iso(
                        row.get("account_cooldown_until")
                    ),
                    "account_quota_reset_at": _iso(
                        row.get("account_quota_reset_at")
                    ),
                    "model_cooldown_until": _iso(
                        row.get("model_cooldown_until")
                    ),
                    "model_quota_reset_at": _iso(
                        row.get("model_quota_reset_at")
                    ),
                }
                for row in rows
            ],
        }

    @router.put("/connections/{connection_id}/pool/order")
    def reorder_pool(
        connection_id: str,
        payload: PoolOrder,
        request: Request,
        if_match: Optional[str] = Header(default=None, alias="If-Match"),
        idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key"),
    ):
        owner = _provider_owner(request, write=True)
        expected = _if_match(if_match)
        operation = f"pool.order:{connection_id}"
        material = _request_material(payload)
        material["expected_revision"] = expected
        try:
            key, digest, replay = _begin_idempotent(
                store,
                owner=owner,
                operation=operation,
                key_header=idempotency_key,
                payload=material,
            )
            if replay is not None:
                return _mutation_response(replay, replayed=True)
            connection, accounts = store.reorder_accounts(
                owner=owner,
                connection_id=connection_id,
                account_ids=payload.account_ids,
                expected_revision=expected,
            )
            body = {
                "connection_id": connection.id,
                "revision": int(connection.revision),
                "accounts": [_account_json(row) for row in accounts],
            }
            return _record_response(
                store,
                owner=owner,
                operation=operation,
                key=key,
                digest=digest,
                status_code=200,
                body=body,
                resource_id=connection.id,
            )
        except ProviderStoreError as exc:
            _raise_store_error(exc)

    @router.post("/connections/{connection_id}/pool/use-next")
    def use_next(
        connection_id: str,
        payload: UseNext,
        request: Request,
        if_match: Optional[str] = Header(default=None, alias="If-Match"),
        idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key"),
    ):
        owner = _provider_owner(request, write=True)
        expected = _if_match(if_match)
        operation = f"pool.use-next:{connection_id}"
        material = _request_material(payload)
        material["expected_revision"] = expected
        try:
            key, digest, replay = _begin_idempotent(
                store,
                owner=owner,
                operation=operation,
                key_header=idempotency_key,
                payload=material,
            )
            if replay is not None:
                return _mutation_response(replay, replayed=True)
            connection, account = store.advance_pool_cursor(
                owner=owner,
                connection_id=connection_id,
                model_route_id=payload.model_route_id,
                expected_revision=expected,
            )
            body = {
                "connection_id": connection.id,
                "revision": int(connection.revision),
                "next_account": _account_json(account),
            }
            return _record_response(
                store,
                owner=owner,
                operation=operation,
                key=key,
                digest=digest,
                status_code=200,
                body=body,
                resource_id=connection.id,
            )
        except ProviderStoreError as exc:
            _raise_store_error(exc)

    @router.get("/bindings")
    def list_bindings(request: Request):
        owner = _provider_owner(request)
        try:
            rows = store.list_route_bindings(owner=owner)
        except ProviderStoreError as exc:
            _raise_store_error(exc)
        grouped: dict[str, list[Any]] = {}
        for row in rows:
            grouped.setdefault(row.purpose, []).append(row)
        return {
            "bindings": [
                _binding_group(purpose, grouped[purpose])
                for purpose in sorted(grouped)
            ]
        }

    @router.get("/bindings/{purpose}")
    def get_binding(purpose: str, request: Request, response: Response):
        owner = _provider_owner(request)
        if purpose not in _PURPOSES:
            raise HTTPException(404, "Unknown provider route purpose")
        try:
            rows = store.get_route_bindings_for_purpose(
                owner=owner,
                purpose=purpose,
            )
        except ProviderStoreError as exc:
            _raise_store_error(exc)
        body = _binding_group(purpose, rows)
        response.headers["ETag"] = _etag(body["revision"])
        return body

    @router.put("/bindings/{purpose}")
    def put_binding(
        purpose: str,
        payload: RouteBindingPut,
        request: Request,
        if_match: Optional[str] = Header(default=None, alias="If-Match"),
        idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key"),
    ):
        owner = _provider_owner(request, write=True)
        if purpose not in _PURPOSES:
            raise HTTPException(404, "Unknown provider route purpose")
        expected = _if_match(if_match)
        operation = f"bindings.put:{purpose}"
        material = _request_material(payload)
        material["expected_revision"] = expected
        try:
            key, digest, replay = _begin_idempotent(
                store,
                owner=owner,
                operation=operation,
                key_header=idempotency_key,
                payload=material,
            )
            if replay is not None:
                return _mutation_response(replay, replayed=True)
            route_ids = [entry.model_route_id for entry in payload.routes]
            if len(route_ids) != len(set(route_ids)):
                raise HTTPException(422, "Route binding cannot contain duplicates")
            required_operations = _PURPOSE_OPERATIONS[purpose]
            for route_id in route_ids:
                route = store.get_model_route(
                    owner=owner,
                    model_route_id=route_id,
                )
                if not required_operations.intersection(route.operations or ()):
                    raise HTTPException(
                        422,
                        f"Model route {route_id} does not support the {purpose} purpose",
                    )
            rows = store.put_route_binding(
                owner=owner,
                purpose=purpose,
                model_route_ids=route_ids,
                enabled_by_route={
                    entry.model_route_id: entry.enabled for entry in payload.routes
                },
                expected_revision=expected,
            )
            body = _binding_group(purpose, rows)
            return _record_response(
                store,
                owner=owner,
                operation=operation,
                key=key,
                digest=digest,
                status_code=200,
                body=body,
                resource_id=purpose,
            )
        except ProviderStoreError as exc:
            _raise_store_error(exc)

    @router.delete("/bindings/{purpose}")
    def delete_binding(
        purpose: str,
        request: Request,
        if_match: Optional[str] = Header(default=None, alias="If-Match"),
        idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key"),
    ):
        owner = _provider_owner(request, write=True)
        if purpose not in _PURPOSES:
            raise HTTPException(404, "Unknown provider route purpose")
        expected = _if_match(if_match)
        operation = f"bindings.delete:{purpose}"
        try:
            key, digest, replay = _begin_idempotent(
                store,
                owner=owner,
                operation=operation,
                key_header=idempotency_key,
                payload={"expected_revision": expected},
            )
            if replay is not None:
                return _mutation_response(replay, replayed=True)
            store.delete_route_binding(
                owner=owner,
                purpose=purpose,
                expected_revision=expected,
            )
            body = {"purpose": purpose, "status": "deleted", "revision": expected + 1}
            return _record_response(
                store,
                owner=owner,
                operation=operation,
                key=key,
                digest=digest,
                status_code=200,
                body=body,
                resource_id=purpose,
            )
        except ProviderStoreError as exc:
            _raise_store_error(exc)

    @router.get("/share-recipients")
    def list_share_recipients(request: Request):
        # Recipient discovery is part of the write workflow.  Requiring write
        # scope keeps API tokens that can only inspect providers from also
        # enumerating local user identities.
        owner = _provider_owner(request, write=True)
        return {
            "recipients": [
                {"username": username}
                for username in _share_recipient_usernames(request, owner=owner)
            ]
        }

    @router.put("/models/{model_route_id}/shares/{recipient}")
    def set_model_share(
        model_route_id: str,
        recipient: str,
        payload: ModelShareToggle,
        request: Request,
        idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key"),
    ):
        owner = _provider_owner(request, write=True)
        normalized_recipient = str(recipient or "").strip().lower()
        if not normalized_recipient or len(normalized_recipient) > 128:
            raise HTTPException(422, "Provider share recipient is invalid")
        if normalized_recipient == owner:
            raise HTTPException(422, "Provider share recipient must differ from owner")
        _known_recipient(request, normalized_recipient)
        operation = f"shares.model:{model_route_id}:{normalized_recipient}"
        material = _request_material(payload)
        try:
            # Resolve the connection from the owner-scoped route.  The browser
            # never supplies a source connection or selector that could widen
            # the grant.
            model_route = store.get_model_route(
                owner=owner,
                model_route_id=model_route_id,
            )
            key, digest, replay = _begin_idempotent(
                store,
                owner=owner,
                operation=operation,
                key_header=idempotency_key,
                payload=material,
            )
            if replay is not None:
                return _mutation_response(replay, replayed=True)
            row = store.set_model_share(
                owner=owner,
                recipient=normalized_recipient,
                connection_id=model_route.connection_id,
                model_route_id=model_route.id,
                enabled=payload.enabled,
            )
            active_row = row if row is not None and row.state == "active" else None
            body = {
                "enabled": bool(payload.enabled),
                "share": _owned_share_json(active_row) if active_row is not None else None,
            }
            return _record_response(
                store,
                owner=owner,
                operation=operation,
                key=key,
                digest=digest,
                status_code=200,
                body=body,
                resource_id=(row.id if row is not None else model_route.id),
            )
        except ProviderStoreError as exc:
            _raise_store_error(exc)

    @router.get("/shares")
    def list_owned_shares(
        request: Request,
        include_revoked: bool = Query(default=False),
    ):
        owner = _provider_owner(request)
        try:
            rows = store.list_share_grants(
                owner=owner,
                include_revoked=include_revoked,
            )
        except ProviderStoreError as exc:
            _raise_store_error(exc)
        return {"shares": [_owned_share_json(row) for row in rows]}

    @router.get("/shares/received")
    def list_received_shares(request: Request):
        recipient = _provider_owner(request)
        try:
            projections = store.received_share_projections(recipient=recipient)
            result = [_received_share_json(item) for item in projections]
        except ProviderStoreError as exc:
            _raise_store_error(exc)
        return {"shares": result}

    @router.get("/shares/received/{grant_id}")
    def get_received_share(grant_id: str, request: Request, response: Response):
        recipient = _provider_owner(request)
        try:
            projections = store.received_share_projections(
                recipient=recipient,
                grant_id=grant_id,
            )
            projection = projections[0]
            body = _received_share_json(projection)
        except ProviderStoreError as exc:
            _raise_store_error(exc)
        response.headers["ETag"] = _etag(projection.grant.revision)
        return body

    @router.get("/shares/{grant_id}")
    def get_owned_share(grant_id: str, request: Request, response: Response):
        owner = _provider_owner(request)
        try:
            row = store.get_share_grant(owner=owner, grant_id=grant_id)
        except ProviderStoreError as exc:
            _raise_store_error(exc)
        response.headers["ETag"] = _etag(row.revision)
        return _owned_share_json(row)

    @router.post("/shares")
    def create_share(
        payload: ShareCreate,
        request: Request,
        idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key"),
    ):
        owner = _provider_owner(request, write=True)
        recipient = payload.recipient.strip().lower()
        _known_recipient(request, recipient)
        operation = "shares.create"
        try:
            key, digest, replay = _begin_idempotent(
                store,
                owner=owner,
                operation=operation,
                key_header=idempotency_key,
                payload=_request_material(payload),
            )
            if replay is not None:
                return _mutation_response(replay, replayed=True)
            row = store.create_share_grant(
                owner=owner,
                recipient=recipient,
                connection_id=payload.connection_id,
                account_selector=AccountSelector.parse(payload.account_selector),
                model_selector=ModelSelector.parse(payload.model_selector),
                disclosure_fields=payload.disclosure_fields,
                label=payload.label,
                grant_id=store.deterministic_resource_id(
                    prefix="psg",
                    owner=owner,
                    operation=operation,
                    idempotency_key=key,
                ),
            )
            body = _owned_share_json(row)
            return _record_response(
                store,
                owner=owner,
                operation=operation,
                key=key,
                digest=digest,
                status_code=201,
                body=body,
                resource_id=row.id,
            )
        except ProviderStoreError as exc:
            _raise_store_error(exc)

    @router.patch("/shares/{grant_id}")
    def update_share(
        grant_id: str,
        payload: ShareUpdate,
        request: Request,
        if_match: Optional[str] = Header(default=None, alias="If-Match"),
        idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key"),
    ):
        owner = _provider_owner(request, write=True)
        expected = _if_match(if_match)
        operation = f"shares.update:{grant_id}"
        material = _request_material(payload)
        material["expected_revision"] = expected
        try:
            key, digest, replay = _begin_idempotent(
                store,
                owner=owner,
                operation=operation,
                key_header=idempotency_key,
                payload=material,
            )
            if replay is not None:
                return _mutation_response(replay, replayed=True)
            row = store.replace_share_selectors(
                owner=owner,
                grant_id=grant_id,
                expected_revision=expected,
                account_selector=AccountSelector.parse(payload.account_selector),
                model_selector=ModelSelector.parse(payload.model_selector),
                disclosure_fields=payload.disclosure_fields,
            )
            body = _owned_share_json(row)
            return _record_response(
                store,
                owner=owner,
                operation=operation,
                key=key,
                digest=digest,
                status_code=200,
                body=body,
                resource_id=row.id,
            )
        except ProviderStoreError as exc:
            _raise_store_error(exc)

    @router.delete("/shares/{grant_id}")
    def revoke_share(
        grant_id: str,
        request: Request,
        if_match: Optional[str] = Header(default=None, alias="If-Match"),
        idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key"),
    ):
        owner = _provider_owner(request, write=True)
        expected = _if_match(if_match)
        operation = f"shares.revoke:{grant_id}"
        try:
            key, digest, replay = _begin_idempotent(
                store,
                owner=owner,
                operation=operation,
                key_header=idempotency_key,
                payload={"expected_revision": expected},
            )
            if replay is not None:
                return _mutation_response(replay, replayed=True)
            row = store.revoke_share_grant(
                owner=owner,
                grant_id=grant_id,
                expected_revision=expected,
            )
            body = {
                "id": row.id,
                "status": "revoked",
                "revision": int(row.revision),
            }
            return _record_response(
                store,
                owner=owner,
                operation=operation,
                key=key,
                digest=digest,
                status_code=200,
                body=body,
                resource_id=row.id,
            )
        except ProviderStoreError as exc:
            _raise_store_error(exc)

    @router.post("/shares/{grant_id}/accept")
    def accept_share(grant_id: str, request: Request):
        _provider_owner(request, write=True)
        del grant_id
        raise HTTPException(410, "Provider shares are active immediately")

    @router.put("/shares/{grant_id}/preferred-account")
    def prefer_shared_account(grant_id: str, request: Request):
        _provider_owner(request, write=True)
        del grant_id
        raise HTTPException(410, "Shared-account preferences are no longer supported")

    # The picker/default compatibility projection lives beside the normalized
    # provider API so importing this module cannot revive the retired
    # Compatibility projections for older browser consumers. These responses contain public selectors
    # only; all execution continues to use stable provider-model route IDs.
    compatibility = APIRouter(tags=["managed-model-catalog"])

    def _chat_owner(request: Request) -> str:
        state = getattr(request, "state", None)
        if getattr(state, "api_token", False):
            if getattr(state, "api_token_client_kind", "api") != "api":
                raise HTTPException(403, "This client token cannot use the chat API")
            owner = str(getattr(state, "api_token_owner", "") or "").strip().lower()
            scopes = set(getattr(state, "api_token_scopes", ()) or ())
            if not owner:
                raise HTTPException(403, "API token has no owner")
            if "chat" not in scopes:
                raise HTTPException(403, "API token is not scoped for chat")
            return owner
        admitted = require_user(request)
        return str(effective_user(request) or admitted or "").strip().lower()

    def _allowed_model_ids(request: Request, owner: str) -> Optional[frozenset[str]]:
        if not owner:
            return None
        auth_manager = getattr(getattr(request.app, "state", None), "auth_manager", None)
        get_privileges = getattr(auth_manager, "get_privileges", None)
        if not callable(get_privileges):
            return None
        privileges = get_privileges(owner)
        if not privileges or privileges.get("block_all_models"):
            return frozenset()
        raw = privileges.get("allowed_models")
        allowed = (
            frozenset(str(item) for item in raw)
            if isinstance(raw, list)
            else frozenset()
        )
        if privileges.get("allowed_models_restricted") or allowed:
            return allowed
        return None

    def _project_group(endpoint_id: str, routes: list[Any]) -> dict[str, Any]:
        first = routes[0]
        model_ids = [route.provider_model_id for route in routes]
        displays = [route.display_name for route in routes]
        catalog: list[dict[str, Any]] = []
        for route in routes:
            provider_display_name = provider_family_display_name(route.family_id)
            capabilities = dict(route.capabilities or {})
            capabilities["chat"] = True
            request_options = project_request_options(
                capabilities,
                catalog_revision=route.catalog_revision,
                source=("account-discovery" if "reasoning_effort" in capabilities else "engine-catalog"),
            )
            capabilities["request_options_state"] = "supported" if request_options else "undeclared"
            if request_options:
                capabilities["request_options"] = request_options
            record = build_model_catalog(
                endpoint_id=endpoint_id,
                endpoint_url=MANAGED_ENGINE_PUBLIC_URL,
                model_ids=[route.provider_model_id],
                primary_ids=[route.provider_model_id],
                display_names={route.provider_model_id: route.display_name},
                families={
                    route.provider_model_id: provider_display_name
                },
                discovered=True,
                entitled=True,
                capabilities=capabilities,
            )[0]
            record.update(
                {
                    "provider_model_id": route.provider_model_id,
                    "provider_family_id": route.family_id,
                    "provider_display_name": provider_display_name,
                    "operations": list(route.operations),
                    "catalog_revision": route.catalog_revision,
                    "option_route_id": request_option_route_id(route),
                }
            )
            if route.shared:
                # Used only for privilege filtering, then stripped below.
                record["_provider_model_route_id"] = route.model_route_id
            else:
                record["provider_model_route_id"] = route.model_route_id
            catalog.append(record)

        endpoint_name = first.share_label or first.connection_label
        if first.disclosed_owner:
            endpoint_name = f"{endpoint_name} · shared by {first.disclosed_owner}"
        item: dict[str, Any] = {
            "host": "managed",
            "port": 0,
            "url": MANAGED_ENGINE_PUBLIC_URL,
            "models": model_ids,
            "models_display": displays,
            "models_extra": [],
            "models_extra_display": [],
            "catalog": catalog,
            "endpoint_id": endpoint_id,
            "endpoint_name": endpoint_name,
            "category": (
                "api"
                if first.shared
                else ("local" if first.connection_kind == "local" else "api")
            ),
            "endpoint_kind": "shared" if first.shared else first.connection_kind,
            "model_type": "llm",
            "virtual": True,
            "read_only": True,
            "actions": ["configure_providers"],
            "settings_tab": "providers",
        }
        if first.shared:
            item.update({
                "shared": True,
                "share_id": first.provider_grant_id,
                "share_label": first.share_label,
                "shared_by": first.disclosed_owner,
                "provider_family_id": first.family_id,
                "provider_display_name": provider_family_display_name(first.family_id),
            })
            if first.disclosed_owner:
                item["shared_by"] = first.disclosed_owner
        else:
            item.update(
                {
                    "connection_id": first.connection_id,
                    "billing_lane": first.billing_lane,
                    "family_id": first.family_id,
                }
            )
        return item

    def _filter_allowed_models(
        result: dict[str, Any],
        allowed: Optional[frozenset[str]],
    ) -> dict[str, Any]:
        if allowed is None:
            return result
        visible_items = []
        for item in result["items"]:
            records = [
                record
                for record in item.get("catalog", ())
                if record.get("model_id") in allowed
                or record.get("provider_model_route_id") in allowed
                or record.get("_provider_model_route_id") in allowed
            ]
            visible_ids = {
                str(record["model_id"])
                for record in records
                if record.get("model_id")
            }
            pairs = list(zip(item.get("models", ()), item.get("models_display", ())))
            item["models"] = [model for model, _display in pairs if model in visible_ids]
            item["models_display"] = [
                display for model, display in pairs if model in visible_ids
            ]
            item["catalog"] = records
            if records:
                visible_items.append(item)
        result["items"] = visible_items
        return result

    async def _refresh_inventory_account_once(
        *,
        owner: str,
        account: Any,
        connection: Any,
        request: Request,
        force: bool = False,
    ) -> str:
        key = _inventory_cache_key(owner=owner, connection=connection, account=account)
        now = time.monotonic()
        with _ACCOUNT_INVENTORY_LOCK:
            previous = _ACCOUNT_INVENTORY_CACHE.get(key)
        if (
            not force
            and previous is not None
            and now - float(previous.get("observed_at", 0.0))
            < _ACCOUNT_INVENTORY_FRESHNESS_SECONDS
        ):
            return str(previous["status"])
        try:
            access = store.credential_access(owner=owner, account_id=account.id)
            result = await control.call(
                request=request,
                owner=owner,
                method="_openclank/provider-control/v1/account/validate",
                payload=dict(allowCredentialRefresh=True, **_account_validation_wire(
                    connection=connection,
                    auth_method=account.auth_method,
                    acquisition_method=account.acquisition_method,
                    credential=access.credentials,
                    account_id=account.id,
                    credential_revision=int(access.credential_version),
                )),
            )
            _validate_account_auth_result(result, connection=connection)
            discovery = _validated_account_discovery(
                result,
                account_id=account.id,
                credential_revision=int(access.credential_version),
            )
            revision = int(access.credential_version)
            if result.get("credentialRefreshed") is True:
                rotated = result["credential"]
                if (connection.family_id != "anthropic" or connection.billing_lane != "subscription"
                        or rotated.get("type") != "oauth" or not rotated.get("access") or not rotated.get("refresh")
                        or (access.credentials.get("accountId") and rotated.get("accountId") != access.credentials["accountId"])):
                    raise ProviderConflict("Invalid refreshed provider identity")
                saved = store.replace_account_credential(owner=owner, account_id=account.id,
                    expected_credential_version=revision, credentials=rotated)
                revision = int(saved.credential_version)
            declared_routes = _engine_model_routes(
                store=store,
                owner=owner,
                operation=f"inventory.refresh:{connection.id}:{account.id}",
                idempotency_key=f"{account.id}:{access.credential_version}",
                raw_routes=result.get("modelRoutes") or [],
            )
            store.reconcile_account_discovery(
                owner=owner,
                account_id=account.id,
                model_routes=declared_routes,
                status=str(discovery["status"]),
                authoritative=bool(discovery.get("authoritative")),
                error_code=_discovery_error_code(discovery),
                provenance=discovery.get("provenance"),
                expected_credential_revision=revision,
            )
            with _ACCOUNT_INVENTORY_LOCK:
                _ACCOUNT_INVENTORY_CACHE[key] = {
                    "observed_at": time.monotonic(),
                    "status": str(discovery["status"]),
                    "last_good": str(discovery["status"]) == "complete",
                }
            return str(discovery["status"])
        except Exception as exc:
            with _ACCOUNT_INVENTORY_LOCK:
                _ACCOUNT_INVENTORY_CACHE[key] = {
                    "observed_at": time.monotonic(),
                    "status": "unavailable",
                    "last_good": bool(previous and previous.get("last_good")),
                }
            logger.info(
                "Managed account inventory refresh unavailable for %s: %s",
                account.id,
                type(exc).__name__,
            )

            return "unavailable"

    async def _refresh_inventory_account(
        *,
        owner: str,
        account: Any,
        connection: Any,
        request: Request,
        force: bool = False,
    ) -> str:
        key = _inventory_cache_key(owner=owner, connection=connection, account=account)
        with _ACCOUNT_INVENTORY_LOCK:
            flight = _ACCOUNT_INVENTORY_FLIGHTS.get(key)
            if flight is None or flight.done():
                flight = asyncio.create_task(
                    _refresh_inventory_account_once(
                        owner=owner,
                        account=account,
                        connection=connection,
                        request=request,
                        force=force,
                    )
                )
                _ACCOUNT_INVENTORY_FLIGHTS[key] = flight
        try:
            return await asyncio.shield(flight)
        finally:
            with _ACCOUNT_INVENTORY_LOCK:
                if _ACCOUNT_INVENTORY_FLIGHTS.get(key) is flight:
                    _ACCOUNT_INVENTORY_FLIGHTS.pop(key, None)

    async def _refresh_inventory_on_open(
        *,
        owner: str,
        request: Request,
        force: bool = False,
    ) -> None:
        tasks = [
            _refresh_inventory_account(
                owner=owner,
                account=account,
                connection=connection,
                request=request,
                force=force,
            )
            for connection in store.list_connections(owner=owner) if connection.enabled
            for account in store.list_accounts(owner=owner, connection_id=connection.id) if account.enabled
        ]
        await asyncio.gather(*tasks)

    @compatibility.get("/api/models")
    async def api_models(
        request: Request,
        refresh: bool = False,
        background: bool = False,
    ):
        """Project the owner-visible normalized chat catalog without probes."""
        del background
        owner = _chat_owner(request)
        try:
            if refresh:
                await _refresh_inventory_on_open(owner=owner, request=request, force=True)
            own_routes, shared_routes = list_chat_routes(
                owner,
                provider_store=store,
            )
        except Exception as exc:
            logger.error("Normalized provider catalogue failed closed: %s", exc)
            raise HTTPException(503, "Provider catalogue is unavailable") from exc

        result: dict[str, Any] = {"hosts": [], "items": []}
        for endpoint_id, routes in sorted(group_chat_routes(own_routes).items()):
            result["items"].append(_project_group(endpoint_id, routes))
        for endpoint_id, routes in sorted(group_chat_routes(shared_routes).items()):
            result["items"].append(_project_group(endpoint_id, routes))
        _filter_allowed_models(result, _allowed_model_ids(request, owner))
        for item in result["items"]:
            for record in item.get("catalog", ()):
                record.pop("_provider_model_route_id", None)
        return result

    @compatibility.get("/api/default-chat")
    def get_default_chat(request: Request):
        from src.openclank.modality_facade import _configured_text_route
        from src.openclank.operation_router import ManagedOperationUnavailable

        owner = _chat_owner(request)
        normalized_owner = normalized_provider_owner(owner)
        allowed = _allowed_model_ids(request, owner)
        try:
            own_routes, shared_routes = list_chat_routes(
                owner,
                provider_store=store,
            )
            routes = [*own_routes, *shared_routes]
            configured = _configured_text_route(
                owner=normalized_owner,
                purpose="chat",
                operation="chat.complete",
                provider_store=store,
            )
            by_route_id = {route.model_route_id: route for route in routes}
            bindings = [
                binding
                for binding in store.list_route_bindings(owner=normalized_owner)
                if binding.purpose == "chat"
            ]
        except (ProviderStoreError, ChatRouteUnavailable, ManagedOperationUnavailable) as exc:
            logger.error("Normalized default chat lookup failed: %s", exc)
            raise HTTPException(503, "Default provider route is unavailable") from exc

        ordered = []
        seen = set()
        if configured is not None:
            ordered.append(configured)
            seen.add(configured.model_route_id)
        for binding in bindings:
            route = by_route_id.get(binding.model_route_id)
            if not binding.enabled or route is None or route.model_route_id in seen:
                continue
            ordered.append(route)
            seen.add(route.model_route_id)
        ordered.extend(route for route in routes if route.model_route_id not in seen)
        for route in ordered:
            if allowed is not None and (
                route.provider_model_id not in allowed
                and route.model_route_id not in allowed
            ):
                continue
            return {
                "endpoint_id": route.public_endpoint_id,
                "endpoint_url": MANAGED_ENGINE_PUBLIC_URL,
                "model": route.provider_model_id,
            }
        return {"endpoint_id": "", "endpoint_url": "", "model": ""}

    @compatibility.get("/api/tools")
    async def list_tools(request: Request):
        """Return the authoritative executable tool-tag set."""
        disabled = set(load_settings().get("disabled_tools", ()))
        tools = []
        for tag in sorted(TOOL_TAGS):
            requested = tag not in disabled
            tools.append({
                "id": tag,
                "source": "first-party-adapter",
                "enabled": requested,
                "requested_enabled": requested,
                "effective_enabled": requested,
                "availability": "disabled" if not requested else "available",
                "reason": "Disabled by administrator" if not requested else "Registered first-party adapter",
            })
        native_ids = []
        native_metadata = {}
        native_status = "engine-stopped"
        supervisor = getattr(getattr(request, "app", None), "state", None)
        supervisor = getattr(supervisor, "mimo_supervisor", None)
        try:
            owner = _provider_owner(request)
            worker = supervisor.worker_for_owner(owner) if supervisor is not None else None
            if worker is not None and worker.is_alive():
                async with worker.internal_http_client(timeout=2.0) as client:
                    response = await client.get("/experimental/tool/metadata")
                    if response.is_success:
                        payload = response.json()
                        rows = payload if isinstance(payload, list) else payload.get("tools", [])
                        native_metadata = {str(item.get("id")): item for item in rows if isinstance(item, dict) and str(item.get("id", "")).strip()}
                        native_ids = sorted(native_metadata)
                        native_status = "fresh"
                    else:
                        response = await client.get("/experimental/tool/ids")
                        if response.is_success:
                            payload = response.json()
                            native_ids = sorted({str(item) for item in (payload if isinstance(payload, list) else payload.get("ids", [])) if str(item).strip()})
                            native_status = "ids-only"
                        else:
                            native_status = "engine-query-failed"
        except Exception:
            native_status = "engine-query-unavailable"
        known = {item["id"] for item in tools}
        for tag in native_ids:
            if tag in known:
                continue
            requested = tag not in disabled
            metadata = native_metadata.get(tag, {})
            registered = metadata.get("registered", True)
            effective = bool(requested and registered)
            tools.append({
                "id": tag,
                "source": metadata.get("source", "native-registry"),
                "enabled": requested,
                "requested_enabled": requested,
                "effective_enabled": effective,
                "availability": "disabled" if not requested else "available" if registered else "unavailable",
                "reason": "Disabled by administrator" if not requested else metadata.get("reason", "Registered by the running native engine") if registered else "Not registered by the running native engine",
            })
        tools.sort(key=lambda item: item["id"])
        return {
            "tools": tools,
            "catalog": {
                "source": "native-registry+first-party-adapter",
                "freshness": native_status,
                "native_registry": "engine-owned; IDs are queried from the running managed engine",
            },
        }

    @compatibility.post("/api/tools")
    def update_tools(body: ToolsUpdate, request: Request):
        require_admin(request)
        settings = load_settings()
        settings["disabled_tools"] = body.disabled
        save_settings(settings)
        return {"ok": True, "disabled": body.disabled}

    combined = APIRouter()
    combined.include_router(router)
    combined.include_router(compatibility)
    return combined


__all__ = ["purge_owner_provider_flows", "setup_provider_v1_routes"]
