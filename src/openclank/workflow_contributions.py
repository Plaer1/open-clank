"""Bounded evidence from the canonical, admitted workflow progress producer."""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import time

MAX_ATTEMPTS = 64
MAX_PAGE = 20
MAX_SOURCE_BYTES = 262144
PUBLIC_STATES = {'reported', 'committed', 'not_applied', 'conflict', 'unknown'}
IDENTITY_KEYS = ('sessionID','rootOperationID','actorID','executionRevision','innerTurnID','toolCallID','messageID')


def _text(value):
    return isinstance(value, str) and 0 < len(value.encode('utf-8')) <= 256 and not any(ord(ch)<32 for ch in value)


def _task(ref, identity):
    if (type(ref) is not dict or set(ref) != {'kind','sessionID','taskID','assignmentEventID'}
            or ref['kind'] != 'native-task' or ref['sessionID'] != identity['sessionID']
            or not _text(ref['sessionID']) or not isinstance(ref['taskID'], str) or len(ref['taskID'])>64
            or not re.fullmatch(r'T[0-9]+(?:\.[0-9]+)*',ref['taskID'])
            or type(ref['assignmentEventID']) is not int or not 0 < ref['assignmentEventID'] <= 9007199254740991):
        raise ValueError('unsupported task reference')
    return dict(ref)


def _digest(value):
    return hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode('utf-8')).hexdigest()


def _revision(value):
    return (type(value) is dict and set(value)=={'kind','value'} and value['kind']=='hostFingerprint'
            and isinstance(value['value'],str) and re.fullmatch(r'sha256:[a-f0-9]{64}:(?:0|[1-9][0-9]{0,5})',value['value']) is not None)


def record_progress(chat, *, owner, account, workspace, task_ref, source, operation, current, outcome=None):
    """Join only the source-token-proved task assignment and exact report call.

    The original writer gates both stages. The observation never authorizes a
    write, and an interrupted reporting attempt remains explicitly unsettled.
    """
    from src.openclank.transcript_projection import mutate_plan_state
    if type(source) is not dict or set(source) != set(IDENTITY_KEYS) or not callable(current):
        raise ValueError('invalid contribution source')
    if (any(not _text(source[key]) for key in IDENTITY_KEYS if key!='executionRevision')
            or type(source['executionRevision']) is not int or not 0<=source['executionRevision']<=9007199254740991
            or not isinstance(operation, str) or re.fullmatch(r'workflowop_[a-f0-9]{64}',operation) is None):
        raise ValueError('invalid contribution source')
    task = _task(task_ref, source)
    expected = ['openclank.workflow.operation.v1',account,source['sessionID'],source['rootOperationID'],source['actorID'],source['executionRevision'],source['innerTurnID'],source['toolCallID']]
    digest = hashlib.sha256(json.dumps(expected,ensure_ascii=False,separators=(',',':')).encode('utf-8')).hexdigest()
    if operation != 'workflowop_'+digest:
        raise ValueError('contribution operation changed')
    fixed = {'account':account,'workspace':workspace,'task':task,'source':dict(source),'operation':operation}
    seen = int(time.time()*1000)

    def apply(plan):
        assoc=plan.get('artifact_association')
        if (current() is not True or plan.get('status')=='cleared' or not isinstance(assoc,dict)
                or assoc.get('account_id') != account or assoc.get('workspace_id') != workspace):
            raise ValueError('contribution plan changed')
        ledger = plan.get('progress_contributions', [])
        if not isinstance(ledger, list) or len(ledger)>MAX_ATTEMPTS or any(not isinstance(row,dict) for row in ledger):
            raise ValueError('contribution ledger unavailable')
        rows = list(ledger)
        matches = [index for index,item in enumerate(rows) if item.get('operation') == operation]
        if len(matches)>1:
            raise ValueError('duplicate contribution attempt')
        if not matches:
            if outcome is not None:
                return plan
            record = {**fixed,'observedAt':seen,'state':'reported'}
            if len(rows) == MAX_ATTEMPTS:
                rows.pop(0)
            rows.append(record)
        else:
            index=matches[0]; row=rows[index]
            if any(row.get(key)!=value for key,value in fixed.items()):
                raise ValueError('contribution source changed')
            if outcome is None or row.get('state') in {'committed','not_applied','conflict'}:
                return plan
            state=outcome.get('state')
            version=outcome.get('revision')
            if outcome.get('operationID')!=operation:
                state='unknown'
            if state=='complete' and _revision(version):
                rows[index]={**row,'state':'committed','sourceRevision':dict(version)}
            else:
                rows[index]={**row,'state':state if state in {'not_applied','conflict'} else 'unknown'}
        return {**plan,'progress_contributions':rows}

    mutate_plan_state(chat,owner,apply,current_authority=current)


async def qualified_progress(*, owner, account, workspace, chat, repository, context, facade, root, current):
    """Read current scoped evidence for historic report attempts without inference."""
    from src.openclank.transcript_projection import get_mimo_state
    from src.openclank.workflow_memory_links import read_current_member
    from src.plan_approval import workflow_member_path

    async def snapshot():
        plan=(await asyncio.to_thread(get_mimo_state,chat,owner=owner)).get('plan_state')
        return plan if isinstance(plan,dict) else {}

    plan=await snapshot()
    ledger=plan.get('progress_contributions',[])
    if not isinstance(ledger,list) or len(ledger)>MAX_ATTEMPTS or not await current():
        raise ValueError('contribution scope changed')
    assoc=plan.get('artifact_association')
    if not isinstance(assoc,dict) or assoc.get('account_id')!=account or assoc.get('workspace_id')!=workspace:
        assoc={}
    members=assoc.get('members')
    members=members if isinstance(members,dict) else {}
    meta=members.get('metaplan')
    host=facade._providers.get('host')
    if host is None or host.operation_store is not repository:
        raise ValueError('Files authority changed')

    rows=[]
    remaining=MAX_SOURCE_BYTES
    for record in reversed(ledger):
        if len(rows)>=MAX_PAGE:
            break
        if (not isinstance(record,dict) or record.get('account')!=account or record.get('workspace')!=workspace
                or record.get('state') not in PUBLIC_STATES or type(record.get('observedAt')) is not int
                or not _text(record.get('operation')) or not isinstance(record.get('source'),dict)):
            continue
        source=record['source']
        try:
            ref=_task(record.get('task'),source)
            if (set(source)!=set(IDENTITY_KEYS) or any(not _text(source[key]) for key in IDENTITY_KEYS if key!='executionRevision')
                    or type(source['executionRevision']) is not int):
                continue
        except (KeyError,TypeError,ValueError):
            continue
        task_ref={key:ref[key] for key in ('sessionID','taskID','assignmentEventID')}
        item={'taskRef':task_ref,**source,'operationID':record['operation'],'state':record['state'],
              'observedAt':record['observedAt'],'sourceAvailable':False,'witnessSHA256':_digest(record)}
        version=record.get('sourceRevision')
        if record['state']=='committed' and _revision(version) and remaining>0:
            # The task name comes from the original native assignment and is
            # only a selector into the authenticated, current plan association.
            material=['openclank.workflow.task-ref.v1',ref['sessionID'],ref['taskID'],ref['assignmentEventID']]
            name='task-'+hashlib.sha256(json.dumps(material,ensure_ascii=False,separators=(',',':')).encode('utf-8')).hexdigest()
            member=members.get('progress:'+name)
            try:
                if (not isinstance(meta,dict) or not isinstance(member,dict) or member.get('name') != name
                        or member.get('task_ref') != ref or member.get('role') != 'progress' or member.get('revision') != version
                        or member.get('relative_path') != workflow_member_path(meta.get('relative_path'),'progress',name)):
                    raise ValueError('source version unavailable')
                resource,actual,content,path=await read_current_member(host=host,facade=facade,files=context,root=root,
                    member=member,current=current,max_bytes=remaining)
                if os.path.commonpath((root,path)) == root and actual == version:
                    remaining-=len(content)
                    item.update(sourceAvailable=True,source={'resourceRef':resource['ref'],'revision':actual})
            except (ValueError,OSError,TypeError):
                pass
            except Exception:
                if not await current():
                    raise ValueError('contribution scope changed')
        rows.append(item)
    new=await snapshot()
    if (new.get('progress_contributions',[])!=ledger or new.get('artifact_association')!=plan.get('artifact_association')
            or not await current()):
        raise ValueError('contribution scope changed')
    return {'version':1,'coverage':'partial_task_progress','items':rows}
