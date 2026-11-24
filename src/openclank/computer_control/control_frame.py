"""Private same-control-stream frame acquisition; no portal selection or authority."""
from __future__ import annotations
import asyncio, json, os, signal, struct
from src.openclank.recording_child import _profile_readiness

async def acquire(claim, current):
    """Consume a fresh Host-owned remote and return only a bounded validated PNG."""
    helper=None;proc=None;remote=claim.pop("fd",None)
    if type(remote) is not int:return None
    try:
        if not await current():return None
        helper=await _profile_readiness("control-frame-v1")
        if not helper or not await current():return None
        spawn=asyncio.create_task(asyncio.create_subprocess_exec('/proc/self/fd/%d'%helper['fd'],'--control-frame-v1','--remote-fd',str(remote),
            pass_fds=(helper['fd'],remote),stdin=asyncio.subprocess.PIPE,stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,start_new_session=True,limit=8193))
        cancelled=False
        while not spawn.done():
            try: await asyncio.shield(spawn)
            except asyncio.CancelledError: cancelled=True
        proc=spawn.result()
        os.close(remote);remote=None
        if cancelled:raise asyncio.CancelledError
        async def exchange():
            proc.stdin.write(json.dumps({'version':1,'serial':claim['serial']},separators=(',',':')).encode());await proc.stdin.drain();proc.stdin.close()
            header=await proc.stdout.readline()
            if not header.endswith(b'\n') or len(header)>8192:raise ValueError('invalid frame')
            info=json.loads(header)
            if (type(info) is not dict or set(info)!={'version','state','pixel_size','png_bytes'} or info['version']!=1
                or info['state']!='frame' or info['pixel_size']!=list(claim['size']) or type(info['png_bytes']) is not int
                or not 24<=info['png_bytes']<=64*1024*1024):raise ValueError('invalid frame')
            data=await proc.stdout.readexactly(info['png_bytes'])
            if await proc.stdout.read(1) or await proc.wait()!=0:raise ValueError('invalid frame')
            if data[:8]!=b'\x89PNG\r\n\x1a\n' or data[12:16]!=b'IHDR' or list(struct.unpack('>II',data[16:24]))!=info['pixel_size']:raise ValueError('invalid frame')
            return data
        data=await asyncio.wait_for(exchange(),8)
        if not await current():return None
        return data
    except (OSError,ValueError,TypeError,KeyError,asyncio.TimeoutError,asyncio.IncompleteReadError):return None
    finally:
        if remote is not None:os.close(remote)
        if proc is not None and proc.returncode is None:
            try:os.killpg(proc.pid,signal.SIGKILL)
            except ProcessLookupError:pass
            waiter=asyncio.create_task(proc.wait())
            while not waiter.done():
                try:await asyncio.shield(waiter)
                except asyncio.CancelledError:continue
        if helper is not None:os.close(helper['fd'])
