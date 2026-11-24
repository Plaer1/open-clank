"""Finite ELF v1 header/table declarations; not an ELF validator."""
from __future__ import annotations

import asyncio
import struct
import time

PROFILE = 'elf-v1-metadata-1'
MAX_FILE = 256 * 1024 * 1024
MAX_OBSERVED = 1024 * 1024
MAX_ENTRIES = 1024
MAX_DISPLAY = 256
MAX_NAMES = 256 * 1024
MAX_NAME = 1024
MAX_CALLS = 2048
MAX_SECONDS = 14


class ElfUnsupported(ValueError):
    pass


async def inspect(read_range, size: int) -> dict:
    if type(size) is not int or not 16 <= size <= MAX_FILE:
        raise ElfUnsupported('ELF file size is unsupported')
    observed = calls = 0
    deadline = time.monotonic() + MAX_SECONDS

    async def read(offset: int, length: int) -> bytes:
        nonlocal observed, calls
        if offset < 0 or length < 0 or offset > size or length > size - offset:
            raise ElfUnsupported('ELF metadata extent is truncated')
        if not length:
            return b''
        if calls >= MAX_CALLS or length > MAX_OBSERVED - observed:
            raise ElfUnsupported('ELF metadata budget exceeded')
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ElfUnsupported('ELF inspection deadline exceeded')
        try:
            async with asyncio.timeout(remaining):
                data = await read_range(offset, length)
        except TimeoutError as exc:
            raise ElfUnsupported('ELF inspection deadline exceeded') from exc
        if not isinstance(data, bytes) or len(data) != length:
            raise ElfUnsupported('ELF metadata range is truncated')
        observed += length
        calls += 1
        return data

    ident = await read(0, 16)
    if ident[:4] != b'\x7fELF' or ident[4] not in (1, 2) or ident[5] not in (1, 2) or ident[6] != 1:
        raise ElfUnsupported('Only ELF v1 32/64-bit declared byte orders are supported')
    bits = 32 if ident[4] == 1 else 64
    order = '<' if ident[5] == 1 else '>'
    header_size, ph_size, sh_size = (52, 32, 40) if bits == 32 else (64, 56, 64)
    header = ident + await read(16, header_size - 16)
    fmt = order + ('HHIIIIIHHHHHH' if bits == 32 else 'HHIQQQIHHHHHH')
    typ, machine, version, entry, phoff, shoff, flags, ehsize, phentsize, phnum, shentsize, shnum, shstrndx = struct.unpack_from(fmt, header, 16)
    if version != 1 or ehsize != header_size:
        raise ElfUnsupported('Unsupported ELF header layout')
    if phnum == 0xffff or shstrndx == 0xffff or (shnum == 0 and shoff != 0):
        raise ElfUnsupported('ELF extended numbering is unsupported')
    if phnum + shnum > MAX_ENTRIES:
        raise ElfUnsupported('ELF table entry limit exceeded')
    if (phnum and (phoff == 0 or phentsize != ph_size)) or (not phnum and phoff != 0):
        raise ElfUnsupported('Unsupported program table declaration')
    if (shnum and (shoff == 0 or shentsize != sh_size)) or (not shnum and (shoff != 0 or shstrndx != 0)):
        raise ElfUnsupported('Unsupported section table declaration')
    if shstrndx >= shnum and shstrndx != 0:
        raise ElfUnsupported('ELF name table index is invalid')
    ph = await read(phoff, phnum * ph_size)
    sh = await read(shoff, shnum * sh_size)
    sections = []
    for i in range(shnum):
        values = struct.unpack_from(order + ('IIIIIIIIII' if bits == 32 else 'IIQQQQIIQQ'), sh, i * sh_size)
        name, kind, sflags, address, offset, length, link, info, align, entsize = values
        if kind not in (0, 8) and (offset > size or length > size - offset):
            raise ElfUnsupported('ELF section extent is outside file')
        sections.append({'nameOffset':name, 'type':kind, 'flags':str(sflags), 'address':str(address), 'offset':str(offset), 'size':str(length), 'link':link, 'info':info, 'alignment':str(align), 'entrySize':str(entsize), 'name':None})
    names = b''
    if shstrndx:
        table = sections[shstrndx]
        if table['type'] != 3 or int(table['size']) > MAX_NAMES:
            raise ElfUnsupported('ELF section name table is unsupported')
        names = await read(int(table['offset']), int(table['size']))
        if not names or names[0] != 0 or names[-1] != 0:
            raise ElfUnsupported('ELF section name table is malformed')
        for section in sections:
            if section['type'] == 0:
                continue
            at = section['nameOffset']
            if at >= len(names):
                raise ElfUnsupported('ELF section name offset is invalid')
            end = names.find(b'\0', at, min(len(names), at + MAX_NAME + 1))
            if end < 0:
                raise ElfUnsupported('ELF section name is unterminated or too long')
            raw = names[at:end]
            try:
                section['name'] = raw.decode('utf-8', errors='strict')
            except UnicodeDecodeError:
                section['name'] = 'bytes:' + raw.hex()
    programs = []
    for i in range(phnum):
        v = struct.unpack_from(order + ('IIIIIIII' if bits == 32 else 'IIQQQQQQ'), ph, i * ph_size)
        if bits == 32:
            kind, offset, vaddr, paddr, filesz, memsz, pflags, align = v
        else:
            kind, pflags, offset, vaddr, paddr, filesz, memsz, align = v
        if kind != 0 and (offset > size or filesz > size - offset):
            raise ElfUnsupported('ELF segment extent is outside file')
        programs.append({'type':kind, 'flags':pflags, 'offset':str(offset), 'virtualAddress':str(vaddr), 'physicalAddress':str(paddr), 'fileSize':str(filesz), 'memorySize':str(memsz), 'alignment':str(align)})
    return {'profile':PROFILE, 'bits':bits, 'byteOrder':'little' if ident[5] == 1 else 'big', 'fileBytes':size,
            'objectType':typ, 'machine':machine, 'osAbi':ident[7], 'abiVersion':ident[8], 'entryAddress':str(entry), 'flags':flags,
            'programCount':phnum, 'sectionCount':shnum, 'sectionNames':'present' if shstrndx else 'absent',
            'programs':programs[:MAX_DISPLAY], 'sections':sections[:MAX_DISPLAY],
            'omittedPrograms':max(0, phnum-MAX_DISPLAY), 'omittedSections':max(0, shnum-MAX_DISPLAY),
            'observedBytes':observed, 'rangeCalls':calls, 'contentSHA256':None, 'payloadIntegrity':'not_checked'}
