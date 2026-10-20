"""Independent writers must not both pass one loose-document revision guard."""

import asyncio
import multiprocessing
from pathlib import Path

import pytest

from src.openclank.copal_commit_lock import copal_commit_lock
from src.openclank.copal_loose import LooseCopalRepository


def _writer(root, document, text, connection, entered=None, release=None):
    repository = LooseCopalRepository(root)
    if entered is not None:
        original = repository._write_record

        def paused(*args, **kwargs):
            entered.set()
            if not release.wait(10):
                raise RuntimeError('test did not release writer')
            return original(*args, **kwargs)

        repository._write_record = paused
    connection.send('started')
    result = asyncio.run(repository.call('write', {
        'owner':'alice', 'workspace_id':'study', 'id':document['id'],
        'base':document['head'], 'content':text,
    }))
    connection.send(result)
    connection.close()


def _hold_lock(root, entered):
    with copal_commit_lock(Path(root)):
        entered.send('locked')
        entered.recv()


def test_two_processes_cannot_commit_the_same_revision(tmp_path):
    root = tmp_path / 'vaults'
    repository = LooseCopalRepository(root)
    document = asyncio.run(repository.call('create', {
        'owner':'alice', 'workspace_id':'study', 'name':'race.md', 'content':'initial',
    }))['doc']
    ctx = multiprocessing.get_context('spawn')
    entered, release = ctx.Event(), ctx.Event()
    first_pipe, first_child = ctx.Pipe()
    second_pipe, second_child = ctx.Pipe()
    first = ctx.Process(target=_writer, args=(root, document, 'first', first_child, entered, release))
    second = ctx.Process(target=_writer, args=(root, document, 'second', second_child))
    try:
        first.start()
        assert first_pipe.poll(10) and first_pipe.recv() == 'started'
        assert entered.wait(10), 'first writer is paused inside the commit boundary'
        second.start()
        assert second_pipe.poll(10) and second_pipe.recv() == 'started'
        assert not second_pipe.poll(0.3), 'second process must wait outside the revision check'
        release.set()
        assert first_pipe.poll(10) and second_pipe.poll(10)
        first_result, second_result = first_pipe.recv(), second_pipe.recv()
        assert first_result['outcome'] == 'committed'
        assert second_result['outcome'] == 'stale'
        assert second_result['doc']['text'] == 'first'
        current = asyncio.run(repository.call('get', {'owner':'alice', 'workspace_id':'study', 'id':document['id']}))
        assert current['text'] == 'first'
    finally:
        release.set()
        for process in (first, second):
            if process.pid is not None:
                process.join(5)
                if process.is_alive():
                    process.terminate()
                    process.join(5)
        first_pipe.close()
        second_pipe.close()


def test_process_exit_releases_backend_lock(tmp_path):
    ctx = multiprocessing.get_context('spawn')
    parent, child = ctx.Pipe()
    holder = ctx.Process(target=_hold_lock, args=(tmp_path, child))
    holder.start()
    try:
        assert parent.poll(10) and parent.recv() == 'locked'
        holder.terminate()
        holder.join(5)
        assert not holder.is_alive()
        with copal_commit_lock(tmp_path):
            pass
    finally:
        if holder.is_alive():
            holder.terminate()
        holder.join(5)
        parent.close()


@pytest.mark.asyncio
async def test_external_edit_conflicts_and_preserves_exact_newlines(tmp_path):
    repository = LooseCopalRepository(tmp_path)
    scope = {'owner':'alice', 'workspace_id':'study'}
    original = '\ufefffirst\r\nsecond\r\n'
    document = (await repository.call('create', {**scope, 'name':'external.md', 'content':original}))['doc']
    assert document['text'] == original
    current = await repository.call('get', {**scope, 'id':document['id']})
    assert current['head'] == document['head']
    path = next(tmp_path.rglob('external.md'))
    path.write_bytes(b'external\r\n')
    result = await repository.call('write', {**scope, 'id':document['id'], 'base':document['head'], 'content':'clobber'})
    assert result['outcome'] == 'stale'
    assert result['doc']['head'] != document['head']
    assert result['doc']['text'] == 'external\r\n'
    assert path.read_bytes() == b'external\r\n'
