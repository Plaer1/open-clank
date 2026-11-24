"""Owner-bound next-prompt suggestion through the existing managed utility."""
from __future__ import annotations
import asyncio
import hashlib
import json
from fastapi import APIRouter, HTTPException, Request

_SYSTEM = "Predict the user's most likely next request from the conversation. Return one short first-person request, at most 100 characters, without explanation or markup."
_UNKNOWN = object()
_active: dict[str, asyncio.Task | object] = {}

def setup_prompt_suggestion_routes(manager, verify_owner):
    router = APIRouter()

    def identity(request, sid):
        from src.auth_helpers import require_user
        owner = require_user(request)
        if (getattr(request.state, 'api_token', False) or getattr(request.state, 'internal_tool_authenticated', False)
                or not getattr(request.state, 'authenticated', False)):
            raise HTTPException(403, 'A human browser session is required')
        verify_owner(request, sid, manager)
        state = request.app.state
        auth, barrier = getattr(state, 'auth_manager', None), getattr(state, 'account_request_barrier', None)
        account = auth.account_id(owner) if auth else None
        if not account or not barrier or auth.username_for_account_id(account) != owner:
            raise HTTPException(403, 'Account authority unavailable')
        capture = getattr(state, 'capture_original_credential', None)
        try: current = capture(request, owner, account)
        except Exception as exc: raise HTTPException(403, 'Credential unavailable') from exc
        if not callable(current) or current() is not True: raise HTTPException(403, 'Credential unavailable')
        def credential():
            try:
                return (getattr(state, 'auth_manager', None) is auth and getattr(state, 'account_request_barrier', None) is barrier
                        and auth.account_id(owner) == account and auth.username_for_account_id(account) == owner
                        and verify_owner(request, sid, manager) == owner and current() is True)
            except Exception:
                return False
        return owner, account, barrier, credential

    def snapshot(owner, sid):
        from src import agent_runs
        from src.agent_settings import validate_agent_settings
        from src.settings import get_user_setting
        from routes.chat_routes import _resolve_turn_model_request_options
        from src.openclank.chat_routing import resolve_chat_route
        settings = validate_agent_settings(get_user_setting('agent_settings', owner, {}) or {})
        enabled = settings.get('suggestions', {}).get('enabled') is True
        session = manager.get_session(sid)
        if session.owner != owner or session.archived or session.incognito or agent_runs.is_active(sid):
            raise ValueError('Current chat is unavailable')
        from core.database import SessionLocal, ChatMessage as DbChatMessage
        with session._history_lock:
            rows = [(m.persistence_id or (m.metadata or {}).get('_db_id'), m.role, m.content)
                    for m in session.history if m.role in ('user', 'assistant')]
            if not rows or rows[-1][1] != 'assistant' or not any(row[1] == 'user' for row in rows[:-1]):
                raise ValueError('A completed response is required')
            if any(not isinstance(c, str) for _, _, c in rows): raise ValueError('Conversation unavailable')
            context = rows[-8:]
            if any(not ident or len(c.encode('utf-8')) > 32768 for ident, _, c in context):
                raise ValueError('Conversation unavailable')
            with SessionLocal() as db:
                selected_ids = {ident for ident, _, _ in context}
                stored_ids = {item[0] for item in db.query(DbChatMessage.id).filter(
                    DbChatMessage.session_id == sid, DbChatMessage.id.in_(selected_ids)).all()}
                if selected_ids != stored_ids: raise ValueError('Conversation is not persisted')
                transcript = manager._projection_fingerprint(db, sid, session.history)

        options = _resolve_turn_model_request_options(session, owner, None)
        if options is None: raise ValueError('Current model options unavailable')
        route = resolve_chat_route(owner=owner, endpoint_id=session.endpoint_id, model_id=session.model,
                                   model_route_id=session.provider_model_route_id)
        from core.provider_models import ProviderConnection, ProviderModelRoute, ProviderShareGrant
        with SessionLocal() as db:
            selected = db.get(ProviderModelRoute, route.model_route_id)
            connection = db.get(ProviderConnection, route.connection_id)
            grant = db.get(ProviderShareGrant, route.provider_grant_id) if route.provider_grant_id else None
            if (selected is None or connection is None or selected.connection_id != connection.id
                    or selected.provider_model_id != route.provider_model_id or selected.catalog_revision != route.catalog_revision
                    or not selected.enabled or selected.deleted_at is not None or not connection.enabled or connection.deleted_at is not None
                    or (route.provider_grant_id and (grant is None or grant.state != 'active' or grant.recipient != owner))):
                raise ValueError('Current route unavailable')
            revisions = [selected.revision, connection.revision, grant.revision if grant else None]
        material = {'enabled': enabled, 'chat': sid, 'route': route.model_route_id,
                    'connection': route.connection_id, 'model': route.provider_model_id,
                    'grant': route.provider_grant_id, 'catalog': route.catalog_revision,
                    'route_revisions': revisions, 'capabilities': route.capabilities,
                    'options': options, 'transcript': transcript, 'messages': context}
        digest = hashlib.sha256(json.dumps(material, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        return material, digest

    @router.get('/session/{sid}/next-prompt')
    async def readback(request: Request, sid: str):
        owner, _, barrier, current = identity(request, sid)
        try:
            async with barrier.admitted(owner):
                material, digest = snapshot(owner, sid)
                if not current(): raise ValueError('Credential changed')
                return {'enabled': material['enabled'], 'state': 'selected' if material['enabled'] else 'off',
                        'revision': digest, 'selection': {'source': 'chat', 'model': material['model'],
                        'thinking': material['options']['options'], 'max_output_tokens': 100, 'cost': 'unknown'}}
        except (KeyError, ValueError): return {'enabled': None, 'state': 'unavailable', 'selection': None}

    @router.post('/session/{sid}/next-prompt')
    async def generate(request: Request, sid: str):
        from src.openclank.operation_router import get_operation_router, ManagedOperationRequest, _thaw_json
        from src.openclank.operation_journal import OperationJournalStore
        owner, account, barrier, current = identity(request, sid)
        try:
            data = bytearray()
            async for chunk in request.stream():
                if len(chunk) > 512 - len(data): raise ValueError('request too large')
                data.extend(chunk)
            body = json.loads(data)
        except (ValueError, TypeError, UnicodeError) as exc:
            raise HTTPException(400, 'Invalid suggestion request') from exc
        if (type(body) is not dict or set(body) != {'revision'} or not isinstance(body['revision'], str)
                or len(body['revision']) != 64 or any(ch not in '0123456789abcdef' for ch in body['revision'])):
            raise HTTPException(400, 'Invalid suggestion request')
        async with barrier.admitted(owner):
            try: material, digest = snapshot(owner, sid)
            except (KeyError, ValueError) as exc: raise HTTPException(409, 'Suggestion unavailable') from exc
            if not material['enabled'] or digest != body['revision'] or not current(): raise HTTPException(409, 'Suggestion changed')
            key = hashlib.sha256(('next-prompt-v1\0' + account + '\0' + digest).encode()).hexdigest()
            if key in _active: raise HTTPException(409, 'Original suggestion is pending or unknown')
            if len(_active) >= 1024: raise HTTPException(503, 'Suggestion capacity unavailable')
            selection = material['options']
            option_snapshot = {'version': 1, 'routeID': material['route'], 'optionRouteID': selection['route_id'],
                               'catalogRevision': selection['catalog_revision'],
                               'model': {'providerID': material['connection'], 'modelID': material['model']},
                               'options': selection['options']}
            operation = ManagedOperationRequest(owner=owner, operation='chat.complete', purpose='next_prompt',
                model_route_id=material['route'], grant_id=material['grant'], root_operation_id='suggestion_' + key[:32],
                idempotency_key=key, options={'modelRequestOptions': option_snapshot},
                input={'maxOutputTokens': 100, 'messages': [{'role':'system','content':_SYSTEM}] + [{'role': role, 'content': content}
                       for _, role, content in material['messages']]})
            engine = get_operation_router()
            try:
                prepared = engine.prepare(operation)
                payload = _thaw_json(prepared.payload)
                exact = {field: payload[field] for field in ('operation','routes','input','artifactInputs','options')}
                if OperationJournalStore(engine._session_factory).discover_exact(owner=owner, root_operation_id=operation.root_operation_id,
                        idempotency_key=key, request=exact) is not None:
                    raise HTTPException(409, 'Original suggestion already exists')
                owned = engine.new_prepared_owned(prepared)
            except HTTPException: raise
            except Exception as exc: raise HTTPException(503, 'Suggestion unavailable') from exc
            async def cancel_original():
                cancellation = asyncio.create_task(owned.cancel())
                cancellation.add_done_callback(lambda done: None if done.cancelled() else done.exception())
                try:
                    await asyncio.wait_for(asyncio.shield(cancellation), timeout=1.0)
                except (asyncio.TimeoutError, Exception):
                    pass
            async def drive():
                deadline = float(asyncio.get_running_loop().time() + 30)
                if await owned.ready(deadline=deadline) != {'state':'ready'}: raise ValueError('Suggestion unavailable')
                def before_issue():
                    if not current() or snapshot(owner, sid)[1] != digest: raise ValueError('Suggestion changed')
                if await owned.arm(before_issue=before_issue, deadline=deadline) != {'state':'issued'}: raise ValueError('Suggestion unavailable')
                try:
                    result = await asyncio.wait_for(owned.wait(), timeout=max(0.01, deadline - asyncio.get_running_loop().time()))
                except asyncio.TimeoutError:
                    await cancel_original()
                    raise ValueError('Suggestion outcome unknown')
                if not current() or snapshot(owner, sid)[1] != digest or result.state != 'complete': raise ValueError('Suggestion changed')
                text = result.output.get('text')
                if not isinstance(text, str): raise ValueError('Suggestion unavailable')
                return text.strip().split('\n', 1)[0][:100]
            task = asyncio.create_task(drive()); _active[key] = task
            def finished(done):
                error = None if done.cancelled() else done.exception()
                if _active.get(key) is done:
                    if (error or done.cancelled()) and owned.state != 'suppressed':
                        # Keep only a bounded no-reissue marker, never a task or traceback.
                        _active[key] = _UNKNOWN
                    else:
                        _active.pop(key, None)
            task.add_done_callback(finished)
            try:
                while not task.done():
                    try: still_current = current() and snapshot(owner, sid)[1] == digest
                    except Exception: still_current = False
                    if not still_current or await request.is_disconnected():
                        await cancel_original()
                        raise HTTPException(409, 'Suggestion cancelled')
                    await asyncio.wait({task}, timeout=.2)
                suggestion = task.result()
                if not suggestion: return {'state':'no_suggestion', 'revision':digest}
                return {'state':'ready', 'revision':digest, 'suggestion':suggestion}
            except HTTPException: raise
            except asyncio.CancelledError:
                await cancel_original(); raise
            except Exception as exc: raise HTTPException(409, 'Suggestion unavailable') from exc
    return router
