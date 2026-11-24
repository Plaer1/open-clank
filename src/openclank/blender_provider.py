"""Resolve an owner-bound, job-only configured Blender profile."""
import asyncio
import hashlib
import os
import re
import stat
import selectors
import subprocess
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path

from src.openclank.blender_child import artifact_authority

INTERFACE = 'blender-render-v1'


@dataclass(frozen=True)
class BlenderProfile:
    ownerAccountID: str
    connectionID: str
    connectionRevision: int
    interface: str
    executable_path: str = field(repr=False)
    executable_sha256: str
    executable_version: str
    executable_identity: tuple[int, int, int, int]
    runner_sha256: str
    artifact_authority: str

    def job_binding(self):
        return {'ownerAccountID':self.ownerAccountID, 'connectionID':self.connectionID,
                'connectionRevision':self.connectionRevision, 'interface':self.interface}

    def child_profile(self):
        return {'adapter':'blender-glb-png-v1', 'executable_sha256':self.executable_sha256,
                'executable_version':self.executable_version, 'runner_sha256':self.runner_sha256,
                'artifact_authority':self.artifact_authority}

    def four_heading_child_profile(self):
        runner = Path(__file__).with_name('blender_four_runner.py').read_bytes()
        if len(runner) > 128*1024: raise ValueError('Blender runner unavailable')
        return {**self.child_profile(), 'adapter':'blender-glb-four-heading-v1',
                'runner_sha256':hashlib.sha256(runner).hexdigest()}

    def material_bake_child_profile(self):
        if not re.fullmatch(r'4\.5\.[0-9]+', self.executable_version):
            raise ValueError('Blender 4.5 profile required')
        runner = Path(__file__).with_name('blender_material_runner.py').read_bytes()
        if len(runner) > 128*1024: raise ValueError('Blender runner unavailable')
        return {**self.child_profile(), 'adapter':'blender-material-bake-v1',
                'runner_sha256':hashlib.sha256(runner).hexdigest()}

    def rig_child_profile(self):
        runner = Path(__file__).with_name('blender_rig_runner.py').read_bytes()
        if len(runner) > 128*1024: raise ValueError('Blender runner unavailable')
        return {**self.child_profile(), 'adapter':'blender-glb-rig-clip-v1',
                'runner_sha256':hashlib.sha256(runner).hexdigest()}


def _version(fd, expected):
    process = subprocess.Popen([f'/proc/self/fd/{fd}', '--version'], stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env={'PATH':os.defpath}, pass_fds=(fd,))
    try:
        selector = selectors.DefaultSelector()
        selector.register(process.stdout, selectors.EVENT_READ)
        deadline = time.monotonic() + 5
        data = bytearray()
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not selector.select(remaining): raise ValueError('Blender version timeout')
            chunk = os.read(process.stdout.fileno(), 4096)
            if not chunk: break
            data.extend(chunk)
            if len(data) > 4096: raise ValueError('Blender version output too large')
        if process.wait(timeout=max(0.001, deadline-time.monotonic())) != 0:
            raise ValueError('Blender version unavailable')
        if data.decode('utf-8', errors='strict').splitlines()[0].split()[:2] != ['Blender', expected]:
            raise ValueError('Blender version changed')
    finally:
        if process.poll() is None: process.kill()
        process.wait()
        process.stdout.close()


def _current(*, store, auth, artifacts, owner, account, connection, revision):
    if auth.account_id(owner) != account or auth.username_for_account_id(account) != owner:
        raise ValueError('Blender account unavailable')
    row = store.get_connection(owner=owner, connection_id=connection)
    if (row.owner != owner or row.id != connection or row.revision != revision or not row.enabled or row.deleted_at is not None
            or row.family_id != 'blender' or row.adapter_id != INTERFACE or row.kind != 'local' or row.billing_lane != 'local'):
        raise ValueError('Blender profile unavailable')
    settings = row.settings
    if not isinstance(settings, dict) or set(settings) != {'interface','executablePath','executableSHA256','executableVersion'} or settings['interface'] != INTERFACE:
        raise ValueError('Blender profile unavailable')
    path, digest, version = settings['executablePath'], settings['executableSHA256'], settings['executableVersion']
    if (not isinstance(path,str) or not os.path.isabs(path) or os.path.realpath(path) != path
            or not isinstance(digest,str) or not re.fullmatch(r'[0-9a-f]{64}',digest)
            or not isinstance(version,str) or not re.fullmatch(r'[0-9]+\.[0-9]+\.[0-9]+',version)):
        raise ValueError('Blender profile unavailable')
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > 512*1024*1024 or not (info.st_mode & 0o111):
            raise ValueError('Blender executable unavailable')
    finally:
        os.close(fd)
    runner = Path(__file__).with_name('blender_runner.py').read_bytes()
    if len(runner) > 128*1024: raise ValueError('Blender runner unavailable')
    if auth.account_id(owner) != account or auth.username_for_account_id(account) != owner:
        raise ValueError('Blender account changed')
    return BlenderProfile(account, connection, revision, INTERFACE, path, digest, version,
                          (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns),
                          hashlib.sha256(runner).hexdigest(), artifact_authority(artifacts))


def qualify_blender_profile(profile):
    """Explicit pre-dispatch qualification of the exact configured binary."""
    fd = os.open(profile.executable_path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        before = os.fstat(fd)
        identity = lambda value:(value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns)
        if identity(before) != profile.executable_identity:
            raise ValueError('Blender executable changed')
        with os.fdopen(os.dup(fd), 'rb') as handle:
            if hashlib.file_digest(handle, 'sha256').hexdigest() != profile.executable_sha256:
                raise ValueError('Blender executable changed')
        _version(fd, profile.executable_version)
        if identity(os.fstat(fd)) != profile.executable_identity:
            raise ValueError('Blender executable changed')
    finally:
        os.close(fd)


@asynccontextmanager
async def admitted_blender_profile(*, store, auth_manager, account_barrier, artifact_store, owner,
                                    ownerAccountID, connectionID, connectionRevision, interface):
    if interface != INTERFACE or account_barrier is None:
        raise ValueError('Blender profile unavailable')
    async with account_barrier.admitted(owner):
        args = dict(store=store, auth=auth_manager, artifacts=artifact_store, owner=owner,
                    account=ownerAccountID, connection=connectionID, revision=connectionRevision)
        profile = await asyncio.to_thread(_current, **args)
        async def assert_current():
            if await asyncio.to_thread(_current, **args) != profile:
                raise ValueError('Blender profile changed')
        yield profile, assert_current
