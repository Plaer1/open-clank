from concurrent.futures import ThreadPoolExecutor
import sqlite3
import threading

import pytest

import services.memory.principal_context as principal_context
from services.memory.principal_context import (
    MAX_ASSISTANT_LABEL_LENGTH,
    build_assistant_entity_match_candidate,
    ensure_principal_context,
    render_identity_template,
)
from src.frankenmemory_v2 import V2Repository


def _db(path):
    conn = sqlite3.connect(path)
    conn.executescript("""
    CREATE TABLE fm_v2_change_sets(owner_id TEXT, change_set_id TEXT, actor_type TEXT, actor_id TEXT, reason TEXT, source_event_id TEXT, contract_version TEXT, content_hash TEXT, created_at TEXT, PRIMARY KEY(owner_id, change_set_id));
    CREATE TABLE fm_v2_entities(owner_id TEXT, entity_id TEXT, workspace_key TEXT, project_key TEXT, created_change_set_id TEXT, current_revision INTEGER, created_at TEXT, PRIMARY KEY(owner_id, entity_id));
    CREATE TABLE fm_v2_entity_revisions(owner_id TEXT, entity_id TEXT, revision INTEGER, payload TEXT, content_hash TEXT, actor_type TEXT, actor_id TEXT, reason TEXT, change_set_id TEXT, previous_revision INTEGER, created_at TEXT, PRIMARY KEY(owner_id, entity_id, revision));
    CREATE TABLE fm_v2_entity_roles(owner_id TEXT, entity_id TEXT, revision INTEGER, workspace_key TEXT, project_key TEXT, role TEXT, is_current INTEGER, PRIMARY KEY(owner_id, entity_id, revision, role));
    CREATE TABLE fm_v2_entity_aliases(owner_id TEXT, entity_id TEXT, revision INTEGER, alias TEXT, PRIMARY KEY(owner_id, entity_id, revision, alias));
    CREATE TABLE fm_v2_entity_tags(owner_id TEXT, entity_id TEXT, revision INTEGER, tag TEXT, PRIMARY KEY(owner_id, entity_id, revision, tag));
    CREATE TABLE fm_v2_outbox(owner_id TEXT, event_id TEXT, change_set_id TEXT, kind TEXT, payload_json TEXT, source_revision INTEGER, created_at TEXT, PRIMARY KEY(owner_id, event_id));
    CREATE TABLE fm_v2_knowledge_blocks(owner_id TEXT, block_id TEXT, workspace_key TEXT, project_key TEXT, subject_entity_id TEXT, predicate TEXT, claim_slot TEXT, cardinality TEXT, created_change_set_id TEXT, current_revision INTEGER, created_at TEXT);
    CREATE TABLE fm_v2_knowledge_revisions(owner_id TEXT, block_id TEXT, revision INTEGER, value_type TEXT, value_json TEXT, expected_value_type TEXT, kind TEXT, tags_json TEXT, status TEXT, confidence REAL, trust REAL, activation TEXT, activation_rationale TEXT, valid_from TEXT, valid_to TEXT, evidence_json TEXT, content_hash TEXT, actor_type TEXT, actor_id TEXT, reason TEXT, source_event_id TEXT, change_set_id TEXT, previous_revision INTEGER, created_at TEXT);
    CREATE TABLE fm_v2_sources(owner_id TEXT, source_id TEXT, source_revision INTEGER, workspace_key TEXT, project_key TEXT, source_uri TEXT, content_hash TEXT, source_type TEXT, created_at TEXT);
    CREATE TABLE fm_v2_evidence(owner_id TEXT, evidence_id TEXT, source_id TEXT, source_revision INTEGER, locator_type TEXT, locator_json TEXT, quote TEXT, extraction_contract TEXT, parser_version TEXT, raw_value_json TEXT, normalized_value_json TEXT, created_at TEXT);
    CREATE TABLE curated(id TEXT PRIMARY KEY, content TEXT NOT NULL, kind TEXT NOT NULL, owner TEXT, workspace_id TEXT NOT NULL DEFAULT 'global', archived INTEGER NOT NULL DEFAULT 0, metadata TEXT NOT NULL DEFAULT 'null', created_at TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL DEFAULT '');
    """)
    conn.commit(); conn.close()


def test_principal_context_is_idempotent_and_scoped(tmp_path):
    db = tmp_path / "fm.sqlite"
    _db(str(db))
    first = ensure_principal_context(owner="allie", workspace_id="ws", db_path=str(db))
    second = ensure_principal_context(owner="allie", workspace_id="ws", db_path=str(db))
    other = ensure_principal_context(owner="e", workspace_id="ws", db_path=str(db))
    assert first["assistant_entity_id"] == second["assistant_entity_id"]
    assert first["handler_entity_id"] == second["handler_entity_id"]
    assert first["revision"] == second["revision"] == 1
    assert first["assistant_entity_id"] != other["assistant_entity_id"]
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT count(*) FROM fm_v2_principal_bindings").fetchone()[0] == 2
        roles = {row[0] for row in conn.execute("SELECT role FROM fm_v2_entity_roles")}
    assert "handler" in roles
    assert any(role.startswith("assistant_self:") for role in roles)


def test_principal_cache_invalidation_requires_exact_owner():
    original = set(principal_context._ENSURED_SCOPES)
    try:
        principal_context._ENSURED_SCOPES.clear()
        principal_context._ENSURED_SCOPES.update(
            {
                ("db", "allie", "global", "", "default", "Ada", "allie", "Handler"),
                ("db", "e", "global", "", "default", "Astryx", "e", "Handler"),
            }
        )
        snapshot = set(principal_context._ENSURED_SCOPES)
        for owner in (None, "", "   "):
            with pytest.raises(ValueError, match="requires an owner"):
                principal_context.invalidate_principal_cache(owner)
            assert principal_context._ENSURED_SCOPES == snapshot

        principal_context.invalidate_principal_cache("allie")
        assert {key[1] for key in principal_context._ENSURED_SCOPES} == {"e"}
    finally:
        principal_context._ENSURED_SCOPES.clear()
        principal_context._ENSURED_SCOPES.update(original)


def test_erasure_restart_reseeds_stable_self_and_neutral_handler(tmp_path):
    db = tmp_path / "fm.sqlite"
    _db(str(db))
    first = principal_context.ensure_principal_context_cached(
        owner="allie",
        workspace_id="global",
        assistant_label="Ada",
        handler_label="Allie",
        db_path=str(db),
    )
    with sqlite3.connect(db) as conn:
        for table in (
            "fm_v2_principal_bindings",
            "fm_v2_entity_roles",
            "fm_v2_entity_aliases",
            "fm_v2_entity_tags",
            "fm_v2_entity_revisions",
            "fm_v2_entities",
            "fm_v2_outbox",
            "fm_v2_change_sets",
            "curated",
        ):
            conn.execute(f"DELETE FROM {table} WHERE owner_id=?" if table != "curated" else f"DELETE FROM {table} WHERE owner=?", ("allie",))
        conn.commit()

    principal_context.invalidate_principal_cache("allie")
    reseeded = principal_context.ensure_principal_context_cached(
        owner="allie",
        workspace_id="global",
        assistant_label="Ada",
        db_path=str(db),
    )
    # Simulate a process restart, not merely another hot-cache lookup.
    principal_context._ENSURED_SCOPES.clear()
    restarted = principal_context.ensure_principal_context_cached(
        owner="allie",
        workspace_id="global",
        assistant_label="Ada",
        db_path=str(db),
    )

    assert reseeded["assistant_entity_id"] == first["assistant_entity_id"]
    assert reseeded["handler_entity_id"] == first["handler_entity_id"]
    assert restarted == reseeded
    repo = V2Repository(str(db))
    assistant = repo.get_entity(
        owner="allie",
        entity_id=reseeded["assistant_entity_id"],
        workspace_id="global",
    )
    handler = repo.get_entity(
        owner="allie",
        entity_id=reseeded["handler_entity_id"],
        workspace_id="global",
    )
    assert assistant["canonical_label"] == "Ada"
    assert handler["canonical_label"] == "Handler"


def test_assistant_persona_rename_revises_once_without_rebinding(tmp_path):
    db = tmp_path / "fm.sqlite"
    _db(str(db))
    first = ensure_principal_context(
        owner="allie",
        workspace_id="ws",
        assistant_label="Ada",
        db_path=str(db),
    )
    repo = V2Repository(str(db))
    original = repo.get_entity(
        owner="allie",
        entity_id=first["assistant_entity_id"],
        workspace_id="ws",
    )
    assert original["canonical_label"] == "Ada"
    assert set(("me", "I", "myself", "Ada")).issubset(original["aliases"])

    with_extra_alias = repo.update_entity(
        owner="allie",
        entity_id=first["assistant_entity_id"],
        expected_revision=original["revision"],
        payload={"aliases": [*original["aliases"], "the helpful one"]},
        workspace_id="ws",
        reason="test unrelated reviewed alias",
    )
    renamed = ensure_principal_context(
        owner="allie",
        workspace_id="ws",
        assistant_label="Astryx",
        db_path=str(db),
    )
    repeated = ensure_principal_context(
        owner="allie",
        workspace_id="ws",
        assistant_label="Astryx",
        db_path=str(db),
    )
    current = repo.get_entity(
        owner="allie",
        entity_id=first["assistant_entity_id"],
        workspace_id="ws",
    )
    handler = repo.get_entity(
        owner="allie",
        entity_id=first["handler_entity_id"],
        workspace_id="ws",
    )

    assert current["revision"] == with_extra_alias["revision"] + 1
    assert current["canonical_label"] == "Astryx"
    assert set(("me", "I", "myself", "Ada", "Astryx", "the helpful one")).issubset(
        current["aliases"]
    )
    assert current["reason"] == "sync reviewed default persona name"
    assert first["assistant_entity_id"] == renamed["assistant_entity_id"] == repeated["assistant_entity_id"]
    assert first["handler_entity_id"] == renamed["handler_entity_id"] == repeated["handler_entity_id"]
    assert first["revision"] == renamed["revision"] == repeated["revision"] == 1
    assert handler["canonical_label"] == "Handler"
    assert "Astryx" not in handler["aliases"]
    with sqlite3.connect(db) as conn:
        assert conn.execute(
            "SELECT count(*) FROM fm_v2_entity_revisions WHERE owner_id=? AND entity_id=?",
            ("allie", first["assistant_entity_id"]),
        ).fetchone()[0] == 3


def test_concurrent_persona_rename_has_one_winning_revision(monkeypatch, tmp_path):
    db = tmp_path / "fm.sqlite"
    _db(str(db))
    initial = ensure_principal_context(
        owner="allie",
        workspace_id="ws",
        assistant_label="Ada",
        db_path=str(db),
    )
    original_update = V2Repository.update_entity
    update_barrier = threading.Barrier(20)

    def synchronized_update(repository, **kwargs):
        if kwargs.get("actor_id") == "memory-persona-sync":
            update_barrier.wait(timeout=10)
        return original_update(repository, **kwargs)

    monkeypatch.setattr(V2Repository, "update_entity", synchronized_update)

    def rename(_):
        return ensure_principal_context(
            owner="allie",
            workspace_id="ws",
            assistant_label="Astryx",
            db_path=str(db),
        )

    with ThreadPoolExecutor(max_workers=20) as executor:
        results = list(executor.map(rename, range(20)))

    assert all(result == results[0] for result in results)
    assert results[0]["assistant_entity_id"] == initial["assistant_entity_id"]
    assert results[0]["handler_entity_id"] == initial["handler_entity_id"]
    assert results[0]["revision"] == initial["revision"] == 1
    with sqlite3.connect(db) as conn:
        revisions = conn.execute(
            "SELECT count(*) FROM fm_v2_entity_revisions WHERE owner_id=? AND entity_id=?",
            ("allie", initial["assistant_entity_id"]),
        ).fetchone()[0]
        current_revision = conn.execute(
            "SELECT current_revision FROM fm_v2_entities WHERE owner_id=? AND entity_id=?",
            ("allie", initial["assistant_entity_id"]),
        ).fetchone()[0]
    assert revisions == current_revision == 2


def test_default_persona_names_are_owner_scoped(monkeypatch, tmp_path):
    db = tmp_path / "fm.sqlite"
    _db(str(db))
    names = {"allie": "Ada", "e": "Astryx"}
    seen = []

    def fake_default_persona(owner):
        seen.append(owner)
        return {"name": names[owner]}

    monkeypatch.setattr("src.default_persona.get_default_persona", fake_default_persona)
    allie = ensure_principal_context(owner="allie", workspace_id="ws", db_path=str(db))
    e = ensure_principal_context(owner="e", workspace_id="ws", db_path=str(db))
    repo = V2Repository(str(db))
    allie_self = repo.get_entity(
        owner="allie", entity_id=allie["assistant_entity_id"], workspace_id="ws"
    )
    e_self = repo.get_entity(owner="e", entity_id=e["assistant_entity_id"], workspace_id="ws")

    assert seen == ["allie", "e"]
    assert allie_self["canonical_label"] == "Ada"
    assert e_self["canonical_label"] == "Astryx"
    assert "Ada" in allie_self["aliases"] and "Ada" not in e_self["aliases"]
    assert "Astryx" in e_self["aliases"] and "Astryx" not in allie_self["aliases"]


def test_unavailable_default_persona_falls_back(monkeypatch, tmp_path):
    db = tmp_path / "fm.sqlite"
    _db(str(db))

    def unavailable(_owner):
        raise OSError("persona store unavailable")

    monkeypatch.setattr("src.default_persona.get_default_persona", unavailable)
    context = ensure_principal_context(owner="allie", db_path=str(db))
    entity = V2Repository(str(db)).get_entity(
        owner="allie",
        entity_id=context["assistant_entity_id"],
        workspace_id="global",
    )

    assert entity["canonical_label"] == "Open Clank"
    assert "Open Clank" in entity["aliases"]


def test_explicit_assistant_label_is_sanitized_and_bounded(tmp_path):
    db = tmp_path / "fm.sqlite"
    _db(str(db))
    context = ensure_principal_context(
        owner="allie",
        assistant_label="  Ada\x00\n\u202e  " + ("x" * 200),
        db_path=str(db),
    )
    entity = V2Repository(str(db)).get_entity(
        owner="allie",
        entity_id=context["assistant_entity_id"],
        workspace_id="global",
    )

    assert entity["canonical_label"].startswith("Ada ")
    assert len(entity["canonical_label"]) == MAX_ASSISTANT_LABEL_LENGTH
    assert "\x00" not in entity["canonical_label"]
    assert "\n" not in entity["canonical_label"]
    assert "\u202e" not in entity["canonical_label"]
    assert entity["canonical_label"] in entity["aliases"]


def test_identity_tokens_are_explicit_only():
    assert render_identity_template("%USER% prefers tea; the user interface stays literal", handler_label="Allie") == "Allie prefers tea; the user interface stays literal"
    assert render_identity_template("%SELF{poss_det} project", assistant_subject="me") == "my project"


def test_persona_alias_builds_review_only_assistant_match_candidate():
    entity_id = "principal_assistant_" + ("a" * 32)
    candidate = build_assistant_entity_match_candidate(
        source_text="Yesterday,  ＡＳＴＲＹＸ\n ＰＲＩＭＥ built a turn-taking lock.",
        matched_alias="Astryx Prime",
        assistant_label="Astryx   Prime",
        assistant_entity_id=entity_id,
    )

    assert candidate == {
        "contract": "openclank.memory-entity-match-candidate/v1",
        "entity_id": entity_id,
        "role": "assistant_self",
        "matched_alias": "Astryx Prime",
        "match_method": "persona_setting_exact",
        "state": "proposed",
        "requires_review": True,
    }


@pytest.mark.parametrize(
    ("source_text", "matched_alias", "assistant_label", "entity_id"),
    [
        ("Adaline wrote this.", "Ada", "Ada", "principal_assistant_" + ("a" * 32)),
        ("I wrote this.", "I", "I", "principal_assistant_" + ("a" * 32)),
        ("me wrote this.", "me", "me", "principal_assistant_" + ("a" * 32)),
        ("myself wrote this.", "myself", "myself", "principal_assistant_" + ("a" * 32)),
        ("Ada wrote this.", "Ada", "Astryx", "principal_assistant_" + ("a" * 32)),
        ("Ada wrote this.", "Ada", "Ada", "model_supplied_entity"),
        ("Open Clank wrote this.", "\x00\u202e", "\x00\u202e", "principal_assistant_" + ("a" * 32)),
    ],
)
def test_persona_alias_candidate_rejects_ambiguous_or_untrusted_matches(
    source_text, matched_alias, assistant_label, entity_id
):
    assert build_assistant_entity_match_candidate(
        source_text=source_text,
        matched_alias=matched_alias,
        assistant_label=assistant_label,
        assistant_entity_id=entity_id,
    ) is None


def test_ensure_fails_open_when_v2_bootstrap_raises_operation_error(
    monkeypatch, tmp_path
):
    from src.frankenmemory_v2 import V2OperationError

    db = tmp_path / "fm.sqlite"
    _db(str(db))

    def conflicting_create(repository, **kwargs):
        raise V2OperationError("conflict", "simulated concurrent bootstrap loss")

    monkeypatch.setattr(V2Repository, "create_entity", conflicting_create)
    # A repository error must fail open (None) instead of propagating into a
    # 500 on the caller's route.
    assert (
        ensure_principal_context(owner="allie", workspace_id="ws", db_path=str(db))
        is None
    )


def test_ensure_closes_its_sqlite_connections(monkeypatch, tmp_path):
    db = tmp_path / "fm.sqlite"
    _db(str(db))
    connections = []

    class TrackedConnection(sqlite3.Connection):
        closed = False

        def close(self):
            self.closed = True
            super().close()

    real_connect = sqlite3.connect

    def tracking_connect(*args, **kwargs):
        kwargs.setdefault("factory", TrackedConnection)
        connection = real_connect(*args, **kwargs)
        connections.append(connection)
        return connection

    monkeypatch.setattr(sqlite3, "connect", tracking_connect)
    context = ensure_principal_context(owner="allie", workspace_id="ws", db_path=str(db))

    assert context is not None
    assert connections, "expected the bootstrap to open sqlite connections"
    assert all(connection.closed for connection in connections)


def test_handler_display_label_defaults_and_follows_rename(tmp_path):
    from services.memory.principal_context import resolve_handler_display_label

    db = tmp_path / "fm.sqlite"
    _db(str(db))
    # No bootstrap yet: the neutral fallback, never an invented identity.
    assert resolve_handler_display_label("allie", db_path=str(db)) == "Handler"

    context = ensure_principal_context(owner="allie", workspace_id="ws", db_path=str(db))
    assert resolve_handler_display_label("allie", workspace_id="ws", db_path=str(db)) == "Handler"

    repo = V2Repository(str(db))
    handler = repo.get_entity(
        owner="allie", entity_id=context["handler_entity_id"], workspace_id="ws",
    )
    repo.update_entity(
        owner="allie",
        entity_id=context["handler_entity_id"],
        expected_revision=handler["revision"],
        payload={"canonical_label": "Allie"},
        workspace_id="ws",
        reason="handler rename",
    )
    # A rename changes the rendered label without touching stored claims.
    assert resolve_handler_display_label("allie", workspace_id="ws", db_path=str(db)) == "Allie"


def test_handler_display_label_is_failure_safe(tmp_path):
    from services.memory.principal_context import resolve_handler_display_label

    db = tmp_path / "fm.sqlite"
    _db(str(db))
    assert resolve_handler_display_label(None, db_path=str(db)) == "Handler"
    assert resolve_handler_display_label("", db_path=str(db)) == "Handler"
    assert resolve_handler_display_label("allie", db_path=str(tmp_path / "missing.db")) == "Handler"


def test_reviewed_handler_profile_name_syncs_stable_principal(tmp_path):
    from services.memory.principal_context import (
        ensure_principal_context_cached,
        invalidate_principal_cache,
        resolve_handler_display_label,
    )

    db = tmp_path / "fm.sqlite"
    _db(str(db))
    initial = ensure_principal_context(
        owner="allie", workspace_id="global", db_path=str(db)
    )
    metadata = {
        "category": "identity",
        "source_document_role": "handler_profile",
        "import_review": {"reviewed_by": "allie"},
        "subject_attribution": {
            "contract": "openclank.memory-subject-attribution/v1",
            "role": "handler",
            "entity_id": initial["handler_entity_id"],
        },
    }
    with sqlite3.connect(db) as conn:
        conn.execute(
            "INSERT INTO curated(id,content,kind,owner,workspace_id,metadata,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (
                "name-memory",
                "%USER%'s name is Allie.",
                "persona",
                "allie",
                "global",
                __import__("json").dumps(metadata),
                "2026-08-25T00:00:00Z",
                "2026-08-25T00:00:00Z",
            ),
        )
        conn.commit()

    assert resolve_handler_display_label("allie", db_path=str(db)) == "Allie"
    invalidate_principal_cache("allie")
    synced = ensure_principal_context_cached(
        owner="allie", workspace_id="global", db_path=str(db)
    )
    handler = V2Repository(str(db)).get_entity(
        owner="allie",
        entity_id=synced["handler_entity_id"],
        workspace_id="global",
    )

    assert synced["handler_entity_id"] == initial["handler_entity_id"]
    assert handler["canonical_label"] == "Allie"
    assert {"handler", "Handler", "Allie"}.issubset(set(handler["aliases"]))
    assert handler["reason"] == "sync owner-reviewed Handler preferred name"


@pytest.mark.parametrize(
    "mutation",
    [
        {"reviewed_by": "mallory"},
        {"role": "assistant_self"},
        {"entity_id": "principal_handler_" + ("f" * 32)},
        {"category": "fact"},
    ],
)
def test_untrusted_or_misattributed_name_does_not_rename_handler(tmp_path, mutation):
    from services.memory.principal_context import resolve_handler_display_label

    db = tmp_path / "fm.sqlite"
    _db(str(db))
    context = ensure_principal_context(owner="allie", db_path=str(db))
    metadata = {
        "category": "identity",
        "source_document_role": "handler_profile",
        "import_review": {"reviewed_by": "allie"},
        "subject_attribution": {
            "contract": "openclank.memory-subject-attribution/v1",
            "role": "handler",
            "entity_id": context["handler_entity_id"],
        },
    }
    if "reviewed_by" in mutation:
        metadata["import_review"]["reviewed_by"] = mutation["reviewed_by"]
    elif "category" in mutation:
        metadata["category"] = mutation["category"]
    else:
        metadata["subject_attribution"].update(mutation)
    with sqlite3.connect(db) as conn:
        conn.execute(
            "INSERT INTO curated(id,content,kind,owner,workspace_id,metadata,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (
                "bad-name",
                "%USER%'s name is Mallory.",
                "persona",
                "allie",
                "global",
                __import__("json").dumps(metadata),
                "2026-08-25T00:00:00Z",
                "2026-08-25T00:00:00Z",
            ),
        )
        conn.commit()

    assert resolve_handler_display_label("allie", db_path=str(db)) == "Handler"
