"""Qualified, one-shot human recording draft creation; no route authority here."""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import tempfile
import uuid

from core.atomic_io import atomic_write_text, rename_directory_noreplace
from src.openclank.history_capture import (
    HistoryContext, StrictTreeLimits, _batch_entries, _envelope, _fingerprint,
    _strict_directory_manifest,
)
from .skill_format import Skill, slugify
from .skill_lifecycle import ensure_revision, locked

_RECORDING_DRAFT_SEAL = object()
_LIMITS = StrictTreeLimits(32, 4, 1024 * 1024, 2 * 1024 * 1024, 4 * 1024 * 1024, 3.0)


class _RecordingDraftAuthority:
    """Only a qualified route may issue this original-account, source-bound authority."""
    __slots__ = ('owner', 'account', 'workspace', 'witness', '_current', '_history', '_seal')

    def __init__(self, seal, *, owner, account, workspace, witness, current, history):
        if (seal is not _RECORDING_DRAFT_SEAL or not all(type(x) is str and x and len(x) <= 512 for x in (owner, account, workspace))
                or type(witness) is not dict or set(witness) != {'source', 'anchor', 'task'}
                or not callable(current) or not callable(history)):
            raise ValueError('recording draft authority unavailable')
        source, anchor, task = witness['source'], witness['anchor'], witness['task']
        if (type(source) is not dict or set(source) != {'source_resource_id','source_revision','source_sha256','source_size','source_object_tag'}
                or type(source['source_resource_id']) is not str or not source['source_resource_id']
                or type(source['source_revision']) is not dict or set(source['source_revision']) != {'kind','value'}
                or source['source_revision'].get('kind') != 'hostFingerprint'
                or type(source['source_revision'].get('value')) is not str
                or not re.fullmatch(r'[0-9a-f]{64}', str(source['source_sha256']))
                or not re.fullmatch(r'[0-9a-f]{64}', str(source['source_object_tag']))
                or type(source['source_size']) is not int or not 0 < source['source_size'] <= 64*1024*1024
                or type(anchor) is not dict or not (
                    set(anchor) == {'kind','timeMs'} and anchor.get('kind') == 'frame'
                    and type(anchor['timeMs']) is int and 0 <= anchor['timeMs'] <= 86400000
                    or set(anchor) == {'kind','startMs','endMs'} and anchor.get('kind') == 'time_range'
                    and type(anchor['startMs']) is int and type(anchor['endMs']) is int
                    and 0 <= anchor['startMs'] < anchor['endMs'] <= 86400000)
                or task is not None and type(task) is not dict):
            raise ValueError('recording source witness unavailable')
        if task is not None:
            from src.openclank.plan_feedback import _scope
            if set(task) != {'chatID', 'scope'} or type(task['chatID']) is not str or not 0 < len(task['chatID']) <= 256:
                raise ValueError('recording task witness unavailable')
            _scope(task['scope'])
        self.owner, self.account, self.workspace = owner, account, workspace
        self.witness = json.loads(json.dumps(witness, allow_nan=False))
        self._current, self._history, self._seal = current, history, seal

    def assert_current(self):
        if self._seal is not _RECORDING_DRAFT_SEAL or self._current() is not True:
            raise ValueError('recording draft authority changed')


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False).encode('utf-8')


def add_recording_draft(manager, *, authority, request_key, fields):
    """Stage and publish once; exceptions after dispatch require original status only."""
    if type(authority) is not _RecordingDraftAuthority:
        raise ValueError('recording draft authority unavailable')
    if type(request_key) is not str or not re.fullmatch(r'[A-Za-z0-9_-]{22,128}', request_key):
        raise ValueError('invalid request key')
    allowed = {'name','description','category','when_to_use','procedure','pitfalls','verification','tags'}
    if type(fields) is not dict or set(fields) != allowed:
        raise ValueError('invalid draft fields')
    for key in ('name','description','category','when_to_use'):
        if type(fields[key]) is not str or len(fields[key].encode()) > 4096:
            raise ValueError('invalid draft fields')
    for key in ('procedure','pitfalls','verification','tags'):
        if type(fields[key]) is not list or len(fields[key]) > 64 or any(type(v) is not str or len(v.encode()) > 4096 for v in fields[key]):
            raise ValueError('invalid draft fields')
    if not fields['name'].strip() or not fields['description'].strip() or len(_canonical(fields)) > 65536:
        raise ValueError('invalid draft fields')
    authority.assert_current()
    action_id = 'skill-recording-' + hashlib.sha256(_canonical([authority.account, request_key])).hexdigest()
    skill_id = str(uuid.uuid5(uuid.NAMESPACE_URL, action_id))
    category = slugify(fields['category'] or 'general', fallback='general')
    name = slugify(fields['name'], fallback='skill')
    category_dir = os.path.abspath(os.path.join(manager.skills_root, category))
    if os.path.realpath(category_dir) != category_dir:
        raise ValueError('skill category unavailable')
    binding = {'version': 1, 'account': authority.account, 'owner': authority.owner,
               'workspace': authority.workspace, 'skill_id': skill_id, 'name': name,
               'category': category, 'fields': fields, 'witness': authority.witness}
    binding_sha = hashlib.sha256(_canonical(binding)).hexdigest()
    marker = {'version': 1, 'binding_sha256': binding_sha}
    def build(stage_dir):
        sk = Skill(name=name, skill_id=skill_id, revision=1, description=fields['description'],
            category=category, tags=list(fields['tags']), status='draft', source='user', owner=authority.owner,
            when_to_use=fields['when_to_use'], procedure=list(fields['procedure']),
            pitfalls=list(fields['pitfalls']), verification=list(fields['verification']))
        path = os.path.join(stage_dir, 'SKILL.md')
        atomic_write_text(path, sk.to_markdown())
        state = {'published': None, 'recording_draft': {'version': 1, 'action_id': action_id,
            'binding_sha256': binding_sha, 'account_id': authority.account, 'skill_id': skill_id,
            'initial_revision': 1, 'initial_hash': sk.content_hash, 'source': authority.witness['source'],
            'anchor': authority.witness['anchor'], 'task': authority.witness['task'], 'correspondence': 'unverified'}}
        ensure_revision(sk, path, state=state)
        return sk
    from .skill_draft_once import create_once
    return create_once(manager, authority=authority, action_id=action_id, skill_id=skill_id,
        category_dir=category_dir, name=name, binding_sha=binding_sha, tool_id='recording-skill-draft',
        stage_prefix='.recording-skill-', limits=_LIMITS, build=build, validate=_validate_completed,
        status_error='original recording draft status required')


def _validate_completed(client, action, action_id, owner, account, *, expected_manifest=None,
                        expected_binding=None, expected_resource=None, include_citation=False):
    import base64
    before = action['before_resources']; after = action['after_resources']
    if (action['action_id'] != action_id or action['actor_id'] != owner
            or action['actor_account_id'] != account or action['actor_kind'] != 'human'
            or action['tool_id'] != 'recording-skill-draft' or action['operation'] != 'create'
            or action['state'] != 'Complete' or type(before) is not list or len(before) != 1
            or type(after) is not list or len(after) != 1): raise ValueError('recording action unavailable')
    b, a = before[0], after[0]
    marker = b['metadata']['opaque']
    if (type(marker) is not dict or set(marker) != {'version', 'binding_sha256'}
            or type(marker['version']) is not int or marker['version'] != 1
            or type(marker['binding_sha256']) is not str
            or not re.fullmatch(r'[0-9a-f]{64}', marker['binding_sha256'])
            or action['coverage']['metadata']['batch_once'] != marker
            or expected_binding is not None and marker['binding_sha256'] != expected_binding
            or b['existence'] != 'Absent' or b['resource_type'] != 'Directory'
            or a['existence'] != 'Present' or a['resource_type'] != 'Directory'
            or a['resource_key'] != b['resource_key']
            or expected_resource is not None and b['resource_key'] != expected_resource):
        raise ValueError('recording action unavailable')
    manifest = client.read_version(action_id, a['after'])
    if (type(manifest) is not bytes or len(manifest) > _LIMITS.max_manifest_bytes
            or expected_manifest is not None and manifest != expected_manifest):
        raise ValueError('recording after-state unavailable')
    tree = json.loads(manifest)
    rows = tree.get('entries') if type(tree) is dict and tree.get('version') == 1 and tree.get('root_type') == 'directory' else None
    if type(rows) is not list or len(rows) > _LIMITS.max_entries: raise ValueError('recording tree unavailable')
    def file_bytes(path):
        found = [row for row in rows if type(row) is dict and row.get('path') == path and row.get('type') == 'file']
        if len(found) != 1: raise ValueError('recording file unavailable')
        row = found[0]; raw = base64.b64decode(row['content'], validate=True)
        if (len(raw) > _LIMITS.max_file_bytes or row.get('size') != len(raw)
                or hashlib.sha256(raw).hexdigest() != row.get('sha256')): raise ValueError('recording file changed')
        return raw
    state = json.loads(file_bytes('_lifecycle.json')); witness = state['recording_draft']
    head = file_bytes('SKILL.md'); skill = Skill.from_markdown(head.decode('utf-8'))
    skill_id = str(uuid.uuid5(uuid.NAMESPACE_URL, action_id)); head_hash = witness['initial_hash']
    history = state['history']
    if (witness['action_id'] != action_id or witness['account_id'] != account
            or witness['binding_sha256'] != marker['binding_sha256'] or witness['skill_id'] != skill_id
            or witness['initial_revision'] != 1 or not re.fullmatch(r'[0-9a-f]{64}', head_hash)
            or state['skill_id'] != skill_id or state['owner'] != owner or state['published'] is not None
            or state['head_revision'] != 1 or state['head_hash'] != head_hash
            or skill.skill_id != skill_id or skill.revision != 1 or skill.owner != owner
            or skill.status != 'draft' or skill.source != 'user' or skill.content_hash != head_hash
            or skill.compute_content_hash() != head_hash or type(history) is not list or len(history) != 1):
        raise ValueError('recording lifecycle unavailable')
    row = history[0]
    if (row['skill_id'] != skill_id or row['owner'] != owner or row['revision'] != 1
            or row['content_hash'] != head_hash or row['snapshot_sha256'] != hashlib.sha256(head).hexdigest()
            or file_bytes(row['snapshot']) != head): raise ValueError('recording snapshot unavailable')
    result = {'state': 'complete', 'action_id': action_id, 'skill_id': skill_id,
              'initial_revision': 1, 'initial_hash': head_hash}
    if include_citation:
        source = witness['source']; anchor = witness['anchor']
        if (type(source) is not dict or type(source.get('source_sha256')) is not str
                or not re.fullmatch(r'[0-9a-f]{64}', source['source_sha256']) or type(anchor) is not dict
                or not ((set(anchor) == {'kind', 'timeMs'} and anchor.get('kind') == 'frame'
                         and type(anchor['timeMs']) is int and 0 <= anchor['timeMs'] <= 86400000)
                    or (set(anchor) == {'kind', 'startMs', 'endMs'} and anchor.get('kind') == 'time_range'
                         and type(anchor['startMs']) is int and type(anchor['endMs']) is int
                         and 0 <= anchor['startMs'] < anchor['endMs'] <= 86400000))
                or witness.get('correspondence') != 'unverified'):
            raise ValueError('recording citation unavailable')
        result['citation'] = {'scope': 'historical_initial_revision', 'initial_revision': 1,
            'initial_hash': head_hash, 'source_sha256': source['source_sha256'],
            'anchor': dict(anchor), 'correspondence': 'unverified'}
    return result


def recording_draft_status(*, owner, account, request_key, client, current):
    """Read only the original retained human action; missing/expired means unknown."""
    if (not callable(current) or current() is not True or type(owner) is not str or not owner
            or type(account) is not str or not account or type(request_key) is not str
            or not re.fullmatch(r'[A-Za-z0-9_-]{22,128}', request_key)):
        raise ValueError('recording draft status unavailable')
    action_id = 'skill-recording-' + hashlib.sha256(_canonical([account, request_key])).hexdigest()
    unknown = {'state': 'unknown', 'action_id': action_id}
    try:
        result = _validate_completed(client, client.get_capture_action(action_id)['Action'], action_id, owner, account)
        return result if current() is True else unknown
    except Exception:
        return unknown
