"""An unlinked readonly snapshot of one freshly authorized Host WebM."""
import os
import stat
import tempfile


async def snapshot_video(facade, context, *, source_ref, source_revision, current):
    if os.name!='posix' or not os.path.isdir('/proc/self/fd'): raise ValueError('readonly snapshot unavailable')
    import fcntl
    writable=tempfile.TemporaryFile(mode='w+b',buffering=0); readonly=None
    try:
        os.fchmod(writable.fileno(),0o600)
        before=os.fstat(writable.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_nlink!=0: raise ValueError('snapshot unavailable')
        evidence,source_current=await facade._host_video_source(context,source_ref=source_ref,expected_revision=source_revision,
            current=current,snapshot_fd=writable.fileno())
        after=os.fstat(writable.fileno())
        if ((before.st_dev,before.st_ino)!=(after.st_dev,after.st_ino) or after.st_size!=evidence['source_size']
            or not await source_current()): raise ValueError('snapshot changed')
        readonly=os.open('/proc/self/fd/%d'%writable.fileno(),os.O_RDONLY|os.O_CLOEXEC)
        check=os.fstat(readonly)
        if ((check.st_dev,check.st_ino,check.st_size)!=(after.st_dev,after.st_ino,after.st_size)
            or fcntl.fcntl(readonly,fcntl.F_GETFL)&os.O_ACCMODE != os.O_RDONLY): raise ValueError('readonly snapshot unavailable')
        writable.close(); writable=None
        return {'fd':readonly,'identity':(check.st_dev,check.st_ino,check.st_size),'evidence':evidence,'current':source_current}
    except BaseException:
        if readonly is not None:os.close(readonly)
        raise
    finally:
        if writable is not None:writable.close()
