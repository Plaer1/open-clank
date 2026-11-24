"""Fixed Hunyuan wrapper over existing local-mesh launch ownership."""
from src.openclank import local_mesh_launcher as _owner

async def launch_fixed(child, source, *, profile, artifact_store, admission, barrier, owner, before_dispatch):
    return await _owner.launch_fixed("hunyuan", child, source, profile=profile, artifact_store=artifact_store, admission=admission, barrier=barrier, owner=owner, before_dispatch=before_dispatch)
