"""One synthetic S03 semantic fixture; no runtime authority is activated."""
import json
import sqlite3
from pathlib import Path

import pytest

from src.memery_field_authority import OwnerReviewContext, TrustedProducerContext, candidate_disposition, conflict_disposition, decide_fields, proposal_fields

ROOT = Path(__file__).resolve().parents[1]


def test_inactive_field_decisions_and_revision_authority():
    fixture = json.loads((ROOT / "contracts/memery/v3/s03-field-authority-fixtures.json").read_text())
    for case in fixture["cases"]:
        args = dict(case["input"])
        if "review" in args:
            review = args["review"]
            review["approved_paths"] = tuple(review["approved_paths"])
            args["review"] = OwnerReviewContext(**review)
        if "producer" in args:
            producer = args["producer"]
            producer["validated_inherited_paths"] = tuple(producer.get("validated_inherited_paths", ()))
            args["producer"] = TrustedProducerContext(**producer)
        assert decide_fields(**args) == case["expected"], case["name"]
        if "expected_changes" in case:
            assert proposal_fields(args["current"], args["proposed"]) == case["expected_changes"], case["name"]
    for state, expected in fixture["candidate_states"].items():
        assert candidate_disposition(state) == expected
    for state, expected in fixture["conflict_states"].items():
        assert conflict_disposition(state) == expected

    conn = sqlite3.connect(":memory:")
    conn.execute("PRAGMA foreign_keys=ON")
    schema_dir = ROOT / "native/memery/crates/memery-core/src/store"
    conn.executescript((schema_dir / "current-schema.sql").read_text())
    conn.executescript((schema_dir / "schema-v17-authority.sql").read_text())
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 16
    conn.execute("INSERT INTO fm_v2_change_sets(owner_id,change_set_id,actor_type,actor_id,reason,contract_version,content_hash,created_at) VALUES (?,?,?,?,?,?,?,?)", ("alice", "cs", "model", "producer", "test", "frankenmemory.v2", "h", "now"))
    conn.execute("INSERT INTO fm_v2_entities(owner_id,entity_id,workspace_key,project_key,created_change_set_id,current_revision,created_at) VALUES (?,?,?,?,?,?,?)", ("alice", "entity", "work-a", "", "cs", 0, "now"))
    conn.execute("INSERT INTO fm_v2_knowledge_blocks(owner_id,block_id,workspace_key,project_key,subject_entity_id,predicate,claim_slot,cardinality,created_change_set_id,current_revision,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)", ("alice", "block", "work-a", "", "entity", "test", "slot", "single", "cs", 0, "now"))
    conn.execute("INSERT INTO fm_v2_knowledge_revisions(owner_id,block_id,revision,value_type,value_json,kind,status,confidence,trust,activation,evidence_json,content_hash,actor_type,actor_id,reason,change_set_id,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", ("alice", "block", 1, "boolean", '{"type":"boolean","value":false}', "fact", "active", .5, .5, "active", "[]", "h", "model", "producer", "test", "cs", "now"))
    insert = "INSERT INTO fm_v2_revision_field_authority(owner_id,block_id,revision,field_path,origin,producer_type,producer_id,approved_by,evidence_json,change_set_id,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)"
    row = ("alice", "block", 1, "/enabled", "manual", "model", "producer", "alice", "[]", "cs", "now")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(insert, (*row[:7], None, *row[8:]))
    conn.execute(insert, row)
    assert conn.execute("PRAGMA recursive_triggers").fetchone()[0] == 0
    with pytest.raises(sqlite3.IntegrityError, match="invalid field authority"):
        conn.execute("INSERT OR REPLACE INTO fm_v2_revision_field_authority SELECT * FROM fm_v2_revision_field_authority")
    conn.rollback()
    conn.close()
