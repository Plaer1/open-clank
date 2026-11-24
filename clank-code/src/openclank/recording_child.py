"""One original recording child and its bounded private output custody."""
import asyncio
import hashlib
import json
import os
import signal
import stat
import sys
import tempfile
import time
import uuid
from pathlib import Path

from src import bg_jobs
from src.openclank.artifacts import account_artifact_owner
from src.openclank.blender_child import artifact_authority
from src.openclank.native_capture_session_linux import recording_start, recording_command, validate_recording_reply, observed_recording_start, observed_recording_command, validate_observed_recording_reply, trim_start, validate_trim_reply
from src.openclank.recording_contract import MAX_BYTES

_RUNS = {}


def readiness():
    if os.name != 'posix' or not Path('/proc/self/fd').is_dir(): return None
    if getattr(sys, 'frozen', False):
        from src.runtime_paths import get_app_root
        path = Path(get_app_root()) / 'bin' / 'openclank-native-capture-frame'
    else:
        path = Path(__file__).resolve().parents[2] / 'native/capture/target/release/openclank-native-capture-frame'
    fd = None
    try:
        path = path.resolve(strict=True)
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or not before.st_mode & 0o111 or not 0 < before.st_size <= MAX_BYTES:
            raise ValueError('recording helper unavailable')
        digest = hashlib.sha256()
        while True:
            block = os.read(fd, 1024 * 1024)
            if not block: break
            digest.update(block)
        after = os.fstat(fd)
        identity = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
        if identity != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns): raise ValueError('helper changed')
        return {'fd':fd, 'sha256':digest.hexdigest(), 'identity':identity}
    except (OSError, ValueError):
        if fd is not None: os.close(fd)
        return None


async def _profile_readiness(required):
    """Probe a held helper and retain its FD only for an advertised profile."""
    pending=asyncio.create_task(asyncio.to_thread(readiness));cancelled=False;helper=None;process=None
    try:
        while not pending.done():
            try: await asyncio.shield(pending)
            except asyncio.CancelledError: cancelled=True
        helper=pending.result()
        if cancelled: raise asyncio.CancelledError
        if not helper: return None
        spawn=asyncio.create_task(asyncio.create_subprocess_exec('/proc/self/fd/%d' % helper['fd'],'--capabilities',
            pass_fds=(helper['fd'],),stdin=asyncio.subprocess.DEVNULL,stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,start_new_session=True,limit=4097))
        while not spawn.done():
            try: await asyncio.shield(spawn)
            except asyncio.CancelledError: cancelled=True
        process=spawn.result()
        if cancelled: raise asyncio.CancelledError
        async def read_bounded():
            chunks=[];total=0
            while True:
                chunk=await process.stdout.read(4097-total)
                if not chunk: return b''.join(chunks)
                chunks.append(chunk);total+=len(chunk)
                if total>4096: return b''.join(chunks)
        try:
            data=await asyncio.wait_for(read_bounded(),2)
            code=await asyncio.wait_for(process.wait(),2)
        except asyncio.TimeoutError: return None
        if code!=0 or len(data)>4096 or not data.endswith(b'\n') or data.count(b'\n')!=1: return None
        value=json.loads(data)
        if (not isinstance(value,dict) or set(value)!={'version','profiles'} or type(value.get('version')) is not int
            or value['version']!=1 or not isinstance(value.get('profiles'),list) or len(value['profiles'])>16
            or any(not isinstance(item,str) or len(item)>64 for item in value['profiles'])
            or len(set(value['profiles']))!=len(value['profiles']) or required not in value['profiles']): return None
        def rehash():
            before=os.fstat(helper['fd']); os.lseek(helper['fd'],0,os.SEEK_SET); digest=hashlib.sha256()
            while True:
                block=os.read(helper['fd'],1024*1024)
                if not block: break
                digest.update(block)
            after=os.fstat(helper['fd'])
            identity=(after.st_dev,after.st_ino,after.st_size,after.st_mtime_ns)
            return ((before.st_dev,before.st_ino,before.st_size,before.st_mtime_ns)==identity==helper['identity']
                    and digest.hexdigest()==helper['sha256'])
        check=asyncio.create_task(asyncio.to_thread(rehash))
        while not check.done():
            try: await asyncio.shield(check)
            except asyncio.CancelledError: cancelled=True
        if cancelled: raise asyncio.CancelledError
        if not check.result(): return None
        accepted=helper;helper=None
        return accepted
    except (OSError,ValueError,TypeError,json.JSONDecodeError): return None
    finally:
        if process is not None and process.returncode is None:
            try: os.killpg(process.pid,signal.SIGKILL)
            except ProcessLookupError: pass
            waiter=asyncio.create_task(process.wait())
            while not waiter.done():
                try: await asyncio.shield(waiter)
                except asyncio.CancelledError: continue
        if helper is not None: os.close(helper['fd'])


async def trim_readiness():
    return await _profile_readiness('trim-v4')


async def control_recording_readiness():
    return await _profile_readiness('record-v8')


async def observed_recording_readiness():
    return await _profile_readiness('record-v5')


async def audio_recording_readiness():
    return await _profile_readiness('record-v6')


async def crop_recording_readiness():
    return await _profile_readiness('record-v7')


def runner_sha256():
    return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


def _key(child):
    return (child['job_id'], child['account_id'], child['child_id'], child['nonce'])


async def _command(entry, action):
    if entry.get('audio'):
        from src.openclank.recording_audio_contract import command
        value=command(action,entry['generation'])
        if entry.get('crop'): value['version']=7
    elif entry.get('control'):
        from src.openclank.native_capture_session_linux import control_recording_command
        value=control_recording_command(action,entry['generation'])
    else: value=(observed_recording_command if entry.get('observed') else recording_command)(action, entry['generation'])
    data = json.dumps(value, separators=(',', ':')).encode() + b'\n'
    process = entry['process']
    if process.returncode is not None or process.stdin is None: raise ValueError('recording child unavailable')
    process.stdin.write(data)
    await process.stdin.drain()


def _recorder_pidfd(process, identity_socket, helper_identity, launch_tick):
    """Receive the original child's self-opened pidfd on its inherited socket."""
    import array, socket
    if not hasattr(signal, 'pidfd_send_signal') or not hasattr(os, 'P_PIDFD'):
        raise ValueError('recorder process identity unavailable')
    identity_socket.settimeout(3)
    data, ancillary, flags, _ = identity_socket.recvmsg(1, socket.CMSG_SPACE(array.array('i').itemsize), socket.MSG_CMSG_CLOEXEC)
    fds=array.array('i')
    for level, kind, data_item in ancillary:
        if level == socket.SOL_SOCKET and kind == socket.SCM_RIGHTS:
            fds.frombytes(data_item[:len(data_item)-(len(data_item)%fds.itemsize)])
    if data != b'\x01' or flags & (socket.MSG_CTRUNC|socket.MSG_TRUNC) or len(fds)!=1:
        for fd in fds: os.close(fd)
        raise ValueError('recorder process identity unavailable')
    fd=fds[0]
    try:
        os.waitid(os.P_PIDFD, fd, os.WEXITED | os.WNOHANG | os.WNOWAIT)
        with open(f'/proc/self/fdinfo/{fd}',encoding='utf-8') as stream:
            fields=dict(line.rstrip().split(':',1) for line in stream if ':' in line)
        if int(fields.get('Pid','-1').strip()) != process.pid: raise ValueError('recorder process identity changed')
        exe=os.stat(f'/proc/{process.pid}/exe')
        with open(f'/proc/{process.pid}/stat',encoding='utf-8') as stream: fields=stream.read().rsplit(')',1)[1].split()
        if ((exe.st_dev,exe.st_ino) != tuple(helper_identity[:2]) or int(fields[19]) < launch_tick
                or int(fields[2]) != process.pid or process.returncode is not None): raise ValueError('recorder process identity changed')
        return fd
    except BaseException:
        os.close(fd); raise


class ControlRecordingGuard:
    """Signal only a held original-child pidfd, including a Stop/spawn race."""
    def __init__(self, loop, opener=_recorder_pidfd):
        import threading
        self.loop=loop; self.revoked=threading.Event(); self.pidfd=None; self.opener=opener
    def kill(self):
        fd=self.pidfd
        if fd is not None:
            try: signal.pidfd_send_signal(fd,signal.SIGKILL)
            except ProcessLookupError: pass
    def revoke(self):
        self.revoked.set()
        try: self.loop.call_soon_threadsafe(self.kill)
        except RuntimeError: pass
    def attach(self, process, identity_socket, helper_identity, launch_tick):
        self.pidfd=self.opener(process,identity_socket,helper_identity,launch_tick)
        if self.revoked.is_set(): self.kill(); return False
        return True
    def close(self):
        fd,self.pidfd=self.pidfd,None
        if fd is not None: os.close(fd)


def _stop_recorder(process, guard):
    if guard is not None:
        guard.kill()
        if guard.pidfd is None and process.stdin is not None: process.stdin.close()
    elif process.returncode is None:
        try: os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError: pass


async def launch(child, helper, *, store, scope_current, custody_current, barrier, owner, snapshot=None, before_spawn=None, fact_current=None, control_identity=None):
    key = _key(child)
    if key in _RUNS: raise ValueError('recording child already owned')
    profile = child['profile']; process = None; output = None; read_fd = write_fd = None
    stream_fd = None; control_revoked = None; guard = None; identity_host = identity_child = None
    preview_read = preview_write = None; crop = profile.get('recording_protocol') == 'record-v7'
    trim = profile.get('adapter') == 'trim-webm-v1'; snapshot_fd = snapshot.get('fd') if isinstance(snapshot,dict) else None
    try:
        if (helper['sha256'] != profile['helper_sha256'] or runner_sha256() != profile['runner_sha256']
            or artifact_authority(store) != profile['artifact_authority'] or not await scope_current()):
            raise ValueError('recording authority changed')
        claimed = await asyncio.to_thread(bg_jobs.claim_media_local_child, child['job_id'], account_id=child['account_id'], child_id=child['child_id'], nonce=child['nonce'])
        if claimed != {**child, 'state':'running'}: raise ValueError('recording child changed')
        output = tempfile.TemporaryFile(mode='w+b', buffering=0)
        os.fchmod(output.fileno(), 0o600)
        before = os.fstat(output.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 0: raise ValueError('recording spool unavailable')
        read_fd, write_fd = os.pipe()
        info = os.fstat(helper['fd'])
        if (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns) != helper['identity'] or not await scope_current():
            raise ValueError('recording helper changed')
        if control_identity is not None:
            from src.openclank.computer_control.hoststate import STATE
            guard=ControlRecordingGuard(asyncio.get_running_loop()); control_revoked=guard.revoked
            binding=control_identity['resource_binding']['control_stream']
            claim=STATE.claim_recording_stream(control_identity['owner'],control_identity['account_id'],control_identity['session_id'],binding,guard.revoke)
            if claim is None: raise ValueError('control stream unavailable')
            stream_fd=claim['fd']; stream_serial=claim['serial']
        if trim:
            import fcntl
            if type(snapshot_fd) is not int: raise ValueError('trim snapshot unavailable')
            source_stat=os.fstat(snapshot_fd)
            if (not stat.S_ISREG(source_stat.st_mode) or source_stat.st_nlink != 0
                or (source_stat.st_dev,source_stat.st_ino,source_stat.st_size)!=snapshot.get('identity')
                or fcntl.fcntl(snapshot_fd,fcntl.F_GETFL)&os.O_ACCMODE != os.O_RDONLY
                or snapshot.get('evidence',{}).get('source_sha256') != profile['input_sha256']):
                raise ValueError('trim snapshot changed')
            argv=('--trim-v4','--input-fd',str(snapshot_fd),'--output-fd',str(write_fd))
            inherited=(helper['fd'],write_fd,snapshot_fd)
        else:
            if snapshot is not None: raise ValueError('recording snapshot unexpected')
            argv=(('--record-v8' if control_identity is not None else '--record-v7' if crop else '--record-v6' if profile.get('recording_protocol') == 'record-v6' else '--record-v5' if profile.get('recording_protocol') == 'record-v5' else '--record-v3'),'--output-fd',str(write_fd)); inherited=(helper['fd'],write_fd)
            if control_identity is not None:
                if control_revoked.is_set() or not await scope_current(): raise ValueError('control stream changed')
                import socket
                identity_host,identity_child=socket.socketpair()
                argv += ('--remote-fd',str(stream_fd),'--serial',str(stream_serial),'--identity-fd',str(identity_child.fileno())); inherited += (stream_fd,identity_child.fileno())
            if crop:
                preview_read,preview_write=os.pipe()
                argv += ('--preview-fd',str(preview_write)); inherited += (preview_write,)
        if profile.get('recording_protocol') in ('record-v6','record-v7') and (not callable(before_spawn) or await before_spawn() is not True):
            raise ValueError('recording request authority changed')
        launch_tick = max(0, time.clock_gettime_ns(time.CLOCK_BOOTTIME) * os.sysconf('SC_CLK_TCK') // 1_000_000_000 - 1) if control_identity is not None else 0
        spawn=asyncio.create_task(asyncio.create_subprocess_exec('/proc/self/fd/%d' % helper['fd'], *argv,
            pass_fds=inherited, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL, start_new_session=True, limit=8193))
        cancelled=False
        while not spawn.done():
            try: await asyncio.shield(spawn)
            except asyncio.CancelledError: cancelled=True
        process=spawn.result()
        if control_identity is not None:
            identity_child.close(); identity_child=None
            if not await asyncio.to_thread(guard.attach,process,identity_host,helper['identity'],launch_tick) or not await scope_current(): raise ValueError('control stream changed')
            identity_host.close(); identity_host=None
        if cancelled: raise asyncio.CancelledError
        os.close(write_fd); write_fd = None
        if preview_write is not None: os.close(preview_write); preview_write=None
        entry = {'child':child, 'process':process, 'generation':uuid.uuid4().hex, 'lock':asyncio.Lock(),
                 'ready':asyncio.get_running_loop().create_future(), 'done':None, 'cancelled':False, 'trim':trim, 'observed':profile.get('recording_protocol') in ('record-v5','record-v8'), 'control':control_identity is not None, 'control_guard':guard, 'audio':profile.get('recording_protocol') in ('record-v6','record-v7'), 'crop':crop, 'preview_fd':preview_read, 'preview':None, 'choices':None, 'selection':None, 'fact_current':fact_current}
        _RUNS[key] = entry
        owned_fd, owned_output, owned_snapshot = read_fd, output, snapshot_fd
        initial_guard = [before_spawn] if profile.get('recording_protocol') in ('record-v6','record-v7') else []
        preview_read=None
        before_spawn = None
        async def owned():
            entered = False
            try:
                async with barrier.admitted(owner):
                    entered = True
                    return await _settle(entry, owned_fd, owned_output, store, scope_current, custody_current, initial_guard)
            finally:
                if not entered:
                    if entry.get('preview_fd') is not None: os.close(entry['preview_fd']); entry['preview_fd']=None
                    if not entry['ready'].done(): entry['ready'].set_exception(ValueError('recording admission unavailable'))
                    _stop_recorder(process,guard)
                    try: await asyncio.shield(process.wait())
                    except BaseException: pass
                    os.close(owned_fd); owned_output.close()
                if owned_snapshot is not None: os.close(owned_snapshot)
                _RUNS.pop(key, None)
                if guard is not None: guard.close()
        entry['done'] = asyncio.create_task(owned())
        entry['done'].add_done_callback(lambda task: None if task.cancelled() else task.exception())
        read_fd = None; output = None; snapshot_fd = None
        await asyncio.shield(entry['ready'])
        return child['child_id']
    except BaseException:
        if process is not None and key not in _RUNS:
            _stop_recorder(process,guard)
            wait_task=asyncio.create_task(process.wait())
            while not wait_task.done():
                try: await asyncio.shield(wait_task)
                except asyncio.CancelledError: continue
            wait_task.result()
        raise
    finally:
        os.close(helper['fd'])
        if read_fd is not None: os.close(read_fd)
        if write_fd is not None: os.close(write_fd)
        if preview_read is not None: os.close(preview_read)
        if preview_write is not None: os.close(preview_write)
        if output is not None: output.close()
        if snapshot_fd is not None: os.close(snapshot_fd)
        if stream_fd is not None: os.close(stream_fd)
        if identity_host is not None: identity_host.close()
        if identity_child is not None: identity_child.close()
        if guard is not None and key not in _RUNS: guard.close()


async def _settle(entry, read_fd, output, store, current, custody_current, initial_guard=None):
    process = entry['process']; child = entry['child']; total = 0; pipe_ok = True; capped = False; terminal = None
    async def pump():
        nonlocal total, pipe_ok, capped
        stream = os.fdopen(read_fd, 'rb', buffering=0)
        try:
            while True:
                data = await asyncio.to_thread(stream.read, 65536)
                if not data: break
                if total + len(data) > MAX_BYTES:
                    data = data[:MAX_BYTES-total]; capped = True; pipe_ok = False
                offset = 0
                while offset < len(data):
                    written = await asyncio.to_thread(output.write, data[offset:])
                    if not isinstance(written,int) or written <= 0: raise OSError('recording spool write failed')
                    offset += written; total += written
                if capped: break
        except (OSError, ValueError): pipe_ok = False
        finally: stream.close()
    pump_task = asyncio.create_task(pump())
    async def preview_pump():
        stream=os.fdopen(entry['preview_fd'],'rb',buffering=0); entry['preview_fd']=None; data=bytearray()
        try:
            while True:
                chunk=await asyncio.to_thread(stream.read,65536)
                if not chunk: return bytes(data)
                data.extend(chunk)
                if len(data)>2*1024*1024: raise ValueError('recording preview too large')
        finally: stream.close()
    preview_task=asyncio.create_task(preview_pump()) if entry.get('crop') else None
    async def watch_scope():
        while process.returncode is None:
            await asyncio.sleep(0.5)
            try: valid = await (custody_current() if entry['trim'] else current())
            except Exception: valid = False
            if not valid:
                entry['cancelled'] = True
                _stop_recorder(process,entry.get('control_guard'))
                return
    watcher = asyncio.create_task(watch_scope()); protocol_ok = False
    try:
        try:
            if not await current(): raise ValueError('recording scope changed')
            if entry.get('audio'):
                guard=initial_guard.pop() if initial_guard else None
                try:
                    if not callable(guard) or await guard() is not True: raise ValueError('recording request authority changed')
                finally: guard=None
            deadline = int(time.time()*1000) + 120000
            profile=child['profile']
            if entry['audio']:
                if entry.get('crop'):
                    from src.openclank.recording_crop_contract import start
                else:
                    from src.openclank.recording_audio_contract import start
                request=start(entry['generation'],deadline,profile['audio_intent'])
            else:
                request=(trim_start(entry['generation'],deadline,profile['start_ms'],profile['end_ms'])
                         if entry['trim'] else (observed_recording_start if entry['observed'] else recording_start)(entry['generation'],deadline))
            if entry.get('control'): request['version']=8
            process.stdin.write(json.dumps(request, separators=(',', ':')).encode()+b'\n')
            await process.stdin.drain()
            if not entry['ready'].done(): entry['ready'].set_result(None)
            while True:
                line = await asyncio.wait_for(process.stdout.readline(), max(0.01, (deadline-int(time.time()*1000))/1000))
                if not line.endswith(b'\n') or len(line)>8192: break
                if entry['audio']:
                    if entry.get('crop'):
                        from src.openclank.recording_crop_contract import validate_reply
                    else:
                        from src.openclank.recording_audio_contract import validate_reply
                    reply=validate_reply(json.loads(line),entry['generation'])
                else:
                    if entry.get('control'):
                        from src.openclank.native_capture_session_linux import validate_control_recording_reply
                        reply=validate_control_recording_reply(json.loads(line),entry['generation'])
                    else:
                        reply = (validate_trim_reply(json.loads(line),entry['generation'],profile['start_ms'],profile['end_ms'])
                             if entry['trim'] else (validate_observed_recording_reply if entry['observed'] else validate_recording_reply)(json.loads(line),entry['generation']))
                if entry['audio'] and reply.get('state') == 'choices':
                    if entry['choices'] is not None or entry['selection'] is not None or not await current(): raise ValueError('audio choices changed')
                    if not await asyncio.to_thread(bg_jobs.observe_recording_phase, child['job_id'], account_id=child['account_id'], child_id=child['child_id'], nonce=child['nonce'], phase='audio_choices'): raise ValueError('audio phase changed')
                    if entry.get('crop'):
                        from src.openclank.recording_crop_contract import validate_preview
                        preview=await asyncio.wait_for(asyncio.shield(preview_task),max(0.01,(deadline-int(time.time()*1000))/1000))
                        await asyncio.to_thread(validate_preview,reply['preview'],preview)
                        if not await current(): raise ValueError('recording preview changed')
                        entry['preview']={'metadata':reply['preview'],'bytes':preview}
                    entry['choices']=reply['choices']; continue
                if entry['audio'] and reply.get('state') == 'selection_accepted':
                    selected=entry['selection']
                    if not selected or any(reply.get(k)!=selected[k] for k in ('selection_id','selection_digest','selected_kinds')) or (entry.get('crop') and reply.get('crop') != selected.get('crop')): raise ValueError('audio selection changed')
                    if not await asyncio.to_thread(bg_jobs.finish_recording_audio_selection, child['job_id'], account_id=child['account_id'], child_id=child['child_id'], nonce=child['nonce'], selection_id=selected['selection_id'], digest=selected['selection_digest'], generation=entry['generation'], selected_kinds=selected['selected_kinds'], accepted=True, fact_current=entry['fact_current'], crop=selected.get('crop'), source_basis=selected.get('source_basis'), choice_handles=selected.get('choice_handles')): raise ValueError('audio acknowledgement unavailable')
                    selected['accepted']=True;entry['choices']=None;entry['preview']=None;continue
                if entry['audio'] and reply.get('state') in ('starting','recording'):
                    selected=entry['selection']
                    if not selected or not selected.get('accepted') or reply.get('audio') != ('selected' if selected['selected_kinds'] else 'none'): raise ValueError('audio phase changed')
                if reply.get('state') in ('starting','recording'):
                    if not await (custody_current() if entry['trim'] else current()): raise ValueError('child scope changed')
                    if not entry['trim'] and not await asyncio.to_thread(bg_jobs.observe_recording_phase, child['job_id'], account_id=child['account_id'], child_id=child['child_id'], nonce=child['nonce'], phase=reply['state']):
                        raise ValueError('recording phase changed')
                    continue
                if entry['audio'] and reply.get('state') in ('finalized','partial'):
                    selected=entry['selection']
                    if not selected or not selected.get('accepted') or reply.get('selection_id')!=selected['selection_id'] or reply.get('selection_digest')!=selected['selection_digest'] or [v['kind'] for v in reply.get('tracks',[])]!=selected['selected_kinds']: raise ValueError('audio terminal changed')
                    if entry.get('crop'):
                        actual=reply['crop']; wanted=selected['crop']; meta=selected['preview']
                        rect=wanted['rect'] if wanted['mode']=='crop' else {'x':0,'y':0,'width':meta['source_width'],'height':meta['source_height']}
                        if actual['basis_id']!=wanted['basis_id'] or actual['rect']!=rect or any(actual[k]!=meta[k] for k in ('source_width','source_height')): raise ValueError('recording crop changed')
                terminal = reply; protocol_ok = terminal.get('state') in ('finalized','partial'); break
            if protocol_ok and process.stdin is not None:
                process.stdin.close()
        except BaseException:
            if not entry['ready'].done(): entry['ready'].set_exception(ValueError('recording start unavailable'))
        if not protocol_ok and process.returncode is None:
            _stop_recorder(process,entry.get('control_guard'))
        try: await asyncio.wait_for(asyncio.shield(process.wait()), 5)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            if process.returncode is None:
                _stop_recorder(process,entry.get('control_guard'))
        wait_task = asyncio.create_task(process.wait())
        while not wait_task.done():
            try: await asyncio.shield(wait_task)
            except asyncio.CancelledError: continue
        try: code = wait_task.result()
        except BaseException: return 'unknown'
        if entry.get('audio') and entry.get('selection') and not entry['selection'].get('accepted'):
            selected=entry['selection']
            await asyncio.to_thread(bg_jobs.finish_recording_audio_selection,child['job_id'],account_id=child['account_id'],
                child_id=child['child_id'],nonce=child['nonce'],selection_id=selected['selection_id'],
                digest=selected['selection_digest'],generation=entry['generation'],selected_kinds=selected['selected_kinds'],accepted=False,fact_current=entry['fact_current'],crop=selected.get('crop'),source_basis=selected.get('source_basis'),choice_handles=selected.get('choice_handles'))
        while not pump_task.done():
            try: await asyncio.shield(pump_task)
            except asyncio.CancelledError: continue
        try: pump_task.result()
        except BaseException: return 'unknown'
        await asyncio.to_thread(output.flush)
        info = os.fstat(output.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 0 or info.st_size != total or total <= 0 or not await custody_current(): return 'unknown'
        output.seek(0); data = await asyncio.to_thread(output.read, MAX_BYTES+1)
        after = os.fstat(output.fileno())
        if len(data) != total or (info.st_dev,info.st_ino,info.st_size,info.st_mtime_ns)!=(after.st_dev,after.st_ino,after.st_size,after.st_mtime_ns): return 'unknown'
        exact_terminal = bool(protocol_ok and terminal.get('bytes_written') == total)
        reasons=('eos',) if entry['trim'] else ('stopped','duration')
        finalized = bool(pipe_ok and exact_terminal and terminal['state']=='finalized' and terminal['reason'] in reasons and code==0 and data.startswith(b'\x1aE\xdf\xa3') and not entry['cancelled'])
        if artifact_authority(store) != child['profile']['artifact_authority'] or not await custody_current(): return 'unknown'
        if not entry['trim']: finalized = finalized and await current()
        owner = account_artifact_owner(child['account_id']); digest = hashlib.sha256(data).hexdigest()
        row = await asyncio.to_thread(store.put, owner=owner, chunks=[data], media_type='video/webm' if finalized else 'application/octet-stream')
        retained = await asyncio.to_thread(lambda: b''.join(store.read_chunks(owner=owner, artifact_id=row.id)))
        if retained != data or row.content_sha256 != digest or not await custody_current(): return 'unknown'
        row = await asyncio.to_thread(store.acknowledge, owner=owner, artifact_id=row.id)
        if artifact_authority(store) != child['profile']['artifact_authority'] or not await custody_current(): return 'unknown'
        if not entry['trim']: finalized = finalized and await current()
        if artifact_authority(store) != child['profile']['artifact_authority'] or not await custody_current(): return 'unknown'
        metadata = terminal if exact_terminal else None
        receipt = {'outcome':'finalized' if finalized else 'partial','finalized':finalized,'exit_code':code,
                   'reason':terminal['reason'] if metadata else 'output_limit' if capped else 'error',
                   'generation':entry['generation'],'metadata':metadata,'artifact_id':row.id,'sha256':digest,
                   'size':total,'mime_type':'video/webm' if finalized else 'application/octet-stream'}
        return 'settled' if await asyncio.to_thread(bg_jobs.finish_media_local_child, child['job_id'], account_id=child['account_id'], child_id=child['child_id'], nonce=child['nonce'], receipt=receipt) else 'unknown'
    except BaseException:
        return 'unknown'
    finally:
        if process.returncode is None:
            _stop_recorder(process,entry.get('control_guard'))
        wait_task = asyncio.create_task(process.wait())
        while not wait_task.done():
            try: await asyncio.shield(wait_task)
            except asyncio.CancelledError: continue
        watcher.cancel()
        while not pump_task.done():
            try: await asyncio.shield(pump_task)
            except asyncio.CancelledError: continue
        if preview_task is not None:
            while not preview_task.done():
                try: await asyncio.shield(preview_task)
                except asyncio.CancelledError: continue
            try: preview_task.result()
            except BaseException: pass
        entry['preview']=None
        output.close()


def owns(child):
    entry = _RUNS.get(_key(child))
    return bool(entry and entry['done'] and not entry['done'].done())


def audio_choices(child):
    entry = _RUNS.get(_key(child))
    if not entry or not entry.get('audio') or entry.get('choices') is None or entry.get('selection') is not None or not owns(child): return None
    result={'generation':entry['generation'], 'choices':[dict(v) for v in entry['choices']]}
    if entry.get('crop'):
        import base64
        preview=entry.get('preview')
        if preview is None: return None
        result['preview']={**preview['metadata'],'png':base64.b64encode(preview['bytes']).decode('ascii')}
    return result


async def select_audio(child, *, revision, selection_id, generation, microphone, system_output, current, crop=None):
    from src.openclank.recording_audio_contract import selection
    entry = _RUNS.get(_key(child))
    if entry is None or not entry.get('audio') or entry['generation'] != generation: raise ValueError('audio choices unavailable')
    if entry.get('crop'):
        from src.openclank.recording_crop_contract import selection as crop_selection
        preview=entry.get('preview')
        if preview is None or type(crop) is not dict or crop.get('basis_id') != preview['metadata']['basis_id']: raise ValueError('recording preview unavailable')
        message=crop_selection(generation,selection_id,microphone,system_output,crop,source_width=preview['metadata']['source_width'],source_height=preview['metadata']['source_height'])
    else:
        if crop is not None: raise ValueError('recording crop unavailable')
        message=selection(generation,selection_id,microphone,system_output)
    kinds=[k for k,v in (('microphone',microphone),('system_output',system_output)) if v is not None]
    async with entry['lock']:
        if entry['cancelled'] or not await current(): raise ValueError('audio authority changed')
        row=await asyncio.to_thread(bg_jobs.get_media,child['job_id'],account_id=child['account_id'])
        previous=(row or {}).get('audio_selection')
        if previous is None:
            choices=entry.get('choices');offered=child['profile']['audio_intent']
            if choices is None or any(value is not None and (not offered[kind] or not any(c['kind']==kind and c['choice_id']==value for c in choices)) for kind,value in (('microphone',microphone),('system_output',system_output))): raise ValueError('audio choice changed')
        claim=await asyncio.to_thread(bg_jobs.issue_recording_audio_selection,child['job_id'],account_id=child['account_id'],revision=revision,child_id=child['child_id'],nonce=child['nonce'],generation=generation,selection_id=selection_id,digest=message['selection_digest'],selected_kinds=kinds,crop=crop,source_basis=({k:preview['metadata'][k] for k in ('source_width','source_height')} if entry.get('crop') else None),choice_handles=({'microphone':microphone,'system_output':system_output} if entry.get('crop') else None))
        if not claim['issue']: return 'status_only'
        selected={'selection_id':selection_id,'selection_digest':message['selection_digest'],'selected_kinds':kinds,'accepted':False, **({'crop':message['crop'],'preview':dict(preview['metadata']),'source_basis':{k:preview['metadata'][k] for k in ('source_width','source_height')},'choice_handles':{'microphone':microphone,'system_output':system_output}} if entry.get('crop') else {})}
        entry['selection']=selected
        try:
            if not await current(): raise ValueError('audio authority changed')
            process=entry['process']
            if process.returncode is not None or process.stdin is None: raise ValueError('audio child unavailable')
            data=json.dumps(message,separators=(',',':')).encode()+b'\n'
            if len(data)>8192: raise ValueError('audio command unavailable')
            process.stdin.write(data);await process.stdin.drain()
            return 'issued'
        except BaseException:
            task=asyncio.create_task(asyncio.to_thread(bg_jobs.finish_recording_audio_selection,child['job_id'],account_id=child['account_id'],child_id=child['child_id'],nonce=child['nonce'],selection_id=selection_id,digest=message['selection_digest'],generation=generation,selected_kinds=kinds,accepted=False,fact_current=entry['fact_current'],crop=crop,source_basis=selected.get('source_basis'),choice_handles=selected.get('choice_handles')))
            while not task.done():
                try: await asyncio.shield(task)
                except asyncio.CancelledError: continue
            task.result();raise


async def stop(child, *, stop_id, token, current):
    entry = _RUNS.get(_key(child))
    if entry is None: return 'unknown'
    accepted = False
    async with entry['lock']:
        cancelled = False
        try:
            if not entry['cancelled'] and await current():
                await _command(entry, 'stop'); accepted = True
        except asyncio.CancelledError:
            cancelled = True
        except Exception:
            pass
        task = asyncio.create_task(asyncio.to_thread(bg_jobs.finish_recording_stop, child['job_id'], account_id=child['account_id'], stop_id=stop_id, token=token, accepted=accepted))
        while not task.done():
            try: await asyncio.shield(task)
            except asyncio.CancelledError: cancelled = True
        task.result()
        if cancelled: raise asyncio.CancelledError
    return 'accepted' if accepted else 'unknown'


async def cancel(child, *, current):
    entry = _RUNS.get(_key(child))
    if entry is None: return 'unknown'
    async with entry['lock']:
        if not await current(): return 'unknown'
        entry['cancelled'] = True
        try:
            if entry['trim']:
                if entry['process'].stdin is None: return 'unknown'
                entry['process'].stdin.close()
            else: await _command(entry, 'cancel')
        except Exception: return 'unknown'
    return 'cancel_requested'
