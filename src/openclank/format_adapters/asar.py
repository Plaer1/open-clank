"""Bounded ASAR Pickle/index inspection; never reads archive payloads."""
from __future__ import annotations

import hashlib
import json
import re
import struct

PROFILE = 'asar-header-1'
MAX_HEADER = 2 * 1024 * 1024
MAX_ENTRIES = 4096
MAX_DEPTH = 32
MAX_DISPLAY = 256
MAX_SAFE = 2**53 - 1


class AsarUnsupported(ValueError):
    pass


def header_length(prefix: bytes, size: int) -> int:
    if len(prefix) != 8 or type(size) is not int or size < 16 or size > MAX_SAFE:
        raise AsarUnsupported('ASAR size header unavailable')
    payload, length = struct.unpack('<II', prefix)
    if payload != 4 or length < 8 or length > MAX_HEADER or length % 4 or length > size - 8:
        raise AsarUnsupported('ASAR header size is unsupported')
    return length


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result: raise AsarUnsupported('duplicate ASAR index key')
        result[key] = value
    return result


def _segment(value):
    try:
        return (isinstance(value, str) and value not in {'', '.', '..'} and len(value.encode('utf-8')) <= 255
                and not any(c in value for c in ('/', '\\', '\x00')) and not any(ord(c) < 32 for c in value))
    except UnicodeError:
        return False


def inspect(prefix: bytes, header: bytes, size: int) -> dict:
    length = header_length(prefix, size)
    if len(header) != length: raise AsarUnsupported('ASAR header is truncated')
    payload, text_length = struct.unpack('<Ii', header[:8])
    aligned = (text_length + 3) & ~3
    if text_length < 0 or payload != length - 4 or 4 + aligned != payload or 8 + text_length > length:
        raise AsarUnsupported('ASAR Pickle framing is unsupported')
    try:
        tree = json.loads(header[8:8+text_length].decode('utf-8'), object_pairs_hook=_unique,
            parse_constant=lambda _: (_ for _ in ()).throw(AsarUnsupported('invalid ASAR number')))
    except (UnicodeError, ValueError, RecursionError) as exc:
        raise AsarUnsupported('ASAR index is invalid') from exc
    if not isinstance(tree, dict) or set(tree) != {'files'} or not isinstance(tree['files'], dict):
        raise AsarUnsupported('ASAR root is unsupported')
    if len(tree['files']) > MAX_ENTRIES or any(not _segment(name) for name in tree['files']):
        raise AsarUnsupported('ASAR root entries are unsupported')
    stack = [(name, node, 1) for name, node in reversed(list(tree['files'].items()))]
    count = 0; rows = []; intervals = []; payload_start = 8 + length
    while stack:
        path, node, depth = stack.pop()
        if depth > MAX_DEPTH or not isinstance(node, dict): raise AsarUnsupported('ASAR entry is unsupported')
        parts = path.split('/')
        if any(not _segment(part) for part in parts): raise AsarUnsupported('unsafe ASAR entry name')
        count += 1
        if count > MAX_ENTRIES: raise AsarUnsupported('ASAR entry budget exceeded')
        unpacked = node.get('unpacked', False)
        if type(unpacked) is not bool: raise AsarUnsupported('invalid ASAR unpacked flag')
        if 'files' in node:
            if set(node) - {'files','unpacked'} or not isinstance(node['files'], dict): raise AsarUnsupported('invalid ASAR directory')
            if len(stack) + len(node['files']) + count > MAX_ENTRIES: raise AsarUnsupported('ASAR entry budget exceeded')
            kind = 'directory'
            for name, child in reversed(list(node['files'].items())):
                if not _segment(name): raise AsarUnsupported('unsafe ASAR entry name')
                stack.append((path + '/' + name, child, depth + 1))
            row = {'path':path,'kind':kind,'unpacked':unpacked}
        elif 'link' in node:
            if set(node) - {'link','unpacked'} or not isinstance(node['link'], str) or any(not _segment(p) for p in node['link'].split('/')):
                raise AsarUnsupported('invalid ASAR link')
            row = {'path':path,'kind':'link','unpacked':unpacked,'link':node['link']}
        else:
            if set(node) - {'size','offset','unpacked','executable','integrity'}: raise AsarUnsupported('unknown ASAR file field')
            amount = node.get('size')
            if type(amount) is not int or amount < 0 or amount > MAX_SAFE: raise AsarUnsupported('invalid ASAR file size')
            if 'executable' in node and type(node['executable']) is not bool: raise AsarUnsupported('invalid ASAR executable flag')
            if 'integrity' in node and not isinstance(node['integrity'], dict): raise AsarUnsupported('invalid ASAR integrity declaration')
            row = {'path':path,'kind':'file','unpacked':unpacked,'size':amount}
            if not unpacked:
                offset = node.get('offset')
                if not isinstance(offset, str) or not re.fullmatch(r'0|[1-9][0-9]{0,15}', offset): raise AsarUnsupported('invalid ASAR offset')
                offset = int(offset)
                if offset > MAX_SAFE or payload_start + offset + amount > size: raise AsarUnsupported('ASAR range exceeds archive')
                row['offset'] = payload_start + offset
                if amount: intervals.append((offset, offset + amount))
        if len(rows) < MAX_DISPLAY: rows.append(row)
    intervals.sort()
    for previous, current in zip(intervals, intervals[1:]):
        if current[0] < previous[1] and current != previous: raise AsarUnsupported('overlapping ASAR ranges are unsupported')
    return {'profile':PROFILE,'headerSHA256':hashlib.sha256(prefix+header).hexdigest(),
            'headerRange':{'offset':0,'length':8+length},'entryCount':count,'omittedEntries':count-len(rows),
            'entries':rows,'contentSHA256':None,'payloadIntegrity':'not_checked','coverage':'partial' if count>len(rows) else 'complete'}
