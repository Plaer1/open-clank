"""Exact preimage capture within an already authorized, locked Skills stage."""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import os
import stat

from src.openclank.history_capture import HistoryContext, begin_file_capture, complete_file_capture
from .skill_lifecycle import PreparedReadBudget, load_state, state_path
from .skill_format import Skill


@contextmanager
def capture_stage(authority, owner, skill_id, path, expected_head, expected_state, refs):
    directory = os.path.dirname(path)
    named = os.lstat(directory)
    if not stat.S_ISDIR(named.st_mode) or os.path.realpath(directory) != os.path.abspath(directory):
        raise ValueError('skill History directory unavailable')
    budget = PreparedReadBudget(max_file=8*1024*1024, max_bytes=24*1024*1024, seconds=4)
    lifecycle = state_path(path)
    before = [budget.read(path), budget.read(lifecycle)]
    candidate = Skill.from_markdown(before[0].decode('utf-8'), path=path)
    if candidate.to_markdown() != expected_head.to_markdown() or load_state(path, budget=budget) != expected_state:
        raise ValueError('skill History preimage changed')
    scope = authority.open_history(owner, skill_id, directory, (named.st_dev, named.st_ino))
    handle = None
    try:
        now = os.lstat(directory)
        if (now.st_dev, now.st_ino) != (named.st_dev, named.st_ino):
            raise ValueError('skill History directory changed')
        check = PreparedReadBudget(max_file=8*1024*1024, max_bytes=24*1024*1024, seconds=4)
        if [check.read(path), check.read(lifecycle)] != before:
            raise ValueError('skill History preimage changed')
        client = scope['client']
        context = HistoryContext(actor_id=owner, account_id=authority.account_id, workspace_id=scope['workspace_id'],
            root_bindings=((scope['root_id'],directory),), client=client, actor_kind='human', tool_id='skill-metadata')
        handle = begin_file_capture(path, paths=[lifecycle], operation='edit', context=context)
        if not handle.available or handle.batch_entries is None:
            raise ValueError('skill History preimage unavailable')
        record = client.get_capture_action(handle.action_id)['Action']
        resources = record.get('before_resources')
        if type(resources) is not list or len(resources) != 2 or len(handle.batch_entries) != 2:
            raise ValueError('skill History preimage unavailable')
        versions = []
        for entry, captured, expected in zip(handle.batch_entries, resources, before):
            version = captured.get('before') if type(captured) is dict else None
            key = entry['resource_key']
            if type(version) is not dict or captured.get('resource_key') != key or client.read_version(handle.action_id, version) != expected:
                raise ValueError('skill History preimage unavailable')
            versions.append({'resource':key, 'version':version, 'sha256':hashlib.sha256(expected).hexdigest(), 'length':len(expected)})
        check = PreparedReadBudget(max_file=8*1024*1024, max_bytes=24*1024*1024, seconds=4)
        if [check.read(path), check.read(lifecycle)] != before:
            raise ValueError('skill History preimage changed')
        try:
            yield {'action_id':handle.action_id, 'versions':versions}
        except BaseException:
            # A partial stage must not be completed under revoked authority.
            # The original action remains available for status/reconciliation.
            try:
                authority.assert_current(owner, refs)
                if not scope['is_current'](): raise ValueError('skill History scope changed')
            except Exception:
                handle.close()
                handle = None
            else:
                complete_file_capture(handle, path)
                handle = None
            raise
        else:
            try:
                authority.assert_current(owner, refs)
                if not scope['is_current'](): raise ValueError('skill History scope changed')
            except Exception:
                handle.close()
                handle = None
                raise ValueError('skill History settlement unknown')
            result = complete_file_capture(handle, path)
            handle = None
            if result.get('history_status') != 'complete':
                raise ValueError('skill History settlement unknown')
    finally:
        if handle is not None:
            handle.abort()
            handle.close()
        authority.close_history(scope)


def read_stage_preimage(authority, owner, binding):
    """Read only the original actor's two exact retained stage preimages."""
    if type(binding) is not dict or set(binding) != {'action_id','versions'} or type(binding['action_id']) is not str or type(binding['versions']) is not list or len(binding['versions']) != 2:
        raise ValueError('skill History preimage unavailable')
    client = authority.read_history(owner)
    record = client.get_capture_action(binding['action_id'])['Action']
    if (record.get('action_id') != binding['action_id'] or record.get('actor_id') != owner
        or record.get('actor_account_id') != authority.account_id or record.get('actor_kind') != 'human'
        or record.get('operation') != 'edit' or record.get('tool_id') != 'skill-metadata'):
        raise ValueError('skill History action unavailable')
    recorded = record.get('before_resources')
    if type(recorded) is not list or len(recorded) != 2:
        raise ValueError('skill History preimage unavailable')
    output = []
    for entry, actual in zip(binding['versions'], recorded):
        if type(entry) is not dict or set(entry) != {'resource','version','sha256','length'} or type(actual) is not dict:
            raise ValueError('skill History preimage unavailable')
        key = entry['resource']; version = entry['version']
        if (type(key) is not dict or key.get('account_id') != authority.account_id or key.get('provider') != 'filesystem'
            or type(key.get('workspace_id')) is not str or not key['workspace_id'].startswith('skills:')
            or actual.get('resource_key') != key or actual.get('before') != version
            or type(entry['length']) is not int or not 0 <= entry['length'] <= 8*1024*1024):
            raise ValueError('skill History preimage unavailable')
        raw = client.read_version(binding['action_id'], version)
        if type(raw) is not bytes or len(raw) != entry['length'] or hashlib.sha256(raw).hexdigest() != entry['sha256']:
            raise ValueError('skill History preimage unavailable')
        output.append(raw)
    if binding['versions'][0]['resource'] == binding['versions'][1]['resource'] or binding['versions'][0]['resource']['workspace_id'] != binding['versions'][1]['resource']['workspace_id']:
        raise ValueError('skill History preimage unavailable')
    return output
