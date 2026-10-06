"""Focused D09 proof for imported unpartitioned MiMo transcript state."""

from __future__ import annotations

import json
import sqlite3

import pytest

from src.openclank.conversation_archive import ConversationArchive
from src.openclank.mimo_history_migration import (
    OWNERLESS_SOURCE,
    MimoHistoryMigrationError,
    apply_migration,
    plan_migration,
    snapshot_source,
)


def _seed_mimo(path, *, include_file=False, include_real_shape_empty_envelopes=False):
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE session (id TEXT PRIMARY KEY, project_id TEXT NOT NULL, workspace_id TEXT,
          title TEXT NOT NULL, time_created INTEGER NOT NULL, time_updated INTEGER NOT NULL);
        CREATE TABLE message (id TEXT PRIMARY KEY, session_id TEXT NOT NULL, agent_id TEXT NOT NULL,
          time_created INTEGER NOT NULL, time_updated INTEGER NOT NULL, data TEXT NOT NULL);
        CREATE TABLE part (id TEXT PRIMARY KEY, message_id TEXT NOT NULL, session_id TEXT NOT NULL,
          time_created INTEGER NOT NULL, time_updated INTEGER NOT NULL, data TEXT NOT NULL);
        CREATE TABLE history_fts (part_id TEXT PRIMARY KEY, session_id TEXT NOT NULL, message_id TEXT NOT NULL,
          project_id TEXT NOT NULL, kind TEXT NOT NULL, body TEXT NOT NULL, time_created INTEGER NOT NULL);
        """
    )
    conn.execute("INSERT INTO session VALUES (?,?,?,?,?,?)", ("chat-1", "project-1", "workspace-1", "old chat", 10, 20))
    conn.execute("INSERT INTO message VALUES (?,?,?,?,?,?)", ("msg-user", "chat-1", "main", 11, 12, json.dumps({"role": "user"})))
    conn.execute("INSERT INTO message VALUES (?,?,?,?,?,?)", ("msg-assistant", "chat-1", "main", 13, 14, json.dumps({"role": "assistant", "modelID": "old/model"})))
    parts = [
        ("part-user", "msg-user", "chat-1", 11, 12, {"type": "text", "text": "private owner e text"}),
        ("part-tool", "msg-assistant", "chat-1", 13, 14, {"type": "tool", "tool": "shell", "state": {"status": "completed", "output": "proof"}}),
    ]
    if include_file:
        parts.append(("part-file", "msg-assistant", "chat-1", 15, 16, {"type": "file", "mime": "image/png", "url": "data:image/png;base64,aGVsbG8="}))
    for pid, mid, sid, created, updated, data in parts:
        conn.execute("INSERT INTO part VALUES (?,?,?,?,?,?)", (pid, mid, sid, created, updated, json.dumps(data)))
    if include_real_shape_empty_envelopes:
        # The real source has 17 sessions with no parts and two messages with
        # no parts. Keep the distinctive sparse shape without copying live data.
        for index in range(17):
            conn.execute(
                "INSERT INTO session VALUES (?,?,?,?,?,?)",
                (f"empty-chat-{index}", "project-1", "workspace-1", "empty chat", 30 + index, 30 + index),
            )
        for index in range(2):
            conn.execute(
                "INSERT INTO message VALUES (?,?,?,?,?,?)",
                (f"empty-message-{index}", "chat-1", "main", 50 + index, 50 + index, json.dumps({"role": "assistant"})),
            )
    # Derived history intentionally includes an orphan that must never be imported.
    conn.execute("INSERT INTO history_fts VALUES (?,?,?,?,?,?,?)", ("orphan-history", "chat-1", "gone", "project-1", "tool_input", "not canonical", 1))
    conn.commit()
    conn.close()


def test_snapshot_plan_apply_restart_and_owner_isolation(tmp_path):
    live = tmp_path / "live-mimocode.db"
    copy = tmp_path / "copied-mimocode.db"
    _seed_mimo(live, include_file=True)
    snapshot_source(live, copy)
    frozen = plan_migration(
        copy,
        owner_mapping={OWNERLESS_SOURCE: "e"},
        allowed_owners=("allie", "e", "mom"),
    )
    assert frozen.parts == 3
    archive = ConversationArchive(str(tmp_path / "conversation_archive.sqlite3"))
    first = apply_migration(copy, archive, plan=frozen, batch_size=1)
    assert first["accepted"] == 3 and first["duplicate"] == 0
    # A freshly constructed archive object models service restart.
    second = apply_migration(copy, ConversationArchive(str(tmp_path / "conversation_archive.sqlite3")), plan=frozen)
    assert second["accepted"] == 0 and second["duplicate"] == 3
    assert archive.search(owner="e", chat_id="chat-1", query="private owner e text")["hits"]
    assert archive.search(owner="allie", chat_id="chat-1", query="private owner e text")["hits"] == []
    assert archive.search(owner="mom", chat_id="chat-1", query="private owner e text")["hits"] == []
    assert archive.search(owner="future", chat_id="chat-1", query="private owner e text")["hits"] == []
    detail = archive.get_part(owner="e", chat_id="chat-1", message_id="msg-assistant", part_id="part-file")
    assert detail["ok"] is True
    assert detail["part"]["assets"][0]["asset_id"] == "mimo-file:part-file"
    assert archive.get_media(owner="mom", asset_id="mimo-file:part-file")["error"] == "asset_not_found"
    assert archive.get_media(owner="e", asset_id="mimo-file:part-file")["ok"] is True
    with sqlite3.connect(tmp_path / "conversation_archive.sqlite3") as conn:
        assert conn.execute("SELECT count(*) FROM conversation_parts WHERE owner='e'").fetchone()[0] == 3
        assert conn.execute("SELECT count(*) FROM conversation_parts WHERE part_id='orphan-history'").fetchone()[0] == 0


def test_plan_requires_explicit_ownerless_mapping_and_allowed_owner(tmp_path):
    source = tmp_path / "copied-mimocode.db"
    _seed_mimo(source)
    with pytest.raises(MimoHistoryMigrationError, match="owner_mapping"):
        plan_migration(source, owner_mapping=None, allowed_owners=("e",))
    with pytest.raises(MimoHistoryMigrationError, match="not in allowed_owners"):
        plan_migration(source, owner_mapping={OWNERLESS_SOURCE: "e"}, allowed_owners=("allie",))


def test_real_shape_empty_session_and_message_envelopes_are_durably_accounted(tmp_path):
    source = tmp_path / "copied-mimocode.db"
    _seed_mimo(source, include_real_shape_empty_envelopes=True)
    frozen = plan_migration(source, owner_mapping={OWNERLESS_SOURCE: "e"}, allowed_owners=("allie", "e", "mom"))
    assert (frozen.sessions, frozen.messages, frozen.parts) == (18, 4, 2)
    assert (frozen.empty_sessions, frozen.empty_messages) == (17, 2)

    archive_path = tmp_path / "archive.db"
    first = apply_migration(source, ConversationArchive(str(archive_path)), plan=frozen)
    second = apply_migration(source, ConversationArchive(str(archive_path)), plan=frozen)

    assert (first["accepted_sessions"], first["accepted_messages"]) == (17, 2)
    assert (second["duplicate_sessions"], second["duplicate_messages"]) == (17, 2)
    with sqlite3.connect(archive_path) as conn:
        rows = conn.execute(
            "SELECT record_kind, source_id, chat_id, payload_json FROM conversation_archive_migration_envelopes WHERE owner='e' ORDER BY record_kind, source_id"
        ).fetchall()
        assert len(rows) == 19
        assert (rows[0][0], rows[0][2]) == ("message", "chat-1")
        assert json.loads(rows[0][3])["id"].startswith("empty-message-")
        assert conn.execute("SELECT count(*) FROM conversation_parts WHERE chat_id LIKE 'empty-chat-%'").fetchone()[0] == 0


def test_apply_rejects_source_copy_that_changed_after_plan(tmp_path):
    source = tmp_path / "copied-mimocode.db"
    _seed_mimo(source)
    frozen = plan_migration(source, owner_mapping={OWNERLESS_SOURCE: "e"}, allowed_owners=("e",))
    with sqlite3.connect(source) as conn:
        conn.execute("UPDATE part SET data=? WHERE id='part-user'", (json.dumps({"type": "text", "text": "changed"}),))
        conn.commit()
    with pytest.raises(MimoHistoryMigrationError, match="changed after planning"):
        apply_migration(source, ConversationArchive(str(tmp_path / "archive.db")), plan=frozen)
