"""Bounded exact-byte text edits for the existing collaborative Files owner.

This module neither reads a resource nor grants authority. Offsets are UTF-8
byte offsets into the exact supplied base; ambiguous transforms fail closed.
"""
from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping

MAX_TEXT_BYTES = 1024 * 1024
MAX_INSERT_BYTES = 256 * 1024
MAX_EDITS = 64


class TextEditError(ValueError):
    pass


class TextEditConflict(TextEditError):
    pass


def _text(value: object) -> bytes:
    if (not isinstance(value, bytes) or len(value) > MAX_TEXT_BYTES or b'\x00' in value
            or b'\r' in value or value.startswith(b'\xef\xbb\xbf')):
        raise TextEditError('unsupported text')
    try:
        value.decode('utf-8', 'strict')
    except UnicodeDecodeError as exc:
        raise TextEditError('unsupported text') from exc
    return value


def _boundary(value: bytes, offset: int) -> bool:
    return offset == 0 or offset == len(value) or (value[offset] & 0xc0) != 0x80


def rebase_text_edits(*, base: bytes, base_sha256: str, current: bytes, edits: object) -> dict:
    """Rebase nonoverlapping edits over at most one conservative remote span.

    Returns internal exact bytes and their hash; the caller must perform a
    fresh authorized current-revision CAS and retain the existing History
    receipt. No result here establishes mutation, ordering or authorization.
    """
    base = _text(base); current = _text(current)
    if (not isinstance(base_sha256, str) or not re.fullmatch(r'[0-9a-f]{64}', base_sha256)
            or hashlib.sha256(base).hexdigest() != base_sha256):
        raise TextEditError('base fingerprint mismatch')
    if not isinstance(edits, list) or not 1 <= len(edits) <= MAX_EDITS:
        raise TextEditError('invalid text edits')
    prepared = []; inserted = 0; previous_end = -1; previous_insertion = False
    for edit in edits:
        if not isinstance(edit, Mapping) or set(edit) != {'start', 'end', 'text'}:
            raise TextEditError('invalid text edit')
        start, end, text = edit['start'], edit['end'], edit['text']
        if (type(start) is not int or type(end) is not int or not 0 <= start <= end <= len(base)
                or start < previous_end or (start == previous_end and start == end and previous_insertion)
                or not _boundary(base, start) or not _boundary(base, end)
                or not isinstance(text, str)):
            raise TextEditError('invalid text edit')
        try:
            data = text.encode('utf-8', 'strict')
        except UnicodeEncodeError as exc:
            raise TextEditError('invalid text edit') from exc
        inserted += len(data)
        if inserted > MAX_INSERT_BYTES or b'\x00' in data or b'\r' in data or data.startswith(b'\xef\xbb\xbf'):
            raise TextEditError('text edits exceed bounds')
        prepared.append((start, end, data)); previous_end = end; previous_insertion = start == end

    # One changed span deliberately treats all intervening changes as unknown;
    # it never infers a multi-operation history from bytes.
    prefix = 0; common = min(len(base), len(current))
    while prefix < common and base[prefix] == current[prefix]: prefix += 1
    while prefix and (not _boundary(base, prefix) or not _boundary(current, prefix)): prefix -= 1
    suffix = 0
    while (suffix < len(base)-prefix and suffix < len(current)-prefix
           and base[len(base)-suffix-1] == current[len(current)-suffix-1]): suffix += 1
    while suffix and (not _boundary(base, len(base)-suffix) or not _boundary(current, len(current)-suffix)): suffix -= 1
    base_end, current_end = len(base)-suffix, len(current)-suffix
    changed = base != current
    shift = current_end-base_end
    mapped = []
    for start, end, data in prepared:
        if changed:
            # Nonempty half-open ranges may meet at a boundary. Concurrent
            # insertions exactly on a boundary have ambiguous ordering.
            insertion = start == end
            if end < prefix or (end == prefix and not insertion):
                pass
            elif start > base_end or (start == base_end and not insertion):
                start += shift; end += shift
            else:
                raise TextEditConflict('overlapping or ambiguous concurrent edit')
        mapped.append((start, end, data))
    result = current
    for start, end, data in reversed(mapped): result = result[:start] + data + result[end:]
    _text(result)
    return {'version':1, 'current_sha256':hashlib.sha256(current).hexdigest(),
            'result_sha256':hashlib.sha256(result).hexdigest(), 'result':result}
