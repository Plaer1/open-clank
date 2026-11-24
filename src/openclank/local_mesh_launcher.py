"""Stage and detach the single owned TRELLIS job wrapper."""
import asyncio
import json
import os
from pathlib import Path
import re
import subprocess
import sys

from src import bg_jobs
from src.constants import DATA_DIR
from src.openclank.blender_child import artifact_authority


def _stage_and_launch(kind, child, source, *, profile, artifact_store, admission, before_dispatch, loop):
    if kind not in ("trellis", "hunyuan"): raise ValueError("unsupported local mesh child")
    if sys.platform != 'linux' or getattr(sys, 'frozen', False) or not callable(before_dispatch):
        raise ValueError('TRELLIS launcher unavailable')
    def current():
        if asyncio.run_coroutine_threadsafe(before_dispatch(), loop).result() is False:
            raise ValueError('TRELLIS caller changed')
    current()
    child_id=child['child_id']
    if not re.fullmatch(r'[0-9a-f]{32}',child_id) or artifact_authority(artifact_store)!=child['profile']['artifact_authority']:
        raise ValueError('TRELLIS child unavailable')
    session=artifact_store._session_factory()
    try:
        url=session.get_bind().url
        if url.get_backend_name()!='sqlite' or not url.database or url.database==':memory:': raise ValueError('artifact database unavailable')
        database=os.path.realpath(url.database)
    finally: session.close()
    base=bg_jobs._JOBS_DIR
    base.mkdir(parents=True,exist_ok=True,mode=0o700)
    if base.is_symlink(): raise ValueError('job directory unavailable')
    admission(); stage=base/(kind+'-'+child_id); stage.mkdir(mode=0o700)
    descriptor={'job_id':child['job_id'],'account_id':child['account_id'],'child_id':child_id,'nonce':child['nonce'],
        'profile':child['profile'],'runtime':profile.private_descriptor(),'artifact_root':str(artifact_store.root),'artifact_db':database}
    for name,data in [('input.png',source),('child.json',json.dumps(descriptor,sort_keys=True,separators=(',',':'),ensure_ascii=False).encode())]:
        fd=os.open(stage/name,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
        with os.fdopen(fd,'wb') as handle: handle.write(data); handle.flush(); os.fsync(handle.fileno())
    fd=os.open(stage,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
    try: os.fsync(fd)
    finally: os.close(fd)
    current(); admission()
    root=Path(__file__).resolve().parent.parent.parent
    env={'PATH':os.defpath,'OPEN_CLANK_DATA_DIR':str(DATA_DIR),'DATABASE_URL':'sqlite:///'+database}
    subprocess.Popen([sys.executable,'-m','src.openclank.'+kind+'_child',child_id],cwd=str(root),stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,env=env,start_new_session=True)


async def launch_fixed(kind, child, source, *, profile, artifact_store, admission, barrier, owner, before_dispatch):
    async with barrier.admitted(owner):
        task=asyncio.create_task(asyncio.to_thread(_stage_and_launch,kind,child,source,profile=profile,artifact_store=artifact_store,admission=admission,before_dispatch=before_dispatch,loop=asyncio.get_running_loop()))
        cancelled=False
        while True:
            try: result=await asyncio.shield(task); break
            except asyncio.CancelledError:
                cancelled=True
                if task.done(): result=task.result(); break
        if cancelled: raise asyncio.CancelledError
        return result
