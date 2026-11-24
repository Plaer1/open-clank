"""Human review of exact admitted recall; no rating or outcome store."""
from __future__ import annotations
import asyncio
import base64
import hashlib
import hmac
import json
import re
from dataclasses import asdict


class RecallFeedbackUnavailable(ValueError): pass


def _assessment_key(account, item, polarity):
    kind='tool' if item['signal']=='tool_admitted' else 'context'
    memory=item['memory']
    raw=json.dumps(['openclank.s06.helped.v1',account,kind,item['event_id'],memory['version'],memory['provider'],
                    memory['owner'],memory['workspace_id'],memory['project_id'],memory['block_id'],memory['revision'],
                    memory['value_sha256'],polarity],separators=(',',':'),ensure_ascii=False).encode()
    return hashlib.sha256(raw).hexdigest()


def _selector(payload):
    from src.secret_storage import keyed_digest
    raw=json.dumps(payload,sort_keys=True,separators=(',',':'),ensure_ascii=False)
    mac=keyed_digest(raw,context='openclank.s06.helped-selector.v1')
    return base64.urlsafe_b64encode(json.dumps({'payload':payload,'mac':mac},separators=(',',':'),ensure_ascii=False).encode()).decode().rstrip('=')


def _read_selector(token):
    from src.secret_storage import keyed_digest
    if type(token) is not str or not 1<=len(token)<=16384 or not re.fullmatch(r'[A-Za-z0-9_-]+',token):
        raise RecallFeedbackUnavailable('selector unavailable')
    try:
        envelope=json.loads(base64.urlsafe_b64decode(token+'='*(-len(token)%4)))
        if type(envelope) is not dict or set(envelope)!={'payload','mac'} or type(envelope['payload']) is not dict or type(envelope['mac']) is not str:
            raise ValueError()
        raw=json.dumps(envelope['payload'],sort_keys=True,separators=(',',':'),ensure_ascii=False)
        if not hmac.compare_digest(envelope['mac'],keyed_digest(raw,context='openclank.s06.helped-selector.v1')): raise ValueError()
        return envelope['payload']
    except Exception as exc: raise RecallFeedbackUnavailable('selector unavailable') from exc


async def review(request, *, owner, account, chat_id, message_id, body, action, session_manager, session_factory, auth, barrier, verify_owner):
    from src.openclank.chat_ratings import _current
    from src.openclank.chat_source_bridge import canonical_source_provider
    from src.openclank.conversation_archive import get_conversation_archive
    from src.openclank.memory_job_admission import TargetAuthority
    from src.openclank.memory_policy import STORE
    from src.openclank.mimo_supervisor import _queued_owned_thread
    from src.openclank.session_map import OwnerSessionMap
    from src.memery_external_sources import SourceRef, CurrentHostUserSource
    from src.memery_provider import _HostObservationProjection
    from app import _s06_admitted_ready, _owner_request_fenced

    expected={'expected_source_revision','expected_source_hash'}
    if action != 'preview': expected |= {'selector','polarity','request_key'}
    if (type(body) is not dict or set(body)!=expected or body.get('expected_source_revision')!=1
            or type(body.get('expected_source_revision')) is not int or type(body.get('expected_source_hash')) is not str
            or not re.fullmatch(r'[0-9a-f]{64}',body['expected_source_hash'])):
        raise RecallFeedbackUnavailable('feedback request unavailable')
    if action != 'preview' and (body['polarity'] not in ('positive','negative') or type(body['request_key']) is not str
            or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}',body['request_key'])):
        raise RecallFeedbackUnavailable('feedback request unavailable')
    state=request.app.state; supervisor=getattr(state,'mimo_supervisor',None)
    if not supervisor: raise RecallFeedbackUnavailable('feedback unavailable')
    pool=supervisor.owner_lifecycle_pool(); provider=canonical_source_provider(state)
    if provider is None or provider is not getattr(state,'memory_provider',None): raise RecallFeedbackUnavailable('feedback unavailable')
    # These methods exist only with the positively negotiated native helped profile.
    if any(not callable(getattr(provider,name,None)) for name in ('_host_negotiate_helped','_host_helped_ready','_host_helped_selector_page','_host_append_helped','_host_helped_status')):
        raise RecallFeedbackUnavailable('feedback unavailable')
    mapping=OwnerSessionMap(pool._runtime_home(owner) / 'session-map.json',owner)
    authority=TargetAuthority(auth_manager=auth,supervisor=pool)
    verifier=CurrentHostUserSource(owner=owner,auth_manager=auth,session_factory=session_factory,archive=get_conversation_archive(),
        workspace_repository=getattr(state,'files_policy_repository',None),policy_store=STORE,guarded_preparation=provider._guarded_preparation)
    async def read_source():
        def read():
            db=session_factory()
            try: return _current(db,owner=owner,account=account,chat_id=chat_id,message_id=message_id,auth=auth)
            finally: db.close()
        row=await _queued_owned_thread(read)
        if row['role']!='user' or row['source_revision']!=1 or row['source_hash']!=body['expected_source_hash']:
            raise RecallFeedbackUnavailable('source changed')
        return SourceRef(2 if row['workspace_id'] is None else 1,'host_user_body',account,chat_id,'main',message_id,'body',1,row['source_hash'],row['workspace_id'],None)
    async with barrier.admitted(owner):
        async with mapping.async_chat_admission_lock(chat_id,blocking=False,existing_only=True) as guard:
            source=await read_source(); target=await _queued_owned_thread(authority.resolve,account)
            if not await provider._host_negotiate_helped(owner=target.memory_owner,workspace_id=target.memory_workspace):
                raise RecallFeedbackUnavailable('feedback unavailable')
            def read_policy():
                privileges=auth.get_privileges(owner)
                allowed=privileges.get('can_manage_memory',True) if isinstance(privileges,dict) else False
                return STORE.effective_snapshot(owner,read_privilege=allowed,write_privilege=allowed,incognito=False,no_memory=False,compare_mode=False)
            async def current():
                try:
                    mapping.validate_chat_guard(chat_id,guard)
                    if (auth.account_id(owner)!=account or auth.username_for_account_id(account)!=owner or verify_owner()!=owner
                            or _owner_request_fenced(owner) or getattr(session_manager.sessions.get(chat_id),'incognito',False)
                            or await read_source()!=source or await _queued_owned_thread(authority.resolve,account)!=target
                            or not await _queued_owned_thread(_s06_admitted_ready,state,supervisor)
                            or not await _queued_owned_thread(provider._host_helped_ready)):
                        return False
                    policy=await _queued_owned_thread(read_policy)
                    if (policy.get('eligibility_state')!='available' or policy.get('read_allowed') is not True
                            or (action=='commit' and policy.get('direct_write_allowed') is not True)
                            or (await _queued_owned_thread(verifier.verify_feedback_origin,source)).get('state')!='available'):
                        return False
                    mapping.validate_chat_guard(chat_id,guard)
                    return (auth.account_id(owner)==account and auth.username_for_account_id(account)==owner
                            and verify_owner()==owner and not _owner_request_fenced(owner)
                            and await read_source()==source and await _queued_owned_thread(authority.resolve,account)==target)
                except Exception:return False
            if not await current(): raise RecallFeedbackUnavailable('feedback unavailable')
            cursor='o:'; origins=[]
            for _ in range(32):
                rows, following=await _queued_owned_thread(provider._host_source_refs,account_id=account,chat_id=chat_id,
                    message_id=message_id,target_authority=authority,after_job_id=cursor,limit=128)
                origins.extend(row for row in rows if type(row) is _HostObservationProjection and row.source==source)
                if len(origins)>1: raise RecallFeedbackUnavailable('source ambiguous')
                if following is None: break
                if not following.startswith('o:') or following<=cursor: raise RecallFeedbackUnavailable('source unavailable')
                cursor=following
            else: raise RecallFeedbackUnavailable('source unavailable')
            if len(origins)!=1 or not await current(): raise RecallFeedbackUnavailable('source unavailable')
            origin=origins[0]
            page=await provider._host_helped_selector_page(origin,after=None,limit=64)
            if not await current() or type(page) is not dict or page.get('state') not in ('ready','partial'):
                raise RecallFeedbackUnavailable('feedback unavailable')
            fixed_authority={key:getattr(origin,key) for key in ('origin_id','origin_hash','captured_owner','current_fingerprint','rebind_id','rebind_hash')}
            if action=='preview':
                policy=await _queued_owned_thread(read_policy)
                if not await current(): raise RecallFeedbackUnavailable('feedback unavailable')
                return {'state':'ready','source':{'revision':source.revision,'hash':source.content_hash},'write_allowed':policy.get('direct_write_allowed') is True,
                    'items':[{'selector':_selector({'source':asdict(source),'event_id':item['event_id'],'signal':item['signal'],'memory':item['memory'],'authority':fixed_authority}),
                        'memory':{'blockID':item['memory']['block_id'],'revision':item['memory']['revision']},
                        'admission':item['signal'],'display':item['display'],'recorded':item['recorded'],
                        'keys':{polarity:_assessment_key(account,item,polarity) for polarity in ('positive','negative')}} for item in page['items']],
                    'partial':page['state']=='partial' or page.get('next_after') is not None}
            selected=_read_selector(body['selector'])
            if (set(selected)!={'source','event_id','signal','memory','authority'} or selected['source']!=asdict(source)
                    or type(selected['authority']) is not dict or type(selected['memory']) is not dict):
                raise RecallFeedbackUnavailable('selected memory unavailable')
            matches=[item for item in page['items'] if item.get('event_id')==selected['event_id'] and item.get('signal')==selected['signal'] and item.get('memory')==selected['memory']]
            if len(matches)!=1: raise RecallFeedbackUnavailable('selected memory unavailable')
            item=matches[0]
            if body['request_key'] != _assessment_key(account,item,body['polarity']):
                raise RecallFeedbackUnavailable('feedback identity changed')
            if action=='commit' and item['recorded'].get(body['polarity']) is True:
                if not await current(): raise RecallFeedbackUnavailable('feedback unavailable')
                return {'state':'replayed'}
            fixed={'version':1,'producer':'host_human_review.v1','owner':origin.owner,'workspace_id':origin.workspace_id,'account_id':account,
                   'admitted_event_id':item['event_id'],'admitted_kind':'tool' if item['signal']=='tool_admitted' else 'context',
                   'authority':selected['authority'],'memory':item['memory'],'polarity':body['polarity'],'request_key':body['request_key']}
            if not await current(): raise RecallFeedbackUnavailable('feedback unavailable')
            deadline=asyncio.get_running_loop().time()+2.0
            result=await (provider._host_append_helped(fixed,source=source,guard=guard,origin=origin,deadline=deadline) if action=='commit'
                          else provider._host_helped_status(fixed,source=source,guard=guard,origin=origin,deadline=deadline))
            if not await current(): raise RecallFeedbackUnavailable('feedback outcome unknown')
            if type(result) is not dict or result.get('state')!='applied': raise RecallFeedbackUnavailable('feedback outcome unknown')
            return {'state':'applied' if action=='commit' else 'replayed'}
