"""One configured Voidless prompt-to-64px image job."""
import asyncio
import base64
import hashlib
import io
import json
import re
from contextlib import asynccontextmanager
from urllib.parse import quote

import httpx
from PIL import Image
from src import bg_jobs
from src.openclank.artifacts import media_artifact_owner
from src.openclank.media_adapters.base import Artifact, CollectionProfile, NotDispatched, Submission
from src.openclank.media_adapters.meshy import fetch_asset

ORIGIN='https://api.voidless.dev'
CDN='cdn.voidless.dev'
MAX_PNG=32*1024*1024
MAX_JSON=256*1024
_SAFE_SEGMENT=re.compile(r'[A-Za-z0-9_-][A-Za-z0-9._-]*\Z')


def _key(value):
    if type(value) is not str or len(value.encode('ascii','strict'))>350: raise ValueError('invalid image key')
    parts=value.split('/')
    if (len(parts)<3 or parts[0]!='assets' or any(not part or part in ('.','..') or not _SAFE_SEGMENT.fullmatch(part) for part in parts)
        or not parts[-1].lower().endswith('.png')): raise ValueError('invalid image key')
    return value


def encode_locator(key, asset_id):
    key=_key(key)
    if type(asset_id) is not int or not 0<asset_id<=9007199254740991: raise ValueError('invalid image locator')
    data=json.dumps([key,asset_id],ensure_ascii=True,separators=(',',':')).encode('ascii')
    token='v1.'+base64.urlsafe_b64encode(data).decode('ascii').rstrip('=')
    if len(token)>512: raise ValueError('invalid image locator')
    return token


def decode_locator(token):
    if type(token) is not str or not 0<len(token)<=512 or not re.fullmatch(r'v1\.[A-Za-z0-9_-]+',token): raise ValueError('invalid image locator')
    encoded=token[3:]
    try: value=json.loads(base64.b64decode(encoded+'='*((-len(encoded))%4),altchars=b'-_',validate=True))
    except Exception as exc: raise ValueError('invalid image locator') from exc
    if type(value) is not list or len(value)!=2 or encode_locator(value[0],value[1])!=token: raise ValueError('invalid image locator')
    return value[0],value[1]


def validate_request(request):
    if type(request) is not dict or set(request)!={'parameters'} or type(request['parameters']) is not dict or set(request['parameters'])!={'prompt'}:
        raise ValueError('invalid image request')
    prompt=request['parameters']['prompt']
    if type(prompt) is not str or not prompt.strip() or len(prompt)>800 or len(prompt.encode('utf-8','strict'))>3200:
        raise ValueError('invalid image request')
    return {'model':'flux2-klein','prompt':prompt,'colours':8,'dimension':'2d','size':64,'type':'object',
            'seed':0,'palette':[],'referenceImage':'','styleImage':''}


def _validate_png(data):
    if type(data) is not bytes or not 0<len(data)<=MAX_PNG: raise ValueError('image unavailable')
    with Image.open(io.BytesIO(data)) as image:
        if image.format!='PNG' or image.size!=(64,64) or getattr(image,'is_animated',False): raise ValueError('image unavailable')
        image.verify()
    return hashlib.sha256(data).hexdigest()


class VoidlessAdapter:
    adapter_id='voidless-image-v1'; version='1'; route_kind='remote'
    supports_lookup=False; supports_cancel=False
    collection_profile=CollectionProfile(control_read=True,owned_fetch=True)

    def __init__(self, *, resolve_profile, artifact_store, before_dispatch, scope_current, client_factory=httpx.AsyncClient, asset_fetch=fetch_asset):
        if not callable(before_dispatch) or not callable(scope_current): raise ValueError('image caller authority unavailable')
        self.resolve_profile,self.artifact_store,self.before_dispatch,self.scope_current=resolve_profile,artifact_store,before_dispatch,scope_current
        self.client_factory,self.asset_fetch=client_factory,asset_fetch

    async def _request(self, job, admission, path, body, *, issue=False):
        async with self.client_factory(base_url=ORIGIN,follow_redirects=False,timeout=20.0,trust_env=False,cookies=None) as client:
            async with self.resolve_profile(job['identity'],job=job) as (_profile,check,credential):
                claim=job.get('claim') or {}
                await check(job['id'],claim.get('token'),claim.get('action'))
                key=await credential(job['id'],claim.get('token'),claim.get('action'))
                if issue:
                    try:
                        if await self.before_dispatch() is not True: raise ValueError('caller changed')
                        await check(job['id'],claim.get('token'),claim.get('action'))
                        key=await credential(job['id'],claim.get('token'),claim.get('action'))
                        if await self.scope_current(job['identity']) is not True: raise ValueError('image scope changed')
                        if await self.before_dispatch() is not True: raise ValueError('caller changed')
                    except Exception as exc: raise NotDispatched('image authority changed') from exc
                admission()
                async with client.stream('POST',path,json=body,headers={'x-api-key':key}) as response:
                    if response.status_code != 200: raise ValueError('provider response unavailable')
                    chunks=[]; size=0
                    async for chunk in response.aiter_bytes():
                        size+=len(chunk)
                        if size>MAX_JSON: raise ValueError('provider response unavailable')
                        chunks.append(chunk)
        def unique(pairs):
            value={}
            for key,item in pairs:
                if key in value: raise ValueError('duplicate response')
                value[key]=item
            return value
        value=json.loads(b''.join(chunks),object_pairs_hook=unique,parse_constant=lambda _: (_ for _ in ()).throw(ValueError('invalid number')))
        if type(value) is not dict: raise ValueError('provider response unavailable')
        return value

    async def submit(self, request, job, admission):
        try: body=validate_request(request)
        except (ValueError,UnicodeError) as exc: raise NotDispatched('invalid image request') from exc
        value=await self._request(job,admission,'/api/generate/image',body,issue=True)
        return Submission('queued',encode_locator(value.get('file'),value.get('assetId')))

    async def _resolved(self, job, admission):
        key,asset_id=decode_locator(job.get('remote_task_id'))
        value=await self._request(job,admission,'/api/assets/resolve-image',{'image':key})
        expected='https://'+CDN+'/'+quote(key,safe='/')
        if value.get('key')!=key or type(value.get('assetId')) is not int or value['assetId']!=asset_id or value.get('url')!=expected:
            raise ValueError('image resolution unavailable')
        return expected

    async def status(self, job, admission):
        try: await self._resolved(job,admission)
        except (ValueError,UnicodeError): return Submission('unknown_outcome',job.get('remote_task_id'))
        return Submission('collecting',job['remote_task_id'])

    async def cancel(self, job, admission):
        raise NotDispatched('image cancellation unavailable')

    @asynccontextmanager
    async def _guard(self, job, admission):
        async with self.resolve_profile(job['identity'],job=job) as (_profile,check,credential):
            claim=job.get('claim') or {}
            if await self.before_dispatch() is not True: raise ValueError('image caller changed')
            await check(job['id'],claim.get('token'),claim.get('action'))
            await credential(job['id'],claim.get('token'),claim.get('action'))
            if await self.scope_current(job['identity']) is not True: raise ValueError('image scope changed')
            if await self.before_dispatch() is not True: raise ValueError('image caller changed')
            admission()
            yield

    @staticmethod
    async def _drained(function,*args,**kwargs):
        task=asyncio.create_task(asyncio.to_thread(function,*args,**kwargs)); cancelled=False
        while True:
            try: value=await asyncio.shield(task); break
            except asyncio.CancelledError:
                cancelled=True
                if task.done(): value=task.result(); break
        if cancelled: raise asyncio.CancelledError
        return value

    async def collect(self, job, admission):
        url=await self._resolved(job,admission)
        current=await asyncio.to_thread(bg_jobs.get_media,job['id'],account_id=job['account_id'])
        claim=job.get('claim') or {}
        if (not current or current.get('identity')!=job['identity'] or current.get('claim')!=claim
            or claim.get('action')!='collect' or current.get('remote_task_id')!=job.get('remote_task_id')):
            raise ValueError('image claim changed')
        async with self._guard(job,admission):
            data=await self._drained(self.asset_fetch,url,admission=admission,limit=MAX_PNG,expected_host=CDN)
        digest=await self._drained(_validate_png,data)
        owner=media_artifact_owner(job['account_id'])
        def retain():
            admission()
            row=self.artifact_store.put(owner=owner,chunks=[data],media_type='image/png')
            chunks=[]; total=0
            for chunk in self.artifact_store.read_chunks(owner=owner,artifact_id=row.id):
                total+=len(chunk)
                if total>len(data): raise ValueError('retained image changed')
                chunks.append(chunk)
            retained=b''.join(chunks)
            if total!=len(data) or _validate_png(retained)!=digest: raise ValueError('retained image changed')
            row=self.artifact_store.acknowledge(owner=owner,artifact_id=row.id)
            if row.content_sha256!=digest or row.size_bytes!=len(data): raise ValueError('retained image changed')
            return Artifact(row.id,digest,'image/png',len(data))
        async with self._guard(job,admission):
            return await self._drained(retain)

    async def fetch_artifact(self, identity, job, current):
        if await current() is not True: raise ValueError('publication authority changed')
        owner=media_artifact_owner(identity['account_id']); artifact=job['artifact']
        def read():
            row=self.artifact_store.get(owner=owner,artifact_id=artifact['artifact_id'])
            if row.state!='acknowledged' or row.media_type!='image/png' or row.content_sha256!=artifact['sha256'] or row.size_bytes!=artifact['size'] or row.size_bytes>MAX_PNG: raise ValueError('retained image changed')
            chunks=[]; size=0
            for chunk in self.artifact_store.read_chunks(owner=owner,artifact_id=row.id):
                size+=len(chunk)
                if size>row.size_bytes: raise ValueError('retained image changed')
                chunks.append(chunk)
            data=b''.join(chunks)
            if size!=row.size_bytes or _validate_png(data)!=row.content_sha256: raise ValueError('retained image changed')
            return data
        data=await self._drained(read)
        if await current() is not True: raise ValueError('publication authority changed')
        return data
