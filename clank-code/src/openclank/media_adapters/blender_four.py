"""Versioned four-heading Blender operation using the existing local child."""
import asyncio
import hashlib
from dataclasses import dataclass
from src import bg_jobs
from src.openclank.artifacts import media_artifact_owner
from src.openclank.media_adapters.base import Artifact, CollectionProfile
from src.openclank.media_adapters.blender import BlenderAdapter

@dataclass(frozen=True)
class FourHeadingArtifact(Artifact):
    layout: dict

class BlenderFourAdapter(BlenderAdapter):
    collection_profile = CollectionProfile(artifact_type=FourHeadingArtifact)
    adapter_id = 'blender-glb-four-heading-v1'

    async def collect(self, job, admission):
        admission()
        current=await asyncio.to_thread(bg_jobs.get_media,job['id'],account_id=job['account_id'])
        child=current.get('local_child') if current else None
        receipt=child.get('receipt') if isinstance(child,dict) and child.get('state')=='terminal' else None
        if not isinstance(receipt,dict) or receipt.get('exit_code')!=0 or not isinstance(receipt.get('layout'),dict):
            raise ValueError('render receipt unavailable')
        owner=media_artifact_owner(job['account_id'])
        data=b''.join(self.artifact_store.read_chunks(owner=owner,artifact_id=receipt['artifact_id']))
        if len(data)!=receipt['output_size'] or hashlib.sha256(data).hexdigest()!=receipt['output_sha256']:
            raise ValueError('retained render changed')
        admission()
        return FourHeadingArtifact(receipt['artifact_id'],receipt['output_sha256'],'image/png',len(data),dict(receipt['layout']))
