"""Finite expansion contract cases; only synthetic packages and temporary stores."""
import copy
import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

from test_treehouse_global import world, command, small_catalogue
from src.openclank.copal_treehouse import TreeHouseError
from src.openclank.copal_treehouse_repository import TreeHouseRepository
from src.openclank.treehouse_global_catalogue import (
    GlobalCatalogue, GlobalLearning, SOURCE_ID, canonical_digest,
    assignment_requirement_digest, subject_requirements_digest,
)
from src.openclank.treehouse_curriculum_validation import validate_resources


def envelope(c, repo, *, kind='submission.submit', cid='ai:models', **changes):
    g = GlobalLearning(c, repo, 'alice', 'one')
    aid = c.unit(cid, c.subject_metadata(cid)['unitIds'][0])['assignmentIds'][0]
    row = g.rows.get(cid, {'revision': 0, 'resetGeneration': 0})
    payload = {'courseId': cid, 'sourceRef': g.source_ref(cid), 'packageRevision': c.revision,
               'resetGeneration': row['resetGeneration'], 'assignmentId': aid,
               'assignmentRequirementDigest': c.requirements(aid)['requirementDigest'], 'answer': 'Reasoned paper response'}
    payload.update(changes)
    return {'type': kind, 'commandId': 'fixture:' + kind + ':' + str(row['revision']),
            'expectedRevision': row['revision'], 'payload': payload}


def revise(c, cid):
    for state in (c._states[cid], c._projection_states[cid]):
        aid = next(iter(state['assignments']))
        state['assignments'][aid]['requirementRevision'] += 1
        state['assignments'][aid]['requirementDigest'] = assignment_requirement_digest(state['assignments'][aid])
        state['courses'][cid]['requirementsDigest'] = subject_requirements_digest(state['courses'][cid], state['assignments'])


@pytest.mark.parametrize('kind', ['submission.submit', 'submission.draft'])
@pytest.mark.parametrize('digest', [None, 'sha256:' + '0' * 64])
def test_missing_or_changed_displayed_requirement_never_rebases(world, kind, digest):
    c, repo = world
    env = envelope(c, repo, kind=kind, assignmentRequirementDigest=digest)
    with pytest.raises(TreeHouseError) as exc:
        GlobalLearning(c, repo, 'alice', 'one').command(env)
    assert exc.value.code == 'stale_requirement'
    assert repo.get_global_progress('alice', SOURCE_ID) == {}


def test_changed_requirement_package_and_generation_fences(world):
    c, repo = world
    old = envelope(c, repo)
    revise(c, 'ai:models')
    with pytest.raises(TreeHouseError) as exc:
        GlobalLearning(c, repo, 'alice', 'one').command(old)
    assert exc.value.code == 'stale_requirement'
    c.revision = 'fixture:next-package'
    with pytest.raises(TreeHouseError) as exc:
        GlobalLearning(c, repo, 'alice', 'one').command(old)
    assert exc.value.code == 'stale_curriculum'
    command(c, repo, 'ai:models', kind='progress.reset')
    fresh = envelope(c, repo, resetGeneration=0)
    with pytest.raises(TreeHouseError) as exc:
        GlobalLearning(c, repo, 'alice', 'one').command(fresh)
    assert exc.value.code == 'stale'
    assert not GlobalLearning(c, repo, 'alice', 'one').progress('ai:models')['complete']


def test_saved_drafts_and_answers_have_honest_recovery_status(world):
    c, repo = world
    command(c, repo, 'ai:models', kind='submission.draft', answer='Saved draft')
    unit = 'ai:models:identity'
    detail = GlobalLearning(c, repo, 'alice', 'two').unit_detail('ai:models', unit)
    aid = detail['assignments'][0]['id']
    assert detail['submissions'][aid]['draftRecoveryStatus'] == 'current'
    assert not detail['progress']['complete']
    revise(c, 'ai:models')
    detail = GlobalLearning(c, repo, 'alice', 'two').unit_detail('ai:models', unit)
    assert detail['submissions'][aid]['draftAnswer'] == 'Saved draft'
    assert detail['submissions'][aid]['draftRecoveryStatus'] == 'previous'
    # Legacy unversioned draft remains recoverable, never inferred current.
    with repo._connect() as db:
        row = db.execute('SELECT state_json FROM treehouse_global_progress WHERE learner_account_id=? AND course_id=?', ('alice', 'ai:models')).fetchone()
        state = json.loads(row[0])
        next(iter(state['submissions'].values())).pop('draftRequirementDigest')
        db.execute('UPDATE treehouse_global_progress SET state_json=? WHERE learner_account_id=? AND course_id=?', (json.dumps(state), 'alice', 'ai:models'))
    assert GlobalLearning(c, repo, 'alice', 'two').unit_detail('ai:models', unit)['submissions'][aid]['draftRecoveryStatus'] == 'unknown'
    command(c, repo, 'ai:models', answer='Explicit revised response')
    detail = GlobalLearning(c, repo, 'alice', 'two').unit_detail('ai:models', unit)
    assert detail['submissions'][aid]['answerRecoveryStatus'] == 'current'
    assert detail['progress']['complete']
    revise(c, 'ai:models')
    detail = GlobalLearning(c, repo, 'alice', 'two').unit_detail('ai:models', unit)
    assert detail['submissions'][aid]['answerRecoveryStatus'] == 'previous'
    assert not detail['progress']['complete']
    command(c, repo, 'ai:models', kind='progress.reset')
    detail = GlobalLearning(c, repo, 'alice', 'two').unit_detail('ai:models', unit)
    assert detail['submissions'][aid]['answerRecoveryStatus'] == 'reset'
    assert not GlobalLearning(c, repo, 'bob', 'two').unit_detail('ai:models', unit)['submissions']


@pytest.mark.parametrize('trigger', ['course.open', 'submission.draft'])
def test_reparented_existing_evidence_recognized_on_command_not_get(world, trigger):
    c, repo = world
    # Old AI required both subjects; OC is deliberately dormant in this
    # pre-move in-memory fixture, never an admissible published package.
    moved = 'ai:openclank-chat'
    c.manifest['subjects'][moved]['mainQuestId'] = 'treehouse:ai'
    c.manifest['mainQuests']['treehouse:ai']['members'].append({'courseId': moved, 'required': True})
    command(c, repo, 'ai:models')
    command(c, repo, moved)
    old = repo.global_completion_history('alice', SOURCE_ID, 'treehouse:ai')
    assert len(old) == 1 and len(old[0]['children']) == 2
    c.manifest['subjects'][moved]['mainQuestId'] = 'treehouse:openclank'
    c.manifest['mainQuests']['treehouse:ai']['members'] = [{'courseId':'ai:models','required':True}]
    before = copy.deepcopy(repo.get_global_progress('alice', SOURCE_ID))
    g = GlobalLearning(c, repo, 'alice', 'two')
    assert g.root_detail('treehouse:openclank')['mainQuest']['complete']
    assert repo.global_completion_history('alice', SOURCE_ID, 'treehouse:openclank') == []
    assert repo.get_global_progress('alice', SOURCE_ID) == before
    response = command(c, repo, moved, kind=trigger)
    history = repo.global_completion_history('alice', SOURCE_ID, 'treehouse:openclank')
    assert len(history) == 1 and history[0]['mainQuestId'] == 'treehouse:openclank'
    assert response['mainQuestProgress'][0]['historicalCompletions'] == history
    assert next(r for r in response['globalCatalogue']['mainQuests'] if r['id'] == 'treehouse:openclank')['historicalCompletions'] == history
    assert repo.global_completion_history('alice', SOURCE_ID, 'treehouse:ai') == old
    command(c, repo, moved, kind=trigger, command_id='distinct-repeat')
    assert repo.global_completion_history('alice', SOURCE_ID, 'treehouse:openclank') == history


def test_receipt_failure_rolls_back_work_stats_and_idempotency(world, monkeypatch):
    c, repo = world
    env = envelope(c, repo)
    original = repo.put_global_updates
    def fail_receipt_producer(learner, source, command_id, payload, updates, producer, **kwargs):
        def failing_response(current_rows):
            # Called inside BEGIN IMMEDIATE, after progress/stat writes. Do not
            # corrupt the schema: production admission rightly rejects triggers.
            value = producer(current_rows)
            assert value['_completionReceipts']
            assert current_rows['ai:models']['revision'] == 1
            raise RuntimeError('fixture failure')
        return original(learner, source, command_id, payload, updates, failing_response, **kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(repo, 'put_global_updates', fail_receipt_producer)
        with pytest.raises(RuntimeError, match='fixture failure'):
            GlobalLearning(c, repo, 'alice', 'one').command(env)
    assert repo.get_global_progress('alice', SOURCE_ID) == {}
    with repo._connect() as db:
        assert db.execute('SELECT count(*) FROM treehouse_global_idempotency').fetchone()[0] == 0
        assert db.execute('SELECT count(*) FROM treehouse_stats_occurrences').fetchone()[0] == 0
    result = GlobalLearning(c, repo, 'alice', 'one').command(env)
    assert len(result['mainQuestProgress'][0]['historicalCompletions']) == 1
    replay = GlobalLearning(c, repo, 'alice', 'two').command(env)
    assert replay['replayed'] and not replay['changed']
    assert len(repo.global_completion_history('alice', SOURCE_ID, 'treehouse:ai')) == 1


def resource_fixture(tmp_path):
    path = tmp_path / 'static/treehouse/fixtures/input.txt'
    path.parent.mkdir(parents=True)
    path.write_bytes(b'complete input\n')
    uid = 'ai:models:identity'
    record = {'id':'paper-input', 'path':'static/treehouse/fixtures/input.txt', 'mime':'text/plain', 'bytes':path.stat().st_size,
              'digest':'sha256:' + hashlib.sha256(path.read_bytes()).hexdigest(), 'unitIds':[uid], 'license':'MIT',
              'provenance':{'kind':'first-party-fixture', 'status':'synthetic'}, 'kind':'data'}
    return {'resources':[record], 'units':{uid:{'resourceIds':['paper-input']}}}


@pytest.mark.parametrize('mutation', ['bytes','digest','mime','path','association','missing','private','active','symlink','limit'])
def test_resource_bytes_and_authority_admission(tmp_path, mutation):
    m = resource_fixture(tmp_path)
    assert validate_resources(m, tmp_path)['paper-input']['bytes'] == 15
    record = m['resources'][0]
    if mutation == 'bytes': record['bytes'] += 1
    if mutation == 'digest': record['digest'] = 'sha256:' + '0' * 64
    if mutation == 'mime': record['mime'] = 'text/html'
    if mutation == 'path': record['path'] = 'static/treehouse/fixtures/../../private.txt'
    if mutation == 'association': m['units']['ai:models:identity']['resourceIds'] = []
    if mutation == 'missing': (tmp_path / record['path']).unlink()
    if mutation == 'private': record['provenance']['path'] = '.clanker/secret.md'
    if mutation == 'active':
        target = tmp_path / 'static/treehouse/fixtures/input.html'
        target.write_bytes(b'complete input\n'); record['path'] = 'static/treehouse/fixtures/input.html'
    if mutation == 'symlink':
        target = tmp_path / record['path']; target.unlink(); (tmp_path / 'outside.txt').write_bytes(b'complete input\n'); target.symlink_to(tmp_path / 'outside.txt')
    if mutation == 'limit': m['resources'] *= 129
    with pytest.raises((ValueError, OSError)):
        validate_resources(m, tmp_path)


def converter_module():
    path = Path(__file__).resolve().parents[1] / 'scripts/build_treehouse_curriculum.py'
    spec = importlib.util.spec_from_file_location('expansion_converter', path)
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    return module


def author_packets(tmp_path):
    packets = []
    roots = [('programming','cs:values'),('games','games:design'),('ai','ai:models'),('openclank','ai:openclank-chat')]
    for name, sid in roots:
        path = tmp_path / name; (path / 'units').mkdir(parents=True)
        uid, aid = sid + ':toy', sid + ':assessment:toy'
        data = {'schemaVersion':1, 'mainQuest':{'id':'treehouse:'+name,'title':name,'curriculumRevision':2,'rollupRuleVersion':1,'members':[{'courseId':sid,'required':True}]},
                'subjects':[{'id':sid,'title':name,'objective':'Reason about supplied input','courseType':'quest','learningContractVersion':1,'curriculumRevision':2,'freeExploration':True,'completionCriteria':{'activityIds':[],'assignmentIds':[aid],'assignmentMode':'submitted'},'units':[uid]}],
                'units':[{'id':uid,'subjectId':sid,'title':'Toy','outcome':'Explain input','bodyPath':'units/toy.md','assignmentId':aid,'sourceKeys':['shared:fixture']}],
                'assignments':[{'id':aid,'unitId':uid,'prompt':'Explain the supplied toy.','successConditions':['Reason from input.'],'failureFeedback':'Check input.','hint':'Read given rule.','evidenceType':'self-report','verificationLimit':'Paper only.','promptInBody':False}],
                'topicLinks':[], 'sources':[{'id':'shared:fixture','kind':'original','title':'Shared supplied fixture'}]}
        if name == 'games':
            route = sid + ':route'
            data['units'].append({'id':route,'subjectId':sid,'title':'Routes','outcome':'Find topic','bodyPath':'units/route.md','assignmentId':None,'readingOnly':True})
            data['subjects'][0]['units'].append(route)
            data['topicLinks'].append({'fromId':uid,'toId':route,'type':'related','reason':'Choose practice'})
            (path / 'units/route.md').write_text('Read the routes; no completion credit.')
        (path / 'units/toy.md').write_text('[Application](#treehouse-unit=ai%3Aopenclank-chat%3Atoy)\nSupplied rule: add one to two.')
        target = path / 'manifest.md'; target.write_text('```json\n'+json.dumps(data)+'\n```');packets.append(target)
    return packets


def test_converter_four_roots_shared_sources_route_and_explicit_prompt(tmp_path):
    module = converter_module(); packets = author_packets(tmp_path)
    manifest, files = module.convert(packets, 'fixture:2')
    assert len(manifest['mainQuests']) == 4 and len(manifest['sources']) == 1
    assert manifest['units']['games:design:route']['assignmentIds'] == []
    assert manifest['units']['games:design:route']['readingOnly']
    assert not json.loads(files['subjects/ai-openclank-chat.json'])['assignments']['ai:openclank-chat:assessment:toy']['promptInBody']
    out = tmp_path / 'native';out.mkdir();(out / 'subjects').mkdir()
    for name, raw in files.items(): (out / name).write_bytes(raw)
    admitted = GlobalCatalogue(out)
    assert len(admitted.manifest['mainQuests']) == 4
    assert 'games:design:route' not in admitted.subject_metadata('games:design')['completionCriteria']['activityIds']
    with pytest.raises(ValueError, match='four'):
        module.convert(packets[:3], 'fixture:2')


@pytest.mark.parametrize('mutation', ['source','fragment','optional-fragment','work-fragment','private','route','edge'])
def test_converter_rejects_conflicts_or_unresolved_learner_strings(tmp_path, mutation):
    module = converter_module(); packets = author_packets(tmp_path)
    data = module.author_manifest(packets[-1])
    if mutation == 'source': data['sources'][0]['title'] = 'Conflicting descriptor'
    if mutation == 'fragment': (packets[-1].parent / 'units/toy.md').write_text('[Missing](#treehouse-unit=ai%3Amissing%3Atoy)')
    if mutation == 'optional-fragment': data['units'][0]['optionalSections'] = [{'title':'More','content':'[Missing](#treehouse-unit=ai%3Amissing%3Atoy)'}]
    if mutation == 'work-fragment': data['assignments'][0]['failureFeedback'] = '[Missing](#treehouse-unit=ai%3Amissing%3Atoy)'
    if mutation == 'private': data['sources'][0]['path'] = '.clanker/private.md'
    if mutation == 'route':
        route = module.author_manifest(packets[1]); route['units'][-1].pop('readingOnly');packets[1].write_text('```json\n'+json.dumps(route)+'\n```')
    if mutation == 'edge':
        edge = {'fromId':'ai:models:toy','toId':'ai:openclank-chat:toy','type':'related','reason':'A'}
        data['topicLinks'] = [edge, {**edge,'reason':'B'}]
    packets[-1].write_text('```json\n'+json.dumps(data)+'\n```')
    with pytest.raises(ValueError): module.convert(packets, 'fixture:2')


@pytest.mark.parametrize('mutation', ['traversal','absolute','backslash','symlink','nested','case','duplicate','entries','expanded','two-roots','control','device'])
def test_archive_members_rejected_without_extraction(tmp_path, mutation):
    import io, stat, zipfile, warnings
    m = resource_fixture(tmp_path)
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
        name = 'toy/main.txt'
        if mutation == 'traversal': name = 'toy/../outside.txt'
        if mutation == 'absolute': name = '/toy/main.txt'
        if mutation == 'backslash': name = 'toy\\main.txt'
        if mutation == 'nested': name = 'toy/nested.zip'
        if mutation == 'control': name = 'toy/bad\nname.txt'
        if mutation in {'symlink','device'}:
            info = zipfile.ZipInfo(name)
            info.create_system = 3
            info.external_attr = ((stat.S_IFLNK if mutation == 'symlink' else stat.S_IFCHR) | 0o644) << 16
            archive.writestr(info, b'target')
        else:
            archive.writestr(name, b'x' * (32 * 1024 * 1024 + 1) if mutation == 'expanded' else b'input')
        if mutation == 'case': archive.writestr('toy/MAIN.txt', b'conflict')
        if mutation == 'duplicate':
            with warnings.catch_warnings():
                warnings.simplefilter('ignore');archive.writestr(name, b'conflict')
        if mutation == 'two-roots': archive.writestr('other/main.txt', b'input')
        if mutation == 'entries':
            for i in range(256): archive.writestr('toy/item-'+str(i)+'.txt', b'x')
    raw = stream.getvalue()
    record = m['resources'][0]
    record.update(path='static/treehouse/fixtures/input.zip', mime='application/zip', kind='download', bytes=len(raw), digest='sha256:'+hashlib.sha256(raw).hexdigest())
    (tmp_path / record['path']).write_bytes(raw)
    with pytest.raises(ValueError): validate_resources(m, tmp_path)
    # Metadata-only private portability does not read/unzip remote bytes.
    assert validate_resources(m, tmp_path, check_bytes=False)


def test_resource_converter_and_loader_share_integrity_checks(tmp_path, monkeypatch):
    import io, zipfile
    module = converter_module(); packets = author_packets(tmp_path)
    monkeypatch.setattr(module, 'ROOT', tmp_path)
    m = resource_fixture(tmp_path)
    resource = m['resources'][0]
    resource['unitIds'] = ['ai:models:toy']
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, 'w') as archive: archive.writestr('toy/README.md', 'Original source; paper fixture only.')
    raw = stream.getvalue()
    resource.update(path='static/treehouse/fixtures/input.zip', mime='application/zip', kind='download', bytes=len(raw), digest='sha256:'+hashlib.sha256(raw).hexdigest())
    (tmp_path / resource['path']).write_bytes(raw)
    manifest, files = module.convert(packets, 'fixture:2', resources=[resource])
    out = tmp_path / 'native';(out / 'subjects').mkdir(parents=True)
    for name, raw in files.items(): (out / name).write_bytes(raw)
    catalogue = GlobalCatalogue(out, project_root=tmp_path)
    repo = TreeHouseRepository(tmp_path / 'learning.sqlite3')
    g = GlobalLearning(catalogue, repo, 'alice', 'one')
    detail = g.unit_detail('ai:models', 'ai:models:toy')
    assert detail['resources'][0]['url'] == '/static/treehouse/fixtures/input.zip'
    assert g.unit_detail('games:design', 'games:design:toy')['resources'] == []
    assert g.subject_detail('ai:models')['workRequirements']['ai:models:assessment:toy']['requirementDigest']
    (tmp_path / resource['path']).write_bytes(b'tampered')
    with pytest.raises(ValueError): module.convert(packets, 'fixture:2', resources=[resource])
    with pytest.raises(TreeHouseError) as exc: GlobalCatalogue(out, project_root=tmp_path)
    assert exc.value.code == 'invalid_global_resource'


@pytest.mark.parametrize('bad', [None, {}, [None], [{'id': []}]])
def test_resource_bad_types_are_controlled(tmp_path, bad):
    with pytest.raises(ValueError): validate_resources({'resources':bad, 'units':{}}, tmp_path)


def test_fresh_draft_after_reset_and_pending_attempt_supersedes_old_draft(world):
    c, repo = world
    command(c, repo, 'ai:models', answer='Prior completed response')
    command(c, repo, 'ai:models', kind='progress.reset')
    command(c, repo, 'ai:models', kind='submission.draft', answer='New draft after reset')
    detail = GlobalLearning(c, repo, 'alice', 'two').unit_detail('ai:models', 'ai:models:identity')
    saved = detail['submissions']['ai:models:assessment:identity']
    assert saved['status'] == 'draft' and saved['resetGeneration'] == 1
    assert saved['draftRecoveryStatus'] == 'current' and saved['draftAnswer'] == 'New draft after reset'
    assert not detail['progress']['complete']
    draft = {'response':'Old unfinished attempt','outcome':{'status':'blocked','learnerConfirmed':False,'evidenceText':'No build'}}
    command(c, repo, 'games:design', kind='submission.draft', answer=draft)
    failed = {'response':'New failed attempt','outcome':{'status':'failed','learnerConfirmed':False,'evidenceText':'Actual failure detail'}}
    command(c, repo, 'games:design', answer=failed)
    detail = GlobalLearning(c, repo, 'alice', 'two').unit_detail('games:design', 'games:design:prototype-and-playtest')
    saved = next(iter(detail['submissions'].values()))
    assert saved['status'] == 'pending' and saved['answer'] == failed
    assert 'draftAnswer' not in saved and 'draftRequirementDigest' not in saved


def test_exact_moved_root_reset_membership_and_all_global_vector(tmp_path):
    from treehouse_global_fixtures import four_root_package
    c = GlobalCatalogue(four_root_package(tmp_path / 'package'))
    repo = TreeHouseRepository(tmp_path / 'learning.sqlite3')
    g = GlobalLearning(c, repo, 'alice', 'one')
    ai = g.root_detail('treehouse:ai')['subjects']
    oc = g.root_detail('treehouse:openclank')['subjects']
    assert len(ai) == 8 and len(oc) == 10 and len(c.manifest['subjects']) == 49
    def scopes(subjects):
        return [{'courseId':s['id'],'progressRevision':s['progressRevision'],'resetGeneration':s['resetGeneration']} for s in subjects]
    old_vector = scopes(ai + oc)
    base = {'type':'progress.reset','commandId':'old-vector','expectedRevision':None,
            'payload':{'sourceRef':g.source_ref(),'packageRevision':c.revision,'mainQuestId':'treehouse:ai','resetScopes':old_vector}}
    with pytest.raises(TreeHouseError) as exc: g.command(base)
    assert exc.value.code == 'invalid_reset_scope' and repo.get_global_progress('alice', SOURCE_ID) == {}
    stale = copy.deepcopy(base);stale['payload']['packageRevision'] = '2026-10-14.1'
    with pytest.raises(TreeHouseError) as exc: g.command(stale)
    assert exc.value.code == 'stale_curriculum'
    base['payload']['resetScopes'] = scopes(ai)
    g.command(base)
    rows = repo.get_global_progress('alice', SOURCE_ID)
    assert set(rows) == {s['id'] for s in ai} and all(r['resetGeneration'] == 1 for r in rows.values())
    g = GlobalLearning(c, repo, 'alice', 'two')
    base = {'type':'progress.reset','commandId':'oc-vector','expectedRevision':None,
            'payload':{'sourceRef':g.source_ref(),'packageRevision':c.revision,'mainQuestId':'treehouse:openclank','resetScopes':scopes(g.root_detail('treehouse:openclank')['subjects'])}}
    g.command(base)
    g = GlobalLearning(c, repo, 'alice', 'two')
    assert len(g.rows) == 18 and all(r['resetGeneration'] == 1 for r in g.rows.values())
    all_subjects = [subject for rid in c.manifest['mainQuests'] for subject in g.root_detail(rid)['subjects']]
    g.command({'type':'progress.reset','commandId':'all-vector','expectedRevision':None,
               'payload':{'sourceRef':g.source_ref(),'packageRevision':c.revision,'scope':'all-global','resetScopes':scopes(all_subjects)}})
    rows = repo.get_global_progress('alice', SOURCE_ID)
    assert len(rows) == 49 and all(rows[s['id']]['resetGeneration'] == 2 for s in ai + oc)
    assert all(r['resetGeneration'] == 1 for cid, r in rows.items() if cid.startswith(('cs:','games:')))
    assert repo.get_global_progress('bob', SOURCE_ID) == {}
