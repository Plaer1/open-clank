import json

from fastapi import FastAPI
from fastapi.testclient import TestClient

from routes.copal_routes import setup_copal_routes


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
    assert bridge.docs["STATE"]["kind"] == "treehouse-state"
    create_call = next(call for call in bridge.calls if call[0] == "create")
    assert create_call[1]["owner"] == "local"
    assert create_call[1]["workspace_id"] == "school"

    created = command(http, "profile.create", {"id": "learner", "displayName": "Learner", "roles": ["learner"]}, 0, "create-profile")
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
