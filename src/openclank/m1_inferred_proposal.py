"""M1 inferred proposals: admitted turn -> existing private Native F8 owner.

A host-user body is an anchor snapshot, not proof of model causation or full
context. This adapter does not expose or accept a source selector from models.
"""
from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import re
import weakref
from dataclasses import asdict

_KINDS = frozenset(('fact', 'contact', 'project', 'goal', 'identity', 'preference', 'task', 'unknown'))
_OPERATION = re.compile(r'm1op_[0-9a-f]{64}\Z')
_HEX = re.compile(r'[0-9a-f]{64}\Z')
_FIELDS = ('sessionID', 'rootOperationID', 'actorID', 'executionRevision', 'innerTurnID')
_CAPABILITY = 'preextracted_host_anchor_v1'


class _HostAnchorAdmission:
    __slots__ = ('__weakref__',)


# A process-local identity capability shared only with the private provider owner.
_host_anchor_admissions = weakref.WeakKeyDictionary()


def _issue_host_anchor_admission(provider, body, before_issue):
    if provider is None or type(body) is not dict or not callable(before_issue):
        raise ValueError('host anchor admission unavailable')
    encoded = json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('utf-8')
    if len(encoded) > 32768:
        raise ValueError('host anchor admission unavailable')
    admission = _HostAnchorAdmission()
    _host_anchor_admissions[admission] = {'provider': provider, 'body': encoded, 'guard': before_issue, 'projected': False}
    return admission


def _project_host_anchor_admission(admission, provider):
    if type(admission) is not _HostAnchorAdmission:
        raise ValueError('host anchor admission unavailable')
    record = _host_anchor_admissions.get(admission)
    if not record or record['provider'] is not provider or record['projected']:
        raise ValueError('host anchor admission unavailable')
    record['projected'] = True
    body = json.loads(record['body'].decode('utf-8'))
    def current():
        try:
            return (_host_anchor_admissions.get(admission) is record and record['provider'] is provider and
                    record['projected'] is True and
                    json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('utf-8') == record['body'] and
                    record['guard']() is True)
        except Exception:
            return False
    return body, current


def _retire_host_anchor_admission(admission):
    if type(admission) is _HostAnchorAdmission:
        _host_anchor_admissions.pop(admission, None)


def _digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, separators=(',', ':')).encode('utf-8')).hexdigest()


def _args_digest(request, status):
    return _digest(['openclank.memory-inferred.v1', 'status' if status else 'save',
                    None if status else request['kind'], request['operationID'] if status else request['text']])


def _source_ref(*, identity, account, chat, authority_workspace):
    """Read the actual Host root body and original privacy decision; no model selector."""
    from core.database import SessionLocal
    from sqlalchemy import text
    from src.memery_external_sources import SourceRef, _source_ref_error
    with SessionLocal() as db:
        row = db.execute(text("SELECT m.source_body_revision,m.source_body_hash,s.workspace_id,i.origin_json,i.origin_state "
            "FROM chat_messages m JOIN sessions s ON s.id=m.session_id "
            "JOIN host_source_delivery_intent i ON i.owner=:account AND i.chat_id=s.id AND i.message_id=m.id "
            "AND i.part_id='body' AND i.revision=1 WHERE m.id=:message AND m.session_id=:chat AND m.role='user'"),
            {'account': account, 'message': identity['rootOperationID'], 'chat': chat}).first()
    if not row or row[0] != 1 or not isinstance(row[1], str) or not _HEX.fullmatch(row[1]) or (row[2] or '') != authority_workspace:
        raise ValueError('source unavailable')
    origin = json.loads(row[3]); choices = json.loads(origin['choices_json'])
    if (origin.get('capture_eligible') is not True or row[4] not in ('pending', 'delivered') or
            any(choices.get(k) is not False for k in ('incognito', 'noMemory', 'compareMode'))):
        raise ValueError('source privacy unavailable')
    ref = SourceRef(2 if row[2] is None else 1, 'host_user_body', account, chat, 'main',
                    identity['rootOperationID'], 'body', 1, row[1], row[2], None)
    if _source_ref_error(ref):
        raise ValueError('source unavailable')
    return ref


async def dispatch(bridge, request: dict, *, status: bool) -> dict:
    """Only the bridge invokes this after closed method-schema validation."""
    unavailable = {'state': 'unavailable'}
    issued = False
    operation_id = None
    try:
        from core.database import SessionLocal
        from core.models import get_session_manager_instance
        from src.memery_external_sources import CurrentHostUserSource
        from src.openclank.chat_source_bridge import canonical_source_provider
        from src.openclank.conversation_archive import get_conversation_archive
        from src.openclank.memory_job_admission import TargetAuthority
        from src.openclank.memory_policy import STORE
        from src.openclank.memory_policy_projection import policy_coordinator

        identity = {field: request[field] for field in _FIELDS}
        tool_call = request['toolCallID']; message = request['messageID']
        revision = identity['executionRevision']
        if type(revision) is not int or revision < 0 or revision > 9007199254740991: return unavailable
        if (not isinstance(tool_call, str) or not tool_call or not isinstance(message, str) or not message or
                not isinstance(request.get('argsSHA256'), str) or request['argsSHA256'] != _args_digest(request, status)):
            return unavailable
        if status:
            operation_id = request['operationID']
            if not isinstance(operation_id, str) or not _OPERATION.fullmatch(operation_id): return unavailable
        else:
            candidate = request['text']
            if (request['kind'] not in _KINDS or not isinstance(candidate, str) or not candidate or
                    candidate != candidate.strip() or len(candidate.encode('utf-8')) > 4000): return unavailable
        key = bridge._managed_turn_key(identity)
        async with bridge._managed_execution_lock:
            entry = bridge._managed_actor_turns.get(key)
            if not isinstance(entry, dict) or entry.get('state') != 'active': return unavailable
            owner, account, chat, policy_revision = (entry.get('owner'), entry.get('ownerAccountID'),
                                                       entry.get('stableChatID'), entry.get('policyRevision'))
            turn_snapshot = {field: entry.get(field) for field in ('owner','ownerAccountID','stableChatID','authorityWorkspaceID','mappingRevision','workspaceRevision','mapRevision','policyRevision','holderID')}
        if not all(isinstance(v, str) and v for v in (owner, account, chat)): return unavailable
        execution = bridge._managed_actor_executions.get(key[:4])
        owner_admission = execution.get('ownerAdmission') if isinstance(execution, dict) else None
        owner_barrier = execution.get('ownerAdmissionBarrier') if isinstance(execution, dict) else None
        if (not execution or owner_admission is None or execution.get('ownerAdmissionAccount') != account or
                execution.get('ownerAdmissionStopped') or getattr(owner_admission, 'released', True) or
                getattr(bridge, '_ownerAdmissionHolderStopped', False)): return unavailable
        decision = await bridge._handle_managed_tool_admit({**identity, 'toolName': 'memory', 'kind': 'tool', 'toolCallID': tool_call})
        if decision.get('allowed') is not True: return unavailable
        # The existing policy resolver installs receiptPrivacy during admission.
        async with bridge._managed_execution_lock:
            if bridge._managed_actor_turns.get(key) is not entry or entry.get('state') != 'active': return unavailable
            privacy = copy.deepcopy(entry.get('receiptPrivacy'))
            if not isinstance(privacy, dict) or any(privacy.get(k) is not False for k in ('incognito', 'noMemory')): return unavailable
        resolved = await bridge.resolve_managed_actor_execution(identity['sessionID'], identity['rootOperationID'],
                                                                 identity['actorID'], identity['executionRevision'])
        root = resolved.get('rootSessionID')
        if not isinstance(root, str) or not root: return unavailable
        binding = await bridge._handle_session_binding_read({'sessionID': root})
        if (binding.get('authority') or binding.get('memoryEnabled') is not True or binding.get('owner') != owner or
                binding.get('stableChatID') != chat or binding.get('transition')): return unavailable
        manager = get_session_manager_instance(); state = getattr(manager, 'lifecycle_context', None) if manager else None
        auth = getattr(state, 'auth_manager', None); barrier = getattr(state, 'account_request_barrier', None)
        supervisor = getattr(state, 'mimo_supervisor', None); pool = supervisor.owner_lifecycle_pool() if supervisor else None
        worker = pool.worker_for_owner(owner) if pool else None
        provider = canonical_source_provider(state) if state is not None else None
        coordinator = policy_coordinator()
        if (not auth or not barrier or owner_barrier is not barrier or not worker or not provider or not coordinator or
                coordinator.projection is not getattr(state, 'memory_policy_projection', None) or
                auth.account_id(owner) != account or auth.username_for_account_id(account) != owner or
                getattr(worker, '_bridge', None) is not bridge or not worker.is_alive()): return unavailable
        holder = bridge._managed_provider_generation; generation = worker.installed_generation
        process = getattr(worker, '_proc', None)
        root_state = bridge._turns.get(root)
        owner_state = pool._states.get(pool._key(owner))
        epoch = pool._owner_lifecycle_epoch(pool._key(owner))
        if (not process or not root_state or not owner_state or root_state.root_operation_id != identity['rootOperationID'] or
                getattr(root_state, 'provider_root_closing', False) or not await barrier.is_live(owner, owner_admission)): return unavailable
        workspace = binding.get('canonicalCwd'); workspace_id = binding.get('authorityWorkspaceID')
        if not holder or not isinstance(workspace, str) or not isinstance(workspace_id, str): return unavailable
        source_read = getattr(worker, 'session_memory_inferred_source_request', None)
        if not callable(source_read): return unavailable
        source_request = {**{f: identity[f] for f in ('actorID','rootOperationID','executionRevision','innerTurnID')},
                          'messageID': message, 'toolCallID': tool_call, 'toolName': 'memory', 'argsSHA256': request['argsSHA256'],
                          'stableChatID': chat}
        if identity['sessionID'] != root: source_request['sourceSessionID'] = identity['sessionID']
        authority = TargetAuthority(auth_manager=auth, supervisor=pool)
        target = await asyncio.to_thread(authority.resolve, account)
        if target.memory_owner != provider._scope(owner).owner or target.memory_workspace != provider._workspace_id: return unavailable
        source_authority = CurrentHostUserSource(owner=owner, auth_manager=auth, session_factory=SessionLocal,
            archive=get_conversation_archive(), workspace_repository=getattr(state, 'files_policy_repository', None),
            policy_store=STORE, guarded_preparation=getattr(provider, '_guarded_preparation', None))
        privileges = auth.get_privileges(owner); allowed = privileges.get('can_manage_memory', True) if isinstance(privileges, dict) else False
        if allowed is not True: return unavailable
        session = manager.get_session(chat)
        if session is None or getattr(session, 'owner', None) != owner or getattr(session, 'incognito', False) or getattr(session, 'archived', False): return unavailable
        if not status:
            operation_id = 'm1op_' + _digest(['openclank.m1.operation.v1', account, identity['sessionID'],
                identity['rootOperationID'], identity['actorID'], identity['executionRevision'], identity['innerTurnID'], tool_call])


        async with barrier.admitted(owner):
            async with bridge._durable_session_map.async_chat_admission_lock(chat, blocking=False, existing_only=True) as guard:
                mapping = bridge._durable_session_map
                mapping.validate_chat_guard(chat, guard)
                def current():
                    try:
                        privacy_now = entry.get('receiptPrivacy')
                        root_context = bridge._session_context.get(root)
                        return (isinstance(root_context, dict) and root_context.get('root_operation_id') == identity['rootOperationID'] and
                            root_context.get('memory_read_allowed') is True and not root_context.get('incognito') and not root_context.get('auxiliary') and not getattr(bridge, '_ownerAdmissionHolderStopped', False) and
                            bridge._managed_actor_executions.get(key[:4]) is execution and not execution.get('ownerAdmissionStopped') and
                            execution.get('ownerAdmissionBarrier') is owner_barrier and execution.get('ownerAdmission') is owner_admission and
                            execution.get('ownerAdmissionAccount') == account and not getattr(owner_admission, 'released', True) and
                            not entry.get('ownerAdmissionStopped') and entry.get('ownerAdmission') is owner_admission and
                            entry.get('ownerAdmissionBarrier') is owner_barrier and entry.get('ownerAdmissionAccount') == account and
                            bridge._turns.get(root) is root_state and root_state.root_operation_id == identity['rootOperationID'] and
                            not getattr(root_state, 'provider_root_closing', False) and not getattr(root_state, 'privacy_capture_veto', False) and
                            bridge._managed_actor_turns.get(key) is entry and entry.get('state') == 'active' and
                            all(entry.get(field) == value for field, value in turn_snapshot.items()) and
                            entry.get('policyRevision') == policy_revision and entry.get('ownerAccountID') == account and
                            privacy_now == privacy and isinstance(privacy_now, dict) and all(privacy_now.get(k) is False for k in ('incognito','noMemory')) and
                            auth.account_id(owner) == account and auth.username_for_account_id(account) == owner and
                            auth.get_privileges(owner).get('can_manage_memory', True) is True and
                            manager.get_session(chat) is session and not getattr(session, 'incognito', False) and not getattr(session, 'archived', False) and
                            STORE.read_eligibility(owner, read_privilege=True, write_privilege=False, incognito=False, no_memory=False, compare_mode=False).get('read_allowed') is True and
                            supervisor.owner_lifecycle_pool() is pool and pool.worker_for_owner(owner) is worker and
                            pool._states.get(pool._key(owner)) is owner_state and owner_state.active is worker and owner_state.status == 'ready' and
                            owner_state.generation == generation and pool._owner_lifecycle_epoch(pool._key(owner)) == epoch and
                            worker.is_alive() and getattr(worker, '_proc', None) is process and getattr(worker, '_bridge', None) is bridge and worker.installed_generation == generation and
                            bridge._managed_provider_generation == holder and getattr(getattr(worker, '_managed_callbacks', None), 'holder_id', None) == holder and
                            canonical_source_provider(state) is provider and policy_coordinator() is coordinator and
                            authority.resolve(account) == target and
                            coordinator.checked_refs(owner, account, target.memory_workspace) == refs)
                    except Exception: return False
                refs = coordinator.checked_refs(owner, account, target.memory_workspace)
                async def proof():
                    result = await source_read(chat, root, workspace, source_request, owner=owner)
                    return result == {'state': 'current', 'argsSHA256': request['argsSHA256']}
                read_policy = STORE.read_eligibility(owner, read_privilege=allowed, write_privilege=False,
                    incognito=False, no_memory=False, compare_mode=False)
                if read_policy.get('eligibility_state') != 'available' or read_policy.get('read_allowed') is not True: return unavailable
                if not current() or not await barrier.is_live(owner, owner_admission) or await bridge._handle_session_binding_read({'sessionID': root}) != binding or not await proof(): return unavailable
                if not await provider._host_negotiate_native_memory_ops(owner=target.memory_owner, workspace_id=target.memory_workspace): return unavailable
                if _CAPABILITY not in provider._native_memory_ops_support(): return unavailable
                if not current() or not await barrier.is_live(owner, owner_admission) or await bridge._handle_session_binding_read({'sessionID': root}) != binding or not await proof(): return unavailable
                # Name admission only establishes that some memory operation
                # is allowed. Recheck the actual canonical operation after the
                # genuine Native source proof, without fabricating a tool part.
                async def operation_policy_current():
                    resolver = getattr(bridge, '_managed_tool_policy_resolver', None)
                    if not callable(resolver) or not current(): return False
                    actual = await bridge.resolve_managed_actor_execution(identity['sessionID'],identity['rootOperationID'],identity['actorID'],identity['executionRevision'])
                    if actual.get('rootSessionID') != root: return False
                    permitted = await resolver(identity,entry,'recall_memory' if status else 'manage_memory','tool',actual)
                    return permitted is True and current()
                if not await operation_policy_current(): return unavailable
                base = {'version': 1, 'owner': target.memory_owner, 'workspace_id': target.memory_workspace,
                        'account_id': account, 'policy_projection': refs, 'request_key': operation_id}
                if status:
                    result = await provider._host_preextracted_host_anchor_status(base)
                    if not current() or not await barrier.is_live(owner, owner_admission) or await bridge._handle_session_binding_read({'sessionID': root}) != binding or not await proof() or not await operation_policy_current(): return unavailable
                    state_name = result.get('state') if isinstance(result, dict) else None
                    if state_name in ('terminal', 'unknown'): return {'state': state_name, 'operationID': operation_id}
                    original = result.get('result') if state_name == 'committed' else None
                    items = original.get('items') if isinstance(original, dict) else None
                    sha = result.get('body_sha256')
                    if not isinstance(items, list) or len(items) != 1 or not isinstance(sha, str) or not _HEX.fullmatch(sha): return unavailable
                    item = items[0]
                    if not isinstance(item, dict) or item.get('state') not in ('staged','duplicate','conflict'): return unavailable
                    answer = {'state':'committed','operationID':operation_id,'bodySHA256':sha,'candidateID':item['candidate_id'],'disposition':item['state']}
                    if item.get('state') == 'conflict': answer['conflictID'] = item['conflict_id']
                    return answer
                source = await asyncio.to_thread(_source_ref, identity=identity, account=account, chat=chat, authority_workspace=workspace_id)
                if (await asyncio.to_thread(source_authority.verify_feedback_origin, source)).get('state') != 'available': return unavailable
                capture = STORE.capture_eligibility(owner, read_privilege=allowed, write_privilege=allowed, incognito=False, no_memory=False, compare_mode=False)
                if capture.get('eligibility_state') != 'available' or capture.get('capture_allowed') is not True: return unavailable
                if not current() or not await barrier.is_live(owner, owner_admission) or await bridge._handle_session_binding_read({'sessionID': root}) != binding or not await proof() or not await operation_policy_current(): return unavailable
                body = {**base, 'items': [{'kind': request['kind'], 'text': candidate,
                    'source': {'kind': 'host_user_body_anchor', 'reference': asdict(source), 'coverage': 'host_user_body_anchor_only'}}]}
                def before_issue():
                    nonlocal issued
                    try:
                        mapping.validate_chat_guard(chat, guard)
                        live_capture = STORE.capture_eligibility(owner, read_privilege=allowed, write_privilege=allowed, incognito=False, no_memory=False, compare_mode=False)
                        ok = (current() and live_capture.get('eligibility_state') == 'available' and live_capture.get('capture_allowed') is True and
                              _source_ref(identity=identity, account=account, chat=chat, authority_workspace=workspace_id) == source and
                              source_authority.verify_feedback_origin(source).get('state') == 'available')
                        if ok: issued = True
                        return ok
                    except Exception: return False
                admission = _issue_host_anchor_admission(provider, body, before_issue)
                try:
                    result = await provider._host_preextracted_host_anchor_stage(admission)
                finally:
                    _retire_host_anchor_admission(admission)
                if not current() or await bridge._handle_session_binding_read({'sessionID': root}) != binding or not await proof():
                    return {'state':'outcome_unknown','operationID':operation_id} if issued else unavailable
                items = result.get('items') if isinstance(result, dict) else None
                if not isinstance(items, list) or len(items) != 1 or not isinstance(items[0], dict):
                    return {'state':'outcome_unknown','operationID':operation_id} if issued else unavailable
                item = items[0]
                if item.get('state') not in ('staged','duplicate','conflict'): return {'state':'outcome_unknown','operationID':operation_id} if issued else unavailable
                answer = {'state': {'staged':'pending', 'duplicate':'duplicate', 'conflict':'conflict'}[item['state']], 'operationID':operation_id, 'candidateID':item['candidate_id']}
                if item['state'] == 'conflict': answer['conflictID'] = item['conflict_id']
                return answer
    except asyncio.CancelledError:
        raise
    except Exception:
        return {'state':'outcome_unknown','operationID':operation_id} if issued and operation_id else unavailable
