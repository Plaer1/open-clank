"""Private staging owner for one reviewed, already authorized Git utility.

This is not a route, a worktree creation API, or a publication authority. The
caller supplies the original human account admission, bound worker/lease, root
and fresh authorization predicate. An uncertain issued Source call is drained
under that same utility; its reservation is never retried or adopted.
"""
from __future__ import annotations
import asyncio
import hashlib
import json
import secrets
import time
import os
import re
from src.openclank.account_request_barrier import UtilityLifetime
from src.openclank.files_service_client import _WorktreeStageCustody
from src.openclank.git_checkout_authority import issue_guard
from src.openclank.git_worktree_stage_authority import attach_original, issue_current
from src.openclank.git_worktree_stage_phase import WorktreeStagePhase, _inventory, _encoded
from src.secret_storage import decrypt, encrypt
from src.openclank.git_worktree_receipts import record_cleanup


def reviewed_digest(*, account, location_id, workspace_id, chat_id, operation_id, native_id, source_path, root_object_tag, preview, destination=None):
    begin={'kind':'begin','targetOID':preview['targetOID'],'treeOID':preview['treeOID'],
        'entries':preview['entries'],'directories':preview['directories'],'totalBytes':preview['totalBytes']}
    plan=_inventory(begin)
    value=['openclank.git-worktree.review.v1',account,location_id,workspace_id,chat_id,operation_id,native_id,source_path,root_object_tag,plan]
    if destination is not None:
        if (type(destination) is not dict or set(destination)!={'parent','objectTag','name'}
                or type(destination['parent']) is not str or not destination['parent'].startswith('/') or len(destination['parent'].encode('utf-8','strict'))>4096
                or type(destination['objectTag']) is not str or not re.fullmatch(r'[0-9a-f]{64}',destination['objectTag'])
                or type(destination['name']) is not str or not destination['name'] or len(destination['name'].encode('utf-8','strict'))>255
                or destination['name'] in ('.','..','.git','.clanker','.clankers','.references') or any(ord(ch)<32 or ord(ch)==127 or ch in '/\\' for ch in destination['name'])):
            raise ValueError('worktree destination unavailable')
        value=['openclank.git-worktree.review-destination.v2',account,location_id,workspace_id,chat_id,operation_id,native_id,source_path,root_object_tag,plan,destination['parent'],destination['objectTag'],destination['name']]
    return hashlib.sha256(_encoded(value)).hexdigest(),plan,begin


def _reserve(repository, *, account, location_id, workspace_id, chat_id, operation_id, digest, generation, expires):
    pending={'version':1,'state':'pending','operation':'git_worktree_stage','intentDigest':digest,
        'chatID':chat_id,'locationID':location_id,'workspaceID':workspace_id,
        'policyTransition':{'fromGeneration':generation,'toGeneration':generation}}
    old=repository.reserve_operation(owner_subject_id=account,operation_id=operation_id,request_digest=digest,
        generation=generation,receipt=pending,phase='git_worktree_stage_pending')
    if old is not None:return None
    once=repository.create_git_worktree_stage_once(subject_id=account,location_id=location_id,workspace_id=workspace_id,chat_id=chat_id,
        operation_id=operation_id,request_digest=digest,expected_generation=generation,expires_unix_ms=expires)
    if once['generation']!=generation+1:raise ValueError('worktree stage policy changed')
    pending['policyTransition']['toGeneration']=once['generation']
    repository.record_operation(owner_subject_id=account,operation_id=operation_id,request_digest=digest,generation=generation,receipt=pending,phase='git_worktree_stage_pending')
    consumed=repository.consume_git_worktree_stage_once(subject_id=account,location_id=location_id,workspace_id=workspace_id,chat_id=chat_id,
        operation_id=operation_id,request_digest=digest,binding_id=once['binding_id'],expected_generation=once['generation'])
    if consumed['generation']!=generation+2:raise ValueError('worktree stage policy changed')
    pending['policyTransition']['toGeneration']=consumed['generation']
    repository.record_operation(owner_subject_id=account,operation_id=operation_id,request_digest=digest,generation=generation,receipt=pending,phase='git_worktree_stage_pending')
    return once['binding_id'],consumed['generation']


async def stage_reviewed(*, utility, repository, client, location_id, workspace_id, chat_id, native_id,
                         source_path, root_path, root_object_tag, preview, expected_digest, current, destination=None, product_input_sha=None):
    """Use one original admitted utility and Once receipt; never reissue an existing ID.

    `current` must revalidate the original human credential, registered resource,
    session binding and account barrier at the supplied policy generation. The caller owns their original lease and
    must keep it while this coroutine runs. No public endpoint invokes this owner.
    """
    if type(utility) is not UtilityLifetime or not callable(current) or utility.issued or utility.settled:
        raise ValueError('worktree stage admission unavailable')
    loop=asyncio.get_running_loop();deadline=loop.time()+300.0
    account=utility.account;operation_id=utility.idempotency_key
    digest,plan,begin=reviewed_digest(account=account,location_id=location_id,workspace_id=workspace_id,chat_id=chat_id,
        operation_id=operation_id,native_id=native_id,source_path=source_path,root_object_tag=root_object_tag,preview=preview,destination=destination)
    generation=repository.generation()
    if digest!=expected_digest or utility.request_hash!=digest or utility.root!='git-worktree-stage' or await current(generation) is not True:
        raise ValueError('worktree stage review changed')
    admission=_reserve(repository,account=account,location_id=location_id,workspace_id=workspace_id,chat_id=chat_id,
        operation_id=operation_id,digest=digest,generation=generation,expires=int(time.time()*1000)+299000)
    if admission is None:return {'state':'status_only'}
    binding_id,active_generation=admission
    protected_pending=None
    if destination is not None:
        row=repository.get_operation(owner_subject_id=account,operation_id=operation_id)
        if type(row) is not dict or row.get('digest')!=digest or type(row.get('receipt')) is not dict:raise ValueError('worktree receipt unavailable')
        protected_pending=dict(row['receipt']);protected_pending.update({'humanActor':utility.owner,'targetOID':preview['targetOID'],'sourcePathSHA256':hashlib.sha256(source_path.encode('utf-8','strict')).hexdigest(),'destination':destination})
        if type(product_input_sha) is not str or not re.fullmatch(r'[0-9a-f]{64}',product_input_sha):raise ValueError('worktree selector unavailable')
        protected_pending['productInputSHA256']=product_input_sha
        repository.record_operation(owner_subject_id=account,operation_id=operation_id,request_digest=digest,generation=generation,receipt=protected_pending,phase='git_worktree_stage_pending')
    def policy_current():
        try:
            row=repository.get_binding(binding_id)
            return bool(repository.generation()==active_generation and row.status=='consumed' and row.remaining_uses==0
                and row.generation==active_generation and row.revision==2 and row.subject_id==account
                and row.location_id==location_id and row.workspace_id==workspace_id and row.chat_id==chat_id
                and row.resource_ref=='git-worktree-stage-action:sha256:'+digest and row.operation=='git_worktree_stage')
        except Exception:return False
    async def fresh():return await current(active_generation) is True and policy_current()
    if not await fresh():raise ValueError('worktree stage authority changed')
    capability=secrets.token_hex(32)
    action=hashlib.sha256(_encoded(['openclank.git-worktree.native-action.v1',native_id,chat_id,operation_id,digest,plan])).hexdigest()
    identity={'operationID':operation_id,'requestDigest':digest,'capability':capability,'actionSHA256':action,'gitPlanDigest':plan}
    native={'operation_id':operation_id,'request_digest':digest,'git_plan_digest':plan,'root_id':location_id,'root_path':root_path,'root_object_tag':root_object_tag}
    custody=await _WorktreeStageCustody.acquire(client(active_generation) if callable(client) else client,root_path,operation_id,before_issue=issue_guard(fresh,deadline=deadline))
    phase=WorktreeStagePhase(identity=identity,expected_begin=begin,native_binding=native,custody=custody,verify=fresh)
    context=attach_original(utility,phase,fresh,deadline)
    context.source_binding=(chat_id,native_id,source_path,hashlib.sha256(_encoded(preview)).hexdigest())
    context.reviewed_destination=json.loads(_encoded(destination)) if destination is not None else None
    context.stage_repository=repository;context.stage_generation=generation;context.stage_pending=protected_pending
    custody.before_issue=issue_guard(lambda:issue_current(utility,context,policy_current),deadline=deadline)
    remaining=max(1,int((deadline-loop.time())*1000))
    if remaining<=1 or not await fresh():raise ValueError('worktree stage authority changed')
    payload={'version':1,'stableChatID':chat_id,'operationID':operation_id,'requestDigest':digest,'capability':capability,
        'gitPlanDigest':plan,'targetOID':preview['targetOID'],'expectedPreview':preview,'timeoutMs':remaining}
    utility.issued=True
    task=asyncio.create_task(utility.worker.session_git_worktree_stage_request(chat_id,native_id,source_path,payload,
        owner=utility.owner,timeout=max(0.001,deadline-loop.time())))
    utility.git_worktree_stage_http_task=task
    cancelled=False
    while not task.done():
        try:await asyncio.shield(task)
        except asyncio.CancelledError:cancelled=True;utility.cancel_requested=True
        except Exception:break
    try:
        response=task.result();result=response.json() if response.status_code==200 else {'state':'unknown'}
    except asyncio.CancelledError:
        cancelled=True;utility.cancel_requested=True;result={'state':'unknown'}
    except Exception:result={'state':'unknown'}
    try:
        if not cancelled and type(result) is dict and result.get('state')=='sealed' and phase.state=='sealed' and result.get('targetManifestSHA256')==phase.target and await issue_current(utility,context,policy_current) and await fresh():
            return {'state':'sealed','targetManifestSHA256':phase.target}
    except asyncio.CancelledError:
        utility.cancel_requested=True
    phase.state='unknown';utility.host_cleanup_unknown=True
    # A lost HTTP result does not prove the Source callback or child drained.
    # Keep the original utility/lease until its original worker positively exits.
    child=asyncio.create_task(utility.process.wait())
    while not child.done():
        try:await asyncio.shield(child)
        except asyncio.CancelledError:utility.cancel_requested=True
    if child.cancelled() or type(child.result()) is not int or utility.process.returncode!=child.result():
        raise RuntimeError('original worktree stage custody unavailable')
    return {'state':'unknown'}


async def register_staged(*, utility, chat_id, native_id, source_path, preview, destination_parent, destination_parent_object_tag, destination_name):
    """Continue only the sealed original utility into one private Git registration."""
    context=getattr(utility,'git_worktree_stage_context',None)
    if type(utility) is not UtilityLifetime or context is None or context.utility is not utility or context.phase.state!='sealed':
        raise ValueError('worktree stage unavailable')
    phase=context.phase;loop=asyncio.get_running_loop();deadline=context.deadline
    if getattr(context,'source_binding',None)!=(chat_id,native_id,source_path,hashlib.sha256(_encoded(preview)).hexdigest()):
        raise ValueError('worktree source binding changed')
    if (type(destination_parent) is not str or not destination_parent.startswith('/') or len(destination_parent.encode('utf-8','strict'))>4096
        or type(destination_parent_object_tag) is not str or not re.fullmatch(r'[0-9a-f]{64}',destination_parent_object_tag)
        or type(destination_name) is not str or not destination_name or len(destination_name.encode('utf-8','strict'))>255
        or destination_name in ('.','..','.git','.clanker','.clankers','.references') or '/' in destination_name or '\\' in destination_name or any(ord(ch)<32 or ord(ch)==127 for ch in destination_name)
        or await issue_current(utility,context,lambda:True) is not True or await context.recheck() is not True):
        raise ValueError('worktree registration unavailable')
    if context.reviewed_destination is not None and context.reviewed_destination!={'parent':destination_parent,'objectTag':destination_parent_object_tag,'name':destination_name}:
        raise ValueError('worktree destination review changed')
    base={k:v for k,v in phase.native.items() if k!='root_path'}
    try:
        response=await phase.custody.exchange('worktree_private_prepare',{**base,'destination_parent_object_tag':destination_parent_object_tag,'destination_name':destination_name},target=destination_parent)
        value=response.get('data') if type(response) is dict else None
        if type(value) is not dict or set(value)!={'state','private_path','journal_directory','private_identity'} or value['state']!='ready':raise ValueError('private stage unknown')
        private_path=value['private_path'];journal=value['journal_directory'];identity=value['private_identity']
        if (type(private_path) is not str or type(journal) is not str or not journal.startswith('/') or private_path!=os.path.join(journal,utility.idempotency_key+'.worktree-private')
                or len(private_path.encode('utf-8','strict'))>4096 or len(journal.encode('utf-8','strict'))>4096):raise ValueError('private stage changed')
        destination_digest=hashlib.sha256(_encoded(['openclank.git-worktree.destination.v1',destination_parent,destination_parent_object_tag,destination_name])).hexdigest()
        action=hashlib.sha256(_encoded(['openclank.git-worktree.registration.v1',native_id,chat_id,utility.idempotency_key,utility.request_hash,
            phase.identity['gitPlanDigest'],private_path,identity,source_path,preview['targetOID'],destination_digest])).hexdigest()
        phase.arm_registration(action_sha256=action,private_identity=identity,private_path=private_path,source_path=source_path)
        phase.registration['destinationPath']=os.path.join(destination_parent,destination_name)
        remaining=max(1,int((deadline-loop.time())*1000))
        if remaining<=1 or await issue_current(utility,context,lambda:True) is not True or await context.recheck() is not True:raise ValueError('worktree registration expired')
        payload={'version':1,'stableChatID':chat_id,'operationID':utility.idempotency_key,'requestDigest':utility.request_hash,
            'capability':phase.identity['capability'],'gitPlanDigest':phase.identity['gitPlanDigest'],'targetOID':preview['targetOID'],
            'expectedPreview':preview,'privatePath':private_path,'journalDirectory':journal,'privateIdentity':identity,
            'destinationDigest':destination_digest,'timeoutMs':remaining}
        task=asyncio.create_task(utility.worker.session_git_worktree_register_request(chat_id,native_id,source_path,payload,
            owner=utility.owner,timeout=max(.001,deadline-loop.time())))
        utility.git_worktree_registration_http_task=task
        cancelled=False
        while not task.done():
            try:await asyncio.shield(task)
            except asyncio.CancelledError:cancelled=True;utility.cancel_requested=True
            except Exception:break
        try:
            reply=task.result();result=reply.json() if reply.status_code==200 else {'state':'unknown'}
        except asyncio.CancelledError:
            cancelled=True;utility.cancel_requested=True;result={'state':'unknown'}
        except Exception:result={'state':'unknown'}
        if not cancelled and type(result) is dict and result=={'state':'registered'} and phase.state=='registered' and await issue_current(utility,context,lambda:True) and await context.recheck():
            return {'state':'registered'}
    except asyncio.CancelledError:
        utility.cancel_requested=True
    except Exception:
        pass
    phase.state='unknown';utility.host_cleanup_unknown=True
    child=asyncio.create_task(utility.process.wait())
    while not child.done():
        try:await asyncio.shield(child)
        except asyncio.CancelledError:utility.cancel_requested=True
    if child.cancelled() or type(child.result()) is not int or utility.process.returncode!=child.result():raise RuntimeError('original worktree registration custody unavailable')
    return {'state':'unknown'}


async def materialize_registered(*, utility):
    """Materialize the accepted registration only in its original private stage."""
    context=getattr(utility,'git_worktree_stage_context',None)
    if type(utility) is not UtilityLifetime or context is None or context.utility is not utility or context.phase.state!='registered':
        raise ValueError('worktree registration unavailable')
    phase=context.phase;deadline=context.deadline;remaining=int((deadline-asyncio.get_running_loop().time())*1000)
    if remaining<=0 or await issue_current(utility,context,lambda:True) is not True or await context.recheck() is not True:
        raise ValueError('worktree authority changed')
    base={k:v for k,v in phase.native.items() if k!='root_path'}
    try:
        reply=await phase.custody.exchange('worktree_private_materialize',{**base,'timeout_ms':min(300000,remaining)})
        value=reply.get('data') if type(reply) is dict else None
        if type(value) is dict and value=={'state':'materialized','target_manifest_sha256':phase.target} and await issue_current(utility,context,lambda:True) and await context.recheck():
            phase.state='materialized';return {'state':'materialized','targetManifestSHA256':phase.target}
    except asyncio.CancelledError:
        utility.cancel_requested=True
    except Exception:
        pass
    phase.state='unknown';utility.host_cleanup_unknown=True
    child=asyncio.create_task(utility.process.wait())
    while not child.done():
        try:await asyncio.shield(child)
        except asyncio.CancelledError:utility.cancel_requested=True
    if child.cancelled() or type(child.result()) is not int or utility.process.returncode!=child.result():raise RuntimeError('original worktree materialization custody unavailable')
    return {'state':'unknown'}


async def prepare_materialized_history(*, utility):
    """Prepare the original absent-folder action before any publication."""
    context=getattr(utility,'git_worktree_stage_context',None)
    if type(utility) is not UtilityLifetime or context is None or context.utility is not utility or context.phase.state!='materialized':
        raise ValueError('worktree materialization unavailable')
    phase=context.phase;remaining=int((context.deadline-asyncio.get_running_loop().time())*1000)
    if remaining<=0 or await issue_current(utility,context,lambda:True) is not True or await context.recheck() is not True:
        raise ValueError('worktree authority changed')
    base={k:v for k,v in phase.native.items() if k!='root_path'}
    try:
        reply=await phase.custody.exchange('worktree_private_prepare_history',{**base,'timeout_ms':min(300000,remaining)})
        if type(reply) is dict and reply.get('data')=={'state':'history_prepared'} and await issue_current(utility,context,lambda:True) and await context.recheck():
            phase.state='history_prepared';return {'state':'history_prepared'}
    except asyncio.CancelledError:
        utility.cancel_requested=True
    except Exception:
        pass
    phase.state='unknown';utility.host_cleanup_unknown=True
    child=asyncio.create_task(utility.process.wait())
    while not child.done():
        try:await asyncio.shield(child)
        except asyncio.CancelledError:utility.cancel_requested=True
    if child.cancelled() or type(child.result()) is not int or utility.process.returncode!=child.result():raise RuntimeError('original worktree history custody unavailable')
    return {'state':'unknown'}


def _publication_action(*,native_id,chat_id,operation_id,request_digest,git_plan_digest,source_path,private_path,destination_path,registration):
    ident=lambda v:[v['device'],v['inode']]
    file=lambda v:[v['device'],v['inode'],v['size'],v['sha256']]
    marker=registration['marker']
    material=['openclank.git-worktree.publication.v1',native_id,chat_id,operation_id,request_digest,git_plan_digest,source_path,private_path,destination_path,
        ident(registration['privateIdentity']),file(marker)+[marker['bytesBase64']],[registration['adminName'],*ident(registration['adminIdentity'])],file(registration['backlink']),file(registration['head']),file(registration['index'])]
    return hashlib.sha256(_encoded(material)).hexdigest()


def _settle_reviewed_stage(utility,context):
    pending=context.stage_pending
    if pending is None:return True  # retained legacy private callers are not advertised
    repository=context.stage_repository
    with repository._connect() as db:
        repository._begin(db)
        row=db.execute('SELECT request_digest,generation,receipt_ciphertext,lifecycle_phase FROM file_operations WHERE owner_subject_id=? AND operation_id=?',(utility.account,utility.idempotency_key)).fetchone()
        if row is None or row['request_digest']!=utility.request_hash or row['generation']!=context.stage_generation or row['lifecycle_phase']!='git_worktree_stage_pending':db.rollback();return False
        old=row['receipt_ciphertext']
        try:previous=json.loads(decrypt(str(old)))
        except Exception:db.rollback();return False
        if previous!=pending:db.rollback();return False
        receipt=dict(pending);receipt.update({'state':'applied','settled':True,'settlement':'original_git_files_history_ack','cleanup':'pending'})
        encoded=encrypt(json.dumps(receipt,sort_keys=True,separators=(',',':'),ensure_ascii=False))
        changed=db.execute("UPDATE file_operations SET receipt_ciphertext=?,lifecycle_phase='git_worktree_stage_settled' WHERE owner_subject_id=? AND operation_id=? AND request_digest=? AND generation=? AND lifecycle_phase='git_worktree_stage_pending' AND receipt_ciphertext=?",(encoded,utility.account,utility.idempotency_key,utility.request_hash,context.stage_generation,old))
        if changed.rowcount!=1:db.rollback();return False
        db.commit();return True


async def publish_prepared(*, utility,chat_id,native_id,source_path,preview):
    """Publish only the History-prepared original; never issue another attempt."""
    context=getattr(utility,'git_worktree_stage_context',None)
    if type(utility) is not UtilityLifetime or context is None or context.utility is not utility or context.phase.state!='history_prepared':raise ValueError('worktree publication unavailable')
    if getattr(context,'source_binding',None)!=(chat_id,native_id,source_path,hashlib.sha256(_encoded(preview)).hexdigest()):raise ValueError('worktree source binding changed')
    phase=context.phase;deadline=context.deadline;loop=asyncio.get_running_loop();remaining=int((deadline-loop.time())*1000)
    if remaining<=0 or await issue_current(utility,context,lambda:True) is not True or await context.recheck() is not True:raise ValueError('worktree authority changed')
    registration=phase.registration['witness'];private_path=phase.registration['privatePath'];destination=phase.registration['destinationPath']
    action=_publication_action(native_id=native_id,chat_id=chat_id,operation_id=utility.idempotency_key,request_digest=utility.request_hash,
        git_plan_digest=phase.identity['gitPlanDigest'],source_path=source_path,private_path=private_path,destination_path=destination,registration=registration)
    phase.arm_publication(action_sha256=action,destination_path=destination,deadline=deadline)
    payload={'version':1,'stableChatID':chat_id,'operationID':utility.idempotency_key,'requestDigest':utility.request_hash,
        'capability':phase.identity['capability'],'gitPlanDigest':phase.identity['gitPlanDigest'],'privatePath':private_path,
        'destinationPath':destination,'registration':registration,'timeoutMs':min(300000,remaining)}
    task=asyncio.create_task(utility.worker.session_git_worktree_publish_request(chat_id,native_id,source_path,payload,
        owner=utility.owner,timeout=max(.001,deadline-loop.time())))
    utility.git_worktree_publication_http_task=task;cancelled=False
    while not task.done():
        try:await asyncio.shield(task)
        except asyncio.CancelledError:cancelled=True;utility.cancel_requested=True
        except Exception:break
    try:
        response=task.result();result=response.json() if response.status_code==200 else {'state':'unknown'}
    except asyncio.CancelledError:
        cancelled=True;utility.cancel_requested=True;result={'state':'unknown'}
    except Exception:result={'state':'unknown'}
    try:
        if not cancelled and type(result) is dict and result=={'state':'committed'} and phase.state=='completed' and await issue_current(utility,context,lambda:True) and await context.recheck() and _settle_reviewed_stage(utility,context):
            if context.stage_pending is None:return {'state':'completed'}
            try:
                released=(await phase.custody.exchange('worktree_release_completed',{k:v for k,v in phase.native.items() if k!='root_path'})).get('data')
                if released=={'state':'released'} and record_cleanup(context.stage_repository,utility,context.stage_generation,'git_worktree_stage','released'):return {'state':'completed','cleanup':'released'}
            except Exception:pass
            record_cleanup(context.stage_repository,utility,context.stage_generation,'git_worktree_stage','unknown')
            utility.host_cleanup_unknown=True;return {'state':'completed','cleanup':'unknown'}
    except asyncio.CancelledError:utility.cancel_requested=True
    phase.state='unknown';utility.host_cleanup_unknown=True
    child=asyncio.create_task(utility.process.wait())
    while not child.done():
        try:await asyncio.shield(child)
        except asyncio.CancelledError:utility.cancel_requested=True
    if child.cancelled() or type(child.result()) is not int or utility.process.returncode!=child.result():raise RuntimeError('original worktree publication custody unavailable')
    return {'state':'unknown'}
