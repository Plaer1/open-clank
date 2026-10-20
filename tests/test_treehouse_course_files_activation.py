"""Stopped synthetic stores: exact native/evidence preservation and recovery fences."""
import copy
import json
from pathlib import Path
import secrets
import sqlite3
import subprocess
import sys

import pytest
from core.auth import AuthManager
from scripts.activate_treehouse_course_files import apply, preview, resume_preview, reindex, inventory, restore_native
from src.openclank.copal_treehouse import new_treehouse_state
from src.openclank.copal_treehouse_repository import TreeHouseRepository
from src.openclank.treehouse_course_files import PrivateCourseFiles
from src.openclank.treehouse_storage_activation import readonly, logical_digest, logical_snapshot

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def world(tmp_path):
    auth = tmp_path/'auth.json'; manager = AuthManager(str(auth))
    assert manager.setup('conversionowner',secrets.token_urlsafe(32))
    account = manager.account_id('conversionowner')
    db,loose,shared = tmp_path/'treehouse.sqlite3',tmp_path/'vaults',tmp_path/'shared'
    repo = TreeHouseRepository(db,course_files_root=loose)
    state = new_treehouse_state('Conversion owner')
    cid,mid,uid,aid='course:edited','module:one','activity:blue','assignment:one'
    state['courses'][cid]={'id':cid,'title':'Edited and retired private course','ownerId':account,'courseType':'quest',
        'status':'published','moduleIds':[mid],'completionCriteria':{'activityIds':[],'assignmentIds':[aid],'assignmentMode':'submitted'},'shareToken':'retained-opaque-share',
        'retiredAt':'historical-retirement','replacementMainQuestId':'treehouse:games'}
    state['modules'][mid]={'id':mid,'courseId':cid,'activityIds':[uid],'assignmentIds':[aid],'title':'One module','status':'published'}
    state['activities'][uid]={'id':uid,'courseId':cid,'moduleId':mid,'status':'published','content':'# Exact blue\n\nAlready edited.',
        'optionalSections':[{'title':'Extra','content':'Exact optional section.'}],
        'sourceAttachments':[{'id':'attachment-to-preserve','sourceRef':{'kind':'account'}}]}
    state['assignments'][aid]={'id':aid,'courseId':cid,'moduleId':mid,'status':'published','assessmentType':'text','graded':False,'prompt':'Name the color.',
        'successConditions':['Blue is named.'],'questions':[{'id':'q','prompt':'Which color?',
        'options':[{'id':'blue','text':'Blue'},{'id':'red','text':'Red'}],'correctOptionIds':['blue']}]}
    state['skills']['unscoped']={'id':'unscoped','title':'Independent old skill'}
    state['events']=[{'id':'old-evidence','type':'submission.saved'}]
    state['processedCommands']={'old-command':{'ok':True,'result':{'unchanged':True}}}
    state['extensions']={'learning':{'records':[{'id':'record:one','courseId':cid,'status':'valid'}]}}
    repo.put_catalogue(account,'default',state,expected_revision=0)
    with sqlite3.connect(db) as connection:
        connection.execute('INSERT INTO treehouse_progress VALUES(?,?,?,?,?,?,?,?)',('recipient',account,'default',cid,'{"answer":"old"}',4,2,1.0))
        connection.execute('INSERT INTO treehouse_idempotency VALUES(?,?,?,?,?,?)',(account,'default','command:old','digest','{"ok":true}',1.0))
        connection.execute('INSERT INTO treehouse_grants VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',('grant:old',account,'default',cid,'recipient','learn',2,'tokenhash','accepted',None,3,1.0,'group:old'))
        connection.execute('INSERT INTO treehouse_recipient_index VALUES(?,?,?,?,?,?,?)',('recipient','default','grant:old',account,cid,2,1))
    args=dict(db=db,auth=auth,loose_root=loose,bundle=ROOT/'src/openclank/treehouse_curriculum',shared_root=shared)
    return args,repo,state,account,tmp_path/'recovery'


def test_exact_definition_and_all_other_rows_preserved(world):
    args,repo,state,account,backup=world
    with readonly(args['db']) as c: before=logical_snapshot(c)
    plan=preview(**args)
    receipt=apply(args['db'],plan,backup,writers_stopped=True)
    assert receipt['privateNativeDefinitionsExact'] and receipt['nonCatalogueRowsExact']
    assert receipt['policyGrantsCreated']==0
    with readonly(args['db']) as c:
        after=logical_snapshot(c)
        raw=json.loads(c.execute('SELECT state_json FROM treehouse_catalogues').fetchone()[0])
    assert before[0]==after[0]
    assert {k:v for k,v in before[1:] if k!='treehouse_catalogues'}=={k:v for k,v in after[1:] if k!='treehouse_catalogues'}
    assert raw['_courseFilesEnabled'] is True
    assert 'content' not in raw['activities']['activity:blue']
    assert 'prompt' not in raw['assignments']['assignment:one']
    assert restore_native(raw,PrivateCourseFiles(args['loose_root']))==state
    current,_=repo.get_catalogue(account,'default')
    assert current['activities']['activity:blue']['sourceAttachments']==state['activities']['activity:blue']['sourceAttachments']
    assert current['courses']['course:edited']['shareToken']==state['courses']['course:edited']['shareToken']
    assert (args['shared_root']/'.authority.json').is_file()
    frozen=inventory(args['loose_root'])
    assert apply(args['db'],plan,backup,writers_stopped=True)['replayed'] is True
    assert inventory(args['loose_root'])==frozen


@pytest.mark.parametrize('phase',['staged','database','marker'])
def test_interrupted_boundary_manual_replay(world,phase):
    args,_,_,_,backup=world;plan=preview(**args)
    with pytest.raises(RuntimeError,match='Injected interruption'):
        apply(args['db'],plan,backup,writers_stopped=True,fail_after=phase)
    if phase!='marker': assert not (args['shared_root']/'.authority.json').exists()
    receipt=apply(args['db'],plan,backup,writers_stopped=True)
    assert receipt['privateNativeDefinitionsExact']
    assert (args['shared_root']/'.authority.json').is_file()


def test_stale_database_inventory_and_auth_refused_without_switch(world):
    args,_,_,_,backup=world;plan=preview(**args)
    args['loose_root'].mkdir();(args['loose_root']/'new.txt').write_text('A later external write')
    with pytest.raises(ValueError,match='inventory changed'):
        apply(args['db'],plan,backup,writers_stopped=True)
    assert not backup.exists()
    plan=preview(**args)
    with sqlite3.connect(args['db']) as c:c.execute("UPDATE treehouse_catalogues SET revision=revision+1")
    with pytest.raises(ValueError,match='changed'):
        apply(args['db'],plan,backup,writers_stopped=True)
    assert not (args['shared_root']/'.authority.json').exists()


def test_staged_edit_and_wrong_operation_refused(world):
    args,_,_,_,backup=world;plan=preview(**args)
    with pytest.raises(RuntimeError):apply(args['db'],plan,backup,writers_stopped=True,fail_after='staged')
    wrong={**plan,'operationId':'another-operation'}
    with pytest.raises(ValueError,match='another preview'):
        apply(args['db'],wrong,backup,writers_stopped=True)
    source=next(args['shared_root'].rglob('*.md'));source.write_text('Later authored content')
    with pytest.raises(ValueError,match='Staged files changed'):
        apply(args['db'],plan,backup,writers_stopped=True)
    assert source.read_text()=='Later authored content'
    assert not (args['shared_root']/'.authority.json').exists()


def test_no_confirmation_and_existing_backup_refused(world):
    args,_,_,_,backup=world;plan=preview(**args)
    with pytest.raises(ValueError,match='Stop writers'):apply(args['db'],plan,backup)
    backup.mkdir()
    with pytest.raises(ValueError,match='Backup destination exists'):
        apply(args['db'],plan,backup,writers_stopped=True)


def test_missing_real_auth_identity_refused(world):
    args,_,_,_,_=world
    with sqlite3.connect(args['db']) as c:c.execute("UPDATE treehouse_catalogues SET owner_account_id='unknown-immutable-account'")
    with pytest.raises(ValueError,match='immutable auth mapping'):preview(**args)


@pytest.mark.parametrize('where',['shared','private'])
def test_actual_mid_extraction_requires_explicit_resume_and_new_backup(world,monkeypatch,where):
    args,_,state,_,backup=world;plan=preview(**args)
    with readonly(args['db']) as c: before=logical_digest(c)
    if where=='shared':
        with pytest.raises(RuntimeError,match='during shared'):
            apply(args['db'],plan,backup,writers_stopped=True,fail_after='shared-file')
    else:
        native=PrivateCourseFiles._call
        def interrupt(self,owner,workspace,operation,**kwargs):
            result=native(self,owner,workspace,operation,**kwargs)
            if operation=='create': raise RuntimeError('Actual first native private create interrupted')
            return result
        with monkeypatch.context() as patch:
            patch.setattr(PrivateCourseFiles,'_call',interrupt)
            with pytest.raises(RuntimeError,match='Actual first native'):
                apply(args['db'],plan,backup,writers_stopped=True)
    with readonly(args['db']) as c: assert logical_digest(c)==before
    assert not (args['shared_root']/'.authority.json').exists()
    with pytest.raises(ValueError,match='Staged files changed'):
        apply(args['db'],plan,backup,writers_stopped=True)
    fresh=resume_preview(args['db'],plan,backup)
    assert fresh['resume']['priorBackup']==str(backup)
    second=backup.with_name('recovery-second')
    assert apply(args['db'],fresh,second,writers_stopped=True)['privateNativeDefinitionsExact']
    assert (backup/'treehouse.sqlite3').is_file() and (second/'treehouse.sqlite3').is_file()
    with readonly(args['db']) as c: raw=json.loads(c.execute('SELECT state_json FROM treehouse_catalogues').fetchone()[0])
    assert restore_native(raw,PrivateCourseFiles(args['loose_root']))==state


def test_expired_sessions_preview_and_apply_are_byte_preserving(world):
    args,_,_,_,backup=world
    sessions=args['auth'].with_name('sessions.json')
    expired=json.dumps({secrets.token_urlsafe(32):{'username':'conversionowner','expiry':1,'created':1}},indent=3).encode()
    sessions.write_bytes(expired);sessions.chmod(0o600)
    auth=args['auth'].read_bytes()
    plan=preview(**args)
    assert sessions.read_bytes()==expired and args['auth'].read_bytes()==auth
    apply(args['db'],plan,backup,writers_stopped=True)
    assert sessions.read_bytes()==expired and args['auth'].read_bytes()==auth
    assert (backup/'sessions.json').read_bytes()==expired
    assert (backup/'auth.json').read_bytes()==auth


def test_preview_never_creates_adjacent_sessions(world):
    args,_,_,_,_=world;sessions=args['auth'].with_name('sessions.json')
    assert not sessions.exists();preview(**args);assert not sessions.exists()


def test_real_cli_preview_apply_and_replay(world):
    args,_,_,_,backup=world;output=backup.with_name('preview.json');receipt=backup.with_name('receipt.json')
    command=[sys.executable,str(ROOT/'scripts/activate_treehouse_course_files.py')]
    flags=[part for key,value in args.items() for part in ('--'+key.replace('_','-'),str(value))]
    first=subprocess.run(command+['preview',*flags,'--output',str(output)],capture_output=True,text=True)
    assert first.returncode==0,first.stderr
    second=subprocess.run(command+['apply',*flags,'--preview',str(output),'--backup-dir',str(backup),'--writers-stopped','--output',str(receipt)],capture_output=True,text=True)
    assert second.returncode==0,second.stderr
    assert json.loads(receipt.read_text())['privateNativeDefinitionsExact']
    assert output.stat().st_mode&0o077==0 and receipt.stat().st_mode&0o077==0
    replay=subprocess.run(command+['apply',*flags,'--preview',str(output),'--backup-dir',str(backup),'--writers-stopped'],capture_output=True,text=True)
    assert replay.returncode==0,replay.stderr
    assert json.loads(replay.stdout)['replayed']


def test_invalid_active_marker_never_reports_replay_success(world):
    args,_,_,_,backup=world;plan=preview(**args);apply(args['db'],plan,backup,writers_stopped=True)
    marker=args['shared_root']/'.authority.json';data=json.loads(marker.read_text());data['format']='invalid'
    marker.write_text(json.dumps(data))
    with pytest.raises(ValueError,match='marker is invalid'):
        apply(args['db'],plan,backup,writers_stopped=True)
    assert json.loads(marker.read_text())['format']=='invalid'


def test_reindex_cli_delegates_without_learner_database_mutation(world):
    args,_,_,_,backup=world;plan=preview(**args);receipt=apply(args['db'],plan,backup,writers_stopped=True)
    with readonly(args['db']) as c: before=logical_digest(c)
    result=subprocess.run([sys.executable,str(ROOT/'scripts/activate_treehouse_course_files.py'),'reindex',
        '--shared-root',str(args['shared_root']),'--project-root',str(ROOT)],capture_output=True,text=True)
    assert result.returncode==0,result.stderr
    data=json.loads(result.stdout)
    assert data['generation']==receipt['sharedIndex']['generation'] and data['replayed']
    with readonly(args['db']) as c: assert logical_digest(c)==before


def test_reindex_refuses_fabricated_operator_identity(world,monkeypatch):
    args,_,_,_,backup=world;plan=preview(**args);apply(args['db'],plan,backup,writers_stopped=True)
    monkeypatch.setattr('scripts.activate_treehouse_course_files.os.geteuid',lambda:-1)
    with pytest.raises(ValueError,match='actual OS owner'):reindex(args['shared_root'])


def test_unmapped_zero_course_catalogue_row_is_byte_exact(world):
    args,_,_,_,backup=world
    orphan=new_treehouse_state('Unmapped empty legacy state')
    orphan['skills']['independent']={'id':'independent','title':'Unscoped retained skill'}
    orphan['events']=[{'id':'legacy-event','type':'old'}]
    raw=json.dumps(orphan,indent=3)+'\n'
    row=('unmapped-legacy-owner','legacy-workspace',raw,37,123.25)
    with sqlite3.connect(args['db']) as c:c.execute('INSERT INTO treehouse_catalogues VALUES(?,?,?,?,?)',row)
    plan=preview(**args);apply(args['db'],plan,backup,writers_stopped=True)
    with sqlite3.connect(args['db']) as c:
        after=c.execute("SELECT owner_account_id,workspace_id,state_json,revision,updated_at FROM treehouse_catalogues WHERE owner_account_id='unmapped-legacy-owner'").fetchone()
    assert after==row
    assert '_courseFilesEnabled' not in json.loads(after[2])
    assert apply(args['db'],plan,backup,writers_stopped=True)['replayed']


def test_mapped_empty_catalogue_enables_future_source_creation(world):
    args,repo,_,account,backup=world
    empty=new_treehouse_state('Mapped empty scope')
    repo.put_catalogue(account,'empty-scope',empty,expected_revision=0)
    plan=preview(**args);apply(args['db'],plan,backup,writers_stopped=True)
    with sqlite3.connect(args['db']) as c:
        raw=c.execute('SELECT state_json FROM treehouse_catalogues WHERE owner_account_id=? AND workspace_id=?',(account,'empty-scope')).fetchone()[0]
    value=json.loads(raw)
    assert value.pop('_courseFilesEnabled') is True
    assert value==empty
    assert repo.course_files_enabled(json.loads(raw))
