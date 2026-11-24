"""Bounded first-party ELF declarations for an authorized analysis source.

This projection does not attest an executable, validate the whole binary, or
make an inspection receipt a saved analysis result.
"""
from __future__ import annotations

import hashlib
import json

from src.openclank.analysis_adapters.base import CapturedSource, MAX_SOURCE_BYTES
from src.openclank.format_adapters import elf


async def inspect_captured_elf(source: CapturedSource) -> bytes:
    """Inspect exactly the bytes captured by the existing Files source owner."""
    if (type(source) is not CapturedSource or type(source.data) is not bytes
        or not 0 < len(source.data) <= MAX_SOURCE_BYTES
        or hashlib.sha256(source.data).hexdigest() != source.sha256):
        raise ValueError('analysis source unavailable')

    async def read_range(offset: int, length: int) -> bytes:
        if type(offset) is not int or type(length) is not int or offset < 0 or length < 0 or offset + length > len(source.data):
            raise ValueError('analysis source range unavailable')
        return source.data[offset:offset + length]

    observed = await elf.inspect(read_range, len(source.data))
    if observed.get('profile') != elf.PROFILE:
        raise ValueError('analysis profile unavailable')
    # Header declarations only. Omitted table entries are not inferred facts.
    fields = ('bits', 'byteOrder', 'fileBytes', 'objectType', 'machine', 'osAbi',
              'abiVersion', 'entryAddress', 'flags', 'programCount', 'sectionCount',
              'sectionNames', 'omittedPrograms', 'omittedSections', 'observedBytes', 'rangeCalls')
    result = {'version': 1, 'operation': 'info', 'producer': 'openclank.elf-metadata',
              'producerVersion': elf.PROFILE, 'sourceSHA256': source.sha256,
              'sourceRevision': dict(source.revision), 'coverage': 'partial_declared_metadata',
              'payloadIntegrity': 'not_checked', 'info': {key: observed[key] for key in fields}}
    raw = json.dumps(result, ensure_ascii=True, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('ascii')
    if len(raw) > 50 * 1024:
        raise ValueError('analysis result unavailable')
    return raw
