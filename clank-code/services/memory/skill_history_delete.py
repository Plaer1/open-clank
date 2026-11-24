"""Private POSIX exact-tree effect for a reviewed, locked Skills operation.

No route invokes this owner.  Interrupted or unprovable effects remain on the
original native History action; neither a delete retry nor a rollback is issued.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import stat
import sys

from core.atomic_io import rename_directory_noreplace
from src.openclank import history_capture as capture
from .skill_format import Skill
from .skill_lifecycle import _LOCK_STATE, _SkillMetadataAuthority, _prepared_unique_pairs


def _action_id(owner, account, request_key):
    if (type(request_key) is not str or not request_key or request_key != request_key.strip()
            or len(request_key.encode()) > 256):
        raise ValueError('skill delete request unavailable')
    return 'skill-delete-' + hashlib.sha256(json.dumps([owner, account, request_key], separators=(',', ':')).encode()).hexdigest()


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def _identity(value):
    return (int(value.st_dev), int(value.st_ino))


def _require_locked(path):
    skill_dir = os.path.dirname(os.path.abspath(path))
    skills_root = os.path.dirname(os.path.dirname(skill_dir))
    held = getattr(_LOCK_STATE, 'held', {})
    required = (os.path.realpath(os.path.join(skills_root, '.lifecycle.lock')),
                os.path.realpath(os.path.join(skill_dir, '.lifecycle.lock')))
    if not all(held.get(item, 0) for item in required):
        raise ValueError('skill delete locks unavailable')


def _review_manifest(manifest, review, owner):
    if hashlib.sha256(manifest).hexdigest() != review['tree_sha256']:
        raise ValueError('reviewed skill tree changed')
    parsed = json.loads(manifest.decode('utf-8'), object_pairs_hook=_prepared_unique_pairs)
    if type(parsed) is not dict or set(parsed) != {'version','root_type','entries'} or parsed['version'] != 1 or parsed['root_type'] != 'directory' or type(parsed['entries']) is not list:
        raise ValueError('skill tree unavailable')
    by_path = {}
    for entry in parsed['entries']:
        if type(entry) is not dict or type(entry.get('path')) is not str or entry['path'] in by_path:
            raise ValueError('skill tree unavailable')
        by_path[entry['path']] = entry
    def read(name):
        entry = by_path.get(name)
        if type(entry) is not dict or entry.get('type') != 'file' or entry.get('content_encoding') != 'base64':
            raise ValueError('skill tree unavailable')
        raw = base64.b64decode(entry.get('content',''), validate=True)
        if len(raw) != entry.get('size') or hashlib.sha256(raw).hexdigest() != entry.get('sha256'):
            raise ValueError('skill tree unavailable')
        return raw
    head_raw, lifecycle_raw = read('SKILL.md'), read('_lifecycle.json')
    if hashlib.sha256(head_raw).hexdigest() != review['head_sha256'] or hashlib.sha256(lifecycle_raw).hexdigest() != review['lifecycle_sha256']:
        raise ValueError('reviewed skill state changed')
    skill = Skill.from_markdown(head_raw.decode('utf-8'))
    state = json.loads(lifecycle_raw.decode('utf-8'), object_pairs_hook=_prepared_unique_pairs)
    if (type(state) is not dict or skill.skill_id != review['skill_id'] or skill.owner != owner
            or skill.revision != review['revision'] or skill.content_hash != review['content_hash']
            or skill.compute_content_hash() != review['content_hash'] or state.get('skill_id') != skill.skill_id
            or state.get('owner') != owner or state.get('head_revision') != skill.revision
            or state.get('head_hash') != skill.content_hash or state.get('published') != review['published']
            or any(state.get(k) is not None for k in ('metadata_pending','metadata_audit_pending','metadata_recovery_pending'))):
        raise ValueError('reviewed skill state changed')
    return by_path


def _remove_members(root_fd, records, limits):
    """Remove only manifest members through no-follow descriptors; errors stay partial."""
    budget = capture._StrictTreeBudget(limits)
    children = {}
    for path, row in records.items():
        parts = path.split('/')
        if any(not part or part in ('.','..') for part in parts): raise ValueError('skill tree unavailable')
        parent = '/'.join(parts[:-1])
        children.setdefault(parent, {})[parts[-1]] = row
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    def visit(directory_fd, relative):
        budget.check()
        expected = children.get(relative, {})
        with os.scandir(directory_fd) as iterator:
            names = []
            for entry in iterator:
                names.append(entry.name); budget.names(len(names))
        if set(names) != set(expected): raise ValueError('skill tree changed during removal')
        for name in sorted(names):
            budget.check(); row = expected[name]
            info = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            if stat.S_ISLNK(info.st_mode) or (info.st_mode & 0o7777) != row.get('mode') or info.st_mtime_ns // 1_000_000 != row.get('mtime_millis'):
                raise ValueError('skill tree changed during removal')
            child_relative = name if not relative else relative + '/' + name
            if row.get('type') == 'directory' and stat.S_ISDIR(info.st_mode):
                child = os.open(name, flags, dir_fd=directory_fd)
                try:
                    opened = os.fstat(child)
                    if not capture._same_stability(info, opened): raise ValueError('skill directory changed')
                    visit(child, child_relative)
                    current = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
                    if _identity(current) != _identity(opened): raise ValueError('skill directory changed')
                    os.rmdir(name, dir_fd=directory_fd)
                    if os.fstat(child).st_nlink != 0: raise ValueError('skill directory removal unknown')
                finally: os.close(child)
            elif row.get('type') == 'file' and stat.S_ISREG(info.st_mode):
                raw = capture._read_strict_fd(name, directory_fd, info, budget=budget)
                if len(raw) != row.get('size') or hashlib.sha256(raw).hexdigest() != row.get('sha256'):
                    raise ValueError('skill file changed')
                current = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
                if not capture._same_stability(current, info): raise ValueError('skill file changed')
                os.unlink(name, dir_fd=directory_fd)
            else:
                raise ValueError('skill tree type changed')
    visit(root_fd, '')


def _prepare_once(client, context, directory, action_id, binding_sha256, manifest, limits, identity):
    """A fresh native grant is required; Existing is status-only."""
    envelope = capture._envelope(action_id=action_id, operation='delete', path=directory,
        before=manifest, context=context, client=client, paths=[])
    marker = {'version':1, 'binding_sha256':binding_sha256}
    envelope['coverage']['metadata'] = {'batch_once':marker}
    identities = {directory:identity}
    entries, observed = capture._batch_entries(path=directory, paths=[], envelope=envelope, context=context,
        strict_tree=True, expected_identities=identities, strict_tree_limits=limits)
    if len(entries) != 1 or observed != manifest:
        raise ValueError('skill delete preimage changed')
    entries[0]['metadata']['opaque'] = marker
    capture._validate_strict_batch_preimage(targets=[directory], entries=entries,
        expected_identities=identities, strict_tree_limits=limits)
    result = client.prepare_batch_once(envelope, entries)
    if result.get('created') is not True:
        raise ValueError('skill delete already issued; inspect status')
    action = result.get('action')
    if type(action) is not dict or action.get('state') != 'BeforeDurable':
        raise ValueError('skill delete History preparation unknown')
    return capture.CaptureHandle(action_id, client, envelope, observed, context, 'prepared', 'before_durable',
        batch_entries=entries, paths=(directory,), strict_tree=True, expected_identities=identities,
        strict_tree_limits=limits)


def _settle_absent(client, handle):
    """Settle only an already positively observed removal on its original action."""
    if handle.batch_entries is None or len(handle.batch_entries) != 1:
        raise ValueError('skill delete History action unavailable')
    before = handle.batch_entries[0]; key = before['resource_key']
    client.record_live(handle.action_id, {'action_id':handle.action_id, 'status':'Committed', 'fingerprint':'missing'})
    after = {'resource_key':key, 'locator':before.get('new_locator'), 'existence':'Absent',
        'resource_type':'Directory', 'metadata':{'mode':None,'size':None,'modified_millis':None,'opaque':None},
        'content':None, 'fingerprint':'missing', 'coverage':{'metadata':{'exact_after':True},'byte_len':0,'content_digest':None},
        'outcome':{'resource_id':key['resource_id'],'status':'Committed','revision':{'Opaque':{'kind':'fingerprint','value':'missing'}}}}
    result = client.complete_batch(handle.action_id, [after])
    action = result.get('Action') if type(result) is dict else None
    if type(action) is not dict or action.get('state') != 'Complete': raise ValueError('skill delete settlement unknown')
    return action


def remove_reviewed_tree(*, authority, owner, path, review, request_key, limits, revalidate):
    """Execute one reviewed delete; caller must hold both canonical Skills locks.

    `revalidate(fd, phase)` is the trusted manager's exact current head/pointer/receipt check against the pinned directory.
    Any error after preparing History is status-only; never reissue this request.
    """
    if sys.platform != 'linux' or type(authority) is not _SkillMetadataAuthority or type(limits) is not capture.StrictTreeLimits or not callable(revalidate):
        raise ValueError('skill delete unavailable')
    keys = {'skill_id','revision','content_hash','head_sha256','lifecycle_sha256','published','tree_sha256','directory_identity','parent_identity','domain_refs'}
    if type(review) is not dict or set(review) != keys or type(review['domain_refs']) is not list:
        raise ValueError('skill delete review unavailable')
    _require_locked(path)
    directory = os.path.dirname(os.path.abspath(path)); parent = os.path.dirname(directory); name = os.path.basename(directory)
    if os.path.basename(path) != 'SKILL.md' or os.path.realpath(parent) != parent:
        raise ValueError('skill delete path unavailable')
    action_id = _action_id(owner, authority.account_id, request_key)
    review_digest = _digest(review)
    tombstone = '.skill-delete-' + hashlib.sha256(action_id.encode()).hexdigest()[:40]
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    parent_fd = os.open(parent, flags); tree_fd = None; scope = None; handle = None
    try:
        if list(_identity(os.fstat(parent_fd))) != review['parent_identity']:
            raise ValueError('skill delete parent changed')
        tree_fd = os.open(name, flags, dir_fd=parent_fd); initial = os.fstat(tree_fd)
        if list(_identity(initial)) != review['directory_identity']:
            raise ValueError('skill delete tree changed')
        authority.assert_current(owner, review['domain_refs'])
        if revalidate(tree_fd, 'before_capture') is not True: raise ValueError('skill delete authority changed')
        manifest = capture._strict_directory_manifest(directory, expected=initial, limits=limits,
            pinned_root_fd=tree_fd, pinned_parent_fd=parent_fd, pinned_name=name)
        _review_manifest(manifest, review, owner)
        scope = authority.open_history(owner, review['skill_id'], directory, _identity(initial))
        if not scope['is_current'](): raise ValueError('skill delete scope changed')
        client = scope['client']
        context = capture.HistoryContext(actor_id=owner, account_id=authority.account_id, workspace_id=scope['workspace_id'],
            root_bindings=((scope['root_id'], directory),), client=client, actor_kind='human', tool_id='skill-delete')
        handle = _prepare_once(client, context, directory, action_id, review_digest, manifest, limits, _identity(initial))
        record = client.get_capture_action(action_id)['Action']; rows = record.get('before_resources')
        if (record.get('state') != 'BeforeDurable' or record.get('actor_id') != owner or record.get('actor_account_id') != authority.account_id
                or record.get('operation') != 'delete' or record.get('tool_id') != 'skill-delete'
                or (record.get('coverage') or {}).get('metadata', {}).get('batch_once') != {'version':1,'binding_sha256':review_digest}
                or type(rows) is not list or len(rows) != 1 or rows[0].get('resource_key') != handle.batch_entries[0]['resource_key']
                or (rows[0].get('metadata') or {}).get('opaque') != {'version':1,'binding_sha256':review_digest}
                or client.read_version(action_id, rows[0].get('before')) != manifest):
            raise ValueError('skill delete History preimage unavailable')
        authority.assert_current(owner, review['domain_refs'])
        if not scope['is_current']() or revalidate(tree_fd, 'before_move') is not True: raise ValueError('skill delete authority changed')
        current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if _identity(current) != _identity(initial) or capture._strict_directory_manifest(directory, expected=current, limits=limits,
                pinned_root_fd=tree_fd, pinned_parent_fd=parent_fd, pinned_name=name) != manifest:
            raise ValueError('skill tree changed before removal')
        rename_directory_noreplace(parent_fd, name, parent_fd, tombstone)
        moved = os.stat(tombstone, dir_fd=parent_fd, follow_symlinks=False)
        if _identity(moved) != _identity(initial) or _identity(os.fstat(tree_fd)) != _identity(initial):
            raise ValueError('skill delete move unknown')
        moved_path = os.path.join(parent, tombstone)
        if capture._strict_directory_manifest(moved_path, expected=moved, limits=limits,
                pinned_root_fd=tree_fd, pinned_parent_fd=parent_fd, pinned_name=tombstone) != manifest:
            raise ValueError('skill delete tree changed after move')
        authority.assert_current(owner, review['domain_refs'])
        if not scope['is_current']() or revalidate(tree_fd, 'after_move') is not True: raise ValueError('skill delete authority changed')
        _remove_members(tree_fd, _review_manifest(manifest, review, owner), limits)
        if _identity(os.stat(tombstone, dir_fd=parent_fd, follow_symlinks=False)) != _identity(initial):
            raise ValueError('skill delete tree changed')
        os.rmdir(tombstone, dir_fd=parent_fd)
        if os.fstat(tree_fd).st_nlink != 0: raise ValueError('skill delete removal unknown')
        for leaf in (name, tombstone):
            try: os.stat(leaf, dir_fd=parent_fd, follow_symlinks=False)
            except FileNotFoundError: continue
            raise ValueError('skill delete absence unknown')
        os.fsync(parent_fd)
        authority.assert_current(owner, review['domain_refs'])
        if not scope['is_current'](): raise ValueError('skill delete settlement unknown')
        action = _settle_absent(client, handle)
        return {'state':'complete','action_id':action_id,'review_sha256':review_digest,'history_action':action}
    finally:
        try:
            if handle is not None: handle.close()
            if scope is not None: authority.close_history(scope)
        finally:
            if tree_fd is not None: os.close(tree_fd)
            os.close(parent_fd)


def delete_fact_status(*, owner, account, request_key, review_sha256, client, current):
    """Project only the original action fact; no source or version read."""
    if (type(owner) is not str or not owner or type(account) is not str or not account
            or type(review_sha256) is not str or len(review_sha256) != 64
            or any(c not in '0123456789abcdef' for c in review_sha256)
            or not callable(current) or current() is not True):
        raise ValueError('skill delete status unavailable')
    action_id = _action_id(owner, account, request_key)
    action = client.get_capture_action(action_id)['Action']
    if current() is not True: raise ValueError('skill delete status unavailable')
    marker = {'version':1,'binding_sha256':review_sha256}
    if (type(action) is not dict or action.get('action_id') != action_id
            or action.get('actor_id') != owner or action.get('actor_account_id') != account
            or action.get('actor_kind') != 'human' or action.get('operation') != 'delete'
            or action.get('tool_id') != 'skill-delete'
            or (action.get('coverage') or {}).get('metadata', {}).get('batch_once') != marker):
        raise ValueError('skill delete status unavailable')
    key = action.get('resource_key'); before = action.get('before_resources'); after = action.get('after_resources')
    live = action.get('live')
    complete = (action.get('state') == 'Complete' and type(key) is dict
        and key.get('account_id') == account and key.get('provider') == 'filesystem'
        and type(key.get('workspace_id')) is str and key['workspace_id'].startswith('skills:')
        and type(key.get('resource_id')) is str and bool(key['resource_id'])
        and action.get('modified_resource_ids') == [key] and type(before) is list and len(before) == 1
        and type(before[0]) is dict and before[0].get('resource_key') == key
        and before[0].get('existence') == 'Present' and before[0].get('resource_type') == 'Directory'
        and (before[0].get('metadata') or {}).get('opaque') == marker
        and type(after) is list and len(after) == 1 and type(after[0]) is dict
        and after[0].get('resource_key') == key and after[0].get('resource_type') == 'Directory'
        and after[0].get('existence') == 'Absent' and after[0].get('fingerprint') == 'missing'
        and (after[0].get('after') or {}).get('content') == 'Tombstone'
        and type(live) is dict and live.get('action_id') == action_id
        and live.get('status') == 'Committed' and live.get('fingerprint') == 'missing')
    if current() is not True: raise ValueError('skill delete status unavailable')
    return {'state':'complete' if complete else 'unknown', 'action_id':action_id, 'review_sha256':review_sha256}


def reviewed_delete_status(*, authority, owner, request_key, review_sha256):
    """Compatibility wrapper for the original sealed manager owner."""
    if type(authority) is not _SkillMetadataAuthority: raise ValueError('skill delete status unavailable')
    def current():
        try:
            authority.assert_current(owner, [])
            return True
        except Exception:
            return False
    return delete_fact_status(owner=owner, account=authority.account_id, request_key=request_key,
        review_sha256=review_sha256, client=authority.read_history(owner), current=current)
