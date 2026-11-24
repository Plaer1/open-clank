"""Private bounded retained-after bytes for one qualified recorded Files member."""
from __future__ import annotations

import hashlib
import re

from src.openclank.history_change_qualifier import qualify_selected_change
from src.openclank.history_change_sets import parse_locator
from src.openclank.history_io import bounded_history_io


async def capture_recorded_after(locator, selected_resource_id, *, client, account, context, provider, current):
    """Return private recorded bytes and mapping; the caller must independently bind its workspace."""
    async def qualify():
        if current() is not True: raise ValueError('history authority changed')
        result = await qualify_selected_change(locator, selected_resource_id, client=client, account=account,
            context=context, provider=provider, current=current)
        if current() is not True: raise ValueError('history authority changed')
        return result

    selected = await qualify()
    identity = parse_locator(locator)
    item = selected['item']
    after = item.get('after') if type(item) is dict else None
    if (type(item) is not dict or item.get('action_id') != identity['actionID'] or item.get('ambiguous_phase') is not False
            or type(after) is not dict or after.get('action_id') != identity['actionID'] or after.get('phase') != 'after'
            or after.get('source_provider') not in {'odysseus-files','mimo-filesystem'} or after.get('resource_type') != 'file'
            or after.get('availability') != 'available' or after.get('can_read') is not True
            or type(after.get('id')) is not str or not after['id'] or len(after['id']) > 1024
            or type(after.get('version_id')) is not str or not after['version_id']
            or (identity['version'] == 1 and after['version_id'] != identity['afterVersionID'])):
        raise ValueError('recorded after source unavailable')
    size = after.get('size_bytes')
    if type(size) is not int or not 1 <= size <= 1024 * 1024:
        raise ValueError('recorded after source unavailable')
    fingerprint = after.get('fingerprint')
    match = re.fullmatch(r'sha256:([0-9a-f]{64}):(0|[1-9][0-9]*)', fingerprint) if type(fingerprint) is str else None
    if not match or int(match.group(2)) != size:
        raise ValueError('recorded after source unavailable')
    parts = []; offset = 0
    while offset < size:
        if current() is not True: raise ValueError('history authority changed')
        length = min(256 * 1024, size - offset)
        chunk = await bounded_history_io(lambda offset=offset, length=length:client.read_resource_version_chunk(
            selected['resource_id'], after['id'], offset=offset, length=length))
        if current() is not True: raise ValueError('history authority changed')
        data = chunk.get('content') if type(chunk) is dict else None
        if (type(chunk) is not dict or chunk.get('version') != after or chunk.get('offset') != offset
                or type(data) is not bytes or not data or len(data) > length
                or chunk.get('eof') is not (offset + len(data) == size)):
            raise ValueError('recorded after source changed')
        parts.append(data); offset += len(data)
    data = b''.join(parts)
    if len(data) != size or hashlib.sha256(data).hexdigest() != match.group(1):
        raise ValueError('recorded after source changed')
    if await qualify() != selected: raise ValueError('history source changed')
    return {'qualified':selected,'bytes':data,'sha256':match.group(1),'size':size}


async def digest_recorded_after(locator, selected_resource_id, *, client, account, context, provider, current):
    """Return a bounded retained-after digest; never materialize its bytes here."""
    async def qualify():
        if current() is not True: raise ValueError('history authority changed')
        selected=await qualify_selected_change(locator,selected_resource_id,client=client,account=account,context=context,provider=provider,current=current)
        if current() is not True: raise ValueError('history authority changed')
        return selected
    selected=await qualify(); identity=parse_locator(locator); item=selected['item']; version=item.get('after') if type(item) is dict else None
    if (type(item) is not dict or item.get('action_id')!=identity['actionID'] or item.get('ambiguous_phase') is not False
            or type(version) is not dict or version.get('action_id')!=identity['actionID'] or version.get('phase')!='after'
            or version.get('source_provider') not in {'odysseus-files','mimo-filesystem'} or version.get('resource_type')!='file'
            or version.get('availability')!='available' or version.get('can_read') is not True
            or type(version.get('id')) is not str or not version['id'] or len(version['id'])>1024
            or type(version.get('version_id')) is not str or not version['version_id']
            or (identity['version']==1 and version['version_id']!=identity['afterVersionID'])): raise ValueError('recorded after source unavailable')
    size=version.get('size_bytes'); fingerprint=version.get('fingerprint')
    match=re.fullmatch(r'sha256:([0-9a-f]{64}):(0|[1-9][0-9]*)',fingerprint) if type(fingerprint) is str else None
    if type(size) is not int or not 0<=size<=64*1024*1024 or not match or int(match.group(2))!=size: raise ValueError('recorded after source unavailable')
    result=await bounded_history_io(lambda:client.digest_resource_version(selected['resource_id'],version['id']),timeout=5.5)
    if (current() is not True or result!={'version':version,'sha256':match.group(1),'size':size} or await qualify()!=selected):
        raise ValueError('history source changed')
    return {'qualified':selected,'sha256':result['sha256'],'size':size}


async def verify_current_text(path, expected, *, provider, context, current):
    """Bounded decode of the independently authorized current Files revision."""
    import asyncio, codecs
    origin_entry=await provider.resource_for_path(context,path=path)
    if origin_entry.revision!=expected or current() is not True: raise ValueError('Recorded source changed')
    origin=origin_entry.origin_id
    before=await provider.stat_inspection(context,origin_id=origin)
    content=await provider.content(context,origin_id=origin)
    if (before.kind!='file' or before.origin_id!=origin or type(before.size) is not int or not 0<=before.size<=64*1024*1024
            or content.origin_id!=origin or content.size!=before.size or content.etag!=before.revision.get('value') or content.stream is None):
        raise ValueError('Recorded source unavailable')
    fingerprint=re.fullmatch(r'sha256:([0-9a-f]{64}):(0|[1-9][0-9]*)',str(expected.get('value',''))) if type(expected) is dict and expected.get('kind')=='hostFingerprint' else None
    if not fingerprint or int(fingerprint.group(2))!=before.size: raise ValueError('Recorded source changed')
    sha=hashlib.sha256();total=0;prefix=bytearray();decoder=None
    async with asyncio.timeout(5):
        async for chunk in content.stream(0,before.size):
            if type(chunk) is not bytes or total+len(chunk)>before.size: raise ValueError('Recorded source changed')
            total+=len(chunk);sha.update(chunk)
            if decoder is None:
                prefix.extend(chunk)
                if len(prefix)<3 and total<before.size: continue
                encoding='utf-16' if prefix.startswith((b'\xff\xfe',b'\xfe\xff')) else 'utf-8-sig'
                decoder=codecs.getincrementaldecoder(encoding)(errors='strict');decoded=decoder.decode(bytes(prefix),final=False);prefix.clear()
            else: decoded=decoder.decode(chunk,final=False)
            if '\x00' in decoded: raise ValueError('Recorded source is not text')
        if decoder is None:
            encoding='utf-16' if prefix.startswith((b'\xff\xfe',b'\xfe\xff')) else 'utf-8-sig'
            decoder=codecs.getincrementaldecoder(encoding)(errors='strict');decoded=decoder.decode(bytes(prefix),final=True)
        else: decoded=decoder.decode(b'',final=True)
        if '\x00' in decoded: raise ValueError('Recorded source is not text')
    after=await provider.stat_inspection(context,origin_id=origin)
    if total!=before.size or sha.hexdigest()!=fingerprint.group(1) or after.revision!=before.revision or after.size!=before.size or current() is not True:
        raise ValueError('Recorded source changed')
