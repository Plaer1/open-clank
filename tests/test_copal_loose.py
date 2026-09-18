import asyncio
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.openclank.copal_loose import LooseCopalBridge
from routes.copal_routes import setup_copal_routes
from src.openclank.copal_bridge import CopalBridgeError


@pytest.mark.asyncio
async def test_loose_copal_uses_visible_document_files_and_stable_heads(tmp_path):
    bridge = LooseCopalBridge(tmp_path / "vaults")
    await bridge.start()
    created = await bridge.call(
        "create",
        {"owner": "owner", "workspace_id": "notes", "name": "Ideas/First.md", "kind": "markdown", "content": "one\n"},
    )
    document = created["doc"]
    visible_files = list((tmp_path / "vaults").rglob("First.md"))

    assert created["outcome"] == "created"
    assert len(visible_files) == 1
    assert visible_files[0].read_text() == "one\n"
    assert document["storage"] == "files"
    assert document["format"] == "copal-loose-v1"

    updated = await bridge.call(
        "write",
        {"owner": "owner", "workspace_id": "notes", "id": document["id"], "base": document["head"], "content": "two\n"},
    )
    assert updated["doc"]["head"] != document["head"]
    assert visible_files[0].read_text() == "two\n"
    history = list((visible_files[0].parent.parent / ".copal" / "history").rglob("*.json"))
    assert history


@pytest.mark.asyncio
async def test_loose_create_retry_reuses_action_and_document_identity(tmp_path):
    bridge = LooseCopalBridge(tmp_path / "vaults")
    args = {"owner": "owner", "workspace_id": "notes", "name": "Retry.md", "content": "one", "action_id": "create-retry-1"}
    first = await bridge.call("create", args)
    replay = await bridge.call("create", args)
    assert replay["replayed"] is True
    assert replay["doc"]["id"] == first["doc"]["id"]
    manifest = next((tmp_path / "vaults").rglob("manifest.json"))
    operations = json.loads(manifest.read_text())["operations"]
    assert [item for item in operations if item.get("actionId") == "create-retry-1"]


@pytest.mark.asyncio
async def test_loose_copal_scope_and_stale_write_fail_closed(tmp_path):
    bridge = LooseCopalBridge(tmp_path / "vaults")
    first = await bridge.call("create", {"owner": "alice", "workspace_id": "home", "name": "A.md", "content": "private"})
    hidden = await bridge.call("index", {"owner": "bob", "workspace_id": "home"})
    stale = await bridge.call(
        "write",
        {"owner": "alice", "workspace_id": "home", "id": first["doc"]["id"], "base": "stale", "content": "clobber"},
    )

    assert hidden["docs"] == []
    assert stale["outcome"] == "stale"
    assert (await bridge.call("get", {"owner": "alice", "workspace_id": "home", "id": first["doc"]["id"]}))["text"] == "private"


@pytest.mark.asyncio
async def test_loose_index_treats_all_corpus_as_a_wildcard(tmp_path):
    bridge = LooseCopalBridge(tmp_path / "vaults")
    scope = {"owner": "alice", "workspace_id": "home"}
    await bridge.call("create", {**scope, "name": "Note.md", "corpus": "notes", "content": "note"})
    await bridge.call("create", {**scope, "name": "Wiki.md", "kind": "wiki", "corpus": "wiki", "content": "wiki"})

    indexed = await bridge.call("index", {**scope, "corpus": "all"})
    status = await bridge.call("scoped_status", scope)

    assert indexed["total"] == 2
    assert {doc["corpus"] for doc in indexed["docs"]} == {"notes", "wiki"}
    assert status["documents"] == 2
    assert status["visible_documents"] == 2


@pytest.mark.asyncio
async def test_loose_metadata_page_sorts_by_filesystem_byte_size(tmp_path):
    bridge = LooseCopalBridge(tmp_path / "vaults")
    scope = {"owner": "owner", "workspace_id": "notes"}
    await bridge.call("create", {**scope, "name": "Z-small.md", "content": "é"})
    await bridge.call("create", {**scope, "name": "A-large.md", "content": "0123456789"})

    asc = await bridge.call(
        "metadata_page",
        {
            **scope,
            "corpus": "all",
            "hidden": "exclude",
            "state": "active",
            "sort_key": "size",
            "sort_direction": "asc",
        },
    )
    desc = await bridge.call(
        "metadata_page",
        {
            **scope,
            "corpus": "all",
            "hidden": "exclude",
            "state": "active",
            "sort_key": "size",
            "sort_direction": "desc",
        },
    )

    assert [(row["name"], row["size"]) for row in asc["docs"]] == [
        ("Z-small.md", 2),
        ("A-large.md", 10),
    ]
    assert [(row["name"], row["size"]) for row in desc["docs"]] == [
        ("A-large.md", 10),
        ("Z-small.md", 2),
    ]


@pytest.mark.asyncio
async def test_loose_keyed_task_pages_are_bounded_filtered_restart_safe_and_instrumented(tmp_path):
    bridge = LooseCopalBridge(tmp_path / "vaults")
    scope = {"owner": "owner", "workspace_id": "notes"}
    items = [
        {
            "id": f"DOC:{index:04d}",
            "source": "markdown" if index % 2 else "vault",
            "label": f"Task {index:04d}",
            "text": f"Task {index:04d}",
            "checked": bool(index % 3 == 0),
        }
        for index in range(5000)
    ]
    rebuilt = await bridge.call(
        "task_index_update",
        {**scope, "generation": "g1", "records": {"DOC": {"head": "h1", "items": items}}, "removed": [], "rebuild": True, "sourceReads": 1},
    )
    assert rebuilt["sourceReads"] == 1
    assert rebuilt["rewrittenRows"] >= 5002
    assert rebuilt["rewrittenBytes"] > len(json.dumps(items))

    first = await bridge.call("task_index_page", {**scope, "generation": "g1", "limit": 100})
    assert len(first["items"]) == 100
    assert first["scannedRows"] <= 101
    assert first["returnedRows"] == 100
    cursor = first["nextCursor"]
    seen = {item["id"] for item in first["items"]}
    while cursor:
        page = await bridge.call("task_index_page", {**scope, "generation": "g1", "cursor": cursor, "limit": 100})
        assert not seen.intersection(item["id"] for item in page["items"])
        seen.update(item["id"] for item in page["items"])
        cursor = page["nextCursor"]
    assert len(seen) == 5000

    markdown = await bridge.call("task_index_page", {**scope, "generation": "g1", "source": "markdown", "limit": 100})
    assert markdown["total"] == 2500
    assert markdown["indexedTotal"] == 5000
    assert markdown["matchedTotal"] == 2500
    assert all(item["source"] == "markdown" for item in markdown["items"])
    assert markdown["scannedRows"] > markdown["returnedRows"]

    await bridge.call("task_index_update", {**scope, "generation": "g2", "records": {}, "removed": ["DOC"], "sourceReads": 1})
    with pytest.raises(CopalBridgeError, match="stale_cursor"):
        await bridge.call("task_index_page", {**scope, "generation": "g1", "cursor": first["nextCursor"], "limit": 100})
    restarted = LooseCopalBridge(tmp_path / "vaults")
    fresh = await restarted.call("task_index_page", {**scope, "generation": "g2", "limit": 10})
    assert fresh["items"] == []


@pytest.mark.asyncio
async def test_loose_copal_allows_only_typed_copal_system_documents(tmp_path):
    bridge = LooseCopalBridge(tmp_path / "vaults")
    scope = {"owner": "owner", "workspace_id": "notes"}

    planning = await bridge.call(
        "create",
        {**scope, "name": ".copal/planning.json", "kind": "planning", "content": "{}"},
    )
    event = await bridge.call(
        "create",
        {**scope, "name": ".copal/events/event-1.json", "kind": "copal-event", "content": "{}"},
    )

    assert planning["doc"]["name"] == ".copal/planning.json"
    assert planning["doc"]["hidden"] is True
    assert event["doc"]["name"] == ".copal/events/event-1.json"
    assert event["doc"]["hidden"] is True
    assert list((tmp_path / "vaults").rglob("planning.json"))

    for name, kind in (
        (".copal/manifest.json", "planning"),
        (".copal/history/forged.json", "copal-event"),
        (".copal/events/forged.json", "markdown"),
        (".copal/arbitrary.json", "calendar-projection"),
    ):
        with pytest.raises(CopalBridgeError, match="invalid loose Copal document path"):
            await bridge.call("create", {**scope, "name": name, "kind": kind, "content": "{}"})


@pytest.mark.asyncio
async def test_loose_copal_trash_is_recoverable_metadata_not_permanent_delete(tmp_path):
    bridge = LooseCopalBridge(tmp_path / "vaults")
    created = await bridge.call("create", {"owner": "owner", "workspace_id": "notes", "name": "Trash.md", "content": "keep"})
    deleted = await bridge.call("trash", {"owner": "owner", "workspace_id": "notes", "id": created["doc"]["id"]})

    assert deleted["outcome"] == "deleted"
    assert (await bridge.call("index", {"owner": "owner", "workspace_id": "notes"}))["docs"] == []
    trash = await bridge.call("trash_list", {"owner": "owner", "workspace_id": "notes"})
    assert trash["docs"][0]["trashed"] is True
    manifest = next((tmp_path / "vaults").rglob("manifest.json"))
    assert json.loads(manifest.read_text())["documents"][created["doc"]["id"]]["trashed"] is True


@pytest.mark.asyncio
async def test_loose_copal_history_checkpoint_restore_and_trash_listing_match_bridge_vocabulary(tmp_path):
    bridge = LooseCopalBridge(tmp_path / "vaults")
    created = await bridge.call("create", {"owner": "owner", "workspace_id": "notes", "name": "History.md", "content": "one"})
    doc = created["doc"]
    checkpoint = await bridge.call("checkpoint", {"owner": "owner", "workspace_id": "notes", "id": doc["id"], "message": "first"})
    assert checkpoint["doc"]["id"] == doc["id"]
    written = await bridge.call("write", {"owner": "owner", "workspace_id": "notes", "id": doc["id"], "base": doc["head"], "content": "two"})
    history = await bridge.call("history", {"owner": "owner", "workspace_id": "notes", "id": doc["id"]})
    assert len(history["changes"]) >= 3
    restored = await bridge.call("restore", {"owner": "owner", "workspace_id": "notes", "id": doc["id"], "commit": doc["head"]})
    assert restored["doc"]["text"] == "one"
    await bridge.call("trash", {"owner": "owner", "workspace_id": "notes", "id": doc["id"]})
    assert (await bridge.call("trash", {"owner": "owner", "workspace_id": "notes"}))["docs"]
    recovered = await bridge.call("restore_deleted", {"owner": "owner", "workspace_id": "notes", "id": doc["id"]})
    assert recovered["doc"]["text"] == "one"
    assert written["doc"]["head"] != doc["head"]


def test_loose_bridge_keeps_api_note_projection_on_files(monkeypatch, tmp_path):
    monkeypatch.setenv("AUTH_ENABLED", "false")
    app = FastAPI()
    app.include_router(setup_copal_routes())
    app.state.copal_bridge = LooseCopalBridge(tmp_path / "vaults")
    client = TestClient(app)
    response = client.post("/api/copal/documents", json={"name": "Loose.md", "content": "# Ordinary file"})
    assert response.status_code == 200
    note = response.json()["doc"]
    assert note["storage"] == "files"
    assert note["format"] == "copal-loose-v1"
    assert list((tmp_path / "vaults").rglob("Loose.md"))
