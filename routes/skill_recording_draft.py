"""Named-human route authority for explicitly source-linked skill drafts."""
from __future__ import annotations

import asyncio

from fastapi import HTTPException
from src.auth_helpers import require_user, require_privilege, is_internal_tool_identity
from src.openclank.files_facade import ProviderContext
from src.openclank.resource_refs import resolve_resource_ref
from services.memory.skill_recording_draft import (
    _RecordingDraftAuthority, _RECORDING_DRAFT_SEAL, recording_draft_status,
)


def _principal(request):
    user = require_user(request)
    if (not user or is_internal_tool_identity(user) or getattr(request.state, 'api_token', False)
            or getattr(request.state, 'internal_tool_authenticated', False)):
        raise HTTPException(403, 'A named user session is required')
    require_privilege(request, 'can_manage_memory')
    state = request.app.state
    auth = getattr(state, 'auth_manager', None)
    barrier = getattr(state, 'account_request_barrier', None)
    reset = getattr(state, 'memory_reset_coordinator', None)
    lifecycle = getattr(state, 'account_owner_lifecycle', None)
    token = request.cookies.get('open_clank_session')
    account = auth.account_id(user) if auth and token else None
    if (not barrier or not callable(reset) or lifecycle is None or not account
            or auth.get_username_for_token(token) != user or auth.username_for_account_id(account) != user):
        raise HTTPException(503, 'Skill draft authority unavailable')

    def current():
        try:
            privileges = auth.get_privileges(user)
            return bool(getattr(state, 'auth_manager', None) is auth
                and getattr(state, 'account_request_barrier', None) is barrier
                and getattr(state, 'memory_reset_coordinator', None) is reset
                and getattr(state, 'account_owner_lifecycle', None) is lifecycle
                and request.cookies.get('open_clank_session') == token
                and auth.get_username_for_token(token) == user and auth.account_id(user) == account
                and auth.username_for_account_id(account) == user and type(privileges) is dict
                and privileges.get('can_manage_memory') is True
                and not lifecycle.owner_has_active_operation(user) and not reset().owner_is_fenced(user)
                and not getattr(request.state, 'api_token', False)
                and not getattr(request.state, 'internal_tool_authenticated', False))
        except Exception:
            return False
    return state, user, account, barrier, current


async def create_recording_draft(request, body, manager):
    state, user, account, barrier, principal_current = _principal(request)
    target = body.recordingTarget
    if (type(target) is not dict or set(target) not in (
            {'resourceRef', 'expectedRevision', 'anchor'}, {'resourceRef', 'expectedRevision', 'anchor', 'task'})
            or type(target.get('resourceRef')) is not str or type(body.recordingRequestKey) is not str
            or body.status != 'draft' or body.source != 'user' or body.platforms or body.requires_toolsets
            or body.fallback_for_toolsets or body.teacher_model or body.session_id or body.title or body.problem
            or body.solution or body.steps or body.version != '1.0.0' or body.confidence != 0.8):
        raise HTTPException(400, 'Invalid recording draft request')
    task = target.get('task')
    if task is not None and (type(task) is not dict or set(task) != {'chatID', 'scope'}
            or type(task['chatID']) is not str or not task['chatID']):
        raise HTTPException(400, 'Invalid recording task scope')
    repository = getattr(state, 'files_policy_repository', None)
    factory = getattr(state, 'files_facade_state_factory', None)
    publisher = getattr(state, 'skill_history_scopes', None)
    if repository is None or not callable(factory) or publisher is None:
        raise HTTPException(503, 'Recording draft unavailable')
    async with barrier.admitted(user):
        try:
            generation = repository.generation()
            ref = resolve_resource_ref(target['resourceRef'], expected_owner_subject_id=account,
                                       current_policy_generation=generation)
            workspace = str(ref.workspace_id or 'default')
            auth = state.auth_manager
            context = ProviderContext(account, user, generation, bool(auth.is_admin(user)), workspace)
            facade = factory(workspace)
            loop = asyncio.get_running_loop()
            def base_current():
                return (principal_current() and getattr(state, 'files_policy_repository', None) is repository
                    and getattr(state, 'files_facade_state_factory', None) is factory
                    and getattr(state, 'skill_history_scopes', None) is publisher
                    and repository.generation() == generation and bool(auth.is_admin(user)) == context.is_admin)
            async def task_current():
                if not base_current(): return False
                if task is None: return True
                from src.openclank.plan_feedback import _scope, capture_feedback_scope
                manager_for_session = getattr(state, 'session_manager', None)
                return bool(manager_for_session and _scope(task['scope']) == await capture_feedback_scope(
                    request, task['chatID'], user, account, manager_for_session) and base_current())
            async def source_authority():
                return await task_current()
            if not await task_current(): raise ValueError('Recording authority changed')
            evidence, source_current = await facade._host_video_source(context, source_ref=target['resourceRef'],
                expected_revision=target['expectedRevision'], current=source_authority)
            async def qualified_current():
                return bool(await task_current() and await source_current() and await task_current())
            if not await qualified_current(): raise ValueError('Recording source changed')
            def current_sync():
                future = asyncio.run_coroutine_threadsafe(qualified_current(), loop)
                try: return future.result(timeout=10) is True
                except Exception:
                    future.cancel()
                    return False
            def history(action, directory, identity, parent_fd, child, scope):
                if action == 'open_create':
                    if not current_sync(): raise ValueError('Recording authority changed')
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
                        if not selected['is_current'](): raise ValueError('Recording authority changed')
                        return selected
                    except BaseException:
                        asyncio.run_coroutine_threadsafe(publisher.close(selected), loop).result()
                        raise
                if action == 'close':
                    try: return asyncio.run_coroutine_threadsafe(publisher.close(scope), loop).result()
                    except BaseException:
                        publisher.supervisor.fence_now(scope['process'])
                        raise
                raise ValueError('Skill History unavailable')
            authority = _RecordingDraftAuthority(_RECORDING_DRAFT_SEAL, owner=user, account=account,
                workspace=workspace, witness={'source': evidence, 'anchor': target['anchor'], 'task': task},
                current=current_sync, history=history)
            fields = {'name': body.name or '', 'description': body.description or '', 'category': body.category,
                'when_to_use': body.when_to_use or '', 'procedure': body.procedure, 'pitfalls': body.pitfalls,
                'verification': body.verification, 'tags': body.tags}
            work = asyncio.create_task(asyncio.to_thread(manager.add_recording_draft, authority=authority,
                request_key=body.recordingRequestKey, fields=fields))
            try: result = await asyncio.shield(work)
            except asyncio.CancelledError:
                while not work.done():
                    try: await asyncio.shield(work)
                    except asyncio.CancelledError: continue
                work.result()
                raise
            if not await qualified_current(): raise ValueError('Recording authority changed')
            return {'ok': True, 'skill': result}
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(409, 'Recording draft unavailable or outcome unknown; check original status') from exc


async def original_recording_draft_status(request, request_key):
    state, user, account, barrier, current = _principal(request)
    publisher = getattr(state, 'skill_history_scopes', None)
    if publisher is None: raise HTTPException(503, 'Skill History unavailable')
    async with barrier.admitted(user):
        if not current(): raise HTTPException(409, 'Skill authority changed')
        try:
            client = publisher.supervisor.client_for(user, account)
            return await asyncio.to_thread(recording_draft_status, owner=user, account=account,
                request_key=request_key, client=client, current=current)
        except Exception as exc:
            raise HTTPException(409, 'Skill draft status unavailable') from exc


async def historical_recording_citation(request, skill, manager):
    """Qualify a claimed creation action against retained native History."""
    import json
    import os
    from services.memory.skill_lifecycle import PreparedReadBudget, state_path
    from services.memory.skill_recording_draft import _validate_completed
    state, user, account, barrier, current = _principal(request)
    publisher = getattr(state, 'skill_history_scopes', None)
    if publisher is None: return {'state': 'unavailable'}
    async with barrier.admitted(user):
        def read():
            if not current() or skill.get('owner') != user: return None
            path = skill.get('path'); skill_id = skill.get('skill_id')
            if type(path) is not str or type(skill_id) is not str or not skill_id: return None
            root = os.path.realpath(manager.skills_root)
            if os.path.realpath(path) != os.path.abspath(path) or os.path.commonpath((root, path)) != root:
                return {'state': 'unavailable'}
            try:
                original = manager._read_skill(path)
                if (original is None or original.owner != user or original.skill_id != skill_id
                        or original.revision != skill.get('revision') or original.content_hash != skill.get('content_hash')):
                    return {'state': 'unavailable'}
                try:
                    payload = PreparedReadBudget(max_file=1024*1024, max_bytes=1024*1024, seconds=2).read(state_path(path))
                except FileNotFoundError:
                    return None
                lifecycle = json.loads(payload); claim = lifecycle.get('recording_draft')
                if claim is None: return None
                action_id = claim.get('action_id') if type(claim) is dict else None
                if type(action_id) is not str or not __import__('re').fullmatch(r'skill-recording-[0-9a-f]{64}', action_id):
                    return {'state': 'unavailable'}
                client = publisher.supervisor.client_for(user, account)
                checked = _validate_completed(client, client.get_capture_action(action_id)['Action'], action_id,
                    user, account, include_citation=True)
                after = manager._read_skill(path)
                if (not current() or after is None or after.owner != user or after.skill_id != skill_id
                        or after.revision != original.revision or after.content_hash != original.content_hash
                        or checked['skill_id'] != skill_id): return {'state': 'unavailable'}
                return {'state': 'available', **checked['citation']}
            except Exception:
                return {'state': 'unavailable'}
        if not current(): return {'state': 'unavailable'}
        return await asyncio.to_thread(read)
