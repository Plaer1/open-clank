"""Closed, portable S10 knowledge snapshot and recipient review evidence."""
import hashlib
import json
import re
import uuid

FORMAT = 'openclank.knowledge-snapshot.v1'
PROFILE = 's10.curated-transfer.v1'
CATEGORIES = {'fact': 'fact', 'persona': 'identity', 'instruction': 'preference', 'episodic': 'task'}
_HEX = re.compile(r'^[0-9a-f]{64}$')


def _bytes(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False).encode('utf-8', 'strict')


def canonical_bytes(value):
    raw = _bytes(value)
    if len(raw) > 65536:
        raise ValueError('knowledge snapshot too large')
    return raw


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate key')
        result[key] = value
    return result


def _constant(_):
    raise ValueError('nonfinite number')


def validate_package(value):
    if type(value) is not dict or set(value) != {'format', 'origin', 'knowledge'} or value['format'] != FORMAT:
        raise ValueError('invalid knowledge snapshot')
    origin, knowledge = value['origin'], value['knowledge']
    if (type(origin) is not dict or set(origin) != {'item_ref', 'revision', 'value_sha256', 'record_type', 'declared_scope'}
            or type(origin['item_ref']) is not str or re.fullmatch(r'knowledge-[0-9a-f]{64}', origin['item_ref']) is None
            or type(origin['revision']) is not int or not 1 <= origin['revision'] <= 9007199254740991
            or type(origin['value_sha256']) is not str or _HEX.fullmatch(origin['value_sha256']) is None
            or type(origin['record_type']) is not str or origin['record_type'] not in CATEGORIES or origin['declared_scope'] != {'kind': 'selected_item'}
            or type(origin['declared_scope']) is not dict):
        raise ValueError('invalid knowledge origin')
    if (type(knowledge) is not dict or set(knowledge) != {'text', 'category', 'text_sha256'}
            or type(knowledge['text']) is not str or not knowledge['text'] or len(knowledge['text']) > 5000 or knowledge['text'] != knowledge['text'].strip()
            or '\x00' in knowledge['text'] or knowledge['category'] != CATEGORIES[origin['record_type']]):
        raise ValueError('invalid knowledge content')
    raw = knowledge['text'].encode('utf-8', 'strict')
    if len(raw) > 16384 or type(knowledge['text_sha256']) is not str or knowledge['text_sha256'] != _sha(raw):
        raise ValueError('invalid knowledge content')
    canonical_bytes(value)
    return value


def parse_package(raw):
    if type(raw) is not bytes or len(raw) > 65536:
        raise ValueError('invalid package bytes')
    try:
        value = json.loads(raw.decode('utf-8', 'strict'), object_pairs_hook=_pairs, parse_constant=_constant)
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise ValueError('invalid package bytes') from exc
    return validate_package(value)


def package_sha256(value):
    return _sha(canonical_bytes(validate_package(value)))


def recipient_descriptor(raw, *, account_id, workspace_id, action_id):
    package = parse_package(raw)
    for value in (account_id, workspace_id):
        if type(value) is not str or not value or value != value.strip() or len(value.encode('utf-8')) > 512 or any(ord(c) < 32 or ord(c) == 127 for c in value):
            raise ValueError('invalid recipient scope')
    if type(action_id) is not str or str(uuid.UUID(action_id)) != action_id:
        raise ValueError('invalid action id')
    knowledge = package['knowledge']
    review = {'version': 's10.curated-knowledge-review.v1', 'account_id': account_id, 'workspace_id': workspace_id,
              'action_id': action_id, 'mode': 'review_only', 'raw_sha256': _sha(raw),
              'canonical_sha256': package_sha256(package), 'text_sha256': knowledge['text_sha256'],
              'category': knowledge['category'], 'origin': package['origin']}
    descriptor = {'version': 1, 'raw_json': raw.decode('utf-8', 'strict'), **{k: v for k, v in review.items() if k != 'version'}, 'review_sha256': _sha(_bytes(review))}
    if len(_bytes(descriptor)) > 256 * 1024:
        raise ValueError('descriptor too large')
    return descriptor
