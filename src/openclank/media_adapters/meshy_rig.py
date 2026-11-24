"""One rigging request for an owned, captured GLB through the Meshy API."""
import asyncio
import base64
import hashlib
import json
import math
from urllib.parse import quote

from src.openclank.media_adapters.base import NotDispatched, Submission
from src.openclank.media_adapters.meshy import MAX_GLB, MeshyAdapter
from src.openclank.owned_mesh_rig import inspect_textured_rig_source, inspect_textured_rig_result


def rig_task_path(task):
    if (not isinstance(task, str) or not task or task in ('.','..') or len(task.encode('utf-8')) > 512
        or any(ord(c) < 32 or ord(c) == 127 or c in '/\\' for c in task)):
        raise ValueError('Meshy task unavailable')
    return '/openapi/v1/rigging/' + quote(task, safe='')


class MeshyRigAdapter(MeshyAdapter):
    adapter_id = 'meshy-rig-v1'

    def __init__(self, *, source, source_current, before_dispatch=None, **kwargs):
        super().__init__(**kwargs)
        if not callable(source) or not callable(source_current): raise ValueError('rig source unavailable')
        self.source, self.source_current, self.before_dispatch = source, source_current, before_dispatch

    async def _caller_current(self, identity):
        if await self.source_current(identity) is not True: raise ValueError('rig source changed')
        if self.before_dispatch is not None and await self.before_dispatch() is not True:
            raise ValueError('rig caller changed')

    async def submit(self, request, job, admission):
        try:
            if (type(request) is not dict or set(request) != {'parameters'} or type(request['parameters']) is not dict
                or set(request['parameters']) != {'height_meters'}): raise ValueError('invalid rig request')
            height = request['parameters']['height_meters']
            if type(height) not in (int,float) or not math.isfinite(height) or not 0 < height <= 10:
                raise ValueError('invalid rig height')
            identity=job['identity']; binding=identity['resource_binding']
            evidence,data=await self.source(identity)
            if (not isinstance(data,bytes) or not 0 < len(data) <= MAX_GLB
                or evidence.get('recipe_ref') != binding['source_ref']
                or evidence.get('recipe_revision') != binding['source_revision']
                or evidence.get('recipe_sha256') != binding['source_sha256']
                or identity['input_hashes'] != [binding['source_sha256']]):
                raise ValueError('rig source changed')
            def prepare():
                result=inspect_textured_rig_source(data)
                if result['sha256'] != binding['source_sha256'] or hashlib.sha256(data).hexdigest() != binding['source_sha256']:
                    raise ValueError('rig source changed')
                url='data:model/gltf-binary;base64,' + base64.b64encode(data).decode('ascii')
                return json.dumps({'model_url':url,'height_meters':height},separators=(',',':'),allow_nan=False).encode('utf-8')
            worker=asyncio.create_task(asyncio.to_thread(prepare))
            cancelled=False
            while True:
                try: raw_body=await asyncio.shield(worker); break
                except asyncio.CancelledError:
                    cancelled=True
                    if worker.done(): raw_body=worker.result(); break
            if cancelled: raise asyncio.CancelledError
            await self._caller_current(identity)
        except Exception as exc:
            raise NotDispatched('rig source unavailable or changed') from exc
        result=await self._request(job,admission,'POST','/openapi/v1/rigging',
            raw_body=raw_body,
            before_dispatch=lambda:self._caller_current(identity))
        task=result.get('result'); rig_task_path(task)
        return Submission('queued',task)

    async def _task(self, job, admission):
        task=job.get('remote_task_id')
        value=await self._request(job,admission,'GET',rig_task_path(task))
        if value.get('id') != task or value.get('type') != 'rig': raise ValueError('Meshy task changed')
        return value

    def _result_url(self, value):
        result=value.get('result')
        if value.get('status') != 'SUCCEEDED' or not isinstance(result,dict) or not isinstance(result.get('rigged_character_glb_url'),str):
            raise ValueError('Meshy rig unavailable')
        return result['rigged_character_glb_url']

    async def collect(self, job, admission):
        await self._caller_current(job['identity'])
        return await super().collect(job, admission)

    async def _fetch_current(self, job):
        await self._caller_current(job['identity'])

    async def _retention_current(self, job):
        await self._caller_current(job['identity'])

    async def _validate_output(self, data):
        task=asyncio.create_task(asyncio.to_thread(inspect_textured_rig_result,data))
        while True:
            try: return (await asyncio.shield(task))['sha256']
            except asyncio.CancelledError:
                if task.done(): return task.result()['sha256']
