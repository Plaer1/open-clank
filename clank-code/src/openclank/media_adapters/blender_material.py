"""Fixed configured-Blender material bake using the existing local child."""
import asyncio
import hashlib

from src import bg_jobs
from src.openclank.artifacts import media_artifact_owner
from src.openclank.blender_child import artifact_authority
from src.openclank.blender_material_inspect import inspect_material_source
from src.openclank.material_bundle import validate_bundle
from src.openclank.media_adapters.base import Artifact, NotDispatched, Submission
from src.openclank.media_adapters.blender import BlenderAdapter
from src.openclank.media_adapters.trim import _settled_thread


class BlenderMaterialAdapter(BlenderAdapter):
    adapter_id = 'blender-material-bake-v1'

    async def submit(self, request, job, admission):
        binding = job['identity']['resource_binding']
        try:
            if request != {'parameters': {'resolution': binding['resolution']}}:
                raise ValueError('bake request changed')
            profile = await self.profile_for(job['identity'])
            source = await self.capture_source(job['identity'])
            digest = (await _settled_thread(inspect_material_source, source))['source_sha256']
            if digest != binding['source_sha256'] or job['identity']['input_hashes'] != [digest]:
                raise ValueError('bake source changed')
            expected = {'adapter','executable_sha256','executable_version','runner_sha256','artifact_authority'}
            if not isinstance(profile,dict) or set(profile)!=expected or profile['adapter']!=self.adapter_id or profile['artifact_authority'] != artifact_authority(self.artifact_store):
                raise ValueError('bake profile changed')
            profile = {**profile, 'input_sha256':digest, 'resolution':binding['resolution'], 'recipe':'blender-material-channels-v1'}
            admission()
            child = await _settled_thread(bg_jobs.reserve_media_local_child, job['id'], account_id=job['account_id'], token=job['submission_token'], profile=profile)
        except Exception as exc:
            raise NotDispatched('local bake preparation unavailable') from exc
        admission()
        await self.launch_fixed(child, source, admission)
        return Submission('running', child['child_id'])

    async def collect(self, job, admission):
        admission()
        current=await asyncio.to_thread(bg_jobs.get_media,job['id'],account_id=job['account_id'])
        child=current.get('local_child') if current else None
        receipt=child.get('receipt') if isinstance(child,dict) and child.get('state')=='terminal' else None
        if not isinstance(receipt,dict) or receipt.get('exit_code') != 0: raise ValueError('bake receipt unavailable')
        def read():
            data=b''.join(self.artifact_store.read_chunks(owner=media_artifact_owner(job['account_id']),artifact_id=receipt['artifact_id']))
            if len(data)!=receipt['output_size'] or hashlib.sha256(data).hexdigest()!=receipt['output_sha256']: raise ValueError('bake artifact changed')
            binding=current['identity']['resource_binding']
            validate_bundle(data,expected_source_sha256=binding['source_sha256'],expected_resolution=binding['resolution'])
            return len(data)
        size=await _settled_thread(read)
        admission()
        return Artifact(receipt['artifact_id'],receipt['output_sha256'],'application/zip',size)

    async def fetch_artifact(self, identity, job, current):
        if not await current(): raise ValueError('publication authority changed')
        artifact=job.get('artifact') or {}
        def read():
            data=b''.join(self.artifact_store.read_chunks(owner=media_artifact_owner(identity['account_id']),artifact_id=artifact['artifact_id']))
            if len(data)!=artifact['size'] or hashlib.sha256(data).hexdigest()!=artifact['sha256']: raise ValueError('bake artifact changed')
            binding=identity['resource_binding']
            validate_bundle(data,expected_source_sha256=binding['source_sha256'],expected_resolution=binding['resolution'])
            return data
        data=await _settled_thread(read)
        if not await current(): raise ValueError('publication authority changed')
        return data
