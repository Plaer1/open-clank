"""Backup import must dedup memories against the importing user only.

Historically import_data deduped incoming memories against every tenant's
rows, so a memory whose text matched ANY other user's memory was silently
skipped — the importing user lost their own data. Under provider-always the
invariant is structural: the dedup set comes from provider.list_page(owner=…),
which can only ever see the caller's own records. These tests pin both sides
of that: another tenant's identical text can't block an import, and the
caller's own duplicate still dedups.
"""
import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock

import routes.backup_routes as br


class _Req:
    def __init__(self, body):
        self._body = body

    async def json(self):
        return self._body


class _Provider:
    provider_id = "stub"

    def __init__(self, records=None):
        self.records = records or []
        self.remember_calls = []

    async def list_page(self, *, owner=None, limit=1000, cursor=None):
        return [r for r in self.records if r.owner == owner], None

    async def remember(self, text, **kwargs):
        self.remember_calls.append((text, kwargs))
        return SimpleNamespace(id="m_new")

    async def pin(self, memory_id, pinned, *, owner=None):
        return True


def _record(text, owner):
    return SimpleNamespace(text=text, owner=owner)


def _setup(monkeypatch, provider, user="alice"):
    monkeypatch.setattr(br, "require_admin", lambda request: None)
    monkeypatch.setattr(br, "get_current_user", lambda request: user)

    skills = MagicMock()
    skills.load_all.return_value = []
    router = br.setup_backup_routes(MagicMock(), MagicMock(), skills, memory_provider=provider)
    for r in router.routes:
        if r.path == "/api/import" and "POST" in getattr(r, "methods", set()):
            return r.endpoint
    raise AssertionError("/api/import route missing")


def test_user_can_import_memory_matching_another_users_text(monkeypatch):
    # bob already has "buy milk"; alice imports her own "Buy Milk". The
    # provider scopes the dedup listing to alice, so bob's row is invisible
    # by construction and the import must land.
    provider = _Provider([_record("buy milk", owner="bob")])
    endpoint = _setup(monkeypatch, provider)
    asyncio.run(endpoint(_Req({"memories": [{"text": "Buy Milk"}]})))

    assert [call[0] for call in provider.remember_calls] == ["Buy Milk"]
    assert provider.remember_calls[0][1]["owner"] == "alice"


def test_users_own_duplicate_is_still_skipped(monkeypatch):
    provider = _Provider([_record("buy milk", owner="alice")])
    endpoint = _setup(monkeypatch, provider)
    asyncio.run(endpoint(_Req({"memories": [{"text": "Buy Milk"}]})))

    assert provider.remember_calls == []
