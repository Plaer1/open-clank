import copy
import sqlite3

import pytest

from scripts.retire_treehouse_defaults import preview, apply, restore, logical_digest, readonly
from src.openclank.copal_treehouse import new_treehouse_state
from src.openclank.copal_treehouse_repository import TreeHouseRepository
from src.openclank.treehouse_field_guide import instantiate_field_guide


def seeded(tmp_path):
    path = tmp_path / 'legacy.sqlite3'
    repo = TreeHouseRepository(path)
    state = instantiate_field_guide(new_treehouse_state('owner-account'), 'owner-account')
    ids = list(state['courses'])
    state['activities'][next(k for k,v in state['activities'].items() if v['courseId'] == ids[0])]['content'] += '\nLearner edit retained.'
    state['courses']['custom-course'] = {'id': 'custom-course', 'title': state['courses'][ids[1]]['title'], 'ownerId': 'owner-account', 'status': 'draft', 'moduleIds': []}
    repo.put_catalogue('owner-account', 'one', state, expected_revision=0)
    grant = repo.create_share(owner_account_id='owner-account', workspace_id='one', course_id=ids[1], recipient_account_id='learner-account', role='learn', access_revision=state['revision'], now='now', command_id='grant', payload={'courseId':ids[1]})
    return path, repo, ids, grant


def test_preview_recoverable_retirement_replay_and_restore(tmp_path):
    path, repo, ids, grant = seeded(tmp_path)
    with readonly(path) as db: before = logical_digest(db)
    plan = preview(path, 'operation')
    with readonly(path) as db: assert logical_digest(db) == before
    assert plan['counts']['edited'] == 1 and plan['counts']['pristine'] == 4 and plan['counts']['custom'] == 1
    receipt = apply(path, plan, tmp_path / 'backup.sqlite3')
    assert (tmp_path / 'backup.sqlite3').stat().st_mode & 0o777 == 0o600
    assert set(receipt['retiredCourseIds']) == set(ids)
    archived, _ = repo.get_catalogue('owner-account', 'one')
    assert archived['courses']['custom-course']['status'] == 'draft'
    assert 'Learner edit retained.' in next(v['content'] for v in archived['activities'].values() if v['courseId'] == ids[0])
    assert repo.grant(grant['grantId'])['revoked_at']
    replay = apply(path, plan, tmp_path / 'should-not-create.sqlite3')
    assert replay['replayed'] and replay['outputDigest'] == receipt['outputDigest']
    assert not (tmp_path / 'should-not-create.sqlite3').exists()
    assert restore(path, tmp_path / 'backup.sqlite3', receipt)['restored']
    with readonly(path) as db: assert logical_digest(db) == before


def test_races_tampered_preview_and_transaction_failure_preserve_data(tmp_path):
    path, repo, _, _ = seeded(tmp_path)
    plan = preview(path, 'failure')
    with pytest.raises(RuntimeError): apply(path, plan, tmp_path / 'backup.sqlite3', inject_failure=True)
    with readonly(path) as db: assert logical_digest(db) == plan['inputDigest']
    edited = copy.deepcopy(plan); edited['courses'][0]['classification'] = 'custom'
    with pytest.raises(ValueError, match='Preview was modified'): apply(path, edited, tmp_path / 'other.sqlite3')
    state, revision = repo.get_catalogue('owner-account', 'one')
    state['revision'] += 1
    repo.put_catalogue('owner-account', 'one', state, expected_revision=revision)
    with pytest.raises(ValueError, match='Database changed'): apply(path, plan, tmp_path / 'stale.sqlite3')


def test_unknown_version_schema_only_race_and_newer_restore_refusal(tmp_path):
    path, repo, ids, _ = seeded(tmp_path)
    state, revision = repo.get_catalogue('owner-account', 'one')
    state['fieldGuide']['templateVersion'] = 'unknown-version'
    state['revision'] += 1
    repo.put_catalogue('owner-account', 'one', state, expected_revision=revision)
    plan = preview(path, 'unknown')
    assert plan['counts']['unknown'] == 5
    receipt = apply(path, plan, tmp_path / 'unknown.sqlite3')
    assert receipt['retiredCourseIds'] == []
    assert set(repo.accessible_course_refs('owner-account', 'one')[i]['courseId'] for i in range(6)) == set(ids) | {'custom-course'}
    with sqlite3.connect(path) as db:
        db.execute('CREATE INDEX schema_race ON treehouse_catalogues(revision)')
    with pytest.raises(ValueError, match='Newer database state'):
        restore(path, tmp_path / 'unknown.sqlite3', receipt)
    changed_plan = preview(path, 'schema-only')
    with sqlite3.connect(path) as db:
        db.execute('DROP INDEX schema_race')
    with pytest.raises(ValueError, match='Database changed'):
        apply(path, changed_plan, tmp_path / 'schema-race.sqlite3')


def test_legacy_seed_entrypoint_is_no_write_and_manage_starts_empty(tmp_path, monkeypatch):
    import asyncio
    import os
    import subprocess
    import sys
    from pathlib import Path
    from src.openclank import copal_manage
    script = Path(__file__).resolve().parents[1] / 'scripts/seed_treehouse_class.py'
    result = subprocess.run([sys.executable, str(script)], cwd=tmp_path, env={**os.environ,'HOME':str(tmp_path)}, capture_output=True, text=True)
    assert result.returncode != 0 and 'retired' in result.stderr.lower()
    assert not (tmp_path / '.odysseus').exists()
    path = tmp_path / 'tool.sqlite3'
    monkeypatch.setenv('TREEHOUSE_REPOSITORY_PATH',str(path))
    monkeypatch.setattr(copal_manage,'configured_bridge',lambda:object())
    monkeypatch.setattr(copal_manage,'_configured_history_client',lambda *_:None)
    asyncio.run(copal_manage.manage_copal({'action':'treehouse.command','workspace':'one','commandId':'private-author','expectedRevision':0,'command':{'type':'course.create','payload':{'id':'custom-only','title':'My class'}}},owner='owner-account',account_id='owner-account',actor_id='owner-account'))
    state,_ = TreeHouseRepository(path).get_catalogue('owner-account','one')
    assert set(state['courses']) == {'custom-only'}
