"""Private Files/History join for a Host-verified completed graph result.

This module is not a request schema or a source of graph authority. Its caller
must hold the authenticated Host operation and current Files/Native witnesses.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
from collections.abc import Mapping
from typing import Any, Callable

from src.openclank.files_service_client import FilesServiceError, OdysseusFilesClient

_FIELDS = frozenset({'version','account_id','actor_id','operation_id','action_id','request_sha256',
    'target_sha256','scope_sha256','producer_sha256','native_receipt_sha256','result_sha256','result_length'})
_DIGEST = re.compile(r'[0-9a-f]{64}\Z')
_ID = re.compile(r'[A-Za-z0-9_-]{1,128}\Z')
_DOMAIN = 'openclank.graph-topology.capture.v1'


def _closed_context(value: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != _FIELDS:
        raise ValueError('graph capture context unavailable')
    c = dict(value)
    if (type(c['version']) is not int or c['version'] != 1 or type(c['result_length']) is not int
        or c['result_length'] < 0 or c['result_length'] > 64 * 1024 * 1024
        or any(type(c[k]) is not str or not c[k] or len(c[k].encode('utf-8')) > 256 or any(ord(x)<32 for x in c[k]) for k in ('account_id','actor_id'))
        or any(type(c[k]) is not str or _ID.fullmatch(c[k]) is None for k in ('operation_id','action_id'))
        or any(type(c[k]) is not str or _DIGEST.fullmatch(c[k]) is None for k in ('request_sha256','target_sha256','scope_sha256','producer_sha256','native_receipt_sha256','result_sha256'))):
        raise ValueError('graph capture context unavailable')
    body=[_DOMAIN,c['account_id'],c['actor_id'],c['operation_id'],c['action_id'],c['target_sha256'],c['scope_sha256'],c['producer_sha256'],c['native_receipt_sha256'],c['result_sha256'],c['result_length']]
    if hashlib.sha256(json.dumps(body,ensure_ascii=False,separators=(',',':'),allow_nan=False).encode()).hexdigest()!=c['request_sha256']:
        raise ValueError('graph capture context unavailable')
    return c


class GraphCaptureFilesClient(OdysseusFilesClient):
    """An uncached App-lane child bound to one already-verified Host result."""
    def __init__(self, owner: str, *, app_scope: dict[str, Any], context: Mapping[str, Any], current: Callable[[], bool], binary: str | None = None):
        self._graph_context = _closed_context(context)
        if not callable(current): raise ValueError('graph capture authority unavailable')
        self._graph_current = current
        self._graph_process = None
        self._graph_history = None
        self._finish_exchange_started = False
        super().__init__(owner, binary=binary, app_scope=app_scope, transport='framed')

    def _service_environment(self) -> dict[str, str]:
        if self._graph_current() is not True: raise ValueError('graph capture authority unavailable')
        env = super()._service_environment(); c = self._graph_context
        if (self._graph_current() is not True or env.get('OPEN_CLANK_FILES_LANE') != 'app'
            or env.get('OPENCLANK_HISTORY_ACCOUNT_ID') != c['account_id'] or env.get('OPENCLANK_HISTORY_ACTOR_ID') != c['actor_id']
            or not env.get('OPENCLANK_HISTORY_SOCKET') or not env.get('OPENCLANK_HISTORY_TOKEN')):
            raise ValueError('graph capture authority unavailable')
        env['OPENCLANK_GRAPH_CAPTURE_CONTEXT'] = json.dumps(c,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False)
        return env

    def _request_sync(self, request: dict[str, Any]) -> dict[str, Any]:
        # A capture owns its original child; never silently respawn on a
        # History binding change or a lost process, especially after finish.
        with self._lock:
            self._assert_policy_generation()
            if self._graph_current() is not True: raise FilesServiceError('graph capture authority changed',code='mutation_outcome_unknown')
            if self._graph_process is None:
                self._refresh_history_binding_locked()
                self._start_locked()
                self._graph_process=self._process
                self._graph_history=dict(self._history_environment_snapshot or {})
            if (self._graph_process is None or self._process is not self._graph_process or self._graph_process.poll() is not None
                or self._history_environment()!=self._graph_history or self._graph_current() is not True):
                raise FilesServiceError('graph capture owner changed',code='mutation_outcome_unknown')
            if request.get("operation") == "stage_finish":
                self._finish_exchange_started = True
            result=self._exchange_locked(request)
            if self._graph_current() is not True: raise FilesServiceError('graph capture authority changed',code='mutation_outcome_unknown')
            return result

    async def stage_graph(self, *, target: str, content: bytes, before_finish) -> dict[str, Any]:
        c=self._graph_context
        if (type(target) is not str or not os.path.isabs(target) or os.path.realpath(target)!=target
            or hashlib.sha256(target.encode('utf-8','strict')).hexdigest()!=c['target_sha256']
            or type(content) is not bytes or len(content)!=c['result_length']
            or hashlib.sha256(content).hexdigest()!=c['result_sha256'] or self._graph_current() is not True):
            raise ValueError('graph capture authority unavailable')
        stage_id=None; finish_started=False; self.finish_started=False
        try:
            response=await self.stage_begin(os.path.dirname(target)); data=response.get('data') if isinstance(response,dict) else None
            if not isinstance(data,dict): raise ValueError('graph staging unavailable')
            stage_id=data.get('stage_id'); chunk=data.get('max_chunk_bytes'); limit=data.get('max_total_bytes')
            if (type(stage_id) is not str or not stage_id or len(stage_id)>256 or type(chunk) is not int or not 1<=chunk<=512*1024
                or type(limit) is not int or not 1<=limit<=64*1024*1024 or len(content)>limit): raise ValueError('graph staging unavailable')
            for offset in range(0,len(content),chunk):
                if self._graph_current() is not True: raise ValueError('graph capture authority changed')
                await self.stage_chunk(stage_id,content[offset:offset+chunk],offset=offset)
            if self._graph_current() is not True or not callable(before_finish) or await before_finish() is not True: raise ValueError('graph capture authority changed')
            finish_started=True; self.finish_started=True
            response=await self.stage_finish(stage_id,target,length=len(content),digest='sha256:'+c['result_sha256'],root_operation_id=c['operation_id'])
            history=response.get('history') if isinstance(response,dict) else None
            if (self._graph_current() is not True or not isinstance(history,dict) or history.get('action_id')!=c['action_id']
                or history.get('status')!='complete' or history.get('durable') is not True):
                return {'state':'unknown','action_id':c['action_id']}
            return {'state':'captured','action_id':c['action_id']}
        except BaseException:
            if stage_id and not finish_started:
                try: await self.stage_abort(stage_id)
                except Exception: pass
            raise


def qualify_graph_action(client, *, context: Mapping[str, Any], target: str, history_workspace: str,
                         current: Callable[[], bool]) -> dict[str, Any]:
    """Read the exact retained action and bytes; absence never permits retry."""
    c=_closed_context(context)
    if (not callable(current) or current() is not True or type(target) is not str or not os.path.isabs(target)
        or os.path.realpath(target)!=target or hashlib.sha256(target.encode()).hexdigest()!=c['target_sha256']):
        raise ValueError('graph capture authority unavailable')
    response=client.get_capture_action(c['action_id']); a=response.get('Action') if isinstance(response,dict) else None
    key=a.get('resource_key') if isinstance(a,dict) else None
    before=a.get('before_resources') if isinstance(a,dict) else None; after=a.get('after_resources') if isinstance(a,dict) else None
    live=a.get('live') if isinstance(a,dict) else None
    marker=(a.get('coverage') or {}).get('metadata') if isinstance(a,dict) and isinstance(a.get('coverage'),dict) else None
    fp=f"sha256:{c['result_sha256']}:{c['result_length']}"
    if (not isinstance(a,dict) or a.get('action_id')!=c['action_id'] or a.get('actor_account_id')!=c['account_id']
        or a.get('actor_id')!=c['actor_id'] or a.get('actor_kind')!='human' or a.get('tool_id')!='graph_topology_capture'
        or a.get('operation')!='create' or a.get('state')!='Complete' or not isinstance(marker,dict)
        or marker.get('graph_topology_capture_v1')!=c or marker.get('batch_once')!={'version':1,'binding_sha256':c['request_sha256']}
        or not isinstance(key,dict) or key.get('account_id')!=c['account_id'] or key.get('workspace_id')!=history_workspace or key.get('provider')!='odysseus-files'
        or a.get('modified_resource_ids')!=[key] or not isinstance(before,list) or len(before)!=1 or not isinstance(before[0],dict)
        or before[0].get('resource_key')!=key or before[0].get('existence')!='Absent' or before[0].get('fingerprint')!='missing'
        or (before[0].get('old_locator') or {}).get('location_label')!=target or not isinstance(after,list) or len(after)!=1 or not isinstance(after[0],dict)
        or after[0].get('resource_key')!=key or after[0].get('existence')!='Present' or after[0].get('fingerprint')!=fp
        or (after[0].get('locator') or {}).get('location_label')!=target or not isinstance(after[0].get('after'),dict)
        or after[0]['after'].get('fingerprint')!=fp or type(after[0]['after'].get('version_id')) is not str
        or not after[0]['after']['version_id'] or len(after[0]['after']['version_id'])>512
        or not isinstance(after[0].get('coverage'),dict) or after[0]['coverage'].get('byte_len')!=c['result_length']
        or after[0]['coverage'].get('content_digest')!='sha256:'+c['result_sha256']
        or not isinstance(live,dict) or live.get('action_id')!=c['action_id'] or live.get('status')!='Committed' or live.get('fingerprint')!=fp
        or not isinstance(live.get('committed_resources'),list) or len(live['committed_resources'])!=1
        or live['committed_resources'][0].get('resource_key')!=key or live['committed_resources'][0].get('fingerprint')!=fp):
        raise ValueError('graph captured action unavailable')
    payload=client.read_version(c['action_id'],after[0]['after'],max_bytes=c['result_length'])
    if (type(payload) is not bytes or len(payload)!=c['result_length'] or hashlib.sha256(payload).hexdigest()!=c['result_sha256'] or current() is not True):
        raise ValueError('graph captured version unavailable')
    return {'state':'saved','action_id':c['action_id'],'resource_key':dict(key),'version_id':after[0]['after'].get('version_id'),'bytes':payload}


async def _target(facade, context, *, parent_ref: str, name: str, expected_parent_revision, current):
    from src.openclank.files_host_provider import _path
    if (not callable(current) or current() is not True or type(name) is not str or not name or name!=name.strip()
        or len(name.encode('utf-8'))>240 or name in ('.','..') or '/' in name or '\\' in name or '\x00' in name
):
        raise ValueError('graph target unavailable')
    provider,parent=facade._provider_for_ref(context,parent_ref,capability='write')
    if getattr(parent,'provider',None)!='host':raise ValueError('graph target unavailable')
    path=_path(parent.origin_id); scope=provider._scope(context)
    observed=await provider.stat(context,origin_id=parent.origin_id)
    if (observed.origin_id!=parent.origin_id or 'children' not in observed.capabilities or 'write' not in observed.capabilities
        or not provider._path_writable(context,path,scope)):
        raise ValueError('graph target unavailable')
    facade._check_revision(observed,expected_parent_revision)
    target=os.path.join(path,name)
    if not os.path.isabs(target) or os.path.realpath(target)!=target or current() is not True:
        raise ValueError('graph target unavailable')
    return provider,parent,scope,target


async def graph_stage_capacity(facade, context, *, parent_ref: str, name: str, expected_parent_revision, current) -> dict[str, Any]:
    """Probe real scoped stage limits before Native starts, then abort the stage."""
    provider,parent,scope,target=await _target(facade,context,parent_ref=parent_ref,name=name,expected_parent_revision=expected_parent_revision,current=current)
    client=provider._client(context,scope); stage=None
    try:
        response=await client.stage_begin(os.path.dirname(target)); data=response.get('data') if isinstance(response,dict) else None
        if not isinstance(data,dict):raise ValueError('graph staging unavailable')
        stage=data.get('stage_id'); chunk=data.get('max_chunk_bytes'); limit=data.get('max_total_bytes')
        if (type(stage) is not str or not stage or type(chunk) is not int or not 1<=chunk<=512*1024
            or type(limit) is not int or not 1<=limit<=64*1024*1024):raise ValueError('graph staging unavailable')
        await client.stage_abort(stage);stage=None
        await _target(facade,context,parent_ref=parent_ref,name=name,expected_parent_revision=expected_parent_revision,current=current)
        return {'max_total_bytes':limit,'max_chunk_bytes':chunk,'target_sha256':hashlib.sha256(target.encode('utf-8')).hexdigest()}
    finally:
        if stage:
            try: await client.stage_abort(stage)
            except Exception: pass


async def create_graph_capture(facade, context, *, parent_ref: str, name: str, expected_parent_revision,
                               producer_context: Mapping[str, Any], content: bytes, current,
                               before_dispatch=None, binary: str | None = None) -> dict[str, Any]:
    """Private trusted Host entry: existing Files scope and mutation claim remain authoritative."""
    from src.openclank.files_facade import FilesFacadeError
    from src.openclank.files_host_provider import _origin
    c=_closed_context(producer_context)
    provider,parent,scope,target=await _target(facade,context,parent_ref=parent_ref,name=name,expected_parent_revision=expected_parent_revision,current=current)
    if c['account_id']!=str(context.owner_subject_id) or hashlib.sha256(target.encode('utf-8')).hexdigest()!=c['target_sha256']:
        raise ValueError('graph capture authority unavailable')
    client=GraphCaptureFilesClient(context.owner_username,app_scope=scope,context=c,current=current,binary=binary)
    async def checked():
        p,ref,actual_scope,actual_target=await _target(facade,context,parent_ref=parent_ref,name=name,expected_parent_revision=expected_parent_revision,current=current)
        if p is not provider or ref.origin_id!=parent.origin_id or actual_scope!=scope or actual_target!=target:return False
        if before_dispatch is not None and await before_dispatch() is not True:return False
        return current() is True
    async def work(begin_mutation,known_no_effect):
        try:
            async def finish_guard():
                if not await checked():return False
                begin_mutation();return True
            result=await client.stage_graph(target=target,content=content,before_finish=finish_guard)
            if result.get('state')!='captured':
                raise FilesFacadeError('Graph capture outcome requires reconciliation',code='mutation_outcome_unknown')
            observed=await provider.stat(context,origin_id=_origin(target))
            if observed.origin_id!=_origin(target) or observed.revision!={'kind':'hostFingerprint','value':f"sha256:{c['result_sha256']}:{c['result_length']}"} or current() is not True:
                raise FilesFacadeError('Graph capture outcome requires reconciliation',code='mutation_outcome_unknown')
            return {'state':'captured','action_id':c['action_id'],'resource':facade._resource(context,'host',observed,parent_stable_id=parent.stable_id)}
        except BaseException as error:
            # Closing acquires the same transport lock as the exchange, so the
            # marker is final only after the owned child has been drained.
            await asyncio.to_thread(client.close)
            with client._lock:
                unissued = (not client._finish_exchange_started and
                            (client._graph_process is None or client._graph_process.poll() is not None))
            if unissued:
                known_no_effect()
                raise FilesServiceError('Graph capture was not issued', code='graph_capture_unissued') from error
            raise
        finally:
            # The shielded mutation task owns the child through its drain even
            # if its caller is cancelled; the caller must not close it early.
            await asyncio.to_thread(client.close)
    return await provider._run_claimed_host_mutation(context,[target],work,delayed_start=True,
        recovery={'kind':'graph_topology_capture_v1','operation_id':c['operation_id'],'action_id':c['action_id'],'request_sha256':c['request_sha256']})


async def reconcile_graph_capture(facade, context, *, parent_ref: str, name: str,
                                  original: Mapping[str, Any], history_client, history_workspace: str,
                                  actor_id: str, current) -> dict[str, Any]:
    """Qualify the real retained action and release only its exact existing claim.

    ``original`` comes from the Host operation owner, never from a browser marker.
    A missing or unqualified action remains unavailable and grants no retry.
    """
    from src.openclank.files_host_provider import _path
    if (not isinstance(original,Mapping) or set(original)!={'operation_id','action_id','request_sha256'}
        or any(type(original.get(k)) is not str or _ID.fullmatch(original[k]) is None for k in ('operation_id','action_id'))
        or type(original.get('request_sha256')) is not str or _DIGEST.fullmatch(original['request_sha256']) is None
        or type(actor_id) is not str or not actor_id or not callable(current) or current() is not True
        or type(name) is not str or not name or name!=name.strip() or name in ('.','..') or '/' in name or '\\' in name
        or '\x00' in name or len(name.encode('utf-8'))>240):
        raise ValueError('graph capture status unavailable')
    async def resolve():
        provider,parent=facade._provider_for_ref(context,parent_ref,capability='children')
        if getattr(parent,'provider',None)!='host':raise ValueError('graph capture status unavailable')
        scope=provider._scope(context); observed=await provider.stat(context,origin_id=parent.origin_id)
        if observed.origin_id!=parent.origin_id or 'children' not in observed.capabilities or current() is not True:
            raise ValueError('graph capture status unavailable')
        target=os.path.join(_path(parent.origin_id),name)
        if not os.path.isabs(target) or os.path.realpath(target)!=target:raise ValueError('graph capture status unavailable')
        return provider,parent,scope,target
    provider,parent,scope,target=await resolve()
    response=await asyncio.to_thread(history_client.get_capture_action,original['action_id'])
    action=response.get('Action') if isinstance(response,dict) else None
    metadata=(action.get('coverage') or {}).get('metadata') if isinstance(action,dict) and isinstance(action.get('coverage'),dict) else None
    c=_closed_context(metadata.get('graph_topology_capture_v1') if isinstance(metadata,dict) else {})
    if (c['account_id']!=str(context.owner_subject_id) or c['actor_id']!=actor_id
        or any(c[k]!=original[k] for k in ('operation_id','action_id','request_sha256'))):
        raise ValueError('graph capture status unavailable')
    result=await asyncio.to_thread(qualify_graph_action,history_client,context=c,target=target,history_workspace=history_workspace,current=current)
    p,ref,now_scope,now_target=await resolve()
    if p is not provider or ref.origin_id!=parent.origin_id or now_scope!=scope or now_target!=target or current() is not True:
        raise ValueError('graph capture status unavailable')
    recovery={'kind':'graph_topology_capture_v1','operation_id':c['operation_id'],'action_id':c['action_id'],'request_sha256':c['request_sha256']}
    offset=0;matches=[]
    while offset<2048:
        batch=provider.operation_store.list_operations(owner_subject_id=context.owner_subject_id,
            operation_prefix='__host_path_claim__',phase='host_path_claim',offset=offset,limit=256)
        for item in batch:
            claim=(item.get('receipt') or {}).get('_host_writer_claim')
            if (isinstance(claim,Mapping) and claim.get('recovery')==recovery and claim.get('paths')==[target]
                and claim.get('workspace_id')==str(context.workspace_id) and claim.get('token')==item.get('digest')):
                matches.append((item['operation_id'],claim['token']))
        if len(batch)<256:break
        offset+=256
    else:raise ValueError('graph claim inventory unavailable')
    if len(matches)>1 or current() is not True:raise ValueError('graph claim unavailable')
    if matches:
        provider.operation_store.release_host_mutation(owner_subject_id=str(context.owner_subject_id),operation_id=matches[0][0],token=matches[0][1])
    result['claim_state']='released' if matches else 'absent'
    return result
