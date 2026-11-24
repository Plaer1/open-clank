"""Owner-scoped portable snapshots of an existing published skill."""
from __future__ import annotations

import hashlib
import hmac
import os
import sys
import re
from pathlib import Path, PurePosixPath

from .skill_lifecycle import (
    PreparedReadBudget, PreparedReadUnavailable, load_state,
    prepared_existing_lock, read_bundle_snapshot,
)
from .skill_snapshot_format import from_published_skill, review_package


def _current(current):
    if not callable(current) or current() is not True:
        raise PreparedReadUnavailable('skill export unavailable')


def _preview(manager, *, owner, account, skill_id, current):
    if any(type(v) is not str or not v or v != v.strip() or len(v.encode('utf-8')) > 512
           for v in (owner, account, skill_id)):
        raise PreparedReadUnavailable('skill export unavailable')
    _current(current)
    budget = PreparedReadBudget()
    matches = []
    with prepared_existing_lock(os.path.join(manager.skills_root, 'PROMOTIONS')):
        with os.scandir(manager.skills_root) as categories:
            for category in categories:
                budget.entry()
                if not category.is_dir(follow_symlinks=False) or category.name.startswith('.owner-purge-'):
                    continue
                with os.scandir(category.path) as entries:
                    for entry in entries:
                        budget.entry()
                        if not entry.is_dir(follow_symlinks=False) or entry.name.startswith('.owner-purge-'):
                            continue
                        path = os.path.join(entry.path, 'SKILL.md')
                        if not os.path.isfile(path) or os.path.islink(path):
                            continue
                        with prepared_existing_lock(path):
                            head = manager._read_skill(path, budget=budget)
                            if head is None:
                                raise PreparedReadUnavailable('skill export unavailable')
                            if head.owner != owner or head.skill_id != skill_id:
                                continue
                            state = load_state(path, budget=budget)
                            pointer = state.get('published')
                            if (type(pointer) is not dict or type(pointer.get('publisher')) is not str
                                    or not pointer['publisher'].startswith(('user:', 'admin:'))):
                                raise PreparedReadUnavailable('skill export unavailable')
                            active, pointer = manager._active_skill(path, head, state, budget=budget)
                            if active is None or type(pointer) is not dict:
                                raise PreparedReadUnavailable('skill export unavailable')
                            platform_name = ('windows' if sys.platform.startswith('win')
                                else 'macos' if sys.platform == 'darwin' else 'linux')
                            declared_platforms = {str(item).lower() for item in active.platforms}
                            if declared_platforms and platform_name not in declared_platforms:
                                raise PreparedReadUnavailable('skill export unavailable')
                            bundle = read_bundle_snapshot(path, pointer, budget=budget)
                            if bundle is None:
                                raise PreparedReadUnavailable('skill export unavailable')
                            companions = {}
                            for relative, metadata in bundle['files'].items():
                                if relative == 'SKILL.md':
                                    continue
                                data = budget.read(Path(bundle['root']).joinpath(*PurePosixPath(relative).parts))
                                if len(data) != metadata['size'] or hashlib.sha256(data).hexdigest() != metadata['sha256']:
                                    raise PreparedReadUnavailable('skill export unavailable')
                                companions[relative] = data.decode('utf-8')
                            reference = {'skill_id': active.skill_id, 'revision': active.revision,
                                'content_sha256': active.content_hash,
                                'snapshot_sha256': pointer.get('snapshot_sha256'),
                                'bundle_sha256': pointer.get('bundle_sha256')}
                            package = from_published_skill(active, reference=reference, companions=companions)
                            review = review_package(package, destination_name=package['skill']['name'])
                            matches.append({'package': package, 'review': review})
    _current(current)
    if len(matches) != 1:
        raise PreparedReadUnavailable('skill export unavailable')
    return matches[0]


def prepare_skill_snapshot_export(manager, *, owner, account, skill_id, current):
    return _preview(manager, owner=owner, account=account, skill_id=skill_id, current=current)


def confirm_skill_snapshot_export(manager, *, owner, account, skill_id, review_sha256, current):
    if type(review_sha256) is not str or re.fullmatch(r'[0-9a-f]{64}', review_sha256) is None:
        raise PreparedReadUnavailable('skill export unavailable')
    result = _preview(manager, owner=owner, account=account, skill_id=skill_id, current=current)
    if not hmac.compare_digest(result['review']['review_sha256'], review_sha256):
        raise PreparedReadUnavailable('skill export changed')
    _current(current)
    return result['package']
