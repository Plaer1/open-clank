"""Manual S08 journal work through the existing owner and outcome authorities.

The caller is responsible for authenticated human admission and passing a live
owner-scope currentness check. This adapter creates no scheduler, store or
model selection and does not run the legacy groom.
"""
from __future__ import annotations

from typing import Callable


async def consolidate_chat_now(*, scheduler, state, owner: str, account_id: str,
                               chat_id: str, memory_owner: str, workspace_id: str, current: Callable[[], bool]) -> dict:
    """Run at most one already-authorized chat journal page, or report refusal.

    A replayed recorded result can be acknowledged without a new model call;
    consequently this response never claims when an actual model run occurred.
    """
    if (not all(type(value) is str and value and value == value.strip() and len(value.encode('utf-8')) <= 512
                for value in (owner, account_id, chat_id, memory_owner, workspace_id)) or not callable(current)):
        return {'state': 'unavailable', 'lastActualRun': {'state':'unavailable'}}
    controller = getattr(scheduler, '_s05_controller', None)
    if (type(controller) is not tuple or len(controller) != 2 or type(controller[1]) is not dict
            or getattr(scheduler, '_s05_registry_state', None) is not state
            or getattr(state, 's05_profile', None) is not controller[0]):
        return {'state': 'unavailable', 'lastActualRun': {'state':'unavailable'}}
    profile, registry = controller
    entry = registry.get(account_id)
    callback = getattr(state, 's08_rules_callback', None)
    if type(entry) is not tuple or len(entry) != 3 or not callable(callback):
        return {'state': 'unavailable', 'lastActualRun': {'state':'unavailable'}}
    lifecycle, assignment, router = entry

    def still_current() -> bool:
        try:
            return (current() is True and getattr(scheduler, '_s05_controller', None) is controller
                    and getattr(scheduler, '_s05_registry_state', None) is state
                    and getattr(state, 's05_profile', None) is profile
                    and getattr(state, 's08_rules_callback', None) is callback
                    and registry.get(account_id) is entry and lifecycle.owner == owner
                    and lifecycle.account_id == account_id and assignment.owner == owner
                    and lifecycle.binding.memory_owner == memory_owner and lifecycle.binding.memory_workspace == workspace_id
                    and lifecycle.target_authority.resolve(account_id) == lifecycle.binding
                    and not scheduler._owner_lifecycle_fenced(owner))
        except Exception:
            return False

    if not still_current():
        return {'state': 'unavailable', 'lastActualRun': {'state':'unavailable'}}
    try:
        profile.assert_current()
        from src.openclank.fm_journal_delta import run_chat_pass

        async def run_job(journal):
            if not still_current():
                return False
            async def action(*, owner: str):
                if owner != lifecycle.owner or not still_current():
                    return False
                return await callback(state, lifecycle, None, (), router,
                                      output_kind='journal_synthesis', journal=journal)
            result = await scheduler._run_owner_aux_action(owner, action)
            return result is True and still_current()

        result = await run_chat_pass(profile=profile, lifecycle=lifecycle, assignment=assignment,
                                     account_id=account_id, chat_id=chat_id, run_job=run_job)
        if not still_current() or type(result) is not dict:
            return {'state': 'unavailable', 'lastActualRun': {'state':'unavailable'}}
        allowed = {'off','unavailable','empty','blocked_overlap','blocked_gap','deferred','acked','ack_refused','not_coverage','selected','staged'}
        status = result.get('state')
        if type(status) is not str or status not in allowed:
            status = 'unavailable'
        report = {'state': status, 'lastActualRun': {'state':'unavailable'}}
        from src.openclank.fm_journal_coverage import CLASSES
        counts = result.get('counts')
        if isinstance(counts, dict) and all(type(counts.get(key)) is int and counts[key] >= 0 for key in CLASSES):
            report['coverage'] = {key: counts[key] for key in CLASSES}
        reason = result.get('reason')
        if isinstance(reason, str) and reason in ('route_unavailable', 'source_changed', 'gates_closed', 'run_unavailable'):
            report['skip_reason'] = reason
        return report
    except Exception:
        return {'state': 'unavailable', 'lastActualRun': {'state':'unavailable'}}


async def retained_history(*, context, provider, output_kind='summary'):
    """Report only verified retained settlements in the current owner scope."""
    unavailable={'version':1,'state':'unavailable'}
    try:
        from src.openclank.memory_policy_projection import policy_coordinator
        if output_kind not in ('summary','journal_synthesis','connection'): return unavailable
        current=context['current_write']
        if current() is not True: return unavailable
        ready=getattr(provider,'_host_s08_policy_ready',None)
        if not callable(ready): return unavailable
        if ready(output_kind) is not True:
            if getattr(provider,'_owner_rpc_busy',False) or getattr(provider,'_s08_policy_session',None) is not None:
                return unavailable
            negotiate=getattr(provider,'_host_negotiate_s08_policy',None)
            if not callable(negotiate) or not await negotiate(owner=context['owner'],workspace_id=context['workspace']):
                return unavailable
        coordinator=policy_coordinator()
        if coordinator is None or current() is not True: return unavailable
        refs=coordinator.checked_refs(context['user'],context['account'],context['workspace'])
        read=getattr(provider,'_host_s08_last_retained_run',None)
        if not callable(read): return unavailable
        request={'version':1,'owner':context['owner'],'workspace_id':context['workspace'],
                 'account_id':context['account'],'policy_projection':refs}
        if output_kind != 'summary': request['output_kind']=output_kind
        value=await read(request,current=current)
        return value if current() is True and type(value) is dict else unavailable
    except Exception:
        return unavailable
