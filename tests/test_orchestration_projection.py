"""Synthetic ownership and pagination fixtures for O01's read projection."""
import pytest
import asyncio
import base64
import json

import httpx

from src.openclank.orchestration_projection import (
    ProjectionError,
    SessionBinding,
    build_projection,
    decode_cursor,
    encode_cursor,
    find_session_actor,
    resolve_binding,
    validate_actors,
    validate_tasks,
    session_http_path,
    orchestration_read_suffix,
    find_direct_peer,
)


def test_cross_owner_and_stale_map_are_rejected():
    data = {"mapRevision": 8, "chats": {"chat-a": {
        "owner": "alice", "current": "engine-a", "revision": 3,
    }}}
    with pytest.raises(ProjectionError, match="not mapped"):
        resolve_binding(data, "bob", "chat-a", 8)
    with pytest.raises(ProjectionError) as error:
        resolve_binding(data, "alice", "chat-a", 7)
    assert error.value.code == "STALE_MAP"
    with pytest.raises(ProjectionError) as error:
        resolve_binding({**data, "quarantine": ["chat-a"]}, "alice", "chat-a", 8)
    assert error.value.code == "CHAT_QUARANTINED"


def test_actor_membership_is_taken_from_native_session_actor_list():
    actors = [{"sessionID": "native-root", "actorID": "worker-1"}]
    assert find_session_actor(actors, "worker-1") == actors[0]
    assert find_session_actor(actors, "other-owner-actor") is None


def test_cursor_is_bound_to_chat_revision_actor_and_filter_for_paged_reads():
    binding = SessionBinding("chat-a", "engine-a", 8, 3)
    cursor = encode_cursor("opaque-native-before-token", binding, "worker-1", "tools")
    assert decode_cursor(cursor, binding, "worker-1", "tools") == "opaque-native-before-token"
    with pytest.raises(ProjectionError) as error:
        decode_cursor(cursor, binding, "worker-2", "tools")
    assert error.value.code == "CURSOR_SCOPE_MISMATCH"
    with pytest.raises(ProjectionError):
        decode_cursor(cursor, SessionBinding("chat-a", "engine-a", 9, 3), "worker-1", "tools")
    for invalid in (None, [], "wrong"):
        raw = base64.urlsafe_b64encode(json.dumps(invalid).encode()).decode().rstrip("=")
        with pytest.raises(ProjectionError) as error:
            decode_cursor(raw, binding, "worker-1", "tools")
        assert error.value.code in {"INVALID_CURSOR", "CURSOR_SCOPE_MISMATCH"}


def test_actor_and_task_parent_edges_stay_separate_and_workflow_absence_is_unknown():
    binding = SessionBinding("chat-a", "engine-a", 8, 3)
    projection = build_projection(
        binding,
        [{"actorID": "child", "sessionID": "engine-a", "parentActorID": "root"}],
        [{"id": "T1.1", "session_id": "engine-a", "parent_task_id": "T1"}],
        None,
        workflow_available=None,
    )
    assert projection["actorEdges"] == [{"kind": "actor_parent", "sessionId": "engine-a", "parentActorId": "root", "actorId": "child"}]
    assert projection["taskEdges"] == [{"kind": "task_parent", "sessionId": "engine-a", "parentTaskId": "T1", "taskId": "T1.1"}]
    assert projection["workflows"] == {
        "available": None, "runs": None, "reason": "runtime_availability_unreported"
    }
    unavailable = build_projection(binding, [], [], None, workflow_available=False)
    assert unavailable["workflows"] == {"available": False, "runs": None, "reason": "runtime_unavailable"}


def test_native_query_builder_preserves_message_filters_and_cursor():
    path = session_http_path(
        "native-session", "message?limit=50&agent_id=actor%2F1&before=opaque", "/workspace with spaces",
    )
    observed = {}

    async def handler(request):
        observed["url"] = request.url
        return httpx.Response(200, json=[])

    async def request_path():
        async with httpx.AsyncClient(base_url="http://engine", transport=httpx.MockTransport(handler)) as client:
            await client.get(path)

    asyncio.run(request_path())
    assert observed["url"].path == "/session/native-session/message"
    assert dict(observed["url"].params) == {
        "limit": "50", "agent_id": "actor/1", "before": "opaque", "directory": "/workspace with spaces",
    }


def test_registry_shapes_reject_cross_session_rows_before_projection():
    with pytest.raises(ProjectionError, match="cross-session actor"):
        validate_actors([{"actorID": "x", "sessionID": "other"}], "expected")
    with pytest.raises(ProjectionError, match="cross-session task"):
        validate_tasks([{"id": "T1", "session_id": "other"}], "expected")


def test_child_supervisor_reads_require_a_native_direct_peer_and_allowlisted_get_path():
    children = [{"id": "peer-a", "parentID": "root-a", "directory": "/peer"}]
    assert find_direct_peer(children, "root-a", "peer-a")["directory"] == "/peer"
    assert find_direct_peer(children, "other-root", "peer-a") is None
    assert find_direct_peer(children, "root-a", "guessed-peer") is None
    assert orchestration_read_suffix("message?limit=10&agent_id=peer-a&before=cursor")[0] == "message"
    for unsupported in ("resume", "../message", "message?actor_id=other", "message?before=x&before=y"):
        with pytest.raises(ValueError):
            orchestration_read_suffix(unsupported)


class _FakeSupervisor:
    def __init__(self, home):
        self.home = home
        self.calls = []
        self.mutate_map_on_task = False
        self.corrupt_map_on_task = False
        self.malformed_actors = False

    def _runtime_home(self, owner):
        return self.home

    async def session_http_request(self, session_id, method, suffix, *, owner, payload=None):
        self.calls.append(("root", session_id, method, suffix, owner))
        if suffix == "task" and self.mutate_map_on_task:
            data = json.loads((self.home / "session-map.json").read_text())
            data["mapRevision"] = 5
            (self.home / "session-map.json").write_text(json.dumps(data))
        if suffix == "task" and self.corrupt_map_on_task:
            (self.home / "session-map.json").write_text("not-json")
        data = {
            "actors": [], "task": [],
            "children?visible=true": [{"id": "peer-native", "parentID": "engine-a", "directory": "/peer"}],
        }.get(suffix, [])
        if suffix == "actors" and self.malformed_actors:
            data = {"actors": "not-an-array"}
        return httpx.Response(200, json=data)

    async def session_child_http_request(self, root_id, child_id, suffix, *, owner):
        self.calls.append(("child", root_id, child_id, suffix, owner))
        data = {
            "actors": [{"sessionID": "peer-native", "actorID": "peer-native", "mode": "peer",
                        "parentActorID": "main", "hostContext": {"chatID": "chat-a", "workspaceID": "ws",
                        "grantID": "private-grant", "accountID": "private-account", "cwd": "/private"}}],
            "task": [],
            "message?limit=20&agent_id=peer-native": [{"parts": [{"type": "tool", "tool": "read"}]}],
        }.get(suffix, [])
        headers = {"X-Next-Cursor": "native-cursor"} if suffix.startswith("message?") else None
        return httpx.Response(200, json=data, headers=headers)

    async def workflow_availability_request(self, root_id, native_id, *, owner):
        return httpx.Response(200, json={"available": True, "runs": [{"runID": "wf-run", "sessionID": native_id}]})


def test_route_projects_verified_peer_binding_and_scoped_message_page(tmp_path, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from routes import orchestration_routes

    home = tmp_path / "owner"
    home.mkdir()
    (home / "session-map.json").write_text(json.dumps({
        "version": 2, "mapRevision": 4,
        "chats": {"chat-a": {"owner": "alice", "current": "engine-a", "aliases": [], "revision": 2}},
    }))
    supervisor = _FakeSupervisor(home)
    app = FastAPI()
    app.state.mimo_supervisor = supervisor
    monkeypatch.setattr(orchestration_routes, "effective_user", lambda request: "alice")
    @app.middleware("http")
    async def authenticated(request, call_next):
        request.state.authenticated = True
        return await call_next(request)
    app.include_router(orchestration_routes.setup_orchestration_routes(
        None, owner_verifier=lambda *args, **kwargs: "alice",
        option_resolver=lambda owner, chat, actor, value: {
            "present": "modelOptions" in value, "routeID": "route-fixture",
            "optionRouteID": "route-fixture", "catalogRevision": 1,
            "model": {"providerID": "provider-fixture", "modelID": "model-fixture"},
            "options": value.get("modelOptions", {}).get("options", {}),
        },
    ))

    with TestClient(app) as client:
        response = client.get("/api/orchestration/chat-a")
        assert response.status_code == 200
        result = response.json()
        assert result["mapRevision"] == 4
    assert result["sessionBindings"] == [
        {"sessionId": "engine-a", "parentSessionId": None, "kind": "root"},
        {"sessionId": "peer-native", "parentSessionId": "engine-a", "kind": "peer_child"},
    ]
    child_actor = next(actor for actor in result["actors"] if actor["actorID"] == "peer-native")
    assert child_actor["sessionBinding"]["sessionId"] == "peer-native"
    assert child_actor["hostContext"] == {"chatID": "chat-a", "workspaceID": "ws"}
    assert result["workflows"]["sessions"][1]["runs"][0]["sessionID"] == "peer-native"

    with TestClient(app) as client:
        page_response = client.get(
            "/api/orchestration/chat-a/actors/peer-native/messages?map_revision=4&limit=20&tool_filter=tools&actor_session_id=peer-native",
        )
    assert page_response.status_code == 200
    page = page_response.json()
    assert page["actorSessionId"] == "peer-native"
    assert len(page["messages"]) == 1
    assert decode_cursor(page["nextCursor"], SessionBinding("chat-a", "engine-a", 4, 2),
                         "peer-native", "tools", "peer-native") == "native-cursor"
    assert any(call[0] == "child" and call[2] == "peer-native" for call in supervisor.calls)

    with TestClient(app) as client:
        malformed_cursor = client.get(
            "/api/orchestration/chat-a/actors/peer-native/messages?map_revision=4&limit=20&before=W10%3D&tool_filter=tools&actor_session_id=peer-native",
        )
        guessed_child = client.get(
            "/api/orchestration/chat-a/actors/guessed-actor/messages?map_revision=4&actor_session_id=guessed-peer",
        )
    assert malformed_cursor.status_code == 400
    assert guessed_child.status_code == 404

    supervisor.mutate_map_on_task = True
    with TestClient(app) as client:
        stale_response = client.get("/api/orchestration/chat-a?map_revision=4")
        supervisor.mutate_map_on_task = False
        data = json.loads((home / "session-map.json").read_text())
        data["mapRevision"] = 4
        (home / "session-map.json").write_text(json.dumps(data))
        assert stale_response.status_code == 409

        supervisor.malformed_actors = True
        malformed_registry = client.get("/api/orchestration/chat-a?map_revision=4")
        assert malformed_registry.status_code == 502
        assert malformed_registry.json()["detail"]["code"] == "ENGINE_INVALID_RESPONSE"
        supervisor.malformed_actors = False
        (home / "session-map.json").write_text(json.dumps({
            "version": 2, "mapRevision": 4,
            "chats": {"chat-a": {"owner": "alice", "current": "engine-a", "aliases": [], "revision": 2}},
        }))
        supervisor.corrupt_map_on_task = True
        corrupt_map = client.get("/api/orchestration/chat-a?map_revision=4")
        assert corrupt_map.status_code == 503
        assert corrupt_map.json()["detail"]["code"] == "SESSION_MAP_UNAVAILABLE"


def test_http_receipt_retry_keeps_client_identity_after_actor_advances(tmp_path, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from routes import orchestration_routes

    home = tmp_path / "owner"
    home.mkdir()
    (home / "session-map.json").write_text(json.dumps({
        "version": 2, "mapRevision": 4,
        "chats": {"chat-a": {"owner": "alice", "current": "engine-a", "aliases": [], "revision": 2}},
    }))

    class ReceiptSupervisor:
        def __init__(self):
            self.revision = 3
            self.origin_turn = "turn-1"
            self.admissions = []
            self.receipt = None

        def _runtime_home(self, owner):
            return home

        async def session_http_request(self, session_id, method, suffix, *, owner, payload=None):
            assert session_id == "chat-a" and owner == "alice"
            if suffix == "actors":
                body = [{"sessionID": "engine-a", "actorID": "main", "mode": "main",
                         "status": "running", "executionRevision": self.revision,
                         "hostContext": {"chatID": "chat-a"}}]
            elif suffix == "actors/main/input" and method == "GET":
                body = [self.receipt] if self.receipt else []
            elif suffix == "message?limit=1000&agent_id=main":
                body = [{"info": {"id": self.origin_turn, "role": "user"}}]
            elif suffix == "actors/main/input" and method == "POST":
                self.admissions.append(payload)
                if self.receipt is None:
                    self.receipt = {"requestID": payload["requestID"], "sessionID": "engine-a",
                                    "state": "pending-boundary", "messageID": "msg-1", "text": payload["text"],
                                        "executionRevision": payload["executionRevision"], "originTurnID": payload.get("originTurnID"),
                                        "mode": payload["mode"], "attachmentRefs": payload["attachmentRefs"],
                                    "attachmentUploadIDs": payload["attachmentUploadIDs"],
                                    "questionRefs": payload["questionRefs"],
                                    "modelRequestOptions": payload["modelRequestOptions"], "turnPolicy": payload.get("turnPolicy"), "attachmentCount": len(payload["attachmentParts"]),
                                    "attachmentDigests": ["fixture-digest"]}
                    # Simulate the first input being delivered and the actor advancing before HTTP retry.
                    self.revision += 1
                    self.origin_turn = "turn-2"
                    body = self.receipt
                else:
                    assert payload["requestID"] == self.receipt["requestID"]
                    assert payload["text"] == self.admissions[0]["text"]
                    assert payload["attachmentRefs"] == self.admissions[0]["attachmentRefs"]
                    assert payload["questionRefs"] == self.admissions[0]["questionRefs"]
                    if payload["mode"] == "steer":
                        self.receipt["mode"] = "steer"
                        body = {"requestID": payload["requestID"], "state": "pending-boundary", "mode": "steer"}
                    else:
                        body = self.receipt
            else:
                raise AssertionError(f"unexpected native request: {method} {suffix}")
            return httpx.Response(200, json=body)

    supervisor = ReceiptSupervisor()
    upload_path = tmp_path / "notes.txt"
    upload_path.write_text("trusted text resource")

    class UploadHandler:
        def __init__(self):
            self.claim_attempts = []
            self.resolves = 0
            self.allow = True

        def claim_upload_reference(self, upload_id, *, owner, reference_scope):
            self.claim_attempts.append((upload_id, owner, reference_scope))
            return {"id": upload_id} if self.allow and owner == "alice" else None

        def resolve_upload(self, upload_id, *, owner):
            self.resolves += 1
            if not self.allow or owner != "alice":
                return None
            return {"path": str(upload_path), "mime": "text/plain", "name": "notes.txt"}

        def _inside_upload_dir(self, path):
            return path == str(upload_path)

        def is_image_file(self, name, mime): return False
        def is_audio_file(self, name, mime): return False
        def is_document_file(self, name, mime): return True
        def settle_reference_claim(self, *args, **kwargs): pass

    upload_handler = UploadHandler()
    from src import tool_utils
    monkeypatch.setattr(tool_utils, "get_upload_handler", lambda: upload_handler)
    app = FastAPI()
    app.state.mimo_supervisor = supervisor
    monkeypatch.setattr(orchestration_routes, "effective_user", lambda request: "alice")

    @app.middleware("http")
    async def authenticated(request, call_next):
        request.state.authenticated = True
        return await call_next(request)

    app.include_router(orchestration_routes.setup_orchestration_routes(
        None, owner_verifier=lambda *args, **kwargs: "alice",
        option_resolver=lambda owner, chat, actor, value: {
            "present": "modelOptions" in value, "routeID": "route-fixture",
            "optionRouteID": "route-fixture", "catalogRevision": 1,
            "model": {"providerID": "provider-fixture", "modelID": "model-fixture"},
            "options": value.get("modelOptions", {}).get("options", {}),
        },
    ))
    policy = {"version": 1, "mode": "agent", "allowBash": False, "allowWebSearch": False,
              "useWeb": False, "useResearch": False, "compareMode": False, "incognito": False, "noMemory": False}
    body = {"requestID": "request-stable", "text": "same client input", "attachmentRefs": [],
            "turnPolicy": policy, "uploadIDs": ["upload-1"],
            "questionRefs": [], "mode": "after-turn",
            "modelOptions": {"route_id": "route-fixture", "catalog_revision": 1,
                             "options": {"reasoning_effort": "none"}}}
    with TestClient(app) as client:
        for flag in ("noMemory", "compareMode"):
            denied = client.post("/api/orchestration/chat-a/actors/main/input?map_revision=4", json={
                **body, "requestID": "unsupported-" + flag, "turnPolicy": {**policy, flag: True}})
            assert denied.status_code == 409
            assert denied.json()["detail"]["code"] == "INPUT_POLICY_UNAVAILABLE"
            assert not supervisor.admissions and not upload_handler.claim_attempts
        first = client.post("/api/orchestration/chat-a/actors/main/input?map_revision=4", json=body)
        retry = client.post("/api/orchestration/chat-a/actors/main/input?map_revision=4", json=body)
    assert first.status_code == retry.status_code == 200
    assert first.json()["receipt"] == retry.json()["receipt"] == supervisor.receipt
    assert first.json()["deliveryState"] == "pending-boundary"
    assert len(supervisor.admissions) == 1
    assert supervisor.admissions[0]["executionRevision"] == 3
    assert supervisor.admissions[0]["originTurnID"] == "turn-1"
    assert supervisor.admissions[0]["attachmentUploadIDs"] == ["upload-1"]
    assert supervisor.admissions[0]["attachmentParts"][-1]["mime"] == "text/plain"
    assert supervisor.admissions[0]["attachmentParts"][-1]["url"].startswith("data:text/plain;base64,")
    assert upload_handler.resolves == 1
    assert len(upload_handler.claim_attempts) == 1

    send_now = client.post("/api/orchestration/chat-a/actors/main/input?map_revision=4", json={
        **body, "mode": "steer", "handoffID": "deliberate-send-now",
    })
    assert send_now.status_code == 200
    assert supervisor.receipt["mode"] == "steer"
    assert len(supervisor.admissions) == 2
    assert supervisor.admissions[1]["attachmentParts"] == []
    assert upload_handler.resolves == 1

    # Legacy identity is retained and cannot be promoted or resumed under guessed privacy.
    supported_receipt = supervisor.receipt
    legacy_receipt = {key: value for key, value in supported_receipt.items() if key != "turnPolicy"}
    legacy_receipt["mode"] = "after-turn"
    supervisor.receipt = legacy_receipt
    legacy_body = {key: value for key, value in body.items() if key != "turnPolicy"}
    previous_admissions = len(supervisor.admissions)
    legacy = client.post("/api/orchestration/chat-a/actors/main/input?map_revision=4", json={**legacy_body, "mode": "steer"})
    assert legacy.status_code == 200 and legacy.json()["deliveryState"] == "unavailable"
    assert legacy.json()["receipt"] == legacy_receipt and legacy_receipt["mode"] == "after-turn"
    assert len(supervisor.admissions) == previous_admissions
    supervisor.receipt = supported_receipt

    changed_refs = client.post("/api/orchestration/chat-a/actors/main/input?map_revision=4", json={
        **body, "attachmentRefs": ["other-message/other-part"],
    })
    assert changed_refs.status_code == 409
    changed_options = client.post("/api/orchestration/chat-a/actors/main/input?map_revision=4", json={
        **body, "modelOptions": {"route_id": "route-fixture", "catalog_revision": 1,
                                 "options": {"reasoning_effort": "high"}},
    })
    assert changed_options.status_code == 409
    assert len(supervisor.admissions) == 2

    upload_handler.allow = False
    denied = client.post("/api/orchestration/chat-a/actors/main/input?map_revision=4", json={
        **body, "requestID": "request-denied",
    })
    assert denied.status_code == 409
    assert len(supervisor.admissions) == 2



def test_input_policy_resolver_refuses_unsupported_receipts_before_consumption(monkeypatch):
    from types import SimpleNamespace
    from routes import orchestration_routes as routes
    import src.openclank.browser_control_service as browser
    monkeypatch.setattr(browser, "configured_browser_control_service", lambda: None)
    projection = {"blockAllToolCalls": False, "disabledTools": [], "hiddenTools": [], "allowedTools": None}
    monkeypatch.setattr(routes, "effective_receipt_policy_ceiling", lambda *args, **kwargs: dict(projection))
    policy = {"version": 1, "mode": "agent", "allowBash": False, "allowWebSearch": False,
              "useWeb": False, "useResearch": False, "compareMode": False, "incognito": False, "noMemory": False}
    identity = {"sessionID": "engine", "rootOperationID": "root", "actorID": "main", "executionRevision": 1, "innerTurnID": "turn"}
    current = {"owner": "alice", "stableChatID": "chat", "sessionID": "engine", "rootSessionID": "engine", "actorID": "main",
               "authorityWorkspaceID": "workspace", "mapRevision": 2, "mappingRevision": 1, "workspaceRevision": 1,
               "authority": False}
    async def run(choice, ceiling=None):
        entry = {"rawPolicyCeiling": ceiling}
        class Bridge:
            _managed_actor_turns = {"key": entry}
            _managed_execution_lock = asyncio.Lock()
            _turns = {}
            def _managed_turn_key(self, value): return "key"
            async def resolve_managed_actor_execution(self, *args): return dict(current)
            def set_managed_input_policy_resolver(self, resolver): self.resolver = resolver
        bridge = Bridge()
        request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(auth_manager=None)))
        routes.install_input_policy_resolver(request, bridge, None)
        receipt = {"requestID": "receipt", "payloadHash": "a"*64, "executionRevision": 1,
                   "mapRevision": 2, "mappingRevision": 1, "workspaceRevision": 1,
                   "text": "follow up", "turnPolicy": choice, "admissionCeiling": dict(projection)}
        return await bridge.resolver(identity, [receipt])
    for choice in ({**policy, "noMemory": True}, {**policy, "compareMode": True}, None):
        result = asyncio.run(run(choice))
        assert result["batchDisposition"] == "unavailable"
        assert result["decisions"][0]["state"] == "unavailable" and result["decisions"][0].get("reason")
    supported = asyncio.run(run(policy))
    assert supported["batchDisposition"] == "ready" and supported["decisions"][0]["state"] == "ready"
    assert supported["toolProjection"]["blockAllToolCalls"] is True
    current_ceiling = dict(projection, choiceSnapshot=policy)
    admitted = asyncio.run(run(policy, current_ceiling))
    assert admitted["batchDisposition"] == "ready"
    assert admitted["toolProjection"]["blockAllToolCalls"] is False


def test_unified_observation_is_bounded_scoped_and_preserves_source_identity():
    from src.openclank.orchestration_projection import TaskObservationScope, project_task_observations, decode_task_observation_cursor

    scope = TaskObservationScope("alice", SessionBinding("chat-a", "root", 8, 3), ("root", "peer"), "ws")
    pages = {
        "native_actor": {"availability": "partial", "reason": "next_page", "observedAt": 100,
                         "nextCursor": "actor-next", "items": [
            {"sessionID": "root", "actorID": "worker", "status": "running", "executionRevision": 4,
             "description": "Worker", "hostContext": {"chatID": "chat-a", "workspaceID": "ws", "accountID": "secret"}},
        ]},
        "native_task": {"availability": "available", "observedAt": 101, "items": [
            {"session_id": "root", "id": "T1", "status": "open", "summary": "Assigned", "owner": "worker", "last_event_at": 9, "prompt": "secret"},
            {"session_id": "peer", "id": "T1", "status": "open", "summary": "Unassigned", "last_event_at": 10},
        ]},
    }
    result = project_task_observations(scope, pages)
    tasks = result["sources"]["native_task"]["items"]
    assert [row["sourceRef"]["sessionId"] for row in tasks] == ["root", "peer"]
    assert tasks[0]["displayCategory"] == "commitment" and tasks[1]["links"] == []
    assert tasks[0]["links"][0]["target"] == {"kind": "native_actor", "sessionId": "root", "nativeId": "worker"}
    assert result["sources"]["native_actor"]["items"][0]["displayCategory"] == "execution"
    assert result["sources"]["native_actor"]["availability"] == "partial"
    cursor = result["sources"]["native_actor"]["nextCursor"]
    assert decode_task_observation_cursor(scope, "native_actor", cursor) == "actor-next"
    with pytest.raises(ProjectionError):
        decode_task_observation_cursor(scope, "native_task", cursor)
    with pytest.raises(ProjectionError):
        decode_task_observation_cursor(TaskObservationScope("bob", scope.binding, scope.verified_session_ids, "ws"), "native_actor", cursor)
    assert result["sources"]["copal"] == {"availability": "unavailable", "reason": "adapter_not_installed", "items": []}
    assert "secret" not in json.dumps(result)
    assert project_task_observations(scope, {"native_task": {"availability": "available", "observedAt": 1, "items": []}})["sources"]["native_task"]["items"] == []
    with pytest.raises(ProjectionError):
        project_task_observations(scope, {"native_task": {"availability": "available", "observedAt": 1, "items": [pages["native_task"]["items"][0] | {"session_id": "other"}]}})
    with pytest.raises(ProjectionError):
        project_task_observations(scope, {"native_task": {"availability": "available", "observedAt": 1, "items": [pages["native_task"]["items"][0]] * 101}})
    with pytest.raises(ProjectionError):
        project_task_observations(TaskObservationScope("", scope.binding, scope.verified_session_ids), pages)


def _observe(actors, tasks=(), sessions=("root", "peer", "other")):
    from src.openclank.orchestration_projection import TaskObservationScope, project_task_observations
    scope = TaskObservationScope("alice", SessionBinding("chat-a", "root", 8, 3), tuple(sessions), "ws")
    base = {"status": "idle", "executionRevision": 1, "description": "d"}
    pages = {"native_actor": {"availability": "available", "observedAt": 1, "items": [{**base, **a} for a in actors]},
             "native_task": {"availability": "available", "observedAt": 1, "items": [{"status": "open", "summary": "t", **t} for t in tasks]}}
    result = project_task_observations(scope, pages)["sources"]
    return ({row["sourceRef"]["nativeId"] + "@" + row["sourceRef"]["sessionId"]: row for row in result["native_actor"]["items"]},
            {row["sourceRef"]["nativeId"] + "@" + row["sourceRef"]["sessionId"]: row for row in result["native_task"]["items"]})


def test_actor_parent_links_resolve_only_inside_the_verified_projection():
    actors, _ = _observe([
        {"sessionID": "root", "actorID": "child", "parentActorID": "main"},   # parent appears later in the page
        {"sessionID": "root", "actorID": "main"},
        {"sessionID": "root", "actorID": "orphan", "parentActorID": "gone"},   # parent not verified here
        {"sessionID": "other", "actorID": "lost", "parentActorID": "dup"},     # id verified in two other sessions
        {"sessionID": "root", "actorID": "dup"}, {"sessionID": "peer", "actorID": "dup"},
        {"sessionID": "peer", "actorID": "pchild", "parentActorID": "dup"},    # same-session parent wins
    ])
    assert actors["child@root"]["links"] == [{"kind": "actor_parent", "target": {"kind": "native_actor", "sessionId": "root", "nativeId": "main"},
                                              "provenance": "native_actor.parentActorID"}]
    assert actors["child@root"]["linkCoverage"] == "observed" and actors["main@root"]["linkCoverage"] == "observed"
    assert actors["orphan@root"]["links"] == [] and actors["orphan@root"]["linkCoverage"] == "unknown"
    assert actors["lost@other"]["links"] == [] and actors["lost@other"]["linkCoverage"] == "unknown"
    assert actors["pchild@peer"]["links"][0]["target"]["sessionId"] == "peer"
    assert all("parentActorID" not in row for row in actors.values())  # no raw field passes through


def test_task_parent_links_stay_inside_one_session_and_coverage_follows_owner_and_parent():
    _, tasks = _observe([{"sessionID": "root", "actorID": "worker"}], [
        {"session_id": "root", "id": "c", "parent_task_id": "p", "owner": "worker"},
        {"session_id": "root", "id": "p"},
        {"session_id": "root", "id": "x", "parent_task_id": "missing"},
        {"session_id": "peer", "id": "y", "parent_task_id": "p"},  # parent exists only in another session
    ])
    assert [l["kind"] for l in tasks["c@root"]["links"]] == ["assigned_to", "task_parent"]
    assert tasks["c@root"]["links"][1] == {"kind": "task_parent", "target": {"kind": "native_task", "sessionId": "root", "nativeId": "p"},
                                          "provenance": "native_task.parent_task_id"}
    assert tasks["c@root"]["linkCoverage"] == "observed"
    assert tasks["x@root"]["links"] == [] and tasks["x@root"]["linkCoverage"] == "unknown"
    assert tasks["y@peer"]["links"] == [] and tasks["y@peer"]["linkCoverage"] == "unknown"
    assert all("parent_task_id" not in row for row in tasks.values())


def test_actor_models_are_projected_in_closed_form_absent_is_unknown_and_malformed_is_rejected():
    actors, _ = _observe([
        {"sessionID": "root", "actorID": "a", "requestedModel": "p/big", "effectiveModel": {"providerID": "p", "modelID": "small"}},
        {"sessionID": "root", "actorID": "b"},
    ])
    assert actors["a@root"]["requestedModel"] == "p/big" and actors["a@root"]["effectiveModel"] == {"providerID": "p", "modelID": "small"}
    assert "requestedModel" not in actors["b@root"] and "effectiveModel" not in actors["b@root"]
    # present-but-malformed rejects the row, like lastOutcome and display do
    for bad in ({"requestedModel": ""}, {"requestedModel": "x" * 201}, {"requestedModel": "a\nb"}, {"requestedModel": 5},
                {"effectiveModel": "p/m"}, {"effectiveModel": {"providerID": "p"}}, {"effectiveModel": {"providerID": "p", "modelID": "m", "x": 1}},
                {"effectiveModel": {"providerID": "", "modelID": "m"}}, {"parentActorID": " pad "}):
        with pytest.raises(ProjectionError):
            _observe([{"sessionID": "root", "actorID": "a", **bad}])


def test_observation_route_reads_one_verified_page_and_keeps_source_scope(tmp_path, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from routes import orchestration_routes

    home = tmp_path / "owner"
    home.mkdir()
    (home / "session-map.json").write_text(json.dumps({"version": 2, "mapRevision": 4,
        "chats": {"chat-a": {"owner": "alice", "current": "engine-a", "aliases": [], "revision": 2}}}))

    class Supervisor:
        def __init__(self): self.calls = []
        def _runtime_home(self, owner): return home
        async def session_observation_request(self, chat, target, source, limit, after, *, owner):
            self.calls.append((chat, target, source, limit, after, owner))
            if target == "guessed": raise PermissionError("not peer")
            if target == "invalid": return httpx.Response(200, json={"items": [{"id": "T1", "session_id": "other"}], "observedAt": 42})
            if source == "actor" and target == "peer":
                return httpx.Response(200, json={"items": [{"sessionID": "peer", "actorID": "worker", "status": "idle",
                    "lastOutcome": "success", "executionRevision": 2, "description": "Worker"}], "observedAt": 43})
            if source == "actor": return httpx.Response(404, json={"error": "older engine"})
            items = [{"session_id": target, "id": "T1" if after is None else "T2",
                      "status": "open", "summary": "Visible", "owner": "unseen-worker", "last_event_at": 1}]
            return httpx.Response(200, json={"items": items, "observedAt": 42,
                                           **({"nextCursor": "T1"} if after is None else {})})

    supervisor = Supervisor()
    app = FastAPI()
    app.state.mimo_supervisor = supervisor
    monkeypatch.setattr(orchestration_routes, "effective_user", lambda request: request.headers.get("X-Owner", "alice"))
    @app.middleware("http")
    async def authenticated(request, call_next):
        request.state.authenticated = True
        return await call_next(request)
    app.include_router(orchestration_routes.setup_orchestration_routes(None, owner_verifier=lambda *args, **kwargs: None))
    async def exercise():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://fixture") as client:
            first = await client.get("/api/orchestration/chat-a/observations/native_task?target_session_id=peer&limit=1")
            assert first.status_code == 200
            page = first.json()["sources"]["native_task"]
            assert page["availability"] == "partial" and page["items"][0]["linkCoverage"] == "unknown"
            assert page["items"][0]["sourceRef"] == {"kind": "native_task", "sessionId": "peer", "nativeId": "T1"}
            cursor = page["nextCursor"]
            second = await client.get("/api/orchestration/chat-a/observations/native_task", params={"target_session_id": "peer", "limit": 1, "cursor": cursor})
            assert second.status_code == 200 and second.json()["sources"]["native_task"]["items"][0]["sourceRef"]["nativeId"] == "T2"
            assert (await client.get("/api/orchestration/chat-a/observations/native_task", params={"target_session_id": "other", "cursor": cursor})).status_code == 400
            assert (await client.get("/api/orchestration/chat-a/observations/native_task?target_session_id=guessed")).status_code == 404
            invalid = await client.get("/api/orchestration/chat-a/observations/native_task?target_session_id=invalid")
            assert invalid.status_code == 502 and invalid.json()["detail"]["code"] == "INVALID_OBSERVATION_ROW"
            denied = await client.get("/api/orchestration/chat-a/observations/native_task", headers={"X-Owner": "bob"})
            assert denied.status_code in {403, 404, 503} and "Visible" not in denied.text
            actor = await client.get("/api/orchestration/chat-a/observations/native_actor?target_session_id=peer")
            assert actor.status_code == 200 and actor.json()["sources"]["native_actor"]["items"][0]["nativeOutcome"] == "success"
            missing = await client.get("/api/orchestration/chat-a/observations/native_actor")
            assert missing.status_code == 200 and missing.json()["sources"]["native_actor"]["availability"] == "unavailable"
    asyncio.run(exercise())
    assert supervisor.calls == [("chat-a", "peer", "task", 1, None, "alice"),
                                ("chat-a", "peer", "task", 1, "T1", "alice"),
                                ("chat-a", "guessed", "task", 50, None, "alice"),
                                ("chat-a", "invalid", "task", 50, None, "alice"),
                                ("chat-a", "peer", "actor", 50, None, "alice"),
                                ("chat-a", "engine-a", "actor", 50, None, "alice")]
