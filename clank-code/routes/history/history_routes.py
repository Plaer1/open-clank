"""History routes — session history, truncation, fork, conversation topics."""

import hashlib
import json
import os
import uuid
import logging
import re
import asyncio
import threading
import time
from collections import OrderedDict
import base64
from typing import Dict, Any, Optional

from fastapi import APIRouter, Request, HTTPException, Query

from core.models import ChatMessage
from core.database import SessionLocal, ChatMessage as DbChatMessage, Session as DbSession
from src.auth_helpers import effective_user
from src.openclank.chat_source_bridge import HostSourceFenceUnavailable
from src.topic_analyzer import analyze_topics
from src.upload_handler import reserve_message_upload_references
from src.transcript_layout import strip_invalid_transcript_layout
from src.agent_actor_accounting import actor_accounting_for_assistant
from routes.session_routes import (
    _message_role,
    _message_text,
    _prepare_context_mutation,
    _verify_session_owner,
)

logger = logging.getLogger(__name__)


def _graph_context_boundary(operation):
    return {"operation":operation,"eventID":uuid.uuid4().hex,"observedAt":int(time.time()*1000)}



_PAIR_READ_ONLY = object()

# Disposable, bounded discovery hints only. Every selected root is reauthorized
# by the source owner; this position neither acknowledges nor grants anything.
_assistant_recovery_positions = OrderedDict()
_assistant_recovery_lock = threading.Lock()

def _assistant_recovery_window(account, chat, root_ids):
    if not root_ids:
        return []
    identity = hashlib.sha256(json.dumps(root_ids, ensure_ascii=False, separators=(',', ':')).encode('utf-8')).hexdigest()
    key = (account, chat, identity)
    now = time.monotonic()
    with _assistant_recovery_lock:
        while _assistant_recovery_positions:
            first = next(iter(_assistant_recovery_positions))
            if now - _assistant_recovery_positions[first][1] <= 300:
                break
            _assistant_recovery_positions.popitem(last=False)
        position, _ = _assistant_recovery_positions.pop(key, (0, now))
        position %= len(root_ids)
        chosen = (root_ids + root_ids)[position:position + min(16, len(root_ids))]
        _assistant_recovery_positions[key] = ((position + len(chosen)) % len(root_ids), now)
        while len(_assistant_recovery_positions) > 128:
            _assistant_recovery_positions.popitem(last=False)
    return chosen


async def _read_complete_paired_text(request, session_id, session_manager, *, payload=None, captured=None, _snapshot=False, _continuation=None):
    """Complete supported text coverage, observed under existing owner guards."""
    from src.auth_helpers import require_user, is_internal_tool_identity
    from routes.orchestration_routes import _get_observation_binding, _ensure_observation_binding_current
    from src.openclank.transcript_projection import get_managed_binding
    from src.openclank.chat_source_bridge import read_current_forward_closure
    from src.openclank.conversation_archive import get_conversation_archive
    from src.memery_external_sources import CurrentHostUserSource, SourceRef
    from src.openclank.memory_policy import STORE
    from src.openclank.chat_ratings import host_text_snapshot, native_structural_snapshot, complete_paired_text_target, complete_paired_text_descriptor, _descriptor_hash, RatingConflict, RatingUnavailable
    from routes.prefs_routes import PREFERENCE_LOCK
    from contextlib import AsyncExitStack
    from sqlalchemy import text
    try:
        user=require_user(request)
        if is_internal_tool_identity(user) or getattr(request.state,'api_token',False) or getattr(request.state,'internal_tool_authenticated',False):raise RatingUnavailable('complete text unavailable')
        state_owner=request.app.state;auth=state_owner.auth_manager;barrier=state_owner.account_request_barrier
        factory=getattr(state_owner,'capture_original_credential',None)
        account=auth.account_id(user)
        if not account or auth.username_for_account_id(account)!=user or not callable(factory):raise RatingUnavailable('complete text unavailable')
        if captured is None:
            credential=factory(request,user,account)
        else:
            if (type(captured) is not tuple or len(captured)!=5 or captured[:4]!=(user,account,auth,factory)):
                raise RatingUnavailable('complete text unavailable')
            credential=captured[4]
        if not callable(credential) or credential() is not True:raise RatingUnavailable('complete text unavailable')
        async with barrier.admitted(user):
            owner,supervisor,binding,mapping=await asyncio.to_thread(_get_observation_binding,request,session_id,None,session_manager)
            if owner!=user:raise RatingUnavailable('complete text unavailable')
            async with mapping.async_chat_admission_lock(session_id,blocking=False,existing_only=True) as guard:
                async with AsyncExitStack() as stack:
                    authority=await asyncio.to_thread(get_managed_binding,session_id,owner=owner)
                    pool=supervisor.owner_lifecycle_pool();key=pool._key(owner)
                    lease,state,bridge,holder,generation=await pool.acquire_rating_operation(owner=owner)
                    async def release_lease():
                        task=asyncio.create_task(lease.release());cancelled=False
                        while not task.done():
                            try:await asyncio.shield(task)
                            except asyncio.CancelledError:cancelled=True
                        task.result()
                        if cancelled:raise asyncio.CancelledError
                    stack.push_async_callback(release_lease)
                    worker=lease.worker;process=getattr(worker,'_proc',None)
                    context=(getattr(bridge,'_session_context',{}) or {}).get(binding.engine_session_id)
                    cwd=authority.get('physicalCwd')
                    if (not state or state.status!='ready' or not worker or not worker.is_alive() or not holder or not bridge or process is None
                            or not isinstance(context,dict) or context.get('incognito') or context.get('auxiliary')
                            or authority.get('engineSessionID')!=binding.engine_session_id or authority.get('mappingRevision')!=binding.binding_revision
                            or authority.get('transition') is not None or not isinstance(cwd,str)):
                        raise RatingUnavailable('complete text unavailable')
                    def current():
                        try:
                            mapping.validate_chat_guard(session_id,guard);_ensure_observation_binding_current(mapping,owner,session_id,binding)
                            pool._assert_owner_lifecycle(key,lease._owner_epoch)
                            return (not lease._released and credential() is True and getattr(state_owner,'capture_original_credential',None) is factory
                                and getattr(state_owner,'auth_manager',None) is auth and getattr(state_owner,'account_request_barrier',None) is barrier
                                and auth.account_id(owner)==account and auth.username_for_account_id(account)==owner
                                and _verify_session_owner(request,session_id,session_manager)==owner and get_managed_binding(session_id,owner=owner)==authority
                                and pool._states.get(key) is state and state.active is worker and state.status=='ready' and state.generation==generation
                                and worker.is_alive() and worker._bridge is bridge and getattr(worker,'_proc',None) is process
                                and (getattr(bridge,'_session_context',{}) or {}).get(binding.engine_session_id) is context
                                and not context.get('incognito') and not context.get('auxiliary')
                                and getattr(getattr(worker,'_managed_callbacks',None),'holder_id',None)==holder
                                and not getattr(session_manager.sessions.get(session_id),'incognito',False))
                        except Exception:return False
                    def host_observation(db):
                        if not current():raise RatingUnavailable('complete text unavailable')
                        archive=get_conversation_archive()
                        host=host_text_snapshot(db,owner=owner,account=account,chat_id=session_id,auth=auth)
                        items=host['items']
                        if not items or len(items)%2:raise RatingUnavailable('complete text unavailable')
                        verifier=None if _snapshot else CurrentHostUserSource(owner=owner,auth_manager=auth,session_factory=SessionLocal,archive=archive,
                            workspace_repository=getattr(request.app.state,'files_policy_repository',None),policy_store=STORE,
                            guarded_preparation=getattr(getattr(getattr(request.app.state,'s05_profile',None),'provider',None),'_guarded_preparation',None))
                        pairs=[]
                        for index in range(0,len(items),2):
                            user_item,answer=items[index:index+2]
                            if user_item['role']!='user' or answer['role']!='assistant':raise RatingUnavailable('complete text unavailable')
                            # Refuse transformations and structured display metadata rather than infer rendered identity.
                            for item in (user_item,answer):
                                raw=db.execute(text('SELECT metadata FROM chat_messages WHERE session_id=:chat AND id=:id'),{'chat':session_id,'id':item['message_id']}).scalar()
                                meta=json.loads(raw) if raw else {}
                                if type(meta) is not dict or any(meta.get(k) for k in ('tool_events','attachments','managed_native','hidden')):
                                    raise RatingUnavailable('complete text unavailable')
                            entry=read_current_forward_closure(db,archive,owner=account,provider_owner=owner,chat_id=session_id,message_id=user_item['message_id'])
                            pair=entry['pair'] if entry else None
                            if not pair or pair['output']['hostMessageID']!=answer['message_id'] or pair['identity']['sessionID']!=binding.engine_session_id:
                                raise RatingUnavailable('complete text unavailable')
                            ref=SourceRef(2 if host['workspace_id'] is None else 1,'host_user_body',account,session_id,'main',user_item['message_id'],'body',user_item['source_revision'],user_item['source_hash'],host['workspace_id'],None)
                            if not _snapshot and verifier.execution_receipt(ref).get('state')!='available':raise RatingUnavailable('complete text unavailable')
                            pairs.append(entry)
                        if not current():raise RatingUnavailable('complete text unavailable')
                        return host,pairs
                    def read_host():
                        with SessionLocal() as db:return host_observation(db)
                    async def read_native():
                        if not await asyncio.to_thread(current):raise RatingUnavailable('complete text unavailable')
                        response=await worker.session_rating_source_request(session_id,binding.engine_session_id,cwd,{'stableChatID':session_id,'version':2},owner=owner)
                        if response.status_code!=200 or len(response.content)>1600000 or not await asyncio.to_thread(current):raise RatingUnavailable('complete text unavailable')
                        value=response.json();native_structural_snapshot(value)
                        if value['sessionID']!=binding.engine_session_id:raise RatingUnavailable('complete text unavailable')
                        return value
                    first_host=await asyncio.to_thread(read_host);first_native=await read_native()
                    second_host=await asyncio.to_thread(read_host);second_native=await read_native()
                    if first_host!=second_host or first_native!=second_native:raise RatingConflict('complete text changed')
                    def commit():
                        with PREFERENCE_LOCK:
                            db=SessionLocal()
                            try:
                                db.execute(text('BEGIN IMMEDIATE' if payload is not None else 'BEGIN'))
                                def matches(active_db):return current() and host_observation(active_db)==second_host
                                if not matches(db):raise RatingUnavailable('complete text changed')
                                if _snapshot:
                                    host=second_host[0]
                                    descriptor=complete_paired_text_descriptor(host,second_native,second_host[1])
                                    messages=[{'role':item['role'],'text':item['text']} for item in host['items']]
                                    if (not messages or len(messages)>256 or any(type(item['text']) is not str or item['role'] not in ('user','assistant') for item in messages)
                                            or sum(len(item['text'].encode('utf-8', 'strict')) for item in messages)>1048576):
                                        raise RatingUnavailable('complete text unavailable')
                                    result={'version':1,'chat_id':session_id,'workspace_id':host['workspace_id'],
                                            'source_sha256':_descriptor_hash(descriptor),'messages':messages}
                                else:
                                    result=complete_paired_text_target(db,owner=owner,account=account,chat_id=session_id,auth=auth,
                                        host=second_host[0],snapshot=second_native,pairs=second_host[1],current=matches,payload=payload)
                                if not matches(db):raise RatingUnavailable('complete text changed')
                                if payload is None:db.rollback()
                                else:db.commit()
                                return result
                            except BaseException:db.rollback();raise
                            finally:db.close()
                    task=asyncio.create_task(asyncio.to_thread(commit))
                    try:result=await asyncio.shield(task)
                    except asyncio.CancelledError:
                        while not task.done():
                            try:await asyncio.shield(task)
                            except asyncio.CancelledError:continue
                        task.result();raise
                    try:
                        if await read_native()!=second_native or await asyncio.to_thread(read_host)!=second_host or not await asyncio.to_thread(current):raise RatingUnavailable('complete text changed')
                    except asyncio.CancelledError:raise
                    except Exception:
                        if payload is None:raise
                        return {**result,'current_state':'unknown'}
                    if _snapshot and _continuation is not None:
                        active=True; task=asyncio.current_task()
                        async def revalidate():
                            if not active or asyncio.current_task() is not task:
                                raise RatingUnavailable('complete text unavailable')
                            if await read_native()!=second_native or await asyncio.to_thread(read_host)!=second_host or not await asyncio.to_thread(current):
                                raise RatingUnavailable('complete text changed')
                            if not active or asyncio.current_task() is not task:
                                raise RatingUnavailable('complete text unavailable')
                        try:
                            return await _continuation(result,revalidate)
                        finally:
                            active=False
                    return {**result,'current_state':'current'} if payload is not None and result.get('state')=='applied' else result
    except asyncio.CancelledError:raise
    except RatingConflict as exc:raise HTTPException(409,'Complete text changed') from exc
    except Exception as exc:raise HTTPException(503,'Complete text unavailable') from exc


async def _read_complete_chat_snapshot(request, chat_id, session_manager, *, captured, continuation=None):
    """Read only the matched supported text; never mint a rating or memory grant."""
    if type(captured) is not tuple or len(captured) != 5 or (continuation is not None and not callable(continuation)):
        raise HTTPException(503, 'Complete text unavailable')
    return await _read_complete_paired_text(request, chat_id, session_manager, captured=captured, _snapshot=True, _continuation=continuation)


async def _read_forward_pair(request, session_id, message_id, session_manager, *, segments=_PAIR_READ_ONLY, payload=None):
    """One explicit current pair; native/host observations are not atomic."""
    from src.auth_helpers import require_user, is_internal_tool_identity
    from routes.orchestration_routes import _get_observation_binding, _ensure_observation_binding_current
    from src.openclank.transcript_projection import get_managed_binding
    from src.openclank.chat_source_bridge import read_forward_pair_candidate
    from src.openclank.conversation_archive import get_conversation_archive
    from src.memery_external_sources import CurrentHostUserSource, SourceRef
    from src.openclank.memory_policy import STORE
    from src.openclank.chat_ratings import native_descriptor
    from sqlalchemy import text
    from contextlib import AsyncExitStack
    from routes.prefs_routes import PREFERENCE_LOCK
    from src.openclank.chat_ratings import paired_target, RatingConflict
    try:
        user=require_user(request)
        if is_internal_tool_identity(user) or getattr(request.state,'api_token',False) or getattr(request.state,'internal_tool_authenticated',False): raise ValueError('unavailable')
        auth=request.app.state.auth_manager; barrier=request.app.state.account_request_barrier
        account=auth.account_id(user)
        if not account or auth.username_for_account_id(account)!=user: raise ValueError('unavailable')
        async with barrier.admitted(user):
            owner,supervisor,binding,mapping=await asyncio.to_thread(_get_observation_binding,request,session_id,None,session_manager)
            if owner!=user: raise ValueError('unavailable')
            async with mapping.async_chat_admission_lock(session_id,blocking=False,existing_only=True) as guard:
                async with AsyncExitStack() as ownerstack:
                    authority=await asyncio.to_thread(get_managed_binding,session_id,owner=owner)
                    pool=supervisor.owner_lifecycle_pool(); key=pool._key(owner)
                    lease,state,bridge,holder,generation=await pool.acquire_rating_operation(owner=owner)
                    async def release_lease():
                        task=asyncio.create_task(lease.release()); cancelled=False
                        while not task.done():
                            try: await asyncio.shield(task)
                            except asyncio.CancelledError: cancelled=True
                        task.result()
                        if cancelled: raise asyncio.CancelledError
                    ownerstack.push_async_callback(release_lease)
                    worker=lease.worker
                    process=getattr(worker,'_proc',None)
                    context=(getattr(bridge,'_session_context',{}) or {}).get(binding.engine_session_id)
                    cwd=authority.get('physicalCwd')
                    if (not state or state.status!='ready' or not worker or not worker.is_alive() or not holder or not bridge or process is None or not isinstance(context,dict)
                        or context.get('incognito') or context.get('auxiliary') or authority.get('engineSessionID')!=binding.engine_session_id
                        or authority.get('mappingRevision')!=binding.binding_revision or authority.get('transition') is not None or not isinstance(cwd,str)):
                        raise ValueError('unavailable')
                    def current():
                        try:
                            mapping.validate_chat_guard(session_id,guard); _ensure_observation_binding_current(mapping,owner,session_id,binding)
                            pool._assert_owner_lifecycle(key,lease._owner_epoch)
                            return (not lease._released and auth.account_id(owner)==account and auth.username_for_account_id(account)==owner
                                and _verify_session_owner(request,session_id,session_manager)==owner and get_managed_binding(session_id,owner=owner)==authority
                                and pool._states.get(key) is state and state.active is worker and state.status=='ready' and state.generation==generation
                                and worker.is_alive() and worker._bridge is bridge and getattr(worker,'_proc',None) is process
                                and (getattr(bridge,'_session_context',{}) or {}).get(binding.engine_session_id) is context
                                and not context.get('incognito') and not context.get('auxiliary')
                                and getattr(getattr(worker,'_managed_callbacks',None),'holder_id',None)==holder
                                and not getattr(session_manager.sessions.get(session_id),'incognito',False))
                        except Exception: return False
                    def candidate():
                        if not current(): return None
                        archive=get_conversation_archive()
                        with SessionLocal() as db:
                            pair=read_forward_pair_candidate(db,archive,owner=account,chat_id=session_id,message_id=message_id)
                            workspace=db.execute(text('SELECT workspace_id FROM sessions WHERE id=:chat'),{'chat':session_id}).scalar()
                        if not pair: return None
                        ref=SourceRef(2 if workspace is None else 1,'host_user_body',account,session_id,'main',message_id,'body',pair['input']['hostRevision'],pair['input']['hostHash'],workspace,None)
                        verifier=CurrentHostUserSource(owner=owner,auth_manager=auth,session_factory=SessionLocal,archive=archive,
                            workspace_repository=getattr(request.app.state,'files_policy_repository',None),policy_store=STORE,guarded_preparation=getattr(getattr(getattr(request.app.state,'s05_profile',None),'provider',None),'_guarded_preparation',None))
                        return pair if verifier.execution_receipt(ref).get('state')=='available' and current() else None
                    first=await asyncio.to_thread(candidate)
                    if not first or first['identity']['sessionID']!=binding.engine_session_id: raise ValueError('unavailable')
                    async def native_targets():
                        if not await asyncio.to_thread(current): raise ValueError('unavailable')
                        response=await worker.session_rating_source_request(session_id,binding.engine_session_id,cwd,{'stableChatID':session_id},owner=owner)
                        if response.status_code!=200 or len(response.content)>1600000 or not await asyncio.to_thread(current): raise ValueError('unavailable')
                        snapshot=response.json()
                        if type(snapshot) is not dict or snapshot.get('sessionID')!=binding.engine_session_id: raise ValueError('unavailable')
                        result=[]
                        for keypart in ('input','output'):
                            part=first[keypart]
                            descriptor=native_descriptor(snapshot,'message',part_id=part['nativePartID'])
                            item=descriptor['items'][0]
                            if item['messageID']!=part['nativeMessageID'] or item['textSHA256']!=part['nativeTextSHA256'] or item['role']!=('user' if keypart=='input' else 'assistant'): raise ValueError('unavailable')
                            result.append(descriptor)
                        return result,snapshot
                    before,_=await native_targets()
                    if await asyncio.to_thread(candidate)!=first: raise ValueError('unavailable')
                    after,snapshot=await native_targets()
                    if before!=after or await asyncio.to_thread(candidate)!=first or not await asyncio.to_thread(current): raise ValueError('unavailable')
                    if segments is _PAIR_READ_ONLY:
                        return {'version':1,'state':'ready','coverage':'one_forward_turn','pair':first}
                    def write():
                        with PREFERENCE_LOCK:
                            db=SessionLocal()
                            try:
                                db.execute(text('BEGIN IMMEDIATE' if payload is not None else 'BEGIN'))
                                archive=get_conversation_archive()
                                def pair_current(active_db):
                                    value=read_forward_pair_candidate(active_db,archive,owner=account,chat_id=session_id,message_id=message_id) if current() else None
                                    return value if value==first else None
                                if pair_current(db) is None: raise ValueError('pair changed')
                                result=paired_target(db,owner=owner,account=account,chat_id=session_id,auth=auth,
                                    pair=first,snapshot=snapshot,segments=segments,payload=payload,pair_current=pair_current)
                                if pair_current(db) is None: raise ValueError('pair changed')
                                if payload is None: db.rollback()
                                else: db.commit()
                                return result
                            except BaseException:
                                db.rollback(); raise
                            finally: db.close()
                    task=asyncio.create_task(asyncio.to_thread(write))
                    try:
                        result=await asyncio.shield(task)
                    except asyncio.CancelledError:
                        while not task.done():
                            try: await asyncio.shield(task)
                            except asyncio.CancelledError: continue
                        task.result(); raise
                    try:
                        final,_=await native_targets()
                        if final!=after or await asyncio.to_thread(candidate)!=first or not await asyncio.to_thread(current):
                            raise ValueError('pair changed')
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        if payload is None: raise
                        return {**result,'current_state':'unknown'}
                    if payload is not None and result.get('state')=='applied':
                        return {**result,'current_state':'current'}
                    return result
    except asyncio.CancelledError:
        raise
    except RatingConflict:
        raise
    except Exception:
        return {'version':1,'state':'unavailable'}


def _last_proven_main_context(owner, chat_id):
    """Read only the current live worker's exact admitted main dispatch."""
    from src.model_dispatch import get_agent_supervisor
    from src.openclank.acp_supervisor_adapter import AcpSupervisorAdapter
    from core.provider_models import ProviderOperationBinding
    from core.stats_models import StatsEvent
    if not owner:
        return None
    supervisor = get_agent_supervisor()
    if not isinstance(supervisor, AcpSupervisorAdapter):
        return None
    worker = supervisor.owner_lifecycle_pool().worker_for_owner(owner)
    if worker is None or not worker.is_alive() or worker._bridge is None:
        return None
    bridge = worker._bridge
    mapping = bridge._durable_session_map.lookup(chat_id)
    if not mapping or mapping.get("owner") != owner:
        return None
    engine = mapping["current"]
    native_context = bridge._session_context.get(engine) or {}
    if native_context.get("incognito") or native_context.get("auxiliary"):
        return None
    root = (bridge._session_state.get(engine) or {}).get("last_root_operation_id")
    holder = bridge._managed_provider_generation
    if not root or not holder:
        return None
    with SessionLocal() as db:
        rows = db.query(StatsEvent).filter_by(owner=owner, session_id=chat_id, root_operation_id=root,
            event_kind="context_receipt", source="context_receipt").limit(3).all()
        matches = []
        for row in rows:
            fact = (row.event_metadata or {}).get("context_receipt") or {}
            identity = fact.get("identity") or {}
            if (fact.get("instance_id") != holder or fact.get("mapping_revision") != mapping["revision"]
                or identity.get("sessionID") != engine or identity.get("rootOperationID") != root
                or identity.get("actorID") != "main"):
                continue
            binding = db.query(ProviderOperationBinding).filter_by(owner=owner,id=fact.get("binding_id"),
                revision=fact.get("binding_revision"),root_operation_id=root,engine_session_id=engine,
                actor_id="main",execution_revision=identity.get("executionRevision")).first()
            if binding is not None:
                matches.append((row, fact))
        if len(matches) != 1:
            return None
        row, fact = matches[0]
        events = db.query(StatsEvent).filter_by(owner=owner,root_operation_id=root,operation_id=row.operation_id,
            attempt_id=fact.get("dispatch_id"),source="sdk",terminal=True).limit(3).all()
        events = [event for event in events if (event.event_metadata or {}).get("instance_id") == holder]
        if len(events) != 1:
            return None
        event = events[0]
        meta = event.event_metadata or {}
        coverage = (meta.get("metric_coverage") or {}).get("input_tokens") or {}
        if (meta.get("normalization_profile") != "managed-sdk-inclusive-v1" or coverage.get("state") != "reported"
            or coverage.get("coverage") != "complete" or event.input_tokens is None
            or meta.get("covered_dispatch_ids") != [fact.get("dispatch_id")] or meta.get("loss_reasons")):
            return None
        result = {"tokens":event.input_tokens,"state":"reported","source":"sdk_single_dispatch",
                  "cache_included":True,"as_of":event.event_time.isoformat() + "Z"}
    current_context = bridge._session_context.get(engine) or {}
    if (current_context.get("incognito") or current_context.get("auxiliary")
        or bridge._durable_session_map.lookup(chat_id) != mapping
        or (bridge._session_state.get(engine) or {}).get("last_root_operation_id") != root
        or bridge._managed_provider_generation != holder or worker._bridge is not bridge or not worker.is_alive()):
        return None
    return result

from src.openclank.history_io import bounded_history_io as _bounded_history_io, HISTORY_IO_WORKERS as _HISTORY_IO_WORKERS
_HISTORY_RESOURCE_MATCH_LIMIT = 16

_HISTORY_INLINE_MEDIA_THRESHOLD = 200_000
_DATA_IMAGE_RE = re.compile(r"data:image/[^;,\"]+;base64,[A-Za-z0-9+/=\s]+")


def _leading_system_prefix(messages):
    """Return genuine authority before any prior compaction projection."""
    prefix = []
    for message in messages:
        if _message_role(message) != "system":
            break
        metadata = getattr(message, "metadata", None)
        if not isinstance(metadata, dict):
            metadata = message.get("metadata", {}) if isinstance(message, dict) else {}
        content = _message_text(message)
        if metadata.get("compacted") or content.startswith("[Conversation summary"):
            break
        prefix.append(message)
    return prefix


def _history_display_content(content: Any) -> Any:
    """Return a lightweight browser-display copy of stored message content.

    Older multimodal user messages may be persisted as a JSON *string*
    containing image_url blocks with inline base64 image bytes. Those bytes are
    needed for model calls when the turn is first sent, but they should not be
    sent back through /api/history every time the user opens the chat. The
    attachment metadata already carries file ids/names for the UI cards.
    """
    if isinstance(content, list):
        text_parts = []
        omitted_media = 0
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text":
                text = block.get("text")
                if isinstance(text, str) and text:
                    text_parts.append(text)
            elif block.get("type") in {"image_url", "input_image", "audio", "input_audio"}:
                omitted_media += 1
        text = "\n".join(text_parts).strip()
        if omitted_media and not text:
            return f"[{omitted_media} media attachment{'s' if omitted_media != 1 else ''} omitted from history view]"
        return text

    if not isinstance(content, str):
        return content
    if len(content) < _HISTORY_INLINE_MEDIA_THRESHOLD and "data:image/" not in content:
        return content

    stripped = content.lstrip()
    if stripped.startswith("["):
        try:
            blocks = json.loads(content)
        except (json.JSONDecodeError, TypeError, ValueError):
            blocks = None
        if isinstance(blocks, list):
            text_parts = []
            for block in blocks:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "text":
                    text = block.get("text")
                    if isinstance(text, str) and text:
                        text_parts.append(text)
            if text_parts:
                return "\n".join(text_parts).strip()

    if "data:image/" in content:
        return _DATA_IMAGE_RE.sub("[inline image omitted from history view]", content)
    return content


def _merge_continue_rows_to_delete(db_messages, db1, db2):
    """DB rows to delete when merging the last two assistant messages.

    Always the second assistant message (db2), plus ONLY the single
    intervening "continue" user message (the one carrying "previous response
    was interrupted") — matching the in-memory merge. The previous code
    deleted the whole index range between the two assistant rows, destroying
    any tool/system/user messages in between and desyncing the DB from the
    in-memory history.
    """
    to_delete = [db2]
    i1 = next((i for i, m in enumerate(db_messages) if m is db1), None)
    i2 = next((i for i, m in enumerate(db_messages) if m is db2), None)
    if i1 is not None and i2 is not None and i2 - 1 > i1:
        between = db_messages[i2 - 1]
        if getattr(between, "role", "") == "user" and            "previous response was interrupted" in (getattr(between, "content", "") or ""):
            to_delete.append(between)
    return to_delete


def setup_history_routes(session_manager, upload_handler=None) -> APIRouter:
    router = APIRouter(tags=["history"])

    def _settled_visibility(state):
        from src.openclank.session_operations import visibility, HistoryOperationError
        try: return visibility(state, require_settled=True)
        except HistoryOperationError as exc:
            raise HTTPException(409, {"code": exc.code, "message": "Resolve pending native history before reading transcript"}) from exc

    def _reserve_message_uploads(
        request: Request,
        content: Any,
        metadata: Any = None,
    ) -> None:
        try:
            missing_id = reserve_message_upload_references(
                upload_handler,
                effective_user(request),
                content,
                metadata,
            )
        except (TypeError, ValueError) as exc:
            raise HTTPException(400, "Invalid message attachment metadata") from exc
        if missing_id:
            raise HTTPException(
                409,
                f"Referenced upload is no longer available: {missing_id}",
            )

    def _db_history_entry(m: DbChatMessage) -> Dict[str, Any]:
        entry = {"role": m.role, "content": _history_display_content(m.content)}
        meta = {}
        if m.meta_data:
            try:
                meta = json.loads(m.meta_data) or {}
            except (json.JSONDecodeError, ValueError):
                meta = {}
        if m.timestamp and "timestamp" not in meta:
            meta["timestamp"] = m.timestamp.isoformat() + "Z"
        meta = strip_invalid_transcript_layout(m.content, meta)
        meta["_db_id"] = m.id  # Server-owned committed message identity.
        accounting = actor_accounting_for_assistant(m.id)
        if accounting is not None:
            meta["actor_accounting"] = accounting
        elif meta.get("tool_events"):
            meta["actor_accounting"] = {"state": "not_recorded", "total": None, "actors": []}
        if meta:
            entry["metadata"] = meta
        return entry

    def _db_message_metadata(m: DbChatMessage) -> Dict[str, Any]:
        meta = {}
        if m.meta_data:
            try:
                meta = json.loads(m.meta_data) or {}
            except (json.JSONDecodeError, ValueError):
                meta = {}
        if m.timestamp and "timestamp" not in meta:
            meta["timestamp"] = m.timestamp.isoformat() + "Z"
        return meta

    def _hydrate_session_history_from_db(session_id: str, rows: list[DbChatMessage]) -> None:
        """Rebuild in-memory context from raw DB rows after a history load.

        The browser history endpoint can return paged/display-trimmed messages,
        but the next model call reads ``session.history``. After a restart or a
        stale in-memory session, selecting an old chat through the paged endpoint
        used to show the transcript while the model only saw fresh context.
        """
        if not rows:
            return
        try:
            session = session_manager.get_session(session_id)
        except KeyError:
            return
        with session._history_lock:
            if session_manager.sessions.get(session_id) is not session:
                return
            with SessionLocal() as current_db:
                current = current_db.query(DbChatMessage).filter(
                    DbChatMessage.session_id == session_id
                ).order_by(DbChatMessage.timestamp).all()
                if [(m.id, m.role, m.content, m.meta_data) for m in current] != [
                    (m.id, m.role, m.content, m.meta_data) for m in rows
                ]:
                    return
                session.history = [
                    ChatMessage(role=m.role, content=m.content, metadata=_db_message_metadata(m) or None)
                    for m in current
                ]
                session.message_count = len(session.history)

    def _session_needs_db_history_hydration(session_id: str, total: int) -> bool:
        try:
            session = session_manager.get_session(session_id)
        except KeyError:
            return False
        return len(session.history or []) < int(total or 0)

    @router.get("/api/history/{session_id}")
    async def get_session_history(
        request: Request,
        session_id: str,
        limit: Optional[int] = None,
        offset: Optional[int] = None,
    ) -> Dict[str, Any]:
        owner = _verify_session_owner(request, session_id)
        with SessionLocal() as boundary_db:
            boundary = boundary_db.query(DbSession).filter(DbSession.id == session_id).first()
            if boundary is not None: _settled_visibility(boundary.mimo_state)
        cached = session_manager.sessions.get(session_id)
        if not getattr(cached, "incognito", False):
            from src.openclank.conversation_archive import get_conversation_archive, ArchiveUnavailableError
            try:
                if get_conversation_archive().chat_erasure_started(owner=owner, chat_id=session_id):
                    raise HTTPException(409, "Conversation deletion is pending; retry erasure to finish")
            except ArchiveUnavailableError as exc:
                raise HTTPException(503, str(exc)) from exc
            from src.openclank.chat_source_bridge import recover_chat, available
            try:
                recovered_ids = []
                with SessionLocal() as recovery_db:
                    if available(recovery_db):
                        archive = get_conversation_archive()
                        account = archive.resolve_owner(owner)
                        recover_chat(recovery_db, archive, owner=account, chat_id=session_id, delivered_ids=recovered_ids)
                if recovered_ids:
                    from src.openclank.account_request_barrier import _current_admission
                    from src.openclank.chat_source_bridge import reconcile_recovered_assistants
                    auth = getattr(request.app.state, 'auth_manager', None)
                    barrier = getattr(request.app.state, 'account_request_barrier', None)
                    if (getattr(request.state, 'authenticated', False) and auth is not None
                            and auth.account_id(owner) == account and auth.username_for_account_id(account) == owner):
                        await reconcile_recovered_assistants(state=request.app.state, owner=owner, account=account,
                            chat_id=session_id, admission=_current_admission.get(), barrier=barrier, delivered_ids=recovered_ids)
            except Exception:
                logger.warning("host source recovery unavailable")
        if limit is not None:
            page_limit = max(1, min(int(limit), 100))
            db = SessionLocal()
            try:
                db_session = db.query(DbSession).filter(DbSession.id == session_id).first()
                if db_session is None:
                    raise HTTPException(404, f"Session '{session_id}' not found")

                from src.openclank.session_operations import visibility
                excluded = _settled_visibility(db_session.mimo_state)
                total = (
                    db.query(DbChatMessage)
                    .filter(DbChatMessage.session_id == session_id, ~DbChatMessage.id.in_(excluded))
                    .count()
                )
                page_offset = int(offset) if offset is not None else max(total - page_limit, 0)
                page_offset = max(0, min(page_offset, total))
                rows = (
                    db.query(DbChatMessage)
                    .filter(DbChatMessage.session_id == session_id, ~DbChatMessage.id.in_(excluded))
                    .order_by(DbChatMessage.timestamp)
                    .offset(page_offset)
                    .limit(page_limit)
                    .all()
                )
                if _session_needs_db_history_hydration(session_id, total):
                    full_rows = (
                        db.query(DbChatMessage)
                        .filter(DbChatMessage.session_id == session_id, ~DbChatMessage.id.in_(excluded))
                        .order_by(DbChatMessage.timestamp)
                        .all()
                    )
                    _hydrate_session_history_from_db(session_id, full_rows)
                visible_rows = [(m, _db_history_entry(m)) for m in rows]
                visible_rows = [(m, entry) for m, entry in visible_rows
                                if not (entry.get("metadata") or {}).get("hidden")]
                history_dict = [entry for _, entry in visible_rows]
                result = {
                    "history": history_dict,
                    "model": db_session.model,
                    "endpoint_url": db_session.endpoint_url,
                    "name": db_session.name,
                    "offset": page_offset,
                    "limit": page_limit,
                    "total": total,
                    "has_more_before": page_offset > 0,
                    "has_more_after": page_offset + len(rows) < total,
                }
                root_ids = [m.id for m, entry in visible_rows if entry.get('role') == 'user' and m.id]
                db.close()
                if root_ids and not getattr(cached, 'incognito', False):
                    try:
                        from src.openclank.account_request_barrier import _current_admission
                        from src.openclank.chat_source_bridge import reconcile_history_roots
                        auth = getattr(request.app.state, 'auth_manager', None)
                        account = auth.account_id(owner) if auth is not None else None
                        if (getattr(request.state, 'authenticated', False) and account
                                and auth.username_for_account_id(account) == owner):
                            root_ids = _assistant_recovery_window(account, session_id, root_ids)
                            await reconcile_history_roots(state=request.app.state, owner=owner, account=account,
                                chat_id=session_id, admission=_current_admission.get(),
                                barrier=getattr(request.app.state, 'account_request_barrier', None), root_ids=root_ids)
                    except Exception:
                        logger.debug('assistant component reconciliation unavailable')
                return result
            finally:
                db.close()

        try:
            session = session_manager.get_session(session_id)
        except KeyError:
            raise HTTPException(404, f"Session '{session_id}' not found")

        history_dict = []
        for msg in session.history:
            if isinstance(msg, ChatMessage):
                # Skip hidden messages (e.g. compaction summaries for AI context)
                if msg.metadata and msg.metadata.get("hidden"):
                    continue
                entry = {"role": msg.role, "content": _history_display_content(msg.content)}
                if msg.metadata:
                    meta = strip_invalid_transcript_layout(msg.content, msg.metadata)
                    accounting = actor_accounting_for_assistant(meta.get("_db_id"))
                    if accounting is not None:
                        meta = {**meta, "actor_accounting": accounting}
                    elif meta.get("tool_events"):
                        meta = {**meta, "actor_accounting": {"state": "not_recorded", "total": None, "actors": []}}
                    entry["metadata"] = meta
                history_dict.append(entry)
            elif isinstance(msg, dict):
                if msg.get("metadata", {}).get("hidden"):
                    continue
                entry = {
                    "role": msg.get("role", ""),
                    "content": _history_display_content(msg.get("content", "")),
                }
                if msg.get("metadata"):
                    meta = strip_invalid_transcript_layout(
                        msg.get("content", ""), msg["metadata"]
                    )
                    accounting = actor_accounting_for_assistant(meta.get("_db_id"))
                    if accounting is not None:
                        meta = {**meta, "actor_accounting": accounting}
                    elif meta.get("tool_events"):
                        meta = {**meta, "actor_accounting": {"state": "not_recorded", "total": None, "actors": []}}
                    entry["metadata"] = meta
                history_dict.append(entry)

        # Fallback: load from DB if in-memory is empty
        if not history_dict:
            db = SessionLocal()
            try:
                from src.openclank.session_operations import visibility
                view_row = db.query(DbSession).filter(DbSession.id == session_id).first()
                excluded = _settled_visibility(view_row.mimo_state if view_row else {})
                db_messages = (
                    db.query(DbChatMessage)
                    .filter(DbChatMessage.session_id == session_id, ~DbChatMessage.id.in_(excluded))
                    .order_by(DbChatMessage.timestamp)
                    .all()
                )
                db_history = []
                for m in db_messages:
                    db_history.append(_db_history_entry(m))
                if db_history:
                    # Rebuild in-memory history from the full set so hidden
                    # messages (e.g. compaction summaries) are kept for AI context.
                    _hydrate_session_history_from_db(session_id, db_messages)
                # Response excludes hidden messages, matching the in-memory path.
                history_dict = [
                    m for m in db_history
                    if not (m.get("metadata") or {}).get("hidden")
                ]
            except HTTPException:
                raise
            except Exception as e:
                logger.error(f"DB fallback failed for {session_id}: {e}")
            finally:
                db.close()

        return {
            "history": history_dict,
            "model": session.model,
            "endpoint_url": session.endpoint_url,
            "name": session.name,
        }

    @router.post("/api/session/{session_id}/truncate")
    async def truncate_session(request: Request, session_id: str, graph_activity: bool = False):
        verified_owner = _verify_session_owner(request, session_id)
        try:
            session = session_manager.get_session(session_id)
        except KeyError:
            raise HTTPException(404, "Session not found")
        mutation_owner = await _prepare_context_mutation(
            request, session_id, session_owner=getattr(session, "owner", None),
            verified_owner=verified_owner,
        )
        try:
            body = await request.json()
            keep_count = body.get("keep_count", 0)
            result = await session_manager.truncate_messages_guarded(session_id, keep_count)
            receipt={"status":"ok","kept":keep_count,"truncated":result}
            if graph_activity and result is True and type(keep_count) is int and keep_count==0:
                receipt["activity_boundary"]=_graph_context_boundary("clear")
            return receipt
        except KeyError:
            raise HTTPException(404, "Session not found")
        except Exception as e:
            logger.error(f"Truncate error {session_id}: {e}")
            raise HTTPException(500, str(e))

    @router.post("/api/session/{session_id}/message")
    async def add_message(request: Request, session_id: str):
        """Add a message to a session (for slash command persistence)."""
        _verify_session_owner(request, session_id)
        await _prepare_context_mutation(request, session_id)
        try:
            body = await request.json()
            role = body.get("role", "assistant")
            content = body.get("content", "")
            if not content:
                raise HTTPException(400, "content is required")
            metadata = body.get("metadata")
            _reserve_message_uploads(request, content, metadata)
            msg = ChatMessage(role=role, content=content, metadata=metadata)
            session_manager.add_message(session_id, msg)
            return {"status": "ok"}
        except KeyError:
            raise HTTPException(404, "Session not found")

    @router.post("/api/session/{session_id}/delete-messages")
    async def delete_messages(request: Request, session_id: str):
        """Delete specific messages by DB ID (or legacy index)."""
        _verify_session_owner(request, session_id)
        await _prepare_context_mutation(request, session_id)
        try:
            body = await request.json()
            msg_ids = body.get("msg_ids", [])
            indices = body.get("indices")  # legacy fallback

            if not isinstance(msg_ids, list) or any(not isinstance(mid, str) or not mid for mid in msg_ids):
                raise HTTPException(400, "Invalid message IDs")
            if indices is not None and (not isinstance(indices, list) or any(type(index) is not int for index in indices)):
                raise HTTPException(400, "Invalid message indices")
            from src.openclank.chat_source_bridge import guarded_source_mutation
            owner = _verify_session_owner(request, session_id)
            def selected_ids():
                with SessionLocal() as check:
                    query = check.query(DbChatMessage).filter(DbChatMessage.session_id == session_id)
                    if msg_ids:
                        return tuple(sorted(row.id for row in query.filter(DbChatMessage.id.in_(msg_ids)).all()))
                    if indices:
                        rows = query.order_by(DbChatMessage.timestamp).all()
                        return tuple(sorted({rows[index].id for index in indices if 0 <= index < len(rows)}))
                    return ()
            initial_ids = selected_ids()
            def linked_root_ids():
                candidate_ids = tuple(sorted(set(initial_ids) | set(msg_ids or ())))
                if not candidate_ids:
                    return ()
                from sqlalchemy import bindparam, text
                from src.openclank.chat_source_bridge import origin_available
                from src.openclank.conversation_archive import get_conversation_archive
                with SessionLocal() as check:
                    if not origin_available(check):
                        return ()
                    account = get_conversation_archive().resolve_owner(owner)
                    if not account:
                        raise HostSourceFenceUnavailable("Memory source owner is unavailable")
                    stmt = text("SELECT DISTINCT message_id FROM host_source_delivery_intent WHERE owner=:owner AND chat_id=:chat AND part_id='body' AND revision=1 AND origin_assistant_id IN :ids").bindparams(bindparam('ids', expanding=True))
                    return tuple(sorted(row[0] for row in check.execute(stmt, {'owner':account, 'chat':session_id, 'ids':candidate_ids})))
            initial_roots = linked_root_ids()
            # Exact requested IDs can still own durable source jobs even if
            # their canonical message was already removed on a prior attempt.
            fence_ids = tuple(sorted(set(msg_ids if msg_ids else initial_ids) | set(initial_roots)))
            def pre_fence():
                if _verify_session_owner(request, session_id) != owner or selected_ids() != initial_ids or linked_root_ids() != initial_roots:
                    raise HTTPException(409, "Message selection changed; refresh and try again")
            async with guarded_source_mutation(request, owner=owner, chat_id=session_id, message_ids=fence_ids, pre_fence=pre_fence) as source_guard:
                if source_guard is not None:
                    from src.openclank.chat_source_bridge import retire_skill_draft_manifests
                    from src.openclank.conversation_archive import get_conversation_archive
                    archive = get_conversation_archive()
                    # M5 admits only revision-1 main-actor user roots. Linked
                    # completion roots are included in the held fence set.
                    for root_id in fence_ids:
                        identity = archive.private_part_retirement_identity(owner=source_guard.account,
                            chat_id=session_id, actor_id='main', message_id=root_id,
                            part_id='body', revision=1)
                        if identity is not None and identity['role'] == 'user' and identity['part_type'] == 'text':
                            await retire_skill_draft_manifests(capability=source_guard, archive=archive,
                                part={'actor_id':'main', 'message_id':root_id, 'part_id':'body', 'revision':1})
                    pre_fence()
                session = session_manager.get_session(session_id)
                with session._history_lock:
                    db = SessionLocal()
                    try:
                        if initial_ids or msg_ids:
                            from src.openclank.chat_source_bridge import origin_available, withdraw_deleted_messages
                            if origin_available(db):
                                from src.openclank.conversation_archive import get_conversation_archive
                                archive = get_conversation_archive()
                                account = archive.resolve_owner(owner)
                                if not account:
                                    raise HostSourceFenceUnavailable("Memory source owner is unavailable")
                                withdraw_deleted_messages(db, archive, owner=account, chat_id=session_id,
                                    message_ids=tuple(sorted(set(initial_ids) | set(msg_ids or ()))))
                        if msg_ids:
                            # New ID-based delete
                            deleted = 0
                            for mid in msg_ids:
                                db_msg = db.query(DbChatMessage).filter(
                                    DbChatMessage.id == mid,
                                    DbChatMessage.session_id == session_id,
                                ).first()
                                if db_msg:
                                    db.delete(db_msg)
                                    deleted += 1

                            # Remove from in-memory history by matching _db_id
                            def _get_db_id(m):
                                meta = m.metadata if isinstance(m, ChatMessage) else (m.get('metadata') if isinstance(m, dict) else None)
                                return meta.get('_db_id') if isinstance(meta, dict) else None
                            next_history = [m for m in session.history if _get_db_id(m) not in msg_ids]
                        elif indices:
                            # Legacy index-based delete
                            indices = sorted(indices, reverse=True)
                            db_messages = db.query(DbChatMessage).filter(
                                DbChatMessage.session_id == session_id
                            ).order_by(DbChatMessage.timestamp).all()

                            deleted = 0
                            next_history = list(session.history)
                            for idx in indices:
                                if 0 <= idx < len(db_messages):
                                    db.delete(db_messages[idx])
                                    deleted += 1
                                if 0 <= idx < len(next_history):
                                    next_history.pop(idx)
                        else:
                            return {"status": "ok", "deleted": 0}

                        db_session = db.query(DbSession).filter(DbSession.id == session_id).first()
                        if db_session:
                            db_session.message_count = len(next_history)
                            from datetime import datetime, timezone
                            db_session.updated_at = datetime.now(timezone.utc)

                        db.commit()
                        session.history = next_history
                        session.message_count = len(next_history)
                        return {"status": "ok", "deleted": deleted}
                    finally:
                        db.close()
        except HostSourceFenceUnavailable:
            raise HTTPException(503, "Memory source transition is unavailable")
        except KeyError:
            raise HTTPException(404, "Session not found")
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Delete messages error {session_id}: {e}")
            raise HTTPException(500, str(e))

    async def _message_rating(request: Request, session_id: str, message_id: str, payload=None, kind=None, segments=None):
        from src.auth_helpers import require_user, is_internal_tool_identity
        from src.openclank.chat_ratings import execute, RatingConflict, RatingUnavailable
        user=require_user(request)
        if is_internal_tool_identity(user): raise HTTPException(403,'A named user is required')
        if payload is not None and (getattr(request.state, 'api_token', False) or getattr(request.state, 'internal_tool_authenticated', False)):
            raise HTTPException(403, 'An interactive human is required')
        owner=_verify_session_owner(request,session_id,session_manager)
        state=request.app.state;auth=getattr(state,'auth_manager',None);barrier=getattr(state,'account_request_barrier',None)
        if auth is None or barrier is None or owner!=user: raise HTTPException(503,'Rating authority unavailable')
        account=auth.account_id(user)
        if not account or auth.username_for_account_id(account)!=user: raise HTTPException(503,'Rating authority unavailable')
        def current():
            try:
                cached=session_manager.sessions.get(session_id)
                lifecycle=getattr(state,'account_owner_lifecycle',None)
                return (not getattr(cached,'incognito',False) and auth.account_id(user)==account and auth.username_for_account_id(account)==user
                    and lifecycle is not None and not lifecycle.owner_has_active_operation(user)
                    and _verify_session_owner(request,session_id,session_manager)==user)
            except Exception:return False
        try:
            async with barrier.admitted(user):
                task=asyncio.create_task(asyncio.to_thread(execute,SessionLocal,owner=user,account=account,chat_id=session_id,
                    message_id=message_id,auth=auth,current=current,payload=payload,kind=kind,segments=segments))
                try:return await asyncio.shield(task)
                except asyncio.CancelledError:
                    while not task.done():
                        try:await asyncio.shield(task)
                        except asyncio.CancelledError:continue
                    task.result();raise
        except RatingConflict as exc:raise HTTPException(409,str(exc)) from exc
        except RatingUnavailable as exc:raise HTTPException(503,str(exc)) from exc

    @router.get("/api/session/{session_id}/message-rating/{message_id}")
    async def get_message_rating(request: Request, session_id: str, message_id: str):
        return await _message_rating(request,session_id,message_id)

    @router.post("/api/session/{session_id}/message-rating/{message_id}")
    async def set_message_rating(request: Request, session_id: str, message_id: str):
        body=await request.json()
        keys={'expected_source_revision','expected_source_hash','expected_rating_revision','action','value','note','request_key'}
        if type(body) is not dict or set(body)!=keys:raise HTTPException(400,'Exact rating request is required')
        return await _message_rating(request,session_id,message_id,body)

    @router.post("/api/session/{session_id}/message-rating/{message_id}/recall-feedback/{action}")
    async def recall_feedback(request: Request, session_id: str, message_id: str, action: str):
        from src.auth_helpers import require_user, is_internal_tool_identity
        from src.openclank.recall_feedback import review, RecallFeedbackUnavailable
        if action not in ('preview','commit','reconcile'): raise HTTPException(404,'Feedback unavailable')
        user=require_user(request)
        if (is_internal_tool_identity(user) or getattr(request.state,'api_token',False)
                or getattr(request.state,'internal_tool_authenticated',False)):
            raise HTTPException(403,'An interactive human is required')
        owner=_verify_session_owner(request,session_id,session_manager)
        auth=getattr(request.app.state,'auth_manager',None);barrier=getattr(request.app.state,'account_request_barrier',None)
        account=auth.account_id(user) if auth else None
        if owner!=user or not account or not barrier or auth.username_for_account_id(account)!=user:
            raise HTTPException(503,'Feedback unavailable')
        try:
            return await review(request,owner=user,account=account,chat_id=session_id,message_id=message_id,
                body=await request.json(),action=action,session_manager=session_manager,session_factory=SessionLocal,
                auth=auth,barrier=barrier,verify_owner=lambda:_verify_session_owner(request,session_id,session_manager))
        except RecallFeedbackUnavailable as exc:
            raise HTTPException(503,'Feedback unavailable or outcome unknown') from exc

    @router.post("/api/session/{session_id}/complete-text-rating")
    async def complete_text_rating(request: Request, session_id: str):
        from src.auth_helpers import require_user, is_internal_tool_identity
        user=require_user(request); state=request.app.state
        if is_internal_tool_identity(user) or getattr(request.state,'api_token',False) or getattr(request.state,'internal_tool_authenticated',False):
            raise HTTPException(403,'An interactive human is required')
        auth=getattr(state,'auth_manager',None);factory=getattr(state,'capture_original_credential',None)
        account=auth.account_id(user) if auth else None
        if not account or auth.username_for_account_id(account)!=user or not callable(factory):
            raise HTTPException(503,'Complete text unavailable')
        credential=factory(request,user,account)
        if not callable(credential) or credential() is not True:raise HTTPException(503,'Complete text unavailable')
        captured=(user,account,auth,factory,credential)
        body=await request.json()
        if type(body) is not dict or set(body)!={'payload'} or (body['payload'] is not None and type(body['payload']) is not dict):
            raise HTTPException(400,'Exact complete text request required')
        return await _read_complete_paired_text(request,session_id,session_manager,payload=body['payload'],captured=captured)

    @router.post("/api/session/{session_id}/rating-target/preview")
    async def preview_rating_target(request: Request, session_id: str):
        body=await request.json()
        if type(body) is not dict or set(body)!={'kind','segments'} or body['kind'] not in ('chat','section'):
            raise HTTPException(400,'Exact target is required')
        return await _message_rating(request,session_id,'target',kind=body['kind'],segments=body['segments'])

    @router.post("/api/session/{session_id}/rating-target")
    async def set_rating_target(request: Request, session_id: str):
        body=await request.json()
        keys={'kind','segments','expected_descriptor_hash','expected_rating_revision','action','value','note','request_key'}
        if type(body) is not dict or set(body)!=keys or body['kind'] not in ('chat','section'):
            raise HTTPException(400,'Exact target is required')
        payload={k:body[k] for k in keys-{'kind','segments'}}
        return await _message_rating(request,session_id,'target',payload,kind=body['kind'],segments=body['segments'])

    @router.post("/api/session/{session_id}/paired-rating")
    async def forward_pair_rating(request: Request, session_id: str):
        from src.openclank.chat_ratings import RatingConflict, paired_status
        from src.auth_helpers import require_user, is_internal_tool_identity
        from routes.prefs_routes import PREFERENCE_LOCK
        from sqlalchemy import text
        user=require_user(request)
        if is_internal_tool_identity(user) or getattr(request.state,'api_token',False) or getattr(request.state,'internal_tool_authenticated',False):
            raise HTTPException(403,'An interactive human is required')
        auth=getattr(request.app.state,'auth_manager',None); barrier=getattr(request.app.state,'account_request_barrier',None)
        account=auth.account_id(user) if auth else None
        if not account or not barrier or auth.username_for_account_id(account)!=user: raise HTTPException(503,'Forward pair rating unavailable')
        try:
            async with barrier.admitted(user):
                raw=bytearray()
                async for chunk in request.stream():
                    if len(raw)+len(chunk)>16384: raise HTTPException(413,'Forward pair request too large')
                    raw.extend(chunk)
                def unique_pairs(items):
                    result={}
                    for key,value in items:
                        if key in result: raise ValueError('duplicate field')
                        result[key]=value
                    return result
                try: body=json.loads(raw.decode('utf-8'),object_pairs_hook=unique_pairs)
                except (ValueError,UnicodeError): raise HTTPException(400,'Exact forward pair target required')
                def valid_message_id(value):
                    try: return type(value) is str and bool(value) and len(value.encode('utf-8'))<=256
                    except UnicodeError: return False
                if (type(body) is not dict or set(body) not in ({'message_id','segments','payload'},{'message_id','segments','payload','mode'})
                        or body.get('mode','apply') not in ('apply','reconcile') or not valid_message_id(body['message_id'])
                        or (body['segments'] is not None and (type(body['segments']) is not list or not 1<=len(body['segments'])<=2))
                        or (body['payload'] is not None and type(body['payload']) is not dict)):
                    raise HTTPException(400,'Exact forward pair target required')
                def current():
                    return (auth.account_id(user)==account and auth.username_for_account_id(account)==user
                        and _verify_session_owner(request,session_id,session_manager)==user)
                if not current(): raise HTTPException(503,'Forward pair rating unavailable')
                if body.get('mode')=='reconcile':
                    if body['payload'] is None or body['segments'] is None: raise HTTPException(400,'Exact original request required')
                    def status():
                        with PREFERENCE_LOCK:
                            db=SessionLocal()
                            try:
                                db.execute(text('BEGIN'))
                                if not current(): raise ValueError('account changed')
                                value=paired_status(db,owner=user,account=account,chat_id=session_id,auth=auth,
                                    message_id=body['message_id'],segments=body['segments'],payload=body['payload'])
                                if not current(): raise ValueError('account changed')
                                db.rollback(); return value
                            finally: db.close()
                    task=asyncio.create_task(asyncio.to_thread(status))
                    try: return await asyncio.shield(task)
                    except asyncio.CancelledError:
                        while not task.done():
                            try: await asyncio.shield(task)
                            except asyncio.CancelledError: continue
                        task.result(); raise
                result=await _read_forward_pair(request,session_id,body['message_id'],session_manager,segments=body['segments'],payload=body['payload'])
                if not current() or result.get('state')=='unavailable': raise HTTPException(503,'Forward pair rating unavailable')
                return result
        except RatingConflict as exc:
            raise HTTPException(409,'Forward pair rating changed') from exc

    @router.post("/api/session/{session_id}/native-rating")
    async def native_rating(request: Request, session_id: str):
        from src.auth_helpers import require_user, is_internal_tool_identity
        from src.openclank.chat_ratings import native_target, RatingConflict, RatingUnavailable
        from routes.orchestration_routes import _get_observation_binding, _ensure_observation_binding_current
        from src.openclank.transcript_projection import get_managed_binding
        from routes.prefs_routes import PREFERENCE_LOCK
        from sqlalchemy import text
        body=await request.json()
        if type(body) is not dict or set(body)!={'kind','segments','part_id','payload'} or body['kind'] not in ('message','chat','section'):
            raise HTTPException(400,'Exact native target required')
        user=require_user(request)
        if is_internal_tool_identity(user) or (body['payload'] is not None and (getattr(request.state,'api_token',False) or getattr(request.state,'internal_tool_authenticated',False))):
            raise HTTPException(403,'An interactive human is required')
        auth=getattr(request.app.state,'auth_manager',None);barrier=getattr(request.app.state,'account_request_barrier',None)
        account=auth.account_id(user) if auth else None
        if not account or auth.username_for_account_id(account)!=user or barrier is None:raise HTTPException(503,'Native rating unavailable')
        try:
            async with barrier.admitted(user):
                owner,supervisor,binding,mapping=await asyncio.to_thread(_get_observation_binding,request,session_id,None,session_manager)
                if owner!=user:raise RatingUnavailable('native rating unavailable')
                async with mapping.async_chat_admission_lock(session_id,blocking=False,existing_only=True) as guard:
                    authority=await asyncio.to_thread(get_managed_binding,session_id,owner=owner)
                    cwd=authority.get('physicalCwd');pool=supervisor.owner_lifecycle_pool()
                    key=pool._key(owner);state=pool._states.get(key);worker=state.active if state else None
                    bridge=getattr(worker,'_bridge',None);holder=getattr(getattr(worker,'_managed_callbacks',None),'holder_id',None)
                    context=(getattr(bridge,'_session_context',{}) or {}).get(binding.engine_session_id) or {}
                    if (not state or state.status!='ready' or not worker or not worker.is_alive() or not holder or not bridge or context.get('incognito') or context.get('auxiliary')
                        or authority.get('engineSessionID')!=binding.engine_session_id or authority.get('mappingRevision')!=binding.binding_revision
                        or authority.get('transition') is not None or not isinstance(cwd,str)):
                        raise RatingUnavailable('native rating unavailable')
                    generation=state.generation
                    def current():
                        try:
                            mapping.validate_chat_guard(session_id,guard)
                            _ensure_observation_binding_current(mapping,owner,session_id,binding)
                            return (auth.account_id(owner)==account and auth.username_for_account_id(account)==owner and _verify_session_owner(request,session_id,session_manager)==owner
                                and get_managed_binding(session_id,owner=owner)==authority
                                and pool._states.get(key) is state and state.active is worker and state.status=='ready' and state.generation==generation and worker.is_alive()
                                and worker._bridge is bridge and getattr(getattr(worker,'_managed_callbacks',None),'holder_id',None)==holder
                                and not getattr(session_manager.sessions.get(session_id),'incognito',False))
                        except Exception:return False
                    async def source():
                        if not await asyncio.to_thread(current):raise RatingUnavailable('native rating unavailable')
                        response=await pool.session_rating_source_request(session_id,binding.engine_session_id,cwd,{'stableChatID':session_id},owner=owner)
                        if response.status_code!=200 or len(response.content)>1600000 or not await asyncio.to_thread(current):raise RatingUnavailable('native rating unavailable')
                        value=response.json()
                        if type(value) is not dict or value.get('sessionID')!=binding.engine_session_id:raise RatingUnavailable('native rating unavailable')
                        return value
                    first=await source()
                    if body['payload'] is not None:
                        second=await source()
                        if first!=second:raise RatingConflict('native source changed')
                        first=second
                    def commit():
                        with PREFERENCE_LOCK:
                            db=SessionLocal()
                            try:
                                db.execute(text('BEGIN IMMEDIATE' if body['payload'] is not None else 'BEGIN'))
                                if not current():raise RatingUnavailable('native rating unavailable')
                                result=native_target(db,owner=owner,account=account,chat_id=session_id,auth=auth,snapshot=first,kind=body['kind'],segments=body['segments'],part_id=body['part_id'],payload=body['payload'])
                                if not current():raise RatingUnavailable('native rating unavailable')
                                if body['payload'] is None:db.rollback()
                                else:db.commit()
                                return result
                            except BaseException:db.rollback();raise
                            finally:db.close()
                    task=asyncio.create_task(asyncio.to_thread(commit))
                    try:return await asyncio.shield(task)
                    except asyncio.CancelledError:
                        while not task.done():
                            try:await asyncio.shield(task)
                            except asyncio.CancelledError:continue
                        task.result();raise
        except RatingConflict as exc:raise HTTPException(409,'Native rating changed') from exc
        except Exception as exc:raise HTTPException(503,'Native rating unavailable') from exc

    @router.get("/api/session/{session_id}/message-source/{message_id}")
    async def message_source(request: Request, session_id: str, message_id: str):
        _verify_session_owner(request, session_id)
        from sqlalchemy import text
        from src.openclank.chat_source_bridge import available, digest
        with SessionLocal() as db:
            row = db.query(DbChatMessage).filter(DbChatMessage.session_id == session_id, DbChatMessage.id == message_id).first()
            if row is None:
                raise HTTPException(404, "Message not found")
            revision = None
            if available(db):
                source = db.execute(text("SELECT source_body_revision,source_body_hash FROM chat_messages WHERE session_id=:chat AND id=:message"), {"chat": session_id, "message": message_id}).first()
                if source and source[0] is not None and source[1] == digest(row.content):
                    revision = source[0]
            return {"content": row.content, "source_revision": revision}

    @router.post("/api/session/{session_id}/edit-message")
    async def edit_message(request: Request, session_id: str):
        """Edit the content of a message by its database ID."""
        _verify_session_owner(request, session_id)
        await _prepare_context_mutation(request, session_id)
        try:
            body = await request.json()
            msg_id = body.get("msg_id")
            content = body.get("content")
            if not isinstance(msg_id, str) or not msg_id or content is None:
                raise HTTPException(400, "msg_id and content are required")

            _reserve_message_uploads(request, content)

            from src.openclank.chat_source_bridge import guarded_source_mutation
            owner = _verify_session_owner(request, session_id)
            def edit_selection():
                from src.openclank.chat_source_bridge import origin_available
                from src.openclank.conversation_archive import get_conversation_archive
                from sqlalchemy import text
                with SessionLocal() as check:
                    message = check.query(DbChatMessage).filter(DbChatMessage.id == msg_id, DbChatMessage.session_id == session_id).first()
                    if message is None:
                        raise HTTPException(404, "Message not found")
                    roots = ()
                    if message.role == 'assistant' and origin_available(check):
                        account = get_conversation_archive().resolve_owner(owner)
                        roots = tuple(sorted(item[0] for item in check.execute(text("SELECT message_id FROM host_source_delivery_intent WHERE owner=:owner AND chat_id=:chat AND part_id='body' AND revision=1 AND origin_assistant_id=:message"), {'owner':account,'chat':session_id,'message':msg_id})))
                    return message.role, message.content, roots
            initial_selection = edit_selection()
            def pre_fence():
                from src.openclank.chat_source_bridge import available, digest
                if edit_selection() != initial_selection:
                    raise HTTPException(409, "Message changed; refresh and try again")
                from sqlalchemy import text
                if _verify_session_owner(request, session_id) != owner:
                    raise HTTPException(409, "Session owner changed")
                with SessionLocal() as check:
                    message = check.query(DbChatMessage).filter(DbChatMessage.id == msg_id, DbChatMessage.session_id == session_id).first()
                    if message is None:
                        raise HTTPException(404, "Message not found")
                    if available(check):
                        baseline = check.execute(text("SELECT source_body_revision,source_body_hash FROM chat_messages WHERE session_id=:chat AND id=:message"), {"chat":session_id,"message":msg_id}).first()
                        if baseline and (baseline[0] is not None or baseline[1] is not None):
                            expected=body.get("expected_revision")
                            if type(expected) is not int or expected != baseline[0] or baseline[1] != digest(message.content):
                                raise HTTPException(409, "Message source changed; refresh and try again")
            async with guarded_source_mutation(request, owner=owner, chat_id=session_id, message_ids=tuple(sorted({msg_id, *initial_selection[2]})), pre_fence=pre_fence, _bulk=True) as _source_guard:
                pre_fence()
                if _verify_session_owner(request, session_id) != owner:
                    raise HTTPException(409, "Session owner changed")
                session = session_manager.get_session(session_id)
                db = SessionLocal()
                try:
                    if _source_guard is not None and db.get_bind().dialect.name == 'sqlite':
                        db.connection().exec_driver_sql('BEGIN IMMEDIATE')
                    if _source_guard is not None:
                        db.query(DbSession).filter(DbSession.id == session_id).with_for_update().first()
                        session_manager._lock_source_intents(db, session_id)
                    db_msg = db.query(DbChatMessage).filter(
                        DbChatMessage.id == msg_id,
                        DbChatMessage.session_id == session_id,
                    ).first()
                    if not db_msg:
                        raise HTTPException(404, "Message not found")
                    if (db_msg.role, db_msg.content) != initial_selection[:2]:
                        raise HTTPException(409, "Message changed; refresh and try again")
                    if db_msg.role == 'assistant':
                        from src.openclank.chat_source_bridge import withdraw_deleted_messages, origin_available
                        from src.openclank.conversation_archive import get_conversation_archive
                        if origin_available(db):
                            archive = get_conversation_archive()
                            account = archive.resolve_owner(owner)
                            from sqlalchemy import text
                            current_roots = tuple(sorted(item[0] for item in db.execute(text("SELECT message_id FROM host_source_delivery_intent WHERE owner=:owner AND chat_id=:chat AND part_id='body' AND revision=1 AND origin_assistant_id=:message"), {'owner':account,'chat':session_id,'message':msg_id})))
                            if current_roots != initial_selection[2]:
                                raise HTTPException(409, "Message source changed; refresh and try again")
                            withdraw_deleted_messages(db, archive, owner=account, chat_id=session_id, message_ids=(msg_id,))

                    from src.openclank.chat_source_bridge import available, edit, deliver
                    from src.openclank.conversation_archive import get_conversation_archive
                    from sqlalchemy import text
                    source_enabled = available(db)
                    baseline = None
                    if source_enabled:
                        baseline = db.execute(text("SELECT source_body_revision, source_body_hash FROM chat_messages WHERE id=:message AND session_id=:chat"), {"message": msg_id, "chat": session_id}).first()
                    meta = {}
                    if db_msg.meta_data:
                        try: meta = json.loads(db_msg.meta_data)
                        except (json.JSONDecodeError, ValueError): pass
                    meta['edited'] = True
                    revision = None
                    if baseline and (baseline[0] is not None or baseline[1] is not None):
                        source_session = db.query(DbSession).filter(DbSession.id == session_id).first()
                        archive = get_conversation_archive()
                        owner = archive.resolve_owner(source_session.owner)
                        try:
                            revision = edit(db, archive=archive, owner=owner, chat_id=session_id, message_id=msg_id,
                                            expected_revision=body.get("expected_revision"), body=content)
                        except ValueError:
                            raise HTTPException(409, "Message source changed; refresh and try again")
                        # Raw SQL keeps the ORM hook from double-bumping the revision.
                        db.execute(text('UPDATE chat_messages SET metadata=:metadata WHERE id=:message AND session_id=:chat'), {"metadata": json.dumps(meta), "message": msg_id, "chat": session_id})
                    else:
                        db_msg.content = content
                        db_msg.meta_data = json.dumps(meta)
                    db.commit()
                    # Never publish an in-memory edit before its canonical commit.
                    for hmsg in session.history:
                        hmeta = hmsg.metadata if isinstance(hmsg, ChatMessage) else hmsg.get('metadata')
                        if isinstance(hmeta, dict) and hmeta.get('_db_id') == msg_id:
                            if isinstance(hmsg, ChatMessage):
                                hmsg.content = content
                                hmsg.metadata['edited'] = True
                            elif isinstance(hmsg, dict):
                                hmsg['content'] = content
                                hmsg['metadata']['edited'] = True
                            break
                    delivery = "unavailable"
                    try:
                        if revision is not None:
                            delivery = deliver(db, archive, owner=owner, chat_id=session_id, message_id=msg_id, revision=revision)
                    except Exception:
                        db.rollback()
                        logger.warning("host source delivery pending", exc_info=True)
                        delivery = "pending"
                    return {"status": "ok", "source_revision": revision, "archive_delivery": delivery}
                finally:
                    db.close()
        except HostSourceFenceUnavailable:
            raise HTTPException(503, "Memory source transition is unavailable")
        except KeyError:
            raise HTTPException(404, "Session not found")
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Edit message error {session_id}: {e}")
            raise HTTPException(500, str(e))

    @router.post("/api/session/{session_id}/mark-stopped")
    async def mark_stopped(request: Request, session_id: str):
        """Mark the last assistant message as stopped by user."""
        _verify_session_owner(request, session_id)
        try:
            session = session_manager.get_session(session_id)
            # Find last assistant message and add stopped metadata
            for msg in reversed(session.history):
                if (isinstance(msg, ChatMessage) and msg.role == 'assistant') or \
                   (isinstance(msg, dict) and msg.get('role') == 'assistant'):
                    if isinstance(msg, ChatMessage):
                        if not msg.metadata:
                            msg.metadata = {}
                        msg.metadata['stopped'] = True
                        if not msg.metadata.get('model'):
                            msg.metadata['model'] = session.model
                    else:
                        if 'metadata' not in msg:
                            msg['metadata'] = {}
                        msg['metadata']['stopped'] = True
                        if not msg['metadata'].get('model'):
                            msg['metadata']['model'] = session.model
                    break
            # Also update in DB
            db = SessionLocal()
            try:
                import json as _json
                db_messages = (
                    db.query(DbChatMessage)
                    .filter(DbChatMessage.session_id == session_id, DbChatMessage.role == 'assistant')
                    .order_by(DbChatMessage.timestamp.desc())
                    .first()
                )
                if db_messages:
                    meta = {}
                    if db_messages.meta_data:
                        try:
                            meta = _json.loads(db_messages.meta_data)
                        except (json.JSONDecodeError, ValueError):
                            pass
                    meta['stopped'] = True
                    if not meta.get('model'):
                        meta['model'] = session.model
                    db_messages.meta_data = _json.dumps(meta)
                    db.commit()
            finally:
                db.close()
            session_manager.save_sessions()
            return {"status": "ok"}
        except KeyError:
            raise HTTPException(404, "Session not found")
        except Exception as e:
            logger.error(f"Mark stopped error {session_id}: {e}")
            raise HTTPException(500, str(e))

    @router.post("/api/session/{session_id}/update-last-meta")
    async def update_last_meta(request: Request, session_id: str):
        """Merge metadata into the last assistant message (e.g. save variants)."""
        _verify_session_owner(request, session_id)
        try:
            body = await request.json()
            meta_update = body.get("metadata", {})
            session = session_manager.get_session(session_id)
            message_id = body.get('message_id')
            if message_id is not None:
                expected_hash = body.get('expected_hash')
                expected_content = body.get('expected_content')
                if (not isinstance(message_id,str) or not message_id or not isinstance(meta_update,dict)
                    or not ((isinstance(expected_hash,str) and re.fullmatch(r'[0-9a-f]{64}',expected_hash)) or isinstance(expected_content,str))):
                    raise HTTPException(400,'Message target is invalid')
                db = SessionLocal()
                try:
                    if db.get_bind().dialect.name == 'sqlite':
                        db.connection().exec_driver_sql('BEGIN IMMEDIATE')
                    row = db.query(DbChatMessage).filter_by(id=message_id,session_id=session_id,role='assistant').with_for_update().first()
                    if row is None or not isinstance(row.content,str) or (expected_hash and hashlib.sha256(row.content.encode()).hexdigest() != expected_hash) or (expected_hash is None and row.content != expected_content):
                        raise HTTPException(409,'Message changed; refresh and try again')
                    try:
                        metadata = json.loads(row.meta_data or '{}')
                    except (ValueError,TypeError):
                        metadata = {}
                    if not isinstance(metadata,dict): metadata = {}
                    metadata.update(meta_update)
                    row.meta_data = json.dumps(metadata)
                    db.commit()
                finally:
                    db.close()
                for item in session.history:
                    meta = item.metadata if isinstance(item,ChatMessage) else item.get('metadata')
                    value = item.content if isinstance(item,ChatMessage) else item.get('content')
                    if isinstance(meta,dict) and meta.get('_db_id') == message_id and isinstance(value,str) and ((expected_hash and hashlib.sha256(value.encode()).hexdigest() == expected_hash) or (expected_hash is None and value == expected_content)):
                        meta.update(meta_update)
                        break
                return {'status':'ok','message_id':message_id}

            # Update in-memory
            for msg in reversed(session.history):
                if (isinstance(msg, ChatMessage) and msg.role == 'assistant') or \
                   (isinstance(msg, dict) and msg.get('role') == 'assistant'):
                    if isinstance(msg, ChatMessage):
                        if not msg.metadata:
                            msg.metadata = {}
                        msg.metadata.update(meta_update)
                    else:
                        if 'metadata' not in msg:
                            msg['metadata'] = {}
                        msg['metadata'].update(meta_update)
                    break

            # Update in DB
            db = SessionLocal()
            try:
                import json as _json
                db_msg = (
                    db.query(DbChatMessage)
                    .filter(DbChatMessage.session_id == session_id, DbChatMessage.role == 'assistant')
                    .order_by(DbChatMessage.timestamp.desc())
                    .first()
                )
                if db_msg:
                    meta = {}
                    if db_msg.meta_data:
                        try: meta = _json.loads(db_msg.meta_data)
                        except (json.JSONDecodeError, ValueError): pass
                    meta.update(meta_update)
                    db_msg.meta_data = _json.dumps(meta)
                    db.commit()
            finally:
                db.close()
            session_manager.save_sessions()
            return {"status": "ok"}
        except HTTPException:
            raise
        except KeyError:
            raise HTTPException(404, "Session not found")
        except Exception as e:
            logger.error(f"Update last meta error {session_id}: {e}")
            raise HTTPException(500, str(e))

    @router.post("/api/session/{session_id}/merge-last-assistant")
    async def merge_last_assistant(request: Request, session_id: str):
        """Merge the last two assistant messages into one (for continue)."""
        _verify_session_owner(request, session_id)
        from src.openclank.chat_source_bridge import canonical_source_provider
        guarded_provider = canonical_source_provider(request.app.state)
        guarded = guarded_provider is not None
        await _prepare_context_mutation(request, session_id)
        try:
            body = await request.json()
        except Exception as exc:
            raise HTTPException(400, 'Invalid JSON') from exc
        if not isinstance(body,dict):
            raise HTTPException(400, 'Invalid JSON')
        supplied_ids = 'left_id' in body or 'right_id' in body
        if guarded or supplied_ids:
            try:
                separator = body.get('separator', '\n\n')
                if not isinstance(separator,str) or len(separator) > 4096:
                    raise HTTPException(400, 'Invalid separator')
                session = session_manager.get_session(session_id)
                history = list(session.history)
                assistant_indices = [i for i,item in enumerate(history) if _message_role(item) == 'assistant']
                if len(assistant_indices) < 2:
                    return {'status':'ok','merged':False}
                left, right = assistant_indices[-2:]
                def identity(item):
                    meta = item.metadata if isinstance(item,ChatMessage) else item.get('metadata')
                    return str(getattr(item,'persistence_id',None) or (meta or {}).get('_db_id') or '')
                if (not isinstance(body.get('left_id'),str) or not isinstance(body.get('right_id'),str)
                    or identity(history[left]) != body['left_id'] or identity(history[right]) != body['right_id']):
                    raise HTTPException(409,'Message selection changed; refresh and try again')
                chosen = [left,right]
                if right - 1 > left and _message_role(history[right-1]) == 'user' and 'previous response was interrupted' in _message_text(history[right-1]):
                    chosen.append(right-1)
                expectations = []
                for idx in sorted(chosen):
                    item = history[idx]; mid = identity(item); role = _message_role(item); text_value = _message_text(item)
                    if not mid or not isinstance(text_value,str):
                        raise HTTPException(409,'Message identity unavailable; refresh and try again')
                    expectations.append((mid,role,hashlib.sha256(text_value.encode()).hexdigest()))
                content = _message_text(history[left]) + separator + _message_text(history[right])
                new_id = str(uuid.uuid4())
                derived = ChatMessage(role='assistant',content=content,persistence_id=new_id,
                    metadata={'presentation_provenance':{'kind':'continue_merge','sources':[{'id':mid,'sha256':digest} for mid,_role,digest in expectations]}})
                proposal = [derived if i == left else item for i,item in enumerate(history) if i not in set(chosen) or i == left]
                await session_manager.replace_messages_guarded(session_id,proposal,_expected_sources=expectations,_required_provider=guarded_provider)
                return {'status':'ok','merged':True,'message_id':new_id,
                    'source_message_ids':[expectations[0][0],identity(history[right])]}
            except HTTPException:
                raise
            except Exception as exc:
                raise HTTPException(409,'Message changed; refresh and try again') from exc
        try:
            if canonical_source_provider(request.app.state) is not None:
                raise HTTPException(409, 'Memory source authority changed; refresh and try again')
            separator = body.get("separator", "\n\n")
            session = session_manager.get_session(session_id)

            with session._history_lock:
                # Find last two assistant messages in-memory
                ai_indices = []
                for i, msg in enumerate(session.history):
                    role = msg.role if isinstance(msg, ChatMessage) else msg.get('role', '')
                    if role == 'assistant':
                        ai_indices.append(i)

                if len(ai_indices) < 2:
                    return {"status": "ok", "merged": False}

                idx1, idx2 = ai_indices[-2], ai_indices[-1]
                msg1, msg2 = session.history[idx1], session.history[idx2]

                content1 = msg1.content if isinstance(msg1, ChatMessage) else msg1.get('content', '')
                content2 = msg2.content if isinstance(msg2, ChatMessage) else msg2.get('content', '')
                merged_content = content1 + separator + content2

                # Merge metadata
                meta1 = (msg1.metadata if isinstance(msg1, ChatMessage) else msg1.get('metadata')) or {}
                meta2 = (msg2.metadata if isinstance(msg2, ChatMessage) else msg2.get('metadata')) or {}
                merged_meta = {**meta1, **meta2}
                merged_meta.pop('stopped', None)  # no longer stopped after continue

                # Update first message, remove second
                if isinstance(msg1, ChatMessage):
                    msg1.content = merged_content
                    msg1.metadata = merged_meta
                else:
                    msg1['content'] = merged_content
                    msg1['metadata'] = merged_meta

                # Also remove the hidden "continue" user message between them if present
                # It's the message at idx2-1 if it's a user message with continue text
                remove_indices = [idx2]
                if idx2 - 1 > idx1:
                    between = session.history[idx2 - 1]
                    between_role = between.role if isinstance(between, ChatMessage) else between.get('role', '')
                    between_content = between.content if isinstance(between, ChatMessage) else between.get('content', '')
                    if between_role == 'user' and 'previous response was interrupted' in between_content:
                        remove_indices.insert(0, idx2 - 1)

                for ri in sorted(remove_indices, reverse=True):
                    session.history.pop(ri)

                # Update DB
                db = SessionLocal()
                try:
                    import json as _json
                    db_messages = (
                        db.query(DbChatMessage)
                        .filter(DbChatMessage.session_id == session_id)
                        .order_by(DbChatMessage.timestamp)
                        .all()
                    )
                    # Find last two assistant messages in DB
                    ai_db = [(i, m) for i, m in enumerate(db_messages) if m.role == 'assistant']
                    if len(ai_db) >= 2:
                        (_, db1), (_, db2) = ai_db[-2], ai_db[-1]
                        db1.content = merged_content
                        db1.meta_data = _json.dumps(merged_meta)

                        # Mirror the in-memory deletion: remove the second assistant
                        # message and ONLY the "continue" user message between them
                        # (not arbitrary tool/system/user rows). The old
                        # range-delete destroyed every row between the two assistant
                        # messages, desyncing the DB from the in-memory history.
                        for _row in _merge_continue_rows_to_delete(db_messages, db1, db2):
                            db.delete(_row)

                        db.commit()
                finally:
                    db.close()
                session_manager.save_sessions()
                return {"status": "ok", "merged": True}
        except HTTPException:
            raise
        except KeyError:
            raise HTTPException(404, "Session not found")
        except Exception as e:
            logger.error(f"Merge assistant error {session_id}: {e}")
            raise HTTPException(500, str(e))

    @router.post("/api/session/{session_id}/fork")
    async def fork_session(request: Request, session_id: str):
        """Copy a non-Native transcript into a new Host chat."""
        user = _verify_session_owner(request, session_id)
        try:
            body = await request.json()
            if not isinstance(body, dict):
                raise HTTPException(400, "Transcript copy request must be an object")
            keep_count = body.get("keep_count", 0)
            message_id = body.get("host_message_id")
            side = body.get("boundary")
            revision = body.get("expected_transcript_revision")
            if (type(keep_count) is not int or ("expected_transcript_revision" in body and type(revision) is not int)
                    or (message_id is not None and (not isinstance(message_id, str) or not message_id
                                                   or side not in ("before", "through") or revision is None))):
                raise HTTPException(400, "Invalid transcript copy boundary")

            from src.openclank.response_style import style_snapshot
            from src.openclank.session_file_history import canonical_session_current
            from src.openclank.session_fork import fork_source_kind

            source = session_manager.get_session(session_id, allow_pending_history=True)
            with SessionLocal() as db:
                row = db.query(DbSession).filter(DbSession.id == session_id, DbSession.owner == user).first()
                if row is None or not canonical_session_current(session_manager, sid=session_id, owner=user, workspace_id=row.workspace_id):
                    raise HTTPException(404, "Original transcript is unavailable")
                if fork_source_kind(row) != "transcript-copy":
                    raise HTTPException(409, "Native chats must use the Native fork owner")
                if revision is not None and revision != int(row.transcript_revision or 0):
                    raise HTTPException(409, "The source transcript changed. Select the source again.")
                source_style = style_snapshot(row.response_style, row.response_style_revision)['mode']
            history = list(source.history)
            if message_id is not None:
                matching = [index for index, msg in enumerate(history) if (msg.metadata or {}).get("_db_id") == message_id]
                if len(matching) != 1:
                    raise HTTPException(409, "The selected source message is unavailable")
                keep_count = matching[0] + (side == "through")
            # Create new session
            new_id = str(uuid.uuid4())
            fork_name = f"\u2ADD {source.name}"
            new_session = session_manager.create_session(
                session_id=new_id,
                name=fork_name,
                endpoint_url=source.endpoint_url,
                model=source.model,
                endpoint_id=getattr(source, "endpoint_id", None),
                rag=False,
                owner=getattr(source, 'owner', None),
                response_style=source_style,
            )

            # Copy messages up to keep_count
            msgs_to_copy = history[:keep_count]
            for msg in msgs_to_copy:
                # Copy the metadata dict. Sharing it would let the fork's
                # persistence (add_message -> _persist_message stamps
                # _db_id/timestamp onto the dict) mutate the SOURCE session's
                # in-memory messages, corrupting their _db_id and breaking
                # edit/delete-by-id on the original conversation.
                meta = dict(msg.metadata) if isinstance(msg.metadata, dict) else None
                new_session.add_message(ChatMessage(msg.role, msg.content, meta))
            try:
                from src.event_bus import fire_event
                fire_event("session_created", getattr(source, 'owner', None))
            except Exception:
                logger.debug("session_created event dispatch failed", exc_info=True)

            return {
                "status": "ok",
                "kind": "transcript-copy",
                "id": new_id,
                "name": fork_name,
                "kept": len(msgs_to_copy),
            }
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Fork error {session_id}: {e}")
            raise HTTPException(500, str(e))

    @router.get("/api/conversations/topics")
    async def get_conversation_topics(request: Request) -> Dict[str, Any]:
        from src.auth_helpers import require_user
        user = require_user(request)
        try:
            return analyze_topics(session_manager, owner=user or None)
        except Exception as e:
            raise HTTPException(500, f"Topic analysis failed: {e}")

    @router.get("/api/session/{session_id}/context")
    async def get_session_context_usage(request: Request, session_id: str) -> Dict[str, Any]:
        """Return an estimated whole-chat context usage for the session's model.

        Streaming footers report the prompt size for the last request. This
        endpoint estimates the persisted session context so the header can show
        when the whole chat is approaching compaction.
        """
        context_owner = _verify_session_owner(request, session_id)
        try:
            session = session_manager.get_session(session_id)
        except KeyError:
            raise HTTPException(404, "Session not found")

        try:
            from src.model_context import estimate_tokens, get_context_length

            messages = session.get_context_messages()
            used = int(estimate_tokens(messages))
            ctx_len = int(get_context_length(session.endpoint_url, session.model) or 0)
            pct = round((used / ctx_len) * 100, 1) if ctx_len else 0.0
            pct = max(0.0, min(100.0, pct))
            visible_messages = sum(
                1 for m in session.history
                if not (getattr(m, "metadata", None) or {}).get("hidden")
            )
            compacted_messages = sum(
                1 for m in session.history
                if (getattr(m, "metadata", None) or {}).get("compacted")
            )
            can_compact = used > 0
            try:
                last_request = None if getattr(session, "incognito", False) else await _bounded_history_io(
                    lambda: _last_proven_main_context(context_owner, session_id), timeout=1.5)
            except Exception:
                last_request = None
            return {
                "session_id": session_id,
                "model": session.model,
                "endpoint_url": session.endpoint_url,
                "used_tokens": used,
                "projection_estimate": {"tokens": used, "state": "estimated", "source": "persisted_host_projection"},
                "last_request": last_request,
                "unsent_tail": {"state": "unavailable"},
                "context_length": ctx_len,
                "context_percent": pct,
                "messages": visible_messages,
                "context_messages": len(messages),
                "compacted_messages": compacted_messages,
                "can_compact": can_compact,
                "should_compact": pct >= 70,
                "auto_compact_threshold": 85,
            }
        except Exception as e:
            logger.error(f"Context usage error {session_id}: {e}")
            raise HTTPException(500, str(e))

    @router.post("/api/session/{session_id}/compact")
    async def compact_session(request: Request, session_id: str):
        """Manually trigger context compaction for a session."""
        verified_owner = _verify_session_owner(request, session_id)
        from src.auth_helpers import effective_user
        owner = effective_user(request)
        try:
            session = session_manager.get_session(session_id)
        except KeyError:
            raise HTTPException(404, "Session not found")
        # Persisted owner remains authoritative after authenticated scope check.
        owner = getattr(session, "owner", None) or owner

        # One active compactor per execution context: refuse host compaction
        # when a persistent ACP engine compactor owns the session. Bind on the
        # real protocol (model_target.transport), not missing Session attrs.
        # Checked before any context-mutation side effects.
        from src.context_compactor import session_has_persistent_engine

        if session_has_persistent_engine(session):
            raise HTTPException(
                409,
                "Persistent ACP sessions compact through the engine "
                "(session.summarize); host compaction is reserved for "
                "finite contexts.",
            )
        mutation_owner = await _prepare_context_mutation(
            request, session_id, session_owner=getattr(session, "owner", None),
            verified_owner=verified_owner,
        )
        owner = mutation_owner or getattr(session, "owner", None) or owner
        try:
            from src.model_context import estimate_tokens, get_context_length
            from src.openclank.modality_facade import complete_text

            from core.session_manager import _history_fingerprint
            with session._history_lock:
                original_history = _history_fingerprint(session)

            system_prefix = _leading_system_prefix(session.history)
            conversation = session.history[len(system_prefix):]
            keep_count = min(8, max(4, len(conversation) // 4))
            older = conversation[:-keep_count]
            if not older:
                return {"status": "ok", "message": "Not enough messages to compact"}

            ctx_len = get_context_length(session.endpoint_url, session.model)
            messages_before = session.get_context_messages()
            used_before = estimate_tokens(messages_before)
            pct_before = round((used_before / ctx_len) * 100, 1) if ctx_len else 0
            msg_count_before = len(session.history)

            # Keep only last 4 conversation messages, summarize the rest.
            # System/persona authority is split out before this boundary.
            recent = conversation[-keep_count:]

            # Build text to summarize
            convo_text = "\n".join(
                f"{_message_role(m).upper()}: "
                f"{_message_text(m)[:2000]}"
                for m in older
            )

            from src.context_compactor import SELF_SUMMARY_SYSTEM_PROMPT, normalize_compaction_summary
            compaction_count = sum(1 for m in session.history if isinstance(m, ChatMessage) and "[Conversation summary" in (m.content or ""))
            sys_prompt = SELF_SUMMARY_SYSTEM_PROMPT.replace("{count}", str(len(older))).replace("{n}", str(compaction_count + 1))
            import hashlib

            summary = await complete_text(
                owner=owner,
                purpose="utility",
                messages=[
                    {"role": "system", "content": sys_prompt},
                    {"role": "user", "content": convo_text},
                ],
                temperature=0.2,
                max_output_tokens=1024,
                idempotency_key=(
                    "history-compact-"
                    + hashlib.sha256(
                        f"{session_id}\0{convo_text}".encode("utf-8")
                    ).hexdigest()[:32]
                ),
            )
            summary = normalize_compaction_summary(summary)
            if not (summary or "").strip():
                raise HTTPException(502, "Compaction summary was empty; history unchanged")

            # Replace session history: preserve the leading system/persona
            # prefix, then add the summary and recent conversation.  The
            # prefix is active authority, not source text to summarize away.
            system_summary = ChatMessage(
                role="system",
                content=f"[Conversation summary — {len(older)} earlier messages were compacted]\n\n{summary}",
                metadata={
                    "compacted": True,
                    "hidden": True,
                    "compaction_trigger": "manual",
                    "compaction_actor": "host_finite",
                    "source_boundary_split": len(older),
                },
            )
            # Visible assistant message just shows stats
            summary_msg = ChatMessage(
                role="assistant",
                content=f"**Conversation compacted** — {len(older)} messages summarized, {len(recent)} kept.",
                metadata={"compacted": True, "messages_removed": len(older)},
            )
            new_history = system_prefix + [system_summary, summary_msg] + list(recent)
            # Projection writer: archives source parts first, preserves
            # retained message IDs, never rekeys original history identity.
            # Archive failure raises recoverable-unavailable and leaves source
            # intact — the broad except below must not swallow that as 500.
            try:
                replaced = await session_manager.replace_messages_guarded(session_id, new_history, _expected_history=original_history)
            except Exception as exc:
                from src.openclank.conversation_archive import ArchiveUnavailableError

                if isinstance(exc, ArchiveUnavailableError):
                    raise HTTPException(
                        503,
                        "conversation archive unavailable; history unchanged",
                    ) from exc
                raise
            if not replaced:
                raise HTTPException(500, "Failed to save compacted history")
            logger.info(f"Compact: session {session_id} projection committed with {len(new_history)} messages (was {msg_count_before})")

            session_manager.save_sessions()

            used_after = estimate_tokens(session.get_context_messages())
            pct_after = round((used_after / ctx_len) * 100, 1) if ctx_len else 0

            return {
                "status": "ok",
                "message": f"Compacted: {msg_count_before} msgs → {len(session.history)} msgs ({pct_before}% → {pct_after}%)",
                "before": pct_before,
                "after": pct_after,
                "ok": True,
                "summarized": len(older),
                "kept": len(recent),
                "message_count": len(session.history),
                "projection": "host_finite",
                "source_parts_retained": True,
                "activity_boundary": _graph_context_boundary("compact"),
            }

        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Manual compact error {session_id}: {e}")
            raise HTTPException(500, str(e))

    return router


def setup_history_settings_routes() -> APIRouter:
    """Authenticated policy/usage API for the Lore history foundation."""
    import os
    from src.auth_helpers import effective_user, get_current_user, require_user
    from src.openclank import file_policy as file_policy_module
    from src.openclank.filesystem_registry import FilesystemRootRegistry
    from src.openclank.history_client import (
        HistoryAvailabilityError,
        HistoryClient,
        HistoryClientError,
        registered_resource_pages,
    )
    from src.openclank.history_paths import scope_id
    from fastapi.routing import APIRoute
    from fastapi.responses import JSONResponse
    from fastapi.exceptions import RequestValidationError

    class _StrictHistoryVersionRoute(APIRoute):
        def get_route_handler(self):
            original=super().get_route_handler()
            async def handler(request):
                strict=str(request.query_params.get('strict_account','')).lower() in {'1','true','yes','on'}
                if not strict:return await original(request)
                headers={'Cache-Control':'private, no-store'}
                try:
                    response=await original(request)
                    response.headers['Cache-Control']=headers['Cache-Control']
                    return response
                except HTTPException as exc:
                    return JSONResponse({'detail':exc.detail},status_code=exc.status_code,headers={**(exc.headers or {}),**headers})
                except RequestValidationError:
                    return JSONResponse({'detail':'Invalid History request'},status_code=422,headers=headers)
                except Exception:
                    return JSONResponse({'detail':'History is unavailable'},status_code=503,headers=headers)
            return handler

    class _PrivateHistoryRoute(APIRoute):
        def get_route_handler(self):
            original=super().get_route_handler()
            async def handler(request):
                headers={'Cache-Control':'private, no-store'}
                try:
                    response=await original(request);response.headers['Cache-Control']=headers['Cache-Control'];return response
                except HTTPException as exc:return JSONResponse({'detail':exc.detail},status_code=exc.status_code,headers={**(exc.headers or {}),**headers})
                except RequestValidationError:return JSONResponse({'detail':'Invalid History request'},status_code=422,headers=headers)
                except Exception:return JSONResponse({'detail':'History is unavailable'},status_code=503,headers=headers)
            return handler

    router = APIRouter(prefix="/api/history", tags=["history-settings"])

    def is_admin(request: Request, user: str) -> bool:
        manager = getattr(request.app.state, "auth_manager", None)
        return bool(manager and user and manager.is_admin(user))

    def account_id_for(request: Request, user: str) -> str:
        manager = getattr(request.app.state, "auth_manager", None)
        resolver = getattr(manager, "account_id", None)
        if callable(resolver) and user:
            try:
                resolved = resolver(user)
                if resolved:
                    return str(resolved)
            except Exception:
                pass
        return str(user or "local-installation")

    def authorized_roots(owner: str, admin: bool) -> list[dict[str, Any]]:
        registry = FilesystemRootRegistry()
        roots: list[dict[str, Any]] = []
        if admin:
            roots.extend(registry.list(owner))
        roots.extend(
            item.get("root") or {}
            for item in registry.visibility_for_subject(owner)
        )
        return [
            root for root in roots
            if root.get("kind") == "recursive_directory"
            and root.get("enabled")
            and root.get("availability") == "available"
            and "read" in set(root.get("capabilities") or [])
        ]

    def is_authorized_directory(value: str, roots: list[dict[str, Any]]) -> bool:
        import os
        candidate = os.path.realpath(os.path.expanduser(value))
        for root in roots:
            base = os.path.realpath(str(root.get("canonical_path") or ""))
            try:
                if base and os.path.commonpath((base, candidate)) == base:
                    return True
            except ValueError:
                continue
        return False

    def service_client(account_id: str, request: Request) -> HistoryClient:
        socket_path = os.environ.get("OPENCLANK_HISTORY_SOCKET")
        if not socket_path:
            raise HTTPException(503, "history service is unavailable")
        supervisor = getattr(request.app.state, "history_supervisor", ...)
        if supervisor is None:
            raise HTTPException(503, "history service is unavailable")
        if supervisor is not ...:
            process = getattr(supervisor, "process", None)
            poll = getattr(process, "poll", None)
            if process is None or (callable(poll) and poll() is not None):
                raise HTTPException(503, "history service is unavailable")
        actor_id = str(get_current_user(request) or account_id)
        return HistoryClient(socket_path, actor_id=actor_id, account_id=account_id, timeout=4.0)

    def availability_http_error(exc: HistoryAvailabilityError) -> HTTPException:
        status_code = {
            "resource_not_found": 404,
            "version_not_found": 404,
            "version_expired": 410,
            "version_expiring": 409,
            "version_not_restorable": 409,
            "resource_unavailable": 409,
            "invalid_cursor": 422,
            "invalid_limit": 422,
            "invalid_offset": 416,
            "invalid_version_ref": 422,
            "ambiguous_version_ref": 409,
            "change_index_incomplete": 409,
            "stale_cursor": 409,
            "change_resource_ambiguous": 409,
        }.get(exc.code, 503)
        return HTTPException(
            status_code,
            detail={"code": exc.code, "message": str(exc)},
        )

    async def resolve_history_resource_ids(
        client: HistoryClient,
        resource_id: str,
        request: Request,
        owner: str,
        account_id: str,
        *,
        resolved_paths: dict[str, str] | None = None,
        policy_repository=None,
        allow_absent: bool = False,
    ) -> list[str]:
        """Resolve Files identity through current authorized paths and History registrations.

        Registry paths and private IDs stay inside this server process. Inactive
        registrations qualify only while the exact path remains the current,
        permitted Host resource for this authenticated account.
        """
        requested = str(resource_id or "").strip()
        private_requested = requested.startswith("file:")
        if private_requested and resolved_paths is None:
            response = await _bounded_history_io(lambda: client.resolve_resource(requested))
            handle = (response.get("Resource") or {}).get("handle") if isinstance(response, dict) else None
            if not isinstance(handle, dict) or str(handle.get("account_id") or "") != account_id:
                raise HistoryAvailabilityError("resource_not_found", "registered Files resource is unavailable")
            return [requested]
        if not private_requested and not re.fullmatch(r"resource-[0-9a-f]{64}", requested):
            raise HistoryAvailabilityError("resource_not_found", "registered Files resource is unavailable")

        from pathlib import Path

        from src.openclank.files_facade import FilesFacadeError, ProviderContext
        from src.openclank.files_host_provider import HostFilesProvider, host_origin_for_path
        from src.openclank.resource_refs import stable_resource_id

        files_user = str(get_current_user(request) or owner or "").strip().lower()
        if not files_user or account_id_for(request, files_user) != account_id:
            raise HistoryAvailabilityError("resource_not_found", "registered Files resource is unavailable")
        supplied_policy=policy_repository is not None
        if not supplied_policy:policy_repository=file_policy_module.FilePolicyRepository()
        files_context = ProviderContext(
            owner_subject_id=account_id,
            owner_username=files_user,
            policy_generation=policy_repository.generation(),
            is_admin=is_admin(request, files_user),
            workspace_id="default",
        )
        if supplied_policy:
            factory=getattr(request.app.state,"files_facade_factory",None)
            if not callable(factory):raise HistoryAvailabilityError("resource_unavailable","Files authority unavailable")
            owner_facade=factory(request,files_context)
            host_provider=owner_facade._providers.get("host")
            if host_provider is None:raise HistoryAvailabilityError("resource_unavailable","Files authority unavailable")
        else:
            host_provider=HostFilesProvider(registry=FilesystemRootRegistry())

        from src.openclank.history_change_qualifier import resolve_registered_history_ids
        return await resolve_registered_history_ids(client, requested, account=account_id, context=files_context,
            provider=host_provider, resolved_paths=resolved_paths if resolved_paths is not None else {}, allow_absent=allow_absent)

    async def run_history_candidate_calls(candidate_ids: list[str], call_factory, *, timeout: float = 1.5):
        """Run at most one worker-sized batch of trusted IPC calls at a time."""
        results = []
        for start in range(0, len(candidate_ids), _HISTORY_IO_WORKERS):
            batch = candidate_ids[start : start + _HISTORY_IO_WORKERS]
            results.extend(
                await asyncio.gather(
                    *(
                        _bounded_history_io(call_factory(private_id), timeout=timeout)
                        for private_id in batch
                    )
                )
            )
        return results

    def history_page_cursor(version: Dict[str, Any]) -> str:
        timestamp = version.get("timestamp_millis")
        action_id = version.get("action_id")
        version_id = version.get("version_id")
        source_provider = version.get("source_provider", "filesystem")
        if (
            isinstance(timestamp, bool)
            or not isinstance(timestamp, int)
            or timestamp < 0
            or not isinstance(action_id, str)
            or not action_id
            or not isinstance(version_id, str)
            or not version_id
            or not isinstance(source_provider, str)
            or not source_provider
            or len(source_provider) > 128
        ):
            raise HistoryAvailabilityError("resource_unavailable", "History returned an invalid version descriptor")
        payload = json.dumps(
            {
                "timestamp_millis": timestamp,
                "action_id": action_id,
                "version_id": version_id,
                "source_provider": source_provider,
            },
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
        return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")

    async def list_history_versions_for_candidates(
        client: HistoryClient,
        candidate_ids: list[str],
        *,
        cursor: str | None,
        limit: int,
    ) -> Dict[str, Any]:
        pages = await run_history_candidate_calls(
            candidate_ids,
            lambda private_id: lambda: client.list_resource_versions(
                private_id,
                cursor=cursor,
                limit=limit,
            ),
        )
        combined = []
        seen_version_refs: dict[str, str] = {}
        has_more = False
        for private_id, page in zip(candidate_ids, pages):
            has_more = has_more or page.get("next_cursor") is not None
            for item in page["items"]:
                version_ref = str(item.get("id") or "")
                if not version_ref:
                    raise HistoryAvailabilityError("resource_unavailable", "History returned a version without an identity")
                previous_owner = seen_version_refs.get(version_ref)
                if previous_owner is not None and previous_owner != private_id:
                    raise HistoryAvailabilityError(
                        "resource_unavailable",
                        "a History version matches multiple private registrations",
                    )
                seen_version_refs[version_ref] = private_id
                combined.append(item)
        combined.sort(
            key=lambda item: (
                int(item.get("timestamp_millis", -1)),
                str(item.get("action_id") or ""),
                str(item.get("version_id") or ""),
                str(item.get("source_provider") or "filesystem"),
            ),
            reverse=True,
        )
        has_more = has_more or len(combined) > limit
        selected = combined[:limit]
        next_cursor = history_page_cursor(selected[-1]) if has_more and selected else None
        return {"items": selected, "next_cursor": next_cursor}

    async def run_version_lookup(candidate_ids: list[str], call_factory):
        outcomes = []
        for start in range(0, len(candidate_ids), _HISTORY_IO_WORKERS):
            batch = candidate_ids[start : start + _HISTORY_IO_WORKERS]
            outcomes.extend(
                await asyncio.gather(
                    *(
                        _bounded_history_io(call_factory(private_id), timeout=1.5)
                        for private_id in batch
                    ),
                    return_exceptions=True,
                )
            )
        matches = []
        failures = []
        for private_id, outcome in zip(candidate_ids, outcomes):
            if isinstance(outcome, BaseException):
                if not isinstance(outcome, HistoryAvailabilityError) or outcome.code != "version_not_found":
                    failures.append(outcome)
            else:
                matches.append((private_id, outcome))
        if failures:
            failure = failures[0]
            if isinstance(failure, BaseException):
                raise failure
        if len(matches) > 1:
            raise HistoryAvailabilityError(
                "resource_unavailable",
                "selected History version matches multiple private registrations",
            )
        if not matches:
            raise HistoryAvailabilityError("version_not_found", "selected History version is unavailable")
        return matches[0]

    async def find_history_version_resource_id(
        client: HistoryClient,
        candidate_ids: list[str],
        version_ref: str,
    ) -> str:
        private_id, _selection = await run_version_lookup(
            candidate_ids,
            lambda private_id: lambda: client.resolve_resource_version(private_id, version_ref),
        )
        return private_id

    async def read_history_version_chunk_for_candidates(
        client: HistoryClient,
        candidate_ids: list[str],
        version_ref: str,
        *,
        offset: int,
        length: int,
    ) -> Dict[str, Any]:
        _private_id, chunk = await run_version_lookup(
            candidate_ids,
            lambda private_id: lambda: client.read_resource_version_chunk(
                private_id,
                version_ref,
                offset=offset,
                length=length,
            ),
        )
        return chunk

    def public_history_preview(preview: Dict[str, Any], resource_id: str) -> Dict[str, Any]:
        result = dict(preview)
        resource = result.get("resource")
        if isinstance(resource, dict):
            safe_resource = {"resource_id": resource_id}
            if isinstance(resource.get("generation"), int):
                safe_resource["generation"] = resource["generation"]
            result["resource"] = safe_resource
        return result

    def public_history_restore_result(response: Dict[str, Any], resource_id: str, request: Request) -> Dict[str, Any]:
        result = dict(response)
        receipt = result.get("Restore")
        if isinstance(receipt, dict):
            safe_receipt = dict(receipt)
            resources = receipt.get("resources")
            if isinstance(resources, list):
                safe_receipt["resources"] = [
                    {**dict(item), "resource_id": resource_id}
                    for item in resources
                    if isinstance(item, dict)
                ]
            result["Restore"] = safe_receipt
            proof = receipt.get("verification") or {}
            if receipt.get("outcome") == "Complete" and proof.get("status") == "Verified" and proof.get("content_hash") and proof.get("content_hash") == proof.get("restored_content_hash"):
                from src.openclank.achievement_producers import record_activity
                record_activity(request, "lore.restore.committed", str(proof.get("restore_id") or ""), {
                    "resourceId": resource_id, "versionId": proof.get("version_id"),
                    "contentHash": proof.get("content_hash"), "restoredContentHash": proof.get("restored_content_hash"),
                    "receiptKind": "VerifiedRestore", "digestOnly": False,
                })
            if isinstance(safe_receipt.get("verification"), dict):
                safe_receipt["verification"] = {**safe_receipt["verification"], "resource_id": resource_id}
        return result

    async def owned_host_restore(request, **kwargs):
        from src.openclank.files_facade import ProviderContext
        from src.openclank.history_restore_custody import owned_host_restore as owned
        repository = file_policy_module.FilePolicyRepository()
        generation = repository.generation()
        context = ProviderContext(owner_subject_id=kwargs["account_id"], owner_username=kwargs["owner"],
            policy_generation=generation, is_admin=is_admin(request, kwargs["owner"]), workspace_id=None)
        factory = getattr(request.app.state, "files_facade_factory", None)
        if not callable(factory):
            raise HTTPException(503, "Files authority unavailable")
        provider = factory(request, context)._providers.get("host")
        repository = getattr(provider, "operation_store", None)
        if repository is None or repository.generation() != generation:
            raise HTTPException(503, "Files authority changed")
        supervisor = getattr(request.app.state, "history_supervisor", ...)
        process = getattr(supervisor, "process", None) if supervisor is not ... else None
        def original_current():
            if supervisor is ...:
                history_live = getattr(request.app.state, "history_supervisor", ...) is ...
            else:
                history_live = (getattr(request.app.state, "history_supervisor", None) is supervisor
                    and process is not None and supervisor.process is process and process.poll() is None)
            return bool(history_live and getattr(request.app.state, "auth_manager", None) is kwargs["auth"]
                and getattr(request.app.state, "account_request_barrier", None) is kwargs["barrier"]
                and is_admin(request, kwargs["owner"]) == context.is_admin)
        async def revalidate_source():
            paths = {}
            candidates = await resolve_history_resource_ids(kwargs["client"], kwargs["requested_id"], request,
                kwargs["owner"], kwargs["account_id"], resolved_paths=paths, policy_repository=repository, allow_absent=True)
            return kwargs["private_id"] in candidates and paths.get(kwargs["private_id"]) == kwargs["path"]
        return await owned(provider=provider, repository=repository, context=context, generation=generation,
            revalidate_source=revalidate_source, original_current=original_current, **kwargs)

    def visible_snapshot(owner: str, admin: bool, request: Request) -> dict[str, Any]:
        account_id = account_id_for(request, owner)
        client = service_client(account_id, request)
        try:
            policy_response = client.get_policy()
            usage_response = client.get_usage()
            status_response = client.get_status()
        except (HistoryClientError, OSError, ValueError) as exc:
            raise HTTPException(503, f"history service unavailable: {exc}") from exc
        policy = dict(policy_response.get("Policy") or {})
        status = dict(status_response.get("Status") or {})
        status["available"] = True
        if not admin:
            inherited_global = dict(policy.get("global") or {})
            inherited_global["inherited"] = True
            policy["global"] = inherited_global
        roots = authorized_roots(owner, admin)
        visible_scopes = []
        for scope in policy.get("scopes") or []:
            if not isinstance(scope, dict):
                continue
            if not admin and scope.get("owner_account_id") != account_id:
                continue
            if scope.get("kind") == "Global":
                continue
            if scope.get("kind") in {"Workspace", "Directory"}:
                scope["kind"] = str(scope["kind"]).lower()
            if scope.get("kind") == "directory" and not is_authorized_directory(str(scope.get("root") or scope.get("value") or ""), roots):
                continue
            if scope.get("kind") == "directory":
                canonical = os.path.realpath(str(scope.get("root") or ""))
                match = next((root for root in roots if os.path.realpath(str(root.get("canonical_path"))) == canonical), None)
                if not match:
                    continue
                scope["root_id"] = match.get("id")
                scope["display_path"] = match.get("display_path")
            visible_scopes.append(scope)
        accounts = policy.pop("accounts", {})
        policy["capture_inherited"] = account_id not in accounts
        policy["capture"] = accounts.get(account_id, policy.get("capture_defaults"))
        policy["scopes"] = visible_scopes
        result = {
            "account_id": account_id,
            "policy": policy,
            "usage": usage_response.get("Usage") or {},
            "status": status,
            "directory_options": [
                {"id": root.get("id"), "label": root.get("display_path") or root.get("canonical_path")}
                for root in roots
            ],
        }
        return result

    def workspace_options(owner: str, request: Request, policy: Dict[str, Any], *, admin: bool = False) -> list[str]:
        account_id = account_id_for(request, owner)
        # Workspace choices come from the canonical Files policy repository,
        # so a newly-created workspace is available to History before a
        # history scope has been configured for it. Existing scope IDs remain
        # visible as edit targets during migration or after a workspace was
        # archived, but they never broaden account visibility.
        options = {
            str(scope.get("workspace_id"))
            for scope in (policy.get("scopes") or [])
            if isinstance(scope, dict)
            and (admin or scope.get("owner_account_id") == account_id)
            and str(scope.get("kind") or "").lower() == "workspace"
            and scope.get("workspace_id")
        }
        try:
            repository = file_policy_module.FilePolicyRepository()
            workspaces = repository.list_workspaces(
                owner_subject_id=None if admin else account_id,
                include_archived=False,
            )
            options.update(str(workspace.id) for workspace in workspaces if workspace.id)
        except (file_policy_module.FilePolicyError, OSError, ValueError) as exc:
            # History settings remain readable when the canonical policy store
            # is temporarily unavailable; configured scope IDs still support
            # safe editing and the next request can retry discovery.
            logger.warning("canonical workspace discovery unavailable: %s", exc)
        options.add("default")
        return sorted(item for item in options if item)

    @router.post("/restore")
    async def restore_history_resource(request: Request) -> Dict[str, Any]:
        """Forward an authenticated restore through the Rust provider boundary.

        ``destination_path`` is only an untrusted routing hint. The service
        resolves it against its private ResourceKey registry and rejects it
        unless it names the registered resource in the typed request.
        """
        user = require_user(request)
        owner = effective_user(request) or user
        account_id = account_id_for(request, owner)
        auth = getattr(request.app.state, "auth_manager", None)
        barrier = getattr(request.app.state, "account_request_barrier", None)
        if (owner != user or auth is None or barrier is None or not account_id or auth.account_id(owner) != account_id
                or auth.username_for_account_id(account_id) != owner):
            raise HTTPException(503, "History restore account unavailable")
        try:
            raw=bytearray()
            async for chunk in request.stream():
                if len(raw)+len(chunk)>1024*1024:raise HTTPException(413,'History restore request is too large')
                raw.extend(chunk)
            body=json.loads(raw)
            if isinstance(body,dict) and body.get('profile')=='regular_file_v1':
                def unique_pairs(pairs):
                    value={}
                    for key,item in pairs:
                        if key in value:raise ValueError('duplicate key')
                        value[key]=item
                    return value
                body=json.loads(raw,object_pairs_hook=unique_pairs)
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(400, "history restore must be a JSON object") from exc
        if not isinstance(body, dict):
            raise HTTPException(422, "history restore must be a JSON object")
        if body.get('profile') == 'regular_file_v1':
            from src.auth_helpers import is_internal_tool_identity
            from src.openclank.files_facade import ProviderContext
            from src.openclank import regular_file_restore as regular
            phase=body.get('phase')
            required={'profile','phase','resource_id','source'} | ({'request_id','expected_digest'} if phase in {'confirm','status'} else set())
            if (phase not in {'preview','confirm','status'} or set(body)!=required or is_internal_tool_identity(user)
                    or getattr(request.state,'api_token',False) or getattr(request.state,'internal_tool_authenticated',False)):
                raise HTTPException(422,'Regular-file restore request unavailable')
            supervisor=getattr(request.app.state,'history_supervisor',None);store=getattr(request.app.state,'files_policy_repository',None)
            process=getattr(supervisor,'process',None);factory=getattr(request.app.state,'files_facade_factory',None)
            if supervisor is None or store is None or process is None or (phase!='status' and not callable(factory)):raise HTTPException(503,'Regular-file restore unavailable')
            context=ProviderContext(owner_subject_id=account_id,owner_username=owner,policy_generation=store.generation(),is_admin=False,workspace_id=None)
            provider=factory(request,context)._providers.get('host') if phase!='status' else None
            if phase!='status' and (provider is None or getattr(provider,'operation_store',None) is not store):raise HTTPException(503,'Regular-file restore unavailable')
            def original_current():
                try:return (getattr(request.app.state,'auth_manager',None) is auth and getattr(request.app.state,'account_request_barrier',None) is barrier and getattr(request.app.state,'files_policy_repository',None) is store and getattr(request.app.state,'history_supervisor',None) is supervisor and supervisor.process is process and process.poll() is None and require_user(request)==user and (effective_user(request) or user)==user and auth.account_id(owner)==account_id and auth.username_for_account_id(account_id)==owner)
                except Exception:return False
            common=dict(resource_id=body['resource_id'],source=body['source'],owner=owner,account=account_id,auth=auth,barrier=barrier,supervisor=supervisor,store=store,original_current=original_current)
            try:
                if phase=='status':return await regular.status(request_id=body['request_id'],expected_digest=body['expected_digest'],**common)
                if phase=='preview':return await regular.preview(context=context,provider=provider,**common)
                return await regular.confirm(request_id=body['request_id'],expected_digest=body['expected_digest'],context=context,provider=provider,**common)
            except ValueError as exc:raise HTTPException(409,'Regular-file restore unavailable') from exc
            except HTTPException:raise
            except Exception as exc:raise HTTPException(503,'Regular-file restore unavailable') from exc
        if 'profile' in body:
            raise HTTPException(422,'Unknown History restore profile')
        restore_request = body.get("request")
        source = body.get("source")
        resource_id = body.get("resource_id")
        version_ref = body.get("version_ref")
        destination_path = body.get("destination_path")
        has_selection = isinstance(resource_id, str) and bool(resource_id.strip()) and isinstance(version_ref, str) and bool(version_ref.strip())
        selection_was_requested = resource_id is not None or version_ref is not None
        if selection_was_requested and not has_selection:
            raise HTTPException(422, "resource_id and version_ref must be provided together")
        if source is not None and has_selection:
            raise HTTPException(422, "provide either a version selection or a source receipt")
        has_destination_hint = isinstance(destination_path, str) and bool(destination_path.strip())
        pathless_selection = has_selection and source is None and not has_destination_hint
        if not pathless_selection and (
            not isinstance(restore_request, dict)
            or (not isinstance(source, dict) and not has_selection)
            or not has_destination_hint
        ):
            raise HTTPException(422, "restore requires a selected version or source receipt and a registered destination")
        if pathless_selection:
            nested_request = restore_request if isinstance(restore_request, dict) else {}
            restore_id = str(body.get("restore_id") or nested_request.get("restore_id") or "").strip()
            expected_fingerprint = body.get("expected_destination_fingerprint")
            if expected_fingerprint is None:
                expected_fingerprint = nested_request.get("expected_destination_fingerprint")
            if not restore_id or len(restore_id) > 256:
                raise HTTPException(422, "restore_id is required")
            if not isinstance(expected_fingerprint, str) or not expected_fingerprint or len(expected_fingerprint) > 256:
                raise HTTPException(422, "the reviewed destination fingerprint from restore preview is required")
        try:
            client = service_client(account_id, request)
            if pathless_selection:
                public_resource_id = resource_id
                resolved_paths: dict[str, str] = {}
                private_resource_ids = await resolve_history_resource_ids(
                    client, public_resource_id, request, owner, account_id,
                    resolved_paths=resolved_paths, allow_absent=True,
                )
                private_resource_id = await find_history_version_resource_id(
                    client, private_resource_ids, version_ref
                )
                selection = await _bounded_history_io(lambda: client.resolve_resource_version(private_resource_id, version_ref))
                selected_action = str(selection["version"].get("action_id") or "")
                selected_version = str(selection["version"].get("version_id") or "")
                path = resolved_paths.get(private_resource_id)
                if not path or not selected_action or not selected_version:
                    raise HTTPException(503, "History restore identity unavailable")
                response = await owned_host_restore(request, owner=owner, account_id=account_id, auth=auth, barrier=barrier, client=client,
                    requested_id=public_resource_id, private_id=private_resource_id, path=path, restore_id=restore_id,
                    action_id=selected_action, version_id=selected_version, version_ref=version_ref,
                    call=lambda: client.restore_resource_version(
                        restore_id,
                        private_resource_id,
                        version_ref,
                        expected_destination_fingerprint=expected_fingerprint,
                    ))
                result = public_history_restore_result(response, public_resource_id, request)
                receipt = result.get("Restore")
                if isinstance(receipt, dict) and receipt.get("outcome") == "Complete":
                    # The mutation has committed. Refresh failure must never turn
                    # it into a failed restore or encourage a mutation retry.
                    try:
                        from src.openclank.files_facade import FilesFacade, ProviderContext
                        from src.openclank.files_host_provider import HostFilesProvider
                        files_user = str(get_current_user(request) or owner or "").strip().lower()
                        if not files_user or account_id_for(request, files_user) != account_id:
                            raise ValueError("restore account changed")
                        path = resolved_paths.get(private_resource_id)
                        if not path:
                            raise ValueError("registered refresh locator unavailable")
                        context = ProviderContext(
                            owner_subject_id=account_id, owner_username=files_user,
                            policy_generation=file_policy_module.FilePolicyRepository().generation(),
                            is_admin=is_admin(request, files_user), workspace_id="default",
                        )
                        facade = FilesFacade([HostFilesProvider(registry=FilesystemRootRegistry())])
                        refreshed = await facade.host_resource_for_path(context, path=path)
                        if refreshed.get("id") != public_resource_id or refreshed.get("provider") != "host":
                            raise ValueError("restored resource identity changed")
                        receipt["refreshed_resource"] = refreshed
                    except Exception:
                        receipt["refresh_error"] = "The restored file could not be reauthorized for its open view."
                return result

            if str(restore_request.get("account_id") or "") != account_id:
                raise HTTPException(403, "restore request belongs to another account")
            destination = restore_request.get("destination")
            if not isinstance(destination, dict) or str(destination.get("account_id") or "") != account_id:
                raise HTTPException(403, "restore destination belongs to another account")
            if not str(restore_request.get("restore_id") or "").strip():
                raise HTTPException(422, "restore_id is required")
            if not has_selection and not all(str(restore_request.get(field) or "").strip() for field in ("source_action_id", "source_version_id")):
                raise HTTPException(422, "restore source identifiers are required")
            restore_id = str(restore_request.get("restore_id") or "").strip()
            requested_id = resource_id if has_selection else str(destination.get("resource_id") or "")
            resolved_paths = {}
            selected_resource_ids = await resolve_history_resource_ids(client, requested_id, request, owner, account_id,
                resolved_paths=resolved_paths, allow_absent=True)
            selected_resource_id = str(destination.get("resource_id") or "")
            if has_selection:
                selected_resource_id = await find_history_version_resource_id(
                    client, selected_resource_ids, version_ref
                )
            if selected_resource_id not in selected_resource_ids or not resolved_paths.get(selected_resource_id):
                raise HTTPException(403, "History restore destination is unavailable")
            path = resolved_paths[selected_resource_id]
            if os.path.realpath(destination_path) != path:
                raise HTTPException(403, "History restore destination does not match the registered resource")

            effective_request = dict(restore_request)
            selected_source = source
            if has_selection:
                selection = await _bounded_history_io(lambda: client.resolve_resource_version(selected_resource_id, version_ref))
                resource = selection["resource"]
                version = selection["version"]
                if (str(destination.get("resource_id") or "") not in {str(resource.get("resource_id") or ""), str(resource_id)}
                        or str(destination.get("workspace_id") or "") != str(resource.get("workspace_id") or "")
                        or str(destination.get("provider") or "") != "filesystem"
                        or str(destination.get("account_id") or "") != str(resource.get("account_id") or "")):
                    raise HTTPException(403, "restore destination must be the selected Files resource")
                selected_action = str(version.get("action_id") or "")
                selected_version = str(version.get("version_id") or "")
                if restore_request.get("source_action_id") not in (None, selected_action) or restore_request.get("source_version_id") not in (None, selected_version):
                    raise HTTPException(409, "restore source changed from the selected History version")
                effective_request["destination"] = {**dict(destination), "resource_id": str(resource.get("resource_id") or "")}
                effective_request["source_action_id"] = selected_action
                effective_request["source_version_id"] = selected_version
                selected_source = selection["receipt"]
            selected_action = str(effective_request.get("source_action_id") or "")
            selected_version = str(effective_request.get("source_version_id") or "")
            if not selected_action or not selected_version:
                raise HTTPException(422, "restore source identifiers are required")

            def apply_selected_restore():
                response = client.restore_host(
                    effective_request,
                    selected_source,
                    destination_path=path,
                    source_host_metadata=body.get("source_host_metadata") if isinstance(body.get("source_host_metadata"), dict) else None,
                )
                return response

            response = await owned_host_restore(request, owner=owner, account_id=account_id, auth=auth, barrier=barrier, client=client,
                requested_id=requested_id, private_id=selected_resource_id, path=path, restore_id=restore_id,
                action_id=selected_action, version_id=selected_version, version_ref=version_ref if has_selection else None, call=apply_selected_restore)
            return public_history_restore_result(response, str(resource_id or ""), request)
        except HTTPException:
            raise
        except HistoryAvailabilityError as exc:
            raise availability_http_error(exc) from exc
        except HistoryClientError as exc:
            message = str(exc)
            status = 409 if "conflict" in message.lower() else 422
            raise HTTPException(status, message) from exc
        except (OSError, ValueError) as exc:
            raise HTTPException(503, f"history service unavailable: {exc}") from exc

    @router.post("/capture-repair/{action_id}")
    async def repair_history_capture(action_id: str, request: Request) -> Dict[str, Any]:
        """Repair a selected committed recovery gap under the original actor."""
        from src.auth_helpers import copal_owner_for_user
        from src.openclank.copal_errors import CopalBridgeError
        from src.openclank.history_repair import CaptureRepairUnavailable, repair_capture, snapshot_native_resource
        from src.openclank.files_facade import FilesFacadeError, ProviderContext
        from src.openclank.files_host_provider import HostFilesProvider, host_origin_for_path
        user = require_user(request)
        account_id = account_id_for(request, user)
        if not action_id.strip() or len(action_id) > 256:
            raise HTTPException(422, "Recovery action identity is invalid")
        client = service_client(account_id, request)

        async def copal_snapshot(key):
            bridge = getattr(request.app.state, "copal_bridge", None)
            if bridge is None:
                raise CaptureRepairUnavailable("Copal storage is unavailable; recovery gap remains.")
            return await bridge.call("get", {"owner": copal_owner_for_user(user), "workspace_id": key["workspace_id"], "id": key["resource_id"]}, timeout=4)

        async def file_snapshot(path):
            if not os.path.isabs(path):
                raise CaptureRepairUnavailable("The recovery locator is invalid.")
            context = ProviderContext(owner_subject_id=account_id, owner_username=user,
                policy_generation=file_policy_module.FilePolicyRepository().generation(), is_admin=is_admin(request, user), workspace_id="default")
            provider = HostFilesProvider(registry=FilesystemRootRegistry())
            content = await provider.content(context, origin_id=host_origin_for_path(path))
            if content.size is None or content.size > 10 * 1024 * 1024 or content.stream is None:
                raise CaptureRepairUnavailable("This recovery copy requires a bounded provider reconciliation.")
            captured = bytearray()
            async for chunk in content.stream(0, content.size):
                captured.extend(chunk)
                if len(captured) > 10 * 1024 * 1024:
                    raise CaptureRepairUnavailable("Recovery copy exceeds the repair bound.")
            if len(captured) != content.size:
                raise CaptureRepairUnavailable("The original file snapshot is unavailable.")
            return bytes(captured)

        async def resource_snapshot(proof):
            context = ProviderContext(owner_subject_id=account_id, owner_username=user,
                policy_generation=file_policy_module.FilePolicyRepository().generation(), is_admin=is_admin(request, user), workspace_id=proof['resource_key']['workspace_id'])
            provider = HostFilesProvider(registry=FilesystemRootRegistry())
            async def authorize(path):
                await provider._authorized_path(context, host_origin_for_path(path))
            return await snapshot_native_resource(proof, authorize=authorize, read_file=file_snapshot)

        try:
            return await repair_capture(client, action_id, account_id=account_id, copal_snapshot=copal_snapshot, file_snapshot=file_snapshot, resource_snapshot=resource_snapshot)
        except CaptureRepairUnavailable as exc:
            raise HTTPException(409, {"code": "capture_repair_unavailable", "message": str(exc), "action_id": action_id}) from exc
        except HistoryClientError as exc:
            # Exact actor/account failures remain unavailable; a human cannot
            # choose an agent actor identity to acquire its completion rights.
            raise HTTPException(409, {"code": "capture_repair_unavailable", "message": "Recovery action is unavailable or requires its original actor.", "action_id": action_id}) from exc
        except (FilesFacadeError, CopalBridgeError, OSError, asyncio.TimeoutError) as exc:
            raise HTTPException(503, {"code": "capture_repair_unavailable", "message": "Recovery snapshot is unavailable; the gap remains.", "action_id": action_id}) from exc

    async def _captured_change_value(request: Request, locator: str, *, metadata: bool = False, html_chat: str | None = None, result_chat: str | None = None, result_member: str | None = None) -> Dict[str, Any]:
        """Resolve an exact action under fresh named-human Files authority."""
        from src.auth_helpers import is_internal_tool_identity
        from src.openclank.files_facade import ProviderContext
        from src.openclank.history_change_qualifier import qualify_captured_change
        from src.openclank.history_change_sets import parse_locator
        user = require_user(request); state = request.app.state
        manager = getattr(state, 'auth_manager', None)
        if is_internal_tool_identity(user) or getattr(request.state, 'api_token', False) or getattr(request.state, 'internal_tool_authenticated', False):
            raise HTTPException(403, 'A named user session is required')
        try:
            account = manager.account_id(user)
            if not account or manager.username_for_account_id(account) != user or (effective_user(request) or user) != user:
                raise ValueError()
            parse_locator(locator)
        except (AttributeError, TypeError, ValueError, UnicodeError):
            raise HTTPException(400, 'Captured change is unavailable')
        try:
            policy = getattr(state, 'files_policy_repository', None)
            factory = getattr(state, 'files_facade_factory', None)
            supervisor = getattr(state, 'history_supervisor', ...)
            process = getattr(supervisor, 'process', None) if supervisor is not ... else None
            if policy is None or not callable(factory): raise ValueError()
            generation = policy.generation(); admin = is_admin(request, user)
            context = ProviderContext(owner_subject_id=account, owner_username=user, policy_generation=generation,
                                      is_admin=admin, workspace_id='default')
            provider = factory(request, context)._providers.get('host')
            if provider is None or getattr(provider, 'operation_store', None) is not policy: raise ValueError()
            client = service_client(account, request)
            def current():
                try:
                    poll = getattr(process, 'poll', None)
                    return (require_user(request) == user and not is_internal_tool_identity(user)
                        and not getattr(request.state, 'api_token', False) and not getattr(request.state, 'internal_tool_authenticated', False)
                        and (effective_user(request) or user) == user and getattr(state, 'auth_manager', None) is manager
                        and manager.account_id(user) == account and manager.username_for_account_id(account) == user
                        and getattr(state, 'files_policy_repository', None) is policy and policy.generation() == generation
                        and getattr(state, 'files_facade_factory', None) is factory and is_admin(request, user) == admin
                        and getattr(state, 'history_supervisor', ...) is supervisor
                        and (supervisor is ... or (getattr(supervisor, 'process', None) is process
                            and process is not None and (not callable(poll) or poll() is None))))
                except Exception: return False
            identity=parse_locator(locator)
            private_member = result_chat is not None and html_chat is None and identity.get('version') == 2 and isinstance(result_member,str)
            qualified = None if private_member else await qualify_captured_change(locator, client=client, account=account, context=context,
                provider=provider, current=current, metadata=metadata or html_chat is not None or result_chat is not None)
            if html_chat is None and result_chat is None: return qualified
            from src.openclank.history_after_source import capture_recorded_after, digest_recorded_after, verify_current_text
            if identity.get('origin',{}).get('host_chat_id') != (html_chat if html_chat is not None else result_chat):
                raise ValueError('Recorded source unavailable')
            if identity.get('version') == 1 and qualified.get('version') == 1 and result_member is None:
                selected=qualified.get('current_resource_id')
            elif private_member:
                selected=result_member
            else: raise ValueError('Recorded source unavailable')
            if not isinstance(selected,str): raise ValueError('Recorded source unavailable')
            reader=capture_recorded_after if html_chat is not None else digest_recorded_after
            source=await reader(locator,selected,client=client,account=account,
                context=context,provider=provider,current=current)
            path=source['qualified']['path']; entry=await provider.resource_for_path(context,path=path)
            expected={'kind':'hostFingerprint','value':f"sha256:{source['sha256']}:{source['size']}"}
            if entry.kind != 'file' or entry.revision != expected or not current(): raise ValueError('HTML source changed')
            facade=factory(request,context); resource=facade._resource(context,'host',entry,parent_stable_id=None)
            format=resource.get('format') or {}; operation=(format.get('operations') or {}).get('browserPreview') or {}
            if identity.get('version') == 2:
                if (format.get('operations') or {}).get('source',{}).get('available') is not True: raise ValueError('Recorded text source unavailable')
                await verify_current_text(path,expected,provider=provider,context=context,current=current)
            if (resource.get('id') != selected or resource.get('revision') != expected
                or resource.get('ref') != format.get('resourceRef') or format.get('providerRevision') != expected
                or format.get('policyGeneration') != generation or (html_chat is not None and (operation.get('available') is not True or resource.get('mime_type') != 'text/html')) or not current()): raise ValueError('HTML preview unavailable')
            after=await provider.resource_for_path(context,path=path)
            if after.origin_id != entry.origin_id or after.revision != expected or not current(): raise ValueError('HTML source changed')
            return {'version':1,'resourceRef':resource['ref'],'resourceID':resource['id'],'revision':expected,
                    'workspaceScope':context.workspace_id,'title':resource.get('name') or 'Saved file',
                    **({'formatID':str(format.get('formatId') or 'unknown')[:128]} if result_chat is not None else {})}
        except Exception:
            raise HTTPException(503, 'Captured change is unavailable')

    @router.post('/observed-step')
    async def observed_step(request: Request):
        """Read a bounded, unverified observation under current owner custody."""
        import time
        from contextlib import AsyncExitStack
        from fastapi.responses import JSONResponse
        from src.auth_helpers import is_internal_tool_identity
        from src.openclank.history_change_sets import strict_json
        from src.openclank.transcript_projection import get_managed_binding
        from src.secret_storage import encrypt, decrypt
        from routes.orchestration_routes import _get_observation_binding, _ensure_observation_binding_current

        headers = {'Cache-Control': 'private, no-store'}
        try:
            raw = bytearray()
            async for chunk in request.stream():
                if len(raw) + len(chunk) > 8192: raise ValueError('request too large')
                raw.extend(chunk)
            body = strict_json(raw.decode('utf-8'))
            if type(body) is not dict or set(body) not in ({'version','chatID','selector'}, {'version','chatID','selector','cursor'}, {'version','locator'}, {'version','locator','turn'}): raise ValueError()
            if type(body['version']) is not int or body['version'] != 1: raise ValueError()
            turn = body.get('turn') is True
            if 'turn' in body and not turn: raise ValueError()
            catalog = 'locator' in body
            if catalog:
                from src.openclank.history_change_sets import parse_locator
                origin = parse_locator(body['locator'])['origin']
                chat, selector = origin['host_chat_id'], None
                selected = [origin['engine_session_id'],origin['assistant_message_id'],None,None]
            else:
                chat, selector = body['chatID'], body['selector']
            if not isinstance(chat, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,256}', chat): raise ValueError()
            prefix = 'oc-snapshot:step:v1:'
            if not catalog:
                if not isinstance(selector, str) or not selector.startswith(prefix): raise ValueError()
                encoded = selector[len(prefix):]
                if not re.fullmatch(r'[A-Za-z0-9_-]{1,2048}', encoded): raise ValueError()
                data = base64.urlsafe_b64decode(encoded + '=' * (-len(encoded) % 4))
                selected = strict_json(data.decode('utf-8'))
                if (type(selected) is not list or len(selected) != 4
                    or any(not isinstance(x, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,256}', x) for x in selected[:3])
                    or type(selected[3]) is not int or not 0 <= selected[3] <= 9007199254740991
                    or base64.urlsafe_b64encode(json.dumps(selected,ensure_ascii=False,separators=(',',':')).encode()).decode().rstrip('=') != encoded): raise ValueError()
            user = require_user(request)
            auth = request.app.state.auth_manager; barrier = request.app.state.account_request_barrier
            policy = request.app.state.files_policy_repository
            account = auth.account_id(user)
            if (not account or auth.username_for_account_id(account) != user or (effective_user(request) or user) != user
                or is_internal_tool_identity(user) or getattr(request.state,'api_token',False)
                or getattr(request.state,'internal_tool_authenticated',False)): raise ValueError()
            async with barrier.admitted(user):
                qualified = await _captured_change_value(request,body['locator'],metadata=True) if catalog else None
                owner, supervisor, binding, mapping = await asyncio.to_thread(_get_observation_binding,request,chat,None,session_manager)
                if owner != user or binding.engine_session_id != selected[0]: raise ValueError()
                async with mapping.async_chat_admission_lock(chat,blocking=False,existing_only=True) as guard:
                    async with AsyncExitStack() as stack:
                        authority = await asyncio.to_thread(get_managed_binding,chat,owner=owner)
                        pool = supervisor.owner_lifecycle_pool(); key = pool._key(owner)
                        lease,state,bridge,holder,generation = await pool.acquire_rating_operation(owner=owner)
                        async def release():
                            task = asyncio.create_task(lease.release()); cancelled = False
                            while not task.done():
                                try: await asyncio.shield(task)
                                except asyncio.CancelledError: cancelled = True
                            task.result()
                            if cancelled: raise asyncio.CancelledError
                        stack.push_async_callback(release)
                        worker = lease.worker; process = getattr(worker,'_proc',None)
                        context = (getattr(bridge,'_session_context',{}) or {}).get(binding.engine_session_id)
                        cwd = authority.get('physicalCwd'); policy_generation = policy.generation()
                        if (not state or state.status != 'ready' or not worker or not worker.is_alive() or not holder or process is None
                            or not isinstance(context,dict) or context.get('incognito') or context.get('auxiliary')
                            or authority.get('engineSessionID') != binding.engine_session_id or authority.get('mappingRevision') != binding.binding_revision
                            or authority.get('transition') is not None or not isinstance(cwd,str)): raise ValueError()
                        def current():
                            try:
                                mapping.validate_chat_guard(chat,guard); _ensure_observation_binding_current(mapping,owner,chat,binding)
                                pool._assert_owner_lifecycle(key,lease._owner_epoch)
                                return (not lease._released and request.app.state.auth_manager is auth and request.app.state.account_request_barrier is barrier
                                    and request.app.state.files_policy_repository is policy and policy.generation() == policy_generation
                                    and require_user(request) == owner and (effective_user(request) or owner) == owner
                                    and not is_internal_tool_identity(owner) and not getattr(request.state,'api_token',False)
                                    and not getattr(request.state,'internal_tool_authenticated',False)
                                    and auth.account_id(owner) == account and auth.username_for_account_id(account) == owner
                                    and _verify_session_owner(request,chat,session_manager) == owner and get_managed_binding(chat,owner=owner) == authority
                                    and pool._states.get(key) is state and state.active is worker and state.status == 'ready' and state.generation == generation
                                    and worker.is_alive() and worker._bridge is bridge and getattr(worker,'_proc',None) is process
                                    and (getattr(bridge,'_session_context',{}) or {}).get(binding.engine_session_id) is context
                                    and not context.get('incognito') and not context.get('auxiliary')
                                    and getattr(getattr(worker,'_managed_callbacks',None),'holder_id',None) == holder
                                    and not getattr(session_manager.sessions.get(chat),'incognito',False))
                            except Exception: return False
                        if turn:
                            def root_current():
                                with SessionLocal() as db:
                                    row=(db.query(DbChatMessage.id).join(DbSession,DbSession.id==DbChatMessage.session_id)
                                        .filter(DbChatMessage.id==origin['root_operation_id'],DbChatMessage.session_id==chat,
                                            DbChatMessage.role=='user',DbSession.owner==owner).one_or_none())
                                    return row is not None and current()
                            async def read_turn(payload):
                                if not await asyncio.to_thread(root_current): raise ValueError()
                                response=await worker.session_observed_step_request(chat,binding.engine_session_id,cwd,payload,owner=owner)
                                if response.status_code!=200 or len(response.content)>1048576 or not await asyncio.to_thread(root_current): raise ValueError()
                                return strict_json(response.content.decode('utf-8','strict'))
                            head=await read_turn({'version':1,'stableChatID':chat,'head':True})
                            if (type(head) is not dict or set(head)!={'version','sessionID','hostChatID','head'} or head['sessionID']!=selected[0] or head['hostChatID']!=chat or type(head['head']) is not dict or set(head['head'])!={'messageID','timeCreated'}): raise ValueError()
                            scan_payload={'version':1,'stableChatID':chat,'rootOperationID':origin['root_operation_id'],'headMessageID':head['head']['messageID'],'anchorMessageID':selected[1]}
                            scan=await read_turn(scan_payload)
                            if (type(scan) is not dict or set(scan)!={'version','assurance','coverage','sessionID','hostChatID','rootOperationID','head','unknown','steps'} or scan['version']!=1 or scan['assurance']!='unverified_local_observation' or scan['coverage']!='partial' or scan['sessionID']!=selected[0] or scan['hostChatID']!=chat or scan['rootOperationID']!=origin['root_operation_id'] or scan['head']!=head['head'] or type(scan['unknown']) is not bool or type(scan['steps']) is not list or len(scan['steps'])>64): raise ValueError()
                            actions=[];seen=set();unknown=scan['unknown']; anchor_found=False; qualified_members=0
                            for step in scan['steps']:
                                if (type(step) is not dict or step.get('accountID')!=account or step.get('workspaceID')!=authority.get('authorityWorkspaceID') or step.get('sessionID')!=selected[0] or step.get('hostChatID')!=chat or step.get('rootOperationID')!=origin['root_operation_id'] or type(step.get('members')) is not list or len(step['members'])>64): raise ValueError()
                                for index,member in enumerate(step['members']):
                                    if type(member) is not dict or member.get('sequence')!=index: raise ValueError()
                                    if member.get('state')=='pending': unknown=True; continue
                                    locator=member.get('actionRef')
                                    if member.get('state')!='complete' or not isinstance(locator,str) or locator in seen or len(seen)>=64: raise ValueError()
                                    seen.add(locator)
                                    if locator==body['locator']: anchor_found=True
                                    try:
                                        comparison=await _captured_change_value(request,locator,metadata=True)
                                        qualified_members+=len(comparison['members']) if comparison.get('version')==2 else 1
                                        if qualified_members>64: raise ValueError('comparison limit exceeded')
                                        actions.append({'state':'available','locator':locator,'comparison':comparison})
                                    except HTTPException: unknown=True;actions.append({'state':'unavailable'})
                            if not anchor_found or not actions: raise ValueError()
                            if await read_turn(scan_payload)!=scan or await read_turn({'version':1,'stableChatID':chat,'head':True})!=head or await _captured_change_value(request,body['locator'],metadata=True)!=qualified: raise ValueError()
                            result={'version':1,'coverage':'partial_recorded_actions','unknown':unknown,'actions':actions}
                            if len(json.dumps(result,ensure_ascii=False,separators=(',',':')).encode('utf-8'))>1048576: raise ValueError('comparison limit exceeded')
                            return JSONResponse(result,headers=headers)
                        payload = ({'version':1,'stableChatID':chat,'messageID':selected[1],'actionRef':body['locator']} if catalog else
                            {'version':1,'stableChatID':chat,'messageID':selected[1],'stepID':selected[2],'membershipRevision':selected[3]})
                        async def native():
                            if not await asyncio.to_thread(current): raise ValueError()
                            response = await worker.session_observed_step_request(chat,binding.engine_session_id,cwd,payload,owner=owner)
                            if response.status_code != 200 or len(response.content) > 65536 or not await asyncio.to_thread(current): raise ValueError()
                            value = strict_json(response.content.decode('utf-8'))
                            keys = {'version','assurance','sessionID','messageID','stepID','membershipRevision','accountID','hostChatID','workspaceID','actorID','executionRevision','rootOperationID','innerTurnID','coverage','members'}
                            if (type(value) is not dict or set(value) != keys or value['version'] != 1
                                or value['assurance'] != 'unverified_local_observation' or value['sessionID'] != selected[0]
                                or value['messageID'] != selected[1] or value['stepID'] != selected[2]
                                or value['membershipRevision'] != selected[3] or value['accountID'] != account or value['hostChatID'] != chat
                                or type(value['members']) is not list or len(value['members']) > 64): raise ValueError()
                            for i,member in enumerate(value['members']):
                                if type(member) is not dict or member.get('sequence') != i or type(member.get('sequence')) is not int: raise ValueError()
                                if member.get('state') == 'pending':
                                    if set(member) != {'sequence','state'}: raise ValueError()
                                elif member.get('state') == 'complete':
                                    if set(member) != {'sequence','state','actionRef'} or not isinstance(member['actionRef'],str) or len(member['actionRef'].encode()) > 4096: raise ValueError()
                                else: raise ValueError()
                            return value
                        if catalog:
                            async def catalog_read():
                                if not await asyncio.to_thread(current): raise ValueError()
                                response = await worker.session_observed_step_request(chat,binding.engine_session_id,cwd,payload,owner=owner)
                                if response.status_code != 200 or len(response.content) > 32768 or not await asyncio.to_thread(current): raise ValueError()
                                value = strict_json(response.content.decode('utf-8'))
                                if (type(value) is not dict or set(value) != {'version','assurance','sessionID','messageID','hostChatID','candidates'}
                                    or value['version'] != 1 or value['assurance'] != 'unverified_local_observation'
                                    or value['sessionID'] != selected[0] or value['messageID'] != selected[1] or value['hostChatID'] != chat
                                    or type(value['candidates']) is not list or len(value['candidates']) > 20): raise ValueError()
                                result = []
                                for candidate in value['candidates']:
                                    if (type(candidate) is not dict or set(candidate) != {'stepID','membershipRevision','coverage','workspaceID','accountID'}
                                        or candidate['accountID'] != account or candidate['workspaceID'] != authority.get('authorityWorkspaceID')
                                        or not isinstance(candidate['stepID'],str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,256}',candidate['stepID'])
                                        or type(candidate['membershipRevision']) is not int or not 0 <= candidate['membershipRevision'] <= 9007199254740991
                                        or candidate['coverage'] not in ('open','partial','interrupted')): raise ValueError()
                                    data = [selected[0],selected[1],candidate['stepID'],candidate['membershipRevision']]
                                    token = prefix + base64.urlsafe_b64encode(json.dumps(data,ensure_ascii=False,separators=(',',':')).encode()).decode().rstrip('=')
                                    result.append({'selector':token,'coverage':candidate['coverage']})
                                return value,result
                            first_catalog,candidates = await catalog_read()
                            if await _captured_change_value(request,body['locator'],metadata=True) != qualified: raise ValueError()
                            second_catalog,_ = await catalog_read()
                            if second_catalog != first_catalog or not await asyncio.to_thread(current): raise ValueError()
                            return JSONResponse({'version':1,'kind':'step_candidates','chatID':chat,'coverage':'partial',
                                'assurance':'unverified_local_observation','candidates':candidates},headers=headers)
                        first = await native()
                        digest = hashlib.sha256(json.dumps(first,sort_keys=True,ensure_ascii=False,separators=(',',':')).encode()).hexdigest()
                        scope = {'version':1,'account':account,'chat':chat,'selector':selector,'policy':policy_generation,
                            'holder':holder,'generation':generation,'epoch':lease._owner_epoch,'process':id(process),
                            'mapRevision':binding.map_revision,'bindingRevision':binding.binding_revision,'partHash':digest}
                        offset = 0
                        if 'cursor' in body:
                            cursor = body['cursor']
                            if not isinstance(cursor,str) or not cursor.startswith('os1.') or len(cursor)>8192: raise ValueError()
                            saved = strict_json(decrypt('enc:'+cursor[4:]))
                            if type(saved) is not dict or set(saved) != set(scope)|{'offset','expires'}: raise ValueError()
                            if any(saved.get(k) != v for k,v in scope.items()) or type(saved['offset']) is not int or not 0 < saved['offset'] < len(first['members']) or type(saved['expires']) is not int or saved['expires'] < int(time.time()): raise ValueError()
                            offset = saved['offset']
                        output = []
                        for member in first['members'][offset:offset+20]:
                            item = {'sequence':member['sequence'],'state':'pending' if member['state']=='pending' else 'unavailable'}
                            if member['state'] == 'complete':
                                try:
                                    resolved = await _captured_change_value(request,member['actionRef'],metadata=True)
                                    if resolved['version'] == 2:
                                        item.update(state='qualified', locator=member['actionRef'], members=[
                                            {k:part[k] for k in ('state','current_resource_id','current_name') if k in part}
                                            for part in resolved['members']])
                                    else:
                                        item.update(state='qualified',current_resource_id=resolved['current_resource_id'],current_name=resolved['current_name'],locator=member['actionRef'])
                                except HTTPException: pass
                            output.append(item)
                        if await native() != first or not await asyncio.to_thread(current): raise ValueError()
                        end = offset + len(output); next_cursor = None
                        if end < len(first['members']):
                            token = encrypt(json.dumps({**scope,'offset':end,'expires':int(time.time())+900},sort_keys=True,separators=(',',':')))
                            if not token.startswith('enc:'): raise ValueError()
                            next_cursor = 'os1.' + token[4:]
                        return JSONResponse({'version':1,'selector':selector,'coverage':'partial','assurance':'unverified_local_observation','members':output,'next_cursor':next_cursor},headers=headers)
        except Exception:
            return JSONResponse({'version':1,'state':'unavailable'},status_code=409,headers=headers)

    async def _captured_change(request: Request) -> Dict[str, Any]:
        body = await request.json()
        if type(body) is not dict or set(body) != {'locator'}: raise HTTPException(400, 'Captured change is unavailable')
        return await _captured_change_value(request, body['locator'])

    @router.post('/captured-html-resource')
    @router.post('/captured-resource')
    async def captured_html_resource(request: Request):
        """Resolve one recorded after-version under current Human Files."""
        from fastapi.responses import JSONResponse
        from src.openclank.history_change_sets import strict_json
        from src.openclank.conversation_archive import get_conversation_archive
        headers={'Cache-Control':'private, no-store'}
        try:
            chunks=[]; size=0
            async for chunk in request.stream():
                size += len(chunk)
                if size > 8192: raise ValueError('unavailable')
                chunks.append(chunk)
            body=strict_json(b''.join(chunks).decode('utf-8','strict'))
            if type(body) is not dict or set(body) not in ({'locator','chat'},{'locator','chat','resourceID'}) or not isinstance(body['chat'],str) or not body['chat'] or len(body['chat'])>256: raise ValueError('unavailable')
            if 'resourceID' in body and (request.url.path.endswith('/captured-html-resource') or not isinstance(body['resourceID'],str) or not re.fullmatch(r'resource-[0-9a-f]{64}',body['resourceID'])): raise ValueError('unavailable')
            user=require_user(request);state=request.app.state;auth=getattr(state,'auth_manager',None);barrier=getattr(state,'account_request_barrier',None)
            manager=getattr(state,'session_manager',None);token=request.cookies.get('open_clank_session');account=auth.account_id(user) if auth else None
            if not account or not barrier or not manager or not token or auth.get_username_for_token(token)!=user or auth.username_for_account_id(account)!=user: raise ValueError('unavailable')
            chat=body['chat'];session=await asyncio.to_thread(manager.get_session,chat)
            def current():
                try:
                    key=barrier._owner(user)
                    return bool(require_user(request)==user and state.auth_manager is auth and state.account_request_barrier is barrier
                        and request.cookies.get('open_clank_session')==token and auth.get_username_for_token(token)==user
                        and auth.account_id(user)==account and auth.username_for_account_id(account)==user
                        and manager.sessions.get(chat) is session and session.owner==user and not getattr(session,'incognito',False)
                        and not getattr(session,'archived',False) and not barrier._fenced(key)
                        and not (barrier._absolute_fence and barrier._absolute_fence(key)))
                except Exception:return False
            def durable():
                db=SessionLocal()
                try:
                    row=db.query(DbSession.owner,DbSession.archived).filter(DbSession.id==chat).first()
                    return bool(row and row.owner==user and not row.archived and not get_conversation_archive().chat_erasure_started(owner=user,chat_id=chat))
                finally: db.close()
            async with barrier.admitted(user):
                if not current(): raise ValueError('unavailable')
                is_html=request.url.path.endswith('/captured-html-resource')
                result=await _captured_change_value(request,body['locator'],**({'html_chat':chat} if is_html else {'result_chat':chat,'result_member':body.get('resourceID')}))
                if not current() or not await asyncio.to_thread(durable) or not current(): raise ValueError('unavailable')
                return JSONResponse(result,headers=headers)
        except Exception:
            return JSONResponse({'detail':'Saved HTML is unavailable' if request.url.path.endswith('/captured-html-resource') else 'Saved file is unavailable'},status_code=409,headers=headers)

    @router.post('/captured-change-metadata')
    async def captured_change_metadata(request: Request):
        from fastapi.responses import JSONResponse
        from src.openclank.history_change_sets import strict_json
        from src.auth_helpers import is_internal_tool_identity
        headers = {'Cache-Control':'private, no-store'}
        try:
            user = require_user(request); manager = getattr(request.app.state, 'auth_manager', None)
            account = manager.account_id(user)
            if not account or manager.username_for_account_id(account) != user or (effective_user(request) or user) != user or is_internal_tool_identity(user) or getattr(request.state, 'api_token', False) or getattr(request.state, 'internal_tool_authenticated', False): raise ValueError()
            raw = bytearray()
            async for chunk in request.stream():
                raw.extend(chunk)
                if len(raw) > 96 * 1024: raise ValueError()
            body = strict_json(raw.decode('utf-8', 'strict'))
            if type(body) is not dict or set(body) != {'version','locators'} or type(body['version']) is not int or body['version'] != 1 or type(body['locators']) is not list or not 1 <= len(body['locators']) <= 20 or len(set(body['locators'])) != len(body['locators']): raise ValueError()
            items = []
            for locator in body['locators']:
                try:
                    result = await _captured_change_value(request, locator, metadata=True)
                    if result['version'] == 2:
                        items.append({'state':'available','version':2,'coverage':result['coverage'],'members':[
                            {k:part[k] for k in ('state','current_resource_id','current_name') if k in part}
                            for part in result['members']]})
                        if manager.account_id(user) != account or manager.username_for_account_id(account) != user or require_user(request) != user: raise ValueError()
                        continue
                    change = result['item']
                    allowed = {k:change.get(k) for k in ('operation','state','timestamp_millis','coverage','rename','binary')}
                    if any(v is not None and (type(v) not in (str,int) or isinstance(v, str) and len(v.encode('utf-8','strict')) > 256) for v in allowed.values()): raise ValueError()
                    if allowed['timestamp_millis'] is not None:
                        millis = allowed['timestamp_millis']
                        if type(millis) is not int or millis < 0 or millis > 18446744073709551615: raise ValueError()
                        allowed['timestamp_millis'] = str(millis)
                    items.append({'state':'available','resource_id':result['current_resource_id'],'current_name':result['current_name'],'change':allowed})
                except Exception:
                    items.append({'state':'unavailable'})
                if manager.account_id(user) != account or manager.username_for_account_id(account) != user or require_user(request) != user: raise ValueError()
            return JSONResponse({'version':1,'items':items}, headers=headers)
        except Exception:
            return JSONResponse({'detail':'Captured changes unavailable'}, status_code=503, headers=headers)

    @router.post('/captured-change')
    async def captured_change(request: Request):
        from fastapi.responses import JSONResponse
        headers={'Cache-Control':'private, no-store'}
        try:return JSONResponse(await _captured_change(request),headers=headers)
        except HTTPException as exc:return JSONResponse({'detail':exc.detail},status_code=exc.status_code,headers={**(exc.headers or {}),**headers})
        except Exception:return JSONResponse({'detail':'Captured change is unavailable'},status_code=503,headers=headers)

    def _history_writer_display(change):
        if type(change) is not dict: return change
        result = {k:v for k,v in change.items() if k != 'writer_identity'}
        value = change.get('writer_identity')
        keys = {'version','session_id','actor_id','execution_revision','root_operation_id','inner_turn_id','tool_call_id','source'}
        source_keys = {'session_id','actor_id','execution_revision','root_operation_id','inner_turn_id'}
        if (type(value) is not dict or set(value) != keys or value.get('version') != 1 or type(value.get('version')) is not int
                or change.get('actor_kind') != 'agent' or change.get('tool_id') != 'workflow_artifact'
                or type(change.get('actor_id')) is not str or not change['actor_id'].startswith('agent:') or len(change['actor_id']) <= 6): return result
        source = value.get('source')
        if type(source) is not dict or set(source) != source_keys or source.get('root_operation_id') != value.get('root_operation_id'): return result
        def display_text(item):
            try:
                return type(item) is str and bool(item) and len(item.encode('utf-8')) <= 256 and not any(ord(c) < 32 or ord(c) == 127 for c in item)
            except UnicodeError:
                return False
        if not display_text(change['actor_id']): return result
        for row in (value, source):
            if type(row.get('execution_revision')) is not int or not 0 <= row['execution_revision'] <= 9007199254740991: return result
            if any(not display_text(row.get(name)) for name in ('session_id','actor_id','root_operation_id','inner_turn_id')): return result
        if not display_text(value.get('tool_call_id')): return result
        result['writer_identity'] = {**value, 'source':dict(source)}
        return result

    def _history_writer_page(page, grouped=False):
        page = dict(page)
        if grouped:
            page['groups'] = [{**group, 'actions':[{**member, 'change':_history_writer_display(member.get('change'))} for member in group['actions']]} for group in page['groups']]
        else:
            page['items'] = [{**item, 'change':_history_writer_display(item.get('change'))} if 'change' in item else _history_writer_display(item) for item in page['items']]
        return page

    async def managed_change_fragments(resource_id:str,request:Request,limit:int=Query(default=50,ge=1,le=100),cursor:str|None=Query(default=None,max_length=4096)):
        from src.auth_helpers import is_internal_tool_identity
        user=require_user(request);manager=getattr(request.app.state,'auth_manager',None)
        def current(account):
            return bool(manager and require_user(request)==user and not is_internal_tool_identity(user) and not getattr(request.state,'api_token',False)
                and not getattr(request.state,'internal_tool_authenticated',False) and (effective_user(request) or user)==user
                and manager.account_id(user)==account and manager.username_for_account_id(account)==user)
        account=manager.account_id(user) if manager is not None else None
        if not account or not current(account):raise HTTPException(403,'History account is unavailable')
        try:
            client=service_client(account,request)
            candidates=await resolve_history_resource_ids(client,resource_id,request,user,account)
            if len(candidates)!=1 or not current(account):raise ValueError('History resource unavailable')
            page=await _bounded_history_io(lambda:client.list_managed_change_fragments(candidates[0],cursor=cursor,limit=limit))
            if not current(account) or await resolve_history_resource_ids(client,resource_id,request,user,account)!=candidates or not current(account):raise ValueError('History resource changed')
            return {'resource_id':resource_id,**_history_writer_page(page,grouped=True)}
        except HistoryAvailabilityError as exc:raise availability_http_error(exc) from exc
        except HTTPException:raise
        except (HistoryClientError,OSError,ValueError) as exc:raise HTTPException(503,'Managed change history is unavailable') from exc

    router.add_api_route('/resources/{resource_id}/managed-change-fragments',managed_change_fragments,methods=['GET'],route_class_override=_PrivateHistoryRoute)

    async def ordered_managed_changes(resource_id:str,request:Request,limit:int=Query(default=50,ge=1,le=100),cursor:str|None=Query(default=None,max_length=16384),origin_action_id:str|None=Query(default=None,max_length=256)):
        from src.auth_helpers import is_internal_tool_identity
        user=require_user(request);manager=getattr(request.app.state,'auth_manager',None)
        def current(account):
            return bool(manager and require_user(request)==user and not is_internal_tool_identity(user) and not getattr(request.state,'api_token',False)
                and not getattr(request.state,'internal_tool_authenticated',False) and (effective_user(request) or user)==user
                and manager.account_id(user)==account and manager.username_for_account_id(account)==user)
        account=manager.account_id(user) if manager is not None else None
        if not account or not current(account):raise HTTPException(403,'History account is unavailable')
        try:
            client=service_client(account,request);candidates=await resolve_history_resource_ids(client,resource_id,request,user,account)
            if len(candidates)!=1 or not current(account):raise ValueError('History resource unavailable')
            page=await _bounded_history_io(lambda:client.list_ordered_managed_changes(candidates[0],origin_action_id=origin_action_id,cursor=cursor,limit=limit))
            if not current(account) or await resolve_history_resource_ids(client,resource_id,request,user,account)!=candidates or not current(account):raise ValueError('History resource changed')
            return {'resource_id':resource_id,**_history_writer_page(page)}
        except HistoryAvailabilityError as exc:raise availability_http_error(exc) from exc
        except HTTPException:raise
        except (HistoryClientError,OSError,ValueError) as exc:raise HTTPException(503,'Ordered change history is unavailable') from exc

    router.add_api_route('/resources/{resource_id}/ordered-managed-changes',ordered_managed_changes,methods=['GET'],route_class_override=_PrivateHistoryRoute)

    async def cross_resource_origin(resource_id:str,request:Request,origin_action_id:str=Query(min_length=1,max_length=512),cursor:str|None=Query(default=None,max_length=16384)):
        """Bounded provenance fragments; each projected Files resource is separately authorized."""
        from pathlib import Path
        from src.auth_helpers import is_internal_tool_identity
        from src.secret_storage import encrypt,decrypt
        from src.openclank.files_host_provider import host_origin_for_path
        from src.openclank.resource_refs import stable_resource_id
        user=require_user(request);manager=getattr(request.app.state,'auth_manager',None)
        account=manager.account_id(user) if manager else None
        def account_current():
            try:return bool(account and require_user(request)==user and not is_internal_tool_identity(user)
                and not getattr(request.state,'api_token',False) and not getattr(request.state,'internal_tool_authenticated',False)
                and (effective_user(request) or user)==user and manager.account_id(user)==account and manager.username_for_account_id(account)==user)
            except Exception:return False
        if not account_current() or not re.fullmatch(r'resource-[0-9a-f]{64}',resource_id):raise HTTPException(403,'History unavailable')
        policy=getattr(request.app.state,"files_policy_repository",None)
        if policy is None:raise HTTPException(503,'Files authority unavailable')
        try:
            generation=policy.generation();client=service_client(account,request)
            source_ids=await resolve_history_resource_ids(client,resource_id,request,user,account,policy_repository=policy)
            if len(source_ids)!=1 or not account_current() or policy.generation()!=generation:raise ValueError('source unavailable')
            native_cursor=None
            if cursor is not None:
                if not cursor.startswith('oh1.') or len(cursor)>16384:raise HTTPException(409,'History page needs refresh')
                try:decoded=json.loads(decrypt('enc:'+cursor[4:]))
                except Exception:raise HTTPException(409,'History page needs refresh')
                if (type(decoded) is not dict or set(decoded)!={'v','account','resource','action','generation','native'} or decoded.get('v')!=1
                    or decoded.get('account')!=account or decoded.get('resource')!=resource_id or decoded.get('action')!=origin_action_id
                    or decoded.get('generation')!=generation or type(decoded.get('native')) is not dict):raise HTTPException(409,'History page needs refresh')
                native_cursor=decoded['native']
            page=await _bounded_history_io(lambda:client.cross_origin_candidates(source_ids[0],origin_action_id,cursor=native_cursor))
            proof={'incarnation':page['incarnation'],'origin_revision':page['origin_revision'],'registry_revision':page['registry_revision'],'authority_generation':page['authority_generation']}
            output=[];validated=[];filtered=False;last=None;stopped=False
            def authorize(candidate):
                row=candidate.get('registration');key=candidate.get('resource')
                if (type(row) is not dict or type(key) is not dict or row.get('active') is not True or row.get('account_id')!=account
                    or row.get('resource_id')!=key.get('resource_id') or row.get('workspace_id')!=key.get('workspace_id') or key.get('account_id')!=account):
                    return None
                actors=row.get('actor_ids');relative=row.get('relative_path');root_path=row.get('root_path');root_id=row.get('root_id')
                if (not isinstance(actors,list) or (actors and client.actor_id not in actors) or not isinstance(relative,str) or not relative
                    or not isinstance(root_path,str) or not Path(root_path).is_absolute() or not isinstance(root_id,str) or not root_id
                    or Path(relative).is_absolute() or any(part in {'.','..'} for part in Path(relative).parts)):
                    return None
                location=policy.get_location(root_id)
                if (not location.enabled or location.availability!='available' or location.kind!='directory'
                    or os.path.normpath(str(location.canonical_path))!=os.path.normpath(root_path)):return None
                scope=policy.resolve(subject_id=account,is_admin=is_admin(request,user),origin='app',location_id=root_id,
                    capability='read',relative_resource=relative,observation_only=True)
                if not scope.allowed or scope.policy_generation!=generation:return None
                path=str(Path(root_path)/relative);origin=host_origin_for_path(path)
                return {'resource_id':stable_resource_id(owner_subject_id=account,provider='host',origin_id=origin),
                    'name':Path(relative).name,'recorded_millis':candidate.get('recorded_millis'),'state':candidate.get('state'),'availability':candidate.get('availability'),'locator':candidate.get('locator')}
            for index,candidate in enumerate(page['items']):
                last=candidate['position']
                try:public=authorize(candidate)
                except Exception:public=None
                if public is None:filtered=True;continue
                output.append(public);validated.append(candidate)
                if len(output)==20:
                    stopped=index+1<len(page['items']) or page['next'] is not None
                    break
            if not account_current() or policy.generation()!=generation:raise HTTPException(409,'History page needs refresh')
            for candidate,previous in zip(validated,output):
                try:again=authorize(candidate)
                except Exception:again=None
                if again!=previous:raise HTTPException(409,'History page needs refresh')
            fence=await _bounded_history_io(lambda:client.cross_origin_fence(source_ids[0],origin_action_id))
            if not account_current() or policy.generation()!=generation or fence!=proof:raise HTTPException(409,'History page needs refresh')
            more=bool(stopped or page['next'] is not None)
            next_cursor=None
            if more and last:
                native={**proof,'after':last};sealed=encrypt(json.dumps({'v':1,'account':account,'resource':resource_id,'action':origin_action_id,'generation':generation,'native':native},sort_keys=True,separators=(',',':')))
                if not isinstance(sealed,str) or not sealed.startswith('enc:') or len(sealed)>16380:raise ValueError('cursor unavailable')
                next_cursor='oh1.'+sealed[4:]
            return {'version':1,'coverage':'recorded_origin_fragment','order':'recorded_time_then_unknown','items':output,'partial':bool(filtered or more),'next_cursor':next_cursor}
        except HTTPException:raise
        except HistoryAvailabilityError as exc:
            if exc.code=='stale_cursor':raise HTTPException(409,'History page needs refresh')
            raise availability_http_error(exc)
        except HistoryClientError:
            if cursor is not None:raise HTTPException(409,'History page needs refresh')
            raise HTTPException(503,'Origin history is unavailable')
        except Exception:raise HTTPException(503,'Origin history is unavailable')

    router.add_api_route('/resources/{resource_id}/cross-resource-origin',cross_resource_origin,methods=['GET'],route_class_override=_PrivateHistoryRoute)

    async def workspace_recorded_activity(resource_id:str,request:Request,cursor:str|None=Query(default=None,max_length=16384),
        graph_activity:bool=False,graph_workspace_id:str|None=Query(default=None,max_length=256),
        graph_checkout_id:str|None=Query(default=None,max_length=256)):
        """Recorded direct-action fragments for the selected resource's current root."""
        from pathlib import Path
        from src.auth_helpers import is_internal_tool_identity
        from src.secret_storage import encrypt,decrypt
        from src.openclank.files_facade import ProviderContext
        from src.openclank.files_host_provider import host_origin_for_path
        from src.openclank.resource_refs import stable_resource_id
        from src.openclank.history_client import registered_resource_pages
        from src.openclank.history_graph_activity import project_qualified_history_member, summarize_history_page
        from src.openclank.code_query import resolve_query_binding
        from src.openclank.code_index_lifecycle import workspace_index_event

        if (graph_workspace_id is None) != (graph_checkout_id is None) or (graph_workspace_id is not None and not graph_activity):
            raise HTTPException(400,'Invalid Graph activity scope')
        user=require_user(request);manager=getattr(request.app.state,'auth_manager',None)
        barrier=getattr(request.app.state,'account_request_barrier',None);policy=getattr(request.app.state,'files_policy_repository',None)
        account=manager.account_id(user) if manager else None
        supervisor=getattr(request.app.state,'history_supervisor',None);process=getattr(supervisor,'process',None)
        def current():
            try:return bool(account and barrier and policy and process and getattr(request.app.state,'auth_manager',None) is manager and getattr(request.app.state,'account_request_barrier',None) is barrier and getattr(request.app.state,'files_policy_repository',None) is policy and getattr(request.app.state,'history_supervisor',None) is supervisor and getattr(supervisor,'process',None) is process and process.poll() is None and require_user(request)==user and not is_internal_tool_identity(user) and not getattr(request.state,'api_token',False) and not getattr(request.state,'internal_tool_authenticated',False) and (effective_user(request) or user)==user and manager.account_id(user)==account and manager.username_for_account_id(account)==user and policy.generation()==generation and is_admin(request,user)==admin)
            except Exception:return False
        if not account or not barrier or not policy or not re.fullmatch(r'resource-[0-9a-f]{64}',resource_id):raise HTTPException(403,'History unavailable')
        try:
          async with barrier.admitted(user):
            generation=policy.generation();admin=is_admin(request,user);client=service_client(account,request)
            if not current():raise ValueError('authority unavailable')
            graph_root=None;graph_binding=None;graph_scope=None;graph_location=None
            if graph_workspace_id is not None:
                native,graph_binding=resolve_query_binding(repository=policy,auth_manager=manager,
                    owner=user,workspace_id=graph_workspace_id,purpose='app_folder')
                graph_root=native['root'];graph_location=native['location_id']
                status=await workspace_index_event(repository=policy,provider=getattr(request.app.state,'memory_provider',None),
                    auth_manager=manager,owner=user,workspace_id=graph_workspace_id,subscriber_id='history-graph-activity',
                    action='status',purpose='app_folder')
                if (not current() or status.get('workspace_id')!=native['workspace_id']
                    or status.get('workspace_revision')!=native['workspace_revision'] or status.get('policy_generation')!=generation
                    or status.get('checkout_id')!=graph_checkout_id or not status.get('head_generation')):
                    raise HTTPException(409,'Graph publication changed')
                graph_scope={'workspace_id':native['workspace_id'],'checkout_id':graph_checkout_id,
                    'workspace_revision':native['workspace_revision'],'policy_generation':generation}
            factory=getattr(request.app.state,'files_facade_factory',None)
            if not callable(factory):raise ValueError('Files unavailable')
            context=ProviderContext(owner_subject_id=account,owner_username=user,policy_generation=generation,is_admin=admin,workspace_id=None)
            provider=factory(request,context)._providers.get('host')
            if provider is None or provider.operation_store is not policy:raise ValueError('Files unavailable')
            async def resolve_anchor():
                if graph_root is None:
                    return await resolve_history_resource_ids(client,resource_id,request,user,account,policy_repository=policy)
                root_origin=host_origin_for_path(graph_root)
                root_public_id=stable_resource_id(owner_subject_id=account,provider='host',origin_id=root_origin)
                if resource_id!=root_public_id:
                    return await resolve_history_resource_ids(client,resource_id,request,user,account,policy_repository=policy)
                # Binding a Graph workspace does not create a Lore registration
                # on its directory. An existing member in this exact workspace
                # can anchor the same native window without registering anything.
                actual_root=await provider.resource_for_path(context,path=graph_root)
                if not current() or actual_root.kind!='folder' or actual_root.origin_id!=root_origin:
                    raise ValueError('Graph root unavailable')
                async def fetch_page(after):
                    return await _bounded_history_io(lambda:client.list_registered_resources(cursor=after,limit=100))
                async for page in registered_resource_pages(fetch_page):
                    if not current():raise ValueError('Graph root changed')
                    for member in page['items']:
                        if (member.get('active') is not True or member.get('account_id')!=account
                            or member.get('workspace_id')!=graph_scope['workspace_id'] or member.get('root_id')!=graph_location):
                            continue
                        actors=member.get('actor_ids')
                        if type(actors) is not list or (actors and client.actor_id not in actors):continue
                        root=Path(member['root_path']);relative=Path(member['relative_path'])
                        if not root.is_absolute() or relative.is_absolute() or '..' in relative.parts:continue
                        member_path=str(root/relative)
                        if os.path.realpath(member_path)!=member_path or os.path.commonpath((graph_root,member_path))!=graph_root:continue
                        try:actual=await provider.resource_for_path(context,path=member_path)
                        except Exception:continue
                        if not current():raise ValueError('Graph root changed')
                        if actual.origin_id==host_origin_for_path(member_path):return [member['resource_id']]
                raise HistoryAvailabilityError('resource_not_found','Graph root has no current Lore registration')
            source=await resolve_anchor()
            if len(source)!=1 or not current():raise ValueError('source unavailable')
            native_cursor=None;expected=None
            if cursor is not None:
                if not cursor.startswith('wa1.'):raise HTTPException(409,'History page needs refresh')
                try: decoded=json.loads(decrypt('enc:'+cursor[4:]))
                except Exception:raise HTTPException(409,'History page needs refresh')
                fields={'v','account','anchor','generation','root_id','workspace_id','provider','native'}
                if graph_activity: fields.add('graph_scope')
                if type(decoded) is not dict or set(decoded)!=fields or decoded.get('graph_scope')!=graph_scope or decoded['v']!=1 or decoded['account']!=account or decoded['anchor']!=resource_id or decoded['generation']!=generation or decoded['provider']!='mimo-filesystem' or type(decoded['native']) is not dict:raise HTTPException(409,'History page needs refresh')
                native_cursor=decoded['native'];expected=(decoded['root_id'],decoded['workspace_id'])
            arguments={'cursor':native_cursor}
            if graph_activity: arguments['graph_activity']=True
            page=await _bounded_history_io(lambda:client.workspace_activity_candidates(source[0],**arguments))
            proof={k:page[k] for k in ('incarnation','scopes_digest','registry_revision','authority_generation','root_id','workspace_id')}
            if (graph_scope is not None and (proof['root_id']!=graph_location or proof['workspace_id']!=graph_scope['workspace_id'])):
                raise HTTPException(409,'Recorded activity belongs to a different workspace')
            if not current() or (expected is not None and expected!=(proof['root_id'],proof['workspace_id'])):raise HTTPException(409,'History page needs refresh')
            async def authorize(candidate):
                row=candidate['registration'];key=candidate['resource']
                if type(row) is not dict or row.get('active') is not True or row.get('account_id')!=account or row.get('workspace_id')!=proof['workspace_id'] or row.get('root_id')!=proof['root_id'] or key.get('account_id')!=account or key.get('workspace_id')!=proof['workspace_id'] or key.get('provider')!='mimo-filesystem' or row.get('resource_id')!=key.get('resource_id') or candidate['availability']!='available' or candidate['locator'] is None:return None
                actors=row.get('actor_ids');relative=row.get('relative_path');root=row.get('root_path')
                if type(actors) is not list or (actors and client.actor_id not in actors) or type(relative) is not str or not relative or type(root) is not str or not Path(root).is_absolute() or Path(relative).is_absolute() or any(x in {'.','..'} for x in Path(relative).parts):return None
                location=policy.get_location(proof['root_id'])
                if not location.enabled or location.availability!='available' or location.kind!='directory' or os.path.normpath(str(location.canonical_path))!=os.path.normpath(root):return None
                scope=policy.resolve(subject_id=account,is_admin=admin,origin='app',location_id=proof['root_id'],capability='read',relative_resource=relative,observation_only=True)
                if not scope.allowed or scope.policy_generation!=generation:return None
                path=str(Path(root)/relative);origin=host_origin_for_path(path)
                actual=await provider.resource_for_path(context,path=path)
                if not current() or actual.origin_id!=origin:return None
                public={'resource_id':stable_resource_id(owner_subject_id=account,provider='host',origin_id=origin),
                    'name':Path(relative).name,'recorded_millis':candidate['recorded_millis'],'state':candidate['state'],'locator':candidate['locator']}
                if not graph_activity: return public
                if graph_root is not None and (os.path.realpath(path)!=path or os.path.commonpath((graph_root,path))!=graph_root):
                    return None
                return project_qualified_history_member(candidate,public,path=path,graph_root=graph_root,graph_scope=graph_scope)
            output=[];validated=[];last=None;stopped=False
            for index,candidate in enumerate(page['items']):
                last=candidate['position']
                try:public=await authorize(candidate)
                except Exception:public=None
                if public is None:continue
                output.append(public);validated.append(candidate)
                if len(output)==20:stopped=index+1<len(page['items']) or page['next'] is not None;break
            for candidate,previous in zip(validated,output):
                if await authorize(candidate)!=previous:raise HTTPException(409,'History page needs refresh')
            again=await resolve_anchor()
            fence=await _bounded_history_io(lambda:client.workspace_activity_fence(source[0]))
            if not current() or again!=source or fence!=proof:raise HTTPException(409,'History page needs refresh')
            if graph_binding is not None:
                _,bound_now=resolve_query_binding(repository=policy,auth_manager=manager,
                    owner=user,workspace_id=graph_workspace_id,purpose='app_folder')
                if bound_now!=graph_binding or not current():raise HTTPException(409,'Graph activity scope changed')
            next_cursor=None
            if (stopped or page['next'] is not None) and last:
                native={k:proof[k] for k in ('incarnation','scopes_digest','registry_revision','authority_generation')};native['after']=last
                token={'v':1,'account':account,'anchor':resource_id,'generation':generation,'root_id':proof['root_id'],
                    'workspace_id':proof['workspace_id'],'provider':'mimo-filesystem','native':native}
                if graph_activity: token['graph_scope']=graph_scope
                sealed=encrypt(json.dumps(token,sort_keys=True,separators=(',',':')))
                if not isinstance(sealed,str) or not sealed.startswith('enc:') or len(sealed)>16380:raise ValueError('cursor unavailable')
                next_cursor='wa1.'+sealed[4:]
            result={'version':1,'scope':'one_registered_root','coverage':'partial_recorded_activity','order':'recorded_timestamp_action_resource','items':output,'next_cursor':next_cursor}
            if graph_activity: result['projection']=summarize_history_page(output,graph_scope)
            return result
        except HTTPException:raise
        except HistoryAvailabilityError as exc:
            if exc.code=='stale_cursor':raise HTTPException(409,'History page needs refresh')
            raise availability_http_error(exc)
        except HistoryClientError:
            if cursor is not None:raise HTTPException(409,'History page needs refresh')
            raise HTTPException(503,'Workspace activity unavailable')
        except Exception:raise HTTPException(503,'Workspace activity unavailable')

    router.add_api_route('/resources/{resource_id}/workspace-recorded-activity',workspace_recorded_activity,methods=['GET'],route_class_override=_PrivateHistoryRoute)

    async def memery_versions(memory_id: str, request: Request):
        from src.memory_versioned import VersionedMemoryNotFound

        from services.memory.memery_history import account_current, change_key, client_for

        require_user(request)
        owner = get_current_user(request)
        if not owner or len(memory_id) > 512:
            raise HTTPException(404, "Memery record unavailable")
        state = request.app.state
        account = state.auth_manager.account_id(owner)
        if not account_current(state, owner, account):
            raise HTTPException(403, "Memery authorization unavailable")
        provider = getattr(state, "memory_provider", None)
        if provider is None:
            raise HTTPException(503, "Memery is unavailable")
        manifest = None
        if getattr(state, "history_supervisor", None) is not None:
            try:
                client = client_for(state.history_supervisor, account)
                manifest = await _bounded_history_io(client.get_memery_manifest, timeout=1.0)
            except (HTTPException, HistoryClientError):
                pass
        try:
            detail = await provider.versioned_detail(memory_id, owner=owner)
        except VersionedMemoryNotFound:
            raise HTTPException(404, "Memery record unavailable") from None
        retained = {(r["block"], r["revision"], r["change"], r["digest"]) for r in
                    manifest["entries"]} if manifest is not None else set()
        for row in detail["history"]:
            key = (detail["block_id"], row["revision"], change_key(row["change_set_id"]), row["content_hash"])
            row["history_reference"] = ("retained" if key in retained else
                                        "unavailable" if manifest is None else "unpublished")
        if get_current_user(request) != owner or not account_current(state, owner, account):
            raise HTTPException(403, "Memery authorization changed")
        return detail

    router.add_api_route("/memery/{memory_id}/versions", memery_versions, methods=["GET"],
                         route_class_override=_PrivateHistoryRoute)

    async def recorded_memery(request: Request, offset: int = Query(default=0, ge=0, le=512)):
        from urllib.parse import quote
        from services.memory.memery_history import account_current, client_for, _query_batch
        from src.openclank.chat_source_bridge import canonical_source_provider

        owner = require_user(request)
        state = request.app.state
        account = state.auth_manager.account_id(owner)
        if get_current_user(request) != owner or not account_current(state, owner, account):
            raise HTTPException(403, "Memery authorization unavailable")
        provider = canonical_source_provider(state) or getattr(state, "memory_provider", None)
        path = provider._local_db_path() if hasattr(provider, "_local_db_path") else None
        if not path or not getattr(state, "history_supervisor", None):
            raise HTTPException(503, "Recorded Memery changes are unavailable")
        scope = provider._scope(owner)
        client = client_for(state.history_supervisor, account)
        items = []
        for assets in (False, True):
            manifest = await _bounded_history_io(lambda: client.get_memery_manifest(assets=assets))
            _, _, _, removed = await _bounded_history_io(lambda: _query_batch(
                path, scope.owner, scope.workspace_id, manifest, assets=assets, enumerate_only=True))
            for entry in manifest["entries"]:
                if entry["change"] in removed:
                    continue
                resource = quote(entry["block"], safe="")
                items.append({"resource_id": entry["block"], "revision": entry["revision"],
                              "recorded_at_millis": entry["at"],
                              "category": "memery_artifacts" if assets else
                                          "memery_evidence" if entry["evidence"] else "memery_records",
                              "detail_url": f"/api/memory/assets/{resource}" if assets else
                                            f"/api/history/memery/{resource}/versions"})
        if get_current_user(request) != owner or not account_current(state, owner, account):
            raise HTTPException(403, "Memery authorization changed")
        items.sort(key=lambda v: (v["recorded_at_millis"], v["resource_id"], v["revision"]), reverse=True)
        return {"items": items[offset:offset + 50], "next_offset": offset + 50 if len(items) > offset + 50 else None}

    router.add_api_route("/memery/recorded", recorded_memery, methods=["GET"], route_class_override=_PrivateHistoryRoute)

    async def checkpoint_memery(memory_id: str, revision: int, request: Request):
        from services.memory.memery_history import account_current, client_for, reference
        from src.openclank.analysis_snapshot_host import snapshot_named_human

        owner = snapshot_named_human(request)
        state = request.app.state
        account = state.auth_manager.account_id(owner)
        if not account_current(state, owner, account):
            raise HTTPException(403, "Memery authorization unavailable")
        detail = await state.memory_provider.versioned_detail(memory_id, owner=owner)
        row = next((v for v in detail["history"] if v["revision"] == revision), None)
        if row is None:
            raise HTTPException(404, "Memery revision unavailable")
        item = reference({**row, "evidence": row["reason"] == "approved analysis snapshot"})
        client = client_for(state.history_supervisor, account)
        manifest = await _bounded_history_io(client.get_memery_manifest)
        if snapshot_named_human(request) != owner or not account_current(state, owner, account):
            raise HTTPException(403, "Memery authorization changed")
        result = await _bounded_history_io(lambda: client.settle_memery_manifest(
            manifest["generation"], manifest["position"], manifest["witness"], [item], [], checkpoint=True))
        if not any(entry["change"] == item["change"] for entry in result["entries"]):
            raise HTTPException(409, "History settings exclude this revision")
        return {"recorded": True, "revision": revision}

    router.add_api_route("/memery/{memory_id}/versions/{revision}/checkpoint", checkpoint_memery, methods=["POST"],
                         route_class_override=_PrivateHistoryRoute)

    async def recover_memery_component(memory_id: str, resource_id: str, request: Request):
        from fastapi.responses import Response
        from src.openclank.analysis_snapshot_host import snapshot_named_human, with_saved_snapshot

        owner = snapshot_named_human(request)
        account = account_id_for(request, owner)
        state = request.app.state
        if len(memory_id) > 512 or not re.fullmatch(r"resource-[0-9a-f]{64}", resource_id):
            raise HTTPException(404, "Saved component unavailable")

        async def recover(_utility, _reference, manifest, current):
            matches = [n for n in manifest["components"] if n["kind"] == "file" and n["resource_id"] == resource_id]
            if len(matches) != 1 or manifest["account_id"] != account:
                raise HTTPException(404, "Saved component unavailable")
            node = matches[0]
            if node["size_bytes"] > 64 * 1024 * 1024:
                raise HTTPException(413, "Component exceeds the 64 MiB recovery limit")
            policy = state.files_policy_repository
            generation = policy.generation()
            client = service_client(account, request)

            async def authorized():
                if policy.generation() != generation:
                    raise HTTPException(409, "Files access changed")
                return await resolve_history_resource_ids(client, resource_id, request, owner, account,
                                                          policy_repository=policy, allow_absent=True)

            from services.memory.memery_history import change_key, client_for
            from src.openclank.analysis_projects import read_only_project
            from src.openclank.files_facade import ProviderContext
            from src.openclank.memery_component_history import component_path

            detail = await state.s05_profile.provider.versioned_detail(memory_id, owner=owner)
            change = change_key(detail["current"]["change_set_id"])
            owned = client_for(state.history_supervisor, account)
            location = await _bounded_history_io(lambda: owned.memery_component_location(
                change, node["sha256"], resource_id))
            if location is not None:
                workspace = manifest["workspace_id"]
                context = ProviderContext(owner_subject_id=account, owner_username=owner,
                    policy_generation=generation, is_admin=is_admin(request, owner), workspace_id=workspace)
                project = read_only_project(facade=state.files_facade_factory(request, context),
                    context=context, repository=policy, auth_manager=state.auth_manager, owner=owner,
                    account=account, workspace_id=workspace)
                if project.identity["projectKey"] != manifest["project_key"]:
                    raise HTTPException(403, "Component project changed")
                await project._check()
                component_path(project, policy, location, resource_id)
                data = bytearray()
                parts = max(1, (node["size_bytes"] + 262143) // 262144)
                for part in range(parts):
                    if policy.generation() != generation:
                        raise HTTPException(403, "Component access changed")
                    content = await _bounded_history_io(lambda: owned.read_memery_component_part(
                        change, node["sha256"], resource_id, part))
                    if content is None or len(data) + len(content) > node["size_bytes"]:
                        raise HTTPException(410, "Memery component unavailable")
                    data.extend(content)
                if len(data) != node["size_bytes"] or hashlib.sha256(data).hexdigest() != node["sha256"]:
                    raise HTTPException(409, "Retained component checksum changed")
                await current()
                await project._check()
                component_path(project, policy, location, resource_id)
                return Response(content=bytes(data), media_type="application/octet-stream",
                    headers={"Content-Disposition": "attachment", "X-Content-Type-Options": "nosniff"})

            ids = await authorized()
            cursor = None
            version = None
            for _ in range(4):
                page = await list_history_versions_for_candidates(client, ids, cursor=cursor, limit=50)
                version = next((item["id"] for item in page["items"]
                                if item.get("can_read") and item.get("size_bytes") == node["size_bytes"]
                                and item.get("fingerprint") == node["revision"]["value"]), None)
                cursor = page.get("next_cursor")
                if version or not cursor:
                    break
            if version is None:
                raise HTTPException(404, "No matching retained Files version found")
            private = await find_history_version_resource_id(client, ids, version)
            data = bytearray()
            while True:
                if policy.generation() != generation:
                    raise HTTPException(409, "Files access changed")
                offset = len(data)
                chunk = await _bounded_history_io(lambda: client.read_resource_version_chunk(
                    private, version, offset=offset, length=256 * 1024))
                content = chunk["content"]
                if (not isinstance(content, bytes) or chunk["offset"] != offset or
                        len(data) + len(content) > node["size_bytes"] or (not content and not chunk["eof"])):
                    raise HTTPException(409, "Retained version is incomplete")
                data.extend(content)
                if chunk["eof"]:
                    break
            if (len(data) != node["size_bytes"] or hashlib.sha256(data).hexdigest() != node["sha256"]
                    or await authorized() != ids):
                raise HTTPException(409, "Retained version does not match the saved component")
            await current()
            return Response(content=bytes(data), media_type="application/octet-stream",
                            headers={"Content-Disposition": "attachment", "X-Content-Type-Options": "nosniff"})

        return await with_saved_snapshot(request=request, provider=state.s05_profile.provider, batch_store=None,
            user=owner, original_account=account, memory_id=memory_id, human_current=lambda: snapshot_named_human(request),
            callback=recover, verified_capture_only=True)

    router.add_api_route("/memery/{memory_id}/components/{resource_id}", recover_memery_component,
                         methods=["GET"], route_class_override=_PrivateHistoryRoute)

    @router.get("/resources/{resource_id}/changes")
    async def list_history_resource_changes(
        resource_id: str,
        request: Request,
        limit: int = Query(default=50, ge=1, le=100),
        cursor: str | None = Query(default=None, max_length=4096),
    ) -> Dict[str, Any]:
        user = require_user(request)
        owner = effective_user(request) or user
        account_id = account_id_for(request, owner)
        try:
            client = service_client(account_id, request)
            candidates = await resolve_history_resource_ids(client, resource_id, request, owner, account_id)
            if len(candidates) != 1:
                raise HistoryAvailabilityError("change_resource_ambiguous", "Change groups are unavailable for this resource's multiple History identities.")
            page = await _bounded_history_io(lambda: client.list_resource_changes(candidates[0], cursor=cursor, limit=limit))
        except HistoryAvailabilityError as exc:
            raise availability_http_error(exc) from exc
        except HTTPException:
            raise
        except (HistoryClientError, OSError, ValueError) as exc:
            raise HTTPException(503, "Change history is unavailable.") from exc
        return {"resource_id": resource_id, **_history_writer_page(page)}

    @router.get("/resources/removed-files")
    async def list_removed_files(
        request: Request,
        folder_ref: str = Query(min_length=1, max_length=4096),
        limit: int = Query(default=25, ge=1, le=50),
        cursor: int = Query(default=0, ge=0, le=100000),
    ) -> Dict[str, Any]:
        """Removed files directly inside the Files folder that is open: names, removal time and size only."""
        from src.openclank.files_facade import FilesFacadeError
        user = require_user(request)
        owner = effective_user(request) or user
        account_id = account_id_for(request, owner)
        try:
            from src.openclank.files_facade import ProviderContext
            from src.openclank.files_host_provider import _path
            from src.openclank.history_removed_files import removed_files
            store = getattr(request.app.state, "files_policy_repository", None)
            factory = getattr(request.app.state, "files_facade_factory", None)
            if store is None or not callable(factory):
                raise HistoryAvailabilityError("resource_unavailable", "Files authority unavailable")
            context = ProviderContext(owner_subject_id=account_id, owner_username=owner, policy_generation=store.generation(),
                                      is_admin=is_admin(request, owner), workspace_id=None)
            facade = factory(request, context)
            provider = facade._providers.get("host")
            if provider is None or provider.operation_store is not store:
                raise HistoryAvailabilityError("resource_unavailable", "Files authority unavailable")
            _provider, ref = facade._provider_for_ref(context, folder_ref, capability="children")
            if ref.provider != "host":
                raise HistoryAvailabilityError("resource_not_found", "folder unavailable")
            client = service_client(account_id, request)
            return await removed_files(client, account=account_id, context=context, provider=provider,
                                       folder_path=_path(ref.origin_id), cursor=cursor, limit=limit)
        except HistoryAvailabilityError as exc:
            raise availability_http_error(exc) from exc
        except HTTPException:
            raise
        except FilesFacadeError as exc:
            raise HTTPException(409, "Removed files unavailable") from exc
        except (HistoryClientError, OSError, ValueError) as exc:
            raise HTTPException(503, f"history service unavailable: {exc}") from exc

    @router.get("/resources/folder-changes")
    async def list_folder_changes(
        request: Request,
        folder_ref: str = Query(min_length=1, max_length=4096),
        limit: int = Query(default=25, ge=1, le=50),
        cursor: int = Query(default=0, ge=0, le=100000),
    ) -> Dict[str, Any]:
        """Files History recorded in the open Files folder: state, time, action and size only."""
        from src.openclank.files_facade import FilesFacadeError
        user = require_user(request)
        owner = effective_user(request) or user
        account_id = account_id_for(request, owner)
        try:
            from src.openclank.files_facade import ProviderContext
            from src.openclank.files_host_provider import _path
            from src.openclank.history_removed_files import folder_changes
            store = getattr(request.app.state, "files_policy_repository", None)
            factory = getattr(request.app.state, "files_facade_factory", None)
            if store is None or not callable(factory):
                raise HistoryAvailabilityError("resource_unavailable", "Files authority unavailable")
            context = ProviderContext(owner_subject_id=account_id, owner_username=owner, policy_generation=store.generation(),
                                      is_admin=is_admin(request, owner), workspace_id=None)
            facade = factory(request, context)
            provider = facade._providers.get("host")
            if provider is None or provider.operation_store is not store:
                raise HistoryAvailabilityError("resource_unavailable", "Files authority unavailable")
            _provider, ref = facade._provider_for_ref(context, folder_ref, capability="children")
            if ref.provider != "host":
                raise HistoryAvailabilityError("resource_not_found", "folder unavailable")
            client = service_client(account_id, request)
            return await folder_changes(client, account=account_id, context=context, provider=provider,
                                       folder_path=_path(ref.origin_id), cursor=cursor, limit=limit)
        except HistoryAvailabilityError as exc:
            raise availability_http_error(exc) from exc
        except HTTPException:
            raise
        except FilesFacadeError as exc:
            raise HTTPException(409, "Removed files unavailable") from exc
        except (HistoryClientError, OSError, ValueError) as exc:
            raise HTTPException(503, f"history service unavailable: {exc}") from exc

    @router.get("/resources/{resource_id}/versions")
    async def list_history_resource_versions(
        resource_id: str,
        request: Request,
        limit: int = Query(default=50, ge=1, le=100),
        cursor: str | None = Query(default=None, max_length=2048),
    ) -> Dict[str, Any]:
        """List bounded Lore version metadata for an authenticated Files resource."""
        user = require_user(request)
        owner = effective_user(request) or user
        account_id = account_id_for(request, owner)
        try:
            client = service_client(account_id, request)
            private_resource_ids = await resolve_history_resource_ids(
                client, resource_id, request, owner, account_id, allow_absent=True
            )
            page = await list_history_versions_for_candidates(
                client,
                private_resource_ids,
                cursor=cursor,
                limit=limit,
            )
        except HistoryAvailabilityError as exc:
            raise availability_http_error(exc) from exc
        except HTTPException:
            raise
        except (HistoryClientError, OSError, ValueError) as exc:
            raise HTTPException(503, f"history service unavailable: {exc}") from exc
        return {"resource_id": resource_id, **page}

    async def _read_history_resource_version(
        resource_id: str,
        version_ref: str,
        request: Request,
        offset: int = Query(default=0, ge=0),
        limit: int = Query(default=64 * 1024, ge=1, le=256 * 1024),
        strict_account: bool = Query(default=False),
    ) -> Dict[str, Any]:
        """Read one bounded base64 chunk of one exact listed version."""
        user = require_user(request)
        owner = effective_user(request) or user
        account_id = account_id_for(request, owner)
        def strict_current():
            from src.auth_helpers import is_internal_tool_identity
            manager=getattr(request.app.state,'auth_manager',None)
            return bool(manager and require_user(request)==user and (effective_user(request) or user)==user
                and not is_internal_tool_identity(user) and not getattr(request.state,'api_token',False) and not getattr(request.state,'internal_tool_authenticated',False)
                and owner==user and manager.account_id(user)==account_id and manager.username_for_account_id(account_id)==user)
        if strict_account and not strict_current():raise HTTPException(403,'History account is unavailable')
        try:
            client = service_client(account_id, request)
            private_resource_ids = await resolve_history_resource_ids(
                client, resource_id, request, owner, account_id, allow_absent=True
            )
            chunk = await read_history_version_chunk_for_candidates(
                client,
                private_resource_ids,
                version_ref,
                offset=offset,
                length=limit,
            )
            if strict_account and (not strict_current() or await resolve_history_resource_ids(client,resource_id,request,owner,account_id)!=private_resource_ids or not strict_current()):raise HTTPException(403,'History account is unavailable')
        except HistoryAvailabilityError as exc:
            raise availability_http_error(exc) from exc
        except HTTPException:
            raise
        except (HistoryClientError, OSError, ValueError) as exc:
            raise HTTPException(503, f"history service unavailable: {exc}") from exc
        content = chunk.get("content")
        return {
            "resource_id": resource_id,
            "version": chunk["version"],
            "offset": chunk["offset"],
            "content_encoding": "base64",
            "content": base64.b64encode(content).decode("ascii") if content is not None else None,
            "eof": chunk["eof"],
        }

    async def read_history_resource_version(
        resource_id: str, version_ref: str, request: Request,
        offset: int = Query(default=0, ge=0), limit: int = Query(default=64 * 1024, ge=1, le=256 * 1024),
        strict_account: bool = Query(default=False),
    ):
        if not strict_account:
            return await _read_history_resource_version(resource_id,version_ref,request,offset,limit,False)
        from fastapi.responses import JSONResponse
        headers={'Cache-Control':'private, no-store'}
        try:return JSONResponse(await _read_history_resource_version(resource_id,version_ref,request,offset,limit,True),headers=headers)
        except HTTPException as exc:return JSONResponse({'detail':exc.detail},status_code=exc.status_code,headers={**(exc.headers or {}),**headers})
        except Exception:return JSONResponse({'detail':'History is unavailable'},status_code=503,headers=headers)

    router.add_api_route('/resources/{resource_id}/versions/{version_ref}',read_history_resource_version,methods=['GET'],route_class_override=_StrictHistoryVersionRoute)

    @router.get("/resources/{resource_id}/versions/{version_ref}/restore-preview")
    async def preview_history_resource_restore(
        resource_id: str,
        version_ref: str,
        request: Request,
    ) -> Dict[str, Any]:
        """Review the selected source against the current private Files target."""
        user = require_user(request)
        owner = effective_user(request) or user
        account_id = account_id_for(request, owner)
        try:
            client = service_client(account_id, request)
            private_resource_ids = await resolve_history_resource_ids(
                client, resource_id, request, owner, account_id, allow_absent=True
            )
            private_resource_id = await find_history_version_resource_id(
                client, private_resource_ids, version_ref
            )
            preview = await _bounded_history_io(
                lambda: client.preview_resource_version_restore(private_resource_id, version_ref)
            )
        except HistoryAvailabilityError as exc:
            raise availability_http_error(exc) from exc
        except HTTPException:
            raise
        except (HistoryClientError, OSError, ValueError) as exc:
            raise HTTPException(503, f"history service unavailable: {exc}") from exc
        return public_history_preview(preview, resource_id)

    @router.get("/settings")
    async def get_history_settings(request: Request) -> Dict[str, Any]:
        user = require_user(request)
        owner = effective_user(request) or user or "local-installation"

        def load_settings():
            admin = is_admin(request, user)
            result = visible_snapshot(owner, admin, request)
            result["owner"] = owner
            result["workspace_options"] = workspace_options(
                owner,
                request,
                result.get("policy") or {},
                admin=admin,
            )
            return result

        return await _bounded_history_io(load_settings)

    @router.put("/settings")
    async def put_history_settings(request: Request) -> Dict[str, Any]:
        user = require_user(request)
        try:
            body = await request.json()
        except Exception as exc:
            raise HTTPException(400, "history settings must be a JSON object") from exc
        if not isinstance(body, dict):
            raise HTTPException(422, "history settings must be a JSON object")
        try:
            expected = int(body.get("expected_revision"))
        except (TypeError, ValueError) as exc:
            raise HTTPException(428, "expected_revision is required") from exc
        requested = body.get("policy", body)
        if not isinstance(requested, dict):
            raise HTTPException(422, "policy must be an object")
        admin = is_admin(request, user)
        owner = effective_user(request) or user
        account_id = account_id_for(request, owner)
        if "expected_account_id" in body and body["expected_account_id"] != account_id:
            raise HTTPException(409, "Authenticated account changed")
        client = service_client(account_id, request)
        try:
            current_response = await _bounded_history_io(client.get_policy)
        except (HistoryClientError, OSError, ValueError) as exc:
            raise HTTPException(503, f"history service unavailable: {exc}") from exc
        current = dict(current_response.get("Policy") or {})
        if not admin and ("global" in requested or "capture_defaults" in requested):
            raise HTTPException(403, "installation history target requires administration")
        roots = authorized_roots(owner, admin)
        patch = dict(requested)
        if "scopes" in requested:
            requested_scopes = requested.get("scopes") or []
            if not isinstance(requested_scopes, list):
                raise HTTPException(422, "scopes must be an array")
            options = set(
                await _bounded_history_io(
                    lambda: workspace_options(owner, request, current, admin=admin)
                )
            )
            for scope in requested_scopes:
                if not isinstance(scope, dict):
                    raise HTTPException(422, "scope must be an object")
                scope_owner = str(scope.get("owner_account_id") or account_id).strip()
                if not admin and scope_owner != account_id:
                    raise HTTPException(403, "workspace history settings belong to their owner")
                kind = str(scope.get("kind") or "").strip().lower()
                if kind not in {"workspace", "directory"}:
                    raise HTTPException(422, "History scopes must select a workspace or directory")
                if kind == "workspace" and str(scope.get("workspace_id") or "").strip() not in options:
                    raise HTTPException(403, "workspace is not assigned to this account")
                if kind == "workspace" and not str(scope.get("workspace_id") or "").strip():
                    raise HTTPException(422, "workspace history settings require a workspace id")
                if kind == "directory":
                    root_id = str(scope.get("root_id") or "")
                    selected = next((root for root in roots if root.get("id") == root_id and root.get("kind") == "recursive_directory"), None)
                    if selected is None:
                        raise HTTPException(403, "directory history settings require an assigned Files root")
                    candidate = os.path.realpath(str(selected.get("canonical_path")))
                scope["scope_id"] = scope_id(
                    kind,
                    candidate if kind == "directory" else str(scope.get("workspace_id") or ""),
                    str(scope.get("workspace_id") or "") or None,
                    scope_owner,
                )
                scope["revision"] = int(current.get("revision") or expected) + 1
                scope["enabled"] = bool(scope.get("enabled", True))
                scope["kind"] = {"global": "Global", "workspace": "Workspace", "directory": "Directory"}.get(kind, scope.get("kind"))
                scope["owner_account_id"] = scope_owner
                if scope["kind"] == "Directory":
                    scope["root"] = candidate
                    scope.pop("root_id", None)
                    scope.pop("value", None)
            assigned_paths = {os.path.realpath(str(root.get("canonical_path"))) for root in roots}
            preserved = [
                scope for scope in current.get("scopes") or []
                if scope.get("kind") == "Global"
                or (scope.get("kind") == "Directory" and os.path.realpath(str(scope.get("root"))) not in assigned_paths)
                or (not admin and scope.get("owner_account_id") != account_id)
            ]
            patch["scopes"] = preserved + requested_scopes
        merged = dict(current)
        if isinstance(requested.get("global"), dict):
            merged["global"] = {**(current.get("global") or {}), **requested["global"]}
        if "scopes" in patch:
            merged["scopes"] = patch["scopes"]
        if "capture_defaults" in requested:
            defaults = requested["capture_defaults"]
            if type(defaults) is not dict or set(defaults) != set(current.get("capture_defaults") or {}):
                raise HTTPException(422, "Invalid installation capture defaults")
            merged["capture_defaults"] = defaults
        if "capture" in requested:
            capture = requested["capture"]
            if capture is not None and not isinstance(capture, dict):
                raise HTTPException(422, "capture must be an object or null")
            accounts = dict(current.get("accounts") or {})
            if capture is None:
                accounts.pop(account_id, None)
            else:
                defaults = current.get("capture_defaults") or {}
                if set(capture) != set(defaults):
                    raise HTTPException(422, "Invalid capture settings")
                accounts[account_id] = capture
            merged["accounts"] = accounts
        for key in ("revision",):
            merged[key] = current.get(key)
        # GetPolicy returns only authorized account rows. The worker merges those
        # rows against the current revision and retains every other account.
        submission = merged
        try:
            await _bounded_history_io(
                lambda: client.set_policy(submission, expected_revision=expected)
            )
        except HistoryClientError as exc:
            message = str(exc)
            status = 409 if "RevisionMismatch" in message or "revision" in message.lower() else 422
            raise HTTPException(status, message) from exc
        def load_updated_settings():
            result = visible_snapshot(owner, admin, request)
            result["owner"] = owner or "local-installation"
            result["workspace_options"] = workspace_options(
                owner,
                request,
                result.get("policy") or {},
                admin=admin,
            )
            return result

        return await _bounded_history_io(load_updated_settings)

    return router
