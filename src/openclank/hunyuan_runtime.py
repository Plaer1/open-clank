"""Owner-bound configured local Hunyuan shape profile; no Host model loads."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from contextlib import asynccontextmanager
import hashlib
import json
import os
import re

from src.openclank.hunyuan_recipe import RECIPE_ID, SOURCE_REVISION, MODEL_REVISION, REQUIRED_COMPONENTS, FIXED_SHA256, FIXED_SIZES, DEVICES
from src.openclank.trellis_runtime import TrellisProfile, _HASH, _relative, _text, _installed_current

INTERFACE = 'hunyuan3d-v2-shape-v1'


def validate_declaration(settings):
    if (type(settings) is not dict or set(settings)!={'interface','recipe','installationRoot','device','interpreter','manifest'}
            or settings['interface']!=INTERFACE or settings['recipe']!=RECIPE_ID or settings['device'] not in DEVICES):
        raise ValueError('Hunyuan declaration unavailable')
    root=_text(settings['installationRoot'],4096)
    if not os.path.isabs(root) or os.path.normpath(root)!=root: raise ValueError('Hunyuan root unavailable')
    interpreter,manifest=settings['interpreter'],settings['manifest']
    if (type(interpreter) is not dict or set(interpreter)!={'relativePath','sha256','version'}
            or type(interpreter.get('sha256')) is not str or not _HASH.fullmatch(interpreter['sha256'])
            or type(interpreter.get('version')) is not str or not re.fullmatch(r'3\.10\.[0-9]+',interpreter['version'])):
        raise ValueError('Hunyuan interpreter unavailable')
    _relative(interpreter['relativePath'])
    if (type(manifest) is not dict or set(manifest)!={'version','sourceRevision','modelRevision','components'}
            or type(manifest['version']) is not int or manifest['version']!=1
            or manifest['sourceRevision']!=SOURCE_REVISION or manifest['modelRevision']!=MODEL_REVISION
            or type(manifest['components']) is not list or len(manifest['components'])!=len(REQUIRED_COMPONENTS)):
        raise ValueError('Hunyuan manifest unavailable')
    seen=set()
    for item in manifest['components']:
        if (type(item) is not dict or set(item)!={'role','relativePath','size','sha256','acquisitionOperationID'}
                or type(item['role']) is not str or item['role'] not in REQUIRED_COMPONENTS or item['role'] in seen
                or _relative(item['relativePath'])!=REQUIRED_COMPONENTS[item['role']]
                or type(item['size']) is not int or not 1<=item['size']<=2**53-1
                or item['sha256']!=FIXED_SHA256[item['role']]
                or (item['role'] in FIXED_SIZES and item['size']!=FIXED_SIZES[item['role']])):
            raise ValueError('Hunyuan component unavailable')
        receipt=item['acquisitionOperationID']
        if receipt is not None:
            _text(receipt,128)
            if not item['relativePath'].startswith('model/'): raise ValueError('Hunyuan acquisition unavailable')
        seen.add(item['role'])
    if seen!=set(REQUIRED_COMPONENTS): raise ValueError('Hunyuan component unavailable')
    return json.loads(json.dumps(settings,ensure_ascii=False,allow_nan=False))


def declaration_sha256(descriptor):
    if type(descriptor) is not dict or set(descriptor)!={'installationRoot','device','interpreter','manifest'}:
        raise ValueError('Hunyuan declaration unavailable')
    validate_declaration({'interface':INTERFACE,'recipe':RECIPE_ID,**descriptor})
    raw=json.dumps(descriptor,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False).encode('utf-8','strict')
    if len(raw)>131072: raise ValueError('Hunyuan declaration unavailable')
    return hashlib.sha256(raw).hexdigest()


def _check_acquisitions(settings,owner,account):
    selected=[x for x in settings['manifest']['components'] if x['acquisitionOperationID'] is not None]
    if not selected:return
    from src.constants import COOKBOOK_STATE_FILE
    from src.openclank.cookbook_state import transaction, PRIVATE_KEY
    with transaction(COOKBOOK_STATE_FILE) as state:
        records=state.get(PRIVATE_KEY,{})
        for item in selected:
            row=records.get(item['acquisitionOperationID']); review=(row or {}).get('review') or {}; receipt=(row or {}).get('receipt') or {}
            if (not row or row.get('owner')!=owner or row.get('account_id')!=account or row.get('state')!='complete'
                    or review.get('repoID')!='tencent/Hunyuan3D-2' or review.get('revision')!=MODEL_REVISION or receipt.get('status')!='complete'):
                raise ValueError('Hunyuan acquisition unavailable')
            name=item['relativePath'].removeprefix('model/')
            matches=[entry for entry in review['files'] if entry.get('path')==name]
            outputs=[entry for entry in receipt['files'] if entry.get('path')==name]
            if len(matches)!=1 or len(outputs)!=1 or matches[0].get('size')!=item['size'] or outputs[0]!={'path':name,'size':item['size'],'sha256':item['sha256']}:
                raise ValueError('Hunyuan acquisition changed')


@dataclass(frozen=True)
class HunyuanProfile(TrellisProfile):
    helperSHA256: str
    def job_binding(self):
        return {**super().job_binding(),'device':self.private_descriptor()['device']}
    def child_profile(self):
        return {**super().child_profile(),'adapter':INTERFACE,'recipe':RECIPE_ID,'device':self.private_descriptor()['device'],'helperSHA256':self.helperSHA256}


def _current(*,store,auth,artifacts,owner,account,connection,revision):
    return _installed_current(store=store,auth=auth,artifacts=artifacts,owner=owner,account=account,connection=connection,revision=revision,
        family='hunyuan-shape',interface=INTERFACE,validate=validate_declaration,acquisitions=_check_acquisitions,
        descriptor_fields=('installationRoot','device','interpreter','manifest'),digest_for=declaration_sha256,
        runner_name='hunyuan_runner.py',recipe_name='hunyuan_recipe.py',profile_factory=HunyuanProfile,helper_name='trellis_runner.py')


@asynccontextmanager
async def admitted_hunyuan_profile(*,store,auth_manager,account_barrier,artifact_store,owner,ownerAccountID,connectionID,connectionRevision,interface):
    if interface!=INTERFACE or account_barrier is None: raise ValueError('Hunyuan profile unavailable')
    async with account_barrier.admitted(owner):
        args=dict(store=store,auth=auth_manager,artifacts=artifact_store,owner=owner,account=ownerAccountID,connection=connectionID,revision=connectionRevision)
        profile=await asyncio.to_thread(_current,**args)
        async def check():
            if await asyncio.to_thread(_current,**args)!=profile: raise ValueError('Hunyuan profile changed')
        yield profile,check
