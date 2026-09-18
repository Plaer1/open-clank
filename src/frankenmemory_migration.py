"""Idempotent legacy-to-v2 migration planner and applicator.

The runner is intentionally explicit: it never runs at app startup, never
guesses an owner, and never replaces the legacy read path.  Plan rows freeze a
source fingerprint; apply refuses stale rows and appends an immutable event.

Write quiesce is required: run ``plan`` and ``apply`` only while no memory
writer (the app, fm-mcp, importers) is attached to the database.  Apply
recomputes the planner watermark before and after the apply loop and fails
closed on any source drift, so a concurrent write aborts the run instead of
being captured — stop writers, re-plan, and re-apply.  (An earlier draft
carried a legacy-outbox replay channel for online capture; it never gained a
production producer and was removed on 2026-08-20 rather than shipped
half-wired — see the S05 robonote.)
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


CONVERSION_VERSION = "legacy-to-v2-2026-08-02"
SUPPORTED_TABLES = (
    "curated",
    "raw",
    "facts",
    "candidates",
    "graph_nodes",
    "graph_edges",
    "graph_cues",
    "memory_quarantine",
    "memory_tombstones",
    "memory_retention_policy",
    "memory_retention_operations",
    "memory_leases",
)


class MigrationError(RuntimeError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def _hash(value: Any) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _row_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {key: row[key] for key in row.keys()}


def _source_fingerprint(row: sqlite3.Row) -> str:
    return _hash({"columns": list(row.keys()), "values": _row_dict(row)})


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone() is not None


def _db_identity(conn: sqlite3.Connection) -> tuple[str, int]:
    try:
        database_id = conn.execute("SELECT value FROM fm_meta WHERE key='database_id'").fetchone()[0]
        version = int(conn.execute("PRAGMA user_version").fetchone()[0])
    except (sqlite3.Error, TypeError, IndexError) as exc:
        raise MigrationError("database identity/schema is unavailable") from exc
    if version < 11:
        raise MigrationError("v2 schema migration 11 is not installed")
    return str(database_id), version


def _key_columns(conn: sqlite3.Connection, table: str) -> list[str]:
    columns = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return [
        str(row[1])
        for row in sorted((row for row in columns if int(row[5] or 0) > 0), key=lambda row: int(row[5]))
    ]


def _source_key(row: sqlite3.Row, key_columns: list[str]) -> str:
    values = [row[column] for column in key_columns]
    return str(values[0]) if len(values) == 1 else _json(values)


def _source_rows(conn: sqlite3.Connection) -> Iterable[tuple[str, str, sqlite3.Row]]:
    for table in SUPPORTED_TABLES:
        key_columns = _key_columns(conn, table)
        if not key_columns:
            continue
        order = ",".join(f'"{column}"' for column in key_columns)
        for row in conn.execute(f"SELECT * FROM {table} ORDER BY {order}"):
            yield table, _source_key(row, key_columns), row


def _row_for_table(conn: sqlite3.Connection, table: str, key: str) -> sqlite3.Row | None:
    if table not in SUPPORTED_TABLES:
        raise MigrationError(f"unsupported source table: {table}")
    key_columns = _key_columns(conn, table)
    if not key_columns:
        return None
    if len(key_columns) == 1:
        values: list[Any] = [key]
    else:
        try:
            values = json.loads(key)
        except json.JSONDecodeError as exc:
            raise MigrationError(f"invalid composite source key for {table}") from exc
        if not isinstance(values, list) or len(values) != len(key_columns):
            raise MigrationError(f"invalid composite source key for {table}")
    where = " AND ".join(f'"{column}"=?' for column in key_columns)
    return conn.execute(f"SELECT * FROM {table} WHERE {where}", values).fetchone()


def _scope(row: sqlite3.Row) -> tuple[str | None, str, str]:
    keys = set(row.keys())
    owner = str(row["owner"]).strip() if "owner" in keys and row["owner"] is not None else None
    workspace = str(row["workspace_id"]).strip() if "workspace_id" in keys and row["workspace_id"] is not None else ""
    return owner or None, workspace, ""


def plan_migration(
    db_path: str | Path,
    *,
    conversion_version: str = CONVERSION_VERSION,
    backup_path: str | Path | None = None,
) -> str:
    """Freeze one deterministic migration plan and return its run ID."""
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA foreign_keys=ON")
        database_id, schema_version = _db_identity(conn)
        now = _now()
        source_rows = list(_source_rows(conn))
        # The migration ID is derived from every source revision, not a MAX()
        # watermark whose value can stay unchanged while an older row changes.
        high_water = _hash([(table, key, _source_fingerprint(row)) for table, key, row in source_rows])
        backup_name = backup_digest = None
        if backup_path is not None:
            backup = Path(backup_path).expanduser().resolve()
            if not backup.is_file():
                raise MigrationError("verified backup path is not a file")
            backup_name = str(backup)
            backup_digest = _file_hash(backup)
        run_id = "mig_" + _hash(
            [database_id, schema_version, high_water, conversion_version, backup_digest]
        )[:32]
        existing = conn.execute("SELECT migration_run_id FROM fm_v2_migration_runs WHERE migration_run_id=?", (run_id,)).fetchone()
        if existing:
            return run_id
        rows: list[tuple[Any, ...]] = []
        for table, key, row in source_rows:
            owner, workspace, project = _scope(row)
            if owner is None:
                classification = "ambiguous_scope"
                targets: list[str] = []
                activation = "quarantine"
                rule = "owner-required-v1"
            elif table == "curated" and _valid_tags(row["tags"] if "tags" in row.keys() else None):
                block = f"kb_legacy_{key}"
                entity = f"entity_legacy_{owner}_{key}"
                classification = "exact" if not int(row["archived"] or 0) else "historical"
                targets = [entity, block]
                activation = "grandfathered_active" if classification == "exact" else "historical"
                rule = "curated-record-v1"
            elif table == "curated":
                classification = "invalid"
                targets = []
                activation = "quarantine"
                rule = "curated-invalid-payload-v1"
            else:
                classification = "unsupported_preserved"
                targets = []
                activation = "no_activation"
                rule = f"{table}-evidence-only-v1"
            rows.append((run_id, table, key, owner, workspace, project, _source_fingerprint(row), _json(targets), rule, classification, activation, now))
        manifest_hash = _hash(rows)
        with conn:
            conn.execute(
                "INSERT INTO fm_v2_migration_runs(migration_run_id,source_database_id,source_schema_version,source_high_water_mark,conversion_version,manifest_hash,backup_path,backup_hash,state,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?, 'planned', ?,?)",
                (run_id, database_id, schema_version, high_water, conversion_version, manifest_hash, backup_name, backup_digest, now, now),
            )
            conn.executemany(
                "INSERT INTO fm_v2_migration_plan(migration_run_id,source_table,source_key,owner_id,workspace_key,project_key,source_fingerprint,planned_target_ids,conversion_rule,classification,activation_treatment,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                rows,
            )
        return run_id
    finally:
        conn.close()


def _valid_tags(value: Any) -> bool:
    """Validate legacy tag JSON without allowing one bad row to abort planning."""
    if value in (None, "", "null"):
        return True
    try:
        parsed = json.loads(str(value))
    except (TypeError, json.JSONDecodeError):
        return False
    return isinstance(parsed, list) and all(isinstance(item, str) for item in parsed)


def _event_hash(previous: str | None, payload: dict[str, Any]) -> str:
    return _hash({"previous_event_hash": previous, **payload})


def _source_snapshot(conn: sqlite3.Connection) -> str:
    """Recompute the planner watermark from every supported source row."""
    rows = [
        (table, key, _source_fingerprint(row))
        for table, key, row in _source_rows(conn)
    ]
    return _hash(rows)


def _verify_event_chain(conn: sqlite3.Connection, run_id: str) -> None:
    """Reject a tampered/partially copied migration event stream."""
    previous: str | None = None
    events = conn.execute(
        "SELECT * FROM fm_v2_migration_events WHERE migration_run_id=? ORDER BY sequence",
        (run_id,),
    ).fetchall()
    for event in events:
        if event["previous_event_hash"] != previous:
            raise MigrationError(f"migration event chain is broken at sequence {event['sequence']}")
        try:
            target_ids = json.loads(event["target_ids"] or "[]")
        except json.JSONDecodeError as exc:
            raise MigrationError("migration event target_ids is invalid JSON") from exc
        payload = {
            "migration_run_id": run_id,
            "plan_id": event["plan_id"],
            "sequence": event["sequence"],
            "phase": event["phase"],
            "attempt": event["attempt"],
            "checkpoint": event["checkpoint"],
            "target_ids": target_ids,
            "apply_result": event["apply_result"],
            "validation_result": event["validation_result"],
            "actor": event["actor"],
        }
        expected = _event_hash(previous, payload)
        if event["event_hash"] != expected:
            raise MigrationError(f"migration event hash is invalid at sequence {event['sequence']}")
        previous = event["event_hash"]


def _apply_curated(conn: sqlite3.Connection, plan: sqlite3.Row, row: sqlite3.Row, now: str) -> list[str]:
    owner = str(plan["owner_id"])
    key = str(plan["source_key"])
    entity_id = f"entity_legacy_{owner}_{key}"
    block_id = f"kb_legacy_{key}"
    change_set = f"cs_{plan['migration_run_id']}_{plan['plan_id']}"
    source_id = f"source_legacy_curated_{key}"
    content = str(row["content"] or "")
    archived = bool(row["archived"] or 0)
    status = "retracted" if archived else "active"
    payload = {"entity_type": "legacy_record", "canonical_label": key, "aliases": [], "roles": [], "tags": json.loads(row["tags"] or "[]")}
    value = {"type": "string", "value": content}
    revision_payload = {
        "value": value,
        "kind": str(row["kind"] or row["scene_name"] or "fact"),
        "status": status,
        "source_id": source_id,
        "legacy_id": key,
    }
    conn.execute(
        "INSERT OR IGNORE INTO fm_v2_change_sets(owner_id,change_set_id,actor_type,actor_id,reason,source_event_id,contract_version,content_hash,created_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (owner, change_set, "migration", "open-clank", "legacy migration", key, "frankenmemory.v2", _hash(revision_payload), now),
    )
    conn.execute(
        "INSERT OR IGNORE INTO fm_v2_sources(owner_id,source_id,workspace_key,project_key,source_uri,source_revision,content_hash,source_type,created_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (owner, source_id, str(plan["workspace_key"]), str(plan["project_key"]), f"legacy://curated/{key}", 1, _source_fingerprint(row), "legacy", now),
    )
    conn.execute(
        "INSERT OR IGNORE INTO fm_v2_evidence(owner_id,evidence_id,source_id,source_revision,locator_type,locator_json,quote,extraction_contract,parser_version,raw_value_json,normalized_value_json,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (owner, f"ev_{source_id}", source_id, 1, "legacy_row", _json({"table": "curated", "id": key}), content, "legacy-migration", "legacy", _json(content), _json(content), now),
    )
    conn.execute(
        "INSERT OR IGNORE INTO fm_v2_entities(owner_id,entity_id,workspace_key,project_key,created_change_set_id,current_revision,created_at) VALUES (?,?,?,?,?,?,?)",
        (owner, entity_id, str(plan["workspace_key"]), str(plan["project_key"]), change_set, 1, now),
    )
    conn.execute(
        "INSERT OR IGNORE INTO fm_v2_entity_revisions(owner_id,entity_id,revision,payload,content_hash,actor_type,actor_id,reason,change_set_id,created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (owner, entity_id, 1, _json(payload), _hash(payload), "migration", "open-clank", "legacy migration", change_set, now),
    )
    conn.execute(
        "INSERT OR IGNORE INTO fm_v2_knowledge_blocks(owner_id,block_id,workspace_key,project_key,subject_entity_id,predicate,claim_slot,cardinality,created_change_set_id,current_revision,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (owner, block_id, str(plan["workspace_key"]), str(plan["project_key"]), entity_id, "legacy_text", key, "single", change_set, 1, now),
    )
    conn.execute(
        "INSERT OR IGNORE INTO fm_v2_knowledge_revisions(owner_id,block_id,revision,value_type,value_json,expected_value_type,kind,tags_json,status,confidence,trust,activation,activation_rationale,evidence_json,content_hash,actor_type,actor_id,reason,source_event_id,change_set_id,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (owner, block_id, 1, "string", _json(value), "string", str(row["kind"] or "fact"), str(row["tags"] or "[]"), status, float(row["confidence_score"] or 0), float(row["trust_score"] or 0), str(plan["activation_treatment"]), "legacy scoped record", _json([f"ev_{source_id}"]), _hash(revision_payload), "migration", "open-clank", "legacy migration", key, change_set, now),
    )
    conn.execute(
        "INSERT OR IGNORE INTO fm_v2_outbox(owner_id,event_id,change_set_id,kind,payload_json,source_revision,created_at) VALUES (?,?,?,?,?,?,?)",
        (owner, f"outbox_{change_set}", change_set, "legacy_migrated", _json({"owner_id": owner, "block_id": block_id, "source_id": source_id}), 1, now),
    )
    return [entity_id, block_id, source_id]


def apply_migration(db_path: str | Path, run_id: str) -> dict[str, int | str]:
    """Apply a frozen plan once; stale rows and unknown owners stay visible."""
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA foreign_keys=ON")
        database_id, schema_version = _db_identity(conn)
        run = conn.execute("SELECT * FROM fm_v2_migration_runs WHERE migration_run_id=?", (run_id,)).fetchone()
        if run is None:
            raise MigrationError(f"unknown migration run: {run_id}")
        if str(run["source_database_id"]) != database_id or int(run["source_schema_version"]) != schema_version:
            raise MigrationError("migration plan belongs to a different database identity or schema")
        if "backup_path" in run.keys() and run["backup_path"]:
            backup = Path(str(run["backup_path"]))
            if not backup.is_file() or _file_hash(backup) != str(run["backup_hash"] or ""):
                raise MigrationError("verified migration backup is missing or changed")
        if _source_snapshot(conn) != str(run["source_high_water_mark"]):
            raise MigrationError("migration source changed after the plan was frozen")
        manifest_rows = conn.execute(
            "SELECT migration_run_id,source_table,source_key,owner_id,workspace_key,project_key,source_fingerprint,planned_target_ids,conversion_rule,classification,activation_treatment,created_at FROM fm_v2_migration_plan WHERE migration_run_id=? ORDER BY plan_id",
            (run_id,),
        ).fetchall()
        if _hash([tuple(row) for row in manifest_rows]) != str(run["manifest_hash"]):
            raise MigrationError("migration manifest hash does not match its plan rows")
        _verify_event_chain(conn, run_id)
        applied = skipped = quarantined = 0
        with conn:
            conn.execute("UPDATE fm_v2_migration_runs SET state='applying',updated_at=? WHERE migration_run_id=? AND state IN ('planned','applying','paused')", (_now(), run_id))
        plans = conn.execute("SELECT * FROM fm_v2_migration_plan WHERE migration_run_id=? ORDER BY plan_id", (run_id,)).fetchall()
        for plan in plans:
            result = ""
            with conn:
                conn.execute("BEGIN IMMEDIATE")
                if conn.execute("SELECT 1 FROM fm_v2_migration_events WHERE plan_id=?", (plan["plan_id"],)).fetchone():
                    result = "already_applied"
                else:
                    now = _now()
                    row = _row_for_table(conn, plan["source_table"], plan["source_key"])
                    if row is None or _source_fingerprint(row) != plan["source_fingerprint"]:
                        raise MigrationError(f"stale migration source: {plan['source_table']}:{plan['source_key']}")
                    if plan["classification"] in {"ambiguous_scope", "invalid"}:
                        result, targets = "quarantined", []
                    elif plan["classification"] == "exact" and plan["source_table"] == "curated":
                        targets = _apply_curated(conn, plan, row, now)
                        result = "applied"
                    else:
                        result, targets = "preserved", []
                    previous = conn.execute("SELECT event_hash FROM fm_v2_migration_events WHERE migration_run_id=? ORDER BY sequence DESC LIMIT 1", (run_id,)).fetchone()
                    previous_hash = previous[0] if previous else None
                    sequence = int(conn.execute("SELECT COALESCE(MAX(sequence),0)+1 FROM fm_v2_migration_events WHERE migration_run_id=?", (run_id,)).fetchone()[0])
                    payload = {"migration_run_id": run_id, "plan_id": plan["plan_id"], "sequence": sequence, "phase": "apply", "attempt": 1, "checkpoint": str(plan["plan_id"]), "target_ids": targets, "apply_result": result, "validation_result": "scope_and_source_fingerprint_checked", "actor": "open-clank"}
                    conn.execute(
                        "INSERT INTO fm_v2_migration_events(migration_run_id,event_id,plan_id,sequence,phase,attempt,checkpoint,target_ids,apply_result,validation_result,actor,previous_event_hash,event_hash,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (run_id, f"evt_{uuid.uuid4().hex}", plan["plan_id"], sequence, "apply", 1, str(plan["plan_id"]), _json(targets), result, "scope_and_source_fingerprint_checked", "open-clank", previous_hash, _event_hash(previous_hash, payload), now),
                    )
            if result == "already_applied":
                skipped += 1
            elif result == "applied":
                applied += 1
            elif result == "quarantined":
                quarantined += 1
            else:
                skipped += 1
        with conn:
            conn.execute("BEGIN IMMEDIATE")
            if _source_snapshot(conn) != str(run["source_high_water_mark"]):
                raise MigrationError("migration source changed during apply")
            conn.execute("UPDATE fm_v2_migration_runs SET state='applied',updated_at=? WHERE migration_run_id=?", (_now(), run_id))
        return {"migration_run_id": run_id, "applied": applied, "skipped": skipped, "quarantined": quarantined}
    finally:
        conn.close()


def shadow_compare(db_path: str | Path, run_id: str) -> dict[str, Any]:
    """Compare legacy curated membership/lifecycle/content to its v2 projection."""
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        if conn.execute(
            "SELECT 1 FROM fm_v2_migration_runs WHERE migration_run_id=?", (run_id,)
        ).fetchone() is None:
            raise MigrationError(f"unknown migration run: {run_id}")
        differences: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()
        if _table_exists(conn, "curated"):
            for row in conn.execute("SELECT * FROM curated ORDER BY owner,workspace_id,id"):
                owner, workspace, _ = _scope(row)
                if owner is None:
                    continue
                key = str(row["id"])
                seen.add((owner, key))
                canonical = conn.execute(
                    """SELECT b.workspace_key,b.project_key,r.value_json,r.status
                       FROM fm_v2_knowledge_blocks b
                       JOIN fm_v2_knowledge_revisions r
                         ON r.owner_id=b.owner_id AND r.block_id=b.block_id
                        AND r.revision=b.current_revision
                      WHERE b.owner_id=? AND b.block_id=?""",
                    (owner, f"kb_legacy_{key}"),
                ).fetchone()
                if canonical is None:
                    differences.append({"owner_id": owner, "source_key": key, "kind": "missing_canonical"})
                    continue
                typed = json.loads(str(canonical["value_json"]))
                expected = {
                    "workspace_key": workspace,
                    "status": "retracted" if bool(row["archived"]) else "active",
                    "text": str(row["content"] or ""),
                }
                actual = {
                    "workspace_key": str(canonical["workspace_key"]),
                    "status": str(canonical["status"]),
                    "text": str(typed.get("value") or "") if isinstance(typed, dict) else "",
                }
                if actual != expected:
                    differences.append({"owner_id": owner, "source_key": key, "kind": "projection_mismatch", "expected": expected, "actual": actual})
        for row in conn.execute(
            "SELECT owner_id,block_id FROM fm_v2_knowledge_blocks WHERE block_id LIKE 'kb_legacy_%' ORDER BY owner_id,block_id"
        ):
            key = str(row["block_id"])[len("kb_legacy_"):]
            if (str(row["owner_id"]), key) not in seen:
                differences.append({"owner_id": row["owner_id"], "source_key": key, "kind": "canonical_only"})
        return {
            "migration_run_id": run_id,
            "equivalent": not differences,
            "compared": len(seen),
            "differences": differences,
        }
    finally:
        conn.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("db_path")
    sub = parser.add_subparsers(dest="command", required=True)
    plan = sub.add_parser("plan")
    plan.add_argument("--backup")
    apply = sub.add_parser("apply")
    apply.add_argument("run_id")
    shadow = sub.add_parser("shadow-compare")
    shadow.add_argument("run_id")
    args = parser.parse_args()
    try:
        if args.command == "plan":
            result = plan_migration(args.db_path, backup_path=args.backup)
        elif args.command == "apply":
            result = apply_migration(args.db_path, args.run_id)
        else:
            result = shadow_compare(args.db_path, args.run_id)
    except MigrationError as exc:
        parser.error(str(exc))
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
