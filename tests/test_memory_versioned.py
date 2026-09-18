import sqlite3

import pytest

from src.frankenmemory_v2 import V2Repository
from src.memory_versioned import (
    VersionedMemoryConflict,
    VersionedMemoryError,
    canonical_graph,
    get_knowledge,
    list_knowledge,
    propose_knowledge,
    transition_knowledge,
)


def _schema(path):
    with sqlite3.connect(path) as conn:
        conn.executescript(
            """
            PRAGMA foreign_keys=ON;
            CREATE TABLE fm_v2_change_sets(owner_id TEXT, change_set_id TEXT, actor_type TEXT, actor_id TEXT, reason TEXT, source_event_id TEXT, contract_version TEXT, content_hash TEXT, created_at TEXT, PRIMARY KEY(owner_id,change_set_id));
            CREATE TABLE fm_v2_entities(owner_id TEXT, entity_id TEXT, workspace_key TEXT, project_key TEXT, created_change_set_id TEXT, current_revision INTEGER, created_at TEXT, PRIMARY KEY(owner_id,entity_id));
            CREATE TABLE fm_v2_entity_revisions(owner_id TEXT, entity_id TEXT, revision INTEGER, payload TEXT, content_hash TEXT, actor_type TEXT, actor_id TEXT, reason TEXT, change_set_id TEXT, previous_revision INTEGER, created_at TEXT, PRIMARY KEY(owner_id,entity_id,revision));
            CREATE TABLE fm_v2_predicates(predicate TEXT PRIMARY KEY, value_type TEXT, cardinality TEXT, allow_unknown INTEGER DEFAULT 0);
            CREATE TABLE fm_v2_knowledge_blocks(owner_id TEXT, block_id TEXT, workspace_key TEXT, project_key TEXT, subject_entity_id TEXT, predicate TEXT, claim_slot TEXT, cardinality TEXT, created_change_set_id TEXT, current_revision INTEGER, created_at TEXT, PRIMARY KEY(owner_id,block_id));
            CREATE TABLE fm_v2_knowledge_revisions(owner_id TEXT, block_id TEXT, revision INTEGER, value_type TEXT, value_json TEXT, expected_value_type TEXT, kind TEXT, tags_json TEXT, status TEXT, confidence REAL, trust REAL, activation TEXT, activation_rationale TEXT, valid_from TEXT, valid_to TEXT, evidence_json TEXT, content_hash TEXT, actor_type TEXT, actor_id TEXT, reason TEXT, source_event_id TEXT, change_set_id TEXT, previous_revision INTEGER, created_at TEXT, PRIMARY KEY(owner_id,block_id,revision));
            CREATE TABLE fm_v2_conflicts(owner_id TEXT, conflict_id TEXT, block_id TEXT, current_revision INTEGER, competing_payload TEXT, state TEXT, rationale TEXT, created_change_set_id TEXT, resolved_change_set_id TEXT, created_at TEXT, resolved_at TEXT, PRIMARY KEY(owner_id,conflict_id));
            CREATE TABLE fm_v2_sources(owner_id TEXT, source_id TEXT, workspace_key TEXT, project_key TEXT, source_uri TEXT, source_revision INTEGER, content_hash TEXT, source_type TEXT, forget_state TEXT DEFAULT 'active', created_at TEXT, PRIMARY KEY(owner_id,source_id,source_revision));
            CREATE TABLE fm_v2_evidence(owner_id TEXT, evidence_id TEXT, source_id TEXT, source_revision INTEGER, locator_type TEXT, locator_json TEXT, quote TEXT, extraction_contract TEXT, parser_version TEXT, raw_value_json TEXT, normalized_value_json TEXT, created_at TEXT, PRIMARY KEY(owner_id,evidence_id));
            CREATE TABLE fm_v2_outbox(owner_id TEXT, event_id TEXT, change_set_id TEXT, kind TEXT, payload_json TEXT, source_revision INTEGER, state TEXT DEFAULT 'pending', created_at TEXT, PRIMARY KEY(owner_id,event_id));
            CREATE TRIGGER fm_v2_status_contract BEFORE INSERT ON fm_v2_knowledge_revisions
            WHEN NEW.status NOT IN ('open','active','superseded','retracted')
              OR (NEW.status='open' AND (NEW.value_type<>'null' OR NEW.expected_value_type IS NULL))
            BEGIN SELECT RAISE(ABORT, 'invalid knowledge revision'); END;
            """
        )


def _repository(path, *, workspace="global", project=None):
    _schema(path)
    repository = V2Repository(path)
    repository.create_entity(
        owner="alice",
        workspace_id=workspace,
        project_id=project,
        entity_id="entity-user",
        entity_type="person",
        canonical_label="User",
        roles=["self"],
        tags=["self"],
    )
    return repository


def _payload(value, *, kind="fact", status="active", expected="string"):
    return {
        "value": value,
        "expected_value_type": expected,
        "kind": kind,
        "tags": ["self"],
        "status": status,
        "confidence": 1.0,
        "trust": 1.0,
        "activation": "manual",
        "activation_rationale": {"source": "test"},
        "evidence_ids": [],
    }


def test_proposals_are_not_duplicated_in_v2_knowledge(tmp_path):
    path = str(tmp_path / "fm.db")
    _schema(path)
    with pytest.raises(VersionedMemoryError, match="Rust candidate queue"):
        propose_knowledge(owner="alice", value="do not publish", db_path=path)
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT count(*) FROM fm_v2_knowledge_revisions").fetchone() == (0,)


def test_published_edit_history_and_conflict_are_revisioned(tmp_path):
    path = str(tmp_path / "fm.db")
    repository = _repository(path)
    repository.create_knowledge(
        owner="alice",
        workspace_id="global",
        block_id="block-color",
        subject_entity_id="entity-user",
        predicate="favorite_color",
        claim_slot="favorite-color",
        payload=_payload({"type": "string", "value": "blue"}),
        source_event_id="memory:memory-color",
    )
    edited = transition_knowledge(
        "memory-color",
        "edit",
        owner="alice",
        expected_revision=1,
        value="green",
        db_path=path,
    )
    assert edited["block_id"] == "block-color"
    assert edited["current"]["status"] == "active"
    assert edited["current"]["text"] == "green"

    with pytest.raises(VersionedMemoryConflict) as stale:
        transition_knowledge(
            "block-color",
            "correct",
            owner="alice",
            expected_revision=1,
            value="purple",
            db_path=path,
        )
    assert stale.value.current["revision"] == 2
    detail = get_knowledge("memory-color", owner="alice", db_path=path)
    assert [revision["revision"] for revision in detail["history"]] == [2, 1]
    assert detail["conflicts"][0]["state"] == "open"


def test_open_question_resolves_reopens_and_reverts_same_block(tmp_path):
    path = str(tmp_path / "fm.db")
    repository = _repository(path)
    repository.create_knowledge(
        owner="alice",
        workspace_id="global",
        block_id="block-name",
        subject_entity_id="entity-user",
        predicate="name",
        claim_slot="user-name",
        payload=_payload(
            {"type": "null"},
            kind="open_question",
            status="open",
            expected="string",
        ),
        source_event_id="memory:question-name",
    )
    resolved = transition_knowledge(
        "question-name",
        "resolve",
        owner="alice",
        expected_revision=1,
        value="E",
        actor_type="ai",
        actor_id="open-clank-agent",
        db_path=path,
    )
    assert resolved["block_id"] == "block-name"
    assert resolved["current"]["status"] == "active"
    assert resolved["current"]["value"] == {"type": "string", "value": "E"}

    reopened = transition_knowledge(
        "block-name", "reopen", owner="alice", expected_revision=2, db_path=path
    )
    assert reopened["current"]["status"] == "open"
    assert reopened["current"]["value"] == {"type": "null"}

    reverted = transition_knowledge(
        "block-name",
        "revert",
        owner="alice",
        expected_revision=3,
        target_revision=2,
        db_path=path,
    )
    assert reverted["current"]["status"] == "active"
    assert reverted["current"]["value"]["value"] == "E"


def test_project_scope_isolated_canonical_graph(tmp_path):
    path = str(tmp_path / "fm.db")
    repository = _repository(path, workspace="repo", project="open-clank")
    repository.create_knowledge(
        owner="alice",
        workspace_id="repo",
        project_id="open-clank",
        block_id="block-guidance",
        subject_entity_id="entity-user",
        predicate="project_guidance",
        claim_slot="safe-imports",
        payload=_payload(
            {"type": "string", "value": "Keep imports inside the workspace."},
            kind="fact",
        ),
    )
    assert list_knowledge(
        owner="alice", workspace_id="repo", project_id="other", db_path=path
    ) == []
    graph = canonical_graph(
        "overview",
        owner="alice",
        workspace_id="repo",
        project_id="open-clank",
        db_path=path,
    )
    assert graph["canonical"] is True
    assert "block-guidance" in {node["id"] for node in graph["nodes"]}
    assert {edge["tag"] for edge in graph["edges"]} >= {"project_guidance"}
    narrow = canonical_graph(
        "overview",
        owner="alice",
        workspace_id="repo",
        project_id="open-clank",
        limit=1,
        db_path=path,
    )
    visible = {node["id"] for node in narrow["nodes"]}
    assert all(
        edge["src_id"] in visible and edge["dst_id"] in visible
        for edge in narrow["edges"]
    )
    assert canonical_graph(
        "overview", owner="bob", workspace_id="repo", project_id="open-clank", db_path=path
    )["node_total"] == 0


def test_canonical_graph_trace_uses_browser_contract_node_ids(tmp_path):
    path = str(tmp_path / "fm.db")
    repository = _repository(path)
    repository.create_knowledge(
        owner="alice",
        workspace_id="global",
        block_id="block-tea",
        subject_entity_id="entity-user",
        predicate="likes",
        claim_slot="favorite-drink",
        payload=_payload({"type": "string", "value": "tea"}),
    )

    graph = canonical_graph(
        "trace",
        owner="alice",
        node_id="entity-user",
        to_node_id="block-tea",
        db_path=path,
    )

    assert graph["paths"]
    assert graph["paths"][0]["node_ids"] == ["entity-user", "block-tea"]
    assert "nodes" not in graph["paths"][0]


def _curated(path):
    with sqlite3.connect(path) as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS curated("
            "id TEXT PRIMARY KEY, owner TEXT, workspace_id TEXT, archived INTEGER DEFAULT 0)"
        )


def _curated_add(path, memory_id, *, archived=0):
    with sqlite3.connect(path) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO curated(id, owner, workspace_id, archived) VALUES (?,?,?,?)",
            (memory_id, "alice", "global", archived),
        )


def _curated_archive(path, memory_id, archived):
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE curated SET archived=? WHERE id=?", (archived, memory_id))


def _question_block(path, block_suffix, memory_id, **payload_overrides):
    with sqlite3.connect(path) as conn:
        initialized = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='fm_v2_change_sets'"
        ).fetchone()
    repository = V2Repository(path) if initialized else _repository(path)
    repository.create_knowledge(
        owner="alice",
        workspace_id="global",
        block_id=f"block-{block_suffix}",
        subject_entity_id="entity-user",
        predicate="question",
        claim_slot=f"slot-{block_suffix}",
        payload=_payload(
            {"type": "null"}, kind="open_question", status="open", **payload_overrides
        ),
        source_event_id=f"memory:{memory_id}",
    )


def test_resolved_question_stays_visible_with_archived_legacy_row(tmp_path):
    """S02: resolving archives the compatibility row by design; the answer
    must remain reachable through the gated v2 read paths."""
    from src.frankenmemory_v2 import (
        get_current_record,
        list_current_records,
        search_current_records,
    )

    path = str(tmp_path / "fm.db")
    _question_block(path, "name", "question-name")
    _curated(path)
    _curated_add(path, "question-name")
    # A stale open question whose legacy row is gone stays hidden.
    _question_block(path, "stale", "question-stale")
    _curated_add(path, "question-stale", archived=1)

    resolved = transition_knowledge(
        "question-name", "resolve", owner="alice", expected_revision=1,
        value="E", actor_type="user", actor_id="alice", db_path=path,
    )
    assert resolved["current"]["status"] == "active"
    # The legacy resolve archives the compatibility row.
    _curated_archive(path, "question-name", 1)

    row = get_current_record(
        "question-name", owner="alice", workspace_id="global",
        require_legacy_presence=True, db_path=path,
    )
    assert row is not None
    assert row["text"] == "E"
    assert row["kind"] == "open_question"
    assert row["status"] == "active"

    visible = list_current_records(
        owner="alice", workspace_id="global", require_legacy_presence=True, db_path=path,
    )
    visible_ids = {str(item["id"]) for item in visible}
    assert "question-name" in visible_ids
    assert "question-stale" not in visible_ids

    hits = search_current_records(
        "open question", owner="alice", workspace_id="global",
        require_legacy_presence=True, db_path=path,
    )
    assert any(str(hit["id"]) == "question-name" for hit in hits)


def test_reopen_after_resolve_requires_live_legacy_row_again(tmp_path):
    """S02: reopen restores the open-question contract — visible only while
    its compatibility row is live (the provider reopens both sides)."""
    from src.frankenmemory_v2 import get_current_record

    path = str(tmp_path / "fm.db")
    _question_block(path, "name", "question-name")
    _curated(path)
    _curated_add(path, "question-name")
    transition_knowledge(
        "question-name", "resolve", owner="alice", expected_revision=1,
        value="E", db_path=path,
    )
    _curated_archive(path, "question-name", 1)

    reopened = transition_knowledge(
        "block-name", "reopen", owner="alice", expected_revision=2, db_path=path,
    )
    assert reopened["current"]["status"] == "open"
    # Legacy side not yet reopened: the question is hidden again, not split.
    assert get_current_record(
        "question-name", owner="alice", workspace_id="global",
        require_legacy_presence=True, db_path=path,
    ) is None
    # Provider reopen also revives the compatibility row; then it shows.
    _curated_archive(path, "question-name", 0)
    row = get_current_record(
        "question-name", owner="alice", workspace_id="global",
        require_legacy_presence=True, db_path=path,
    )
    assert row is not None
    assert row["status"] == "open"


def test_resolve_compensation_keeps_question_visible_when_legacy_never_archived(tmp_path):
    """S02: when the legacy resolve fails, the v2 reopen compensation must
    restore visibility — the compatibility row was never archived."""
    from src.frankenmemory_v2 import get_current_record

    path = str(tmp_path / "fm.db")
    _question_block(path, "name", "question-name")
    _curated(path)
    _curated_add(path, "question-name")
    transition_knowledge(
        "question-name", "resolve", owner="alice", expected_revision=1,
        value="E", db_path=path,
    )
    # Legacy resolve failed → row stayed live → compensation reopen.
    transition_knowledge(
        "block-name", "reopen", owner="alice", expected_revision=2,
        actor_type="system", actor_id="resolve-compensation",
        reason="legacy resolve failed", db_path=path,
    )
    row = get_current_record(
        "question-name", owner="alice", workspace_id="global",
        require_legacy_presence=True, db_path=path,
    )
    assert row is not None
    assert row["status"] == "open"


def test_get_current_record_resolves_block_and_memory_identities(tmp_path):
    """S04: the keyed SQL prefilter must not change which identities resolve."""
    from src.frankenmemory_v2 import get_current_record

    path = str(tmp_path / "fm.db")
    _question_block(path, "name", "question-name")
    _curated(path)
    _curated_add(path, "question-name")
    repository = V2Repository(path)
    repository.create_knowledge(
        owner="alice",
        workspace_id="global",
        block_id="block-orphan",
        subject_entity_id="entity-user",
        predicate="note",
        claim_slot="orphan-note",
        payload=_payload({"type": "string", "value": "no legacy id"}),
    )

    by_memory = get_current_record(
        "question-name", owner="alice", workspace_id="global",
        require_legacy_presence=True, db_path=path,
    )
    assert by_memory is not None
    assert by_memory["id"] == "question-name"
    # Block-id addressing still works when the block has no memory identity.
    by_block = get_current_record(
        "block-orphan", owner="alice", workspace_id="global", db_path=path
    )
    assert by_block is not None
    assert by_block["id"] == "block-orphan"
    # ... but never rewrites a memory-identified block's wire id.
    assert get_current_record(
        "block-name", owner="alice", workspace_id="global",
        require_legacy_presence=True, db_path=path,
    ) is None
    assert get_current_record(
        "missing", owner="alice", workspace_id="global", db_path=path
    ) is None


def test_details_batch_matches_per_block_detail(tmp_path):
    """S04 characterization: the set-based reader assembles byte-identical
    detail dicts to the per-block reader it replaced."""
    from src.memory_versioned import KnowledgeScope, _detail, _details

    path = str(tmp_path / "fm.db")
    repository = _repository(path)
    with sqlite3.connect(path) as conn:
        conn.execute(
            "INSERT INTO fm_v2_sources(owner_id,source_id,workspace_key,project_key,"
            "source_uri,source_revision,content_hash,source_type,forget_state,created_at)"
            " VALUES ('alice','src-1','global','','message://alice/s/m1',1,"
            "'hash-1','conversation','active','2026-01-01T00:00:00Z')"
        )
        conn.execute(
            "INSERT INTO fm_v2_evidence(owner_id,evidence_id,source_id,source_revision,"
            "locator_type,locator_json,quote,extraction_contract,parser_version,"
            "raw_value_json,normalized_value_json,created_at)"
            " VALUES ('alice','ev-1','src-1',1,'message','{}','quoted text','test','1',"
            "'{}','{}','2026-01-01T00:00:00Z')"
        )
    payload = _payload({"type": "string", "value": "blue"})
    payload["evidence_ids"] = ["ev-1"]
    repository.create_knowledge(
        owner="alice",
        workspace_id="global",
        block_id="block-color",
        subject_entity_id="entity-user",
        predicate="favorite_color",
        claim_slot="favorite-color",
        payload=payload,
        source_event_id="memory:memory-color",
    )
    repository.create_knowledge(
        owner="alice",
        workspace_id="global",
        block_id="block-tea",
        subject_entity_id="entity-user",
        predicate="likes",
        claim_slot="favorite-drink",
        payload=_payload({"type": "string", "value": "tea"}),
    )
    # A second revision and a recorded conflict exercise those assembly paths.
    transition_knowledge(
        "memory-color", "edit", owner="alice", expected_revision=1,
        value="green", db_path=path,
    )
    with pytest.raises(VersionedMemoryConflict):
        transition_knowledge(
            "block-color", "correct", owner="alice", expected_revision=1,
            value="purple", db_path=path,
        )

    scope = KnowledgeScope("alice", "global", "")
    block_ids = ["block-color", "block-tea"]
    assert _details(repository, scope, block_ids) == [
        _detail(repository, scope, block_id) for block_id in block_ids
    ]


def test_list_knowledge_connection_count_stays_constant(tmp_path, monkeypatch):
    """S04: listing N blocks must not pay per-block connection setup."""
    path = str(tmp_path / "fm.db")
    repository = _repository(path)
    for suffix in ("one", "two", "three"):
        repository.create_knowledge(
            owner="alice",
            workspace_id="global",
            block_id=f"block-{suffix}",
            subject_entity_id="entity-user",
            predicate="note",
            claim_slot=f"slot-{suffix}",
            payload=_payload({"type": "string", "value": suffix}),
        )

    real_connect = sqlite3.connect
    calls = []

    def counting_connect(*args, **kwargs):
        calls.append(args[0] if args else kwargs.get("database"))
        return real_connect(*args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", counting_connect)
    details = list_knowledge(owner="alice", db_path=path)
    assert len(details) == 3
    # One connection for the block-id page, one for the set-based detail
    # assembly; the per-block reader paid five connections per block.
    assert len(calls) <= 3
