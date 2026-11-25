import sqlite3
from types import SimpleNamespace

import pytest

from src.general_hex_guidance import GeneralHexGuidance
from src.general_hex_library import GeneralHexConflict, GeneralHexError, GeneralHexNotFound, ensure_general_hex_schema
from src.memery_v2 import mirror_record
from pathlib import Path


def setup_store(path):
    ensure_general_hex_schema(path)
    with sqlite3.connect(path) as db:
        db.executescript(Path("native/memery/crates/memery-core/src/store/current-schema.sql").read_text())
        db.execute("PRAGMA user_version=16")


def save_memory(path, owner="alice", key="fact", workspace="repo", text="Release on Friday.", action="upsert"):
    with sqlite3.connect(path) as db:
        db.execute("INSERT OR REPLACE INTO curated(owner,id,workspace_id,kind,content,archived,updated_at,created_at) VALUES(?,?,?,?,?,0,'1','1')", (owner, key, workspace, "fact", text))
    record = SimpleNamespace(id=key, text=text, category="fact", kind="fact", metadata={}, source_type="human")
    assert mirror_record(record, owner=owner, workspace_id=workspace, action=action, db_path=str(path))


@pytest.fixture
def context(tmp_path):
    path = tmp_path / "facts.sqlite"
    setup_store(path)
    save_memory(path)
    service = GeneralHexGuidance(path)
    hex_id = service.repo.create("alice", title="Release day", body="Release on Thursday.")["hex_id"]
    request = dict(account_id="a1", hex_id=hex_id, expected_revision=1, action="revise",
                   reason="The release agreement now specifies Friday.", body="Release on Friday.",
                   evidence_ids=["fact"], workspace_id="repo")
    return path, service, request


def test_approval_revalidates_sources_parents_and_account(context):
    path, svc, req = context
    parents = [svc.repo.create("alice", title=name, body=name)["hex_id"] for name in ("Calendar", "Releases")]
    draft = svc.propose("alice", **req, parent_ids=parents)
    assert svc.propose("alice", **req, parent_ids=parents)["draft_id"] == draft["draft_id"]
    assert svc.repo.get("alice", req["hex_id"])["body"] == "Release on Thursday."
    assert svc.pending("alice", "a2")["proposals"] == []
    with pytest.raises(GeneralHexNotFound):
        svc.decide("alice", "a2", draft["draft_id"], accept=True)
    with pytest.raises(GeneralHexConflict):
        svc.decide("alice", "a1", draft["draft_id"], accept=True, current=lambda:False)
    revised = svc.decide("alice", "a1", draft["draft_id"], accept=True)
    assert revised["revision"] == 2 and revised["provenance"]["guidance"]["parent_ids"] == parents
    assert "label" not in revised["provenance"]["guidance"]["sources"][0]
    with pytest.raises(GeneralHexError, match="ancestor"):
        svc.propose("alice", **dict(req, hex_id=parents[0]), parent_ids=[req["hex_id"]])
    old = svc.propose("alice", **dict(req, action="retire", body=None, expected_revision=2))
    save_memory(path, text="Release on Tuesday.")
    with pytest.raises(GeneralHexConflict):
        svc.decide("alice", "a1", old["draft_id"], accept=True)
    svc.decide("alice", "a1", old["draft_id"], accept=False)
    fresh = svc.propose("alice", **dict(req, action="retire", body=None, expected_revision=2))
    disabled = svc.decide("alice", "a1", fresh["draft_id"], accept=True)
    assert disabled["enabled"] is False and disabled["accepted"] and disabled["revision"] == 3
    with pytest.raises(GeneralHexConflict):
        svc.decide("alice", "a1", fresh["draft_id"], accept=True)
    with pytest.raises(GeneralHexConflict):
        svc.repo.restore("alice", req["hex_id"], revision=2, expected_revision=3, account_id="a1")
    with pytest.raises(GeneralHexConflict):
        svc.repo.update("alice", req["hex_id"], expected_revision=3, enabled=True, account_id="a1")
    review = svc.propose("alice", **dict(req, expected_revision=3, body="Release on Tuesday."), actor="owner")
    assert svc.decide("alice", "a1", review["draft_id"], accept=True)["enabled"]


def test_only_current_owned_live_memory_can_support_guidance(context):
    path, svc, req = context
    save_memory(path, owner="bob", key="bob-fact")
    save_memory(path, key="other-space", workspace="other")
    visible = svc.evidence("alice", "repo")
    assert [item["id"] for item in visible["items"]] == ["fact"]
    save_memory(path, action="delete")
    assert svc.evidence("alice", "repo")["items"] == []
    for key in ("fact", "bob-fact", "other-space"):
        with pytest.raises(GeneralHexConflict):
            svc.propose("alice", **dict(req, evidence_ids=[key]))


def test_erasure_revokes_evidence_before_review(context):
    path, svc, req = context
    draft = svc.propose("alice", **req)
    with sqlite3.connect(path) as db:
        db.execute("INSERT INTO fm_v2_privacy_erasure_context VALUES(?, ?, NULL, ?, ?, ?)",
                   ("alice", "request-1", "alice", "remove", "1"))
    assert svc.evidence("alice", "repo", account_id="a1")["items"] == []
    with pytest.raises(GeneralHexConflict):
        svc.decide("alice", "a1", draft["draft_id"], accept=True)


def test_pending_decision_revalidates_parent_and_target(context):
    _, svc, req = context
    parent = svc.repo.create("alice", title="Parent", body="Original")
    draft = svc.propose("alice", **req, parent_ids=[parent["hex_id"]])
    svc.repo.update("alice", parent["hex_id"], expected_revision=1, body="Changed")
    with pytest.raises(GeneralHexConflict, match="parent"):
        svc.decide("alice", "a1", draft["draft_id"], accept=True)
    svc.repo.update("alice", req["hex_id"], expected_revision=1, body="Owner's edit")
    with pytest.raises(GeneralHexConflict, match="Hex changed"):
        svc.decide("alice", "a1", draft["draft_id"], accept=True)
    assert svc.repo.get("alice", req["hex_id"])["body"] == "Owner's edit"


@pytest.mark.parametrize("auth", [{"api_token": True}, {"internal_tool_authenticated": True}])
def test_token_and_agent_cannot_accept_or_bypass_review(auth):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from routes.general_hex_routes import setup_general_hex_routes
    app = FastAPI()
    @app.middleware("http")
    async def bound(request, call_next):
        for key, value in auth.items():
            setattr(request.state, key, value)
        return await call_next(request)
    app.include_router(setup_general_hex_routes(db_path="unused", owner_resolver=lambda _: "alice"))
    client = TestClient(app)
    assert client.post("/api/hexes/guidance/proposals/abc/accept").status_code == 403
    assert client.get("/api/hexes/guidance/evidence").status_code == 403
    assert client.patch("/api/hexes/general/abc", json={"expected_revision":1, "enabled":True}).status_code == 403
