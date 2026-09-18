from __future__ import annotations

import asyncio
import json
import io
import sqlite3
from types import SimpleNamespace
from io import BytesIO

import pytest
from fastapi import UploadFile
from PIL import Image

import routes.memory_routes as memory_routes
from services.memory.import_batch import (
    ImportBatchError,
    MemoryImportBatchStore,
    item_identity,
)
from services.memory.media_assets import MediaAssetError, MemoryMediaStore, admit_photo


def _db(path):
    with sqlite3.connect(path) as conn:
        conn.executescript(
            """
            CREATE TABLE fm_v2_jobs (
                owner_id TEXT NOT NULL,
                job_id TEXT NOT NULL,
                kind TEXT NOT NULL,
                workspace_key TEXT NOT NULL DEFAULT '',
                project_key TEXT NOT NULL DEFAULT '',
                idempotency_key TEXT NOT NULL,
                state TEXT NOT NULL,
                current_attempt_id TEXT,
                attempt_count INTEGER NOT NULL DEFAULT 0,
                input_hash TEXT NOT NULL,
                result_json TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY(owner_id, job_id),
                UNIQUE(owner_id, idempotency_key)
            );
            CREATE TABLE fm_v2_attempts (
                owner_id TEXT NOT NULL,
                attempt_id TEXT NOT NULL,
                job_id TEXT NOT NULL,
                attempt_number INTEGER NOT NULL,
                state TEXT NOT NULL,
                lease_epoch INTEGER NOT NULL,
                lease_owner TEXT,
                lease_expires_at TEXT,
                heartbeat_at TEXT,
                progress_watermark TEXT,
                error_json TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY(owner_id, attempt_id)
            );
            CREATE TRIGGER fm_v2_job_state_validate
            BEFORE UPDATE OF state ON fm_v2_jobs
            WHEN NOT (
                OLD.state=NEW.state OR
                (OLD.state='queued' AND NEW.state IN ('active','cancelled')) OR
                (OLD.state='active' AND NEW.state IN (
                    'awaiting_review','retry_wait','succeeded','failed_terminal','cancelled'
                )) OR
                (OLD.state='retry_wait' AND NEW.state IN ('queued','cancelled')) OR
                (OLD.state='awaiting_review' AND NEW.state IN ('queued','succeeded','cancelled'))
            )
            BEGIN
                SELECT RAISE(ABORT, 'invalid job state transition');
            END;
            """
        )


def _items(*contents: bytes):
    return [
        {
            "item_id": item_identity(content),
            "filename": f"note-{index}.md",
            "byte_size": len(content),
            "content_hash": __import__("hashlib").sha256(content).hexdigest(),
            "extension": ".md",
        }
        for index, content in enumerate(contents)
    ]


def test_owner_staging_preview_and_purge_are_scoped_and_cas_bound(tmp_path):
    db = tmp_path / "fm.db"
    _db(db)
    store = MemoryImportBatchStore(str(db), str(tmp_path))
    alice_batch = "batch_" + ("a" * 32)
    bob_batch = "batch_" + ("b" * 32)
    alice_item = "item_" + ("c" * 32)
    bob_item = "item_" + ("d" * 32)
    store.stage_bytes("alice", alice_batch, alice_item, b"alice source")
    store.stage_bytes("bob", bob_batch, bob_item, b"bob source")

    preview = store.preview_owner_staging("alice")
    assert preview["count"] == 1
    assert preview["bytes"] == len(b"alice source")
    assert preview["fingerprint"].startswith("sha256:")

    stale = dict(preview)
    stale["fingerprint"] = "sha256:stale"
    with pytest.raises(ImportBatchError, match="stale"):
        store.purge_owner_staging("alice", expected=stale)
    assert store.read_staged("alice", alice_batch, alice_item) == b"alice source"

    result = store.purge_owner_staging("alice", expected=preview)
    assert result == {"complete": True, "count": 1}
    assert store.preview_owner_staging("alice")["count"] == 0
    assert store.read_staged("bob", bob_batch, bob_item) == b"bob source"

    # Idempotent replay after another nuke component needed a retry.
    assert store.purge_owner_staging("alice", expected=preview) == {
        "complete": True,
        "count": 0,
    }


def test_batch_parent_children_are_idempotent_and_filename_independent(tmp_path, monkeypatch):
    db = tmp_path / "fm.db"
    _db(db)
    monkeypatch.setattr("services.memory.import_batch.seal_job_manifest", lambda **_: {})
    store = MemoryImportBatchStore(str(db), str(tmp_path))
    first = store.create(owner="alice", workspace_id="global", session_id=None, items=_items(b"one", b"two"))
    replay = store.create(owner="alice", workspace_id="global", session_id=None, items=_items(b"one", b"two"))

    assert first["batch_id"].startswith("batch_")
    assert replay["replayed"] is True
    assert len(first["items"]) == 2
    assert all(item["item_id"].startswith("item_") for item in first["items"])
    assert first["items"][0]["item_id"] != first["items"][1]["item_id"]

    updated = store.update_item(
        owner="alice",
        batch_id=first["batch_id"],
        item_id=first["items"][0]["item_id"],
        state="awaiting_review",
        result={"outcome": "suggestions", "suggestions": [{"text": "one"}]},
    )
    assert updated["state"] == "active"
    updated = store.update_item(
        owner="alice",
        batch_id=first["batch_id"],
        item_id=first["items"][1]["item_id"],
        state="failed_terminal",
        error={"code": "unsupported_file"},
    )
    assert updated["state"] == "awaiting_review"
    status = store.get(owner="alice", batch_id=first["batch_id"])
    assert status["counts"] == {"selected": 2, "completed": 1, "reused": 0, "failed": 1, "pending": 0}
    suggestion_item = next(item for item in status["items"] if item.get("result", {}).get("suggestions"))
    assert suggestion_item["result"]["suggestions"][0]["text"] == "one"


def test_persona_identity_snapshot_invalidates_same_bytes_batch_replay(tmp_path, monkeypatch):
    db = tmp_path / "fm.db"
    _db(db)
    monkeypatch.setattr("services.memory.import_batch.seal_job_manifest", lambda **_: {})
    store = MemoryImportBatchStore(str(db), str(tmp_path))
    nyx_context = "1" * 64
    nova_context = "2" * 64

    first = store.create(
        owner="alice",
        workspace_id="global",
        session_id=None,
        items=_items(b"Nyx keeps the violet notebook."),
        identity_context_hash=nyx_context,
    )
    after_rename = store.create(
        owner="alice",
        workspace_id="global",
        session_id=None,
        items=_items(b"Nyx keeps the violet notebook."),
        identity_context_hash=nova_context,
    )
    replay = store.create(
        owner="alice",
        workspace_id="global",
        session_id=None,
        items=_items(b"Nyx keeps the violet notebook."),
        identity_context_hash=nova_context,
    )

    assert first["batch_id"] != after_rename["batch_id"]
    assert first["identity_context_hash"] == nyx_context
    assert after_rename["identity_context_hash"] == nova_context
    assert after_rename.get("replayed") is not True
    assert replay["batch_id"] == after_rename["batch_id"]
    assert replay["replayed"] is True


def test_source_document_role_is_part_of_batch_semantic_identity(tmp_path, monkeypatch):
    db = tmp_path / "fm.db"
    _db(db)
    monkeypatch.setattr("services.memory.import_batch.seal_job_manifest", lambda **_: {})
    store = MemoryImportBatchStore(str(db), str(tmp_path))
    base = _items(b"same bytes")[0]
    handler = dict(base, filename="USER.md", document_role="handler_profile")
    generic = dict(base, filename="notes.md", document_role="generic_document")
    renamed_handler = dict(base, filename="HUMAN-PROFILE.md", document_role="handler_profile")

    first = store.create(
        owner="alice", workspace_id="global", session_id=None, items=[handler]
    )
    different_role = store.create(
        owner="alice", workspace_id="global", session_id=None, items=[generic]
    )
    same_role = store.create(
        owner="alice", workspace_id="global", session_id=None, items=[renamed_handler]
    )

    assert first["batch_id"] != different_role["batch_id"]
    assert same_role["batch_id"] == first["batch_id"]
    assert same_role["replayed"] is True


def test_batch_review_preserves_extraction_and_records_decisions(tmp_path, monkeypatch):
    db = tmp_path / "fm.db"
    _db(db)
    monkeypatch.setattr("services.memory.import_batch.seal_job_manifest", lambda **_: {})
    store = MemoryImportBatchStore(str(db), str(tmp_path))
    created = store.create(owner="alice", workspace_id="global", session_id=None, items=_items(b"profile"))
    item_id = created["items"][0]["item_id"]
    store.update_item(
        owner="alice",
        batch_id=created["batch_id"],
        item_id=item_id,
        state="awaiting_review",
        result={
            "outcome": "suggestions",
            "suggestions": [
                {"text": "Allie drinks tea.", "category": "fact"},
                {"text": "Discard this transient status.", "category": "fact"},
            ],
        },
    )
    initial = store.get(owner="alice", batch_id=created["batch_id"])
    suggestions = initial["items"][0]["result"]["suggestions"]
    accept_id, reject_id = [suggestion["suggestion_id"] for suggestion in suggestions]
    assert accept_id != reject_id
    assert all(suggestion["proposal_fingerprint"] for suggestion in suggestions)

    prepared = store.prepare_review_decision(
        owner="alice",
        batch_id=created["batch_id"],
        suggestion_id=accept_id,
        action="accept",
        review_key="review-accept",
        proposal={"text": "Allie prefers tea.", "category": "preference"},
    )
    assert prepared["review"]["state"] == "publishing"
    assert prepared["review"]["edited"] is True
    assert "review-accept" not in json.dumps(prepared)
    assert store.prepare_review_decision(
        owner="alice",
        batch_id=created["batch_id"],
        suggestion_id=accept_id,
        action="accept",
        review_key="review-accept",
        proposal={"text": "Allie prefers tea.", "category": "preference"},
    )["replayed"] is True
    store.finalize_review_decision(
        owner="alice",
        batch_id=created["batch_id"],
        suggestion_id=accept_id,
        review_key="review-accept",
        outcome="accepted",
        publication_id="m_tea",
        publication_kind="memory",
    )
    store.prepare_review_decision(
        owner="alice",
        batch_id=created["batch_id"],
        suggestion_id=reject_id,
        action="reject",
        review_key="review-reject",
    )
    finalized = store.finalize_review_decision(
        owner="alice",
        batch_id=created["batch_id"],
        suggestion_id=reject_id,
        review_key="review-reject",
        outcome="rejected",
    )
    assert finalized["review"]["state"] == "rejected"

    status = store.get(owner="alice", batch_id=created["batch_id"])
    assert status["state"] == "succeeded"
    assert status["items"][0]["state"] == "succeeded"
    assert status["review_counts"] == {
        "total": 2,
        "awaiting_review": 0,
        "publishing": 0,
        "accepted": 1,
        "reused": 0,
        "rejected": 1,
        "deferred": 0,
        "edited": 1,
        "pending": 0,
    }
    accepted = next(s for s in status["items"][0]["result"]["suggestions"] if s["suggestion_id"] == accept_id)
    assert accepted["text"] == "Allie drinks tea."  # Original model proposal is retained.
    assert accepted["review"]["proposal"] == {"text": "Allie prefers tea.", "category": "preference"}
    assert accepted["review"]["publication"] == {"id": "m_tea", "kind": "memory"}
    assert store.finalize_review_decision(
        owner="alice",
        batch_id=created["batch_id"],
        suggestion_id=accept_id,
        review_key="review-accept",
        outcome="accepted",
        publication_id="m_tea",
        publication_kind="memory",
    )["replayed"] is True
    with pytest.raises(ImportBatchError) as exc:
        store.prepare_review_decision(
            owner="alice",
            batch_id=created["batch_id"],
            suggestion_id=accept_id,
            action="reject",
            review_key="different-review",
        )
    assert exc.value.code == "review_finalized"


def test_unedited_question_context_is_not_flagged_edited(tmp_path, monkeypatch):
    db = tmp_path / "fm.db"
    _db(db)
    monkeypatch.setattr("services.memory.import_batch.seal_job_manifest", lambda **_: {})
    store = MemoryImportBatchStore(str(db), str(tmp_path))
    created = store.create(
        owner="alice", workspace_id="global", session_id=None, items=_items(b"questions")
    )
    item_id = created["items"][0]["item_id"]
    # The model's raw associations: no contract, no owner scope, and the
    # second one relies on the default mode.
    contexts = [
        {"mode": "inquiry", "target": {"kind": "entity", "id": "principal_handler_abc"}},
        {"target": {"kind": "claim_slot", "id": "identity.preferred_name"}},
    ]
    store.update_item(
        owner="alice", batch_id=created["batch_id"], item_id=item_id,
        state="awaiting_review",
        result={"suggestions": [
            {"text": "What should the assistant call the handler?", "category": "unknown",
             "question_context": contexts[0]},
            {"text": "What is the handler's preferred name?", "category": "unknown",
             "question_context": contexts[1]},
        ]},
    )
    suggestions = store.get(owner="alice", batch_id=created["batch_id"])["items"][0]["result"]["suggestions"]

    from services.memory.question_context import normalize_question_context

    # The review route re-normalizes an unedited context (contract, default
    # mode, owner scope) before the decision is fenced; that normalization
    # alone is not an owner edit.
    unedited = store.prepare_review_decision(
        owner="alice", batch_id=created["batch_id"],
        suggestion_id=suggestions[0]["suggestion_id"], action="accept",
        review_key="review-unedited",
        proposal={
            "text": suggestions[0]["text"],
            "category": "unknown",
            "question_context": normalize_question_context(
                contexts[0], owner="alice", workspace_id="global"
            ),
        },
    )
    assert unedited["review"]["edited"] is False

    # A genuine association change still reads as edited.
    retargeted = dict(contexts[1])
    retargeted["target"] = {"kind": "claim_slot", "id": "identity.pronouns"}
    edited = store.prepare_review_decision(
        owner="alice", batch_id=created["batch_id"],
        suggestion_id=suggestions[1]["suggestion_id"], action="accept",
        review_key="review-retargeted",
        proposal={
            "text": suggestions[1]["text"],
            "category": "unknown",
            "question_context": normalize_question_context(
                retargeted, owner="alice", workspace_id="global"
            ),
        },
    )
    assert edited["review"]["edited"] is True


def test_invalid_model_question_context_gets_typed_422_at_review(tmp_path, monkeypatch):
    db = tmp_path / "fm.db"
    _db(db)
    monkeypatch.setattr("services.memory.import_batch.seal_job_manifest", lambda **_: {})
    monkeypatch.setattr("src.auth_helpers.require_privilege", lambda request, privilege: "alice")
    monkeypatch.setattr(memory_routes, "get_current_user", lambda request: "alice")
    monkeypatch.setattr("routes.prefs_routes._load_for_user", lambda user: {"memory_mode": "automatic"})

    class Provider:
        provider_id = "stub"
        _fm_db_path = str(db)

        async def list_memories(self, *, owner=None, limit=1000):
            return []

        async def remember(self, text, **kwargs):
            return SimpleNamespace(id="memory-imported")

    router = memory_routes.setup_memory_routes(
        SimpleNamespace(find_duplicates=lambda text, records: []),
        SimpleNamespace(get_session=lambda _sid: SimpleNamespace(owner="alice")),
        memory_provider=Provider(),
    )
    endpoint = next(
        route.endpoint
        for route in router.routes
        if route.path == "/api/memory/import-batches/{batch_id}/review" and "POST" in route.methods
    )
    store = MemoryImportBatchStore(str(db), str(tmp_path))
    created = store.create(
        owner="alice", workspace_id="global", session_id=None, items=_items(b"question source")
    )
    item_id = created["items"][0]["item_id"]
    # An invalid model association is stored raw at extraction; the review
    # boundary is where the owner gets typed guidance.
    store.update_item(
        owner="alice", batch_id=created["batch_id"], item_id=item_id,
        state="awaiting_review",
        result={"suggestions": [{
            "text": "What is the handler's preferred name?",
            "category": "unknown",
            "question_context": {"mode": "guess", "target": {"kind": "entity", "id": "x"}},
        }]},
    )
    suggestion_id = store.get(owner="alice", batch_id=created["batch_id"])[
        "items"
    ][0]["result"]["suggestions"][0]["suggestion_id"]

    request = SimpleNamespace(
        state=SimpleNamespace(current_user="alice"),
        app=SimpleNamespace(state=SimpleNamespace(auth_manager=None)),
        headers={"Idempotency-Key": "review-invalid-context"},
    )

    async def body():
        return {"suggestion_id": suggestion_id, "action": "accept"}

    request.json = body
    with pytest.raises(memory_routes.HTTPException) as excinfo:
        asyncio.run(endpoint(request=request, batch_id=created["batch_id"]))
    assert excinfo.value.status_code == 422
    assert excinfo.value.detail["code"] == "INVALID_QUESTION_CONTEXT"
    assert excinfo.value.detail["retryable"] is False
    assert "missing_slot or inquiry" in excinfo.value.detail["message"]


def _misattributed_suggestion(text):
    # A mis-attributed extraction the owner must be able to discard: the
    # stored proposal violates the publish wording gates (Handler role
    # without the %USER% token) and carries an invalid model question
    # association.
    return {
        "text": text,
        "category": "preference",
        "subject_role": "handler",
        "document_role": "mixed_memory",
        "subject_attribution": {
            "contract": "openclank.memory-subject-attribution/v1",
            "role": "handler",
            "entity_id": "principal_handler_" + ("a" * 32),
            "document_role": "mixed_memory",
            "method": "markdown_context",
            "state": "proposed",
            "requires_review": True,
            "section": "Preferences",
            "source_evidence_hash": "b" * 64,
        },
        "question_context": {"mode": "guess", "target": {"kind": "entity", "id": "x"}},
    }


def test_reject_records_extraction_proposal_and_replays(tmp_path, monkeypatch):
    db = tmp_path / "fm.db"
    _db(db)
    monkeypatch.setattr("services.memory.import_batch.seal_job_manifest", lambda **_: {})
    store = MemoryImportBatchStore(str(db), str(tmp_path))
    created = store.create(owner="alice", workspace_id="global", session_id=None, items=_items(b"routine"))
    item_id = created["items"][0]["item_id"]
    store.update_item(
        owner="alice", batch_id=created["batch_id"], item_id=item_id,
        state="awaiting_review",
        result={"suggestions": [
            _misattributed_suggestion("I want a weekday 6:00 AM reminder for my checklist."),
            _misattributed_suggestion("My weekday work routine includes entering time."),
        ]},
    )
    suggestions = store.get(owner="alice", batch_id=created["batch_id"])[
        "items"
    ][0]["result"]["suggestions"]
    reject_id, defer_id = [suggestion["suggestion_id"] for suggestion in suggestions]

    # Reject/defer publish nothing, so the publish gates do not apply: the
    # fence records the immutable extraction proposal for audit, which is
    # deterministic and therefore replay-safe.
    prepared = store.prepare_review_decision(
        owner="alice", batch_id=created["batch_id"],
        suggestion_id=reject_id, action="reject", review_key="review-reject-invalid",
    )
    assert prepared["review"]["state"] == "publishing"
    assert prepared["review"]["edited"] is False
    proposal = prepared["review"]["proposal"]
    assert proposal["text"] == "I want a weekday 6:00 AM reminder for my checklist."
    assert proposal["subject_attribution"]["role"] == "handler"
    assert proposal["question_context"] == {"mode": "guess", "target": {"kind": "entity", "id": "x"}}
    replay = store.prepare_review_decision(
        owner="alice", batch_id=created["batch_id"],
        suggestion_id=reject_id, action="reject", review_key="review-reject-invalid",
    )
    assert replay["replayed"] is True
    assert replay["review"]["proposal"] == proposal
    finalized = store.finalize_review_decision(
        owner="alice", batch_id=created["batch_id"],
        suggestion_id=reject_id, review_key="review-reject-invalid", outcome="rejected",
    )
    assert finalized["review"]["state"] == "rejected"
    with pytest.raises(ImportBatchError) as exc:
        store.prepare_review_decision(
            owner="alice", batch_id=created["batch_id"],
            suggestion_id=reject_id, action="reject", review_key="other-key",
        )
    assert exc.value.code == "review_finalized"

    deferred = store.prepare_review_decision(
        owner="alice", batch_id=created["batch_id"],
        suggestion_id=defer_id, action="defer", review_key="review-defer-invalid",
    )
    assert deferred["review"]["proposal"]["text"] == "My weekday work routine includes entering time."
    finalized = store.finalize_review_decision(
        owner="alice", batch_id=created["batch_id"],
        suggestion_id=defer_id, review_key="review-defer-invalid", outcome="deferred",
    )
    assert finalized["review"]["state"] == "deferred"


def test_reject_and_defer_skip_publish_validation_at_review(tmp_path, monkeypatch):
    db = tmp_path / "fm.db"
    _db(db)
    monkeypatch.setattr("services.memory.import_batch.seal_job_manifest", lambda **_: {})
    monkeypatch.setattr("src.auth_helpers.require_privilege", lambda request, privilege: "alice")
    monkeypatch.setattr(memory_routes, "get_current_user", lambda request: "alice")
    monkeypatch.setattr("routes.prefs_routes._load_for_user", lambda user: {"memory_mode": "automatic"})

    class Provider:
        provider_id = "stub"
        _fm_db_path = str(db)

        async def list_memories(self, *, owner=None, limit=1000):
            return []

        async def remember(self, text, **kwargs):
            return SimpleNamespace(id="memory-imported")

    router = memory_routes.setup_memory_routes(
        SimpleNamespace(find_duplicates=lambda text, records: []),
        SimpleNamespace(get_session=lambda _sid: SimpleNamespace(owner="alice")),
        memory_provider=Provider(),
    )
    endpoint = next(
        route.endpoint
        for route in router.routes
        if route.path == "/api/memory/import-batches/{batch_id}/review" and "POST" in route.methods
    )
    store = MemoryImportBatchStore(str(db), str(tmp_path))
    created = store.create(
        owner="alice", workspace_id="global", session_id=None, items=_items(b"routine source")
    )
    item_id = created["items"][0]["item_id"]
    store.update_item(
        owner="alice", batch_id=created["batch_id"], item_id=item_id,
        state="awaiting_review",
        result={"suggestions": [
            _misattributed_suggestion("I want a weekday 6:00 AM reminder for my checklist."),
            _misattributed_suggestion("My weekday work routine includes entering time."),
            _misattributed_suggestion("I keep my notes in the workshop."),
        ]},
    )
    suggestions = store.get(owner="alice", batch_id=created["batch_id"])[
        "items"
    ][0]["result"]["suggestions"]
    reject_id, defer_id, accept_id = [suggestion["suggestion_id"] for suggestion in suggestions]

    def _request(key, suggestion_id, action):
        request = SimpleNamespace(
            state=SimpleNamespace(current_user="alice"),
            app=SimpleNamespace(state=SimpleNamespace(auth_manager=None)),
            headers={"Idempotency-Key": key},
        )

        async def body():
            return {"suggestion_id": suggestion_id, "action": action}

        request.json = body
        return request

    rejected = asyncio.run(endpoint(
        request=_request("review-reject-route", reject_id, "reject"),
        batch_id=created["batch_id"],
    ))
    assert rejected["ok"] is True
    assert rejected["review"]["review"]["state"] == "rejected"
    assert rejected["review"]["review"]["proposal"]["text"] == (
        "I want a weekday 6:00 AM reminder for my checklist."
    )
    deferred = asyncio.run(endpoint(
        request=_request("review-defer-route", defer_id, "defer"),
        batch_id=created["batch_id"],
    ))
    assert deferred["ok"] is True
    assert deferred["review"]["review"]["state"] == "deferred"

    # Accept of the same malformed suggestion keeps every publish gate.
    with pytest.raises(memory_routes.HTTPException) as excinfo:
        asyncio.run(endpoint(
            request=_request("review-accept-route", accept_id, "accept"),
            batch_id=created["batch_id"],
        ))
    assert excinfo.value.status_code == 422
    assert excinfo.value.detail["code"] == "INVALID_QUESTION_CONTEXT"


def test_batch_review_preserves_server_entity_candidate_against_browser_override(
    tmp_path, monkeypatch
):
    db = tmp_path / "fm.db"
    _db(db)
    monkeypatch.setattr("services.memory.import_batch.seal_job_manifest", lambda **_: {})
    store = MemoryImportBatchStore(str(db), str(tmp_path))
    created = store.create(
        owner="alice", workspace_id="global", session_id=None, items=_items(b"Ada wrote this.")
    )
    assistant_id = "principal_assistant_" + ("a" * 32)
    attacker_id = "principal_assistant_" + ("b" * 32)
    candidate = {
        "contract": "openclank.memory-entity-match-candidate/v1",
        "entity_id": assistant_id,
        "role": "assistant_self",
        "matched_alias": "Ada",
        "match_method": "persona_setting_exact",
        "state": "proposed",
        "requires_review": True,
    }
    store.update_item(
        owner="alice",
        batch_id=created["batch_id"],
        item_id=created["items"][0]["item_id"],
        state="awaiting_review",
        result={
            "suggestions": [{
                "text": "Ada wrote this.",
                "category": "fact",
                "entity_match_candidates": [candidate],
            }],
        },
    )
    suggestion = store.get(owner="alice", batch_id=created["batch_id"])["items"][0][
        "result"
    ]["suggestions"][0]
    malicious = dict(candidate, entity_id=attacker_id, matched_alias="Mallory")
    prepared = store.prepare_review_decision(
        owner="alice",
        batch_id=created["batch_id"],
        suggestion_id=suggestion["suggestion_id"],
        action="accept",
        review_key="candidate-review",
        proposal={
            "text": "Ada wrote this clearly.",
            "category": "fact",
            "entity_match_candidates": [malicious],
        },
    )

    assert prepared["review"]["proposal"]["entity_match_candidates"] == [candidate]
    assert prepared["review"]["proposal"]["entity_match_candidates"][0]["entity_id"] == assistant_id


def test_batch_drops_malformed_entity_match_candidate(tmp_path, monkeypatch):
    db = tmp_path / "fm.db"
    _db(db)
    monkeypatch.setattr("services.memory.import_batch.seal_job_manifest", lambda **_: {})
    store = MemoryImportBatchStore(str(db), str(tmp_path))
    created = store.create(
        owner="alice", workspace_id="global", session_id=None, items=_items(b"Ada wrote this.")
    )
    store.update_item(
        owner="alice",
        batch_id=created["batch_id"],
        item_id=created["items"][0]["item_id"],
        state="awaiting_review",
        result={
            "suggestions": [{
                "text": "Ada wrote this.",
                "category": "fact",
                "entity_match_candidates": [{
                    "contract": "openclank.memory-entity-match-candidate/v1",
                    "entity_id": "model_supplied_entity",
                    "role": "assistant_self",
                    "matched_alias": "Ada",
                    "match_method": "persona_setting_exact",
                    "state": "proposed",
                    "requires_review": True,
                }],
            }],
        },
    )

    suggestion = store.get(owner="alice", batch_id=created["batch_id"])["items"][0][
        "result"
    ]["suggestions"][0]
    assert "entity_match_candidates" not in suggestion


def test_batch_review_preserves_server_handler_attribution_against_browser_override(
    tmp_path, monkeypatch
):
    db = tmp_path / "fm.db"
    _db(db)
    monkeypatch.setattr("services.memory.import_batch.seal_job_manifest", lambda **_: {})
    store = MemoryImportBatchStore(str(db), str(tmp_path))
    created = store.create(
        owner="alice",
        workspace_id="global",
        session_id=None,
        items=_items(b"handler preference"),
    )
    attribution = {
        "contract": "openclank.memory-subject-attribution/v1",
        "role": "handler",
        "entity_id": "principal_handler_" + ("a" * 32),
        "document_role": "mixed_memory",
        "method": "markdown_context",
        "state": "proposed",
        "requires_review": True,
        "section": "Preferences",
        "source_evidence_hash": "b" * 64,
    }
    item_id = created["items"][0]["item_id"]
    store.update_item(
        owner="alice",
        batch_id=created["batch_id"],
        item_id=item_id,
        state="awaiting_review",
        result={"suggestions": [{
            "text": "%USER%'s preferred checklist includes showering.",
            "category": "preference",
            "subject_role": "handler",
            "document_role": "mixed_memory",
            "subject_attribution": attribution,
        }]},
    )
    suggestion = store.get(owner="alice", batch_id=created["batch_id"])["items"][0][
        "result"
    ]["suggestions"][0]
    prepared = store.prepare_review_decision(
        owner="alice",
        batch_id=created["batch_id"],
        suggestion_id=suggestion["suggestion_id"],
        action="accept",
        review_key="handler-review",
        proposal={
            "text": "%USER%'s preferred checklist includes showering.",
            "category": "preference",
            "subject_attribution": dict(
                attribution,
                entity_id="principal_handler_" + ("c" * 32),
            ),
        },
    )

    assert prepared["review"]["proposal"]["subject_attribution"] == attribution
    assert prepared["review"]["proposal"]["subject_role"] == "handler"


def test_review_rejects_text_that_changes_a_reserved_subject(tmp_path, monkeypatch):
    db = tmp_path / "fm.db"
    _db(db)
    monkeypatch.setattr("services.memory.import_batch.seal_job_manifest", lambda **_: {})
    store = MemoryImportBatchStore(str(db), str(tmp_path))
    attributions = (
        (
            {
                "contract": "openclank.memory-subject-attribution/v1",
                "role": "handler",
                "entity_id": "principal_handler_" + ("a" * 32),
                "document_role": "handler_profile",
                "method": "document_profile",
                "state": "proposed",
                "requires_review": True,
                "section": "Context",
                "source_evidence_hash": "b" * 64,
            },
            "%USER% prefers tea.",
            "I prefer coffee.",
        ),
        (
            {
                "contract": "openclank.memory-subject-attribution/v1",
                "role": "assistant_self",
                "entity_id": "principal_assistant_" + ("a" * 32),
                "document_role": "assistant_identity",
                "method": "document_profile",
                "state": "proposed",
                "requires_review": True,
                "section": "Identity",
                "source_evidence_hash": "c" * 64,
            },
            "My name is Ada.",
            "%USER%'s name is Allie.",
        ),
        (
            {
                "contract": "openclank.memory-subject-attribution/v1",
                "role": "assistant_self",
                "entity_id": "principal_assistant_" + ("a" * 32),
                "document_role": "assistant_identity",
                "method": "document_profile",
                "state": "proposed",
                "requires_review": True,
                "section": "Identity",
                "source_evidence_hash": "d" * 64,
            },
            "My preferred drink is tea.",
            "Allie prefers coffee.",
        ),
    )
    for index, (attribution, original, edited) in enumerate(attributions):
        created = store.create(
            owner="alice",
            workspace_id="global",
            session_id=None,
            items=_items(f"subject-{index}".encode()),
        )
        item_id = created["items"][0]["item_id"]
        store.update_item(
            owner="alice",
            batch_id=created["batch_id"],
            item_id=item_id,
            state="awaiting_review",
            result={"suggestions": [{
                "text": original,
                "category": "preference",
                "subject_role": attribution["role"],
                "document_role": attribution["document_role"],
                "subject_attribution": attribution,
            }]},
        )
        suggestion = store.get(owner="alice", batch_id=created["batch_id"])["items"][0][
            "result"
        ]["suggestions"][0]
        with pytest.raises(ImportBatchError) as exc:
            store.prepare_review_decision(
                owner="alice",
                batch_id=created["batch_id"],
                suggestion_id=suggestion["suggestion_id"],
                action="accept",
                review_key=f"subject-edit-{index}",
                proposal={"text": edited, "category": "preference"},
            )
        assert exc.value.code == "subject_attribution_conflict"


def test_batch_retry_creates_replacement_child_under_v2_state_guard(tmp_path, monkeypatch):
    db = tmp_path / "fm.db"
    _db(db)
    monkeypatch.setattr("services.memory.import_batch.seal_job_manifest", lambda **_: {})
    store = MemoryImportBatchStore(str(db), str(tmp_path))
    created = store.create(owner="alice", workspace_id="global", session_id=None, items=_items(b"bad", b"good"))
    failed_id, good_id = [item["item_id"] for item in created["items"]]
    store.stage_bytes("alice", created["batch_id"], failed_id, b"bad")
    store.update_item(
        owner="alice", batch_id=created["batch_id"], item_id=good_id,
        state="succeeded", result={"outcome": "empty", "suggestions": []},
    )
    store.update_item(
        owner="alice", batch_id=created["batch_id"], item_id=failed_id,
        state="failed_terminal", error={"code": "unsupported_file"},
    )
    assert store.get(owner="alice", batch_id=created["batch_id"])["state"] == "awaiting_review"

    retry = store.begin_item_retry(owner="alice", batch_id=created["batch_id"], item_id=failed_id)
    retry_id = retry["item"]["item_id"]
    assert retry_id != failed_id
    assert retry["item"]["retry_of"] == failed_id
    assert retry["item"]["document_role"] == "generic_document"
    assert store.read_staged("alice", created["batch_id"], retry_id) == b"bad"
    status = store.get(owner="alice", batch_id=created["batch_id"])
    assert status["state"] == "active"
    old = next(item for item in status["items"] if item["item_id"] == failed_id)
    assert old["state"] == "failed_terminal"
    assert old["superseded_by"] == retry_id
    assert next(item for item in status["items"] if item["item_id"] == retry_id)["state"] == "queued"

    store.update_item(
        owner="alice", batch_id=created["batch_id"], item_id=retry_id,
        state="awaiting_review", result={"outcome": "suggestions", "suggestions": [{"text": "Recovered fact."}]},
    )
    status = store.get(owner="alice", batch_id=created["batch_id"])
    assert status["state"] == "awaiting_review"
    assert status["counts"] == {"selected": 2, "completed": 2, "reused": 0, "failed": 0, "pending": 0}


def test_all_failed_batch_remains_actionable_and_old_retry_is_fenced(tmp_path, monkeypatch):
    db = tmp_path / "fm.db"
    _db(db)
    monkeypatch.setattr("services.memory.import_batch.seal_job_manifest", lambda **_: {})
    store = MemoryImportBatchStore(str(db), str(tmp_path))
    created = store.create(owner="alice", workspace_id="global", session_id=None, items=_items(b"bad"))
    failed_id = created["items"][0]["item_id"]
    store.stage_bytes("alice", created["batch_id"], failed_id, b"bad")
    store.update_item(
        owner="alice", batch_id=created["batch_id"], item_id=failed_id,
        state="failed_terminal", error={"code": "MODEL_UNAVAILABLE"},
    )
    assert store.get(owner="alice", batch_id=created["batch_id"])["state"] == "awaiting_review"

    retry = store.begin_item_retry(owner="alice", batch_id=created["batch_id"], item_id=failed_id)
    retry_id = retry["item"]["item_id"]
    assert retry["state"] == "active"
    with pytest.raises(ImportBatchError) as exc:
        store.begin_item_retry(owner="alice", batch_id=created["batch_id"], item_id=failed_id)
    assert exc.value.code == "item_retry_superseded"

    store.update_item(
        owner="alice", batch_id=created["batch_id"], item_id=retry_id,
        state="failed_terminal", error={"code": "MODEL_UNAVAILABLE"},
    )
    replacement = store.begin_item_retry(
        owner="alice", batch_id=created["batch_id"], item_id=retry_id
    )
    assert replacement["item"]["retry_of"] == retry_id


def test_batch_review_failure_releases_the_fenced_card_for_retry(tmp_path, monkeypatch):
    db = tmp_path / "fm.db"
    _db(db)
    monkeypatch.setattr("services.memory.import_batch.seal_job_manifest", lambda **_: {})
    store = MemoryImportBatchStore(str(db), str(tmp_path))
    created = store.create(owner="alice", workspace_id="global", session_id=None, items=_items(b"one"))
    item_id = created["items"][0]["item_id"]
    store.update_item(
        owner="alice", batch_id=created["batch_id"], item_id=item_id,
        state="awaiting_review", result={"suggestions": [{"text": "A durable fact."}]},
    )
    suggestion_id = store.get(owner="alice", batch_id=created["batch_id"])["items"][0]["result"]["suggestions"][0]["suggestion_id"]
    store.prepare_review_decision(
        owner="alice", batch_id=created["batch_id"], suggestion_id=suggestion_id,
        action="accept", review_key="first-save",
    )
    failed = store.fail_review_decision(
        owner="alice", batch_id=created["batch_id"], suggestion_id=suggestion_id,
        review_key="first-save", error={"code": "PROVIDER_DOWN", "message": "Memory provider unavailable", "retryable": True},
    )
    assert failed["review"]["state"] == "awaiting_review"
    assert failed["review"]["last_failure"]["code"] == "PROVIDER_DOWN"
    prepared_again = store.prepare_review_decision(
        owner="alice", batch_id=created["batch_id"], suggestion_id=suggestion_id,
        action="accept", review_key="second-save",
    )
    assert prepared_again["review"]["state"] == "publishing"
    status = store.get(owner="alice", batch_id=created["batch_id"])
    stored = status["items"][0]["result"]["suggestions"][0]
    assert stored["review_history"][0]["last_failure"]["code"] == "PROVIDER_DOWN"


def test_batch_owner_scope_and_staged_source_ttl(tmp_path, monkeypatch):
    db = tmp_path / "fm.db"
    _db(db)
    monkeypatch.setattr("services.memory.import_batch.seal_job_manifest", lambda **_: {})
    store = MemoryImportBatchStore(str(db), str(tmp_path))
    first = store.create(owner="alice", workspace_id="global", session_id=None, items=_items(b"secret"))
    item_id = first["items"][0]["item_id"]
    store.stage_bytes("alice", first["batch_id"], item_id, b"secret")
    assert store.read_staged("alice", first["batch_id"], item_id) == b"secret"
    with pytest.raises(ImportBatchError) as exc:
        store.get(owner="bob", batch_id=first["batch_id"])
    assert exc.value.code == "batch_not_found"


def test_duplicate_bytes_can_be_distinct_source_items(tmp_path, monkeypatch):
    db = tmp_path / "fm.db"
    _db(db)
    monkeypatch.setattr("services.memory.import_batch.seal_job_manifest", lambda **_: {})
    store = MemoryImportBatchStore(str(db), str(tmp_path))
    created = store.create(
        owner="alice", workspace_id="global", session_id=None,
        items=[
            {"item_id": "item-a", "filename": "a.md", "byte_size": 4, "content_hash": "same", "extension": ".md"},
            {"item_id": "item-b", "filename": "b.md", "byte_size": 4, "content_hash": "same", "extension": ".md"},
        ],
    )
    assert len(created["items"]) == 2
    assert len({item["item_id"] for item in created["items"]}) == 2
    assert {item["source_id"] for item in created["items"]} == {"item-a", "item-b"}


def test_batch_rejects_oversized_selection(tmp_path, monkeypatch):
    db = tmp_path / "fm.db"
    _db(db)
    monkeypatch.setattr("services.memory.import_batch.seal_job_manifest", lambda **_: {})
    store = MemoryImportBatchStore(str(db), str(tmp_path))
    with pytest.raises(ImportBatchError) as exc:
        store.create(
            owner="alice",
            workspace_id="global",
            session_id=None,
            items=[{
                "item_id": "item_large",
                "filename": "large.md",
                "byte_size": 51 * 1024 * 1024,
                "content_hash": "a" * 64,
                "extension": ".md",
            }],
        )
    assert exc.value.code == "batch_byte_limit"


def test_batch_route_processes_multiple_json_files_and_replays(tmp_path, monkeypatch):
    db = tmp_path / "fm.db"
    _db(db)
    monkeypatch.setattr("services.memory.import_batch.seal_job_manifest", lambda **_: {})
    monkeypatch.setattr("src.auth_helpers.require_privilege", lambda request, privilege: "alice")
    monkeypatch.setattr(memory_routes, "get_current_user", lambda request: "alice")
    persona_calls = []
    principal_calls = []

    def persona_label(owner, supplied=None):
        persona_calls.append((owner, supplied))
        return "Nyx"

    def principal_context(**kwargs):
        principal_calls.append(kwargs)
        # Uphold the production invariant the ensure-cache relies on: the
        # ensured binding is readable back from fm_v2_principal_bindings, so
        # memoized follow-up requests resolve the same identity context.
        with sqlite3.connect(str(db)) as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS fm_v2_principal_bindings("
                "owner_id TEXT, workspace_key TEXT, project_key TEXT,"
                " binding_key TEXT, assistant_entity_id TEXT,"
                " handler_entity_id TEXT, revision INTEGER,"
                " created_at TEXT, updated_at TEXT,"
                " PRIMARY KEY(owner_id,workspace_key,project_key,binding_key))"
            )
            conn.execute(
                "INSERT OR REPLACE INTO fm_v2_principal_bindings"
                " VALUES (?,?,?,?,?,?,1,'2026-01-01T00:00:00Z','2026-01-01T00:00:00Z')",
                (
                    "alice",
                    str(kwargs.get("workspace_id") or "global"),
                    str(kwargs.get("project_id") or ""),
                    f"assistant:default:handler:{kwargs.get('handler_principal_id') or 'alice'}",
                    "principal_assistant_0123456789abcdef0123456789abcdef",
                    "principal_handler_fedcba9876543210fedcba9876543210",
                ),
            )
        return {
            "assistant_entity_id": "principal_assistant_0123456789abcdef0123456789abcdef",
            "handler_entity_id": "principal_handler_fedcba9876543210fedcba9876543210",
        }

    monkeypatch.setattr(
        "services.memory.principal_context.resolve_assistant_persona_label",
        persona_label,
    )
    monkeypatch.setattr(
        "services.memory.principal_context.ensure_principal_context",
        principal_context,
    )
    provider = SimpleNamespace(_fm_db_path=str(db), provider_id="stub")
    session_manager = SimpleNamespace(
        get_session=lambda _sid: SimpleNamespace(owner="alice")
    )
    router = memory_routes.setup_memory_routes(
        SimpleNamespace(), session_manager, memory_provider=provider
    )
    endpoint = next(
        route.endpoint
        for route in router.routes
        if route.path == "/api/memory/import-batches" and "POST" in route.methods
    )
    files = [
        UploadFile(file=io.BytesIO(b'[{"text":"one","category":"fact"}]'), filename="one.json"),
        UploadFile(file=io.BytesIO(b'[{"text":"two","category":"project"}]'), filename="two.json"),
    ]
    request = SimpleNamespace(state=SimpleNamespace(current_user="alice"))
    result = __import__("asyncio").run(endpoint(request=request, session="s1", files=files))
    assert result["state"] == "awaiting_review"
    assert result["counts"] == {"selected": 2, "completed": 2, "reused": 0, "failed": 0, "pending": 0}
    assert {s["text"] for item in result["items"] for s in item["result"]["suggestions"]} == {"one", "two"}
    # The ensure wrapper resolves the persona label once more per request to
    # key its memo, and the bootstrap itself runs only on the first request
    # with an unchanged identity (S04 memoization).
    assert persona_calls == [("alice", None), ("alice", "Nyx")]
    assert len(principal_calls) == 1

    replay = __import__("asyncio").run(
        endpoint(
            request=SimpleNamespace(state=SimpleNamespace(current_user="alice")),
            session="s1",
            files=[
                UploadFile(file=io.BytesIO(b'[{"text":"one","category":"fact"}]'), filename="renamed-a.json"),
                UploadFile(file=io.BytesIO(b'[{"text":"two","category":"project"}]'), filename="renamed-b.json"),
            ],
        )
    )
    assert replay["batch_id"] == result["batch_id"]
    assert persona_calls == [("alice", None), ("alice", "Nyx")] * 2
    # Memo hit: the unchanged identity does not re-run the bootstrap.
    assert len(principal_calls) == 1


def test_batch_item_failure_never_persists_or_displays_html_provider_body(tmp_path, monkeypatch):
    db = tmp_path / "fm.db"
    _db(db)
    monkeypatch.setattr("services.memory.import_batch.seal_job_manifest", lambda **_: {})
    monkeypatch.setattr("src.auth_helpers.require_privilege", lambda request, privilege: "alice")
    monkeypatch.setattr(memory_routes, "get_current_user", lambda request: "alice")

    async def giant_html(*_args, **_kwargs):
        raise RuntimeError("<!doctype html><html><body>private upstream diagnostic</body></html>")

    monkeypatch.setattr(memory_routes, "complete_text", giant_html)
    monkeypatch.setattr(
        "src.openclank.modality_facade.managed_route_preflight",
        lambda **_kwargs: {"configured": True, "binding_revision": 1, "eligible_routes": []},
    )
    router = memory_routes.setup_memory_routes(
        SimpleNamespace(),
        SimpleNamespace(get_session=lambda _sid: SimpleNamespace(owner="alice")),
        memory_provider=SimpleNamespace(_fm_db_path=str(db), provider_id="stub"),
    )
    endpoint = next(
        route.endpoint
        for route in router.routes
        if route.path == "/api/memory/import-batches" and "POST" in route.methods
    )
    result = asyncio.run(endpoint(
        request=SimpleNamespace(state=SimpleNamespace(current_user="alice")),
        session=None,
        files=[UploadFile(file=io.BytesIO(b"plain text"), filename="note.md")],
    ))
    error = result["items"][0]["error"]
    assert error["code"] == "MEMORY_IMPORT_EXTRACTION_FAILED"
    assert "<" not in error["message"]
    assert "private upstream" not in error["message"]


def test_failed_batch_retry_rejects_changed_persona_and_reupload_rekeys(tmp_path, monkeypatch):
    db = tmp_path / "fm.db"
    _db(db)
    monkeypatch.setattr("services.memory.import_batch.seal_job_manifest", lambda **_: {})
    monkeypatch.setattr("src.auth_helpers.require_privilege", lambda request, privilege: "alice")
    monkeypatch.setattr(memory_routes, "get_current_user", lambda request: "alice")
    persona = {"name": "Nyx"}
    principal_id = "principal_assistant_0123456789abcdef0123456789abcdef"
    monkeypatch.setattr(
        "services.memory.principal_context.resolve_assistant_persona_label",
        lambda owner, supplied=None: str(supplied or persona["name"]),
    )
    monkeypatch.setattr(
        "services.memory.principal_context.ensure_principal_context",
        lambda **_kwargs: {
            "assistant_entity_id": principal_id,
            "handler_entity_id": "principal_handler_fedcba9876543210fedcba9876543210",
        },
    )
    monkeypatch.setattr(
        "src.openclank.modality_facade.managed_route_preflight",
        lambda **_kwargs: {"configured": True, "binding_revision": 1, "eligible_routes": []},
    )
    model_calls = []

    async def fail_model(*_args, **_kwargs):
        model_calls.append(persona["name"])
        raise RuntimeError("provider failed")

    monkeypatch.setattr(memory_routes, "complete_text", fail_model)
    router = memory_routes.setup_memory_routes(
        SimpleNamespace(),
        SimpleNamespace(get_session=lambda _sid: SimpleNamespace(owner="alice")),
        memory_provider=SimpleNamespace(_fm_db_path=str(db), provider_id="stub"),
    )
    batch_endpoint = next(
        route.endpoint for route in router.routes
        if route.path == "/api/memory/import-batches" and "POST" in route.methods
    )
    retry_endpoint = next(
        route.endpoint for route in router.routes
        if route.path == "/api/memory/import-batches/{batch_id}/retry" and "POST" in route.methods
    )

    first = asyncio.run(batch_endpoint(
        request=SimpleNamespace(state=SimpleNamespace(current_user="alice")),
        session=None,
        files=[UploadFile(file=io.BytesIO(b"a notebook"), filename="note.md")],
    ))
    failed_item_id = first["items"][0]["item_id"]
    original_item_count = len(first["items"])
    assert model_calls == ["Nyx"]

    persona["name"] = "Nova"
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        asyncio.run(retry_endpoint(
            request=SimpleNamespace(state=SimpleNamespace(current_user="alice")),
            batch_id=first["batch_id"],
            item_id=failed_item_id,
            session=None,
        ))
    assert exc.value.status_code == 409
    assert exc.value.detail["code"] == "MEMORY_IMPORT_IDENTITY_CONTEXT_CHANGED"
    unchanged = MemoryImportBatchStore(str(db), str(tmp_path)).get(
        owner="alice", batch_id=first["batch_id"]
    )
    assert len(unchanged["items"]) == original_item_count
    assert model_calls == ["Nyx"]

    reuploaded = asyncio.run(batch_endpoint(
        request=SimpleNamespace(state=SimpleNamespace(current_user="alice")),
        session=None,
        files=[UploadFile(file=io.BytesIO(b"a notebook"), filename="note.md")],
    ))
    assert reuploaded["batch_id"] != first["batch_id"]
    assert model_calls == ["Nyx", "Nova"]


def test_retry_http_failure_terminalizes_replacement_child(tmp_path, monkeypatch):
    from fastapi import HTTPException

    db = tmp_path / "fm.db"
    _db(db)
    monkeypatch.setattr("services.memory.import_batch.seal_job_manifest", lambda **_: {})
    monkeypatch.setattr("src.auth_helpers.require_privilege", lambda request, privilege: "alice")
    monkeypatch.setattr(memory_routes, "get_current_user", lambda request: "alice")
    monkeypatch.setattr(
        "services.memory.principal_context.resolve_assistant_persona_label",
        lambda owner, supplied=None: "Nyx",
    )
    monkeypatch.setattr(
        "services.memory.principal_context.ensure_principal_context_cached",
        lambda **_kwargs: {
            "assistant_entity_id": "principal_assistant_" + ("a" * 32),
            "handler_entity_id": "principal_handler_" + ("b" * 32),
        },
    )
    monkeypatch.setattr(
        "src.openclank.modality_facade.managed_route_preflight",
        lambda **_kwargs: {
            "configured": True,
            "binding_revision": 1,
            "eligible_routes": [],
        },
    )
    calls = [RuntimeError("first attempt failed")]

    async def completion(*_args, **_kwargs):
        error = calls.pop(0)
        raise error

    monkeypatch.setattr(memory_routes, "complete_text", completion)
    router = memory_routes.setup_memory_routes(
        SimpleNamespace(),
        SimpleNamespace(get_session=lambda _sid: SimpleNamespace(owner="alice")),
        memory_provider=SimpleNamespace(_fm_db_path=str(db), provider_id="stub"),
    )
    batch_endpoint = next(
        route.endpoint for route in router.routes
        if route.path == "/api/memory/import-batches" and "POST" in route.methods
    )
    retry_endpoint = next(
        route.endpoint for route in router.routes
        if route.path == "/api/memory/import-batches/{batch_id}/retry" and "POST" in route.methods
    )
    request = lambda: SimpleNamespace(
        state=SimpleNamespace(current_user="alice"),
        app=SimpleNamespace(state=SimpleNamespace(auth_manager=None)),
    )
    first = asyncio.run(batch_endpoint(
        request=request(),
        session=None,
        files=[UploadFile(file=io.BytesIO(b"a notebook"), filename="note.md")],
    ))
    original_id = first["items"][0]["item_id"]
    calls.append(HTTPException(status_code=409, detail={
        "code": "MEMORY_ROUTE_UNCONFIGURED",
        "message": "Choose a Memory model.",
        "retryable": True,
    }))

    with pytest.raises(HTTPException) as exc:
        asyncio.run(retry_endpoint(
            request=request(),
            batch_id=first["batch_id"],
            item_id=original_id,
            session=None,
        ))
    assert exc.value.status_code == 409

    stored = MemoryImportBatchStore(str(db), str(tmp_path)).get(
        owner="alice", batch_id=first["batch_id"]
    )
    replacement = next(
        item for item in stored["items"] if item.get("retry_of") == original_id
    )
    assert replacement["state"] == "failed_terminal"
    assert replacement["error"]["code"] == "MEMORY_ROUTE_UNCONFIGURED"
    assert stored["state"] == "awaiting_review"
    assert stored["counts"]["pending"] == 0


def test_single_file_import_never_returns_html_provider_body(tmp_path, monkeypatch):
    db = tmp_path / "fm.db"
    _db(db)
    monkeypatch.setattr("src.auth_helpers.require_privilege", lambda request, privilege: "alice")
    monkeypatch.setattr(memory_routes, "get_current_user", lambda request: "alice")

    async def giant_html(*_args, **_kwargs):
        raise RuntimeError("<!doctype html><html><body>private upstream diagnostic</body></html>")

    monkeypatch.setattr(memory_routes, "complete_text", giant_html)
    monkeypatch.setattr(
        "src.openclank.modality_facade.managed_route_preflight",
        lambda **_kwargs: {"configured": True, "binding_revision": 1, "eligible_routes": []},
    )
    router = memory_routes.setup_memory_routes(
        SimpleNamespace(),
        SimpleNamespace(get_session=lambda _sid: SimpleNamespace(owner="alice")),
        memory_provider=SimpleNamespace(_fm_db_path=str(db), provider_id="stub"),
    )
    endpoint = next(
        route.endpoint for route in router.routes
        if route.path == "/api/memory/import" and "POST" in route.methods
    )
    request = SimpleNamespace(state=SimpleNamespace(current_user="alice"))
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        asyncio.run(endpoint(
            request=request,
            session=None,
            file=UploadFile(file=io.BytesIO(b"plain text"), filename="note.md"),
        ))
    assert exc.value.status_code == 502
    assert exc.value.detail["code"] == "MEMORY_IMPORT_EXTRACTION_FAILED"
    assert "<" not in exc.value.detail["message"]


def test_batch_review_route_publishes_once_with_import_provenance(tmp_path, monkeypatch):
    db = tmp_path / "fm.db"
    _db(db)
    monkeypatch.setattr("services.memory.import_batch.seal_job_manifest", lambda **_: {})
    monkeypatch.setattr("src.auth_helpers.require_privilege", lambda request, privilege: "alice")
    monkeypatch.setattr(memory_routes, "get_current_user", lambda request: "alice")
    monkeypatch.setattr("routes.prefs_routes._load_for_user", lambda user: {"memory_mode": "automatic"})

    class Provider:
        provider_id = "stub"

        def __init__(self):
            self._fm_db_path = str(db)
            self.remember_calls = []

        async def list_memories(self, *, owner=None, limit=1000):
            return []

        async def remember(self, text, **kwargs):
            self.remember_calls.append((text, kwargs))
            return SimpleNamespace(id="memory-imported")

    provider = Provider()
    manager = SimpleNamespace(find_duplicates=lambda text, records: [])
    router = memory_routes.setup_memory_routes(
        manager,
        SimpleNamespace(get_session=lambda _sid: SimpleNamespace(owner="alice")),
        memory_provider=provider,
    )
    endpoint = next(
        route.endpoint
        for route in router.routes
        if route.path == "/api/memory/import-batches/{batch_id}/review" and "POST" in route.methods
    )
    store = MemoryImportBatchStore(str(db), str(tmp_path))
    created = store.create(
        owner="alice", workspace_id="global", session_id=None, items=_items(b"imported source")
    )
    item_id = created["items"][0]["item_id"]
    store.update_item(
        owner="alice", batch_id=created["batch_id"], item_id=item_id,
        state="awaiting_review",
        result={"suggestions": [{
            "text": "%USER% — A sourced imported fact.",
            "category": "fact",
            "subject_role": "handler",
            "document_role": "handler_profile",
            "subject_attribution": {
                "contract": "openclank.memory-subject-attribution/v1",
                "role": "handler",
                "entity_id": "principal_handler_" + ("a" * 32),
                "document_role": "handler_profile",
                "method": "document_profile",
                "state": "proposed",
                "requires_review": True,
                "section": "Context",
                "source_evidence_hash": "b" * 64,
            },
        }]},
    )
    status = store.get(owner="alice", batch_id=created["batch_id"])
    suggestion_id = status["items"][0]["result"]["suggestions"][0]["suggestion_id"]

    def request():
        value = SimpleNamespace(
            state=SimpleNamespace(current_user="alice"),
            app=SimpleNamespace(state=SimpleNamespace(auth_manager=None)),
            headers={"Idempotency-Key": "review-once"},
        )

        async def body():
            return {
                "suggestion_id": suggestion_id,
                "action": "accept",
                "proposal": {"text": "%USER% — A sourced imported fact.", "category": "fact"},
            }

        value.json = body
        return value

    first = asyncio.run(endpoint(request=request(), batch_id=created["batch_id"]))
    assert first["ok"] is True
    assert len(provider.remember_calls) == 1
    text, kwargs = provider.remember_calls[0]
    assert text == "%USER% — A sourced imported fact."
    assert kwargs["source"] == "memory_import"
    assert kwargs["source_type"] == "auto_extracted"
    provenance = kwargs["metadata"]["import_review"]
    assert provenance["batch_id"] == created["batch_id"]
    assert provenance["item_id"] == item_id
    assert provenance["suggestion_id"] == suggestion_id
    assert kwargs["metadata"]["subject_role"] == "handler"
    assert kwargs["metadata"]["source_document_role"] == "handler_profile"
    assert kwargs["metadata"]["subject_attribution"]["entity_id"].startswith(
        "principal_handler_"
    )

    replay = asyncio.run(endpoint(request=request(), batch_id=created["batch_id"]))
    assert replay["ok"] is True
    assert replay["review"]["replayed"] is True
    assert len(provider.remember_calls) == 1


def test_replayed_publishing_review_without_landed_write_releases_and_republishes(tmp_path, monkeypatch):
    db = tmp_path / "fm.db"
    _db(db)
    monkeypatch.setattr("services.memory.import_batch.seal_job_manifest", lambda **_: {})
    monkeypatch.setattr("src.auth_helpers.require_privilege", lambda request, privilege: "alice")
    monkeypatch.setattr(memory_routes, "get_current_user", lambda request: "alice")
    monkeypatch.setattr("routes.prefs_routes._load_for_user", lambda user: {"memory_mode": "automatic"})

    class Provider:
        provider_id = "stub"
        _fm_db_path = str(db)

        async def list_memories(self, *, owner=None, limit=1000):
            return []

        async def remember(self, text, **kwargs):
            return SimpleNamespace(id="m_reconciled")

    router = memory_routes.setup_memory_routes(
        SimpleNamespace(find_duplicates=lambda text, records: []),
        SimpleNamespace(get_session=lambda _sid: SimpleNamespace(owner="alice")),
        memory_provider=Provider(),
    )
    endpoint = next(
        route.endpoint
        for route in router.routes
        if route.path == "/api/memory/import-batches/{batch_id}/review" and "POST" in route.methods
    )
    store = MemoryImportBatchStore(str(db), str(tmp_path))
    created = store.create(owner="alice", workspace_id="global", session_id=None, items=_items(b"one"))
    item_id = created["items"][0]["item_id"]
    store.update_item(
        owner="alice", batch_id=created["batch_id"], item_id=item_id,
        state="awaiting_review", result={"suggestions": [{"text": "A fact.", "category": "fact"}]},
    )
    suggestion_id = store.get(owner="alice", batch_id=created["batch_id"])["items"][0]["result"]["suggestions"][0]["suggestion_id"]
    # Simulate a crash after prepare: the decision is fenced, nothing landed.
    store.prepare_review_decision(
        owner="alice", batch_id=created["batch_id"], suggestion_id=suggestion_id,
        action="accept", review_key="already-publishing",
        proposal={"text": "A fact.", "category": "fact"},
    )
    request = SimpleNamespace(
        state=SimpleNamespace(current_user="alice"),
        app=SimpleNamespace(state=SimpleNamespace(auth_manager=None)),
        headers={"Idempotency-Key": "already-publishing"},
    )

    async def body():
        return {
            "suggestion_id": suggestion_id,
            "action": "accept",
            "proposal": {"text": "A fact.", "category": "fact"},
        }

    request.json = body
    result = asyncio.run(endpoint(request=request, batch_id=created["batch_id"]))
    assert result["ok"] is True
    stored = store.get(owner="alice", batch_id=created["batch_id"])["items"][0]["result"]["suggestions"][0]
    assert stored["review"]["state"] == "accepted"
    assert stored["review"]["publication"]["id"] == "m_reconciled"
    # The released fence is preserved as history, not silently forgotten.
    assert any(
        entry.get("last_failure", {}).get("code") == "review_reconciled"
        for entry in stored.get("review_history", [])
    )


def test_post_publish_review_receipt_failure_reconciles_to_reused(tmp_path, monkeypatch):
    db = tmp_path / "fm.db"
    _db(db)
    monkeypatch.setattr("services.memory.import_batch.seal_job_manifest", lambda **_: {})
    monkeypatch.setattr("src.auth_helpers.require_privilege", lambda request, privilege: "alice")
    monkeypatch.setattr(memory_routes, "get_current_user", lambda request: "alice")
    monkeypatch.setattr("routes.prefs_routes._load_for_user", lambda user: {"memory_mode": "automatic"})

    saved = []

    class Provider:
        provider_id = "stub"
        _fm_db_path = str(db)

        async def list_memories(self, *, owner=None, limit=1000):
            return list(saved)

        async def remember(self, text, **kwargs):
            record = SimpleNamespace(
                id="saved-before-receipt", text=text, timestamp="now",
                category="fact", source="memory_import", owner="alice",
                session_id=None, metadata={},
            )
            saved.append(record)
            return record

    original_finalize = MemoryImportBatchStore.finalize_review_decision
    failed_once = {"done": False}

    def fail_accept_receipt_once(self, *args, **kwargs):
        if kwargs.get("outcome") == "accepted" and not failed_once["done"]:
            failed_once["done"] = True
            raise ImportBatchError("receipt_write_failed", "The review receipt could not be written.", retryable=True)
        return original_finalize(self, *args, **kwargs)

    monkeypatch.setattr(MemoryImportBatchStore, "finalize_review_decision", fail_accept_receipt_once)
    router = memory_routes.setup_memory_routes(
        SimpleNamespace(find_duplicates=lambda text, records: [
            {"id": record["id"]} for record in records
        ]),
        SimpleNamespace(get_session=lambda _sid: SimpleNamespace(owner="alice")),
        memory_provider=Provider(),
    )
    endpoint = next(
        route.endpoint
        for route in router.routes
        if route.path == "/api/memory/import-batches/{batch_id}/review" and "POST" in route.methods
    )
    store = MemoryImportBatchStore(str(db), str(tmp_path))
    created = store.create(owner="alice", workspace_id="global", session_id=None, items=_items(b"one"))
    item_id = created["items"][0]["item_id"]
    store.update_item(
        owner="alice", batch_id=created["batch_id"], item_id=item_id,
        state="awaiting_review", result={"suggestions": [{"text": "A fact.", "category": "fact"}]},
    )
    suggestion_id = store.get(owner="alice", batch_id=created["batch_id"])["items"][0]["result"]["suggestions"][0]["suggestion_id"]
    request = SimpleNamespace(
        state=SimpleNamespace(current_user="alice"),
        app=SimpleNamespace(state=SimpleNamespace(auth_manager=None)),
        headers={"Idempotency-Key": "post-publish-receipt"},
    )

    async def body():
        return {
            "suggestion_id": suggestion_id,
            "action": "accept",
            "proposal": {"text": "A fact.", "category": "fact"},
        }

    request.json = body
    from fastapi import HTTPException

    # First attempt: the memory is saved but the receipt write fails.
    with pytest.raises(HTTPException) as exc:
        asyncio.run(endpoint(request=request, batch_id=created["batch_id"]))
    assert exc.value.status_code == 503
    assert exc.value.detail["code"] == "REVIEW_RECONCILIATION_REQUIRED"
    stored = store.get(owner="alice", batch_id=created["batch_id"])["items"][0]["result"]["suggestions"][0]
    assert stored["review"]["state"] == "publishing"
    assert len(saved) == 1

    # Retry with the same key: the landed write is adopted as `reused`
    # instead of publishing a duplicate or dead-ending on the fence.
    result = asyncio.run(endpoint(request=request, batch_id=created["batch_id"]))
    assert result["ok"] is True
    stored = store.get(owner="alice", batch_id=created["batch_id"])["items"][0]["result"]["suggestions"][0]
    assert stored["review"]["state"] == "reused"
    assert stored["review"]["publication"]["id"] == "saved-before-receipt"
    assert len(saved) == 1


def test_photo_admission_normalizes_and_stores_owner_scoped_asset(tmp_path):
    source = Image.new("RGB", (8, 4), (120, 40, 200))
    raw = BytesIO()
    source.save(raw, format="PNG")
    admitted = admit_photo(raw.getvalue(), "memory.png", "image/png")
    assert admitted.media_type == "image/png"
    assert admitted.width == 8 and admitted.height == 4
    assert admitted.canonical_sha256

    store = MemoryMediaStore(str(tmp_path / "fm.db"), str(tmp_path))
    stored = store.put(
        owner="alice",
        source_id="item-photo",
        filename="memory.png",
        photo=admitted,
        associated_text="A violet notebook is on the desk.",
        provenance={"route": "memory-route"},
    )
    assert stored["asset_id"] == admitted.asset_id
    listed = store.list_assets("alice")
    assert listed[0]["representations"][0]["text"] == "A violet notebook is on the desk."
    _, blob = store.read_blob("alice", admitted.asset_id)
    assert blob == admitted.bytes
    with pytest.raises(MediaAssetError) as exc:
        store.read_blob("bob", admitted.asset_id)
    assert exc.value.code == "asset_not_found"


def test_photo_admission_rejects_svg_and_animation():
    with pytest.raises(MediaAssetError) as svg:
        admit_photo(b"<svg></svg>", "memory.svg", "image/svg+xml")
    assert svg.value.code == "photo_format_unsupported"


def test_list_pending_surfaces_only_unresolved_owner_batches(tmp_path, monkeypatch):
    db = tmp_path / "fm.db"
    _db(db)
    monkeypatch.setattr("services.memory.import_batch.seal_job_manifest", lambda **_: {})
    store = MemoryImportBatchStore(str(db), str(tmp_path))
    active = store.create(owner="alice", workspace_id="global", session_id=None, items=_items(b"one"))
    review = store.create(owner="alice", workspace_id="global", session_id=None, items=_items(b"two"))
    store.create(owner="bob", workspace_id="global", session_id=None, items=_items(b"three"))
    done = store.create(owner="alice", workspace_id="global", session_id=None, items=_items(b"four"))

    store.update_item(
        owner="alice",
        batch_id=review["batch_id"],
        item_id=review["items"][0]["item_id"],
        state="awaiting_review",
        result={"outcome": "suggestions", "suggestions": [{"text": "two fact", "category": "fact"}]},
    )
    store.update_item(
        owner="alice",
        batch_id=done["batch_id"],
        item_id=done["items"][0]["item_id"],
        state="succeeded",
        result={"outcome": "empty", "suggestions": []},
    )

    batches = store.list_pending(owner="alice")
    ids = [batch["batch_id"] for batch in batches]
    assert review["batch_id"] in ids
    assert active["batch_id"] in ids
    assert done["batch_id"] not in ids
    assert all(batch["batch_id"].startswith("batch_") for batch in batches)
    # Bob's unresolved batch is owner-scoped out.
    assert len(ids) == 2
    # Most recently updated first.
    assert ids[0] == review["batch_id"]
    summary = next(batch for batch in batches if batch["batch_id"] == review["batch_id"])
    assert summary["state"] == "awaiting_review"
    assert summary["review_counts"]["pending"] == 1
    assert summary["filenames"] == ["note-0.md"]
    assert summary["created_at"] and summary["updated_at"]


def test_list_pending_rejects_terminal_states_and_bad_limits(tmp_path, monkeypatch):
    db = tmp_path / "fm.db"
    _db(db)
    monkeypatch.setattr("services.memory.import_batch.seal_job_manifest", lambda **_: {})
    store = MemoryImportBatchStore(str(db), str(tmp_path))
    store.create(owner="alice", workspace_id="global", session_id=None, items=_items(b"one"))
    assert store.list_pending(owner="alice", states=("succeeded",)) == []
    assert len(store.list_pending(owner="alice", limit=0)) == 1


def test_import_batch_list_endpoint_supports_timeout_recovery(tmp_path, monkeypatch):
    db = tmp_path / "fm.db"
    _db(db)
    monkeypatch.setattr("services.memory.import_batch.seal_job_manifest", lambda **_: {})
    monkeypatch.setattr("src.auth_helpers.require_privilege", lambda request, privilege: "alice")
    monkeypatch.setattr(memory_routes, "get_current_user", lambda request: "alice")
    provider = SimpleNamespace(_fm_db_path=str(db), provider_id="stub")
    router = memory_routes.setup_memory_routes(
        SimpleNamespace(),
        SimpleNamespace(get_session=lambda _sid: SimpleNamespace(owner="alice")),
        memory_provider=provider,
    )
    post_endpoint = next(
        route.endpoint
        for route in router.routes
        if route.path == "/api/memory/import-batches" and "POST" in route.methods
    )
    list_endpoint = next(
        route.endpoint
        for route in router.routes
        if route.path == "/api/memory/import-batches" and "GET" in route.methods
    )
    created = asyncio.run(post_endpoint(
        request=SimpleNamespace(state=SimpleNamespace(current_user="alice")),
        session=None,
        files=[UploadFile(file=io.BytesIO(b'[{"text":"one","category":"fact"}]'), filename="one.json")],
    ))
    assert created["state"] == "awaiting_review"

    listed = asyncio.run(list_endpoint(
        request=SimpleNamespace(state=SimpleNamespace(current_user="alice")),
    ))
    assert listed["contract"] == "openclank.memory-import-batch-list/v1"
    assert [batch["batch_id"] for batch in listed["batches"]] == [created["batch_id"]]
    batch = listed["batches"][0]
    assert batch["state"] == "awaiting_review"
    assert batch["review_counts"]["pending"] == 1
    assert batch["filenames"] == ["one.json"]


def test_extraction_suggestions_are_capped_at_100(tmp_path, monkeypatch):
    # The 100-fact ceiling is a server admission bound: a broken or hostile
    # model must not stuff unbounded review cards into the durable ledger.
    db = tmp_path / "fm.db"
    _db(db)
    monkeypatch.setattr("services.memory.import_batch.seal_job_manifest", lambda **_: {})
    store = MemoryImportBatchStore(str(db), str(tmp_path))
    created = store.create(owner="alice", workspace_id="global", session_id=None, items=_items(b"one"))
    item_id = created["items"][0]["item_id"]
    store.update_item(
        owner="alice", batch_id=created["batch_id"], item_id=item_id,
        state="awaiting_review",
        result={"suggestions": [{"text": f"fact {i}", "category": "fact"} for i in range(150)]},
    )
    stored = store.get(owner="alice", batch_id=created["batch_id"])["items"][0]
    suggestions = stored["result"]["suggestions"]
    assert len(suggestions) == 100
    assert suggestions[0]["text"] == "fact 0"
    assert suggestions[-1]["text"] == "fact 99"
    assert all(s["suggestion_id"].startswith("suggestion_") for s in suggestions)


def test_create_rejects_non_numeric_byte_size(tmp_path, monkeypatch):
    db = tmp_path / "fm.db"
    _db(db)
    monkeypatch.setattr("services.memory.import_batch.seal_job_manifest", lambda **_: {})
    store = MemoryImportBatchStore(str(db), str(tmp_path))
    items = _items(b"one")
    items[0]["byte_size"] = "not-a-number"
    with pytest.raises(ImportBatchError) as exc:
        store.create(owner="alice", workspace_id="global", session_id=None, items=items)
    assert exc.value.code == "invalid_manifest"


def test_staged_identity_rejects_traversal(tmp_path, monkeypatch):
    db = tmp_path / "fm.db"
    _db(db)
    monkeypatch.setattr("services.memory.import_batch.seal_job_manifest", lambda **_: {})
    store = MemoryImportBatchStore(str(db), str(tmp_path))
    created = store.create(owner="alice", workspace_id="global", session_id=None, items=_items(b"one"))
    batch_id = created["batch_id"]
    for bad in ("item_../x", "item_ABCDEF", "item_", "../batch_x", "item_" + "a" * 200):
        with pytest.raises(ImportBatchError) as exc:
            store.stage_bytes("alice", batch_id, bad, b"x")
        assert exc.value.code == "invalid_identity"
        with pytest.raises(ImportBatchError) as exc:
            store.read_staged("alice", batch_id, bad)
        assert exc.value.code == "invalid_identity"
    with pytest.raises(ImportBatchError) as exc:
        store.stage_bytes("alice", "batch_../escape", "item_" + "a" * 32, b"x")
    assert exc.value.code == "invalid_identity"


def _handler_attribution():
    return {
        "contract": "openclank.memory-subject-attribution/v1",
        "role": "handler",
        "entity_id": "principal_handler_" + "a" * 32,
        "document_role": "handler_profile",
        "method": "document_profile",
        "state": "proposed",
        "requires_review": True,
        "section": "",
        "source_evidence_hash": "b" * 64,
    }


def test_batch_payload_renders_handler_label_and_keeps_raw_text(tmp_path, monkeypatch):
    db = tmp_path / "fm.db"
    _db(db)
    monkeypatch.setattr("services.memory.import_batch.seal_job_manifest", lambda **_: {})
    monkeypatch.setattr("src.auth_helpers.require_privilege", lambda request, privilege: "alice")
    monkeypatch.setattr(memory_routes, "get_current_user", lambda request: "alice")
    # The owner renamed their Handler principal; the Brain displays "Allie".
    monkeypatch.setattr(memory_routes, "resolve_handler_display_label", lambda *a, **k: "Allie")
    provider = SimpleNamespace(_fm_db_path=str(db), provider_id="stub")
    router = memory_routes.setup_memory_routes(
        SimpleNamespace(),
        SimpleNamespace(get_session=lambda _sid: SimpleNamespace(owner="alice")),
        memory_provider=provider,
    )
    get_endpoint = next(
        route.endpoint
        for route in router.routes
        if route.path == "/api/memory/import-batches/{batch_id}" and "GET" in route.methods
    )
    store = MemoryImportBatchStore(str(db), str(tmp_path))
    created = store.create(owner="alice", workspace_id="global", session_id=None, items=_items(b"one"))
    item_id = created["items"][0]["item_id"]
    store.update_item(
        owner="alice", batch_id=created["batch_id"], item_id=item_id,
        state="awaiting_review",
        result={"suggestions": [{
            "text": "%USER% prefers tea.",
            "category": "fact",
            "subject_attribution": _handler_attribution(),
        }]},
    )
    payload = asyncio.run(get_endpoint(
        request=SimpleNamespace(state=SimpleNamespace(current_user="alice")),
        batch_id=created["batch_id"],
    ))
    suggestion = payload["items"][0]["result"]["suggestions"][0]
    assert suggestion["text"] == "Allie prefers tea."
    assert suggestion["raw_text"] == "%USER% prefers tea."
    # The durable ledger keeps the canonical token.
    stored = store.get(owner="alice", batch_id=created["batch_id"])["items"][0]["result"]["suggestions"][0]
    assert stored["text"] == "%USER% prefers tea."
    assert "raw_text" not in stored


def test_review_submit_maps_rendered_handler_label_back_to_token(tmp_path, monkeypatch):
    db = tmp_path / "fm.db"
    _db(db)
    monkeypatch.setattr("services.memory.import_batch.seal_job_manifest", lambda **_: {})
    monkeypatch.setattr("src.auth_helpers.require_privilege", lambda request, privilege: "alice")
    monkeypatch.setattr(memory_routes, "get_current_user", lambda request: "alice")
    monkeypatch.setattr("routes.prefs_routes._load_for_user", lambda user: {"memory_mode": "automatic"})
    monkeypatch.setattr(memory_routes, "resolve_handler_display_label", lambda *a, **k: "Allie")

    saved = []

    class Provider:
        provider_id = "stub"
        _fm_db_path = str(db)

        async def list_memories(self, *, owner=None, limit=1000):
            return list(saved)

        async def remember(self, text, **kwargs):
            record = SimpleNamespace(
                id="m_handler", text=text, timestamp="now",
                category="fact", source="memory_import", owner="alice",
                session_id=None, metadata=dict(kwargs.get("metadata") or {}),
            )
            saved.append(record)
            return record

    router = memory_routes.setup_memory_routes(
        SimpleNamespace(find_duplicates=lambda text, records: []),
        SimpleNamespace(get_session=lambda _sid: SimpleNamespace(owner="alice")),
        memory_provider=Provider(),
    )
    endpoint = next(
        route.endpoint
        for route in router.routes
        if route.path == "/api/memory/import-batches/{batch_id}/review" and "POST" in route.methods
    )
    store = MemoryImportBatchStore(str(db), str(tmp_path))
    created = store.create(owner="alice", workspace_id="global", session_id=None, items=_items(b"one"))
    item_id = created["items"][0]["item_id"]
    store.update_item(
        owner="alice", batch_id=created["batch_id"], item_id=item_id,
        state="awaiting_review",
        result={"suggestions": [{
            "text": "%USER% prefers tea.",
            "category": "fact",
            "subject_attribution": _handler_attribution(),
        }]},
    )
    suggestion_id = store.get(owner="alice", batch_id=created["batch_id"])["items"][0]["result"]["suggestions"][0]["suggestion_id"]
    request = SimpleNamespace(
        state=SimpleNamespace(current_user="alice"),
        app=SimpleNamespace(state=SimpleNamespace(auth_manager=None)),
        headers={"Idempotency-Key": "rendered-submit"},
    )

    async def body():
        # The browser reviewed the rendered projection, never the token.
        return {
            "suggestion_id": suggestion_id,
            "action": "accept",
            "proposal": {"text": "Allie prefers tea.", "category": "fact"},
        }

    request.json = body
    result = asyncio.run(endpoint(request=request, batch_id=created["batch_id"]))
    assert result["ok"] is True
    # The published and ledgered claim keeps the canonical, rename-safe token.
    assert saved[0].text == "%USER% prefers tea."
    stored = store.get(owner="alice", batch_id=created["batch_id"])["items"][0]["result"]["suggestions"][0]
    assert stored["review"]["state"] == "accepted"


def _awaiting_review_batch(store, content=b"one", suggestion_text="A durable fact."):
    created = store.create(owner="alice", workspace_id="global", session_id=None, items=_items(content))
    store.update_item(
        owner="alice", batch_id=created["batch_id"], item_id=created["items"][0]["item_id"],
        state="awaiting_review", result={"suggestions": [{"text": suggestion_text}]},
    )
    return created


def test_dismiss_marks_batch_and_redismiss_is_idempotent(tmp_path, monkeypatch):
    db = tmp_path / "fm.db"
    _db(db)
    monkeypatch.setattr("services.memory.import_batch.seal_job_manifest", lambda **_: {})
    store = MemoryImportBatchStore(str(db), str(tmp_path))
    created = _awaiting_review_batch(store)

    dismissed = store.dismiss_batch(owner="alice", batch_id=created["batch_id"])
    assert dismissed["dismissed_at"]
    status = store.get(owner="alice", batch_id=created["batch_id"])
    assert status["dismissed_at"] == dismissed["dismissed_at"]
    # The payload marker never moves the v2 job state column.
    assert status["state"] == "awaiting_review"
    replay = store.dismiss_batch(owner="alice", batch_id=created["batch_id"])
    assert replay["dismissed_at"] == dismissed["dismissed_at"]


def test_dismissed_batch_leaves_list_pending_but_stays_gettable(tmp_path, monkeypatch):
    db = tmp_path / "fm.db"
    _db(db)
    monkeypatch.setattr("services.memory.import_batch.seal_job_manifest", lambda **_: {})
    store = MemoryImportBatchStore(str(db), str(tmp_path))
    created = _awaiting_review_batch(store)
    assert [batch["batch_id"] for batch in store.list_pending(owner="alice")] == [created["batch_id"]]

    store.dismiss_batch(owner="alice", batch_id=created["batch_id"])
    assert store.list_pending(owner="alice") == []
    status = store.get(owner="alice", batch_id=created["batch_id"])
    assert status["batch_id"] == created["batch_id"]
    assert status["dismissed_at"]


def test_review_after_dismiss_clears_marker_but_replay_keeps_it(tmp_path, monkeypatch):
    db = tmp_path / "fm.db"
    _db(db)
    monkeypatch.setattr("services.memory.import_batch.seal_job_manifest", lambda **_: {})
    store = MemoryImportBatchStore(str(db), str(tmp_path))
    created = _awaiting_review_batch(store)
    suggestion_id = store.get(owner="alice", batch_id=created["batch_id"])[
        "items"
    ][0]["result"]["suggestions"][0]["suggestion_id"]
    store.dismiss_batch(owner="alice", batch_id=created["batch_id"])

    # Owner re-engagement un-dismisses: a newly fenced decision clears the marker.
    prepared = store.prepare_review_decision(
        owner="alice", batch_id=created["batch_id"], suggestion_id=suggestion_id,
        action="accept", review_key="review-after-dismiss",
    )
    assert prepared["replayed"] is False
    assert prepared["review"]["state"] == "publishing"
    status = store.get(owner="alice", batch_id=created["batch_id"])
    assert "dismissed_at" not in status
    assert [batch["batch_id"] for batch in store.list_pending(owner="alice")] == [created["batch_id"]]

    # Replaying the already-fenced decision must not silently clear a fresh dismissal.
    store.dismiss_batch(owner="alice", batch_id=created["batch_id"])
    replay = store.prepare_review_decision(
        owner="alice", batch_id=created["batch_id"], suggestion_id=suggestion_id,
        action="accept", review_key="review-after-dismiss",
    )
    assert replay["replayed"] is True
    assert store.get(owner="alice", batch_id=created["batch_id"])["dismissed_at"]


def test_dismiss_rejects_batches_not_waiting_for_review(tmp_path, monkeypatch):
    db = tmp_path / "fm.db"
    _db(db)
    monkeypatch.setattr("services.memory.import_batch.seal_job_manifest", lambda **_: {})
    store = MemoryImportBatchStore(str(db), str(tmp_path))
    created = store.create(owner="alice", workspace_id="global", session_id=None, items=_items(b"one", b"two"))

    # A freshly created batch is queued — still working, not reviewable.
    with pytest.raises(ImportBatchError) as exc:
        store.dismiss_batch(owner="alice", batch_id=created["batch_id"])
    assert exc.value.code == "invalid_batch_state"

    store.update_item(
        owner="alice", batch_id=created["batch_id"], item_id=created["items"][0]["item_id"],
        state="awaiting_review", result={"suggestions": [{"text": "one"}]},
    )
    assert store.get(owner="alice", batch_id=created["batch_id"])["state"] == "active"
    with pytest.raises(ImportBatchError) as exc:
        store.dismiss_batch(owner="alice", batch_id=created["batch_id"])
    assert exc.value.code == "invalid_batch_state"
    assert "dismissed_at" not in store.get(owner="alice", batch_id=created["batch_id"])

    with pytest.raises(ImportBatchError) as exc:
        store.dismiss_batch(owner="alice", batch_id="batch_missing")
    assert exc.value.code == "batch_not_found"


def test_dismiss_route_is_owner_scoped_and_privileged(tmp_path, monkeypatch):
    db = tmp_path / "fm.db"
    _db(db)
    monkeypatch.setattr("services.memory.import_batch.seal_job_manifest", lambda **_: {})
    privileges = []

    def require(request, privilege):
        privileges.append(privilege)
        return "alice"

    monkeypatch.setattr("src.auth_helpers.require_privilege", require)
    current = {"user": "alice"}
    monkeypatch.setattr(memory_routes, "get_current_user", lambda request: current["user"])
    router = memory_routes.setup_memory_routes(
        SimpleNamespace(),
        SimpleNamespace(get_session=lambda _sid: SimpleNamespace(owner="alice")),
        memory_provider=SimpleNamespace(_fm_db_path=str(db), provider_id="stub"),
    )
    endpoint = next(
        route.endpoint
        for route in router.routes
        if route.path == "/api/memory/import-batches/{batch_id}/dismiss" and "POST" in route.methods
    )
    store = MemoryImportBatchStore(str(db), str(tmp_path))
    created = _awaiting_review_batch(store)

    def request():
        return SimpleNamespace(
            state=SimpleNamespace(current_user=current["user"]),
            app=SimpleNamespace(state=SimpleNamespace(auth_manager=None)),
        )

    # A foreign owner cannot see or dismiss the batch.
    current["user"] = "bob"
    with pytest.raises(memory_routes.HTTPException) as excinfo:
        asyncio.run(endpoint(request=request(), batch_id=created["batch_id"]))
    assert excinfo.value.status_code == 409
    assert excinfo.value.detail["code"] == "batch_not_found"
    assert "dismissed_at" not in store.get(owner="alice", batch_id=created["batch_id"])

    current["user"] = "alice"
    dismissed = asyncio.run(endpoint(request=request(), batch_id=created["batch_id"]))
    assert dismissed["dismissed_at"]
    assert dismissed["state"] == "awaiting_review"
    assert privileges == ["can_manage_memory", "can_manage_memory"]
