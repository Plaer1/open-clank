"""Bounded first-event journeys using the real disposable Files repository."""
import asyncio

import httpx
import pytest
from fastapi import FastAPI

import routes.copal_routes as routes
from src.openclank.copal_errors import CopalBridgeError
from src.openclank.copal_loose import LooseCopalBridge
from src.openclank.copal_planning import EVENT_KIND, TRACKS_KIND, track_registry_from_document


@pytest.fixture
def journey(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENCLANK_HISTORY_SOCKET", raising=False)
    monkeypatch.setattr(routes, "require_user", lambda _request: "fixture")
    monkeypatch.setattr(routes, "record_activity", lambda *_args, **_kwargs: None)
    async def no_projection(*_args):
        return {"enabled": False}
    monkeypatch.setattr(routes, "_project_canonical_workspace", no_projection)
    bridge = LooseCopalBridge(tmp_path)
    app = FastAPI()
    app.state.copal_bridge = bridge
    app.include_router(routes.setup_copal_routes())
    return app, bridge


def payload(action="first-a", name="  Research  "):
    return {"actionId": action, "trackName": name, "event": {"title": "First event", "description": "Details", "startDate": "2026-10-13", "dueDate": "2026-10-14"}}


async def request(app, value, workspace="default"):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://fixture") as client:
        return await client.post(f"/api/copal/planning/first-event?workspace={workspace}", json=value)


async def counts(bridge, workspace="default"):
    docs = (await bridge.call("index", {"owner": "fixture", "workspace_id": workspace}))["docs"]
    return [d for d in docs if d["kind"] == TRACKS_KIND], [d for d in docs if d["kind"] == EVENT_KIND]


def test_validation_replay_conflict_and_explicit_adoption(journey):
    async def run():
        app, bridge = journey
        assert (await request(app, payload(name="   "))).status_code == 422
        assert (await request(app, {**payload(), "event": {"startDate": "not-a-date"}})).status_code == 422
        assert await counts(bridge) == ([], [])
        first = await request(app, payload())
        assert first.status_code == 200, first.text
        receipt = first.json()
        assert receipt["track"]["name"] == "Research"
        assert receipt["event"]["trackId"] == receipt["track"]["id"]
        assert not receipt["event"]["floating"]
        retry = await request(app, payload())
        assert retry.status_code == 200 and retry.json()["event"]["id"] == receipt["event"]["id"]
        assert (await request(app, payload(name="Other"))).status_code == 409
        other = payload("first-b", "Another")
        stale = await request(app, other)
        assert stale.status_code == 409 and stale.json()["detail"]["outcome"] == "stale"
        adopted = await request(app, {**other, "existingTrackId": receipt["track"]["id"]})
        assert adopted.status_code == 200
        registries, events = await counts(bridge)
        assert len(registries) == 1 and len(events) == 2
        assert len(track_registry_from_document(registries[0])["tracks"]) == 1
        isolated = await request(app, payload(), workspace="other")
        assert isolated.status_code == 200
        assert isolated.json()["event"]["id"] != receipt["event"]["id"]
    asyncio.run(run())


def test_double_submission_and_distinct_concurrent_actions(journey):
    async def run():
        app, bridge = journey
        same = await asyncio.gather(request(app, payload()), request(app, payload()))
        assert [r.status_code for r in same] == [200, 200]
        assert same[0].json()["event"]["id"] == same[1].json()["event"]["id"]
        assert len((await counts(bridge))[1]) == 1
        distinct = await asyncio.gather(request(app, payload("x", "X"), "race"), request(app, payload("y", "Y"), "race"))
        assert sorted(r.status_code for r in distinct) == [200, 409]
        registries, events = await counts(bridge, "race")
        assert len(registries) == len(events) == 1
        winning = next(r.json() for r in distinct if r.status_code == 200)
        assert winning["track"]["name"] in {"X", "Y"}
    asyncio.run(run())


def test_partial_failure_response_loss_and_projection_failure(journey, monkeypatch):
    async def run():
        app, bridge = journey
        original = bridge.call
        failure = {"before": True, "after": False}
        async def flaky(operation, args, timeout=20):
            if operation == "create" and args.get("kind") == EVENT_KIND:
                if failure["before"]:
                    failure["before"] = False
                    raise CopalBridgeError("fixture event storage unavailable")
                if failure["after"]:
                    failure["after"] = False
                    await original(operation, args, timeout=timeout)
                    raise CopalBridgeError("fixture response lost after commit")
            return await original(operation, args, timeout=timeout)
        monkeypatch.setattr(bridge, "call", flaky)
        partial = await request(app, payload())
        assert partial.status_code == 503 and partial.json()["detail"]["outcome"] == "partial"
        registries, events = await counts(bridge)
        assert len(registries) == 1 and not events
        failure["after"] = True
        recovered = await request(app, payload())
        assert recovered.status_code == 200
        assert len((await counts(bridge))[1]) == 1
        async def projection_error(*_args):
            raise CopalBridgeError("fixture projection index unavailable after commit")
        monkeypatch.setattr(routes, "_project_canonical_workspace", projection_error)
        confirmed = await request(app, payload())
        assert confirmed.status_code == 200
        assert confirmed.json()["calendar_projection"]["ok"] is False
        assert confirmed.json()["event"]["id"] == recovered.json()["event"]["id"]
        assert len((await counts(bridge))[1]) == 1
    asyncio.run(run())
