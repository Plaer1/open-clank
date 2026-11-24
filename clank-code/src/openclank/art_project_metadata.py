"""Closed, descriptive ArtProject metadata validation; never an access grant."""
from __future__ import annotations

import json
import math
import re


class ArtProjectMetadataError(ValueError):
    pass


_ID = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$')
_HEX = re.compile(r'^[0-9a-f]{64}$')
_COLOR = re.compile(r'^#[0-9a-fA-F]{8}$')
_MISSING = object()
_SAFE = 9007199254740991


def _text(value, maximum, *, empty=False, style=False):
    if type(value) is not str:
        raise ArtProjectMetadataError('invalid metadata text')
    try: raw = value.encode('utf-8', 'strict')
    except UnicodeError as exc: raise ArtProjectMetadataError('invalid metadata text') from exc
    if len(raw) > maximum or (not empty and not raw): raise ArtProjectMetadataError('invalid metadata text')
    if any((ord(c) < 32 or 127 <= ord(c) <= 159) and not (style and c in '\n\t') for c in value):
        raise ArtProjectMetadataError('invalid metadata text')
    return value


def _integer(value, minimum, maximum):
    if type(value) not in (int, float) or (type(value) is float and (not math.isfinite(value) or not value.is_integer())) or not minimum <= value <= maximum:
        raise ArtProjectMetadataError('invalid metadata integer')
    return int(value)


def _object(value, required, optional=frozenset()):
    if type(value) is not dict or not required.issubset(value) or set(value)-required-optional:
        raise ArtProjectMetadataError('invalid metadata object')


def _id(value):
    if type(value) is not str or not _ID.fullmatch(value): raise ArtProjectMetadataError('invalid metadata ID')
    return value


def _json_value(value):
    stack=[(value,False)]; active=set()
    while stack:
        item,leaving=stack.pop()
        if leaving:
            active.remove(id(item)); continue
        if item is None or type(item) is bool: continue
        if type(item) is str:
            try: item.encode('utf-8','strict')
            except UnicodeError as exc: raise ArtProjectMetadataError('invalid metadata Unicode') from exc
            continue
        if type(item) in (int,float):
            try: finite=math.isfinite(float(item))
            except (OverflowError,ValueError) as exc: raise ArtProjectMetadataError('invalid metadata number') from exc
            if not finite: raise ArtProjectMetadataError('invalid metadata number')
            continue
        if type(item) not in (dict,list): raise ArtProjectMetadataError('invalid metadata value')
        if id(item) in active: raise ArtProjectMetadataError('cyclic metadata')
        active.add(id(item)); stack.append((item,True))
        if type(item) is list:
            stack.extend((child,False) for child in item)
        else:
            for key,child in item.items():
                if type(key) is not str: raise ArtProjectMetadataError('invalid metadata key')
                stack.append((key,False)); stack.append((child,False))


def _equal(a,b):
    stack=[(a,b)]; seen=set()
    while stack:
        left,right=stack.pop()
        if type(left) in (int,float) and type(right) in (int,float):
            if left != right: return False
            continue
        if type(left) is not type(right): return False
        if type(left) is dict:
            if left.keys() != right.keys(): return False
            pair=(id(left),id(right))
            if pair in seen: continue
            seen.add(pair); stack.extend((left[k],right[k]) for k in left)
        elif type(left) is list:
            if len(left)!=len(right): return False
            pair=(id(left),id(right))
            if pair in seen: continue
            seen.add(pair); stack.extend(zip(left,right))
        elif left != right: return False
    return True


def validate_art_project(value):
    """Validate known v1 exactly, preserving input values and array order."""
    _object(value, {'version','references','palette','style','clips'})
    if _integer(value['version'],1,_SAFE) != 1: raise ArtProjectMetadataError('unsupported metadata version')
    _text(value['style'],2048,empty=True,style=True)
    refs=value['references']; palette=value['palette']; clips=value['clips']
    if type(refs) is not list or len(refs)>64 or type(palette) is not list or len(palette)>64 or type(clips) is not list or len(clips)>32:
        raise ArtProjectMetadataError('metadata collection exceeds bounds')
    ref_ids=set()
    for item in refs:
        _object(item,{'id','label','resourceKey','revision'},{'sha256'})
        ident=_id(item['id']); _text(item['label'],128)
        if ident in ref_ids: raise ArtProjectMetadataError('duplicate reference')
        ref_ids.add(ident)
        key=item['resourceKey']; _object(key,{'provider','account_id','workspace_id','resource_id'})
        _text(key['provider'],128); _text(key['account_id'],256); _text(key['resource_id'],2048)
        if key['workspace_id'] is not None: _text(key['workspace_id'],256)
        revision=item['revision']; _object(revision,{'kind','value'}); _text(revision['kind'],128); _text(revision['value'],2048)
        if 'sha256' in item and (type(item['sha256']) is not str or not _HEX.fullmatch(item['sha256'])): raise ArtProjectMetadataError('invalid metadata digest')
    color_ids=set()
    for item in palette:
        _object(item,{'id','name','color'}); ident=_id(item['id']); _text(item['name'],128)
        if ident in color_ids or type(item['color']) is not str or not _COLOR.fullmatch(item['color']): raise ArtProjectMetadataError('invalid palette')
        color_ids.add(ident)
    clip_ids=set(); frame_ids=set(); total=0
    for clip in clips:
        _object(clip,{'id','name','facing','frames'}); ident=_id(clip['id']); _text(clip['name'],128); _text(clip['facing'],128,empty=True)
        if ident in clip_ids or type(clip['frames']) is not list or not clip['frames']: raise ArtProjectMetadataError('invalid clip')
        clip_ids.add(ident)
        for frame in clip['frames']:
            total+=1
            if total>256: raise ArtProjectMetadataError('too many frames')
            _object(frame,{'id','referenceId','width','height','pivot'}); ident=_id(frame['id'])
            if ident in frame_ids: raise ArtProjectMetadataError('duplicate frame')
            frame_ids.add(ident)
            if frame['referenceId'] is not None and (type(frame['referenceId']) is not str or frame['referenceId'] not in ref_ids): raise ArtProjectMetadataError('unknown reference')
            width=_integer(frame['width'],1,16384); height=_integer(frame['height'],1,16384)
            pivot=frame['pivot']
            if pivot is not None:
                _object(pivot,{'x','y'}); _integer(pivot['x'],0,width); _integer(pivot['y'],0,height)
    _json_value(value)
    def json_numbers(node):
        if type(node) is float: return int(node)
        if type(node) is list: return [json_numbers(item) for item in node]
        if type(node) is dict: return {key:json_numbers(item) for key,item in node.items()}
        return node
    try: size=len(json.dumps(json_numbers(value),ensure_ascii=False,separators=(',',':'),allow_nan=False).encode('utf-8','strict'))
    except (TypeError,ValueError,UnicodeError) as exc: raise ArtProjectMetadataError('invalid metadata') from exc
    if size>65536: raise ArtProjectMetadataError('metadata exceeds bounds')


def _version(value):
    if type(value) is not dict or 'version' not in value: raise ArtProjectMetadataError('invalid metadata version')
    return _integer(value['version'],1,_SAFE)


def validate_art_project_state(state, *, prior_state=None, allow_unchanged_legacy=False):
    """Validate new input against an actual stored prior, without normalization."""
    if type(state) is not dict: raise ArtProjectMetadataError('invalid project state')
    current=state.get('artProject',_MISSING)
    prior=prior_state.get('artProject',_MISSING) if type(prior_state) is dict else _MISSING
    if prior is not _MISSING:
        try:
            prior_version=_version(prior)
            if prior_version == 1: validate_art_project(prior)
        except ArtProjectMetadataError:
            if not (allow_unchanged_legacy and current is not _MISSING and _equal(current,prior)):
                raise ArtProjectMetadataError('invalid existing metadata is read-only')
            return
        if prior_version != 1:
            if current is _MISSING or not _equal(current,prior):
                raise ArtProjectMetadataError('unsupported metadata is read-only')
            return
    if current is _MISSING: return
    version=_version(current)
    if version == 1: validate_art_project(current)
    else: _json_value(current)
