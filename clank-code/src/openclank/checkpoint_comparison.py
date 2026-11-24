"""Bounded read-only comparison of one already-authorized checkpoint item."""
from __future__ import annotations

import asyncio
import hashlib
import re
from typing import Any

from src.openclank.files_service_client import _checkpoint_owned_thread

_LIMIT = 256 * 1024
_CHUNK = 64 * 1024
_FINGERPRINT = re.compile(r'^sha256:([0-9a-f]{64}):(0|[1-9][0-9]*)$')


def _text_state(data: bytes, fingerprint: str) -> dict[str, Any]:
    if b'\0' in data:
        return {'state': 'binary', 'fingerprint': fingerprint}
    try:
        value = data.decode('utf-8', 'strict')
    except UnicodeDecodeError:
        return {'state': 'binary', 'fingerprint': fingerprint}
    return {'state': 'text', 'text': value, 'fingerprint': fingerprint}


async def compare_checkpoint_file(source: dict[str, Any]) -> dict[str, Any]:
    """Read exact historical bytes and a separately authorized current object.

    The caller supplies a server-private, original-ledger-qualified source and
    retains its account admission. No private identity is returned to the UI.
    """
    keys = {'resource_id','request_id','native_resource_id','version_ref','fingerprint','size','history','open_current','current','current_file','source_current'}
    if type(source) is not dict or set(source) != keys:
        raise ValueError('checkpoint comparison unavailable')
    resource, request_id = source['resource_id'], source['request_id']
    native, version, fingerprint, size = source['native_resource_id'], source['version_ref'], source['fingerprint'], source['size']
    if (type(resource) is not str or not resource.startswith('resource-') or len(resource) > 256
            or type(request_id) is not str or not re.fullmatch(r'[A-Za-z0-9_-]{16,80}',request_id)
            or type(native) is not str or not native or len(native) > 1024
            or type(version) is not str or not version or len(version) > 1024
            or type(fingerprint) is not str or (match := _FINGERPRINT.fullmatch(fingerprint)) is None
            or type(size) is not int or size < 0 or size > 16 * 1024 * 1024 or int(match.group(2)) != size
            or not all(callable(source[name]) for name in ('open_current','current','current_file','source_current'))
            or not callable(getattr(source['history'],'read_resource_version_chunk',None))):
        raise ValueError('checkpoint comparison unavailable')

    async def current() -> None:
        if await source['current']() is not True:
            raise ValueError('checkpoint comparison authority changed')

    async def checkpoint() -> dict[str, Any]:
        if size > _LIMIT:
            return {'state':'oversize','fingerprint':fingerprint}
        data = bytearray(); offset = 0
        while True:
            await current()
            length = min(_CHUNK, max(1, size - offset))
            chunk = await _checkpoint_owned_thread(lambda: source['history'].read_resource_version_chunk(native, version, offset=offset, length=length))
            await current()
            if (type(chunk) is not dict or type(chunk.get('version')) is not dict or chunk['version'].get('id') != version
                    or chunk['version'].get('operation') != 'selected_checkpoint_v1' or chunk['version'].get('phase') != 'checkpoint'
                    or chunk['version'].get('source_provider') != 'odysseus-files' or chunk['version'].get('fingerprint') != fingerprint
                    or chunk['version'].get('size_bytes') != size or chunk['version'].get('availability') != 'available' or chunk['version'].get('can_read') is not True
                    or chunk.get('offset') != offset or type(chunk.get('eof')) is not bool):
                raise ValueError('checkpoint version unavailable')
            content = chunk.get('content')
            if not isinstance(content, bytes) or len(content) > length or len(data) + len(content) > size:
                raise ValueError('checkpoint version unavailable')
            data.extend(content); offset += len(content)
            if chunk['eof']:
                break
            if not content or offset >= size:
                raise ValueError('checkpoint version incomplete')
        if len(data) != size or hashlib.sha256(data).hexdigest() != match.group(1):
            raise ValueError('checkpoint version changed')
        return _text_state(bytes(data),fingerprint)

    async def live() -> dict[str, Any]:
        await current()
        content = await source['open_current']()
        await current()
        if type(content) is dict:
            if (set(content)!={'oversize','origin_id','size','etag'} or content['oversize'] is not True
                    or type(content['origin_id']) is not str or not content['origin_id']
                    or type(content['size']) is not int or content['size']<=_LIMIT
                    or type(content['etag']) is not str or re.fullmatch(r'[0-9a-f]{64}',content['etag']) is None
                    or await source['current_file'](content) is not True):
                raise ValueError('current file unavailable')
            await current()
            return {'state':'oversize'}
        length = getattr(content,'size',None)
        if (type(length) is not int or length < 0 or not isinstance(getattr(content,'etag',None),str)
                or not content.etag or not callable(getattr(content,'stream',None))
                or getattr(content,'data',None) is not None or getattr(content,'path',None) is not None):
            raise ValueError('current file unavailable')
        if length > _LIMIT:
            if await source['current_file'](content) is not True: raise ValueError('current file changed')
            await current()
            return {'state':'oversize'}
        data = bytearray()
        if length:
            async for chunk in content.stream(0,length+1):
                if not isinstance(chunk,bytes) or len(data)+len(chunk)>length: raise ValueError('current file changed')
                data.extend(chunk)
        if len(data)!=length or await source['current_file'](content) is not True:
            raise ValueError('current file changed')
        await current()
        actual=f'sha256:{hashlib.sha256(data).hexdigest()}:{len(data)}'
        return _text_state(bytes(data),actual)

    await current()
    if await source['source_current']() is not True: raise ValueError('checkpoint source changed')
    try: historical = await checkpoint()
    except asyncio.CancelledError: raise
    except Exception: historical = {'state':'unavailable'}
    await current()
    try: present = await live()
    except asyncio.CancelledError: raise
    except Exception: present = {'state':'unavailable'}
    await current()
    if await source['source_current']() is not True: raise ValueError('checkpoint source changed')
    await current()
    return {'version':1,'coverage':'one_selected_checkpoint_file','resource_id':resource,'request_id':request_id,
            'checkpoint':historical,'current':present}
