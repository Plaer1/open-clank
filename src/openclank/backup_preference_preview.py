"""Closed preview of one non-authorizing Open Clank backup preference."""
from __future__ import annotations

import json
import math

MAX_SOURCE_BYTES = 4 * 1024 * 1024
_MAX_DEPTH = 64
_MAX_NODES = 100_000
_FIELD = 'deliverable_reduced_motion'


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate field')
        result[key] = value
    return result


def _invalid_constant(_):
    raise ValueError('invalid JSON number')


def preview_backup(raw: bytes) -> dict:
    """Return only the recognized boolean and bounded unsupported counts."""
    if type(raw) is not bytes or not 1 <= len(raw) <= MAX_SOURCE_BYTES:
        raise ValueError('backup unavailable')
    try:
        document = json.loads(raw.decode('utf-8'), object_pairs_hook=_unique, parse_constant=_invalid_constant)
        stack = [(document, 0)]
        count = 0
        while stack:
            value, depth = stack.pop()
            count += 1
            if count > _MAX_NODES or depth > _MAX_DEPTH:
                raise ValueError('backup exceeds work limit')
            if type(value) is float and not math.isfinite(value):
                raise ValueError('invalid JSON number')
            if type(value) is dict:
                stack.extend((item, depth + 1) for item in value.values())
            elif type(value) is list:
                stack.extend((item, depth + 1) for item in value)
        if type(document) is not dict or type(document.get('version')) is not int or document['version'] != 2:
            raise ValueError('unsupported backup version')
        preferences = document.get('preferences')
        if type(preferences) is not dict or type(preferences.get(_FIELD)) is not bool:
            raise ValueError('preference unavailable')
        return {'sourceFormat':'openclank-backup', 'sourceVersion':2,
                'field':'preferences.' + _FIELD, 'value':preferences[_FIELD],
                'destination':'account.' + _FIELD,
                'unsupportedTopLevelCount':sum(key not in {'version','preferences'} for key in document),
                'unsupportedPreferenceCount':sum(key != _FIELD for key in preferences)}
    except (UnicodeError, ValueError, TypeError, RecursionError, OverflowError) as exc:
        raise ValueError('backup unavailable') from exc
