"""Real WAL contention fixtures; no services, native owner or user stores."""
from __future__ import annotations

import asyncio
import copy
import sqlite3
import time
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from src.openclank.code_index_lifecycle import workspace_index_event
from src.openclank.file_policy import FilePolicyRepository


@pytest.fixture
def policy(tmp_path):
    repository = FilePolicyRepository(tmp_path / 'authority.sqlite')
    users = {'alice': {'account_id': 'account-alice', 'is_admin': False},
             'bob': {'account_id': 'account-bob', 'is_admin': False}}
    repository.sync_subjects(users)
    folder = tmp_path / 'codebase'
    folder.mkdir()
    location = repository.create_location(actor_subject_id='account-alice', path=str(folder),
        kind='directory', capabilities=('read', 'write'))
    workspace = repository.create_workspace(actor_subject_id='account-alice',
        owner_subject_id='account-alice', location_id=location.id, name='Source')
    repository.create_binding(actor_subject_id='account-alice', binding_class='people',
        subject_id='account-alice', location_id=location.id, capabilities=('read', 'write'))
    repository.create_binding(actor_subject_id='account-alice', binding_class='agent',
        subject_id='account-alice', location_id=location.id, capabilities=('read', 'write'))
    return repository, users, location, workspace


@contextmanager
def writer(repository):
    connection = sqlite3.connect(repository.db_path, isolation_level=None)
    try:
        assert connection.execute('PRAGMA journal_mode').fetchone()[0] == 'wal'
        connection.execute('BEGIN IMMEDIATE')
        yield connection
    finally:
        connection.rollback()
        connection.close()


def snapshot(repository):
    with repository._connect() as connection:
        return (connection.execute('SELECT generation FROM file_policy_meta').fetchone()[0],
                connection.execute('SELECT count(*) FROM file_policy_audit').fetchone()[0],
                [tuple(r) for r in connection.execute('SELECT * FROM file_policy_subject_aliases ORDER BY username')])


def trace_connections(repository, monkeypatch):
    original = repository._connect
    statements = []
    def connect():
        connection = original()
        # Only this fixture bounds intentionally blocked mutations; production
        # busy timeouts are unchanged.
        connection.execute('PRAGMA busy_timeout=25')
        connection.set_trace_callback(statements.append)
        return connection
    monkeypatch.setattr(repository, '_connect', connect)
    return statements


def scope(location, **kwargs):
    return dict(subject_id='account-alice', is_admin=False, origin='agent',
                location_id=location.id, capability='read', **kwargs)


@pytest.mark.parametrize('operation', ['subjects', 'expiry', 'generation', 'app', 'agent', 'denied', 'status'])
def test_unchanged_projection_and_status_do_not_need_held_authority_writer(policy, monkeypatch, operation):
    repository, users, location, workspace = policy
    before = snapshot(repository)
    statements = trace_connections(repository, monkeypatch)
    with writer(repository):
        started = time.monotonic()
        if operation == 'subjects':
            repository.sync_subjects({' ALICE ': users['alice'], 'BOB': users['bob']})
        elif operation == 'expiry':
            repository.expire_bindings()
        elif operation == 'generation':
            assert repository.generation() == before[0]
        elif operation in {'app', 'agent', 'denied'}:
            args = scope(location)
            args['origin'] = 'app' if operation == 'app' else 'agent'
            if operation == 'denied': args['subject_id'] = 'account-bob'
            decision = repository.resolve(**args)
            assert decision.allowed is (operation != 'denied')
            assert decision.policy_generation == before[0]
        else:
            from src.openclank import files_principal
            auth = SimpleNamespace(users=users, account_id=lambda name: users[name]['account_id'], is_admin=lambda _: False)
            request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(auth_manager=auth)))
            monkeypatch.setattr(files_principal, 'require_user', lambda _: 'alice')
            assert files_principal.files_principal(request, repository=repository) == ('account-alice', 'alice', False)
            calls = []
            async def invoke(**arguments):
                calls.append(arguments)
                return {'lifecycle': 'running', 'progress': {'completed_files': 3880, 'total_files': 3881}}
            result = asyncio.run(workspace_index_event(repository=repository,
                provider=SimpleNamespace(code_index_lifecycle=invoke), auth_manager=auth,
                owner='alice', workspace_id=workspace.id, subscriber_id='read-fixture', action='status'))
            assert result['lifecycle'] == 'running' and len(calls) == 1
        assert time.monotonic() - started < 1.0
    assert snapshot(repository) == before
    assert not any(s.lstrip().upper().startswith(('BEGIN IMMEDIATE', 'UPDATE ', 'INSERT ', 'DELETE ')) for s in statements)


@pytest.mark.parametrize('change', ['recreated', 'admin', 'delete', 'rename', 'tombstone'])
def test_real_identity_changes_still_require_writer_then_update_once(policy, monkeypatch, change):
    repository, users, _, _ = policy
    desired = copy.deepcopy(users)
    if change == 'recreated': desired['alice']['account_id'] = 'new-account-alice'
    elif change == 'admin': desired['alice']['is_admin'] = True
    elif change == 'delete': del desired['bob']
    elif change == 'rename': desired['renamed'] = desired.pop('alice')
    else:
        with repository._connect() as connection:
            connection.execute("INSERT INTO file_policy_subject_aliases VALUES('deleted:old','retired-subject',1)")
    before = snapshot(repository)
    trace_connections(repository, monkeypatch)
    with writer(repository), pytest.raises(sqlite3.OperationalError, match='locked'):
        repository.sync_subjects(desired)
    assert snapshot(repository) == before
    repository.sync_subjects(desired)
    after = snapshot(repository)
    assert after[:2] == (before[0] + 1, before[1] + 1)
    repository.sync_subjects(desired)
    assert snapshot(repository) == after
    aliases = {name: (subject, admin) for name, subject, admin in after[2]}
    if change == 'tombstone': assert aliases['deleted:old'] == ('retired-subject', 0)
    elif change == 'recreated': assert aliases['alice'][0] == 'new-account-alice'
    elif change == 'rename': assert 'alice' not in aliases and aliases['renamed'][0] == 'account-alice'


def test_partial_normalized_sync_preserves_other_aliases_and_invalid_records(policy, monkeypatch):
    repository, users, _, _ = policy
    before = snapshot(repository)
    trace_connections(repository, monkeypatch)
    with writer(repository):
        repository.sync_subjects({' ALICE ': users['alice'], 'invalid': None, 'missing': {}}, complete=False)
        repository.sync_subjects({}, complete=False)
    assert snapshot(repository) == before
    repository.sync_subjects({'alice': users['alice']}, complete=True)
    assert [r[0] for r in snapshot(repository)[2]] == ['alice']


def test_delta_is_rechecked_after_writer_acquisition_without_duplicate_audit(policy, monkeypatch):
    repository, users, _, _ = policy
    other = FilePolicyRepository(repository.db_path)
    desired = copy.deepcopy(users)
    desired['alice']['account_id'] = 'recreated'
    before = snapshot(repository)
    begin = repository._begin
    calls = []
    def raced_begin(connection):
        calls.append(True)
        other.sync_subjects(desired)
        begin(connection)
    monkeypatch.setattr(repository, '_begin', raced_begin)
    repository.sync_subjects(desired)
    after = snapshot(repository)
    assert len(calls) == 1 and after[:2] == (before[0] + 1, before[1] + 1)
    assert after[2][0][1] == 'recreated'


def test_caller_mutation_at_begin_cannot_change_captured_subject_or_admin(policy, monkeypatch):
    repository, users, _, _ = policy
    desired = copy.deepcopy(users)
    desired['alice'] = {'account_id': 'captured-account', 'is_admin': True}
    begin = repository._begin
    def raced_begin(connection):
        desired['alice'].update(account_id='later-account', is_admin=False)
        begin(connection)
    monkeypatch.setattr(repository, '_begin', raced_begin)
    repository.sync_subjects(desired)
    assert snapshot(repository)[2][0] == ('alice', 'captured-account', 1)


def test_normalized_collisions_use_final_values_and_final_noop_needs_no_writer(policy, monkeypatch):
    repository, users, _, _ = policy
    before = snapshot(repository)
    trace_connections(repository, monkeypatch)
    with writer(repository):
        repository.sync_subjects({' ALICE ': {'account_id': 'intermediate', 'is_admin': True},
                                 'alice': users['alice'], ' BOB ': users['bob']})
    assert snapshot(repository) == before
    repository.sync_subjects({' ALICE ': users['alice'],
                             'alice': {'account_id': 'final-account', 'is_admin': True}, 'BOB': users['bob']})
    after = snapshot(repository)
    assert after[2][0] == ('alice', 'final-account', 1)
    assert after[:2] == (before[0] + 1, before[1] + 1)


def test_no_operation_resolve_keeps_one_coherent_read_snapshot(policy, monkeypatch):
    repository, _, location, _ = policy
    other = FilePolicyRepository(repository.db_path)
    before = snapshot(repository)
    original = repository._connect
    changed = []
    def connect():
        connection = original()
        def trace(sql):
            if sql.startswith('SELECT * FROM file_policy_locations') and not changed:
                changed.append(True)
                other.disable_location(location.id, actor_subject_id='account-alice')
        connection.set_trace_callback(trace)
        return connection
    monkeypatch.setattr(repository, '_connect', connect)
    decision = repository.resolve(**scope(location))
    assert changed and decision.allowed and decision.policy_generation == before[0]
    after = repository.resolve(**scope(location))
    assert not after.allowed and after.policy_generation > before[0]


def test_due_expiry_needs_writer_and_still_denies_after_expiration(policy, monkeypatch):
    repository, _, location, _ = policy
    expired = repository.create_binding(actor_subject_id='account-alice', binding_class='people',
        subject_id='account-bob', location_id=location.id, capabilities=('read',), expires_unix_ms=1)
    before = snapshot(repository)
    trace_connections(repository, monkeypatch)
    with writer(repository), pytest.raises(sqlite3.OperationalError, match='locked'):
        repository.resolve(subject_id='account-bob', is_admin=False, origin='app', location_id=location.id, capability='read')
    assert snapshot(repository) == before
    assert not repository.resolve(subject_id='account-bob', is_admin=False, origin='app', location_id=location.id, capability='read').allowed
    assert repository.get_binding(expired.id).status == 'expired'
    after = snapshot(repository)
    assert after[:2] == (before[0] + 1, before[1] + 1)
    repository.expire_bindings()
    assert snapshot(repository) == after


def test_expiry_rechecks_under_writer_if_another_writer_revoked_it(policy, monkeypatch):
    repository, _, location, _ = policy
    expired = repository.create_binding(actor_subject_id='account-alice', binding_class='people',
        subject_id='account-bob', location_id=location.id, capabilities=('read',), expires_unix_ms=1)
    other = FilePolicyRepository(repository.db_path)
    begin = repository._begin
    before = snapshot(repository)
    def raced_begin(connection):
        other.revoke_binding(expired.id, actor_subject_id='account-alice')
        begin(connection)
    monkeypatch.setattr(repository, '_begin', raced_begin)
    repository.expire_bindings()
    assert snapshot(repository)[:2] == (before[0] + 1, before[1] + 1)
    assert repository.get_binding(expired.id).status == 'revoked'


def test_once_operation_still_requires_writer_and_consumes_exactly_once(policy, monkeypatch):
    repository, _, location, _ = policy
    approval = repository.create_binding(actor_subject_id='account-alice', binding_class='operation',
        subject_id='account-alice', location_id=location.id, capabilities=('read',),
        operation='inspect', resource_ref='fixture-resource', lifetime='once')
    before = snapshot(repository)
    trace_connections(repository, monkeypatch)
    arguments = scope(location, required_operation='inspect', resource_ref='fixture-resource')
    with writer(repository), pytest.raises(sqlite3.OperationalError, match='locked'):
        repository.resolve(**arguments)
    assert snapshot(repository) == before
    first = repository.resolve(**arguments)
    assert first.allowed and first.consumed_binding_id == approval.id
    assert not repository.resolve(**arguments).allowed
    assert snapshot(repository)[:2] == (before[0] + 1, before[1] + 1)
