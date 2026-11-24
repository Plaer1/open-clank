"""Managed Evolve callback using existing actor, Files and History authorities."""
from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path


async def dispatch(bridge, request: dict) -> dict:
    """Refuse if any original managed-call authority cannot be revalidated."""
    unavailable = {'state': 'unavailable'}
    identity = {k: request[k] for k in ('sessionID','rootOperationID','actorID','executionRevision','innerTurnID')}
    try:
        admission = await bridge._handle_managed_tool_admit({**identity, 'toolName':'evolve_hexes',
            'kind':'tool', 'toolCallID':request['toolCallID']})
        invocation = admission.get('invocationID')
        if admission.get('allowed') is not True or not isinstance(invocation, str) or not invocation:
            return unavailable
        frozen = await bridge.resolve_managed_policy_invocation(identity, invocation, 'evolve_hexes', request['toolCallID'])
        from core.models import get_session_manager_instance
        manager = get_session_manager_instance()
        state = getattr(manager, 'lifecycle_context', None) if manager else None
        auth = getattr(state, 'auth_manager', None)
        repository = getattr(state, 'files_policy_repository', None)
        barrier = getattr(state, 'account_request_barrier', None)
        history = getattr(state, 'history_supervisor', None)
        provider = getattr(state, 'memory_provider', None)
        supervisor = getattr(state, 'mimo_supervisor', None)
        if not all((auth, repository, barrier, history, provider, supervisor)):
            return unavailable
        owner, account = frozen.get('owner'), frozen.get('ownerAccountID')
        workspace_id, cwd = frozen.get('authorityWorkspaceID'), frozen.get('canonicalCwd')
        if not all(isinstance(x, str) and x for x in (owner, account, workspace_id, cwd)):
            return unavailable
        pool = supervisor.owner_lifecycle_pool()
        worker = pool.worker_for_owner(owner)
        worker_generation = getattr(worker, 'installed_generation', None)
        holder = getattr(bridge, '_managed_provider_generation', None)
        epoch = pool._owner_lifecycle_epoch(owner)
        process = getattr(worker, '_proc', None)
        callbacks = getattr(worker, '_managed_callbacks', None)
        if (not worker or getattr(worker, '_bridge', None) is not bridge or not worker.is_alive()
                or process is None or getattr(callbacks, 'holder_id', None) != holder
                or not holder or type(worker_generation) is not int):
            return unavailable
        from src.openclank.workspace_policy_service import resolve_owned_workspace
        from src.general_hex_runtime import active_hex_db_path, prepare_hex_authority
        from src.project_hex import get_project
        db = prepare_hex_authority(active_hex_db_path(provider))
        project = get_project(request['projectID'], owner=owner, db_path=db)
        workspace = resolve_owned_workspace(repository, workspace_id=workspace_id, owner_username=owner,
                                             auth_manager=auth, purpose='agent_workspace')
        root = workspace.path
        if not project or project['workspace_id'] != workspace_id or Path(project['canonical_root']).resolve() != Path(root).resolve():
            return unavailable
        try: Path(cwd).resolve().relative_to(Path(root).resolve())
        except ValueError: return unavailable
        location = repository.get_location(workspace.workspace.location_id)
        from src.project_hex import require_hex_activation
        resolved = require_hex_activation(root, owner=owner, project_id=request['projectID'], db_path=db, workspace_root=root)
        paths = [Path(resolved.contract_path), Path(root) / 'AGENTS.md']
        relative = [str(path.relative_to(Path(location.canonical_path))).replace('\\','/') for path in paths]
        generation = repository.generation()
        def files_allowed():
            for name in relative:
                decision = repository.resolve(subject_id=account, is_admin=bool(auth.is_admin(owner)), origin='agent',
                    location_id=location.id, capability='write', workspace_id=workspace_id, relative_resource=name,
                    observation_only=True)
                if not decision.allowed: return False
            return True
        if not files_allowed(): return unavailable
        key = bridge._managed_turn_key(identity)
        async with bridge._managed_execution_lock:
            turn = bridge._managed_actor_turns.get(key)
            execution = bridge._managed_actor_executions.get(key[:4])
            owner_admission = execution.get('ownerAdmission') if isinstance(execution, dict) else None
            owner_barrier = execution.get('ownerAdmissionBarrier') if isinstance(execution, dict) else None
            original_credential = execution.get('originalCredentialCurrent') if isinstance(execution, dict) else None
            if (not turn or not execution or owner_barrier is not barrier or owner_admission is None
                    or not callable(original_credential) or execution.get('ownerAdmissionAccount') != account):
                return unavailable
            request_digest = hashlib.sha256(json.dumps(request, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()).hexdigest()
            calls = turn.setdefault('evolveCallBindings', {})
            if not isinstance(calls, dict) or (len(calls) >= 128 and request['toolCallID'] not in calls):
                return unavailable
            if request['toolCallID'] in calls and calls[request['toolCallID']] != request_digest:
                return unavailable
            calls[request['toolCallID']] = request_digest
        if not await bridge._input_origin_current(identity, request['sourceSetGeneration'], request['sourceSetSHA256']):
            return unavailable
        actor = 'managed-' + hashlib.sha256(json.dumps([account, identity['actorID']], separators=(',', ':')).encode()).hexdigest()
        eligible = []
        for item in history.authorized_roots:
            try:
                base = Path(item['canonical_path']).resolve()
                Path(root).resolve().relative_to(base)
                if account not in (item.get('account_ids') or ()): continue
                if item.get('actor_ids') and actor not in item['actor_ids']: continue
                if item.get('workspace_ids') and workspace_id not in item['workspace_ids']: continue
                eligible.append((len(base.parts), str(item['root_id']), str(base)))
            except (KeyError, TypeError, ValueError, OSError):
                continue
        if not eligible: return unavailable
        _, root_id, root_path = max(eligible)
        from src.openclank.history_capture import HistoryContext
        context = HistoryContext(actor_id=actor, account_id=account, workspace_id=workspace_id,
            client=history.client_for(actor, account), actor_kind='agent', session_id=frozen['stableChatID'],
            run_id=identity['rootOperationID'], tool_id='evolve_hexes', root_bindings=((root_id, root_path),))
        async def recheck():
            try:
                if (get_session_manager_instance() is not manager or getattr(manager, 'lifecycle_context', None) is not state
                        or getattr(state, 'auth_manager', None) is not auth or getattr(state, 'files_policy_repository', None) is not repository
                        or getattr(state, 'account_request_barrier', None) is not barrier or getattr(state, 'history_supervisor', None) is not history
                        or not any(str(item.get('root_id')) == root_id and str(Path(item.get('canonical_path') or '').resolve()) == root_path
                            and account in (item.get('account_ids') or ()) and (not item.get('actor_ids') or actor in item['actor_ids'])
                            and (not item.get('workspace_ids') or workspace_id in item['workspace_ids']) for item in history.authorized_roots)
                        or getattr(state, 'memory_provider', None) is not provider or active_hex_db_path(provider) != db
                        or get_project(request['projectID'], owner=owner, db_path=db) != project
                        or getattr(state, 'mimo_supervisor', None) is not supervisor
                        or supervisor.owner_lifecycle_pool() is not pool or pool.worker_for_owner(owner) is not worker
                        or getattr(worker, '_bridge', None) is not bridge or not worker.is_alive()
                        or getattr(worker, 'installed_generation', None) != worker_generation
                        or getattr(worker, '_proc', None) is not process or getattr(worker, '_managed_callbacks', None) is not callbacks
                        or getattr(callbacks, 'holder_id', None) != holder
                        or getattr(bridge, '_managed_provider_generation', None) != holder or pool._owner_lifecycle_epoch(owner) != epoch
                        or auth.account_id(owner) != account
                        or auth.username_for_account_id(account) != owner or repository.generation() != generation
                        or execution.get('originalCredentialCurrent') is not original_credential
                        or original_credential() is not True
                        or getattr(owner_admission, 'released', True) or not await barrier.is_live(owner, owner_admission)):
                    return False
                if not await bridge._input_origin_current(identity, request['sourceSetGeneration'], request['sourceSetSHA256']):
                    return False
                latest = await bridge.resolve_managed_policy_invocation(identity, invocation, 'evolve_hexes', request['toolCallID'])
                if latest != frozen: return False
                async with bridge._managed_execution_lock:
                    if (bridge._managed_actor_turns.get(key) is not turn or bridge._managed_actor_executions.get(key[:4]) is not execution
                            or turn.get('evolveCallBindings', {}).get(request['toolCallID']) != request_digest):
                        return False
                    source_set=turn.get('inputOriginSet')
                    privacy=turn.get('receiptPrivacy')
                    if privacy is not None and (type(privacy) is not dict or type(privacy.get('receipts')) is not tuple):return False
                    if source_set is None:
                        if (identity['actorID']=='main' or request['sourceSetGeneration']!=0 or request['sourceSetSHA256']!=hashlib.sha256(b'[]').hexdigest()
                                or (privacy is not None and privacy['receipts']!=())):return False
                    else:
                        if (not source_set.get('valid') or source_set.get('generation')!=request['sourceSetGeneration']
                                or source_set.get('sha256')!=request['sourceSetSHA256']):return False
                        if identity['actorID']=='main' and identity['sessionID']==turn.get('rootSessionID'):
                            original=turn.get('forwardPairInput'); root_link=source_set.get('root')
                            if (type(original) is not dict or type(root_link) is not dict or original.get('identity')!=identity
                                    or any(original.get(src)!=root_link.get(dst) for src,dst in
                                        (('messageID','rootMessageID'),('partID','rootPartID'),('textSHA256','rootTextSHA256')))):return False
                        admitted={(item['requestID'],item['payloadHash']) for item in source_set['receipts']}
                        if privacy is not None and any(item not in admitted for item in privacy['receipts']):return False
                fresh = resolve_owned_workspace(repository, workspace_id=workspace_id, owner_username=owner,
                                                 auth_manager=auth, purpose='agent_workspace')
                return (fresh.workspace == workspace.workspace and Path(fresh.path).resolve() == Path(root).resolve()
                        and files_allowed())
            except Exception:
                return False
        if not await recheck(): return unavailable
        loop = asyncio.get_running_loop()
        def current():
            try: return asyncio.run_coroutine_threadsafe(recheck(), loop).result(timeout=10) is True
            except Exception: return False
        def run():
            from src import project_hex as owner_api
            common = dict(owner=owner, account_id=account, project_id=request['projectID'], db_path=db)
            operation = request['operation']
            if operation in {'preview','apply'}:
                if request.get('trigger') != {'kind':'agent_proposal'}: return unavailable
                trigger = {'kind':'agent_proposal', 'toolCallID':request['toolCallID'], 'rootOperationID':identity['rootOperationID']}
                if owner_api.inspect_project_policy(request['projectID'], owner=owner, db_path=db).get('projection', {}).get('source_revision') != request['policyRevision']:
                    return unavailable
                if operation == 'preview':
                    value = owner_api.preview_evolve_candidate(**common, expected_mode_revision=request['modeRevision'],
                        expected_contract_hash=request['expectedContractHash'], candidate_text=request['candidateText'],
                        reason=request['reason'], trigger=trigger)
                    if value['candidate_sha256'] != request['candidateSHA256'] or not current(): return unavailable
                else:
                    value = owner_api.apply_evolve_candidate(**common, actor_id=actor, expected_mode_revision=request['modeRevision'],
                        expected_policy_revision=request['policyRevision'], expected_contract_hash=request['expectedContractHash'],
                        candidate_text=request['candidateText'], candidate_sha256=request['candidateSHA256'], reason=request['reason'],
                        trigger=trigger, history_context=context, current=current)
            else:
                fn = {'reconcile':owner_api.reconcile_evolve_candidate,'rollback':owner_api.rollback_evolve_candidate,
                      'rollback-reconcile':owner_api.reconcile_evolve_rollback}[operation]
                value = fn(**common, actor_id=actor, operation_id=request['operationID'], history_context=context, current=current)
            names = {'operation_id':'operationID','transition_id':'transitionID','history_action_id':'historyActionID',
                     'contract_hash':'contractHash','candidate_sha256':'candidateSHA256','mode_revision':'modeRevision',
                     'policy_revision':'policyRevision','trigger_evidence':'triggerEvidence','state':'state','diff':'diff','reason':'reason'}
            result = {names[k]:v for k,v in value.items() if k in names and v is not None}
            if isinstance(result.get('diff'), str) and len(result['diff']) > 524288:
                return unavailable
            if isinstance(result.get('reason'), str): result['reason'] = result['reason'][:4000]
            return result
        task = asyncio.create_task(asyncio.to_thread(run))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            await asyncio.shield(task)
            raise
    except asyncio.CancelledError:
        raise
    except Exception:
        return unavailable
