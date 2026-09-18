from __future__ import annotations

import json
import hashlib
import zipfile

import pytest

import src.openclank.copal_manage as copal_manage_module
from src.openclank.copal_manage import CopalManageError, manage_copal
from src.openclank.copal_treehouse import TREEHOUSE_COMMAND_TYPES, new_treehouse_state


class ManageBridge:
    def __init__(self):
        self.calls = []
        self.docs = {
            "N1": {"id": "N1", "kind": "note", "corpus": "notes", "name": "A.md", "head": "h1", "text": "old"},
            "T1": {"id": "T1", "kind": "copal-tracks", "name": ".copal/tracks.json", "head": "t1", "text": json.dumps({"schemaVersion": 2, "tracks": [{"id": "a", "name": "A", "parentTrackId": None}, {"id": "b", "name": "B", "parentTrackId": None}]})},
        }

    async def call(self, operation, args, timeout=20):
        self.calls.append((operation, dict(args)))
        if operation == "index": return {"docs": list(self.docs.values())}
        if operation == "trash": return {"docs": []}
        if operation == "write":
            doc = self.docs[args["id"]]
            if args.get("base") != doc["head"]: return {"outcome": "stale", "doc": doc}
            doc["text"] = args["content"]; doc["head"] += "-next"
            return {"outcome": "committed", "doc": dict(doc)}
        if operation == "rename":
            doc = self.docs[args["id"]]; doc["name"] = args["name"]; return {"outcome": "committed", "doc": dict(doc)}
        if operation == "create":
            doc = {"id": "N2", "kind": args["kind"], "corpus": args["corpus"], "name": args["name"], "head": "h2", "text": args["content"]}; self.docs["N2"] = doc; return {"outcome": "created", "doc": dict(doc)}
        if operation in {"delete", "restore_deleted", "checkpoint"}: return {"outcome": "committed", "doc": self.docs.get(args["id"])}
        raise AssertionError(operation)


class HistoryRecorder:
    def __init__(self):
        self.prepares = []
        self.live = []
        self.completes = []
        self.rebinds = []

    def prepare(self, envelope, *, content, fingerprint):
        self.prepares.append((envelope, content, fingerprint))
        return {"Action": {"action_id": envelope["action_id"]}}

    def record_live(self, action_id, receipt):
        self.live.append((action_id, receipt))
        return {"Action": {"action_id": action_id}}

    def complete(self, action_id, *, content, fingerprint):
        self.completes.append((action_id, content, fingerprint))
        return {"Action": {"action_id": action_id}}

    def rebind_resource(self, action_id, resource_id):
        self.rebinds.append((action_id, resource_id))
        return {"Action": {"action_id": action_id}}


@pytest.mark.asyncio
async def test_manage_copal_history_hook_captures_one_typed_action_around_managed_write():
    bridge = ManageBridge()
    history = HistoryRecorder()
    result = await manage_copal(
        {"action": "notes.edit", "workspace": "home", "id": "N1", "content": "new"},
        owner="alice",
        account_id="alice",
        bridge=bridge,
        history_client=history,
    )
    assert result["saved"] is True
    assert len(history.prepares) == len(history.live) == len(history.completes) == 1
    envelope, before, _ = history.prepares[0]
    before_envelope = json.loads(before)
    assert before_envelope["text"] == "old"
    assert before_envelope["id"] == "N1"
    assert envelope["resource_key"] == {
        "account_id": "alice", "workspace_id": "home", "provider": "copal", "resource_id": "N1",
    }
    assert envelope["guard_resource_ids"] == []
    assert history.live[0][0] == history.completes[0][0] == envelope["action_id"]
    after = json.loads(json.loads(history.completes[0][1])["text"])
    assert after["schemaVersion"] == 1
    assert [block["text"] for block in after["body"]["blocks"]] == ["new"]
    assert after["properties"] == []
    assert after["relations"] == []


def test_history_capture_preserves_the_managed_copal_envelope():
    captured = copal_manage_module._HistoryMutationBridge._bytes({
        "text": "body",
        "properties": {"status": "active"},
        "relations": [{"target": "N2", "kind": "references"}],
        "extensions": {"calendar": {"color": "blue"}},
        "attachments": [{"id": "asset-1", "name": "diagram.png"}],
        "propertyDefinitions": [{"key": "status", "type": "text"}],
        "tags": ["planning"],
    })
    assert captured is not None
    envelope = json.loads(captured)
    assert envelope == {
        "text": "body",
        "properties": {"status": "active"},
        "relations": [{"target": "N2", "kind": "references"}],
        "extensions": {"calendar": {"color": "blue"}},
        "attachments": [{"id": "asset-1", "name": "diagram.png"}],
        "propertyDefinitions": [{"key": "status", "type": "text"}],
        "tags": ["planning"],
    }


class PausedHistoryRecorder(HistoryRecorder):
    def prepare(self, envelope, *, content, fingerprint):
        raise copal_manage_module.HistoryClientError("history_paused_budget: global target reached")


class FailedLiveBridge(ManageBridge):
    async def call(self, operation, args, timeout=20):
        if operation == "write":
            self.calls.append((operation, dict(args)))
            raise RuntimeError("injected Copal interruption")
        return await super().call(operation, args, timeout)


@pytest.mark.asyncio
async def test_manage_copal_interruption_records_not_committed_and_preserves_error():
    history = HistoryRecorder()
    with pytest.raises(RuntimeError, match="injected Copal interruption"):
        await manage_copal(
            {"action": "notes.edit", "workspace": "home", "id": "N1", "content": "new"},
            owner="alice",
            account_id="alice",
            bridge=FailedLiveBridge(),
            history_client=history,
        )
    assert len(history.prepares) == 1
    assert len(history.completes) == 0
    assert history.live == [(
        history.prepares[0][0]["action_id"],
        {
            "action_id": history.prepares[0][0]["action_id"],
            "status": "NotCommitted",
            "fingerprint": None,
            "after_unavailable": True,
            "resource_id": "N1",
        },
    )]


@pytest.mark.asyncio
async def test_history_budget_pause_is_reported_while_live_save_commits():
    result = await manage_copal(
        {"action": "notes.edit", "workspace": "home", "id": "N1", "content": "new"},
        owner="alice",
        account_id="acct-alice",
        bridge=ManageBridge(),
        history_client=PausedHistoryRecorder(),
    )
    assert result["saved"] is True
    assert result["data"]["history"]["status"] == "paused"
    assert result["data"]["history"]["phase"] == "budget"


class StructuredBridge(ManageBridge):
    def __init__(self):
        super().__init__()
        self.docs["N1"].update({
            "format": "copal-note-v1",
            "storage": "database",
            "text": "# Native\n- [ ] prove it",
            "blocks": [
                {"id": "blk_1", "type": "heading", "source": "# Native", "text": "Native", "level": 1},
                {"id": "blk_2", "type": "task", "source": "- [ ] prove it", "text": "prove it", "checked": False},
            ],
            "tasks": [{"id": "N1:blk_2", "line": 2, "done": False, "text": "prove it"}],
            "propertyDefinitions": [{"id": "prop_1", "key": "status", "type": "text", "value": "active"}],
            "properties": {"status": "active"},
            "relations": [],
            "tags": [],
            "extensions": {},
        })

    async def call(self, operation, args, timeout=20):
        if operation == "history":
            self.calls.append((operation, dict(args)))
            return {"commits": [{"id": "c0", "name": "Native"}]}
        if operation == "restore":
            self.calls.append((operation, dict(args)))
            return {"outcome": "committed", "doc": self.docs[args["id"]]}
        result = await super().call(operation, args, timeout)
        if operation == "write":
            self.docs[args["id"]]["format"] = "copal-note-v1"
        return result


class CopalSurfaceBridge(ManageBridge):
    def __init__(self):
        super().__init__()
        self.docs["B1"] = {
            "id": "B1", "kind": "base", "name": "Notes.base", "head": "b1",
            "text": "version: 1\nviews:\n  - id: table\n    name: Table\n    columns: [file.name, status]\n",
        }
        self.docs["R1"] = {
            "id": "R1", "kind": "markdown", "name": "Row.md", "head": "r1",
            "text": "---\nstatus: draft\n---\n# Row\n",
        }
        state = new_treehouse_state("alice")
        self.docs["TH1"] = {
            "id": "TH1", "kind": "treehouse-state", "name": ".copal/treehouse-state.json", "head": "th1",
            "text": json.dumps(state, separators=(",", ":")),
        }


class TransferBridge(ManageBridge):
    def __init__(self, data_dir):
        super().__init__()
        self.data_dir = data_dir
        self.docs = {
            "N1": {"id": "N1", "kind": "markdown", "corpus": "notes", "name": "A.md", "head": "h1", "text": "old"},
        }
        self.operation_docs = {}

    async def call(self, operation, args, timeout=20):
        if operation == "index":
            docs = [*self.docs.values(), *self.operation_docs.values()]
            if args.get("kind"):
                docs = [doc for doc in docs if doc.get("kind") == args["kind"]]
            return {"docs": docs}
        if operation == "create" and args.get("kind") == "copal-operation":
            op_id = f"OP{len(self.operation_docs) + 1}"
            self.operation_docs[op_id] = {"id": op_id, "kind": "copal-operation", "name": args["name"], "head": "op1", "text": args["content"]}
            return {"outcome": "created", "doc": dict(self.operation_docs[op_id])}
        if operation == "write" and args.get("id") in self.operation_docs:
            doc = self.operation_docs[args["id"]]
            if args.get("base") != doc["head"]:
                return {"outcome": "stale", "doc": dict(doc)}
            doc["text"] = args["content"]
            doc["head"] += "-next"
            return {"outcome": "committed", "doc": dict(doc)}
        if operation == "export_snapshot":
            return {"docs": list(self.docs.values())}
        if operation == "import_vault":
            root = __import__("pathlib").Path(args["path"])
            self.imported = sorted(str(path.relative_to(root)) for path in root.rglob("*") if path.is_file())
            return {"notes": 1, "assets": 0, "op": "IMPORT1"}
        return await super().call(operation, args, timeout)


@pytest.mark.asyncio
async def test_manage_copal_note_edit_uses_fresh_head_and_owner_scope():
    bridge = ManageBridge()
    result = await manage_copal({"action": "notes.edit", "workspace": "home", "id": "N1", "content": "new"}, owner="alice", bridge=bridge)
    assert result["saved"] is True
    write = next(args for op, args in bridge.calls if op == "write")
    assert write["owner"] == "alice" and write["workspace_id"] == "home" and write["base"] == "h1"


@pytest.mark.asyncio
async def test_manage_copal_track_reparent_preserves_registry_shape():
    bridge = ManageBridge()
    result = await manage_copal({"action": "timeline.track.reparent", "workspace": "home", "id": "b", "parentTrackId": "a"}, owner="alice", bridge=bridge)
    registry = result["data"]["registry"]
    assert registry["tracks"][1]["id"] == "b" and registry["tracks"][1]["parentTrackId"] == "a"
    assert json.loads(bridge.docs["T1"]["text"])["schemaVersion"] == 2


@pytest.mark.asyncio
async def test_manage_copal_bulk_trash_requires_server_preview_token():
    bridge = ManageBridge()
    preview = await manage_copal({"action": "maintenance.bulk_trash.preview", "workspace": "home", "ids": ["N1"]}, owner="alice", bridge=bridge)
    assert preview["preview"] is True
    assert any(op == "create" and args.get("kind") == "copal-operation" for op, args in bridge.calls)
    # Applying after the in-process cache is gone proves the operation record is
    # the authority across worker/process boundaries.
    copal_manage_module._PREVIEWS.clear()
    applied = await manage_copal({"action": "maintenance.bulk_trash.apply", "workspace": "home", "ids": ["N1"], "previewToken": preview["previewToken"]}, owner="alice", bridge=bridge)
    assert applied["saved"] is True
    with pytest.raises(CopalManageError) as error:
        await manage_copal({"action": "maintenance.bulk_trash.apply", "workspace": "home", "ids": ["N1"], "previewToken": "wrong"}, owner="alice", bridge=bridge)
    assert error.value.code == "invalid_preview"


@pytest.mark.asyncio
async def test_manage_copal_note_create_and_metadata_patch_keep_native_record():
    bridge = StructuredBridge()
    created = await manage_copal({"action": "notes.create", "workspace": "home", "name": "Native.md", "content": "# New", "properties": {"score": 9}}, owner="alice", bridge=bridge)
    assert created["saved"] is True
    create_call = next(args for op, args in bridge.calls if op == "create")
    record = json.loads(create_call["content"])
    assert record["schemaVersion"] == 1 and record["properties"][0]["value"] == 9

    patched = await manage_copal({"action": "notes.patch_metadata", "workspace": "home", "id": "N1", "patch": {"properties": {"score": 9}, "tags": ["native"]}}, owner="alice", bridge=bridge)
    assert patched["saved"] is True
    write = [args for op, args in bridge.calls if op == "write"][-1]
    record = json.loads(write["content"])
    assert record["schemaVersion"] == 1
    assert any(item["key"] == "score" and item["value"] == 9 for item in record["properties"])
    assert record["tags"] == ["native"]


@pytest.mark.asyncio
async def test_manage_copal_todo_completion_preserves_structured_storage():
    bridge = StructuredBridge()
    result = await manage_copal({"action": "todo.complete", "workspace": "home", "id": "N1:blk_2"}, owner="alice", bridge=bridge)
    assert result["data"]["done"] is True
    write = [args for op, args in bridge.calls if op == "write"][-1]
    assert json.loads(write["content"])["schemaVersion"] == 1


@pytest.mark.asyncio
async def test_manage_copal_graph_and_version_restore_are_exact_and_preview_bound():
    bridge = StructuredBridge()
    linked = await manage_copal({"action": "graph.link", "workspace": "home", "id": "N1", "patch": {"target": "Target", "kind": "link"}}, owner="alice", bridge=bridge)
    assert linked["saved"] is True
    record = json.loads([args for op, args in bridge.calls if op == "write"][-1]["content"])
    assert record["relations"][0]["target"] == "Target"

    preview = await manage_copal({"action": "notes.restore_version.preview", "workspace": "home", "id": "N1", "commitId": "c0"}, owner="alice", bridge=bridge)
    applied = await manage_copal({"action": "notes.restore_version.apply", "workspace": "home", "id": "N1", "commitId": "c0", "previewToken": preview["previewToken"]}, owner="alice", bridge=bridge)
    assert applied["saved"] is True
    assert any(op == "restore" for op, _ in bridge.calls)


@pytest.mark.asyncio
async def test_manage_copal_mind_heading_uses_structural_paths():
    bridge = ManageBridge()
    bridge.docs["N1"].update({"kind": "markdown", "text": "# A\n## B\n## C\n# D"})
    renamed = await manage_copal({"action": "mind.heading.rename", "workspace": "home", "id": "N1", "patch": {"path": ["A", "B"], "title": "Renamed"}}, owner="alice", bridge=bridge)
    assert renamed["saved"] is True
    assert "## Renamed" in bridge.docs["N1"]["text"]
    added = await manage_copal({"action": "mind.heading.add", "workspace": "home", "id": "N1", "patch": {"parentPath": ["A"], "title": "Child"}}, owner="alice", bridge=bridge)
    assert added["saved"] is True
    assert "## Child" in bridge.docs["N1"]["text"]


@pytest.mark.asyncio
async def test_manage_copal_bases_preview_apply_and_row_update_are_scoped():
    bridge = CopalSurfaceBridge()
    preview = await manage_copal({"action": "bases.migrate.preview", "workspace": "home", "id": "B1"}, owner="alice", bridge=bridge)
    assert preview["preview"] is True and preview["data"]["id"] == "B1"
    applied = await manage_copal({"action": "bases.migrate.apply", "workspace": "home", "id": "B1", "previewToken": preview["previewToken"]}, owner="alice", bridge=bridge)
    assert applied["saved"] is True
    updated = await manage_copal({"action": "bases.row.update", "workspace": "home", "id": "B1", "patch": {"baseId": "R1", "property": "status", "value": "done"}}, owner="alice", bridge=bridge)
    assert updated["data"]["rowId"] == "R1"
    assert "status:" in bridge.docs["R1"]["text"] and "done" in bridge.docs["R1"]["text"]


@pytest.mark.asyncio
async def test_manage_copal_treehouse_command_uses_owner_profile_and_revision():
    bridge = CopalSurfaceBridge()
    result = await manage_copal(
        {
            "action": "treehouse.command",
            "workspace": "home",
            "commandId": "cmd-1",
            "command": {"type": "skill.create", "payload": {"id": "skill-1", "title": "Native", "description": ""}},
            "expectedRevision": 0,
        },
        owner="alice",
        bridge=bridge,
    )
    assert result["saved"] is True and result["data"]["result"]["skillId"] == "skill-1"
    state = json.loads(bridge.docs["TH1"]["text"])
    assert state["revision"] == 1 and "skill-1" in state["skills"]


@pytest.mark.asyncio
async def test_manage_copal_rejects_unknown_treehouse_command_type():
    with pytest.raises(CopalManageError) as error:
        await manage_copal(
            {"action": "treehouse.command", "workspace": "home", "commandId": "cmd-unknown", "command": {"type": "shell.exec", "payload": {}}, "expectedRevision": 0},
            owner="alice",
            bridge=CopalSurfaceBridge(),
        )
    assert error.value.code == "unsupported_command"


@pytest.mark.asyncio
async def test_manage_copal_import_preview_apply_uses_owner_scoped_attachment(monkeypatch, tmp_path):
    import src.openclank.copal_transfer as transfer

    upload_root = tmp_path / "uploads"
    upload_root.mkdir()
    attachment_id = "a" * 32 + ".zip"
    archive_path = upload_root / attachment_id
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("Notes/Hello.md", "# Hello\n")
    (upload_root / "uploads.json").write_text(json.dumps({"row": {"id": attachment_id, "name": "vault.zip", "owner": "alice", "mime": "application/zip"}}), encoding="utf-8")
    monkeypatch.setattr(transfer, "UPLOAD_DIR", str(upload_root))
    monkeypatch.setattr(transfer, "DATA_DIR", str(tmp_path / "app-data"))
    bridge = TransferBridge(tmp_path / "copal")
    preview = await manage_copal({"action": "maintenance.import.preview", "workspace": "home", "attachmentId": attachment_id}, owner="alice", bridge=bridge)
    assert preview["preview"] is True and preview["data"]["files"] == 1
    applied = await manage_copal({"action": "maintenance.import.apply", "workspace": "home", "attachmentId": attachment_id, "previewToken": preview["previewToken"]}, owner="alice", bridge=bridge)
    assert applied["saved"] is True and bridge.imported == ["Notes/Hello.md"]


@pytest.mark.asyncio
async def test_manage_copal_export_preview_apply_returns_private_download_handle(tmp_path):
    bridge = TransferBridge(tmp_path / "copal")
    preview = await manage_copal({"action": "maintenance.export.preview", "workspace": "home"}, owner="alice", bridge=bridge)
    assert preview["preview"] is True and preview["data"]["documents"] == 1
    applied = await manage_copal({"action": "maintenance.export.apply", "workspace": "home", "previewToken": preview["previewToken"]}, owner="alice", bridge=bridge)
    result = applied["data"]
    assert applied["saved"] is False and result["downloadId"] and result["downloadUrl"].startswith("/api/copal/export/download/")
    artifact = (bridge.data_dir / "copal-transfers" / f"{result['downloadId']}.zip")
    assert artifact.is_file()
    with zipfile.ZipFile(artifact) as archive:
        assert "A.md" in archive.namelist()


@pytest.mark.asyncio
async def test_manage_copal_routes_every_treehouse_command_category():
    bridge = CopalSurfaceBridge()
    revision = 0

    async def command(kind, payload):
        nonlocal revision
        result = await manage_copal(
            {"action": "treehouse.command", "workspace": "home", "commandId": f"route-{revision}", "command": {"type": kind, "payload": payload}, "expectedRevision": revision},
            owner="alice",
            bridge=bridge,
        )
        revision += 1
        assert result["saved"] is True
        return result["data"]["result"]

    await command("profile.create", {"id": "teacher", "displayName": "Teacher", "roles": ["instructor"]})
    await command("profile.update", {"profileId": "teacher", "displayName": "Lead Teacher"})
    await command("skill.create", {"id": "skill:one", "title": "One"})
    await command("skill.update", {"skillId": "skill:one", "description": "Updated"})
    await command("course.create", {"id": "course:one", "title": "Course"})
    await command("course.update", {"courseId": "course:one", "description": "Updated"})
    await command("module.create", {"id": "module:one", "courseId": "course:one", "title": "Module"})
    await command("module.update", {"moduleId": "module:one", "description": "Updated"})
    await command("activity.create", {"id": "activity:one", "moduleId": "module:one", "title": "Activity", "skillIds": ["skill:one"]})
    await command("activity.update", {"activityId": "activity:one", "content": "Updated"})
    await command("assignment.create", {"id": "assignment:one", "moduleId": "module:one", "title": "Assignment", "skillIds": ["skill:one"]})
    await command("assignment.update", {"assignmentId": "assignment:one", "prompt": "Updated"})
    await command("assignment.publish", {"assignmentId": "assignment:one"})
    await command("module.reorder_items", {"moduleId": "module:one", "activityIds": ["activity:one"], "assignmentIds": ["assignment:one"]})
    await command("course.reorder_modules", {"courseId": "course:one", "moduleIds": ["module:one"]})
    await command("course.author.add", {"courseId": "course:one", "profileId": "teacher"})
    await command("course.publish", {"courseId": "course:one"})
    await command("enrollment.enroll", {"courseId": "course:one"})
    await command("activity.complete", {"activityId": "activity:one"})
    submission = await command("submission.submit", {"assignmentId": "assignment:one", "answer": "done"})
    await command("submission.grade", {"submissionId": submission["submissionId"], "score": 90})
    evidence = await command("evidence.submit", {"id": "evidence:one", "skillId": "skill:one", "description": "Proof"})
    await command("evidence.review", {"evidenceId": evidence["evidenceId"], "decision": "approved"})
    await command("badge.create", {"id": "badge:one", "title": "Badge", "criteria": {"type": "points", "threshold": 1}})
    await command("badge.update", {"badgeId": "badge:one", "description": "Updated"})
    await command("quest.create", {"id": "quest:one", "title": "Quest", "activityIds": ["activity:one"]})
    await command("quest.update", {"questId": "quest:one", "description": "Updated"})
    await command("quest.delete", {"questId": "quest:one"})
    await command("badge.delete", {"badgeId": "badge:one"})
    await command("assignment.delete", {"assignmentId": "assignment:one"})
    await command("activity.delete", {"activityId": "activity:one"})
    await command("skill.delete", {"skillId": "skill:one"})
    await command("enrollment.unenroll", {"courseId": "course:one"})
    await command("course.archive", {"courseId": "course:one"})
    await command("module.delete", {"moduleId": "module:one"})
    await command("course.delete", {"courseId": "course:one"})

    assert len(TREEHOUSE_COMMAND_TYPES) == 40
    assert revision == 36

@pytest.mark.asyncio
async def test_manage_copal_repeated_actions_get_fresh_ids_and_explicit_retry_is_stable():
    bridge = ManageBridge()
    history = HistoryRecorder()
    first = await manage_copal(
        {"action": "notes.rename", "workspace": "home", "id": "N1", "name": "B.md"},
        owner="alice", account_id="alice", bridge=bridge, history_client=history,
    )
    second = await manage_copal(
        {"action": "notes.rename", "workspace": "home", "id": "N1", "name": "C.md"},
        owner="alice", account_id="alice", bridge=bridge, history_client=history,
    )
    assert first["saved"] and second["saved"]
    assert len(history.prepares) == 2
    assert history.prepares[0][0]["action_id"] != history.prepares[1][0]["action_id"]

    retry_history = HistoryRecorder()
    retry_bridge = ManageBridge()
    for _ in range(2):
        await manage_copal(
            {"action": "notes.rename", "workspace": "home", "id": "N1", "name": "retry.md", "actionId": "copal-retry-1"},
            owner="alice", account_id="alice", bridge=retry_bridge, history_client=retry_history,
        )
    assert [item[0]["action_id"] for item in retry_history.prepares] == ["copal-retry-1", "copal-retry-1"]


class StaleWriteBridge(ManageBridge):
    async def call(self, operation, args, timeout=20):
        if operation == "write":
            self.calls.append((operation, dict(args)))
            return {"outcome": "stale", "doc": dict(self.docs[args["id"]])}
        return await super().call(operation, args, timeout)


@pytest.mark.asyncio
async def test_manage_copal_stale_live_outcome_is_recorded_as_conflict():
    history = HistoryRecorder()
    with pytest.raises(CopalManageError, match="changed before") as error:
        await manage_copal(
            {"action": "notes.edit", "workspace": "home", "id": "N1", "content": "new"},
            owner="alice", account_id="alice", bridge=StaleWriteBridge(), history_client=history,
        )
    assert error.value.code == "stale"
    assert len(history.prepares) == 1
    assert len(history.completes) == 0
    assert history.live[0][1]["status"] == "Conflict"

@pytest.mark.asyncio
async def test_manage_copal_create_rebinds_history_to_provider_identity():
    bridge = ManageBridge()
    history = HistoryRecorder()
    result = await manage_copal(
        {"action": "notes.create", "workspace": "home", "name": "new.md", "content": "created"},
        owner="alice", account_id="alice", bridge=bridge, history_client=history,
    )
    assert result["saved"] is True
    assert len(history.rebinds) == 1
    assert history.rebinds[0] == (history.prepares[0][0]["action_id"], "N2")

class UnavailableHistoryRecorder(HistoryRecorder):
    def prepare(self, envelope, *, content, fingerprint):
        raise OSError("history socket unavailable")


@pytest.mark.asyncio
async def test_manage_copal_transport_outage_keeps_live_save_usable():
    bridge = ManageBridge()
    result = await manage_copal(
        {"action": "notes.edit", "workspace": "home", "id": "N1", "content": "new"},
        owner="alice", account_id="alice", bridge=bridge, history_client=UnavailableHistoryRecorder(),
    )
    assert result["saved"] is True
    assert result["data"]["history"]["status"] == "failed"
    assert result["data"]["history"]["phase"] == "before"
    assert bridge.docs["N1"]["head"] == "h1-next"

@pytest.mark.asyncio
async def test_manage_copal_bulk_apply_captures_each_resource_as_its_own_action():
    bridge = ManageBridge()
    bridge.docs["N3"] = {"id": "N3", "kind": "note", "corpus": "notes", "name": "C.md", "head": "h3", "text": "third"}
    preview = await manage_copal(
        {"action": "maintenance.bulk_trash.preview", "workspace": "home", "ids": ["N1", "N3"]},
        owner="alice", bridge=bridge,
    )
    history = HistoryRecorder()
    applied = await manage_copal(
        {"action": "maintenance.bulk_trash.apply", "workspace": "home", "ids": ["N1", "N3"], "previewToken": preview["previewToken"]},
        owner="alice", account_id="alice", bridge=bridge, history_client=history,
    )
    assert applied["data"]["ids"] == ["N1", "N3"]
    deletes = [item for item in history.prepares if item[0]["operation"] == "delete"]
    assert [item[0]["resource_key"]["resource_id"] for item in deletes] == ["N1", "N3"]
    assert deletes[0][0]["action_id"] != deletes[1][0]["action_id"]
    delete_actions = {item[0]["action_id"] for item in deletes}
    assert len([item for item in history.live if item[0] in delete_actions]) == 2
    assert len([item for item in history.completes if item[0] in delete_actions]) == 2
