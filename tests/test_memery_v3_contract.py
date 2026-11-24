"""Contract-only proposal: no runtime registration or migration is implied."""
import json
from pathlib import Path

from jsonschema import Draft202012Validator

ROOT = Path(__file__).resolve().parents[1] / "contracts" / "memery"


def test_proposed_v3_knowledge_reference_and_v2_rejection():
    v2 = json.loads((ROOT / "v2/schema.json").read_text())
    v3 = json.loads((ROOT / "v3/schema.json").read_text())
    cases = json.loads((ROOT / "v3/contract-fixtures.json").read_text())
    assert v2["properties"]["contract_version"]["const"] == "frankenmemory.v2"
    assert v3["properties"]["contract_version"]["const"] == "frankenmemory.v3"
    validator = Draft202012Validator(v3["$defs"]["typed_value"])
    legacy = Draft202012Validator(v2["$defs"]["typed_value"])
    for case in cases["typed_value"]:
        assert validator.is_valid(case["value"]) is case["valid"], case["name"]
        if case["value"]["type"] == "knowledge_ref":
            assert not legacy.is_valid(case["value"]), case["name"]


def test_inactive_representation_connected_transaction():
    import sqlite3

    import pytest

    from src.memery_representation import append_revision_metadata, prepare_revision_metadata, resolve_knowledge_reference, validate_revision_metadata, validate_v3_value
    from src.memery_v2 import V2OperationError

    conn = sqlite3.connect(":memory:")
    conn.execute("PRAGMA foreign_keys=ON")
    schema = ROOT.parents[1] / "native/memery/crates/memery-core/src/store/current-schema.sql"
    additions = schema.with_name("schema-v17-additions.sql")
    conn.executescript(schema.read_text())
    conn.executescript(additions.read_text())
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 16
    for owner, block, workspace in (("alice", "target", "work-a"), ("alice", "hidden", "work-b"), ("bob", "foreign", "work-a")):
        change = f"cs-{owner}-{block}"
        conn.execute("INSERT INTO fm_v2_change_sets(owner_id,change_set_id,actor_type,actor_id,reason,contract_version,content_hash,created_at) VALUES (?,?,?,?,?,?,?,?)", (owner, change, "user", owner, "fixture", "frankenmemory.v2", "h", "2026-01-01"))
        conn.execute("INSERT INTO fm_v2_entities(owner_id,entity_id,workspace_key,project_key,created_change_set_id,current_revision,created_at) VALUES (?,?,?,?,?,?,?)", (owner, f"entity-{block}", workspace, "", change, 0, "2026-01-01"))
        conn.execute("INSERT INTO fm_v2_knowledge_blocks(owner_id,block_id,workspace_key,project_key,subject_entity_id,predicate,claim_slot,cardinality,created_change_set_id,current_revision,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)", (owner, block, workspace, "", f"entity-{block}", "fixture", block, "single", change, 0, "2026-01-01"))
        for revision in range(1, 3 if block == "target" else 2):
            conn.execute("INSERT INTO fm_v2_knowledge_revisions(owner_id,block_id,revision,value_type,value_json,kind,status,confidence,trust,activation,evidence_json,content_hash,actor_type,actor_id,reason,source_event_id,change_set_id,previous_revision,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (owner, block, revision, "string", '{"type":"string","value":"x"}', "fact", "active", 0.5, 0.5, "active", '["evidence-1"]', "h", "user", owner, "fixture", "source-event", change, revision - 1 if revision > 1 else None, "2026-01-01"))
        conn.execute("UPDATE fm_v2_knowledge_blocks SET current_revision=? WHERE owner_id=? AND block_id=?", (2 if block == "target" else 1, owner, block))
    conn.commit()
    scope = {"authenticated_owner": "alice", "owner": "alice", "workspace_id": "work-a", "project_id": None}
    assert resolve_knowledge_reference(conn, **scope, value={"type": "knowledge_ref", "value": {"block_id": "target", "revision": 1}}) == {"block_id": "target", "revision": 1}
    assert resolve_knowledge_reference(conn, **scope, value={"type": "knowledge_ref", "value": {"block_id": "target"}})["revision"] == 2
    for block in ("hidden", "foreign", "missing"):
        with pytest.raises(V2OperationError, match="knowledge reference unavailable") as exc:
            resolve_knowledge_reference(conn, **scope, value={"type": "knowledge_ref", "value": {"block_id": block}})
        assert exc.value.code == "not_found"
    with pytest.raises(V2OperationError, match="knowledge reference unavailable"):
        resolve_knowledge_reference(conn, **scope, value={"type": "knowledge_ref", "value": {"block_id": "target", "revision": 9}})
    assert validate_v3_value({"type": "entity_ref", "value": {"entity_id": "entity-target"}})["type"] == "entity_ref"
    with pytest.raises(V2OperationError):
        validate_revision_metadata({"authority": "manual"})
    draft = json.loads((ROOT / "v3/schema.json").read_text())
    metadata_validator = Draft202012Validator(draft["$defs"]["revision_metadata"])
    assert metadata_validator.is_valid({"domain_refs": [{"block_id": "target", "revision": 1}]})
    assert not metadata_validator.is_valid({"domain_refs": ["target"]})
    conn.execute("BEGIN")
    with pytest.raises(V2OperationError, match="new revision"):
        prepare_revision_metadata(conn, **scope, block_id="target", revision=1, producer_actor_type="user", producer_actor_id="alice", change_set_id="cs-alice-target")
    with pytest.raises(V2OperationError, match="new change set"):
        prepare_revision_metadata(conn, **scope, block_id="target", revision=3, producer_actor_type="user", producer_actor_id="alice", change_set_id="cs-alice-target")
    prepared = prepare_revision_metadata(conn, **scope, block_id="target", revision=3, producer_actor_type="model", producer_actor_id="extractor", change_set_id="cs-new")
    conn.execute("INSERT INTO fm_v2_change_sets(owner_id,change_set_id,actor_type,actor_id,reason,contract_version,content_hash,created_at) VALUES (?,?,?,?,?,?,?,?)", ("alice", "cs-new", "model", "extractor", "fixture", "frankenmemory.v2", "h", "2026-01-01"))
    conn.execute("INSERT INTO fm_v2_knowledge_revisions(owner_id,block_id,revision,value_type,value_json,kind,status,confidence,trust,activation,evidence_json,content_hash,actor_type,actor_id,reason,source_event_id,change_set_id,previous_revision,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", ("alice", "target", 3, "string", '{"type":"string","value":"new"}', "fact", "active", .5, .5, "active", '["evidence-1"]', "h", "model", "extractor", "fixture", "source-event", "cs-new", 2, "2026-01-01"))
    with pytest.raises(sqlite3.IntegrityError, match="invalid revision metadata"):
        conn.execute("INSERT INTO fm_v2_revision_metadata(owner_id,block_id,revision,metadata_schema,metadata_json,metadata_hash,actor_type,actor_id,change_set_id,source_event_id,evidence_json,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", ("alice", "target", 3, "openclank.memory-revision-metadata.v1", '{"authority":"manual"}', "a" * 64, "model", "extractor", "cs-new", "source-event", '["evidence-1"]', "2026-01-01"))
    for raw in (
        '{"domain_refs":[{"block_id":"hidden","revision":1}]}',
        '{"domain_refs":[{"block_id":"foreign","revision":1}]}',
        '{"domain_refs":[{"block_id":"target","revision":9}]}',
        '{"domain_refs":[{"block_id":" target","revision":1}]}',
        '{"domain_refs":[{"block_id":"target","block_id":"target","revision":1}]}',
    ):
        with pytest.raises(sqlite3.IntegrityError, match="invalid revision metadata"):
            conn.execute("INSERT INTO fm_v2_revision_metadata(owner_id,block_id,revision,metadata_schema,metadata_json,metadata_hash,actor_type,actor_id,change_set_id,source_event_id,evidence_json,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", ("alice", "target", 3, "openclank.memory-revision-metadata.v1", raw, "a" * 64, "model", "extractor", "cs-new", "source-event", '["evidence-1"]', "2026-01-01"))
    result = append_revision_metadata(conn, context=prepared, metadata={"name": "Beacon", "domain_refs": [{"block_id": "target", "revision": 1}], "processing_role": "extractor"}, created_at="2026-01-01")
    assert len(result["metadata_hash"]) == 64
    assert conn.execute("SELECT actor_type,actor_id,source_event_id,evidence_json FROM fm_v2_revision_metadata").fetchone() == ("model", "extractor", "source-event", '["evidence-1"]')
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("UPDATE fm_v2_revision_metadata SET metadata_json='{}'")
    assert conn.execute("PRAGMA recursive_triggers").fetchone()[0] == 0
    with pytest.raises(sqlite3.IntegrityError, match="invalid revision metadata"):
        conn.execute("INSERT OR REPLACE INTO fm_v2_revision_metadata SELECT * FROM fm_v2_revision_metadata")
    with pytest.raises(V2OperationError, match="context reused"):
        append_revision_metadata(conn, context=prepared, metadata={}, created_at="2026-01-01")
    conn.rollback()
    assert conn.execute("SELECT count(*) FROM fm_v2_revision_metadata").fetchone()[0] == 0
    conn.execute("BEGIN")
    stale = prepare_revision_metadata(conn, **scope, block_id="target", revision=3, producer_actor_type="model", producer_actor_id="extractor", change_set_id="cs-again")
    conn.commit()
    conn.execute("BEGIN")
    with pytest.raises(V2OperationError, match="transaction changed"):
        append_revision_metadata(conn, context=stale, metadata={}, created_at="2026-01-01")
    conn.rollback()
    # The existing v16 whitelist remains installed: draft SQL does not activate v3 writes.
    with pytest.raises(sqlite3.IntegrityError, match="invalid knowledge revision"):
        conn.execute("INSERT INTO fm_v2_knowledge_revisions(owner_id,block_id,revision,value_type,value_json,kind,status,confidence,trust,activation,evidence_json,content_hash,actor_type,actor_id,reason,change_set_id,previous_revision,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", ("alice", "target", 3, "knowledge_ref", '{"type":"knowledge_ref","value":{"block_id":"target","revision":1}}', "fact", "active", .5, .5, "active", "[]", "h", "user", "alice", "fixture", "cs-alice-target", 2, "2026-01-01"))
    # Isolate the candidate's reference check in the disposable database only;
    # production integration must replace the v16 whitelist without losing its checks.
    conn.rollback()
    conn.execute("DROP TRIGGER fm_v2_knowledge_revision_validate")
    insert_ref = "INSERT INTO fm_v2_knowledge_revisions(owner_id,block_id,revision,value_type,value_json,kind,status,confidence,trust,activation,evidence_json,content_hash,actor_type,actor_id,reason,change_set_id,previous_revision,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
    def ref_row(revision, value):
        return ("alice", "target", revision, "knowledge_ref", json.dumps(value), "fact", "active", .5, .5, "active", "[]", "h", "user", "alice", "fixture", "cs-alice-target", revision - 1, "2026-01-01")
    conn.execute(insert_ref, ref_row(3, {"type": "knowledge_ref", "value": {"block_id": "target", "revision": 1}}))
    for bad in ({"block_id": "hidden"}, {"block_id": "foreign"}, {"block_id": "target", "revision": 9}, {"block_id": "target", "revision": None}, {"block_id": " target", "revision": 1}, {"block_id": "\u00a0target", "revision": 1}):
        with pytest.raises(sqlite3.IntegrityError, match="knowledge reference unavailable"):
            conn.execute(insert_ref, ref_row(4, {"type": "knowledge_ref", "value": bad}))
    duplicate = list(ref_row(4, {"type": "knowledge_ref", "value": {"block_id": "target", "revision": 1}}))
    duplicate[4] = '{"type":"knowledge_ref","value":{"block_id":"target","block_id":"target","revision":1}}'
    with pytest.raises(sqlite3.IntegrityError, match="knowledge reference unavailable"):
        conn.execute(insert_ref, duplicate)
    conn.rollback()
    conn.close()
