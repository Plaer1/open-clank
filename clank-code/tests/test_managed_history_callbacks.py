import asyncio
from concurrent.futures import ThreadPoolExecutor
import threading

import pytest

from src.openclank.conversation_archive import ConversationArchive
from src.openclank.managed_history import MUTATE, QUERY, ManagedHistoryCallbacks
from src.openclank.history_broker_adapter import HistoryBrokerAdapter
from src.openclank.conversation_archive import SourcePart


def run(value):
    return asyncio.run(value)


def test_managed_history_derives_owner_chat_replays_and_rejects_cross_owner(tmp_path):
    archive = ConversationArchive(str(tmp_path / "archive.db"))
    callbacks = ManagedHistoryCallbacks(archive, lambda session: ("alice", "chat-a") if session == "ses-a" else ("bob", "chat-b"))
    event = {"messageID": "m1", "partID": "p1", "role": "user", "partType": "text", "content": {"text": "alice private"}}
    first = run(callbacks.dispatch(MUTATE, {"sessionID": "ses-a", "operation": "upsert", "events": [event]}))
    replay = run(callbacks.dispatch(MUTATE, {"sessionID": "ses-a", "operation": "replay", "events": [event]}))
    archive.append_parts([SourcePart(owner="alice", chat_id="older-chat", actor_id="main", message_id="older", part_id="p", content={"text": "alice legacy"})])
    assert (first["accepted"], replay["duplicate"]) == (1, 1)
    global_hits = run(callbacks.dispatch(QUERY, {"sessionID": "ses-a", "operation": "search", "scope": "global", "query": "alice legacy"}))["result"]["hits"]
    assert [(hit["session_id"], hit["kind"], hit["tool_name"], hit["score"]) for hit in global_hits] == [("older-chat", "user_text", None, 1.0)]
    assert run(callbacks.dispatch(QUERY, {"sessionID": "ses-b", "operation": "search", "query": "alice private"}))["result"]["hits"] == []
    assert run(callbacks.dispatch(QUERY, {"sessionID": "ses-a", "operation": "get", "messageID": "m1", "partID": "p1"}))["ok"]


def test_explicit_legacy_chat_is_same_owner_only_and_mutation_has_no_chat_override(tmp_path):
    archive = ConversationArchive(str(tmp_path / "archive.db"))
    callbacks = ManagedHistoryCallbacks(archive, lambda session: ("alice", "current") if session == "ses-a" else ("bob", "current-b"))
    archive.append_parts([SourcePart(owner="alice", chat_id="legacy", actor_id="main", message_id="legacy-m", part_id="legacy-p", content={"text": "legacy"}, assets=({"asset_id": "legacy-asset", "mime_type": "image/png", "byte_size": 1, "provenance": {}},))])
    run(callbacks.dispatch(MUTATE, {"sessionID": "ses-a", "operation": "upsert", "events": [{"messageID": "m", "partID": "p", "role": "user", "partType": "text", "content": {"owner": "bob", "chatID": "forged", "text": "x"}}]}))
    assert run(callbacks.dispatch(QUERY, {"sessionID": "ses-a", "operation": "get", "chatID": "current", "messageID": "m", "partID": "p"}))["ok"]
    assert run(callbacks.dispatch(QUERY, {"sessionID": "ses-a", "operation": "get", "chatID": "legacy", "messageID": "legacy-m", "partID": "legacy-p"}))["ok"]
    assert run(callbacks.dispatch(QUERY, {"sessionID": "ses-a", "operation": "around", "chatID": "legacy", "messageID": "legacy-m"}))["result"]["session_id"] == "legacy"
    assert run(callbacks.dispatch(QUERY, {"sessionID": "ses-a", "operation": "media", "chatID": "legacy", "assetID": "legacy-asset", "messageID": "legacy-m", "partID": "legacy-p"}))["result"]["attachments"][0]["asset_id"] == "legacy-asset"
    assert not run(callbacks.dispatch(QUERY, {"sessionID": "ses-b", "operation": "get", "chatID": "current", "messageID": "m", "partID": "p"}))["ok"]
    assert not run(callbacks.dispatch(QUERY, {"sessionID": "ses-b", "operation": "media", "chatID": "legacy", "assetID": "legacy-asset", "messageID": "legacy-m", "partID": "legacy-p"}))["ok"]
    assert archive.get_part(owner="alice", chat_id="current", message_id="m", part_id="p")["ok"]
    assert not archive.get_part(owner="bob", chat_id="forged", message_id="m", part_id="p")["ok"]
    with pytest.raises(Exception):
        run(callbacks.dispatch(MUTATE, {"sessionID": "ses-a", "owner": "bob", "operation": "upsert", "events": [{"messageID": "m2", "partID": "p2", "role": "user", "partType": "text", "content": "x"}]}))


def test_managed_history_media_and_tombstone_replay_survive_callback_restart(tmp_path):
    archive = ConversationArchive(str(tmp_path / "archive.db"))
    binding = lambda _session: ("alice", "chat-a")
    callbacks = ManagedHistoryCallbacks(archive, binding)
    event = {
        "messageID": "m1",
        "partID": "p1",
        "revision": 7,
        "role": "assistant",
        "partType": "file",
        "content": {"text": "image attachment"},
    }
    first = run(callbacks.dispatch(MUTATE, {"sessionID": "ses-a", "operation": "upsert", "events": [event]}))
    archive.append_parts([SourcePart(
        owner="alice", chat_id="chat-a", actor_id="main", message_id="m1", part_id="p1", revision=8,
        content={"text": "asset"}, assets=({"asset_id": "asset-1", "mime_type": "image/png", "byte_size": 9, "provenance": {}},),
    )])
    media = run(callbacks.dispatch(QUERY, {"sessionID": "ses-a", "operation": "media", "assetID": "asset-1", "messageID": "m1", "partID": "p1"}))
    assert media["result"] == {"ok": True, "attachments": [{"asset_id": "asset-1", "mime_type": "image/png", "filename": None, "byte_size": 9}]}
    restarted = ManagedHistoryCallbacks(ConversationArchive(str(tmp_path / "archive.db")), binding)
    replay = run(restarted.dispatch(MUTATE, {"sessionID": "ses-a", "operation": "replay", "events": [event]}))
    duplicate_replay = run(restarted.dispatch(MUTATE, {"sessionID": "ses-a", "operation": "replay", "events": [event]}))
    tombstone = run(restarted.dispatch(MUTATE, {"sessionID": "ses-a", "operation": "tombstone", "events": [{"messageID": "m1", "partID": "p1"}]}))
    duplicate_tombstone = run(restarted.dispatch(MUTATE, {"sessionID": "ses-a", "operation": "tombstone", "events": [{"messageID": "m1", "partID": "p1"}]}))
    assert (first["accepted"], replay["accepted"], duplicate_replay["duplicate"], tombstone["accepted"], duplicate_tombstone["duplicate"]) == (1, 1, 1, 1, 1)


def test_managed_same_millisecond_updates_are_host_versioned_and_restart_replay_converges(tmp_path):
    archive_path = str(tmp_path / "archive.db")
    callbacks = ManagedHistoryCallbacks(ConversationArchive(archive_path), lambda _session: ("alice", "chat-a"))
    base = {"sessionID": "ses-a", "operation": "upsert"}
    first = run(callbacks.dispatch(MUTATE, {**base, "events": [{"messageID": "m", "partID": "p", "role": "assistant", "partType": "text", "content": {"text": "first"}, "timeUpdated": 100}]}))
    second = run(callbacks.dispatch(MUTATE, {**base, "events": [{"messageID": "m", "partID": "p", "role": "assistant", "partType": "text", "content": {"text": "second"}, "timeUpdated": 100}]}))
    latest = run(callbacks.dispatch(QUERY, {"sessionID": "ses-a", "operation": "get", "messageID": "m", "partID": "p"}))
    restarted = ManagedHistoryCallbacks(ConversationArchive(archive_path), lambda _session: ("alice", "chat-a"))
    replay = run(restarted.dispatch(MUTATE, {"sessionID": "ses-a", "operation": "replay", "events": [{"messageID": "m", "partID": "p", "role": "assistant", "partType": "text", "content": {"text": "second"}, "timeUpdated": 100}]}))
    assert (first["accepted"], second["accepted"], replay["duplicate"]) == (1, 1, 1)
    assert latest["result"]["part"]["text"] == "second"


def test_managed_reappearance_after_tombstone_allocates_a_live_revision(tmp_path):
    archive_path = str(tmp_path / "archive.db")
    callbacks = ManagedHistoryCallbacks(ConversationArchive(archive_path), lambda _session: ("alice", "chat-a"))
    event = {"messageID": "m", "partID": "p", "role": "assistant", "partType": "text", "content": {"text": "again"}}
    assert run(callbacks.dispatch(MUTATE, {"sessionID": "ses-a", "operation": "upsert", "events": [event]}))["accepted"] == 1
    assert run(callbacks.dispatch(MUTATE, {"sessionID": "ses-a", "operation": "tombstone", "events": [{"messageID": "m", "partID": "p"}]}))["accepted"] == 1
    reappeared = run(callbacks.dispatch(MUTATE, {"sessionID": "ses-a", "operation": "upsert", "events": [event]}))
    restarted = ManagedHistoryCallbacks(ConversationArchive(archive_path), lambda _session: ("alice", "chat-a"))
    replay = run(restarted.dispatch(MUTATE, {"sessionID": "ses-a", "operation": "replay", "events": [event]}))
    part = run(restarted.dispatch(QUERY, {"sessionID": "ses-a", "operation": "get", "messageID": "m", "partID": "p"}))
    assert (reappeared["accepted"], replay["duplicate"], part["result"]["part"]["text"]) == (1, 1, "again")
    assert ConversationArchive(archive_path).get_part(owner="alice", chat_id="chat-a", message_id="m", part_id="p")["part"]["revision"] == 2
    metadata_changed = run(restarted.dispatch(MUTATE, {"sessionID": "ses-a", "operation": "upsert", "events": [{**event, "role": "user"}]}))
    newest = ConversationArchive(archive_path).get_part(owner="alice", chat_id="chat-a", message_id="m", part_id="p")["part"]
    assert (metadata_changed["accepted"], newest["revision"], newest["role"]) == (1, 3, "user")


def test_managed_legacy_tool_filters_rendering_and_file_media(tmp_path):
    archive = ConversationArchive(str(tmp_path / "archive.db"))
    callbacks = ManagedHistoryCallbacks(archive, lambda session: ("alice", "current") if session == "ses-a" else ("bob", "current"))
    tool = SourcePart(
        owner="alice", chat_id="legacy", actor_id="main", message_id="tool-m", part_id="tool-p", role="assistant", part_type="tool", time_created=200,
        content={"mimo": {"part": {"type": "tool", "tool": "Bash", "state": {"status": "completed", "input": {"cmd": "echo legacy"}, "output": "done"}}}},
    )
    archive.append_parts([tool, SourcePart(owner="alice", chat_id="legacy", actor_id="main", message_id="old", part_id="old-p", role="assistant", part_type="tool", time_created=100, content={"tool": "Bash", "state": {"status": "error", "input": {}, "error": "legacy old"}})])
    file_event = {"messageID": "file-m", "partID": "file-p", "role": "user", "partType": "file", "content": {"type": "file", "filename": "capture.png", "mime": "image/png", "url": "https://example.test/capture.png"}, "timeCreated": 300}
    assert run(callbacks.dispatch(MUTATE, {"sessionID": "ses-a", "operation": "upsert", "events": [file_event]}))["accepted"] == 1
    filtered = run(callbacks.dispatch(QUERY, {"sessionID": "ses-a", "operation": "search", "scope": "global", "query": "legacy", "kind": ["tool_output"], "toolName": "Bash", "timeAfter": 150, "timeBefore": 250, "limit": 1}))
    assert [(hit["session_id"], hit["kind"], hit["tool_name"]) for hit in filtered["result"]["hits"]] == [("legacy", "tool_output", "Bash")]
    detail = run(callbacks.dispatch(QUERY, {"sessionID": "ses-a", "operation": "get", "chatID": "legacy", "messageID": "tool-m", "partID": "tool-p"}))
    around = run(callbacks.dispatch(QUERY, {"sessionID": "ses-a", "operation": "around", "chatID": "legacy", "messageID": "tool-m"}))
    media = run(callbacks.dispatch(QUERY, {"sessionID": "ses-a", "operation": "media", "assetID": "mimo-file:file-p", "messageID": "file-m", "partID": "file-p"}))
    assert (detail["result"]["part"]["tool_name"], detail["result"]["part"]["text"]) == ("Bash", 'tool: Bash\ninput: {"cmd":"echo legacy"}\noutput: "done"')
    anchor = next(message for message in around["result"]["messages"] if message["message_id"] == "tool-m")
    assert (around["result"]["session_id"], anchor["parts"][0]["tool_name"], anchor["parts"][0]["text"]) == ("legacy", "Bash", 'tool: Bash\ninput: {"cmd":"echo legacy"}\noutput: "done"')
    assert media["result"]["attachments"][0]["asset_id"] == "mimo-file:file-p"
    assert not run(callbacks.dispatch(QUERY, {"sessionID": "ses-b", "operation": "get", "chatID": "legacy", "messageID": "tool-m", "partID": "tool-p"}))["ok"]
    for invalid in ({"limit": 51}, {"kind": ["tool_input"] * 7}, {"timeAfter": -1}):
        with pytest.raises(Exception):
            run(callbacks.dispatch(QUERY, {"sessionID": "ses-a", "operation": "search", "query": "legacy", **invalid}))


def test_managed_revision_reversion_and_concurrent_updates_are_lossless(tmp_path):
    archive_path = str(tmp_path / "archive.db")
    archive = ConversationArchive(archive_path)
    base = dict(owner="alice", chat_id="chat-a", actor_id="main", message_id="m", part_id="p")
    for text in ("A", "B", "A"):
        result = archive.append_managed_parts([SourcePart(**base, content={"text": text}, time_updated=100)])
        assert result["accepted"] == 1
    assert archive.get_part(**base)["part"]["text"] == "A"

    barrier = threading.Barrier(8)

    def append(index):
        worker = ConversationArchive(archive_path)
        barrier.wait()
        return worker.append_managed_parts([SourcePart(**base, content={"text": f"concurrent-{index}"}, time_updated=100)])

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(append, range(8)))
    assert [result["accepted"] for result in results] == [1] * 8
    with archive._connect() as conn:
        revisions = conn.execute(
            "SELECT revision, content_hash FROM conversation_parts WHERE owner=? AND chat_id=? AND actor_id=? AND message_id=? AND part_id=? ORDER BY revision",
            tuple(base.values()),
        ).fetchall()
    assert [row["revision"] for row in revisions] == list(range(1, 12))
    assert len({row["content_hash"] for row in revisions}) == 10


def test_shared_adapter_global_is_owner_wide_and_append_rejects_scope_injection(tmp_path):
    archive = ConversationArchive(str(tmp_path / "archive.db"))
    adapter = HistoryBrokerAdapter(archive)
    archive.append_parts([SourcePart(owner="alice", chat_id="old-chat", actor_id="main", message_id="m", part_id="p", content={"text": "legacy"})])
    assert adapter.history_search({"owner": "alice", "chat_id": "current"}, query="legacy", scope="global")["hits"]
    rejected = adapter.history_append_parts({"owner": "alice", "chat_id": "current"}, [SourcePart(owner="bob", chat_id="other", actor_id="main", message_id="m", part_id="p")])
    assert rejected["error"] == "archive_unavailable"
