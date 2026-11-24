"""Stateless source-owner review and confirmation for one prepared item."""
import asyncio
import hashlib

from services.memory.knowledge_snapshot_format import CATEGORIES, canonical_bytes, package_sha256, validate_package


def _digest(value):
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def _package(item, text):
    if type(item) is not dict or set(item) != {'selected', 'trust', 'record_type'}:
        raise ValueError('prepared item unavailable')
    selected = item['selected']
    if type(selected) is not dict or set(selected) != {'curated_id', 'reference'}:
        raise ValueError('prepared item unavailable')
    ref = selected['reference']
    if (type(ref) is not dict or set(ref) != {'version', 'provider', 'owner', 'workspace_id', 'project_id', 'block_id', 'revision', 'value_sha256'}
            or type(ref['version']) is not int or ref['version'] != 1 or ref['provider'] != 'fm-v2'
            or ref['project_id'] is not None or type(item['record_type']) is not str or item['record_type'] not in CATEGORIES):
        raise ValueError('prepared item unavailable')
    text_hash = hashlib.sha256(text.encode('utf-8', 'strict')).hexdigest()
    package = {'format': 'openclank.knowledge-snapshot.v1',
               'origin': {'item_ref': 'knowledge-' + _digest(ref), 'revision': ref['revision'],
                          'value_sha256': ref['value_sha256'], 'record_type': item['record_type'],
                          'declared_scope': {'kind': 'selected_item'}},
               'knowledge': {'text': text, 'category': CATEGORIES[item['record_type']], 'text_sha256': text_hash}}
    return validate_package(package)


async def _capture(*, provider, owner, account_id, workspace_id, policy_projection, current, curated_id=None, expected=None):
    if await current() is not True:
        raise ValueError('source authority changed')
    value = await provider._host_prepared_knowledge_read(owner=owner, workspace_id=workspace_id,
        account_id=account_id, policy_projection=policy_projection, limit=1, cursor=None,
        expected=expected, curated_id=curated_id, deadline=asyncio.get_running_loop().time()+1.5)
    if await current() is not True or type(value) is not dict or value.get('state') != 'ready':
        raise ValueError('source unavailable')
    return value


async def preview(*, provider, owner, account_id, workspace_id, policy_projection, current, curated_id):
    first = await _capture(provider=provider, owner=owner, account_id=account_id, workspace_id=workspace_id,
                           policy_projection=policy_projection, current=current, curated_id=curated_id)
    item = first['item']
    verified = await _capture(provider=provider, owner=owner, account_id=account_id, workspace_id=workspace_id,
                              policy_projection=policy_projection, current=current, expected=item)
    if verified['body'] != first['body']:
        raise ValueError('source changed')
    package = _package(item, verified['body'])
    review = {'version':'s10.knowledge-export-review.v1', 'owner':owner, 'account_id':account_id,
              'workspace_id':workspace_id, 'policy_projection':policy_projection, 'item':item,
              'package_sha256':package_sha256(package)}
    return {'package':package, 'package_sha256':review['package_sha256'], 'review_sha256':_digest(review), 'expected':item}


async def confirm(*, provider, owner, account_id, workspace_id, policy_projection, current, curated_id, expected, review_sha256):
    captured = await _capture(provider=provider, owner=owner, account_id=account_id, workspace_id=workspace_id,
                              policy_projection=policy_projection, current=current, expected=expected)
    if captured['item']['selected']['curated_id'] != curated_id:
        raise ValueError('source changed')
    package = _package(captured['item'], captured['body'])
    review = {'version':'s10.knowledge-export-review.v1', 'owner':owner, 'account_id':account_id,
              'workspace_id':workspace_id, 'policy_projection':policy_projection, 'item':captured['item'],
              'package_sha256':package_sha256(package)}
    if _digest(review) != review_sha256 or await current() is not True:
        raise ValueError('source review changed')
    return canonical_bytes(package)
