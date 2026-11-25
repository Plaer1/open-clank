"""Managed tool selectors cannot select an owner, approve, or widen the turn scope."""
import json
import sqlite3
from types import SimpleNamespace

import pytest

from src.general_hex_composition import GeneralHexComposition
from src.general_hex_library import GeneralHexRepository
from src.openclank.managed_memery import _guidance
from src.openclank.managed_protocol import ManagedMethodValidationError, validate_managed_method_request


def test_model_can_propose_only_against_pinned_guidance_and_scoped_evidence(tmp_path):
    path = tmp_path / "memery.db"
    from tests.test_general_hex_guidance import save_memory, setup_store

    setup_store(path)
    provider = SimpleNamespace(_local_db_path=lambda: path)
    library = GeneralHexRepository(path)
    target = library.create("alice", title="Runtime", body="Use the old runtime.")
    unrelated = library.create("alice", title="Private project", body="Other guidance.", tags=["other"])
    GeneralHexComposition(path).snapshot("alice", "turn")
    save_memory(path, key="source", workspace="project", text="The runtime was replaced.")
    save_memory(path, key="elsewhere", workspace="elsewhere", text="Another project")
    save_memory(path, key="other", owner="bob", workspace="project", text="Another account")

    def run(args, current=lambda: True):
        return _guidance(provider, "alice", "account", "project", "turn", args, current)

    context = json.loads(run({"action":"guidance_context"})["text"])
    assert [item["id"] for item in context["evidence"]] == ["source"]
    assert [item["hex_id"] for item in context["rules"]] == [target["hex_id"]]
    proposal = dict(action="propose_guidance", change="retire", hex_id=target["hex_id"],
                    expected_revision=1, evidence_ids=["source"], reason="The old runtime was removed.")
    assert run({**proposal, "hex_id": unrelated["hex_id"]})["state"] == "rejected"
    assert run({**proposal, "evidence_ids": ["other"]})["state"] == "rejected"
    assert run(proposal, current=lambda: False)["state"] == "rejected"
    result = json.loads(run(proposal)["text"])
    assert result["pending_review"] and result["draft_id"]
    assert json.loads(run(proposal)["text"]) == result
    assert library.get("alice", target["hex_id"]) == target
    with sqlite3.connect(path) as conn:
        row = conn.execute("SELECT payload_json,consumed FROM fm_general_hex_exchange_drafts").fetchone()
        assert json.loads(row[0])["actor"] == "assistant" and row[1] == 0

    identity = dict(sessionID="session", actorID="main", rootOperationID="turn", executionRevision=1, innerTurnID="inner")
    def validate(arguments):
        validate_managed_method_request("_openclank/session/v1/memory/manage",
            dict(identity=identity, messageID="message", toolCallID="call", arguments=arguments))
    validate(proposal)
    validate({**proposal, "change":"revise", "body":"Use the new runtime."})
    for override in ({"change":"accept"}, {"accepted":True}, {"owner":"bob"}, {"workspace_id":"elsewhere"},
                     {"evidence_ids":[]}, {"change":"revise"}, {"body":"New rule"}):
        with pytest.raises(ManagedMethodValidationError):
            validate({**proposal, **override})
