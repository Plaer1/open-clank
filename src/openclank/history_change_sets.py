"""Closed, owner-authored local History membership embedded in Markdown."""
from __future__ import annotations

import base64
import json
import re

MAX_DOCUMENT = 256 * 1024
FENCE = '```open-clank.local-change-set/v1\n'
END = '\n```'


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate key')
        result[key] = value
    return result


def strict_json(data):
    return json.loads(data, object_pairs_hook=unique_object, parse_constant=lambda _: (_ for _ in ()).throw(ValueError('invalid constant')))


def text(value, maximum, *, required=False):
    if type(value) is not str or (required and not value) or len(value.encode('utf-8', 'strict')) > maximum:
        raise ValueError('invalid text')
    if any(0xD800 <= ord(c) <= 0xDFFF or (ord(c) < 32 and c not in '\n\t') for c in value):
        raise ValueError('invalid text')
    return value


def parse_locator(locator):
    if type(locator) is not str or len(locator) > 4096:
        raise ValueError('invalid locator')
    prefix = next((p for p in ('oc-history-action:v1:', 'oc-history-action:v2:') if locator.startswith(p)), None)
    if prefix is None: raise ValueError('invalid locator')
    encoded = locator[len(prefix):]
    if not re.fullmatch(r'[A-Za-z0-9_-]+', encoded):
        raise ValueError('invalid locator')
    raw = base64.urlsafe_b64decode(encoded + '=' * ((-len(encoded)) % 4))
    if len(raw) > 3000 or base64.urlsafe_b64encode(raw).decode().rstrip('=') != encoded:
        raise ValueError('invalid locator')
    value = strict_json(raw.decode('utf-8', 'strict'))
    version = 1 if prefix == 'oc-history-action:v1:' else 2
    keys = ({'version','actionID','resourceID','beforeVersionID','afterVersionID','origin'} if version == 1
            else {'version','actionID','requestDigest','completeDigest','origin'})
    origin_keys = {'version','host_chat_id','engine_session_id','root_operation_id','actor_id','execution_revision','inner_turn_id','tool_call_id','assistant_message_id'}
    if type(value) is not dict or set(value) != keys or type(value['version']) is not int or value['version'] != version or type(value['origin']) is not dict or set(value['origin']) != origin_keys:
        raise ValueError('invalid locator')
    if version == 2:
        # The native writer emits JSON.stringify in insertion order; require the
        # exact original compact bytes, not an alternate interpretation.
        if json.dumps(value, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode('utf-8') != raw:
            raise ValueError('invalid locator')
        if any(type(value[k]) is not str or not re.fullmatch(r'[0-9a-f]{64}', value[k]) for k in ('requestDigest','completeDigest')):
            raise ValueError('invalid locator')
    origin = value['origin']
    if type(origin['version']) is not int or origin['version'] != 1 or type(origin['execution_revision']) is not int or not 0 <= origin['execution_revision'] <= 9007199254740991:
        raise ValueError('invalid locator')
    fields = ('actionID','resourceID','beforeVersionID','afterVersionID') if version == 1 else ('actionID',)
    for item in [value[k] for k in fields] + [origin[k] for k in origin_keys - {'version','execution_revision'}]:
        if type(item) is not str or not item or len(item.encode('utf-8', 'strict')) > 256 or any(ord(c) < 32 or 127 <= ord(c) <= 159 or 0xD800 <= ord(c) <= 0xDFFF for c in item):
            raise ValueError('invalid locator')
    if version == 1 and not value['resourceID'].startswith('file:'):
        raise ValueError('invalid locator')
    return value


def validate(value):
    if type(value) is not dict or set(value) != {'version','title','description','members'} or type(value['version']) is not int or value['version'] != 1:
        raise ValueError('invalid set')
    text(value['title'], 200, required=True); text(value['description'], 16384)
    members = value['members']
    if type(members) is not list or len(members) > 100:
        raise ValueError('invalid members')
    seen = set()
    for member in members:
        if type(member) is not dict or set(member) != {'locator'}:
            raise ValueError('invalid member')
        parsed = parse_locator(member['locator'])
        identity = json.dumps(parsed, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)
        if identity in seen:
            raise ValueError('duplicate member')
        seen.add(identity)
    return value


def parse(document):
    text(document, MAX_DOCUMENT)
    openings = list(re.finditer(r'^```open-clank\.local-change-set/[^\n]*$', document, re.MULTILINE))
    if len(openings) != 1 or openings[0].group() != FENCE.rstrip('\n'):
        raise ValueError('set block unavailable')
    start = openings[0].start(); end = document.find(END, start + len(FENCE))
    if (start and document[start - 1] != '\n') or end < 0 or (end + len(END) < len(document) and document[end + len(END)] != '\n'):
        raise ValueError('set block unavailable')
    return validate(strict_json(document[start + len(FENCE):end]))


def patch(document, value, *, create=False):
    validate(value); text(document, MAX_DOCUMENT)
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)
    block = FENCE + encoded + END
    if create:
        if document:
            raise ValueError('new document must be empty')
        result = block + '\n'
    else:
        parse(document)
        start = re.search(r'^```open-clank\.local-change-set/v1$', document, re.MULTILINE).start()
        end = document.index(END, start + len(FENCE)) + len(END)
        result = document[:start] + block + document[end:]
    text(result, MAX_DOCUMENT)
    return result
