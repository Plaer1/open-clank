"""Pinned public GitHub reader for marketplace review; no donor execution."""
from __future__ import annotations
import asyncio
import hashlib
import time
from dataclasses import replace
from services.memory import skill_importer as s

async def github_reader(url, *, current):
    if not callable(current) or await current() is not True: raise ValueError('Marketplace authority unavailable')
    s._validate_user_github_url(url)
    source = await _drained(lambda:s._resolve_pinned_source(s.parse_skill_source(url)))
    if await current() is not True: raise ValueError('Marketplace authority unavailable')
    if source.kind != 'directory':
        raise ValueError('Marketplace source must be a directory')
    return GithubReader(source, current)

async def _drained(fn):
    task=asyncio.create_task(asyncio.to_thread(fn)); cancelled=False
    while True:
        try: value=await asyncio.shield(task); break
        except asyncio.CancelledError:
            cancelled=True
            if task.done(): value=task.result(); break
    if cancelled: raise asyncio.CancelledError
    return value

class GithubReader:
    def __init__(self, source, current):
        self.source = source
        self.identity = {'kind': 'github', 'url': source.canonical_uri}
        self._reads = 0
        self._current = current
        self._deadline = time.monotonic()+120
        self._observed = {}

    def _path(self, relative):
        from services.memory.marketplace_importer import safe_path
        relative = safe_path(relative, empty=True)
        return '/'.join(x for x in (self.source.path, relative) if x)

    async def _get(self, url, limit):
        if time.monotonic()>self._deadline or await self._current() is not True: raise ValueError('Marketplace authority unavailable')
        self._reads += 1
        if self._reads > 256: raise ValueError('Marketplace read limit exceeded')
        def fetch():
            response = s._get_checked(url, headers={'Accept':'application/vnd.github+json'}, timeout=30.0,
                max_bytes=limit, allowed_hosts=s._GITHUB_HOSTS, require_https=True)
            s._assert_github_url(str(response.url), context='redirect target')
            return response
        response=await _drained(fetch)
        if time.monotonic()>self._deadline or await self._current() is not True: raise ValueError('Marketplace authority unavailable')
        return response

    async def read(self, relative):
        from services.memory.marketplace_importer import MarketplaceNotFound, safe_path
        relative=safe_path(relative)
        parent,_,name=relative.rpartition('/')
        rows=await self.list(parent)
        found=[x for x in rows if x['name']==name]
        if not found: raise MarketplaceNotFound(relative)
        if len(found)!=1 or found[0]['kind']!='file': raise ValueError('Marketplace file unavailable')
        response = await self._get(s._raw_url(self.source, self._path(relative)), s.MAX_FILE_BYTES)
        if response.status_code >= 400: raise s._github_response_error(response)
        data=bytes(response.content)
        self._observe(('read',relative), hashlib.sha256(data).hexdigest())
        return data

    def _observe(self,key,value):
        if key in self._observed and self._observed[key]!=value: raise ValueError('Marketplace source changed')
        self._observed[key]=value

    async def list(self, relative):
        from services.memory.marketplace_importer import MarketplaceNotFound, safe_path
        relative=safe_path(relative,empty=True)
        if relative:
            parent,_,name=relative.rpartition('/')
            rows=await self.list(parent)
            found=[x for x in rows if x['name']==name]
            if not found: raise MarketplaceNotFound(relative)
            if len(found)!=1 or found[0]['kind']!='directory': raise ValueError('Marketplace directory unavailable')
        response = await self._get(s._api_contents_url(self.source, self._path(relative)), s.MAX_METADATA_BYTES)
        if response.status_code == 404: raise ValueError('Marketplace directory unavailable')
        if response.status_code >= 400: raise s._github_response_error(response)
        value = response.json()
        if type(value) is not list or len(value) >= 1000: raise ValueError('Incomplete marketplace listing')
        rows = []
        for item in value:
            if type(item) is not dict or type(item.get('name')) is not str or item.get('type') not in ('file','dir') or item.get('target') is not None or item.get('submodule_git_url') is not None:
                raise ValueError('Unsupported marketplace entry')
            from services.memory.marketplace_importer import safe_path
            name=safe_path(item['name'])
            if '/' in name or item.get('path') != '/'.join(x for x in (self._path(relative),name) if x): raise ValueError('Invalid marketplace listing')
            rows.append({'name':name, 'kind':'directory' if item['type']=='dir' else 'file'})
        if len({x['name'].casefold() for x in rows})!=len(rows): raise ValueError('Ambiguous marketplace listing')
        rows.sort(key=lambda x:x['name'])
        self._observe(('list',relative),rows)
        return rows

    async def finish(self):
        s._require_pinned_ref(self.source)
        observed=list(self._observed.items())
        for (kind,path),expected in observed:
            if kind=='list': await self.list(path)
            elif kind=='read': await self.read(path)
            else:
                from services.memory.marketplace_importer import MarketplaceNotFound
                try: await self.list(path)
                except MarketplaceNotFound: continue
                raise ValueError('Marketplace source changed')
        if await self._current() is not True: raise ValueError('Marketplace authority unavailable')
