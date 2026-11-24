"""Bounded material bundle through the existing local-child custody owner."""
import asyncio
import hashlib

from src import bg_jobs
from src.openclank.artifacts import media_artifact_owner
from src.openclank.blender_child import artifact_authority
from src.openclank.material_bundle import pack, validate_bundle
from src.openclank.media_adapters.base import Artifact, NotDispatched, Submission
from src.openclank.media_adapters.blender import BlenderAdapter
from src.openclank.media_adapters.trim import _settled_thread


class MaterialPackAdapter(BlenderAdapter):
    adapter_id = 'material-pack-v1'

    def __init__(self, *, scope, barrier, owner_for_account, artifact_store, before_dispatch):
        if not callable(before_dispatch): raise ValueError('material caller authority unavailable')
        self.scope, self.barrier = scope, barrier
        self.owner_for_account, self.artifact_store = owner_for_account, artifact_store
        self.before_dispatch = before_dispatch

    async def _current(self, identity, admission):
        if not await self.scope.current(identity):
            raise ValueError('material source changed')
        await self.before_dispatch()
        admission()

    async def submit(self, request, job, admission):
        identity = job['identity']; binding = identity['resource_binding']
        try:
            maps = await self.scope.source(identity)
            await self._current(identity, admission)
            child = await _settled_thread(bg_jobs.reserve_media_local_child, job['id'], account_id=job['account_id'],
                token=job['submission_token'], profile={'adapter':self.adapter_id,'input_hashes':identity['input_hashes'],
                    'manifest_sha256':binding['manifest_sha256'],'artifact_authority':artifact_authority(self.artifact_store)})
        except Exception as exc:
            raise NotDispatched('material source unavailable') from exc

        async def owned():
            async with self.barrier.admitted(self.owner_for_account(job['account_id'])):
                await self._current(identity, admission)
                claimed = await asyncio.to_thread(bg_jobs.claim_media_local_child, job['id'], account_id=job['account_id'],
                    child_id=child['child_id'], nonce=child['nonce'])
                try:
                    def compose():
                        data, manifest = pack(maps)
                        validate_bundle(data, expected_manifest_sha256=manifest, expected_input_hashes=identity['input_hashes'])
                        return data, manifest
                    work = asyncio.create_task(asyncio.to_thread(compose))
                    while True:
                        try:
                            data, manifest = await asyncio.shield(work); break
                        except asyncio.CancelledError:
                            if work.done(): data, manifest = work.result(); break
                    if manifest != binding['manifest_sha256']: raise ValueError('material manifest changed')
                    await self._current(identity, admission)
                    digest = hashlib.sha256(data).hexdigest()
                except BaseException:
                    settled = await asyncio.shield(asyncio.to_thread(bg_jobs.finish_media_local_child, job['id'],
                        account_id=job['account_id'], child_id=child['child_id'], nonce=child['nonce'],
                        receipt={'exit_code':1,'output_sha256':None,'output_size':0}))
                    if not settled: raise ValueError('material failure receipt unavailable')
                    return False
                def retain():
                    admission()
                    current = bg_jobs.get_media(job['id'], account_id=job['account_id'])
                    if not current or current.get('local_child') != claimed or artifact_authority(self.artifact_store) != claimed['profile']['artifact_authority']:
                        raise ValueError('material authority changed')
                    owner = media_artifact_owner(job['account_id'])
                    row = self.artifact_store.put(owner=owner, chunks=[data], media_type='application/zip')
                    retained = b''.join(self.artifact_store.read_chunks(owner=owner, artifact_id=row.id))
                    if len(retained) != len(data) or hashlib.sha256(retained).hexdigest() != digest:
                        raise ValueError('material artifact changed')
                    validate_bundle(retained, expected_manifest_sha256=binding['manifest_sha256'], expected_input_hashes=identity['input_hashes'])
                    row = self.artifact_store.acknowledge(owner=owner, artifact_id=row.id)
                    if row.content_sha256 != digest or row.size_bytes != len(data): raise ValueError('material artifact changed')
                    return bg_jobs.finish_media_local_child(job['id'], account_id=job['account_id'], child_id=child['child_id'],
                        nonce=child['nonce'], receipt={'exit_code':0,'output_sha256':digest,'output_size':len(data),'artifact_id':row.id})
                work = asyncio.create_task(asyncio.to_thread(retain))
                while True:
                    try: saved = await asyncio.shield(work); break
                    except asyncio.CancelledError:
                        if work.done(): saved = work.result(); break
                if not saved: raise ValueError('material receipt unavailable')
                return True
        task = asyncio.create_task(owned()); cancelled = False
        task.add_done_callback(lambda done: None if done.cancelled() else done.exception())
        while True:
            try: completed = await asyncio.shield(task); break
            except asyncio.CancelledError:
                cancelled = True
                if task.done(): completed = task.result(); break
        if cancelled: raise asyncio.CancelledError
        if not completed: return Submission('failed', child['child_id'])
        return Submission('running', child['child_id'])

    async def collect(self, job, admission):
        admission()
        current = await asyncio.to_thread(bg_jobs.get_media, job['id'], account_id=job['account_id'])
        child = current.get('local_child') if current else None
        receipt = child.get('receipt') if isinstance(child,dict) and child.get('state') == 'terminal' else None
        if not isinstance(receipt,dict) or receipt.get('exit_code') != 0: raise ValueError('material receipt unavailable')
        data = b''.join(self.artifact_store.read_chunks(owner=media_artifact_owner(job['account_id']), artifact_id=receipt['artifact_id']))
        if len(data) != receipt['output_size'] or hashlib.sha256(data).hexdigest() != receipt['output_sha256']:
            raise ValueError('material artifact changed')
        identity = current['identity']; binding = identity['resource_binding']
        await _settled_thread(validate_bundle, data, expected_manifest_sha256=binding['manifest_sha256'], expected_input_hashes=identity['input_hashes'])
        admission()
        return Artifact(receipt['artifact_id'], receipt['output_sha256'], 'application/zip', len(data))
