"""TUI-only adapter for current, blocking ACP interactions."""
from __future__ import annotations
from dataclasses import dataclass
import json
from fastapi import HTTPException
from src.openclank.account_request_barrier import _current_admission
from src.openclank.transcript_projection import get_projection


@dataclass(frozen=True)
class ManagedShellApproval:
    owner: str
    account: str
    chat: str
    request: object
    pending: object
    source: tuple
    current: object
    policy_revision: object
    holder: object
    generation: object


@dataclass(frozen=True)
class ManagedFileApproval:
    owner: str
    account: str
    chat: str
    request: object
    pending: object
    source: tuple
    current: object
    policy_revision: object
    holder: object
    generation: object


@dataclass(frozen=True)
class TuiInputGuard:
    request: object
    current: object

    def valid(self, request):
        return request is self.request and callable(self.current) and self.current() is True


def capture(request, owner):
    auth = getattr(request.app.state, 'auth_manager', None)
    barrier = getattr(request.app.state, 'account_request_barrier', None)
    admission = _current_admission.get()
    supervisor = getattr(request.app.state, 'mimo_supervisor', None)
    try:
        barrier.assert_live_admission(owner, admission)
        account = auth.account_id(owner)
        if not account or auth.username_for_account_id(account) != owner: raise ValueError()
        credential = request.app.state.capture_original_credential(request, owner, account)
        worker = supervisor.worker_for_owner(owner)
        if not worker or not worker.is_alive() or not callable(credential) or credential() is not True: raise ValueError()
    except Exception as exc:
        raise HTTPException(503, 'Current interaction authority unavailable') from exc
    def current():
        try:
            barrier.assert_live_admission(owner, admission)
            return (request.app.state.auth_manager is auth and request.app.state.account_request_barrier is barrier
                    and request.app.state.mimo_supervisor is supervisor and supervisor.worker_for_owner(owner) is worker
                    and worker.is_alive() and auth.account_id(owner) == account
                    and auth.username_for_account_id(account) == owner and credential() is True)
        except Exception:
            return False
    return worker, current


def _matches(req, owner, session_id, projection):
    return (req.owner == owner and req.odysseus_session_id == session_id and not req._future.done()
            and projection and projection.get('mimo_session_id') == getattr(req, 'mimo_session_id', getattr(req, 'session_id', None))
            and projection.get('active_turn_id') == req.turn_id
            and int(projection.get('transcript_revision') or 0) == req.revision)


def offered(req):
    if not isinstance(req.options, list): return []
    choices = []
    for value in req.options:
        key = value if isinstance(value, str) else value.get('optionId') if isinstance(value, dict) else None
        if key in ('once', 'reject') and key not in choices: choices.append(key)
    return choices


def read(worker, current, owner, session_id, *, questions, approvals):
    if not current(): raise HTTPException(403, 'TUI credential or account changed')
    projection = get_projection(session_id, owner=owner)
    items = []
    omitted = False
    if questions:
        handler = getattr(worker, 'question_handler', None)
        if handler is None: omitted = True
        for req in list(getattr(handler, 'pending_requests', {}).values()):
            if _matches(req, owner, session_id, projection):
                payload = req.payload()
                if len(json.dumps(payload, ensure_ascii=False).encode()) <= 32768:
                    items.append({'kind':'question','request':payload,'tool':None})
                else: omitted = True
    if approvals:
        handler = getattr(worker, 'permission_handler', None)
        if handler is None: omitted = True
        for req in list(getattr(handler, 'pending_requests', {}).values()):
            if _matches(req, owner, session_id, projection):
                options = offered(req)
                detail = req.raw_input if isinstance(req.raw_input, dict) else {}
                payload = {'request_id':req.request_id,'session_id':session_id,'turn_id':req.turn_id,
                           'revision':req.revision,'permission_type':req.title,'detail':detail,'options':options,'tool':None}
                if options and len(json.dumps(payload, ensure_ascii=False).encode()) <= 32768:
                    items.append({'kind':'approval','request':payload})
                else: omitted = True
    complete = not omitted and len(items) <= 32
    if not current(): raise HTTPException(403, 'TUI credential or account changed')
    return {'items':items[:32], 'complete':complete, 'coverage':'blocking_acp_only'}


def resolve(worker, current, owner, session_id, value):
    if not current(): raise HTTPException(403, 'TUI credential or account changed')
    kind, request_id = value['kind'], value['request_id']
    handler = getattr(worker, 'question_handler' if kind == 'question' else 'permission_handler', None)
    req = getattr(handler, 'pending_requests', {}).get(request_id)
    projection = get_projection(session_id, owner=owner)
    if (req is None or not _matches(req, owner, session_id, projection)
            or req.turn_id != value['turn_id'] or req.revision != value['revision']):
        raise HTTPException(409, 'Interaction is stale or unavailable')
    if not current(): raise HTTPException(403, 'TUI credential or account changed')
    if kind == 'question':
        result = handler.resolve_result(request_id, owner=owner, session_id=session_id,
            answers=value.get('answers'), rejected=value.get('rejected', False))
        if result == 'invalid_answer': raise HTTPException(422, 'Invalid question answer')
        if result != 'accepted': raise HTTPException(409, 'Question is stale or unavailable')
    else:
        option = value.get('option')
        if option not in offered(req): raise HTTPException(400, 'Approval choice unavailable')
        if not handler.resolve_for(request_id, option, owner=owner, session_id=session_id):
            raise HTTPException(409, 'Approval is stale or unavailable')
    return {'state':'accepted'}


async def _shell_evidence(worker, current, owner, account, chat):
    """Find only evidence retained in the original live managed admission."""
    from src.shell_policy import current_shell_approval
    bridge = getattr(worker, '_bridge', None)
    if bridge is None or not current(): return [], False
    try:
        async with bridge._managed_execution_lock:
            from itertools import islice
            records = list(islice(((entry, record, record.get('shellApprovalEvidence'))
                for entry in bridge._managed_actor_turns.values() if isinstance(entry, dict)
                and entry.get('state') == 'active' and not entry.get('backgroundStartClosing')
                for record in entry.get('backgroundStartAdmitted', {}).values() if isinstance(record, dict)), 33))
        result = []
        complete = len(records) <= 32
        records = records[:32]
        for entry, record, evidence in records:
            if type(evidence) is not ManagedShellApproval: continue
            if evidence.owner != owner or evidence.account != account or evidence.chat != chat: continue
            req = evidence.request
            source = dict(evidence.source)
            if (record.get('shellApproval') != (req.request_id, owner, chat, account)
                    or source.get('rootOperationID') != req.turn_id
                    or not all(isinstance(source.get(key), str) and source[key] for key in
                               ('sessionID','rootOperationID','actorID','innerTurnID','messageID','toolCallID','toolName'))
                    or type(source.get('executionRevision')) is not int
                    or current_shell_approval(req.request_id, owner=owner, session_id=chat, account_id=account) is not evidence.pending):
                complete = False
                continue
            try: live = await evidence.current()
            except Exception: live = False
            if not live or not current():
                complete = False
                continue
            async with bridge._managed_execution_lock:
                if (not current() or current_shell_approval(req.request_id, owner=owner, session_id=chat, account_id=account) is not evidence.pending
                        or entry.get('policyRevision') != evidence.policy_revision
                        or getattr(bridge, '_managed_provider_generation', None) != evidence.holder
                        or getattr(getattr(worker, '_managed_callbacks', None), 'holder_id', None) != evidence.holder
                        or getattr(worker, 'installed_generation', None) != evidence.generation
                        or not any(value is entry for value in bridge._managed_actor_turns.values())
                        or not any(value is record for value in entry.get('backgroundStartAdmitted', {}).values())
                        or entry.get('state') != 'active' or entry.get('backgroundStartClosing')
                        or record.get('shellApprovalEvidence') is not evidence
                        or record.get('shellApproval') != (req.request_id,owner,chat,account)):
                    complete = False
                    continue
            result.append((entry, record, evidence))
        return result, complete
    except Exception:
        return [], False


async def read_managed_shell(worker, current, owner, account, chat):
    found, complete = await _shell_evidence(worker, current, owner, account, chat)
    items = []
    for _, _, evidence in found:
        req = evidence.request
        choices = offered(req)
        payload = {'request_id':req.request_id,'session_id':chat,'turn_id':req.turn_id,
            'revision':req.revision,'permission_type':req.title,'detail':req.raw_input,
            'options':choices,'source':dict(evidence.source),
            'notice':'Command redaction is best effort; details may contain sensitive paths.'}
        if choices and len(json.dumps(payload, ensure_ascii=False).encode()) <= 32768:
            items.append({'kind':'shell_approval','request':payload})
        else: complete = False
    if not current(): raise HTTPException(403, 'TUI credential or account changed')
    return items, complete


async def resolve_managed_shell(worker, current, owner, account, chat, value):
    from src.shell_policy import resolve_shell_approval
    found, _ = await _shell_evidence(worker, current, owner, account, chat)
    matches = [(entry, record, evidence) for entry, record, evidence in found
        if evidence.request.request_id == value['request_id'] and evidence.request.turn_id == value['turn_id']
        and evidence.request.revision == value['revision']]
    if len(matches) != 1: raise HTTPException(409, 'Shell approval is stale or unavailable')
    entry, record, evidence = matches[0]
    option = value.get('option')
    if option not in offered(evidence.request): raise HTTPException(400, 'Approval choice unavailable')
    if not current(): raise HTTPException(403, 'TUI credential or account changed')
    bridge = getattr(worker, '_bridge', None)
    async with bridge._managed_execution_lock:
        if (not current() or entry.get('policyRevision') != evidence.policy_revision
                or getattr(bridge, '_managed_provider_generation', None) != evidence.holder
                or getattr(getattr(worker, '_managed_callbacks', None), 'holder_id', None) != evidence.holder
                or getattr(worker, 'installed_generation', None) != evidence.generation
                or not any(value is entry for value in bridge._managed_actor_turns.values())
                or not any(value is record for value in entry.get('backgroundStartAdmitted', {}).values())
                or entry.get('state') != 'active' or entry.get('backgroundStartClosing')
                or record.get('shellApprovalEvidence') is not evidence
                or record.get('shellApproval') != (evidence.request.request_id,owner,chat,account)):
            raise HTTPException(409, 'Shell approval is stale or unavailable')
        if not resolve_shell_approval(evidence.request.request_id, option, owner=owner,
                session_id=chat, account_id=account, expected_pending=evidence.pending):
            raise HTTPException(409, 'Shell approval is stale or unavailable')
    return {'state':'accepted'}


async def _file_evidence(worker, current, owner, account, chat):
    """Find only evidence retained in the original live managed admission."""
    from src.agent_tools.filesystem_tools import current_file_approval
    bridge = getattr(worker, '_bridge', None)
    if bridge is None or not current(): return [], False
    try:
        async with bridge._managed_execution_lock:
            from itertools import islice
            records = list(islice(((entry, record, record.get('fileApprovalEvidence'))
                for entry in bridge._managed_actor_turns.values() if isinstance(entry, dict)
                and entry.get('state') == 'active' and not entry.get('backgroundStartClosing')
                for record in entry.get('backgroundStartAdmitted', {}).values() if isinstance(record, dict)), 33))
        result = []
        complete = len(records) <= 32
        records = records[:32]
        for entry, record, evidence in records:
            if type(evidence) is not ManagedFileApproval: continue
            if evidence.owner != owner or evidence.account != account or evidence.chat != chat: continue
            req = evidence.request
            source = dict(evidence.source)
            if (record.get('fileApproval') != (req.request_id, owner, chat, account, evidence.pending)
                    or source.get('rootOperationID') != req.turn_id
                    or not all(isinstance(source.get(key), str) and source[key] for key in
                               ('sessionID','rootOperationID','actorID','innerTurnID','messageID','toolCallID','toolName'))
                    or type(source.get('executionRevision')) is not int
                    or current_file_approval(req.request_id, owner=owner, session_id=chat, account_id=account) is not evidence.pending):
                complete = False
                continue
            try: live = await evidence.current()
            except Exception: live = False
            if not live or not current():
                complete = False
                continue
            async with bridge._managed_execution_lock:
                if (not current() or current_file_approval(req.request_id, owner=owner, session_id=chat, account_id=account) is not evidence.pending
                        or entry.get('policyRevision') != evidence.policy_revision
                        or getattr(bridge, '_managed_provider_generation', None) != evidence.holder
                        or getattr(getattr(worker, '_managed_callbacks', None), 'holder_id', None) != evidence.holder
                        or getattr(worker, 'installed_generation', None) != evidence.generation
                        or not any(value is entry for value in bridge._managed_actor_turns.values())
                        or not any(value is record for value in entry.get('backgroundStartAdmitted', {}).values())
                        or entry.get('state') != 'active' or entry.get('backgroundStartClosing')
                        or record.get('fileApprovalEvidence') is not evidence
                        or record.get('fileApproval') != (req.request_id,owner,chat,account,evidence.pending)):
                    complete = False
                    continue
            result.append((entry, record, evidence))
        return result, complete
    except Exception:
        return [], False


async def read_managed_files(worker, current, owner, account, chat):
    found, complete = await _file_evidence(worker, current, owner, account, chat)
    items = []
    for _, _, evidence in found:
        req = evidence.request
        choices = offered(req)
        import os
        raw = dict(evidence.pending.detail)
        try:
            base = os.path.realpath(evidence.pending.workspace)
            detail = {'action':raw['action']}
            for key in ('source','destination'):
                if raw.get(key):
                    target = os.path.realpath(raw[key])
                    if os.path.commonpath((base,target)) != base: raise ValueError('outside workspace')
                    detail[key] = os.path.relpath(target,base)
            if raw.get('source_fingerprint'): detail['source_fingerprint'] = raw['source_fingerprint']
        except (KeyError, TypeError, ValueError):
            complete = False
            continue
        payload = {'request_id':req.request_id,'session_id':chat,'turn_id':req.turn_id,
            'revision':req.revision,'permission_type':req.title,'detail':detail,
            'options':choices,'source':dict(evidence.source)}
        if choices and len(json.dumps(payload, ensure_ascii=False).encode()) <= 32768:
            items.append({'kind':'file_approval','request':payload})
        else: complete = False
    if not current(): raise HTTPException(403, 'TUI credential or account changed')
    return items, complete


async def resolve_managed_files(worker, current, owner, account, chat, value):
    from src.agent_tools.filesystem_tools import resolve_file_approval
    found, _ = await _file_evidence(worker, current, owner, account, chat)
    matches = [(entry, record, evidence) for entry, record, evidence in found
        if evidence.request.request_id == value['request_id'] and evidence.request.turn_id == value['turn_id']
        and evidence.request.revision == value['revision']]
    if len(matches) != 1: raise HTTPException(409, 'File approval is stale or unavailable')
    entry, record, evidence = matches[0]
    option = value.get('option')
    if option not in offered(evidence.request): raise HTTPException(400, 'Approval choice unavailable')
    if not current(): raise HTTPException(403, 'TUI credential or account changed')
    bridge = getattr(worker, '_bridge', None)
    async with bridge._managed_execution_lock:
        if (not current() or entry.get('policyRevision') != evidence.policy_revision
                or getattr(bridge, '_managed_provider_generation', None) != evidence.holder
                or getattr(getattr(worker, '_managed_callbacks', None), 'holder_id', None) != evidence.holder
                or getattr(worker, 'installed_generation', None) != evidence.generation
                or not any(value is entry for value in bridge._managed_actor_turns.values())
                or not any(value is record for value in entry.get('backgroundStartAdmitted', {}).values())
                or entry.get('state') != 'active' or entry.get('backgroundStartClosing')
                or record.get('fileApprovalEvidence') is not evidence
                or record.get('fileApproval') != (evidence.request.request_id,owner,chat,account,evidence.pending)):
            raise HTTPException(409, 'File approval is stale or unavailable')
        if not resolve_file_approval(evidence.request.request_id, option, owner=owner,
                session_id=chat, account_id=account, expected_pending=evidence.pending):
            raise HTTPException(409, 'File approval is stale or unavailable')
    return {'state':'accepted'}
