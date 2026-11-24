"""Bounded PE image header/section declarations, not executable validation."""
from __future__ import annotations

import asyncio
import struct
import time

PROFILE = 'pe-image-metadata-1'
MAX_FILE = 256 * 1024 * 1024
MAX_OBSERVED = 1024 * 1024
MAX_SECTIONS = 96
MAX_OPTIONAL = 4096
MAX_CALLS = 64
MAX_SECONDS = 14


class PeUnsupported(ValueError):
    pass


async def inspect(read_range, size: int) -> dict:
    if type(size) is not int or not 64 <= size <= MAX_FILE:
        raise PeUnsupported('PE image size is unsupported')
    deadline = time.monotonic() + MAX_SECONDS
    observed = calls = 0

    async def read(offset: int, length: int) -> bytes:
        nonlocal observed, calls
        if offset < 0 or length < 0 or offset > size or length > size - offset:
            raise PeUnsupported('PE metadata extent is truncated')
        if not length:
            return b''
        if calls >= MAX_CALLS or length > MAX_OBSERVED - observed:
            raise PeUnsupported('PE metadata budget exceeded')
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise PeUnsupported('PE inspection deadline exceeded')
        try:
            async with asyncio.timeout(remaining):
                data = await read_range(offset, length)
        except TimeoutError as exc:
            raise PeUnsupported('PE inspection deadline exceeded') from exc
        if not isinstance(data, bytes) or len(data) != length:
            raise PeUnsupported('PE metadata range is truncated')
        calls += 1
        observed += length
        return data

    dos = await read(0, 64)
    if dos[:2] != b'MZ':
        raise PeUnsupported('Only PE images with a DOS header are supported')
    pe_offset = struct.unpack_from('<I', dos, 60)[0]
    if pe_offset < 64:
        raise PeUnsupported('PE header overlaps the DOS header')
    coff = await read(pe_offset, 24)
    if coff[:4] != b'PE\0\0':
        raise PeUnsupported('PE signature is unavailable')
    machine, count, timestamp, _symbols, _symbol_count, optional_size, characteristics = struct.unpack_from('<HHIIIHH', coff, 4)
    if count > MAX_SECTIONS or optional_size > MAX_OPTIONAL:
        raise PeUnsupported('PE header limit exceeded')
    optional_offset = pe_offset + 24
    optional = await read(optional_offset, optional_size)
    if optional_size < 2:
        raise PeUnsupported('PE optional header is truncated')
    magic = struct.unpack_from('<H', optional)[0]
    if magic == 0x10b:
        minimum, image_base = 96, lambda: struct.unpack_from('<I', optional, 28)[0]
        bits = 32
    elif magic == 0x20b:
        minimum, image_base = 112, lambda: struct.unpack_from('<Q', optional, 24)[0]
        bits = 64
    else:
        raise PeUnsupported('Only PE32 and PE32+ images are supported')
    if optional_size < minimum:
        raise PeUnsupported('PE optional header is truncated')
    directory_count = struct.unpack_from('<I', optional, minimum - 4)[0]
    if directory_count > (optional_size - minimum) // 8:
        raise PeUnsupported('PE data directories exceed the optional header')
    table_offset = optional_offset + optional_size
    table_size = count * 40
    table = await read(table_offset, table_size)
    table_end = table_offset + table_size
    headers_size = struct.unpack_from('<I', optional, 60)[0]
    if headers_size < table_end or headers_size > size:
        raise PeUnsupported('PE declared headers overlap or exceed the file')
    sections = []
    for index in range(count):
        offset = index * 40
        name_field = table[offset:offset+8]
        raw_name = name_field.split(b'\0', 1)[0]
        try:
            if any(name_field[len(raw_name):]):
                raise UnicodeDecodeError('utf-8', name_field, len(raw_name), len(name_field), 'nonzero name padding')
            name = raw_name.decode('utf-8', errors='strict')
        except UnicodeDecodeError:
            name = 'bytes:' + name_field.hex()
        virtual_size, rva, raw_size, raw_offset = struct.unpack_from('<IIII', table, offset + 8)
        section_flags = struct.unpack_from('<I', table, offset + 36)[0]
        if raw_size and (raw_offset < headers_size or raw_offset > size or raw_size > size - raw_offset):
            raise PeUnsupported('PE section raw extent overlaps headers or exceeds the file')
        sections.append({'name':name, 'virtualSize':virtual_size, 'virtualAddressRVA':rva,
                         'rawSize':raw_size, 'rawOffset':raw_offset if raw_size else None,
                         'characteristics':section_flags})
    return {'profile':PROFILE, 'bits':bits, 'fileBytes':size, 'machine':machine,
            'coffCharacteristics':characteristics, 'timestamp':timestamp,
            'entryPointRVA':struct.unpack_from('<I', optional, 16)[0], 'imageBase':str(image_base()),
            'subsystem':struct.unpack_from('<H', optional, 68)[0], 'sizeOfImage':struct.unpack_from('<I', optional, 56)[0],
            'sizeOfHeaders':headers_size, 'peHeaderOffset':pe_offset, 'sectionTableOffset':table_offset,
            'sectionCount':count, 'sections':sections, 'dataDirectories':'not_inspected',
            'observedBytes':observed, 'rangeCalls':calls, 'contentSHA256':None, 'payloadIntegrity':'not_checked'}
