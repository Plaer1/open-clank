"""Provider-backed memory audit uses provider mutations, not memory.json."""

import asyncio
import json

from src.memory_provider import MemoryRecord
from services.memory.memory_extractor import audit_memories, audit_provider_memories


class FakeProvider:
    def __init__(self):
        self.records = [
            MemoryRecord(id="keep", text="User likes tea", category="preference"),
            MemoryRecord(id="edit", text="User works in sales", category="fact"),
            MemoryRecord(id="remove", text="The assistant used markdown", category="fact"),
        ]
        self.updates = []
        self.deletes = []

    async def list_memories(self, *, owner=None, limit=100):
        return list(self.records[:limit])

    async def update(self, memory_id, *, text=None, category=None, owner=None):
        for record in self.records:
            if record.id == memory_id:
                record.text = text or record.text
                record.category = category or record.category
                self.updates.append((memory_id, record.text, record.category))
                return record
        return None

    async def delete(self, memory_id, *, owner=None):
        self.deletes.append(memory_id)
        self.records = [record for record in self.records if record.id != memory_id]
        return True


class FakeLifecycle:
    def __init__(self, provider):
        self.provider = provider
        self.deletes = []
        self.restores = []

    async def delete(self, memory_id, *, owner=None, workspace_id=None):
        self.deletes.append((memory_id, owner, workspace_id))
        deleted = await self.provider.delete(memory_id, owner=owner)
        if not deleted:
            return None
        return {"tombstone_id": f"tombstone-{memory_id}"}

    async def forget(self, action, *, owner=None, tombstone_id=None, **kwargs):
        self.restores.append((action, owner, tombstone_id, kwargs))
        return {"restored": action == "restore"}


class FakeNativeMemoryManager:
    def __init__(self, tmp_path):
        self.memory_file = str(tmp_path / "memory.json")
        self.entries = [
            {
                "id": "keep",
                "text": "User likes tea",
                "category": "preference",
                "owner": "alice",
            },
            {
                "id": "remove",
                "text": "The assistant used markdown",
                "category": "fact",
                "owner": "alice",
            },
        ]
        self.saved = []

    def load(self, *, owner=None):
        return [entry.copy() for entry in self.entries if entry.get("owner") == owner]

    def load_all(self):
        return [entry.copy() for entry in self.entries]

    def save(self, entries):
        self.saved.append([entry.copy() for entry in entries])
        self.entries = [entry.copy() for entry in entries]


def test_provider_audit_updates_and_deletes_active_records(monkeypatch):
    completion_calls = []

    async def fake_complete_text(**kwargs):
        completion_calls.append(kwargs)
        return (
            '{"operations":['
            '{"id":"keep","action":"keep","text":"User likes tea","category":"preference"},'
            ' {"id":"edit","action":"keep","text":"User works in enterprise sales","category":"project"},'
            ' {"id":"remove","action":"delete","reason":"assistant-only activity"}'
            ']}'
        )

    monkeypatch.setattr(
        "services.memory.memory_extractor._complete_text",
        fake_complete_text,
    )
    provider = FakeProvider()
    lifecycle = FakeLifecycle(provider)

    result = asyncio.run(audit_provider_memories(
        provider,
        "http://llm",
        "model",
        owner="alice",
        memory_lifecycle=lifecycle,
        root_operation_id="root_audit_1",
    ))

    assert result["ok"] is True
    assert result["status"] == "applied"
    assert result["before"] == 3
    assert result["after"] == 2
    assert result["removed"] == 1
    assert result["updated"] == 1
    assert result["applied"] is True
    assert completion_calls[0]["owner"] == "alice"
    assert completion_calls[0]["purpose"] == "memory"
    assert completion_calls[0]["root_operation_id"] == "root_audit_1"
    assert "endpoint_url" not in completion_calls[0]
    assert "headers" not in completion_calls[0]
    assert provider.updates == [("edit", "User works in enterprise sales", "project")]
    assert provider.deletes == ["remove"]
    assert lifecycle.deletes == [("remove", "alice", None)]


def _all_keep_operations():
    return {
        "operations": [
            {
                "id": "keep",
                "action": "keep",
                "text": "User likes tea",
                "category": "preference",
            },
            {
                "id": "edit",
                "action": "keep",
                "text": "User works in sales",
                "category": "fact",
            },
            {
                "id": "remove",
                "action": "keep",
                "text": "The assistant used markdown",
                "category": "fact",
            },
        ]
    }


def _run_audit(monkeypatch, output, *, apply=True, lifecycle=True):
    async def fake_complete_text(**kwargs):
        if isinstance(output, BaseException):
            raise output
        return output

    monkeypatch.setattr(
        "services.memory.memory_extractor._complete_text",
        fake_complete_text,
    )
    provider = FakeProvider()
    coordinator = FakeLifecycle(provider) if lifecycle else None
    result = asyncio.run(audit_provider_memories(
        provider,
        owner="alice",
        memory_lifecycle=coordinator,
        apply=apply,
    ))
    return result, provider, coordinator


def test_provider_audit_empty_model_output_is_typed_failure_without_mutation(monkeypatch):
    result, provider, lifecycle = _run_audit(monkeypatch, "")

    assert result == {
        "ok": False,
        "status": "failed",
        "before": 3,
        "after": 3,
        "removed": 0,
        "updated": 0,
        "applied": False,
        "already_tidy": False,
        "error": {
            "code": "empty_model_output",
            "message": "The memory model returned no usable Tidy result. No memories were changed.",
        },
    }
    assert provider.updates == []
    assert provider.deletes == []
    assert lifecycle.deletes == []


def test_provider_audit_non_json_and_incomplete_output_are_non_destructive(monkeypatch):
    result, provider, lifecycle = _run_audit(monkeypatch, "<html>Bad Gateway</html>")

    assert result["ok"] is False
    assert result["status"] == "failed"
    assert result["error"]["code"] == "invalid_model_output"
    assert result["before"] == result["after"] == 3
    assert provider.updates == []
    assert provider.deletes == []
    assert lifecycle.deletes == []

    incomplete = {"operations": [_all_keep_operations()["operations"][0]]}
    result, provider, lifecycle = _run_audit(monkeypatch, json.dumps(incomplete))

    assert result["ok"] is False
    assert result["error"]["code"] == "incomplete_model_output"
    assert result["before"] == result["after"] == 3
    assert provider.updates == []
    assert provider.deletes == []
    assert lifecycle.deletes == []


def test_provider_audit_unknown_id_is_non_destructive(monkeypatch):
    output = _all_keep_operations()
    output["operations"][2]["id"] = "unknown-record"
    result, provider, lifecycle = _run_audit(monkeypatch, json.dumps(output))

    assert result["ok"] is False
    assert result["error"]["code"] == "unknown_memory_id"
    assert provider.updates == []
    assert provider.deletes == []
    assert lifecycle.deletes == []


def test_provider_audit_timeout_is_typed_failure_without_mutation(monkeypatch):
    result, provider, lifecycle = _run_audit(monkeypatch, asyncio.TimeoutError())

    assert result["ok"] is False
    assert result["status"] == "failed"
    assert result["error"]["code"] == "model_timeout"
    assert result["before"] == result["after"] == 3
    assert provider.updates == []
    assert provider.deletes == []
    assert lifecycle.deletes == []


def test_provider_audit_preview_returns_changes_without_mutating(monkeypatch):
    output = _all_keep_operations()
    output["operations"][1]["text"] = "User works in enterprise sales"
    result, provider, lifecycle = _run_audit(
        monkeypatch,
        json.dumps(output),
        apply=False,
    )

    assert result["ok"] is True
    assert result["status"] == "preview"
    assert result["applied"] is False
    assert result["updated"] == 1
    assert result["removed"] == 0
    assert result["proposal"]["updates"] == [{
        "id": "edit",
        "text": "User works in enterprise sales",
        "category": "fact",
    }]
    assert provider.updates == []
    assert provider.deletes == []
    assert lifecycle.deletes == []


def test_provider_audit_does_not_update_before_unavailable_delete_boundary(monkeypatch):
    output = _all_keep_operations()
    output["operations"][1]["text"] = "User works in enterprise sales"
    output["operations"][2] = {
        "id": "remove",
        "action": "delete",
        "reason": "assistant-only activity",
    }
    result, provider, _ = _run_audit(
        monkeypatch,
        json.dumps(output),
        lifecycle=False,
    )

    assert result["ok"] is False
    assert result["error"]["code"] == "coordinated_deletion_unavailable"
    assert provider.updates == []
    assert provider.deletes == []


def test_provider_audit_compensates_an_update_when_delete_apply_fails(monkeypatch):
    class RejectingLifecycle(FakeLifecycle):
        async def delete(self, memory_id, *, owner=None, workspace_id=None):
            self.deletes.append((memory_id, owner, workspace_id))
            return None

    async def fake_complete_text(**kwargs):
        return json.dumps({
            "operations": [
                {
                    "id": "keep",
                    "action": "keep",
                    "text": "User likes tea",
                    "category": "preference",
                },
                {
                    "id": "edit",
                    "action": "keep",
                    "text": "User works in enterprise sales",
                    "category": "project",
                },
                {
                    "id": "remove",
                    "action": "delete",
                    "reason": "assistant-only activity",
                },
            ]
        })

    monkeypatch.setattr(
        "services.memory.memory_extractor._complete_text",
        fake_complete_text,
    )
    provider = FakeProvider()
    lifecycle = RejectingLifecycle(provider)

    result = asyncio.run(audit_provider_memories(
        provider,
        owner="alice",
        memory_lifecycle=lifecycle,
    ))

    assert result["ok"] is False
    assert result["status"] == "failed"
    assert result["error"]["code"] == "provider_mutation_failed"
    assert result["applied"] is False
    assert result["rollback"] == {
        "updates_restored": 1,
        "deletes_restored": 0,
        "errors": [],
        "verified": True,
    }
    assert next(record for record in provider.records if record.id == "edit").text == "User works in sales"
    assert next(record for record in provider.records if record.id == "edit").category == "fact"
    assert any(update == ("edit", "User works in enterprise sales", "project") for update in provider.updates)
    assert provider.updates[-1] == ("edit", "User works in sales", "fact")


def test_native_audit_requires_complete_explicit_operations(monkeypatch, tmp_path):
    async def fake_complete_text(**kwargs):
        return json.dumps({
            "operations": [{
                "id": "keep",
                "action": "keep",
                "text": "User likes tea",
                "category": "preference",
            }]
        })

    monkeypatch.setattr(
        "services.memory.memory_extractor._complete_text",
        fake_complete_text,
    )
    manager = FakeNativeMemoryManager(tmp_path)

    result = asyncio.run(audit_memories(manager, None, owner="alice"))

    assert result["ok"] is False
    assert result["status"] == "failed"
    assert result["error"]["code"] == "incomplete_model_output"
    assert manager.saved == []
    assert [entry["id"] for entry in manager.entries] == ["keep", "remove"]


def test_native_audit_accepts_explicit_delete_operations(monkeypatch, tmp_path):
    async def fake_complete_text(**kwargs):
        return json.dumps({
            "operations": [
                {
                    "id": "keep",
                    "action": "keep",
                    "text": "User likes tea",
                    "category": "preference",
                },
                {
                    "id": "remove",
                    "action": "delete",
                    "reason": "assistant-only activity",
                },
            ]
        })

    monkeypatch.setattr(
        "services.memory.memory_extractor._complete_text",
        fake_complete_text,
    )
    manager = FakeNativeMemoryManager(tmp_path)

    result = asyncio.run(audit_memories(manager, None, owner="alice"))

    assert result["ok"] is True
    assert result["status"] == "applied"
    assert result["before"] == 2
    assert result["after"] == 1
    assert result["removed"] == 1
    assert result["applied"] is True
    assert [entry["id"] for entry in manager.entries] == ["keep"]
