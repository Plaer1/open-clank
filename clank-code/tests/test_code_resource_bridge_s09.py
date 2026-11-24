"""Bounded S09 persisted-producer/DTO fixtures, no live app or model."""
import asyncio
import json
import os
from pathlib import Path
import sqlite3
from types import SimpleNamespace

import pytest

assert os.environ["PYTHONDONTWRITEBYTECODE"] == "1"
assert Path(os.environ["OPEN_CLANK_DATA_DIR"]).is_relative_to(Path("/tmp"))
from src.openclank import code_resource_bridge as bridge


@pytest.fixture
def fixture(tmp_path, monkeypatch):
    path = tmp_path / "producer.db"
    with sqlite3.connect(path) as c:
        c.executescript("""
        CREATE TABLE fm_v2_sources(owner_id,source_id,source_revision,workspace_key,project_key,content_hash,source_type,forget_state);
        CREATE TABLE fm_v2_evidence(owner_id,evidence_id,source_id,source_revision,locator_type,locator_json,quote,extraction_contract,parser_version);
        CREATE TABLE fm_v2_documents(owner_id,document_id,source_id,source_revision,parser_version);
        CREATE TABLE fm_v2_chunks(owner_id,chunk_id,document_id,locator_json,text,content_hash);
        INSERT INTO fm_v2_sources VALUES('alice','source',1,'workspace','','aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa','provider_record','active');
        INSERT INTO fm_v2_evidence VALUES('alice','evidence','source',1,'memory_record','{"record_id":"record","source_revision":1}','original quote','provider-record-v1','open-clank');
        INSERT INTO fm_v2_documents VALUES('alice','document','source',1,'test');
        INSERT INTO fm_v2_chunks VALUES('alice','chunk','document','{"type":"text","start":0,"end":5,"line_start":1,"line_end":1,"headings":[],"block_type":"paragraph"}','hello','bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb');
        """)
    repository = SimpleNamespace(generation=lambda: 1)
    ws = SimpleNamespace(owner_subject_id="account-alice", id="workspace", location_id="loc", revision=1)
    binding = SimpleNamespace(workspace=ws, path="/private/not-returned")
    def resolve(repo, *, owner_username, workspace_id, **kw):
        if owner_username != "alice" or workspace_id != "workspace":
            from src.openclank.workspace_policy_service import WorkspacePolicyServiceError
            raise WorkspacePolicyServiceError("not public")
        return binding
    monkeypatch.setattr("src.openclank.workspace_policy_service.resolve_owned_workspace", resolve)
    args = dict(repository=repository, provider=SimpleNamespace(_fm_db_path=str(path)), auth_manager=object(), owner="alice")
    return args, path, binding


def body(kind="memory_evidence"):
    ref = dict(kind=kind, evidence_id="evidence", source_revision=1) if kind == "memory_evidence" else dict(kind=kind, document_id="document", chunk_id="chunk", source_revision=1)
    return dict(workspace_id="workspace", operation="resolve", reference=ref)


@pytest.mark.parametrize("kind", ["memory_evidence", "document_chunk"])
async def test_persisted_provenance_is_scoped_not_disk_or_code_association(fixture, kind):
    args, _, _ = fixture
    result = await bridge.resolve_resource(**args, body=body(kind))
    assert result["state"] == "current" and result["validation_kind"] == "persisted_revision"
    assert result["current_source_verified"] is False and result["destination"] is None and result["source"] is None
    assert result["provenance"]["source_revision"] == 1
    assert result["provenance"]["locator_unit"] == ("memory_record_revision" if kind == "memory_evidence" else "unicode_characters_end_exclusive")
    assert "unavailable" in result["provenance"]["code_association"]
    assert len(bridge._json(result)) == result["cost"]["serialized_bytes"] <= 32768
    assert "/private" not in json.dumps(result)


@pytest.mark.parametrize("change,state", [("UPDATE fm_v2_sources SET forget_state='forgotten'", "erased"),
    ("UPDATE fm_v2_sources SET forget_state='forget_requested'", "erased"),
    ("DELETE FROM fm_v2_evidence", "missing"),
    ("INSERT INTO fm_v2_sources SELECT owner_id,source_id,2,workspace_key,project_key,content_hash,source_type,forget_state FROM fm_v2_sources", "stale")])
async def test_persisted_lifecycle_no_cache(fixture, change, state):
    args, path, _ = fixture
    assert (await bridge.resolve_resource(**args, body=body()))["state"] == "current"
    with sqlite3.connect(path) as c: c.execute(change)
    result = await bridge.resolve_resource(**args, body=body())
    assert result["state"] == state
    if state in {"erased", "missing"}:
        assert result["excerpt"] is None and result["provenance"] is None


@pytest.mark.parametrize("selection", ["other_owner", "other_workspace", "other_revision"])
async def test_no_owner_workspace_or_revision_fallback(fixture, selection):
    args, _, _ = fixture
    request = body()
    if selection == "other_owner": args = {**args, "owner": "bob"}
    if selection == "other_workspace": request["workspace_id"] = "elsewhere"
    if selection == "other_revision": request["reference"]["source_revision"] = 2
    result = await bridge.resolve_resource(**args, body=request)
    assert result["state"] in {"denied", "missing"} and result["reference"] is None
    assert result["excerpt"] is None


@pytest.mark.parametrize("payload", [None, [], {"operation": []}, {"workspace_id": "workspace", "operation": "resolve", "reference": {"kind": [], "id": "bad"}},
    {**body(), "root": "/private"}, {**body(), "excerpt_bytes": True}, {**body(), "excerpt_bytes": 8193},
    {**body(), "reference": {**body()["reference"], "uri": "file:///private"}},
    {**body(), "reference": {**body()["reference"], "source_revision": -1}}])
async def test_malformed_closed_inputs_never_dependency_calls(fixture, payload):
    args, _, _ = fixture
    result = await bridge.resolve_resource(**args, body=payload)
    assert result["state"] == "invalid" and result["reference"] is None


async def test_large_or_malicious_persisted_locator_is_not_reinterpreted(fixture):
    args, path, _ = fixture
    for locator in ['{"uri":"file:///private","start":0}', '"' + 'x' * 9000 + '"', '{broken']:
        with sqlite3.connect(path) as c: c.execute("UPDATE fm_v2_evidence SET locator_json=?", (locator,))
        result = await bridge.resolve_resource(**args, body=body())
        assert result["state"] == "unavailable" and result["excerpt"] is None
        assert "file:///private" not in json.dumps(result)


async def test_late_scope_change_and_cancellation_fail_closed(fixture, monkeypatch):
    args, _, _ = fixture
    entered, release = asyncio.Event(), asyncio.Event()
    import src.memory_versioned as versioned
    original = versioned.read_source_reference
    # to_thread executes the existing producer read, never a source crawler.
    calls = 0
    def changed(**kw):
        nonlocal calls
        calls += 1
        result = original(**kw)
        args["repository"].generation = lambda: 2
        return result
    monkeypatch.setattr(versioned, "read_source_reference", changed)
    result = await bridge.resolve_resource(**args, body=body())
    assert result["state"] == "denied" and result["excerpt"] is None and calls == 1


async def test_capability_receipt_names_unavailable_owners(fixture):
    args, _, _ = fixture
    result = await bridge.resolve_resource(**args, body=dict(workspace_id="workspace", operation="capabilities"))
    assert result["error"] is None
    for kind in ("review", "trace", "skills", "ontology", "code_association"):
        cap = next(c for c in result["capabilities"] if c["kind"] == kind)
        assert cap["available"] is False and cap["producer"] and cap["reason"]


@pytest.mark.parametrize("evidence_state,identity_state,expected", [("ambiguous", "stable", "ambiguous"), ("stale", "stable", "stale"), ("unsupported", "stable", "unavailable"), ("metadata", "ambiguous_snapshot_local", "ambiguous")])
async def test_actual_c04_state_field_refuses_source_lookup(fixture, monkeypatch, evidence_state, identity_state, expected):
    args, _, _ = fixture
    receipt = dict(workspace_id="workspace", registration_id="registration", checkout_id="checkout", source_snapshot_id="snapshot", head_generation="head", admission_epoch=0, policy_generation=1, config_fingerprint="config")
    calls = []
    async def inspect(**kw):
        calls.append(kw["query"]["operation"])
        return dict(error=None, receipt=receipt, hits=[dict(id="decl:" + "a" * 64, evidence_state=evidence_state, properties=dict(identity_state=identity_state))])
    monkeypatch.setattr(bridge, "query_code", inspect)
    result = await bridge.resolve_resource(**args, body=dict(workspace_id="workspace", operation="resolve", reference=dict(kind="code", id="decl:" + "a" * 64, receipt=receipt)))
    assert result["state"] == expected and result["destination"] is None
    assert calls == ["inspect"]


async def test_oversize_quote_and_negative_document_range_fail_closed(fixture):
    args, path, _ = fixture
    with sqlite3.connect(path) as c:
        c.execute("UPDATE fm_v2_evidence SET quote=?", ("x" * 9000,))
        c.execute("UPDATE fm_v2_chunks SET locator_json=?", (json.dumps(dict(type="text", start=10, end=1, line_start=1, line_end=1)),))
    for kind in ("memory_evidence", "document_chunk"):
        result = await bridge.resolve_resource(**args, body=body(kind))
        assert result["state"] == "unavailable" and result["excerpt"] is None


async def test_cancellation_does_not_turn_into_empty_success(fixture, monkeypatch):
    args, _, _ = fixture
    receipt = dict(workspace_id="workspace", registration_id="registration", checkout_id="checkout", source_snapshot_id="snapshot", head_generation="head", admission_epoch=0, policy_generation=1, config_fingerprint="config")
    entered = asyncio.Event()
    async def held(**kw):
        entered.set(); await asyncio.Event().wait()
    monkeypatch.setattr(bridge, "query_code", held)
    task = asyncio.create_task(bridge.resolve_resource(**args, body=dict(workspace_id="workspace", operation="resolve", reference=dict(kind="code", id="decl:" + "a" * 64, receipt=receipt))))
    await entered.wait(); task.cancel()
    with pytest.raises(asyncio.CancelledError): await task


@pytest.mark.parametrize("mutation,state", [("UPDATE fm_v2_sources SET forget_state='forgotten'", "erased"), ("DELETE FROM fm_v2_evidence", "missing")])
async def test_late_persisted_erasure_or_deletion_retains_no_quote(fixture, monkeypatch, mutation, state):
    args, path, _ = fixture
    import src.memory_versioned as versioned
    original, count = versioned.read_source_reference, 0
    def read(**kw):
        nonlocal count
        count += 1
        value = original(**kw)
        if count == 1:
            with sqlite3.connect(path) as conn: conn.execute(mutation)
        return value
    monkeypatch.setattr(versioned, "read_source_reference", read)
    result = await bridge.resolve_resource(**args, body=body())
    assert result["state"] == state and result["excerpt"] is None and result["provenance"] is None
