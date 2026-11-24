"""Bounded, non-authoritative memory link discovery in workflow Markdown.

A parsed identifier is only a lookup selector; Memory and Files owners must
independently authorize every resolution and every current source resource.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

_MAX_BYTES = 262_144
_MAX_LINKS = 64
_TOKEN = re.compile(r'\[\[memory:([A-Za-z0-9][A-Za-z0-9._~-]{0,255})(?:\|((?:\\.|[^\]\\\r\n])*))?\]\]')
_FENCE = re.compile(r'^ {0,3}(`{3,}|~{3,})(.*)$')


@dataclass(frozen=True)
class MemoryLinks:
    identifiers: tuple[str, ...]
    complete: bool


def memory_link_identifiers(markdown: str) -> MemoryLinks:
    """Read bounded link selectors outside fenced and inline code spans."""
    if not isinstance(markdown, str):
        return MemoryLinks((), False)
    try:
        if len(markdown.encode('utf-8', 'strict')) > _MAX_BYTES or '\x00' in markdown:
            return MemoryLinks((), False)
    except UnicodeError:
        return MemoryLinks((), False)
    found: list[str] = []
    fence: str | None = None
    inline: int | None = None
    lines = markdown.splitlines()
    if lines and lines[0].strip() == '---':
        end = next ( (i for i in range(1, len(lines)) if lines[i].strip() == '---'), None)
        if end is not None:
            lines = lines[end + 1:]
        else:
            # An opening frontmatter delimiter without a close is ambiguous;
            # do not interpret its content as proven Markdown links.
            return MemoryLinks((), False)
    comment = False
    partial = False
    for line in lines:
        marker = _FENCE.match(line) if not comment and inline is None else None
        if marker:
            mark, tail = marker.groups()
            if fence is None:
                fence = mark
            elif mark[0] == fence[0] and len(mark) >= len(fence) and not tail.strip():
                fence = None
            continue
        if fence is not None:
            continue
        pos = 0
        while pos < len(line):
            if comment:
                end = line.find('-->', pos)
                if end < 0:
                    break
                comment = False
                pos = end + 3
                continue
            if inline is None and line.startswith('<!--', pos):
                comment = True
                pos += 4
                continue
            if inline is None and line.startswith('%%', pos):
                end = line.find('%%', pos + 2)
                if end < 0:
                    partial = True
                    break
                pos = end + 2
                continue
            if line[pos] == '\\':
                pos += 2
                continue
            if line[pos] == '`':
                end = pos
                while end < len(line) and line[end] == '`':
                    end += 1
                width = end - pos
                if inline is None:
                    inline = width
                elif inline == width:
                    inline = None
                pos = end
                continue
            if inline is None and (pos == 0 or line[pos-1] != '!') and line.startswith('[[memory:', pos):
                match = _TOKEN.match(line, pos)
                if match:
                    found.append(match.group(1))
                    if len(found) > _MAX_LINKS:
                        return MemoryLinks(tuple(dict.fromkeys(found[:_MAX_LINKS])), False)
                    pos = match.end()
                    continue
            pos += 1
    return MemoryLinks(tuple(dict.fromkeys(found)), fence is None and inline is None and not comment and not partial)


async def read_current_member(*, host, facade, files, root, member, current, max_bytes=_MAX_BYTES):
    """Reauthorize one bound member and compare object identity and bytes."""
    import hashlib
    import os
    from src.openclank.files_host_provider import _origin
    if not await current() or not isinstance(member,dict) or member.get('pending_write') is not None:
        raise ValueError('member unavailable')
    relative=member.get('relative_path')
    if not isinstance(relative,str): raise ValueError('member unavailable')
    path=os.path.abspath(os.path.join(root,*relative.split('/')))
    if os.path.commonpath((root,path))!=root or os.path.realpath(path)!=path: raise ValueError('member changed')
    before=await host.stat_inspection(files,origin_id=_origin(path))
    if (not isinstance(member.get('object_tag'),str) or not re.fullmatch(r'[0-9a-f]{64}',member['object_tag'])
        or not isinstance(before.revision,dict) or before.revision.get('kind')!='hostObjectIdentity'
        or before.revision.get('value')!=member['object_tag'] or before.kind!='file'
        or type(before.size) is not int or not 0<=before.size<=_MAX_BYTES or before.size>max_bytes): raise ValueError('member unavailable')
    observed=await host.stat(files,origin_id=_origin(path)); resource=facade._resource(files,'host',observed,parent_stable_id=None)
    if (resource.get('id')!=member.get('resource_id') or observed.kind!='file' or observed.size!=before.size
        or not isinstance(member.get('revision'),dict) or member['revision']!=observed.revision): raise ValueError('member changed')
    stream=await host.content(files,origin_id=_origin(path))
    if stream.origin_id!=_origin(path) or stream.size!=before.size or stream.etag!=before.revision['value'] or stream.stream is None: raise ValueError('member changed')
    chunks=[];size=0
    async for chunk in stream.stream(0,before.size):
        if not isinstance(chunk,bytes): raise ValueError('member unavailable')
        size+=len(chunk)
        if size>before.size: raise ValueError('member changed')
        chunks.append(chunk)
    content=b''.join(chunks); revision={'kind':'hostFingerprint','value':f'sha256:{hashlib.sha256(content).hexdigest()}:{len(content)}'}
    after=await host.stat_inspection(files,origin_id=_origin(path))
    if (len(content)!=before.size or observed.revision!=revision or after.revision!=before.revision or after.size!=before.size
        or not await current()): raise ValueError('member changed')
    return resource,revision,content,path


async def current_plan_references(request, context: dict, *, session_id: str, block_id: str, owned) -> dict:
    """Derive links from the current human-authorized plan, without an index.

    Human Files reauthorizes the actual member; inaccessible, stale,
    over-budget or unprovable members make the answer explicitly incomplete.
    """
    import asyncio
    import hashlib
    import json
    import os
    from pathlib import Path
    from src.openclank.files_principal import files_principal
    from src.openclank.files_facade import ProviderContext
    from src.openclank.files_host_provider import _origin
    from src.openclank.transcript_projection import get_mimo_state
    from src.openclank.workspace_policy_service import resolve_owned_workspace
    from src.plan_approval import workflow_member_path
    unavailable = {'complete':False,'items':[]}
    if (not isinstance(session_id,str) or not session_id or len(session_id)>256 or
            not isinstance(block_id,str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._~-]{0,255}',block_id)):
        return unavailable
    try:
        state=request.app.state;repository=state.files_policy_repository;auth=state.auth_manager
        account,user,admin=files_principal(request,repository=repository)
        if account!=context['account'] or user!=context['user'] or not await owned(context['current_live']): return unavailable
        plan=get_mimo_state(session_id,owner=user).get('plan_state');assoc=plan.get('artifact_association') if isinstance(plan,dict) else None
        reserved=plan.get('artifact_set') if isinstance(plan,dict) else None
        if (not isinstance(assoc,dict) or not isinstance(reserved,dict) or plan.get('status')=='cleared' or
                assoc.get('account_id')!=account or assoc.get('workspace_id')!=reserved.get('workspace_id') or
                assoc.get('artifact_set_id')!=reserved.get('artifact_set_id') or assoc.get('plan_revision')!=plan.get('revision') or
                assoc.get('plan_digest')!=plan.get('digest') or reserved.get('plan_revision')!=plan.get('revision') or
                reserved.get('plan_digest')!=plan.get('digest')): return unavailable
        workspace_id=assoc['workspace_id'];resolved=resolve_owned_workspace(repository,workspace_id=workspace_id,owner_username=user,auth_manager=auth,purpose='app_folder')
        root=os.path.realpath(resolved.path);generation=repository.generation()
        files=ProviderContext(account,user,generation,admin,workspace_id);facade=state.files_facade_factory(request,files);host=facade._providers.get('host')
        baseline=json.dumps(assoc,sort_keys=True,separators=(',',':'),ensure_ascii=False)
        async def current():
            try:
                now=get_mimo_state(session_id,owner=user).get('plan_state');bound=now.get('artifact_association') if isinstance(now,dict) else None
                return bool(await owned(context['current_live']) and repository.generation()==generation and
                    auth.account_id(user)==account and auth.username_for_account_id(account)==user and
                    isinstance(bound,dict) and now.get('revision')==plan.get('revision') and now.get('digest')==plan.get('digest') and
                    json.dumps(bound,sort_keys=True,separators=(',',':'),ensure_ascii=False)==baseline and
                    resolve_owned_workspace(repository,workspace_id=workspace_id,owner_username=user,auth_manager=auth,purpose='app_folder')==resolved)
            except Exception:return False
        if not await current():return unavailable
        members=assoc.get('members');meta=members.get('metaplan') if isinstance(members,dict) else None
        if not isinstance(members,dict) or not isinstance(meta,dict):return unavailable
        intents=assoc.get('create_intents') or {}
        if not isinstance(intents,dict):return unavailable
        keys=sorted(members);complete=len(keys)<=32 and all(key in members for key in intents);items=[];total=0
        for key in keys[:32]:
            member=members.get(key)
            try:
                if not isinstance(member,dict) or member.get('pending_write') is not None:raise ValueError('member pending')
                role,name=member.get('role'),member.get('name')
                if key!=(role if name is None else role+':'+name) or member.get('relative_path')!=workflow_member_path(meta.get('relative_path'),role,name):raise ValueError('member mismatch')
                resource,revision,content,_path=await read_current_member(host=host,facade=facade,files=files,root=root,member=member,current=current,max_bytes=_MAX_BYTES-total)
                total+=len(content)
                if total>_MAX_BYTES:complete=False;break
                links=memory_link_identifiers(content.decode('utf-8','strict'))
                if not links.complete:complete=False
                if block_id in links.identifiers:items.append({'role':role,'name':name,'resourceRef':resource['ref'],'revision':revision})
            except Exception:complete=False
        if not await current():return unavailable
        return {'complete':complete,'items':items}
    except Exception:
        return unavailable
