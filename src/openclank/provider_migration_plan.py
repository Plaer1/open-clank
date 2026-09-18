"""Deterministic legacy-provider to normalized-provider mapping plan."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping
from urllib.parse import urlsplit, urlunsplit

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from core.provider_models import (
    ProviderAccount,
    ProviderAccountEntitlement,
    ProviderBase,
    ProviderConnection,
    ProviderLegacyAlias,
    ProviderModelRoute,
    ProviderRouteBinding,
    ProviderShareGrant,
)
from src.openclank.provider_credentials import (
    CredentialScope,
    credential_fingerprint,
    seal_credential,
)
from src.openclank.provider_migration import ProviderMigrationError
from src.secret_storage import decrypt


_PROVIDER_REFERENCE_COLUMNS = {
    "sessions": {
        "provider_model_route_id": "ix_sessions_provider_model_route_id",
    },
    "scheduled_tasks": {
        "provider_model_route_id": "ix_scheduled_tasks_provider_model_route_id",
    },
    "crew_members": {
        "provider_model_route_id": "ix_crew_members_provider_model_route_id",
    },
    "comparisons": {
        "provider_model_route_a_id": "ix_comparisons_provider_model_route_a_id",
        "provider_model_route_b_id": "ix_comparisons_provider_model_route_b_id",
    },
}


def _stable_id(prefix: str, *parts: object) -> str:
    digest = hashlib.sha256(
        "\0".join(str(part or "") for part in parts).encode("utf-8")
    ).hexdigest()[:32]
    return f"{prefix}_{digest}"


def _owner(value: Any, fallback: str | None) -> str:
    normalized = str(value or fallback or "").strip().lower()
    if not normalized:
        raise ProviderMigrationError("legacy provider record has no deterministic owner")
    return normalized


def _normalized_url(value: Any) -> str | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = urlsplit(raw)
    except ValueError:
        return None
    if not parsed.scheme or not parsed.netloc or not parsed.hostname:
        # Retain legacy scheme-less local addresses, but never retain a query,
        # fragment, or obvious user-info credential as connection identity.
        sanitized = raw.split("#", 1)[0].split("?", 1)[0]
        if "@" in sanitized:
            sanitized = sanitized.rsplit("@", 1)[1]
        return sanitized.rstrip("/")
    host = parsed.hostname.lower()
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    try:
        port = parsed.port
    except ValueError:
        return None
    netloc = f"{host}:{port}" if port is not None else host
    path = parsed.path.rstrip("/")
    for suffix in ("/chat/completions", "/completions", "/models", "/v1/messages"):
        if path.endswith(suffix):
            path = path[: -len(suffix)].rstrip("/")
    return urlunsplit((parsed.scheme.lower(), netloc, path, "", ""))


def _safe_report_url(value: str | None) -> str | None:
    """Return a credential-free URL suitable for migration diagnostics."""

    if not value:
        return None
    try:
        parsed = urlsplit(value)
        if not parsed.scheme or not parsed.hostname:
            return None
        host = parsed.hostname.lower()
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        port = parsed.port
    except ValueError:
        return None
    netloc = f"{host}:{port}" if port is not None else host
    # Paths can themselves carry gateway tokens.  The report needs only the
    # origin to distinguish connection locations safely.
    return urlunsplit((parsed.scheme.lower(), netloc, "", "", ""))


def _family_adapter(
    *,
    base_url: str | None,
    name: str = "",
    provider_hint: str = "",
    endpoint_kind: str = "",
) -> tuple[str, str, str]:
    haystack = " ".join((base_url or "", name, provider_hint)).lower()
    if "anthropic" in haystack:
        return "anthropic", "anthropic-messages", "official"
    if "openrouter" in haystack:
        return "openrouter", "openai-chat", "official"
    if "copilot" in haystack or "github" in provider_hint.lower():
        return "github-copilot", "copilot-chat", "subscription"
    if "chatgpt" in haystack or "backend-api/codex" in haystack:
        return "openai", "openai-responses", "subscription"
    if "api.openai.com" in haystack or provider_hint.lower() == "openai":
        return "openai", "openai-responses", "official"
    if "generativelanguage.googleapis.com" in haystack or "gemini" in haystack:
        return "google", "google-generative-ai", "official"
    if "api.x.ai" in haystack or provider_hint.lower() in {"xai", "x-ai"}:
        return "xai", "xai-responses", "official"
    if "deepseek" in haystack:
        return "deepseek", "openai-chat", "official"
    if "xiaomi" in haystack or provider_hint.lower() in {"mimo", "xiaomi"}:
        return "xiaomi", "mimo-native", "official"
    if "ollama" in haystack or re.search(r"(?:127\.0\.0\.1|localhost):11434", haystack):
        return "ollama", "ollama", "local"
    if endpoint_kind == "local" or re.search(r"(?:127\.0\.0\.1|localhost|\[::1\])", haystack):
        return "openai-compatible", "openai-chat", "local"
    return "openai-compatible", "openai-chat", "custom_gateway"


def _billing_lane(
    *, family: str, kind: str, credential: Mapping[str, Any] | None
) -> str:
    if kind == "local":
        return "local"
    if kind == "subscription" or (credential and credential.get("type") == "oauth"):
        return "subscription"
    if kind == "custom_gateway":
        return "custom"
    return "metered_api"


def _auth_method(credential: Mapping[str, Any]) -> str:
    return "oauth" if credential.get("type") == "oauth" else "api_key"


def _auth_class(billing_lane: str) -> str:
    if billing_lane == "metered_api":
        return "metered"
    return billing_lane


def _json_list(value: Any) -> list[str]:
    if value is None or value == "":
        return []
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError):
            return []
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _credential(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ProviderMigrationError("legacy provider credential is not an object")
    result = dict(value)
    auth_type = result.get("type")
    if auth_type == "api" and str(result.get("key") or ""):
        return result
    if auth_type == "oauth" and str(result.get("access") or ""):
        result.setdefault("refresh", "")
        result.setdefault("expires", 0)
        return result
    if auth_type == "wellknown" and result.get("key") and result.get("token"):
        return result
    raise ProviderMigrationError("legacy provider credential shape is unsupported")


@dataclass(slots=True)
class PlannedConnection:
    id: str
    owner: str
    family_id: str
    adapter_id: str
    kind: str
    billing_lane: str
    label: str
    normalized_url: str | None
    settings: dict[str, Any]
    enabled: bool


@dataclass(slots=True)
class PlannedAccount:
    id: str
    connection_id: str
    owner: str
    label: str
    auth_method: str
    auth_class: str
    sort_order: int
    enabled: bool
    safe_identity: dict[str, Any]
    credential: dict[str, Any] = field(repr=False)


@dataclass(slots=True)
class PlannedRoute:
    id: str
    connection_id: str
    owner: str
    provider_model_id: str
    display_name: str
    operations: list[str]
    capabilities: dict[str, Any]
    visibility: str
    provenance: dict[str, Any]


@dataclass(slots=True)
class PlannedShare:
    id: str
    owner: str
    recipient: str
    connection_id: str
    billing_lane: str
    label: str
    account_ids: list[str]
    model_route_ids: list[str]
    state: str
    accepted: bool
    legacy_share_id: str


@dataclass(slots=True)
class PlannedAlias:
    id: str
    owner: str
    legacy_kind: str
    legacy_id: str
    connection_id: str | None = None
    model_route_id: str | None = None
    share_grant_id: str | None = None
    provenance: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class PlannedReference:
    table: str
    row_id: str
    model_route_id: str
    active: bool
    column: str = "provider_model_route_id"


@dataclass(slots=True)
class PlannedRouteBinding:
    id: str
    owner: str
    purpose: str
    ordinal: int
    model_route_id: str
    enabled: bool = True


@dataclass(slots=True)
class ProviderMappingPlan:
    connections: list[PlannedConnection]
    accounts: list[PlannedAccount]
    routes: list[PlannedRoute]
    entitlements: list[tuple[str, str]]
    shares: list[PlannedShare]
    aliases: list[PlannedAlias]
    references: list[PlannedReference]
    route_bindings: list[PlannedRouteBinding]
    blockers: list[str]
    warnings: list[str]

    def safe_report(self) -> dict[str, Any]:
        return {
            "counts": {
                "connections": len(self.connections),
                "accounts": len(self.accounts),
                "model_routes": len(self.routes),
                "entitlements": len(self.entitlements),
                "share_grants": len(self.shares),
                "legacy_aliases": len(self.aliases),
                "live_references": len(self.references),
                "route_bindings": len(self.route_bindings),
            },
            "connections": [
                {
                    "id": row.id,
                    "owner": row.owner,
                    "family_id": row.family_id,
                    "adapter_id": row.adapter_id,
                    "kind": row.kind,
                    "billing_lane": row.billing_lane,
                    "normalized_url": _safe_report_url(row.normalized_url),
                }
                for row in self.connections
            ],
            "blockers": list(self.blockers),
            "warnings": list(self.warnings),
        }


def _rows(connection: sqlite3.Connection, table: str) -> list[dict[str, Any]]:
    try:
        cursor = connection.execute(f'SELECT * FROM "{table}"')
    except sqlite3.OperationalError:
        return []
    names = [item[0] for item in cursor.description]
    return [dict(zip(names, row)) for row in cursor.fetchall()]


def build_provider_mapping_plan(
    *,
    database_path: Path,
    owner_assignment: str | None,
    settings_path: Path | None = None,
    user_prefs_path: Path | None = None,
    embedding_path: Path | None = None,
) -> ProviderMappingPlan:
    database = sqlite3.connect(str(database_path), timeout=30)
    database.row_factory = sqlite3.Row
    connections: dict[str, PlannedConnection] = {}
    accounts: dict[str, PlannedAccount] = {}
    routes: dict[str, PlannedRoute] = {}
    aliases: list[PlannedAlias] = []
    blockers: list[str] = []
    warnings: list[str] = []
    endpoint_connection: dict[str, str] = {}
    native_connection: dict[tuple[str, str, str], str] = {}
    hidden_native: set[tuple[str, str, str]] = set()
    embedding_route_id: str | None = None
    settings_owner = str(owner_assignment or "").strip().lower()

    def add_account(
        *,
        connection_id: str,
        owner: str,
        label: str,
        auth_method: str,
        auth_class: str,
        sort_order: int,
        enabled: bool,
        safe_identity: Mapping[str, Any],
        credential: dict[str, Any],
    ) -> str:
        """Add one exact connection-scoped credential, deduplicating safely."""

        fingerprint = credential_fingerprint(
            credential,
            owner=owner,
            connection_id=connection_id,
        )
        account_id = _stable_id(
            "pac",
            owner,
            connection_id,
            "credential",
            fingerprint,
        )
        normalized_identity = {
            str(key): str(value)
            for key, value in safe_identity.items()
            if value not in (None, "")
        }
        existing = accounts.get(account_id)
        if existing is None:
            accounts[account_id] = PlannedAccount(
                id=account_id,
                connection_id=connection_id,
                owner=owner,
                label=label,
                auth_method=auth_method,
                auth_class=auth_class,
                sort_order=sort_order,
                enabled=enabled,
                safe_identity=normalized_identity,
                credential=credential,
            )
            return account_id

        # Exact duplicate credentials collapse only because the fingerprint is
        # scoped to this owner and exact connection.  Merge nonsecret display
        # metadata deterministically so database row order cannot affect a plan.
        existing.label = min(existing.label, label)
        existing.sort_order = min(existing.sort_order, sort_order)
        existing.enabled = existing.enabled or enabled
        for key, value in normalized_identity.items():
            current = existing.safe_identity.get(key)
            if current in (None, "") or str(value) < str(current):
                existing.safe_identity[key] = value
        return account_id

    try:
        auth_rows = {row["id"]: row for row in _rows(database, "provider_auth_sessions")}
        sessions = _rows(database, "sessions")
        shares_raw = _rows(database, "model_shares")
        subscriptions = _rows(database, "model_share_subscriptions")
        endpoint_models: dict[str, set[str]] = {}
        for session in sessions:
            endpoint_id = str(session.get("endpoint_id") or "")
            model = str(session.get("model") or "").strip()
            if endpoint_id and model:
                endpoint_models.setdefault(endpoint_id, set()).add(model)
        for share in shares_raw:
            source_id = str(share.get("source_id") or "")
            model = str(share.get("model_id") or "").strip()
            if source_id and model:
                endpoint_models.setdefault(source_id, set()).add(model)

        for endpoint in _rows(database, "model_endpoints"):
            legacy_id = str(endpoint.get("id") or "")
            try:
                owner = _owner(endpoint.get("owner"), owner_assignment)
            except ProviderMigrationError as exc:
                blockers.append(f"endpoint {legacy_id or '(missing id)'}: {exc}")
                continue
            provider_auth = auth_rows.get(endpoint.get("provider_auth_id"))
            credential: dict[str, Any] | None = None
            provider_hint = ""
            if provider_auth:
                provider_hint = str(provider_auth.get("provider") or "")
                access = decrypt(str(provider_auth.get("access_token") or ""))
                refresh = decrypt(str(provider_auth.get("refresh_token") or ""))
                if not access:
                    blockers.append(f"endpoint {legacy_id}: subscription credential is unreadable")
                    continue
                credential = {
                    "type": "oauth",
                    "access": access,
                    "refresh": refresh,
                    "expires": 0,
                }
                if provider_auth.get("account_id"):
                    credential["accountId"] = str(provider_auth["account_id"])
            else:
                key = decrypt(str(endpoint.get("api_key") or ""))
                if key:
                    credential = {"type": "api", "key": key}
            raw_url = str(endpoint.get("base_url") or "").strip()
            url = _normalized_url(raw_url)
            if raw_url and url is None:
                blockers.append(f"endpoint {legacy_id}: base URL is malformed")
                continue
            family, adapter, kind = _family_adapter(
                base_url=url,
                name=str(endpoint.get("name") or ""),
                provider_hint=provider_hint,
                endpoint_kind=str(endpoint.get("endpoint_kind") or ""),
            )
            lane = _billing_lane(family=family, kind=kind, credential=credential)
            official = kind in {"official", "subscription"}
            identity = (owner, family, adapter, url or "", lane, "" if official else legacy_id)
            connection_id = _stable_id("pcn", *identity)
            endpoint_connection[legacy_id] = connection_id
            connections.setdefault(
                connection_id,
                PlannedConnection(
                    id=connection_id,
                    owner=owner,
                    family_id=family,
                    adapter_id=adapter,
                    kind=kind,
                    billing_lane=lane,
                    label=str(endpoint.get("name") or family.title()),
                    normalized_url=url,
                    settings={
                        "model_refresh_mode": endpoint.get("model_refresh_mode"),
                        "model_refresh_interval": endpoint.get("model_refresh_interval"),
                        "model_refresh_timeout": endpoint.get("model_refresh_timeout"),
                    },
                    enabled=bool(endpoint.get("is_enabled", 1)),
                ),
            )
            if credential:
                add_account(
                    connection_id=connection_id,
                    owner=owner,
                    label=str(
                        (provider_auth or {}).get("label")
                        or endpoint.get("name")
                        or "Imported account"
                    ),
                    auth_method=_auth_method(credential),
                    auth_class=_auth_class(lane),
                    sort_order=len([row for row in accounts.values() if row.connection_id == connection_id]),
                    enabled=bool(endpoint.get("is_enabled", 1)),
                    safe_identity={
                        key: str(value)
                        for key, value in {
                            "account_id": (provider_auth or {}).get("account_id"),
                        }.items()
                        if value
                    },
                    credential=credential,
                )
            models = set(_json_list(endpoint.get("cached_models")))
            models.update(_json_list(endpoint.get("pinned_models")))
            models.update(endpoint_models.get(legacy_id, set()))
            hidden = set(_json_list(endpoint.get("hidden_models")))
            model_type = str(endpoint.get("model_type") or "llm")
            for model in sorted(models):
                route_id = _stable_id("pmr", connection_id, model)
                operations = (
                    ["image.generate", "image.edit", "image.img2img"]
                    if model_type == "image"
                    else ["chat.stream", "chat.complete"]
                )
                routes[route_id] = PlannedRoute(
                    id=route_id,
                    connection_id=connection_id,
                    owner=owner,
                    provider_model_id=model,
                    display_name=model,
                    operations=operations,
                    capabilities={},
                    visibility="hidden" if model in hidden else "visible",
                    provenance={"legacy_endpoint_id": legacy_id},
                )
                aliases.append(
                    PlannedAlias(
                        id=_stable_id("pla", owner, "endpoint-model", legacy_id, model),
                        owner=owner,
                        legacy_kind="endpoint-model",
                        legacy_id=f"{legacy_id}:{model}",
                        connection_id=connection_id,
                        model_route_id=route_id,
                    )
                )
            aliases.append(
                PlannedAlias(
                    id=_stable_id("pla", owner, "endpoint", legacy_id),
                    owner=owner,
                    legacy_kind="endpoint",
                    legacy_id=legacy_id,
                    connection_id=connection_id,
                    provenance={"display_name": str(endpoint.get("name") or "")},
                )
            )

        for pref in _rows(database, "mimo_model_prefs"):
            hidden_native.add(
                (
                    _owner(pref.get("owner"), owner_assignment),
                    str(pref.get("provider_id") or ""),
                    str(pref.get("model_id") or ""),
                )
            )

        referenced_native_models: dict[tuple[str, str], set[str]] = {}
        for session in sessions:
            owner = _owner(session.get("owner"), owner_assignment)
            endpoint_id = str(session.get("endpoint_id") or "")
            model = str(session.get("model") or "")
            if endpoint_id.startswith("mimo:") and model:
                provider = endpoint_id.split(":", 1)[1]
                if provider == "auto" and "/" in model:
                    provider, model = model.split("/", 1)
                referenced_native_models.setdefault((owner, provider), set()).add(model)
        for share in shares_raw:
            if str(share.get("source_kind")) != "native":
                continue
            owner = _owner(share.get("owner"), owner_assignment)
            provider = str(share.get("source_id") or "").removeprefix("mimo:")
            referenced_native_models.setdefault((owner, provider), set()).add(
                str(share.get("model_id") or "")
            )

        for store_row in _rows(database, "mimo_auth_store"):
            owner = _owner(store_row.get("owner"), owner_assignment)
            try:
                payload = json.loads(decrypt(str(store_row.get("payload") or "{}")))
            except (TypeError, ValueError):
                blockers.append(f"native provider store for {owner} is malformed")
                continue
            pools: list[dict[str, Any]] = []
            if payload.get("version") == 2 and isinstance(payload.get("pools"), dict):
                pools = [dict(value) for value in payload["pools"].values() if isinstance(value, dict)]
            elif isinstance(payload, dict):
                for provider, raw_credential in payload.items():
                    if not isinstance(raw_credential, Mapping):
                        continue
                    pools.append(
                        {
                            "connectionID": str(provider),
                            "providerID": str(provider),
                            "billingLane": "legacy",
                            "accounts": {
                                f"legacy:{provider}": {
                                    "id": f"legacy:{provider}",
                                    "label": "Imported account",
                                    "enabled": True,
                                    "order": 0,
                                    "credential": dict(raw_credential),
                                }
                            },
                        }
                    )
            for pool in pools:
                provider = str(pool.get("providerID") or pool.get("connectionID") or "").strip()
                raw_accounts = pool.get("accounts") if isinstance(pool.get("accounts"), dict) else {}
                credential_samples = [
                    value.get("credential")
                    for value in raw_accounts.values()
                    if isinstance(value, dict) and isinstance(value.get("credential"), Mapping)
                ]
                sample = credential_samples[0] if credential_samples else None
                family, adapter, kind = _family_adapter(
                    base_url=None,
                    provider_hint=provider,
                )
                if family == "openai-compatible" and kind == "custom_gateway":
                    blockers.append(
                        f"native provider {provider or '(missing id)'} has no canonical base URL"
                    )
                    continue
                lane = str(pool.get("billingLane") or "")
                if lane in {"", "legacy"}:
                    lane = _billing_lane(family=family, kind=kind, credential=sample)
                kind = {
                    "subscription": "subscription",
                    "custom": "custom_gateway",
                    "local": "local",
                }.get(lane, "official")
                connection_id = _stable_id("pcn", owner, family, adapter, "", lane, "native")
                native_connection[(owner, provider, lane)] = connection_id
                connections.setdefault(
                    connection_id,
                    PlannedConnection(
                        id=connection_id,
                        owner=owner,
                        family_id=family,
                        adapter_id=adapter,
                        kind=kind,
                        billing_lane=lane,
                        label=provider.replace("-", " ").title(),
                        normalized_url=None,
                        settings={},
                        enabled=True,
                    ),
                )
                for order, (legacy_account_id, value) in enumerate(
                    sorted(raw_accounts.items(), key=lambda item: (int(item[1].get("order", 0)) if isinstance(item[1], dict) else 0, item[0]))
                ):
                    if not isinstance(value, dict):
                        continue
                    try:
                        credential = _credential(value.get("credential"))
                    except ProviderMigrationError as exc:
                        blockers.append(f"native provider {provider} account is invalid: {exc}")
                        continue
                    add_account(
                        connection_id=connection_id,
                        owner=owner,
                        label=str(value.get("label") or "Imported account"),
                        auth_method=_auth_method(credential),
                        auth_class=_auth_class(lane),
                        sort_order=int(value.get("order", order)),
                        enabled=bool(value.get("enabled", True)),
                        safe_identity=dict(value.get("identity") or {}),
                        credential=credential,
                    )
                models = referenced_native_models.get((owner, provider), set())
                models.update(
                    model
                    for pref_owner, pref_provider, model in hidden_native
                    if pref_owner == owner and pref_provider == provider
                )
                for model in sorted(value for value in models if value):
                    route_id = _stable_id("pmr", connection_id, model)
                    routes[route_id] = PlannedRoute(
                        id=route_id,
                        connection_id=connection_id,
                        owner=owner,
                        provider_model_id=model,
                        display_name=model,
                        operations=["chat.stream", "chat.complete"],
                        capabilities={},
                        visibility=(
                            "hidden" if (owner, provider, model) in hidden_native else "visible"
                        ),
                        provenance={"legacy_native_provider": provider},
                    )
                    aliases.append(
                        PlannedAlias(
                            id=_stable_id("pla", owner, "native-model", provider, model),
                            owner=owner,
                            legacy_kind="native-model",
                            legacy_id=f"mimo:{provider}:{model}",
                            connection_id=connection_id,
                            model_route_id=route_id,
                        )
                    )
                aliases.append(
                    PlannedAlias(
                        id=_stable_id("pla", owner, "native-provider", provider, lane),
                        owner=owner,
                        legacy_kind="native-provider",
                        legacy_id=f"mimo:{provider}:{lane}",
                        connection_id=connection_id,
                    )
                )

        # The old embedding settings file was a separate credential and route
        # authority.  Convert it before computing account entitlements.  An
        # empty existing file means the installation actually used the local
        # FastEmbed fallback; migration makes that choice explicit as a
        # normalized keyless local-executor route instead of preserving a
        # hidden fallback.
        embedding_file_exists = bool(
            embedding_path is not None and Path(embedding_path).is_file()
        )
        legacy_embedding: dict[str, Any] = {}
        if embedding_file_exists:
            try:
                raw_embedding = json.loads(
                    Path(embedding_path).read_text(encoding="utf-8")
                )
                if not isinstance(raw_embedding, dict):
                    raise ValueError("embedding settings are not an object")
                legacy_embedding = raw_embedding
            except (OSError, UnicodeError, ValueError):
                blockers.append("provider-bearing settings file embedding endpoint is malformed")
        if embedding_file_exists and not settings_owner:
            blockers.append("legacy embedding route has no deterministic owner")
        elif embedding_file_exists and settings_owner:
            raw_embedding_url = str(legacy_embedding.get("url") or "").strip()
            url = _normalized_url(raw_embedding_url)
            model = str(legacy_embedding.get("model") or "").strip()
            raw_key = str(legacy_embedding.get("api_key") or "")
            key = ""
            if raw_key:
                try:
                    key = decrypt(raw_key)
                except Exception:
                    key = ""
                if not key:
                    blockers.append("legacy embedding credential is unreadable")
            if raw_embedding_url and url is None:
                blockers.append("legacy embedding endpoint URL is malformed")
            elif url:
                if not model:
                    blockers.append("active legacy embedding endpoint has no model identity")
                family, adapter, kind = _family_adapter(
                    base_url=url,
                    name="Legacy embedding endpoint",
                )
                credential = {"type": "api", "key": key} if key else None
                lane = _billing_lane(
                    family=family,
                    kind=kind,
                    credential=credential,
                )
                connection_id = _stable_id(
                    "pcn",
                    settings_owner,
                    family,
                    adapter,
                    url,
                    lane,
                    "legacy-embeddings",
                )
                connections[connection_id] = PlannedConnection(
                    id=connection_id,
                    owner=settings_owner,
                    family_id=family,
                    adapter_id=adapter,
                    kind=kind,
                    billing_lane=lane,
                    label="Imported embedding endpoint",
                    normalized_url=url,
                    settings={},
                    enabled=bool(model),
                )
                if credential:
                    add_account(
                        connection_id=connection_id,
                        owner=settings_owner,
                        label="Imported embedding account",
                        auth_method="api_key",
                        auth_class=_auth_class(lane),
                        sort_order=0,
                        enabled=bool(model),
                        safe_identity={},
                        credential=credential,
                    )
                if model:
                    embedding_route_id = _stable_id("pmr", connection_id, model)
                    routes[embedding_route_id] = PlannedRoute(
                        id=embedding_route_id,
                        connection_id=connection_id,
                        owner=settings_owner,
                        provider_model_id=model,
                        display_name=model,
                        operations=["embeddings.create"],
                        capabilities={},
                        visibility="visible",
                        provenance={"legacy_embedding_settings": True},
                    )
            else:
                connection_id = _stable_id(
                    "pcn", settings_owner, "local-executor", "embeddings"
                )
                connections[connection_id] = PlannedConnection(
                    id=connection_id,
                    owner=settings_owner,
                    family_id="local-executor",
                    adapter_id="openclank-local-executor",
                    kind="local",
                    billing_lane="local",
                    label="Open Clank local executors",
                    normalized_url=None,
                    settings={},
                    enabled=True,
                )
                embedding_route_id = _stable_id(
                    "pmr", connection_id, "fastembed/default"
                )
                routes[embedding_route_id] = PlannedRoute(
                    id=embedding_route_id,
                    connection_id=connection_id,
                    owner=settings_owner,
                    provider_model_id="fastembed/default",
                    display_name="FastEmbed (local)",
                    operations=["embeddings.create"],
                    capabilities={
                        "localExecutorID": "openclank.fastembed.v1",
                        "batch": True,
                        "normalized": True,
                    },
                    visibility="visible",
                    provenance={"legacy_fastembed_fallback": True},
                )

        entitlements = sorted(
            (account.id, route.id)
            for account in accounts.values()
            for route in routes.values()
            if account.connection_id == route.connection_id
        )

        account_ids_by_connection: dict[str, list[str]] = {}
        for account in accounts.values():
            account_ids_by_connection.setdefault(account.connection_id, []).append(account.id)
        route_by_connection_model = {
            (route.connection_id, route.provider_model_id): route.id
            for route in routes.values()
        }
        subscriptions_by_share: dict[str, list[dict[str, Any]]] = {}
        for subscription in subscriptions:
            subscriptions_by_share.setdefault(str(subscription.get("share_id") or ""), []).append(subscription)
        planned_shares: list[PlannedShare] = []
        for share in shares_raw:
            legacy_share_id = str(share.get("id") or "")
            owner = _owner(share.get("owner"), owner_assignment)
            source_kind = str(share.get("source_kind") or "")
            source_id = str(share.get("source_id") or "")
            if source_kind == "endpoint":
                connection_id = endpoint_connection.get(source_id)
            else:
                provider = source_id.removeprefix("mimo:")
                matches = [
                    cid
                    for (candidate_owner, candidate_provider, _lane), cid in native_connection.items()
                    if candidate_owner == owner and candidate_provider == provider
                ]
                connection_id = sorted(matches)[0] if len(matches) == 1 else None
            model = str(share.get("model_id") or "")
            route_id = route_by_connection_model.get((connection_id, model)) if connection_id else None
            candidates = sorted(account_ids_by_connection.get(connection_id or "", []))
            for subscription in subscriptions_by_share.get(legacy_share_id, []):
                accepted = bool(subscription.get("enabled")) and bool(share.get("active", 1))
                if not connection_id or not route_id or len(candidates) != 1:
                    if accepted:
                        blockers.append(
                            f"accepted legacy share {legacy_share_id} cannot map to one account and model"
                        )
                    else:
                        warnings.append(f"inactive legacy share {legacy_share_id} was not imported")
                    continue
                grant_id = _stable_id(
                    "psg",
                    owner,
                    legacy_share_id,
                    subscription.get("subscriber"),
                )
                connection = connections[connection_id]
                planned_shares.append(
                    PlannedShare(
                        id=grant_id,
                        owner=owner,
                        recipient=str(subscription.get("subscriber") or "").strip().lower(),
                        connection_id=connection_id,
                        billing_lane=connection.billing_lane,
                        label="Shared provider",
                        account_ids=candidates,
                        model_route_ids=[route_id],
                        state="active" if share.get("active", 1) else "revoked",
                        accepted=accepted,
                        legacy_share_id=legacy_share_id,
                    )
                )
                aliases.append(
                    PlannedAlias(
                        id=_stable_id("pla", owner, "share", legacy_share_id, subscription.get("subscriber")),
                        owner=owner,
                        legacy_kind="share",
                        legacy_id=f"{legacy_share_id}:{subscription.get('subscriber')}",
                        connection_id=connection_id,
                        model_route_id=route_id,
                        share_grant_id=grant_id,
                    )
                )

        planned_share_lookup = {
            (row.legacy_share_id, row.recipient): row
            for row in planned_shares
            if row.accepted and row.state == "active"
        }

        def resolve_route(owner: str, endpoint_id: str, model: str) -> str | None:
            endpoint_id = str(endpoint_id or "").strip()
            model = str(model or "").strip()
            if endpoint_id.startswith("shared:"):
                share = planned_share_lookup.get((endpoint_id.split(":", 1)[1], owner))
                if share and (not model or routes[share.model_route_ids[0]].provider_model_id == model):
                    return share.model_route_ids[0]
                return None
            if endpoint_id.startswith("mimo:"):
                provider = endpoint_id.split(":", 1)[1]
                if provider == "auto" and "/" in model:
                    provider, model = model.split("/", 1)
                provider_connections = {
                    connection_id
                    for (candidate_owner, candidate_provider, _lane), connection_id
                    in native_connection.items()
                    if candidate_owner == owner and candidate_provider == provider
                }
                candidates = [
                    route.id
                    for route in routes.values()
                    if route.owner == owner
                    and route.provider_model_id == model
                    and route.connection_id in provider_connections
                ]
                return candidates[0] if len(candidates) == 1 else None
            connection_id = endpoint_connection.get(endpoint_id)
            if connection_id and connections[connection_id].owner == owner:
                return route_by_connection_model.get((connection_id, model))
            return None

        references: list[PlannedReference] = []
        reference_specs = (
            ("sessions", sessions, False),
            ("scheduled_tasks", _rows(database, "scheduled_tasks"), True),
            ("crew_members", _rows(database, "crew_members"), True),
        )
        for table, values, can_block in reference_specs:
            for value in values:
                endpoint_id = str(value.get("endpoint_id") or "").strip()
                model = str(value.get("model") or "").strip()
                if not endpoint_id and not model:
                    continue
                row_owner = _owner(value.get("owner"), owner_assignment)
                active = (
                    str(value.get("status") or "").lower() == "active"
                    if table == "scheduled_tasks"
                    else bool(value.get("is_active", True))
                )
                route_id = resolve_route(row_owner, endpoint_id, model)
                if route_id:
                    references.append(
                        PlannedReference(
                            table=table,
                            row_id=str(value.get("id") or ""),
                            model_route_id=route_id,
                            active=active,
                        )
                    )
                elif can_block and active and str(value.get("task_type") or "llm") == "llm":
                    blockers.append(
                        f"active {table} record {value.get('id')} has an unresolved provider route"
                    )
                else:
                    warnings.append(
                        f"historical {table} record {value.get('id')} retains unresolved provider provenance"
                    )

        for value in _rows(database, "comparisons"):
            row_owner = _owner(value.get("owner"), owner_assignment)
            for side in ("a", "b"):
                route_id = resolve_route(
                    row_owner,
                    str(value.get(f"endpoint_{side}") or ""),
                    str(value.get(f"model_{side}") or ""),
                )
                if route_id:
                    references.append(
                        PlannedReference(
                            table="comparisons",
                            row_id=str(value.get("id") or ""),
                            model_route_id=route_id,
                            active=False,
                            column=f"provider_model_route_{side}_id",
                        )
                    )
                elif value.get(f"endpoint_{side}") or value.get(f"model_{side}"):
                    warnings.append(
                        f"historical comparison {value.get('id')} side {side} retains unresolved provider provenance"
                    )

        def load_json(path: Path | None) -> dict[str, Any]:
            if path is None or not Path(path).is_file():
                return {}
            try:
                value = json.loads(Path(path).read_text(encoding="utf-8"))
                return value if isinstance(value, dict) else {}
            except (OSError, UnicodeError, ValueError):
                blockers.append(f"provider-bearing settings file {Path(path).name} is malformed")
                return {}

        route_bindings: list[PlannedRouteBinding] = []

        def add_binding(owner: str, purpose: str, endpoint_id: str, model: str, ordinal: int) -> None:
            route_id = resolve_route(owner, endpoint_id, model)
            if not route_id:
                if endpoint_id or model:
                    warnings.append(
                        f"{purpose} default for {owner} could not be mapped and will be unset"
                    )
                return
            if any(
                row.owner == owner and row.purpose == purpose and row.model_route_id == route_id
                for row in route_bindings
            ):
                return
            route_bindings.append(
                PlannedRouteBinding(
                    id=_stable_id("prb", owner, purpose, ordinal, route_id),
                    owner=owner,
                    purpose=purpose,
                    ordinal=ordinal,
                    model_route_id=route_id,
                )
            )

        preferences = load_json(user_prefs_path).get("_users", {})
        if isinstance(preferences, dict):
            for raw_owner, values in preferences.items():
                if not isinstance(values, dict):
                    continue
                preference_owner = _owner(raw_owner, owner_assignment)
                endpoint_id = str(values.get("default_endpoint_id") or "")
                model = str(values.get("default_model") or "")
                add_binding(preference_owner, "chat", endpoint_id, model, 0)
                for index, fallback in enumerate(values.get("default_model_fallbacks") or [], start=1):
                    if isinstance(fallback, dict):
                        add_binding(
                            preference_owner,
                            "chat",
                            str(fallback.get("endpoint_id") or endpoint_id),
                            str(fallback.get("model") or ""),
                            index,
                        )
                    else:
                        add_binding(preference_owner, "chat", endpoint_id, str(fallback), index)

        settings = load_json(settings_path)
        if settings_owner:
            purposes = {
                "chat": ("default_endpoint_id", "default_model", "default_model_fallbacks"),
                "utility": ("utility_endpoint_id", "utility_model", "utility_model_fallbacks"),
                "memory": ("memory_endpoint_id", "memory_model", "memory_model_fallbacks"),
                "research": ("research_endpoint_id", "research_model", None),
                "tasks": ("task_endpoint_id", "task_model", None),
                "vision": ("vision_endpoint_id", "vision_model", "vision_model_fallbacks"),
                "images": ("image_endpoint_id", "image_model", None),
                "tts": ("tts_endpoint_id", "tts_model", None),
                "stt": ("stt_endpoint_id", "stt_model", None),
            }
            for purpose, (endpoint_key, model_key, fallback_key) in purposes.items():
                endpoint_id = str(settings.get(endpoint_key) or "")
                model = str(settings.get(model_key) or "")
                fallbacks_value = settings.get(fallback_key) if fallback_key else None
                # The first migration must not change the effective model for
                # memory work.  Clone the owner's Utility chain only when no
                # explicit Memory setting exists; once materialized the two
                # bindings are independent and edits cannot cross them.
                if purpose == "memory" and not endpoint_id and not model and not fallbacks_value:
                    endpoint_id = str(settings.get("utility_endpoint_id") or "")
                    model = str(settings.get("utility_model") or "")
                    fallbacks_value = settings.get("utility_model_fallbacks") or []
                add_binding(settings_owner, purpose, endpoint_id, model, 0)
                for index, fallback in enumerate(fallbacks_value or [], start=1) if fallback_key else ():
                    if isinstance(fallback, dict):
                        add_binding(
                            settings_owner,
                            purpose,
                            str(fallback.get("endpoint_id") or endpoint_id),
                            str(fallback.get("model") or ""),
                            index,
                        )
                    else:
                        add_binding(settings_owner, purpose, endpoint_id, str(fallback), index)
        if embedding_route_id and settings_owner:
            route_bindings.append(
                PlannedRouteBinding(
                    id=_stable_id(
                        "prb",
                        settings_owner,
                        "embeddings",
                        0,
                        embedding_route_id,
                    ),
                    owner=settings_owner,
                    purpose="embeddings",
                    ordinal=0,
                    model_route_id=embedding_route_id,
                )
            )
    finally:
        database.close()
    return ProviderMappingPlan(
        connections=sorted(connections.values(), key=lambda row: row.id),
        accounts=sorted(accounts.values(), key=lambda row: (row.connection_id, row.sort_order, row.id)),
        routes=sorted(routes.values(), key=lambda row: (row.connection_id, row.provider_model_id)),
        entitlements=entitlements,
        shares=sorted(planned_shares, key=lambda row: row.id),
        aliases=sorted(aliases, key=lambda row: row.id),
        references=sorted(
            references,
            key=lambda row: (row.table, row.row_id, row.column),
        ),
        route_bindings=sorted(
            route_bindings,
            key=lambda row: (row.owner, row.purpose, row.ordinal),
        ),
        blockers=blockers,
        warnings=warnings,
    )


def apply_provider_mapping_plan(
    *,
    database_url: str,
    plan: ProviderMappingPlan,
) -> dict[str, int]:
    """Apply a frozen plan transactionally and idempotently."""

    if plan.blockers:
        raise ProviderMigrationError("provider mapping plan has blockers")
    engine = create_engine(database_url)
    ProviderBase.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)
    db = session_factory()
    try:
        # The activation gate runs before core.init_db. Install every current
        # ORM route reference, even when the frozen plan has no row to update.
        # Otherwise a valid empty legacy table would remain unusable at boot.
        connection = db.connection()
        tables = {
            str(row[0])
            for row in connection.exec_driver_sql(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        for table, columns in _PROVIDER_REFERENCE_COLUMNS.items():
            if table not in tables:
                continue
            column_names = {
                str(row[1])
                for row in connection.exec_driver_sql(
                    f'PRAGMA table_info("{table}")'
                ).fetchall()
            }
            for column, index_name in columns.items():
                if column not in column_names:
                    connection.exec_driver_sql(
                        f'ALTER TABLE "{table}" ADD COLUMN "{column}" TEXT'
                    )
                connection.exec_driver_sql(
                    f'CREATE INDEX IF NOT EXISTS "{index_name}" '
                    f'ON "{table}" ("{column}")'
                )

        for value in plan.connections:
            existing = db.get(ProviderConnection, value.id)
            if existing is None:
                db.add(
                    ProviderConnection(
                        id=value.id,
                        owner=value.owner,
                        family_id=value.family_id,
                        adapter_id=value.adapter_id,
                        kind=value.kind,
                        billing_lane=value.billing_lane,
                        label=value.label,
                        normalized_url=value.normalized_url,
                        settings=value.settings,
                        enabled=value.enabled,
                    )
                )
            elif (
                existing.owner,
                existing.family_id,
                existing.adapter_id,
                existing.billing_lane,
            ) != (value.owner, value.family_id, value.adapter_id, value.billing_lane):
                raise ProviderMigrationError("normalized connection conflicts with frozen plan")
        db.flush()
        for value in plan.accounts:
            fingerprint = credential_fingerprint(
                value.credential,
                owner=value.owner,
                connection_id=value.connection_id,
            )
            existing = db.get(ProviderAccount, value.id)
            if existing is None:
                db.add(
                    ProviderAccount(
                        id=value.id,
                        connection_id=value.connection_id,
                        owner=value.owner,
                        label=value.label,
                        auth_method=value.auth_method,
                        auth_class=value.auth_class,
                        sort_order=value.sort_order,
                        enabled=value.enabled,
                        credential_envelope=seal_credential(
                            value.credential,
                            CredentialScope(value.owner, value.connection_id, value.id, 1),
                        ),
                        credential_fingerprint=fingerprint,
                        credential_version=1,
                        safe_identity=value.safe_identity,
                    )
                )
            elif existing.credential_fingerprint != fingerprint:
                raise ProviderMigrationError("normalized account conflicts with frozen plan")
        for value in plan.routes:
            existing = db.get(ProviderModelRoute, value.id)
            if existing is None:
                db.add(
                    ProviderModelRoute(
                        id=value.id,
                        connection_id=value.connection_id,
                        owner=value.owner,
                        provider_model_id=value.provider_model_id,
                        display_name=value.display_name,
                        operations=value.operations,
                        capabilities=value.capabilities,
                        visibility=value.visibility,
                        provenance=value.provenance,
                        enabled=True,
                    )
                )
        db.flush()
        for account_id, route_id in plan.entitlements:
            if db.get(ProviderAccountEntitlement, (account_id, route_id)) is None:
                db.add(
                    ProviderAccountEntitlement(
                        account_id=account_id,
                        model_route_id=route_id,
                        eligible=True,
                        evidence={"source": "legacy-migration"},
                    )
                )
        for value in plan.shares:
            if db.get(ProviderShareGrant, value.id) is None:
                db.add(
                    ProviderShareGrant(
                        id=value.id,
                        owner=value.owner,
                        recipient=value.recipient,
                        connection_id=value.connection_id,
                        billing_lane=value.billing_lane,
                        label=value.label,
                        account_selector={
                            "mode": "explicit_accounts",
                            "account_ids": value.account_ids,
                        },
                        model_selector={
                            "mode": "explicit_models",
                            "model_route_ids": value.model_route_ids,
                        },
                        disclosure_fields=[],
                        state=value.state,
                        revision=1,
                        accepted_revision=1 if value.accepted else None,
                    )
                )
        db.flush()
        for value in plan.aliases:
            if db.get(ProviderLegacyAlias, value.id) is None:
                db.add(
                    ProviderLegacyAlias(
                        id=value.id,
                        owner=value.owner,
                        legacy_kind=value.legacy_kind,
                        legacy_id=value.legacy_id,
                        connection_id=value.connection_id,
                        model_route_id=value.model_route_id,
                        share_grant_id=value.share_grant_id,
                        provenance=value.provenance,
                    )
                )
        for value in plan.route_bindings:
            if db.get(ProviderRouteBinding, value.id) is None:
                db.add(
                    ProviderRouteBinding(
                        id=value.id,
                        owner=value.owner,
                        purpose=value.purpose,
                        ordinal=value.ordinal,
                        model_route_id=value.model_route_id,
                        enabled=value.enabled,
                    )
                )
        # Reference updates are part of the migration transaction. Historical
        # endpoint/model columns remain nonsecret provenance until the final
        # legacy schema drop, while execution switches to this stable route ID.
        for table in sorted({row.table for row in plan.references}):
            column_names = {
                str(row[1])
                for row in connection.exec_driver_sql(
                    f'PRAGMA table_info("{table}")'
                ).fetchall()
            }
            for column in sorted(
                {row.column for row in plan.references if row.table == table}
            ):
                if column not in column_names:
                    connection.exec_driver_sql(
                        f'ALTER TABLE "{table}" ADD COLUMN "{column}" TEXT'
                    )
        for value in plan.references:
            db.execute(
                text(
                    f'UPDATE "{value.table}" SET "{value.column}" = :route '
                    "WHERE id = :row_id"
                ),
                {"route": value.model_route_id, "row_id": value.row_id},
            )
        db.commit()
        return {
            "connections": len(plan.connections),
            "accounts": len(plan.accounts),
            "routes": len(plan.routes),
            "entitlements": len(plan.entitlements),
            "shares": len(plan.shares),
            "aliases": len(plan.aliases),
            "references": len(plan.references),
            "route_bindings": len(plan.route_bindings),
        }
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
        engine.dispose()


__all__ = [
    "ProviderMappingPlan",
    "apply_provider_mapping_plan",
    "build_provider_mapping_plan",
]
