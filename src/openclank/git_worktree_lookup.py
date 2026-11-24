"""Owner-scoped, read-only discovery of one completed worktree version."""
from __future__ import annotations
import asyncio
import re
from src.openclank.files_service_client import _WorktreeStageCustody
from src.openclank.git_checkout_authority import issue_guard

async def lookup_worktree_version(*,client,root_path,root_id,root_object_tag,operation_id,current):
    if (type(root_path) is not str or not root_path.startswith('/') or type(root_id) is not str or not root_id
            or type(root_object_tag) is not str or not re.fullmatch(r'[0-9a-f]{64}',root_object_tag)
            or type(operation_id) is not str or not re.fullmatch(r'[A-Za-z0-9_-]{16,80}',operation_id) or not callable(current)):
        raise ValueError('worktree lookup unavailable')
    deadline=asyncio.get_running_loop().time()+10
    if await current() is not True:raise ValueError('worktree lookup unavailable')
    custody=await _WorktreeStageCustody.acquire(client,root_path,operation_id,before_issue=issue_guard(current,deadline=deadline),profile='lookup')
    data=(await custody.exchange('worktree_lookup',{'operation_id':operation_id,'root_id':root_id,'root_path':root_path,'root_object_tag':root_object_tag})).get('data')
    if (type(data) is not dict or set(data)!={'state','versionID','operation'} or data['state']!='ready'
            or data['operation'] not in ('source_worktree_create_v2','source_worktree_inverse_v2','source_worktree_redo_v2')
            or type(data['versionID']) is not str or not data['versionID'] or len(data['versionID'].encode('utf-8','strict'))>256
            or await current() is not True):raise ValueError('worktree lookup unavailable')
    return data
