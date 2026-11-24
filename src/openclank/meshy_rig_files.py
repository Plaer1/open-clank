"""Owned textured mesh and destination custody for Meshy rigging."""
import asyncio

from src.openclank.media_files import MeshyFilesScope


class MeshyRigFilesScope(MeshyFilesScope):
    async def _source(self, context, ref, revision=None):
        from src.openclank.owned_mesh_rig import inspect_textured_rig_source
        evidence, data = await self.facade._capture_media_source(context, resource_ref=ref,
            expected_revision=revision, limit=64 * 1024 * 1024)
        checked = await asyncio.to_thread(inspect_textured_rig_source, data)
        if checked['sha256'] != evidence['recipe_sha256']:
            raise ValueError('rig source changed')
        return evidence, data

    async def capture(self, identity, *, source_ref, destination_ref, destination_name):
        context = await self.context_for(identity)
        if (context.owner_subject_id != identity['account_id'] or context.workspace_id != identity['workspace_id']
                or context.policy_generation != identity['policy_generation'] or not await self.session_current(identity)):
            raise ValueError('media scope changed')
        async with self.resolve_profile(identity) as (profile, check, _credential):
            source, _ = await self._source(context, source_ref)
            destination = await self.facade.capture_media_destination(context, resource_ref=destination_ref)
            await check()
            if not await self.session_current(identity): raise ValueError('media session changed')
            binding = {**profile.job_binding(), 'source_ref':source['recipe_ref'],
                'source_revision':source['recipe_revision'], 'source_sha256':source['recipe_sha256'],
                'destination_ref':destination['destination_ref'], 'destination_revision':destination['destination_revision'],
                'destination_name':destination_name, 'project_ref':None, 'project_revision':None}
            return binding, {'source':source, 'folder':destination['destination_label']}

    async def source(self, identity):
        if not await self.current(identity): raise ValueError('media scope changed')
        binding = identity['resource_binding']
        evidence, data = await self._source(await self.context_for(identity), binding['source_ref'], binding['source_revision'])
        if evidence['recipe_sha256'] != binding['source_sha256'] or not await self.current(identity):
            raise ValueError('rig source changed')
        return evidence, data

    async def current(self, identity):
        try:
            context = await self.context_for(identity); binding = identity['resource_binding']
            if (context.owner_subject_id != identity['account_id'] or context.workspace_id != identity['workspace_id']
                    or context.policy_generation != identity['policy_generation'] or not await self.session_current(identity)):
                return False
            async with self.resolve_profile(identity) as (profile, check, _credential):
                if any(profile.job_binding().get(k) != binding.get(k) for k in profile.job_binding()): return False
                source, _ = await self._source(context, binding['source_ref'], binding['source_revision'])
                destination = await self.facade.capture_media_destination(context, resource_ref=binding['destination_ref'])
                await check()
                return (source['recipe_sha256'] == binding['source_sha256']
                    and destination['destination_revision'] == binding['destination_revision']
                    and await self.session_current(identity))
        except Exception:
            return False
