"""Temporary-data checks for owner/revision/read-only logging boundaries."""
import threading
import os
import tempfile
from pathlib import Path

# This probe is runnable without the repository conftest. Establish isolated
# paths BEFORE project imports because core.database initializes on import.
_probe_directory = tempfile.TemporaryDirectory(prefix="openclank-logging-probe-")
_probe_root = Path(_probe_directory.name)
os.environ.setdefault("OPEN_CLANK_DATA_DIR", str(_probe_root / "data"))
os.environ.setdefault("DATABASE_URL", f"sqlite:///{_probe_root / 'host.sqlite3'}")
os.environ.setdefault("FM_DB_PATH", str(_probe_root / "fm.sqlite3"))
if Path(os.environ["OPEN_CLANK_DATA_DIR"]).resolve() == Path(__file__).resolve().parents[1] / "data":
    raise RuntimeError("Logging probe refuses the live default data directory")
if os.environ["DATABASE_URL"] == f"sqlite:///{Path(__file__).resolve().parents[1] / 'data/app.db'}":
    raise RuntimeError("Logging probe refuses the live database")

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from services.logging.projection import LoggingError, LoggingProjection, iter_tool_facts
from services.stats.privacy import identity_handle
from src.openclank.conversation_archive import ConversationArchive, SourcePart
from routes.logging_routes import setup_logging_routes


@pytest.fixture
def source(tmp_path):
    archive = ConversationArchive(str(tmp_path / "archive.sqlite3"))
    archive.append_parts([
        SourcePart(owner="alice", chat_id="chat-a", actor_id="main", message_id="m1", part_id="text", content="first visible", event_sequence=1),
        SourcePart(owner="alice", chat_id="chat-a", actor_id="child", message_id="m2", part_id="tool", part_type="tool",
                   content={"mimo": {"part": {"tool": "read", "state": {"status": "completed", "input": {"path": "private"},
                                                                                "output": "secret output", "time": {"start": 10, "end": 30}}}}}, event_sequence=2),
        SourcePart(owner="bob", chat_id="chat-b", actor_id="main", message_id="b1", part_id="text", content="foreign secret", event_sequence=3),
        SourcePart(owner="alice", chat_id="chat-a", actor_id="main", message_id="m3", part_id="text", content="third", event_sequence=3),
    ])
    return archive, LoggingProjection(archive.db_path, cursor_secret=b"fixture-secret")


def test_owner_revision_order_and_metadata_first_export(source):
    archive, reader = source
    first = reader.parts("alice", limit=1)
    assert first["items"][0]["text"] == "first visible"
    assert "foreign" not in str(reader.parts("alice"))
    with pytest.raises(LoggingError, match="invalid cursor"):
        reader.parts("bob", cursor=first["page"]["next_cursor"])
    before = reader.prune_preview("alice")
    old_ref = first["items"][0]["source_ref"]
    archive.append_managed_parts([SourcePart(owner="alice", chat_id="chat-a", actor_id="main", message_id="m1", part_id="text",
                                            content="revised first", event_sequence=1)])
    with pytest.raises(LoggingError) as exc:
        reader.parts("alice", cursor=first["page"]["next_cursor"])
    assert exc.value.code == "stale_cursor"
    with pytest.raises(LoggingError) as exc:
        reader.get_part("alice", old_ref)
    assert exc.value.code == "stale_cursor"
    with pytest.raises(LoggingError) as exc:
        reader.prune_apply("alice", before["preview_id"])
    assert exc.value.code == "stale_cursor"
    assert [p["message_id"] for p in reader.parts("alice")["items"]] == ["m1", "m2", "m3"]
    export = reader.export("alice")
    assert "text" not in export["items"][0]
    assert "secret output" not in str(export)
    assert "secret output" in str(reader.export("alice", include_bodies=True))
    assert reader.prune_apply("alice", reader.prune_preview("alice")["preview_id"])["removed_records"] == 0
    assert archive.count_parts(owner="alice", chat_id="chat-a")["total"] > 0


def test_tool_facts_and_literal_search(source):
    _, reader = source
    facts = iter_tool_facts("alice", projection=reader)["facts"]
    assert len(facts) == 1
    assert facts[0]["tool_name"] == "read"
    assert facts[0]["duration_ms"] == 20
    assert facts[0]["terminal"] is True
    assert "secret output" not in str(facts)
    assert reader.parts("alice", {"tool_name": "read", "status": "completed"})["items"][0]["actor_id"] == "child"
    assert reader.parts("alice", query_text="%")['items'] == []
    assert len(reader.parts("alice", query_text="first")['items']) == 1
    assert reader.parts("bob", {"session_id": identity_handle("alice", "session_id", "chat-a")})['items'] == []
    event = threading.Event()
    event.set()
    with pytest.raises(LoggingError) as exc:
        reader.parts("alice", cancel_event=event)
    assert exc.value.code == "cancelled"


def test_missing_archive_does_not_create_it(tmp_path):
    missing = tmp_path / "missing.sqlite3"
    with pytest.raises(LoggingError) as exc:
        LoggingProjection(missing).sessions("alice")
    assert exc.value.code == "archive_unavailable"
    assert not missing.exists()


def test_real_routes_enforce_owner_and_revision(source):
    archive, reader = source
    app = FastAPI()
    app.include_router(setup_logging_routes(projection=reader, owner_resolver=lambda r: r.headers.get("x-test-owner")))
    client = TestClient(app)
    assert client.get("/api/logging/v1/sessions").status_code == 401
    headers = {"x-test-owner": "alice"}
    listing = client.get("/api/logging/v1/sessions", headers=headers).json()
    handle = listing["items"][0]["handle"]
    detail = client.get(f"/api/logging/v1/sessions/{handle}", headers=headers)
    assert detail.status_code == 200
    assert len(detail.json()["items"]) == 3
    assert client.get(f"/api/logging/v1/sessions/{handle}", headers={"x-test-owner": "bob"}).status_code == 404
    exported = client.post("/api/logging/v1/export", headers=headers, json={}).json()
    assert all("text" not in part for part in exported["items"])
    ref = detail.json()["items"][0]["source_ref"]
    assert client.post("/api/logging/v1/parts/get", headers=headers, json={"source_ref": ref}).json()["text"] == "first visible"
    assert client.post("/api/logging/v1/parts/get", headers={"x-test-owner": "bob"}, json={"source_ref": ref}).status_code == 404
    preview = client.post("/api/logging/v1/prune/preview", headers=headers, json={}).json()
    assert preview["preview"]["removable_records"] == 0
    assert client.post("/api/logging/v1/prune/preview", headers=headers, json={"target": "advanced_bodies"}).status_code == 503


def test_route_capacity_released_only_after_workers_exit():
    from concurrent.futures import ThreadPoolExecutor
    gate = threading.Event()
    started = threading.Barrier(3)

    class SlowReader:
        def sessions(self, *args, **kwargs):
            started.wait(timeout=2)
            gate.wait(timeout=2)
            return {"items": []}

    app = FastAPI()
    app.include_router(setup_logging_routes(projection=SlowReader(), owner_resolver=lambda _r: "alice"))
    client = TestClient(app)
    with ThreadPoolExecutor(max_workers=2) as workers:
        first = workers.submit(client.get, "/api/logging/v1/sessions")
        second = workers.submit(client.get, "/api/logging/v1/sessions")
        try:
            started.wait(timeout=2)
            assert client.get("/api/logging/v1/sessions").status_code == 503
        finally:
            gate.set()
        assert first.result(timeout=2).status_code == 200
        assert second.result(timeout=2).status_code == 200
