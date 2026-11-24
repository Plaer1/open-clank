"""One recipient-owned draft from a reviewed portable skill snapshot."""
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
from .skill_draft_once import create_once
from .skill_format import Skill, _now_iso
from .skill_lifecycle import ensure_revision
from .skill_snapshot_format import canonical_bytes, validate_package, package_sha256, review_package, _path

_SKILL_SNAPSHOT_DRAFT_SEAL = object()
_LIMITS = StrictTreeLimits(512, 10, 1024*1024, 4*1024*1024, 8*1024*1024, 3.0)
_KEY = re.compile(r'[A-Za-z0-9_-]{22,128}')
_FIELDS = ('description','version','tags','platforms','when_to_use','procedure','pitfalls','verification','body_extra')

class _SkillSnapshotDraftAuthority:
    __slots__ = ('owner','account','workspace','witness','_current','_history','_seal')
    def __init__(self, seal, *, owner, account, workspace, witness, current, history):
        if (seal is not _SKILL_SNAPSHOT_DRAFT_SEAL or not all(type(x) is str and 0 < len(x.encode()) <= 512 for x in (owner,account,workspace))
                or type(witness) is not dict or not callable(current) or not callable(history)):
            raise ValueError('skill snapshot authority unavailable')
        self.owner,self.account,self.workspace=owner,account,workspace
        self.witness=json.loads(canonical_bytes(witness))
        self._current,self._history,self._seal=current,history,seal
    def assert_current(self):
        if self._seal is not _SKILL_SNAPSHOT_DRAFT_SEAL or self._current() is not True:
            raise ValueError('skill snapshot authority changed')

def _digest(value): return hashlib.sha256(canonical_bytes(value)).hexdigest()
def _action(account,key):
    if type(key) is not str or not _KEY.fullmatch(key): raise ValueError('invalid request key')
    return 'skill-snapshot-'+_digest([account,key])

def _skill(package, review, *, owner, skill_id):
    authored=package['skill']; name=review['destination']['name']
    skill=Skill(name=name,skill_id=skill_id,revision=1,category='imported',owner=owner,status='draft',source='imported',created=_now_iso(),
        source_uri='skill-snapshot:'+review['package_sha256'],source_revision=str(package['origin']['revision']),
        source_status='imported_unverified', **{key:authored[key] for key in _FIELDS})
    # The actual native parser/serializer must preserve the selected authored fields.
    encoded=skill.to_markdown()
    if len(encoded.encode('utf-8'))>256*1024: raise ValueError('native skill exceeds reader limit')
    parsed=Skill.from_markdown(encoded)
    if any(getattr(parsed,key)!=authored[key] for key in _FIELDS): raise ValueError('native skill fields unavailable')
    return skill

def add_skill_snapshot_draft(manager, *, authority, request_key, package, review):
    if type(authority) is not _SkillSnapshotDraftAuthority: raise ValueError('skill snapshot authority unavailable')
    package=validate_package(package)
    if type(review) is not dict or type(review.get('destination')) is not dict: raise ValueError('skill snapshot review unavailable')
    checked=review_package(package,destination_name=review['destination'].get('name'))
    if checked!=review or authority.witness!=review or package_sha256(package)!=review['package_sha256']:
        raise ValueError('skill snapshot review changed')
    authority.assert_current(); action_id=_action(authority.account,request_key); skill_id=str(uuid.uuid5(uuid.NAMESPACE_URL,action_id))
    name=review['destination']['name']; skill=_skill(package,review,owner=authority.owner,skill_id=skill_id)
    root=os.path.abspath(manager.skills_root); category=os.path.join(root,'imported')
    if os.path.realpath(root)!=root: raise ValueError('skill root unavailable')
    if os.path.lexists(category):
        info=os.lstat(category)
        if not stat.S_ISDIR(info.st_mode) or os.path.realpath(category)!=category: raise ValueError('skill category unavailable')
        parent=category; child=name; capture='imported'; prefix=''
    else:
        parent=root; child='imported'; capture='root'; prefix=name+'/'
    # Check the exact staged file/directory topology before reserving History.
    paths={prefix+'SKILL.md',prefix+'_lifecycle.json',prefix+'_revisions/00000001-'+skill.content_hash+'.md'}
    paths.update(prefix+item['path'] for item in package['files'])
    directories=set()
    for path in paths:
        parts=path.split('/')
        directories.update('/'.join(parts[:n]) for n in range(1,len(parts)))
    if len(paths)+len(directories)>_LIMITS.max_entries: raise ValueError('skill snapshot tree exceeds limit')
    binding={'version':1,'owner':authority.owner,'account':authority.account,'workspace':authority.workspace,
        'skill_id':skill_id,'name':name,'category':'imported','package_sha256':review['package_sha256'],
        'review_sha256':review['review_sha256'],'origin':review['origin'],'source_skill_name':review['skill']['name'],
        'authored_sha256':_digest(review['skill']),'files':review['files'],
        'capture_root':capture,'prefix':prefix,'destination':child}
    # Lifecycle adds a bounded revision row and framing; reserve space for it.
    if len(json.dumps(binding,indent=2).encode('utf-8'))>240*1024: raise ValueError('skill snapshot witness exceeds reader limit')
    binding_sha=_digest(binding); admitted=[]
    def build(stage):
        from src.openclank.attachment_admission import admit_references
        authority.assert_current()
        admitted.append(admit_references(authority.owner,'skills',package['skill'],[item['utf8'] for item in package['files']]))
        dest=os.path.join(stage,name) if capture=='root' else stage
        if capture=='root': os.mkdir(dest)
        for item in package['files']:
            target=os.path.join(dest,item['path']); os.makedirs(os.path.dirname(target),exist_ok=True); atomic_write_text(target,item['utf8'])
        path=os.path.join(dest,'SKILL.md'); atomic_write_text(path,skill.to_markdown())
        state={'published':None,'skill_snapshot_import':{'version':1,'action_id':action_id,'binding_sha256':binding_sha,
            'account_id':authority.account,'skill_id':skill_id,'initial_revision':1,'initial_hash':skill.content_hash,'binding':binding}}
        ensure_revision(skill,path,state=state)
        return skill
    result=create_once(manager,authority=authority,action_id=action_id,skill_id=skill_id,category_dir=parent,name=child,
        binding_sha=binding_sha,tool_id='skill-snapshot-draft',stage_prefix='.skill-snapshot-',limits=_LIMITS,
        build=build,validate=_validate_completed,status_error='original skill snapshot status required')
    from src.openclank.attachment_admission import settle_references
    if admitted: settle_references(admitted[0])
    result['name']=name
    return result

def _validate_completed(client,action,action_id,owner,account,*,expected_manifest=None,expected_binding=None,expected_resource=None):
    before=action['before_resources']; after=action['after_resources']
    if (action['action_id']!=action_id or action['actor_id']!=owner or action['actor_account_id']!=account
            or action['actor_kind']!='human' or action['tool_id']!='skill-snapshot-draft' or action['operation']!='create'
            or action['state']!='Complete' or type(before) is not list or len(before)!=1 or type(after) is not list or len(after)!=1):
        raise ValueError('skill snapshot action unavailable')
    b,a=before[0],after[0]; marker=b['metadata']['opaque']
    if (type(marker) is not dict or set(marker)!={'version','binding_sha256'} or marker['version']!=1
            or not re.fullmatch(r'[0-9a-f]{64}',str(marker['binding_sha256'])) or action['coverage']['metadata']['batch_once']!=marker
            or expected_binding is not None and marker['binding_sha256']!=expected_binding
            or b['existence']!='Absent' or b['resource_type']!='Directory' or a['existence']!='Present' or a['resource_type']!='Directory'
            or a['resource_key']!=b['resource_key'] or expected_resource is not None and b['resource_key']!=expected_resource):
        raise ValueError('skill snapshot action unavailable')
    manifest=client.read_version(action_id,a['after'])
    if type(manifest) is not bytes or len(manifest)>_LIMITS.max_manifest_bytes or expected_manifest is not None and manifest!=expected_manifest:
        raise ValueError('skill snapshot after-state unavailable')
    tree=json.loads(manifest); rows=tree.get('entries') if type(tree) is dict and tree.get('version')==1 and tree.get('root_type')=='directory' else None
    if type(rows) is not list or len(rows)>_LIMITS.max_entries: raise ValueError('skill snapshot tree unavailable')
    def read(path):
        found=[row for row in rows if type(row) is dict and row.get('path')==path and row.get('type')=='file']
        if len(found)!=1: raise ValueError('skill snapshot file unavailable')
        row=found[0]; raw=base64.b64decode(row['content'],validate=True)
        if len(raw)>_LIMITS.max_file_bytes or row.get('size')!=len(raw) or row.get('sha256')!=hashlib.sha256(raw).hexdigest(): raise ValueError('skill snapshot file changed')
        return raw
    lifecycle=[r['path'] for r in rows if type(r) is dict and r.get('type')=='file' and (r.get('path')=='_lifecycle.json' or str(r.get('path','')).endswith('/_lifecycle.json'))]
    if len(lifecycle)!=1: raise ValueError('skill snapshot lifecycle unavailable')
    prefix=lifecycle[0][:-len('_lifecycle.json')]; state=json.loads(read(lifecycle[0])); witness=state['skill_snapshot_import']; binding=witness['binding']
    name=binding['name']; skill_id=str(uuid.uuid5(uuid.NAMESPACE_URL,action_id)); head=read(prefix+'SKILL.md'); skill=Skill.from_markdown(head.decode('utf-8'))
    capture=binding['capture_root']; expected_prefix=name+'/' if capture=='root' else ''
    if (capture not in ('root','imported') or prefix!=expected_prefix or binding['prefix']!=prefix
            or binding['destination']!=('imported' if capture=='root' else name) or binding['account']!=account or binding['owner']!=owner
            or binding['skill_id']!=skill_id or binding['category']!='imported' or _digest(binding)!=marker['binding_sha256']
            or witness['action_id']!=action_id or witness['account_id']!=account or witness['skill_id']!=skill_id
            or witness['binding_sha256']!=marker['binding_sha256'] or witness['initial_revision']!=1
            or state['skill_id']!=skill_id or state['owner']!=owner or state['published'] is not None
            or state['head_revision']!=1 or state['head_hash']!=witness['initial_hash'] or skill.skill_id!=skill_id
            or skill.owner!=owner or skill.name!=name or skill.category!='imported' or skill.status!='draft' or skill.source!='imported'
            or skill.source_uri!='skill-snapshot:'+binding['package_sha256'] or skill.source_revision!=str(binding['origin']['revision'])
            or skill.source_status!='imported_unverified' or skill.source_memory_ids or skill.revision!=1
            or skill.content_hash!=witness['initial_hash'] or skill.compute_content_hash()!=witness['initial_hash']):
        raise ValueError('skill snapshot lifecycle unavailable')
    authored={'name':binding['source_skill_name'],**{key:getattr(skill,key) for key in _FIELDS}}
    if _digest(authored)!=binding['authored_sha256']: raise ValueError('skill snapshot fields changed')
    unsigned={'version':1,'package_sha256':binding['package_sha256'],'destination':{'name':name,'category':'imported'},
              'origin':binding['origin'],'skill':authored,'files':binding['files']}
    if _digest(unsigned)!=binding['review_sha256']: raise ValueError('skill snapshot review unavailable')
    history=state['history']; snapshot='_revisions/00000001-'+skill.content_hash+'.md'
    if type(history) is not list or len(history)!=1: raise ValueError('skill snapshot revision unavailable')
    row=history[0]
    if (row['skill_id']!=skill_id or row['owner']!=owner or row['revision']!=1 or row['content_hash']!=skill.content_hash
            or row['snapshot']!=snapshot or row['snapshot_sha256']!=hashlib.sha256(head).hexdigest() or read(prefix+snapshot)!=head):
        raise ValueError('skill snapshot revision unavailable')
    descriptors=binding['files']
    if type(descriptors) is not list or len(descriptors)>63: raise ValueError('skill snapshot files unavailable')
    expected={prefix+'SKILL.md',prefix+'_lifecycle.json',prefix+snapshot}; seen=set()
    for item in descriptors:
        if type(item) is not dict or set(item)!={'path','size','sha256'}: raise ValueError('skill snapshot files unavailable')
        path=item['path']; size=item['size']; digest=item['sha256']
        if type(path) is not str or _path(path)!=path or path.casefold() in seen or type(size) is not int or not 0<=size<=262144 or type(digest) is not str or not re.fullmatch(r'[0-9a-f]{64}',digest):
            raise ValueError('skill snapshot files unavailable')
        seen.add(path.casefold()); raw=read(prefix+path)
        if len(raw)!=size or hashlib.sha256(raw).hexdigest()!=digest: raise ValueError('skill snapshot file changed')
        expected.add(prefix+path)
    actual_files=[r.get('path') for r in rows if type(r) is dict and r.get('type')=='file']; expected_dirs=set()
    for path in expected:
        parts=path.split('/'); expected_dirs.update('/'.join(parts[:n]) for n in range(1,len(parts)))
    actual_dirs=[r.get('path') for r in rows if type(r) is dict and r.get('type')=='directory']
    if len(actual_files)!=len(expected) or set(actual_files)!=expected or len(actual_dirs)!=len(expected_dirs) or set(actual_dirs)!=expected_dirs or len(rows)!=len(actual_files)+len(actual_dirs):
        raise ValueError('skill snapshot tree changed')
    return {'state':'complete','action_id':action_id,'skill_id':skill_id,'name':name,'initial_revision':1,'initial_hash':skill.content_hash}

def skill_snapshot_draft_status(*,owner,account,request_key,client,current):
    if not callable(current) or current() is not True or type(owner) is not str or not owner or type(account) is not str or not account:
        raise ValueError('skill snapshot status unavailable')
    action_id=_action(account,request_key); unknown={'state':'unknown','action_id':action_id}
    try:
        result=_validate_completed(client,client.get_capture_action(action_id)['Action'],action_id,owner,account)
        return result if current() is True else unknown
    except Exception:
        return unknown
