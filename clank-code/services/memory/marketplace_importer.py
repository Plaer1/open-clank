"""Bounded, staged marketplace inspection for one explicitly selected skill."""
from __future__ import annotations
import hashlib
import json
import re
from services.memory.skill_format import slugify

class MarketplaceNotFound(ValueError):
    """Reader proved that the requested entry is absent."""


def safe_path(path, *, empty=False):
    if type(path) is not str or (not path and not empty) or len(path.encode('utf-8','strict')) > 2048:
        raise ValueError('Invalid marketplace path')
    if path and (path.startswith('/') or '\\' in path or any(x in ('', '.', '..') for x in path.split('/')) or
                 any(ord(c) < 32 or ord(c) == 127 for c in path)):
        raise ValueError('Invalid marketplace path')
    return path


def _hash(data): return hashlib.sha256(data).hexdigest()
def _encoded(value): return json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False).encode('utf-8','strict')
def _text(value, limit=256):
    if type(value) is not str or not value or len(value.encode('utf-8','strict')) > limit or any(ord(c)<32 or ord(c)==127 for c in value):
        raise ValueError('Invalid marketplace text')
    return value

def _json(raw):
    if type(raw) is not bytes or len(raw)>100000: raise ValueError('Manifest too large')
    def pairs(items):
        result={}
        for k,v in items:
            if k in result: raise ValueError('Duplicate manifest key')
            result[k]=v
        return result
    value=json.loads(raw.decode('utf-8','strict'),object_pairs_hook=pairs,parse_constant=lambda _: (_ for _ in ()).throw(ValueError('Invalid JSON')))
    if type(value) is not dict: raise ValueError('Invalid manifest')
    stack=[(value,0)]; count=0
    while stack:
        node,depth=stack.pop(); count+=1
        if count>10000 or depth>32: raise ValueError('Manifest complexity exceeded')
        if type(node) is dict:
            for key,child in node.items():
                _text(key,2048); stack.append((child,depth+1))
        elif type(node) is list: stack.extend((child,depth+1) for child in node)
        elif type(node) is str:
            if len(node.encode('utf-8','strict'))>8192 or any(ord(c)<32 or ord(c)==127 for c in node): raise ValueError('Invalid manifest text')
        elif node is not None and type(node) not in (bool,int,float): raise ValueError('Invalid manifest value')
    return value

async def _manifest(reader,path,optional=False):
    try: raw=await reader.read(path)
    except MarketplaceNotFound:
        if optional: return {}, None
        raise
    return _json(raw),_hash(raw)

def _join(base,path):
    path=safe_path(path.removeprefix('./'),empty=True)
    return safe_path('/'.join(x for x in (base,path) if x),empty=True)

def _source(value):
    if type(value) is str:
        if not value.startswith('./'): return None
        return {'kind':'relative','path':safe_path(value[2:],empty=True)}
    if type(value) is dict and value.get('source')=='git-subdir' and set(value)<= {'source','url','path','ref'}:
        url=_text(value.get('url'),512); path=safe_path(value.get('path')); ref=_text(value.get('ref','main'),256)
        if not re.fullmatch(r'[A-Za-z0-9_.-]+',ref): return None
        if re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+',url): url='https://github.com/'+url
        if not re.fullmatch(r'https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+(?:\.git)?',url): return None
        return {'kind':'github','url':url.removesuffix('.git')+'/tree/'+ref+'/'+path}
    if type(value) is dict and value.get('source')=='github' and set(value)<= {'source','repo','ref'}:
        repo=_text(value.get('repo'),200)
        if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+',repo): return None
        ref=value.get('ref','main'); _text(ref,256)
        if not re.fullmatch(r'[A-Za-z0-9_.-]+',ref): return None
        return {'kind':'github','url':f'https://github.com/{repo}/tree/{ref}'}
    return None

async def catalog(reader):
    manifest,digest=await _manifest(reader,'.claude-plugin/marketplace.json')
    name=_text(manifest.get('name')); owner=manifest.get('owner')
    if type(owner) is not dict: raise ValueError('Invalid marketplace owner')
    _text(owner.get('name'))
    plugins=manifest.get('plugins')
    if type(plugins) is not list or len(plugins)>256: raise ValueError('Invalid marketplace plugins')
    rows=[]; seen=set()
    for entry in plugins:
        if type(entry) is not dict: raise ValueError('Invalid plugin')
        plugin=_text(entry.get('name'))
        if plugin.casefold() in seen: raise ValueError('Duplicate plugin')
        seen.add(plugin.casefold()); source=_source(entry.get('source'))
        raw_source=entry.get('source')
        kind='relative' if type(raw_source) is str else raw_source.get('source') if type(raw_source) is dict else None
        if type(kind) is not str or not re.fullmatch(r'[A-Za-z0-9_.-]{1,64}',kind): kind='unknown'
        rows.append({'name':plugin,'source':source,'supported':source is not None,'source_kind':kind,
                     'unsupported_reason':None if source is not None else 'unsupported_source'})
    await reader.finish()
    return {'version':1,'source':reader.identity,'name':name,'owner':owner['name'],'manifest_sha256':digest,'plugins':rows}

async def _plugin(reader,plugin_name,external_reader_factory):
    manifest,digest=await _manifest(reader,'.claude-plugin/marketplace.json')
    plugins=manifest.get('plugins')
    if type(plugins) is not list or len(plugins)>256 or any(type(x) is not dict for x in plugins): raise ValueError('Invalid marketplace plugins')
    names=[_text(x.get('name')) for x in plugins]
    if len(set(x.casefold() for x in names))!=len(names): raise ValueError('Duplicate plugin')
    _text(manifest.get('name'))
    if type(manifest.get('owner')) is not dict: raise ValueError('Invalid marketplace owner')
    _text(manifest['owner'].get('name'))
    entries=[x for x in plugins if x.get('name')==plugin_name]
    if len(entries)!=1: raise ValueError('Plugin unavailable')
    entry=entries[0]; selected=_source(entry.get('source'))
    if selected is None: raise ValueError('Unsupported plugin source')
    if selected['kind']=='relative': target=reader; base=selected['path']
    else: target=await external_reader_factory(selected['url']); base=''
    metadata,mdigest=await _manifest(target,_join(base,'.claude-plugin/plugin.json'),optional=True)
    if metadata.get('name') not in (None,plugin_name): raise ValueError('Plugin identity mismatch')
    if entry.get('strict',True) is not True: raise ValueError('Unsupported non-strict plugin')
    # Both declarations together have semantics dependent on strict mode;
    # refuse that combination rather than silently choose one.
    if 'skills' in entry and 'skills' in metadata: raise ValueError('Unsupported combined skills declarations')
    declared=entry.get('skills',metadata.get('skills',[]))
    if type(declared) is str: declared=[declared]
    if type(declared) is not list or len(declared)>64 or any(type(x) is not str for x in declared): raise ValueError('Unsupported skills declaration')
    selected_roots=[_join(base,'' if x in ('SKILL.md','./SKILL.md','.', './') else x.removesuffix('/SKILL.md')) for x in declared]
    roots=list(dict.fromkeys(([] if 'skills' in entry else [_join(base,'skills')])+selected_roots))
    if len(set(x.casefold() for x in roots)) != len(roots): raise ValueError('Ambiguous skill roots')
    return manifest,digest,entry,target,base,metadata,mdigest,roots

async def _components(reader,plugin_name,selected):
    manifest,digest,entry,target,base,metadata,mdigest,roots=selected
    declared=entry.get('skills',metadata.get('skills',[]))
    skills=[]; default_missing=False
    for root in roots:
        try: rows=await target.list(root)
        except MarketplaceNotFound:
            if root==_join(base,'skills'): default_missing=True
            continue
        for row in rows:
            if type(row) is not dict or set(row)!={'name','kind'}: raise ValueError('Invalid listing')
            name=safe_path(row['name'])
            if '/' in name: raise ValueError('Invalid listing')
            if name=='SKILL.md' and row['kind']=='file': skills.append(root)
            elif row['kind']=='directory' and root != base:
                child=_join(root,name)
                try: await target.read(_join(child,'SKILL.md'))
                except MarketplaceNotFound: continue
                skills.append(child)
    if not declared and default_missing:
        try: await target.read(_join(base,'SKILL.md'))
        except MarketplaceNotFound: pass
        else: skills.append(base)
    if len(skills)>64 or len(set(x.casefold() for x in skills))!=len(skills): raise ValueError('Ambiguous skills')
    known=('commands','agents','hooks','mcpServers','lspServers','outputStyles','settings','themes','workflows','monitors')
    unsupported=set(k for k in known if k in entry or k in metadata)
    default_names={'commands','agents','hooks','.mcp.json','.lsp.json','output-styles','settings.json','themes','workflows','monitors','bin'}
    try: root_rows=await target.list(base)
    except MarketplaceNotFound: root_rows=[]
    unsupported.update('default:'+row['name'] for row in root_rows if row['name'] in default_names)
    allowed={'name','source','strict','skills','version','license','description','author','homepage','repository','keywords','category','tags'} | set(known)
    unsupported.update('unknown:'+key for key in (set(entry)|set(metadata))-allowed)
    unsupported=sorted(unsupported)
    version=metadata.get('version',entry.get('version'))
    if version is not None: _text(version,256)
    license=metadata.get('license',entry.get('license'))
    if license is not None: _text(license,1024)
    return {'version':1,'source':reader.identity,'plugin_name':plugin_name,'plugin_source':target.identity,'skills':skills,'unsupported':unsupported,'license':license,'plugin_version':version}

async def components(reader,plugin_name,external_reader_factory):
    selected=await _plugin(reader,plugin_name,external_reader_factory)
    view=await _components(reader,plugin_name,selected)
    await selected[3].finish()
    if selected[3] is not reader: await reader.finish()
    return view

def draft_name(plugin_name,skill_path):
    combined=_text(plugin_name)+('-'+safe_path(skill_path).split('/')[-1] if skill_path else '')
    normalized=re.sub(r'[^a-z0-9]+','-',combined.strip().lower()).strip('-')
    if not normalized or len(normalized.encode('utf-8'))>60: raise ValueError('Marketplace destination name unavailable')
    return slugify(combined,fallback='skill')

async def preview(reader,plugin_name,skill_path,external_reader_factory):
    selected=await _plugin(reader,plugin_name,external_reader_factory)
    view=await _components(reader,plugin_name,selected)
    if skill_path not in view['skills']: raise ValueError('Skill unavailable')
    manifest,digest,entry,target,base,metadata,mdigest,roots=selected
    files={}; seen=set(); total=0
    async def walk(path,relative,depth):
        nonlocal total
        if depth>4: raise ValueError('Bundle depth exceeded')
        rows=await target.list(path)
        for row in rows:
            if type(row) is not dict or set(row)!={'name','kind'}: raise ValueError('Invalid listing')
            name=safe_path(row['name'])
            if '/' in name: raise ValueError('Invalid listing')
            rel=safe_path('/'.join(x for x in (relative,name) if x)); key=rel.casefold()
            if key in seen: raise ValueError('Ambiguous bundle paths')
            seen.add(key)
            if row['kind']=='directory': await walk(_join(path,name),rel,depth+1)
            elif row['kind']=='file':
                if rel.lower().endswith('skill.md') and rel != 'SKILL.md': raise ValueError('Ambiguous skill entry')
                if len(files)>=64: raise ValueError('Bundle file limit')
                raw=await target.read(_join(path,name))
                if type(raw) is not bytes or len(raw)>400000: raise ValueError('Invalid bundle file')
                text=raw.decode('utf-8','strict')
                if '\x00' in text: raise ValueError('Binary bundle file')
                total+=len(raw)
                if total>2000000: raise ValueError('Bundle size limit')
                files[rel]=text
            else: raise ValueError('Unsupported bundle entry')
    await walk(skill_path,'',0)
    if 'SKILL.md' not in files: raise ValueError('Skill root unavailable')
    rows=[{'path':p,'size':len(files[p].encode()),'sha256':_hash(files[p].encode())} for p in sorted(files)]
    facts={'version':1,'catalog':{'source':reader.identity,'manifest_sha256':digest},
           'plugin':{'name':plugin_name,'source':target.identity,'manifest_sha256':mdigest},'skill_path':skill_path,
           'name':draft_name(plugin_name,skill_path),'category':'imported','license':view['license'],'plugin_version':view['plugin_version'],'unsupported':view['unsupported'],'files':rows,'bundle_sha256':_hash(_encoded(rows))}
    facts['review_sha256']=_hash(_encoded(facts))
    await target.finish()
    if target is not reader: await reader.finish()
    return facts,files
