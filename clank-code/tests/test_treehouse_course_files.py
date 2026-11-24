"""Course files are active source text; discovery/progress never hydrate bodies."""
import copy
import importlib.util
import json
from pathlib import Path

import pytest

from src.openclank.copal_treehouse import TreeHouseError
from src.openclank.treehouse_course_files import (
    canonical_bytes, content_head, split_course_definition, hydrate_activity,
    hydrate_assignment, source_path,
)
from src.openclank.treehouse_global_catalogue import GlobalCatalogue, canonical_digest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def files_package(tmp_path):
    original = GlobalCatalogue(ROOT / 'src/openclank/treehouse_curriculum')
    manifest = copy.deepcopy(original.manifest)
    manifest['contentFormat'] = 'course-files-v1'
    for cid, meta in manifest['subjects'].items():
        definition = {key: original.subject_state(cid)[key] for key in ('courses','modules','activities','assignments','skills','badges','quests')}
        metadata, files = split_course_definition(definition)
        raw = canonical_bytes(metadata)
        files[meta['definitionPath']] = raw
        meta['definitionDigest'] = content_head(raw)
        for name, data in files.items():
            target = tmp_path / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
    manifest['packageDigest'] = canonical_digest({k:v for k,v in manifest.items() if k != 'packageDigest'})
    (tmp_path / 'manifest.json').write_bytes(canonical_bytes(manifest))
    return tmp_path, original


def test_full_native_definitions_hydrate_exactly(files_package):
    path, original = files_package
    catalogue = GlobalCatalogue(path, project_root=ROOT)
    for cid in original.manifest['subjects']:
        hydrated, before = catalogue.subject_state(cid), original.subject_state(cid)
        for kind in ('courses','modules','activities','assignments','skills','badges','quests'):
            assert hydrated[kind] == before[kind]
        assert catalogue.projection_state(cid)['courses'][cid]['requirementsDigest'] == original.projection_state(cid)['courses'][cid]['requirementsDigest']


def test_cold_metadata_and_progress_are_body_free(files_package, monkeypatch):
    path, _ = files_package
    import src.openclank.treehouse_course_files as source
    reads = []
    original = source.read_content
    def read(*args, **kwargs):
        reads.append(args[1]['path'])
        return original(*args, **kwargs)
    monkeypatch.setattr(source, 'read_content', read)
    original_stat = Path.stat
    stats = []
    def stat(target, *args, **kwargs):
        if target.is_relative_to(path / 'courses'):
            stats.append(str(target))
        return original_stat(target, *args, **kwargs)
    monkeypatch.setattr(Path, 'stat', stat)
    catalogue = GlobalCatalogue(path, project_root=ROOT)
    for cid in catalogue.manifest['subjects']:
        catalogue.projection_state(cid)
        catalogue.subject_metadata(cid)
    from src.openclank.treehouse_global_catalogue import GlobalLearning
    class ReadOnlyProgress:
        def get_global_progress(self, *args): return {}
        def global_completion_history(self, *args): return []
    learning = GlobalLearning(catalogue, ReadOnlyProgress(), 'learner-a', 'workspace-a')
    assert len(learning.discovery()['lessons']) == 291
    assert len(learning.summary()['mainQuests']) == 4
    for cid in catalogue.manifest['subjects']:
        learning.subject_detail(cid)
    assert reads == []
    assert stats == []
    unit_id, unit = next(iter(catalogue.manifest['units'].items()))
    value = catalogue.unit(unit['courseId'], unit_id)
    assert value['content']
    expected = 1 + len(value.get('optionalSections', []))
    assert len(reads) == expected
    assert all('/lessons/' in p for p in reads)
    for aid in value['assignmentIds']:
        assert catalogue.requirements(aid)['prompt']
    assert len(reads) == expected + len(value['assignmentIds'])


def test_bad_work_text_is_localized_not_an_uncontrolled_error(files_package):
    path, _ = files_package
    catalogue = GlobalCatalogue(path, project_root=ROOT)
    cid, state = next(iter(catalogue._states.items()))
    aid, assignment = next(iter(state['assignments'].items()))
    target = path / assignment['workFile']['path']
    before = target.read_bytes()
    target.write_text('{"prompt":42,"successConditions":[]}')
    with pytest.raises(TreeHouseError) as exc:
        catalogue.requirements(aid)
    assert exc.value.code == 'course_content_unavailable'
    assert catalogue.projection_state(cid)['assignments'][aid]['requirementDigest'] == assignment['requirementDigest']
    target.write_bytes(before)
    assert catalogue.requirements(aid)['prompt']


def test_external_body_edit_and_localized_failure(files_package):
    path, _ = files_package
    catalogue = GlobalCatalogue(path, project_root=ROOT)
    uid, meta = next(iter(catalogue.manifest['units'].items()))
    cid = meta['courseId']
    reference = catalogue._states[cid]['activities'][uid]['contentFile']
    target = path / reference['path']
    before = target.read_bytes()
    target.write_bytes(before + b'\nSource edit.\n')
    assert catalogue.unit(cid, uid)['content'].endswith('Source edit.\n')
    assert catalogue.content_source(cid, uid)['contentHead'] != reference['originDigest']
    assert catalogue._states[cid]['courses'][cid]['requirementsDigest']
    target.unlink()
    with pytest.raises(TreeHouseError) as exc:
        catalogue.unit(cid, uid)
    assert exc.value.code == 'course_content_unavailable'
    other_id, other = next((key,value) for key,value in catalogue.manifest['units'].items() if key != uid)
    assert catalogue.unit(other['courseId'], other_id)['content']
    # Startup does not read missing bodies or overwrite them from the bundle.
    reopened = GlobalCatalogue(path, project_root=ROOT)
    assert reopened.subject_metadata(cid)
    assert not target.exists()
    target.write_bytes(before)
    assert reopened.unit(cid, uid)['content'].encode() == before


@pytest.mark.parametrize('name', ['../x.md','/x.md','x\\y.md','x/../y.md','a//b.md','x.json'])
def test_reference_boundaries(tmp_path, name):
    with pytest.raises(ValueError):
        source_path(tmp_path, {'path':name,'originDigest':'sha256:'+'0'*64}, suffix='.md')


def test_symlink_and_duplicate_authority_rejected(tmp_path):
    outside = tmp_path.parent / (tmp_path.name + '-outside.md')
    outside.write_text('private')
    (tmp_path / 'lesson.md').symlink_to(outside)
    with pytest.raises(ValueError, match='Symlink'):
        source_path(tmp_path, {'path':'lesson.md','originDigest':'sha256:'+'0'*64}, suffix='.md')


def test_converter_externalization_is_representation_only(files_package):
    path, original = files_package
    spec = importlib.util.spec_from_file_location('course_file_converter', ROOT / 'scripts/build_treehouse_curriculum.py')
    converter = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(converter)
    manifest = copy.deepcopy(original.manifest)
    files = {meta['definitionPath']: canonical_bytes({kind:original.subject_state(cid)[kind] for kind in ('courses','modules','activities','assignments','skills','badges','quests')}) for cid,meta in manifest['subjects'].items()}
    converter.externalize_course_files(manifest, files)
    assert manifest['contentFormat'] == 'course-files-v1'
    for cid, meta in manifest['subjects'].items():
        definition = json.loads(files[meta['definitionPath']])
        assert all('content' not in unit for unit in definition['activities'].values())
        assert all('prompt' not in work for work in definition['assignments'].values())
        assert meta['requirementsDigest'] == original.manifest['subjects'][cid]['requirementsDigest']
    assert len(files) > 49 + 291
    assert any('/ai-agents-' in name for name in files)


def test_private_loose_install_replay_selected_read_and_save(tmp_path):
    from src.openclank.treehouse_course_files import PrivateCourseFiles
    store = PrivateCourseFiles(tmp_path/'vaults')
    definition = {kind:{} for kind in ('courses','modules','activities','assignments','skills','badges','quests')}
    definition['courses']['course:own'] = {'id':'course:own', 'ownerId':'account-a','moduleIds':['module:own']}
    definition['modules']['module:own'] = {'id':'module:own','courseId':'course:own','activityIds':['activity:own'],'assignmentIds':[]}
    definition['activities']['activity:own'] = {'id':'activity:own', 'courseId':'course:own','moduleId':'module:own', 'content':'Original\n', 'optionalSections':[]}
    pointer, metadata = store.install(definition, owner='alice', account_id='account-a', workspace='one', action_id='activate')
    replay_pointer, replay_metadata = store.install(definition, owner='alice', account_id='account-a', workspace='one', action_id='activate')
    assert replay_pointer == pointer and replay_metadata == metadata
    assert store.metadata(pointer, account_id='account-a', workspace='one') == metadata
    for account, workspace in [('account-b','one'),('account-a','two')]:
        with pytest.raises(ValueError, match='identity'):
            store.metadata(pointer, account_id=account, workspace=workspace)
    item = metadata['activities']['activity:own']
    activity, source = store.selected_activity(pointer, item)
    assert activity == definition['activities']['activity:own']
    outcome = store.save_text(pointer, item['contentFile']['path'], 'Edited\n', base=source['contentHead'], action_id='save-a', actor_id='account-a')
    assert outcome['outcome'] == 'committed'
    assert store.selected_activity(pointer, item)[0]['content'] == 'Edited\n'
    stale = store.save_text(pointer, item['contentFile']['path'], 'Overwrite', base=source['contentHead'], action_id='save-b', actor_id='account-a')
    assert stale['outcome'] == 'stale'
    assert store.save_text(pointer, item['contentFile']['path'], 'Edited\n', base=source['contentHead'], action_id='save-a', actor_id='account-a')['outcome'] == 'committed'
    with pytest.raises(ValueError, match='differs'):
        store.install(definition, owner='alice', account_id='account-a', workspace='one', action_id='activate')


def test_source_backed_private_authoring_round_trip_and_stale_heads(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from scripts.treehouse_candidate import build_candidate
    app = build_candidate(tmp_path/'runtime')
    client = TestClient(app)
    auth = {'username':'fileauthor','password':'Disposable-course-source-password'}
    assert client.post('/api/auth/setup',json=auth).status_code == 200
    assert client.post('/api/auth/login',json=auth).status_code == 200
    prefix = '/api/copal/treehouse'
    snapshot = client.get(prefix).json()
    account = snapshot['accountId']
    repo = app.state.treehouse_repository
    state, revision = repo.get_catalogue(account,'default')
    state['_courseFilesEnabled'] = True
    repo.put_catalogue(account,'default',state,expected_revision=revision)
    n = 0
    def command(kind,payload,status=200):
        nonlocal n
        n += 1
        snap = client.get(prefix).json()
        cid = payload.get('courseId')
        if cid and cid in snap['state']['courses']:
            payload.setdefault('metadataBase',snap['state']['courses'][cid].get('sourceMetadataHead'))
        response = client.post(prefix+'/commands',json={'type':kind,'commandId':f'file-case-{n}',
            'expectedRevision':snap['state']['revision'],'payload':payload})
        assert response.status_code == status, response.text
        return response.json()
    cid = command('course.create',{'id':'course:file-own','title':'File-backed Tutorial','courseType':'tutorial'})['result']['courseId']
    mid = command('module.create',{'courseId':cid,'title':'Read one file'})['result']['moduleId']
    uid = command('activity.create',{'courseId':cid,'moduleId':mid,'title':'Supplied lesson','content':'Original exact body\n','status':'published'})['result']['activityId']
    detail_url = prefix+f'/courses/{cid}/activities/{uid}'
    detail = client.get(detail_url).json()
    assert detail['activity']['content'] == 'Original exact body'
    assert detail['permissions']['editText'] and not detail['permissions']['editSource']
    snap = client.get(prefix).json()
    assert snap['state']['activities'][uid]['contentDeferred']
    assert 'content' not in snap['state']['activities'][uid]
    assert 'contentFile' not in snap['state']['activities'][uid]
    assert '_courseFileSources' not in snap['state']
    saved = command('activity.update',{'courseId':cid,'activityId':uid,'content':'Saved exact body\n',
        'contentBase':detail['contentSource']['contentHead'],'metadataBase':detail['sourceMetadataHead']})
    assert saved['result']['contentWrite']['receipt']['outcome'] == 'committed'
    assert client.get(detail_url).json()['activity']['content'] == 'Saved exact body'
    stale = command('activity.update',{'courseId':cid,'activityId':uid,'content':'Old tab overwrite',
        'contentBase':detail['contentSource']['contentHead'],'metadataBase':detail['sourceMetadataHead']},409)
    assert stale['detail']['code'] in {'source_metadata_conflict','content_conflict'}
    exported = client.get(prefix+f'/courses/{cid}/export')
    assert exported.status_code == 200, exported.text
    assert 'Saved exact body' in exported.text
    assert 'contentFile' not in exported.text and '_courseFileSources' not in exported.text
    imported=client.post(prefix+'/courses/import',json=exported.json())
    assert imported.status_code==200,imported.text
    imported_id=imported.json()['courseId']; imported_uid=imported.json()['idMap']['activities'][uid]
    assert imported_id!=cid
    assert client.get(prefix+f'/courses/{imported_id}/activities/{imported_uid}').json()['activity']['content']=='Saved exact body'
    imported_state,_=repo.get_catalogue(account,'default')
    assert imported_id in imported_state['_courseFileSources'] and 'content' not in imported_state['activities'][imported_uid]
    replay=client.post(prefix+'/courses/import',json=exported.json())
    assert replay.status_code==200 and replay.json()['outcome']=='replayed'
    # A published course editor keeps its bounded authoring grant, without
    # receiving a generic handle into the owner's Files namespace.
    preview_path = prefix+f'/courses/{cid}/learner-preview'
    preview = client.get(preview_path).json()
    assert client.post(preview_path,json={'visible':True,'revision':preview['preview']['revision'],'accountId':account}).status_code == 200
    command('course.publish',{'courseId':cid})
    assert client.post('/api/auth/users',json={'username':'fileeditor','password':auth['password'],'is_admin':False}).status_code == 200
    other = TestClient(app)
    assert other.post('/api/auth/login',json={'username':'fileeditor','password':auth['password']}).status_code == 200
    other_id = other.get(prefix).json()['accountId']
    share = command('course.share',{'courseId':cid,'recipientId':other_id,'capability':'edit'})['result']
    accepted = other.post(prefix+'/commands',json={'type':'course.accept_share','commandId':'accept-file-edit','payload':{'shareToken':share['shareToken']}})
    assert accepted.status_code == 200, accepted.text
    recipient_detail = other.get(detail_url).json()
    assert recipient_detail['permissions']['editText'] and not recipient_detail['permissions']['editSource']
    assert other.post(detail_url+'/source-open',json={}).status_code == 403
    edited = other.post(prefix+'/commands',json={'type':'activity.update','commandId':'recipient-file-edit',
        'expectedRevision':other.get(prefix).json()['state']['revision'],
        'payload':{'courseId':cid,'activityId':uid,'content':'Recipient scoped edit',
            'metadataBase':recipient_detail['sourceMetadataHead'],'contentBase':recipient_detail['contentSource']['contentHead']}})
    assert edited.status_code == 200, edited.text
    assert edited.json()['result']['contentWrite']['receipt']['outcome'] == 'committed'
    assert client.get(detail_url).json()['activity']['content'] == 'Recipient scoped edit'
    # Edit recipients retain draft author visibility; learn recipients never
    # receive unpublished rows or quiz answer keys through selected hydration.
    draft_id=command('activity.create',{'courseId':cid,'moduleId':mid,'title':'Private draft','content':'Draft only','status':'draft'})['result']['activityId']
    assert draft_id in other.get(prefix).json()['state']['activities']
    command('course.update',{'courseId':cid,'completionCriteria':{'activityIds':[uid],'assignmentIds':[],'assignmentMode':'submitted'}})
    quiz_id=command('assignment.create',{'courseId':cid,'moduleId':mid,'title':'Private quiz','prompt':'Choose one','assessmentType':'quiz','maxPoints':10,
        'questions':[{'id':'q1','prompt':'Choose four','options':[{'id':'a','text':'4'},{'id':'b','text':'5'}],'correctOptionIds':['a'],'points':10}]})['result']['assignmentId']
    command('assignment.publish',{'courseId':cid,'assignmentId':quiz_id})
    assert client.post('/api/auth/users',json={'username':'filelearner','password':auth['password'],'is_admin':False}).status_code==200
    learner=TestClient(app); assert learner.post('/api/auth/login',json={'username':'filelearner','password':auth['password']}).status_code==200
    learner_id=learner.get(prefix).json()['accountId']
    learner_share=command('course.share',{'courseId':cid,'recipientId':learner_id,'capability':'learn'})['result']
    assert learner.post(prefix+'/commands',json={'type':'course.accept_share','commandId':'accept-source-learn','payload':{'shareToken':learner_share['shareToken']}}).status_code==200
    assert draft_id not in learner.get(prefix).json()['state']['activities']
    current,_=repo.get_catalogue(account,'default')
    repo.course_source_store().metadata(current['_courseFileSources'][cid],account_id=account,workspace='default')
    quiz_detail=learner.get(prefix+f'/courses/{cid}/assignments/{quiz_id}/detail')
    assert quiz_detail.status_code==200,quiz_detail.text
    assert 'correctOptionIds' not in quiz_detail.json()['assignment']['questions'][0]
    assert client.get(prefix+f'/courses/{cid}/assignments/{quiz_id}/detail').json()['assignment']['questions'][0]['correctOptionIds']==['a']
    # Native authoring honors the same document readOnly flag as Files.
    stored, _ = repo.get_catalogue(account,'default')
    pointer = stored['_courseFileSources'][cid]
    store = repo.course_source_store()
    doc = store.document(pointer,stored['activities'][uid]['contentFile']['path'])
    manifest = store.loose._load(store.vault(pointer))
    manifest['documents'][doc['id']]['readOnly'] = True
    store.loose._save(store.vault(pointer),manifest)
    readonly = client.get(detail_url).json()
    assert not readonly['permissions']['editText']
    denied = command('activity.update',{'courseId':cid,'activityId':uid,'content':'Forbidden readOnly edit',
        'metadataBase':readonly['sourceMetadataHead'],'contentBase':readonly['contentSource']['contentHead']},409)
    assert denied['detail']['code'] == 'source_read_only'
    assert client.get(detail_url).json()['activity']['content'] == 'Recipient scoped edit'
    # Separate repository connections serialize revocation after an already
    # authorized source commit; a later recipient write is refused.
    import threading
    from src.openclank.copal_treehouse_repository import TreeHouseRepository
    from src.openclank.treehouse_course_files import PrivateCourseFiles
    manifest['documents'][doc['id']]['readOnly']=False; store.loose._save(store.vault(pointer),manifest)
    second_repo=TreeHouseRepository(repo.path)
    entered=threading.Event(); done=threading.Event(); failures=[]
    grant=repo.grant(share['grantId'])
    def revoke():
        entered.set()
        try:
            second_repo.revoke_share(owner_account_id=account,workspace_id='default',grant_id=share['grantId'],expected_revision=grant['revision'],now='2026-10-16T00:00:00Z')
        except BaseException as exc: failures.append(exc)
        finally: done.set()
    worker=threading.Thread(target=revoke)
    native_save=PrivateCourseFiles.save_text
    def interleave(self,*args,**kwargs):
        if kwargs.get('actor_id')==other_id and not entered.is_set():
            worker.start(); assert entered.wait(1); assert not done.wait(.05)
        return native_save(self,*args,**kwargs)
    monkeypatch.setattr(PrivateCourseFiles,'save_text',interleave)
    displayed=other.get(detail_url).json()
    serialized=other.post(prefix+'/commands',json={'type':'activity.update','commandId':'serialized-recipient-save',
        'expectedRevision':other.get(prefix).json()['state']['courses'][cid]['curriculumRevision'],
        'payload':{'courseId':cid,'activityId':uid,'content':'Authorized before revocation',
            'metadataBase':displayed['sourceMetadataHead'],'contentBase':displayed['contentSource']['contentHead']}})
    assert serialized.status_code==200,serialized.text
    worker.join(3); assert done.is_set() and not failures
    assert client.get(detail_url).json()['activity']['content']=='Authorized before revocation'
    denied=other.post(prefix+'/commands',json={'type':'activity.update','commandId':'after-revocation',
        'payload':{'courseId':cid,'activityId':uid,'content':'Forbidden after revocation',
            'metadataBase':displayed['sourceMetadataHead'],'contentBase':displayed['contentSource']['contentHead']}})
    assert denied.status_code in {403,404},denied.text
    assert client.get(detail_url).json()['activity']['content']=='Authorized before revocation'


def test_active_generation_cache_dirty_child_and_work_fences(tmp_path, monkeypatch):
    from test_treehouse_global import small_catalogue
    from scripts.build_treehouse_curriculum import externalize_course_files
    from src.openclank.treehouse_shared_course_index import reindex_shared_course_files
    from src.openclank.treehouse_global_catalogue import GlobalLearning
    from src.openclank.copal_treehouse_repository import TreeHouseRepository
    native = small_catalogue(tmp_path/'native')
    files = {meta['definitionPath']:(native.path/meta['definitionPath']).read_bytes() for meta in native.manifest['subjects'].values()}
    manifest=copy.deepcopy(native.manifest)
    externalize_course_files(manifest,files)
    manifest['packageDigest']=canonical_digest({k:v for k,v in manifest.items() if k!='packageDigest'})
    source=tmp_path/'shared'; source.mkdir()
    for name,raw in files.items():
        target=source/name; target.parent.mkdir(parents=True,exist_ok=True); target.write_bytes(raw)
    (source/'manifest.json').write_bytes(canonical_bytes(manifest))
    first=reindex_shared_course_files(source,project_root=ROOT)
    catalogue=GlobalCatalogue(source,project_root=ROOT,manifest_path=first['manifestPath']); catalogue.active_root=True
    repo=TreeHouseRepository(tmp_path/'personal.sqlite3')
    def send(cid,aid=None,kind='submission.submit',key=None,work_base=None):
        learning=GlobalLearning(catalogue,repo,'alice','one')
        row=learning.rows.get(cid,{'revision':0,'resetGeneration':0})
        payload={'courseId':cid,'sourceRef':learning.source_ref(cid),'packageRevision':catalogue.revision,'resetGeneration':row['resetGeneration']}
        if aid:
            assignment,selected=catalogue.assignment_with_source(aid)
            payload.update(assignmentId=aid,answer='Reasoned response',assignmentRequirementDigest=assignment['requirementDigest'],workBase=work_base or selected['contentHead'])
        return learning.command({'type':kind,'commandId':key or f"{cid}:{row['revision']}",'expectedRevision':row['revision'],'payload':payload})
    for cid in ('cs:values','cs:control'):
        for aid in catalogue._states[cid]['assignments']:
            complete=send(cid,aid)
    assert next(root for root in complete['mainQuestProgress'] if root['id']=='treehouse:programming')['complete']
    before=repo.global_completion_history('alice','openclank:treehouse-core','treehouse:programming')
    assert len(before)==1
    dirty_id='cs:values'
    descriptor=catalogue.manifest['sourceIndex']['subjects'][dirty_id]
    path=source/descriptor['path']; raw=path.read_bytes()
    value=json.loads(raw); value['courses'][dirty_id]['title']='Externally changed metadata'
    path.write_bytes(canonical_bytes(value))
    opened=send('cs:control',kind='course.open')
    assert not next(root for root in opened['mainQuestProgress'] if root['id']=='treehouse:programming')['complete']
    assert repo.global_completion_history('alice','openclank:treehouse-core','treehouse:programming')==before
    with pytest.raises(TreeHouseError) as pending:
        catalogue.unit(dirty_id,catalogue.subject_metadata(dirty_id)['unitIds'][0])
    assert pending.value.code=='source_index_pending'
    # A new completed generation is discoverable without body loading.
    old_stamp=catalogue.completed_generation_stamp()
    second=reindex_shared_course_files(source,project_root=ROOT)
    assert second['generation']==first['generation']+1 and catalogue.completed_generation_stamp()!=old_stamp
    refreshed=GlobalCatalogue(source,project_root=ROOT,manifest_path=second['manifestPath']); refreshed.active_root=True
    assert refreshed.subject_metadata(dirty_id)['title']=='Externally changed metadata'
    catalogue=refreshed
    aid=next(iter(catalogue._states['cs:control']['assignments']))
    assignment,selected=catalogue.assignment_with_source(aid)
    work=source/catalogue._states['cs:control']['assignments'][aid]['workFile']['path']
    work_value=json.loads(work.read_bytes()); work_value['prompt']='Updated wording, same requirement revision'
    work.write_bytes(canonical_bytes(work_value))
    with pytest.raises(TreeHouseError) as stale:
        send('cs:control',aid,key='stale-work',work_base=selected['contentHead'])
    assert stale.value.code=='stale_work_source'
    current=send('cs:control',aid,key='current-work')
    state=repo.get_global_progress('alice','openclank:treehouse-core')['cs:control']['state']
    attempt=next(iter(state['submissions'].values()))
    assert attempt['workHead']==content_head(work.read_bytes())
    assert state['events'][-1]['data'].get('workHead') or any(event['data'].get('workHead')==attempt['workHead'] for event in state['events'])
    assert len(repo.global_completion_history('alice','openclank:treehouse-core','treehouse:programming'))==1
    from scripts.treehouse_candidate import build_candidate
    from fastapi.testclient import TestClient
    import src.openclank.treehouse_shared_course_index as index_module
    app=build_candidate(tmp_path/'runtime'); client=TestClient(app)
    auth={'username':'generationreader','password':'Disposable-course-source-password'}
    assert client.post('/api/auth/setup',json=auth).status_code==200
    assert client.post('/api/auth/login',json=auth).status_code==200
    catalogue._generation_stamp=catalogue.completed_generation_stamp()
    app.state.treehouse_global_catalogue=catalogue
    calls=[]; latest=index_module.latest_shared_index
    def counted(root): calls.append(root); return latest(root)
    monkeypatch.setattr(index_module,'latest_shared_index',counted)
    assert client.get('/api/copal/treehouse').status_code==200
    assert client.get('/api/copal/treehouse').status_code==200 and calls==[]
    value=json.loads(path.read_bytes()); value['courses'][dirty_id]['title']='New completed generation'
    path.write_bytes(canonical_bytes(value)); reindex_shared_course_files(source,project_root=ROOT)
    calls.clear()
    assert client.get('/api/copal/treehouse').status_code==200
    assert app.state.treehouse_global_catalogue.subject_metadata(dirty_id)['title']=='New completed generation'
    assert client.get('/api/copal/treehouse').status_code==200 and len(calls)==1


def test_private_work_external_change_final_commit_fence_and_quiz_keys(tmp_path):
    from src.openclank.copal_treehouse import new_treehouse_state, apply_treehouse_command
    from src.openclank.copal_treehouse_repository import TreeHouseRepository
    from src.openclank.treehouse_course_files import install_private_definition, hydrate_private_command, assert_private_evidence_current
    repo=TreeHouseRepository(tmp_path/'db.sqlite3')
    state=new_treehouse_state('alice'); state['profiles']['alice']={**state['profiles'].pop('owner'),'id':'alice'}
    def run(kind,payload):
        nonlocal state
        state,result,_=apply_treehouse_command(state,{'type':kind,'payload':payload},actor_id='alice',command_id=str(state['revision']))
        return result
    cid=run('course.create',{'id':'course:quiz','title':'Quiz'})['courseId']
    mid=run('module.create',{'courseId':cid,'title':'Questions'})['moduleId']
    aid=run('assignment.create',{'moduleId':mid,'title':'Choose','prompt':'Choose one','assessmentType':'quiz','maxPoints':10,
        'questions':[{'id':'q1','prompt':'Two plus two','options':[{'id':'a','text':'4'},{'id':'b','text':'5'}],'correctOptionIds':['a'],'points':10}]})['assignmentId']
    # Private native quizzes have no global successConditions requirement.
    install_private_definition(repo,state,cid,owner='alice',account_id='alice',workspace='one',action_id='quiz-convert')
    repo.create_catalogue_if_absent('alice','one',state)
    selected,_=repo.get_catalogue('alice','one'); pointer=selected['_courseFileSources'][cid]
    assignment,source=repo.course_source_store().selected_assignment_with_source(pointer,selected['assignments'][aid])
    assert assignment['questions'][0]['correctOptionIds']==['a'] and 'successConditions' not in assignment
    payload={'assignmentId':aid,'workBase':source['contentHead']}
    store=repo.course_source_store()
    name=selected['assignments'][aid]['workFile']['path']
    store.contained(pointer,name)
    work_path=store.vault(pointer)/name
    hydrate_private_command(repo,selected,payload,'submission.draft')
    work=json.loads(work_path.read_bytes()); work['questions'][0]['correctOptionIds']=['b']; work_path.write_bytes(canonical_bytes(work))
    with repo.achievement_transaction(), pytest.raises(TreeHouseError) as stale:
        assert_private_evidence_current(repo,selected,'alice','one')
    assert stale.value.code=='stale_work_source'
    assert repo.get_progress('alice','alice','one',cid)[0] is None
    current,_=repo.get_catalogue('alice','one')
    _,fresh=store.selected_assignment_with_source(pointer,current['assignments'][aid])
    hydrate_private_command(repo,current,{'assignmentId':aid,'workBase':fresh['contentHead']},'submission.draft')
    work_path.unlink()
    with repo.achievement_transaction(),pytest.raises(TreeHouseError) as missing:
        assert_private_evidence_current(repo,current,'alice','one')
    assert missing.value.code=='course_content_unavailable'


def test_new_course_native_commit_reports_pending_index(tmp_path,monkeypatch):
    from fastapi.testclient import TestClient
    from scripts.treehouse_candidate import build_candidate
    from src.openclank.copal_treehouse_repository import TreeHouseRepositoryError
    app=build_candidate(tmp_path/'runtime'); client=TestClient(app)
    auth={'username':'pendingauthor','password':'Disposable-course-source-password'}
    assert client.post('/api/auth/setup',json=auth).status_code==200
    assert client.post('/api/auth/login',json=auth).status_code==200
    prefix='/api/copal/treehouse'; account=client.get(prefix).json()['accountId']; repo=app.state.treehouse_repository
    state,revision=repo.get_catalogue(account,'default'); state['_courseFilesEnabled']=True; repo.put_catalogue(account,'default',state,expected_revision=revision)
    original=repo.put_catalogue
    def fail_index(owner,workspace,state,**kwargs):
        if 'course:pending-create' in state.get('courses',{}):
            raise TreeHouseRepositoryError('injected_index_failure','Injected after source commit')
        return original(owner,workspace,state,**kwargs)
    monkeypatch.setattr(repo,'put_catalogue',fail_index)
    response=client.post(prefix+'/commands',json={'type':'course.create','commandId':'pending-source-create',
        'expectedRevision':client.get(prefix).json()['state']['revision'],'payload':{'id':'course:pending-create','title':'Preserved native source'}})
    assert response.status_code==200,response.text
    write=response.json()['result']['contentWrite']
    assert write['outcome']=='committed-index-pending' and write['sourceReceipts']
    assert 'course:pending-create' not in repo.get_catalogue(account,'default')[0]['courses']


def test_shared_selected_detail_files_capabilities_are_request_scoped(files_package,tmp_path):
    from fastapi.testclient import TestClient
    from scripts.treehouse_candidate import build_candidate
    from src.auth_helpers import require_user
    from src.openclank.files_facade import FilesFacadeError
    source,_=files_package
    app=build_candidate(tmp_path/'runtime'); author=TestClient(app)
    password='Disposable-course-source-password'
    assert author.post('/api/auth/setup',json={'username':'sourcewriter','password':password}).status_code==200
    assert author.post('/api/auth/login',json={'username':'sourcewriter','password':password}).status_code==200
    assert author.post('/api/auth/users',json={'username':'sourcereader','password':password,'is_admin':False}).status_code==200
    reader=TestClient(app); assert reader.post('/api/auth/login',json={'username':'sourcereader','password':password}).status_code==200
    catalogue=GlobalCatalogue(source,project_root=ROOT); catalogue.active_root=True
    app.state.treehouse_global_catalogue=catalogue
    cid='ai:models'; uid=catalogue.subject_metadata(cid)['unitIds'][0]
    url=f'/api/copal/treehouse/courses/{cid}/activities/{uid}?source=system&sourceId=openclank:treehouse-core'
    initial=author.get(url)
    assert initial.status_code==200 and initial.json()['unit']['content']
    assert initial.json()['contentSource']['canEditInFiles'] is False
    calls=[]; capabilities={'sourcewriter':['open','read','write'],'sourcereader':['open','read']}
    class Files:
        async def host_resource_for_path(self,ctx,*,path):
            calls.append((ctx['username'],ctx['workspace'],path))
            if capabilities[ctx['username']] is None:
                raise FilesFacadeError('No source grant',code='access_denied')
            return {'resource_ref':'opaque-fixture-'+ctx['username'],'capabilities':capabilities[ctx['username']]}
    app.state.treehouse_files_context=lambda request,workspace: {'username':require_user(request),'workspace':workspace}
    app.state.treehouse_files_facade=lambda request,copal_workspace: Files()
    writable=author.get(url).json()
    readonly=reader.get(url+'&workspace=other').json()
    assert writable['contentSource']['canOpenInFiles'] and writable['contentSource']['canEditInFiles']
    assert readonly['contentSource']['canOpenInFiles'] and not readonly['contentSource']['canEditInFiles']
    assert 'resource_ref' not in writable['contentSource'] and 'path' not in writable['contentSource']
    assert 'canEditInFiles' not in catalogue._states[cid]['activities'][uid]
    request={'sourceRef':{'kind':'system','sourceId':'openclank:treehouse-core','courseId':cid}}
    source_url=f'/api/copal/treehouse/courses/{cid}/activities/{uid}/source-open'
    assert author.post(source_url,json=request).json()['canEdit'] is True
    assert reader.post(source_url,json=request).json()['canEdit'] is False
    capabilities['sourcewriter']=['open','read']
    assert author.get(url).json()['contentSource']['canEditInFiles'] is False
    capabilities['sourcereader']=None
    denied=reader.get(url)
    assert denied.status_code==200 and denied.json()['unit']['content']
    assert denied.json()['contentSource']['canOpenInFiles'] is False
    assert reader.post(source_url,json=request).status_code==403
    expected=source_path(source,catalogue._states[cid]['activities'][uid]['contentFile'],suffix='.md')
    assert calls and {Path(call[2]) for call in calls}=={expected}
    assert any(user=='sourcereader' and workspace=='other' for user,workspace,_ in calls)


def test_course_install_preserves_unrelated_loose_manifest_and_normal_refresh(tmp_path):
    from src.openclank.copal_loose import LooseCopalRepository
    from src.openclank.treehouse_course_files import PrivateCourseFiles
    from src.openclank.official_docs import OFFICIAL_DOCS_SEED_VERSION
    root=tmp_path/'vaults'; normal=LooseCopalRepository(root)
    args={'owner':'conversion-owner','workspace_id':'one'}
    # Real ordinary provisioning creates the maintained reference records.
    normal._call_sync('status',args)
    note=normal._call_sync('create',{**args,'name':'Unrelated.md','kind':'markdown','content':'Original unrelated content','action_id':'original-note'})['doc']
    vault=normal._vault(args['owner'],args['workspace_id'])
    manifest=normal._load(vault)
    officials=[]
    for ident,record in manifest['documents'].items():
        if record.get('officialRef'):
            officials.append(ident)
            record.update(head='sha256:'+'0'*64,officialDigest='old-exact-digest',officialVersion=OFFICIAL_DOCS_SEED_VERSION-1,updatedAt=1.0)
    assert officials
    normal._save(vault,manifest)
    # Also exercise unrelated external-file mtime/head bookkeeping, which a
    # conversion must not perform just because it uses the native bridge.
    note_path=vault/manifest['documents'][note['id']]['path']
    note_path.write_text('Already externally edited before conversion')
    before=copy.deepcopy(normal._load(vault))
    content_before={str(path.relative_to(vault)):path.read_bytes() for path in vault.rglob('*') if path.is_file() and '.copal' not in path.relative_to(vault).parts}
    store=PrivateCourseFiles(root)
    definition={kind:{} for kind in ('courses','modules','activities','assignments','skills','badges','quests')}
    cid='course:preserve-other-records'; mid='module:preserve'; uid='activity:preserve'
    definition['courses'][cid]={'id':cid,'ownerId':'account-a','moduleIds':[mid]}
    definition['modules'][mid]={'id':mid,'courseId':cid,'activityIds':[uid],'assignmentIds':[]}
    definition['activities'][uid]={'id':uid,'courseId':cid,'moduleId':mid,'content':'Exact migrated course body'}
    pointer,metadata,receipt=store.install(definition,owner=args['owner'],account_id='account-a',workspace='one',action_id='preserving-install',with_receipts=True)
    assert receipt['outcome']=='committed' and receipt['sourceReceipts']
    assert store.metadata(pointer,account_id='account-a',workspace='one')==metadata
    assert store.selected_activity(pointer,metadata['activities'][uid])[0]['content']=='Exact migrated course body'
    after=normal._load(vault)
    assert {key:after['documents'][key] for key in before['documents']}==before['documents']
    assert {key:value for key,value in after.items() if key not in {'documents','operations'}}=={key:value for key,value in before.items() if key not in {'documents','operations'}}
    assert after['operations'][:len(before['operations'])]==before['operations']
    additions=set(after['documents'])-set(before['documents'])
    assert additions and all(after['documents'][key]['name'].startswith('TreeHouse/Courses/') for key in additions)
    assert all((vault/name).read_bytes()==raw for name,raw in content_before.items())
    # Normal adapters retain automatic Help and ordinary external refresh.
    # A caller-supplied bridge argument cannot select the trusted scope.
    normal._call_sync('get',{**args,'id':note['id'],'refresh_prefix':'TreeHouse/Courses/'})
    refreshed=normal._load(vault)
    assert all(refreshed['documents'][key]['officialVersion']==OFFICIAL_DOCS_SEED_VERSION for key in officials)
    assert refreshed['documents'][note['id']]['head']!=before['documents'][note['id']]['head']
