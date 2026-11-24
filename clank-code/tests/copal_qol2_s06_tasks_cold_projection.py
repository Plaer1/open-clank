"""S06 cold Tasks projection recovery through the keyed production bridge.

The task index is a Files projection maintained by the Copal repository. Losing or corrupting that derived table must cause the authenticated
Tasks route to rebuild from canonical native note records, preserving each
note as the only task authority.
"""

from __future__ import annotations

import json

import httpx
import pytest
from fastapi import FastAPI, Request
from types import SimpleNamespace

from routes.copal_routes import setup_copal_routes
from src.openclank.copal_loose import LooseCopalBridge


def _native_note(*, blocks):
    return json.dumps(
        {
            "schemaVersion": 1,
            "body": {"type": "doc", "blocks": blocks},
            "properties": [],
            "relations": [],
            "extensions": {},
        },
        separators=(",", ":"),
    )


def _task(block_id, text, *, checked=False):
    return {
        "id": block_id,
        "type": "task",
        "source": f"- [{'x' if checked else ' '}] {text}",
        "text": text,
        "checked": checked,
    }


def _app(bridge):
    app = FastAPI()

    @app.middleware("http")
    async def disposable_identity(request: Request, call_next):
        request.state.current_user = request.headers.get("x-test-user")
        request.state.authenticated = request.state.current_user in {"alice", "bob"}
        return await call_next(request)

    app.include_router(setup_copal_routes())
    app.state.copal_bridge = bridge
    app.state.auth_manager = SimpleNamespace(users={"alice": {}, "bob": {}}, account_id=lambda username: f"account:{username}")
    return app


async def _create(bridge, owner, workspace, name, blocks):
    result = await bridge.call(
        "create",
        {
            "owner": owner,
            "workspace_id": workspace,
            "kind": "note",
            "name": name,
            "content": _native_note(blocks=blocks),
        },
    )
    return result["doc"]


async def _tasks(http, user, workspace):
    response = await http.get(
        "/api/copal/tasks",
        params={"workspace": workspace},
        headers={"x-test-user": user},
    )
    assert response.status_code == 200, response.text
    result = response.json()
    result["items"] = [item for item in result["items"] if not (item.get("document") or {}).get("readOnly")]
    return result


@pytest.mark.asyncio
async def test_keyed_tasks_rebuild_from_native_notes_after_stale_and_cold_projection(
    tmp_path,
):
    """Exercise actual LooseCopalBridge -> authenticated route -> keyed Files index."""

    data_dir = tmp_path / "copal-keyed-cold"
    bridge = LooseCopalBridge(data_dir)
    await bridge.start()
    try:
        alice_note = await _create(
            bridge,
            "alice",
            "personal",
            "Cold.md",
            [_task("blk_keep", "Keep"), _task("blk_remove", "Remove")],
        )
        alice_other_workspace = await _create(
            bridge,
            "alice",
            "archive",
            "Archive.md",
            [_task("blk_archive", "Archive")],
        )
        bob_note = await _create(
            bridge,
            "bob",
            "personal",
            "Cold.md",
            [_task("blk_secret", "Secret")],
        )
        app = _app(bridge)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://s06") as http:
            initial = await _tasks(http, "alice", "personal")
            assert [item["text"] for item in initial["items"]] == ["Keep", "Remove"]
            assert initial["snapshotRevision"]
            indexed = await bridge.call(
                "task_index_get", {"owner": "alice", "workspace_id": "personal"}
            )
            task_documents = {
                document_id: record
                for document_id, record in indexed["documents"].items()
                if record.get("items") and document_id == alice_note["id"]
            }
            assert set(task_documents) == {alice_note["id"]}
            cached = indexed["documents"][alice_note["id"]]
            assert cached["head"] == alice_note["head"]
            assert {item["id"] for item in cached["items"]} == {
                f"{alice_note['id']}:blk_keep",
                f"{alice_note['id']}:blk_remove",
            }
            canonical_before = await bridge.call(
                "get", {"owner": "alice", "workspace_id": "personal", "id": alice_note["id"]}
            )

            # A changed source note must invalidate stale derived rows. Keep
            # one block identity and remove the other to prove exact ID/rev
            # handling rather than merely matching visible task text.
            updated_content = _native_note(
                blocks=[_task("blk_keep", "Changed", checked=True)]
            )
            updated = await bridge.call(
                "write",
                {
                    "owner": "alice",
                    "workspace_id": "personal",
                    "id": alice_note["id"],
                    "content": updated_content,
                    "base": alice_note["head"],
                },
            )
            assert updated["outcome"] == "committed"
            changed_head = updated["doc"]["head"]
            # An incremental update must retain rows belonging to unchanged documents.
            await _tasks(http, "alice", "personal")
            patched_index = await bridge.call("task_index_get", {"owner": "alice", "workspace_id": "personal"})
            untouched = {key: value for key, value in indexed["documents"].items() if key != alice_note["id"]}
            assert untouched
            assert all(patched_index["documents"].get(key) == value for key, value in untouched.items())
            # Deliberately make only the derived index stale/incorrect. The
            # canonical note bytes remain untouched and are the recovery source.
            await bridge.call(
                "task_index_update",
                {
                    "owner": "alice",
                    "workspace_id": "personal",
                    "generation": "corrupt-derived-generation",
                    "records": {
                        alice_note["id"]: {
                            "head": canonical_before["head"],
                            "resourceId": cached["resourceId"],
                            "items": cached["items"],
                        }
                    },
                    "removed": [],
                    "rebuild": True,
                    "sourceReads": 0,
                },
            )
            repaired = await _tasks(http, "alice", "personal")
            assert [(item["id"], item["text"], item["checked"]) for item in repaired["items"]] == [
                (f"{alice_note['id']}:blk_keep", "Changed", True)
            ]
            assert repaired["items"][0]["sourceRevision"] == changed_head
            assert repaired["snapshotRevision"]
            source_after_repair = await bridge.call(
                "get", {"owner": "alice", "workspace_id": "personal", "id": alice_note["id"]}
            )
            assert source_after_repair["text"] == updated_content
            assert source_after_repair["head"] == changed_head

            # Clear only the keyed derived table, then stop and recreate the
            # actual repository facade. The restarted route must lazily rebuild.
            await bridge.call(
                "task_index_update",
                {
                    "owner": "alice",
                    "workspace_id": "personal",
                    "generation": "",
                    "records": {},
                    "removed": [],
                    "rebuild": True,
                    "sourceReads": 0,
                },
            )
            cold_meta = await bridge.call(
                "task_index_generation", {"owner": "alice", "workspace_id": "personal"}
            )
            assert cold_meta["total"] == 0
            assert cold_meta["sourceRevision"] == ""
            await bridge.stop()
            reopened = LooseCopalBridge(data_dir)
            await reopened.start()
            try:
                restarted_app = _app(reopened)
                restarted_transport = httpx.ASGITransport(app=restarted_app)
                async with httpx.AsyncClient(
                    transport=restarted_transport, base_url="http://s06-restarted"
                ) as restarted_http:
                    rebuilt = await _tasks(restarted_http, "alice", "personal")
                    assert [(item["id"], item["text"], item["checked"]) for item in rebuilt["items"]] == [
                        (f"{alice_note['id']}:blk_keep", "Changed", True)
                    ]
                    assert rebuilt["items"][0]["sourceRevision"] == changed_head
                    assert rebuilt["snapshotRevision"]
                    rebuilt_index = await reopened.call(
                        "task_index_get", {"owner": "alice", "workspace_id": "personal"}
                    )
                    assert rebuilt_index["documents"][alice_note["id"]]["head"] == changed_head

                    # The keyed table is tenant/workspace scoped, including
                    # after a restart; no raw document ID widens authority.
                    alice_archive = await _tasks(restarted_http, "alice", "archive")
                    assert [item["text"] for item in alice_archive["items"]] == ["Archive"]
                    assert alice_archive["items"][0]["id"] == f"{alice_other_workspace['id']}:blk_archive"
                    bob_personal = await _tasks(restarted_http, "bob", "personal")
                    assert [item["text"] for item in bob_personal["items"]] == ["Secret"]
                    assert bob_personal["items"][0]["id"] == f"{bob_note['id']}:blk_secret"
                    alice_cannot_see_bob = await _tasks(restarted_http, "alice", "personal")
                    assert "Secret" not in {item["text"] for item in alice_cannot_see_bob["items"]}
            finally:
                await reopened.stop()
        print(
            "keyed Copal bridge cold recovery: native note IDs/revisions, "
            "stale changed/removed rows, restart rebuild, and account/workspace isolation passed"
        )
    finally:
        if bridge.is_alive():
            await bridge.stop()
