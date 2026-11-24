"""One explicit checkpoint-file restore using existing History and Files custody."""
from __future__ import annotations
import hashlib, json, re, secrets
from pathlib import Path
from fastapi import HTTPException
from src.openclank.history_client import HistoryClient
from src.openclank.history_io import bounded_history_io
from src.openclank.history_restore_custody import owned_host_restore

_ID = re.compile(r"[A-Za-z0-9_-]{16,80}")
_DIGEST = re.compile(r"[0-9a-f]{64}")
_FP = re.compile(r"[0-9a-f]{64}")

def _hash(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=False).encode('utf-8','strict')).hexdigest()

def _identity(source):
    return {key:source[key] for key in ('checkpoint_id','checkpoint_digest','binding','native','resource_id','native_resource_id','version_ref','fingerprint','size','path','source_action_id','source_version_id')}

async def _review(facade, context, *, checkpoint_id, resource_id, auth, barrier, supervisor, current, request_id):
    source = await facade.retained_checkpoint_restore_source(context,request_id=checkpoint_id,resource_id=resource_id,
        auth=auth,barrier=barrier,history_supervisor=supervisor,current=current)
    history=source['history']; process=source['history_process']
    preview=await bounded_history_io(lambda:history.preview_resource_version_restore(source['native_resource_id'],source['version_ref']))
    destination=preview.get('destination') or {}; fingerprint=destination.get('expected_fingerprint')
    match=_FP.fullmatch(fingerprint) if isinstance(fingerprint,str) else None
    if (not match or destination.get('exists') is not True or preview.get('effect')!='replace'
            or preview.get('captures_current_destination') is not True or source['file'].size is None
            or type(source['file'].size) is not int or source['file'].size < 0
            or supervisor.process is not process or process.poll() is not None or await current() is not True):
        raise ValueError('checkpoint restore unavailable')
    fresh=await facade.retained_checkpoint_restore_source(context,request_id=checkpoint_id,resource_id=resource_id,
        auth=auth,barrier=barrier,history_supervisor=supervisor,current=current)
    if (_identity(fresh)!=_identity(source) or fresh['history_process'] is not process
            or fresh['file'].size != source['file'].size
            or supervisor.process is not process or process.poll() is not None or await current() is not True):
        raise ValueError('checkpoint restore source changed')
    identity=_identity(source)
    material={'version':1,'kind':'selected_checkpoint_restore_v1','account':context.owner_subject_id,
        'owner':context.owner_username,'request_id':request_id,'source':identity,'destination_fingerprint':fingerprint,
        'destination_size':source['file'].size,'policy_generation':context.policy_generation,'history_process':id(process)}
    digest=_hash(material)
    return source, fingerprint, digest, {'name':source['file'].name,'source_fingerprint':source['fingerprint'],
        'source_size':source['size'],'destination_fingerprint':fingerprint,'destination_size':source['file'].size,
        'effect':'replace','captures_current_destination':True,'consistency':'one_file'}

async def preview(facade, context, *, checkpoint_id, resource_id, auth, barrier, supervisor, current):
    request_id=secrets.token_urlsafe(24)
    _,_,digest,review=await _review(facade,context,checkpoint_id=checkpoint_id,resource_id=resource_id,
        auth=auth,barrier=barrier,supervisor=supervisor,current=current,request_id=request_id)
    return {'version':1,'state':'ready','checkpoint_id':checkpoint_id,'resource_id':resource_id,
        'request_id':request_id,'expected_digest':digest,'review':review}

async def status(repository, *, owner, account, auth, barrier, supervisor, request_id, expected_digest, original_current):
    if not _ID.fullmatch(request_id or '') or not _DIGEST.fullmatch(expected_digest or ''):
        raise ValueError('checkpoint restore unavailable')
    async with barrier.admitted(owner):
        if not callable(original_current) or original_current() is not True or auth.account_id(owner)!=account or auth.username_for_account_id(account)!=owner:
            raise ValueError('checkpoint restore unavailable')
        saved=repository.get_operation(owner_subject_id=account,operation_id=request_id)
        row=saved.get('receipt') if isinstance(saved,dict) else None
        if not saved or saved.get('digest')!=expected_digest or not isinstance(row,dict) or row.get('kind')!='selected_checkpoint_restore_v1':
            return {'version':1,'state':'unknown','request_id':request_id,'expected_digest':expected_digest}
        state=row.get('state')
        if state in {'complete','conflict','partial','refused'}:
            return {'version':1,'state':state,'request_id':request_id,'expected_digest':expected_digest,'resource_id':row['resource_id'],'verified':bool(row.get('verified',False))}
        try:
            process=supervisor.process; actor=row['history_actor']
            if process is None or process.poll() is not None or str(supervisor.socket_path)!=row['history_socket']:raise ValueError()
            history=HistoryClient(row['history_socket'],actor_id=actor,account_id=account,token=supervisor.credential_token(actor,account),timeout=1.5)
            observed=await bounded_history_io(lambda:history.get_restore_status(row['restore_id'],row['native_resource_id'],row['source_action_id'],row['source_version_id'],row['destination_fingerprint']))
            if (original_current() is not True or supervisor.process is not process or process.poll() is not None or auth.account_id(owner)!=account or auth.username_for_account_id(account)!=owner):raise ValueError()
            result=observed['state']
            if result in {'complete','conflict','partial'} and isinstance(row.get('claim'),dict):
                repository.settle_checkpoint_restore(owner_subject_id=account,operation_id=request_id,request_digest=expected_digest,
                    claim=row['claim'],state=result,verified=observed['verified'])
                return {'version':1,'state':result,'request_id':request_id,'expected_digest':expected_digest,'resource_id':row['resource_id'],'verified':observed['verified']}
            return {'version':1,'state':result if result in {'pending','unknown'} else 'unknown','request_id':request_id,'expected_digest':expected_digest}
        except Exception:
            return {'version':1,'state':'unknown','request_id':request_id,'expected_digest':expected_digest}

async def confirm(facade, context, *, checkpoint_id, resource_id, request_id, expected_digest, auth, barrier, supervisor, current, original_current):
    if not _ID.fullmatch(request_id or '') or not _DIGEST.fullmatch(expected_digest or ''):
        raise ValueError('checkpoint restore unavailable')
    store=facade._operation_store; owner=context.owner_username; account=context.owner_subject_id
    existing=store.get_operation(owner_subject_id=account,operation_id=request_id)
    if existing is not None:
        existing_row=existing.get('receipt')
        if (existing['digest']!=expected_digest or not isinstance(existing_row,dict)
                or existing_row.get('checkpoint_id')!=checkpoint_id or existing_row.get('resource_id')!=resource_id):
            raise ValueError('checkpoint restore conflict')
        return await status(store,owner=owner,account=account,auth=auth,barrier=barrier,supervisor=supervisor,request_id=request_id,expected_digest=expected_digest,original_current=original_current)
    try:
        source, fingerprint, digest, _=await _review(facade,context,checkpoint_id=checkpoint_id,resource_id=resource_id,
            auth=auth,barrier=barrier,supervisor=supervisor,current=current,request_id=request_id)
        if digest!=expected_digest:
            raise ValueError('checkpoint restore review changed')
    except Exception:
        # This branch precedes any reservation, claim or native restore. Spend
        # only this exact request ID while the original account is current.
        try:
            async with barrier.admitted(owner):
                if (original_current() is not True or await current() is not True
                        or auth.account_id(owner)!=account or auth.username_for_account_id(account)!=owner):
                    raise ValueError('account authority changed')
                refused={'kind':'selected_checkpoint_restore_v1','state':'refused','claim':None,
                    'checkpoint_id':checkpoint_id,'resource_id':resource_id,'verified':False}
                previous=store.reserve_operation(owner_subject_id=account,operation_id=request_id,
                    request_digest=expected_digest,generation=context.policy_generation,
                    receipt=refused,phase='selected_checkpoint_restore_v1')
                if previous is not None:
                    old=previous.get('receipt')
                    if (not isinstance(old,dict) or old.get('kind')!='selected_checkpoint_restore_v1'
                            or old.get('checkpoint_id')!=checkpoint_id or old.get('resource_id')!=resource_id):
                        raise HTTPException(409,'Checkpoint restore request conflicts with its original identity')
                if (original_current() is not True or await current() is not True
                        or auth.account_id(owner)!=account or auth.username_for_account_id(account)!=owner):
                    raise ValueError('account authority changed')
        except HTTPException:
            raise
        except Exception as exc:
            if getattr(exc,'code',None)=='idempotency_conflict':
                raise HTTPException(409,'Checkpoint restore request conflicts with its original identity') from exc
            return {'version':1,'state':'unknown','request_id':request_id,'expected_digest':expected_digest}
        return await status(store,owner=owner,account=account,auth=auth,barrier=barrier,supervisor=supervisor,
            request_id=request_id,expected_digest=expected_digest,original_current=original_current)
    restore_id='checkpoint-restore-'+_hash(['openclank.checkpoint-restore.v1',account,request_id])
    row={'kind':'selected_checkpoint_restore_v1','state':'pending','claim':None,'resource_id':resource_id,
        'checkpoint_id':checkpoint_id,'checkpoint_digest':source['checkpoint_digest'],'binding':source['binding'],
        'native_resource_id':source['native_resource_id'],'version_ref':source['version_ref'],
        'source_action_id':source['source_action_id'],'source_version_id':source['source_version_id'],
        'destination_fingerprint':fingerprint,'restore_id':restore_id,'history_actor':source['binding']['history_actor'],
        'history_socket':str(source['history'].socket_path)}
    previous=store.reserve_operation(owner_subject_id=account,operation_id=request_id,request_digest=digest,
        generation=context.policy_generation,receipt=row,phase='selected_checkpoint_restore_v1')
    if previous is not None:
        return await status(store,owner=owner,account=account,auth=auth,barrier=barrier,supervisor=supervisor,request_id=request_id,expected_digest=expected_digest,original_current=original_current)
    provider=facade._providers['host']; identity=_identity(source); process=source['history_process']
    async def revalidate():
        fresh=await facade.retained_checkpoint_restore_source(context,request_id=checkpoint_id,resource_id=resource_id,
            auth=auth,barrier=barrier,history_supervisor=supervisor,current=current)
        return _identity(fresh)==identity and fresh['history_process'] is process
    def dispatch_current():
        return (original_current() is True and supervisor.process is process and process.poll() is None and auth.account_id(owner)==account
            and auth.username_for_account_id(account)==owner and context.is_admin is False
            and store.generation()==context.policy_generation)
    def claim_factory(workspace):
        return store.claim_checkpoint_restore(owner_subject_id=account,operation_id=request_id,request_digest=digest,
            generation=context.policy_generation,workspace_id=workspace,path=source['path'])
    def refuse_unclaimed():
        store.refuse_unclaimed_checkpoint_restore(owner_subject_id=account,operation_id=request_id,request_digest=digest)
    def settle(claim,receipt):
        outcome=receipt.get('outcome') if receipt else None
        state={'Complete':'complete','Conflict':'conflict','Partial':'partial'}.get(outcome) if receipt else 'refused'
        if state is None:
            return  # The original native error is not proof of no issue.
        proof=receipt.get('verification') if receipt else None
        verified=bool(isinstance(proof,dict) and proof.get('status')=='Verified' and proof.get('content_hash')==proof.get('restored_content_hash') and proof.get('content_hash'))
        store.settle_checkpoint_restore(owner_subject_id=account,operation_id=request_id,request_digest=digest,claim=claim,state=state,verified=verified)
    try:
        await owned_host_restore(owner=owner,account_id=account,auth=auth,barrier=barrier,client=source['history'],
            requested_id=resource_id,private_id=source['native_resource_id'],path=source['path'],restore_id=restore_id,
            action_id=source['source_action_id'],version_id=source['source_version_id'],version_ref=source['version_ref'],
            call=lambda:source['history'].restore_resource_version(restore_id,source['native_resource_id'],source['version_ref'],expected_destination_fingerprint=fingerprint),
            provider=provider,repository=store,context=context,generation=context.policy_generation,revalidate_source=revalidate,
            original_current=dispatch_current,claim_factory=claim_factory,settle_claim=settle,refuse_unclaimed=refuse_unclaimed)
    except Exception:
        pass
    return await status(store,owner=owner,account=account,auth=auth,barrier=barrier,supervisor=supervisor,request_id=request_id,expected_digest=expected_digest,original_current=original_current)
