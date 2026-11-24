"""Bounded public Hugging Face metadata, never credentials or model bytes."""
from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime, timezone
from urllib.parse import quote

import httpx

ORIGIN = 'https://huggingface.co'
MAX_BODY = 2 * 1024 * 1024
MAX_FILES = 1000
DISPLAY_FILES = 100
_SHA = re.compile(r'^[0-9a-f]{40}$')
_HEX = re.compile(r'^[0-9a-f]{64}$')


class HubUnavailable(ValueError):
    pass


async def public_json(path, *, params=None):
    if not path.startswith('/api/models') or '?' in path or '#' in path:
        raise HubUnavailable('Public Hub request unavailable')
    try:
        async with asyncio.timeout(15):
            async with httpx.AsyncClient(base_url=ORIGIN, timeout=15.0, follow_redirects=False, trust_env=False, headers={'Accept':'application/json'}) as client:
                async with client.stream('GET', path, params=params) as response:
                    if response.status_code != 200:
                        raise HubUnavailable('Public Hub metadata unavailable')
                    pieces = []; total = 0
                    async for piece in response.aiter_bytes():
                        total += len(piece)
                        if total > MAX_BODY: raise HubUnavailable('Public Hub response exceeds the supported limit')
                        pieces.append(piece)
    except (httpx.HTTPError, TimeoutError) as exc:
        raise HubUnavailable('Public Hub metadata unavailable') from exc
    try:
        return json.loads(b''.join(pieces))
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise HubUnavailable('Public Hub response is invalid') from exc


def _safe_text(value, limit=256):
    if not isinstance(value, str) or not value:
        return None
    try:
        if len(value.encode('utf-8')) > limit or any(ord(c) < 32 for c in value): return None
    except UnicodeError:
        return None
    return value


def _path(value):
    value = _safe_text(value, 1024)
    if not value or '\\' in value or value.startswith('/') or any(part in {'', '.', '..'} for part in value.split('/')):
        raise HubUnavailable('Public Hub file declaration is unsafe')
    return value


async def manifest(repo_id):
    if (not isinstance(repo_id, str) or len(repo_id.encode('utf-8')) > 256
        or not re.fullmatch(r'[A-Za-z0-9._-]+/[A-Za-z0-9._-]+', repo_id)
        or any(part in {'.','..'} for part in repo_id.split('/'))):
        raise HubUnavailable('Invalid public repository identity')
    encoded = '/'.join(quote(part, safe='') for part in repo_id.split('/'))
    head = await public_json('/api/models/' + encoded)
    if (not isinstance(head, dict) or head.get('private') is True or (head.get('id') or head.get('modelId')) != repo_id
        or not isinstance(head.get('sha'), str) or not _SHA.fullmatch(head['sha'])):
        raise HubUnavailable('Public repository revision unavailable')
    sha = head['sha']
    data = await public_json('/api/models/' + encoded + '/revision/' + sha, params={'blobs':'true'})
    if not isinstance(data, dict) or (data.get('id') or data.get('modelId')) != repo_id or data.get('sha') != sha or data.get('private') is True:
        raise HubUnavailable('Public repository revision changed')
    raw_files = data.get('siblings')
    files = []
    if raw_files is not None:
        if not isinstance(raw_files, list): raise HubUnavailable('Public file inventory is invalid')
        for item in raw_files[:MAX_FILES]:
            if not isinstance(item, dict): raise HubUnavailable('Public file declaration is invalid')
            size = item.get('size')
            if type(size) is not int or not 0 <= size <= 2**53-1: size = None
            blob = item.get('blobId')
            if not isinstance(blob, str) or not _SHA.fullmatch(blob): blob = None
            lfs = item.get('lfs') if isinstance(item.get('lfs'), dict) else {}
            digest = lfs.get('sha256') or lfs.get('oid')
            if isinstance(digest, str) and digest.startswith('sha256:'): digest = digest[7:]
            if not isinstance(digest, str) or not _HEX.fullmatch(digest): digest = None
            files.append({'path':_path(item.get('rfilename')), 'size':size, 'blobID':blob, 'lfsSHA256':digest})
    card = data.get('cardData') if isinstance(data.get('cardData'), dict) else {}
    license_value = _safe_text(card.get('license'), 128)
    license_source = 'cardData' if license_value else None
    if not license_value and isinstance(data.get('tags'), list):
        candidates = [_safe_text(tag[8:], 128) for tag in data['tags'][:1000] if isinstance(tag, str) and tag.startswith('license:')]
        candidates = sorted({value for value in candidates if value})
        if len(candidates) == 1: license_value, license_source = candidates[0], 'tag'
    gated = data.get('gated')
    gating = 'gated' if gated is True or gated in ('auto','manual') else 'ungated' if gated is False else 'unknown'
    shown = files[:DISPLAY_FILES]
    return {'repoID':repo_id, 'revision':sha, 'task':_safe_text(data.get('pipeline_tag')),
        'library':_safe_text(data.get('library_name')),
        'license':{'value':license_value, 'source':license_source, 'status':'declared' if license_value else 'unknown'},
        'gating':{'state':gating}, 'files':shown,
        'coverage':{'state':'partial' if raw_files is not None and len(raw_files) > DISPLAY_FILES else 'unknown', 'returned':len(shown),
                    'omitted':len(raw_files)-len(shown) if raw_files is not None else None},
        'observedAt':datetime.now(timezone.utc).isoformat()}
