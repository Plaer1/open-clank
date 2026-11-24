"""Resolve a workflow result selector under current Human Files authority."""
from __future__ import annotations
import asyncio
import json
import os
from src.openclank.workflow_result_origin import origin_binding
from src.openclank.workflow_memory_links import read_current_member

async def resolve(*, selector, account, owner, repository, facade, auth, session, current):
    from src.openclank.files_facade import ProviderContext
    from src.openclank.ordinary_create import _identity, _digest
    from src.openclank.transcript_projection import get_mimo_state
    from src.openclank.workflow_file_replace import _hash
    from src.openclank.workspace_policy_service import resolve_owned_workspace
    from src.plan_approval import workflow_member_path
    try:
        if not isinstance(selector,dict) or set(selector)!={'version','kind','account','chat','sessionID','rootOperationID','actorID','executionRevision','innerTurnID','toolCallID','operationID','artifactSetID','planRevision','planDigest','role','name','revision'}:
            return None
        if selector.get('kind')!='workflow-member' or selector.get('account')!=account or selector.get('chat')!=session.id or session.owner!=owner or not await current(): return None
        origin={k:v for k,v in selector.items() if k not in ('kind','revision')}
        revision=selector['revision']; operation=origin['operationID']
        if not isinstance(revision,dict) or set(revision)!={'kind','value'} or revision.get('kind')!='hostFingerprint': return None
        plan=get_mimo_state(session.id,owner=owner).get('plan_state'); association=plan.get('artifact_association') if isinstance(plan,dict) else None
        reserved=plan.get('artifact_set') if isinstance(plan,dict) else None
        if (not isinstance(association,dict) or not isinstance(reserved,dict) or plan.get('status')=='cleared'
            or association.get('account_id')!=account or association.get('artifact_set_id')!=origin['artifactSetID']
            or reserved.get('artifact_set_id')!=origin['artifactSetID'] or association.get('plan_revision')!=origin['planRevision']
            or association.get('plan_digest')!=origin['planDigest'] or plan.get('revision')!=origin['planRevision']
            or plan.get('digest')!=origin['planDigest'] or reserved.get('plan_revision')!=origin['planRevision']
            or reserved.get('plan_digest')!=origin['planDigest'] or association.get('workspace_id')!=reserved.get('workspace_id')): return None
        workspace=association['workspace_id']; members=association.get('members'); role,name=origin['role'],origin['name']; key=role if name is None else role+':'+name
        member=members.get(key) if isinstance(members,dict) else None; meta=members.get('metaplan') if isinstance(members,dict) else None
        if (not isinstance(member,dict) or not isinstance(meta,dict) or member.get('role')!=role or member.get('name')!=name
            or member.get('relative_path')!=workflow_member_path(meta.get('relative_path'),role,name) or member.get('revision')!=revision
            or member.get('pending_write') is not None): return None
        binding=resolve_owned_workspace(repository,workspace_id=workspace,owner_username=owner,auth_manager=auth,purpose='app_folder')
        root=os.path.realpath(binding.path); path=os.path.abspath(os.path.join(root,*member['relative_path'].split('/')))
        if os.path.commonpath((root,path))!=root or os.path.realpath(path)!=path: return None
        # Receipts are owner-scoped and immutable with respect to their request
        # digest. A selector supplied by imported chat is never authority.
        create=_identity(account,workspace,origin['sessionID'],'agent',operation)
        replace='workflow-'+_hash(['openclank.workflow.replace-key.v1',account,workspace,operation])
        matched=[]
        for kind,key_id in (('create',create),('replace',replace)):
            row=repository.get_operation(owner_subject_id=account,operation_id=key_id)
            receipt=row.get('receipt') if isinstance(row,dict) else None
            if not isinstance(receipt,dict) or receipt.get('workflow_origin')!=origin_binding(row.get('digest'),origin): continue
            native=receipt.get('binding') if kind=='create' else receipt.get('native_binding')
            if not isinstance(native,dict): continue
            fingerprint=f"sha256:{native.get('desired_sha256')}:{native.get('desired_size')}"
            if revision.get('value')!=fingerprint or receipt.get('workspace')!=workspace: continue
            capture=receipt.get('capture_identity'); desired=native.get('desired_sha256'); size=native.get('desired_size')
            if not isinstance(capture,dict) or capture.get('account_id')!=account or type(size) is not int or not 0<=size<=262144: continue
            if kind=='create':
                action='create_'+_digest(['openclank.files.create-action.v1',key_id]); parent=native.get('parent_revision')
                if (receipt.get('kind')!='ordinary_create' or receipt.get('lane')!='agent' or receipt.get('session')!=origin['sessionID']
                    or receipt.get('path')!=path or receipt.get('settled') is not True
                    or receipt.get('confirmed_commit')!={'version':1,'fingerprint':fingerprint}
                    or native.get('operation_id')!=key_id or native.get('action_id')!=action
                    or native.get('request_digest')!=_digest(['openclank.files.ordinary-create.v1',account,path,parent,desired,size,key_id,action])
                    or row.get('digest')!=_digest(['openclank.files.create-request.v1',account,workspace,origin['sessionID'],'agent',path,parent,desired,size,key_id,action])): continue
            else:
                action='workflow-'+_hash(['openclank.workflow.replace-action.v1',key_id]); expected=native.get('expected_fingerprint')
                if (receipt.get('kind')!='workflow.artifact.v1' or receipt.get('state')!='complete' or receipt.get('revision')!=revision
                    or not isinstance(receipt.get('claim'),dict) or receipt.get('target_digest')!=_hash(['openclank.workflow.target.v1',account,workspace,path])
                    or native.get('operation_id')!=key_id or native.get('action_id')!=action
                    or native.get('request_digest')!=_hash(['openclank.files.ordinary-replace.v1',account,path,expected,desired,key_id,action])
                    or row.get('digest')!=_hash(['openclank.workflow.replace-request.v1',account,workspace,path,expected,desired,size,key_id,action])): continue
            matched.append((key_id,row))
        if len(matched)!=1: return None
        generation=repository.generation(); files=ProviderContext(account,owner,generation,bool(auth.is_admin(owner)),workspace)
        host=facade._providers.get('host'); baseline=json.dumps(association,sort_keys=True,separators=(',',':'))
        def durable_scope_current():
            now=get_mimo_state(session.id,owner=owner).get('plan_state'); assoc=now.get('artifact_association') if isinstance(now,dict) else None
            return bool(repository.generation()==generation and auth.account_id(owner)==account
                and auth.username_for_account_id(account)==owner and isinstance(assoc,dict)
                and now.get('revision')==origin['planRevision'] and now.get('digest')==origin['planDigest']
                and json.dumps(assoc,sort_keys=True,separators=(',',':'))==baseline
                and resolve_owned_workspace(repository,workspace_id=workspace,owner_username=owner,auth_manager=auth,purpose='app_folder')==binding)
        async def still_current():
            try:
                if not await current(): return False
                observed=await asyncio.to_thread(durable_scope_current)
                return bool(observed and await current())
            except Exception: return False
        if host is None or not await still_current(): return None
        resource,actual,_content,_path=await read_current_member(host=host,facade=facade,files=files,root=root,member=member,current=still_current)
        if actual!=revision or _path!=path or not await still_current(): return None
        final_receipt=await asyncio.to_thread(repository.get_operation,owner_subject_id=account,operation_id=matched[0][0])
        if final_receipt != matched[0][1] or not await still_current(): return None
        return {'resourceRef':resource['ref'],'revision':actual}
    except Exception:
        return None
