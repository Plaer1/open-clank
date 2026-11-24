"""Internal owned Linux ScreenCast session. No public tool or input authority."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
import json
import hashlib
import struct
import sys
from pathlib import Path
import os
import signal
import time
import uuid
from typing import Callable
from src.openclank.native_capture_linux import _helper


@dataclass(frozen=True)
class CaptureScope:
    owner: str
    account: str
    workspace: str
    task: str
    epoch: str


class CaptureSessionError(ValueError):
    pass


class CaptureSession:
    """Single-use context owned by a trusted scope and its invalidation event."""
    def __init__(self, scope: CaptureScope, current: Callable[[CaptureScope], bool],
                 invalidated: asyncio.Event, deadline_ms: int, *, frames: bool = False):
        if (not isinstance(scope, CaptureScope) or not all(isinstance(x, str) and 0 < len(x) <= 256 for x in
                (scope.owner, scope.account, scope.workspace, scope.task, scope.epoch))
                or not callable(current) or not isinstance(invalidated, asyncio.Event)
                or type(deadline_ms) is not int or not 0 < deadline_ms - int(time.time()*1000) <= 120000):
            raise CaptureSessionError('invalid_scope')
        self.scope, self.current, self.invalidated, self.deadline_ms = scope, current, invalidated, deadline_ms
        self.frames = frames is True
        self._busy = False
        self._active = None
        self.generation = uuid.uuid4().hex
        self.process = None
        self._watch = None
        self._teardown = None
        self._retired = False
        self._used = False

    def _check(self):
        try:
            valid = self.current(self.scope) is True
        except Exception:
            valid = False
        if self._retired or self.invalidated.is_set() or not valid or int(time.time()*1000) >= self.deadline_ms:
            raise CaptureSessionError('scope_changed')

    async def __aenter__(self):
        self._check()
        if self._used:
            raise CaptureSessionError('session_consumed')
        self._used = True
        if self.frames:
            if getattr(sys, "frozen", False):
                from src.runtime_paths import get_app_root
                helper = Path(get_app_root()) / "bin" / "openclank-native-capture-frame"
            else:
                helper = Path(__file__).resolve().parents[2] / "native/capture/target/release/openclank-native-capture-frame"
        else:
            helper = _helper()
        if not helper.is_file() or not os.access(helper, os.X_OK):
            raise CaptureSessionError('unavailable')
        try:
            self.process = await asyncio.create_subprocess_exec(str(helper), '--session-v2', stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL, start_new_session=True, limit=8193)
            self._check()
            self._watch = asyncio.create_task(self._guard())
            assert self.process.stdin is not None and self.process.stdout is not None
            request = {'version':2, 'action':'open', 'generation':self.generation, 'deadline_ms':self.deadline_ms}
            self.process.stdin.write(json.dumps(request, separators=(',', ':')).encode()+b'\n')
            await self.process.stdin.drain()
            line = await self.process.stdout.readline()
            self._check()
            if not line.endswith(b'\n') or len(line) > 8192:
                raise CaptureSessionError('invalid_response')
            try:
                reply = json.loads(line)
            except (ValueError, UnicodeError) as exc:
                raise CaptureSessionError('invalid_response') from exc
            if (not isinstance(reply, dict) or reply.get('version') != 2 or reply.get('generation') != self.generation
                    or reply.get('state') != 'active' or set(reply) != {'version','generation','state','source_type','logical_size','cursor'}
                    or reply['source_type'] not in ('monitor','window','unknown') or reply['cursor'] != 'embedded'):
                raise CaptureSessionError('unavailable')
            size = reply['logical_size']
            if size is not None and (not isinstance(size, list) or len(size) != 2 or any(type(n) is not int or n <= 0 or n > 65536 for n in size)):
                raise CaptureSessionError('invalid_response')
            self._check()
            self._active = {'version':2, 'generation':self.generation, 'state':'active', 'source_type':reply['source_type'],
                    'logical_size':size, 'cursor':'embedded'}
            return dict(self._active)
        except BaseException:
            await self.close()
            raise

    async def latest_frame(self):
        if not self.frames or not self._active or self._busy or self.process is None:
            raise CaptureSessionError('frame_unavailable')
        self._busy = True
        try:
            self._check()
            process = self.process
            assert process.stdin is not None and process.stdout is not None
            process.stdin.write(json.dumps({'version':2,'action':'frame','generation':self.generation}).encode()+b'\n')
            await process.stdin.drain(); self._check()
            timeout = max(0, (self.deadline_ms-int(time.time()*1000))/1000)
            line = await asyncio.wait_for(process.stdout.readline(), timeout); self._check()
            if not line.endswith(b'\n') or len(line)>8192: raise CaptureSessionError('invalid_response')
            reply = json.loads(line)
            if not isinstance(reply,dict) or reply.get('version')!=2 or reply.get('generation')!=self.generation: raise CaptureSessionError('invalid_response')
            if reply.get('state')=='no_frame' and set(reply)=={'version','generation','state'}: return None
            if set(reply)!={'version','generation','state','geometry','pixel_size','pts_ns','png_bytes'} or reply['state']!='frame': raise CaptureSessionError('invalid_response')
            size=reply['pixel_size']; count=reply['png_bytes']; pts=reply['pts_ns']; geometry=reply['geometry']
            if (not isinstance(size,list) or len(size)!=2 or any(type(n) is not int or n<=0 or n>4096 for n in size)
                or size[0]*size[1]>16_000_000 or type(count) is not int or not 24<=count<=64*1024*1024
                or type(geometry) is not int or geometry<=0 or (pts is not None and (type(pts) is not int or pts<0))): raise CaptureSessionError('invalid_response')
            timeout=max(0,(self.deadline_ms-int(time.time()*1000))/1000)
            data=await asyncio.wait_for(process.stdout.readexactly(count), timeout); self._check()
            if data[:8]!=b'\x89PNG\r\n\x1a\n' or data[12:16]!=b'IHDR' or list(struct.unpack('>II',data[16:24]))!=size: raise CaptureSessionError('invalid_response')
            return {'generation':self.generation,'geometry':geometry,'source_type':self._active['source_type'],
                'logical_size':self._active['logical_size'],'pixel_size':size,'cursor':'embedded',
                'pts_ns':pts,'pts_timebase':'stream_nanoseconds' if pts is not None else None,
                'receipt_time_ms':int(time.time()*1000),'transform':None,'drop_count':None,
                'sha256':hashlib.sha256(data).hexdigest(),'png':data}
        except BaseException:
            await self.close()
            raise
        finally:
            self._busy = False

    async def _guard(self):
        delay = max(0, (self.deadline_ms-int(time.time()*1000))/1000)
        invalidation = asyncio.create_task(self.invalidated.wait())
        process_exit = asyncio.create_task(self.process.wait())
        try:
            await asyncio.wait((invalidation, process_exit), timeout=delay, return_when=asyncio.FIRST_COMPLETED)
        finally:
            invalidation.cancel()
            process_exit.cancel()
            await asyncio.gather(invalidation, process_exit, return_exceptions=True)
        self._start_close()

    def _start_close(self):
        if self._teardown is None:
            self._retired = True
            self._teardown = asyncio.create_task(self._drain())
        return self._teardown

    async def close(self):
        task = self._start_close()
        cancelled = False
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                cancelled = True
        task.result()
        if cancelled:
            raise asyncio.CancelledError

    async def _drain(self):
        task, self._watch = self._watch, None
        if task and task is not asyncio.current_task():
            task.cancel()
        process, self.process = self.process, None
        if process is not None:
            if process.returncode is None and process.stdin is not None:
                try:
                    process.stdin.write(json.dumps({'version':2,'action':'stop','generation':self.generation}).encode()+b'\n')
                    await process.stdin.drain()
                    process.stdin.close()
                    await asyncio.wait_for(process.wait(), 1)
                except (OSError, ConnectionError, asyncio.TimeoutError):
                    pass
            if process.returncode is None:
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                try:
                    await asyncio.wait_for(process.wait(), 1)
                except asyncio.TimeoutError:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    await process.wait()
        if task and task is not asyncio.current_task():
            await asyncio.gather(task, return_exceptions=True)

    async def __aexit__(self, *_):
        await self.close()


def _record_generation(generation):
    if not isinstance(generation, str) or len(generation) != 32 or any(c not in '0123456789abcdef' for c in generation):
        raise CaptureSessionError('invalid_generation')


def recording_start(generation, deadline_ms, *, duration_ms=60000, fps=15, max_width=1920, max_height=1080, max_bytes=67108864):
    """Build a request for the caller's already-owned recorder child."""
    _record_generation(generation)
    limits = ((duration_ms, 60000), (fps, 30), (max_width, 1920), (max_height, 1080), (max_bytes, 67108864))
    if (type(deadline_ms) is not int or not 0 < deadline_ms-int(time.time()*1000) <= 120000
            or any(type(value) is not int or not 0 < value <= upper for value,upper in limits)):
        raise CaptureSessionError('invalid_recording_request')
    return {'version':3, 'action':'start', 'generation':generation, 'deadline_ms':deadline_ms,
            'duration_ms':duration_ms, 'fps':fps, 'max_width':max_width, 'max_height':max_height, 'max_bytes':max_bytes}


def recording_command(action, generation):
    _record_generation(generation)
    if action not in ('stop','cancel','frame'): raise CaptureSessionError('invalid_recording_request')
    return {'version':3, 'action':action, 'generation':generation}


def validate_recording_reply(value, generation):
    """Validate control metadata; host EOF/wait/custody remain mandatory."""
    _record_generation(generation)
    if not isinstance(value, dict) or value.get('version') != 3 or value.get('generation') != generation:
        raise CaptureSessionError('invalid_recording_reply')
    if value.get('state') == 'unsupported':
        if set(value) != {'version','state','generation','feature'} or value.get('feature') != 'latest_frame':
            raise CaptureSessionError('invalid_recording_reply')
        return dict(value)
    if value.get('state') in ('starting','recording'):
        if (set(value) != {'version','state','generation','source_type','logical_size','cursor','audio'}
                or value['source_type'] not in ('monitor','window','unknown') or value['cursor'] != 'embedded' or value['audio'] != 'none'):
            raise CaptureSessionError('invalid_recording_reply')
        size = value['logical_size']
        if size is not None and (not isinstance(size,list) or len(size)!=2 or any(type(x) is not int or not 0 < x <= 65536 for x in size)):
            raise CaptureSessionError('invalid_recording_reply')
    else:
        fields={'version','state','generation','reason','encoded_size','first_pts_ns','last_pts_ns','timebase','frames_in','frames_encoded','drops','bytes_written'}
        if (set(value)!=fields or value.get('state') not in ('finalized','partial')
                or value['reason'] not in ('stopped','duration','cancelled','source_lost','output_limit','error')
                or (value['state']=='finalized' and value['reason'] not in ('stopped','duration'))
                or value['timebase']!='stream_nanoseconds' or type(value['bytes_written']) is not int
                or not 0<=value['bytes_written']<=67108864):
            raise CaptureSessionError('invalid_recording_reply')
        size=value['encoded_size']
        if size is not None and (not isinstance(size,list) or len(size)!=2 or type(size[0]) is not int or type(size[1]) is not int or not 0<size[0]<=1920 or not 0<size[1]<=1080):
            raise CaptureSessionError('invalid_recording_reply')
        if value['state']=='finalized' and (size is None or value['bytes_written']==0): raise CaptureSessionError('invalid_recording_reply')
        for key in ('first_pts_ns','last_pts_ns','frames_in','frames_encoded','drops'):
            if value[key] is not None and (type(value[key]) is not int or not 0<=value[key]<=2**64-1): raise CaptureSessionError('invalid_recording_reply')
    return dict(value)


def trim_start(generation, deadline_ms, start_ms, end_ms, *, max_bytes=67108864):
    """Build a finite trim request; the caller owns both passed descriptors."""
    _record_generation(generation)
    if (type(deadline_ms) is not int or not 0 < deadline_ms-int(time.time()*1000) <= 120000
            or type(start_ms) is not int or type(end_ms) is not int or not 0 <= start_ms < end_ms <= 86400000
            or end_ms-start_ms>60000 or type(max_bytes) is not int or not 0<max_bytes<=67108864):
        raise CaptureSessionError('invalid_trim_request')
    return {'version':4,'action':'trim','generation':generation,'deadline_ms':deadline_ms,
            'start_ms':start_ms,'end_ms':end_ms,'max_bytes':max_bytes}


def validate_trim_reply(value,generation,start_ms,end_ms):
    """Validate metadata only; original child wait, EOF and Files custody are separate."""
    _record_generation(generation)
    if not isinstance(value,dict) or value.get('version')!=4 or value.get('generation')!=generation:
        raise CaptureSessionError('invalid_trim_reply')
    if value.get('state')=='starting':
        if set(value)!={'version','state','generation'}: raise CaptureSessionError('invalid_trim_reply')
        return dict(value)
    fields={'version','state','generation','reason','requested_start_ms','requested_end_ms','first_pts_ns','last_pts_ns',
            'last_duration_ns','timebase','selected_frames','encoded_frames','coverage','bytes_written'}
    if (set(value)!=fields or value['state'] not in ('finalized','partial') or value['reason'] not in ('eos','output_limit','cancelled','unsupported')
            or type(value['requested_start_ms']) is not int or type(value['requested_end_ms']) is not int
            or value['requested_start_ms']!=start_ms or value['requested_end_ms']!=end_ms or value['timebase']!='source_stream_nanoseconds'
            or value['coverage'] not in ('reached_end','source_eos_before_end','unknown') or type(value['bytes_written']) is not int
            or not 0<=value['bytes_written']<=67108864): raise CaptureSessionError('invalid_trim_reply')
    for key in ('first_pts_ns','last_pts_ns','last_duration_ns'):
        if value[key] is not None and (type(value[key]) is not int or not 0<=value[key]<=2**64-1): raise CaptureSessionError('invalid_trim_reply')
    for key in ('selected_frames','encoded_frames'):
        if type(value[key]) is not int or not 0<=value[key]<=2**64-1: raise CaptureSessionError('invalid_trim_reply')
    if value['state']=='finalized' and (value['reason']!='eos' or not value['selected_frames'] or not value['encoded_frames']
            or value['encoded_frames']!=value['selected_frames'] or not value['bytes_written'] or value['first_pts_ns'] is None or value['last_pts_ns'] is None
            or not start_ms*1000000<=value['first_pts_ns']<=value['last_pts_ns']<end_ms*1000000): raise CaptureSessionError('invalid_trim_reply')
    return dict(value)


def control_recording_start(generation, deadline_ms, **limits):
    return {**recording_start(generation, deadline_ms, **limits), 'version':8}

def control_recording_command(action, generation):
    return {**recording_command(action, generation), 'version':8}

def validate_control_recording_reply(value, generation):
    if not isinstance(value,dict) or value.get('version') != 8: raise CaptureSessionError('invalid_recording_reply')
    copy = dict(value); copy['version'] = 5
    validate_observed_recording_reply(copy, generation)
    return dict(value)

def observed_recording_start(generation, deadline_ms, **limits):
    return {**recording_start(generation, deadline_ms, **limits), 'version':5}

def observed_recording_command(action, generation):
    return {**recording_command(action, generation), 'version':5}

def validate_observed_recording_reply(value, generation):
    """Validate metadata only; the caller must verify EOF, wait and custody."""
    import re
    if not isinstance(value,dict) or value.get('version')!=5: raise CaptureSessionError('invalid_recording_reply')
    base=dict(value); base['version']=3; obs=base.pop('observations',None)
    result=validate_recording_reply(base,generation)
    if result['state'] not in ('finalized','partial'):
        if 'observations' in value: raise CaptureSessionError('invalid_recording_reply')
        return dict(value)
    if not isinstance(obs,dict) or set(obs)!={'source_pixels','output_pixels','caps_changed','source','rate_output','encoded','videorate'} or type(obs['caps_changed']) is not bool: raise CaptureSessionError('invalid_recording_reply')
    for key in ('source_pixels','output_pixels'):
        size=obs[key]
        if size is not None and (not isinstance(size,list) or len(size)!=2 or any(type(n) is not int or not 0<n<=65536 for n in size)): raise CaptureSessionError('invalid_recording_reply')
    if obs['output_pixels']!=value['encoded_size']: raise CaptureSessionError('invalid_recording_reply')
    for key in ('source','rate_output','encoded'):
        row=obs[key]; counts=('buffers','missing_pts','missing_running'); times=('first_pts_ns','last_pts_ns','first_running_ns','last_running_ns')
        if not isinstance(row,dict) or set(row)!=set(counts+times) or any(type(row[k]) is not int or not 0<=row[k]<=2**53-1 for k in counts): raise CaptureSessionError('invalid_recording_reply')
        for k in times:
            n=row[k]
            if n is not None and (not isinstance(n,str) or not re.fullmatch(r'0|[1-9][0-9]{0,19}',n) or int(n)>2**64-1): raise CaptureSessionError('invalid_recording_reply')
    rate=obs['videorate']
    if not isinstance(rate,dict) or set(rate)!={'in','out','drop','duplicate'} or any(n is not None and (type(n) is not int or not 0<=n<=2**53-1) for n in rate.values()): raise CaptureSessionError('invalid_recording_reply')
    return dict(value)
