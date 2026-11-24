"""Private Blender adapter over the existing J0 and artifact authorities.

The app supplies verified profile, source and fixed-wrapper launcher callbacks,
all bound to the current account and Files authority.
"""
from __future__ import annotations

import asyncio
import hashlib
from src import bg_jobs
from src.openclank.artifacts import media_artifact_owner
from src.openclank.blender_child import artifact_authority, validate_glb
from src.openclank.media_adapters.base import Artifact, NotDispatched, Submission


def bind_child_profile(profile, input_sha256, *, adapter='blender-glb-png-v1'):
    """Join server-configured executable identity to the captured GLB digest."""
    expected = {'adapter','executable_sha256','executable_version','runner_sha256','artifact_authority'}
    if adapter not in {'blender-glb-png-v1','blender-glb-four-heading-v1','blender-glb-rig-clip-v1'} or not isinstance(profile, dict) or set(profile) != expected or profile.get('adapter') != adapter:
        raise ValueError('invalid Blender child profile')
    for key in ('executable_sha256','runner_sha256','artifact_authority'):
        bg_jobs._media_hash(profile[key])
    bg_jobs._media_text(profile['executable_version'], 'executable version', 128)
    return {**profile, 'input_sha256':bg_jobs._media_hash(input_sha256)}


class BlenderAdapter:
    adapter_id = 'blender-glb-png-v1'
    version = '1'
    route_kind = 'local'
    supports_lookup = True
    supports_cancel = False  # No unscoped PID signal or descendant-exit claim.

    def __init__(self, *, profile_for, capture_source, launch_fixed, artifact_store):
        self.profile_for = profile_for
        self.capture_source = capture_source
        self.launch_fixed = launch_fixed
        self.artifact_store = artifact_store

    async def submit(self, request, job, admission):
        try:
            profile = await self.profile_for(job['identity'])
            source = await self.capture_source(job['identity'])
            digest = validate_glb(source)
            if (digest not in job['identity']['input_hashes']
                    or digest != (job['identity'].get('resource_binding') or {}).get('source_sha256')):
                raise ValueError('source changed')
            profile = bind_child_profile(profile, digest, adapter=self.adapter_id)
            if profile['artifact_authority'] != artifact_authority(self.artifact_store):
                raise ValueError('artifact authority changed')
            admission()
            child = await asyncio.to_thread(bg_jobs.reserve_media_local_child, job['id'],
                account_id=job['account_id'], token=job['submission_token'], profile=profile)
        except Exception as exc:
            raise NotDispatched('local render preparation unavailable') from exc
        # The launcher accepts only this fixed attachment and captured bytes.
        # An exception from here may follow spawn and must remain unknown.
        admission()
        await self.launch_fixed(child, source, admission)
        return Submission('running', child['child_id'])

    async def status(self, job, admission):
        admission()
        current = await asyncio.to_thread(bg_jobs.get_media, job['id'], account_id=job['account_id'])
        child = current.get('local_child') if current else None
        if not isinstance(child, dict): return Submission('unknown_outcome')
        if child.get('state') == 'launching': return Submission('unknown_outcome')
        if child.get('state') == 'running': return Submission('running', child.get('child_id'))
        if child.get('state') != 'terminal': return Submission('unknown_outcome')
        receipt = child.get('receipt') or {}
        if receipt.get('exit_code') != 0: return Submission('failed')
        if not await asyncio.to_thread(bg_jobs.adopt_media_local_artifact, job['id'], account_id=job['account_id'], child_id=child['child_id']):
            return Submission('unknown_outcome')
        return Submission('collecting', child['child_id'])

    async def cancel(self, job, admission):
        raise NotDispatched('exact local-child cancellation is unavailable')

    async def collect(self, job, admission):
        admission()
        current = await asyncio.to_thread(bg_jobs.get_media, job['id'], account_id=job['account_id'])
        child = current.get('local_child') if current else None
        receipt = child.get('receipt') if isinstance(child, dict) and child.get('state') == 'terminal' else None
        if not isinstance(receipt, dict) or receipt.get('exit_code') != 0:
            raise ValueError('render receipt unavailable')
        owner = media_artifact_owner(job['account_id'])
        data = b''.join(self.artifact_store.read_chunks(owner=owner, artifact_id=receipt['artifact_id']))
        if len(data) != receipt['output_size'] or hashlib.sha256(data).hexdigest() != receipt['output_sha256']:
            raise ValueError('retained render changed')
        admission()
        return Artifact(receipt['artifact_id'], receipt['output_sha256'], 'image/png', len(data))

    async def fetch_artifact(self, identity, job, current):
        if not await current(): raise ValueError('publication authority changed')
        artifact = job.get('artifact') or {}
        owner = media_artifact_owner(identity['account_id'])
        data = b''.join(self.artifact_store.read_chunks(owner=owner, artifact_id=artifact['artifact_id']))
        if len(data) != artifact['size'] or hashlib.sha256(data).hexdigest() != artifact['sha256']:
            raise ValueError('retained render changed')
        if not await current(): raise ValueError('publication authority changed')
        return data
