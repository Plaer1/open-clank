"""Synthetic Cookbook review-to-worker path; no actual SDK, process or network."""
import hashlib
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch
import httpx
from fastapi import FastAPI
from routes import cookbook_routes
from src.openclank import hub_discovery
from src.openclank.cookbook_state import transaction, PRIVATE_KEY
from src.openclank.cookbook_worker import run, ChildSettled


class FlowTest(unittest.IsolatedAsyncioTestCase):
 async def test_review_dispatch_receipt(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp)/'cache'; root.mkdir(); path=Path(tmp)/'state.json'; payload=b'abc'; sha=hashlib.sha256(payload).hexdigest()
   manifest={'repoID':'org/model','revision':'a'*40,'gating':{'state':'ungated'},'license':{'value':None,'source':None,'status':'unknown'},'files':[{'path':'file','size':3,'blobID':None,'lfsSHA256':sha}]}
   class Auth:
    users={"alice":{}}
    def is_admin(self, owner): return owner == "alice"
    def account_id(self, owner): return 'account'
    def username_for_account_id(self, account): return 'alice'
   class Proc:
    async def wait(self): return 0
   calls=[]
   async def spawn(*args,**kwargs): calls.append(args); raise RuntimeError("lost launch reply")
   async def get_manifest(repo): return manifest
   app=FastAPI(); app.state.auth_manager=Auth()
   @app.middleware('http')
   async def identity(request, call_next):
    kind=request.headers.get('x-test-auth','human')
    request.state.authenticated=True
    request.state.current_user='internal-tool' if kind=='internal' else 'alice'
    request.state.internal_tool_authenticated=kind=='internal'
    request.state.api_token=kind=='api'
    return await call_next(request)
   fake_sdk=types.ModuleType('huggingface_hub')
   def download(repo_id,filename,revision,cache_dir,token,endpoint): pass
   fake_sdk.hf_hub_download=download
   with patch.object(cookbook_routes,'COOKBOOK_STATE_FILE',str(path)), patch.object(hub_discovery,'manifest',get_manifest), patch.object(cookbook_routes.shutil,'which',lambda name:'/bin/tmux'), patch('importlib.metadata.version',return_value='0.30.0'), patch.dict('sys.modules',{'huggingface_hub':fake_sdk}), patch.object(cookbook_routes.asyncio,'create_subprocess_exec',spawn):
    app.include_router(cookbook_routes.setup_cookbook_routes(provider_store=object(),provider_control=object()))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
     for kind in ('internal','api'):
      denied=await client.get('/api/cookbook/pinned',headers={'x-test-auth':kind}); self.assertEqual(denied.status_code,403)
     response=await client.post('/api/cookbook/pinned/prepare',json={'repoID':'org/model','revision':'a'*40,'files':['file'],'cacheRoot':str(root)})
     self.assertEqual(response.status_code,200,response.text); prepared=response.json(); op=prepared['operationID']
     response=await client.post(f'/api/cookbook/pinned/{op}/confirm',json={'revision':prepared['revision']}); self.assertEqual(response.status_code,200,response.text)
     response=await client.post(f'/api/cookbook/pinned/{op}/confirm',json={'revision':prepared['revision']}); self.assertEqual(response.status_code,200); self.assertEqual(len(calls),1)
     with transaction(path) as state: row=state[PRIVATE_KEY][op]; digest=row['digest']; cache=row['cache']
     directory=root/cache['name']; repo=directory/'models--org--model'; blob=repo/'blobs'/'blob'; blob.parent.mkdir(parents=True); blob.write_bytes(payload); snap=repo/'snapshots'/('a'*40); snap.mkdir(parents=True); (snap/'file').symlink_to('../../blobs/blob')
     receipt=run(path,op,digest,2,lambda _,fd:ChildSettled(True,[f'/proc/self/fd/{fd}/models--org--model/snapshots/{"a"*40}/file']))
     self.assertEqual(receipt['status'],'complete')
     status_response=await client.get(f'/api/cookbook/pinned/{op}'); self.assertEqual(status_response.headers['cache-control'],'private, no-store'); status=status_response.json(); self.assertEqual(status['receipt']['files'][0]['sha256'],sha)
     listing=(await client.get('/api/cookbook/pinned')).json(); self.assertEqual(listing['items'][0]['state'],'complete')
