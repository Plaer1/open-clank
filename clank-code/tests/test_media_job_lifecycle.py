"""One isolated lifecycle using only fake adapters and temporary job authority."""
import asyncio
import json
import tempfile
import time
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from src import bg_jobs
from src.openclank.media_jobs import MediaCoordinator, request_digest
from src.openclank.media_adapters.base import Submission, Artifact

class Barrier:
    def __init__(self): self.fenced=False; self.active=0; self._absolute_fence=None
    def _fenced(self,_): return self.fenced
    @asynccontextmanager
    async def admitted(self,owner):
        if self.fenced: raise RuntimeError('account fenced')
        self.active+=1
        try: yield
        finally: self.active-=1

class Adapter:
    adapter_id='fake'; version='1'; route_kind='remote'; supports_lookup=False; supports_cancel=False
    def __init__(self,scope,barrier): self.calls=0; self.lost=False; self.scope=scope; self.barrier=barrier; self.collects=0; self.invalidate=False; self.gate=None; self.entered=asyncio.Event()
    async def submit(self,request,job,admission):
        if self.gate is not None:
            self.entered.set(); await self.gate.wait()
        admission()
        assert self.barrier.active>0
        assert job['idempotency_key'] == 'media:'+job['id']
        self.calls+=1
        if self.lost:
            try: await asyncio.sleep(1)
            except asyncio.CancelledError: await asyncio.sleep(.03); return Submission('queued','late-task')
        return Submission('queued','task')
    async def status(self,job,admission): admission(); return Submission('collecting',job['remote_task_id'])
    async def cancel(self,job,admission): admission(); return 'confirmed'
    async def collect(self,job,admission):
        admission()
        self.collects+=1
        if self.invalidate: self.scope['current']=False
        return Artifact('artifact-id','a'*64,'image/png',4)

class Publisher:
    def __init__(self): self.calls=0; self.lost=False
    async def publish(self,identity,job,current):
        assert await current()
        self.calls+=1
        if self.lost:
            try: await asyncio.sleep(1)
            except asyncio.CancelledError: await asyncio.sleep(.03)
        return {'operation_id':job['id'],'item_id':'output','resource_ref':'opaque-ref','sha256':'a'*64,'mime_type':'image/png'}

class MediaTest(unittest.IsolatedAsyncioTestCase):
    async def test_lifecycle_shell_corruption_purge_and_late(self):
        import ast, os, re, hashlib
        from contextlib import contextmanager
        from typing import Any, Mapping
        from core.atomic_io import atomic_write_json, atomic_write_batch, AtomicFileChange
        tree=ast.parse(Path('src/openclank/account_ancillary_lifecycle.py').read_text())
        cls=next(x for x in tree.body if isinstance(x,ast.ClassDef) and x.name=='AccountAncillaryLifecycle')
        cls.body=[x for x in cls.body if getattr(x,'name','') in {'_load_jobs','_job_store_lock','_job_matches','_job_rows_from','_job_tokens','_identity_token','_rename_jobs','_purge_jobs'}]
        def digest(items): return hashlib.sha256(json.dumps(sorted(items),separators=(',',':')).encode()).hexdigest()
        namespace={'os':os,'json':json,'Path':Path,'Any':Any,'Mapping':Mapping,'contextmanager':contextmanager,'atomic_write_json':atomic_write_json,'atomic_write_batch':atomic_write_batch,'AtomicFileChange':AtomicFileChange,'AccountAncillaryLifecycleError':RuntimeError,'hashlib':hashlib,'_digest':digest,'_SAFE_ID_RE':re.compile(r'^[A-Za-z0-9_.-]+$')}
        exec(compile(ast.fix_missing_locations(ast.Module(body=[cls],type_ignores=[])),'account-fixture','exec'),namespace)
        AccountAncillaryLifecycle=namespace['AccountAncillaryLifecycle']
        with tempfile.TemporaryDirectory() as temp:
            old_store,old_dir=bg_jobs._STORE,bg_jobs._JOBS_DIR
            bg_jobs._STORE=Path(temp)/'jobs.json'; bg_jobs._JOBS_DIR=Path(temp)/'sidecars'; bg_jobs._JOBS_DIR.mkdir()
            try:
                request={'operation':'image.generate','prompt':'not persisted'}
                identity={'account_id':'account','owner':'user','session_id':'session','workspace_id':'workspace','actor_id':'actor','turn_id':'turn',
                    'root_operation_id':'root','attempt_id':'attempt','request_digest':request_digest(request),'adapter_id':'fake','adapter_version':'1',
                    'operation':'image.generate','route_id':'route','route_kind':'remote','model_revision':'model-v1','policy_generation':1,'input_hashes':[]}
                scope={'current':True}
                async def current(_): return scope['current']
                barrier=Barrier(); adapter=Adapter(scope,barrier); publisher=Publisher(); coordinator=MediaCoordinator(barrier=barrier,account_for_owner=lambda owner: 'account' if owner=='user' else None,owner_for_account=lambda account:'user' if account=='account' else None,scope_current=current,adapters={'fake':adapter},publisher=publisher,timeout=.01)
                with bg_jobs._store_lock():
                    bg_jobs._save({'old':{'id':'old','session_id':'session','owner':'user','workspace':temp,'status':'done','ended_at':time.time(),'followed_up':True}})
                job=await coordinator.prepare(identity,request)
                self.assertEqual((await coordinator.prepare(identity,request))['id'],job['id'])
                binding={'connectionID':'route','connectionRevision':1,'interface':'comfyui-workflow-v1','ownerAccountID':'account','recipe_ref':'recipe-ref','recipe_revision':{'kind':'etag','value':'r1'},
                    'recipe_sha256':'b'*64,'destination_ref':'destination-ref','destination_revision':{'kind':'etag','value':'d1'},
                    'destination_name':'result.png','project_ref':'project','project_revision':{'kind':'etag','value':'p1'}}
                bound={**identity,'root_operation_id':'bound','attempt_id':'bound','resource_binding':binding}
                frozen=await coordinator.prepare(bound,request)
                self.assertEqual(frozen['identity']['resource_binding'],binding)
                with self.assertRaises(ValueError):
                    await coordinator.prepare({**bound,'resource_binding':{**binding,'destination_name':'changed.png'}},request)
                self.assertNotIn('not persisted',bg_jobs._STORE.read_text())
                job=await coordinator.submit(identity,job['id'],request); self.assertEqual(job['status'],'queued')
                self.assertEqual((await coordinator.cancel(identity,job['id']))['status'],'queued')
                job=await coordinator.status(identity,job['id']); self.assertEqual(job['status'],'collecting')
                adapter.invalidate=True
                job=await coordinator.collect(identity,job['id']); self.assertEqual(job['status'],'collecting'); self.assertIn('artifact',job)
                scope['current']=True; adapter.invalidate=False
                job=await coordinator.collect(identity,job['id']); self.assertEqual(job['status'],'complete'); self.assertEqual(adapter.collects,1)
                self.assertEqual(job['result']['sha256'],'a'*64)
                self.assertEqual((await coordinator.status(identity,job['id']))['status'],'complete')
                publication={**identity,'root_operation_id':'publication','attempt_id':'publication'}
                publishing=await coordinator.prepare(publication,request)
                publishing=await coordinator.submit(publication,publishing['id'],request)
                publishing=await coordinator.status(publication,publishing['id'])
                publisher.lost=True
                publishing=await coordinator.collect(publication,publishing['id'])
                self.assertEqual(publishing['status'],'unknown_outcome')
                calls=publisher.calls
                self.assertEqual((await coordinator.reconcile(publication,publishing['id']))['reconciliation_required'],'files_receipt')
                self.assertEqual(publisher.calls,calls)
                barrier.fenced=True
                await asyncio.sleep(.06)
                self.assertEqual(bg_jobs.get_media(publishing['id'],account_id='account')['status'],'complete')
                barrier.fenced=False; publisher.lost=False
                self.assertEqual([x['id'] for x in bg_jobs.list_for_scope(session_id='session',owner='user',workspace=temp)],['old'])
                self.assertIsNone(bg_jobs.kill(job['id'],session_id='session',owner='user',workspace=temp))
                self.assertFalse(bg_jobs.claim_pending_followups('test'))
                adapter.lost=True; second={**identity,'root_operation_id':'second','attempt_id':'second'}
                pending=await coordinator.prepare(second,request); pending=await coordinator.submit(second,pending['id'],request)
                self.assertEqual(pending['status'],'unknown_outcome'); self.assertEqual(adapter.calls,3)
                self.assertEqual((await coordinator.status(second,pending['id']))['status'],'unknown_outcome')
                barrier.fenced=True
                await asyncio.sleep(.06)
                pending=bg_jobs.get_media(pending['id'],account_id='account'); self.assertEqual(pending['remote_task_id'],'late-task')
                barrier.fenced=False
                adapter.supports_cancel=True
                self.assertEqual((await coordinator.cancel(second,pending['id']))['status'],'cancelled')
                adapter.lost=False
                fourth={**identity,'root_operation_id':'fourth','attempt_id':'fourth'}
                detached=await coordinator.prepare(fourth,request)
                detached=await coordinator.submit(fourth,detached['id'],request)
                foreign=bg_jobs.reserve_media({**identity,'account_id':'other-account','root_operation_id':'foreign','attempt_id':'foreign'})
                bg_jobs.detach_media_for_chat(session_id='session',owner='user',account_id='account')
                detached=bg_jobs.get_media(detached['id'],account_id='account')
                self.assertTrue(detached['chat_detached']); self.assertTrue(detached['publication_withdrawn'])
                self.assertFalse(bg_jobs.get_media(foreign['id'],account_id='other-account')['chat_detached'])
                detached=await coordinator.status(detached['identity'],detached['id'],reconcile_detached=True)
                self.assertEqual(detached['status'],'collecting')
                with bg_jobs._store_lock():
                    rows=bg_jobs._load(); rows['future']={'kind':'future','account_id':'account','opaque':'preserve'}; bg_jobs._save(rows)
                with self.assertRaises(bg_jobs.JobStoreError):
                    await coordinator.prepare({**identity,'root_operation_id':'future'},request)
                self.assertEqual(bg_jobs._load()['future']['opaque'],'preserve')
                with bg_jobs._store_lock():
                    rows=bg_jobs._load(); rows.pop('future'); bg_jobs._save(rows)
                sixth={**identity,'root_operation_id':'sixth','attempt_id':'sixth'}
                fenced=await coordinator.prepare(sixth,request)
                adapter.gate=asyncio.Event(); before_calls=adapter.calls
                running=asyncio.create_task(coordinator.submit(sixth,fenced['id'],request))
                await adapter.entered.wait(); barrier.fenced=True; adapter.gate.set()
                with self.assertRaises(RuntimeError): await running
                self.assertEqual(adapter.calls,before_calls)
                barrier.fenced=False; adapter.gate=None
                fifth={**identity,'root_operation_id':'fifth','attempt_id':'fifth'}
                interrupted=await coordinator.prepare(fifth,request)
                frozen=bg_jobs.claim_media(interrupted['id'],account_id='account',revision=interrupted['revision'],action='submit')
                calls=adapter.calls
                interrupted=await coordinator.reconcile(fifth,interrupted['id'])
                self.assertEqual(interrupted['status'],'unknown_outcome'); self.assertEqual(adapter.calls,calls)
                self.assertTrue(bg_jobs.record_media_submission(interrupted['id'],account_id='account',token=frozen['token'],remote_task_id='recovered-task'))
                third={**identity,'root_operation_id':'third','attempt_id':'third'}
                pending=await coordinator.prepare(third,request)
                claim=bg_jobs.claim_media(pending['id'],account_id='account',revision=pending['revision'],action='submit')
                lifecycle=AccountAncillaryLifecycle.__new__(AccountAncillaryLifecycle)
                lifecycle.paths=SimpleNamespace(background_jobs_file=bg_jobs._STORE,background_jobs_dir=bg_jobs._JOBS_DIR)
                before=lifecycle._job_tokens(lifecycle._job_rows_from(lifecycle._load_jobs(),'user','account'))
                self.assertTrue(bg_jobs.record_media_submission(pending['id'],account_id='account',token=claim['token'],remote_task_id='late'))
                with self.assertRaises(RuntimeError):
                    lifecycle._rename_jobs('user','renamed',account_id='account',expected_jobs=(len(before),digest(before)))
                lifecycle._rename_jobs('user','renamed',account_id='account')
                self.assertEqual(bg_jobs.get_media(pending['id'],account_id='account')['identity']['owner'],'user')
                self.assertEqual(bg_jobs.get_media(foreign['id'],account_id='other-account')['owner'],'user')
                count,unproved=lifecycle._purge_jobs('renamed',account_id='account'); self.assertGreaterEqual(count,1); self.assertTrue(unproved)
                self.assertIsNotNone(bg_jobs.get_media(foreign['id'],account_id='other-account'))
                self.assertFalse(bg_jobs.record_media_submission(pending['id'],account_id='account',token=claim['token'],remote_task_id='late'))
                bg_jobs._STORE.write_text('{bad')
                with self.assertRaises(bg_jobs.JobStoreError): await coordinator.prepare(identity,request)
                with patch('src.bg_jobs.subprocess.Popen') as spawn:
                    with self.assertRaises(bg_jobs.JobStoreError): bg_jobs.launch('echo test','session',owner='user',workspace=temp)
                    spawn.assert_not_called()
            finally:
                bg_jobs._STORE, bg_jobs._JOBS_DIR=old_store,old_dir
