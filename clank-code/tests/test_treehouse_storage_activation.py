import copy
import json
import sqlite3
import subprocess
import sys

import pytest

from src.openclank.copal_treehouse_repository import TreeHouseRepository, TreeHouseRepositoryError
from src.openclank.treehouse_storage_activation import preview, apply, verify, restore, readonly, logical_snapshot


def legacy_store(tmp_path):
    """Remove globals BEFORE the constructor under test; never auto-upgrade fixture."""
    path = tmp_path / 'legacy.sqlite3'
    TreeHouseRepository(path)
    with sqlite3.connect(path) as db:
        for name in ('progress', 'idempotency', 'reset_generations', 'completions'):
            db.execute('DROP TABLE treehouse_global_' + name)
        db.execute('CREATE TABLE old_evidence(id TEXT PRIMARY KEY,body TEXT NOT NULL)')
        db.execute("INSERT INTO old_evidence VALUES('old','preserve exact edited evidence')")
    return path


def snapshot(path):
    with readonly(path) as db: return logical_snapshot(db)


def activate(path, tmp_path, op='upgrade'):
    return apply(path, preview(path, op), tmp_path / (op + '.backup'), writers_stopped=True)


def test_true_legacy_admission_activation_replay_restore(tmp_path):
    path = legacy_store(tmp_path)
    before = snapshot(path)
    repo = TreeHouseRepository(path)
    assert snapshot(path) == before
    assert repo.global_storage_status()['status'] == 'activation-required'
    with pytest.raises(TreeHouseRepositoryError) as failure: repo.get_global_progress('alice', 'system')
    assert failure.value.code == 'global_progress_activation_required'
    plan = preview(path, 'activation')
    assert snapshot(path) == before and len(plan['availability']['missingTables']) == 4
    receipt = apply(path, plan, tmp_path/'backup', writers_stopped=True)
    assert receipt['createdTables'] == plan['availability']['missingTables']
    assert (tmp_path/'backup').stat().st_mode & 0o777 == 0o600
    assert repo.get_global_progress('alice', 'system') == {}
    after = snapshot(path)
    assert all(item in after[1:] for item in before[1:])
    assert verify(path)['logicalDigest'] == receipt['outputDigest']
    replay = apply(path, plan, tmp_path/'never-created', writers_stopped=True)
    assert replay == {**receipt, 'replayed': True} and not (tmp_path/'never-created').exists()
    ready = activate(path, tmp_path, 'already-ready')
    assert ready['changed'] is False and snapshot(path) == after
    assert restore(path, tmp_path/'backup', receipt, writers_stopped=True)['restored']
    assert snapshot(path) == before


def test_partial_preserves_existing_global_rows_and_legacy_rows(tmp_path):
    path = legacy_store(tmp_path)
    from src.openclank.copal_treehouse_repository import GLOBAL_SCHEMA
    with sqlite3.connect(path) as db:
        db.execute(GLOBAL_SCHEMA.split(';')[0])
        db.execute('INSERT INTO treehouse_global_progress VALUES(?,?,?,?,?,?,?)', ('alice','source','subject','{"events":[]}',7,2,123.5))
    before = snapshot(path)
    receipt = activate(path, tmp_path)
    assert len(receipt['createdTables']) == 3
    assert all(item in snapshot(path)[1:] for item in before[1:])
    assert TreeHouseRepository(path).get_global_progress('alice','source')['subject']['revision'] == 7


@pytest.mark.parametrize('definition', [
    'learner_account_id INTEGER NOT NULL, source_id TEXT NOT NULL, course_id TEXT NOT NULL, state_json TEXT NOT NULL, revision INTEGER NOT NULL, reset_epoch INTEGER NOT NULL, updated_at REAL NOT NULL, PRIMARY KEY(learner_account_id,source_id,course_id)',
    'learner_account_id TEXT NOT NULL, source_id TEXT NOT NULL, course_id TEXT NOT NULL, state_json TEXT NOT NULL, revision INTEGER NOT NULL, reset_epoch INTEGER NOT NULL, updated_at REAL NOT NULL',
    'learner_account_id TEXT, source_id TEXT NOT NULL, course_id TEXT NOT NULL, state_json TEXT NOT NULL, revision INTEGER NOT NULL, reset_epoch INTEGER NOT NULL, updated_at REAL NOT NULL, PRIMARY KEY(learner_account_id,source_id,course_id)',
    'learner_account_id TEXT NOT NULL UNIQUE, source_id TEXT NOT NULL, course_id TEXT NOT NULL, state_json TEXT NOT NULL, revision INTEGER NOT NULL, reset_epoch INTEGER NOT NULL, updated_at REAL NOT NULL, PRIMARY KEY(learner_account_id,source_id,course_id)',
    'learner_account_id TEXT COLLATE NOCASE NOT NULL, source_id TEXT NOT NULL, course_id TEXT NOT NULL, state_json TEXT NOT NULL, revision INTEGER NOT NULL, reset_epoch INTEGER NOT NULL, updated_at REAL NOT NULL, PRIMARY KEY(learner_account_id,source_id,course_id)',
    'learner_account_id TEXT NOT NULL, source_id TEXT NOT NULL, course_id TEXT NOT NULL, state_json TEXT NOT NULL, revision INTEGER NOT NULL, reset_epoch INTEGER NOT NULL, updated_at REAL NOT NULL, extra TEXT GENERATED ALWAYS AS (course_id) VIRTUAL, PRIMARY KEY(learner_account_id,source_id,course_id)',
    'learner_account_id TEXT NOT NULL, source_id TEXT NOT NULL, course_id TEXT NOT NULL, state_json TEXT NOT NULL, revision INTEGER NOT NULL, reset_epoch INTEGER NOT NULL, updated_at REAL NOT NULL, PRIMARY KEY(learner_account_id DESC,source_id,course_id)',
])
def test_incompatible_column_type_nullability_or_pk_refuses_before_backup(tmp_path, definition):
    path = legacy_store(tmp_path)
    with sqlite3.connect(path) as db: db.execute('CREATE TABLE treehouse_global_progress ('+definition+')')
    before = snapshot(path)
    assert TreeHouseRepository(path).global_storage_status()['status'] == 'incompatible'
    with pytest.raises(ValueError, match='Incompatible'): activate(path,tmp_path)
    assert snapshot(path) == before and not (tmp_path/'upgrade.backup').exists()
    with pytest.raises(TreeHouseRepositoryError) as failure: TreeHouseRepository(path).get_global_progress('alice','source')
    assert failure.value.code == 'global_progress_schema_incompatible'


def test_preview_change_race_backup_conflict_failure_and_newer_restore_refusal(tmp_path):
    path = legacy_store(tmp_path)
    plan = preview(path,'one'); before = snapshot(path)
    with pytest.raises(ValueError, match='writers'): apply(path,plan,tmp_path/'none')
    changed = copy.deepcopy(plan);changed['availability']['missingTables'] = []
    with pytest.raises(ValueError, match='changed'): apply(path,changed,tmp_path/'none',writers_stopped=True)
    occupied = tmp_path/'occupied';occupied.write_text('keep')
    with pytest.raises(FileExistsError): apply(path,plan,occupied,writers_stopped=True)
    with pytest.raises(RuntimeError): apply(path,plan,tmp_path/'failure',writers_stopped=True,inject_failure=True)
    assert snapshot(path) == before
    with sqlite3.connect(path) as db: db.execute("UPDATE old_evidence SET body='new' WHERE id='old'")
    with pytest.raises(ValueError,match='changed'): apply(path,plan,tmp_path/'none',writers_stopped=True)
    receipt = activate(path,tmp_path)
    with sqlite3.connect(path) as db: db.execute("UPDATE old_evidence SET body='newer' WHERE id='old'")
    newer = snapshot(path)
    with pytest.raises(ValueError,match='Newer'): restore(path,tmp_path/'upgrade.backup',receipt,writers_stopped=True)
    assert snapshot(path) == newer


def test_write_between_backup_and_transaction_refuses_without_schema_changes(tmp_path, monkeypatch):
    path=legacy_store(tmp_path);plan=preview(path,'race');connect=sqlite3.connect
    def racing_connect(target,*args,**kwargs):
        if target==path and kwargs.get('isolation_level','default') is None:
            with connect(path) as writer:writer.execute("UPDATE old_evidence SET body='concurrent' WHERE id='old'")
        return connect(target,*args,**kwargs)
    monkeypatch.setattr(sqlite3,'connect',racing_connect)
    with pytest.raises(ValueError,match='changed after backup'):apply(path,plan,tmp_path/'backup',writers_stopped=True)
    assert verify(path)['availability']['status']=='activation-required'
    with readonly(path) as db: assert db.execute("SELECT body FROM old_evidence").fetchone()[0]=='concurrent'


def test_cli_private_outputs_and_no_overwrite_before_mutation(tmp_path):
    path=legacy_store(tmp_path);script='scripts/activate_treehouse_storage.py'
    out=tmp_path/'preview.json'
    run=lambda *args: subprocess.run([sys.executable,script,*args],capture_output=True,text=True)
    assert run('preview','--db',str(path),'--output',str(out)).returncode == 0
    assert out.stat().st_mode & 0o777 == 0o600
    before=snapshot(path)
    assert run('apply','--db',str(path),'--preview',str(out),'--backup',str(tmp_path/'backup'),'--writers-stopped','--output',str(out)).returncode != 0
    assert snapshot(path)==before


def test_normal_auth_private_survives_missing_and_incompatible_then_four_roots(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from scripts.treehouse_candidate import build_candidate
    app=build_candidate(tmp_path)
    client=TestClient(app);password='Synthetic-test-only-strong-password'
    assert client.post('/api/auth/setup',json={'username':'oldowner','password':password}).status_code==200
    assert client.post('/api/auth/login',json={'username':'oldowner','password':password}).status_code==200
    prefix='/api/copal/treehouse'
    initial=client.get(prefix).json();repo=app.state.treehouse_repository
    cmd={'type':'course.create','commandId':'private-old','expectedRevision':initial['state']['revision'],'payload':{'id':'private-old-course','title':'Edited old course'}}
    assert client.post(prefix+'/commands',json=cmd).status_code==200
    with sqlite3.connect(repo.path) as db:
        for name in ('progress','idempotency','reset_generations','completions'): db.execute('DROP TABLE treehouse_global_'+name)
    app.state.treehouse_repository=TreeHouseRepository(repo.path)
    before=snapshot(repo.path)
    response=client.get(prefix,params={'courseId':'private-old-course'})
    assert response.status_code==200,response.text
    data=response.json();assert data['globalCatalogue'] is None and data['globalAvailability']['status']=='activation-required'
    assert 'private-old-course' in data['state']['courses']
    assert client.post('/api/auth/users',json={'username':'otherlearner','password':password,'is_admin':False}).status_code==200
    other=TestClient(app);assert other.post('/api/auth/login',json={'username':'otherlearner','password':password}).status_code==200
    assert other.get(prefix,params={'courseId':'private-old-course'}).status_code==404
    # This account creates its own empty native catalogue; snapshot the revised baseline.
    before=snapshot(repo.path)
    denied=client.post(prefix+'/commands',json={'type':'course.open','commandId':'global-old','expectedRevision':0,'payload':{'courseId':'ai:models','sourceRef':{'kind':'system','sourceId':'openclank:treehouse-core'},'packageRevision':'2026-10-14.2','resetGeneration':0}})
    assert denied.status_code==503 and denied.json()['detail']['code']=='global_progress_activation_required'
    assert snapshot(repo.path)==before
    receipt=activate(repo.path,tmp_path)
    result=client.get(prefix).json()
    assert result['globalAvailability']['status']=='ready' and len(result['globalCatalogue']['mainQuests'])==4
    assert 'private-old-course' in result['state']['courses']
    with sqlite3.connect(repo.path) as db:db.execute('ALTER TABLE treehouse_global_progress RENAME COLUMN revision TO invalid_revision')
    incompatible=client.get(prefix).json()
    assert incompatible['globalAvailability']['status']=='incompatible' and incompatible['globalCatalogue'] is None
    assert 'private-old-course' in incompatible['state']['courses']
    with sqlite3.connect(repo.path) as db:db.execute('ALTER TABLE treehouse_global_progress RENAME COLUMN invalid_revision TO revision')
    # Only known storage unavailability may degrade; arbitrary catalogue errors escape.
    monkeypatch.setattr('src.openclank.treehouse_global_catalogue.GlobalLearning.summary',lambda self: (_ for _ in ()).throw(RuntimeError('unexpected fault')))
    with pytest.raises(RuntimeError,match='unexpected fault'):client.get(prefix)
