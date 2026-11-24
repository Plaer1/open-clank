import sys
import types
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException


def _load_module(monkeypatch):
    db_stub = types.ModuleType("core.database")
    db_stub.EditorDraft = MagicMock()
    db_stub.SessionLocal = MagicMock()
    monkeypatch.setitem(sys.modules, "core.database", db_stub)
    monkeypatch.delitem(sys.modules, "routes.editor_draft_routes", raising=False)

    import routes.editor_draft_routes as mod

    return mod


def test_load_payload_rejects_non_object_json(monkeypatch):
    mod = _load_module(monkeypatch)

    assert mod._load_payload("[]") == {}
    assert mod._load_payload('"draft"') == {}
    assert mod._load_payload("{bad json") == {}
    assert mod._load_payload('{"layers": []}') == {"layers": []}


@pytest.mark.asyncio
async def test_draft_read_requires_user_and_rejects_other_owner(monkeypatch):
    mod = _load_module(monkeypatch)
    endpoint = next(route.endpoint for route in mod.setup_editor_draft_routes().routes if route.path.endswith('/{draft_id}') and 'GET' in route.methods)
    request = types.SimpleNamespace(state=types.SimpleNamespace(authenticated=False))
    with pytest.raises(HTTPException) as denied:
        await endpoint(request, 'draft')
    assert denied.value.status_code == 401
    db = mod.SessionLocal.return_value
    db.query.return_value.filter.return_value.first.return_value = types.SimpleNamespace(owner='bob')
    monkeypatch.setattr(mod, 'require_user', lambda _: 'alice')
    with pytest.raises(HTTPException) as missing:
        await endpoint(request, 'draft')
    assert missing.value.status_code == 404
    assert not mod._owns(types.SimpleNamespace(owner=None), 'alice')


@pytest.mark.asyncio
async def test_draft_create_replays_only_identical_request_for_same_owner(monkeypatch):
    import uuid
    from sqlalchemy.exc import IntegrityError

    mod = _load_module(monkeypatch)
    endpoint = next(route.endpoint for route in mod.setup_editor_draft_routes().routes if 'POST' in route.methods)
    monkeypatch.setattr(mod, 'require_user', lambda _: 'alice')
    body = mod.DraftCreate(request_id=uuid.uuid4(), payload={'layers': []})
    db = mod.SessionLocal.return_value
    db.commit.side_effect = IntegrityError('insert', {}, RuntimeError())
    row = types.SimpleNamespace(owner='alice', id='draft', name='Untitled', width=None, height=None,
                                source_image_id=None, thumbnail=None, payload='{"layers":[]}',
                                is_active=True, created_at=None, updated_at=None)
    db.get.return_value = row
    assert (await endpoint(None, body))['id'] == row.id
    alice_id = db.get.call_args.args[1]
    row.payload = '{"layers":[1]}'
    with pytest.raises(HTTPException) as changed:
        await endpoint(None, body)
    assert changed.value.status_code == 409
    monkeypatch.setattr(mod, 'require_user', lambda _: 'bob')
    with pytest.raises(HTTPException) as foreign:
        await endpoint(None, body)
    assert foreign.value.status_code == 409
    assert db.get.call_args.args[1] != alice_id
