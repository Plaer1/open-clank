"""Finite Sprixen one-sprite adapter; credentials never enter responses."""
import asyncio
import hashlib
import io
import json
import re
from urllib.parse import urlsplit

import httpx
from PIL import Image
from src.openclank.artifacts import media_artifact_owner
from src import bg_jobs
from src.openclank.media_adapters.base import Artifact, NotDispatched, Submission, CollectionProfile

ORIGIN = 'https://api.sprixen.com'
MAX_JSON = 256 * 1024
MAX_PNG = 16 * 1024 * 1024
_ID = re.compile(r'gen_[A-Za-z0-9_-]{1,124}\Z')
_ASSET = re.compile(r'/api/public/uploads/generations/[A-Za-z0-9_-]+/[A-Za-z0-9_.-]+\.png\Z')


def validate_request(request):
    if not isinstance(request, dict) or set(request) != {'parameters'} or not isinstance(request['parameters'], dict):
        raise ValueError('invalid sprite request')
    params = request['parameters']
    if set(params) != {'prompt','resolution','pixelPerfect'}:
        raise ValueError('invalid sprite parameters')
    prompt, resolution, pixel = params['prompt'], params['resolution'], params['pixelPerfect']
    if not isinstance(prompt,str) or not 3 <= len(prompt) <= 6000 or len(prompt.encode('utf-8')) > 12000:
        raise ValueError('invalid sprite prompt')
    if resolution not in {'32x32','64x64','128x128','256x256','512x512'} or pixel not in {'auto','on','off'}:
        raise ValueError('unsupported sprite parameters')
    return {'prompt':prompt, 'type':'sprite', 'variantCount':1, 'resolution':resolution, 'pixelPerfect':pixel}


class SprixenAdapter:
    collection_profile = CollectionProfile(control_read=True)
    adapter_id = 'sprixen-sprite-v1'
    version = '1'
    route_kind = 'remote'
    supports_lookup = False  # No ID after a lost POST cannot be reconciled by listing.
    supports_cancel = False

    def __init__(self, *, resolve_profile, artifact_store, client_factory=httpx.AsyncClient):
        self.resolve_profile, self.artifact_store, self.client_factory = resolve_profile, artifact_store, client_factory

    async def _claim_current(self, job):
        current = await asyncio.to_thread(bg_jobs.get_media, job['id'], account_id=job['account_id'])
        claim = job.get('claim') or {}
        if (not current or current.get('identity') != job.get('identity')
                or current.get('claim') != claim or claim.get('action') != 'collect'
                or not job.get('remote_task_id') or current.get('remote_task_id') != job.get('remote_task_id')):
            raise ValueError('Sprixen result claim changed')

    async def _request(self, job, admission, method, path, *, body=None, authenticated=True, limit=MAX_JSON):
        async def receive(client, headers):
            admission()
            async with client.stream(method, path, json=body, headers=headers) as response:
                allowed = {200, 201, 202} if method == 'POST' else {200}
                if response.status_code not in allowed: raise ValueError('Sprixen response unavailable')
                chunks=[]; total=0
                async for chunk in response.aiter_bytes():
                    total += len(chunk)
                    if total > limit: raise ValueError('Sprixen response exceeds bounds')
                    chunks.append(chunk)
                return b''.join(chunks)
        async with self.client_factory(base_url=ORIGIN, follow_redirects=False, timeout=20.0, trust_env=False, cookies=None) as client:
            if not authenticated:
                await self._claim_current(job)
                return await receive(client, {})
            async with self.resolve_profile(job['identity'], job=job) as (_profile, check, credential):
                claim = job.get('claim') or {}
                await check(job['id'], claim.get('token'), claim.get('action'))
                key = await credential(job['id'], claim.get('token'), claim.get('action'))
                return await receive(client, {'Authorization':'Bearer ' + key})

    async def _json(self, *args, **kwargs):
        def unique(pairs):
            result={}
            for key,value in pairs:
                if key in result: raise ValueError('duplicate Sprixen response key')
                result[key]=value
            return result
        value=json.loads(await self._request(*args, **kwargs), object_pairs_hook=unique,
                         parse_constant=lambda _: (_ for _ in ()).throw(ValueError('invalid number')))
        if not isinstance(value, dict): raise ValueError('invalid Sprixen response')
        return value

    async def submit(self, request, job, admission):
        try: body=validate_request(request)
        except (ValueError, UnicodeError) as exc: raise NotDispatched('invalid sprite request') from exc
        value=await self._json(job,admission,'POST','/v1/generations',body=body)
        task=value.get('id')
        if not isinstance(task,str) or not _ID.fullmatch(task) or value.get('status') != 'pending':
            raise ValueError('Sprixen submission outcome unknown')
        return Submission('queued',task)

    async def status(self, job, admission):
        task=job.get('remote_task_id')
        if not isinstance(task,str) or not _ID.fullmatch(task): return Submission('unknown_outcome')
        value=await self._json(job,admission,'GET','/v1/generations/'+task)
        if value.get('id') != task: return Submission('unknown_outcome')
        state=value.get('status')
        return Submission({'pending':'queued','completed':'collecting','failed':'failed'}.get(state,'unknown_outcome'),task)

    async def cancel(self, job, admission):
        raise NotDispatched('Sprixen cancellation unavailable')

    async def collect(self, job, admission):
        task=job.get('remote_task_id')
        if not isinstance(task,str) or not _ID.fullmatch(task): raise ValueError('Sprixen task unavailable')
        value=await self._json(job,admission,'GET','/v1/generations/'+task)
        paths=value.get('resultUrls')
        if value.get('id') != task or value.get('status') != 'completed' or not isinstance(paths,list) or len(paths) != 1:
            raise ValueError('Sprixen result unavailable')
        path=paths[0]
        if not isinstance(path,str) or not _ASSET.fullmatch(path) or urlsplit(path).query or '..' in path.split('/'):
            raise ValueError('Sprixen asset path unsupported')
        data=await self._request(job,admission,'GET',path,authenticated=False,limit=MAX_PNG)
        with Image.open(io.BytesIO(data)) as image:
            if image.format != 'PNG' or not 1 <= image.width <= 4096 or not 1 <= image.height <= 4096 or image.width*image.height > 16777216:
                raise ValueError('Sprixen PNG unsupported')
            image.verify()
        digest=hashlib.sha256(data).hexdigest(); owner=media_artifact_owner(job['account_id'])
        async with self.resolve_profile(job['identity'], job=job) as (_profile, check, _credential):
            claim=job.get('claim') or {}
            await check(job['id'], claim.get('token'), claim.get('action')); admission()
            def retain():
                admission()
                row=self.artifact_store.put(owner=owner,chunks=[data],media_type='image/png')
                retained=b''.join(self.artifact_store.read_chunks(owner=owner,artifact_id=row.id))
                if len(retained)!=len(data) or hashlib.sha256(retained).hexdigest()!=digest: raise ValueError('artifact changed')
                row=self.artifact_store.acknowledge(owner=owner,artifact_id=row.id)
                if row.content_sha256!=digest or row.size_bytes!=len(data): raise ValueError('artifact changed')
                return Artifact(row.id,digest,'image/png',len(data))
            task=asyncio.create_task(asyncio.to_thread(retain)); cancelled=False
            while True:
                try: result=await asyncio.shield(task); break
                except asyncio.CancelledError:
                    cancelled=True
                    if task.done(): result=task.result(); break
            # Return a settled custody fact after cancellation so J0's late
            # result hook can retain it; no publication is initiated here.
            return result

    async def fetch_artifact(self, identity, job, current):
        if not await current(): raise ValueError('publication authority changed')
        artifact=job['artifact']; owner=media_artifact_owner(identity['account_id'])
        data=await asyncio.to_thread(lambda:b''.join(self.artifact_store.read_chunks(owner=owner,artifact_id=artifact['artifact_id'])))
        if len(data)!=artifact['size'] or hashlib.sha256(data).hexdigest()!=artifact['sha256'] or not await current():
            raise ValueError('retained sprite changed')
        return data
