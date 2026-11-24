"""One explicit Meshy text-to-3D preview using existing job custody."""
from __future__ import annotations

import asyncio
import hashlib
import http.client
import json
import socket
import ssl
import threading
import time
from urllib.parse import quote, urlsplit

import httpx

from src import bg_jobs
from src.openclank.artifacts import media_artifact_owner
from src.openclank.blender_child import MAX_GLB, validate_glb
from src.openclank.media_adapters.base import Artifact, NotDispatched, Submission, CollectionProfile

ORIGIN = 'https://api.meshy.ai'
ASSET_HOST = 'assets.meshy.ai'
GLB_MIME = 'model/gltf-binary'
MAX_JSON = 256 * 1024
MAX_ASSET_URL = 8192


def validate_request(request):
    if not isinstance(request, dict) or set(request) != {'parameters'} or not isinstance(request['parameters'], dict):
        raise ValueError('invalid Meshy request')
    params = request['parameters']
    if set(params) != {'prompt', 'shouldRemesh'}:
        raise ValueError('invalid Meshy parameters')
    prompt, remesh = params['prompt'], params['shouldRemesh']
    if (not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 800
            or len(prompt.encode('utf-8')) > 3200 or type(remesh) is not bool):
        raise ValueError('invalid Meshy parameters')
    return {'mode':'preview', 'prompt':prompt, 'model_type':'standard', 'ai_model':'meshy-7.1',
            'geometry_resolution':'standard', 'should_remesh':remesh, 'target_formats':['glb']}


def task_path(task):
    if (not isinstance(task, str) or not task or task in ('.', '..') or len(task.encode('utf-8')) > 512
            or any(ord(c) < 32 or ord(c) == 127 or c in '/\\' for c in task)):
        raise ValueError('Meshy task unavailable')
    return '/openapi/v2/text-to-3d/' + quote(task, safe='')


def fetch_asset(url, *, admission, limit=MAX_GLB, total_timeout=20.0, read_timeout=5.0, expected_host=ASSET_HOST):
    """Read one fixed-origin asset without client URL logging or redirects."""
    try:
        if not isinstance(url, str) or len(url.encode('utf-8')) > MAX_ASSET_URL:
            raise ValueError()
        parsed = urlsplit(url)
        if (parsed.scheme != 'https' or parsed.hostname != expected_host or parsed.port not in (None, 443)
                or parsed.username is not None or parsed.password is not None or parsed.fragment
                or not parsed.path.startswith('/') or any(ord(c) < 32 or ord(c) == 127 for c in url)):
            raise ValueError()
        target = parsed.path + (('?' + parsed.query) if parsed.query else '')
        deadline = time.monotonic() + total_timeout
        connection = http.client.HTTPSConnection(expected_host, 443, timeout=min(read_timeout, total_timeout),
                                                   context=ssl.create_default_context())
        connection.set_debuglevel(0)
        timer = None
        try:
            connection.connect()
            sock=connection.sock
            remaining=deadline-time.monotonic()
            if sock is None or remaining<=0: raise ValueError()
            def interrupt():
                try: sock.shutdown(socket.SHUT_RDWR)
                except OSError: pass
            timer=threading.Timer(remaining,interrupt)
            timer.daemon=True
            timer.start()
            admission()
            connection.request('GET', target, headers={'Accept':'application/octet-stream', 'Connection':'close'})
            response = connection.getresponse()
            if response.status != 200:
                raise ValueError()
            length = response.getheader('Content-Length')
            if length is not None and (not length.isdecimal() or int(length) > limit):
                raise ValueError()
            chunks = []; total = 0
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ValueError()
                sock.settimeout(min(read_timeout, remaining))
                chunk = response.read1(min(65536, limit + 1 - total))
                if not chunk:
                    break
                total += len(chunk)
                if total > limit:
                    raise ValueError()
                chunks.append(chunk)
            return b''.join(chunks)
        finally:
            if timer is not None: timer.cancel()
            connection.close()
    except Exception:
        raise ValueError('Meshy asset unavailable') from None


class MeshyAdapter:
    collection_profile = CollectionProfile(control_read=True, owned_fetch=True)
    adapter_id = 'meshy-preview-v1'
    version = '1'
    route_kind = 'remote'
    supports_lookup = False
    supports_cancel = False
    origin = ORIGIN

    def __init__(self, *, resolve_profile, artifact_store, client_factory=httpx.AsyncClient, asset_fetch=fetch_asset):
        self.resolve_profile, self.artifact_store = resolve_profile, artifact_store
        self.client_factory, self.asset_fetch = client_factory, asset_fetch
        self.before_dispatch = None

    async def _request(self, job, admission, method, path, *, body=None, raw_body=None, before_dispatch=None):
        async with self.client_factory(base_url=self.origin, follow_redirects=False, timeout=20.0, trust_env=False, cookies=None) as client:
            async with self.resolve_profile(job['identity'], job=job) as (_profile, check, credential):
                claim = job.get('claim') or {}
                await check(job['id'], claim.get('token'), claim.get('action'))
                key = await credential(job['id'], claim.get('token'), claim.get('action'))
                admission()
                if before_dispatch is not None:
                    try: await before_dispatch()
                    except Exception as exc: raise NotDispatched('Meshy source changed') from exc
                if before_dispatch is not None:
                    await check(job['id'], claim.get('token'), claim.get('action'))
                    key = await credential(job['id'], claim.get('token'), claim.get('action'))
                    admission()
                headers={'Authorization':'Bearer ' + key}
                if raw_body is not None: headers['Content-Type']='application/json'
                async with client.stream(method, path, json=body, content=raw_body, headers=headers) as response:
                    if response.status_code not in ({200, 201, 202} if method == 'POST' else {200}):
                        raise ValueError('Meshy response unavailable')
                    chunks=[]; total=0
                    async for chunk in response.aiter_bytes():
                        total += len(chunk)
                        if total > MAX_JSON: raise ValueError('Meshy response exceeds bounds')
                        chunks.append(chunk)
        def unique(pairs):
            result={}
            for name, value in pairs:
                if name in result: raise ValueError('duplicate Meshy response key')
                result[name]=value
            return result
        value=json.loads(b''.join(chunks), object_pairs_hook=unique,
                         parse_constant=lambda _: (_ for _ in ()).throw(ValueError('invalid number')))
        if not isinstance(value, dict): raise ValueError('invalid Meshy response')
        return value

    async def submit(self, request, job, admission):
        try: body=validate_request(request)
        except (ValueError, UnicodeError) as exc: raise NotDispatched('invalid Meshy preview') from exc
        value=await self._request(job,admission,'POST','/openapi/v2/text-to-3d',body=body)
        task=value.get('result')
        task_path(task)
        return Submission('queued',task)

    async def _task(self, job, admission):
        task=job.get('remote_task_id')
        value=await self._request(job,admission,'GET',task_path(task))
        if value.get('id') != task or value.get('type') != 'text-to-3d-preview':
            raise ValueError('Meshy task changed')
        return value

    async def status(self, job, admission):
        try: value=await self._task(job,admission)
        except (ValueError, UnicodeError): return Submission('unknown_outcome')
        state={'PENDING':'queued','IN_PROGRESS':'running','SUCCEEDED':'collecting',
               'FAILED':'failed','CANCELED':'cancelled'}.get(value.get('status'),'unknown_outcome')
        return Submission(state,job.get('remote_task_id'))

    async def cancel(self, job, admission):
        raise NotDispatched('Meshy cancellation unavailable')

    def _result_url(self, value):
        urls=value.get('model_urls')
        if value.get('status') != 'SUCCEEDED' or not isinstance(urls,dict) or not isinstance(urls.get('glb'),str):
            raise ValueError('Meshy GLB unavailable')
        return urls['glb']

    async def _validate_output(self, data):
        if self.adapter_id != 'meshy-refine-v1': return validate_glb(data)
        validation=asyncio.create_task(asyncio.to_thread(validate_glb,data,textured=True))
        while True:
            try: return await asyncio.shield(validation)
            except asyncio.CancelledError:
                if validation.done(): return validation.result()

    async def _fetch_current(self, job):
        pass

    async def _retention_current(self, job):
        if self.before_dispatch is not None: await self.before_dispatch()

    async def collect(self, job, admission):
        value=await self._task(job,admission)
        url=self._result_url(value)
        current=await asyncio.to_thread(bg_jobs.get_media,job['id'],account_id=job['account_id'])
        claim=job.get('claim') or {}
        if (not current or current.get('identity')!=job.get('identity') or current.get('claim')!=claim
                or claim.get('action')!='collect' or current.get('remote_task_id')!=job.get('remote_task_id')):
            raise ValueError('Meshy collection claim changed')
        await self._fetch_current(job)
        admission()
        # Cancellation of an asyncio wrapper cannot stop DNS or the underlying
        # thread. Keep its actual task alive and wait for it to settle.
        fetch=asyncio.create_task(asyncio.to_thread(self.asset_fetch,url,admission=admission))
        while True:
            try:
                data=await asyncio.shield(fetch)
                break
            except asyncio.CancelledError:
                if fetch.done():
                    data=fetch.result()
                    break
        admission()
        digest=await self._validate_output(data)
        admission()
        owner=media_artifact_owner(job['account_id'])
        async with self.resolve_profile(job['identity'],job=job) as (_profile,check,_credential):
            await check(job['id'],claim.get('token'),claim.get('action')); admission()
            await self._retention_current(job)
            admission()
            def retain():
                admission()
                row=self.artifact_store.put(owner=owner,chunks=[data],media_type=GLB_MIME)
                retained=b''.join(self.artifact_store.read_chunks(owner=owner,artifact_id=row.id))
                if len(retained)!=len(data) or hashlib.sha256(retained).hexdigest()!=digest: raise ValueError('artifact changed')
                row=self.artifact_store.acknowledge(owner=owner,artifact_id=row.id)
                if row.content_sha256!=digest or row.size_bytes!=len(data): raise ValueError('artifact changed')
                return Artifact(row.id,digest,GLB_MIME,len(data))
            task=asyncio.create_task(asyncio.to_thread(retain))
            while True:
                try: return await asyncio.shield(task)
                except asyncio.CancelledError:
                    if task.done(): return task.result()

    async def fetch_artifact(self, identity, job, current):
        if not await current(): raise ValueError('publication authority changed')
        artifact=job['artifact']; owner=media_artifact_owner(identity['account_id'])
        data=await asyncio.to_thread(lambda:b''.join(self.artifact_store.read_chunks(owner=owner,artifact_id=artifact['artifact_id'])))
        if len(data)!=artifact['size'] or hashlib.sha256(data).hexdigest()!=artifact['sha256'] or not await current():
            raise ValueError('retained Meshy output changed')
        return data


class MeshyRefineAdapter(MeshyAdapter):
    """One separately admitted refine of an exact retained preview."""
    adapter_id = 'meshy-refine-v1'

    def __init__(self, *, source_current, **kwargs):
        super().__init__(**kwargs)
        self.source_current = source_current

    async def submit(self, request, job, admission):
        params = request.get('parameters') if isinstance(request,dict) and set(request)=={'parameters'} else None
        if not isinstance(params,dict) or set(params)!={'enablePBR'} or type(params['enablePBR']) is not bool:
            raise NotDispatched('invalid Meshy refine')
        binding=job['identity']['resource_binding']
        try:
            await self.source_current(job['identity'])
            value=await self._request(job,admission,'GET',task_path(binding['sourceTaskID']))
            await self.source_current(job['identity'])
            if value.get('id')!=binding['sourceTaskID'] or value.get('type')!='text-to-3d-preview' or value.get('status')!='SUCCEEDED':
                raise ValueError('preview unavailable')
        except Exception as exc:
            raise NotDispatched('Meshy preview unavailable') from exc
        body={'mode':'refine','preview_task_id':binding['sourceTaskID'],'enable_pbr':params['enablePBR'],'target_formats':['glb']}
        value=await self._request(job,admission,'POST','/openapi/v2/text-to-3d',body=body,
            before_dispatch=lambda:self.source_current(job['identity']))
        task=value.get('result');task_path(task)
        return Submission('queued',task)

    async def _task(self, job, admission):
        task=job.get('remote_task_id')
        value=await self._request(job,admission,'GET',task_path(task))
        if value.get('id')!=task or value.get('type')!='text-to-3d-refine': raise ValueError('Meshy task changed')
        return value
