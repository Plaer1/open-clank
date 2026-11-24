"""Named-admin staged marketplace review and original-only draft status."""
from __future__ import annotations

import asyncio
import json

from fastapi import HTTPException, Request

from routes.skill_recording_draft import _principal
from src.openclank.account_request_barrier import _current_admission
from src.openclank.files_facade import ProviderContext


def _capture(request, *, source=True):
    state, user, account, barrier, principal_current = _principal(request)
    auth = getattr(state, 'auth_manager', None)
    admission = _current_admission.get()
    repository = getattr(state, 'files_policy_repository', None)
    factory = getattr(state, 'files_facade_state_factory', None)
    publisher = getattr(state, 'skill_history_scopes', None)
    if not publisher or auth.is_admin(user) is not True or (source and (repository is None or not callable(factory))):
        raise HTTPException(503, 'Marketplace authority unavailable')
    generation = repository.generation() if source else None
    def current():
        try:
            barrier.assert_live_admission(user, admission)
            return bool(principal_current() and auth.is_admin(user) is True
                and getattr(state, 'skill_history_scopes', None) is publisher
                and (not source or (getattr(state, 'files_policy_repository', None) is repository
                    and getattr(state, 'files_facade_state_factory', None) is factory
                    and repository.generation() == generation)))
        except Exception:
            return False
    if not current(): raise HTTPException(409, 'Marketplace authority unavailable')
    return state, user, account, barrier, repository, factory, publisher, generation, current


async def _body(request):
    data = bytearray()
    async for chunk in request.stream():
        if len(data) + len(chunk) > 16384: raise HTTPException(413, 'Marketplace request exceeds its bound')
        data.extend(chunk)
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result: raise ValueError('Duplicate key')
            result[key] = value
        return result
    value = json.loads(data, object_pairs_hook=pairs, parse_constant=lambda _: (_ for _ in ()).throw(ValueError('Invalid JSON')))
    if type(value) is not dict: raise ValueError('Invalid marketplace request')
    return value


async def _drain(work):
    task = asyncio.create_task(work)
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        while not task.done():
            try: await asyncio.shield(task)
            except asyncio.CancelledError: continue
        task.result()
        raise


async def marketplace_call(request: Request, action: str, manager):
    captured = _capture(request)
    state, user, account, barrier, repository, factory, publisher, generation, current = captured
    if action not in ('catalog', 'components', 'preview', 'import'):
        raise HTTPException(404, 'Unknown marketplace action')
    async with barrier.admitted(user):
        try:
            if not current(): raise ValueError('Authority changed')
            body = await _body(request)
            if not current(): raise ValueError('Authority changed')
            keys = {'source'} | ({'plugin_name'} if action != 'catalog' else set()) | ({'skill_path'} if action in ('preview','import') else set()) | ({'review_sha256','request_key'} if action == 'import' else set())
            if set(body) != keys or type(body['source']) is not dict: raise ValueError('Invalid marketplace request')
            source = body['source']
            async def guard(): return current()
            from services.memory.marketplace_source import github_reader
            external_readers = []
            reader = None
            async def external(url):
                if not current(): raise ValueError('Authority changed')
                reader = await github_reader(url, current=guard)
                if not current(): raise ValueError('Authority changed')
                external_readers.append(reader)
                return reader
            if source.get('kind') == 'registry':
                from services.memory.marketplace_registries import inspect_registry
                selector, result, files = await inspect_registry(
                    source, action, body.get('plugin_name'), body.get('skill_path'), guard,
                )
                workspace = 'default'
                if action == 'import':
                    if result.get('blocked'):
                        raise HTTPException(403, 'The upstream release is blocked')
                    if files is None:
                        raise ValueError('Marketplace release unavailable')
                    facts = result
            elif source.get('kind') == 'github' and set(source) == {'kind','url'} and type(source['url']) is str:
                reader = await external(source['url'])
                selector = dict(reader.identity)
                workspace = 'default'
            elif source.get('kind') == 'files' and set(source) == {'kind','workspace_id','resource_ref','expected_revision'}:
                from src.openclank.marketplace_files_source import MarketplaceFilesSource
                workspace = source['workspace_id']
                if type(workspace) is not str or not workspace or len(workspace) > 512: raise ValueError('Invalid workspace')
                context = ProviderContext(account, user, generation, False, workspace)
                reader = await MarketplaceFilesSource.open(facade=factory(workspace), context=context,
                    repository=repository, root_ref=source['resource_ref'], expected_revision=source['expected_revision'], current=guard)
                selector = dict(source)
            else: raise ValueError('Invalid marketplace source')
            if not current(): raise ValueError('Authority changed')
            if reader is not None:
                from services.memory.marketplace_importer import catalog, components, preview
                if action == 'catalog': result = await catalog(reader)
                elif action == 'components': result = await components(reader, body['plugin_name'], external)
                else:
                    facts, files = await preview(reader, body['plugin_name'], body['skill_path'], external)
                    result = facts
            if not current(): raise ValueError('Authority changed')
            if action != 'import':
                return {'selector': selector, 'result': result}
            if type(body['review_sha256']) is not str or facts.get('review_sha256') != body['review_sha256']:
                raise ValueError('Marketplace review changed')
            loop = asyncio.get_running_loop()
            async def original_current():
                return current()
            async def source_before_publish():
                if not current(): return False
                if reader is not None:
                    await reader.finish()
                for external_reader in external_readers:
                    if external_reader is not reader: await external_reader.finish()
                return current()
            def check_on_loop(check):
                future = asyncio.run_coroutine_threadsafe(check(), loop)
                try: return future.result(timeout=10) is True
                except Exception:
                    future.cancel()
                    return False
            def current_sync(): return check_on_loop(original_current)
            def before_publish(): return check_on_loop(source_before_publish)
            def history(action_name, directory, identity, parent_fd, child, scope):
                if action_name == 'open_create':
                    if not current_sync(): raise ValueError('Authority changed')
                    process = publisher.supervisor.process
                    try:
                        selected = asyncio.run_coroutine_threadsafe(publisher.open_create(owner=user, account=account,
                            directory=directory, identity=identity, parent_fd=parent_fd, child=child), loop).result()
                    except BaseException:
                        publisher.supervisor.fence_now(process)
                        raise
                    try:
                        selected['client'] = publisher.supervisor.client_for(user, account)
                        selected['is_current'] = lambda: (publisher.supervisor.process is selected['process']
                            and selected['process'].poll() is None and current_sync())
                        if not selected['is_current'](): raise ValueError('Authority changed')
                        return selected
                    except BaseException:
                        asyncio.run_coroutine_threadsafe(publisher.close(selected), loop).result()
                        raise
                if action_name == 'close':
                    try: return asyncio.run_coroutine_threadsafe(publisher.close(scope), loop).result()
                    except BaseException:
                        publisher.supervisor.fence_now(scope['process'])
                        raise
                raise ValueError('History unavailable')
            from services.memory.skill_marketplace_draft import _MarketplaceDraftAuthority, _MARKETPLACE_DRAFT_SEAL
            authority = _MarketplaceDraftAuthority(_MARKETPLACE_DRAFT_SEAL, owner=user, account=account,
                workspace=workspace, witness=facts, current=current_sync, before_publish=before_publish, history=history)
            result = await _drain(asyncio.to_thread(manager.add_marketplace_draft, authority=authority,
                request_key=body['request_key'], review=facts, files=files))
            if not current(): raise ValueError('Authority changed')
            return {'ok': True, 'skill': result}
        except HTTPException: raise
        except Exception as exc:
            raise HTTPException(409, 'Marketplace operation unavailable or outcome unknown; check original status') from exc


async def marketplace_status(request: Request, request_key: str):
    state, user, account, barrier, repository, factory, publisher, generation, current = _capture(request, source=False)
    async with barrier.admitted(user):
        try:
            if not current(): raise ValueError('Authority changed')
            from services.memory.skill_marketplace_draft import marketplace_draft_status
            client = publisher.supervisor.client_for(user, account)
            loop = asyncio.get_running_loop()
            async def check_current():
                return current()
            def current_sync():
                future = asyncio.run_coroutine_threadsafe(check_current(), loop)
                try: return future.result(timeout=10) is True
                except Exception:
                    future.cancel()
                    return False
            result = await _drain(asyncio.to_thread(marketplace_draft_status, owner=user, account=account,
                request_key=request_key, client=client, current=current_sync))
            if not current(): raise ValueError('Authority changed')
            return result
        except Exception as exc:
            raise HTTPException(409, 'Marketplace status unavailable') from exc
