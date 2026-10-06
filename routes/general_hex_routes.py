"""Authenticated General library API; owner identity never comes from payloads."""
from fastapi import APIRouter, Body, HTTPException, Request
from src.general_hex_library import (
    GeneralHexRepository, GeneralHexError, GeneralHexConflict, GeneralHexNotFound,
)


def _owner(request):
    from src.auth_helpers import effective_user, require_authenticated_request
    require_authenticated_request(request)
    owner = str(effective_user(request) or '').strip().lower()
    if not owner:
        raise HTTPException(401, 'trusted Hex owner identity is required')
    return owner


def setup_general_hex_routes(*, db_path=None, db_path_resolver=None, owner_resolver=_owner):
    router = APIRouter(prefix='/api/hexes', tags=['general-hexes'])
    repository = GeneralHexRepository(db_path)

    def call(operation, *args, **kwargs):
        try:
            if db_path_resolver is not None:
                from src.general_hex_runtime import prepare_hex_authority
                path = db_path_resolver()
                if not path:
                    raise HTTPException(503, 'General Hex authority unavailable: no pinned local Frankenmemory store')
                prepare_hex_authority(path)
                operation = getattr(GeneralHexRepository(path), operation.__name__)
            return operation(*args, **kwargs)
        except GeneralHexConflict as exc:
            raise HTTPException(409, str(exc)) from exc
        except GeneralHexNotFound as exc:
            raise HTTPException(404, str(exc)) from exc
        except (GeneralHexError, TypeError) as exc:
            raise HTTPException(422, str(exc)) from exc

    def fields(payload, allowed):
        if set(payload) - set(allowed):
            raise HTTPException(422, 'unsupported fields')
        return dict(payload)

    def reviewed(payload, owner):
        result = dict(payload)
        if result.get('accepted') is True or (result.get('authorship', 'user') == 'user' and 'title' in result and 'body' in result):
            provenance = result.get('provenance') or {}
            if not isinstance(provenance, dict):
                raise HTTPException(422, 'provenance must be a JSON object')
            from datetime import datetime, timezone
            result['provenance'] = dict(provenance, acceptance=dict(owner_id=owner, source='explicit-user-save', accepted_at=datetime.now(timezone.utc).isoformat()))
        return result

    @router.get('/general')
    def listing(request: Request, search: str = '', tag: str | None = None, limit: int = 100, offset: int = 0):
        entries = call(repository.list, owner_resolver(request), search=search, tags=[tag] if tag else None, limit=limit, offset=offset)
        return {'entries': entries, 'coverage': 'page', 'limit': limit, 'offset': offset, 'next_offset': offset + limit if len(entries) == limit else None}

    @router.post('/general')
    def create(request: Request, payload: dict = Body(...)):
        owner = owner_resolver(request)
        values = fields(payload, ('title', 'body', 'tags', 'enabled', 'accepted', 'authorship', 'provenance'))
        return call(repository.create, owner, **reviewed(values, owner))

    @router.get('/general/{hex_id}')
    def get(request: Request, hex_id: str, revision: int | None = None):
        return call(repository.get, owner_resolver(request), hex_id, revision)

    @router.patch('/general/{hex_id}')
    def update(request: Request, hex_id: str, payload: dict = Body(...)):
        owner = owner_resolver(request)
        values = fields(payload, ('expected_revision', 'title', 'body', 'tags', 'enabled', 'accepted', 'authorship', 'provenance'))
        if 'expected_revision' not in values:
            raise HTTPException(422, 'expected_revision is required')
        if 'provenance' not in values and (values.get('accepted') is True or ('title' in values and 'body' in values)):
            values['provenance'] = call(repository.get, owner, hex_id)['provenance']
        return call(repository.update, owner, hex_id, **reviewed(values, owner))

    @router.post('/general/{hex_id}/accept')
    def accept(request: Request, hex_id: str, payload: dict = Body(...)):
        owner = owner_resolver(request)
        values = fields(payload, ('expected_revision',))
        if 'expected_revision' not in values:
            raise HTTPException(422, 'expected_revision is required')
        current = call(repository.get, owner, hex_id)
        reviewed_values = reviewed(dict(accepted=True, provenance=current['provenance']), owner)
        return call(repository.update, owner, hex_id, **values, **reviewed_values)

    @router.get('/general/{hex_id}/history')
    def history(request: Request, hex_id: str, limit: int = 100, offset: int = 0):
        revisions = call(repository.history, owner_resolver(request), hex_id, limit=limit, offset=offset)
        return {'revisions': revisions, 'coverage': 'page', 'limit': limit, 'offset': offset, 'next_offset': offset + limit if len(revisions) == limit else None}

    @router.post('/general/{hex_id}/restore')
    def restore(request: Request, hex_id: str, payload: dict = Body(...)):
        return call(repository.restore, owner_resolver(request), hex_id, **fields(payload, ('revision', 'expected_revision')))

    @router.delete('/general/{hex_id}')
    def delete(request: Request, hex_id: str, expected_revision: int):
        return call(repository.delete, owner_resolver(request), hex_id, expected_revision=expected_revision)

    @router.get('/context-tags/{context_kind}/{context_id:path}')
    def context(request: Request, context_kind: str, context_id: str):
        return call(repository.get_context_tags, owner_resolver(request), context_kind, context_id)

    @router.put('/context-tags/{context_kind}/{context_id:path}')
    def save_context(request: Request, context_kind: str, context_id: str, payload: dict = Body(...)):
        return call(repository.set_context_tags, owner_resolver(request), context_kind, context_id, **fields(payload, ('tags', 'expected_revision')))

    return router
