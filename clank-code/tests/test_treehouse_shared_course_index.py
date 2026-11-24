"""Shared explicit metadata admission; never touch lesson/work source files."""
import json
import os
from pathlib import Path

import pytest

from src.openclank.treehouse_course_files import canonical_bytes, content_head
from src.openclank.treehouse_global_catalogue import (
    GlobalCatalogue, canonical_digest, assignment_requirement_digest, subject_requirements_digest,
)
from src.openclank.treehouse_shared_course_index import (
    latest_shared_index, reindex_shared_course_files,
)
import src.openclank.treehouse_shared_course_index as index

PROJECT = Path(__file__).resolve().parents[1]
PACKAGE = PROJECT / 'src/openclank/treehouse_curriculum'


@pytest.fixture
def source(tmp_path):
    manifest = json.loads((PACKAGE / 'manifest.json').read_bytes())
    assert manifest['contentFormat'] == 'course-files-v1'
    (tmp_path / 'manifest.json').write_bytes(canonical_bytes(manifest))
    for subject in manifest['subjects'].values():
        target = tmp_path / subject['definitionPath']
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((PACKAGE / subject['definitionPath']).read_bytes())
    # No courses/ directory: not even an existence check of body paths is needed.
    return tmp_path


def reindex(root):
    return reindex_shared_course_files(root, project_root=PROJECT)


def edit_json(path, change):
    value = json.loads(path.read_bytes())
    change(value)
    path.write_bytes(canonical_bytes(value))


def raw_subject(root):
    catalogue = json.loads((root / 'catalogue.json').read_bytes())
    cid, record = next(iter(catalogue['subjects'].items()))
    return cid, root / record['definitionPath']


def test_metadata_only_initial_generation_replay_and_root_relative_body_resolution(source, monkeypatch):
    original_read, original_stat, original_lstat, original_open = Path.open, Path.stat, Path.lstat, os.open
    def body(path):
        return path.is_relative_to(source / 'courses')
    def open_(path, *args, **kwargs):
        assert not body(path), f'Body opened: {path}'
        return original_read(path, *args, **kwargs)
    def stat(path, *args, **kwargs):
        assert not body(path), f'Body stat: {path}'
        return original_stat(path, *args, **kwargs)
    def lstat(path, *args, **kwargs):
        assert not body(path), f'Body lstat: {path}'
        return original_lstat(path, *args, **kwargs)
    def os_open(path, *args, **kwargs):
        assert not body(Path(path)), f'Body OS open: {path}'
        return original_open(path, *args, **kwargs)
    with monkeypatch.context() as guarded:
        guarded.setattr(Path, 'open', open_)
        guarded.setattr(Path, 'stat', stat)
        guarded.setattr(Path, 'lstat', lstat)
        guarded.setattr(os, 'open', os_open)
        first = reindex(source)
        replay = reindex(source)
        assert first['generation'] == replay['generation'] == 1
        assert first['replayed'] is False and replay['replayed'] is True
        assert latest_shared_index(source)['packageDigest'] == first['packageDigest']
        catalogue = GlobalCatalogue(source, manifest_path=first['manifestPath'], project_root=PROJECT)
        assert len(catalogue.manifest['subjects']) == 49
        assert len(catalogue.manifest['units']) == 291
    assert not (source / 'courses').exists()
    cid, unit_id = next((cid, uid) for cid, state in catalogue._states.items()
                        for uid, unit in state['activities'].items() if not unit.get('optionalSections'))
    descriptor = catalogue._states[cid]['activities'][unit_id]['contentFile']
    selected = source / descriptor['path']
    selected.parent.mkdir(parents=True)
    selected.write_bytes((PACKAGE / descriptor['path']).read_bytes())
    assert catalogue.unit(cid, unit_id)['content']
    assert not (source / Path(first['manifestPath']).parent / 'courses').exists()


def test_derive_title_units_and_semantic_digests_without_rewriting_raw_metadata(source):
    first = reindex(source)
    cid, path = raw_subject(source)
    def edit(value):
        value['courses'][cid]['title'] = 'Edited course title'
        required = value['courses'][cid]['completionCriteria']['assignmentIds'][0]
        value['assignments'][required]['requirementRevision'] += 1
        value['activities'][next(iter(value['activities']))]['title'] = 'Edited lesson title'
    edit_json(path, edit)
    raw = path.read_bytes()
    second = reindex(source)
    assert second['generation'] == 2 and not second['replayed']
    assert path.read_bytes() == raw
    old_manifest = json.loads((source / first['manifestPath']).read_bytes())
    manifest = json.loads((source / second['manifestPath']).read_bytes())
    assert manifest['originPackageDigest'] == old_manifest['originPackageDigest']
    assert manifest['subjects'][cid]['title'] == 'Edited course title'
    native = json.loads((source / manifest['subjects'][cid]['definitionPath']).read_bytes())
    required = native['courses'][cid]['completionCriteria']['assignmentIds'][0]
    assert native['assignments'][required]['requirementDigest'] == assignment_requirement_digest(native['assignments'][required])
    assert native['courses'][cid]['requirementsDigest'] == subject_requirements_digest(native['courses'][cid], native['assignments'])
    assert manifest['sourceIndex']['subjects'][cid]['head'] == content_head(raw)
    assert old_manifest['subjects'][cid]['title'] != manifest['subjects'][cid]['title']
    assert (source / first['manifestPath']).exists()


def test_catalogue_global_head_fences_roots_links_and_minimal_catalogue_provenance(source):
    first = reindex(source)
    def edit(value):
        value.pop('originPackageDigest')
        value.pop('originPackageRevision')
        value['mainQuests']['treehouse:programming']['title'] = 'Edited Programming title'
    edit_json(source / 'catalogue.json', edit)
    raw = (source / 'catalogue.json').read_bytes()
    second = reindex(source)
    manifest = json.loads((source / second['manifestPath']).read_bytes())
    assert second['sourceDigest'] != first['sourceDigest']
    assert manifest['sourceIndex']['catalogueHead'] == content_head(raw)
    assert manifest['mainQuests']['treehouse:programming']['title'] == 'Edited Programming title'
    assert (source / 'catalogue.json').read_bytes() == raw
    edit_json(source / 'catalogue.json', lambda c: c['topicLinks'].append({
        'fromId':'missing:unit', 'toId':'missing:other', 'type':'related', 'reason':'Invalid edge'}))
    with pytest.raises(Exception, match='Topic edge'):
        reindex(source)
    assert latest_shared_index(source)['generation'] == second['generation']


def test_unresolved_raw_course_remains_local_and_prior_completed_generation_survives_restart(source):
    first = reindex(source)
    manifest = json.loads((source / first['manifestPath']).read_bytes())
    cid, path = raw_subject(source)
    path.write_bytes(b'{broken')
    with pytest.raises(ValueError):
        reindex(source)
    restarted = latest_shared_index(source)
    assert restarted['manifestPath'] == first['manifestPath']
    heads = manifest['sourceIndex']['subjects']
    mismatch = [key for key, ref in heads.items() if content_head((source / ref['path']).read_bytes()) != ref['head']]
    assert mismatch == [cid]
    # Loader's current-source fences consume this exact raw-head vector; the
    # cache remains intact for unrelated courses, not a silent repair of raw.
    assert path.read_bytes() == b'{broken'


def test_corrupt_and_uncommitted_generations_are_never_selected(source):
    first = reindex(source)
    cid, path = raw_subject(source)
    edit_json(path, lambda d: d['courses'][cid].update(title='Second generation'))
    second = reindex(source)
    latest_manifest = json.loads((source / second['manifestPath']).read_bytes())
    cached = source / latest_manifest['subjects'][cid]['definitionPath']
    cached.write_bytes(b'{broken')
    commits = source / '.indexes/commits'
    (commits / '00000000000000000003.json').write_bytes(b'{interrupted')
    (source / '.indexes/uncommitted').mkdir()
    assert latest_shared_index(source)['manifestPath'] == first['manifestPath']
    assert (source / second['manifestPath']).exists()
    assert cached.read_bytes() == b'{broken'


def test_crash_before_commit_never_selects_index_and_explicit_retry_resumes(source, monkeypatch):
    original = index._new
    def interrupted(root, relative, raw):
        if relative.startswith('.indexes/commits/'):
            raise OSError('Interrupted before completed record')
        return original(root, relative, raw)
    monkeypatch.setattr(index, '_new', interrupted)
    with pytest.raises(OSError, match='Interrupted'):
        reindex(source)
    assert latest_shared_index(source) is None
    assert list((source / '.indexes').glob('*/manifest.json'))
    monkeypatch.setattr(index, '_new', original)
    assert reindex(source)['generation'] == 1


def test_metadata_edit_during_admission_cannot_publish_mixed_generation(source, monkeypatch):
    first = reindex(source)
    cid, path = raw_subject(source)
    edit_json(path, lambda d: d['courses'][cid].update(title='Before validation'))
    original = index.GlobalCatalogue
    def changed(*args, **kwargs):
        value = original(*args, **kwargs)
        edit_json(path, lambda d: d['courses'][cid].update(title='Changed during validation'))
        return value
    monkeypatch.setattr(index, 'GlobalCatalogue', changed)
    with pytest.raises(ValueError, match='changed during admission'):
        reindex(source)
    assert latest_shared_index(source)['generation'] == first['generation']


@pytest.mark.parametrize('escape', ['../outside.json', '/outside.json', 'courses/x/work/key.json', '.indexes/old/subjects/x.json'])
def test_source_definition_paths_cannot_select_work_or_escape_metadata(source, escape):
    reindex(source)
    edit_json(source / 'catalogue.json', lambda c: next(iter(c['subjects'].values())).update(definitionPath=escape))
    with pytest.raises(ValueError, match='metadata path'):
        reindex(source)


def test_active_missing_catalogue_never_reseeds_and_symlink_metadata_never_opens(source, tmp_path):
    (source / '.authority.json').write_text('{}')
    with pytest.raises(ValueError, match='catalogue is missing'):
        reindex(source)
    (source / '.authority.json').unlink()
    reindex(source)
    _, path = raw_subject(source)
    outside = tmp_path / 'outside.json'
    outside.write_bytes(path.read_bytes())
    path.unlink()
    path.symlink_to(outside)
    with pytest.raises(ValueError, match='Symlink'):
        reindex(source)


def test_replay_does_not_overwrite_completed_records_or_raw_files(source):
    first = reindex(source)
    files = {p.relative_to(source): p.read_bytes() for p in source.rglob('*.json')}
    second = reindex(source)
    assert second['replayed'] and second['generation'] == first['generation']
    assert {p.relative_to(source): p.read_bytes() for p in source.rglob('*.json')} == files


def test_duplicate_metadata_keys_and_nonregular_source_are_rejected(source):
    reindex(source)
    _, path = raw_subject(source)
    path.write_bytes(b'{"courses":{},"courses":{}}')
    with pytest.raises(ValueError, match='Duplicate'):
        reindex(source)
    path.unlink()
    os.mkfifo(path)
    with pytest.raises(ValueError, match='ordinary file'):
        reindex(source)
