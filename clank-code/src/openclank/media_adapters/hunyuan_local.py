"""Fixed local Hunyuan shape job using the existing J0 custody path."""
import hashlib
from src import bg_jobs
from src.openclank.artifacts import media_artifact_owner
from src.openclank.blender_child import validate_glb
from src.openclank.media_adapters.base import Artifact
from src.openclank.media_adapters.local_mesh import LocalMeshAdapter
from src.openclank.media_adapters.trim import _settled_thread


class HunyuanShapeAdapter(LocalMeshAdapter):
    adapter_id='hunyuan3d-v2-shape-v1'

    async def collect(self,job,admission):
        admission()
        current=await _settled_thread(bg_jobs.get_media,job['id'],account_id=job['account_id'])
        child=current.get('local_child') if current else None
        receipt=child.get('receipt') if isinstance(child,dict) and child.get('state')=='terminal' else None
        if not isinstance(receipt,dict) or receipt.get('exit_code')!=0: raise ValueError('Hunyuan receipt unavailable')
        def read():
            data=b''.join(self.artifact_store.read_chunks(owner=media_artifact_owner(job['account_id']),artifact_id=receipt['artifact_id']))
            if (not 0<len(data)<=64*1024*1024 or len(data)!=receipt['output_size']
                    or hashlib.sha256(data).hexdigest()!=receipt['output_sha256']): raise ValueError('Hunyuan artifact changed')
            validate_glb(data,textured=False)
            return len(data)
        size=await _settled_thread(read)
        admission()
        return Artifact(receipt['artifact_id'],receipt['output_sha256'],'model/gltf-binary',size)
