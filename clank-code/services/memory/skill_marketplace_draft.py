"""Human-reviewed marketplace draft, published once through Skills History."""
from __future__ import annotations
import base64
import hashlib
import json
import os
import re
import stat
import uuid

from core.atomic_io import atomic_write_text
from src.openclank.history_capture import StrictTreeLimits
from .skill_format import Skill
from .skill_lifecycle import _safe_bundle_path, ensure_revision
from .skill_draft_once import create_once
from .marketplace_importer import _encoded, draft_name, safe_path

_MARKETPLACE_DRAFT_SEAL = object()
_LIMITS = StrictTreeLimits(192, 10, 1024 * 1024, 4 * 1024 * 1024, 8 * 1024 * 1024, 3.0)
_KEY = re.compile(r'[A-Za-z0-9_-]{22,128}')

class _MarketplaceDraftAuthority:
    __slots__ = ('owner','account','workspace','witness','_current','_history','_before_publish','_seal')
    def __init__(self, seal, *, owner, account, workspace, witness, current, history, before_publish):
        if (seal is not _MARKETPLACE_DRAFT_SEAL or not all(type(x) is str and 0 < len(x.encode()) <= 512 for x in (owner,account,workspace))
                or type(witness) is not dict or not callable(current) or not callable(history) or not callable(before_publish)):
            raise ValueError('marketplace authority unavailable')
        self.owner, self.account, self.workspace = owner, account, workspace
        self.witness = json.loads(_encoded(witness))
        self._current, self._history, self._before_publish, self._seal = current, history, before_publish, seal
    def assert_current(self):
        if self._seal is not _MARKETPLACE_DRAFT_SEAL or self._current() is not True:
            raise ValueError('marketplace authority changed')

    def assert_source_current(self):
        if self._before_publish() is not True: raise ValueError('marketplace source changed')

def _digest(value):
    return hashlib.sha256(_encoded(value)).hexdigest()

def _action(account, key):
    if type(key) is not str or not _KEY.fullmatch(key): raise ValueError('invalid request key')
    return 'skill-marketplace-' + _digest([account,key])

def _verify_review(review, files):
    expected = {'version','catalog','plugin','skill_path','name','category','license','plugin_version','unsupported','files','bundle_sha256','review_sha256'}
    if type(review) is not dict or set(review) != expected or review['version'] != 1 or review['category'] != 'imported':
        raise ValueError('marketplace review unavailable')
    if (type(review['plugin']) is not dict or set(review['plugin']) != {'name','source','manifest_sha256'}
            or type(review['catalog']) is not dict or set(review['catalog']) != {'source','manifest_sha256'}
            or type(files) is not dict or not 1 <= len(files) <= 64 or 'SKILL.md' not in files):
        raise ValueError('marketplace bundle unavailable')
    if review['name'] != draft_name(review['plugin']['name'],review['skill_path']): raise ValueError('marketplace name changed')
    rows=[]; seen=set(); total=0
    for path, value in sorted(files.items()):
        if type(path) is not str or type(value) is not str or safe_path(path) != path or _safe_bundle_path(path) != path:
            raise ValueError('invalid marketplace path')
        if path.casefold() in seen or (path.lower().endswith('skill.md') and path != 'SKILL.md'):
            raise ValueError('ambiguous marketplace path')
        seen.add(path.casefold()); raw=value.encode('utf-8'); total+=len(raw)
        if len(raw)>400000 or total>2000000: raise ValueError('marketplace bundle too large')
        rows.append({'path':path,'size':len(raw),'sha256':hashlib.sha256(raw).hexdigest()})
    unsigned=dict(review); digest=unsigned.pop('review_sha256')
    if rows != review['files'] or _digest(rows) != review['bundle_sha256'] or _digest(unsigned) != digest:
        raise ValueError('marketplace review changed')

def _source(review):
    source = review['plugin']['source']
    if type(source) is not dict: raise ValueError('marketplace source unavailable')
    return 'marketplace:' + _encoded(source).decode('utf-8'), review['plugin']['manifest_sha256'] or review['bundle_sha256']

def add_marketplace_draft(manager, *, authority, request_key, review, files):
    if type(authority) is not _MarketplaceDraftAuthority: raise ValueError('marketplace authority unavailable')
    _verify_review(review,files)
    if authority.witness != review: raise ValueError('marketplace review changed')
    authority.assert_current()
    action_id = _action(authority.account,request_key); skill_id=str(uuid.uuid5(uuid.NAMESPACE_URL,action_id))
    name=review['name']; root=os.path.abspath(manager.skills_root); category=os.path.join(root,'imported')
    if os.path.realpath(root) != root: raise ValueError('skill root unavailable')
    if os.path.lexists(category):
        info=os.lstat(category)
        if not stat.S_ISDIR(info.st_mode) or os.path.realpath(category)!=category: raise ValueError('skill category unavailable')
        parent=category; child=name; capture='imported'; prefix=''
    else:
        parent=root; child='imported'; capture='root'; prefix=name+'/'
    binding={'version':1,'account':authority.account,'owner':authority.owner,'workspace':authority.workspace,
             'skill_id':skill_id,'name':name,'category':'imported','review':review,
             'capture_root':capture,'prefix':prefix,'destination':child}
    binding_sha=_digest(binding); admitted=[]
    def build(stage):
        from src.openclank.attachment_admission import admit_references
        authority.assert_current()
        sk=Skill.from_markdown(files['SKILL.md'])
        if sk.source_memory_ids or str(sk.source_uri or '').startswith('memory-promotion:'):
            raise ValueError('marketplace memory source unavailable')
        manager._assert_memory_sources_available(authority.owner,sk.source_memory_ids,sk.source_uri)
        admitted.append(admit_references(authority.owner,'skills',files))
        dest=os.path.join(stage,name) if capture=='root' else stage
        if capture=='root': os.mkdir(dest)
        for path,content in sorted(files.items()):
            target=os.path.join(dest,path); os.makedirs(os.path.dirname(target),exist_ok=True); atomic_write_text(target,content)
        sk.name=name; sk.category='imported'; sk.owner=authority.owner; sk.source_status=sk.status
        sk.source='imported'; sk.source_uri,sk.source_revision=_source(review); sk.status='draft'; sk.skill_id=skill_id; sk.revision=1
        path=os.path.join(dest,'SKILL.md'); atomic_write_text(path,sk.to_markdown())
        state={'published':None,'marketplace_import':{'version':1,'action_id':action_id,'binding_sha256':binding_sha,
            'account_id':authority.account,'skill_id':skill_id,'initial_revision':1,'initial_hash':sk.content_hash,'binding':binding}}
        ensure_revision(sk,path,state=state)
        return sk
    result=create_once(manager,authority=authority,action_id=action_id,skill_id=skill_id,category_dir=parent,name=child,
        binding_sha=binding_sha,tool_id='marketplace-skill-draft',stage_prefix='.marketplace-skill-',limits=_LIMITS,
        build=build,validate=_validate_completed,status_error='original marketplace draft status required')
    from src.openclank.attachment_admission import settle_references
    if admitted: settle_references(admitted[0])
    result['name']=name
    return result

def _validate_completed(client, action, action_id, owner, account, *, expected_manifest=None, expected_binding=None, expected_resource=None):
    before=action['before_resources']; after=action['after_resources']
    if (action['action_id'] != action_id or action['actor_id'] != owner or action['actor_account_id'] != account
            or action['actor_kind'] != 'human' or action['tool_id'] != 'marketplace-skill-draft'
            or action['operation'] != 'create' or action['state'] != 'Complete' or len(before)!=1 or len(after)!=1):
        raise ValueError('marketplace action unavailable')
    b,a=before[0],after[0]; marker=b['metadata']['opaque']
    if (type(marker) is not dict or set(marker)!={'version','binding_sha256'} or marker['version']!=1
            or not re.fullmatch(r'[0-9a-f]{64}',str(marker['binding_sha256']))
            or action['coverage']['metadata']['batch_once']!=marker or expected_binding is not None and marker['binding_sha256']!=expected_binding
            or b['existence']!='Absent' or b['resource_type']!='Directory' or a['existence']!='Present'
            or a['resource_type']!='Directory' or a['resource_key']!=b['resource_key']
            or expected_resource is not None and b['resource_key']!=expected_resource): raise ValueError('marketplace action unavailable')
    manifest=client.read_version(action_id,a['after'])
    if type(manifest) is not bytes or len(manifest)>_LIMITS.max_manifest_bytes or expected_manifest is not None and manifest!=expected_manifest:
        raise ValueError('marketplace after-state unavailable')
    tree=json.loads(manifest); rows=tree.get('entries') if type(tree) is dict and tree.get('version')==1 and tree.get('root_type')=='directory' else None
    if type(rows) is not list or len(rows)>_LIMITS.max_entries: raise ValueError('marketplace tree unavailable')
    def read(path):
        found=[r for r in rows if type(r) is dict and r.get('path')==path and r.get('type')=='file']
        if len(found)!=1: raise ValueError('marketplace file unavailable')
        row=found[0]; raw=base64.b64decode(row['content'],validate=True)
        if len(raw)>_LIMITS.max_file_bytes or row.get('size')!=len(raw) or row.get('sha256')!=hashlib.sha256(raw).hexdigest(): raise ValueError('marketplace file changed')
        return raw
    lifecycle=[r['path'] for r in rows if type(r) is dict and r.get('type')=='file' and (r.get('path')=='_lifecycle.json' or str(r.get('path','')).endswith('/_lifecycle.json'))]
    if len(lifecycle)!=1: raise ValueError('marketplace lifecycle unavailable')
    prefix=lifecycle[0][:-len('_lifecycle.json')]; state=json.loads(read(lifecycle[0])); witness=state['marketplace_import']; binding=witness['binding']
    name=binding['name']; skill_id=str(uuid.uuid5(uuid.NAMESPACE_URL,action_id)); head=read(prefix+'SKILL.md'); sk=Skill.from_markdown(head.decode('utf-8'))
    capture=binding['capture_root']; dest=binding['destination']; expected_prefix=name+'/' if capture=='root' else ''
    if (capture not in ('root','imported') or prefix!=expected_prefix or binding['prefix']!=prefix
            or dest!=('imported' if capture=='root' else name) or binding['account']!=account or binding['owner']!=owner
            or binding['skill_id']!=skill_id or binding['category']!='imported' or _digest(binding)!=marker['binding_sha256']
            or witness['action_id']!=action_id or witness['account_id']!=account or witness['skill_id']!=skill_id
            or witness['binding_sha256']!=marker['binding_sha256'] or witness['initial_revision']!=1
            or state['skill_id']!=skill_id or state['owner']!=owner or state['published'] is not None
            or state['head_revision']!=1 or state['head_hash']!=witness['initial_hash'] or sk.skill_id!=skill_id
            or sk.owner!=owner or sk.name!=name or sk.category!='imported' or sk.status!='draft' or sk.source!='imported'
            or sk.revision!=1 or sk.content_hash!=witness['initial_hash'] or sk.compute_content_hash()!=witness['initial_hash']
            or (sk.source_uri,sk.source_revision)!=_source(binding['review'])): raise ValueError('marketplace lifecycle unavailable')
    history=state['history']
    if type(history) is not list or len(history)!=1: raise ValueError('marketplace snapshot unavailable')
    row=history[0]
    expected_snapshot = '_revisions/00000001-' + sk.content_hash + '.md'
    if (row['skill_id']!=skill_id or row['owner']!=owner or row['revision']!=1 or row['content_hash']!=sk.content_hash
            or row['snapshot']!=expected_snapshot or row['snapshot_sha256']!=hashlib.sha256(head).hexdigest()
            or read(prefix+expected_snapshot)!=head): raise ValueError('marketplace snapshot unavailable')
    # Review describes the exact input bundle. SKILL.md is deliberately normalized;
    # every companion must retain its original bytes in the captured History tree.
    review=binding['review']; descriptors=review['files']
    if type(descriptors) is not list or not 1 <= len(descriptors) <= 64: raise ValueError('marketplace bundle unavailable')
    expected_files={prefix+'SKILL.md',prefix+'_lifecycle.json',prefix+expected_snapshot}; seen=set()
    for item in descriptors:
        if type(item) is not dict or set(item)!={'path','size','sha256'}: raise ValueError('marketplace bundle unavailable')
        path=item['path']; size=item['size']; digest=item['sha256']
        if (type(path) is not str or safe_path(path)!=path or _safe_bundle_path(path)!=path or path.casefold() in seen
                or path.lower().endswith('skill.md') and path!='SKILL.md' or type(size) is not int or not 0 <= size <= 400000
                or type(digest) is not str or not re.fullmatch(r'[0-9a-f]{64}',digest)):
            raise ValueError('marketplace bundle unavailable')
        seen.add(path.casefold()); expected_files.add(prefix+path)
        if path!='SKILL.md':
            raw=read(prefix+path)
            if len(raw)!=size or hashlib.sha256(raw).hexdigest()!=digest: raise ValueError('marketplace companion changed')
    if 'skill.md' not in seen or _digest(descriptors)!=review['bundle_sha256']:
        raise ValueError('marketplace bundle unavailable')
    unsigned=dict(review); review_digest=unsigned.pop('review_sha256')
    if _digest(unsigned)!=review_digest: raise ValueError('marketplace review unavailable')
    actual_files=[item.get('path') for item in rows if type(item) is dict and item.get('type')=='file']
    expected_dirs=set()
    for path in expected_files:
        parts=path.split('/')
        expected_dirs.update('/'.join(parts[:n]) for n in range(1,len(parts)))
    actual_dirs=[item.get('path') for item in rows if type(item) is dict and item.get('type')=='directory']
    if (len(actual_files)!=len(expected_files) or set(actual_files)!=expected_files
            or len(actual_dirs)!=len(expected_dirs) or set(actual_dirs)!=expected_dirs
            or len(rows)!=len(actual_files)+len(actual_dirs)):
        raise ValueError('marketplace tree changed')
    return {'state':'complete','action_id':action_id,'skill_id':skill_id,'name':name,'initial_revision':1,'initial_hash':sk.content_hash}

def marketplace_draft_status(*, owner, account, request_key, client, current):
    if not callable(current) or current() is not True or type(owner) is not str or not owner or type(account) is not str or not account:
        raise ValueError('marketplace status unavailable')
    action_id=_action(account,request_key); unknown={'state':'unknown','action_id':action_id}
    try:
        result=_validate_completed(client,client.get_capture_action(action_id)['Action'],action_id,owner,account)
        return result if current() is True else unknown
    except Exception:
        return unknown
