"""Named-human host admission for approved snapshot publication and reads."""
from __future__ import annotations

import asyncio
import hashlib
import re

from src.openclank.account_request_barrier import UtilityLifetime
from src.openclank.analysis_snapshot_capture import open_snapshot_preview
from src.openclank.analysis_snapshot_contract import decode_manifest
from src.openclank.analysis_snapshot_publication import publication_request, saved_analysis
from src.openclank.analysis_snapshot_upload import (SnapshotPublicationIdentity, claim_snapshot_upload,
    snapshot_pending_scope, verify_snapshot_upload)


def snapshot_named_human(request):
    """Apply the same Memory-management and named-human route admission."""
    from src.auth_helpers import require_privilege, require_user, is_internal_tool_identity
    require_privilege(request,'can_manage_memory')
    user=require_user(request)
    if (is_internal_tool_identity(user) or getattr(request.state,'api_token',None)
        or getattr(request.state,'internal_tool_authenticated',False)):
        raise ValueError('named human required')
    return user


async def _owned(fn, *args, **kwargs):
    """Do not release account admission while an owned worker thread is running."""
    task = asyncio.create_task(asyncio.to_thread(fn, *args, **kwargs))
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
        except Exception:
            break
    result = task.result()
    if cancelled:
        raise asyncio.CancelledError
    return result


def _selector(value):
    if (type(value) is not dict or set(value) != {'request_key','request_sha256'}
        or type(value['request_key']) is not str or re.fullmatch(r'ma4_[0-9a-f]{64}',value['request_key']) is None
        or type(value['request_sha256']) is not str or re.fullmatch(r'[0-9a-f]{64}',value['request_sha256']) is None):
        raise ValueError('snapshot selector unavailable')
    return value


async def snapshot_action(*, request, provider, batch_store, user, body, memory_id=None, human_current=None, original_account=None, read_body=None, on_verified=None, verified_capture_only=False):
    """Run one bounded explicit action; browser selectors are never authority."""
    from src.memory_scope import memory_owner, chat_workspace
    from src.openclank.memory_job_admission import SingleHostProfile, TargetAuthority
    from src.openclank.memory_policy import STORE, explicit_human_review_allowed
    from src.openclank.memory_policy_projection import policy_coordinator
    from src.openclank.chat_source_bridge import canonical_source_provider
    from src.tool_utils import get_upload_handler

    state=request.app.state; auth=getattr(state,'auth_manager',None)
    account=auth.account_id(user) if auth else None
    barrier=getattr(state,'account_request_barrier',None); supervisor=getattr(state,'mimo_supervisor',None)
    profile=getattr(state,'s05_profile',None); coordinator=policy_coordinator()
    reset_factory=getattr(state,'memory_reset_coordinator',None)
    owner=memory_owner(user); workspace=chat_workspace()
    if (not account or account!=original_account or auth.username_for_account_id(account)!=user or not barrier or not supervisor
        or type(profile) is not SingleHostProfile or profile.provider is not provider
        or canonical_source_provider(state) is not provider or coordinator is None
        or coordinator.projection is not getattr(state,'memory_policy_projection',None) or not callable(reset_factory)):
        raise ValueError('snapshot authority unavailable')
    if getattr(request.state,'api_token',None) or getattr(request.state,'internal_tool_authenticated',False):
        raise ValueError('named human required')
    admission=await barrier.acquire(user)
    utility=UtilityLifetime(barrier,admission,user,account,'analysis-snapshot-publication','read','0'*64,auth)
    try:
        await barrier.bind_utility(admission,utility)
        if memory_id is None:
            if not callable(read_body): raise ValueError('snapshot request unavailable')
            body=await read_body()
        phase='read' if memory_id is not None else body.get('phase') if type(body) is dict else None
        if phase not in ('prepare','issue','status','read'): raise ValueError('snapshot action unavailable')
        authority=TargetAuthority(auth_manager=auth,supervisor=supervisor.owner_lifecycle_pool())
        target=await _owned(authority.resolve,account)
        async def current(*, write=False):
            await _owned(profile.assert_current)
            if (not await barrier.is_live(user,admission) or not utility.account_current() or not callable(human_current) or human_current()!=user
                or getattr(request.state,'api_token',None) or getattr(request.state,'internal_tool_authenticated',False)
                or state.account_owner_lifecycle.owner_has_active_operation(user) or reset_factory().owner_is_fenced(user)
                or canonical_source_provider(state) is not provider or await _owned(authority.resolve,account)!=target
                or target.memory_owner!=owner or target.memory_workspace!=workspace):
                raise ValueError('snapshot authority changed')
            policy=await _owned(STORE.effective_snapshot,user,read_privilege=True,write_privilege=True,
                                incognito=False,no_memory=False,compare_mode=False)
            if not policy.get('read_allowed',policy.get('direct_write_allowed',False)) or (write and not explicit_human_review_allowed(policy)):
                raise ValueError('snapshot policy unavailable')
            return await _owned(coordinator.checked_refs,user,account,workspace)
        refs=await current(write=phase in ('prepare','issue'))
        deadline=lambda: asyncio.get_running_loop().time()+1.5
        if not await provider._host_analysis_snapshot_capability(owner=owner,workspace_id=workspace,deadline=deadline()):
            raise ValueError('snapshot capability unavailable')
        handler=get_upload_handler()
        if phase=='read':
            if type(memory_id) is not str or not memory_id or len(memory_id)>512:
                raise ValueError('snapshot unavailable')
            context=await provider._s07_owner_rpc(owner,workspace,{'phase':'mirror_context','curated_id':memory_id},deadline())
            binding=context.get('binding') if type(context) is dict else None
            snapshot=context.get('snapshot_copy') if type(context) is dict else None
            if (type(binding) is not dict or context.get('reviewer_account_id')!=account or binding.get('owner')!=owner
                or binding.get('workspace')!=workspace or binding.get('curated_id')!=memory_id or binding.get('project')!=''
                or type(snapshot) is not dict or set(snapshot)!={'capture_origin','manifest'}
                or snapshot['capture_origin'] not in ('verified_capture','imported_unverified')):
                raise ValueError('snapshot unavailable')
            if verified_capture_only and snapshot['capture_origin'] != 'verified_capture':
                raise ValueError('verified capture required')
            raw=await _owned(verify_snapshot_upload,handler,authority=utility,reference=snapshot['manifest'])
            if await current()!=refs: raise ValueError('snapshot authority changed')
            if on_verified is not None:
                if not callable(on_verified): raise ValueError('snapshot read unavailable')
                result=await on_verified(utility,snapshot['manifest'],decode_manifest(raw),current)
                latest = await provider._s07_owner_rpc(owner, workspace,
                    {'phase': 'mirror_context', 'curated_id': memory_id}, deadline())
                if (latest != context or await current() != refs):
                    raise ValueError('snapshot authority changed')
                return result
            result=await _owned(saved_analysis,reference=snapshot['manifest'],manifest_bytes=raw)
            if await current()!=refs: raise ValueError('snapshot authority changed')
            response = {'state':'ready','capture_origin':snapshot['capture_origin'],**result}
            if snapshot['capture_origin'] == 'verified_capture':
                response['components'] = [
                    {key: node[key] for key in ('resource_id', 'path', 'size_bytes', 'sha256')}
                    for node in decode_manifest(raw)['components'] if node['kind'] == 'file'
                ]
            return response
        selection = body.pop('component_folder', None) if phase in ('issue', 'status') else None
        imported=None
        if phase=='status':
            if set(body) not in ({'phase','selector'},{'phase','selector','imported'}): raise ValueError('invalid snapshot status')
            selector=_selector(body['selector']); imported=body.get('imported')
        else:
            expected={'phase','preview_token','description'} if 'preview_token' in body else {'phase','imported'}
            if phase=='issue': expected=expected|{'selector'}
            if set(body)!=expected: raise ValueError('invalid snapshot request')
            if 'preview_token' in body:
                sealed=open_snapshot_preview(utility,body['preview_token']); reference=sealed['reference']
                raw=await _owned(verify_snapshot_upload,handler,authority=utility,reference=reference)
                manifest=decode_manifest(raw)
                if (manifest['account_id']!=account or manifest['workspace_id']!=sealed['workspace']
                    or manifest['project_key']!=sealed['project'] or manifest['root_identity']!=sealed['root']
                    or manifest['capture_interval']!=sealed['interval']): raise ValueError('snapshot stage changed')
                origin='verified_capture'; description=body['description']
            else:
                imported=body['imported']; origin='imported_unverified'
                if (type(imported) is not dict or set(imported)!={'batch_id','item_id','suggestion_id'}
                    or any(type(v) is not str or not v or len(v)>512 for v in imported.values())): raise ValueError('snapshot review unavailable')
                card=await _owned(batch_store.read_imported_snapshot,owner=user,**imported)
                descriptor=card['descriptor']; reference=descriptor['reference']; description=card['description']
                raw=await _owned(verify_snapshot_upload,handler,authority=utility,reference=reference)
            native,request_hash=publication_request(account=account,owner=owner,workspace=workspace,origin=origin,
                reference=reference,manifest_bytes=raw,description=description,policy=refs)
            if native['version']==2:
                if not await provider._host_analysis_snapshot_capability(owner=owner,workspace_id=workspace,deadline=deadline(),request_version=2):
                    raise ValueError('snapshot capability unavailable')
                if await current(write=True)!=refs: raise ValueError('snapshot authority changed')
            selector={'request_key':native['request_key'],'request_sha256':request_hash}
            if phase=='prepare':
                if await current(write=True)!=refs:raise ValueError('snapshot authority changed')
                return {'state':'prepared','selector':selector,'capture_origin':origin,'description':description}
            if _selector(body['selector'])!=selector: raise ValueError('snapshot request changed')
        if imported is not None and (type(imported) is not dict or set(imported)!={'batch_id','item_id','suggestion_id'}
            or any(type(v) is not str or not v or len(v)>512 for v in imported.values())):raise ValueError('snapshot review unavailable')
        identity=SnapshotPublicationIdentity(account,selector['request_key'],selector['request_sha256'])
        if phase=='issue':
            claim=None; write_status={}
            try:
                claim=await _owned(claim_snapshot_upload,handler,authority=utility,reference=reference,identity=identity)
                if getattr(claim,'newly_created',False) is not True: raise ValueError('snapshot status only')
                if imported:
                    fence=await _owned(batch_store.prepare_imported_snapshot_review,owner=user,**imported,review_key=selector['request_key'])
                    if fence.get('replayed') is not False:raise ValueError('snapshot status only')
                    card_after=await _owned(batch_store.read_imported_snapshot,owner=user,**imported)
                    if card_after!=card:raise ValueError('snapshot review changed')
                if await current(write=True)!=refs:raise ValueError('snapshot authority changed')
                receipt=await provider._host_analysis_snapshot_publish(owner=owner,workspace_id=workspace,request=native,deadline=deadline(),write_status=write_status)
            except BaseException:
                ticket=write_status.get('ticket')
                if claim is not None and getattr(claim,'newly_created',False) is True and (ticket is None or ticket.get('issued') is False):
                    await _owned(handler.settle_reference_claim,claim.upload_id,claim.pending_scope,committed=False)
                raise
        else:
            receipt=await provider._host_analysis_snapshot_status(owner=owner,workspace_id=workspace,
                request={'version':1,'owner':owner,'workspace_id':workspace,'account_id':account,**selector,'current_policy':refs},deadline=deadline())
            if receipt=={'state':'unknown'}:return receipt
        if await current()!=refs:raise ValueError('snapshot authority changed')
        raw = await _owned(verify_snapshot_upload,handler,authority=utility,reference=receipt['manifest'])
        if imported:
            card=await _owned(batch_store.read_imported_snapshot,owner=user,**imported)
            if receipt['capture_origin']!='imported_unverified' or card['descriptor']['reference']!=receipt['manifest']:
                raise ValueError('snapshot review changed')
            await _owned(batch_store.finalize_imported_snapshot_review,owner=user,**imported,review_key=selector['request_key'],publication_id=receipt['curated_id'],publication_kind='memory')
        scope=snapshot_pending_scope(authority=utility,identity=identity)
        await _owned(handler.settle_reference_claim,receipt['manifest']['upload_id'],scope,committed=True,settled_scope='domain:memery')
        if await current()!=refs:raise ValueError('snapshot authority changed')
        result = {'state':'applied','curated_id':receipt['curated_id'],'capture_origin':receipt['capture_origin']}
        if receipt['capture_origin'] == 'verified_capture':
            result['component_history'] = {'state': 'incomplete'}
            if selection is not None:
                try:
                    result['component_history'] = await state.retain_published_analysis(
                        request, user, account, provider, receipt['curated_id'], selection, decode_manifest(raw), current)
                except Exception:
                    pass  # The publication receipt survives for status-only recovery with a current folder.
            if await current() != refs:
                raise ValueError('snapshot authority changed')
        return result
    finally:
        release=asyncio.create_task(barrier.release(admission))
        while not release.done():
            try: await asyncio.shield(release)
            except asyncio.CancelledError: continue
        release.result()
        utility.settled=True


async def with_saved_snapshot(*, request, provider, batch_store, user, original_account, memory_id, human_current, callback, verified_capture_only=False):
    """Hold current Memory/account authority while a trusted host callback reads a verified snapshot."""
    return await snapshot_action(request=request,provider=provider,batch_store=batch_store,user=user,body=None,
        memory_id=memory_id,human_current=human_current,original_account=original_account,on_verified=callback,verified_capture_only=verified_capture_only)
