"""Closed request/response validation for the pre-extracted candidate stage and the update batch.

Shared by the HTTP routes and the provider wrappers so both refuse the same shapes. Nothing here
builds provenance: provenance is set by Native only, so any provenance-looking key is refused.
"""
import json
import re

KINDS = ('fact', 'contact', 'project', 'goal', 'identity', 'preference', 'task', 'unknown')
UPDATE_PATHS = ('/value/value', '/confidence', '/trust', '/tags')
_HEX64 = re.compile(r'[0-9a-f]{64}')
_KEY = re.compile(r'[A-Za-z0-9_-]{22,128}')


class WireError(ValueError):
    """A request that does not match the closed wire shape."""


def _text(value, limit, what):
    if type(value) is not str or not value.strip() or len(value) > limit:
        raise WireError(f'invalid {what}')
    return value


def _count(items, what):
    if type(items) is not list or not 1 <= len(items) <= 100:
        raise WireError(f'{what} must hold 1 to 100 items')


def request_key(value):
    if type(value) is not str or _KEY.fullmatch(value) is None:
        raise WireError('invalid request_key')
    return value


def stage_request_key(value):
    # Native: 1..512 chars, trimmed, no control chars.
    if (type(value) is not str or not 1 <= len(value) <= 512 or value != value.strip()
            or any(ord(c) < 32 or ord(c) == 127 for c in value)):
        raise WireError('invalid request_key')
    return value


CANDIDATE_ID = re.compile(r'preextracted_[0-9a-f]{64}')


def candidate_id(value):
    if type(value) is not str or CANDIDATE_ID.fullmatch(value) is None:
        raise WireError('invalid candidate_id')
    return value


def preview_sha(value):
    if type(value) is not str or _HEX64.fullmatch(value) is None:
        raise WireError('invalid preview_sha256')
    return value


def validate_preextracted_items(items):
    _count(items, 'items')
    for item in items:
        if type(item) is not dict or not {'kind', 'text', 'source'} <= set(item) or not set(item) <= {'kind', 'text', 'source', 'target'}:
            raise WireError('invalid item')  # any provenance/evidence key is an unknown key here
        if item['kind'] not in KINDS:
            raise WireError('invalid kind')
        _text(item['text'], 4000, 'text')
        if len(item['text'].encode('utf-8')) > 4000 or item['text'] != item['text'].strip():
            raise WireError('invalid text')
        source = item['source']
        if type(source) is not dict or set(source) != {'uri', 'revision', 'content_sha256'}:
            raise WireError('invalid source')
        _text(source['uri'], 512, 'source uri')
        if source['uri'] != source['uri'].strip() or source['uri'].startswith(('memory://', 'host-archive://')):
            raise WireError('invalid source uri')
        if type(source['revision']) is not int or source['revision'] < 1:
            raise WireError('invalid source revision')
        if type(source['content_sha256']) is not str or _HEX64.fullmatch(source['content_sha256']) is None:
            raise WireError('invalid source content_sha256')
        if 'target' in item:
            target = item['target']
            if type(target) is not dict or set(target) != {'predicate', 'claim_slot'}:
                raise WireError('invalid target')
            _text(target['predicate'], 256, 'target predicate')
            _text(target['claim_slot'], 512, 'target claim_slot')
    return items


def validate_update_items(items):
    _count(items, 'items')
    seen = set()
    keys = {'memory_id', 'candidate_id', 'origin_request_key', 'prior_receipt_id', 'expected_revision', 'selected_paths', 'payload'}
    for item in items:
        if type(item) is not dict or set(item) != keys:
            raise WireError('invalid item')
        for key in ('memory_id', 'candidate_id', 'origin_request_key', 'prior_receipt_id'):
            _text(item[key], 500, key)
        if item['memory_id'] in seen:
            raise WireError('duplicate memory_id')
        seen.add(item['memory_id'])
        if type(item['expected_revision']) is not int or item['expected_revision'] < 1:
            raise WireError('invalid expected_revision')
        paths = item['selected_paths']
        if (type(paths) is not list or not paths or len(set(paths)) != len(paths)
                or any(type(p) is not str or p not in UPDATE_PATHS for p in paths)):
            raise WireError('invalid selected_paths')
        if type(item['payload']) is not dict:
            raise WireError('invalid payload')
    if len(json.dumps(items, separators=(',', ':'), allow_nan=False).encode('utf-8')) > 64 * 1024:
        raise WireError('batch too large')
    return items
