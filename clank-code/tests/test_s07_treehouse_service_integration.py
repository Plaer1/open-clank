"""S07 service seam: Files lesson preparation -> TreeHouse route CAS.

The browser qualification mounts production handlers separately because the
browser harness cannot mount the full authenticated application. These tests
drive the actual adapter, repository, and HTTP route with the same serialized
receipt fields emitted by those handlers.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from routes.copal_routes import setup_copal_routes
from src.openclank.copal_treehouse import new_treehouse_state
from src.openclank.copal_treehouse_repository import TreeHouseRepository
from src.openclank.file_policy import FilePolicyRepository
from src.openclank.files_facade import FilesFacade, FilesFacadeError, ProviderContext, ProviderResource
from src.openclank.resource_refs import issue_resource_ref
from src.openclank.treehouse_files_adapter import TreeHouseLessonAttachmentTarget


class _SourceProvider:
    name = "host"

    async def stat(self, context, *, origin_id):
        assert origin_id == "host-origin"
        return _source()


def _catalogue() -> dict:
    state = new_treehouse_state("acct-owner")
    state["profiles"]["acct-owner"] = state["profiles"].pop("owner")
    state["courses"]["course:one"] = {
        "id": "course:one",
        "title": "One",
        "ownerId": "acct-owner",
        "authorIds": ["acct-owner"],
        "status": "published",
        "moduleIds": ["module:one"],
    }
    state["modules"]["module:one"] = {
        "id": "module:one",
        "courseId": "course:one",
        "activityIds": ["lesson:one"],
        "assignmentIds": [],
    }
    state["activities"]["lesson:one"] = {
        "id": "lesson:one",
        "courseId": "course:one",
        "moduleId": "module:one",
        "title": "One",
        "status": "published",
    }
    return state


def _source() -> ProviderResource:
    return ProviderResource(
        "host-origin",
        "lesson.md",
        "file",
        ("stat", "read", "download"),
        mime_type="text/markdown",
        revision={"kind": "hostFingerprint", "value": "source-r1"},
    )


async def _facade_prepare(repo: TreeHouseRepository, policy: FilePolicyRepository, operation_id: str, *, generation: int | None = None) -> dict:
    current_generation = policy.generation() if generation is None else generation
    context = ProviderContext("acct-owner", "alice", current_generation, workspace_id="school")
    source = issue_resource_ref(
        owner_subject_id=context.owner_subject_id,
        provider="host",
        origin_id="host-origin",
        kind="file",
        capabilities=("stat", "download"),
        policy_generation=current_generation,
    )
    facade = FilesFacade(
        [_SourceProvider()],
        attachment_targets={"treehouse_lesson": TreeHouseLessonAttachmentTarget(repo)},
    )
    return await facade.prepare_attachment(
        context,
        operation_id=operation_id,
        generation=current_generation,
        source={"resource_ref": source.token, "expected_revision": _source().revision},
        target={
            "kind": "treehouse_lesson",
            "course_id": "course:one",
            "lesson_id": "lesson:one",
            "expected_revision": {"kind": "treehouse", "value": json.dumps({"grantRevision": 0, "catalogueRevision": 0}, separators=(",", ":"))},
        },
        mode="link",
    )


def _accounts() -> SimpleNamespace:
    return SimpleNamespace(
        is_configured=True,
        account_id=lambda username: {"alice": "acct-owner", "bob": "acct-learner"}.get(username),
        username_for_account_id=lambda account_id: {"acct-owner": "alice", "acct-learner": "bob"}.get(account_id),
    )


def _app(tmp_path, policy: FilePolicyRepository, treehouse: TreeHouseRepository) -> TestClient:
    app = FastAPI()
    app.state.auth_manager = _accounts()
    app.state.treehouse_repository = treehouse
    app.state.files_policy_repository = policy
    app.include_router(setup_copal_routes(policy_repository=policy))

    @app.middleware("http")
    async def identity(request, call_next):
        request.state.current_user = request.headers.get("x-treehouse-user", "alice")
        return await call_next(request)

    return TestClient(app)


def _command_payload(receipt: dict, operation_id: str) -> dict:
    # This is the exact snake_case receipt-to-command mapping used by the
    # mounted production TreeHouse client.
    return {
        "courseId": "course:one",
        "lessonId": "lesson:one",
        "operationId": operation_id,
        "preparationReceiptId": receipt["preparation_receipt_id"],
        "mode": "link",
        "expectedRevision": 0,
    }


@pytest.mark.asyncio
async def test_actual_adapter_receipt_and_route_cas_replay_once(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTH_ENABLED", "true")
    treehouse = TreeHouseRepository(tmp_path / "treehouse.sqlite3")
    treehouse.put_catalogue("acct-owner", "school", _catalogue(), expected_revision=None)
    policy = FilePolicyRepository(tmp_path / "policy.sqlite3")
    receipt = await _facade_prepare(treehouse, policy, "lesson-op")
    assert receipt["target_identity"] == {"kind": "treehouse_lesson", "course_id": "course:one", "lesson_id": "lesson:one"}
    assert receipt["source_revision"] == {"kind": "hostFingerprint", "value": "source-r1"}
    assert receipt["insertion"]["media_kind"] == "text/markdown"

    with _app(tmp_path, policy, treehouse) as http:
        body = {"type": "lesson.attach_source", "commandId": "lesson-command", "actorId": "acct-owner", "expectedRevision": 0, "payload": _command_payload(receipt, "lesson-op")}
        committed = http.post("/api/copal/treehouse/commands?workspace=school", json=body)
        replay = http.post("/api/copal/treehouse/commands?workspace=school", json=body)
    assert committed.status_code == 200, committed.text
    assert replay.status_code == 200, replay.text
    assert committed.json()["changed"] is True
    assert replay.json()["changed"] is False
    stored, revision = treehouse.get_catalogue("acct-owner", "school")
    assert revision == 1
    assert [item["operationId"] for item in stored["activities"]["lesson:one"]["sourceAttachments"]] == ["lesson-op"]


@pytest.mark.asyncio
async def test_policy_repository_is_single_authority_and_missing_registration_fails_closed(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTH_ENABLED", "true")
    treehouse = TreeHouseRepository(tmp_path / "treehouse.sqlite3")
    treehouse.put_catalogue("acct-owner", "school", _catalogue(), expected_revision=None)
    shared = FilePolicyRepository(tmp_path / "shared-policy.sqlite3")
    other = FilePolicyRepository(tmp_path / "other-policy.sqlite3")
    other.create_location(actor_subject_id="acct-owner", path=str(tmp_path / "source"), kind="directory", capabilities=("read",))
    mismatched = await _facade_prepare(treehouse, other, "mismatch-op")
    with _app(tmp_path, shared, treehouse) as http:
        body = {"type": "lesson.attach_source", "commandId": "mismatch-command", "actorId": "acct-owner", "expectedRevision": 0, "payload": _command_payload(mismatched, "mismatch-op")}
        response = http.post("/api/copal/treehouse/commands?workspace=school", json=body)
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "policy_generation_changed"
    assert treehouse.get_catalogue("acct-owner", "school")[1] == 0

    current_generation = shared.generation()
    propagated = await _facade_prepare(treehouse, shared, "propagated-op", generation=current_generation)
    shared.create_location(actor_subject_id="acct-owner", path=str(tmp_path / "revoked-source"), kind="directory", capabilities=("read",))
    assert shared.generation() == current_generation + 1
    with _app(tmp_path, shared, treehouse) as http:
        body = {"type": "lesson.attach_source", "commandId": "propagated-command", "actorId": "acct-owner", "expectedRevision": 0, "payload": _command_payload(propagated, "propagated-op")}
        response = http.post("/api/copal/treehouse/commands?workspace=school", json=body)
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "policy_generation_changed"


def test_treehouse_attachment_route_without_policy_registration_fails_closed(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTH_ENABLED", "true")
    treehouse = TreeHouseRepository(tmp_path / "treehouse.sqlite3")
    treehouse.put_catalogue("acct-owner", "school", _catalogue(), expected_revision=None)
    app = FastAPI()
    app.state.auth_manager = _accounts()
    app.state.treehouse_repository = treehouse
    app.include_router(setup_copal_routes())

    @app.middleware("http")
    async def identity(request, call_next):
        request.state.current_user = "alice"
        return await call_next(request)

    with TestClient(app) as http:
        response = http.post(
            "/api/copal/treehouse/commands?workspace=school",
            json={"type": "lesson.attach_source", "commandId": "missing-policy", "actorId": "acct-owner", "expectedRevision": 0, "payload": {"courseId": "course:one", "lessonId": "lesson:one", "operationId": "missing-policy", "preparationReceiptId": "treehouse-prep:missing", "mode": "link", "expectedRevision": 0}},
        )
    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "files_policy_unavailable"


@pytest.mark.asyncio
async def test_actual_facade_denies_learner_lesson_preparation(tmp_path):
    treehouse = TreeHouseRepository(tmp_path / "treehouse.sqlite3")
    treehouse.put_catalogue("acct-owner", "school", _catalogue(), expected_revision=None)
    share = treehouse.create_share(
        owner_account_id="acct-owner", workspace_id="school", course_id="course:one",
        recipient_account_id="acct-learner", role="learn", access_revision=0,
        now="now", command_id="share-learn", payload={"courseId": "course:one"},
    )
    treehouse.accept_share(recipient_account_id="acct-learner", workspace_id="school", token=share["shareToken"], now="now")
    grant = treehouse.grant(share["grantId"])
    assert grant and grant["role"] == "learn"
    policy = FilePolicyRepository(tmp_path / "policy.sqlite3")
    context = ProviderContext("acct-learner", "bob", policy.generation(), workspace_id="school")
    source = issue_resource_ref(owner_subject_id="acct-learner", provider="host", origin_id="host-origin", kind="file", capabilities=("stat", "download"), policy_generation=context.policy_generation)
    facade = FilesFacade([_SourceProvider()], attachment_targets={"treehouse_lesson": TreeHouseLessonAttachmentTarget(treehouse)})
    with pytest.raises(FilesFacadeError) as error:
        await facade.prepare_attachment(
            context,
            operation_id="learner-op",
            generation=context.policy_generation,
            source={"resource_ref": source.token, "expected_revision": _source().revision},
            target={"kind": "treehouse_lesson", "course_id": "course:one", "lesson_id": "lesson:one", "expected_revision": {"kind": "treehouse", "value": json.dumps({"grantRevision": int(grant["revision"]), "catalogueRevision": 0}, separators=(",", ":"))}},
            mode="link",
        )
    assert error.value.code == "resource_unavailable"
    assert treehouse.lesson_attachment_preparation(caller_account_id="acct-learner", workspace_id="school", operation_id="learner-op") is None
