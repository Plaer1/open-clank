import json
import hashlib
import copy
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from routes.copal_routes import setup_copal_routes
from src.openclank.copal_treehouse import new_treehouse_state
from src.openclank.copal_treehouse_repository import TreeHouseRepository


class TreeHouseBridge:
    def __init__(self, data_dir):
        self.data_dir = data_dir
        self.calls = []
        self.docs = {
            "LEGACY": {
                "id": "LEGACY", "kind": "lesson", "name": "Legacy.md", "head": "legacy-head",
                "text": "---\ncourse: Legacy Course\nskill: Legacy Skill\n---\n- [ ] Show it\n",
                "frontmatter": {"course": "Legacy Course", "skill": "Legacy Skill"},
                "treehouse": {"course": "Legacy Course", "skill": "Legacy Skill", "prerequisite": None},
                "tasks": [{"id": "LEGACY:6", "text": "Show it", "done": False}], "tags": [], "links": [],
            }
        }

    def is_alive(self):
        return True

    async def call(self, operation, args, timeout=20):
        self.calls.append((operation, dict(args), timeout))
        if operation == "index":
            docs = list(self.docs.values())
            if args.get("kind"):
                docs = [doc for doc in docs if doc["kind"] == args["kind"]]
            return {"docs": docs}
        if operation == "create":
            document_id = "STATE" if args["kind"] == "treehouse-state" else f"DOC{len(self.docs)}"
            if any(doc["name"] == args["name"] for doc in self.docs.values()):
                raise RuntimeError("already exists")
            self.docs[document_id] = {
                "id": document_id, "kind": args["kind"], "name": args["name"],
                "head": "head-1", "text": args.get("content", ""), "frontmatter": {}, "tags": [], "links": [],
            }
            return {"outcome": "created", "doc": self.docs[document_id]}
        if operation == "get":
            return self.docs[args["id"]]
        if operation == "write":
            doc = self.docs[args["id"]]
            if args.get("base") != doc["head"]:
                return {"outcome": "stale", "doc": doc}
            doc["text"] = args["content"]
            doc["head"] = f"head-{int(doc['head'].split('-')[-1]) + 1}"
            return {"outcome": "committed", "doc": doc}
        if operation == "export_snapshot":
            return {"docs": list(self.docs.values())}
        if operation == "status":
            return {"schema_version": 2, "documents": len(self.docs), "integrity_ok": True, "kinds": {}}
        if operation == "list":
            return {"docs": list(self.docs.values())}
        return {"ok": True}


def make_client(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTH_ENABLED", "false")
    app = FastAPI()
    app.include_router(setup_copal_routes())
    bridge = TreeHouseBridge(tmp_path)
    app.state.copal_bridge = bridge
    return TestClient(app), bridge


def command(http, command_type, payload, revision, command_id, actor="owner"):
    return http.post(
        "/api/copal/treehouse/commands?workspace=school",
        json={"type": command_type, "payload": payload, "actorId": actor, "commandId": command_id, "expectedRevision": revision},
    )


def test_treehouse_state_initializes_in_scoped_redb_and_commands_are_revisioned(tmp_path, monkeypatch):
    http, bridge = make_client(tmp_path, monkeypatch)
    initial = http.get("/api/copal/treehouse?workspace=school&actor=owner")
    assert initial.status_code == 200
    assert initial.json()["state"]["schemaVersion"] == 1
    assert initial.json()["state"]["revision"] == 0
    assert "STATE" not in bridge.docs  # pure GET must not initialize state

    created = command(http, "profile.create", {"id": "learner", "displayName": "Learner", "roles": ["learner"]}, 0, "create-profile")
    assert bridge.docs["STATE"]["kind"] == "treehouse-state"
    create_call = next(call for call in bridge.calls if call[0] == "create")
    assert create_call[1]["owner"] == "local"
    assert create_call[1]["workspace_id"] == "school"
    assert created.status_code == 200
    assert created.json()["result"]["revision"] == 1
    assert json.loads(bridge.docs["STATE"]["text"])["profiles"]["learner"]["roles"] == ["learner"]

    replay = command(http, "profile.create", {"id": "different", "displayName": "Different", "roles": ["learner"]}, 0, "create-profile")
    assert replay.status_code == 200
    assert replay.json()["changed"] is False
    assert replay.json()["result"]["replayed"] is True

    stale = command(http, "course.create", {"title": "Stale"}, 0, "stale-course")
    assert stale.status_code == 409
    assert stale.json()["detail"]["code"] == "stale"


def test_treehouse_role_boundaries_migration_and_integrity_routes(tmp_path, monkeypatch):
    http, bridge = make_client(tmp_path, monkeypatch)
    assert http.get("/api/copal/treehouse?workspace=school").status_code == 200
    created = command(http, "profile.create", {"id": "learner", "displayName": "Learner", "roles": ["learner"]}, 0, "profile")
    revision = created.json()["result"]["revision"]

    denied = command(http, "course.create", {"title": "No"}, revision, "denied", actor="learner")
    assert denied.status_code == 403
    assert denied.json()["detail"]["code"] == "forbidden"

    dry = http.post(
        "/api/copal/treehouse/migrate?workspace=school&dry_run=true",
        json={"actorId": "owner", "commandId": "migration", "expectedRevision": revision},
    )
    assert dry.status_code == 200
    assert dry.json()["plan"]["counts"] == {"documents": 1, "courses": 1, "skills": 1, "tasks": 1}
    assert json.loads(bridge.docs["STATE"]["text"])["courses"] == {}

    applied = http.post(
        "/api/copal/treehouse/migrate?workspace=school&dry_run=false",
        json={"actorId": "owner", "commandId": "migration", "expectedRevision": revision},
    )
    assert applied.status_code == 200
    assert applied.json()["result"]["imported"]["activities"] == 1
    integrity = http.get("/api/copal/treehouse/integrity?workspace=school")
    assert integrity.status_code == 200
    assert integrity.json()["ok"] is True
    assert integrity.json()["eventCount"] == 2  # profile + migration

    again = http.post(
        "/api/copal/treehouse/migrate?workspace=school&dry_run=true",
        json={"actorId": "owner", "commandId": "next", "expectedRevision": applied.json()["result"]["revision"]},
    )
    assert again.json()["plan"]["candidates"] == []


def _authenticated_treehouse_client(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.setenv("TREEHOUSE_REPOSITORY_PATH", str(tmp_path / "treehouse.sqlite3"))
    app = FastAPI()
    app.include_router(setup_copal_routes())

    class Accounts:
        is_configured = True
        accounts = {"alice": "acct-owner", "bob": "acct-bob", "cara": "acct-cara"}

        def account_id(self, username):
            return self.accounts.get(username)

        def username_for_account_id(self, account_id):
            return next((name for name, value in self.accounts.items() if value == account_id), None)

        def list_users(self):
            return [{"username": name, "account_id": account_id} for name, account_id in self.accounts.items()]

    app.state.auth_manager = Accounts()

    @app.middleware("http")
    async def identity(request, call_next):
        request.state.current_user = request.headers.get("x-treehouse-user", "alice")
        return await call_next(request)

    return TestClient(app)


def _seed_multi_account_shared_course(tmp_path):
    repo = TreeHouseRepository(tmp_path / "treehouse.sqlite3")
    owner = new_treehouse_state("acct-owner")
    owner["profiles"]["acct-owner"] = owner["profiles"].pop("owner")
    owner["courses"]["course:shared"] = {"id": "course:shared", "title": "Shared course", "ownerId": "acct-owner", "authorIds": ["acct-owner"], "status": "published", "moduleIds": []}
    owner["submissions"]["owner-submission"] = {"id": "owner-submission", "courseId": "course:shared", "profileId": "acct-owner", "status": "submitted"}
    owner["events"].append({"id": "owner-private-event", "type": "submission.submitted", "subjectId": "acct-owner", "entityId": "owner-submission", "data": {"courseId": "course:shared"}, "at": "2026-01-01T00:00:00Z"})
    repo.put_catalogue("acct-owner", "school", owner, expected_revision=None)
    shares = {}
    for account_id, command_id in (("acct-bob", "share-bob"), ("acct-cara", "share-cara")):
        share = repo.create_share(owner_account_id="acct-owner", workspace_id="school", course_id="course:shared", recipient_account_id=account_id, role="learn", access_revision=owner["revision"], now="now", command_id=command_id, payload={"courseId": "course:shared", "recipientId": account_id})
        repo.accept_share(recipient_account_id=account_id, workspace_id="school", token=share["shareToken"], now="now")
        shares[account_id] = repo.grant(share["grantId"])
    for account_id, marker in (("acct-bob", "bob"), ("acct-cara", "cara")):
        grant = shares[account_id]
        repo.put_progress(account_id, "acct-owner", "school", "course:shared", {
            "profiles": {account_id: {"id": account_id, "displayName": account_id, "roles": ["admin", "instructor", "learner"], "active": True}},
            "submissions": {f"{marker}-submission": {"id": f"{marker}-submission", "courseId": "course:shared", "profileId": account_id, "status": "submitted"}},
            "evidence": {f"{marker}-evidence": {"id": f"{marker}-evidence", "profileId": account_id, "skillId": "private-skill"}},
            "events": [{"id": f"{marker}-private-event", "type": "submission.submitted", "subjectId": account_id, "entityId": f"{marker}-submission", "data": {"courseId": "course:shared"}, "at": "2026-01-01T00:00:00Z"}],
            "enrollments": {}, "processedCommands": {}, "progressResets": {},
        }, expected_revision=0, grant_id=grant["grant_id"], access_revision=grant["revision"], expected_reset_epoch=0, reset_epoch=0)


def test_authenticated_shared_snapshot_is_recipient_only_and_learn_capability(tmp_path, monkeypatch):
    _seed_multi_account_shared_course(tmp_path)
    http = _authenticated_treehouse_client(tmp_path, monkeypatch)
    for username, account_id, own_marker, other_marker in (("bob", "acct-bob", "bob", "cara"), ("cara", "acct-cara", "cara", "bob")):
        response = http.get("/api/copal/treehouse?workspace=school", headers={"x-treehouse-user": username})
        assert response.status_code == 200
        body = response.json()
        assert body["accountId"] == account_id
        state = body["state"]
        assert set(state["submissions"]) == {f"{own_marker}-submission"}
        assert set(state["evidence"]) == {f"{own_marker}-evidence"}
        assert [event["id"] for event in state["events"] if event["type"] == "submission.submitted"] == [f"{own_marker}-private-event"]
        assert set(body["projection"]["learners"]) == {account_id}
        assert "course:shared" not in body["projection"]["courses"]
        capability = body["courseCapabilities"]["course:shared"]
        assert capability["learn"] is True and capability["edit"] is False
        assert f"{other_marker}-submission" not in json.dumps(body)
        assert f"{other_marker}-private-event" not in json.dumps(body)


def test_course_package_round_trip_is_curriculum_only_owner_scoped_and_replay_safe(tmp_path, monkeypatch):
    repo_path = tmp_path / "treehouse.sqlite3"
    monkeypatch.setenv("TREEHOUSE_REPOSITORY_PATH", str(repo_path))
    repo = TreeHouseRepository(repo_path)
    source = new_treehouse_state("acct-owner")
    source["courses"]["course:portable"] = {
        "id": "course:portable", "title": "Portable Course", "ownerId": "acct-owner",
        "authorIds": ["acct-owner"], "status": "published", "moduleIds": ["module:portable"], "prerequisites": ["course:prereq"],
    }
    source["courses"]["course:prereq"] = {
        "id": "course:prereq", "title": "Prerequisite", "ownerId": "acct-owner", "authorIds": ["acct-owner"], "status": "published", "moduleIds": [],
    }
    source["skills"]["skill:base"] = {"id": "skill:base", "title": "Base skill", "prerequisiteIds": []}
    source["skills"]["skill:advanced"] = {"id": "skill:advanced", "title": "Advanced skill", "prerequisiteIds": ["skill:base"]}
    source["modules"]["module:portable"] = {
        "id": "module:portable", "courseId": "course:portable", "title": "Module", "activityIds": ["activity:portable"], "assignmentIds": [],
    }
    source["activities"]["activity:portable"] = {
        "id": "activity:portable", "courseId": "course:portable", "moduleId": "module:portable", "title": "Read", "status": "published", "skillIds": ["skill:advanced"],
    }
    source["badges"]["badge:portable"] = {"id": "badge:portable", "title": "Reader", "criteria": {"type": "course", "courseId": "course:portable"}}
    source["badges"]["badge:prereq"] = {"id": "badge:prereq", "title": "Prerequisite reader", "criteria": {"type": "course", "courseId": "course:prereq"}}
    source["badges"]["badge:skill"] = {"id": "badge:skill", "title": "Skill reader", "criteria": {"type": "skill", "skillId": "skill:base"}}
    source["quests"]["quest:portable"] = {"id": "quest:portable", "title": "Read quest", "activityIds": ["activity:portable"], "assignmentIds": [], "status": "active"}
    source["badges"]["badge:quest"] = {"id": "badge:quest", "title": "Quest reader", "criteria": {"type": "quest", "questId": "quest:portable"}}
    source["profiles"]["owner"]["id"] = "acct-owner"
    source["profiles"]["owner"]["displayName"] = "Owner"
    source["submissions"]["private-submission"] = {"id": "private-submission", "courseId": "course:portable", "profileId": "acct-owner"}
    source["events"].append({"id": "private-event", "type": "submission.submitted", "subjectId": "acct-owner", "entityId": "private-submission", "data": {"courseId": "course:portable"}, "at": "2026-01-01T00:00:00Z"})
    repo.put_catalogue("acct-owner", "school", source, expected_revision=None)
    http = _authenticated_treehouse_client(tmp_path, monkeypatch)

    exported = http.get("/api/copal/treehouse/courses/course:portable/export?workspace=school", headers={"x-treehouse-user": "alice"})
    assert exported.status_code == 200, exported.text
    package = exported.json()
    assert package["format"] == "copal-treehouse-course-v1"
    assert "profiles" not in json.dumps(package)
    assert "private-submission" not in json.dumps(package)
    assert "ownerId" not in json.dumps(package)
    assert package["course"]["id"] == "course:portable"
    assert set(package["courses"]) == {"course:portable", "course:prereq"}
    assert set(package["skills"]) == {"skill:base", "skill:advanced"}
    assert "quest:portable" in package["quests"]
    assert {"badge:portable", "badge:prereq", "badge:skill", "badge:quest"} <= set(package["badges"])
    # Catalogue metadata revisions do not change curriculum identity.
    unrelated, unrelated_revision = repo.get_catalogue("acct-owner", "school")
    unrelated["extensions"]["unrelatedAuditMarker"] = "v2"
    unrelated["revision"] = unrelated_revision + 1
    repo.put_catalogue("acct-owner", "school", unrelated, expected_revision=unrelated_revision)
    reexported = http.get("/api/copal/treehouse/courses/course:portable/export?workspace=school", headers={"x-treehouse-user": "alice"})
    assert reexported.status_code == 200
    assert reexported.json()["sourceRevision"] != package["sourceRevision"]
    assert reexported.json()["packageId"] == package["packageId"]

    imported = http.post("/api/copal/treehouse/courses/import?workspace=school", json=package, headers={"x-treehouse-user": "bob"})
    assert imported.status_code == 200, imported.text
    body = imported.json()
    assert body["outcome"] == "applied"
    imported_course_id = body["courseId"]
    assert imported_course_id != "course:portable" or body["idMap"]["courses"]["course:portable"] == imported_course_id
    replay = http.post("/api/copal/treehouse/courses/import?workspace=school", json=package, headers={"x-treehouse-user": "bob"})
    assert replay.status_code == 200
    assert replay.json()["outcome"] == "replayed"
    assert replay.json()["revision"] == body["revision"]
    changed_package = json.loads(json.dumps(package))
    changed_package["course"]["title"] = "Tampered"
    changed = http.post("/api/copal/treehouse/courses/import?workspace=school", json=changed_package, headers={"x-treehouse-user": "bob"})
    assert changed.status_code == 409
    assert changed.json()["detail"]["code"] == "course_package_digest_mismatch"
    owner_view = http.get("/api/copal/treehouse?workspace=school", headers={"x-treehouse-user": "alice"}).json()
    bob_view = http.get("/api/copal/treehouse?workspace=school", headers={"x-treehouse-user": "bob"}).json()
    assert "private-submission" in json.dumps(owner_view)
    assert imported_course_id in bob_view["state"]["courses"]
    assert "private-submission" not in json.dumps(bob_view)

    def repack(candidate):
        unsigned = {key: value for key, value in candidate.items() if key not in {"packageId", "sourceRevision"}}
        candidate["packageId"] = hashlib.sha256(json.dumps(unsigned, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        return candidate

    text_id = copy.deepcopy(package)
    text_id["course"]["title"] = "course:portable"
    text_id["courses"]["course:portable"]["title"] = "course:portable"
    text_id = repack(text_id)
    text_result = http.post("/api/copal/treehouse/courses/import?workspace=school", json=text_id, headers={"x-treehouse-user": "bob"})
    assert text_result.status_code == 200, text_result.text
    text_course_id = text_result.json()["courseId"]
    bob_after_text = http.get("/api/copal/treehouse?workspace=school", headers={"x-treehouse-user": "bob"}).json()
    assert bob_after_text["state"]["courses"][text_course_id]["title"] == "course:portable"

    wrong_kind = copy.deepcopy(package)
    wrong_kind["modules"]["module:portable"]["activityIds"] = ["assignment:portable"]
    wrong_kind = repack(wrong_kind)
    rejected_kind = http.post("/api/copal/treehouse/courses/import?workspace=school", json=wrong_kind, headers={"x-treehouse-user": "bob"})
    assert rejected_kind.status_code == 409
    assert rejected_kind.json()["detail"]["code"] == "course_package_wrong_kind"

    wrong_owner = copy.deepcopy(package)
    wrong_owner["activities"]["activity:portable"]["courseId"] = "course:other"
    wrong_owner = repack(wrong_owner)
    rejected_owner = http.post("/api/copal/treehouse/courses/import?workspace=school", json=wrong_owner, headers={"x-treehouse-user": "bob"})
    assert rejected_owner.status_code == 409
    assert rejected_owner.json()["detail"]["code"] == "course_package_wrong_owner"

    cross_module = copy.deepcopy(package)
    cross_module["course"]["moduleIds"].append("module:second")
    cross_module["courses"]["course:portable"]["moduleIds"].append("module:second")
    cross_module["modules"]["module:second"] = {"id": "module:second", "courseId": "course:portable", "title": "Second", "activityIds": [], "assignmentIds": []}
    cross_module["activities"]["activity:portable"]["moduleId"] = "module:second"
    cross_module = repack(cross_module)
    rejected_cross_module = http.post("/api/copal/treehouse/courses/import?workspace=school", json=cross_module, headers={"x-treehouse-user": "bob"})
    assert rejected_cross_module.status_code == 409
    assert rejected_cross_module.json()["detail"]["code"] == "course_package_wrong_owner"

    wrong_badge = copy.deepcopy(package)
    wrong_badge["badges"]["badge:portable"]["criteria"] = {"type": "skill", "skillId": "activity:portable"}
    wrong_badge = repack(wrong_badge)
    rejected_badge = http.post("/api/copal/treehouse/courses/import?workspace=school", json=wrong_badge, headers={"x-treehouse-user": "bob"})
    assert rejected_badge.status_code == 409
    assert rejected_badge.json()["detail"]["code"] == "course_package_dangling_reference"


def test_auth_enabled_without_identity_store_fails_closed_for_treehouse_http(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.delenv("TREEHOUSE_REPOSITORY_PATH", raising=False)
    app = FastAPI()
    app.include_router(setup_copal_routes())
    app.state.auth_manager = SimpleNamespace(is_configured=False)
    response = TestClient(app).get("/api/copal/treehouse?workspace=school")
    assert response.status_code in {401, 503}
    if response.status_code == 503:
        assert response.json()["detail"]["code"] == "authentication_unavailable"
