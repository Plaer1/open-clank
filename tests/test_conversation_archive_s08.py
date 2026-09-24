"""S08 shared history, memory assimilation and compaction — focused units.

Failure shapes under test (from the real gaps, not synthetic happy paths):

1. text-pair capture gap — host capture used to store only user/assistant text
   pairs; tool results, tool calls and owned assets vanished from history.
2. native memory merge — managed Frankenmemory search used to merge native
   memory rows, giving two competing retrieval owners.
3. replace_messages rekey — host compaction deleted every durable message row
   and assigned fresh IDs, so original history identity was destroyed.

Plus the S08 contracts: lossless ordered source-part archive, durable
idempotent outbox, one active compactor per execution context, and full-part
history/media access with target paging bounds.
"""

from __future__ import annotations

import json
import os
import uuid

import pytest

import core.database as cdb
import core.session_manager as session_manager_module
from core.models import ChatMessage
from src.context_compactor import maybe_compact
from src.memory_gate import capture_allowed
from src.openclank.conversation_archive import (
    AROUND_AFTER_MAX,
    GET_LENGTH_MAX_UTF16,
    SEARCH_LIMIT_MAX,
    ConversationArchive,
    SourcePart,
    get_conversation_archive,
    reset_conversation_archive_for_test,
)
from src.openclank.history_broker_adapter import HistoryBrokerAdapter
from tests.helpers.sqlite_db import make_temp_sqlite


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def archive(tmp_path):
    reset_conversation_archive_for_test()
    path = tmp_path / "conversation_archive.sqlite3"
    yield ConversationArchive(db_path=str(path))
    reset_conversation_archive_for_test()


@pytest.fixture
def manager_db(monkeypatch, tmp_path):
    SessionLocal, engine, tmpfile = make_temp_sqlite(cdb.Base.metadata)
    monkeypatch.setattr(session_manager_module, "SessionLocal", SessionLocal)
    archive_path = tmp_path / "conversation_archive.sqlite3"
    monkeypatch.setenv("OPENCLANK_CONVERSATION_ARCHIVE_DB", str(archive_path))
    reset_conversation_archive_for_test()
    manager = session_manager_module.SessionManager.__new__(
        session_manager_module.SessionManager
    )
    manager.sessions = {}
    manager.upload_handler = None
    try:
        yield manager, SessionLocal, engine, archive_path
    finally:
        engine.dispose()
        tmpfile.close()
        try:
            os.unlink(tmpfile.name)
        except OSError:
            pass
        reset_conversation_archive_for_test()


def _seed_session(SessionLocal, *, owner="alice", message_count=0, name="s08"):
    session_id = "s08-" + uuid.uuid4().hex
    db = SessionLocal()
    try:
        db.add(
            cdb.Session(
                id=session_id,
                owner=owner,
                name=name,
                model="test-model",
                endpoint_url="http://localhost:11434",
                archived=False,
                message_count=message_count,
            )
        )
        db.commit()
    finally:
        db.close()
    return session_id


def _seed_messages(SessionLocal, session_id, rows):
    """rows: list of (id, role, content, metadata)."""
    db = SessionLocal()
    try:
        for msg_id, role, content, metadata in rows:
            db.add(
                cdb.ChatMessage(
                    id=msg_id,
                    session_id=session_id,
                    role=role,
                    content=content,
                    meta_data=json.dumps(metadata) if metadata else None,
                )
            )
        db.commit()
    finally:
        db.close()


# ---------------------------------------------------------------------------
# 1. text-pair capture gap
# ---------------------------------------------------------------------------


def test_archive_captures_tool_parts_not_just_text_pairs(archive):
    """Tool results / tool calls / assets must be ordered source parts."""
    result = archive.append_parts(
        [
            {
                "owner": "alice",
                "chat_id": "chat-1",
                "actor_id": "main",
                "message_id": "msg-1",
                "part_id": "body",
                "revision": 1,
                "role": "user",
                "part_type": "text",
                "content": "run the tests",
                "event_sequence": 0,
            },
            {
                "owner": "alice",
                "chat_id": "chat-1",
                "actor_id": "main",
                "message_id": "msg-1",
                "part_id": "tool_call:0",
                "revision": 1,
                "role": "assistant",
                "part_type": "tool_call",
                "content": {"tool_name": "bash", "input": {"cmd": "pytest"}},
                "event_sequence": 1,
            },
            {
                "owner": "alice",
                "chat_id": "chat-1",
                "actor_id": "main",
                "message_id": "msg-1",
                "part_id": "tool_result:0",
                "revision": 1,
                "role": "assistant",
                "part_type": "tool_result",
                "content": {"tool_name": "bash", "output": "3 passed"},
                "event_sequence": 2,
            },
            {
                "owner": "alice",
                "chat_id": "chat-1",
                "actor_id": "main",
                "message_id": "msg-2",
                "part_id": "body",
                "revision": 1,
                "role": "assistant",
                "part_type": "text",
                "content": "all green",
                "event_sequence": 3,
                "assets": [
                    {
                        "asset_id": "asset-1",
                        "content_hash": "abc",
                        "mime_type": "text/plain",
                        "byte_size": 12,
                        "provenance": {"source": "host_upload"},
                    }
                ],
            },
        ]
    )
    assert result["accepted"] == 4
    counts = archive.count_parts(owner="alice", chat_id="chat-1")
    assert counts["live"] == 4

    # Search finds tool output as a first-class part (not just text pairs).
    hits = archive.search(owner="alice", chat_id="chat-1", query="3 passed")
    assert hits["ok"] is True
    assert any(h["part_type"] == "tool_result" for h in hits["hits"])

    # Full-part get reads the canonical body, not an FTS preview.
    part = archive.get_part(
        owner="alice",
        chat_id="chat-1",
        message_id="msg-1",
        part_id="tool_call:0",
    )
    assert part["ok"] is True
    assert part["part"]["part_type"] == "tool_call"
    assert "pytest" in part["part"]["text"]

    # Owned asset refs travel with the part.
    body = archive.get_part(
        owner="alice",
        chat_id="chat-1",
        message_id="msg-2",
        part_id="body",
    )
    assert body["ok"] is True
    assert body["part"]["assets"][0]["asset_id"] == "asset-1"


def test_manager_add_message_archives_full_parts(manager_db):
    """SessionManager.add_message archives tool parts, not only the text body."""
    manager, SessionLocal, engine, archive_path = manager_db
    session_id = _seed_session(SessionLocal)
    message = ChatMessage(
        role="assistant",
        content="done",
        metadata={
            "tool_calls": [{"name": "read", "id": "c1"}],
            "tool_results": [{"tool_call_id": "c1", "output": "file contents"}],
            "attachments": [
                {
                    "id": "att-1",
                    "name": "notes.txt",
                    "mime": "text/plain",
                    "size": 5,
                }
            ],
        },
    )
    manager.get_session(session_id)
    manager.add_message(session_id, message)

    archive = ConversationArchive(db_path=str(archive_path))
    counts = archive.count_parts(owner="alice", chat_id=session_id)
    # body + tool_call + tool_result + asset = at least 3 parts (asset may
    # attach to body rather than being its own part).
    assert counts["live"] >= 3

    tool_call = archive.get_part(
        owner="alice",
        chat_id=session_id,
        message_id=message.metadata["_db_id"],
        part_id="tool_call:0",
    )
    assert tool_call["ok"] is True
    assert tool_call["part"]["part_type"] == "tool_call"

    tool_result = archive.get_part(
        owner="alice",
        chat_id=session_id,
        message_id=message.metadata["_db_id"],
        part_id="tool_result:0",
    )
    assert tool_result["ok"] is True
    assert "file contents" in tool_result["part"]["text"]


def test_memory_gate_still_blocks_capture_but_not_archive(manager_db, monkeypatch):
    """Archive plumbing cannot admit memory when memory is off."""
    manager, SessionLocal, engine, archive_path = manager_db
    session_id = _seed_session(SessionLocal)
    manager.get_session(session_id)
    manager.add_message(
        session_id,
        ChatMessage(role="user", content="my secret preference", metadata={}),
    )
    # History archive keeps the source regardless of memory_mode.
    archive = ConversationArchive(db_path=str(archive_path))
    counts = archive.count_parts(owner="alice", chat_id=session_id)
    assert counts["live"] == 1

    # Memory admission stays separately gated.
    assert capture_allowed({"memory_mode": "off"}) is False
    assert capture_allowed({"memory_mode": "off"}, incognito=True) is False
    assert capture_allowed({"memory_mode": "automatic"}) is True
    assert capture_allowed({"memory_mode": "automatic"}, compare_mode=True) is False


# ---------------------------------------------------------------------------
# 2. durable idempotent outbox
# ---------------------------------------------------------------------------


def test_outbox_is_idempotent_on_part_identity(archive):
    part = {
        "owner": "alice",
        "chat_id": "chat-1",
        "actor_id": "main",
        "message_id": "msg-1",
        "part_id": "body",
        "revision": 1,
        "role": "user",
        "part_type": "text",
        "content": "hello",
    }
    first = archive.append_parts([part])
    second = archive.append_parts([part])
    third = archive.append_parts([part])

    assert first["accepted"] == 1
    assert first["enqueued"] == 1
    assert second["duplicate"] == 1
    assert third["duplicate"] == 1
    # Duplicate delivery must not create a second outbox event.
    assert archive.outbox_pending_count(owner="alice", chat_id="chat-1") == 1


def test_outbox_rejects_same_revision_different_content(archive):
    archive.append_parts(
        [{
            "owner": "alice", "chat_id": "chat-1", "actor_id": "main",
            "message_id": "msg-1", "part_id": "body", "revision": 1,
            "role": "user", "part_type": "text", "content": "hello",
        }]
    )
    with pytest.raises(ValueError, match="already exists with different content"):
        archive.append_parts(
            [{
                "owner": "alice", "chat_id": "chat-1", "actor_id": "main",
                "message_id": "msg-1", "part_id": "body", "revision": 1,
                "role": "user", "part_type": "text", "content": "goodbye",
            }]
    )


def test_outbox_claim_mark_and_restart_states(archive):
    archive.append_parts(
        [{
            "owner": "alice", "chat_id": "chat-1", "actor_id": "main",
            "message_id": "msg-1", "part_id": "body", "revision": 1,
            "role": "user", "part_type": "text", "content": "hello",
        }]
    )
    claimed = archive.claim_outbox(owner="alice", chat_id="chat-1", limit=10)
    assert len(claimed) == 1
    assert claimed[0]["state"] == "running"

    # Deliver idempotently.
    assert archive.mark_outbox(claimed[0]["outbox_seq"], state="delivered") is True
    assert archive.outbox_pending_count(owner="alice", chat_id="chat-1") == 0

    # A crash mid-delivery leaves a reconciling/pending state, never false success.
    archive.append_parts(
        [{
            "owner": "alice", "chat_id": "chat-1", "actor_id": "main",
            "message_id": "msg-2", "part_id": "body", "revision": 1,
            "role": "user", "part_type": "text", "content": "again",
        }]
    )
    claimed2 = archive.claim_outbox(owner="alice", chat_id="chat-1", limit=10)
    assert len(claimed2) == 1
    assert archive.mark_outbox(claimed2[0]["outbox_seq"], state="reconciling") is True
    assert archive.outbox_pending_count(owner="alice", chat_id="chat-1") == 1

    # Monotonic cursor makes interruption/restart/backfill deterministic.
    archive.advance_cursor(owner="alice", chat_id="chat-1", consumer="fm", last_outbox_seq=claimed2[0]["outbox_seq"])
    assert archive.cursor(owner="alice", chat_id="chat-1", consumer="fm") == claimed2[0]["outbox_seq"]


def test_pending_delivery_cannot_justify_pruning_source(archive):
    archive.append_parts(
        [{
            "owner": "alice", "chat_id": "chat-1", "actor_id": "main",
            "message_id": "msg-1", "part_id": "body", "revision": 1,
            "role": "user", "part_type": "text", "content": "keep me",
        }]
    )
    key = ("alice", "chat-1", "main", "msg-1", "body", 1)
    assert archive.pending_for_part(key) == 1
    # Tombstone versions the destructive change; the source row remains.
    archive.tombstone_part(
        owner="alice", chat_id="chat-1", message_id="msg-1", part_id="body",
        revision=1, reason="projection_drop",
    )
    counts = archive.count_parts(owner="alice", chat_id="chat-1")
    assert counts["total"] == 1
    assert counts["tombstoned"] == 1
    got = archive.get_part(
        owner="alice", chat_id="chat-1", message_id="msg-1", part_id="body"
    )
    assert got["ok"] is True
    assert got["part"]["tombstone"] is True
    assert got["part"]["text"] == "keep me"


# ---------------------------------------------------------------------------
# 3. replace_messages must not delete/rekey original history
# ---------------------------------------------------------------------------


def test_replace_messages_preserves_retained_ids_and_archives_source(manager_db):
    manager, SessionLocal, engine, archive_path = manager_db
    session_id = _seed_session(SessionLocal)
    original_ids = [f"orig-{uuid.uuid4().hex}" for _ in range(3)]
    _seed_messages(
        SessionLocal,
        session_id,
        [
            (original_ids[0], "user", "turn one", {"source": "before"}),
            (original_ids[1], "assistant", "turn two", {"source": "before"}),
            (original_ids[2], "user", "turn three", {"source": "before"}),
        ],
    )

    # Simulate loaded session with stable identities.
    session = manager.get_session(session_id)
    retained = []
    for msg in list(session.history):
        mid = msg.metadata.get("_db_id") if msg.metadata else None
        if mid in original_ids[-2:]:
            msg.persistence_id = mid
            retained.append(msg)

    summary = ChatMessage(
        role="system",
        content="[Conversation summary]\nsummary of turn one",
        metadata={"compacted": True, "summarized_count": 1},
    )
    incoming = [summary] + retained
    assert manager.replace_messages(session_id, incoming) is True

    # 1) Retained messages must keep their original durable IDs (no rekey).
    db = SessionLocal()
    try:
        rows = {
            r.id: r
            for r in db.query(cdb.ChatMessage).filter(cdb.ChatMessage.session_id == session_id)
        }
    finally:
        db.close()
    for mid in original_ids[-2:]:
        assert mid in rows, f"retained message {mid} was rekeyed or deleted"

    # 2) Dropped originals must still be retrievable from the source archive.
    archive = ConversationArchive(db_path=str(archive_path))
    for mid in original_ids:
        got = archive.get_part(
            owner="alice", chat_id=session_id, message_id=mid, part_id="body"
        )
        assert got["ok"] is True, f"source part for {mid} disappeared after compaction"
        assert got["part"]["text"] in {"turn one", "turn two", "turn three"}

    # 3) The projection is recorded as an active compaction projection.
    projections = archive.list_compaction_projections(owner="alice", chat_id=session_id)
    assert projections, "compaction projection was not recorded"


def test_replace_messages_reservation_failure_leaves_source_and_ids(manager_db):
    """Failed attachment reservation must not delete/rekey durable history."""
    manager, SessionLocal, engine, archive_path = manager_db
    session_id = _seed_session(SessionLocal)
    original_id = "orig-stable-1"
    _seed_messages(
        SessionLocal,
        session_id,
        [(original_id, "user", "keep this", {"source": "before"})],
    )
    session = manager.get_session(session_id)
    incoming = [
        ChatMessage(
            role="user",
            content="new",
            metadata={"attachments": [{"id": "missing-upload"}]},
        )
    ]

    def _fail_reserve(handler, owner, content, metadata):
        return "missing-upload"

    original_reserve = session_manager_module.reserve_message_upload_references
    session_manager_module.reserve_message_upload_references = _fail_reserve
    try:
        assert manager.replace_messages(session_id, incoming) is False
    finally:
        session_manager_module.reserve_message_upload_references = original_reserve

    db = SessionLocal()
    try:
        rows = [r.id for r in db.query(cdb.ChatMessage).filter(cdb.ChatMessage.session_id == session_id)]
    finally:
        db.close()
    assert rows == [original_id]


# ---------------------------------------------------------------------------
# 4. one active compactor per execution context
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_persistent_acp_sessions_skip_host_pre_dispatch_compaction(monkeypatch):
    """MiMo is the single active compactor for ACP persistent sessions."""
    called = {"complete": 0}

    async def _complete_text(**kwargs):
        called["complete"] += 1
        return "should not run"

    monkeypatch.setattr(
        "src.context_compactor._complete_text", _complete_text, raising=False
    )
    monkeypatch.setattr(
        "src.context_compactor.get_context_length", lambda *_a, **_k: 100
    )

    class _Sess:
        owner = "alice"
        history = []

    messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "a " * 200},
        {"role": "assistant", "content": "b " * 200},
        {"role": "user", "content": "c " * 200},
        {"role": "assistant", "content": "d " * 200},
    ]
    out, ctx_len, was_compacted = await maybe_compact(
        _Sess(), "http://x", "m", messages, has_persistent_engine=True
    )
    assert was_compacted is False
    assert out is messages
    assert called["complete"] == 0


@pytest.mark.asyncio
async def test_finite_contexts_keep_host_compactor(monkeypatch):
    class _Sess:
        owner = "alice"
        id = None
        history = []

    async def _fake_summary(**_kwargs):
        return "a short summary"

    monkeypatch.setattr("src.context_compactor.get_context_length", lambda *_a, **_k: 50)
    monkeypatch.setattr("src.context_compactor._complete_text", _fake_summary)

    messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "a " * 400},
        {"role": "assistant", "content": "b " * 400},
        {"role": "user", "content": "c " * 400},
        {"role": "assistant", "content": "d " * 400},
    ]
    out, _ctx, was_compacted = await maybe_compact(
        _Sess(), "http://x", "m", messages, has_persistent_engine=False
    )
    # Finite host contexts still compact (projection writer).
    assert was_compacted is True
    assert any("Conversation summary" in (m.get("content") or "") for m in out)


# ---------------------------------------------------------------------------
# 5. full-part history / media parity + bounds + owner scope
# ---------------------------------------------------------------------------


def test_history_get_returns_full_part_not_fts_preview(archive):
    long_text = "x" * 5000
    archive.append_parts(
        [{
            "owner": "alice", "chat_id": "chat-1", "actor_id": "main",
            "message_id": "msg-1", "part_id": "body", "revision": 1,
            "role": "user", "part_type": "text", "content": long_text,
        }]
    )
    # Search may only show a snippet; get must return the canonical body.
    hits = archive.search(owner="alice", chat_id="chat-1", query="xxxx")
    assert hits["ok"] is True
    assert all(len(h.get("snippet", "")) <= 240 for h in hits["hits"])

    got = archive.get_part(
        owner="alice", chat_id="chat-1", message_id="msg-1", part_id="body"
    )
    assert got["ok"] is True
    # Body is clamped to the target get budget, and has_more/page cursor work.
    assert len(got["part"]["text"]) <= GET_LENGTH_MAX_UTF16
    if got["has_more"]:
        assert got["next_offset"] is not None
        page2 = archive.get_part(
            owner="alice", chat_id="chat-1", message_id="msg-1", part_id="body",
            offset=got["next_offset"],
        )
        assert page2["ok"] is True
        assert page2["part"]["text"] != got["part"]["text"]


def test_unicode_page_boundaries_never_split_surrogates(archive):
    # Astral-plane + BMP mix to exercise UTF-16 surrogate-safe paging.
    text = "\U0001f600" * 5000 + "é" * 5000
    archive.append_parts(
        [{
            "owner": "alice", "chat_id": "chat-1", "actor_id": "main",
            "message_id": "msg-1", "part_id": "body", "revision": 1,
            "role": "user", "part_type": "text", "content": text,
        }]
    )
    got = archive.get_part(
        owner="alice", chat_id="chat-1", message_id="msg-1", part_id="body",
        length=100,
    )
    assert got["ok"] is True
    visible = got["part"]["text"]
    # A lone surrogate would appear as a replacement char after a bad cut.
    assert "\ud800" not in visible
    assert "\udfff" not in visible
    assert visible  # non-empty
    if got["has_more"]:
        assert got["next_offset"] > 0


def test_search_limit_is_bounded_and_global_scope_is_owner_scoped(archive):
    for i in range(60):
        archive.append_parts(
            [{
                "owner": "alice", "chat_id": "chat-1", "actor_id": "main",
                "message_id": f"msg-{i}", "part_id": "body", "revision": 1,
                "role": "user", "part_type": "text", "content": f"needle {i}",
            }]
        )
    hits = archive.search(owner="alice", chat_id="chat-1", query="needle", limit=500)
    assert hits["ok"] is True
    assert len(hits["hits"]) == SEARCH_LIMIT_MAX

    # Same-owner global is allowed.
    global_hits = archive.search(owner="alice", query="needle", scope="global")
    assert global_hits["ok"] is True
    assert global_hits["hits"]

    # Another owner cannot see these hits.
    other = archive.search(owner="bob", query="needle", scope="global")
    assert other["ok"] is True
    assert other["hits"] == []

    # Guessed IDs fail closed through around/get.
    around = archive.around(
        owner="bob", chat_id="chat-1", anchor_message_id="msg-0"
    )
    assert around["ok"] is False
    assert around["error"] == "anchor_not_found"

    got = archive.get_part(
        owner="bob", chat_id="chat-1", message_id="msg-0", part_id="body"
    )
    assert got["ok"] is False
    assert got["error"] == "not_found"


def test_media_returns_owned_asset_and_refuses_historical_file_urls(archive):
    archive.append_parts(
        [{
            "owner": "alice", "chat_id": "chat-1", "actor_id": "main",
            "message_id": "msg-1", "part_id": "body", "revision": 1,
            "role": "user", "part_type": "text", "content": "see attached",
            "assets": [
                {
                    "asset_id": "asset-1",
                    "content_hash": "deadbeef",
                    "mime_type": "text/plain",
                    "byte_size": 4,
                    "provenance": {"source": "host_upload", "path": "file:///old/path.txt"},
                }
            ],
        }]
    )
    media = archive.get_media(owner="alice", asset_id="asset-1")
    assert media["ok"] is True
    assert media["asset"]["asset_id"] == "asset-1"
    assert media["asset"]["content_hash"] == "deadbeef"
    # Historical file:// is a reference only — never auto-dereferenced.
    assert media["asset"]["locator"] is None
    assert "file://" in media["warning"]

    # Missing asset is an explicit recoverable result, not a crash.
    missing = archive.get_media(owner="alice", asset_id="nope")
    assert missing["ok"] is False
    assert missing["error"] == "asset_not_found"
    assert missing["recoverable"] is True

    # Cross-owner guessed asset fails closed.
    stolen = archive.get_media(owner="bob", asset_id="asset-1")
    assert stolen["ok"] is False


def test_around_bounds_and_stable_chronological_order(archive):
    for i in range(20):
        archive.append_parts(
            [{
                "owner": "alice", "chat_id": "chat-1", "actor_id": "main",
                "message_id": f"msg-{i}", "part_id": "body", "revision": 1,
                "event_sequence": i,
                "role": "user" if i % 2 == 0 else "assistant",
                "part_type": "text", "content": f"line {i}",
                "time_created": 1000 + i,
            }]
        )
    around = archive.around(
        owner="alice", chat_id="chat-1", anchor_message_id="msg-10",
        before=3, after=3,
    )
    assert around["ok"] is True
    assert around["before"] <= AROUND_AFTER_MAX
    assert around["after"] <= AROUND_AFTER_MAX
    ids = [m["message_id"] for m in around["messages"]]
    assert "msg-10" in ids
    # Stable chronological order.
    assert ids == sorted(ids, key=lambda mid: int(mid.split("-")[1]))


def test_broker_adapter_scopes_and_unavailable_are_recoverable(archive, monkeypatch):
    adapter = HistoryBrokerAdapter(archive=archive)
    binding = {"owner": "alice", "chat_id": "chat-1"}
    archive.append_parts(
        [{
            "owner": "alice", "chat_id": "chat-1", "actor_id": "main",
            "message_id": "msg-1", "part_id": "body", "revision": 1,
            "role": "user", "part_type": "text", "content": "scoped content",
        }]
    )
    found = adapter.history_search(binding, query="scoped")
    assert found["ok"] is True
    assert found["hits"]

    # Missing owner fails closed with a recoverable/unavailable shape.
    denied = adapter.history_search({"owner": "", "chat_id": "chat-1"}, query="scoped")
    assert denied["ok"] is False
    assert denied.get("error") == "owner_required"

    # Shared-service failure returns explicit recoverable unavailable; no
    # unscoped/native fallback payload is produced.
    monkeypatch.setattr(
        type(archive),
        "search",
        lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("broker down")),
    )
    down = adapter.history_search(binding, query="scoped")
    assert down["ok"] is False
    assert down["error"] == "archive_unavailable"
    assert down["recoverable"] is True


def test_two_owners_colliding_ids_stay_isolated(archive):
    for owner in ("alice", "bob"):
        archive.append_parts(
            [{
                "owner": owner, "chat_id": "shared-chat-id", "actor_id": "main",
                "message_id": "colliding-msg", "part_id": "body", "revision": 1,
                "role": "user", "part_type": "text", "content": f"secret of {owner}",
            }]
        )
    alice = archive.get_part(
        owner="alice", chat_id="shared-chat-id", message_id="colliding-msg", part_id="body"
    )
    bob = archive.get_part(
        owner="bob", chat_id="shared-chat-id", message_id="colliding-msg", part_id="body"
    )
    assert alice["ok"] and bob["ok"]
    assert "alice" in alice["part"]["text"]
    assert "bob" in bob["part"]["text"]
    assert alice["part"]["content_hash"] != bob["part"]["content_hash"]

    # Aliases are owner-scoped too.
    archive.append_parts(
        [{
            "owner": "alice", "chat_id": "shared-chat-id", "actor_id": "main",
            "message_id": "colliding-msg", "part_id": "body", "revision": 2,
            "role": "user", "part_type": "text", "content": "alice v2",
            "aliases": (("host_message", "legacy-1"),),
        }]
    )
    assert archive.resolve_alias(owner="alice", alias_kind="host_message", alias_id="legacy-1")
    assert archive.resolve_alias(owner="bob", alias_kind="host_message", alias_id="legacy-1") is None


def test_compaction_projection_preserves_source_parts(archive):
    # Source parts first.
    for i in range(6):
        archive.append_parts(
            [{
                "owner": "alice", "chat_id": "chat-1", "actor_id": "main",
                "message_id": f"msg-{i}", "part_id": "body", "revision": 1,
                "role": "user", "part_type": "text", "content": f"history {i}",
            }]
        )
    archive.record_compaction_projection(
        owner="alice",
        chat_id="chat-1",
        summary_id="summary-1",
        projection_revision=1,
        summary_text="summary of history 0-2",
        included_message_ids=["msg-0", "msg-1", "msg-2"],
        retained_message_ids=["msg-3", "msg-4", "msg-5"],
        trigger_kind="manual",
    )
    # After every successful or failed compaction, all retained original
    # source parts remain retrievable with stable identity.
    for i in range(6):
        got = archive.get_part(
            owner="alice", chat_id="chat-1", message_id=f"msg-{i}", part_id="body"
        )
        assert got["ok"] is True
        assert got["part"]["text"] == f"history {i}"
    projections = archive.list_compaction_projections(owner="alice", chat_id="chat-1")
    assert len(projections) == 1
    assert projections[0]["trigger_kind"] == "manual"


def test_old_id_aliases_preserved(archive):
    archive.append_parts(
        [{
            "owner": "alice", "chat_id": "chat-1", "actor_id": "main",
            "message_id": "new-msg-1", "part_id": "body", "revision": 1,
            "role": "user", "part_type": "text", "content": "aliased",
            "aliases": (
                ("host_message", "legacy-host-9"),
                ("engine_part", "eng-part-3"),
            ),
        }]
    )
    host = archive.resolve_alias(owner="alice", alias_kind="host_message", alias_id="legacy-host-9")
    engine = archive.resolve_alias(owner="alice", alias_kind="engine_part", alias_id="eng-part-3")
    assert host["message_id"] == "new-msg-1"
    assert engine["message_id"] == "new-msg-1"
    assert engine["part_id"] == "body"


# ---------------------------------------------------------------------------
# 8. Cross-review blockers (1900bc20): archive gate, ACP manual refusal,
#    tool-part capture on the replace_messages archive path.
# ---------------------------------------------------------------------------


def test_replace_messages_archive_failure_blocks_delete(manager_db):
    """Archive append failure must abort the mutation and leave source intact."""
    from src.openclank.conversation_archive import ArchiveUnavailableError

    manager, SessionLocal, engine, archive_path = manager_db
    session_id = _seed_session(SessionLocal)
    original_id = "orig-stable-archive-fail"
    _seed_messages(
        SessionLocal,
        session_id,
        [(original_id, "user", "keep this", {"source": "before"})],
    )
    session = manager.get_session(session_id)
    incoming = [
        ChatMessage(role="system", content="[Conversation summary]\nsummary", metadata={"compacted": True})
    ]

    def _fail_append(parts, **kwargs):
        raise RuntimeError("archive db down")

    archive = get_conversation_archive()
    original_append = archive.append_parts
    archive.append_parts = _fail_append
    try:
        with pytest.raises(ArchiveUnavailableError):
            manager.replace_messages(session_id, incoming)
    finally:
        archive.append_parts = original_append

    db = SessionLocal()
    try:
        rows = [r.id for r in db.query(cdb.ChatMessage).filter(cdb.ChatMessage.session_id == session_id)]
    finally:
        db.close()
    assert rows == [original_id], "projection delete ran despite archive failure"
    assert [m for m in session.history if getattr(m, "metadata", {}) and m.metadata.get("_db_id") == original_id]


def test_replace_messages_tombstone_failure_blocks_delete(manager_db):
    """Tombstone failure must abort the mutation and leave source intact."""
    from src.openclank.conversation_archive import ArchiveUnavailableError

    manager, SessionLocal, engine, archive_path = manager_db
    session_id = _seed_session(SessionLocal)
    original_id = "orig-stable-tombstone-fail"
    _seed_messages(
        SessionLocal,
        session_id,
        [(original_id, "user", "keep this", {"source": "before"})],
    )
    session = manager.get_session(session_id)
    incoming = [
        ChatMessage(role="system", content="[Conversation summary]\nsummary", metadata={"compacted": True})
    ]

    def _fail_tombstone(**kwargs):
        raise RuntimeError("tombstone store down")

    archive = get_conversation_archive()
    original_tombstone = archive.tombstone_part
    archive.tombstone_part = _fail_tombstone
    try:
        with pytest.raises(ArchiveUnavailableError):
            manager.replace_messages(session_id, incoming)
    finally:
        archive.tombstone_part = original_tombstone

    db = SessionLocal()
    try:
        rows = [r.id for r in db.query(cdb.ChatMessage).filter(cdb.ChatMessage.session_id == session_id)]
    finally:
        db.close()
    assert rows == [original_id], "projection delete ran despite tombstone failure"


def test_replace_messages_archives_tool_parts_not_just_body(manager_db):
    """replace_messages archive path must capture tool_calls/tool_results."""
    manager, SessionLocal, engine, archive_path = manager_db
    session_id = _seed_session(SessionLocal)
    original_id = f"orig-tools-{uuid.uuid4().hex}"
    _seed_messages(
        SessionLocal,
        session_id,
        [
            (
                original_id,
                "assistant",
                "calling tools",
                {
                    "actor_id": "worker-1",
                    "tool_calls": [{"name": "read", "id": "c1"}],
                    "tool_results": [{"tool_call_id": "c1", "output": "file contents"}],
                    "attachments": [{"attachment_id": "att-1", "name": "a.png", "mime": "image/png"}],
                },
            ),
        ],
    )
    session = manager.get_session(session_id)
    incoming = [
        ChatMessage(role="system", content="[Conversation summary]\nsummary", metadata={"compacted": True})
    ]
    assert manager.replace_messages(session_id, incoming) is True

    archive = ConversationArchive(db_path=str(archive_path))
    for part_id, expected in (
        ("body", "calling tools"),
        ("tool_call:0", {"name": "read", "id": "c1"}),
        ("tool_result:0", {"tool_call_id": "c1", "output": "file contents"}),
    ):
        got = archive.get_part(
            owner="alice",
            chat_id=session_id,
            actor_id="worker-1",
            message_id=original_id,
            part_id=part_id,
        )
        assert got["ok"] is True, f"source part {part_id} missing after replace_messages"
        if isinstance(expected, str):
            assert got["part"]["text"] == expected
    # Assets ride the row as asset refs.
    got_assets = archive.get_part(
        owner="alice",
        chat_id=session_id,
        actor_id="worker-1",
        message_id=original_id,
        part_id="asset:0",
    )
    assert got_assets["ok"] is True
    # Tombstone used the row's real actor_id, not hardcoded "main".
    got_body = archive.get_part(
        owner="alice",
        chat_id=session_id,
        actor_id="worker-1",
        message_id=original_id,
        part_id="body",
    )
    assert got_body["part"]["tombstone"] is True
    assert got_body["part"]["actor_id"] == "worker-1"


def test_manual_compact_routes_refuse_persistent_acp(monkeypatch):
    """Both host manual compact routes must 409 when MiMo owns compaction."""
    from types import SimpleNamespace

    from fastapi import APIRouter, FastAPI
    from fastapi.testclient import TestClient

    import routes.history.history_routes as history_routes
    import routes.session_routes as session_routes
    from src.context_compactor import session_has_persistent_engine

    class _FakeQuery:
        def filter(self, *a, **k):
            return self

        def first(self):
            return SimpleNamespace(message_count=0, updated_at=None)

    class _FakeDb:
        def query(self, model):
            return _FakeQuery()

        def close(self):
            pass

    class _FakeManager:
        def __init__(self, session):
            self.session = session

        def get_session(self, session_id):
            return self.session

        def replace_messages(self, session_id, messages):  # pragma: no cover - must not run
            raise AssertionError("host compact must not rewrite ACP sessions")

        def save_sessions(self):  # pragma: no cover
            pass

    acp_session = SimpleNamespace(
        id="session-acp",
        name="ACP",
        endpoint_url="mimo://acp",
        model="conn/model",
        headers={},
        owner="alice",
        history=[ChatMessage(role="user", content=f"m{i}") for i in range(8)],
        message_count=8,
        get_context_messages=lambda: [{"role": "user", "content": "m"}] * 8,
    )
    assert session_has_persistent_engine(acp_session) is True

    finite_session = SimpleNamespace(
        id="session-finite",
        name="Finite",
        endpoint_url="http://localhost:11434",
        model="llama",
        headers={},
        owner="alice",
        history=[ChatMessage(role="user", content=f"m{i}") for i in range(8)],
        message_count=8,
        get_context_messages=lambda: [{"role": "user", "content": "m"}] * 8,
    )
    assert session_has_persistent_engine(finite_session) is False

    manager = _FakeManager(acp_session)
    monkeypatch.setattr(
        session_routes,
        "router",
        APIRouter(prefix="/api", tags=["sessions"]),
    )
    monkeypatch.setattr(session_routes, "_verify_session_owner", lambda request, session_id: None)
    monkeypatch.setattr(history_routes, "_verify_session_owner", lambda request, session_id: None)
    monkeypatch.setattr(history_routes, "SessionLocal", lambda: _FakeDb())
    monkeypatch.setattr(session_routes, "SessionLocal", lambda: _FakeDb())
    import src.agent_runs as agent_runs

    monkeypatch.setattr(agent_runs, "is_active", lambda session_id: False)

    # Each host compact route is registered alone so both are actually hit
    # (they share the /api/session/{id}/compact path).
    for label, include in (
        ("session_routes", lambda app: app.include_router(session_routes.setup_session_routes(manager, {}))),
        ("history_routes", lambda app: app.include_router(history_routes.setup_history_routes(manager))),
    ):
        app = FastAPI()
        include(app)
        response = TestClient(app).post("/api/session/session-acp/compact")
        assert response.status_code == 409, f"{label} must refuse ACP compact"
        assert "Persistent ACP" in response.text, label
        assert len(manager.session.history) == 8, label
