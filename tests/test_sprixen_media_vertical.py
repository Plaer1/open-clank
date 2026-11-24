"""Fake Sprixen HTTP through real credential/J0/artifact/Files authorities."""
import hashlib
import io
import tempfile
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import httpx
from PIL import Image
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from core.provider_models import ProviderBase
from core.operation_models import OperationBase
from src import bg_jobs, secret_storage
from src.openclank.provider_store import ProviderStore
from src.openclank.artifacts import ArtifactStore
from src.openclank.sprixen_provider import admitted_sprixen_profile, _current
from src.openclank.sprixen_human import SprixenHumanService
from src.openclank.media_adapters.sprixen import SprixenAdapter
from src.openclank.media_files import SprixenFilesScope, MediaFilesPublisher
from src.openclank.media_jobs import MediaCoordinator
from src.openclank.files_facade import FilesFacade, ProviderContext, ProviderResource

class Vertical(unittest.IsolatedAsyncioTestCase):
 async def test_sprite(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp); engine=create_engine('sqlite:///'+str(root/'db'),connect_args={'check_same_thread':False}); ProviderBase.metadata.create_all(engine); OperationBase.metadata.create_all(engine)
   factory=sessionmaker(bind=engine); store=ProviderStore(factory); artifacts=ArtifactStore(root/'spool',session_factory=factory)
   image=Image.new('RGBA',(32,32),'red'); buf=io.BytesIO(); image.save(buf,format='PNG'); image.close(); png=buf.getvalue()
   mode={'lost':False,'bad_asset':False,'stale':False,'key':'spx_live_fixture'}; calls=[]
   def respond(req):
    calls.append((req.method,req.url.path))
    if req.url.path.startswith('/api/public/'):
     self.assertNotIn('authorization',req.headers); self.assertNotIn('cookie',req.headers)
     if mode.get('stale_on_asset'): mode['stale']=True
     return httpx.Response(200,content=png)
    self.assertEqual(req.headers.get('authorization'),'Bearer '+mode['key'])
    if req.method=='POST':
     if mode['lost']: raise httpx.ReadTimeout('lost')
     if mode.get('duplicate'): return httpx.Response(200,content=b'{"id":"gen_fixture","id":"gen_other","status":"pending"}')
     return httpx.Response(200,json={'id':'gen_fixture','status':'pending'})
    path='/other' if mode['bad_asset'] else '/api/public/uploads/generations/usr_1/gen_fixture_0.png'
    return httpx.Response(200,json={'id':'gen_fixture','status':'completed','resultUrls':[path]})
   transport=httpx.MockTransport(respond)
   class Barrier:
    _absolute_fence=None
    def _fenced(self,owner): return False
    @asynccontextmanager
    async def admitted(self,owner): yield
   barrier=Barrier(); auth=SimpleNamespace(account_id=lambda _: 'host-id',username_for_account_id=lambda _: 'alice')
   class Provider:
    name='host'; finishes=0
    async def stat(self,context,*,origin_id): return ProviderResource('dest','Folder','folder',('stat','children','write'),revision={'kind':'v','value':'2' if mode['stale'] else '1'})
    async def stage_import(self,context,*,upload,**kwargs):
     data=await upload.read(1000000); return {'stage_id':'stage','digest':'sha256:'+hashlib.sha256(data).hexdigest(),'length':len(data)}
    async def finish_import(self,context,**kwargs): self.finishes+=1; return {'state':'complete','items':[{'item_id':'output','outcome':'committed','resource_ref':'sealed'}]}
   provider=Provider(); facade=FilesFacade([provider]); context=ProviderContext('host-id','alice',1,False,'workspace')
   facade._provider_for_ref=lambda *args,**kwargs:(provider,SimpleNamespace(origin_id='dest',provider='host',kind='folder',stable_id='dest'))
   async def context_for(_): return context
   async def session_current(_): return True
   def resolve(identity, job=None): return admitted_sprixen_profile(store=store,auth_manager=auth,account_barrier=barrier,owner='alice',identity=identity,job=job)
   async def selection(owner,host,connection_id,account_id):
    c=store.get_connection(owner=owner,connection_id=connection_id); a=store.get_account(owner=owner,account_id=account_id)
    return _current(store=store,auth=auth,owner=owner,host_account=host,connection_id=c.id,connection_revision=c.revision,provider_account_id=a.id,provider_account_revision=a.revision,credential_version=a.credential_version).job_binding()
   scope=SprixenFilesScope(facade=facade,context_for=context_for,session_current=session_current,resolve_profile=resolve)
   adapter=SprixenAdapter(resolve_profile=resolve,artifact_store=artifacts,client_factory=lambda **kw:httpx.AsyncClient(transport=transport,**kw))
   publisher=MediaFilesPublisher(facade=facade,context_for=context_for,fetch_artifact=adapter.fetch_artifact)
   coordinator=MediaCoordinator(barrier=barrier,account_for_owner=auth.account_id,owner_for_account=auth.username_for_account_id,scope_current=scope.current,control_current=scope.control,adapters={adapter.adapter_id:adapter},publisher=publisher,timeout=1)
   service=SprixenHumanService(coordinator=coordinator,scope=scope,profile_selection=selection,barrier=barrier,recovery_context_for=context_for,artifact_store=artifacts)
   with patch.object(secret_storage,'_KEY_PATH',root/'key'), patch.object(secret_storage,'_fernet',None), patch.object(bg_jobs,'_STORE',root/'jobs.json'), patch.object(bg_jobs,'_JOBS_DIR',root/'jobs'):
    connection=store.create_connection(owner='alice',connection_id='connection',family_id='sprixen',adapter_id='sprixen-sprite-v1',kind='official',billing_lane='metered_api',label='Sprite',normalized_url='https://api.sprixen.com',settings={'interface':'sprixen-sprite-v1'})
    account=store.create_account(owner='alice',connection_id=connection.id,account_id='provider-account',label='key',auth_method='api_key',acquisition_method='manual-key',auth_class='metered',credentials={'type':'api','key':'spx_live_fixture'})
    params={'prompt':'red sprite','resolution':'32x32','pixelPerfect':'auto'}
    async def prepare(): return await service.prepare_sprite(account='host-id',owner='alice',workspace='workspace',generation=1,session_id='chat',connection_id=connection.id,provider_account_id=account.id,destination_ref='dest',destination_name='out.png',parameters=params)
    prepared=await prepare(); result=await service.run(account='host-id',owner='alice',job_id=prepared['jobID'],revision=prepared['revision'],parameters=params); self.assertEqual(result['state'],'queued')
    result=await service.action(account='host-id',owner='alice',job_id=prepared['jobID'],action='status'); self.assertEqual(result['state'],'collecting')
    result=await service.action(account='host-id',owner='alice',job_id=prepared['jobID'],action='collect'); self.assertEqual(result['state'],'complete'); self.assertEqual(provider.finishes,1)
    mode['lost']=True; lost=await prepare(); result=await service.run(account='host-id',owner='alice',job_id=lost['jobID'],revision=lost['revision'],parameters=params); self.assertEqual(result['state'],'unknown_outcome'); before=len(calls)
    result=await service.action(account='host-id',owner='alice',job_id=lost['jobID'],action='status'); self.assertEqual(result['state'],'unknown_outcome'); self.assertEqual(len(calls),before)
    mode['lost']=False; mode['bad_asset']=True
    rejected=await prepare(); await service.run(account='host-id',owner='alice',job_id=rejected['jobID'],revision=rejected['revision'],parameters=params)
    await service.action(account='host-id',owner='alice',job_id=rejected['jobID'],action='status')
    result=await service.action(account='host-id',owner='alice',job_id=rejected['jobID'],action='collect'); self.assertEqual(result['state'],'unknown_outcome'); self.assertEqual(provider.finishes,1)
    mode['bad_asset']=False; held=await prepare(); await service.run(account='host-id',owner='alice',job_id=held['jobID'],revision=held['revision'],parameters=params)
    await service.action(account='host-id',owner='alice',job_id=held['jobID'],action='status')
    mode['stale_on_asset']=True
    result=await service.action(account='host-id',owner='alice',job_id=held['jobID'],action='collect'); self.assertEqual(result['state'],'collecting'); self.assertEqual(provider.finishes,1)
    preview=await service.recovery_preview(account='host-id',owner='alice',job_id=held['jobID'],destination_ref='dest',destination_name='recovered.png')
    result=await service.recovery_save(account='host-id',owner='alice',job_id=held['jobID'],revision=preview['revision'],recovery_id=preview['recoveryID']); self.assertEqual(result['state'],'complete'); self.assertEqual(provider.finishes,2)
    mode['stale_on_asset']=False; mode['stale']=False
    persisted=(root/'jobs.json').read_text(); self.assertNotIn('spx_live_fixture',persisted); self.assertNotIn('red sprite',persisted)
    mode['duplicate']=True; duplicate=await prepare(); result=await service.run(account='host-id',owner='alice',job_id=duplicate['jobID'],revision=duplicate['revision'],parameters=params)
    self.assertEqual(result['state'],'unknown_outcome'); mode['duplicate']=False
    known=await prepare(); result=await service.run(account='host-id',owner='alice',job_id=known['jobID'],revision=known['revision'],parameters=params); self.assertEqual(result['state'],'queued')
    stale=await prepare(); before=len(calls)
    store.replace_account_credential(owner='alice',account_id=account.id,expected_credential_version=1,credentials={'type':'api','key':'spx_live_new'}); mode['key']='spx_live_new'
    with self.assertRaises(Exception): await service.run(account='host-id',owner='alice',job_id=stale['jobID'],revision=stale['revision'],parameters=params)
    with self.assertRaises(Exception): await service.run(account='host-id',owner='alice',job_id=known['jobID'],revision=known['revision'],parameters=params)
    self.assertEqual(len(calls),before)
    result=await service.action(account='host-id',owner='alice',job_id=known['jobID'],action='status'); self.assertEqual(result['state'],'collecting')
    result=await service.action(account='host-id',owner='alice',job_id=known['jobID'],action='collect'); self.assertEqual(result['state'],'collecting')
    self.assertEqual(provider.finishes,2)
    preview=await service.recovery_preview(account='host-id',owner='alice',job_id=known['jobID'],destination_ref='dest',destination_name='rotated.png')
    result=await service.recovery_save(account='host-id',owner='alice',job_id=known['jobID'],revision=preview['revision'],recovery_id=preview['recoveryID']); self.assertEqual(result['state'],'complete')
    second_account=store.create_account(owner='alice',connection_id=connection.id,account_id='other-provider-account',label='other',auth_method='api_key',acquisition_method='manual-key',auth_class='metered',credentials={'type':'api','key':'spx_live_other'})
    disabled=await prepare(); await service.run(account='host-id',owner='alice',job_id=disabled['jobID'],revision=disabled['revision'],parameters=params)
    original=bg_jobs.get_media(disabled['jobID'],account_id='host-id'); claim=bg_jobs.claim_media(disabled['jobID'],account_id='host-id',revision=original['revision'],action='status')
    wrong={**original['identity'],'resource_binding':{**original['identity']['resource_binding'],'providerAccountID':second_account.id}}
    with self.assertRaises(ValueError):
     async with admitted_sprixen_profile(store=store,auth_manager=auth,account_barrier=barrier,owner='alice',identity=wrong,job=claim['record']): pass
    self.assertTrue(bg_jobs.finish_media(disabled['jobID'],account_id='host-id',token=claim['token'],status='queued'))
    current=store.get_account(owner='alice',account_id=account.id); store.update_account(owner='alice',account_id=account.id,expected_revision=current.revision,enabled=False)
    before=len(calls); result=await service.action(account='host-id',owner='alice',job_id=disabled['jobID'],action='status'); self.assertEqual(result['state'],'unknown_outcome'); self.assertEqual(len(calls),before)
   engine.dispose()
