"""Explicit media coordinator on the existing bg_jobs authority.

Private dependency-injected source only: no provider registration, scheduler,
public endpoint or separate persistence. Files publication remains injected.
"""
from __future__ import annotations
import asyncio
import hashlib
import json
import math
import os
import sqlite3
from pathlib import Path
from dataclasses import asdict
from typing import Any, Mapping

from src import bg_jobs
from src.openclank.media_adapters.base import Artifact, Submission, NotDispatched, artifact_from_record, collection_profile, media_operation_facts

MAX_REQUEST_BYTES = 65536

class MediaUnavailable(RuntimeError):
    pass


class _DispatchWindow:
    def __init__(self, current): self.active=True; self.current=current
    def check(self):
        if not self.active or not self.current(): raise MediaUnavailable('media dispatch admission expired')
    def close(self): self.active=False


def request_digest(request: Mapping[str, Any]) -> str:
    visited=0; text_bytes=0
    def check(value,depth):
        nonlocal visited,text_bytes
        visited+=1
        if visited>2048 or depth>8: raise MediaUnavailable('media request exceeds structure limits')
        if value is None or type(value) is bool: return
        if type(value) is int:
            if abs(value)>2**53-1: raise MediaUnavailable('media integer exceeds bounds')
            return
        if type(value) is float:
            if not math.isfinite(value): raise MediaUnavailable('invalid media number')
            return
        if isinstance(value,str):
            if len(value)>MAX_REQUEST_BYTES: raise MediaUnavailable('media string exceeds bounds')
            try: text_bytes+=len(value.encode('utf-8'))
            except UnicodeError as exc: raise MediaUnavailable('invalid media text') from exc
            if text_bytes>MAX_REQUEST_BYTES: raise MediaUnavailable('media text exceeds bounds')
            return
        if isinstance(value,list):
            for item in value: check(item,depth+1)
            return
        if isinstance(value,dict):
            for key,item in value.items():
                if not isinstance(key,str) or len(key)>256: raise MediaUnavailable('invalid media key')
                check(key,depth+1); check(item,depth+1)
            return
        raise MediaUnavailable('unsupported media value')
    check(request,0)
    try:
        raw=json.dumps(request,sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False).encode('utf-8')
    except (ValueError,TypeError,UnicodeError,RecursionError) as exc:
        raise MediaUnavailable('invalid media request') from exc
    if len(raw)>MAX_REQUEST_BYTES: raise MediaUnavailable('media request exceeds bounds')
    return hashlib.sha256(raw).hexdigest()


class MediaCoordinator:
    def __init__(self, *, barrier, account_for_owner, owner_for_account, scope_current, adapters, publisher=None, timeout=30.0, control_current=None, foreground_runtime=None, foreground_profile=None, foreground_provider=None):
        if not 0.01 <= timeout <= 120: raise ValueError('invalid media deadline')
        self.barrier=barrier; self.account_for_owner=account_for_owner; self.owner_for_account=owner_for_account; self.scope_current=scope_current
        self.adapters=dict(adapters); self.publisher=publisher; self.timeout=timeout; self.control_current=control_current or scope_current
        self.foreground_runtime=foreground_runtime;self.foreground_profile=foreground_profile;self.foreground_provider=foreground_provider

    @staticmethod
    async def _settled_thread(func, *args, **kwargs):
        task=asyncio.create_task(asyncio.to_thread(func,*args,**kwargs));cancelled=False
        while not task.done():
            try:await asyncio.shield(task)
            except asyncio.CancelledError:cancelled=True
        result=task.result()
        if cancelled:raise asyncio.CancelledError
        return result

    @staticmethod
    def _shadow_state(provider, operation, owner, connection, account_id):
        """Observe this producer's exact optional local shadow, never infer success."""
        if provider is None:return ('no_store',hashlib.sha256(b'no-provider').hexdigest())
        resolver=getattr(provider,'_local_db_path',None);path=resolver() if callable(resolver) else None
        if not path or getattr(provider,'_broker_url',None):return ('unknown','unavailable')
        path=Path(path).resolve(strict=True);info=path.stat()
        identity=hashlib.sha256(json.dumps([str(path),info.st_dev,info.st_ino],separators=(',',':')).encode()).hexdigest()
        with sqlite3.connect(path.as_uri()+'?mode=ro',uri=True,timeout=0) as db:
            if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='fm_v2_s05_schema'").fetchone() is None:return ('no_store',identity)
            occupancy=db.execute('SELECT verified FROM fm_v2_s05_occupancy WHERE singleton=1').fetchone()
            if not occupancy or occupancy[0]!=0:return ('unknown',identity)
            version=db.execute('SELECT version FROM fm_v2_s05_schema').fetchone()
            if not version or version[0] not in (1,2,3,4):return ('unknown',identity)
            row=db.execute('SELECT owner_id,connection_id,coverage_generation,state' + (',account_id' if version[0]==4 else '') + ' FROM fm_v2_s05_foreground WHERE operation_id=?',(operation,)).fetchone()
            if row is not None and version[0]==4 and row[4]!=account_id:return ('unknown',identity)
            if row is None:return ('unobserved',identity)
            if tuple(row[:3])!=(owner,connection,'media_shadow_v1') or row[3] not in ('active','settled'):return ('unknown',identity)
            return (row[3],identity)

    async def _foreground_start(self, identity, claim):
        from src.memory_scope import memory_owner
        from src.openclank.memory_job_admission import observe_shadow, _conn
        operation=bg_jobs.media_foreground_operation(claim['record'],claim['token'])
        owner=memory_owner(self._account(identity));connection=identity['route_id']
        if claim['record'].get('owner') != owner:raise MediaUnavailable('media owner changed')
        runtime=self.foreground_runtime() if callable(self.foreground_runtime) else None
        provider=self.foreground_provider() if callable(self.foreground_provider) else None
        profile=self.foreground_profile() if callable(self.foreground_profile) else None
        if runtime is None:
            if profile is not None and provider is None:raise MediaUnavailable('media foreground provider unavailable')
            if provider is not None:await self._settled_thread(observe_shadow,provider,operation_id=operation,owner=owner,connection_id=connection,generation='media_shadow_v1',event='start',account_id=identity['account_id'])
            if profile is not None:
                def require_unverified():
                    profile.assert_current()
                    with _conn(profile.path) as db:
                        row=db.execute('SELECT verified FROM fm_v2_s05_occupancy WHERE singleton=1').fetchone()
                        if not row or row[0]!=0:raise MediaUnavailable('media foreground coverage unavailable')
                await self._settled_thread(require_unverified)
            try:observed,observer=await self._settled_thread(self._shadow_state,provider,operation,owner,connection,identity['account_id'])
            except (OSError,sqlite3.Error):observed,observer='unknown','unavailable'
            generation={'active':'media_shadow_v1','no_store':'media_no_store_v1','unobserved':'media_unobserved_v1'}.get(observed,'media_unknown_v1')
            binding={'version':1,'operation_id':operation,'account_id':identity['account_id'],'memory_owner':owner,'connection_id':connection,
                     'coverage_generation':generation,'clock_generation':generation,'observer_identity':observer}
            await self._settled_thread(bg_jobs.bind_media_foreground,claim['record']['id'],account_id=identity['account_id'],token=claim['token'],binding=binding)
            return None
        if profile is not runtime.profile or (callable(self.foreground_provider) and provider is not runtime.provider):
            raise MediaUnavailable('media foreground authority changed')
        handle=await self._settled_thread(runtime.enter,owner=owner,account_id=identity['account_id'],connection_id=connection,operation_id=operation)
        _,observer=await self._settled_thread(self._shadow_state,runtime.provider,operation,owner,connection,identity['account_id'])
        binding={'version':1,'operation_id':operation,'account_id':identity['account_id'],'memory_owner':owner,'connection_id':connection,
                 'coverage_generation':runtime.coverage_generation,'clock_generation':runtime.clock_generation,'observer_identity':observer}
        await self._settled_thread(bg_jobs.bind_media_foreground,claim['record']['id'],account_id=identity['account_id'],token=claim['token'],binding=binding)
        return handle

    async def _foreground_terminal(self, identity, job_id, token, fact):
        current=await self._settled_thread(bg_jobs.get_media,job_id,account_id=identity['account_id'])
        binding=current.get('model_foreground') if current else None
        if binding is None:
            # Unqualified/shadow jobs have no authority to settle a verified row.
            return
        saved=await self._settled_thread(bg_jobs.record_media_model_terminal,job_id,account_id=identity['account_id'],token=token,fact=fact)
        await self._settle_foreground(identity,job_id,saved)

    async def _foreground_presend_unsent(self, identity, claim):
        """Record known unsent before resolving this attempt's FM accounting."""
        from src.memory_scope import memory_owner
        operation=bg_jobs.media_foreground_operation(claim['record'],claim['token']);owner=memory_owner(self._account(identity));connection=identity['route_id']
        if claim['record'].get('owner') != owner:raise MediaUnavailable('media owner changed')
        await self._settled_thread(bg_jobs.record_media_presend_unsent,claim['record']['id'],account_id=identity['account_id'],token=claim['token'],operation_id=operation)
        row=await self._settled_thread(bg_jobs.get_media,claim['record']['id'],account_id=identity['account_id'])
        binding=row.get('model_foreground') if row else None
        runtime=self.foreground_runtime() if callable(self.foreground_runtime) else None
        profile=self.foreground_profile() if callable(self.foreground_profile) else None
        provider=self.foreground_provider() if callable(self.foreground_provider) else None
        if binding is None and runtime is not None:
            if profile is not runtime.profile or (callable(self.foreground_provider) and provider is not runtime.provider):raise MediaUnavailable('media foreground authority changed')
            state=await self._settled_thread(runtime.inspect_media,owner=owner,account_id=identity['account_id'],connection_id=connection,operation_id=operation)
            if state=='absent':
                await self._settled_thread(bg_jobs.record_media_presend_unsent,claim['record']['id'],account_id=identity['account_id'],token=claim['token'],operation_id=operation,absent_coverage=(runtime.coverage_generation,runtime.clock_generation))
                return
            _,observer=await self._settled_thread(self._shadow_state,runtime.provider,operation,owner,connection,identity['account_id'])
            binding={'version':1,'operation_id':operation,'account_id':identity['account_id'],'memory_owner':owner,'connection_id':connection,'coverage_generation':runtime.coverage_generation,'clock_generation':runtime.clock_generation,'observer_identity':observer}
            await self._settled_thread(bg_jobs.bind_media_foreground,claim['record']['id'],account_id=identity['account_id'],token=claim['token'],binding=binding)
        elif binding is None and runtime is None and profile is None:
            state,observer=await self._settled_thread(self._shadow_state,provider,operation,owner,connection,identity['account_id'])
            generation={'active':'media_shadow_v1','no_store':'media_no_store_v1','unobserved':'media_unobserved_v1'}.get(state)
            if generation is not None:
                binding={'version':1,'operation_id':operation,'account_id':identity['account_id'],'memory_owner':owner,'connection_id':connection,'coverage_generation':generation,'clock_generation':generation,'observer_identity':observer}
                await self._settled_thread(bg_jobs.bind_media_foreground,claim['record']['id'],account_id=identity['account_id'],token=claim['token'],binding=binding)
        if binding is None:raise MediaUnavailable('media pre-send accounting unavailable')
        await self._foreground_terminal(identity,claim['record']['id'],claim['token'],'not_dispatched')

    async def _settle_foreground(self, identity, job_id, binding):
        from src.openclank.memory_job_admission import observe_shadow
        generation=binding['coverage_generation'];provider=self.foreground_provider() if callable(self.foreground_provider) else None
        if generation in {'media_shadow_v1','media_no_store_v1','media_unobserved_v1','media_unknown_v1'}:
            if generation=='media_shadow_v1' and provider is not None:
                await self._settled_thread(observe_shadow,provider,operation_id=binding['operation_id'],owner=binding['memory_owner'],connection_id=binding['connection_id'],generation='media_shadow_v1',event='finished',account_id=binding['account_id'])
            try:state,observer=await self._settled_thread(self._shadow_state,provider,binding['operation_id'],binding['memory_owner'],binding['connection_id'],binding['account_id'])
            except (OSError,sqlite3.Error):raise MediaUnavailable('media shadow settlement unavailable')
            expected={'media_shadow_v1':('settled','shadow_settled'),'media_no_store_v1':('no_store','no_store'),'media_unobserved_v1':('unobserved','shadow_unobserved')}.get(generation)
            if not expected or state!=expected[0] or observer!=binding['observer_identity']:raise MediaUnavailable('media shadow settlement unavailable')
            mode=expected[1]
        else:
            runtime=self.foreground_runtime() if callable(self.foreground_runtime) else None
            if (runtime is None or runtime.coverage_generation!=generation or runtime.clock_generation!=binding['clock_generation']
                or (callable(self.foreground_profile) and self.foreground_profile() is not runtime.profile)
                or (callable(self.foreground_provider) and provider is not runtime.provider)):
                raise MediaUnavailable('media foreground generation unavailable')
            _,observer=await self._settled_thread(self._shadow_state,runtime.provider,binding['operation_id'],binding['memory_owner'],binding['connection_id'],binding['account_id'])
            if observer!=binding['observer_identity']:raise MediaUnavailable('media foreground store changed')
            await self._settled_thread(runtime.leave,(runtime.profile.path,binding['memory_owner'],binding['account_id'],binding['connection_id'],binding['operation_id']));mode='fm_settled'
        await self._settled_thread(bg_jobs.acknowledge_media_model_settlement,job_id,account_id=identity['account_id'],binding=binding,mode=mode)

    def _account(self, identity):
        owner=self.owner_for_account(identity['account_id'])
        if not owner or self.account_for_owner(owner) != identity['account_id']:
            raise MediaUnavailable('account authority changed')
        return owner

    def _adapter(self, identity):
        adapter=self.adapters.get(identity['adapter_id'])
        if (adapter is None or adapter.adapter_id!=identity['adapter_id'] or adapter.version!=identity['adapter_version'] or adapter.route_kind!=identity['route_kind']):
            raise MediaUnavailable('configured media adapter is unavailable')
        facts=media_operation_facts(identity['adapter_id'],identity['adapter_version'])
        if facts is not None:
            if facts.operation!=identity['operation']:
                raise MediaUnavailable('configured media operation is unavailable')
            try: collection_profile(adapter)
            except ValueError as exc: raise MediaUnavailable('configured media custody is unavailable') from exc
        return adapter

    def _window(self, identity):
        owner=self._account(identity)
        def current():
            try:
                absolute=getattr(self.barrier,'_absolute_fence',None)
                return self._account(identity)==owner and not self.barrier._fenced(owner) and not (absolute and absolute(owner))
            except Exception:
                return False
        return _DispatchWindow(current)

    async def _owned(self, identity, operation):
        owner=self._account(identity)
        async with self.barrier.admitted(owner):
            if self._account(identity)!=owner: raise MediaUnavailable('account authority changed')
            return operation()

    async def _admitted(self, identity, operation):
        owner=self._account(identity)
        async with self.barrier.admitted(owner):
            if self._account(identity)!=owner: raise MediaUnavailable('account authority changed')
            return await operation()

    async def _scope(self, identity, *, control=False):
        task=asyncio.create_task((self.control_current if control else self.scope_current)(identity))
        try:
            done,_=await asyncio.wait({task},timeout=self.timeout)
        except asyncio.CancelledError:
            task.cancel(); task.add_done_callback(lambda done:None if done.cancelled() else done.exception()); raise
        if not done:
            task.cancel(); task.add_done_callback(lambda done:None if done.cancelled() else done.exception()); return False
        return bool(task.result())

    async def _read(self, identity, job_id):
        def read():
            job=bg_jobs.get_media(job_id,account_id=identity['account_id'])
            if job and job.get('owner') != self._account(identity): raise MediaUnavailable('media owner changed')
            return job
        return await self._owned(identity,read)

    async def _late_owned(self, identity, job_id, operation):
        # Factual receipt retention has no external effect and never creates
        # a row. The operation itself compares immutable account+exact token
        # under the same lock used by account rename/purge.
        return operation()

    async def prepare(self, identity: Mapping[str,Any], request: Mapping[str,Any]):
        return await self._admitted(identity,lambda:self._prepare(identity,request))

    async def _prepare(self, identity: Mapping[str,Any], request: Mapping[str,Any]):
        identity=dict(identity)
        if self._account(identity) != identity['owner']: raise MediaUnavailable('media owner changed')
        if request_digest(request)!=identity.get('request_digest'): raise MediaUnavailable('media request changed')
        self._adapter(identity)
        if not await self._scope(identity): raise MediaUnavailable('media scope changed')
        return await self._owned(identity,lambda:bg_jobs.reserve_media(identity))

    async def prepare_once(self, identity: Mapping[str,Any], request: Mapping[str,Any]):
        """Reserve under the normal owner and report whether this call created it."""
        async def prepare():
            value=dict(identity)
            if self._account(value)!=value['owner'] or request_digest(request)!=value.get('request_digest'):
                raise MediaUnavailable('media request changed')
            self._adapter(value)
            if not await self._scope(value): raise MediaUnavailable('media scope changed')
            return await self._owned(value,lambda:bg_jobs.reserve_media(value,with_created=True))
        return await self._admitted(identity,prepare)

    async def _claim(self, identity, job_id, action, *, allow_detached=False, expected_revision=None):
        def claim():
            job=bg_jobs.get_media(job_id,account_id=identity['account_id'])
            if not job or job.get('owner')!=self._account(identity) or job['identity']!=identity or (job['chat_detached'] and not (allow_detached and action in {'status','cancel'})): raise MediaUnavailable('media job unavailable')
            if expected_revision is not None and job['revision'] != expected_revision: raise MediaUnavailable('media job revision changed')
            return bg_jobs.claim_media(job_id,account_id=identity['account_id'],revision=job['revision'],action=action)
        return await self._owned(identity,claim)

    async def _finish(self, identity, job_id, token, status, **kwargs):
        return bg_jobs.finish_media(job_id,account_id=identity['account_id'],token=token,status=status,**kwargs)

    async def _late_submit(self, identity, job_id, token, task):
        try:
            answer=task.result()
            allowed={'queued','running'} if identity['adapter_id'] in bg_jobs._S05_MEDIA_MODELS else {'queued','running','collecting'}
            if isinstance(answer,Submission) and answer.task_id and answer.state in allowed:
                if identity['route_kind']=='remote':
                    await self._late_owned(identity,job_id,lambda:bg_jobs.record_media_submission(job_id,account_id=identity['account_id'],token=token,remote_task_id=answer.task_id))
                else:
                    await self._late_owned(identity,job_id,lambda:bg_jobs.record_media_local_start(job_id,account_id=identity['account_id'],token=token,local_process_job_id=answer.task_id))
        except BaseException:
            pass

    async def _late_result(self, identity, job_id, token, task, kind):
        try:
            value=task.result()
            if kind=='artifact' and isinstance(value,Artifact):
                await self._late_owned(identity,job_id,lambda:bg_jobs.record_media_artifact(job_id,account_id=identity['account_id'],token=token,artifact=asdict(value)))
            elif kind=='cancel' and value=='confirmed':
                owner=self._account(identity)
                async with self.barrier.admitted(owner):
                    if identity['adapter_id'] in bg_jobs._S05_MEDIA_MODELS:await self._foreground_terminal(identity,job_id,token,'cancelled')
                await self._late_owned(identity,job_id,lambda:bg_jobs.record_media_cancel(job_id,account_id=identity['account_id'],token=token))
            elif kind=='publication' and isinstance(value,dict):
                await self._late_owned(identity,job_id,lambda:bg_jobs.record_media_publication(job_id,account_id=identity['account_id'],token=token,result=value))
        except BaseException:
            pass

    async def _bounded(self, awaitable, identity, job_id, token, kind=None, window=None, *, cancel_on_timeout=True):
        task=asyncio.ensure_future(awaitable)
        def retire():
            if cancel_on_timeout: task.cancel()
            if kind:
                task.add_done_callback(lambda done:asyncio.create_task(self._late_result(identity,job_id,token,done,kind)))
            else:
                task.add_done_callback(lambda done:None if done.cancelled() else done.exception())
        try:
            done,_=await asyncio.wait({task},timeout=self.timeout)
        except asyncio.CancelledError:
            if window: window.close()
            retire(); raise
        if not done:
            if window: window.close()
            retire(); raise TimeoutError('media adapter deadline exceeded')
        if window: window.close()
        return task.result()

    async def submit(self, identity: Mapping[str,Any], job_id: str, request: Mapping[str,Any], *, expected_revision=None):
        return await self._admitted(identity,lambda:self._submit(identity,job_id,request,expected_revision=expected_revision))

    async def _submit(self, identity: Mapping[str,Any], job_id: str, request: Mapping[str,Any], *, expected_revision=None):
        identity=dict(identity); adapter=self._adapter(identity)
        if request_digest(request)!=identity['request_digest'] or not await self._scope(identity): raise MediaUnavailable('media source changed')
        claim=await self._claim(identity,job_id,'submit',expected_revision=expected_revision); token=claim['token']
        if identity['adapter_id'] in bg_jobs._S05_MEDIA_MODELS:
            try:await self._foreground_start(identity,claim)
            except BaseException:
                preserved=False;accounted=False
                try:
                    await self._foreground_presend_unsent(identity,claim);preserved=True;accounted=True
                except BaseException:
                    try:
                        row=await self._settled_thread(bg_jobs.get_media,job_id,account_id=identity['account_id'])
                        preserved=bool(row and (row.get('model_presend') or row.get('model_terminal')))
                    except BaseException:pass
                if not accounted:
                    profile=self.foreground_profile() if callable(self.foreground_profile) else None
                    invalidate=getattr(profile,'invalidate',None)
                    if callable(invalidate):invalidate()
                await asyncio.shield(self._finish(identity,job_id,token,'failed' if preserved else 'unknown_outcome',**({'reason':'not_dispatched'} if preserved else {})))
                raise
        elif identity['adapter_id'] not in bg_jobs._S05_MEDIA_CPU:
            await self._finish(identity,job_id,token,'unknown_outcome');raise MediaUnavailable('media model coverage unavailable')
        window=self._window(identity); task=asyncio.create_task(adapter.submit(dict(request),claim['record'],window.check))
        try:
            done,_=await asyncio.wait({task},timeout=self.timeout)
        except asyncio.CancelledError:
            window.close()
            task.cancel(); task.add_done_callback(lambda completed:asyncio.create_task(self._late_submit(identity,job_id,token,completed)))
            await asyncio.shield(self._finish(identity,job_id,token,'unknown_outcome'))
            raise
        if not done:
            window.close()
            task.cancel(); task.add_done_callback(lambda completed:asyncio.create_task(self._late_submit(identity,job_id,token,completed)))
            await self._finish(identity,job_id,token,'unknown_outcome')
            return await self._read(identity,job_id)
        window.close()
        try:
            answer=task.result()
            allowed={'queued','running'} if identity['adapter_id'] in bg_jobs._S05_MEDIA_MODELS else {'queued','running','collecting','failed'}
            if not isinstance(answer,Submission) or answer.state not in allowed: raise ValueError()
            kwargs={'remote_task_id':answer.task_id} if identity['route_kind']=='remote' else {'local_process_job_id':answer.task_id}
            if not await self._finish(identity,job_id,token,answer.state,**kwargs):
                await self._late_submit(identity,job_id,token,task)
        except NotDispatched:
            if identity['adapter_id'] in bg_jobs._S05_MEDIA_MODELS: await self._foreground_terminal(identity,job_id,token,'not_dispatched')
            await self._finish(identity,job_id,token,'failed',reason='not_dispatched')
        except (Exception,asyncio.CancelledError):
            await self._finish(identity,job_id,token,'unknown_outcome')
        return await self._read(identity,job_id)

    async def status(self, identity: Mapping[str,Any], job_id: str, *, reconcile_detached=False):
        return await self._admitted(identity,lambda:self._status(identity,job_id,reconcile_detached=reconcile_detached))

    async def _status(self, identity: Mapping[str,Any], job_id: str, *, reconcile_detached=False):
        identity=dict(identity)
        job=await self._read(identity,job_id)
        if not job or job['identity']!=identity: raise MediaUnavailable('media job unavailable')
        if identity['adapter_id'] in bg_jobs._S05_MEDIA_MODELS and job.get('model_terminal') and not job.get('model_settlement'):
            await self._settle_foreground(identity,job_id,job['model_foreground']);return await self._read(identity,job_id)
        if job['chat_detached'] and not reconcile_detached: raise MediaUnavailable('detached media requires explicit reconciliation')
        if job['status'] in {'complete','failed','cancelled'}: return job
        adapter=self._adapter(identity)
        if not await self._scope(identity, control=True): raise MediaUnavailable('media control authority changed')
        if identity.get('adapter_id') in ('latex-pdflatex-v1','trim-webm-v1') and job['status'] == 'collecting': return job
        if job.get('unknown_action') == 'publish': return {**job,'reconciliation_required':'files_receipt'}
        if job['status']=='unknown_outcome' and not job.get('remote_task_id') and not adapter.supports_lookup: return job
        claim=await self._claim(identity,job_id,'status',allow_detached=reconcile_detached)
        try:
            window=self._window(identity)
            answer=await self._bounded(adapter.status(claim['record'],window.check),identity,job_id,claim['token'],window=window)
            if not isinstance(answer,Submission) or answer.state not in {'queued','running','collecting','failed','cancelled','unknown_outcome'}: raise ValueError()
            if identity['adapter_id'] in bg_jobs._S05_MEDIA_MODELS and answer.state in {'collecting','failed','cancelled'}:
                await self._foreground_terminal(identity,job_id,claim['token'],{'collecting':'completed','failed':'failed','cancelled':'cancelled'}[answer.state])
            kwargs = ({'remote_task_id':answer.task_id} if identity['route_kind']=='remote' else {'local_process_job_id':answer.task_id}) if answer.task_id else {}
            await self._finish(identity,job_id,claim['token'],answer.state,**kwargs)
        except asyncio.CancelledError:
            await asyncio.shield(self._finish(identity,job_id,claim['token'],'unknown_outcome'))
            raise
        except Exception:
            await self._finish(identity,job_id,claim['token'],'unknown_outcome')
        return await self._read(identity,job_id)

    async def reconcile(self, identity: Mapping[str,Any], job_id: str, *, reconcile_detached=False):
        return await self._admitted(identity,lambda:self._reconcile(identity,job_id,reconcile_detached=reconcile_detached))

    async def _reconcile(self, identity: Mapping[str,Any], job_id: str, *, reconcile_detached=False):
        """Mark an existing in-flight claim unknown, then use only provider lookup."""
        identity=dict(identity)
        if not await self._scope(identity, control=True): raise MediaUnavailable('media control authority changed')
        job=await self._read(identity,job_id)
        if not job or job['identity']!=identity or (job['chat_detached'] and not reconcile_detached): raise MediaUnavailable('media job unavailable')
        facts=media_operation_facts(identity.get('adapter_id'),identity.get('adapter_version'))
        claim=job.get('claim') or {}
        legacy_owned=identity.get('adapter_id') in ('meshy-preview-v1','meshy-refine-v1','tripo-text3d-v1','fal-flux-schnell-v1','fal-hunyuan-image3d-v1','comfyui-flac-v1')
        if claim.get('action')=='collect' and (claim.get('owned_lifetime') in bg_jobs.OWNED_LIFETIMES or (facts.owned_fetch if facts is not None else legacy_owned)):
            # Only the exact owning task can settle this claim. A different
            # coordinator or restarted process cannot infer that it stopped.
            return {**job,'status':'unknown_outcome'}
        if job.get('claim'):
            await self._owned(identity,lambda:bg_jobs.mark_media_uncertain(job_id,account_id=identity['account_id'],revision=job['revision']))
        job=await self._read(identity,job_id)
        if not job or job['status']!='unknown_outcome': return job
        return await self.status(identity,job_id,reconcile_detached=reconcile_detached)

    async def cancel(self, identity: Mapping[str,Any], job_id: str):
        return await self._admitted(identity,lambda:self._cancel(identity,job_id))

    async def _cancel(self, identity: Mapping[str,Any], job_id: str):
        identity=dict(identity); adapter=self._adapter(identity)
        if not await self._scope(identity, control=True): raise MediaUnavailable('media control authority changed')
        job=await self._read(identity,job_id)
        if not job or job['identity']!=identity: raise MediaUnavailable('media job unavailable')
        if job['status'] in {'complete','failed','cancelled'}: return job
        if job['status']=='prepared':
            claim=await self._claim(identity,job_id,'cancel',allow_detached=True); await self._finish(identity,job_id,claim['token'],'cancelled')
        elif not adapter.supports_cancel: return {**job,'cancel_outcome':'unsupported'}
        else:
            claim=await self._claim(identity,job_id,'cancel',allow_detached=True)
            try:
                window=self._window(identity)
                result=await self._bounded(adapter.cancel(claim['record'],window.check),identity,job_id,claim['token'],'cancel',window)
                if result=='confirmed' and identity['adapter_id'] in bg_jobs._S05_MEDIA_MODELS:await self._foreground_terminal(identity,job_id,claim['token'],'cancelled')
                saved=await self._finish(identity,job_id,claim['token'],'cancelled' if result=='confirmed' else 'cancel_requested')
                if not saved and result=='confirmed':
                    await self._late_owned(identity,job_id,lambda:bg_jobs.record_media_cancel(job_id,account_id=identity['account_id'],token=claim['token']))
            except asyncio.CancelledError:
                await asyncio.shield(self._finish(identity,job_id,claim['token'],'unknown_outcome'))
                raise
            except Exception:
                await self._finish(identity,job_id,claim['token'],'unknown_outcome')
        return await self._read(identity,job_id)

    async def collect(self, identity: Mapping[str,Any], job_id: str):
        return await self._admitted(identity,lambda:self._collect(identity,job_id))

    async def _collect(self, identity: Mapping[str,Any], job_id: str):
        identity=dict(identity); adapter=self._adapter(identity)
        # Sprixen collection is a known-ID provider read. Its exact claim and
        # current selected account are validated at the adapter send boundary;
        # original destination remains required for automatic publication.
        profile = collection_profile(adapter)
        control_read = profile.control_read
        if not await self._scope(identity,control=control_read): raise MediaUnavailable('media scope changed')
        claim=await self._claim(identity,job_id,'collect')
        owned_collect=None
        try:
            existing=claim['record'].get('artifact')
            window=self._window(identity)
            if not existing and profile.owned_fetch:
                # This task owns the original claim and account admission until
                # the actual fetch thread and any custody operation settle.
                async def collect_owned():
                    try:
                        async with self.barrier.admitted(self._account(identity)):
                            value=await adapter.collect(claim['record'],window.check)
                            if not isinstance(value,Artifact): raise ValueError()
                            if not await self._finish(identity,job_id,claim['token'],'collecting',artifact=asdict(value)):
                                await self._late_owned(identity,job_id,lambda:bg_jobs.record_media_artifact(job_id,account_id=identity['account_id'],token=claim['token'],artifact=asdict(value)))
                            return value
                    except BaseException:
                        await asyncio.shield(self._finish(identity,job_id,claim['token'],'unknown_outcome'))
                        raise
                owned_collect=asyncio.create_task(collect_owned())
                owned_collect.add_done_callback(lambda done:None if done.cancelled() else done.exception())
                done,_=await asyncio.wait({owned_collect},timeout=self.timeout)
                if not done:
                    window.close()
                    row=await self._read(identity,job_id)
                    return {**row,'status':'unknown_outcome'}
                artifact=owned_collect.result()
                window.close()
            else:
                artifact=artifact_from_record(existing,profile) if existing else await self._bounded(
                    adapter.collect(claim['record'],window.check),identity,job_id,claim['token'],'artifact',window)
            if not isinstance(artifact,Artifact): raise ValueError()
            if owned_collect is None and not await self._finish(identity,job_id,claim['token'],'collecting',artifact=asdict(artifact)):
                await self._late_owned(identity,job_id,lambda:bg_jobs.record_media_artifact(job_id,account_id=identity['account_id'],token=claim['token'],artifact=asdict(artifact)))
                return await self._read(identity,job_id)
        except asyncio.CancelledError:
            if owned_collect is not None:
                window.close()
            else:
                await asyncio.shield(self._finish(identity,job_id,claim['token'],'unknown_outcome'))
            raise
        except Exception:
            if owned_collect is None:
                await self._finish(identity,job_id,claim['token'],'unknown_outcome')
            return await self._read(identity,job_id)
        if self.publisher is None or not await self._scope(identity):
            return await self._read(identity,job_id)
        claim=await self._claim(identity,job_id,'publish')
        try:
            async def current():
                window.check()
                job=await self._read(identity,job_id)
                valid=bool(job and not job['publication_withdrawn'] and (job.get('claim') or {}).get('token')==claim['token'] and await self._scope(identity))
                window.check()
                return valid
            # The existing Files publisher must invoke this guard at its commit boundary.
            window=self._window(identity)
            async def owned_publication():
                async with self.barrier.admitted(self._account(identity)):
                    return await self.publisher.publish(identity,claim['record'],current)
            result=await self._bounded(owned_publication(),identity,job_id,claim['token'],'publication',window,cancel_on_timeout=False)
            if not isinstance(result,dict) or result.get('sha256')!=artifact.sha256 or result.get('mime_type')!=artifact.mime_type: raise ValueError()
            if not await self._finish(identity,job_id,claim['token'],'complete',result=result):
                await self._late_owned(identity,job_id,lambda:bg_jobs.record_media_publication(job_id,account_id=identity['account_id'],token=claim['token'],result=result))
        except asyncio.CancelledError:
            await asyncio.shield(self._finish(identity,job_id,claim['token'],'unknown_outcome'))
            raise
        except Exception:
            await self._finish(identity,job_id,claim['token'],'unknown_outcome')
        return await self._read(identity,job_id)
