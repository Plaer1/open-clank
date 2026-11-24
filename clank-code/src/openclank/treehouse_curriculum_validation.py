"""Shared, side-effect-free admission rules for published curriculum metadata."""
from __future__ import annotations

import hashlib
import io
import json
import re
import stat
import zipfile
from pathlib import Path, PurePosixPath
from urllib.parse import unquote

ROOT_IDS = frozenset({'treehouse:programming', 'treehouse:games', 'treehouse:ai', 'treehouse:openclank'})
RESOURCE_MIMES = {'data': {'application/json', 'text/plain'}, 'image': {'image/png'},
                  'download': {'application/zip', 'application/octet-stream', 'text/plain'}}


def validate_public_strings(value):
    if isinstance(value, str):
        local_text = re.sub(r'https?://[^\s)\]">]+', '', value)
        if any(token in value.replace('\\', '/') for token in ('.clanker/', '.references/', 'file://')) or any(token in local_text for token in ('/Users/', '/home/')):
            raise ValueError('Private author path in published curriculum')
    elif isinstance(value, dict):
        for key, item in value.items():
            validate_public_strings(key)
            validate_public_strings(item)
    elif isinstance(value, list):
        for item in value:
            validate_public_strings(item)


def validate_fragments(value, unit_ids):
    """Check canonical links in every learner string, including optional/work text."""
    if isinstance(value, str):
        for target in re.findall(r'#treehouse-unit=([^\s)"<>]+)', value):
            if unquote(target) not in unit_ids:
                raise ValueError('Unknown canonical curriculum fragment: ' + target)
    elif isinstance(value, dict):
        for item in value.values():
            validate_fragments(item, unit_ids)
    elif isinstance(value, list):
        for item in value:
            validate_fragments(item, unit_ids)


def validate_sources(sources, units):
    if not isinstance(sources, dict):
        raise ValueError('Source register must be an object')
    validate_public_strings(sources)
    for key, record in sources.items():
        if not isinstance(record, dict) or record.get('id') != key or ':' not in key:
            raise ValueError('Sources need canonical qualified IDs')
        for name in ('path', 'codePaths'):
            paths = record.get(name, [])
            if isinstance(paths, str):
                paths = [paths]
            for path in paths:
                if not isinstance(path, str) or '\\' in path or re.match(r'^[A-Za-z]:', path) or PurePosixPath(path).is_absolute() or '..' in PurePosixPath(path).parts:
                    raise ValueError('Source provenance path escapes publication')
    for unit in units.values():
        if not set(unit.get('sourceKeys', [])).issubset(sources):
            raise ValueError('Unit references missing source')


def validate_archive(raw, provenance):
    """Inspect bounded original source archives; never extract or execute."""
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        entries = archive.infolist()
        if len(entries) > 256 or sum(item.file_size for item in entries) > 32 * 1024 * 1024:
            raise ValueError('Archive exceeds entry or expansion limit')
        names, roots = set(), set()
        for item in entries:
            name = item.orig_filename
            parts = name.rstrip('/').split('/')
            mode = stat.S_IFMT(item.external_attr >> 16)
            if (not name or any(ord(c) < 32 or ord(c) == 127 for c in name) or '\\' in name or ':' in name
                or name.startswith('/') or any(p in {'', '.', '..'} for p in parts)
                or mode not in {0, stat.S_IFREG, stat.S_IFDIR} or item.flag_bits & 1
                or item.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}
                or any(any(c in p for c in '<>"|?*') or p.endswith(('.', ' ')) for p in parts)
                or any(p in {'.git', '.godot', 'node_modules', 'target', '__pycache__'} for p in parts)):
                raise ValueError('Unsafe archive member')
            folded = name.rstrip('/').casefold()
            if folded in names:
                raise ValueError('Duplicate or case-colliding archive path')
            names.add(folded); roots.add(parts[0])
            if Path(name).suffix.lower() in {'.zip', '.jar', '.tar', '.gz', '.tgz', '.7z', '.rar'}:
                # The sole reviewed bootstrap exception is pinned by member,
                # upstream URL, license and actual bytes in public provenance.
                approved = next((c for c in provenance.get('components', []) if isinstance(c, dict) and c.get('path') == name), None)
                if not (name.endswith('/gradle/wrapper/gradle-wrapper.jar') and approved and str(approved.get('url', '')).startswith('https://') and approved.get('license')
                        and approved.get('digest') == 'sha256:' + hashlib.sha256(archive.read(item)).hexdigest()):
                    raise ValueError('Nested archive is not an admitted bootstrap')
        if len(roots) != 1 or any('/' not in item.filename.rstrip('/') and not item.is_dir() for item in entries):
            raise ValueError('Archive needs one named project root')
        if archive.testzip() is not None:
            raise ValueError('Corrupt archive member')


def validate_resources(manifest, project_root, *, check_bytes=True):
    try:
        return _validate_resources(manifest, project_root, check_bytes=check_bytes)
    except (TypeError, KeyError, OSError, UnicodeError, zipfile.BadZipFile, RuntimeError) as exc:
        raise ValueError('Invalid public resource register or bytes') from exc


def _validate_resources(manifest, project_root, *, check_bytes=True):
    resources = manifest.get('resources', [])
    if not isinstance(resources, list) or len(resources) > 128:
        raise ValueError('Invalid resource register')
    root = Path(project_root).resolve()
    found, total = {}, 0
    asset_ids = {a['id'] for a in manifest.get('assets', [])}
    fields = {'id', 'path', 'mime', 'bytes', 'digest', 'unitIds', 'license', 'provenance', 'kind'}
    for record in resources:
        if not isinstance(record, dict) or set(record) != fields:
            raise ValueError('Invalid resource descriptor fields')
        ident, relative = record['id'], record['path']
        if not isinstance(ident, str) or not re.fullmatch(r'[a-z][a-z0-9-]{0,127}', ident) or ident in found or ident in asset_ids:
            raise ValueError('Duplicate or invalid resource identity')
        if not isinstance(relative, str) or not re.fullmatch(r'static/treehouse/(fixtures|projects)/[a-z0-9][a-z0-9/_.-]*', relative) or '..' in PurePosixPath(relative).parts or str(PurePosixPath(relative)) != relative:
            raise ValueError('Resource path escapes public roots')
        path = (root / relative).resolve()
        allowed = [root / 'static/treehouse' / name for name in ('fixtures', 'projects')]
        if not any(path.is_relative_to(base) for base in allowed) or not path.is_relative_to(root) or (check_bytes and not path.is_file()):
            raise ValueError('Missing or escaping public resource')
        if not isinstance(record['kind'], str) or not isinstance(record['mime'], str) or record['mime'] not in RESOURCE_MIMES.get(record['kind'], set()):
            raise ValueError('Unsupported resource MIME/kind')
        suffixes = {'application/json': {'.json'}, 'text/plain': {'.txt'}, 'image/png': {'.png'}, 'application/zip': {'.zip'}, 'application/octet-stream': {'.bin'}}
        if path.suffix not in suffixes[record['mime']]:
            raise ValueError('Resource extension differs from approved MIME')
        if type(record['bytes']) is not int or not 0 <= record['bytes'] <= 8 * 1024 * 1024 or (check_bytes and path.stat().st_size != record['bytes']):
            raise ValueError('Resource byte length differs')
        total += record['bytes']
        if total > 32 * 1024 * 1024:
            raise ValueError('Resource register exceeds byte limit')
        if not isinstance(record['digest'], str) or not re.fullmatch(r'sha256:[a-f0-9]{64}', record['digest']) or (check_bytes and record['digest'] != 'sha256:' + hashlib.sha256(path.read_bytes()).hexdigest()):
            raise ValueError('Resource digest differs')
        ids = record['unitIds']
        if not isinstance(ids, list) or not ids or len(ids) != len(set(ids)) or not set(ids).issubset(manifest['units']):
            raise ValueError('Invalid resource unit ownership')
        if not isinstance(record['license'], str) or not record['license'].strip() or not isinstance(record['provenance'], dict) or not record['provenance']:
            raise ValueError('Resource needs license and provenance')
        validate_public_strings(record)
        if check_bytes and record['mime'] in {'text/plain', 'application/json'}:
            text = path.read_text(encoding='utf-8')
            validate_public_strings(text)
            if '\x00' in text:
                raise ValueError('Text resource contains binary data')
            if record['mime'] == 'application/json':
                json.loads(text)
        if check_bytes and record['mime'] == 'image/png' and not path.read_bytes().startswith(b'\x89PNG\r\n\x1a\n'):
            raise ValueError('Image resource is not PNG')
        if check_bytes and record['mime'] == 'application/zip':
            validate_archive(path.read_bytes(), record['provenance'])
        found[ident] = record
    for uid, unit in manifest['units'].items():
        ids = unit.get('resourceIds', [])
        if not isinstance(ids, list) or len(ids) > 64 or len(ids) != len(set(ids)) or not set(ids).issubset(found):
            raise ValueError('Unit references invalid resource')
        if any(uid not in found[ident]['unitIds'] for ident in ids):
            raise ValueError('Foreign resource association')
    for ident, record in found.items():
        if any(ident not in manifest['units'][uid].get('resourceIds', []) for uid in record['unitIds']):
            raise ValueError('Resource association differs from unit')
    return found
