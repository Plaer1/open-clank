"""Bounded Mach-O header and universal slice framing, not validation."""
from __future__ import annotations

import asyncio
import struct
import time

PROFILE = 'macho-framing-1'
MAX_FILE = 256 * 1024 * 1024
MAX_OBSERVED = 1024 * 1024
MAX_SLICES = 64
MAX_COMMANDS = 1024
MAX_DISPLAY = 256
MAX_CALLS = 2048
MAX_SECONDS = 14


class MachoUnsupported(ValueError):
    pass


async def inspect(read_range, size: int) -> dict:
    if type(size) is not int or not 8 <= size <= MAX_FILE:
        raise MachoUnsupported('Mach-O file size is unsupported')
    deadline = time.monotonic() + MAX_SECONDS
    calls = observed = 0

    async def read(offset: int, length: int) -> bytes:
        nonlocal calls, observed
        if offset < 0 or length < 0 or offset > size or length > size - offset:
            raise MachoUnsupported('Mach-O framing is truncated')
        if not length:
            return b''
        remaining = deadline - time.monotonic()
        if calls >= MAX_CALLS or length > MAX_OBSERVED - observed or remaining <= 0:
            raise MachoUnsupported('Mach-O inspection budget exceeded')
        try:
            async with asyncio.timeout(remaining):
                data = await read_range(offset, length)
        except TimeoutError as exc:
            raise MachoUnsupported('Mach-O inspection deadline exceeded') from exc
        if not isinstance(data, bytes) or len(data) != length:
            raise MachoUnsupported('Mach-O framing is truncated')
        calls += 1
        observed += length
        return data

    prefix = await read(0, 8)
    magic = prefix[:4]
    thin = {b'\xce\xfa\xed\xfe':(32,'<'), b'\xfe\xed\xfa\xce':(32,'>'),
            b'\xcf\xfa\xed\xfe':(64,'<'), b'\xfe\xed\xfa\xcf':(64,'>')}
    result = {'profile':PROFILE, 'fileBytes':size, 'contentSHA256':None,
              'payloadIntegrity':'not_checked'}
    if magic in thin:
        bits, order = thin[magic]
        header_size = 28 if bits == 32 else 32
        header = prefix + await read(8, header_size - 8)
        cpu, subtype, file_type, count, window, flags = struct.unpack_from(order + 'iiIIII', header, 4)
        if count > MAX_COMMANDS or window > size - header_size:
            raise MachoUnsupported('Mach-O command bounds are unsupported')
        end = header_size + window
        offset = header_size
        commands = []
        for index in range(count):
            await asyncio.sleep(0)
            if offset > end or end - offset < 8:
                raise MachoUnsupported('Mach-O command framing is truncated')
            cmd, command_size = struct.unpack(order + 'II', await read(offset, 8))
            if command_size < 8 or command_size % (4 if bits == 32 else 8) or command_size > end - offset:
                raise MachoUnsupported('Mach-O command size is invalid')
            if index < MAX_DISPLAY:
                commands.append({'command':cmd, 'offset':offset, 'size':command_size})
            offset += command_size
        if offset != end:
            raise MachoUnsupported('Mach-O command count and extent differ')
        result.update({'container':'thin', 'bits':bits, 'byteOrder':'little' if order == '<' else 'big',
                       'cpuType':cpu, 'cpuSubtype':subtype, 'fileType':file_type, 'flags':flags,
                       'commandCount':count, 'commandBytes':window, 'commands':commands,
                       'omittedCommands':count-len(commands)})
    elif magic in (b'\xca\xfe\xba\xbe', b'\xca\xfe\xba\xbf'):
        bits = 32 if magic[-1] == 0xbe else 64
        count = struct.unpack_from('>I', prefix, 4)[0]
        if count > MAX_SLICES:
            raise MachoUnsupported('Mach-O slice count exceeds limit')
        width = 20 if bits == 32 else 32
        table = await read(8, count * width)
        table_end = 8 + count * width
        slices = []
        ranges = []
        for index in range(count):
            if bits == 32:
                cpu, subtype, offset, length, align = struct.unpack_from('>iiIII', table, index * width)
                reserved = 0
            else:
                cpu, subtype, offset, length, align, reserved = struct.unpack_from('>iiQQII', table, index * width)
            if align > (31 if bits == 32 else 63) or reserved != 0:
                raise MachoUnsupported('Mach-O slice alignment or reserved field is unsupported')
            if not length or offset < table_end or offset > size or length > size - offset or offset % (1 << align):
                raise MachoUnsupported('Mach-O slice extent is invalid')
            end = offset + length
            if any(offset < prior_end and start < end for start, prior_end in ranges):
                raise MachoUnsupported('Mach-O slice extents overlap')
            ranges.append((offset, end))
            slices.append({'cpuType':cpu, 'cpuSubtype':subtype, 'fileOffset':str(offset),
                           'fileSize':str(length), 'alignmentExponent':align})
        result.update({'container':'fat', 'bits':bits, 'byteOrder':'big', 'sliceCount':count,
                       'slices':slices, 'sliceContents':'not_inspected'})
    else:
        raise MachoUnsupported('Unsupported Mach-O magic or universal byte order')
    result.update({'observedBytes':observed, 'rangeCalls':calls})
    return result
