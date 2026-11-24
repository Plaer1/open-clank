"""Closed deterministic bundle of supplied PNG material maps; no source authority."""
from __future__ import annotations

import hashlib
import io
import json
import re
import warnings
import zipfile

from PIL import Image

MAX_MAP = 4 * 1024 * 1024
MAX_BUNDLE = 24 * 1024 * 1024
MAX_MANIFEST = 16 * 1024
_ROLES = ('baseColor', 'roughness', 'metallic', 'normal')
_PATHS = ('textures/base-color.png', 'textures/roughness.png', 'textures/metallic.png', 'textures/normal.png')


def _canonical(manifest):
    try:
        data = json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('utf-8', 'strict')
    except (TypeError, ValueError, UnicodeError, RecursionError) as exc:
        raise ValueError('material manifest unavailable') from exc
    if len(data) > MAX_MANIFEST:
        raise ValueError('material manifest unavailable')
    return data


def manifest_sha256(manifest):
    return hashlib.sha256(_canonical(manifest)).hexdigest()


def inspect_maps(maps):
    """Validate exact ordered role/bytes pairs and derive portable facts."""
    if type(maps) is not list or len(maps) not in (3, 4):
        raise ValueError('material maps unavailable')
    facts = []
    dimensions = None
    for index, item in enumerate(maps):
        if type(item) is not tuple or len(item) != 2 or item[0] != _ROLES[index]:
            raise ValueError('material roles unavailable')
        role, data = item
        if (type(data) is not bytes or not 33 <= len(data) <= MAX_MAP or data[:8] != b'\x89PNG\r\n\x1a\n'
                or data[12:16] != b'IHDR' or data[24] != 8):
            raise ValueError('material PNG unavailable')
        try:
            with warnings.catch_warnings():
                warnings.simplefilter('error', Image.DecompressionBombWarning)
                with Image.open(io.BytesIO(data)) as image:
                    width, height = image.size
                    allowed = ('RGB', 'RGBA') if role == 'baseColor' else ('RGB',) if role == 'normal' else ('L',)
                    if (image.format != 'PNG' or image.mode not in allowed
                            or getattr(image, 'n_frames', 1) != 1 or getattr(image, 'is_animated', False)
                            or not 1 <= width <= 2048 or not 1 <= height <= 2048):
                        raise ValueError('material PNG unavailable')
                    image.verify()
                with Image.open(io.BytesIO(data)) as image:
                    image.load()
        except Exception as exc:
            raise ValueError('material PNG unavailable') from exc
        if dimensions is None:
            dimensions = (width, height)
        elif dimensions != (width, height):
            raise ValueError('material dimensions differ')
        facts.append({'role':role, 'path':_PATHS[index], 'mime_type':'image/png',
            'sha256':hashlib.sha256(data).hexdigest(), 'size':len(data), 'width':width, 'height':height,
            'color_space':'srgb' if role == 'baseColor' else 'linear',
            'channel':'rgb' if role in ('baseColor', 'normal') else 'l',
            'normal_convention':'tangent-opengl-y+' if role == 'normal' else None,
            'provenance':'provided'})
    manifest = {'schema':'open-clank.material-bundle/v1', 'workflow':'metallic-roughness',
                'uv_set':0, 'alpha_mode':'opaque', 'maps':facts}
    _canonical(manifest)
    return manifest


def _archive(manifest, maps, originals=()):
    output = io.BytesIO()
    with zipfile.ZipFile(output, 'w', compression=zipfile.ZIP_STORED, allowZip64=False) as archive:
        for name, data in [('material.json', _canonical(manifest)), *[(_PATHS[i], item[1]) for i, item in enumerate(maps)], *originals]:
            info = zipfile.ZipInfo(name, (1980, 1, 1, 0, 0, 0))
            info.create_system = 3
            info.external_attr = 0o100644 << 16
            info.compress_type = zipfile.ZIP_STORED
            archive.writestr(info, data)
    result = output.getvalue()
    if len(result) > MAX_BUNDLE:
        raise ValueError('material bundle too large')
    return result


def pack(maps):
    manifest = inspect_maps(maps)
    data = _archive(manifest, maps)
    digest = manifest_sha256(manifest)
    validate_bundle(data, expected_manifest_sha256=digest,
                    expected_input_hashes=[entry['sha256'] for entry in manifest['maps']])
    return data, digest


def _bake_derivation(source_sha256, resolution):
    if (type(source_sha256) is not str or re.fullmatch(r'[0-9a-f]{64}', source_sha256) is None
            or type(resolution) is not int or resolution not in (256, 512, 1024, 2048)):
        raise ValueError('material bake provenance unavailable')
    return {'recipe':'blender-material-channels-v1', 'blender_api':'4.5', 'source_sha256':source_sha256,
            'resolution':resolution, 'quantization_bits':8, 'margin':0}


def _baked_manifest(maps, source_sha256, resolution):
    derivation = _bake_derivation(source_sha256, resolution)
    manifest = inspect_maps(maps)
    if len(maps) == 3 and any(_baked_scalar(item[1], resolution) != item[1] for item in maps[1:]):
        raise ValueError('material scalar maps unavailable')
    if len(maps) != 3 or any(item['width'] != resolution or item['height'] != resolution for item in manifest['maps']):
        raise ValueError('material bake dimensions unavailable')
    for item in manifest['maps']:
        item['provenance'] = 'baked'
    manifest['schema'] = 'open-clank.material-bundle/v2'
    manifest['derivation'] = derivation
    return manifest


def _baked_scalar(data, resolution):
    if (type(data) is not bytes or not 33 <= len(data) <= MAX_MAP or data[:8] != b'\x89PNG\r\n\x1a\n'
            or data[12:16] != b'IHDR' or data[24] != 8):
        raise ValueError('material PNG unavailable')
    try:
        with warnings.catch_warnings():
            warnings.simplefilter('error', Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as image:
                if (image.format != 'PNG' or image.mode not in ('L', 'RGB', 'RGBA')
                        or image.size != (resolution, resolution) or 'transparency' in image.info
                        or getattr(image, 'n_frames', 1) != 1
                        or getattr(image, 'is_animated', False)):
                    raise ValueError('material PNG unavailable')
                image.verify()
            with Image.open(io.BytesIO(data)) as image:
                image.load()
                if image.mode == 'L':
                    return data
                red, green, blue = (image.getchannel(channel) for channel in ('R', 'G', 'B'))
                if red.tobytes() != green.tobytes() or red.tobytes() != blue.tobytes():
                    raise ValueError('material scalar channels differ')
                if image.mode == 'RGBA' and image.getchannel('A').getextrema() != (255, 255):
                    raise ValueError('material scalar alpha unavailable')
                output = io.BytesIO()
                red.save(output, format='PNG')
                result = output.getvalue()
                if len(result) > MAX_MAP:
                    raise ValueError('material PNG unavailable')
                return result
    except Exception as exc:
        raise ValueError('material PNG unavailable') from exc


def pack_baked(maps, source_sha256, resolution):
    """Pack fixed-recipe baked maps; source digest is provenance, not authority."""
    _bake_derivation(source_sha256, resolution)
    if type(maps) is not list or len(maps) != 3 or any(type(item) is not tuple or len(item) != 2 or item[0] != _ROLES[i] for i, item in enumerate(maps)):
        raise ValueError('material roles unavailable')
    normalized = [maps[0], (maps[1][0], _baked_scalar(maps[1][1], resolution)),
                  (maps[2][0], _baked_scalar(maps[2][1], resolution))]
    manifest = _baked_manifest(normalized, source_sha256, resolution)
    data = _archive(manifest, normalized)
    digest = manifest_sha256(manifest)
    validate_bundle(data, expected_manifest_sha256=digest, expected_source_sha256=source_sha256,
                    expected_resolution=resolution)
    return data, digest


_GENERATED_PATHS = ('source/roughness.png', 'source/metallic.png')

def _generated_derivation(value):
    if type(value) is not dict or set(value) != {'job_id','provider_job_id','provider_id','connection_id','connection_revision','workflow','resolution','outputs'}:
        raise ValueError('generated material provenance unavailable')
    import uuid
    try:
        if type(value['job_id']) is not str or re.fullmatch(r'[0-9a-f]{32}',value['job_id']) is None:
            raise ValueError('invalid job')
        job = value['provider_job_id']
        if type(job) is not str or str(uuid.UUID(job)) != job: raise ValueError('invalid provider job')
    except (ValueError, AttributeError) as exc: raise ValueError('generated material provenance unavailable') from exc
    connection=value['connection_id']
    if (type(connection) is not str or not connection or len(connection.encode('utf-8','strict'))>256 or any(ord(c)<32 for c in connection)
            or value['provider_id'] != 'comfyui' or type(value['connection_revision']) is not int or not 0 <= value['connection_revision'] <= 9007199254740991
            or type(value['resolution']) is not int or value['resolution'] not in (256,512,1024,2048)):
        raise ValueError('generated material provenance unavailable')
    workflow=value['workflow']
    if (type(workflow) is not dict or set(workflow) != {'resource_id','revision','sha256'}
            or type(workflow['resource_id']) is not str or re.fullmatch(r'resource-[0-9a-f]{64}',workflow['resource_id']) is None
            or type(workflow['sha256']) is not str or re.fullmatch(r'[0-9a-f]{64}',workflow['sha256']) is None):
        raise ValueError('generated material provenance unavailable')
    rev=workflow['revision']
    if type(rev) is not dict or set(rev) != {'kind','value'}: raise ValueError('generated material provenance unavailable')
    for item in (rev['kind'],rev['value']):
        if type(item) is not str or not item or len(item.encode('utf-8','strict'))>512 or any(ord(c)<32 for c in item): raise ValueError('generated material provenance unavailable')
    outputs=value['outputs']
    if type(outputs) is not dict or set(outputs) != set(_ROLES[:3]): raise ValueError('generated material provenance unavailable')
    seen=set()
    for role in _ROLES[:3]:
        item=outputs[role]
        if (type(item) is not dict or set(item) != {'node_id','index'} or type(item['node_id']) is not str
                or not item['node_id'] or len(item['node_id'].encode('utf-8','strict'))>128
                or any(ord(c)<32 for c in item['node_id']) or type(item['index']) is not int or not 0<=item['index']<=63
                or (item['node_id'],item['index']) in seen): raise ValueError('generated material provenance unavailable')
        seen.add((item['node_id'],item['index']))
    _canonical(value)
    return value

def _generated_manifest(maps, originals, derivation):
    _generated_derivation(derivation)
    if type(originals) is not list or len(originals)!=2: raise ValueError('material originals unavailable')
    resolution=derivation['resolution']
    if any(_baked_scalar(originals[i],resolution)!=maps[i+1][1] for i in range(2)):
        raise ValueError('material source changed')
    manifest=inspect_maps(maps)
    if len(maps)!=3 or any(item['width']!=resolution or item['height']!=resolution for item in manifest['maps']):
        raise ValueError('material dimensions unavailable')
    for i,item in enumerate(manifest['maps']):
        original = maps[0][1] if i==0 else originals[i-1]
        item['provenance']='generated'
        item['original_sha256']=hashlib.sha256(original).hexdigest()
        item['original_size']=len(original)
        item['original_path']=_PATHS[0] if i==0 else _GENERATED_PATHS[i-1]
        item['transformation']='identity' if original==maps[i][1] else 'equal-channel-to-l'
    manifest['schema']='open-clank.material-bundle/v3'
    manifest['derivation']=derivation
    return manifest

def pack_generated(maps, derivation):
    """Pack the three original outputs of one declared workflow, with exact provenance."""
    _generated_derivation(derivation)
    if type(maps) is not list or len(maps)!=3 or any(type(item) is not tuple or len(item)!=2 or item[0]!=_ROLES[i] for i,item in enumerate(maps)):
        raise ValueError('material roles unavailable')
    originals=[maps[1][1],maps[2][1]]
    normalized=[maps[0],(maps[1][0],_baked_scalar(originals[0],derivation['resolution'])),
                (maps[2][0],_baked_scalar(originals[1],derivation['resolution']))]
    manifest=_generated_manifest(normalized,originals,derivation)
    data=_archive(manifest,normalized,list(zip(_GENERATED_PATHS,originals)))
    digest=manifest_sha256(manifest)
    validate_bundle(data,expected_manifest_sha256=digest,expected_generated=derivation)
    return data,digest


def validate_bundle(data, *, expected_manifest_sha256=None, expected_input_hashes=None,
                    expected_source_sha256=None, expected_resolution=None, expected_generated=None):
    """Validate exact canonical ZIP and map bytes without extracting files."""
    if type(data) is not bytes or not 0 < len(data) <= MAX_BUNDLE:
        raise ValueError('material bundle unavailable')
    try:
        with zipfile.ZipFile(io.BytesIO(data), 'r') as archive:
            entries = archive.infolist()
            names = [entry.filename for entry in entries]
            if names not in ([ 'material.json', *_PATHS[:3] ], [ 'material.json', *_PATHS ], ['material.json', *_PATHS[:3], *_GENERATED_PATHS]):
                raise ValueError('material entries unavailable')
            if archive.comment:
                raise ValueError('material ZIP unavailable')
            for entry in entries:
                limit = MAX_MANIFEST if entry.filename == 'material.json' else MAX_MAP
                if (entry.compress_type != zipfile.ZIP_STORED or entry.flag_bits != 0 or entry.extra or entry.comment
                        or entry.date_time != (1980, 1, 1, 0, 0, 0) or entry.create_system != 3
                        or entry.external_attr != 0o100644 << 16 or not 0 < entry.file_size <= limit
                        or entry.compress_size != entry.file_size):
                    raise ValueError('material ZIP unavailable')
            raw_manifest = archive.read(entries[0])
            generated_entries = len(entries)==6 and names[-2:]==list(_GENERATED_PATHS)
            map_entries = entries[1:4] if generated_entries else entries[1:]
            maps = [(_ROLES[i], archive.read(entry)) for i, entry in enumerate(map_entries)]
            originals = [archive.read(entry) for entry in entries[4:]] if generated_entries else []
        parsed = json.loads(raw_manifest.decode('utf-8', 'strict'))
        if type(parsed) is not dict:
            raise ValueError('material manifest unavailable')
        if parsed.get('schema') == 'open-clank.material-bundle/v3':
            if not generated_entries or expected_source_sha256 is not None or expected_resolution is not None or expected_input_hashes is not None: raise ValueError('material source changed')
            derivation=parsed.get('derivation')
            manifest=_generated_manifest(maps, originals, derivation)
            if expected_generated is not None and derivation != _generated_derivation(expected_generated): raise ValueError('material source changed')
        elif parsed.get('schema') == 'open-clank.material-bundle/v2':
            if generated_entries or expected_generated is not None: raise ValueError('material source changed')
            derivation = parsed.get('derivation')
            if type(derivation) is not dict:
                raise ValueError('material bake provenance unavailable')
            source_sha256, resolution = derivation.get('source_sha256'), derivation.get('resolution')
            manifest = _baked_manifest(maps, source_sha256, resolution)
            if ((expected_source_sha256 is not None and source_sha256 != expected_source_sha256)
                    or (expected_resolution is not None and resolution != expected_resolution)):
                raise ValueError('material source changed')
        else:
            if generated_entries or expected_generated is not None or expected_source_sha256 is not None or expected_resolution is not None:
                raise ValueError('material source changed')
            manifest = inspect_maps(maps)
        if raw_manifest != _canonical(manifest) or _archive(manifest, maps, list(zip(_GENERATED_PATHS,originals)) if generated_entries else ()) != data:
            raise ValueError('material bundle changed')
        if expected_manifest_sha256 is not None and manifest_sha256(manifest) != expected_manifest_sha256:
            raise ValueError('material manifest changed')
        if expected_input_hashes is not None and expected_input_hashes != [entry['sha256'] for entry in manifest['maps']]:
            raise ValueError('material source changed')
        return manifest
    except (zipfile.BadZipFile, RuntimeError, OSError, EOFError, OverflowError, UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        raise ValueError('material bundle unavailable') from exc
