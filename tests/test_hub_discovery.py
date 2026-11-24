"""One public Hub route fixture; no network or model acquisition."""
import json
import subprocess
import unittest
from unittest.mock import patch

import httpx
from fastapi import FastAPI
from src.auth_helpers import require_user
from routes.cookbook_routes import setup_cookbook_routes
from src.openclank import hub_discovery


class HubDiscoveryTest(unittest.IsolatedAsyncioTestCase):
    async def test_public_pinned_manifest_and_bounds(self):
        sha='a'*40; calls=[]; mode={'value':'ok'}
        def upstream(request):
            self.assertEqual(request.url.host,'huggingface.co')
            self.assertNotIn('authorization',request.headers); self.assertNotIn('cookie',request.headers)
            calls.append((request.url.path,dict(request.url.params)))
            if request.url.path == '/api/models':
                return httpx.Response(200,json=[{'id':'org/model','pipeline_tag':'text-generation','tags':[]}])
            if mode['value']=='oversize': return httpx.Response(200,content=b' '* (hub_discovery.MAX_BODY+1))
            if '/revision/' not in request.url.path: return httpx.Response(200,json={'id':'org/model','sha':sha})
            return httpx.Response(200,json={'id':'org/model','sha':'b'*40 if mode['value']=='stale' else sha,
                'gated':False,'cardData':{'license':'apache-2.0'},'siblings':[{'rfilename':f'file{i}.bin','size':i} for i in range(101)]})
        app=FastAPI(); app.include_router(setup_cookbook_routes(provider_store=object(),provider_control=object()))
        app.dependency_overrides[require_user]=lambda:'fixture-user'
        actual=httpx.AsyncClient
        async with actual(transport=httpx.ASGITransport(app=app),base_url='http://fixture') as client:
            with patch.object(hub_discovery.httpx,'AsyncClient',side_effect=lambda **kwargs:actual(transport=httpx.MockTransport(upstream),**kwargs)):
                result=await client.get('/api/cookbook/hf-latest',params={'search':'wanted','limit':1})
                self.assertEqual(result.json()['models'][0]['repo_id'],'org/model')
                self.assertEqual(calls[0][1]['search'],'wanted')
                result=await client.get('/api/cookbook/hf-public-manifest',params={'repo_id':'org/model'})
                body=result.json(); self.assertEqual(body['revision'],sha); self.assertEqual(body['coverage']['state'],'partial')
                self.assertEqual((len(body['files']),body['coverage']['omitted']),(100,1))
                self.assertIsNone(body['files'][0]['blobID'])
                self.assertEqual(calls[-1][0],f'/api/models/org/model/revision/{sha}')
                script = r'''
import fs from 'node:fs';
import {createHubDetailController} from './static/js/cookbookHubDiscovery.js';
const body=JSON.parse(fs.readFileSync(0,'utf8')); let owner='a', seen=[], release;
const controller=createHubDetailController({getOwner:()=>owner,isVisible:()=>true,render:x=>seen.push(x),
  fetchImpl:async url=>{if(!url.startsWith('/api/cookbook/hf-public-manifest?')) throw Error('unexpected request'); return {ok:true,json:async()=>body};}});
if (!await controller.select('org/model') || seen.at(-1)?.revision!==body.revision) throw Error('selection failed');
const stale=createHubDetailController({getOwner:()=>owner,isVisible:()=>true,render:x=>seen.push(x),
  fetchImpl:()=>new Promise(resolve=>{release=resolve;})});
const pending=stale.select('org/model'); owner='b'; stale.clear(); release({ok:true,json:async()=>body});
if (await pending || seen.at(-1)!==null) throw Error('stale selection rendered');
owner='a'; let requests=[];
const swapping=createHubDetailController({getOwner:()=>owner,isVisible:()=>true,render:x=>seen.push(x),
  fetchImpl:()=>new Promise(resolve=>requests.push(resolve))});
const first=swapping.select('org/old'), second=swapping.select('org/model');
requests[0]({ok:true,json:async()=>({...body,repoID:'org/old'})});
requests[1]({ok:true,json:async()=>body});
if (await first || !await second || seen.at(-1)?.repoID!=='org/model') throw Error('stale selection won');
'''
                checked=subprocess.run(['node','--input-type=module','-e',script],input=json.dumps(body),text=True,capture_output=True,timeout=10)
                self.assertEqual(checked.returncode,0,checked.stderr)
                mode['value']='stale'
                self.assertEqual((await client.get('/api/cookbook/hf-public-manifest',params={'repo_id':'org/model'})).status_code,503)
                mode['value']='oversize'
                self.assertEqual((await client.get('/api/cookbook/hf-public-manifest',params={'repo_id':'org/model'})).status_code,503)
        self.assertTrue(all(path.startswith('/api/models') for path,_ in calls))
