"""Fixed selected-joint clip over the existing local child authority."""
import asyncio
from src import bg_jobs
from src.openclank.blender_child import artifact_authority
from src.openclank.blender_rig_inspect import inspect_rig
from src.openclank.media_adapters.base import NotDispatched, Submission
from src.openclank.media_adapters.blender import bind_child_profile
from src.openclank.media_adapters.blender_four import BlenderFourAdapter

class BlenderRigAdapter(BlenderFourAdapter):
    adapter_id='blender-glb-rig-clip-v1'
    async def submit(self,request,job,admission):
        try:
            profile=await self.profile_for(job['identity']); source=await self.capture_source(job['identity'])
            found=inspect_rig(source); rig=job['identity']['resource_binding']['rig']
            if found['sourceSHA256']!=rig['sourceSHA256'] or found['hierarchy']!=rig['hierarchy'] or rig['selected'] not in found['joints']:
                raise ValueError('rig source changed')
            profile=bind_child_profile(profile,found['sourceSHA256'],adapter=self.adapter_id); profile['rig']=rig
            if profile['artifact_authority']!=artifact_authority(self.artifact_store): raise ValueError('artifact authority changed')
            admission(); child=await asyncio.to_thread(bg_jobs.reserve_media_local_child,job['id'],account_id=job['account_id'],token=job['submission_token'],profile=profile)
        except Exception as exc:
            raise NotDispatched('rig preparation unavailable') from exc
        admission(); await self.launch_fixed(child,source,admission)
        return Submission('running',child['child_id'])
