"""Content-free, non-authoritative descriptions of one permission request.

The action digest binds the complete displayed request details, not a grant or
an assertion that the operation executed. Unsupported input simply has no
projection; the existing permission flow remains authoritative.
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any

_MAX_SAFE_INTEGER = 9_007_199_254_740_991
_KEY = re.compile(r"^[\x20-\x7e]+$")


def explain_operation(*, request_id: str, session_id: str, turn_id: str,
                      revision: int, permission_type: str, detail: dict[str, Any]) -> dict[str, Any] | None:
    """Return a closed, bounded explanation for the exact C1 request, or None."""
    if (any(type(x) is not str for x in (request_id, session_id, turn_id, permission_type))
            or type(revision) is not int or not 0 <= revision <= _MAX_SAFE_INTEGER
            or type(detail) is not dict):
        return None
    entries = 0
    text_bytes = 0
    def check(value: Any, depth: int) -> bool:
        nonlocal entries, text_bytes
        entries += 1
        if entries > 256 or depth > 8:
            return False
        if value is None or type(value) is bool:
            return True
        if type(value) is int:
            return -_MAX_SAFE_INTEGER <= value <= _MAX_SAFE_INTEGER
        if type(value) is str:
            try:
                if len(value) > 65_536:
                    return False
                text_bytes += len(value.encode('utf-8'))
                return text_bytes <= 65_536
            except UnicodeEncodeError:
                return False
        if type(value) is list:
            return all(check(item, depth + 1) for item in value)
        if type(value) is dict:
            for key, item in value.items():
                if type(key) is not str or len(key) > 256 or _KEY.fullmatch(key) is None:
                    return False
                text_bytes += len(key)
                if text_bytes > 65_536 or not check(item, depth + 1):
                    return False
            return True
        return False
    material = [request_id, session_id, turn_id, revision, permission_type, detail]
    if not check(material, 0):
        return None
    try:
        encoded = json.dumps(material, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')
    except (TypeError, ValueError, UnicodeEncodeError):
        return None
    if len(encoded) > 65_536:
        return None
    intent = ('requested_file_edit' if permission_type == 'edit' and type(detail.get('filepath')) is str
              and detail['filepath'] else 'unknown')
    return {'version': 1, 'request_id': request_id, 'session_id': session_id, 'turn_id': turn_id,
            'revision': revision, 'action_sha256': hashlib.sha256(encoded).hexdigest(), 'intent': intent,
            'destructive': 'unknown', 'network': 'unknown', 'privilege': 'unknown',
            'recovery': 'unknown', 'grant': 'depends_on_choice'}
