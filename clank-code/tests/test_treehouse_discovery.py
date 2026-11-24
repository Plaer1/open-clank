"""Shared discovery reads canonical learning without copying or earning work."""
import copy
import json
import sqlite3

import pytest
from fastapi.testclient import TestClient

from scripts.treehouse_candidate import build_candidate
from src.openclank.treehouse_global_catalogue import GlobalLearning, SOURCE_ID
from src.openclank.treehouse_storage_activation import readonly, logical_snapshot

PREFIX = '/api/copal/treehouse'


@pytest.fixture
def world(tmp_path):
    app = build_candidate(tmp_path)
    alice, bob = TestClient(app), TestClient(app)
    password = 'Disposable-discovery-test-password'
    assert alice.post('/api/auth/setup', json={'username':'discoveryalice','password':password}).status_code == 200
    assert alice.post('/api/auth/login', json={'username':'discoveryalice','password':password}).status_code == 200
    assert alice.post('/api/auth/users', json={'username':'discoverybob','password':password,'is_admin':False}).status_code == 200
    assert bob.post('/api/auth/login', json={'username':'discoverybob','password':password}).status_code == 200
    owner = alice.get(PREFIX).json()['accountId']
    bob.get(PREFIX)
    repo = app.state.treehouse_repository
    state, revision = repo.get_catalogue(owner, 'default')
    # A historical private ID may collide with a system ID. Do not merge maps.
    state['courses']['ai:models'] = {'id':'ai:models','title':'Private collision tutorial','description':'Private-only discovery marker',
        'ownerId':owner,'status':'draft','courseType':'tutorial','moduleIds':[]}
    state['courses']['private-quest'] = {'id':'private-quest','title':'Private quest','description':'Private quest marker',
        'ownerId':owner,'status':'draft','courseType':'quest','moduleIds':[]}
    state.setdefault('extensions', {})['learning'] = {'collections':{'my-list':{'id':'my-list','title':'Private collection','description':'Only my entries','courseIds':['ai:models']}}}
    state['revision'] = revision + 1
    repo.put_catalogue(owner, 'default', state, expected_revision=revision)
    return app, alice, bob, repo, owner


def library(client, **query):
    response = client.get(PREFIX + '/learning-library', params=query)
    assert response.status_code == 200, response.text
    return response.json()


def stored(repo):
    with readonly(repo.path) as db: return logical_snapshot(db)


def test_real_four_root_metadata_is_body_free_and_preserves_private_namespace(world, monkeypatch):
    app, alice, bob, repo, owner = world
    catalogue = app.state.treehouse_global_catalogue
    def body_forbidden(*args, **kwargs): raise AssertionError('Discovery loaded a lesson body')
    monkeypatch.setattr(catalogue, 'subject_state', body_forbidden)
    monkeypatch.setattr(catalogue, 'unit', body_forbidden)
    before = stored(repo)
    data = library(alice)
    shared = data['sharedCatalogue']
    assert data['accountId'] == owner and data['workspace'] == 'default'
    assert data['globalAvailability']['status'] == 'ready'
    assert shared['packageDigest'] == catalogue.manifest['packageDigest']
    assert {r['id'] for r in shared['mainQuests']} == {'treehouse:programming','treehouse:games','treehouse:ai','treehouse:openclank'}
    assert (len(shared['quests']), len(shared['lessons']), len(shared['topicLinks'])) == (49,291,735)
    assert any(c['id'] == 'ai:models' and c['title'] == 'Private collision tutorial' for c in data['courses'])
    quest = next(q for q in shared['quests'] if q['id'] == 'ai:models')
    assert quest['title'] != 'Private collision tutorial'
    assert quest['sourceRef'] == {'kind':'system','sourceId':SOURCE_ID,'courseId':'ai:models'}
    assert data['collections'][0]['courseIds'] == ['ai:models']
    assert data['credentials'] == []
    forbidden = {'content','body','answer','draftAnswer','submissions','profiles','events','evidenceEventIds','historicalCompletions','skills','badges'}
    def walk(value):
        if isinstance(value, dict):
            assert not (set(value) & forbidden)
            for child in value.values(): walk(child)
        elif isinstance(value, list):
            for child in value: walk(child)
    walk(shared)
    assert len(json.dumps(shared).encode()) < 400_000
    assert stored(repo) == before
    other = library(bob)
    assert other['courses'] == [] and other['collections'] == []
    assert 'Private collision tutorial' not in json.dumps(other)


def test_shared_search_index_remains_complete_under_private_query_and_type_filters(world):
    _, alice, _, _, _ = world
    unfiltered = library(alice)
    for query, course_type, expected_private in [('', 'tutorial', ['ai:models']), ('', 'quest', ['private-quest']),
                                                ('Private-only discovery marker', '', ['ai:models']), ('NO_MATCH_7914', 'tutorial', [])]:
        data = library(alice, q=query, courseType=course_type)
        assert [c['id'] for c in data['courses']] == expected_private
        assert data['sharedCatalogue'] == unfiltered['sharedCatalogue']
        assert data['sharedSearchScope'] == 'complete-title-outcome-metadata-index'
    lesson = next(u for u in unfiltered['sharedCatalogue']['lessons'] if u['id'] == 'games:production:project-route')
    assert lesson['readingOnly'] and lesson['assignmentIds'] == lesson['requiredAssignmentIds'] == []


def test_canonical_progress_resume_reset_and_account_workspace_isolation(world):
    app, alice, bob, repo, owner = world
    catalogue = app.state.treehouse_global_catalogue
    before = library(alice)['sharedCatalogue']
    cid, uid = 'ai:models', 'ai:models:identity'
    def command(kind, ident):
        g = GlobalLearning(catalogue, repo, owner, 'default')
        row = g.rows.get(cid, {'revision':0,'resetGeneration':0})
        payload = {'courseId':cid,'sourceRef':g.source_ref(cid),'packageRevision':catalogue.revision,'resetGeneration':row['resetGeneration']}
        if kind == 'course.open': payload['activityId'] = uid
        if kind == 'submission.submit':
            aid = catalogue.unit(cid,uid)['assignmentIds'][0]
            payload.update(assignmentId=aid,assignmentRequirementDigest=catalogue.requirements(aid)['requirementDigest'],answer='The architecture is the blueprint; the checkpoint holds weights; the website is a harness. Prompts can change its behavior.')
        return g.command({'type':kind,'commandId':ident,'expectedRevision':row['revision'],'payload':payload})
    command('course.open','resume')
    command('submission.submit','reasoned-response')
    current = library(alice)
    shared = current['sharedCatalogue']
    quest = next(q for q in shared['quests'] if q['id'] == cid)
    canonical = GlobalLearning(catalogue,repo,owner,'default').subject_summary(cid)
    assert quest['progressRevision'] == canonical['progressRevision']
    assert all(value == canonical['progress'][key] for key,value in quest['progress'].items())
    assert quest['progress']['completed'] == 1 and quest['progress']['resumeActivityId'] == uid
    assert shared['progressFingerprint'] != before['progressFingerprint']
    other_workspace = library(alice, workspace='another-workspace')
    assert other_workspace['sharedCatalogue'] == shared and other_workspace['courses'] == []
    other = next(q for q in library(bob)['sharedCatalogue']['quests'] if q['id'] == cid)
    assert other['progress']['completed'] == 0 and not other['progress']['resumeActivityId']
    stable = stored(repo)
    assert library(alice)['sharedCatalogue'] == shared and stored(repo) == stable
    command('progress.reset','reset')
    reset = library(alice)['sharedCatalogue']
    assert reset['progressFingerprint'] != shared['progressFingerprint']
    reset_quest = next(q for q in reset['quests'] if q['id'] == cid)
    assert reset_quest['resetGeneration'] == quest['resetGeneration'] + 1 and reset_quest['progress']['completed'] == 0


@pytest.mark.parametrize('mode', ['missing','incompatible'])
def test_unavailable_storage_is_explicit_and_private_still_accessible(world, mode):
    _, alice, bob, repo, _ = world
    with sqlite3.connect(repo.path) as db:
        if mode == 'missing': db.execute('DROP TABLE treehouse_global_progress')
        else: db.execute('ALTER TABLE treehouse_global_progress RENAME COLUMN revision TO invalid_revision')
    before = stored(repo)
    data = library(alice)
    assert data['globalAvailability']['status'] == ('activation-required' if mode == 'missing' else 'incompatible')
    assert data['sharedCatalogue'] is None
    assert {c['id'] for c in data['courses']} == {'ai:models','private-quest'}
    assert library(bob)['courses'] == []
    assert stored(repo) == before


def test_auth_denial_and_unexpected_adapter_failure_are_not_hidden(world, monkeypatch):
    app, alice, _, _, _ = world
    assert TestClient(app).get(PREFIX+'/learning-library').status_code == 401
    assert alice.get(PREFIX+'/learning-library',params={'courseType':'system'}).status_code == 400
    monkeypatch.setattr(GlobalLearning,'discovery',lambda self: (_ for _ in ()).throw(RuntimeError('unexpected discovery failure')))
    with pytest.raises(RuntimeError,match='unexpected discovery failure'): library(alice)


def test_private_completion_record_remains_eligible_scoped_and_independent_of_discussion(world):
    _, alice, bob, _, _ = world
    sequence = 0
    def command(kind, payload):
        nonlocal sequence
        sequence += 1
        state = alice.get(PREFIX).json()
        response = alice.post(PREFIX+'/commands',json={'type':kind,'commandId':'records-fixture-'+str(sequence),
            'expectedRevision':state['state']['revision'],'payload':payload})
        assert response.status_code == 200, response.text
        return response.json()['result']
    cid = command('course.create',{'title':'Completion record tutorial','courseType':'tutorial'})['courseId']
    mid = command('module.create',{'courseId':cid,'title':'One private lesson'})['moduleId']
    uid = command('activity.create',{'moduleId':mid,'title':'Read this supplied private input',
        'activityType':'markdown','content':'A private Tutorial requires content traversal, not assessed mastery.','status':'published'})['activityId']
    preview_path = PREFIX+'/courses/'+cid+'/learner-preview'
    preview = alice.get(preview_path).json()
    assert alice.post(preview_path,json={'visible':True,'revision':preview['preview']['revision'],'accountId':preview['accountId']}).status_code == 200
    command('course.publish',{'courseId':cid})
    path = PREFIX+'/courses/'+cid+'/learning'
    def issue(client, ident, **payload):
        view = alice.get(path).json()
        return client.post(path,json={'type':'credential.issue','commandId':ident,'expectedRevision':view['revision'],'payload':payload})
    ineligible = issue(alice,'ineligible-record')
    assert ineligible.status_code == 409 and ineligible.json()['detail']['code'] == 'completion_required'
    assert issue(bob,'forbidden-record').status_code == 404
    command('enrollment.enroll',{'courseId':cid})
    command('course.open',{'courseId':cid,'activityId':uid})
    command('activity.complete',{'courseId':cid,'activityId':uid})
    eligible = alice.get(path).json()
    assert eligible['completion']['eligible'] and eligible['posts'] == []
    issued = issue(alice,'eligible-record')
    assert issued.status_code == 200, issued.text
    record_id = issued.json()['credentialId']
    export_path = PREFIX+'/courses/'+cid+'/credentials/'+record_id+'/export'
    exported = alice.get(export_path,params={'format':'json'})
    assert exported.status_code == 200 and exported.json()['status'] == 'valid'
    assert not exported.json()['reviewedAssessment']
    assert bob.get(export_path,params={'format':'json'}).status_code == 404
    assert any(r['id'] == record_id for r in library(alice)['credentials'])
    current = alice.get(path).json()
    revoked = alice.post(path,json={'type':'credential.revoke','commandId':'revoke-private-record',
        'expectedRevision':current['revision'],'payload':{'credentialId':record_id,'reason':'Disposable record lifecycle qualification'}})
    assert revoked.status_code == 200, revoked.text
    assert alice.get(export_path,params={'format':'json'}).json()['status'] == 'revoked'
