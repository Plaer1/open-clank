"""One fixed fal FLUX schnell text-to-PNG profile."""
import asyncio, hashlib, io, re
from urllib.parse import urlsplit
from PIL import Image
from src import bg_jobs
from src.openclank.artifacts import media_artifact_owner
from src.openclank.media_adapters.base import Artifact, CollectionProfile, NotDispatched, Submission
from src.openclank.media_adapters.meshy import MeshyAdapter, fetch_asset

_ID=re.compile(r'^[A-Za-z0-9_-]{1,128}$')

def validate_request(request):
    if not isinstance(request,dict) or set(request)!={'parameters'} or not isinstance(request['parameters'],dict) or set(request['parameters'])!={'prompt'}:
        raise ValueError('invalid image request')
    prompt=request['parameters']['prompt']
    if not isinstance(prompt,str) or not prompt.strip() or len(prompt)>1024 or len(prompt.encode('utf-8'))>4096:
        raise ValueError('invalid image prompt')
    return {'prompt':prompt,'num_images':1,'output_format':'png','image_size':'square_hd','enable_safety_checker':True}

class FalAdapter(MeshyAdapter):
    adapter_id='fal-flux-schnell-v1'
    origin='https://queue.fal.run'
    collection_profile=CollectionProfile(control_read=True,owned_fetch=True)
    def __init__(self, *, resolve_profile, artifact_store, client_factory=None, asset_fetch=None, before_dispatch=None):
        kwargs={'resolve_profile':resolve_profile,'artifact_store':artifact_store}
        if client_factory is not None: kwargs['client_factory']=client_factory
        super().__init__(**kwargs);self.before_dispatch=before_dispatch
        self.asset_fetch=asset_fetch or self._asset
    @staticmethod
    def _asset(url, *, admission):
        parsed=urlsplit(url)
        if parsed.scheme!='https' or parsed.hostname not in {'v3.fal.media','v3b.fal.media'} or parsed.username or parsed.password or parsed.port not in (None,443):
            raise ValueError('fal asset unavailable')
        return fetch_asset(url,admission=admission,expected_host=parsed.hostname,limit=32*1024*1024)
    def _path(self,job):
        task=job.get('remote_task_id')
        if not isinstance(task,str) or not _ID.fullmatch(task): raise ValueError('fal task unavailable')
        return '/fal-ai/flux/requests/'+task
    async def _request(self,job,admission,method,path,*,body=None,before_dispatch=None):
        # The inherited transport is fixed-origin, bounded, no redirects/proxies.
        # fal authenticates with Key rather than Bearer.
        async with self.client_factory(base_url=self.origin,follow_redirects=False,timeout=20.0,trust_env=False,cookies=None) as client:
            async with self.resolve_profile(job['identity'],job=job) as (_,check,credential):
                claim=job.get('claim') or {};await check(job['id'],claim.get('token'),claim.get('action'))
                key=await credential(job['id'],claim.get('token'),claim.get('action'));admission()
                if before_dispatch is not None:
                    try: await before_dispatch()
                    except Exception as exc: raise NotDispatched('media authority changed') from exc
                headers={'Authorization':'Key '+key}
                if method=='POST': headers.update({'X-Fal-No-Retry':'1','x-app-fal-disable-fallback':'true'})
                is_status=method=='GET' and path==self._path(job)+'/status'
                allowed={200,201,202} if method=='POST' else ({200,202} if is_status else {200})
                async with client.stream(method,path,json=body,headers=headers) as response:
                    if response.status_code not in allowed:raise ValueError('fal response unavailable')
                    code=response.status_code;chunks=[];size=0
                    async for chunk in response.aiter_bytes():
                        size+=len(chunk)
                        if size>2*1024*1024:raise ValueError('fal response too large')
                        chunks.append(chunk)
        import json
        def unique(pairs):
            out={}
            for k,v in pairs:
                if k in out:raise ValueError('duplicate key')
                out[k]=v
            return out
        value=json.loads(b''.join(chunks),object_pairs_hook=unique,parse_constant=lambda _:(_ for _ in ()).throw(ValueError('invalid number')))
        if not isinstance(value,dict):raise ValueError('fal response unavailable')
        if is_status and code==202 and (value.get('request_id')!=job.get('remote_task_id') or value.get('status') not in {'IN_QUEUE','IN_PROGRESS'}):raise ValueError('fal status unavailable')
        return value
    async def submit(self,request,job,admission):
        try: body=validate_request(request)
        except (ValueError,UnicodeError) as exc:raise NotDispatched('invalid image request') from exc
        value=await self._request(job,admission,'POST','/fal-ai/flux/schnell',body=body,before_dispatch=self.before_dispatch)
        task=value.get('request_id')
        if not isinstance(task,str) or not _ID.fullmatch(task):raise ValueError('fal submission unknown')
        return Submission('queued',task)
    async def status(self,job,admission):
        value=await self._request(job,admission,'GET',self._path(job)+'/status')
        if value.get('request_id')!=job.get('remote_task_id'):return Submission('unknown_outcome')
        status=value.get('status')
        if status=='COMPLETED' and (value.get('error') or value.get('error_type')):return Submission('failed',job['remote_task_id'])
        if status=='COMPLETED':
            result=await self._request(job,admission,'GET',self._path(job))
            if result.get('error') or result.get('error_type'):return Submission('failed',job['remote_task_id'])
            if not isinstance(result.get('images'),list) or len(result['images'])!=1:return Submission('unknown_outcome',job['remote_task_id'])
            return Submission('collecting',job['remote_task_id'])
        return Submission({'IN_QUEUE':'queued','IN_PROGRESS':'running'}.get(status,'unknown_outcome'),job['remote_task_id'])
    async def collect(self,job,admission):
        result=await self._request(job,admission,'GET',self._path(job));images=result.get('images')
        if result.get('error') or result.get('error_type') or not isinstance(images,list) or len(images)!=1 or not isinstance(images[0],dict) or images[0].get('content_type')!='image/png' or not isinstance(images[0].get('url'),str):raise ValueError('fal output unavailable')
        fetch=asyncio.create_task(asyncio.to_thread(self.asset_fetch,images[0]['url'],admission=admission))
        while True:
            try:data=await asyncio.shield(fetch);break
            except asyncio.CancelledError:
                if fetch.done():data=fetch.result();break
        if len(data)>32*1024*1024:raise ValueError('fal PNG too large')
        with Image.open(io.BytesIO(data)) as image:
            if image.format!='PNG' or not 1<=image.width<=4096 or not 1<=image.height<=4096 or image.width*image.height>16777216:raise ValueError('fal PNG unsupported')
            image.verify()
        digest=hashlib.sha256(data).hexdigest();owner=media_artifact_owner(job['account_id']);claim=job.get('claim') or {}
        async with self.resolve_profile(job['identity'],job=job) as (_,check,_):
            await check(job['id'],claim.get('token'),claim.get('action'));admission()
            if self.before_dispatch is not None:await self.before_dispatch()
            def retain():
                admission();row=self.artifact_store.put(owner=owner,chunks=[data],media_type='image/png')
                retained=b''.join(self.artifact_store.read_chunks(owner=owner,artifact_id=row.id))
                if len(retained)!=len(data) or hashlib.sha256(retained).hexdigest()!=digest:raise ValueError('artifact changed')
                row=self.artifact_store.acknowledge(owner=owner,artifact_id=row.id)
                return Artifact(row.id,digest,'image/png',len(data))
            task=asyncio.create_task(asyncio.to_thread(retain))
            while True:
                try:return await asyncio.shield(task)
                except asyncio.CancelledError:
                    if task.done():return task.result()
