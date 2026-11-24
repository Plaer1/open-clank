"""Manager-owned review and dispatch for protected Skill deletion."""
from __future__ import annotations

import hashlib
import json
import os

from src.openclank import history_capture as capture
from src.openclank.resource_metadata import validate_envelope
from .skill_history_delete import remove_reviewed_tree, reviewed_delete_status
from .skill_lifecycle import PreparedReadBudget, _SkillMetadataAuthority, _prepared_unique_pairs, load_state, locked, state_path


# Internal finite profile; a request cannot select weaker limits.
_LIMITS = capture.StrictTreeLimits(max_entries=1024, max_depth=32, max_file_bytes=4*1024*1024,
    max_input_bytes=16*1024*1024, max_manifest_bytes=24*1024*1024, timeout_seconds=4)


def _review_locked(manager, skill_id, owner, authority, path):
    if type(authority) is not _SkillMetadataAuthority: raise ValueError('skill delete unavailable')
    budget = PreparedReadBudget(max_file=8*1024*1024, max_bytes=24*1024*1024, seconds=4)
    head = manager._read_skill(path, budget=budget)
    state = load_state(path, budget=budget)
    head_raw = budget.read(path); lifecycle_raw = budget.read(state_path(path))
    if json.loads(lifecycle_raw.decode('utf-8'), object_pairs_hook=_prepared_unique_pairs) != state:
        raise ValueError('skill delete state changed')
    if (head is None or head.skill_id != skill_id or head.owner != owner or type(state) is not dict
            or any(state.get(k) is not None for k in ('metadata_pending','metadata_audit_pending','metadata_recovery_pending'))):
        raise ValueError('skill delete unavailable')
    manager._qualified_metadata_state(path, head, state, authority, budget=budget)
    active, pointer = manager._active_skill(path, head, state, budget=budget, metadata_authority=authority)
    if active is None or type(pointer) is not dict: raise ValueError('skill delete publication unavailable')
    envelope = validate_envelope(head.resource_metadata)
    refs = envelope['metadata'].get('domain_refs', [])
    authority.assert_current(owner, refs)
    directory = os.path.dirname(os.path.abspath(path)); parent = os.path.dirname(directory); name = os.path.basename(directory)
    if os.path.basename(path) != 'SKILL.md' or os.path.realpath(parent) != parent: raise ValueError('skill delete path unavailable')
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    parent_fd = os.open(parent, flags); tree_fd = None
    try:
        tree_fd = os.open(name, flags, dir_fd=parent_fd)
        parent_info = os.fstat(parent_fd); tree_info = os.fstat(tree_fd)
        named = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if (tree_info.st_dev, tree_info.st_ino) != (named.st_dev, named.st_ino): raise ValueError('skill delete tree changed')
        manifest = capture._strict_directory_manifest(directory, expected=tree_info, limits=_LIMITS,
            pinned_root_fd=tree_fd, pinned_parent_fd=parent_fd, pinned_name=name)
    finally:
        if tree_fd is not None: os.close(tree_fd)
        os.close(parent_fd)
    # This second read binds the review to the same head/lifecycle seen by
    # qualification and the manifest, including its audit/metadata receipts.
    check = PreparedReadBudget(max_file=8*1024*1024, max_bytes=24*1024*1024, seconds=4)
    if check.read(path) != head_raw or check.read(state_path(path)) != lifecycle_raw:
        raise ValueError('skill delete state changed')
    selector = {'skill_id':skill_id,'revision':head.revision,'content_hash':head.content_hash,
        'head_sha256':hashlib.sha256(head_raw).hexdigest(),'lifecycle_sha256':hashlib.sha256(lifecycle_raw).hexdigest(),
        'published':pointer,'tree_sha256':hashlib.sha256(manifest).hexdigest(),
        'directory_identity':[tree_info.st_dev,tree_info.st_ino], 'parent_identity':[parent_info.st_dev,parent_info.st_ino],
        'domain_refs':refs}
    from .skill_history_delete import _review_manifest
    _review_manifest(manifest, selector, owner)
    return selector, head_raw, lifecycle_raw


def preview(manager, skill_id, owner, authority):
    with locked(os.path.join(manager.skills_root, 'PROMOTIONS')):
        found = manager._find_skill(skill_id, owner)
        if not found: raise ValueError('skill delete unavailable')
        path, _ = found
        with locked(path):
            selector, head_raw, _ = _review_locked(manager, skill_id, owner, authority, path)
            from .skill_format import Skill
            return {'state':'ready','name':Skill.from_markdown(head_raw.decode('utf-8')).name,'review':selector}


def remove(manager, skill_id, owner, authority, review, request_key):
    with locked(os.path.join(manager.skills_root, 'PROMOTIONS')):
        found = manager._find_skill(skill_id, owner)
        if not found: raise ValueError('skill delete unavailable; inspect original status')
        path, _ = found
        with locked(path):
            actual, head_raw, lifecycle_raw = _review_locked(manager, skill_id, owner, authority, path)
            if type(review) is not dict or review != actual: raise ValueError('skill delete review changed')
            refs = actual['domain_refs']
            def revalidate(tree_fd, phase):
                if phase not in ('before_capture','before_move','after_move'): return False
                authority.assert_current(owner, refs)
                budget = capture._StrictTreeBudget(_LIMITS)
                for name, expected in (('SKILL.md',head_raw),('_lifecycle.json',lifecycle_raw)):
                    info = os.stat(name, dir_fd=tree_fd, follow_symlinks=False)
                    if capture._read_strict_fd(name, tree_fd, info, budget=budget) != expected: return False
                return True
            result = remove_reviewed_tree(authority=authority, owner=owner, path=path, review=actual,
                request_key=request_key, limits=_LIMITS, revalidate=revalidate)
    # Claim cleanup is safe to defer; a failure must not conceal the committed
    # History result or release an unproven attachment claim.
    try:
        from src.openclank.attachment_admission import refresh_domain_references
        from src.openclank.attachment_inventory import skills_inventory
        refresh_domain_references('skills', lambda: skills_inventory(manager))
        references = 'attempted'
    except Exception:
        references = 'unavailable'
    return {'state':result['state'],'action_id':result['action_id'],'review_sha256':result['review_sha256'], 'reference_refresh':references}


def status(skill_id, owner, authority, request_key, review):
    from .skill_history_delete import _digest
    if type(review) is not dict or review.get('skill_id') != skill_id:
        raise ValueError('skill delete status unavailable')
    return reviewed_delete_status(authority=authority, owner=owner, request_key=request_key, review_sha256=_digest(review))
