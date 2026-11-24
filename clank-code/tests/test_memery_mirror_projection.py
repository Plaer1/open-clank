"""Synthetic fact and reserved-question projection against the legacy mirror."""
from types import SimpleNamespace
import json
from pathlib import Path

import pytest
from tests.test_memery_v2_bridge import _db
from src.memery_v2 import V2Repository, V2OperationError, mirror_record, prepare_mirror_projection


def test_shared_fact_and_reserved_question_projection(tmp_path):
    path = str(tmp_path / "projection.db")
    _db(path)
    repository = V2Repository(path)
    repository.create_entity(owner="alice", workspace_id="global", entity_id="handler", entity_type="person", canonical_label="Handler", roles=["handler"])
    fixture = json.loads(Path("contracts/memery/v3/s03-mirror-projections.json").read_text())
    records = [SimpleNamespace(**record) for record in fixture["records"]]
    for record in records:
        projection = prepare_mirror_projection(record, owner="alice", workspace_id="global", db_path=path, strict=True)
        assert mirror_record(record, owner="alice", workspace_id="global", db_path=path)
        stored = repository.get_knowledge(owner="alice", workspace_id="global", block_id=projection["block_id"])
        assert stored["value"] == projection["value"]
        assert stored["subject_entity_id"] == projection["entity_id"]
        assert stored["claim_slot"] == projection["claim_slot"]
    assert prepare_mirror_projection(records[1], owner="alice", workspace_id="global", db_path=path, strict=True)["requirements"]["question_target"] == {"kind":"entity", "id":"handler"}
    records[1].metadata["question_context"]["target"]["id"] = "missing"
    with pytest.raises(V2OperationError, match="question target unavailable"):
        prepare_mirror_projection(records[1], owner="alice", workspace_id="global", db_path=path, strict=True)
