"""One admitted workflow artifact set over the existing plan and Files owners.

The caller owns live actor/root, privacy, workspace and account admission. The
callbacks below are private Host guards, never model- or client-issued grants.
"""
from __future__ import annotations

import os
from collections.abc import Mapping

from src.openclank.files_host_provider import _origin
from src.openclank.transcript_projection import get_mimo_state
from src.openclank.workflow_file_access import (workflow_create, workflow_create_status,
    resolve_workflow_member, workflow_replace, workflow_status, workflow_read, qualified_workflow_object_tag)
from src.plan_approval import (bind_artifact_association, bind_artifact_member,
    reserve_artifact_set, join_artifact_association, workflow_member_path, record_metaplan_create, record_member_create, pending_artifact_write,
    create_initial_workflow_draft, transition_workflow_plan)
from src.plan_files import metaplan_relpath


async def apply(*, facade, context, chat_id: str, workspace_root: str, subject: Mapping,
                role: str, name: str | None, operation_id: str, text: str | None,
                expected_revision: Mapping | None, status: bool, current, current_sync, history_identity, writer_session_id: str, client_factory, history_root_id: str, history_root_path: str, task_ref=None, read: bool = False, initial_plan_allowed: bool = False, writer_identity=None):
    """Execute an already authenticated invocation or query its original status.

    `current_sync` is the exact Host account/turn/plan transaction fence;
    `current` additionally rechecks the Native writer invocation and Files root.
    They must be current per call, not recovered historical credentials.
    """
    unavailable = {'state':'unavailable'} if read else {'state':'unavailable','operationID':operation_id}
    if (not all(callable(fn) for fn in (current,current_sync,history_identity,client_factory))
            or current_sync() is not True or await current() is not True): return unavailable
    plan = get_mimo_state(chat_id,owner=context.owner_username).get('plan_state')
    if plan is None and initial_plan_allowed is True and role == 'metaplan' and not status and not read and expected_revision is None:
        if not isinstance(text,str) or not text.strip() or len(text)>8192 or await current() is not True or current_sync() is not True:
            return unavailable
        # The existing plan owner performs a create-only CAS; no approval is granted.
        plan = create_initial_workflow_draft(chat_id,context.owner_username,text,current_authority=current_sync)
        if await current() is not True or current_sync() is not True: return unavailable
    if not isinstance(plan,dict) or plan.get('status') == 'cleared': return unavailable
    revision,digest = plan.get('revision'),plan.get('digest')
    if type(revision) is not int or revision < 1 or not isinstance(digest,str): return unavailable
    async def guard():
        if current_sync() is not True or await current() is not True: return False
        latest = get_mimo_state(chat_id,owner=context.owner_username).get('plan_state')
        return (isinstance(latest,dict) and latest.get('status') != 'cleared' and latest.get('revision') == revision
                and latest.get('digest') == digest and current_sync() is True)
    if not await guard(): return unavailable
    pre_reserved=False
    if role == 'metaplan' and not status and not read and isinstance(text,str) and text.strip()!=str(plan.get('plan') or '').strip():
        if initial_plan_allowed is not True or expected_revision is None or len(text)>8192: return unavailable
        old_assoc=plan.get('artifact_association');old_set=plan.get('artifact_set')
        old_members=old_assoc.get('members') if isinstance(old_assoc,dict) else None
        intents=old_assoc.get('create_intents') if isinstance(old_assoc,dict) else None
        if not isinstance(old_members,dict) or not isinstance(old_set,dict) or not isinstance(old_members.get('metaplan'),dict): return unavailable
        if intents is not None and not isinstance(intents,dict): return unavailable
        for key,intent in (intents or {}).items():
            if key not in old_members:
                return {'state':'unknown','operationID':intent.get('operation_id',operation_id)} if isinstance(intent,dict) else unavailable
        for member in old_members.values():
            if not isinstance(member,dict): return unavailable
            pending=member.get('pending_write')
            if pending is not None:
                return {'state':'unknown','operationID':pending.get('operation_id',operation_id)} if isinstance(pending,dict) else unavailable
        meta_path=old_members['metaplan'].get('relative_path')
        checked={}
        for key,member in old_members.items():
            try:
                if key != (member.get('role') if member.get('name') is None else member.get('role')+':'+member.get('name')) or member.get('relative_path')!=workflow_member_path(meta_path,member.get('role'),member.get('name')):
                    return unavailable
                observed=await resolve_workflow_member(facade,context,workspace_root=workspace_root,member=member,history_identity=history_identity,current=guard)
                if member.get('revision') != observed.get('revision'): return unavailable
                if key=='metaplan' and observed.get('revision')!=expected_revision: return {'state':'conflict','operationID':operation_id}
                checked[key]=member
            except Exception:return unavailable
        if not await guard() or current_sync() is not True: return unavailable
        plan=transition_workflow_plan(chat_id,context.owner_username,expected_plan=plan,text=text,operation_id=operation_id,
            verified_members=checked,writer_authority=current_sync,current_authority=current_sync)
        revision,digest=plan['revision'],plan['digest'];pre_reserved=True
        if not await guard(): return {'state':'unknown','operationID':operation_id}
    subject = dict(subject)
    association = plan.get('artifact_association')
    reserved = plan.get('artifact_set')
    if association is None and (role != 'metaplan' or read): return unavailable
    if association is None and not status and not read and reserved is None:
        reserved = reserve_artifact_set(chat_id,context.owner_username,account_id=context.owner_subject_id,
            workspace_id=context.workspace_id,subject=subject,expected_revision=revision,expected_digest=digest,
            association_authority=current_sync,current_authority=current_sync)
    # A later live writer joins the same incomplete reservation; its original
    # subject remains provenance, never a present-tense authorization.
    if (not isinstance(reserved,dict) or reserved.get('account_id') != context.owner_subject_id
            or reserved.get('workspace_id') != context.workspace_id or reserved.get('plan_revision') != revision
            or reserved.get('plan_digest') != digest or not await guard()): return unavailable
    set_id=reserved.get('artifact_set_id')
    workflow_origin = None
    if not status and not read and isinstance(writer_identity,dict):
        from src.openclank.workflow_result_origin import origin_binding
        workflow_origin={'version':1,'account':context.owner_subject_id,'chat':chat_id,
            **{key:writer_identity.get(key) for key in ('sessionID','rootOperationID','actorID','executionRevision','innerTurnID','toolCallID')},
            'operationID':operation_id,'artifactSetID':set_id,'planRevision':revision,'planDigest':digest,'role':role,'name':name}
        # Validate at the already-admitted Host boundary, before any file issue.
        origin_binding('0'*64,workflow_origin)
    if association is not None:
        if status or read:
            if (not isinstance(association,dict) or association.get('artifact_set_id') != set_id
                    or association.get('account_id') != context.owner_subject_id or association.get('workspace_id') != context.workspace_id
                    or association.get('plan_revision') != revision or association.get('plan_digest') != digest
                    or reserved.get('plan_revision') != revision or reserved.get('plan_digest') != digest
                    or association.get('subject') != reserved.get('subject') or not await guard()): return unavailable
        else:
            association=join_artifact_association(chat_id,context.owner_username,artifact_set_id=set_id,
                account_id=context.owner_subject_id,workspace_id=context.workspace_id,expected_revision=revision,
                expected_digest=digest,writer_authority=current_sync,current_authority=current_sync)
        meta=(association.get('members') or {}).get('metaplan')
        if isinstance(meta,dict):
            metaplan=meta['relative_path']
        else:
            intent=reserved.get('metaplan_create')
            if not isinstance(intent,dict) or role != 'metaplan': return unavailable
            metaplan=intent.get('relative_path')
    else:
        intent=reserved.get('metaplan_create')
        metaplan=(intent.get('relative_path') if isinstance(intent,dict) else None) or plan.get('artifact_relpath') or metaplan_relpath(str(plan.get('plan') or ''))
    relative=workflow_member_path(metaplan,role,name)
    root=os.path.realpath(workspace_root)
    target=os.path.abspath(os.path.join(root,*relative.split('/')))
    if os.path.commonpath((root,target)) != root or os.path.realpath(target) != target: return unavailable
    provider=facade._providers.get('host')
    if provider is None: return unavailable
    members=(association.get('members') or {}) if isinstance(association,dict) else {}
    if association is not None and isinstance(members.get('metaplan'),dict):
        # No member result or create is valid if the bound metaplan drifted.
        await resolve_workflow_member(facade,context,workspace_root=root,member=members['metaplan'],
                                      history_identity=history_identity,current=guard)
    async def object_tag(expected):
        return await qualified_workflow_object_tag(context,path=target,expected_revision=expected,workspace_root=root,chat_id=chat_id,
            history_root_id=history_root_id,history_root_path=history_root_path,current=guard)
    key=role if name is None else role+':'+name
    member=members.get(key)
    if role=='progress' and member is not None and (not isinstance(member,dict) or member.get('task_ref')!=task_ref): return unavailable
    if read and member is None:
        intent=reserved.get('metaplan_create') if role=='metaplan' else (association.get('create_intents') or {}).get(key)
        return {'state':'unknown' if intent is not None else 'not_found'}
    if member is not None:
        resource=await resolve_workflow_member(facade,context,workspace_root=root,member=member,
                                               history_identity=history_identity,current=guard)
        if read:
            return await workflow_read(facade,context,resource_ref=resource['ref'],resource_id=member['resource_id'],
                workspace_root=root,chat_id=chat_id,history_resource_id=member['history_resource_id'],history_identity=history_identity,current=guard,history_root_id=history_root_id,history_root_path=history_root_path)
        args=dict(facade=facade,context=context,resource_ref=resource['ref'],resource_id=member['resource_id'],
                  key=operation_id,history_resource_id=member['history_resource_id'],history_identity=history_identity,current=guard,client_factory=client_factory)
        if not status:
            pending=member.get('pending_write')
            if pending is not None and not (pre_reserved and pending=={'operation_id':operation_id}):
                if not isinstance(pending,dict) or not isinstance(pending.get('operation_id'),str): return unavailable
                original_id=pending['operation_id']; original=await workflow_status(**{**args,'key':original_id})
                if original.get('state')!='complete': return {'state':'unknown','operationID':original_id}
                tag=await object_tag(original['revision'])
                if tag is None: return {'state':'unknown','operationID':original_id}
                pending_artifact_write(chat_id,context.owner_username,artifact_set_id=set_id,role=role,name=name,
                    operation_id=original_id,expected_revision=revision,expected_digest=digest,writer_authority=current_sync,current_authority=current_sync,settle=True,committed_revision=original['revision'],committed_object_tag=tag)
                if original_id==operation_id:
                    return {'state':'complete','operationID':original_id,'resourceRef':resource['ref'],'revision':original['revision']}
                if not await guard(): return unavailable
            if not pre_reserved:
                pending_artifact_write(chat_id,context.owner_username,artifact_set_id=set_id,role=role,name=name,
                    operation_id=operation_id,expected_revision=revision,expected_digest=digest,writer_authority=current_sync,current_authority=current_sync)
        result=await workflow_status(**args) if status else await workflow_replace(**args,expected_revision=expected_revision,text=text,workflow_origin=workflow_origin)
        if not status and result.get('state')=='complete':
            if not await guard(): return {'state':'unknown','operationID':operation_id}
            tag=await object_tag(result['revision'])
            if tag is None: return {'state':'unknown','operationID':operation_id}
            pending_artifact_write(chat_id,context.owner_username,artifact_set_id=set_id,role=role,name=name,
                operation_id=operation_id,expected_revision=revision,expected_digest=digest,writer_authority=current_sync,current_authority=current_sync,settle=True,committed_revision=result['revision'],committed_object_tag=tag)
        if status and result.get('state') == 'unknown':
            original_intent=reserved.get('metaplan_create') if role=='metaplan' else (association.get('create_intents') or {}).get(key)
            if isinstance(original_intent,dict) and original_intent.get('operation_id')==operation_id:
                original=await workflow_create_status(client_factory=client_factory,repository=provider.operation_store,
                    account=context.owner_subject_id,workspace=context.workspace_id,session_id=original_intent['writer_session_id'],key=operation_id,path=target,current=guard)
                if original.get('state') == 'committed':
                    return {'state':'complete','operationID':operation_id,'resourceRef':resource['ref'],'revision':original['revision']}
        if result.get('state') == 'complete': return {'state':'complete','operationID':operation_id,'resourceRef':resource['ref'],'revision':result['revision']}
        return {'state':result.get('state','unknown'),'operationID':operation_id}
    intent = reserved.get('metaplan_create') if role == 'metaplan' else (association.get('create_intents') or {}).get(key)
    if status:
        if not isinstance(intent,dict) or intent.get('operation_id') != operation_id or intent.get('relative_path') != relative: return unavailable
        result=await workflow_create_status(client_factory=client_factory,repository=provider.operation_store,account=context.owner_subject_id,
            workspace=context.workspace_id,session_id=intent['writer_session_id'],key=operation_id,path=target,current=guard)
        return {'state':'partial' if result.get('state') == 'committed' else 'unknown' if result.get('state') in {'unknown','not_applied'} else 'unavailable','operationID':operation_id}
    if expected_revision is not None or not isinstance(text,str): return unavailable
    if role == 'metaplan' and text.strip() != str(plan.get('plan') or '').strip(): return unavailable
    old=intent
    if isinstance(old,dict):
        original=old.get('operation_id')
        checked=await workflow_create_status(client_factory=client_factory,repository=provider.operation_store,account=context.owner_subject_id,
            workspace=context.workspace_id,session_id=old['writer_session_id'],key=original,path=target,current=guard)
        if checked.get('state') != 'committed': return {'state':'unknown','operationID':original}
        try:
            observed=await provider.stat(context,origin_id=_origin(target))
            resource=facade._resource(context,'host',observed,parent_stable_id=None)
            history_id=await history_identity(target)
            if (observed.origin_id != _origin(target) or observed.kind != 'file' or resource.get('revision') != checked['revision']
                    or not isinstance(history_id,str) or not history_id.startswith('file:') or not await guard()):
                return {'state':'partial','operationID':original}
            if association is None:
                association=bind_artifact_association(chat_id,context.owner_username,
                    {'version':1,'account_id':context.owner_subject_id,'workspace_id':context.workspace_id,
                     'subject':reserved['subject'],'metaplan_resource_id':resource['id']},expected_revision=revision,expected_digest=digest,
                    association_authority=current_sync,current_authority=current_sync)
            tag=await object_tag(resource['revision'])
            if tag is None: return {'state':'partial','operationID':original}
            bind_artifact_member(chat_id,context.owner_username,artifact_set_id=set_id,role=role,name=name,relative_path=relative,
                resource_id=resource['id'],history_resource_id=history_id,revision=resource['revision'],object_tag=tag,expected_revision=revision,expected_digest=digest,
                writer_authority=current_sync,current_authority=current_sync,task_ref=task_ref)
            if not await guard(): return {'state':'partial','operationID':original}
            return {'state':'complete','operationID':original,'resourceRef':resource['ref'],'revision':resource['revision']}
        except Exception:
            return {'state':'partial','operationID':original}
    parent_path=os.path.dirname(target)
    try:
        entry=await provider.stat(context,origin_id=_origin(parent_path))
    except Exception:
        return unavailable
    if entry.origin_id != _origin(parent_path) or entry.kind not in {'folder','provider_root'}: return unavailable
    parent=facade._resource(context,'host',entry,parent_stable_id=None)
    if role == 'metaplan':
        record_metaplan_create(chat_id,context.owner_username,artifact_set_id=set_id,operation_id=operation_id,writer_session_id=writer_session_id,
            relative_path=relative,expected_revision=revision,expected_digest=digest,writer_authority=current_sync,current_authority=current_sync)
    else:
        record_member_create(chat_id,context.owner_username,artifact_set_id=set_id,role=role,name=name,operation_id=operation_id,writer_session_id=writer_session_id,
            relative_path=relative,expected_revision=revision,expected_digest=digest,writer_authority=current_sync,current_authority=current_sync,task_ref=task_ref)
    async def bind(resource,history_id):
        nonlocal association
        if await guard() is not True or current_sync() is not True: return False
        if association is None:
            association=bind_artifact_association(chat_id,context.owner_username,
                {'version':1,'account_id':context.owner_subject_id,'workspace_id':context.workspace_id,
                 'subject':reserved['subject'],'metaplan_resource_id':resource['id']},expected_revision=revision,expected_digest=digest,
                association_authority=current_sync,current_authority=current_sync)
        tag=await object_tag(resource['revision'])
        if tag is None: return False
        bind_artifact_member(chat_id,context.owner_username,artifact_set_id=set_id,role=role,name=name,relative_path=relative,
            resource_id=resource['id'],history_resource_id=history_id,revision=resource['revision'],object_tag=tag,expected_revision=revision,expected_digest=digest,
            writer_authority=current_sync,current_authority=current_sync,task_ref=task_ref)
        return True
    result=await workflow_create(facade,context,parent_ref=parent['ref'],name=os.path.basename(target),text=text,
        expected_parent_revision=parent['revision'],key=operation_id,session_id=writer_session_id,client_factory=client_factory,current=guard,history_identity=history_identity,bind=bind,workflow_origin=workflow_origin)
    if result.get('state') == 'complete':
        resource=result['resource']; return {'state':'complete','operationID':operation_id,'resourceRef':resource['ref'],'revision':resource['revision']}
    return {'state':result.get('state','unknown'),'operationID':operation_id}
