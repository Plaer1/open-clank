"""Finite safetensors declaration inspection; tensor data is never read."""
from __future__ import annotations

import hashlib
import json
import math
import struct

PROFILE = "safetensors-header-1"
MAX_HEADER = 2 * 1024 * 1024
MAX_ENTRIES = 4096
MAX_OUTPUT = 256
MAX_DIMS = 16
MAX_INT = (1 << 53) - 1  # Exact in the existing JSON/browser receipt.
_DTYPES = {"BOOL": 1, "U8": 1, "I8": 1, "I16": 2, "U16": 2, "F16": 2, "BF16": 2,
           "I32": 4, "U32": 4, "F32": 4, "I64": 8, "U64": 8, "F64": 8}


class Unsupported(ValueError):
    pass


def header_length(prefix: bytes, size: int) -> int:
    if len(prefix) != 8 or type(size) is not int or size < 10 or size > MAX_INT:
        raise Unsupported("Truncated safetensors length")
    length = struct.unpack("<Q", prefix)[0]
    if length < 2 or length > MAX_HEADER or length > size - 8:
        raise Unsupported("Safetensors header exceeds bounds or is truncated")
    return length


def inspect(prefix: bytes, header: bytes, size: int) -> dict:
    length = header_length(prefix, size)
    if len(header) != length or header[:1] != b"{":
        raise Unsupported("Safetensors header is truncated or malformed")
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise Unsupported("Duplicate safetensors key")
            result[key] = value
        return result
    def constant(_):
        raise Unsupported("Non-finite JSON value")
    try:
        doc = json.loads(header.decode("utf-8"), object_pairs_hook=pairs, parse_constant=constant)
    except (UnicodeError, ValueError, RecursionError) as exc:
        raise Unsupported("Invalid safetensors JSON header") from exc
    if not isinstance(doc, dict) or len(doc) > MAX_ENTRIES + 1:
        raise Unsupported("Safetensors entry limit exceeded")
    metadata = doc.pop("__metadata__", {})
    if not isinstance(metadata, dict) or any(not isinstance(k, str) or not isinstance(v, str) for k, v in metadata.items()):
        raise Unsupported("Unsupported safetensors metadata")
    declarations = []
    ranges = []
    data_size = size - 8 - length
    for name, tensor in doc.items():
        try:
            name_length = len(name.encode("utf-8")) if isinstance(name, str) else 0
        except UnicodeError as exc:
            raise Unsupported("Invalid tensor name") from exc
        if not name or not name_length or name_length > 1024 or not isinstance(tensor, dict) or set(tensor) != {"dtype", "shape", "data_offsets"}:
            raise Unsupported("Unsupported tensor declaration")
        dtype, shape, offsets = tensor["dtype"], tensor["shape"], tensor["data_offsets"]
        if not isinstance(dtype, str) or dtype not in _DTYPES or not isinstance(shape, list) or len(shape) > MAX_DIMS or not isinstance(offsets, list) or len(offsets) != 2:
            raise Unsupported("Unsupported tensor dtype, shape or offsets")
        if any(type(v) is not int or v < 0 or v > MAX_INT for v in shape + offsets):
            raise Unsupported("Invalid tensor dimension or offset")
        count = math.prod(shape)
        start, end = offsets
        if count > MAX_INT // _DTYPES[dtype] or end < start or end > data_size or end - start != count * _DTYPES[dtype]:
            raise Unsupported("Tensor declaration has inconsistent byte range")
        ranges.append((start, end))
        if len(declarations) < MAX_OUTPUT:
            declarations.append({"name": name, "dtype": dtype, "shape": shape, "dataOffsets": [start, end], "declaredBytes": end - start})
    position = 0
    for start, end in sorted(ranges):
        if start != position:
            raise Unsupported("Tensor ranges overlap or leave gaps")
        position = end
    if position != data_size:
        raise Unsupported("Tensor declarations do not cover the data region")
    return {"profile": PROFILE, "headerSHA256": hashlib.sha256(header).hexdigest(),
            "headerRange": {"offset": 8, "length": length}, "contentSHA256": None,
            "fileBytes": size, "dataBytes": data_size, "tensorCount": len(doc),
            "tensors": declarations, "omittedTensors": max(0, len(doc) - len(declarations)),
            "metadataEntries": len(metadata), "payloadIntegrity": "not_checked"}
