"""Fixed Hunyuan wrapper over existing local-mesh custody."""
import subprocess
from src.openclank import local_mesh_child as _owner

def run(**kwargs): return _owner.run(kind="hunyuan", **kwargs)
def _worker_main(child_id): return _owner.worker_main("hunyuan", child_id)

if __name__ == "__main__":
    import sys
    if len(sys.argv) != 2: raise SystemExit(2)
    _worker_main(sys.argv[1])
