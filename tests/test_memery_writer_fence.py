"""Synthetic checks for inert, connection-local writer registration."""
import sqlite3
from pathlib import Path

import pytest

from src.memery_v2 import _AttachmentConnection
from src.memery_writer_fence import (
    RegistrationStatus,
    WRITER_CAPABILITY,
    connect_writer,
    register_writer_capability,
)


def test_inert_writer_epoch_preserves_factory_and_trusted_schema():
    conn = connect_writer(":memory:", factory=_AttachmentConnection)
    try:
        assert isinstance(conn, _AttachmentConnection)
        assert conn.execute("SELECT fm_writer_capability()").fetchone()[0] == WRITER_CAPABILITY
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 0
        assert not conn.execute("SELECT name FROM sqlite_schema WHERE type='trigger'").fetchall()
        conn.execute("PRAGMA trusted_schema=OFF")
        assert register_writer_capability(conn) is RegistrationStatus.TRUSTED_SCHEMA_DISABLED
        assert conn.execute("PRAGMA trusted_schema").fetchone()[0] == 0
    finally:
        conn.close()


def test_inactive_v17_fence_rejects_old_connections_and_null():
    root = Path(__file__).resolve().parents[1]
    schema = root / "native/memery/crates/memery-core/src/store"
    uri = "file:s03_fence_fixture?mode=memory&cache=shared"
    admin = sqlite3.connect(uri, uri=True)
    old = sqlite3.connect(uri, uri=True)
    try:
        admin.executescript((schema / "current-schema.sql").read_text())
        admin.executescript((schema / "schema-v17-additions.sql").read_text())
        admin.executescript((schema / "schema-v17-authority.sql").read_text())
        admin.executescript((schema / "schema-v17-writer-fence.sql").read_text())
        sql = "INSERT INTO fm_v2_privacy_erasure_context VALUES ('owner','erase',NULL,'owner','test','now')"
        with pytest.raises(sqlite3.OperationalError, match="fm_writer_capability"):
            old.execute(sql)
        old.create_function("fm_writer_capability", 0, lambda: "openclank.frankenmemory.writer.v1")
        with pytest.raises(sqlite3.IntegrityError, match="incompatible memory writer"):
            old.execute(sql)
        old.create_function("fm_writer_capability", 0, lambda: None)
        with pytest.raises(sqlite3.IntegrityError, match="incompatible memory writer"):
            old.execute(sql)
        old.create_function("fm_writer_capability", 0, lambda: "openclank.frankenmemory.writer.v2")
        old.execute(sql)
        assert old.execute("SELECT count(*) FROM fm_v2_privacy_erasure_context").fetchone()[0] == 1
    finally:
        old.close()
        admin.close()


def test_mirror_context_index_caps_only_current_candidates():
    import json
    import re

    root = Path(__file__).resolve().parents[1]
    schema = root / "native/memery/crates/memery-core/src/store"
    source = (root / "native/memery/crates/memery-core/src/repository.rs").read_text()
    match = re.search(r'tx\.prepare\("(SELECT o\.kind,o\.change_set_id,o\.source_revision,o\.payload_json .*?LIMIT 33)"\)', source)
    assert match, "native current-context query must be available"
    query = match.group(1)
    conn = sqlite3.connect(":memory:")
    try:
        conn.executescript((schema / "current-schema.sql").read_text())
        conn.executescript((schema / "schema-v17-authority.sql").read_text())
        conn.execute("INSERT INTO fm_v2_entities(owner_id,entity_id,workspace_key,project_key,created_change_set_id,current_revision,created_at) VALUES ('alice','subject','global','','create',0,'now')")
        conn.execute("INSERT INTO fm_v2_knowledge_blocks(owner_id,block_id,workspace_key,project_key,subject_entity_id,predicate,claim_slot,cardinality,created_change_set_id,current_revision,created_at) VALUES ('alice','block','global','','subject','fact','memory','single','create',65,'now')")
        for revision in range(1, 66):
            payload = json.dumps({"curated_id":"memory", "block_id":"block", "revision":revision})
            conn.execute("INSERT INTO fm_v2_outbox(owner_id,event_id,change_set_id,kind,payload_json,source_revision,created_at) VALUES ('alice',?,?, 'mirror_update',?,?,'now')", (f"event-{revision}",f"change-{revision}",payload,revision))
        rows = conn.execute(query, ("alice", "memory", "global")).fetchall()
        assert len(rows) == 1 and rows[0][2] == 65
        assert any("s03_mirror_receipt_context" in str(row) for row in conn.execute("EXPLAIN QUERY PLAN " + query, ("alice", "memory", "global")))
        for n in range(33):
            payload = json.dumps({"curated_id":"memory", "block_id":"block", "revision":65})
            conn.execute("INSERT INTO fm_v2_outbox(owner_id,event_id,change_set_id,kind,payload_json,source_revision,created_at) VALUES ('alice',?,?, 'mirror_update',?,65,'now')", (f"duplicate-{n}",f"dup-change-{n}",payload))
        assert len(conn.execute(query, ("alice", "memory", "global")).fetchall()) == 33
        assert conn.execute(query, ("alice", "memory", "other")).fetchall() == []
    finally:
        conn.close()
