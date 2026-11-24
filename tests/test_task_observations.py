"""Focused scheduler observation boundary with disposable stores."""
import asyncio
from types import SimpleNamespace

import httpx
from fastapi import FastAPI
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from core.database import Base, ScheduledTask, TaskRun, TaskWaitRequest
from src.openclank.file_policy import FilePolicyRepository
import routes.task.task_routes as task_routes


def test_scheduler_observation_owner_pages_and_workspace_no_maintenance(tmp_path, monkeypatch):
    engine = create_engine('sqlite://', connect_args={'check_same_thread': False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine)
    repo = FilePolicyRepository(tmp_path / 'policy.sqlite')
    repo.sync_subjects({'alice': {'account_id': 'account-alice', 'is_admin': False},
                        'bob': {'account_id': 'account-bob', 'is_admin': False}})
    folder = tmp_path / 'workspace'
    folder.mkdir()
    location = repo.create_location(actor_subject_id='account-alice', path=str(folder), kind='directory', capabilities=('read',))
    workspace = repo.create_workspace(actor_subject_id='account-alice', owner_subject_id='account-alice', location_id=location.id, name='Source')
    repo.create_binding(actor_subject_id='account-alice', binding_class='people', subject_id='account-alice', location_id=location.id, capabilities=('read',))
    expired = repo.create_binding(actor_subject_id='account-alice', binding_class='people', subject_id='account-bob', location_id=location.id, capabilities=('read',), expires_unix_ms=1)
    with sessions.begin() as db:
        db.add_all([ScheduledTask(id='a', owner='alice', name='A', workspace_id=workspace.id, prompt='secret prompt'),
                    ScheduledTask(id='b', owner='alice', name='B'), ScheduledTask(id='c', owner='bob', name='C'),
                    TaskRun(id='r', task_id='a', result='secret result'),
                    TaskWaitRequest(id='w', task_id='a', run_id='r', session_id='s', owner='alice', workspace_id=workspace.id, payload='secret payload')])
    monkeypatch.setattr(task_routes, 'SessionLocal', sessions)
    monkeypatch.setattr(task_routes, 'require_user', lambda request: None)
    monkeypatch.setattr(task_routes, 'effective_user', lambda request: 'alice')
    app = FastAPI()
    app.state.files_policy_repository = repo
    app.state.auth_manager = SimpleNamespace(account_id=lambda owner: 'account-' + owner, is_admin=lambda owner: False)
    app.include_router(task_routes.setup_task_routes(SimpleNamespace()))
    async def exercise():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
            first = await client.get('/api/tasks/observations/scheduler_routine', params={'limit': 1})
            assert first.status_code == 200, first.text
            page = first.json()['sources']['scheduler_routine']
            assert [x['title'] for x in page['items']] == ['A']
            second = await client.get('/api/tasks/observations/scheduler_routine', params={'cursor': page['nextCursor']})
            assert [x['title'] for x in second.json()['sources']['scheduler_routine']['items']] == ['B']
            assert (await client.get('/api/tasks/observations/scheduler_run', params={'cursor': page['nextCursor']})).status_code == 400
            for source in ('scheduler_routine', 'scheduler_run', 'scheduler_wait'):
                result = await client.get('/api/tasks/observations/' + source, params={'workspace_id': workspace.id})
                assert result.status_code == 200, result.text
                assert len(result.json()['sources'][source]['items']) == 1
                assert 'secret' not in result.text
    asyncio.run(exercise())
    assert repo.get_binding(expired.id).status == 'active'
    decision = repo.resolve(subject_id='account-bob', is_admin=False, origin='app', capability='read', location_id=location.id, workspace_id=workspace.id, observation_only=True)
    assert not decision.allowed
    assert repo.get_binding(expired.id).status == 'active'
