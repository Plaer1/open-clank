"""Private, pinned ComfyUI workflow adapter. No registration or polling."""
from __future__ import annotations

import asyncio
import hashlib
import inspect
import io
import ipaddress
import json
import math
import uuid
from urllib.parse import urlsplit

import httpx
from PIL import Image
from src.openclank.media_adapters.base import Artifact, Submission, NotDispatched
from src.openclank.artifacts import media_artifact_owner

MAX_JSON = 1024 * 1024
MAX_PNG = 32 * 1024 * 1024


def _decode_recipe(raw):
    if not isinstance(raw, bytes) or not 0 < len(raw) <= MAX_JSON:
        raise ValueError('invalid approved workflow')
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result: raise ValueError('duplicate workflow key')
            result[key] = value
        return result
    recipe = json.loads(raw, object_pairs_hook=unique, parse_constant=lambda _: (_ for _ in ()).throw(ValueError('invalid workflow number')))
    return recipe


def _inspect_recipe(recipe):
    if (not isinstance(recipe, dict) or set(recipe) != {'version','workflow','slots'}
        or type(recipe['version']) is not int or recipe['version'] != 1
        or not isinstance(recipe['workflow'], dict) or not 0 < len(recipe['workflow']) <= 256
        or not isinstance(recipe['slots'], dict) or len(recipe['slots']) > 32):
        raise ValueError('invalid approved workflow')
    classes = set()
    for node_id, node in recipe['workflow'].items():
        if (not isinstance(node_id, str) or len(node_id) > 128 or not isinstance(node, dict)
            or not isinstance(node.get('class_type'), str) or not 0 < len(node['class_type']) <= 128
            or any(ord(c) < 32 for c in node['class_type']) or not isinstance(node.get('inputs'), dict)):
            raise ValueError('invalid approved workflow node')
        classes.add(node['class_type'])
    slots = []
    destinations = set()
    for name, slot in recipe['slots'].items():
        if (not isinstance(name, str) or not 0 < len(name) <= 128 or any(ord(c) < 32 for c in name)
            or not isinstance(slot, dict)
            or slot['type'] not in {'string','integer','number','boolean'}
            or not isinstance(slot['node'], str) or not isinstance(slot['input'], str)
            or slot['node'] not in recipe['workflow'] or slot['input'] not in recipe['workflow'][slot['node']]['inputs']):
            raise ValueError('invalid approved workflow slot')
        numeric = slot['type'] in {'integer','number'}
        if set(slot) != ({'node','input','type','min','max'} if numeric else {'node','input','type'}):
            raise ValueError('invalid approved workflow bounds')
        if numeric:
            expected = (int,) if slot['type'] == 'integer' else (int, float)
            if (type(slot['min']) not in expected or type(slot['max']) not in expected
                or not math.isfinite(slot['min']) or not math.isfinite(slot['max'])
                or abs(slot['min']) > 2**53-1 or abs(slot['max']) > 2**53-1 or slot['min'] > slot['max']):
                raise ValueError('invalid approved workflow bounds')
        target = (slot['node'], slot['input'])
        if target in destinations: raise ValueError('duplicate workflow slot')
        destinations.add(target)
        slots.append({'name':name, 'type':slot['type'], **({'min':slot['min'],'max':slot['max']} if numeric else {})})
    return recipe, {'node_classes': sorted(classes), 'slots': sorted(slots, key=lambda slot:slot['name'])}


def inspect_recipe(raw):
    return _inspect_recipe(_decode_recipe(raw))


def _render_recipe(recipe, summary, params):
    if not isinstance(params, dict) or set(params) != set(recipe['slots']):
        raise ValueError('invalid approved workflow parameters')
    graph = recipe['workflow']
    kinds = {'string':str, 'integer':int, 'number':float, 'boolean':bool}
    for key, slot in recipe['slots'].items():
        node, name, kind = slot['node'], slot['input'], kinds[slot['type']]
        value = params[key]
        if ((type(value) is not kind and not (slot['type'] == 'number' and type(value) is int)) or (isinstance(value, str) and len(value.encode('utf-8')) > 16384)
            or (type(value) is float and not math.isfinite(value)) or (type(value) is int and abs(value) > 2**53-1)
            or (slot['type'] in {'integer','number'} and not slot['min'] <= value <= slot['max'])):
            raise ValueError('invalid workflow parameter')
        graph[node]['inputs'][name] = value
    return graph, summary


def render_recipe(raw, params):
    recipe, summary = inspect_recipe(raw)
    return _render_recipe(recipe, summary, params)


def inspect_material_recipe(raw):
    recipe = _decode_recipe(raw)
    if (not isinstance(recipe, dict) or set(recipe) != {'version','workflow','slots','resolution','outputs'}
        or type(recipe['version']) is not int or recipe['version'] != 2
        or type(recipe['resolution']) is not int or recipe['resolution'] not in (256,512,1024,2048)
        or not isinstance(recipe['outputs'], dict) or set(recipe['outputs']) != {'baseColor','roughness','metallic'}):
        raise ValueError('invalid material workflow')
    outputs = recipe['outputs']
    seen = set()
    for selector in outputs.values():
        if (not isinstance(selector, dict) or set(selector) != {'node_id','index'}
            or not isinstance(selector['node_id'], str) or not selector['node_id']
            or len(selector['node_id'].encode('utf-8','strict')) > 128
            or any(ord(c) < 32 for c in selector['node_id']) or selector['node_id'] not in recipe['workflow']
            or type(selector['index']) is not int or not 0 <= selector['index'] <= 63):
            raise ValueError('invalid material workflow output')
        key = (selector['node_id'], selector['index'])
        if key in seen: raise ValueError('duplicate material workflow output')
        seen.add(key)
    base = {'version':1,'workflow':recipe['workflow'],'slots':recipe['slots']}
    _, summary = _inspect_recipe(base)
    return base, {**summary, 'resolution':recipe['resolution'], 'outputs':outputs}


def parse_material_recipe(raw, parameters):
    base, summary = inspect_material_recipe(raw)
    return _render_recipe(base, summary, parameters)


class ComfyUIAdapter:
    adapter_id = 'comfyui'
    version = '1'
    route_kind = 'remote'
    supports_lookup = False  # Caller UUID does not prove submission deduplication.
    supports_cancel = True

    def __init__(self, *, resolve_profile, capture_recipe, artifact_store, client_factory=httpx.AsyncClient, before_dispatch=None):
        self.resolve_profile = resolve_profile
        self.capture_recipe = capture_recipe
        self.artifact_store = artifact_store
        self.client_factory = client_factory
        self.before_dispatch = before_dispatch

    @staticmethod
    async def _owned_thread(function, *, retain_result_after_cancel=False):
        task = asyncio.create_task(asyncio.to_thread(function))
        cancelled = False
        while True:
            try:
                result = await asyncio.shield(task)
                break
            except asyncio.CancelledError:
                cancelled = True
                if task.done():
                    result = task.result()
                    break
        if cancelled and not retain_result_after_cancel: raise asyncio.CancelledError
        return result

    def _read_artifact(self, owner, artifact_id, expected_size):
        chunks = []
        size = 0
        for chunk in self.artifact_store.read_chunks(owner=owner, artifact_id=artifact_id):
            size += len(chunk)
            if size > expected_size or size > MAX_PNG: raise ValueError('retained media artifact exceeds bounds')
            chunks.append(chunk)
        if size != expected_size: raise ValueError('retained media artifact changed')
        return b''.join(chunks)

    def _profile(self, job, profile):
        binding = job['identity']['resource_binding']
        actual = profile.job_binding()
        if (any(actual.get(k) != binding[k] for k in ('ownerAccountID', 'connectionID', 'connectionRevision', 'interface'))
            or profile.credentialMode != 'none'):
            raise ValueError('media profile changed')
        origin = profile.origin
        parsed = urlsplit(origin if isinstance(origin, str) else '')
        try:
            loopback = parsed.hostname == 'localhost' or ipaddress.ip_address(parsed.hostname).is_loopback
        except ValueError:
            loopback = False
        if (not loopback or parsed.scheme not in {'http', 'https'} or not parsed.netloc
            or parsed.username is not None or parsed.password is not None
            or parsed.path not in {'', '/'} or parsed.query or parsed.fragment):
            raise ValueError('media profile origin unavailable')
        return origin.rstrip('/')

    async def _request(self, job, admission, method, path, *, body=None, params=None, limit=MAX_JSON):
        binding = job['identity']['resource_binding']
        async with self.resolve_profile(job['identity']) as (profile, assert_current):
            origin = self._profile(job, profile)
            async with self.client_factory(base_url=origin, follow_redirects=False, timeout=20.0, trust_env=False) as client:
                await assert_current()
                allowed = admission()
                if inspect.isawaitable(allowed): allowed = await allowed
                if allowed is False: raise ValueError('media dispatch admission expired')
                if self.before_dispatch is not None:
                    try:
                        if await self.before_dispatch() is not True: raise ValueError('media authority changed')
                    except Exception as exc:
                        if method == 'POST': raise NotDispatched('media authority changed') from exc
                        raise
                async with client.stream(method, path, json=body, params=params) as response:
                    if response.status_code != 200:
                        raise ValueError('media provider response unavailable')
                    chunks = []
                    size = 0
                    async for chunk in response.aiter_bytes():
                        size += len(chunk)
                        if size > limit:
                            raise ValueError('media provider response exceeds bounds')
                        chunks.append(chunk)
                    return b''.join(chunks)

    async def _json(self, *args, **kwargs):
        raw = await self._request(*args, **kwargs)
        result = json.loads(raw)
        if not isinstance(result, dict): raise ValueError('invalid media provider response')
        return result

    async def submit(self, request, job, admission):
        binding = job['identity']['resource_binding']
        try:
            evidence, raw = await self.capture_recipe(job['identity'])
            if any(evidence.get(k) != binding[k] for k in ('recipe_ref', 'recipe_revision', 'recipe_sha256')):
                raise ValueError('media recipe changed')
            graph, _summary = render_recipe(raw, request.get('parameters'))
        except Exception as exc:
            raise NotDispatched('media preparation unavailable or changed') from exc
        task_id = str(uuid.UUID(job['id']))
        answer = await self._json(job, admission, 'POST', '/prompt', body={'prompt': graph, 'prompt_id': task_id})
        if answer.get('prompt_id') != task_id: raise ValueError('media task identity mismatch')
        return Submission('queued', task_id)

    async def status(self, job, admission):
        task = str(uuid.UUID(job['remote_task_id']))
        answer = await self._json(job, admission, 'GET', '/api/jobs/' + task)
        if answer.get('id') != task: raise ValueError('media task identity mismatch')
        states = {'pending':'queued', 'in_progress':'running', 'completed':'collecting', 'failed':'failed', 'cancelled':'cancelled'}
        return Submission(states.get(answer.get('status'), 'unknown_outcome'), task)

    async def cancel(self, job, admission):
        task = str(uuid.UUID(job['remote_task_id']))
        await self._json(job, admission, 'POST', '/api/jobs/' + task + '/cancel')
        return 'requested'  # Acknowledgement does not prove process exit.

    async def collect(self, job, admission):
        task = str(uuid.UUID(job['remote_task_id']))
        answer = await self._json(job, admission, 'GET', '/api/jobs/' + task)
        output = answer.get('preview_output')
        if (answer.get('id') != task or answer.get('status') != 'completed'
            or answer.get('outputs_count') != 1 or not isinstance(output, dict)
            or output.get('mediaType') != 'images'):
            raise ValueError('media output unavailable')
        locator = {k: output.get(k) for k in ('filename', 'subfolder', 'type')}
        if (any(not isinstance(v, str) or len(v.encode('utf-8')) > 120 for v in locator.values())
            or locator['type'] != 'output' or locator['subfolder'] != ''
            or '/' in locator['filename'] or '\\' in locator['filename']
            or not locator['filename'].lower().endswith('.png')):
            raise ValueError('unsupported media output')
        data = await self._request(job, admission, 'GET', '/view', params=locator, limit=MAX_PNG)
        if not data.startswith(b'\x89PNG\r\n\x1a\n'): raise ValueError('media output is not PNG')
        with Image.open(io.BytesIO(data)) as image:
            if image.format != 'PNG' or image.width < 1 or image.height < 1 or image.width * image.height > 16_777_216:
                raise ValueError('media PNG exceeds supported bounds')
            image.verify()
        digest = hashlib.sha256(data).hexdigest()
        owner = media_artifact_owner(job['account_id'])
        def retain():
            row = self.artifact_store.put(owner=owner, chunks=[data], media_type='image/png')
            retained = self._read_artifact(owner, row.id, len(data))
            if len(retained) != len(data) or hashlib.sha256(retained).hexdigest() != digest:
                raise ValueError('retained media artifact changed')
            acknowledged = self.artifact_store.acknowledge(owner=owner, artifact_id=row.id)
            if acknowledged.content_sha256 != digest or acknowledged.size_bytes != len(data):
                raise ValueError('retained media artifact changed')
            return Artifact(row.id, digest, 'image/png', len(data))
        # This admission belongs to the custody task itself. If the caller
        # times out while the disk commit drains, account purge must still wait
        # for that owned task; no later write can follow a completed purge.
        async with self.resolve_profile(job['identity']) as (profile, assert_current):
            self._profile(job, profile)
            await assert_current()
            if self.before_dispatch is not None and await self.before_dispatch() is not True: raise ValueError('media authority changed')
            admission()
            return await self._owned_thread(retain, retain_result_after_cancel=True)

    async def fetch_artifact(self, identity, job, current):
        if not await current(): raise ValueError('media publication authority changed')
        owner = media_artifact_owner(identity['account_id'])
        def read():
            row = self.artifact_store.get(owner=owner, artifact_id=job['artifact']['artifact_id'])
            if (row.content_sha256 != job['artifact']['sha256'] or row.size_bytes != job['artifact']['size']
                or row.media_type != 'image/png' or row.state != 'acknowledged' or row.size_bytes > MAX_PNG):
                raise ValueError('retained media artifact changed')
            return self._read_artifact(owner, row.id, row.size_bytes)
        data = await self._owned_thread(read)
        if not await current(): raise ValueError('media publication authority changed')
        return data
