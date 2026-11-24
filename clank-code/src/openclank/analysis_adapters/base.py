"""Bounded, private source capture for an admitted native-analysis call."""
from __future__ import annotations
import hashlib
from dataclasses import dataclass
from typing import Any, Mapping

MAX_SOURCE_BYTES = 16 * 1024 * 1024

class CaptureUnavailable(ValueError):
    pass

@dataclass(frozen=True)
class CapturedSource:
    resource_ref: str
    revision: dict[str, Any]
    sha256: str
    data: bytes


async def _bounded_identity(facade, provider, context, ref):
    """Reject oversized files with bounded descriptor metadata before hashing."""
    inspect = getattr(provider, 'stat_inspection', None)
    if not callable(inspect):
        raise CaptureUnavailable('Bounded Host identity is unavailable')
    entry = await inspect(context, origin_id=ref.origin_id)
    revision = facade._provider_revision(entry)
    if (entry.origin_id != ref.origin_id or entry.kind != 'file' or type(entry.size) is not int
        or not 0 < entry.size <= MAX_SOURCE_BYTES or not isinstance(revision, Mapping)
        or revision.get('kind') != 'hostObjectIdentity' or not isinstance(revision.get('value'), str)):
        raise CaptureUnavailable('Source is stale or exceeds the capture limit')
    return entry.size, dict(revision)

async def capture_source(facade, context, resource_ref: str, revision: Mapping[str, Any]) -> CapturedSource:
    provider, ref = facade._provider_for_ref(context, resource_ref, capability='download')
    if ref.provider != 'host':
        raise CaptureUnavailable('Only authorized Host files are supported')
    size, identity = await _bounded_identity(facade, provider, context, ref)
    before = await provider.stat(context, origin_id=ref.origin_id)
    if (before.origin_id != ref.origin_id or before.kind != 'file' or facade._provider_revision(before) != dict(revision)
        or type(before.size) is not int or before.size != size
        or await _bounded_identity(facade, provider, context, ref) != (size, identity)):
        raise CaptureUnavailable('Source is stale or exceeds the capture limit')
    source = await provider.content(context, origin_id=ref.origin_id)
    if (source.origin_id != ref.origin_id or source.size != before.size or source.etag != identity['value']
        or source.stream is None or source.data is not None or source.path is not None):
        raise CaptureUnavailable('Bounded Host source stream is unavailable')
    parts=[]; total=0; digest=hashlib.sha256()
    async for chunk in source.stream(0,before.size):
        if not isinstance(chunk,bytes): raise CaptureUnavailable('Source stream is invalid')
        total += len(chunk)
        if total > before.size or total > MAX_SOURCE_BYTES: raise CaptureUnavailable('Source stream exceeded its bound')
        parts.append(chunk); digest.update(chunk)
    if total != before.size or not await source_current(facade,context,resource_ref,revision):
        raise CaptureUnavailable('Source changed during capture')
    return CapturedSource(resource_ref,dict(revision),digest.hexdigest(),b''.join(parts))

async def source_current(facade, context, resource_ref: str, revision: Mapping[str, Any]) -> bool:
    try:
        provider, ref = facade._provider_for_ref(context, resource_ref, capability='download')
        if ref.provider != 'host' or not facade._generation_current(context): return False
        size, identity = await _bounded_identity(facade, provider, context, ref)
        current = await provider.stat(context,origin_id=ref.origin_id)
        return (current.origin_id == ref.origin_id and current.kind == 'file' and current.size == size
            and facade._provider_revision(current) == dict(revision)
            and await _bounded_identity(facade, provider, context, ref) == (size, identity)
            and facade._generation_current(context))
    except Exception:
        return False
