"""Read-only admission of existing provider stores; no migration execution."""
import json
import os
import re
import sqlite3
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlsplit, urlunsplit

class ProviderStartupError(RuntimeError):
    pass

PROVIDER_ENV_AUTHORITIES = frozenset(
    {
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "OPENROUTER_API_KEY",
        "GOOGLE_GENERATIVE_AI_API_KEY",
        "GEMINI_API_KEY",
        "XAI_API_KEY",
        "GITHUB_TOKEN",
        "GITHUB_COPILOT_TOKEN",
        "MIMO_API_KEY",
        "MIMOCODE_PROVIDER_AUTH_FD",
        "MIMOCODE_CONFIG_CONTENT",
        "MIMOCODE_HOME",
        "EMBEDDING_API_KEY",
        "TTS_API_KEY",
        "STT_API_KEY",
        # Retired provider *configuration* authorities. Connections, route
        # bindings, local-executor model IDs, and trust settings now live in
        # the normalized provider repository and are validated by the engine.
        "LLM_HOST",
        "LLM_HOSTS",
        "LLM_CONNECT_TIMEOUT",
        "LLM_CA_BUNDLE",
        "OLLAMA_BASE_URL",
        "OLLAMA_URL",
        "LM_STUDIO_URL",
        "RESEARCH_LLM_ENDPOINT",
        "EMBEDDING_URL",
        "EMBEDDING_MODEL",
        "EMBEDDING_BLOCK_PRIVATE_IPS",
        "FASTEMBED_MODEL",
        "FASTEMBED_CACHE_PATH",
        "FM_EMBED_API_BASE",
        "FM_EMBED_API_KEY",
        "FM_EMBED_MODEL",
        "FM_EMBED_DIMENSIONS",
        "FM_EMBED_TIMEOUT_MS",
        "OPEN_CLANK_SMALL_MODEL",
        "ODYSSEUS_SMALL_MODEL",
        "ODYSSEUS_GROUNDING_MODEL",
        "ODYSSEUS_MODEL_KEEPALIVE",
        "ODYSSEUS_SAM_MODEL",
        "ODYSSEUS_ALLOW_OLLAMA_CLI_SCAN",
        "MIMO_HIDDEN_MODELS",
        "MIMO_SHADOW_ENDPOINT_PROVIDERS",
        "OPENCLAW_CONFIG_PATH",
    }
)


NORMALIZED_PROVIDER_TABLES = frozenset(
    {
        "provider_connections",
        "provider_accounts",
        "provider_model_routes",
        "provider_account_entitlements",
        "provider_account_health",
        "provider_account_model_health",
        "provider_operation_bindings",
        "provider_rotation_cursors",
        "provider_route_bindings",
        "provider_refresh_leases",
        "provider_credential_leases",
        "provider_share_grants",
        "provider_idempotency_records",
        "provider_legacy_aliases",
    }
)


_PROVIDER_ROUTE_CONSUMERS = {
    "comparisons": (
        "provider_model_route_a_id",
        "provider_model_route_b_id",
    ),
    "crew_members": ("provider_model_route_id",),
    "scheduled_tasks": ("provider_model_route_id",),
    "sessions": ("provider_model_route_id",),
}


def _dynamic_models_dev_connection_reason(values: Mapping[str, str]) -> str | None:
    family = values["family"]
    adapter = values["adapter"]
    if adapter not in _MODELS_DEV_ADAPTERS:
        return f"unknown family {family or '(empty)'}"
    if not _MODELS_DEV_FAMILY_RE.fullmatch(family):
        return f"invalid ModelsDev family {family or '(empty)'}"
    if values["kind"] != "official":
        return "ModelsDev provider requires official connection kind"
    if values["billing_lane"] != "metered_api":
        return "ModelsDev provider requires metered API billing lane"
    try:
        parsed = urlsplit(values["normalized_url"])
    except ValueError:
        parsed = None
    if (
        parsed is None
        or parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        return "ModelsDev provider requires a safe HTTPS URL"
    return None


def _tables(connection: sqlite3.Connection) -> set[str]:
    return {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
    }


def _table_count(connection: sqlite3.Connection, table: str) -> int:
    return int(connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])


def _columns(connection: sqlite3.Connection, table: str) -> set[str]:
    return {
        str(row[1])
        for row in connection.execute(f'PRAGMA table_info("{table}")').fetchall()
    }


def _verify_activation_schema(
    connection: sqlite3.Connection,
    tables: set[str],
) -> None:
    missing = [
        f"{table}.{column}"
        for table, required_columns in _PROVIDER_ROUTE_CONSUMERS.items()
        for column in required_columns
        if table not in tables or column not in _columns(connection, table)
    ]
    if missing:
        raise ProviderStartupError(
            "provider activation schema is missing required route columns: "
            + ", ".join(missing)
        )


def _verify_activation_connections(connection: sqlite3.Connection) -> None:
    required = {
        "id",
        "family_id",
        "adapter_id",
        "kind",
        "billing_lane",
        "normalized_url",
        "enabled",
        "deleted_at",
    }
    columns = _columns(connection, "provider_connections")
    missing = sorted(required.difference(columns))
    if missing:
        raise ProviderStartupError(
            "provider activation schema is missing connection columns: "
            + ", ".join(missing)
        )

    invalid: list[str] = []
    rows = connection.execute(
        "SELECT id, family_id, adapter_id, kind, billing_lane, normalized_url "
        "FROM provider_connections "
        "WHERE enabled = 1 AND deleted_at IS NULL ORDER BY id"
    ).fetchall()
    for identity, family, adapter, kind, billing_lane, normalized_url in rows:
        values = {
            "family": str(family or "").strip(),
            "adapter": str(adapter or "").strip(),
            "kind": str(kind or "").strip(),
            "billing_lane": str(billing_lane or "").strip(),
            "normalized_url": str(normalized_url or "").strip(),
        }
        contract = _PROVIDER_CONNECTION_CONTRACT.get(values["family"])
        reason: str | None = None
        if contract is None:
            reason = _dynamic_models_dev_connection_reason(values)
        elif values["adapter"] not in contract["adapters"]:
            reason = f"unsupported adapter {values['adapter'] or '(empty)'}"
        elif values["kind"] not in contract["kinds"]:
            reason = f"unsupported connection kind {values['kind'] or '(empty)'}"
        elif values["billing_lane"] not in contract["billing_lanes"]:
            reason = f"unsupported billing lane {values['billing_lane'] or '(empty)'}"
        elif values["kind"] == "subscription" and values["billing_lane"] != "subscription":
            reason = "subscription kind requires subscription billing lane"
        elif values["kind"] == "local" and values["billing_lane"] != "local":
            reason = "local kind requires local billing lane"
        if reason:
            invalid.append(f"{identity}: {reason}")
    if invalid:
        raise ProviderStartupError(
            "provider connections are not activation-ready: " + "; ".join(invalid)
        )


_PROVIDER_CONNECTION_CONTRACT = {
    "anthropic": {
        "adapters": frozenset({"anthropic-messages"}),
        "kinds": frozenset({"official", "subscription", "custom_gateway"}),
        "billing_lanes": frozenset({"metered_api", "subscription", "custom"}),
    },
    "deepseek": {
        "adapters": frozenset({"openai-chat"}),
        "kinds": frozenset({"official"}),
        "billing_lanes": frozenset({"metered_api"}),
    },
    "github-copilot": {
        "adapters": frozenset({"copilot-chat"}),
        "kinds": frozenset({"subscription"}),
        "billing_lanes": frozenset({"subscription"}),
    },
    "google": {
        "adapters": frozenset({"google-generative-ai", "google-vertex"}),
        "kinds": frozenset({"official", "custom_gateway"}),
        "billing_lanes": frozenset({"metered_api", "custom"}),
    },
    "local-executor": {
        "adapters": frozenset({"openclank-local-executor"}),
        "kinds": frozenset({"local"}),
        "billing_lanes": frozenset({"local"}),
    },
    "ollama": {
        "adapters": frozenset({"ollama"}),
        "kinds": frozenset({"local"}),
        "billing_lanes": frozenset({"local"}),
    },
    "openai": {
        "adapters": frozenset({"openai-responses", "openai-chat"}),
        "kinds": frozenset({"official", "subscription", "custom_gateway"}),
        "billing_lanes": frozenset({"metered_api", "subscription", "custom"}),
    },
    "openai-compatible": {
        "adapters": frozenset({"openai-chat", "openai-responses"}),
        "kinds": frozenset({"custom_gateway", "local"}),
        "billing_lanes": frozenset({"custom", "local"}),
    },
    "openrouter": {
        "adapters": frozenset({"openai-chat"}),
        "kinds": frozenset({"official", "custom_gateway"}),
        "billing_lanes": frozenset({"metered_api", "custom"}),
    },
    "xai": {
        "adapters": frozenset({"xai-responses"}),
        "kinds": frozenset({"official", "subscription", "custom_gateway"}),
        "billing_lanes": frozenset({"metered_api", "subscription", "custom"}),
    },
    "xiaomi": {
        "adapters": frozenset({"mimo-native"}),
        "kinds": frozenset({"official", "subscription"}),
        "billing_lanes": frozenset({"metered_api", "subscription"}),
    },
}


_MODELS_DEV_ADAPTERS = frozenset(
    {"models-dev-anthropic", "models-dev-openai-compatible"}
)


_MODELS_DEV_FAMILY_RE = re.compile(r"^[a-z0-9](?:[a-z0-9._-]{0,126}[a-z0-9])?$")


def validate_provider_store(data_dir):
    data = Path(data_dir)
    database = data / "app.db"
    if not database.is_file():
        return {"needed": False, "phase": "fresh", "complete": True}
    journal = data / ".migrations" / "provider-cutover-v1.json"
    if journal.exists():
        try:
            state = json.loads(journal.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ProviderStartupError("Provider migration journal is unreadable; inspect it with workspace tools") from exc
        if state.get("schema_version") != 1 or state.get("phase") != "complete":
            raise ProviderStartupError("Provider store conversion is unfinished. Stop writers, retain a complete backup, and finish the reviewed offline conversion before starting this release. No runtime conversion was performed.")
    connection = sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        tables = _tables(connection)
        if not tables:
            return {"needed": False, "phase": "fresh", "complete": True}
        retired = tables.intersection({"model_endpoints", "model_capabilities", "provider_auth_sessions", "mimo_auth_store", "mimo_model_prefs", "model_shares", "model_share_subscriptions", "mimo_projection_states"})
        if retired:
            raise ProviderStartupError("Existing provider store does not match this release. Stop writers and retain a complete backup; restore the matching release or prepare an offline conversion. No runtime conversion was performed; retired tables: " + ", ".join(sorted(retired)))
        missing = NORMALIZED_PROVIDER_TABLES.difference(tables)
        if missing:
            raise ProviderStartupError("Existing provider schema is incomplete; run explicit workspace migration: " + ", ".join(sorted(missing)))
        _verify_activation_schema(connection, tables)
        _verify_activation_connections(connection)
        if connection.execute("PRAGMA foreign_key_check").fetchone():
            raise ProviderStartupError("Existing provider store has foreign-key violations")
        return {"needed": False, "phase": "current", "complete": True}
    finally:
        connection.close()
