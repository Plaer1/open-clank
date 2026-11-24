"""Disposable fake HTTP and existing Files staged-import seam."""
import asyncio
import hashlib
import io
import json
import tempfile
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import httpx
from PIL import Image
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from core.operation_models import OperationBase
from src.openclank.artifacts import ArtifactStore, ArtifactNotFound, media_artifact_owner
from src.openclank.files_facade import FilesFacade, FilesFacadeError, ProviderContent, ProviderContext, ProviderResource
from src.openclank.media_adapters.comfyui import ComfyUIAdapter, render_recipe
from src.openclank.media_files import MediaFilesPublisher, MediaFilesScope
from src.openclank.media_jobs import MediaCoordinator, request_digest
from src.openclank.media_human import HumanMediaService
from src import bg_jobs


class ComfyVertical(unittest.IsolatedAsyncioTestCase):
    async def test_pinned_task_and_files_receipt(self):
        private = tempfile.TemporaryDirectory(); self.addCleanup(private.cleanup)
        engine = create_engine('sqlite:///' + str(Path(private.name)/'artifacts.db'), connect_args={'check_same_thread':False})
        self.addCleanup(engine.dispose); OperationBase.metadata.create_all(engine)
        artifacts = ArtifactStore(Path(private.name)/'spool',session_factory=sessionmaker(bind=engine))
        image = io.BytesIO()
        Image.new('RGB', (1, 1), 'red').save(image, format='PNG')
        png = image.getvalue()
        recipe = json.dumps({'version':1,'workflow':{'1': {'class_type':'FixtureNode','inputs': {'text': ''}}},
                             'slots':{'prompt':{'node':'1','input':'text','type':'string'}}}).encode()
        binding = {'connectionID':'route', 'connectionRevision':1, 'interface':'comfyui-workflow-v1',
                   'ownerAccountID':'account', 'recipe_ref':'recipe', 'recipe_revision':{'kind':'v','value':'1'},
                   'recipe_sha256':hashlib.sha256(recipe).hexdigest(), 'destination_ref':'dest',
                   'destination_revision':{'kind':'v','value':'1'}, 'destination_name':'out.png',
                   'project_ref':'project', 'project_revision':{'kind':'v','value':'1'}}
        identity = {'account_id':'account','workspace_id':'default','policy_generation':1,'resource_binding':binding}
        calls = []; fault={'lost':False}
        def respond(request):
            calls.append((request.method, request.url.path))
            if request.url.path == '/prompt':
                if fault['lost']: raise httpx.ReadTimeout('fixture lost reply')
                return httpx.Response(200, json={'prompt_id':json.loads(request.content)['prompt_id']})
            if request.url.path.endswith('/cancel'): return httpx.Response(200, json={'cancelled':True})
            if request.url.path == '/view': return httpx.Response(404 if fault.get('removed') else 200, content=png)
            return httpx.Response(200, json={'id':request.url.path.rsplit('/',1)[-1], 'status':'completed', 'outputs_count':1,
                'preview_output':{'filename':'out.png','subfolder':'','type':'output','mediaType':'images'}})
        transport=httpx.MockTransport(respond)
        @asynccontextmanager
        async def profile(identity):
            if fault.get('profile_off'): raise ValueError('profile unavailable')
            async def current_profile(): pass
            yield (SimpleNamespace(origin='http://127.0.0.1:8188',credentialMode='none',job_binding=lambda:{k:binding[k] for k in ('connectionID','connectionRevision','interface','ownerAccountID')}),current_profile)
        async def capture(_):
            evidence={k:binding[k] for k in ('recipe_ref','recipe_revision','recipe_sha256')}
            if fault.get('stale'): evidence['recipe_sha256']='0'*64
            return evidence,recipe
        adapter=ComfyUIAdapter(resolve_profile=profile,capture_recipe=capture,artifact_store=artifacts,
            client_factory=lambda **kw:httpx.AsyncClient(transport=transport,**kw))
        job={'id':'0123456789abcdef0123456789abcdef','account_id':'account','identity':identity,'identity_digest':'a'*64}
        submission=await adapter.submit({'parameters':{'prompt':'fixture'}},job,lambda:None)
        task=submission.task_id; job['remote_task_id']=task
        self.assertEqual((await adapter.status(job,lambda:None)).state,'collecting')
        self.assertEqual(await adapter.cancel(job,lambda:None),'requested')
        artifact=await adapter.collect(job,lambda:None); job['artifact']=vars(artifact)
        artifact_owner=media_artifact_owner('account')
        duplicate=artifacts.put(owner=artifact_owner,chunks=[png],media_type='image/png')
        self.assertEqual((duplicate.id,duplicate.state,duplicate.expires_at),(artifact.artifact_id,'acknowledged',None))
        fault['removed']=True

        gate={'current':True, 'invalidate':False}
        class Provider:
            name='host'; finishes=0; revision='1'
            async def stat(self, context, *, origin_id):
                if origin_id == 'recipe':
                    return ProviderResource('recipe','recipe.json','file',('stat','download'),size=len(recipe),revision={'kind':'v','value':self.revision})
                return ProviderResource('dest','Folder','folder',('stat','children','write'),revision={'kind':'v','value':self.revision})
            async def content(self, context, *, origin_id):
                return ProviderContent('recipe','recipe.json','application/json',data=recipe,size=len(recipe))
            async def stage_import(self, context, *, upload, **kwargs):
                data=await upload.read(1000000)
                if gate['invalidate']: gate['current']=False
                return {'stage_id':'stage','digest':'sha256:'+hashlib.sha256(data).hexdigest(),'length':len(data)}
            async def finish_import(self, context, **kwargs):
                self.finishes+=1
                return {'state':'complete','items':[{'item_id':'output','outcome':'committed','resource_ref':'sealed'}]}
        provider=Provider(); facade=FilesFacade([provider]); context=ProviderContext('account','user',1)
        def resolve_ref(ctx,ref,**kw):
            if fault.get('expired') and ref == 'recipe': raise FilesFacadeError('expired',code='resource_ref_stale')
            return provider,SimpleNamespace(origin_id=ref,provider='host',kind='folder',stable_id=ref)
        facade._provider_for_ref=resolve_ref
        async def context_for(_): return context
        async def current(): return gate['current']
        async def allowed(_): return True
        async def project(_): return {'project_ref':'project','project_revision':{'kind':'v','value':'1'}}
        scope_authority=MediaFilesScope(facade=facade,context_for=context_for,session_current=allowed,
            project_current=allowed,capture_project=project,resolve_profile=profile)
        issued,_=await scope_authority.capture({**identity,'route_id':'route'},recipe_ref='recipe',destination_ref='dest',destination_name='out.png')
        self.assertEqual(issued,binding)
        self.assertTrue(await scope_authority.current(identity))
        publisher=MediaFilesPublisher(facade=facade,context_for=context_for,fetch_artifact=adapter.fetch_artifact)
        result=await publisher.publish(identity,job,current)
        self.assertEqual(result['sha256'],hashlib.sha256(png).hexdigest())
        self.assertEqual(provider.finishes,1)
        self.assertEqual(sum(1 for method,path in calls if path=='/view'),1)
        fault['removed']=False
        numeric=json.dumps({'version':1,'workflow':{'1':{'class_type':'FixtureNode','inputs':{'steps':1}}},
            'slots':{'steps':{'node':'1','input':'steps','type':'integer','min':1,'max':20}}}).encode()
        self.assertEqual(render_recipe(numeric,{'steps':20})[0]['1']['inputs']['steps'],20)
        with self.assertRaises(ValueError): render_recipe(numeric,{'steps':21})
        with self.assertRaises(ValueError): render_recipe(numeric.replace(b'"version": 1',b'"version": true'),{'steps':1})
        gate['invalidate']=True
        with self.assertRaises(Exception): await publisher.publish(identity,{**job,'id':'2123456789abcdef0123456789abcdef'},current)
        self.assertEqual(provider.finishes,1)
        self.assertEqual(b''.join(artifacts.read_chunks(owner=artifact_owner,artifact_id=artifact.artifact_id)),png)
        gate['invalidate']=False; gate['current']=True
        provider.revision='2'
        self.assertFalse(await scope_authority.current(identity))
        with self.assertRaises(Exception): await publisher.publish(identity,{**job,'id':'1123456789abcdef0123456789abcdef'},current)
        self.assertEqual(provider.finishes,1)

        class Barrier:
            _absolute_fence=None
            def _fenced(self, owner): return False
            @asynccontextmanager
            async def admitted(self, owner): yield
        provider.revision='1'
        request={'parameters':{'prompt':'fixture'}}
        full={**identity,'owner':'user','session_id':'session','actor_id':'actor','turn_id':'turn',
              'root_operation_id':'root','attempt_id':'attempt','request_digest':request_digest(request),
              'adapter_id':'comfyui','adapter_version':'1','operation':'image.generate',
              'route_id':'route','route_kind':'remote','model_revision':'model','input_hashes':[]}
        async def scope(_): return True
        coordinator=MediaCoordinator(barrier=Barrier(),account_for_owner=lambda _: 'account',owner_for_account=lambda _: 'user',
            scope_current=scope,adapters={'comfyui':adapter},publisher=publisher,timeout=1)
        with tempfile.TemporaryDirectory() as root:
            old_store,old_dir=bg_jobs._STORE,bg_jobs._JOBS_DIR
            bg_jobs._STORE=Path(root)/'jobs.json'; bg_jobs._JOBS_DIR=Path(root)/'jobs'; bg_jobs._JOBS_DIR.mkdir()
            try:
                queued=await coordinator.prepare(full,request)
                queued=await coordinator.submit(full,queued['id'],request)
                self.assertEqual(queued['status'],'queued')
                self.assertEqual((await coordinator.cancel(full,queued['id']))['status'],'cancel_requested')
                collecting=await coordinator.status(full,queued['id'])
                self.assertEqual(collecting['status'],'collecting')
                completed=await coordinator.collect(full,queued['id'])
                self.assertEqual(completed['status'],'complete')
                self.assertEqual(completed['result']['sha256'],hashlib.sha256(png).hexdigest())
                coordinator.control_current=scope_authority.control
                fault['profile_off']=True
                self.assertEqual((await coordinator.status(full,completed['id']))['status'],'complete')
                fault['profile_off']=False
                page=bg_jobs.list_media(account_id='account',owner='user',limit=1)
                self.assertEqual(len(page['items']),1)
                self.assertEqual(bg_jobs.list_media(account_id='other',owner='user')['items'],[])
                other={**full,'root_operation_id':'stale','attempt_id':'stale'}
                stale=await coordinator.prepare(other,request)
                fault['stale']=True; count=calls.count(('POST','/prompt'))
                stale=await coordinator.submit(other,stale['id'],request)
                self.assertEqual((stale['status'],stale['reason']),('failed','not_dispatched'))
                self.assertEqual(calls.count(('POST','/prompt')),count)
                fault['stale']=False
                fault['lost']=True
                other={**full,'root_operation_id':'lost','attempt_id':'lost'}
                lost=await coordinator.prepare(other,request)
                lost=await coordinator.submit(other,lost['id'],request)
                self.assertEqual(lost['status'],'unknown_outcome')
                count=calls.count(('POST','/prompt'))
                self.assertEqual((await coordinator.reconcile(other,lost['id']))['status'],'unknown_outcome')
                self.assertEqual(calls.count(('POST','/prompt')),count)
                fault['lost']=False
                adapter.capture_recipe=scope_authority.recipe
                coordinator.scope_current=scope_authority.current
                coordinator.control_current=scope_authority.control
                async def select_profile(owner, account, connection):
                    return {k:binding[k] for k in ('connectionID','connectionRevision','interface','ownerAccountID')}
                human=HumanMediaService(coordinator=coordinator,scope=scope_authority,profile_selection=select_profile,barrier=coordinator.barrier,
                    recovery_context_for=context_for,artifact_store=artifacts)
                prepared=await human.prepare(account='account',owner='user',workspace='default',generation=1,session_id='session',
                    connection_id='route',recipe_ref='recipe',destination_ref='dest',destination_name='out.png',parameters={'prompt':'fixture'})
                self.assertEqual(prepared['state'],'prepared')
                with self.assertRaises(Exception):
                    await human.run(account='account',owner='user',job_id=prepared['jobID'],revision=prepared['revision']+1,parameters={'prompt':'fixture'})
                started=await human.run(account='account',owner='user',job_id=prepared['jobID'],revision=prepared['revision'],parameters={'prompt':'fixture'})
                self.assertEqual(started['state'],'queued')
                fault['expired']=True
                self.assertEqual((await human.action(account='account',owner='user',job_id=prepared['jobID'],action='cancel'))['state'],'cancel_requested')
                self.assertEqual((await human.action(account='account',owner='user',job_id=prepared['jobID'],action='status'))['state'],'collecting')
                with self.assertRaises(Exception): await human.action(account='account',owner='user',job_id=prepared['jobID'],action='collect')
                fault['expired']=False
                withdrawn=await human.prepare(account='account',owner='user',workspace='default',generation=1,session_id='session',
                    connection_id='route',recipe_ref='recipe',destination_ref='dest',destination_name='out.png',parameters={'prompt':'fixture'})
                await human.run(account='account',owner='user',job_id=withdrawn['jobID'],revision=withdrawn['revision'],parameters={'prompt':'fixture'})
                await human.action(account='account',owner='user',job_id=withdrawn['jobID'],action='status')
                coordinator.publisher=None
                self.assertEqual((await human.action(account='account',owner='user',job_id=withdrawn['jobID'],action='collect'))['state'],'collecting')
                uncertain=await human.prepare(account='account',owner='user',workspace='default',generation=1,session_id='session',
                    connection_id='route',recipe_ref='recipe',destination_ref='dest',destination_name='out.png',parameters={'prompt':'fixture'})
                await human.run(account='account',owner='user',job_id=uncertain['jobID'],revision=uncertain['revision'],parameters={'prompt':'fixture'})
                await human.action(account='account',owner='user',job_id=uncertain['jobID'],action='status')
                await human.action(account='account',owner='user',job_id=uncertain['jobID'],action='collect')
                uncertain_row=bg_jobs.get_media(uncertain['jobID'],account_id='account')
                claim=bg_jobs.claim_media(uncertain['jobID'],account_id='account',revision=uncertain_row['revision'],action='publish')
                bg_jobs.mark_media_uncertain(uncertain['jobID'],account_id='account',revision=claim['revision'])
                with self.assertRaises(Exception): await human.recovery_preview(account='account',owner='user',job_id=uncertain['jobID'],destination_ref='dest',destination_name='no.png')
                self.assertIsNone(bg_jobs.get_media(uncertain['jobID'],account_id='account').get('recovery'))
                pending=await human.prepare(account='account',owner='user',workspace='default',generation=1,session_id='session',
                    connection_id='route',recipe_ref='recipe',destination_ref='dest',destination_name='out.png',parameters={'prompt':'fixture'})
                await human.run(account='account',owner='user',job_id=pending['jobID'],revision=pending['revision'],parameters={'prompt':'fixture'})
                await human.action(account='account',owner='user',job_id=pending['jobID'],action='status')
                await human.action(account='account',owner='user',job_id=pending['jobID'],action='collect')
                another=await human.prepare(account='account',owner='user',workspace='default',generation=1,session_id='session',
                    connection_id='route',recipe_ref='recipe',destination_ref='dest',destination_name='out.png',parameters={'prompt':'fixture'})
                await human.run(account='account',owner='user',job_id=another['jobID'],revision=another['revision'],parameters={'prompt':'fixture'})
                await human.action(account='account',owner='user',job_id=another['jobID'],action='status')
                coordinator.publisher=None
                self.assertEqual((await human.action(account='account',owner='user',job_id=another['jobID'],action='collect'))['state'],'collecting')
                fault['expired']=True; fault['removed']=True
                preview=await human.recovery_preview(account='account',owner='user',job_id=another['jobID'],destination_ref='dest',destination_name='recovered.png')
                count=provider.finishes
                original_import=facade.import_file
                async def lost_import(*args,**kwargs):
                    await original_import(*args,**kwargs)
                    raise TimeoutError('fixture lost Files reply')
                facade.import_file=lost_import
                with self.assertRaises(TimeoutError):
                    await human.recovery_save(account='account',owner='user',job_id=another['jobID'],revision=preview['revision'],recovery_id=preview['recoveryID'])
                facade.import_file=original_import
                saved=await human.recovery_save(account='account',owner='user',job_id=another['jobID'],revision=preview['revision'],recovery_id=preview['recoveryID'])
                self.assertEqual(saved['state'],'complete'); self.assertEqual(provider.finishes,count+1)
                self.assertEqual((await human.recovery_save(account='account',owner='user',job_id=another['jobID'],revision=preview['revision'],recovery_id=preview['recoveryID']))['state'],'complete')
                self.assertEqual(provider.finishes,count+1)
                from src.openclank.account_request_barrier import AccountRequestBarrier
                real_barrier=AccountRequestBarrier(lambda owner:False)
                coordinator.barrier=real_barrier; coordinator.timeout=.01; human.barrier=real_barrier
                pending_preview=await human.recovery_preview(account='account',owner='user',job_id=pending['jobID'],destination_ref='dest',destination_name='settled.png')
                started_event=asyncio.Event(); release_event=asyncio.Event()
                async def slow_reply(*args,**kwargs):
                    result=await original_import(*args,**kwargs)
                    started_event.set(); await release_event.wait(); return result
                facade.import_file=slow_reply
                with self.assertRaises(TimeoutError):
                    await human.recovery_save(account='account',owner='user',job_id=pending['jobID'],revision=pending_preview['revision'],recovery_id=pending_preview['recoveryID'])
                await started_event.wait()
                with self.assertRaises(TimeoutError): await asyncio.wait_for(real_barrier.drain('user'),.01)
                drain=asyncio.create_task(real_barrier.drain('user'))
                await asyncio.sleep(.01); self.assertFalse(drain.done())
                release_event.set(); await asyncio.wait_for(drain,1)
                await asyncio.sleep(.01)
                settled=bg_jobs.get_media(pending['jobID'],account_id='account')
                self.assertEqual(settled['status'],'complete')
                recovery=settled['recovery']
                with bg_jobs._store_lock():
                    rows=bg_jobs._load(); rows.pop(pending['jobID']); bg_jobs._save(rows)
                self.assertFalse(bg_jobs.finish_media_recovery(pending['jobID'],account_id='account',recovery_id=recovery['id'],token=recovery['token'],result=None))
                from src.openclank.file_policy import FilePolicyRepository
                policy=FilePolicyRepository(Path(root)/'policy.sqlite3')
                exact=policy.claim_host_mutation(owner_subject_id='account',workspace_id='default',generation=policy.generation(),paths=[str(Path(root)/'target')])
                self.assertTrue(policy.has_unsettled_host_mutation(owner_subject_id='account'))
                self.assertFalse(policy.has_unsettled_host_mutation(owner_subject_id='other-account'))
                policy.release_host_mutation(owner_subject_id='account',**exact)
                self.assertFalse(policy.has_unsettled_host_mutation(owner_subject_id='account'))
                facade.import_file=original_import
                bg_jobs.detach_media_for_chat(session_id='session',owner='user',account_id='account')
                with self.assertRaises(Exception): await human.recovery_preview(account='account',owner='user',job_id=withdrawn['jobID'],destination_ref='dest',destination_name='no.png')
                other_owner=media_artifact_owner('recreated-account')
                other=artifacts.put(owner=other_owner,chunks=[png],media_type='image/png')
                artifacts.acknowledge(owner=other_owner,artifact_id=other.id)
                # The private fixture environment lacks unrelated bs4; use the
                # actual two lifecycle methods without importing search services.
                import ast
                from typing import Any
                tree=ast.parse(Path('src/openclank/account_lifecycle.py').read_text())
                cls=next(node for node in tree.body if isinstance(node,ast.ClassDef) and node.name=='AccountOwnerLifecycle')
                cls.body=[node for node in cls.body if getattr(node,'name','') in {'media_artifact_inventory','purge_owner'}]
                namespace={'Any':Any, '_owner':lambda value:value, 'purge_default_persona_owner':lambda *a,**k:0,
                    'AccountOwnerLifecycleReceipt':lambda *args:SimpleNamespace(stores=args[2]), 'AccountOwnerLifecycleError':RuntimeError}
                exec(compile(ast.fix_missing_locations(ast.Module(body=[cls],type_ignores=[])),'lifecycle-fixture','exec'),namespace)
                AccountOwnerLifecycle=namespace['AccountOwnerLifecycle']
                lifecycle=AccountOwnerLifecycle.__new__(AccountOwnerLifecycle)
                lifecycle.artifact_store=artifacts
                lifecycle._purge_host_intents=lambda owner: 0
                lifecycle.conversation_archive=lifecycle.provider_store=lifecycle.operation_journal=SimpleNamespace(purge_owner=lambda owner:0)
                lifecycle.preset_manager=SimpleNamespace(presets={})
                inventory=lifecycle.media_artifact_inventory('account')
                lifecycle.purge_owner('user',subject_id='account',expected_media_inventory=inventory)
                with self.assertRaises(ArtifactNotFound): artifacts.get(owner=artifact_owner,artifact_id=artifact.artifact_id)
                self.assertEqual(artifacts.get(owner=other_owner,artifact_id=other.id).content_sha256,artifact.sha256)
                # A same-size corrupt dedup payload is never accepted as verified.
                artifacts._path(other.relative_path).write_bytes(b'x'*len(png))
                with self.assertRaises(Exception): artifacts.put(owner=other_owner,chunks=[png],media_type='image/png')
            finally:
                bg_jobs._STORE,bg_jobs._JOBS_DIR=old_store,old_dir
