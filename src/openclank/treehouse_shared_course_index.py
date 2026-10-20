"""Explicit metadata-only admission of the editable shared course source.

Raw catalogue/course JSON remains editable authority. Completed immutable
generations are derived indexes, never replacement lesson or work bodies.
"""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import tempfile

from src.openclank.copal_commit_lock import copal_commit_lock
from src.openclank.treehouse_course_files import canonical_bytes, content_head, source_key
from src.openclank.treehouse_global_catalogue import (
    GlobalCatalogue, KINDS, SOURCE_ID, assignment_requirement_digest,
    canonical_digest, subject_requirements_digest,
)

CATALOGUE_FORMAT = 'treehouse-course-catalogue-v1'
RECEIPT_FORMAT = 'treehouse-course-index-v1'
MAX_METADATA_BYTES = 16 * 1024 * 1024
_DIGEST = re.compile(r'sha256:[0-9a-f]{64}\Z')
_COMMIT = re.compile(r'([0-9]{20})\.json\Z')


def _path(root: Path, relative: str) -> Path:
    """Contain metadata without resolving or inspecting any body reference."""
    if (not isinstance(relative, str) or not relative or '\\' in relative or ':' in relative
            or PurePosixPath(relative).is_absolute()
            or any(part in {'', '.', '..'} for part in relative.split('/'))):
        raise ValueError('Invalid shared metadata path')
    target = root / relative
    for part in (target, *target.parents):
        if part == root.parent:
            break
        if part.is_symlink():
            raise ValueError('Symlink shared metadata path')
    return target


def _read(root: Path, relative: str) -> bytes:
    path = _path(root, relative)
    descriptor = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0))
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise ValueError('Shared metadata is not an ordinary file')
        with os.fdopen(descriptor, 'rb', closefd=False) as stream:
            raw = stream.read(MAX_METADATA_BYTES + 1)
    finally:
        os.close(descriptor)
    if len(raw) > MAX_METADATA_BYTES:
        raise ValueError('Shared metadata exceeds limit')
    return raw


def _object(raw: bytes) -> dict:
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError('Duplicate shared metadata key')
            result[key] = value
        return result
    value = json.loads(raw, object_pairs_hook=unique)
    if not isinstance(value, dict):
        raise ValueError('Shared metadata must be an object')
    return value


def _new(root: Path, relative: str, raw: bytes) -> None:
    """Publish a complete new file atomically, without replacing an old one."""
    target = _path(root, relative)
    target.parent.mkdir(parents=True, exist_ok=True)
    # Repeat after mkdir: refuse an existing index/source symlink component.
    target = _path(root, relative)
    if target.exists():
        if _read(root, relative) != raw:
            raise ValueError('Immutable shared metadata already differs')
        return
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(prefix='.index-', dir=target.parent, delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, target)
        except FileExistsError:
            if _read(root, relative) != raw:
                raise ValueError('Concurrent shared metadata publication differs')
        if os.name != 'nt':
            descriptor = os.open(target.parent, os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _seed(root: Path) -> None:
    if _path(root, 'catalogue.json').exists():
        return
    if _path(root, '.authority.json').exists():
        raise ValueError('Active shared source catalogue is missing; repair it explicitly')
    origin = _object(_read(root, 'manifest.json'))
    if (origin.get('contentFormat') != 'course-files-v1'
            or origin.get('packageDigest') != canonical_digest({k: v for k, v in origin.items() if k != 'packageDigest'})):
        raise ValueError('Initial shared publication must be an admitted course-file package')
    catalogue = {key: copy.deepcopy(origin[key]) for key in (
        'sourceId', 'packageRevision', 'mainQuests', 'sources', 'topicLinks', 'assets', 'resources')}
    catalogue.update(format=CATALOGUE_FORMAT, originPackageRevision=origin['packageRevision'],
                     originPackageDigest=origin['packageDigest'], subjects={
                         cid: {'id': cid, 'definitionPath': metadata['definitionPath']}
                         for cid, metadata in origin['subjects'].items()})
    _new(root, 'catalogue.json', canonical_bytes(catalogue))


def _inputs(root: Path) -> tuple[dict, dict, dict, str]:
    raw = _read(root, 'catalogue.json')
    catalogue = _object(raw)
    if (catalogue.get('format') != CATALOGUE_FORMAT or catalogue.get('sourceId') != SOURCE_ID
            or not isinstance(catalogue.get('subjects'), dict) or not catalogue['subjects']
            or not isinstance(catalogue.get('packageRevision'), str) or not catalogue['packageRevision']):
        raise ValueError('Unsupported shared source catalogue')
    allowed = {'format', 'sourceId', 'packageRevision', 'mainQuests', 'sources', 'topicLinks',
               'assets', 'resources', 'subjects', 'originPackageRevision', 'originPackageDigest'}
    if set(catalogue) - allowed:
        raise ValueError('Shared source catalogue has duplicate/unknown index authority')
    if 'originPackageDigest' not in catalogue or 'originPackageRevision' not in catalogue:
        origin = _object(_read(root, 'manifest.json'))
        if origin.get('packageDigest') != canonical_digest({k: v for k, v in origin.items() if k != 'packageDigest'}):
            raise ValueError('Shared publication provenance differs')
        catalogue.setdefault('originPackageDigest', origin['packageDigest'])
        catalogue.setdefault('originPackageRevision', origin['packageRevision'])
    definitions, subjects, paths = {}, {}, set()
    for cid, entry in sorted(catalogue['subjects'].items()):
        if not isinstance(entry, dict) or set(entry) != {'id', 'definitionPath'} or entry['id'] != cid:
            raise ValueError('Invalid shared source subject identity')
        path = entry['definitionPath']
        # Source course metadata is deliberately separate from lessons/work.
        if not isinstance(path, str) or not path.startswith('subjects/') or not path.endswith('.json') or path in paths:
            raise ValueError('Invalid or duplicate editable subject metadata path')
        source = _read(root, path)
        paths.add(path)
        definitions[cid] = _object(source)
        subjects[cid] = {'path': path, 'head': content_head(source)}
    source_index = {'cataloguePath': 'catalogue.json', 'catalogueHead': content_head(raw), 'subjects': subjects}
    digest = canonical_digest(source_index)
    return catalogue, definitions, source_index, digest


def _manifest(catalogue: dict, definitions: dict, source_index: dict, source_digest: str) -> tuple[dict, dict]:
    manifest = {key: copy.deepcopy(catalogue[key]) for key in (
        'sourceId', 'mainQuests', 'sources', 'topicLinks', 'assets', 'resources')}
    manifest.update(format='openclank-treehouse-global-v1', schemaVersion=1,
                    contentFormat='course-files-v1', subjects={}, units={}, sourceIndex=source_index,
                    sourceDigest=source_digest, packageRevision=catalogue['packageRevision'] + '+files.' + source_digest[7:23],
                    originPackageRevision=catalogue.get('originPackageRevision', catalogue['packageRevision']),
                    originPackageDigest=catalogue.get('originPackageDigest'))
    if not _DIGEST.fullmatch(str(manifest['originPackageDigest'])):
        raise ValueError('Shared origin package digest is missing or invalid')
    owners = {}
    for root_id, root in manifest['mainQuests'].items():
        for member in root['members']:
            cid = member['courseId']
            if cid in owners:
                raise ValueError('Duplicate shared root membership')
            owners[cid] = root_id
    files = {}
    for cid, original in definitions.items():
        definition = copy.deepcopy(original)
        if set(definition) != set(KINDS) or set(definition['courses']) != {cid}:
            raise ValueError('Invalid shared native definition maps')
        for assignment in definition['assignments'].values():
            assignment['requirementDigest'] = assignment_requirement_digest(assignment)
        course = definition['courses'][cid]
        course['requirementsDigest'] = subject_requirements_digest(course, definition['assignments'])
        order = [identity for mid in course['moduleIds'] for identity in definition['modules'][mid]['activityIds']]
        relative = f'.indexes/{source_digest[7:]}/subjects/{source_key(cid)}.json'
        raw = canonical_bytes(definition)
        files[relative] = raw
        metadata = {key: copy.deepcopy(course[key]) for key in (
            'id', 'title', 'objective', 'curriculumRevision', 'completionCriteria', 'requirementsDigest')}
        metadata.update(mainQuestId=owners[cid], definitionPath=relative,
                        definitionDigest=content_head(raw), unitIds=order)
        manifest['subjects'][cid] = metadata
        for identity, activity in definition['activities'].items():
            if identity in manifest['units']:
                raise ValueError('Duplicate shared unit identity')
            manifest['units'][identity] = {key: copy.deepcopy(activity[key]) for key in (
                'id', 'courseId', 'title', 'outcome', 'assignmentIds', 'sourceKeys', 'assetIds', 'resourceIds', 'readingOnly')}
    manifest['packageDigest'] = canonical_digest(manifest)
    return manifest, files


def _valid_commit(root: Path, name: str) -> dict | None:
    try:
        receipt = _object(_read(root, '.indexes/commits/' + name))
        if (receipt.get('format') != RECEIPT_FORMAT or type(receipt.get('generation')) is not int
                or receipt['generation'] < 1 or name != f"{receipt['generation']:020d}.json"
                or receipt.get('receiptDigest') != canonical_digest({k: v for k, v in receipt.items() if k != 'receiptDigest'})
                or not _DIGEST.fullmatch(str(receipt.get('sourceDigest')))):
            return None
        prefix = '.indexes/' + receipt['sourceDigest'][7:] + '/'
        if receipt.get('manifestPath') != prefix + 'manifest.json':
            return None
        manifest = _object(_read(root, receipt['manifestPath']))
        if (manifest.get('format') != 'openclank-treehouse-global-v1'
                or manifest.get('schemaVersion') != 1 or manifest.get('sourceId') != SOURCE_ID
                or manifest.get('contentFormat') != 'course-files-v1'
                or not isinstance(manifest.get('subjects'), dict) or not manifest['subjects']
                or set(manifest['sourceIndex']['subjects']) != set(manifest['subjects'])
                or manifest.get('packageDigest') != receipt.get('packageDigest')
                or manifest.get('packageDigest') != canonical_digest({k: v for k, v in manifest.items() if k != 'packageDigest'})
                or manifest.get('packageRevision') != receipt.get('packageRevision')
                or manifest.get('sourceDigest') != receipt['sourceDigest']
                or canonical_digest(manifest.get('sourceIndex')) != receipt['sourceDigest']):
            return None
        for cid, subject in manifest['subjects'].items():
            if subject['definitionPath'] != prefix + 'subjects/' + source_key(cid) + '.json':
                return None
            if content_head(_read(root, subject['definitionPath'])) != subject['definitionDigest']:
                return None
        return receipt
    except (ValueError, OSError, KeyError, TypeError, OverflowError):
        return None


def latest_shared_index(root: str | Path) -> dict | None:
    """Select only a complete intact generation; never inspect current bodies."""
    root = Path(root).resolve()
    try:
        commits = _path(root, '.indexes/commits')
        if not commits.exists():
            return None
        names = [p.name for p in commits.iterdir() if _COMMIT.fullmatch(p.name)]
    except (OSError, ValueError):
        return None
    for name in sorted(names, reverse=True):
        receipt = _valid_commit(root, name)
        if receipt is not None:
            return copy.deepcopy(receipt)
    return None


def reindex_shared_course_files(root: str | Path, *, project_root: str | Path | None = None) -> dict:
    """Admit raw metadata under the existing writer lock, then commit last."""
    supplied = Path(root)
    if supplied.is_symlink() or not supplied.is_dir():
        raise ValueError('Shared course source root must be an existing ordinary directory')
    root = supplied.resolve()
    with copal_commit_lock(root):
        _seed(root)
        catalogue, definitions, source_index, source_digest = _inputs(root)
        latest = latest_shared_index(root)
        if latest and latest['sourceDigest'] == source_digest:
            return {**latest, 'replayed': True}
        manifest, files = _manifest(catalogue, definitions, source_index, source_digest)
        relative = f'.indexes/{source_digest[7:]}/manifest.json'
        for path, raw in files.items():
            _new(root, path, raw)
        _new(root, relative, canonical_bytes(manifest))
        GlobalCatalogue(root, project_root=project_root, manifest_path=relative)
        # External editors do not take our lock. Never admit a mixed read set.
        if _inputs(root)[2] != source_index:
            raise ValueError('Shared metadata changed during admission; reindex explicitly')
        commits = _path(root, '.indexes/commits')
        commits.mkdir(parents=True, exist_ok=True)
        generations = [int(m.group(1)) for p in commits.iterdir() if (m := _COMMIT.fullmatch(p.name))]
        generation = max(generations, default=0) + 1
        if generation >= 10**20:
            raise ValueError('Shared generation counter exhausted')
        receipt = {'format': RECEIPT_FORMAT, 'generation': generation, 'manifestPath': relative,
                   'packageDigest': manifest['packageDigest'], 'packageRevision': manifest['packageRevision'],
                   'sourceDigest': source_digest}
        receipt['receiptDigest'] = canonical_digest(receipt)
        _new(root, f'.indexes/commits/{generation:020d}.json', canonical_bytes(receipt))
        return {**copy.deepcopy(receipt), 'replayed': False}
