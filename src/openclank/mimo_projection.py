"""Secret-free, owner-scoped provider catalog for managed Open Clank workers.

The managed engine receives provider *topology* here.  Credentials and account
selection are intentionally absent: a worker obtains an exact, capability-bound
credential lease from the provider-store callbacks for each root operation.

This module retains the old projection function names because the supervisor's
generation-swap machinery still consumes them.  Projection generations are now
pure functions of normalized provider rows; the retired ``MimoProjectionState``
table is neither read nor written.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Mapping
from urllib.parse import urlsplit, urlunsplit

import core.database as core_database
from core.provider_models import (
    ProviderAccount,
    ProviderAccountEntitlement,
    ProviderAccountHealth,
    ProviderConnection,
    ProviderModelRoute,
    ProviderRouteBinding,
    ProviderShareGrant,
)
from src.openclank.provider_store import _KEYLESS_CONNECTION_KINDS


LOCAL_INSTALLATION_OWNER = "local-installation"

# These translations remain for historical session display/lookup only.  They
# are not used to invent a provider or route in the normalized projection.
_RUNTIME_TO_PUBLIC_MODEL = {
    "mimo/mimo-auto": "xiaomi/mimo-auto",
}
_PUBLIC_TO_RUNTIME_MODEL = {
    public: runtime for runtime, public in _RUNTIME_TO_PUBLIC_MODEL.items()
}

_RUNTIME_OPERATIONS = frozenset(
    {
        "chat.stream",
        "chat.complete",
        "vision.describe",
        "image.generate",
        "image.edit",
        "image.inpaint",
        "image.img2img",
        "image.upscale",
        "image.denoise",
        "image.segment",
        "image.remove_background",
        "image.restore_face",
        "audio.synthesize",
        "audio.transcribe",
        "embeddings.create",
    }
)
_SMALL_MODEL_OPERATIONS = frozenset({"chat.stream", "chat.complete"})

# Values match the customized engine's managed adapter dispatcher.  In
# particular, a stable connection ID is the runtime provider ID; the family ID
# must not be reused because one owner may have several independent routes and
# billing lanes for the same family.
_ADAPTER_NPM = {
    "anthropic-messages": "@ai-sdk/anthropic",
    "copilot-chat": "@ai-sdk/github-copilot",
    "google-generative-ai": "@ai-sdk/google",
    "google-vertex": "@ai-sdk/google-vertex",
    "mimo-native": "@ai-sdk/openai-compatible",
    "models-dev-anthropic": "@ai-sdk/anthropic",
    "models-dev-openai-compatible": "@ai-sdk/openai-compatible",
    "ollama": "@ai-sdk/openai-compatible",
    "xai-responses": "@ai-sdk/xai",
}

# Pre-release provider migration plans used descriptive labels rather than the
# managed engine's frozen family/adapter IDs. Keep those already-written rows
# bootable while new archive replays emit canonical identities directly.
_MIGRATED_RUNTIME_IDENTITIES = {
    ("anthropic", "anthropic"): ("anthropic", "anthropic-messages"),
    ("openrouter", "openrouter"): ("openrouter", "openai-chat"),
    ("copilot", "copilot"): ("github-copilot", "copilot-chat"),
    ("openai", "chatgpt-codex"): ("openai", "openai-responses"),
    ("openai", "openai"): ("openai", "openai-responses"),
    ("xai", "xai"): ("xai", "xai-responses"),
    ("xiaomi", "xiaomi"): ("xiaomi", "mimo-native"),
    ("deepseek", "deepseek"): ("deepseek", "openai-chat"),
    ("compatible", "openai-compatible"): ("openai-compatible", "openai-chat"),
    ("local", "openai-compatible"): ("openai-compatible", "openai-chat"),
}

_DEFAULT_APIS = {
    ("anthropic", "default"): "https://api.anthropic.com/v1",
    ("deepseek", "default"): "https://api.deepseek.com",
    ("github-copilot", "default"): "https://api.githubcopilot.com",
    ("google", "google-generative-ai"): "https://generativelanguage.googleapis.com",
    ("ollama", "default"): "http://127.0.0.1:11434/v1",
    ("openai", "default"): "https://api.openai.com/v1",
    ("openai", "subscription"): "https://chatgpt.com/backend-api/codex",
    ("openrouter", "default"): "https://openrouter.ai/api/v1",
    ("xai", "default"): "https://api.x.ai/v1",
    ("xiaomi", "default"): "https://api.xiaomimimo.com/v1",
}

# Provider settings pass semantic validation in the managed engine before
# persistence.  This second, host-side filter is defense in depth: even a
# malformed/imported row cannot put obvious credential material in worker
# config, logs, or a projection fingerprint.
_SECRET_SETTING_NAMES = frozenset(
    {
        "apikey",
        "accesstoken",
        "refreshtoken",
        "token",
        "secret",
        "clientsecret",
        "password",
        "credential",
        "credentials",
        "authorization",
        "cookie",
        "setcookie",
    }
)
_SDK_SETTING_ALIASES = {
    "enterprise_url": "enterpriseUrl",
    "enterpriseUrl": "enterpriseUrl",
    "resource_name": "resourceName",
    "resourceName": "resourceName",
    "project": "project",
    "location": "location",
    "region": "region",
    "set_cache_key": "setCacheKey",
    "setCacheKey": "setCacheKey",
    "timeout": "timeout",
    "header_timeout": "headerTimeout",
    "headerTimeout": "headerTimeout",
    "chunk_timeout": "chunkTimeout",
    "chunkTimeout": "chunkTimeout",
}


class ProjectionConfigurationError(ValueError):
    """A live chat route cannot be represented by the managed engine."""


def public_native_model_id(model_id: str) -> str:
    """Translate a historical private runtime ID to its display ID."""

    value = str(model_id or "").strip()
    return _RUNTIME_TO_PUBLIC_MODEL.get(value, value)


def runtime_native_model_id(model_id: str) -> str:
    """Translate a historical display ID for old session resolution."""

    value = str(model_id or "").strip()
    return _PUBLIC_TO_RUNTIME_MODEL.get(value, value)


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _owner(value: Any) -> str:
    return str(value or "").strip().lower() or LOCAL_INSTALLATION_OWNER


def _secret_setting_name(value: Any) -> bool:
    normalized = "".join(character for character in str(value).lower() if character.isalnum())
    return normalized in _SECRET_SETTING_NAMES


def _nonsecret_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            str(key): _nonsecret_json(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
            if not _secret_setting_name(key)
        }
    if isinstance(value, (list, tuple)):
        return [_nonsecret_json(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    # Normalized settings are JSON, but fail closed if a malformed ORM value
    # reaches this boundary instead of serializing an object representation.
    return None


def _runtime_identity(connection: ProviderConnection) -> tuple[str, str]:
    identity = (
        str(connection.family_id or "").strip(),
        str(connection.adapter_id or "").strip(),
    )
    return _MIGRATED_RUNTIME_IDENTITIES.get(identity, identity)


def _runtime_kind(connection: ProviderConnection) -> str:
    return {
        "official_api": "official",
        "local_server": "local",
    }.get(str(connection.kind or "").strip(), str(connection.kind or "").strip())


def _npm_for(connection: ProviderConnection) -> str:
    family, adapter = _runtime_identity(connection)
    if adapter == "openai-responses":
        return (
            "@ai-sdk/openai-compatible"
            if family == "openai-compatible"
            else "@ai-sdk/openai"
        )
    if adapter == "openai-chat":
        if family == "openrouter":
            return "@openrouter/ai-sdk-provider"
        if family in {"openai-compatible", "ollama", "deepseek"}:
            return "@ai-sdk/openai-compatible"
        return "@ai-sdk/openai"
    npm = _ADAPTER_NPM.get(adapter)
    if npm is None:
        raise ProjectionConfigurationError(
            f"connection {connection.id} uses unsupported managed adapter {adapter or '(empty)'}"
        )
    return npm


def _default_api(connection: ProviderConnection) -> str | None:
    family, adapter = _runtime_identity(connection)
    lane = str(connection.billing_lane or "").strip()
    if family == "openai" and lane == "subscription":
        return _DEFAULT_APIS[("openai", "subscription")]
    return _DEFAULT_APIS.get((family, adapter)) or _DEFAULT_APIS.get((family, "default"))


def _runtime_api(connection: ProviderConnection) -> str:
    raw = str(connection.normalized_url or _default_api(connection) or "").strip()
    if not raw:
        raise ProjectionConfigurationError(
            f"connection {connection.id} requires a normalized provider URL"
        )
    parsed = urlsplit(raw)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ProjectionConfigurationError(
            f"connection {connection.id} has an invalid normalized provider URL"
        )

    path = parsed.path.rstrip("/")
    family, adapter = _runtime_identity(connection)
    if adapter == "ollama" or family == "ollama":
        if path.endswith("/api") or path.endswith("/v1"):
            path = path.rsplit("/", 1)[0]
        path = f"{path}/v1" if path else "/v1"
    return urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))


def _provider_options(connection: ProviderConnection, api: str) -> dict[str, Any]:
    safe_settings = _nonsecret_json(dict(connection.settings or {}))
    family, adapter = _runtime_identity(connection)
    options: dict[str, Any] = {
        "baseURL": api,
        "_openclankConnectionID": connection.id,
        "_openclankFamilyID": family,
        "_openclankAdapterID": adapter,
        "_openclankBillingLane": connection.billing_lane,
        "_openclankConnectionKind": _runtime_kind(connection),
    }
    if safe_settings:
        options["_openclankSettings"] = safe_settings
    for source, target in _SDK_SETTING_ALIASES.items():
        if source in safe_settings and safe_settings[source] is not None:
            options[target] = safe_settings[source]
    return options


def _model_config(
    connection: ProviderConnection,
    route: ProviderModelRoute,
    *,
    npm: str,
    api: str,
) -> dict[str, Any]:
    capabilities = _nonsecret_json(dict(route.capabilities or {}))
    family, adapter = _runtime_identity(connection)
    model: dict[str, Any] = {
        "id": route.provider_model_id,
        "name": route.display_name or route.provider_model_id,
        "provider": {"npm": npm, "api": api},
        "options": {
            "_openclankConnectionID": connection.id,
            "_openclankModelRouteID": route.id,
            "_openclankFamilyID": family,
            "_openclankAdapterID": adapter,
            "_openclankBillingLane": connection.billing_lane,
            "_openclankOperations": sorted(str(item) for item in (route.operations or ())),
            "_openclankCatalogRevision": int(route.catalog_revision or 0),
        },
    }
    # Copy only fields understood by the engine's provider config schema.  The
    # complete normalized capability object remains host-owned.
    # Lifecycle status stays advisory in that object. Connection-scoped config
    # entries default to active in the engine after host route admission; the
    # config schema does not accept an explicit "active" status.
    for name in (
        "family",
        "release_date",
        "attachment",
        "reasoning",
        "temperature",
        "tool_call",
        "interleaved",
        "cost",
        "limit",
        "modalities",
        "experimental",
        "cachePromptTTL",
    ):
        if name in capabilities:
            model[name] = capabilities[name]
    return model


@dataclass(frozen=True)
class ProjectionSnapshot:
    owner: str
    providers: dict[str, dict]
    small_model: str | None
    fingerprint: str
    # Compatibility fields remain empty.  Consumers must never infer that
    # projection config is a credential transport.
    credential_digests: dict[str, str]
    source_endpoints: dict[str, str]
    native_auth_digest: str | None
    credentials: dict[str, str] = field(repr=False, compare=False)

    @property
    def public_id(self) -> str:
        return self.fingerprint[:12]

    @property
    def source_connections(self) -> dict[str, str]:
        """Normalized spelling for the legacy ``source_endpoints`` field."""

        return self.source_endpoints

    def run_closure(self, provider_id: str, model_id: str) -> str | None:
        """Return the exact nonsecret topology needed for one runtime model."""

        provider = self.providers.get(str(provider_id or ""))
        models = (provider or {}).get("models") or {}
        if provider is None or not isinstance(models, dict) or model_id not in models:
            return None
        return _canonical(
            {
                "provider": {key: value for key, value in provider.items() if key != "models"},
                "model": models[model_id],
                "small_model": self.small_model,
                "source_connection": self.source_endpoints.get(provider_id),
            }
        )


def _route_supports_runtime(
    route: ProviderModelRoute,
    connection: ProviderConnection,
) -> bool:
    """Return whether the engine needs an SDK-provider catalog entry.

    Registered local executors are selected and leased by the managed engine,
    but the actual model is invoked through the capability-bound host broker.
    They therefore must *not* be projected as an AI SDK provider (there is no
    URL, npm adapter, or credential to project).  Remote non-chat modalities do
    need a provider entry so the typed operation router can resolve their SDK
    model.
    """

    operations = {str(item) for item in (route.operations or ())}
    if not _RUNTIME_OPERATIONS.intersection(operations):
        return False
    local_executor = (
        str(connection.family_id or "").strip() == "local-executor"
        or str(connection.adapter_id or "").strip() == "openclank-local-executor"
    )
    if local_executor:
        # A malformed imported local-executor row must never become a chat
        # provider. Returning it here lets `_npm_for` reject that configuration
        # explicitly; valid typed local operations bypass SDK projection.
        return bool(_SMALL_MODEL_OPERATIONS.intersection(operations))
    return True


def _small_model(
    routes: list[ProviderModelRoute],
    bindings: list[ProviderRouteBinding],
) -> str | None:
    route_by_id = {route.id: route for route in routes}
    for binding in bindings:
        if binding.purpose != "utility" or not binding.enabled:
            continue
        route = route_by_id.get(binding.model_route_id)
        if route is not None and _SMALL_MODEL_OPERATIONS.intersection(route.operations or ()):
            return f"{route.connection_id}/{route.provider_model_id}"
    chat_routes = [
        route
        for route in routes
        if _SMALL_MODEL_OPERATIONS.intersection(route.operations or ())
    ]
    if not chat_routes:
        return None
    route = chat_routes[0]
    return f"{route.connection_id}/{route.provider_model_id}"


def build_projection_snapshot(owner: str) -> ProjectionSnapshot:
    """Build a normalized spawn catalog without reading any credential row.

    The recipient's worker executes shared operations, so it needs the granted
    connection/model *topology* even though the source owner retains all
    credentials and account selection. Active grant selectors are applied here
    and again by every host callback; this projection is not authorization.
    """

    normalized_owner = _owner(owner)
    from routes.prefs_routes import _load_for_user
    # Preserve native dynamic defaults when the owner has no explicit override.
    # Loading DEFAULT_SETTINGS here would turn an undefined native reserve into
    # a fixed 20k value before the selected model is known.
    agent_settings = (_load_for_user(normalized_owner) or {}).get("agent_settings", {})
    db = core_database.SessionLocal()
    try:
        own_connections = (
            db.query(ProviderConnection)
            .filter(
                ProviderConnection.owner == normalized_owner,
                ProviderConnection.enabled.is_(True),
                ProviderConnection.deleted_at.is_(None),
            )
            .order_by(ProviderConnection.id)
            .all()
        )
        own_connection_ids = {connection.id for connection in own_connections}
        grants = (
            db.query(ProviderShareGrant)
            .filter(
                ProviderShareGrant.recipient == normalized_owner,
                ProviderShareGrant.state == "active",
            )
            .order_by(ProviderShareGrant.id)
            .all()
        )
        shared_connection_ids = sorted({grant.connection_id for grant in grants})
        shared_connections = (
            db.query(ProviderConnection)
            .filter(
                ProviderConnection.id.in_(shared_connection_ids),
                ProviderConnection.enabled.is_(True),
                ProviderConnection.deleted_at.is_(None),
            )
            .order_by(ProviderConnection.id)
            .all()
            if shared_connection_ids
            else []
        )
        connection_by_id = {
            connection.id: connection
            for connection in (*own_connections, *shared_connections)
        }
        connection_ids = sorted(connection_by_id)
        if connection_ids:
            account_rows = db.query(ProviderAccount).filter(
                ProviderAccount.connection_id.in_(connection_ids),
                ProviderAccount.deleted_at.is_(None),
                ProviderAccount.enabled.is_(True),
            ).all()
            account_ids_by_connection: dict[str, set[str]] = {}
            for account in account_rows:
                account_ids_by_connection.setdefault(account.connection_id, set()).add(account.id)
            entitlement_rows = db.query(ProviderAccountEntitlement).filter(
                ProviderAccountEntitlement.account_id.in_([row.id for row in account_rows])
                if account_rows else ProviderAccountEntitlement.account_id == "__none__",
            ).all()
            account_health = {
                row.account_id: row
                for row in db.query(ProviderAccountHealth).filter(
                    ProviderAccountHealth.account_id.in_([row.id for row in account_rows])
                    if account_rows else ProviderAccountHealth.account_id == "__none__",
                ).all()
            }
            entitled_rows = [
                row
                for row in entitlement_rows
                if row.eligible
                and str(getattr(account_health.get(row.account_id), "state", "healthy"))
                not in {"disabled", "reauth_required", "revoked"}
            ]
            snapshot_connections: set[str] = set()
            entitled_route_ids_by_connection: dict[str, set[str]] = {}
            route_connection = {
                route.id: route.connection_id
                for route in db.query(ProviderModelRoute).filter(
                    ProviderModelRoute.connection_id.in_(connection_ids),
                ).all()
            }
            for entitlement in entitled_rows:
                connection_id = route_connection.get(entitlement.model_route_id)
                if connection_id:
                    snapshot_connections.add(connection_id)
                    entitled_route_ids_by_connection.setdefault(connection_id, set()).add(
                        entitlement.model_route_id
                    )
            for entitlement in entitlement_rows:
                connection_id = route_connection.get(entitlement.model_route_id)
                if connection_id:
                    snapshot_connections.add(connection_id)
            candidate_routes = (
                db.query(ProviderModelRoute)
                .filter(
                    ProviderModelRoute.connection_id.in_(connection_ids),
                    ProviderModelRoute.enabled.is_(True),
                    ProviderModelRoute.visibility == "visible",
                    ProviderModelRoute.deleted_at.is_(None),
                )
                .order_by(
                    ProviderModelRoute.connection_id,
                    ProviderModelRoute.provider_model_id,
                    ProviderModelRoute.id,
                )
                .all()
            )
            own_routes = [
                route
                for route in candidate_routes
                if route.owner == normalized_owner
                and route.connection_id in own_connection_ids
                and (
                    (
                        str(connection_by_id[route.connection_id].kind or "").strip().lower()
                        in _KEYLESS_CONNECTION_KINDS
                        and not account_ids_by_connection.get(route.connection_id)
                    )
                    or (
                        account_ids_by_connection.get(route.connection_id)
                        and route.connection_id in snapshot_connections
                        and route.id in entitled_route_ids_by_connection.get(
                            route.connection_id, set()
                        )
                    )
                )
                and _route_supports_runtime(route, connection_by_id[route.connection_id])
            ]
            shared_route_ids: set[str] = set()
            shared_grant_material: list[dict[str, Any]] = []
            for grant in grants:
                connection = connection_by_id.get(grant.connection_id)
                selector = grant.model_selector or {}
                mode = str(selector.get("mode") or "").strip()
                if (
                    connection is None
                    or connection.owner != grant.owner
                    or connection.billing_lane != grant.billing_lane
                    or mode not in {"all_live_models", "explicit_models"}
                ):
                    continue
                explicit_ids = {
                    str(value).strip()
                    for value in (selector.get("model_route_ids") or ())
                    if str(value).strip()
                }
                selected_account_ids = account_ids_by_connection.get(grant.connection_id, set())
                account_selector = grant.account_selector or {}
                if account_selector.get("mode") == "explicit_accounts":
                    selected_account_ids &= {
                        str(value).strip()
                        for value in (account_selector.get("account_ids") or ())
                    }
                eligible_shared_ids = {
                    route_id
                    for route_id in entitled_route_ids_by_connection.get(grant.connection_id, set())
                    if any(
                        row.account_id in selected_account_ids
                        for row in entitled_rows
                        if row.model_route_id == route_id
                    )
                }
                selected_ids = sorted(
                    route.id
                    for route in candidate_routes
                    if route.owner == grant.owner
                    and route.connection_id == grant.connection_id
                    and (
                        mode == "all_live_models"
                        or route.id in explicit_ids
                    )
                    and (
                        (
                            str(connection.kind or "").strip().lower()
                            in _KEYLESS_CONNECTION_KINDS
                            and not account_ids_by_connection.get(grant.connection_id)
                        )
                        or (
                            account_ids_by_connection.get(grant.connection_id)
                            and grant.connection_id in snapshot_connections
                            and route.id in eligible_shared_ids
                        )
                    )
                    and _route_supports_runtime(route, connection)
                )
                shared_route_ids.update(selected_ids)
                shared_grant_material.append(
                    {
                        "id": grant.id,
                        "revision": int(grant.revision or 0),
                        "connection_id": grant.connection_id,
                        "model_route_ids": selected_ids,
                    }
                )
            shared_routes = [
                route for route in candidate_routes if route.id in shared_route_ids
            ]
            routes = sorted(
                {route.id: route for route in (*own_routes, *shared_routes)}.values(),
                key=lambda route: (
                    route.connection_id,
                    route.provider_model_id,
                    route.id,
                ),
            )
            route_ids = [route.id for route in own_routes]
            bindings = (
                db.query(ProviderRouteBinding)
                .filter(
                    ProviderRouteBinding.owner == normalized_owner,
                    ProviderRouteBinding.purpose == "utility",
                    ProviderRouteBinding.enabled.is_(True),
                    ProviderRouteBinding.model_route_id.in_(route_ids),
                )
                .order_by(ProviderRouteBinding.ordinal, ProviderRouteBinding.id)
                .all()
                if route_ids
                else []
            )
        else:
            own_routes = []
            routes = []
            bindings = []
            shared_route_ids = set()
            shared_grant_material = []
    finally:
        db.close()

    routes_by_connection: dict[str, list[ProviderModelRoute]] = {}
    for route in routes:
        routes_by_connection.setdefault(route.connection_id, []).append(route)

    providers: dict[str, dict] = {}
    for connection_id in sorted(routes_by_connection):
        connection = connection_by_id[connection_id]
        npm = _npm_for(connection)
        api = _runtime_api(connection)
        providers[connection.id] = {
            "name": (
                connection.label or connection.id
                if connection.id in own_connection_ids
                else f"Shared {_runtime_identity(connection)[0]}"
            ),
            "npm": npm,
            "api": api,
            "options": {**_provider_options(connection, api), "_openclankAgentSettings": agent_settings},
            "models": {
                route.provider_model_id: _model_config(
                    connection,
                    route,
                    npm=npm,
                    api=api,
                )
                for route in routes_by_connection[connection_id]
            },
            "only_configured_models": True,
        }

    # A grant supplies explicit-operation topology, not an implicit default.
    # Only an owner-controlled route binding may seed the worker's small model.
    small_model = _small_model(own_routes, bindings)
    sources = {provider_id: provider_id for provider_id in providers}
    material = {
        "owner": normalized_owner,
        "providers": providers,
        "shared_grants": shared_grant_material,
        "source_connections": sources,
        "small_model": small_model,
        "agent_settings": agent_settings,
    }
    fingerprint = hashlib.sha256(_canonical(material).encode("utf-8")).hexdigest()
    return ProjectionSnapshot(
        owner=normalized_owner,
        providers=providers,
        small_model=small_model,
        fingerprint=fingerprint,
        credential_digests={},
        source_endpoints=sources,
        native_auth_digest=None,
        credentials={},
    )


def build_shared_projection_snapshot(access: Any) -> ProjectionSnapshot:
    """Return a fail-closed marker for the retired dedicated-share worker.

    Normalized grants now contribute nonsecret topology to the recipient's
    ordinary snapshot in :func:`build_projection_snapshot`. Credentials and
    account selection still cross only the revalidating host callbacks.
    """

    owner = _owner(getattr(access, "actor_owner", ""))
    material = {
        "owner": owner,
        "legacy_shared_projection": "retired",
        "share_id": str(getattr(access, "share_id", "") or ""),
        "revision": int(getattr(access, "revision", 0) or 0),
    }
    return ProjectionSnapshot(
        owner=owner,
        providers={},
        small_model=None,
        fingerprint=hashlib.sha256(_canonical(material).encode("utf-8")).hexdigest(),
        credential_digests={},
        source_endpoints={},
        native_auth_digest=None,
        credentials={},
    )


def _generation(fingerprint: str) -> int:
    # A deterministic generation removes the need for a legacy mutable table.
    # Sixty bits keeps the value comfortably within signed 64-bit consumers.
    return int(fingerprint[:15], 16) or 1


def reconcile_projection(snapshot: ProjectionSnapshot, *, materializing: bool) -> dict[str, Any]:
    """Describe the desired generation without reading or mutating database state."""

    return {
        "owner": snapshot.owner,
        "desired_fingerprint": snapshot.public_id,
        "generation": _generation(snapshot.fingerprint),
        "status": "pending" if materializing else "not_materialized",
        "last_error_code": None,
    }


def mark_projection(
    owner: str,
    fingerprint: str,
    generation: int,
    *,
    status: str,
    error_code: str | None = None,
) -> None:
    """Compatibility no-op: worker state is owned by the in-memory supervisor."""

    del owner, fingerprint, generation, status, error_code


def projection_public(row: Any) -> dict[str, Any]:
    """Render either a normalized snapshot or an old row-shaped object safely."""

    if isinstance(row, ProjectionSnapshot):
        return reconcile_projection(row, materializing=False)
    fingerprint = str(getattr(row, "desired_fingerprint", "") or "")
    return {
        "owner": str(getattr(row, "owner_id", "") or ""),
        "desired_fingerprint": fingerprint[:12],
        "generation": int(getattr(row, "generation", 0) or 0),
        "status": str(getattr(row, "status", "") or ""),
        "last_error_code": getattr(row, "last_error_code", None),
    }


def safe_additive_delta(old: ProjectionSnapshot, new: ProjectionSnapshot) -> bool:
    """True only when existing route topology is identical and additions are safe."""

    def without_settings(value: object) -> object:
        if isinstance(value, dict):
            return {
                key: without_settings(item)
                for key, item in value.items()
                if key != "_openclankAgentSettings"
            }
        if isinstance(value, list):
            return [without_settings(item) for item in value]
        return value

    if old.owner != new.owner or old.small_model != new.small_model:
        return False
    for provider_id, old_provider in old.providers.items():
        new_provider = new.providers.get(provider_id)
        if new_provider is None:
            return False
        if _canonical(without_settings({key: value for key, value in old_provider.items() if key != "models"})) != _canonical(
            without_settings({key: value for key, value in new_provider.items() if key != "models"})
        ):
            return False
        old_models = old_provider.get("models") or {}
        new_models = new_provider.get("models") or {}
        if not isinstance(old_models, dict) or not isinstance(new_models, dict):
            return False
        for model_id, model in old_models.items():
            if model_id not in new_models or _canonical(without_settings(model)) != _canonical(without_settings(new_models[model_id])):
                return False
    return True
