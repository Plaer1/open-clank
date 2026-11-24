"""Named-human review of portable skill snapshots and original draft status."""
from __future__ import annotations

import asyncio
import json

from fastapi.responses import JSONResponse

from routes.skill_recording_draft import _principal
from routes.skill_marketplace import _drain
from src.openclank.account_request_barrier import _current_admission

_HEADERS = {'Cache-Control': 'private, no-store'}
_LIMIT = 5 * 1024 * 1024


def _result(value, status=200):
    return JSONResponse(value, status_code=status, headers=_HEADERS)


def _capture(request, manager, *, history):
    state, user, account, barrier, principal_current = _principal(request)
    admission = _current_admission.get()
    publisher = getattr(state, 'skill_history_scopes', None) if history else None
    supervisor = getattr(publisher, 'supervisor', None) if history else None
    process = getattr(supervisor, 'process', None) if history else None
    if manager is None or (history and (publisher is None or process is None or process.poll() is not None)):
        raise ValueError('Snapshot authority unavailable')
    client = supervisor.client_for(user, account) if history else None
    def current():
        try:
            barrier.assert_live_admission(user, admission)
            return bool(principal_current() and (not history or
                (getattr(state, 'skill_history_scopes', None) is publisher
                 and publisher.supervisor is supervisor and supervisor.process is process and process.poll() is None)))
        except Exception:
            return False
    if not current(): raise ValueError('Snapshot authority unavailable')
    return user, account, barrier, publisher, supervisor, client, current


async def _body(request, *, limit):
    data = bytearray()
    async for chunk in request.stream():
        if len(data) + len(chunk) > limit: raise ValueError('Snapshot request exceeds bound')
        data.extend(chunk)
    def pairs(items):
        value = {}
        for key, item in items:
            if key in value: raise ValueError('Duplicate JSON key')
            value[key] = item
        return value
    value = json.loads(data, object_pairs_hook=pairs,
        parse_constant=lambda _: (_ for _ in ()).throw(ValueError('Invalid JSON')))
    if type(value) is not dict: raise ValueError('Invalid request')
    return value


def _sync_current(loop, current):
    async def check(): return current()
    def run():
        future = asyncio.run_coroutine_threadsafe(check(), loop)
        try: return future.result(timeout=10) is True
        except Exception:
            future.cancel()
            return False
    return run


def _history_callback(publisher, supervisor, user, account, loop, current_sync):
    def history(action, directory, identity, parent_fd, child, scope):
        if action == 'open_create':
            if not current_sync(): raise ValueError('Authority changed')
            process = supervisor.process
            try:
                selected = asyncio.run_coroutine_threadsafe(publisher.open_create(owner=user, account=account,
                    directory=directory, identity=identity, parent_fd=parent_fd, child=child), loop).result()
            except BaseException:
                supervisor.fence_now(process)
                raise
            try:
                selected['client'] = supervisor.client_for(user, account)
                selected['is_current'] = lambda: (publisher.supervisor is supervisor and supervisor.process is selected['process']
                    and selected['process'].poll() is None and current_sync())
                if not selected['is_current'](): raise ValueError('Authority changed')
                return selected
            except BaseException:
                asyncio.run_coroutine_threadsafe(publisher.close(selected), loop).result()
                raise
        if action == 'close':
            try: return asyncio.run_coroutine_threadsafe(publisher.close(scope), loop).result()
            except BaseException:
                supervisor.fence_now(scope['process'])
                raise
        raise ValueError('History unavailable')
    return history


async def snapshot_call(request, action, manager):
    try:
        if action not in ('export-preview','export','import-preview','import','external-preview','external-import'): return _result({'error':'unavailable'},404)
        user, account, barrier, publisher, supervisor, client, current = _capture(request, manager, history=action in ('import','external-import'))
        async with barrier.admitted(user):
            if not current(): raise ValueError('Authority changed')
            body = await _body(request, limit=9 * 1024 * 1024 if action == 'import-preview' else _LIMIT)
            if not current(): raise ValueError('Authority changed')
            keys = ({'skill_id'} if action == 'export-preview' else {'skill_id','review_sha256'} if action == 'export' else
                {'raw','destination_name'} if action == 'external-preview' else {'raw','destination_name','review_sha256','request_key'} if action == 'external-import' else
                {'package_json','destination_name'} if action == 'import-preview' else {'package','destination_name','review_sha256','request_key'})
            if set(body) != keys: raise ValueError('Invalid request')
            loop = asyncio.get_running_loop()
            current_sync = _sync_current(loop, current)
            if action == 'export-preview':
                value = await _drain(asyncio.to_thread(manager.prepare_skill_snapshot_export,
                    owner=user, account=account, skill_id=body['skill_id'], current=current_sync))
            elif action == 'export':
                from services.memory.skill_snapshot_format import package_sha256
                package = await _drain(asyncio.to_thread(manager.confirm_skill_snapshot_export,
                    owner=user, account=account, skill_id=body['skill_id'], review_sha256=body['review_sha256'], current=current_sync))
                value = {'package':package, 'package_sha256':package_sha256(package)}
            elif action in ('external-preview','external-import'):
                from services.memory.external_skill_import import review_external, ExternalSkillAuthority, _SEAL, create_external
                review, _ = review_external(body['raw'],body['destination_name'])
                if action == 'external-preview':
                    value = {'review':review}
                else:
                    if review['unsupported'] or body['review_sha256'] != review['review_sha256']: raise ValueError('Review changed')
                    authority = ExternalSkillAuthority(_SEAL, owner=user, account=account, workspace='default', witness=review,
                        current=current_sync, history=_history_callback(publisher,supervisor,user,account,loop,current_sync))
                    skill = await _drain(asyncio.to_thread(create_external,manager,authority=authority,
                        request_key=body['request_key'],raw=body['raw'],review=review))
                    value = {'ok':True,'skill':skill}
            else:
                from services.memory.skill_snapshot_format import parse_package, validate_package, review_package
                if action == 'import-preview':
                    if type(body['package_json']) is not str: raise ValueError('Invalid package')
                    package = parse_package(body['package_json'].encode('utf-8', 'strict'))
                else:
                    package = validate_package(body['package'])
                review = review_package(package, destination_name=body['destination_name'])
                if action == 'import-preview': value = {'package':package, 'review':review}
                else:
                    if body['review_sha256'] != review['review_sha256']: raise ValueError('Review changed')
                    from services.memory.skill_snapshot_draft import _SkillSnapshotDraftAuthority, _SKILL_SNAPSHOT_DRAFT_SEAL
                    authority = _SkillSnapshotDraftAuthority(_SKILL_SNAPSHOT_DRAFT_SEAL, owner=user, account=account,
                        workspace='default', witness=review, current=current_sync,
                        history=_history_callback(publisher, supervisor, user, account, loop, current_sync))
                    skill = await _drain(asyncio.to_thread(manager.add_skill_snapshot_draft, authority=authority,
                        request_key=body['request_key'], package=package, review=review))
                    value = {'ok':True, 'skill':skill}
            if not current(): raise ValueError('Authority changed')
            response = _result(value)
            if not current(): raise ValueError('Authority changed')
            return response
    except Exception:
        return _result({'error':'Snapshot unavailable or outcome unknown; check original status'},409)


async def snapshot_status(request, request_key, manager):
    try:
        user, account, barrier, publisher, supervisor, client, current = _capture(request, manager, history=True)
        async with barrier.admitted(user):
            if not current(): raise ValueError('Authority changed')
            profile=request.query_params.get('profile')
            if profile not in (None,'external'): raise ValueError('Unknown snapshot profile')
            from services.memory.skill_snapshot_draft import skill_snapshot_draft_status
            from services.memory.external_skill_import import status_external
            current_sync = _sync_current(asyncio.get_running_loop(), current)
            value = await _drain(asyncio.to_thread(status_external if profile=='external' else skill_snapshot_draft_status, owner=user, account=account,
                request_key=request_key, client=client, current=current_sync))
            if not current(): raise ValueError('Authority changed')
            response = _result(value)
            if not current(): raise ValueError('Authority changed')
            return response
    except Exception:
        return _result({'error':'Snapshot status unavailable'},409)
