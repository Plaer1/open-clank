"""Provider-backed memory audit uses provider mutations, not memory.json."""

import asyncio

from src.memory_provider import MemoryRecord
from services.memory.memory_extractor import audit_provider_memories


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


def test_provider_audit_updates_and_deletes_active_records(monkeypatch):
    async def fake_llm(*args, **kwargs):
        return (
            '[{"id":"keep","text":"User likes tea","category":"preference"},'
            ' {"id":"edit","text":"User works in enterprise sales","category":"project"}]'
        )

    monkeypatch.setattr("src.llm_core.llm_call_async", fake_llm)
    provider = FakeProvider()

    result = asyncio.run(audit_provider_memories(provider, "http://llm", "model", owner="alice"))

    assert result == {"before": 3, "after": 2}
    assert provider.updates == [("edit", "User works in enterprise sales", "project")]
    assert provider.deletes == ["remove"]
