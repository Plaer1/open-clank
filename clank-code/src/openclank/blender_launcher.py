"""Fixed detached wrapper launch using the existing J0 child attachment."""
import asyncio
import json
import os
import re
import subprocess
import sys
from pathlib import Path

from src import bg_jobs
from src.constants import DATA_DIR
from src.openclank.blender_child import artifact_authority


def _stage_and_launch(child, source, *, profile, artifact_store, admission, before_dispatch, loop):
    if sys.platform != 'linux' or getattr(sys, 'frozen', False):
        raise ValueError('local render launcher unavailable')
    def check_caller():
        if before_dispatch is not None:
            if not callable(before_dispatch): raise ValueError('bake caller authority unavailable')
            if asyncio.run_coroutine_threadsafe(before_dispatch(), loop).result() is False:
                raise ValueError('bake caller authority unavailable')
    adapter = child['profile']['adapter']
    if adapter == 'blender-material-bake-v1' and not callable(before_dispatch):
        raise ValueError('bake caller authority unavailable')
    if adapter == 'blender-material-bake-v1' or (adapter in {'blender-glb-png-v1', 'blender-glb-four-heading-v1', 'blender-glb-rig-clip-v1'} and before_dispatch is not None):
        check_caller()
    child_id = child['child_id']
    if not re.fullmatch(r'[0-9a-f]{32}', child_id): raise ValueError('invalid child')
    if artifact_authority(artifact_store) != child['profile']['artifact_authority']:
        raise ValueError('artifact authority changed')
    session = artifact_store._session_factory()
    try:
        url = session.get_bind().url
        if url.get_backend_name() != 'sqlite' or not url.database or url.database == ':memory:':
            raise ValueError('artifact database unavailable')
        database = os.path.realpath(url.database)
    finally:
        session.close()
    base = bg_jobs._JOBS_DIR
    base.mkdir(parents=True, exist_ok=True, mode=0o700)
    if base.is_symlink(): raise ValueError('job directory unavailable')
    stage = base / ('blender-' + child_id)
    admission()
    stage.mkdir(mode=0o700)  # Existing stage is never adopted or overwritten.
    descriptor = {'job_id':child['job_id'], 'account_id':child['account_id'], 'child_id':child_id,
        'nonce':child['nonce'], 'profile':child['profile'], 'executable_path':profile.executable_path,
        'artifact_root':str(artifact_store.root), 'artifact_db':database}
    for name, data in [('input.glb',source), ('child.json',json.dumps(descriptor,sort_keys=True,separators=(',',':')).encode())]:
        fd = os.open(stage/name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'wb') as handle:
            handle.write(data); handle.flush(); os.fsync(handle.fileno())
    directory = os.open(stage, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try: os.fsync(directory)
    finally: os.close(directory)
    if adapter == 'blender-material-bake-v1' or (adapter in {'blender-glb-png-v1', 'blender-glb-four-heading-v1', 'blender-glb-rig-clip-v1'} and before_dispatch is not None): check_caller()
    admission()
    root = Path(__file__).resolve().parent.parent.parent
    env = {'PATH':os.defpath, 'OPEN_CLANK_DATA_DIR':str(DATA_DIR),
           'DATABASE_URL':'sqlite:///' + database}
    subprocess.Popen([sys.executable, '-m', 'src.openclank.blender_child', child_id], cwd=str(root),
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        env=env, start_new_session=True)


async def launch_fixed(child, source, *, profile, artifact_store, admission, barrier, owner, before_dispatch=None):
    # The task that owns the actual spawn holds its own existing account
    # admission even when its caller times out or is cancelled.
    async with barrier.admitted(owner):
        task = asyncio.create_task(asyncio.to_thread(_stage_and_launch, child, source,
            profile=profile, artifact_store=artifact_store, admission=admission, before_dispatch=before_dispatch, loop=asyncio.get_running_loop()))
        cancelled = False
        while True:
            try:
                result = await asyncio.shield(task)
                break
            except asyncio.CancelledError:
                cancelled = True
                if task.done():
                    result = task.result()
                    break
        if cancelled:
            raise asyncio.CancelledError
        return result
