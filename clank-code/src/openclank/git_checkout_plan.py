"""Bounded, Files-authorized preview for a reviewed Git checkout.

This is read-only and grants no mutation authority.  The original caller owns
account/session currentness and the Native Files journal owns publication.
"""
from __future__ import annotations

import difflib
import hashlib
import json
import os
import re
import stat

from src.openclank.files_host_provider import _origin, _path
from src.openclank.git_workbench import allowed_relative_path

MAX_TRACKED=4096
MAX_SOURCE_BYTES=128*1024*1024
MAX_SINGLE_SOURCE=32*1024*1024
MAX_ITEM=256*1024
MAX_CHANGED=64
MAX_AFTER=4*1024*1024


def _compact(value):
    return json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False).encode('utf-8','strict')


def _text(data):
    if b'\0' in data: raise ValueError('binary checkout participant unavailable')
    return data.decode('utf-8','strict')


def _entries(values,width):
    if type(values) is not list or len(values)>65536: raise ValueError('checkout inventory unavailable')
    result={};previous=None
    for row in values:
        if (type(row) is not dict or set(row)!={'path','mode','oid'} or not allowed_relative_path(row['path'])
                or type(row['mode']) is not int or row['mode'] not in (0o100644,0o100755)
                or type(row['oid']) is not str or not re.fullmatch('[0-9a-f]{%d}'%width,row['oid'])):
            raise ValueError('checkout inventory unavailable')
        encoded=row['path'].encode('utf-8','strict')
        if previous is not None and encoded<=previous: raise ValueError('checkout inventory unavailable')
        previous=encoded;result[row['path']]=row
    return result


async def build_checkout_files(*,preview,expected_operation,expected_ref,provider,repository,context,
        root_origin,location_id,chat_id,is_admin,current,read_blob,probe_child):
    """Prove tracked raw cleanliness and produce ≤64 exact Files participants.

    Untracked/ignored paths are never enumerated. Native checkout prepare is
    responsible for exact affected-path absence and pinned-parent collision checks.
    """
    if (type(preview) is not dict or set(preview)!={'version','operation','source','targetRef','targetOID','targetEntries','changed'}
            or preview['version']!=1 or preview['operation']!=expected_operation or preview['targetRef']!=expected_ref
            or expected_operation not in ('switch','pull') or type(preview['source']) is not dict):
        raise ValueError('checkout preview unavailable')
    source=preview['source'];physical=source.get('physicalIndex')
    if type(physical) is not dict or physical.get('objectFormat') not in ('sha1','sha256'): raise ValueError('checkout source unavailable')
    width=40 if physical['objectFormat']=='sha1' else 64
    if type(preview['targetOID']) is not str or not re.fullmatch('[0-9a-f]{%d}'%width,preview['targetOID']): raise ValueError('checkout target unavailable')
    old=_entries(source.get('indexEntries'),width);head=_entries(source.get('headEntries'),width);new=_entries(preview['targetEntries'],width)
    if old!=head or len(old)>MAX_TRACKED: raise ValueError('checkout source unavailable')
    changed=sorted((path for path in old.keys()|new.keys() if old.get(path)!=new.get(path)),key=lambda value:value.encode('utf-8'))
    if type(preview['changed']) is not list or preview['changed']!=changed or not 1<=len(changed)<=MAX_CHANGED: raise ValueError('checkout participant unavailable')
    changed_set=set(changed);root=_path(root_origin)
    if not os.path.isabs(root) or await current() is not True: raise ValueError('checkout authority changed')
    root_entry=await probe_child(parent_path=root,root_id=location_id,parent_object_tag=None,component=None)
    if (type(root_entry) is not dict or root_entry.get('state')!='present' or root_entry.get('kind')!='directory'
            or not re.fullmatch(r'[0-9a-f]{64}',str(root_entry.get('object_tag') or ''))): raise ValueError('checkout root unavailable')
    root_revision={'kind':'hostObjectIdentity','value':root_entry['object_tag']}
    scope=provider._scope(context);client=provider._stream_client(context,scope)
    before={};source_total=0
    for path,row in old.items():
        if await current() is not True: raise ValueError('checkout authority changed')
        decision=repository.resolve(subject_id=context.owner_subject_id,is_admin=is_admin,origin='app',location_id=location_id,
            capability='read',workspace_id=context.workspace_id,relative_resource=path,chat_id=chat_id,group_ids=(),observation_only=True)
        if not decision.allowed or decision.policy_generation!=context.policy_generation: raise ValueError('checkout read unavailable')
        absolute=os.path.join(root,path)
        if os.path.commonpath((root,absolute))!=root: raise ValueError('checkout path unavailable')
        opened=await client.open_handle(absolute);size=opened.get('size');mode=opened.get('mode');tag=opened.get('object_tag')
        if (opened.get('kind')!='File' or type(size) is not int or not 0<=size<=MAX_SINGLE_SOURCE or type(mode) is not int
                or not stat.S_ISREG(mode) or bool(mode & 0o111)!=(row['mode']==0o100755)
                or type(tag) is not str or not re.fullmatch(r'[0-9a-f]{64}',tag) or type(opened.get('handle')) is not dict):
            raise ValueError('checkout source unavailable')
        source_total+=size
        if source_total>MAX_SOURCE_BYTES or (path in changed_set and size>MAX_ITEM): raise ValueError('checkout source bound')
        git_hash=hashlib.sha1() if width==40 else hashlib.sha256();git_hash.update(('blob %d\0'%size).encode('ascii'))
        content_hash=hashlib.sha256();count=0;captured=bytearray() if path in changed_set else None
        if size:
            async for chunk in client.stream_read_handle(opened['handle'],offset=0,length=size):
                if type(chunk) is not bytes or count+len(chunk)>size or await current() is not True: raise ValueError('checkout source changed')
                count+=len(chunk);git_hash.update(chunk);content_hash.update(chunk)
                if captured is not None: captured.extend(chunk)
        if count!=size or git_hash.hexdigest()!=row['oid']: raise ValueError('tracked worktree is changed')
        post=await provider.stat_inspection(context,origin_id=_origin(absolute))
        if (post.kind!='file' or post.origin_id!=_origin(absolute) or post.size!=size
                or dict(post.revision or {})!={'kind':'hostObjectIdentity','value':tag}): raise ValueError('checkout source changed')
        if captured is not None:
            if (mode & 0o777) not in (0o644,0o755): raise ValueError('checkout mode unavailable')
            raw=bytes(captured);_text(raw)
            before[path]=({'kind':'file','sha256':content_hash.hexdigest(),'size':size,'object_tag':tag,'mode':mode & 0o777},raw)
    participants=[];review=[];after_total=0
    # Exact-child probes avoid scanning unrelated siblings. Native prepare
    # independently verifies every affected absence and pins each parent.
    directories={'':root_revision['value']};created=set()
    for path in changed:
        parts=path.split('/'); parent=''
        for component in parts[:-1]:
            child='/'.join(filter(None,(parent,component)))
            if child in directories or child in created:
                parent=child;continue
            if await current() is not True: raise ValueError('checkout authority changed')
            absolute=os.path.join(root,child)
            decision=repository.resolve(subject_id=context.owner_subject_id,is_admin=is_admin,origin='app',location_id=location_id,
                capability='read',workspace_id=context.workspace_id,relative_resource=child,chat_id=chat_id,group_ids=(),observation_only=True)
            if not decision.allowed or decision.policy_generation!=context.policy_generation: raise ValueError('checkout parent unavailable')
            if parent in created: result={'state':'absent'}
            else: result=await probe_child(parent_path=os.path.join(root,parent),root_id=location_id,parent_object_tag=directories[parent],component=component)
            if type(result) is not dict: raise ValueError('checkout parent unavailable')
            if result.get('state')=='absent':
                decision=repository.resolve(subject_id=context.owner_subject_id,is_admin=is_admin,origin='app',location_id=location_id,
                    capability='write',workspace_id=context.workspace_id,relative_resource=child,chat_id=chat_id,group_ids=(),observation_only=True)
                if not decision.allowed or decision.policy_generation!=context.policy_generation or not provider._path_writable(context,absolute,scope): raise ValueError('checkout parent unavailable')
                if path in old: raise ValueError('tracked checkout parent unavailable')
                created.add(child)
                participants.append({'relative_path':child,'before':{'kind':'absent'},'after':{'kind':'directory','mode':0o755}})
            elif result.get('state')=='present' and result.get('kind')=='directory' and type(result.get('object_tag')) is str and re.fullmatch(r'[0-9a-f]{64}',result['object_tag']):
                directories[child]=result['object_tag']
            else: raise ValueError('checkout parent unavailable')
            parent=child
        if path not in old and parent not in created:
            if await current() is not True: raise ValueError('checkout authority changed')
            for capability in ('read','write'):
                decision=repository.resolve(subject_id=context.owner_subject_id,is_admin=is_admin,origin='app',location_id=location_id,
                    capability=capability,workspace_id=context.workspace_id,relative_resource=path,chat_id=chat_id,group_ids=(),observation_only=True)
                if not decision.allowed or decision.policy_generation!=context.policy_generation: raise ValueError('checkout path unavailable')
            result=await probe_child(parent_path=os.path.join(root,parent),root_id=location_id,parent_object_tag=directories[parent],component=parts[-1])
            if type(result) is not dict or result.get('state')!='absent': raise ValueError('checkout target collision')
    for path in changed:
        if await current() is not True: raise ValueError('checkout authority changed')
        absolute=os.path.join(root,path)
        for capability in ('read','write'):
            decision=repository.resolve(subject_id=context.owner_subject_id,is_admin=is_admin,origin='app',location_id=location_id,
                capability=capability,workspace_id=context.workspace_id,relative_resource=path,chat_id=chat_id,group_ids=(),observation_only=True)
            if not decision.allowed or decision.policy_generation!=context.policy_generation: raise ValueError('checkout path unavailable')
        if not provider._path_writable(context,absolute,scope): raise ValueError('checkout write unavailable')
        previous,before_bytes=before.get(path,({'kind':'absent'},b''));target=new.get(path)
        if target is None: after={'kind':'absent'};after_bytes=b''
        else:
            after_bytes=await read_blob(target['oid'])
            if type(after_bytes) is not bytes or len(after_bytes)>MAX_ITEM: raise ValueError('checkout target unavailable')
            blob_hash=hashlib.sha1() if width==40 else hashlib.sha256()
            blob_hash.update(('blob %d\0'%len(after_bytes)).encode('ascii'));blob_hash.update(after_bytes)
            if blob_hash.hexdigest()!=target['oid']: raise ValueError('checkout target changed')
            _text(after_bytes);after_total+=len(after_bytes)
            if after_total>MAX_AFTER: raise ValueError('checkout target bound')
            mode=0o755 if target['mode']==0o100755 else 0o644
            if previous['kind']=='file' and previous['mode']!=mode: raise ValueError('checkout mode change unavailable')
            after={'kind':'file','sha256':hashlib.sha256(after_bytes).hexdigest(),'bytes':list(after_bytes),'mode':mode}
        participants.append({'relative_path':path,'before':previous,'after':after})
        before_text=_text(before_bytes);after_text=_text(after_bytes)
        lines_before=before_text.splitlines(keepends=True);lines_after=after_text.splitlines(keepends=True)
        diff=None
        if len(lines_before)+len(lines_after)<=2000:
            candidate=''.join(difflib.unified_diff(lines_before,lines_after,fromfile='before',tofile='after'))
            if len(candidate.encode('utf-8','strict'))<=16384: diff=candidate
        review.append({'path':path,'before':previous['kind'],'after':after['kind'],
            'beforeSHA256':previous.get('sha256'),'afterSHA256':after.get('sha256'),
            'beforeSize':previous.get('size',0),'afterSize':len(after_bytes),'diff':diff,'diffTruncated':diff is None,
            'requiresAbsenceCheck':previous['kind']=='absent'})
    if len(participants)>MAX_CHANGED: raise ValueError('checkout participant bound')
    if await current() is not True: raise ValueError('checkout authority changed')
    root_after=await probe_child(parent_path=root,root_id=location_id,parent_object_tag=root_revision['value'],component=None)
    if (type(root_after) is not dict or root_after.get('state')!='present' or root_after.get('kind')!='directory'
            or root_after.get('object_tag')!=root_revision['value']): raise ValueError('checkout root changed')
    plan={'version':1,'operation':expected_operation,'targetRef':expected_ref,'targetOID':preview['targetOID'],
        'source':source,'targetEntries':preview['targetEntries'],'changed':changed,'rootObjectTag':root_revision['value']}
    digest=hashlib.sha256(_compact(['openclank.git-checkout.plan.v1',plan])).hexdigest()
    return {'gitPlan':plan,'gitPlanDigest':digest,'rootObjectTag':root_revision['value'],'items':participants,'review':review}


def files_prepare_request(*, plan, account_id, operation_id, root_id, root_path):
    """Bind the exact Files journal plan to its authenticated History account."""
    if (type(plan) is not dict or set(plan)!={'gitPlan','gitPlanDigest','rootObjectTag','items','review'}
            or type(account_id) is not str or not account_id or type(root_id) is not str or not root_id
            or type(root_path) is not str or not os.path.isabs(root_path)
            or type(operation_id) is not str or not re.fullmatch(r'[A-Za-z0-9_-]{16,80}',operation_id)
            or type(plan['items']) is not list or not 1<=len(plan['items'])<=MAX_CHANGED
            or any(type(plan.get(k)) is not str or not re.fullmatch(r'[0-9a-f]{64}',plan[k]) for k in ('gitPlanDigest','rootObjectTag'))):
        raise ValueError('checkout Files request unavailable')
    items=json.loads(_compact(plan['items']))
    material=['openclank.git-checkout.files.v1',account_id,operation_id,root_id,plan['rootObjectTag'],plan['gitPlanDigest'],items]
    encoded=_compact(material)
    if len(encoded)>8*1024*1024-4096: raise ValueError('checkout Files request bound')
    digest=hashlib.sha256(encoded).hexdigest()
    return {'operation_id':operation_id,'request_digest':digest,'git_plan_digest':plan['gitPlanDigest'],
        'root_id':root_id,'root_path':root_path,'root_object_tag':plan['rootObjectTag'],'items':items}


async def verify_checkout_git_state(*, preview, candidate_sha256, event, read_index, read_git, current):
    """Observe the installed index and actual current branch for a phase boundary.

    Callbacks are already authorized managed-owner reads. Missing/truncated
    branch coverage refuses; this is an observation under Git's held locks.
    """
    try:
        if type(preview) is not dict or type(event) is not dict or await current() is not True: return False
        kind=event.get('kind');source=preview['source'];physical=source['physicalIndex']
        if kind=='before_ref':
            expected_index=candidate_sha256;expected_head=source['headOID'];expected_ref=source['headRef']
        elif kind=='finalize' and event.get('headOID')==preview['targetOID']:
            expected_index=candidate_sha256;expected_head=preview['targetOID']
            expected_ref=preview['targetRef'] if preview['operation']=='switch' else source['headRef']
        elif kind in ('failure','finalize'):
            expected_index=physical['sha256'];expected_head=source['headOID'];expected_ref=source['headRef']
        else: return False
        observed=await read_index()
        if (type(observed) is not dict or observed.get('sha256')!=expected_index
                or any(observed.get(k)!=physical.get(k) for k in ('objectFormat','root','git','objects'))): return False
        head=(await read_git('head')).decode('ascii','strict').strip()
        raw=await read_git('branches')
        if type(raw) is not bytes or len(raw)>262144: return False
        rows=raw.splitlines();active=[]
        if len(rows)>100: return False
        for row in rows:
            parts=row.split(b'\0')
            if len(parts)!=4 or parts[-1]!=b'' or parts[2] not in (b'*',b' '): return False
            ref=parts[0].decode('utf-8','strict');oid=parts[1].decode('ascii','strict')
            if not re.fullmatch(r'[0-9a-f]{40}(?:[0-9a-f]{24})?',oid): return False
            if parts[2]==b'*': active.append((ref,oid))
        return head==expected_head and active==[(expected_ref,expected_head)] and await current() is True
    except Exception:
        return False
