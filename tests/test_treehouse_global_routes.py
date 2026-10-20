import secrets
import uuid

from fastapi.testclient import TestClient

from scripts.treehouse_candidate import build_candidate
from treehouse_global_fixtures import four_root_package
from src.openclank.treehouse_global_catalogue import GlobalCatalogue


def test_normal_auth_system_export_private_fork_work_and_isolation(tmp_path, monkeypatch):
    catalogue = GlobalCatalogue(four_root_package(tmp_path / 'package'))
    uid = 'ai:models:identity'
    public_resource = {'id':'route-paper', 'path':'static/treehouse/fixtures/route-paper.txt', 'mime':'text/plain', 'bytes':0,
                       'digest':'sha256:' + __import__('hashlib').sha256(b'').hexdigest(), 'unitIds':[uid], 'license':'MIT',
                       'provenance':{'kind':'first-party-fixture'}, 'kind':'data'}
    # Source metadata fixture only: byte admission is covered independently.
    catalogue.resources['route-paper'] = public_resource
    catalogue.manifest.setdefault('resources', []).append(public_resource)
    catalogue.manifest['units'][uid].setdefault('resourceIds', []).append('route-paper')
    for definition in (catalogue._states['ai:models'], catalogue._projection_states['ai:models']):
        definition['activities'][uid].setdefault('resourceIds', []).append('route-paper')
    local_key = 'fixture:LOCAL'
    local_unit = catalogue.subject_metadata('ai:openclank-chat')['unitIds'][0]
    catalogue.manifest['sources'][local_key] = {'id':local_key,'kind':'first-party-inspection','title':'Fixture code provenance',
        'codePaths':['src/openclank/treehouse_global_catalogue.py'], 'candidateIdentity':{'sourceDigests':{'src/openclank/treehouse_global_catalogue.py':'sha256:'+__import__('hashlib').sha256(__import__('pathlib').Path('src/openclank/treehouse_global_catalogue.py').read_bytes()).hexdigest()}}}
    catalogue.manifest['units'][local_unit]['sourceKeys'].append(local_key)
    catalogue._states['ai:openclank-chat']['activities'][local_unit]['sourceKeys'].append(local_key)
    monkeypatch.setattr('src.openclank.treehouse_global_catalogue.load_global_catalogue', lambda: catalogue)
    app = build_candidate(tmp_path)
    alice = TestClient(app)
    bob = TestClient(app)
    password = secrets.token_urlsafe(24)
    assert alice.post('/api/auth/setup', json={'username':'fixturealice', 'password':password}).status_code == 200
    assert alice.post('/api/auth/login', json={'username':'fixturealice', 'password':password}).status_code == 200
    assert alice.post('/api/auth/users', json={'username':'fixturebob', 'password':password, 'is_admin':False}).status_code == 200
    assert bob.post('/api/auth/login', json={'username':'fixturebob', 'password':password}).status_code == 200
    prefix = '/api/copal/treehouse'
    initial = alice.get(prefix).json()
    assert len(initial['globalCatalogue']['mainQuests']) == 4
    assert initial['state']['courses'] == {}
    exported = alice.get(prefix + '/courses/ai:models/export', params={'source':'system', 'sourceId':'openclank:treehouse-core'})
    assert exported.status_code == 200, exported.text
    package = exported.json()
    assert 'sourceRef' not in package['course']
    metadata = package['learningMetadata']
    assert metadata['originMainQuest']['id'] == 'treehouse:ai'
    assert public_resource in metadata['resources']
    assert metadata['topicLinks'] and any(edge['external'] for edge in metadata['topicLinks'])
    referenced = {key for unit in package['activities'].values() for key in unit.get('sourceKeys', [])}
    assert set(metadata['sources']).issubset(referenced)
    import hashlib,json
    from urllib.parse import quote
    original_unit = next(iter(package['activities']))
    external_unit = next(edge['toId'] for edge in metadata['topicLinks'] if edge['external'])
    package['activities'][original_unit]['content'] += '\n[Own unit](#treehouse-unit=' + quote(original_unit,safe='') + ') [External concept](#treehouse-unit=' + quote(external_unit,safe='') + ')'
    unsigned = {k:v for k,v in package.items() if k not in {'packageId','sourceRevision'}}
    package['packageId'] = hashlib.sha256(json.dumps(unsigned,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()).hexdigest()
    assert alice.get(prefix + '/courses/ai:missing/export',params={'source':'system','sourceId':'openclank:treehouse-core'}).status_code == 404
    local_export = alice.get(prefix + '/courses/ai:openclank-chat/export',params={'source':'system','sourceId':'openclank:treehouse-core'})
    assert local_export.status_code == 200, local_export.text
    assert local_export.json()['learningMetadata']['originMainQuest']['id'] == 'treehouse:openclank'
    provenance = local_export.json()['learningMetadata']['sources'][local_key]
    assert provenance['codePaths'] and provenance['candidateIdentity']['sourceDigests']
    assert all(not p.startswith('/') and '.clanker' not in p for p in provenance['codePaths'])
    for invalid_resources in ({'not':'a list'}, [None]):
        malformed = __import__('copy').deepcopy(package)
        malformed['learningMetadata']['resources'] = invalid_resources
        unsigned_bad = {k:v for k,v in malformed.items() if k not in {'packageId','sourceRevision'}}
        malformed['packageId'] = hashlib.sha256(json.dumps(unsigned_bad,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()).hexdigest()
        denied = alice.post(prefix + '/courses/import', json=malformed)
        assert denied.status_code == 400 and denied.json()['detail']['code'] == 'invalid_topic_metadata'
        assert alice.get(prefix).json()['state']['courses'] == {}
    imported = alice.post(prefix + '/courses/import', json=package)
    assert imported.status_code == 200, imported.text
    receipt = imported.json()
    imported_unit = receipt['idMap']['activities'][original_unit]
    cid = receipt['courseId']
    assert cid.startswith('course:import-')
    assert alice.post(prefix + '/courses/import', json=package).json()['outcome'] == 'replayed'
    def command(kind, payload):
        snapshot = alice.get(prefix).json()
        result = alice.post(prefix + '/commands', json={'type':kind, 'commandId':str(uuid.uuid4()), 'expectedRevision':snapshot['state']['revision'], 'payload':payload})
        assert result.status_code == 200, result.text
        return result.json()
    command('course.open', {'courseId':cid})
    command('course.update', {'courseId':cid, 'title':'My private models practice'})
    command('enrollment.enroll', {'courseId':cid})
    snapshot = alice.get(prefix).json()
    imported_body = snapshot['state']['activities'][imported_unit]['content']
    assert '#treehouse-unit=' + quote(imported_unit,safe='') in imported_body
    assert '#treehouse-unit=' + quote(external_unit,safe='') in imported_body
    assert '#treehouse-unit=' + quote(original_unit,safe='') not in imported_body
    course = snapshot['state']['courses'][cid]
    imported_resource_unit = receipt['idMap']['activities'][uid]
    assert next(r for r in course['learningMetadata']['resources'] if r['id'] == 'route-paper')['unitIds'] == [imported_resource_unit]
    assert 'route-paper' in snapshot['state']['activities'][imported_resource_unit]['resourceIds']
    assert course['title'] == 'My private models practice'
    assert 'sourceRef' not in course and '_systemSource' not in course
    aid = course['completionCriteria']['assignmentIds'][0]
    assignment = snapshot['state']['assignments'][aid]
    unit = snapshot['state']['activities'][assignment['unitId']]
    assert aid in unit['assignmentIds'] and assignment['unitId'].startswith('activitie:import-') is False
    command('submission.submit', {'courseId':cid, 'assignmentId':aid, 'answer':'I distinguish the model from the harness using observed configuration.'})
    assert bob.get(prefix).json()['state']['courses'] == {}
    assert alice.get(prefix + '/courses/ai:models/summary', params={'source':'system','sourceId':'openclank:treehouse-core'}).json()['progress']['percent'] == 0
    assert bob.get(prefix, params={'courseId':cid}).status_code == 404
    bob_revision = bob.get(prefix).json()['state']['revision']
    assert bob.post(prefix + '/commands', json={'type':'course.update', 'commandId':str(uuid.uuid4()), 'expectedRevision':bob_revision, 'payload':{'courseId':cid,'title':'Forged edit'}}).status_code in (403,404)
    shared = command('course.share', {'courseId':cid,'recipientId':'fixturebob','capability':'learn'})['result']
    bob_snapshot = bob.get(prefix).json()
    accepted = bob.post(prefix + '/commands',json={'type':'course.accept_share','commandId':str(uuid.uuid4()),'expectedRevision':bob_snapshot['state']['revision'],'payload':{'shareToken':shared['shareToken']}})
    assert accepted.status_code == 200, accepted.text
    bob_snapshot = bob.get(prefix).json()
    assert cid in bob_snapshot['state']['courses']
    denied_edit = bob.post(prefix + '/commands',json={'type':'course.update','commandId':str(uuid.uuid4()),'expectedRevision':bob_snapshot['state']['revision'],'payload':{'courseId':cid,'title':'Recipient cannot edit'}})
    assert denied_edit.status_code in (403,404)
    enrolled = bob.post(prefix + '/commands',json={'type':'enrollment.enroll','commandId':str(uuid.uuid4()),'expectedRevision':bob_snapshot['state']['revision'],'payload':{'courseId':cid}})
    assert enrolled.status_code == 200, enrolled.text
    bob_snapshot = bob.get(prefix).json()
    bob_work = bob.post(prefix + '/commands',json={'type':'submission.submit','commandId':str(uuid.uuid4()),'expectedRevision':bob_snapshot['state']['revision'],'payload':{'courseId':cid,'assignmentId':aid,'answer':'A separate learner response'}})
    assert bob_work.status_code == 200, bob_work.text
    assert alice.get(prefix).json()['state']['courses'][cid]['title'] == 'My private models practice'
    # Offline explicit retirement on this disposable owner store. The old owner
    # link is typed, exports retained definitions, and grants remain revoked.
    from scripts.retire_treehouse_defaults import preview, apply
    from src.openclank.treehouse_field_guide import instantiate_field_guide
    repo = app.state.treehouse_repository
    owner = initial['accountId']
    state, revision = repo.get_catalogue(owner, 'default')
    state = instantiate_field_guide(state, owner)
    state['revision'] = revision + 1
    repo.put_catalogue(owner, 'default', state, expected_revision=revision)
    retired_id = next(k for k in state['courses'] if k != cid)
    plan = preview(repo.path, 'offline-retirement')
    apply(repo.path, plan, tmp_path / 'before-retirement.sqlite3')
    old_link = alice.get(prefix, params={'courseId':retired_id})
    assert old_link.status_code == 409 and old_link.json()['detail']['code'] == 'course_retired'
    assert alice.get(prefix + '/courses/' + retired_id + '/export').status_code == 200
    assert bob.get(prefix, params={'courseId':retired_id}).status_code == 404
