"""Bounded heuristic inspection of selected unsigned PC FastFiles."""
from __future__ import annotations

import asyncio
import hashlib
import re
import struct
import time
import zlib

PROFILE = 'fastfile-cod4-v5-1'
WAW_PROFILE = 'fastfile-waw-v387-1'
REGISTRY_PROFILE = 'fastfile-scan-2'
MAX_INPUT = 32 * 1024 * 1024
MAX_EXPANDED = 64 * 1024 * 1024
MAX_CANDIDATES = 2048
MAX_NAME = 256
CHUNK = 64 * 1024
MAX_SECONDS = 15
# The pattern can consume 12 framing bytes, 256 prefix bytes, a dot,
# 32 extension bytes and a NUL, even though overlong names are refused.
SCAN_LOOKAHEAD = 12 + 256 + 1 + 32 + 1
_PATTERN = re.compile(rb'(?=\xff{4}(.{4})\xff{4}([A-Za-z0-9_/\\.\-]{1,256}\.[A-Za-z0-9_]{1,32})\x00)', re.DOTALL)


class FastfileUnsupported(ValueError):
    pass


async def inspect(read_range, size: int) -> dict:
    if type(size) is not int or not 14 <= size <= MAX_INPUT:
        raise FastfileUnsupported('FastFile input size is unsupported')
    deadline = time.monotonic() + MAX_SECONDS
    def check():
        if time.monotonic() >= deadline:
            raise FastfileUnsupported('FastFile inspection deadline exceeded')
    prefix = await read_range(0, 12)
    version = struct.unpack('<I', prefix[8:])[0]
    if prefix[:8] != b'IWffu100' or version not in (5, 387):
        raise FastfileUnsupported('Only unsigned CoD4 v5 and WaW v387 FastFiles are supported')
    profile, variant = (PROFILE, 'unsigned-cod4-v5') if version == 5 else (WAW_PROFILE, 'unsigned-waw-v387')
    digest = hashlib.sha256(prefix)
    decoder = zlib.decompressobj()
    expanded = bytearray()
    offset = 12
    try:
        while offset < size:
            check()
            part = await read_range(offset, min(CHUNK, size - offset))
            digest.update(part)
            offset += len(part)
            while part:
                await asyncio.sleep(0)
                check()
                budget = MAX_EXPANDED - len(expanded)
                out = decoder.decompress(part, min(CHUNK, budget + 1))
                if len(out) > budget:
                    raise FastfileUnsupported('FastFile expanded size exceeds limit')
                expanded.extend(out)
                part = decoder.unconsumed_tail
                if decoder.eof:
                    if decoder.unused_data or part or offset != size:
                        raise FastfileUnsupported('FastFile has trailing data')
                    break
            if decoder.eof and offset != size:
                raise FastfileUnsupported('FastFile has trailing data')
        if not decoder.eof or decoder.unused_data:
            raise FastfileUnsupported('FastFile stream is truncated')
    except zlib.error as exc:
        raise FastfileUnsupported('FastFile zlib stream is invalid') from exc
    rows = []
    for base in range(0, len(expanded), CHUNK):
        await asyncio.sleep(0)
        check()
        end = min(base + CHUNK, len(expanded))
        window = expanded[base:min(end + SCAN_LOOKAHEAD, len(expanded))]
        for match in _PATTERN.finditer(window):
            check()
            # Every absolute start belongs to one non-overlapping core chunk.
            if base + match.start() >= end:
                break
            if len(match.group(2)) > MAX_NAME:
                raise FastfileUnsupported('FastFile candidate name exceeds limit')
            length = int.from_bytes(match.group(1), 'little')
            start = base + match.start() + 12 + len(match.group(2)) + 1
            if not 0 < length <= len(expanded) - start:
                continue
            if len(rows) >= MAX_CANDIDATES:
                raise FastfileUnsupported('FastFile candidate limit exceeded')
            rows.append({'name':match.group(2).decode('ascii'), 'declaredLength':length,
                         'decompressedOffset':start})
    return {'profile':profile, 'variant':variant, 'contentSHA256':digest.hexdigest(),
            'sourceRange':{'offset':0,'length':size}, 'expandedBytes':len(expanded),
            'candidateCount':len(rows), 'candidates':rows,
            'coverage':'heuristic_non_exhaustive', 'payloadIntegrity':'not_checked'}
