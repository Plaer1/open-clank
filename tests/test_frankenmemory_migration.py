import sqlite3

import pytest

import src.frankenmemory_migration as migration
from src.frankenmemory_migration import (
    MigrationError,
    apply_migration,
    plan_migration,
    shadow_compare,
)


def _empty_v2_db(path):
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE fm_meta(key TEXT PRIMARY KEY,value TEXT)")
        conn.execute("INSERT INTO fm_meta(key,value) VALUES ('database_id','db-a')")
        conn.execute("PRAGMA user_version=12")
        conn.executescript(
            """
            CREATE TABLE fm_v2_migration_runs(migration_run_id TEXT PRIMARY KEY, source_database_id TEXT, source_schema_version INTEGER, source_high_water_mark TEXT, conversion_version TEXT, manifest_hash TEXT, outbox_high_water_mark INTEGER NOT NULL DEFAULT 0, backup_path TEXT, backup_hash TEXT, state TEXT, created_at TEXT, updated_at TEXT);
            CREATE TABLE fm_v2_migration_plan(migration_run_id TEXT, plan_id INTEGER PRIMARY KEY AUTOINCREMENT, source_table TEXT, source_key TEXT, owner_id TEXT, workspace_key TEXT, project_key TEXT, source_fingerprint TEXT, planned_target_ids TEXT, conversion_rule TEXT, classification TEXT, activation_treatment TEXT, created_at TEXT);
            CREATE TABLE fm_v2_migration_events(migration_run_id TEXT, event_id TEXT PRIMARY KEY, plan_id INTEGER, sequence INTEGER, phase TEXT, attempt INTEGER, checkpoint TEXT, target_ids TEXT, apply_result TEXT, validation_result TEXT, exception_id TEXT, rollback_marker TEXT, actor TEXT, previous_event_hash TEXT, event_hash TEXT, created_at TEXT);
            CREATE TABLE fm_v2_legacy_outbox(sequence INTEGER PRIMARY KEY AUTOINCREMENT,event_id TEXT NOT NULL UNIQUE,source_table TEXT NOT NULL,source_key TEXT NOT NULL,operation TEXT NOT NULL,owner_id TEXT,workspace_key TEXT NOT NULL DEFAULT '',project_key TEXT NOT NULL DEFAULT '',source_revision TEXT NOT NULL,pre_fingerprint TEXT,post_fingerprint TEXT,idempotency_key TEXT NOT NULL UNIQUE,payload_version TEXT NOT NULL,payload_json TEXT,state TEXT NOT NULL DEFAULT 'pending',created_at TEXT NOT NULL,applied_at TEXT);
            CREATE TABLE fm_v2_migration_outbox_events(migration_run_id TEXT NOT NULL,outbox_sequence INTEGER NOT NULL,disposition TEXT NOT NULL,target_ids TEXT NOT NULL,event_hash TEXT NOT NULL,created_at TEXT NOT NULL,PRIMARY KEY(migration_run_id,outbox_sequence));
            """
        )


def _add_curated_and_targets(path):
    with sqlite3.connect(path) as conn:
        conn.executescript(
            """
            CREATE TABLE curated(id TEXT PRIMARY KEY, content TEXT, kind TEXT, scene_name TEXT, tags TEXT, archived INTEGER, confidence_score REAL, trust_score REAL, owner TEXT, workspace_id TEXT);
            CREATE TABLE fm_v2_change_sets(owner_id TEXT, change_set_id TEXT, actor_type TEXT, actor_id TEXT, reason TEXT, source_event_id TEXT, contract_version TEXT, content_hash TEXT, created_at TEXT, PRIMARY KEY(owner_id,change_set_id));
            CREATE TABLE fm_v2_sources(owner_id TEXT, source_id TEXT, workspace_key TEXT, project_key TEXT, source_uri TEXT, source_revision INTEGER, content_hash TEXT, source_type TEXT, created_at TEXT, PRIMARY KEY(owner_id,source_id,source_revision));
            CREATE TABLE fm_v2_evidence(owner_id TEXT, evidence_id TEXT, source_id TEXT, source_revision INTEGER, locator_type TEXT, locator_json TEXT, quote TEXT, extraction_contract TEXT, parser_version TEXT, raw_value_json TEXT, normalized_value_json TEXT, created_at TEXT, PRIMARY KEY(owner_id,evidence_id));
            CREATE TABLE fm_v2_entities(owner_id TEXT, entity_id TEXT, workspace_key TEXT, project_key TEXT, created_change_set_id TEXT, current_revision INTEGER, created_at TEXT, PRIMARY KEY(owner_id,entity_id));
            CREATE TABLE fm_v2_entity_revisions(owner_id TEXT, entity_id TEXT, revision INTEGER, payload TEXT, content_hash TEXT, actor_type TEXT, actor_id TEXT, reason TEXT, change_set_id TEXT, created_at TEXT, PRIMARY KEY(owner_id,entity_id,revision));
            CREATE TABLE fm_v2_knowledge_blocks(owner_id TEXT, block_id TEXT, workspace_key TEXT, project_key TEXT, subject_entity_id TEXT, predicate TEXT, claim_slot TEXT, cardinality TEXT, created_change_set_id TEXT, current_revision INTEGER, created_at TEXT, PRIMARY KEY(owner_id,block_id));
            CREATE TABLE fm_v2_knowledge_revisions(owner_id TEXT, block_id TEXT, revision INTEGER, value_type TEXT, value_json TEXT, expected_value_type TEXT, kind TEXT, tags_json TEXT, status TEXT, confidence REAL, trust REAL, activation TEXT, activation_rationale TEXT, evidence_json TEXT, content_hash TEXT, actor_type TEXT, actor_id TEXT, reason TEXT, source_event_id TEXT, change_set_id TEXT, previous_revision INTEGER, created_at TEXT, PRIMARY KEY(owner_id,block_id,revision));
            CREATE TABLE fm_v2_outbox(owner_id TEXT, event_id TEXT, change_set_id TEXT, kind TEXT, payload_json TEXT, source_revision INTEGER, created_at TEXT, PRIMARY KEY(owner_id,event_id));
            """
        )
        conn.execute(
            "INSERT INTO curated VALUES (?,?,?,?,?,?,?,?,?,?)",
            ("legacy-1", "legacy payload", "fact", None, "[]", 0, 0.9, 0.8, "alice", "global"),
        )


def test_apply_refuses_a_plan_from_another_database_identity(tmp_path):
    path = str(tmp_path / "migration.db")
    _empty_v2_db(path)
    run_id = plan_migration(path)
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE fm_meta SET value='db-b' WHERE key='database_id'")
    with pytest.raises(MigrationError, match="different database identity"):
        apply_migration(path, run_id)


def test_empty_plan_is_idempotent_and_commits_applied_state(tmp_path):
    path = str(tmp_path / "migration.db")
    _empty_v2_db(path)
    run_id = plan_migration(path)
    assert apply_migration(path, run_id)["migration_run_id"] == run_id
    assert apply_migration(path, run_id)["skipped"] == 0
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT state FROM fm_v2_migration_runs WHERE migration_run_id=?", (run_id,)).fetchone()[0] == "applied"


def test_plan_covers_composite_graph_cues_and_tombstones(tmp_path):
    path = str(tmp_path / "migration.db")
    _empty_v2_db(path)
    with sqlite3.connect(path) as conn:
        conn.executescript(
            """
            CREATE TABLE graph_cues(
                cue TEXT NOT NULL,
                node_id TEXT NOT NULL,
                owner TEXT,
                workspace_id TEXT NOT NULL,
                PRIMARY KEY(cue,node_id)
            );
            CREATE TABLE memory_tombstones(
                id TEXT PRIMARY KEY,
                owner TEXT NOT NULL,
                workspace_id TEXT NOT NULL,
                source_key TEXT NOT NULL
            );
            """
        )
        conn.execute(
            "INSERT INTO graph_cues VALUES (?,?,?,?)",
            ("name", "node-1", "alice", "global"),
        )
        conn.execute(
            "INSERT INTO memory_tombstones VALUES (?,?,?,?)",
            ("forget-1", "alice", "global", "source-1"),
        )

    run_id = plan_migration(path)
    with sqlite3.connect(path) as conn:
        rows = conn.execute(
            "SELECT source_table,source_key FROM fm_v2_migration_plan ORDER BY source_table"
        ).fetchall()
    assert rows == [
        ("graph_cues", '["name","node-1"]'),
        ("memory_tombstones", "forget-1"),
    ]
    result = apply_migration(path, run_id)
    assert result["skipped"] == 2
    with sqlite3.connect(path) as conn:
        assert conn.execute(
            "SELECT count(*) FROM fm_v2_migration_events WHERE migration_run_id=?",
            (run_id,),
        ).fetchone()[0] == 2


def test_converted_rows_roll_back_when_audit_event_cannot_be_written(tmp_path, monkeypatch):
    path = str(tmp_path / "migration.db")
    _empty_v2_db(path)
    _add_curated_and_targets(path)
    run_id = plan_migration(path)

    def fail_after_conversion():
        raise RuntimeError("simulated crash before audit event")

    monkeypatch.setattr(migration.uuid, "uuid4", fail_after_conversion)
    with pytest.raises(RuntimeError, match="before audit event"):
        apply_migration(path, run_id)

    with sqlite3.connect(path) as conn:
        for table in (
            "fm_v2_change_sets",
            "fm_v2_sources",
            "fm_v2_evidence",
            "fm_v2_entities",
            "fm_v2_entity_revisions",
            "fm_v2_knowledge_blocks",
            "fm_v2_knowledge_revisions",
            "fm_v2_outbox",
            "fm_v2_migration_events",
        ):
            assert conn.execute(f"SELECT count(*) FROM {table}").fetchone() == (0,)
        assert conn.execute(
            "SELECT state FROM fm_v2_migration_runs WHERE migration_run_id=?",
            (run_id,),
        ).fetchone() == ("applying",)


def test_converted_rows_and_audit_event_commit_once(tmp_path):
    path = str(tmp_path / "migration.db")
    _empty_v2_db(path)
    _add_curated_and_targets(path)
    run_id = plan_migration(path)

    assert apply_migration(path, run_id)["applied"] == 1
    assert apply_migration(path, run_id)["skipped"] == 1
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT count(*) FROM fm_v2_knowledge_revisions").fetchone() == (1,)
        assert conn.execute("SELECT count(*) FROM fm_v2_outbox").fetchone() == (1,)
        assert conn.execute(
            "SELECT apply_result FROM fm_v2_migration_events WHERE migration_run_id=?",
            (run_id,),
        ).fetchall() == [("applied",)]


def test_source_mutation_during_conversion_prevents_applied_marker(tmp_path):
    path = str(tmp_path / "migration.db")
    _empty_v2_db(path)
    _add_curated_and_targets(path)
    run_id = plan_migration(path)
    with sqlite3.connect(path) as conn:
        conn.executescript(
            """
            CREATE TRIGGER mutate_legacy_during_migration_event
            AFTER INSERT ON fm_v2_migration_events
            BEGIN
                UPDATE curated
                SET content='changed during conversion'
                WHERE id='legacy-1';
            END;
            """
        )

    with pytest.raises(MigrationError, match="source changed during apply"):
        apply_migration(path, run_id)

    with sqlite3.connect(path) as conn:
        assert conn.execute(
            "SELECT state FROM fm_v2_migration_runs WHERE migration_run_id=?",
            (run_id,),
        ).fetchone() == ("applying",)
        assert conn.execute(
            "SELECT content FROM curated WHERE id='legacy-1'"
        ).fetchone() == ("changed during conversion",)
        assert conn.execute(
            "SELECT count(*) FROM fm_v2_knowledge_revisions"
        ).fetchone() == (1,)
        assert conn.execute(
            "SELECT count(*) FROM fm_v2_migration_events WHERE migration_run_id=?",
            (run_id,),
        ).fetchone() == (1,)


def test_apply_fails_closed_when_source_changes_between_plan_and_apply(tmp_path):
    """S05 quiesce contract: with the replay channel removed, any legacy
    write between plan and apply — including a forget-style delete — aborts
    the run; the operator stops writers, re-plans, and re-applies."""
    path = str(tmp_path / "migration.db")
    _empty_v2_db(path)
    _add_curated_and_targets(path)
    run_id = plan_migration(path)
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE curated SET content='written after plan' WHERE id='legacy-1'")
    with pytest.raises(MigrationError, match="source changed after the plan was frozen"):
        apply_migration(path, run_id)
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT count(*) FROM fm_v2_knowledge_revisions").fetchone() == (0,)

    # A forget (delete) between plan and apply drifts the watermark too.
    forget_run = plan_migration(path)
    with sqlite3.connect(path) as conn:
        conn.execute("DELETE FROM curated WHERE id='legacy-1'")
    with pytest.raises(MigrationError, match="source changed after the plan was frozen"):
        apply_migration(path, forget_run)

    # After a fresh plan on the quiesced source, apply and shadow agree.
    clean_run = plan_migration(path)
    assert apply_migration(path, clean_run)["applied"] == 0
    assert shadow_compare(path, clean_run)["equivalent"] is True
