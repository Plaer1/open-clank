"""Actual persisted note-write receipts; no product server or live data."""
import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI

import routes.copal_routes as routes
from src.openclank.copal_loose import LooseCopalBridge
from src.openclank.history_client import HistoryClientError


@pytest.fixture
def journey(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENCLANK_HISTORY_SOCKET", raising=False)
    monkeypatch.setattr(routes, "require_user", lambda _request: "fixture")
    monkeypatch.setattr(routes, "record_activity", lambda *_args, **_kwargs: None)
    bridge = LooseCopalBridge(tmp_path)
    bridge.supports_task_index_lookup = False
    # Official reference installation is independent of Save integrity.
    monkeypatch.setattr(bridge, "_ensure_official_references", lambda _v, manifest, _a: manifest)
    app = FastAPI()
    app.state.copal_bridge = bridge
    app.include_router(routes.setup_copal_routes())
    return app, bridge


async def create(bridge, workspace="default", name="Original.md"):
    return (await bridge.call("create", {"owner": "fixture", "workspace_id": workspace, "kind": "note", "name": name, "content": routes._encode_note("before", {}, [])}))["doc"]


async def write(app, doc, payload, workspace="default"):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://fixture") as client:
        return await client.put(f"/api/copal/documents/{doc['id']}?workspace={workspace}", json=payload)


def payload(doc, action="save-a", content="saved A"):
    return {"actionId": action, "content": content, "base": doc["head"], "properties": {}, "relations": []}


def snapshot_path(bridge, response):
    doc = response["doc"]
    return bridge._history_dir(bridge._vault("fixture", "default"), doc) / f"{doc['head']}.json"


def test_original_receipt_replay_after_later_edit_and_payload_scope_binding(journey):
    async def run():
        app, bridge = journey
        doc = await create(bridge)
        wire = payload(doc)
        first = await write(app, doc, wire)
        assert first.status_code == 200, first.text
        original = first.json()
        assert original["outcome"] == "committed"
        assert original["doc"]["text"] == "saved A"
        later = await write(app, doc, payload(original["doc"], "save-b", "saved B"))
        assert later.status_code == 200
        # A fresh repository instance proves receipts aren't a route cache.
        app.state.copal_bridge = LooseCopalBridge(bridge.data_dir)
        replay = await write(app, doc, wire)
        assert replay.status_code == 200, replay.text
        assert replay.json()["doc"] == original["doc"]
        assert replay.json()["actionId"] == wire["actionId"]
        assert replay.json()["outcome"] == "committed"
        assert replay.json()["replayed"] is True
        current = await bridge.call("get", {"owner": "fixture", "workspace_id": "default", "id": doc["id"]})
        assert routes._note_view(current)["text"] == "saved B"
        assert current["head"] == later.json()["doc"]["head"]
        changed = await write(app, doc, {**wire, "content": "changed action"})
        assert changed.status_code == 409 and changed.json()["detail"]["outcome"] == "idempotency_conflict"
        app.state.auth_manager = SimpleNamespace(account_id=lambda _u: "different-account")
        assert (await write(app, doc, wire)).status_code == 409
        app.state.auth_manager = None
        other_doc = await create(bridge, name="Another.md")
        other_target = await write(app, other_doc, payload(other_doc, "save-a"))
        assert other_target.status_code == 409
        stale_new_action = await write(app, doc, payload(doc, "new-action"))
        assert stale_new_action.status_code == 409 and stale_new_action.json()["detail"]["outcome"] == "stale"
        scoped_doc = await create(bridge, "other")
        scoped = await write(app, scoped_doc, payload(scoped_doc, "save-a"), "other")
        assert scoped.status_code == 200
        journal = json.loads(bridge._guarded.path.read_text())
        assert not journal["pending"]
        assert all("text" not in r.get("document_metadata", {}) for r in journal["receipts"].values())
        assert "saved A" not in bridge._guarded.path.read_text()
    asyncio.run(run())


@pytest.mark.parametrize("corrupt", [None, [], "missing"])
def test_unavailable_original_snapshot_is_explicit(journey, corrupt):
    async def run():
        app, bridge = journey
        doc = await create(bridge)
        wire = payload(doc)
        first = await write(app, doc, wire)
        assert first.status_code == 200
        path = snapshot_path(bridge, first.json())
        if corrupt == "missing":
            path.unlink()
        else:
            path.write_text(json.dumps(corrupt))
        replay = await write(app, doc, wire)
        assert replay.status_code == 503
        assert replay.json()["detail"]["outcome"] == "receipt_unavailable"
    asyncio.run(run())


def install_history(monkeypatch):
    history = {"actions": {}, "prepares": [], "completes": [], "live": []}
    class History:
        def __init__(self, *_args, **_kwargs):
            pass
        def prepare(self, envelope, *, content, fingerprint):
            action = envelope["action_id"]
            history["actions"][action] = {"before": content, "Action": {**envelope, "state": "Applying"}}
            history["prepares"].append((action, content))
        def get_capture_action(self, action):
            if action not in history["actions"]:
                raise HistoryClientError("missing", "original capture unavailable")
            return {"Action": history["actions"][action]["Action"]}
        def record_live(self, action, receipt):
            record = history["actions"][action]["Action"]
            if record["state"] != "Applying":
                raise HistoryClientError("InvalidTransition")
            record["live"] = dict(receipt)
            record["state"] = "Applied" if receipt["status"] == "Committed" else "NeedsReconciliation"
            history["live"].append((action, receipt))
        def complete(self, action, *, content, fingerprint):
            record = history["actions"][action]["Action"]
            if record["state"] not in {"Applied", "AfterCaptureFailed"} or record.get("live", {}).get("status") != "Committed":
                raise HistoryClientError("InvalidTransition")
            record["state"] = "Complete"
            history["completes"].append((action, content))
        def after_unavailable(self, action):
            history["actions"][action]["Action"]["state"] = "AfterCaptureFailed"
    monkeypatch.setattr(routes, "HistoryClient", History)
    monkeypatch.setenv("OPENCLANK_HISTORY_SOCKET", "/fixture/history.sock")
    return history


@pytest.mark.parametrize("after_write", [False, True])
def test_pending_crash_recovery_and_history_original_receipt(journey, monkeypatch, after_write):
    history = install_history(monkeypatch)
    async def run():
        app, bridge = journey
        app.state.auth_manager = SimpleNamespace(account_id=lambda _u: "fixture-account")
        doc = await create(bridge)
        wire = payload(doc)
        old_write = bridge._write_record
        def crash(vault, manifest, record, content, **kwargs):
            if after_write:
                old_write(vault, manifest, record, content, **kwargs)
            raise OSError("fixture crash at write boundary")
        monkeypatch.setattr(bridge, "_write_record", crash)
        failed = await write(app, doc, wire)
        assert failed.status_code == 503
        assert len(history["prepares"]) == 1
        interrupted = json.loads(bridge._guarded.path.read_text())
        assert interrupted["pending"] or interrupted["receipts"]
        monkeypatch.setattr(bridge, "_write_record", old_write)
        # Simulate a fresh process; existing write-ahead recovery is authority.
        fresh = LooseCopalBridge(bridge.data_dir)
        app.state.copal_bridge = fresh
        recovered = await write(app, doc, wire)
        assert recovered.status_code == 200, recovered.text
        assert recovered.json()["doc"]["text"] == "saved A"
        assert len(history["prepares"]) == 1, "replay never replaces original History preimage"
        assert len(history["completes"]) == 0
        assert recovered.json()["history"]["status"] == "failed"
        assert recovered.json()["history"]["durable"] is False
        assert recovered.json()["history"]["phase"] == "replay_reconciliation"
        later = await write(app, doc, payload(recovered.json()["doc"], "save-b", "later B"))
        assert later.status_code == 200
        replay = await write(app, doc, wire)
        assert replay.status_code == 200
        assert replay.json()["doc"] == recovered.json()["doc"]
        assert len(history["completes"]) == 1, "Unknown/partial original capture is never falsely completed"
        assert replay.json()["history"]["status"] == "failed"
        assert len(history["prepares"]) == 2
        assert not json.loads(fresh._guarded.path.read_text())["pending"]
    asyncio.run(run())


def test_pending_external_change_retains_same_action_partial_binding(journey, monkeypatch):
    async def run():
        app, bridge = journey
        doc = await create(bridge)
        wire = payload(doc)
        old_write = bridge._write_record
        monkeypatch.setattr(bridge, "_write_record", lambda *_a, **_kw: (_ for _ in ()).throw(OSError("fixture crash before write")))
        failed = await write(app, doc, wire)
        assert failed.status_code == 503
        monkeypatch.setattr(bridge, "_write_record", old_write)
        vault, manifest = bridge._scope({"owner": "fixture", "workspace_id": "default"})
        record = manifest["documents"][doc["id"]]
        (vault / record["path"]).write_text(routes._encode_note("external change", {}, []))
        fresh = LooseCopalBridge(bridge.data_dir)
        app.state.copal_bridge = fresh
        partial = await write(app, doc, wire)
        assert partial.status_code == 503
        assert partial.json()["detail"]["outcome"] == "partial"
        current = await fresh.call("get", {"owner": "fixture", "workspace_id": "default", "id": doc["id"]})
        assert routes._note_view(current)["text"] == "external change"
        changed = await write(app, doc, {**wire, "content": "different request"})
        assert changed.status_code == 409
    asyncio.run(run())


@pytest.mark.parametrize("mismatch", ["workspace_id", "resource_id", "actor_account_id", "tool_id"])
def test_history_replay_identity_mismatch_keeps_capture_untouched(journey, monkeypatch, mismatch):
    history = install_history(monkeypatch)
    async def run():
        app, bridge = journey
        app.state.auth_manager = SimpleNamespace(account_id=lambda _u: "fixture-account")
        doc = await create(bridge)
        wire = payload(doc)
        first = await write(app, doc, wire)
        assert first.status_code == 200
        action = history["actions"][wire["actionId"]]["Action"]
        if mismatch in {"workspace_id", "resource_id"}:
            action["resource_key"] = {**action["resource_key"], mismatch: "other"}
        else:
            action[mismatch] = "other"
        before = (len(history["prepares"]), len(history["live"]), len(history["completes"]))
        replay = await write(app, doc, wire)
        assert replay.status_code == 200
        assert replay.json()["doc"] == first.json()["doc"]
        assert replay.json()["history"]["status"] == "failed"
        assert replay.json()["history"]["phase"] == "replay_lookup"
        assert before == (len(history["prepares"]), len(history["live"]), len(history["completes"]))
    asyncio.run(run())


@pytest.mark.parametrize("state", ["Complete", "Applied", "AfterCaptureFailed", "NeedsReconciliation"])
def test_history_replay_honors_real_capture_states(journey, monkeypatch, state):
    history = install_history(monkeypatch)
    async def run():
        app, bridge = journey
        app.state.auth_manager = SimpleNamespace(account_id=lambda _u: "fixture-account")
        doc = await create(bridge)
        wire = payload(doc)
        first = await write(app, doc, wire)
        assert first.status_code == 200
        original_after = history["completes"][0][1]
        later = await write(app, doc, payload(first.json()["doc"], "save-b", "later B"))
        assert later.status_code == 200
        action = history["actions"][wire["actionId"]]["Action"]
        action["state"] = state
        if state == "NeedsReconciliation":
            action["live"]["status"] = "Unknown"
        before = (len(history["prepares"]), len(history["live"]), len(history["completes"]))
        replay = await write(app, doc, wire)
        assert replay.status_code == 200
        assert replay.json()["doc"] == first.json()["doc"]
        assert len(history["prepares"]) == before[0] and len(history["live"]) == before[1]
        if state in {"Applied", "AfterCaptureFailed"}:
            assert len(history["completes"]) == before[2] + 1
            assert history["completes"][-1][1] == original_after
            assert replay.json()["history"]["status"] == "complete"
        elif state == "Complete":
            assert len(history["completes"]) == before[2]
            assert replay.json()["history"]["status"] == "complete"
        else:
            assert len(history["completes"]) == before[2]
            assert replay.json()["history"]["status"] == "failed"
            assert replay.json()["history"]["durable"] is False
    asyncio.run(run())
