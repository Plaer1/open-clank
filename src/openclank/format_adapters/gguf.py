"""Bounded GGUF v3 declaration reader; never reads tensor payload bytes."""
from __future__ import annotations
import hashlib
import math
import re
import struct

PROFILE = 'gguf-v3-header-1'
MAX_HEADER = 2 * 1024 * 1024
MAX_RECORDS = 512
MAX_ARRAY = 512
MAX_READS = 8192
MAX_INT = (1 << 53) - 1
_TYPES = {0:'F32',1:'F16',2:'Q4_0',3:'Q4_1',6:'Q5_0',7:'Q5_1',8:'Q8_0',9:'Q8_1',10:'Q2_K',11:'Q3_K',12:'Q4_K',13:'Q5_K',14:'Q6_K',15:'Q8_K',16:'IQ2_XXS',17:'IQ2_XS',18:'IQ3_XXS',19:'IQ1_S',20:'IQ4_NL',21:'IQ3_S',22:'IQ2_S',23:'IQ4_XS',24:'I8',25:'I16',26:'I32',27:'I64',28:'F64',29:'IQ1_M',30:'BF16',34:'TQ1_0',35:'TQ2_0',39:'MXFP4'}
_SIZES = {0:4,1:2,24:1,25:2,26:4,27:8,28:8,30:2}
_SCALARS = {0:'B',1:'b',2:'H',3:'h',4:'I',5:'i',6:'f',7:'B',10:'Q',11:'q',12:'d'}

class Unsupported(ValueError):
    pass

async def inspect(read_range, size: int) -> dict:
    if type(size) is not int or not 24 <= size <= MAX_INT:
        raise Unsupported('GGUF file size is unsupported')
    offset = 0; calls = 0; digest = hashlib.sha256()
    async def take(n):
        nonlocal offset, calls
        if type(n) is not int or n < 0 or n > MAX_HEADER - offset or n > size - offset or calls >= MAX_READS:
            raise Unsupported('GGUF header or work limit exceeded')
        raw = await read_range(offset, n) if n else b''
        if len(raw) != n: raise Unsupported('GGUF header is truncated')
        calls += 1; offset += n; digest.update(raw)
        return raw
    async def number(fmt):
        return struct.unpack('<'+fmt, await take(struct.calcsize('<'+fmt)))[0]
    async def string():
        n = await number('Q')
        if n > 32768: raise Unsupported('GGUF string limit exceeded')
        try: return (await take(n)).decode('utf-8')
        except UnicodeError as exc: raise Unsupported('GGUF string is not UTF-8') from exc
    async def value(kind, depth=0):
        if depth > 3: raise Unsupported('GGUF array depth exceeded')
        if kind in _SCALARS:
            val = await number(_SCALARS[kind])
            if kind == 7 and val not in (0,1): raise Unsupported('Invalid GGUF boolean')
            if isinstance(val,float) and not math.isfinite(val): raise Unsupported('Invalid GGUF float')
            return val
        if kind == 8: return await string()
        if kind == 9:
            elem = await number('I'); count = await number('Q')
            if elem not in _SCALARS and elem not in (8, 9): raise Unsupported('Unknown GGUF array element type')
            if count > MAX_ARRAY: raise Unsupported('GGUF array limit exceeded')
            for _ in range(count): await value(elem,depth+1)
            return None
        raise Unsupported('Unknown GGUF metadata type')
    if await take(4) != b'GGUF' or await number('I') != 3:
        raise Unsupported('Only little-endian GGUF v3 is supported')
    tensor_count = await number('Q'); metadata_count = await number('Q')
    if tensor_count > MAX_RECORDS or metadata_count > MAX_RECORDS:
        raise Unsupported('GGUF declaration count exceeds limit')
    alignment = 32; seen = set()
    for _ in range(metadata_count):
        key = await string()
        if not re.fullmatch(r'[a-z0-9_]+(?:\.[a-z0-9_]+)*',key) or key in seen:
            raise Unsupported('Invalid or duplicate GGUF metadata key')
        seen.add(key); kind = await number('I'); val = await value(kind)
        if key == 'general.alignment':
            if kind != 4 or type(val) is not int or val < 1 or val > 4096 or val & (val-1):
                raise Unsupported('Unsupported GGUF alignment')
            alignment = val
    tensors=[]; names=set()
    for _ in range(tensor_count):
        name=await string(); dims=await number('I')
        if not name or name in names or not 1 <= dims <= 4: raise Unsupported('Invalid GGUF tensor declaration')
        names.add(name); shape=[await number('Q') for _ in range(dims)]
        if any(not x or x > MAX_INT for x in shape): raise Unsupported('Invalid GGUF tensor dimensions')
        kind=await number('I'); relative=await number('Q')
        if relative > MAX_INT or relative % alignment: raise Unsupported('Invalid GGUF tensor offset')
        count=math.prod(shape); width=_SIZES.get(kind)
        if count > MAX_INT or (width is not None and count > MAX_INT // width): raise Unsupported('GGUF tensor size overflow')
        tensors.append({'name':name,'shape':shape,'typeCode':kind,'typeName':_TYPES.get(kind),'dataOffset':relative,'declaredBytes':count*width if width else None})
    data_start = (offset + alignment - 1) // alignment * alignment
    if data_start > size: raise Unsupported('GGUF data region is truncated')
    for tensor in tensors:
        start=data_start+tensor['dataOffset']; length=tensor['declaredBytes']
        if start > size or (length is not None and length > size-start): raise Unsupported('GGUF tensor range is truncated')
    known=sorted((t['dataOffset'],t['dataOffset']+t['declaredBytes']) for t in tensors if t['declaredBytes'] is not None)
    if any(a[1] > b[0] for a,b in zip(known,known[1:])): raise Unsupported('GGUF tensor ranges overlap')
    unknown=sum(t['declaredBytes'] is None for t in tensors)
    return {'profile':PROFILE,'headerSHA256':digest.hexdigest(),'headerRange':{'offset':0,'length':offset},
        'contentSHA256':None,'fileBytes':size,'tensorCount':tensor_count,'metadataEntries':metadata_count,
        'tensors':tensors[:256],'omittedTensors':max(0,tensor_count-256),'unknownTensorSizes':unknown,
        'dataStart':data_start,'alignment':alignment,'payloadIntegrity':'not_checked',
        'coverage':'partial_unknown_tensor_sizes' if unknown else 'declared_ranges_only'}
