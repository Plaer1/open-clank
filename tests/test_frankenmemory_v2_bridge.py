import asyncio
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from types import SimpleNamespace

import pytest

from src.memory_provider import MemoryRecord
from src.frankenmemory_provider import FrankenmemoryProvider
from src.frankenmemory_v2 import (
    RevisionConflict,
    V2Repository,
    claim_job,
    complete_job,
    execute_v2_operation,
    get_current_record,
    list_current_records,
    mirror_capture_job,
    mirror_record,
    seal_job_manifest,
    search_current_records,
)


def _db(path):
    with sqlite3.connect(path) as conn:
        conn.executescript(
            """
            CREATE TABLE fm_v2_change_sets(owner_id TEXT, change_set_id TEXT, actor_type TEXT, actor_id TEXT, reason TEXT, source_event_id TEXT, contract_version TEXT, content_hash TEXT, created_at TEXT, PRIMARY KEY(owner_id,change_set_id));
            CREATE TABLE fm_v2_predicates(predicate TEXT PRIMARY KEY, value_type TEXT, cardinality TEXT, allow_unknown INTEGER DEFAULT 0);
            CREATE TABLE fm_v2_entities(owner_id TEXT, entity_id TEXT, workspace_key TEXT, project_key TEXT, created_change_set_id TEXT, current_revision INTEGER, created_at TEXT, PRIMARY KEY(owner_id,entity_id));
            CREATE TABLE fm_v2_entity_revisions(owner_id TEXT, entity_id TEXT, revision INTEGER, payload TEXT, content_hash TEXT, actor_type TEXT, actor_id TEXT, reason TEXT, change_set_id TEXT, previous_revision INTEGER, created_at TEXT, PRIMARY KEY(owner_id,entity_id,revision));
            CREATE TABLE fm_v2_entity_aliases(owner_id TEXT,entity_id TEXT,revision INTEGER,alias TEXT,PRIMARY KEY(owner_id,entity_id,revision,alias));
            CREATE TABLE fm_v2_entity_roles(owner_id TEXT,entity_id TEXT,revision INTEGER,workspace_key TEXT,project_key TEXT,role TEXT,is_current INTEGER,PRIMARY KEY(owner_id,entity_id,revision,role));
            CREATE UNIQUE INDEX one_current_self ON fm_v2_entity_roles(owner_id,workspace_key,project_key) WHERE role='self' AND is_current=1;
            CREATE TABLE fm_v2_entity_tags(owner_id TEXT,entity_id TEXT,revision INTEGER,tag TEXT,PRIMARY KEY(owner_id,entity_id,revision,tag));
            CREATE TABLE fm_v2_knowledge_blocks(owner_id TEXT, block_id TEXT, workspace_key TEXT, project_key TEXT, subject_entity_id TEXT, predicate TEXT, claim_slot TEXT, cardinality TEXT, created_change_set_id TEXT, current_revision INTEGER, created_at TEXT, PRIMARY KEY(owner_id,block_id));
            CREATE TABLE fm_v2_knowledge_revisions(owner_id TEXT, block_id TEXT, revision INTEGER, value_type TEXT, value_json TEXT, expected_value_type TEXT, kind TEXT, tags_json TEXT, status TEXT, confidence REAL, trust REAL, activation TEXT, activation_rationale TEXT, valid_from TEXT, valid_to TEXT, evidence_json TEXT, content_hash TEXT, actor_type TEXT, actor_id TEXT, reason TEXT, source_event_id TEXT, change_set_id TEXT, previous_revision INTEGER, created_at TEXT, PRIMARY KEY(owner_id,block_id,revision));
            CREATE TABLE fm_v2_conflicts(owner_id TEXT,conflict_id TEXT,block_id TEXT,current_revision INTEGER,competing_payload TEXT,state TEXT,rationale TEXT,created_change_set_id TEXT,resolved_change_set_id TEXT,resolved_at TEXT,created_at TEXT,PRIMARY KEY(owner_id,conflict_id));
            CREATE TABLE fm_v2_sources(owner_id TEXT, source_id TEXT, workspace_key TEXT, project_key TEXT, source_uri TEXT, source_revision INTEGER, content_hash TEXT, source_type TEXT, forget_state TEXT DEFAULT 'active', created_at TEXT, PRIMARY KEY(owner_id,source_id,source_revision));
            CREATE TABLE fm_v2_evidence(owner_id TEXT, evidence_id TEXT, source_id TEXT, source_revision INTEGER, locator_type TEXT, locator_json TEXT, quote TEXT, extraction_contract TEXT, parser_version TEXT, raw_value_json TEXT, normalized_value_json TEXT, created_at TEXT, PRIMARY KEY(owner_id,evidence_id));
            CREATE TABLE fm_v2_revision_evidence(owner_id TEXT,block_id TEXT,revision INTEGER,evidence_id TEXT,PRIMARY KEY(owner_id,block_id,revision,evidence_id));
            CREATE TABLE fm_v2_outbox(owner_id TEXT, event_id TEXT, change_set_id TEXT, kind TEXT, payload_json TEXT, source_revision INTEGER, state TEXT DEFAULT 'pending', created_at TEXT, PRIMARY KEY(owner_id,event_id));
            CREATE TABLE fm_v2_jobs(owner_id TEXT, job_id TEXT, kind TEXT, workspace_key TEXT, project_key TEXT, idempotency_key TEXT, state TEXT, current_attempt_id TEXT, attempt_count INTEGER, input_hash TEXT, result_json TEXT, created_at TEXT, updated_at TEXT, PRIMARY KEY(owner_id,job_id));
            """
        )


def test_bridge_is_typed_scoped_and_revisioned(tmp_path):
    path = str(tmp_path / "fm.db")
    _db(path)
    record = SimpleNamespace(
        id="m-name", text="E", category="identity", kind="identity",
        metadata={"about": "user", "predicate": "name", "tags": ["self"]},
        source_type="human", confidence_score=0.9, trust_score=0.8,
    )
    assert mirror_record(record, owner="alice", workspace_id="global", db_path=path)
    record.text = "Elliot"
    assert mirror_record(record, owner="alice", workspace_id="global", db_path=path)
    with sqlite3.connect(path) as conn:
        block = conn.execute("SELECT current_revision FROM fm_v2_knowledge_blocks").fetchone()
        assert block == (2,)
        revision = conn.execute("SELECT status,tags_json,value_json FROM fm_v2_knowledge_revisions ORDER BY revision DESC LIMIT 1").fetchone()
        assert revision[0] == "active"
        assert "self" in json.loads(revision[1])
        assert json.loads(revision[2])["value"] == "Elliot"
        assert conn.execute("SELECT max(source_revision) FROM fm_v2_sources").fetchone()[0] == 2
        assert conn.execute("SELECT max(source_revision) FROM fm_v2_evidence").fetchone()[0] == 2
        assert conn.execute("SELECT count(*) FROM fm_v2_outbox").fetchone()[0] == 2


def test_question_target_reuses_reserved_entity_without_relabeling_it(tmp_path):
    path = str(tmp_path / "fm.db")
    _db(path)
    repo = V2Repository(path)
    repo.create_entity(
        owner="alice", workspace_id="global", entity_id="handler-1",
        entity_type="person", canonical_label="Allie", roles=["handler"],
    )
    record = SimpleNamespace(
        id="m-question-handler", text="What is their name?", category="unknown",
        metadata={"question_context": {"mode": "missing_slot", "target": {"kind": "entity", "id": "handler-1"}, "predicate": "preferred_name", "claim_slot": "identity.preferred_name"}},
        source_type="human",
    )
    assert mirror_record(record, owner="alice", workspace_id="global", db_path=path)
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT count(*) FROM fm_v2_entity_revisions WHERE entity_id='handler-1'").fetchone()[0] == 1
        assert json.loads(conn.execute("SELECT payload FROM fm_v2_entity_revisions WHERE entity_id='handler-1'").fetchone()[0])["canonical_label"] == "Allie"
        assert conn.execute("SELECT subject_entity_id,predicate FROM fm_v2_knowledge_blocks").fetchone() == ("handler-1", "preferred_name")


def test_reviewed_handler_import_binds_reserved_entity_without_relabeling(tmp_path):
    path = str(tmp_path / "fm.db")
    _db(path)
    repo = V2Repository(path)
    handler_id = "principal_handler_" + ("a" * 32)
    repo.create_entity(
        owner="alice",
        workspace_id="global",
        entity_id=handler_id,
        entity_type="person",
        canonical_label="Handler",
        aliases=["handler"],
        roles=["handler"],
    )
    record = SimpleNamespace(
        id="m-handler-name",
        text="%USER%'s name is Allie.",
        category="identity",
        kind="persona",
        metadata={
            "category": "identity",
            "source_document_role": "handler_profile",
            "import_review": {"reviewed_by": "alice"},
            "subject_attribution": {
                "contract": "openclank.memory-subject-attribution/v1",
                "entity_id": handler_id,
                "role": "handler",
                "state": "proposed",
                "requires_review": True,
            },
        },
        source_type="human",
    )

    assert mirror_record(record, owner="alice", workspace_id="global", db_path=path)
    with sqlite3.connect(path) as conn:
        assert conn.execute(
            "SELECT subject_entity_id,predicate,claim_slot FROM fm_v2_knowledge_blocks"
        ).fetchone() == (handler_id, "preferred_name", "identity.preferred_name")
        assert conn.execute(
            "SELECT count(*) FROM fm_v2_entity_revisions WHERE entity_id=?",
            (handler_id,),
        ).fetchone() == (1,)
        payload = json.loads(
            conn.execute(
                "SELECT payload FROM fm_v2_entity_revisions WHERE entity_id=?",
                (handler_id,),
            ).fetchone()[0]
        )
    assert payload["canonical_label"] == "Handler"
    assert payload["roles"] == ["handler"]


@pytest.mark.parametrize(
    "mutation",
    [
        {"reviewed_by": "mallory"},
        {"entity_id": "principal_handler_" + ("b" * 32)},
        {"role": "assistant_self"},
    ],
)
def test_untrusted_handler_attribution_cannot_seize_reserved_entity(tmp_path, mutation):
    path = str(tmp_path / "fm.db")
    _db(path)
    handler_id = "principal_handler_" + ("a" * 32)
    V2Repository(path).create_entity(
        owner="alice",
        workspace_id="global",
        entity_id=handler_id,
        entity_type="person",
        canonical_label="Handler",
        roles=["handler"],
    )
    attribution = {
        "contract": "openclank.memory-subject-attribution/v1",
        "entity_id": handler_id,
        "role": "handler",
        "state": "proposed",
        "requires_review": True,
    }
    review = {"reviewed_by": "alice"}
    if "reviewed_by" in mutation:
        review.update(mutation)
    else:
        attribution.update(mutation)
    record = SimpleNamespace(
        id="m-malicious-name",
        text="%USER%'s name is Mallory.",
        category="identity",
        kind="persona",
        metadata={
            "import_review": review,
            "subject_attribution": attribution,
        },
        source_type="human",
    )

    assert mirror_record(record, owner="alice", workspace_id="global", db_path=path)
    with sqlite3.connect(path) as conn:
        subject = conn.execute(
            "SELECT subject_entity_id FROM fm_v2_knowledge_blocks"
        ).fetchone()[0]
    assert subject != handler_id


def test_concurrent_mirrors_serialize_revisions_and_outbox(tmp_path):
    path = str(tmp_path / "fm.db")
    _db(path)
    start = Barrier(2)

    def write(content):
        start.wait()
        return mirror_record(
            SimpleNamespace(
                id="m-race",
                text=content,
                category="fact",
                metadata={"about": "race", "predicate": "value"},
                source="user",
                source_type="human",
            ),
            owner="alice",
            workspace_id="global",
            db_path=path,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(write, ("first payload", "second payload")))
    assert results == [True, True]

    with sqlite3.connect(path) as conn:
        assert conn.execute(
            "SELECT current_revision FROM fm_v2_knowledge_blocks WHERE claim_slot='m-race'"
        ).fetchone() == (2,)
        revisions = conn.execute(
            "SELECT revision,value_json FROM fm_v2_knowledge_revisions ORDER BY revision"
        ).fetchall()
        assert [row[0] for row in revisions] == [1, 2]
        assert {json.loads(row[1])["value"] for row in revisions} == {
            "first payload",
            "second payload",
        }
        assert conn.execute("SELECT count(*) FROM fm_v2_change_sets").fetchone() == (2,)
        outbox = conn.execute(
            "SELECT change_set_id,payload_json FROM fm_v2_outbox"
        ).fetchall()
        assert len(outbox) == 2
        for change_set_id, payload_json in outbox:
            payload = json.loads(payload_json)
            assert conn.execute(
                "SELECT change_set_id FROM fm_v2_knowledge_revisions WHERE block_id=? AND revision=?",
                (payload["block_id"], payload["revision"]),
            ).fetchone() == (change_set_id,)


def test_mirror_persists_source_actor_and_activation_provenance(tmp_path):
    path = str(tmp_path / "fm.db")
    _db(path)
    records = (
        (
            SimpleNamespace(
                id="m-ai",
                text="agent discovered",
                category="fact",
                metadata={"about": "ai source"},
                source="ai_agent",
                source_type="ai",
            ),
            "upsert",
            "ai",
            "ai_agent",
            "candidate",
            None,
        ),
        (
            SimpleNamespace(
                id="m-reviewed",
                text="reviewed discovery",
                category="fact",
                metadata={"about": "reviewed source", "manually_reviewed": True},
                source="ai_agent",
                # The legacy review path currently labels the accepted row
                # human; the original source still identifies the author.
                source_type="human",
            ),
            "review_candidate",
            "ai",
            "ai_agent",
            "review_approved",
            "alice",
        ),
        (
            SimpleNamespace(
                id="m-unknown",
                text="unknown provenance",
                category="fact",
                metadata={"about": "unknown source"},
                source="mystery",
                source_type="mystery",
            ),
            "upsert",
            "automation",
            "mystery",
            "candidate",
            None,
        ),
    )
    for record, action, *_expected in records:
        assert mirror_record(
            record,
            owner="alice",
            workspace_id="global",
            action=action,
            db_path=path,
        )

    with sqlite3.connect(path) as conn:
        for record, action, actor_type, actor_id, activation, reviewer in records:
            knowledge = conn.execute(
                "SELECT r.actor_type,r.actor_id,r.activation,r.activation_rationale,r.change_set_id,b.subject_entity_id "
                "FROM fm_v2_knowledge_revisions r JOIN fm_v2_knowledge_blocks b "
                "ON b.owner_id=r.owner_id AND b.block_id=r.block_id "
                "WHERE b.owner_id='alice' AND b.claim_slot=?",
                (record.id,),
            ).fetchone()
            assert knowledge[:3] == (actor_type, actor_id, activation)
            rationale = json.loads(knowledge[3])["provenance"]
            assert rationale["reviewed_by"] == reviewer
            assert conn.execute(
                "SELECT actor_type,actor_id,reason FROM fm_v2_change_sets WHERE owner_id='alice' AND change_set_id=?",
                (knowledge[4],),
            ).fetchone() == (actor_type, actor_id, action)
            assert conn.execute(
                "SELECT actor_type,actor_id,reason FROM fm_v2_entity_revisions "
                "WHERE owner_id='alice' AND entity_id=? AND change_set_id=?",
                (knowledge[5], knowledge[4]),
            ).fetchone() == (actor_type, actor_id, action)
            assert conn.execute(
                "SELECT source_type FROM fm_v2_sources WHERE owner_id='alice' AND source_uri=?",
                (f"memory://{record.id}",),
            ).fetchone() == (("auto_extracted" if record.id == "m-unknown" else "ai"),)


def test_corrected_provenance_appends_even_when_content_is_unchanged(tmp_path):
    path = str(tmp_path / "fm.db")
    _db(path)
    record = SimpleNamespace(
        id="m-provenance",
        text="same content",
        category="fact",
        metadata={"about": "provenance correction"},
        source="mystery",
        source_type="mystery",
    )
    assert mirror_record(record, owner="alice", workspace_id="global", db_path=path)
    record.source = "ai_agent"
    record.source_type = "ai"
    assert mirror_record(record, owner="alice", workspace_id="global", db_path=path)

    with sqlite3.connect(path) as conn:
        assert conn.execute(
            "SELECT current_revision FROM fm_v2_knowledge_blocks"
        ).fetchone() == (2,)
        assert conn.execute(
            "SELECT actor_type,activation FROM fm_v2_knowledge_revisions ORDER BY revision"
        ).fetchall() == [("automation", "candidate"), ("ai", "candidate")]
        assert conn.execute(
            "SELECT source_revision,source_type FROM fm_v2_sources ORDER BY source_revision"
        ).fetchall() == [(1, "auto_extracted"), (2, "ai")]
        assert conn.execute("SELECT count(*) FROM fm_v2_outbox").fetchone() == (2,)


def test_owner_trust_is_append_only_and_separate_from_content(tmp_path):
    path = str(tmp_path / "fm.db")
    _db(path)
    repository = V2Repository(path)
    record = SimpleNamespace(
        id="trust-me", text="A durable claim", category="fact", kind="fact",
        metadata={"about": "claim", "predicate": "value"}, source_type="ai",
    )
    assert mirror_record(record, owner="alice", workspace_id="global", db_path=path)
    block_id, revision, original_hash = sqlite3.connect(path).execute(
        "SELECT block_id,current_revision,content_hash FROM fm_v2_knowledge_blocks b JOIN fm_v2_knowledge_revisions r USING(owner_id,block_id) WHERE b.owner_id='alice' ORDER BY r.revision DESC LIMIT 1"
    ).fetchone()
    assigned = repository.assign_trust(
        owner="alice", subject_kind="knowledge_revision", subject_id=block_id,
        subject_revision=revision, trust=0.82, reason_code="owner_review",
    )
    assert assigned["state"] == "assigned"
    assert repository.get_trust(
        owner="alice", subject_kind="knowledge_revision", subject_id=block_id,
        subject_revision=revision,
    )["trust"] == pytest.approx(0.82)
    revoked = repository.assign_trust(
        owner="alice", subject_kind="knowledge_revision", subject_id=block_id,
        subject_revision=revision, state="revoked", reason_code="owner_retraction",
        expected_assignment_id=assigned["assignment_id"],
    )
    assert revoked["trust"] is None
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT count(*) FROM fm_v2_trust_assignments").fetchone() == (2,)
        assert conn.execute(
            "SELECT content_hash FROM fm_v2_knowledge_revisions WHERE owner_id='alice' AND block_id=? AND revision=?",
            (block_id, revision),
        ).fetchone()[0] == original_hash


def test_provider_trust_bridge_round_trips_owner_assignment(tmp_path):
    path = str(tmp_path / "fm.db")
    _db(path)
    record = SimpleNamespace(
        id="provider-trust", text="Provider-visible claim", category="fact",
        kind="fact", metadata={"about": "claim", "predicate": "value"},
        source_type="ai",
    )
    assert mirror_record(record, owner="alice", workspace_id="global", db_path=path)
    block_id, revision = sqlite3.connect(path).execute(
        "SELECT block_id,current_revision FROM fm_v2_knowledge_blocks WHERE owner_id='alice'"
    ).fetchone()
    provider = FrankenmemoryProvider(command="/unused", env={"FM_DB_PATH": path})
    assigned = asyncio.run(provider.assign_trust(
        owner="alice", subject_kind="knowledge_revision", subject_id=block_id,
        subject_revision=revision, trust=0.73, reason_code="owner_review",
    ))
    current = asyncio.run(provider.get_trust(
        owner="alice", subject_kind="knowledge_revision", subject_id=block_id,
        subject_revision=revision, workspace_id="global",
    ))
    assert assigned["assignment_id"] == current["assignment_id"]
    assert current["trust"] == pytest.approx(0.73)


def test_derived_job_manifest_seals_accounting_and_retries_idempotently(tmp_path):
    path = str(tmp_path / "fm.db")
    _db(path)
    selection = seal_job_manifest(
        owner="alice", job_id="job-1", phase="selection", selected_ids=["a", "b"],
        config_hash="cfg", model_hash="model", tool_hash="tool", input_hash="input",
        db_path=path,
    )
    assert selection["phase"] == "selection"
    outcome = seal_job_manifest(
        owner="alice", job_id="job-1", phase="outcome", selected_ids=["b", "a"],
        completed_ids=["a"], reused_ids=["b"], config_hash="cfg", model_hash="model",
        tool_hash="tool", input_hash="input", parent_manifest_id=selection["manifest_id"],
        db_path=path,
    )
    assert outcome["phase"] == "outcome"
    retry = seal_job_manifest(
        owner="alice", job_id="job-1", phase="outcome", selected_ids=["a", "b"],
        completed_ids=["a"], reused_ids=["b"], config_hash="cfg", model_hash="model",
        tool_hash="tool", input_hash="input", parent_manifest_id=selection["manifest_id"],
        db_path=path,
    )
    assert retry["manifest_id"] == outcome["manifest_id"]
    with pytest.raises(Exception):
        seal_job_manifest(
            owner="alice", job_id="job-1", phase="outcome", selected_ids=["a", "b"],
            completed_ids=["a"], failed_ids=["a", "b"], config_hash="cfg", model_hash="model",
            tool_hash="tool", input_hash="input", parent_manifest_id=selection["manifest_id"],
            db_path=path,
        )


def test_derived_job_manifest_seal_opens_immediate_write_transaction(tmp_path, monkeypatch):
    """The read-then-insert seal must hold the write lock from the start."""
    path = str(tmp_path / "fm.db")
    _db(path)
    statements = []
    real_connect = sqlite3.connect

    class _RecordingConnection:
        def __init__(self, *args, **kwargs):
            self._conn = real_connect(*args, **kwargs)

        @property
        def row_factory(self):
            return self._conn.row_factory

        @row_factory.setter
        def row_factory(self, value):
            self._conn.row_factory = value

        def execute(self, sql, parameters=()):
            statements.append(sql.lstrip().upper())
            return self._conn.execute(sql, parameters)

        def commit(self):
            self._conn.commit()

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return self._conn.__exit__(*exc)

    monkeypatch.setattr(sqlite3, "connect", _RecordingConnection)
    seal_job_manifest(
        owner="alice", job_id="job-lock", phase="selection", selected_ids=["a"],
        config_hash="cfg", model_hash="model", tool_hash="tool", input_hash="input",
        db_path=path,
    )
    read = next(
        index
        for index, sql in enumerate(statements)
        if sql.startswith("SELECT * FROM FM_V2_JOB_MANIFESTS")
    )
    assert any(sql == "BEGIN IMMEDIATE" for sql in statements[:read])


def test_derived_job_manifest_concurrent_sealers_get_one_idempotent_row(tmp_path):
    path = str(tmp_path / "fm.db")
    _db(path)
    start = Barrier(4)

    def seal(_):
        start.wait()
        return seal_job_manifest(
            owner="alice", job_id="job-race", phase="selection", selected_ids=["a", "b"],
            config_hash="cfg", model_hash="model", tool_hash="tool", input_hash="input",
            db_path=path,
        )

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(seal, range(4)))
    assert {result["manifest_id"] for result in results} == {results[0]["manifest_id"]}
    with sqlite3.connect(path) as conn:
        assert conn.execute(
            "SELECT count(*) FROM fm_v2_job_manifests "
            "WHERE owner_id='alice' AND job_id='job-race'"
        ).fetchone() == (1,)


def test_open_question_is_a_block_and_capture_is_idempotent(tmp_path):
    path = str(tmp_path / "fm.db")
    _db(path)
    question = SimpleNamespace(
        id="candidate-q", text="What is the user's name?", category="unknown",
        kind="unknown", metadata={"about": "user", "predicate": "name", "tags": ["self"]},
        source_type="human",
    )
    assert mirror_record(question, owner="alice", workspace_id="global", db_path=path)
    assert mirror_capture_job(owner="alice", session_id="s1", user_text="hi", assistant_text="hello", workspace_id="global", db_path=path)
    assert mirror_capture_job(owner="alice", session_id="s1", user_text="hi", assistant_text="hello", workspace_id="global", db_path=path)
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT kind,status,value_type FROM fm_v2_knowledge_revisions").fetchone() == ("open_question", "open", "null")
        value = json.loads(conn.execute("SELECT value_json FROM fm_v2_knowledge_revisions").fetchone()[0])
        assert value == {"type": "null"}
        rationale = json.loads(conn.execute("SELECT activation_rationale FROM fm_v2_knowledge_revisions").fetchone()[0])
        assert rationale["about"]["label"] == "user"
        assert conn.execute("SELECT workspace_key FROM fm_v2_jobs").fetchone()[0] == "global"
        assert conn.execute("SELECT count(*) FROM fm_v2_jobs").fetchone()[0] == 1


def test_job_claim_and_completion_are_fenced(tmp_path):
    path = str(tmp_path / "fm.db")
    _db(path)
    with sqlite3.connect(path) as conn:
        conn.execute("INSERT INTO fm_v2_jobs(owner_id,job_id,kind,workspace_key,project_key,idempotency_key,state,attempt_count,input_hash,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)", ("alice", "job-1", "parse", "global", "", "parse:1", "queued", 0, "hash", "now", "now"))
        conn.execute("CREATE TABLE fm_v2_attempts(owner_id TEXT,attempt_id TEXT PRIMARY KEY,job_id TEXT,attempt_number INTEGER,state TEXT,lease_epoch INTEGER,lease_owner TEXT,lease_expires_at TEXT,heartbeat_at TEXT,progress_watermark TEXT,error_json TEXT,created_at TEXT,updated_at TEXT)")
    claim = claim_job(owner="alice", job_id="job-1", worker_id="w1", db_path=path)
    assert claim and claim["lease_epoch"] == 1
    assert claim_job(owner="alice", job_id="job-1", worker_id="w2", db_path=path) is None
    assert not complete_job(owner="alice", job_id="job-1", attempt_id=claim["attempt_id"], worker_id="w2", lease_epoch=1, db_path=path)
    assert complete_job(owner="alice", job_id="job-1", attempt_id=claim["attempt_id"], worker_id="w1", lease_epoch=1, db_path=path, result={"ok": True})
    assert not complete_job(owner="alice", job_id="job-1", attempt_id=claim["attempt_id"], worker_id="w1", lease_epoch=1, db_path=path)


def test_expired_job_lease_is_reclaimed_and_stale_worker_stays_fenced(tmp_path):
    path = str(tmp_path / "fm.db")
    _db(path)
    with sqlite3.connect(path) as conn:
        conn.execute("INSERT INTO fm_v2_jobs(owner_id,job_id,kind,workspace_key,project_key,idempotency_key,state,attempt_count,input_hash,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)", ("alice", "job-1", "parse", "global", "", "parse:1", "queued", 0, "hash", "now", "now"))
        conn.execute("CREATE TABLE fm_v2_attempts(owner_id TEXT,attempt_id TEXT PRIMARY KEY,job_id TEXT,attempt_number INTEGER,state TEXT,lease_epoch INTEGER,lease_owner TEXT,lease_expires_at TEXT,heartbeat_at TEXT,progress_watermark TEXT,error_json TEXT,created_at TEXT,updated_at TEXT)")

    stale = claim_job(owner="alice", job_id="job-1", worker_id="w1", lease_seconds=1, db_path=path)
    assert stale
    with sqlite3.connect(path) as conn:
        conn.execute(
            "UPDATE fm_v2_attempts SET lease_expires_at='1970-01-01T00:00:00+00:00' WHERE attempt_id=?",
            (stale["attempt_id"],),
        )

    current = claim_job(owner="alice", job_id="job-1", worker_id="w2", db_path=path)
    assert current and current["lease_epoch"] == 2
    assert not complete_job(owner="alice", job_id="job-1", attempt_id=stale["attempt_id"], worker_id="w1", lease_epoch=1, db_path=path)
    assert complete_job(owner="alice", job_id="job-1", attempt_id=current["attempt_id"], worker_id="w2", lease_epoch=2, db_path=path)
    with sqlite3.connect(path) as conn:
        assert conn.execute(
            "SELECT state FROM fm_v2_attempts WHERE attempt_id=?", (stale["attempt_id"],)
        ).fetchone() == ("failed_retryable",)


def test_v2_native_reader_is_tenant_scoped(tmp_path):
    path = str(tmp_path / "fm.db")
    _db(path)
    record = SimpleNamespace(id="m-a", text="Alice's private fact", category="fact", metadata={})
    other = SimpleNamespace(id="m-b", text="Bob's private fact", category="fact", metadata={})
    assert mirror_record(record, owner="alice", workspace_id="global", db_path=path)
    assert mirror_record(other, owner="bob", workspace_id="global", db_path=path)
    rows = list_current_records(owner="alice", workspace_id="global", db_path=path)
    assert [row["text"] for row in rows] == ["Alice's private fact"]
    assert list_current_records(owner="bob", workspace_id="global", db_path=path)[0]["owner"] == "bob"


def test_v2_preferred_reader_requires_live_legacy_source(tmp_path):
    path = str(tmp_path / "fm.db")
    _db(path)
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE curated (id TEXT PRIMARY KEY, content TEXT, owner TEXT, workspace_id TEXT DEFAULT 'global', archived INTEGER DEFAULT 0)")
    live = SimpleNamespace(id="m-live", text="still current", category="fact", metadata={})
    stale = SimpleNamespace(id="m-stale", text="shadow only", category="fact", metadata={})
    assert mirror_record(live, owner="alice", workspace_id="global", db_path=path)
    assert mirror_record(stale, owner="alice", workspace_id="global", db_path=path)
    with sqlite3.connect(path) as conn:
        conn.execute("INSERT INTO curated(id,content,owner,workspace_id,archived) VALUES (?,?,?,'global',0)", ("m-live", "still current", "alice"))
    rows = list_current_records(owner="alice", workspace_id="global", require_legacy_presence=True, db_path=path)
    assert [row["id"] for row in rows] == ["m-live"]
    hits = search_current_records("current", owner="alice", workspace_id="global", require_legacy_presence=True, db_path=path)
    assert [row["id"] for row in hits] == ["m-live"]
    assert get_current_record("m-live", owner="alice", workspace_id="global", require_legacy_presence=True, db_path=path)["text"] == "still current"
    assert get_current_record("m-stale", owner="alice", workspace_id="global", require_legacy_presence=True, db_path=path) is None


def test_bridge_preserves_category_and_revisions_predicate_only_edits(tmp_path):
    path = str(tmp_path / "fm.db")
    _db(path)
    record = MemoryRecord(
        id="m-project",
        text="ships Friday",
        category="project",
        kind="project",
        metadata={"about": "Open Clank", "predicate": "deadline"},
    )
    assert mirror_record(record, owner="alice", workspace_id="global", db_path=path)
    assert list_current_records(owner="alice", workspace_id="global", db_path=path)[0]["category"] == "project"

    record.metadata["predicate"] = "schedule"
    assert mirror_record(record, owner="alice", workspace_id="global", db_path=path)
    record.category = record.kind = "preference"
    assert mirror_record(record, owner="alice", workspace_id="global", db_path=path)
    assert mirror_record(record, owner="alice", workspace_id="global", db_path=path)

    with sqlite3.connect(path) as conn:
        assert conn.execute(
            "SELECT predicate,current_revision FROM fm_v2_knowledge_blocks"
        ).fetchone() == ("schedule", 3)
        assert conn.execute("SELECT count(*) FROM fm_v2_change_sets").fetchone()[0] == 3
        assert conn.execute("SELECT count(*) FROM fm_v2_outbox").fetchone()[0] == 3
    current = list_current_records(owner="alice", workspace_id="global", db_path=path)[0]
    assert current["category"] == "preference"


def test_current_reader_deduplicates_before_limit(tmp_path):
    path = str(tmp_path / "fm.db")
    _db(path)
    assert mirror_record(SimpleNamespace(id="m-b", text="B", category="fact", metadata={}), owner="alice", workspace_id="global", db_path=path)
    assert mirror_record(SimpleNamespace(id="m-a", text="A", category="fact", metadata={}), owner="alice", workspace_id="global", db_path=path)
    with sqlite3.connect(path) as conn:
        block = conn.execute(
            "SELECT * FROM fm_v2_knowledge_blocks WHERE claim_slot='m-a'"
        ).fetchone()
        revision = conn.execute(
            "SELECT * FROM fm_v2_knowledge_revisions WHERE block_id=?", (block[1],)
        ).fetchone()
        duplicate_block = list(block)
        duplicate_block[1] = "legacy-category-duplicate"
        duplicate_block[5] = "old-category"
        duplicate_block[-1] = "9999-01-01T00:00:00Z"
        duplicate_revision = list(revision)
        duplicate_revision[1] = "legacy-category-duplicate"
        duplicate_revision[-1] = "9999-01-01T00:00:00Z"
        conn.execute(
            "INSERT INTO fm_v2_knowledge_blocks VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            duplicate_block,
        )
        conn.execute(
            "INSERT INTO fm_v2_knowledge_revisions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            duplicate_revision,
        )

    rows = list_current_records(owner="alice", workspace_id="global", limit=2, db_path=path)
    assert [row["id"] for row in rows] == ["m-a", "m-b"]


def _canonical_repository(path):
    _db(path)
    repository = V2Repository(path)
    repository.create_entity(
        owner="alice",
        workspace_id="workspace",
        entity_id="entity-project",
        entity_type="project",
        canonical_label="Open Clank",
        roles=["subject"],
        tags=["memory"],
    )
    return repository


def _knowledge_payload(text, *, status="active"):
    return {
        "value": {"type": "string", "value": text},
        "expected_value_type": "string",
        "kind": "fact",
        "tags": ["release"],
        "status": status,
        "confidence": 0.9,
        "trust": 0.8,
        "activation": "manual",
        "activation_rationale": "user authored",
        "evidence_ids": [],
    }


def test_repository_crud_history_diff_blame_conflict_and_revert(tmp_path):
    path = str(tmp_path / "fm.db")
    repository = _canonical_repository(path)
    created = repository.create_knowledge(
        owner="alice",
        workspace_id="workspace",
        block_id="block-release",
        subject_entity_id="entity-project",
        predicate="release_note",
        claim_slot="current",
        payload=_knowledge_payload("alpha"),
    )
    assert created["revision"] == 1

    updated = repository.update_knowledge(
        owner="alice",
        workspace_id="workspace",
        block_id="block-release",
        expected_revision=1,
        payload={"value": {"type": "string", "value": "beta"}},
        reason="correct release",
    )
    assert updated["revision"] == 2
    assert updated["value"]["value"] == "beta"

    with pytest.raises(RevisionConflict) as stale:
        repository.update_knowledge(
            owner="alice",
            workspace_id="workspace",
            block_id="block-release",
            expected_revision=1,
            payload={"value": {"type": "string", "value": "lost update"}},
        )
    assert stale.value.current["revision"] == 2
    assert stale.value.proposal["expected_revision"] == 1

    diff = repository.knowledge_diff(
        owner="alice",
        workspace_id="workspace",
        block_id="block-release",
        from_revision=1,
        to_revision=2,
    )
    assert diff["changes"]["value"] == {
        "before": {"type": "string", "value": "alpha"},
        "after": {"type": "string", "value": "beta"},
    }
    blame = repository.knowledge_blame(
        owner="alice", workspace_id="workspace", block_id="block-release"
    )
    assert blame["fields"]["value"]["revision"] == 2

    reverted = repository.revert_knowledge(
        owner="alice",
        workspace_id="workspace",
        block_id="block-release",
        expected_revision=2,
        target_revision=1,
    )
    assert reverted["revision"] == 3
    assert reverted["value"]["value"] == "alpha"
    assert repository.get_knowledge(
        owner="alice", workspace_id="workspace", block_id="block-release", revision=2
    )["value"]["value"] == "beta"

    conflict = repository.record_conflict(
        owner="alice",
        workspace_id="workspace",
        block_id="block-release",
        expected_revision=2,
        proposal={"value": {"type": "string", "value": "gamma"}},
    )
    assert conflict["current"]["revision"] == 3
    assert repository.get_knowledge(
        owner="alice", workspace_id="workspace", block_id="block-release"
    )["revision"] == 3
    resolved = repository.resolve_conflict(
        owner="alice",
        workspace_id="workspace",
        conflict_id=conflict["conflict_id"],
        expected_revision=3,
        payload={"value": {"type": "string", "value": "gamma"}},
    )
    assert resolved["revision"] == 4
    assert resolved["value"]["value"] == "gamma"
    assert [row["revision"] for row in repository.knowledge_history(
        owner="alice", workspace_id="workspace", block_id="block-release"
    )] == [1, 2, 3, 4]


def test_expected_revision_writers_have_one_winner(tmp_path):
    path = str(tmp_path / "fm.db")
    repository = _canonical_repository(path)
    repository.create_knowledge(
        owner="alice",
        workspace_id="workspace",
        block_id="block-race",
        subject_entity_id="entity-project",
        predicate="race",
        claim_slot="one",
        payload=_knowledge_payload("initial"),
    )
    start = Barrier(2)

    def update(value):
        start.wait()
        try:
            repository.update_knowledge(
                owner="alice",
                workspace_id="workspace",
                block_id="block-race",
                expected_revision=1,
                payload={"value": {"type": "string", "value": value}},
            )
            return "won"
        except RevisionConflict:
            return "conflict"

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = sorted(pool.map(update, ("left", "right")))
    assert outcomes == ["conflict", "won"]
    with sqlite3.connect(path) as conn:
        assert conn.execute(
            "SELECT current_revision FROM fm_v2_knowledge_blocks WHERE block_id='block-race'"
        ).fetchone() == (2,)
        assert conn.execute(
            "SELECT count(*) FROM fm_v2_knowledge_revisions WHERE block_id='block-race'"
        ).fetchone() == (2,)


def test_repository_rolls_back_when_outbox_write_crashes(tmp_path):
    path = str(tmp_path / "fm.db")
    repository = _canonical_repository(path)
    with sqlite3.connect(path) as conn:
        before_change_sets = conn.execute("SELECT count(*) FROM fm_v2_change_sets").fetchone()[0]
        conn.executescript(
            """
            CREATE TRIGGER fail_knowledge_outbox
            BEFORE INSERT ON fm_v2_outbox WHEN NEW.kind='knowledge_revision'
            BEGIN SELECT RAISE(ABORT, 'simulated outbox failure'); END;
            """
        )
    with pytest.raises(sqlite3.IntegrityError, match="simulated outbox failure"):
        repository.create_knowledge(
            owner="alice",
            workspace_id="workspace",
            block_id="block-crash",
            subject_entity_id="entity-project",
            predicate="crash",
            claim_slot="one",
            payload=_knowledge_payload("must roll back"),
        )
    with sqlite3.connect(path) as conn:
        assert conn.execute(
            "SELECT count(*) FROM fm_v2_knowledge_blocks WHERE block_id='block-crash'"
        ).fetchone() == (0,)
        assert conn.execute("SELECT count(*) FROM fm_v2_change_sets").fetchone() == (before_change_sets,)


def test_repository_scope_and_disabled_transport_fail_closed(tmp_path):
    path = str(tmp_path / "fm.db")
    repository = _canonical_repository(path)
    repository.create_knowledge(
        owner="alice",
        workspace_id="workspace",
        block_id="block-private",
        subject_entity_id="entity-project",
        predicate="private",
        claim_slot="one",
        payload=_knowledge_payload("private"),
    )
    with pytest.raises(Exception, match="not found"):
        repository.get_knowledge(owner="bob", workspace_id="workspace", block_id="block-private")

    request = {
        "contract_version": "frankenmemory.v2",
        "operation": "knowledge.get",
        "scope": {"owner_id": "alice", "workspace_id": "workspace", "project_id": None},
        "payload": {"block_id": "block-private"},
    }
    assert execute_v2_operation(request, authenticated_owner="alice", db_path=path)["payload"]["error"]["code"] == "unsupported_contract"
    denied = execute_v2_operation(request, authenticated_owner="bob", enabled=True, db_path=path)
    assert denied["payload"]["error"]["code"] == "scope_denied"
    assert "block-private" not in json.dumps(denied)
    allowed = execute_v2_operation(request, authenticated_owner="alice", enabled=True, db_path=path)
    assert allowed["payload"]["block_id"] == "block-private"
