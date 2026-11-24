"""Bounded Human-selected Files export; selectors never grant read authority."""
from __future__ import annotations
import asyncio
import hashlib
import io
import json
import re
import time
import zipfile
from fnmatch import fnmatchcase
from src.secret_storage import encrypt, decrypt

MAX_BYTES = 16 * 1024 * 1024
MAX_FILES = 16
MAX_SECONDS = 30


def _text(value, limit=1024):
    if not isinstance(value,str) or len(value.encode('utf-8','strict')) > limit or any(ord(c)<32 and c not in '\n\t' for c in value):
        raise ValueError('Review text unavailable')
    return value.strip()


def _name(value):
    if not isinstance(value,str) or not value or len(value.encode('utf-8','strict')) > 240:
        raise ValueError('File name unavailable')
    if (value in ('.','..') or value.endswith(('.', ' ')) or any(ord(c)<32 or ord(c)==127 or c in '/\\:<>"|?*' for c in value)
        or re.fullmatch(r'(?i:con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\..*)?',value)):
        raise ValueError('File name unavailable')
    return value


def _finding(name):
    # Finite basename criteria from scripts/check_release_artifacts.py; not a content scan.
    n=name.lower()
    if n in {'.env.example','secrets.env.example'}: return []
    if n == '.env' or fnmatchcase(n,'.env.bak.*') or fnmatchcase(n,'.env.bak-*') or fnmatchcase(n,'.env.*-source') or n.startswith('secrets.env.') or fnmatchcase(n,'history-credentials*.json') or fnmatchcase(n,'.history-credentials*.tmp'):
        return ['selected filename matches a release hygiene sensitive-name criterion']
    return []


async def _capture(files, ctx, selected, current):
    if not isinstance(selected,list) or not 1 <= len(selected) <= MAX_FILES: raise ValueError('Selection unavailable')
    rows=[]; total=0; seen=set()
    async def check(item, expected_origin=None):
        if not await current() or not files._generation_current(ctx): raise ValueError('Authority changed')
        provider, ref=files._provider_for_ref(ctx,item['ref'],capability='download')
        stat_provider, stat_ref=files._provider_for_ref(ctx,item['ref'],capability='stat')
        if (provider is not stat_provider or ref != stat_ref or ref.provider != 'host' or ref.kind != 'file'
            or ref.stable_id != item['id'] or (expected_origin is not None and ref.origin_id != expected_origin)):
            raise ValueError('Selection unavailable')
        entry=await provider.stat(ctx,origin_id=ref.origin_id)
        observed=await provider.stat_inspection(ctx,origin_id=ref.origin_id)
        if (entry.origin_id != ref.origin_id or entry.kind != 'file' or dict(entry.revision or {}) != item['revision']
            or observed.origin_id != ref.origin_id or observed.kind != 'file' or type(observed.size) is not int or observed.size < 0
            or observed.size != entry.size or (observed.revision or {}).get('kind') != 'hostObjectIdentity'
            or not re.fullmatch(r'[0-9a-f]{64}',str((observed.revision or {}).get('value') or ''))): raise ValueError('File changed')
        return provider,ref,entry,observed
    async with asyncio.timeout(MAX_SECONDS):
        for index,item in enumerate(selected):
            if type(item) is not dict or set(item) != {'ref','id','revision'} or not isinstance(item['ref'],str) or not isinstance(item['id'],str) or type(item['revision']) is not dict:
                raise ValueError('Selection unavailable')
            provider,ref,entry,observed=await check(item)
            if ref.origin_id in seen: raise ValueError('Duplicate file')
            seen.add(ref.origin_id); total+=observed.size
            if total > MAX_BYTES: raise ValueError('Selection exceeds export limit')
            name=_name(str(entry.name)); source=await provider.content(ctx,origin_id=ref.origin_id)
            if source.origin_id != ref.origin_id or source.size != observed.size or source.etag != observed.revision['value'] or source.stream is None or source.data is not None or source.path is not None:
                raise ValueError('Opened file changed')
            chunks=[]; size=0; digest=hashlib.sha256()
            async for chunk in source.stream(0,observed.size+1):
                if not isinstance(chunk,bytes) or not chunk: raise ValueError('Invalid file stream')
                size+=len(chunk)
                if size > observed.size: raise ValueError('File changed')
                digest.update(chunk);chunks.append(chunk)
                if not await current(): raise ValueError('Authority changed')
            if size != observed.size: raise ValueError('File changed')
            fp='sha256:'+digest.hexdigest()+':'+str(size)
            if item['revision'] != {'kind':'hostFingerprint','value':fp}: raise ValueError('File changed')
            rows.append({'item':item,'origin':ref.origin_id,'object':observed.revision,'data':b''.join(chunks),
                         'public':{'resourceID':ref.stable_id,'revision':dict(item['revision']),'name':name,
                            'archivePath':f'files/{index+1:04d}/{name}','sha256':digest.hexdigest(),'size':size,'findings':_finding(name)}})
        for row in rows:
            _,_,_,observed=await check(row['item'],row['origin'])
            if observed.revision != row['object'] or observed.size != len(row['data']): raise ValueError('File changed')
        if not await current(): raise ValueError('Authority changed')
    return rows


async def preview(files,ctx,items,omissions,license_text,attribution,privacy,current):
    if privacy is not True: raise ValueError('Privacy review is required')
    if not isinstance(omissions,list) or len(omissions)>32: raise ValueError('Omissions unavailable')
    review={'omissions':[_text(x,512) for x in omissions], 'license':_text(license_text,2048),
            'attribution':_text(attribution,2048), 'privacy':'human_reviewed_not_certified'}
    rows=await _capture(files,ctx,items,current)
    now=int(time.time())
    plan={'v':1,'account':ctx.owner_subject_id,'workspace':ctx.workspace_id,'generation':ctx.policy_generation,'issued':now,'expires':now+600,
          'items':items,'review':review,'members':[r['public'] for r in rows]}
    encoded=encrypt(json.dumps(plan,ensure_ascii=False,sort_keys=True,separators=(',',':')))
    if not encoded.startswith('enc:'): raise ValueError('Preview unavailable')
    return {'version':1,'previewToken':'mw4p1.'+encoded[4:],'members':plan['members'],'review':review,
            'limitations':['Content secret scanning and license verification are unavailable.','Files were observed over an interval, not atomically.']}


async def export(files,ctx,token,current,*,first_ref):
    if not isinstance(token,str) or not token.startswith('mw4p1.') or len(token)>600000: raise ValueError('Preview unavailable')
    raw=decrypt('enc:'+token[6:])
    plan=json.loads(raw)
    now=int(time.time())
    if (type(plan) is not dict or set(plan)!={'v','account','workspace','generation','issued','expires','items','review','members'}
        or not isinstance(plan['items'],list) or not plan['items'] or plan['items'][0].get('ref') != first_ref
        or plan['v']!=1 or plan['account']!=ctx.owner_subject_id or plan['workspace']!=ctx.workspace_id or plan['generation']!=ctx.policy_generation
        or type(plan['issued']) is not int or type(plan['expires']) is not int or plan['issued']>now or plan['expires']<=now or plan['expires']-plan['issued']>600): raise ValueError('Preview unavailable')
    rows=await _capture(files,ctx,plan['items'],current)
    members=[r['public'] for r in rows]
    if members != plan['members'] or not await current(): raise ValueError('Reviewed files changed')
    manifest={'version':1,'kind':'selected_outputs','coverage':'explicit_selection_only','members':members,'review':plan['review'],
              'limitations':['Content secret scanning and license verification are unavailable.','Files were observed over an interval, not atomically.']}
    output=io.BytesIO()
    with zipfile.ZipFile(output,'w',compression=zipfile.ZIP_STORED) as archive:
        for row in rows: archive.writestr(row['public']['archivePath'],row['data'])
        archive.writestr('openclank-manifest.json',json.dumps(manifest,ensure_ascii=True,separators=(',',':')).encode())
    # Recheck all source bindings after assembly before exposing bytes.
    for row in rows:
        provider,ref=files._provider_for_ref(ctx,row['item']['ref'],capability='download')
        stat_provider,stat_ref=files._provider_for_ref(ctx,row['item']['ref'],capability='stat')
        if provider is not stat_provider or ref != stat_ref: raise ValueError('Reviewed files changed')
        observed=await provider.stat_inspection(ctx,origin_id=ref.origin_id)
        entry=await provider.stat(ctx,origin_id=ref.origin_id)
        if ref.origin_id != row['origin'] or observed.revision != row['object'] or observed.size != len(row['data']) or dict(entry.revision or {}) != row['item']['revision'] or not await current(): raise ValueError('Reviewed files changed')
    return output.getvalue()
