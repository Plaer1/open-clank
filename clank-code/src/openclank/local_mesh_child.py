"""Claim one admitted TRELLIS job and retain only a verified GLB."""
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile

from PIL import Image
from src import bg_jobs
from src.openclank.artifacts import media_artifact_owner
from src.openclank.blender_child import artifact_authority, validate_glb



def _verified(path, expected, size=None, executable=False):
    if os.path.realpath(path)!=path: raise ValueError('TRELLIS path changed')
    fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW)
    try:
        st=os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or (size is not None and st.st_size!=size) or (executable and not st.st_mode&0o111): raise ValueError('TRELLIS component unavailable')
        with os.fdopen(os.dup(fd),'rb') as stream: digest=hashlib.file_digest(stream,'sha256').hexdigest()
        now=os.fstat(fd)
        key=lambda v:(v.st_dev,v.st_ino,v.st_size,v.st_mtime_ns,v.st_ctime_ns)
        if digest!=expected or key(st)!=key(now) or key(st)!=key(os.lstat(path)): raise ValueError('TRELLIS component changed')
        return fd
    except BaseException:
        os.close(fd); raise


def _weight(path, size):
    if os.path.realpath(path)!=path: raise ValueError('TRELLIS path changed')
    fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW)
    try:
        st=os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_size!=size: raise ValueError('TRELLIS weight unavailable')
        return fd
    except BaseException:
        os.close(fd); raise


def run(*, kind, job_id, account_id, child_id, nonce, source, runtime, profile, artifact_store):
    if kind == "trellis":
        from src.openclank.trellis_runtime import declaration_sha256
        adapter="trellis-image3d-v1"
    elif kind == "hunyuan":
        from src.openclank.hunyuan_runtime import declaration_sha256
        adapter="hunyuan3d-v2-shape-v1"
    else: raise ValueError("unsupported local mesh child")
    if profile.get("adapter") != adapter or (kind == "hunyuan" and runtime.get("device") != profile.get("device")): raise ValueError("local mesh profile unavailable")
    child=bg_jobs.claim_media_local_child(job_id,account_id=account_id,child_id=child_id,nonce=nonce)
    if child['profile']!=profile or artifact_authority(artifact_store)!=profile['artifact_authority'] or declaration_sha256(runtime)!=profile['manifestSHA256'] or runtime['interpreter']['sha256']!=profile['interpreterSHA256'] or runtime['interpreter']['version']!=profile['interpreterVersion'] or hashlib.sha256(source).hexdigest()!=profile['input_sha256']:
        raise ValueError('TRELLIS child identity changed')
    issued=False
    try:
        if not 0<len(source)<=4*1024*1024: raise ValueError('TRELLIS input unavailable')
        with Image.open(io.BytesIO(source)) as image:
            if image.format!='PNG' or image.mode!='RGBA' or not 1<=image.width<=4096 or not 1<=image.height<=4096 or getattr(image,'is_animated',False): raise ValueError('TRELLIS input unavailable')
            image.load(); alpha=image.getchannel('A'); lo,hi=alpha.getextrema()
            if (kind=='trellis' and (lo==255 or hi<=204)) or (kind=='hunyuan' and (alpha.getbbox() is None or alpha.getbbox()[2]-alpha.getbbox()[0]<2 or alpha.getbbox()[3]-alpha.getbbox()[1]<2)): raise ValueError('local mesh alpha unavailable')
        root=runtime['installationRoot']; handles=[]; observed=[]; owned=None; process=None; waited=False
        try:
            interpreter=os.path.join(root,runtime['interpreter']['relativePath'])
            binary=_verified(interpreter,profile['interpreterSHA256'],executable=True); handles.append(binary); observed.append((interpreter,binary,os.fstat(binary)))
            components={}
            for item in runtime['manifest']['components']:
                path=os.path.join(root,item['relativePath']); fd=(_weight(path,item['size']) if item['role'].endswith('_weights') or item['role']=='weights' else _verified(path,item['sha256'],item['size'])); handles.append(fd); observed.append((path,fd,os.fstat(fd))); components[item['role']]={'path':path,'size':item['size'],'sha256':item['sha256']}
            recipe=str(Path(__file__).with_name(kind+'_recipe.py').resolve())
            recipe_fd=_verified(recipe,profile['recipeSHA256']); handles.append(recipe_fd); observed.append((recipe,recipe_fd,os.fstat(recipe_fd)))
            if not 0<os.fstat(recipe_fd).st_size<=128*1024: raise ValueError('TRELLIS recipe unavailable')
            if kind=='hunyuan':
                helper=str(Path(__file__).with_name('trellis_runner.py').resolve())
                helper_fd=_verified(helper,profile['helperSHA256']); handles.append(helper_fd); observed.append((helper,helper_fd,os.fstat(helper_fd)))
                if not 0<os.fstat(helper_fd).st_size<=128*1024: raise ValueError('local mesh helper unavailable')
            runner=Path(__file__).with_name(kind+'_runner.py').read_bytes()
            if not 0<len(runner)<=128*1024 or hashlib.sha256(runner).hexdigest()!=profile['runnerSHA256']: raise ValueError('TRELLIS runner changed')
            current=bg_jobs.get_media(job_id,account_id=account_id)
            if not current or current.get('local_child')!=child: raise ValueError('TRELLIS claim changed')
            owned=tempfile.mkdtemp(prefix='openclank-trellis-'); temp=Path(owned)
            inp=temp/'input.png'; out=temp/'output.glb'; script=temp/'runner.py'; desc=temp/'descriptor.json'
            inp.write_bytes(source); script.write_bytes(runner)
            desc.write_text(json.dumps({'version':1,'seed':profile['seed'],'input':str(inp),'output':str(out),'components':components,**({'device':profile['device']} if kind=='hunyuan' else {})},sort_keys=True,separators=(',',':')))
            for path in (inp,script,desc): os.chmod(path,0o400)
            repo=Path(__file__).resolve().parent.parent.parent
            issued=True
            process=subprocess.Popen([f'/proc/self/fd/{binary}','-B',str(script),'--',str(desc)],cwd=str(repo),env={'PATH':os.defpath,'PYTHONPATH':str(repo)},stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,pass_fds=(binary,),start_new_session=True)
            try: code=process.wait(timeout=600)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid,signal.SIGKILL); process.wait(); raise RuntimeError('TRELLIS descendant settlement unknown')
            # A reaped leader alone does not prove its original group is empty.
            try:
                os.killpg(process.pid, 0)
            except ProcessLookupError:
                pass
            else:
                raise RuntimeError('TRELLIS descendant settlement unknown')
            waited=True
            key=lambda v:(v.st_dev,v.st_ino,v.st_size,v.st_mtime_ns,v.st_ctime_ns)
            for path,fd,before in observed:
                if key(before)!=key(os.fstat(fd)) or key(before)!=key(os.lstat(path)): raise ValueError('TRELLIS component changed')
            if code!=0: return bg_jobs.finish_media_local_child(job_id,account_id=account_id,child_id=child_id,nonce=nonce,receipt={'exit_code':code,'output_sha256':None,'output_size':0})
            fd=os.open(out,os.O_RDONLY|os.O_NOFOLLOW)
            try:
                st=os.fstat(fd)
                if not stat.S_ISREG(st.st_mode) or not 0<st.st_size<=64*1024*1024: raise ValueError('TRELLIS output unavailable')
                with os.fdopen(os.dup(fd),'rb') as stream: data=stream.read(64*1024*1024+1)
                if len(data)!=st.st_size: raise ValueError('TRELLIS output changed')
            finally: os.close(fd)
            digest=validate_glb(data,textured=kind=="trellis")
            current=bg_jobs.get_media(job_id,account_id=account_id)
            if not current or current.get('local_child')!=child: raise ValueError('TRELLIS claim changed')
            owner=media_artifact_owner(account_id); artifact=artifact_store.put(owner=owner,chunks=[data],media_type='model/gltf-binary')
            retained=b''.join(artifact_store.read_chunks(owner=owner,artifact_id=artifact.id))
            if len(retained)!=len(data) or validate_glb(retained,textured=kind=="trellis")!=digest: raise ValueError('TRELLIS artifact changed')
            artifact=artifact_store.acknowledge(owner=owner,artifact_id=artifact.id)
            if artifact.content_sha256!=digest or artifact.size_bytes!=len(data): raise ValueError('TRELLIS receipt changed')
            return bg_jobs.finish_media_local_child(job_id,account_id=account_id,child_id=child_id,nonce=nonce,receipt={'exit_code':0,'output_sha256':digest,'output_size':len(data),'artifact_id':artifact.id})
        finally:
            for fd in handles: os.close(fd)
            if owned and (not issued or waited): shutil.rmtree(owned)
    except Exception:
        if issued:
            raise
        return bg_jobs.finish_media_local_child(job_id,account_id=account_id,child_id=child_id,nonce=nonce,receipt={'exit_code':1,'output_sha256':None,'output_size':0})


def worker_main(kind, child_id):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from src.openclank.artifacts import ArtifactStore
    if not re.fullmatch(r'[0-9a-f]{32}',child_id): raise ValueError('invalid child')
    stage=bg_jobs._JOBS_DIR/(kind+'-'+child_id)
    if stage.is_symlink() or not stage.is_dir(): raise ValueError('invalid stage')
    def read(name,limit):
        fd=os.open(stage/name,os.O_RDONLY|os.O_NOFOLLOW)
        try:
            with os.fdopen(os.dup(fd),'rb') as stream: data=stream.read(limit+1)
            if len(data)>limit: raise ValueError('stage exceeds bounds')
            return data
        finally: os.close(fd)
    d=json.loads(read('child.json',262144))
    if type(d) is not dict or set(d)!={'job_id','account_id','child_id','nonce','profile','runtime','artifact_root','artifact_db'} or d['child_id']!=child_id: raise ValueError('invalid descriptor')
    engine=create_engine('sqlite:///'+d['artifact_db'],connect_args={'check_same_thread':False})
    try:
        store=ArtifactStore(Path(d['artifact_root']),session_factory=sessionmaker(bind=engine))
        run(kind=kind,job_id=d['job_id'],account_id=d['account_id'],child_id=child_id,nonce=d['nonce'],source=read('input.png',4*1024*1024),runtime=d['runtime'],profile=d['profile'],artifact_store=store)
    finally: engine.dispose()

