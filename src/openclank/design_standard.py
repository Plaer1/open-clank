"""Validate a portable Open Clank standard as inert, bounded data."""
import hashlib
import json
import re
from pathlib import PurePosixPath

_TOP = {'id','version','purpose','sourceRevision','sourceHashes','authority','baselinePalettes','paletteRoleSources','grammar','studies','assetSha256'}
_STUDY = {'id','family','name','meaning','intendedUse','avoid','roleBindings','source','thumbnails','provenance','revision'}
_ROLES = {'bg','fg','panel','border','accent','raised','well','outline','success','warning','error','sky'}
_SHA = re.compile(r'^[a-f0-9]{64}$')
_VERSION = re.compile(r'^1\.(?:0|[1-9][0-9]{0,5})\.(?:0|[1-9][0-9]{0,5})$')

def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result: raise ValueError('duplicate standard field')
        result[key] = value
    return result

def _text(value, limit=1024):
    if type(value) is not str or not value or len(value.encode('utf-8')) > limit or any(ord(c) < 32 or ord(c) == 127 for c in value): raise ValueError('invalid standard text')
    return value

def _path(value):
    _text(value, 512)
    path = PurePosixPath(value)
    if not path.parts or path.is_absolute() or '\\' in value or ':' in value or any(p in ('.','..') for p in path.parts) or str(path) != value: raise ValueError('invalid standard path')
    return value

def validate_design_standard(raw):
    if type(raw) is not bytes or not 0 < len(raw) <= 65536: raise ValueError('standard unavailable')
    try: value = json.loads(raw.decode('utf-8','strict'), object_pairs_hook=_object, parse_constant=lambda _: (_ for _ in ()).throw(ValueError('invalid number')))
    except (UnicodeError, ValueError, TypeError, RecursionError) as exc: raise ValueError('standard unavailable') from exc
    if type(value) is not dict or set(value) != _TOP or value['id'] != 'open-clank.deliverable-design' or type(value['version']) is not str or not _VERSION.fullmatch(value['version']): raise ValueError('unsupported standard')
    for key in ('purpose','sourceRevision','authority'): _text(value[key])
    for key in ('sourceHashes','assetSha256'):
        obj=value[key]
        if type(obj) is not dict or not 1 <= len(obj) <= (128 if key == 'assetSha256' else 32): raise ValueError('invalid standard manifest')
        for path,digest in obj.items():
            _path(path)
            if type(digest) is not str or not _SHA.fullmatch(digest): raise ValueError('invalid standard digest')
    palettes=value['baselinePalettes']
    if type(palettes) is not dict or set(palettes) != {'dark','light'}: raise ValueError('invalid standard palettes')
    for palette in palettes.values():
        if type(palette) is not dict or set(palette) != _ROLES or any(type(c) is not str or not re.fullmatch(r'#[0-9a-fA-F]{6}',c) for c in palette.values()): raise ValueError('invalid standard palette')
    roles=value['paletteRoleSources']; grammar=value['grammar']
    if type(roles) is not dict or set(roles) != _ROLES or type(grammar) is not dict or set(grammar) != {'composition','geometry','typography','motion','provenance'}: raise ValueError('invalid standard grammar')
    for item in (*roles.values(),*grammar.values()): _text(item)
    studies=value['studies']
    if type(studies) is not list or len(studies) != 26: raise ValueError('invalid standard studies')
    seen=set(); referenced=set()
    for study in studies:
        if type(study) is not dict or set(study) != _STUDY: raise ValueError('invalid study')
        ident=study['id']; family=study['family']
        if type(ident) is not str or not re.fullmatch(r'(?:GI|FT)(?:0[1-9]|1[0-3])',ident) or ident in seen or family != ('glyph-icon' if ident.startswith('GI') else 'form-shape-texture'): raise ValueError('invalid study identity')
        seen.add(ident)
        for key in ('name','meaning','intendedUse','avoid','provenance','revision'): _text(study[key])
        roles=study['roleBindings']
        if type(roles) is not list or not 1 <= len(roles) <= 12 or any(type(r) is not str or r not in _ROLES for r in roles) or len(set(roles)) != len(roles): raise ValueError('invalid study roles')
        thumbs=study['thumbnails']
        if type(thumbs) is not dict or set(thumbs) != {'light','dark'}: raise ValueError('invalid thumbnails')
        for path in (study['source'],thumbs['light'],thumbs['dark']):
            if _path(path) not in value['assetSha256'] or path in referenced: raise ValueError('missing or duplicate declared asset')
            referenced.add(path)
    if len(referenced)!=78 or set(value['assetSha256'])!=referenced: raise ValueError('invalid declared assets')
    return value, hashlib.sha256(raw).hexdigest()
