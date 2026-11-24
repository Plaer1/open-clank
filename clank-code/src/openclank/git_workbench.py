"""Closed, bounded projections for the private Git workbench transport.

These helpers are not an authorization boundary: the caller must resolve and
recheck the current Files read policy before returning any individual path.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from collections.abc import Callable

_MAX_BYTES = 262144
_PRIVATE = frozenset({'.git', '.clanker', '.clankers', '.references'})


async def derive_git_commit_witness(*, utility, provider, repository, account: str,
        root_origin: str, location_id: str, workspace_id: str | None,
        policy_generation: int, chat_id: str, is_admin: bool, group_ids: tuple[str, ...],
        current, native_read) -> dict:
    """Derive complete private Git metadata under the original recursive root.

    The caller owns both callbacks and must bound native transport before
    decoding. Git entries are metadata, not reads of current worktree files.
    This is a point-in-time witness, not an approval or cross-store lock.
    """
    from src.openclank.account_request_barrier import UtilityLifetime
    from src.openclank.files_facade import ProviderContext
    from src.openclank.files_host_provider import _path

    if (type(utility) is not UtilityLifetime or not callable(current) or not callable(native_read)
            or type(account) is not str or not account or utility.account != account
            or not utility.account_current() or utility.admission.released
            or type(policy_generation) is not int or policy_generation < 0
            or type(is_admin) is not bool or type(group_ids) is not tuple
            or any(type(group) is not str or not group for group in group_ids)
            or type(location_id) is not str or not location_id or type(chat_id) is not str or not chat_id
            or (workspace_id is not None and (type(workspace_id) is not str or not workspace_id))):
        raise ValueError('Git commit witness unavailable')
    root = _path(root_origin)
    if os.path.realpath(root) != root:
        raise ValueError('Git commit root unavailable')
    context = ProviderContext(account, utility.owner, policy_generation, is_admin=is_admin, workspace_id=workspace_id)

    def root_identity():
        fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
        try:
            observed = os.fstat(fd); current_root = os.lstat(root)
            if (not stat.S_ISDIR(observed.st_mode) or not stat.S_ISDIR(current_root.st_mode)
                    or (observed.st_dev,observed.st_ino) != (current_root.st_dev,current_root.st_ino)
                    or not 0 <= observed.st_dev <= 9007199254740991
                    or not 0 < observed.st_ino <= 9007199254740991):
                raise ValueError('Git root changed')
            return {'device':observed.st_dev,'inode':observed.st_ino}
        finally:
            os.close(fd)

    async def authority():
        if (not utility.account_current() or not await utility.barrier.is_live(utility.owner,utility.admission)
                or await current() is not True or repository.generation() != policy_generation):
            raise ValueError('Git commit authority changed')
        if workspace_id is not None:
            workspace = repository.get_workspace(workspace_id)
            if (workspace.archived or workspace.owner_subject_id != account or workspace.location_id != location_id
                    or workspace.relative_folder != ''):
                raise ValueError('Git commit workspace unavailable')
        scope = provider._scope(context)
        if not provider._path_writable(context,root,scope):
            raise ValueError('Git commit write unavailable')
        for capability in ('read','write'):
            effective = repository.resolve(subject_id=account,is_admin=is_admin,origin='app',location_id=location_id,
                capability=capability,workspace_id=workspace_id,relative_resource='',chat_id=chat_id,
                group_ids=group_ids,observation_only=True)
            if not effective.allowed or effective.policy_generation != policy_generation:
                raise ValueError('Git commit policy unavailable')
        selected = await provider.authorize_readonly_root(context,origin_id=root_origin,expected_revision=None)
        if (selected['root_id'] != location_id or selected['root'] != root
                or selected['generation'] != policy_generation):
            raise ValueError('Git commit root changed')
        identity = root_identity()
        if await current() is not True or not utility.account_current() or repository.generation() != policy_generation:
            raise ValueError('Git commit authority changed')
        return identity

    def validate(value, identity):
        if type(value) is not dict or set(value) != {'version','physicalIndex','indexEntries','headEntries','headOID','headRef','config','executable'} or type(value['version']) is not int or value['version'] != 1:
            raise ValueError('Git commit metadata unavailable')
        physical = value['physicalIndex']
        if type(physical) is not dict or set(physical) != {'version','sha256','device','inode','size','objectFormat','root','git','objects'}:
            raise ValueError('Git commit index unavailable')
        safe = lambda n: type(n) is int and 0 <= n <= 9007199254740991
        hexdigest = lambda v,n: type(v) is str and len(v) == n and re.fullmatch(r'[0-9a-f]+',v) is not None
        def directory(item):
            return (type(item) is dict and set(item) == {'device','inode'} and safe(item['device'])
                and safe(item['inode']) and item['inode'] > 0)
        if (type(physical['version']) is not int or physical['version'] != 1 or not hexdigest(physical['sha256'],64)
                or physical['objectFormat'] not in ('sha1','sha256') or not safe(physical['device'])
                or not safe(physical['inode']) or physical['inode'] == 0 or not safe(physical['size'])
                or not 32 <= physical['size'] <= 4*1024*1024
                or any(not directory(physical[key]) for key in ('root','git','objects')) or physical['root'] != identity):
            raise ValueError('Git commit index unavailable')
        width = 40 if physical['objectFormat'] == 'sha1' else 64
        if not hexdigest(value['headOID'],width): raise ValueError('Git HEAD unavailable')
        ref = value['headRef']
        if (type(ref) is not str or not ref.startswith('refs/heads/') or len(ref.encode('utf-8','strict')) > 1024
                or any(ord(c) < 32 or ord(c) == 127 for c in ref)):
            raise ValueError('Git ref unavailable')
        for key,limit in (('config',128*1024),('executable',128*1024*1024)):
            item = value[key]
            expected = {'device','inode','size','sha256'} | ({'path','version'} if key == 'executable' else set())
            if (type(item) is not dict or set(item) != expected or not safe(item['device'])
                    or not safe(item['inode']) or item['inode'] == 0 or not safe(item['size'])
                    or item['size'] > limit or not hexdigest(item['sha256'],64)):
                raise ValueError('Git executable or config unavailable')
            if key == 'executable':
                path,version = item['path'],item['version']
                if (type(path) is not str or not os.path.isabs(path) or '\0' in path or len(path.encode('utf-8','strict')) > 4096
                        or type(version) is not str or not re.fullmatch(r'git version 2\.(?:[5-9][0-9]|[1-9][0-9]{2,})(?:\.[0-9]+)?',version)):
                    raise ValueError('Git executable unavailable')
        budget = 0
        for key in ('indexEntries','headEntries'):
            entries = value[key]
            if type(entries) is not list or len(entries) > 65536: raise ValueError('Git entries unavailable')
            previous = None
            for entry in entries:
                if (type(entry) is not dict or set(entry) != {'path','mode','oid'}
                        or type(entry['path']) is not str
                        or not allowed_relative_path(entry['path']) or entry['mode'] not in (0o100644,0o100755)
                        or type(entry['mode']) is not int or not hexdigest(entry['oid'],width)):
                    raise ValueError('Git entry unavailable')
                encoded = entry['path'].encode('utf-8','strict')
                if previous is not None and encoded <= previous: raise ValueError('Git entry order unavailable')
                previous = encoded; budget += len(encoded) + width + 80
                if budget > 4*1024*1024: raise ValueError('Git metadata too large')
        encoded = json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False).encode('utf-8','strict')
        if len(encoded) > 4*1024*1024: raise ValueError('Git metadata too large')
        return encoded

    before_root = await authority()
    before = await native_read()
    encoded = validate(before,before_root)
    middle_root = await authority()
    after = await native_read()
    if middle_root != before_root or validate(after,middle_root) != encoded:
        raise ValueError('Git commit metadata changed')
    if await authority() != before_root:
        raise ValueError('Git commit root changed')
    snapshot = {'version':1,'account':account,'location':location_id,'workspace':workspace_id,
        'chat':chat_id,'policyGeneration':policy_generation,'root':root,'git':json.loads(encoded)}
    material = json.dumps(['openclank.git-commit.witness.v1',snapshot],sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False).encode('utf-8','strict')
    return {'version':1,'digest':hashlib.sha256(material).hexdigest(),'snapshot':snapshot}


async def derive_git_index_witness(*, utility, provider, repository, account: str,
        root_origin: str, file_origin: str, location_id: str, workspace_id: str | None,
        policy_generation: int, chat_id: str, is_admin: bool, group_ids: tuple[str, ...],
        current, native_read) -> dict:
    """Derive one private raw-byte witness under the retained original owner.

    ``current`` checks that original account, map, holder and lease remain live.
    ``native_read`` is the retained worker adapter, returning the closed snapshot
    described below; neither callback is supplied by the browser. No witness is
    an authorization token, and callers must rederive it at each effect boundary.
    """
    from src.openclank.account_request_barrier import UtilityLifetime
    from src.openclank.files_facade import ProviderContext
    from src.openclank.files_host_provider import _path

    if (type(utility) is not UtilityLifetime or not callable(current) or not callable(native_read)
            or utility.account != account or not utility.account_current() or utility.admission.released
            or type(policy_generation) is not int or policy_generation < 0
            or type(is_admin) is not bool or type(group_ids) is not tuple
            or any(type(group) is not str or not group for group in group_ids)
            or (workspace_id is not None and (type(workspace_id) is not str or not workspace_id))):
        raise ValueError('Git witness unavailable')
    root, path = _path(root_origin), _path(file_origin)
    relative = os.path.relpath(path, root).replace(os.sep, '/')
    if (not allowed_relative_path(relative) or os.path.commonpath([root, path]) != root
            or os.path.normpath(os.path.join(root, relative)) != os.path.normpath(path)):
        raise ValueError('Git witness unavailable')
    context = ProviderContext(account, utility.owner, policy_generation, is_admin=is_admin, workspace_id=workspace_id)

    async def authority():
        if (not await utility.barrier.is_live(utility.owner, utility.admission)
                or not utility.account_current() or await current() is not True
                or repository.generation() != policy_generation):
            raise ValueError('Git witness stale')
        scope = provider._scope(context)
        if (not provider._path_writable(context, root, scope) or not provider._path_writable(context, path, scope)):
            raise ValueError('Git write unavailable')
        for resource in ('', relative):
            for capability in ('read', 'write'):
                effective = repository.resolve(subject_id=account, is_admin=is_admin, origin='app',
                    location_id=location_id, capability=capability, workspace_id=workspace_id,
                    relative_resource=resource, chat_id=chat_id, group_ids=group_ids, observation_only=True)
                if not effective.allowed or effective.policy_generation != policy_generation:
                    raise ValueError('Git policy unavailable')
        selected = await provider.authorize_readonly_root(context, origin_id=root_origin, expected_revision=None)
        if selected['root_id'] != location_id or selected['root'] != root or selected['generation'] != policy_generation:
            raise ValueError('Git root changed')
        if await current() is not True or repository.generation() != policy_generation:
            raise ValueError('Git witness stale')
        return scope

    def snapshot(value):
        if type(value) is not dict or set(value) != {'root', 'git', 'objectFormat', 'head', 'indexEntry', 'treeEntry', 'physicalIndex'}:
            raise ValueError('Git snapshot unavailable')
        if value['root'] != root or value['git'] != os.path.join(root, '.git'):
            raise ValueError('Git namespace changed')
        physical = value['physicalIndex']
        if type(physical) is not dict or set(physical) != {'version','sha256','device','inode','size','objectFormat','root','git','objects'}:
            raise ValueError('Git index unavailable')
        safe = lambda n: type(n) is int and 0 <= n <= 9007199254740991
        if (physical['version'] != 1 or not re.fullmatch(r'[0-9a-f]{64}', str(physical['sha256']))
                or physical['objectFormat'] not in ('sha1', 'sha256') or value['objectFormat'] != physical['objectFormat'] or not safe(physical['device'])
                or not safe(physical['inode']) or physical['inode'] == 0 or not safe(physical['size'])
                or not 32 <= physical['size'] <= 4194304):
            raise ValueError('Git index unavailable')
        for name in ('root', 'git', 'objects'):
            item = physical[name]
            if (type(item) is not dict or set(item) != {'device','inode'} or not safe(item['device'])
                    or not safe(item['inode']) or item['inode'] == 0):
                raise ValueError('Git namespace unavailable')
        width = 40 if physical['objectFormat'] == 'sha1' else 64
        oid = lambda v: type(v) is str and len(v) == width and re.fullmatch(r'[0-9a-f]+', v) is not None
        if not oid(value['head']):
            raise ValueError('Git HEAD unavailable')
        for name in ('indexEntry', 'treeEntry'):
            entry = value[name]
            if entry is not None and (type(entry) is not dict or set(entry) != {'path','mode','oid'}
                    or entry['path'] != relative or entry['mode'] not in ('100644','100755') or not oid(entry['oid'])):
                raise ValueError('Git entry unavailable')
        return value

    scope = await authority()
    before = snapshot(await native_read(relative))
    client = provider._stream_client(context, scope)
    opened = await client.open_handle(path)
    size, mode, tag = opened.get('size'), opened.get('mode'), opened.get('object_tag')
    if (opened.get('kind') != 'File' or type(size) is not int or not 0 <= size <= _MAX_BYTES
            or type(mode) is not int or not stat.S_ISREG(mode)
            or type(tag) is not str or not re.fullmatch(r'[0-9a-f]{64}', tag)
            or type(opened.get('handle')) is not dict):
        raise ValueError('Git file unavailable')
    data = bytearray()
    if size:
        async for chunk in client.stream_read_handle(opened['handle'], offset=0, length=size):
            if type(chunk) is not bytes or len(data) + len(chunk) > size:
                raise ValueError('Git file changed')
            data.extend(chunk)
    if len(data) != size:
        raise ValueError('Git file changed')
    post = await provider.stat_inspection(context, origin_id=file_origin)
    if (post.kind != 'file' or post.origin_id != file_origin or post.size != size
            or dict(post.revision or {}) != {'kind':'hostObjectIdentity','value':tag}):
        raise ValueError('Git file changed')
    await authority()
    after = snapshot(await native_read(relative))
    if before != after:
        raise ValueError('Git snapshot changed')
    await authority()
    post = await provider.stat_inspection(context, origin_id=file_origin)
    if (post.kind != 'file' or post.origin_id != file_origin or post.size != size
            or dict(post.revision or {}) != {'kind':'hostObjectIdentity','value':tag}
            or await current() is not True or repository.generation() != policy_generation):
        raise ValueError('Git file changed')
    digest_input = {'version':1, 'account':account, 'location':location_id, 'workspace':workspace_id,
        'chat':chat_id, 'policyGeneration':policy_generation, 'root':root, 'path':relative,
        'objectTag':tag, 'size':size, 'mode':mode, 'sha256':hashlib.sha256(data).hexdigest(), 'git':before}
    digest = hashlib.sha256(json.dumps(digest_input, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()).hexdigest()
    return {'version':1, 'digest':digest, 'comparison':'raw', 'snapshot':digest_input, 'bytes':bytes(data)}


def allowed_relative_path(path: object) -> bool:
    if not isinstance(path, str) or not path or len(path.encode('utf-8')) > 4096 or '\x00' in path or path.startswith('/'):
        return False
    return all(part not in {'', '.', '..'} and part not in _PRIVATE for part in path.split('/'))


def index_fingerprint(data: bytes, *, exists: bool) -> str:
    """Keep absent index distinct from the hash of a present empty index."""
    if not isinstance(data, bytes) or len(data) > _MAX_BYTES or type(exists) is not bool or (not exists and data):
        raise ValueError('unavailable')
    return 'sha256:' + hashlib.sha256(data).hexdigest() if exists else 'absent'


def status_projection(data: bytes, *, may_read: Callable[[str], bool]) -> dict:
    """Project porcelain-v1 -z rows after a caller's current per-path check."""
    if not isinstance(data, bytes) or len(data) > _MAX_BYTES or (data and not data.endswith(b'\0')):
        raise ValueError('unavailable')
    visible = []
    partial = False
    for row in data.split(b'\0')[:-1]:
        if len(row) < 4 or row[2:3] != b' ':
            raise ValueError('unavailable')
        try:
            code, path = row[:2].decode('ascii'), row[3:].decode('utf-8', 'strict')
        except UnicodeError as exc:
            raise ValueError('unavailable') from exc
        if len(code) != 2 or any(c not in ' MADRCU?!' for c in code):
            raise ValueError('unavailable')
        if not allowed_relative_path(path) or not may_read(path):
            partial = True
            continue
        # --no-renames is required by the fixed native command. Unknown rows
        # remain visible as such rather than being interpreted as clean.
        visible.append({'path': path, 'index': code[0], 'worktree': code[1], 'untracked': code == '??'})
    return {'items': visible, 'partial': partial, 'clean': not visible and not partial}
