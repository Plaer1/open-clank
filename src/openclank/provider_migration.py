"""Pre-app provider hard-cut inventory, preflight, and migration journal.

This module intentionally avoids importing ``core.database``: the bootstrap
must finish (or fail) before the normal application's import-time migrations
and background services can admit work.
"""

from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from core.atomic_io import atomic_write_json
from src.secret_storage import decrypt


MIGRATION_SCHEMA_VERSION = 1
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
LEGACY_PROVIDER_TABLES = frozenset(
    {
        "model_endpoints",
        "model_capabilities",
        "provider_auth_sessions",
        "mimo_auth_store",
        "mimo_model_prefs",
        "model_shares",
        "model_share_subscriptions",
        "mimo_projection_states",
    }
)

_GLOBAL_PROVIDER_SETTING_KEYS = frozenset(
    {
        "default_endpoint_id",
        "default_model",
        "default_model_fallbacks",
        "utility_endpoint_id",
        "utility_model",
        "utility_model_fallbacks",
        "memory_endpoint_id",
        "memory_model",
        "memory_model_fallbacks",
        "research_endpoint_id",
        "research_model",
        "task_endpoint_id",
        "task_model",
        "vision_endpoint_id",
        "vision_model",
        "vision_model_fallbacks",
        "image_endpoint_id",
        "image_model",
        "tts_endpoint_id",
        "tts_model",
        "stt_endpoint_id",
        "stt_model",
    }
)


class ProviderMigrationError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class LegacyInventory:
    counts: dict[str, int]
    owners: tuple[str, ...]
    ownerless_credentials: int
    accepted_shares: int
    active_automations: int
    stale_references: int
    configured_environment_names: tuple[str, ...]
    malformed_credentials: int


@dataclass(frozen=True, slots=True)
class ProviderPreflight:
    ready: bool
    owner_assignment: str | None
    inventory: LegacyInventory
    blockers: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()

    def safe_report(self) -> dict[str, Any]:
        """Return a report containing counts and variable names, never values."""

        return {
            "schema_version": MIGRATION_SCHEMA_VERSION,
            "ready": self.ready,
            "owner_assignment": self.owner_assignment,
            "inventory": asdict(self.inventory),
            "blockers": list(self.blockers),
            "warnings": list(self.warnings),
        }


def _tables(connection: sqlite3.Connection) -> set[str]:
    return {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }


def _columns(connection: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in connection.execute(f'PRAGMA table_info("{table}")')}


def _count(connection: sqlite3.Connection, table: str, where: str = "", params=()) -> int:
    suffix = f" WHERE {where}" if where else ""
    return int(connection.execute(f'SELECT COUNT(*) FROM "{table}"{suffix}', params).fetchone()[0])


def _admins(auth_file: Path) -> tuple[str, ...]:
    try:
        payload = json.loads(auth_file.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return ()
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ProviderMigrationError("Open Clank application login file is malformed") from exc
    users = payload.get("users") if isinstance(payload, dict) else None
    if not isinstance(users, dict):
        return ()
    return tuple(
        sorted(
            str(name).strip().lower()
            for name, record in users.items()
            if str(name).strip()
            and isinstance(record, dict)
            and record.get("is_admin") is True
        )
    )


def _global_provider_settings_state(path: Path | None) -> tuple[bool, bool]:
    """Return ``(has_authority, malformed)`` without exposing setting values."""

    if path is None or not Path(path).is_file():
        return False, False
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return False, True
    if not isinstance(payload, dict):
        return False, True
    return (
        any(
            key in payload
            and payload[key] not in (None, "", [], {})
            for key in _GLOBAL_PROVIDER_SETTING_KEYS
        ),
        False,
    )


def _credential_integrity(connection: sqlite3.Connection, tables: set[str]) -> int:
    malformed = 0
    if "model_endpoints" in tables and "api_key" in _columns(connection, "model_endpoints"):
        for (value,) in connection.execute(
            "SELECT api_key FROM model_endpoints WHERE api_key IS NOT NULL AND api_key != ''"
        ):
            if not decrypt(str(value or "")):
                malformed += 1
    if "provider_auth_sessions" in tables:
        columns = _columns(connection, "provider_auth_sessions")
        names = [name for name in ("access_token", "refresh_token") if name in columns]
        if names:
            for row in connection.execute(
                f"SELECT {', '.join(names)} FROM provider_auth_sessions"
            ):
                for value in row:
                    if value and not decrypt(str(value)):
                        malformed += 1
    if "mimo_auth_store" in tables and "payload" in _columns(connection, "mimo_auth_store"):
        for (value,) in connection.execute(
            "SELECT payload FROM mimo_auth_store WHERE payload IS NOT NULL AND payload != ''"
        ):
            plaintext = decrypt(str(value or ""))
            try:
                parsed = json.loads(plaintext)
                if not isinstance(parsed, dict):
                    raise ValueError
            except (ValueError, TypeError):
                malformed += 1
    return malformed


def _owners(connection: sqlite3.Connection, tables: set[str]) -> tuple[set[str], int]:
    owners: set[str] = set()
    ownerless = 0
    for table in ("model_endpoints", "provider_auth_sessions", "mimo_auth_store"):
        if table not in tables or "owner" not in _columns(connection, table):
            continue
        for (value,) in connection.execute(f'SELECT owner FROM "{table}"'):
            normalized = str(value or "").strip().lower()
            if normalized:
                owners.add(normalized)
            else:
                ownerless += 1
    return owners, ownerless


def _share_state(connection: sqlite3.Connection, tables: set[str]) -> tuple[int, int]:
    if not {"model_shares", "model_share_subscriptions"}.issubset(tables):
        return 0, 0
    accepted = _count(
        connection,
        "model_share_subscriptions",
        "enabled = 1",
    )
    missing_predicates: list[str] = []
    if "model_endpoints" in tables:
        missing_predicates.append(
            "(share.source_kind = 'endpoint' AND NOT EXISTS "
            "(SELECT 1 FROM model_endpoints ep WHERE ep.id = share.source_id))"
        )
    else:
        missing_predicates.append("share.source_kind = 'endpoint'")
    if "mimo_auth_store" in tables:
        missing_predicates.append(
            "(share.source_kind = 'native' AND NOT EXISTS "
            "(SELECT 1 FROM mimo_auth_store auth WHERE auth.owner = share.owner))"
        )
    else:
        missing_predicates.append("share.source_kind = 'native'")
    unresolved = int(
        connection.execute(
            "SELECT COUNT(*) "
            "FROM model_share_subscriptions sub "
            "JOIN model_shares share ON share.id = sub.share_id "
            "WHERE sub.enabled = 1 AND share.active = 1 AND ("
            + " OR ".join(missing_predicates)
            + ")"
        ).fetchone()[0]
    )
    return accepted, unresolved


def _reference_state(connection: sqlite3.Connection, tables: set[str]) -> tuple[int, int]:
    active = 0
    stale = 0
    if "scheduled_tasks" in tables:
        columns = _columns(connection, "scheduled_tasks")
        if {"status", "task_type"}.issubset(columns):
            active = _count(
                connection,
                "scheduled_tasks",
                "status = 'active' AND task_type = 'llm'",
            )
        if "endpoint_id" in columns and "model_endpoints" in tables:
            stale += int(
                connection.execute(
                    """
                    SELECT COUNT(*) FROM scheduled_tasks task
                     WHERE task.endpoint_id IS NOT NULL AND task.endpoint_id != ''
                       AND NOT EXISTS (
                         SELECT 1 FROM model_endpoints ep WHERE ep.id = task.endpoint_id
                       )
                    """
                ).fetchone()[0]
            )
    for table in ("sessions", "crew_members"):
        if table not in tables or "endpoint_id" not in _columns(connection, table):
            continue
        if "model_endpoints" in tables:
            stale += int(
                connection.execute(
                    f"""
                    SELECT COUNT(*) FROM {table} item
                     WHERE item.endpoint_id IS NOT NULL AND item.endpoint_id != ''
                       AND item.endpoint_id NOT LIKE 'mimo:%'
                       AND item.endpoint_id NOT LIKE 'shared:%'
                       AND NOT EXISTS (
                         SELECT 1 FROM model_endpoints ep WHERE ep.id = item.endpoint_id
                       )
                    """
                ).fetchone()[0]
            )
    return active, stale


def inventory_legacy_provider_state(
    *,
    database_path: Path,
    auth_file: Path,
    environment: dict[str, str] | None = None,
) -> LegacyInventory:
    database_path = Path(database_path)
    if not database_path.is_file():
        raise ProviderMigrationError(f"provider database does not exist: {database_path}")
    connection = sqlite3.connect(str(database_path), timeout=30)
    try:
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            raise ProviderMigrationError("provider database failed SQLite integrity_check")
        tables = _tables(connection)
        counts = {
            table: _count(connection, table)
            for table in sorted(LEGACY_PROVIDER_TABLES & tables)
        }
        owners, ownerless = _owners(connection, tables)
        accepted, unresolved_shares = _share_state(connection, tables)
        active, stale = _reference_state(connection, tables)
        malformed = _credential_integrity(connection, tables)
        if unresolved_shares:
            counts["unresolved_accepted_share_sources"] = unresolved_shares
    finally:
        connection.close()
    configured = tuple(
        sorted(
            name
            for name in PROVIDER_ENV_AUTHORITIES
            if str((environment or os.environ).get(name) or "").strip()
        )
    )
    return LegacyInventory(
        counts=counts,
        owners=tuple(sorted(owners)),
        ownerless_credentials=ownerless,
        accepted_shares=accepted,
        active_automations=active,
        stale_references=stale,
        configured_environment_names=configured,
        malformed_credentials=malformed,
    )


def provider_migration_preflight(
    *,
    database_path: Path,
    auth_file: Path,
    explicit_owner: str | None = None,
    auth_enabled: bool = True,
    environment: dict[str, str] | None = None,
    embedding_path: Path | None = None,
    settings_path: Path | None = None,
) -> ProviderPreflight:
    inventory = inventory_legacy_provider_state(
        database_path=database_path,
        auth_file=auth_file,
        environment=environment,
    )
    admins = _admins(Path(auth_file)) if auth_enabled else ()
    owner_assignment = str(explicit_owner or "").strip().lower() or None
    blockers: list[str] = []
    warnings: list[str] = []
    global_settings, malformed_settings = _global_provider_settings_state(settings_path)
    if malformed_settings:
        blockers.append("provider-bearing settings file settings.json is malformed")
    if inventory.malformed_credentials:
        blockers.append(
            f"{inventory.malformed_credentials} provider credential record(s) cannot be decrypted or parsed"
        )
    unresolved = inventory.counts.get("unresolved_accepted_share_sources", 0)
    if unresolved:
        blockers.append(f"{unresolved} accepted share source(s) cannot be resolved")
    ownerless_provider_authority = (
        bool(inventory.ownerless_credentials)
        or bool(embedding_path is not None and Path(embedding_path).is_file())
        or global_settings
    )
    if ownerless_provider_authority:
        if not auth_enabled:
            owner_assignment = owner_assignment or "local-installation"
        elif owner_assignment:
            if owner_assignment not in admins:
                blockers.append("explicit legacy provider owner is not an Open Clank admin")
        elif len(admins) == 1:
            owner_assignment = admins[0]
        else:
            blockers.append(
                "ownerless provider credentials require `openclank migrate providers --owner USER`"
            )
    elif auth_enabled and owner_assignment and owner_assignment not in admins:
        blockers.append("explicit legacy provider owner is not an Open Clank admin")
    if inventory.stale_references:
        warnings.append(
            f"{inventory.stale_references} stale historical provider reference(s) will remain provenance"
        )
    if inventory.configured_environment_names:
        warnings.append(
            "provider environment authorities are present and must be imported or removed: "
            + ", ".join(inventory.configured_environment_names)
        )
    return ProviderPreflight(
        ready=not blockers,
        owner_assignment=owner_assignment,
        inventory=inventory,
        blockers=tuple(blockers),
        warnings=tuple(warnings),
    )


class MigrationJournal:
    """Atomic phase journal; mixed/partial schema states fail closed."""

    PHASES = (
        "preflight",
        "workers_drained",
        "snapshot_verified",
        "plan_frozen",
        "transaction_committed",
        "legacy_scrubbed",
        "complete",
    )

    def __init__(self, path: Path):
        self.path = Path(path)

    def read(self) -> dict[str, Any] | None:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ProviderMigrationError("provider migration journal is corrupt") from exc
        if payload.get("schema_version") != MIGRATION_SCHEMA_VERSION:
            raise ProviderMigrationError("provider migration journal schema is incompatible")
        if payload.get("phase") not in self.PHASES:
            raise ProviderMigrationError("provider migration journal phase is invalid")
        return payload

    def advance(self, phase: str, **safe_details: Any) -> dict[str, Any]:
        if phase not in self.PHASES:
            raise ProviderMigrationError("provider migration phase is invalid")
        current = self.read()
        index = self.PHASES.index(phase)
        if current:
            old_index = self.PHASES.index(current["phase"])
            if index < old_index or index > old_index + 1:
                raise ProviderMigrationError("provider migration phase transition is invalid")
        payload = {
            "schema_version": MIGRATION_SCHEMA_VERSION,
            "phase": phase,
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "details": safe_details,
        }
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        atomic_write_json(str(self.path), payload, indent=2)
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass
        return payload


__all__ = [
    "LegacyInventory",
    "MigrationJournal",
    "ProviderMigrationError",
    "ProviderPreflight",
    "inventory_legacy_provider_state",
    "provider_migration_preflight",
]
