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


def _human_change(request, owner_resolver):
    # Agent calls can propose guidance. Applying it is a signed-in owner action.
    if (getattr(request.state, 'internal_tool_authenticated', False) or getattr(request.state, 'api_token', False)
            or request.headers.get('authorization')):
        raise HTTPException(403, 'Changing Hexes requires a signed-in user')
    return owner_resolver(request)


def setup_general_hex_routes(*, db_path=None, db_path_resolver=None, owner_resolver=_owner):
    router = APIRouter(prefix='/api/hexes', tags=['general-hexes'])
    repository = GeneralHexRepository(db_path)

    def call(operation, *args, **kwargs):
        try:
            if db_path_resolver is not None:
                from src.general_hex_runtime import prepare_hex_authority
                path = db_path_resolver()
                if not path:
                    raise HTTPException(503, 'General Hex authority unavailable: no pinned local Memery store')
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
        owner = _human_change(request, owner_resolver)
        values = fields(payload, ('title', 'body', 'tags', 'enabled', 'accepted', 'authorship', 'provenance'))
        return call(repository.create, owner, **reviewed(values, owner))

    @router.get('/general/{hex_id}')
    def get(request: Request, hex_id: str, revision: int | None = None):
        return call(repository.get, owner_resolver(request), hex_id, revision)

    @router.patch('/general/{hex_id}')
    def update(request: Request, hex_id: str, payload: dict = Body(...)):
        owner = _human_change(request, owner_resolver)
        values = fields(payload, ('expected_revision', 'title', 'body', 'tags', 'enabled', 'accepted', 'authorship', 'provenance'))
        if 'expected_revision' not in values:
            raise HTTPException(422, 'expected_revision is required')
        if 'provenance' not in values and (values.get('accepted') is True or ('title' in values and 'body' in values)):
            values['provenance'] = call(repository.get, owner, hex_id)['provenance']
        return call(repository.update, owner, hex_id, account_id=request.app.state.auth_manager.account_id(owner), **reviewed(values, owner))

    @router.post('/general/{hex_id}/accept')
    def accept(request: Request, hex_id: str, payload: dict = Body(...)):
        owner = _human_change(request, owner_resolver)
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
        owner = _human_change(request, owner_resolver)
        return call(repository.restore, owner, hex_id, account_id=request.app.state.auth_manager.account_id(owner), **fields(payload, ('revision', 'expected_revision')))

    @router.delete('/general/{hex_id}')
    def delete(request: Request, hex_id: str, expected_revision: int):
        return call(repository.delete, _human_change(request, owner_resolver), hex_id, expected_revision=expected_revision)

    @router.get('/context-tags/{context_kind}/{context_id:path}')
    def context(request: Request, context_kind: str, context_id: str):
        return call(repository.get_context_tags, owner_resolver(request), context_kind, context_id)

    @router.put('/context-tags/{context_kind}/{context_id:path}')
    def save_context(request: Request, context_kind: str, context_id: str, payload: dict = Body(...)):
        return call(repository.set_context_tags, _human_change(request, owner_resolver), context_kind, context_id, **fields(payload, ('tags', 'expected_revision')))

    def guidance_context(request):
        from src.auth_helpers import require_privilege
        from src.general_hex_guidance import GeneralHexGuidance
        from src.general_hex_runtime import prepare_hex_authority
        from src.memory_gate import memory_mode
        from routes.prefs_routes import _load_for_user
        owner = _human_change(request, owner_resolver)
        if require_privilege(request, 'can_manage_memory') != owner:
            raise HTTPException(403, 'Memery management permission required')
        state = request.app.state
        auth = state.auth_manager
        account = auth.account_id(owner)
        try:
            credential = state.capture_original_credential(request, owner, account)
            path = db_path_resolver() if db_path_resolver else db_path
            service = GeneralHexGuidance(prepare_hex_authority(path))
        except Exception as exc:
            raise HTTPException(503, 'Guidance review authority unavailable') from exc
        def current():
            prefs = _load_for_user(owner)
            return (credential() is True and auth.get_privileges(owner).get('can_manage_memory') is True
                    and prefs.get('memory_enabled') is not False and memory_mode(prefs) != 'off'
                    and (db_path_resolver is None or str(db_path_resolver()) == str(path)))
        if not current():
            raise HTTPException(403, 'Memery review is disabled for this account')
        return service, owner, account, current

    def guidance_call(operation, *args, **kwargs):
        try:
            return operation(*args, **kwargs)
        except GeneralHexConflict as exc:
            raise HTTPException(409, str(exc)) from exc
        except GeneralHexNotFound as exc:
            raise HTTPException(404, str(exc)) from exc
        except (GeneralHexError, TypeError) as exc:
            raise HTTPException(422, str(exc)) from exc

    @router.get('/guidance/evidence')
    def guidance_evidence(request: Request, workspace_id: str | None = None, search: str = '', limit: int = 100, offset: int = 0):
        service, owner, account, current = guidance_context(request)
        result = guidance_call(service.evidence, owner, workspace_id, account_id=account, search=search, limit=limit, offset=offset)
        if not current():
            raise HTTPException(403, 'Memery review authority changed')
        return result

    @router.get('/guidance/proposals')
    def pending_guidance(request: Request, limit: int = 100, offset: int = 0):
        service, owner, account, current = guidance_context(request)
        result = guidance_call(service.pending, owner, account, limit=limit, offset=offset)
        if not current():
            raise HTTPException(403, 'Memery review authority changed')
        return result

    @router.post('/guidance/proposals')
    def propose_guidance(request: Request, payload: dict = Body(...)):
        service, owner, account, current = guidance_context(request)
        values = fields(payload, ('hex_id','expected_revision','action','reason','evidence_ids','workspace_id','parent_ids','tags','body'))
        return guidance_call(service.propose, owner, account_id=account, actor='owner', current=current, **values)

    @router.post('/guidance/proposals/{draft_id}/{decision}')
    def decide_guidance(request: Request, draft_id: str, decision: str):
        if decision not in ('accept', 'reject'):
            raise HTTPException(404, 'Unknown review decision')
        service, owner, account, current = guidance_context(request)
        return guidance_call(service.decide, owner, account, draft_id, accept=decision == 'accept', current=current)

    from routes.general_hex_scan import register_guidance_scan
    register_guidance_scan(router, guidance_context)
    return router
