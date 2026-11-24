"""Host coordination for one explicitly selected indexed topology capture."""
from __future__ import annotations

import asyncio
import os
import time

from src.openclank.code_query import resolve_query_binding
from src.openclank.graph_topology_codec import _path, validate_receipt


async def capture_current_topology(*, repository, provider, files_provider, files_context,
                                   auth, owner, workspace_id, receipt, paths, limits,
                                   current, deadline):
    """Read the existing published index under original Human and Files scope.

    Indexed evidence is not asserted to equal a later live source read. The
    Files checks retain the selected objects and permission across capture;
    Native owns the published receipt and extraction/coverage proof.
    """
    validate_receipt(receipt)
    if (type(paths) is not list or not paths or any(_path(item) != item for item in paths)
        or paths != sorted(set(paths), key=lambda item:item.encode('utf-8'))
        or type(limits) is not dict or set(limits) != {'files','nodes','edges','work','bytes','page_bytes'}
        or any(type(value) is not int or value <= 0 for value in limits.values())
        or len(paths) > limits['files'] or not callable(current) or current() is not True
        or type(deadline) not in (int,float) or deadline <= time.monotonic()):
        raise ValueError('Topology selection unavailable')
    native, identity = resolve_query_binding(repository=repository, auth_manager=auth,
        owner=owner, workspace_id=workspace_id, purpose='app_folder')
    root = native['root']
    if not os.path.isabs(root) or os.path.realpath(root) != root:
        raise ValueError('Topology workspace unavailable')
    def check():
        if current() is not True:
            raise ValueError('Topology authority changed')
        _, now = resolve_query_binding(repository=repository, auth_manager=auth,
            owner=owner, workspace_id=workspace_id, purpose='app_folder')
        if now != identity:
            raise ValueError('Topology workspace changed')
        return True
    async def files():
        result=[]
        for rel in paths:
            check()
            path=os.path.join(root,*rel.split('/'))
            if os.path.commonpath((root,path)) != root or os.path.realpath(path) != path:
                raise ValueError('Topology source unavailable')
            entry=await files_provider.resource_for_path(files_context,path=path)
            if (entry.kind != 'file' or not {'open','download'}.intersection(entry.capabilities)
                or not isinstance(entry.revision,dict) or not entry.revision):
                raise ValueError('Topology source unavailable')
            result.append((entry.origin_id,entry.kind,dict(entry.revision)))
        check()
        return result
    check()
    async with asyncio.timeout(min(120, max(0, deadline-time.monotonic()))):
        before=await files()
        result=await provider.code_topology(binding=native,request={'version':1,'profile':'indexed_topology_v1',
            'paths':list(paths),'expected_receipt':receipt,'limits':dict(limits)},current=check,deadline=deadline)
        if await files()!=before:
            raise ValueError('Topology source changed')
        check()
        if (not isinstance(result,dict) or type(result.get('bytes')) is not bytes
            or type(result.get('record')) is not dict or result['record'].get('receipt')!=receipt):
            raise ValueError('Topology result unavailable')
        async def before_dispatch():
            check()
            if await files()!=before:
                return False
            fence=await provider.code_read_fence(binding=native, expected_receipt=receipt)
            if not isinstance(fence,dict) or not isinstance(fence.get('fence'),str):
                return False
            return check()
        return {**result, 'before_dispatch':before_dispatch}
