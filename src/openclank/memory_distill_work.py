"""Explicit repeated-pattern distillation from current, qualified sources.

Selectors are not authority: every root and tool part is independently
rechecked while the existing human source guards are held.
"""
import hashlib
import copy
import base64
import json
import time
from dataclasses import asdict

from src.memery_external_sources import SourceRef, _source_ref_error
from src.openclank.memory_job_admission import _completed_turn_thread
from src.openclank.s08_source_guards import held_s08_human_review_sources


def _validate_distill_sources(*, roots, occurrences, authority, archive, account):
    rows=[]
    for root,entry in zip(roots,occurrences):
        result=authority.execution_receipt(root)
        if result.get('state') != 'available': raise ValueError('source unavailable')
        identity=result['receipt']; group=[]; seen=set()
        for part in entry['parts']:
            if type(part) is not dict or set(part) != {'messageID','partID','revision','contentHash'}:
                raise ValueError('invalid part')
            key=(part['messageID'],part['partID'])
            if key in seen: raise ValueError('duplicate part')
            seen.add(key)
            row=archive.get_qualified_tool_pattern(account_id=account,chat_id=root.chat_id,actor_id='main',
                message_id=part['messageID'],part_id=part['partID'],revision=part['revision'],content_hash=part['contentHash'])
            if (not row or type(row.get('wallEndMS')) is not int or not int(time.time()*1000)-30*86400000 <= row['wallEndMS'] <= int(time.time()*1000) or any(row[k] != identity[k] for k in ('sessionID','rootOperationID','actorID','executionRevision'))):
                raise ValueError('tool source unavailable')
            group.append(row)
        if (any(type(item.get('eventSequence')) is not int or type(item.get('archiveSequence')) is not int
                or type(item.get('startUS')) is not int or type(item.get('endUS')) is not int
                or type(item.get('clockID')) is not str or not item['clockID'] for item in group)
            or len({item['runtimeGeneration'] for item in group}) != 1
            or len({item['clockID'] for item in group}) != 1
            or any(a['eventSequence'] >= b['eventSequence'] or a['archiveSequence'] >= b['archiveSequence']
                   or a['endUS'] > b['startUS'] for a,b in zip(group,group[1:]))):
            raise ValueError('tool order unavailable')
        rows.append(group)
    if len({group[0]['sessionID'] for group in rows}) != len(rows):
        raise ValueError('distinct native sessions required')
    if len({tuple((p['toolName'],p['argumentShapeSHA256']) for p in group) for group in rows}) != 1:
        raise ValueError('pattern not repeated')
    return rows


def _distill_context_excerpt(*, roots, rows, authority, archive, account):
    from src.shell_policy import redact_text
    def excerpt(value,maximum):
        # Redact the complete bounded input before taking an excerpt.
        redacted=redact_text(value)
        return redacted.encode('utf-8')[:maximum].decode('utf-8','ignore')
    contexts=[]
    for root,group in zip(roots,rows):
        body=authority.hydrate(root)
        if body.get('state')!='available' or type(body.get('content')) is not str: raise ValueError('source unavailable')
        text=body['content']
        user=excerpt(text,512) if len(text.encode('utf-8'))<=8192 else '[source too large for excerpt]'
        tools=[]
        for part in group:
            output=archive.get_qualified_tool_output(account_id=account,chat_id=root.chat_id,actor_id='main',
                message_id=part['messageID'],part_id=part['partID'],revision=part['revision'],content_hash=part['contentHash'],offset=0,limit=4096)
            if output is None: raise ValueError('tool source unavailable')
            value=excerpt(output['text'],128) if output['nextOffset'] is None else '[output too large for excerpt]'
            tools.append({'tool':part['toolName'],'argumentShape':part['argumentShape'],'outputExcerpt':value})
        contexts.append({'userExcerpt':user,'tools':tools})
    return contexts


def _distill_prompt(contexts):
    from src.teacher_escalation import _UNTRUSTED_TRACE_GUARD
    return ('Infer a possible reusable skill only if the evidence supports one. These bounded, best-effort redacted excerpts and repeated tool shapes do not prove success or a complete procedure. '
        'Return a fenced JSON object with name, description and procedure for a reviewable draft, or return null when evidence is insufficient.\n'
        +_UNTRUSTED_TRACE_GUARD+'\n<<<UNTRUSTED_TRACE>>>\n'+json.dumps(contexts,ensure_ascii=False,separators=(',',':'))+'\n<<<END_UNTRUSTED_TRACE>>>')


async def distill_selected(*, request, context, provider, occurrences):
    from core.database import SessionLocal
    from src.memery_external_sources import CurrentHostUserSource
    from src.openclank.conversation_archive import get_conversation_archive
    from src.openclank.memory_policy import STORE
    from src.teacher_escalation import _teacher_setting, _call_teacher, _extract_skill_json
    from src.tool_implementations import do_manage_skills

    if type(occurrences) is not list or not 2 <= len(occurrences) <= 8:
        return {'state':'unavailable'}
    roots=[]
    try:
        for entry in occurrences:
            if type(entry) is not dict or set(entry) != {'source','parts'} or type(entry['source']) is not dict:
                raise ValueError('invalid selection')
            root=SourceRef(**entry['source'])
            if (_source_ref_error(root) or root.account_id != context['account']
                    or root.actor_id != 'main' or root.revision != 1 or type(entry['parts']) is not list
                    or not 2 <= len(entry['parts']) <= 8):
                raise ValueError('invalid selection')
            roots.append(root)
        if len({root.chat_id for root in roots}) != len(roots) or len({root.message_id for root in roots}) != len(roots):
            raise ValueError('distinct sessions required')
    except (TypeError,ValueError):
        return {'state':'unavailable'}
    archive=get_conversation_archive()
    authority=CurrentHostUserSource(owner=context['user'],auth_manager=request.app.state.auth_manager,session_factory=SessionLocal,
        archive=archive,workspace_repository=request.app.state.files_policy_repository,policy_store=STORE,guarded_preparation=provider._guarded_preparation)
    current=context['current_write']
    try:
        async with held_s08_human_review_sources(request=request,user=context['user'],account=context['account'],
                provider=provider,sources=tuple(roots),current=current) as (_,validate):
            rows,cancelled=await _completed_turn_thread(lambda: _validate_distill_sources(roots=roots,occurrences=occurrences,authority=authority,archive=archive,account=context['account']))
            if cancelled: raise __import__('asyncio').CancelledError
            teacher=(_teacher_setting('teacher_model',context['user'],'') or '').strip()
            if not teacher or _teacher_setting('teacher_enabled',context['user'],False) is not True or not await validate():
                return {'state':'unavailable'}
            selector={'version':1,'sources':[{'chatID':r.chat_id,'rootMessageID':r.message_id,'rootHash':r.content_hash,
                'parts':entry['parts']} for r,entry in zip(roots,occurrences)]}
            selector_bytes=json.dumps(selector,sort_keys=True,separators=(',',':'),ensure_ascii=False).encode()
            if len(selector_bytes)>24576: return {'state':'unavailable'}
            selection=hashlib.sha256(selector_bytes).hexdigest()
            contexts,cancelled=await _completed_turn_thread(lambda: _distill_context_excerpt(roots=roots,rows=rows,authority=authority,archive=archive,account=context['account']))
            if cancelled: raise __import__('asyncio').CancelledError
            prompt=_distill_prompt(contexts)
            if len(prompt.encode('utf-8'))>32768: return {'state':'unavailable'}
            before,cancelled=await _completed_turn_thread(lambda: _validate_distill_sources(roots=roots,occurrences=occurrences,authority=authority,archive=archive,account=context['account']))
            if cancelled: raise __import__('asyncio').CancelledError
            if before != rows or not await validate(): return {'state':'unavailable'}
            response=await _call_teacher(teacher,prompt,owner=context['user'],root_operation_id=roots[0].message_id+'-distill-'+selection)
            if response is None: return {'state':'unavailable'}
            if not await validate() or _teacher_setting('teacher_enabled',context['user'],False) is not True or (_teacher_setting('teacher_model',context['user'],'') or '').strip() != teacher:
                return {'state':'unavailable'}
            again,cancelled=await _completed_turn_thread(lambda: _validate_distill_sources(roots=roots,occurrences=occurrences,authority=authority,archive=archive,account=context['account']))
            if cancelled: raise __import__('asyncio').CancelledError
            if again != rows: return {'state':'unavailable'}
            skill=_extract_skill_json(response)
            if skill is None: return {'state':'no_draft'}
            skill['action']='add';skill['status']='draft';skill['source']='repeated-workflow-inference';skill['teacher_model']=teacher
            skill['source_uri']='data:application/vnd.openclank.conversation-selection+json;base64,'+base64.b64encode(selector_bytes).decode('ascii')
            skill['source_revision']=selection
            if not await validate(): return {'state':'unavailable'}
            result=await do_manage_skills(json.dumps(skill,ensure_ascii=False),owner=context['user'])
            return {'state':'processed' if isinstance(result,dict) and not result.get('error') else 'unavailable','result':result}
    except (ValueError,KeyError,TypeError):
        return {'state':'unavailable'}


def _discover_distill_groups(*, chat_ids, account, user, workspace, archive, authority, since):
    from core.database import SessionLocal
    from sqlalchemy import text
    result=[]
    for chat in chat_ids:
        db=SessionLocal()
        try:
            roots=db.execute(text("""SELECT i.message_id,i.content_hash,s.workspace_id FROM host_source_delivery_intent i
                JOIN sessions s ON s.id=i.chat_id WHERE i.owner=:account AND i.chat_id=:chat AND s.owner=:user
                AND i.part_id='body' AND i.revision=1 AND i.origin_completed=1
                AND i.origin_state IN ('pending','delivered','projection_user_dropped','projection_assistant_dropped','projection_both_dropped')
                ORDER BY i.message_id DESC LIMIT 8"""),{'account':account,'chat':chat,'user':user}).all()
        finally: db.close()
        candidates=archive.list_qualified_tool_candidates(account_id=account,chat_id=chat,since_ms=since)
        groups=[]
        for message,digest,root_workspace in roots:
            if workspace is not None and root_workspace != workspace: continue
            ref=SourceRef(1,'host_user_body',account,chat,'main',message,'body',1,digest,root_workspace,None)
            if _source_ref_error(ref): continue
            receipt=authority.execution_receipt(ref)
            if receipt.get('state')!='available': continue
            identity=receipt['receipt']; parts=[]; shapes=[]; observed=[]
            for candidate in candidates:
                item=archive.get_qualified_tool_pattern(account_id=account,chat_id=chat,actor_id='main',
                    message_id=candidate['message_id'],part_id=candidate['part_id'],revision=candidate['revision'],content_hash=candidate['content_hash'])
                if (not item or type(item.get('wallEndMS')) is not int or not since<=item['wallEndMS']<=int(time.time()*1000)
                    or any(item[k]!=identity[k] for k in ('sessionID','rootOperationID','actorID','executionRevision'))): continue
                parts.append({'messageID':candidate['message_id'],'partID':candidate['part_id'],'revision':candidate['revision'],'contentHash':candidate['content_hash']})
                shapes.append((item['toolName'],item['argumentShapeSHA256']))
                observed.append(item)
                if len(parts)==8: break
            if (len(parts)>=2 and all(type(i.get('eventSequence')) is int and type(i.get('archiveSequence')) is int
                    and type(i.get('startUS')) is int and type(i.get('endUS')) is int
                    and type(i.get('clockID')) is str and i['clockID'] for i in observed)
                and len({i['runtimeGeneration'] for i in observed})==1 and len({i['clockID'] for i in observed})==1
                and all(a['eventSequence']<b['eventSequence'] and a['archiveSequence']<b['archiveSequence']
                        and a['endUS']<=b['startUS'] for a,b in zip(observed,observed[1:]))):
                groups.append((ref,parts,tuple(shapes),identity['sessionID']))
        result.append(groups)
    return result


def _choose_distill_groups(groups):
    signatures={}
    for group in groups:
        for root,parts,shape,session_id in group:
            bucket=signatures.setdefault(shape,{})
            bucket.setdefault(root.chat_id,(root,parts,session_id))
    for bucket in signatures.values():
        distinct=[]; native_sessions=set()
        for root,parts,session_id in bucket.values():
            if session_id in native_sessions: continue
            native_sessions.add(session_id);distinct.append((root,parts))
        if len(distinct)>=2: return distinct[:8]
    return None


async def preview_selected_chats(*, request, context, provider, chat_ids):
    """Return only a bounded, revalidatable selector, never source content."""
    from core.database import SessionLocal
    from sqlalchemy import text
    from src.memery_external_sources import CurrentHostUserSource
    from src.openclank.conversation_archive import get_conversation_archive
    from src.openclank.memory_policy import STORE
    if (type(chat_ids) is not list or not 2 <= len(chat_ids) <= 8 or
        any(type(c) is not str or not c or c!=c.strip() or len(c.encode('utf-8'))>256 for c in chat_ids) or
        len(set(chat_ids))!=len(chat_ids)):
        return {'state':'unavailable','reason':'invalid_selection'}
    archive=get_conversation_archive()
    authority=CurrentHostUserSource(owner=context['user'],auth_manager=request.app.state.auth_manager,session_factory=SessionLocal,
        archive=archive,workspace_repository=request.app.state.files_policy_repository,policy_store=STORE,guarded_preparation=provider._guarded_preparation)
    since=int(time.time()*1000)-30*86400000
    try:
        groups,cancelled=await _completed_turn_thread(lambda: _discover_distill_groups(chat_ids=chat_ids,account=context['account'],user=context['user'],workspace=None,archive=archive,authority=authority,since=since))
        if cancelled: raise __import__('asyncio').CancelledError
        chosen=_choose_distill_groups(groups)
        if not chosen: return {'state':'no_match','complete':False,'reason':'bounded_or_unavailable_sources'}
        roots=tuple(item[0] for item in chosen)
        async with held_s08_human_review_sources(request=request,user=context['user'],account=context['account'],
                provider=provider,sources=roots,current=context['current_write']) as (_,validate):
            if not await validate(): return {'state':'unavailable','reason':'source_changed'}
            return {'state':'available','complete':False,'reason':'bounded_selection',
                'occurrences':[{'source':asdict(root),'parts':parts} for root,parts in chosen]}
    except Exception:
        return {'state':'unavailable','reason':'source_unavailable'}


def automatic_selection_identity(occurrences):
    """Canonical private selector identity; admission still requires live source guards."""
    import re
    if type(occurrences) is not list or not 2<=len(occurrences)<=8: raise ValueError('invalid selection')
    chats=set()
    for entry in occurrences:
        if type(entry) is not dict or set(entry)!={'source','parts'} or type(entry['source']) is not dict:
            raise ValueError('invalid selection')
        root=SourceRef(**entry['source'])
        if _source_ref_error(root) or root.chat_id in chats or type(entry['parts']) is not list or not 2<=len(entry['parts'])<=8:
            raise ValueError('invalid selection')
        chats.add(root.chat_id);seen=set()
        for part in entry['parts']:
            if type(part) is not dict or set(part)!={'messageID','partID','revision','contentHash'}:
                raise ValueError('invalid selection')
            message,part_id,revision,digest=part['messageID'],part['partID'],part['revision'],part['contentHash']
            if (type(message) is not str or not message or len(message.encode('utf-8'))>512
                    or type(part_id) is not str or not part_id or len(part_id.encode('utf-8'))>512
                    or type(revision) is not int or revision<1 or type(digest) is not str
                    or re.fullmatch(r'[0-9a-f]{64}',digest) is None or (message,part_id) in seen):
                raise ValueError('invalid selection')
            seen.add((message,part_id))
    raw=json.dumps({'version':1,'ordered_sources':occurrences},sort_keys=True,ensure_ascii=False,
        separators=(',',':'),allow_nan=False).encode('utf-8')
    if len(raw)>24576: raise ValueError('selection too large')
    return hashlib.sha256(raw).hexdigest()


def automatic_skill_operation_id(*, account_id, workspace_id, selection_sha256):
    import re
    if (any(type(value) is not str or not value or value!=value.strip() or len(value.encode('utf-8'))>512
            for value in (account_id,workspace_id)) or type(selection_sha256) is not str
            or re.fullmatch(r'[0-9a-f]{64}',selection_sha256) is None):
        raise ValueError('automatic operation identity unavailable')
    raw=json.dumps(['openclank.skill-draft.operation.v1',account_id,workspace_id,selection_sha256],
        ensure_ascii=False,separators=(',',':'),allow_nan=False).encode('utf-8')
    return 'skilldraft_'+hashlib.sha256(raw).hexdigest()


def automatic_skill_policy_snapshot(*, lifecycle, assignment):
    """Capture the existing accepted owner policy and configured teacher for later exact recheck."""
    from routes.prefs_routes import PREFERENCE_LOCK, _load_for_user
    from src.openclank.chat_source_bridge import CompletedTurnLifecycleOwner
    from src.openclank.memory_job_admission import AssignmentAuthority, _completed_turn_limits
    from src.openclank.memory_policy_projection import exact_json, policy_coordinator, preference_inputs
    from src.openclank.chat_routing import resolve_chat_model_spec
    from src.teacher_escalation import _teacher_setting
    if (type(lifecycle) is not CompletedTurnLifecycleOwner or type(assignment) is not AssignmentAuthority
            or assignment.owner!=lifecycle.owner): raise ValueError('automatic policy unavailable')
    lifecycle.profile.assert_current()
    base,limits=_completed_turn_limits(lifecycle.target_authority,assignment)
    coordinator=policy_coordinator()
    if coordinator is None: raise ValueError('automatic policy unavailable')
    refs=coordinator.checked_refs(lifecycle.owner,lifecycle.account_id,lifecycle.binding.memory_workspace)
    with PREFERENCE_LOCK:
        prefs=_load_for_user(lifecycle.owner)
        agent=prefs.get('agent_settings') if type(prefs) is dict else None
        distill=agent.get('distill') if type(agent) is dict else None
        if (type(prefs.get('_skill_draft_policy_version')) is not int or prefs['_skill_draft_policy_version']!=1
                or type(distill) is not dict or distill.get('auto') is not True):
            raise ValueError('automatic distillation disabled')
        interval=distill.get('interval_days',30)
        if type(interval) is not int or not 0<=interval<=365:
            raise ValueError('automatic interval unavailable')
        raw=exact_json({'preferences':preference_inputs(prefs),'can_manage_memory':True})
    if len(raw.encode('utf-8'))>65536 or hashlib.sha256(raw.encode('utf-8')).hexdigest()!=refs['account']['fingerprint']:
        raise ValueError('automatic policy changed')
    if _teacher_setting('teacher_enabled',lifecycle.owner,False) is not True:
        raise ValueError('teacher unavailable')
    spec=(_teacher_setting('teacher_model',lifecycle.owner,'') or '').strip()
    if not spec: raise ValueError('teacher unavailable')
    route=resolve_chat_model_spec(owner=lifecycle.owner,model_spec=spec)
    selected={'version':1,'connection_id':route.connection_id,'model_route_id':route.model_route_id,
        'provider_grant_id':route.provider_grant_id,'teacher_spec_sha256':hashlib.sha256(spec.encode('utf-8')).hexdigest()}
    selected_raw=exact_json(selected).encode('utf-8')
    if (not route.connection_id or not route.model_route_id or len(selected_raw)>4096
            or _teacher_setting('teacher_enabled',lifecycle.owner,False) is not True
            or (_teacher_setting('teacher_model',lifecycle.owner,'') or '').strip()!=spec
            or coordinator.checked_refs(lifecycle.owner,lifecycle.account_id,lifecycle.binding.memory_workspace)!=refs):
        raise ValueError('automatic policy changed')
    lifecycle.profile.assert_current()
    return {'account_policy_input_json':raw,'policy_projection':refs,'policy_fingerprint':base['fingerprint'],
        'target_fingerprint':lifecycle.binding.fingerprint,'limits':limits,'assignment':selected,
        'assignment_sha256':hashlib.sha256(selected_raw).hexdigest(),'teacher_spec':spec}


def automatic_skill_original(*, lifecycle, occurrences, snapshot, prompt):
    """Build the bounded immutable S05 original; a retained original does not grant dispatch."""
    from src.openclank.chat_source_bridge import CompletedTurnLifecycleOwner
    from src.agent_tools.model_interaction_tools import _TEACHER_SYSTEM_PROMPT
    if type(lifecycle) is not CompletedTurnLifecycleOwner or type(snapshot) is not dict or type(prompt) is not str:
        raise ValueError('automatic original unavailable')
    selection=automatic_selection_identity(occurrences)
    operation=automatic_skill_operation_id(account_id=lifecycle.account_id,
        workspace_id=lifecycle.binding.memory_workspace,selection_sha256=selection)
    selected=snapshot['assignment'];limits=snapshot['limits']
    if type(selected) is not dict or len(prompt.encode('utf-8'))>32768:
        raise ValueError('automatic original unavailable')
    messages=[{'role':'system','content':_TEACHER_SYSTEM_PROMPT},{'role':'user','content':prompt}]
    messages_raw=json.dumps(messages,sort_keys=True,ensure_ascii=False,separators=(',',':'),allow_nan=False).encode('utf-8')
    prompt_sha256=hashlib.sha256(messages_raw).hexdigest()
    # UTF-8 byte count plus fixed framing is a conservative provider input bound.
    input_bound=len(_TEACHER_SYSTEM_PROMPT.encode('utf-8'))+len(prompt.encode('utf-8'))+256
    output_bound=min(limits.job_output_tokens,16384)
    if not 0<input_bound<=1000000 or not 1<=output_bound<=16384:
        raise ValueError('automatic original unavailable')
    return copy.deepcopy({'version':1,'owner':lifecycle.binding.memory_owner,'account_id':lifecycle.account_id,
        'workspace_id':lifecycle.binding.memory_workspace,'operation_id':operation,'ordered_sources':occurrences,
        'selection_sha256':selection,'prompt_sha256':prompt_sha256,'policy_projection':snapshot['policy_projection'],
        'account_policy_input_json':snapshot['account_policy_input_json'],
        'target_fingerprint':snapshot['target_fingerprint'],'policy_fingerprint':snapshot['policy_fingerprint'],
        'assignment':selected,'assignment_sha256':snapshot['assignment_sha256'],
        'connection_id':selected['connection_id'],'model_route_id':selected['model_route_id'],
        'max_input_tokens':input_bound,'max_output_tokens':output_bound,
        'daily_calls':limits.daily_calls,'daily_tokens':limits.daily_tokens})


from contextlib import asynccontextmanager


@asynccontextmanager
async def held_automatic_skill_sources(*, lifecycle, assignment, occurrences, selection_sha256, current):
    """Retain the actual automatic source boundary; a selector alone grants nothing."""
    from src.openclank.chat_source_bridge import CompletedTurnLifecycleOwner
    from src.openclank.s08_source_guards import held_s08_automatic_sources
    if (type(lifecycle) is not CompletedTurnLifecycleOwner or not callable(current)
            or automatic_selection_identity(occurrences)!=selection_sha256):
        raise ValueError('automatic selection unavailable')
    roots=tuple(SourceRef(**entry['source']) for entry in occurrences)
    source_workspace=roots[0].workspace_id
    if (type(source_workspace) is not str or not source_workspace
            or any(root.account_id!=lifecycle.account_id or root.workspace_id!=source_workspace
                   or root.actor_id!='main' or root.revision!=1 for root in roots)):
        raise ValueError('automatic selection unavailable')
    archive=lifecycle.source_authority._archive; authority=lifecycle.source_authority
    async with held_s08_automatic_sources(lifecycle=lifecycle,sources=roots,current=current) as (guards,source_current):
        async def read_rows():
            rows,cancelled=await _completed_turn_thread(lambda: _validate_distill_sources(
                roots=roots,occurrences=occurrences,authority=authority,archive=archive,account=lifecycle.account_id))
            if cancelled: raise __import__('asyncio').CancelledError
            return rows
        rows=await read_rows()
        async def validate():
            if automatic_selection_identity(occurrences)!=selection_sha256 or not await source_current(): return False
            try:
                return await read_rows()==rows and await source_current()
            except (ValueError,KeyError,TypeError):
                return False
        if not await validate(): raise ValueError('automatic selection changed')
        from src.openclank.chat_source_bridge import mint_review_currentness
        def full_check():
            if current() is not True or automatic_selection_identity(occurrences)!=selection_sha256:
                raise ValueError('automatic selection changed')
            for root in roots:
                lifecycle.for_source(source=root,guard=guards[root.chat_id])
            fresh=_validate_distill_sources(roots=roots,occurrences=occurrences,authority=authority,
                archive=archive,account=lifecycle.account_id)
            if fresh!=rows or current() is not True: raise ValueError('automatic selection changed')
        (witness,_),cancelled=await _completed_turn_thread(lambda: mint_review_currentness(
            lifecycle_owner=lifecycle,sources=tuple((root,guards[root.chat_id]) for root in roots),
            assignment=assignment,validate=full_check))
        if cancelled:
            witness.close()
            raise __import__('asyncio').CancelledError
        newest=max(part['wallEndMS'] for group in rows for part in group)
        expiry=min(part['wallEndMS']+30*86400000 for group in rows for part in group)
        def assert_preframe():
            now=int(time.time()*1000)
            if now<newest or now>expiry: raise ValueError('automatic source window changed')
            witness.assert_current()
        try:
            yield roots,rows,validate,assert_preframe,witness,guards
        finally:
            witness.close()


@asynccontextmanager
async def prepare_automatic_skill_draft(*, lifecycle, assignment, occurrences, selection_sha256, current):
    """Prepare one original under real source guards; issuing and settlement have separate owners."""
    snapshot,cancelled=await _completed_turn_thread(lambda: automatic_skill_policy_snapshot(lifecycle=lifecycle,assignment=assignment))
    if cancelled: raise __import__('asyncio').CancelledError
    async with held_automatic_skill_sources(lifecycle=lifecycle,assignment=assignment,occurrences=occurrences,
            selection_sha256=selection_sha256,current=current) as (roots,rows,validate,assert_preframe,witness,guards):
        contexts,cancelled=await _completed_turn_thread(lambda: _distill_context_excerpt(roots=roots,rows=rows,
            authority=lifecycle.source_authority,archive=lifecycle.source_authority._archive,account=lifecycle.account_id))
        if cancelled: raise __import__('asyncio').CancelledError
        prompt=_distill_prompt(contexts)
        original=automatic_skill_original(lifecycle=lifecycle,occurrences=occurrences,snapshot=snapshot,prompt=prompt)
        again,cancelled=await _completed_turn_thread(lambda: automatic_skill_policy_snapshot(lifecycle=lifecycle,assignment=assignment))
        if cancelled: raise __import__('asyncio').CancelledError
        if again!=snapshot or not await validate(): raise ValueError('automatic operation changed')
        assert_preframe()
        yield {'original':original,'teacher_spec':snapshot['teacher_spec'],'prompt':prompt,
            'limits':snapshot['limits'],'validate':validate,'assert_preframe':assert_preframe,
            'currentness':witness,'guards':guards}


async def preview_automatic_sources(*, lifecycle, source_workspace_id, current):
    """Select one bounded repeat using the admitted maintenance lifecycle; this does not issue work."""
    from core.database import SessionLocal
    from sqlalchemy import text
    from src.openclank.chat_source_bridge import CompletedTurnLifecycleOwner
    from src.openclank.s08_source_guards import held_s08_automatic_sources
    if (type(lifecycle) is not CompletedTurnLifecycleOwner or not callable(current)
            or type(source_workspace_id) is not str or not source_workspace_id or source_workspace_id!=source_workspace_id.strip()):
        return {'state':'unavailable'}
    account=lifecycle.account_id; user=lifecycle.owner; workspace=source_workspace_id
    archive=lifecycle.source_authority._archive; authority=lifecycle.source_authority
    since=int(time.time()*1000)-30*86400000
    def discover():
        from datetime import datetime, timezone
        cutoff=datetime.fromtimestamp(since/1000,timezone.utc).replace(tzinfo=None)
        db=SessionLocal()
        try:
            chats=[row[0] for row in db.execute(text("""SELECT id FROM sessions WHERE owner=:user AND workspace_id=:workspace
                AND last_message_at>=:cutoff ORDER BY last_message_at DESC,id LIMIT 32"""),
                {'user':user,'workspace':workspace,'cutoff':cutoff}).all()]
        finally: db.close()
        if len(chats)<2: return None
        return _choose_distill_groups(_discover_distill_groups(chat_ids=chats,account=account,user=user,
            workspace=workspace,archive=archive,authority=authority,since=since))
    try:
        if current() is not True: return {'state':'unavailable'}
        chosen,cancelled=await _completed_turn_thread(discover)
        if cancelled: raise __import__('asyncio').CancelledError
        if not chosen: return {'state':'no_match','complete':False}
        roots=tuple(item[0] for item in chosen)
        async with held_s08_automatic_sources(lifecycle=lifecycle,sources=roots,current=current) as (_,validate):
            if not await validate(): return {'state':'unavailable'}
            occurrences=[{'source':asdict(root),'parts':parts} for root,parts in chosen]
            selection_sha256=automatic_selection_identity(occurrences)
            return {'state':'available','complete':False,'occurrences':occurrences,'selection_sha256':selection_sha256,
                'operation_id':automatic_skill_operation_id(account_id=account,workspace_id=lifecycle.binding.memory_workspace,
                    selection_sha256=selection_sha256)}
    except Exception:
        return {'state':'unavailable'}


async def discover_automatic_source_workspaces(*, lifecycle, current):
    """Return bounded owner-chat workspace hints; each source still needs live admission."""
    from datetime import datetime, timezone
    from core.database import SessionLocal
    from sqlalchemy import text
    from src.openclank.chat_source_bridge import CompletedTurnLifecycleOwner
    if type(lifecycle) is not CompletedTurnLifecycleOwner or not callable(current):
        return {'state': 'unavailable'}
    cutoff = datetime.fromtimestamp(time.time() - 30 * 86400, timezone.utc).replace(tzinfo=None)
    def read():
        if current() is not True:
            raise ValueError('automatic source owner changed')
        lifecycle.profile.assert_current()
        if (lifecycle.target_authority.resolve(lifecycle.account_id) != lifecycle.binding
                or lifecycle.account_lifecycle.owner_has_active_operation(lifecycle.owner)):
            raise ValueError('automatic source owner changed')
        db = SessionLocal()
        try:
            rows = db.execute(text('''SELECT workspace_id FROM sessions
                WHERE owner=:owner AND workspace_id IS NOT NULL AND last_message_at>=:cutoff
                ORDER BY last_message_at DESC,id LIMIT 33'''),
                {'owner': lifecycle.owner, 'cutoff': cutoff}).all()
        finally:
            db.close()
        if current() is not True:
            raise ValueError('automatic source owner changed')
        lifecycle.profile.assert_current()
        if (lifecycle.target_authority.resolve(lifecycle.account_id) != lifecycle.binding
                or lifecycle.account_lifecycle.owner_has_active_operation(lifecycle.owner)):
            raise ValueError('automatic source owner changed')
        values = list(dict.fromkeys(row[0] for row in rows))
        if any(type(value) is not str or not value or value != value.strip()
               or len(value.encode('utf-8')) > 512 for value in values):
            raise ValueError('automatic source workspace unavailable')
        return {'state': 'available', 'workspaceIDs': values[:32], 'complete': False}
    try:
        result, cancelled = await _completed_turn_thread(read)
        if cancelled:
            raise __import__('asyncio').CancelledError
        return result
    except Exception:
        return {'state': 'unavailable'}
