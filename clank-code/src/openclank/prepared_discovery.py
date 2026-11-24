"""Host-only S10 read adapter over existing scoped skill and actor authorities.

Nothing in this module registers a route, writes a store, emits a memory event,
loads context for a model, or publishes a skill.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import platform

from src.openclank.analysis_snapshot_host import _owned


class PreparedUnavailable(ValueError):
    pass


class PreparedSkillsReader:
    """Pin an existing manager; re-authorize the exact active actor on every read."""

    def __init__(self, *, state, manager):
        from services.memory.skills import SkillsManager
        from src.constants import DATA_DIR
        if (type(manager) is not SkillsManager or os.path.realpath(manager.data_dir) != os.path.realpath(DATA_DIR)
                or os.path.realpath(manager.skills_root) != os.path.join(os.path.realpath(DATA_DIR), 'skills')):
            raise PreparedUnavailable('prepared skill manager unavailable')
        self._state = state
        self._manager = manager

    async def _actor(self, bridge, identity, *, require_skill=True):
        from src.openclank.acp_bridge import ACPBridge
        if type(bridge) is not ACPBridge or type(identity) is not dict:
            raise PreparedUnavailable('prepared actor unavailable')
        key=bridge._managed_turn_key(identity)
        if require_skill:
            fields=('sessionID','rootOperationID','actorID','executionRevision','innerTurnID')
            if set(identity)!=set(fields): raise PreparedUnavailable('prepared actor unavailable')
            async with bridge._managed_execution_lock:
                initial=bridge._managed_actor_turns.get(key)
                if type(initial) is not dict or initial.get('state')!='active': raise PreparedUnavailable('prepared actor unavailable')
                revision=initial.get('policyRevision')
            decision=await bridge._handle_managed_tool_admit({**identity,'toolName':'skill','kind':'skill_use'})
            if type(decision) is not dict or decision.get('allowed') is not True: raise PreparedUnavailable('prepared actor access unavailable')
            async with bridge._managed_execution_lock:
                if bridge._managed_actor_turns.get(key) is not initial or initial.get('state')!='active' or initial.get('policyRevision')!=revision:
                    raise PreparedUnavailable('prepared actor changed')
                turn={**identity,**{field:initial.get(field) for field in ('owner','stableChatID','authorityWorkspaceID','mappingRevision','workspaceRevision','mapRevision','policyRevision','ownerAccountID')}}
        else:
            turn=await bridge.resolve_managed_recall_turn(identity)
        if turn.get('actorID') != identity.get('actorID') or not turn.get('ownerAccountID'):
            raise PreparedUnavailable('prepared actor unavailable')
        async with bridge._managed_execution_lock:
            entry=bridge._managed_actor_turns.get(key)
            if require_skill and (entry is not initial or entry.get('policyRevision')!=revision):
                raise PreparedUnavailable('prepared actor changed')
            privacy=entry.get('receiptPrivacy') if type(entry) is dict and entry.get('state')=='active' else None
            if (type(privacy) is not dict or type(privacy.get('incognito')) is not bool
                    or type(privacy.get('noMemory')) is not bool or privacy['incognito'] or privacy['noMemory']):
                raise PreparedUnavailable('prepared privacy unavailable')
        if not require_skill and await bridge.resolve_managed_recall_turn(identity) != turn:
            raise PreparedUnavailable('prepared actor changed')
        return turn

    async def _read(self, bridge, identity, *, limit=None, cursor=None, descriptor=None):
        from src.openclank.memory_job_admission import TargetAuthority
        from src.openclank.session_map import OwnerSessionMap
        from src.openclank.memory_policy import STORE
        state=self._state
        turn=await self._actor(bridge,identity)
        owner=turn.get('owner'); account=turn.get('ownerAccountID'); chat=turn.get('stableChatID')
        auth=getattr(state,'auth_manager',None); supervisor=getattr(state,'mimo_supervisor',None)
        barrier=getattr(state,'account_request_barrier',None)
        if any(type(x) is not str or not x for x in (owner,account,chat)) or auth is None or supervisor is None or barrier is None:
            raise PreparedUnavailable('prepared owner unavailable')
        worker=supervisor.worker_for_owner(owner)
        if worker is None or worker.bridge is not bridge:
            raise PreparedUnavailable('prepared holder unavailable')
        pool=supervisor.owner_lifecycle_pool(); authority=TargetAuthority(auth_manager=auth,supervisor=pool)
        target=await asyncio.to_thread(authority.resolve,account)
        mapping=OwnerSessionMap(pool._runtime_home(owner)/'session-map.json',owner)
        actual_platform=platform.system().lower()
        if actual_platform == 'darwin':
            actual_platform = 'macos'
        if actual_platform not in {'linux', 'macos', 'windows'}:
            raise PreparedUnavailable('prepared platform unavailable')
        actor_key=hashlib.sha256(json.dumps({'turn':turn,'target':target.fingerprint,'platform':actual_platform},
            sort_keys=True,separators=(',',':'),ensure_ascii=False).encode()).hexdigest()
        async def current(guard):
            mapping.validate_chat_guard(chat,guard)
            if (supervisor.worker_for_owner(owner) is not worker or worker.bridge is not bridge
                    or auth.account_id(owner)!=account or auth.username_for_account_id(account)!=owner
                    or await asyncio.to_thread(authority.resolve,account)!=target
                    or await self._actor(bridge,identity)!=turn):
                raise PreparedUnavailable('prepared authority changed')
            privileges=auth.get_privileges(owner)
            if type(privileges) is not dict or privileges.get('can_manage_memory') is not True:
                raise PreparedUnavailable('prepared privilege unavailable')
            policy=await asyncio.to_thread(STORE.effective_snapshot,owner,read_privilege=True,write_privilege=True,
                incognito=False,no_memory=False,compare_mode=False)
            if not policy.get('read_allowed'):
                raise PreparedUnavailable('prepared memory policy unavailable')
            mapping.validate_chat_guard(chat,guard)
        async with barrier.admitted(owner):
            async with mapping.async_chat_admission_lock(chat, blocking=False, existing_only=True) as guard:
                await current(guard)
                kwargs={'owner':owner,'account_id':account,'actor_key':actor_key,'platform_name':actual_platform}
                # Metadata is an optional profile: failure defers those entries,
                # while ordinary published skills remain available.
                metadata_check=None
                try:
                    from src.openclank.memory_job_admission import SingleHostProfile
                    from src.openclank.memory_policy_projection import policy_coordinator
                    from src.openclank.chat_source_bridge import canonical_source_provider
                    from services.memory.skill_lifecycle import _SkillMetadataAuthority,_SKILL_METADATA_SEAL
                    provider=canonical_source_provider(state);profile=getattr(state,'s05_profile',None);coordinator=policy_coordinator()
                    reset_factory=getattr(state,'memory_reset_coordinator',None);lifecycle=getattr(state,'account_owner_lifecycle',None)
                    if (type(profile) is not SingleHostProfile or profile.provider is not provider or provider is None or coordinator is None
                        or coordinator.projection is not getattr(state,'memory_policy_projection',None) or not callable(reset_factory) or lifecycle is None
                        or target.memory_owner!=provider._scope(owner).owner or target.memory_workspace!=provider._workspace_id):raise PreparedUnavailable('metadata profile unavailable')
                    async def metadata_check():
                        await current(guard);await asyncio.to_thread(profile.assert_current)
                        if lifecycle.owner_has_active_operation(owner) or reset_factory().owner_is_fenced(owner) or canonical_source_provider(state) is not provider:raise PreparedUnavailable('metadata fenced')
                        refs=await _owned(coordinator.checked_refs,owner,account,target.memory_workspace)
                        if lifecycle.owner_has_active_operation(owner) or reset_factory().owner_is_fenced(owner) or auth.account_id(owner)!=account or auth.username_for_account_id(account)!=owner:raise PreparedUnavailable('metadata changed')
                        if not await provider._host_metadata_capability(owner=target.memory_owner,workspace_id=target.memory_workspace,deadline=asyncio.get_running_loop().time()+0.5):raise PreparedUnavailable('metadata unavailable')
                        result=await provider._host_metadata_read(owner=target.memory_owner,workspace_id=target.memory_workspace,account_id=account,refs=refs,deadline=asyncio.get_running_loop().time()+0.5)
                        await current(guard);await asyncio.to_thread(profile.assert_current)
                        if lifecycle.owner_has_active_operation(owner) or reset_factory().owner_is_fenced(owner) or await _owned(coordinator.checked_refs,owner,account,target.memory_workspace)!=refs:raise PreparedUnavailable('metadata changed')
                        if lifecycle.owner_has_active_operation(owner) or reset_factory().owner_is_fenced(owner) or auth.account_id(owner)!=account or auth.username_for_account_id(account)!=owner:raise PreparedUnavailable('metadata changed')
                        items=result.get('items') if type(result) is dict else None
                        if type(items) is not list or len(items)>128 or any(type(v) is not dict or type(v.get('block_id')) is not str or type(v.get('revision')) is not int for v in items):raise PreparedUnavailable('metadata unavailable')
                        return refs,items
                    meta=await metadata_check()
                    allowed={(v['block_id'],v['revision']) for v in meta[1]}
                    kwargs['metadata_authority']=_SkillMetadataAuthority(_SKILL_METADATA_SEAL,owner=owner,account_id=account,actor=f'user:{owner}',current=lambda _:True,domains=lambda _,values:type(values) is list and all(type(v) is dict and (v.get('block_id'),v.get('revision')) in allowed for v in values))
                    kwargs['metadata_generation']=hashlib.sha256(json.dumps(meta,sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False).encode()).hexdigest()
                except Exception:
                    metadata_check=None
                if descriptor is None:
                    work=asyncio.create_task(asyncio.to_thread(self._manager._prepared_skill_page,**kwargs,limit=limit,cursor=cursor))
                else:
                    work=asyncio.create_task(asyncio.to_thread(self._manager._prepared_skill_hydrate,**kwargs,descriptor=descriptor))
                try:
                    result=await asyncio.shield(work)
                except asyncio.CancelledError:
                    # Do not release the host guard while the bounded reader still owns file locks.
                    while not work.done():
                        try:
                            await asyncio.shield(work)
                        except asyncio.CancelledError:
                            continue
                        except Exception:
                            break
                    raise
                await current(guard)
                if metadata_check is not None and await metadata_check()!=meta:raise PreparedUnavailable('metadata changed')
                return result

    async def page(self, bridge, identity, *, limit=16, cursor=None):
        try:
            return await self._read(bridge,identity,limit=limit,cursor=cursor)
        except Exception as exc:
            if str(exc) == 'prepared snapshot stale':
                return {'version':1,'state':'stale','reason':'restart'}
            return {'version':1,'state':'unavailable','reason':'prepared_authority_unavailable'}

    async def hydrate(self, bridge, identity, *, descriptor):
        try:
            return await self._read(bridge,identity,descriptor=descriptor)
        except Exception:
            return {'version':1,'state':'unavailable','reason':'prepared_authority_unavailable'}

    async def metadata_current(self,bridge,identity,proof):
        """Recheck the published skill and current FM domains under the active turn."""
        try:
            from src.openclank.memory_job_admission import SingleHostProfile,TargetAuthority
            from src.openclank.memory_policy import STORE
            from src.openclank.memory_policy_projection import policy_coordinator
            from src.openclank.chat_source_bridge import canonical_source_provider
            from src.openclank.session_map import OwnerSessionMap
            from services.memory.skill_lifecycle import _SkillMetadataAuthority,_SKILL_METADATA_SEAL
            if type(proof) is not dict or set(proof)!={'skillID','revision','contentHash','snapshotSHA256','metadataReceiptSHA256'}:raise PreparedUnavailable('skill metadata unavailable')
            turn=await self._actor(bridge,identity);owner=turn['owner'];account=turn['ownerAccountID'];chat=turn['stableChatID'];state=self._state
            auth=state.auth_manager;supervisor=state.mimo_supervisor;worker=supervisor.worker_for_owner(owner);barrier=state.account_request_barrier
            provider=canonical_source_provider(state);profile=getattr(state,'s05_profile',None);coordinator=policy_coordinator()
            if worker is None or worker.bridge is not bridge or type(profile) is not SingleHostProfile or profile.provider is not provider or coordinator is None or coordinator.projection is not getattr(state,'memory_policy_projection',None):raise PreparedUnavailable('skill authority unavailable')
            target_authority=TargetAuthority(auth_manager=auth,supervisor=supervisor.owner_lifecycle_pool());target=await asyncio.to_thread(target_authority.resolve,account)
            if target.memory_owner!=provider._scope(owner).owner or target.memory_workspace!=provider._workspace_id:raise PreparedUnavailable('skill target unavailable')
            reset_factory=getattr(state,'memory_reset_coordinator',None);lifecycle=getattr(state,'account_owner_lifecycle',None)
            if not callable(reset_factory) or lifecycle is None:raise PreparedUnavailable('skill lifecycle unavailable')
            mapping=OwnerSessionMap(supervisor.owner_lifecycle_pool()._runtime_home(owner)/'session-map.json',owner)
            async def current(guard):
                mapping.validate_chat_guard(chat,guard);await asyncio.to_thread(profile.assert_current)
                if (supervisor.worker_for_owner(owner) is not worker or worker.bridge is not bridge or auth.account_id(owner)!=account or auth.username_for_account_id(account)!=owner or await target_authority_async()!=target or await self._actor(bridge,identity)!=turn or canonical_source_provider(state) is not provider):raise PreparedUnavailable('skill authority changed')
                if lifecycle.owner_has_active_operation(owner) or reset_factory().owner_is_fenced(owner):raise PreparedUnavailable('skill lifecycle fenced')
                privileges=auth.get_privileges(owner)
                if type(privileges) is not dict or privileges.get('can_manage_memory') is not True:raise PreparedUnavailable('skill privilege unavailable')
                policy=await asyncio.to_thread(STORE.effective_snapshot,owner,read_privilege=True,write_privilege=True,incognito=False,no_memory=False,compare_mode=False)
                if not policy.get('read_allowed'):raise PreparedUnavailable('skill policy unavailable')
                refs=await _owned(coordinator.checked_refs,owner,account,target.memory_workspace)
                mapping.validate_chat_guard(chat,guard)
                if lifecycle.owner_has_active_operation(owner) or reset_factory().owner_is_fenced(owner) or auth.account_id(owner)!=account or auth.username_for_account_id(account)!=owner:raise PreparedUnavailable('skill authority changed')
                return refs
            async def target_authority_async():return await asyncio.to_thread(target_authority.resolve,account)
            async with barrier.admitted(owner):
                async with mapping.async_chat_admission_lock(chat,blocking=False,existing_only=True) as guard:
                    refs=await current(guard);loop=asyncio.get_running_loop()
                    if not await provider._host_metadata_capability(owner=target.memory_owner,workspace_id=target.memory_workspace,deadline=loop.time()+0.5):raise PreparedUnavailable('skill metadata unavailable')
                    temporary=_SkillMetadataAuthority(_SKILL_METADATA_SEAL,owner=owner,account_id=account,actor=f'user:{owner}',current=lambda _:True,domains=lambda _,values:True)
                    async def snapshot():
                        work=asyncio.create_task(asyncio.to_thread(self._manager._prepared_metadata_proof,owner=owner,authority=temporary,expected=proof))
                        try:return await asyncio.shield(work)
                        except asyncio.CancelledError:
                            while not work.done():
                                try:await asyncio.shield(work)
                                except asyncio.CancelledError:continue
                            work.result();raise
                    first=await snapshot()
                    domains=await provider._host_metadata_read(owner=target.memory_owner,workspace_id=target.memory_workspace,account_id=account,refs=refs,deadline=loop.time()+0.5)
                    available={(v.get('block_id'),v.get('revision')) for v in domains.get('items',[]) if type(v) is dict}
                    if any((v.get('block_id'),v.get('revision')) not in available for v in first['domain_refs']):raise PreparedUnavailable('skill domain changed')
                    if await snapshot()!=first or await current(guard)!=refs:raise PreparedUnavailable('skill metadata changed')
            return {'state':'ready'}
        except Exception:return {'state':'unavailable'}

    async def knowledge(self, bridge, identity, *, limit=16, cursor=None, expected=None):
        """Read reviewed knowledge under current host/account/policy authority."""
        try:
            return await self._knowledge(bridge,identity,limit=limit,cursor=cursor,expected=expected)
        except Exception:
            return {'version':1,'state':'unavailable','reason':'reviewed_knowledge_authority_unavailable'}

    async def _knowledge(self, bridge, identity, *, limit, cursor, expected, read_ticket=None):
        from src.openclank.memory_job_admission import TargetAuthority, SingleHostProfile
        from src.openclank.session_map import OwnerSessionMap
        from src.openclank.memory_policy import STORE
        from src.openclank.memory_policy_projection import policy_coordinator
        from src.openclank.chat_source_bridge import canonical_source_provider
        state=self._state; turn=await self._actor(bridge,identity,require_skill=False)
        owner=turn.get('owner');account=turn.get('ownerAccountID');chat=turn.get('stableChatID')
        auth=getattr(state,'auth_manager',None);supervisor=getattr(state,'mimo_supervisor',None);barrier=getattr(state,'account_request_barrier',None)
        if any(type(v) is not str or not v for v in (owner,account,chat)) or auth is None or supervisor is None or barrier is None:
            raise PreparedUnavailable('prepared knowledge authority unavailable')
        worker=supervisor.worker_for_owner(owner);provider=canonical_source_provider(state);profile=getattr(state,'s05_profile',None)
        coordinator=policy_coordinator()
        if (worker is None or worker.bridge is not bridge or type(profile) is not SingleHostProfile or profile.provider is not provider
            or provider is None or coordinator is None or coordinator.projection is not getattr(state,'memory_policy_projection',None)):
            raise PreparedUnavailable('prepared knowledge profile unavailable')
        pool=supervisor.owner_lifecycle_pool();authority=TargetAuthority(auth_manager=auth,supervisor=pool)
        target=await asyncio.to_thread(authority.resolve,account)
        if target.memory_owner!=provider._scope(owner).owner or target.memory_workspace!=provider._workspace_id:
            raise PreparedUnavailable('prepared knowledge target unavailable')
        mapping=OwnerSessionMap(pool._runtime_home(owner)/'session-map.json',owner)
        lifecycle=getattr(state,'account_owner_lifecycle',None);reset_factory=getattr(state,'memory_reset_coordinator',None)
        if lifecycle is None or not callable(reset_factory):raise PreparedUnavailable('prepared knowledge lifecycle unavailable')
        async def current(guard):
            mapping.validate_chat_guard(chat,guard)
            await asyncio.to_thread(profile.assert_current)
            if (supervisor.worker_for_owner(owner) is not worker or worker.bridge is not bridge or canonical_source_provider(state) is not provider
                or auth.account_id(owner)!=account or auth.username_for_account_id(account)!=owner
                or await asyncio.to_thread(authority.resolve,account)!=target or await self._actor(bridge,identity,require_skill=False)!=turn):
                raise PreparedUnavailable('prepared knowledge authority changed')
            privileges=auth.get_privileges(owner)
            if type(privileges) is not dict or privileges.get('can_manage_memory') is not True: raise PreparedUnavailable('prepared knowledge privilege unavailable')
            policy=await asyncio.to_thread(STORE.effective_snapshot,owner,read_privilege=True,write_privilege=True,incognito=False,no_memory=False,compare_mode=False)
            if not policy.get('read_allowed'): raise PreparedUnavailable('prepared knowledge policy unavailable')
            refs=await _owned(coordinator.checked_refs,owner,account,target.memory_workspace)
            mapping.validate_chat_guard(chat,guard)
            if lifecycle.owner_has_active_operation(owner) or reset_factory().owner_is_fenced(owner) or auth.account_id(owner)!=account or auth.username_for_account_id(account)!=owner:raise PreparedUnavailable('prepared knowledge authority changed')
            return refs
        async with barrier.admitted(owner):
            async with mapping.async_chat_admission_lock(chat,blocking=False,existing_only=True) as guard:
                refs=await current(guard)
                result=await provider._host_prepared_knowledge_read(owner=target.memory_owner,workspace_id=target.memory_workspace,
                    account_id=account,policy_projection=refs,limit=limit,cursor=cursor,expected=expected,
                    deadline=asyncio.get_running_loop().time()+1.0,**({'read_ticket':read_ticket} if read_ticket is not None else {}))
                if result is None or await current(guard)!=refs: raise PreparedUnavailable('prepared knowledge changed')
                return result

    async def snapshot(self, bridge, identity, *, item, offset=0, proof=None):
        """Revalidate one current prepared snapshot and read retained bytes for this actor."""
        try:
            return await self._snapshot(bridge,identity,item=item,offset=offset,proof=proof)
        except asyncio.CancelledError:
            raise
        except Exception:
            return None

    async def _snapshot(self, bridge, identity, *, item, offset, proof):
        import base64
        from src.openclank.account_request_barrier import UtilityLifetime
        from src.openclank.analysis_snapshot_contract import decode_manifest
        from src.openclank.analysis_snapshot_host import _owned
        from src.openclank.analysis_snapshot_upload import verify_snapshot_upload
        from src.openclank.chat_source_bridge import canonical_source_provider
        from src.openclank.memory_job_admission import SingleHostProfile
        from src.openclank.memory_policy_projection import policy_coordinator
        from src.tool_utils import get_upload_handler
        if type(item) is not dict or type(offset) is not int or offset<0:
            raise PreparedUnavailable('snapshot selector unavailable')
        turn=await self._actor(bridge,identity,require_skill=False)
        state=self._state;owner=turn['owner'];account=turn['ownerAccountID'];auth=state.auth_manager
        supervisor=state.mimo_supervisor;pool=supervisor.owner_lifecycle_pool();barrier=state.account_request_barrier
        worker=pool.worker_for_owner(owner);provider=canonical_source_provider(state);profile=getattr(state,'s05_profile',None)
        lifecycle=getattr(state,'account_owner_lifecycle',None);reset_factory=getattr(state,'memory_reset_coordinator',None)
        coordinator=policy_coordinator()
        if (worker is None or getattr(worker,'_bridge',None) is not bridge or type(profile) is not SingleHostProfile
            or profile.provider is not provider or coordinator is None or coordinator.projection is not getattr(state,'memory_policy_projection',None)
            or lifecycle is None or not callable(reset_factory)):
            raise PreparedUnavailable('snapshot authority unavailable')
        process=getattr(worker,'_proc',None);generation=getattr(worker,'installed_generation',None)
        transport=getattr(provider,'_owned_transport',None);transport_generation=getattr(provider,'_owned_generation',None)
        from src.openclank.fm_owned_stdio import OwnedStdio
        if type(transport) is not OwnedStdio or not transport.current(transport_generation):raise PreparedUnavailable('snapshot transport unavailable')
        holder=getattr(bridge,'_managed_provider_generation',None);epoch=pool._owner_lifecycle_epoch(owner)
        pool._assert_owner_lifecycle(owner,epoch)
        if (getattr(getattr(worker,'_managed_callbacks',None),'holder_id',None)!=holder or getattr(worker,'_owner',None)!=owner
            or getattr(bridge,'_owner',None)!=owner):raise PreparedUnavailable('snapshot holder unavailable')
        if process is None or getattr(process,'returncode',None) is not None or generation is None or holder is None:
            raise PreparedUnavailable('snapshot worker unavailable')
        admission=await barrier.acquire(owner)
        key=hashlib.sha256(json.dumps(identity,sort_keys=True,separators=(',',':'),ensure_ascii=False).encode()).hexdigest()
        utility=UtilityLifetime(barrier,admission,owner,account,'prepared-snapshot-read',key,key,auth,
            supervisor=pool,worker=worker,bridge=bridge,holder=holder,generation=generation,epoch=epoch,process=process)
        try:
            await barrier.bind_utility(admission,utility)
            async def current():
                await _owned(profile.assert_current)
                pool._assert_owner_lifecycle(owner,epoch)
                if (not await barrier.is_live(owner,admission) or not utility.account_current()
                    or pool.worker_for_owner(owner) is not worker or getattr(worker,'_bridge',None) is not bridge
                    or getattr(worker,'_proc',None) is not process or getattr(process,'returncode',None) is not None
                    or getattr(worker,'installed_generation',None)!=generation or getattr(bridge,'_managed_provider_generation',None)!=holder
                    or getattr(getattr(worker,'_managed_callbacks',None),'holder_id',None)!=holder
                    or getattr(provider,'_owned_transport',None) is not transport or getattr(provider,'_owned_generation',None) is not transport_generation
                    or not transport.current(transport_generation) or canonical_source_provider(state) is not provider or lifecycle.owner_has_active_operation(owner)
                    or reset_factory().owner_is_fenced(owner) or await self._actor(bridge,identity,require_skill=False)!=turn):
                    raise PreparedUnavailable('snapshot authority changed')
            async def native():
                value=await self._knowledge(bridge,identity,limit=1,cursor=None,expected=item,read_ticket={})
                if type(value) is not dict or value.get('state')!='ready' or value.get('item')!=item or type(value.get('body')) is not str:
                    raise PreparedUnavailable('snapshot item changed')
                return value
            async def context():
                selected=item.get('selected');ref=selected.get('reference') if type(selected) is dict else None
                if type(ref) is not dict:raise PreparedUnavailable('snapshot item unavailable')
                result=await provider._s07_owner_rpc(ref['owner'],ref['workspace_id'],{'phase':'mirror_context','curated_id':selected['curated_id']},asyncio.get_running_loop().time()+1.0,read_ticket={})
                binding=result.get('binding') if type(result) is dict else None
                snapshot=result.get('snapshot_copy') if type(result) is dict else None
                if (type(binding) is not dict or result.get('reviewer_account_id')!=account or binding.get('owner')!=ref['owner']
                    or binding.get('workspace')!=ref['workspace_id'] or binding.get('project')!=''
                    or binding.get('curated_id')!=selected['curated_id'] or binding.get('block_id')!=ref['block_id']
                    or binding.get('revision')!=ref['revision'] or type(result.get('prior_receipt_id')) is not str
                    or type(snapshot) is not dict or set(snapshot)!={'capture_origin','manifest'}
                    or snapshot['capture_origin'] not in ('verified_capture','imported_unverified')):
                    raise PreparedUnavailable('snapshot receipt unavailable')
                return result
            await current();first=await native();receipt=await context()
            try: summary=json.loads(first['body'])
            except (ValueError,TypeError):raise PreparedUnavailable('snapshot body unavailable')
            snapshot=receipt['snapshot_copy'];reference=snapshot['manifest']
            if (type(summary) is not dict or summary.get('version')!='openclank.analysis-snapshot-memory.v1'
                or summary.get('capture_origin')!=snapshot['capture_origin'] or type(summary.get('manifest')) is not dict
                or summary['manifest'].get('reference')!=reference):raise PreparedUnavailable('snapshot body changed')
            raw=await _owned(verify_snapshot_upload,get_upload_handler(),authority=utility,reference=reference)
            manifest=await _owned(decode_manifest,raw);dossier=manifest['dossier'];result=dossier['result'];data=base64.b64decode(result['data'],validate=True)
            if (hashlib.sha256(data).hexdigest()!=result['sha256'] or len(data)!=result['size_bytes'] or len(data)>51200
                or summary.get('dossier')!={k:v for k,v in dossier.items() if k!='result'}):raise PreparedUnavailable('snapshot bytes changed')
            immutable={'item':item,'receipt':receipt['prior_receipt_id'],'manifest':reference['manifest_sha256'],
                       'sha256':result['sha256'],'sizeBytes':len(data),'captureOrigin':snapshot['capture_origin'],'operation':dossier['operation']}
            if proof is not None and proof!=immutable:raise PreparedUnavailable('snapshot selector changed')
            if offset>=len(data):raise PreparedUnavailable('snapshot offset unavailable')
            data[:offset].decode('utf-8','strict')
            end=min(offset+4096,len(data))
            while end>offset:
                try:fragment=data[offset:end].decode('utf-8','strict');break
                except UnicodeDecodeError:end-=1
            else:raise PreparedUnavailable('snapshot fragment unavailable')
            if await native()!=first or await context()!=receipt:raise PreparedUnavailable('snapshot changed')
            await current()
            return {'proof':immutable,'body':first['body'],'fragment':fragment,'startByte':offset,'endByte':end,
                    'totalBytes':len(data),'sha256':result['sha256'],'captureOrigin':snapshot['capture_origin'],
                    'operation':dossier['operation'],'complete':end==len(data)}
        finally:
            release=asyncio.create_task(barrier.release(admission))
            while not release.done():
                try:await asyncio.shield(release)
                except asyncio.CancelledError:continue
            release.result();utility.settled=True
