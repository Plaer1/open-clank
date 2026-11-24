import copy
import json
from pathlib import Path

import pytest
from treehouse_global_fixtures import four_root_package

from src.openclank.copal_treehouse import TreeHouseError
from src.openclank.copal_treehouse_repository import TreeHouseRepository, TreeHouseRepositoryError
from src.openclank.treehouse_global_catalogue import (
    GlobalCatalogue, GlobalLearning, SOURCE_ID, canonical_digest,
    load_global_catalogue, subject_requirements_digest,
)


def small_catalogue(path):
    shipped = GlobalCatalogue(four_root_package(path.parent / 'source-package'))
    selected = {'cs:values': ['cs:values:representation', 'cs:values:encoding'],
                'cs:control': shipped.manifest['subjects']['cs:control']['unitIds'][:1],
                'cs:rust': shipped.manifest['subjects']['cs:rust']['unitIds'][:1],
                'games:design': ['games:design:prototype-and-playtest'],
                'ai:models': ['ai:models:identity'],
                'ai:openclank-chat': ['ai:openclank-chat:sessions']}
    path.mkdir()
    (path / 'subjects').mkdir()
    m = copy.deepcopy(shipped.manifest)
    # This deliberately tiny command fixture embeds hydrated native text.
    # It is not an editable source package and must declare that honestly.
    m.pop('contentFormat', None)
    m['subjects'], m['units'], m['assets'], m['resources'] = {}, {}, [], []
    for root in m['mainQuests'].values(): root['members'] = []
    for cid, unit_ids in selected.items():
        original = shipped.subject_state(cid)
        defs = {k: copy.deepcopy(original[k]) for k in ('courses', 'modules', 'activities', 'assignments', 'skills', 'badges', 'quests')}
        defs['activities'] = {k: defs['activities'][k] for k in unit_ids}
        for unit in defs['activities'].values():
            unit.update(assetIds=[], resourceIds=[], content='Supplied isolated progress fixture; explain the stated work.', optionalSections=[])
        aids = [a for u in defs['activities'].values() for a in u['assignmentIds']]
        defs['assignments'] = {k: defs['assignments'][k] for k in aids}
        course = defs['courses'][cid]
        course['completionCriteria'] = {'activityIds': [], 'assignmentIds': aids, 'assignmentMode': 'submitted'}
        course['requirementsDigest'] = subject_requirements_digest(course, defs['assignments'])
        for module in defs['modules'].values(): module.update(activityIds=unit_ids, assignmentIds=aids)
        metadata = shipped.subject_metadata(cid)
        metadata.update(unitIds=unit_ids, completionCriteria=course['completionCriteria'])
        raw = (json.dumps(defs, sort_keys=True, separators=(',', ':'), ensure_ascii=False) + '\n').encode()
        import hashlib
        metadata['definitionDigest'] = 'sha256:' + hashlib.sha256(raw).hexdigest()
        (path / metadata['definitionPath']).write_bytes(raw)
        m['subjects'][cid] = metadata
        for uid in unit_ids:
            m['units'][uid] = {**shipped.manifest['units'][uid], 'assetIds': [], 'resourceIds': []}
        m['mainQuests'][metadata['mainQuestId']]['members'].append({'courseId': cid, 'required': cid != 'cs:rust'})
    cs_units = [k for k in m['units'] if k.startswith('cs:')]
    m['topicLinks'] = [{'fromId': a, 'toId': b, 'type': 'related', 'reason': 'Shared representation practice'} for a, b in zip(cs_units, cs_units[1:])]
    m['topicLinks'].append({'fromId': 'games:design:prototype-and-playtest', 'toId': 'cs:values:representation', 'type': 'requires', 'reason': 'Interpret the toy state before this fixture practical action'})
    m['packageDigest'] = canonical_digest({k: v for k, v in m.items() if k != 'packageDigest'})
    (path / 'manifest.json').write_text(json.dumps(m))
    return GlobalCatalogue(path)


@pytest.fixture
def world(tmp_path):
    return small_catalogue(tmp_path / 'package'), TreeHouseRepository(tmp_path / 'learning.sqlite3')


def command(c, repo, cid, *, learner='alice', kind='submission.submit', answer='A reasoned response', command_id=None, service=None):
    g = service or GlobalLearning(c, repo, learner, 'one')
    unit = c.subject_metadata(cid)['unitIds'][0]
    row = g.rows.get(cid, {'revision': 0, 'resetGeneration': 0})
    payload = {'courseId': cid, 'sourceRef': g.source_ref(cid), 'packageRevision': c.revision, 'resetGeneration': row['resetGeneration']}
    if kind in {'submission.submit', 'submission.draft'}:
        aid = c.unit(cid, unit)['assignmentIds'][0]
        payload.update(assignmentId=aid, answer=answer, assignmentRequirementDigest=c.requirements(aid)['requirementDigest'])
    if kind == 'course.open': payload['activityId'] = unit
    return g.command({'type': kind, 'commandId': command_id or f'{kind}:{cid}:{row["revision"]}', 'actorId': learner, 'expectedRevision': row['revision'], 'payload': payload})


def test_workspace_independent_personal_progress_and_replay(world):
    c, repo = world
    first = command(c, repo, 'ai:models', command_id='once')
    assert first['progress']['complete']
    from src.openclank.treehouse_stats import read
    with repo._connect() as db:
        stats = read(db,'alice','one')
        count = db.execute('SELECT count(*) FROM treehouse_stats_occurrences WHERE account_id=?',('alice',)).fetchone()[0]
        assert count > 0
        assert read(db,'alice','another')['totals'] == stats['totals']
        assert read(db,'alice','another')['history'] == stats['history']
        assert read(db,'bob','another')['totals'] == []
        assert read(db,'bob','another')['history'] == []
    same = GlobalLearning(c, repo, 'alice', 'another')
    assert same.progress('ai:models')['complete']
    assert not GlobalLearning(c, repo, 'bob', 'another').progress('ai:models')['complete']
    with pytest.raises(TreeHouseRepositoryError, match='another payload'):
        command(c, repo, 'ai:models', command_id='once')
    replay = same.command({'type':'submission.submit','commandId':'once','actorId':'alice','expectedRevision':0,'payload':{'courseId':'ai:models','sourceRef':same.source_ref('ai:models'),'packageRevision':c.revision,'resetGeneration':0,'assignmentId':'ai:models:assessment:identity','assignmentRequirementDigest':c.requirements('ai:models:assessment:identity')['requirementDigest'],'answer':'A reasoned response'}})
    assert replay['changed'] is False and replay['replayed']
    with repo._connect() as db:
        assert db.execute('SELECT count(*) FROM treehouse_stats_occurrences WHERE account_id=?',('alice',)).fetchone()[0] == count


def test_practical_failure_pending_success_shape_and_prerequisite(world):
    c, repo = world
    success = {'response': 'Played the toy', 'outcome': {'status': 'success', 'learnerConfirmed': True, 'evidenceText': 'Observed the stated terminal outcome'}}
    with pytest.raises(TreeHouseError, match='Necessary concepts'):
        command(c, repo, 'games:design', answer=success)
    failed = {'response': 'I tried the toy playtest', 'outcome': {'status': 'failed', 'learnerConfirmed': False, 'evidenceText': 'No expected outcome'}}
    pending = command(c, repo, 'games:design', answer=failed)
    assert pending['result']['status'] == 'pending' and not pending['progress']['complete']
    command(c, repo, 'cs:values')
    with pytest.raises(TreeHouseError, match='Confirm observed success'):
        command(c, repo, 'games:design', answer={'response': 'Attempt', 'outcome': {'status': 'success', 'learnerConfirmed': True, 'evidenceText': ''}})
    done = command(c, repo, 'games:design', answer=success)
    assert done['progress']['complete'] and not done['progress']['verified']
    assert any(e['type'] == 'submission.attempt_saved' for e in repo.get_global_progress('alice', SOURCE_ID)['games:design']['state']['events'])


def test_parent_receipt_does_not_duplicate_after_repeat_work(world):
    c, repo = world
    command(c, repo, 'cs:control')
    command(c, repo, 'cs:values')
    g = GlobalLearning(c, repo, 'alice', 'one')
    payload = {'courseId':'cs:values', 'sourceRef':g.source_ref('cs:values'), 'packageRevision':c.revision, 'resetGeneration':0, 'assignmentId':c.unit('cs:values','cs:values:encoding')['assignmentIds'][0], 'answer':'Explicit encoding reasoning'}
    payload['assignmentRequirementDigest'] = c.requirements(payload['assignmentId'])['requirementDigest']
    g.command({'type':'submission.submit','commandId':'encoding','actorId':'alice','expectedRevision':1,'payload':payload})
    before = repo.global_completion_history('alice', SOURCE_ID, 'treehouse:programming')
    assert len(before) == 1
    command(c, repo, 'cs:values', answer='A different reasoned response', command_id='repeat')
    assert repo.global_completion_history('alice', SOURCE_ID, 'treehouse:programming') == before


def test_required_elective_parent_reset_and_semantic_revision_matrix(world):
    c, repo = world
    command(c, repo, 'cs:rust')
    command(c, repo, 'cs:control')
    command(c, repo, 'cs:values')
    g = GlobalLearning(c, repo, 'alice', 'one')
    assert not g.root_progress('treehouse:programming')['complete']
    payload = {'courseId':'cs:values','sourceRef':g.source_ref('cs:values'),'packageRevision':c.revision,'resetGeneration':0,'assignmentId':c.unit('cs:values','cs:values:encoding')['assignmentIds'][0],'answer':'Explain the encoding boundary'}
    payload['assignmentRequirementDigest'] = c.requirements(payload['assignmentId'])['requirementDigest']
    g.command({'type':'submission.submit','commandId':'finish','actorId':'alice','expectedRevision':1,'payload':payload})
    assert GlobalLearning(c,repo,'alice','two').root_progress('treehouse:programming')['complete']
    assignment_id = payload['assignmentId']
    # Cosmetics preserve evidence; an explicit semantic revision invalidates
    # current completion but retains the old historical parent receipt.
    from src.openclank.treehouse_global_catalogue import assignment_requirement_digest
    c._states['cs:values']['assignments'][assignment_id]['prompt'] = 'Corrected typography'
    assert GlobalLearning(c,repo,'alice','two').progress('cs:values')['complete']
    for definition in (c._states['cs:values'], c._projection_states['cs:values']):
        assignment = definition['assignments'][assignment_id]
        assignment['requirementRevision'] += 1
        assignment['requirementDigest'] = assignment_requirement_digest(assignment)
        definition['courses']['cs:values']['requirementsDigest'] = subject_requirements_digest(definition['courses']['cs:values'],definition['assignments'])
    g = GlobalLearning(c,repo,'alice','two')
    assert not g.root_progress('treehouse:programming')['complete']
    assert len(repo.global_completion_history('alice',SOURCE_ID,'treehouse:programming')) == 1
    scopes = [{'courseId':s['id'],'progressRevision':s['progressRevision'],'resetGeneration':s['resetGeneration']} for s in g.root_detail('treehouse:programming')['subjects']]
    envelope = {'type':'progress.reset','commandId':'all-root','expectedRevision':None,'payload':{'sourceRef':g.source_ref(),'packageRevision':c.revision,'mainQuestId':'treehouse:programming','resetScopes':scopes}}
    reset = g.command(envelope)
    assert all(s['resetGeneration'] == 1 and s['progress']['percent'] == 0 for s in reset['subjects'])
    with pytest.raises(TreeHouseError, match='Learning state changed'):
        GlobalLearning(c,repo,'alice','two').command({**envelope,'commandId':'stale-reset'})


def test_prerequisite_reset_between_evaluation_and_commit_is_fenced(world, monkeypatch):
    c, repo = world
    command(c, repo, 'cs:values')
    old = GlobalLearning(c, repo, 'alice', 'one')
    original = repo.put_global_updates
    def concurrent_reset(*args, **kwargs):
        monkeypatch.setattr(repo, 'put_global_updates', original)
        command(c, repo, 'cs:values', kind='progress.reset', command_id='concurrent-reset')
        return original(*args, **kwargs)
    monkeypatch.setattr(repo, 'put_global_updates', concurrent_reset)
    with pytest.raises(TreeHouseRepositoryError, match='Prerequisite evidence changed'):
        command(c, repo, 'games:design', service=old, answer={'response': 'Played', 'outcome': {'status': 'success', 'learnerConfirmed': True, 'evidenceText': 'Outcome observed'}})
    assert 'games:design' not in repo.get_global_progress('alice', SOURCE_ID)


def test_parent_response_uses_transaction_current_sibling_state(world, monkeypatch):
    c, repo = world
    command(c, repo, 'cs:control')
    command(c, repo, 'cs:values')
    old = GlobalLearning(c, repo, 'alice', 'one')
    original = repo.put_global_updates
    def concurrent_reset(*args, **kwargs):
        monkeypatch.setattr(repo, 'put_global_updates', original)
        command(c, repo, 'cs:control', kind='progress.reset', command_id='sibling-reset')
        return original(*args, **kwargs)
    monkeypatch.setattr(repo, 'put_global_updates', concurrent_reset)
    payload = {'courseId': 'cs:values', 'sourceRef': old.source_ref('cs:values'), 'packageRevision': c.revision, 'resetGeneration': 0, 'assignmentId': c.unit('cs:values', 'cs:values:encoding')['assignmentIds'][0], 'answer': 'Explicit encoding reasoning'}
    payload['assignmentRequirementDigest'] = c.requirements(payload['assignmentId'])['requirementDigest']
    response = old.command({'type': 'submission.submit', 'commandId': 'last-values', 'expectedRevision': 1, 'actorId': 'alice', 'payload': payload})
    assert response['progress']['complete']
    assert response['mainQuestProgress'][0]['complete'] is False


def test_dashboard_never_loads_bodies_and_atomic_reset_vector(world, monkeypatch):
    c, repo = world
    monkeypatch.setattr(c, 'subject_state', lambda *_: pytest.fail('Dashboard loaded lesson bodies'))
    g = GlobalLearning(c, repo, 'alice', 'one')
    assert len(g.summary()['mainQuests']) == 4
    detail = g.root_detail('treehouse:programming')
    payload = {'sourceRef': g.source_ref(), 'packageRevision': c.revision, 'mainQuestId': 'treehouse:programming', 'resetScopes': [{'courseId': s['id'], 'progressRevision': s['progressRevision'], 'resetGeneration': s['resetGeneration']} for s in detail['subjects']]}
    response = g.command({'type': 'progress.reset', 'commandId': 'root-reset', 'expectedRevision': None, 'payload': payload})
    assert len(response['subjects']) == 3 and all(s['resetGeneration'] == 1 for s in response['subjects'])
    with pytest.raises(TreeHouseError):
        g.command({'type': 'progress.reset', 'commandId': 'bad', 'payload': {'sourceRef': g.source_ref(), 'packageRevision': c.revision, 'resetScopes': []}})


def test_shipped_loader_strict_assets_and_semantic_spelling_compatibility(tmp_path):
    c = GlobalCatalogue(four_root_package(tmp_path / 'package'))
    assert len(c.manifest['subjects']) == 49 and len(c.manifest['mainQuests']) == 4
    from src.openclank.treehouse_global_catalogue import assignment_requirement_digest
    assignment = c.requirements('ai:models:assessment:identity')
    cosmetic = {**assignment, 'prompt': 'A spelling correction', 'successConditions': ['Same reviewed semantic requirement']}
    assert assignment_requirement_digest(cosmetic) == assignment['requirementDigest']
    assert assignment_requirement_digest({**cosmetic, 'requirementRevision': 2}) != assignment['requirementDigest']


@pytest.mark.parametrize('mutation', ['digest', 'missing', 'ownership', 'membership'])
def test_admission_rejects_tampered_missing_asset_and_invalid_membership(tmp_path, mutation):
    import shutil
    shipped = GlobalCatalogue(four_root_package(tmp_path / 'original'))
    path = tmp_path / 'package'
    shutil.copytree(shipped.path, path)
    manifest = copy.deepcopy(shipped.manifest)
    if mutation == 'digest': manifest['assets'][0]['digest'] = 'sha256:' + '0' * 64
    if mutation == 'missing': manifest['assets'][0]['path'] = 'static/treehouse/assets/not-a-published-asset.json'
    if mutation == 'ownership': manifest['assets'][0]['unitIds'] = ['cs:values:representation']
    if mutation == 'membership': manifest['mainQuests']['treehouse:ai']['members'].append({'courseId':'cs:values', 'required':True})
    manifest['packageDigest'] = canonical_digest({k:v for k,v in manifest.items() if k != 'packageDigest'})
    (path / 'manifest.json').write_text(json.dumps(manifest))
    with pytest.raises((TreeHouseError, FileNotFoundError)):
        GlobalCatalogue(path)


def test_concept_target_does_not_require_optional_practice_and_responses_stay_open(world):
    c, repo = world
    target = c.manifest['units']['cs:values:representation']
    target['assignmentIds'].append('fixture:optional-practice')
    c._projection_states['cs:values']['assignments']['fixture:optional-practice'] = {'id':'fixture:optional-practice', 'completionEvidence':'observed-success'}
    command(c, repo, 'cs:values')
    g = GlobalLearning(c, repo, 'alice', 'one')
    assert g.availability('games:design:prototype-and-playtest')['status'] == 'available'
    assert g.assignment_availability({'completionEvidence':'response','unitId':'games:design:prototype-and-playtest'})['status'] == 'available'
