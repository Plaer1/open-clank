"""Private shared pinned Skills/History once-create transaction."""
import hashlib
import os
import shutil
import stat
import tempfile

from core.atomic_io import rename_directory_noreplace
from src.openclank.history_capture import HistoryContext, _batch_entries, _envelope, _fingerprint, _strict_directory_manifest
from .skill_lifecycle import locked


def create_once(manager, *, authority, action_id, skill_id, category_dir, name, binding_sha, tool_id, stage_prefix, limits, build, validate, status_error):
    from .skill_automatic_draft import _SkillDraftAgentAuthority
    agent = type(authority) is _SkillDraftAgentAuthority
    if agent and authority.mode != 'create':
        raise ValueError('automatic skill create authority unavailable')
    marker = {'version': 1, 'binding_sha256': binding_sha}
    with locked(os.path.join(manager.skills_root, 'PROMOTIONS')):
        with locked(os.path.join(manager.skills_root, '.import')):
            authority.assert_current()
            parent_fd = os.open(category_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            stage_dir = ''; stage_fd = None; stage_parent = None; scope = None; published = False
            try:
                parent = os.fstat(parent_fd)
                if not stat.S_ISDIR(parent.st_mode): raise ValueError('skill category unavailable')
                scope = authority._history('open_create', category_dir, (parent.st_dev, parent.st_ino), parent_fd, name, None)
                client = scope['client']
                context = HistoryContext(actor_id=authority.actor if agent else authority.owner, account_id=authority.account,
                    workspace_id=scope['workspace_id'], actor_kind='agent' if agent else 'human', tool_id=tool_id,
                    root_bindings=((scope['root_id'], category_dir),), client=client)
                target = os.path.join(category_dir, name)
                envelope = _envelope(action_id=action_id, operation='create', path=target, before=None, context=context, client=client)
                envelope['coverage']['metadata'] = {'batch_once': marker}
                if agent:
                    envelope['coverage']['metadata']['skill_draft'] = authority.lineage
                entries, before = _batch_entries(path=target, paths=[], envelope=envelope, context=context)
                if before is not None or entries[0]['existence'] != 'Absent': raise ValueError('skill destination exists')
                entries[0]['resource_type'] = 'Directory'
                entries[0]['metadata']['opaque'] = marker
                authority.assert_current()
                if agent:
                    authority.bind_once_original(action_id=action_id, skill_id=skill_id, binding_sha=binding_sha,
                        resource_key=entries[0]['resource_key'], directory=category_dir, child=name)
                reserved = client.prepare_batch_once(envelope, entries)
                if reserved.get('created') is not True: raise ValueError(status_error)
                authority.assert_current()
                stage_dir = tempfile.mkdtemp(prefix=stage_prefix, dir=manager.data_dir)
                stage_parent = os.open(os.path.dirname(stage_dir), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
                stage_name = os.path.basename(stage_dir)
                stage_fd = os.open(stage_name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=stage_parent)
                stage_info = os.fstat(stage_fd)
                stage_named = os.stat(stage_name, dir_fd=stage_parent, follow_symlinks=False)
                if (stage_named.st_dev, stage_named.st_ino) != (stage_info.st_dev, stage_info.st_ino): raise ValueError('stage changed')
                sk = build(stage_dir)
                os.fsync(stage_fd)
                stage_info = os.fstat(stage_fd)
                _strict_directory_manifest(stage_dir, expected=stage_info, limits=limits, pinned_root_fd=stage_fd,
                    pinned_parent_fd=stage_parent, pinned_name=stage_name)
                authority.assert_current()
                if not scope['is_current'](): raise ValueError('History authority changed')
                before_stage = os.stat(stage_name, dir_fd=stage_parent, follow_symlinks=False)
                if (before_stage.st_dev, before_stage.st_ino) != (stage_info.st_dev, stage_info.st_ino): raise ValueError('stage changed')
                before_publish = getattr(authority, 'assert_source_current', None)
                if before_publish is not None: before_publish()
                authority.assert_current()
                if not scope['is_current'](): raise ValueError('History authority changed')
                rename_directory_noreplace(stage_parent, stage_name, parent_fd, name)
                published = True
                named = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
                if (named.st_dev, named.st_ino) != (stage_info.st_dev, stage_info.st_ino): raise ValueError('published skill changed')
                os.fsync(parent_fd)
                stage_info = os.fstat(stage_fd)
                if (named.st_dev, named.st_ino) != (stage_info.st_dev, stage_info.st_ino): raise ValueError('published skill changed')
                after = _strict_directory_manifest(target, expected=stage_info, limits=limits,
                    pinned_root_fd=stage_fd, pinned_parent_fd=parent_fd, pinned_name=name)
                authority.assert_current()
                if not scope['is_current'](): raise ValueError('History authority changed')
                client.record_live(action_id, {'action_id': action_id, 'status': 'Committed',
                    'fingerprint': _fingerprint(after), 'after_unavailable': False})
                item = dict(entries[0]); item.pop('old_locator', None); item['locator'] = item.pop('new_locator')
                item.update(existence='Present', content=after, fingerprint=_fingerprint(after),
                    metadata={'mode': named.st_mode & 0o7777, 'size': None,
                              'modified_millis': named.st_mtime_ns // 1000000, 'opaque': None},
                    outcome={'resource_id': item['resource_key']['resource_id'], 'status': 'Committed',
                             'revision': {'Opaque': {'kind': 'fingerprint', 'value': _fingerprint(after)}}},
                    coverage={'metadata': {'exact_after': True}, 'byte_len': len(after),
                              'content_digest': 'sha256:' + hashlib.sha256(after).hexdigest()})
                if after != _strict_directory_manifest(target, expected=stage_info, limits=limits,
                        pinned_root_fd=stage_fd, pinned_parent_fd=parent_fd, pinned_name=name):
                    raise ValueError('published skill changed')
                completed = client.complete_batch(action_id, [item])
                validate(client, completed['Action'], action_id, authority.owner, authority.account,
                    expected_manifest=after, expected_binding=binding_sha, expected_resource=entries[0]['resource_key'])
                authority.assert_current()
                return {'action_id': action_id, 'skill_id': skill_id, 'name': name,
                        'initial_revision': 1, 'initial_hash': sk.content_hash}
            finally:
                try:
                    if scope is not None: authority._history('close', None, None, None, None, scope)
                finally:
                    try:
                        if stage_fd is not None: os.close(stage_fd)
                    finally:
                        try:
                            if stage_parent is not None: os.close(stage_parent)
                        finally:
                            os.close(parent_fd)
                            if stage_dir and not published: shutil.rmtree(stage_dir, ignore_errors=True)

