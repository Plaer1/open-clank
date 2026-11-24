"""Vendor-neutral base for local image-to-mesh jobs run through a fixed local child."""
import hashlib
from src import bg_jobs
from src.openclank.blender_child import artifact_authority
from src.openclank.media_adapters.base import NotDispatched,Submission
from src.openclank.media_adapters.blender import BlenderAdapter
from src.openclank.media_adapters.trim import _settled_thread
from src.openclank.trellis_human import validate_request


class LocalMeshAdapter(BlenderAdapter):
    async def submit(self,request,job,admission):
        try:
            seed=validate_request(request)
            profile=await self.profile_for(job['identity'])
            evidence,source=await self.capture_source(job['identity'])
            binding=job['identity']['resource_binding']
            if (evidence!=binding['image_source'] or job['identity']['input_hashes']!=[evidence['sha256']]
                    or hashlib.sha256(source).hexdigest()!=evidence['sha256']
                    or profile.get('artifact_authority')!=artifact_authority(self.artifact_store)):
                raise ValueError('TRELLIS source changed')
            child_profile={**profile,'input_sha256':evidence['sha256'],'seed':seed}
            admission()
            child=await _settled_thread(bg_jobs.reserve_media_local_child,job['id'],account_id=job['account_id'],
                token=job['submission_token'],profile=child_profile)
        except Exception as exc:
            raise NotDispatched('TRELLIS preparation unavailable') from exc
        admission()
        await self.launch_fixed(child,source,admission)
        return Submission('running',child['child_id'])
