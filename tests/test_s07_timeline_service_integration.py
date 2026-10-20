"""S07 Timeline service seam: Files preparation -> planning route CAS.

The mounted browser journey proves delegated drop ownership, but its CAS map is
necessarily a fixture.  These tests use the production Files facade route, the
production Copal attachment provider, the loose Copal bridge, and the
production planning event route against a disposable workspace.
"""

from __future__ import annotations

import json

import httpx
import pytest
from fastapi import FastAPI

from routes.copal_routes import setup_copal_routes
from routes.files_facade_routes import setup_files_facade_routes
from src.openclank.copal_loose import LooseCopalBridge
from src.openclank.copal_planning import serialize_event, serialize_track_registry
from src.openclank.file_policy import FilePolicyRepository
from src.openclank.resource_refs import issue_resource_ref


WORKSPACE = "timeline"
OWNER = "local"


async def _client(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTH_ENABLED", "false")
    monkeypatch.setenv("COPAL_CALENDAR_PROJECTION_ENABLED", "false")
    bridge = LooseCopalBridge(tmp_path / "copal")
    policy = FilePolicyRepository(tmp_path / "files-policy.sqlite3")
    app = FastAPI()
    app.state.copal_bridge = bridge
    app.state.files_policy_repository = policy
    app.include_router(setup_copal_routes(policy_repository=policy))
    app.include_router(setup_files_facade_routes(policy_repository=policy))
    transport = httpx.ASGITransport(app=app)
    client = httpx.AsyncClient(transport=transport, base_url="http://timeline.test")
    return client, bridge, policy


async def _seed(bridge: LooseCopalBridge) -> tuple[str, str, str]:
    tracks = [{"id": "home", "name": "Home", "color": "#14b8a6", "icon": "home", "enabled": True}]
    await bridge.call(
        "create",
        {
            "owner": OWNER,
            "workspace_id": WORKSPACE,
            "name": ".copal/tracks.json",
            "kind": "copal-tracks",
            "content": serialize_track_registry(tracks),
        },
    )
    event_result = await bridge.call(
        "create",
        {
            "owner": OWNER,
            "workspace_id": WORKSPACE,
            "name": ".copal/events/event-1.json",
            "kind": "copal-event",
            "content": serialize_event(
                {"title": "Pack", "startDate": "2026-09-09", "trackId": "home"},
                tracks=tracks,
            ),
        },
    )
    source_result = await bridge.call(
        "create",
        {
            "owner": OWNER,
            "workspace_id": WORKSPACE,
            "name": "source.md",
            "kind": "note",
            "content": "# Source\n\nReadable attachment.\n",
        },
    )
    return (
        str(event_result["doc"]["id"]),
        str(source_result["doc"]["id"]),
        str(event_result["doc"]["head"]),
    )


async def _resource_rows(client: httpx.AsyncClient, query: str) -> dict:
    response = await client.post(
        "/api/files-v1/search?copal_workspace=timeline",
        json={"query": query, "limit": 20, "sort": {"key": "name", "direction": "asc"}},
    )
    assert response.status_code == 200, response.text
    rows = response.json()["entries"]
    assert len(rows) == 1
    return rows[0]


async def _prepare_body(client, policy, source, target, operation_id, *, generation=None):
    current_generation = policy.generation() if generation is None else generation
    return {
        "operation_id": operation_id,
        "generation": current_generation,
        "source": {"resource_ref": source["ref"], "expected_revision": source["revision"]},
        "target": {"kind": "copal_document", "resource_ref": target["ref"], "expected_revision": target["revision"]},
        "mode": "link",
    }


@pytest.mark.asyncio
async def test_timeline_attachment_uses_real_files_and_planning_cas_with_replay(tmp_path, monkeypatch):
    client, bridge, policy = await _client(tmp_path, monkeypatch)
    try:
        event_id, source_id, initial_head = await _seed(bridge)
        source = await _resource_rows(client, "source.md")
        target_response = await client.post(
            "/api/files-v1/resolve-resource",
            json={"resource_key": {"provider": "copal", "account_id": "local-installation", "workspace_id": WORKSPACE, "resource_id": event_id}},
        )
        assert target_response.status_code == 200, target_response.text
        target = target_response.json()
        assert target["provider"] == "copal"
        assert "write" in target["capabilities"]

        body = await _prepare_body(client, policy, source, target, "timeline-attach-1")
        prepared = await client.post("/api/files-v1/attachments/prepare?copal_workspace=timeline", json=body)
        assert prepared.status_code == 200, prepared.text
        receipt = prepared.json()
        assert receipt["target_identity"]["kind"] == "copal_document"
        assert receipt["source_revision"] == source["revision"]
        assert receipt["generation"] == policy.generation()

        # A lost response/retry replays the durable Files preparation and does
        # not create a second asset.
        replay = await client.post("/api/files-v1/attachments/prepare?copal_workspace=timeline", json=body)
        assert replay.status_code == 200, replay.text
        assert replay.json()["preparation_receipt_id"] == receipt["preparation_receipt_id"]

        # A response lost after the real POST is recoverable through a fresh
        # route facade, while another workspace cannot inspect the receipt.
        recovered = await client.get("/api/files-v1/attachments/timeline-attach-1?copal_workspace=timeline")
        assert recovered.status_code == 200, recovered.text
        assert recovered.json()["state"] == "complete"
        assert recovered.json()["preparation"] == receipt
        assert recovered.json()["operation_id"] == "timeline-attach-1"
        assert recovered.json()["generation"] == policy.generation()
        wrong_workspace = await client.get("/api/files-v1/attachments/timeline-attach-1?copal_workspace=elsewhere")
        assert wrong_workspace.status_code == 404, wrong_workspace.text
        assert wrong_workspace.json()["detail"]["code"] == "resource_unavailable"
        assert "preparation" not in wrong_workspace.json()["detail"]
        assets_before = [row for row in (await bridge.call("export_snapshot", {"owner": OWNER, "workspace_id": WORKSPACE}))["docs"] if row.get("kind") == "asset"]
        assert len(assets_before) == 1

        attachment = {
            "operationId": "timeline-attach-1",
            "preparationReceiptId": receipt["preparation_receipt_id"],
            "mode": "link",
            "sourceRevision": receipt["source_revision"],
            "targetRevision": receipt["target_revision"],
            "asset": receipt["asset"],
            "insertion": receipt["insertion"],
        }
        committed = await client.patch(
            f"/api/copal/planning/events/{event_id}?workspace={WORKSPACE}",
            json={"base": initial_head, "patch": {"attachments": [attachment]}},
        )
        assert committed.status_code == 200, committed.text
        assert committed.json()["event"]["copal_extra"]["attachments"] == [attachment]
        current_head = committed.json()["event"]["head"]

        # The same preparation cannot be applied over a changed event head;
        # the planning route leaves the canonical event untouched.
        stale = await client.patch(
            f"/api/copal/planning/events/{event_id}?workspace={WORKSPACE}",
            json={"base": initial_head, "patch": {"attachments": [attachment, attachment]}},
        )
        assert stale.status_code == 409
        current = await bridge.call("metadata_get", {"owner": OWNER, "workspace_id": WORKSPACE, "id": event_id, "state": "active", "hidden": "include", "corpus": "all"})
        assert current["head"] == current_head
        assert current_head != initial_head

        # Reusing the source's old revision after a real provider write is
        # rejected before another asset can be materialized.
        source_current = await bridge.call("metadata_get", {"owner": OWNER, "workspace_id": WORKSPACE, "id": source_id, "state": "active", "hidden": "include", "corpus": "all"})
        await bridge.call("write", {"owner": OWNER, "workspace_id": WORKSPACE, "id": source_id, "base": source_current["head"], "content": "# Source\n\nChanged.\n"})
        stale_source = await client.post("/api/files-v1/attachments/prepare?copal_workspace=timeline", json=await _prepare_body(client, policy, source, target, "stale-source"))
        assert stale_source.status_code == 409
        assets_after_source_conflict = [row for row in (await bridge.call("export_snapshot", {"owner": OWNER, "workspace_id": WORKSPACE}))["docs"] if row.get("kind") == "asset"]
        assert len(assets_after_source_conflict) == 1
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_timeline_attachment_rejects_scope_generation_and_malformed_requests_without_mutation(tmp_path, monkeypatch):
    client, bridge, policy = await _client(tmp_path, monkeypatch)
    try:
        event_id, source_id, initial_head = await _seed(bridge)
        source = await _resource_rows(client, "source.md")
        target = (await client.post(
            "/api/files-v1/resolve-resource",
            json={"resource_key": {"provider": "copal", "account_id": "local-installation", "workspace_id": WORKSPACE, "resource_id": event_id}},
        )).json()
        snapshot = lambda: bridge.call("export_snapshot", {"owner": OWNER, "workspace_id": WORKSPACE})
        before = await snapshot()

        malformed = await client.post(
            "/api/files-v1/attachments/prepare?copal_workspace=timeline",
            json={"operation_id": "bad", "generation": policy.generation(), "source": {"resource_ref": source["ref"], "unexpected": True}, "target": {"kind": "copal_document", "resource_ref": target["ref"], "expected_revision": target["revision"]}, "mode": "link"},
        )
        assert malformed.status_code == 422

        # A ref issued for another owner is rejected before Copal materializes
        # bytes. Workspace identity is likewise checked by the sealed ref.
        foreign = dict(source)
        foreign["ref"] = issue_resource_ref(
            owner_subject_id="other-account",
            provider="copal",
            origin_id=f"document:{WORKSPACE}:{source_id}",
            kind="document",
            capabilities=("stat", "open", "read", "download"),
            policy_generation=policy.generation(),
            workspace_id=WORKSPACE,
        ).token
        unauthorized = await client.post(
            "/api/files-v1/attachments/prepare?copal_workspace=timeline",
            json=await _prepare_body(client, policy, foreign, target, "foreign-1"),
        )
        assert unauthorized.status_code in {404, 409}

        wrong_workspace = await client.post(
            "/api/files-v1/resolve-resource",
            json={"resource_key": {"provider": "copal", "account_id": "local-installation", "workspace_id": "elsewhere", "resource_id": event_id}},
        )
        assert wrong_workspace.status_code in {404, 409}

        old_generation = policy.generation()
        policy.create_location(actor_subject_id=OWNER, path=str(tmp_path / "generation-bump"), kind="directory", capabilities=("read",))
        assert policy.generation() > old_generation
        stale_generation = await client.post(
            "/api/files-v1/attachments/prepare?copal_workspace=timeline",
            json=await _prepare_body(client, policy, source, target, "stale-generation", generation=old_generation),
        )
        assert stale_generation.status_code == 409

        after = await snapshot()
        before_docs = {(row["id"], row.get("head"), row.get("kind")) for row in before["docs"]}
        after_docs = {(row["id"], row.get("head"), row.get("kind")) for row in after["docs"]}
        assert before_docs == after_docs
        event = await bridge.call("metadata_get", {"owner": OWNER, "workspace_id": WORKSPACE, "id": event_id, "state": "active", "hidden": "include", "corpus": "all"})
        assert event["head"] == initial_head
        assert "attachments" not in event.get("text", "")
    finally:
        await client.aclose()
