"""Fixed Cookbook acquisition worker. No command or URL comes from the model."""
import hashlib
import json
import os
import stat
import subprocess
import sys
import selectors
import time
from dataclasses import dataclass
from pathlib import Path

from src.openclank.cookbook_state import PRIVATE_KEY, transaction, save, advance, finish_verified

MAX_TOTAL = 8 * 1024**3

@dataclass(frozen=True)
class ChildSettled:
    success: bool
    paths: object = None


def _cache_root(destination):
    path = Path(destination['path'])
    if not path.is_absolute() or str(path) != os.path.normpath(str(path)):
        raise ValueError('unsafe cache root')
    fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in path.parts[1:]:
            nxt = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = nxt
        info = os.fstat(fd)
        if (info.st_dev, info.st_ino) != (destination['device'], destination['inode']):
            raise ValueError('cache root changed')
        return fd
    except BaseException:
        os.close(fd)
        raise


def _walk(root_fd, parts):
    fd = os.dup(root_fd)
    try:
        for part in parts:
            nxt = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = nxt
        return fd
    except BaseException:
        os.close(fd)
        raise


def _verify_file(root_fd, returned, expected, review):
    prefix = f'/proc/self/fd/{root_fd}/'
    if not isinstance(returned, str) or not returned.startswith(prefix):
        raise ValueError('output escaped pinned cache')
    parts = returned[len(prefix):].split('/')
    repo = 'models--' + review['repoID'].replace('/', '--')
    expected_parts = [repo, 'snapshots', review['revision']] + expected['path'].split('/')
    if parts != expected_parts or any(x in ('', '.', '..') for x in parts):
        raise ValueError('unexpected snapshot path')
    parent = _walk(root_fd, parts[:-1])
    file_fd = None
    link = None
    target = None
    try:
        try:
            file_fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent)
        except OSError:
            # Only the exact selected snapshot leaf may be a relative symlink to
            # this repository's blobs directory. Never resolve arbitrary links.
            link = os.readlink(parts[-1], dir_fd=parent)
            normalized = os.path.normpath(os.path.join(*parts[:-1], link))
            target = normalized.split('/')
            if len(target) != 3 or target[:2] != [repo, 'blobs'] or not target[2] or target[2] in ('.', '..'):
                raise ValueError('unsupported snapshot link')
            blobdir = _walk(root_fd, [repo, 'blobs'])
            try:
                file_fd = os.open(target[2], os.O_RDONLY | os.O_NOFOLLOW, dir_fd=blobdir)
            finally:
                os.close(blobdir)
            if os.readlink(parts[-1], dir_fd=parent) != link:
                raise ValueError('snapshot link changed')
        before = os.fstat(file_fd)
        if not stat.S_ISREG(before.st_mode) or before.st_size != expected['size']:
            raise ValueError('output size or type mismatch')
        h = hashlib.sha256()
        with os.fdopen(file_fd, 'rb', closefd=False) as stream:
            while chunk := stream.read(1024 * 1024):
                h.update(chunk)
        after = os.fstat(file_fd)
        if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
            raise ValueError('output changed while hashing')
        digest = h.hexdigest()
        if link is not None:
            if os.readlink(parts[-1], dir_fd=parent) != link:
                raise ValueError('snapshot link changed')
            blobdir = _walk(root_fd, [repo, 'blobs'])
            try:
                check = os.open(target[2], os.O_RDONLY | os.O_NOFOLLOW, dir_fd=blobdir)
                try:
                    current = os.fstat(check)
                    if (current.st_dev, current.st_ino, current.st_size, current.st_mtime_ns) != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
                        raise ValueError('blob changed')
                finally:
                    os.close(check)
            finally:
                os.close(blobdir)
        else:
            check = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent)
            try:
                current = os.fstat(check)
                if (current.st_dev, current.st_ino, current.st_size, current.st_mtime_ns) != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
                    raise ValueError('snapshot file changed')
            finally:
                os.close(check)
        if expected['lfsSHA256'] is not None and digest != expected['lfsSHA256']:
            raise ValueError('declared LFS hash mismatch')
        return {'path':expected['path'], 'size':before.st_size, 'sha256':digest}
    finally:
        if file_fd is not None:
            os.close(file_fd)
        os.close(parent)


def run(state_path, operation_id, digest, revision, child):
    """Claim once, await owned child, verify files and write one terminal CAS."""
    with transaction(state_path) as state:
        row = state.get(PRIVATE_KEY, {}).get(operation_id)
        if not row or row.get('digest') != digest or not row.get('review'):
            raise ValueError('operation unavailable')
        account, review, cache = row['account_id'], row['review'], row.get('cache')
        if not cache or cache.get('phase') != 'created':
            raise ValueError('job cache unavailable')
    parent_fd = _cache_root(review['destination'])
    try:
        root_fd = os.open(cache['name'], os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd)
    finally:
        os.close(parent_fd)
    info = os.fstat(root_fd)
    if (info.st_dev, info.st_ino) != (cache['device'], cache['inode']):
        os.close(root_fd)
        raise ValueError('job cache changed')
    try:
        with transaction(state_path) as state:
            row = state.get(PRIVATE_KEY, {}).get(operation_id)
            if not row or row.get('review') != review or row.get('cache') != cache:
                raise ValueError('operation changed')
            advance(state, operation_id=operation_id, account_id=account, digest=digest,
                    revision=revision, expected='dispatching', target='running')
            running_revision = revision + 1
            save(state_path, state)
    except BaseException:
        os.close(root_fd)
        raise
    verified = []
    try:
        try:
            if sum(f['size'] for f in review['files']) > MAX_TOTAL:
                raise ValueError('acquisition exceeds size budget')
            outcome = child(review, root_fd)
            if not isinstance(outcome, ChildSettled):
                raise RuntimeError('child settlement is unknown')
            if not outcome.success:
                receipt = {'status':'failed', 'files':[]}
            else:
                receipt = None
            paths = outcome.paths
            if receipt is None and (not isinstance(paths, list) or len(paths) != len(review['files'])):
                raise ValueError('child output mismatch')
            for item, returned in zip(review['files'], paths or []):
                verified.append(_verify_file(root_fd, returned, item, review))
            check_fd = _cache_root(review['destination'])
            try:
                check_job = os.open(cache['name'], os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=check_fd)
                try:
                    check = os.fstat(check_job)
                    if (check.st_dev, check.st_ino) != (info.st_dev, info.st_ino):
                        raise ValueError('job cache changed')
                finally:
                    os.close(check_job)
            finally:
                os.close(check_fd)
            if receipt is None:
                receipt = {'status':'complete', 'files':verified}
        finally:
            os.close(root_fd)
    except Exception:
        if 'outcome' not in locals() or not isinstance(outcome, ChildSettled):
            raise
        receipt = {'status':'failed', 'files':verified}
    with transaction(state_path) as state:
        finish_verified(state, operation_id=operation_id, account_id=account, digest=digest,
                        revision=running_revision, receipt=receipt)
        save(state_path, state)
    return receipt


_CHILD = r"""
import json, sys
from huggingface_hub import hf_hub_download
request = json.load(sys.stdin)
paths = []
for item in request['files']:
    paths.append(hf_hub_download(repo_id=request['repoID'], filename=item['path'],
        revision=request['revision'], cache_dir=request['destination']['path'],
        token=False, endpoint='https://huggingface.co'))
json.dump(paths, sys.stdout)
"""


def fixed_child(review, root_fd):
    """Bound output and wait for the exact isolated SDK child."""
    if sys.platform != 'linux' or not os.path.isdir('/proc/self/fd'):
        raise ValueError('pinned cache adapter unavailable')
    root = f'/proc/self/fd/{root_fd}'
    env = {'PATH':os.defpath, 'HF_HUB_DISABLE_IMPLICIT_TOKEN':'1',
           'HF_HUB_DISABLE_TELEMETRY':'1', 'HF_HUB_DISABLE_XET':'1',
           'HF_HOME':root, 'HF_HUB_CACHE':root, 'HF_XET_CACHE':root + '/xet',
           'XDG_CACHE_HOME':root, 'XDG_CONFIG_HOME':root, 'XDG_STATE_HOME':root, 'TMPDIR':root,
           'PYTHONIOENCODING':'utf-8'}
    request = dict(review)
    request['destination'] = {'path':f'/proc/self/fd/{root_fd}'}
    payload = json.dumps(request, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
    child = subprocess.Popen([sys.executable, '-I', '-c', _CHILD], stdin=subprocess.PIPE,
                             stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=env, pass_fds=(root_fd,))
    try:
        selector = selectors.DefaultSelector()
        os.set_blocking(child.stdin.fileno(), False)
        selector.register(child.stdin, selectors.EVENT_WRITE)
        selector.register(child.stdout, selectors.EVENT_READ)
        deadline = time.monotonic() + 3600
        chunks = bytearray()
        sent = 0
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ValueError('acquisition child deadline exceeded')
            events = selector.select(remaining)
            if not events:
                raise ValueError('acquisition child deadline exceeded')
            for key, _ in events:
                if key.fileobj is child.stdin:
                    sent += os.write(child.stdin.fileno(), payload[sent:sent+65536])
                    if sent == len(payload):
                        selector.unregister(child.stdin)
                        child.stdin.close()
                else:
                    chunk = os.read(child.stdout.fileno(), 65536)
                    if not chunk:
                        selector.unregister(child.stdout)
                    else:
                        chunks.extend(chunk)
                        if len(chunks) > 524288:
                            raise ValueError('child result too large')
        code = child.wait(timeout=max(0.001, deadline-time.monotonic()))
        try:
            paths = json.loads(chunks) if code == 0 else None
        except (ValueError, UnicodeError):
            paths = None
        return ChildSettled(code == 0 and paths is not None, paths)
    except Exception:
        if child.poll() is None:
            child.kill()
        child.wait()  # Failure here means no positive settlement evidence.
        return ChildSettled(False)
    finally:
        child.stdout.close()


def main():
    if len(sys.argv) != 5:
        raise SystemExit(2)
    run(sys.argv[1], sys.argv[2], sys.argv[3], int(sys.argv[4]), fixed_child)


if __name__ == '__main__':
    main()
