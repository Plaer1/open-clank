"""Fail-closed pre-application provider cutover coordinator.

This module must remain importable before :mod:`core.database`.  It works
against SQLite directly, verifies the private engine first, journals every
phase, and stores the frozen mapping plan encrypted.  Normal application
startup is allowed only after the journal reaches ``complete``.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlsplit, urlunsplit

from src.openclank.migration_snapshot import (
    MigrationLock,
    create_rollback_archive,
    verify_rollback_archive,
)
from src.openclank.provider_migration import (
    LEGACY_PROVIDER_TABLES,
    MigrationJournal,
    ProviderMigrationError,
    provider_migration_preflight,
)
from src.openclank.provider_migration_plan import (
    PlannedAccount,
    PlannedAlias,
    PlannedConnection,
    PlannedReference,
    PlannedRoute,
    PlannedRouteBinding,
    PlannedShare,
    ProviderMappingPlan,
    apply_provider_mapping_plan,
    build_provider_mapping_plan,
)
from src.secret_storage import _get_fernet, encrypt


PLAN_MAGIC = b"OPENCLANK-PROVIDER-PLAN-V1\n"
PROVIDER_CUTOVER_VERSION = 1
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

# Application startup imports ORM models that select these route-reference
# columns even when the corresponding table has no rows.  The mapping plan may
# contain no row-level references for an empty table, so cutover verification
# must enforce the schema contract independently of the plan contents.
_PROVIDER_ROUTE_CONSUMERS = {
    "comparisons": (
        "provider_model_route_a_id",
        "provider_model_route_b_id",
    ),
    "crew_members": ("provider_model_route_id",),
    "scheduled_tasks": ("provider_model_route_id",),
    "sessions": ("provider_model_route_id",),
}

# Keep the pre-application gate explicit and dependency-free.  Importing the
# runtime projection here would import core.database before the cutover has
# selected and verified the installation database.  These values mirror the
# managed engine's provider-control catalog and projection adapter allow-list.
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

# ModelsDev-backed families are admitted dynamically by the managed engine,
# but execution is still limited to two SDKs that ship in the verified engine
# artifact.  Keep this pre-application gate narrow so a database edit cannot
# turn an arbitrary package or URL into provider authority.
_MODELS_DEV_ADAPTERS = frozenset(
    {"models-dev-anthropic", "models-dev-openai-compatible"}
)
_MODELS_DEV_FAMILY_RE = re.compile(r"^[a-z0-9](?:[a-z0-9._-]{0,126}[a-z0-9])?$")


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


@dataclass(frozen=True, slots=True)
class CutoverPaths:
    data_dir: Path
    database: Path
    auth_file: Path
    settings: Path
    user_prefs: Path
    embedding: Path
    migration_dir: Path
    journal: Path
    lock: Path
    active: Path
    plan: Path

    @classmethod
    def for_data_dir(cls, data_dir: Path | str) -> "CutoverPaths":
        root = Path(data_dir).expanduser().resolve()
        migrations = root / ".migrations"
        return cls(
            data_dir=root,
            database=root / "app.db",
            auth_file=root / "auth.json",
            settings=root / "settings.json",
            user_prefs=root / "user_prefs.json",
            embedding=root / "embedding_endpoint.json",
            migration_dir=migrations,
            journal=migrations / "provider-cutover-v1.json",
            lock=migrations / "provider-cutover-v1.lock",
            active=migrations / "provider-cutover-v1.active",
            plan=migrations / "provider-cutover-v1.ocplan",
        )


@dataclass(frozen=True, slots=True)
class CutoverResult:
    needed: bool
    complete: bool
    phase: str
    archive: str | None = None
    counts: Mapping[str, int] | None = None


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
        raise ProviderMigrationError(
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
        raise ProviderMigrationError(
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
        raise ProviderMigrationError(
            "provider connections are not activation-ready: " + "; ".join(invalid)
        )


def cutover_complete(data_dir: Path | str) -> bool:
    """Return whether the durable provider cutover journal is complete."""

    journal = MigrationJournal(CutoverPaths.for_data_dir(data_dir).journal).read()
    return bool(journal and journal.get("phase") == "complete")


def provider_cutover_needed(database: Path | str) -> bool:
    path = Path(database)
    if not path.is_file():
        return False
    connection = sqlite3.connect(str(path), timeout=10)
    try:
        tables = _tables(connection)
        return any(
            _table_count(connection, table) > 0
            for table in LEGACY_PROVIDER_TABLES.intersection(tables)
        )
    finally:
        connection.close()


def _assert_schema_resumable(paths: CutoverPaths, journal: Mapping[str, Any] | None) -> None:
    if not paths.database.is_file():
        return
    connection = sqlite3.connect(str(paths.database), timeout=10)
    try:
        tables = _tables(connection)
        present = NORMALIZED_PROVIDER_TABLES.intersection(tables)
        if present and present != NORMALIZED_PROVIDER_TABLES:
            raise ProviderMigrationError(
                "normalized provider schema is partial; restore the verified rollback archive"
            )
        normalized_rows = sum(_table_count(connection, table) for table in present)
        legacy_rows = sum(
            _table_count(connection, table)
            for table in LEGACY_PROVIDER_TABLES.intersection(tables)
        )
        if normalized_rows and legacy_rows and journal is None:
            raise ProviderMigrationError(
                "mixed legacy and normalized provider data has no migration journal"
            )
    finally:
        connection.close()


def _discard_empty_normalized_draft(paths: CutoverPaths) -> int:
    """Remove a pre-release normalized draft schema only when it has no data.

    ``create_all`` cannot add columns to a table created by an earlier
    development build.  Empty draft tables have no authority to preserve, so
    rebuilding them from the checked-in schema is deterministic.  Any row in
    the draft makes this function a no-op and the mixed-state guard fails
    closed instead.
    """

    if not paths.database.is_file():
        return 0
    connection = sqlite3.connect(str(paths.database), timeout=10)
    try:
        present = NORMALIZED_PROVIDER_TABLES.intersection(_tables(connection))
        if not present or any(_table_count(connection, table) for table in present):
            return 0
        connection.execute("PRAGMA foreign_keys = OFF")
        for table in sorted(present):
            connection.execute(f'DROP TABLE "{table}"')
        connection.commit()
        return len(present)
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def _atomic_private_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _plan_payload(plan: ProviderMappingPlan) -> dict[str, Any]:
    return {
        "schema_version": PROVIDER_CUTOVER_VERSION,
        "connections": [asdict(value) for value in plan.connections],
        "accounts": [asdict(value) for value in plan.accounts],
        "routes": [asdict(value) for value in plan.routes],
        "entitlements": [list(value) for value in plan.entitlements],
        "shares": [asdict(value) for value in plan.shares],
        "aliases": [asdict(value) for value in plan.aliases],
        "references": [asdict(value) for value in plan.references],
        "route_bindings": [asdict(value) for value in plan.route_bindings],
        "blockers": list(plan.blockers),
        "warnings": list(plan.warnings),
    }


def freeze_mapping_plan(path: Path, plan: ProviderMappingPlan) -> str:
    """Persist the secret-bearing frozen plan only as authenticated ciphertext."""

    plaintext = (
        json.dumps(_plan_payload(plan), sort_keys=True, separators=(",", ":")) + "\n"
    ).encode("utf-8")
    encrypted = PLAN_MAGIC + _get_fernet().encrypt(plaintext)
    _atomic_private_write(path, encrypted)
    return hashlib.sha256(encrypted).hexdigest()


def load_frozen_mapping_plan(path: Path) -> ProviderMappingPlan:
    raw = path.read_bytes()
    if not raw.startswith(PLAN_MAGIC):
        raise ProviderMigrationError("frozen provider plan has an invalid header")
    try:
        payload = json.loads(_get_fernet().decrypt(raw[len(PLAN_MAGIC) :]))
    except Exception as exc:
        raise ProviderMigrationError("frozen provider plan authentication failed") from exc
    if payload.get("schema_version") != PROVIDER_CUTOVER_VERSION:
        raise ProviderMigrationError("frozen provider plan schema is incompatible")
    try:
        return ProviderMappingPlan(
            connections=[PlannedConnection(**value) for value in payload["connections"]],
            accounts=[PlannedAccount(**value) for value in payload["accounts"]],
            routes=[PlannedRoute(**value) for value in payload["routes"]],
            entitlements=[tuple(value) for value in payload["entitlements"]],
            shares=[PlannedShare(**value) for value in payload["shares"]],
            aliases=[PlannedAlias(**value) for value in payload["aliases"]],
            references=[PlannedReference(**value) for value in payload["references"]],
            route_bindings=[PlannedRouteBinding(**value) for value in payload["route_bindings"]],
            blockers=list(payload.get("blockers") or []),
            warnings=list(payload.get("warnings") or []),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ProviderMigrationError("frozen provider plan is malformed") from exc


def _parse_database_timestamp(value: Any) -> float:
    if not value:
        return 0.0
    text = str(value).replace("Z", "+00:00")
    try:
        from datetime import datetime

        return datetime.fromisoformat(text).timestamp()
    except (TypeError, ValueError, OSError):
        return 0.0


def reconcile_legacy_runtime_credentials(paths: CutoverPaths) -> int:
    """Mirror only newer, valid generation auth caches into encrypted SQLite.

    Owner directory names are keyed hashes.  We derive those hashes solely
    from owners already present in the encrypted legacy table and never infer
    or log an identity from a filesystem name.
    """

    runtime = paths.data_dir / "runtime" / "agent-engine"
    if not runtime.is_dir() or not paths.database.is_file():
        return 0
    connection = sqlite3.connect(str(paths.database), timeout=30)
    connection.row_factory = sqlite3.Row
    updated = 0
    try:
        if "mimo_auth_store" not in _tables(connection):
            return 0
        columns = {
            str(row[1])
            for row in connection.execute('PRAGMA table_info("mimo_auth_store")')
        }
        timestamp_column = "updated_at" if "updated_at" in columns else None
        selected = "owner, payload" + (", updated_at" if timestamp_column else "")
        for row in connection.execute(f"SELECT {selected} FROM mimo_auth_store"):
            owner = str(row["owner"] or "")
            owner_key = hashlib.sha256(owner.encode("utf-8")).hexdigest()
            candidates = list(
                (runtime / owner_key / "generations").glob("*/mimocode/data/auth.json")
            )
            if not candidates:
                continue
            newest = max(candidates, key=lambda value: value.stat().st_mtime)
            stored_time = _parse_database_timestamp(
                row["updated_at"] if timestamp_column else None
            )
            if newest.stat().st_mtime <= stored_time:
                continue
            try:
                raw = newest.read_text(encoding="utf-8")
                decoded = json.loads(raw)
            except (OSError, UnicodeError, ValueError):
                raise ProviderMigrationError(
                    "newest managed provider credential cache is malformed"
                ) from None
            if not isinstance(decoded, dict):
                raise ProviderMigrationError(
                    "newest managed provider credential cache is malformed"
                )
            encrypted = encrypt(json.dumps(decoded, sort_keys=True, separators=(",", ":")))
            assignments = "payload = ?"
            values: list[Any] = [encrypted]
            if timestamp_column:
                assignments += ", updated_at = CURRENT_TIMESTAMP"
            values.append(owner)
            connection.execute(
                f"UPDATE mimo_auth_store SET {assignments} WHERE owner = ?",
                values,
            )
            updated += 1
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
    return updated


_SECRET_KEY_RE = re.compile(
    r"(?:authorization|api[_-]?key|access[_-]?token|refresh[_-]?token|credential|secret)",
    re.IGNORECASE,
)
_SETUP_PROVIDER_RE = re.compile(r"^\s*/setup\s+provider\s+\S+", re.IGNORECASE)


def _scrub_json(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(key): _scrub_json(item)
            for key, item in value.items()
            if not _SECRET_KEY_RE.search(str(key))
        }
    if isinstance(value, list):
        return [_scrub_json(item) for item in value]
    if isinstance(value, str) and value.lower().startswith("bearer "):
        return "[provider credential removed]"
    return value


def _safe_historical_url(value: Any) -> str:
    raw = str(value or "")
    try:
        parsed = urlsplit(raw)
    except ValueError:
        return ""
    if not parsed.scheme or not parsed.hostname:
        return raw if "@" not in raw and "?" not in raw else ""
    host = parsed.hostname
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    if parsed.port:
        host = f"{host}:{parsed.port}"
    return urlunsplit((parsed.scheme, host, parsed.path, "", ""))


def _scrub_json_column(
    connection: sqlite3.Connection,
    *,
    table: str,
    column: str,
    row_id: str = "id",
) -> int:
    columns = {
        str(row[1]) for row in connection.execute(f'PRAGMA table_info("{table}")')
    }
    if column not in columns or row_id not in columns:
        return 0
    changed = 0
    for identity, raw in connection.execute(
        f'SELECT "{row_id}", "{column}" FROM "{table}" WHERE "{column}" IS NOT NULL'
    ):
        if isinstance(raw, (dict, list)):
            parsed = raw
        else:
            try:
                parsed = json.loads(str(raw))
            except (TypeError, ValueError):
                continue
        scrubbed = _scrub_json(parsed)
        if scrubbed != parsed:
            connection.execute(
                f'UPDATE "{table}" SET "{column}" = ? WHERE "{row_id}" = ?',
                (json.dumps(scrubbed, sort_keys=True, separators=(",", ":")), identity),
            )
            changed += 1
    return changed


def _drop_legacy_column(
    connection: sqlite3.Connection,
    *,
    table: str,
    column: str,
) -> bool:
    """Drop one retired selector column and any index that depends on it."""

    columns = {
        str(row[1]) for row in connection.execute(f'PRAGMA table_info("{table}")')
    }
    if column not in columns:
        return False
    for row in connection.execute(f'PRAGMA index_list("{table}")').fetchall():
        index_name = str(row[1])
        indexed = {
            str(value[2])
            for value in connection.execute(
                f'PRAGMA index_info("{index_name}")'
            ).fetchall()
        }
        if column in indexed:
            escaped = index_name.replace('"', '""')
            connection.execute(f'DROP INDEX IF EXISTS "{escaped}"')
    connection.execute(f'ALTER TABLE "{table}" DROP COLUMN "{column}"')
    return True


def scrub_legacy_provider_state(paths: CutoverPaths) -> dict[str, int]:
    """Scrub plaintext remnants and remove retired provider authorities."""

    connection = sqlite3.connect(str(paths.database), timeout=30)
    scrubbed_json = 0
    scrubbed_messages = 0
    dropped = 0
    try:
        connection.execute("PRAGMA foreign_keys = OFF")
        connection.execute("PRAGMA secure_delete = ON")
        connection.execute("BEGIN IMMEDIATE")
        tables = _tables(connection)
        if "sessions" in tables:
            scrubbed_json += _scrub_json_column(
                connection, table="sessions", column="headers"
            )
            scrubbed_json += _scrub_json_column(
                connection, table="sessions", column="mimo_state"
            )
            columns = {
                str(row[1]) for row in connection.execute('PRAGMA table_info("sessions")')
            }
            if "endpoint_url" in columns:
                for identity, raw in connection.execute(
                    "SELECT id, endpoint_url FROM sessions WHERE endpoint_url IS NOT NULL"
                ):
                    safe = _safe_historical_url(raw)
                    if safe != str(raw or ""):
                        connection.execute(
                            "UPDATE sessions SET endpoint_url = ? WHERE id = ?",
                            (safe, identity),
                        )
                        scrubbed_json += 1
        if "chat_messages" in tables:
            for identity, content in connection.execute(
                "SELECT id, content FROM chat_messages WHERE content LIKE '/setup provider %'"
            ):
                if _SETUP_PROVIDER_RE.match(str(content or "")):
                    connection.execute(
                        "UPDATE chat_messages SET content = ? WHERE id = ?",
                        ("/setup provider [credential removed during provider cutover]", identity),
                    )
                    scrubbed_messages += 1
            scrubbed_json += _scrub_json_column(
                connection, table="chat_messages", column="metadata"
            )
        # Stable normalized route IDs are now the only executable selectors.
        # The old URL/endpoint columns are retired rather than left as a
        # dangling FK to the soon-to-be-dropped ModelEndpoint authority.
        for table in ("sessions", "scheduled_tasks", "crew_members"):
            if table not in tables:
                continue
            for column in ("endpoint_id", "endpoint_url"):
                _drop_legacy_column(connection, table=table, column=column)
            # Transitional display-only compatibility for the large canonical
            # session/task domain.  These no longer carry a FK, URL, key, or
            # executable selector; all rows are NULL and execution is bound by
            # provider_model_route_id.  They can be removed mechanically once
            # old response serializers stop emitting the fields.
            connection.execute(
                f'ALTER TABLE "{table}" ADD COLUMN endpoint_id TEXT'
            )
            connection.execute(
                f'ALTER TABLE "{table}" ADD COLUMN endpoint_url TEXT'
            )
        if "comparisons" in tables:
            for column in ("endpoint_a", "endpoint_b"):
                _drop_legacy_column(connection, table="comparisons", column=column)
            connection.execute(
                'ALTER TABLE "comparisons" ADD COLUMN endpoint_a TEXT'
            )
            connection.execute(
                'ALTER TABLE "comparisons" ADD COLUMN endpoint_b TEXT'
            )
        for table in (
            "model_share_subscriptions",
            "model_shares",
            "model_capabilities",
            "mimo_model_prefs",
            "mimo_projection_states",
            "mimo_auth_store",
            "model_endpoints",
            "provider_auth_sessions",
        ):
            if table in tables:
                connection.execute(f'DROP TABLE "{table}"')
                dropped += 1
        connection.commit()
        connection.execute("VACUUM")
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            raise ProviderMigrationError("provider database failed post-cutover integrity check")
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()

    for path in (paths.settings, paths.user_prefs):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            continue
        except (OSError, UnicodeError, ValueError) as exc:
            raise ProviderMigrationError(
                f"cannot scrub malformed provider-bearing file {path.name}"
            ) from exc
        scrubbed = _scrub_json(payload)
        _atomic_private_write(
            path,
            (json.dumps(scrubbed, sort_keys=True, indent=2) + "\n").encode("utf-8"),
        )
    paths.embedding.unlink(missing_ok=True)

    runtime = (paths.data_dir / "runtime" / "agent-engine").resolve()
    if runtime.is_relative_to(paths.data_dir) and runtime.is_dir():
        for auth in runtime.rglob("auth.json"):
            if auth.is_file() and auth.resolve().is_relative_to(runtime):
                auth.unlink()
        for owner_root in runtime.iterdir():
            generations = owner_root / "generations"
            if owner_root.is_dir() and generations.is_dir():
                shutil.rmtree(generations)
    return {
        "legacy_tables_dropped": dropped,
        "json_records_scrubbed": scrubbed_json,
        "setup_credentials_scrubbed": scrubbed_messages,
    }


def verify_cutover_state(paths: CutoverPaths) -> dict[str, int]:
    connection = sqlite3.connect(str(paths.database), timeout=30)
    try:
        tables = _tables(connection)
        remaining = LEGACY_PROVIDER_TABLES.intersection(tables)
        if remaining:
            raise ProviderMigrationError(
                "legacy provider tables remain after cutover: " + ", ".join(sorted(remaining))
            )
        missing = NORMALIZED_PROVIDER_TABLES.difference(tables)
        if missing:
            raise ProviderMigrationError(
                "normalized provider tables are missing after cutover: "
                + ", ".join(sorted(missing))
            )
        _verify_activation_schema(connection, tables)
        _verify_activation_connections(connection)
        integrity = connection.execute("PRAGMA foreign_key_check").fetchall()
        if integrity:
            raise ProviderMigrationError("provider cutover left foreign-key violations")
        return {
            "connections": _table_count(connection, "provider_connections"),
            "accounts": _table_count(connection, "provider_accounts"),
            "model_routes": _table_count(connection, "provider_model_routes"),
            "share_grants": _table_count(connection, "provider_share_grants"),
        }
    finally:
        connection.close()


def run_provider_cutover(
    *,
    data_dir: Path | str,
    explicit_owner: str | None = None,
    auth_enabled: bool = True,
    verify_engine: bool = True,
) -> CutoverResult:
    """Run or resume the provider migration before importing the application."""

    paths = CutoverPaths.for_data_dir(data_dir)
    paths.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    journal = MigrationJournal(paths.journal)
    current = journal.read()
    if current is None:
        _discard_empty_normalized_draft(paths)
    _assert_schema_resumable(paths, current)
    if current and current["phase"] == "complete":
        counts = verify_cutover_state(paths)
        return CutoverResult(True, True, "complete", counts=counts)
    if current is None and not provider_cutover_needed(paths.database):
        return CutoverResult(False, True, "not_required")

    if verify_engine:
        from src.openclank.engine_build import ensure_engine_ready

        ensure_engine_ready().require()

    with MigrationLock(paths.lock):
        current = journal.read()
        details = dict((current or {}).get("details") or {})
        phase = (current or {}).get("phase")
        paths.migration_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        _atomic_private_write(paths.active, b"provider-cutover-v1\n")
        try:
            if phase is None:
                preflight = provider_migration_preflight(
                    database_path=paths.database,
                    auth_file=paths.auth_file,
                    explicit_owner=explicit_owner,
                    auth_enabled=auth_enabled,
                    embedding_path=paths.embedding,
                    settings_path=paths.settings,
                )
                if not preflight.ready:
                    raise ProviderMigrationError("; ".join(preflight.blockers))
                details.update(
                    owner_assignment=preflight.owner_assignment,
                    inventory=preflight.safe_report()["inventory"],
                    warnings=list(preflight.warnings),
                )
                current = journal.advance("preflight", **details)
                phase = current["phase"]

            if phase == "preflight":
                reconciled = reconcile_legacy_runtime_credentials(paths)
                # The updater/server manager must have stopped the service
                # before invoking this pre-app bootstrap.  An exclusive SQLite
                # transaction catches any still-active writer without killing
                # an unrelated process.
                probe = sqlite3.connect(str(paths.database), timeout=1)
                try:
                    probe.execute("BEGIN EXCLUSIVE")
                    probe.rollback()
                except sqlite3.OperationalError as exc:
                    raise ProviderMigrationError(
                        "provider migration cannot drain active database writers"
                    ) from exc
                finally:
                    probe.close()
                details["runtime_credential_stores_reconciled"] = reconciled
                current = journal.advance("workers_drained", **details)
                phase = current["phase"]

            if phase == "workers_drained":
                snapshot = create_rollback_archive(paths.data_dir)
                verify_rollback_archive(snapshot.archive)
                details.update(
                    archive=snapshot.archive.name,
                    archive_sha256=snapshot.sha256,
                    archive_file_count=snapshot.file_count,
                )
                current = journal.advance("snapshot_verified", **details)
                phase = current["phase"]

            if phase == "snapshot_verified":
                plan = build_provider_mapping_plan(
                    database_path=paths.database,
                    owner_assignment=details.get("owner_assignment"),
                    settings_path=paths.settings,
                    user_prefs_path=paths.user_prefs,
                    embedding_path=paths.embedding,
                )
                if plan.blockers:
                    raise ProviderMigrationError("; ".join(plan.blockers))
                details["plan_sha256"] = freeze_mapping_plan(paths.plan, plan)
                details["plan_counts"] = plan.safe_report()["counts"]
                current = journal.advance("plan_frozen", **details)
                phase = current["phase"]

            if phase == "plan_frozen":
                plan = load_frozen_mapping_plan(paths.plan)
                counts = apply_provider_mapping_plan(
                    database_url=f"sqlite:///{paths.database}",
                    plan=plan,
                )
                details["applied_counts"] = counts
                current = journal.advance("transaction_committed", **details)
                phase = current["phase"]

            if phase == "transaction_committed":
                details["scrub_counts"] = scrub_legacy_provider_state(paths)
                current = journal.advance("legacy_scrubbed", **details)
                phase = current["phase"]

            if phase == "legacy_scrubbed":
                counts = verify_cutover_state(paths)
                details["verified_counts"] = counts
                current = journal.advance("complete", **details)
                phase = current["phase"]

            counts = dict(details.get("verified_counts") or verify_cutover_state(paths))
            return CutoverResult(
                needed=True,
                complete=phase == "complete",
                phase=str(phase),
                archive=str(details.get("archive") or "") or None,
                counts=counts,
            )
        finally:
            if journal.read() and journal.read()["phase"] == "complete":
                paths.active.unlink(missing_ok=True)


__all__ = [
    "CutoverPaths",
    "CutoverResult",
    "NORMALIZED_PROVIDER_TABLES",
    "cutover_complete",
    "freeze_mapping_plan",
    "load_frozen_mapping_plan",
    "provider_cutover_needed",
    "reconcile_legacy_runtime_credentials",
    "run_provider_cutover",
    "scrub_legacy_provider_state",
    "verify_cutover_state",
]
