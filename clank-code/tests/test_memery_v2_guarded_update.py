"""Disposable connected test of the inactive guarded repository seam."""
import sqlite3
import json
import hashlib
from pathlib import Path

import pytest

from tests.test_memery_v2_bridge import _db
from src.memery_field_authority import OwnerReviewContext
from src.memery_v2 import V2Repository, V2OperationError, RevisionConflict, _hash, _sealed_json, guarded_update_operation


def test_guarded_update_replay_subset_and_rollback(tmp_path, monkeypatch):
    path = str(tmp_path / "synthetic.db")
    _db(path)
    vectors = json.loads(Path("contracts/memery/v3/s03-field-authority-fixtures.json").read_text())["receipt_vectors"]
    assert all(hashlib.sha256(v["operation_json"].encode()).hexdigest() == v["sha256"] for v in vectors)
    assert vectors[0]["sha256"] != vectors[1]["sha256"]
    base = V2Repository(path)
    base.create_entity(owner="alice", workspace_id="global", entity_id="e", entity_type="person", canonical_label="A")
    base.create_knowledge(owner="alice", workspace_id="global", block_id="b", subject_entity_id="e", predicate="p", claim_slot="s", payload={"value": {"type": "string", "value": "old"}, "tags": ["old"]})
    sql = Path("native/memery/crates/memery-core/src/store/schema-v17-authority.sql").read_text()
    with sqlite3.connect(path) as conn:
        conn.executescript(sql)
        conn.execute("PRAGMA user_version=17")
        row = conn.execute("SELECT change_set_id,actor_type,actor_id FROM fm_v2_knowledge_revisions WHERE owner_id='alice' AND block_id='b' AND revision=1").fetchone()
        for field, origin in (("/value", "manual"), ("/tags", "inferred")):
            conn.execute("INSERT INTO fm_v2_revision_field_authority(owner_id,block_id,revision,field_path,origin,producer_type,producer_id,approved_by,evidence_json,change_set_id,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)", ("alice","b",1,field,origin,row[1],row[2],"alice" if origin == "manual" else None,"[]",row[0],"now"))

    class SyntheticV17(V2Repository):
        def _connect(self):
            conn = super()._connect()
            conn.create_function("fm_writer_capability", 0, lambda: "openclank.frankenmemory.writer.v2", deterministic=True)
            return conn

    repo = SyntheticV17(path, guarded_v17=True)
    payload = {"value": {"type":"string", "value":"new"}, "tags":["new"], "confidence":0.5, "activation_rationale":{"typed":False}}
    operation = guarded_update_operation(owner="alice", workspace="global", project="", block_id="b", expected_revision=1, payload=payload, request_key="request-1", actor_type="user", actor_id="alice", reason="update knowledge", source_event_id=None, approved_paths=("/value/value",))
    raw_operation = json.dumps(operation, ensure_ascii=False, indent=2).replace('"confidence": 0.5', '"confidence": 5e-1')
    review = OwnerReviewContext("alice", "alice", 1, "request-1", hashlib.sha256(raw_operation.encode()).hexdigest(), ("/value/value",))
    args = dict(owner="alice", workspace_id="global", block_id="b", expected_revision=1, payload=payload, request_key="request-1", trusted_review=review, sealed_operation_json=raw_operation)
    result = repo.update_knowledge(**args)
    assert result["revision"] == 2 and result["value"]["value"] == "new" and result["tags"] == ["old"]
    assert result["field_decision"]["review_paths"] == ["/activation_rationale", "/tags"]
    assert repo.update_knowledge(**args) == result
    canonical = _sealed_json(operation)
    canonical_review = OwnerReviewContext("alice", "alice", 1, "request-1", hashlib.sha256(canonical.encode()).hexdigest(), ("/value/value",))
    with pytest.raises(V2OperationError, match="payload mismatch"):
        repo.update_knowledge(**{**args, "sealed_operation_json":canonical, "trusted_review":canonical_review})
    wrong_type = raw_operation.replace('"typed": false', '"typed": 0')
    with pytest.raises(V2OperationError, match="sealed operation mismatch"):
        repo.update_knowledge(**{**args, "sealed_operation_json":wrong_type})
    altered = {**payload, "activation_rationale":{"typed":0}}
    altered_operation = {**operation, "payload":altered}
    altered_review = OwnerReviewContext("alice", "alice", 1, "request-1", _hash(altered_operation), ("/value/value",))
    with pytest.raises(V2OperationError, match="payload mismatch"):
        repo.update_knowledge(**{**args, "payload":altered, "trusted_review":altered_review, "sealed_operation_json":_sealed_json(altered_operation)})
    with pytest.raises(V2OperationError, match="hash mismatch"):
        repo.update_knowledge(**{**args, "payload":{"tags":["other"]}, "sealed_operation_json":_sealed_json({**operation,"payload":{"tags":["other"]}})})
    with pytest.raises(RevisionConflict):
        repo.update_knowledge(**{**args, "request_key":"other", "sealed_operation_json":_sealed_json({**operation,"request_key":"other"}), "trusted_review":OwnerReviewContext("alice","alice",1,"other",_hash({**operation,"request_key":"other"}),("/value/value",))})
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT count(*) FROM fm_v2_knowledge_revisions WHERE block_id='b'").fetchone()[0] == 2
        assert conn.execute("SELECT count(*) FROM fm_v2_outbox WHERE kind='knowledge_revision'").fetchone()[0] == 2
        assert conn.execute("SELECT origin FROM fm_v2_revision_field_authority WHERE block_id='b' AND revision=2 AND field_path='/tags'").fetchone()[0] == "inferred"

    from src.memory_versioned import PublishedReviewRequest, transition_knowledge
    original_connect = V2Repository._connect
    def synthetic_connect(self):
        conn = original_connect(self)
        conn.create_function("fm_writer_capability", 0, lambda: "openclank.frankenmemory.writer.v2", deterministic=True)
        return conn
    monkeypatch.setattr(V2Repository, "_connect", synthetic_connect)
    context = PublishedReviewRequest("alice", "global", "retract-1", ("/status",))
    transition = dict(identifier="b", action="retract", owner="alice", workspace_id="global", expected_revision=2,
                      db_path=path, guarded_v17=True, published_review=context)
    first = transition_knowledge(**transition)
    later = transition_knowledge(identifier="b", action="revert", owner="alice", workspace_id="global", expected_revision=3, target_revision=1, db_path=path, guarded_v17=True, published_review=PublishedReviewRequest("alice", "global", "revert-1", ("/status",)))
    replay = transition_knowledge(**transition)
    assert first["applied_revision"] == replay["applied_revision"] == 3
    assert replay["applied"]["status"] == "retracted"
    assert later["current_revision"] == replay["current_revision"] == 4
    assert replay["latest"]["status"] == "active"


def test_guarded_create_transaction(tmp_path):
    from src.memery_field_authority import TrustedProducerContext
    path = str(tmp_path / "create.db")
    _db(path)
    base = V2Repository(path)
    base.create_entity(owner="alice", workspace_id="global", entity_id="e", entity_type="person", canonical_label="A")
    with sqlite3.connect(path) as conn:
        conn.executescript(Path("native/memery/crates/memery-core/src/store/schema-v17-authority.sql").read_text())
        conn.execute("PRAGMA user_version=17")
    class SyntheticV17(V2Repository):
        def _connect(self):
            conn = super()._connect()
            conn.create_function("fm_writer_capability", 0, lambda: "openclank.frankenmemory.writer.v2", deterministic=True)
            return conn
    repo = SyntheticV17(path, guarded_v17=True)
    args = dict(owner="alice", workspace_id="global", block_id="created", subject_entity_id="e", predicate="p", claim_slot="s", payload={"value":{"type":"string","value":"new"}}, trusted_producer=TrustedProducerContext("automatic"), request_key="create-1")
    first = repo.create_knowledge(**args)
    assert first["revision"] == 1 and set(first["field_decision"]["origins"].values()) == {"inferred"}
    assert repo.create_knowledge(**args) == first
    with pytest.raises(V2OperationError, match="payload mismatch"):
        repo.create_knowledge(**{**args, "payload":{"value":{"type":"string","value":"other"}}})
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT count(*) FROM fm_v2_knowledge_revisions WHERE block_id='created'").fetchone()[0] == 1

    mixed_args = {**args, "block_id":"mixed", "request_key":"create-mixed"}
    operation = {"version":"s03.published-create.v1", "owner":"alice", "workspace":"global", "project":"", "block_id":"mixed", "subject_entity_id":"e", "predicate":"p", "claim_slot":"s", "cardinality":"single", "payload":mixed_args["payload"], "request_key":"create-mixed", "actor_type":"user", "actor_id":"alice", "reason":"create knowledge", "source_event_id":None, "approved_paths":["/value"], "producer":{"kind":"automatic", "validated_inherited_paths":[]}}
    mixed_args["trusted_review"] = OwnerReviewContext("alice", "alice", 0, "create-mixed", _hash(operation), ("/value",))
    mixed = repo.create_knowledge(**mixed_args)
    assert mixed["field_decision"]["origins"]["/value"] == "manual"
    assert mixed["field_decision"]["origins"]["/confidence"] == "inferred"
