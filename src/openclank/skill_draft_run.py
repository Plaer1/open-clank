"""One scheduler-owned, S05-reserved automatic skill draft.

The scheduler supplies only its installed owner tuple. Source identities are
selected and revalidated by the existing archive/lifecycle owners. A prior or
ambiguous Native original is never issued again by this worker.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import time

_OWNER_SEAL = object()


class _SkillDraftOwner:
    def __init__(self, seal, *, scheduler, state, profile, lifecycle, assignment, router, barrier, admission):
        if seal is not _OWNER_SEAL:
            raise ValueError('skill draft owner unavailable')
        self._seal, self.scheduler, self.state, self.profile = seal, scheduler, state, profile
        self.lifecycle, self.assignment, self.router = lifecycle, assignment, router
        self.barrier, self.admission = barrier, admission
        self.publisher = getattr(state, 'skill_history_scopes', None)
        self.process = getattr(getattr(self.publisher, 'supervisor', None), 'process', None)

    def base_current(self):
        from core.database import SessionLocal
        from src.memery_external_sources import CurrentHostUserSource
        from src.openclank.acp_supervisor_adapter import AcpSupervisorAdapter
        from src.openclank.chat_source_bridge import (CompletedTurnLifecycleOwner,
            _PUBLICATION_CAPABILITY_SEAL, canonical_source_provider)
        from src.openclank.conversation_archive import get_conversation_archive
        from src.openclank.memory_policy import STORE
        from src.openclank.history_client import HistoryServiceSupervisor
        from src.openclank.memory_job_admission import AssignmentAuthority
        from src.openclank.operation_router import ManagedOperationRouter, get_operation_router
        from src.openclank.skill_history_scope import SkillHistoryScopes
        from src.task_scheduler import TaskScheduler
        try:
            controller = self.scheduler._s05_controller
            entry = controller[1].get(self.lifecycle.account_id) if type(controller) is tuple else None
            supervisor = getattr(self.state, 'mimo_supervisor', None)
            source = self.lifecycle.source_authority
            auth = getattr(self.state, 'auth_manager', None)
            return bool(self._seal is _OWNER_SEAL and type(self.scheduler) is TaskScheduler
                and self.scheduler._s05_registry_state is self.state
                and getattr(getattr(self.scheduler, '_session_manager', None), 'lifecycle_context', None) is self.state
                and type(controller) is tuple and controller[0] is self.profile
                and type(entry) is tuple and len(entry) == 3
                and entry[0] is self.lifecycle and entry[1] is self.assignment and entry[2] is self.router
                and type(self.lifecycle) is CompletedTurnLifecycleOwner and self.lifecycle._seal is _PUBLICATION_CAPABILITY_SEAL
                and self.lifecycle.profile is self.profile and getattr(self.state, 's05_profile', None) is self.profile
                and type(supervisor) is AcpSupervisorAdapter and supervisor._s05_profile is self.profile
                and self.lifecycle.target_authority._supervisor is supervisor.owner_lifecycle_pool()
                and self.lifecycle.target_authority._auth is auth and self.lifecycle.account_lifecycle is getattr(self.state, 'account_owner_lifecycle', None)
                and type(source) is CurrentHostUserSource and source._owner == self.lifecycle.owner and source._auth is auth
                and source._archive is get_conversation_archive() and source._session_factory is SessionLocal
                and source._policy is STORE and source._workspaces is getattr(self.state, 'files_policy_repository', None)
                and source._workspaces is not None and canonical_source_provider(self.state) is self.lifecycle.provider
                and type(self.assignment) is AssignmentAuthority and self.assignment.owner == self.lifecycle.owner
                and type(self.router) is ManagedOperationRouter and get_operation_router() is self.router
                and getattr(self.state, 'account_request_barrier', None) is self.barrier
                and type(self.publisher) is SkillHistoryScopes and not self.publisher._failed
                and getattr(self.state, 'skill_history_scopes', None) is self.publisher
                and type(self.publisher.supervisor) is HistoryServiceSupervisor
                and self.process is not None and self.publisher.supervisor.process is self.process and self.process.poll() is None
                and self.lifecycle.provider is self.profile.provider
                and self.lifecycle.target_authority.resolve(self.lifecycle.account_id) == self.lifecycle.binding
                and not self.lifecycle.account_lifecycle.owner_has_active_operation(self.lifecycle.owner))
        except Exception:
            return False

    def current(self):
        return bool(self.base_current() and self.lifecycle.provider._host_skill_draft_profile_current(self.lifecycle))


async def _native_write(owner, phase, request, *, current, deadline, ticket_out=None):
    if not current():
        raise ValueError('skill draft authority changed')
    ticket = {} if ticket_out is None else ticket_out
    result = await owner.lifecycle.provider._host_candidate_request(
        owner.lifecycle.binding.memory_owner, owner.lifecycle.binding.memory_workspace,
        {'phase': phase, 'request': request}, require_live=True, write_deadline=float(deadline),
        write_status=ticket, before_issue=current)
    if not current():
        raise ValueError('skill draft authority changed')
    return result


async def _native_original(owner, phase, request, *, deadline):
    if not owner.current():
        raise ValueError('skill draft authority changed')
    result = await owner.lifecycle.provider._s07_owner_rpc(
        owner.lifecycle.binding.memory_owner, owner.lifecycle.binding.memory_workspace,
        {'phase': phase, 'request': request}, float(deadline))
    if not owner.current():
        raise ValueError('skill draft authority changed')
    return result


def _qualified_usage(owner, session, result):
    """Use only the observed exact operation and the original profile clock."""
    from src.openclank.operation_router import ManagedOperationResult
    try:
        attempt = session.attempt
        reserve = attempt['reservation_original']
        original, reservation = reserve['original'], reserve['reservation']
        handle = session.handle
        utility = getattr(handle, '_utility', None)
        worker = getattr(utility, 'worker', None)
        if (handle is None or handle._task is None or not handle._task.done()
                or handle._task.cancelled() or handle._task.exception() is not None
                or handle._task.result() is not result or utility is None or utility.owned_send is not handle
                or not utility.issued or not utility.response_seen or not utility.account_current()
                or utility.lease is None or utility.lease.worker is not worker
                or getattr(worker, '_proc', None) is not utility.process
                or getattr(worker, '_bridge', None) is not utility.bridge
                or getattr(worker, 'installed_generation', None) != utility.generation
                or getattr(utility.bridge, '_managed_provider_generation', None) != utility.holder):
            return None
        if (type(result) is not ManagedOperationResult or session.result is not result
                or result.commit_reason == 'outcome_unknown' or result.root_operation_id != original['operation_id']
                or result.operation != 'chat.complete' or result.model_route_id != original['model_route_id']
                or result.connection_id != original['connection_id'] or not owner.current()
                or owner.profile.clock_generation != reservation['clock_generation']):
            return None
        owner.profile.assert_current()
        end = owner.profile.monotonic_tick()
        start = reservation['now_tick']
        values = (result.usage.get('inputTokens'), result.usage.get('outputTokens'))
        if (type(start) is not int or type(end) is not int or end < start
                or any(type(v) is not int or v < 0 for v in values)):
            return None
        elapsed = (end - start + 999) // 1000
        if (elapsed > reservation['job_seconds'] or values[0] > original['max_input_tokens']
                or values[1] > original['max_output_tokens'] or not owner.current()):
            return None
        return {'elapsed_seconds': elapsed, 'input_tokens': values[0], 'output_tokens': values[1]}
    except Exception:
        return None


async def _terminal(owner, session, disposition, draft_sha=None):
    from src.openclank.skill_draft_reservation import _sha
    usage = None if disposition == 'uncertain' else getattr(session, 'qualified_usage', None)
    original = {'version': 1, 'attempt': session.attempt, 'disposition': disposition,
                'draft_sha256': draft_sha, 'usage': usage}
    session.state = 'terminal_issued'
    deadline = asyncio.get_running_loop().time() + 30.0
    status = {}
    try:
        result = await _native_write(owner, 'skill_draft_terminal', original,
            current=owner.current, deadline=deadline, ticket_out=status)
    except Exception:
        ticket = status.get('ticket')
        if type(ticket) is not dict or not owner.current():
            raise
        provider = owner.lifecycle.provider
        snapshot = await provider._owned_original_checked_snapshot(ticket, 'skill_draft_terminal', original)
        read_ticket = {}
        result = await provider._s07_owner_rpc(owner.lifecycle.binding.memory_owner,
            owner.lifecycle.binding.memory_workspace,
            {'phase': 'skill_draft_result_original', 'request': session.attempt},
            float(asyncio.get_running_loop().time()+5.0), read_ticket=read_ticket)
        await provider._owned_original_checked_current(ticket, 'skill_draft_terminal', original, snapshot)
        if (read_ticket.get('transport') is not snapshot[1] or read_ticket.get('generation') is not snapshot[2]
                or not owner.current()):
            raise ValueError('skill draft terminal custody changed')
    expected = {'version': 1, 'state': disposition, 'job_id': session.attempt['job_id'],
        'attempt_id': session.attempt['attempt_id'], 'lease_epoch': session.attempt['lease_epoch'],
        'draft_sha256': draft_sha, 'request_sha256': _sha(original)}
    if disposition != 'uncertain':
        expected['charge_basis'] = 'actual' if usage is not None else 'reserved_bound'
    if (type(result) is not dict or type(result.get('version')) is not int
            or type(result.get('lease_epoch')) is not int or result != expected):
        raise ValueError('skill draft terminal unavailable')
    session.state = 'terminal_' + disposition
    return result


async def _settle_terminal(owner, session, disposition, draft_sha=None):
    """Join this exact terminal write before propagating cancellation."""
    task = asyncio.create_task(_terminal(owner, session, disposition, draft_sha))
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
    try:
        result = task.result()
    except Exception as error:
        if cancelled:
            raise asyncio.CancelledError from error
        raise
    if cancelled:
        raise asyncio.CancelledError
    return result


async def _read_drained_original(owner, phase, original, status_phase, request, status):
    ticket = status.get('ticket') if type(status) is dict else None
    if type(ticket) is not dict or not owner.current():
        return None
    provider = owner.lifecycle.provider
    try:
        snapshot = await provider._owned_original_checked_snapshot(ticket, phase, original)
        read_ticket = {}
        result = await provider._s07_owner_rpc(owner.lifecycle.binding.memory_owner,
            owner.lifecycle.binding.memory_workspace, {'phase': status_phase, 'request': request},
            float(asyncio.get_running_loop().time()+5.0), read_ticket=read_ticket)
        await provider._owned_original_checked_current(ticket, phase, original, snapshot)
        if (read_ticket.get('transport') is snapshot[1] and read_ticket.get('generation') is snapshot[2]
                and owner.current()):
            return result
    except Exception:
        pass
    return None


async def _retire_unstarted(owner, original, reserve, prepare_status, reserve_status, queued, receipt):
    """Reconcile only this drained original, then cancel/revoke a proved unissued attempt."""
    from src.openclank.skill_draft_reservation import _sha
    if not owner.current() or type(original) is not dict:
        return
    if reserve is not None:
        fact = receipt
        if fact is None:
            fact = await _read_drained_original(owner, 'skill_draft_reserve', reserve,
                'skill_draft_reserve_original', reserve, reserve_status)
        if fact == {'version': 1, 'state': 'unknown'}:
            reserve = None
        else:
            job = original['operation_id']; attempt_id = 'skillattempt_' + hashlib.sha256(job.encode()).hexdigest()
            receipt_id = 'cs_skill_reserve_' + hashlib.sha256(job.encode()).hexdigest()
            epoch = fact.get('foreground_epoch') if type(fact) is dict else None
            expected = {'version': 1, 'state': 'reserved', 'original_sha256': _sha(original),
                'request_sha256': _sha(reserve), 'account_id': original['account_id'], 'owner': original['owner'],
                'job_id': job, 'attempt_id': attempt_id, 'lease_epoch': 1,
                'foreground_epoch': epoch, 'reserve_receipt_id': receipt_id}
            if (type(epoch) is int and epoch >= 0 and type(fact.get('version')) is int
                    and type(fact.get('lease_epoch')) is int and fact == expected):
                attempt = {'version': 1, 'reservation_original': reserve, 'job_id': job,
                    'attempt_id': attempt_id, 'lease_epoch': 1}
                await _native_write(owner, 'skill_draft_revoke', attempt, current=owner.current,
                    deadline=float(asyncio.get_running_loop().time()+5.0))
            return
    if queued is None:
        queued = await _read_drained_original(owner, 'skill_draft_prepare', original,
            'skill_draft_prepare_original', original, prepare_status)
    if queued == {'version': 1, 'state': 'queued', 'job_id': original['operation_id']}:
        await _native_write(owner, 'skill_draft_prepared_cancel', original, current=owner.current,
            deadline=float(asyncio.get_running_loop().time()+5.0))


async def _reconcile_dispatch(owner, session):
    """Read the exact issued original after its transport drains; never resend it."""
    from src.openclank.skill_draft_reservation import _sha
    ticket = session.dispatch_ticket.get('ticket') if type(session.dispatch_ticket) is dict else None
    if type(ticket) is not dict or not owner.current():
        return 'unknown'
    provider = owner.lifecycle.provider
    try:
        snapshot = await provider._owned_original_checked_snapshot(ticket, 'skill_draft_dispatch', session.attempt)
        read_ticket = {}
        result = await provider._s07_owner_rpc(owner.lifecycle.binding.memory_owner,
            owner.lifecycle.binding.memory_workspace, {'phase': 'skill_draft_original', 'request': session.attempt},
            float(asyncio.get_running_loop().time()+5.0), read_ticket=read_ticket)
        await provider._owned_original_checked_current(ticket, 'skill_draft_dispatch', session.attempt, snapshot)
        if (read_ticket.get('transport') is not snapshot[1] or read_ticket.get('generation') is not snapshot[2]
                or not owner.current()):
            return 'unknown'
        attempt = session.attempt
        dispatched = {'version': 1, 'state': 'dispatched', 'job_id': attempt['job_id'],
            'attempt_id': attempt['attempt_id'], 'lease_epoch': attempt['lease_epoch'], 'request_sha256': _sha(attempt)}
        reserve = attempt['reservation_original']; original = reserve['original']
        reserved = {'version': 1, 'state': 'reserved', 'original_sha256': _sha(original),
            'request_sha256': _sha(reserve), 'account_id': original['account_id'], 'owner': original['owner'],
            'job_id': attempt['job_id'], 'attempt_id': attempt['attempt_id'], 'lease_epoch': attempt['lease_epoch'],
            'foreground_epoch': session.foreground_epoch, 'reserve_receipt_id': session.reserve_receipt_id}
        if (type(result) is dict and type(result.get('version')) is int
                and type(result.get('lease_epoch')) is int and result == dispatched):
            session.state = 'dispatched'
            return 'dispatched'
        if (type(result) is dict and type(result.get('version')) is int
                and type(result.get('lease_epoch')) is int and type(result.get('foreground_epoch')) is int
                and result == reserved):
            session.state = 'ready'
            return 'reserved'
    except Exception:
        pass
    return 'unknown'


async def run_one_skill_draft(*, scheduler, state, profile, lifecycle, assignment, router):
    """Run at most one new exact original; existing/unknown originals remain status-only."""
    from src.agent_tools.model_interaction_tools import _TEACHER_SYSTEM_PROMPT
    from src.openclank.account_request_barrier import AccountRequestBarrier
    from src.openclank.memory_distill_work import (discover_automatic_source_workspaces,
        preview_automatic_sources, prepare_automatic_skill_draft)
    from src.openclank.operation_router import ManagedOperationRequest
    from src.openclank.skill_draft_reservation import (_new_reserved_skill_draft_session,
        _ready_reserved_skill_draft_session, _dispatch_reserved_skill_draft_session,
        _wait_issued_skill_draft_session, _begin_skill_draft_save, _begin_skill_draft_status, _SEAL)
    from services.memory.skill_automatic_draft import (_SkillDraftAgentAuthority,
        add_automatic_skill_draft, automatic_skill_draft_status)
    from services.memory.skills import SkillsManager
    from src.constants import DATA_DIR

    barrier = getattr(state, 'account_request_barrier', None)
    if type(barrier) is not AccountRequestBarrier:
        return {'state': 'unavailable'}
    admission = await barrier.acquire(lifecycle.owner)
    owner = _SkillDraftOwner(_OWNER_SEAL, scheduler=scheduler, state=state, profile=profile,
        lifecycle=lifecycle, assignment=assignment, router=router, barrier=barrier, admission=admission)
    session = None
    dispatched = False
    queued_original = None
    prepare_original = None
    prepare_status = {}
    reserve_original = None
    reserve_status = {}
    reserve_receipt = None
    reserve_issued = False
    save_cancelled = False
    try:
        if not owner.base_current() or not await lifecycle.provider._host_negotiate_skill_draft_profile(
                lifecycle=lifecycle, barrier=barrier, admission=admission) or not owner.current():
            return {'state': 'unavailable'}
        workspaces = await discover_automatic_source_workspaces(lifecycle=lifecycle, current=owner.current)
        if workspaces.get('state') != 'available':
            return {'state': 'unavailable'}
        selection = None
        for workspace in workspaces.get('workspaceIDs', ()):
            candidate = await preview_automatic_sources(lifecycle=lifecycle, source_workspace_id=workspace,
                current=owner.current)
            if candidate.get('state') == 'available':
                selection = candidate
                break
        if selection is None:
            return {'state': 'no_match'}
        occurrences, digest = selection['occurrences'], selection['selection_sha256']
        deadline = float(asyncio.get_running_loop().time() + 300.0)
        async with prepare_automatic_skill_draft(lifecycle=lifecycle, assignment=assignment,
                occurrences=occurrences, selection_sha256=digest, current=owner.current) as held:
            original = held['original']
            deadline = float(min(deadline, asyncio.get_running_loop().time() + held['limits'].job_seconds))
            from src.openclank.fm_journal_delta import s05_gates_open
            from src.openclank.memory_job_admission import _completed_turn_thread
            gates, cancelled = await _completed_turn_thread(lambda: s05_gates_open(profile,
                owner=original['owner'], limits=held['limits'], input_bound=original['max_input_tokens']))
            if cancelled: raise asyncio.CancelledError
            if gates is not True: return {'state': 'deferred'}
            prior = await _native_original(owner, 'skill_draft_prepare_original', original, deadline=deadline)
            if type(prior) is not dict or type(prior.get('version')) is not int or prior != {'version': 1, 'state': 'unknown'}:
                return {'state': 'existing'}
            messages = [{'role': 'system', 'content': _TEACHER_SYSTEM_PROMPT},
                        {'role': 'user', 'content': held['prompt']}]
            prepared = router.prepare(ManagedOperationRequest(owner=lifecycle.owner, operation='chat.complete',
                purpose='utility', input={'messages': messages, 'maxOutputTokens': original['max_output_tokens']},
                model_route_id=original['model_route_id'], grant_id=original['assignment']['provider_grant_id'],
                root_operation_id=original['operation_id'], idempotency_key=original['operation_id']))
            def held_current():
                try:
                    if not owner.current(): return False
                    held['assert_preframe']()
                    return True
                except Exception:
                    return False
            prepare_original = original
            queued = await _native_write(owner, 'skill_draft_prepare', original,
                current=held_current, deadline=deadline, ticket_out=prepare_status)
            if type(queued) is not dict or type(queued.get('version')) is not int or queued != {'version': 1, 'state': 'queued', 'job_id': original['operation_id']}:
                raise ValueError('skill draft preparation unavailable')
            queued_original = original
            limits = held['limits']
            reservation = {'version': 1, 'owner': original['owner'], 'job_id': original['operation_id'],
                'worker_id': 'skill_'+hashlib.sha256((lifecycle.account_id+'\0'+profile.coverage_generation+'\0'+profile.clock_generation).encode()).hexdigest(),
                'coverage_generation': profile.coverage_generation, 'clock_generation': profile.clock_generation,
                'now_tick': profile.monotonic_tick(), 'quiet_seconds': limits.quiet_seconds,
                'job_seconds': limits.job_seconds, 'daily_seconds': limits.daily_seconds,
                'target_fingerprint': original['target_fingerprint'], 'policy_fingerprint': original['policy_fingerprint']}
            reserve = {'version': 1, 'original': original, 'reservation': reservation}
            reserve_original = reserve
            reserve_issued = True
            receipt = await _native_write(owner, 'skill_draft_reserve', reserve,
                current=held_current, deadline=deadline, ticket_out=reserve_status)
            reserve_receipt = receipt
            session = _new_reserved_skill_draft_session(execution_owner=owner, lifecycle=lifecycle,
                assignment=assignment, router=router, prepared=prepared, held=held, reserve=reserve,
                receipt=receipt, deadline=deadline)
        ready = await _ready_reserved_skill_draft_session(session)
        if ready['state'] != 'ready':
            return {'state': ready['state']}
        async with prepare_automatic_skill_draft(lifecycle=lifecycle, assignment=assignment,
                occurrences=occurrences, selection_sha256=digest, current=owner.current) as held:
            issued = await _dispatch_reserved_skill_draft_session(session, held)
            dispatched = True
        if issued['state'] != 'issued':
            await _settle_terminal(owner, session, 'uncertain')
            return {'state': 'unknown'}
        observed = await _wait_issued_skill_draft_session(session)
        if observed['state'] != 'observed':
            await _settle_terminal(owner, session, 'uncertain')
            return {'state': 'unknown'}
        result = observed['result']
        session.qualified_usage = _qualified_usage(owner, session, result)
        if result.commit_reason == 'outcome_unknown':
            await _settle_terminal(owner, session, 'uncertain')
            return {'state': 'unknown'}
        if result.state != 'complete' or result.committed is not True or not isinstance(result.output.get('text'), str):
            await _settle_terminal(owner, session, 'failed' if result.state == 'failed' else 'uncertain')
            return {'state': 'unavailable'}
        async with prepare_automatic_skill_draft(lifecycle=lifecycle, assignment=assignment,
                occurrences=occurrences, selection_sha256=digest, current=owner.current) as held:
            _begin_skill_draft_save(session, held)
            try:
                authority = _SkillDraftAgentAuthority(_SEAL, state=state, session=session,
                    publisher=owner.publisher, loop=asyncio.get_running_loop())
                from src.openclank.memory_job_admission import _completed_turn_thread
                try:
                    saved, save_cancelled = await _completed_turn_thread(
                        lambda: add_automatic_skill_draft(SkillsManager(DATA_DIR), authority=authority))
                except BaseException as error:
                    if session.state != 'save_unknown':
                        raise
                    session.clear_sources()
                    _begin_skill_draft_status(session, held)
                    reader = _SkillDraftAgentAuthority(_SEAL, state=state, session=session,
                        publisher=owner.publisher, loop=asyncio.get_running_loop(), mode='read')
                    original, status_cancelled = await _completed_turn_thread(
                        lambda: automatic_skill_draft_status(authority=reader))
                    if original.get('state') != 'complete':
                        raise error
                    saved = {'state': 'draft', 'draftSHA256': original['draftSHA256'], 'draft': original['draft']}
                    save_cancelled = status_cancelled or isinstance(error, asyncio.CancelledError)
            finally:
                session.clear_sources()
        if saved.get('state') == 'draft':
            await _settle_terminal(owner, session, 'draft', saved['draftSHA256'])
        elif saved.get('state') == 'no_draft':
            await _settle_terminal(owner, session, 'no_draft')
        else:
            raise ValueError('skill draft result unavailable')
        if save_cancelled:
            raise asyncio.CancelledError
        return {'state': saved['state']}
    except asyncio.CancelledError:
        async def stop_original():
            if session is not None and session.handle is not None:
                await session.handle.cancel()
            if session is not None and session.state == 'dispatch_issued':
                await _reconcile_dispatch(owner, session)
            if session is not None and session.state in ('dispatched', 'model_issued', 'model_wait', 'model_unknown', 'model_complete', 'saving', 'save_unknown'):
                try: await _settle_terminal(owner, session, 'uncertain')
                except Exception: pass
        stop = asyncio.create_task(stop_original())
        while not stop.done():
            try: await asyncio.shield(stop)
            except asyncio.CancelledError: continue
        stop.result()
        raise
    except Exception:
        if session is not None and session.state == 'dispatch_issued':
            await _reconcile_dispatch(owner, session)
        if session is not None and (dispatched or session.state in ('dispatched', 'model_issued', 'model_wait', 'model_unknown', 'model_complete', 'saving', 'save_unknown')) and session.state not in ('terminal_issued', 'terminal_uncertain', 'terminal_draft', 'terminal_no_draft', 'terminal_failed'):
            try: await _settle_terminal(owner, session, 'uncertain')
            except Exception: pass
        return {'state': 'unavailable'}
    finally:
        async def cleanup():
            drained = 'unknown'
            if session is not None and session.handle is not None:
                try:
                    await session.handle.cancel()
                    drained = await session.handle.drain(deadline=float(asyncio.get_running_loop().time()+5.0))
                except Exception:
                    pass
            if owner.current() and session is None and prepare_original is not None:
                try:
                    queued = ({'version': 1, 'state': 'queued', 'job_id': queued_original['operation_id']}
                        if queued_original is not None else None)
                    await _retire_unstarted(owner, prepare_original, reserve_original if reserve_issued else None,
                        prepare_status, reserve_status, queued, reserve_receipt)
                except Exception:
                    pass
            if owner.current() and session is not None and session.state in ('prepared', 'ready_wait', 'ready', 'ready_suppressed', 'ready_unknown'):
                try:
                    await _native_write(owner, 'skill_draft_revoke', session.attempt,
                        current=owner.current, deadline=float(asyncio.get_running_loop().time()+5.0))
                except Exception:
                    pass
            if session is not None and drained != 'unknown' and getattr(lifecycle.provider, '_skill_draft_session', None) is session:
                lifecycle.provider._skill_draft_session = None
            await barrier.release(admission)
        task = asyncio.create_task(cleanup())
        while not task.done():
            try: await asyncio.shield(task)
            except asyncio.CancelledError: continue
        task.result()
