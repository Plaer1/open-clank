"""Bounded WebAssembly core-binary framing inspection, never validation."""
from __future__ import annotations

import asyncio
import time

PROFILE = "wasm-core-v1-framing-1"
MAX_FILE = 256 * 1024 * 1024
MAX_OBSERVED = 1024 * 1024
MAX_SECTIONS = 1024
MAX_DISPLAY = 256
MAX_NAME = 4096
MAX_CALLS = 2048
MAX_SECONDS = 14


class WasmUnsupported(ValueError):
    pass


class _BudgetStop(Exception):
    pass


async def inspect(read_range, size: int) -> dict:
    if type(size) is not int or not 8 <= size <= MAX_FILE:
        raise WasmUnsupported("WebAssembly file size is unsupported")
    deadline = time.monotonic() + MAX_SECONDS
    calls = observed = 0

    async def take(offset: int, length: int) -> bytes:
        nonlocal calls, observed
        if offset < 0 or length < 0 or offset > size or length > size - offset:
            raise WasmUnsupported("WebAssembly framing is truncated")
        if length == 0:
            return b""
        if calls >= MAX_CALLS or length > MAX_OBSERVED - observed:
            raise _BudgetStop()
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise _BudgetStop()
        try:
            async with asyncio.timeout(remaining):
                data = await read_range(offset, length)
        except TimeoutError as exc:
            raise _BudgetStop() from exc
        if not isinstance(data, bytes) or len(data) != length:
            raise WasmUnsupported("WebAssembly range is truncated")
        calls += 1
        observed += length
        return data

    async def u32(offset: int, end: int) -> tuple[int, int]:
        value = 0
        for index in range(5):
            if offset >= end:
                raise WasmUnsupported("WebAssembly integer is truncated")
            byte = (await take(offset, 1))[0]
            offset += 1
            if index == 4 and (byte & 0xf0):
                raise WasmUnsupported("WebAssembly integer overflows u32")
            value |= (byte & 0x7f) << (index * 7)
            if not byte & 0x80:
                return value, offset
        raise WasmUnsupported("WebAssembly integer is too long")

    try:
        prefix = await take(0, 8)
    except _BudgetStop as exc:
        raise WasmUnsupported("WebAssembly inspection deadline exceeded") from exc
    if prefix != b"\x00asm\x01\x00\x00\x00":
        raise WasmUnsupported("Only WebAssembly core binary version 1 framing is supported")

    offset = 8
    scanned = 0
    rows = []
    partial = False
    while offset < size:
        await asyncio.sleep(0)
        if scanned >= MAX_SECTIONS:
            partial = True
            break
        try:
            section_offset = offset
            section_id = (await take(offset, 1))[0]
            length, payload_offset = await u32(offset + 1, size)
            if length > size - payload_offset:
                raise WasmUnsupported("WebAssembly section exceeds the file")
            end = payload_offset + length
            name = None
            if section_id == 0:
                name_length, name_offset = await u32(payload_offset, end)
                if name_length > MAX_NAME:
                    raise WasmUnsupported("WebAssembly custom section name exceeds limit")
                if name_length > end - name_offset:
                    raise WasmUnsupported("WebAssembly custom section name is truncated")
                try:
                    name = (await take(name_offset, name_length)).decode("utf-8", errors="strict")
                except UnicodeDecodeError as exc:
                    raise WasmUnsupported("WebAssembly custom section name is not UTF-8") from exc
            scanned += 1
            if len(rows) < MAX_DISPLAY:
                rows.append({"id":section_id, "sectionOffset":section_offset,
                             "payloadOffset":payload_offset, "payloadLength":length, "customName":name})
            offset = end
        except _BudgetStop:
            partial = True
            break
    return {"profile":PROFILE, "fileBytes":size, "scannedSections":scanned,
            "omittedScannedSections":scanned-len(rows), "sections":rows,
            "observedBytes":observed, "rangeCalls":calls,
            "coverage":"partial_budget" if partial else "framing_only",
            "contentSHA256":None, "payloadIntegrity":"not_checked"}
