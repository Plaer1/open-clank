"""J0 -> fixed fake child -> retained artifact -> existing Files import."""
import hashlib
import io
import json
import struct
import tempfile
import importlib.util
import sys
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from PIL import Image
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from core.operation_models import OperationBase
from src import bg_jobs
from src.openclank.artifacts import ArtifactStore
from src.openclank.blender_child import artifact_authority, run, SettledRender, validate_glb
from src.openclank.files_facade import FilesFacade, ProviderContext, ProviderResource
from src.openclank.media_adapters.blender import BlenderAdapter, bind_child_profile
from src.openclank.media_files import MediaFilesPublisher, BlenderFilesScope
from src.openclank.blender_human import BlenderHumanService
from src.openclank import blender_provider
from src.openclank.blender_provider import BlenderProfile
from src.openclank.media_jobs import MediaCoordinator, request_digest
spec=importlib.util.spec_from_file_location('services.memory.skill_lifecycle',Path('services/memory/skill_lifecycle.py'))
module=importlib.util.module_from_spec(spec); sys.modules[spec.name]=module; spec.loader.exec_module(module)
from src.openclank.account_ancillary_lifecycle import AccountAncillaryLifecycle, AccountAncillaryLifecycleError

class Vertical(unittest.IsolatedAsyncioTestCase):
 async def test_render_files(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp); engine=create_engine('sqlite:///'+str(root/'db'),connect_args={'check_same_thread':False}); OperationBase.metadata.create_all(engine)
   artifacts=ArtifactStore(root/'artifacts',session_factory=sessionmaker(bind=engine))
   doc=json.dumps({'asset':{'version':'2.0'},'buffers':[]},separators=(',',':')).encode(); doc+=b' '*((-len(doc))%4)
   source=struct.pack('<4sII',b'glTF',2,20+len(doc))+struct.pack('<I4s',len(doc),b'JSON')+doc; digest=hashlib.sha256(source).hexdigest()
   image=Image.new('RGB',(512,512),'blue'); buf=io.BytesIO(); image.save(buf,format='PNG'); image.close(); png=buf.getvalue()
   configured=BlenderProfile('account','route',1,'blender-render-v1','/configured/blender','b'*64,'4.0.0',(1,2,3,4),'c'*64,artifact_authority(artifacts))
   profile=configured.child_profile()
   binding={'connectionID':'route','connectionRevision':1,'interface':'blender-render-v1','ownerAccountID':'account','source_ref':'source','source_revision':{'kind':'v','value':'1'},'source_sha256':digest,'destination_ref':'dest','destination_revision':{'kind':'v','value':'1'},'destination_name':'out.png','project_ref':None,'project_revision':None}
   request={'parameters':{}}; identity={'account_id':'account','owner':'alice','session_id':'chat','workspace_id':'workspace','actor_id':'human','turn_id':'turn','root_operation_id':'root','attempt_id':'attempt','request_digest':request_digest(request),'adapter_id':'blender-glb-png-v1','adapter_version':'1','operation':'image.render','route_id':'route','route_kind':'local','model_revision':'fixed','policy_generation':1,'input_hashes':[digest],'resource_binding':binding}
   gate={'current':True}; calls=[]
   class Provider:
    name='host'; finishes=0
    async def stat(self, context, *, origin_id): return ProviderResource('dest','Folder','folder',('stat','children','write'),revision={'kind':'v','value':'1'})
    async def stage_import(self,context,*,upload,**kwargs):
     data=await upload.read(1000000); return {'stage_id':'stage','digest':'sha256:'+hashlib.sha256(data).hexdigest(),'length':len(data)}
    async def finish_import(self,context,**kwargs):
     self.finishes+=1; return {'state':'complete','items':[{'item_id':'output','outcome':'committed','resource_ref':'sealed'}]}
   provider=Provider(); facade=FilesFacade([provider]); context=ProviderContext('account','alice',1,False,'workspace')
   facade._provider_for_ref=lambda *args,**kw:(provider,SimpleNamespace(origin_id='dest',provider='host',kind='folder',stable_id='dest'))
   async def context_for(_): return context
   async def profile_for(_): return profile
   async def capture(_): return source
   async def launch(child, captured, admission):
    admission()
    self.assertEqual(bind_child_profile(configured.child_profile(),validate_glb(captured)),child['profile'])
    pending=bg_jobs.get_media(child['job_id'],account_id='account')
    with self.assertRaises(AccountAncillaryLifecycleError):
     AccountAncillaryLifecycle.__new__(AccountAncillaryLifecycle)._job_rows_from({child['job_id']:pending},'alice','account')
    calls.append(child['child_id']); run(job_id=child['job_id'],account_id='account',child_id=child['child_id'],nonce=child['nonce'],source=captured,execute=lambda *_:SettledRender(0,png),artifact_store=artifacts)
    raise RuntimeError('lost launch reply')
   adapter=BlenderAdapter(profile_for=profile_for,capture_source=capture,launch_fixed=launch,artifact_store=artifacts)
   publisher=MediaFilesPublisher(facade=facade,context_for=context_for,fetch_artifact=adapter.fetch_artifact)
   class Barrier:
    _absolute_fence=None
    def _fenced(self,owner): return False
    @asynccontextmanager
    async def admitted(self,owner): yield
   async def scope(_): return gate['current']
   async def session_current(_): return True
   authority=BlenderFilesScope(facade=facade,context_for=context_for,session_current=session_current,resolve_profile=None)
   coordinator=MediaCoordinator(barrier=Barrier(),account_for_owner=lambda _: 'account',owner_for_account=lambda _: 'alice',scope_current=scope,control_current=authority.control,adapters={adapter.adapter_id:adapter},publisher=publisher,timeout=1)
   binary=root/'blender'; binary.write_bytes(b'fake'); binary.chmod(0o700)
   auth=SimpleNamespace(account_id=lambda _: 'account',username_for_account_id=lambda _: 'alice')
   row=SimpleNamespace(owner='alice',id='route',revision=1,enabled=True,deleted_at=None,family_id='blender',adapter_id='blender-render-v1',kind='local',billing_lane='local',settings={'interface':'blender-render-v1','executablePath':str(binary),'executableSHA256':'b'*64,'executableVersion':'4.0.0'})
   store=SimpleNamespace(get_connection=lambda **_:row)
   with patch.object(blender_provider,'_version',side_effect=AssertionError('unexpected version probe')):
    blender_provider._current(store=store,auth=auth,artifacts=artifacts,owner='alice',account='account',connection='route',revision=1)

   with patch.object(bg_jobs,'_STORE',root/'jobs.json'), patch.object(bg_jobs,'_JOBS_DIR',root/'jobs'):
    job=await coordinator.prepare(identity,request); job=await coordinator.submit(identity,job['id'],request)
    self.assertEqual(job['status'],'unknown_outcome'); job=await coordinator.status(identity,job['id']); self.assertEqual(job['status'],'collecting')
    job=await coordinator.collect(identity,job['id']);  self.assertEqual(job['status'],'complete'); self.assertEqual(job['result']['sha256'],hashlib.sha256(png).hexdigest()); self.assertEqual(provider.finishes,1)
    self.assertEqual(len(calls),1)
    another={**identity,'root_operation_id':'second','attempt_id':'second'}
    other=await coordinator.prepare(another,request); other=await coordinator.submit(another,other['id'],request)
    binary.unlink(); gate['current']=False
    with patch.object(blender_provider,'_version',side_effect=AssertionError('unexpected version probe')):
     other=await coordinator.status(another,other['id'])
    self.assertEqual(other['status'],'collecting'); self.assertIsNotNone(other.get('artifact'))
    with self.assertRaises(Exception): await coordinator.collect(another,other['id'])
    self.assertEqual(provider.finishes,1)
    service=BlenderHumanService(coordinator=coordinator,scope=authority,profile_selection=None,barrier=Barrier(),recovery_context_for=context_for,artifact_store=artifacts)
    preview=await service.recovery_preview(account='account',owner='alice',job_id=other['id'],destination_ref='dest',destination_name='recovered.png')
    recovered=await service.recovery_save(account='account',owner='alice',job_id=other['id'],revision=preview['revision'],recovery_id=preview['recoveryID'])
    self.assertEqual(recovered['state'],'complete'); self.assertEqual(provider.finishes,2)
   engine.dispose()
