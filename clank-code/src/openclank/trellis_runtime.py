"""Owner-bound, job-only TRELLIS declaration; never loads model weights on Host."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path
import re
import stat

from src.openclank.blender_child import artifact_authority
from src.openclank.trellis_recipe import (RECIPE_ID, TRELLIS_REVISION, MODEL_REVISION,
    DINO_REVISION, REQUIRED_COMPONENTS, FIXED_SHA256)

INTERFACE = 'trellis-image3d-v1'
_HASH = re.compile(r'[a-f0-9]{64}\Z')


def _text(value, maximum):
    if (type(value) is not str or not value or len(value.encode('utf-8', 'strict')) > maximum
            or any(ord(ch) < 32 or ord(ch) == 127 for ch in value)):
        raise ValueError('TRELLIS declaration unavailable')
    return value


def _relative(value):
    value = _text(value, 4096)
    if value.startswith('/') or '\\' in value or any(part in ('', '.', '..') for part in value.split('/')):
        raise ValueError('TRELLIS path unavailable')
    return value


def validate_declaration(settings):
    """Validate closed owner-reviewed settings; this performs no filesystem I/O."""
    if (type(settings) is not dict or set(settings) != {'interface','recipe','installationRoot','interpreter','manifest'}
            or settings['interface'] != INTERFACE or settings['recipe'] != RECIPE_ID):
        raise ValueError('TRELLIS declaration unavailable')
    root = _text(settings['installationRoot'], 4096)
    if not os.path.isabs(root) or os.path.normpath(root) != root:
        raise ValueError('TRELLIS root unavailable')
    interpreter, manifest = settings['interpreter'], settings['manifest']
    if (type(interpreter) is not dict or set(interpreter) != {'relativePath','sha256','version'}
            or not _HASH.fullmatch(interpreter.get('sha256', '') if type(interpreter.get('sha256')) is str else '')
            or type(interpreter.get('version')) is not str or not re.fullmatch(r'3\.10\.[0-9]+', interpreter['version'])):
        raise ValueError('TRELLIS interpreter unavailable')
    _relative(interpreter['relativePath'])
    if (type(manifest) is not dict or set(manifest) != {'version','sourceRevision','modelRevision','dinoRevision','components'}
            or type(manifest['version']) is not int or manifest['version'] != 1
            or manifest['sourceRevision'] != TRELLIS_REVISION or manifest['modelRevision'] != MODEL_REVISION
            or manifest['dinoRevision'] != DINO_REVISION or type(manifest['components']) is not list
            or len(manifest['components']) != len(REQUIRED_COMPONENTS)):
        raise ValueError('TRELLIS manifest unavailable')
    seen = set()
    for item in manifest['components']:
        if (type(item) is not dict or set(item) != {'role','relativePath','size','sha256','acquisitionOperationID'}
                or type(item['role']) is not str or item['role'] not in REQUIRED_COMPONENTS or item['role'] in seen
                or _relative(item['relativePath']) != REQUIRED_COMPONENTS[item['role']]
                or type(item['size']) is not int or not 1 <= item['size'] <= 2**53-1
                or type(item['sha256']) is not str or not _HASH.fullmatch(item['sha256'])
                or (item['role'] in FIXED_SHA256 and item['sha256'] != FIXED_SHA256[item['role']])):
            raise ValueError('TRELLIS component unavailable')
        receipt = item['acquisitionOperationID']
        if receipt is not None:
            _text(receipt, 128)
            if not item['relativePath'].startswith('model/'):
                raise ValueError('TRELLIS acquisition unavailable')
        seen.add(item['role'])
    if seen != set(REQUIRED_COMPONENTS): raise ValueError('TRELLIS component unavailable')
    return json.loads(json.dumps(settings, ensure_ascii=False, allow_nan=False))


def declaration_sha256(descriptor):
    if type(descriptor) is not dict or set(descriptor) != {'installationRoot','interpreter','manifest'}:
        raise ValueError('TRELLIS declaration unavailable')
    validate_declaration({'interface':INTERFACE, 'recipe':RECIPE_ID, **descriptor})
    raw=json.dumps(descriptor, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('utf-8', 'strict')
    if len(raw)>131072: raise ValueError('TRELLIS declaration unavailable')
    return hashlib.sha256(raw).hexdigest()


def _stat_file(root, relative, expected_size=None, executable=False):
    path=os.path.join(root, relative)
    if os.path.realpath(path) != path: raise ValueError('TRELLIS path unavailable')
    directories=[]
    parent=root
    for part in relative.split('/')[:-1]:
        parent=os.path.join(parent,part)
        entry=os.lstat(parent)
        if not stat.S_ISDIR(entry.st_mode): raise ValueError('TRELLIS path unavailable')
        directories.append((entry.st_dev,entry.st_ino,entry.st_ctime_ns))
    info=os.lstat(path)
    if (not stat.S_ISREG(info.st_mode) or (expected_size is not None and info.st_size != expected_size)
            or (executable and not info.st_mode & 0o111)):
        raise ValueError('TRELLIS component unavailable')
    return (tuple(directories),(info.st_dev,info.st_ino,info.st_size,info.st_mtime_ns,info.st_ctime_ns))


def _check_acquisitions(settings, owner, account):
    selected=[item for item in settings['manifest']['components'] if item['acquisitionOperationID'] is not None]
    if not selected: return
    from src.constants import COOKBOOK_STATE_FILE
    from src.openclank.cookbook_state import transaction, PRIVATE_KEY
    with transaction(COOKBOOK_STATE_FILE) as state:
        records=state.get(PRIVATE_KEY, {})
        for item in selected:
            row=records.get(item['acquisitionOperationID'])
            if (not row or row.get('owner') != owner or row.get('account_id') != account or row.get('state') != 'complete'
                    or (row.get('review') or {}).get('repoID') != 'microsoft/TRELLIS-image-large'
                    or (row.get('review') or {}).get('revision') != MODEL_REVISION
                    or (row.get('receipt') or {}).get('status') != 'complete'):
                raise ValueError('TRELLIS acquisition unavailable')
            name=item['relativePath'].removeprefix('model/')
            reviews=[entry for entry in row['review']['files'] if entry.get('path') == name]
            receipts=[entry for entry in row['receipt']['files'] if entry.get('path') == name]
            if (len(reviews)!=1 or len(receipts)!=1 or reviews[0].get('size')!=item['size']
                    or receipts[0] != {'path':name,'size':item['size'],'sha256':item['sha256']}):
                raise ValueError('TRELLIS acquisition changed')


@dataclass(frozen=True)
class TrellisProfile:
    ownerAccountID: str
    connectionID: str
    connectionRevision: int
    interface: str
    manifestSHA256: str
    interpreterSHA256: str
    interpreterVersion: str
    runnerSHA256: str
    recipeSHA256: str
    artifact_authority: str
    _descriptor: str = field(repr=False)
    _identities: tuple = field(repr=False)

    def private_descriptor(self):
        return json.loads(self._descriptor)

    def job_binding(self):
        return {'connectionID':self.connectionID,'connectionRevision':self.connectionRevision,
                'interface':self.interface,'ownerAccountID':self.ownerAccountID,'manifestSHA256':self.manifestSHA256}

    def child_profile(self):
        return {'adapter':INTERFACE,'recipe':RECIPE_ID,'connectionID':self.connectionID,
                'connectionRevision':self.connectionRevision,'ownerAccountID':self.ownerAccountID,
                'manifestSHA256':self.manifestSHA256,'interpreterSHA256':self.interpreterSHA256,
                'interpreterVersion':self.interpreterVersion,'runnerSHA256':self.runnerSHA256,
                'recipeSHA256':self.recipeSHA256,'artifact_authority':self.artifact_authority}


def _installed_current(*,store,auth,artifacts,owner,account,connection,revision,
                       family,interface,validate,acquisitions,descriptor_fields,digest_for,
                       runner_name,recipe_name,profile_factory,helper_name=None):
    """Existing account/profile/stat authority shared by finite installed recipes."""
    if auth.account_id(owner)!=account or auth.username_for_account_id(account)!=owner:
        raise ValueError('Local media account unavailable')
    row=store.get_connection(owner=owner,connection_id=connection)
    if (row.owner!=owner or row.id!=connection or row.revision!=revision or not row.enabled or row.deleted_at is not None
            or row.family_id!=family or row.adapter_id!=interface or row.kind!='local' or row.billing_lane!='local'
            or row.normalized_url is not None):
        raise ValueError('Local media profile unavailable')
    settings=validate(row.settings)
    acquisitions(settings,owner,account)
    descriptor={key:settings[key] for key in descriptor_fields}
    digest=digest_for(descriptor); root=settings['installationRoot']
    root_info=os.lstat(root)
    if os.path.realpath(root)!=root or not stat.S_ISDIR(root_info.st_mode): raise ValueError('Local media root unavailable')
    identities=[(root_info.st_dev,root_info.st_ino,root_info.st_ctime_ns),
                _stat_file(root,settings['interpreter']['relativePath'],executable=True)]
    identities.extend(_stat_file(root,item['relativePath'],item['size']) for item in settings['manifest']['components'])
    def fixed_sha(name):
        fd=os.open(Path(__file__).with_name(name),os.O_RDONLY | os.O_NOFOLLOW)
        try:
            before=os.fstat(fd)
            if not stat.S_ISREG(before.st_mode) or not 0 < before.st_size <= 128*1024:
                raise ValueError('Local media fixed source unavailable')
            with os.fdopen(os.dup(fd),'rb') as stream: data=stream.read(128*1024+1)
            after=os.fstat(fd)
            if (len(data)!=before.st_size or (before.st_dev,before.st_ino,before.st_size,before.st_mtime_ns,before.st_ctime_ns)
                    != (after.st_dev,after.st_ino,after.st_size,after.st_mtime_ns,after.st_ctime_ns)):
                raise ValueError('Local media fixed source changed')
            return hashlib.sha256(data).hexdigest()
        finally: os.close(fd)
    runner_sha=fixed_sha(runner_name); recipe_sha=fixed_sha(recipe_name)
    helper_sha=fixed_sha(helper_name) if helper_name is not None else None
    if auth.account_id(owner)!=account or auth.username_for_account_id(account)!=owner: raise ValueError('Local media account changed')
    return profile_factory(account,connection,revision,interface,digest,settings['interpreter']['sha256'],
        settings['interpreter']['version'],runner_sha,recipe_sha,artifact_authority(artifacts),
        json.dumps(descriptor,ensure_ascii=False,sort_keys=True,separators=(',',':')),tuple(identities),
        **({'helperSHA256':helper_sha} if helper_sha is not None else {}))


def _current(*,store,auth,artifacts,owner,account,connection,revision):
    return _installed_current(store=store,auth=auth,artifacts=artifacts,owner=owner,account=account,connection=connection,revision=revision,
        family='trellis',interface=INTERFACE,validate=validate_declaration,acquisitions=_check_acquisitions,
        descriptor_fields=('installationRoot','interpreter','manifest'),digest_for=declaration_sha256,
        runner_name='trellis_runner.py',recipe_name='trellis_recipe.py',profile_factory=TrellisProfile)


@asynccontextmanager
async def admitted_trellis_profile(*,store,auth_manager,account_barrier,artifact_store,owner,
                                   ownerAccountID,connectionID,connectionRevision,interface):
    if interface!=INTERFACE or account_barrier is None: raise ValueError('TRELLIS profile unavailable')
    async with account_barrier.admitted(owner):
        args=dict(store=store,auth=auth_manager,artifacts=artifact_store,owner=owner,account=ownerAccountID,
                  connection=connectionID,revision=connectionRevision)
        profile=await asyncio.to_thread(_current,**args)
        async def check():
            if await asyncio.to_thread(_current,**args)!=profile: raise ValueError('TRELLIS profile changed')
        yield profile,check
