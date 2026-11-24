"""Owned, fixed pdflatex child for the existing local document job.

The process is not a sandbox. Cancellation confirms only the original waited
compiler child; descendants are not claimed to be confined or settled.
"""
import asyncio
import hashlib
import os
import shutil
import signal
import stat
import sys
import tempfile
from pathlib import Path

from src import bg_jobs
from src.openclank.artifacts import account_artifact_owner
from src.openclank.blender_child import artifact_authority
from src.openclank.latex_contract import ADAPTER, MAX_PDF_BYTES, safe_relative

_RUNS = {}
_LOG_LIMIT = 256 * 1024
_TIMEOUT = 60


def compiler_readiness():
    """Read-only installed-binary check; version is deliberately unprobed."""
    if os.name != 'posix' or getattr(sys,'frozen',False) or not Path('/proc/self/fd').is_dir(): return None
    candidate = shutil.which('pdflatex')
    if not candidate: return None
    try:
        path = Path(candidate).resolve(strict=True)
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or not (before.st_mode & 0o111) or not 0 < before.st_size <= 64*1024*1024: raise ValueError()
        digest = hashlib.sha256()
        while True:
            block = os.read(fd,1024 * 1024)
            if not block: break
            digest.update(block)
        after = os.fstat(fd)
        if (before.st_dev,before.st_ino,before.st_size,before.st_mtime_ns) != (after.st_dev,after.st_ino,after.st_size,after.st_mtime_ns): raise ValueError()
        os.lseek(fd,0,os.SEEK_SET)
        return {'fd':fd,'executable_sha256':digest.hexdigest(),'executable_version':'unprobed',
                'identity':(before.st_dev,before.st_ino,before.st_size,before.st_mtime_ns)}
    except (OSError,ValueError):
        if 'fd' in locals(): os.close(fd)
        return None


def runner_sha256():
    return hashlib.sha256(Path(__file__).read_bytes() + Path(__file__).with_name('latex_exec.py').read_bytes()).hexdigest()


async def launch(child, inputs, compiler, *, store, scope_current, barrier, owner):
    """Claim exactly once and retain the task/handle before exposing running."""
    key=(child['job_id'],child['account_id'],child['child_id'],child['nonce'])
    if key in _RUNS: raise ValueError('LaTeX child already owned')
    try:
        if (child.get('profile',{}).get('executable_sha256') != compiler['executable_sha256']
            or child.get('profile',{}).get('runner_sha256') != runner_sha256()
            or child.get('profile',{}).get('artifact_authority') != artifact_authority(store)
            or not await scope_current()): raise ValueError('LaTeX authority changed')
        claimed=await asyncio.to_thread(bg_jobs.claim_media_local_child,child['job_id'],account_id=child['account_id'],child_id=child['child_id'],nonce=child['nonce'])
        if claimed != {**child,'state':'running'}: raise ValueError('LaTeX child claim changed')
    except BaseException:
        os.close(compiler['fd']); raise
    root=Path(tempfile.mkdtemp(prefix='openclank-latex-'))
    process=None
    try:
        if len(inputs) != len(child['profile']['input_hashes']): raise ValueError('LaTeX inputs changed')
        for index, (name,data) in enumerate(inputs):
            if (not isinstance(data,bytes) or hashlib.sha256(data).hexdigest() != child['profile']['input_hashes'][index]
                or (index == 0 and name != 'main.tex')): raise ValueError('LaTeX input changed')
            if index: safe_relative(name)
            target=root/name; target.parent.mkdir(parents=True,exist_ok=True)
            with target.open('xb') as output: output.write(data)
            os.chmod(target,0o400)
        fd=compiler['fd']; info=os.fstat(fd)
        if (info.st_dev,info.st_ino,info.st_size,info.st_mtime_ns) != compiler['identity'] or not await scope_current(): raise ValueError('LaTeX compiler changed')
        # Explicit program and format are necessary when executing via /proc/self/fd.
        launcher=Path(__file__).with_name('latex_exec.py')
        process=await asyncio.create_subprocess_exec(sys.executable,'-I','-u',str(launcher),str(fd),
            pass_fds=(fd,),cwd=str(root),stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.STDOUT,env={'PATH':os.defpath},start_new_session=True)
        entry={'key':key,'process':process,'root':root,'child':child,'lock':asyncio.Lock(),'cancel_requested':False,'done':None}
        _RUNS[key]=entry
        async def owned_settle():
            try:
                async with barrier.admitted(owner): return await _settle(entry,store,scope_current)
            except BaseException:
                # Admission can fail after spawn. Retain the original handle,
                # stop and wait it; without custody authority the outcome stays unknown.
                if process.returncode is None:
                    try: os.killpg(process.pid,signal.SIGKILL)
                    except ProcessLookupError: pass
                try: await asyncio.shield(process.wait())
                except BaseException: pass
                async def drain():
                    while await process.stdout.read(65536): pass
                try: await asyncio.wait_for(drain(),3)
                except BaseException: pass
                shutil.rmtree(root,ignore_errors=True)
                return 'unknown'
        entry['done']=asyncio.create_task(owned_settle())
        def forget(task):
            _RUNS.pop(key,None)
            if not task.cancelled(): task.exception()
        entry['done'].add_done_callback(forget)
        return child['child_id']
    except BaseException:
        if process is not None and process.returncode is None:
            try: os.killpg(process.pid,signal.SIGKILL)
            except ProcessLookupError: pass
            await process.wait()
        shutil.rmtree(root,ignore_errors=True)
        await asyncio.to_thread(bg_jobs.finish_media_local_child,child['job_id'],account_id=child['account_id'],child_id=child['child_id'],nonce=child['nonce'],receipt={'exit_code':1,'output_sha256':None,'output_size':0})
        raise
    finally:
        os.close(compiler['fd'])


async def _settle(entry,store,scope_current):
    child,process,root=entry['child'],entry['process'],entry['root']; logs=bytearray(); timed_out=False
    async def drain():
        while True:
            chunk=await process.stdout.read(65536)
            if not chunk: return
            if len(logs)<_LOG_LIMIT: logs.extend(chunk[:_LOG_LIMIT-len(logs)])
    reader=asyncio.create_task(drain())
    try:
        try: code=await asyncio.wait_for(process.wait(),_TIMEOUT)
        except asyncio.TimeoutError:
            timed_out=True
            if process.returncode is None:
                try: os.killpg(process.pid,signal.SIGKILL)
                except ProcessLookupError: pass
            code=await process.wait()
        await asyncio.wait_for(reader,3)
        async with entry['lock']:
            cancelled=entry['cancel_requested'] and code in (-signal.SIGTERM,-signal.SIGKILL)
            receipt={'exit_code':code if -255 <= code <= 255 and code else 1,'output_sha256':None,'output_size':0}
            if code == 0 and not entry['cancel_requested'] and not timed_out and await scope_current():
                fd=os.open(root/'output.pdf',os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
                try:
                    info=os.fstat(fd)
                    if not stat.S_ISREG(info.st_mode) or not 5 < info.st_size <= MAX_PDF_BYTES: raise ValueError('PDF output unavailable')
                    with os.fdopen(fd,'rb',closefd=False) as handle: data=handle.read(MAX_PDF_BYTES+1)
                    after=os.fstat(fd)
                    if (info.st_dev,info.st_ino,info.st_size,info.st_mtime_ns)!=(after.st_dev,after.st_ino,after.st_size,after.st_mtime_ns) or len(data)!=info.st_size or not data.startswith(b'%PDF-'): raise ValueError('PDF output changed')
                finally: os.close(fd)
                if not await scope_current() or artifact_authority(store)!=child['profile']['artifact_authority']: raise ValueError('PDF authority changed')
                digest=hashlib.sha256(data).hexdigest(); owner=account_artifact_owner(child['account_id'])
                row=await asyncio.to_thread(store.put,owner=owner,chunks=[data],media_type='application/pdf')
                retained=await asyncio.to_thread(lambda:b''.join(store.read_chunks(owner=owner,artifact_id=row.id)))
                if retained!=data or row.content_sha256!=digest or not await scope_current(): raise ValueError('PDF custody changed')
                row=await asyncio.to_thread(store.acknowledge,owner=owner,artifact_id=row.id)
                receipt={'exit_code':0,'output_sha256':digest,'output_size':len(data),'artifact_id':row.id}
            settled=await asyncio.to_thread(bg_jobs.finish_media_local_child,child['job_id'],account_id=child['account_id'],child_id=child['child_id'],nonce=child['nonce'],receipt=receipt)
            return 'cancelled' if settled and cancelled else 'settled' if settled else 'unknown'
    except BaseException:
        # The owned original is stopped and waited; no receipt is fabricated if capture did not settle.
        if process.returncode is None:
            try: os.killpg(process.pid,signal.SIGKILL)
            except ProcessLookupError: pass
            try: await asyncio.shield(process.wait())
            except BaseException: pass
        return 'unknown'
    finally:
        if not reader.done(): reader.cancel()
        shutil.rmtree(root,ignore_errors=True)


def owns(child):
    key=(child['job_id'],child['account_id'],child['child_id'],child['nonce']);entry=_RUNS.get(key)
    return bool(entry and entry.get('done') and not entry['done'].done())


async def cancel(child):
    key=(child['job_id'],child['account_id'],child['child_id'],child['nonce']); entry=_RUNS.get(key)
    if not entry: return 'unknown'
    async with entry['lock']:
        if entry['done'].done() or entry['process'].returncode is not None: return 'unknown'
        entry['cancel_requested']=True
        try: os.killpg(entry['process'].pid,signal.SIGTERM)
        except ProcessLookupError: return 'unknown'
    try: result=await asyncio.wait_for(asyncio.shield(entry['done']),5)
    except asyncio.TimeoutError: return 'unknown'
    return 'confirmed' if result == 'cancelled' else 'unknown'
